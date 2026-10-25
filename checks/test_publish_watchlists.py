"""publish_market.py watchlists: the tabs, breadth and daily closes the research library gets, and the
1W / YTD / 1D-vs-index return columns its watchlist tables show.

Since 2026-10-23 the screener publishes its watchlists (holdings included, by the owner's choice) so
Claude chat and the library's Q&A can answer "how did my India watchlist do this week?". The breadth must
be the app's own (breadth_math), the closes two years rolling, and each hourly run must write only the
last few days of a series it already stored.

Offline: invented tickers and series, a recording connection, fetch_snapshot's downloads replaced.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import publish_market as pm  # noqa: E402
import stock_data as sd  # noqa: E402
from breadth_math import assemble_closes, compute_breadth  # noqa: E402

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- which lists, with which stocks ---------------------------------------------------------------------
registry = {"in_hold": {"label": "India Invested", "benchmark": "^CRSLDX"},
            "us_hold": {"label": "US Invested", "benchmark": "SPY"},
            "in_ideas": {"label": "India Watchlist", "benchmark": "^CRSLDX"}}
watchlists = {"in_hold": ["ZED.NS", "ACME.NS", "ZED.NS", ""], "us_hold": ["ACME"], "in_ideas": ["ACME.NS", "NEW.NS"]}
groups = {"all_hold": ["in_hold", "us_hold", "gone"], "all_ideas": ["in_ideas"], "empty": []}
labels = {"all_hold": "All Invested", "all_ideas": "All Watchlist", "empty": "Empty"}
lists = pm.watchlist_records(registry, watchlists, groups, labels)
by = {w["name"]: w for w in lists}
check([w["name"] for w in lists] == ["in_hold", "us_hold", "in_ideas", "all_hold", "all_ideas"]
      and [w["ord"] for w in lists] == [0, 1, 2, 3, 4], "tabs in markets.json order, then the groups; an empty group is left out")
check(by["in_hold"]["tickers"] == ["ZED.NS", "ACME.NS"] and by["in_hold"]["kind"] == "watchlist"
      and by["in_hold"]["label"] == "India Invested", "a tab's stocks once each, blanks dropped, with its label")
check(by["all_hold"]["tickers"] == ["ZED.NS", "ACME.NS", "ACME"] and by["all_hold"]["kind"] == "group"
      and by["all_hold"]["benchmark"] is None, "a group: its members' stocks once each; mixed benchmarks give none")
check(by["all_ideas"]["benchmark"] == "^CRSLDX", "a group whose members share a benchmark keeps it")

# --- breadth: each index as stored, each list by breadth_math ------------------------------------------
idx = pd.bdate_range(end="2026-10-22", periods=700)
rng = np.random.default_rng(7)


def walk(start):
    return {d.strftime("%Y-%m-%d"): round(float(v), 2) for d, v in zip(idx, start * np.cumprod(1 + rng.normal(0, 0.01, len(idx))))}


perf = {"as_of": "2026-10-22 21:00 UTC",
        "markets": {"in_hold": {"ZED.NS": walk(100), "ACME.NS": walk(50), "^CRSLDX": walk(20000)},
                    "us_hold": {"ACME": walk(30), "SPY": walk(500)},
                    "in_ideas": {"ACME.NS": walk(999), "NEW.NS": walk(10)}},
        "benchmarks": {"SPY": walk(501), "QQQ": walk(400), "^CRSLDX": walk(20001)}}
block = {"above": 3, "below": 1, "total": 4, "pct_above": 75.0, "pct_above_50": 50.0, "partial": False, "live": False,
         "as_of": "2026-10-22 21:00", "history": {"2026-10-21": 70.0, "2026-10-22": 75.0},
         "history_50": {"2026-10-22": 50.0}, "highs_history": {"2026-10-21": 1.0, "2026-10-22": 2.0},
         "lows_history": {"2026-10-21": 0.5, "2026-10-22": 0.0}}
breadth = {"as_of": "2026-10-22 21:00", "markets": {"INDIA": block, "US": {"history": {}}}}
series = pm.breadth_records(breadth, perf, lists)
sb = {s["series"]: s for s in series}
check(sb["INDIA"]["kind"] == "index" and sb["INDIA"]["label"] == "Nifty 500" and "US" not in sb and "NASDAQ100" not in sb,
      "an index block is published under its library name; a failed or missing leg is skipped (the stored one stays)")
check(sb["INDIA"]["points"] == [("2026-10-21", 70.0, None, 1.0, 0.5), ("2026-10-22", 75.0, 50.0, 2.0, 0.0)],
      "index points: date, % above 200d, % above 50d (blank where absent), % at highs, % at lows")
check(sb["INDIA"]["latest"] == {"above": 3, "below": 1, "total": 4, "pct_above": 75.0, "pct_above_50": 50.0, "partial": False,
                                "live": False, "date": "2026-10-22"}, "the index's latest reading, without its history")
own = compute_breadth(assemble_closes(perf["markets"], by["all_hold"]["tickers"])[0])
check(sb["all_hold"]["points"] == pm._breadth_points(own) and sb["all_hold"]["kind"] == "group"
      and sb["all_hold"]["latest"]["pct_above"] == own["pct_above"] and sb["all_hold"]["latest"]["stocks"] == 3,
      "a list's breadth is breadth_math's over its stocks' closes -- the app's Watchlist Breadth chart's numbers")
check(len(sb["in_ideas"]["points"]) == 700 - 252, "the 52-week warm-up is dropped, as in the app")

# --- closes: two years, every stock and benchmark, from the first block holding it ----------------------
prices = pm.price_series(perf, lists, today=date(2026, 10, 22))
check(set(prices) == {"ZED.NS", "ACME.NS", "ACME", "NEW.NS", "^CRSLDX", "SPY", "QQQ"},
      "closes for every list's stocks, its benchmark and the selectable indexes")
check(prices["SPY"][-1][1] == perf["benchmarks"]["SPY"]["2026-10-22"] and prices["ACME.NS"][-1][1] == perf["markets"]["in_hold"]["ACME.NS"]["2026-10-22"],
      "a ticker in two blocks is taken from the first (benchmarks, then the tabs in order)")
check(all(d >= "2024-10-22" for d, _ in prices["ZED.NS"]) and len(prices["ZED.NS"]) < 700,
      f"only the last {pm.PRICE_DAYS} days are kept")

# --- what each run writes ----------------------------------------------------------------------------------
pts = [(f"2026-10-{d:02d}", d) for d in range(1, 23)]
check(pm.to_write(pts, None) == pts and pm.to_write(pts, (None, 0)) == pts, "a series not stored yet is written in full")
check(pm.to_write(pts, ("2026-10-20", 22)) == pts[9:], f"a stored series: from {pm.REWRITE_DAYS} days before its newest date on")
check(pm.to_write(pts, ("2026-10-20", 5)) == pts, "a series stored with far fewer rows (a cut-off first run) is written in full")


class Cur:
    def __init__(self, conn):
        self.conn, self.rowcount = conn, 0

    def execute(self, sql, params=None):
        self.conn.log.append((sql, params))
        self.rowcount = 4 if sql.startswith("DELETE FROM kg_prices") else 0

    def executemany(self, sql, seq):
        self.conn.log.append((sql, list(seq)))

    def fetchall(self):
        return self.conn.stored.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Conn:
    def __init__(self, stored=()):
        self.log, self.stored, self.transactions = [], list(stored), 0

    def cursor(self):
        return Cur(self)

    def transaction(self):
        conn = self

        class T:
            def __enter__(self):
                conn.transactions += 1

            def __exit__(self, *a):
                return False
        return T()


c = Conn()
pm.write_watchlists(c, lists)
sql = [s for s, _ in c.log]
members = next(p for s, p in c.log if s.startswith("INSERT INTO kg_watchlist_members"))
check(c.transactions == 1 and sql[:2] == ["DELETE FROM kg_watchlist_members", "DELETE FROM kg_watchlists"]
      and ("all_hold", "ACME") in members and len(members) == sum(len(w["tickers"]) for w in lists),
      "watchlists and members are replaced in one transaction (a tab removed in the app disappears)")

c = Conn([[("INDIA", "2026-10-22", 2), ("all_hold", "2026-10-22", len(sb["all_hold"]["points"]))]])
n = pm.write_breadth(c, series)
deletes = [p for s, p in c.log if s.startswith("DELETE FROM kg_breadth")]
check(n == 2 + len(sb["in_hold"]["points"]) + len(sb["us_hold"]["points"]) + len(sb["in_ideas"]["points"]) + len(sb["all_ideas"]["points"])
      + len([p for p in sb["all_hold"]["points"] if p[0] >= "2026-10-12"]),
      "breadth: new series in full, stored ones from 10 days back")
check(len(deletes) == 2 and all(p == ([s["series"] for s in series],) for p in deletes)
      and all("kind <> 'index'" in s for s, _ in c.log if s.startswith("DELETE FROM kg_breadth")),
      "the series of lists that no longer exist are dropped; an index series never is")

c = Conn([[("SPY", "2026-10-22", 500)]])
written, deleted = pm.write_prices(c, prices, today=date(2026, 10, 22))
check(written == sum(len(v) for t, v in prices.items() if t != "SPY") + len([d for d, _ in prices["SPY"] if d >= "2026-10-12"])
      and deleted == 4, "closes: new tickers in full, stored ones from 10 days back; the delete's count is reported")
dsql, dparams = next((s, p) for s, p in c.log if s.startswith("DELETE FROM kg_prices"))
check(dparams == ("2024-10-22", list(prices)), "closes older than two years, and of tickers no list holds, are deleted")

real = (sd.load_markets_registry, sd.load_watchlists, sd.load_watchlist_groups)
sd.load_markets_registry, sd.load_watchlists, sd.load_watchlist_groups = lambda: registry, lambda: {}, lambda: {}
try:
    c = Conn()
    check(pm.publish_watchlists(c) == 1 and not c.log, "no list with any stock (data files not loaded): nothing is written")
finally:
    sd.load_markets_registry, sd.load_watchlists, sd.load_watchlist_groups = real
check(pm.main(["publish_market.py", "watchlists"]) == 0, "without MARKET_DB_URL the mode publishes nothing and does not fail")

# --- the 1W, YTD and 1D-vs-index columns ------------------------------------------------------------------
FIELDS = ["Open", "High", "Low", "Close", "Volume"]
days = pd.bdate_range(end="2026-10-22", periods=300)
stock = 100 + np.arange(len(days)) * 0.1
bench = 20000 + np.arange(len(days)) * 3.0 + (np.arange(len(days)) % 2) * 50


def frame(tickers, closes):
    f = pd.DataFrame(index=days, columns=pd.MultiIndex.from_product([tickers, FIELDS]), dtype=float)
    for t, c in zip(tickers, closes):
        f[(t, "Open")], f[(t, "High")], f[(t, "Low")], f[(t, "Close")], f[(t, "Volume")] = c, c + 1, c - 1, c, 1000.0
    return f


class FakeTicker:
    def __init__(self, t):
        self.info = {"longName": "Zed Industries Limited"}
        self.cash_flow = self.income_stmt = self.balance_sheet = self.quarterly_income_stmt = pd.DataFrame()


NOW = datetime(2026, 10, 23, 1, 40, tzinfo=timezone.utc)


class FixedNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


real = (sd._download_with_retries, sd.yf.Ticker, sd.datetime)
sd._download_with_retries = lambda tickers, period: frame(["ZED.NS", "^CRSLDX"], [stock, bench])
sd.yf.Ticker = FakeTicker
sd.datetime = FixedNow
try:
    rows, _ = sd.fetch_snapshot(["ZED.NS"], benchmark="^CRSLDX", settings=dict(sd.DEFAULT_SETTINGS))
finally:
    sd._download_with_retries, sd.yf.Ticker, sd.datetime = real
row = rows[0] if rows else {}
year_end = stock[[i for i, d in enumerate(days) if d.year == 2025][-1]]
check(row.get("perf_1w") == round((stock[-1] / stock[-6] - 1) * 100, 1), f"Perf 1W %: 5 sessions back ({row.get('perf_1w')})")
check(row.get("perf_ytd") == round((stock[-1] / year_end - 1) * 100, 1),
      f"Perf YTD %: against the last close of the previous calendar year ({row.get('perf_ytd')})")
check(row.get("rel_ret_1d_index") == round(((stock[-1] / stock[-2] - 1) - (bench[-1] / bench[-2] - 1)) * 100, 1),
      f"1D Ret vs Index: the day's return minus the index's ({row.get('rel_ret_1d_index')})")

labels_by_field = {v: k for k, v in sd.get_filterable_metrics().items()}
check(all(labels_by_field.get(f) == lbl for f, lbl in (("perf_1w", "Perf 1W %"), ("perf_ytd", "Perf YTD %"),
                                                       ("rel_ret_1d_index", "1D Ret vs Index"))),
      "the new columns are filterable under their column labels")
import glossary  # noqa: E402
import market_api  # noqa: E402
_settings = dict(sd.DEFAULT_SETTINGS)
defs = glossary.column_definitions(_settings, glossary.ema_col_labels(_settings))
check(all(k in defs for k in ("Perf 1W %", "Perf YTD %", "1D Ret vs Index")), "each has a glossary entry")
check({"perf_1w", "perf_ytd", "rel_ret_1d_index"} <= {f for f, *_ in market_api.METRIC_GROUPS["returns"][1]},
      "and is shared with the research library in the returns group")

print(f"\nFAILURES: {fails}")
sys.exit(1 if fails else 0)
