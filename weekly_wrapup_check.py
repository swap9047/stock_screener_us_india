#!/usr/bin/env python3
"""
Scheduled weekly wrap-up. Meant to run once a week (Sunday evening ET, via
.github/workflows/weekly-wrapup.yml) -- independent of whether the Streamlit
app is open.

Fetches every watchlist, evaluates the alert rules flagged for the wrap-up
(Alert Rules tab -> Weekly wrap-up), sends the digest to Discord, and then
advances the tenure counters in weekly_wrapup_state.json.

This is the ONLY writer of weekly_wrapup_state.json. The app's on-demand
report is strictly read-only, so you can run it as often as you like without
disturbing the Wk column.

Run: python3 weekly_wrapup_check.py [--dry-run]
    --dry-run  print the digest, send nothing, write nothing.
"""

import sys
from datetime import date

from alerts import load_discord_webhook, load_rules, send_discord_batch
from stock_data import fetch_all_markets, get_filterable_metrics, load_markets_registry, load_settings
from weekly_wrapup import (
    advance_state,
    build_discord_messages,
    build_wrapup,
    load_wrapup_state,
    save_wrapup_state,
    selected_rules,
)


def main():
    dry_run = "--dry-run" in sys.argv

    all_rules = load_rules()
    chosen = selected_rules(all_rules)
    if not chosen:
        print("No alert rules are flagged for the weekly wrap-up "
              "(Alert Rules tab -> Weekly wrap-up). Nothing to do.")
        return

    settings = load_settings()
    combined, as_of, per_market = fetch_all_markets(settings=settings)
    breakdown = " + ".join(f"{len(rows)} {mkt}" for mkt, rows in per_market.items())
    print(f"Building weekly wrap-up over {len(chosen)} alert(s) against {breakdown} tickers...")

    metric_labels = {v: k for k, v in get_filterable_metrics(settings).items()}
    state = load_wrapup_state()
    run_date = date.today()

    # Pass the FULL ruleset so rule->rule references resolve even when the
    # referenced rule isn't itself in the wrap-up.
    wrapup = build_wrapup(
        all_rules, combined, state,
        metric_labels=metric_labels,
        registry=load_markets_registry(),
        run_date=run_date,
        as_of=as_of,
    )

    if wrapup["cycle_ids"]:
        print("WARNING: circular alert references among rule(s): "
              f"{sorted(wrapup['cycle_ids'])} -- treated as not matching.")

    messages = build_discord_messages(wrapup)
    for m in messages:
        print("\n" + m)

    if dry_run:
        print(f"\n--dry-run: {len(messages)} message(s) NOT sent; "
              "weekly_wrapup_state.json NOT modified.")
        return

    webhook = load_discord_webhook()
    if not webhook:
        # Nothing was delivered, so nothing has "been in the list for a week"
        # from the reader's point of view -- leave the counters alone.
        print("\nNo DISCORD_WEBHOOK_URL / discord_config.json set — wrap-up was NOT sent "
              "anywhere, and tenure counters were left unchanged.")
        return

    ok, detail = send_discord_batch(webhook, messages)
    if not ok:
        # Advancing on a run that didn't fully land would silently rebase
        # every Wk value with no way to recover the old entered dates, so a
        # partial send is treated as no send. Worst case next week's numbers
        # are one run stale, which is visible and self-correcting.
        print(f"Failed to send one or more messages to Discord ({detail}) — tenure "
              "counters left unchanged; will retry next run.")
        return

    save_wrapup_state(advance_state(state, wrapup, run_date=run_date))
    print(f"Sent {len(messages)} message(s) to Discord and advanced tenure state "
          f"({len(wrapup['rollup'])} stock(s) tracked).")


if __name__ == "__main__":
    main()
