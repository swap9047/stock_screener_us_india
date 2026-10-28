"""Quarterly results job (quarterly-results.yml, daily): Yahoo's quarterly income
statements for every index_registry index member and every watchlist stock,
into quarterly_results.json (see quarterly_results.py for the file and why the
quarters are kept).

Only companies that are due are fetched (quarterly_results.is_due): never
fetched, results expected in this season and not stored yet, or not fetched for
REFETCH_DAYS. The first run fetches everyone (~1,100 companies); outside the
results seasons a run fetches a few dozen.

What was stored is never lost to a bad run: a company whose fetch failed keeps
its quarters, an index whose member list failed keeps its stored members, and
the file is written atomically. When Yahoo refuses most of a run (fewer than
MIN_SUCCESS_SHARE of the attempts return statements) the run stops early,
saves what it got, and exits 1 so the slot gate tries again.

Logs print counts only: index members are public, and naming only the held ones
among them would say which are held (log_redact.py masks watchlist tickers).
"""

import io
import json
import os
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone

import index_registry
import quarterly_results as qr

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(SCRIPT_DIR, qr.QUARTERLY_RESULTS_FILE)

# Pause between companies: one statements request each.
PAUSE_SECONDS = 0.4
# Stop and save after this long, well inside the job's timeout; what is left
# is still due at the next run.
TIME_BUDGET_SECONDS = 100 * 60
# Checked once this many companies were attempted: below this share returning
# statements, Yahoo is refusing the runner.
MIN_SUCCESS_SHARE = 0.3
CHECK_AFTER = 60


def load_previous():
    """The stored file, or an empty one -- never raises (regenerable data)."""
    try:
        with open(OUT_FILE) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def index_members(previous_members):
    """{index key: [tickers]}, each from its own member list; one that fails
    keeps the stored list."""
    import refresh_market_breadth as rmb
    out, failed = {}, []
    for ix in index_registry.INDEXES:
        try:
            out[ix["key"]] = list(dict.fromkeys(rmb.members_getter(ix)()))
        except Exception:
            failed.append(ix["key"])
            if previous_members.get(ix["key"]):
                out[ix["key"]] = previous_members[ix["key"]]
    return out, failed


def fetch_statement(ticker):
    """{period end: {row name: value}} from Yahoo, {} when it has none, None on an error."""
    import yfinance as yf
    try:
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            df = yf.Ticker(ticker).quarterly_income_stmt
    except Exception:
        return None
    if df is None or getattr(df, "empty", True):
        return {}
    return {str(col)[:10]: {str(r): df.at[r, col] for r in df.index} for col in df.columns}


def field_coverage(tickers):
    """{"India"/"US": {field: companies whose latest stored quarter has it}, plus
    "companies"} -- the log's check that Yahoo serves each field the charts use
    (Operating Income is missing for most banks, an EPS row for some Indian
    filers). Counts only: these are index members (see the module docstring)."""
    out = {}
    for t, entry in tickers.items():
        qs = entry.get("q") or {}
        if not qs:
            continue
        region = "India" if t.endswith((".NS", ".BO")) else "US"
        latest = qs[max(qs)]
        c = out.setdefault(region, {"companies": 0, **{f: 0 for f in qr.ROWS}})
        c["companies"] += 1
        for f in qr.ROWS:
            c[f] += latest.get(f) is not None
    return out


def run(previous, watch_tickers, today, fetch=fetch_statement, members_fn=index_members,
        sleep=time.sleep, clock=time.monotonic):
    """The new file's content and an exit code."""
    members, members_failed = members_fn(previous.get("members") or {})
    universe = list(dict.fromkeys([t for m in members.values() for t in m] + list(watch_tickers)))
    stored = previous.get("tickers") or {}
    tickers = {t: stored[t] for t in universe if t in stored}
    due = [t for t in universe if qr.is_due(stored.get(t), today)]
    print(f"{len(universe)} companies ({len(watch_tickers)} from watchlists); {len(due)} due today")
    start = clock()
    tried = got = empty = errors = 0
    stopped = None
    for t in due:
        if clock() - start > TIME_BUDGET_SECONDS:
            stopped = "time budget"
            break
        if tried >= CHECK_AFTER and got < MIN_SUCCESS_SHARE * tried:
            stopped = "Yahoo refusing"
            break
        tried += 1
        periods = fetch(t)
        if periods is None:
            errors += 1
        else:
            entry = tickers.get(t) or {}
            new = qr.normalize(periods)
            got += bool(new)
            empty += not new
            tickers[t] = {"fetched": today.isoformat(), "q": qr.merge(entry.get("q"), new)}
        sleep(PAUSE_SECONDS)
    status = {"companies": len(universe), "due": len(due), "fetched": got, "no_statements": empty,
              "errors": errors, "members_failed": members_failed, "stopped": stopped}
    print(f"Fetched {got} with statements, {empty} without, {errors} errors"
          + (f"; stopped early ({stopped}), {len(due) - tried} left for the next run" if stopped else ""))
    if members_failed:
        print(f"::warning::member list failed for {members_failed} -- kept the stored lists")
    coverage = field_coverage(tickers)
    status["coverage"] = coverage
    for region, c in sorted(coverage.items()):
        print(f"{region}: {c['companies']} companies with results; latest quarter has "
              + ", ".join(f"{f} {c[f]}" for f in qr.ROWS))
    out = {"as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "tickers": tickers, "members": members, "status": status}
    code = 1 if stopped == "Yahoo refusing" else 0
    if stopped == "Yahoo refusing":
        print(f"::error::only {got} of {tried} companies returned statements -- Yahoo is refusing; "
              "saved what came back, the slot gate tries again")
    return out, code


def main():
    from json_store import atomic_write_json
    from stock_data import load_watchlists
    watch = [t for ts in load_watchlists().values() for t in ts if t]
    out, code = run(load_previous(), watch, datetime.now(timezone.utc).date())
    atomic_write_json(OUT_FILE, out)
    print(f"Saved {OUT_FILE}")
    return code


if __name__ == "__main__":
    sys.exit(main())
