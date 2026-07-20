"""
Alert rule engine shared by app.py (rule builder + preview) and
alert_check.py (the scheduled background checker that sends Discord alerts).

Rule schema (alerts_config.json):
[
  {
    "id": "a1b2c3",
    "scope": "ALL" | "<TICKER>",
    "metric": "cross_below_10wema" | ... (see METRICS below),
    "threshold": <number|null>,
    "enabled": true
  }
]

Cross metrics (no threshold) fire on the bar where the cross actually
happens (edge event already computed in stock_data.fetch_snapshot).

Threshold metrics (RSI/RS above/below) are edge-triggered here using
alert_state.json: they fire once when the value *enters* the condition,
then stay silent until the value exits and re-enters (so you get one
alert per new occurrence, not one every single day it stays true).
"""

import json
import os
from datetime import date

import requests

from stock_data import load_settings

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RULES_FILE = os.path.join(SCRIPT_DIR, "alerts_config.json")
STATE_FILE = os.path.join(SCRIPT_DIR, "alert_state.json")
DISCORD_CONFIG_FILE = os.path.join(SCRIPT_DIR, "discord_config.json")


def build_metrics(settings=None):
    """Alert metric definitions. Keys are stable identifiers (unchanged even
    if the underlying period is edited in Settings, so saved rules in
    alerts_config.json keep working) -- only the display label embeds the
    currently configured period."""
    settings = settings or load_settings()
    w_fast, w_mid, w_slow = settings["ema_weekly"]
    d_fast, d_mid, d_slow = settings["ema_daily"]
    return {
        "cross_below_10wema": {"label": f"Crosses BELOW {w_fast} WEMA", "kind": "cross", "field": "crossed_below_10"},
        "cross_above_10wema": {"label": f"Crosses ABOVE {w_fast} WEMA", "kind": "cross", "field": "crossed_above_10"},
        "cross_below_40wema": {"label": f"Crosses BELOW {w_slow} WEMA", "kind": "cross", "field": "crossed_below_40"},
        "cross_above_40wema": {"label": f"Crosses ABOVE {w_slow} WEMA", "kind": "cross", "field": "crossed_above_40"},
        "cross_below_10dema": {"label": f"Crosses BELOW {d_fast} DEMA", "kind": "cross", "field": "crossed_below_10_daily"},
        "cross_above_10dema": {"label": f"Crosses ABOVE {d_fast} DEMA", "kind": "cross", "field": "crossed_above_10_daily"},
        "cross_below_50dema": {"label": f"Crosses BELOW {d_mid} DEMA", "kind": "cross", "field": "crossed_below_50"},
        "cross_above_50dema": {"label": f"Crosses ABOVE {d_mid} DEMA", "kind": "cross", "field": "crossed_above_50"},
        "cross_below_200dema": {"label": f"Crosses BELOW {d_slow} DEMA", "kind": "cross", "field": "crossed_below_200"},
        "cross_above_200dema": {"label": f"Crosses ABOVE {d_slow} DEMA", "kind": "cross", "field": "crossed_above_200"},
        "rsi_daily_below": {"label": "RSI Daily below threshold", "kind": "threshold_below", "field": "rsi14_daily"},
        "rsi_daily_above": {"label": "RSI Daily above threshold", "kind": "threshold_above", "field": "rsi14_daily"},
        "rsi_weekly_below": {"label": "RSI Weekly below threshold", "kind": "threshold_below", "field": "rsi14_weekly"},
        "rsi_weekly_above": {"label": "RSI Weekly above threshold", "kind": "threshold_above", "field": "rsi14_weekly"},
        "rsi_monthly_below": {"label": "RSI Monthly below threshold", "kind": "threshold_below", "field": "rsi14_monthly"},
        "rsi_monthly_above": {"label": "RSI Monthly above threshold", "kind": "threshold_above", "field": "rsi14_monthly"},
        "rs_daily_below": {"label": "RS Daily below threshold", "kind": "threshold_below", "field": "rs_daily"},
        "rs_daily_above": {"label": "RS Daily above threshold", "kind": "threshold_above", "field": "rs_daily"},
        "rs_weekly_below": {"label": "RS Weekly below threshold", "kind": "threshold_below", "field": "rs_weekly"},
        "rs_weekly_above": {"label": "RS Weekly above threshold", "kind": "threshold_above", "field": "rs_weekly"},
        "rs_monthly_below": {"label": "RS Monthly below threshold", "kind": "threshold_below", "field": "rs_monthly"},
        "rs_monthly_above": {"label": "RS Monthly above threshold", "kind": "threshold_above", "field": "rs_monthly"},
        "vstop_weekly_flip": {"label": "VStop Weekly flips (trend change)", "kind": "cross", "field": "vstop_weekly_flipped"},
    }


# Backwards-compatible module-level snapshot (import-time settings). Prefer
# build_metrics(settings) in new code so labels always reflect live settings.
METRICS = build_metrics()


def load_rules():
    if not os.path.exists(RULES_FILE):
        return []
    with open(RULES_FILE) as f:
        return json.load(f)


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
    a GitHub Actions cron job), then discord_config.json (local Streamlit
    runs). The Streamlit app itself additionally checks st.secrets first
    (see app.py's get_discord_webhook)."""
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


def _applicable_tickers(rule, all_tickers):
    if rule["scope"] == "ALL":
        return all_tickers
    return [rule["scope"]] if rule["scope"] in all_tickers else []


def _condition_now(row, metric_def, threshold):
    kind = metric_def["kind"]
    field = metric_def["field"]
    val = row.get(field)
    if val is None:
        return False, val
    if kind == "cross":
        return bool(val), val
    if kind == "threshold_below":
        return (val < threshold), val
    if kind == "threshold_above":
        return (val > threshold), val
    return False, val


def preview_rules(rules, snapshot_results, metrics=None):
    """Returns list of dicts describing current truth value of every
    rule x ticker combo, WITHOUT touching alert_state.json. Used by the
    Streamlit app to show 'what would fire right now'."""
    metrics = metrics if metrics is not None else METRICS
    by_ticker = {r["ticker"]: r for r in snapshot_results}
    all_tickers = list(by_ticker.keys())
    out = []
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        metric_def = metrics.get(rule["metric"])
        if not metric_def:
            continue
        for ticker in _applicable_tickers(rule, all_tickers):
            row = by_ticker.get(ticker)
            if not row:
                continue
            is_true, val = _condition_now(row, metric_def, rule.get("threshold"))
            out.append({
                "rule_id": rule["id"], "ticker": ticker, "metric": rule["metric"],
                "label": metric_def["label"], "threshold": rule.get("threshold"),
                "value": val, "is_true_now": is_true,
            })
    return out


def evaluate_and_fire(rules, snapshot_results, state, metrics=None):
    """Edge-triggered evaluation used by the scheduled alert_check.py run.
    Returns (messages list, updated_state)."""
    metrics = metrics if metrics is not None else METRICS
    by_ticker = {r["ticker"]: r for r in snapshot_results}
    all_tickers = list(by_ticker.keys())
    today = date.today().isoformat()
    messages = []
    new_state = dict(state)

    for rule in rules:
        if not rule.get("enabled", True):
            continue
        metric_def = metrics.get(rule["metric"])
        if not metric_def:
            continue
        threshold = rule.get("threshold")

        for ticker in _applicable_tickers(rule, all_tickers):
            row = by_ticker.get(ticker)
            if not row:
                continue
            is_true, val = _condition_now(row, metric_def, threshold)
            key = f"{rule['id']}:{ticker}"
            prev = new_state.get(key, {"was_active": False, "last_triggered_date": None})

            if metric_def["kind"] == "cross":
                # Already an edge event from stock_data; just dedupe same-day reruns.
                if is_true and prev.get("last_triggered_date") != today:
                    messages.append(_format_message(ticker, metric_def["label"], val, threshold))
                    new_state[key] = {"was_active": True, "last_triggered_date": today}
                elif not is_true:
                    new_state[key] = {"was_active": False, "last_triggered_date": prev.get("last_triggered_date")}
                else:
                    new_state[key] = prev
            else:
                # Threshold metric: fire only on transition into the condition.
                if is_true and not prev.get("was_active"):
                    messages.append(_format_message(ticker, metric_def["label"], val, threshold))
                    new_state[key] = {"was_active": True, "last_triggered_date": today}
                else:
                    new_state[key] = {"was_active": is_true, "last_triggered_date": prev.get("last_triggered_date")}

    return messages, new_state


def _format_message(ticker, label, val, threshold):
    if threshold is not None:
        return f"**{ticker}** — {label} (value: {val}, threshold: {threshold})"
    return f"**{ticker}** — {label} (value: {val})"


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
