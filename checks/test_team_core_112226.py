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

B: a mixed India + US list's breadth. Each day's denominator was the stocks
   with a close that day, and dashboard_perf carries India's forming bar from
   ~23:45 ET until the US prints, so the latest reading counted India alone
   (5 of 10 above -> 5 of 6, 83.3%). A stock whose whole exchange is shut on a
   row now counts at its own latest session there; one exchange's panel (every
   index) is unchanged, held here to an independent per-day count.

A: alerts.ALLOWED_HOURS (the hours the app's schedule picker offers) is the
   daily-alerts.yml gate's `slots`, parsed the way slot_gate.py parses them: an
   hour the gate never works is a rule that never fires.

Offline: invented tickers (ACME.NS) and frames, the download and yf.Ticker
replaced, no network.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import yaml  # noqa: E402

import alerts  # noqa: E402
import breadth_math as bm  # noqa: E402
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

# ---------------------------------------------------------------- B: mixed-list breadth


def own_sma_count(panel, day, window=200):
    """Independent per-day count: (above, total) over the stocks with a close
    on `day` and `window` closes of their own up to it."""
    above = total = 0
    for c in panel:
        own = panel[c].dropna()
        own = own[own.index <= day]
        if day not in own.index or len(own) < window:
            continue
        total += 1
        above += own.iloc[-1] > own.iloc[-window:].mean()
    return above, total


idx_b = pd.bdate_range("2025-01-01", "2026-11-20")
rng = np.random.default_rng(0)
cols = {}
for i in range(6):   # India, rising: above the SMA
    cols[f"ZIN{i}.NS"] = 100 * np.exp(np.cumsum(rng.normal(0.002, 0.01, len(idx_b))))
for i in range(4):   # US, falling: below it
    cols[f"ACM{i}"] = 100 * np.exp(np.cumsum(rng.normal(-0.002, 0.01, len(idx_b))))
mixed = pd.DataFrame(cols, index=idx_b)
day_b = bm.compute_breadth(mixed)
exp_a, exp_t = own_sma_count(mixed, idx_b[-1])
check((day_b["above"], day_b["total"]) == (exp_a, exp_t) and exp_t == 10,
      f"B1 a full day counts both markets ({day_b['above']}/{day_b['total']})")

# The next morning (ET): India's bar is in, the US has not traded yet.
nxt = idx_b[-1] + pd.Timedelta(days=3)
morning = pd.concat([mixed, pd.DataFrame(
    {c: [mixed[c].iloc[-1] * 1.001 if c.endswith(".NS") else np.nan] for c in mixed}, index=[nxt])])
b_m = bm.compute_breadth(morning)
check(b_m["total"] == 10 and b_m["above"] == day_b["above"],
      f"B1 an India-only latest row still counts the US stocks at their own last close "
      f"({b_m['above']}/{b_m['total']}, was India alone)")
check(b_m["history"][str(nxt.date())] == b_m["pct_above"],
      "B1 ...and the history's latest point says the same")
check(b_m["drawdown"] is not None and all(
      abs(b_m["drawdown"][k] - day_b["drawdown"][k]) <= 10.0 for k in b_m["drawdown"]),
      "B1 the drawdown shares are over all 10 stocks too (within one stock of the day before)")

# An India-only holiday inside the history: that row counts the India stocks too.
hol = mixed.copy()
hday = idx_b[-30]
hol.loc[hday, [c for c in hol if c.endswith(".NS")]] = np.nan
b_h = bm.compute_breadth(hol)
prev = str(idx_b[-31].date())
check(abs(b_h["history"][str(hday.date())] - b_h["history"][prev]) <= 10.0,
      f"B1 an India holiday in the history keeps both markets in its denominator "
      f"({b_h['history'][str(hday.date())]} vs {b_h['history'][prev]} the day before)")
only_us = own_sma_count(hol, hday)
check(only_us[1] == 4 and b_h["history"][str(hday.date())] != round(only_us[0] / only_us[1] * 100, 1),
      "B1 (which used to read the 4 US stocks alone)")

# A stock silent on a day its OWN exchange traded (a suspension) is not carried.
sus = mixed.copy()
sus.loc[idx_b[-1], "ZIN0.NS"] = np.nan
check(bm.compute_breadth(sus)["total"] == 9, "B1 a stock with no close on a day its exchange traded is left out")

# Carried at most CARRY_ROWS rows: an exchange shut longer drops out.
long = pd.concat([mixed, pd.DataFrame(
    {c: [np.nan] * (bm.CARRY_ROWS + 1) if c.endswith(".NS") else mixed[c].iloc[-1] * np.ones(bm.CARRY_ROWS + 1)
     for c in mixed}, index=pd.bdate_range(idx_b[-1] + pd.Timedelta(days=1), periods=bm.CARRY_ROWS + 1))])
check(bm.compute_breadth(long)["total"] == 4,
      f"B1 an exchange shut for more than {bm.CARRY_ROWS} rows drops out of the latest count")

# One exchange (every index, a single-market list): identical to the per-day
# count, gaps and all -- nothing is carried.
single = mixed[[c for c in mixed if c.endswith(".NS")]].copy()
single.loc[idx_b[-1], "ZIN1.NS"] = np.nan          # one stock silent on the last day
single.loc[idx_b[-5:-2], "ZIN2.NS"] = np.nan       # another silent a few days
single.loc[idx_b[-20], :] = np.nan                 # a row nobody traded
b_s = bm.compute_breadth(single)
check((b_s["above"], b_s["total"]) == own_sma_count(single, idx_b[-1]),
      f"B2 a single-market list's latest reading is the plain per-day count ({b_s['above']}/{b_s['total']})")
days_ok = all(b_s["history"][str(d.date())] == round(a / t * 100, 1)
              for d in idx_b[-40:] if str(d.date()) in b_s["history"]
              for a, t in [own_sma_count(single, d)])
check(days_ok and str(idx_b[-20].date()) not in b_s["history"],
      "B2 ...and every day of its history (a row nobody traded is still dropped)")
check(bm.calendar_group("ZED.NS") == bm.calendar_group("ZED.BO") == "IN"
      and bm.calendar_group("ACME") == bm.calendar_group("ACME.B") == "US",
      "B3 calendar groups by Yahoo suffix: .NS/.BO India, anything else the US")

# ---------------------------------------------------------------- A: alert hours vs the gate
wf = yaml.safe_load(open(REPO / ".github/workflows/daily-alerts.yml"))
gates = [st for job in wf["jobs"].values() for st in job.get("steps", [])
         if st.get("uses") == "./.github/actions/slot-gate"]
check(len(gates) == 1, f"A1 daily-alerts.yml has one slot-gate step ({len(gates)})")
slots = sorted(int(h) for h in str(gates[0]["with"]["slots"]).replace(" ", "").split(",") if h) if gates else None
check(slots == sorted(alerts.ALLOWED_HOURS),
      f"A1 alerts.ALLOWED_HOURS {sorted(alerts.ALLOWED_HOURS)} is the gate's slots {slots}")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
