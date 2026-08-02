#!/usr/bin/env python3
"""
Scheduled data refresh. Meant to run once daily (GitHub Actions, ~7:00 AM
ET, before market open) -- fetches BOTH watchlists (prices + all computed
indicators) via yfinance and saves the result to data_snapshot.json.

This exists so the Streamlit app can load a ready-made snapshot instantly
instead of hitting yfinance live every time someone opens it (yfinance
calls are the slow part of a page load, and re-fetching identically for
every visitor/session is wasteful). The app still has a manual "Refresh
Data" button for on-demand live data -- this snapshot is just the default.

Config (same folder):
    watchlist.json      - {"US": [...], "INDIA": [...]}
    settings.json        - calc parameters (EMA lengths, thresholds, etc.)
    data_snapshot.json    - auto-managed output, read by the app on load

Run: python3 refresh_data.py
"""

from stock_data import load_watchlists, load_settings, fetch_all_markets, save_data_snapshot


def main():
    watchlists = load_watchlists()
    settings = load_settings()
    total = sum(len(v) for v in watchlists.values())
    if total == 0:
        print("Watchlist is empty. Nothing to refresh.")
        return

    breakdown = " + ".join(f"{len(tks)} {mkt}" for mkt, tks in watchlists.items())
    print(f"Fetching {total} tickers ({breakdown})...")
    combined, as_of, per_market = fetch_all_markets(watchlists, settings=settings)
    save_data_snapshot(as_of, per_market, settings=settings)
    print(f"Saved data_snapshot.json (as_of {as_of}, {len(combined)} rows).")


if __name__ == "__main__":
    main()
