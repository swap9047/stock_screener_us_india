"""Public-log privacy, the price corrections' edge cases and Sentiment's resume (owner's review, 2026-11-22).

L1  log_redact masks tickers held only in the AI stores, stage_analysis.json, the alert state files or a snapshot row;
    a missing or corrupt file is tolerated
L2  the orphan prune of both nightly AI jobs prints a count, never the tickers
M1  market_scope: positions <-> keys, old key names accepted, junk / out of range ignored with a count-only warning,
    a scope that matched nothing stays a scope
M2  the scripts read positions (Sentiment, Expert Take, the snapshot refresh, the news digest)
M3  the app dispatches positions; no workflow's `markets` input promises names
P1  a listed factor near 1 (10/11) applies on an unrepaired series and not on a repaired (flat) one; the load warns
P2  a bad tick down-and-back followed by a real demerger: the demerger is still adjusted; a gap on the last bar is
P3  a listed correction divides Volume before its date by the factor; an automatic gap keeps Volume
R1  Sentiment resumes like Expert Take: the 9 PM ET slot, a scheduled full run skips what this slot already wrote

Offline: invented tickers (ACME, ZED.NS, ...), stubbed models, snapshot and stores.
"""

import ast
import contextlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import log_redact as lr
import market_scope as ms
import price_gaps as pg

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def captured(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*a, **kw)
    return out, buf.getvalue()


# --- L1 ---------------------------------------------------------------------------------------------
d = tempfile.mkdtemp(prefix="redact_")
def put(name, obj):
    with open(os.path.join(d, name), "w") as fh:
        json.dump(obj, fh)

put("watchlist.json", {"us_picks": ["ACME"]})
put("expert_views.json", {"ZED.NS": {"verdict": "HOLD"}})
put("fundamentals.json", {"ZFUND": {"sentiment": "Positive"}, "_meta": {"x": 1}})
put("stage_analysis.json", {"meta": {"uploaded": "2026-11-20"}, "tickers": {"ZSTG.BO": {"stage": "S2 Advancing"}}})
put("alert_state.json", {"rule_1:ZALRT": {"was_active": True}, "junkkey": 1})
put("weekly_wrapup_state.json", {"last_run": "2026-11-15", "entries": {"rule_2:ZWRAP.NS": {"entered": "2026-11-01"}}})
put("data_snapshot.json", {"per_market": {"old_list": [{"ticker": "ZSNAP", "company_name": "Zsnap Holdings"}]}})
with open(os.path.join(d, "ticker_notes.json"), "w") as fh:
    fh.write("{not json")                                   # corrupt: tolerated

pats = lr.build_patterns(d)
r = lambda s: lr.redact(s, pats)
check(r("Pruned ZED.NS") == "Pruned [ticker]" and r("ZED") == "[ticker]",
      "L1: a ticker only in expert_views.json is masked (and its bare symbol)")
check(r("ZFUND ZSTG.BO ZALRT ZWRAP.NS ZSNAP") == " ".join(["[ticker]"] * 5),
      "L1: fundamentals.json keys, stage_analysis tickers, alert / wrap-up state tickers and snapshot row tickers")
check(r("_meta junkkey ACME") == "_meta junkkey [ticker]", "L1: meta keys and keys without a ticker part are not taken")
out = subprocess.run(f"cd {d} && printf 'kept ZED.NS view\\n' | {sys.executable} -u {REPO}/log_redact.py",
                     shell=True, capture_output=True, text=True)
check(out.stdout == "kept [ticker] view\n", f"L1: pipe mode masks a ticker held only in expert_views.json ({out.stdout!r})")
d2 = tempfile.mkdtemp(prefix="redact_bad_")
for name in ("expert_views.json", "stage_analysis.json", "alert_state.json", "data_snapshot.json"):
    with open(os.path.join(d2, name), "w") as fh:
        fh.write("[1, 2")
with open(os.path.join(d2, "weekly_wrapup_state.json"), "w") as fh:
    json.dump(["a", "list"], fh)
with open(os.path.join(d2, "fundamentals.json"), "w") as fh:
    json.dump({"ZOK": {}}, fh)
try:
    pats2 = lr.build_patterns(d2)
    check(lr.redact("ZOK", pats2) == "[ticker]", "L1: corrupt or odd-shaped files are skipped, the rest still read")
except Exception as e:
    check(False, f"L1: corrupt files raised {e!r}")

# --- L2 ---------------------------------------------------------------------------------------------
import refresh_expert_views as rev
import refresh_fundamentals as rf

for mod in (rev, rf):
    store = {"ACME": {}, "ZORPH.NS": {}, "ZORPH2": {}}
    n, text = captured(mod._prune_orphans, store, {"us_picks": ["ACME"]}, "store")
    check(n == 2 and set(store) == {"ACME"} and "ZORPH" not in text and "Pruned 2 store orphan(s)" in text,
          f"L2: {mod.__name__} prunes orphans and prints only the count ({text.strip()!r})")

# --- M1 ---------------------------------------------------------------------------------------------
KEYS = ["wl_a", "wl_b", "wl_c"]
check(ms.encode(["wl_b", "wl_c"], KEYS) == "2,3" and ms.encode(["wl_c", "wl_a"], KEYS) == "3,1",
      "M1: encode sends 1-based positions in registry order")
check(ms.encode(["wl_b", "not_registered"], KEYS) == "2", "M1: an unregistered key is never sent")
check(ms.resolve("2,3", KEYS) == (["wl_b", "wl_c"], 0) and ms.resolve(" 3 , 1 ", KEYS) == (["wl_a", "wl_c"], 0),
      "M1: positions map back to keys (registry order, spaces ignored)")
check(ms.resolve("wl_b", KEYS) == (["wl_b"], 0) and ms.resolve("1,wl_c", KEYS) == (["wl_a", "wl_c"], 0),
      "M1: an old key name (an older app, a run by hand) is still accepted, mixed with positions too")
check(ms.resolve("", KEYS) == ([], 0) and ms.resolve(None, KEYS) == ([], 0) and ms.resolve(" , ", KEYS) == ([], 0),
      "M1: blank is every watchlist")
check(ms.resolve("0,4,-1,2,junk,2.5", KEYS) == (["wl_b"], 5), "M1: out of range, negative, junk and decimals are ignored, counted")
logged = []
sel = ms.only_markets("2,9,secret_list", KEYS, log=logged.append)
check(sel == {"wl_b"} and len(logged) == 1 and "2 entries ignored" in logged[0] and "secret_list" not in logged[0]
      and "9" not in logged[0].replace("1-3", ""), f"M1: the warning is a count, never the entry ({logged})")
logged = []
check(ms.only_markets("7", KEYS, log=logged.append) == {ms.NO_MATCH} and logged,
      "M1: a scope that matched nothing stays a scope (not 'every watchlist')")
check(ms.only_markets("", KEYS, log=logged.append) == set(), "M1: blank -> empty set (every watchlist)")
check(not re.fullmatch(r"[a-z0-9_]+", ms.NO_MATCH), "M1: NO_MATCH can never be a registry key (keys are slugs)")
check(ms.from_env(KEYS, environ={"REFRESH_MARKETS": "3"}, log=lambda m: None) == {"wl_c"}, "M1: from_env reads REFRESH_MARKETS")

# --- M2 ---------------------------------------------------------------------------------------------
import time as _time
WL = {"wl_a": ["A0", "A1"], "wl_b": ["B0", "B1"], "wl_c": ["C0"]}
SNAP = {"per_market": {m: [{"ticker": t, "company_name": t, "stage": "S2 Advancing"} for t in tks] for m, tks in WL.items()}}
ENV_KEYS = ("REFRESH_MARKETS", "REFRESH_LIMIT", "REFRESH_PENDING_ONLY", "GITHUB_EVENT_NAME", "SCHEDULED_DISPATCH",
            "REFRESH_SENTIMENT_FIRST")
rf.time.sleep = lambda s: None


def set_env(env):
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update(env)


def run_rf(env, store):
    set_env(env)
    calls = []

    def gen(client, row, is_retry=False):
        calls.append(row["ticker"])
        return {"sentiment": "Positive", "reasoning": "r", "model_used": "m", "news_source": "g",
                "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")}

    rf.get_gemini_api_key = lambda: "k"
    rf.llm_util.make_client = lambda key: object()
    rf.llm_util.log_key_usage = lambda client: None
    rf.load_data_snapshot = lambda: SNAP
    rf.load_watchlists = lambda: WL
    rf.load_fundamentals = lambda: store
    rf.save_fundamentals = lambda s: None
    rf.generate_fundamental_view = gen
    _, text = captured(rf.main)
    return sorted(calls), text


calls, _ = run_rf({"REFRESH_MARKETS": "2"}, {})
check(calls == ["B0", "B1"], f"M2: Sentiment scoped by a position ({calls})")
calls, _ = run_rf({"REFRESH_MARKETS": "wl_c"}, {})
check(calls == ["C0"], f"M2: Sentiment still takes an old key name ({calls})")
calls, text = run_rf({"REFRESH_MARKETS": "9"}, {})
check(calls == [] and "1 entry ignored" in text, f"M2: an out-of-range position runs nothing, with a warning ({calls})")

rev.time.sleep = lambda s: None
rev_calls = []
rev.get_gemini_api_key = lambda: "k"
rev.llm_util.make_client = lambda key: object()
rev.llm_util.log_key_usage = lambda client: None
rev.load_data_snapshot = lambda: SNAP
rev.load_watchlists = lambda: WL
rev.load_expert_views = lambda: {}
rev.save_expert_views = lambda s: None
rev.load_fundamentals = lambda: {}
rev.active_alerts_for_prompt = lambda rows: None
rev.alerts_text_for = lambda a, t: ""
rev.generate_expert_view = lambda client, row, **kw: rev_calls.append(row["ticker"]) or {
    "verdict": "HOLD", "headline": "h", "model_used": "m", "as_of": "2026-11-22 01:00"}
set_env({"REFRESH_MARKETS": "1,3"})
captured(rev.main)
check(sorted(rev_calls) == ["A0", "A1", "C0"], f"M2: Expert Take scoped by positions ({sorted(rev_calls)})")

import news_check as nc
got = {}
nc.get_gemini_api_key = lambda: "k"
nc.load_watchlists = lambda: WL
nc.build_news_summary = lambda wl, key, **kw: got.setdefault("built", wl) and {
    "as_of": "new", "totals": {"searched": 1, "failed": 0}, "markets": {m: {"summary": "NEW"} for m in wl}}
nc.load_news_summary = lambda: {"as_of": "old", "markets": {}}
nc.save_news_summary = lambda data: None
nc.load_discord_webhook = lambda: ""
set_env({"REFRESH_MARKETS": "3"})
try:
    captured(nc.main)
except SystemExit:
    pass
check(got.get("built") == {"wl_c": ["C0"]}, f"M2: the news digest scoped by a position ({got.get('built')})")

import refresh_data as rd
seen = {}
def fake_fetch(watchlists, **kw):
    seen["wl"] = dict(watchlists)
    raise RuntimeError("stop here")
rd.load_watchlists = lambda: WL
rd.load_settings = lambda: {}
rd.fetch_all_markets = fake_fetch
set_env({"REFRESH_MARKETS": "2"})
try:
    captured(rd.main)
except RuntimeError:
    pass
check(seen.get("wl") == {"wl_b": ["B0", "B1"]}, f"M2: the snapshot refresh scoped by a position ({seen.get('wl')})")
set_env({"REFRESH_MARKETS": "8"})
seen.clear()
_, text = captured(rd.main)
check("wl" not in seen and "Nothing to refresh" in text, "M2: the snapshot refresh with nothing matched fetches nothing")
set_env({})

# --- M3 ---------------------------------------------------------------------------------------------
app_src = (REPO / "app.py").read_text()
tree = ast.parse(app_src)
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "trigger_ai_refresh_workflow")
sent = {}
fake_st = types.SimpleNamespace(error=lambda m: sent.setdefault("error", m), success=lambda m: sent.setdefault("ok", m),
                                secrets=None)
fake_sync = types.ModuleType("github_sync")
fake_sync.get_github_config = lambda secrets: ("tok", "owner/repo", "main")
fake_sync.trigger_github_workflow = lambda token, repo, wf, inputs=None: (sent.setdefault("inputs", inputs), (True, ""))[1]
real_sync = sys.modules.get("github_sync")
sys.modules["github_sync"] = fake_sync
ns = {"st": fake_st, "load_markets_registry": lambda: {"wl_a": {}, "wl_b": {}, "wl_c": {}}}
exec(compile(ast.Module(body=[fn], type_ignores=[]), "app.py", "exec"), ns)
ns["trigger_ai_refresh_workflow"]("expert-views.yml", "Expert Views", "Re-analyze All", ["wl_c", "wl_a"], pending_only=True)
check(sent.get("inputs") == {"markets": "3,1", "pending_only": "true"},
      f"M3: the app dispatches positions, never the keys ({sent.get('inputs')})")
sent.clear()
ns["trigger_ai_refresh_workflow"]("expert-views.yml", "Expert Views", "Re-analyze All", ["unknown_key"])
check("inputs" not in sent and "error" in sent, "M3: a view whose lists are not registered dispatches nothing")
if real_sync is not None:
    sys.modules["github_sync"] = real_sync
else:
    sys.modules.pop("github_sync", None)
check(not re.search(r'"markets"\s*:\s*",".join', app_src), "M3: no other dispatch in app.py joins market keys")

promises = re.compile(r"\b(market|watchlist) (keys?|names?)\b", re.I)
markets_inputs = 0
for path in sorted((REPO / ".github/workflows").glob("*.yml")):
    wf = yaml.safe_load(path.read_text()) or {}
    on = wf.get("on", wf.get(True)) or {}
    inputs = ((on.get("workflow_dispatch") or {}) if isinstance(on, dict) else {}).get("inputs") or {}
    for name, spec in inputs.items():
        desc = str((spec or {}).get("description", ""))
        if promises.search(desc):
            check(False, f"M3: {path.name} input {name!r} promises names: {desc!r}")
        if name == "markets":
            markets_inputs += 1
            check("position" in desc.lower(), f"M3: {path.name}'s markets input says positions")
check(markets_inputs == 3, f"M3: the three AI workflows' markets inputs were all checked ({markets_inputs})")

# --- P1 ---------------------------------------------------------------------------------------------
TMP = tempfile.mkdtemp(prefix="price_privacy_")
FILE = os.path.join(TMP, "price_adjustments.json")
pg.CORRECTIONS_FILE = FILE
days = pd.bdate_range("2026-10-12", periods=14)


def write(obj):
    with open(FILE, "w") as fh:
        json.dump(obj, fh)
    os.utime(FILE, ns=(0, os.stat(FILE).st_mtime_ns + 1_000_000))


F11 = 10 / 11
_, text = captured(write, {"ACME.NS": [{"before": "2026-10-20", "factor": F11}]})
_, text = captured(pg.load_corrections)
check("::warning::" in text and "1 listed factor" in text and "ACME" not in text,
      f"P1: a factor within the tolerance of 1 is counted at load, without its ticker ({text.strip()!r})")
_, again = captured(pg.load_corrections)
check(again == "", "P1: the warning is printed once per file version (cached)")
unrepaired = pd.Series([110.0] * 6 + [100.0] * 8, index=days)        # the x10/11 break still in the data
repaired = pd.Series([100.0] * 14, index=days)                        # Yahoo fixed it: flat across the date
adj, found = pg.adjust_series(unrepaired, "ACME.NS")
check(len(found) == 1 and np.allclose(adj.values, 100.0), f"P1: 10/11 applies on an unrepaired series ({found})")
adj, found = pg.adjust_series(repaired, "ACME.NS")
check(found == [] and adj.equals(repaired), "P1: 10/11 does not apply on a repaired (flat) series -- no fake 9% step")
moved = pd.Series([110.0] * 6 + [100.0 * 1.02] * 8, index=days)      # the break plus a +2% day
check(len(pg.adjust_series(moved, "ACME.NS")[1]) == 1, "P1: 10/11 plus a small day's move still matches")
# An independent restatement of the rule: within 15% of the factor and at least twice as far from 1.
for obs in (0.62, 0.64, 0.70, 5 / 7, 0.75, 0.79, 0.80, 0.83, 0.86, 1.0):
    expect = abs(math.log(obs / (5 / 7))) <= math.log(1.15) and abs(math.log(obs)) >= 2 * abs(math.log(obs / (5 / 7)))
    if pg._break_matches(obs, 5 / 7) != expect:
        check(False, f"P1: rule disagrees at observed {obs}")
check(pg._break_matches(0.643, 5 / 7) and pg._break_matches(0.786, 5 / 7) and not pg._break_matches(1.0, 5 / 7)
      and not pg._break_matches(0.0, 5 / 7), "P1: 5/7 still matches with a +-10% day, never a flat or zero step")
_, text = captured(write, {"ACME.NS": [{"before": "2026-10-20", "factor": 5 / 7}]})
_, text = captured(pg.load_corrections)
check("::warning::" not in text, "P1: a 5/7 factor gives no warning")

# --- P2 ---------------------------------------------------------------------------------------------
# a bad tick (x0.1 and back) at bars 3-4, then a real demerger (x0.1, holds) at bar 6
seq = pd.Series([100, 101, 102, 10, 102, 103, 10.3, 10.4, 10.5, 10.6], dtype=float,
                index=pd.bdate_range("2026-10-01", periods=10))
found = pg.gaps(seq)
check([str(a.date()) for a, _ in found] == [str(seq.index[6].date())],
      f"P2: the bad tick is skipped and the demerger after it is still found ({[str(a.date()) for a, _ in found]})")
adj, _ = pg.adjust_series(seq, "ZED.NS")
check(pg.gaps(adj) == [] and abs(adj.iloc[5] / adj.iloc[6] - 1) < 0.05, "P2: the adjusted series has no gap left")
last = pd.Series([100, 101, 102, 103, 10.3], dtype=float, index=pd.bdate_range("2026-10-01", periods=5))
found = pg.gaps(last)
check(len(found) == 1 and found[0][0] == last.index[-1],
      "P2: a gap on the last bar (a demerger's record date) is adjusted at once, by design")

# --- P3 ---------------------------------------------------------------------------------------------
write({"ACME.NS": [{"before": "2026-10-20", "factor": 5 / 7}]})
p = pd.Series([700.0] * 6 + [500.0] * 8, index=days)
frame = pd.DataFrame({"Open": p, "High": p, "Low": p, "Close": p, "Volume": np.array([1000] * 14, dtype="int64")})
out, found = pg.adjust_frame(frame, "ACME.NS")
before = days < pd.Timestamp("2026-10-20")
check(len(found) == 1 and (out["Volume"][before] == 1400).all() and (out["Volume"][~before] == 1000).all(),
      f"P3: a listed 5/7 correction turns old volumes into new shares (x7/5) ({list(out['Volume'][:2])})")
check(out["Volume"].dtype == np.dtype("int64") and (frame["Volume"] == 1000).all(),
      "P3: an integer Volume stays integer, and the caller's frame is not modified")
demerge = pd.Series([450.0] * 5 + [45.0] * 9, index=days)
dframe = pd.DataFrame({"Open": demerge, "High": demerge, "Low": demerge, "Close": demerge, "Volume": 1000.0})
out, found = pg.adjust_frame(dframe, "ZED.NS")
check(len(found) == 1 and (out["Volume"] == 1000.0).all(), "P3: an automatic 40% gap (a demerger) keeps Volume")
raw = pd.concat({"ACME.NS": frame}, axis=1)
d_out, which = pg.adjust_download(raw, ["ACME.NS"])
check(which == ["ACME.NS"] and (d_out["ACME.NS"]["Volume"][before] == 1400).all(),
      "P3: the download frame carries the scaled Volume too")

# --- R1 ---------------------------------------------------------------------------------------------
utc = lambda *a: datetime(*a, tzinfo=timezone.utc)
check(rf.slot_start_utc(utc(2026, 10, 23, 2, 0)) == utc(2026, 10, 23, 1, 0),
      "R1: 22:00 EDT -> tonight's 21:00 EDT slot (01:00 UTC)")
check(rf.slot_start_utc(utc(2026, 10, 23, 6, 30)) == utc(2026, 10, 23, 1, 0),
      "R1: a retry at 02:30 EDT still works last night's 21:00 slot")
check(rf.slot_start_utc(utc(2026, 10, 23, 0, 30)) == utc(2026, 10, 22, 1, 0), "R1: 20:30 EDT -> the slot before, never a future one")
check(rf.slot_start_utc(utc(2026, 11, 3, 3, 0)) == utc(2026, 11, 3, 2, 0), "R1: 21:00 EST is 02:00 UTC after the clocks change")
wf = (REPO / ".github/workflows/fundamentals.yml").read_text()
check(re.search(rf'slots: "{rf.SLOT_HOUR_ET}"', wf) is not None, f"R1: SLOT_HOUR_ET ({rf.SLOT_HOUR_ET}) is the gate's slot")
step = next(s for s in yaml.safe_load(wf)["jobs"]["build"]["steps"] if "refresh_fundamentals.py" in str(s.get("run", "")))
check(step["env"].get("SCHEDULED_DISPATCH") == "${{ github.event.inputs.scheduled }}",
      "R1: fundamentals.yml passes SCHEDULED_DISPATCH to the step, as expert-views.yml does")

since = utc(2026, 10, 23, 1, 0)
good = {"sentiment": "Positive", "reasoning": "r", "model_used": "m", "news_source": "g", "as_of": "2026-10-23 02:10"}
check(rf.written_since(good, since) and not rf.written_since({**good, "as_of": "2026-10-23 00:59"}, since),
      "R1: a valid view written after the slot is done, one before it is not")
check(not rf.written_since(rf._unknown_fallback("x") | {"as_of": "2026-10-23 02:10"}, since),
      "R1: the failure stub is never done")
check(not rf.written_since({**good, "sentiment": "Unknown", "news_source": "⚪ No Source"}, since),
      "R1: an Unknown that exists only because the search failed is not done")
check(not rf.written_since(None, since) and not rf.written_since({**good, "as_of": "garbage"}, since),
      "R1: no view or an unreadable as_of is not done")

now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
old = "2020-01-01 00:00"


def store_with_done():
    s = {t: {**good, "as_of": old} for tks in WL.values() for t in tks}
    for t in ("A0", "B1"):
        s[t] = {**s[t], "as_of": now}
    s["C0"] = {**good, "sentiment": "Unknown", "news_source": "⚪ No Source", "as_of": now}   # search failed: not done
    return s


ALL = sorted({t for tks in WL.values() for t in tks})
calls, text = run_rf({"GITHUB_EVENT_NAME": "workflow_dispatch", "SCHEDULED_DISPATCH": "true"}, store_with_done())
check(calls == ["A1", "B0", "C0"] and "2 ticker(s) already done this slot" in text,
      f"R1: a Worker dispatch for the slot skips what this slot wrote and redoes a failed search ({calls})")
calls, _ = run_rf({"GITHUB_EVENT_NAME": "schedule"}, store_with_done())
check(calls == ["A1", "B0", "C0"], "R1: a schedule event resumes too")
calls, _ = run_rf({"GITHUB_EVENT_NAME": "workflow_dispatch"}, store_with_done())
check(calls == ALL, "R1: a dispatch by hand redoes everything")
calls, _ = run_rf({"GITHUB_EVENT_NAME": "workflow_dispatch", "SCHEDULED_DISPATCH": "false"}, store_with_done())
check(calls == ALL, "R1: scheduled=false redoes everything")
calls, _ = run_rf({"SCHEDULED_DISPATCH": "true", "REFRESH_LIMIT": "2"}, store_with_done())
check(calls == ["A0", "A1"], f"R1: a limited run is a smoke test, not a resume ({calls})")
calls, _ = run_rf({"SCHEDULED_DISPATCH": "true", "REFRESH_MARKETS": "2"}, store_with_done())
check(calls == ["B0", "B1"], f"R1: a scoped run redoes its whole watchlist ({calls})")
bad = store_with_done()
bad["A1"] = rf._unknown_fallback("x")
calls, _ = run_rf({"SCHEDULED_DISPATCH": "true", "REFRESH_PENDING_ONLY": "true"}, bad)
check(calls == ["A1"], f"R1: a pending-only run takes the pending stocks, done this slot or not ({calls})")
set_env({})

print("FAILURES:", fails)
sys.exit(fails)
