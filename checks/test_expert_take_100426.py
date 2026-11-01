"""Expert Take: the columns decide, the AI explains (2026-10-04); Stage x TA Rules since 2026-11-01.

The model's verdict matched a rule over the existing columns for ~81% of
tickers, so the columns decide. Since 2026-11-01 (owner: "stage and ta should
suffice") TheWrap Stage Analysis and the TA Rules flowchart decide, first match
wins:

  no Stage / Unknown           PENDING
  S1 + Stage Mom Strong/Recovering   ACCUMULATE (early turn), whatever TA says
  TA Exit / Be Cautious        CAUTION
  S2 + Bullish Signal          ACCUMULATE
  S2 + Maintain/Add            ACCUMULATE within 3% (either side) of the 10W EMA,
                               else HOLD (extended above it, or under it)
  S2 otherwise                 HOLD
  S1 + Bullish Signal          ACCUMULATE, else HOLD
  S3                           HOLD
  S4 + Maintain/Add / Bullish  HOLD (early turn), else CAUTION

Sentiment, Trend, Tech Uptrend, CE and PEAD never decide. The verdict is
computed LIVE from the row. The nightly AI writes the explanation and trade
plan, and reads 14 days of material non-earnings news: a dated, quoted negative
event in a named category is shown as an FYI and never changes the verdict
(owner, 2026-11-01; from 2026-10-04 it lowered it one step).

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


def row(stage=S2, ta="Maintain/Add", mom=WKG, d=1.0, ce="No", pead="No", trend="Uptrend", tu=1, **kw):
    """A row with the Stage fields and a live close `d`% from a 10W EMA of 100 (d=None:
    no 10W EMA). trend / tu stay on it (the write-up's prompt reads them) but decide
    nothing."""
    out = {"stage": stage, "ta_rules": ta, "stage_mom": mom, "cheat_entry": ce, "pead": pead,
           "trend": trend, "tech_uptrend": tu, "last_close": 100.0 + (d or 0), "ema10": 100.0 if d is not None else None}
    return {**out, **kw}


TAS = ("Bullish Signal", "Maintain/Add", "Wait/Watch", "Momentum Fading", "Be Cautious", "Exit")
if decide:
    # Every Stage x every TA value, worked out by hand from the table above (Stage Mom
    # Weakening, so the S1 Mom route stays shut; the close 1% above the 10W).
    WANT = {
        S2: ("ACCUMULATE", "ACCUMULATE", "HOLD", "HOLD", "CAUTION", "CAUTION"),
        S1: ("ACCUMULATE", "HOLD", "HOLD", "HOLD", "CAUTION", "CAUTION"),
        S3: ("HOLD", "HOLD", "HOLD", "HOLD", "CAUTION", "CAUTION"),
        S4: ("HOLD", "HOLD", "CAUTION", "CAUTION", "CAUTION", "CAUTION"),
    }
    for stage, wants in WANT.items():
        for ta, want in zip(TAS, wants):
            got = decide(row(stage=stage, ta=ta), "Neutral")[0]
            check(got == want, f"{stage[:2]} + {ta} -> {want} (got {got})")
    for r, want, why in (
        # the S2 10W buy zone, both edges
        (row(d=3.0), "ACCUMULATE", "S2 Maintain/Add 3.0% above the 10W: in the buy zone"),
        (row(d=3.1), "HOLD", "...3.1% above: extended, Hold"),
        (row(d=17.0), "HOLD", "...17% above: extended, Hold"),
        (row(d=-3.0), "ACCUMULATE", "...3.0% under the 10W: still the buy zone"),
        (row(d=-3.1), "HOLD", "...3.1% under: Hold (under the 10W this week)"),
        (row(d=None), "HOLD", "...no 10W EMA on the row: Hold, never a guess"),
        (row(ta="Bullish Signal", d=12.0), "ACCUMULATE", "an S2 breakout needs no 10W test"),
        # S1: Stage Mom up is an early Accumulate whatever TA says; nowhere else
        (row(stage=S1, mom=REC, ta="Exit", d=-6.0), "ACCUMULATE", "S1 + Mom Recovering over a TA Exit"),
        (row(stage=S1, mom=UP, ta="Be Cautious"), "ACCUMULATE", "S1 + Mom Strong over Be Cautious"),
        (row(stage=S1, mom=WEAK, ta="Maintain/Add"), "HOLD", "S1 + Mom Weak, Maintain/Add: Hold"),
        (row(stage=S1, mom=WEAK, ta="Exit"), "CAUTION", "S1 + Mom Weak, Exit: Caution"),
        (row(stage=S2, mom=WEAK, ta="Maintain/Add"), "ACCUMULATE", "Stage Mom does not count in S2"),
        (row(stage=S3, mom=UP, ta="Maintain/Add"), "HOLD", "...nor in S3"),
        (row(stage=S4, mom=UP, ta="Wait/Watch"), "CAUTION", "...nor in S4"),
        # no Stage
        (row(stage=None), "PENDING", "no Stage upload for the ticker: Pending"),
        (row(stage="Unknown"), "PENDING", "Stage Unknown (too little history): Pending"),
        ({"trend": "Uptrend", "tech_uptrend": 1, "ta_rules": "Maintain/Add"}, "PENDING",
         "a row without the Stage fields: Pending, not the old Trend rule"),
    ):
        got = decide(r, "Neutral")[0]
        check(got == want, f"{why} -> {want} (got {got})")
    # Sentiment, Trend, Tech Uptrend, CE and PEAD decide nothing
    for stage in (S1, S2, S3, S4):
        for ta in TAS:
            base = decide(row(stage=stage, ta=ta), "Neutral")[0]
            others = {decide(row(stage=stage, ta=ta, trend=t, tu=u, ce=c, pead=pd), sent)[0]
                      for sent in ("Positive", "Negative", "Unknown", None)
                      for t, u in (("Strong Downtrend", 0), ("Strong Uptrend", 1))
                      for c, pd in (("Yes", "Strong"), ("No", "No"))}
            if others != {base}:
                check(False, f"{stage[:2]} + {ta}: Sentiment/Trend/Tech Uptrend/CE/PEAD changed the verdict ({others})")
    check(True, "Sentiment, Trend, Tech Uptrend, CE and PEAD never change the verdict (every Stage x TA)")
    v, reasons = decide(row(ta="Exit", ta_rules_detail={"decided_by": {"test": "slow"}}), "Negative")
    check(v == "CAUTION" and reasons[0] == "Exit signal in S2" and "TA Rules: Exit (broke the 40W EMA)" in reasons
          and reasons[-1].startswith("Context, not counted: Stage Mom Weakening ↓ · Sentiment Negative"),
          f"an S2 40W break says so, and lists the context it did not count ({reasons})")
    v, reasons = decide(row(d=1.5), "Positive")
    check(reasons[0] == "S2 buy zone: +1.5% from the 10W EMA", f"the buy zone names the distance ({reasons[0]})")
    v, reasons = decide(row(d=8.0), "Positive")
    check(reasons[0].startswith("S2 extended: +8.0% above the 10W EMA"), f"extended says so ({reasons[0]})")
    v, reasons = decide(row(stage=S1, mom=REC, ta="Exit"), "Positive")
    check(reasons[0].startswith("Early Accumulate: an S1 base turning (Stage Mom Recovering ↑; TA Rules not counted"),
          f"the S1 early Accumulate says why ({reasons[0]})")
    v, reasons = decide(row(stage=None), "Positive")
    check(reasons == [ev.NO_STAGE_REASON], f"a Pending names the missing upload ({reasons})")
    check(abs(ev.distance_to_10w({"last_close": 103.0, "ema10": 100.0}) - 3.0) < 1e-9
          and ev.distance_to_10w({"last_close": 103.0}) is None and ev.distance_to_10w({"last_close": "x", "ema10": 1}) is None,
          "distance_to_10w: % from the 10W EMA, None when it cannot be worked out")

# --- news is FYI: shown with the verdict, never changes it (owner, 2026-11-01) ------
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
    for r, want in ((row(), "ACCUMULATE"), (row(ta="Wait/Watch"), "HOLD"), (row(ta="Exit"), "CAUTION")):
        got = lower(r, view, sentiment="Neutral", today=TODAY)
        check(got["verdict"] == got["base"] == want and not got["news_lowered"] and got["news_noted"]
              and got["news_risk"] == RISK,
              f"an active news risk is FYI: {want} stays {want}, the risk is attached (got {got['verdict']})")
    got = lower(row(ta="Wait/Watch"), {"news_risk": {**RISK, "material_negative": False}},
                sentiment="Neutral", today=TODAY)
    check(got["verdict"] == "HOLD" and not got["news_noted"] and got["news_risk"] is None,
          "no active risk: nothing attached")
    got = lower(row(stage=None), view, sentiment="Neutral", today=TODAY)
    check(got["verdict"] == "PENDING" and not got["news_noted"], "a Pending row carries no news FYI")
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
check("does NOT change it" in p and "lowered" not in p.lower(),
      "the prompt says a material negative event is FYI and does not change the verdict")
check("within 3% of the 10W EMA" in p and "Sentiment, Trend, Tech Uptrend, CE and PEAD are context" in p,
      "the prompt states the Stage x TA rule, with the 10W buy zone, and what is only context")
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
check(v.get("base_verdict") == "ACCUMULATE" and v.get("verdict") == "ACCUMULATE"
      and v["news_risk"]["material_negative"],
      f"stored: ACCUMULATE with the fraud item kept as FYI ({v.get('base_verdict')}, {v.get('verdict')})")
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

# R5 an active risk shows in the hover at any verdict (FYI since 2026-11-01).
got = ev.expert_take_for_row(row(ta="Exit"), {"news_risk": RISK, "news_used": "x"},
                             sentiment="Neutral", today=TODAY)
check(got["verdict"] == "CAUTION" and not got["news_lowered"] and got.get("news_noted"),
      "R5: an active risk on a Caution is reported (news_noted)")
check("News FYI (does not change the verdict)" in (REPO / "app.py").read_text(), "R5: ...and the hover shows it")
check("⚠️" in (REPO / "app.py").read_text().split("def _expert_take_cell")[1][:2500],
      "R6: a failed write-up is visible on the badge, not only in the hover")

# --- app wiring ----------------------------------------------------------------------------
APP = (REPO / "app.py").read_text()
check("expert_take_for_row(" in APP and "chart_rule_verdict" not in APP and "validate_verdict" not in APP,
      "the app uses the live verdict, not the old model verdict + guard")
check("Lowered from" not in APP and "lowered by news" not in APP and 'if take["news_noted"]:' in APP,
      "the app shows news as FYI (the ⚑ badge and hover), never as a lowered verdict")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
