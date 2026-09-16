"""slot_gate.decide: one run per ET slot, late runs allowed, dropped crons harmless."""

import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
GATE = str(Path(__file__).resolve().parents[1] / ".github/actions/slot-gate")
sys.path.insert(0, REPO)
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, GATE)

import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import slot_gate

ET = ZoneInfo("America/New_York")
fails = 0
def check(ok, label):
    global fails; fails += not ok; print("PASS" if ok else "FAIL", label)

def et(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=ET)

def decide(now, slots=(21,), grace=22, days=None, worked=()):
    return slot_gate.decide(now, slots, grace, days, list(worked))

# --- the daily 21:00 ET slot
run, why = decide(et(2026, 9, 15, 21, 20))
check(run, f"on time (21:20 ET) -> run ({why})")
run, why = decide(et(2026, 9, 16, 2, 30))
check(run, f"late, 5.5h after the slot -> run ({why})")
run, why = decide(et(2026, 9, 16, 18, 30))
check(run, f"very late, 21.5h after -> still run inside the 22h window ({why})")
run, why = decide(et(2026, 9, 16, 19, 30))
check(not run, f"22.5h after -> stale, skip ({why})")
run, why = decide(et(2026, 9, 15, 20, 30))
check(not run and "already" not in why, f"before the first slot of the day -> previous slot is 21:00 yesterday, 23.5h old -> skip ({why})")

# --- one run per slot
worked_now = [et(2026, 9, 15, 21, 30).astimezone(timezone.utc)]
run, why = decide(et(2026, 9, 15, 23, 0), worked=worked_now)
check(not run, f"work already done for this slot -> skip ({why})")
run, why = decide(et(2026, 9, 16, 21, 30), worked=worked_now)
check(run, f"yesterday's work does not satisfy today's slot ({why})")
before_slot = [et(2026, 9, 15, 13, 0).astimezone(timezone.utc)]
run, why = decide(et(2026, 9, 15, 21, 30), worked=before_slot)
check(run, f"a run from earlier the same day (before the slot) does not count ({why})")

# --- weekday filter applies to the SLOT's day, not the run's
run, why = decide(et(2026, 9, 13, 23, 0), days=["SUN"])   # 2026-09-13 is a Sunday
check(run, f"Sunday 21:00 slot, run at 23:00 Sunday -> run ({why})")
run, why = decide(et(2026, 9, 14, 3, 0), days=["SUN"])
check(run, f"Sunday slot, run 06:00 later on Monday -> still Sunday's slot ({why})")
run, why = decide(et(2026, 9, 15, 21, 30), days=["SUN"])
check(not run, f"Monday 21:00 slot is not a Sunday slot -> skip ({why})")

# --- daylight saving: slot is wall-clock 21:00 ET on both sides
for label, day in (("spring forward", (2026, 3, 8)), ("fall back", (2026, 11, 1))):
    slot = slot_gate.latest_slot(et(day[0], day[1], day[2], 21, 30), (21,))
    check(slot.hour == 21 and slot.date() == datetime(*day).date(), f"{label}: slot is 21:00 ET that day ({slot})")

# --- market breadth: two slots a day, 10h window
B = dict(slots=(10, 22), grace=10)
run, why = decide(et(2026, 9, 15, 11, 0), **B)
check(run and slot_gate.latest_slot(et(2026, 9, 15, 11, 0), (10, 22)).hour == 10, f"11:00 ET -> the 10:00 slot ({why})")
run, why = decide(et(2026, 9, 16, 3, 0), **B)
check(run and slot_gate.latest_slot(et(2026, 9, 16, 3, 0), (10, 22)).hour == 22, f"03:00 ET -> yesterday's 22:00 slot, 5h old ({why})")
run, why = decide(et(2026, 9, 16, 9, 0), **B)
check(not run, f"09:00 ET -> 22:00 slot is 11h old, past the 10h window ({why})")
run, why = decide(et(2026, 9, 15, 23, 0), worked=[et(2026, 9, 15, 22, 10).astimezone(timezone.utc)], **B)
check(not run, f"second fire inside the 22:00 slot -> skip ({why})")
run, why = decide(et(2026, 9, 15, 23, 0), worked=[et(2026, 9, 15, 11, 0).astimezone(timezone.utc)], **B)
check(run, f"the 10:00 slot's run does not satisfy the 22:00 slot ({why})")

# --- runs whose work job did NOT run must not count (the Sunday bug)
run, why = decide(et(2026, 9, 15, 23, 0), worked=[])
check(run, f"an earlier run that skipped its work job leaves the slot undone ({why})")

# --- is_rule_due after dropping the cron-season argument

import inspect
import alerts
check("cron_schedule" not in inspect.signature(alerts.is_rule_due).parameters, "is_rule_due no longer takes cron_schedule")
weekday = {"schedule": {"type": "scheduled", "days": ["MON", "TUE", "WED", "THU", "FRI"], "time_et": "21:00"}}
daily = {"schedule": {"type": "scheduled", "days": list(slot_gate.DAY_CODES), "time_et": "21:00"}}
scan_only = {"schedule": {"type": "none"}}
check(alerts.is_rule_due(weekday, et(2026, 9, 14, 21, 30)), "weekday rule due on Monday 21:30 ET")
check(alerts.is_rule_due(weekday, et(2026, 9, 15, 3, 0)), "weekday rule still due when the run lands 03:00 ET Tuesday (counts as Monday)")
check(alerts.is_rule_due(weekday, et(2026, 9, 19, 2, 0)), "Friday rule still due when the run lands Saturday 02:00 ET")
# Saturday EVENING counts as Friday's slot by design (rule_hour >= 18 rolls back),
# so a weekday rule fires there too; alert_state dedups it since those pairs are
# already active. Unchanged by the gate.
check(alerts.is_rule_due(weekday, et(2026, 9, 19, 21, 0)), "Saturday evening counts as Friday's slot for a weekday rule")
check(alerts.is_rule_due(weekday, et(2026, 9, 19, 12, 0)), "the rollback covers all of Saturday, not just the evening")
check(not alerts.is_rule_due(weekday, et(2026, 9, 13, 21, 0)), "weekday rule not due on Sunday evening")
check(alerts.is_rule_due(daily, et(2026, 9, 13, 21, 0)), "every-day rule due on Sunday evening")
check(not alerts.is_rule_due(scan_only, et(2026, 9, 14, 21, 30)), "scan-only rule never due")

# --- the 2026-09-14 outage: GitHub fired only the EST line and the run started 03:42 ET
outage_now = et(2026, 9, 15, 3, 42)
gate_run, why = decide(outage_now)
check(gate_run and alerts.is_rule_due(daily, outage_now) and alerts.is_rule_due(weekday, outage_now),
      f"2026-09-14 slot: the delayed run now owns the slot and the rules are due ({why})")


# --- worked_runs must look back over the WHOLE window, not one API page.
# An hourly wake-up makes ~24 runs a day while the window is 22 h, so the run
# that actually did the slot can fall off page 1 -- and the gate would then redo
# it (for expert-views, hours of Gemini spend).
import json as _json, types as _types
def fake_api_factory(total_runs, worked_index, per_page_seen):
    """`total_runs` completed runs, newest first, one hour apart; the run at
    `worked_index` is the one whose work job succeeded."""
    base = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    runs = [{"id": i, "run_started_at": (base - timedelta(hours=i)).isoformat().replace("+00:00", "Z")}
            for i in range(total_runs)]
    def fake(path, token):
        if "/runs?" in path or path.endswith("/runs"):
            per_page = int(path.split("per_page=")[1].split("&")[0]) if "per_page=" in path else 30
            page = int(path.split("&page=")[1].split("&")[0]) if "&page=" in path else 1
            per_page_seen.append((per_page, page))
            start = (page - 1) * per_page
            return {"workflow_runs": runs[start:start + per_page]}
        run_id = int(path.split("/runs/")[1].split("/jobs")[0])
        conclusion = "success" if run_id == worked_index else "skipped"
        return {"jobs": [{"name": "build", "conclusion": conclusion},
                         {"name": "gate", "conclusion": "success"}]}
    return fake

real_api = slot_gate._api
seen = []
try:
    slot_gate._api = fake_api_factory(30, 21, seen)      # the satisfying run is 21 h old
    since = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc) - timedelta(hours=46)
    worked = slot_gate.worked_runs("o/r", "expert-views.yml", "build", "t", since)
finally:
    slot_gate._api = real_api
check(len(worked) == 1, f"worked_runs finds the satisfying run 21 pages deep ({len(worked)} found, pages={seen[:3]})")
print("FAILURES:", fails); sys.exit(fails)
