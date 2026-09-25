"""reject_stale_rows: a held (newer) stored row takes the fields it LACKS from
the fresh row, and never goes backwards in time.

2026-09-24: right after the TA Rules column shipped, Yahoo served five .BO
tickers a session behind. The guard correctly kept the newer stored rows --
but those were computed before TA Rules existed, so the column read blank on
them until Yahoo caught up. Now the stored row keeps every price and
indicator it has and only gains the absent keys.

Also run through a real save/load of the snapshot file twice, so a later
refresh that again gets older data cannot undo either half.

Offline: invented tickers, a temp snapshot file, no network.
"""

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import stock_data as sd

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


TODAY = date(2026, 9, 24)
# stored: newer session, computed by the old code (no ta_rules key at all)
STORED = {"ticker": "ZED.BO", "data_end": "2026-09-23", "last_close": 110.0, "ema40": 100.0}
# fresh: Yahoo a session behind, computed by the new code
FRESH = {"ticker": "ZED.BO", "data_end": "2026-09-22", "last_close": 105.0, "ema40": 99.0,
         "ta_rules": "Maintain/Add", "ta_rules_detail": {"week": "2026-09-18"}}

stored_before = dict(STORED)
out, stale = sd.reject_stale_rows({"m": [dict(FRESH)]}, {"m": [STORED]}, today=TODAY)
row = out["m"][0]
check(row["data_end"] == "2026-09-23" and row["last_close"] == 110.0 and row["ema40"] == 100.0,
      "the newer stored row's prices and indicators are kept -- nothing moves backwards")
check(row.get("ta_rules") == "Maintain/Add" and row.get("ta_rules_detail") == {"week": "2026-09-18"},
      "fields the stored row lacked are filled from the fresh row")
check(stale == {"m": ["ZED.BO"]}, "the ticker is still reported as served stale")
check(STORED == stored_before, "the previous snapshot's row object is not mutated")

# Same keys on both sides (the normal case): the stored row comes back as-is.
same_old = dict(STORED, ta_rules="Exit", ta_rules_detail={"week": "2026-09-11"})
out, _ = sd.reject_stale_rows({"m": [dict(FRESH)]}, {"m": [same_old]}, today=TODAY)
check(out["m"][0] == same_old, "a held row that already has every field is unchanged (its own ta_rules wins)")

# A NEWER fresh row still replaces the stored one outright.
newer = dict(FRESH, data_end="2026-09-24", last_close=120.0)
out, stale = sd.reject_stale_rows({"m": [newer]}, {"m": [STORED]}, today=TODAY)
check(out["m"][0] == newer and not stale, "a newer fetch replaces the stored row entirely")

# The max_hold_days valve is untouched: a stored row too old to hold loses.
out, _ = sd.reject_stale_rows({"m": [dict(FRESH)]}, {"m": [STORED]}, today=date(2026, 10, 5))
check(out["m"][0] == FRESH, "past max_hold_days the fresh row wins, as before")

# --- two refreshes through the real snapshot file ------------------------------
d = tempfile.mkdtemp()
orig = sd.DATA_SNAPSHOT_FILE
sd.DATA_SNAPSHOT_FILE = os.path.join(d, "data_snapshot.json")
try:
    sd.save_data_snapshot("2026-09-23 18:00 ET", {"m": [dict(STORED)]})

    def refresh(fresh_row):
        previous = (sd.load_data_snapshot() or {}).get("per_market") or {}
        per_market, _ = sd.reject_stale_rows({"m": [dict(fresh_row)]}, previous, today=TODAY)
        per_market, _ = sd.fill_snapshot_gaps(per_market, previous, {"m": ["ZED.BO"]})
        sd.save_data_snapshot("2026-09-24 00:00 ET", per_market)
        return sd.load_data_snapshot()["per_market"]["m"][0]

    first = refresh(FRESH)
    check(first["data_end"] == "2026-09-23" and first.get("ta_rules") == "Maintain/Add",
          "refresh 1 (older data): saved row keeps 09-23 prices and gains TA Rules")

    # Refresh 2: Yahoo STILL a session behind, now with a different verdict.
    second = refresh(dict(FRESH, ta_rules="Exit", last_close=90.0))
    check(second["data_end"] == "2026-09-23" and second["last_close"] == 110.0,
          "refresh 2 (older data again): the 09-23 row is not undone")
    check(second.get("ta_rules") == "Maintain/Add",
          "refresh 2: the held row keeps its own TA Rules rather than the older fetch's")

    # Refresh 3: Yahoo catches up -- the whole row moves forward.
    third = refresh(dict(FRESH, data_end="2026-09-24", last_close=121.0, ta_rules="Bullish Signal"))
    check(third["data_end"] == "2026-09-24" and third["last_close"] == 121.0 and third["ta_rules"] == "Bullish Signal",
          "refresh 3 (newer data): the row is replaced by the newer fetch")
finally:
    sd.DATA_SNAPSHOT_FILE = orig

# --- a renamed verdict value is migrated as the snapshot loads -----------------
# "Maintain Position / Add" became "Maintain/Add" (2026-09-24). A HELD row keeps
# its stored value, so the rename has to happen on load, not only on refresh.
sd.DATA_SNAPSHOT_FILE = os.path.join(d, "data_snapshot.json")
try:
    sd.save_data_snapshot("2026-09-24 00:00 ET", {"m": [dict(STORED, ta_rules="Maintain Position / Add")]})
    loaded = sd.load_data_snapshot()["per_market"]["m"][0]
    check(loaded["ta_rules"] == "Maintain/Add", "an old 'Maintain Position / Add' loads as 'Maintain/Add'")
    held, _ = sd.reject_stale_rows({"m": [dict(FRESH, ta_rules="Exit")]}, sd.load_data_snapshot()["per_market"],
                                   today=TODAY)
    check(held["m"][0]["ta_rules"] == "Maintain/Add", "and a held row is saved back under the new label")
finally:
    sd.DATA_SNAPSHOT_FILE = orig

print("TOTAL", "all passed" if not fails else "")
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
