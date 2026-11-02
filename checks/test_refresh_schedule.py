"""The sidebar's refresh schedule (refresh_schedule.py) vs the window the data jobs run in (2026-10-23).

The window lives in two places that cannot share code: the Cloudflare Worker (cloudflare/worker.js, the only
clock since 2026-11-02) and refresh_schedule.py, which the app's stale-data warning reads. This holds the
app's copy to the slot gate's decisions with the window as parameters (the breadth gate's until 2026-11-02),
hour by hour over 14 months across both daylight-saving changes, and then to the Worker's own `isActive`,
run under node (skipped without it) on every :00 tick.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".github/actions/slot-gate"))

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import refresh_schedule as rs
import slot_gate

fails = 0
def check(ok, label):
    global fails; fails += not ok; print("PASS" if ok else "FAIL", label)

ET = ZoneInfo("America/New_York")
# The window as the gate took it (market-breadth.yml's gate until 2026-11-02): hourly slots but 18-21, SUN:22-FRI:17.
slots = [h for h in range(24) if h not in (18, 19, 20, 21)]
window = slot_gate.parse_window("SUN:22-FRI:17")

mismatch = []
t = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
while t < datetime(2027, 12, 20, tzinfo=timezone.utc):
    et = t.astimezone(ET)
    gate_says = slot_gate.decide(et, slots, 1, [], [], window)[0]
    if gate_says != rs.in_refresh_window(t):
        mismatch.append(f"{et:%a %Y-%m-%d %H:%M} ET gate={gate_says}")
    t += timedelta(hours=1)
check(not mismatch, f"refresh_schedule agrees with the gate's decisions for that window every hour for 14 months, both DST changes ({mismatch[:3]})")

# And with the Worker itself: its isActive, run under node, on every :00 tick (the :30 ticks of the 17:00 hour
# are the one place a minute-level rule and an hour-level one differ, so only :00 is compared).
import json, shutil, subprocess, tempfile
node = shutil.which("node")
if node:
    tmp = Path(tempfile.mkdtemp(prefix="worker_rs_"))
    (tmp / "w.mjs").write_text((REPO / "cloudflare/worker.js").read_text() + "\nexport { isActive };\n")
    (tmp / "r.mjs").write_text('import { isActive } from "./w.mjs"; const [a, b] = process.argv.slice(2).map(Number); const out = [];'
                              ' for (let t = a; t <= b; t += 3600000) if (isActive(new Date(t))) out.push(t); console.log(JSON.stringify(out));')
    lo, hi = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc), datetime(2027, 12, 20, tzinfo=timezone.utc)
    r = subprocess.run([node, "r.mjs", str(int(lo.timestamp() * 1000)), str(int(hi.timestamp() * 1000))], cwd=tmp, capture_output=True, text=True, timeout=120)
    worker_on = {datetime.fromtimestamp(ms / 1000, tz=timezone.utc) for ms in json.loads(r.stdout)}
    app_on, t = set(), lo
    while t <= hi:
        if rs.in_refresh_window(t):
            app_on.add(t)
        t += timedelta(hours=1)
    diff = sorted(worker_on ^ app_on)
    check(not diff, f"refresh_schedule agrees with the Worker's own window on every :00 tick for 14 months ({[d.astimezone(ET).strftime('%a %m-%d %H:%M') for d in diff[:3]]})")
    shutil.rmtree(tmp, ignore_errors=True)
else:
    print("SKIP node is not installed: the Worker's window was not compared")

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
