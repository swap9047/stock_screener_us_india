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
                    "sentiment": {"sentiment": "Positive", "future_guidance": "20% growth"}},
      "AI views keep only the listed write-up fields (no model names, no internal reasoning)")
check(ma.ai_views(None, {}) == {}, "a ticker without write-ups has no AI views")
check([ma.listing_of(t) for t in ("ACME", "ZED.BO", "zed.ns")] == [("US", "USD"), ("BSE", "INR"), ("NSE", "INR")],
      "exchange and currency from the suffix")

# --- choosing groups --------------------------------------------------------------------
picked = ma.select_groups(rec, ["price", "valuation", "no_such_group"])
check(set(picked) == {"price", "valuation"} and picked["price"] == {"last_close": 101.5, "data_end": "2026-10-20"},
      "only the asked groups, unknown names ignored, empty values left out")
check(set(ma.select_groups(rec, [])) == {"price", "momentum", "valuation", "trend", "ai_views"},
      "no groups asked means every group that has values")
check("ai_views" not in ma.select_groups(dict(rec, ai={}), ["ai_views", "price"]), "no write-ups, no ai_views group")

# --- benchmarks and the live computation ----------------------------------------------------
check(ma.benchmark_for("ZED.NS") == ("Nifty 500", sd.INDEX_DEFINITIONS["Nifty 500"])
      and ma.benchmark_for("ACME") == ("S&P 500", sd.INDEX_DEFINITIONS["S&P 500"])
      and ma.benchmark_for("ACME", {"ACME": "Nifty 500"})[0] == "Nifty 500",
      "benchmark: the recorded index first, else Nifty 500 for India and the S&P 500 for US listings")

calls = []


def fake_fetch(tickers, benchmark="SPY", settings=None, **kw):
    calls.append((tuple(tickers), benchmark))
    return [{"ticker": t, "company_name": t.title(), "last_close": 10.0} for t in tickers if t != "GONE"], "now"


real_fetch = sd.fetch_snapshot
sd.fetch_snapshot = fake_fetch
try:
    rows = ma.compute_rows(["acme", "ZED.NS", "GONE", "ACME", " "], settings=settings)
finally:
    sd.fetch_snapshot = real_fetch
check(sorted(calls) == sorted([(("ACME", "GONE"), "SPY"), (("ZED.NS",), "^CRSLDX")]),
      f"one fetch per benchmark, tickers upper-cased and deduplicated ({calls})")
check(set(rows) == {"ACME", "ZED.NS"} and rows["ZED.NS"]["index_name"] == "Nifty 500",
      "a ticker Yahoo returned nothing for is absent; the index is recorded")

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
        raise RuntimeError("Yahoo refused")


    ma.compute_rows = boom
    body, status = endpoint.market_response(["ACME"], [])
    check(status == 502 and "Yahoo refused" in body["error"] and any("rate limited" in l for l in body["log"]),
          "a failed computation is a 502 that carries Yahoo's messages")
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
