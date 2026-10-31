#!/usr/bin/env python3
"""Write the News tab's market reads (market_read.py) into market_read.json.

Run by market-read.yml at 7:15 AM ET (after NSE's close) and 5:15 PM ET (after the
US close). For Nifty 500 and the S&P 500 (market_read.READ_INDEX_KEYS), and then for India Invested and US
Invested (list_read.LIST_READ_KEYS: the Watchlist Breadth panels, read in the market the index read
describes), when the latest breadth reading is final and has no read yet, it asks Gemini for one,
saving after each read so a run cut short keeps what it wrote; an index that already has its session's read is
skipped, so a retry or a second run costs nothing.

    MARKET_READ_BACKFILL  n: also write the n sessions before the latest one that
                          have no read (seeds the "earlier reads" a new index starts
                          with). Default 0.
    MARKET_READ_FORCE     1: write the latest session's read again.

Exit 1 when a reading is still forming/part-published (the slot gate retries the
slot) or every model failed on a read; the reads that did succeed are saved either way.
Counts only in the log: a read describes the invested list and the repo's logs are public.
"""
import json
import os
import sys

import list_read
import market_read
import model_settings
from llm_util import make_client
from stock_data import load_markets_registry, load_settings, load_watchlists

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    try:
        with open(os.path.join(HERE, name)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def main():
    breadth, perf = _load("market_breadth.json"), _load("dashboard_perf.json")
    if not breadth.get("markets"):
        print("No market_breadth.json to read -- nothing to write.")
        return 1
    client = make_client()
    if client is None:
        print("No GEMINI_API_KEY set -- cannot write the market reads.")
        return 1
    backfill = os.environ.get("MARKET_READ_BACKFILL", "").strip()
    backfill = int(backfill) if backfill.isdigit() else 0
    force = os.environ.get("MARKET_READ_FORCE", "").strip().lower() in ("1", "true", "yes")

    # The models, attempts and thinking budget saved in the sidebar's Settings · Models ("R1").
    settings = load_settings()
    data = market_read.load_market_read()
    watchlists = load_watchlists()
    save = lambda: market_read.save_market_read(data)
    written, awaiting, failed = market_read.run(client, breadth, perf, watchlists, data,
                                                backfill=backfill, force=force, on_write=save,
                                                tiers=model_settings.tiers("R1", settings),
                                                budget=model_settings.budget("R1", settings))
    # The invested lists' reads (the Watchlist Breadth panels), after the index reads they take as the market
    # context. Step "R2" in Settings · Models.
    labels = {k: (v or {}).get("label") for k, v in load_markets_registry().items()}
    list_written, list_failed = list_read.run_lists(
        client, breadth, perf, watchlists, data, _load("data_snapshot.json"), _load("quarterly_results.json"),
        labels=labels, backfill=backfill, force=force, on_write=save,
        tiers=model_settings.tiers("R2", settings), budget=model_settings.budget("R2", settings))
    written += list_written
    failed += list_failed
    if written:
        save()
    print(f"[market read] {written} written, {awaiting} still forming, {failed} failed")
    return 1 if (awaiting or failed) else 0


if __name__ == "__main__":
    sys.exit(main())
