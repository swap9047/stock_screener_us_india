"""Expert Views: 3 tickers at a time, and a scheduled retry resumes (2026-10-23).

Serial runs had reached 4-6 h a night against GitHub's 360-minute job limit, and
every gate retry of a slot started again from the first ticker -- on 2026-10-20
four attempts re-analysed what earlier ones had finished. Offline: Gemini, the
snapshot and the store are stubbed with invented tickers.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import os, re, threading, time
from datetime import datetime, timezone

import refresh_expert_views as rev

fails = 0
def check(ok, label):
    global fails; fails += not ok; print("PASS" if ok else "FAIL", label)

utc = lambda *a: datetime(*a, tzinfo=timezone.utc)

# --- the slot this run works: latest 01:00 ET, both sides of daylight saving
check(rev.slot_start_utc(utc(2026, 10, 23, 7, 0)) == utc(2026, 10, 23, 5, 0), "03:00 EDT -> tonight's 01:00 EDT slot (05:00 UTC)")
check(rev.slot_start_utc(utc(2026, 10, 23, 4, 30)) == utc(2026, 10, 22, 5, 0), "00:30 EDT -> last night's slot, not one in the future")
check(rev.slot_start_utc(utc(2026, 11, 3, 8, 0)) == utc(2026, 11, 3, 6, 0), "03:00 EST -> 01:00 EST is 06:00 UTC after the clocks change")
wf = (REPO / ".github/workflows/expert-views.yml").read_text()
check(re.search(rf'slots: "{rev.SLOT_HOUR_ET}"', wf) is not None,
      f"SLOT_HOUR_ET ({rev.SLOT_HOUR_ET}) is the expert-views gate's slot")

# --- what counts as already done
since = utc(2026, 10, 23, 5, 0)
good = {"verdict": "HOLD", "headline": "h", "model_used": "m", "as_of": "2026-10-23 06:10"}
check(rev.written_since(good, since), "a valid view written after the slot is done")
check(not rev.written_since({**good, "as_of": "2026-10-23 04:59"}, since), "one written before the slot is not")
check(not rev.written_since({"verdict": "PENDING", "headline": "Analysis pending", "as_of": "2026-10-23 06:10"}, since),
      "a pending placeholder written after the slot is not done")
check(not rev.written_since(None, since) and not rev.written_since({**good, "as_of": "garbage"}, since),
      "no view, or an unreadable as_of, is not done")

# --- the job itself, stubbed
WL = {"wl_a": [f"A{i}" for i in range(6)], "wl_b": ["A1"] + [f"B{i}" for i in range(4)]}
SNAP = {"per_market": {m: [{"ticker": t, "company_name": t, "trend": "Uptrend"} for t in tks] for m, tks in WL.items()}}
real_sleep = time.sleep          # rev.time IS the time module: keep the real one for the stub
rev.time.sleep = lambda s: None

def run(env, store):
    for k in ("REFRESH_MARKETS", "REFRESH_LIMIT", "GITHUB_EVENT_NAME"):
        os.environ.pop(k, None)
    os.environ.update(env)
    calls, live, peak, lock = [], [0], [0], threading.Lock()

    def gen(client, row, **kw):
        with lock:
            live[0] += 1; peak[0] = max(peak[0], live[0])
        real_sleep(0.05)       # real, so overlap is observable
        with lock:
            live[0] -= 1; calls.append(row["ticker"])
        return {"verdict": "HOLD", "headline": "new", "model_used": "m",
                "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")}

    rev.get_gemini_api_key = lambda: "k"
    rev.llm_util.make_client = lambda key: object()
    rev.load_data_snapshot = lambda: SNAP
    rev.load_watchlists = lambda: WL
    rev.load_expert_views = lambda: store
    rev.save_expert_views = lambda s: None
    rev.load_fundamentals = lambda: {}
    rev.generate_expert_view = gen
    rev.active_alerts_for_prompt = lambda rows: None
    rev.alerts_text_for = lambda a, t: ""
    rev.main()
    return calls, peak[0]

now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
old = (datetime(2020, 1, 1)).strftime("%Y-%m-%d %H:%M")
def store_with_done():
    s = {t: {"verdict": "HOLD", "headline": "x", "model_used": "m", "as_of": old} for tks in WL.values() for t in tks}
    for t in ("A0", "A1", "B2"):
        s[t] = {**s[t], "as_of": now}          # already analysed this slot
    s["A3"] = {"verdict": "PENDING", "headline": "Analysis pending", "as_of": now}   # a failure stub: not done
    return s

ALL = sorted({t for tks in WL.values() for t in tks})
calls, peak = run({"GITHUB_EVENT_NAME": "schedule"}, store_with_done())
check(sorted(calls) == sorted(set(ALL) - {"A0", "A1", "B2"}),
      f"a scheduled full run skips the tickers done this slot and redoes the failure stub ({sorted(calls)})")
check(peak == rev.MAX_CONCURRENT_TICKERS == 3, f"tickers are analysed 3 at a time (peak {peak})")
check(len(calls) == len(set(calls)), "a ticker in two watchlists is analysed once")

calls, _ = run({"GITHUB_EVENT_NAME": "workflow_dispatch"}, store_with_done())
check(sorted(calls) == ALL, "a manual dispatch redoes everything")
calls, _ = run({"GITHUB_EVENT_NAME": "schedule", "REFRESH_MARKETS": "wl_b"}, store_with_done())
check(sorted(calls) == sorted(WL["wl_b"]), "a scoped run (the app's Re-analyze All) redoes its whole watchlist")
calls, _ = run({"GITHUB_EVENT_NAME": "schedule", "REFRESH_LIMIT": "2"}, store_with_done())
check(sorted(calls) == ["A0", "A1"], f"a limited smoke test takes the first N, done or not ({sorted(calls)})")

print("FAILURES:", fails); sys.exit(fails)
