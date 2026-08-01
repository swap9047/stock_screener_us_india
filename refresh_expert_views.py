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
from expert_views import (
    load_expert_views, save_expert_views, generate_expert_view, _is_valid_view,
    EXPERT_STALE_DAYS, _view_age_days, stale_view_fallback,
)
from news_summary import get_gemini_api_key, get_nvidia_api_key


def _ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def _apply_result(expert_views, tk, view, old_view, elapsed):
    """Decide what to persist for one ticker. Returns (failed_inc, fallback_inc, detail).

    - Fresh valid view: write it.
    - Failed generation with a fresh prior: keep prior (bounded by staleness).
    - Failed generation with a stale prior (regeneration has failed for more
      than EXPERT_STALE_DAYS): overwrite with an honest pending placeholder
      rather than silently keep showing an unverified old verdict.
    - Failed generation with no usable prior: write the failed view as-is.
    """
    if _is_valid_view(view):
        expert_views[tk] = view
        fallback_inc = 1 if "⚪" in view.get("news_source", "") else 0
        return 0, fallback_inc, f"OK ({elapsed:.1f}s) verdict={view.get('verdict')}"
    if _is_valid_view(old_view):
        age = _view_age_days(old_view)
        if age is not None and age > EXPERT_STALE_DAYS:
            expert_views[tk] = stale_view_fallback(f"previous view is {age:.1f} days old")
            return 1, 0, f"FAILED ({elapsed:.1f}s); prior stale ({age:.1f}d) -> wrote pending"
        return 0, 0, f"FAILED ({elapsed:.1f}s), keeping prior result"
    expert_views[tk] = view
    return 1, 0, f"FAILED ({elapsed:.1f}s): {view.get('headline')}"

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

    total_processed = 0
    total_failed = 0
    total_fallback_used = 0
    retry_queue = []

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

            try:
                t0 = time.time()
                view = generate_expert_view(
                    client,
                    row,
                    nvidia_api_key=nvidia_api_key,
                    is_retry=False
                )
                elapsed = time.time() - t0

                failed_inc, fallback_inc, detail = _apply_result(expert_views, tk, view, old_view, elapsed)
                total_failed += failed_inc
                total_fallback_used += fallback_inc
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - {detail}")
            except TimeoutError as e:
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - TIMEOUT: {e}. Added to retry queue.")
                retry_queue.append((market, tk, row, old_view))
            except Exception as e:
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - EXCEPTION: {e}")
                total_failed += 1

            # Save incrementally in case the script times out or crashes
            save_expert_views(expert_views)
            total_processed += 1

            # Sleep to strictly respect Gemini RPM (Requests Per Minute) limits
            if idx < len(mkt_tickers) - 1:
                time.sleep(5)
                
    # Process retry queue
    if retry_queue:
        print(f"\n[{_ts()}] === Processing Retry Queue ({len(retry_queue)} tickers) ===")
        for idx, (market, tk, row, old_view) in enumerate(retry_queue):
            company_name = row.get("company_name", tk)
            print(f"[{_ts()}] [RETRY] [{market}] [{idx+1}/{len(retry_queue)}] {tk} ({company_name}) - starting...")
            try:
                t0 = time.time()
                view = generate_expert_view(
                    client,
                    row,
                    nvidia_api_key=nvidia_api_key,
                    is_retry=True
                )
                elapsed = time.time() - t0

                failed_inc, fallback_inc, detail = _apply_result(expert_views, tk, view, old_view, elapsed)
                total_failed += failed_inc
                total_fallback_used += fallback_inc
                print(f"[{_ts()}] [RETRY] [{market}] [{idx+1}/{len(retry_queue)}] {tk} - {detail}")
            except Exception as e:
                print(f"[{_ts()}] [RETRY] [{market}] [{idx+1}/{len(retry_queue)}] {tk} - EXCEPTION: {e}")
                total_failed += 1
                
            save_expert_views(expert_views)

            if idx < len(retry_queue) - 1:
                time.sleep(30)

    print(f"\nDone. Processed {total_processed} initial tickers and {len(retry_queue)} retries.")
    print(f"No-Source Fallbacks: {total_fallback_used} times.")
    print(f"Final failures: {total_failed}.")

if __name__ == "__main__":
    main()
