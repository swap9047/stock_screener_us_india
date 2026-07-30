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

def calculate_breadth(tickers, label):
    import io
    from contextlib import redirect_stderr
    
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Downloading 6y data (for 5y breadth + EMA warmup) for {len(tickers)} {label} tickers...")
    batch_size = 50
    all_data = []
    failed_tickers = set(tickers)
    
    # Phase 1: Batches with 1 minute delay
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i+batch_size]
        f = io.StringIO()
        with redirect_stderr(f):
            data = yf.download(batch, period="6y", interval="1d", auto_adjust=True, progress=False, threads=False)
            
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
                retry_data = yf.download(t, period="6y", interval="1d", auto_adjust=True, progress=False)
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
                retry_data = yf.download(t, period="6y", interval="1d", auto_adjust=True, progress=False)
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
    
    # Calculate 200d EMA
    ema200 = closes.ewm(span=200, adjust=False).mean()
    is_above = closes > ema200
    
    # Calculate 52w High/Low (252 trading days)
    high52 = closes.rolling(window=252, min_periods=126).max()
    low52 = closes.rolling(window=252, min_periods=126).min()
    
    is_new_high = closes >= high52
    is_new_low = closes <= low52
    
    valid_counts = closes.notna().sum(axis=1)
    
    breadth_series = (is_above.sum(axis=1) / valid_counts) * 100
    highs_series = (is_new_high.sum(axis=1) / valid_counts) * 100
    lows_series = (is_new_low.sum(axis=1) / valid_counts) * 100
    
    valid_mask = valid_counts > 0
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
        
    latest_close = closes.iloc[-1]
    latest_ema = ema200.iloc[-1]
    above_now = (latest_close > latest_ema).sum()
    total_now = len(latest_close.dropna())
    pct_above_now = float(above_now / total_now * 100) if total_now > 0 else 0.0
    
    print(f"{label}: Latest {above_now}/{total_now} ({pct_above_now:.1f}%) above 200d EMA")
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
    results = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "markets": {}
    }
    
    try:
        sp500 = get_sp500_tickers()
        results["markets"]["US"] = calculate_breadth(sp500, "S&P 500")
    except Exception as e:
        print(f"Error processing US breadth: {e}")
        
    try:
        nifty = get_nifty500_tickers()
        results["markets"]["INDIA"] = calculate_breadth(nifty, "Nifty 500")
    except Exception as e:
        print(f"Error processing India breadth: {e}")
        
    with open(BREADTH_FILE, "w") as f:
        json.dump(results, f, indent=2)
        
    print(f"Saved to {BREADTH_FILE}")

if __name__ == "__main__":
    main()
