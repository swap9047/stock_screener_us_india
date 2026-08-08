import json
import os
import concurrent.futures
from datetime import datetime, timedelta, timezone
from google.genai import types
import yfinance as yf

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FUNDAMENTALS_FILE = os.path.join(SCRIPT_DIR, "fundamentals.json")

# India reports quarterly results with a longer lag than the US (~30-45 days
# post quarter-end vs ~2-3 weeks), so a US-calibrated window makes recent
# Indian earnings look "not found" and pushes the guard toward Unknown less
# often than it should for stale India cases. Widen the window per market.
SEARCH_WINDOW_DAYS = {"INDIA": 45, "US": 25}

def _generate_with_timeout(client, model, contents, config, timeout=120):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(client.models.generate_content, model=model, contents=contents, config=config)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise TimeoutError(f"API call to {model} timed out after {timeout}s")
    finally:
        executor.shutdown(wait=False)

def fetch_fundamental_news(client, ticker, market, company_name, is_retry=False):
    from stock_data import get_exchange_label

    as_of_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    window = SEARCH_WINDOW_DAYS.get(market, SEARCH_WINDOW_DAYS["US"])
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=window)).strftime("%Y-%m-%d")
    exchange = get_exchange_label(market)
    
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

SENTIMENT_STALE_DAYS = 4

def _is_valid_view(view):
    if not view:
        return False
    sentiment = view.get("sentiment")
    # "Unknown" is a legitimate terminal state (e.g. no news found) and must
    # NOT be treated as a failed generation, or the refresh loop would keep
    # the previous (possibly hallucinated) verdict forever.
    if sentiment not in ("Positive", "Neutral", "Negative", "Unknown"):
        return False
    if "error" in str(view).lower() or "pending" in str(view).lower():
        return False
    return True

def _view_age_days(view):
    """Age of a view in days, or None if as_of is missing/unparseable."""
    as_of = (view or {}).get("as_of")
    if not as_of:
        return None
    try:
        # Timestamps are stored as "YYYY-MM-DD HH:MM" (UTC). Normalize the
        # space to 'T' so fromisoformat works on Python < 3.11 too.
        ts = datetime.fromisoformat(as_of.replace(" ", "T", 1))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400
    except Exception:
        return None

def _field_has_data(field_val):
    """True if a schema text field contains real data (not N/A-like)."""
    val = (field_val or "").strip()
    if not val:
        return False
    low = val.lower()
    if low in ("n/a", "none", "nil", "na"):
        return False
    return not any(m in low for m in ("n/a", "not available", "no news", "no data"))

def _field_is_placeholder(field_val):
    """True if the field, AS A WHOLE, is just an N/A-style placeholder — not
    merely containing an N/A fragment for one sub-value amid real data (e.g.
    "Revenue Rs 582 cr, EPS N/A" is NOT a placeholder; `_field_has_data`'s
    substring check would wrongly say it is, since "n/a" appears inside it).
    Case/whitespace-normalized so "n/a", "N/A ", "Not Available" etc. all
    count consistently, unlike the old exact `!= "N/A"` check."""
    v = (field_val or "").strip().lower()
    return v in ("", "n/a", "na", "none", "nil", "not available", "no data", "no news")

def _structured_field_is_set(val):
    """True if a structured evidence field (eps_value/guidance_change/
    analyst_action) holds a real, non-null/none/N-A value. Unlike
    `_field_has_data`, this does not look for substrings inside free text —
    the model must commit to an explicit value or an explicit null/"none"."""
    val = (val or "").strip().lower()
    return bool(val) and val not in ("none", "n/a", "na", "null")

def _has_hard_evidence(view):
    """True if the view contains current-quarter EPS, explicit guidance, or an
    analyst action. Revenue/revenue-growth figures alone do NOT count.

    Prefers the structured fields (eps_value/guidance_change/analyst_action)
    the model must now explicitly commit to, since free-text substring checks
    (the old approach) let vague mentions like "EPS: N/A (pending)" pass as
    evidence just because the word "eps" appeared. Falls back to the legacy
    free-text check only for cached views generated before this schema.
    """
    if any(k in view for k in ("eps_value", "guidance_change", "analyst_action")):
        return (
            _structured_field_is_set(view.get("eps_value"))
            or _structured_field_is_set(view.get("guidance_change"))
            or _structured_field_is_set(view.get("analyst_action"))
        )
    if _field_has_data(view.get("future_guidance")) or _field_has_data(view.get("analyst_coverage")):
        return True
    earnings = (view.get("earnings_summary") or "").lower()
    if earnings and "eps" in earnings and "eps n/a" not in earnings and "eps not" not in earnings:
        return True
    return False

def _fetch_last_reported_earnings_date(ticker):
    """Best-effort, no-guessing lookup of the most recent CONFIRMED (already
    reported, not estimated) earnings date via yfinance.

    Returns a date, or None if unavailable/unverifiable. Callers must fail
    OPEN on None — small/mid-cap coverage (especially India) is patchy, and
    treating "yfinance has no data" the same as "no earnings reported" would
    wrongly zero out legitimate results just because of a data-source gap.

    Timeout-guarded (unlike a bare call) because this runs once per ticker in
    the sequential GitHub Actions refresh loop, and GitHub-hosted runners
    share IP ranges Yahoo Finance sometimes rate-limits/slows -- a hang here
    with no timeout would stall the whole batch job.
    """
    try:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(lambda: yf.Ticker(ticker).get_earnings_dates(limit=8))
        try:
            df = future.result(timeout=15)
        except concurrent.futures.TimeoutError:
            df = None
        finally:
            executor.shutdown(wait=False)
        if df is None or df.empty:
            return None
        today = datetime.now(timezone.utc).date()
        has_reported_col = "Reported EPS" in df.columns
        for idx, row in df.sort_index(ascending=False).iterrows():
            d = idx.date() if hasattr(idx, "date") else idx
            if d > today:
                continue
            if has_reported_col:
                val = row.get("Reported EPS")
                if val is None or val != val:  # NaN check without a pandas import
                    continue
            return d
        return None
    except Exception:
        return None

def _check_quarter_freshness(model_earnings_date, real_last_earnings_date, market):
    """Pure comparison (no I/O) — True if the model's claimed earnings date is
    consistent with the actual last-reported date, or if there isn't enough
    signal to contradict the model (fail open).

    Only returns False when a CONFIRMED earnings report exists inside the
    search window (so the model should have found and cited it) and the
    model either didn't cite a date or cited one that doesn't match.
    """
    if real_last_earnings_date is None:
        return True
    window = SEARCH_WINDOW_DAYS.get(market, SEARCH_WINDOW_DAYS["US"])
    today = datetime.now(timezone.utc).date()
    if (today - real_last_earnings_date).days > window:
        return True  # nothing new expected within the search window either way
    if not model_earnings_date:
        return False
    try:
        model_date = datetime.strptime(model_earnings_date, "%Y-%m-%d").date()
    except Exception:
        return False
    return abs((model_date - real_last_earnings_date).days) <= 10

def _validate_sentiment(view):
    """Deterministic post-hoc guard (independent of prompt compliance).

    Returns (sentiment, flag) where flag is:
      ""             -> keep the view as-is
      "STALE"        -> as_of older than SENTIMENT_STALE_DAYS
      "STALE_QUARTER"-> a confirmed earnings report exists that the model's
                        news/reasoning didn't verifiably account for
      "NO_DATA"      -> all three data fields missing/"N/A"
      "PARTIAL"      -> only revenue/soft data; no EPS/guidance/analyst
                        evidence, so a directional verdict is capped at
                        "Neutral"
    """
    if not view:
        return "Unknown", "NO_DATA"
    age = _view_age_days(view)
    if age is not None and age > SENTIMENT_STALE_DAYS:
        return "Unknown", "STALE"
    if view.get("quarter_verified") is False:
        return "Unknown", "STALE_QUARTER"
    fields = ["earnings_summary", "future_guidance", "analyst_coverage"]
    if not any(not _field_is_placeholder(view.get(f)) for f in fields):
        return "Unknown", "NO_DATA"
    sentiment = view.get("sentiment", "Unknown")
    if sentiment in ("Positive", "Negative") and not _has_hard_evidence(view):
        return "Neutral", "PARTIAL"
    return sentiment, ""

def generate_fundamental_view(client, row_data, news_text=None, news_source=None, is_retry=False):
    ticker = row_data.get("ticker", "UNKNOWN")
    market = row_data.get("market", "US")
    company_name = row_data.get("company_name", ticker)

    if news_text is None:
        news_text, news_source = fetch_fundamental_news(client, ticker, market, company_name, is_retry=is_retry)

    window = SEARCH_WINDOW_DAYS.get(market, SEARCH_WINDOW_DAYS["US"])
    prompt = f"""You are a fundamental equities analyst. Review the provided news facts for {company_name} (Ticker: {ticker}) and extract the current quarter's Earnings, Guidance, and Analyst Coverage. Evaluate the overall fundamental sentiment and provide your reasoning.

======================================================================
RECENT FUNDAMENTAL NEWS & ANNOUNCEMENTS
======================================================================
{news_text}
======================================================================

CRITICAL RULES (these override everything else):
1. If the news text says "No recent fundamental news found" or is empty, set "sentiment" to "Unknown" and all other fields (including the structured ones below) to "N/A"/null. Never guess or hallucinate a sentiment based on the company's past history, sector trends, or general knowledge.
2. If the current quarter's EPS AND explicit forward guidance AND analyst upgrade/downgrade are ALL missing from the news facts, the strongest allowed "sentiment" is "Neutral" — revenue/revenue-growth figures alone cannot support "Positive" or "Negative".
3. A directional verdict ("Positive"/"Negative") REQUIRES at least one of the structured fields below (eps_value, guidance_change, analyst_action) to be a real, specific value — not a vague or partial mention. Do not infer sentiment from company reputation, sector trends, or past performance — only from the specific structured facts found in the news above for the current quarter.
4. "earnings_report_date" must be the exact date (YYYY-MM-DD) of the earnings release found in the news, or null if none was found. If the most recent quarter's earnings fall outside the {window}-day search window, set "earnings_report_date" to null and "sentiment" to "Unknown".
5. MANDATORY: whenever "earnings_summary", "eps_value", or "analyst_coverage" contains real (non-N/A) data drawn from an actual earnings release, "earnings_report_date" MUST be filled in with that release's exact date — never leave it null while citing real earnings/EPS/analyst-coverage figures. This field is validated independently of the others; leaving it null when real data exists will cause the whole result to be discarded even if everything else is correct.

Return ONLY a valid JSON object matching this schema:
{{
  "earnings_summary": "Q2 EPS beat by $0.05, Revenue $1.2B (+10% YoY) (or 'N/A if no news')",
  "future_guidance": "Maintained full-year revenue guidance; raised EPS guidance (or 'N/A if no news')",
  "analyst_coverage": "Upgraded by Morgan Stanley to Overweight (PT $150) (or 'N/A if no news')",
  "earnings_report_date": "2026-07-17" or null,
  "eps_value": "$2.02 actual vs $1.89 est." or null,
  "guidance_change": "raised" | "lowered" | "maintained" | null,
  "analyst_action": "upgrade" | "downgrade" | null,
  "sentiment": "Positive" | "Neutral" | "Negative" | "Unknown",
  "reasoning": "Explain why you chose this sentiment based on the facts."
}}"""

    def _pending_fallback(reason, used_model="Error"):
        return {
            "earnings_summary": "N/A",
            "future_guidance": "N/A",
            "analyst_coverage": "N/A",
            "earnings_report_date": None,
            "eps_value": None,
            "guidance_change": None,
            "analyst_action": None,
            "sentiment": "Unknown",
            "reasoning": f"Analysis pending -- {reason}",
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "news_source": news_source or "⚪ Unknown",
            "model_used": used_model,
        }

    def _finalize(data, model_used):
        data["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        data["news_used"] = news_text
        data["news_source"] = news_source or "⚪ Unknown"
        data["model_used"] = model_used
        real_date = _fetch_last_reported_earnings_date(ticker)
        data["quarter_verified"] = _check_quarter_freshness(data.get("earnings_report_date"), real_date, market)
        data["real_earnings_date"] = real_date.isoformat() if real_date else None
        return data

    from stock_data import load_settings
    settings = load_settings()
    model = settings.get("sentiment_reasoning_model", "models/gemini-3.5-flash-lite")
    budget = settings.get("sentiment_thinking_budget", 8192)

    # 1. Primary Reasoning Model (configurable, defaults to Gemini 3.5 Flash Lite with thinking)
    try:
        config_kwargs = {"response_mime_type": "application/json"}
        if "gemma" not in model:
            if isinstance(budget, str):
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=budget)
            else:
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)

        config = types.GenerateContentConfig(**config_kwargs)
        resp = _generate_with_timeout(client, model, prompt, config, timeout=120)
        data = json.loads(resp.text)
        return _finalize(data, model.split("/")[-1])
    except Exception as e:
        print(f"  [{model} reasoning failed] {ticker}: {e} -> Falling back to 31b")

    # 2. Final Fallback (Gemma 4 31B)
    try:
        config = types.GenerateContentConfig(response_mime_type="application/json")
        resp = _generate_with_timeout(client, "models/gemma-4-31b-it", prompt, config, timeout=120)
        data = json.loads(resp.text)
        return _finalize(data, "gemma-4-31b-it (Fallback)")
    except Exception as e2:
        print(f"  [31b reasoning failed] {ticker}: {e2} -> Giving up")
        return _pending_fallback(str(e2))


def analyze_single_ticker_sentiment(ticker, row_data, api_key):
    """Regenerate one ticker's Sentiment view and persist it.

    The single-ticker counterpart to refresh_fundamentals.py's batch loop, so
    the UI's re-analyze buttons can refresh Sentiment alongside Expert Take
    instead of leaving it to the nightly job. Mirrors
    expert_views.analyze_single_ticker.

    A failed generation is discarded rather than written: generate_fundamental_view
    falls back to a "pending" stub on error, and persisting that would throw away
    a perfectly good existing view. Returns the stored view, or None if the call
    failed and the previous view was kept.
    """
    from google import genai

    client = genai.Client(api_key=api_key)
    view = generate_fundamental_view(client, row_data)
    if not _is_valid_view(view):
        return None
    all_views = load_fundamentals()
    all_views[ticker] = view
    save_fundamentals(all_views)
    return view
