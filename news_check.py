#!/usr/bin/env python3
"""
Scheduled news digest checker. Meant to run once daily (GitHub Actions,
8:00 PM ET, after market close) -- independent of the alert checker.

Uses Gemini (Google Search grounding) to build a single collated summary
of important news/announcements/major stock moves in the last 24 hours for
each watchlist in scope (controlled by news_watchlist_scope in settings.json;
empty = all markets), then sends to Discord and saves to news_summary.json
(which the Streamlit app's News tab reads).

Config/env (same folder):
    watchlist.json        - {market_key: [tickers], ...} (see markets.json for the registered keys)
    settings.json         - includes news_watchlist_scope (which watchlists to run)
    GEMINI_API_KEY (env)  - Gemini API key (GitHub Actions repo secret)
    DISCORD_WEBHOOK_URL (env) - Discord webhook (same secret alert_check.py uses)
    news_summary.json     - auto-managed output, read by the app's News tab

Run: python3 news_check.py
"""

import sys

from stock_data import load_watchlists
from alerts import load_discord_webhook, send_discord_batch
from news_summary import (
    get_gemini_api_key,
    build_news_summary,
    save_news_summary,
    build_discord_messages,
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

    breakdown = " + ".join(f"{len(tks)} {mkt}" for mkt, tks in watchlists.items())
    print(f"Building news summary for {breakdown} tickers via Gemini grounded search...")
    news_data = build_news_summary(watchlists, api_key)
    save_news_summary(news_data)
    totals = news_data.get("totals", {})
    print(f"Saved news_summary.json (as_of {news_data['as_of']}). Totals: {totals}")

    webhook = load_discord_webhook()
    if webhook:
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
