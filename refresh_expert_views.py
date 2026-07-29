"""
Background script to automatically generate AI Expert Takes for all
watchlisted tickers. Intended to run via GitHub Actions at 1:00 PM ET daily.
It uses data_snapshot.json and automatically pulls news from news_summary.json.
"""

import os
import time
from datetime import datetime, timezone
from google import genai
from stock_data import load_data_snapshot, load_watchlists
from expert_views import load_expert_views, save_expert_views, generate_expert_view, _is_valid_view
from news_summary import get_gemini_api_key, get_nvidia_api_key, load_news_summary


def _ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def main():
    api_key = get_gemini_api_key()
    nvidia_api_key = get_nvidia_api_key()

    if not api_key:
        print("Error: GEMINI_API_KEY not found. Skipping expert views refresh.")
        return

    client = genai.Client(api_key=api_key)
    
    # Load data snapshot (generated at 7 AM ET)
    snapshot = load_data_snapshot()
    if not snapshot or "per_market" not in snapshot:
        print("Error: Invalid or missing data_snapshot.json. Run refresh_data.py first.")
        return

    # Load global watchlist to know exactly what to process
    watchlists = load_watchlists()
    expert_views = load_expert_views()
    news_summary_data = load_news_summary()

    total_processed = 0
    total_failed = 0

    # Process each market
    for market, mkt_tickers in watchlists.items():
        if market not in snapshot["per_market"]:
            continue
            
        print(f"\n[{_ts()}] === {market} Watchlist ({len(mkt_tickers)} tickers) ===")
        results = snapshot["per_market"][market]

        for idx, tk in enumerate(mkt_tickers):
            try:
                row = next((r for r in results if r["ticker"] == tk), None)
            except Exception:
                row = None

            if not row:
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - SKIP (no data in snapshot)")
                continue

            company_name = row.get("company_name", tk)
            print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} ({company_name}) - starting...")

            old_view = expert_views.get(tk)

            # Find news for this ticker from news_summary.json as fallback
            ticker_news_fallback = None
            if news_summary_data and market in news_summary_data:
                market_news = news_summary_data[market].get("news_by_ticker", {})
                if tk in market_news:
                    ticker_news_fallback = market_news[tk]

            try:
                t0 = time.time()
                view = generate_expert_view(
                    client,
                    row,
                    nvidia_api_key=nvidia_api_key,
                    news_text_fallback=ticker_news_fallback
                )
                elapsed = time.time() - t0

                if _is_valid_view(view):
                    expert_views[tk] = view
                    print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - OK ({elapsed:.1f}s) verdict={view.get('verdict')}")
                elif _is_valid_view(old_view):
                    print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - FAILED ({elapsed:.1f}s), keeping prior result")
                else:
                    expert_views[tk] = view
                    print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - FAILED ({elapsed:.1f}s): {view.get('headline')}")
                    total_failed += 1
            except Exception as e:
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - EXCEPTION: {e}")
                total_failed += 1

            # Save incrementally in case the script times out or crashes
            save_expert_views(expert_views)
            total_processed += 1

            # Sleep to strictly respect Gemini RPM (Requests Per Minute) limits
            if idx < len(mkt_tickers) - 1:
                time.sleep(5)

    print(f"\nDone. Processed {total_processed} tickers. {total_failed} failures.")

if __name__ == "__main__":
    main()
