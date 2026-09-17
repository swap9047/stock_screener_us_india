"""Scheduled dashboard performance-data refresher.

Fetches 5y daily closes for every watchlist ticker (plus each market's
benchmark) and writes them to dashboard_perf.json. The Streamlit Dashboard
page reads this file instead of hitting yfinance live at render time, so the
only refresh point for this data is this script -- run by the same GitHub
Actions workflow that refreshes market breadth (market-breadth.yml).

Output schema:
    {
      "as_of": "... UTC",
      "period": "5y",
      "markets": { "<market>": { "<ticker>": {"YYYY-MM-DD": close, ...}, ... } }
    }

The benchmark ticker (US: SPY, INDIA: ^CRSLDX) is stored as a normal column
within its market, matching what the app's calculate_portfolio_returns()
expects.

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


def _download_series(tickers):
    """Bulk-download 5y daily closes for `tickers`, retrying failed ones
    individually. Returns a dict {ticker: pd.Series of close}."""
    series = {}
    failed = list(tickers)
    data = None
    try:
        buf = io.StringIO()
        with redirect_stderr(buf):
            data = yf.download(failed, period="5y", interval="1d", auto_adjust=False, progress=False, threads=False)
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
                rd = yf.download(t, period="5y", interval="1d", auto_adjust=False, progress=False)
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

    # A market that has dropped out of watchlist.json is intentionally NOT
    # carried over from `previous` -- only markets we still track are written.
    payload = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "period": "5y",
        "markets": markets_out,
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