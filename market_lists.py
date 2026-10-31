"""The screener's lists for Claude chat (api/mcp.py): its watchlists as tables, market breadth and daily
closes, read from the research library's database where publish_market.py's watchlists mode puts them
(hourly, from market-breadth.yml).

    list_watchlists(db)                          every tab and combined group, with its latest breadth
    watchlist_table(db, name, groups, ...)       one list: a row per stock with the metric groups asked for
    market_breadth(db, name, days)               an index's or a list's breadth, day by day
    price_history(db, companies | watchlist, days)  daily closes; a ticker not held is fetched from Yahoo

Each takes loose names ("my India holdings", "nifty") and never raises for a bad one: it answers
found=False with the names it does know. The numbers are the screener's own, so a chat reads the same
table and breadth the dashboard shows.
"""

import re
from datetime import datetime, timedelta, timezone
from statistics import mean, median

import index_registry
import market_api
import market_lookup

# market_breadth.json's indexes, by the words people use for them (index_registry's aliases).
INDEX_ALIASES = {ix["key"]: ix["aliases"] for ix in index_registry.INDEXES}
# Each index's price series, the ticker dashboard_perf.json stores it under.
INDEX_BENCHMARKS = {ix["key"]: ix["benchmark"] for ix in index_registry.INDEXES}
DEFAULT_GROUPS = ("price", "returns", "trend")
MAX_BREADTH_DAYS = 1830
MAX_PRICE_DAYS = 730
MAX_PRICE_COMPANIES = 10
# Above this many closes in one reply, every 5th session is kept (and the last): a 90-stock group over a
# year is ~23k numbers, more than a chat can use.
MAX_PRICE_POINTS = 6000
# Return fields the table's summary counts and averages.
SUMMARY_FIELDS = ("pct_change_1d", "perf_1w", "perf_ytd", "perf_1m", "perf_3m", "perf_6m", "perf_1y", "perf_3y",
                  "rel_ret_1d_index", "rel_ret_1w_index", "rel_ret_1m_index", "rel_ret_6m_index")
_FILLER = {"my", "the", "list", "tab", "stocks", "watch", "screener", "of", "in"}
_SYNONYMS = {"holdings": "invested", "holding": "invested", "portfolio": "invested", "positions": "invested"}


def _words(text):
    return " ".join(re.sub(r"[^0-9a-z&]+", " ", str(text or "").lower()).split())


def find_watchlist(lists, text):
    """(name or None, candidates): the list a loose name means. Exact name or label first, then the only list
    whose label and name hold every word ("holdings"/"portfolio" read as invested; filler like "my" dropped).
    Close to the research library's find_watchlist, so both connectors read a name alike."""
    q = _words(text)
    if not q or not lists:
        return None, [w["label"] for w in lists or []]
    for w in lists:
        if q in (_words(w["name"].replace("_", " ")), _words(w["label"])):
            return w["name"], []
    words = [_SYNONYMS.get(x, x) for x in q.split() if x not in _FILLER]

    def holds(w, x):
        # A word of the label or name starting with it ("invest", "substack"), or a run of 4+ letters inside the
        # label written as one word ("market" in "Substack-OutperformingMarket").
        text = f"{_words(w['label'])} {_words(w['name'].replace('_', ' '))}"
        return any(t.startswith(x) for t in text.split()) or (len(x) >= 4 and x in text.replace(" ", ""))
    hits = [w for w in lists if words and all(holds(w, x) for x in words)]
    if len(hits) == 1:
        return hits[0]["name"], []
    return None, [w["label"] for w in (hits or lists)]


def _index_series(text):
    q = _words(text)
    for series, names in INDEX_ALIASES.items():
        if q == series.lower() or q in names:
            return series
    return None


def list_watchlists(db):
    """Every list and the indexes with breadth."""
    lists, series = db.watchlists(), {s["series"]: s for s in db.breadth_series()}
    return {
        "watchlists": [{"name": w["name"], "label": w["label"], "kind": w["kind"], "stocks": w["members"],
                        "benchmark": w["benchmark"], "breadth_latest": (series.get(w["name"]) or {}).get("latest")}
                       for w in lists],
        "indexes": [{"series": s["series"], "label": s["label"], "breadth_latest": s["latest"]}
                    for s in series.values() if s["kind"] == "index"],
        "note": "The user's stock screener tabs (India/US Invested are the user's holdings; a 'group' combines tabs). "
                "get_watchlist gives one as a table, get_market_breadth its breadth, get_price_history its closes.",
    }


def _sort_field(sort_by, catalog):
    """The catalog field a sort_by means: a field name or a label, any case."""
    s = str(sort_by or "").strip().lower()
    if not s:
        return None
    for c in catalog:
        if s in (c["field"].lower(), str(c["label"]).lower()):
            return c["field"]
    return None


def watchlist_table(db, name, groups=None, sort_by=None, descending=True, limit=None, settings=None):
    """One list as a table: a row per stock (ticker, company, price_date and the chosen groups' metrics under
    their screener labels), sorted when asked; a summary of each return shown (stocks up/down, median,
    average); the definitions; the latest breadth; members without data."""
    lists = db.watchlists()
    wl, choices = find_watchlist(lists, name)
    if not wl:
        return {"query": name, "found": False, "available": choices,
                "note": "No single watchlist matches; pass one of the available names."}
    meta = next(w for w in lists if w["name"] == wl)
    groups = market_api.with_companions([g for g in (groups or DEFAULT_GROUPS) if g in market_api.METRIC_GROUPS]
                                        or list(DEFAULT_GROUPS))
    catalog = market_api.catalog(settings)
    by_field = {c["field"]: c for c in catalog}
    fields = [f for g in groups for f, *_ in market_api.METRIC_GROUPS[g][1]]
    members = db.watchlist_members(wl)
    stored = db.rows(members) if members else {}
    rows = []
    for t in members:
        rec = stored.get(t)
        if not rec:
            continue
        metrics = rec.get("metrics") or {}
        row = {"ticker": t, "company": rec.get("company_name"), "price_date": rec.get("data_end")}
        row.update({f: metrics.get(f) for f in fields})
        rows.append(row)
    key = _sort_field(sort_by, catalog)
    if key:
        filled = [r for r in rows if isinstance(r.get(key), (int, float))]
        rows = sorted(filled, key=lambda r: r[key], reverse=bool(descending)) + [r for r in rows if r not in filled]
    summary = {}
    for f in SUMMARY_FIELDS:
        if f not in fields:
            continue
        vals = [r[f] for r in rows if isinstance(r.get(f), (int, float))]
        if vals:
            summary[by_field[f]["label"]] = {"stocks": len(vals), "up": sum(v > 0 for v in vals), "down": sum(v < 0 for v in vals),
                                             "median": round(median(vals), 2), "average": round(mean(vals), 2)}
    shown = rows[:int(limit)] if limit and int(limit) > 0 else rows
    label = {f: by_field.get(f, {}).get("label", f) for f in fields}
    breadth = next((s["latest"] for s in db.breadth_series() if s["series"] == wl), None)
    return {
        "query": name, "found": True, "watchlist": wl, "label": meta["label"], "kind": meta["kind"],
        "benchmark": meta["benchmark"], "stocks": len(members), "groups": groups,
        "sorted_by": label.get(key) if key else None, "rows_shown": len(shown),
        "rows": [{k if k in ("ticker", "company", "price_date") else label[k]: v for k, v in r.items()} for r in shown],
        "summary": summary, "breadth_latest": breadth,
        "without_data": [t for t in members if t not in stored],
        "definitions": {label[f]: by_field[f]["description"] for f in fields if f in by_field and by_field[f].get("description")},
        "note": "The user's stock screener, from Yahoo Finance daily bars (not live quotes); each row's price_date is its "
                "latest bar. Cite figures as (screener, <price_date>). The summary covers every stock, not only the rows shown.",
    }


# kg_breadth's drawdown columns (2026-10-30) and how a reply names them: % of stocks at least that far below
# their 52-week high.
DRAWDOWN_COLUMNS = (("pct_down_10", "% 10%+ below 52-week high"), ("pct_down_20", "% 20%+ below 52-week high"),
                    ("pct_down_30", "% 30%+ below 52-week high"), ("pct_down_40", "% 40%+ below 52-week high"))


def market_breadth(db, name, days=365):
    """An index's or a list's breadth over the last `days`: [date, % above 200d SMA, % above 50d SMA,
    % at 52-week highs, % at 52-week lows], plus % 10 / 20 / 30 / 40%+ below the 52-week high once the
    table has those columns and the series has them."""
    days = max(1, min(int(days or 365), MAX_BREADTH_DAYS))
    series = {s["series"]: s for s in db.breadth_series()}
    key = _index_series(name)
    if not key:
        key, _ = find_watchlist(db.watchlists(), name)
    if not key or key not in series:
        return {"query": name, "found": False, "available": [s["label"] for s in series.values()],
                "note": f"Pass an index ({index_registry.label_list()}) or one of the watchlists."}
    meta = series[key]
    since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    stored = db.breadth_points(key, since)
    # The drawdown columns only when the table has them and this series has a value in them -- a column of
    # nulls would read as "no stock is down".
    down = [(c, label) for c, label in DRAWDOWN_COLUMNS if any(p.get(c) is not None for p in stored)]
    points = [[p["date"], p["pct_above_200"], p["pct_above_50"], p["pct_highs"], p["pct_lows"], *(p.get(c) for c, _ in down)]
              for p in stored]
    return {"query": name, "found": True, "series": key, "label": meta["label"], "kind": meta["kind"], "days": days,
            "latest": meta["latest"], "as_of": meta["as_of"],
            "columns": ["date", "% above 200-day SMA", "% above 50-day SMA", "% at 52-week high", "% at 52-week low",
                        *(label for _, label in down)],
            "points": points,
            "note": "Market breadth from the user's stock screener: the share of the index's (or list's) stocks above their "
                    "200- and 50-day simple moving averages, at 52-week highs and lows, and at least 10 / 20 / 30 / 40% "
                    "below their 52-week high (closes; 10% is a correction, 20% a bear market), each trading day. "
                    "latest.total is how many stocks the reading counts; latest.drawdown is the latest share at each "
                    "level, keyed by the level."}


# A quarterly results reply's figures (quarterly_results.FIELDS), as it names them.
RESULT_FIELDS = (("rev", "revenue growth %"), ("ni", "profit growth %"), ("eps", "EPS growth %"), ("margin", "OPM %"))
MAX_RESULT_QUARTERS = 12


def quarterly_results(db, name, basis="yoy", quarters=8):
    """An index's or a list's quarterly results: per calendar quarter, the median company's revenue, profit and EPS
    growth and OPM -- the News tab's quarterly results charts, as publish_market.py watchlists stores them."""
    basis = "qoq" if str(basis or "").strip().lower() == "qoq" else "yoy"
    quarters = max(1, min(int(quarters or 8), MAX_RESULT_QUARTERS))
    stored = db.quarterly_results() or {}
    series = stored.get("series") or {}
    key = _index_series(name)
    if not key:
        key, _ = find_watchlist(db.watchlists(), name)
    if not key or key not in series:
        return {"query": name, "found": False, "available": [s["label"] for s in series.values()],
                "note": f"Pass an index ({index_registry.label_list()}) or one of the watchlists."}
    s = series[key]
    rows = (s.get(basis) or [])[-quarters:]
    return {
        "query": name, "found": True, "series": key, "label": s["label"], "kind": s["kind"], "basis": basis.upper(),
        "stocks": s.get("stocks"), "companies_with_results": s.get("companies"), "as_of": stored.get("as_of"),
        "columns": ["quarter", *(label for _, label in RESULT_FIELDS), "companies in each median (revenue, profit, EPS, OPM)",
                    "companies that reported the quarter", "still being reported", "companies whose base quarter Yahoo lacks"],
        "points": [[r["label"], *(r.get(f) for f, _ in RESULT_FIELDS), [(r.get("n") or {}).get(f) for f, _ in RESULT_FIELDS],
                    r.get("reported"), bool(r.get("season")), r.get("gap") or 0] for r in rows],
        "note": ("Quarterly results from the user's stock screener (Yahoo's quarterly income statements), the same numbers "
                 "as its News tab charts. Each figure is the MEDIAN company's: revenue growth, profit growth (reported "
                 "net income, one-offs included) and EPS growth on the same quarter a year earlier (YoY) or the quarter "
                 "before (QoQ), and OPM (operating income after depreciation over revenue). Quarters are calendar "
                 "quarters: an Indian company's Q2 FY27 is Sep '26. A company with a loss or zero in the base quarter is "
                 "left out of that growth figure, and one whose share count jumped (a bonus issue or split) out of EPS "
                 "growth; banks have no OPM. A figure from fewer than 5 companies or under 30% of those that reported "
                 "the quarter is null. 'Still being reported': fewer than half the companies of the quarter before have "
                 "reported, so the median can still move. A company whose base quarter is a hole in Yahoo's data is "
                 "left out of that 30% and counted in the last column. Three years of quarters are stored, so YoY has "
                 "fewer quarters until the history builds."),
    }


# The reads' parts in the order the News tab draws its charts, as the reply names them.
READ_PARTS = (("market", "the market around the list (list reads)"),
              ("breadth", "breadth (% of stocks above their 200- and 50-day averages)"),
              ("highs_lows", "52-week highs vs lows"),
              ("drawdown", "stocks 10-40% below their 52-week high"),
              ("return", "the user's invested list vs the index"),
              ("results", "quarterly results (median company; list reads)"),
              ("stocks", "the holdings behind the charts (list reads)"))
MAX_READ_HISTORY = 5


def _read_entry(e):
    out = {"session": e.get("session"), "written": e.get("written"),
           "model": str(e.get("model") or "").replace("models/", ""), "headline": e.get("headline")}
    if e.get("benchmark"):
        out["against"] = e["benchmark"]
    out.update({part: e.get(part) for part, _ in READ_PARTS if e.get(part)})
    if e.get("takeaways"):
        out["key_takeaways"] = list(e["takeaways"])
    return out


def market_read(db, name=None, history=1):
    """The News tab's AI market reads (the (i) boxes): for Nifty 500 and the S&P 500 a headline and a few sentences on
    each chart; for the India Invested and US Invested panels (the Watchlist Breadth section) the same plus the quarterly
    results, the holdings behind them and key takeaways for the investor. Written after each market's close. No name:
    everything that has a read. `history` n adds the n-1 earlier reads (newest first), to see what changed."""
    history = max(1, min(int(history or 1), MAX_READ_HISTORY))
    stored = db.market_read() or {}
    indexes, lists = stored.get("indexes") or {}, stored.get("lists") or {}
    wanted_idx, wanted_lists = list(indexes), list(lists)
    if str(name or "").strip():
        wanted_idx, wanted_lists = [], []
        key = _index_series(name)
        if key and key in indexes:
            wanted_idx = [key]
        else:
            list_key, _ = find_watchlist(db.watchlists(), name)
            if list_key and list_key in lists:
                wanted_lists = [list_key]
        if not wanted_idx and not wanted_lists:
            return {"query": name, "found": False,
                    "available": [v.get("label", k) for g in (indexes, lists) for k, v in g.items()],
                    "note": "Market reads are written for these indexes and invested lists only."}

    def reads(slot):
        entries = sorted((e for e in (slot.get("entries") or []) if isinstance(e, dict)),
                         key=lambda e: e.get("session") or "", reverse=True)[:history]
        return [_read_entry(e) for e in entries]

    return {
        "found": bool(wanted_idx or wanted_lists), "as_of": stored.get("generated_at"),
        "indexes": [{"index": indexes[k].get("label", k), "reads": reads(indexes[k])} for k in wanted_idx],
        "invested_lists": [{"list": lists[k].get("label", k), "reads": reads(lists[k])} for k in wanted_lists],
        "parts": {part: label for part, label in READ_PARTS},
        "note": ("AI-written from the screener's own chart data (the same numbers as get_market_breadth, get_price_history "
                 "and get_quarterly_results) after each market's close: Nifty 500 and India Invested about 7:15 AM ET, the "
                 "S&P 500 and US Invested about 5:15 PM ET; 'session' is the trading day it reads. A part is left out when "
                 "the chart had nothing to add. Every number in a read comes from that data. It is analysis, not advice. "
                 "The 'return' part of an index read compares the user's own invested list with the index; an invested "
                 "list's read is set against its home index ('against'), names holdings, and ends with key_takeaways "
                 "for the investor."),
    }


def yahoo_closes(symbols, days):
    """{symbol: [(date, close)]} from Yahoo (split-adjusted, dividends not added -- like the stored closes).
    Empty on any failure."""
    import yfinance as yf
    from llm_util import call_with_timeout
    period = "2y" if days > 365 else "1y" if days > 180 else "6mo" if days > 90 else "3mo"
    try:
        raw = call_with_timeout(lambda: yf.download(list(symbols), period=period, interval="1d", group_by="ticker",
                                                    auto_adjust=False, progress=False, threads=True), 30, name="yf.download")
    except Exception:
        return {}
    try:
        # A session still trading is not a close (stock_data's rule for every job that judges closes).
        import stock_data
        raw, _ = stock_data.drop_forming_daily_bars(raw, list(symbols))
    except Exception:
        pass
    out = {}
    for s in symbols:
        try:
            closes = (raw[s] if len(symbols) > 1 or s in raw.columns.get_level_values(0) else raw)["Close"].dropna()
        except Exception:
            continue
        out[s] = [(d.strftime("%Y-%m-%d"), round(float(c), 4)) for d, c in closes.items()]
    return out


def _thin(series):
    """(series, step): every step-th close of each series counting back from its last, when the reply would hold
    more than MAX_PRICE_POINTS closes -- at least weekly (5), coarser for a big group over two years. step 1 = all."""
    total = sum(len(v) for v in series.values())
    if total <= MAX_PRICE_POINTS:
        return series, 1
    step = max(5, -(-total // MAX_PRICE_POINTS))
    return {t: [p for i, p in enumerate(v) if (len(v) - 1 - i) % step == 0] for t, v in series.items()}, step


def price_history(db, companies=None, watchlist=None, days=90, fetch=yahoo_closes, yahoo=market_lookup.yahoo_symbol):
    """Daily closes over the last `days`: a watchlist's stocks plus its benchmark, or up to MAX_PRICE_COMPANIES
    names or tickers. Stored closes cover the screener's watchlist stocks and benchmarks (two years); any
    other ticker is fetched from Yahoo now (not stored)."""
    days = max(1, min(int(days or 90), MAX_PRICE_DAYS))
    since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    out = {"days": days, "since": since}
    if watchlist:
        lists = db.watchlists()
        wl, choices = find_watchlist(lists, watchlist)
        if not wl:
            return {"query": watchlist, "found": False, "available": choices,
                    "note": "No single watchlist matches; pass one of the available names."}
        meta = next(w for w in lists if w["name"] == wl)
        tickers = db.watchlist_members(wl) + ([meta["benchmark"]] if meta["benchmark"] else [])
        out.update(watchlist=wl, label=meta["label"], benchmark=meta["benchmark"])
        resolved = [(t, t) for t in dict.fromkeys(tickers)]
        unmatched = []
    else:
        names = [str(c) for c in (companies or []) if str(c).strip()][:MAX_PRICE_COMPANIES]
        if not names:
            return {"found": False, "note": "Pass companies (names or tickers) or a watchlist."}
        resolved, unmatched = [], []
        # A ticker whose closes are stored (a watchlist stock, SPY, QQQ, ^CRSLDX) is taken as typed; an index name
        # becomes its benchmark; anything else goes through the screener's name lookup.
        held = set(db.prices([q.strip().upper() for q in names], since))
        for q in names:
            index_benchmark = INDEX_BENCHMARKS.get(_index_series(q) or "")
            up = q.strip().upper()
            t = index_benchmark or (up if up in held or up.startswith("^") else market_lookup.resolve(db, q, yahoo)[0])
            if not t and market_lookup.TICKER_RE.match(up):
                t = up   # a ticker no listing or search knows: Yahoo's own answer says whether it exists
            (resolved.append((q, t)) if t else unmatched.append(q))
    tickers = list(dict.fromkeys(t for _, t in resolved))
    series = db.prices(tickers, since) if tickers else {}
    need = [t for t in tickers if not series.get(t)]
    fetched = []
    if need and not watchlist:
        symbols = market_lookup.price_symbols(db, need)
        got = fetch(list(dict.fromkeys(symbols.values())), days)
        for t in need:
            pts = [p for p in got.get(symbols[t], []) if p[0] >= since]
            if pts:
                series[t] = pts
                fetched.append(t)
    series, step = _thin({t: series[t] for t in tickers if series.get(t)})
    out.update(found=bool(series), closes={t: [list(p) for p in v] for t, v in series.items()},
               matched={q: t for q, t in resolved if not watchlist and q != t} or None,
               fetched_from_yahoo=fetched, missing=[t for t in tickers if t not in series], unmatched=unmatched,
               sampled=f"every {step}th session, counting back from the latest" if step > 1 else None,
               note="Daily closes from the user's stock screener (split-adjusted, dividends not added). Watchlist stocks "
                    "and benchmarks are stored two years back; other tickers are fetched from Yahoo on request. "
                    "Benchmarks: " + ", ".join(f"{ix['benchmark']} {ix['label']}" for ix in index_registry.INDEXES) + ".")
    return {k: v for k, v in out.items() if v not in (None, [], {})} | {"found": bool(series)}
