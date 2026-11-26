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
W6  the Worker is the only clock
W7  a tick's requests: a gated job with a run in progress or queued is not dispatched (it would cancel a
    queued run by hand), a failed listing dispatches anyway, and the optional Discord notices (a failed
    dispatch; the token's expiry, once a day)

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
    "news-summary.yml": ([(16, 0)], {6, 2}),
    "market-read.yml": ([(6, 30), (17, 30)], {0, 1, 2, 3, 4}),
    "market-library.yml": ([(7, 0), (17, 0)], None),
    "quarterly-results.yml": ([(4, 0)], None),
    "expert-views.yml": ([(2, 0)], None),
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
                    if slot.astimezone(UTC).astimezone(ET).hour != h:
                        # The spring-forward Sunday has no 02:00 in New York: the slot does not exist that night, and
                        # the Worker (which looks for the wall-clock hour) sends nothing. That is Expert Take's 2 AM
                        # slot on a top-up night (its full runs are Wednesday and Saturday), so one Sunday a year
                        # skips a top-up and Monday's slot works as usual.
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
    # On CI a missing node is a broken runner (checks.yml installs it), not a pass: the SKIP used to
    # count as one, so the Worker could stop being checked on every push without anyone seeing it.
    check(not os.environ.get("GITHUB_ACTIONS"), "W1: node is installed (required on CI; skipped locally)")
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
    check(due(at(2026, 11, 2, 6, 0, ET)) == {"data-refresh.yml", "market-breadth.yml",
                                             "quarterly-results.yml+scheduled", "expert-views.yml+scheduled"},
          "W2: Monday 6:00 ET: data-refresh, breadth, and the retries of the 4:00 results job (2nd) and 2:00 Expert Take (4th); no news digest (Sunday + Wednesday 4 PM only)")
    check(due(at(2026, 11, 2, 21, 0, ET)) == {"market-library.yml+scheduled", "fundamentals.yml+scheduled", "daily-alerts.yml+scheduled"},
          "W2: Monday 9 PM ET (inside the quiet gap): the two 9 PM jobs, and the 4th retry of the 5 PM library")
    check(due(at(2026, 11, 1, 21, 0, ET)) == {"market-library.yml+scheduled", "fundamentals.yml+scheduled", "daily-alerts.yml+scheduled",
                                              "weekly-wrapup.yml+scheduled", "news-summary.yml+scheduled"},
          "W2: Sunday 9 PM ET: the wrap-up joins them, and the news digest's 5th retry (its slot is Sunday 4 PM)")
    check(due(at(2026, 11, 1, 16, 0, ET)) == {"news-summary.yml+scheduled"},
          "W2: Sunday 4 PM ET: the news digest's slot (Sunday + Wednesday only)")
    check(due(at(2026, 11, 4, 16, 0, ET)) == {"data-refresh.yml", "market-breadth.yml", "news-summary.yml+scheduled"},
          "W2: Wednesday 4 PM ET: the news digest's slot, with the last data-refresh / breadth ticks of the day's active period")
    check("news-summary.yml+scheduled" not in due(at(2026, 11, 5, 16, 0, ET)) and "news-summary.yml+scheduled" not in due(at(2026, 11, 3, 16, 0, ET)),
          "W2: Tuesday and Thursday 4 PM ET: no news digest")
    check("news-summary.yml+scheduled" in due(at(2026, 11, 5, 0, 0, ET)) and "news-summary.yml+scheduled" not in due(at(2026, 11, 5, 0, 30, ET)),
          "W2: Wednesday's retries run to midnight (the 8th at 00:00 Thursday) and no further")
    check(due(at(2026, 11, 2, 2, 0, ET)) == {"data-refresh.yml", "market-breadth.yml", "expert-views.yml+scheduled",
                                             "fundamentals.yml+scheduled", "daily-alerts.yml+scheduled", "weekly-wrapup.yml+scheduled"},
          "W2: Monday 2 AM ET: Expert Take's slot, plus Sunday's 9 PM jobs (5th retry); the 5 PM library's last retry was at 1 AM")
    check(due(at(2026, 11, 2, 1, 0, ET)) == {"data-refresh.yml", "market-breadth.yml", "market-library.yml+scheduled",
                                             "fundamentals.yml+scheduled", "daily-alerts.yml+scheduled", "weekly-wrapup.yml+scheduled"},
          "W2: Monday 1 AM ET: Sunday's 9 PM jobs (4th retry) and the 5 PM library (8th, the last); Expert Take is not yet due")
    check("expert-views.yml+scheduled" not in due(at(2026, 11, 2, 1, 0, ET)),
          "W2: Monday 1 AM ET: Expert Take's slot has moved to 2 AM, so nothing dispatches it at 1")
    check("expert-views.yml+scheduled" not in due(at(2027, 3, 14, 7, 0, UTC)) and "expert-views.yml+scheduled" not in due(at(2027, 3, 14, 6, 0, UTC)),
          "W2: the spring-forward Sunday has no 02:00 ET, so no Expert Take dispatch that night (a top-up night; Monday works as usual)")
    check(due(at(2026, 11, 3, 17, 30, ET)) == {"market-read.yml+scheduled"},
          "W2: Tuesday 5:30 PM ET: only the evening read (data-refresh is quiet from 17:30)")
    check(due(at(2026, 11, 3, 17, 0, ET)) == {"data-refresh.yml", "market-breadth.yml", "market-library.yml+scheduled"},
          "W2: Tuesday 5:00 PM ET: the last data-refresh and breadth tick, and the library")
    check(due(at(2026, 11, 7, 6, 30, ET)) == set(), "W2: Saturday 6:30 ET: nothing (no weekday read, no data-refresh)")
    check(due(at(2026, 11, 7, 9, 0, ET)) == {"market-library.yml+scheduled", "quarterly-results.yml+scheduled",
                                             "expert-views.yml+scheduled"},
          "W2: Saturday 9:00 ET: only retries of the daily jobs (no data-refresh, no weekday read, no news digest)")
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
    # compared as sets: "SUN,WED" and "WED,SUN" are the same days
    check(set(filter(None, gate.get("days", "").split(","))) == set(filter(None, want_days.split(","))),
          f"W3: {wf_name}: the gate's days ({gate.get('days', '')!r}) are the Worker's ({want_days!r})")
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
      "W5: --force (every rule checked whatever its day) only for a dispatch by hand; a Worker dispatch is the normal check of the rules due")
check("github.event.inputs.scheduled != 'true') || needs.gate.outputs.due == 'true'" in alerts,
      "W5: a Worker dispatch only starts the alert check when a rule is due, like a schedule")
lib = (WF / "market-library.yml").read_text()
check("github.event_name == 'schedule' || github.event.inputs.scheduled == 'true'" in lib and "--min-gap 50" in lib,
      "W5: the library's min-gap applies to a Worker dispatch too")
exp = (WF / "expert-views.yml").read_text()
check("SCHEDULED_DISPATCH: ${{ github.event.inputs.scheduled }}" in exp and "SCHEDULED_DISPATCH" in (REPO / "refresh_expert_views.py").read_text(),
      "W5: Expert Take's resume reads the flag (checks/test_expert_views_resume.py runs it)")

# --- W6 the Worker is the only clock (phase 2, owner 2026-11-02) -------------------------------------
# Every workflow but checks.yml (it runs on a push) is started by the Worker and by nothing else: no GitHub
# `schedule:` trigger, and every one is named in worker.js, so a job cannot be left without a clock.
every = sorted(p.name for p in WF.glob("*.yml") if p.name != "checks.yml")
for name in every:
    wf = yaml.safe_load((WF / name).read_text())
    on = wf.get(True) or wf.get("on")
    check(isinstance(on, dict) and "schedule" not in on and "workflow_dispatch" in on,
          f"W6: {name} has no GitHub schedule and can be dispatched")
    check(f'"{name}"' in WORKER, "W6: ...and cloudflare/worker.js names it (" + name + ")")
for name in ("data-refresh.yml", "market-breadth.yml"):
    wf = yaml.safe_load((WF / name).read_text())
    check("gate" not in wf["jobs"] and all("needs" not in j for j in wf["jobs"].values()),
          f"W6: {name} has no gate job (nothing but a dispatch reaches it)")
check(sorted(every) == sorted(["daily-alerts.yml", "data-refresh.yml", "expert-views.yml", "fundamentals.yml", "keep-alive.yml",
                               "market-breadth.yml", "market-library.yml", "market-read.yml", "news-summary.yml",
                               "quarterly-results.yml", "weekly-wrapup.yml"]),
      f"W6: the eleven scheduled workflows are exactly the Worker's ({every})")

# --- W7 what a tick sends (2026-11-22): a busy gated job is not dispatched, Discord notices -----------
# The real scheduled() handler under node with fetch replaced: each scenario lists canned replies by URL
# (first match wins; anything else is a 204 dispatch, an empty runs listing or a 204 Discord post), and the
# harness prints every request it made and whether the handler threw.
HARNESS = r"""
import worker from "./w.mjs";
const sc = JSON.parse(process.argv[2]);
const calls = [];
globalThis.fetch = async (url, init = {}) => {
  const method = init.method || "GET";
  calls.push({ url, method, body: init.body || null });
  for (const r of sc.replies || []) {
    if (url.includes(r.match) && (!r.method || r.method === method)) {
      if (r.throw) throw new Error("network down");
      return new Response(r.body === undefined ? null : JSON.stringify(r.body), { status: r.status || 200, headers: r.headers || {} });
    }
  }
  if (url.includes("/runs?")) return new Response(JSON.stringify({ total_count: 0, workflow_runs: [] }), { status: 200, headers: sc.headers || {} });
  return new Response(null, { status: 204, headers: sc.headers || {} });
};
console.log = () => {}; console.warn = () => {}; console.error = () => {};
let threw = false;
try { await worker.scheduled({ scheduledTime: sc.time }, sc.env || { GH_DISPATCH_TOKEN: "t" }, {}); } catch (e) { threw = true; }
process.stdout.write(JSON.stringify({ calls, threw }));
"""

if node:
    tmp7 = Path(tempfile.mkdtemp(prefix="worker7_"))
    (tmp7 / "w.mjs").write_text(WORKER)
    (tmp7 / "h.mjs").write_text(HARNESS)

    def tick(when_utc, replies=(), env=None, headers=None):
        sc = {"time": int(when_utc.timestamp() * 1000), "replies": list(replies),
              "env": env or {"GH_DISPATCH_TOKEN": "t"}, "headers": headers or {}}
        r = subprocess.run([node, "h.mjs", json.dumps(sc)], cwd=tmp7, capture_output=True, text=True, timeout=60)
        if r.returncode:
            print(r.stderr[:500])
        out = json.loads(r.stdout)
        posts = {c["url"].split("/workflows/")[1].split("/")[0] for c in out["calls"]
                 if c["method"] == "POST" and c["url"].endswith("/dispatches")}
        lists = [c["url"] for c in out["calls"] if "/runs?" in c["url"]]
        discord = [json.loads(c["body"])["content"] for c in out["calls"] if c["url"].startswith("https://discord.example")]
        return posts, lists, discord, out["threw"]

    read_tick = datetime(2026, 11, 2, 6, 30, tzinfo=ET).astimezone(UTC)   # data-refresh + market-read (gated)
    posts, lists, discord, threw = tick(read_tick)
    check(posts == {"data-refresh.yml", "market-read.yml"} and not threw,
          f"W7: nothing running -> the gated job is dispatched as before ({sorted(posts)})")
    check(len(lists) == 2 and all("market-read.yml/runs?status=" in u for u in lists)
          and {u.split("status=")[1].split("&")[0] for u in lists} == {"in_progress", "queued"},
          "W7: ...after asking for its in-progress and queued runs; the ungated data-refresh is never asked about")
    busy = {"match": "market-read.yml/runs?status=in_progress", "body": {"total_count": 1, "workflow_runs": [{"id": 1}]}}
    posts, lists, discord, threw = tick(read_tick, [busy])
    check(posts == {"data-refresh.yml"} and not threw,
          "W7: a run in progress -> the gated job is NOT dispatched (it would cancel a queued run by hand); data-refresh still is")
    queued = {"match": "market-read.yml/runs?status=queued", "body": {"total_count": 1, "workflow_runs": [{"id": 2}]}}
    posts, lists, discord, threw = tick(read_tick, [queued])
    check(posts == {"data-refresh.yml"}, "W7: a queued run -> not dispatched either")
    posts, lists, discord, threw = tick(read_tick, [{"match": "/runs?", "status": 500, "body": {"message": "x"}}])
    check(posts == {"data-refresh.yml", "market-read.yml"} and not threw,
          "W7: a listing that fails (HTTP 500) -> dispatched anyway (fail open)")
    posts, lists, discord, threw = tick(read_tick, [{"match": "/runs?", "throw": True}])
    check(posts == {"data-refresh.yml", "market-read.yml"} and not threw,
          "W7: a listing that throws -> dispatched anyway")

    hook = {"GH_DISPATCH_TOKEN": "t", "DISCORD_WEBHOOK_URL": "https://discord.example/hook"}
    bad = {"match": "market-read.yml/dispatches", "method": "POST", "status": 422, "body": {"message": "bad input"}}
    posts, lists, discord, threw = tick(read_tick, [bad], env=hook)
    check(threw and len(discord) == 1 and "market-read.yml (HTTP 422)" in discord[0],
          f"W7: a failed dispatch -> one Discord notice naming the workflow, and the tick still throws ({discord})")
    posts, lists, discord, threw = tick(read_tick, [bad])
    check(threw and not discord, "W7: ...no Discord binding -> no notice, nothing else changes")

    midnight = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)          # keep-alive is dispatched on every 00:00 UTC tick
    soon = {"github-authentication-token-expiration": "2026-11-08 12:00:00 UTC"}
    posts, lists, discord, threw = tick(midnight, env=hook, headers=soon)
    check("keep-alive.yml" in posts and len(discord) == 1 and "2026-11-08" in discord[0] and "5 day" in discord[0],
          f"W7: a token with 5 days left -> one expiry warning on the 00:00 UTC tick ({discord})")
    posts, lists, discord, threw = tick(midnight, env=hook, headers={"github-authentication-token-expiration": "2026-11-10 09:00:00 -0800"})
    check(len(discord) == 1 and "2026-11-10" in discord[0], "W7: ...an offset expiry ('-0800') parses too")
    posts, lists, discord, threw = tick(midnight, env=hook, headers={"github-authentication-token-expiration": "2027-01-30 00:00:00 UTC"})
    check(not discord, "W7: 88 days left -> no warning")
    posts, lists, discord, threw = tick(read_tick, env=hook, headers=soon)
    check(not discord, "W7: a short expiry on any other tick -> no warning (once a day)")
    posts, lists, discord, threw = tick(midnight, headers=soon)
    check(not discord and not threw, "W7: no Discord binding -> the expiry is a console line only")
    shutil.rmtree(tmp7, ignore_errors=True)
else:
    check(True, "W7: (skipped) no node")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
