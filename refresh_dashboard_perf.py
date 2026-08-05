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
            data = yf.download(failed, period="5y", interval="1d", auto_adjust=True, progress=False, threads=False)
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
                rd = yf.download(t, period="5y", interval="1d", auto_adjust=True, progress=False)
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


def main():
    settings = load_settings()
    watchlists = load_watchlists()
    benchmarks = get_benchmarks(settings)

    markets_out = {}
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
        markets_out[market] = outer
        print(f"[{market}] stored {len(outer)} series")

    payload = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "period": "5y",
        "markets": markets_out,
    }
    with open(OUT_FILE, "w") as fh:
        json.dump(payload, fh)
    print(f"Saved {OUT_FILE}")


if __name__ == "__main__":
    main()