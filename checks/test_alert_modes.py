"""Per-rule notify_mode: incremental (edge-triggered) vs full (everything matching).

Drives alerts.evaluate_and_fire over three consecutive runs with a stub state and
asserts exactly the behaviour the modes promise:

    incremental   ping [ZED.NS] -> SILENT -> ping [ACME]
    full          ping [ZED.NS] -> ping [ZED.NS] -> ping [ZED.NS, ACME]

Offline: invented tickers, no data files, no network. build_discord_messages_for_rule
is stubbed to record (rule_id, tickers) -- which IS the contract under test, since
what differs between the modes is WHICH tickers a rule reports, not how the table
is formatted. One case at the end renders a real table to prove the full list
actually reaches the message.
"""

import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO)

import alerts

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- harness --------------------------------------------------------------
# Rows carry one metric; a rule matches when rsi > 50. Scope "ALL" so
# _applicable_tickers never needs the markets registry (no data files).
COND = [{"metric_a": "rsi14_daily", "operator": ">", "compare_type": "value", "value": 50}]


def rule(rule_id, mode=None):
    r = {"id": rule_id, "name": rule_id, "scope": "ALL", "conditions": COND, "enabled": True}
    if mode is not None:
        r["notify_mode"] = mode
    return alerts.normalize_rule(r)


def rows(**rsi):
    return [{"ticker": t, "rsi14_daily": v} for t, v in rsi.items()]


_sent = []
alerts.build_discord_messages_for_rule = (
    lambda rule, tickers, by_ticker, labels, limit=1900:
    _sent.append((rule["id"], sorted(tickers))) or [f"{rule['id']}: {sorted(tickers)}"]
)


def run_sequence(r, snapshots):
    """Feed successive snapshots through evaluate_and_fire, carrying state
    forward exactly as alert_check.py does. Returns the reported tickers per run."""
    state, out = {}, []
    for snap in snapshots:
        _sent.clear()
        _msgs, state = alerts.evaluate_and_fire([r], snap, state)
        out.append(sorted(t for _rid, ts in _sent for t in ts))
    return out, state


# Day 1: ZED.NS matches. Day 2: ZED.NS still matches, nothing new.
# Day 3: ACME starts matching too.
SEQ = [rows(**{"ZED.NS": 60, "ACME": 10}),
       rows(**{"ZED.NS": 60, "ACME": 10}),
       rows(**{"ZED.NS": 60, "ACME": 70})]

inc, inc_state = run_sequence(rule("r_inc", "incremental"), SEQ)
check(inc == [["ZED.NS"], [], ["ACME"]],
      f"incremental: new -> silent -> only the new one  (got {inc})")

full, full_state = run_sequence(rule("r_full", "full"), SEQ)
check(full == [["ZED.NS"], ["ZED.NS"], ["ACME", "ZED.NS"]],
      f"full: every match, every run  (got {full})")

# The default, and the migration path: a rule saved before this key existed.
legacy, _ = run_sequence(rule("r_legacy"), SEQ)
check(legacy == inc, "a rule with no notify_mode key behaves exactly like incremental")
check(alerts.notify_mode({"id": "x"}) == "incremental", "missing notify_mode reads as incremental")
check(alerts.notify_mode({"notify_mode": "FULL"}) == "incremental",
      "an unrecognised notify_mode falls back to incremental, the quieter mode")

# A full rule is not an unconditional ping: nothing matching means nothing sent.
quiet, _ = run_sequence(rule("r_quiet", "full"), [rows(**{"ZED.NS": 10, "ACME": 10})])
check(quiet == [[]], f"full stays silent when nothing matches  (got {quiet})")

# Both modes keep the edge state current, so flipping full -> incremental does
# not re-announce what was already active.
check(full_state["r_full:ZED.NS"]["was_active"] is True,
      "a full rule still writes was_active (flipping back to incremental won't re-flood)")
check(full_state["r_full:ACME"]["was_active"] is True, "full rule records every matching ticker")
check(inc_state["r_inc:ZED.NS"]["was_active"] is True, "incremental state unchanged by this feature")

# last_triggered_date tracks when a pair was last ANNOUNCED.
after_silent, st_mid = run_sequence(rule("r_inc2", "incremental"), SEQ[:2])
check(st_mid["r_inc2:ZED.NS"]["last_triggered_date"] is not None,
      "incremental keeps the date from the run that announced it")

# A ticker that stops matching and later matches again re-fires in BOTH modes.
again = [rows(**{"ZED.NS": 60}), rows(**{"ZED.NS": 10}), rows(**{"ZED.NS": 60})]
re_inc, _ = run_sequence(rule("r_re", "incremental"), again)
check(re_inc == [["ZED.NS"], [], ["ZED.NS"]], f"incremental re-fires after dropping out  (got {re_inc})")

# Disabled and condition-less rules send nothing, whatever the mode.
off = alerts.normalize_rule({"id": "r_off", "scope": "ALL", "conditions": COND,
                             "enabled": False, "notify_mode": "full"})
check(run_sequence(off, SEQ)[0] == [[], [], []], "a disabled full rule sends nothing")

# --- the real message actually carries the full list ----------------------
real_builder = alerts.__dict__["build_discord_messages_for_rule"]
import importlib
alerts_fresh = importlib.reload(alerts)
import stock_data
stock_data.load_markets_registry = lambda: {"m1": {"label": "Demo", "benchmark": "SPY"}}
snap = [dict(r, market="m1") for r in SEQ[2]]
msgs = alerts_fresh.build_discord_messages_for_rule(
    rule("r_msg", "full"), ["ZED.NS", "ACME"], {r["ticker"]: r for r in snap},
    {"rsi14_daily": "RSI-D"})
body = "\n".join(msgs)
check(bool(msgs) and "ZED.NS" in body and "ACME" in body,
      "a full rule's Discord table lists every matching ticker, not just the new one")
check("RSI-D" in body, "the table still uses metric LABELS, not raw keys")


# --- alert_check: a full rule is excluded from the send-failure rollback ---
# When Discord can't be reached, alert_check rolls newly-triggered keys back to
# was_active=False so the next run retries them. A full rule re-sends everything
# matching anyway, so rolling it back would buy no retry -- it would only leave
# was_active=False on a ticker that IS active, which would re-announce if the
# rule were later switched to incremental.
import alert_check

SCHED = {"type": "scheduled", "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
         "time_et": "21:00"}


def rollback_state_for(mode):
    r = alerts.normalize_rule({"id": "rb", "name": "rb", "scope": "ALL", "conditions": COND,
                               "enabled": True, "schedule": dict(SCHED), "notify_mode": mode})
    saved = {k: getattr(alert_check, k) for k in
             ("load_rules", "is_rule_due", "fetch_all_markets", "load_data_snapshot",
              "load_watchlists", "load_state_status", "save_state", "load_discord_webhook")}
    captured = {}
    try:
        snap = [dict(x, market="m1") for x in rows(**{"ZED.NS": 60})]
        alert_check.load_rules = lambda: [r]
        alert_check.is_rule_due = lambda *a, **kw: True
        alert_check.fetch_all_markets = lambda **kw: (snap, "now", {"m1": snap})
        alert_check.load_data_snapshot = lambda: {"per_market": {}, "short_history": {}}
        alert_check.load_watchlists = lambda: {"m1": ["ZED.NS"]}
        alert_check.load_state_status = lambda: ({}, True)   # trusted + empty -> not seeding
        alert_check.save_state = lambda s: captured.update(s)
        alert_check.load_discord_webhook = lambda: None      # nothing was sent -> rollback path
        alert_check.main()
    finally:
        for k, v in saved.items():
            setattr(alert_check, k, v)
    return captured


inc_rb = rollback_state_for("incremental")
full_rb = rollback_state_for("full")
check(inc_rb.get("rb:ZED.NS", {}).get("was_active") is False,
      "incremental: an undelivered alert is rolled back so the next run retries it")
check(full_rb.get("rb:ZED.NS", {}).get("was_active") is True,
      "full: state is NOT rolled back -- it re-sends next run regardless")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
