"""Market breadth fixes from the 2026-10-04 review.

B1 Every slot re-downloaded both markets (~1000 tickers x 6 years, ~53 min),
   though each slot has only one new close: India's by the 10 AM ET slot, the
   US's by 10 PM. A market whose latest completed session is already stored is
   now skipped (status "unchanged"), checked with one index download.
B2 Saturday's two slots fetched nothing new: the gate runs MON-FRI.
B3 The same 2 Nifty symbols failed every run and cost ~8 min of retry waits.
   Retries run only when more than 1% of an index failed.
B4 The 5-year history uses TODAY's index members, so the past looks stronger
   than it was; the chart says so.

Offline: stubbed downloads, no network, no data files.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import refresh_market_breadth as rmb

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def fn(name):
    f = getattr(rmb, name, None)
    if f is None:
        check(False, f"refresh_market_breadth.{name} exists")
    return f


# --- B1 is there a new session? ------------------------------------------------------
needs = fn("needs_refresh")
if needs:
    block = {"history": {"2026-09-30": 42.2, "2026-10-01": 38.6}}
    check(needs(None, "2026-10-01"), "B1: no stored block -> refresh")
    check(needs(block, None), "B1: the session check failed -> refresh (never skip blind)")
    check(needs(block, "2026-10-02"), "B1: a newer completed session -> refresh")
    check(not needs(block, "2026-10-01"), "B1: the latest session is already stored -> skip")
    check(needs({"history": {}}, "2026-10-01"), "B1: an empty history -> refresh")

latest = fn("latest_completed_session")
if latest:
    idx = pd.to_datetime(["2026-09-30", "2026-10-01", "2026-10-02"]).tz_localize("America/New_York")
    closes = pd.Series([1.0, 2.0, 3.0], index=idx, name="SPY")
    got = latest("SPY", "America/New_York", (16, 0),
                 download=lambda t: pd.DataFrame({"Close": closes}),
                 now=pd.Timestamp("2026-10-02 22:00", tz="America/New_York"))
    check(got == "2026-10-02", f"B1: after the US close, today's session is complete ({got})")
    got = latest("SPY", "America/New_York", (16, 0),
                 download=lambda t: pd.DataFrame({"Close": closes}),
                 now=pd.Timestamp("2026-10-02 11:00", tz="America/New_York"))
    check(got == "2026-10-01", f"B1: mid-session, today's bar is still forming ({got})")

    def boom(t):
        raise RuntimeError("throttled")
    check(latest("SPY", "America/New_York", (16, 0), download=boom) is None,
          "B1: a failed check returns None (and the market is refreshed)")

# main(): an unchanged market is not downloaded; a changed one is
calls = []
with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "market_breadth.json")
    stored = {"as_of": "2026-10-03 17:00", "markets": {
        "US": {"as_of": "2026-10-03 17:00", "total": 501, "history": {"2026-10-02": 42.3}},
        "INDIA": {"as_of": "2026-10-03 17:00", "total": 490, "history": {"2026-09-30": 42.2}}},
        "status": {}}
    Path(path).write_text(json.dumps(stored))
    saved = (rmb.BREADTH_FILE, rmb.get_sp500_tickers, rmb.get_nifty500_tickers, rmb.calculate_breadth,
             getattr(rmb, "latest_completed_session", None))
    rmb.BREADTH_FILE = path
    rmb.get_sp500_tickers = lambda: ["A"] * 10
    rmb.get_nifty500_tickers = lambda: ["B.NS"] * 10
    rmb.calculate_breadth = lambda tickers, label, tz, hhmm: (calls.append(label) or
                                                              {"total": 9, "pct_above": 50.0, "history": {"2026-10-01": 50.0}})
    rmb.latest_completed_session = lambda ticker, tz, hhmm, **k: {"SPY": "2026-10-02", "^CRSLDX": "2026-10-01"}[ticker]
    try:
        rmb.main()
        out = json.loads(Path(path).read_text())
    finally:
        (rmb.BREADTH_FILE, rmb.get_sp500_tickers, rmb.get_nifty500_tickers, rmb.calculate_breadth, _l) = saved
        if _l:
            rmb.latest_completed_session = _l
check(calls == ["Nifty 500"], f"B1: only the market with a new session is downloaded ({calls})")
check(out["status"] == {"US": "unchanged", "INDIA": "ok"}, f"B1: statuses say so ({out['status']})")
check(out["markets"]["US"]["history"] == {"2026-10-02": 42.3} and out["markets"]["US"]["as_of"] == out["as_of"],
      "B1: the unchanged block is kept, and marked current (so the app does not call it stale)")

# --- B2 / B4 / workflow ---------------------------------------------------------------
WF = (REPO / ".github/workflows/market-breadth.yml").read_text()
check('days: "MON,TUE,WED,THU,FRI"' in WF, "B2: the breadth slots run Monday to Friday")
check('("ok", "unchanged")' in WF, "B1: the verify step accepts an unchanged market")
APP = (REPO / "app.py").read_text()
check("survivorship" in APP.lower(), "B4: the breadth charts note the survivorship bias")

# --- B3 retries only when worth it ----------------------------------------------------
retry = fn("worth_retrying")
if retry:
    check(not retry(2, 501), "B3: 2 of 501 failed (0.4%) -> no 8-minute retry waits")
    check(retry(6, 501), "B3: 6 of 501 (1.2%) -> retry")
    check(not retry(0, 501), "B3: nothing failed -> nothing to retry")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
