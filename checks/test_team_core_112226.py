"""Core review fixes of 2026-11-22.

R: price levels and the below_* / crossed_* flags. The levels (last close, the
   weekly EMAs, the daily SMAs, VStop, 52-week high / low) were rounded to 1
   decimal and the flags then compared the unrounded close with the ROUNDED
   averages, so on a ~5-unit stock a close of 5.0197 under a true 200-day SMA of
   5.0396 read as not below (5.0197 < 5.0 is False), not crossed, and a user
   rule "Last Close < 200 DSMA" compared 5.0 < 5.0. The flags now come from the
   unrounded series (expected values recomputed here with pandas, not read from
   the module), and the stored levels keep enough precision that a rule
   comparing two of them agrees.

D: the indicator download's price basis -- split-adjusted, dividends NOT
   back-adjusted (auto_adjust=False), as TradingView's default chart and the
   breadth and performance jobs; every indicator reads Close.

Offline: invented tickers (ACME.NS) and frames, the download and yf.Ticker
replaced, no network.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import filters  # noqa: E402
import stock_data as sd  # noqa: E402

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def ohlc(close, idx, vol=100000.0):
    close = np.asarray(close, float)
    return pd.DataFrame({"Open": close, "High": close * 1.001, "Low": close * 0.999,
                         "Close": close, "Volume": vol}, index=idx)


def run(frames, bench):
    """fetch_snapshot over invented frames: the download returns them, every
    yf.Ticker call fails (no company details), no sleeping, no 5-minute bars."""
    raw = pd.concat(frames, axis=1)
    saved = (sd._download_with_retries, sd.yf.Ticker, sd.time.sleep, sd.fill_unpublished_daily_bars)

    class Offline:
        def __init__(self, t):
            raise RuntimeError("offline")

    sd._download_with_retries = lambda all_t, period: raw.copy()
    sd.yf.Ticker = Offline
    sd.time.sleep = lambda s: None
    sd.fill_unpublished_daily_bars = lambda raw, t, now=None, download=None: (raw, [])
    try:
        res, _ = sd.fetch_snapshot([t for t in frames if t != bench], benchmark=bench,
                                   settings=dict(sd.DEFAULT_SETTINGS))
    finally:
        sd._download_with_retries, sd.yf.Ticker, sd.time.sleep, sd.fill_unpublished_daily_bars = saved
    return {r["ticker"]: r for r in res}


# ---------------------------------------------------------------- R0 round_price
check(sd.round_price(5.0197) == 5.02, "R0 a 5-unit level keeps 3 decimals (4 significant figures)")
check(sd.round_price(0.51234) == 0.5123, "R0 a sub-1 level keeps 4 significant figures")
check(sd.round_price(1234.567) == 1234.57, "R0 a 4-digit level keeps 2 decimals")
check(sd.round_price(None) is None and sd.round_price(0.0) == 0.0, "R0 None and 0 pass through")

# ---------------------------------------------------------------- R1 the low-price scenario
# A ~5-unit stock drifting up 5.03 -> 5.04 for four years, then one session down
# to 5.02: under every average (each about 5.039-5.04), every average rounds to
# 5.0 at 1 decimal, and the session before sat above every one (an uptrend), so
# each flag is a real "below" and a real "crossed below". The last day is a
# Friday, so the weekly bar is the completed week.
idx = pd.bdate_range(end="2026-11-13", periods=1000)
n = len(idx)
c = 5.03 + 0.01 * np.linspace(0, 1, n)
c[-1] = 5.02
bench = ohlc(np.linspace(100, 150, n), idx)
row = run({"ACME.NS": ohlc(c, idx), "^CRSLDX": bench}, "^CRSLDX")["ACME.NS"]

close = pd.Series(c, index=idx)
weekly = close.resample("W-FRI").last().dropna()
w_periods = dict(zip(("10", "20", "40"), sd.DEFAULT_SETTINGS["ema_weekly"]))
d_periods = dict(zip(("10_daily", "50", "200"), sd.DEFAULT_SETTINGS["ema_daily"]))

expect = {}
for suffix, span in w_periods.items():
    ema = weekly.ewm(span=span, adjust=False).mean()
    expect[suffix] = (float(ema.iloc[-1]), float(ema.iloc[-2]), float(weekly.iloc[-1]), float(weekly.iloc[-2]))
for suffix, win in d_periods.items():
    sma = close.rolling(win, min_periods=1).mean()
    expect[suffix] = (float(sma.iloc[-1]), float(sma.iloc[-2]), float(close.iloc[-1]), float(close.iloc[-2]))

meaningful = all(round(e[0], 1) <= e[2] < e[0] and e[3] >= e[1] for e in expect.values())
check(meaningful, "R1 the scenario is one 1-decimal rounding hid: close in [round(avg,1), avg), above it the bar before")

for suffix, (cur, prev, last, prev_last) in expect.items():
    below = last < cur
    check(row.get(f"below_{suffix}") is below, f"R1 below_{suffix} is {below} (unrounded close vs unrounded average)")
    if suffix in ("10", "40", "10_daily", "50", "200"):
        crossed = prev_last >= prev and last < cur
        check(row.get(f"crossed_below_{suffix}") is crossed,
              f"R1 crossed_below_{suffix} is {crossed}")
        check(row.get(f"crossed_above_{suffix}") is False, f"R1 crossed_above_{suffix} stays False")

key = {"10": "ema10", "20": "ema20", "40": "ema40", "10_daily": "ema10_daily", "50": "ema50", "200": "ema200"}
for suffix, field in key.items():
    stored = row.get(field)
    check(stored is not None and abs(stored - expect[suffix][0]) < 0.001,
          f"R1 {field} is stored within 0.001 of the true average ({stored} vs {expect[suffix][0]:.4f})")
    check(row["last_close"] < stored, f"R1 the stored last close is below the stored {field}")

check(row["last_close"] == 5.02, "R1 last_close is stored as 5.02, not 5.0")

# The user rule from the review: "Last Close < 200 DSMA", through the shared engine.
rule = {"metric_a": "last_close", "operator": "<", "compare_type": "metric", "metric_b": "ema200"}
check(filters.passes_filter(row, rule) is True, "R1 the rule 'Last Close < 200 DSMA' matches")

# 52-week high: stored to 4 significant figures, the distance from the unrounded high.
hi = float((close * 1.001).tail(252).max())
check(abs(row["week52_high"] - hi) < 0.001, "R1 week52_high is stored within 0.001 of the true high")
check(row["week52_distance"] == round(max(0.0, (hi - c[-1]) / hi * 100), 1),
      "R1 week52_distance is measured from the unrounded high")

# ---------------------------------------------------------------- R2 a false "crossed above"
# The mirror image: a slow downtrend 5.06 -> 5.045, then one session UP to 5.03
# -- still under every average (~5.045-5.05), which round to 5.0. The old test
# (close >= the rounded 5.0) called that a cross above every average.
c2 = 5.06 - 0.015 * np.linspace(0, 1, n)
c2[-2] = 5.02
c2[-1] = 5.03
row2 = run({"ACME.NS": ohlc(c2, idx), "^CRSLDX": bench}, "^CRSLDX")["ACME.NS"]
close2 = pd.Series(c2, index=idx)
for suffix, win in (("50", d_periods["50"]), ("200", d_periods["200"])):
    sma = close2.rolling(win, min_periods=1).mean()
    real_above = c2[-2] < sma.iloc[-2] and c2[-1] >= sma.iloc[-1]
    check(not real_above and round(float(sma.iloc[-1]), 1) <= c2[-1],
          f"R2 scenario: close under the {win}-day SMA but over its 1-decimal value")
    check(row2.get(f"crossed_above_{suffix}") is False, f"R2 crossed_above_{suffix} is not set falsely")
    check(row2.get(f"below_{suffix}") is True, f"R2 below_{suffix} is True")

# ---------------------------------------------------------------- D: the price basis
# Owner, 2026-11-22: the indicator download is split-adjusted only (auto_adjust=False),
# as TradingView's default chart and the breadth / performance jobs; every indicator
# reads Close, never the "Adj Close" that download also carries.
calls = []
idx_d = pd.bdate_range(end="2026-11-13", periods=400)
base = 100 + np.arange(len(idx_d)) * 0.1
frame = pd.concat({t: ohlc(base * k, idx_d) for t, k in (("ACME.NS", 1.0), ("^CRSLDX", 2.0))}, axis=1)
for t in ("ACME.NS", "^CRSLDX"):
    frame[(t, "Adj Close")] = frame[(t, "Close")] * 0.5   # a basis nothing may read
frame = frame.sort_index(axis=1)


def fake_download(*args, **kwargs):
    calls.append(kwargs)
    return frame.copy()


saved_dl, saved_ticker = sd.yf.download, sd.yf.Ticker
saved_sleep, saved_fill = sd.time.sleep, sd.fill_unpublished_daily_bars


class _Offline:
    def __init__(self, t):
        raise RuntimeError("offline")


sd.yf.download, sd.yf.Ticker, sd.time.sleep = fake_download, _Offline, (lambda s: None)
sd.fill_unpublished_daily_bars = lambda raw, t, now=None, download=None: (raw, [])
try:
    res_d, _ = sd.fetch_snapshot(["ACME.NS"], benchmark="^CRSLDX", settings=dict(sd.DEFAULT_SETTINGS))
    sd._download_intraday(["ACME.NS"])
finally:
    sd.yf.download, sd.yf.Ticker = saved_dl, saved_ticker
    sd.time.sleep, sd.fill_unpublished_daily_bars = saved_sleep, saved_fill

check(len(calls) == 2 and all(c.get("auto_adjust") is False for c in calls),
      "D1 the daily and the 5-minute downloads both ask for auto_adjust=False")
check(calls and calls[0].get("interval") == "1d" and calls[-1].get("interval") == "5m",
      "D1 (the two calls captured are the daily and the intraday one)")
row_d = res_d[0] if res_d else {}
check(row_d.get("last_close") == sd.round_price(base[-1]), "D2 last_close is the Close column, not Adj Close")
sma200 = float(pd.Series(base).rolling(200).mean().iloc[-1])
check(row_d.get("ema200") == sd.round_price(sma200), "D2 the 200-day SMA is over Close")
check(row_d.get("week52_high") == sd.round_price(float((base * 1.001)[-252:].max())),
      "D2 the 52-week high is over High (not scaled by Adj Close)")
check(row_d.get("rs_daily") is not None and abs(row_d["rs_daily"]) < 1.0,
      "D2 RS vs a benchmark moving in step is ~0 (both read Close)")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
