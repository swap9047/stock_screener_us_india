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

import concurrent.futures
import json
import os
import time
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(SCRIPT_DIR, "watchlist.json")
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "settings.json")
DATA_SNAPSHOT_FILE = os.path.join(SCRIPT_DIR, "data_snapshot.json")
INVESTED_FILE = os.path.join(SCRIPT_DIR, "invested.json")
MARKETS_FILE = os.path.join(SCRIPT_DIR, "markets.json")

# Legacy fallback list -- kept only for the one-off markets.json bootstrap
# and any old code (e.g. debug_snap.py) that still imports it directly. Live
# code should call get_market_keys() instead, since that reflects whatever
# markets have actually been added at runtime, not just these original two.
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
    # -- Trend column (Strong Uptrend/Uptrend/Downtrend/Strong Downtrend) --
    "trend_slope_lookback": 3,   # weeks used for the slow WEMA's regression slope
    "trend_near_high_low_pct": 0.10,   # "Strong" requires price within this % of the 52w high/low
    "trend_volume_ratio": 1.0,   # "Strong" requires avg_volume_10d / avg_volume_100d >= this
    # -- Vol Trend column (Exploding/In-line/Declining) -- independent of Trend/Tech Uptrend --
    "volume_explode_ratio": 1.4,   # avg_volume_10d / avg_volume_100d >= this => "Exploding"
    "volume_decline_ratio": 0.7,   # avg_volume_10d / avg_volume_100d <= this => "Declining"
    # -- Tech Uptrend column (boolean) -- independent of Vol Trend's ratio above --
    "tech_uptrend_min_vstop_weeks": 3,   # weeks since last VStop flip required for Tech Uptrend
    "tech_uptrend_volume_ratio": 1.4,   # avg_volume_10d / avg_volume_100d must be >= this
    
    # -- News Pipeline Defaults --
    "news_search_model": "models/gemma-4-26b-a4b-it",
    "news_reasoning_model": "models/gemini-3.5-flash-lite",
    "news_reasoning_budget": 8192,
    
    # -- Expert Pipeline Defaults --
    "expert_reasoning_model": "models/gemini-3.5-flash-lite",
    "expert_thinking_budget": 8192,

    # -- Sentiment Pipeline Defaults --
    "sentiment_reasoning_model": "models/gemini-3.5-flash-lite",
    "sentiment_thinking_budget": 8192,

    # -- News scope (which watchlist keys to include in news generation) --
    # Empty list = all registered markets (backward-compatible default).
    # Set to e.g. ["US"] to restrict the daily digest to one watchlist only.
    "news_watchlist_scope": [],

    # -- Fundamental columns (Sentiment, Qtr Profit/Revenue Growth %, etc.) --
    "show_fundamental_columns": True,
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


def _slugify_market_key(label):
    """Turns a free-text watchlist label into a safe, stable internal key:
    lowercase, alphanumeric/underscore only. This key is what every JSON
    file, alert rule scope, and cached AI view's "market" field is keyed
    by forever -- the label can be freely renamed later with zero data
    migration because nothing else ever reads the label as an identifier."""
    slug = "".join(c if c.isalnum() else "_" for c in label.strip().lower())
    slug = "_".join(filter(None, slug.split("_")))  # collapse repeated underscores
    return slug or "market"


def load_markets_registry():
    """Returns {key: {"label": str, "benchmark": str}}. Bootstraps
    markets.json from the legacy benchmark_us/benchmark_india settings (so
    any customization an existing user already made is preserved) the first
    time it's called on an install that predates this registry -- written
    immediately so the file always exists after the app's first render,
    same pattern as column_prefs.json's bootstrap."""
    if not os.path.exists(MARKETS_FILE):
        settings = load_settings()
        registry = {
            "US": {"label": "US Watchlist", "benchmark": settings.get("benchmark_us", BENCHMARKS["US"])},
            "INDIA": {"label": "India Watchlist", "benchmark": settings.get("benchmark_india", BENCHMARKS["INDIA"])},
        }
        save_markets_registry(registry)
        return registry
    try:
        with open(MARKETS_FILE) as f:
            registry = json.load(f)
        if not registry:
            raise ValueError("empty registry")
        return registry
    except Exception:
        return {
            "US": {"label": "US Watchlist", "benchmark": BENCHMARKS["US"]},
            "INDIA": {"label": "India Watchlist", "benchmark": BENCHMARKS["INDIA"]},
        }


def save_markets_registry(registry):
    with open(MARKETS_FILE, "w") as f:
        json.dump(registry, f, indent=2)


def get_market_keys():
    """Live list of every registered market's stable key, in registry order.
    Prefer this over the legacy MARKETS constant -- it reflects watchlists
    added at runtime, not just the original US/INDIA pair."""
    return list(load_markets_registry().keys())


def add_watchlist(label, benchmark):
    """Registers a new watchlist: generates a permanent unique key from
    `label` (slugified), adds it to the markets registry with an empty
    ticker list and empty custom-filter list. Returns the new key.

    Deletion is intentionally NOT exposed here or anywhere in the UI --
    removing a watchlist means manually dropping its key from markets.json,
    watchlist.json, and custom_filters.json (a deliberate, code-level-only
    action, not a clickable button)."""
    from filters import load_custom_filters, save_custom_filters

    registry = load_markets_registry()
    base_key = _slugify_market_key(label)
    if base_key.upper() == "ALL":
        base_key = f"{base_key}_market"  # "ALL" is reserved for the alert-scope "every watchlist" option
    key = base_key
    suffix = 2
    while key in registry:
        key = f"{base_key}_{suffix}"
        suffix += 1

    registry[key] = {"label": label.strip(), "benchmark": benchmark.strip()}
    save_markets_registry(registry)

    watchlists = load_watchlists()
    watchlists[key] = []
    save_watchlists(watchlists)

    custom_filters = load_custom_filters()
    custom_filters[key] = []
    save_custom_filters(custom_filters)

    return key


def rename_watchlist(key, new_label):
    """Renames a watchlist's DISPLAY LABEL only -- the internal key (and
    everything keyed by it: watchlist.json, custom_filters.json, alert rule
    scopes, cached AI views' "market" field) never changes, so this is a
    single-field update with no data migration."""
    registry = load_markets_registry()
    if key not in registry:
        raise KeyError(f"Unknown market key: {key}")
    registry[key]["label"] = new_label.strip()
    save_markets_registry(registry)


def get_benchmarks(settings=None):
    """Returns {market_key: benchmark_ticker} for every registered market."""
    registry = load_markets_registry()
    return {key: info["benchmark"] for key, info in registry.items()}


def get_exchange_label(market):
    """Human-readable 'X-listed' phrase for LLM search/reasoning prompts.
    Preserves the exact existing wording for the two pre-registered markets
    (US/INDIA) so their prompts don't change at all; any other market gets a
    generic fallback built from its registry label, since the minimal
    add-watchlist form doesn't collect a dedicated exchange phrase."""
    if market == "INDIA":
        return "NSE/BSE-listed"
    if market == "US":
        return "US-listed"
    label = load_markets_registry().get(market, {}).get("label", market)
    return f"{label}-listed"


def get_benchmark_display(market):
    """Human-readable benchmark name for LLM prompts, e.g. "S&P 500 (SPY)".
    Preserves exact existing wording for US/INDIA; any other market falls
    back to just its benchmark ticker, since the minimal add-watchlist form
    doesn't collect a separate display name."""
    if market == "US":
        return "S&P 500 (SPY)"
    if market == "INDIA":
        return "Nifty 500 (^CRSLDX)"
    return load_markets_registry().get(market, {}).get("benchmark", market)


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
        "RSI-D": "rsi14_daily",
        "RSI-W": "rsi14_weekly",
        "RSI-M": "rsi14_monthly",
        "RS-D": "rs_daily",
        "RS-W": "rs_weekly",
        "RS-M": "rs_monthly",
        "VStop-W": "vstop_weekly",
        "Trend": "trend",
        "Vol Trend": "volume_trend",
        "Net Vol 10D": "net_volume_10d_dir",
        "VStop Dir": "vstop_weekly_direction",
        "52W High": "week52_high",
        "52W Low": "week52_low",
        "Vol 10D": "avg_volume_10d",
        "Vol 100D": "avg_volume_100d",
        "% Chg": "pct_change_1d",
        "Qtr Profit Growth %": "qtr_profit_growth",
        "Qtr Revenue Growth %": "qtr_revenue_growth",
        "VStop Weeks Ago": "vstop_weekly_weeks_since_change",
        "Tech Uptrend": "tech_uptrend",
        "Flag": "flag",
        "Notes": "note",
        "Expert Take": "expert_take",
    }


# Backwards-compatible module-level snapshot, built from whatever is in
# settings.json at import time. Prefer get_filterable_metrics(settings) in
# new code so labels always reflect live (not import-time) settings.
FILTERABLE_METRICS = get_filterable_metrics()


def load_watchlists():
    """Returns {market_key: [tickers]} for every registered market (see
    load_markets_registry()), plus any additional keys already present in
    the raw JSON file -- defensive union, not a hardcoded "US"/"INDIA"
    whitelist, so a newly added market's tickers are never silently dropped."""
    registry_keys = set(load_markets_registry().keys())
    if not os.path.exists(WATCHLIST_FILE):
        return {k: [] for k in registry_keys}
    with open(WATCHLIST_FILE) as f:
        data = json.load(f)
    all_keys = registry_keys | set(data.keys())
    return {k: data.get(k, []) for k in all_keys}


def save_watchlists(watchlists):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(watchlists, f, indent=2)


def load_watchlist(market):
    return load_watchlists().get(market, [])


def save_watchlist(market, tickers):
    all_lists = load_watchlists()
    all_lists[market] = tickers
    save_watchlists(all_lists)


def load_invested_weights():
    """Returns {"TICKER": 1.0, ...} mapping of invested tickers to their portfolio weight."""
    if not os.path.exists(INVESTED_FILE):
        return {}
    with open(INVESTED_FILE) as f:
        return json.load(f)


def save_invested_weights(weights):
    with open(INVESTED_FILE, "w") as f:
        json.dump(weights, f, indent=2)


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


def wilder_smooth(raw, period):
    """Wilder's RMA smoothing (used by RSI's avg gain/loss AND ATR) with
    the original (1978) seeding convention: the first output is a plain
    mean of the first `period` raw values, then smoothed recursively from
    there (avg[t] = (avg[t-1]*(p-1) + raw[t]) / p). `raw` is expected to
    already have a leading NaN (e.g. from .diff()) at index 0. Matches
    TradingView's convention for RSI and ATR alike.

    This is NOT the same as naively running pandas' ewm(alpha=1/period,
    adjust=False) over the whole series from its first data point -- that
    effectively seeds the recursion from the very first available raw
    value instead of a clean period-length average. The two converge once
    there's ample history (the wrong seed's influence decays exponentially,
    ~1% residual after 60 bars), but for a short series -- e.g. monthly RSI
    on a stock with only ~2 years of history since its IPO, or ATR/VStop on
    a recently-listed ticker with few weekly bars -- the wrong seed never
    fully washes out and can be off by 10+ points vs TradingView. Confirmed
    on ENTERO.NS (India, IPO'd Feb 2024, only 30 monthly bars): the old
    ewm-from-bar-zero code gave RSI-M 44.2, correct Wilder seeding gives
    53.6, TradingView showed 54.1.
    """
    if len(raw) <= period:
        return pd.Series([np.nan] * len(raw), index=raw.index)

    seed = raw.iloc[1:period + 1].mean()

    # Prepend the SMA seed to the remaining raw values, then let ewm's own
    # adjust=False recursion (y[0]=x[0], y[t]=alpha*x[t]+(1-alpha)*y[t-1])
    # do the Wilder smoothing from that seed onward -- vectorized, no
    # explicit per-row Python loop needed.
    tail = pd.concat([pd.Series([seed]), raw.iloc[period + 1:]], ignore_index=True)
    smoothed_tail = tail.ewm(alpha=1 / period, adjust=False).mean()

    result = pd.Series(np.nan, index=raw.index)
    result.iloc[period:] = smoothed_tail.values
    return result


def compute_rsi(series, period=14):
    """Wilder's RSI -- see wilder_smooth() for the seeding convention."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = wilder_smooth(gain, period)
    avg_loss = wilder_smooth(loss, period)

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
    """Wilder's ATR: RMA (alpha=1/length) of True Range -- see
    wilder_smooth() for the seeding convention (same fix as RSI)."""
    high, low, close = ohlc_df["High"], ohlc_df["Low"], ohlc_df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    tr.iloc[0] = np.nan  # first bar has no prev_close -- match RSI's .diff() convention
    return wilder_smooth(tr, length)


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
                   avg_volume_10d, avg_volume_100d, slope_lookback,
                   near_high_low_pct=0.10, volume_ratio=1.0, ema_fast=None):
    """Returns (trend_label, trend_rank) -- a 4-level trend-strength read:
    "Strong Uptrend" / "Uptrend" / "Downtrend" / "Strong Downtrend"
    (trend_rank: 4/3/2/1, for numeric sort/filter use).

    Direction (up vs down) is a hard AND across up to 4 conditions --
    "Uptrend" requires ALL of the following that are evaluable (no partial
    credit, no majority vote):
      1. price above the slow WEMA
      2. the WEMA's own regression slope over `slope_lookback` weeks is
         rising (a least-squares fit, not a raw two-point diff -- see note
         below)
      3. fast WEMA above slow WEMA (e.g. 10 WEMA > 40 WEMA) -- moving-average
         alignment, required whenever `ema_fast` is supplied
      4. Mansfield RS vs the benchmark is positive, if available
    "Downtrend" is the mirror image (all 4 conditions bearish). Any mixed
    result -- some conditions bullish, some not, short of unanimous either
    way -- is conservatively classified as "Downtrend": Uptrend must be
    fully earned, not just have more signals in its favor. RS is the only
    condition that can be skipped (when there isn't enough history yet for
    the RS lookback) -- when skipped, only the remaining 3 need to
    unanimously agree.

    Strength ("Strong" prefix) requires BOTH of:
      - price within `near_high_low_pct` of its trailing 52-week high (for an
        uptrend) or 52-week low (for a downtrend) -- i.e. the move has real
        extension. Default 0.10 = within 10%.
      - 10-day average volume >= `volume_ratio` times the 100-day average --
        i.e. recent activity is elevated, not drying up. Default 1.0 = just
        needs to be higher, no minimum multiple.
    Both must agree for "Strong"; otherwise it's just Uptrend/Downtrend.
    These two thresholds are this column's OWN parameters -- independent of
    the similarly-shaped ratios used by the Vol Trend and Tech Uptrend
    columns (see DEFAULT_SETTINGS), so tuning one never moves the others.

    Using a regression slope (not a 2-point endpoint diff) for the WEMA
    direction matters for volatile movers: a violent spike-and-fade (e.g. a
    short squeeze) keeps a longer-window endpoint diff positive for weeks
    after the MA has already turned down, since the old spike still
    outweighs the new decline. A short regression window reads the MA's
    *current* direction instead.

    This is still a simplified, fully mechanical read -- a genuine
    multi-factor stage/trend indicator would also weigh momentum and beta --
    so treat it as a sort/filter aid, not a precise signal. Returns
    (trend_label, trend_rank, detail) where `detail` is a dict of the
    individual condition booleans and raw numbers behind the label, meant
    for building a "why" tooltip. Returns (None, None, None) if there isn't
    enough weekly history yet.
    """
    n = len(ema_slow_series)
    if n < slope_lookback + 1:
        return None, None, None
    window = ema_slow_series.iloc[-(slope_lookback + 1):]
    if window.isna().any():
        return None, None, None
    x = np.arange(len(window))
    slope = np.polyfit(x, window.values, 1)[0]
    last_ma = window.iloc[-1]

    price_above_ma = last_close > last_ma
    slope_rising = slope > 0
    ema_aligned = (ema_fast > last_ma) if ema_fast is not None else None
    rs_positive = (rs_weekly > 0) if rs_weekly is not None else None

    bullish = [price_above_ma, slope_rising]
    bearish = [not price_above_ma, not slope_rising]
    if ema_aligned is not None:
        bullish.append(ema_aligned)
        bearish.append(not ema_aligned)
    if rs_positive is not None:
        bullish.append(rs_positive)
        bearish.append(not rs_positive)

    if all(bullish):
        direction = "Uptrend"
    elif all(bearish):
        direction = "Downtrend"
    else:
        # Mixed signals -- not unanimous either way. Conservative default:
        # Uptrend must be fully confirmed, so anything short of that is
        # Downtrend rather than a partial-credit guess.
        direction = "Downtrend"

    near_high = week52_high is not None and week52_high > 0 and last_close >= week52_high * (1 - near_high_low_pct)
    near_low = week52_low is not None and week52_low > 0 and last_close <= week52_low * (1 + near_high_low_pct)
    volume_rising = (
        avg_volume_10d is not None and avg_volume_100d is not None and avg_volume_10d >= volume_ratio * avg_volume_100d
    )
    near_high_low_relevant = near_high if direction == "Uptrend" else near_low

    if direction == "Uptrend":
        strong = near_high and volume_rising
        label, rank = ("Strong Uptrend" if strong else "Uptrend"), (4 if strong else 3)
    else:
        strong = near_low and volume_rising
        label, rank = ("Strong Downtrend" if strong else "Downtrend"), (1 if strong else 2)

    detail = {
        "direction": direction,
        "strong": strong,
        "last_close": last_close,
        "last_ma": round(float(last_ma), 2),
        "slope": round(float(slope), 4),
        "price_above_ma": price_above_ma,
        "slope_rising": slope_rising,
        "ema_fast": ema_fast,
        "ema_aligned": ema_aligned,
        "rs_weekly": rs_weekly,
        "rs_positive": rs_positive,
        "week52_high": week52_high,
        "week52_low": week52_low,
        "near_high_low_pass": near_high_low_relevant,
        "near_high_low_pct": near_high_low_pct,
        "avg_volume_10d": avg_volume_10d,
        "avg_volume_100d": avg_volume_100d,
        "volume_rising": volume_rising,
        "volume_ratio": volume_ratio,
    }
    return label, rank, detail


def _download_with_retries(all_tickers, period, attempts=3, timeout=90, wait=30):
    """Bulk yf.download() with a hard per-attempt timeout (yfinance/Yahoo can
    hang or stall with no native timeout of its own) and a retry-with-backoff
    loop, mirroring the _generate_with_timeout pattern used for LLM calls
    elsewhere in this app. Raises the last error if all attempts fail, so a
    persistent outage fails the job clearly instead of hanging indefinitely
    or silently proceeding with partial/no data."""
    last_exc = None
    for attempt in range(1, attempts + 1):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                yf.download, all_tickers, period=period, interval="1d",
                group_by="ticker", auto_adjust=True, progress=False, threads=True,
            )
            try:
                return future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                last_exc = TimeoutError(f"yf.download timed out after {timeout}s (attempt {attempt}/{attempts})")
            except Exception as e:
                last_exc = e
        print(f"  [yf.download attempt {attempt}/{attempts} failed] {last_exc}")
        if attempt < attempts:
            time.sleep(wait)
    raise last_exc


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
    trend_near_high_low_pct = settings.get("trend_near_high_low_pct", 0.10)
    trend_volume_ratio = settings.get("trend_volume_ratio", 1.0)
    volume_explode_ratio = settings.get("volume_explode_ratio", 1.4)
    volume_decline_ratio = settings.get("volume_decline_ratio", 0.7)
    tech_uptrend_min_vstop_weeks = settings.get("tech_uptrend_min_vstop_weeks", 3)
    tech_uptrend_volume_ratio = settings.get("tech_uptrend_volume_ratio", 1.4)

    if not tickers:
        return [], datetime.now().strftime("%Y-%m-%d %H:%M")

    all_tickers = list(tickers) + [benchmark]
    raw = _download_with_retries(all_tickers, period)

    bench_daily = raw[benchmark]["Close"].dropna()
    bench_df = raw[benchmark].dropna(how="all")
    bench_weekly = weekly_resample(bench_df)["Close"]
    bench_monthly = monthly_resample(bench_df)["Close"]

    results = []

    for t in tickers:
        company_name = t
        qtr_profit_growth = None
        qtr_revenue_growth = None
        try:
            info = yf.Ticker(t).info
            company_name = info.get("longName") or info.get("shortName") or t
            # Yahoo's own YoY quarterly growth reads (this quarter vs. the
            # same quarter last year) -- returned as decimals (0.278 = 27.8%).
            earnings_growth = info.get("earningsQuarterlyGrowth")
            revenue_growth = info.get("revenueGrowth")
            if earnings_growth is not None:
                qtr_profit_growth = round(earnings_growth * 100, 1)
            if revenue_growth is not None:
                qtr_revenue_growth = round(revenue_growth * 100, 1)
        except Exception as e:
            print(f"  [{t}] Could not fetch company name: {e}")

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

            net_volume_10d_dir = None
            net_volume_10d_ratio = None
            if len(daily_close) >= 11 and len(daily_volume) >= 10:
                last_11_closes = daily_close.tail(11)
                last_10_volumes = daily_volume.tail(10)
                net_vol = 0
                total_vol = 0
                for i in range(1, 11):
                    vol = last_10_volumes.iloc[i-1]
                    total_vol += vol
                    if last_11_closes.iloc[i] > last_11_closes.iloc[i-1]:
                        net_vol += vol
                    elif last_11_closes.iloc[i] < last_11_closes.iloc[i-1]:
                        net_vol -= vol
                net_volume_10d_dir = "Positive" if net_vol > 0 else "Negative"
                if total_vol > 0:
                    net_volume_10d_ratio = round((net_vol / total_vol) * 100, 1)

            # 52-week high/low: intraday extremes over the trailing ~252 trading days
            window_252 = df.tail(252)
            week52_high = round(float(window_252["High"].max()), 1) if window_252["High"].notna().any() else None
            week52_low = round(float(window_252["Low"].min()), 1) if window_252["Low"].notna().any() else None

            trend = trend_rank = trend_detail = None
            if ema40_series is not None:
                trend, trend_rank, trend_detail = compute_trend(
                    last_close, ema40_series, rs_weekly, week52_high, week52_low,
                    avg_volume_10d, avg_volume_100d, trend_slope_lookback,
                    near_high_low_pct=trend_near_high_low_pct, volume_ratio=trend_volume_ratio,
                    ema_fast=ema10,
                )

            # Tech Uptrend: close > weekly VStop (in an uptrend that's held for
            # a while) + close above the slow weekly WEMA + volume surging.
            # Uses its OWN volume ratio (tech_uptrend_volume_ratio) -- independent
            # of Vol Trend's "Exploding" ratio above, even though both default to
            # the same 1.4x, so tuning one column never moves the other.
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
                    and avg_volume_10d > tech_uptrend_volume_ratio * avg_volume_100d
                )

            results.append({
                "ticker": t,
                "company_name": company_name,
                "last_close": round(last_close, 1),
                "pct_change_1d": pct_change_1d,
                "qtr_profit_growth": qtr_profit_growth,
                "qtr_revenue_growth": qtr_revenue_growth,
                "data_start": data_start,
                "data_end": data_end,
                "data_end_age_days": data_end_age_days,
                "avg_volume_10d": avg_volume_10d,
                "avg_volume_100d": avg_volume_100d,
                "volume_trend": volume_trend,
                "net_volume_10d_dir": net_volume_10d_dir,
                "net_volume_10d_ratio": net_volume_10d_ratio,
                "week52_high": week52_high,
                "week52_low": week52_low,
                "trend": trend,
                "trend_rank": trend_rank,
                "trend_detail": trend_detail,
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
    for market in watchlists.keys():
        tickers = watchlists.get(market, [])
        bench = benchmarks.get(market, "SPY")
        results, as_of = fetch_snapshot(tickers, benchmark=bench, period=period, settings=settings)
        for r in results:
            r["market"] = market
        per_market[market] = results

    # Apply user-defined custom columns (custom_columns.py) here -- NOT in
    # app.py -- so every consumer of fetch_all_markets gets them
    # automatically: the Streamlit app, but also alert_check.py and
    # refresh_data.py, which run headless (GitHub Actions) and never touch
    # app.py at all. A custom column needs to be available to alert
    # conditions, not just the table, so it has to be computed here, at the
    # source, rather than bolted on downstream in just one consumer.
    from custom_columns import apply_custom_columns_to_rows
    combined = [r for market in per_market for r in per_market[market]]
    apply_custom_columns_to_rows(combined)

    # Same reasoning as custom columns above -- per-ticker notes/flags
    # (ticker_notes.py) need to be available to alert conditions and the
    # headless scripts too, not just the table, so attach them here at the
    # source rather than only in app.py.
    from ticker_notes import apply_notes_to_rows
    apply_notes_to_rows(combined, min_vstop_weeks=settings.get("tech_uptrend_min_vstop_weeks", 3))

    return combined, as_of, per_market


def _json_default(o):
    """json.dump default= hook for numpy scalar types (np.bool_, np.int64,
    np.float64, etc.) that sneak into result rows from pandas/numpy calcs --
    the stdlib json module doesn't know how to serialize these even though
    they look/behave like native bool/int/float."""
    if isinstance(o, np.generic):
        return o.item()
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


def save_data_snapshot(as_of, per_market, settings=None):
    """Persists a fetch_all_markets() result to disk so the Streamlit app
    can load it directly instead of hitting yfinance live on every session
    -- meant to be called once/day by the scheduled data-refresh workflow
    (see refresh_data.py), not by the app itself. Stores the settings used
    to compute it too, so the app can detect a settings change (EMA
    lengths, thresholds, etc.) since the snapshot ran and fall back to a
    live fetch instead of showing data computed with stale parameters."""
    from datetime import timezone
    with open(DATA_SNAPSHOT_FILE, "w") as f:
        json.dump({
            "as_of": as_of,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "per_market": per_market,
            "settings": settings or {},
        }, f, indent=2, default=_json_default)


def load_data_snapshot():
    if not os.path.exists(DATA_SNAPSHOT_FILE):
        return None
    try:
        with open(DATA_SNAPSHOT_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def snapshot_is_usable(snapshot, watchlists, settings):
    """True if `snapshot` can be shown as-is: it has a row for every ticker
    currently in `watchlists` (for every market), AND it was computed with
    the same settings as `settings`. If someone added a ticker since the
    last scheduled refresh, or changed a calc parameter (EMA length, RSI
    threshold, etc.) in the Settings dialog, the snapshot no longer
    reflects reality -- the app should fall back to a live fetch rather
    than silently show stale/incomplete data until tomorrow's 7 AM run."""
    if not snapshot or not isinstance(snapshot.get("per_market"), dict):
        return False
        
    # Only compare calculation settings, ignoring pipeline/model choices
    # so changing a news model doesn't invalidate the price snapshot!
    snap_calc = {k: v for k, v in snapshot.get("settings", {}).items() if not k.startswith(("news_", "expert_"))}
    curr_calc = {k: v for k, v in settings.items() if not k.startswith(("news_", "expert_"))}
    
    if snap_calc != curr_calc:
        return False
        
    per_market = snapshot["per_market"]
    for market in watchlists.keys():
        snap_tickers = {r.get("ticker") for r in per_market.get(market, [])}
        wanted = set(watchlists.get(market, []))
        if not wanted.issubset(snap_tickers):
            return False
    return True


if __name__ == "__main__":
    combined, as_of, per_market = fetch_all_markets()
    print(json.dumps({"as_of": as_of, **per_market}, indent=2))
