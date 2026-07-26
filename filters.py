"""
Condition-based filtering, shared structure used by both:
  - the per-market custom filter builder on the watchlist tabs (conditions
    here narrow which tickers are SHOWN in the table)
  - the alert rule builder on the Alert Rules tab (conditions here determine
    when a Discord alert FIRES -- see alerts.py, which reuses
    passes_filter_chain from this module so filters and alerts always behave
    identically)

A single condition compares one metric to either another metric or a fixed
value:
    {"metric_a": "ema10", "operator": ">", "compare_type": "value", "value": 40}
    {"metric_a": "ema20", "operator": ">", "compare_type": "metric", "metric_b": "ema40"}

When compare_type is "metric", metric_b may optionally be scaled with a
multiplier and/or offset before the comparison -- e.g. "Vol 10D Avg >= 1.4 *
Vol 100D Avg" is:
    {"metric_a": "avg_volume_10d", "operator": ">=", "compare_type": "metric",
     "metric_b": "avg_volume_100d", "multiplier": 1.4, "offset": 0}
The effective right-hand side is always: row[metric_b] * multiplier + offset
(multiplier defaults to 1, offset defaults to 0, so old saved conditions
without these keys behave exactly as before).

Multiple conditions chain left-to-right, each with its own "logic" field
("AND" or "OR", default "AND") describing how it combines with the running
result of everything before it. The first condition's "logic" is ignored
(nothing precedes it). This allows mixed chains like:
    cond1 AND cond2 AND cond3 OR cond4
evaluated strictly left to right (no parentheses/precedence -- matches how
most simple rule builders, e.g. Zapier/IFTTT, work).

Custom filters are stored per market in custom_filters.json:
{"US": [ {...condition, "id": "..."}, ... ], "INDIA": [ ... ]}

Fixed ("value") comparisons accept text as well as numbers -- e.g.
{"metric_a": "tech_uptrend", "operator": "==", "compare_type": "value",
 "value": "Yes"} matches the same rows as {"value": 1}. Numeric-looking text
("45", "1.4") is parsed to a number; "yes"/"true"/"no"/"false" (any case) map
to 1/0 when the metric itself is numeric (so it lines up with 0/1 fields
like Tech Uptrend); anything else is compared as a case-insensitive string.
See _coerce_fixed_value below.
"""

import json
import operator as op
import os

from ticker_notes import FLAG_CHOICES

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CUSTOM_FILTERS_FILE = os.path.join(SCRIPT_DIR, "custom_filters.json")

OPERATORS = {
    ">": op.gt,
    "<": op.lt,
    ">=": op.ge,
    "<=": op.le,
    "==": None,  # handled with tolerance below
}

EQ_TOLERANCE = 0.05  # values are rounded to 1 decimal, so treat "==" as approx-equal

# Metric field -> its full set of valid values, for any metric that's really
# a category/label rather than a continuous number (Trend, Vol Trend, VStop
# Dir, Tech Uptrend). The app's condition builders use this to swap the
# free-text "Value" box for a multiselect of the real values, and to enable
# the "in" operator (match ANY of several selected values, e.g. Trend in
# [Downtrend, Strong Downtrend]) instead of only single-value "==".
CATEGORICAL_METRICS = {
    "trend": ["Strong Uptrend", "Uptrend", "Downtrend", "Strong Downtrend"],
    "volume_trend": ["Exploding", "In-line", "Declining"],
    "vstop_weekly_direction": ["Up", "Down"],
    "tech_uptrend": ["Yes", "No"],
    "flag": FLAG_CHOICES,  # see ticker_notes.py -- Red/Yellow/Green/Blue
    "expert_take": ["Accumulate", "Hold", "Caution", "Pending"],
}


def _normalize_conditions(val):
    """Accepts old formats -- a plain list with no per-item "logic" (implicit
    AND), or the short-lived {"logic": "AND"/"OR", "filters": [...]}
    global-logic format -- and always returns a flat list of conditions,
    each carrying its own "logic" key."""
    if isinstance(val, dict):
        global_logic = val.get("logic", "AND")
        items = val.get("filters", [])
    else:
        global_logic = "AND"
        items = val or []
    out = []
    for item in items:
        item = dict(item)
        item.setdefault("logic", global_logic)
        out.append(item)
    return out


def load_custom_filters():
    """Returns {"US": [condition, ...], "INDIA": [condition, ...]}."""
    if not os.path.exists(CUSTOM_FILTERS_FILE):
        return {"US": [], "INDIA": []}
    with open(CUSTOM_FILTERS_FILE) as f:
        data = json.load(f)
    return {
        "US": _normalize_conditions(data.get("US", [])),
        "INDIA": _normalize_conditions(data.get("INDIA", [])),
    }


def save_custom_filters(all_filters):
    with open(CUSTOM_FILTERS_FILE, "w") as f:
        json.dump({
            "US": all_filters.get("US", []),
            "INDIA": all_filters.get("INDIA", []),
        }, f, indent=2)


def get_market_filters(market):
    return load_custom_filters().get(market, [])


def save_market_filters(market, filter_list):
    all_filters = load_custom_filters()
    all_filters[market] = filter_list
    save_custom_filters(all_filters)


_BOOL_TRUE_WORDS = {"yes", "true", "1"}
_BOOL_FALSE_WORDS = {"no", "false", "0"}


def _coerce_fixed_value(a, value):
    """Coerces a fixed ("value") comparison target to line up with `a`
    (the current metric_a value) at compare time:
      - non-string values (old saved filters, already numeric) pass through
      - numeric-looking text ("45", "1.4") -> float
      - "yes"/"true"/"no"/"false" (any case) -> 1/0, but only when `a` is
        itself numeric, so it matches 0/1 fields like Tech Uptrend
      - anything else is left as a stripped string for case-insensitive
        string comparison (e.g. a categorical metric)
    """
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    try:
        return float(stripped)
    except ValueError:
        pass
    lowered = stripped.lower()
    if isinstance(a, (int, float)) and not isinstance(a, bool):
        if lowered in _BOOL_TRUE_WORDS:
            return 1
        if lowered in _BOOL_FALSE_WORDS:
            return 0
    return stripped


def _resolve_metric_b(row, filt):
    """Returns the effective right-hand-side value for a condition, or None
    if it can't be resolved (missing metric). Applies multiplier/offset when
    compare_type is "metric" (both default to identity: *1 +0); for the
    "in" operator returns the raw list of selected values unchanged (each
    item gets coerced individually in passes_filter, since they need to be
    compared against `a` one at a time); otherwise coerces a single text
    fixed value (see _coerce_fixed_value)."""
    if filt["compare_type"] == "metric":
        b = row.get(filt["metric_b"])
        if b is None:
            return None
        multiplier = filt.get("multiplier", 1) or 1
        offset = filt.get("offset", 0) or 0
        return b * multiplier + offset
    if filt.get("operator") == "in":
        return filt.get("value")
    return _coerce_fixed_value(row.get(filt["metric_a"]), filt["value"])


def _get_metric_val(row, metric_key):
    val = row.get(metric_key)
    if val is None and metric_key == "expert_take":
        try:
            from expert_views import load_expert_views
            ev = load_expert_views().get(row.get("ticker", ""), {})
            verdict = ev.get("verdict", "").title()
            if not verdict or verdict in ("Pending", "Failed"):
                verdict = "Pending"
            return verdict
        except Exception:
            return "Pending"
    return val


def passes_filter(row, filt):
    """Returns True/False for a single condition against one row. A missing
    value (metric not computed for this ticker) fails the condition."""
    a = _get_metric_val(row, filt["metric_a"])
    if a is None:
        return False
    b = _resolve_metric_b(row, filt)
    if b is None:
        return False
    operator_symbol = filt["operator"]

    if operator_symbol == "in":
        # Matches if `a` equals ANY of the selected values -- e.g. Trend in
        # [Downtrend, Strong Downtrend]. Used for categorical metrics (see
        # CATEGORICAL_METRICS), where `value` is a list of option strings
        # picked from a multiselect, not a single typed value.
        candidates = b if isinstance(b, list) else [b]
        coerced = [_coerce_fixed_value(a, v) for v in candidates]
        if isinstance(a, str):
            a_cmp = a.strip().lower()
            return a_cmp in {str(v).strip().lower() for v in coerced}
        return any(
            (a == v) or (isinstance(v, (int, float)) and not isinstance(v, bool) and abs(a - v) <= EQ_TOLERANCE)
            for v in coerced
        )

    if isinstance(a, str) or isinstance(b, str):
        # Only equality is really meaningful for strings; other operators
        # fall back to lexicographic comparison rather than erroring.
        a_cmp, b_cmp = str(a).strip().lower(), str(b).strip().lower()
        if operator_symbol == "==":
            return a_cmp == b_cmp
        return OPERATORS[operator_symbol](a_cmp, b_cmp)
    if operator_symbol == "==":
        return abs(a - b) <= EQ_TOLERANCE
    return OPERATORS[operator_symbol](a, b)


def passes_filter_chain(row, conditions):
    """Evaluates a list of conditions left-to-right, combining each with the
    running result via its own "logic" field. Empty list => True (no
    restriction)."""
    if not conditions:
        return True
    result = passes_filter(row, conditions[0])
    for cond in conditions[1:]:
        this_result = passes_filter(row, cond)
        if cond.get("logic", "AND") == "OR":
            result = result or this_result
        else:
            result = result and this_result
    return result


def apply_filters(rows, filter_list):
    """Returns the subset of rows that satisfy the full condition chain."""
    if not filter_list:
        return rows
    return [row for row in rows if passes_filter_chain(row, filter_list)]


def _metric_b_expr(filt, metric_labels):
    """Human-readable right-hand side, e.g. 'Vol 100D Avg', '1.4 * Vol 100D
    Avg', or '1.4 * Vol 100D Avg + 5' when a multiplier/offset is set. For
    "in", renders the selected values as a bracketed list, e.g.
    '[Downtrend, Strong Downtrend]'."""
    if filt.get("operator") == "in":
        values = filt.get("value") or []
        return "[" + ", ".join(str(v) for v in values) + "]"
    if filt["compare_type"] != "metric":
        return str(filt["value"])
    label_b = metric_labels.get(filt["metric_b"], filt["metric_b"])
    multiplier = filt.get("multiplier", 1) or 1
    offset = filt.get("offset", 0) or 0
    expr = label_b
    if multiplier != 1:
        expr = f"{multiplier}× {expr}"
    if offset > 0:
        expr = f"{expr} + {offset}"
    elif offset < 0:
        expr = f"{expr} - {abs(offset)}"
    return expr


def describe_filter(filt, metric_labels):
    label_a = metric_labels.get(filt["metric_a"], filt["metric_a"])
    return f"{label_a} {filt['operator']} {_metric_b_expr(filt, metric_labels)}"


def describe_chain(conditions, metric_labels):
    """Human-readable description of a full condition chain, e.g.
    '10 WEMA > 40 WEMA AND RSI Daily > 45 OR RSI Weekly < 30'."""
    parts = []
    for i, cond in enumerate(conditions):
        prefix = "" if i == 0 else f" {cond.get('logic', 'AND')} "
        parts.append(f"{prefix}{describe_filter(cond, metric_labels)}")
    return "".join(parts)


def describe_chain_with_values(row, conditions, metric_labels):
    """Like describe_chain, but appends the row's actual current metric
    values to each condition, e.g. '10 WEMA[42.3] > 40 WEMA[39.1]' -- used in
    the alert preview so you can see why a condition is/isn't true."""
    parts = []
    for i, cond in enumerate(conditions):
        prefix = "" if i == 0 else f" {cond.get('logic', 'AND')} "
        label_a = metric_labels.get(cond["metric_a"], cond["metric_a"])
        val_a = _get_metric_val(row, cond["metric_a"])
        val_a_str = f"{val_a:.1f}" if isinstance(val_a, float) else str(val_a)
        expr_b = _metric_b_expr(cond, metric_labels)
        if cond.get("operator") == "in":
            # expr_b is already the full bracketed value list -- no separate
            # "resolved" value to show alongside it (unlike a metric_b,
            # which has both a label and a live number).
            desc = f"{label_a}[{val_a_str}] in {expr_b}"
        else:
            resolved_b = _resolve_metric_b(row, cond)
            resolved_b_str = f"{resolved_b:.1f}" if isinstance(resolved_b, float) else str(resolved_b)
            desc = f"{label_a}[{val_a_str}] {cond['operator']} {expr_b}[{resolved_b_str}]"
        parts.append(f"{prefix}{desc}")
    return "".join(parts)
