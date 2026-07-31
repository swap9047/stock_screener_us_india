"""
Background script to automatically generate AI Fundamentals for all
watchlisted tickers. Intended to run via GitHub Actions.
"""

import os
import time
from datetime import datetime, timezone
from google import genai
from stock_data import load_data_snapshot, load_watchlists
from fundamentals_eval import load_fundamentals, save_fundamentals, generate_fundamental_view, _is_valid_view
from news_summary import get_gemini_api_key, get_nvidia_api_key

def _ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def main():
    api_key = get_gemini_api_key()
    if not api_key:
        print("Error: GEMINI_API_KEY not found. Skipping fundamentals refresh.")
        return

    client = genai.Client(api_key=api_key)
    
    snapshot = load_data_snapshot()
    if not snapshot or "per_market" not in snapshot:
        print("Error: Invalid or missing data_snapshot.json.")
        return

    watchlists = load_watchlists()
    fundamentals = load_fundamentals()

    total_processed = 0
    total_failed = 0
    retry_queue = []

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

            old_view = fundamentals.get(tk)

            try:
                t0 = time.time()
                view = generate_fundamental_view(
                    client,
                    row,
                    is_retry=False
                )
                elapsed = time.time() - t0

                if _is_valid_view(view):
                    fundamentals[tk] = view
                    print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - OK ({elapsed:.1f}s) sentiment={view.get('sentiment')}")
                elif _is_valid_view(old_view):
                    print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - FAILED ({elapsed:.1f}s), keeping prior result")
                else:
                    fundamentals[tk] = view
                    print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - FAILED ({elapsed:.1f}s): {view.get('sentiment')}")
                    total_failed += 1
            except TimeoutError as e:
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - TIMEOUT: {e}. Added to retry queue.")
                retry_queue.append((market, tk, row, old_view))
            except Exception as e:
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - ERROR: {e}")
                if not _is_valid_view(old_view):
                    fundamentals[tk] = {"sentiment": "Unknown", "reasoning": f"Error: {str(e)}"}
                total_failed += 1
            
            total_processed += 1
            save_fundamentals(fundamentals)
            time.sleep(2)

    # Retry Phase
    if retry_queue:
        print(f"\n[{_ts()}] === Retrying {len(retry_queue)} timed-out stocks ===")
        for idx, (market, tk, row, old_view) in enumerate(retry_queue):
            print(f"[{_ts()}] [RETRY] [{idx+1}/{len(retry_queue)}] {tk} - starting...")
            try:
                t0 = time.time()
                view = generate_fundamental_view(
                    client,
                    row,
                    is_retry=True
                )
                elapsed = time.time() - t0

                if _is_valid_view(view):
                    fundamentals[tk] = view
                    print(f"[{_ts()}] [RETRY] [{idx+1}/{len(retry_queue)}] {tk} - OK ({elapsed:.1f}s) sentiment={view.get('sentiment')}")
                elif _is_valid_view(old_view):
                    print(f"[{_ts()}] [RETRY] [{idx+1}/{len(retry_queue)}] {tk} - FAILED ({elapsed:.1f}s), keeping prior result")
                else:
                    fundamentals[tk] = view
                    print(f"[{_ts()}] [RETRY] [{idx+1}/{len(retry_queue)}] {tk} - FAILED ({elapsed:.1f}s): {view.get('sentiment')}")
                    total_failed += 1
            except Exception as e:
                print(f"[{_ts()}] [RETRY] [{idx+1}/{len(retry_queue)}] {tk} - ERROR: {e}")
                if not _is_valid_view(old_view):
                    fundamentals[tk] = {"sentiment": "Unknown", "reasoning": f"Error: {str(e)}"}
                total_failed += 1
            
            save_fundamentals(fundamentals)
            time.sleep(2)

    print(f"\n[{_ts()}] Fundamentals refresh complete.")
    print(f"Processed: {total_processed}")
    print(f"Failed: {total_failed}")

if __name__ == "__main__":
    main()
