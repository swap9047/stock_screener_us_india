"""Sentiment: forward-looking signals carry the weight; the reported quarter adds a little.

Owner's rule (2026-10-03):
  guidance raised / lowered           +-9  outweighs everything else together
  management's quoted outlook         +-3  improving / cautious
  named-firm analyst action           +-3  upgrade / downgrade
  profit (EPS, else PAT) YoY > 15%    +-1  (strictly; sales recorded, not used)
  beat / miss of a STATED consensus   +-1

Positive needs a positive total AND at least one forward signal pointing up;
Negative the mirror. So the quarter never decides alone, cannot outvote
management (strong growth + beat + cautious outlook = Negative), but breaks a
tie between forward signals.

Offline: invented tickers, stubbed model calls, no data files, no network.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import fundamentals_eval as fe
import llm_util
import refresh_fundamentals as rf
import stock_data as sd

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
QUOTE = "Management on 2026-08-10: 'we expect demand to stay strong next quarter'"


def facts(**kw):
    base = {"eps_value": None, "guidance_change": None, "analyst_action": None, "analyst_firm": None,
            "outlook_tone": None, "outlook_quote": None, "profit_yoy_pct": None, "results_vs_estimate": None,
            "sentiment": "Neutral"}
    base.update(kw)
    return base


def verdict(view, threshold=15.0):
    try:
        return fe.score_sentiment(view, threshold)[0]
    except AttributeError as e:
        return f"missing ({e})"


# --- the rule, case by case -------------------------------------------------------
G = dict(profit_yoy_pct=22.0, revenue_yoy_pct=18.0, results_vs_estimate="beat")    # a strong quarter
D = dict(profit_yoy_pct=-30.0, revenue_yoy_pct=-12.0, results_vs_estimate="miss")   # a weak quarter
UP = dict(outlook_tone="improving", outlook_quote=QUOTE)
DOWN = dict(outlook_tone="cautious", outlook_quote=QUOTE)
DOWNGRADE = dict(analyst_action="downgrade", analyst_firm="Kotak")
CASES = [
    ("named-firm upgrade alone", facts(analyst_action="upgrade", analyst_firm="Kotak"), "Positive"),
    ("named-firm downgrade alone", facts(**DOWNGRADE), "Negative"),
    ("guidance raised alone", facts(guidance_change="raised"), "Positive"),
    ("guidance lowered alone", facts(guidance_change="lowered"), "Negative"),
    ("improving outlook alone", facts(**UP), "Positive"),
    ("cautious outlook alone", facts(**DOWN), "Negative"),
    ("a strong quarter alone does not decide", facts(**G), "Neutral"),
    ("a weak quarter alone does not decide", facts(**D), "Neutral"),
    ("strong growth + beat, management cautious -> Negative (outlook outweighs both)", facts(**G, **DOWN), "Negative"),
    ("weak quarter + miss, management improving -> Positive", facts(**D, **UP), "Positive"),
    ("strong quarter + upgrade", facts(**G, analyst_action="upgrade", analyst_firm="Kotak"), "Positive"),
    ("improving outlook + downgrade cancel", facts(**UP, **DOWNGRADE), "Neutral"),
    ("...a strong quarter breaks that tie upward", facts(**UP, **DOWNGRADE, **G), "Positive"),
    ("...a weak quarter breaks it downward", facts(**UP, **DOWNGRADE, **D), "Negative"),
    ("guidance raised outranks a downgrade", facts(guidance_change="raised", **DOWNGRADE), "Positive"),
    ("guidance lowered outranks improving outlook + upgrade + a strong quarter",
     facts(guidance_change="lowered", analyst_action="upgrade", analyst_firm="Kotak", **UP, **G), "Negative"),
    ("guidance raised outranks a cautious tone", facts(guidance_change="raised", **DOWN), "Positive"),
    ("outlook tone without management's words does not count", facts(**G, outlook_tone="improving"), "Neutral"),
    ("upgrade with no named firm does not count", facts(analyst_action="upgrade", analyst_firm=None), "Neutral"),
    ("an algorithmic-site upgrade does not count", facts(analyst_action="upgrade", analyst_firm="MarketsMojo"), "Neutral"),
    # Found in the 2026-10-03 A/B: Zacks' quant rank arrived as "Zacks Research",
    # past a blocklist entry that only matched "zacks rank".
    ("a Zacks Research upgrade (its quant rank) does not count",
     facts(analyst_action="upgrade", analyst_firm="Zacks Research"), "Neutral"),
    ("guidance maintained is a fact, not a direction", facts(guidance_change="maintained"), "Neutral"),
    ("a steady outlook is a fact, not a direction", facts(outlook_tone="steady", outlook_quote=QUOTE), "Neutral"),
    ("profit exactly +15% does not count (more than 15%)", facts(**UP, **DOWNGRADE, profit_yoy_pct=15.0), "Neutral"),
    ("profit +15.1% counts", facts(**UP, **DOWNGRADE, profit_yoy_pct=15.1), "Positive"),
    ("sales growth is not used", facts(**UP, **DOWNGRADE, revenue_yoy_pct=40.0), "Neutral"),
]
for name, view, want in CASES:
    got = verdict(view)
    check(got == want, f"{name} -> {want} (got {got})")
check(verdict(facts(**UP, **DOWNGRADE, profit_yoy_pct=22.0), threshold=25.0) == "Neutral",
      "the profit threshold is a parameter (+22% does not count at 25%)")
check(verdict(facts(sentiment="Unknown")) == "Unknown", "no facts at all and the model said Unknown -> Unknown")
check(verdict(facts(sentiment="Positive")) == "Neutral", "no facts but the model said Positive -> Neutral")
try:
    _, drivers = fe.score_sentiment(facts(**G, profit_metric="PAT", **UP, analyst_action="upgrade", analyst_firm="Kotak"))
    check(drivers == ["Outlook improving", "Upgrade by Kotak", "Beat consensus estimates", "PAT +22% YoY"],
          f"the drivers list the forward signals first, then the quarter: {drivers}")
except AttributeError as e:
    check(False, f"score_sentiment exists ({e})")

# --- normalising what the model returns -------------------------------------------
def norm(**kw):
    return fe.normalize_view({"sentiment": "Neutral", **kw})


for raw, want in (("+22.5%", 22.5), ("22", 22.0), (-35, -35.0), ("-35 %", -35.0), ("N/A", None),
                  (None, None), (5000, None), (True, None)):
    got = norm(profit_yoy_pct=raw).get("profit_yoy_pct", "absent")
    check(got == want, f"profit_yoy_pct {raw!r} -> {want!r} (got {got!r})")
check(norm(revenue_yoy_pct="+18 %").get("revenue_yoy_pct") == 18.0, "revenue_yoy_pct is parsed like the profit figure")
for raw, want in (("Beat ", "beat"), ("in-line", "inline"), ("MISS", "miss"), ("strong", None), (None, None)):
    got = norm(results_vs_estimate=raw).get("results_vs_estimate", "absent")
    check(got == want, f"results_vs_estimate {raw!r} -> {want!r} (got {got!r})")

# --- Yahoo's year-on-year growth fills in when the news gave absolute figures only --
yahoo = getattr(fe, "_yahoo_results_yoy", None)
if yahoo is None:
    check(False, "fundamentals_eval._yahoo_results_yoy exists")
else:
    india = {"reported_qtr": "Q1 FY27", "qtr_profit_growth": 30.0, "qtr_eps_growth": 28.0, "qtr_revenue_growth": 19.0}
    got = yahoo(india, date(2026, 8, 10))
    check(got and got["profit"][0] == 30.0 and got["revenue"][0] == 19.0 and "Q1 FY27" in got["profit"][1],
          f"Q1 FY27 (Apr-Jun) results announced 10 Aug -> Yahoo's profit +30%, sales +19% ({got})")
    check(yahoo(india, date(2026, 11, 10)) is None, "a report a quarter later is a different quarter -> no Yahoo figure")
    check(yahoo({"reported_qtr": "Q2 2026", "qtr_profit_growth": -18.0}, date(2026, 7, 25))["profit"][0] == -18.0,
          "US calendar quarter: Q2 2026 reported 25 Jul -> -18%")
    check(yahoo({"reported_qtr": "Q2 2026", "qtr_eps_growth": 12.0}, date(2026, 7, 25))["profit"][2] == "EPS",
          "no profit growth -> EPS growth")
    check(yahoo(india, None) is None, "no report date -> no Yahoo figure (can't tell it's the same quarter)")

# --- the read-time guard counts the new facts ------------------------------------------
scored_pos = facts(as_of=NOW, earnings_summary="Q1 PAT Rs 32 cr", future_guidance="N/A", analyst_coverage="N/A",
                   revenue_yoy_pct=18.0, profit_yoy_pct=22.0, results_vs_estimate="beat", **UP, sentiment="Positive")
check(fe._validate_sentiment(scored_pos) == ("Positive", ""), "a Positive resting on a quoted outlook stands")
only_outlook = facts(as_of=NOW, earnings_summary="N/A", future_guidance="Management sees strong demand",
                     analyst_coverage="N/A", **UP, sentiment="Positive")
check(fe._validate_sentiment(only_outlook) == ("Positive", ""),
      "the read-time guard no longer caps an outlook-only Positive (it is the rule now)")
weak = {**scored_pos, "profit_yoy_pct": 5.0, "outlook_tone": None, "outlook_quote": None, "sentiment": "Neutral"}
check(fe._validate_sentiment(weak) == ("Neutral", ""), "a Neutral WITH a profit figure is plain Neutral, not 'no hard evidence'")
check(fe._has_hard_evidence(facts(results_vs_estimate="beat")), "a stated beat is hard evidence")

# --- the tag next to the label ----------------------------------------------------------
_tag = fe.evidence_tag({"revenue_yoy_pct": 18.0, "profit_yoy_pct": 22.4, "outlook_tone": "improving", "outlook_quote": QUOTE})
check(_tag == "Outlook ↑ · Profit +22%", f"forward signals first, then the quarter; no sales ({_tag!r})")
_tag = fe.evidence_tag({"outlook_tone": "improving", "outlook_quote": QUOTE, "analyst_action": "upgrade", "analyst_firm": "Kotak"})
check(_tag == "Upgrade · Outlook ↑", f"an upgrade and the outlook both show ({_tag!r})")
check(fe.evidence_tag({"analyst_action": "upgrade", "analyst_firm": "Kotak"}) == "Upgrade", "an upgrade shows")
check(fe.evidence_tag({"results_vs_estimate": "miss", "profit_yoy_pct": -30.0}) == "Missed est. · Profit -30%",
      "the quarter's facts show even when they don't decide")
check(fe.evidence_tag({"guidance_change": "raised", "outlook_tone": "cautious", "outlook_quote": "q"}) == "Guidance ↑",
      "guidance still wins over the outlook tone")

# --- end to end: the model extracts, the code decides -----------------------------------
real_ladder, real_lookup, real_settings = llm_util.run_model_ladder, fe._fetch_last_reported_earnings_date, sd.load_settings
reply = {}


def fake_ladder(client, prompt, tiers, config_for, label="", subject="", timeout=None, on_success=None):
    reply["prompt"] = prompt
    return fe.normalize_view(dict(reply["data"])), tiers[0][0]


llm_util.run_model_ladder = fake_ladder
fe._fetch_last_reported_earnings_date = lambda ticker, timeout=15: date(2026, 8, 10)
sd.load_settings = lambda: {**sd.DEFAULT_SETTINGS, "sentiment_targeted_retry": False}
try:
    reply["data"] = {"earnings_summary": "Q1 FY27 PAT Rs 32 cr, up 25% YoY", "future_guidance": "N/A",
                     "analyst_coverage": "N/A", "earnings_report_date": "2026-08-10", "eps_value": None,
                     "guidance_change": None, "analyst_action": None, "analyst_firm": None,
                     "outlook_tone": "improving", "outlook_quote": QUOTE, "profit_metric": "PAT", "profit_yoy_pct": 25,
                     "revenue_yoy_pct": 20,
                     "results_vs_estimate": None, "sentiment": "Neutral", "reasoning": "No consensus to compare."}
    row = {"ticker": "ZED.NS", "market": "india_invested", "company_name": "Zed Ltd",
           "reported_qtr": "Q1 FY27", "qtr_profit_growth": 40.0, "qtr_revenue_growth": 30.0}
    v = fe.generate_fundamental_view(None, row, news_text="Q1 FY27 PAT Rs 32 cr, up 25% YoY", news_source="test")
    check(v.get("sentiment") == "Positive" and v.get("model_sentiment") == "Neutral",
          f"the code's verdict (outlook improving) is stored; the model's own is kept ({v.get('sentiment')}, {v.get('model_sentiment')})")
    check(v.get("profit_yoy_pct") == 25.0 and v.get("profit_yoy_source") == "news",
          "the figure from the news wins over Yahoo's")
    check(isinstance(v.get("sentiment_drivers"), list) and v["sentiment_drivers"], "the drivers are stored")
    check(all(k in reply["prompt"] for k in ("profit_yoy_pct", "revenue_yoy_pct", "results_vs_estimate")),
          "the prompt asks for the new fields")

    check(v.get("revenue_yoy_pct") == 20.0 and v.get("revenue_yoy_source") == "news", "sales growth from the news is kept")
    reply["data"] = {**reply["data"], "earnings_summary": "Q1 FY27 PAT Rs 32 cr", "profit_yoy_pct": None,
                     "revenue_yoy_pct": None}
    v = fe.generate_fundamental_view(None, row, news_text="Q1 FY27 PAT Rs 32 cr", news_source="test")
    check(v.get("profit_yoy_pct") == 40.0 and v.get("revenue_yoy_pct") == 30.0
          and "Yahoo" in str(v.get("profit_yoy_source")),
          f"absolute figures only -> Yahoo's YoY for the same quarter fills both in "
          f"({v.get('profit_yoy_pct')}, {v.get('revenue_yoy_pct')}, {v.get('profit_yoy_source')})")

    reply["data"] = {**reply["data"], "earnings_summary": "Q1 FY27 PAT Rs 32 cr, up 25% YoY", "profit_yoy_pct": 25,
                     "outlook_tone": "cautious", "outlook_quote": QUOTE, "sentiment": "Positive"}
    v = fe.generate_fundamental_view(None, row, news_text="Q1 FY27 PAT Rs 32 cr, up 25% YoY", news_source="test")
    check(v.get("sentiment") == "Negative", f"end to end: +25% profit but management cautious -> Negative ({v.get('sentiment')})")
    reply["data"] = {**reply["data"], "earnings_summary": "N/A", "outlook_tone": None, "outlook_quote": None,
                     "profit_yoy_pct": None, "revenue_yoy_pct": None, "sentiment": "Unknown"}
    v = fe.generate_fundamental_view(None, row, news_text="No recent fundamental news found.", news_source="test")
    check(v.get("profit_yoy_pct") is None and v.get("revenue_yoy_pct") is None,
          "no results in the news -> Yahoo does NOT fill in (nothing to anchor the quarter)")
finally:
    llm_util.run_model_ladder, fe._fetch_last_reported_earnings_date, sd.load_settings = real_ladder, real_lookup, real_settings

# --- prompts, settings, placeholders, consumers -----------------------------------------
p = fe.build_sentiment_prompt("Acme Corp", "ACME", "news")
check("FORWARD-LOOKING" in p and "never decides on its own" in p,
      "the prompt says Sentiment is forward-looking and the reported quarter never decides alone")
check("decides the sentiment on its own" in p, "the prompt says a quoted outlook decides on its own")
check("can only tip" in p, "the prompt says the reported quarter can only tip a balance")
captured = {}
llm_util.run_model_ladder = lambda client, prompt, *a, **k: (captured.setdefault("p", prompt), (None, None))[1]
try:
    fe.fetch_fundamental_news(None, "ACME", "us_invested", "Acme Corp", is_retry=True)
finally:
    llm_util.run_model_ladder = real_ladder
check("year-ago" in captured.get("p", ""), "the search asks for year-ago figures")
check(sd.DEFAULT_SETTINGS.get("sentiment_profit_yoy_pct") == 15.0, "the profit threshold is a setting, default 15")
check("sentiment_profit_yoy_pct" not in sd.calc_settings(sd.DEFAULT_SETTINGS), "...not a calculation setting")
check("set_sentiment_profit_yoy" in (REPO / "app.py").read_text(), "...with a Settings field")
fb = rf._unknown_fallback("x")
check(all(k in fb and fb[k] is None for k in ("profit_yoy_pct", "revenue_yoy_pct", "results_vs_estimate")),
      "the Unknown placeholder carries the new keys")

import expert_views as ev
txt = ev._quarter_fundamentals_text({**scored_pos, "profit_metric": "PAT", "results_vs_estimate": "beat"})
check("Profit YoY: +22% (PAT)" in txt and "Sales YoY: +18%" in txt and "beat" in txt,
      "Expert Take section 4 shows the sales and profit changes and the beat")
src = (REPO / "app.py").read_text()
check("sentiment_drivers" in src, "the Sentiment cell's hover text says what decided it")

print("TOTAL", "all passed" if not fails else "")
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
