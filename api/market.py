"""Vercel function: the screener's metrics for any company, for the research library's app.

GET /api/market?q=<company name or ticker>[&q=...]&groups=<group>,<group>[&refresh=1]
GET /api/market?ticker=<Yahoo ticker>[,<ticker>...]      (the older form, still accepted)
  -> {"results": [{query, ticker, how, company_name, exchange, currency, source, data_end, as_of,
                   computed_now, priced_as, groups}],
      "missing": [{query, note}], "seconds": ..., "log": [...]}

Up to market_lookup.MAX_QUERIES per request. groups are market_api.GROUP_NAMES (field-keyed; the
app labels them from the catalog); none means all. Each query goes through market_lookup.lookup:
the stored row while fresh, else computed now with the screener's saved settings and stored. Every
request must carry MARKET_API_TOKEN (Authorization: Bearer <token>, or ?token=); without the variable
the function refuses everything (401).
Vercel's Python runtime runs this file's `handler` class; vercel.json routes /api/market here.
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_http  # noqa: E402  (first: sets the runtime up before yfinance is imported)
import market_api  # noqa: E402
import market_lookup  # noqa: E402

MAX_TICKERS = market_lookup.MAX_QUERIES


def market_response(queries, groups, refresh=False, db=None, compute=None, yahoo=market_lookup.yahoo_symbol,
                    search=market_lookup.yahoo_search):
    """The JSON body for one request, and its HTTP status."""
    if not queries:
        return {"error": "pass ?q=<company name or ticker>, e.g. q=Acme or q=ZED.NS"}, 400
    if len(queries) > MAX_TICKERS:
        return {"error": f"at most {MAX_TICKERS} companies per request"}, 400
    started = time.time()
    own_db = db is None
    if own_db:
        db, _ = market_lookup.MarketDB.from_env()
    try:
        with market_http.captured() as log:
            results = market_lookup.lookup(queries, refresh=refresh, db=db, compute=compute, yahoo=yahoo, search=search)
    except Exception as e:
        return {"error": f"computation failed: {type(e).__name__}: {e}", "log": log.getvalue().splitlines()[-20:]}, 502
    finally:
        if own_db and db is not None:
            db.close()
    found = []
    for r in results:
        if r["found"]:
            rec = r["record"]
            found.append({"query": r["query"], "ticker": r["ticker"], "how": r["how"], "computed_now": r["live"],
                          "priced_as": r["priced_as"]}
                         | {k: rec.get(k) for k in ("company_name", "exchange", "currency", "source", "data_end", "as_of")}
                         | {"groups": market_api.select_groups(rec, groups)})
    return {"results": found, "missing": [{"query": r["query"], "note": r["note"]} for r in results if not r["found"]],
            "seconds": round(time.time() - started, 1), "log": log.getvalue().splitlines()[-20:]}, 200


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not market_http.authorized(self.headers, query, url.path):
            return self._send({"error": "unauthorized"}, 401)
        queries = [q for q in query.get("q", []) if q.strip()]
        queries += [t for v in query.get("ticker", []) for t in v.split(",") if t.strip()]
        groups = [g for v in query.get("groups", []) for g in v.split(",") if g.strip()]
        refresh = (query.get("refresh") or [""])[0].lower() in ("1", "true", "yes")
        body, status = market_response(queries, groups, refresh=refresh)
        self._send(body, status)

    def _send(self, body, status):
        data = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
