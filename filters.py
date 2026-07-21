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

Multiple conditions chain left-to-right, each with its own "logic" field
("AND" or "OR", default "AND") describing how it combines with the running
result of everything before it. The first condition's "logic" is ignored
(nothing precedes it). This allows mixed chains like:
    cond1 AND cond2 AND cond3 OR cond4
evaluated strictly left to right (no parentheses/precedence -- matches how
most simple rule builders, e.g. Zapier/IFTTT, work).

Custom filters are stored per market in custom_filters.json:
{"US": [ {...condition, "id": "..."}, ... ], "INDIA": [ ... ]}
"""

import json
import operator as op
import os

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


def passes_filter(row, filt):
    """Returns True/False for a single condition against one row. A missing
    value (metric not computed for this ticker) fails the condition."""
    a = row.get(filt["metric_a"])
    if a is None:
        return False
    if filt["compare_type"] == "metric":
        b = row.get(filt["metric_b"])
        if b is None:
            return False
    else:
        b = filt["value"]
    operator_symbol = filt["operator"]
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


def describe_filter(filt, metric_labels):
    label_a = metric_labels.get(filt["metric_a"], filt["metric_a"])
    if filt["compare_type"] == "metric":
        label_b = metric_labels.get(filt["metric_b"], filt["metric_b"])
        return f"{label_a} {filt['operator']} {label_b}"
    return f"{label_a} {filt['operator']} {filt['value']}"


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
        val_a = row.get(cond["metric_a"])
        val_a_str = f"{val_a:.1f}" if isinstance(val_a, float) else str(val_a)
        if cond["compare_type"] == "metric":
            label_b = metric_labels.get(cond["metric_b"], cond["metric_b"])
            val_b = row.get(cond["metric_b"])
            val_b_str = f"{val_b:.1f}" if isinstance(val_b, float) else str(val_b)
            desc = f"{label_a}[{val_a_str}] {cond['operator']} {label_b}[{val_b_str}]"
        else:
            desc = f"{label_a}[{val_a_str}] {cond['operator']} {cond['value']}"
        parts.append(f"{prefix}{desc}")
    return "".join(parts)
