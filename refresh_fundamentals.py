"""
Background script to automatically generate AI Fundamentals for all
watchlisted tickers. Intended to run via GitHub Actions.
"""

import os
import time
from datetime import datetime, timezone
from google import genai
from stock_data import load_data_snapshot, load_watchlists
from fundamentals_eval import (
    load_fundamentals, save_fundamentals, generate_fundamental_view,
    _is_valid_view, _validate_sentiment, _view_age_days, SENTIMENT_STALE_DAYS,
)
from news_summary import get_gemini_api_key, get_nvidia_api_key

def _ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def _unknown_fallback(reason):
    """Full-schema Unknown view for failures/stale keep-prior cases.

    Always writes every schema key so the UI never renders a tooltip of
    missing/N/A fields for a bare {sentiment, reasoning} dict.
    """
    return {
        "earnings_summary": "N/A",
        "future_guidance": "N/A",
        "analyst_coverage": "N/A",
        "sentiment": "Unknown",
        "reasoning": f"Analysis unavailable -- {reason}",
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "news_source": "⚪ No Source",
        "model_used": "Error",
    }

def _apply_result(fundamentals, tk, view, old_view, elapsed):
    """Decide what to persist for one ticker. Returns (failed_inc, detail).

    - Fresh valid view (incl. "Unknown"): apply deterministic guard, write it.
    - Failed generation with fresh prior: keep prior (bounded by staleness).
    - Failed generation with stale prior: overwrite with honest Unknown.
    - Failed generation with no usable prior: write full-schema Unknown.
    """
    if _is_valid_view(view):
        sentiment, flag = _validate_sentiment(view)
        if flag:
            view["sentiment"] = "Neutral" if flag == "PARTIAL" else "Unknown"
            view["reasoning"] = f"{view.get('reasoning', '')}\n[auto-downgraded: {flag}]"
        fundamentals[tk] = view
        return 0, f"OK ({elapsed:.1f}s) sentiment={view.get('sentiment')}"
    if _is_valid_view(old_view):
        age = _view_age_days(old_view)
        if age is not None and age > SENTIMENT_STALE_DAYS:
            fundamentals[tk] = _unknown_fallback(f"previous view is {age:.1f} days old")
            return 1, f"FAILED ({elapsed:.1f}s); prior stale ({age:.1f}d) -> wrote Unknown"
        return 0, f"FAILED ({elapsed:.1f}s), keeping prior result"
    reason = str(view.get("reasoning", view.get("sentiment", "no valid result")))[:160]
    fundamentals[tk] = _unknown_fallback(reason)
    return 1, f"FAILED ({elapsed:.1f}s): {view.get('sentiment')} -> wrote Unknown"

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

                failed_inc, detail = _apply_result(fundamentals, tk, view, old_view, elapsed)
                total_failed += failed_inc
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - {detail}")
            except TimeoutError as e:
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - TIMEOUT: {e}. Added to retry queue.")
                retry_queue.append((market, tk, row, old_view))
            except Exception as e:
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - ERROR: {e}")
                age = _view_age_days(old_view)
                if _is_valid_view(old_view) and (age is None or age <= SENTIMENT_STALE_DAYS):
                    pass  # keep prior fresh result
                else:
                    reason = str(e)
                    if _is_valid_view(old_view):
                        reason = f"previous view is {age:.1f} days old; {reason}"
                    fundamentals[tk] = _unknown_fallback(reason)
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

                failed_inc, detail = _apply_result(fundamentals, tk, view, old_view, elapsed)
                total_failed += failed_inc
                print(f"[{_ts()}] [RETRY] [{idx+1}/{len(retry_queue)}] {tk} - {detail}")
            except Exception as e:
                print(f"[{_ts()}] [RETRY] [{idx+1}/{len(retry_queue)}] {tk} - ERROR: {e}")
                age = _view_age_days(old_view)
                if _is_valid_view(old_view) and (age is None or age <= SENTIMENT_STALE_DAYS):
                    pass  # keep prior fresh result
                else:
                    reason = str(e)
                    if _is_valid_view(old_view):
                        reason = f"previous view is {age:.1f} days old; {reason}"
                    fundamentals[tk] = _unknown_fallback(reason)
                total_failed += 1
            
            save_fundamentals(fundamentals)
            time.sleep(2)

    print(f"\n[{_ts()}] Fundamentals refresh complete.")
    print(f"Processed: {total_processed}")
    print(f"Failed: {total_failed}")

if __name__ == "__main__":
    main()
