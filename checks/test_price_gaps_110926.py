"""A demerger's overnight gap Yahoo left unadjusted is back-adjusted on NSE/BSE stocks (owner, 2026-11-09).

On a scheme's record date a watchlist stock's close went from 454.55 to 41.4; with the history unadjusted its
40-week EMA read 242.5 against a price near 70 and the screener said Downtrend / Exit / Caution while TheWrap's
Stage (TradingView, adjusted) said S2 Strong.

G1 A gap beyond 40% on an Indian ticker is found; every earlier price is scaled by it; later prices are untouched.
G2 The threshold: a move inside the band (the exchanges cap a day at 20%) is never adjusted.
G3 A bad tick (one bar 10x off, back within REVERSAL_BARS sessions) is not a corporate action.
G4 A US ticker is never adjusted (a biotech halves on a readout); a BSE ticker is.
G5 Two gaps multiply; a second pass changes nothing.
G6 The frame helpers: OHLC scaled and Volume kept; a yf.download frame adjusts only the Indian ticker and leaves
   the benchmark and the caller's frame alone; a breadth close frame adjusts per column.
G7 The wiring: fetch_snapshot computes its signals from adjusted bars (an uptrend across a gap reads Uptrend, not
   Downtrend / Exit); the perf job's stored closes, the breadth job's closes and Claude chat's live price history
   are adjusted too; the Vercel functions' watch list carries the module.

Offline: invented tickers and prices, stubbed downloads.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import price_gaps as pg

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


days = pd.bdate_range("2025-01-01", periods=10)


def series(values):
    return pd.Series([float(v) for v in values], index=days[:len(values)])


# --- G1 ---------------------------------------------------------------------------------------------
s = series([400, 420, 454.55, 41.4, 43.45, 45.6])
adj, found = pg.adjust_series(s, "ACME.NS")
ratio = 41.4 / 454.55
check(len(found) == 1 and found[0][0] == days[3] and abs(found[0][1] - ratio) < 1e-12,
      f"G1: the gap is found on its ex-date with its ratio ({found})")
check(np.allclose(adj.iloc[:3], s.iloc[:3] * ratio) and adj.iloc[3:].equals(s.iloc[3:]),
      "G1: every earlier close is scaled by the ratio, the ex-date and later ones untouched")
check(pg.gaps(adj) == [], "G1: the adjusted series has no gap left")

# --- G2 ---------------------------------------------------------------------------------------------
for move, want in ((0.80, 0), (1.20, 0), (0.61, 0), (1.63, 0), (0.59, 1), (1.70, 1)):
    got = len(pg.gaps(series([100, 100, 100 * move, 100 * move])))
    check(got == want, f"G2: a one-day move x{move} -> {'adjusted' if want else 'left alone'}")
check(pg.adjust_series(series([100, 120, 96, 115]), "ACME.NS")[1] == [], "G2: ordinary circuit moves are left alone")

# --- G3 ---------------------------------------------------------------------------------------------
tick = series([100, 101, 1010, 102, 103, 104])
check(pg.gaps(tick) == [] and pg.adjust_series(tick, "ACME.NS")[0].equals(tick),
      "G3: a bar 10x off that comes back the next session is a bad tick, not adjusted")
check(pg.gaps(series([100, 101, 10, 10.5, 11, 11.5, 12, 13, 100])) == [(days[2], 10 / 101), (days[8], 100 / 13)],
      "G3: a gap that does not reverse within REVERSAL_BARS sessions is a real one")

# --- G4 ---------------------------------------------------------------------------------------------
check(pg.adjust_series(s, "ACME")[1] == [] and pg.adjust_series(s, "ACME")[0].equals(s),
      "G4: a US ticker is never adjusted")
check(len(pg.adjust_series(s, "QUX.BO")[1]) == 1 and pg.is_indian("zed.ns"), "G4: BSE (and any case) is")

# --- G5 ---------------------------------------------------------------------------------------------
two = series([200, 200, 100, 100, 20, 20])       # x0.5 then x0.2
adj2, found2 = pg.adjust_series(two, "ACME.NS")
check([round(r, 3) for _d, r in found2] == [0.5, 0.2] and np.allclose(adj2.values, [20, 20, 20, 20, 20, 20]),
      f"G5: two gaps multiply ({list(adj2.values)})")
check(pg.adjust_series(adj2, "ACME.NS")[1] == [] and pg.adjust_series(adj, "ACME.NS")[0].equals(adj),
      "G5: a second pass changes nothing")

# --- G6 ---------------------------------------------------------------------------------------------
ohlc = pd.DataFrame({"Open": s * 1.01, "High": s * 1.02, "Low": s * 0.98, "Close": s, "Volume": 1000.0})
f_adj, f_found = pg.adjust_frame(ohlc, "ACME.NS")
check(len(f_found) == 1 and np.allclose(f_adj["High"].iloc[:3], ohlc["High"].iloc[:3] * ratio)
      and f_adj["Volume"].equals(ohlc["Volume"]) and ohlc["Close"].iloc[0] == 400,
      "G6: OHLC scaled, Volume kept, the caller's frame not modified")
bench = pd.DataFrame({"Open": 10.0, "High": 10.0, "Low": 10.0, "Close": series([10, 10, 3, 3, 3, 3]), "Volume": 1.0})
raw = pd.concat({"ACME.NS": ohlc, "^CRSLDX": bench}, axis=1)
out, which = pg.adjust_download(raw, ["ACME.NS", "^CRSLDX", "MISSING.NS"])
check(which == ["ACME.NS"] and np.allclose(out["ACME.NS"]["Close"].values, adj.values)
      and out["^CRSLDX"].equals(raw["^CRSLDX"]) and raw["ACME.NS"]["Close"].iloc[0] == 400,
      "G6: a download frame adjusts only the Indian ticker; benchmark and input untouched")
closes = pd.DataFrame({"ACME.NS": s, "ACME": s, "ZED.NS": series([1, 1, 1, 1, 1, 1])})
c_out, c_which = pg.adjust_closes_frame(closes)
check(c_which == ["ACME.NS"] and np.allclose(c_out["ACME.NS"].values, adj.values) and c_out["ACME"].equals(s),
      "G6: a breadth close frame adjusts each Indian column on its own")
withnan = s.copy(); withnan.iloc[1] = np.nan
check(len(pg.adjust_series(withnan, "ACME.NS")[1]) == 1, "G6: a blank day does not hide or invent a gap")

# --- G7 wiring --------------------------------------------------------------------------------------
import stock_data as sd

n = 520                                              # ~2 years of sessions, a steady uptrend
idx = pd.bdate_range("2024-11-01", periods=n)
base = np.linspace(100, 450, n)
gap_at = n - 20
price = base.copy()
price[gap_at:] = base[gap_at:] * 0.09                 # the scheme's record date, 20 sessions ago
rng = np.random.default_rng(7)
price = price * (1 + rng.normal(0, 0.004, n))


def ohlc_frame(p, vol=1e5):
    p = pd.Series(p, index=idx)
    return pd.DataFrame({"Open": p, "High": p * 1.01, "Low": p * 0.99, "Close": p, "Volume": vol})


fixture = pd.concat({"ACME.NS": ohlc_frame(price), "^CRSLDX": ohlc_frame(np.linspace(100, 110, n))}, axis=1)
saved = {k: getattr(sd, k) for k in ("_download_with_retries", "fill_unpublished_daily_bars", "_fetch_info_with_retry", "_safe_fetch")}
try:
    sd._download_with_retries = lambda t, p, **k: fixture.copy()
    sd.fill_unpublished_daily_bars = lambda r, t, **k: (r, [])
    sd._fetch_info_with_retry = lambda y, t: {}
    sd._safe_fetch = lambda fn, label="": None
    rows, _ = sd.fetch_snapshot(["ACME.NS"], benchmark="^CRSLDX", settings=dict(sd.DEFAULT_SETTINGS))
finally:
    for k, v in saved.items():
        setattr(sd, k, v)
row = rows[0] if rows else {}
check(row.get("trend") in ("Uptrend", "Strong Uptrend") and row.get("ta_rules") not in ("Exit", "Be Cautious"),
      f"G7: fetch_snapshot reads an uptrend across the gap as one (trend {row.get('trend')}, TA {row.get('ta_rules')})")
check(row.get("vstop_weekly_direction") == "Up", f"G7: ...and the weekly VStop points Up ({row.get('vstop_weekly_direction')})")

import refresh_dashboard_perf as rdp

orig_dl = rdp.yf.download
try:
    rdp.yf.download = lambda tickers, **k: pd.concat({"Close": pd.DataFrame({"ACME.NS": s, "ACME": s})}, axis=1)
    got = rdp._download_series(["ACME.NS", "ACME"])
finally:
    rdp.yf.download = orig_dl
check(np.allclose(got["ACME.NS"].values, adj.values) and got["ACME"].equals(s),
      "G7: the perf job stores adjusted closes for an Indian ticker (charts, watchlist breadth, Claude chat's prices)")

src_b = (REPO / "refresh_market_breadth.py").read_text()
i_concat = src_b.find("closes = pd.concat(all_data, axis=1).sort_index()")
i_adj = src_b.find("closes, _gapped = price_gaps.adjust_closes_frame(closes)")
i_math = src_b.find("unpublished = unpublished_tail(closes)")
check(0 < i_concat < i_adj < i_math, "G7: the breadth job adjusts its closes before any breadth maths")

import market_lists
import yfinance as yf

orig = yf.download
try:
    yf.download = lambda symbols, **k: pd.concat({"ACME.NS": ohlc, "ACME": ohlc}, axis=1)
    live = market_lists.yahoo_closes(["ACME.NS", "ACME"], 30)
finally:
    yf.download = orig
check([c for _d, c in live.get("ACME.NS", [])][:3] == [round(float(v), 4) for v in adj.iloc[:3]]
      and [c for _d, c in live.get("ACME", [])][0] == 400.0,
      "G7: Claude chat's live price history is adjusted for an Indian ticker only")
check(" price_gaps.py " in (REPO / "scripts/vercel-ignore.sh").read_text(),
      "G7: the Vercel functions redeploy when the module changes")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
