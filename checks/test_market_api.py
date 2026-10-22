"""market_api: what the screener shares with other apps, and api/market.py, the Vercel endpoint.

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
check(why["expert_take_why"].startswith("Accumulate: ") and "Sentiment: Unknown" in why["expert_take_why"],
      f"Expert Take's reasons, for a library ticker with no Sentiment ({why['expert_take_why']!r})")
import datetime as _dt  # noqa: E402

risk = {"material_negative": True, "category": "regulatory or legal action", "date": _dt.date.today().isoformat(), "quote": "SEBI order"}
lowered = ma.explanations(full, settings, {"news_risk": risk})["expert_take_why"]
check(lowered.startswith("Hold: ") and "Lowered one step for news: regulatory or legal action" in lowered and "SEBI order" in lowered,
      f"a news event that lowered the verdict is named ({lowered!r})")
check(ma.explanations({"ticker": "NEW"}, settings) == {}, "a row without signals has nothing to explain")

# --- choosing groups --------------------------------------------------------------------
picked = ma.select_groups(rec, ["price", "valuation", "no_such_group"])
check(set(picked) == {"price", "valuation"} and picked["price"] == {"last_close": 101.5, "data_end": "2026-10-20"},
      "only the asked groups, unknown names ignored, empty values left out")
check(set(ma.select_groups(rec, [])) == {"price", "momentum", "valuation", "trend", "explain", "ai_views"},
      "no groups asked means every group that has values")
check("ai_views" not in ma.select_groups(dict(rec, ai={}), ["ai_views", "price"]), "no write-ups, no ai_views group")

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

# --- api/market.py ---------------------------------------------------------------------------
import market as endpoint  # noqa: E402

real_compute = ma.compute_rows
ma.compute_rows = lambda tickers, **kw: {"ACME": dict(row, ticker="ACME", company_name="Acme Corp")}
try:
    body, status = endpoint.market_response(["acme", "NOPE"], ["price"])
    check(status == 200 and [r["ticker"] for r in body["results"]] == ["ACME"] and body["missing"] == ["NOPE"]
          and set(body["results"][0]["groups"]) == {"price"},
          "the endpoint returns the asked groups and lists the tickers it could not compute")
    check("india_invested" not in json.dumps(body) and "core holding" not in json.dumps(body),
          "the endpoint publishes nothing private either")
    check(endpoint.market_response([], [])[1] == 400 and endpoint.market_response(["A"] * 6, [])[1] == 400,
          "no ticker, or too many, is a 400")


    def boom(tickers, **kw):
        print("[yf.download attempt 1/3 failed] rate limited")
        import logging
        logging.getLogger("yfinance").error("1 Failed download: YFRateLimitError")
        raise RuntimeError("Yahoo refused")


    ma.compute_rows = boom
    body, status = endpoint.market_response(["ACME"], [])
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
check(endpoint._authorized(Headers(), {}), "no token configured: open (first smoke test only)")
os.environ["MARKET_API_TOKEN"] = "s3cret"
check(endpoint._authorized(Headers(Authorization="Bearer s3cret"), {}) and endpoint._authorized(Headers(), {"token": ["s3cret"]}),
      "the token is accepted as a bearer header or ?token=")
check(not endpoint._authorized(Headers(Authorization="Bearer nope"), {}) and not endpoint._authorized(Headers(), {}),
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
check("NEWCO" not in out.getvalue() and rc == 1 and "3 error line(s) suppressed" in out.getvalue(),
      f"the library run never prints a ticker, whatever channel Yahoo's complaint comes through ({out.getvalue()!r})")
check(_lg.getLogger("yfinance").propagate and not any(isinstance(h, _lg.StreamHandler) and getattr(h, "stream", None) is not None
                                                      and h.stream.__class__ is _io.StringIO for h in _lg.getLogger("yfinance").handlers),
      "...and yfinance's logger is put back as it was")
os.environ.pop("MARKET_DB_URL", None)
check(pm.main(["publish_market.py", "library"]) == 0, "without MARKET_DB_URL it publishes nothing and does not fail")
check(pm.main(["publish_market.py", "bogus"]) == 2, "an unknown mode is a usage error")
src = (REPO / "publish_market.py").read_text()
check("redirect_stdout" in src and "print(t" not in src and "print(f\"{t" not in src,
      "the library run swallows per-ticker messages (library tickers aren't in log_redact's lists)")

# --- deployment files ---------------------------------------------------------------------------
vercel = json.loads((REPO / "vercel.json").read_text())
check([b["src"] for b in vercel["builds"]] == ["api/market.py"], "Vercel builds only api/market.py")
reqs = (REPO / "api" / "requirements.txt").read_text()
check("streamlit" not in reqs and "yfinance" in reqs and "pandas" in reqs,
      "the function installs its own slim requirements, not the app's")
app_reqs = (REPO / "requirements.txt").read_text()
for lib in ("yfinance", "pandas"):
    pin = next(l for l in app_reqs.splitlines() if l.startswith(lib))
    check(pin in reqs, f"{lib} is pinned like the app's ({pin})")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
