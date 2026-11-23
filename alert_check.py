#!/usr/bin/env python3
"""
Scheduled alert checker, run by daily-alerts.yml once per 9:00 PM ET slot (or
by hand) — independent of whether the Streamlit app is open.

Fetches every watchlist, each ticker benchmarked against its own index (see
ticker_index.json), combines them, evaluates alerts_config.json rules
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
                        reject_stale_rows, load_watchlists, enrich_rows)
from json_store import DataFileError
from alerts import (load_rules, load_state_status, save_state, load_discord_webhook, evaluate_and_fire,
                    send_discord_batch, is_rule_due, notify_mode, STATE_FILE,
                    dead_rule_legs, prune_state)


# Above this share of the universe missing, the run is treated as a throttled
# fetch rather than a few dud tickers -- see the guard in main().
MAX_MISSING_FRACTION = 0.10


def post_late_stage_notice():
    """Post the late-Stage-Analysis notice to Discord when the upload is more than
    stage_analysis.STAGE_STALE_DAYS old (owner, 2026-10-31): the same text as the
    app's red banner. daily-alerts.yml's gate counts a late upload as due, so this
    runs once per 9 PM ET slot even when no rule is due. Never raises: a broken
    notice must not cost the night's alerts."""
    try:
        from stage_analysis import load_stage_analysis, upload_status, late_upload_notice
        notice = late_upload_notice(upload_status(
            load_stage_analysis(), [t for tickers in load_watchlists().values() for t in tickers]))
        if not notice:
            return False
        webhook = load_discord_webhook()
        if not webhook:
            print("Stage Analysis upload is late; no Discord webhook set, so it was not posted.")
            return False
        ok, detail = send_discord_batch(webhook, [f"🔴 **{notice}**"])
        print("Stage Analysis upload is late: posted to Discord." if ok
              else f"::warning::Stage Analysis upload is late; the Discord post failed: {detail}")
        return ok
    except Exception as e:  # noqa: BLE001 -- see the docstring
        print(f"::warning::Could not check the Stage Analysis upload's age: {e}")
        return False


def main():
    """The rule check, then the late-Stage notice -- once per slot.

    The notice used to go out first, before anything could fail. But a run that
    then fails its data check exits 1 and the Worker dispatches the slot again
    every hour (up to 8 times): each retry posted the notice again. It now goes
    out when the rule check has ENDED WITHOUT FAILING, whichever way it ended
    (no rules, none due, nothing new, alerts sent), so the retry that gets
    through posts it and the failed attempts post nothing. A night on which
    every attempt fails posts no notice; the app's red banner still shows."""
    try:
        check_rules()
    except SystemExit as e:
        if e.code not in (0, None):
            raise
        post_late_stage_notice()
        raise
    post_late_stage_notice()


def check_rules():
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
    # A substituted row carries flags/notes/AI fields from ITS fetch, not from
    # the files as they are now -- see stock_data.enrich_rows.
    enrich_rows(combined, settings)

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
    # A rule leg that can never match -- a retired metric key, a metric no row
    # has a value for, a reference to a disabled rule -- used to look exactly
    # like a rule that simply had nothing to report. Log only; see
    # alerts.dead_rule_legs.
    for warning in dead_rule_legs(all_rules, combined):
        print(f"WARNING: {warning}")


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
    # message_rules: the rule each message belongs to, for the partial-failure rollback below.
    message_rules = []
    messages, new_state = evaluate_and_fire(all_rules, combined, state, due_rules=due_rules,
                                            metric_labels=metric_labels, message_rules=message_rules)
    # Drop keys for deleted rules and for tickers in no watchlist (not merely
    # missing from tonight's fetch) -- see alerts.prune_state. Done before the
    # diff below, which only ever looks at keys of live rules and members.
    new_state = prune_state(new_state, all_rules, watchlists)
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
    failed = []
    ok, detail = send_discord_batch(
        webhook,
        [f"**Stock Alert Check — {as_of}**"] + list(messages),
        stop_on_failure=False,
        failed=failed,
    )
    if not ok:
        print(f"Discord send failed: {detail}")
        # Roll back only the rules whose own message failed, so the next run
        # announces those again and the ones that DID arrive are not repeated
        # (every newly-triggered key used to be rolled back, re-sending them
        # all). Index 0 is the header line, which belongs to no rule. When the
        # rule of each message is not known, fall back to rolling back them all:
        # a duplicate is better than a silently dropped alert.
        if len(message_rules) == len(messages):
            failed_rules = {message_rules[i - 1] for i in failed if 1 <= i <= len(message_rules)}
        else:
            failed_rules = None
        for k in newly_triggered_keys:
            if failed_rules is not None and k.split(":", 1)[0] not in failed_rules:
                continue
            new_state[k] = {"was_active": False, "last_triggered_date": state.get(k, {}).get("last_triggered_date")}

    save_state(new_state)
    print("Sent to Discord." if ok else "Failed to send one or more messages to Discord -- "
          "the rules whose message failed are announced again next run.")
    if not ok and len(failed) >= len(messages) + 1:
        # EVERY message failed, header included (2026-11-22): the run used to end
        # green, so the slot gate counted the night done and the night's alerts
        # (rolled back above) waited a whole day for the rule's next due run.
        # Red instead: the Worker's next hourly dispatch of the 9 PM slot checks
        # again and sends them. Nothing arrived, so the retry repeats nothing --
        # a PARTLY delivered run stays green, because a retry would repost the
        # "full"-mode rules whose tables did arrive.
        print("::error::No alert message reached Discord -- failing the run so the slot's next "
              "hourly retry sends them.")
        sys.exit(1)


if __name__ == "__main__":
    # Any unreadable data file -- alerts_config.json via load_rules, or either AI
    # store reached through fetch_all_markets' enrichment -- exits 1 with the
    # filename instead of a raw traceback, and the slot gate retries the slot.
    #
    # Failing rather than carrying on is deliberate: this job JUDGES the data, and
    # a rule on Sentiment or Expert Take would silently never match if those
    # fields were quietly missing -- the same reasoning as MAX_MISSING_FRACTION
    # above. alerts_config.json already behaved this way (minus the clean
    # message); the AI stores now match it.
    try:
        main()
    except DataFileError as e:
        print(f"::error::{e}")
        sys.exit(1)
