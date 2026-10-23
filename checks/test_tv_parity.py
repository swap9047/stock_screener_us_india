"""RSI, ATR, ADX and the weekly VStop against bar-by-bar ports of TradingView's Pine built-ins.

The app's indicators claim TradingView parity; this holds them to it on synthetic weekly series of
several lengths, including the short histories of recent listings where seeding conventions show
(2026-10-23: ATR seeded from bar 1 and VStop starting only once ATR existed both differed there).
The references are written from Pine's documented definitions -- ta.rma, ta.rsi, ta.atr, the DMI
script, volStop -- one bar at a time, independently of the vectorised code under test.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

import stock_data as sd

fails = 0
def check(ok, label):
    global fails; fails += not ok; print("PASS" if ok else "FAIL", label)

nan = math.nan
isnan = lambda v: isinstance(v, float) and math.isnan(v)


def pine_rma(x, n):          # ta.rma: SMA of the first n-bar window with no na, then alpha = 1/n
    out, prev = [nan] * len(x), None
    for i in range(len(x)):
        if prev is None:
            w = x[max(0, i - n + 1): i + 1]
            if i >= n - 1 and not any(isnan(v) for v in w):
                prev = sum(w) / n
                out[i] = prev
        else:
            prev = (x[i] + (n - 1) * prev) / n
            out[i] = prev
    return out


def pine_rsi(c, n=14):       # ta.rsi
    ch = [nan] + [c[i] - c[i - 1] for i in range(1, len(c))]
    up = pine_rma([nan if isnan(v) else max(v, 0) for v in ch], n)
    dn = pine_rma([nan if isnan(v) else max(-v, 0) for v in ch], n)
    return [nan if isnan(u) else 100 if d == 0 else 0 if u == 0 else 100 - 100 / (1 + u / d) for u, d in zip(up, dn)]


def pine_tr(h, l, c, handle_na):   # ta.tr(handle_na)
    first = (h[0] - l[0]) if handle_na else nan
    return [first] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]


def pine_atr(h, l, c, n=14):  # ta.atr = ta.rma(ta.tr(true), n)
    return pine_rma(pine_tr(h, l, c, True), n)


def pine_adx(h, l, c, n=14):  # the built-in DMI script's ADX
    up = [nan] + [h[i] - h[i - 1] for i in range(1, len(c))]
    dn = [nan] + [l[i - 1] - l[i] for i in range(1, len(c))]
    pdm = [nan] + [u if (u > d and u > 0) else 0.0 for u, d in zip(up[1:], dn[1:])]
    mdm = [nan] + [d if (d > u and d > 0) else 0.0 for u, d in zip(up[1:], dn[1:])]
    trr = pine_rma(pine_tr(h, l, c, False), n)
    p, m = pine_rma(pdm, n), pine_rma(mdm, n)
    pdi = [nan if isnan(t) else 100 * a / t for a, t in zip(p, trr)]
    mdi = [nan if isnan(t) else 100 * a / t for a, t in zip(m, trr)]
    dx = [nan if isnan(a) else abs(a - b) / ((a + b) or 1) for a, b in zip(pdi, mdi)]
    return [nan if isnan(v) else 100 * v for v in pine_rma(dx, n)]


def pine_volstop(h, l, c, n, factor):   # the built-in "Volatility Stop" (volStop), src = close
    atr, trp = pine_atr(h, l, c, n), pine_tr(h, l, c, False)
    mx = mn = c[0]; up = True; stop = nan; stops, dirs = [], []
    for i, src in enumerate(c):
        atr_m = atr[i] * factor if not isnan(atr[i]) else trp[i]          # nz(atr * factor, ta.tr)
        mx, mn = max(mx, src), min(mn, src)
        if isnan(stop) or isnan(atr_m):                                   # math.max/min with na -> na
            cand = nan
        else:
            cand = max(stop, mx - atr_m) if up else min(stop, mn + atr_m)
        stop = src if isnan(cand) else cand                               # nz(..., src)
        new_up = src - stop >= 0.0
        if new_up != up:
            mx = mn = src
            stop = mx - atr_m if new_up else mn + atr_m
        up = new_up
        stops.append(stop); dirs.append(1 if up else -1)
    return stops, dirs


def walk(n, seed):
    r = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(r.normal(0, 0.03, n)))
    o = c * np.exp(r.normal(0, 0.006, n))
    h = np.maximum(o, c) * np.exp(np.abs(r.normal(0, 0.012, n)))
    l = np.minimum(o, c) * np.exp(-np.abs(r.normal(0, 0.012, n)))
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1e6},
                        index=pd.date_range("2021-01-01", periods=n, freq="W-FRI"))


def same(a, b, tol=1e-9):
    return all((isnan(x) and isnan(y)) or (not isnan(x) and not isnan(y) and abs(x - y) <= tol * max(1, abs(y)))
               for x, y in zip(a, b))


L, F = sd.VSTOP_LENGTH, sd.VSTOP_FACTOR
for n in (30, 60, 120, 260):
    bad = {"rsi": 0, "atr": 0, "adx": 0, "vstop": 0, "dir": 0}
    for seed in range(25):
        df = walk(n, seed)
        h, l, c = df.High.tolist(), df.Low.tolist(), df.Close.tolist()
        bad["rsi"] += not same(sd.compute_rsi(df.Close).tolist(), pine_rsi(c))
        bad["atr"] += not same(sd.compute_atr(df).tolist(), pine_atr(h, l, c))
        bad["adx"] += not same(sd.compute_adx(df).tolist(), pine_adx(h, l, c))
        s_app, d_app = sd.compute_vstop_tv(df, L, F)
        s_tv, d_tv = pine_volstop(h, l, c, L, F)
        bad["vstop"] += not same(s_app.tolist(), s_tv)
        bad["dir"] += d_app.tolist() != d_tv
    for k in ("atr", "adx", "vstop", "dir"):
        check(bad[k] == 0, f"{n} weekly bars: {k} matches TradingView bar for bar on 25 series ({bad[k]} differ)")
    # RSI differs from Pine only where the price never moves (see below); random walks always move.
    check(bad["rsi"] == 0, f"{n} weekly bars: RSI matches TradingView bar for bar on 25 series ({bad['rsi']} differ)")

# A deliberate difference: TradingView's RSI reads 100 when the price has not moved at all
# (no losses, no gains). The app leaves it blank -- a suspended or untraded stock reading
# "RSI 100, overbought" would fire RSI > 70 alert rules on a stock nobody can trade.
check(math.isnan(sd.compute_rsi(pd.Series([100.0] * 40)).iloc[-1]),
      "a price that never moves has no RSI (TradingView says 100; deliberately blank here)")
check(sd.compute_rsi(pd.Series(np.arange(100, 140, dtype=float))).iloc[-1] == 100.0,
      "a price that only rises reads RSI 100, as TradingView's does")

print("FAILURES:", fails); sys.exit(fails)
