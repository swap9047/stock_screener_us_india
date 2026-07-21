"""
Shared calculation logic for the WEMA/RSI/RS watchlist tools.
Used by app.py (Streamlit dashboard) and alert_check.py (scheduled
Discord alert checker).

Watchlists are segregated by market:
  US    - NYSE/Nasdaq/S&P tickers, benchmarked against the S&P 500 (SPY)
  INDIA - NSE (.NS suffix) / BSE (.BO suffix) tickers, benchmarked against
          the Nifty 500 (^CRSLDX on Yahoo Finance) -- a broader read than
          the Nifty 50, matching the S&P 500's breadth. Both benchmarks are
          editable at runtime via the app's Settings dialog (see
          DEFAULT_SETTINGS / load_settings / save_settings below).

Metrics computed per ticker, matching the reference chart's Fast/Medium/Slow
EMA settings (Daily: 10/50/200, Weekly: 10/20/40):
  - EMA10 / EMA20 / EMA40  (weekly closes)
  - EMA10 / EMA50 / EMA200 (daily closes)
  - RSI(14) on daily, weekly, AND monthly closes
  - Mansfield Relative Strength (RSM), on daily / weekly / monthly closes,
    vs the market benchmark, matching the "Mansfield" RS Mode config:
        RSM = ((RSD_today / SMA(RSD, n)) - 1) * 100
        RSD = stock_close / benchmark_close  (Dorsey relative strength ratio)
    Lookbacks (n): daily = 63, weekly = 26, monthly = 12
    RSM > 0  => stock outperforming the benchmark trend
    RSM < 0  => stock underperforming the benchmark trend
    Source: Stan Weinstein / Mansfield RS, as documented by stageanalysis.net
    and implemented in TrendSpider's "RS Mode" indicator.
  - Weekly VStop (Volatility Stop), J. Welles Wilder's ATR-based stop-and-reverse
    system (a cousin of Parabolic SAR). Computed on weekly OHLC bars.
    Length=20, ATR factor=2 -- TradingView's built-in "Volatility Stop" defaults.
    Uptrend:   stop = max(previous stop, close - factor*ATR); flips to downtrend
               if close closes below that stop.
    Downtrend: stop = min(previous stop, close + factor*ATR); flips to uptrend
               if close closes above that stop.
    The stop only ever moves in the trend's favor (never retraces), unlike the
    Chandelier Exit. Reports current value, direction (Up/Down), and the date
    of the most recent trend flip.

All displayed numeric values are rounded to 1 decimal place.
"""

import json
import os
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(SCRIPT_DIR, "watchlist.json")
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "settings.json")

MARKETS = ["US", "INDIA"]

# Fallback/default values -- used to seed settings.json the first time, and
# as safe fallbacks if a key is ever missing. All of these are editable at
# runtime via the app's Settings dialog (see load_settings/save_settings).
BENCHMARKS = {"US": "SPY", "INDIA": "^CRSLDX"}  # S&P 500 proxy / Nifty 500
RS_LOOKBACK_DAILY = 63
RS_LOOKBACK_WEEKLY = 26
RS_LOOKBACK_MONTHLY = 12
VSTOP_LENGTH = 20
VSTOP_FACTOR = 2

DEFAULT_SETTINGS = {
    "ema_weekly": [10, 20, 40],   # weekly EMA fast/mid/slow periods
    "ema_daily": [10, 50, 200],   # daily EMA fast/mid/slow periods
    "rsi_period": 14,
    "rs_lookback_daily": RS_LOOKBACK_DAILY,
    "rs_lookback_weekly": RS_LOOKBACK_WEEKLY,
    "rs_lookback_monthly": RS_LOOKBACK_MONTHLY,
    "vstop_length": VSTOP_LENGTH,
    "vstop_factor": VSTOP_FACTOR,
    "benchmark_us": BENCHMARKS["US"],
    "benchmark_india": BENCHMARKS["INDIA"],
    "trend_slope_lookback": 3,   # weeks used for the slow WEMA's regression slope (Trend read)
    "volume_explode_ratio": 1.4,   # avg_volume_10d / avg_volume_100d >= this => "Exploding"
    "volume_decline_ratio": 0.7,   # avg_volume_10d / avg_volume_100d <= this => "Declining"
    "tech_uptrend_min_vstop_weeks": 3,   # weeks since last VStop flip required for Tech Uptrend
}


def load_settings():
    """Returns the current calculation settings, merged over defaults so a
    partially-written or older settings.json never crashes the app."""
    settings = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                saved = json.load(f)
            settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
        except Exception:
            pass
    return settings


def save_settings(settings):
    clean = {k: settings.get(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}
    with open(SETTINGS_FILE, "w") as f:
        json.dump(clean, f, indent=2)


def get_benchmarks(settings=None):
    settings = settings or load_settings()
    return {"US": settings["benchmark_us"], "INDIA": settings["benchmark_india"]}


def get_filterable_metrics(settings=None):
    """Metrics available for the custom filter builder (label -> field name).
    Labels for the EMA rows embed the currently configured period, so they
    stay accurate when the user edits Settings."""
    settings = settings or load_settings()
    w_fast, w_mid, w_slow = settings["ema_weekly"]
    d_fast, d_mid, d_slow = settings["ema_daily"]
    return {
        "Last Close": "last_close",
        f"{w_fast} WEMA": "ema10",
        f"{w_mid} WEMA": "ema20",
        f"{w_slow} WEMA": "ema40",
        f"{d_fast} DEMA": "ema10_daily",
        f"{d_mid} DEMA": "ema50",
        f"{d_slow} DEMA": "ema200",
        "RSI Daily": "rsi14_daily",
        "RSI Weekly": "rsi14_weekly",
        "RSI Monthly": "rsi14_monthly",
        "RS Daily": "rs_daily",
        "RS Weekly": "rs_weekly",
        "RS Monthly": "rs_monthly",
        "VStop Weekly": "vstop_weekly",
        "Trend Rank": "trend_rank",
        "52W High": "week52_high",
        "52W Low": "week52_low",
        "Vol 10D Avg": "avg_volume_10d",
        "Vol 100D Avg": "avg_volume_100d",
        "% Day Change": "pct_change_1d",
        "VStop Weeks Since Change": "vstop_weekly_weeks_since_change",
        "Tech Uptrend": "tech_uptrend",
    }


# Backwards-compatible module-level snapshot, built from whatever is in
# settings.json at import time. Prefer get_filterable_metrics(settings) in
# new code so labels always reflect live (not import-time) settings.
FILTERABLE_METRICS = get_filterable_metrics()


def load_watchlists():
    """Returns {"US": [...], "INDIA": [...]}."""
    if not os.path.exists(WATCHLIST_FILE):
        return {"US": [], "INDIA": []}
    with open(WATCHLIST_FILE) as f:
        data = json.load(f)
    return {"US": data.get("US", []), "INDIA": data.get("INDIA", [])}


def save_watchlists(watchlists):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump({"US": watchlists.get("US", []), "INDIA": watchlists.get("INDIA", [])}, f, indent=2)


def load_watchlist(market):
    return load_watchlists().get(market, [])


def save_watchlist(market, tickers):
    all_lists = load_watchlists()
    all_lists[market] = tickers
    save_watchlists(all_lists)


def tradingview_url(ticker):
    """Best-effort TradingView chart URL for a yfinance-style ticker.
    TradingView resolves a bare symbol (no exchange prefix) to its listed
    exchange automatically (e.g. symbol=HIMS -> NYSE:HIMS chart), so we
    just strip the .NS/.BO suffix used for Indian tickers and link straight
    to the interactive chart view (not the symbol overview page)."""
    bare = ticker
    if bare.endswith(".NS") or bare.endswith(".BO"):
        bare = bare.rsplit(".", 1)[0]
    return f"https://www.tradingview.com/chart/?symbol={bare}"


def validate_ticker(ticker):
    """Quick check that yfinance actually has recent price data for this
    ticker (catches typos / wrong exchange suffix before saving to the
    watchlist). Returns True if valid, False otherwise. Best-effort: on
    network error it returns True (fails open) so a transient outage
    doesn't block adding a real ticker."""
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        return not hist.empty
    except Exception:
        return True


def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def weekly_resample(daily_df):
    return daily_df.resample("W-FRI").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
    }).dropna()


def monthly_resample(daily_df):
    return daily_df.resample("ME").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
    }).dropna()


def mansfield_rs(stock_close, bench_close, lookback):
    """RSM = ((RSD_today / SMA(RSD, lookback)) - 1) * 100, RSD = stock/benchmark ratio.
    stock_close and bench_close must be aligned (same index) pandas Series."""
    aligned = pd.concat([stock_close, bench_close], axis=1, join="inner").dropna()
    if len(aligned) < lookback + 1:
        return None
    ratio = aligned.iloc[:, 0] / aligned.iloc[:, 1]
    sma = ratio.rolling(lookback).mean()
    if pd.isna(sma.iloc[-1]) or sma.iloc[-1] == 0:
        return None
    rsm = ((ratio.iloc[-1] / sma.iloc[-1]) - 1) * 100
    return round(float(rsm), 1)


def compute_atr(ohlc_df, length=14):
    """Wilder's ATR: RMA (alpha=1/length) of True Range."""
    high, low, close = ohlc_df["High"], ohlc_df["Low"], ohlc_df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def compute_vstop(ohlc_df, length=VSTOP_LENGTH, factor=VSTOP_FACTOR):
    """J. Welles Wilder's Volatility Stop (stop-and-reverse, ATR-based),
    matching TradingView's built-in 'Volatility Stop' indicator defaults.

    Returns (vstop_series, direction_series). direction is 1 (uptrend) or
    -1 (downtrend). The first bar with a valid ATR starts in an assumed
    uptrend (matches the convention used by the stock-indicators library,
    since Wilder's original method has no way to know the 'true' prior
    trend at the very first usable bar).
    """
    atr = compute_atr(ohlc_df, length)
    close = ohlc_df["Close"]
    n = len(ohlc_df)

    vstop = pd.Series(index=ohlc_df.index, dtype=float)
    direction = pd.Series(index=ohlc_df.index, dtype=float)  # float so NaN works before start

    first_valid = atr.first_valid_index()
    if first_valid is None:
        return vstop, direction

    start_pos = ohlc_df.index.get_loc(first_valid)
    direction.iloc[start_pos] = 1
    vstop.iloc[start_pos] = close.iloc[start_pos] - factor * atr.iloc[start_pos]

    for i in range(start_pos + 1, n):
        prev_stop = vstop.iloc[i - 1]
        prev_dir = direction.iloc[i - 1]
        c = close.iloc[i]
        a = atr.iloc[i]
        if pd.isna(a):
            direction.iloc[i] = prev_dir
            vstop.iloc[i] = prev_stop
            continue

        if prev_dir == 1:
            candidate = c - factor * a
            new_stop = max(prev_stop, candidate)
            if c < new_stop:
                direction.iloc[i] = -1
                vstop.iloc[i] = c + factor * a
            else:
                direction.iloc[i] = 1
                vstop.iloc[i] = new_stop
        else:
            candidate = c + factor * a
            new_stop = min(prev_stop, candidate)
            if c > new_stop:
                direction.iloc[i] = 1
                vstop.iloc[i] = c - factor * a
            else:
                direction.iloc[i] = -1
                vstop.iloc[i] = new_stop

    return vstop, direction


def compute_trend(last_close, ema_slow_series, rs_weekly, week52_high, week52_low,
                   avg_volume_10d, avg_volume_100d, slope_lookback):
    """Returns (trend_label, trend_rank) -- a 4-level trend-strength read:
    "Strong Uptrend" / "Uptrend" / "Downtrend" / "Strong Downtrend"
    (trend_rank: 4/3/2/1, for numeric sort/filter use).

    Direction (up vs down) is a 2-3 vote system:
      1. price vs the slow WEMA (above/below)
      2. the WEMA's own regression slope over `slope_lookback` weeks (a
         least-squares fit, not a raw two-point diff -- see note below)
      3. Mansfield RS vs the benchmark (positive/negative), if available
    Majority vote wins; a genuine tie (only when RS is unavailable and the
    other two disagree) falls back to price vs MA.

    Strength ("Strong" prefix) requires BOTH of:
      - price within 10% of its trailing 52-week high (for an uptrend) or
        52-week low (for a downtrend) -- i.e. the move has real extension
      - 10-day average volume > 100-day average volume -- i.e. recent
        activity is elevated, not drying up
    Both must agree for "Strong"; otherwise it's just Uptrend/Downtrend.

    Using a regression slope (not a 2-point endpoint diff) for the WEMA
    direction matters for volatile movers: a violent spike-and-fade (e.g. a
    short squeeze) keeps a longer-window endpoint diff positive for weeks
    after the MA has already turned down, since the old spike still
    outweighs the new decline. A short regression window reads the MA's
    *current* direction instead.

    This is still a simplified, fully mechanical read -- a genuine
    multi-factor stage/trend indicator would also weigh momentum and beta --
    so treat it as a sort/filter aid, not a precise signal. Returns
    (None, None) if there isn't enough weekly history yet.
    """
    n = len(ema_slow_series)
    if n < slope_lookback + 1:
        return None, None
    window = ema_slow_series.iloc[-(slope_lookback + 1):]
    if window.isna().any():
        return None, None
    x = np.arange(len(window))
    slope = np.polyfit(x, window.values, 1)[0]
    last_ma = window.iloc[-1]

    votes = [1 if last_close > last_ma else -1, 1 if slope > 0 else -1]
    if rs_weekly is not None:
        if rs_weekly > 0:
            votes.append(1)
        elif rs_weekly < 0:
            votes.append(-1)
    direction_score = sum(votes)

    if direction_score > 0:
        direction = "Uptrend"
    elif direction_score < 0:
        direction = "Downtrend"
    else:
        direction = "Uptrend" if last_close > last_ma else "Downtrend"

    near_high = week52_high is not None and week52_high > 0 and last_close >= week52_high * 0.90
    near_low = week52_low is not None and week52_low > 0 and last_close <= week52_low * 1.10
    volume_rising = (
        avg_volume_10d is not None and avg_volume_100d is not None and avg_volume_10d > avg_volume_100d
    )

    if direction == "Uptrend":
        strong = near_high and volume_rising
        return ("Strong Uptrend" if strong else "Uptrend"), (4 if strong else 3)
    strong = near_low and volume_rising
    return ("Strong Downtrend" if strong else "Downtrend"), (1 if strong else 2)


def fetch_snapshot(tickers, benchmark="SPY", period="5y", settings=None):
    """Returns (results list of dicts, as_of timestamp string) for one market's tickers."""
    settings = settings or load_settings()
    rsi_period = settings["rsi_period"]
    w_fast, w_mid, w_slow = settings["ema_weekly"]
    d_fast, d_mid, d_slow = settings["ema_daily"]
    rs_lb_daily = settings["rs_lookback_daily"]
    rs_lb_weekly = settings["rs_lookback_weekly"]
    rs_lb_monthly = settings["rs_lookback_monthly"]
    vstop_length = settings["vstop_length"]
    vstop_factor = settings["vstop_factor"]
    trend_slope_lookback = settings.get("trend_slope_lookback", 3)
    volume_explode_ratio = settings.get("volume_explode_ratio", 1.4)
    volume_decline_ratio = settings.get("volume_decline_ratio", 0.7)
    tech_uptrend_min_vstop_weeks = settings.get("tech_uptrend_min_vstop_weeks", 3)

    if not tickers:
        return [], datetime.now().strftime("%Y-%m-%d %H:%M")

    all_tickers = list(tickers) + [benchmark]
    raw = yf.download(all_tickers, period=period, interval="1d", group_by="ticker",
                       auto_adjust=True, progress=False, threads=True)

    bench_daily = raw[benchmark]["Close"].dropna()
    bench_df = raw[benchmark].dropna(how="all")
    bench_weekly = weekly_resample(bench_df)["Close"]
    bench_monthly = monthly_resample(bench_df)["Close"]

    results = []

    for t in tickers:
        try:
            df = raw[t].dropna(how="all")
            if df.empty or len(df) < 60:
                continue

            daily_close = df["Close"].dropna()

            # RSI
            rsi14_daily_series = compute_rsi(daily_close, rsi_period)
            rsi14_daily = round(float(rsi14_daily_series.iloc[-1]), 1) if pd.notna(rsi14_daily_series.iloc[-1]) else None

            weekly = weekly_resample(df)
            rsi14_weekly = None
            if len(weekly) >= rsi_period + 1:
                rsi14_weekly_series = compute_rsi(weekly["Close"], rsi_period)
                if pd.notna(rsi14_weekly_series.iloc[-1]):
                    rsi14_weekly = round(float(rsi14_weekly_series.iloc[-1]), 1)

            monthly = monthly_resample(df)
            rsi14_monthly = None
            if len(monthly) >= rsi_period + 1:
                rsi14_monthly_series = compute_rsi(monthly["Close"], rsi_period)
                if pd.notna(rsi14_monthly_series.iloc[-1]):
                    rsi14_monthly = round(float(rsi14_monthly_series.iloc[-1]), 1)

            # Weekly EMAs (fast/mid/slow, e.g. 10/20/40)
            ema10 = ema20 = ema40 = None
            crossed_below_10 = crossed_below_40 = False
            crossed_above_10 = crossed_above_40 = False
            ema40_series = None

            if len(weekly) >= w_slow + 1:
                ema10_series = weekly["Close"].ewm(span=w_fast, adjust=False).mean()
                ema20_series = weekly["Close"].ewm(span=w_mid, adjust=False).mean()
                ema40_series = weekly["Close"].ewm(span=w_slow, adjust=False).mean()

                last_close_w = weekly["Close"].iloc[-1]
                prev_close_w = weekly["Close"].iloc[-2]

                ema10 = round(float(ema10_series.iloc[-1]), 1)
                ema20 = round(float(ema20_series.iloc[-1]), 1)
                ema40 = round(float(ema40_series.iloc[-1]), 1)

                prev_ema10 = ema10_series.iloc[-2]
                prev_ema40 = ema40_series.iloc[-2]

                crossed_below_10 = bool(prev_close_w >= prev_ema10 and last_close_w < ema10)
                crossed_below_40 = bool(prev_close_w >= prev_ema40 and last_close_w < ema40)
                crossed_above_10 = bool(prev_close_w < prev_ema10 and last_close_w >= ema10)
                crossed_above_40 = bool(prev_close_w < prev_ema40 and last_close_w >= ema40)
            elif len(weekly) >= w_mid + 1:
                ema10_series = weekly["Close"].ewm(span=w_fast, adjust=False).mean()
                ema20_series = weekly["Close"].ewm(span=w_mid, adjust=False).mean()
                ema10 = round(float(ema10_series.iloc[-1]), 1)
                ema20 = round(float(ema20_series.iloc[-1]), 1)

            # Daily EMAs (fast/mid/slow, e.g. 10/50/200)
            ema10_daily = ema50 = ema200 = None
            crossed_below_10_daily = crossed_above_10_daily = False
            crossed_below_50 = crossed_above_50 = False
            crossed_below_200 = crossed_above_200 = False

            if len(daily_close) >= d_fast + 1:
                ema10_daily_series = daily_close.ewm(span=d_fast, adjust=False).mean()
                ema10_daily = round(float(ema10_daily_series.iloc[-1]), 1)
                prev_close_d0 = daily_close.iloc[-2]
                prev_ema10_daily = ema10_daily_series.iloc[-2]
                crossed_below_10_daily = bool(prev_close_d0 >= prev_ema10_daily and daily_close.iloc[-1] < ema10_daily)
                crossed_above_10_daily = bool(prev_close_d0 < prev_ema10_daily and daily_close.iloc[-1] >= ema10_daily)
            if len(daily_close) >= d_mid + 1:
                ema50_series = daily_close.ewm(span=d_mid, adjust=False).mean()
                ema50 = round(float(ema50_series.iloc[-1]), 1)
                prev_close_d = daily_close.iloc[-2]
                prev_ema50 = ema50_series.iloc[-2]
                crossed_below_50 = bool(prev_close_d >= prev_ema50 and daily_close.iloc[-1] < ema50)
                crossed_above_50 = bool(prev_close_d < prev_ema50 and daily_close.iloc[-1] >= ema50)
            if len(daily_close) >= d_slow + 1:
                ema200_series = daily_close.ewm(span=d_slow, adjust=False).mean()
                ema200 = round(float(ema200_series.iloc[-1]), 1)
                prev_close_d2 = daily_close.iloc[-2]
                prev_ema200 = ema200_series.iloc[-2]
                crossed_below_200 = bool(prev_close_d2 >= prev_ema200 and daily_close.iloc[-1] < ema200)
                crossed_above_200 = bool(prev_close_d2 < prev_ema200 and daily_close.iloc[-1] >= ema200)

            # Mansfield RS (daily / weekly / monthly)
            monthly_close = monthly["Close"]
            rs_daily = mansfield_rs(daily_close, bench_daily, rs_lb_daily)
            rs_weekly = mansfield_rs(weekly["Close"], bench_weekly, rs_lb_weekly)
            rs_monthly = mansfield_rs(monthly_close, bench_monthly, rs_lb_monthly)

            # Weekly VStop (Volatility Stop)
            vstop_weekly = None
            vstop_weekly_direction = None
            vstop_weekly_last_change = None
            vstop_weekly_weeks_since_change = None
            vstop_weekly_flipped = False

            if len(weekly) >= vstop_length + 5:
                vstop_series, dir_series = compute_vstop(weekly, length=vstop_length, factor=vstop_factor)
                valid_dir = dir_series.dropna()
                if not valid_dir.empty:
                    vstop_weekly = round(float(vstop_series.iloc[-1]), 1)
                    vstop_weekly_direction = "Up" if valid_dir.iloc[-1] == 1 else "Down"

                    flips = valid_dir[valid_dir.diff().fillna(0) != 0]
                    if not flips.empty:
                        last_change_idx = flips.index[-1]
                        vstop_weekly_last_change = last_change_idx.strftime("%Y-%m-%d")
                        weeks_since = valid_dir.index.get_loc(valid_dir.index[-1]) - valid_dir.index.get_loc(last_change_idx)
                        vstop_weekly_weeks_since_change = int(weeks_since)
                        vstop_weekly_flipped = bool(weeks_since == 0)

            last_close = float(daily_close.iloc[-1])
            pct_change_1d = None
            if len(daily_close) >= 2 and daily_close.iloc[-2]:
                pct_change_1d = round(float((daily_close.iloc[-1] / daily_close.iloc[-2] - 1) * 100), 2)
            data_start = daily_close.index[0].strftime("%Y-%m-%d")
            data_end = daily_close.index[-1].strftime("%Y-%m-%d")
            data_end_age_days = (datetime.now().date() - daily_close.index[-1].date()).days

            daily_volume = df["Volume"].dropna()
            avg_volume_10d = round(float(daily_volume.tail(10).mean())) if len(daily_volume) >= 10 else None
            avg_volume_100d = round(float(daily_volume.tail(100).mean())) if len(daily_volume) >= 100 else None

            volume_trend = None
            if avg_volume_10d is not None and avg_volume_100d is not None and avg_volume_100d > 0:
                vol_ratio = avg_volume_10d / avg_volume_100d
                if vol_ratio >= volume_explode_ratio:
                    volume_trend = "Exploding"
                elif vol_ratio <= volume_decline_ratio:
                    volume_trend = "Declining"
                else:
                    volume_trend = "In-line"

            # 52-week high/low: intraday extremes over the trailing ~252 trading days
            window_252 = df.tail(252)
            week52_high = round(float(window_252["High"].max()), 1) if window_252["High"].notna().any() else None
            week52_low = round(float(window_252["Low"].min()), 1) if window_252["Low"].notna().any() else None

            trend = trend_rank = None
            if ema40_series is not None:
                trend, trend_rank = compute_trend(
                    last_close, ema40_series, rs_weekly, week52_high, week52_low,
                    avg_volume_10d, avg_volume_100d, trend_slope_lookback,
                )

            # Tech Uptrend: close > weekly VStop (in an uptrend that's held for
            # a while) + close above the slow weekly WEMA + volume surging.
            tech_uptrend = 0
            if (
                vstop_weekly is not None
                and vstop_weekly_weeks_since_change is not None
                and ema40 is not None
                and avg_volume_10d is not None
                and avg_volume_100d is not None
            ):
                tech_uptrend = int(
                    last_close > vstop_weekly
                    and vstop_weekly_weeks_since_change > tech_uptrend_min_vstop_weeks
                    and last_close > ema40
                    and avg_volume_10d > volume_explode_ratio * avg_volume_100d
                )

            results.append({
                "ticker": t,
                "last_close": round(last_close, 1),
                "pct_change_1d": pct_change_1d,
                "data_start": data_start,
                "data_end": data_end,
                "data_end_age_days": data_end_age_days,
                "avg_volume_10d": avg_volume_10d,
                "avg_volume_100d": avg_volume_100d,
                "volume_trend": volume_trend,
                "week52_high": week52_high,
                "week52_low": week52_low,
                "trend": trend,
                "trend_rank": trend_rank,
                "tech_uptrend": tech_uptrend,
                "ema10": ema10,
                "ema20": ema20,
                "ema40": ema40,
                "ema10_daily": ema10_daily,
                "ema50": ema50,
                "ema200": ema200,
                "below_10": (last_close < ema10) if ema10 is not None else None,
                "below_20": (last_close < ema20) if ema20 is not None else None,
                "below_40": (last_close < ema40) if ema40 is not None else None,
                "below_10_daily": (last_close < ema10_daily) if ema10_daily is not None else None,
                "below_50": (last_close < ema50) if ema50 is not None else None,
                "below_200": (last_close < ema200) if ema200 is not None else None,
                "crossed_below_10": crossed_below_10,
                "crossed_below_40": crossed_below_40,
                "crossed_above_10": crossed_above_10,
                "crossed_above_40": crossed_above_40,
                "crossed_below_10_daily": crossed_below_10_daily,
                "crossed_above_10_daily": crossed_above_10_daily,
                "crossed_below_50": crossed_below_50,
                "crossed_above_50": crossed_above_50,
                "crossed_below_200": crossed_below_200,
                "crossed_above_200": crossed_above_200,
                "rsi14_daily": rsi14_daily,
                "rsi14_weekly": rsi14_weekly,
                "rsi14_monthly": rsi14_monthly,
                "rs_daily": rs_daily,
                "rs_weekly": rs_weekly,
                "rs_monthly": rs_monthly,
                "vstop_weekly": vstop_weekly,
                "vstop_weekly_direction": vstop_weekly_direction,
                "vstop_weekly_last_change": vstop_weekly_last_change,
                "vstop_weekly_weeks_since_change": vstop_weekly_weeks_since_change,
                "vstop_weekly_flipped": vstop_weekly_flipped,
            })
        except Exception as e:
            print(f"  {t}: ERROR {e}")

    as_of = datetime.now().strftime("%Y-%m-%d %H:%M")
    return results, as_of


def fetch_all_markets(watchlists=None, period="5y", settings=None):
    """Fetches both US and INDIA watchlists with their respective benchmarks
    and returns a combined (results, as_of, per_market_results) tuple.
    per_market_results is {"US": [...], "INDIA": [...]}."""
    if watchlists is None:
        watchlists = load_watchlists()
    settings = settings or load_settings()
    benchmarks = get_benchmarks(settings)

    per_market = {}
    as_of = datetime.now().strftime("%Y-%m-%d %H:%M")
    for market in MARKETS:
        tickers = watchlists.get(market, [])
        bench = benchmarks.get(market, "SPY")
        results, as_of = fetch_snapshot(tickers, benchmark=bench, period=period, settings=settings)
        for r in results:
            r["market"] = market
        per_market[market] = results

    combined = [r for market in MARKETS for r in per_market[market]]
    return combined, as_of, per_market


if __name__ == "__main__":
    combined, as_of, per_market = fetch_all_markets()
    print(json.dumps({"as_of": as_of, "US": per_market["US"], "INDIA": per_market["INDIA"]}, indent=2))
