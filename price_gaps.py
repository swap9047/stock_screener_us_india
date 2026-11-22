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

CORRECTIONS (owner, 2026-11-22). A bonus issue is a smaller step than a demerger (2:5 is x5/7, a 30% fall), so the rule
above never sees it, and Yahoo does not always adjust the history behind it: Anlon's 2:5 bonus (record date 2026-11-02)
left every bar before 2026-10-21 on the old price scale and the later ones on the new, so its 52-week high read -38%
(really about -13%), its weekly averages sat above the price and the screener said Downtrend / Exit / Caution.
`price_adjustments.json` (the private data repo: it names held stocks) lists such breaks by hand:

    {"ACME.NS": [{"before": "2026-10-20", "factor": 0.7142857142857143, "note": "2:5 bonus"}]}

Every bar dated before `before` is multiplied by `factor`. A correction applies only while the break is still in
the data: the first bar on or after `before` over the last one before it must be within CORRECTION_TOLERANCE of
`factor`, so a series Yahoo has since repaired (no break left), or one the correction already fixed, is left alone --
it can never be applied twice. Volume is not scaled. A malformed file or entry is ignored, never raised.

SUSPECTS. A one-session move beyond SUSPECT_LOW / SUSPECT_HIGH in the last SUSPECT_SESSIONS sessions that no rule
adjusted is remembered (`suspect_tickers`) and refresh_data.py prints how many there are, so the next bonus or split
Yahoo mishandles is noticed by the job and not by someone reading a table.

pandas only; every job and the Vercel functions (market_lists) import it.
"""

import json
import math
import os

import pandas as pd

GAP_LOW = 0.6
GAP_HIGH = 1 / GAP_LOW
REVERSAL_BARS = 5
PRICE_COLUMNS = ("Open", "High", "Low", "Close", "Adj Close")

CORRECTIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_adjustments.json")
CORRECTION_TOLERANCE = 0.15
SUSPECT_LOW = 0.75
SUSPECT_HIGH = 1.34
SUSPECT_SESSIONS = 60
SUSPECTS = set()
_CORRECTIONS_CACHE = {}


def load_corrections(path=None):
    """{ticker: [(Timestamp before, factor)]} from price_adjustments.json; {} when the file is absent or unreadable.
    An entry without a parseable date, or with a factor that is not a positive number other than 1, is skipped.
    Never raises (it runs inside every price download)."""
    path = path or CORRECTIONS_FILE
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return {}
    cached = _CORRECTIONS_CACHE.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    out = {}
    try:
        with open(path) as fh:
            raw = json.load(fh)
        for ticker, entries in (raw or {}).items():
            for e in entries if isinstance(entries, list) else []:
                try:
                    before = pd.Timestamp(e["before"])
                    factor = float(e["factor"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not (math.isfinite(factor) and factor > 0 and factor != 1.0) or pd.isna(before):
                    continue
                out.setdefault(str(ticker).strip().upper(), []).append((before, factor))
    except (OSError, ValueError, AttributeError):
        out = {}
    _CORRECTIONS_CACHE[path] = (stamp, out)
    return out


def has_correction(ticker):
    return str(ticker).upper() in load_corrections()


def manual_gaps(closes, ticker):
    """[(before, factor)] of the listed corrections for `ticker` whose break is still in `closes` (see CORRECTIONS)."""
    entries = load_corrections().get(str(ticker).upper())
    if not entries:
        return []
    s = pd.Series(closes).dropna()
    s = s[s > 0]
    out = []
    for before, factor in entries:
        if s.index.tz is not None and before.tzinfo is None:
            before = before.tz_localize(s.index.tz)
        prior, after = s[s.index < before], s[s.index >= before]
        if prior.empty or after.empty:
            continue
        observed = float(after.iloc[0] / prior.iloc[-1])
        if abs(math.log(observed / factor)) <= math.log(1 + CORRECTION_TOLERANCE):
            out.append((before, factor))
    return out


def all_gaps(closes, ticker):
    """Every adjustment for a close series, oldest first: the listed corrections whose break is still there, then the
    corporate-action gaps found in what is left (an Indian ticker only). Applying them all is the product of their
    ratios, which is what `factors` does."""
    manual = manual_gaps(closes, ticker)
    base = closes if not manual else closes * factors(closes.index, manual)
    auto = gaps(base) if is_indian(ticker) else []
    return sorted(manual + auto, key=lambda g: g[0])


def note_suspects(closes, ticker):
    """Remembers `ticker` when an ADJUSTED close series still has a one-session move beyond SUSPECT_LOW / SUSPECT_HIGH
    in its last SUSPECT_SESSIONS sessions (Indian tickers only)."""
    if not is_indian(ticker):
        return
    s = pd.Series(closes).dropna()
    s = s[s > 0].tail(SUSPECT_SESSIONS + 1)
    if len(s) < 2:
        return
    ratio = (s / s.shift(1)).iloc[1:]
    if ((ratio < SUSPECT_LOW) | (ratio > SUSPECT_HIGH)).any():
        SUSPECTS.add(str(ticker).upper())


def suspect_tickers():
    return sorted(SUSPECTS)


def reset_suspects():
    SUSPECTS.clear()


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
    """(closes adjusted, [(ex_date, ratio)]) -- unchanged for a ticker with no listed correction (price_adjustments.json)
    and, if it is not an NSE/BSE one, no gap either."""
    if (not is_indian(ticker) and not has_correction(ticker)) or closes is None or len(closes) == 0:
        return closes, []
    found = all_gaps(closes, ticker)
    if not found:
        return closes, []
    return closes * factors(closes.index, found), found


def adjust_frame(df, ticker):
    """The same for one ticker's OHLC frame (Open/High/Low/Close/Adj Close scaled, Volume kept: a demerger does
    not change the share count). The gaps are found on Close."""
    if (not is_indian(ticker) and not has_correction(ticker)) or df is None or df.empty or "Close" not in df.columns:
        return df, []
    found = all_gaps(df["Close"], ticker)
    if not found:
        note_suspects(df["Close"], ticker)
        return df, []
    f = factors(df.index, found)
    df = df.copy()
    for col in PRICE_COLUMNS:
        if col in df.columns:
            df[col] = df[col] * f
    note_suspects(df["Close"], ticker)
    return df, found


def adjust_download(raw, tickers):
    """A group_by="ticker" yf.download frame (columns (ticker, field)) with each Indian ticker's prices adjusted (and
    any other ticker listed in price_adjustments.json). Returns (raw, [tickers adjusted]). Not in place."""
    adjusted = []
    if raw is None or raw.empty or not isinstance(raw.columns, pd.MultiIndex):
        return raw, adjusted
    have = set(raw.columns.get_level_values(0))
    out = raw
    for t in tickers:
        if t not in have or not (is_indian(t) or has_correction(t)):
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
    """A close frame with one column per ticker (the breadth job's) with each Indian column adjusted (and any other
    column listed in price_adjustments.json). Returns (frame, [tickers adjusted]). Not in place."""
    adjusted = []
    if closes is None or closes.empty:
        return closes, adjusted
    out = closes
    for t in closes.columns:
        if not (is_indian(t) or has_correction(t)):
            continue
        col = closes[t]
        found = all_gaps(col, t)
        if not found:
            continue
        if out is closes:
            out = closes.copy()
        out[t] = col * factors(col.index, found)
        adjusted.append(t)
    return out, adjusted
