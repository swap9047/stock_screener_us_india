"""
Background script to automatically generate AI Fundamentals for all
watchlisted tickers. Intended to run via GitHub Actions.
"""

import concurrent.futures
import os
import sys
import threading
import time
from datetime import datetime, timezone
import llm_util
from json_store import DataFileError
from google import genai
from stock_data import load_data_snapshot, load_watchlists
from fundamentals_eval import (
    load_fundamentals, save_fundamentals, generate_fundamental_view,
    _is_valid_view, _validate_sentiment, _view_age_days, SENTIMENT_STALE_DAYS,
)
from news_summary import get_gemini_api_key

def _ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def _unknown_fallback(reason):
    """Full-schema Unknown view for failures/stale keep-prior cases.

    Always writes every schema key so the UI never renders a tooltip of
    missing/N/A fields for a bare {sentiment, reasoning} dict.
    """
    # Every schema key, genuinely. This used to omit seven of them
    # (earnings_report_date, eps_value, guidance_change, analyst_action,
    # news_used, quarter_verified, real_earnings_date) despite the docstring
    # above -- which silently dropped _has_hard_evidence onto its legacy
    # free-text branch, since that branch is selected by testing whether the
    # structured keys are PRESENT.
    return {
        "earnings_summary": "N/A",
        "future_guidance": "N/A",
        "analyst_coverage": "N/A",
        "earnings_report_date": None,
        "eps_value": None,
        "guidance_change": None,
        "analyst_action": None,
        "sentiment": "Unknown",
        "reasoning": f"Analysis unavailable -- {reason}",
        "targeted_retry": None,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "news_used": "",
        "news_source": "⚪ No Source",
        "model_used": "Error",
        "quarter_verified": True,
        "real_earnings_date": None,
        "guard_flag": "NO_DATA",
    }

def _apply_result(fundamentals, tk, view, old_view, elapsed):
    """Decide what to persist for one ticker. Returns (failed_inc, detail).

    - Fresh valid view (incl. "Unknown"): apply deterministic guard, write it.
    - Failed generation with fresh prior: keep prior (bounded by staleness).
    - Failed generation with stale prior: overwrite with honest Unknown.
    - Failed generation with no usable prior: write full-schema Unknown.
    """
    if _is_valid_view(view):
        # Record what the guard thinks, but do NOT overwrite the model's own
        # verdict on disk. This used to do
        #     view["sentiment"] = "Neutral" if flag == "PARTIAL" else "Unknown"
        # which was both redundant and lossy: app.py re-runs _validate_sentiment
        # at every render site, so the display was already guarded, while the
        # raw Positive/Negative was destroyed in the file. That is why fixing
        # _check_quarter_freshness alone repaired nothing already written --
        # there was no verdict left to recover.
        #
        # It also broke PARTIAL specifically: once "Neutral" was persisted, the
        # next _validate_sentiment saw Neutral, skipped the hard-evidence check
        # and returned no flag, so sentiment_flag_note rendered nothing and the
        # user saw a bare Neutral with no explanation of the downgrade.
        guarded, flag = _validate_sentiment(view)
        view["guard_flag"] = flag or None
        fundamentals[tk] = view
        shown = f"{view.get('sentiment')} -> {guarded} ({flag})" if flag else view.get("sentiment")
        return 0, f"OK ({elapsed:.1f}s) sentiment={shown}"
    if _is_valid_view(old_view):
        age = _view_age_days(old_view)
        if age is not None and age > SENTIMENT_STALE_DAYS:
            fundamentals[tk] = _unknown_fallback(f"previous view is {age:.1f} days old")
            return 1, f"FAILED ({elapsed:.1f}s); prior stale ({age:.1f}d) -> wrote Unknown"
        return 0, f"FAILED ({elapsed:.1f}s), keeping prior result"
    reason = str(view.get("reasoning", view.get("sentiment", "no valid result")))[:160]
    fundamentals[tk] = _unknown_fallback(reason)
    return 1, f"FAILED ({elapsed:.1f}s): {view.get('sentiment')} -> wrote Unknown"

def _prune_orphans(store, watchlists, label):
    """Drop entries for tickers that are no longer in any watchlist.

    Nothing ever removed these, so they sat in the file forever, never
    refreshed, ageing past the staleness threshold -- fundamentals.json carried
    four (one of them a .BO ghost of a ticker since re-added as .NS) and they
    were precisely the entries exceeding it.
    """
    live = {t for tks in watchlists.values() for t in tks}
    orphans = [t for t in store if t not in live]
    for t in orphans:
        del store[t]
    if orphans:
        print(f"[{_ts()}] Pruned {len(orphans)} {label} orphan(s) no longer in any watchlist: {', '.join(sorted(orphans))}")
    return len(orphans)


# How many tickers are analysed at once. Each ticker is a blocking chain of
# grounded search + reasoning calls, so this job is IO-bound, not CPU-bound: the
# 2026-09-18 run took 353 of its 360-minute cap and 136 of those minutes were
# spent waiting on calls that never answered. Two workers roughly halve the wall
# clock. Two and not more because each one holds a Gemini call open and the
# grounded-search models were already returning 500s and timing out under
# ordinary load -- adding request pressure is the way to turn slowness into
# failure. Raise it only with a run's [key rotation] line to show there is room.
MAX_CONCURRENT_TICKERS = 2

# Pause after each ticker, per worker. Named because the two used to be bare
# literals and were easy to misread: the main loop has always been the small one.
TICKER_PAUSE_SECONDS = 2
RETRY_PAUSE_SECONDS = 5


def main():
    api_key = get_gemini_api_key()
    if not api_key:
        print("Error: GEMINI_API_KEY not found. Skipping fundamentals refresh.")
        return

    client = llm_util.make_client(api_key)
    
    snapshot = load_data_snapshot()
    if not snapshot or "per_market" not in snapshot:
        print("Error: Invalid or missing data_snapshot.json.")
        return

    watchlists = load_watchlists()
    # A corrupt store stops the run instead of rebuilding it from empty. This
    # script rewrites the whole file from what it loaded, and commit-data
    # (if: always()) would push that -- so exiting 1 and letting the slot gate
    # retry the slot is the only safe answer. Same choice alert_check.py makes
    # for a partial universe.
    try:
        fundamentals = load_fundamentals()
    except DataFileError as e:
        print(f"::error::{e}")
        sys.exit(1)
    _prune_orphans(fundamentals, watchlists, "fundamentals")

    # REFRESH_MARKETS scopes the run to specific watchlists -- set by the
    # dashboard's per-tab "Re-analyze All" button (see the workflow's
    # `markets` input). Blank/absent means every watchlist, which is what
    # the nightly schedule always gets.
    only_markets = {m.strip() for m in os.environ.get("REFRESH_MARKETS", "").split(",") if m.strip()}
    limit = llm_util.refresh_limit()
    print(f"[{_ts()}] Scope: {', '.join(sorted(only_markets)) if only_markets else 'all watchlists'}"
          + (f", first {limit} ticker(s) only" if limit else ""))

    # A ticker in several watchlists is one analysis, not several. The store is
    # keyed by bare ticker, so the extra runs were pure waste that overwrote
    # each other -- 110 slots for 105 unique tickers today (one x3,
    # three x2), i.e. 5 redundant search+reasoning pairs a night.
    seen = set()

    # The plan is built BEFORE any worker starts, serially and in registry order,
    # so REFRESH_LIMIT picks the same first N tickers it always did and the
    # already-analysed/no-row skips stay deterministic. Only the analysis itself
    # is concurrent.
    plan = []
    for market, mkt_tickers in watchlists.items():
        if limit and len(plan) >= limit:
            break
        if only_markets and market not in only_markets:
            continue
        if market not in snapshot["per_market"]:
            continue

        print(f"\n[{_ts()}] === {market} Watchlist ({len(mkt_tickers)} tickers) ===")
        results = snapshot["per_market"][market]

        for idx, tk in enumerate(mkt_tickers):
            if limit and len(plan) >= limit:
                break
            if tk in seen:
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - SKIP (already analyzed this run)")
                continue
            seen.add(tk)
            row = next((r for r in results if r["ticker"] == tk), None)
            if not row:
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - SKIP (no data in snapshot)")
                continue
            plan.append((market, idx, len(mkt_tickers), tk, row))

    store_lock = threading.Lock()
    counters = {"processed": 0, "failed": 0}

    def _analyse(entry, is_retry=False):
        """One ticker, start to stored result. Runs on a worker thread.

        Every mutation of `fundamentals` and every save happens under
        store_lock: save_fundamentals rewrites the whole file, so two threads
        saving at once could interleave into a torn write, and the store is also
        the thing commit-data pushes. The Gemini calls -- the slow part -- are
        outside the lock, which is the entire point.
        """
        market, idx, total, tk, row = entry
        tag = f"[RETRY]" if is_retry else f"[{market}] [{idx+1}/{total}]"
        company_name = row.get("company_name", tk)
        old_view = fundamentals.get(tk)
        if not is_retry:
            print(f"[{_ts()}] {tag} {tk} ({company_name}) - starting...")
        else:
            print(f"[{_ts()}] {tag} {tk} - starting...")

        requeue = None
        try:
            t0 = time.time()
            view = generate_fundamental_view(client, row, is_retry=is_retry)
            elapsed = time.time() - t0
            with store_lock:
                failed_inc, detail = _apply_result(fundamentals, tk, view, old_view, elapsed)
                counters["failed"] += failed_inc
                save_fundamentals(fundamentals)
            print(f"[{_ts()}] {tag} {tk} - {detail}")
        except TimeoutError as e:
            # First pass only: fetch_fundamental_news raises this so the ticker
            # can be tried again later. On the retry pass is_retry=True makes an
            # exhausted search settle for "no news" instead of raising.
            print(f"[{_ts()}] {tag} {tk} - TIMEOUT: {e}. Added to retry queue.")
            requeue = entry
        except Exception as e:
            print(f"[{_ts()}] {tag} {tk} - ERROR: {e}")
            age = _view_age_days(old_view)
            with store_lock:
                if _is_valid_view(old_view) and (age is None or age <= SENTIMENT_STALE_DAYS):
                    pass  # keep prior fresh result
                else:
                    reason = str(e)
                    if _is_valid_view(old_view):
                        reason = f"previous view is {age:.1f} days old; {reason}"
                    fundamentals[tk] = _unknown_fallback(reason)
                counters["failed"] += 1
                save_fundamentals(fundamentals)
        with store_lock:
            counters["processed"] += 1
        time.sleep(RETRY_PAUSE_SECONDS if is_retry else TICKER_PAUSE_SECONDS)
        return requeue

    def _run_all(entries, is_retry=False):
        """`entries` through the pool, returning whatever asked to be requeued.

        A ThreadPoolExecutor is safe here even though llm_util avoids one: the
        hang it warns about came from shutdown(wait=False) abandoning a worker.
        These workers always finish, because every Gemini call inside them is
        bounded by llm_util.call_with_timeout, and `with` joins them all.
        """
        if not entries:
            return []
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TICKERS) as pool:
            return [r for r in pool.map(lambda e: _analyse(e, is_retry), entries) if r]

    retry_queue = _run_all(plan)

    if retry_queue:
        print(f"\n[{_ts()}] === Retrying {len(retry_queue)} timed-out stocks ===")
        _run_all(retry_queue, is_retry=True)

    total_processed = counters["processed"]
    total_failed = counters["failed"]

    print(f"\n[{_ts()}] Fundamentals refresh complete.")
    print(f"Processed: {total_processed}")
    print(f"Failed: {total_failed}")
    # Per-key calls and failures, worst key first -- see usage_summary.
    llm_util.log_key_usage(client)

if __name__ == "__main__":
    main()
