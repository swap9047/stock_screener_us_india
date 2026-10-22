#!/usr/bin/env python3
"""
Publish the screener's metrics into the research library's database (the transcription app's
knowledge graph on Supabase), where its Q&A pages and Claude chat read them.

    python3 publish_market.py screener   # the snapshot's tickers + their AI write-ups (data-refresh.yml)
    python3 publish_market.py library    # library companies the screener doesn't track (market-library.yml)

What may leave this repo is decided by market_api (a whitelist: never the watchlist a ticker is in,
notes, flags, Interested or custom columns). Rows go into kg_market, the metric menu into
kg_market_catalog and the calc settings into kg_market_meta, so the live endpoint (api/market.py)
computes with the same settings as the snapshot.

MARKET_DB_URL is the session-pooler URI of the `market_writer` role, which can write only those three
tables and read the listings and which tickers the library discusses. Unset (a fork, or before
setup) the script says so and exits 0. MARKET_LIMIT (a number) computes only the first N library
tickers, for a smoke test.

Prints counts only. The library's tickers are not in the files log_redact.py masks, so the
computation's own per-ticker messages are swallowed here rather than reaching this public repo's logs.
"""

import contextlib
import io
import json
import os
import sys
from datetime import datetime, timezone

import market_api
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


def publish_screener(conn):
    from expert_views import load_expert_views
    from fundamentals_eval import load_fundamentals
    snapshot = stock_data.load_data_snapshot()
    if not snapshot:
        print("No data_snapshot.json -- nothing to publish.")
        return 1
    settings = stock_data.load_settings()
    records = screener_records(snapshot, load_expert_views(), load_fundamentals(), settings)
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
          f"removed {removed} no longer tracked; catalog {catalog} metric(s).")
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


def publish_library(conn):
    tickers, tracked = library_tickers(conn)
    limit = os.environ.get("MARKET_LIMIT", "").strip()
    if limit.isdigit() and int(limit) > 0:
        tickers = tickers[:int(limit)]
    print(f"{len(tickers)} library ticker(s) to compute ({tracked} more are the screener's own).")
    if not tickers:
        return 0
    settings = stock_data.load_settings()
    log = io.StringIO()
    try:
        with contextlib.redirect_stdout(log):
            rows = market_api.compute_rows(tickers, settings=settings, ticker_index=stock_data.load_ticker_index())
    except Exception as e:
        print(f"::error::Computation failed ({type(e).__name__}); nothing published.")
        return 1
    as_of = _now()
    records = [market_api.public_record(row, "library", as_of=as_of, settings=settings) for row in rows.values()]
    n = upsert(conn, records) if records else 0
    write_catalog_and_settings(conn, settings)
    failures = sum(1 for line in log.getvalue().splitlines() if "ERROR" in line or "failed" in line.lower())
    print(f"Published {n} of {len(tickers)}; {len(tickers) - n} returned no data from Yahoo ({failures} error line(s) suppressed).")
    # Nothing at all computed means Yahoo refused the run: fail, so the slot gate tries again.
    return 0 if n else 1


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    if mode not in ("screener", "library"):
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
        return publish_screener(conn) if mode == "screener" else publish_library(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
