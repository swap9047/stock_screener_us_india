"""The 2026-11-09 review's remaining findings, fixed 2026-11-16 (owner: "if it doesn't impact others").

  Y   Sentiment's Yahoo fallback keeps the sanity range the model's figures get
  C   a stored close carried into a series back-adjusted this run is put on the new scale
  W   the weekly wrap-up keeps a stock's "weeks in the list" when it had no row one Sunday
  N   the late-Stage notice goes out once per slot: after the rule check, never on a failed attempt
  D   a partly failed Discord send rolls back only the rules whose message failed

Each fix is additive: with no gap, nothing unjudged, no failure, the old result comes back unchanged
(the "unchanged" checks below). Offline, invented tickers.
"""

import math
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pandas as pd

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- Y: the Yahoo fallback's range ------------------------------------------------------
import fundamentals_eval as fe

yahoo = fe._yahoo_results_yoy
D = date(2026, 8, 10)
base = {"reported_qtr": "Q1 FY27"}
got = yahoo({**base, "qtr_profit_growth": 12825.0, "qtr_eps_growth": 3234.0, "qtr_revenue_growth": 19.0}, D)
check(got is not None and got["profit"] is None and got["revenue"][0] == 19.0,
      f"Y1: profit and EPS growth beyond the range are not readings; revenue still fills ({got})")
got = yahoo({**base, "qtr_profit_growth": 12825.0, "qtr_eps_growth": 140.0}, D)
check(got is not None and got["profit"][0] == 140.0 and got["profit"][2] == "EPS",
      f"Y2: an out-of-range profit figure falls back to an in-range EPS figure ({got})")
check(yahoo({**base, "qtr_profit_growth": 12825.0}, D) is None, "Y3: nothing in range -> no Yahoo figure at all")
got = yahoo({**base, "qtr_revenue_growth": 5000.0, "qtr_profit_growth": 30.0}, D)
check(got["revenue"] is None and got["profit"][0] == 30.0, f"Y4: revenue growth is held to the same range ({got})")
lo, hi = fe._PROFIT_YOY_RANGE
got = yahoo({**base, "qtr_profit_growth": hi, "qtr_eps_growth": hi, "qtr_revenue_growth": lo}, D)
check(got["profit"][0] == hi and got["revenue"][0] == lo, "Y5: the range's own ends are kept")
got = yahoo({**base, "qtr_profit_growth": 25.04, "qtr_eps_growth": 28.123, "qtr_revenue_growth": 19.456}, D)
check(got["profit"][0] == 25.04 and got["revenue"][0] == 19.456,
      f"Y6 unchanged: an ordinary figure is passed through exactly, not rounded ({got})")
check(yahoo({**base, "qtr_profit_growth": float("nan")}, D) is None, "Y7: a NaN is not a reading")

# --- C: carried closes across a gap adjusted this run --------------------------------------
import refresh_dashboard_perf as rdp

rescale = rdp.rescale_carried
RATIO = 41.4 / 454.55
# The download, already back-adjusted: Nov 3 is blank at Yahoo; the gap's ex-date is Nov 5.
new = {"2026-11-02": 40.9, "2026-11-04": 41.2, "2026-11-05": 41.4, "2026-11-06": 42.0}
gaps = [("2026-11-05", RATIO)]
old_scale = {**new, "2026-11-03": 452.0}            # stored by a run BEFORE the gap existed
merged = rdp.keep_recent_closes(new, old_scale, today=date(2026, 11, 6))
check(merged["2026-11-03"] == 452.0, "C fixture: the stored close is carried into the hole")
got = rescale(merged, new, gaps)
check(math.isclose(got["2026-11-03"], round(452.0 * RATIO, 4)) and 39 < got["2026-11-03"] < 43,
      f"C1: a close stored on the old scale is put on the adjusted one ({got['2026-11-03']})")
check({d: v for d, v in got.items() if d != "2026-11-03"} == new, "C1: no fresh close is touched")
check(merged["2026-11-03"] == 452.0, "C1: the input is not modified in place")
already = rdp.keep_recent_closes(new, {**new, "2026-11-03": 41.0}, today=date(2026, 11, 6))
check(rescale(already, new, gaps)["2026-11-03"] == 41.0,
      "C2: a close an earlier run had already adjusted is left alone")
after = {"2026-11-02": 40.9, "2026-11-04": 41.2, "2026-11-05": 41.4}
m2 = rdp.keep_recent_closes(after, {"2026-11-06": 42.0}, today=date(2026, 11, 6))
check(rescale(m2, after, gaps)["2026-11-06"] == 42.0, "C3: a carried day on or after the ex-date is never scaled")
check(rescale(merged, new, None) is merged and rescale(merged, new, []) is merged,
      "C4 unchanged: no gap this run -> the merged closes come back as they are")
check(rescale(new, new, gaps) is new, "C4 unchanged: nothing carried -> nothing to do")

# _download_series records the gaps it adjusted, per ticker, and forgets them when there are none.
n = 60
days = pd.bdate_range(end="2026-11-06", periods=n)
price = pd.Series([454.55] * (n - 2) + [41.4, 42.0], index=days)
orig_dl = rdp.yf.download
try:
    rdp.yf.download = lambda tickers, **k: pd.concat({"Close": pd.DataFrame({"ACME.NS": price, "ACME": price})}, axis=1)
    rdp.GAPS_FOUND.clear()
    rdp._download_series(["ACME.NS", "ACME"])
    found = dict(rdp.GAPS_FOUND)
    flat = pd.Series([100.0] * n, index=days)
    rdp.yf.download = lambda tickers, **k: pd.concat({"Close": pd.DataFrame({"ACME.NS": flat, "ACME": flat})}, axis=1)
    rdp._download_series(["ACME.NS", "ACME"])
    cleared = dict(rdp.GAPS_FOUND)
finally:
    rdp.yf.download = orig_dl
    rdp.GAPS_FOUND.clear()
check(list(found) == ["ACME.NS"] and found["ACME.NS"][0][0] == days[-2].strftime("%Y-%m-%d")
      and math.isclose(found["ACME.NS"][0][1], RATIO),
      f"C5: the Indian ticker's gap is recorded with its ex-date and ratio; the US one has none ({found})")
check(cleared == {}, "C5: a later download with no gap forgets it")
src = (REPO / "refresh_dashboard_perf.py").read_text()
check("rescale_carried(keep_recent_closes(closes, prev_block.get(col)), closes, GAPS_FOUND.get(col))" in src,
      "C6: main() rescales what it carries for each market's series")

# --- W: the wrap-up's tenure -----------------------------------------------------------------
import weekly_wrapup as ww

state = {"last_run": "2026-11-08", "entries": {
    "r1:ACME": {"entered": "2026-09-06"},       # matches again this week
    "r1:ZED.NS": {"entered": "2026-08-02"},     # no row this Sunday
    "r1:GONE": {"entered": "2026-07-05"},       # judged, no longer matches
    "r9:ZED.NS": {"entered": "2026-06-07"},     # a rule that left the wrap-up
    "r1:SOLD.NS": {"entered": "2026-05-03"}}}   # no row, and no longer in any watchlist
wrapup = {"alerts": [{"rule_id": "r1", "tickers": ["ACME", "NEWCO"]}]}
RUN = date(2026, 11, 15)
old = ww.advance_state(state, wrapup, run_date=RUN)
check(old == {"last_run": "2026-11-15", "entries": {"r1:ACME": {"entered": "2026-09-06"},
                                                    "r1:NEWCO": {"entered": "2026-11-15"}}},
      "W1 unchanged: without `unjudged`, the state advances exactly as before")
got = ww.advance_state(state, wrapup, run_date=RUN, unjudged={"ZED.NS"})
check(got["entries"].get("r1:ZED.NS") == {"entered": "2026-08-02"},
      "W2: a watchlist stock with no row this run keeps its entered date")
check(set(got["entries"]) == {"r1:ACME", "r1:NEWCO", "r1:ZED.NS"},
      f"W3: a judged non-match, a rule out of the wrap-up and a stock out of the watchlists are still dropped ({sorted(got['entries'])})")
check(ww._weeks_since(got["entries"]["r1:ZED.NS"]["entered"], date(2026, 11, 22)) == 16,
      "W4: next Sunday its count carries on (16 weeks), not 0")
check(state["entries"]["r1:ZED.NS"] == {"entered": "2026-08-02"} and len(state["entries"]) == 5,
      "W5: the old state is not modified")
src = (REPO / "weekly_wrapup_check.py").read_text()
check("advance_state(state, wrapup, run_date=run_date, unjudged=unjudged)" in src
      and "for tickers in watchlists.values() for t in tickers} - judged" in src,
      "W6: the job passes the watchlist stocks that had no row")

# --- D: which messages failed, and whose they were --------------------------------------------
import alerts

alerts.DISCORD_BATCH_PACING_SECONDS = 0
real_post = alerts._post_discord


def send(messages, bad, **kw):
    posted, failed = [], []
    alerts._post_discord = lambda url, content: (posted.append(content), (not any(b in content for b in bad), "boom"))[1]
    try:
        ok, detail = alerts.send_discord_batch("https://discord.invalid/hook", messages, failed=failed, **kw)
    finally:
        alerts._post_discord = real_post
    return ok, failed, posted


ok, failed, posted = send(["head", "one", "two", "three"], bad=["two"], stop_on_failure=False)
check(ok is False and failed == [2] and len(posted) == 4, f"D1: every message is tried; the failed one is named ({failed})")
ok, failed, posted = send(["head", "one", "two", "three"], bad=["one"])
check(ok is False and failed == [1, 2, 3] and len(posted) == 2,
      f"D2: stopping at the first failure, it and everything after it did not arrive ({failed})")
ok, failed, _ = send(["head", "one"], bad=[])
check(ok is True and failed == [], "D3: a clean send lists nothing")
long_tail = "x" * (alerts._DISCORD_SAFE_LEN - 5)
ok, failed, posted = send(["head", long_tail], bad=[alerts.DISCORD_FOOTER], stop_on_failure=False)
check(len(posted) == 3 and ok is False and failed == [],
      f"D4: the footer sent as its own message is not one of the caller's messages ({failed})")
alerts._post_discord = lambda url, content: (True, "")
try:
    check(alerts.send_discord_batch("https://discord.invalid/hook", ["a"]) == (True, ""),
          "D5 unchanged: without `failed`, the call and its result are as before")
finally:
    alerts._post_discord = real_post

COND = [{"metric_a": "rsi14_daily", "operator": ">", "compare_type": "value", "value": 50}]
SCHED = {"type": "scheduled", "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], "time_et": "21:00"}


def rule(rid):
    return alerts.normalize_rule({"id": rid, "name": rid, "scope": "ALL", "conditions": COND, "enabled": True,
                                  "schedule": dict(SCHED), "notify_mode": "incremental"})


real_build = alerts.build_discord_messages_for_rule
alerts.build_discord_messages_for_rule = lambda r, tickers, by_ticker, labels, limit=1900: (
    [f"{r['id']} part 1", f"{r['id']} part 2"] if r["id"] == "ra" else [f"{r['id']} only"])
SNAP = [{"ticker": "ACME", "rsi14_daily": 60, "market": "m1"}, {"ticker": "ZED.NS", "rsi14_daily": 70, "market": "m1"}]
mr = []
msgs, _ = alerts.evaluate_and_fire([rule("ra"), rule("rb")], SNAP, {}, message_rules=mr)
check(msgs == ["ra part 1", "ra part 2", "rb only"] and mr == ["ra", "ra", "rb"],
      f"D6: evaluate_and_fire names the rule of each message ({mr})")
check(alerts.evaluate_and_fire([rule("ra"), rule("rb")], SNAP, {})[0] == msgs,
      "D6 unchanged: without `message_rules` the messages are the same")

import alert_check as ac


def run_alerts(bad, notice=None, fail_data=False, evaluate=None):
    """alert_check.main() with everything stubbed. Returns (saved state, posts in order, exit code)."""
    names = ("load_rules", "is_rule_due", "fetch_all_markets", "load_data_snapshot", "load_watchlists",
             "load_state_status", "save_state", "load_discord_webhook", "send_discord_batch",
             "post_late_stage_notice", "evaluate_and_fire")
    saved = {k: getattr(ac, k) for k in names}
    captured, order = {}, []

    def batch(hook, messages, stop_on_failure=True, failed=None):
        order.append("alerts")
        bad_idx = [i for i, m in enumerate(messages) if any(b in m for b in bad)]
        if failed is not None:
            failed.extend(bad_idx)
        return (not bad_idx), ("boom" if bad_idx else "")

    try:
        ac.load_rules = lambda: [rule("ra"), rule("rb")]
        ac.is_rule_due = lambda *a, **kw: True
        ac.fetch_all_markets = lambda **kw: (SNAP, "now", {"m1": [] if fail_data else SNAP})
        ac.load_data_snapshot = lambda: {"per_market": {}, "short_history": {}}
        ac.load_watchlists = lambda: {"m1": ["ACME", "ZED.NS"]}
        ac.load_state_status = lambda: ({"ra:ACME": {"was_active": False, "last_triggered_date": "2026-11-01"}}, True)
        ac.save_state = lambda s: captured.update(s)
        ac.load_discord_webhook = lambda: "https://discord.invalid/hook"
        ac.send_discord_batch = batch
        ac.post_late_stage_notice = lambda: order.append("notice")
        if evaluate:
            ac.evaluate_and_fire = evaluate
        code = 0
        try:
            ac.main()
        except SystemExit as e:
            code = e.code
    finally:
        for k, v in saved.items():
            setattr(ac, k, v)
    return captured, order, code


active = lambda st, key: st.get(key, {}).get("was_active")
st, order, code = run_alerts(bad=["rb only"])
check(active(st, "ra:ACME") is True and active(st, "ra:ZED.NS") is True,
      "D7: the rule whose messages arrived stays announced (it used to be rolled back and sent again)")
check(active(st, "rb:ACME") is False and active(st, "rb:ZED.NS") is False,
      "D7: the rule whose message failed is rolled back, so the next run announces it")
st, _, _ = run_alerts(bad=["ra part 2"])
check(active(st, "ra:ACME") is False and st["ra:ACME"]["last_triggered_date"] == "2026-11-01"
      and active(st, "rb:ACME") is True,
      "D8: one failed part of a two-part rule rolls that rule back, with its earlier date kept")
st, _, _ = run_alerts(bad=["Stock Alert Check"])
check(all(active(st, k) for k in ("ra:ACME", "ra:ZED.NS", "rb:ACME", "rb:ZED.NS")),
      "D9: only the header line failed -> every alert arrived, nothing is rolled back")
st, _, _ = run_alerts(bad=[])
check(all(active(st, k) for k in ("ra:ACME", "rb:ZED.NS")), "D10 unchanged: a clean send saves every alert as announced")
real_eval = alerts.evaluate_and_fire
st, _, _ = run_alerts(bad=["rb only"],
                      evaluate=lambda rules, rows, state, due_rules=None, metric_labels=None, **kw:
                      real_eval(rules, rows, state, due_rules=due_rules, metric_labels=metric_labels))
check(not any(active(st, k) for k in ("ra:ACME", "ra:ZED.NS", "rb:ACME", "rb:ZED.NS")),
      "D11: when the rule of each message is not known, everything is rolled back as before")

# --- N: the late-Stage notice, once per slot ------------------------------------------------
_, order, code = run_alerts(bad=[])
check(order == ["alerts", "notice"] and code == 0, f"N1: the notice is posted after the rule check ({order})")
_, order, code = run_alerts(bad=[], fail_data=True)
check(code == 1 and order == [],
      f"N2: an attempt that fails its data check posts no notice -- the retry that gets through does ({order}, exit {code})")
saved_rules = ac.load_rules
try:
    ac.load_rules = lambda: []
    posted = []
    saved_notice, ac.post_late_stage_notice = ac.post_late_stage_notice, lambda: posted.append(1)
    ac.main()
    check(posted == [1], "N3: with no rules at all the notice still goes out (the gate ran the job for it)")
finally:
    ac.load_rules, ac.post_late_stage_notice = saved_rules, saved_notice
alerts.build_discord_messages_for_rule = real_build
src = (REPO / "alert_check.py").read_text()
body = src.split("def main():", 1)[1].split("\ndef check_rules():", 1)[0]
check(body.count("post_late_stage_notice()") == 2 and "e.code not in (0, None)" in body,
      "N4: main() posts it on a clean end and on a clean exit, never on a failing one")

print("FAILURES:", fails)
sys.exit(fails)
