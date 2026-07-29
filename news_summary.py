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
from google.genai import types

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NEWS_SUMMARY_FILE = os.path.join(SCRIPT_DIR, "news_summary.json")

SEARCH_MODEL = "models/gemma-4-31b-it"
REASONING_MODEL = "gemini-3.5-flash-lite"
BATCH_SIZE = 5
# 2s delay between search calls as requested by user
SECONDS_BETWEEN_CALLS = 2

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


def fetch_single_raw_news(client, ticker, market, as_of_date, ticker_names=None, model=SEARCH_MODEL):
    """Stage 1: Grounded search call using gemma-4-31b-it. Fetches raw news
    articles and web sources for a single ticker."""
    from datetime import datetime, timedelta
    from news_search import get_stock_news

    cutoff_date = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
    exchange = "NSE/BSE-listed" if market == "INDIA" else "US-listed"
    
    ticker_names = ticker_names or {}
    bare = _bare_ticker(ticker)
    company = ticker_names.get(ticker)
    name = f"{company} ({bare})" if company and company != ticker else bare

    prompt = (
        f"You are a financial news researcher. For the {exchange} stock {name} -- "
        f"search for news, announcements, press releases, analyst notes, and stock moves between "
        f"{cutoff_date} and {as_of_date} (the last 36 hours), AND any major upcoming scheduled events in the next 3-4 days (e.g. earnings, launches). Report any news items you find, "
        "specifying the exact date of each item. Be extremely concise. If there is no news, output nothing."
    )
    try:
        grounding_tool = types.Tool(google_search=types.GoogleSearch())
        config = types.GenerateContentConfig(tools=[grounding_tool])
        resp = client.models.generate_content(model=model, contents=prompt, config=config)
        text = resp.text or ""
        sources = []
        gm = resp.candidates[0].grounding_metadata if resp.candidates else None
        if gm and gm.grounding_chunks:
            for chunk in gm.grounding_chunks:
                web = getattr(chunk, "web", None)
                if web and web.uri:
                    sources.append({"title": web.title, "url": web.uri})
        return f"**{name}**:\n{text}", sources
    except Exception as e:
        print(f"  [gemma search failed] {ticker}: {e} -> Falling back to dense 31b model")
        try:
            resp = client.models.generate_content(model="models/gemma-4-31b-it", contents=prompt, config=config)
            text = resp.text or ""
            sources = []
            gm = resp.candidates[0].grounding_metadata if resp.candidates else None
            if gm and gm.grounding_chunks:
                for chunk in gm.grounding_chunks:
                    web = getattr(chunk, "web", None)
                    if web and web.uri:
                        sources.append({"title": web.title, "url": web.uri})
            return f"**{name}**:\n{text}", sources
        except Exception as e2:
            print(f"  [gemma 31b fallback failed] {ticker}: {e2} -> Falling back to DuckDuckGo/yfinance")
            fallback_text, fallback_source = get_stock_news(ticker, market=market)
            source_dict = [{"title": fallback_source, "url": ""}]
            return f"**{name}**:\n{fallback_text}", source_dict


def filter_batch_with_reasoning(client, raw_text, tickers, market, as_of_date, ticker_names=None, model=REASONING_MODEL, budget=4096):
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
        f"STRICT RECENCY RULE: Evaluate each news item. Keep ONLY items from the last 36 hours, OR major upcoming scheduled events in the next 3-4 days. "
        f"Drop anything older or undated.\n\n"
        "STRICT MATERIALITY RULE:\n"
        "- KEEP ONLY: earnings released in the last 2 days, upcoming earnings/events in the next 3-4 days, M&A/acquisitions, FDA/regulatory approvals, "
        "important board announcements (EXCLUDING dividend and generic day-to-day announcements), analyst coverage and important stock targets, "
        "major contract wins/losses, big institutional and promoter activity, and significant stock movements (+-3%).\n"
        "- DROP ENTIRELY: routine scheduled board meetings/AGMs/EGMs with no outcome yet, ordinary "
        "insider option exercises, routine block trades, minor price fluctuations, dividend announcements, and generic no-news filler.\n\n"
        "For items that pass both rules, write short, clear bullet points under bold ticker headers. "
        "If a ticker has no qualifying items, omit it completely. DO NOT write 'no significant news', just skip the ticker entirely.\n\n"
        f"RAW TEXT:\n{raw_text}"
    )
    try:
        config_kwargs = {}
        if "gemma" not in model:
            if isinstance(budget, str):
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=budget)
            else:
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
        config = types.GenerateContentConfig(**config_kwargs)
        resp = client.models.generate_content(model=model, contents=prompt, config=config)
        return resp.text or raw_text
    except Exception as e:
        print(f"  [gemini reasoning failed] {e} -> Falling back to gemma-4-26b-a4b-it")
        try:
            resp = client.models.generate_content(model="models/gemma-4-26b-a4b-it", contents=prompt)
            return resp.text or raw_text
        except Exception as e2:
            print(f"  [gemma reasoning fallback failed] {e2} -> Returning raw text")
            return raw_text


def collate_market_summary(client, market, batch_texts, as_of_date=None, model=REASONING_MODEL, budget=4096):
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
        config_kwargs = {}
        if "gemma" not in model:
            if isinstance(budget, str):
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=budget)
            else:
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
        config = types.GenerateContentConfig(**config_kwargs)
        resp = client.models.generate_content(model=model, contents=prompt, config=config)
        return resp.text or combined
    except Exception as e:
        print(f"  [gemini collate failed] {e} -> Falling back to gemma-4-26b-a4b-it")
        try:
            resp = client.models.generate_content(model="models/gemma-4-26b-a4b-it", contents=prompt)
            return resp.text or combined
        except Exception as e2:
            print(f"  [gemma collate fallback failed] {e2} -> Returning raw combined text")
            return combined


def build_news_summary(watchlists, api_key, batch_size=BATCH_SIZE):
    """Runs the 3-stage pipeline for both markets. Returns a dict:
    {"as_of": ISO date, "generated_at": ISO datetime, "markets": {"US": {...}, "INDIA": {...}}}"""
    from google import genai
    from stock_data import load_settings
    
    settings = load_settings()
    search_model = settings.get("news_search_model", SEARCH_MODEL)
    reasoning_model = settings.get("news_reasoning_model", REASONING_MODEL)
    if reasoning_model == "gemini-3.5-flash-lite":
        reasoning_model = "models/gemini-3.5-flash-lite"
        
    raw_budget = settings.get("news_reasoning_budget", 4096)
    if isinstance(raw_budget, str) and raw_budget.isdigit():
        thinking_budget = int(raw_budget)
    else:
        thinking_budget = raw_budget

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
    if snapshot and "per_market" in snapshot:
        for mkt_data in snapshot["per_market"].values():
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

        print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] === {market} market: {len(tickers)} tickers ===")

        # Process Stage 1 and Stage 2 per-ticker
        filtered_batch_texts = []
        all_sources = []
        for i, ticker in enumerate(tickers):
            if not first_call:
                time.sleep(SECONDS_BETWEEN_CALLS)
            first_call = False

            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts}] [{market}] [{i+1}/{len(tickers)}] {ticker} - Stage1 search starting...")

            # Stage 1: Grounded web search with chosen model
            t0 = time.time()
            raw_text, sources_or_err = fetch_single_raw_news(client, ticker, market, as_of_date, ticker_names=ticker_names, model=search_model)
            elapsed1 = time.time() - t0

            if raw_text:
                all_sources.extend(sources_or_err)
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"[{ts}] [{market}] [{i+1}/{len(tickers)}] {ticker} - Stage1 done ({elapsed1:.1f}s), Stage2 filter starting...")

                # Stage 2: Reasoning & strict filtering per-ticker with chosen model
                t0 = time.time()
                clean_text = filter_batch_with_reasoning(client, raw_text, [], market, as_of_date, ticker_names=ticker_names, model=reasoning_model, budget=thinking_budget)
                elapsed2 = time.time() - t0
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                if clean_text:
                    filtered_batch_texts.append(clean_text)
                    print(f"[{ts}] [{market}] [{i+1}/{len(tickers)}] {ticker} - done (search {elapsed1:.1f}s + filter {elapsed2:.1f}s = {elapsed1+elapsed2:.1f}s total)")
                else:
                    print(f"[{ts}] [{market}] [{i+1}/{len(tickers)}] {ticker} - filtered to empty ({elapsed1:.1f}s + {elapsed2:.1f}s)")
            else:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"[{ts}] [{market}] [{i+1}/{len(tickers)}] {ticker} - Stage1 FAILED ({elapsed1:.1f}s): {sources_or_err}")

        # Stage 3: Final market collation with chosen model
        print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] [{market}] Stage3 collation starting ({len(filtered_batch_texts)} filtered results)...")
        t0 = time.time()
        collated = collate_market_summary(client, market, filtered_batch_texts, as_of_date=as_of_date, model=reasoning_model, budget=thinking_budget)
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] [{market}] Stage3 done ({time.time()-t0:.1f}s)")

        result["markets"][market] = {
            "summary": collated,
            "sources": all_sources,
            "batch_count": len(tickers),
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
