"""Expert Take: the columns decide, the AI explains (2026-10-04), Stage-led since 2026-10-31.

The model's verdict matched a rule over the existing columns for ~81% of
tickers, so the columns decide. Since 2026-10-31 (owner) TheWrap Stage Analysis
sets the bar and TA Rules, Stage Mom and Sentiment move the stock within it;
Trend and Tech Uptrend are no longer inputs. Each of the three is Good /
Neutral / Bad (TA: Bullish Signal, Maintain/Add Good -- Wait/Watch, Momentum
Fading Neutral -- Be Cautious, Exit Bad; Mom: Strong, Recovering Good --
Weakening Neutral -- Weak Bad; Sentiment: Positive Good, Negative Bad):

  S2  CAUTION on TA Exit or 2+ Bad; ACCUMULATE if TA Good and nothing Bad; else HOLD
  S1  ACCUMULATE (early) if Mom Good and Sentiment not Bad, whatever TA says
      (owner, 2026-11-01: a TA Exit is not considered); otherwise CAUTION on TA
      Exit or 2+ Bad, else HOLD. CE and PEAD never count (CE kept out of S1 too)
  S3  CAUTION if anything Bad, else HOLD
  S4  HOLD if Mom Good and nothing Bad (an early turn), else CAUTION
  no Stage (or "Unknown")  PENDING

The verdict is computed LIVE from the row, so it cannot disagree with the
columns on screen. The nightly AI writes the explanation and trade plan, and
reads 14 days of material non-earnings news: a dated, quoted negative event in
a named category lowers the verdict ONE step (never raises it), until the item
is older than the window.

Offline: invented tickers, stubbed model calls, no network, no data files.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import expert_views as ev
import llm_util

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def fn(name):
    f = getattr(ev, name, None)
    if f is None:
        check(False, f"expert_views.{name} exists")
    return f


decide = fn("decide_expert_verdict")


S1, S2, S3, S4 = "S1 Accumulation", "S2 Advancing", "S3 Distribution", "S4 Decline"
UP, REC, WKG, WEAK = "Strong ↑", "Recovering ↑", "Weakening ↓", "Weak ↓"


def row(stage=S2, ta="Maintain/Add", mom=UP, ce="No", pead="No", trend="Uptrend", tu=1, **kw):
    """A row with the Stage fields. trend / tu stay on it (the write-up's prompt reads
    them) but no longer decide anything."""
    return {"stage": stage, "ta_rules": ta, "stage_mom": mom, "cheat_entry": ce, "pead": pead,
            "trend": trend, "tech_uptrend": tu, **kw}


if decide:
    for r, sent, want, why in (
        # S2: TA Good and nothing Bad -> Accumulate
        (row(), "Positive", "ACCUMULATE", "S2, Maintain/Add, Mom Strong, Positive"),
        (row(ta="Bullish Signal", mom=WKG), "Neutral", "ACCUMULATE", "S2, Bullish Signal, Mom Weakening (Neutral)"),
        (row(mom=REC), "Unknown", "ACCUMULATE", "an Unknown Sentiment is Neutral, not Bad"),
        (row(ta="Maintain/Add", mom=WEAK), "Positive", "HOLD", "S2 with Mom Weak (1 Bad): Hold"),
        (row(), "Negative", "HOLD", "S2 with a Negative Sentiment (1 Bad): Hold"),
        (row(ta="Momentum Fading"), "Positive", "HOLD", "S2 with TA Momentum Fading (Neutral TA): Hold"),
        (row(ta="Wait/Watch"), "Positive", "HOLD", "S2 with TA Wait/Watch: Hold"),
        (row(ta="Be Cautious", mom=UP), "Positive", "HOLD", "S2 with Be Cautious alone (1 Bad): Hold"),
        (row(ta="Be Cautious", mom=WEAK), "Positive", "CAUTION", "S2 with Be Cautious + Mom Weak (2 Bad): Caution"),
        (row(mom=WEAK), "Negative", "CAUTION", "S2 with Mom Weak + Negative (2 Bad): Caution"),
        (row(ta="Exit", mom=UP), "Positive", "CAUTION", "S2 with TA Exit: Caution whatever else"),
        (row(ta=None), "Positive", "HOLD", "S2 with no TA verdict yet: Hold"),
        # Trend and Tech Uptrend are not inputs any more
        (row(trend="Strong Downtrend", tu=0), "Positive", "ACCUMULATE", "a Downtrend / no Tech Uptrend no longer counts"),
        (row(stage=S3, trend="Strong Uptrend", tu=1), "Positive", "HOLD", "...nor does an Uptrend lift an S3"),
        # S1: early Accumulate when the base is turning
        (row(stage=S1, mom=UP), "Neutral", "ACCUMULATE", "S1, TA Good, Mom Strong: early Accumulate"),
        (row(stage=S1, mom=REC), "Positive", "ACCUMULATE", "S1, TA Good, Mom Recovering: early Accumulate"),
        (row(stage=S1, mom=WEAK, ce="Yes"), "Neutral", "HOLD", "S1: CE Yes does NOT stand in for Mom (kept out)"),
        (row(stage=S1, mom=WKG, ce="Yes"), "Positive", "HOLD", "...nor with Mom Weakening"),
        (row(stage=S1, mom=WKG, ce="No"), "Positive", "HOLD", "S1, TA Good, Mom Weakening: Hold"),
        (row(stage=S1, mom=UP), "Negative", "HOLD", "S1 with a Negative Sentiment: no early Accumulate"),
        (row(stage=S1, ta="Momentum Fading", mom=UP), "Positive", "ACCUMULATE", "S1: TA is not counted (Fading)"),
        (row(stage=S1, ta="Be Cautious", mom=REC), "Neutral", "ACCUMULATE", "S1: ...nor Be Cautious"),
        (row(stage=S1, ta="Exit", mom=REC), "Positive", "ACCUMULATE", "S1: ...nor a TA Exit, with Mom Recovering"),
        (row(stage=S1, ta="Exit", mom=UP), "Neutral", "ACCUMULATE", "S1: ...or with Mom Strong"),
        (row(stage=S1, ta=None, mom=UP), "Unknown", "ACCUMULATE", "S1: ...or with no TA verdict"),
        (row(stage=S1, ta="Exit", mom=WKG), "Positive", "CAUTION", "S1 with TA Exit and Mom Weakening: Caution"),
        (row(stage=S1, ta="Exit", mom=WEAK), "Positive", "CAUTION", "S1 with TA Exit and Mom Weak: Caution"),
        (row(stage=S1, ta="Exit", mom=REC), "Negative", "CAUTION", "S1, Exit + Mom up but Negative (2 Bad): Caution"),
        (row(stage=S1, ta="Maintain/Add", mom=REC), "Negative", "HOLD", "S1, TA Good + Mom up but Negative: Hold"),
        (row(stage=S2, ta="Exit", mom=REC), "Positive", "CAUTION", "...the Exit exception is S1's only: S2 Exit is Caution"),
        (row(stage=S1, ta="Maintain/Add", mom=WEAK, ce="Yes"), "Negative", "CAUTION", "S1 with 2 Bad: Caution"),
        (row(stage=S1, ta="Wait/Watch", mom=WEAK), "Neutral", "HOLD", "S1 with 1 Bad and no Exit: Hold"),
        # S3: any Bad is Caution, never Accumulate
        (row(stage=S3, ta="Bullish Signal", mom=UP), "Positive", "HOLD", "S3, all Good: still only Hold"),
        (row(stage=S3, mom=WEAK), "Positive", "CAUTION", "S3 with Mom Weak: Caution"),
        (row(stage=S3), "Negative", "CAUTION", "S3 with a Negative Sentiment: Caution"),
        (row(stage=S3, ta="Be Cautious"), "Positive", "CAUTION", "S3 with Be Cautious: Caution"),
        (row(stage=S3, ta="Momentum Fading", mom=WKG), "Neutral", "HOLD", "S3, all Neutral: Hold"),
        # S4: Caution unless an early turn
        (row(stage=S4, ta="Maintain/Add", mom=REC), "Neutral", "HOLD", "S4, Mom Recovering, nothing Bad: an early-turn Hold"),
        (row(stage=S4, ta="Wait/Watch", mom=UP), "Positive", "HOLD", "S4, Mom Strong, nothing Bad: Hold"),
        (row(stage=S4, ta="Exit", mom=UP), "Positive", "CAUTION", "S4 with Mom up but TA Exit: Caution"),
        (row(stage=S4, ta="Maintain/Add", mom=WKG), "Positive", "CAUTION", "S4 with Mom Weakening: Caution"),
        (row(stage=S4, ta="Bullish Signal", mom=UP, ce="Yes"), "Positive", "HOLD", "S4 is never Accumulate, CE or not"),
        # no Stage
        (row(stage=None), "Positive", "PENDING", "no Stage upload for the ticker: Pending"),
        (row(stage="Unknown"), "Positive", "PENDING", "Stage Unknown (too little history): Pending"),
        ({"trend": "Uptrend", "tech_uptrend": 1, "ta_rules": "Maintain/Add"}, "Positive", "PENDING",
         "a row without the Stage fields at all: Pending, not the old Trend rule"),
        (row(rsi14_weekly=88.0, rsi14_monthly=91.0), "Positive", "ACCUMULATE", "overbought RSI is not an input"),
    ):
        got = decide(r, sent)
        check(isinstance(got, tuple) and got[0] == want, f"{why} -> {want} (got {got[0] if isinstance(got, tuple) else got})")
    v, reasons = decide(row(stage=S2, ta="Exit"), "Negative")
    check(v == "CAUTION" and any("TA Rules: Exit (bad)" in x for x in reasons)
          and any("Sentiment: Negative (bad)" in x for x in reasons) and any("Stage: S2" in x for x in reasons),
          f"the reasons name each input and its grade ({reasons})")
    check(reasons[-1].startswith("Shown, not counted: CE No") and "PEAD No" in reasons[-1],
          f"CE and PEAD are shown, not counted ({reasons[-1]})")
    v, reasons = decide(row(stage=S1, mom=REC, ce="Yes", pead="Strong"), "Neutral")
    check(v == "ACCUMULATE" and any("Early Accumulate" in x and "Stage Mom up" in x for x in reasons)
          and reasons[-1] == "Shown, not counted: CE Yes · PEAD Strong",
          f"an S1 early Accumulate says why, and CE is shown, not counted ({reasons})")
    v, reasons = decide(row(stage=S1, ta="Exit", mom=REC), "Positive")
    check(v == "ACCUMULATE" and any("TA Rules not counted in S1" in x for x in reasons)
          and any("TA Rules: Exit (bad)" in x for x in reasons),
          f"an S1 early Accumulate over a TA Exit says TA was not counted, and still shows it ({reasons})")
    v, reasons = decide(row(stage=S4, mom=REC), "Neutral")
    check(any("early turn" in x for x in reasons), f"an S4 early-turn Hold says so ({reasons})")
    v, reasons = decide(row(stage=None), "Positive")
    check(reasons == [ev.NO_STAGE_REASON], f"a Pending names the missing upload ({reasons})")

# --- news can lower one step, never raise ---------------------------------------
TODAY = date(2026, 10, 4)
RISK = {"material_negative": True, "category": "regulatory or legal action", "date": "2026-09-28",
        "quote": "Newswire 2026-09-28: regulator bars the company from new contracts"}
active = fn("news_risk_active")
lower = fn("expert_take_for_row")
if active:
    check(active(RISK, TODAY), "a dated, quoted, categorised negative event inside 14 days is active")
    check(not active({**RISK, "date": "2026-09-19"}, TODAY), "...15 days old it has expired")
    check(active({**RISK, "date": "2026-09-20"}, TODAY), "...14 days old it still counts")
    check(not active({**RISK, "quote": ""}, TODAY), "no quote, no downgrade")
    check(not active({**RISK, "category": "weak quarterly results"}, TODAY),
          "results are Sentiment's job: not a news-risk category")
    check(not active({**RISK, "material_negative": False}, TODAY), "material_negative False is inactive")
    check(not active({**RISK, "date": "soon"}, TODAY) and not active(None, TODAY), "no usable date -> inactive")
    check(not active({**RISK, "date": "2026-10-09"}, TODAY), "a future date is not news")
if lower:
    view = {"verdict": "HOLD", "news_risk": RISK, "as_of": "2026-10-04 05:00", "headline": "x", "model_used": "m"}
    for r, sent, want in ((row(), "Positive", "HOLD"), (row(ta="Wait/Watch"), "Neutral", "CAUTION"),
                          (row(ta="Exit"), "Neutral", "CAUTION")):
        got = lower(r, view, sentiment=sent, today=TODAY)
        check(got["verdict"] == want and got["news_lowered"] == (got["base"] != want),
              f"news lowers {got['base']} one step -> {want} (got {got['verdict']})")
    got = lower(row(ta="Wait/Watch"), {"news_risk": {**RISK, "material_negative": False}},
                sentiment="Neutral", today=TODAY)
    check(got["verdict"] == "HOLD" and not got["news_lowered"], "no active risk: the columns' verdict stands")
    got = lower(row(), {}, sentiment="Positive", today=TODAY)
    check(got["verdict"] == "ACCUMULATE", "no write-up at all: the columns still decide (no Pending for a missing AI run)")
    got = lower(row(), {"verdict": "HOLD", "model_used": "Error", "headline": "Analysis pending -- x"},
                sentiment="Positive", today=TODAY)
    check(got["verdict"] == "ACCUMULATE", "a failed write-up does not change the verdict either")
    got = lower(row(), view, today=TODAY)
    check(got["base"] == "HOLD" or got["base"] == "ACCUMULATE", "sentiment defaults to the row's own")

# --- the row field every consumer filters on --------------------------------------
import stock_data as sd
# The Stage comes from stage_analysis.json, attached by this same function: BEFORE the
# verdict (until 2026-10-31 it was attached after it, which the Stage-led rule would
# have read as Pending on every row).
_bare = {k: v for k, v in row().items() if k not in ("stage", "stage_mom", "cheat_entry", "pead")}
rows = [{"ticker": "ACME", **_bare}, {"ticker": "ZED.NS", **_bare}]
_stage = {"meta": {}, "tickers": {"ACME": {"stage": S2, "stage_mom": UP, "cheat_entry": "No", "pead": "No",
                                           "uploaded": "2026-10-28"}}}
sd.apply_view_fields_to_rows(rows, fundamentals={}, expert_views={}, interested=set(), stage=_stage)
check(rows[0]["expert_take"] == "Accumulate" and rows[1]["expert_take"] == "Pending",
      f"apply_view_fields_to_rows attaches the Stage, then the live verdict ({rows[0]['expert_take']}, "
      f"{rows[1]['expert_take']}: ZED.NS has no upload)")

# --- the search and the prompt -----------------------------------------------------
captured = {}
_real = llm_util.run_model_ladder
llm_util.run_model_ladder = lambda client, prompt, *a, **k: (captured.setdefault("p", prompt), (None, None))[1]
try:
    ev.fetch_gemma_expert_news(None, "ACME", "us_invested", "Acme Corp", is_retry=True)
finally:
    llm_util.run_model_ladder = _real
sp = captured.get("p", "")
check(f"last {ev.EXPERT_NEWS_WINDOW_DAYS} days" in sp and ev.EXPERT_NEWS_WINDOW_DAYS == 14,
      "the search covers the last 14 days, not 24 hours")
check("next 7 days" in sp, "...and scheduled events in the next 7 days")
check("not quarterly results" in sp.lower() or "do not report quarterly results" in sp.lower(),
      "...and leaves results/guidance/ratings to Sentiment")
for t in ev.NEWS_RISK_CATEGORIES[:3]:
    check(t.split(" or ")[0] in sp.lower(), f"the search asks for '{t}'")

ROW = {"ticker": "ACME", "company_name": "Acme Corp", "market": "us_invested", "last_close": 100.0,
       **row(), "sentiment": "Positive"}
p = ev.build_expert_prompt(ROW, "No recent news found.", "", {})
check("ACCUMULATE" in p and "You do not choose the verdict" in p, "the prompt states the columns' verdict and that the model doesn't pick it")
check("news_risk" in p and "material_negative" in p and '"verdict"' not in p.split("Return ONLY")[-1],
      "the schema asks for news_risk, not a verdict")
check("lowered one step to HOLD" in p, "the prompt says what a material negative event would do")
for c in ev.NEWS_RISK_CATEGORIES:
    check(c in p, f"the prompt lists the category '{c}'")

# --- generation stores the verdict it was written for ----------------------------------
reply = {"headline": "h", "technical_summary": "t", "catalyst_summary": "c", "actionable_take": "a",
         "news_risk": {"material_negative": True, "category": "fraud or accounting",
                       "date": (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat(),
                       "quote": "Newswire: auditor flags irregularities"}}
llm_util.run_model_ladder = lambda client, prompt, tiers, config_for, label="", subject="", timeout=None, on_success=None: (
    ev.normalize_view(dict(reply)), tiers[0][0])
try:
    v = ev.generate_expert_view(None, ROW, news_text="Auditor flags irregularities.", news_source="test",
                                fundamental_view={})
finally:
    llm_util.run_model_ladder = _real
check(v.get("base_verdict") == "ACCUMULATE" and v.get("verdict") == "HOLD",
      f"stored: base ACCUMULATE, lowered to HOLD by the fraud item ({v.get('base_verdict')}, {v.get('verdict')})")
check(ev._is_valid_view(v), "...and it is a valid view")
check(isinstance(v.get("verdict_reasons"), list) and v["verdict_reasons"], "the reasons are stored")

# --- "no news" said in prose, and refusals, are not news ------------------------------
# Found 2026-10-04: 21 of the 56 stored write-ups counted as "news found" were the
# search model refusing ("I cannot access news from the future (October 2026)") or
# saying there was nothing, in a sentence the old prefix markers missed.
nothing = fn("search_found_nothing")
if nothing:
    for txt in ("Since I cannot access news from the future (October 2026), I have nothing to report.",
                "As today's date in your request is October 4, 2026, and I do not have access to news from the future, I cannot provide a report.",
                "No material news or scheduled events were found for the specified dates in 2026.",
                "Since there is no material news or upcoming events found for the specified dates, I am outputting nothing.",
                "<!-- No material news or upcoming events found for the specified periods. -->",
                "No recent news found.", "", None):
        check(nothing(txt), f"no news: {str(txt)[:60]!r}")
    for txt in ("2026-09-28: Regulator bars the company from new government contracts.",
                "**MATERIAL NEWS** | 2026-09-23: Announced adoption of a new AI platform."):
        check(not nothing(txt), f"real news: {txt[:50]!r}")
    check(not ev.expert_view_has_news({"news_used": "No material news was found for the specified period."}),
          "Expert News? reads No for a 'no news' sentence")
refused = fn("search_refused")
if refused:
    check(refused("I do not have access to news from the future, so I cannot provide a report."),
          "a training-cutoff refusal is detected")
    check(not refused("No material news was found for the specified period."), "...an honest 'nothing' is not a refusal")

calls = []


class _Resp:
    def __init__(self, text):
        self.text = text


def _ladder(texts):
    def run(client, prompt, tiers, config_for, label="", subject="", timeout=None, on_success=None):
        for (model, *_), text in zip(tiers, texts):
            calls.append(text)
            try:
                return (on_success(_Resp(text)) if on_success else _Resp(text)), model
            except Exception as e:
                if not llm_util.is_retryable(e):
                    break
        return None, None
    return run


llm_util.run_model_ladder = _ladder(["I cannot access news from the future (October 2026).",
                                     "2026-09-28: Regulator bars the company from new contracts."])
try:
    text, _src = ev.fetch_gemma_expert_news(None, "ACME", "us_invested", "Acme Corp", is_retry=True)
finally:
    llm_util.run_model_ladder = _real
check(len(calls) == 2 and text.startswith("2026-09-28"), f"a refusal is retried on the next attempt ({len(calls)} calls)")
llm_util.run_model_ladder = _ladder(["No material news was found for the specified period."])
try:
    text, _src = ev.fetch_gemma_expert_news(None, "ACME", "us_invested", "Acme Corp", is_retry=True)
finally:
    llm_util.run_model_ladder = _real
check(text == "No recent news found.", f"a 'nothing found' sentence is stored as the no-news marker ({text!r})")
check("do not refuse" in sp.lower(), "the search prompt says the dates are real and not to refuse")

# --- self-review fixes, 2026-10-04 ----------------------------------------------------
# R1 a ticker with no Trend yet cost two model calls a night and was then recorded
#    as a FAILURE (feeding FailureFuse); it needs no write-up at all.
llm_calls = []
llm_util.run_model_ladder = lambda *a, **k: (llm_calls.append(1), (None, None))[1]
try:
    v = ev.generate_expert_view(None, {"ticker": "ACME", "market": "us_invested", "trend": None}, fundamental_view={})
finally:
    llm_util.run_model_ladder = _real
check(not llm_calls, f"R1: no Trend -> no model call ({len(llm_calls)} made)")
llm_util.run_model_ladder = lambda *a, **k: (llm_calls.append(1), (None, None))[1]
try:
    v = ev.generate_expert_view(None, {"ticker": "ACME", "market": "us_invested", **row(stage=None)},
                                fundamental_view={})
finally:
    llm_util.run_model_ladder = _real
check(not llm_calls and "no Stage Analysis" in str(v), f"R1: no Stage -> no model call either ({len(llm_calls)} made)")
src_r = (REPO / "refresh_expert_views.py").read_text()
check('if not row.get("trend") or row.get("stage") not in STAGES:' in src_r,
      "R1: the nightly job skips a ticker with no Trend or no Stage, not as a failure")

# R2 a news risk must survive a night whose search misses it, until it expires.
if hasattr(ev, "resolve_persisted_view"):
    recent = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
    old = {"verdict": "HOLD", "headline": "old", "model_used": "m", "as_of": "2026-10-03 05:00",
           "news_used": "x", "news_risk": {**RISK, "date": recent}}
    new = {"verdict": "ACCUMULATE", "headline": "new", "model_used": "m", "as_of": "2026-10-04 05:00",
           "news_used": "No recent news found.",
           "news_risk": {"material_negative": False, "category": None, "date": None, "quote": None}}
    stored = ev.resolve_persisted_view(dict(new), old)
    check(stored and stored["news_risk"]["material_negative"] and stored["news_risk"].get("carried_from") == "2026-10-03 05:00",
          f"R2: a still-active risk is carried into the new write-up ({(stored or {}).get('news_risk')})")
    expired = {**old, "news_risk": {**RISK, "date": "2026-01-01"}}
    stored = ev.resolve_persisted_view(dict(new), expired)
    check(not stored["news_risk"]["material_negative"], "R2: ...but not once it has expired")
    other = {**new, "news_risk": {**RISK, "date": recent, "category": "fraud or accounting irregularities",
                                  "quote": "auditor resigns"}}
    stored = ev.resolve_persisted_view(dict(other), old)
    check(stored["news_risk"]["quote"] == "auditor resigns", "R2: a new risk replaces the carried one")

# R3 no news found -> no news risk, whatever the model says.
reply2 = dict(reply)
llm_util.run_model_ladder = lambda client, prompt, tiers, config_for, label="", subject="", timeout=None, on_success=None: (
    ev.normalize_view(dict(reply2)), tiers[0][0])
try:
    v = ev.generate_expert_view(None, ROW, news_text="No recent news found.", news_source="test", fundamental_view={})
finally:
    llm_util.run_model_ladder = _real
check(v.get("verdict") == "ACCUMULATE" and not v["news_risk"]["material_negative"],
      f"R3: a risk reported when the search found nothing is dropped ({v.get('verdict')}, {v.get('news_risk')})")

# R4 "rating downgrade" is a CREDIT rating; an analyst's is Sentiment's.
check(any("credit-rating downgrade" in c for c in ev.NEWS_RISK_CATEGORIES)
      and not any(c.endswith(" rating downgrade") and "credit" not in c for c in ev.NEWS_RISK_CATEGORIES),
      "R4: the category says credit-rating downgrade")
check(ev.normalize_view({"news_risk": {"material_negative": True, "category": "d", "date": "2026-10-01",
                                       "quote": "q"}})["news_risk"]["category"] is None,
      "R4: a one-letter category does not match by prefix")

# R5 an active risk on a stock already at Caution still shows in the hover.
got = ev.expert_take_for_row(row(ta="Exit"), {"news_risk": RISK, "news_used": "x"},
                             sentiment="Neutral", today=TODAY)
check(got["verdict"] == "CAUTION" and not got["news_lowered"] and got.get("news_noted"),
      "R5: a risk that cannot lower a Caution is still reported (news_noted)")
check("News risk noted" in (REPO / "app.py").read_text(), "R5: ...and the hover shows it")
check("⚠️" in (REPO / "app.py").read_text().split("def _expert_take_cell")[1][:2500],
      "R6: a failed write-up is visible on the badge, not only in the hover")

# --- app wiring ----------------------------------------------------------------------------
APP = (REPO / "app.py").read_text()
check("expert_take_for_row(" in APP and "chart_rule_verdict" not in APP and "validate_verdict" not in APP,
      "the app uses the live verdict, not the old model verdict + guard")
check("Lowered from" in APP, "the hover says when news lowered the verdict")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
