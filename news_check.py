#!/usr/bin/env python3
"""
Scheduled news digest checker. Meant to run once daily (GitHub Actions,
7:00 AM ET, before market open) -- independent of the alert checker.

Uses Gemini (Google Search grounding) to build a single collated summary
of important news/announcements/major stock moves in the last 24 hours for
each watchlist (US, India), then sends both to Discord and saves the result
to news_summary.json (which the Streamlit app's News tab reads).

Config/env (same folder):
    watchlist.json        - {"US": [...], "INDIA": [...]}
    GEMINI_API_KEY (env)  - Gemini API key (GitHub Actions repo secret)
    DISCORD_WEBHOOK_URL (env) - Discord webhook (same secret alert_check.py uses)
    news_summary.json     - auto-managed output, read by the app's News tab

Run: python3 news_check.py
"""

import sys

from stock_data import load_watchlists
from alerts import load_discord_webhook, send_discord
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
    if not watchlists.get("US") and not watchlists.get("INDIA"):
        print("Both watchlists are empty. Nothing to summarize.")
        return

    print(f"Building news summary for {len(watchlists.get('US', []))} US + "
          f"{len(watchlists.get('INDIA', []))} India tickers via Gemini grounded search...")
    news_data = build_news_summary(watchlists, api_key)
    save_news_summary(news_data)
    print(f"Saved news_summary.json (as_of {news_data['as_of']}).")

    webhook = load_discord_webhook()
    if not webhook:
        print("No DISCORD_WEBHOOK_URL / discord_config.json set -- summary was NOT sent to Discord.")
        return

    messages = build_discord_messages(news_data)
    if not messages:
        print("No summary content to send.")
        return

    ok = True
    for m in messages:
        ok = send_discord(webhook, m) and ok
    print("Sent to Discord." if ok else "Failed to send one or more messages to Discord.")


if __name__ == "__main__":
    main()
