"""
LLM-generated news/announcements summary for the watchlist -- a
Perplexity-Finance-style digest, built via Gemini's Google Search grounding
(the model searches the web itself and cites sources; see
https://ai.google.dev/gemini-api/docs/google-search).

Model choice (gemini-2.5-flash) is NOT arbitrary -- it was picked after
live-testing against this project's actual free-tier quota (checked via the
AI Studio Rate Limit dashboard, which differs a lot from Google's generic
published numbers):
  - The entire Gemini 3.x family (3, 3.1, 3.5, 3.6, including their "Flash
    Lite" variants) has ZERO free Search-grounding quota on this account --
    confirmed by live 429 RESOURCE_EXHAUSTED errors on every 3.x model
    tested, matching the dashboard's "Gemini 3 -> Search grounding: 0/0".
  - Gemini 2.5 Flash's grounding calls DO work (tested live, successfully
    returned cited, dated news), but the account's overall RPD (requests
    per day) cap for that model is just 20/day -- much tighter than the
    ~1,500/day figure Google's public docs advertise generically.

Because of that 20/day ceiling, tickers are batched at BATCH_SIZE (13, not
5) to keep total daily calls comfortably under the cap: 26 US tickers -> 2
batches, 47 India tickers -> 4 batches, plus one collation call per market
= 8 calls/day total, vs a 20/day hard limit. If BATCH_SIZE is lowered back
toward 5, recompute total calls (ceil(n_tickers/batch) per market + 1 per
market) against whatever the live dashboard currently shows -- these quotas
are account-specific and change, so don't assume the numbers in this
docstring still hold without checking https://aistudio.google.com (Rate
Limit page) again first.
"""

import json
import os
import time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NEWS_SUMMARY_FILE = os.path.join(SCRIPT_DIR, "news_summary.json")

GEMINI_MODEL = "gemini-2.5-flash"
BATCH_SIZE = 13
# 5 RPM on the free tier for gemini-2.5-flash (see module docstring) -- 15s
# between calls keeps us at 4/min, a safe margin under that cap.
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


def _bare_ticker(ticker):
    """Strips the .NS/.BO exchange suffix so the search prompt reads
    naturally (e.g. "RELIANCE" instead of "RELIANCE.NS")."""
    if ticker.endswith(".NS") or ticker.endswith(".BO"):
        return ticker.rsplit(".", 1)[0]
    return ticker


def batch_list(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def summarize_batch(client, tickers, market, as_of_date):
    """One grounded Gemini call covering a batch of tickers. Returns
    (text, source_urls) on success, or (None, error_message) on failure --
    callers should skip a failed batch (not fail the whole run) since a
    single batch failing (e.g. transient rate limit) shouldn't lose the
    rest of the day's summary."""
    from google.genai import types

    exchange = "NSE/BSE-listed" if market == "INDIA" else "US-listed"
    names = ", ".join(_bare_ticker(t) for t in tickers)
    prompt = (
        f"You are a financial news analyst. For each of these {exchange} stocks: {names} -- "
        f"search for and report ONLY IMPORTANT, MAJOR news from the last 24 hours as of "
        f"{as_of_date}: major announcements, earnings/results, regulatory or FDA/approval news, "
        "M&A, management changes, and major stock price moves (with a stated reason). This is a "
        "daily digest, so ignore routine/minor news and small price moves -- only include "
        "something if it's genuinely significant. Skip a ticker entirely if there's nothing "
        "important in the last 24 hours -- don't pad with generic/no-news filler. Organize by "
        "ticker with a bold ticker heading. Be concise -- a few bullet points per ticker, not "
        "paragraphs."
    )
    try:
        grounding_tool = types.Tool(google_search=types.GoogleSearch())
        config = types.GenerateContentConfig(tools=[grounding_tool])
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=config)
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


def collate_market_summary(client, market, batch_texts):
    """Synthesizes the batch summaries for one market into a single,
    de-duplicated, well-organized digest. No grounding needed here -- it's
    purely reformatting/merging text Gemini already produced (with real
    search results) in the batch calls, so this call doesn't need to search
    again."""
    if not batch_texts:
        return "No major news for this watchlist's tickers in the last 24 hours."
    combined = "\n\n---\n\n".join(batch_texts)
    prompt = (
        f"Below are separately-generated news summaries for different batches of tickers in "
        f"the same {MARKET_LABELS.get(market, market)}. Merge them into ONE single, unified "
        "daily digest covering the important announcements, developments, and major stock "
        "moves across all batches: keep the per-ticker bold headings, remove any "
        "duplicate/redundant lines, fix any inconsistent formatting, and order tickers "
        "alphabetically. Don't add commentary of your own or invent anything not already "
        "present in the text below.\n\n"
        f"{combined}"
    )
    try:
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return resp.text or combined
    except Exception:
        # Collation failing is not fatal -- fall back to the raw concatenated
        # batch texts so a transient error doesn't lose the whole day's work.
        return combined


def build_news_summary(watchlists, api_key, batch_size=BATCH_SIZE):
    """Runs the full pipeline for both markets. Returns a dict:
    {"as_of": ISO date, "generated_at": ISO datetime, "markets": {"US": {...}, "INDIA": {...}}}
    Each market entry: {"summary": text, "sources": [...], "batch_count": n, "ticker_count": n}.
    Paces calls SECONDS_BETWEEN_CALLS apart to respect the free-tier RPM cap
    -- this function is meant to be run headless (GitHub Actions), so a few
    minutes of runtime is fine."""
    from google import genai

    client = genai.Client(api_key=api_key)
    as_of_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = {
        "as_of": as_of_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "markets": {},
    }

    first_call = True
    for market in ("US", "INDIA"):
        tickers = watchlists.get(market, [])
        if not tickers:
            result["markets"][market] = {
                "summary": "No tickers in this watchlist.", "sources": [], "batch_count": 0, "ticker_count": 0,
            }
            continue

        batches = batch_list(tickers, batch_size)
        batch_texts = []
        all_sources = []
        for batch in batches:
            if not first_call:
                time.sleep(SECONDS_BETWEEN_CALLS)
            first_call = False
            text, sources_or_err = summarize_batch(client, batch, market, as_of_date)
            if text is not None:
                batch_texts.append(text)
                all_sources.extend(sources_or_err)
            else:
                print(f"  Batch {batch} failed: {sources_or_err}")

        if not first_call:
            time.sleep(SECONDS_BETWEEN_CALLS)
        collated = collate_market_summary(client, market, batch_texts)

        result["markets"][market] = {
            "summary": collated,
            "sources": all_sources,
            "batch_count": len(batches),
            "ticker_count": len(tickers),
        }

    return result


def build_discord_messages(news_data, limit=1900):
    """Turns a news_summary.json-shaped dict into a list of Discord-ready
    message strings, one market at a time, splitting a market's summary
    across multiple messages if it exceeds Discord's ~2000-char limit
    (mirrors alerts.build_discord_messages_for_rule's chunking approach,
    but splits on lines/paragraphs instead of table rows since this is
    prose, not a table)."""
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

        # Split into paragraph-ish chunks (blank-line separated) so we never
        # cut a bullet point/ticker section in half if it can be avoided.
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
