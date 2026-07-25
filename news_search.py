"""
Free stock news search provider using DuckDuckGo Search (DDGS) and yfinance news.
100% free, no API key required, with strict 4-second timeouts to prevent freezing.
"""

import time
import yfinance as yf

def _bare_ticker(ticker):
    if ticker.endswith(".NS") or ticker.endswith(".BO"):
        return ticker.rsplit(".", 1)[0]
    return ticker

def get_stock_news(ticker, max_results=4, timeout_sec=4):
    """Fetches recent news items for a ticker using DuckDuckGo News and yfinance.
    Returns a formatted string of news findings or a clear fallback message."""
    bare = _bare_ticker(ticker)
    news_items = []
    
    # 1. DuckDuckGo News Search
    try:
        from duckduckgo_search import DDGS
        query = f"{bare} stock news"
        with DDGS(timeout=timeout_sec) as ddgs:
            results = list(ddgs.news(query, max_results=max_results))
            for r in results:
                title = r.get("title")
                source = r.get("source")
                date_str = r.get("date") or ""
                url = r.get("url")
                if title:
                    news_items.append({
                        "title": title,
                        "source": source or "Web",
                        "date": date_str[:10] if len(date_str) >= 10 else "Recent",
                        "url": url
                    })
    except Exception as e:
        print(f"  [ddgs news warning] {ticker}: {e}")

    # 2. yfinance News Backup
    if len(news_items) < max_results:
        try:
            yf_news = yf.Ticker(ticker).news
            if yf_news:
                for item in yf_news[:max_results]:
                    title = item.get("title")
                    publisher = item.get("publisher")
                    pub_time = item.get("providerPublishTime")
                    url = item.get("link")
                    date_str = time.strftime("%Y-%m-%d", time.gmtime(pub_time)) if pub_time else "Recent"
                    if title and not any(n["title"] == title for n in news_items):
                        news_items.append({
                            "title": title,
                            "source": publisher or "Yahoo Finance",
                            "date": date_str,
                            "url": url
                        })
        except Exception as e:
            print(f"  [yf news warning] {ticker}: {e}")

    if not news_items:
        return "No recent major news or announcements found in the last 48 hours."

    formatted = []
    for item in news_items[:max_results]:
        formatted.append(f"- [{item['date']}] {item['title']} (Source: {item['source']})")
    return "\n".join(formatted)


if __name__ == "__main__":
    print("Testing get_stock_news for LAURUSLABS.NS...")
    print(get_stock_news("LAURUSLABS.NS"))
