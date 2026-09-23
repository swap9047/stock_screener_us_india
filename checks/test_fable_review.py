"""Regression checks for the 2026-09-17 evening review (docs/fable_review_091726.md).

One check per finding, each written before the fix and watched failing:

  F1  save_settings reset every key the Settings dialog does not show.
  F2  is_rule_due judged the run's weekday, not the slot's (a Saturday slot on
      time was "not due"; a Monday slot 11 h late was "not due").
  F3  the stale-ticker caption read an age computed at fetch time.
  F4  a list value with a non-"in" operator raised out of passes_filter.
  F5  wrap-up tenure floored to 0 across a Sunday/Monday run-date boundary.
  F6  the Expert/Sentiment search stages never retried the primary model.
  F7  the breadth job's index scrapes had no request timeout.
  F8  a hung yfinance call left a non-daemon worker for atexit to join.
  F9  the dashboard's bulk re-analyze kept a stale prior view forever.
  F10 a deselected Match-mode control silently meant AND.
  F11 two app actions pushed the whole snapshot without refreshing it first.
  F13 the judging jobs evaluated substituted rows with fetch-time enrichment.
  F14 add_watchlist could mint the combined tabs' reserved keys.

F12 (the wrap-up Send button) needs a rendered app and real data, so it lives
with the other AppTest scripts outside the repo. Offline: invented tickers, temp
files, stubbed clients, no network.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ast
import json
import os
import re
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def redirect(pairs):
    """[(module, attr, path)] -> restore callable."""
    saved = [(m, a, getattr(m, a)) for m, a, _ in pairs]
    for m, a, p in pairs:
        setattr(m, a, p)
    return lambda: [setattr(m, a, v) for m, a, v in saved]


ET = ZoneInfo("America/New_York")

# --- F1 save_settings keeps what the caller did not mention --------------------
import stock_data as sd

d = tempfile.mkdtemp()
restore = redirect([(sd, "SETTINGS_FILE", os.path.join(d, "settings.json"))])
try:
    full = dict(sd.DEFAULT_SETTINGS)
    full["news_search_model"] = "models/gemma-4-31b-it"
    full["show_fundamental_columns"] = False
    json.dump(full, open(sd.SETTINGS_FILE, "w"))
    sd.save_settings({"rsi_period": 9})          # what the Settings dialog does: a subset
    back = sd.load_settings()
    check(back["rsi_period"] == 9, "F1: a subset save applies the keys it names")
    check(back["news_search_model"] == "models/gemma-4-31b-it"
          and back["show_fundamental_columns"] is False,
          "F1: ...and leaves the keys it does not name as they were on disk")
finally:
    restore()

# --- F2 is_rule_due judges the SLOT's weekday --------------------------------
from alerts import is_rule_due

sat = {"schedule": {"type": "scheduled", "days": ["SAT"], "time_et": "21:00"}}
mon = {"schedule": {"type": "scheduled", "days": ["MON"], "time_et": "21:00"}}
weekdays = {"schedule": {"type": "scheduled", "days": ["MON", "TUE", "WED", "THU", "FRI"], "time_et": "21:00"}}
check(is_rule_due(sat, datetime(2026, 9, 19, 22, 0, tzinfo=ET)), "F2: SAT rule, Saturday slot on time -> due")
check(is_rule_due(sat, datetime(2026, 9, 20, 9, 0, tzinfo=ET)), "F2: SAT rule, Saturday slot 12h late -> due")
check(is_rule_due(mon, datetime(2026, 9, 22, 8, 0, tzinfo=ET)), "F2: MON rule, Monday slot 11h late -> due")
check(not is_rule_due(mon, datetime(2026, 9, 22, 21, 30, tzinfo=ET)), "F2: MON rule, Tuesday slot -> not due")
check(not is_rule_due(weekdays, datetime(2026, 9, 21, 10, 0, tzinfo=ET)),
      "F2: Mon-Fri rule at Monday 10:00 is judging SUNDAY's slot -> not due")
check(is_rule_due(weekdays, datetime(2026, 9, 19, 7, 0, tzinfo=ET)), "F2: Mon-Fri rule, Friday slot on Saturday morning -> due")
check(not is_rule_due({"schedule": {"type": "none"}}, datetime(2026, 9, 21, 21, 30, tzinfo=ET)), "F2: scan-only never due")

# --- F3 the stale-ticker age is recomputed at read time ----------------------
rows = [{"ticker": "ACME", "data_end": "2026-09-05", "data_end_age_days": 0},
        {"ticker": "ZED.NS", "data_end": "bad", "data_end_age_days": 0}]
try:
    sd.refresh_data_end_age(rows, today=date(2026, 9, 15))
    check(rows[0]["data_end_age_days"] == 10, "F3: age recomputed from data_end (0 -> 10)")
    # The value this writes feeds `r.get("data_end_age_days", 0) >= 3` in app.py's
    # stale-ticker caption, and .get's default does NOT cover a key that is
    # present and None -- so writing None for an unparseable data_end raised
    # TypeError out of render_market_tab, i.e. took every tab down. There is
    # nothing to recompute for such a row: leave whatever it already carried.
    check(rows[1]["data_end_age_days"] == 0, "F3: unparseable data_end leaves the stored age alone")
    for _r in rows:
        check(isinstance(_r.get("data_end_age_days", 0), int),
              f"F3: {_r['ticker']} age stays int, so the caption's >= 3 test cannot raise")
except AttributeError as e:
    check(False, f"F3: refresh_data_end_age exists ({e})")

# --- F4 a list value with a non-'in' operator fails closed --------------------
from filters import passes_filter

row = {"ticker": "ACME", "rsi14_daily": 55.0}
for op in (">", "==", "<="):
    cond = {"metric_a": "rsi14_daily", "operator": op, "compare_type": "value", "value": ["Yes"]}
    try:
        check(passes_filter(row, cond) is False, f"F4: list value with '{op}' -> False")
    except TypeError as e:
        check(False, f"F4: list value with '{op}' raised {e}")
check(passes_filter(row, {"metric_a": "rsi14_daily", "operator": "in", "compare_type": "value", "value": ["55"]}),
      "F4: ...while 'in' with a list still matches")

# --- F5 wrap-up tenure survives a Sunday/Monday stamp swap -------------------
from weekly_wrapup import _weeks_since

check(_weeks_since("2026-09-14", date(2026, 9, 20)) == 1, "F5: entered on a Monday-stamped run, read on a Sunday one -> 1 week")
check(_weeks_since("2026-09-13", date(2026, 9, 21)) == 1, "F5: entered Sunday, read Monday -> 1 week")
check(_weeks_since("2026-09-13", date(2026, 9, 13)) == 0, "F5: same run -> 0")
check(_weeks_since("2026-09-06", date(2026, 9, 20)) == 2, "F5: two weeks -> 2")

# --- F6 the search stages retry the primary model before conceding ----------
import llm_util
import expert_views
import fundamentals_eval


class _Ladder:
    """Fails the first N calls with a retryable error, then answers."""
    def __init__(self, fail_first):
        self.calls, self.fail_first = [], fail_first
        parent = self

        class models:
            @staticmethod
            def generate_content(model, contents, config):
                parent.calls.append(model)
                if len(parent.calls) <= parent.fail_first:
                    raise Exception("503 UNAVAILABLE: overloaded")
                return type("R", (), {"text": "some news"})()
        self.models = models


saved_backoff = llm_util.RETRY_BACKOFF_SECONDS
llm_util.RETRY_BACKOFF_SECONDS = 0
try:
    for fn, label in ((expert_views.fetch_gemma_expert_news, "expert"),
                      (fundamentals_eval.fetch_fundamental_news, "sentiment")):
        c = _Ladder(fail_first=1)
        text, source = fn(c, "ACME", "us_picks", "Acme Corp")
        check(c.calls == ["models/gemma-4-26b-a4b-it"] * 2 and text == "some news" and "26B" in source,
              f"F6 {label}: one transient failure -> primary retried, not demoted ({c.calls})")
        c = _Ladder(fail_first=2)
        try:
            text, source = fn(c, "ACME", "us_picks", "Acme Corp")
            # Two failures used to demote to 31b on the third rung. That model
            # answered 0 of ~63 calls across four runs, so the ladder is three
            # attempts on 26b now, each on a different API key -- the third rung
            # still has to ANSWER, which is what this check is really about.
            check(c.calls == ["models/gemma-4-26b-a4b-it"] * 3 and "26B" in source,
                  f"F6 {label}: two failures -> a third attempt on 26b, still answered ({c.calls})")
        except TimeoutError:
            check(False, f"F6 {label}: two failures exhausted the ladder instead of reaching 31b ({c.calls})")
finally:
    llm_util.RETRY_BACKOFF_SECONDS = saved_backoff

# --- F7 every requests.get in the breadth job has a timeout -------------------
tree = ast.parse((REPO / "refresh_market_breadth.py").read_text())
gets = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get" and isinstance(n.func.value, ast.Name) and n.func.value.id == "requests"]
check(gets and all(any(k.arg == "timeout" for k in n.keywords) for n in gets),
      f"F7: {len(gets)} requests.get call(s) in refresh_market_breadth.py all pass timeout=")


# --- F8 a hung yfinance call leaves no non-daemon thread behind ---------------
def _stray_workers():
    return [t.name for t in threading.enumerate()
            if t is not threading.main_thread() and t.is_alive() and not t.daemon]


saved_dl = sd.yf.download
sd.yf.download = lambda *a, **kw: time.sleep(3)
try:
    t0 = time.time()
    try:
        sd._download_with_retries(["ACME"], "5y", attempts=1, timeout=0.3, wait=0)
        check(False, "F8: _download_with_retries raised on timeout")
    except TimeoutError:
        check(time.time() - t0 < 2, "F8: _download_with_retries returns at its timeout")
    check(not _stray_workers(), f"F8: ...and leaves no non-daemon worker behind ({_stray_workers()})")
finally:
    sd.yf.download = saved_dl


class _SlowTicker:
    def __init__(self, *a, **kw):
        pass

    def get_earnings_dates(self, limit=8):
        time.sleep(3)


saved_tk = fundamentals_eval.yf.Ticker
fundamentals_eval.yf.Ticker = _SlowTicker
try:
    try:
        t0 = time.time()
        got = fundamentals_eval._fetch_last_reported_earnings_date("ACME", timeout=0.3)
        check(got is None and time.time() - t0 < 2, "F8: earnings-date lookup fails open at its timeout")
        check(not _stray_workers(), f"F8: ...and leaves no non-daemon worker behind ({_stray_workers()})")
    except TypeError as e:
        check(False, f"F8: _fetch_last_reported_earnings_date takes a timeout ({e})")
finally:
    fundamentals_eval.yf.Ticker = saved_tk

# --- F9 bulk re-analyze applies the same persistence rules as the batch ------
# Relative to now, not a fixed date: "fresh" has to stay inside EXPERT_STALE_DAYS
# on whatever day this runs. It was pinned to 2026-09-17 and silently turned
# stale on 2026-09-22, failing CI with no code change at all.
_fresh_stamp = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
valid = {"verdict": "HOLD", "headline": "fine", "model_used": "m", "as_of": _fresh_stamp}
failed = {"verdict": "HOLD", "headline": "Analysis pending -- x", "model_used": "Error", "as_of": _fresh_stamp}
stale_prior = dict(valid, as_of="2026-01-01 00:00")
try:
    store = {"ACME": dict(stale_prior)}
    changed = expert_views.apply_regenerated_view(store, "ACME", dict(failed))
    check(changed and expert_views.is_pending_view(store["ACME"]) and "days old" in store["ACME"]["headline"],
          "F9: failed regeneration over a STALE prior -> honest pending placeholder")
    store = {"ACME": dict(valid)}
    changed = expert_views.apply_regenerated_view(store, "ACME", dict(failed))
    check(not changed and store["ACME"] == valid, "F9: failed regeneration over a fresh prior -> prior kept")
    store = {}
    changed = expert_views.apply_regenerated_view(store, "ACME", dict(valid))
    check(changed and store["ACME"] == valid, "F9: a valid view is written")
except AttributeError as e:
    check(False, f"F9: expert_views.apply_regenerated_view exists ({e})")

app_src = (REPO / "app.py").read_text()
check("apply_regenerated_view(" in app_src.split("def _reanalyze_tickers_in_dashboard")[1].split("\ndef ")[0],
      "F9: _reanalyze_tickers_in_dashboard goes through apply_regenerated_view")

# --- F10 a deselected Match mode reads as OR ---------------------------------
check(re.search(r'scan_mode\s*=\s*\(?\s*scan_mode or "OR"', app_src) is not None,
      'F10: scan_mode falls back to "OR" when the control is deselected (None)')

# --- F11 the snapshot is refreshed from the data repo before the app rebuilds on it
import github_sync as gs
from fake_github import FakeGitHub, use

d = tempfile.mkdtemp()
restore = redirect([(gs, "SCRIPT_DIR", d), (gs, "SYNC_STATE_FILE", os.path.join(d, ".s.json"))])
try:
    local = {"generated_at": "2026-09-16T10:00:00+00:00", "per_market": {"m": [{"ticker": "ACME", "last_close": 1}]}}
    remote_newer = {"generated_at": "2026-09-16T12:00:00+00:00", "per_market": {"m": [{"ticker": "ACME", "last_close": 2}]}}
    remote_older = {"generated_at": "2026-09-16T08:00:00+00:00", "per_market": {"m": [{"ticker": "ACME", "last_close": 0}]}}
    for remote, expect, label in ((remote_newer, 2, "newer remote replaces the local copy"),
                                  (remote_older, 1, "older remote leaves the local copy alone")):
        json.dump(local, open(os.path.join(d, "data_snapshot.json"), "w"))
        json.dump({"checked_at": 9e18, "blobs": {}}, open(gs.SYNC_STATE_FILE, "w"))  # "just checked": must not matter
        use(FakeGitHub({"data_snapshot.json": remote}))
        try:
            gs.refresh_snapshot_from_repo("t", "o/r", "main")
            got = json.load(open(os.path.join(d, "data_snapshot.json")))["per_market"]["m"][0]["last_close"]
            check(got == expect, f"F11: {label}")
        except AttributeError as e:
            check(False, f"F11: github_sync.refresh_snapshot_from_repo exists ({e})")
finally:
    restore()

for fn_name in ("_apply_watchlist_tickers", 'sb1.button("Refresh Data"'):
    body = app_src.split(fn_name, 1)[1]
    first_load = body.find("load_data_snapshot()")
    first_refresh = body.find("refresh_snapshot_from_repo(")
    check(0 <= first_refresh < first_load,
          f"F11: {fn_name.split('(')[0]} pulls the data repo's snapshot before reading the local one")

# --- F13 substituted rows are re-enriched before they are judged ------------
import alert_check
import weekly_wrapup_check
import refresh_data
import ticker_notes as tn
import custom_columns as cc

d = tempfile.mkdtemp()
restore = redirect([
    (tn, "TICKER_NOTES_FILE", os.path.join(d, "ticker_notes.json")),
    (expert_views, "EXPERT_VIEWS_FILE", os.path.join(d, "expert_views.json")),
    (fundamentals_eval, "FUNDAMENTALS_FILE", os.path.join(d, "fundamentals.json")),
    (sd, "INTERESTED_FILE", os.path.join(d, "interested.json")),
    (cc, "CUSTOM_COLUMNS_FILE", os.path.join(d, "custom_columns.json")),
])
try:
    json.dump({"ACME": {"note": "watch", "flag": "Red"}}, open(tn.TICKER_NOTES_FILE, "w"))
    json.dump(["ACME"], open(sd.INTERESTED_FILE, "w"))
    base = {"company_name": "Acme", "last_close": 1.0, "trend": "Uptrend", "tech_uptrend": 1}
    # the stored row: newer data_end, but enrichment frozen at ITS fetch
    # Relative dates for the same reason as F9: reject_stale_rows only holds a
    # stored row while it is under max_hold_days old, so a pinned 2026-09-16
    # stopped being substituted on 2026-09-22. Yesterday and the day before are
    # never the still-forming session, which completed_sessions_only would reject.
    _today_et = datetime.now(ZoneInfo("America/New_York")).date()
    STORED_END = (_today_et - timedelta(days=1)).isoformat()
    FRESH_END = (_today_et - timedelta(days=2)).isoformat()
    stored = {**base, "ticker": "ACME", "data_end": STORED_END, "flag": "Green", "note": "", "interested": False,
              "expert_take": "Accumulate", "sentiment": "Positive", "market": "us_picks"}
    fresh = {**base, "ticker": "ACME", "data_end": FRESH_END, "flag": "Red", "note": "watch", "interested": True,
             "expert_take": "Pending", "sentiment": "Unknown", "market": "us_picks"}
    WL = {"us_picks": ["ACME"]}
    RULE = {"id": "r1", "name": "r", "enabled": True, "scope": "ALL", "notify_mode": "incremental",
            "conditions": [{"metric_a": "flag", "operator": "in", "compare_type": "value", "value": ["Red"], "logic": "AND"}],
            "schedule": {"type": "scheduled", "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], "time_et": "21:00"}}

    def fetch(*a, **kw):
        per = {"us_picks": [dict(fresh)]}
        return [r for rows in per.values() for r in rows], "2026-09-16 22:00 ET", per

    def run(mod, capture_name, capture):
        names = ("fetch_all_markets", "load_watchlists", "load_data_snapshot", "load_rules", "is_rule_due",
                 "load_state_status", "save_state", "send_discord_batch", "load_wrapup_state",
                 "save_wrapup_state", "save_data_snapshot", capture_name)
        saved = {k: getattr(mod, k) for k in names if hasattr(mod, k)}
        try:
            mod.fetch_all_markets = fetch
            mod.load_watchlists = lambda: WL
            mod.load_data_snapshot = lambda: {"per_market": {"us_picks": [dict(stored)]}, "short_history": {}}
            if hasattr(mod, "load_rules"):
                mod.load_rules = lambda: [dict(RULE)]
            if hasattr(mod, "is_rule_due"):
                mod.is_rule_due = lambda *a, **kw: True
            if hasattr(mod, "load_state_status"):
                mod.load_state_status = lambda: ({}, True)
                mod.save_state = lambda s: None
            if hasattr(mod, "load_wrapup_state"):
                mod.load_wrapup_state = lambda: {"last_run": None, "entries": {}}
                mod.save_wrapup_state = lambda s: None
            if hasattr(mod, "send_discord_batch"):
                mod.send_discord_batch = lambda *a, **kw: (True, "")
            setattr(mod, capture_name, capture)
            mod.main()
        finally:
            for k, v in saved.items():
                setattr(mod, k, v)

    got = {}

    def cap_alerts(all_rules, rows, state, **kw):
        got["rows"] = rows
        return [], dict(state)

    run(alert_check, "evaluate_and_fire", cap_alerts)
    r = got["rows"][0]
    check(r["data_end"] == STORED_END, "F13 alert_check: the stored (newer) row was substituted")
    check(r["flag"] == "Red" and r["note"] == "watch" and r["interested"] is True and r["expert_take"] == "Pending",
          f"F13 alert_check: ...and re-enriched from the files before judging ({r['flag']}, {r['note']!r}, {r['interested']}, {r['expert_take']})")

    def cap_wrapup(rules, rows, state, **kw):
        got["rows"] = rows
        return {"run_date": "2026-09-20", "as_of": None, "state_last_run": None, "alerts": [],
                "rollup": [], "cycle_ids": set(), "total_stocks": 0}

    run(weekly_wrapup_check, "build_wrapup", cap_wrapup)
    r = got["rows"][0]
    check(r["flag"] == "Red" and r["interested"] is True,
          f"F13 weekly_wrapup_check: substituted row re-enriched before the digest ({r['flag']}, {r['interested']})")

    def cap_save(as_of, per_market, **kw):
        got["rows"] = per_market["us_picks"]
        return "stamp"

    run(refresh_data, "save_data_snapshot", cap_save)
    r = got["rows"][0]
    check(r["flag"] == "Red" and r["note"] == "watch",
          f"F13 refresh_data: a row kept from the previous snapshot is re-enriched before it is saved ({r['flag']}, {r['note']!r})")
finally:
    restore()

# --- F14 add_watchlist never mints a combined-tab key -------------------------
import filters

d = tempfile.mkdtemp()
restore = redirect([
    (sd, "MARKETS_FILE", os.path.join(d, "markets.json")),
    (sd, "WATCHLIST_FILE", os.path.join(d, "watchlist.json")),
    (sd, "SETTINGS_FILE", os.path.join(d, "settings.json")),
    (filters, "CUSTOM_FILTERS_FILE", os.path.join(d, "custom_filters.json")),
])
try:
    json.dump({"us_picks": {"label": "US Picks", "benchmark": "SPY"}}, open(sd.MARKETS_FILE, "w"))
    keys = [sd.add_watchlist(lbl, "SPY") for lbl in ("All Invested", "All Watchlist", "ALL")]
    reserved = set(sd.DEFAULT_WATCHLIST_GROUPS) | {"all"}
    check(not (set(keys) & reserved), f"F14: reserved labels get a different key ({keys})")
    check(len(set(keys)) == 3 and all(k in sd.load_markets_registry() for k in keys), "F14: ...and each is registered once")
finally:
    restore()

# --- Review of the fixes above turned up three more -------------------------
# R1 the two APP paths that substitute stored rows and then SAVE + PUSH the
#    snapshot were left out of F13's re-enrichment, so the pushed file carries
#    frozen flags/notes/AI fields -- which refresh_expert_views.py reads
#    straight into its prompt.
# R2 refresh_snapshot_from_repo stamped the shared "checked recently" clock, so
#    the regular pull skipped every OTHER generated file for a whole interval,
#    right after the user asked the app to freshen itself.
# R3 the TimeoutError re-wrap relabelled a foreign timeout (socket.timeout IS
#    TimeoutError since 3.10) as "timed out after <the full budget>" and dropped
#    its real message -- the ladder logs are how the nightly runs get diagnosed.

for _site, _guard in (("_apply_watchlist_tickers", "rebuild_snapshot_for_market("),
                      ('sb1.button("Refresh Data"', "fill_snapshot_gaps(")):
    _body = app_src.split(_site, 1)[1].split("_persist_and_serve(", 1)[0]
    check("enrich_rows(" in _body and _body.find(_guard) < _body.find("enrich_rows("),
          f"R1: {_site.split('(')[0]} re-enriches substituted rows before it saves and pushes them")

d = tempfile.mkdtemp()
restore = redirect([(gs, "SCRIPT_DIR", d), (gs, "SYNC_STATE_FILE", os.path.join(d, ".s.json"))])
try:
    json.dump({"generated_at": "2026-09-16T10:00:00+00:00"}, open(os.path.join(d, "data_snapshot.json"), "w"))
    json.dump({"checked_at": 1000.0, "blobs": {}}, open(gs.SYNC_STATE_FILE, "w"))
    use(FakeGitHub({"data_snapshot.json": {"generated_at": "2026-09-16T12:00:00+00:00"}}))
    gs.refresh_snapshot_from_repo("t", "o/r", "main")
    _state = json.load(open(gs.SYNC_STATE_FILE))
    check(_state.get("checked_at") == 1000.0,
          "R2: a one-file force pull leaves the shared pull clock alone")
    check(_state.get("blobs", {}).get("data_snapshot.json"),
          "R2: ...while still recording that file's blob, so it is not re-downloaded")
finally:
    restore()

import llm_util as _lu


class _RaisingClient:
    class models:
        @staticmethod
        def generate_content(model=None, contents=None, config=None):
            raise TimeoutError("read timed out after 2s")


try:
    _lu.generate_with_timeout(_RaisingClient(), "models/x", "p", None, timeout=120)
    check(False, "R3: generate_with_timeout propagates the call's own TimeoutError")
except TimeoutError as e:
    check("read timed out after 2s" in str(e),
          f"R3: a timeout raised BY the call keeps its message (got {e!r})")

_saved_dl = sd.yf.download
try:
    def _boom(*a, **k):
        raise TimeoutError("yahoo read timed out after 2s")
    sd.yf.download = _boom
    try:
        sd._download_with_retries(["ACME"], "5y", attempts=1, timeout=90, wait=0)
        check(False, "R3: _download_with_retries raises when the call times out")
    except TimeoutError as e:
        check("yahoo read timed out after 2s" in str(e),
              f"R3: ...and so does _download_with_retries (got {e!r})")
finally:
    sd.yf.download = _saved_dl

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
