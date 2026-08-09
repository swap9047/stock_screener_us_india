#!/usr/bin/env python3
"""
Scheduled alert checker. Meant to be run daily (via Cowork's scheduler,
cron, or manually) — independent of whether the Streamlit app is open.

Fetches BOTH the US watchlist and the India watchlist, each benchmarked per
settings.json (default: S&P 500 / Nifty 500), combines them, evaluates alerts_config.json rules
with edge-triggered logic (alert_state.json), and sends any newly
triggered alerts to Discord.

Config files (same folder):
    watchlist.json        - {market_key: [tickers], ...} (see markets.json for the registered keys)
    alerts_config.json    - rules (add/edit via the Streamlit app's Alert Rules tab)
    discord_config.json   - {"webhook_url": "..."}
    alert_state.json      - auto-managed, tracks what's already fired

Run: python3 alert_check.py
"""

import sys

from stock_data import fetch_all_markets, load_settings, get_filterable_metrics
from alerts import load_rules, load_state, save_state, load_discord_webhook, evaluate_and_fire, send_discord, is_rule_due


def main():
    all_rules = load_rules()
    if not all_rules:
        print("No alert rules configured yet (alerts_config.json is empty). Nothing to check.")
        return

    # Each rule now carries its own day/time schedule (or "scan only", which
    # never sends to Discord but still counts as a rule for the watchlist
    # scan-filter feature) -- see alerts.is_rule_due. --force bypasses the
    # schedule entirely, useful for testing a rule manually via
    # `python alert_check.py --force` without waiting for its scheduled slot.
    force = "--force" in sys.argv
    if force:
        due_rules = [r for r in all_rules if r.get("enabled", True) and r.get("conditions") and r.get("schedule", {}).get("type") != "none"]
        print("Running in --force mode: checking all scheduled (non scan-only) rules regardless of current time/day.")
    else:
        due_rules = [r for r in all_rules if r.get("enabled", True) and r.get("conditions") and is_rule_due(r)]

    if not due_rules:
        print("No alert rules due for check at this time/day. Nothing to check.")
        return

    settings = load_settings()
    combined, as_of, per_market = fetch_all_markets(settings=settings)
    breakdown = " + ".join(f"{len(rows)} {mkt}" for mkt, rows in per_market.items())
    print(f"Checking {len(due_rules)} rule(s) (of {len(all_rules)} total) against {breakdown} tickers...")

    metric_labels = {v: k for k, v in get_filterable_metrics(settings).items()}
    state = load_state()
    # Pass the FULL ruleset so any rule-references inside due_rules can resolve
    # against rules that aren't due today; only due_rules actually fires.
    messages, new_state = evaluate_and_fire(all_rules, combined, state, due_rules=due_rules, metric_labels=metric_labels)

    # Every rule x ticker key that just flipped false->true this run (i.e. a
    # newly-triggered occurrence) -- derived by diffing new_state against the
    # state we loaded, which is exactly the same "was_active and not
    # prev.was_active" test evaluate_and_fire uses internally. We don't
    # persist these as "fired" until a Discord send has actually been
    # attempted -- otherwise a missing webhook or a failed send would mark
    # the occurrence as delivered when it never reached Discord, silently
    # losing it forever (edge-triggered logic never re-fires a state that's
    # already "active").
    newly_triggered_keys = [
        k for k, v in new_state.items()
        if v.get("was_active") and not state.get(k, {}).get("was_active")
    ]

    if not messages:
        save_state(new_state)
        print("No new alerts triggered.")
        return

    print(f"{len(messages)} new alert(s) triggered:")
    for m in messages:
        print(" -", m)

    webhook = load_discord_webhook()
    if not webhook:
        # Nothing was sent anywhere -- roll back the newly-triggered keys so
        # next run's edge-trigger check sees them as not-yet-fired and
        # retries, instead of saving them as delivered.
        for k in newly_triggered_keys:
            new_state[k] = {"was_active": False, "last_triggered_date": state.get(k, {}).get("last_triggered_date")}
        save_state(new_state)
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

    if not ok:
        # At least one message failed to reach Discord. We can't tell from
        # send_discord's aggregate bool which specific rule's message failed,
        # so conservatively roll back ALL newly-triggered keys -- worst case
        # a ticker that DID send successfully gets re-notified next run,
        # which is a minor duplicate rather than a silently dropped alert.
        for k in newly_triggered_keys:
            new_state[k] = {"was_active": False, "last_triggered_date": state.get(k, {}).get("last_triggered_date")}

    save_state(new_state)
    print("Sent to Discord." if ok else "Failed to send one or more messages to Discord -- will retry next run.")


if __name__ == "__main__":
    main()
