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
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Downloading 6y data (for 5y breadth + EMA warmup) for {len(tickers)} {label} tickers...")
    # Group into batches of 100 to avoid rate limits
    batch_size = 100
    all_data = []
    
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i+batch_size]
        data = yf.download(batch, period="6y", interval="1d", auto_adjust=True, progress=False, threads=True)
        if 'Close' in data.columns:
            closes = data['Close']
        else:
            closes = data
            
        all_data.append(closes)
        time.sleep(1)
        
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Calculating 5-Year Historical 200d EMA Breadth...")
    closes = pd.concat(all_data, axis=1)
    
    # Calculate 200d EMA
    ema200 = closes.ewm(span=200, adjust=False).mean()
    
    # Calculate boolean matrix: True if close > EMA200
    is_above = closes > ema200
    
    # Calculate daily percentage
    # is_above.sum(axis=1) is the number of stocks above EMA
    # closes.notna().sum(axis=1) is the number of active/valid stocks on that day
    valid_counts = closes.notna().sum(axis=1)
    breadth_series = (is_above.sum(axis=1) / valid_counts) * 100
    
    # Drop dates with zero valid stocks
    breadth_series = breadth_series[valid_counts > 0]
    
    # Drop the first 200 days of the dataset to account for EMA warmup
    if len(breadth_series) > 200:
        breadth_series = breadth_series.iloc[200:]
        
    # Keep only the last 5 years (approx 252 * 5 = 1260 trading days, let's just use dates)
    five_years_ago = pd.Timestamp.now(tz=breadth_series.index.tz) - pd.DateOffset(years=5)
    breadth_series = breadth_series[breadth_series.index >= five_years_ago]
    
    # Format into a dictionary { "2020-01-01": 55.4, ... }
    # Also save the current (latest) stats for the gauge chart
    latest_close = closes.iloc[-1]
    latest_ema = ema200.iloc[-1]
    above_now = (latest_close > latest_ema).sum()
    total_now = len(latest_close.dropna())
    
    history_dict = {}
    for date, val in breadth_series.items():
        # date might be Timestamp, format to string YYYY-MM-DD
        date_str = date.strftime("%Y-%m-%d")
        history_dict[date_str] = round(float(val), 1)
        
    pct_above_now = float(above_now / total_now * 100) if total_now > 0 else 0.0
    
    print(f"{label}: Latest {above_now}/{total_now} ({pct_above_now:.1f}%) above 200d EMA")
    return {
        "above": int(above_now),
        "below": int(total_now - above_now),
        "total": int(total_now),
        "pct_above": round(pct_above_now, 1),
        "history": history_dict
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
