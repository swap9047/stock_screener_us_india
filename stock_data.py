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
  - SMA10 / SMA20 / SMA40  (weekly closes)
  - SMA10 / SMA50 / SMA200 (daily closes)
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
    Length=VSTOP_LENGTH (10; TradingView's own default is 20), ATR factor=2,
    both editable in Settings. compute_vstop_tv follows Pine's volStop order:
    ratchet the stop first, then read the trend off the new stop.
    Uptrend:   stop = max(previous stop, close - factor*ATR); flips to downtrend
               if close closes below that stop.
    Downtrend: stop = min(previous stop, close + factor*ATR); flips to uptrend
               if close closes above that stop.
    The stop only ever moves in the trend's favor (never retraces), unlike the
    Chandelier Exit. Reports current value, direction (Up/Down), and the date
    of the most recent trend flip.

All displayed numeric values are rounded to 1 decimal place.
"""

import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(SCRIPT_DIR, "watchlist.json")
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "settings.json")
DATA_SNAPSHOT_FILE = os.path.join(SCRIPT_DIR, "data_snapshot.json")
INTERESTED_FILE = os.path.join(SCRIPT_DIR, "interested.json")
MARKETS_FILE = os.path.join(SCRIPT_DIR, "markets.json")
TICKER_INDEX_FILE = os.path.join(SCRIPT_DIR, "ticker_index.json")
WATCHLIST_GROUPS_FILE = os.path.join(SCRIPT_DIR, "watchlist_groups.json")

# Default combined-tab membership -- group key -> member market keys. Group
# keys/labels are fixed in app.py's COMBINED_TAB_DEFS (not user-creatable,
# per the "reassignment only" design); only membership is user-editable via
# load_watchlist_groups()/save_watchlist_groups() below. This is the mapping
# an install had before that editor existed, so bootstrapping to it is a
# no-op for behavior already in place.
DEFAULT_WATCHLIST_GROUPS = {
    "all_invested": ["us_invested", "india_invested"],
    "all_watchlist": ["india_watchlist", "us_watchlist"],
}

# Legacy fallback list -- kept only for the one-off markets.json bootstrap
# and any old code (e.g. debug_snap.py) that still imports it directly. Live
# code should call get_market_keys() instead, since that reflects whatever
# markets have actually been added at runtime, not just these original two.
MARKETS = ["US", "INDIA"]

# Fallback/default values -- used to seed settings.json the first time, and
# as safe fallbacks if a key is ever missing. All of these are editable at
# runtime via the app's Settings dialog (see load_settings/save_settings).
BENCHMARKS = {"US": "SPY", "INDIA": "^CRSLDX"}  # S&P 500 proxy / Nifty 500

# Known indices a ticker can be assigned to (see detect_ticker_index below),
# each mapped to the benchmark ticker used to compute RS/relative-return
# metrics against it. Deliberately a small, flat table rather than derived
# from the markets registry -- an index is a property of the TICKER, not of
# whichever watchlist(s) it happens to be filed under.
INDEX_DEFINITIONS = {"S&P 500": "SPY", "Nifty 500": "^CRSLDX"}

RS_LOOKBACK_DAILY = 63
RS_LOOKBACK_WEEKLY = 26
RS_LOOKBACK_MONTHLY = 12
VSTOP_LENGTH = 10
VSTOP_FACTOR = 2
# Both VStop engines are fully recursive -- every week's stop depends on the
# entire flip history back to wherever the series starts -- so a short or
# truncated weekly window doesn't just mean "less smoothing", it can put the
# stop on a genuinely different path than a full history would (confirmed:
# an India ticker served a spurious 1317/Down stop from a run whose fetch
# evidently returned less history than a clean pull, while `>= vstop_length
# + 5` -- just 15 bars -- happily passed). Requiring roughly a year of
# weekly bars before trusting the recursion doesn't fix a bad fetch, but it
# stops the pipeline from quietly serving a VStop built on a suspiciously
# short window -- same "blank rather than misleading" fallback the length
# check already used, just a more meaningful floor.
VSTOP_MIN_HISTORY_WEEKS = 52

# Breakout Window (see the block that computes it in fetch_snapshot).
# A prior close up to 5% above today counts as a level price has already
# reached, not as overhead resistance -- so a minor overshoot part-way
# through a base no longer resets the window to that day.
BREAKOUT_TOLERANCE = 1.05
# ~5 years x 252 trading days. fetch_snapshot already requests period="5y",
# so this is a no-op on today's data; it exists so the metric stays bounded
# if that period is ever widened.
BREAKOUT_LOOKBACK = 1260
# Half-width of the swing-high test: a bar is resistance only if its close is
# the highest over the +/-10 bars around it. Without this the walk-back
# happily references a bar that price merely passed through on the way DOWN,
# which is not resistance at all -- 13 of the 18 scan-relevant windows landed
# on such a bar. It also means a brand-new high is not treated as an
# established level until 10 sessions have confirmed it.
BREAKOUT_PIVOT_WIDTH = 10
# Overhead Supply window: 252 trading days (~1 year), matching the 52-week
# framing the distance metrics already use.
OVERHEAD_LOOKBACK = 252

# Periods pinned by the stockscans.in scan definitions these metrics exist to
# serve (Turbo Surge: ADX 14W / VSTOP 14W 2; Alpha Leaders: ADX 12M / RSI 12M).
# Deliberately NOT settings-driven, unlike rsi_period or vstop_length: a rule
# written against "ADX 14W" means period 14, and letting Settings move it would
# silently redefine the screen rather than tune it.
ADX_WEEKLY_PERIOD = 14
ADX_MONTHLY_PERIOD = 12
RSI_MONTHLY_12_PERIOD = 12
VSTOP_LENGTH_14W = 14

DEFAULT_SETTINGS = {
    "ema_weekly": [10, 20, 40],   # weekly EMA fast/mid/slow periods
    "ema_daily": [10, 50, 200],   # daily SMA fast/mid/slow periods
    "rsi_period": 14,
    "rs_lookback_daily": RS_LOOKBACK_DAILY,
    "rs_lookback_weekly": RS_LOOKBACK_WEEKLY,
    "rs_lookback_monthly": RS_LOOKBACK_MONTHLY,
    "vstop_length": VSTOP_LENGTH,
    "vstop_factor": VSTOP_FACTOR,
    # VStop calculation engine. "tv" = exact port of TradingView's built-in
    # Volatility Stop (hard-coded Source=close; stop anchors to the running
    # close max/min since the last stop-and-reverse flip). "app" = legacy
    # close-anchored Wilder stop, kept as an alternative.
    "vstop_mode": "tv",
    # When True, the trailing (in-progress) weekly bar is included in the
    # VStop computation, matching TradingView's live Volatility Stop value.
    # When False, VStop only uses fully completed weekly bars (avoids false
    # stop-and-reverse flips from a partial week, but lags TradingView by one
    # week mid-week).
    "vstop_include_incomplete_week": True,
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
    # -- TA Rules column (TheWrap flowchart, see compute_ta_rules) --
    "ta_converge_pct": 3.0,        # EMAs "converging" when (max - min) of the 3 WEMAs <= this % of the slow one
    "ta_break_pct": 3.0,           # a weekly close must be this % past an EMA / S-R zone to count as broken
    "ta_sr_lookback_weeks": 156,   # weekly bars searched for support/resistance pivots
    "ta_sr_pivot_weeks": 3,        # a pivot's wick is the extreme of +/- this many weeks
    "ta_sr_reaction_pct": 8.0,     # ...and price must then move this % away from it (rebound/retreat)
    "ta_sr_reaction_weeks": 8,     # ...within this many weeks
    "ta_sr_zone_pct": 3.0,         # pivots within this % of each other form one zone
    "ta_sr_min_touches": 2,        # a zone is a level only with at least this many pivots
    "ta_sr_recent_weeks": 13,      # a zone counts as broken only if price was on its other side this recently

    # -- News Pipeline Defaults --
    "news_search_model": "models/gemma-4-26b-a4b-it",
    "news_reasoning_model": "models/gemini-3.5-flash-lite",
    "news_reasoning_budget": 8192,
    # Stage 3 (collation) runs its own, stronger ladder -- see news_summary.
    # These MUST be listed here: load_settings drops any saved key absent from
    # DEFAULT_SETTINGS, so an override that isn't declared is silently ignored.
    "news_collation_model": "models/gemini-3.7-flash",
    "news_collation_fallback_model": "models/gemini-3.6-flash",
    "news_collation_thinking_budget": 8192,
    
    # -- Expert Pipeline Defaults --
    "expert_reasoning_model": "models/gemini-3.5-flash-lite",
    "expert_thinking_budget": 8192,

    # -- Sentiment Pipeline Defaults --
    "sentiment_reasoning_model": "models/gemini-3.5-flash-lite",
    "sentiment_thinking_budget": 8192,
    # One extra grounded search + one extra reasoning pass for a ticker whose
    # broad search found a known earnings announcement but no figures for it
    # (see fundamentals_eval.needs_targeted_retry). Set false to disable the
    # extra calls entirely. Prefixed "sentiment_" so it counts as a UI/pipeline
    # setting and does not invalidate the snapshot cache -- see calc_settings.
    "sentiment_targeted_retry": True,

    "note_dropdown_options": "",

    # -- News scope (which watchlist keys to include in news generation) --
    # Empty list = the all_invested group, NOT every market -- see
    # news_summary.resolve_news_scope / DEFAULT_NEWS_SCOPE_GROUP. May hold market
    # keys, combined group keys ("all_invested", "all_watchlist"), or a mix; set
    # to e.g. ["us_invested"] to restrict the daily digest to one watchlist.
    "news_watchlist_scope": [],

    # -- Fundamental columns (Sentiment, Qtr Profit/Revenue Growth %, etc.) --
    "show_fundamental_columns": True,
}


def load_settings():
    """Returns the current calculation settings, merged over defaults so a
    partially-written or older settings.json never crashes the app."""
    settings = dict(DEFAULT_SETTINGS)
    saved = read_json_strict(SETTINGS_FILE)
    if isinstance(saved, dict):
        settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    return settings


def save_settings(settings):
    """Write `settings`, keeping the CURRENT value of every key it does not name.

    This used to fill the gaps from DEFAULT_SETTINGS. The Settings dialog saves
    only the 18 calculation keys it shows, so one click on its Save silently
    reset the other 15 -- the AI reasoning models and thinking budgets, the news
    scope and models, the fundamental-columns toggle -- to their defaults, and
    the nightly jobs then quietly ran on the default model. Merge over the file
    instead; a caller that really wants defaults passes them explicitly (the
    dialog's "Reset to defaults" does)."""
    current = load_settings()
    clean = {k: settings[k] if k in settings else current[k] for k in DEFAULT_SETTINGS}
    atomic_write_json(SETTINGS_FILE, clean)


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
        us_benchmark = settings.get("benchmark_us", BENCHMARKS["US"])
        india_benchmark = settings.get("benchmark_india", BENCHMARKS["INDIA"])
        registry = {
            "us_invested": {"label": "US Invested", "benchmark": us_benchmark},
            "india_invested": {"label": "India Invested", "benchmark": india_benchmark},
            "india_watchlist": {"label": "India Watchlist", "benchmark": india_benchmark},
            "us_watchlist": {"label": "US Watchlist", "benchmark": us_benchmark},
        }
        save_markets_registry(registry)
        return registry
    registry = read_json_strict(MARKETS_FILE)
    if registry:
        return registry
    return {
            "us_invested": {"label": "US Invested", "benchmark": BENCHMARKS["US"]},
            "india_invested": {"label": "India Invested", "benchmark": BENCHMARKS["INDIA"]},
            "india_watchlist": {"label": "India Watchlist", "benchmark": BENCHMARKS["INDIA"]},
            "us_watchlist": {"label": "US Watchlist", "benchmark": BENCHMARKS["US"]},
        }


def save_markets_registry(registry):
    atomic_write_json(MARKETS_FILE, registry)


# The JSON file primitives live in json_store, which imports nothing but the
# standard library. They used to be defined here -- but stock_data imports
# numpy/pandas/yfinance at module scope, so alerts.load_rules() calling
# read_json_strict silently made `import alerts` require the whole analysis
# stack. daily-alerts.yml's gate installs only `requests`, so it started dying
# with "ModuleNotFoundError: No module named 'numpy'" and no alert could fire.
# Re-exported here so every existing `from stock_data import read_json_strict`
# (and atomic_write_json, and DataFileError) keeps working untouched.
from json_store import DataFileError, read_json_strict, atomic_write_json  # noqa: F401


def load_watchlist_groups():
    """Returns {group_key: [member market keys]} for the fixed combined
    tabs (see DEFAULT_WATCHLIST_GROUPS). Bootstraps the file with today's
    defaults the first time it's called on an install that predates this
    editor, same pattern as load_markets_registry(). Member keys no longer
    present in load_markets_registry() are dropped on read -- watchlist
    deletion isn't exposed in the UI, but a stale reference (e.g. a market
    key renamed outside the app) should never crash a combined tab, just
    quietly stop counting toward it."""
    if not os.path.exists(WATCHLIST_GROUPS_FILE):
        save_watchlist_groups(DEFAULT_WATCHLIST_GROUPS)
        groups = dict(DEFAULT_WATCHLIST_GROUPS)
    else:
        groups = read_json_strict(WATCHLIST_GROUPS_FILE)
        if not isinstance(groups, dict):
            groups = dict(DEFAULT_WATCHLIST_GROUPS)
    registry_keys = set(load_markets_registry().keys())
    return {gk: [m for m in members if m in registry_keys] for gk, members in groups.items()}


def save_watchlist_groups(groups):
    atomic_write_json(WATCHLIST_GROUPS_FILE, groups)


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
    # "all" is the alert-scope "every watchlist" word, and the combined tabs use
    # the DEFAULT_WATCHLIST_GROUPS keys as synthetic market keys for their own
    # widgets. Only "all" used to be reserved, so a watchlist labelled "All
    # Invested" slugged to "all_invested", both tabs then instantiated the same
    # per-market widget keys, and Streamlit's duplicate-key error took every tab
    # down -- with no way back from the UI, since deletion isn't offered.
    if base_key in {"all"} | set(DEFAULT_WATCHLIST_GROUPS):
        base_key = f"{base_key}_market"
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


def get_exchange_label(market, ticker=None):
    """Human-readable 'X-listed' phrase for LLM search/reasoning prompts.

    Resolve off the TICKER SUFFIX first, not the market key. This used to key
    off the market key alone with only "us_invested"/"india_invested"
    hardcoded, so every other watchlist fell through to a generic
    f"{registry label}-listed" -- which fed the search model literal nonsense:
    india_watchlist's 32 .NS tickers were described as "India Watchlist-listed"
    and a user-created watchlist's 22 as
    "<its label>-listed". That was 65 of 110 tickers getting a
    meaningless exchange phrase, and it silently got worse every time a
    watchlist was added, since the registry label is whatever the user typed in
    the add-watchlist form.

    The suffix is the reliable signal: .NS/.BO are Indian listings regardless of
    which watchlist the ticker happens to sit in, and everything else in this
    app is US-listed. `ticker` is optional only so the old single-argument call
    shape keeps working; pass it whenever you have it."""
    if ticker:
        if ticker.endswith(".NS") or ticker.endswith(".BO"):
            return "NSE/BSE-listed"
        return "US-listed"
    # No ticker available -- fall back to the original market-key wording so
    # the two pre-registered markets' prompts stay byte-identical.
    if market == "india_invested":
        return "NSE/BSE-listed"
    return "US-listed"


def get_benchmark_display(market):
    """Human-readable benchmark name for LLM prompts, e.g. "S&P 500 (SPY)".
    Preserves exact existing wording for us_invested/india_invested (see
    get_exchange_label); any other market falls back to just its benchmark
    ticker, since the minimal add-watchlist form doesn't collect a separate
    display name."""
    if market == "us_invested":
        return "S&P 500 (SPY)"
    if market == "india_invested":
        return "Nifty 500 (^CRSLDX)"
    return load_markets_registry().get(market, {}).get("benchmark", market)


def get_filterable_metrics(settings=None):
    """Metrics available for the custom filter builder (label -> field name).
    Labels for the SMA rows embed the currently configured period, so they
    stay accurate when the user edits Settings."""
    settings = settings or load_settings()
    w_fast, w_mid, w_slow = settings["ema_weekly"]
    d_fast, d_mid, d_slow = settings["ema_daily"]
    return {
        "Last Close": "last_close",
        f"{w_fast} WEMA": "ema10",
        f"{w_mid} WEMA": "ema20",
        f"{w_slow} WEMA": "ema40",
        f"{d_fast} DSMA": "ema10_daily",
        f"{d_mid} DSMA": "ema50",
        f"{d_slow} DSMA": "ema200",
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
        # Breakout family. Also registered in app.build_column_defs as optional
        # columns that default to hidden -- offered in the sidebar picker, off
        # until asked for.
        "Breakout Window": "breakout_window",
        "26WH Distance": "week26_distance",
        "52WH Distance": "week52_distance",
        "52W High Age": "week52_high_age",
        "Overhead Supply": "overhead_supply",
        "5Y High": "high_5y",
        "5Y High Distance": "high_5y_distance",
        "Vol 10D": "avg_volume_10d",
        "Vol 20D": "avg_volume_20d",
        "Vol 100D": "avg_volume_100d",
        # Scan-driven metrics: periods are pinned by the scan definitions
        # (see ADX_WEEKLY_PERIOD and friends), not by Settings.
        "ADX-W": "adx_weekly_14",
        "ADX-M": "adx_monthly_12",
        "RSI-M (12)": "rsi12_monthly",
        "VStop-W (14)": "vstop_weekly_14",
        # "vs Index" = vs each ticker's own index benchmark (see ticker_index.json)
        "1M Ret vs Index": "rel_ret_1m_index",
        "6M Ret vs Index": "rel_ret_6m_index",
        "PAT Growth TTM %": "ttm_profit_growth",
        "Revenue Growth TTM %": "ttm_revenue_growth",
        "% Chg": "pct_change_1d",
        # Trailing price returns. These were computed and stored (and shown as
        # app columns) long before they were filterable -- registering them
        # here is what lets a rule reference them. Labels must match the
        # app.build_column_defs labels exactly so the glossary and the
        # condition-builder caption resolve them.
        "Perf 1M %": "perf_1m",
        "Perf 3M %": "perf_3m",
        "Perf 6M %": "perf_6m",
        "Perf 1Y %": "perf_1y",
        "Perf 3Y %": "perf_3y",
        "Qtr Profit Growth %": "qtr_profit_growth",
        "Qtr EPS Growth %": "qtr_eps_growth",
        "Qtr Revenue Growth %": "qtr_revenue_growth",
        "VStop Weeks Ago": "vstop_weekly_weeks_since_change",
        "10/30 W Golden Cross (weeks ago)": "gc_weeks_10_30",
        "1W Ret vs Index": "rel_ret_1w_index",
        "Tech Uptrend": "tech_uptrend",
        "TA Rules": "ta_rules",
        "Flag": "flag",
        "Notes": "note",
        "Expert Take": "expert_take",
        "Expert News?": "expert_news_backed",
        # Table columns that were never registered here, so they couldn't be
        # used in a custom filter or an alert rule despite being visible and
        # sitting on the row dict all along. Anything added to
        # app.build_column_defs belongs here too unless it's genuinely
        # underivable at filter time (see "Alerts" below) -- that omission is
        # what this block is fixing, so don't let the next column repeat it.
        # Labels MUST match build_column_defs character-for-character; the
        # glossary and the condition-builder caption resolve through them.
        "Interested": "interested",
        "Sentiment": "sentiment",
        "Index": "index_name",
        "Company Name": "company_name",
        "Data Thru": "data_end",
        "Reported Qtr": "reported_qtr",
        "P/E (TTM)": "trailing_pe",
        "P/E (Fwd)": "forward_pe",
        "P/B": "pb_ratio",
        "EV/EBITDA": "ev_ebitda",
        "P/Cashflow": "p_cashflow",
        "ROE %": "roe",
        "CFO/OP 5Y": "cfo_op_5yr",
        "ROCE %": "roce",
        # Deliberately NOT registered: "Alerts" (matched_alerts). It's produced
        # by evaluating the alert rules against rows that filtering has already
        # selected, so filtering on it would be circular.
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
    data = read_json_strict(WATCHLIST_FILE, {})
    # Registry order first (that's the order markets.json declares and the
    # tabs render in), then any extra keys the raw file carries, sorted.
    #
    # This used to be `for k in (registry_keys | set(data.keys()))` -- iterating
    # a SET, so the order came from string hash randomization and changed every
    # process. That mattered well beyond cosmetics: the AI refresh scripts key
    # their stores by bare ticker, so for a ticker in more than one watchlist
    # (one is in three) it was last-write-wins with a nondeterministic winner,
    # and the Expert Take prompt embeds the market name and benchmark -- so
    # which analysis survived, and which benchmark the model was shown, varied
    # run to run.
    extra = sorted(set(data.keys()) - registry_keys)
    ordered = [k for k in load_markets_registry().keys()] + extra
    return {k: data.get(k, []) for k in ordered}


def save_watchlists(watchlists):
    atomic_write_json(WATCHLIST_FILE, watchlists)


def load_watchlist(market):
    return load_watchlists().get(market, [])


def save_watchlist(market, tickers):
    all_lists = load_watchlists()
    all_lists[market] = tickers
    save_watchlists(all_lists)


def load_interested():
    """Returns the set of tickers flagged "Interested" in the watchlist editor.

    A plain list on disk, not the {ticker: weight} dict this replaced -- the
    flag is boolean, and the old dict existed only to carry a portfolio weight
    that every entry set to 1.0 (i.e. equal-weight, the same as no weights at
    all). Missing file means nothing is flagged yet.
    """
    return set(read_json_strict(INTERESTED_FILE, []) or [])


def save_interested(tickers):
    """Writes the flagged tickers as a sorted list, so the file diffs cleanly
    when it's pushed to GitHub rather than reshuffling on every save."""
    atomic_write_json(INTERESTED_FILE, sorted(tickers))


def load_ticker_index():
    """Returns {"TICKER": {"index": "S&P 500", "benchmark": "SPY"}, ...} --
    each ticker's permanently-assigned index, set once by
    assign_ticker_index_if_missing() and never overwritten automatically.
    Same flat "ticker -> small dict" shape as ticker_notes.json.

    Bootstraps an empty file on first call if missing (same pattern as
    load_markets_registry()/load_watchlist_groups()) -- not just for
    consistency: github_sync.push_all_config() refuses to push ANY file in
    its list that doesn't exist on disk, so this is what guarantees
    ticker_index.json is always there once fetch_all_markets has run at
    least once, even if every currently-registered ticker already has an
    assignment and backfill_ticker_indices() has nothing new to write."""
    if not os.path.exists(TICKER_INDEX_FILE):
        save_ticker_index({})
        return {}
    data = read_json_strict(TICKER_INDEX_FILE, {})
    return data if isinstance(data, dict) else {}


def save_ticker_index(data):
    atomic_write_json(TICKER_INDEX_FILE, data)


def tradingview_url(ticker):
    """Best-effort TradingView chart URL for a yfinance-style ticker.
    TradingView resolves a bare symbol (no exchange prefix) to its listed
    exchange automatically (e.g. symbol=IBM -> NYSE:IBM chart), so we
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
    on an India ticker (IPO'd Feb 2024, only 30 monthly bars): the old
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


def compute_adx(ohlc_df, period=14):
    """Wilder's ADX (trend STRENGTH, direction-agnostic -- 25+ is the classic
    "trending" threshold; it says nothing about which way).

    Two-stage Wilder smoothing:
      1. TR / +DM / -DM are smoothed by wilder_smooth(), whose "seed from
         bars 1..period, nothing before that" convention is exactly right
         here -- bar 0 has no prior close, so all three are undefined there.
         +DI and -DI first exist at bar `period`.
      2. ADX is a SECOND Wilder smoothing, of DX. This one cannot reuse
         wilder_smooth(): that helper seeds from raw[1:period+1], skipping
         index 0, which is correct for a .diff()-derived series but would
         drop the first genuine DX value here. Wilder seeds ADX from the mean
         of the FIRST `period` DX values, so the recursion is written out.

    First non-NaN ADX therefore lands at bar 2*period - 1 (bar 27 for the
    default 14), matching TradingView. Returns an all-NaN series when there
    isn't enough history rather than raising.
    """
    high = ohlc_df["High"].astype(float)
    low = ohlc_df["Low"].astype(float)
    close = ohlc_df["Close"].astype(float)
    n = len(ohlc_df)

    nan_series = pd.Series(np.nan, index=ohlc_df.index)
    if n < 2 * period:
        return nan_series

    up_move = high.diff()
    down_move = -low.diff()
    # Only the LARGER of the two moves counts, and only when positive --
    # an inside bar contributes no directional movement at all.
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
                        index=ohlc_df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
                         index=ohlc_df.index)

    prev_close = close.shift(1)
    true_range = pd.concat([high - low,
                            (high - prev_close).abs(),
                            (low - prev_close).abs()], axis=1).max(axis=1)

    # np.where turned bar 0's NaN comparisons into 0.0; restore the NaN so
    # wilder_smooth's seed window is bars 1..period, not 0..period-1.
    plus_dm.iloc[0] = np.nan
    minus_dm.iloc[0] = np.nan
    true_range.iloc[0] = np.nan

    atr = wilder_smooth(true_range, period)
    # A zero ATR (a dead, gapless stretch) would make DI infinite; NaN
    # propagates through to ADX as "unknown", which is the honest answer.
    atr = atr.replace(0, np.nan)
    plus_di = 100 * wilder_smooth(plus_dm, period) / atr
    minus_di = 100 * wilder_smooth(minus_dm, period) / atr

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum

    dx_valid = dx.iloc[period:]
    if len(dx_valid) < period or pd.isna(dx_valid.iloc[:period]).any():
        return nan_series

    seed = dx_valid.iloc[:period].mean()
    tail = pd.concat([pd.Series([seed]), dx_valid.iloc[period:]], ignore_index=True)
    smoothed = tail.ewm(alpha=1 / period, adjust=False).mean()

    result = nan_series.copy()
    result.iloc[2 * period - 1:] = smoothed.values
    return result


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


def compute_vstop_tv(ohlc_df, length=VSTOP_LENGTH, factor=VSTOP_FACTOR):
    """Exact port of TradingView's built-in 'Volatility Stop' indicator
    (Pine v6 `volStop`, Source=close hard-coded, Length/Multiplier from
    settings). This is the primary engine (vstop_mode="tv"); the legacy
    compute_vstop() above remains available as vstop_mode="app".

    Differences vs the legacy engine that make it match TradingView:
      * The stop anchors to the running max/min of close SINCE the last
        stop-and-reverse flip (not the current close), so a stop keeps
        ratcheting with the trend instead of freezing at the flip price.
      * On a flip the running max/min reset to the current close, so the
        new stop starts right at close +/- factor*ATR and re-ratchets.
      * ATR is Pine's ta.atr (Wilder RMA seeded with the SMA of the first
        `length` true ranges) -- same convention as compute_atr().

    Returns (vstop_series, direction_series) with the same 1/-1 direction
    convention as compute_vstop(), so downstream flip/Up/Down logic is
    shared.
    """
    atr = compute_atr(ohlc_df, length) * factor
    close = ohlc_df["Close"].to_numpy(dtype=float)
    atr_np = atr.to_numpy(dtype=float)
    n = len(ohlc_df)

    vstop = np.full(n, np.nan)
    direction = np.full(n, np.nan)

    rmax = rmin = None
    stop = None
    uptrend = True
    started = False

    for i in range(n):
        src = close[i]
        a = atr_np[i]
        if np.isnan(a):
            if rmax is not None:
                rmax = max(rmax, src)
                rmin = min(rmin, src)
            continue

        if not started:
            started = True
            rmax = rmin = src
            stop = src - a
            vstop[i] = stop
            direction[i] = 1
            continue

        prev_stop = stop
        prev_uptrend = uptrend
        rmax = max(rmax, src)
        rmin = min(rmin, src)

        if prev_uptrend:
            stop = max(prev_stop, rmax - a)
        else:
            stop = min(prev_stop, rmin + a)

        # Trend is read off the stop JUST ratcheted, as Pine's volStop does:
        #     stop := uptrend ? max(stop, max - atrM) : min(stop, min + atrM)
        #     uptrend := src - stop >= 0.0
        # This used to test `src - prev_stop` before ratcheting. Because the stop
        # only ever moves in the trend's favour, that test is more forgiving: a
        # close between the old stop and the ratcheted one stayed "Up" while
        # holding a stop ABOVE the close, a state a stop-and-reverse can't be
        # in, and the flip landed a bar late. The flip DATE feeds
        # vstop_weekly_weeks_since_change, tech_uptrend and the auto-flag, and on
        # synthetic series it was off by up to 10 weeks.
        uptrend = (src - stop) >= 0.0

        if uptrend != prev_uptrend:
            rmax = rmin = src
            stop = (rmax - a) if uptrend else (rmin + a)

        vstop[i] = stop
        direction[i] = 1 if uptrend else -1

    return pd.Series(vstop, index=ohlc_df.index), pd.Series(direction, index=ohlc_df.index)


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


# TA Rules outcomes, exactly as TheWrap's flowchart words them. Best-first,
# which is also filters.CATEGORICAL_METRICS' declaration (sort) order.
TA_RULES_OUTCOMES = (
    "Bullish Signal", "Maintain/Add", "Wait/Watch",
    "Momentum Fading", "Be Cautious", "Exit",
)


def completed_weekly_bars(weekly, ticker, now=None):
    """`weekly` minus its trailing bar when that week is still forming.

    A W-FRI bar is complete once its Friday is past in the exchange's own
    timezone, or it IS that Friday and the session has settled. Deliberately
    not the VStop test (is the Friday label a printed trading day?): that one
    never releases a week whose Friday is a holiday, and it counts a Friday
    bar as complete while Friday's session is still trading."""
    if weekly is None or len(weekly) == 0:
        return weekly
    now = now or datetime.now(ZoneInfo("UTC"))
    tz = EXCHANGE_SESSIONS[exchange_session(ticker)][0]
    local_today = now.astimezone(ZoneInfo(tz)).date()
    friday = weekly.index[-1].date()
    if friday > local_today or (friday == local_today and forming_session_date(ticker, now) is not None):
        return weekly.iloc[:-1]
    return weekly


def find_sr_zones(weekly, pivot_weeks=3, reaction_pct=8.0, reaction_weeks=8,
                  zone_pct=3.0, min_touches=2):
    """Support/resistance zones from weekly WICKS: prices where the stock turned
    around at least `min_touches` times.

    A swing high is a week whose High tops the +/- `pivot_weeks` weeks around
    it (strictly above the earlier ones, so a flat double-top week pair counts
    once) and after which price RETREATED at least `reaction_pct` within
    `reaction_weeks` weeks. A swing low is the mirror image, with a REBOUND.
    Without the reaction test every sideways wiggle is a "level".

    Pivots within `zone_pct` of the lowest one in a group form a zone. Highs
    and lows count alike: a zone is not support or resistance by origin, only
    by which side of it price is on -- a broken ceiling that later holds as a
    floor is one level with more touches (role reversal).

    Returns [{"low", "high", "touches", "last_touch"}], lowest first."""
    n = len(weekly)
    if n < 2 * pivot_weeks + 2:
        return []
    highs = weekly["High"].to_numpy(dtype=float)
    lows = weekly["Low"].to_numpy(dtype=float)
    up, down = 1 + reaction_pct / 100, 1 - reaction_pct / 100
    pivots = []   # (price, bar position)
    # The last `pivot_weeks` bars can't be pivots yet: their right side hasn't
    # happened. So the breakout week can never create its own level.
    for i in range(pivot_weeks, n - pivot_weeks):
        before = slice(i - pivot_weeks, i)
        after = slice(i + 1, i + 1 + pivot_weeks)
        reaction = slice(i + 1, min(n, i + 1 + reaction_weeks))
        if (highs[i] > highs[before].max() and highs[i] >= highs[after].max()
                and lows[reaction].min() <= highs[i] * down):
            pivots.append((highs[i], i))
        if (lows[i] < lows[before].min() and lows[i] <= lows[after].min()
                and highs[reaction].max() >= lows[i] * up):
            pivots.append((lows[i], i))

    zones, group = [], []

    def _close_group():
        if len(group) >= min_touches:
            zones.append({
                "low": round(float(min(p for p, _ in group)), 2),
                "high": round(float(max(p for p, _ in group)), 2),
                "touches": len(group),
                "last_touch": weekly.index[max(i for _, i in group)].strftime("%Y-%m-%d"),
            })

    for price, i in sorted(pivots):
        if group and price > group[0][0] * (1 + zone_pct / 100):
            _close_group()
            group = []
        group.append((price, i))
    _close_group()
    return zones


def compute_ta_rules(close, ema_fast, ema_mid, ema_slow, zones, recent_closes,
                     converge_pct=3.0, break_pct=3.0, periods=(10, 20, 40)):
    """TheWrap's TA Rules flowchart, node for node. All inputs are the LAST
    COMPLETED weekly bar: its close and the three WEMAs as of that week.

      EMAs converging?  (max - min of the 3 WEMAs <= converge_pct of the slow one)
        Yes -> broken support?     -> Exit
               broken resistance?  -> Bullish Signal
               else                -> Wait/Watch
        No  -> broken slow WEMA?   -> Exit
               broken mid WEMA?    -> Be Cautious
               broken fast WEMA?   -> Momentum Fading
               else                -> Maintain/Add

    "Broken" = the close is more than break_pct past the line. For an S/R zone
    it also needs one of `recent_closes` (the weeks just before this one) on
    the zone's other side: otherwise every zone from years ago that price now
    sits far below would read as broken support. No zones at all falls through
    to Wait/Watch, the chart's own No -> No path.

    Returns (verdict, detail) -- detail feeds app.ta_rules_tooltip."""
    buf = break_pct / 100
    spread_pct = (max(ema_fast, ema_mid, ema_slow) - min(ema_fast, ema_mid, ema_slow)) / ema_slow * 100
    converging = spread_pct <= converge_pct
    detail = {
        "close": round(float(close), 2),
        "ema_fast": round(float(ema_fast), 2),
        "ema_mid": round(float(ema_mid), 2),
        "ema_slow": round(float(ema_slow), 2),
        "periods": list(periods),
        "spread_pct": round(float(spread_pct), 2),
        "converge_pct": converge_pct,
        "break_pct": break_pct,
        "converging": converging,
    }

    if converging:
        broken_support = [z for z in zones if close < z["low"] * (1 - buf)
                          and any(c >= z["low"] for c in recent_closes)]
        broken_resistance = [z for z in zones if close > z["high"] * (1 + buf)
                             and any(c <= z["high"] for c in recent_closes)]
        below = [z for z in zones if z["low"] <= close]
        above = [z for z in zones if z["high"] >= close]
        detail.update({
            "zone_count": len(zones),
            "support": max(below, key=lambda z: z["low"]) if below else None,
            "resistance": min(above, key=lambda z: z["high"]) if above else None,
        })
        if broken_support:
            # The chart asks about support first, so a close that breaks both
            # a support and a resistance zone (a wild week) reads Exit.
            detail["decided_by"] = {"test": "support", "zone": max(broken_support, key=lambda z: z["low"])}
            return "Exit", detail
        if broken_resistance:
            detail["decided_by"] = {"test": "resistance", "zone": max(broken_resistance, key=lambda z: z["high"])}
            return "Bullish Signal", detail
        return "Wait/Watch", detail

    for name, ema, verdict in (("slow", ema_slow, "Exit"),
                               ("mid", ema_mid, "Be Cautious"),
                               ("fast", ema_fast, "Momentum Fading")):
        if close < ema * (1 - buf):
            detail["decided_by"] = {"test": name}
            return verdict, detail
    return "Maintain/Add", detail


def _fetch_info_with_retry(yf_t, ticker, attempts=3, base_delay=3):
    """yf.Ticker.info makes one HTTP request per ticker with no built-in retry --
    back-to-back calls across a full watchlist reliably trip Yahoo's rate limiter
    (429 Too Many Requests / 401 Invalid Crumb), especially from a shared/datacenter
    IP like Streamlit Cloud's. Retries with exponential backoff on those specific
    transient errors; gives up (returns {}) after exhausting attempts so callers
    keep their existing None-safe behavior.

    ALWAYS returns a dict. Under rate limiting yfinance does not always raise --
    it can hand back None instead, and that sailed straight through this
    function's only-catches-exceptions guard. detect_ticker_index then did
    info.get("currency") on None and took the whole Streamlit app down with
    "AttributeError: 'NoneType' object has no attribute 'get'" the moment
    someone added a ticker while Yahoo was throttling. The docstring already
    promised callers a dict; now the code actually keeps that promise."""
    for attempt in range(1, attempts + 1):
        try:
            info = yf_t.info
            return info if isinstance(info, dict) else {}
        except Exception as e:
            msg = str(e)
            if attempt < attempts and any(s in msg for s in ("Too Many Requests", "Rate limited", "Invalid Crumb", "401", "429")):
                time.sleep(base_delay * attempt)
                continue
            print(f"  [{ticker}] .info fetch failed: {e}")
            return {}
    return {}


def detect_ticker_index(ticker):
    """Best-effort, ONE-TIME index detection for a ticker that has no
    ticker_index.json entry yet, via a live yfinance .info lookup (reusing
    the same retry wrapper fetch_snapshot uses for company name/fundamentals).
    Returns (index_name, benchmark) or (None, None) if it can't be classified
    confidently -- callers fall back to the watchlist's registered benchmark
    in that case, same as before this feature existed, rather than guessing
    wrong and silently mis-benchmarking a ticker.

    Currency is checked before exchange code: it's the more stable signal
    (INR/USD), whereas yfinance's `exchange` field uses short, sometimes
    ambiguous codes (NSI/BSE for India; NMS/NYQ/NGM/ASE/PCX/BATS for the
    major US venues) that are only consulted as a fallback.
    """
    info = _fetch_info_with_retry(yf.Ticker(ticker), ticker)
    currency = (info.get("currency") or "").upper()
    exchange = (info.get("exchange") or "").upper()
    if currency == "INR" or exchange in ("NSI", "BSE"):
        return "Nifty 500", INDEX_DEFINITIONS["Nifty 500"]
    if currency == "USD" or exchange in ("NMS", "NYQ", "NGM", "ASE", "PCX", "BATS"):
        return "S&P 500", INDEX_DEFINITIONS["S&P 500"]
    return None, None


def assign_ticker_index_if_missing(ticker, ticker_index=None):
    """Returns this ticker's index assignment, detecting and persisting it
    the FIRST time it's ever seen and leaving it untouched on every call
    after that -- the "pick it once at setup, never auto-change it again"
    behavior. Pass an already-loaded `ticker_index` dict when assigning many
    tickers in a row (see backfill_ticker_indices) to avoid re-reading the
    file on every call; omit it for a one-off lookup."""
    owns_dict = ticker_index is None
    if owns_dict:
        ticker_index = load_ticker_index()
    existing = ticker_index.get(ticker)
    if existing:
        return existing
    index_name, benchmark = detect_ticker_index(ticker)
    if index_name is None:
        return None
    entry = {"index": index_name, "benchmark": benchmark}
    ticker_index[ticker] = entry
    if owns_dict:
        save_ticker_index(ticker_index)
    return entry


def backfill_ticker_indices(tickers):
    """Assigns an index to every ticker in `tickers` that doesn't already
    have one, in a single load/save pass (not one file write per ticker).
    Safe to call on every fetch_all_markets run -- already-assigned tickers
    are skipped without a network call, so steady-state cost is zero."""
    ticker_index = load_ticker_index()
    missing = [t for t in tickers if t not in ticker_index]
    if not missing:
        return
    for t in missing:
        assign_ticker_index_if_missing(t, ticker_index=ticker_index)
    save_ticker_index(ticker_index)


def _download_with_retries(all_tickers, period, attempts=3, timeout=90, wait=30):
    """Bulk yf.download() with a hard per-attempt timeout (yfinance/Yahoo can
    hang or stall with no native timeout of its own) and a retry-with-backoff
    loop, the same llm_util.call_with_timeout the LLM calls use. Raises the
    last error if all attempts fail, so a persistent outage fails the job
    clearly instead of hanging indefinitely or silently proceeding with
    partial/no data.

    call_with_timeout, not a ThreadPoolExecutor: the executor form abandoned a
    NON-daemon worker on timeout, which the interpreter joins at exit -- the
    exact hang llm_util.generate_with_timeout was fixed for on 2026-09-17."""
    from llm_util import call_with_timeout
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return call_with_timeout(
                lambda: yf.download(all_tickers, period=period, interval="1d",
                                    group_by="ticker", auto_adjust=True, progress=False, threads=True),
                timeout, name="yf.download",
            )
        except TimeoutError as e:
            # `e`'s own message, not a rebuilt one: call_with_timeout already
            # says "yf.download timed out after {timeout}s", and a timeout
            # raised by yfinance ITSELF (socket.timeout is TimeoutError since
            # 3.10) would otherwise be relabelled as having taken the full
            # budget it never used.
            last_exc = TimeoutError(f"{e} (attempt {attempt}/{attempts})")
        except Exception as e:
            last_exc = e
        print(f"  [yf.download attempt {attempt}/{attempts} failed] {last_exc}")
        if attempt < attempts:
            time.sleep(wait)
    raise last_exc


def _safe_fetch(fn, label=""):
    """Wraps a yfinance property call so a network error on one statement
    (cash_flow, income_stmt, balance_sheet) doesn't abort the others."""
    try:
        result = fn()
        return result if result is not None and not getattr(result, "empty", False) else None
    except Exception as e:
        print(f"  {label} fetch failed: {e}")
        return None


# Fewest daily bars fetch_snapshot will compute a row from. A ticker below it
# (typically a fresh listing) gets no row at all.
MIN_DAILY_BARS = 60
# How long a short-history record is trusted without being seen again. See
# merge_short_history for why it expires at all.
SHORT_HISTORY_EXPIRY_DAYS = 120


def merge_short_history(previous, fresh, per_market, watchlists, today=None):
    """Combine the snapshot's record of tickers skipped for having fewer than
    MIN_DAILY_BARS bars. Returns {ticker: {"bars": n, "seen": "YYYY-MM-DD"}}.

    WHY this exists: fetch_snapshot silently skips such a ticker, so it never
    gets a snapshot row, fill_snapshot_gaps has nothing to recover, and
    snapshot_is_usable -- which requires a row for every watchlist ticker --
    failed on every page load. From ~2026-08-17 one SME listing
    (1 bar on Yahoo), kept the "Data snapshot is out of date (settings, code,
    or watchlist changed...)" banner up for every visitor, for a reason the
    banner doesn't mention, so the banner stopped meaning anything. Recording the
    skip lets snapshot_is_usable tell "can't be computed yet" from "missing".

    Union, then prune -- so every writer (full refresh, scoped refresh,
    watchlist save, Refresh Data) can pass only what IT learned and nothing
    learned earlier is lost:
      - fresh entries override previous ones;
      - a ticker that now HAS a row is dropped (it has grown enough history);
      - a ticker no longer in any watchlist is dropped;
      - an entry not re-seen for SHORT_HISTORY_EXPIRY_DAYS is dropped. That
        valve matters: a record must not excuse a ticker forever. Past it, a
        still-rowless ticker counts as missing again and the banner comes
        back, which is the honest state."""
    today = today or datetime.now(ZoneInfo("America/New_York")).date()
    merged = dict(previous or {})
    merged.update(fresh or {})
    with_rows = {r.get("ticker") for rows in (per_market or {}).values() for r in rows}
    in_watchlists = {t for tickers in (watchlists or {}).values() for t in tickers}
    out = {}
    for t, rec in merged.items():
        if t in with_rows or t not in in_watchlists:
            continue
        seen = _parse_data_end((rec or {}).get("seen"))
        if seen is None or (today - seen).days > SHORT_HISTORY_EXPIRY_DAYS:
            continue
        out[t] = rec
    return out


# Regular-session close per exchange, in the exchange's own timezone, plus a
# settle margin before a closing print is trusted (same 30m the market-breadth
# job uses in refresh_market_breadth.trim_to_completed_session).
EXCHANGE_SESSIONS = {
    "INDIA": ("Asia/Kolkata", 15, 30),
    "US": ("America/New_York", 16, 0),
}
SESSION_SETTLE_MINUTES = 30
# Index tickers carry no .NS/.BO suffix, so the suffix test alone would file
# the Nifty 500 benchmark under the US session.
INDIA_INDEX_TICKERS = {BENCHMARKS["INDIA"], "^NSEI", "^NSEBANK", "^BSESN"}


def exchange_session(ticker):
    """"INDIA" or "US" -- resolved off the ticker itself, same reasoning as
    get_exchange_label: .NS/.BO (and the India index tickers) are Indian
    listings whichever watchlist holds them, everything else is US-listed."""
    if ticker.endswith((".NS", ".BO")) or ticker in INDIA_INDEX_TICKERS:
        return "INDIA"
    return "US"


def forming_session_date(ticker, now=None):
    """The local date of this ticker's still-forming session, or None when its
    exchange has settled.

    One definition, shared by the two places that need it: drop_forming_daily_bars
    (which blanks such a bar out of a fetch) and reject_stale_rows (which must not
    prefer a STORED row that contains one). They used to disagree, because only
    the first existed -- see reject_stale_rows' completed_sessions_only note.
    """
    now = now or datetime.now(ZoneInfo("UTC"))
    tz, close_h, close_m = EXCHANGE_SESSIONS[exchange_session(ticker)]
    local_now = now.astimezone(ZoneInfo(tz))
    settled = local_now.replace(hour=close_h, minute=close_m, second=0, microsecond=0) \
        + timedelta(minutes=SESSION_SETTLE_MINUTES)
    return local_now.date() if local_now < settled else None


def drop_forming_daily_bars(raw, tickers, now=None):
    """Blank out each ticker's trailing daily bar when its exchange session is
    still open (or closed less than SESSION_SETTLE_MINUTES ago). Returns
    (raw, [tickers trimmed]); `raw` is the group_by="ticker" frame from
    _download_with_retries and is modified in place.

    WHY: the nightly alert and weekly wrap-up jobs are scheduled for 9 PM ET,
    before NSE opens at 23:45 ET, but GitHub starts them hours late -- they
    actually ran at ~01:45-02:15 ET all through 2026-09-07..13, mid-NSE-session.
    yfinance hands back the forming bar, so every India row was judged on a
    partial day (the 2026-09-11 01:42 ET snapshot already carried data_end
    2026-09-11 for all 31 India Invested rows). An edge-triggered alert can
    fire on an intraday cross that reverses by the close, and it never
    re-fires. Dropping the forming bar makes the job read the last COMPLETED
    session whenever it happens to run.

    Per ticker, not per frame: one download mixes exchanges (a US ticker can
    sit in the ^CRSLDX group), and the frame's union calendar has a row for
    today as soon as ANY ticker in it traded today. Only the dashboard's live
    view wants forming bars, so this is opt-in (see fetch_snapshot)."""
    now = now or datetime.now(ZoneInfo("UTC"))
    trimmed = []
    if raw is None or raw.empty:
        return raw, trimmed
    for t in tickers:
        if t not in raw.columns.get_level_values(0):
            continue
        last_idx = raw[t].dropna(how="all").index
        if len(last_idx) == 0:
            continue
        last = last_idx[-1]
        if last.date() == forming_session_date(t, now):
            raw.loc[last, t] = np.nan
            trimmed.append(t)
    return raw, trimmed


def _statement_row(frame, row_name):
    """One statement row as a Series, NaNs dropped.

    yfinance sometimes hands back a frame with the SAME row label twice, and
    .loc on a duplicated label returns a DataFrame rather than a Series. Every
    downstream test then evaluates elementwise -- `eps_q.iloc[4] > 0` becomes a
    Series, and `if` on it raises "truth value of a Series is ambiguous". That
    raise is caught by fetch_snapshot's statements-block except, so the ticker
    silently loses ROCE, CFO/OP 5Y and both growth metrics at once, with only a
    log line to say so. Take the first occurrence, which is what a
    non-duplicated frame would have given.
    """
    row = frame.loc[row_name]
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0]
    return row.dropna()


def _roce_from_statements(inc, bs):
    """ROCE % = Operating Income / (Stockholders Equity + Long Term Debt), all
    from ONE fiscal year: the latest year that reports both equity and
    operating income. Returns None when it can't be computed honestly.

    Three failures in the version this replaces, each picking up numbers that
    looked real:
      - Every input was `.dropna().iloc[0]` on its own row, so a blank latest
        cell silently borrowed an OLDER year. Yahoo blanks Long Term Debt in
        the year a company pays it off (Total Debt that year is only current
        debt + leases), so one ticker was divided by FY2024's 1,143M of since-repaid
        notes, another by FY2025's 1,829M, a third by FY2023's 283M -- 11 of 122
        tickers in the 2026-09-14 data.
      - With no Long Term Debt row at all it fell back to Total Debt (short-term
        debt + leases), so the column mixed two definitions (6 tickers).
      - A missing equity row became 0.0, so capital employed collapsed to debt
        alone and a low-debt company showed a four-digit ROCE.
    Long Term Debt that is blank or absent at the chosen date counts as 0,
    which is what Yahoo's blanks mean in the cases above. A zero or negative
    capital employed (negative equity) gives None rather than a sign-flipped
    ratio."""
    if "Operating Income" not in inc.index or "Stockholders Equity" not in bs.index:
        return None
    equity_row = bs.loc["Stockholders Equity"]
    op_row = inc.loc["Operating Income"]
    # One ticker (2026-09-14) had FY2026 equity but no FY2026 operating income; the
    # old code paired that equity with FY2025 income.
    years = [d for d in bs.columns
             if d in inc.columns and pd.notna(equity_row.get(d)) and pd.notna(op_row.get(d))]
    if not years:
        return None
    year = max(years)
    equity = float(equity_row[year])
    debt = 0.0
    if "Long Term Debt" in bs.index and pd.notna(bs.loc["Long Term Debt"].get(year)):
        debt = float(bs.loc["Long Term Debt"][year])
    cap_emp = equity + debt
    if cap_emp <= 0:
        return None
    return round(float(op_row[year]) / cap_emp * 100, 1)


def fetch_snapshot(tickers, benchmark="SPY", period="5y", settings=None, completed_sessions_only=False,
                   short_history=None):
    """Returns (results list of dicts, as_of timestamp string) for one market's tickers.

    `short_history`, if given, is a dict filled with {ticker: {"bars", "seen"}}
    for every ticker skipped for having fewer than MIN_DAILY_BARS bars -- see
    merge_short_history. A ticker Yahoo returned nothing for is NOT recorded:
    that is a failed fetch, handled by fill_snapshot_gaps.

    completed_sessions_only drops each ticker's still-forming daily bar (see
    drop_forming_daily_bars) -- for the headless alert/wrap-up jobs, which must
    judge closes. The dashboard leaves it off so it keeps showing live prices."""
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
    ta_converge_pct = float(settings.get("ta_converge_pct", 3.0))
    ta_break_pct = float(settings.get("ta_break_pct", 3.0))
    ta_sr_lookback_weeks = int(settings.get("ta_sr_lookback_weeks", 156))
    ta_sr_kwargs = {
        "pivot_weeks": int(settings.get("ta_sr_pivot_weeks", 3)),
        "reaction_pct": float(settings.get("ta_sr_reaction_pct", 8.0)),
        "reaction_weeks": int(settings.get("ta_sr_reaction_weeks", 8)),
        "zone_pct": float(settings.get("ta_sr_zone_pct", 3.0)),
        "min_touches": int(settings.get("ta_sr_min_touches", 2)),
    }
    ta_sr_recent_weeks = int(settings.get("ta_sr_recent_weeks", 13))

    if not tickers:
        return [], datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")

    all_tickers = list(tickers) + [benchmark]
    raw = _download_with_retries(all_tickers, period)
    if completed_sessions_only:
        # Before the benchmark series below are derived, so RS and relative
        # returns compare completed sessions on both sides.
        raw, _forming = drop_forming_daily_bars(raw, all_tickers)
        if _forming:
            print(f"  [fetch_snapshot] dropped still-forming daily bar for {len(_forming)} ticker(s): "
                  f"{', '.join(_forming[:10])}{' ...' if len(_forming) > 10 else ''}")

    bench_daily = raw[benchmark]["Close"].dropna()
    bench_df = raw[benchmark].dropna(how="all")
    bench_weekly = weekly_resample(bench_df)["Close"]
    bench_monthly = monthly_resample(bench_df)["Close"]

    results = []

    for t in tickers:
        company_name = t
        qtr_profit_growth = None
        qtr_revenue_growth = None
        qtr_eps_growth = None
        # Assigned inside the statements try/except below; without a default
        # here a ticker whose statement fetch raises would NameError at
        # results.append instead of reporting blanks.
        ttm_profit_growth = ttm_revenue_growth = None
        roe = cfo_op_5yr = roce = None
        perf_1m = perf_3m = perf_6m = perf_1y = perf_3y = None
        trailing_pe = forward_pe = pb_ratio = ev_ebitda = p_cashflow = reported_qtr = None
        # Reset per ticker like everything above. If yf.Ticker() below raised,
        # the statements block used to read the PREVIOUS ticker's object and
        # store that company's ROCE/growth figures under this symbol.
        yf_t = None
        try:
            yf_t = yf.Ticker(t)
            info = _fetch_info_with_retry(yf_t, t)
            company_name = info.get("longName") or info.get("shortName") or t
            # Yahoo's own YoY quarterly growth reads (this quarter vs. the
            # same quarter last year) -- returned as decimals (0.278 = 27.8%).
            earnings_growth = info.get("earningsQuarterlyGrowth")
            revenue_growth = info.get("revenueGrowth")

            trailing_pe = info.get("trailingPE")
            forward_pe = info.get("forwardPE")
            pb_ratio = info.get("priceToBook")
            ev_ebitda = info.get("enterpriseToEbitda")

            p_cashflow = None
            market_cap = info.get("marketCap") or info.get("nonDilutedMarketCap")
            ocf = info.get("operatingCashflow")
            if market_cap and ocf and ocf != 0:
                p_cashflow = round(market_cap / ocf, 2)

            reported_qtr = None
            mrq_ts = info.get("mostRecentQuarter")
            if mrq_ts:
                dt = datetime.fromtimestamp(mrq_ts)
                m, y = dt.month, dt.year
                if t.endswith(".NS") or t.endswith(".BO"):
                    q = "Q1" if m in (4, 5, 6) else "Q2" if m in (7, 8, 9) else "Q3" if m in (10, 11, 12) else "Q4"
                    fy = y if m > 3 else y - 1
                    reported_qtr = f"{q} FY{str(fy + 1)[-2:]}"
                else:
                    q = "Q1" if m in (1, 2, 3) else "Q2" if m in (4, 5, 6) else "Q3" if m in (7, 8, 9) else "Q4"
                    reported_qtr = f"{q} {y}"

            if earnings_growth is not None:
                qtr_profit_growth = round(earnings_growth * 100, 1)
            if revenue_growth is not None:
                qtr_revenue_growth = round(revenue_growth * 100, 1)

            raw_roe = info.get("returnOnEquity")
            if raw_roe is not None:
                roe = round(raw_roe * 100, 1)
        except Exception as e:
            print(f"  [{t}] Could not fetch company name: {e}")

        # Annual statements: each fetched independently so a timeout on one
        # doesn't zero out metrics computed from the others.
        try:
            if yf_t is None:
                raise RuntimeError("no yfinance Ticker object for this symbol")
            cf  = _safe_fetch(lambda: yf_t.cash_flow,    label=f"[{t}] cash_flow")
            inc = _safe_fetch(lambda: yf_t.income_stmt,   label=f"[{t}] income_stmt")
            bs  = _safe_fetch(lambda: yf_t.balance_sheet, label=f"[{t}] balance_sheet")
            qinc = _safe_fetch(lambda: yf_t.quarterly_income_stmt,
                               label=f"[{t}] quarterly_income_stmt")

            # Diluted EPS YoY for the most recent quarter. Unlike Yahoo's
            # earningsQuarterlyGrowth (which is net income, and drives
            # qtr_profit_growth above), this nets out share issuance -- profit
            # growth funded by a QIP or warrant conversion doesn't accrue per
            # share. Columns come back newest-first, so iloc[4] is the same
            # quarter a year earlier. A non-positive base makes percentage
            # growth meaningless, so leave it blank rather than emit nonsense.
            if qinc is not None and "Diluted EPS" in qinc.index:
                eps_q = _statement_row(qinc, "Diluted EPS")
                if len(eps_q) >= 5 and eps_q.iloc[4] > 0:
                    qtr_eps_growth = round((eps_q.iloc[0] / eps_q.iloc[4] - 1) * 100, 1)

            # Trailing-twelve-month growth: the last 4 quarters against the 4
            # before them. This is NOT the same number as qtr_profit_growth /
            # qtr_revenue_growth above, which are Yahoo's SINGLE-quarter YoY
            # reads -- one soft quarter swings those hard, while TTM averages
            # the seasonality out. The Turbo Surge scan specifies TTM, so
            # substituting the quarterly figure would quietly change the screen.
            #
            # Needs 8 quarters, and Yahoo returns only ~5 for every ticker
            # checked (US and India alike), so in practice BOTH of these are
            # None today. Kept because they are correct as written and light
            # up automatically if a deeper statement source is ever wired in;
            # the Turbo Surge rule uses qtr_profit_growth/qtr_revenue_growth
            # (single-quarter YoY) as the working substitute meanwhile.
            #
            # None, never 0, when the history is short -- a missing
            # denominator must not read as "no growth".
            def _ttm_growth(row_name):
                if qinc is None or row_name not in qinc.index:
                    return None
                q = _statement_row(qinc, row_name)
                if len(q) < 8:
                    return None
                recent = float(q.iloc[0:4].sum())
                prior = float(q.iloc[4:8].sum())
                # A non-positive base makes percentage growth meaningless
                # (and sign-flips it), same guard as qtr_eps_growth above.
                if prior <= 0:
                    return None
                return round((recent / prior - 1) * 100, 1)

            ttm_profit_growth = _ttm_growth("Net Income")
            ttm_revenue_growth = _ttm_growth("Total Revenue")

            if cf is not None and inc is not None:
                if "Operating Cash Flow" in cf.index and "Operating Income" in inc.index:
                    cfo_s = cf.loc["Operating Cash Flow"].dropna()
                    op_s  = inc.loc["Operating Income"].dropna()
                    dates = cfo_s.index.intersection(op_s.index).sort_values(ascending=False)[:5]
                    if len(dates) > 0:
                        op_sum = float(op_s.loc[dates].sum())
                        if op_sum != 0:
                            cfo_op_5yr = round(float(cfo_s.loc[dates].sum()) / op_sum, 2)

            if inc is not None and bs is not None:
                roce = _roce_from_statements(inc, bs)
        except Exception as e:
            print(f"  [{t}] Statement metrics failed: {e}")

        try:
            df = raw[t].dropna(how="all")
            if df.empty:
                continue
            if len(df) < MIN_DAILY_BARS:
                if short_history is not None:
                    short_history[t] = {
                        "bars": int(len(df)),
                        "seen": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d"),
                    }
                continue

            daily_close = df["Close"].dropna()

            def _perf(n):
                if len(daily_close) > n:
                    return round((float(daily_close.iloc[-1]) / float(daily_close.iloc[-n - 1]) - 1) * 100, 1)
                return None

            perf_1m = _perf(22)
            perf_3m = _perf(63)
            perf_6m = _perf(126)
            perf_1y = _perf(252)
            perf_3y = _perf(756)

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

            # Period-12 monthly RSI, kept SEPARATE from rsi14_monthly above:
            # the Alpha Leaders scan specifies RSI 12M, and on ~60 monthly bars
            # a period change of 2 moves the reading by several points, so
            # reusing the period-14 value would quietly shift the screen.
            # rsi_period stays settings-driven; RSI_MONTHLY_12_PERIOD does not,
            # because it is pinned by the scan definition, not a preference.
            rsi12_monthly = None
            if len(monthly) >= RSI_MONTHLY_12_PERIOD + 1:
                rsi12_monthly_series = compute_rsi(monthly["Close"], RSI_MONTHLY_12_PERIOD)
                if pd.notna(rsi12_monthly_series.iloc[-1]):
                    rsi12_monthly = round(float(rsi12_monthly_series.iloc[-1]), 1)

            # ADX -- trend STRENGTH, direction-agnostic. Weekly period 14 and
            # monthly period 12, as the Turbo Surge and Alpha Leaders scans
            # specify. compute_adx needs 2*period bars before it yields
            # anything, so it self-guards on short history and returns NaN.
            adx_weekly_14 = adx_monthly_12 = None
            adx_w_series = compute_adx(weekly, ADX_WEEKLY_PERIOD)
            if len(adx_w_series) and pd.notna(adx_w_series.iloc[-1]):
                adx_weekly_14 = round(float(adx_w_series.iloc[-1]), 1)
            adx_m_series = compute_adx(monthly, ADX_MONTHLY_PERIOD)
            if len(adx_m_series) and pd.notna(adx_m_series.iloc[-1]):
                adx_monthly_12 = round(float(adx_m_series.iloc[-1]), 1)

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

            # TA Rules (TheWrap flowchart, see compute_ta_rules). Judged on the
            # last COMPLETED week only -- the flowchart reads weekly closes, and
            # a mid-week dip must not flip a holding to Exit. EMAs are causal,
            # so recomputing them on the completed bars gives exactly the
            # full-series values as of that week.
            ta_rules = ta_rules_detail = None
            weekly_done = completed_weekly_bars(weekly, t)
            if len(weekly_done) >= w_slow + 1:
                closes_w = weekly_done["Close"]
                ta_rules, ta_rules_detail = compute_ta_rules(
                    float(closes_w.iloc[-1]),
                    float(closes_w.ewm(span=w_fast, adjust=False).mean().iloc[-1]),
                    float(closes_w.ewm(span=w_mid, adjust=False).mean().iloc[-1]),
                    float(closes_w.ewm(span=w_slow, adjust=False).mean().iloc[-1]),
                    find_sr_zones(weekly_done.iloc[-ta_sr_lookback_weeks:], **ta_sr_kwargs),
                    closes_w.iloc[:-1].iloc[-ta_sr_recent_weeks:].tolist() if ta_sr_recent_weeks > 0 else [],
                    converge_pct=ta_converge_pct, break_pct=ta_break_pct,
                    periods=(w_fast, w_mid, w_slow),
                )
                ta_rules_detail["week"] = weekly_done.index[-1].strftime("%Y-%m-%d")

            # Daily SMAs (fast/mid/slow, e.g. 10/50/200)
            ema10_daily = ema50 = ema200 = None
            crossed_below_10_daily = crossed_above_10_daily = False
            crossed_below_50 = crossed_above_50 = False
            crossed_below_200 = crossed_above_200 = False

            if len(daily_close) >= d_fast + 1:
                ema10_daily_series = daily_close.rolling(window=d_fast, min_periods=1).mean()
                ema10_daily = round(float(ema10_daily_series.iloc[-1]), 1)
                prev_close_d0 = daily_close.iloc[-2]
                prev_ema10_daily = ema10_daily_series.iloc[-2]
                crossed_below_10_daily = bool(prev_close_d0 >= prev_ema10_daily and daily_close.iloc[-1] < ema10_daily)
                crossed_above_10_daily = bool(prev_close_d0 < prev_ema10_daily and daily_close.iloc[-1] >= ema10_daily)
            if len(daily_close) >= d_mid + 1:
                ema50_series = daily_close.rolling(window=d_mid, min_periods=1).mean()
                ema50 = round(float(ema50_series.iloc[-1]), 1)
                prev_close_d = daily_close.iloc[-2]
                prev_ema50 = ema50_series.iloc[-2]
                crossed_below_50 = bool(prev_close_d >= prev_ema50 and daily_close.iloc[-1] < ema50)
                crossed_above_50 = bool(prev_close_d < prev_ema50 and daily_close.iloc[-1] >= ema50)
            if len(daily_close) >= d_slow + 1:
                ema200_series = daily_close.rolling(window=d_slow, min_periods=1).mean()
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

            # By default the VStop includes the trailing (in-progress) weekly
            # bar, matching TradingView's live Volatility Stop value. When
            # vstop_include_incomplete_week is off, only completed weekly bars
            # drive the VStop: a W-FRI bar whose Friday close hasn't actually
            # printed is incomplete (e.g. the Friday bar was missing from the
            # pull, or the trailing week is still forming). Its Close then
            # anchors to an earlier trading day and can falsely breach the
            # stop, flipping the stop-and-reverse. Trim any trailing bar(s)
            # whose Friday label is not a printed trading day.
            # RS/WEMA/RSI keep the full weekly series either way.
            weekly_complete = weekly
            if not settings.get("vstop_include_incomplete_week", True):
                daily_days = set(df.index.normalize())
                while len(weekly_complete) > 0 and weekly_complete.index[-1].normalize() not in daily_days:
                    weekly_complete = weekly_complete.iloc[:-1]

            if len(weekly_complete) >= max(vstop_length + 5, VSTOP_MIN_HISTORY_WEEKS):
                if settings.get("vstop_mode", "tv") == "app":
                    vstop_series, dir_series = compute_vstop(weekly_complete, length=vstop_length, factor=vstop_factor)
                else:
                    vstop_series, dir_series = compute_vstop_tv(weekly_complete, length=vstop_length, factor=vstop_factor)
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
                    else:
                        # Never flipped inside the window: the trend has held
                        # for at least the whole valid series. This used to stay
                        # None, and tech_uptrend requires a non-None value, so
                        # the strongest possible trend (Up for 5 years) scored
                        # Tech Uptrend 0, showed a blank VStop Weeks Ago, and
                        # gave the auto-flag a bearish vote. The count is a lower
                        # bound; vstop_weekly_last_change stays None because no
                        # flip date exists, and the table renders it as "N+".
                        vstop_weekly_weeks_since_change = int(len(valid_dir) - 1)

            # Length-14 weekly VStop, for the Turbo Surge scan's "Close >=
            # VSTOP 14W 2". Rides the same engine and the same weekly_complete
            # frame as the length-10 stop above -- only the length differs --
            # so the incomplete-week trimming and the vstop_mode choice both
            # carry over rather than being re-decided here. Only the level is
            # kept; the scan tests price against it and needs nothing else.
            vstop_weekly_14 = None
            if len(weekly_complete) >= max(VSTOP_LENGTH_14W + 5, VSTOP_MIN_HISTORY_WEEKS):
                if settings.get("vstop_mode", "tv") == "app":
                    vstop14_series, _ = compute_vstop(
                        weekly_complete, length=VSTOP_LENGTH_14W, factor=vstop_factor)
                else:
                    vstop14_series, _ = compute_vstop_tv(
                        weekly_complete, length=VSTOP_LENGTH_14W, factor=vstop_factor)
                if pd.notna(vstop14_series.iloc[-1]):
                    vstop_weekly_14 = round(float(vstop14_series.iloc[-1]), 1)

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
            # 20-session average volume, the denominator of the Volume Rocketing
            # surge test (10D >= 1.3x 20D). Deliberately a separate short-window
            # baseline from avg_volume_100d: against 100 days a stock that has
            # been busy for a month already looks normal, which is exactly the
            # move that scan is trying to catch.
            avg_volume_20d = round(float(daily_volume.tail(20).mean())) if len(daily_volume) >= 20 else None

            # Share of the last year's VOLUME that changed hands above today's
            # close -- how much stock is underwater and liable to sell into a
            # rally. Complements breakout_window rather than duplicating it:
            # that measures the AGE of the nearest barrier, this measures the
            # WEIGHT of all of it. One ticker carried 10 confirmed pivots above
            # today and ~30% of a year's volume trapped, while another had 12
            # pivots above yet 0%, because all of its overhead predates the
            # window -- counting levels misleads, weighing recent supply does not.
            #
            # Pairs Close and Volume with a SINGLE dropna. daily_close (:967)
            # and daily_volume above are dropped independently, so a bar missing
            # one field shifts them relative to each other and would pair a
            # close with a different day's volume.
            overhead_supply = None
            close_vol = df[["Close", "Volume"]].dropna().tail(OVERHEAD_LOOKBACK)
            if len(close_vol) >= 2:
                vols = close_vol["Volume"].to_numpy()
                total_vol_window = vols.sum()
                # A window of entirely zero volume yields None, not 0 -- "no
                # data" and "no overhead" must stay distinguishable, same rule
                # as breakout_window's blue-sky sentinel.
                if total_vol_window > 0:
                    is_above = close_vol["Close"].to_numpy() > last_close
                    overhead_supply = round(float(vols[is_above].sum() / total_vol_window * 100), 1)

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
            # Each session's volume, signed by that SAME session's close change.
            # This used to take tail(11) of daily_close and tail(10) of
            # daily_volume -- two independently dropna'd series -- and pair them
            # by position, so one bar missing a Volume shifted every volume onto
            # a neighbouring day's move (the hazard overhead_supply above already
            # guards against). Aligned by date instead: the previous close comes
            # from the close series itself, so a bar with no volume just drops
            # out of the window without disturbing its neighbours.
            _nv = pd.DataFrame({
                "close": df["Close"],
                "prev": daily_close.shift(1),
                "vol": df["Volume"],
            }).dropna().tail(10)
            if len(_nv) >= 10:
                net_vol = 0
                total_vol = 0
                for close_i, prev_i, vol in _nv[["close", "prev", "vol"]].itertuples(index=False):
                    total_vol += vol
                    if close_i > prev_i:
                        net_vol += vol
                    elif close_i < prev_i:
                        net_vol -= vol
                net_volume_10d_dir = "Positive" if net_vol > 0 else "Negative"
                if total_vol > 0:
                    net_volume_10d_ratio = round((net_vol / total_vol) * 100, 1)

            # 52-week high/low: intraday extremes over the trailing ~252 trading days
            window_252 = df.tail(252)
            week52_high = round(float(window_252["High"].max()), 1) if window_252["High"].notna().any() else None
            week52_low = round(float(window_252["Low"].min()), 1) if window_252["Low"].notna().any() else None

            # Trading days since the 52-week high was SET. Distinguishes a name
            # breaking out right now (age 0-10) from one retesting a high set
            # months ago -- both otherwise look identical on 52WH Distance.
            #
            # Derived from the same window_252 frame and the same "High" column
            # week52_high comes from, so the two can never disagree about which
            # bar is the high. Positional argmax rather than idxmax: idxmax
            # returns a timestamp that would need a second lookup to turn into
            # an age, and is ambiguous if the index ever holds duplicates.
            # np.nanargmax returns the FIRST occurrence, so a high matched several
            # times reports the oldest -- stable run to run rather than
            # flip-flopping between equal bars. NaN-aware on purpose: .max()
            # above skips NaN, but plain np.argmax returns the first NaN's
            # position, so a row with a missing High (normal when one download
            # unions two exchanges' calendars) pointed the age at that row
            # instead of the high -- [10, 50, NaN, 20, 30] gave 2, not 3.
            week52_high_age = None
            highs_252 = window_252["High"]
            if highs_252.notna().any():
                week52_high_age = int(len(highs_252) - 1 - int(np.nanargmax(highs_252.to_numpy())))

            # Assigned conditionally below, unlike week52_high/low which always
            # get a value -- without these defaults a ticker missing the inputs
            # would NameError at the results.append rather than reporting blanks.
            week26_distance = week52_distance = breakout_window = None

            # 26-week high (~126 trading days), same intraday-High basis as the 52w figures.
            window_126 = df.tail(126)
            week26_high = round(float(window_126["High"].max()), 1) if window_126["High"].notna().any() else None

            # Distance BELOW the trailing high as a POSITIVE percent: 0 = at the
            # high, 12.5 = 12.5% below. Deliberately the opposite sign to the
            # "% Off 52W High" custom column (which is negative below the high),
            # so breakout rules read literally as "52WH Distance < 10". That
            # custom column is intentionally left alone.
            #
            # Clamped at 0: week26_high/week52_high are rounded to 1dp BEFORE
            # this subtraction, so a close sitting marginally above the rounded
            # high produces a tiny negative that rounds to -0.0 and renders as
            # "-0.0%" -- a minus sign on a metric defined as always positive
            # (an India ticker hit exactly this). A close cannot genuinely exceed its
            # own trailing intraday high, so 0 is the correct floor.
            if week26_high:
                week26_distance = round(max(0.0, (week26_high - last_close) / week26_high * 100), 1)
            if week52_high:
                week52_distance = round(max(0.0, (week52_high - last_close) / week52_high * 100), 1)

            # 5-year high and the distance below it, same intraday-High basis
            # and same clamped-positive convention as the 26w/52w pair above.
            # The whole frame IS the 5-year window -- fetch_snapshot requests
            # period="5y" -- so this is a plain max, not a tail() slice.
            #
            # Pairs with week52_high to separate "at a 1-year high" from "at a
            # 5-year high": a name whose 5Y high sits well above its 52W high
            # still has a multi-year ceiling overhead, which is the setup the
            # Breakout Horizon scan screens for.
            high_5y = high_5y_distance = None
            if df["High"].notna().any():
                high_5y = round(float(df["High"].max()), 1)
                if high_5y:
                    high_5y_distance = round(max(0.0, (high_5y - last_close) / high_5y * 100), 1)

            # Age of the overhead resistance: trading days back to the level
            # price is now testing. A reading of 250 means price is back at a
            # level it last saw ~250 days ago, which is the breakout SETUP
            # these scans look for.
            #
            # A bar counts as overhead only if BOTH hold:
            #
            #   (a) it is a confirmed swing high -- its close is the highest
            #       over the +/-BREAKOUT_PIVOT_WIDTH bars around it, and
            #   (b) its close exceeds BREAKOUT_TOLERANCE (105% of today).
            #
            # (b) means a minor overshoot part-way through a base doesn't reset
            # the window: the walk-back runs past every close within the band
            # to the last real breach. One India ticker read 1 under the old rule
            # despite three years in a tight range; it reads 769 here. A close
            # BELOW today was never overhead, so it never stops the walk and is
            # already inside the window -- which is why the band's 97% floor
            # needs no code.
            #
            # (a) is what stops the metric referencing a bar that price merely
            # transited on the way DOWN. In a recovery through an old decline
            # every price on the way up was touched on the way down, so without
            # the pivot test the window drifts upward day after day and the
            # scans fire on a retracement with no base behind it. Measured over
            # the watchlist, 13 of the 18 scan-relevant windows landed on such
            # a bar, and the pivot test cuts day-to-day churn by about 65%
            # (2420 -> 838 jumps per 120 sessions) and re-firings with it.
            #
            # A corollary of (a): a brand-new high is not resistance until
            # BREAKOUT_PIVOT_WIDTH sessions confirm it. That is deliberate --
            # one India ticker peaked 7% above today just two sessions ago, which
            # the old rule treated as a wall (window 2) rather than as an
            # unfinished move; it now reads 293, its real base.
            #
            # Three things here are load-bearing:
            #
            #  1. The blue-sky test runs BEFORE any of this, so 0 keeps meaning
            #     "no prior close was ever this high" and nothing else. Testing
            #     the band first hands 0 to any name whose entire overhead sits
            #     within 5%, silently dropping it from every scan (one
            #     went 397 -> 0, plus 10 others).
            #  2. max() is the fallback when no bar satisfies (a) AND (b) --
            #     keep the strict answer rather than reporting 0. This is what
            #     holds those at 397 and 35. Consequence:
            #     the metric is NOT monotonic in the tolerance -- widening it
            #     past a ticker's highest overhead drops the value back to
            #     strict (one ticker: 16 at 3%, 1 at 5%).
            #  3. The lookback slice is applied AFTER dropping today's bar, so
            #     it covers BREAKOUT_LOOKBACK *prior* sessions.
            #
            # Positional numpy search rather than index.get_loc -- get_loc
            # returns a slice/array if the date index ever holds duplicates,
            # which would silently yield a wrong count.
            closes = daily_close.to_numpy()
            if len(closes) >= 2:
                today_close = closes[-1]
                prior_closes = closes[:-1][-BREAKOUT_LOOKBACK:]
                n_prior = len(prior_closes)
                at_or_above = np.nonzero(prior_closes >= today_close)[0]
                if not len(at_or_above):
                    breakout_window = 0
                else:
                    strict = n_prior - at_or_above[-1]

                    # Confirmed swing highs. The first and last PIVOT_WIDTH
                    # bars can't be evaluated (no full window either side), so
                    # they're excluded -- which is exactly the confirmation lag
                    # described above.
                    span = 2 * BREAKOUT_PIVOT_WIDTH + 1
                    tolerant = 0
                    if n_prior >= span:
                        windows = np.lib.stride_tricks.sliding_window_view(prior_closes, span)
                        inner = prior_closes[BREAKOUT_PIVOT_WIDTH:n_prior - BREAKOUT_PIVOT_WIDTH]
                        is_pivot = inner == windows.max(axis=1)
                        resistance = np.nonzero(
                            is_pivot & (inner > today_close * BREAKOUT_TOLERANCE)
                        )[0]
                        if len(resistance):
                            tolerant = n_prior - (resistance[-1] + BREAKOUT_PIVOT_WIDTH)

                    breakout_window = int(max(strict, tolerant))

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

            # Weeks since the last 10w/30w WEEKLY golden cross (EMA10 crossing
            # UP through EMA30). None while EMA10 is not currently above EMA30
            # (i.e. no active golden-cross state), no crossover found yet, or
            # not enough weekly history. EMA30 is purpose-built here -- it is
            # NOT one of the configured weekly EMA periods (10/20/40).
            gc_weeks_10_30 = None
            if len(weekly) >= 31:
                ema10_w = weekly["Close"].ewm(span=10, adjust=False).mean()
                ema30_w = weekly["Close"].ewm(span=30, adjust=False).mean()
                crossed_up = (ema10_w > ema30_w) & (ema10_w.shift(1) <= ema30_w.shift(1))
                if bool(ema10_w.iloc[-1] > ema30_w.iloc[-1]) and crossed_up.any():
                    last_cross_pos = int(np.flatnonzero(crossed_up.to_numpy())[-1])
                    gc_weeks_10_30 = int(len(weekly) - 1 - last_cross_pos)

            # Same relative-return idea at 1 month and 6 months, for the Turbo
            # Surge and Alpha Leaders scans. Uses the SAME trading-day offsets
            # as _perf() above (22 / 126) so "Perf 1M %" and this metric always
            # span the same window and can be reasoned about together.
            #
            # Both legs are measured over an INNER-JOINED calendar (the same
            # technique as mansfield_rs), not by slicing each series
            # positionally. The two series are dropna()'d independently and a
            # benchmark can be missing sessions the stock traded -- ^CRSLDX
            # runs 10 bars short of one India ticker over 5 years -- so counting 126
            # bars back on each lands on DIFFERENT dates (2026-02-05 vs
            # 2026-02-02 there) and silently compares mismatched windows. The
            # drift grows with the lookback. The 1-week leg below uses the same
            # aligned calendar.
            aligned_rel = pd.concat([daily_close, bench_daily], axis=1, join="inner").dropna()

            def _rel_ret(n):
                if len(aligned_rel) <= n:
                    return None
                stock_s = aligned_rel.iloc[:, 0]
                bench_s = aligned_rel.iloc[:, 1]
                try:
                    stock_r = float(stock_s.iloc[-1]) / float(stock_s.iloc[-n - 1]) - 1
                    bench_r = float(bench_s.iloc[-1]) / float(bench_s.iloc[-n - 1]) - 1
                except (ZeroDivisionError, IndexError):
                    return None
                return round((stock_r - bench_r) * 100, 1)

            # Relative 1-week return vs this ticker's OWN index benchmark (the
            # benchmark of its fetch group -- ^CRSLDX for Indian listings, SPY for
            # US): stock return minus benchmark return over the last 5 sessions.
            # Was named rel_ret_1w_n50 / labelled "vs Nifty 500" (see
            # filters.METRIC_RENAMES). Now on the same date-aligned calendar as
            # the 1M/6M legs; it used to slice each series positionally
            # (iloc[-6] on independently dropna'd closes), which lands on
            # different dates whenever the stock and benchmark have different
            # sessions in the last week.
            rel_ret_1w_index = _rel_ret(5)
            rel_ret_1m_index = _rel_ret(22)
            rel_ret_6m_index = _rel_ret(126)

            results.append({
                "ticker": t,
                "company_name": company_name,
                "last_close": round(last_close, 1),
                "pct_change_1d": pct_change_1d,
                "perf_1m": perf_1m,
                "perf_3m": perf_3m,
                "perf_6m": perf_6m,
                "perf_1y": perf_1y,
                "perf_3y": perf_3y,
                "qtr_profit_growth": qtr_profit_growth,
                "qtr_eps_growth": qtr_eps_growth,
                "qtr_revenue_growth": qtr_revenue_growth,
                "ttm_profit_growth": ttm_profit_growth,
                "ttm_revenue_growth": ttm_revenue_growth,
                "trailing_pe": trailing_pe,
                "forward_pe": forward_pe,
                "pb_ratio": pb_ratio,
                "ev_ebitda": ev_ebitda,
                "p_cashflow": p_cashflow,
                "roe": roe,
                "cfo_op_5yr": cfo_op_5yr,
                "roce": roce,
                "reported_qtr": reported_qtr,
                "last_price": last_close,
                "data_start": data_start,
                "data_end": data_end,
                "data_end_age_days": data_end_age_days,
                "avg_volume_10d": avg_volume_10d,
                "avg_volume_20d": avg_volume_20d,
                "avg_volume_100d": avg_volume_100d,
                "volume_trend": volume_trend,
                "net_volume_10d_dir": net_volume_10d_dir,
                "net_volume_10d_ratio": net_volume_10d_ratio,
                "week52_high": week52_high,
                "week52_low": week52_low,
                "week52_high_age": week52_high_age,
                "overhead_supply": overhead_supply,
                "week26_distance": week26_distance,
                "week52_distance": week52_distance,
                "high_5y": high_5y,
                "high_5y_distance": high_5y_distance,
                "breakout_window": breakout_window,
                "trend": trend,
                "trend_rank": trend_rank,
                "trend_detail": trend_detail,
                "tech_uptrend": tech_uptrend,
                "ta_rules": ta_rules,
                "ta_rules_detail": ta_rules_detail,
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
                "rsi12_monthly": rsi12_monthly,
                "adx_weekly_14": adx_weekly_14,
                "adx_monthly_12": adx_monthly_12,
                "rs_daily": rs_daily,
                "rs_weekly": rs_weekly,
                "rs_monthly": rs_monthly,
                "vstop_weekly": vstop_weekly,
                "vstop_weekly_14": vstop_weekly_14,
                "vstop_weekly_direction": vstop_weekly_direction,
                "vstop_weekly_last_change": vstop_weekly_last_change,
                "vstop_weekly_weeks_since_change": vstop_weekly_weeks_since_change,
                "vstop_weekly_flipped": vstop_weekly_flipped,
                "gc_weeks_10_30": gc_weeks_10_30,
                "rel_ret_1w_index": rel_ret_1w_index,
                "rel_ret_1m_index": rel_ret_1m_index,
                "rel_ret_6m_index": rel_ret_6m_index,
            })
        except Exception as e:
            print(f"  {t}: ERROR {e}")

        # Small pause between tickers' .info calls (the main source of
        # rate-limit trips, see _fetch_info_with_retry) to avoid tripping
        # Yahoo's limiter in the first place, not just retrying after the fact.
        time.sleep(0.5)

    as_of = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
    return results, as_of


def fetch_all_markets(watchlists=None, period="5y", settings=None, completed_sessions_only=False,
                      short_history=None, skipped_groups=None):
    """Fetches every registered watchlist and returns a combined
    (results, as_of, per_market_results) tuple. per_market_results is
    {market_key: [...], ...}.

    Benchmark grouping is by TICKER, not by market: each ticker carries its
    own permanent index assignment (ticker_index.json, see
    assign_ticker_index_if_missing) detected once via yfinance the first time
    it's ever seen, independent of which watchlist(s) it's filed under. A
    watchlist's registered benchmark (markets.json) is only the fallback for
    a ticker detect_ticker_index couldn't classify. fetch_snapshot itself
    cannot mix benchmarks within one call -- it downloads tickers+benchmark
    in a single batched request and derives ONE bench_daily/weekly/monthly
    series before its per-ticker loop starts -- so tickers are grouped by
    their resolved benchmark and fetch_snapshot is called once per unique
    benchmark, not once per market. This also means fewer total yfinance
    calls in steady state than the old one-call-per-market approach, since
    the ~handful of distinct benchmarks in use is normally smaller than the
    number of watchlists."""
    if watchlists is None:
        watchlists = load_watchlists()
    settings = settings or load_settings()
    benchmarks = get_benchmarks(settings)

    all_tickers_ordered = []
    seen_tickers = set()
    for tickers in watchlists.values():
        for t in tickers:
            if t not in seen_tickers:
                seen_tickers.add(t)
                all_tickers_ordered.append(t)
    backfill_ticker_indices(all_tickers_ordered)
    ticker_index = load_ticker_index()

    # Group every ticker (across ALL markets) by its resolved benchmark.
    tickers_by_benchmark = {}
    for market, tickers in watchlists.items():
        fallback_bench = benchmarks.get(market, "SPY")
        for t in tickers:
            assignment = ticker_index.get(t)
            bench = assignment["benchmark"] if assignment else fallback_bench
            tickers_by_benchmark.setdefault(bench, [])
            if t not in tickers_by_benchmark[bench]:
                tickers_by_benchmark[bench].append(t)

    results_by_ticker = {}
    as_of = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
    for bench, tickers in tickers_by_benchmark.items():
        try:
            rows, as_of = fetch_snapshot(tickers, benchmark=bench, period=period, settings=settings,
                                         completed_sessions_only=completed_sessions_only,
                                         short_history=short_history)
        except Exception as e:
            # Recorded, not just printed: a caller that JUDGES this data (alerts,
            # the weekly digest) has to know it is looking at a partial universe
            # rather than a quiet market. refresh_data.py survives a skip via
            # fill_snapshot_gaps; the headless judges have no such backstop, so
            # they fail the run and let the slot gate retry.
            print(f"  [fetch_all_markets] skipping benchmark group {bench} ({len(tickers)} tickers): {e}")
            if skipped_groups is not None:
                skipped_groups[bench] = list(tickers)
            continue
        for r in rows:
            results_by_ticker[r["ticker"]] = r

    # Reassemble per-market results in each watchlist's original ticker
    # order. Each market gets its OWN COPY of a shared ticker's row dict --
    # a ticker present in two watchlists must not have one market's
    # "market"/"index_name" tag leak into the other's.
    per_market = {}
    for market, tickers in watchlists.items():
        rows = []
        for t in tickers:
            base = results_by_ticker.get(t)
            if base is None:
                continue
            r = dict(base)
            r["market"] = market
            assignment = ticker_index.get(t)
            r["index_name"] = assignment["index"] if assignment else None
            rows.append(r)
        per_market[market] = rows

    # Custom columns, notes/flags and the AI/interested fields are attached
    # HERE -- not in app.py -- so every consumer of fetch_all_markets gets them:
    # the Streamlit app, but also the headless alert_check.py / refresh_data.py
    # runs that never touch app.py. An alert on a custom column, a flag or
    # Sentiment could otherwise never fire headless.
    combined = [r for market in per_market for r in per_market[market]]
    enrich_rows(combined, settings)

    return combined, as_of, per_market


def enrich_rows(rows, settings=None):
    """Attach every non-price field to `rows` in place, from the files as they
    are NOW: custom columns, notes/flags (with the auto-flag vote), and
    interested / sentiment / expert_take / expert_news_backed.

    One entry point because two callers need it and one of them forgot: the
    judging jobs fetch (enriched) rows, then let reject_stale_rows /
    fill_snapshot_gaps substitute the STORED snapshot row wherever Yahoo's copy
    is older -- and the stored row carries these fields as they were when that
    snapshot was built. In the evening that is most of the universe (95 of 147
    rows on 2026-09-16), so a rule on Flag or Interested was judging a flag from
    the last hourly refresh for one half of the table and a live one for the
    other. The app's own load path re-applies these on every run; the jobs now
    do the same after every substitution."""
    settings = settings or load_settings()
    from custom_columns import apply_custom_columns_to_rows
    from ticker_notes import apply_notes_to_rows
    apply_custom_columns_to_rows(rows)
    apply_notes_to_rows(rows, min_vstop_weeks=settings.get("tech_uptrend_min_vstop_weeks", 3))
    apply_view_fields_to_rows(rows)
    return rows


def refresh_data_end_age(rows, today=None):
    """Recompute each row's `data_end_age_days` from its `data_end`, in place.

    fetch_snapshot stores the age at FETCH time and nothing recomputed it, so
    the dashboard's "N tickers haven't updated in 3+ days" caption -- a
    pipeline-stall detector -- reported the age the rows had when the snapshot
    was built, i.e. it stalled with the pipeline.

    A row whose data_end is missing or unparseable is left exactly as it was:
    there is nothing to recompute from, and the field must stay an int. The
    caption tests `row.get("data_end_age_days", 0) >= 3`, and .get's default
    does NOT cover a key that is present and None -- writing None here raised
    TypeError out of the render path, taking every tab down at once."""
    today = today or datetime.now(ZoneInfo("America/New_York")).date()
    for row in rows:
        end = _parse_data_end(row.get("data_end"))
        if end:
            row["data_end_age_days"] = (today - end).days
    return rows


def apply_view_fields_to_rows(rows, fundamentals=None, expert_views=None, interested=None):
    """Attach `interested`, `sentiment`, `expert_take` and `expert_news_backed`
    to every row, in place -- the fields that come from interested.json and the
    two AI result files rather than from prices.

    ONE implementation for every consumer. These used to be attached only in
    app.py's enrichment loop, so the dashboard's rows had them and
    fetch_all_markets' rows -- what alert_check.py and weekly_wrapup_check.py
    evaluate -- did not. passes_filter fails a condition on a missing metric,
    so an alert on Sentiment, Interested or Expert News? matched in the app's
    preview and could never fire in Discord. (expert_take limped along on a
    per-row fallback in filters._get_metric_val that re-read expert_views.json
    for every row and skipped the pending/staleness guards below.)

    The loaded dicts are optional so a caller that already has them (the app,
    once per run) doesn't re-parse the files per call.

    Semantics, unchanged from the app loop they came from:
      - sentiment is the GUARDED value (_validate_sentiment), not the raw one;
      - expert_take checks is_pending_view first -- a failed-generation
        placeholder stores verdict "HOLD", and keying off the verdict alone
        made a broken analysis filterable as a genuine Hold -- then the guarded
        validate_verdict, which demotes an unsupported ACCUMULATE and ages out a
        stale view;
      - expert_news_backed is "Yes"/"No" from expert_view_has_news."""
    from expert_views import load_expert_views, is_pending_view, validate_verdict, expert_view_has_news
    from fundamentals_eval import load_fundamentals, _validate_sentiment
    fundamentals = load_fundamentals() if fundamentals is None else fundamentals
    expert_views = load_expert_views() if expert_views is None else expert_views
    interested = load_interested() if interested is None else interested
    for row in rows:
        ticker = row.get("ticker")
        row["interested"] = ticker in interested
        row["sentiment"] = _validate_sentiment(fundamentals.get(ticker, {}))[0]
        view = expert_views.get(ticker, {})
        if is_pending_view(view):
            row["expert_take"] = "Pending"
        else:
            verdict, _flag = validate_verdict(view, row)
            row["expert_take"] = verdict.title() if verdict in ("ACCUMULATE", "HOLD", "CAUTION") else "Pending"
        row["expert_news_backed"] = "Yes" if expert_view_has_news(view) else "No"
    return rows


def _json_default(o):
    """json.dump default= hook for numpy scalar types (np.bool_, np.int64,
    np.float64, etc.) that sneak into result rows from pandas/numpy calcs --
    the stdlib json module doesn't know how to serialize these even though
    they look/behave like native bool/int/float."""
    if isinstance(o, np.generic):
        return o.item()
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


def _code_fingerprint():
    """Hash of this module's own source, used to detect when a snapshot was
    computed by a since-replaced version of the calc code (e.g. an EMA/SMA
    switch or a vstop parameter change) even if settings.json didn't change.
    Deliberately avoids the `git` CLI/`.git` dir -- not reliably available
    in every deploy sandbox (e.g. Streamlit Cloud), which silently disabled
    an earlier version of this guard."""
    try:
        with open(__file__, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return "unknown"


def save_data_snapshot(as_of, per_market, settings=None, merge=False, short_history=None,
                       provenance=None):
    """Persists a fetch_all_markets() result to disk so the Streamlit app
    can load it directly instead of hitting yfinance live on every session
    -- meant to be called by the scheduled data-refresh workflow (see
    refresh_data.py, hourly cron) rather than on every app load. Stores the settings used
    to compute it too, so the app can detect a settings change (SMA
    lengths, thresholds, etc.) since the snapshot ran and fall back to a
    live fetch instead of showing data computed with stale parameters.

    `merge=True` overlays only the markets in `per_market` onto whatever is
    already on disk, leaving the rest untouched. A SCOPED refresh must use
    it: this file holds every market in one blob, so writing just the
    markets you fetched would delete the others. That is not hypothetical
    -- refresh_market_breadth.py wrote its file the same unconditional way
    and one failing leg silently destroyed 1055 days of US breadth.

    `short_history` is what THIS caller's fetch learned about tickers too new
    to compute (None if it fetched nothing); it is merged with what the file
    already records -- see merge_short_history.

    `provenance` ({"settings", "code_version"}) overrides the stamps written.
    A caller that wrote back rows it did NOT recompute passes the stamps those
    rows were actually computed under -- see the watchlist save in app.py.

    Returns the `generated_at` stamp it wrote."""
    from datetime import timezone
    existing = load_data_snapshot() or {}
    if merge:
        base = dict(existing.get("per_market") or {})
        # A scoped refresh recomputes only its own markets; every other market
        # keeps rows computed under the EXISTING snapshot's stamps. If those
        # don't match the current code/settings, stamping current over the
        # merge hid the "out of date" warning for rows nobody recomputed -- the
        # same failure as the watchlist save (N4). Keep the old stamps then; the
        # next full refresh re-stamps honestly.
        untouched = set(base) - set(per_market)
        if (provenance is None and untouched and base
                and not snapshot_calc_matches(existing, settings)):
            provenance = {"settings": existing.get("settings") or {},
                          "code_version": existing.get("code_version")}
        base.update(per_market)
        per_market = base
    short = merge_short_history(existing.get("short_history"), short_history, per_market, load_watchlists())
    generated_at = datetime.now(timezone.utc).isoformat()
    atomic_write_json(DATA_SNAPSHOT_FILE, {
        "as_of": as_of,
        "generated_at": generated_at,
        "code_version": (provenance or {}).get("code_version", _code_fingerprint()),
        "per_market": per_market,
        "settings": (provenance or {}).get("settings", settings or {}),
        "short_history": short,
    }, default=_json_default)
    # Returned so a caller holding the result in memory (app.py's
    # _persist_and_serve) can tell when the file has since moved past it.
    return generated_at


def load_data_snapshot():
    if not os.path.exists(DATA_SNAPSHOT_FILE):
        return None
    try:
        with open(DATA_SNAPSHOT_FILE) as f:
            snapshot = json.load(f)
    except Exception:
        return None
    # A snapshot written before a metric rename still carries the old keys
    # until the next refresh rewrites it; serve them under the current names so
    # the columns and rules don't go blank in between.
    from filters import METRIC_RENAMES, VALUE_RENAMES
    for rows in ((snapshot or {}).get("per_market") or {}).values():
        for row in rows:
            for old, new in METRIC_RENAMES.items():
                if old in row and new not in row:
                    row[new] = row.pop(old)
            for field, renames in VALUE_RENAMES.items():
                if row.get(field) in renames:
                    row[field] = renames[row[field]]
    return snapshot


def missing_row_tickers(per_market, watchlists=None, short_history=None):
    """{market: [tickers with no row]}, ignoring tickers that legitimately
    cannot have one yet (fewer than MIN_DAILY_BARS bars -- see
    merge_short_history). Same exemption snapshot_is_usable applies, pulled out
    so the headless jobs can ask "is this the whole universe?" before they judge
    it."""
    watchlists = load_watchlists() if watchlists is None else watchlists
    too_new = set(short_history or {})
    out = {}
    for market, tickers in watchlists.items():
        have = {r.get("ticker") for r in per_market.get(market) or []}
        gone = [t for t in tickers if t not in have and t not in too_new]
        if gone:
            out[market] = gone
    return out


def fill_snapshot_gaps(fresh_per_market, previous_per_market, watchlists):
    """Backfills tickers a fetch failed to return from the previous snapshot,
    returning (filled_per_market, {market: [recovered tickers]}).

    fetch_all_markets drops whatever Yahoo would not hand over: a benchmark
    group whose fetch_snapshot raised is skipped wholesale, and inside a group
    a ticker that came back empty simply produces no row. Persisting that
    result verbatim does not record "we could not reach Yahoo", it records
    "this watchlist has four stocks in it" -- which is what happened on
    2026-08-29, when a Refresh Data click during a throttling episode replaced
    30 India rows with 4 and pushed that to GitHub. Every US market was
    untouched, because only the ^CRSLDX group collapsed.

    A missing row means the fetch failed, never that the ticker is gone -- a
    ticker actually removed from a watchlist is absent from `watchlists` and
    is dropped here as usual. So the honest degradation is last-known data for
    the tickers we could not refresh, which the UI already surfaces per row via
    the "Data Thru" column and its stale-ticker warning.

    Callers should tell the user what was recovered; silently serving stale
    rows would trade one invisible failure for another."""
    prev_by_market = {
        m: {r.get("ticker"): r for r in rows}
        for m, rows in (previous_per_market or {}).items()
    }

    filled, recovered = {}, {}
    for market, rows in (fresh_per_market or {}).items():
        fresh_by_ticker = {r.get("ticker"): r for r in rows}
        # Watchlist order is authoritative; fall back to whatever the fetch
        # returned for a market with no watchlist entry.
        wanted = list((watchlists or {}).get(market) or fresh_by_ticker.keys())
        out, from_previous = [], []
        for t in wanted:
            if t in fresh_by_ticker:
                out.append(fresh_by_ticker[t])
                continue
            old = prev_by_market.get(market, {}).get(t)
            if old is not None:
                out.append(old)
                from_previous.append(t)
        filled[market] = out
        if from_previous:
            recovered[market] = from_previous
    return filled, recovered


def _parse_data_end(value):
    """"YYYY-MM-DD" -> date, or None if absent/unparseable."""
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except Exception:
        return None


def reject_stale_rows(fresh_per_market, previous_per_market, max_hold_days=5, today=None,
                      completed_sessions_only=False, now=None):
    """Keep the previous row when a freshly fetched one is OLDER than it.
    Returns (per_market, {market: [tickers]}), the same shape as
    fill_snapshot_gaps.

    fill_snapshot_gaps covers a fetch that returned NOTHING. This covers the
    other half: a fetch that returned a complete, internally consistent row of
    YESTERDAY. Nothing about such a row looks wrong -- it has prices, EMAs,
    RSI, VStop, volumes, a data_end -- so every guard we had waved it through,
    and save_data_snapshot then stamped it with the current as_of and
    overwrote good data with it.

    That is not hypothetical. On 2026-09-08 and again on 2026-09-09, the
    ~21:00 ET run came back with the previous session for most US tickers: in
    the 09-09 21:00 snapshot, 46 of 50 US rows had last_close byte-identical
    to that morning's PRE-MARKET snapshot and data_end had moved backwards
    from 09-09 to 09-08, while the 18:28 run three hours earlier had the right
    data. We pass no start/end to yfinance (period="5y"), so the series
    boundary is entirely Yahoo's -- three tickers did get 09-09, which looks
    like cache variance on their side rather than a clean cutoff. The file then
    claimed a freshness it did not have, which is the actual harm: the
    dashboard showed yesterday's Last and % Chg under tonight's timestamp, and
    "Data Thru" only reddens at 3+ days so a one-session regression was
    invisible.

    max_hold_days is the safety valve, and it is the part that matters. A
    ticker's history can legitimately shrink -- delisting, a symbol change,
    Yahoo dropping coverage -- and a guard without a valve would then serve
    that ticker's last good row forever, trading a visible bug for an
    invisible one. So the previous row is only held while it is itself recent;
    past that, the fresh row wins even though it is older, and the row's own
    data_end/"Data Thru" column carries the staleness to the UI.

    completed_sessions_only MUST match the flag the caller passed to
    fetch_all_markets. Without it this guard quietly undid that flag. The stored
    snapshot is written by refresh_data.py, which keeps forming bars on purpose
    (data-refresh.yml wakes hourly around the clock precisely to sample the live
    NSE session), while a job that judges CLOSES fetches with the forming bar
    dropped. Mid-session the stored India row is therefore stamped today and the
    correct fresh row yesterday -- so this function read the correct row as
    "Yahoo served a stale series" and substituted the forming-bar one. Alerts
    then fired on an intraday cross that could reverse by the close, and being
    edge-triggered, never fired again. With the flag set, a previous row whose
    data_end IS the still-forming session (forming_session_date) can never win:
    it is the row that is wrong, not the fresh one.

    A held row still takes every FIELD it lacks from the fresh row. The stored
    row may predate a code change that added a column: on 2026-09-24 Yahoo
    served five .BO tickers a session behind, so this guard kept rows computed
    before TA Rules existed and the new column read blank on them until Yahoo
    caught up. Filling only ABSENT keys keeps every price and indicator the
    stored row has (nothing moves backwards in time), and a new field carries
    the fresh fetch's value, one session older at most -- and the saved row
    now has that field, so the next refresh holds it like any other.
    """
    prev_by_market = {
        m: {r.get("ticker"): r for r in rows}
        for m, rows in (previous_per_market or {}).items()
    }
    today = today or datetime.now(ZoneInfo("America/New_York")).date()

    out, rejected = {}, {}
    forming_overrides = []
    for market, rows in (fresh_per_market or {}).items():
        prev = prev_by_market.get(market, {})
        kept, stale = [], []
        for row in rows:
            old = prev.get(row.get("ticker"))
            new_end = _parse_data_end(row.get("data_end"))
            old_end = _parse_data_end((old or {}).get("data_end"))
            # Never block on a comparison we cannot make: an unknown date on
            # either side, or a ticker with no history here, takes the fresh row.
            if old is None or new_end is None or old_end is None or new_end >= old_end:
                kept.append(row)
                continue
            if completed_sessions_only and old_end == forming_session_date(row.get("ticker", ""), now):
                # The stored row holds a forming bar -- see the docstring. Taking
                # the fresh row is right even when it is genuinely old: an honest
                # older CLOSE is something a rule can be judged on and "Data Thru"
                # can show, while an incomplete bar is neither. Counted rather
                # than waved through, so a ticker Yahoo is actually serving stale
                # doesn't hide inside this branch.
                forming_overrides.append(row.get("ticker"))
                kept.append(row)
                continue
            if (today - old_end).days > max_hold_days:
                kept.append(row)          # the valve -- see the docstring
                continue
            missing = {k: v for k, v in row.items() if k not in old}
            kept.append({**old, **missing} if missing else old)
            stale.append(row.get("ticker"))
        out[market] = kept
        if stale:
            rejected[market] = stale
    if forming_overrides:
        print(f"  [reject_stale_rows] kept the freshly fetched (completed-session) row for "
              f"{len(forming_overrides)} ticker(s) whose stored row is a still-forming bar: "
              + ", ".join(forming_overrides[:10])
              + (" ..." if len(forming_overrides) > 10 else ""))
    return out, rejected


def rebuild_snapshot_for_market(snap_per_market, market, tickers, fetch_new):
    """Rebuilds ONE market's rows inside a snapshot, fetching only what is
    genuinely missing, and returns (merged_per_market, fetched_tickers).

    `fetch_new(tickers)` is called at most once, with only the tickers that
    have no row anywhere in the snapshot, and must return a list of row dicts.
    It is not called at all when nothing is missing -- which is the point:
    saving a watchlist after deleting a ticker, or with no change, should cost
    no Yahoo traffic whatsoever.

    Reusing a row that was fetched under a DIFFERENT watchlist is deliberate
    and safe. Rows are per-ticker, not per-market: fetch_all_markets groups by
    each ticker's permanent ticker_index.json assignment, "independent of which
    watchlist(s) it's filed under" (see its docstring), so one ticker yields
    the same row whichever list it is filed under -- with exactly one
    exception, the "market" tag stamped on at fetch time. That one IS
    per-market and load-bearing (alerts.py scopes a rule's tickers by it), so
    a reused row is copied and re-tagged rather than filed under the wrong
    watchlist. The copy matters too: mutating the row in place would retag the
    source market's own row as a side effect.

    Markets other than `market` are passed through untouched -- this file holds
    every market in one blob, so rebuilding it from only the market in hand
    would silently delete the others."""
    rows_by_ticker = {}
    for rows in (snap_per_market or {}).values():
        for r in rows:
            rows_by_ticker.setdefault(r.get("ticker"), r)

    to_fetch = [t for t in tickers if t not in rows_by_ticker]
    if to_fetch:
        for r in fetch_new(to_fetch) or []:
            rows_by_ticker[r.get("ticker")] = r

    merged = {m: list(rows) for m, rows in (snap_per_market or {}).items()}
    # Built from the saved ticker order, so removed tickers simply fall out.
    rebuilt = []
    for t in tickers:
        row = rows_by_ticker.get(t)
        if row is None:
            continue
        if row.get("market") != market:
            row = dict(row)
            row["market"] = market
        rebuilt.append(row)
    merged[market] = rebuilt
    return merged, to_fetch


# Settings that change nothing about how a snapshot row is computed. Prefixes
# cover the AI pipeline and note-dropdown settings; the exact keys are a
# display toggle and two legacy benchmark settings that markets.json has
# replaced (they are read only once, by the markets.json migration in
# load_markets_registry). Flipping show_fundamental_columns used to invalidate
# the snapshot and force a live Yahoo fetch for a column-visibility change.
NON_CALC_SETTING_PREFIXES = ("news_", "expert_", "note_", "sentiment_")
NON_CALC_SETTING_KEYS = {"show_fundamental_columns", "benchmark_us", "benchmark_india"}


def calc_settings(settings):
    """The subset of `settings` that affects computed row values."""
    return {k: v for k, v in (settings or {}).items()
            if not k.startswith(NON_CALC_SETTING_PREFIXES) and k not in NON_CALC_SETTING_KEYS}


def calc_settings_diff(snapshot_settings, settings):
    """{key: (snapshot value, current value)} for every calc setting that
    differs -- what the out-of-date warning names."""
    a, b = calc_settings(snapshot_settings), calc_settings(settings)
    return {k: (a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}


def snapshot_calc_matches(snapshot, settings):
    """True if the snapshot's rows were computed by this code with these
    calculation settings -- the half of snapshot_is_usable that is about HOW
    rows were computed rather than WHICH rows exist."""
    if not snapshot:
        return False
    if snapshot.get("code_version") != _code_fingerprint():
        return False
    # Only calculation settings -- see NON_CALC_SETTING_PREFIXES/KEYS.
    return calc_settings(snapshot.get("settings")) == calc_settings(settings)


def snapshot_is_usable(snapshot, watchlists, settings):
    """True if `snapshot` can be shown as-is: it has a row for every ticker
    currently in `watchlists` (for every market), AND it was computed with
    the same settings as `settings` (tickers recorded in the snapshot's
    `short_history` as too new to compute are exempt from the row check -- see
    merge_short_history). If someone added a ticker since the
    last scheduled refresh, or changed a calc parameter (SMA length, RSI
    threshold, etc.) in the Settings dialog, the snapshot no longer
    reflects reality -- the app should fall back to a live fetch rather
    than silently show stale/incomplete data until tomorrow's 7 AM run."""
    if not snapshot or not isinstance(snapshot.get("per_market"), dict):
        return False

    if not snapshot_calc_matches(snapshot, settings):
        return False

    per_market = snapshot["per_market"]
    # Tickers too new to compute (see merge_short_history) have no row by
    # design; they must not make the whole snapshot count as out of date.
    too_new = set(snapshot.get("short_history") or {})
    for market in watchlists.keys():
        snap_tickers = {r.get("ticker") for r in per_market.get(market, [])}
        wanted = set(watchlists.get(market, [])) - too_new
        if not wanted.issubset(snap_tickers):
            return False
    return True


if __name__ == "__main__":
    combined, as_of, per_market = fetch_all_markets()
    print(json.dumps({"as_of": as_of, **per_market}, indent=2))
