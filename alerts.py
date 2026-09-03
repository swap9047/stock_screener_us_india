"""
Alert rule engine shared by app.py (rule builder + preview) and
alert_check.py (the scheduled background checker that sends Discord alerts).

Rule schema (alerts_config.json):
[
  {
    "id": "a1b2c3",
    "name": "Stage 2 breakout",           # optional display label
    "scope": "ALL" | "<any registered market key>" | "<TICKER>",
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
import time
from datetime import date

import requests

from filters import passes_filter_chain, describe_chain, describe_chain_with_values

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RULES_FILE = os.path.join(SCRIPT_DIR, "alerts_config.json")
STATE_FILE = os.path.join(SCRIPT_DIR, "alert_state.json")
DISCORD_CONFIG_FILE = os.path.join(SCRIPT_DIR, "discord_config.json")

SCOPE_LABELS = {"ALL": "All watchlist", "US": "US watchlist", "INDIA": "India watchlist"}

# Alerts-column number color, per rule -- "green"/"red"/None (unset, renders
# plain). Reuses the exact hex values already used for the Sentiment
# column's Bullish/Bearish styling (app.py's _fundamentals_cell), so the
# color language is consistent across the app rather than a new palette.
RULE_COLOR_HEX = {"green": "#00C853", "red": "#FF5252"}


def _scope_label(scope):
    """Human-readable label for a rule's scope: "All watchlist", "<market
    label> watchlist" for any registered market (falls back to the raw key
    if the registry lookup fails for any reason), or the scope itself (a
    single ticker symbol)."""
    if scope == "ALL":
        return SCOPE_LABELS["ALL"]
    try:
        from stock_data import load_markets_registry
        registry = load_markets_registry()
        if scope in registry:
            return f"{registry[scope]['label']} watchlist"
    except Exception:
        pass
    return SCOPE_LABELS.get(scope, scope)

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
ALLOWED_HOURS = [21]  # 9:00 PM ET -- the only hour the workflow runs at
HOUR_LABELS = {21: "9:00 PM"}


def normalize_schedule(sched):
    """Coerce any schedule dict (missing, partial, or malformed) into a
    valid {"type", "days", "time_et"} dict, where time_et is always a valid
    0-23 hour. Idempotent -- any out-of-range/garbled hour gets clamped to
    the nearest workflow-supported hour, so a corrupted or stale schedule can
    never silently break a rule. ALLOWED_HOURS must match the GitHub Actions
    cron wakeup hour(s); currently that means 9:00 PM ET only."""
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

    time_et = sched.get("time_et", "21:00")
    hour = 21
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
    time_et = sched.get("time_et", "21:00")
    return f"🔔 {days_str} @ {time_et} ET"


DUE_TOLERANCE_HOURS = 22
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


def is_rule_due(rule, et_now=None, cron_schedule=None):
    """Is this rule due to be checked right now? Compares the rule's
    schedule against `et_now` (a tz-aware America/New_York datetime;
    defaults to the current time). Matches on day-of-week, then hour within
    DUE_TOLERANCE_HOURS of the scheduled hour -- the workflow YAML owns
    which hour(s) actually wake up, and ALLOWED_HOURS keeps the app's
    schedule picker aligned with those cron triggers, but a scheduler delay
    of up to DUE_TOLERANCE_HOURS still counts as on-time rather than being
    silently missed. Rejects cron schedules that belong to the off-season."""
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

    time_et = sched.get("time_et", "21:00")
    try:
        rule_hour = int(time_et.split(":")[0])
    except Exception:
        rule_hour = 21

    # If an evening rule (e.g. 21:00 ET) runs on Saturday or past midnight
    # due to GitHub Actions runner delays (even 6-23 hrs late), the nominal
    # scheduled day was Friday (yesterday).
    from datetime import timedelta
    effective_dt = et_now
    if rule_hour >= 18:
        if et_now.weekday() == 5:  # Saturday (delayed Friday run)
            effective_dt = et_now - timedelta(days=1)
        elif et_now.hour < 6:
            effective_dt = et_now - timedelta(days=1)

    day_code = DAY_CODES[effective_dt.weekday()]
    allowed_days = sched.get("days", DEFAULT_DAYS)
    if day_code not in allowed_days:
        return False

    if cron_schedule:
        try:
            cron_utc_hour = int(cron_schedule.split()[1])
            expected_utc_hour = int((rule_hour - (et_now.utcoffset().total_seconds() / 3600)) % 24)
            if cron_utc_hour != expected_utc_hour:
                return False
        except Exception:
            pass

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

    def _is_current_condition(cond):
        # Current-format conditions are metric comparisons (metric_a present)
        # OR references to another alert ({"type": "rule", "rule_id": ...},
        # which intentionally has no metric_a).
        return "metric_a" in cond or cond.get("type") == "rule"

    is_current_format = (conditions is not None
                         and (len(conditions) == 0 or _is_current_condition(conditions[0])))
    if is_current_format:
        rule.setdefault("name", "")
        rule.setdefault("scope", "ALL")
        rule.setdefault("enabled", True)
        # Opt-in to the Sunday weekly wrap-up digest (weekly_wrapup.py).
        # Independent of "enabled" and of schedule.type -- a "Scan only" rule
        # that never pings Discord daily can still be in the weekly digest.
        rule.setdefault("weekly_wrapup", False)
        # Alerts-column number color -- "green"/"red"/None. None is a real,
        # distinct state (renders unstyled, sorts between green and red),
        # not "not yet migrated" -- so this is a plain setdefault, same as
        # the fields above, not a value that needs further normalizing.
        rule.setdefault("color", None)
        rule["schedule"] = normalize_schedule(rule.get("schedule"))
        return rule
    return {
        "id": rule.get("id"),
        "name": rule.get("name") or "(old rule format — please re-add)",
        "scope": rule.get("scope", "ALL"),
        "conditions": [],
        "enabled": False,
        "weekly_wrapup": False,
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
    """snapshot_results rows must carry a "market" key (its watchlist's
    stable key -- see stock_data.load_markets_registry()) so a scope of
    "<market key>" can be resolved to every ticker in that watchlist."""
    from stock_data import load_markets_registry

    scope = rule.get("scope", "ALL")
    if scope == "ALL":
        return [r["ticker"] for r in snapshot_results]
    if scope in load_markets_registry():
        return [r["ticker"] for r in snapshot_results if r.get("market") == scope]
    return [scope] if any(r["ticker"] == scope for r in snapshot_results) else []


def _rule_references(rule):
    """rule_ids this rule references via rule-type conditions ({"type":
    "rule", "rule_id": ...}), as a set."""
    return {
        c.get("rule_id")
        for c in rule.get("conditions", [])
        if c.get("type") == "rule" and c.get("rule_id")
    }


def _find_cycle_rule_ids(rules_by_id):
    """Rule ids that participate in a circular reference (self-reference or a
    strongly connected component of size > 1), via Tarjan's SCC. These are
    the ones compute_rule_truth resolves to non-matching."""
    refs = {rid: _rule_references(rule) for rid, rule in rules_by_id.items()}
    index, lowlink = {}, {}
    on_stack = set()
    stack = []
    sccs = []

    def strongconnect(v):
        index[v] = lowlink[v] = len(index)
        stack.append(v)
        on_stack.add(v)
        for w in refs.get(v, ()):
            if w not in rules_by_id:
                continue
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])
        if lowlink[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            sccs.append(comp)

    for v in rules_by_id:
        if v not in index:
            strongconnect(v)

    return {
        rid
        for comp in sccs
        for rid in comp
        if len(comp) > 1 or rid in refs.get(rid, ())
    }


def compute_rule_truth(all_rules, snapshot_results):
    """Which enabled rules are currently true for which ticker, resolving
    rule->rule references ({"type": "rule", "rule_id": ...} conditions) to a
    fixed point.

    Returns (rule_truth, cycle_ids) where rule_truth is
    {rule_id: {ticker: bool}} and cycle_ids is the set of rule ids involved in
    a circular reference.

    Conditions are monotone (AND/OR, no negation), so iterating from the empty
    map converges to the LEAST fixed point -- which is exactly the chosen
    cycle policy: a rule-reference caught inside a cycle resolves to False
    (non-matching) while the rule's other conditions still evaluate normally.
    No infinite loop is possible: each pass is monotone and #passes is capped.

    A referenced rule is evaluated against its own scope (_applicable_tickers)
    on the current snapshot, so a reference to a rule scoped to a different
    watchlist is simply False for tickers outside its scope."""
    enabled = [r for r in all_rules if r.get("enabled", True) and r.get("conditions")]
    by_id = {r["id"]: r for r in enabled}
    if not by_id:
        return {}, set()

    rows_by_ticker = {r["ticker"]: r for r in snapshot_results}

    rule_truth = {}
    max_iter = len(by_id) + 1
    for _ in range(max_iter):
        changed = False
        for rid, rule in by_id.items():
            prev = rule_truth.get(rid)
            new_vals = {}
            for t in _applicable_tickers(rule, snapshot_results):
                row = rows_by_ticker.get(t)
                if not row:
                    continue
                v = passes_filter_chain(row, rule["conditions"], rule_truth)
                new_vals[t] = v
                if prev is None or prev.get(t) != v:
                    changed = True
            rule_truth[rid] = new_vals
        if not changed:
            break

    return rule_truth, _find_cycle_rule_ids(by_id)


def preview_rules(rules, snapshot_results):
    """Returns (out, cycle_ids) -- out is a list of dicts describing current
    truth value of every rule x ticker combo (WITHOUT touching
    alert_state.json), used by the Streamlit app to show 'what would fire
    right now'; cycle_ids is the set of rule ids trapped in a circular
    reference so the UI can warn."""
    rule_truth, cycle_ids = compute_rule_truth(rules, snapshot_results)
    by_ticker = {r["ticker"]: r for r in snapshot_results}
    out = []
    for rule in rules:
        if not rule.get("enabled", True) or not rule.get("conditions"):
            continue
        for ticker in _applicable_tickers(rule, snapshot_results):
            row = by_ticker.get(ticker)
            if not row:
                continue
            is_true = passes_filter_chain(row, rule["conditions"], rule_truth)
            out.append({
                "rule_id": rule["id"],
                "rule_name": rule.get("name", ""),
                "ticker": ticker,
                "conditions": rule["conditions"],
                "row": row,
                "is_true_now": is_true,
            })
    return out, cycle_ids


def active_alerts_by_ticker(rules, snapshot_results, metric_labels=None):
    """{ticker: text} describing every enabled rule that is TRUE right now for
    that ticker, rendered with the live values that make it true.

    Feeds the Expert Take prompt's "ACTIVE ALERT RULES TRIGGERED" section,
    which was a dead parameter for the life of that feature: no caller ever
    passed one, so every analysis was told "None" whether or not rules had
    fired -- a positive claim that nothing triggered, not a missing section.

    Reuses preview_rules (the same evaluation behind the dashboard's Alerts
    column) and filters.describe_chain_with_values (the same rendering the
    AI-review payload uses), so the model is shown exactly what the user sees.

    Indexed once per call rather than evaluated per ticker: preview_rules walks
    every rule x ticker combination, so calling it inside a ~110-ticker refresh
    loop would be quadratic for no gain.
    """
    metric_labels = metric_labels or {}
    rule_by_id = {r["id"]: r for r in rules}
    out = {}
    for p in preview_rules(rules, snapshot_results)[0]:
        if not p["is_true_now"]:
            continue
        name = p["rule_name"] or "(unnamed rule)"
        detail = describe_chain_with_values(p["row"], p["conditions"], metric_labels, rule_by_id)
        out.setdefault(p["ticker"], []).append(f"- {name}: {detail}")
    return {t: "\n".join(lines) for t, lines in out.items()}


def active_alerts_for_prompt(snapshot_results, metric_labels=None):
    """active_alerts_by_ticker over the CONFIGURED rules, or None when no rule
    is configured at all -- the distinction build_expert_prompt renders as
    "not evaluated" rather than "evaluated, nothing fired".

    metric_labels is resolved from settings when not supplied; the import is
    local because stock_data is the heavier module and nothing else in this
    file needs it.
    """
    rules = [r for r in load_rules() if r.get("enabled", True) and r.get("conditions")]
    if not rules:
        return None
    if metric_labels is None:
        from stock_data import get_filterable_metrics, load_settings
        metric_labels = {v: k for k, v in get_filterable_metrics(load_settings()).items()}
    return active_alerts_by_ticker(rules, snapshot_results, metric_labels)


def alerts_text_for(alerts_by_ticker, ticker):
    """The per-ticker value build_expert_prompt expects: None when alerts were
    not evaluated at all (no rules configured, or the caller didn't ask), and
    "" when they were evaluated and nothing is currently true for this ticker.
    The two mean different things to the model, so don't collapse them."""
    if alerts_by_ticker is None:
        return None
    return alerts_by_ticker.get(ticker, "")


def evaluate_and_fire(all_rules, snapshot_results, state, metric_labels=None, due_rules=None):
    """Edge-triggered evaluation used by the scheduled alert_check.py run.
    Returns (messages list, updated_state dict). `all_rules` is the COMPLETE
    ruleset -- needed so rule->rule references resolve even when the
    referenced rule isn't due today. `due_rules` (default: all_rules) is the
    subset that actually fires. `messages` is one Discord-ready table PER RULE
    that has at least one ticker newly triggering today (false -> true
    transition) -- tickers that were already active stay silent, matching the
    existing edge-triggered behavior exactly; only the formatting (a table
    instead of one line per ticker) changed."""
    if due_rules is None:
        due_rules = all_rules
    metric_labels = metric_labels or {}
    rule_truth, cycle_ids = compute_rule_truth(all_rules, snapshot_results)
    by_ticker = {r["ticker"]: r for r in snapshot_results}
    today = date.today().isoformat()
    new_state = dict(state)
    newly_triggered_by_rule = {}  # rule_id -> [ticker, ...]

    for rule in due_rules:
        if not rule.get("enabled", True) or not rule.get("conditions"):
            continue
        for ticker in _applicable_tickers(rule, snapshot_results):
            row = by_ticker.get(ticker)
            if not row:
                continue
            is_true = passes_filter_chain(row, rule["conditions"], rule_truth)
            key = f"{rule['id']}:{ticker}"
            prev = new_state.get(key, {"was_active": False, "last_triggered_date": None})

            if is_true and not prev.get("was_active"):
                newly_triggered_by_rule.setdefault(rule["id"], []).append(ticker)
                new_state[key] = {"was_active": True, "last_triggered_date": today}
            else:
                new_state[key] = {"was_active": is_true, "last_triggered_date": prev.get("last_triggered_date")}

    if cycle_ids:
        print(f"WARNING: circular alert references detected among rule(s): {sorted(cycle_ids)} -- those rule references are treated as not matching.")

    rules_by_id = {r["id"]: r for r in all_rules}
    messages = []
    for rule_id, tickers in newly_triggered_by_rule.items():
        rule = rules_by_id.get(rule_id)
        if rule:
            messages.extend(build_discord_messages_for_rule(rule, tickers, by_ticker, metric_labels))

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


# Discord counts a message's length in UTF-16 code units, not Python code
# points, so every astral-plane character (emoji -- and the digests are full of
# them: the news header's 📰, the wrap-up's 📅/🔥, plus whatever the LLM emits)
# costs 2 on their side and 1 to len(). The 1900 limit against a 2000 cap
# absorbed that by luck; LLM-generated text is exactly where the luck would run
# out. Measure the way the receiver does instead.
def _discord_len(text):
    return len(text.encode("utf-16-le")) // 2


def hard_split_text(text, limit):
    """Splits `text` into pieces that each fit `limit` UTF-16 units, preferring
    whitespace boundaries and falling back to a mid-word cut.

    Exists because every chunker here guards with `... > limit and current`,
    which silently lets a FIRST item that alone exceeds the limit fall through
    and get POSTed -- earning a 400 from Discord and a dropped message that
    reads exactly like truncation. Nothing may be emitted without passing
    through this."""
    if _discord_len(text) <= limit:
        return [text]
    pieces, current = [], ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if _discord_len(candidate) <= limit:
            current = candidate
            continue
        if current:
            pieces.append(current)
            current = ""
        # A single word longer than the limit (a URL, a base64 blob) still has
        # to go somewhere -- cut it mid-word rather than emit it oversized.
        while _discord_len(word) > limit:
            cut = limit
            while cut > 0 and _discord_len(word[:cut]) > limit:
                cut -= 1
            pieces.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        pieces.append(current)
    return pieces


# Discord renders *, _, ~, ` and | as formatting. Rule names are user-typed and
# get interpolated straight into **bold** (and sit immediately above a ``` code
# fence), so an unbalanced * italicises the rest of the message and a backtick
# run can break out of the fence entirely.
_MARKDOWN_SPECIALS = ("\\", "*", "_", "~", "`", "|", ">")


def escape_markdown(text):
    out = str(text)
    for ch in _MARKDOWN_SPECIALS:
        out = out.replace(ch, "\\" + ch)
    return out


def _ascii_table(headers, rows):
    widths = [len(str(h)) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))

    def fmt_row(cells):
        return " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))

    sep = "-+-".join("-" * w for w in widths)
    return "\n".join([fmt_row(headers), sep] + [fmt_row(r) for r in rows])


def chunked_table_messages(title, headers, all_rows, limit=1900):
    """One monospace table under `title`, split across as many Discord
    messages as it takes to stay under `limit` chars. Every chunk repeats the
    title (suffixed "(part N)") and the header row, so each one is readable
    standalone. Returns [] for no rows."""
    if not all_rows:
        return []

    def build_chunk(rows_subset, part=None):
        table = _ascii_table(headers, rows_subset)
        head = title + (f" (part {part})" if part else "")
        return f"{head}\n```\n{table}\n```"

    whole = build_chunk(all_rows)
    if _discord_len(whole) <= limit:
        return [whole]

    # Doesn't fit in one message -- split rows across multiple, each with
    # its own header/title so every chunk is readable standalone.
    messages, current, part = [], [], 1
    for r in all_rows:
        trial = current + [r]
        if _discord_len(build_chunk(trial, part)) > limit and current:
            messages.append(build_chunk(current, part))
            part += 1
            current = [r]
        else:
            current = trial
        # A single row that doesn't fit even on its own used to ride the
        # `and current` guard above straight out to Discord and 400. Cut it
        # up instead. Reachable via a very wide rule (many metric columns) or
        # a long user-typed rule name in the title.
        if len(current) == 1 and _discord_len(build_chunk(current, part)) > limit:
            head = title + f" (part {part})"
            overhead = _discord_len(f"{head}\n```\n\n```")
            for piece in hard_split_text(_ascii_table(headers, current), max(limit - overhead, 1)):
                messages.append(f"{head}\n```\n{piece}\n```")
                part += 1
                head = title + f" (part {part})"
            current = []
    if current:
        messages.append(build_chunk(current, part))
    return messages


def chunked_line_messages(text, limit=1900, head_for_part=None):
    """Splits a newline-separated blob across Discord messages on line
    boundaries, hard-splitting any single line that doesn't fit on its own.

    `head_for_part(part_index)` optionally supplies a prefix line per message
    (part_index is 0-based), so a multi-part digest can repeat its title. The
    prefix counts toward `limit`.

    Shared by the weekly wrap-up header and the news digest, both of which
    previously either had no length check at all or let an oversized single
    line through."""
    def head(part):
        return head_for_part(part) if head_for_part else None

    def wrap(body, part):
        h = head(part)
        return f"{h}\n{body}" if h else body

    lines = [ln for ln in text.split("\n") if ln.strip() or text.strip() == ""]
    if not lines:
        return []

    messages, current, part = [], "", 0
    for ln in lines:
        # A line that can't fit even alone gets cut up rather than emitted
        # oversized (the `and current` bug this replaces).
        room = limit - _discord_len(wrap("", part))
        for piece in hard_split_text(ln, max(room, 1)):
            candidate = f"{current}\n{piece}" if current else piece
            if _discord_len(wrap(candidate, part)) > limit and current:
                messages.append(wrap(current, part))
                part += 1
                current = piece
            else:
                current = candidate
    if current:
        messages.append(wrap(current, part))
    return messages


def build_discord_messages_for_rule(rule, tickers, snapshot_by_ticker, metric_labels, limit=1900):
    """Builds one or more Discord-ready messages for a single rule: a
    monospace table with Ticker as the first column, then a Watchlist column
    for ALL-scope rules (so recipients see which market each ticker belongs
    to at a glance), then one column per metric referenced in the rule's
    conditions with that ticker's current value. Splits into multiple messages
    (repeating the header) if the full table would exceed Discord's ~2000-char
    limit. Returns [] if `tickers` is empty."""
    if not tickers:
        return []

    # For ALL-scope rules, inject a Watchlist column so it's clear which
    # market each ticker belongs to (especially when US and India tickers
    # share similar names or are both in the same fired-rule batch).
    include_watchlist_col = rule.get("scope") == "ALL"
    if include_watchlist_col:
        from stock_data import load_markets_registry
        registry = load_markets_registry()

    metrics = _metrics_used_in_conditions(rule.get("conditions", []))
    headers = ["Ticker"] + (["Watchlist"] if include_watchlist_col else []) + [metric_labels.get(m, m) for m in metrics]
    all_rows = []
    for t in tickers:
        row = snapshot_by_ticker.get(t, {})
        if include_watchlist_col:
            mkey = row.get("market", "")
            wl_label = registry.get(mkey, {}).get("label", mkey)
            all_rows.append([t, wl_label] + [_format_cell(m, row.get(m)) for m in metrics])
        else:
            all_rows.append([t] + [_format_cell(m, row.get(m)) for m in metrics])

    name = rule.get("name") or "(unnamed)"
    scope_label = _scope_label(rule.get("scope"))
    title = f"**{escape_markdown(name)}** — {scope_label}"
    return chunked_table_messages(title, headers, all_rows, limit=limit)


# Discord webhooks throttle bursts (roughly 5 requests per 2 seconds). A
# batch of many messages -- the weekly wrap-up alone can produce 15-25 --
# sent back-to-back trips this well before running out of messages, and a
# 429'd message used to just be dropped: the digest would visibly stop
# partway through on the Discord side, reading exactly like truncation even
# though every message was well under Discord's size limit. Retrying on 429
# using Discord's own `retry_after` (seconds) fixes that at the root.
DISCORD_MAX_RATE_LIMIT_RETRIES = 3
DISCORD_DEFAULT_RETRY_WAIT = 1.0


def _post_discord(webhook_url, content):
    """Posts one message to `webhook_url`, retrying on a 429 rate-limit
    response (see module note above). Returns (ok, detail) -- detail is ''
    on success, otherwise a short human-readable reason (the HTTP status +
    response body, or the network exception text) once retries (if any) are
    exhausted."""
    attempt = 0
    while True:
        try:
            resp = requests.post(webhook_url, json={"content": content}, timeout=10)
        except requests.RequestException as e:
            return False, str(e)
        if resp.status_code in (200, 204):
            return True, ""
        if resp.status_code == 429 and attempt < DISCORD_MAX_RATE_LIMIT_RETRIES:
            wait = DISCORD_DEFAULT_RETRY_WAIT
            try:
                wait = float(resp.json().get("retry_after", wait))
            except Exception:
                pass
            time.sleep(wait)
            attempt += 1
            continue
        return False, f"{resp.status_code}: {resp.text[:200]}"


# There used to be a send_discord(webhook, content) -> bool wrapper here for
# the headless callers. It was removed once alert_check.py and news_check.py
# moved to send_discord_batch: both were looping it with no delay at all, which
# meant a burst (a cold-start alert run is ~17 messages) leaned entirely on
# _post_discord's 3 rate-limit retries, and exhausting them dropped a message
# in the middle of a digest. The batch below paces its posts instead. Use it
# for a single message too -- send_discord_batch(url, [msg]).

DISCORD_BATCH_PACING_SECONDS = 0.3


def send_discord_batch(webhook_url, messages, stop_on_failure=True):
    """Posts each of `messages` in order via _post_discord, pacing them
    slightly (see DISCORD_BATCH_PACING_SECONDS) so a multi-message batch
    -- the weekly wrap-up especially -- mostly avoids Discord's rate limit
    in the first place, rather than relying solely on _post_discord's
    retry-on-429. Stops at the first (non-retryable) failure. Returns
    (ok, detail) -- detail is '' on full success, otherwise the specific
    reason the failing message was rejected. Used by the interactive
    Streamlit buttons and the weekly wrap-up's automated send (unlike
    send_discord, which only returns a bool) so the caller can show/log the
    real cause instead of a generic 'check the webhook URL' message that's
    equally true whether the webhook is dead or a specific message got
    rejected.

    `stop_on_failure=False` posts the remaining messages anyway and reports the
    first failure at the end -- what a multi-part digest wants, since one
    rejected part shouldn't swallow the other four. The default stays True so
    the wrap-up and the interactive buttons keep their existing fail-fast
    behavior."""
    first_detail = ""
    for idx, content in enumerate(messages):
        if idx > 0:
            time.sleep(DISCORD_BATCH_PACING_SECONDS)
        ok, detail = _post_discord(webhook_url, content)
        if not ok:
            if stop_on_failure:
                return False, detail
            first_detail = first_detail or detail
    return (not first_detail), first_detail
