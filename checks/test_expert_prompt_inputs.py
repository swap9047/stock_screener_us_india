"""The Expert Take prompt must not be shown its own previous verdict.

Two paths carried it back in:
  1. Section 3 printed the row's flag as a "user flag". Unless set by hand, that
     flag is ticker_notes' auto-vote, which counts last night's Expert Take.
  2. Section 2 listed the alert rules true for the ticker, including rules on
     `flag` (or on expert_take itself), which fire BECAUSE of that verdict.

Notes are left out of the prompt too, at the user's request.

Section 4 is new: this quarter's checked fundamentals from the Sentiment job
(results, guidance, outlook, named-firm analyst actions), plus the data date,
currency, TA Rules verdict and valuation in section 1.

Offline: invented tickers, stubbed settings and rules, no data files, no network.
"""

import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO)

import alerts
import expert_views
import stock_data
from ticker_notes import MANUAL_FLAG_REASON

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


stock_data.load_settings = lambda: dict(stock_data.DEFAULT_SETTINGS)

BASE = {"ticker": "ACME", "company_name": "Acme Corp", "market": "us_invested",
        "last_close": 100.0, "trend": "Uptrend", "tech_uptrend": 1}
AUTO = {**BASE, "flag": "Green", "note": "sold half in March",
        "flag_reason": "3/4 bullish votes (Expert Take=Accumulate, Trend=Uptrend, Tech Uptrend=Yes)"}
MANUAL = {**BASE, "flag": "Red", "note": "sold half in March", "flag_reason": MANUAL_FLAG_REASON}

p_auto = expert_views.build_expert_prompt(AUTO, "Some news.", "")
p_manual = expert_views.build_expert_prompt(MANUAL, "Some news.", "")
check("- Flag: None" in p_auto and "Green" not in p_auto and "Expert Take=" not in p_auto,
      "an auto-voted flag is not shown to the model")
check("- Flag: Red" in p_manual, "a flag set by hand is still shown")
check("sold half in March" not in p_auto and "sold half in March" not in p_manual and "Note:" not in p_manual,
      "notes are not shown to the model")
check("USER FLAGS & NOTES" not in p_manual and "USER FLAG (set by hand)" in p_manual,
      "section 3 says the flag is one set by hand")


# --- alert rules built on a previous verdict ---------------------------------
def cond(metric, value):
    return {"metric_a": metric, "operator": "in", "compare_type": "value", "value": value, "logic": "AND"}


RULES = [
    {"id": "green", "name": "expert accumulate", "enabled": True, "conditions": [cond("expert_take", ["Accumulate"])]},
    {"id": "acc", "name": "accumulate", "enabled": True, "conditions": [cond("expert_take", ["Accumulate"])]},
    {"id": "via", "name": "refers to green", "enabled": True,
     "conditions": [{"type": "rule", "rule_id": "green", "logic": "AND"}]},
    # Enabled rule leaning on a DISABLED verdict rule: still built on the verdict.
    {"id": "off", "name": "disabled verdict rule", "enabled": False, "conditions": [cond("expert_news_backed", ["Yes"])]},
    {"id": "via_off", "name": "refers to disabled", "enabled": True,
     "conditions": [{"type": "rule", "rule_id": "off", "logic": "AND"}]},
    {"id": "b_side", "name": "metric b", "enabled": True,
     "conditions": [{"metric_a": "trend", "operator": "==", "compare_type": "metric",
                     "metric_b": "expert_take", "logic": "AND"}]},
    {"id": "trend", "name": "uptrend", "enabled": True, "conditions": [cond("trend", ["Uptrend"])]},
    {"id": "trend_ref", "name": "refers to uptrend", "enabled": True,
     "conditions": [{"type": "rule", "rule_id": "trend", "logic": "AND"}]},
    # Flag is set only by hand since 2026-10-02, so a rule on it is the user's
    # judgement, not a prior verdict, and may reach the prompt.
    {"id": "manual", "name": "my green flags", "enabled": True, "conditions": [cond("flag", ["Green"])]},
]
check(alerts.rules_using_prior_verdict(RULES) == {"green", "acc", "via", "off", "via_off", "b_side"},
      "rules on expert_take/expert_news_backed are excluded, directly, as Metric B, or through a referenced rule")
check("manual" not in alerts.rules_using_prior_verdict(RULES),
      "a rule on the (now manual-only) Flag is not excluded")

alerts.load_rules = lambda: [dict(r) for r in RULES]
row = {**AUTO, "expert_take": "Accumulate", "expert_news_backed": "Yes"}
text = alerts.alerts_text_for(alerts.active_alerts_for_prompt([row], {}), "ACME")
check("uptrend" in text and "refers to uptrend" in text, "rules on price data still reach the prompt")
check(not any(n in text for n in ("expert accumulate", "refers to green", "refers to disabled")),
      "rules that fire because of the last verdict do not")

alerts.load_rules = lambda: [dict(r) for r in RULES[:2]]
check(alerts.active_alerts_for_prompt([row], {}) is None,
      "with only verdict-based rules left, the prompt says alerts were not evaluated")

# --- section 4: this quarter's checked fundamentals ---------------------------
# The Expert Take search only looks back 24 hours, so results and guidance never
# reached it. It now gets the Sentiment job's checked facts -- not its label.
import fundamentals_eval
from datetime import datetime, timezone

NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
FACTS = {"as_of": NOW, "sentiment": "Positive", "earnings_report_date": "2026-08-07",
         "earnings_summary": "Q2 EPS $1.10 vs $1.00 est., revenue +12% YoY",
         "future_guidance": "Raised FY revenue guidance to $5.1-5.3B",
         "analyst_coverage": "Upgraded to Overweight by Morgan Stanley",
         "eps_value": "$1.10 vs $1.00 est.", "guidance_change": "raised",
         "analyst_action": "upgrade", "analyst_firm": "Morgan Stanley",
         "outlook_tone": "improving", "outlook_quote": "CEO on 2026-08-07: 'demand strengthening into Q4'"}
ROW = {**BASE, "data_end": "2026-09-25", "trailing_pe": 31.24, "roce": 18.5, "reported_qtr": "Q2 2026",
       "qtr_eps_growth": 22.0, "ta_rules": "Maintain/Add",
       "ta_rules_detail": {"week": "2026-09-25", "periods": [10, 20, 40], "spread_pct": 9.1,
                           "converging": False, "break_pct": 3.0}}

p4 = expert_views.build_expert_prompt(ROW, "No recent news found.", "", FACTS)
sec4 = p4[p4.find("4. THIS QUARTER'S FUNDAMENTALS"):p4.find("5. RECENT WEB NEWS")]
check(all(x in sec4 for x in ("Results announced: 2026-08-07", "EPS: $1.10", "Company guidance: RAISED",
                               "Management outlook (improving)", "Analyst action: upgrade by Morgan Stanley")),
      "section 4 carries the quarter's results, guidance, outlook and named-firm analyst action")
check("Positive" not in sec4 and "Sentiment" not in sec4, "section 4 gives the facts, not Sentiment's label")
check("No news in the last 24 hours. This quarter's fundamentals are in section 4." in p4
      and "NEWS DATA: ABSENT" not in p4, "24h silence with quarter facts is not reported as 'news absent'")
check("NEWS DATA: ABSENT" in expert_views.build_expert_prompt(ROW, "No recent news found.", "", {}),
      "with no 24h news AND no quarter facts, the absent-news warning still fires")
check('"News" in these rules means BOTH section 4' in p4, "the verdict rules count section 4 as news")

site = {**FACTS, "analyst_action": "upgrade", "analyst_firm": "StockInvest.us"}
check("StockInvest" not in expert_views._quarter_fundamentals_text(site),
      "a rating-site 'upgrade' does not reach section 4")
stale = {**FACTS, "as_of": "2026-01-01 00:00"}
check(expert_views._quarter_fundamentals_text(stale).startswith("Not usable"), "a stale Sentiment view is withheld")
check(expert_views._quarter_fundamentals_text(None).startswith("Not provided"),
      "an unavailable file reads as NO INFORMATION, not as no news")

check(all(x in p4 for x in ("Data as of: 2026-09-25 (last daily close; prices in USD)",
                             "TA Rules (TheWrap weekly EMA flowchart): Maintain/Add (week ending 2026-09-25",
                             "Trailing P/E=31.2", "ROCE=18.5%", "Latest quarter (Q2 2026) YoY growth: EPS=22.0%")),
      "section 1 carries the data date, currency, TA Rules verdict, valuation and quarter growth")
check("prices in INR" in expert_views.build_expert_prompt({**ROW, "ticker": "ZED.NS"}, "x", "", FACTS),
      "an India ticker is priced in INR")

# generate_expert_view reads fundamentals.json itself unless handed a view, and
# an unreadable file degrades to "not provided" instead of stopping the run.
import llm_util
seen = {}
_real_ladder, _real_load = llm_util.run_model_ladder, fundamentals_eval.load_fundamentals
llm_util.run_model_ladder = lambda client, prompt, *a, **k: (seen.__setitem__("p", prompt), (None, None))[1]
try:
    fundamentals_eval.load_fundamentals = lambda: {"ACME": FACTS}
    expert_views.generate_expert_view(None, ROW, news_text="x", news_source="s", active_alerts_text="")
    check("upgrade by Morgan Stanley" in seen["p"], "by default the ticker's facts are read from fundamentals.json")

    def _broken():
        raise ValueError("torn file")
    fundamentals_eval.load_fundamentals = _broken
    expert_views.generate_expert_view(None, ROW, news_text="x", news_source="s", active_alerts_text="")
    check("Not provided for this run" in seen["p"], "an unreadable fundamentals.json does not stop Expert Take")
finally:
    llm_util.run_model_ladder, fundamentals_eval.load_fundamentals = _real_ladder, _real_load

# Expert News? counts the quarter's facts, not only the 24-hour search.
check(expert_views.expert_view_has_news({"news_used": "No recent news found.", "quarter_facts_used": True}),
      "Expert News? is Yes when only this quarter's facts were behind the verdict")
check(not expert_views.expert_view_has_news({"news_used": "No recent news found.", "quarter_facts_used": False}),
      "...and No when neither source had anything")
check(expert_views.expert_view_has_news({"news_used": "Upgraded by X on 2026-09-25"}),
      "an older stored view without the new key still reads its 24-hour news")

print("TOTAL", "all passed" if not fails else "")
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
