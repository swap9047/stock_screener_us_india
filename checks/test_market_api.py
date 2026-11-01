"""market_api: what the screener shares with other apps, and api/market.py, the Vercel endpoint.
(market_lookup.py and api/mcp.py, Claude chat's connector: test_market_lookup.py.)

The research library's Q&A and Claude chat read the screener's metrics from the knowledge graph's
database, and api/market.py computes a ticker that isn't there yet. What may leave this repo is a
whitelist (METRIC_GROUPS): the watchlist a ticker sits in, notes, flags, Interested and custom
columns say what is held, and must never be published.

Offline: invented tickers and rows, fetch_snapshot replaced, no network.
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "api"))

import market_api as ma
import stock_data as sd

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


settings = dict(sd.DEFAULT_SETTINGS)

# --- the catalog ------------------------------------------------------------------
cat = ma.catalog(settings)
fields = [c["field"] for c in cat]
check(len(fields) == len(set(fields)), "every catalog field appears once")
check(set(ma.GROUP_NAMES) == {c["group"] for c in cat}, "every group has catalog entries")
check(all(c["label"] and c["description"] for c in cat), "every entry has a label and a description")
labels = {c["field"]: c["label"] for c in cat}
w = settings["ema_weekly"]
check(labels["ema40"] == f"{w[2]} WEMA", f"moving-average labels follow the settings ({labels['ema40']})")
filterable = {v: k for k, v in sd.get_filterable_metrics(settings).items()}
mismatched = [f for f in ma.PUBLIC_FIELDS if f in filterable and filterable[f] != labels[f]]
check(not mismatched, f"labels match the screener's own (get_filterable_metrics): {mismatched}")

# --- the whitelist keeps what is held private ----------------------------------------
PRIVATE = ("market", "note", "flag", "flag_reason", "interested", "custom_w52dist", "matched_alerts")
check(not set(PRIVATE) & set(ma.PUBLIC_FIELDS), "no private field is in the catalog")
_val = {f: u for f, _l, u, _d in ma.METRIC_GROUPS["valuation"][1]}
check(_val.get("market_cap") == "money" and "shares_outstanding" in _val,
      "the valuation group carries the market cap (unit money) and shares outstanding")
row = {"ticker": "ZED.NS", "company_name": "Zed Industries Limited", "last_close": 101.5, "pct_change_1d": float("nan"),
       "rsi14_weekly": 61.234567, "trailing_pe": 24.0, "trend": "Uptrend", "data_end": "2026-10-20",
       "market": "india_invested", "note": "core holding", "flag": "Green", "flag_reason": "manual",
       "interested": True, "custom_w52dist": -4.2}
rec = ma.public_record(row, "screener", expert_view={"verdict": "ACCUMULATE", "headline": "Base breakout", "model_used": "m"},
                       fundamental={"sentiment": "Positive", "future_guidance": "20% growth", "reasoning": "internal"},
                       as_of="2026-10-20T10:00:00+00:00")
blob = json.dumps(rec)
check(not any(p in rec["metrics"] for p in PRIVATE) and "india_invested" not in blob and "core holding" not in blob,
      "a published record carries no watchlist, note, flag, Interested or custom column")
check(rec["metrics"]["pct_change_1d"] is None and rec["metrics"]["rsi14_weekly"] == 61.2346,
      "NaN becomes None and floats are rounded")
check((rec["exchange"], rec["currency"]) == ("NSE", "INR") and rec["source"] == "screener", "identity and provenance")
check(rec["ai"] == {"expert": {"verdict": "ACCUMULATE", "headline": "Base breakout"},
                    "sentiment": {"sentiment": "Positive", "reasoning": "internal", "future_guidance": "20% growth"}},
      "AI views keep only the listed write-up fields: Sentiment's reasoning, never the model names")
check(ma.ai_views(None, {}) == {}, "a ticker without write-ups has no AI views")
check([ma.listing_of(t) for t in ("ACME", "ZED.BO", "zed.ns")] == [("US", "USD"), ("BSE", "INR"), ("NSE", "INR")],
      "exchange and currency from the suffix")

# --- definitions and the "why" behind each signal (glossary.py, shared with the dashboard) ---------
import glossary  # noqa: E402

gdefs = glossary.column_definitions(settings, glossary.ema_col_labels(settings))
check(labels and {c["field"]: c["description"] for c in cat}["trend"] == gdefs["Trend"]
      and {c["field"]: c["description"] for c in cat}["last_close"] == gdefs["Last"],
      "a metric's definition is the dashboard's own glossary entry (Last Close -> its 'Last' column)")
check({c["field"]: c["description"] for c in cat}["below_40"].startswith("True when"),
      "a metric with no glossary entry keeps its own short definition")
full = {"ticker": "ZED.NS", "trend": "Uptrend", "tech_uptrend": 0, "ta_rules": "Maintain/Add", "volume_trend": "In-line",
        "sentiment": None, "median_volume_10d": 70, "median_volume_100d": 100,
        "trend_detail": sd.compute_trend(100, __import__("pandas").Series([90.0, 92.0, 94.0, 96.0]), 2.0, 200.0, 1.0,
                                         1.0, 1.0, 3, ema_fast=98)[2],
        "tech_uptrend_detail": sd.compute_tech_uptrend(100, 110, "Down", 0, 96, 70, 100, 3, 0.3)[1],
        "ta_rules_detail": sd.compute_ta_rules(100.0, 99.0, 97.0, 90.0, [], [99.0])[1]}
why = ma.public_record(full, "library", settings=settings)["metrics"]
check(why["trend_why"] == glossary.trend_tooltip(full, glossary.ema_col_labels(settings))
      and "✓ Price > 40 WEMA" in why["trend_why"] and why["tech_uptrend_why"].startswith("Tech Uptrend requires ALL of:")
      and "Close > Weekly VStop" in why["tech_uptrend_why"] and why["ta_rules_why"].endswith("→ Maintain/Add")
      and "10D ÷ 100D" in why["volume_trend_why"],
      "the explain group is the dashboard's hover text for Trend, Tech Uptrend, TA Rules and Vol Trend")
# A library-only ticker has no Stage Analysis (the weekly upload covers the watchlists), and the
# verdict is Stage-led since 2026-10-31: Pending, saying why.
check(why["expert_take_why"] == "Pending: No Stage Analysis for this stock yet -- upload the weekly CSV",
      f"Expert Take's reasons, for a library ticker with no Stage ({why['expert_take_why']!r})")
# Stage x TA Rules since 2026-11-01: S2 + Maintain/Add within 3% of the 10W EMA is the buy zone.
full = dict(full, stage="S2 Advancing", stage_mom="Strong ↑", cheat_entry="No", pead="Mod",
            last_close=101.0, ema10=100.0)
why = ma.public_record(full, "library", settings=settings)["metrics"]
check(why["expert_take_why"].startswith("Accumulate: S2 buy zone: +1.0% from the 10W EMA; Stage: S2 Advancing")
      and "Sentiment Unknown" in why["expert_take_why"] and "PEAD Mod" in why["expert_take_why"],
      f"Expert Take's reasons, for a row with a Stage and no Sentiment ({why['expert_take_why']!r})")
import datetime as _dt  # noqa: E402

risk = {"material_negative": True, "category": "regulatory or legal action", "date": _dt.date.today().isoformat(), "quote": "SEBI order"}
fyi = ma.explanations(full, settings, {"news_risk": risk})["expert_take_why"]
check(fyi.startswith("Accumulate: ") and "News FYI (does not change the verdict): regulatory or legal action" in fyi
      and "SEBI order" in fyi,
      f"a news event is named as FYI, and the verdict stays (owner, 2026-11-01) ({fyi!r})")
check(ma.explanations({"ticker": "NEW"}, settings) == {}, "a row without signals has nothing to explain")

# --- choosing groups --------------------------------------------------------------------
picked = ma.select_groups(rec, ["price", "valuation", "no_such_group"])
check(set(picked) == {"price", "valuation"} and picked["price"] == {"last_close": 101.5, "data_end": "2026-10-20"},
      "only the asked groups, unknown names ignored, empty values left out")
check(set(ma.select_groups(rec, [])) == {"price", "momentum", "valuation", "trend", "explain", "ai_views"},
      "no groups asked means every group that has values")
check("ai_views" not in ma.select_groups(dict(rec, ai={}), ["ai_views", "price"]), "no write-ups, no ai_views group")
check(ma.with_companions(["price", "trend", "valuation"]) == ["price", "trend", "stage", "valuation"]
      and ma.with_companions(["stage", "trend"]) == ["stage", "trend"] and ma.with_companions(["price"]) == ["price"],
      "trend brings stage, right after it and only once; nothing else changes")
_staged = dict(rec, metrics=dict(rec["metrics"], stage="S2 Advancing", pead="Mod"))
check(ma.select_groups(_staged, ["trend"]).get("stage") == {"stage": "S2 Advancing", "pead": "Mod"}
      and "stage" not in ma.select_groups(_staged, ["price"]) and "stage" not in ma.select_groups(rec, ["trend"]),
      "asking for trend returns the stage group too (Claude chat asked for 'technical data' and skipped it); a stock "
      "without stage values gets none")

# --- benchmarks and the live computation ----------------------------------------------------
check(ma.benchmark_for("ZED.NS") == ("Nifty 500", sd.INDEX_DEFINITIONS["Nifty 500"])
      and ma.benchmark_for("ACME") == ("S&P 500", sd.INDEX_DEFINITIONS["S&P 500"])
      and ma.benchmark_for("ACME", {"ACME": {"index": "Nifty 500", "benchmark": "^CRSLDX"}})[0] == "Nifty 500"
      and ma.benchmark_for("ZED.NS", {"ZED.NS": {"index": None, "benchmark": None}})[0] == "Nifty 500",
      "benchmark: the recorded index first, else Nifty 500 for India and the S&P 500 for US listings")

calls = []


def fake_fetch(tickers, benchmark="SPY", settings=None, **kw):
    calls.append((tuple(tickers), benchmark))
    return [{"ticker": t, "company_name": t.title(), "last_close": 10.0} for t in tickers if t != "GONE"], "now"


real_fetch = sd.fetch_snapshot
sd.fetch_snapshot = fake_fetch
try:
    rows = ma.compute_rows(["acme", "ZED.NS", "GONE", "ACME", " "], settings=settings, retry_wait=0)
finally:
    sd.fetch_snapshot = real_fetch
check(sorted(calls[:2]) == sorted([(("ACME", "GONE"), "SPY"), (("ZED.NS",), "^CRSLDX")]),
      f"one fetch per benchmark, tickers upper-cased and deduplicated ({calls})")
check(set(rows) == {"ACME", "ZED.NS"} and rows["ZED.NS"]["index_name"] == "Nifty 500",
      "a ticker Yahoo returned nothing for is absent; the index is recorded")
check(calls[-1] == (("GONE",), "SPY") and len(calls) == 3,
      f"a missing ticker is asked for once more, on its own ({calls})")

# Yahoo leaves a symbol out of one batch and returns it on the next ask: the retry recovers it.
calls.clear()
seen = set()


def flaky_fetch(tickers, benchmark="SPY", settings=None, **kw):
    calls.append((tuple(tickers), benchmark))
    out = [{"ticker": t, "last_close": 1.0} for t in tickers if not t.endswith(".NS") or t in seen]
    seen.update(tickers)
    return out, "now"


sd.fetch_snapshot = flaky_fetch
try:
    rows = ma.compute_rows(["ZED.NS", "ACME"], settings=settings, retry_wait=0)
    no_retry = ma.compute_rows(["NEW.NS"], settings=settings, retry_wait=None)
finally:
    sd.fetch_snapshot = real_fetch
check(set(rows) == {"ZED.NS", "ACME"} and calls[-2] == (("ZED.NS",), "^CRSLDX"),
      "a ticker left out of the first batch is recovered by the retry")
check(no_retry == {} and calls[-1] == (("NEW.NS",), "^CRSLDX"), "retry_wait=None asks only once")

# --- api/market.py (the lookup itself is checked in test_market_lookup.py) ------------------------
import market as endpoint  # noqa: E402
import market_http  # noqa: E402

yahoo = {"acme": "ACME"}.get
real_compute = ma.compute_rows
ma.compute_rows = lambda tickers, **kw: {"ACME": dict(row, ticker="ACME", company_name="Acme Corp")}
try:
    body, status = endpoint.market_response(["acme", "NOPE"], ["price"], yahoo=yahoo)
    check(status == 200 and [r["ticker"] for r in body["results"]] == ["ACME"] and body["results"][0]["query"] == "acme"
          and [m["query"] for m in body["missing"]] == ["NOPE"] and body["missing"][0]["note"]
          and set(body["results"][0]["groups"]) == {"price"} and body["results"][0]["computed_now"],
          "the endpoint returns the asked groups per query and lists, with a reason, the names it could not find")
    check("india_invested" not in json.dumps(body) and "core holding" not in json.dumps(body),
          "the endpoint publishes nothing private either")
    check(endpoint.market_response([], [])[1] == 400 and endpoint.market_response(["A"] * 6, [])[1] == 400,
          "no company, or too many, is a 400")


    def boom(tickers, **kw):
        print("[yf.download attempt 1/3 failed] rate limited")
        import logging
        logging.getLogger("yfinance").error("1 Failed download: YFRateLimitError")
        raise RuntimeError("Yahoo refused")


    ma.compute_rows = boom
    body, status = endpoint.market_response(["ACME"], [], yahoo=yahoo)
    check(status == 502 and "Yahoo refused" in body["error"] and any("rate limited" in l for l in body["log"])
          and any("1 Failed download" in l for l in body["log"]),
          "a failed computation is a 502 that carries Yahoo's messages, printed and logged by yfinance")
finally:
    ma.compute_rows = real_compute


class Headers(dict):
    def get(self, k, d=None):
        return super().get(k, d)


import os  # noqa: E402

os.environ.pop("MARKET_API_TOKEN", None)
check(not market_http.authorized(Headers(), {}) and not market_http.authorized(Headers(Authorization="Bearer x"), {})
      and not market_http.authorized(Headers(), {}, "/mcp/"),
      "no token configured: closed -- every request refused (2026-10-23; it used to be open)")
os.environ["MARKET_API_TOKEN"] = "s3cret"
check(market_http.authorized(Headers(Authorization="Bearer s3cret"), {})
      and market_http.authorized(Headers(), {"token": ["s3cret"]})
      and market_http.authorized(Headers(), {}, "/mcp/s3cret")
      and market_http.authorized(Headers(**{"x-now-route-matches": "token=s3cret"}), {}, "/api/mcp.py"),
      "the token is accepted as a bearer header, ?token=, or the connector's /mcp/<token> path (as typed or as Vercel rewrites it)")
check(not market_http.authorized(Headers(Authorization="Bearer nope"), {}) and not market_http.authorized(Headers(), {})
      and not market_http.authorized(Headers(), {}, "/mcp/nope") and not market_http.authorized(Headers(), {}, "/api/market"),
      "a wrong or missing token is refused")
os.environ.pop("MARKET_API_TOKEN", None)

# --- publish_market.py: what goes into the research library's database ----------------------------
import publish_market as pm  # noqa: E402

snap = {"generated_at": "2026-10-20T00:44:59+00:00", "per_market": {
    "india_invested": [dict(row, sentiment="Neutral")],
    "india_watchlist": [dict(row, market="india_watchlist")],          # the same ticker in a second watchlist
    "us_watchlist": [{"ticker": "ACME", "company_name": "Acme Corp", "last_close": 5.0, "market": "us_watchlist"}]}}
recs = pm.screener_records(snap, {"ZED.NS": {"headline": "h"}}, {"ZED.NS": {"sentiment": "Positive", "earnings_summary": "e"}})
check([r["ticker"] for r in recs] == ["ZED.NS", "ACME"], "one record per ticker, even in two watchlists")
check(recs[0]["ai"]["sentiment"]["sentiment"] == "Neutral" and recs[0]["as_of"] == snap["generated_at"],
      "Sentiment is the guarded label the row shows, not the raw model output; as_of is the snapshot's")
check(not any(w in json.dumps(recs) for w in ("india_invested", "india_watchlist", "us_watchlist", "core holding")),
      "no watchlist name or note reaches the database")

# TheWrap Stage Analysis: uploaded weekly (stage_analysis.json), never computed, so the publish reads the file --
# an upload lands between snapshot refreshes, and the snapshot's copy of the fields can be a week behind it.
import glossary as _gl  # noqa: E402
import stage_analysis as _sa  # noqa: E402
_stage_cat = [c for c in cat if c["group"] == "stage"]
_defs = _gl.column_definitions(settings, _gl.ema_col_labels(settings))
check([c["field"] for c in _stage_cat] == [f for f, _ in _sa.COLUMN_DEFS] + ["stage_uploaded"]
      and [c["label"] for c in _stage_cat][:7] == [lbl for _, lbl in _sa.COLUMN_DEFS]
      and all(c["description"] == _defs[c["label"]] for c in _stage_cat[:7]) and "uploaded" in _stage_cat[7]["description"],
      "the stage group carries the seven Stage Analysis columns (the dashboard's labels and glossary text) and the upload date")
_stage_file = {"meta": {}, "tickers": {"ZED.NS": {"stage": "S2 Advancing", "stage_prev": "S1 Accumulation", "stage_conf": "Developing",
                                                   "stage_weeks": 8, "stage_mom": "Strong ↑", "cheat_entry": "No", "pead": "Mod",
                                                   "uploaded": "2026-10-25"}}}
_snap_stale = {"generated_at": "2026-10-25T04:55:00+00:00", "per_market": {
    "india_invested": [dict(row, stage=None, stage_mom=None)],
    "us_watchlist": [{"ticker": "ACME", "company_name": "Acme Corp", "last_close": 5.0, "stage": "S4 Decline"}]}}
_srecs = {r["ticker"]: r for r in pm.screener_records(_snap_stale, {}, {}, stage=_stage_file)}
check(_srecs["ZED.NS"]["metrics"]["stage"] == "S2 Advancing" and _srecs["ZED.NS"]["metrics"]["pead"] == "Mod"
      and _srecs["ZED.NS"]["metrics"]["stage_weeks"] == 8 and _srecs["ZED.NS"]["metrics"]["stage_uploaded"] == "2026-10-25"
      and _srecs["ACME"]["metrics"]["stage"] is None and _snap_stale["per_market"]["india_invested"][0]["stage"] is None,
      "the publish takes the Stage fields from the uploaded file, not the snapshot (a ticker the upload lacks gets none), "
      "and leaves the snapshot rows untouched")
check(pm.screener_records(_snap_stale, {}, {})[1]["metrics"]["stage"] == "S4 Decline",
      "without the file the snapshot's own fields are published, as before")


class FakeCur:
    def __init__(self, results):
        self.results, self.sql = results, []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchall(self):
        return self.results.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, results):
        self.cur = FakeCur(results)

    def cursor(self):
        return self.cur


real_load = sd.load_data_snapshot
sd.load_data_snapshot = lambda: snap
try:
    todo, tracked = pm.library_tickers(FakeConn([[("ZED.NS",), ("NEW.NS",), ("ACME",), ("LATE",)], [("LATE",)]]))
finally:
    sd.load_data_snapshot = real_load
check(todo == ["NEW.NS"] and tracked == 3,
      f"library tickers: everything the library discusses minus the screener's own, in the database or the snapshot ({todo}, {tracked})")
# The library run prints counts only: Yahoo's per-ticker complaints (stdout, stderr and yfinance's logger)
# name tickers log_redact.py doesn't know. Run it end to end on a fake connection and capture what it prints.
import contextlib as _cl, io as _io, logging as _lg  # noqa: E401,E402


def noisy_compute(tickers, **kw):
    print("NEWCO.NS: ERROR stdout")
    print("$NEWCO.NS: possibly delisted", file=sys.stderr)
    _lg.getLogger("yfinance").error("1 Failed download: ['NEWCO.NS']")
    return {}


class PubConn(FakeConn):
    def transaction(self):
        return _cl.nullcontext()


FakeCur.executemany = lambda self, sql, rows: self.sql.append(sql)


real_compute, real_load2 = ma.compute_rows, sd.load_data_snapshot
ma.compute_rows, sd.load_data_snapshot = noisy_compute, (lambda: snap)
real_tix = sd.load_ticker_index
sd.load_ticker_index = lambda: {}
out = _io.StringIO()
try:
    with _cl.redirect_stdout(out), _cl.redirect_stderr(out):
        rc = pm.publish_library(PubConn([[("NEWCO.NS",)], []]))
finally:
    ma.compute_rows, sd.load_data_snapshot, sd.load_ticker_index = real_compute, real_load2, real_tix
# 6 lines: the NSE miss is tried once more on BSE (market_lookup.compute_priced), and complains again.
check("NEWCO" not in out.getvalue() and rc == 1 and "6 error line(s) suppressed" in out.getvalue(),
      f"the library run never prints a ticker, whatever channel Yahoo's complaint comes through ({out.getvalue()!r})")
check(_lg.getLogger("yfinance").propagate and not any(isinstance(h, _lg.StreamHandler) and getattr(h, "stream", None) is not None
                                                      and h.stream.__class__ is _io.StringIO for h in _lg.getLogger("yfinance").handlers),
      "...and yfinance's logger is put back as it was")
# --min-gap: the hourly step in data-refresh.yml and the 7/17 fallback skip when the library was just published.
from datetime import datetime as _dt, timedelta as _td, timezone as _tz  # noqa: E402

_ago = lambda m: (_dt.now(_tz.utc) - _td(minutes=m)).isoformat(timespec="seconds")  # noqa: E731
computed = []
ma.compute_rows, sd.load_data_snapshot = (lambda t, **kw: (computed.append(list(t)), {})[1]), (lambda: snap)
sd.load_ticker_index = lambda: {}
try:
    out = _io.StringIO()
    with _cl.redirect_stdout(out):
        rc = pm.publish_library(PubConn([[(_ago(20),)]]), min_gap=50)
    check(rc == 0 and computed == [] and "published 20 min ago" in out.getvalue() and "skipped" in out.getvalue()
          and "NEWCO" not in out.getvalue(), f"published 20 minutes ago: skipped, nothing computed ({out.getvalue().strip()!r})")
    for label, first in (("70 minutes ago", [(_ago(70),)]), ("never (no library rows)", [(None,)])):
        computed.clear()
        with _cl.redirect_stdout(_io.StringIO()):
            pm.publish_library(PubConn([first, [("NEWCO.NS",)], []]), min_gap=50)
        check(computed and computed[0] == ["NEWCO.NS"], f"published {label}: computed as before")
    computed.clear()
    with _cl.redirect_stdout(_io.StringIO()):
        pm.publish_library(PubConn([[("NEWCO.NS",)], []]))
    check(computed and computed[0] == ["NEWCO.NS"], "no --min-gap (a run by hand): never asks, always computes")
finally:
    ma.compute_rows, sd.load_data_snapshot, sd.load_ticker_index = real_compute, real_load2, real_tix
check(pm.main(["publish_market.py", "library", "--min-gap"]) == 2 and pm.main(["publish_market.py", "library", "--min-gap", "x"]) == 2,
      "--min-gap without a number is a usage error")
os.environ.pop("MARKET_DB_URL", None)
check(pm.main(["publish_market.py", "library"]) == 0, "without MARKET_DB_URL it publishes nothing and does not fail")
check(pm.main(["publish_market.py", "bogus"]) == 2, "an unknown mode is a usage error")
src = (REPO / "publish_market.py").read_text()
check("redirect_stdout" in src and "print(t" not in src and "print(f\"{t" not in src,
      "the library run swallows per-ticker messages (library tickers aren't in log_redact's lists)")

# --- deployment files ---------------------------------------------------------------------------
vercel = json.loads((REPO / "vercel.json").read_text())
check([b["src"] for b in vercel["builds"]] == ["api/market.py", "api/mcp.py"], "Vercel builds only the two functions")
import re as _re  # noqa: E402
routes = [(_re.compile(r["src"].replace("(?<", "(?P<")), r["dest"]) for r in vercel["routes"]]
def _route(path):
    return next(((m, d) for rx, d in routes for m in [rx.fullmatch(path)] if m), (None, None))
m, dest = _route("/mcp/s3cret")
check(dest == "api/mcp.py?token=$token" and m.group("token") == "s3cret" and _route("/api/market")[1] == "api/market.py"
      and _route("/mcp/s3cret/")[0].group("token") == "s3cret" and _route("/mcp")[1] == "api/mcp.py"
      and _route("/mcp/a/b")[0] is None,
      "routes: /api/market to the REST function, /mcp/<token> to the MCP server with the token passed on")
reqs = (REPO / "api" / "requirements.txt").read_text()
check("streamlit" not in reqs and "yfinance" in reqs and "pandas" in reqs,
      "the function installs its own slim requirements, not the app's")
app_reqs = (REPO / "requirements.txt").read_text()
for lib in ("yfinance", "pandas"):
    pin = next(l for l in app_reqs.splitlines() if l.startswith(lib))
    check(pin in reqs, f"{lib} is pinned like the app's ({pin})")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
