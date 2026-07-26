"""
LLM-generated news/announcements summary for the watchlist -- a
Perplexity-Finance-style digest built with a 3-stage hybrid architecture:

  Stage 1 (Web Search): gemini-2.5-flash with Google Search Grounding
     Fetches raw news & announcements for each batch of tickers.

  Stage 2 (Reasoning & Filtering): gemini-3.6-flash (no search)
     Filters raw batch notes using deep reasoning against strict recency
     (24-48h window) and material catalyst conditions (earnings, M&A, FDA,
     analyst rating changes, >=5% stock moves with reasons). Drops routine noise.

  Stage 3 (Final Collation): gemini-3.6-flash (no search)
     Combines filtered batch notes per market into a final, crisp, scannable brief.

Batch size is set to 8 (26 US tickers -> 4 batches, 47 India tickers -> 6 batches =
10 grounded search calls/day total, well under the 20 RPD cap for gemini-2.5-flash).
"""

import json
import os
import time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NEWS_SUMMARY_FILE = os.path.join(SCRIPT_DIR, "news_summary.json")

SEARCH_MODEL = "gemini-2.5-flash"
REASONING_MODEL = "gemini-3.6-flash"
BATCH_SIZE = 8
# 15s delay between search calls stays comfortably under 4 calls/min
SECONDS_BETWEEN_CALLS = 15

MARKET_LABELS = {"US": "US Watchlist", "INDIA": "India Watchlist"}


def get_gemini_api_key(st_secrets=None):
    """Returns the Gemini API key, checking (in order) a passed-in
    Streamlit secrets-dict-like object, then the GEMINI_API_KEY env var
    (how the GitHub Actions workflow supplies it). Mirrors
    github_sync.get_github_config / alerts.load_discord_webhook's pattern.
    Returns None if not configured anywhere."""
    if st_secrets is not None:
        try:
            if "GEMINI_API_KEY" in st_secrets:
                return st_secrets["GEMINI_API_KEY"]
        except Exception:
            pass
    return os.environ.get("GEMINI_API_KEY")


def get_nvidia_api_key(st_secrets=None):
    """Returns the NVIDIA API key for DeepSeek/Llama NIM endpoints."""
    if st_secrets is not None:
        try:
            if "NVIDIA_API_KEY" in st_secrets:
                return st_secrets["NVIDIA_API_KEY"]
        except Exception:
            pass
    return os.environ.get("NVIDIA_API_KEY")


def _bare_ticker(ticker):
    """Strips the .NS/.BO exchange suffix so the search prompt reads
    naturally (e.g. "RELIANCE" instead of "RELIANCE.NS")."""
    if ticker.endswith(".NS") or ticker.endswith(".BO"):
        return ticker.rsplit(".", 1)[0]
    return ticker


def batch_list(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def fetch_batch_raw_news(client, tickers, market, as_of_date, ticker_names=None):
    """Stage 1: Grounded search call using gemini-2.5-flash. Fetches raw news
    articles and web sources for a batch of tickers."""
    from google.genai import types
    from datetime import datetime, timedelta

    cutoff_date = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
    exchange = "NSE/BSE-listed" if market == "INDIA" else "US-listed"
    
    ticker_names = ticker_names or {}
    def _format_name(t):
        bare = _bare_ticker(t)
        company = ticker_names.get(t)
        return f"{company} ({bare})" if company and company != t else bare

    names = ", ".join(_format_name(t) for t in tickers)
    prompt = (
        f"You are a financial news researcher. For each of these {exchange} stocks: {names} -- "
        f"search for news, announcements, press releases, analyst notes, and stock moves between "
        f"{cutoff_date} and {as_of_date} (the last 36 hours). Report any news items you find for "
        "each ticker, specifying the exact date of each item. Be extremely concise. If a ticker has no news, skip it."
    )
    try:
        grounding_tool = types.Tool(google_search=types.GoogleSearch())
        config = types.GenerateContentConfig(tools=[grounding_tool])
        resp = client.models.generate_content(model=SEARCH_MODEL, contents=prompt, config=config)
        text = resp.text or ""
        sources = []
        gm = resp.candidates[0].grounding_metadata if resp.candidates else None
        if gm and gm.grounding_chunks:
            for chunk in gm.grounding_chunks:
                web = getattr(chunk, "web", None)
                if web and web.uri:
                    sources.append({"title": web.title, "url": web.uri})
        return text, sources
    except Exception as e:
        return None, str(e)


def filter_batch_with_reasoning(client, raw_text, tickers, market, as_of_date, ticker_names=None):
    """Stage 2: Strict reasoning & condition filtering using gemini-3.6-flash.
    Evaluates raw web notes against recency and material catalyst rules."""
    if not raw_text:
        return ""
    from datetime import datetime, timedelta

    cutoff_date = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
    
    ticker_names = ticker_names or {}
    def _format_name(t):
        bare = _bare_ticker(t)
        company = ticker_names.get(t)
        return f"{company} ({bare})" if company and company != t else bare
        
    names = ", ".join(_format_name(t) for t in tickers)

    prompt = (
        f"You are a senior financial analyst. Below is raw news text gathered for these tickers: {names}.\n\n"
        f"STRICT RECENCY RULE: Evaluate each news item. Keep ONLY items from the last 36 hours. "
        f"Drop anything older or undated.\n\n"
        "STRICT MATERIALITY RULE:\n"
        "- KEEP ONLY: earnings released in the last 2 days, M&A/acquisitions, FDA/regulatory approvals, "
        "important board announcements (EXCLUDING dividend and generic day-to-day announcements), analyst coverage and important stock targets, "
        "major contract wins/losses, big institutional and promoter activity, and significant stock movements (+-3%).\n"
        "- DROP ENTIRELY: routine scheduled board meetings/AGMs/EGMs with no outcome yet, ordinary "
        "insider option exercises, routine block trades, minor price fluctuations, dividend announcements, and generic no-news filler.\n\n"
        "For items that pass both rules, write short, clear bullet points under bold ticker headers. "
        "If a ticker has no qualifying items, omit it completely. DO NOT write 'no significant news', just skip the ticker entirely.\n\n"
        f"RAW TEXT:\n{raw_text}"
    )
    try:
        config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=4096)
        )
        resp = client.models.generate_content(model=REASONING_MODEL, contents=prompt, config=config)
        return resp.text or raw_text
    except Exception:
        return raw_text


def collate_market_summary(client, market, batch_texts, as_of_date=None):
    """Stage 3: Final market collation using gemini-3.6-flash. Combines filtered
    batch summaries into a short, scannable daily brief (Perplexity Finance style)."""
    if not batch_texts or not any(t.strip() for t in batch_texts):
        return "No major news for this watchlist's tickers in the last 24-48 hours."
    from datetime import datetime, timedelta

    if as_of_date:
        cutoff_date = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
        window_line = f"ONLY items from the last 36 hours qualify -- "
    else:
        window_line = "ONLY items from the last 36 hours qualify -- "

    combined = "\n\n---\n\n".join(t for t in batch_texts if t.strip())
    if not combined.strip():
        return "No major news for this watchlist's tickers in the last 36 hours."

    prompt = (
        f"Below are filtered news notes gathered for the {MARKET_LABELS.get(market, market)}.\n\n"
        "Act as an editor for a daily investor briefing (like Perplexity Finance's watchlist digest). "
        "Produce an EXTREMELY CRISP summary (under 500 words, max 1 pager) containing ONLY material, recent items.\n\n"
        f"{window_line}drop any note if it's older than 36 hours.\n\n"
        "Format as a flat list of short bullet points (one line each: **Company Name (Ticker)** - takeaway). "
        "Use the EXACT company name and ticker provided in the notes. Do not hallucinate names. "
        "CRITICAL: If a ticker does not have significant news, SKIP IT ENTIRELY. Do NOT write 'no significant news' or mention it. "
        "If NOTHING in the whole watchlist clears this bar, output EXACTLY ONE SENTENCE: 'No major news for this watchlist's tickers in the last 36 hours.'\n\n"
        f"NOTES:\n{combined}"
    )
    try:
        config = types.GenerateContentConfig(
            # Using 1024 thinking budget per user request: gives the model just enough
            # reasoning room to execute the formatting rules without hallucinating filler.
            thinking_config=types.ThinkingConfig(thinking_budget=1024)
        )
        resp = client.models.generate_content(model=REASONING_MODEL, contents=prompt, config=config)
        return resp.text or combined
    except Exception:
        return combined


def build_news_summary(watchlists, api_key, batch_size=BATCH_SIZE):
    """Runs the 3-stage pipeline for both markets. Returns a dict:
    {"as_of": ISO date, "generated_at": ISO datetime, "markets": {"US": {...}, "INDIA": {...}}}"""
    from google import genai

    client = genai.Client(api_key=api_key)
    as_of_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = {
        "as_of": as_of_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "markets": {},
    }

    first_call = True
    
    from stock_data import load_data_snapshot
    snapshot = load_data_snapshot()
    ticker_names = {}
    if snapshot and "markets" in snapshot:
        for mkt_data in snapshot["markets"].values():
            for row in mkt_data:
                if "company_name" in row:
                    ticker_names[row["ticker"]] = row["company_name"]

    for market in ("US", "INDIA"):
        tickers = watchlists.get(market, [])
        if not tickers:
            result["markets"][market] = {
                "summary": "No tickers in this watchlist.", "sources": [], "batch_count": 0, "ticker_count": 0,
            }
            continue

        batches = batch_list(tickers, batch_size)
        filtered_batch_texts = []
        all_sources = []

        for batch in batches:
            if not first_call:
                time.sleep(SECONDS_BETWEEN_CALLS)
            first_call = False

            # Stage 1: Grounded web search with gemini-2.5-flash
            raw_text, sources_or_err = fetch_batch_raw_news(client, batch, market, as_of_date, ticker_names=ticker_names)
            if raw_text:
                all_sources.extend(sources_or_err)

                # Stage 2: Reasoning & strict filtering with gemini-3.6-flash
                clean_text = filter_batch_with_reasoning(client, raw_text, batch, market, as_of_date, ticker_names=ticker_names)
                if clean_text:
                    filtered_batch_texts.append(clean_text)
            else:
                print(f"  Batch {batch} search failed: {sources_or_err}")

        # Stage 3: Final market collation with gemini-3.6-flash
        collated = collate_market_summary(client, market, filtered_batch_texts, as_of_date=as_of_date)

        result["markets"][market] = {
            "summary": collated,
            "sources": all_sources,
            "batch_count": len(batches),
            "ticker_count": len(tickers),
        }

    return result


def build_discord_messages(news_data, limit=1900):
    """Turns a news_summary.json-shaped dict into a list of Discord-ready message strings."""
    messages = []
    as_of = news_data.get("as_of", "")
    for market in ("US", "INDIA"):
        entry = news_data.get("markets", {}).get(market)
        if not entry:
            continue
        label = MARKET_LABELS.get(market, market)
        title = f"**📰 {label} News — {as_of}**"
        summary = entry.get("summary", "").strip()
        if not summary:
            continue

        parts = summary.split("\n\n")
        chunks, current, part_num = [], "", 1

        def flush(text, part_num, is_first):
            head = title if is_first else f"**{label} News (cont'd, part {part_num})**"
            return f"{head}\n{text}"

        first = True
        for p in parts:
            candidate = (current + "\n\n" + p) if current else p
            if len(flush(candidate, part_num, first)) > limit and current:
                chunks.append(flush(current, part_num, first))
                first = False
                part_num += 1
                current = p
            else:
                current = candidate
        if current:
            chunks.append(flush(current, part_num, first))

        messages.extend(chunks)
    return messages


def load_news_summary():
    if not os.path.exists(NEWS_SUMMARY_FILE):
        return None
    try:
        with open(NEWS_SUMMARY_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def save_news_summary(data):
    with open(NEWS_SUMMARY_FILE, "w") as f:
        json.dump(data, f, indent=2)
