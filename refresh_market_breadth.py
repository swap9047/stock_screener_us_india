import json
import os
import io
import time
from datetime import datetime, timezone
import requests
import pandas as pd
import yfinance as yf

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

def get_nifty500_tickers():
    url = 'https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv'
    headers = {'User-Agent': 'Mozilla/5.0'}
    r = requests.get(url, headers=headers, timeout=30)
    df = pd.read_csv(io.StringIO(r.text))
    return [f"{sym}.NS" for sym in df['Symbol'].tolist()]

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


def needs_refresh(block, expected):
    """False only when the stored block already holds the latest session
    (expected_session), finally -- a live or partial reading is always redone: each slot has one new close (India's
    by 7 AM ET, the US's by 7 PM ET), and re-downloading the other market's ~500
    tickers x 6 years bought nothing but Yahoo throttling risk."""
    history = (block or {}).get("history") or {}
    if not history or not expected or block.get("partial") or block.get("live"):
        return True
    return max(history) < expected


def calculate_breadth(tickers, label, tz, close_hhmm):
    import io
    from contextlib import redirect_stderr

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

    # Calculate 200d SMA. This is the simple 200-day average -- the "200 DSMA"
    # convention used everywhere else in the app (stock_data.py) and by public
    # breadth screeners.
    #
    # min_periods=190 rather than 200: the panel is a union of every ticker's
    # calendar, so a name that simply had no print on a few days carries sporadic
    # NaNs. Demanding all 200 would drop ~190 perfectly good Nifty names over gaps
    # of 1-10 days. Averaging 190+ of the last 200 closes is the same number to
    # within rounding; what we actually want to exclude is recent listings, which
    # the cumulative-history mask below handles directly.
    sma200 = closes.rolling(window=200, min_periods=190).mean()
    sma200 = sma200.where(closes.notna().cumsum() >= 200)
    is_above = closes > sma200

    # Calculate 52w High/Low (252 trading days).
    #
    # Same cumulative-history mask the SMA gets above, for the same reason and
    # it was missing here: without it a stock with 126 sessions of history could
    # post a "52-week high" off a 126-day window (min_periods tolerates gaps
    # INSIDE the window, it does not require the window to be full), while
    # stocks too new to qualify at all still sat in the denominator. The
    # numerator was too generous and the denominator too large -- a split that
    # matters most in exactly the stretches this chart is read for, after an
    # IPO wave or an index reconstitution. Threshold is the window size, as
    # `>= 200` is for the 200-day SMA.
    enough_52w = closes.notna().cumsum() >= 252
    high52 = closes.rolling(window=252, min_periods=126).max().where(enough_52w)
    low52 = closes.rolling(window=252, min_periods=126).min().where(enough_52w)

    is_new_high = closes >= high52
    is_new_low = closes <= low52

    valid_counts = closes.notna().sum(axis=1)
    # Each series needs its OWN denominator: only stocks that actually have the
    # measure in question. ma_counts for the 200d SMA, hl_counts for the 52-week
    # window -- counting a stock that cannot yet qualify understates the rate.
    ma_counts = (closes.notna() & sma200.notna()).sum(axis=1)
    hl_counts = (closes.notna() & high52.notna()).sum(axis=1)

    breadth_series = (is_above.sum(axis=1) / ma_counts) * 100
    # Guard the division rather than leaning on valid_mask: a zero denominator
    # yields inf, and inf survives a later boolean mask if the row is kept.
    highs_series = (is_new_high.sum(axis=1) / hl_counts.replace(0, pd.NA)) * 100
    lows_series = (is_new_low.sum(axis=1) / hl_counts.replace(0, pd.NA)) * 100

    # Warmup: drop the first 252 rows of the DOWNLOADED panel so the 52w
    # high/low window is full. This used to be `iloc[252:]` applied AFTER
    # valid_mask -- but valid_mask had already removed the ~200-row SMA warmup,
    # so the two stacked and ~450 rows were lost. The 6y download then only
    # reached back ~4.2y, and the app's "5 Years" view sat blank for its first
    # ~9 months (history started 2022-06-28 instead of ~2021-09).
    # A panel too short to warm up keeps every row, as the old code did.
    warm = pd.Series(len(closes) <= 252, index=closes.index)
    warm.iloc[252:] = True
    valid_mask = (valid_counts > 0) & (ma_counts > 0) & (hl_counts > 0) & warm
    breadth_series = breadth_series[valid_mask]
    highs_series = highs_series[valid_mask]
    lows_series = lows_series[valid_mask]

    five_years_ago = pd.Timestamp.now(tz=breadth_series.index.tz) - pd.DateOffset(years=5)
    mask_5y = breadth_series.index >= five_years_ago
    
    breadth_series = breadth_series[mask_5y]
    highs_series = highs_series[mask_5y]
    lows_series = lows_series[mask_5y]
    
    history_dict = {}
    highs_dict = {}
    lows_dict = {}
    
    for date in breadth_series.index:
        date_str = date.strftime("%Y-%m-%d")
        history_dict[date_str] = round(float(breadth_series[date]), 1)
        highs_dict[date_str] = round(float(highs_series[date]), 1)
        lows_dict[date_str] = round(float(lows_series[date]), 1)
        
    last_valid_idx = closes.index[-1]
    latest_close = closes.loc[last_valid_idx]
    latest_sma = sma200.loc[last_valid_idx]
    has_ma = latest_close.notna() & latest_sma.notna()
    above_now = int((latest_close > latest_sma).sum())
    total_now = int(has_ma.sum())
    pct_above_now = float(above_now / total_now * 100) if total_now > 0 else 0.0

    print(f"{label}: {last_valid_idx.date()} -- {above_now}/{total_now} ({pct_above_now:.1f}%) above 200d SMA")
    return {
        "above": int(above_now),
        "below": int(total_now - above_now),
        "total": int(total_now),
        "pct_above": round(pct_above_now, 1),
        "history": history_dict,
        "highs_history": highs_dict,
        "lows_history": lows_dict,
        # Stored, so the next run recomputes the day (needs_refresh).
        "partial": partial,
        # Not stored: main() pops it to tell "Yahoo is late" from a holiday.
        "unpublished": unpublished,
    }

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

    # (key, members, label, exchange timezone, regular-session open, close)
    legs = [
        ("US", get_sp500_tickers, "S&P 500", "America/New_York", (9, 30), (16, 0)),
        ("INDIA", get_nifty500_tickers, "Nifty 500", "Asia/Kolkata", (9, 15), (15, 30)),
    ]

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

    # Atomic: this file is committed to the data repo, and a run cancelled
    # mid-dump would push a truncated one.
    from json_store import atomic_write_json
    atomic_write_json(BREADTH_FILE, results)

    print(f"Saved to {BREADTH_FILE}")
    print(f"Status: {results['status']}")

    # Deliberately exits 0 even on a failed leg. The workflow still has to run
    # refresh_dashboard_perf.py and commit, and bailing here would throw away the
    # market that DID refresh. The loud signal is a verify step after the commit.

if __name__ == "__main__":
    main()
