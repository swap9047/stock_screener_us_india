"""The sidebar's refresh schedule (refresh_schedule.py) vs the breadth gate's real decisions (2026-10-23).

The window lives in three places that cannot share code: the Cloudflare Worker, market-breadth.yml's
gate, and refresh_schedule.py, which the app's stale-data warning reads. This holds the app's copy to
the gate's, hour by hour over 14 months across both daylight-saving changes, with the gate's
parameters read from the workflow file itself.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".github/actions/slot-gate"))

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml

import refresh_schedule as rs
import slot_gate

fails = 0
def check(ok, label):
    global fails; fails += not ok; print("PASS" if ok else "FAIL", label)

ET = ZoneInfo("America/New_York")
wf = yaml.safe_load((REPO / ".github/workflows/market-breadth.yml").read_text())
w = next(s for s in wf["jobs"]["gate"]["steps"] if s.get("id") == "slot")["with"]
slots = [int(h) for h in str(w["slots"]).split(",")]
window = slot_gate.parse_window(w["window"])

mismatch = []
t = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
while t < datetime(2027, 12, 20, tzinfo=timezone.utc):
    et = t.astimezone(ET)
    gate_says = slot_gate.decide(et, slots, 1, [], [], window)[0]
    if gate_says != rs.in_refresh_window(t):
        mismatch.append(f"{et:%a %Y-%m-%d %H:%M} ET gate={gate_says}")
    t += timedelta(hours=1)
check(not mismatch, f"refresh_schedule agrees with the breadth gate every hour for 14 months, both DST changes ({mismatch[:3]})")

et = lambda *a: datetime(*a, tzinfo=ET)
# 2026-10-23 is a Friday, 10-24 Saturday, 10-25 Sunday, 10-26 Monday, 10-21 Wednesday.
check(rs.refresh_hours_since(et(2026, 10, 23, 17, 0), et(2026, 10, 25, 21, 59)) < 1.01,
      "a Friday 5 PM snapshot is not overdue on Sunday evening: under an hour of schedule has passed")
check(1 < rs.refresh_hours_since(et(2026, 10, 23, 17, 0), et(2026, 10, 25, 23, 30)) < 3,
      "...and Sunday 11:30 PM counts only the hour and a half since the window reopened")
check(rs.refresh_hours_since(et(2026, 10, 23, 17, 0), et(2026, 10, 26, 4, 0)) > 6,
      "...but by Monday 4 AM, with no refresh since Friday, it is overdue")
check(rs.refresh_hours_since(et(2026, 10, 21, 16, 0), et(2026, 10, 21, 21, 59)) == 2.0,
      "the evening gap counts only 16:00-18:00 (the 17:00 tick's hour is in the schedule)")
check(rs.refresh_hours_since(et(2026, 10, 21, 8, 0), et(2026, 10, 21, 15, 0)) == 7.0,
      "seven weekday daytime hours without a refresh are seven scheduled hours")
check(rs.refresh_hours_since(et(2026, 10, 21, 8, 0), et(2026, 10, 1, 8, 0)) == 0.0, "a clock running backwards counts nothing")
check(rs.refresh_hours_since(et(2026, 9, 1, 8, 0), et(2026, 10, 21, 8, 0)) > 1000,
      "a weeks-old snapshot is plainly stale (raw age, no walk)")
check(not rs.in_refresh_window(et(2026, 10, 24, 12, 0)) and rs.in_refresh_window(et(2026, 10, 25, 22, 0))
      and not rs.in_refresh_window(et(2026, 10, 21, 19, 30)) and rs.in_refresh_window(et(2026, 10, 21, 17, 45)),
      "spot checks: Saturday no, Sunday 22:00 yes, 19:30 no, the 17:00 hour yes")

app_src = (REPO / "app.py").read_text()
check("refresh_hours_since(snapshot_stamp(as_of), _now_utc)" in app_src and "The hourly refresh may have stopped" not in app_src,
      "the sidebar warns on scheduled hours, and no longer blames 'the hourly refresh'")

print("FAILURES:", fails); sys.exit(fails)
