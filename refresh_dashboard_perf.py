"""Scheduled dashboard performance-data refresher.

Fetches 6y daily closes for every watchlist ticker (plus each market's
benchmark) and writes them to dashboard_perf.json. 6 years, not 5 (2026-10-14):
the app's per-watchlist breadth view computes 200-day SMAs and 52-week highs
from these closes, and their warmup would otherwise blank the first year of
its 5-year view. The performance curves cut to the chosen horizon anyway.

The Streamlit Dashboard page reads this file instead of hitting yfinance live
at render time, so the only refresh point for this data is this script -- run
by the same GitHub Actions workflow that refreshes market breadth
(market-breadth.yml).

Output schema:
    {
      "as_of": "... UTC",
      "period": "6y",
      "markets": { "<market>": { "<ticker>": {"YYYY-MM-DD": close, ...}, ... } },
      "benchmarks": { "<ticker>": {"YYYY-MM-DD": close, ...}, ... }
    }

Each market's own benchmark ticker (from markets.json) is stored as a normal
column within its market, matching what the app's calculate_portfolio_returns()
expects. "benchmarks" holds BENCHMARK_SERIES once, for the charts that let you
pick the index -- a US watchlist against the Nasdaq 100 needs QQQ, which no
market block stores (2026-10-15).

Closes are PRICE return (auto_adjust=False -> split-adjusted, dividends not
reinvested). The India benchmark ^CRSLDX is a price index, so dividend-adjusted
watchlist closes gave India Invested a ~1%/yr head start over it. SPY is
price-only here too, so both markets compare like with like.
"""

import io
import json
import os
import time
from contextlib import redirect_stderr
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from stock_data import load_watchlists, load_settings, get_benchmarks

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(SCRIPT_DIR, "dashboard_perf.json")
PERIOD = "6y"
# The indexes the app's charts can be switched between: S&P 500, Nasdaq 100,
# Nifty 500.
BENCHMARK_SERIES = ["SPY", "QQQ", "^CRSLDX"]


def _download_series(tickers):
    """Bulk-download 6y daily closes for `tickers`, retrying failed ones
    individually. Returns a dict {ticker: pd.Series of close}."""
    series = {}
    failed = list(tickers)
    data = None
    try:
        buf = io.StringIO()
        with redirect_stderr(buf):
            data = yf.download(failed, period=PERIOD, interval="1d", auto_adjust=False, progress=False, threads=False)
    except Exception as e:
        print(f"  bulk download error: {e}")

    if data is not None and not data.empty:
        closes = data["Close"] if "Close" in data.columns else data
        if isinstance(closes, pd.Series):
            closes = closes.to_frame(name=failed[0])
        for col in closes.columns:
            s = closes[col].dropna()
            if not s.empty:
                series[col] = s
        failed = [t for t in failed if t not in series]

    # Individual retries for anything the bulk call dropped.
    for t in failed:
        try:
            buf = io.StringIO()
            with redirect_stderr(buf):
                rd = yf.download(t, period=PERIOD, interval="1d", auto_adjust=False, progress=False)
            if not rd.empty and "Close" in rd.columns:
                s = rd["Close"].dropna()
                if isinstance(s, pd.DataFrame):
                    s = s.iloc[:, 0]
                if not s.empty:
                    series[t] = s
        except Exception as e:
            print(f"  {t} retry failed: {e}")
        time.sleep(1)

    return series


# Below this share of a market's requested series actually captured, the run is
# treated as a failed fetch rather than a reading -- the same floor, and the same
# reasoning, as refresh_market_breadth.MIN_COVERAGE_FRACTION.
MIN_COVERAGE_FRACTION = 0.5


# Days a stored close may be carried over when a fresh download ends before it.
KEEP_TAIL_DAYS = 5


def keep_recent_tail(new, old, today=None):
    """`new` ({"YYYY-MM-DD": close}) plus any of `old`'s closes dated after
    new's last day and within KEEP_TAIL_DAYS of `today`.

    Yahoo blanks the latest daily row for hours at a time: at 12:30 AM IST on
    2026-10-15 every Indian ticker's Oct 14 close came back empty, though an
    earlier run had stored it. Written over wholesale, that run would have
    dropped India's latest day from the performance curves and the watchlist
    breadth view. Only recent days are carried, so a delisted ticker's old
    closes never linger.
    """
    if not new or not old:
        return new
    last = max(new)
    today = today or datetime.now(timezone.utc).date()
    floor = (pd.Timestamp(today) - pd.Timedelta(days=KEEP_TAIL_DAYS)).strftime("%Y-%m-%d")
    tail = {d: v for d, v in old.items() if d > last and d >= floor}
    return {**new, **tail} if tail else new


def load_previous():
    """The last good dashboard_perf.json, or {} -- never raises.

    Regenerable data, so a torn or missing file must not stop the run; it just
    means there is nothing to fall back on this time.
    """
    try:
        with open(OUT_FILE) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def main():
    settings = load_settings()
    watchlists = load_watchlists()
    benchmarks = get_benchmarks(settings)

    # This file used to be rebuilt from scratch and written unconditionally, so a
    # throttled leg wrote an EMPTY market and the commit step pushed it --
    # blanking that market's 5-year performance chart until the next successful
    # run (slots are 12h apart). refresh_market_breadth.py learned this on
    # 2026-08-29, when one failing leg destroyed 1055 days of US breadth, and
    # gained a coverage floor plus per-market preservation. This is the same
    # guard: it is the same kind of file, written by the same workflow, in the
    # same job.
    previous = load_previous()
    prev_markets = previous.get("markets") or {}
    markets_out, status = {}, {}
    for market, tickers in watchlists.items():
        bench = benchmarks.get(market, "SPY")
        all_tickers = [t for t in tickers if t] + [bench]
        if not all_tickers:
            continue
        print(f"[{market}] downloading {len(all_tickers)} series (bench={bench})...")
        series = _download_series(all_tickers)
        outer = {
            col: {d.strftime("%Y-%m-%d"): round(float(v), 4) for d, v in s.items()}
            for col, s in series.items()
        }
        prev_block = prev_markets.get(market) or {}
        outer = {col: keep_recent_tail(closes, prev_block.get(col)) for col, closes in outer.items()}
        if len(outer) < MIN_COVERAGE_FRACTION * len(all_tickers):
            # Not a reading. Keep the previous block if there is one; a market
            # with no previous block stays absent, which is honest -- an empty
            # one would render as "this market has no history".
            kept = prev_markets.get(market)
            status[market] = "failed"
            print(f"::warning::[{market}] only {len(outer)} of {len(all_tickers)} series captured "
                  f"(< {MIN_COVERAGE_FRACTION:.0%}) -- treating as a failed fetch, not a reading")
            if kept:
                markets_out[market] = kept
                print(f"  -> kept the previous {market} block ({len(kept)} series)")
            else:
                print(f"  -> no previous {market} block to fall back on; it stays absent")
            continue
        markets_out[market] = outer
        status[market] = "ok"
        print(f"[{market}] stored {len(outer)} series")

    # The selectable benchmarks, once. Same keep-the-previous rule as a market:
    # a failed download keeps the stored series rather than dropping them.
    prev_bench = previous.get("benchmarks") or {}
    print(f"[benchmarks] downloading {len(BENCHMARK_SERIES)} series...")
    bench_series = _download_series(BENCHMARK_SERIES)
    benchmarks_out = {}
    for t in BENCHMARK_SERIES:
        new = {d.strftime("%Y-%m-%d"): round(float(v), 4) for d, v in bench_series[t].items()} \
            if t in bench_series else None
        if new:
            benchmarks_out[t] = keep_recent_tail(new, prev_bench.get(t))
        elif prev_bench.get(t):
            benchmarks_out[t] = prev_bench[t]
            print(f"::warning::[benchmarks] {t} failed to download -- kept the stored series")
        else:
            print(f"::warning::[benchmarks] {t} failed to download and none is stored")
    # "ok" only when every series downloaded afresh: a kept one is stale, and the
    # verify step's "kept previous data" warning is how that becomes visible.
    status["benchmarks"] = "ok" if all(t in bench_series for t in BENCHMARK_SERIES) else "failed"

    # A market that has dropped out of watchlist.json is intentionally NOT
    # carried over from `previous` -- only markets we still track are written.
    payload = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "period": PERIOD,
        "markets": markets_out,
        "benchmarks": benchmarks_out,
        "status": status,
    }
    # Atomic, and 3.6 MB: the longest window in this repo for a cancelled job to
    # land mid-write, after which commit-data (if: always()) pushes the torn file.
    from json_store import atomic_write_json
    atomic_write_json(OUT_FILE, payload)
    print(f"Saved {OUT_FILE}")
    print(f"Status: {status}")


if __name__ == "__main__":
    main()