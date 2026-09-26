"""The Expert Take prompt must not be shown its own previous verdict.

Two paths carried it back in:
  1. Section 3 printed the row's flag as a "user flag". Unless set by hand, that
     flag is ticker_notes' auto-vote, which counts last night's Expert Take.
  2. Section 2 listed the alert rules true for the ticker, including rules on
     `flag` (or on expert_take itself), which fire BECAUSE of that verdict.

Notes are left out of the prompt too, at the user's request.

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
    {"id": "green", "name": "green flag", "enabled": True, "conditions": [cond("flag", ["Green"])]},
    {"id": "acc", "name": "accumulate", "enabled": True, "conditions": [cond("expert_take", ["Accumulate"])]},
    {"id": "via", "name": "refers to green", "enabled": True,
     "conditions": [{"type": "rule", "rule_id": "green", "logic": "AND"}]},
    # Enabled rule leaning on a DISABLED flag rule: still built on the verdict.
    {"id": "off", "name": "disabled flag rule", "enabled": False, "conditions": [cond("flag", ["Green"])]},
    {"id": "via_off", "name": "refers to disabled", "enabled": True,
     "conditions": [{"type": "rule", "rule_id": "off", "logic": "AND"}]},
    {"id": "b_side", "name": "metric b", "enabled": True,
     "conditions": [{"metric_a": "trend", "operator": "==", "compare_type": "metric",
                     "metric_b": "expert_take", "logic": "AND"}]},
    {"id": "trend", "name": "uptrend", "enabled": True, "conditions": [cond("trend", ["Uptrend"])]},
    {"id": "trend_ref", "name": "refers to uptrend", "enabled": True,
     "conditions": [{"type": "rule", "rule_id": "trend", "logic": "AND"}]},
]
check(alerts.rules_using_prior_verdict(RULES) == {"green", "acc", "via", "off", "via_off", "b_side"},
      "rules on flag/expert_take are excluded, directly, as Metric B, or through a referenced rule")

alerts.load_rules = lambda: [dict(r) for r in RULES]
row = {**AUTO, "expert_take": "Accumulate"}
text = alerts.alerts_text_for(alerts.active_alerts_for_prompt([row], {}), "ACME")
check("uptrend" in text and "refers to uptrend" in text, "rules on price data still reach the prompt")
check(not any(n in text for n in ("green flag", "accumulate", "refers to green", "refers to disabled")),
      "rules that fire because of the last verdict do not")

alerts.load_rules = lambda: [dict(r) for r in RULES[:2]]
check(alerts.active_alerts_for_prompt([row], {}) is None,
      "with only verdict-based rules left, the prompt says alerts were not evaluated")

print("TOTAL", "all passed" if not fails else "")
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
