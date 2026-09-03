"""
Background script to automatically generate AI Expert Takes for all
watchlisted tickers. Run by expert-views.yml at 11:00 PM ET daily (03:00 UTC
EDT / 04:00 UTC EST), as the step AFTER that workflow's own refresh_data.py --
so data_snapshot.json is same-day by construction, not by timing luck.

News is fetched per ticker by expert_views.fetch_gemma_expert_news (a grounded
search over the last 24 hours), not read from news_summary.json.
"""

import os
import time
from datetime import datetime, timezone
from google import genai
from stock_data import load_data_snapshot, load_watchlists
from expert_views import (
    load_expert_views, save_expert_views, generate_expert_view, _is_valid_view,
    resolve_persisted_view,
    EXPERT_STALE_DAYS, _view_age_days, stale_view_fallback,
)
from alerts import active_alerts_for_prompt, alerts_text_for
from news_summary import get_gemini_api_key


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
    # The four-case decision lives in expert_views.resolve_persisted_view so the
    # dashboard's re-analyze button applies exactly the same rules -- it used to
    # write unconditionally and could overwrite a good verdict with a failure
    # stub. This function keeps ownership of the counters and log text.
    if _is_valid_view(view):
        expert_views[tk] = view
        fallback_inc = 1 if "⚪" in view.get("news_source", "") else 0
        return 0, fallback_inc, f"OK ({elapsed:.1f}s) verdict={view.get('verdict')}"

    to_store = resolve_persisted_view(view, old_view)
    if to_store is None:
        return 0, 0, f"FAILED ({elapsed:.1f}s), keeping prior result"
    expert_views[tk] = to_store
    if _is_valid_view(old_view):
        age = _view_age_days(old_view)
        return 1, 0, f"FAILED ({elapsed:.1f}s); prior stale ({age:.1f}d) -> wrote pending"
    return 1, 0, f"FAILED ({elapsed:.1f}s): {view.get('headline')}"

def _prune_orphans(store, watchlists, label):
    """Drop entries for tickers that are no longer in any watchlist.

    Nothing ever removed these, so they sat in the file forever, never
    refreshed, ageing past the staleness threshold -- fundamentals.json carried
    four (ADVENZYMES.NS, BNTX, SYNGENE.NS, and INTELLECT.BO, a ghost of the
    current INTELLECT.NS) and they were precisely the entries exceeding it.
    """
    live = {t for tks in watchlists.values() for t in tks}
    orphans = [t for t in store if t not in live]
    for t in orphans:
        del store[t]
    if orphans:
        print(f"[{_ts()}] Pruned {len(orphans)} {label} orphan(s) no longer in any watchlist: {', '.join(sorted(orphans))}")
    return len(orphans)


def main():
    api_key = get_gemini_api_key()

    if not api_key:
        print("Error: GEMINI_API_KEY not found. Skipping expert views refresh.")
        return

    client = genai.Client(api_key=api_key)
    
    # Load the snapshot refreshed by this same workflow minutes earlier.
    snapshot = load_data_snapshot()
    if not snapshot or "per_market" not in snapshot:
        print("Error: Invalid or missing data_snapshot.json. Run refresh_data.py first.")
        return

    # Load global watchlist to know exactly what to process
    watchlists = load_watchlists()
    expert_views = load_expert_views()
    _prune_orphans(expert_views, watchlists, "expert_views")

    # REFRESH_MARKETS scopes the run to specific watchlists -- set by the
    # dashboard's per-tab "Re-analyze All" button (see the workflow's
    # `markets` input). Blank/absent means every watchlist, which is what
    # the nightly schedule always gets.
    only_markets = {m.strip() for m in os.environ.get("REFRESH_MARKETS", "").split(",") if m.strip()}
    print(f"[{_ts()}] Scope: {', '.join(sorted(only_markets)) if only_markets else 'all watchlists'}")

    total_processed = 0
    total_failed = 0
    total_fallback_used = 0
    retry_queue = []
    # One analysis per ticker, not one per watchlist membership. The store is
    # keyed by bare ticker so duplicates overwrote each other -- and not even
    # equivalently, since the prompt embeds {market} and its benchmark, so the
    # surviving analysis depended on dict iteration order.
    seen = set()

    # Process each market
    for market, mkt_tickers in watchlists.items():
        if only_markets and market not in only_markets:
            continue
        if market not in snapshot["per_market"]:
            continue

        print(f"\n[{_ts()}] === {market} Watchlist ({len(mkt_tickers)} tickers) ===")
        results = snapshot["per_market"][market]
        # Section 2 of the Expert Take prompt ("ACTIVE ALERT RULES TRIGGERED").
        # Evaluated ONCE per market over the whole market's rows -- not per
        # ticker, and not over a subset, since a rule may reference another
        # rule and scope resolution needs the full set. Until now nothing ever
        # passed this, so every nightly analysis was told no rules had fired.
        alerts_by_ticker = active_alerts_for_prompt(results)
        if alerts_by_ticker is not None:
            print(f"[{_ts()}] Alert rules currently true for {len(alerts_by_ticker)}/{len(results)} tickers in {market}")

        for idx, tk in enumerate(mkt_tickers):
            if tk in seen:
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - SKIP (already analyzed this run)")
                continue
            seen.add(tk)
            row = next((r for r in results if r["ticker"] == tk), None)

            if not row:
                # Age out a ticker that has fallen out of the snapshot. This
                # used to be a bare `continue`, bypassing _apply_result
                # entirely -- so a ticker that dropped out kept displaying an
                # arbitrarily old ACCUMULATE forever, with nothing in the JSON
                # or the exit code saying so.
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - SKIP (no data in snapshot)")
                age = _view_age_days(old_view := expert_views.get(tk))
                if _is_valid_view(old_view) and age is not None and age > EXPERT_STALE_DAYS:
                    expert_views[tk] = stale_view_fallback(f"no snapshot row; previous view is {age:.1f} days old")
                    total_failed += 1
                continue

            company_name = row.get("company_name", tk)
            print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} ({company_name}) - starting...")

            old_view = expert_views.get(tk)

            alerts_text = alerts_text_for(alerts_by_ticker, tk)

            try:
                t0 = time.time()
                view = generate_expert_view(
                    client,
                    row,
                    active_alerts_text=alerts_text,
                    is_retry=False
                )
                elapsed = time.time() - t0

                failed_inc, fallback_inc, detail = _apply_result(expert_views, tk, view, old_view, elapsed)
                total_failed += failed_inc
                total_fallback_used += fallback_inc
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - {detail}")
            except TimeoutError as e:
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - TIMEOUT: {e}. Added to retry queue.")
                retry_queue.append((market, tk, row, old_view, alerts_text))
            except Exception as e:
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - EXCEPTION: {e}")
                total_failed += 1
                # Apply the staleness circuit breaker here too. Keeping the
                # prior view unconditionally is what let a permanently-failing
                # ticker display a confident verdict indefinitely; the
                # fundamentals refresh already does this in its own handler.
                age = _view_age_days(old_view)
                if _is_valid_view(old_view) and age is not None and age > EXPERT_STALE_DAYS:
                    expert_views[tk] = stale_view_fallback(f"{age:.1f} days old; last error: {e}")

            # Save incrementally in case the script times out or crashes
            save_expert_views(expert_views)
            total_processed += 1

            # Sleep to strictly respect Gemini RPM (Requests Per Minute) limits
            if idx < len(mkt_tickers) - 1:
                time.sleep(5)
                
    # Process retry queue
    if retry_queue:
        print(f"\n[{_ts()}] === Processing Retry Queue ({len(retry_queue)} tickers) ===")
        for idx, (market, tk, row, old_view, alerts_text) in enumerate(retry_queue):
            company_name = row.get("company_name", tk)
            print(f"[{_ts()}] [RETRY] [{market}] [{idx+1}/{len(retry_queue)}] {tk} ({company_name}) - starting...")
            try:
                t0 = time.time()
                view = generate_expert_view(
                    client,
                    row,
                    active_alerts_text=alerts_text,
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
