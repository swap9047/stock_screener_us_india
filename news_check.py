#!/usr/bin/env python3
"""
Scheduled news digest checker. Meant to run once daily (GitHub Actions,
6:00 AM ET, after the previous US session and NSE's close) -- independent of the
alert checker.

Uses Gemini (Google Search grounding) to build a single collated summary
of important news/announcements/major stock moves in the last 24 hours for
each watchlist in scope (controlled by news_watchlist_scope in settings.json;
empty = the all_invested group, see news_summary.resolve_news_scope), then sends to Discord and saves to news_summary.json
(which the Streamlit app's News tab reads).

Config/env (same folder):
    watchlist.json        - {market_key: [tickers], ...} (see markets.json for the registered keys)
    settings.json         - includes news_watchlist_scope (which watchlists to run)
    GEMINI_API_KEY (env)  - Gemini API key (GitHub Actions repo secret)
    DISCORD_WEBHOOK_URL (env) - Discord webhook (same secret alert_check.py uses)
    news_summary.json     - auto-managed output, read by the app's News tab

Run: python3 news_check.py
"""

import os
import sys

from llm_util import refresh_limit
from stock_data import load_watchlists
from alerts import load_discord_webhook, send_discord_batch
from news_summary import (
    get_gemini_api_key,
    build_news_summary,
    save_news_summary,
    build_discord_messages,
    build_followup_messages,
    failed_tickers,
    load_news_summary,
)


def main():
    api_key = get_gemini_api_key()
    if not api_key:
        print("No GEMINI_API_KEY set (env var or Streamlit secrets) -- cannot build news summary.")
        sys.exit(1)

    watchlists = load_watchlists()
    if not any(watchlists.values()):
        print("All watchlists are empty. Nothing to summarize.")
        return

    # A PARTIAL run (REFRESH_MARKETS and/or REFRESH_LIMIT, the workflow's
    # `markets`/`limit` inputs) is a smoke test: it covers only those watchlists
    # and their first N tickers, replaces only those watchlists' digests in
    # news_summary.json, and never posts to Discord -- a two-ticker digest is not
    # the day's news. Scheduled runs set neither.
    only_markets = {m.strip() for m in os.environ.get("REFRESH_MARKETS", "").split(",") if m.strip()}
    limit = refresh_limit()
    partial = bool(only_markets or limit)
    if partial:
        remaining = limit
        scoped = {}
        for mkt, tks in watchlists.items():
            if only_markets and mkt not in only_markets:
                continue
            if remaining is not None:
                tks = tks[:remaining]
                remaining -= len(tks)
            if tks:
                scoped[mkt] = tks
        watchlists = scoped
        if not watchlists:
            print("Partial run selected no tickers. Nothing to summarize.")
            return

    breakdown = " + ".join(f"{len(tks)} {mkt}" for mkt, tks in watchlists.items())
    print(f"Building news summary for {breakdown} tickers via Gemini grounded search"
          + (" (partial run)..." if partial else "..."))
    # A partial run never reuses a failed run's results: it is a smoke test of the
    # pipeline, so it searches what it was given.
    # This slot's earlier run, if any: what it posted and which stocks it could not search.
    previous = load_news_summary() or {}
    news_data = build_news_summary(watchlists, api_key, reuse_failed_run=not partial)
    totals = news_data.get("totals", {})
    # A run where every ticker threw used to be byte-indistinguishable from a
    # genuinely quiet news day: build_news_summary still returned a well-formed
    # dict of "No major news..." summaries, the workflow committed it and went
    # green, and the only evidence was a line in the Actions log. Fail the job
    # instead when the search stage largely didn't work.
    #
    # Decided BEFORE posting. This used to post first and fail after, and a
    # failed run makes the slot gate re-run the job -- so on 2026-09-30 Discord
    # got the 25-of-51-failed digest and then, from the retry, a second one. Now
    # a failed run posts nothing; the file is still saved (and committed) so the
    # News tab's "search failed" count shows what happened, and the retry posts
    # the day's only digest. Decided before SAVING too (2026-11-01): the file is
    # marked run_failed, and the retry searches only the stocks that failed
    # (news_summary.reusable_outcomes).
    #
    # Leftovers (owner, 2026-11-15): with fewer than half failed, the digest goes out, but the stocks that
    # could not be searched used to stay unsearched for the day -- 16 of 38 on 2026-11-10, 9 on 11-13, when
    # Google's search model returned 500 for most calls. Now the run is marked retry_pending and exits 1, so
    # the Worker's next hourly dispatch for the slot runs again; that run reuses every stock already searched
    # (reusable_outcomes), searches only the leftovers, and -- the digest being out (`posted`) -- posts a
    # short follow-up with any news they have instead of a second digest.
    searched = totals.get("searched", 0)
    failed = totals.get("failed", 0)
    run_failed = bool(searched and failed >= max(1, searched // 2))
    same_slot = previous.get("as_of") == news_data.get("as_of")
    already_posted = bool(same_slot and previous.get("posted"))
    retry_pending = bool(not partial and failed)
    # Digest parts an earlier run of this slot posted to no avail (below): resent by this run.
    unsent = list(previous.get("unsent_messages") or []) if already_posted else []
    to_save = {**news_data, "run_failed": run_failed, "retry_pending": retry_pending, "posted": already_posted,
               "unsent_messages": unsent}
    if partial:
        # A smoke test neither fails a slot nor clears a failed one: keep the flags --
        # for the SAME slot only. Carried onto a new slot's date they would make that
        # slot's first scheduled run reuse the older slot's records as its own.
        to_save = {**news_data, "run_failed": bool(same_slot and previous.get("run_failed")),
                   "retry_pending": bool(same_slot and previous.get("retry_pending")),
                   "posted": already_posted,
                   "unsent_messages": unsent,
                   "markets": {**(previous.get("markets") or {}), **(news_data.get("markets") or {})}}
    save_news_summary(to_save)
    print(f"Saved news_summary.json (as_of {news_data['as_of']}). Totals: {totals}")
    # The editor (Stage 3) failing used to show nowhere: on 4 of 18 digests in a
    # week the raw notes went out and the run stayed green. The bullets are
    # formatted in code now (news_summary.format_notes_fallback), but say so.
    for mkt, entry in (news_data.get("markets") or {}).items():
        if (entry or {}).get("collate_status", "ok") != "ok":
            print(f"::warning::News editor failed for one watchlist ({entry['collate_status']}); "
                  "its bullets were formatted automatically.")

    webhook = load_discord_webhook()
    if partial:
        print("Partial run -- summary was NOT sent to Discord.")
    elif run_failed:
        print("Search largely failed -- summary was NOT sent to Discord (the slot gate retries the run).")
    elif already_posted:
        # The digest went out on an earlier run of this slot: only the stocks it could not search are news now,
        # plus any digest part that run could not deliver (unsent_messages).
        late = failed_tickers(previous) - failed_tickers(news_data)
        messages = build_followup_messages(news_data, late)
        if not late:
            if not unsent:
                print("Digest already posted for this slot; no stock that failed then was searched now.")
        elif not messages:
            print(f"Digest already posted; {len(late)} stock(s) searched late, none with news -- nothing to post.")
        outgoing = unsent + messages
        if outgoing and webhook:
            undelivered = []
            ok, detail = send_discord_batch(webhook, outgoing, stop_on_failure=False, failed=undelivered)
            if unsent:
                print(f"Resent {len(unsent)} digest part(s) an earlier run could not deliver.")
            if messages:
                print(f"Posted a follow-up for {len(late)} stock(s) searched late." if ok
                      else f"Failed to send part of the follow-up to Discord -- {detail}")
            still = [outgoing[i] for i in undelivered]
            if still != unsent:
                to_save["unsent_messages"] = still
                save_news_summary(to_save)
        elif outgoing:
            print("No DISCORD_WEBHOOK_URL / discord_config.json set -- the follow-up was NOT sent to Discord.")
    elif webhook:
        messages = build_discord_messages(news_data)
        if messages:
            # stop_on_failure=False: one rejected part must not swallow the
            # other four markets' digests. The batch also paces its posts,
            # which the bare send_discord loop this replaces did not -- and a
            # burst against a webhook that throttles at ~5 per 2s was dropping
            # messages mid-digest once the 429 retries ran out.
            undelivered = []
            ok, detail = send_discord_batch(webhook, messages, stop_on_failure=False, failed=undelivered)
            print("Sent to Discord." if ok else f"Failed to send to Discord -- {detail}")
            # Posted means ANY digest part arrived, not every one (2026-11-22): `posted` used to be set only when
            # the whole batch, footer included, went through, so one failed part on a retry_pending run made the
            # next hourly run post the whole digest again. The parts that did not arrive are kept as
            # unsent_messages and resent, alone, by the slot's next run; nothing arriving at all leaves the
            # digest unposted, and that run posts all of it. Either way the run must be retried, so it is
            # marked retry_pending (which also makes the retry reuse every stock searched here) and exits 1.
            if len(undelivered) < len(messages):
                to_save["posted"] = True
                to_save["unsent_messages"] = [messages[i] for i in undelivered]
            if undelivered:
                retry_pending = True
                to_save["retry_pending"] = True
                print(f"::warning::{len(undelivered)}/{len(messages)} digest part(s) did not reach Discord -- "
                      "the slot's next hourly retry sends them.")
            save_news_summary(to_save)
        else:
            print("No summary content to send.")
    else:
        print("No DISCORD_WEBHOOK_URL / discord_config.json set -- summary was NOT sent to Discord.")

    if run_failed:
        print(f"ERROR: {failed}/{searched} tickers failed their news search -- treating this run as failed.")
        sys.exit(1)
    if to_save.get("unsent_messages") and not partial:
        # Digest parts still not delivered: red, so the next hourly dispatch resends them.
        to_save["retry_pending"] = retry_pending = True
        save_news_summary(to_save)
    if retry_pending:
        # Red on purpose: the slot gate counts only a successful run as the slot's work, so the Worker's next
        # hourly dispatch (8 of them after 6 AM ET) runs again and searches just these.
        if failed:
            print(f"::warning::{failed}/{searched} ticker(s) could not be searched -- the slot's next hourly retry "
                  "searches only those and posts any news as a follow-up.")
        sys.exit(1)

if __name__ == "__main__":
    main()
