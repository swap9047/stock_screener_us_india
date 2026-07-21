#!/usr/bin/env python3
"""
Scheduled alert checker. Meant to be run daily (via Cowork's scheduler,
cron, or manually) — independent of whether the Streamlit app is open.

Fetches BOTH the US watchlist and the India watchlist, each benchmarked per
settings.json (default: S&P 500 / Nifty 500), combines them, evaluates alerts_config.json rules
with edge-triggered logic (alert_state.json), and sends any newly
triggered alerts to Discord.

Config files (same folder):
    watchlist.json        - {"US": [...], "INDIA": [...]}
    alerts_config.json    - rules (add/edit via the Streamlit app's Alert Rules tab)
    discord_config.json   - {"webhook_url": "..."}
    alert_state.json      - auto-managed, tracks what's already fired

Run: python3 alert_check.py
"""

from stock_data import fetch_all_markets, load_settings, get_filterable_metrics
from alerts import load_rules, load_state, save_state, load_discord_webhook, evaluate_and_fire, send_discord


def main():
    rules = load_rules()
    if not rules:
        print("No alert rules configured yet (alerts_config.json is empty). Nothing to check.")
        return

    settings = load_settings()
    combined, as_of, per_market = fetch_all_markets(settings=settings)
    print(f"Checking {len(rules)} rule(s) against {len(per_market['US'])} US + {len(per_market['INDIA'])} India tickers...")

    metric_labels = {v: k for k, v in get_filterable_metrics(settings).items()}
    state = load_state()
    messages, new_state = evaluate_and_fire(rules, combined, state, metric_labels=metric_labels)
    save_state(new_state)

    if not messages:
        print("No new alerts triggered.")
        return

    print(f"{len(messages)} new alert(s) triggered:")
    for m in messages:
        print(" -", m)

    webhook = load_discord_webhook()
    if not webhook:
        print("\nNo discord_config.json / webhook_url set — alerts were NOT sent anywhere.")
        return

    # `messages` is now one Discord-ready table PER RULE (each already sized
    # to fit under Discord's message limit on its own -- see
    # alerts.build_discord_messages_for_rule) -- send the header, then each
    # table, as SEPARATE messages rather than joining them into one, since a
    # joined blob could exceed the limit when several rules fire the same day.
    ok = send_discord(webhook, f"**Stock Alert Check — {as_of}**")
    for m in messages:
        ok = send_discord(webhook, m) and ok
    print("Sent to Discord." if ok else "Failed to send one or more messages to Discord.")


if __name__ == "__main__":
    main()
