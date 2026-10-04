"""The nightly AI jobs stop early when their calls keep failing.

A dead key or exhausted quota fails every ticker: the jobs used to grind on for
~2 hours and then report success, so the slot gate counted the slot as done and
nothing retried. Now 10 failures in a row stop the run (remaining tickers keep
their previous views) and it exits non-zero, so it shows red and the gate's
retry costs minutes. Isolated failures never trip it.

Offline: both jobs run with Gemini, Yahoo and file IO stubbed.
"""

import os
import sys
import time
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO)

import llm_util
import refresh_expert_views as rev
import refresh_fundamentals as rf

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


time.sleep = lambda s: None

# --- the fuse itself ----------------------------------------------------------
f = llm_util.FailureFuse(limit=3)
check([f.record(x) for x in (True, True, False, True, True)] == [False] * 5, "a success resets the streak")
check(f.record(True) is True and f.tripped, "the 3rd failure in a row trips it")
try:
    f.exit_if_tripped()
    check(False, "a tripped fuse exits non-zero")
except SystemExit as e:
    check(e.code == 1, "a tripped fuse exits non-zero")
llm_util.FailureFuse().exit_if_tripped()
check(True, "an untripped fuse exits normally")

# --- wired into both jobs -------------------------------------------------------
TICKERS = [f"T{i:02d}" for i in range(25)]
WL = {"us_invested": TICKERS}
# "trend": the Expert Take job skips a row without one since 2026-10-04.
SNAP = {"per_market": {"us_invested": [{"ticker": t, "company_name": t, "trend": "Uptrend"} for t in TICKERS]}}


def run(mod, gen_name, valid_every):
    """Run `mod` where every `valid_every`-th call returns a valid view (0 = never)."""
    for k in ("REFRESH_MARKETS", "REFRESH_LIMIT"):
        os.environ.pop(k, None)
    calls = []
    mod.get_gemini_api_key = lambda: "k"
    mod.llm_util.make_client = lambda key: object()
    mod.load_data_snapshot = lambda: SNAP
    mod.load_watchlists = lambda: WL

    def gen(client, row, *a, **kw):
        calls.append(row["ticker"])
        ok = valid_every and len(calls) % valid_every == 0
        return {"valid": bool(ok)}
    setattr(mod, gen_name, gen)
    mod._is_valid_view = lambda v: bool(v and v.get("valid"))
    if mod is rf:
        mod._search_failed_unknown = lambda v: False
        mod._apply_result = lambda st, tk, view, old, el: (0, "x")
    else:
        mod._apply_result = lambda st, tk, view, old, el: (0, 0, "x")
    for name in ("load_fundamentals", "load_expert_views"):
        if hasattr(mod, name):
            setattr(mod, name, lambda: {})
    for name in ("save_fundamentals", "save_expert_views"):
        if hasattr(mod, name):
            setattr(mod, name, lambda s: None)
    if hasattr(mod, "active_alerts_for_prompt"):
        mod.active_alerts_for_prompt = lambda rows: None
        mod.alerts_text_for = lambda a, t: ""
    try:
        mod.main()
        code = 0
    except SystemExit as e:
        code = e.code
    return calls, code


for mod, gen in ((rev, "generate_expert_view"), (rf, "generate_fundamental_view")):
    n = mod.__name__
    calls, code = run(mod, gen, valid_every=0)
    # The Sentiment job runs MAX_CONCURRENT_TICKERS at once, so a call or two
    # already in flight may finish after the fuse trips.
    check(10 <= len(calls) <= 10 + rf.MAX_CONCURRENT_TICKERS and code == 1,
          f"{n}: every call failing -> stops after ~10 of 25 and exits 1 (called {len(calls)}, exit {code})")
    calls, code = run(mod, gen, valid_every=3)
    check(len(calls) == 25 and code == 0, f"{n}: scattered failures -> all 25 analysed, exit 0 (called {len(calls)}, exit {code})")

print("TOTAL", "all passed" if not fails else "")
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
