"""Breadth maths: % of a set of stocks above their 200-day SMA, and at 52-week
highs / lows, day by day.

Shared by the nightly market-breadth job (refresh_market_breadth.py, over the
S&P 500 and Nifty 500) and the app's per-watchlist breadth view (over one
watchlist's closes from dashboard_perf.json). Like filters.py, it is shared on
purpose: the two must count a stock the same way, and the copies would drift.

Pure pandas, no network: everything here takes a close panel (dates x tickers,
NaN where a ticker has no close) and returns numbers.
"""

import pandas as pd


def compute_breadth(closes, since=None):
    """Breadth over `closes` (DataFrame, one column per ticker), or None when
    it is empty. `since` (a Timestamp) drops earlier days from the history.

    Returns {"above", "below", "total", "pct_above"} for the latest day --
    counting only stocks that have both a close that day and a 200-day SMA --
    plus "history", "highs_history" and "lows_history": {"YYYY-MM-DD": %}.
    """
    if closes is None or closes.empty:
        return None
    closes = closes.sort_index()

    # Calculate 200d SMA. This is the simple 200-day average -- the "200 DSMA"
    # convention used everywhere else in the app (stock_data.py) and by public
    # breadth screeners.
    #
    # min_periods=190 rather than 200: the panel is a union of every ticker's
    # calendar, so a name that simply had no print on a few days carries sporadic
    # NaNs. Demanding all 200 would drop ~190 perfectly good Nifty names over gaps
    # of 1-10 days. Averaging 190+ of the last 200 closes is the same number to
    # within rounding; what we actually want to exclude is recent listings, which
    # the cumulative-history mask below handles directly.
    sma200 = closes.rolling(window=200, min_periods=190).mean()
    sma200 = sma200.where(closes.notna().cumsum() >= 200)
    is_above = closes > sma200

    # Calculate 52w High/Low (252 trading days).
    #
    # Same cumulative-history mask the SMA gets above, for the same reason and
    # it was missing here: without it a stock with 126 sessions of history could
    # post a "52-week high" off a 126-day window (min_periods tolerates gaps
    # INSIDE the window, it does not require the window to be full), while
    # stocks too new to qualify at all still sat in the denominator. The
    # numerator was too generous and the denominator too large -- a split that
    # matters most in exactly the stretches this chart is read for, after an
    # IPO wave or an index reconstitution. Threshold is the window size, as
    # `>= 200` is for the 200-day SMA.
    enough_52w = closes.notna().cumsum() >= 252
    high52 = closes.rolling(window=252, min_periods=126).max().where(enough_52w)
    low52 = closes.rolling(window=252, min_periods=126).min().where(enough_52w)

    is_new_high = closes >= high52
    is_new_low = closes <= low52

    valid_counts = closes.notna().sum(axis=1)
    # Each series needs its OWN denominator: only stocks that actually have the
    # measure in question. ma_counts for the 200d SMA, hl_counts for the 52-week
    # window -- counting a stock that cannot yet qualify understates the rate.
    # Per day, too: a mixed India + US list has days one exchange is shut, and
    # those stocks simply have no close that day rather than counting as below.
    ma_counts = (closes.notna() & sma200.notna()).sum(axis=1)
    hl_counts = (closes.notna() & high52.notna()).sum(axis=1)

    breadth_series = (is_above.sum(axis=1) / ma_counts) * 100
    # Guard the division rather than leaning on valid_mask: a zero denominator
    # yields inf, and inf survives a later boolean mask if the row is kept.
    highs_series = (is_new_high.sum(axis=1) / hl_counts.replace(0, pd.NA)) * 100
    lows_series = (is_new_low.sum(axis=1) / hl_counts.replace(0, pd.NA)) * 100

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
    highs_series = highs_series[valid_mask]
    lows_series = lows_series[valid_mask]

    if since is not None:
        keep = breadth_series.index >= since
        breadth_series = breadth_series[keep]
        highs_series = highs_series[keep]
        lows_series = lows_series[keep]

    history_dict, highs_dict, lows_dict = {}, {}, {}
    for date in breadth_series.index:
        date_str = date.strftime("%Y-%m-%d")
        history_dict[date_str] = round(float(breadth_series[date]), 1)
        highs_dict[date_str] = round(float(highs_series[date]), 1)
        lows_dict[date_str] = round(float(lows_series[date]), 1)

    last_valid_idx = closes.index[-1]
    latest_close = closes.loc[last_valid_idx]
    latest_sma = sma200.loc[last_valid_idx]
    has_ma = latest_close.notna() & latest_sma.notna()
    above_now = int((latest_close > latest_sma).sum())
    total_now = int(has_ma.sum())
    pct_above_now = float(above_now / total_now * 100) if total_now > 0 else 0.0

    return {
        "above": above_now,
        "below": total_now - above_now,
        "total": total_now,
        "pct_above": round(pct_above_now, 1),
        "history": history_dict,
        "highs_history": highs_dict,
        "lows_history": lows_dict,
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
