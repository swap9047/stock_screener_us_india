"""
AI Stock Expert View Engine. The reasoning model is settings-driven
(`expert_reasoning_model`, default gemini-3.5-flash-lite) with a Gemma ladder
behind it -- see generate_expert_view; this line used to name a hardcoded model
the code has never called.
Evaluates rich quantitative indicators, trend rules, active alert conditions,
and free web news catalysts to produce actionable investor takes.
"""

import os
from datetime import datetime, timedelta, timezone

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
    cutoff_date = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    exchange = get_exchange_label(market, ticker)

    bare = ticker.rsplit(".", 1)[0] if ticker.endswith(".NS") or ticker.endswith(".BO") else ticker
    name = f"{company_name} ({bare})" if company_name and company_name != ticker else bare

    prompt = (
        f"You are a financial news researcher. For the {exchange} stock {name} -- "
        f"search for recent institutional analyst ratings, upgrades/downgrades, press releases, "
        f"and major upcoming catalysts (e.g., earnings (latest quarter only), product launches). "
        f"Today is {as_of_date}. Report NEWS published between {cutoff_date} and {as_of_date} (the last 24 hours), "
        f"AND separately any scheduled EVENTS in the next 3-4 days. "
        "Report any material items you find, specifying the exact date of each item. Be extremely concise. "
        "If there is no material news, output nothing."
    )
    
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(tools=[grounding_tool])

    resp, used = llm_util.run_model_ladder(
        client, prompt, llm_util.same_model_tiers(SEARCH_MODEL),
        lambda m: config, label="expert-search", subject=ticker, timeout=llm_util.SEARCH_TIMEOUT_SECONDS,
    )
    if used is not None:
        text = (resp.text or "").strip()
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
    text = str(view.get("news_used") or "").strip().lower().rstrip(".")
    if not text:
        return False
    return not any(text.startswith(m) for m in EXPERT_NO_NEWS_MARKERS)


def chart_rule_verdict(row):
    """What the chart alone says, in Expert Take's words: ACCUMULATE when Trend
    is up and Tech Uptrend is Yes, CAUTION when Trend is down, HOLD otherwise.

    The app marks an Expert Take verdict that DIFFERS from this with ⚑. On
    2026-09-26 the model agreed with it for 93 of 124 tickers, so the verdict
    was mostly the chart restated; the ⚑ points at the ~30 where the model
    added a view of its own (news, earnings, guidance), which are the ones
    worth reading."""
    row = row or {}
    trend = row.get("trend")
    if trend in ("Uptrend", "Strong Uptrend") and row.get("tech_uptrend"):
        return "ACCUMULATE"
    if trend in ("Downtrend", "Strong Downtrend"):
        return "CAUTION"
    return "HOLD"


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

# Minimum weeks the weekly VStop must have held UP for an ACCUMULATE, per
# VERDICT_RULES clause (b). Kept next to the guard that enforces it.
ACCUMULATE_MIN_VSTOP_WEEKS = 3


def validate_verdict(view, row):
    """Deterministic post-hoc guard on the model's verdict, mirroring
    fundamentals_eval._validate_sentiment.

    Returns (verdict, flag) -- flag is "" when the verdict stands, or
    "UNSUPPORTED_ACCUMULATE" when ACCUMULATE was returned without the
    technical preconditions VERDICT_RULES makes mandatory -- clauses (a) trend,
    (b) VStop up >= 3 weeks and (c) RS positive, i.e. every one of the four
    that is reconstructible from the row; (d) "no negative news catalyst" is a
    judgement about the news text and is not -- in which case the verdict is
    demoted to the rules' own stated default, HOLD.

    VERDICT_RULES was enforced by prompt compliance alone, even though every
    input it names is already a structured field on the snapshot row. Replaying
    the guard over the 49 stored ACCUMULATE verdicts caught two: a US
    ticker with trend=Downtrend and an India ticker with VStop up only 1 week. 4% is a low
    rate, but it was unbounded and unmonitored, and it moves with any model
    swap in the ladder.

    Only ACCUMULATE is checked for support. HOLD is the rules' default and
    needs no evidence, and CAUTION's "at least two of five signals" includes
    news judgement that isn't reconstructible from the row.

    Staleness is checked first and applies to every verdict, returning the
    non-verdict sentinel "PENDING" so consumers fall through to their existing
    pending branch. resolve_persisted_view already ages out a view while the
    nightly refresh is RUNNING and failing; this covers the case it cannot --
    the workflow not running at all (disabled schedule, GitHub's 60-day
    inactivity auto-disable, an expired key), where nothing writes and a
    month-old ACCUMULATE would otherwise keep displaying as current. Sentiment
    has had this at read time all along; Expert Take had it only at write time.
    """
    if not view:
        return None, ""
    age = _view_age_days(view)
    if age is not None and age > EXPERT_STALE_DAYS:
        return "PENDING", "STALE"
    if not row:
        return view.get("verdict"), ""
    if view.get("verdict") != "ACCUMULATE":
        return view.get("verdict"), ""

    if row.get("trend") not in ("Uptrend", "Strong Uptrend"):
        return "HOLD", "UNSUPPORTED_ACCUMULATE"
    if row.get("vstop_weekly_direction") != "Up":
        return "HOLD", "UNSUPPORTED_ACCUMULATE"
    weeks = row.get("vstop_weekly_weeks_since_change")
    if isinstance(weeks, (int, float)) and weeks < ACCUMULATE_MIN_VSTOP_WEEKS:
        return "HOLD", "UNSUPPORTED_ACCUMULATE"
    # Clause (c). Both this and the weeks test above fail OPEN on a missing or
    # non-numeric value, which is the rule's own wording -- "RS is positive or
    # N/A for very new data" -- not an oversight: a ticker with too little
    # history to compute Mansfield RS must not be demoted for it.
    rs = row.get("rs_weekly")
    if isinstance(rs, (int, float)) and rs < 0:
        return "HOLD", "UNSUPPORTED_ACCUMULATE"
    return "ACCUMULATE", ""


def verdict_flag_note(flag, as_of="unknown"):
    """Plain-English note for a validate_verdict flag, for the cell tooltip."""
    if flag == "UNSUPPORTED_ACCUMULATE":
        return ("[Downgraded to Hold: the model returned Accumulate, but the "
                "trend/VStop/RS preconditions in its own rules were not met]")
    if flag == "STALE":
        return (f"[STALE: as_of {as_of} is older than {EXPERT_STALE_DAYS} days -- "
                "the refresh pipeline has not updated this verdict, so it is no "
                "longer shown as current]")
    return ""

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


# The decision rules the model must follow when picking a verdict. Kept as a
# module constant rather than inline in build_expert_prompt because the
# copy-for-AI payload in app.py quotes these same rules back to the user --
# two copies would silently drift the moment the prompt is tuned, and the
# payload would then be describing a decision rule the model never saw.
VERDICT_RULES = """MANDATORY VERDICT RULES — apply these strictly before choosing a verdict:
- HOLD is the DEFAULT. Use it whenever the picture is mixed, data is thin, or confidence is low.
- Trend "Mixed" means its four conditions disagree (see Trend Detail): it is neither an uptrend for ACCUMULATE nor a Downtrend signal for CAUTION.
- ACCUMULATE requires ALL of: (a) Trend is "Uptrend" or "Strong Uptrend", (b) VStop direction is UP held ≥ 3 weeks, (c) RS is positive or N/A for very new data, (d) No negative news catalyst. If news is ABSENT, you may still give ACCUMULATE ONLY if ALL technical conditions above are clearly met — never give ACCUMULATE just because news is absent.
- CAUTION requires AT LEAST TWO of the following five signals to agree — a single isolated signal (e.g. trend just not yet confirmed as an uptrend, with everything else neutral or positive) is NOT enough on its own and must fall through to HOLD instead: (1) Trend is Downtrend or Strong Downtrend (not Mixed), (2) VStop flipped DOWN, (3) RSI > 80 on weekly or monthly (severely overbought), (4) heavy distribution (Net Volume 10D Negative with large ratio), (5) a clearly negative news catalyst.
- NEVER give ACCUMULATE when news shows a negative catalyst (earnings miss, downgrade, regulatory issue, fraud, etc.).
- NEVER give ACCUMULATE solely because news is absent or minimal — absent news → lean HOLD unless technicals fully satisfy the ACCUMULATE criteria above.
- "News" in these rules means BOTH section 4 (this quarter's fundamentals) and section 5 (the last 24 hours). LOWERED guidance, an EPS miss, or a named-firm downgrade in section 4 is a negative catalyst; RAISED guidance or a named-firm upgrade is a positive one."""


# The deterministic guard validate_verdict applies on top of whatever the model
# wrote. Spelled out for the copy-for-AI payload for the same reason
# VERDICT_RULES is -- the reader is told these rules produced the verdict, so
# they must also be told the verdict is not raw model output.
VERDICT_GUARD_RULES = f"""A deterministic guard runs after the model answers and can override it:
- View older than {EXPERT_STALE_DAYS} days -> shown as Pending (STALE): the refresh pipeline has stopped updating this verdict, so it is no longer presented as current.
- ACCUMULATE without the mandatory technical preconditions -- trend not Uptrend/Strong Uptrend, VStop not UP, VStop held < {ACCUMULATE_MIN_VSTOP_WEEKS} weeks, or weekly RS negative -> demoted to HOLD (UNSUPPORTED_ACCUMULATE).
- HOLD and CAUTION are not re-checked: HOLD is the rules' own default, and CAUTION's five-signal test includes news judgement that cannot be reconstructed from the metrics."""


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
    # the way stock_data tests them: strictly greater. (VERDICT_RULES' ">= 3
    # weeks" is a different rule -- ACCUMULATE's own precondition, enforced by
    # validate_verdict -- not Tech Uptrend's.)
    from stock_data import load_settings as _ls
    _s = _ls()
    tu_weeks = _s.get("tech_uptrend_min_vstop_weeks", 3)
    tu_vol = _s.get("tech_uptrend_volume_ratio", 1.4)
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

    # Flag whether news is genuinely absent
    news_absent = not news_text or news_text.strip().lower() in (
        "no recent news found.", "no recent news found", "", "none"
    )
    if news_absent and not has_fundamentals:
        news_quality_note = (
            "⚠️ NEWS DATA: ABSENT — no material news was found for this ticker, in the last 24 hours "
            "or in this quarter's fundamentals. This MUST constrain the verdict (see rules below)."
        )
    elif news_absent:
        news_quality_note = "No news in the last 24 hours. This quarter's fundamentals are in section 4."
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

    prompt = f"""You are an elite equity portfolio manager combining Stan Weinstein stage analysis, trend momentum,
volume accumulation/distribution analysis, and fundamental catalyst evaluation.

Analyze the stock {company_name} (Ticker: {ticker}) ({market} market) using the structured quantitative metrics, this
quarter's checked fundamentals and recent web news provided below.

======================================================================
1. QUANTITATIVE & TECHNICAL METRICS
======================================================================
- Data as of: {data_end} (last daily close; prices in {currency})
- Last Close: {last_close}
- Weekly EMAs (Fast/Mid/Slow): {w_fast} WEMA={ema10}, {w_mid} WEMA={ema20}, {w_slow} WEMA={ema40}
- Daily SMAs (Fast/Mid/Slow): {d_fast} DSMA={ema10_daily}, {d_mid} DSMA={ema50}, {d_slow} DSMA={ema200}
- Momentum RSI: Daily={rsi_d}, Weekly={rsi_w}, Monthly={rsi_m}
- Mansfield Relative Strength (vs {bench}): Daily={rs_d}, Weekly={rs_w}, Monthly={rs_m}
- Trend Status: {trend} (Rank: {trend_rank})
  └ Trend Detail: Price > {w_slow} WEMA: {trend_detail.get('price_above_ma')}, {w_slow} WEMA Slope Rising: {trend_detail.get('slope_rising')}, Fast > Slow WEMA: {trend_detail.get('ema_aligned')}, RS Positive: {trend_detail.get('rs_positive')}, Near 52W High/Low: {trend_detail.get('near_high_low_pass')}
- Volatility Stop (VStop-W): Direction={vstop_dir}, Stop Level={vstop_weekly}, Weeks Held={vstop_wks}
- Tech Uptrend: {tech_uptrend} (Requires VStop uptrend > {tu_weeks} wks, Price > {w_slow} WEMA, Vol 10D > {tu_vol}x Vol 100D)
- Volume Analysis: Vol 10D={vol_10d}, Vol 100D={vol_100d}, Vol Trend={vol_trend}
- Net Volume 10D (Accumulation vs Distribution): Direction={net_vol_dir}, Ratio={net_vol_ratio}%
- 52-Week Range: High={h52}, Low={l52}
- TA Rules (TheWrap weekly EMA flowchart): {_ta_rules_text(row_data)}
- Valuation: Trailing P/E={_fmt(row_data.get('trailing_pe'))}, Forward P/E={_fmt(row_data.get('forward_pe'))}, P/B={_fmt(row_data.get('pb_ratio'))}, EV/EBITDA={_fmt(row_data.get('ev_ebitda'))}, ROE={_fmt(row_data.get('roe'), '%')}, ROCE={_fmt(row_data.get('roce'), '%')}
- Latest quarter ({row_data.get('reported_qtr') or 'N/A'}) YoY growth: EPS={_fmt(row_data.get('qtr_eps_growth'), '%')}, Net profit={_fmt(row_data.get('qtr_profit_growth'), '%')}, Revenue={_fmt(row_data.get('qtr_revenue_growth'), '%')}

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
5. RECENT WEB NEWS & ANNOUNCEMENTS (Last 24 hours via Grounded Search)
======================================================================
{news_quality_note}
{news_text}

======================================================================
EXPERT INSTRUCTIONS:
======================================================================
Evaluate this stock from a disciplined growth-and-momentum investor perspective.

{VERDICT_RULES}

Then:
1. State the Verdict (ACCUMULATE / HOLD / CAUTION).
2. Provide a 1-line headline summarizing the key reason.
3. Concise Technical & Volume Assessment (2-3 sentences).
4. Concise Catalyst Assessment covering sections 4 and 5 — if BOTH are empty, explicitly state "No material news found; verdict based on technicals only."
5. Actionable Take (2-3 sentences): entry/add zones, trailing stop levels, or exit triggers. Write it as analysis for the reader's own research, not as personal investment advice, and do not state certainty about future prices.

Return ONLY a valid JSON object matching this schema:
{{
  "verdict": "ACCUMULATE" | "HOLD" | "CAUTION",
  "headline": "Short 1-line summary statement",
  "technical_summary": "Concise technical/volume takeaway",
  "catalyst_summary": "Concise news/catalyst takeaway",
  "actionable_take": "Clear actionable advice for an investor"
}}"""
    return prompt


# Default for generate_expert_view's fundamental_view: read the ticker's view
# from fundamentals.json. None is a different, deliberate value -- "not
# available for this run" -- which section 4 renders as NO INFORMATION.
_LOAD_FUNDAMENTALS = object()


def generate_expert_view(client, row_data, news_text=None, news_source=None, active_alerts_text=None, is_retry=False,
                         fundamental_view=_LOAD_FUNDAMENTALS):
    from google.genai import types
    from datetime import datetime, timezone

    ticker = row_data.get("ticker", "UNKNOWN")
    market = row_data.get("market", "us_invested")
    company_name = row_data.get("company_name", ticker)

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
        data["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        data["news_used"] = news_text
        data["quarter_facts_used"] = _quarter_fundamentals_text(fundamental_view).startswith("- ")
        data["news_source"] = news_source or "⚪ Unknown"
        data["model_used"] = model.split("/")[-1] if used == model else f"{used.split('/')[-1]} (Fallback)"
        return data
    return _pending_fallback("reasoning ladder exhausted")


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
        return view
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
