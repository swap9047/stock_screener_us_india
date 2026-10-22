"""Vercel function: the screener's metrics for any ticker, computed live.

GET /api/market?ticker=<Yahoo ticker>[,<ticker>...]&groups=<group>,<group>
  -> {"results": [{ticker, company_name, exchange, currency, source, data_end, as_of, groups}],
      "missing": [tickers Yahoo returned nothing for], "seconds": ..., "log": [...]}

Up to MAX_TICKERS per request. groups are market_api.GROUP_NAMES; none means all. When the
MARKET_API_TOKEN environment variable is set, a request must carry it (Authorization: Bearer
<token>, or ?token=); without it the endpoint is open, which is only for the first smoke test.

The numbers come from market_api.compute_rows -> stock_data.fetch_snapshot, the dashboard's own
computation (see market_api's docstring). Vercel's Python runtime runs this file's `handler`
class; vercel.json routes /api/market here and api/requirements.txt lists what it installs.
"""

import contextlib
import hmac
import io
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# yfinance keeps a timezone cache under the user's cache dir, which is read-only on Vercel;
# /tmp is the one writable place. Set before yfinance is imported (by stock_data).
os.environ.setdefault("XDG_CACHE_HOME", "/tmp")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_api  # noqa: E402
import yfinance as yf  # noqa: E402

try:
    yf.set_tz_cache_location("/tmp/yf-tz-cache")
except Exception:
    pass

MAX_TICKERS = 5


def _authorized(headers, query):
    token = os.environ.get("MARKET_API_TOKEN", "")
    if not token:
        return True
    sent = (headers.get("Authorization") or "").removeprefix("Bearer ").strip() or (query.get("token") or [""])[0]
    return hmac.compare_digest(sent.encode(), token.encode())


def market_response(tickers, groups):
    """The JSON body for one request, and its HTTP status."""
    if not tickers:
        return {"error": "pass ?ticker=<Yahoo ticker>, e.g. ACME or ZED.NS"}, 400
    if len(tickers) > MAX_TICKERS:
        return {"error": f"at most {MAX_TICKERS} tickers per request"}, 400
    started = time.time()
    log = io.StringIO()
    try:
        # fetch_snapshot reports Yahoo's failures by printing; keep them for the caller.
        with contextlib.redirect_stdout(log):
            rows = market_api.compute_rows(tickers)
    except Exception as e:
        return {"error": f"computation failed: {type(e).__name__}: {e}",
                "log": log.getvalue().splitlines()[-20:]}, 502
    results = []
    for t in tickers:
        row = rows.get(t.strip().upper())
        if row:
            record = market_api.public_record(row, "live")
            results.append({k: record[k] for k in ("ticker", "company_name", "exchange", "currency", "source",
                                                   "data_end", "as_of")}
                           | {"groups": market_api.select_groups(record, groups)})
    return {"results": results, "missing": [t for t in tickers if t.strip().upper() not in rows],
            "seconds": round(time.time() - started, 1), "log": log.getvalue().splitlines()[-20:]}, 200


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not _authorized(self.headers, query):
            return self._send({"error": "unauthorized"}, 401)
        tickers = [t for v in query.get("ticker", []) for t in v.split(",") if t.strip()]
        groups = [g for v in query.get("groups", []) for g in v.split(",") if g.strip()]
        body, status = market_response(tickers, groups)
        self._send(body, status)

    def _send(self, body, status):
        data = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
