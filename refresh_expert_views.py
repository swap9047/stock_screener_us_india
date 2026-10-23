"""
Background script to automatically generate AI Expert Takes for all
watchlisted tickers. Run by expert-views.yml at 1:00 AM ET daily (05:00 UTC
EDT / 06:00 UTC EST), as the step AFTER that workflow's own refresh_data.py --
so data_snapshot.json is same-day by construction, not by timing luck.

News is fetched per ticker by expert_views.fetch_gemma_expert_news (a grounded
search over the last 14 days of material non-earnings news), not read from
news_summary.json. The verdict itself is decided in code from the columns
(expert_views.decide_expert_verdict); the model writes the explanation and may
report a news risk that lowers it one step.
"""

import concurrent.futures
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import llm_util
from json_store import DataFileError
from stock_data import load_data_snapshot, load_watchlists
from expert_views import (
    load_expert_views, save_expert_views, generate_expert_view, _is_valid_view, carry_news_risk,
    resolve_persisted_view,
    EXPERT_STALE_DAYS, _view_age_days, stale_view_fallback,
)
from alerts import active_alerts_for_prompt, alerts_text_for
from fundamentals_eval import load_fundamentals
from news_summary import get_gemini_api_key


# Tickers analysed at once (2026-10-23). Serial runs had reached 4-6 h a night
# for ~150 tickers, against GitHub's 360-minute job limit, which cancelled three
# of them (2026-10-05 x2, 2026-10-20). The Sentiment job and the news digest
# have run 3 at a time since 2026-10-04 with no rise in failures; see
# refresh_fundamentals.MAX_CONCURRENT_TICKERS for why not more.
MAX_CONCURRENT_TICKERS = 3

# Pause after each ticker, per worker (the serial loop slept 5 s / 30 s).
TICKER_PAUSE_SECONDS = 5
RETRY_PAUSE_SECONDS = 30

# The gate's slot for this workflow (expert-views.yml: slots "1"), in ET.
# checks/test_expert_views_resume.py pins the two together.
SLOT_HOUR_ET = 1
ET = ZoneInfo("America/New_York")


def _ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def slot_start_utc(now=None):
    """The start of the slot this run works: the latest SLOT_HOUR_ET:00 ET at or
    before now, in UTC. The same rule as the slot gate's latest_slot."""
    now_et = (now or datetime.now(timezone.utc)).astimezone(ET)
    start = now_et.replace(hour=SLOT_HOUR_ET, minute=0, second=0, microsecond=0)
    if start > now_et:
        start = (now_et - timedelta(days=1)).replace(hour=SLOT_HOUR_ET, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)


def written_since(view, since_utc):
    """True for a VALID view whose as_of (UTC, "YYYY-MM-DD HH:MM") is at or after
    `since_utc`. A failure stub or pending placeholder is never "done"."""
    if not _is_valid_view(view):
        return False
    try:
        ts = datetime.fromisoformat(str(view.get("as_of", "")).replace(" ", "T", 1))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= since_utc

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
        expert_views[tk] = carry_news_risk(view, old_view)
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


def main():
    api_key = get_gemini_api_key()

    if not api_key:
        print("Error: GEMINI_API_KEY not found. Skipping expert views refresh.")
        return

    client = llm_util.make_client(api_key)
    
    # Load the snapshot refreshed by this same workflow minutes earlier.
    snapshot = load_data_snapshot()
    if not snapshot or "per_market" not in snapshot:
        print("Error: Invalid or missing data_snapshot.json. Run refresh_data.py first.")
        return

    # Load global watchlist to know exactly what to process
    watchlists = load_watchlists()
    # A corrupt store stops the run instead of rebuilding it from empty. This
    # script rewrites the whole file from what it loaded, and commit-data
    # (if: always()) would push that -- so exiting 1 and letting the slot gate
    # retry the slot is the only safe answer. Same choice alert_check.py makes
    # for a partial universe.
    try:
        expert_views = load_expert_views()
    except DataFileError as e:
        print(f"::error::{e}")
        sys.exit(1)
    _prune_orphans(expert_views, watchlists, "expert_views")
    # Section 4 of the prompt: this quarter's checked fundamentals from the
    # Sentiment job (9 PM ET), read once here rather than once per ticker.
    # Unreadable is not fatal here: the Sentiment job fails loudly on its own
    # file, and Expert Take is still worth running on technicals and news.
    try:
        fundamental_views = load_fundamentals()
        print(f"[{_ts()}] Sentiment facts on file for {len(fundamental_views)} tickers")
    except DataFileError as e:
        print(f"::warning::{e} -- Expert Take runs without this quarter's fundamentals")
        fundamental_views = None

    # REFRESH_MARKETS scopes the run to specific watchlists -- set by the
    # dashboard's per-tab "Re-analyze All" button (see the workflow's
    # `markets` input). Blank/absent means every watchlist, which is what
    # the nightly schedule always gets.
    only_markets = {m.strip() for m in os.environ.get("REFRESH_MARKETS", "").split(",") if m.strip()}
    limit = llm_util.refresh_limit()
    print(f"[{_ts()}] Scope: {', '.join(sorted(only_markets)) if only_markets else 'all watchlists'}"
          + (f", first {limit} ticker(s) only" if limit else ""))
    counters = {"processed": 0, "failed": 0, "fallback": 0}
    # Resume (2026-10-23): a SCHEDULED run of the full scope skips tickers whose
    # view was already written after tonight's slot. The gate retries a slot whose
    # run stopped part-way -- the FailureFuse on spent quota, or GitHub's 6-hour
    # job limit -- and every retry used to start again from the first ticker, so
    # on 2026-10-20 four attempts (~16 h of runtime) re-analysed what earlier ones
    # had finished and spent the quota that then tripped the fuse again. A manual
    # dispatch, or a scoped/limited one (the app's Re-analyze buttons, smoke
    # tests), still redoes everything: someone asked for it.
    resume_since = None
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and not only_markets and not limit:
        resume_since = slot_start_utc()
        print(f"[{_ts()}] Resume: skipping views already written since {resume_since:%Y-%m-%d %H:%M} UTC (this slot)")

    # One analysis per ticker, not one per watchlist membership. The store is
    # keyed by bare ticker so duplicates overwrote each other -- and not even
    # equivalently, since the prompt embeds {market} and its benchmark, so the
    # surviving analysis depended on dict iteration order.
    seen = set()
    # The plan is built BEFORE any worker starts, serially and in registry order
    # (as refresh_fundamentals does), so REFRESH_LIMIT picks the same first N
    # tickers it always did and the skips stay deterministic. Only the analysis
    # itself is concurrent.
    plan = []
    resumed = 0
    for market, mkt_tickers in watchlists.items():
        if limit and len(plan) >= limit:
            break
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
            if limit and len(plan) >= limit:
                break
            if tk in seen:
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - SKIP (already queued this run)")
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
                    counters["failed"] += 1
                continue

            if not row.get("trend"):
                # Too little history for a Trend: Expert Take is Pending and
                # needs no write-up. Not a failure -- it used to cost two model
                # calls a night and count toward the FailureFuse.
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - SKIP (no Trend yet)")
                continue
            if resume_since is not None and written_since(expert_views.get(tk), resume_since):
                print(f"[{_ts()}] [{market}] [{idx+1}/{len(mkt_tickers)}] {tk} - SKIP (already analysed this slot)")
                resumed += 1
                continue
            plan.append((market, idx, len(mkt_tickers), tk, row, alerts_text_for(alerts_by_ticker, tk)))

    if resumed:
        print(f"[{_ts()}] Resume: {resumed} ticker(s) already done this slot; {len(plan)} to analyse")
    # The no-row age-outs above edited the store; save them before any worker starts.
    save_expert_views(expert_views)

    store_lock = threading.Lock()
    fuse = llm_util.FailureFuse()

    def _analyse(entry, is_retry=False):
        """One ticker, start to stored result. Runs on a worker thread.

        Every mutation of `expert_views` and every save happens under
        store_lock: save_expert_views rewrites the whole file, so two threads
        saving at once could interleave into a torn write, and the store is also
        what commit-data pushes. The Gemini calls -- the slow part -- are outside
        the lock, which is the entire point.
        """
        market, idx, total, tk, row, alerts_text = entry
        if fuse.tripped:
            return None  # after a run of failures: keep this ticker's previous view
        tag = "[RETRY]" if is_retry else f"[{market}] [{idx+1}/{total}]"
        old_view = expert_views.get(tk)
        print(f"[{_ts()}] {tag} {tk} ({row.get('company_name', tk)}) - starting...")
        requeue = None
        try:
            t0 = time.time()
            view = generate_expert_view(
                client,
                row,
                active_alerts_text=alerts_text,
                is_retry=is_retry,
                fundamental_view=(fundamental_views.get(tk) or {}) if fundamental_views is not None else None,
            )
            elapsed = time.time() - t0
            with store_lock:
                failed_inc, fallback_inc, detail = _apply_result(expert_views, tk, view, old_view, elapsed)
                counters["failed"] += failed_inc
                counters["fallback"] += fallback_inc
                # Save incrementally in case the script times out or crashes
                save_expert_views(expert_views)
            fuse.record(not _is_valid_view(view))
            print(f"[{_ts()}] {tag} {tk} - {detail}")
        except TimeoutError as e:
            if is_retry:
                print(f"[{_ts()}] {tag} {tk} - TIMEOUT again: {e}")
                with store_lock:
                    counters["failed"] += 1
            else:
                print(f"[{_ts()}] {tag} {tk} - TIMEOUT: {e}. Added to retry queue.")
                requeue = entry
            fuse.record(True)
        except Exception as e:
            print(f"[{_ts()}] {tag} {tk} - EXCEPTION: {e}")
            fuse.record(True)
            with store_lock:
                counters["failed"] += 1
                # Apply the staleness circuit breaker here too. Keeping the
                # prior view unconditionally is what let a permanently-failing
                # ticker display a confident verdict indefinitely; the
                # fundamentals refresh already does this in its own handler.
                age = _view_age_days(old_view)
                if _is_valid_view(old_view) and age is not None and age > EXPERT_STALE_DAYS:
                    expert_views[tk] = stale_view_fallback(f"{age:.1f} days old; last error: {e}")
                save_expert_views(expert_views)
        with store_lock:
            counters["processed"] += 1
        # Paces each worker against Gemini's per-minute limits.
        time.sleep(RETRY_PAUSE_SECONDS if is_retry else TICKER_PAUSE_SECONDS)
        return requeue

    def _run_all(entries, is_retry=False):
        """`entries` through the pool, returning whatever asked to be requeued.

        A ThreadPoolExecutor is safe here for the reason refresh_fundamentals
        gives: every Gemini call inside a worker is bounded by
        llm_util.call_with_timeout, so the workers always finish and `with`
        joins them all.
        """
        if not entries:
            return []
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TICKERS) as pool:
            return [r for r in pool.map(lambda e: _analyse(e, is_retry), entries) if r]

    print(f"[{_ts()}] Analysing {len(plan)} ticker(s), {MAX_CONCURRENT_TICKERS} at a time")
    retry_queue = _run_all(plan)

    # Process retry queue -- not after a tripped fuse: the same dead keys
    # would fail every one of them too.
    if retry_queue and not fuse.tripped:
        print(f"\n[{_ts()}] === Processing Retry Queue ({len(retry_queue)} tickers) ===")
        _run_all(retry_queue, is_retry=True)

    print(f"\nDone. Processed {counters['processed']} analyses ({len(retry_queue)} of them retries).")
    print(f"No-Source Fallbacks: {counters['fallback']} times.")
    print(f"Final failures: {counters['failed']}.")
    # Per-key calls and failures, worst key first -- see usage_summary.
    llm_util.log_key_usage(client)
    fuse.exit_if_tripped()

if __name__ == "__main__":
    main()
