"""refresh_fundamentals analyses two tickers at once, safely.

The 2026-09-18 run took 353 of its 360-minute cap: 136 of those minutes were
spent waiting on grounded-search calls that never answered, which is blocking IO,
not work. Three workers cut the wall clock to roughly a third. What must not change: every
ticker is analysed exactly once, the store is never written by two threads at the
same time, and REFRESH_LIMIT still picks the same first N tickers.

Offline: invented tickers, stubbed Gemini and file IO, no network.
"""

import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO)

import os
import threading
import time

import llm_util
import refresh_fundamentals as rf

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


WL = {"us_invested": ["A1", "A2", "A3"], "us_watchlist": ["A2", "B1", "B2"], "india_invested": ["C1.NS"]}
SNAP = {"per_market": {m: [{"ticker": t, "company_name": t} for t in tks if t != "A1"] for m, tks in WL.items()}}
EXPECTED = {"A2", "A3", "B1", "B2", "C1.NS"}


def run(timeout_first=(), limit=None):
    """Runs main() with everything stubbed. Returns (calls, peak_concurrency,
    save_overlaps, store)."""
    for k in ("REFRESH_MARKETS", "REFRESH_LIMIT"):
        os.environ.pop(k, None)
    if limit:
        os.environ["REFRESH_LIMIT"] = str(limit)

    calls, store = [], {}
    state = {"live": 0, "peak": 0, "in_save": 0, "overlaps": 0}
    lock = threading.Lock()
    pending = set(timeout_first)

    def gen(client, row, *a, **kw):
        tk = row["ticker"]
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
            calls.append(tk)
        time.sleep(0.05)          # real, so overlap is observable
        with lock:
            state["live"] -= 1
        if tk in pending and not kw.get("is_retry"):
            raise TimeoutError("stubbed search timeout")
        return {"ok": True, "ticker": tk}

    def save(s):
        with lock:
            state["in_save"] += 1
            if state["in_save"] > 1:
                state["overlaps"] += 1
        time.sleep(0.01)
        with lock:
            state["in_save"] -= 1

    rf.TICKER_PAUSE_SECONDS = 0
    rf.RETRY_PAUSE_SECONDS = 0
    rf.get_gemini_api_key = lambda: "k"
    rf.llm_util.make_client = lambda key: object()
    rf.load_data_snapshot = lambda: SNAP
    rf.load_watchlists = lambda: WL
    rf.load_fundamentals = lambda: store
    rf.save_fundamentals = save
    rf.generate_fundamental_view = gen
    rf._apply_result = lambda st, tk, view, old, el: (st.__setitem__(tk, view), (0, "ok"))[1]
    rf.main()
    return calls, state["peak"], state["overlaps"], store


check(getattr(rf, "MAX_CONCURRENT_TICKERS", None) == 3,
      f"three tickers at a time (got {getattr(rf, 'MAX_CONCURRENT_TICKERS', None)})")

calls, peak, overlaps, store = run()
check(sorted(calls) == sorted(EXPECTED),
      f"every ticker with a row is analysed exactly once: {sorted(calls)}")
check(len(calls) == len(set(calls)), f"no ticker analysed twice: {calls}")
check(set(store) == EXPECTED, f"...and every one lands in the store: {sorted(store)}")
check(peak == 3, f"three analyses really do overlap, and never more than three (peak {peak})")
check(overlaps == 0, f"the store is never saved by two threads at once ({overlaps} overlap(s))")

# REFRESH_LIMIT has to stay deterministic: the plan is built in registry order
# BEFORE any worker starts, so the same first N tickers are chosen every run.
for n, want in ((2, ["A2", "A3"]), (3, ["A2", "A3", "B1"])):
    calls, _, _, _ = run(limit=n)
    check(sorted(calls) == want, f"limit {n} analyses exactly {want} (got {sorted(calls)})")

# A timed-out ticker still reaches the retry phase, and the retry runs with
# is_retry=True (which is what makes an exhausted search settle for "no news"
# instead of raising again).
calls, _, _, store = run(timeout_first={"B1"})
check(calls.count("B1") == 2, f"a timed-out ticker is retried once ({calls.count('B1')} attempt(s))")
check(set(store) == EXPECTED, "...and ends up in the store like the rest")

# --- the grounded searches get their own, shorter timeout --------------------
# Successful search+reasoning pairs ran 36-145s (median 86) on 2026-09-18, so a
# per-call 120s ceiling sat above the whole distribution while 68 calls spent the
# full 120s answering nothing.
check(getattr(llm_util, "SEARCH_TIMEOUT_SECONDS", None) == 120,
      f"SEARCH_TIMEOUT_SECONDS is 120 (got {getattr(llm_util, 'SEARCH_TIMEOUT_SECONDS', None)})")
for mod in ("fundamentals_eval.py", "expert_views.py"):
    src = (Path(REPO) / mod).read_text()
    check("timeout=llm_util.SEARCH_TIMEOUT_SECONDS" in src or "timeout=SEARCH_TIMEOUT_SECONDS" in src,
          f"{mod}'s grounded search uses SEARCH_TIMEOUT_SECONDS, not a literal")
    check("timeout=120" not in src, f"{mod} has no hardcoded 120s call timeout left")

src = (Path(REPO) / "refresh_fundamentals.py").read_text()
check("RETRY_PAUSE_SECONDS = 5" in src, "the retry pause is 5s")
check("TICKER_PAUSE_SECONDS = 2" in src,
      "the main-loop pause stays 2s -- it was never the 30s one, and raising it would cost time")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
