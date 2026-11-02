"""The Cloudflare Worker is the clock for every scheduled job (owner, 2026-11-02).

cloudflare/worker.js is deployed by hand, so a mistake in it would silently stop a job. This runs the
real file under node, tick by tick (every :00 and :30 UTC), and compares what it would dispatch with
an independent model: that one starts from each job's slot and walks FORWARD through its retries,
where the Worker works backward from the tick. Windows: the clock change of 2026-11-01 (end of
daylight time) and 2027-03-14 (start).

W1  the schedule: every tick's dispatches equal the model's (data-refresh / breadth in the active
    period, keep-alive every 4 h, each gated job at its slot and hourly for 8 h after it)
W2  hand-picked ticks (winter and summer), so the model and the Worker cannot be wrong together
W3  the workflows: each gated one takes the `scheduled` input, passes it to its gate, and its work job
    is skipped by a gate that says no; the gate's slots and days equal the Worker's
W4  the gate: a dispatch with scheduled=true reads the history and decides (a done slot is skipped, an
    unreadable history is red); a dispatch by hand still bypasses it
W5  the behaviours a dispatch used to change: no --force on the alerts, the library's min-gap

Offline: no network. Needs node (skipped when it is missing).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

REPO = Path(__file__).resolve().parents[1]
ET, UTC = ZoneInfo("America/New_York"), timezone.utc
fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


WORKER = (REPO / "cloudflare" / "worker.js").read_text()
check(len(re.findall(r"^export ", WORKER, re.M)) == 1 and "export default {" in WORKER,
      "W0: worker.js has the one default export and no other (extra named exports are not Worker handlers)")
check("GH_DISPATCH_TOKEN" in WORKER and "never the data repo" in WORKER, "W0: it keeps the token's scope note")

# --- the independent model ------------------------------------------------------------------
RETRIES = 8
GATED = {                                   # slot (hour, minute) ET, and the slot's weekdays (Monday = 0)
    "news-summary.yml": ([(6, 0)], None),
    "market-read.yml": ([(6, 30), (17, 30)], {0, 1, 2, 3, 4}),
    "market-library.yml": ([(7, 0), (17, 0)], None),
    "quarterly-results.yml": ([(4, 0)], None),
    "expert-views.yml": ([(1, 0)], None),
    "fundamentals.yml": ([(21, 0)], None),
    "daily-alerts.yml": ([(21, 0)], None),
    "weekly-wrapup.yml": ([(21, 0)], {6}),
}


def active(tick):
    """Data-refresh / breadth's active period: Sunday from 22:00, Monday-Thursday except 17:30-21:59,
    Friday until 17:00, never Saturday."""
    t = tick.astimezone(ET)
    m = t.hour * 60 + t.minute
    wd = t.weekday()
    if wd == 6:
        return m >= 22 * 60
    if wd == 5:
        return False
    if wd == 4:
        return m <= 17 * 60
    return m >= 22 * 60 or m <= 17 * 60


def model(start, end):
    """{tick: set of 'workflow' / 'workflow+scheduled'} for every :00/:30 tick in [start, end]."""
    exp = {}
    t = start
    while t <= end:
        s = set()
        if active(t):
            s.add("data-refresh.yml")
            if t.minute == 0:
                s.add("market-breadth.yml")
        if t.minute == 0 and t.hour % 4 == 0:
            s.add("keep-alive.yml")
        exp[t] = s
        t += timedelta(minutes=30)
    d = start.astimezone(ET).date() - timedelta(days=2)
    last = end.astimezone(ET).date() + timedelta(days=1)
    while d <= last:
        for wf, (slots, days) in GATED.items():
            for h, m in slots:
                for fold in (0, 1):          # a slot inside the autumn repeated hour happens twice
                    slot = datetime(d.year, d.month, d.day, h, m, tzinfo=ET, fold=fold)
                    if days is not None and slot.weekday() not in days:
                        continue
                    for k in range(RETRIES + 1):
                        tick = slot.astimezone(UTC) + timedelta(hours=k)
                        if tick in exp:
                            exp[tick].add(wf + "+scheduled")
        d += timedelta(days=1)
    return {t: s for t, s in exp.items() if s}


# --- run the real worker.js under node --------------------------------------------------------
node = shutil.which("node")
if not node:
    print("SKIP node is not installed: the Worker's schedule was not run")
    check(True, "W1: (skipped) no node")
    ticks_by_window = None
else:
    tmp = Path(tempfile.mkdtemp(prefix="worker_"))
    (tmp / "w.mjs").write_text(WORKER + "\nexport { dispatchesFor, GATED_JOBS, RETRY_HOURS, isActive };\n")
    (tmp / "run.mjs").write_text(
        'import { dispatchesFor, GATED_JOBS, RETRY_HOURS } from "./w.mjs";\n'
        "const [start, end] = process.argv.slice(2).map(Number);\n"
        "const out = {};\n"
        "for (let t = start; t <= end; t += 1800000) {\n"
        "  const d = dispatchesFor(new Date(t));\n"
        "  if (d.length) out[new Date(t).toISOString()] = d.map((x) => x.workflow + (x.inputs && x.inputs.scheduled === 'true' ? '+scheduled' : ''));\n"
        "}\n"
        "console.log(JSON.stringify({ ticks: out, jobs: GATED_JOBS, retry: RETRY_HOURS, inputs: dispatchesFor(new Date(Date.UTC(2026, 10, 2, 11, 30))) }));\n")

    def run_node(start, end):
        r = subprocess.run([node, "run.mjs", str(int(start.timestamp() * 1000)), str(int(end.timestamp() * 1000))],
                           cwd=tmp, capture_output=True, text=True, timeout=120)
        if r.returncode:
            print(r.stderr[:500])
        return json.loads(r.stdout)

    windows = [(datetime(2026, 10, 25, 0, 0, tzinfo=UTC), datetime(2026, 11, 15, 0, 0, tzinfo=UTC)),
               (datetime(2027, 3, 7, 0, 0, tzinfo=UTC), datetime(2027, 3, 21, 0, 0, tzinfo=UTC))]
    all_ok, total, worst = True, 0, 0
    first = None
    for lo, hi in windows:
        got = run_node(lo, hi)
        first = first or got
        actual = {datetime.fromisoformat(k.replace("Z", "+00:00")): set(v) for k, v in got["ticks"].items()}
        want = model(lo, hi)
        total += len(want)
        worst = max([worst] + [len(v) for v in actual.values()])
        if actual != want:
            all_ok = False
            diff = [(t.isoformat(), sorted(actual.get(t, set()) ^ want.get(t, set()))) for t in sorted(set(actual) | set(want))
                    if actual.get(t, set()) != want.get(t, set())]
            print("   first differences:", diff[:6])
    check(all_ok, f"W1: all {total} ticks with something due match the model (both clock changes); at most {worst} dispatches on a tick")
    check(worst <= 20, "W1: far under a free Worker's 50 subrequests per invocation")
    check(first["retry"] == RETRIES, f"W1: eight hourly retries ({first['retry']})")

    # --- W2 hand-picked ticks ------------------------------------------------------------------
    def due(when_utc):
        got = run_node(when_utc, when_utc)
        return set(got["ticks"].get(when_utc.strftime("%Y-%m-%dT%H:%M:00.000Z"), []))

    def at(y, mo, d, h, mi, tz):          # a New York or UTC wall time
        return datetime(y, mo, d, h, mi, tzinfo=tz).astimezone(UTC)

    # Winter (EST) and summer (EDT) wall times
    check(due(at(2026, 11, 2, 6, 30, ET)) == {"data-refresh.yml", "market-read.yml+scheduled"},
          "W2: Monday 6:30 ET (winter): data-refresh and the morning market read")
    check(due(at(2026, 11, 2, 6, 0, ET)) == {"data-refresh.yml", "market-breadth.yml", "news-summary.yml+scheduled",
                                             "quarterly-results.yml+scheduled", "expert-views.yml+scheduled"},
          "W2: Monday 6:00 ET: data-refresh, breadth, the news digest's slot, and the retries of the 4:00 results job (2nd) and 1:00 Expert Take (5th)")
    check(due(at(2026, 11, 2, 21, 0, ET)) == {"market-library.yml+scheduled", "fundamentals.yml+scheduled", "daily-alerts.yml+scheduled"},
          "W2: Monday 9 PM ET (inside the quiet gap): the two 9 PM jobs, and the 4th retry of the 5 PM library")
    check(due(at(2026, 11, 1, 21, 0, ET)) == {"market-library.yml+scheduled", "fundamentals.yml+scheduled", "daily-alerts.yml+scheduled",
                                              "weekly-wrapup.yml+scheduled"},
          "W2: Sunday 9 PM ET: the wrap-up joins them")
    check(due(at(2026, 11, 2, 1, 0, ET)) == {"data-refresh.yml", "market-breadth.yml", "expert-views.yml+scheduled", "market-library.yml+scheduled",
                                             "fundamentals.yml+scheduled", "daily-alerts.yml+scheduled", "weekly-wrapup.yml+scheduled"},
          "W2: Monday 1 AM ET: Expert Take's slot, plus Sunday's 9 PM jobs (4th retry) and 5 PM library (8th, the last)")
    check(due(at(2026, 11, 3, 17, 30, ET)) == {"market-read.yml+scheduled"},
          "W2: Tuesday 5:30 PM ET: only the evening read (data-refresh is quiet from 17:30)")
    check(due(at(2026, 11, 3, 17, 0, ET)) == {"data-refresh.yml", "market-breadth.yml", "market-library.yml+scheduled"},
          "W2: Tuesday 5:00 PM ET: the last data-refresh and breadth tick, and the library")
    check(due(at(2026, 11, 7, 6, 30, ET)) == set(), "W2: Saturday 6:30 ET: nothing (no weekday read, no data-refresh)")
    check(due(at(2026, 11, 7, 9, 0, ET)) == {"news-summary.yml+scheduled", "market-library.yml+scheduled", "quarterly-results.yml+scheduled",
                                             "expert-views.yml+scheduled"},
          "W2: Saturday 9:00 ET: only retries of the daily jobs (no data-refresh, no weekday read)")
    check(due(at(2026, 6, 15, 6, 30, ET)) == {"data-refresh.yml", "market-read.yml+scheduled"},
          "W2: Monday 6:30 ET in summer (EDT, 10:30 UTC): the same local time")
    check(due(at(2026, 6, 15, 21, 0, ET)) == {"market-library.yml+scheduled", "fundamentals.yml+scheduled", "daily-alerts.yml+scheduled"},
          "W2: Monday 9 PM ET in summer: the same")
    check("weekly-wrapup.yml+scheduled" in due(at(2026, 11, 2, 2, 0, ET)),
          "W2: Monday 2 AM ET: still Sunday's wrap-up slot (a retry; the gate's days judge the slot)")
    check("weekly-wrapup.yml+scheduled" not in due(at(2026, 11, 2, 6, 0, ET)),
          "W2: ...and no more than 8 hours after it")
    check("market-read.yml+scheduled" not in due(at(2026, 11, 7, 17, 30, ET)),
          "W2: no market read on a Saturday")

    sent = first["inputs"]
    check(all(x.get("inputs") == {"scheduled": "true"} for x in sent if x["workflow"] == "market-read.yml")
          and all("inputs" not in x for x in sent if x["workflow"] == "data-refresh.yml"),
          "W2: a gated job is dispatched with scheduled=true, an ungated one with no inputs")
    shutil.rmtree(tmp, ignore_errors=True)

# --- W3 the workflows ------------------------------------------------------------------------
WF = REPO / ".github" / "workflows"
for wf_name, (slots, days) in GATED.items():
    wf = yaml.safe_load((WF / wf_name).read_text())
    on = wf.get(True) or wf.get("on")
    inp = (on["workflow_dispatch"] or {}).get("inputs", {}).get("scheduled")
    check(inp is not None and inp.get("type") == "boolean" and inp.get("default") is False,
          f"W3: {wf_name} takes a boolean `scheduled` input, false by default")
    gate_steps = [s for j in wf["jobs"].values() for s in j.get("steps", []) if s.get("uses") == "./.github/actions/slot-gate"]
    gate = gate_steps[0]["with"] if gate_steps else {}
    check(gate.get("scheduled") == "${{ github.event.inputs.scheduled }}", f"W3: {wf_name}'s gate is told whether it is a Worker dispatch")
    check({int(x) for x in gate["slots"].split(",")} == {h for h, m in slots},
          f"W3: {wf_name}: the gate's slot hours {gate['slots']} are the Worker's ({sorted({h for h, m in slots})})")
    want_days = "" if days is None else ",".join(["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][d] for d in sorted(days))
    check(gate.get("days", "") == want_days, f"W3: {wf_name}: the gate's days ({gate.get('days', '')!r}) are the Worker's ({want_days!r})")
    ifs = [j.get("if", "") for n, j in wf["jobs"].items() if n != "gate" and "workflow_dispatch" in str(j.get("if", ""))]
    check(ifs and all("github.event.inputs.scheduled != 'true'" in i for i in ifs),
          f"W3: {wf_name}: only a dispatch BY HAND skips the gate ({ifs})")
for ungated in ("data-refresh.yml", "market-breadth.yml", "keep-alive.yml"):
    wf = yaml.safe_load((WF / ungated).read_text())
    on = wf.get(True) or wf.get("on")
    check("workflow_dispatch" in on, f"W3: {ungated} can be dispatched")
for name in set(re.findall(r'"([a-z-]+\.yml)"', WORKER)):
    check((WF / name).exists(), f"W3: the workflow {name} the Worker names exists")

# --- W4 the gate ---------------------------------------------------------------------------------
GATE = REPO / ".github" / "actions" / "slot-gate"
base = dict(os.environ, SLOT_HOURS_ET=",".join(str(h) for h in range(24)), GRACE_HOURS="22", DAYS="", WINDOW="",
            WORK_JOB="check", WORKFLOW_FILE="daily-alerts.yml", REPO="o/r", RUN_ID="1", GH_TOKEN="t")


def gate_run(worked_expr, **env):
    probe = ("import sys; sys.path.insert(0, '.'); import slot_gate; from datetime import datetime\n"
             f"def worked(*a, **k):\n    return {worked_expr}\n"
             "slot_gate.worked_runs = worked\nslot_gate.main()\n")
    r = subprocess.run([sys.executable, "-c", probe], cwd=GATE, env=dict(base, **env), capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr


sched = {"GITHUB_EVENT_NAME": "workflow_dispatch", "SCHEDULED_DISPATCH": "true"}
rc, out, err = gate_run("[]", **sched)
check(rc == 0 and out == "run=true" and "not done yet" in err, f"W4: a Worker dispatch with nothing done reads the history and runs ({out!r})")
rc, out, err = gate_run("[datetime.now(slot_gate.ET)]", **sched)
check(rc == 0 and out == "run=false" and "already done" in err,
      f"W4: ...and is skipped when the slot's work already succeeded (a retry costs nothing) ({out!r})")
rc, out, err = gate_run("(_ for _ in ()).throw(slot_gate.GateUnsure('unreadable'))", **sched)
check(rc == 1 and "::error::" in err and "run=" not in out, "W4: an unreadable history is a red gate for a Worker dispatch (the next hourly retry asks again)")
rc, out, err = gate_run("(_ for _ in ()).throw(slot_gate.GateUnsure('unreadable'))", GITHUB_EVENT_NAME="workflow_dispatch", SCHEDULED_DISPATCH="false")
check(rc == 0 and out == "run=true", "W4: a dispatch by hand (scheduled=false) never reads the history and always runs")
rc, out, err = gate_run("(_ for _ in ()).throw(slot_gate.GateUnsure('unreadable'))", GITHUB_EVENT_NAME="workflow_dispatch")
check(rc == 0 and out == "run=true", "W4: ...nor does one with no scheduled input at all")
check("SCHEDULED_DISPATCH: ${{ inputs.scheduled }}" in (GATE / "action.yml").read_text(), "W4: the action passes the input to the gate script")

# --- W5 what a dispatch used to change --------------------------------------------------------------
alerts = (WF / "daily-alerts.yml").read_text()
check('"${{ github.event_name }}" = "workflow_dispatch" ] && [ "${{ github.event.inputs.scheduled }}" != "true" ]' in alerts
      and "alert_check.py --force" in alerts,
      "W5: --force (every rule posts again) only for a dispatch by hand; a Worker dispatch is the normal edge-triggered check")
check("github.event.inputs.scheduled != 'true') || needs.gate.outputs.due == 'true'" in alerts,
      "W5: a Worker dispatch only starts the alert check when a rule is due, like a schedule")
lib = (WF / "market-library.yml").read_text()
check("github.event_name == 'schedule' || github.event.inputs.scheduled == 'true'" in lib and "--min-gap 50" in lib,
      "W5: the library's min-gap applies to a Worker dispatch too")
exp = (WF / "expert-views.yml").read_text()
check("SCHEDULED_DISPATCH: ${{ github.event.inputs.scheduled }}" in exp and "SCHEDULED_DISPATCH" in (REPO / "refresh_expert_views.py").read_text(),
      "W5: Expert Take's resume reads the flag (checks/test_expert_views_resume.py runs it)")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
