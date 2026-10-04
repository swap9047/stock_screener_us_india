"""
AI Stock Expert View Engine. The reasoning model is settings-driven
(`expert_reasoning_model`, default gemini-3.5-flash-lite) with a Gemma ladder
behind it -- see generate_expert_view; this line used to name a hardcoded model
the code has never called.
Evaluates rich quantitative indicators, trend rules, active alert conditions,
and free web news catalysts to produce actionable investor takes.
"""

import os
import re
from datetime import date, datetime, timedelta, timezone

import llm_util
from news_summary import market_window_date
from google.genai import types

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXPERT_VIEWS_FILE = os.path.join(SCRIPT_DIR, "expert_views.json")

# Shared with news_summary and fundamentals_eval -- see llm_util.
_generate_with_timeout = llm_util.generate_with_timeout


def _clean_json_text(text):
    """Strip markdown code blocks from model JSON output."""
    t = (text or "").strip()
    if t.startswith("```json"):
        t = t[7:]
    elif t.startswith("```"):
        t = t[3:]
    if t.endswith("```"):
        t = t[:-3]
    return t.strip()


# The grounded-search ladder for this pipeline's news stage, and the source
# label written to the view for whichever rung answered.
SEARCH_MODEL = "models/gemma-4-26b-a4b-it"
# Last rung of the REASONING ladder (see generate_expert_view).
REASONING_FALLBACK_MODEL = "models/gemma-4-26b-a4b-it"
SEARCH_SOURCE_LABELS = {SEARCH_MODEL: "🔍 Gemma-4-26B (Google Search)"}
# There is no fallback MODEL any more, deliberately. This used to end on
# models/gemma-4-31b-it, which answered 0 of ~63 calls across four runs on
# 2026-09-17/18 (overwhelmingly 500 INTERNAL) while 26b answered ~83% -- a second
# Gemma on the same backend was never an independent failure domain. The ladder is
# three attempts on SEARCH_MODEL instead, each on a different API key
# (llm_util.same_model_tiers + RotatingGeminiClient._pick's `avoid`).


def fetch_gemma_expert_news(client, ticker, market, company_name, is_retry=False):
    """Fetches news specifically for Expert Views: a grounded search on
    three attempts on SEARCH_MODEL, each on a different API key
    (llm_util.same_model_tiers). There is no second model: see the note above the
    constants for why the 31b rung was dropped rather than kept as insurance.

    This was a hand-rolled two-rung loop (26b, then 31b) with no same-model
    retry, so a single transient 429/503 -- the likeliest failure on a ~110-call
    serial run -- demoted the search to the weaker model. On exhaustion it raises
    TimeoutError so the caller's retry queue picks the ticker up later; on the
    retry pass (is_retry) it settles for "no news" instead."""
    from stock_data import get_exchange_label

    # Exchange-local, not UTC. This workflow fires at 03:00/04:00 UTC, which is
    # 23:00 ET the PREVIOUS day, so a UTC date ran one day ahead of the US
    # session being analysed: a run at 2026-08-14 23:33 ET asked for news
    # "between 2026-08-14 and 2026-08-15", dropping Aug 13 entirely and
    # requesting a New York date that had not happened yet. A US ticker's stored
    # news_used from that run contains items dated August 15. India was fine
    # either way (03:00 UTC = 08:30 IST), which is why only US tickers drifted.
    as_of_date = market_window_date(market, [ticker])
    cutoff_date = (datetime.strptime(as_of_date, "%Y-%m-%d")
                   - timedelta(days=EXPERT_NEWS_WINDOW_DAYS)).strftime("%Y-%m-%d")
    exchange = get_exchange_label(market, ticker)

    bare = ticker.rsplit(".", 1)[0] if ticker.endswith(".NS") or ticker.endswith(".BO") else ticker
    name = f"{company_name} ({bare})" if company_name and company_name != ticker else bare

    # 14 days, not 24 hours (owner, 2026-10-04): the job runs nightly, so a
    # 24-hour window dropped a regulator's action the night after it happened,
    # and on 2026-10-04 it found anything at all for 56 of 124 tickers, mostly
    # upcoming dates. Results, guidance and analyst ratings are left out: the
    # Sentiment job searches back to each company's last results for those, and
    # counting them here too would count them twice.
    categories = "; ".join(NEWS_RISK_CATEGORIES)
    prompt = (
        f"You are a financial news researcher. For the {exchange} stock {name}, today is {as_of_date}. "
        f"Report MATERIAL company news published in the last {EXPERT_NEWS_WINDOW_DAYS} days "
        f"(between {cutoff_date} and {as_of_date}): orders and contracts won or lost, mergers and "
        f"acquisitions, product launches or approvals, and especially negative events -- {categories}. "
        "Do not report quarterly results, guidance or analyst ratings; those are covered elsewhere. "
        f"Separately, list scheduled EVENTS in the next 7 days (results date, AGM, record dates). "
        "List items NEWEST FIRST with the exact date of each, and be extremely concise. "
        # The search model refused ~1 in 60 runs because the dates lie past
        # its training data. They are real; it is meant to search, not recall.
        "These dates are real and current: search the web for them, and do not refuse because of "
        "a training cutoff. If there is no material news, output nothing."
    )
    
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(tools=[grounding_tool])

    def _searched(resp):
        # A refusal is not an answer: raise, and the ladder retries on the next
        # attempt (another key). An ordinary error is retryable (is_retryable).
        if search_refused(getattr(resp, "text", "")):
            raise ValueError("search refused: answered from its training cutoff instead of searching")
        return resp

    resp, used = llm_util.run_model_ladder(
        client, prompt, llm_util.same_model_tiers(SEARCH_MODEL),
        lambda m: config, label="expert-search", subject=ticker, timeout=llm_util.SEARCH_TIMEOUT_SECONDS,
        on_success=_searched,
    )
    if used is not None:
        text = (resp.text or "").strip()
        if search_found_nothing(text):
            text = ""
        return (text or "No recent news found."), SEARCH_SOURCE_LABELS.get(used, f"🔍 {used} (Google Search)")
    if not is_retry:
        raise TimeoutError("Search timed out. Add to retry queue.")
    print(f"  [expert search final fallback] {ticker} -> No source available")
    return "No recent news found.", "⚪ No Source"


def _atomic_write_json(path, data):
    """Delegates to stock_data.atomic_write_json -- this used to be a private
    copy in each of the three store modules."""
    from stock_data import atomic_write_json
    atomic_write_json(path, data)


def load_expert_views():
    """The stored AI views, keyed by ticker.

    Raises json_store.DataFileError when the file EXISTS but will not parse; a
    missing file is still {} (nothing has been generated yet, which is normal).

    It used to swallow every exception and return {}. The refresh loops do
    `store = load_*(); store[tk] = view; save_*(store)`, so one unreadable byte
    turned into a store containing only the tickers that run happened to reach --
    and commit-data pushed that to the data repo. Loud beats a silent wipe of
    work that costs hours of API time to regenerate.
    """
    from json_store import read_json_strict
    return read_json_strict(EXPERT_VIEWS_FILE, {})


def save_expert_views(data):
    _atomic_write_json(EXPERT_VIEWS_FILE, data)


def stale_view_fallback(reason):
    """Full-schema 'HOLD/pending' view for a prior verdict that's gone stale
    (regeneration has failed for more than EXPERT_STALE_DAYS). Mirrors
    fundamentals_eval._unknown_fallback so a stuck pipeline stops silently
    displaying an unverified old ACCUMULATE/CAUTION call as current."""
    return {
        "verdict": "HOLD",
        "headline": f"Analysis pending -- {reason}",
        "technical_summary": "Technical data available in table.",
        "catalyst_summary": "N/A",
        "actionable_take": "Review technical indicators in table.",
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "news_used": "",
        "news_source": "⚪ Unknown",
        "model_used": "Error",
    }


def normalize_view(data):
    """Tidy the model's verdict before it is validated: " accumulate " is the
    same answer as "ACCUMULATE". _is_valid_view matches the three verdicts
    exactly, so a case or whitespace slip used to discard a complete analysis
    and keep the ticker Pending. Anything that is not a dict is returned
    untouched -- the ladder rejects that shape (llm_util.json_object)."""
    if isinstance(data, dict) and isinstance(data.get("verdict"), str):
        data["verdict"] = data["verdict"].strip().upper()
    if isinstance(data, dict) and "news_risk" in data:
        r = data.get("news_risk") if isinstance(data.get("news_risk"), dict) else {}
        cat = str(r.get("category") or "").strip().lower()
        # The listed name, exact or shortened ("fraud or accounting"): a
        # model that trims a category must not silently lose the risk.
        category = next((c for c in NEWS_RISK_CATEGORIES if c == cat), None) or next(
            (c for c in NEWS_RISK_CATEGORIES if len(cat) >= 6 and (c.startswith(cat) or cat.startswith(c))), None)
        data["news_risk"] = {
            "material_negative": r.get("material_negative") is True,
            "category": category,
            "date": str(r.get("date"))[:10] if _parse_day(r.get("date")) else None,
            "quote": str(r.get("quote") or "").strip() or None,
        }
    return data


def _is_valid_view(view):
    """Returns True if a view is a real successful analysis (not a 429/error fallback)."""
    if not view:
        return False
    verdict = view.get("verdict")
    if verdict not in ("ACCUMULATE", "HOLD", "CAUTION"):
        return False
    # Test the sentinel this module writes, not the model's own prose. The old
    # check scanned the headline for "429"/"error"/"analysis pending", so a
    # genuine analysis headlined "Breakout above 429 confirms the uptrend" or
    # "Margin pressure from execution errors" was discarded, the prior view was
    # kept, a failure was counted, and the UI showed the ticker as Pending --
    # with a complete analysis sitting unused in the JSON.
    if view.get("model_used") == "Error":
        return False
    if str(view.get("headline") or "").startswith("Analysis pending -- "):
        return False
    return True


# Sentinels the Expert Take search stage writes when it found nothing. An empty
# news_used means the same thing. Shared so the dashboard's enrichment, its cell
# tooltip and the headless alert rows agree on "no news behind this verdict".
EXPERT_NO_NEWS_MARKERS = ("no recent news found", "no news found", "no material news found", "nothing")

# The search model often says "nothing" in a sentence, or refuses outright
# because the requested dates lie past its training data ("I cannot access news
# from the future (October 2026)"). On 2026-10-04, 21 of the 56 stored write-ups
# counted as "news found" were one or the other, so Expert News? said Yes for
# about 60% more tickers than had any news. Matched near the start only: real
# news can mention "no impact" further in.
_REFUSAL_RE = re.compile(
    r"(do not|don't|cannot|can't|unable to) (have )?(access|provide|browse|retrieve|search)"
    r"|from the future|future dates?\b|knowledge cutoff|training data", re.I)
_NOTHING_RE = re.compile(
    r"\bno (material|recent|relevant|significant|new) (news|updates|announcements|items)"
    r"|nothing (is |was )?(reported|found|to report)|outputting nothing"
    r"|there (is|was|were|are) no (material |recent )?news", re.I)


def search_refused(text):
    """True when the search reply is a refusal, not a search: worth a retry."""
    return bool(_REFUSAL_RE.search(str(text or "")[:400]))


def search_found_nothing(text):
    """True when a search reply carries no news: empty, a no-news marker, a
    refusal, or "nothing found" said in a sentence."""
    t = str(text or "").strip()
    low = t.lower().lstrip("<!-* ").rstrip(".")
    if not low or any(low.startswith(m) for m in EXPERT_NO_NEWS_MARKERS):
        return True
    return search_refused(t) or bool(_NOTHING_RE.search(t[:300]))


def expert_view_has_news(view):
    """The "Expert News?" column: did the verdict have any news behind it --
    the 24-hour search (news_used) OR this quarter's checked fundamentals
    (section 4 of the prompt, recorded as quarter_facts_used). Counting only
    the 24-hour search would call a verdict built on last month's results and
    raised guidance "technicals-only"; in a 2026-09-26 trial 5 of 12 read "No"
    that way while their catalyst summaries cited the quarter's numbers."""
    view = view or {}
    if view.get("quarter_facts_used"):
        return True
    return not search_found_nothing(view.get("news_used"))


# --- The verdict: decided in code from the columns (owner, 2026-10-04) ------------
#
# The model's own verdict matched a rule over the existing columns for 97-100
# of 124 tickers, and most of the rest re-weighed those same columns or cited a
# past EPS miss that the forward-looking Sentiment rule deliberately ignores.
# So the columns decide, live (it cannot disagree with the table), and the AI
# explains it and reads the news for what no column sees:
#
#   ACCUMULATE  (Trend Up/Strong Up OR Tech Uptrend Yes) + TA Rules Maintain/Add
#               or Bullish Signal + Sentiment not Negative. One of the two trend
#               reads is enough (owner, same day): TA and Sentiment must agree.
#           or  TA Rules Bullish Signal + Trend not down + Sentiment not Negative.
#               The breakout fires off CONVERGING EMAs, where Trend is usually
#               Mixed, so requiring an Uptrend would never act on TA's entry.
#   CAUTION     2+ points: TA Exit 2 (the flowchart's own exit call), TA Be
#               Cautious 1, Trend Down/Strong Down 1, Sentiment Negative 1.
#   HOLD        otherwise; PENDING when there is no Trend yet.
#
# Overbought RSI is deliberately not a point: in a trend-following read it is
# strength, and "extended, add on a pullback" belongs in the trade plan.
_UP_TRENDS = ("Uptrend", "Strong Uptrend")
_DOWN_TRENDS = ("Downtrend", "Strong Downtrend")
_TA_BULLISH = ("Maintain/Add", "Bullish Signal")
_CAUTION_POINTS = {"Exit": 2, "Be Cautious": 1}


def decide_expert_verdict(row, sentiment):
    """(verdict, reasons) from the row's columns and the guarded Sentiment
    label -- see the rule above. Reasons are short phrases for the hover."""
    row = row or {}
    trend, ta, tu = row.get("trend"), row.get("ta_rules"), row.get("tech_uptrend")
    if not trend:
        return "PENDING", ["Not enough price history for a Trend yet"]
    negative = sentiment == "Negative"
    sent_txt = f"Sentiment: {sentiment or 'Unknown'}"
    ta_txt = f"TA Rules: {ta or 'n/a'}"
    up_reasons = [f"Trend: {trend}", f"Tech Uptrend: {'Yes' if tu else 'No'}", ta_txt, sent_txt]
    if (trend in _UP_TRENDS or tu) and ta in _TA_BULLISH and not negative:
        return "ACCUMULATE", up_reasons
    if ta == "Bullish Signal" and trend not in _DOWN_TRENDS and not negative:
        return "ACCUMULATE", [ta_txt + " (breakout from a converging base)", f"Trend: {trend}", sent_txt]

    caution = []
    if _CAUTION_POINTS.get(ta):
        caution.append((ta_txt, _CAUTION_POINTS[ta]))
    if trend in _DOWN_TRENDS:
        caution.append((f"Trend: {trend}", 1))
    if negative:
        caution.append((sent_txt, 1))
    if sum(n for _, n in caution) >= 2:
        return "CAUTION", [t for t, _ in caution]

    # A Hold: say what kept it from Accumulate (and any single Caution point).
    blockers = []
    if trend not in _UP_TRENDS and not tu:
        blockers.append(f"Trend: {trend} and Tech Uptrend: No")
    if ta not in _TA_BULLISH:
        blockers.append(ta_txt)
    if negative:
        blockers.append(sent_txt)
    return "HOLD", blockers or up_reasons


# --- The news step: a material negative event lowers one step, never raises ------
# Owner, 2026-10-04. Bad news can hit before the chart reacts, good news is
# confirmed by the chart anyway, and the model reads tone upbeat (outlook
# "improving" ~11x as often as "cautious" on 2026-10-03), so letting news RAISE a
# verdict would amplify that bias. Results, guidance and ratings are not here:
# Sentiment decides those.
EXPERT_NEWS_WINDOW_DAYS = 14
NEWS_RISK_CATEGORIES = (
    "fraud or accounting irregularities",
    "regulatory or legal action",
    "lost major contract or customer",
    "management or auditor exit",
    "dilution or a large capital raise",
    "promoter or insider selling or pledging",
    "plant or operations disruption",
    "debt default or credit-rating downgrade",
)
_LOWER = {"ACCUMULATE": "HOLD", "HOLD": "CAUTION", "CAUTION": "CAUTION"}


def _parse_day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def news_risk_active(news_risk, today=None, window=EXPERT_NEWS_WINDOW_DAYS):
    """True when news_risk records a material negative event that can still
    lower the verdict: flagged, in one of NEWS_RISK_CATEGORIES, quoted, and
    dated within the last `window` days (it expires once it would drop out of
    the search window anyway)."""
    r = news_risk or {}
    if r.get("material_negative") is not True or r.get("category") not in NEWS_RISK_CATEGORIES:
        return False
    if not str(r.get("quote") or "").strip():
        return False
    day = _parse_day(r.get("date"))
    today = today or datetime.now(timezone.utc).date()
    return day is not None and 0 <= (today - day).days <= window


def expert_take_for_row(row, view, sentiment=None, today=None):
    """The Expert Take shown and filtered on: the columns' verdict, lowered one
    step by an active news risk from the stored write-up. Computed LIVE, so it
    follows the columns on screen; the write-up only contributes news_risk.
    Returns {"verdict", "base", "reasons", "news_lowered", "news_risk"}."""
    row = row or {}
    sentiment = row.get("sentiment") if sentiment is None else sentiment
    base, reasons = decide_expert_verdict(row, sentiment)
    risk = (view or {}).get("news_risk")
    active = base in _LOWER and news_risk_active(risk, today)
    lowered = active and _LOWER[base] != base
    # news_noted: an active risk on a verdict that is already Caution -- it
    # cannot lower it, but the reader should still see it.
    return {"verdict": _LOWER[base] if lowered else base, "base": base, "reasons": reasons,
            "news_lowered": lowered, "news_noted": active and not lowered,
            "news_risk": risk if active else None}


def is_pending_view(view):
    """True if a record EXISTS but is a failure/pending placeholder rather than
    a real analysis.

    Both _pending_fallback and stale_view_fallback write verdict "HOLD" (there
    is no "pending" member of the verdict vocabulary), so every consumer that
    branched on the verdict string alone rendered those records as a confident
    Hold -- the UI's own "Failed (Retry)" badge was unreachable for exactly the
    records it was written for. One predicate, used by the badge, the row
    field and the dropdown filter, is what keeps those three agreeing.

    Distinct from `not _is_valid_view(view)`, which is also true for a ticker
    that has never been analysed at all -- that one is "Pending", this one is
    "Failed".
    """
    return bool(view) and not _is_valid_view(view)


EXPERT_STALE_DAYS = 4

def _view_age_days(view):
    """Age of a view in days, or None if as_of is missing/unparseable.

    This is a pipeline-health circuit breaker, not a "has the market moved
    on" freshness check: as_of is refreshed every time generation succeeds
    (even if the verdict is unchanged), so a healthy nightly run keeps this
    at ~0 regardless of how long the same verdict has held. It only climbs
    past EXPERT_STALE_DAYS when regeneration has been failing for several
    consecutive nights, at which point the stale verdict should stop being
    displayed as current."""
    as_of = (view or {}).get("as_of")
    if not as_of:
        return None
    try:
        ts = datetime.fromisoformat(as_of.replace(" ", "T", 1))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400
    except Exception:
        return None


# The verdict rule and the news step, in words. Module constants because the
# prompt AND the copy-for-AI payload in app.py quote them -- two copies would
# drift the moment the rule is tuned. Kept in step with decide_expert_verdict
# and news_risk_active (checks/test_expert_take_100426.py).
VERDICT_RULES = """HOW THE VERDICT IS DECIDED (in code, from the dashboard's columns -- not by the model):
- ACCUMULATE: Trend is Uptrend or Strong Uptrend, OR Tech Uptrend is Yes (one is enough), AND TA Rules is "Maintain/Add" or "Bullish Signal", AND Sentiment is not Negative. Or: TA Rules is "Bullish Signal" (a breakout from converging EMAs, where Trend is usually Mixed), Trend is not a Downtrend, and Sentiment is not Negative.
- CAUTION: at least 2 points, where TA Rules "Exit" counts 2 (the flowchart's own exit call), TA Rules "Be Cautious" 1, Trend Downtrend/Strong Downtrend 1, and Sentiment Negative 1.
- HOLD: everything else. Overbought RSI is not a Caution point: in a trend it is strength."""

NEWS_RISK_RULES = f"""THE NEWS STEP: the model reads the last {EXPERT_NEWS_WINDOW_DAYS} days of material company news. A dated, quoted NEGATIVE event in one of these categories lowers the verdict ONE step (Accumulate -> Hold, Hold -> Caution) until the item is more than {EXPERT_NEWS_WINDOW_DAYS} days old: {"; ".join(NEWS_RISK_CATEGORIES)}. News never raises the verdict, and quarterly results, guidance and analyst ratings are not news here -- Sentiment decides those."""


def _fmt(v, suffix="", digits=1):
    """A metric for the prompt: rounded, with a unit, or "N/A"."""
    if v is None or v == "":
        return "N/A"
    try:
        return f"{float(v):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(v)


def _ta_rules_text(row_data):
    """One line on the TA Rules column: the verdict and the flowchart node that
    decided it, as of the last completed week."""
    verdict = row_data.get("ta_rules")
    d = row_data.get("ta_rules_detail") or {}
    if not verdict or not d:
        return "N/A (not enough completed weekly history)"
    fast, mid, slow = d.get("periods") or (10, 20, 40)
    path = (f"EMAs converging ({d.get('spread_pct')}% spread)" if d.get("converging")
            else f"EMAs not converging ({d.get('spread_pct')}% spread)")
    test = (d.get("decided_by") or {}).get("test")
    decided = {"slow": f"broke the {slow}W EMA", "mid": f"broke the {mid}W EMA", "fast": f"broke the {fast}W EMA",
               "support": "broke a support zone", "resistance": "broke a resistance zone"}.get(test, "no line broken")
    return f"{verdict} (week ending {d.get('week', '?')}: {path}, {decided}; a break = weekly close {d.get('break_pct')}% past the line)"


def _quarter_fundamentals_text(view):
    """Section 4 of the prompt: the Sentiment pipeline's checked facts for the
    current quarter -- EPS, guidance, management outlook, named-firm analyst
    actions -- found by its own search, which reaches back to the company's last
    reported results (fundamentals_eval.search_window_for). The Expert Take search looks
    at the last 24 HOURS only, so before this the model never saw a result or
    guidance change older than a day, and 43 of 124 verdicts on 2026-09-26 were
    technicals-only. Sentiment never reads Expert Take, so this adds no loop.

    Facts, not Sentiment's Positive/Negative label: the label would just be
    copied, and the two columns are meant to be read side by side. The same
    guards as the Sentiment cell apply: a stale, unconfirmed-quarter or
    data-less view is withheld, and an analyst action is kept only with a
    named brokerage (fundamentals_eval._normalize_analyst)."""
    from fundamentals_eval import _validate_sentiment, _normalize_analyst, _field_is_placeholder
    if view is None:
        return "Not provided for this run -- treat as NO INFORMATION, not as evidence of no news."
    if not view:
        return "None on file for this ticker."
    _sentiment, flag = _validate_sentiment(view)
    if flag in ("STALE", "STALE_QUARTER", "NO_DATA"):
        why = {"STALE": "the analysis is out of date", "STALE_QUARTER": "a newer quarterly report exists that it did not cover",
               "NO_DATA": "no earnings, guidance or analyst facts were found"}[flag]
        return f"Not usable -- {why}. Treat as NO INFORMATION."
    v = _normalize_analyst(dict(view))
    lines = []
    if v.get("earnings_report_date"):
        lines.append(f"- Results announced: {v['earnings_report_date']}")
    if not _field_is_placeholder(v.get("earnings_summary")):
        lines.append(f"- Earnings: {v['earnings_summary']}")
    if v.get("eps_value"):
        lines.append(f"- EPS: {v['eps_value']}")
    sales = v.get("revenue_yoy_pct")
    if isinstance(sales, (int, float)) and not isinstance(sales, bool):
        source = " (Yahoo)" if "Yahoo" in str(v.get("revenue_yoy_source") or "") else ""
        lines.append(f"- Sales YoY: {sales:+.0f}%{source}")
    yoy = v.get("profit_yoy_pct")
    if isinstance(yoy, (int, float)) and not isinstance(yoy, bool):
        source = " (Yahoo)" if "Yahoo" in str(v.get("profit_yoy_source") or "") else ""
        lines.append(f"- Profit YoY: {yoy:+.0f}% ({v.get('profit_metric') or 'profit'}){source}")
    if v.get("results_vs_estimate") in ("beat", "miss", "inline"):
        lines.append(f"- Results vs consensus estimates: {v['results_vs_estimate']}")
    if v.get("guidance_vs_consensus") in ("above", "below", "inline"):
        source = f" ({v['guidance_consensus_quote']})" if v.get("guidance_consensus_quote") else ""
        lines.append(f"- Guidance vs consensus: {v['guidance_vs_consensus']}{source}")
    if v.get("guidance_change") or not _field_is_placeholder(v.get("future_guidance")):
        change = f"{v['guidance_change'].upper()} -- " if v.get("guidance_change") else ""
        lines.append(f"- Company guidance: {change}{v.get('future_guidance') or ''}".rstrip(" -"))
    if v.get("outlook_tone") and v.get("outlook_quote"):
        lines.append(f"- Management outlook ({v['outlook_tone']}): {v['outlook_quote']}")
    if v.get("analyst_action") and v.get("analyst_firm"):
        lines.append(f"- Analyst action: {v['analyst_action']} by {v['analyst_firm']}")
    if not lines:
        return "Nothing specific found since the last reported results."
    return "\n".join(lines) + f"\n(Checked {v.get('as_of', '?')} UTC.)"


def build_expert_prompt(row_data, news_text, active_alerts_text=None, fundamental_view=None):
    # These two used to be hardcoded as "> 3 wks" and ">= 1.4x" in the prompt
    # text below, but both are settings-driven -- and this repo has run
    # tech_uptrend_volume_ratio well below 1.4, so the model was being told Tech
    # Uptrend implied a 1.4x volume expansion when it did not. Both are stated
    # the way stock_data tests them: strictly greater.
    from stock_data import load_settings as _ls
    _s = _ls()
    tu_weeks = _s.get("tech_uptrend_min_vstop_weeks", 3)
    tu_vol = _s.get("tech_uptrend_volume_ratio", 0.3)
    # Periods from Settings, like the table's column labels. The daily averages
    # are SIMPLE moving averages (stock_data: rolling().mean()); the prompt
    # used to call them "Daily EMAs ... DEMA" with the periods hard-coded.
    w_fast, w_mid, w_slow = _s.get("ema_weekly", [10, 20, 40])
    d_fast, d_mid, d_slow = _s.get("ema_daily", [10, 50, 200])
    ticker = row_data.get("ticker", "UNKNOWN")
    company_name = row_data.get("company_name", ticker)
    market = row_data.get("market", "us_invested")
    last_close = row_data.get("last_close", "N/A")
    ema10 = row_data.get("ema10", "N/A")
    ema20 = row_data.get("ema20", "N/A")
    ema40 = row_data.get("ema40", "N/A")
    ema10_daily = row_data.get("ema10_daily", "N/A")
    ema50 = row_data.get("ema50", "N/A")
    ema200 = row_data.get("ema200", "N/A")
    rsi_d = row_data.get("rsi14_daily", "N/A")
    rsi_w = row_data.get("rsi14_weekly", "N/A")
    rsi_m = row_data.get("rsi14_monthly", "N/A")
    rs_d = row_data.get("rs_daily", "N/A")
    rs_w = row_data.get("rs_weekly", "N/A")
    rs_m = row_data.get("rs_monthly", "N/A")
    trend = row_data.get("trend", "N/A")
    trend_rank = row_data.get("trend_rank", "N/A")
    trend_detail = row_data.get("trend_detail") or {}
    vstop_weekly = row_data.get("vstop_weekly", "N/A")
    vstop_dir = row_data.get("vstop_weekly_direction", "N/A")
    vstop_wks = row_data.get("vstop_weekly_weeks_since_change", "N/A")
    tech_uptrend = "YES" if row_data.get("tech_uptrend") else "NO"
    vol_10d = row_data.get("avg_volume_10d", "N/A")
    vol_100d = row_data.get("avg_volume_100d", "N/A")
    med_10d = row_data.get("median_volume_10d", "N/A")
    med_100d = row_data.get("median_volume_100d", "N/A")
    vol_trend = row_data.get("volume_trend", "N/A")
    net_vol_dir = row_data.get("net_volume_10d_dir", "N/A")
    net_vol_ratio = row_data.get("net_volume_10d_ratio", "N/A")
    h52 = row_data.get("week52_high", "N/A")
    l52 = row_data.get("week52_low", "N/A")
    # Only a flag the user set by hand. Every other flag is ticker_notes'
    # auto-vote, which counts the PREVIOUS Expert Take verdict as one of its
    # four votes -- and section 3 below presents it as the user's own flag. So
    # an ACCUMULATE helped paint the row Green, and the next night the model was
    # told "the user flagged this Green": each verdict propped up the next. On
    # 2026-09-26 none of the 124 flags was manual and 106 cited Expert Take.
    # Notes are left out as well, at the user's request.
    from ticker_notes import MANUAL_FLAG_REASON
    flag = row_data.get("flag") if row_data.get("flag_reason") == MANUAL_FLAG_REASON else None
    flag = flag or "None"
    from stock_data import benchmark_display_for_row
    bench = benchmark_display_for_row(row_data)
    
    from stock_data import exchange_session
    currency = "INR" if exchange_session(ticker) == "INDIA" else "USD"
    data_end = row_data.get("data_end", "N/A")
    fundamentals_text = _quarter_fundamentals_text(fundamental_view)
    has_fundamentals = fundamentals_text.startswith("- ")

    # The verdict is decided in code from the columns (decide_expert_verdict);
    # the model only explains it and reports news risk. Sentiment is the row's
    # guarded label when the caller attached it, else guarded here.
    sentiment = _row_sentiment(row_data, fundamental_view)
    base_verdict, verdict_reasons = decide_expert_verdict(row_data, sentiment)
    lowered_verdict = _LOWER.get(base_verdict, base_verdict)
    tu_detail = (row_data.get("tech_uptrend_detail") or {}).get("passed") or {}
    tu_failed = [k.replace("_", " ") for k, ok in tu_detail.items() if not ok]
    neutral = [name for key, name in (("slope_neutral", "slope"), ("ma_neutral", "10W vs 40W"),
                                      ("rs_neutral", "RS")) if trend_detail.get(key)]

    # Flag whether news is genuinely absent
    news_absent = not news_text or news_text.strip().lower() in (
        "no recent news found.", "no recent news found", "", "none"
    )
    if news_absent and not has_fundamentals:
        news_quality_note = (
            f"⚠️ NEWS DATA: ABSENT — no material news was found for this ticker in the last "
            f"{EXPERT_NEWS_WINDOW_DAYS} days, nor fundamentals for this quarter. Say so in the catalyst summary."
        )
    elif news_absent:
        news_quality_note = (f"No material news in the last {EXPERT_NEWS_WINDOW_DAYS} days. "
                             "This quarter's fundamentals are in section 4.")
    else:
        news_quality_note = ""

    # "None" reads to the model as "every rule was checked and none fired" --
    # a positive signal. For the life of this feature no caller passed anything
    # here, so that claim was made on every single analysis without a single
    # rule ever having been evaluated. These three cases are now distinct:
    # text = rules fired, "" = evaluated and none fired, None = not evaluated.
    if active_alerts_text is None:
        alerts_section = ("Not evaluated for this run -- treat as NO INFORMATION about alert "
                          "rules, not as evidence that nothing triggered.")
    elif not str(active_alerts_text).strip():
        alerts_section = "None -- every enabled rule was evaluated for this ticker and none is currently true."
    else:
        alerts_section = str(active_alerts_text)

    prompt = f"""You are an equity analyst writing a short, disciplined note on {company_name} (Ticker: {ticker}) ({market} market)
for a growth-and-momentum investor, from the dashboard's columns, the key levels, this quarter's checked
fundamentals and the last {EXPERT_NEWS_WINDOW_DAYS} days of material news below.

======================================================================
1. THE COLUMNS AND THE VERDICT THEY GIVE
======================================================================
- Trend: {trend} (Rank: {trend_rank}). Price > {w_slow} WEMA: {trend_detail.get('price_above_ma')}, {w_slow} WEMA slope rising: {trend_detail.get('slope_rising')}, {w_fast} > {w_slow} WEMA: {trend_detail.get('ema_aligned')}, RS positive: {trend_detail.get('rs_positive')}{f"; too close to call, not voting: {', '.join(neutral)}" if neutral else ""}
- Tech Uptrend: {tech_uptrend} (Requires VStop uptrend > {tu_weeks} wks, Price > {w_slow} WEMA, median Vol 10D > {tu_vol}x median Vol 100D){f"; failing: {', '.join(tu_failed)}" if tu_failed else ""}
- TA Rules (TheWrap weekly EMA flowchart): {_ta_rules_text(row_data)}
- Sentiment (forward-looking, from section 4): {sentiment}
- VERDICT FROM THE COLUMNS: {base_verdict} -- {"; ".join(verdict_reasons)}

{VERDICT_RULES}

======================================================================
1b. KEY LEVELS (for the technical note and the trade plan)
======================================================================
- Data as of: {data_end} (last daily close; prices in {currency}); Last Close: {last_close}
- Weekly EMAs: {w_fast} WEMA={ema10}, {w_mid} WEMA={ema20}, {w_slow} WEMA={ema40}
- Daily SMAs (Fast/Mid/Slow): {d_fast} DSMA={ema10_daily}, {d_mid} DSMA={ema50}, {d_slow} DSMA={ema200}
- VStop-W: Direction={vstop_dir}, Stop Level={vstop_weekly}, Weeks Held={vstop_wks}
- RSI: Daily={rsi_d}, Weekly={rsi_w}, Monthly={rsi_m}; Mansfield RS (vs {bench}): Daily={rs_d}, Weekly={rs_w}, Monthly={rs_m}
- Volume: median day 10D={med_10d}, 100D={med_100d} (averages {vol_10d} / {vol_100d}); Vol Trend={vol_trend}; Net Volume 10D: {net_vol_dir}, {net_vol_ratio}%
- 52-Week Range: High={h52}, Low={l52}
- Valuation: Trailing P/E={_fmt(row_data.get('trailing_pe'))}, Forward P/E={_fmt(row_data.get('forward_pe'))}, P/B={_fmt(row_data.get('pb_ratio'))}, EV/EBITDA={_fmt(row_data.get('ev_ebitda'))}, ROE={_fmt(row_data.get('roe'), '%')}, ROCE={_fmt(row_data.get('roce'), '%')}
- Latest quarter ({row_data.get('reported_qtr') or 'N/A'}) YoY growth: EPS={_fmt(row_data.get('qtr_eps_growth'), '%')}, Net profit={_fmt(row_data.get('qtr_profit_growth'), '%')}, Revenue={_fmt(row_data.get('qtr_revenue_growth'), '%')} (context only: Sentiment judges the fundamentals)

======================================================================
2. ACTIVE ALERT RULES TRIGGERED
======================================================================
{alerts_section}

======================================================================
3. USER FLAG (set by hand)
======================================================================
- Flag: {flag}

======================================================================
4. THIS QUARTER'S FUNDAMENTALS (results, guidance, analyst actions since the last reported results, checked)
======================================================================
{fundamentals_text}

======================================================================
5. MATERIAL NEWS, LAST {EXPERT_NEWS_WINDOW_DAYS} DAYS, AND EVENTS IN THE NEXT 7 (via Grounded Search)
======================================================================
{news_quality_note}
{news_text}

======================================================================
INSTRUCTIONS
======================================================================
You do not choose the verdict: the columns give {base_verdict}. Your two jobs:

A. NEWS RISK. {NEWS_RISK_RULES}
   Report one in "news_risk" ONLY if section 5 shows a specific, dated negative event of one of those
   categories, and copy the news's own words into "quote". If you report one, the verdict will be
   lowered one step to {lowered_verdict}. Anything else (no such event, or a general worry) is
   "material_negative": false with the other fields null.

B. THE NOTE, for the final verdict ({base_verdict}, or {lowered_verdict} if you reported a news risk):
1. A 1-line headline with the key reason.
2. Technical & volume assessment (2-3 sentences), from sections 1 and 1b.
3. Catalyst assessment covering sections 4 and 5 -- if both are empty, say "No material news found;
   verdict based on the columns only."
4. Actionable take (2-3 sentences): entry/add zones, trailing stop levels, or exit triggers from the key
   levels. If RSI is overbought, say to add on a pullback rather than chase. Write it as analysis for the
   reader's own research, not as personal investment advice, and do not state certainty about future prices.
Do not argue against the verdict; explain it.

Return ONLY a valid JSON object matching this schema:
{{
  "headline": "Short 1-line summary statement",
  "technical_summary": "Concise technical/volume takeaway",
  "catalyst_summary": "Concise news/catalyst takeaway",
  "actionable_take": "Clear actionable plan for the reader's research",
  "news_risk": {{
    "material_negative": true | false,
    "category": one of the categories listed above, or null,
    "date": "YYYY-MM-DD" or null,
    "quote": "the news's own words, with the source and date" or null
  }}
}}"""
    return prompt


# Default for generate_expert_view's fundamental_view: read the ticker's view
# from fundamentals.json. None is a different, deliberate value -- "not
# available for this run" -- which section 4 renders as NO INFORMATION.
_LOAD_FUNDAMENTALS = object()


def _row_sentiment(row_data, fundamental_view):
    """The guarded Sentiment label for the verdict: the row's own when the
    caller attached it (the dashboard), else guarded from the view."""
    if (row_data or {}).get("sentiment") is not None:
        return row_data["sentiment"]
    if not fundamental_view:
        return "Unknown"
    from fundamentals_eval import _validate_sentiment
    return _validate_sentiment(fundamental_view)[0]


def generate_expert_view(client, row_data, news_text=None, news_source=None, active_alerts_text=None, is_retry=False,
                         fundamental_view=_LOAD_FUNDAMENTALS):
    from google.genai import types
    from datetime import datetime, timezone

    ticker = row_data.get("ticker", "UNKNOWN")
    market = row_data.get("market", "us_invested")
    company_name = row_data.get("company_name", ticker)
    if not row_data.get("trend"):
        # No Trend yet (too little price history): the verdict is Pending and
        # needs no write-up, so spend no model calls on it.
        return stale_view_fallback("not enough price history for a Trend yet")

    if news_text is None:
        try:
            news_text, news_source = fetch_gemma_expert_news(client, ticker, market, company_name, is_retry=is_retry)
        except TimeoutError:
            # Let the requeue signal through -- see the matching comment in
            # fundamentals_eval.generate_fundamental_view. TimeoutError is an
            # OSError is an Exception, so the blanket handler below swallowed
            # the raise at fetch_gemma_expert_news's ladder end, leaving
            # refresh_expert_views' retry_queue permanently empty and its whole
            # retry phase unreachable.
            raise
        except Exception as e:
            print(f"  [expert news fetch failed/timeout] {ticker}: {e} -> Proceeding with technical evaluation only")
            news_text, news_source = "No recent news found.", "⚪ No Source"

    # Section 4 (this quarter's fundamentals). A caller looping over many
    # tickers passes the view it already loaded; anyone else gets it read here,
    # so no call path silently goes without it.
    # A corrupt fundamentals.json must not stop Expert Take: it is the
    # Sentiment job's file, and that job already fails loudly on it.
    if fundamental_view is _LOAD_FUNDAMENTALS:
        from fundamentals_eval import load_fundamentals
        try:
            fundamental_view = load_fundamentals().get(ticker) or {}
        except Exception as e:
            print(f"  [expert] {ticker}: fundamentals.json unreadable ({e}) -> section 4 not provided")
            fundamental_view = None
    prompt = build_expert_prompt(row_data, news_text, active_alerts_text, fundamental_view)

    from stock_data import load_settings
    settings = load_settings()
    model = settings.get("expert_reasoning_model", "models/gemini-3.5-flash-lite")
    budget = settings.get("expert_thinking_budget", 8192)

    def _pending_fallback(reason, used_model="Error"):
        # catalyst_summary used to be set to `news_text` -- the raw,
        # unsummarised search output -- so a failed analysis rendered a wall of
        # scraped headlines in the field the UI labels "catalyst summary".
        # news_used is the field that holds raw news, and it was missing here
        # entirely, so any consumer doing view["news_used"] hit a KeyError on
        # exactly the fallback records.
        return {
            "verdict": "HOLD",
            "headline": f"Analysis pending -- {reason}",
            "technical_summary": "Technical data available in table.",
            "catalyst_summary": "N/A",
            "actionable_take": "Review technical indicators in table.",
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "news_used": news_text,
            "news_source": news_source or "⚪ Unknown",
            "model_used": used_model,
        }

    if not model.startswith("models/"):
        model = f"models/{model}"

    def _config_for(m):
        kwargs = {"response_mime_type": "application/json"}
        # Gemma rejects a thinking config.
        if "gemma" not in m:
            if isinstance(budget, str):
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=budget)
            else:
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
        return types.GenerateContentConfig(**kwargs)

    # The good model twice (a short backoff between), then Gemma. The middle
    # tier is new: this used to demote on the FIRST exception, so a single
    # transient 429 -- the likeliest failure on a serial ~110-ticker run --
    # permanently dropped that ticker to a weaker model for the night. The
    # ladder also stops early now on a terminal error instead of burning every
    # tier on a bad key or an exhausted quota.
    #
    # The fallback is 26b alone. gemma-4-31b-it used to sit ahead of it, and
    # it answered 0 of ~63 calls on the search side (llm_util.same_model_tiers),
    # so every exhausted primary spent a full call timeout on it first.
    tiers = llm_util.standard_tiers(model, REASONING_FALLBACK_MODEL)
    data, used = llm_util.run_model_ladder(
        client, prompt, tiers, _config_for, label="expert", subject=ticker,
        on_success=lambda resp: normalize_view(llm_util.json_object(_clean_json_text(resp.text))),
    )
    if used is not None:
        # The verdict is the columns', lowered one step by an active news risk
        # (expert_take_for_row) -- stored with the note so the write-up says
        # which verdict it was written for. The app recomputes it live.
        if "news_risk" not in data or search_found_nothing(news_text):
            # No field, or no news found: a "risk" with nothing behind it is
            # the model's imagination, so it never lowers a verdict.
            data["news_risk"] = normalize_view({"news_risk": None})["news_risk"]
        take = expert_take_for_row(row_data, data, sentiment=_row_sentiment(row_data, fundamental_view))
        if take["base"] == "PENDING":
            return _pending_fallback("not enough price history for a Trend yet")
        data["base_verdict"], data["verdict"] = take["base"], take["verdict"]
        data["verdict_reasons"] = take["reasons"]
        data["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        data["news_used"] = news_text
        data["quarter_facts_used"] = _quarter_fundamentals_text(fundamental_view).startswith("- ")
        data["news_source"] = news_source or "⚪ Unknown"
        data["model_used"] = model.split("/")[-1] if used == model else f"{used.split('/')[-1]} (Fallback)"
        return data
    return _pending_fallback("reasoning ladder exhausted")


def carry_news_risk(view, old_view, today=None):
    """Keep last night's still-active news risk when tonight's write-up has
    none. The search varies a lot night to night (it found anything at all for
    35 of 124 tickers on 2026-10-04), so without this a downgrade flickered off
    the first night the search missed the event, though it was still inside
    the 14-day window. A new active risk replaces the old one; an expired one
    is dropped. Returns `view`, updated in place."""
    if news_risk_active((view or {}).get("news_risk"), today):
        return view
    old = (old_view or {}).get("news_risk")
    if news_risk_active(old, today):
        view["news_risk"] = {**old, "carried_from": old.get("carried_from") or old_view.get("as_of")}
    return view


def resolve_persisted_view(view, old_view):
    """Which view should actually be stored, given a freshly generated `view`
    and whatever is already on disk. Returns the view to write, or None meaning
    "keep what's there".

    Four cases, in order:
      - fresh view is valid                  -> write it
      - invalid, prior is valid and fresh    -> keep the prior (return None)
      - invalid, prior is valid but stale    -> write an honest pending
                                                placeholder, so a pipeline that
                                                has been failing for days stops
                                                showing an unverified old
                                                verdict as current
      - invalid, no usable prior             -> write the failure, so the UI can
                                                render "Failed (Retry)" instead
                                                of nothing at all

    Extracted so the nightly batch and the dashboard's per-ticker re-analyze
    button cannot disagree. They did: the batch applied all four rules, while
    analyze_single_ticker wrote whatever it got. A transient API failure behind
    that button therefore replaced a good ACCUMULATE with a
    HOLD/"Analysis pending"/model_used="Error" stub -- destroying the verdict
    rather than keeping it. The Sentiment counterpart already guarded against
    this, so the two features behaved differently on the same click.
    """
    if _is_valid_view(view):
        return carry_news_risk(view, old_view)
    if _is_valid_view(old_view):
        age = _view_age_days(old_view)
        if age is not None and age > EXPERT_STALE_DAYS:
            return stale_view_fallback(f"previous view is {age:.1f} days old")
        return None
    return view


def apply_regenerated_view(store, ticker, view):
    """Put a freshly generated `view` into `store` under the persistence rules
    of resolve_persisted_view. Returns True if the store changed.

    The one place a regenerated view enters a store, so the nightly batch, the
    per-ticker button and the dashboard's bulk re-analyze cannot disagree. The
    bulk path used to do `if _is_valid_view(view): store[tk] = view` and
    otherwise keep the prior unconditionally -- so a prior that had aged past
    EXPERT_STALE_DAYS was never replaced by the honest pending placeholder the
    other two paths write."""
    to_store = resolve_persisted_view(view, store.get(ticker))
    if to_store is None:
        return False
    store[ticker] = to_store
    return True


def analyze_single_ticker(ticker, row_data, api_key, active_alerts_text=None, is_retry=True, client=None,
                          fundamental_view=_LOAD_FUNDAMENTALS):
    """Regenerate one ticker's Expert Take and persist it.

    Returns the stored view, or None when generation failed and the existing
    view was kept (same contract as
    fundamentals_eval.analyze_single_ticker_sentiment).

    `client` lets a caller looping over tickers reuse one RotatingGeminiClient --
    see analyze_single_ticker_sentiment for why that matters."""
    client = client or llm_util.make_client(api_key)
    view = generate_expert_view(client, row_data, active_alerts_text=active_alerts_text, is_retry=is_retry,
                                fundamental_view=fundamental_view)
    all_views = load_expert_views()
    if not apply_regenerated_view(all_views, ticker, view):
        return None
    save_expert_views(all_views)
    return all_views[ticker]

# Trigger Streamlit Cloud hot-reload
