"""Discord delivery, nightly commits and the sync state (review of 2026-11-22).

O1 News digest: a send that delivers part 0 and fails part 1 marks the slot posted and keeps part 1; the
   slot's next run resends only part 1 (no second digest) and goes green. Nothing delivered: not posted,
   red, and the retry posts the whole digest once.
O2 _post_discord retries a 5xx and a connection error (2 retries, growing wait), never a read timeout or a
   4xx; a 429 still waits Discord's retry_after.
O3 The weekly wrap-up exits 1 when NO message arrived (the state not advanced) and stays green on a partial
   send; the alert check exits 1 when every message failed, stays green on a partial one (only the failed
   rule rolled back).
O4 merge_keyed.py, the commit-time three-way merge of a {ticker: view} store: decision table, and the real
   commit-data bash end to end in a temp git fixture -- an entry the app wrote while the job ran survives,
   the job's own entries and prunes land.
O5 github_sync: concurrent sync-state updates from two threads lose nothing, and no fixed ".tmp" path is
   used for the state or a pulled file.
O6 alerts._format_rule_message (no caller) is gone.

Offline: invented tickers (ACME, ZED.NS, ZIN1.NS), Discord and GitHub replaced, git on a temp directory.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import requests
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import alerts

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


alerts.time.sleep = lambda s: None     # the batch's pacing and the retry waits


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def fake_post(plan):
    """requests.post replaced: each call takes the next item of `plan` (a status code or an exception)."""
    calls = []

    def post(url, json=None, timeout=None):
        calls.append(json["content"])
        item = plan.pop(0) if plan else 204
        if isinstance(item, BaseException):
            raise item
        return Resp(item, {"retry_after": 0.01} if item == 429 else {"message": "x"})
    return post, calls


# --- O1 the news digest ---------------------------------------------------------------------------
import news_check as nc

state = {"file": None}
sent = []
deliver = {"fail": set()}     # contents (by substring) that do not arrive


def stub_batch(hook, msgs, stop_on_failure=True, failed=None):
    sent.append(list(msgs))
    bad = [i for i, m in enumerate(msgs) if any(s in m for s in deliver["fail"])]
    if failed is not None:
        failed.extend(bad)
    return (not bad), ("500: x" if bad else "")


def digest(as_of="2026-11-22"):
    tickers = {"ACME": {"status": "quiet"}, "ZED.NS": {"status": "quiet"}}
    counts = {"material": 0, "quiet": 2, "degraded": 0, "failed": 0}
    return {"as_of": as_of, "totals": {**counts, "searched": 2},
            "markets": {"us_invested": {"summary": "* us digest", "counts": counts, "tickers": {"ACME": tickers["ACME"]}},
                        "india_invested": {"summary": "* india digest", "counts": counts, "tickers": {"ZED.NS": tickers["ZED.NS"]}}}}


nc.get_gemini_api_key = lambda: "k"
nc.load_watchlists = lambda: {"us_invested": ["ACME"], "india_invested": ["ZED.NS"]}
nc.load_news_summary = lambda: state["file"]
nc.save_news_summary = lambda d: state.__setitem__("file", json.loads(json.dumps(d)))
nc.load_discord_webhook = lambda: "https://discord.example/hook"
nc.send_discord_batch = stub_batch
builds = []


def build(w, k, reuse_failed_run=True):
    from news_summary import reusable_outcomes
    from datetime import date
    builds.append(set(reusable_outcomes(state["file"], date(2026, 11, 22))))
    return digest()


nc.build_news_summary = build
for k in ("REFRESH_MARKETS", "REFRESH_LIMIT"):
    os.environ.pop(k, None)


def run_news():
    sent.clear()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            nc.main()
        return 0, [m for b in sent for m in b]
    except SystemExit as e:
        return e.code, [m for b in sent for m in b]


deliver["fail"] = {"india digest"}
code, msgs = run_news()
f = state["file"]
check(code == 1 and len(msgs) == 2 and f["posted"] and f["retry_pending"]
      and len(f["unsent_messages"]) == 1 and "india digest" in f["unsent_messages"][0],
      f"O1: part 0 delivered, part 1 failed -> posted, the failed part kept, red for a retry (exit {code})")
deliver["fail"] = set()
code, msgs = run_news()
f = state["file"]
check(code == 0 and len(msgs) == 1 and "india digest" in msgs[0] and not any("us digest" in m for m in msgs),
      f"O1: the retry resends ONLY the undelivered part -- no second digest ({len(msgs)} message(s))")
check(f["posted"] and not f["unsent_messages"] and not f["retry_pending"], "O1: ...and the slot is then complete")
check(builds[-1] == {"ACME", "ZED.NS"}, "O1: the retry reused every stock searched (no search repeated)")
code, msgs = run_news()
check(code == 0 and msgs == [], "O1: a third run of the slot posts nothing")

state["file"] = None
deliver["fail"] = {"digest"}       # every part fails
code, msgs = run_news()
f = state["file"]
check(code == 1 and not f["posted"] and f["retry_pending"] and not f["unsent_messages"],
      "O1: nothing delivered -> not posted, red (the gate retries the slot)")
deliver["fail"] = set()
code, msgs = run_news()
check(code == 0 and sum("digest" in m for m in msgs) == 2 and state["file"]["posted"],
      "O1: ...and the retry posts the whole digest, once")

state["file"] = None
code, msgs = run_news()
check(code == 0 and len(msgs) == 2 and state["file"]["posted"] and not state["file"]["unsent_messages"],
      "O1: a clean first run is unchanged: posted, green")

# --- O2 _post_discord retries ---------------------------------------------------------------------
real_post = requests.post


def post_with(plan):
    post, calls = fake_post(list(plan))
    requests.post = post
    try:
        ok, detail = alerts._post_discord("https://discord.example/hook", "hello")
    finally:
        requests.post = real_post
    return ok, detail, len(calls)


ok, detail, n = post_with([502, 503, 204])
check(ok and n == 3, f"O2: 502, 503, then 204 -> delivered on the third try ({n} posts)")
ok, detail, n = post_with([500, 500, 500, 204])
check(not ok and n == 3 and detail.startswith("500"), f"O2: a 5xx three times -> failed after 2 retries ({n} posts, {detail[:10]!r})")
ok, detail, n = post_with([requests.ConnectionError("refused"), 204])
check(ok and n == 2, "O2: a connection error -> retried")
ok, detail, n = post_with([requests.ConnectTimeout("connect timeout"), 204])
check(ok and n == 2, "O2: a CONNECT timeout (never sent) -> retried")
ok, detail, n = post_with([requests.ReadTimeout("read timeout"), 204])
check(not ok and n == 1, "O2: a READ timeout -> not retried (the message may have been posted)")
ok, detail, n = post_with([400, 204])
check(not ok and n == 1, "O2: a 400 -> not retried")
ok, detail, n = post_with([429, 502, 204])
check(ok and n == 3, "O2: a 429 then a 502 -> both retried")

# --- O3 the wrap-up and the alert check -----------------------------------------------------------
import weekly_wrapup_check as wc

saved_state = {}
wc.load_rules = lambda: [{"id": "r1"}]
wc.eligible_rules = lambda rules: rules
wc.load_settings = lambda: {}
wc.fetch_all_markets = lambda **kw: ([{"ticker": "ACME", "market": "m1"}], "now", {"m1": [{"ticker": "ACME", "market": "m1"}]})
wc.load_data_snapshot = lambda: {}
wc.reject_stale_rows = lambda pm, prev, **kw: (pm, {})
wc.enrich_rows = lambda rows, s: None
wc.load_watchlists = lambda: {"m1": ["ACME"]}
wc.get_filterable_metrics = lambda s: {}
wc.load_wrapup_state = lambda: {"entries": {}}
wc.build_wrapup = lambda *a, **kw: {"cycle_ids": [], "rollup": []}
wc.load_markets_registry = lambda: {}
wc.build_discord_messages = lambda w: ["part one", "part two", "part three"]
wc.load_discord_webhook = lambda: "https://discord.example/hook"
wc.advance_state = lambda *a, **kw: {"advanced": True}
wc.save_wrapup_state = lambda s: saved_state.update(s)
argv = sys.argv
sys.argv = ["weekly_wrapup_check.py"]


def run_wrapup(plan):
    saved_state.clear()
    post, calls = fake_post(list(plan))
    requests.post = post
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            wc.main()
        code = 0
    except SystemExit as e:
        code = e.code
    finally:
        requests.post = real_post
    return code, len(calls)


code, n = run_wrapup([400])
check(code == 1 and not saved_state and n == 1,
      f"O3: wrap-up, nothing delivered -> exit 1 (the slot retries), state NOT advanced (exit {code})")
code, n = run_wrapup([204, 400])
check(code == 0 and not saved_state and n == 2,
      "O3: wrap-up, part delivered then a failure -> green (a retry would repost it), state not advanced")
code, n = run_wrapup([])
check(code == 0 and saved_state.get("advanced"), "O3: wrap-up, all delivered -> state advanced, green")
code, n = run_wrapup([502, 204, 204, 204])
check(code == 0 and saved_state.get("advanced") and n == 4, "O3: a 502 on the first post is retried and the wrap-up lands")

import alert_check as ac

COND = [{"metric_a": "rsi14_daily", "operator": ">", "compare_type": "value", "value": 50}]
SCHED = {"type": "scheduled", "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], "time_et": "21:00"}
RULES = [alerts.normalize_rule({"id": i, "name": i, "scope": "ALL", "conditions": COND, "enabled": True,
                                "schedule": dict(SCHED)}) for i in ("ra", "rb")]
snap = [{"ticker": "ZED.NS", "market": "m1", "rsi14_daily": 60}, {"ticker": "ZIN1.NS", "market": "m1", "rsi14_daily": 70}]
captured = {}
ac.load_rules = lambda: RULES
ac.is_rule_due = lambda *a, **kw: True
ac.fetch_all_markets = lambda **kw: (snap, "now", {"m1": snap})
ac.load_data_snapshot = lambda: {"per_market": {}, "short_history": {}}
ac.load_watchlists = lambda: {"m1": ["ZED.NS", "ZIN1.NS"]}
ac.load_state_status = lambda: ({}, True)
ac.save_state = lambda s: captured.update(s)
ac.load_discord_webhook = lambda: "https://discord.example/hook"
ac.build_discord_messages_for_rule = alerts.build_discord_messages_for_rule = (
    lambda rule, tickers, by_ticker, labels, limit=1900: [f"table {rule['id']}"])
sys.argv = ["alert_check.py"]


def run_alerts(fail_rule_ids, fail_header=False):
    captured.clear()

    def post(url, json=None, timeout=None):
        c = json["content"]
        bad = (fail_header and "Stock Alert Check" in c) or any(f"table {r}" in c for r in fail_rule_ids)
        return Resp(400 if bad else 204)
    requests.post = post
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            ac.check_rules()
        code = 0
    except SystemExit as e:
        code = e.code
    finally:
        requests.post = real_post
    return code


code = run_alerts({"ra", "rb"}, fail_header=True)
check(code == 1 and captured and all(v.get("was_active") is False for k, v in captured.items()),
      f"O3: alerts, every message failed -> exit 1 (the slot retries) with every alert rolled back (exit {code})")
code = run_alerts({"rb"})
check(code == 0 and captured["ra:ZED.NS"]["was_active"] is True and captured["rb:ZED.NS"]["was_active"] is False,
      "O3: alerts, one rule's message failed -> green; only that rule is rolled back (the next due run re-announces it)")
code = run_alerts(set(), fail_header=True)
check(code == 0, "O3: alerts, only the header failed -> green (the tables arrived)")
code = run_alerts(set())
check(code == 0 and all(v["was_active"] for v in captured.values()), "O3: alerts, all delivered -> green, nothing rolled back")
sys.argv = argv

# --- O4 merge_keyed.py ------------------------------------------------------------------------------
sys.path.insert(0, str(REPO / ".github/actions/commit-data"))
import merge_keyed as mk


def v(stamp, tag=""):
    return {"verdict": "HOLD", "as_of": stamp, "tag": tag}


base = {"ACME": v("2026-11-21 01:00"), "ZED.NS": v("2026-11-21 01:00"), "ZIN1.NS": v("2026-11-21 01:00"),
        "OLD": v("2026-11-20 01:00"), "GONE": v("2026-11-20 01:00"), "BOTH": v("2026-11-21 01:00")}
ours = {"ACME": v("2026-11-22 01:10", "run"),          # the run rewrote it
        "ZED.NS": base["ZED.NS"],                       # untouched by the run
        "ZIN1.NS": v("2026-11-22 01:20", "run"),        # both changed; the app's is newer
        "BOTH": v("2026-11-22 03:00", "run"),            # both changed; the run's is newer
        "NEW": v("2026-11-22 01:30", "run")}            # OLD and GONE pruned by the run
theirs = {"ACME": base["ACME"], "ZED.NS": v("2026-11-22 02:00", "app"), "ZIN1.NS": v("2026-11-22 02:30", "app"),
          "OLD": base["OLD"], "GONE": v("2026-11-22 02:40", "app"), "BOTH": v("2026-11-22 02:00", "app"),
          "APPNEW": v("2026-11-22 02:50", "app")}
m = mk.merge(base, ours, theirs)
check(m["ACME"]["tag"] == "run", "O4: changed by the run only -> the run's entry")
check(m["ZED.NS"]["tag"] == "app", "O4: changed on the branch only (the app's re-analyze) -> kept, not reverted")
check(m["ZIN1.NS"]["tag"] == "app" and m["BOTH"]["tag"] == "run", "O4: changed on both -> the newer as_of wins, either way")
check("OLD" not in m, "O4: pruned by the run, untouched on the branch -> deleted")
check(m["GONE"]["tag"] == "app", "O4: pruned by the run but rewritten on the branch -> kept")
check(m["NEW"]["tag"] == "run" and m["APPNEW"]["tag"] == "app", "O4: a key new on either side is kept")
check(mk.merge({"A": v("1")}, {"A": v("2", "run")}, {"A": v("2", "app")})["A"]["tag"] == "run",
      "O4: the same stamp on both -> the run's")
check(mk.merge({}, {"A": {"x": 1}}, {"A": {"x": 2}})["A"] == {"x": 1}, "O4: no stamps and no base -> the run's")
check(mk.merge({"A": {"x": 1}}, {"A": {"x": 1}}, {}) == {}, "O4: deleted on the branch, untouched by the run -> stays deleted")


def git(*args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return r


work = Path(tempfile.mkdtemp(prefix="ops_commit_"))
bare, app = work / "remote.git", work / "app"
subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
subprocess.run(["git", "clone", "-q", str(bare), str(app)], check=True, capture_output=True)
for repo_dir in (app,):
    git("config", "user.email", "t@t.t", cwd=repo_dir)
    git("config", "user.name", "t", cwd=repo_dir)
(app / "expert_views.json").write_text(json.dumps(base, indent=2))
(app / "data_snapshot.json").write_text(json.dumps({"generated_at": "2026-11-21 01:00"}))
git("add", "-A", cwd=app)
git("commit", "-qm", "seed", cwd=app)
git("branch", "-M", "main", cwd=app)
git("push", "-q", "origin", "main", cwd=app)

# The job: load-data's checkout and its copy step (with the tag), the run's output, commit-data's bash.
ws = work / "ws"
ws.mkdir()
subprocess.run(["git", "clone", "-q", "-b", "main", str(bare), str(ws / "data")], check=True, capture_output=True)
load = yaml.safe_load((REPO / ".github/actions/load-data/action.yml").read_text())
load_sh = next(s["run"] for s in load["runs"]["steps"] if "run" in s)
r = subprocess.run(["bash", "-e", "-c", load_sh], cwd=ws, capture_output=True, text=True)
check(r.returncode == 0 and git("rev-parse", "data-loaded", cwd=ws / "data").returncode == 0,
      f"O4: load-data copies the files and tags the commit it loaded ({r.stderr.strip()[:80]})")

# Meanwhile the app re-analyzes two tickers and pushes them (push_json_entry_changes' effect).
git("pull", "-q", "origin", "main", cwd=app)
mid = {k: val for k, val in json.loads((app / "expert_views.json").read_text()).items()}
mid["ZED.NS"] = v("2026-11-22 02:00", "app")
mid["ZIN1.NS"] = v("2026-11-22 02:30", "app")
mid["APPNEW"] = v("2026-11-22 02:50", "app")
(app / "expert_views.json").write_text(json.dumps(mid, indent=2))
git("commit", "-qam", "app edit", cwd=app)
git("push", "-q", "origin", "main", cwd=app)

(ws / "expert_views.json").write_text(json.dumps(ours, indent=2))
commit = yaml.safe_load((REPO / ".github/actions/commit-data/action.yml").read_text())
commit_sh = next(s["run"] for s in commit["runs"]["steps"] if "run" in s)
env = dict(os.environ, FILES="expert_views.json", ENTRIES_ONLY="", ENTRIES_MERGE="expert_views.json",
           MESSAGE="test", ACTION_PATH=str(REPO / ".github/actions/commit-data"), GITHUB_WORKSPACE=str(ws),
           GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t.t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t.t")
r = subprocess.run(["bash", "-e", "-c", commit_sh], cwd=ws, env=env, capture_output=True, text=True)
git("pull", "-q", "origin", "main", cwd=app)
tip = json.loads((app / "expert_views.json").read_text())
check(r.returncode == 0, "O4: commit-data with entries-merge pushes"
      + ("" if r.returncode == 0 else f" ({(r.stdout + r.stderr).strip()[-200:]!r})"))
check(tip.get("ZED.NS", {}).get("tag") == "app" and tip.get("ZIN1.NS", {}).get("tag") == "app" and "APPNEW" in tip,
      "O4: end to end -- the entries the app pushed while the job ran survive the nightly commit")
check(tip.get("ACME", {}).get("tag") == "run" and tip.get("NEW", {}).get("tag") == "run" and "OLD" not in tip
      and tip.get("BOTH", {}).get("tag") == "run",
      "O4: ...and the job's own entries and prunes land")

# Without entries-merge (the old whole-file commit) the app's entries would have been reverted: the fixture
# really exercises the bug.
r_old = mk.merge(base, ours, ours)
check(r_old.get("ZED.NS", {}).get("tag") != "app", "O4: (control) the run's own copy alone lacks the app's entries")
shutil.rmtree(work, ignore_errors=True)

# --- O5 github_sync's sync state ---------------------------------------------------------------------
import github_sync as gs

src = (REPO / "github_sync.py").read_text()
check('f"{SYNC_STATE_FILE}.tmp"' not in src and 'f"{path}.tmp"' not in src,
      "O5: no fixed temp path for the sync state or a pulled data file")
d = tempfile.mkdtemp(prefix="ops_sync_")
saved_attrs = (gs.SCRIPT_DIR, gs.SYNC_STATE_FILE)
gs.SCRIPT_DIR, gs.SYNC_STATE_FILE = d, os.path.join(d, ".data_sync_state.json")
try:
    gs._write_sync_state({"checked_at": 1, "blobs": {}})
    N = 60
    start = threading.Barrier(2)

    def pusher(prefix):
        start.wait()
        for i in range(N):
            gs._record_pushed({f"{prefix}{i}.json": f"{i:040d}"})

    ts = [threading.Thread(target=pusher, args=(p,)) for p in ("a", "b")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    blobs = gs._read_sync_state().get("blobs") or {}
    check(len(blobs) == 2 * N, f"O5: two threads recording pushes at once lose nothing ({len(blobs)} of {2 * N})")
    gs._note_merge("merged")
    check(gs._read_sync_state()["merge_note"]["text"] == "merged" and len(gs._read_sync_state()["blobs"]) == 2 * N,
          "O5: a merge note keeps the blobs")
    check(not [n for n in os.listdir(d) if n.endswith(".tmp")], "O5: no temp file left behind")

    # A pull that downloads while another session pushes: the push's records survive the pull's write.
    remote = {"settings.json": "1" * 40, "news_summary.json": "2" * 40}

    def slow_tree(token, repo, branch):
        gs._record_pushed({"watchlist.json": "9" * 40})     # another session pushes mid-pull
        return remote, None

    class Blob:
        status_code = 200
        content = b'{"x": 1}'

    real_tree, real_get = gs._remote_tree, gs.requests.get
    gs._remote_tree = slow_tree
    gs.requests.get = lambda *a, **kw: Blob()
    try:
        updated, note = gs.pull_generated_files("t", "o/r", files=["news_summary.json"], force=True)
    finally:
        gs._remote_tree, gs.requests.get = real_tree, real_get
    blobs = gs._read_sync_state()["blobs"]
    check(updated == ["news_summary.json"] and blobs.get("news_summary.json") == "2" * 40
          and blobs.get("watchlist.json") == "9" * 40 and len(blobs) == 2 * N + 2,
          f"O5: a pull keeps a push recorded while it downloaded ({note})")
    check(json.load(open(os.path.join(d, "news_summary.json"))) == {"x": 1}
          and not [n for n in os.listdir(d) if n.endswith(".tmp")], "O5: the pulled file landed, no temp file left")
finally:
    gs.SCRIPT_DIR, gs.SYNC_STATE_FILE = saved_attrs
    shutil.rmtree(d, ignore_errors=True)

# --- O6 dead code ------------------------------------------------------------------------------------
check(not hasattr(alerts, "_format_rule_message"), "O6: alerts._format_rule_message (no caller) is gone")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
