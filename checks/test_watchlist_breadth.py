"""Per-watchlist breadth view (2026-10-14) and the breadth maths it shares with
the index-breadth job.

W1 breadth_math.compute_breadth: % above the 200-day SMA, % at 52-week highs and
   lows, each over only the stocks that have the measure, per day.
W2 refresh_market_breadth.calculate_breadth computes through it: same numbers.
W3 assemble_closes builds one panel from dashboard_perf.json's per-watchlist
   blocks: a ticker in two blocks appears once, a benchmark is found in another
   watchlist's block, a ticker with no series is reported.
W4 the app view: watchlist + group dropdown, a benchmark per watchlist, 6 years
   of performance data so the 5-year view is not blanked by the warmup.

Offline: invented tickers, no network, no data files.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import breadth_math as bm
import refresh_market_breadth as rmb

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- W1 ------------------------------------------------------------------------------
idx = pd.bdate_range("2025-01-01", periods=400)
up = pd.Series(np.linspace(100, 200, 400), index=idx)      # above its SMA, at a 52w high
down = pd.Series(np.linspace(200, 100, 400), index=idx)    # below its SMA, at a 52w low
new = pd.Series(np.nan, index=idx)
new.iloc[-150:] = np.linspace(100, 300, 150)               # too new for either measure
panel = pd.DataFrame({"ACME": up, "BOLT": up * 1.5, "CORE": up * 2, "DUSK": down, "NEWCO": new})
b = bm.compute_breadth(panel)
last = str(idx[-1].date())
check(b["total"] == 4 and b["above"] == 3 and b["pct_above"] == 75.0,
      f"W1: 3 of the 4 stocks with a 200-day SMA are above it; the 150-day listing is not counted "
      f"({b['above']}/{b['total']})")
check(b["history"][last] == 75.0 and b["highs_history"][last] == 75.0 and b["lows_history"][last] == 25.0,
      f"W1: highs 3 of 4, lows 1 of 4 on the last day ({b['highs_history'][last]}, {b['lows_history'][last]})")
check(min(b["history"]) == str(idx[252].date()), "W1: the first 252 rows are warmup")

# the 50-day (2026-10-15): the 150-day listing HAS 50 sessions, so it counts there
check(b["pct_above_50"] == 80.0 and b["history_50"][last] == 80.0,
      f"W1: % above the 50-day: 4 of 5 (the new listing qualifies for 50 days, not 200) ({b['pct_above_50']})")
check(set(b["history_50"]) <= set(b["history"]), "W1: the 50-day series is on the same days as the 200-day")
tiny = panel.copy()
tiny["FRESH"] = np.nan
tiny.loc[idx[-30:], "FRESH"] = np.linspace(1, 9, 30)      # 30 sessions: under 50
check(bm.compute_breadth(tiny)["pct_above_50"] == 80.0, "W1: a 30-session listing is left out of the 50-day too")

mixed = panel.copy()
mixed["ZED.NS"] = up * 3
mixed.loc[idx[-1], "ZED.NS"] = np.nan          # its exchange shut on the last day
b2 = bm.compute_breadth(mixed)
check(b2["total"] == 4 and b2["history"][last] == 75.0,
      f"W1: a stock with no close that day (its exchange shut) is left out of that day, not counted below "
      f"({b2['total']}, {b2['history'][last]})")
check(b2["history"][str(idx[-2].date())] == 80.0, "W1: ...and counted the day before (4 of 5 above)")

since = idx[-10]
b3 = bm.compute_breadth(panel, since=since)
check(min(b3["history"]) == str(since.date()) and len(b3["history"]) == 10, "W1: since= cuts the history")
check(bm.compute_breadth(pd.DataFrame()) is None, "W1: an empty panel -> None")

# --- W2 ------------------------------------------------------------------------------
def dl(t, **k):
    names = t if isinstance(t, list) else [t]
    df = panel[names].copy()
    df.columns = pd.MultiIndex.from_tuples([("Close", n) for n in names], names=["Price", "Ticker"])
    return df
saved = (rmb.yf.download, rmb.time.sleep)
try:
    rmb.yf.download, rmb.time.sleep = dl, (lambda s: None)
    job = rmb.calculate_breadth(list(panel.columns), "Test", "America/New_York", (16, 0))
finally:
    rmb.yf.download, rmb.time.sleep = saved
shared = bm.compute_breadth(panel, since=pd.Timestamp.now() - pd.DateOffset(years=5))
check(all(job[k] == shared[k] for k in ("history", "highs_history", "lows_history", "above", "total", "pct_above")),
      "W2: the index-breadth job and the watchlist view compute the same numbers")
check("compute_breadth" in (REPO / "refresh_market_breadth.py").read_text(), "W2: the job calls breadth_math")

# --- W3 ------------------------------------------------------------------------------
perf = {
    "us_one": {"ACME": {"2026-10-13": 10.0, "2026-10-14": 11.0}, "SPY": {"2026-10-13": 500.0, "2026-10-14": 501.0}},
    "us_two": {"ACME": {"2026-10-13": 99.0}, "BOLT": {"2026-10-12": 5.0, "2026-10-14": 6.0}},
    "india_one": {"ZED.NS": {"2026-10-14": 70.0}, "^CRSLDX": {"2026-10-14": 21000.0}},
}
p, missing = bm.assemble_closes(perf, ["ACME", "BOLT", "ACME", "GHOST", "ZED.NS"])
check(list(p.columns) == ["ACME", "BOLT", "ZED.NS"] and missing == ["GHOST"],
      f"W3: a repeated ticker appears once, in order; one with no series is reported ({list(p.columns)}, {missing})")
check(p.loc["2026-10-13", "ACME"] == 10.0, "W3: a ticker's series comes from the first block that has it")
check(list(p.index.strftime("%Y-%m-%d")) == ["2026-10-12", "2026-10-13", "2026-10-14"]
      and np.isnan(p.loc["2026-10-13", "BOLT"]), "W3: one sorted calendar, NaN where a ticker has no close")
bench, _ = bm.assemble_closes(perf, ["^CRSLDX"])
check(list(bench.columns) == ["^CRSLDX"], "W3: the benchmark is found in another watchlist's block")
empty, missing = bm.assemble_closes({}, ["ACME"])
check(empty.empty and missing == ["ACME"], "W3: nothing stored -> an empty panel, everything missing")

# --- W4 ------------------------------------------------------------------------------
APP = (REPO / "app.py").read_text()
check('key=f"breadth_watchlist_{side}"' in APP and "_wb_options = list(_wb_labels)" in APP
      and "_wb_labels.update(combined_display_label_by_key)" in APP,
      "W4: each side's dropdown offers every watchlist and the combined groups")
check('_watchlist_side("left", "india_invested", col_left)' in APP
      and '_watchlist_side("right", "us_invested", col_right)' in APP,
      "W4: split in two -- India Invested on the left, US Invested on the right by default (2026-10-15)")
check('key=f"breadth_benchmark_{side}_{wb_key}"' in APP and "index=0 if india_share > 0.5 else 1" in APP
      and '"Nifty 500": ("^CRSLDX", "INDIA"), "S&P 500": ("SPY", "US")' in APP,
      "W4: a benchmark per side and watchlist, defaulting to the list's exchange (Nifty 500 / S&P 500)")
check("compute_breadth(closes, since=_wb_cutoff)" in APP and "assemble_closes(_perf_markets, tickers)" in APP,
      "W4: the view computes through breadth_math, from dashboard_perf.json")
check('"breadth_watchlist_left", "breadth_watchlist_right",' in APP and 'startswith("breadth_benchmark_")' in APP,
      "W4: the view's choices survive leaving the News tab")
PERF = (REPO / "refresh_dashboard_perf.py").read_text()
check('PERIOD = "6y"' in PERF and 'period="5y"' not in PERF,
      "W4: performance data is 6 years, so the 5-year view keeps its first year after the warmup")

import refresh_dashboard_perf as rdp
from datetime import date
keep = rdp.keep_recent_tail
new = {"2026-10-12": 1.0, "2026-10-13": 2.0}
old = {"2026-10-12": 0.9, "2026-10-13": 1.9, "2026-10-14": 3.0}
check(keep(new, old, today=date(2026, 10, 15)) == {"2026-10-12": 1.0, "2026-10-13": 2.0, "2026-10-14": 3.0},
      "W4: a download ending before the stored series keeps the stored latest day (Yahoo blanked it)")
check(keep({"2026-10-14": 3.1}, old, today=date(2026, 10, 15)) == {"2026-10-14": 3.1},
      "W4: a download that has the day replaces the stored close (no carry-over)")
check(keep({"2026-01-05": 1.0}, {"2026-01-05": 1.0, "2026-01-06": 1.1}, today=date(2026, 10, 15)) == {"2026-01-05": 1.0},
      "W4: an old stored tail (a delisted ticker) is not carried")
check(keep({}, old) == {} and keep(new, None) == new, "W4: nothing to merge -> the download as is")

# --- dashboard_perf.json "benchmarks" (2026-10-15: SPY, QQQ, ^CRSLDX once) ------------
import json, tempfile, os
def run_perf(previous, fail=()):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "dashboard_perf.json")
    if previous is not None:
        Path(path).write_text(json.dumps(previous))
    saved = (rdp.OUT_FILE, rdp._download_series, rdp.load_watchlists, rdp.load_settings, rdp.get_benchmarks)
    days = pd.bdate_range("2026-10-08", "2026-10-14")
    rdp.OUT_FILE = path
    rdp._download_series = lambda tickers: {t: pd.Series(range(1, len(days) + 1), index=days, dtype=float)
                                            for t in tickers if t not in fail}
    rdp.load_watchlists = lambda: {"us_one": ["ACME", "BOLT"]}
    rdp.load_settings = lambda: {}
    rdp.get_benchmarks = lambda settings=None: {"us_one": "SPY"}
    try:
        rdp.main()
        return json.loads(Path(path).read_text())
    finally:
        rdp.OUT_FILE, rdp._download_series, rdp.load_watchlists, rdp.load_settings, rdp.get_benchmarks = saved
out = run_perf(None)
check(sorted(out["benchmarks"]) == ["QQQ", "SPY", "^CRSLDX"] and out["status"]["benchmarks"] == "ok"
      and max(out["benchmarks"]["QQQ"]) == "2026-10-14",
      f"perf: every selectable index is stored once under benchmarks ({sorted(out['benchmarks'])})")
check(sorted(out["markets"]["us_one"]) == ["ACME", "BOLT", "SPY"], "perf: market blocks are unchanged (own benchmark kept)")
prev = {"markets": {}, "benchmarks": {"QQQ": {"2026-10-13": 9.0}}}
out = run_perf(prev, fail=("QQQ",))
check(out["benchmarks"]["QQQ"] == {"2026-10-13": 9.0} and out["status"]["benchmarks"] == "failed",
      "perf: a failed benchmark download keeps the stored series, and says so")

APP2 = (REPO / "app.py").read_text()
check('"Nasdaq 100": ("NASDAQ100", "QQQ")' in APP2 and 'key="breadth_us_index"' in APP2,
      "app: the US column switches between S&P 500 and Nasdaq 100")
check('"Nasdaq 100": ("QQQ", "NASDAQ100")' in APP2, "app: the watchlist view offers Nasdaq 100 as a benchmark")
check('perf_data.get("benchmarks")' in APP2 and APP2.count('perf_data.get("benchmarks")') >= 2,
      "app: both charts take a benchmark the market block lacks from dashboard_perf.json's benchmarks")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
