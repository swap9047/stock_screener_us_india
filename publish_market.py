#!/usr/bin/env python3
"""
Publish the screener's metrics into the research library's database (the transcription app's
knowledge graph on Supabase), where its Q&A pages and Claude chat read them.

    python3 publish_market.py screener   # the snapshot's tickers + their AI write-ups (data-refresh.yml)
    python3 publish_market.py library    # library companies the screener doesn't track (market-library.yml)
    python3 publish_market.py library --min-gap 50   # ... unless they were published under 50 minutes ago
    python3 publish_market.py watchlists # the tabs, breadth and daily closes (market-breadth.yml)

The library is published hourly by data-refresh.yml (its first run each hour) and at 7 AM / 5 PM ET by
market-library.yml as the fallback; --min-gap makes whichever run comes second in an hour skip.

What may leave this repo in a metrics row is decided by market_api (a whitelist: never notes, flags,
Interested or custom columns). Rows go into kg_market, the metric menu into kg_market_catalog and the calc
settings into kg_market_meta, so the live endpoint (api/market.py) computes with the same settings as the
snapshot.

The watchlists mode publishes the tabs themselves -- holdings included, by the owner's choice (2026-10-23),
so Claude chat and the library's Q&A can answer "how did my India watchlist do this week?" -- into
kg_watchlists / kg_watchlist_members; each index's and each watchlist's breadth into kg_breadth_series /
kg_breadth (breadth_math, the app's own maths); and two years of daily closes of every watchlist stock and
benchmark into kg_prices. Membership only: still no notes, flags, Interested or custom columns.

MARKET_DB_URL is the session-pooler URI of the `market_writer` role, which can write only those market
tables and read the listings and which tickers the library discusses. Unset (a fork, or before
setup) the script says so and exits 0. MARKET_LIMIT (a number) computes only the first N library
tickers, for a smoke test.

Prints counts only. The library's tickers are not in the files log_redact.py masks, so the
computation's own per-ticker messages are swallowed here rather than reaching this public repo's logs.
"""

import contextlib
import io
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import market_api
import market_lookup
import stock_data

# Below this many rows a snapshot is treated as broken, and screener rows missing from it are kept
# rather than deleted from the database.
MIN_SNAPSHOT_ROWS = 10


def connect(url):
    import psycopg
    # prepare_threshold=None: Supabase's poolers can't hold prepared statements.
    return psycopg.connect(url, autocommit=True, prepare_threshold=None, connect_timeout=20)


UPSERT_SQL = (
    "INSERT INTO kg_market (ticker, company_name, exchange, currency, source, data_end, as_of, metrics, ai, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s) ON CONFLICT (ticker) DO UPDATE SET "
    "company_name = excluded.company_name, exchange = excluded.exchange, currency = excluded.currency, "
    "source = excluded.source, data_end = excluded.data_end, as_of = excluded.as_of, metrics = excluded.metrics, "
    "ai = excluded.ai, updated_at = excluded.updated_at")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert(conn, records):
    now = _now()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, [
                (r["ticker"], r["company_name"], r["exchange"], r["currency"], r["source"], r["data_end"], r["as_of"],
                 json.dumps(r["metrics"]), json.dumps(r["ai"]), now) for r in records])
    return len(records)


def write_catalog_and_settings(conn, settings):
    """The metric menu and the calc settings, replaced in one transaction."""
    entries = market_api.catalog(settings)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kg_market_catalog")
            cur.executemany("INSERT INTO kg_market_catalog (field, grp, label, unit, description, ord) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            [(e["field"], e["group"], e["label"], e["unit"], e["description"], e["ord"]) for e in entries])
            cur.execute("INSERT INTO kg_market_meta (key, value) VALUES ('calc_settings', %s) "
                        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                        (json.dumps(stock_data.calc_settings(settings)),))
    return len(entries)


def screener_records(snapshot, expert_views, fundamentals, settings=None):
    """One record per snapshot ticker (a ticker in two watchlists once). Its Sentiment label is the
    guarded one the row carries (what the dashboard shows), not the raw model output."""
    as_of = snapshot.get("generated_at") or _now()
    records = {}
    for rows in (snapshot.get("per_market") or {}).values():
        for row in rows:
            t = row.get("ticker")
            if not t or t in records:
                continue
            fundamental = dict(fundamentals.get(t) or {})
            if fundamental and row.get("sentiment"):
                fundamental["sentiment"] = row["sentiment"]
            records[t] = market_api.public_record(row, "screener", expert_views.get(t), fundamental, as_of=as_of,
                                                  settings=settings)
    return list(records.values())


def keep_previous_values(conn, records, details_missing):
    """Fill each record's blanks from the row already stored (market_lookup.keep_previous): a run whose
    calls to Yahoo for company details failed must not blank out the name, valuation and growth the
    last run stored. The number of tickers that kept something -- counts only, as everything here prints."""
    if not records:
        return 0
    stored = market_lookup.MarketDB(conn).rows([r["ticker"] for r in records])
    return sum(1 for r in records if market_lookup.keep_previous(r, stored.get(r["ticker"]), details_missing.get(r["ticker"])))


def publish_screener(conn):
    from expert_views import load_expert_views
    from fundamentals_eval import load_fundamentals
    snapshot = stock_data.load_data_snapshot()
    if not snapshot:
        print("No data_snapshot.json -- nothing to publish.")
        return 1
    settings = stock_data.load_settings()
    records = screener_records(snapshot, load_expert_views(), load_fundamentals(), settings)
    kept = keep_previous_values(conn, records, {r["ticker"]: r["company_name"] == r["ticker"] for r in records})
    n = upsert(conn, records)
    removed = 0
    if len(records) >= MIN_SNAPSHOT_ROWS:
        # A ticker dropped from every watchlist stops being "screener"; if the library discusses it,
        # the library job computes it again.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kg_market WHERE source = 'screener' AND NOT (ticker = ANY(%s))",
                        ([r["ticker"] for r in records],))
            removed = cur.rowcount
    catalog = write_catalog_and_settings(conn, settings)
    print(f"Published {n} screener ticker(s), {sum(1 for r in records if r['ai'])} with AI write-ups; "
          f"removed {removed} no longer tracked; catalog {catalog} metric(s); {kept} kept earlier values for fields "
          f"Yahoo left blank.")
    return 0


def library_tickers(conn):
    """(to compute, already published by the screener): every ticker the library's narrators discussed,
    minus the screener's own."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT ticker FROM kg_views WHERE ticker IS NOT NULL AND ticker <> ''")
        library = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT ticker FROM kg_market WHERE source = 'screener'")
        screener = {r[0] for r in cur.fetchall()}
    snapshot = stock_data.load_data_snapshot() or {}
    screener |= {row.get("ticker") for rows in (snapshot.get("per_market") or {}).values() for row in rows}
    return sorted(library - screener), len(library & screener)


def library_published_minutes_ago(conn, now=None):
    """Minutes since the newest library row was published, or None (no library rows, or a time that
    can't be read -- then the run publishes)."""
    with conn.cursor() as cur:
        cur.execute("SELECT max(updated_at) FROM kg_market WHERE source = 'library'")
        row = cur.fetchall()
    try:
        last = datetime.fromisoformat(str(row[0][0]).replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except (IndexError, TypeError, ValueError):
        return None
    return ((now or datetime.now(timezone.utc)) - last).total_seconds() / 60


def publish_library(conn, min_gap=None):
    """Compute and publish the library's companies; with min_gap (minutes), skip when they were published
    more recently than that -- the hourly step in data-refresh.yml and the 7/17 fallback job both ask."""
    if min_gap:
        ago = library_published_minutes_ago(conn)
        if ago is not None and ago < min_gap:
            print(f"Library companies were published {ago:.0f} min ago (under {min_gap}); skipped.")
            return 0
    tickers, tracked = library_tickers(conn)
    limit = os.environ.get("MARKET_LIMIT", "").strip()
    if limit.isdigit() and int(limit) > 0:
        tickers = tickers[:int(limit)]
    print(f"{len(tickers)} library ticker(s) to compute ({tracked} more are the screener's own).")
    if not tickers:
        return 0
    settings = stock_data.load_settings()
    log = io.StringIO()
    # Yahoo's complaints name the ticker ("$XYZ.NS: possibly delisted", "Failed downloads: [...]") and come
    # through yfinance's logger and stderr, not stdout. Library tickers aren't in log_redact.py's lists, so
    # all three are kept here: a local run of the first version printed them, and the public Actions log
    # would have shown them too.
    yf_logger = logging.getLogger("yfinance")
    saved = (yf_logger.handlers[:], yf_logger.propagate)
    yf_logger.handlers, yf_logger.propagate = [logging.StreamHandler(log)], False
    try:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            # A BSE listing's "<scrip code>.BO" is priced under Yahoo's SYMBOL.BO (market_lookup.price_symbols),
            # and an NSE listing too new for the indicators under the same symbol on BSE.
            rows = market_lookup.compute_priced(tickers, settings, db=market_lookup.MarketDB(conn),
                                                ticker_index=stock_data.load_ticker_index())
    except Exception as e:
        print(f"::error::Computation failed ({type(e).__name__}); nothing published.")
        return 1
    finally:
        yf_logger.handlers, yf_logger.propagate = saved
    as_of = _now()
    records = [market_lookup.priced_record(t, row, "library", settings=settings, as_of=as_of) for t, row in rows.items()]
    kept = keep_previous_values(conn, records, {t: row.get("details_failed") for t, row in rows.items()})
    n = upsert(conn, records) if records else 0
    write_catalog_and_settings(conn, settings)
    failures = sum(1 for line in log.getvalue().splitlines()
                   if any(w in line.lower() for w in ("error", "failed", "delisted", "rate limit")))
    print(f"Published {n} of {len(tickers)}; {len(tickers) - n} returned no data from Yahoo ({failures} error line(s) suppressed); "
          f"{kept} kept earlier values for fields Yahoo left blank.")
    # Nothing at all computed means Yahoo refused the run: fail, so the slot gate tries again.
    return 0 if n else 1


# ---- watchlists, breadth and closes (the watchlists mode, run by market-breadth.yml after its commit) ----

# market_breadth.json's index blocks, as the research library names them.
INDEX_SERIES = {"INDIA": "Nifty 500", "US": "S&P 500", "NASDAQ100": "Nasdaq 100"}
# What a breadth series' "latest" reading carries (the rest of a block is its history).
BREADTH_LATEST_KEYS = ("above", "below", "total", "pct_above", "pct_above_50", "partial", "live")
# Daily closes kept in kg_prices: two years, enough for any return or chart a chat asks about.
PRICE_DAYS = 730
# Each run rewrites a stored series' last 10 days (a "live" reading redone, a late Yahoo bar) and writes a
# series that isn't stored yet in full: a few hundred rows an hour instead of ~85k.
REWRITE_DAYS = 10


def watchlist_records(registry, watchlists, groups, group_labels):
    """[{name, label, kind, benchmark, ord, tickers}]: every tab in markets.json order (kind "watchlist"), then
    the combined tabs (kind "group": their members' stocks, each once; a benchmark only when every member
    shares one)."""
    out = []
    for i, (key, meta) in enumerate(registry.items()):
        meta = meta if isinstance(meta, dict) else {}
        out.append({"name": key, "label": meta.get("label") or key, "kind": "watchlist", "benchmark": meta.get("benchmark"),
                    "ord": i, "tickers": list(dict.fromkeys(t for t in watchlists.get(key) or [] if t))})
    by_name = {w["name"]: w for w in out}
    for j, (key, label) in enumerate(group_labels.items()):
        members = [m for m in groups.get(key) or [] if m in by_name]
        if not members:
            continue
        benchmarks = {by_name[m]["benchmark"] for m in members}
        out.append({"name": key, "label": label, "kind": "group",
                    "benchmark": benchmarks.pop() if len(benchmarks) == 1 else None, "ord": len(registry) + j,
                    "tickers": list(dict.fromkeys(t for m in members for t in by_name[m]["tickers"]))})
    return out


def _breadth_points(result):
    """[(date, % above 200d, % above 50d, % at highs, % at lows)] oldest first, from a breadth block or a
    breadth_math.compute_breadth result (the same keys)."""
    hist = result.get("history") or {}
    h50, highs, lows = (result.get(k) or {} for k in ("history_50", "highs_history", "lows_history"))
    return [(d, hist[d], h50.get(d), highs.get(d), lows.get(d)) for d in sorted(hist)]


def breadth_records(breadth, perf, lists):
    """[{series, label, kind, latest, as_of, points}]: each index block market_breadth.json holds, then each
    watchlist and group over its stocks' closes in dashboard_perf.json -- breadth_math, the maths of the app's
    Watchlist Breadth chart, so the two never disagree."""
    from breadth_math import assemble_closes, compute_breadth
    out = []
    for key, label in INDEX_SERIES.items():
        block = ((breadth or {}).get("markets") or {}).get(key)
        if not isinstance(block, dict) or not block.get("history"):
            continue   # a leg that failed keeps the stored series
        points = _breadth_points(block)
        latest = {k: block.get(k) for k in BREADTH_LATEST_KEYS if k in block}
        latest["date"] = points[-1][0]
        out.append({"series": key, "label": label, "kind": "index", "latest": latest,
                    "as_of": block.get("as_of") or (breadth or {}).get("as_of"), "points": points})
    markets = (perf or {}).get("markets") or {}
    for w in lists:
        closes, missing = assemble_closes(markets, w["tickers"])
        result = compute_breadth(closes) if not closes.empty else None
        if not result or not result.get("history"):
            continue
        points = _breadth_points(result)
        latest = {k: result.get(k) for k in BREADTH_LATEST_KEYS if k in result}
        latest.update(date=points[-1][0], stocks=len(w["tickers"]), without_prices=len(missing))
        out.append({"series": w["name"], "label": w["label"], "kind": w["kind"], "latest": latest,
                    "as_of": (perf or {}).get("as_of"), "points": points})
    return out


def price_series(perf, lists, today=None):
    """{ticker: [(date, close)]} for the last PRICE_DAYS: every watchlist stock, each watchlist's benchmark and
    the selectable indexes (dashboard_perf.json's "benchmarks"), each from the first block holding it."""
    blocks = [(perf or {}).get("benchmarks") or {}] + list(((perf or {}).get("markets") or {}).values())
    wanted = list(dict.fromkeys([t for w in lists for t in w["tickers"]] + [w["benchmark"] for w in lists if w["benchmark"]]
                                + list((perf or {}).get("benchmarks") or {})))
    cutoff = ((today or datetime.now(timezone.utc).date()) - timedelta(days=PRICE_DAYS)).isoformat()
    out = {}
    for t in wanted:
        series = next((b[t] for b in blocks if isinstance(b, dict) and b.get(t)), None)
        if series:
            out[t] = [(d, float(c)) for d, c in sorted(series.items()) if d >= cutoff and c is not None]
    return {t: s for t, s in out.items() if s}


def to_write(points, stored):
    """The points a run writes. `stored` is (newest stored date, stored row count) or None: everything for a
    series not stored yet, or stored with under 90% of the points (a first run cut off, rows from a test),
    else the points from REWRITE_DAYS before the newest stored date on."""
    if not stored or not stored[0] or (stored[1] or 0) < 0.9 * len(points):
        return list(points)
    since = (datetime.fromisoformat(str(stored[0])[:10]).date() - timedelta(days=REWRITE_DAYS)).isoformat()
    return [p for p in points if p[0] >= since]


def _stored(conn, table, key):
    """{key: (newest date, row count)} for kg_breadth by series or kg_prices by ticker."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {key}, max(date), count(*) FROM {table} GROUP BY {key}")
        return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def write_watchlists(conn, lists):
    """Every watchlist and its members, replaced in one transaction (a tab removed in the app disappears)."""
    now = _now()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kg_watchlist_members")
            cur.execute("DELETE FROM kg_watchlists")
            cur.executemany("INSERT INTO kg_watchlists (name, label, kind, benchmark, members, ord, updated_at) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            [(w["name"], w["label"], w["kind"], w["benchmark"], len(w["tickers"]), w["ord"], now) for w in lists])
            cur.executemany("INSERT INTO kg_watchlist_members (name, ticker) VALUES (%s, %s)",
                            [(w["name"], t) for w in lists for t in w["tickers"]])


def write_breadth(conn, series):
    """Upsert each series' reading and its new points; drop the series of watchlists that no longer exist
    (an index series is never dropped: a failed leg keeps its last one). The number of points written."""
    stored = _stored(conn, "kg_breadth", "series")
    rows = [(s["series"], *p) for s in series for p in to_write(s["points"], stored.get(s["series"]))]
    now = _now()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO kg_breadth_series (series, label, kind, latest, as_of, updated_at) "
                            "VALUES (%s, %s, %s, %s::jsonb, %s, %s) ON CONFLICT (series) DO UPDATE SET label = excluded.label, "
                            "kind = excluded.kind, latest = excluded.latest, as_of = excluded.as_of, updated_at = excluded.updated_at",
                            [(s["series"], s["label"], s["kind"], json.dumps(s["latest"]), s["as_of"], now) for s in series])
            cur.executemany("INSERT INTO kg_breadth (series, date, pct_above_200, pct_above_50, pct_highs, pct_lows) "
                            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (series, date) DO UPDATE SET "
                            "pct_above_200 = excluded.pct_above_200, pct_above_50 = excluded.pct_above_50, "
                            "pct_highs = excluded.pct_highs, pct_lows = excluded.pct_lows", rows)
            keep = [s["series"] for s in series]
            cur.execute("DELETE FROM kg_breadth WHERE series IN (SELECT series FROM kg_breadth_series "
                        "WHERE kind <> 'index' AND NOT (series = ANY(%s)))", (keep,))
            cur.execute("DELETE FROM kg_breadth_series WHERE kind <> 'index' AND NOT (series = ANY(%s))", (keep,))
    return len(rows)


def write_prices(conn, series, today=None):
    """Upsert the new closes, drop those older than PRICE_DAYS and the tickers no watchlist holds any more.
    (rows written, rows deleted)."""
    stored = _stored(conn, "kg_prices", "ticker")
    rows = [(t, d, c) for t, pts in series.items() for d, c in to_write(pts, stored.get(t))]
    cutoff = ((today or datetime.now(timezone.utc).date()) - timedelta(days=PRICE_DAYS)).isoformat()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO kg_prices (ticker, date, close) VALUES (%s, %s, %s) "
                            "ON CONFLICT (ticker, date) DO UPDATE SET close = excluded.close", rows)
            cur.execute("DELETE FROM kg_prices WHERE date < %s OR NOT (ticker = ANY(%s))", (cutoff, list(series)))
            deleted = cur.rowcount
    return len(rows), deleted


def _read_json(name):
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def publish_watchlists(conn):
    from watchlist_labels import COMBINED_TAB_LABELS
    lists = watchlist_records(stock_data.load_markets_registry(), stock_data.load_watchlists(),
                              stock_data.load_watchlist_groups(), COMBINED_TAB_LABELS)
    # An empty set means the data files didn't load; publishing it would wipe every list in the database.
    if not any(w["tickers"] for w in lists):
        print("::error::No watchlist has any stock -- the data files did not load; nothing published.")
        return 1
    perf, breadth = _read_json("dashboard_perf.json"), _read_json("market_breadth.json")
    write_watchlists(conn, lists)
    series = breadth_records(breadth, perf, lists)
    points = write_breadth(conn, series) if series else 0
    prices = price_series(perf, lists)
    written, deleted = write_prices(conn, prices) if prices else (0, 0)
    print(f"Published {sum(w['kind'] == 'watchlist' for w in lists)} watchlist(s) and {sum(w['kind'] == 'group' for w in lists)} "
          f"group(s) ({len({t for w in lists for t in w['tickers']})} stocks); breadth for "
          f"{sum(s['kind'] == 'index' for s in series)} index(es) and {sum(s['kind'] != 'index' for s in series)} list(s), "
          f"{points} point(s) written; closes for {len(prices)} ticker(s), {written} row(s) written, {deleted} removed.")
    if not perf:
        print("::warning::dashboard_perf.json missing -- watchlist breadth and closes not refreshed.")
    return 0


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    min_gap = None
    if "--min-gap" in argv:
        i = argv.index("--min-gap")
        try:
            min_gap = float(argv[i + 1])
        except (IndexError, ValueError):
            mode = ""
    if mode not in ("screener", "library", "watchlists"):
        print(__doc__)
        return 2
    url = os.environ.get("MARKET_DB_URL", "").strip()
    if not url:
        print("MARKET_DB_URL is not set -- the research library's database is not configured; nothing published.")
        return 0
    try:
        conn = connect(url)
    except Exception as e:
        # The driver's message can quote the host; the class says enough in a public log.
        print(f"::error::Could not connect to the research library's database ({type(e).__name__}).")
        return 1
    try:
        if mode == "watchlists":
            return publish_watchlists(conn)
        return publish_screener(conn) if mode == "screener" else publish_library(conn, min_gap)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
