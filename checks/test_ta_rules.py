"""TA Rules column: TheWrap's weekly flowchart, node for node.

    EMAs converging? -- Yes: broken support -> Exit, broken resistance ->
    Bullish Signal, else Wait/Watch. No: broken 40W -> Exit, 20W -> Be
    Cautious, 10W -> Momentum Fading, else Maintain/Add.

"Broken" = the weekly close is more than ta_break_pct past the line; an S/R
zone must also have had a close on its other side within ta_sr_recent_weeks.
Zones are weekly-wick turning points (with a real move away) clustered by
price; highs and lows count alike (role reversal).

Offline: invented price paths, no network, no data files.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from datetime import datetime, timezone

import pandas as pd

import filters
import stock_data as sd

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def verdict(close, emas, zones=(), recent=(), **kw):
    return sd.compute_ta_rules(close, *emas, list(zones), list(recent), **kw)[0]


def weekly_from(closes, wick=0.0):
    """W-FRI bars from a close path; High/Low sit `wick` (fraction) outside it."""
    idx = pd.date_range("2023-01-06", periods=len(closes), freq="W-FRI")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * (1 + wick), "Low": c * (1 - wick),
                         "Close": c, "Volume": 1000.0})


# --- the trending branch: 40W, then 20W, then 10W ---------------------------
TREND = (110.0, 100.0, 90.0)   # fast, mid, slow; spread 22% of the slow one
check(verdict(120, TREND) == "Maintain/Add", "above all three WEMAs -> Maintain/Add")
check(verdict(108, TREND) == "Maintain/Add",
      "1.8% under the 10W is inside the 3% buffer -> still Maintain/Add")
check(verdict(106, TREND) == "Momentum Fading", "3.6% under the 10W -> Momentum Fading")
check(verdict(96, TREND) == "Be Cautious", "more than 3% under the 20W -> Be Cautious")
check(verdict(90 * 0.98, TREND) == "Be Cautious", "2% under the 40W is NOT broken")
check(verdict(90 * 0.96, TREND) == "Exit", "4% under the 40W is broken -> Exit")
check(verdict(95, TREND, break_pct=0) == "Be Cautious",
      "break_pct=0: any close under the 20W breaks it")
# Literal reading, flagged to the user: fanned-out EMAs in a DOWNtrend and a
# bounce above all three is still the chart's "Maintain/Add".
check(verdict(95, (80.0, 85.0, 92.0)) == "Maintain/Add",
      "downtrend EMAs, close above all three -> Maintain/Add (as drawn)")
check(verdict(70, (80.0, 85.0, 92.0)) == "Exit", "downtrend, close under the 40W -> Exit")

# --- the converging branch: support, then resistance -------------------------
FLAT = (100.0, 99.0, 98.0)   # spread 2.04% of the slow one
SUP = {"low": 80.0, "high": 82.0, "touches": 2, "last_touch": "2025-01-03"}
RES = {"low": 100.0, "high": 102.0, "touches": 2, "last_touch": "2025-06-06"}
ZONES = [SUP, RES]
RECENT_INSIDE = [90.0, 95.0]
check(verdict(106, FLAT, ZONES, RECENT_INSIDE) == "Bullish Signal",
      "close > 102 x 1.03 after closes below it -> Bullish Signal")
check(verdict(104, FLAT, ZONES, RECENT_INSIDE) == "Wait/Watch",
      "close above resistance but inside the 3% buffer -> Wait/Watch")
check(verdict(77, FLAT, ZONES, RECENT_INSIDE) == "Exit", "close < 80 x 0.97 after closes above it -> Exit")
check(verdict(79, FLAT, ZONES, RECENT_INSIDE) == "Wait/Watch", "close inside the support buffer -> Wait/Watch")
check(verdict(90, FLAT, ZONES, RECENT_INSIDE) == "Wait/Watch", "inside the range -> Wait/Watch")
check(verdict(90, FLAT) == "Wait/Watch", "no zones at all -> the chart's No -> No path, Wait/Watch")
# Recency: price has sat under support for the whole window -> not a fresh
# break; without this every old zone above a falling stock would read Exit.
check(verdict(77, FLAT, ZONES, [74.0, 75.0]) == "Wait/Watch",
      "support last held before the recent window -> not broken")
check(verdict(106, FLAT, ZONES, [107.0, 108.0]) == "Wait/Watch",
      "already above resistance all window -> not a fresh breakout")
# Support is asked first: a close that breaks one zone's support and another's
# resistance reads Exit.
LOW_RES = {"low": 69.0, "high": 70.0, "touches": 2, "last_touch": "2024-03-01"}
check(verdict(77, FLAT, [SUP, LOW_RES], [81.0, 69.5]) == "Exit",
      "support and resistance both broken -> support is checked first -> Exit")

# Convergence threshold
check(verdict(106, (103.0, 101.5, 100.0), ZONES, RECENT_INSIDE) == "Bullish Signal",
      "spread exactly 3% counts as converging")
check(verdict(106, (103.1, 101.5, 100.0), ZONES, RECENT_INSIDE) == "Maintain/Add",
      "spread 3.1% is not converging -> the EMA branch decides")
check(verdict(106, (104.0, 101.0, 100.0), ZONES, RECENT_INSIDE, converge_pct=5) == "Bullish Signal",
      "converge_pct is honoured")

_, d = sd.compute_ta_rules(77, *FLAT, ZONES, RECENT_INSIDE)
check(d["decided_by"] == {"test": "support", "zone": SUP}, "detail names the zone that decided it")
_, d = sd.compute_ta_rules(90, *FLAT, ZONES, RECENT_INSIDE)
check(d["support"] == SUP and d["resistance"] == RES,
      "detail carries the zone under price (support) and over it (resistance)")

# --- zones -----------------------------------------------------------------
# Flat at 90 with two dips to ~80 that rebound 12.5% -> one 2-touch zone.
path = [90.0] * 60
path[10], path[30] = 80.0, 81.0
zones = sd.find_sr_zones(weekly_from(path))
check(len(zones) == 1 and zones[0]["touches"] == 2 and zones[0]["low"] == 80.0 and zones[0]["high"] == 81.0,
      f"two rebounds from ~80 form one zone 80-81 (got {zones})")

one = [90.0] * 60
one[10] = 80.0
check(sd.find_sr_zones(weekly_from(one)) == [], "a single turning point is not a level")
check(len(sd.find_sr_zones(weekly_from(one), min_touches=1)) == 1, "min_touches is honoured")

shallow = [90.0] * 60
shallow[10], shallow[30] = 85.5, 85.5   # a 5.3% rebound, under the 8% reaction
check(sd.find_sr_zones(weekly_from(shallow)) == [], "dips that rebound less than 8% are not turning points")
check(len(sd.find_sr_zones(weekly_from(shallow), reaction_pct=5)) == 1, "reaction_pct is honoured")

apart = [95.0] * 60
apart[10], apart[30] = 80.0, 83.0   # 3.75% apart, wider than the 3% zone
check(sd.find_sr_zones(weekly_from(apart)) == [], "turning points 3.75% apart are two levels, not one")
check(len(sd.find_sr_zones(weekly_from(apart), zone_pct=6)) == 1, "zone_pct is honoured")

# Role reversal: a ceiling at 100 (then a drop to 85), later broken, retested
# from above at 101 and bounced to 112 -> one level, two touches.
rr = [85.0] * 10 + [100.0] + [85.0] * 15 + [110.0] * 10 + [101.0] + [112.0] * 10
rz = sd.find_sr_zones(weekly_from(rr))
check(any(z["low"] == 100.0 and z["high"] == 101.0 and z["touches"] == 2 for z in rz),
      f"a broken ceiling retested as a floor is one level (got {rz})")

# Wicks, not closes: the dips only show in the Low.
wick = weekly_from([90.0] * 60)
wick.iloc[10, wick.columns.get_loc("Low")] = 80.0
wick.iloc[30, wick.columns.get_loc("Low")] = 80.5
wz = sd.find_sr_zones(wick)
check(len(wz) == 1 and wz[0]["low"] == 80.0, "turning points are read from the weekly wicks")

# The breakout week can't make its own level: the last pivot_weeks bars
# have no right side yet.
late = [90.0] * 30
late[10], late[-2] = 80.0, 80.0
check(sd.find_sr_zones(weekly_from(late)) == [], "a dip 1 week ago is not yet a confirmed turning point")

# --- completed weeks only ---------------------------------------------------
wk = weekly_from([100.0] * 5)
wk.index = pd.date_range("2026-08-28", periods=5, freq="W-FRI")   # ends Fri 2026-09-25
last = wk.index[-1]


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


check(sd.completed_weekly_bars(wk, "ACME", now=utc(2026, 9, 23, 18)).index[-1] < last,
      "Wednesday: this week's bar is still forming and is dropped")
check(sd.completed_weekly_bars(wk, "ACME", now=utc(2026, 9, 25, 15)).index[-1] < last,
      "Friday 11:00 ET, session open: still forming")
check(sd.completed_weekly_bars(wk, "ACME", now=utc(2026, 9, 25, 23)).index[-1] == last,
      "Friday 19:00 ET, session settled: complete")
check(sd.completed_weekly_bars(wk, "ACME", now=utc(2026, 9, 26, 12)).index[-1] == last,
      "Saturday: complete")
check(sd.completed_weekly_bars(wk, "ZED.NS", now=utc(2026, 9, 25, 5)).index[-1] < last,
      "India Friday 10:30 IST: still forming")
check(sd.completed_weekly_bars(wk, "ZED.NS", now=utc(2026, 9, 25, 12)).index[-1] == last,
      "India Friday 17:30 IST: complete")
# A holiday Friday: the week is over on Monday even though Friday never
# printed. The VStop test (is the label a trading day?) would hold it forever.
check(sd.completed_weekly_bars(wk, "ACME", now=utc(2026, 9, 28, 14)).index[-1] == last,
      "a past Friday counts as complete whether or not it traded")

# --- registration -------------------------------------------------------------
check(filters.CATEGORICAL_METRICS["ta_rules"] == list(sd.TA_RULES_OUTCOMES),
      "the filter dropdown offers exactly the six outcomes, best-first")
check("ta_rules" in filters.TEXT_METRICS, "ta_rules is a text metric (kept out of Metric B)")
check(sd.get_filterable_metrics(dict(sd.DEFAULT_SETTINGS)).get("TA Rules") == "ta_rules",
      "TA Rules is filterable and alertable")
ta_keys = [k for k in sd.DEFAULT_SETTINGS if k.startswith("ta_")]
check(len(ta_keys) == 9 and all(k in sd.calc_settings(sd.DEFAULT_SETTINGS) for k in ta_keys),
      f"all 9 TA thresholds are settings, and changing one invalidates the snapshot ({ta_keys})")
row = {"ta_rules": "Exit"}
check(filters.passes_filter(row, {"metric_a": "ta_rules", "operator": "in", "compare_type": "value",
                                  "value": ["Exit", "Be Cautious"]}),
      "a rule 'TA Rules in [Exit, Be Cautious]' matches an Exit row")

print("TOTAL", "all passed" if not fails else "")
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
