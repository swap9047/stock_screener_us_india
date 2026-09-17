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

import os
import sys

from stock_data import (fetch_all_markets, load_settings, get_filterable_metrics,
                        load_data_snapshot, missing_row_tickers,
                        reject_stale_rows, load_watchlists)
from alerts import (load_rules, load_state_status, save_state, load_discord_webhook, evaluate_and_fire,
                    send_discord_batch, is_rule_due, notify_mode, STATE_FILE)


# Above this share of the universe missing, the run is treated as a throttled
# fetch rather than a few dud tickers -- see the guard in main().
MAX_MISSING_FRACTION = 0.10


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
    # Completed sessions only: GitHub starts this "9:15 PM ET" job hours late,
    # inside NSE's 23:45-06:00 ET session, and alerts must judge closes rather
    # than a forming intraday bar -- see stock_data.drop_forming_daily_bars.
    skipped_groups = {}
    # short_history collects tickers Yahoo has too few bars for to compute at all
    # (MIN_DAILY_BARS). Reading it only from the stored snapshot missed one that
    # drops below the floor in THIS fetch -- dropping a forming bar can do it --
    # and the coverage check below then counted it as a gap.
    fresh_short_history = {}
    combined, as_of, per_market = fetch_all_markets(settings=settings, completed_sessions_only=True,
                                                    skipped_groups=skipped_groups,
                                                    short_history=fresh_short_history)
    breakdown = " + ".join(f"{len(rows)} {mkt}" for mkt, rows in per_market.items())
    print(f"Checking {len(due_rules)} rule(s) (of {len(all_rules)} total) against {breakdown} tickers...")

    # A job that JUDGES this data needs to know whether it is looking at the
    # whole universe. fetch_all_markets drops whatever Yahoo would not hand over
    # -- a whole benchmark group whose fetch raised, or a ticker that came back
    # empty -- and refresh_data.py absorbs that with reject_stale_rows /
    # fill_snapshot_gaps. Here there is no backstop, so:
    #
    #   - a skipped GROUP, or more than MAX_MISSING_FRACTION of the universe
    #     missing, is systemic (a throttle): fail, and let the hourly slot gate
    #     (.github/actions/slot-gate) retry the same slot on a later wake-up;
    #   - a handful of individual misses only warns. Failing on those would let
    #     one delisted ticker block sending alerts indefinitely, every slot, forever.
    #
    # A row Yahoo served OLDER than the one already stored is stale data, not
    # news, so keep the stored one (same call refresh_data.py makes).
    previous = (load_data_snapshot() or {}).get("per_market") or {}
    # completed_sessions_only must match the fetch above: the stored snapshot
    # keeps forming bars on purpose, so without it this call would hand the
    # forming India bar straight back and undo the whole point of the flag.
    per_market, stale = reject_stale_rows(per_market, previous, completed_sessions_only=True)
    if stale:
        print(f"WARNING: {sum(len(v) for v in stale.values())} ticker(s) came back older than "
              "the stored row; kept the newer stored one.")
    combined = [r for rows in per_market.values() for r in rows]

    short_history = {**((load_data_snapshot() or {}).get("short_history") or {}), **fresh_short_history}
    # Pass the watchlists explicitly: the job has already loaded them for the
    # fetch, and a helper that re-reads them from disk hides that dependency.
    watchlists = load_watchlists()
    gaps = missing_row_tickers(per_market, watchlists, short_history=short_history)
    n_gaps = sum(len(v) for v in gaps.values())
    universe = sum(len(v) for v in watchlists.values()) or 1
    if skipped_groups or n_gaps > MAX_MISSING_FRACTION * universe:
        print(f"::error::Incomplete data: {len(skipped_groups)} benchmark group(s) skipped, "
              f"{n_gaps} of {universe} ticker(s) without a row. Not sending alerts on a partial "
              "universe; the slot gate will retry this slot on a later run.")
        for bench, tickers in sorted(skipped_groups.items()):
            print(f"  skipped group {bench}: {len(tickers)} ticker(s)")
        for mkt, tickers in sorted(gaps.items()):
            print(f"  {mkt}: {len(tickers)} ticker(s) missing")
        sys.exit(1)
    if gaps:
        print(f"WARNING: {n_gaps} ticker(s) have no row this run and cannot match anything: "
              + ", ".join(f"{mkt} ({len(t)})" for mkt, t in sorted(gaps.items())))


    metric_labels = {v: k for k, v in get_filterable_metrics(settings).items()}
    # No TRUSTWORTHY state means we have no record of what already fired -- a
    # fresh install, state lost, or a torn/garbled file. Evaluating against {}
    # would treat every currently-true rule x ticker as newly triggered and
    # flood Discord with them in one run (what an actions/cache eviction used to
    # do). Record the current truth instead and send nothing; from the next run
    # on, only real false->true transitions fire.
    #
    # This asks load_state_status WHY the state is empty rather than testing
    # os.path.exists: a torn file exists, so the old test let exactly the case
    # this branch defends against straight through.
    state, state_trusted = load_state_status()
    seeding = not state_trusted
    # Pass the FULL ruleset so any rule-references inside due_rules can resolve
    # against rules that aren't due today; only due_rules actually fires.
    messages, new_state = evaluate_and_fire(all_rules, combined, state, due_rules=due_rules, metric_labels=metric_labels)
    if seeding:
        save_state(new_state)
        active = sum(1 for v in new_state.values() if v.get("was_active"))
        why = "not found" if not os.path.exists(STATE_FILE) else "unusable (see the warning above)"
        print(f"{os.path.basename(STATE_FILE)} {why} -- seeded it with {active} currently-active "
              f"rule/ticker pair(s) and sent NOTHING ({len(messages)} message(s) suppressed). "
              "Alerts fire on new transitions from the next run.")
        return

    # Every rule x ticker key that just flipped false->true this run (i.e. a
    # newly-triggered occurrence) -- derived by diffing new_state against the
    # state we loaded, which is exactly the same "was_active and not
    # prev.was_active" test evaluate_and_fire uses internally. We don't
    # persist these as "fired" until a Discord send has actually been
    # attempted -- otherwise a missing webhook or a failed send would mark
    # the occurrence as delivered when it never reached Discord, silently
    # losing it forever (edge-triggered logic never re-fires a state that's
    # already "active").
    #
    # Full-mode rules are excluded: they re-send every currently-matching ticker
    # on the next due run regardless of state, so rolling their keys back would
    # buy no retry -- it would only leave was_active=False on a ticker that IS
    # active, which would then re-announce if the rule were switched back to
    # incremental. Rule ids are hex and carry no colon, so the key splits cleanly.
    full_rule_ids = {r["id"] for r in all_rules if notify_mode(r) == "full"}
    newly_triggered_keys = [
        k for k, v in new_state.items()
        if v.get("was_active") and not state.get(k, {}).get("was_active")
        and k.split(":", 1)[0] not in full_rule_ids
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
    # Paced, and every message attempted: a cold-start run is ~17 messages
    # back-to-back against a webhook that throttles at roughly 5 per 2s, and
    # the bare unpaced loop this replaces relied entirely on _post_discord's 3
    # rate-limit retries -- exhausting them dropped a message mid-digest.
    ok, detail = send_discord_batch(
        webhook,
        [f"**Stock Alert Check — {as_of}**"] + list(messages),
        stop_on_failure=False,
    )
    if not ok:
        print(f"Discord send failed: {detail}")
        # At least one message failed to reach Discord. We can't tell from
        # the batch's aggregate bool which specific rule's message failed,
        # so conservatively roll back ALL newly-triggered keys -- worst case
        # a ticker that DID send successfully gets re-notified next run,
        # which is a minor duplicate rather than a silently dropped alert.
        for k in newly_triggered_keys:
            new_state[k] = {"was_active": False, "last_triggered_date": state.get(k, {}).get("last_triggered_date")}

    save_state(new_state)
    print("Sent to Discord." if ok else "Failed to send one or more messages to Discord -- will retry next run.")


if __name__ == "__main__":
    main()
