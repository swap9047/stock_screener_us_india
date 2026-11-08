"""Retry Pending runs in the background; an Expert run asked for writes a missing Sentiment first (owner, 2026-11-08).

The app's "Retry Pending" buttons ran inside the browser session, and Streamlit stops a session's
script when its browser closes (or a phone puts the tab to sleep), so a retry died with the screen.

B1 llm_util.pending_only() reads the workflows' REFRESH_PENDING_ONLY switch.
B2 refresh_fundamentals.py with the switch analyses only the stocks whose Sentiment is not valid.
B3 refresh_expert_views.py with the switch analyses only the stocks whose Expert Take is not valid
   (a stock with no Stage is skipped either way, as the app's count now leaves it out).
B4 Sentiment first: with REFRESH_SENTIMENT_FIRST a run writes a stock's missing Sentiment before its
   Expert Take and hands it to the prompt; never on the scheduled run, never for a valid Sentiment; a
   failure does not stop the Expert Take; the written tickers are listed in fundamentals.json.entries.
B5 commit-data's `entries-only`: merge_entries.py commits only the listed entries over the branch's
   copy, and the action's bash does it end to end against a real git remote (a Sentiment the other
   workflow committed meanwhile survives); with no list the file is left out.
B6 The workflows: a boolean `pending_only` input, passed to the step, titled "partial run"; the
   Expert job sets REFRESH_SENTIMENT_FIRST only on a dispatch that is not the Worker's scheduled one.
B7 The app: Retry Pending dispatches the workflow with pending_only (no ticker names), Expert Take's
   count skips a stock with no Stage, and the in-browser Re-analyze writes a missing Sentiment first.

Offline: invented tickers, stubbed models, temp git repos.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import llm_util
import refresh_expert_views as rev
import refresh_fundamentals as rf

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


ENV_KEYS = ("REFRESH_MARKETS", "REFRESH_LIMIT", "REFRESH_PENDING_ONLY", "REFRESH_SENTIMENT_FIRST",
            "GITHUB_EVENT_NAME", "SCHEDULED_DISPATCH")


def set_env(env):
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update(env)


# --- B1 -------------------------------------------------------------------------------------------
for value, want in (("true", True), ("True", True), ("false", False), ("", False), (None, False)):
    set_env({} if value is None else {"REFRESH_PENDING_ONLY": value})
    check(llm_util.pending_only() is want, f"B1: REFRESH_PENDING_ONLY={value!r} -> {want}")

time.sleep = lambda s: None   # rev.time and rf.time are this module: no pacing in the stubs
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
WL = {"wl_a": ["ACME", "ZED.NS", "QUX.BO"], "wl_b": ["ZED.NS", "NOSTAGE"]}
ROWS = {t: {"ticker": t, "company_name": t, "stage": "S2 Advancing"} for t in ("ACME", "ZED.NS", "QUX.BO")}
ROWS["NOSTAGE"] = {"ticker": "NOSTAGE", "company_name": "NOSTAGE", "stage": None}
SNAP = {"per_market": {m: [ROWS[t] for t in tks] for m, tks in WL.items()}}

GOOD_SENT = {"sentiment": "Positive", "model_used": "m", "reasoning": "r", "as_of": NOW}
BAD_SENT = {"sentiment": "Unknown", "model_used": "Error", "reasoning": "Analysis unavailable -- x", "as_of": NOW}
GOOD_EXP = {"verdict": "HOLD", "headline": "h", "model_used": "m", "as_of": NOW}
BAD_EXP = {"verdict": "HOLD", "headline": "Analysis pending -- x", "model_used": "Error", "as_of": NOW}


# --- B2 Sentiment job ------------------------------------------------------------------------------
def run_rf(env, store):
    set_env(env)
    calls = []
    rf.get_gemini_api_key = lambda: "k"
    rf.llm_util.make_client = lambda key: object()
    rf.load_data_snapshot = lambda: SNAP
    rf.load_watchlists = lambda: WL
    rf.load_fundamentals = lambda: store
    rf.save_fundamentals = lambda s: None
    rf.generate_fundamental_view = lambda client, row, **kw: calls.append(row["ticker"]) or dict(GOOD_SENT)
    rf.main()
    return sorted(calls)


store = {"ACME": dict(GOOD_SENT), "ZED.NS": dict(BAD_SENT)}     # QUX.BO, NOSTAGE: none at all
check(run_rf({"REFRESH_PENDING_ONLY": "true"}, store) == ["NOSTAGE", "QUX.BO", "ZED.NS"],
      "B2: Sentiment pending-only analyses the failed and the missing, not the valid one")
check(run_rf({}, {"ACME": dict(GOOD_SENT)}) == ["ACME", "NOSTAGE", "QUX.BO", "ZED.NS"],
      "B2: without the switch every stock is analysed, as before")
check(run_rf({"REFRESH_PENDING_ONLY": "true", "REFRESH_MARKETS": "wl_b"}, {"ACME": dict(BAD_SENT)}) == ["NOSTAGE", "ZED.NS"],
      "B2: pending-only stays inside the tab's watchlists")

# --- B3 / B4 Expert job ----------------------------------------------------------------------------
tmp = tempfile.mkdtemp()
rev.FUNDAMENTALS_FILE = os.path.join(tmp, "fundamentals.json")
ENTRIES = rev.FUNDAMENTALS_FILE + ".entries"


def run_rev(env, experts, sentiments, sent_fn=None):
    set_env(env)
    if os.path.exists(ENTRIES):
        os.remove(ENTRIES)
    exp_calls, sent_calls, prompt_sent, saved = [], [], {}, []
    rev.get_gemini_api_key = lambda: "k"
    rev.llm_util.make_client = lambda key: object()
    rev.load_data_snapshot = lambda: SNAP
    rev.load_watchlists = lambda: WL
    rev.load_expert_views = lambda: experts
    rev.save_expert_views = lambda s: None
    if sentiments is None:
        def unreadable():
            raise rev.DataFileError("fundamentals.json is not valid JSON")
        rev.load_fundamentals = unreadable
    else:
        rev.load_fundamentals = lambda: sentiments
    rev.save_fundamentals = lambda s: saved.append(json.loads(json.dumps(s)))
    rev.active_alerts_for_prompt = lambda rows: None
    rev.alerts_text_for = lambda a, t: ""

    def gen_sent(client, row, **kw):
        sent_calls.append(row["ticker"])
        return sent_fn(row) if sent_fn else {**GOOD_SENT, "reasoning": f"new {row['ticker']}"}

    def gen_exp(client, row, fundamental_view=None, **kw):
        exp_calls.append(row["ticker"])
        prompt_sent[row["ticker"]] = (fundamental_view or {}).get("reasoning")
        return dict(GOOD_EXP)

    rev.generate_fundamental_view = gen_sent
    rev.generate_expert_view = gen_exp
    rev.main()
    entries = json.load(open(ENTRIES)) if os.path.exists(ENTRIES) else None
    return sorted(exp_calls), sorted(sent_calls), prompt_sent, saved, entries


MANUAL = {"GITHUB_EVENT_NAME": "workflow_dispatch", "REFRESH_SENTIMENT_FIRST": "true"}
experts = {"ACME": dict(GOOD_EXP), "ZED.NS": dict(BAD_EXP)}
e, s, p, saved, entries = run_rev({**MANUAL, "REFRESH_PENDING_ONLY": "true"}, experts,
                                  {"ACME": dict(GOOD_SENT), "ZED.NS": dict(BAD_SENT)})
check(e == ["QUX.BO", "ZED.NS"], f"B3: Expert pending-only analyses the failed and the missing, not the valid one or the no-Stage one ({e})")
check(s == ["QUX.BO", "ZED.NS"], f"B4: Sentiment first for exactly the analysed stocks whose Sentiment is not valid ({s})")
check(p.get("ZED.NS") == "new ZED.NS" and p.get("QUX.BO") == "new QUX.BO",
      "B4: the Expert prompt gets the Sentiment just written")
check(entries == ["QUX.BO", "ZED.NS"] and saved and saved[-1]["ZED.NS"]["reasoning"] == "new ZED.NS"
      and saved[-1]["ACME"] == GOOD_SENT, f"B4: the written tickers are listed for the commit, the rest kept ({entries})")

e, s, _p, _s, entries = run_rev({**MANUAL}, {}, {"ACME": dict(GOOD_SENT)})
check(e == ["ACME", "QUX.BO", "ZED.NS"] and s == ["QUX.BO", "ZED.NS"],
      "B4: a Re-analyze All (no pending switch) runs every Expert Take and only the missing Sentiments")

for env, label in (({"GITHUB_EVENT_NAME": "workflow_dispatch", "SCHEDULED_DISPATCH": "true", "REFRESH_SENTIMENT_FIRST": "true"},
                    "the Worker's scheduled run, even if the switch were set"),
                   ({"GITHUB_EVENT_NAME": "workflow_dispatch"}, "a run without the switch")):
    e, s, _p, _s, entries = run_rev(env, {}, {})
    check(s == [] and entries is None and len(e) == 3, f"B4: no Sentiment call on {label}")

e, s, _p, _s, entries = run_rev({**MANUAL}, {}, None)
check(s == [] and len(e) == 3, "B4: with fundamentals.json unreadable nothing is written to it, Expert Take still runs")


def boom(row):
    raise RuntimeError("search died")


e, s, p, _s, entries = run_rev({**MANUAL}, {}, {}, sent_fn=boom)
check(len(s) == 3 and len(e) == 3 and entries is None,
      "B4: a Sentiment that raises is logged, nothing is listed, and the Expert Take goes ahead")
e, s, p, saved, entries = run_rev({**MANUAL}, {}, {}, sent_fn=lambda row: {"sentiment": "garbage"})
check(entries == ["ACME", "QUX.BO", "ZED.NS"] and saved[-1]["ACME"].get("model_used") == "Error",
      "B4: a failed Sentiment is stored as the nightly job stores it (an honest Unknown) and listed")

# --- B5 commit-data entries-only -------------------------------------------------------------------
ACTION = REPO / ".github/actions/commit-data"
MERGE = ACTION / "merge_entries.py"
d = tempfile.mkdtemp()
branch_p, ours_p, list_p = (os.path.join(d, n) for n in ("branch.json", "ours.json", "ours.json.entries"))
json.dump({"ACME": "branch", "ZED.NS": "branch-newer"}, open(branch_p, "w"))
json.dump({"ACME": "stale", "ZED.NS": "stale", "QUX.BO": "ours"}, open(ours_p, "w"))
json.dump(["QUX.BO", "GONE"], open(list_p, "w"))
r = subprocess.run([sys.executable, str(MERGE), branch_p, ours_p, list_p])
check(r.returncode == 0 and json.load(open(branch_p)) == {"ACME": "branch", "ZED.NS": "branch-newer", "QUX.BO": "ours"},
      "B5: only the listed entry takes this run's value; the branch's others stay")
before = open(branch_p).read()
os.remove(list_p)
r = subprocess.run([sys.executable, str(MERGE), branch_p, ours_p, list_p])
check(r.returncode == 1 and open(branch_p).read() == before, "B5: no list -> exit 1, the branch copy untouched")
json.dump([], open(list_p, "w"))
check(subprocess.run([sys.executable, str(MERGE), branch_p, ours_p, list_p]).returncode == 1, "B5: an empty list -> exit 1")

action = yaml.safe_load((ACTION / "action.yml").read_text())
check("entries-only" in action["inputs"] and action["inputs"]["entries-only"].get("default") == "",
      "B5: commit-data takes an optional entries-only input")
script = action["runs"]["steps"][0]["run"]
check(subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0, "B5: the action's bash still parses")


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


work = tempfile.mkdtemp()
bare = os.path.join(work, "remote.git")
seed = os.path.join(work, "seed")
git("init", "-q", "--bare", "-b", "main", bare, cwd=work)
git("clone", "-q", bare, seed, cwd=work)
for k, v in (("user.email", "t@t.t"), ("user.name", "t")):
    git("config", k, v, cwd=seed)
git("checkout", "-q", "-b", "main", cwd=seed)
json.dump({"ACME": {"s": "old"}, "ZED.NS": {"s": "old"}}, open(os.path.join(seed, "fundamentals.json"), "w"))
json.dump({"ACME": {"v": "old"}}, open(os.path.join(seed, "expert_views.json"), "w"))
git("add", ".", cwd=seed); git("commit", "-q", "-m", "seed", cwd=seed); git("push", "-q", "origin", "main", cwd=seed)
ws = os.path.join(work, "ws")
os.makedirs(ws)
git("clone", "-q", bare, os.path.join(ws, "data"), cwd=work)
# This job's copies, loaded at its start: ZED.NS stale, QUX.BO's Sentiment written by it.
json.dump({"ACME": {"s": "old"}, "ZED.NS": {"s": "old"}, "QUX.BO": {"s": "expert job"}}, open(os.path.join(ws, "fundamentals.json"), "w"))
json.dump(["QUX.BO"], open(os.path.join(ws, "fundamentals.json.entries"), "w"))
json.dump({"ACME": {"v": "new"}}, open(os.path.join(ws, "expert_views.json"), "w"))
# Meanwhile the Sentiment workflow commits a newer ZED.NS.
json.dump({"ACME": {"s": "old"}, "ZED.NS": {"s": "sentiment job"}}, open(os.path.join(seed, "fundamentals.json"), "w"))
git("commit", "-q", "-am", "sentiment run", cwd=seed); git("push", "-q", "origin", "main", cwd=seed)


def run_action(files, entries_only):
    env = {**os.environ, "FILES": files, "ENTRIES_ONLY": entries_only, "MESSAGE": "test [automated]",
           "ACTION_PATH": str(ACTION), "GITHUB_WORKSPACE": ws}
    return subprocess.run(["bash", "-e", "-c", script], cwd=ws, env=env, capture_output=True, text=True)


r = run_action("expert_views.json fundamentals.json", "fundamentals.json")
git("pull", "-q", "origin", "main", cwd=seed)
got = json.load(open(os.path.join(seed, "fundamentals.json")))
check(r.returncode == 0 and got == {"ACME": {"s": "old"}, "ZED.NS": {"s": "sentiment job"}, "QUX.BO": {"s": "expert job"}},
      f"B5: end to end, the other workflow's newer Sentiment survives and this run's entry lands ({got}; {r.stderr[-300:]})")
check(json.load(open(os.path.join(seed, "expert_views.json"))) == {"ACME": {"v": "new"}},
      "B5: a file the job owns is still committed whole")
os.remove(os.path.join(ws, "fundamentals.json.entries"))
json.dump({"ACME": {"s": "STALE COPY"}}, open(os.path.join(ws, "fundamentals.json"), "w"))
r = run_action("fundamentals.json", "fundamentals.json")
git("pull", "-q", "origin", "main", cwd=seed)
check(r.returncode == 0 and json.load(open(os.path.join(seed, "fundamentals.json")))["ACME"] == {"s": "old"}
      and "no entries written by this run" in r.stdout,
      "B5: with no entries list (the scheduled run) the job's fundamentals.json is left out")

# --- B6 workflows --------------------------------------------------------------------------------------
for wf_name, step_name in (("expert-views.yml", "Generate AI Expert Views"), ("fundamentals.yml", "Generate AI Fundamental Views")):
    text = (REPO / ".github/workflows" / wf_name).read_text()
    wf = yaml.safe_load(text)
    on = wf.get("on", wf.get(True))
    inp = on["workflow_dispatch"]["inputs"].get("pending_only") or {}
    check(inp.get("type") == "boolean" and inp.get("default") is False, f"B6: {wf_name} has a boolean pending_only input, off by default")
    step = next(s for s in wf["jobs"]["build"]["steps"] if s.get("name") == step_name)
    check(step["env"].get("REFRESH_PENDING_ONLY") == "${{ github.event.inputs.pending_only }}",
          f"B6: {wf_name} passes it to the AI step")
    check("github.event.inputs.pending_only == 'true'" in wf["run-name"] and "'partial run'" in wf["run-name"],
          f"B6: {wf_name} titles a pending-only run 'partial run' (never the slot's work)")
exp = yaml.safe_load((REPO / ".github/workflows/expert-views.yml").read_text())
step = next(s for s in exp["jobs"]["build"]["steps"] if s.get("name") == "Generate AI Expert Views")
check(step["env"].get("REFRESH_SENTIMENT_FIRST")
      == "${{ github.event_name == 'workflow_dispatch' && github.event.inputs.scheduled != 'true' }}",
      "B6: Sentiment first only on a dispatch that is not the Worker's scheduled one")
commit = next(s for s in exp["jobs"]["build"]["steps"] if s.get("uses") == "./.github/actions/commit-data")
check("fundamentals.json" in commit["with"]["files"].split() and commit["with"].get("entries-only") == "fundamentals.json",
      "B6: the Expert job commits fundamentals.json per entry only")
fund = yaml.safe_load((REPO / ".github/workflows/fundamentals.yml").read_text())
fcommit = next(s for s in fund["jobs"]["build"]["steps"] if s.get("uses") == "./.github/actions/commit-data")
check(not fcommit["with"].get("entries-only"), "B6: the Sentiment job, which owns the file, still commits it whole")
check("*.entries" in (REPO / ".gitignore").read_text(), "B6: an entries list (tickers) can never be committed to this public repo")

# --- B7 the app ------------------------------------------------------------------------------------------
src = (REPO / "app.py").read_text()
retry = src[src.index('f"⚠️ Retry Pending ({pending_count})"'):]
retry = retry[:retry.index("if c4.button(")]
check("trigger_ai_refresh_workflow(" in retry and "pending_only=True" in retry and "_reanalyze_tickers_in_dashboard" not in retry,
      "B7: Retry Pending dispatches the workflow instead of running in the browser")
check("disabled=pending_count == 0 or not scope_markets" in retry, "B7: it needs no Gemini key in the app, only a scope")
trig = src[src.index("def trigger_ai_refresh_workflow"):src.index("def _reanalyze_tickers_in_dashboard")]
check('inputs["pending_only"] = "true"' in trig and '"markets": ",".join(markets)' in trig,
      "B7: the dispatch carries the switch and the watchlist keys -- never a ticker")
check(re.search(r'expert_pending = \[r\["ticker"\] for r in results\s+if r.get\("stage"\) in STAGES', src) is not None,
      "B7: Expert Take's pending count leaves out a stock with no Stage")
dash = src[src.index("def _reanalyze_tickers_in_dashboard"):src.index("def _model_settings_budget_text")]
i_sent = dash.find("analyze_single_ticker_sentiment(tk, row, api_key, is_retry=True, client=client)")
i_exp = dash.find("view = generate_expert_view(")
check(0 < i_sent < i_exp and "_is_valid_sentiment_view(fund_views.get(tk))" in dash,
      "B7: the in-browser Expert Re-analyze writes a missing Sentiment before the Expert Take")
check('filenames=("fundamentals.json",), tickers=sentiment_done' in dash,
      "B7: ...and pushes only those Sentiment entries")

set_env({})
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
