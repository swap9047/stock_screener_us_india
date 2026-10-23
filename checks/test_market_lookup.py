"""market_lookup.py and api/mcp.py: a company name or ticker in, the screener's numbers out -- for the
research library's Q&A (api/market.py) and Claude chat's connector (the MCP server).

Offline: an in-memory stand-in for the database, invented listings (ACME, ZED.NS), compute_rows and
Yahoo's symbol search replaced, no network.
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "api"))

import market_api as ma  # noqa: E402
import market_lookup as ml  # noqa: E402
import mcp  # noqa: E402

ml.DETAILS_RETRY_WAIT_S = 0   # no real waits; the retry itself is checked below with the real value
fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def trigram_similarity(a, b):
    """pg_trgm's similarity() for two single-word keys, as the research library reproduces it."""
    tri = lambda k: {f"  {k} "[i:i + 3] for i in range(len(k) + 1)}  # noqa: E731
    ta, tb = tri(a), tri(b)
    return len(ta & tb) / len(ta | tb) if a and b else 0.0


def now_iso(hours_ago=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


class FakeDB:
    """MarketDB's queries over lists, answering as the SQL would."""

    def __init__(self, listings=(), aliases=None, market=None, calc=None):
        self.listings = [dict(l, match_key=ml.match_key(l["name"])) for l in listings]
        self.aliases = aliases or {}
        self.market = market or {}
        self.calc = calc or {}
        self.saved, self.closed = [], False
        self.symbols = {}

    def close(self):
        self.closed = True

    def known_tickers(self, tickers):
        return {t for t in tickers if t in self.market or any(l["ticker"] == t for l in self.listings)}

    def alias_ticker(self, key):
        return self.aliases.get(key)

    def listings_exact(self, key):
        return [l for l in self.listings if l["match_key"] == key]

    def listings_starting(self, key):
        return [l for l in self.listings if l["match_key"].startswith(key)][:20]

    def listings_similar(self, key):
        scored = [dict(l, score=trigram_similarity(l["match_key"], key)) for l in self.listings]
        return sorted([l for l in scored if l["score"] >= 0.45], key=lambda l: -l["score"])[:3]

    def rows(self, tickers):
        return {t: json.loads(json.dumps(self.market[t])) for t in tickers if t in self.market}

    def calc_settings(self):
        return self.calc

    def listings_by_ticker(self, tickers):
        return {l["ticker"]: l for l in self.listings if l["ticker"] in tickers}

    def yahoo_symbols(self):
        return dict(self.symbols)

    def save_yahoo_symbols(self, found):
        self.symbols.update(found)

    def save(self, records):
        self.saved += records
        for r in records:
            self.market[r["ticker"]] = r


LISTINGS = [
    {"ticker": "ZED.NS", "name": "Zed Industries Limited", "exchange": "NSE"},
    {"ticker": "500001.BO", "name": "ZED INDUSTRIES LTD.", "exchange": "BSE", "isin": "INE0ZED01011"},
    {"ticker": "500002.BO", "name": "Quill Foods Limited", "exchange": "BSE", "isin": "INE0QUL01012"},
    {"ticker": "500003.BO", "name": "Orbit Wires Limited", "exchange": "BSE", "isin": ""},
    {"ticker": "ZED", "name": "Zed Holdings Inc. - Common Stock", "exchange": "NASDAQ"},
    {"ticker": "LAURO.NS", "name": "Lauro Labs Limited", "exchange": "NSE"},
    {"ticker": "SYNCOREX.NS", "name": "Syncorex International Limited", "exchange": "NSE"},
    {"ticker": "ACMEP.NS", "name": "Acme Pharma Limited", "exchange": "NSE"},
    {"ticker": "ACMEC.NS", "name": "Acme Chemicals Limited", "exchange": "NSE"},
]
no_yahoo = lambda name: None  # noqa: E731

# --- match_key: the research library's own key ------------------------------------------------------
check(ml.match_key("Lauro Labs Limited") == ml.match_key("lauro laboratories") == ml.match_key("Lauro Labs") == "laurolaboratories",
      "match_key reads Labs as Laboratories and drops Limited, as the library does")
check(ml.match_key("Divi’s Labs") == "divislaboratories" and ml.match_key("M&M") == "mandm"
      and ml.match_key("Zed Holdings Inc.") == "zed" and ml.match_key("Nerée") == "neree",
      "apostrophes, accents, '&' and Holdings/Inc as knowledge_store.match_key")

# --- resolve: name or ticker -> ticker --------------------------------------------------------------
db = FakeDB(LISTINGS, aliases={"zeddy": "ZED.NS"})
cases = [
    ("zed", ("ZED.NS", "ticker"), "a bare symbol is read as NSE first, over a US listing with the same symbol"),
    ("500001.bo", ("500001.BO", "ticker"), "a BSE code with its suffix"),
    ("500001", ("500001.BO", "ticker"), "a bare BSE code finds its .BO listing"),
    ("Zeddy", ("ZED.NS", "decision"), "a name decision the library made comes first"),
    ("newco.bo", ("NEWCO.BO", "ticker"), "a ticker with its suffix is taken as typed, even one the listings don't hold"),
    ("540999", ("540999.BO", "ticker"), "a bare six-digit number is a BSE scrip code, listed or not"),
    ("Zed Industries", ("ZED.NS", "listing"), "a listing with the same name; NSE over BSE for one company"),
    ("Lauro Laboratories Ltd", ("LAURO.NS", "listing"), "the library's spelling rules apply (Labs = Laboratories)"),
    ("Syncorex Int", ("SYNCOREX.NS", "listing"), "the only company whose name starts with it"),
    ("Syncorex", ("SYNCOREX.NS", "ticker"), "a word that is also a known symbol is that symbol"),
    ("Syncorx International", ("SYNCOREX.NS", "similar"), "a misspelling: the clearly closest listing"),
]
for query, want, label in cases:
    got = ml.resolve(db, query, no_yahoo)
    check(got == want, f"{label} ({query!r} -> {got})")
check(ml.resolve(db, "Acme", no_yahoo) == (None, ""),
      "a name two companies start with is not guessed (Acme Pharma or Acme Chemicals?)")
check(ml.resolve(db, "Acme", {"Acme": "ACME"}.get) == ("ACME", "yahoo"), "...and goes to Yahoo's symbol search instead")
check(ml.resolve(db, "Zedd", no_yahoo)[1] != "listing", "a prefix shorter than five letters is never taken as the company")
check(ml.resolve(db, "Brand New Co", {"Brand New Co": "BNC.NS"}.get) == ("BNC.NS", "yahoo"),
      "a company outside every listing still resolves through Yahoo")
check(ml.resolve(db, "  ", no_yahoo) == (None, "") and ml.resolve(db, "Nothing Like It", no_yahoo) == (None, ""),
      "blank or unknown: no ticker")
check(ml.resolve(None, "acme", no_yahoo) == ("ACME", "ticker") and ml.resolve(None, "Acme Pharma", no_yahoo) == (None, "")
      and ml.resolve(None, "acme.bo", no_yahoo) == ("ACME.BO", "ticker") and ml.resolve(None, "500001", no_yahoo) == ("500001.BO", "ticker"),
      "without a database a ticker is taken as typed, and a name needs Yahoo")


class Broken(FakeDB):
    def listings_similar(self, key):
        raise RuntimeError("function similarity(text, text) does not exist")


check(ml.resolve(db, "Zeta Industries", no_yahoo) == (None, ""),
      "a name only loosely like a listing (Zeta / Zed Industries) is not taken")
check(ml.resolve(Broken(LISTINGS), "Syncorx International", {"Syncorx International": "SYNCOREX.NS"}.get)
      == ("SYNCOREX.NS", "yahoo"), "a database without pg_trgm falls back to Yahoo instead of failing")

# --- BSE: Yahoo prices SYMBOL.BO, never the scrip code the library's listings carry -------------------
searches = []


def search(text):
    searches.append(text)
    return {"INE0ZED01011": [{"symbol": "ZEDIND.NS", "exchange": "NSI", "quoteType": "EQUITY"},
                             {"symbol": "500001.BO", "exchange": "BSE", "quoteType": "EQUITY"},
                             {"symbol": "ZEDIND.BO", "exchange": "BSE", "quoteType": "EQUITY"}],
            "Orbit Wires Limited": [{"symbol": "ORBITW.BO", "exchange": "BSE", "quoteType": "EQUITY"}],
            "Quill Foods Limited": [{"symbol": "QUILL.BO", "exchange": "BSE", "quoteType": "EQUITY"}]}.get(text, [])


db = FakeDB(LISTINGS)
got = ml.price_symbols(db, ["500001.BO", "500002.BO", "500003.BO", "ZED.NS", "ACME"], search)
check(got == {"500001.BO": "ZEDIND.BO", "500002.BO": "500002.BO", "500003.BO": "ORBITW.BO", "ZED.NS": "ZED.NS", "ACME": "ACME"},
      f"a BSE scrip code is priced under Yahoo's BSE symbol for its ISIN (never a numeric or NSE one); by name only "
      f"without an ISIN; not found, it stays as it is; other tickers as they are ({got})")
check(searches == ["INE0ZED01011", "INE0QUL01012", "Orbit Wires Limited"] and db.symbols == {"500001.BO": "ZEDIND.BO", "500003.BO": "ORBITW.BO"},
      "a listing with an ISIN is searched by ISIN only (a name could find another company); found symbols are remembered")
searches.clear()
ml.price_symbols(db, ["500001.BO", "500003.BO", "ZED.NS"], search)
check(searches == [], "a remembered symbol is not searched again")
check(ml.price_symbols(None, ["ZED.NS"], search) == {"ZED.NS": "ZED.NS"} and ml.price_symbols(None, ["500001.BO"], search) == {"500001.BO": "500001.BO"},
      "without a database nothing is searched for a code it can't look up")

priced_calls = []


def bse_compute(tickers, settings=None):
    priced_calls.append(list(tickers))
    return {t: {"ticker": t, "company_name": f"{t} co", "last_close": 10.0, "data_end": "2026-10-20"}
            for t in tickers if t not in ("NEWCO.NS", "500002.BO")}


rows = ml.compute_priced(["500001.BO", "NEWCO.NS", "ZED.NS", "500002.BO"], {}, db=db, compute=bse_compute, search=search)
check(priced_calls == [["ZEDIND.BO", "NEWCO.NS", "ZED.NS", "500002.BO"], ["NEWCO.BO"]],
      f"computed under Yahoo's symbols; an NSE listing that gives no row is tried once on BSE ({priced_calls})")
check({t: r["priced_as"] for t, r in rows.items()} == {"500001.BO": "ZEDIND.BO", "NEWCO.NS": "NEWCO.BO", "ZED.NS": "ZED.NS"},
      "rows are keyed by the ticker asked, with the symbol their prices came from")
rec = ml.priced_record("500001.BO", rows["500001.BO"], "library")
check(rec["ticker"] == "500001.BO" and (rec["exchange"], rec["currency"]) == ("BSE", "INR")
      and ml.priced_record("NEWCO.NS", rows["NEWCO.NS"], "live")["exchange"] == "BSE",
      "the record keeps the library's ticker and names the exchange the prices are really from")

priced_calls.clear()
db = FakeDB(LISTINGS)
out = ml.lookup(["500001", "NEWCO.NS", "500002"], db=db, compute=bse_compute, yahoo=no_yahoo, search=search)
check([(r["ticker"], r["found"], r.get("priced_as")) for r in out]
      == [("500001.BO", True, "ZEDIND.BO"), ("NEWCO.NS", True, "NEWCO.BO"), ("500002.BO", False, None)]
      and [r["ticker"] for r in db.saved] == ["500001.BO", "NEWCO.NS"],
      "lookup: a scrip code is found and saved under it; a BSE code Yahoo has no symbol for is a miss")
check("try SYMBOL.BO" in out[2]["note"], "...whose note says to try the BSE symbol")
lab = ml.labelled(out[0], ["price"])
check(lab["priced_as"] == "ZEDIND.BO" and "Yahoo's ZEDIND.BO (BSE)" in lab["note"] and "priced_as" not in ml.labelled(dict(out[0], priced_as="500001.BO"), ["price"]),
      "the answer says which Yahoo symbol the prices are from, when it isn't the ticker")

# --- Yahoo's company details failing: retried once after 5 s, then the stored values kept -----------
waits = []
real_sleep = ml.time.sleep
ml.time.sleep = waits.append
attempts = []


def flaky_compute(tickers, settings=None):
    attempts.append(list(tickers))
    first = len(attempts) == 1
    out = {}
    for t in tickers:
        ok = t == "ZED.NS" or (t == "FLAKY.NS" and not first)
        out[t] = {"ticker": t, "company_name": f"{t} Limited" if ok else t, "last_close": 50.0, "data_end": "2026-10-20",
                  **({"trailing_pe": 20.0, "roe": 12.0} if ok else {})}
    return out


try:
    rows = ml.compute_priced(["ZED.NS", "FLAKY.NS", "DEAD.NS"], {}, compute=flaky_compute, details_retry_wait=5)
finally:
    ml.time.sleep = real_sleep
check(attempts == [["ZED.NS", "FLAKY.NS", "DEAD.NS"], ["FLAKY.NS", "DEAD.NS"]] and waits == [5],
      f"only the tickers whose details failed are computed again, once, 5 s later ({attempts}, {waits})")
check(rows["FLAKY.NS"]["trailing_pe"] == 20.0 and not rows["FLAKY.NS"]["details_failed"]
      and rows["DEAD.NS"]["details_failed"] and not rows["ZED.NS"]["details_failed"],
      "a retry that got the details is used; one that didn't leaves the row marked")
check(ml.compute_priced(["DEAD.NS"], {}, compute=lambda t, settings=None: {"DEAD.NS": {"ticker": "DEAD.NS", "company_name": "DEAD.NS"}},
                        details_retry_wait=None)["DEAD.NS"]["details_failed"], "details_retry_wait=None: no retry")

old_row = {"ticker": "DEAD.NS", "company_name": "Dead Industries Limited",
           "metrics": {"trailing_pe": 31.0, "roe": 9.5, "roce": 14.0, "reported_qtr": "Q1 FY27", "last_close": 40.0}}
rec = ml.priced_record("DEAD.NS", rows["DEAD.NS"], "library")
kept = ml.keep_previous(rec, old_row, True)
check(rec["company_name"] == "Dead Industries Limited" and rec["metrics"]["trailing_pe"] == 31.0 and rec["metrics"]["roce"] == 14.0
      and rec["metrics"]["last_close"] == 50.0 and set(kept) == {"trailing_pe", "roe", "roce", "reported_qtr", "company_name"},
      f"details still missing: the name, valuation and growth come from the stored row; the prices stay new ({kept})")
rec = {"ticker": "LOSS.NS", "company_name": "Loss Co", "metrics": {"forward_pe": 40.0}}
kept = ml.keep_previous(rec, {"ticker": "LOSS.NS", "company_name": "Loss Co", "metrics": {"trailing_pe": 18.0, "roce": 7.0}}, False)
check(kept == ["roce"] and "trailing_pe" not in rec["metrics"] and rec["metrics"]["roce"] == 7.0,
      "details fine: a blank P/E is Yahoo's answer (a loss-making year) and stays blank; a blank statement figure is kept")
check(ml.keep_previous({"ticker": "N", "company_name": "N", "metrics": {}}, None, True) == [], "nothing stored: nothing kept")

db = FakeDB(LISTINGS, market={"DEAD.NS": dict(old_row, as_of=now_iso(30), source="library", ai={})})
out = ml.lookup(["DEAD.NS"], db=db, compute=lambda t, settings=None: {"DEAD.NS": {"ticker": "DEAD.NS", "company_name": "DEAD.NS",
                                                                                  "last_close": 41.0, "data_end": "2026-10-22"}},
                yahoo=no_yahoo)
saved = db.saved[-1]
check(saved["company_name"] == "Dead Industries Limited" and saved["metrics"]["trailing_pe"] == 31.0 and saved["metrics"]["last_close"] == 41.0,
      "a live lookup whose details failed twice saves the new prices with the stored name and fundamentals")

import publish_market as pm  # noqa: E402

real_db = pm.market_lookup.MarketDB
pm.market_lookup.MarketDB = lambda conn: FakeDB(market={"DEAD.NS": old_row})
try:
    recs = [{"ticker": "DEAD.NS", "company_name": "DEAD.NS", "metrics": {"last_close": 41.0}},
            {"ticker": "NEW.NS", "company_name": "New Limited", "metrics": {"last_close": 5.0}}]
    n_kept = pm.keep_previous_values(object(), recs, {"DEAD.NS": True, "NEW.NS": False})
finally:
    pm.market_lookup.MarketDB = real_db
check(n_kept == 1 and recs[0]["metrics"]["trailing_pe"] == 31.0 and recs[0]["company_name"] == "Dead Industries Limited"
      and pm.keep_previous_values(object(), [], {}) == 0,
      "the publish jobs fill blanks from the stored rows and count the tickers that kept something")

# --- staleness -------------------------------------------------------------------------------------
check(not ml.is_stale({"as_of": now_iso(1)}) and ml.is_stale({"as_of": now_iso(ml.STALE_HOURS + 1)})
      and ml.is_stale({"as_of": None}) and ml.is_stale({}) and not ml.is_stale({"as_of": now_iso(1)[:19]}),
      "a row is stale past STALE_HOURS, or with no readable as_of (a naive time is UTC)")

# --- lookup: stored while fresh, computed and saved otherwise ---------------------------------------
row = {"ticker": "ZED.NS", "company_name": "Zed Industries Limited", "last_close": 101.5, "rsi14_weekly": 61.2,
       "trailing_pe": 24.0, "trend": "Uptrend", "tech_uptrend": 3, "ta_rules": "Maintain/Add", "data_end": "2026-10-20",
       "market": "india_invested", "note": "core holding", "flag": "Green"}
calls = []


def compute(tickers, settings=None):
    calls.append((list(tickers), settings))
    return {t: dict(row, ticker=t, company_name=next((l["name"] for l in LISTINGS if l["ticker"] == t), t))
            for t in tickers if not t.startswith(("GONE.", "NEWCO.NS", "500009"))}


fresh_rec = ma.public_record(dict(row, ticker="LAURO.NS"), "screener", as_of=now_iso(1))
old_rec = ma.public_record(row, "screener", fundamental={"sentiment": "Negative", "reasoning": "r"}, as_of=now_iso(30))
old_rec["ai"]["expert"] = {"verdict": "AVOID", "headline": "h"}
db = FakeDB(LISTINGS, market={"LAURO.NS": fresh_rec, "ZED.NS": old_rec}, calc={"rsi_period": 9, "not_a_setting": 1})
out = ml.lookup(["Lauro Labs", "zed", "Zed Industries", "Nothing Like It", "GONE.NS"], db=db, compute=compute,
                yahoo={"GONE.NS": "GONE.NS"}.get)
check([r["query"] for r in out] == ["Lauro Labs", "zed", "Zed Industries", "Nothing Like It", "GONE.NS"],
      "one result per query, in the order asked")
check(out[0]["found"] and not out[0]["live"] and out[0]["record"]["source"] == "screener",
      "a fresh stored row is used as it is")
check([c[0] for c in calls] == [["ZED.NS", "GONE.NS"], ["GONE.BO"]],
      f"only stale or missing tickers are computed, each once though asked twice (and the NSE miss once on BSE) "
      f"({[c[0] for c in calls]})")
check(calls[0][1]["rsi_period"] == 9 and "not_a_setting" not in calls[0][1],
      "computed with the screener's saved calc settings (unknown keys ignored)")
zed = out[1]["record"]
check(out[1]["live"] and out[2]["record"] is zed and zed["source"] == "live" and zed["as_of"] > old_rec["as_of"],
      "a stale row is computed now")
check(zed["ai"] == old_rec["ai"] and zed["metrics"]["expert_take"] == ma.columns_verdict(dict(row, sentiment="Negative")),
      "a recomputed screener ticker keeps its AI write-ups and its guarded Sentiment, so Expert Take matches the dashboard")
check([r["ticker"] for r in db.saved] == ["ZED.NS"] and "india_invested" not in json.dumps(db.saved)
      and "core holding" not in json.dumps(db.saved), "the computed row is saved, with nothing private in it")
check(not out[3]["found"] and out[3]["ticker"] is None and "ABC.NS" in out[3]["note"],
      "an unknown name says how to ask by ticker")
check(not out[4]["found"] and out[4]["ticker"] == "GONE.NS" and "no price data" in out[4]["note"],
      "a ticker Yahoo has no prices for says so")

calls.clear()
ml.lookup(["Lauro Labs"], refresh=True, db=db, compute=compute, yahoo=no_yahoo)
check(calls and calls[0][0] == ["LAURO.NS"], "refresh recomputes even a fresh row")
calls.clear()
nodb = ml.lookup(["acme"], db=None, compute=compute, yahoo=no_yahoo)
check(nodb[0]["found"] and nodb[0]["live"] and calls[0][1]["rsi_period"] == ml.screener_settings(None)["rsi_period"],
      "without a database: computed with the default settings, nothing stored")
check(len(ml.lookup(["a", "b", "c", "d", "e", "f", ""], db=None, compute=lambda t, settings=None: {}, yahoo=no_yahoo))
      == ml.MAX_QUERIES, "at most MAX_QUERIES per lookup")

# --- labelled: what a model reads -------------------------------------------------------------------
cat = ma.catalog(ml.screener_settings(None))
labels = {c["field"]: c["label"] for c in cat}
lab = ml.labelled(out[1], ["price", "explain", "ai_views"], catalog=cat)
check(lab["metrics"]["price"][labels["last_close"]] == 101.5 and lab["price_date"] == "2026-10-20"
      and lab["company"] == "Zed Industries Limited" and lab["currency"] == "INR",
      "metrics under the screener's labels, with the price date and currency")
check(labels["last_close"] in lab["definitions"] and not any(labels[f] in lab["definitions"] for f in lab["metrics"]["explain"]
                                                             if f in labels) and "trend_why" not in lab["definitions"],
      "a definition for every metric shown, none for the explain text")
check(lab["ai_views"] == old_rec["ai"] and "ai_views" not in ml.labelled(out[1], ["price"], catalog=cat),
      "AI write-ups only when asked")
check("matched_by" not in lab and "Yahoo" in ml.labelled(dict(out[1], how="yahoo"), ["price"], catalog=cat)["matched_by"],
      "a Yahoo or closest-name match asks the reader to check the company")
check(ml.labelled(out[3]) == {"query": "Nothing Like It", "found": False, "ticker": None, "note": out[3]["note"]},
      "a miss carries its note")

# --- api/mcp.py: the MCP server ---------------------------------------------------------------------
dbs = []


def factory():
    dbs.append(FakeDB(LISTINGS, market={"LAURO.NS": fresh_rec}))
    return dbs[-1]


kw = {"db_factory": factory, "compute": compute, "yahoo": no_yahoo}
status, init = mcp.handle_body(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}), **kw)
check(status == 200 and init["id"] == 1 and init["result"]["protocolVersion"] == "2025-03-26"
      and "tools" in init["result"]["capabilities"] and "get_market_metrics" in init["result"]["instructions"],
      "initialize: the client's protocol version, a tools capability and instructions")
check(mcp.handle_body(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "1999-01-01"}}), **kw)[1]["result"]["protocolVersion"]
      == mcp.PROTOCOL_VERSIONS[0], "an unknown protocol version gets the newest this server speaks")
check(mcp.handle_body(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), **kw) == (202, None),
      "a notification is accepted with no body")
status, listed = mcp.handle_body(json.dumps({"jsonrpc": "2.0", "id": "a", "method": "tools/list"}), **kw)
tools = {t["name"]: t for t in listed["result"]["tools"]}
desc = tools["get_market_metrics"]["description"]
check(set(tools) == {"get_market_metrics", "metric_catalog"} and all(w in desc for w in ("NSE", "BSE", "price", "RSI", "before web search")),
      "tools/list: the description says what it is for, so Claude picks it outside a Project too")
check(tools["get_market_metrics"]["inputSchema"]["properties"]["groups"]["items"]["enum"] == list(ma.GROUP_NAMES)
      and all(t["annotations"]["readOnlyHint"] for t in tools.values()), "groups are the published ones; both tools read-only")
check(not any(w in json.dumps(listed) for w in ("ZED", "LAURO", "SYNCOREX")), "no ticker in the tool list (this repo is public)")

dbs.clear()
calls.clear()
status, called = mcp.handle_body(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
    "name": "get_market_metrics", "arguments": {"companies": ["Lauro Labs", "Zed Industries", "Nothing Like It"],
                                                "groups": ["price", "valuation", "bogus"]}}}), **kw)
res = called["result"]
body = res["structuredContent"]
check(status == 200 and not res["isError"] and json.loads(res["content"][0]["text"]) == body,
      "tools/call: the same JSON as text and as structured content")
check([r["found"] for r in body["results"]] == [True, True, False] and set(body["results"][0]["metrics"]) == {"price", "valuation"},
      "results per company, only the asked groups (unknown names ignored), misses included")
check(calls and calls[0][0] == ["ZED.NS"] and [r["ticker"] for r in dbs[0].saved] == ["ZED.NS"] and dbs[0].closed,
      "a company with no stored row is computed and saved; the connection is closed after the call")
check("india_invested" not in res["content"][0]["text"] and "core holding" not in res["content"][0]["text"],
      "the connector publishes nothing private")
one = mcp.handle_body(json.dumps({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
    "name": "get_market_metrics", "arguments": {"companies": "Lauro Labs"}}}), **kw)[1]["result"]
check(not one["isError"] and one["structuredContent"]["results"][0]["ticker"] == "LAURO.NS",
      "a single company passed as a string is accepted")
cat_reply = mcp.handle_body(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                        "params": {"name": "metric_catalog", "arguments": {}}}), **kw)[1]["result"]
check(not cat_reply["isError"] and len(cat_reply["structuredContent"]["metrics"]) == len(cat)
      and all(m["description"] for m in cat_reply["structuredContent"]["metrics"]), "metric_catalog: every metric with its definition")
for args, why in (({}, "no companies"), ({"companies": []}, "an empty list")):
    r = mcp.handle_body(json.dumps({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                                    "params": {"name": "get_market_metrics", "arguments": args}}), **kw)[1]["result"]
    check(r["isError"] and "companies" in r["content"][0]["text"] and "structuredContent" not in r,
          f"{why}: a tool error the model can read and correct")


def failing(tickers, settings=None):
    raise RuntimeError("Yahoo refused")


dbs.clear()
r = mcp.handle_body(json.dumps({"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {
    "name": "get_market_metrics", "arguments": {"companies": ["Zed Industries"]}}}),
    db_factory=factory, compute=failing, yahoo=no_yahoo)[1]["result"]
check(r["isError"] and "Yahoo refused" in r["content"][0]["text"] and dbs[0].closed,
      "a failed computation is a tool error, and the connection is still closed")
r = mcp.handle_body(json.dumps({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                                "params": {"name": "nope", "arguments": {}}}), **kw)[1]["result"]
check(r["isError"] and "unknown tool" in r["content"][0]["text"], "an unknown tool is a tool error")
check(mcp.handle_body(json.dumps({"jsonrpc": "2.0", "id": 13, "method": "resources/list"}), **kw)[1]["error"]["code"] == -32601
      and mcp.handle_body(b"{not json", **kw)[0] == 400
      and mcp.handle_body(json.dumps({"id": 14, "method": "ping"}), **kw)[1]["error"]["code"] == -32600
      and mcp.handle_body(json.dumps({"jsonrpc": "2.0", "id": 15, "method": "ping"}), **kw)[1]["result"] == {},
      "unknown method -32601, bad JSON 400, not JSON-RPC 2.0 -32600, ping {}")
status, batch = mcp.handle_body(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                            {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]), **kw)
check(status == 200 and [m["id"] for m in batch] == [1, 2], "a batch gets one reply per request, none for notifications")

# --- over HTTP, as Vercel serves it: the token in the path -----------------------------------------
os.environ.pop("MARKET_DB_URL", None)
os.environ["MARKET_API_TOKEN"] = "s3cret"
class QuietHandler(mcp.handler):
    def log_message(self, *a):
        pass


server = HTTPServer(("127.0.0.1", 0), QuietHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_address[1]}"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(path, payload):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    try:
        with opener.open(req, timeout=10) as resp:
            return resp.status, resp.read().decode(), resp.headers.get("Content-Type")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers.get("Content-Type")


try:
    status, text, ctype = post("/mcp/s3cret", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    check(status == 200 and ctype == "application/json" and "get_market_metrics" in text, "POST /mcp/<token>: served")
    check(post("/mcp/wrong", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0] == 401
          and post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0] == 401,
          "a wrong or missing token: 401")
    check(post("/mcp/s3cret", {"jsonrpc": "2.0", "method": "notifications/initialized"})[:2] == (202, ""),
          "a notification: 202, empty")
    try:
        opener.open(base + "/mcp/s3cret", timeout=10)
        get_status = 200
    except urllib.error.HTTPError as e:
        get_status = e.code
    check(get_status == 405, "GET (an event stream) is 405: this server only answers POSTs")
finally:
    server.shutdown()
    os.environ.pop("MARKET_API_TOKEN", None)

print(f"\n{'FAILED' if fails else 'OK'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
