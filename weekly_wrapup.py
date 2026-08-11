"""
Weekly wrap-up: a Sunday digest over a user-chosen subset of alert rules.

Two parts, in this order:

  1. One table per selected alert -- ticker, the columns that alert's own
     conditions reference, and Wk (weeks since that ticker entered the
     alert's list; 0 = entered this week).
  2. A cross-alert roll-up -- ticker, how many of the selected alerts it
     appears in, and which alert numbers. Sorted most-active first.

This is a STATUS report, not a history report: it evaluates the selected
rules against the current snapshot (on Sunday evening that's Friday's close)
rather than replaying what fired during the week. The only historical
element is Wk, and that comes from weekly_wrapup_state.json.

Why a dedicated state file rather than reusing alert_state.json: that file
is gitignored, lives only in the GitHub Actions cache, is never written for
"Scan only" rules, and is only updated for rules that were due that day --
so its last_triggered_date would be blank or wrong for a large share of
rows here.

Pure logic only -- no Streamlit, no network -- so app.py (on-demand,
read-only) and weekly_wrapup_check.py (scheduled, authoritative) share one
implementation. This mirrors the alerts.py / alert_check.py split.

STATE OWNERSHIP: build_wrapup never mutates state. Only advance_state
writes, and only weekly_wrapup_check.py calls it. Running the report from
the app any number of times, on any day, cannot move a counter.
"""

import json
import os
from datetime import date, datetime

from alerts import (
    _applicable_tickers,
    _format_cell,
    _metrics_used_in_conditions,
    _scope_label,
    chunked_table_messages,
    compute_rule_truth,
)
from filters import describe_chain

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WRAPUP_STATE_FILE = os.path.join(SCRIPT_DIR, "weekly_wrapup_state.json")


# --- State ---------------------------------------------------------------
# {"last_run": "YYYY-MM-DD",
#  "entries": {"<rule_id>:<TICKER>": {"entered": "YYYY-MM-DD"}}}
#
# advance_state PRUNES any key that didn't match in the current run, so
# "key present in state" means exactly "was present at the previous
# authoritative run". That makes the continuity test trivial -- key present
# keeps its entered date, key absent starts a fresh one -- and it's what
# makes a ticker that falls off and later returns correctly read 0 again
# rather than resuming its old tenure.


def load_wrapup_state():
    if not os.path.exists(WRAPUP_STATE_FILE):
        return {"last_run": None, "entries": {}}
    try:
        with open(WRAPUP_STATE_FILE) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        # A corrupted state file must not take the whole digest down -- the
        # report is still fully correct without it, every ticker just reads
        # as newly entered.
        return {"last_run": None, "entries": {}}
    if not isinstance(raw, dict):
        return {"last_run": None, "entries": {}}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    return {"last_run": raw.get("last_run"), "entries": entries}


def save_wrapup_state(state):
    with open(WRAPUP_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _weeks_since(entered, run_date):
    """Whole weeks between two dates, floored. Integer-days // 7 rather than
    ISO-week arithmetic so a GitHub Actions scheduler delay of a few hours
    (or a run that lands after midnight) can never jump the counter."""
    entered_d = _parse_date(entered)
    if entered_d is None:
        return 0
    return max(0, (run_date - entered_d).days // 7)


# --- Rule selection ------------------------------------------------------


def is_eligible(rule):
    """Selectable for the wrap-up: enabled and actually has conditions.
    Deliberately includes "Scan only" rules (schedule.type == "none") -- the
    wrap-up is exactly where a rule too noisy for a daily Discord ping still
    earns its keep."""
    return bool(rule.get("enabled", True)) and bool(rule.get("conditions"))


def eligible_rules(rules):
    return [r for r in rules if is_eligible(r)]


def selected_rules(rules):
    """Rules that go into the wrap-up, in alerts_config.json order -- which
    is what fixes the 1..N numbering the roll-up's Alerts# refers to."""
    return [r for r in eligible_rules(rules) if r.get("weekly_wrapup")]


# --- Columns -------------------------------------------------------------


def wrapup_columns(rule, rules_by_id):
    """The columns to show for one alert, as (kind, keys).

    kind == "metric": the metrics the rule's conditions compare (the normal
    case). kind == "rule": the alerts it references, one Yes/No column each.

    The second case exists because _metrics_used_in_conditions returns []
    for a rule built purely out of other rules (rule-type conditions carry
    no metric_a), which would render as a bare ticker list with nothing to
    read. For such a rule the referenced alerts ARE its "columns used"."""
    metrics = _metrics_used_in_conditions(rule.get("conditions", []))
    if metrics:
        return "metric", metrics

    refs, seen = [], set()
    for cond in rule.get("conditions", []):
        rid = cond.get("rule_id")
        if cond.get("type") == "rule" and rid and rid not in seen:
            seen.add(rid)
            refs.append(rid)
    return "rule", refs


def _column_labels(kind, keys, metric_labels, rules_by_id):
    if kind == "metric":
        return [metric_labels.get(k, k) for k in keys]
    return [(rules_by_id.get(k, {}).get("name") or k) for k in keys]


def _column_values(kind, keys, ticker, row, rule_truth):
    if kind == "metric":
        return [_format_cell(k, row.get(k)) for k in keys]
    return ["Yes" if rule_truth.get(k, {}).get(ticker) else "No" for k in keys]


# --- Build ---------------------------------------------------------------


def build_wrapup(rules, snapshot_results, state, metric_labels=None,
                 registry=None, run_date=None, as_of=None):
    """The whole report as a plain dict, so app.py can render it as tables
    and weekly_wrapup_check.py can render it as Discord text from the exact
    same numbers.

    PURE: `state` is read, never written. Callers that want the tenure
    counters to advance must call advance_state explicitly.

    `rules` must be the COMPLETE ruleset (not just the selected ones) so
    rule->rule references resolve even when the referenced rule isn't itself
    in the wrap-up -- same contract as alerts.evaluate_and_fire.
    """
    metric_labels = metric_labels or {}
    registry = registry or {}
    run_date = run_date or date.today()
    entries = state.get("entries", {})

    chosen = selected_rules(rules)
    rules_by_id = {r["id"]: r for r in rules}
    rule_truth, cycle_ids = compute_rule_truth(rules, snapshot_results)
    rows_by_ticker = {r["ticker"]: r for r in snapshot_results}

    alerts_out = []
    # ticker -> {"nums": [...], "weeks": [...], "market": ...}
    per_ticker = {}

    for num, rule in enumerate(chosen, start=1):
        kind, keys = wrapup_columns(rule, rules_by_id)
        labels = _column_labels(kind, keys, metric_labels, rules_by_id)
        headers = ["Ticker", "Watchlist"] + labels + ["Wk"]

        rows, tickers, weeks = [], [], {}
        for ticker in _applicable_tickers(rule, snapshot_results):
            if not rule_truth.get(rule["id"], {}).get(ticker):
                continue
            row = rows_by_ticker.get(ticker)
            if row is None:
                continue

            entry = entries.get(f"{rule['id']}:{ticker}")
            wk = _weeks_since(entry.get("entered"), run_date) if entry else 0

            market_key = row.get("market", "")
            watchlist = registry.get(market_key, {}).get("label", market_key)
            rows.append([ticker, watchlist]
                        + _column_values(kind, keys, ticker, row, rule_truth)
                        + [str(wk)])
            tickers.append(ticker)
            weeks[ticker] = wk

            agg = per_ticker.setdefault(ticker, {"nums": [], "weeks": [], "market": watchlist})
            agg["nums"].append(num)
            agg["weeks"].append(wk)

        alerts_out.append({
            "num": num,
            "rule_id": rule["id"],
            "name": rule.get("name") or "(unnamed)",
            "scope_label": _scope_label(rule.get("scope")),
            "scan_only": rule.get("schedule", {}).get("type") == "none",
            "column_kind": kind,
            "headers": headers,
            "rows": rows,
            "tickers": tickers,
            "weeks": weeks,
            # Human-readable condition summary, e.g. "10 WEMA > 40 WEMA AND RSI > 45".
            # metric_labels is passed in reverse (key=internal, value=display) so we
            # build the forward map on the fly: {internal_key: display_label}.
            "description": describe_chain(
                rule.get("conditions", []),
                {v: k for k, v in (metric_labels or {}).items()},
                rules_by_id,
            ),
        })

    # Most active first; ties broken by longest-standing, so a name that has
    # been sitting in its alerts for months outranks one that arrived today.
    rollup = sorted(
        (
            {
                "ticker": t,
                "market": agg["market"],
                "count": len(agg["nums"]),
                "alert_nums": agg["nums"],
                "oldest_weeks": max(agg["weeks"]),
            }
            for t, agg in per_ticker.items()
        ),
        key=lambda d: (-d["count"], -d["oldest_weeks"], d["ticker"]),
    )

    return {
        "run_date": run_date.isoformat(),
        "as_of": as_of,
        "state_last_run": state.get("last_run"),
        "alerts": alerts_out,
        "rollup": rollup,
        "cycle_ids": cycle_ids,
        "total_stocks": len(per_ticker),
    }


def advance_state(state, wrapup, run_date=None):
    """The ONLY writer of tenure. Returns a new state dict -- keeps the
    entered date for every ticker already present, stamps run_date on every
    newly present one, and drops everything that didn't match this run.

    Call this exactly once per authoritative (scheduled) run, and only after
    the digest has actually been delivered -- advancing on a run nobody
    received would silently rebase every counter with no way to recover the
    old dates."""
    run_date = run_date or date.today()
    prev = state.get("entries", {})
    entries = {}
    for alert in wrapup["alerts"]:
        for ticker in alert["tickers"]:
            key = f"{alert['rule_id']}:{ticker}"
            existing = prev.get(key)
            entries[key] = {
                "entered": existing.get("entered") if existing else run_date.isoformat()
            }
    return {"last_run": run_date.isoformat(), "entries": entries}


# --- Discord rendering ---------------------------------------------------


def _pretty_date(iso):
    d = _parse_date(iso)
    return d.strftime("%a %d %b %Y") if d else (iso or "—")


def build_discord_messages(wrapup, limit=1900):
    """Header + legend, then one table per alert, then the roll-up -- each a
    separate Discord message so a big week can't blow the size limit by
    being joined into one blob (same reasoning as alert_check.py)."""
    if not wrapup["alerts"]:
        return []

    header = [f"**📅 Weekly wrap-up — {_pretty_date(wrapup['run_date'])}**"]
    matched = [a for a in wrapup["alerts"] if a["rows"]]
    header.append(
        f"{len(wrapup['alerts'])} alert(s) · {wrapup['total_stocks']} stock(s) · "
        f"Wk 0 = entered this week"
    )
    header.append("")
    for a in wrapup["alerts"]:
        suffix = "" if a["rows"] else " — no matches"
        header.append(f"**{a['num']}.** {a['name']} — {a['scope_label']} ({len(a['rows'])}){suffix}")
    if wrapup["rollup"]:
        header.append("")
        header.append(f"🔥 Most active stocks summary follows at the end ↓")

    messages = ["\n".join(header)]

    for a in matched:
        # Title line: alert name + scope
        title = f"**{a['num']}. {a['name']}** — {a['scope_label']}"
        # Append the human-readable condition description so Discord readers
        # know what each alert is actually checking for (e.g. "10 WEMA > 40 WEMA").
        desc = a.get("description", "").strip()
        if desc:
            title += f"\n_{desc}_"
        messages.extend(chunked_table_messages(title, a["headers"], a["rows"], limit=limit))

    if wrapup["rollup"]:
        rows = [
            [r["ticker"], r["market"], str(r["count"]),
             ",".join(str(n) for n in r["alert_nums"]), str(r["oldest_weeks"])]
            for r in wrapup["rollup"]
        ]
        messages.extend(chunked_table_messages(
            "**🔥 Most active stocks** — across the alerts above",
            ["Ticker", "Watchlist", "# Alerts", "Alerts#", "Oldest Wk"],
            rows,
            limit=limit,
        ))

    return messages
