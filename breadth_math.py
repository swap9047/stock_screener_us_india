"""Breadth maths: % of a set of stocks above their 200-day SMA, at 52-week
highs / lows, and down 10 / 20 / 30 / 40% or more from their 52-week high, day
by day.

Shared by the nightly market-breadth job (refresh_market_breadth.py, over the
S&P 500 and Nifty 500) and the app's per-watchlist breadth view (over one
watchlist's closes from dashboard_perf.json). Like filters.py, it is shared on
purpose: the two must count a stock the same way, and the copies would drift.

Pure pandas, no network: everything here takes a close panel (dates x tickers,
NaN where a ticker has no close) and returns numbers.
"""

import pandas as pd

# How far below its 52-week high a stock is counted as "down" (owner,
# 2026-10-30): at least 10% (a correction), 20% (a bear market), 30% and 40%
# (50% until the same day: at most a few % of an index got that far, a line
# along the floor).
# Each series counts every stock at least that far down, so they nest (the 20%
# share is never above the 10% one) and never cross.
DRAWDOWN_LEVELS = (10, 20, 30, 40)


def _own_sessions(closes, fn):
    """`fn` applied to each ticker's own closes (its NaN rows dropped), then
    lined back up with the panel's dates -- NaN on the days it has no close."""
    return closes.apply(lambda s: fn(s.dropna()).reindex(s.index))


def compute_breadth(closes, since=None):
    """Breadth over `closes` (DataFrame, one column per ticker), or None when
    it is empty. `since` (a Timestamp) drops earlier days from the history.

    Returns {"above", "below", "total", "pct_above"} for the latest day --
    counting only stocks that have both a close that day and a 200-day SMA --
    plus "history", "highs_history" and "lows_history": {"YYYY-MM-DD": %}.
    "pct_above_50" / "history_50" are the same over the 50-day SMA (2026-10-15,
    the charts' 50-day overlay), on the same days as "history".
    "drawdown" / "drawdown_history": {"10": %, ...} / {"10": {"YYYY-MM-DD": %}}
    -- the share of stocks at least DRAWDOWN_LEVELS % below their 52-week high
    (2026-10-30), over the same stocks and days as the highs / lows.
    """
    if closes is None or closes.empty:
        return None
    closes = closes.sort_index()

    # Calculate 200d SMA. This is the simple 200-day average -- the "200 DSMA"
    # convention used everywhere else in the app (stock_data.py) and by public
    # breadth screeners.
    #
    # Over each stock's OWN last 200 closes (_own_sessions), not the panel's last
    # 200 rows. The panel is a union of every ticker's calendar, and a row window
    # broke on it: it needed 190 closes in 200 rows, and an India stock in a mixed
    # India + US group had 189 (the US trades on Indian holidays and vice versa).
    # All Invested read 69.2% of 39 stocks instead of 75.0% of 52, and differed on
    # over half its days, by up to 26 points (44 on the 50-day) -- 2026-10-15. A
    # full window of a stock's own closes also needs no min_periods: a name with
    # no print on a few days just reaches a little further back, and a listing
    # with under 200 sessions has no SMA, so it is out of numerator and
    # denominator alike.
    sma200 = _own_sessions(closes, lambda s: s.rolling(window=200).mean())
    is_above = closes > sma200
    # The 50-day the same way, so a new listing is not "above" an average of its
    # first few days.
    sma50 = _own_sessions(closes, lambda s: s.rolling(window=50).mean())
    is_above_50 = closes > sma50

    # Calculate 52w High/Low (252 trading days), over each stock's own 252
    # closes like the SMAs. The full window matters: a stock with 126 sessions
    # of history used to post a "52-week high" off a 126-day window, while
    # stocks too new to qualify at all still sat in the denominator. The
    # numerator was too generous and the denominator too large -- a split that
    # matters most in exactly the stretches this chart is read for, after an
    # IPO wave or an index reconstitution.
    high52 = _own_sessions(closes, lambda s: s.rolling(window=252).max())
    low52 = _own_sessions(closes, lambda s: s.rolling(window=252).min())

    is_new_high = closes >= high52
    is_new_low = closes <= low52
    # Down at least L% from the 52-week high, on closes like the highs above (a
    # close, not an intraday high, so a stock at its high is 0% down). NaN where
    # the window is not full, so it is out of the count like the highs / lows.
    off_high = (1 - closes / high52) * 100
    is_down = {lvl: off_high >= lvl - 1e-9 for lvl in DRAWDOWN_LEVELS}

    valid_counts = closes.notna().sum(axis=1)
    # Each series needs its OWN denominator: only stocks that actually have the
    # measure in question. ma_counts for the 200d SMA, hl_counts for the 52-week
    # window -- counting a stock that cannot yet qualify understates the rate.
    # Per day, too: a mixed India + US list has days one exchange is shut, and
    # those stocks simply have no close that day rather than counting as below.
    ma_counts = (closes.notna() & sma200.notna()).sum(axis=1)
    ma50_counts = (closes.notna() & sma50.notna()).sum(axis=1)
    hl_counts = (closes.notna() & high52.notna()).sum(axis=1)

    breadth_series = (is_above.sum(axis=1) / ma_counts) * 100
    breadth50_series = (is_above_50.sum(axis=1) / ma50_counts.replace(0, pd.NA)) * 100
    # Guard the division rather than leaning on valid_mask: a zero denominator
    # yields inf, and inf survives a later boolean mask if the row is kept.
    highs_series = (is_new_high.sum(axis=1) / hl_counts.replace(0, pd.NA)) * 100
    lows_series = (is_new_low.sum(axis=1) / hl_counts.replace(0, pd.NA)) * 100
    down_series = {lvl: (is_down[lvl].sum(axis=1) / hl_counts.replace(0, pd.NA)) * 100 for lvl in DRAWDOWN_LEVELS}

    # Warmup: drop the first 252 rows of the DOWNLOADED panel so the 52w
    # high/low window is full. This used to be `iloc[252:]` applied AFTER
    # valid_mask -- but valid_mask had already removed the ~200-row SMA warmup,
    # so the two stacked and ~450 rows were lost. The 6y download then only
    # reached back ~4.2y, and the app's "5 Years" view sat blank for its first
    # ~9 months (history started 2022-06-28 instead of ~2021-09).
    # A panel too short to warm up keeps every row, as the old code did.
    warm = pd.Series(len(closes) <= 252, index=closes.index)
    warm.iloc[252:] = True
    valid_mask = (valid_counts > 0) & (ma_counts > 0) & (hl_counts > 0) & warm
    breadth_series = breadth_series[valid_mask]
    breadth50_series = breadth50_series[valid_mask]
    highs_series = highs_series[valid_mask]
    lows_series = lows_series[valid_mask]
    down_series = {lvl: v[valid_mask] for lvl, v in down_series.items()}

    if since is not None:
        keep = breadth_series.index >= since
        breadth_series = breadth_series[keep]
        breadth50_series = breadth50_series[keep]
        highs_series = highs_series[keep]
        lows_series = lows_series[keep]
        down_series = {lvl: v[keep] for lvl, v in down_series.items()}

    history_dict, history50_dict, highs_dict, lows_dict = {}, {}, {}, {}
    down_dict = {str(lvl): {} for lvl in DRAWDOWN_LEVELS}
    for date in breadth_series.index:
        date_str = date.strftime("%Y-%m-%d")
        history_dict[date_str] = round(float(breadth_series[date]), 1)
        if pd.notna(breadth50_series[date]):
            history50_dict[date_str] = round(float(breadth50_series[date]), 1)
        highs_dict[date_str] = round(float(highs_series[date]), 1)
        lows_dict[date_str] = round(float(lows_series[date]), 1)
        for lvl in DRAWDOWN_LEVELS:
            v = down_series[lvl][date]
            if pd.notna(v):
                down_dict[str(lvl)][date_str] = round(float(v), 1)

    last_valid_idx = closes.index[-1]
    latest_close = closes.loc[last_valid_idx]
    latest_sma = sma200.loc[last_valid_idx]
    has_ma = latest_close.notna() & latest_sma.notna()
    above_now = int((latest_close > latest_sma).sum())
    total_now = int(has_ma.sum())
    pct_above_now = float(above_now / total_now * 100) if total_now > 0 else 0.0
    latest_sma50 = sma50.loc[last_valid_idx]
    has_ma50 = latest_close.notna() & latest_sma50.notna()
    total50 = int(has_ma50.sum())
    pct_above_50 = round(float((latest_close > latest_sma50).sum() / total50 * 100), 1) if total50 else None
    latest_off = off_high.loc[last_valid_idx].dropna()
    drawdown = ({str(lvl): round(float((latest_off >= lvl - 1e-9).sum() / len(latest_off) * 100), 1)
                 for lvl in DRAWDOWN_LEVELS} if len(latest_off) else None)

    return {
        "above": above_now,
        "below": total_now - above_now,
        "total": total_now,
        "pct_above": round(pct_above_now, 1),
        "history": history_dict,
        "highs_history": highs_dict,
        "lows_history": lows_dict,
        "pct_above_50": pct_above_50,
        "history_50": history50_dict,
        "drawdown": drawdown,
        "drawdown_history": down_dict,
    }


def assemble_closes(perf_markets, tickers):
    """(close panel for `tickers`, tickers with no stored series).

    `perf_markets` is dashboard_perf.json's "markets": {watchlist: {ticker:
    {"YYYY-MM-DD": close}}}. Each ticker's series is taken from the FIRST block
    that has it, so a ticker held in two watchlists -- a combined group's
    members, or a benchmark such as SPY stored in every US watchlist -- appears
    once. Columns follow `tickers`' order, de-duplicated.
    """
    seen, columns, missing = set(), {}, []
    for t in tickers:
        if t in seen:
            continue
        seen.add(t)
        series = next((block[t] for block in (perf_markets or {}).values()
                       if isinstance(block, dict) and block.get(t)), None)
        if series:
            columns[t] = pd.Series(series, dtype=float)
        else:
            missing.append(t)
    if not columns:
        return pd.DataFrame(), missing
    panel = pd.DataFrame(columns)
    panel.index = pd.to_datetime(panel.index)
    return panel.sort_index(), missing
