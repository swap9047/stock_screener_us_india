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
        return rule
    return {
        "id": rule.get("id"),
        "name": rule.get("name") or "(old rule format — please re-add)",
        "scope": rule.get("scope", "ALL"),
        "conditions": [],
        "enabled": False,
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
    Returns (messages list, updated_state dict)."""
    metric_labels = metric_labels or {}
    by_ticker = {r["ticker"]: r for r in snapshot_results}
    today = date.today().isoformat()
    messages = []
    new_state = dict(state)

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
                messages.append(_format_rule_message(ticker, rule, metric_labels))
                new_state[key] = {"was_active": True, "last_triggered_date": today}
            else:
                new_state[key] = {"was_active": is_true, "last_triggered_date": prev.get("last_triggered_date")}

    return messages, new_state


def _format_rule_message(ticker, rule, metric_labels):
    name = rule.get("name") or ""
    desc = describe_chain(rule["conditions"], metric_labels)
    if name:
        return f"**{ticker}** — {name}: {desc}"
    return f"**{ticker}** — {desc}"


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
