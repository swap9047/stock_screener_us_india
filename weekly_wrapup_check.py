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
from datetime import date, datetime
from zoneinfo import ZoneInfo

from json_store import DataFileError
from alerts import load_discord_webhook, load_rules, send_discord_batch
from stock_data import (fetch_all_markets, get_filterable_metrics, load_markets_registry,
                        load_settings, load_data_snapshot, missing_row_tickers,
                        reject_stale_rows, load_watchlists, enrich_rows)
from weekly_wrapup import (
    advance_state,
    build_discord_messages,
    build_wrapup,
    load_wrapup_state,
    save_wrapup_state,
    eligible_rules,
)


# Above this share of the universe missing, the run is treated as a throttled
# fetch rather than a few dud tickers -- see the guard in main().
MAX_MISSING_FRACTION = 0.10


def main():
    dry_run = "--dry-run" in sys.argv

    all_rules = load_rules()
    chosen = eligible_rules(all_rules)
    if not chosen:
        print("No enabled alert rules with conditions found. Nothing to do.")
        return

    settings = load_settings()
    # Completed sessions only: the "Sunday 9 PM ET" run actually lands ~2 AM
    # Monday ET, mid India Monday session, and the digest reports week-end
    # status -- see stock_data.drop_forming_daily_bars.
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
    print(f"Building weekly wrap-up over {len(chosen)} alert(s) against {breakdown} tickers...")

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
    #     one delisted ticker block building the digest indefinitely, every slot, forever.
    #
    # A row Yahoo served OLDER than the one already stored is stale data, not
    # news, so keep the stored one (same call refresh_data.py makes).
    previous = (load_data_snapshot() or {}).get("per_market") or {}
    # completed_sessions_only must match the fetch above -- see reject_stale_rows.
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
              f"{n_gaps} of {universe} ticker(s) without a row. Not building the digest on a partial "
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
    state = load_wrapup_state()
    # ET, not the runner's local date -- same reasoning as
    # alerts.evaluate_and_fire. The "Sunday 9 PM ET" slot lands at ~01:00-02:00
    # UTC Monday, so date.today() recorded Monday. Tenure arithmetic stayed
    # correct (both ends of the subtraction skewed together), but the stored
    # `entered` dates were a day ahead of the slot they belong to.
    run_date = datetime.now(ZoneInfo("America/New_York")).date()

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
