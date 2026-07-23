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
    from datetime import datetime, timedelta

    cutoff_date = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
    exchange = "NSE/BSE-listed" if market == "INDIA" else "US-listed"
    names = ", ".join(_bare_ticker(t) for t in tickers)
    prompt = (
        f"You are a financial news analyst. For each of these {exchange} stocks: {names} -- "
        f"search for and report ONLY IMPORTANT, MAJOR news, and ONLY items dated between "
        f"{cutoff_date} and {as_of_date} (the last 24-48 hours) -- major announcements, "
        "earnings/results, regulatory or FDA/approval news, M&A, management changes, and major "
        "stock price moves (with a stated reason). STRICT recency rule: every item must be about "
        f"something that was announced, published, or happened within that {cutoff_date} to "
        f"{as_of_date} window -- not older news you find while searching, and not a company's "
        "full-year or quarterly financial figures unless those figures were freshly reported/"
        "announced within that window. State the specific date of each item. If you can't "
        "confirm an item's date falls in that window, leave it out. This is a daily digest, so "
        "also ignore routine/minor news and small price moves -- only include something if it's "
        "genuinely significant AND recent. Skip a ticker entirely if there's nothing that "
        "qualifies -- don't pad with generic/no-news filler. Organize by ticker with a bold "
        "ticker heading. Be concise -- a few bullet points per ticker, not paragraphs."
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


def collate_market_summary(client, market, batch_texts, as_of_date=None):
    """The batch step (summarize_batch) casts a wide net -- it's a search
    call, not an editor, so it tends to include borderline/routine items
    (a scheduled board meeting, an ordinary block trade, a small price
    tick) alongside genuinely important ones, and occasionally an item
    that isn't actually recent (e.g. a company's FY results surfaced by
    search even though they weren't freshly announced). This step is
    where the actual filtering happens: it's a strict editorial pass,
    Perplexity-Finance-style, that keeps ONLY material AND recent items
    and writes them as a short, scannable brief -- not an exhaustive
    per-ticker report. No grounding needed -- it only edits text Gemini
    already produced (with real search results) in the batch calls, it
    doesn't search again."""
    if not batch_texts:
        return "No major news for this watchlist's tickers in the last 24-48 hours."
    from datetime import datetime, timedelta
    if as_of_date:
        cutoff_date = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
        window_line = f"ONLY items dated between {cutoff_date} and {as_of_date} (the last 24-48 hours) qualify -- "
    else:
        window_line = "ONLY items from the last 24-48 hours qualify -- "
    combined = "\n\n---\n\n".join(batch_texts)
    prompt = (
        f"Below are raw notes gathered for tickers in the {MARKET_LABELS.get(market, market)} "
        "-- some entries are genuinely important and recent, many are routine noise or stale "
        "info that shouldn't be in a daily briefing. Act as an editor for a daily investor "
        "briefing (think Perplexity Finance's watchlist digest): produce a SHORT, CRISP summary "
        "containing ONLY material, recent items.\n\n"
        "KEEP: major earnings beats/misses with concrete numbers, M&A, regulatory/FDA/approval "
        "outcomes, large stock price moves (roughly >=5%) WITH a clear stated reason, analyst "
        "rating or price-target changes from named firms, leadership changes, major contract "
        "wins/losses, credit rating changes, and anything else genuinely market-moving -- "
        f"{window_line}if a note doesn't state a date, or its date is older than that window, "
        "drop it even if it sounds important.\n\n"
        "DROP entirely: routine scheduled board meetings/investor calls/AGMs/EGMs where no "
        "outcome is known yet, ordinary block/bulk trades and insider option exercises unless "
        "unusually large, small price moves without a clear catalyst, generic 'no major news' "
        "filler, stale/older items outside the recency window above, and anything duplicated "
        "across entries.\n\n"
        "Format as a flat list of short bullet points (one line each: **Ticker** -- takeaway), "
        "not per-ticker sections or headers -- most tickers won't have anything worth including, "
        "and that's fine. If NOTHING in the whole watchlist clears this bar, say so in one "
        "sentence instead of listing routine items. Don't invent anything not already present "
        "in the notes below.\n\n"
        f"{combined}"
    )
    try:
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return resp.text or combined
    except Exception:
        # Filtering failing is not fatal -- fall back to the raw concatenated
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
        collated = collate_market_summary(client, market, batch_texts, as_of_date=as_of_date)

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
