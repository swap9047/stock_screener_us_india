"""The invariants that keep one bad file or one bad fetch from doing damage.

1. A malformed condition fails closed instead of raising through every tab and
   the nightly job (filters.passes_filter).
2. A corrupt USER-DATA file is loud (DataFileError naming it), never an empty
   default that the next save would push over the data repo; regenerable state
   still falls back to empty.
3. Every root-JSON writer is atomic, so a crash mid-write cannot leave a torn
   file behind.
4. A job that judges prices refuses a partial universe and lets the slot gate
   retry, but tolerates a few individual misses.

All fixtures are invented and every file path is redirected to a temp dir.
"""

import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO)

import json
import os
import tempfile

import alerts
import custom_columns as cc
import filters
import stock_data as sd
import ticker_notes as tn

fails = 0
def check(ok, label):
    global fails; fails += not ok; print("PASS" if ok else "FAIL", label)

# ------------------------------------------------------------------ 1. fail closed
row = {"ticker": "ACME", "rsi14_daily": 50.0, "trend": "Uptrend"}
good = {"metric_a": "rsi14_daily", "operator": ">", "compare_type": "value", "value": 1}
malformed = {
    "missing metric_a": {k: v for k, v in good.items() if k != "metric_a"},
    "missing operator": {k: v for k, v in good.items() if k != "operator"},
    "missing compare_type": {k: v for k, v in good.items() if k != "compare_type"},
    "unknown operator": {**good, "operator": "BAD"},
    "metric_a not a string": {**good, "metric_a": None},
    "metric compare with no metric_b": {"metric_a": "rsi14_daily", "operator": ">", "compare_type": "metric"},
}
for label, filt in malformed.items():
    try:
        got = filters.passes_filter(row, filt)
        check(got is False, f"{label} -> False, not a crash (got {got!r})")
    except Exception as e:
        check(False, f"{label} -> raised {type(e).__name__}: {e}")
check(filters.passes_filter(row, good) is True, "a valid condition still passes")
# "in" is implemented inline and is NOT a key in OPERATORS: the guard checks
# VALID_OPERATORS, or it silently rejects every categorical condition.
cat = {"metric_a": "trend", "operator": "in", "compare_type": "value", "value": ["Uptrend", "Strong Uptrend"]}
check(filters.passes_filter(row, cat) is True, "an 'in' condition on a categorical still matches")
check(filters.passes_filter({"trend": "Downtrend"}, cat) is False, "an 'in' condition still rejects a non-member")
try:
    filters.passes_filter_chain(row, [malformed["unknown operator"], good], {})
    check(True, "a chain containing a malformed condition still evaluates")
except Exception as e:
    check(False, f"chain raised {type(e).__name__}: {e}")
try:
    filters.describe_filter(malformed["missing compare_type"], {})
    check(True, "the description path (Discord builder) survives a malformed condition")
except Exception as e:
    check(False, f"describe_filter raised {type(e).__name__}: {e}")

# --------------------------------------------- 2. corrupt files: loud vs guarded
TORN = '{"a": [1, 2'          # what a crash mid-write leaves behind

def redirect(module, attr, content):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "f.json")
    open(p, "w").write(content)
    setattr(module, attr, p)
    return p

LOUD = [
    (sd, "WATCHLIST_FILE", lambda: sd.load_watchlists()),
    (sd, "MARKETS_FILE", lambda: sd.load_markets_registry()),
    (sd, "SETTINGS_FILE", lambda: sd.load_settings()),
    (sd, "INTERESTED_FILE", lambda: sd.load_interested()),
    (sd, "WATCHLIST_GROUPS_FILE", lambda: sd.load_watchlist_groups()),
    (sd, "TICKER_INDEX_FILE", lambda: sd.load_ticker_index()),
    (alerts, "RULES_FILE", lambda: alerts.load_rules()),
    (filters, "CUSTOM_FILTERS_FILE", lambda: filters.load_custom_filters()),
    (cc, "CUSTOM_COLUMNS_FILE", lambda: cc.load_custom_columns()),
    (tn, "TICKER_NOTES_FILE", lambda: tn.load_ticker_notes()),
]
for module, attr, call in LOUD:
    orig = getattr(module, attr)
    redirect(module, attr, TORN)
    try:
        call()
        check(False, f"corrupt {attr} returned a default silently")
    except sd.DataFileError as e:
        check("f.json" in str(e), f"corrupt {attr} -> DataFileError naming the file")
    except Exception as e:
        check(False, f"corrupt {attr} -> {type(e).__name__} instead of DataFileError")
    finally:
        setattr(module, attr, orig)

orig = alerts.STATE_FILE
redirect(alerts, "STATE_FILE", TORN)
try:
    check(alerts.load_state() == {}, "corrupt alert state -> empty, alerts still run (regenerable)")
finally:
    alerts.STATE_FILE = orig

# ------------------------------------------------------------- 3. atomic writes
SAVERS = [
    (sd, "WATCHLIST_FILE", lambda: sd.save_watchlists({"m": ["ACME"]})),
    (sd, "MARKETS_FILE", lambda: sd.save_markets_registry({"m": {"label": "M"}})),
    (sd, "SETTINGS_FILE", lambda: sd.save_settings({"x": 1})),
    (sd, "INTERESTED_FILE", lambda: sd.save_interested({"ACME"})),
    (sd, "WATCHLIST_GROUPS_FILE", lambda: sd.save_watchlist_groups({"g": []})),
    (sd, "TICKER_INDEX_FILE", lambda: sd.save_ticker_index({"ACME": {}})),
    (alerts, "RULES_FILE", lambda: alerts.save_rules([{"id": "r"}])),
    (alerts, "STATE_FILE", lambda: alerts.save_state({"k": {}})),
    (filters, "CUSTOM_FILTERS_FILE", lambda: filters.save_custom_filters({"m": []})),
    (cc, "CUSTOM_COLUMNS_FILE", lambda: cc.save_custom_columns([{"id": "c"}])),
    (tn, "TICKER_NOTES_FILE", lambda: tn.save_ticker_notes({"ACME": {"note": "n"}})),
    (sd, "DATA_SNAPSHOT_FILE", lambda: sd.save_data_snapshot("2026-09-16 10:00 ET", {"m": [{"ticker": "ACME"}]}, {})),
]
GOOD = json.dumps({"keep": "me"})
real_dump = json.dump
def exploding_dump(*a, **kw):
    raise RuntimeError("simulated crash mid-write")
for module, attr, call in SAVERS:
    orig = getattr(module, attr)
    p = redirect(module, attr, GOOD)
    try:
        json.dump = exploding_dump
        try:
            call()
        except RuntimeError:
            pass
        finally:
            json.dump = real_dump
        leftovers = [f for f in os.listdir(os.path.dirname(p)) if f.endswith(".tmp")]
        check(open(p).read() == GOOD, f"{attr}: a crash mid-write leaves the previous file intact")
        call()
        json.load(open(p))
        check(True, f"{attr}: a normal save writes valid JSON")
    finally:
        setattr(module, attr, orig)

# ------------------------------------------------- 4. no judging a partial universe
import alert_check
import weekly_wrapup_check

WL = {"us_picks": ["ACME", "WIDG", "ZED"], "in_picks": ["A&B.NS", "QQQ.NS"]}
def fetch(per_market=None, skip=None, too_new=None):
    def _fetch(*a, **kw):
        if skip and kw.get("skipped_groups") is not None:
            kw["skipped_groups"]["^BENCH"] = ["ACME", "WIDG"]
        if too_new and kw.get("short_history") is not None:
            kw["short_history"].update(too_new)
        per = per_market if per_market is not None else {}
        return [r for rows in per.values() for r in rows], "2026-09-16 10:00 ET", per
    return _fetch

def rows_for(markets):
    return {m: [{"ticker": t, "company_name": t, "last_close": 1.0} for t in tickers]
            for m, tickers in markets.items()}

# One enabled, scheduled rule, so the jobs reach the coverage guard instead of
# returning early. They must not depend on alerts_config.json existing: CI runs
# on a checkout with no data files at all.
RULE = {"id": "r1", "name": "check rule", "enabled": True, "scope": "ALL",
        "conditions": [{"metric_a": "last_close", "operator": ">", "compare_type": "value",
                        "value": 0, "logic": "AND"}],
        "schedule": {"type": "scheduled", "days": list("MON TUE WED THU FRI SAT SUN".split()),
                     "time_et": "21:00"},
        "weekly_wrapup": True}

STUBBED = ("fetch_all_markets", "send_discord_batch", "load_watchlists", "load_rules",
           "load_state_status", "save_state", "load_data_snapshot", "is_rule_due",
           "load_wrapup_state", "save_wrapup_state")


def run_job(mod, fetch_fn):
    """Runs main() with everything external stubbed; returns 'exit<N>' or 'returned'."""
    saved = {k: getattr(mod, k) for k in STUBBED if hasattr(mod, k)}
    sent = []
    try:
        mod.fetch_all_markets = fetch_fn
        mod.send_discord_batch = lambda *a, **kw: sent.append(1) or (True, "")
        mod.load_watchlists = lambda: WL
        mod.load_data_snapshot = lambda: {"short_history": {}}
        mod.load_rules = lambda: [dict(RULE)]
        if hasattr(mod, "is_rule_due"):
            mod.is_rule_due = lambda *a, **kw: True
        if hasattr(mod, "load_wrapup_state"):
            mod.load_wrapup_state = lambda: {"last_run": None, "entries": {}}
            mod.save_wrapup_state = lambda s: None
        # load_state_status, not load_state: alert_check asks WHY the state is
        # empty (missing/torn both mean "seed, send nothing"). Stubbing the old
        # name silently stopped applying when that changed, and the real
        # save_state then wrote alert_state.json into the repo -- clobbering the
        # live dedup state on a developer's machine. Stub what is actually called.
        if hasattr(mod, "load_state_status"):
            mod.load_state_status = lambda: ({"x": {"was_active": True, "last_triggered_date": "2026-09-01"}}, True)
            mod.save_state = lambda s: None
        try:
            mod.main()
            return "returned", sent
        except SystemExit as e:
            return f"exit{e.code}", sent
    finally:
        for k, v in saved.items():
            setattr(mod, k, v)

def _data_fingerprint():
    """(exists, mtime, size) for the state files the headless jobs can write."""
    out = {}
    for f in ("alert_state.json", "weekly_wrapup_state.json"):
        path = os.path.join(REPO, f)
        try:
            st = os.stat(path)
            out[f] = (True, st.st_mtime_ns, st.st_size)
        except OSError:
            out[f] = (False, None, None)
    return out


_before_fp = _data_fingerprint()

for mod, name in ((alert_check, "alert_check"), (weekly_wrapup_check, "weekly_wrapup_check")):
    outcome, sent = run_job(mod, fetch(rows_for({"us_picks": ["ACME"]}), skip=True))
    check(outcome == "exit1" and not sent, f"{name}: a skipped benchmark group -> exit 1, nothing sent ({outcome})")
    outcome, sent = run_job(mod, fetch(rows_for(WL)))
    check(outcome == "returned", f"{name}: a complete universe proceeds ({outcome})")
    # one ticker of five missing is 20%, above the 10% ceiling -> fail; as
    # "too new to compute" it is exempt instead
    partial = {"us_picks": ["ACME", "WIDG", "ZED"], "in_picks": ["A&B.NS"]}
    outcome, _ = run_job(mod, fetch(rows_for(partial)))
    check(outcome == "exit1", f"{name}: a 20% shortfall -> exit 1 ({outcome})")
    outcome, _ = run_job(mod, fetch(rows_for(partial), too_new={"QQQ.NS": {"bars": 12}}))
    check(outcome == "returned", f"{name}: the same gap, but too new to compute, proceeds ({outcome})")
    # A stub that stops applying is invisible -- renaming load_state to
    # load_state_status once left the real save_state wired up, and running the
    # checks then wrote alert_state.json into the repo, clobbering the live dedup
    # state on a developer's machine. Compare a fingerprint taken BEFORE the run:
    # these files legitimately exist on a machine that runs the app, so mere
    # existence proves nothing -- only that the run left them untouched does.
    check(_data_fingerprint() == _before_fp,
          f"{name}: leaves alert_state.json / weekly_wrapup_state.json untouched")

print("FAILURES:", fails); sys.exit(fails)
