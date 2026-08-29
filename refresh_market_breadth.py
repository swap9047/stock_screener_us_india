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

def get_sp500_tickers():
    url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
    headers = {'User-Agent': 'Mozilla/5.0'}
    r = requests.get(url, headers=headers)
    table = pd.read_html(io.StringIO(r.text))[0]
    return table['Symbol'].str.replace('.', '-').tolist()

def get_nifty500_tickers():
    url = 'https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv'
    headers = {'User-Agent': 'Mozilla/5.0'}
    r = requests.get(url, headers=headers)
    df = pd.read_csv(io.StringIO(r.text))
    return [f"{sym}.NS" for sym in df['Symbol'].tolist()]

def trim_to_completed_session(closes, tz, close_hhmm):
    """Drop the trailing row when it is today's still-forming bar.

    The job is scheduled for the US close, but the throttled downloads push the
    India leg to roughly 11:30 IST -- mid-NSE-session -- so yfinance hands back a
    live intraday bar for today. Dropping it only when the exchange has not yet
    closed in its own timezone keeps the US leg (which runs ~22:00 ET, well after
    the 16:00 close) on its freshest settled bar.
    """
    if closes.empty:
        return closes
    now = pd.Timestamp.now(tz=tz)
    close_h, close_m = close_hhmm
    # +30m of slack so Yahoo has settled the closing print before we trust it.
    cutoff = now.normalize() + pd.Timedelta(hours=close_h, minutes=close_m + 30)
    if closes.index[-1].date() == now.date() and now < cutoff:
        return closes.iloc[:-1]
    return closes

def calculate_breadth(tickers, label, tz, close_hhmm):
    import io
    from contextlib import redirect_stderr

    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Downloading 6y data (for 5y breadth + SMA warmup) for {len(tickers)} {label} tickers...")
    batch_size = 50
    all_data = []
    failed_tickers = set(tickers)
    
    # Phase 1: Batches with 1 minute delay
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
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Failed tickers in this batch: {missing}")
            
        if i + batch_size < len(tickers):
            time.sleep(120) # 2 minute wait between batches
            
    # Phase 2: Retry failures with 3 minute wait
    if failed_tickers:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Phase 1 complete. {len(failed_tickers)} failed tickers. Waiting 3 minutes before Phase 2 retry...")
        time.sleep(180)
        
        for t in list(failed_tickers):
            with redirect_stderr(io.StringIO()):
                retry_data = yf.download(t, period="6y", interval="1d", auto_adjust=False, progress=False)
            if not retry_data.empty and 'Close' in retry_data.columns and not retry_data['Close'].isna().all():
                s = retry_data['Close']
                s.name = t
                all_data.append(s.to_frame())
                failed_tickers.remove(t)
            time.sleep(3) # Small delay between individual retries
            
    # Phase 3: Retry remaining failures with 5 minute wait
    if failed_tickers:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Phase 2 complete. {len(failed_tickers)} still failed. Waiting 5 minutes before Phase 3 (Final) retry...")
        time.sleep(300)
        
        for t in list(failed_tickers):
            with redirect_stderr(io.StringIO()):
                retry_data = yf.download(t, period="6y", interval="1d", auto_adjust=False, progress=False)
            if not retry_data.empty and 'Close' in retry_data.columns and not retry_data['Close'].isna().all():
                s = retry_data['Close']
                s.name = t
                all_data.append(s.to_frame())
                failed_tickers.remove(t)
            time.sleep(3)
            
    if failed_tickers:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Final failures that could not be downloaded: {failed_tickers}")
        
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

    # Calculate 52w High/Low (252 trading days)
    high52 = closes.rolling(window=252, min_periods=126).max()
    low52 = closes.rolling(window=252, min_periods=126).min()

    is_new_high = closes >= high52
    is_new_low = closes <= low52

    valid_counts = closes.notna().sum(axis=1)
    # Breadth needs its own denominator: only stocks that actually have a 200d SMA.
    ma_counts = (closes.notna() & sma200.notna()).sum(axis=1)

    breadth_series = (is_above.sum(axis=1) / ma_counts) * 100
    highs_series = (is_new_high.sum(axis=1) / valid_counts) * 100
    lows_series = (is_new_low.sum(axis=1) / valid_counts) * 100

    valid_mask = (valid_counts > 0) & (ma_counts > 0)
    breadth_series = breadth_series[valid_mask]
    highs_series = highs_series[valid_mask]
    lows_series = lows_series[valid_mask]
    
    # Drop first 252 days for warmup
    if len(breadth_series) > 252:
        breadth_series = breadth_series.iloc[252:]
        highs_series = highs_series.iloc[252:]
        lows_series = lows_series.iloc[252:]
        
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
        try:
            tickers = get_tickers()
            block = calculate_breadth(tickers, label, tz, close_hhmm)
            # calculate_breadth returns None when every download batch failed.
            # Storing that None used to be worse than storing nothing: the app
            # does markets.get("US", {}).get("total"), which raises AttributeError
            # on None and takes down the whole News tab rather than one chart.
            if not block:
                raise ValueError("no usable price data returned")
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

    with open(BREADTH_FILE, "w") as f:
        json.dump(results, f, indent=2)

    print(f"Saved to {BREADTH_FILE}")
    print(f"Status: {results['status']}")

    # Deliberately exits 0 even on a failed leg. The workflow still has to run
    # refresh_dashboard_perf.py and commit, and bailing here would throw away the
    # market that DID refresh. The loud signal is a verify step after the commit.

if __name__ == "__main__":
    main()
