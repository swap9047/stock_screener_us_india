"""Back-adjust an Indian stock's prices across a corporate-action gap Yahoo left unadjusted (owner, 2026-11-09).

On a demerger or scheme of arrangement the listed stock's price drops overnight by the value that moved to the
new company, and Yahoo does not adjust the history before it (it does for splits and bonuses). On 2026-10-08 a
watchlist stock's close went from 454.55 to 41.4 on its scheme's record date: every average spanning the gap
was computed from the old prices -- its 40-week EMA read 242.5 against a price near 70 -- so Trend read
Downtrend, TA Rules Exit and Expert Take Caution, while TradingView (and TheWrap's Stage export, read from it)
adjusts the history and showed S2 with Strong momentum.

The rule, NSE and BSE only (`.NS` / `.BO`): a one-day close-to-close move beyond GAP_LOW / GAP_HIGH (40% down,
or the same ratio up) is a corporate action, never a trading move -- the exchanges' price bands stop a stock at
20% at most (an F&O stock's dynamic circuit moves in 10% steps over a session) -- and every price before it is
multiplied by the ratio, which is how TradingView adjusts a spin-off. The ratio includes that day's own trading
move, which the band keeps small. A US stock is left alone: a biotech can halve or treble on a trial readout.

A gap that reverses within REVERSAL_BARS sessions to within the band (a bad Yahoo tick: one bar 10x off, then
back) is not adjusted. An adjusted series has no gap left, so a second pass changes nothing.

pandas only; every job and the Vercel functions (market_lists) import it.
"""

import pandas as pd

GAP_LOW = 0.6
GAP_HIGH = 1 / GAP_LOW
REVERSAL_BARS = 5
PRICE_COLUMNS = ("Open", "High", "Low", "Close", "Adj Close")


def is_indian(ticker):
    return str(ticker).upper().endswith((".NS", ".BO"))


def gaps(closes):
    """[(ex_date, ratio)] of the corporate-action gaps in a close series, oldest first."""
    s = pd.Series(closes).dropna()
    s = s[s > 0]
    if len(s) < 2:
        return []
    ratio = (s / s.shift(1)).iloc[1:]
    pos = [(i + 1, float(r)) for i, r in enumerate(ratio.values) if r < GAP_LOW or r > GAP_HIGH]
    skip = set()
    for n, (i, r) in enumerate(pos):
        for j, r2 in pos[n + 1:]:
            if j - i > REVERSAL_BARS:
                break
            if j not in skip and GAP_LOW <= r * r2 <= GAP_HIGH:
                skip.update((i, j))
                break
    return [(s.index[i], r) for i, r in pos if i not in skip]


def factors(index, found):
    """A multiplier per row of `index`: the product of the ratios of every gap after that row's date."""
    f = pd.Series(1.0, index=index)
    for ex_date, r in found:
        f.loc[f.index < ex_date] *= r
    return f


def adjust_series(closes, ticker):
    """(closes adjusted, [(ex_date, ratio)]) -- unchanged for a non-Indian ticker or with no gap."""
    if not is_indian(ticker) or closes is None or len(closes) == 0:
        return closes, []
    found = gaps(closes)
    if not found:
        return closes, []
    return closes * factors(closes.index, found), found


def adjust_frame(df, ticker):
    """The same for one ticker's OHLC frame (Open/High/Low/Close/Adj Close scaled, Volume kept: a demerger does
    not change the share count). The gaps are found on Close."""
    if not is_indian(ticker) or df is None or df.empty or "Close" not in df.columns:
        return df, []
    found = gaps(df["Close"])
    if not found:
        return df, []
    f = factors(df.index, found)
    df = df.copy()
    for col in PRICE_COLUMNS:
        if col in df.columns:
            df[col] = df[col] * f
    return df, found


def adjust_download(raw, tickers):
    """A group_by="ticker" yf.download frame (columns (ticker, field)) with each Indian ticker's prices adjusted.
    Returns (raw, [tickers adjusted]). Not in place."""
    adjusted = []
    if raw is None or raw.empty or not isinstance(raw.columns, pd.MultiIndex):
        return raw, adjusted
    have = set(raw.columns.get_level_values(0))
    out = raw
    for t in tickers:
        if t not in have or not is_indian(t):
            continue
        new, found = adjust_frame(raw[t], t)
        if not found:
            continue
        if out is raw:
            out = raw.copy()
        for col in new.columns:
            out[(t, col)] = new[col]
        adjusted.append(t)
    return out, adjusted


def adjust_closes_frame(closes):
    """A close frame with one column per ticker (the breadth job's) with each Indian column adjusted.
    Returns (frame, [tickers adjusted]). Not in place."""
    adjusted = []
    if closes is None or closes.empty:
        return closes, adjusted
    out = closes
    for t in closes.columns:
        if not is_indian(t):
            continue
        col = closes[t]
        found = gaps(col)
        if not found:
            continue
        if out is closes:
            out = closes.copy()
        out[t] = col * factors(col.index, found)
        adjusted.append(t)
    return out, adjusted
