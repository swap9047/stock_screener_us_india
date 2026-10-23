"""Vercel function: the screener as an MCP server, for a Claude chat custom connector.

POST /mcp/<token>   (vercel.json routes it here; the token is MARKET_API_TOKEN)

Streamable HTTP, stateless: every POST carries JSON-RPC (one message or a batch) and gets a JSON
reply -- no sessions, no server-sent events, which is all a request/response tool server needs and
all a serverless function can hold. Methods: initialize, ping, tools/list, tools/call; notifications
get 202. GET is 405 (no event stream).

Tools:
  get_market_metrics  price, technicals and valuation for 1-5 companies by name or ticker, with the
                      screener's definitions and its reasons for each signal (market_lookup.lookup:
                      stored while fresh, else computed now with the screener's saved settings)
  metric_catalog      every metric by group, with its definition

The tool descriptions are what Claude reads when deciding which tool to use, in any chat where the
connector is on -- so they say plainly what the tool is for.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_http  # noqa: E402  (first: sets the runtime up before yfinance is imported)
import market_api  # noqa: E402
import market_lookup  # noqa: E402

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "stock-screener", "title": "Stock screener market data", "version": "1.0"}
INSTRUCTIONS = (
    "The user's own stock screener. For any question about a listed company's current share price, returns, "
    "moving averages, RSI, relative strength, trend signals (Trend, Tech Uptrend, TA Rules, Expert Take), volume, "
    "52-week range or valuation multiples, call get_market_metrics before searching the web, and cite each figure "
    "with its price_date as (screener, <date>). Explain a signal with the tool's explain group and definitions, "
    "which are the screener's own rules. Use web search for news, results and guidance, which this tool does not have.")

GROUP_HELP = ("price, returns, moving_averages, momentum (RSI, ADX), relative_strength (Mansfield RS vs Nifty 500 / "
              "S&P 500), trend (Trend, Tech Uptrend, TA Rules, Expert Take, weekly VStop), volume, range (52-week, "
              "5-year high, breakouts), valuation (P/E TTM and forward, P/B, EV/EBITDA, P/Cashflow), quality_growth "
              "(ROE, ROCE, cash conversion, latest quarter's growth), explain (why each signal reads as it does, "
              "condition by condition), ai_views (the screener's own Expert Take and Sentiment write-ups, watchlist "
              "stocks only)")

TOOLS = [
    {
        "name": "get_market_metrics",
        "title": "Stock price, technicals and valuation",
        "description": (
            "Current share price, returns, moving averages, RSI, relative strength, trend signals (Trend, Tech Uptrend, "
            "TA Rules, Expert Take) with the reasons behind each, volume, 52-week range, valuation multiples (P/E, P/B, "
            "EV/EBITDA), ROE/ROCE and growth for any NSE, BSE or US listed stock, computed by the user's own stock "
            "screener from Yahoo Finance daily bars. Use this first for any question about a stock's price, technical "
            "setup or valuation, before web search. Accepts company names as spoken or written ('Acme Industries') or "
            "Yahoo tickers ('ACME.NS' for NSE, '500001.BO' for BSE, 'ACME' for the US). Returns metrics by group with the screener's definition of "
            "each, and price_date: cite figures as (screener, <price_date>)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "companies": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": market_lookup.MAX_QUERIES,
                              "description": "Company names or tickers, 1-5."},
                "groups": {"type": "array", "items": {"type": "string", "enum": list(market_api.GROUP_NAMES)},
                           "description": f"Metric groups to return (omit for all): {GROUP_HELP}."},
                "refresh": {"type": "boolean", "description": "Recompute from Yahoo now even if a recent value is stored."},
            },
            "required": ["companies"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "metric_catalog",
        "title": "What each screener metric means",
        "description": "Every metric the stock screener computes, by group, with its definition (the screener's own glossary).",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
]


def call_tool(name, args, db=None, compute=None, yahoo=market_lookup.yahoo_symbol):
    """(result dict, is_error) for one tools/call."""
    settings = market_lookup.screener_settings(db)
    if name == "metric_catalog":
        return {"groups": GROUP_HELP, "metrics": [{k: c[k] for k in ("group", "field", "label", "unit", "description")}
                                                  for c in market_api.catalog(settings)]}, False
    if name != "get_market_metrics":
        return {"error": f"unknown tool {name!r}"}, True
    companies = args.get("companies")
    if isinstance(companies, str):
        companies = [companies]
    companies = [str(c) for c in (companies or []) if str(c).strip()]
    if not companies:
        return {"error": "pass companies: a list of 1-5 names or tickers"}, True
    groups = [g for g in (args.get("groups") or []) if g in market_api.GROUP_NAMES] or None
    with market_http.captured():
        results = market_lookup.lookup(companies, refresh=bool(args.get("refresh")), db=db, compute=compute, yahoo=yahoo)
    catalog = market_api.catalog(settings)
    return {"results": [market_lookup.labelled(r, groups, settings, catalog) for r in results]}, False


def handle_message(msg, db_factory=lambda: market_lookup.MarketDB.from_env()[0], **kw):
    """The JSON-RPC reply to one message, or None for a notification."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "invalid request"}}
    if "id" not in msg:
        return None
    method, params, mid = msg["method"], msg.get("params") or {}, msg["id"]
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                  "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        db = db_factory()
        try:
            body, is_error = call_tool(params.get("name"), params.get("arguments") or {}, db=db, **kw)
        except Exception as e:
            body, is_error = {"error": f"{type(e).__name__}: {e}"}, True
        finally:
            if db is not None:
                db.close()
        text = json.dumps(body, ensure_ascii=False, default=str)
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if not is_error:
            result["structuredContent"] = body
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def handle_body(raw, **kw):
    """(status, reply or None) for a POST body: one message or a batch."""
    try:
        msg = json.loads(raw or b"null")
    except ValueError:
        return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
    if isinstance(msg, list):
        replies = [r for r in (handle_message(m, **kw) for m in msg) if r is not None]
        return (200, replies) if replies else (202, None)
    reply = handle_message(msg, **kw)
    return (200, reply) if reply is not None else (202, None)


class handler(BaseHTTPRequestHandler):
    def _authorized(self):
        url = urlparse(self.path)
        return market_http.authorized(self.headers, parse_qs(url.query), url.path)

    def do_POST(self):
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        status, reply = handle_body(self.rfile.read(length) if length else b"")
        self._send(status, reply)

    def do_GET(self):
        # No server-sent event stream: a stateless server only answers POSTs.
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.end_headers()

    def do_DELETE(self):
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.end_headers()

    def _send(self, status, body):
        self.send_response(status)
        if body is None:
            self.end_headers()
            return
        data = json.dumps(body, default=str).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
