#!/usr/bin/env python3
"""
Scheduled background script for pre-computing AI Expert Views (gemini-3.6-flash)
across both US and India watchlists. Runs alongside refresh_data.py in GitHub Actions.
Saves results to expert_views.json.
"""

import os
import time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv(".env")

from stock_data import load_watchlists, load_settings, fetch_all_markets
from news_summary import get_gemini_api_key
from expert_views import load_expert_views, save_expert_views, generate_expert_view


def main():
    api_key = get_gemini_api_key()
    if not api_key:
        print("GEMINI_API_KEY not configured. Skipping expert views refresh.")
        return

    from google import genai
    client = genai.Client(api_key=api_key)

    settings = load_settings()
    watchlists = load_watchlists()
    combined, as_of, per_market = fetch_all_markets(watchlists=watchlists, settings=settings)

    if not combined:
        print("No tickers found. Skipping expert views refresh.")
        return

    print(f"Refreshing AI Expert Views for {len(combined)} tickers...")
    existing = load_expert_views()
    updated_count = 0

    for row in combined:
        ticker = row.get("ticker")
        if not ticker:
            continue
        
        print(f"  Analyzing {ticker}...")
        view = generate_expert_view(client, row)
        existing[ticker] = view
        updated_count += 1
        time.sleep(4.1)  # 15 RPM free tier limit

    save_expert_views(existing)
    print(f"Successfully saved {updated_count} expert views to expert_views.json.")


if __name__ == "__main__":
    main()
