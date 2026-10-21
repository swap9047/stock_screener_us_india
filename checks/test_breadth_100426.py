"""Market breadth fixes from the 2026-10-04 review.

B1 Every slot re-downloaded both markets (~1000 tickers x 6 years, ~53 min),
   though each slot has only one new close: India's by the 7 AM ET slot, the
   US's by 7 PM (10 AM / 10 PM until 2026-10-08). A market whose latest completed session is already stored is
   now skipped (status "unchanged"). Since 2026-10-09 "the latest session" is
   computed from each exchange's clock (expected_session) rather than asked of
   one Yahoo index series, which lagged. A latest day only part of the index has
   a close for is computed from those names and marked "partial"; one with no
   close at all (a row of NaN) is "awaiting". Both make the gate retry.
B2 Saturday's two slots fetched nothing new: the gate runs MON-FRI.
B3 The same 2 Nifty symbols failed every run and cost ~8 min of retry waits.
   Since 2026-10-09 the batches are 5 s apart (was 120 s) and every failure is
   retried on its own, 5 s apart, up to 3 rounds, stopping on a round that
   recovers nothing (retry_failed).
B4 The 5-year history uses TODAY's index members, so the past looks stronger
   than it was; the chart says so.

Offline: stubbed downloads, no network, no data files.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import refresh_market_breadth as rmb

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def fn(name):
    f = getattr(rmb, name, None)
    if f is None:
        check(False, f"refresh_market_breadth.{name} exists")
    return f


# --- B1 is there a new session? ------------------------------------------------------
needs = fn("needs_refresh")
if needs:
    block = {"history": {"2026-09-30": 42.2, "2026-10-01": 38.6}, "history_50": {"2026-10-01": 40.0}}
    check(needs(None, "2026-10-01"), "B1: no stored block -> refresh")
    check(needs(block, None), "B1: the session check failed -> refresh (never skip blind)")
    check(needs(block, "2026-10-02"), "B1: a newer completed session -> refresh")
    check(not needs(block, "2026-10-01"), "B1: the latest session is already stored -> skip")
    check(needs({"history": {}}, "2026-10-01"), "B1: an empty history -> refresh")
    check(needs({"history": {"2026-10-01": 38.6}}, "2026-10-01"),
          "B1: a block from before the 50-day series is redone once, though its date is current")

exp = fn("expected_session")
live = fn("is_live")
if exp and live:
    NY, IN = "America/New_York", "Asia/Kolkata"
    US_OPEN, US_CLOSE, IN_OPEN, IN_CLOSE = (9, 30), (16, 0), (9, 15), (15, 30)
    def at(ts, tz):
        return pd.Timestamp(ts, tz=tz)
    # Since 2026-10-14 the latest session is the one that has OPENED (the line
    # follows the latest fetch, not the day close).
    check(exp(NY, US_OPEN, now=at("2026-10-09 09:29", NY)) == "2026-10-08",
          "B1: before the US open -> the day before")
    check(exp(NY, US_OPEN, now=at("2026-10-09 09:30", NY)) == "2026-10-09",
          "B1: at the US open -> today, its bar still forming")
    check(exp(NY, US_OPEN, now=at("2026-10-09 20:35", NY)) == "2026-10-09",
          "B1: Friday 8:35 PM ET -> Friday (the 2026-10-09 miss)")
    check(exp(NY, US_OPEN, now=at("2026-10-12 07:00", NY)) == "2026-10-09",
          "B1: Monday 7 AM ET -> back over the weekend to Friday")
    check(exp(NY, US_OPEN, now=at("2026-10-10 12:00", NY)) == "2026-10-09", "B1: Saturday -> Friday")
    check(exp(IN, IN_OPEN, now=at("2026-10-09 07:00", NY)) == "2026-10-09",
          "B1: the 7 AM ET slot -> India's that day (4:30 PM IST)")
    check(exp(IN, IN_OPEN, now=at("2026-10-08 23:44", NY)) == "2026-10-08",
          "B1: 9:14 AM IST -> India's day before")
    check(exp(IN, IN_OPEN, now=at("2026-10-08 23:45", NY)) == "2026-10-09",
          "B1: 9:15 AM IST (11:45 PM ET) -> India's new day, forming")
    check(exp(IN, IN_OPEN, now=at("2026-10-09 19:00", NY)) == "2026-10-09",
          "B1: the 7 PM ET slot (Saturday 4:30 AM IST) -> India's Friday")

    check(live("2026-10-14", NY, US_CLOSE, now=at("2026-10-14 11:44", NY)),
          "B1: a US reading at 11:44 AM ET is live")
    check(live("2026-10-14", NY, US_CLOSE, now=at("2026-10-14 16:29", NY)),
          "B1: ...until 30 minutes after the close")
    check(not live("2026-10-14", NY, US_CLOSE, now=at("2026-10-14 16:30", NY)), "B1: then final")
    check(not live("2026-10-13", NY, US_CLOSE, now=at("2026-10-14 11:44", NY)), "B1: an earlier day is never live")
    check(not live("2026-10-14", IN, IN_CLOSE, now=at("2026-10-14 11:44", NY)),
          "B1: India at 11:44 AM ET (9:14 PM IST) is final")

tail = fn("unpublished_tail")
share = fn("latest_day_share")
if tail and share:
    idx = pd.bdate_range("2026-09-28", periods=10)
    full = pd.DataFrame(1.0, index=idx, columns=[f"T{i}" for i in range(20)])
    check(tail(full) == [] and share(full) == 1.0, "B1: a fully published panel: nothing unpublished, share 1.0")
    nan_tail = full.copy(); nan_tail.iloc[-1] = float("nan")
    check(tail(nan_tail) == [str(idx[-1].date())],
          f"B1: a trailing row of NaN is a session with no close yet ({tail(nan_tail)})")
    check(tail(full.iloc[:-1]) == [], "B1: a holiday has no row at all -> nothing unpublished")
    thin = full.copy(); thin.iloc[-1, 5:] = float("nan")
    check(tail(thin) == [] and share(thin) == 0.25, f"B1: 5 of 20 names published -> share 0.25 ({share(thin)})")
    near = full.copy(); near.iloc[-1, :1] = float("nan")
    check(share(near) == 0.95, "B1: 19 of 20 -> 0.95, above the partial line")
    check(rmb.PUBLISHED_MIN_SHARE == 0.9, "B1: under 90% published counts as partial")

    # calculate_breadth computes a part-published day from the names that have it
    long_idx = pd.bdate_range("2025-01-01", periods=600)
    rising = pd.Series(range(600), index=long_idx, dtype=float) + 100
    falling = 800 - pd.Series(range(600), index=long_idx, dtype=float)
    def batch_frame(names, last_row_names):
        data = {}
        for n in names:
            col = (rising if n.startswith("U") else falling).copy()
            if n not in last_row_names:
                col.iloc[-1] = float("nan")
            data[("Close", n)] = col
        df = pd.DataFrame(data)
        df.columns = pd.MultiIndex.from_tuples(df.columns, names=["Price", "Ticker"])
        return df
    names = [f"U{i}" for i in range(10)] + [f"D{i}" for i in range(10)]
    saved_dl, saved_sleep = rmb.yf.download, rmb.time.sleep
    try:
        rmb.time.sleep = lambda s: None
        some = {f"U{i}" for i in range(8)} | {f"D{i}" for i in range(4)}
        rmb.yf.download = lambda t, **k: batch_frame(t if isinstance(t, list) else [t], some)
        b = rmb.calculate_breadth(names, "Test", "America/New_York", (16, 0))
        last = max(b["history"])
        check(last == str(long_idx[-1].date()) and b["total"] == 12 and b["history"][last] == 66.7 and b["partial"],
              f"B1: a 12-of-20 latest day is kept, computed from those 12 (8 above -> 66.7%), marked partial "
              f"({last}, {b['total']}, {b['history'].get(last)}, partial={b['partial']})")
        rmb.yf.download = lambda t, **k: batch_frame(t if isinstance(t, list) else [t], set())
        b = rmb.calculate_breadth(names, "Test", "America/New_York", (16, 0))
        check(max(b["history"]) == str(long_idx[-2].date()) and b["unpublished"] == [str(long_idx[-1].date())]
              and not b["partial"], f"B1: a day with no close at all is unpublished ({b['unpublished']})")
        # 2026-10-09 9:28 PM ET: 1 of 503 US names had a Friday close
        rmb.yf.download = lambda t, **k: batch_frame(t if isinstance(t, list) else [t], {"U0"})
        b = rmb.calculate_breadth(names, "Test", "America/New_York", (16, 0))
        check(max(b["history"]) == str(long_idx[-2].date()) and b["unpublished"] == [str(long_idx[-1].date())]
              and b["total"] == 20 and not b["partial"],
              f"B1: a day under half published (1 of 20) is held back, not read from one name "
              f"(ends {max(b['history'])}, unpublished {b['unpublished']})")
        rmb.yf.download = lambda t, **k: batch_frame(t if isinstance(t, list) else [t], set(names[:10]))
        b = rmb.calculate_breadth(names, "Test", "America/New_York", (16, 0))
        check(max(b["history"]) == str(long_idx[-1].date()) and b["total"] == 10 and b["partial"],
              "B1: exactly half published is computed (from those 10), marked partial")
    finally:
        rmb.yf.download, rmb.time.sleep = saved_dl, saved_sleep


def run_main(stored, expected, block, live_days=None):
    """main() with the downloads stubbed; returns (saved file, legs downloaded).
    `live_days` {tz: {dates}}: what is_live answers (default: nothing is live).

    The Nasdaq 100 leg (2026-10-15) is stubbed too -- nothing here may touch the
    network -- and, unless `block` has a "Nasdaq 100" entry, left out of what is
    returned: these checks are about the US and India legs."""
    calls = []
    live_days = live_days or {}
    # The fixtures are current blocks: give them the 50-day series (2026-10-15),
    # or needs_refresh redoes every one of them.
    stored = json.loads(json.dumps(stored))
    for v in (stored.get("markets") or {}).values():
        v.setdefault("history_50", {})
    with_nasdaq = "Nasdaq 100" in block
    block = {"Nasdaq 100": {"total": 9, "pct_above": 50.0, "history": {"2026-10-01": 50.0},
                            "partial": False, "unpublished": []}, **block}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "market_breadth.json")
        Path(path).write_text(json.dumps(stored))
        saved = (rmb.BREADTH_FILE, rmb.get_sp500_tickers, rmb.get_nifty500_tickers,
                 rmb.calculate_breadth, rmb.expected_session, rmb.is_live, rmb.get_nasdaq100_tickers)
        rmb.BREADTH_FILE = path
        rmb.is_live = lambda day, tz, hhmm, **k: day in live_days.get(tz, set())
        rmb.get_sp500_tickers = lambda: ["A"] * 10
        rmb.get_nifty500_tickers = lambda: ["B.NS"] * 10
        rmb.get_nasdaq100_tickers = lambda: ["C"] * 10
        rmb.calculate_breadth = lambda tickers, label, tz, hhmm: (calls.append(label) or dict(block[label]))
        rmb.expected_session = lambda tz, hhmm, **k: expected[tz]
        try:
            rmb.main()
            out = json.loads(Path(path).read_text())
            if not with_nasdaq:
                out["status"].pop("NASDAQ100", None)
                out["markets"].pop("NASDAQ100", None)
                calls = [c for c in calls if c != "Nasdaq 100"]
            return out, calls
        finally:
            (rmb.BREADTH_FILE, rmb.get_sp500_tickers, rmb.get_nifty500_tickers,
             rmb.calculate_breadth, rmb.expected_session, rmb.is_live, rmb.get_nasdaq100_tickers) = saved


STORED = {"as_of": "2026-10-03 17:00", "markets": {
    "US": {"as_of": "2026-10-03 17:00", "total": 501, "history": {"2026-10-02": 42.3}},
    "INDIA": {"as_of": "2026-10-03 17:00", "total": 490, "history": {"2026-09-30": 42.2}}},
    "status": {}}
NEW_INDIA = {"total": 9, "pct_above": 50.0, "history": {"2026-10-01": 50.0}, "partial": False, "unpublished": []}
out, calls = run_main(STORED, {"America/New_York": "2026-10-02", "Asia/Kolkata": "2026-10-01"},
                      {"Nifty 500": NEW_INDIA})
check(calls == ["Nifty 500"], f"B1: only the market short of its expected session is downloaded ({calls})")
check(out["status"] == {"US": "unchanged", "INDIA": "ok"}, f"B1: statuses say so ({out['status']})")
check(out["markets"]["US"]["history"] == {"2026-10-02": 42.3} and out["markets"]["US"]["as_of"] == out["as_of"],
      "B1: the unchanged block is kept, and marked current (so the app does not call it stale)")
check("unpublished" not in out["markets"]["INDIA"], "B1: the unpublished list is not stored")

# 2026-10-09: Friday traded, Yahoo served it as NaN -> awaiting, block still saved
LATE_US = {"total": 9, "pct_above": 46.7, "history": {"2026-10-08": 46.7}, "partial": False, "unpublished": ["2026-10-09"]}
out, calls = run_main(STORED, {"America/New_York": "2026-10-09", "Asia/Kolkata": "2026-09-30"},
                      {"S&P 500": LATE_US})
check(out["status"] == {"US": "awaiting", "INDIA": "unchanged"},
      f"B1: an expected session Yahoo has not published -> 'awaiting' ({out['status']})")
check(out["markets"]["US"]["history"] == {"2026-10-08": 46.7},
      "B1: ...and the block, as current as Yahoo allows, is still saved")

# a holiday: short of the expected day, but no unpublished row -> ok
HOLIDAY_US = {"total": 9, "pct_above": 46.7, "history": {"2026-10-08": 46.7}, "partial": False, "unpublished": []}
out, calls = run_main(STORED, {"America/New_York": "2026-10-09", "Asia/Kolkata": "2026-09-30"},
                      {"S&P 500": HOLIDAY_US})
check(out["status"]["US"] == "ok", f"B1: short of the expected day with no unpublished row (a holiday) -> ok ({out['status']})")

# a part-published latest day: saved and shown, status "partial", recomputed next run
PART_US = {"total": 250, "pct_above": 45.0, "history": {"2026-10-08": 46.7, "2026-10-09": 45.0},
           "partial": True, "unpublished": []}
out, calls = run_main(STORED, {"America/New_York": "2026-10-09", "Asia/Kolkata": "2026-09-30"},
                      {"S&P 500": PART_US})
us = out["markets"]["US"]
check(out["status"]["US"] == "partial" and max(us["history"]) == "2026-10-09" and us["total"] == 250,
      f"B1: a part-published Friday is shown on Friday, from 250 names, status partial ({out['status']})")
check(us.get("partial") is True and rmb.needs_refresh(us, "2026-10-09"),
      "B1: ...stored as partial, so the next run recomputes it although its date is current")

# never replace a stored reading with a staler one
STORED_FRI = {"as_of": "2026-10-09 16:18", "markets": {
    "US": {"as_of": "2026-10-09 16:18", "total": 499, "history": {"2026-10-08": 46.7}},
    "INDIA": {"as_of": "2026-10-09 16:18", "total": 488, "history": {"2026-10-09": 38.5}, "partial": True}},
    "status": {}}
EARLY = {"total": 480, "pct_above": 39.9, "history": {"2026-10-08": 39.9}, "partial": False, "unpublished": ["2026-10-09"]}
out, _ = run_main(STORED_FRI, {"America/New_York": "2026-10-08", "Asia/Kolkata": "2026-10-09"}, {"Nifty 500": EARLY})
check(out["markets"]["INDIA"]["history"] == {"2026-10-09": 38.5},
      f"B1: a download ending a day earlier keeps the stored block ({out['markets']['INDIA']['history']})")
THINNER = {"total": 250, "pct_above": 41.0, "history": {"2026-10-09": 41.0}, "partial": True, "unpublished": []}
out, _ = run_main(STORED_FRI, {"America/New_York": "2026-10-08", "Asia/Kolkata": "2026-10-09"}, {"Nifty 500": THINNER})
check(out["markets"]["INDIA"]["total"] == 488 and out["markets"]["INDIA"]["history"] == {"2026-10-09": 38.5},
      "B1: the same day from fewer names keeps the stored block")
FULLER = {"total": 495, "pct_above": 38.9, "history": {"2026-10-09": 38.9}, "partial": False, "unpublished": []}
out, _ = run_main(STORED_FRI, {"America/New_York": "2026-10-08", "Asia/Kolkata": "2026-10-09"}, {"Nifty 500": FULLER})
check(out["markets"]["INDIA"]["total"] == 495 and out["status"]["INDIA"] == "ok"
      and not out["markets"]["INDIA"].get("partial"),
      f"B1: the same day from more names replaces a partial block, status ok ({out['status']})")

# --- live readings (2026-10-14: follow the latest fetch, not the day close) ----------
NY = "America/New_York"
EXP = {NY: "2026-10-14", "Asia/Kolkata": "2026-09-30"}
LIVE_US = {"total": 500, "pct_above": 49.9, "history": {"2026-10-13": 49.4, "2026-10-14": 49.9},
           "partial": False, "unpublished": []}
out, calls = run_main(STORED, EXP, {"S&P 500": LIVE_US}, live_days={NY: {"2026-10-14"}})
us = out["markets"]["US"]
check(out["status"]["US"] == "live" and us["live"] is True and max(us["history"]) == "2026-10-14",
      f"B1: mid-session the line ends on today, stored live, status live ({out['status']})")
check(rmb.needs_refresh(us, "2026-10-14"), "B1: a live reading is redone at the next run")

STORED_LIVE = {"as_of": "x", "status": {}, "markets": {
    "US": {"as_of": "x", "total": 500, "history": {"2026-10-13": 49.4, "2026-10-14": 49.9}, "live": True},
    "INDIA": {"as_of": "x", "total": 490, "history": {"2026-09-30": 42.2}}}}
# after the close, Yahoo blanks the day: the download ends a day early -> keep the
# intraday reading, but retry (awaiting) rather than count the slot done on it
BLANK = {"total": 500, "pct_above": 49.4, "history": {"2026-10-13": 49.4}, "partial": False,
         "unpublished": ["2026-10-14"]}
out, _ = run_main(STORED_LIVE, EXP, {"S&P 500": BLANK})
check(max(out["markets"]["US"]["history"]) == "2026-10-14" and out["status"]["US"] == "awaiting",
      f"B1: after the close, a blank day keeps the intraday reading and retries ({out['status']})")
# the final close arrives, even from one name fewer -> it replaces the live reading
FINAL = {"total": 499, "pct_above": 50.2, "history": {"2026-10-13": 49.4, "2026-10-14": 50.2},
         "partial": False, "unpublished": []}
out, _ = run_main(STORED_LIVE, EXP, {"S&P 500": FINAL})
us = out["markets"]["US"]
check(us["history"]["2026-10-14"] == 50.2 and not us["live"] and out["status"]["US"] == "ok",
      f"B1: a final close replaces the live reading, even from fewer names ({out['status']})")
# a newer live reading replaces an older live one, whatever its count
LATER = dict(LIVE_US, total=480, history={"2026-10-13": 49.4, "2026-10-14": 51.0})
out, _ = run_main(STORED_LIVE, EXP, {"S&P 500": LATER}, live_days={NY: {"2026-10-14"}})
check(out["markets"]["US"]["history"]["2026-10-14"] == 51.0, "B1: a newer live reading replaces an older one")

# --- Nasdaq 100 (2026-10-15): a third leg, stored under its own key ------------------
NDX = {"total": 101, "pct_above": 55.0, "history": {"2026-10-14": 55.0}, "partial": False, "unpublished": []}
out, calls = run_main(STORED, {"America/New_York": "2026-10-14", "Asia/Kolkata": "2026-09-30"},
                      {"S&P 500": dict(NDX, total=500), "Nasdaq 100": NDX})
check("Nasdaq 100" in calls and out["status"].get("NASDAQ100") == "ok"
      and out["markets"]["NASDAQ100"]["history"] == {"2026-10-14": 55.0},
      f"Nasdaq 100 is computed and stored as its own block ({out['status']})")
check(out["markets"]["US"]["total"] == 500, "...without touching the S&P 500 block")
legs = [l for l in (REPO / "refresh_market_breadth.py").read_text().splitlines() if '("NASDAQ100"' in l]
check(legs and '"America/New_York", (9, 30), (16, 0)' in legs[0], "Nasdaq 100 runs on the US exchange clock")

nd = fn("get_nasdaq100_tickers")
if nd:
    import types
    class Resp:
        def __init__(self, text="", js=None): self.text, self._js = text, js
        def json(self): return self._js
    wiki = ("<table><tr><th>Ticker</th><th>Company</th></tr>"
            + "".join(f"<tr><td>T{i}</td><td>Co {i}</td></tr>" for i in range(99))
            + "<tr><td>ACME.B</td><td>Acme</td></tr></table>")
    api = {"data": {"data": {"rows": [{"symbol": f"N{i}"} for i in range(101)]}}}
    saved_req = rmb.requests
    try:
        rmb.requests = types.SimpleNamespace(get=lambda url, **k: Resp(wiki) if "wikipedia" in url else Resp(js=api))
        got = nd()
        check(len(got) == 100 and got[-1] == "ACME-B", f"Nasdaq 100 members from Wikipedia, dots as dashes ({len(got)})")
        rmb.requests = types.SimpleNamespace(get=lambda url, **k: Resp("<p>no table</p>") if "wikipedia" in url else Resp(js=api))
        got = nd()
        check(len(got) == 101 and got[0] == "N0", "Wikipedia without the table -> Nasdaq's list API")
        rmb.requests = types.SimpleNamespace(get=lambda url, **k: Resp(wiki.replace("T9", "")[:200]) if "wikipedia" in url
                                             else Resp(js={"data": {"data": {"rows": [{"symbol": "X"}]}}}))
        try:
            nd(); check(False, "a broken parse from both sources raises")
        except ValueError:
            check(True, "a broken parse from both sources raises (the leg fails, the stored block stays)")
    finally:
        rmb.requests = saved_req

# --- B2 / B4 / workflow ---------------------------------------------------------------
WF = (REPO / ".github/workflows/market-breadth.yml").read_text()
check('window: "SUN:18-FRI:23"' in WF, "B2: the breadth slots run Sunday 6 PM to Friday 11 PM ET, no Saturday slot")
check('{"US", "INDIA", "NASDAQ100"}' in WF, "the verify step expects the Nasdaq 100 block too")
check('("ok", "unchanged", "live", "awaiting", "partial")' in WF and 'v in ("awaiting", "partial")' in WF and "sys.exit(1)" in WF,
      "B1: the verify step accepts an unchanged market and fails an awaiting or partial one (so the gate retries)")
APP = (REPO / "app.py").read_text()
check("survivorship" in APP.lower(), "B4: the breadth charts note the survivorship bias")
check('"6 Months": 6, "1 Year": 12, "1.5 Years": 18, "2 Years": 24,' in APP and '"3 Years": 36, "5 Years": 60}' in APP
      and "DateOffset(months=filter_months)" in APP,
      "the breadth charts offer 6 months, 1, 1.5, 2, 3 and 5 years (2026-10-15)")
check('" intraday" if _breadth_block(market_key).get("live")' in APP, "a live breadth reading is labelled intraday")
check('st.selectbox("Time Horizon", list(_horizon_months), index=2,' in APP,
      "the horizon is a dropdown opening on 1.5 Years, the third entry (owner, 2026-10-15)")

# --- B3 the retry loop ----------------------------------------------------------------
check(rmb.BATCH_PAUSE_SECONDS == 5 and rmb.RETRY_PAUSE_SECONDS == 5,
      "B3: 5 s between batches and between retries")
BSRC = (REPO / "refresh_market_breadth.py").read_text()
check("time.sleep(120)" not in BSRC and "time.sleep(180)" not in BSRC and "time.sleep(300)" not in BSRC,
      "B3: the 2/3/5-minute waits are gone")

retry = fn("retry_failed")
if retry:
    _idx = pd.bdate_range("2026-01-05", periods=3)

    def yf_frame(t):
        # yfinance 1.5's shape for ONE ticker: MultiIndex columns, so ["Close"]
        # is a one-column frame -- the shape the old retry check crashed on.
        cols = pd.MultiIndex.from_tuples([("Close", t), ("Open", t)], names=["Price", "Ticker"])
        return pd.DataFrame([[1.0, 1.0], [2.0, 2.0], [3.0, 3.0]], index=_idx, columns=cols)

    def flaky(fail_times):
        seen = {}
        def dl(t):
            seen[t] = seen.get(t, 0) + 1
            if seen[t] <= fail_times.get(t, 0):
                return pd.DataFrame()
            return yf_frame(t)
        return dl, seen

    sleeps = []
    dl, seen = flaky({"AAA": 1, "BBB": 2, "CCC": 0})
    got, still = retry(["AAA", "BBB", "CCC"], download=dl, sleep=sleeps.append)
    check(still == [] and sorted(f.columns[0] for f in got) == ["AAA", "BBB", "CCC"]
          and seen == {"AAA": 2, "BBB": 3, "CCC": 1},
          f"B3: rounds 1-3 recover one each, the last failing twice first ({still}, {seen})")
    check(all(f.iloc[:, 0].tolist() == [1.0, 2.0, 3.0] for f in got),
          "B3: the close column is taken from yfinance's one-ticker frame")
    check(sleeps and set(sleeps) == {5} and len(sleeps) == sum(seen.values()),
          f"B3: 5 s before every retry call ({sleeps})")

    dl, seen = flaky({"DEAD": 99})
    got, still = retry(["DEAD"], download=dl, sleep=lambda s: None)
    check(still == ["DEAD"] and got == [] and seen["DEAD"] == 1,
          f"B3: a round that recovers nothing ends the loop (tried {seen['DEAD']}x)")

    dl, seen = flaky({"AAA": 99, "BBB": 1, "CCC": 0})
    got, still = retry(["AAA", "BBB", "CCC"], download=dl, sleep=lambda s: None)
    check(still == ["AAA"] and seen == {"AAA": 3, "BBB": 2, "CCC": 1},
          f"B3: keeps going while a round recovers something, stops on one that doesn't ({seen})")

    dl, seen = flaky({"AAA": 99, "BBB": 0, "CCC": 1, "DDD": 2, "EEE": 3})
    got, still = retry(["AAA", "BBB", "CCC", "DDD", "EEE"], download=dl, sleep=lambda s: None)
    check(still == ["AAA", "EEE"] and seen["EEE"] == rmb.MAX_RETRY_PASSES == 3,
          f"B3: never more than MAX_RETRY_PASSES rounds ({seen}, left {still})")

    def boom(t):
        raise RuntimeError("throttled")
    got, still = retry(["AAA"], download=boom, sleep=lambda s: None)
    check(still == ["AAA"], "B3: a download that raises counts as a failure, not a crash")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
