"""market_lists.py and the MCP list tools: the screener's watchlists, breadth and closes for Claude chat.

"How did my India watchlist do this week, best and worst?" needs the tab as a table (sortable, with a
summary over every stock), loose names ("my India holdings"), breadth for indexes and tabs, and closes for
held and non-held tickers, without ever raising on a bad name.

Offline: invented lists and tickers over a fake database; Yahoo replaced.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "api"))

import market_lists as mls  # noqa: E402
import mcp  # noqa: E402

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


TODAY = datetime.now(timezone.utc).date()
DAYS = [(TODAY - timedelta(days=i)).isoformat() for i in range(400, -1, -1)]
LISTS = [
    {"name": "in_hold", "label": "India Invested", "kind": "watchlist", "benchmark": "^CRSLDX", "members": 3, "ord": 0},
    {"name": "us_hold", "label": "US Invested", "kind": "watchlist", "benchmark": "SPY", "members": 1, "ord": 1},
    {"name": "in_ideas", "label": "India Watchlist", "kind": "watchlist", "benchmark": "^CRSLDX", "members": 2, "ord": 2},
    {"name": "subs", "label": "Substack-OutperformingMarket", "kind": "watchlist", "benchmark": "^IXIC", "members": 1, "ord": 3},
    {"name": "all_hold", "label": "All Invested", "kind": "group", "benchmark": None, "members": 4, "ord": 4},
]
MEMBERS = {"in_hold": ["ACME.NS", "ZED.NS", "NODATA.NS"], "us_hold": ["ACME"], "in_ideas": ["NEW.NS", "ZED.NS"],
           "subs": ["ACME"], "all_hold": ["ACME.NS", "ZED.NS", "NODATA.NS", "ACME"]}


def rec(t, chg, w1, ytd):
    return {"ticker": t, "company_name": f"{t} Ltd", "data_end": DAYS[-1],
            "metrics": {"last_close": 100.0, "pct_change_1d": chg, "perf_1w": w1, "perf_ytd": ytd, "trend": "Uptrend"}}


MARKET = {"ACME.NS": rec("ACME.NS", 1.5, 4.0, 20.0), "ZED.NS": rec("ZED.NS", -2.0, -1.0, None),
          "NEW.NS": rec("NEW.NS", 0.0, 9.0, 3.0), "ACME": rec("ACME", 0.5, None, 1.0)}
SERIES = [{"series": "INDIA", "label": "Nifty 500", "kind": "index", "latest": {"pct_above": 30.0}, "as_of": "x"},
          {"series": "in_hold", "label": "India Invested", "kind": "watchlist", "latest": {"pct_above": 80.0}, "as_of": "x"}]
POINTS = {s["series"]: [{"date": d, "pct_above_200": 50.0, "pct_above_50": 40.0, "pct_highs": 1.0, "pct_lows": 2.0} for d in DAYS]
          for s in SERIES}
def _row(label, rev, season=False, gap=0):
    return {"label": label, "rev": rev, "ni": rev + 1, "eps": rev + 2, "margin": 15.0,
            "n": {"rev": 40, "ni": 39, "eps": 38, "margin": 30}, "reported": 50, "season": season, "gap": gap}


QUARTERLY = {"as_of": "2026-10-30 08:00 UTC", "series": {
    "INDIA": {"label": "Nifty 500", "kind": "index", "stocks": 501, "companies": 499,
              "yoy": [_row("Mar '26", 14.5), _row("Jun '26", 18.0), _row("Sep '26", 18.4, season=True, gap=119)],
              "qoq": [_row("Jun '26", 2.0), _row("Sep '26", 3.0, season=True)]},
    "in_hold": {"label": "India Invested", "kind": "watchlist", "stocks": 3, "companies": 2,
                "yoy": [{**_row("Sep '26", 5.0), "ni": None, "eps": None}], "qoq": []}}}
PRICES = {t: [(d, 100.0 + i) for i, d in enumerate(DAYS)] for t in ("ACME.NS", "ZED.NS", "^CRSLDX", "SPY", "ACME")}
MARKET_READ = {"generated_at": "2026-10-31T04:36:40Z", "indexes": {
    "INDIA": {"label": "Nifty 500", "entries": [
        {"session": "2026-10-29", "written": "2026-10-30 11:20 UTC", "model": "models/gemini-3.8-flash", "headline": "Older Nifty headline",
         "breadth": "old breadth", "highs_lows": None, "drawdown": None, "return": None},
        {"session": "2026-10-30", "written": "2026-10-31 04:30 UTC", "model": "models/gemini-3.8-flash", "headline": "Nifty breadth is weak",
         "breadth": "Only 31.8% above the 200-day average.", "highs_lows": "Lows outnumber highs for 12 sessions.", "drawdown": None,
         "return": "The list trails over a month."}]},
    "US": {"label": "S&P 500", "entries": [
        {"session": "2026-10-30", "written": "2026-10-31 04:30 UTC", "model": "models/gemini-3.6-flash", "headline": "S&P 500 holds broad strength",
         "breadth": "59.2% above the 200-day average.", "highs_lows": None, "drawdown": "Healing.", "return": None}]}}}


class DB:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def watchlists(self):
        return [dict(w) for w in LISTS]

    def watchlist_members(self, name):
        return sorted(MEMBERS.get(name, []))

    def breadth_series(self):
        return [dict(s) for s in SERIES]

    def quarterly_results(self):
        return QUARTERLY

    def market_read(self):
        return MARKET_READ

    def breadth_points(self, series, since):
        return [p for p in POINTS.get(series, []) if p["date"] >= since]

    def prices(self, tickers, since):
        return {t: [p for p in PRICES[t] if p[0] >= since] for t in tickers if t in PRICES}

    def rows(self, tickers):
        return {t: MARKET[t] for t in tickers if t in MARKET}

    def known_tickers(self, tickers):
        return {t for t in tickers if t in MARKET or t in PRICES}

    def alias_ticker(self, key):
        return None

    def listings_exact(self, key):
        return []

    def listings_starting(self, key):
        return []

    def listings_similar(self, key):
        return []

    def calc_settings(self):
        return {}

    def yahoo_symbols(self):
        return {}


db = DB()

# --- loose names ------------------------------------------------------------------------------------------
for text, want in (("India Invested", "in_hold"), ("in_hold", "in_hold"), ("my india holdings", "in_hold"),
                   ("US portfolio", "us_hold"), ("india watchlist", "in_ideas"), ("all invested", "all_hold"),
                   ("outperforming market", "subs"), ("substack", "subs")):
    got, _ = mls.find_watchlist(LISTS, text)
    check(got == want, f"find_watchlist({text!r}) -> {want} ({got})")
got, choices = mls.find_watchlist(LISTS, "my holdings")
check(got is None and choices == ["India Invested", "US Invested", "All Invested"],
      "an ambiguous name matches nothing and lists the lists it could mean")
check(mls.find_watchlist(LISTS, "")[0] is None and mls.find_watchlist([], "x")[0] is None, "an empty name or no lists: no match")

# --- list_watchlists ----------------------------------------------------------------------------------------
body, err = mcp.call_tool("list_watchlists", {}, db=db)
check(not err and [w["label"] for w in body["watchlists"]] == [w["label"] for w in LISTS]
      and body["watchlists"][0]["breadth_latest"] == {"pct_above": 80.0} and body["watchlists"][1]["breadth_latest"] is None
      and body["indexes"] == [{"series": "INDIA", "label": "Nifty 500", "breadth_latest": {"pct_above": 30.0}}],
      "list_watchlists: every list in tab order with its breadth, and the indexes")

# --- get_watchlist ------------------------------------------------------------------------------------------
body, err = mcp.call_tool("get_watchlist", {"name": "my india holdings", "groups": ["price", "returns"], "sort_by": "perf_1w"}, db=db)
check(not err and body["found"] and body["label"] == "India Invested" and body["stocks"] == 3
      and [r["ticker"] for r in body["rows"]] == ["ACME.NS", "ZED.NS"] and body["without_data"] == ["NODATA.NS"],
      "get_watchlist: a row per stock with data, sorted by the field, best first; members without data listed")
row = body["rows"][0]
check(row["Perf 1W %"] == 4.0 and row["% Chg"] == 1.5 and row["price_date"] == DAYS[-1] and "Trend" not in row
      and body["sorted_by"] == "Perf 1W %", "rows carry the screener's labels for the chosen groups only")
check(body["summary"]["% Chg"] == {"stocks": 2, "up": 1, "down": 1, "median": -0.25, "average": -0.25}
      and body["summary"]["Perf YTD %"]["stocks"] == 1, "the summary counts every stock with the figure: up, down, median, average")
check("Perf 1W %" in body["definitions"] and body["breadth_latest"] == {"pct_above": 80.0},
      "the definitions of the fields shown and the list's breadth come along")
body, _ = mcp.call_tool("get_watchlist", {"name": "India Watchlist", "sort_by": "% Chg", "descending": False, "limit": 1}, db=db)
check([r["ticker"] for r in body["rows"]] == ["ZED.NS"] and body["rows_shown"] == 1 and "Trend" in body["rows"][0]
      and body["summary"]["% Chg"]["stocks"] == 2, "worst first by label, limited; the summary still covers the whole list; default groups include trend")
body, _ = mcp.call_tool("get_watchlist", {"name": "All Invested", "sort_by": "perf_ytd"}, db=db)
check([r["ticker"] for r in body["rows"]] == ["ACME.NS", "ACME", "ZED.NS"], "a stock without the sort figure goes last")
body, err = mcp.call_tool("get_watchlist", {"name": "my holdings"}, db=db)
check(not err and body["found"] is False and "India Invested" in body["available"], "an ambiguous name: found false with the choices")
check(mcp.call_tool("get_watchlist", {}, db=db)[1] is True, "no name: an error the model can read")
check(mcp.call_tool("get_watchlist", {"name": "India Watchlist", "groups": ["trend"]}, db=db)[0]["groups"] == ["trend", "stage"]
      and mcp.call_tool("get_watchlist", {"name": "India Watchlist"}, db=db)[0]["groups"] == ["price", "returns", "trend", "stage"],
      "a watchlist table with trend carries stage too, the default one included")
check(mcp.call_tool("list_watchlists", {}, db=None)[1] is True, "no database: an error, not an exception")
check(mcp.call_tool("get_market_read", {}, db=None)[1] is True, "get_market_read with no database: an error, not an exception")

# --- get_quarterly_results (2026-10-30) ------------------------------------------------------------------
body, err = mcp.call_tool("get_quarterly_results", {"name": "Nifty 500"}, db=db)
check(not err and body["found"] and body["series"] == "INDIA" and body["basis"] == "YOY"
      and [p[0] for p in body["points"]] == ["Mar '26", "Jun '26", "Sep '26"]
      and body["points"][-1][1:5] == [18.4, 19.4, 20.4, 15.0] and body["points"][-1][5] == [40, 39, 38, 30]
      and body["points"][-1][6:] == [50, True, 119] and len(body["columns"]) == len(body["points"][0]),
      "quarterly: an index by name, YoY by default -- quarter, the four medians, companies in each, reported, "
      "still being reported, Yahoo's missing base quarters")
body, _ = mcp.call_tool("get_quarterly_results", {"name": "nifty 500", "basis": "QoQ", "quarters": 1}, db=db)
check(body["basis"] == "QOQ" and [p[0] for p in body["points"]] == ["Sep '26"], "quarterly: QoQ, and the last N quarters")
body, _ = mcp.call_tool("get_quarterly_results", {"name": "india holdings"}, db=db)
check(body["found"] and body["series"] == "in_hold" and body["points"][0][1] == 5.0 and body["points"][0][2] is None,
      "quarterly: a watchlist by loose name; a thin figure reads null, not 0")
body, _ = mcp.call_tool("get_quarterly_results", {"name": "dow jones"}, db=db)
check(body["found"] is False and "Nifty 500" in body["available"], "quarterly: unknown -> found false with what is available")
body, err = mcp.call_tool("get_quarterly_results", {}, db=db)
check(err and "name" in body["error"], "quarterly: no name is an error")
body, _ = mcp.call_tool("get_quarterly_results", {"name": "Nifty 500", "quarters": 999}, db=db)
check(len(body["points"]) == 3, "quarterly: quarters is capped")
# --- get_market_read (2026-10-31) -----------------------------------------------------------------------
body, err = mcp.call_tool("get_market_read", {}, db=db)
by_idx = {i["index"]: i["reads"] for i in body["indexes"]}
check(not err and body["found"] and set(by_idx) == {"Nifty 500", "S&P 500"} and body["as_of"] == "2026-10-31T04:36:40Z",
      "market read: no name gives every index that has one")
nifty = by_idx["Nifty 500"]
check(len(nifty) == 1 and nifty[0]["session"] == "2026-10-30" and nifty[0]["headline"] == "Nifty breadth is weak"
      and nifty[0]["model"] == "gemini-3.8-flash" and set(nifty[0]) == {"session", "written", "model", "headline", "breadth", "highs_lows", "return"},
      f"market read: the latest read by default, the model without its models/ prefix, empty parts left out ({sorted(nifty[0])})")
body, _ = mcp.call_tool("get_market_read", {"name": "nifty 500", "history": 3}, db=db)
check([r["session"] for r in body["indexes"][0]["reads"]] == ["2026-10-30", "2026-10-29"] and len(body["indexes"]) == 1,
      "market read: an index by loose name, history gives the earlier reads newest first")
body, _ = mcp.call_tool("get_market_read", {"name": "S&P", "history": 99}, db=db)
check(body["indexes"][0]["index"] == "S&P 500" and len(body["indexes"][0]["reads"]) == 1, "market read: S&P by alias; history capped")
body, _ = mcp.call_tool("get_market_read", {"name": "Nifty 50"}, db=db)
check(body["found"] is False and sorted(body["available"]) == ["Nifty 500", "S&P 500"],
      "market read: an index with no read is found false, listing the two that have one")
body, _ = mcp.call_tool("get_market_read", {"name": "my india holdings"}, db=db)
check(body["found"] is False, "market read: a watchlist name is not an index")
MARKET_READ_SAVED, MARKET_READ = MARKET_READ, {}
body, err = mcp.call_tool("get_market_read", {}, db=db)
check(not err and body["found"] is False and body["indexes"] == [], "market read: nothing stored yet is an empty answer, not an error")
MARKET_READ = MARKET_READ_SAVED
check("analysis, not advice" in mcp.call_tool("get_market_read", {}, db=db)[0]["note"]
      and "get_market_read" in mcp.INSTRUCTIONS, "market read: the reply says it is analysis, and the connector's instructions name the tool")

_qr_saved = QUARTERLY
QUARTERLY = {}
body, _ = mcp.call_tool("get_quarterly_results", {"name": "Nifty 500"}, db=db)
check(body["found"] is False, "quarterly: nothing published yet -> found false, no error")
QUARTERLY = _qr_saved

# --- get_market_breadth -------------------------------------------------------------------------------------
body, _ = mcp.call_tool("get_market_breadth", {"name": "Nifty 500", "days": 30}, db=db)
check(body["found"] and body["series"] == "INDIA" and len(body["points"]) == 31 and body["points"][-1] == [DAYS[-1], 50.0, 40.0, 1.0, 2.0]
      and len(body["columns"]) == 5, "an index by name: the last N days, [date, %200, %50, highs, lows]")
body, _ = mcp.call_tool("get_market_breadth", {"name": "india holdings"}, db=db)
check(body["series"] == "in_hold" and len(body["points"]) == 366 and body["days"] == 365, "a watchlist by loose name; a year by default")
body, _ = mcp.call_tool("get_market_breadth", {"name": "nifty", "days": 99999}, db=db)
check(body["days"] == mls.MAX_BREADTH_DAYS, "days is capped")
body, _ = mcp.call_tool("get_market_breadth", {"name": "dow jones"}, db=db)
check(body["found"] is False and "Nifty 500" in body["available"], "unknown: found false with what is available")
body, _ = mcp.call_tool("get_market_breadth", {"name": "us invested"}, db=db)
check(body["found"] is False, "a list with no breadth stored: found false")
# The drawdown columns (2026-10-30): named only where the table has them and the series has values.
body, _ = mcp.call_tool("get_market_breadth", {"name": "Nifty 500", "days": 30}, db=db)
check(len(body["columns"]) == 5 and all(len(p) == 5 for p in body["points"]),
      "drawdown: before the columns exist (rows without them), the reply is the five columns, unchanged")
_saved = {k: [dict(p) for p in v] for k, v in POINTS.items()}
for p in POINTS["INDIA"]:
    p.update(pct_down_10=60.0, pct_down_20=30.0, pct_down_30=10.0, pct_down_40=1.0)
for p in POINTS["in_hold"]:
    p.update(pct_down_10=None, pct_down_20=None, pct_down_30=None, pct_down_40=None)
body, _ = mcp.call_tool("get_market_breadth", {"name": "Nifty 500", "days": 30}, db=db)
check(body["columns"][5:] == ["% 10%+ below 52-week high", "% 20%+ below 52-week high", "% 30%+ below 52-week high",
                              "% 40%+ below 52-week high"] and body["points"][-1][5:] == [60.0, 30.0, 10.0, 1.0],
      "drawdown: with the columns filled, each point carries % 10 / 20 / 30 / 40%+ below the 52-week high")
body, _ = mcp.call_tool("get_market_breadth", {"name": "india holdings"}, db=db)
check(len(body["columns"]) == 5, "drawdown: columns of nulls (a series not written yet) are left out, not read as 0%")
POINTS.clear()
POINTS.update(_saved)

# --- get_price_history --------------------------------------------------------------------------------------
asked = []


def fake_fetch(symbols, days):
    asked.append(list(symbols))
    return {"QUUX": [(DAYS[-2], 60.0), (DAYS[-1], 61.0)]}


body, err = mcp.call_tool("get_price_history", {"watchlist": "my india holdings", "days": 10}, db=db, fetch=fake_fetch)
check(not err and body["label"] == "India Invested" and set(body["closes"]) == {"ACME.NS", "ZED.NS", "^CRSLDX"}
      and body["missing"] == ["NODATA.NS"] and len(body["closes"]["ZED.NS"]) == 11 and not asked,
      "a watchlist: its stored closes plus its benchmark, the last N days; nothing fetched for it")
body, _ = mcp.call_tool("get_price_history", {"companies": ["nifty 500", "spy", "QUUX"], "days": 5}, db=db, fetch=fake_fetch,
                        yahoo=lambda name: None)
check(set(body["closes"]) == {"^CRSLDX", "SPY", "QUUX"} and body["fetched_from_yahoo"] == ["QUUX"] and asked == [["QUUX"]]
      and body["matched"] == {"nifty 500": "^CRSLDX", "spy": "SPY"},
      "companies: an index name is its benchmark, a stored ticker as typed, any other fetched from Yahoo")
body, _ = mcp.call_tool("get_price_history", {"watchlist": "All Invested", "days": 730}, db=db)
total = sum(len(v) for v in body["closes"].values())
check(body.get("sampled") is None and total == 3 * 401 and "^CRSLDX" not in body["closes"],
      "under the size limit every close is kept; a group with mixed benchmarks adds none")
real_max = mls.MAX_PRICE_POINTS
mls.MAX_PRICE_POINTS = 500
try:
    body, _ = mcp.call_tool("get_price_history", {"watchlist": "All Invested", "days": 730}, db=db)
finally:
    mls.MAX_PRICE_POINTS = real_max
check(body["sampled"].startswith("every ") and all(v[-1][0] == DAYS[-1] for v in body["closes"].values())
      and sum(len(v) for v in body["closes"].values()) <= 500 + 3,
      "over the limit: every Nth session counting back from the latest, which is always kept")
check(mcp.call_tool("get_price_history", {}, db=db)[1] is True, "neither companies nor a watchlist: an error")
body, _ = mcp.call_tool("get_price_history", {"watchlist": "my holdings"}, db=db)
check(body["found"] is False and body["available"], "an ambiguous watchlist: found false with the choices")

# --- the tool list and the public repo ----------------------------------------------------------------------
listed = mcp.handle_body(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}), db_factory=lambda: None)[1]
tools = {t["name"]: t for t in listed["result"]["tools"]}
check(all(n in tools for n in mcp.LIST_TOOLS) and "sort_by" in tools["get_watchlist"]["inputSchema"]["properties"]
      and "holdings" in tools["list_watchlists"]["description"], "the four list tools are offered, described for when to use them")
check("list_watchlists" in mcp.INSTRUCTIONS and "get_market_breadth" in mcp.INSTRUCTIONS, "the server's instructions point to them")
called = mcp.handle_body(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
    "name": "get_watchlist", "arguments": {"name": "india invested"}}}), db_factory=lambda: db)[1]
check(called["result"]["structuredContent"]["found"] and db.closed, "through JSON-RPC: structured content, database closed")

print(f"\nFAILURES: {fails}")
sys.exit(1 if fails else 0)
