"""
Custom filter builder: lets the user express conditions like
"10 WEMA > 40 WEMA" or "200 DEMA >= 200" -- comparing one metric
against either another metric or a fixed number.

Filters are stored per market in custom_filters.json:
{
  "US":    [ {id, metric_a, operator, compare_type, metric_b|value} ],
  "INDIA": [ ... ]
}

compare_type is "metric" (compare metric_a to metric_b) or
"value" (compare metric_a to a fixed number).
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


def load_custom_filters():
    if not os.path.exists(CUSTOM_FILTERS_FILE):
        return {"US": [], "INDIA": []}
    with open(CUSTOM_FILTERS_FILE) as f:
        data = json.load(f)
    return {"US": data.get("US", []), "INDIA": data.get("INDIA", [])}


def save_custom_filters(all_filters):
    with open(CUSTOM_FILTERS_FILE, "w") as f:
        json.dump({"US": all_filters.get("US", []), "INDIA": all_filters.get("INDIA", [])}, f, indent=2)


def get_market_filters(market):
    return load_custom_filters().get(market, [])


def save_market_filters(market, filter_list):
    all_filters = load_custom_filters()
    all_filters[market] = filter_list
    save_custom_filters(all_filters)


def passes_filter(row, filt):
    """Returns True if row satisfies the filter, False if it fails,
    None if a required value is missing (treated as 'unknown' -> excluded)."""
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


def apply_filters(rows, filter_list):
    """AND-combines all filters in filter_list; returns the subset of rows
    that satisfy every one."""
    if not filter_list:
        return rows
    out = []
    for row in rows:
        if all(passes_filter(row, f) for f in filter_list):
            out.append(row)
    return out


def describe_filter(filt, metric_labels):
    label_a = metric_labels.get(filt["metric_a"], filt["metric_a"])
    if filt["compare_type"] == "metric":
        label_b = metric_labels.get(filt["metric_b"], filt["metric_b"])
        return f"{label_a} {filt['operator']} {label_b}"
    return f"{label_a} {filt['operator']} {filt['value']}"
