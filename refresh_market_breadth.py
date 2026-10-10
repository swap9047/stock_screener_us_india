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

# One index series per market, to see whether a new session has closed since the
# stored block (needs_refresh) before downloading ~500 constituents.
SESSION_PROBES = {"US": "SPY", "INDIA": "^CRSLDX"}


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

def trim_to_completed_session(closes, tz, close_hhmm, now=None):
    """Drop the trailing row when it is today's still-forming bar.

    A slot is after its market's close (7 AM ET for India, 7 PM ET for the US),
    but a leg can still run while its exchange is open -- the job used to push
    the India leg to ~11:30 IST, mid-session, and a manual dispatch can land
    anywhere -- and then yfinance hands back a live intraday bar for today.
    Dropping it only when the exchange has not yet closed in its own timezone
    keeps a leg run after the close on its freshest settled bar.
    """
    if closes.empty:
        return closes
    now = now.tz_convert(tz) if now is not None else pd.Timestamp.now(tz=tz)
    close_h, close_m = close_hhmm
    # +30m of slack so Yahoo has settled the closing print before we trust it.
    cutoff = now.normalize() + pd.Timedelta(hours=close_h, minutes=close_m + 30)
    if closes.index[-1].date() == now.date() and now < cutoff:
        return closes.iloc[:-1]
    return closes

def latest_completed_session(ticker, tz, close_hhmm, download=None, now=None):
    """The date ("YYYY-MM-DD") of the latest COMPLETED session for `ticker`, or
    None when it can't be told -- in which case the market is refreshed, never
    skipped blind. One small download instead of ~500."""
    download = download or (lambda t: yf.download(t, period="10d", interval="1d",
                                                  auto_adjust=False, progress=False))
    try:
        data = download(ticker)
        closes = data["Close"]
        if isinstance(closes, pd.DataFrame):
            closes = closes.iloc[:, 0]
        closes = closes.dropna()
        if closes.empty:
            return None
        closes = trim_to_completed_session(closes, tz, close_hhmm, now=now)
        return None if closes.empty else closes.index[-1].strftime("%Y-%m-%d")
    except Exception as e:
        print(f"  session check for {ticker} failed ({e}); refreshing anyway")
        return None


def needs_refresh(block, latest_session):
    """False only when the stored block already holds the latest completed
    session: each slot has one new close (India's by 7 AM ET, the US's by
    7 PM ET), and re-downloading the other market's ~500 tickers x 6 years
    bought nothing but Yahoo throttling risk and ~25 minutes."""
    history = (block or {}).get("history") or {}
    if not history or not latest_session:
        return True
    return max(history) < latest_session


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
    closes = pd.concat(all_data, axis=1)
    closes = closes.dropna(how="all")
    closes = trim_to_completed_session(closes, tz, close_hhmm)
    if closes.empty:
        return None

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
        "lows_history": lows_dict
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

    legs = [
        ("US", get_sp500_tickers, "S&P 500", "America/New_York", (16, 0)),
        ("INDIA", get_nifty500_tickers, "Nifty 500", "Asia/Kolkata", (15, 30)),
    ]

    for key, get_tickers, label, tz, close_hhmm in legs:
        kept = results["markets"].get(key)
        latest = latest_completed_session(SESSION_PROBES[key], tz, close_hhmm)
        if not needs_refresh(kept, latest):
            # Nothing new since the stored block. Mark it current (as_of = now)
            # so the app doesn't label it stale -- "as_of" means "current as of",
            # and with no newer session it is.
            print(f"{label}: latest completed session {latest} already stored -- skipped")
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
        results["markets"][key] = block
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
