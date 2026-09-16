"""Decide whether this run should do the work for its schedule slot.

WHY this exists: every time-gated workflow used to carry two cron lines, one per
DST season, and a gate that rejected the line belonging to the other season.
That only works if GitHub fires the right line. On 2026-09-14's 9:15 PM ET slot
it fired only the EST line, the gate rejected it, and the day's alerts never
ran. GitHub's scheduler is best-effort (observed 4-5 h late, and it drops lines),
so no single cron can be load-bearing.

Instead every workflow wakes hourly and this gate answers one question: has the
most recent slot already been done? Slots are wall-clock hours in New York, so
DST needs no arithmetic. "Done" is read from the GitHub API: an earlier run of
the same workflow, started at or after the slot, whose WORK job actually
succeeded. The work job matters -- a run whose gate said "no" also reports
success, and counting those would skip the slot forever.

Reads inputs from the environment (see action.yml) and prints `run=true|false`
on stdout for $GITHUB_OUTPUT; the reasoning goes to stderr.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DAY_CODES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
API = "https://api.github.com"
PER_PAGE = 50
MAX_PAGES = 6


def latest_slot(now_et, slot_hours):
    """The most recent slot start at or before `now_et`, as an ET datetime."""
    starts = []
    for back in (0, 1):
        day = (now_et - timedelta(days=back)).date()
        for hour in slot_hours:
            start = datetime(day.year, day.month, day.day, int(hour), tzinfo=ET)
            if start <= now_et:
                starts.append(start)
    return max(starts) if starts else None


def decide(now_et, slot_hours, grace_hours, days, worked_at):
    """(run, reason). `worked_at` holds the start times of runs that did work."""
    slot = latest_slot(now_et, slot_hours)
    if slot is None:
        return False, "no slot has started yet"
    age = now_et - slot
    if age >= timedelta(hours=grace_hours):
        return False, f"slot {slot:%a %Y-%m-%d %H:%M} ET is {age.total_seconds() / 3600:.1f}h old (stale after {grace_hours}h)"
    if days and DAY_CODES[slot.weekday()] not in days:
        return False, f"slot {slot:%a %Y-%m-%d %H:%M} ET is not on {','.join(days)}"
    for started in worked_at:
        if started.astimezone(ET) >= slot:
            return False, f"slot {slot:%a %Y-%m-%d %H:%M} ET already done by a run at {started.astimezone(ET):%H:%M} ET"
    return True, f"slot {slot:%a %Y-%m-%d %H:%M} ET, {age.total_seconds() / 3600:.1f}h ago, not done yet"


def _api(path, token):
    req = urllib.request.Request(f"{API}{path}", headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def worked_runs(repo, workflow, work_job, token, since, exclude_run_id=None):
    """Start times of this workflow's runs since `since` whose work job succeeded.

    Never raises: on an API failure it returns [] with a note, so the gate errs
    toward running. A duplicate run is cheap; a silently skipped slot is not.
    """
    # Page until the runs are older than `since`, rather than trusting one page:
    # an hourly wake-up makes ~24 runs a day while the window can be 22 h, so the
    # run that actually did the slot falls off a single 20-run page and the gate
    # would redo the work (for expert-views, hours of Gemini spend). MAX_PAGES
    # bounds it at ~300 runs, far beyond any window this gate uses.
    candidates = []
    try:
        for page in range(1, MAX_PAGES + 1):
            batch = (_api(f"/repos/{repo}/actions/workflows/{workflow}/runs"
                          f"?per_page={PER_PAGE}&status=completed&page={page}", token)
                     .get("workflow_runs") or [])
            candidates.extend(batch)
            oldest = batch[-1].get("run_started_at") or batch[-1].get("created_at") if batch else None
            if len(batch) < PER_PAGE or (oldest and datetime.fromisoformat(oldest.replace("Z", "+00:00")) < since):
                break
    except (urllib.error.URLError, ValueError, KeyError) as e:
        print(f"could not list runs ({e}) -- assuming the slot is not done", file=sys.stderr)
        return []
    out = []
    for run in candidates:
        if str(run.get("id")) == str(exclude_run_id):
            continue
        started = run.get("run_started_at") or run.get("created_at")
        if not started:
            continue
        started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if started_dt < since:
            continue
        try:
            jobs = _api(f"/repos/{repo}/actions/runs/{run['id']}/jobs", token)
        except (urllib.error.URLError, ValueError, KeyError) as e:
            print(f"could not read jobs of run {run.get('id')} ({e}) -- ignoring it", file=sys.stderr)
            continue
        if any(j.get("name") == work_job and j.get("conclusion") == "success" for j in jobs.get("jobs") or []):
            out.append(started_dt)
    return out


def main():
    slot_hours = [int(h) for h in os.environ["SLOT_HOURS_ET"].replace(" ", "").split(",") if h]
    grace_hours = float(os.environ["GRACE_HOURS"])
    days = [d.strip().upper() for d in os.environ.get("DAYS", "").split(",") if d.strip()]
    now_et = datetime.now(ET)
    # Look back far enough to cover the window, whichever slot we land on.
    since = (now_et - timedelta(hours=grace_hours + 24)).astimezone(ZoneInfo("UTC"))
    worked = worked_runs(os.environ["REPO"], os.environ["WORKFLOW_FILE"], os.environ["WORK_JOB"],
                         os.environ["GH_TOKEN"], since, os.environ.get("RUN_ID"))
    run, reason = decide(now_et, slot_hours, grace_hours, days, worked)
    print(f"now {now_et:%a %Y-%m-%d %H:%M} ET | {reason}", file=sys.stderr)
    print(f"run={'true' if run else 'false'}")


if __name__ == "__main__":
    main()
