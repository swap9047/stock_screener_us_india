"""Expert Take, redesigned 2026-10-04: the columns decide, the AI explains.

The model's verdict matched a rule over the existing columns for ~81% of
tickers, and most of the rest re-weighed those same columns (or cited a past
EPS miss the forward-looking Sentiment rule rules out). So:

  ACCUMULATE  (Trend Up/Strong Up OR Tech Uptrend Yes -- owner, same day: one of
              the two is enough) AND TA Rules Maintain/Add or Bullish Signal AND
              Sentiment not Negative
          or  TA Rules Bullish Signal (a breakout from a converging base, where
              Trend is usually Mixed) AND Trend not down AND Sentiment not Negative
  CAUTION     2+ points: TA Exit 2 (the flowchart's own exit), TA Be Cautious 1,
              Trend Down/Strong Down 1, Sentiment Negative 1
  HOLD        otherwise; PENDING without a Trend (too little history)

The verdict is computed LIVE from the row, so it cannot disagree with the
columns on screen. The nightly AI writes the explanation and trade plan, and
reads 14 days of material non-earnings news: a dated, quoted negative event in
a named category lowers the verdict ONE step (never raises it), until the item
is older than the window. Overbought RSI is not a Caution point.

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


def row(trend="Uptrend", tu=1, ta="Maintain/Add", **kw):
    return {"trend": trend, "tech_uptrend": tu, "ta_rules": ta, **kw}


if decide:
    for r, sent, want, why in (
        (row(), "Positive", "ACCUMULATE", "all three chart reads bullish, Sentiment Positive"),
        (row(trend="Strong Uptrend", ta="Bullish Signal"), "Neutral", "ACCUMULATE", "Strong Uptrend + breakout"),
        (row(), "Unknown", "ACCUMULATE", "an Unknown Sentiment does not block"),
        (row(), "Negative", "HOLD", "a Bearish Sentiment blocks Accumulate (1 point: Hold)"),
        (row(tu=0), "Positive", "ACCUMULATE", "Trend up is enough without Tech Uptrend"),
        (row(trend="Mixed", tu=1), "Positive", "ACCUMULATE", "...and Tech Uptrend is enough without Trend up"),
        (row(trend="Mixed", tu=0), "Positive", "HOLD", "neither Trend up nor Tech Uptrend: Hold"),
        (row(trend="Downtrend", tu=0, ta="Maintain/Add"), "Neutral", "HOLD", "a Downtrend without Tech Uptrend: Hold"),
        (row(trend="Mixed", tu=1, ta="Momentum Fading"), "Positive", "HOLD", "TA still has to agree"),
        (row(trend="Mixed", tu=1), "Negative", "HOLD", "...and so does Sentiment"),
        (row(ta="Momentum Fading"), "Positive", "HOLD", "TA Momentum Fading blocks Accumulate"),
        (row(ta="Wait/Watch"), "Positive", "HOLD", "TA Wait/Watch blocks Accumulate"),
        (row(trend="Mixed", tu=0, ta="Bullish Signal"), "Neutral", "ACCUMULATE",
         "TA's breakout counts on its own while Trend is Mixed"),
        (row(trend="Downtrend", tu=0, ta="Bullish Signal"), "Neutral", "HOLD", "...but not in a Downtrend"),
        (row(trend="Mixed", tu=0, ta="Bullish Signal"), "Negative", "HOLD", "...nor against a Bearish Sentiment"),
        (row(trend="Mixed", tu=0, ta="Exit"), "Neutral", "CAUTION", "TA Exit alone is Caution (it counts double)"),
        (row(trend="Uptrend", tu=1, ta="Exit"), "Positive", "CAUTION", "...even in an Uptrend: the flowchart says Exit"),
        (row(trend="Downtrend", tu=0, ta="Be Cautious"), "Neutral", "CAUTION", "Downtrend + Be Cautious"),
        (row(trend="Downtrend", tu=0, ta="Maintain/Add"), "Negative", "CAUTION", "Downtrend + Bearish Sentiment"),
        (row(trend="Mixed", tu=0, ta="Be Cautious"), "Negative", "CAUTION", "Be Cautious + Bearish Sentiment"),
        (row(trend="Downtrend", tu=0, ta="Maintain/Add"), "Positive", "HOLD", "a Downtrend alone is 1 point: Hold"),
        (row(trend="Mixed", tu=0, ta="Be Cautious"), "Neutral", "HOLD", "Be Cautious alone is 1 point: Hold"),
        (row(trend="Strong Uptrend", rsi14_weekly=88.0, rsi14_monthly=91.0), "Positive", "ACCUMULATE",
         "overbought RSI is strength in a trend, not a Caution point"),
        (row(ta=None), "Positive", "HOLD", "no TA verdict yet: no Accumulate"),
        ({"trend": None}, "Positive", "PENDING", "no Trend (too little history): Pending"),
    ):
        got = decide(r, sent)
        check(isinstance(got, tuple) and got[0] == want, f"{why} -> {want} (got {got[0] if isinstance(got, tuple) else got})")
    v, reasons = decide(row(trend="Mixed", tu=0, ta="Exit"), "Negative")
    check(v == "CAUTION" and any("TA Rules: Exit" in x for x in reasons) and any("Sentiment" in x for x in reasons),
          f"the reasons name what decided it ({reasons})")
    v, reasons = decide(row(ta="Momentum Fading"), "Positive")
    check(any("Momentum Fading" in x for x in reasons), f"a Hold's reasons name what blocked Accumulate ({reasons})")

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
    for r, sent, want in ((row(), "Positive", "HOLD"), (row(trend="Mixed", tu=0, ta="Wait/Watch"), "Neutral", "CAUTION"),
                          (row(trend="Mixed", tu=0, ta="Exit"), "Neutral", "CAUTION")):
        got = lower(r, view, sentiment=sent, today=TODAY)
        check(got["verdict"] == want and got["news_lowered"] == (got["base"] != want),
              f"news lowers {got['base']} one step -> {want} (got {got['verdict']})")
    got = lower(row(trend="Mixed", tu=0, ta="Wait/Watch"), {"news_risk": {**RISK, "material_negative": False}},
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
rows = [{"ticker": "ACME", **row()}, {"ticker": "ZED.NS", "trend": None}]
sd.apply_view_fields_to_rows(rows, fundamentals={}, expert_views={}, interested=set())
check(rows[0]["expert_take"] == "Accumulate" and rows[1]["expert_take"] == "Pending",
      f"apply_view_fields_to_rows attaches the live verdict ({rows[0]['expert_take']}, {rows[1]['expert_take']})")

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
        for (model, _), text in zip(tiers, texts):
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

# --- app wiring ----------------------------------------------------------------------------
APP = (REPO / "app.py").read_text()
check("expert_take_for_row(" in APP and "chart_rule_verdict" not in APP and "validate_verdict" not in APP,
      "the app uses the live verdict, not the old model verdict + guard")
check("Lowered from" in APP, "the hover says when news lowered the verdict")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
