import json
import os
import io
import time
from datetime import datetime, timezone
import requests
import pandas as pd
import yfinance as yf

from breadth_math import compute_breadth
import index_registry

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BREADTH_FILE = os.path.join(SCRIPT_DIR, "market_breadth.json")

# Minimum share of an index's constituents a run must actually capture before
# its numbers are believed. See the check in main() for why this exists.
MIN_COVERAGE_FRACTION = 0.5

# Pause after each 50-ticker download. It was 120 s, which made the wait alone
# ~20 minutes per market (11 batches); the owner cut it to 5 s on 2026-10-09. If
# Yahoo starts throttling (the 2026-08-29 run got 1 of 503 US tickers), this is
# the knob -- MIN_COVERAGE_FRACTION above keeps the previous block meanwhile.
BATCH_PAUSE_SECONDS = 5

# Tickers that failed their batch are retried one at a time, this far apart, for
# up to MAX_RETRY_PASSES rounds. It replaced a 3-minute and a 5-minute wait
# before two retry rounds, which is also why every failure is now retried: the
# old rounds were skipped when <= 1% failed, because they cost ~8 minutes for
# the same 2 Nifty symbols every run.
RETRY_PAUSE_SECONDS = 5
MAX_RETRY_PASSES = 3

# Minutes after the close before a day's reading counts as final rather than
# live (is_live): Yahoo needs a while to settle the closing print.
SETTLE_MINUTES = 30

# Yahoo publishes a day's daily bars with a lag and not all at once. Before it
# does, it serves the day as a row of NaN -- on 2026-10-09 at 9:15 PM ET every US
# name still had an empty Friday close, though the session's hourly bars were all
# there -- and part-way through, as a row only some names have (25 of 50 Nifty
# names at 9:20 PM). A part-published latest day is still used, computed from the
# names that have it (owner, 2026-10-09: always show the latest date), but when
# fewer than this share of the names that had a close over the five days before
# have one, the block is marked "partial" and recomputed at the next wake-up --
# otherwise that reading would stand for good, since its date satisfies
# needs_refresh. Under MIN_COVERAGE_FRACTION the day is too thin to compute and
# is held back as unpublished (calculate_breadth).
PUBLISHED_MIN_SHARE = 0.9

# Closes already downloaded in this main() run, by ticker, and the tickers that
# failed every attempt (2026-10-28). Nifty 50, Midcap 150 and Smallcap 250 are
# all inside Nifty 500, and Nasdaq 100 is almost all inside the S&P 500, so a
# later leg takes those closes instead of downloading them again: the three Nifty
# legs cost almost nothing. A leg still fails on its own -- a ticker the earlier
# leg could not get is simply missing from both. None outside main(), so a direct
# call (the checks) always downloads.
_RUN_CACHE = None


def _download_one(ticker):
    from contextlib import redirect_stderr
    with redirect_stderr(io.StringIO()):
        return yf.download(ticker, period="6y", interval="1d", auto_adjust=False, progress=False)


def retry_failed(failed, download=None, sleep=None, label=""):
    """Retry each failed ticker on its own, RETRY_PAUSE_SECONDS apart, for up to
    MAX_RETRY_PASSES rounds. Returns (close frames recovered, still failed).

    A round that recovers nothing ends the loop: a delisted symbol fails every
    time, and so does everything while Yahoo is blocking the runner, and neither
    gets better by asking again 5 s later."""
    download = download or _download_one
    sleep = sleep or time.sleep
    recovered, failed = [], list(failed)
    for n in range(1, MAX_RETRY_PASSES + 1):
        if not failed:
            break
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {label}: retry round {n}/{MAX_RETRY_PASSES} "
              f"for {len(failed)} failed ticker(s), {RETRY_PAUSE_SECONDS}s apart")
        still = []
        for t in failed:
            sleep(RETRY_PAUSE_SECONDS)
            try:
                data = download(t)
                s = data["Close"] if not data.empty and "Close" in data.columns else pd.Series(dtype=float)
            except Exception:
                s = pd.Series(dtype=float)
            # yfinance 1.5 returns a one-column FRAME for a single ticker
            # (MultiIndex columns), so take the column before testing it. The
            # retry code this replaced tested the frame directly --
            # `not frame.isna().all()` raises "truth value of a Series is
            # ambiguous" -- and took the whole market leg down whenever its
            # retries actually ran.
            if isinstance(s, pd.DataFrame):
                s = s.iloc[:, 0] if s.shape[1] else pd.Series(dtype=float)
            if not s.isna().all():
                recovered.append(s.rename(t).to_frame())
            else:
                still.append(t)
        if len(still) == len(failed):
            failed = still
            break
        failed = still
    return recovered, failed

def get_sp500_tickers():
    url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
    headers = {'User-Agent': 'Mozilla/5.0'}
    # timeout, like every other requests call here: a hung scrape held the job
    # to GitHub's 6 h cap and, via the concurrency group, the next slot too.
    r = requests.get(url, headers=headers, timeout=30)
    table = pd.read_html(io.StringIO(r.text))[0]
    return table['Symbol'].str.replace('.', '-').tolist()

def get_nasdaq100_tickers():
    """Nasdaq-100 members (2026-10-15: the app's US column can switch to it).

    From Wikipedia's list page, like the S&P 500's -- the index article itself no
    longer carries the table. Nasdaq's own list API is the fallback: one source
    being down or reshaped should not cost the leg. Fewer than 90 names from
    either is treated as a broken parse, not a membership change.
    """
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        r = requests.get('https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies',
                         headers=headers, timeout=30)
        table = next(t for t in pd.read_html(io.StringIO(r.text)) if 'Ticker' in t.columns)
        tickers = table['Ticker'].astype(str).str.strip().str.replace('.', '-').tolist()
        if len(tickers) >= 90:
            return tickers
        print(f"  Nasdaq-100 list from Wikipedia had only {len(tickers)} names -- trying Nasdaq")
    except Exception as e:
        print(f"  Nasdaq-100 list from Wikipedia failed ({e}) -- trying Nasdaq")
    r = requests.get('https://api.nasdaq.com/api/quote/list-type/nasdaq100',
                     headers={**headers, 'Accept': 'application/json'}, timeout=30)
    rows = ((r.json().get('data') or {}).get('data') or {}).get('rows') or []
    tickers = [str(row['symbol']).strip().replace('.', '-') for row in rows if row.get('symbol')]
    if len(tickers) < 90:
        raise ValueError(f"Nasdaq-100 list has only {len(tickers)} names from either source")
    return tickers

def get_nse_index_tickers(list_name, size=None):
    """An NSE index's members, as Yahoo symbols, from NSE's own list CSV
    (index_registry's nse_list). Fewer than 80% of `size` names is a broken
    download (an HTML error page parses as a one-column table), not a
    membership change, so it raises and the leg keeps its stored block."""
    url = f'https://nsearchives.nseindia.com/content/indices/{list_name}'
    headers = {'User-Agent': 'Mozilla/5.0'}
    r = requests.get(url, headers=headers, timeout=30)
    df = pd.read_csv(io.StringIO(r.text))
    tickers = [f"{str(sym).strip()}.NS" for sym in df['Symbol'].tolist()]
    if size and len(tickers) < 0.8 * size:
        raise ValueError(f"{list_name} has only {len(tickers)} names (expected about {size})")
    return tickers

def get_nifty500_tickers():
    return get_nse_index_tickers("ind_nifty500list.csv", 500)


# The member lists that are not an NSE CSV, by index key. Looked up by name when
# a leg runs, so the checks can replace any of them.
MEMBER_SOURCES = {"US": "get_sp500_tickers", "NASDAQ100": "get_nasdaq100_tickers",
                  "INDIA": "get_nifty500_tickers"}


def members_getter(ix):
    """A no-argument function returning index_registry entry `ix`'s members."""
    name = MEMBER_SOURCES.get(ix["key"])
    if name:
        return lambda: globals()[name]()
    return lambda: get_nse_index_tickers(ix["nse_list"], ix.get("size"))

def expected_session(tz, open_hhmm, now=None):
    """The latest session that has STARTED by now, from the exchange's own
    clock: today in `tz` once the exchange has opened, else the day before,
    stepping back over weekends. "YYYY-MM-DD".

    From the open, not the close (owner, 2026-10-14): the line follows the
    latest prices fetched, today's still-forming bar included, rather than
    waiting for the day's close. A reading taken before the close is stored as
    live (is_live) and recomputed at every later run until the final one lands.

    Replaced asking Yahoo, which answered from one index series (SPY, ^CRSLDX)
    and lagged: on 2026-10-09 it still gave Thursday at 8:35 PM and 9:12 PM ET,
    so both markets were skipped as "already stored" -- ^CRSLDX had even lost a
    Friday close it served that morning, while its constituents kept theirs.

    Exchange holidays are not known here. On one the stored block is a day short
    of this, the market is downloaded, and calculate_breadth finds no row at all
    for the day -- which is how a holiday is told from a day Yahoo has not
    published any close for yet (a row of NaN, see unpublished_tail).
    """
    now = now.tz_convert(tz) if now is not None else pd.Timestamp.now(tz=tz)
    open_h, open_m = open_hhmm
    day = now.normalize()
    if now < day + pd.Timedelta(hours=open_h, minutes=open_m):
        day -= pd.Timedelta(days=1)
    while day.weekday() >= 5:
        day -= pd.Timedelta(days=1)
    return day.strftime("%Y-%m-%d")


def is_live(day, tz, close_hhmm, now=None):
    """True when `day` ("YYYY-MM-DD") is today in `tz` and its close has not yet
    settled (SETTLE_MINUTES after it): its bar is still forming, and the reading
    built from it is not final."""
    now = now.tz_convert(tz) if now is not None else pd.Timestamp.now(tz=tz)
    close_h, close_m = close_hhmm
    settled = now.normalize() + pd.Timedelta(hours=close_h, minutes=close_m + SETTLE_MINUTES)
    return day == now.strftime("%Y-%m-%d") and now < settled


def unpublished_tail(closes):
    """Dates ("YYYY-MM-DD") of the trailing rows with no close at all: sessions
    Yahoo lists but has published nothing for yet. Nothing can be computed for
    them; a holiday, by contrast, has no row."""
    counts = closes.notna().sum(axis=1)
    n = 0
    while n < len(counts) and counts.iloc[-1 - n] == 0:
        n += 1
    return [d.strftime("%Y-%m-%d") for d in counts.index[len(counts) - n:]]


def latest_day_share(closes):
    """How many names have a close on the last day, as a share of the median
    count over the five days before (1.0 when there is too little to compare)."""
    counts = closes.notna().sum(axis=1)
    if len(counts) < 6:
        return 1.0
    reference = counts.iloc[-6:-1].median()
    return float(counts.iloc[-1] / reference) if reference else 1.0


# Series added after blocks were first stored: needs_refresh redoes a block
# without one, and a stored block kept over a staler download takes the
# download's (_run_legs).
CARRIED_SERIES = ("history_50", "drawdown_history", "drawdown")


def lacks_series(block, series):
    """True when `block` has no `series`, or -- for the drawdown ones -- lacks one
    of breadth_math.DRAWDOWN_LEVELS: the 50% level became 40% on 2026-10-30, and a
    block stored with the old levels has a drawdown series without the new one."""
    value = (block or {}).get(series)
    if series in ("drawdown_history", "drawdown"):
        from breadth_math import DRAWDOWN_LEVELS
        return not value or any(str(lvl) not in value for lvl in DRAWDOWN_LEVELS)
    return not value


def needs_refresh(block, expected):
    """False only when the stored block already holds the latest session
    (expected_session) as a final reading -- a live or partial one is always
    redone. Each slot has one new close (India's by 7 AM ET, the US's by 7 PM
    ET), and re-downloading the other market's ~500 tickers x 6 years bought
    nothing but Yahoo throttling risk."""
    history = (block or {}).get("history") or {}
    if not history or not expected or block.get("partial") or block.get("live"):
        return True
    # A block from before the 50-day series existed (2026-10-15) is redone once,
    # rather than waiting for the next session to fill the overlay in.
    if "history_50" not in block:
        return True
    # Likewise a block from before the drawdown series (2026-10-30), or from
    # before one of its levels (lacks_series).
    if lacks_series(block, "drawdown_history"):
        return True
    return max(history) < expected


def calculate_breadth(tickers, label, tz, close_hhmm):
    import io
    from contextlib import redirect_stderr

    cache = _RUN_CACHE
    requested = list(dict.fromkeys(tickers))
    if cache is not None:
        reused = [t for t in requested if t in cache["closes"]]
        tickers = [t for t in requested if t not in cache["closes"] and t not in cache["failed"]]
        if len(tickers) < len(requested):
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {label}: {len(reused)} of {len(requested)} "
                  f"already downloaded this run, {len(requested) - len(reused) - len(tickers)} already failed")
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Downloading 6y data (for 5y breadth + SMA warmup) for {len(tickers)} {label} tickers...")
    batch_size = 50
    all_data = []
    failed_tickers = set(tickers)
    
    # Phase 1: batches of 50, BATCH_PAUSE_SECONDS apart
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i+batch_size]
        f = io.StringIO()
        with redirect_stderr(f):
            # auto_adjust=False -> Yahoo's Close is still split-adjusted but keeps
            # dividends in the price, matching how public 200-DMA screeners compute
            # breadth. Back-adjusting for dividends drags the 200d average down and
            # inflates the % above by roughly a point.
            data = yf.download(batch, period="6y", interval="1d", auto_adjust=False, progress=False, threads=False)
            
        if 'Close' in data.columns:
            closes = data['Close']
        else:
            if isinstance(data, pd.DataFrame) and not data.empty:
                closes = data
            else:
                closes = pd.DataFrame()
                
        if isinstance(closes, pd.Series):
            closes = closes.to_frame(name=batch[0])
            
        missing = set(batch) - set(closes.columns)
        for col in closes.columns:
            if closes[col].isna().all():
                missing.add(col)
                closes = closes.drop(columns=[col])
                
        if not closes.empty:
            all_data.append(closes)
            failed_tickers -= set(closes.columns)
            
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {label}: Processed {min(i+batch_size, len(tickers))}/{len(tickers)}. Failures in this batch: {len(missing)}")
        if missing:
            # Count only -- see log_redact.py: this list is public index members, and
            # masking just the held ones among them would reveal which are held.
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Failed tickers in this batch: {len(missing)}")
            
        if i + batch_size < len(tickers):
            time.sleep(BATCH_PAUSE_SECONDS)
            
    # Phase 2: each failed ticker on its own, RETRY_PAUSE_SECONDS apart
    if failed_tickers:
        recovered, still = retry_failed(sorted(failed_tickers), label=label)
        all_data.extend(recovered)
        failed_tickers = set(still)

    if failed_tickers:
        # Count only -- see log_redact.py: this list is public index members, and
        # masking just the held ones among them would reveal which are held.
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Final failures that could not be downloaded: {len(failed_tickers)}")
        
    if cache is not None:
        for frame in all_data:
            for col in frame.columns:
                cache["closes"][col] = frame[col]
        cache["failed"].update(failed_tickers)
        all_data = [cache["closes"][t].to_frame() for t in requested if t in cache["closes"]]

    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Calculating Historical Metrics...")
    # Today's still-forming bar is KEPT (owner, 2026-10-14: follow the latest
    # fetch, not the day close); main() marks such a reading live. It used to be
    # dropped until 30 minutes after the close.
    closes = pd.concat(all_data, axis=1).sort_index()
    # Before dropna(how="all"): an unpublished day is a row of NaN, and dropping
    # it first would make it look like a holiday (no row at all).
    unpublished = unpublished_tail(closes)
    if unpublished:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {label}: Yahoo has no close "
              f"for {', '.join(unpublished)} yet -- nothing to compute it from")
    closes = closes.dropna(how="all")
    # A latest day fewer than MIN_COVERAGE_FRACTION of the index has a close for
    # is not published yet either. At 9:28 PM ET on 2026-10-09 exactly 1 of 503
    # US names had a Friday close: computed from it, the day read 0.0% -- the
    # 2026-08-29 incident's shape -- and main()'s coverage check would have
    # thrown the whole block away as a failed fetch. Hold the day back instead.
    while len(closes) > 6 and latest_day_share(closes) < MIN_COVERAGE_FRACTION:
        unpublished.append(closes.index[-1].strftime("%Y-%m-%d"))
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {label}: {closes.index[-1].date()} is "
              f"{latest_day_share(closes):.0%} published -- too few names to compute it from yet")
        closes = closes.iloc[:-1]
    unpublished.sort()
    if closes.empty:
        return None
    share = latest_day_share(closes)
    partial = share < PUBLISHED_MIN_SHARE
    if partial:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {label}: {closes.index[-1].date()} is only "
              f"{share:.0%} published -- computed from the names that have it, marked partial")

    # The maths itself is shared with the app's per-watchlist breadth view
    # (breadth_math.compute_breadth), so the two count a stock the same way.
    block = compute_breadth(closes, since=pd.Timestamp.now(tz=closes.index.tz) - pd.DateOffset(years=5))
    if block is None:
        return None
    print(f"{label}: {closes.index[-1].date()} -- {block['above']}/{block['total']} "
          f"({block['pct_above']:.1f}%) above 200d SMA")
    # Stored, so the next run recomputes the day (needs_refresh).
    block["partial"] = partial
    # Not stored: main() pops it to tell "Yahoo is late" from a holiday.
    block["unpublished"] = unpublished
    return block

def main():
    print("=== Refreshing Market Breadth ===")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    # Start from what is already on disk rather than an empty dict. This used to
    # build "markets" from scratch and dump it unconditionally, so ONE failed leg
    # did not merely skip an update -- it deleted that market's stored history.
    # When lxml dropped out of the dependency tree the S&P scrape began raising,
    # and the very next run replaced 1055 days of US breadth with nothing; the
    # app then rendered an empty chart titled "Current: --%, Captured: --" for 8
    # days. Degrade to stale data, never to no data.
    previous = {}
    if os.path.exists(BREADTH_FILE):
        try:
            with open(BREADTH_FILE) as f:
                previous = json.load(f)
        except Exception as e:
            # A corrupt file must not stop the refresh -- we simply have no
            # earlier blocks to fall back on for a leg that fails.
            print(f"Warning: could not read existing {BREADTH_FILE}: {e}")

    # Falsy blocks are filtered out on the way in: an older snapshot can hold an
    # explicit null under a market key, and carrying that forward would keep
    # re-writing the value that used to crash the News tab.
    results = {
        "as_of": now,
        "markets": {k: v for k, v in (previous.get("markets") or {}).items() if v},
        "status": {},
    }

    # (key, members, label, exchange timezone, regular-session open, close), one
    # per index_registry entry, in its order: Nifty 500 ahead of the three Nifty
    # indexes inside it and the S&P 500 ahead of Nasdaq 100, so _RUN_CACHE has
    # their closes by then.
    legs = [(ix["key"], members_getter(ix), ix["label"], *index_registry.SESSIONS[ix["region"]])
            for ix in index_registry.INDEXES]

    global _RUN_CACHE
    _RUN_CACHE = {"closes": {}, "failed": set()}
    try:
        _run_legs(legs, results, now)
    finally:
        _RUN_CACHE = None

    # Atomic: this file is committed to the data repo, and a run cancelled
    # mid-dump would push a truncated one.
    from json_store import atomic_write_json
    atomic_write_json(BREADTH_FILE, results)

    print(f"Saved to {BREADTH_FILE}")
    print(f"Status: {results['status']}")

    # Deliberately exits 0 even on a failed leg. The workflow still has to run
    # refresh_dashboard_perf.py and commit, and bailing here would throw away the
    # market that DID refresh. The loud signal is a verify step after the commit.


def _run_legs(legs, results, now):
    """Each leg into results["markets"] / results["status"] (main's loop)."""
    for key, get_tickers, label, tz, open_hhmm, close_hhmm in legs:
        kept = results["markets"].get(key)
        expected = expected_session(tz, open_hhmm)
        if not needs_refresh(kept, expected):
            # Nothing new since the stored block. Mark it current (as_of = now)
            # so the app doesn't label it stale -- "as_of" means "current as of",
            # and with no newer session it is.
            print(f"{label}: expected session {expected} already stored -- skipped")
            kept["as_of"] = now
            results["status"][key] = "unchanged"
            continue
        try:
            tickers = get_tickers()
            block = calculate_breadth(tickers, label, tz, close_hhmm)
            # calculate_breadth returns None when every download batch failed.
            # Storing that None used to be worse than storing nothing: the app
            # does markets.get("US", {}).get("total"), which raises AttributeError
            # on None and takes down the whole News tab rather than one chart.
            if not block:
                raise ValueError("no usable price data returned")
            # A run can "succeed" having captured almost nothing. On 2026-08-29
            # Yahoo throttled the runner and the US leg came back with 1 of 503
            # constituents -- a perfectly well-formed block reporting 0.0% above
            # the 200d SMA, which passed the falsy check above, overwrote 501
            # good tickers and wrote a 0.0 point into the history. Coverage is
            # the only thing separating that from a real reading, so require a
            # majority of the requested universe. The floor is deliberately
            # loose: the India leg legitimately lands near 310/501 on the early
            # run and ~490 on the later one, and both are real data.
            captured = block.get("total") or 0
            if captured < MIN_COVERAGE_FRACTION * len(tickers):
                raise ValueError(
                    f"only {captured} of {len(tickers)} tickers captured "
                    f"(< {MIN_COVERAGE_FRACTION:.0%}) -- treating as a failed fetch, not a reading"
                )
        except Exception as e:
            print(f"Error processing {key} breadth: {e}")
            results["status"][key] = "failed"
            kept = results["markets"].get(key)
            if kept:
                print(f"  -> keeping previous {key} block from {kept.get('as_of', 'an unknown date')}")
            else:
                print(f"  -> no previous {key} block to fall back on; it stays absent")
            continue

        # Per-market as_of, because a preserved block is stale and the top-level
        # as_of no longer describes it. The UI reads this to label the chart.
        block["as_of"] = now
        unpublished = block.pop("unpublished", [])
        block["live"] = is_live(max(block.get("history") or {"": 0}), tz, close_hhmm)

        # Never trade a stored reading for a staler one: a block ending on an
        # earlier day, or the same day computed from fewer names. Yahoo's latest
        # day is not stable -- at 9:20 PM ET on 2026-10-09 only half the Nifty
        # names still had the Friday close 488 had when the 11:55 AM run stored it.
        # A final reading always beats a live one of the same day, and a newer
        # live one an older live one (for those the count says nothing).
        def _currency(b):
            final = not b.get("live")
            return (max(b.get("history") or {"": 0}), final, (b.get("total") or 0) if final else 0)
        if kept and _currency(kept) > _currency(block):
            k = _currency(kept)
            print(f"{label}: the stored block is more current ({k[0]}, {'final' if k[1] else 'live'}, "
                  f"{kept.get('total')} names) than this download -- keeping it")
            # A series the stored block lacks is taken from this download, which is a
            # day or a few names behind but otherwise right. The drawdown series
            # (2026-10-30) was added to every block by the first run after its merge,
            # and that run's downloads were staler than the stored blocks (Yahoo had
            # blanked India's latest closes, the S&P 500 came back a name short): it
            # kept five of the six blocks without the series, and needs_refresh redid
            # them -- a full download of every index -- each hour until the next session.
            # Its latest "drawdown" is the download's last day, as its history says.
            # The same goes for a series stored with an old set of levels (lacks_series).
            for series in CARRIED_SERIES:
                if lacks_series(kept, series) and block.get(series):
                    kept[series] = block[series]
                    print(f"{label}: {series} taken from this download (the stored block has none)")
            block = kept
            block["as_of"] = now
        results["markets"][key] = block
        # "awaiting": the expected session traded but Yahoo has no close for it
        # yet. "partial": the latest day is computed from part of the index.
        # Either way the block is saved (it is as current as Yahoo allows), and
        # the verify step fails the run so the slot gate tries again at the next
        # wake-up instead of counting the slot as done until the next one.
        # Short of the expected day with NO unpublished row is a holiday: "ok".
        last = max(block.get("history") or {"": 0})
        if last < expected and unpublished:
            print(f"{label}: Yahoo has no close for {expected} yet -- latest is {last}; will retry")
            results["status"][key] = "awaiting"
        elif block.get("partial"):
            print(f"{label}: {last} computed from {block.get('total')} names (partly published); will retry")
            results["status"][key] = "partial"
        elif block.get("live") and not is_live(last, tz, close_hhmm):
            # A reading taken mid-session, kept because this download had no
            # final close for the day yet (Yahoo blanks the daily row for hours
            # after the close). Retry like "awaiting", or the slot would count as
            # done on an intraday number.
            print(f"{label}: {last} is still the intraday reading and the close has passed; will retry")
            results["status"][key] = "awaiting"
        elif block.get("live"):
            # Not a failure: the session is still trading. needs_refresh redoes
            # it at the next run, which is the next slot (or a manual run).
            print(f"{label}: {last} is live (session still open) -- recomputed at the next run")
            results["status"][key] = "live"
        else:
            results["status"][key] = "ok"

if __name__ == "__main__":
    main()
