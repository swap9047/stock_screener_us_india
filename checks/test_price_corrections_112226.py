"""Listed price corrections and the suspect-break warning (owner, 2026-11-22).

On 2026-11-22 a watchlist stock's 52-week high read -38% (really about -13%): its 2:5 bonus (record date 2026-11-02) was
adjusted by Yahoo only for the bars from 2026-10-21 on, so the older bars stayed on the old price scale and the weekly
averages sat above the price (Downtrend / Exit / Caution). A one-session step of x5/7 is far inside the 40% gap rule
(price_gaps.py), so it was never seen. `price_adjustments.json` (the data repo) now lists such a break by hand.

C1  load_corrections: parsed; a bad entry, a bad file or no file is skipped and never raises
C2  a correction scales every bar before its date and no later one; it is reported like a gap
C3  it applies only while the break is there: a repaired series, a corrected one and a mismatching step are left alone
C4  it combines with the 40% gap rule (the ratios multiply); a US ticker listed in the file is corrected too
C5  the frame helpers (OHLC scaled, Volume before the date divided by the factor; the download frame; the breadth close frame)
C6  the wiring: fetch_snapshot's 52-week distance and trend, the perf job (and its carried-close rescale), refresh_data
S1  the suspect list: a step beyond SUSPECT_LOW / SUSPECT_HIGH inside the last 60 sessions, never a corrected one

Offline: invented tickers and prices, stubbed downloads, a temporary corrections file.
"""

import json
import math
import os
import sys
import tempfile
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


TMP = tempfile.mkdtemp(prefix="price_corrections_")
FILE = os.path.join(TMP, "price_adjustments.json")
pg.CORRECTIONS_FILE = FILE
F = 5 / 7


def write(obj):
    with open(FILE, "w") as fh:
        json.dump(obj, fh)
    os.utime(FILE, ns=(0, os.stat(FILE).st_mtime_ns + 1_000_000))   # a new mtime for the cache


days = pd.bdate_range("2026-10-12", periods=14)          # 12 Oct .. 29 Oct


def series(values, start=0):
    return pd.Series([float(v) for v in values], index=days[start:start + len(values)])


# --- C1 ---------------------------------------------------------------------------------------------
check(pg.load_corrections() == {}, "C1: no file -> no corrections")
write({"acme.ns": [{"before": "2026-10-20", "factor": F, "note": "2:5 bonus"},
                   {"before": "not a date", "factor": 0.5},
                   {"before": "2026-10-20", "factor": "x"},
                   {"before": "2026-10-20", "factor": -2},
                   {"before": "2026-10-20", "factor": 1.0},
                   {"factor": 0.5}, "junk", None],
       "BAD.NS": "not a list", "ZED.NS": [{"before": "2026-01-05", "factor": 0.5}]})
got = pg.load_corrections()
check(sorted(got) == ["ACME.NS", "ZED.NS"] and got["ACME.NS"] == [(pd.Timestamp("2026-10-20"), F)],
      f"C1: entries are read (ticker upper-cased); a bad date, factor 1, a negative or a junk entry is skipped ({got})")
open(FILE, "w").write("{ not json")
os.utime(FILE, ns=(0, os.stat(FILE).st_mtime_ns + 2_000_000))
check(pg.load_corrections() == {}, "C1: a corrupt file is ignored, never raised")
write(["a", "list"])
check(pg.load_corrections() == {}, "C1: a file that is not an object is ignored")

# --- C2 ---------------------------------------------------------------------------------------------
write({"ACME.NS": [{"before": "2026-10-20", "factor": F}]})
# 12-19 Oct on the old scale (x1.4), then the new scale; 20 Oct is a holiday in the series, as it was for the real stock
old = [700, 705, 695, 700, 700, 700]                                   # 12 .. 19 Oct
new = [488.8, 477.5, 448.4, 477.9, 461.8, 484.8, 497.1, 474.8]           # 20 .. 29 Oct
s = pd.Series(old + new, index=days[:14])
adj, found = pg.adjust_series(s, "ACME.NS")
before = s.index < pd.Timestamp("2026-10-20")
check(len(found) == 1 and found[0] == (pd.Timestamp("2026-10-20"), F), f"C2: the correction is reported like a gap ({found})")
check(np.allclose(adj[before].values, s[before].values * F) and adj[~before].equals(s[~before]),
      "C2: every bar before the date is scaled by the factor, the date's own bar and later ones are untouched")
check(math.isclose(adj[~before].iloc[0] / adj[before].iloc[-1], 488.8 / (700 * F), rel_tol=1e-9)
      and 0.95 < adj[~before].iloc[0] / adj[before].iloc[-1] < 1.01,
      "C2: the break is gone (the step across the date is an ordinary move)")
check(s.iloc[0] == 700, "C2: the input is not modified")

# --- C3 ---------------------------------------------------------------------------------------------
again, found2 = pg.adjust_series(adj, "ACME.NS")
check(found2 == [] and again.equals(adj), "C3: a second pass changes nothing (the break is gone, so it cannot apply twice)")
repaired = pd.Series([v * F for v in old] + new, index=days[:14])          # Yahoo fixed its history
check(pg.adjust_series(repaired, "ACME.NS")[1] == [] and pg.adjust_series(repaired, "ACME.NS")[0].equals(repaired),
      "C3: a series Yahoo has since repaired is left alone")
step = pd.Series([700] * 6 + [630] * 8, index=days[:14], dtype=float)       # a -10% step, not x5/7
check(pg.adjust_series(step, "ACME.NS")[1] == [], "C3: a step that does not match the factor is left alone")
near = pd.Series([700] * 6 + [700 * F * 0.90] * 8, index=days[:14], dtype=float)     # the factor and a -10% day
check(len(pg.adjust_series(near, "ACME.NS")[1]) == 1, "C3: the factor plus an ordinary day's move (inside the tolerance) still matches")
check(pg.adjust_series(s[s.index >= pd.Timestamp("2026-10-20")], "ACME.NS")[1] == [], "C3: a series that starts after the date has nothing before it")
check(pg.adjust_series(s[s.index < pd.Timestamp("2026-10-20")], "ACME.NS")[1] == [], "C3: a series that ends before the date has nothing after it")
check(pg.adjust_series(s, "OTHER.NS")[1] == [] and pg.adjust_series(s, "OTHER.NS")[0].equals(s),
      "C3: a ticker not in the file is untouched")
tz = s.copy()
tz.index = tz.index.tz_localize("Asia/Kolkata")
check(len(pg.adjust_series(tz, "ACME.NS")[1]) == 1, "C3: a timezone-aware index (Yahoo's) works")
withnan = s.copy(); withnan.iloc[2] = np.nan
check(len(pg.adjust_series(withnan, "ACME.NS")[1]) == 1, "C3: a blank day does not hide the break")

# --- C4 ---------------------------------------------------------------------------------------------
write({"ACME.NS": [{"before": "2026-10-20", "factor": F}], "USCO": [{"before": "2026-10-20", "factor": F}]})
# a demerger gap (-89%, 14 -> 15 Oct) that the 40% rule finds by itself, and the x5/7 break the file lists (19 -> 20 Oct)
combo = pd.Series([4000.0, 4100, 4200, 450, 455, 460, 330, 332, 328, 331, 330, 333, 334, 335], index=days)
adj, found = pg.adjust_series(combo, "ACME.NS")
check([a for a, _ in found] == [days[3], pd.Timestamp("2026-10-20")],
      f"C4: the 40% gap rule and the listed correction both apply, oldest first ({[str(a.date()) for a, _ in found]})")
check(np.isclose(adj.iloc[0], 4000 * F * 450 / 4200) and np.isclose(adj.iloc[3], 450 * F) and np.isclose(adj.iloc[5], 460 * F)
      and adj.iloc[6:].equals(combo.iloc[6:]),
      "C4: bars before both are scaled by both ratios, bars between them by the correction only, later bars not at all")
check(pg.gaps(adj) == [] and len(pg.manual_gaps(adj, "ACME.NS")) == 0, "C4: the result has neither a gap nor a break left")
us = pd.Series([140.0] * 6 + [100.0] * 8, index=days[:14])
usadj, usfound = pg.adjust_series(us, "USCO")
check(len(usfound) == 1 and np.allclose(usadj.iloc[:6].values, 140 * F),
      "C4: a US ticker is corrected when it is listed (the file is explicit)")
check(pg.adjust_series(us, "USOTHER")[1] == [] and pg.adjust_series(us, "USOTHER")[0].equals(us),
      "C4: ...and an unlisted US ticker is never touched")

# --- C5 ---------------------------------------------------------------------------------------------
write({"ACME.NS": [{"before": "2026-10-20", "factor": F}]})


def ohlc(p, vol=1000.0):
    p = pd.Series(p, index=days[:14]) if not isinstance(p, pd.Series) else p
    return pd.DataFrame({"Open": p, "High": p * 1.02, "Low": p * 0.98, "Close": p, "Volume": vol})


frame = ohlc(s)
out, found = pg.adjust_frame(frame, "ACME.NS")
check(np.allclose(out["High"][before].values, frame["High"][before].values * F) and out["High"][~before].equals(frame["High"][~before])
      and np.allclose(out["Volume"][before].values, 1000 / F) and (out["Volume"][~before] == 1000).all() and len(found) == 1,
      "C5: OHLC is scaled before the date, Volume before it is in new shares (/factor, owner's review 2026-11-22), later bars kept")
raw = pd.concat({"ACME.NS": frame, "^CRSLDX": ohlc(pd.Series(np.linspace(100, 110, 14), index=days[:14]))}, axis=1)
d_out, d_which = pg.adjust_download(raw, ["ACME.NS", "^CRSLDX"])
check(d_which == ["ACME.NS"] and np.allclose(d_out["ACME.NS"]["Close"][before].values, s[before].values * F)
      and d_out["^CRSLDX"].equals(raw["^CRSLDX"]) and raw["ACME.NS"]["Close"].iloc[0] == 700,
      "C5: the download frame adjusts only the listed ticker; the benchmark and the caller's frame are untouched")
closes = pd.DataFrame({"ACME.NS": s, "ZED.NS": pd.Series(1.0, index=days[:14])})
c_out, c_which = pg.adjust_closes_frame(closes)
check(c_which == ["ACME.NS"] and np.allclose(c_out["ACME.NS"].values, pg.adjust_series(s, "ACME.NS")[0].values),
      "C5: the breadth close frame adjusts the listed column")

# --- C6 wiring --------------------------------------------------------------------------------------
import stock_data as sd

n = 520
idx = pd.bdate_range("2024-11-01", periods=n)
base = np.linspace(100, 450, n)
k = n - 12                                              # the break, 12 sessions ago
old_scale = base.copy()
old_scale[:k] *= 1.4                                    # Yahoo left the older bars on the old price scale
rng = np.random.default_rng(11)
old_scale = old_scale * (1 + rng.normal(0, 0.003, n))
break_date = idx[k].strftime("%Y-%m-%d")


def snap_row(file_obj):
    p = pd.Series(old_scale, index=idx)
    fixture = pd.concat({"ACME.NS": pd.DataFrame({"Open": p, "High": p * 1.01, "Low": p * 0.99, "Close": p, "Volume": 1e5}),
                         "^CRSLDX": pd.DataFrame({"Open": base, "High": base, "Low": base, "Close": base, "Volume": 1e5},
                                                 index=idx)}, axis=1)
    keep = {k2: getattr(sd, k2) for k2 in ("_download_with_retries", "fill_unpublished_daily_bars", "_fetch_info_with_retry", "_safe_fetch")}
    try:
        sd._download_with_retries = lambda t, per, **kw: fixture.copy()
        sd.fill_unpublished_daily_bars = lambda r, t, **kw: (r, [])
        sd._fetch_info_with_retry = lambda y, t: {}
        sd._safe_fetch = lambda fn, label="": None
        rows, _ = sd.fetch_snapshot(["ACME.NS"], benchmark="^CRSLDX", settings=dict(sd.DEFAULT_SETTINGS))
    finally:
        for k2, v in keep.items():
            setattr(sd, k2, v)
    return rows[0] if rows else {}


write({})
plain = snap_row(None)
write({"ACME.NS": [{"before": break_date, "factor": F}]})
fixed = snap_row(None)
check(plain.get("week52_distance", 0) > 20, f"C6: without the correction the 52-week high is far off ({plain.get('week52_distance')})")
check(fixed.get("week52_distance", 99) < 3, f"C6: with it the stock reads at its high ({fixed.get('week52_distance')})")
check(fixed.get("trend") in ("Uptrend", "Strong Uptrend") and plain.get("trend") not in ("Strong Uptrend",),
      f"C6: and the trend is an uptrend, as the prices are ({plain.get('trend')} -> {fixed.get('trend')})")

import refresh_dashboard_perf as rdp

orig_dl = rdp.yf.download
try:
    rdp.yf.download = lambda tickers, **kw: pd.concat({"Close": pd.DataFrame({"ACME.NS": pd.Series(old_scale, index=idx)})}, axis=1)
    rdp.GAPS_FOUND.clear()
    got = rdp._download_series(["ACME.NS"])
    gaps_found = dict(rdp.GAPS_FOUND)
finally:
    rdp.yf.download = orig_dl
    rdp.GAPS_FOUND.clear()
check(np.allclose(got["ACME.NS"].values[:k], old_scale[:k] * F) and np.allclose(got["ACME.NS"].values[k:], old_scale[k:])
      and gaps_found.get("ACME.NS") == [(break_date, F)],
      "C6: the perf job stores corrected closes and records the correction (so a carried stored close is rescaled too)")
carried = {d: v for d, v in zip(idx.strftime("%Y-%m-%d"), old_scale)}
fresh = {d: v for d, v in zip(idx.strftime("%Y-%m-%d"), got["ACME.NS"].values)}
stale_day = idx[k - 2].strftime("%Y-%m-%d")
fresh_missing = {d: v for d, v in fresh.items() if d != stale_day}
merged = rdp.keep_recent_closes(fresh_missing, {stale_day: carried[stale_day]}, today=idx[k - 1].date())   # as of that week
check(stale_day in merged and abs(rdp.rescale_carried(merged, fresh_missing, gaps_found["ACME.NS"])[stale_day] - fresh[stale_day]) < 1e-3,
      "C6: a stored close carried into a day Yahoo blanked comes out on the corrected scale")

src = (REPO / "refresh_data.py").read_text()
check("price_gaps.suspect_tickers()" in src and "::warning::" in src.split("suspects = price_gaps.suspect_tickers()", 1)[1][:400]
      and "{suspects" not in src, "C6: refresh_data.py warns with a count and names no stock (the log is public)")
import github_sync

check("price_adjustments.json" in github_sync.PULLABLE_FILES and "price_adjustments.json" in github_sync.DATA_FILES,
      "C6: the app downloads the file too (a live refresh in the app corrects the same way)")

# --- S1 suspects ------------------------------------------------------------------------------------
write({"ACME.NS": [{"before": "2026-10-20", "factor": F}]})
pg.reset_suspects()
brk = pd.Series([700.0] * 40 + [488.0] * 20, index=pd.bdate_range("2026-09-01", periods=60))
pg.adjust_frame(ohlc(brk.set_axis(pd.bdate_range("2026-09-01", periods=60))), "ZED.NS")
check(pg.suspect_tickers() == ["ZED.NS"], "S1: a -30% step inside the last 60 sessions, adjusted by nothing, is a suspect")
pg.reset_suspects()
old_brk = pd.Series([700.0] * 10 + [488.0] * 90, index=pd.bdate_range("2026-05-01", periods=100))
pg.adjust_frame(ohlc(old_brk.set_axis(pd.bdate_range("2026-05-01", periods=100))), "ZED.NS")
check(pg.suspect_tickers() == [], "S1: the same step more than 60 sessions ago is not")
pg.adjust_frame(ohlc(pd.Series([700.0] * 40 + [640.0] * 20, index=pd.bdate_range("2026-09-01", periods=60))), "ZED.NS")
check(pg.suspect_tickers() == [], "S1: an ordinary -9% day is not")
us_brk = pd.Series([700.0] * 40 + [488.0] * 20, index=pd.bdate_range("2026-09-01", periods=60))
pg.adjust_frame(ohlc(us_brk), "USONLY")
check(pg.suspect_tickers() == [], "S1: only Indian tickers are watched")
write({"ZED.NS": [{"before": str(pd.bdate_range("2026-09-01", periods=60)[40].date()), "factor": 488 / 700}]})
pg.adjust_frame(ohlc(us_brk), "ZED.NS")
check(pg.suspect_tickers() == [], "S1: a step the listed correction removes is not a suspect")
pg.reset_suspects()

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
