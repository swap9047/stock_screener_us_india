#!/usr/bin/env python3
"""
Scheduled news digest checker. Meant to run once daily (GitHub Actions,
8:00 PM ET, after market close) -- independent of the alert checker.

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
    news_data = build_news_summary(watchlists, api_key)
    to_save = news_data
    if partial:
        previous = load_news_summary() or {}
        to_save = {**news_data, "markets": {**(previous.get("markets") or {}), **(news_data.get("markets") or {})}}
    save_news_summary(to_save)
    totals = news_data.get("totals", {})
    print(f"Saved news_summary.json (as_of {news_data['as_of']}). Totals: {totals}")

    webhook = load_discord_webhook()
    if partial:
        print("Partial run -- summary was NOT sent to Discord.")
    elif webhook:
        messages = build_discord_messages(news_data)
        if messages:
            # stop_on_failure=False: one rejected part must not swallow the
            # other four markets' digests. The batch also paces its posts,
            # which the bare send_discord loop this replaces did not -- and a
            # burst against a webhook that throttles at ~5 per 2s was dropping
            # messages mid-digest once the 429 retries ran out.
            ok, detail = send_discord_batch(webhook, messages, stop_on_failure=False)
            print("Sent to Discord." if ok else f"Failed to send to Discord -- {detail}")
        else:
            print("No summary content to send.")
    else:
        print("No DISCORD_WEBHOOK_URL / discord_config.json set -- summary was NOT sent to Discord.")

    # A run where every ticker threw used to be byte-indistinguishable from a
    # genuinely quiet news day: build_news_summary still returned a well-formed
    # dict of "No major news..." summaries, the workflow committed it and went
    # green, and the only evidence was a line in the Actions log. Fail the job
    # instead when the search stage largely didn't work.
    searched = totals.get("searched", 0)
    failed = totals.get("failed", 0)
    if searched and failed >= max(1, searched // 2):
        print(f"ERROR: {failed}/{searched} tickers failed their news search -- treating this run as failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
