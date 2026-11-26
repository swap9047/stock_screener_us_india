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

The same route also carries the daily Gemini call counts per key (owner, 2026-11-26), so no third function is bundled:
GET  /api/market?usage=today|YYYY-MM-DD  -> {"day", "quota_tz", "rows": [{key_id, model, calls}]}  (the dashboard's table)
POST /api/market  {"key_usage": [[day, key_id, model, calls], ...]}  -> {"added": n}  (adds to the stored counts; the dashboard
sends its own calls this way, since it holds no database secret). Keys are 8-hex fingerprints, never keys.
Vercel's Python runtime runs this file's `handler` class; vercel.json routes /api/market here.
"""

import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_http  # noqa: E402  (first: sets the runtime up before yfinance is imported)
import market_api  # noqa: E402
import market_lookup  # noqa: E402

MAX_TICKERS = market_lookup.MAX_QUERIES
KEY_USAGE_MAX_ROWS = 500          # rows one POST may carry
POST_MAX_BYTES = 64 * 1024
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEY_ID = re.compile(r"^[0-9a-f]{8}$")


def usage_response(day, db=None):
    """One quota day's Gemini calls per key and model, for the dashboard's table: ({day, quota_tz, rows}, status)."""
    import llm_util
    day = llm_util.quota_day() if day in ("", "today", "1") else day
    if not _DAY.match(day):
        return {"error": "usage= takes today or a date like 2026-11-26"}, 400
    own_db = db is None
    if own_db:
        db, err = market_lookup.MarketDB.from_env()
    if db is None:
        return {"error": err or "no database configured"}, 503
    try:
        rows = db.key_usage(day)
    except Exception as e:
        return {"error": f"could not read the counts ({type(e).__name__})"}, 502
    finally:
        if own_db:
            db.close()
    return {"day": day, "quota_tz": llm_util.QUOTA_TZ, "rows": rows}, 200


def add_usage_response(payload, db=None):
    """Add the posted counts to the stored ones: ({added}, status). Every row is checked (a date, an 8-hex key id, a model
    name, a positive whole count), so a bad client can't write anything else into the table."""
    rows = payload.get("key_usage") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows or len(rows) > KEY_USAGE_MAX_ROWS:
        return {"error": f"key_usage must be a list of 1 to {KEY_USAGE_MAX_ROWS} rows [day, key_id, model, calls]"}, 400
    clean = []
    for row in rows:
        ok = isinstance(row, (list, tuple)) and len(row) == 4
        if ok:
            day, key_id, model, calls = row
            ok = (isinstance(day, str) and _DAY.match(day) and isinstance(key_id, str) and _KEY_ID.match(key_id)
                  and isinstance(model, str) and 0 < len(model) <= 80 and isinstance(calls, int)
                  and not isinstance(calls, bool) and 0 < calls <= 1_000_000)
        if not ok:
            return {"error": "each row must be [YYYY-MM-DD, 8-hex key id, model name, positive whole count]"}, 400
        clean.append((day, key_id, model, calls))
    own_db = db is None
    if own_db:
        db, err = market_lookup.MarketDB.from_env()
    if db is None:
        return {"error": err or "no database configured"}, 503
    try:
        added = db.add_key_usage(clean)
    except Exception as e:
        return {"error": f"could not add the counts ({type(e).__name__})"}, 502
    finally:
        if own_db:
            db.close()
    return {"added": added}, 200


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
        if "usage" in query:
            body, status = usage_response((query["usage"] or [""])[0])
            return self._send(body, status)
        queries = [q for q in query.get("q", []) if q.strip()]
        queries += [t for v in query.get("ticker", []) for t in v.split(",") if t.strip()]
        groups = [g for v in query.get("groups", []) for g in v.split(",") if g.strip()]
        refresh = (query.get("refresh") or [""])[0].lower() in ("1", "true", "yes")
        body, status = market_response(queries, groups, refresh=refresh)
        self._send(body, status)

    def do_POST(self):
        url = urlparse(self.path)
        if not market_http.authorized(self.headers, parse_qs(url.query), url.path):
            return self._send({"error": "unauthorized"}, 401)
        try:
            size = int(self.headers.get("Content-Length") or 0)
            if not 0 < size <= POST_MAX_BYTES:
                return self._send({"error": f"the body must be 1 to {POST_MAX_BYTES} bytes of JSON"}, 413 if size else 400)
            payload = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeDecodeError):
            return self._send({"error": "the body is not valid JSON"}, 400)
        body, status = add_usage_response(payload)
        self._send(body, status)

    def _send(self, body, status):
        data = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
