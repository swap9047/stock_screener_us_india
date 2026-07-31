import json
import os
import concurrent.futures
from datetime import datetime, timedelta, timezone
from google.genai import types

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FUNDAMENTALS_FILE = os.path.join(SCRIPT_DIR, "fundamentals.json")

def _generate_with_timeout(client, model, contents, config, timeout=120):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.models.generate_content, model=model, contents=contents, config=config)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"API call to {model} timed out after {timeout}s")

def fetch_fundamental_news(client, ticker, market, company_name, is_retry=False):
    as_of_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    exchange = "NSE/BSE-listed" if market == "INDIA" else "US-listed"
    
    bare = ticker.rsplit(".", 1)[0] if ticker.endswith(".NS") or ticker.endswith(".BO") else ticker
    name = f"{company_name} ({bare})" if company_name and company_name != ticker else bare

    prompt = (
        f"Search for the MOST RECENT quarterly earnings release (current quarter only, ignore older quarters), "
        f"forward guidance, and recent analyst coverage/ratings for {exchange} stock {name} "
        f"between {cutoff_date} and {as_of_date}. "
        "Extract hard numbers (EPS, Revenue, Guidance) and explicit analyst upgrades/downgrades. "
        "Be extremely concise. If there is no material news, output nothing."
    )
    
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(tools=[grounding_tool])
    
    try:
        resp = _generate_with_timeout(client, "models/gemma-4-26b-a4b-it", prompt, config, timeout=120)
        text = resp.text or ""
        if not text.strip():
            return "No recent fundamental news found.", "🔍 Gemma-4-26B (Google Search)"
        return text, "🔍 Gemma-4-26B (Google Search)"
    except Exception as e:
        print(f"  [fundamental gemma 26b search failed/timeout] {ticker}: {e} -> Falling back to 31b")
        try:
            resp = _generate_with_timeout(client, "models/gemma-4-31b-it", prompt, config, timeout=120)
            text = resp.text or ""
            if not text.strip():
                return "No recent fundamental news found.", "🔍 Gemma-4-31B (Google Search)"
            return text, "🔍 Gemma-4-31B (Google Search)"
        except Exception as e2:
            print(f"  [fundamental gemma 31b search failed/timeout] {ticker}: {e2}")
            if not is_retry:
                raise TimeoutError("Search timed out. Add to retry queue.")
            return "No recent fundamental news found.", "⚪ No Source"

def load_fundamentals():
    if not os.path.exists(FUNDAMENTALS_FILE):
        return {}
    try:
        with open(FUNDAMENTALS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_fundamentals(data):
    with open(FUNDAMENTALS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def _is_valid_view(view):
    if not view:
        return False
    sentiment = view.get("sentiment")
    if sentiment not in ("Positive", "Neutral", "Negative"):
        return False
    if "error" in str(view).lower() or "pending" in str(view).lower():
        return False
    return True

def generate_fundamental_view(client, row_data, news_text=None, news_source=None, is_retry=False):
    ticker = row_data.get("ticker", "UNKNOWN")
    market = row_data.get("market", "US")
    company_name = row_data.get("company_name", ticker)

    if news_text is None:
        news_text, news_source = fetch_fundamental_news(client, ticker, market, company_name, is_retry=is_retry)

    prompt = f"""You are a fundamental equities analyst. Review the provided news facts for {company_name} (Ticker: {ticker}) and extract the current quarter's Earnings, Guidance, and Analyst Coverage. Evaluate the overall fundamental sentiment and provide your reasoning.

======================================================================
RECENT FUNDAMENTAL NEWS & ANNOUNCEMENTS
======================================================================
{news_text}
======================================================================

Return ONLY a valid JSON object matching this schema:
{{
  "earnings_summary": "Q2 EPS beat by $0.05, Revenue $1.2B (+10% YoY) (or 'N/A if no news')",
  "future_guidance": "Maintained full-year revenue guidance; raised EPS guidance (or 'N/A if no news')",
  "analyst_coverage": "Upgraded by Morgan Stanley to Overweight (PT $150) (or 'N/A if no news')",
  "sentiment": "Positive" | "Neutral" | "Negative" | "Unknown",
  "reasoning": "Explain why you chose this sentiment based on the facts."
}}"""

    def _pending_fallback(reason, used_model="Error"):
        return {
            "earnings_summary": "N/A",
            "future_guidance": "N/A",
            "analyst_coverage": "N/A",
            "sentiment": "Unknown",
            "reasoning": f"Analysis pending -- {reason}",
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "news_source": news_source or "⚪ Unknown",
            "model_used": used_model,
        }

    # 1. Primary Reasoning Model (Gemini 3.1 Flash Lite with Reasoning)
    try:
        config_kwargs = {
            "response_mime_type": "application/json",
            "thinking_config": types.ThinkingConfig(thinking_budget=4096)
        }
        config = types.GenerateContentConfig(**config_kwargs)
        resp = client.models.generate_content(model="models/gemini-3.1-flash-lite", contents=prompt, config=config)
        data = json.loads(resp.text)
        data["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        data["news_used"] = news_text
        data["news_source"] = news_source or "⚪ Unknown"
        data["model_used"] = "gemini-3.1-flash-lite"
        return data
    except Exception as e:
        print(f"  [3.1-flash-lite reasoning failed] {ticker}: {e} -> Falling back to 31b")

    # 2. Fallback to 31b
    try:
        config = types.GenerateContentConfig(response_mime_type="application/json")
        resp = client.models.generate_content(model="models/gemma-4-31b-it", contents=prompt, config=config)
        data = json.loads(resp.text)
        data["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        data["news_used"] = news_text
        data["news_source"] = news_source or "⚪ Unknown"
        data["model_used"] = "gemma-4-31b-it (Fallback)"
        return data
    except Exception as e2:
        print(f"  [31b reasoning failed] {ticker}: {e2} -> Giving up")
        return _pending_fallback(str(e2))
