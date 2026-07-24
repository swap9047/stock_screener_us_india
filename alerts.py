"""
Alert rule engine shared by app.py (rule builder + preview) and
alert_check.py (the scheduled background checker that sends Discord alerts).

Rule schema (alerts_config.json):
[
  {
    "id": "a1b2c3",
    "name": "Stage 2 breakout",           # optional display label
    "scope": "ALL" | "US" | "INDIA" | "<TICKER>",
    "conditions": [
        {"metric_a": "ema10", "operator": ">", "compare_type": "value", "value": 40},
        {"metric_a": "rsi14_daily", "operator": ">", "compare_type": "value", "value": 45, "logic": "OR"}
    ],
    "enabled": true
  }
]

Conditions use the exact same structure as the per-market custom filter
builder on the watchlist tabs (see filters.py): any metric can be compared
to another metric or to a fixed value, and multiple conditions chain
left-to-right with a per-condition AND/OR (e.g. cond1 AND cond2 AND cond3 OR
cond4). This module reuses filters.passes_filter_chain, so alert conditions
and watchlist filters always evaluate identically.

Rules are edge-triggered via alert_state.json: an alert fires once when the
combined condition transitions from false to true, then stays silent until
it exits and re-enters (one Discord ping per new occurrence, not one every
single day the condition remains true).
"""

import json
import os
from datetime import date

import requests

from filters import passes_filter_chain, describe_chain

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RULES_FILE = os.path.join(SCRIPT_DIR, "alerts_config.json")
STATE_FILE = os.path.join(SCRIPT_DIR, "alert_state.json")
DISCORD_CONFIG_FILE = os.path.join(SCRIPT_DIR, "discord_config.json")

SCOPE_LABELS = {"ALL": "All watchlist", "US": "US watchlist", "INDIA": "India watchlist"}

# --- Per-rule scheduling -----------------------------------------------
# Each rule carries a "schedule" dict: {"type": "scheduled"|"none", "days":
# [...], "time_et": "HH:00"}. "none" means the rule is a scan-only rule --
# never sent to Discord, but still usable as a watchlist filter (see
# app.py's "Filter by Saved Scans / Alerts").
#
# ALLOWED_HOURS must always match EXACTLY which hour(s) the GitHub Actions
# workflow (daily-alerts.yml) actually wakes up at -- currently just
# 10:00 PM ET, once/day, to keep Actions usage minimal. It was briefly
# widened to all 24 hours (workflow running hourly) but that decoupled the
# picker from reality: the dropdown let you pick, say, 3:00 PM, but the
# workflow never woke up then, so that rule would silently never fire.
# Restricting ALLOWED_HOURS back down to exactly what the workflow supports
# means the picker can never offer an hour that doesn't actually work --
# if you want more/different hours later, both this list AND the workflow's
# `on.schedule` cron lines need to change together (see daily-alerts.yml's
# comment block for the DST-safe two-line-per-hour pattern).
# normalize_schedule() clamps any invalid/unsupported hour to the nearest
# allowed one, so a stale or corrupted schedule can never silently hold up
# a rule.
DAY_CODES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
DAY_LABELS = {"MON": "Mon", "TUE": "Tue", "WED": "Wed", "THU": "Thu", "FRI": "Fri", "SAT": "Sat", "SUN": "Sun"}
DEFAULT_DAYS = ["MON", "TUE", "WED", "THU", "FRI"]
ALLOWED_HOURS = [22]  # 10:00 PM ET -- the only hour the workflow runs at
HOUR_LABELS = {22: "10:00 PM"}


def normalize_schedule(sched):
    """Coerce any schedule dict (missing, partial, or malformed) into a
    valid {"type", "days", "time_et"} dict, where time_et is always a valid
    0-23 hour. Idempotent -- any out-of-range/garbled hour gets clamped to
    the nearest workflow-supported hour, so a corrupted or stale schedule can
    never silently break a rule. ALLOWED_HOURS must match the GitHub Actions
    cron wakeup hour(s); currently that means 10:00 PM ET only."""
    if not isinstance(sched, dict):
        sched = {}

    stype = sched.get("type", "scheduled")
    if stype not in ("scheduled", "none"):
        stype = "scheduled"

    days = sched.get("days")
    if not isinstance(days, list) or not days:
        days = list(DEFAULT_DAYS)
    else:
        days = [d.upper() for d in days if isinstance(d, str) and d.upper() in DAY_CODES]
        if not days:
            days = list(DEFAULT_DAYS)

    time_et = sched.get("time_et", "22:00")
    hour = 22
    if isinstance(time_et, str) and ":" in time_et:
        h, _, _m = time_et.partition(":")
        if h.isdigit() and 0 <= int(h) < 24:
            hour = int(h)
    if hour not in ALLOWED_HOURS:
        hour = min(ALLOWED_HOURS, key=lambda a: abs(a - hour))
    time_et = f"{hour:02d}:00"

    return {"type": stype, "days": days, "time_et": time_et}


def describe_schedule(rule):
    sched = rule.get("schedule", {})
    if sched.get("type") == "none":
        return "🔍 Scan Only"
    days = sched.get("days", DEFAULT_DAYS)
    days_str = ", ".join(DAY_LABELS.get(d, d) for d in days)
    time_et = sched.get("time_et", "22:00")
    return f"🔔 {days_str} @ {time_et} ET"


DUE_TOLERANCE_HOURS = 4
# GitHub Actions' scheduler is documented as best-effort: a cron trigger can
# be delayed well past its nominal time, especially during high load (we
# saw this directly -- both of the day's cron runs landed as gate-only
# "not due" on 2026-07-23 even though one of them nominally corresponds to
# exactly 22:00 ET). An exact-hour match has zero tolerance for that delay,
# so a late-running trigger silently misses its whole day. Widening the
# match to a +/-1 hour window absorbs realistic scheduler delay (up to
# ~60 min) without any real downside: the workflow only ever wakes up
# around this one daily window, and alerts are edge-triggered (see
# evaluate_and_fire), so if both of the day's cron lines happen to land
# inside the window, the second run just finds nothing new to fire.


def is_rule_due(rule, et_now=None):
    """Is this rule due to be checked right now? Compares the rule's
    schedule against `et_now` (a tz-aware America/New_York datetime;
    defaults to the current time). Matches on day-of-week, then hour within
    DUE_TOLERANCE_HOURS of the scheduled hour -- the workflow YAML owns
    which hour(s) actually wake up, and ALLOWED_HOURS keeps the app's
    schedule picker aligned with those cron triggers, but a scheduler delay
    of up to DUE_TOLERANCE_HOURS still counts as on-time rather than being
    silently missed."""
    sched = rule.get("schedule", {})
    if sched.get("type") == "none":
        return False

    if et_now is None:
        from datetime import datetime
        try:
            import zoneinfo
            et_tz = zoneinfo.ZoneInfo("America/New_York")
        except Exception:
            # Practically unreachable: GitHub Actions runs Python 3.11+,
            # where zoneinfo is stdlib. No safe fixed-offset fallback
            # exists here (it would be wrong by an hour half the year),
            # so if this ever triggers, treat nothing as due rather than
            # guess.
            return False
        et_now = datetime.now(et_tz)

    day_code = DAY_CODES[et_now.weekday()]
    allowed_days = sched.get("days", DEFAULT_DAYS)
    if day_code not in allowed_days:
        return False

    time_et = sched.get("time_et", "22:00")
    try:
        rule_hour = int(time_et.split(":")[0])
    except Exception:
        rule_hour = 22

    hour_diff = min((et_now.hour - rule_hour) % 24, (rule_hour - et_now.hour) % 24)
    return hour_diff <= DUE_TOLERANCE_HOURS


def normalize_rule(rule):
    """Upgrade older rule formats to the current metric-comparison schema.
    Idempotent. Older formats used preset event/threshold metrics (e.g.
    "cross_below_10wema") that have no equivalent here -- those rules are
    kept (so nothing silently disappears) but disabled with empty
    conditions, so you can see them and re-add with the new condition
    builder."""
    conditions = rule.get("conditions")
    is_current_format = conditions is not None and (len(conditions) == 0 or "metric_a" in conditions[0])
    if is_current_format:
        rule.setdefault("name", "")
        rule.setdefault("scope", "ALL")
        rule.setdefault("enabled", True)
        rule["schedule"] = normalize_schedule(rule.get("schedule"))
        return rule
    return {
        "id": rule.get("id"),
        "name": rule.get("name") or "(old rule format — please re-add)",
        "scope": rule.get("scope", "ALL"),
        "conditions": [],
        "enabled": False,
        "schedule": normalize_schedule(None),
    }


def load_rules():
    if not os.path.exists(RULES_FILE):
        return []
    with open(RULES_FILE) as f:
        raw = json.load(f)
    return [normalize_rule(r) for r in raw]


def save_rules(rules):
    with open(RULES_FILE, "w") as f:
        json.dump(rules, f, indent=2)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_discord_webhook():
    """Checks, in order: DISCORD_WEBHOOK_URL env var (for headless runs like
    GitHub Actions), then discord_config.json (local Streamlit runs).
    The Streamlit app also checks st.secrets first (see app.py)."""
    env_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if env_url:
        return env_url
    if not os.path.exists(DISCORD_CONFIG_FILE):
        return None
    try:
        with open(DISCORD_CONFIG_FILE) as f:
            cfg = json.load(f)
        return cfg.get("webhook_url") or None
    except Exception:
        return None


def _applicable_tickers(rule, snapshot_results):
    """snapshot_results rows must carry a "market" key (US/INDIA) -- set by
    stock_data.fetch_all_markets -- so scope US/INDIA can be resolved."""
    scope = rule.get("scope", "ALL")
    if scope == "ALL":
        return [r["ticker"] for r in snapshot_results]
    if scope in ("US", "INDIA"):
        return [r["ticker"] for r in snapshot_results if r.get("market") == scope]
    return [scope] if any(r["ticker"] == scope for r in snapshot_results) else []


def preview_rules(rules, snapshot_results):
    """Returns list of dicts describing current truth value of every rule x
    ticker combo, WITHOUT touching alert_state.json. Used by the Streamlit
    app to show 'what would fire right now'."""
    by_ticker = {r["ticker"]: r for r in snapshot_results}
    out = []
    for rule in rules:
        if not rule.get("enabled", True) or not rule.get("conditions"):
            continue
        for ticker in _applicable_tickers(rule, snapshot_results):
            row = by_ticker.get(ticker)
            if not row:
                continue
            is_true = passes_filter_chain(row, rule["conditions"])
            out.append({
                "rule_id": rule["id"],
                "rule_name": rule.get("name", ""),
                "ticker": ticker,
                "conditions": rule["conditions"],
                "row": row,
                "is_true_now": is_true,
            })
    return out


def evaluate_and_fire(rules, snapshot_results, state, metric_labels=None):
    """Edge-triggered evaluation used by the scheduled alert_check.py run.
    Returns (messages list, updated_state dict). `messages` is one Discord-
    ready table PER RULE that has at least one ticker newly triggering today
    (false -> true transition) -- tickers that were already active stay
    silent, matching the existing edge-triggered behavior exactly; only the
    formatting (a table instead of one line per ticker) changed."""
    metric_labels = metric_labels or {}
    by_ticker = {r["ticker"]: r for r in snapshot_results}
    today = date.today().isoformat()
    new_state = dict(state)
    newly_triggered_by_rule = {}  # rule_id -> [ticker, ...]

    for rule in rules:
        if not rule.get("enabled", True) or not rule.get("conditions"):
            continue
        for ticker in _applicable_tickers(rule, snapshot_results):
            row = by_ticker.get(ticker)
            if not row:
                continue
            is_true = passes_filter_chain(row, rule["conditions"])
            key = f"{rule['id']}:{ticker}"
            prev = new_state.get(key, {"was_active": False, "last_triggered_date": None})

            if is_true and not prev.get("was_active"):
                newly_triggered_by_rule.setdefault(rule["id"], []).append(ticker)
                new_state[key] = {"was_active": True, "last_triggered_date": today}
            else:
                new_state[key] = {"was_active": is_true, "last_triggered_date": prev.get("last_triggered_date")}

    rules_by_id = {r["id"]: r for r in rules}
    messages = []
    for rule_id, tickers in newly_triggered_by_rule.items():
        messages.extend(build_discord_messages_for_rule(rules_by_id[rule_id], tickers, by_ticker, metric_labels))

    return messages, new_state


def _format_rule_message(ticker, rule, metric_labels):
    name = rule.get("name") or ""
    desc = describe_chain(rule["conditions"], metric_labels)
    if name:
        return f"**{ticker}** — {name}: {desc}"
    return f"**{ticker}** — {desc}"


def _metrics_used_in_conditions(conditions):
    """Every metric referenced by a rule's condition chain, in first-seen
    order (metric_a always; metric_b too when compare_type is "metric"),
    deduplicated -- these become the table's columns."""
    seen = []
    for cond in conditions:
        metrics_here = [cond.get("metric_a")]
        if cond.get("compare_type") == "metric":
            metrics_here.append(cond.get("metric_b"))
        for m in metrics_here:
            if m and m not in seen:
                seen.append(m)
    return seen


def _format_cell(metric_key, value):
    if value is None:
        return "—"
    if metric_key == "tech_uptrend":
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:,.1f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _ascii_table(headers, rows):
    widths = [len(str(h)) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))

    def fmt_row(cells):
        return " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))

    sep = "-+-".join("-" * w for w in widths)
    return "\n".join([fmt_row(headers), sep] + [fmt_row(r) for r in rows])


def build_discord_messages_for_rule(rule, tickers, snapshot_by_ticker, metric_labels, limit=1900):
    """Builds one or more Discord-ready messages for a single rule: a
    monospace table with Ticker as the first column, then one column per
    metric referenced in the rule's conditions with that ticker's current
    value. Splits into multiple messages (repeating the header) if the full
    table would exceed Discord's ~2000-char limit. Returns [] if `tickers`
    is empty."""
    if not tickers:
        return []
    metrics = _metrics_used_in_conditions(rule.get("conditions", []))
    headers = ["Ticker"] + [metric_labels.get(m, m) for m in metrics]
    all_rows = []
    for t in tickers:
        row = snapshot_by_ticker.get(t, {})
        all_rows.append([t] + [_format_cell(m, row.get(m)) for m in metrics])

    name = rule.get("name") or "(unnamed)"
    scope_label = SCOPE_LABELS.get(rule.get("scope"), rule.get("scope"))
    title = f"**{name}** — {scope_label}"

    def build_chunk(rows_subset, part=None):
        table = _ascii_table(headers, rows_subset)
        head = title + (f" (part {part})" if part else "")
        return f"{head}\n```\n{table}\n```"

    whole = build_chunk(all_rows)
    if len(whole) <= limit:
        return [whole]

    # Doesn't fit in one message -- split rows across multiple, each with
    # its own header/title so every chunk is readable standalone.
    messages, current, part = [], [], 1
    for r in all_rows:
        trial = current + [r]
        if len(build_chunk(trial, part)) > limit and current:
            messages.append(build_chunk(current, part))
            part += 1
            current = [r]
        else:
            current = trial
    if current:
        messages.append(build_chunk(current, part))
    return messages


def send_discord(webhook_url, content):
    try:
        resp = requests.post(webhook_url, json={"content": content}, timeout=10)
    except requests.RequestException as e:
        print(f"Discord send failed: {e}")
        return False
    if resp.status_code not in (200, 204):
        print(f"Discord send failed: {resp.status_code} {resp.text}")
        return False
    return True
