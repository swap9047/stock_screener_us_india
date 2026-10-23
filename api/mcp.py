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
  list_watchlists     the screener's tabs (holdings included) and indexes, with their latest breadth
  get_watchlist       one tab as a table: a row per stock with the groups asked for, sortable, summarised
  get_market_breadth  an index's or a tab's breadth, day by day
  get_price_history   daily closes of a tab or of named companies
  (market_lists, over what publish_market.py's watchlists mode stores hourly)

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
import market_lists  # noqa: E402
import market_lookup  # noqa: E402

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
# A JSON-RPC batch may hold this many messages (2026-10-23). Unbounded, one POST could carry any
# number of tools/call, each computing up to market_lookup.MAX_QUERIES companies live from Yahoo and
# storing them. Claude sends one message per request; 10 leaves room for any client that batches.
MAX_BATCH = 10
SERVER_INFO = {"name": "stock-screener", "title": "Stock screener market data", "version": "1.0"}
INSTRUCTIONS = (
    "The user's own stock screener. For any question about a listed company's current share price, returns, "
    "moving averages, RSI, relative strength, trend signals (Trend, Tech Uptrend, TA Rules, Expert Take), volume, "
    "52-week range or valuation multiples, call get_market_metrics before searching the web, and cite each figure "
    "with its price_date as (screener, <date>). Explain a signal with the tool's explain group and definitions, "
    "which are the screener's own rules. For the user's watchlists and holdings (how a list did today or this week, its "
    "best and worst stocks), call list_watchlists, then get_watchlist with the groups and sort you need; for market "
    "breadth (Nifty 500, S&P 500, Nasdaq 100 or a watchlist) get_market_breadth; for a price chart or a return over "
    "any window get_price_history. Use web search for news, results and guidance, which these tools do not have.")

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
            "Yahoo tickers ('ACME.NS' for NSE, 'ACME.BO' for BSE, 'ACME' for the US; a six-digit BSE scrip code "
            "also works). Returns metrics by group with the screener's definition of "
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
        "name": "list_watchlists",
        "title": "The user's watchlists and holdings",
        "description": (
            "Every tab of the user's stock screener -- watchlists and the holdings lists (India Invested, US Invested), "
            "plus groups combining them -- with how many stocks each holds, its benchmark and its latest breadth "
            "(% of its stocks above the 200- and 50-day averages), and the indexes with breadth. Call first for any "
            "question about the user's watchlists or portfolio, then get_watchlist."),
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_watchlist",
        "title": "One watchlist as a table",
        "description": (
            "One of the user's screener watchlists or holdings lists as a table: a row per stock with the metric groups "
            "asked for, a summary of each return (stocks up/down, median, average over the whole list), the metric "
            "definitions and the list's breadth. For 'how did it do today / this week / this year' use groups "
            "['price', 'returns'] (returns: 1W, YTD, 1M, 3M, 6M, 1Y, 3Y and vs-index); for best and worst performers sort "
            "by that return (e.g. sort_by 'perf_1w', descending true for best, false for worst) with a limit. "
            "Names are loose: 'India watchlist', 'my US holdings', 'all invested'."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The watchlist, as named in list_watchlists or loosely."},
                "groups": {"type": "array", "items": {"type": "string", "enum": list(market_api.METRIC_GROUPS)},
                           "description": f"Metric groups per stock (default price, returns, trend): {GROUP_HELP}."},
                "sort_by": {"type": "string", "description": "A metric field or label to sort by, e.g. pct_change_1d, perf_1w, perf_ytd, rsi14_daily."},
                "descending": {"type": "boolean", "description": "Largest first (default true)."},
                "limit": {"type": "integer", "minimum": 1, "description": "Rows to return after sorting (default all)."},
            },
            "required": ["name"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_market_breadth",
        "title": "Market breadth, day by day",
        "description": (
            "Market breadth from the user's screener for an index (Nifty 500, S&P 500, Nasdaq 100) or one of the user's "
            "watchlists: each trading day, the % of its stocks above their 200-day and 50-day simple moving averages and "
            "at 52-week highs and lows, plus the latest counts. The same numbers as the screener's breadth charts."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "An index ('Nifty 500', 'S&P 500', 'Nasdaq 100') or a watchlist."},
                "days": {"type": "integer", "minimum": 1, "maximum": market_lists.MAX_BREADTH_DAYS,
                         "description": "Calendar days of history (default 365)."},
            },
            "required": ["name"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_price_history",
        "title": "Daily closing prices",
        "description": (
            "Daily closes for a watchlist's stocks and its benchmark, or for up to 10 companies or indexes by name or "
            "ticker (Nifty 500, S&P 500, Nasdaq 100 work too), over the last N days (up to 730). For returns over a "
            "window the returns group does not have, or to compare stocks over the same dates. Watchlist stocks are "
            "stored; any other listed stock is fetched from Yahoo."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "companies": {"type": "array", "items": {"type": "string"}, "maxItems": market_lists.MAX_PRICE_COMPANIES,
                              "description": "Company names or tickers (or an index name)."},
                "watchlist": {"type": "string", "description": "A watchlist instead of companies: its stocks plus its benchmark."},
                "days": {"type": "integer", "minimum": 1, "maximum": market_lists.MAX_PRICE_DAYS,
                         "description": "Calendar days back (default 90)."},
            },
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


LIST_TOOLS = ("list_watchlists", "get_watchlist", "get_market_breadth", "get_price_history")


def call_tool(name, args, db=None, compute=None, yahoo=market_lookup.yahoo_symbol, search=market_lookup.yahoo_search,
              fetch=None):
    """(result dict, is_error) for one tools/call."""
    settings = market_lookup.screener_settings(db)
    if name == "metric_catalog":
        return {"groups": GROUP_HELP, "metrics": [{k: c[k] for k in ("group", "field", "label", "unit", "description")}
                                                  for c in market_api.catalog(settings)]}, False
    if name in LIST_TOOLS:
        if db is None:
            return {"error": "the screener's database is not reachable right now"}, True
        if name == "list_watchlists":
            return market_lists.list_watchlists(db), False
        if name == "get_watchlist":
            if not str(args.get("name") or "").strip():
                return {"error": "pass name: a watchlist (see list_watchlists)"}, True
            return market_lists.watchlist_table(db, args["name"], args.get("groups"), args.get("sort_by"),
                                                args.get("descending", True), args.get("limit"), settings), False
        if name == "get_market_breadth":
            if not str(args.get("name") or "").strip():
                return {"error": "pass name: an index or a watchlist"}, True
            return market_lists.market_breadth(db, args["name"], args.get("days") or 365), False
        companies = args.get("companies")
        if isinstance(companies, str):
            companies = [companies]
        if not companies and not str(args.get("watchlist") or "").strip():
            return {"error": "pass companies (names or tickers) or a watchlist"}, True
        with market_http.captured():
            return market_lists.price_history(db, companies, args.get("watchlist"), args.get("days") or 90,
                                              **({"fetch": fetch} if fetch else {}), yahoo=yahoo), False
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
        results = market_lookup.lookup(companies, refresh=bool(args.get("refresh")), db=db, compute=compute, yahoo=yahoo,
                                       search=search)
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
        if len(msg) > MAX_BATCH:
            return 400, {"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32600, "message": f"batch too large: at most {MAX_BATCH} messages"}}
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
