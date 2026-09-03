import concurrent.futures
import json
import os
from datetime import datetime, timedelta, timezone
from google.genai import types

import llm_util
import yfinance as yf

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FUNDAMENTALS_FILE = os.path.join(SCRIPT_DIR, "fundamentals.json")

# India reports quarterly results with a longer lag than the US (~30-45 days
# post quarter-end vs ~2-3 weeks), so a US-calibrated window makes recent
# Indian earnings look "not found".
#
# Resolve this off the TICKER SUFFIX, not the market key. It used to be a dict
# keyed by markets.json keys with an unrecognized key falling back to the US
# window -- the same shape as the old get_exchange_label bug (see
# stock_data.py), and it failed the same way: watchlists are user-creatable
# from the dashboard, so every new one silently got a US window. That is not
# hypothetical -- "wrap_earnings_watchlist" was added with 11 .NS/.BO tickers
# and a ^CRSLDX benchmark, and every one of them was being searched with a
# 25-day window instead of 45.
INDIA_SEARCH_WINDOW_DAYS = 45
US_SEARCH_WINDOW_DAYS = 25


def search_window_days(market=None, ticker=None):
    """News-search lookback for a ticker, in days. `market` is accepted only
    so old single-argument call sites keep working; the suffix wins."""
    if ticker and (ticker.endswith(".NS") or ticker.endswith(".BO")):
        return INDIA_SEARCH_WINDOW_DAYS
    if ticker:
        return US_SEARCH_WINDOW_DAYS
    return INDIA_SEARCH_WINDOW_DAYS if market == "india_invested" else US_SEARCH_WINDOW_DAYS


# How far back a cited earnings date may sit and still belong to the SAME
# reporting period. Deliberately separate from the search window above: one is
# "how far back do we look for news", the other is "is this the current
# quarter". _check_quarter_freshness used to reuse the search window for both.
QUARTER_SPAN_DAYS = 92

# Shared with news_summary and expert_views -- see llm_util.
_generate_with_timeout = llm_util.generate_with_timeout


def _clean_json_text(text):
    """Strip markdown code blocks from model JSON output."""
    t = (text or "").strip()
    if t.startswith("```json"):
        t = t[7:]
    elif t.startswith("```"):
        t = t[3:]
    if t.endswith("```"):
        t = t[:-3]
    return t.strip()

def fetch_fundamental_news(client, ticker, market, company_name, is_retry=False):
    from stock_data import get_exchange_label

    as_of_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    window = search_window_days(market, ticker)
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=window)).strftime("%Y-%m-%d")
    exchange = get_exchange_label(market, ticker)
    
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

def _atomic_write_json(path, data):
    """Write JSON via temp file + os.replace.

    The plain truncate-and-write this replaces was called once PER TICKER by
    the refresh loops -- ~110 rewrites of a 150 KB file per run. A crash or a
    job timeout landing mid-dump left truncated JSON, and the loader's bare
    except then returned {} on the next run, so the whole store was silently
    rebuilt from empty with every prior view lost.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_fundamentals():
    if not os.path.exists(FUNDAMENTALS_FILE):
        return {}
    try:
        with open(FUNDAMENTALS_FILE) as f:
            return json.load(f)
    except Exception as e:
        # Loud: returning {} silently means the next save rebuilds the store
        # from empty and every prior view is gone.
        print(f"ERROR: could not read {FUNDAMENTALS_FILE}: {e} -- treating as empty!")
        return {}

def save_fundamentals(data):
    _atomic_write_json(FUNDAMENTALS_FILE, data)

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
    # Test the sentinel this module actually writes, not free text. This used
    # to be `"error" in str(view).lower() or "pending" in str(view).lower()` --
    # str(view) includes news_used, i.e. up to a kilobyte of raw search output,
    # so a legitimate view whose news mentioned "FDA approval pending" or
    # "margin of error" was classified as a failed generation and discarded.
    # Nothing tripped it in the current data, but the blast radius grew with
    # every headline the search stage returned.
    if view.get("model_used") == "Error":
        return False
    if str(view.get("reasoning", "")).startswith("Analysis pending -- "):
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

    The `concurrent.futures` import this needs was missing from the module for
    the life of the function, so every call raised NameError, the fail-open
    `except Exception` below turned that into None, and _check_quarter_freshness
    therefore never had a real date to compare against: quarter_verified was
    True and real_earnings_date null for all 118 stored views, i.e. the
    STALE_QUARTER guard had never once fired. Keep the import.
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

def _check_quarter_freshness(model_earnings_date, real_last_earnings_date, market, ticker=None):
    """Pure comparison (no I/O) — True if the model's claimed earnings date is
    consistent with the actual last-reported date, or if there isn't enough
    signal to contradict the model (fail open).

    Only returns False when a CONFIRMED earnings report exists inside the
    search window (so the model should have found and cited it) and the
    model either didn't cite a date or cited one that doesn't match.
    """
    if real_last_earnings_date is None:
        return True
    window = search_window_days(market, ticker)
    today = datetime.now(timezone.utc).date()
    if (today - real_last_earnings_date).days > window:
        return True  # nothing new expected within the search window either way

    # A missing or unparseable date is NOT evidence that the model analysed the
    # wrong quarter -- it only means it didn't cite a release date, which
    # Indian coverage frequently doesn't state. This used to `return False`,
    # and it was the single largest source of destroyed verdicts: of the 31
    # Indian tickers inside the window, 14 gave no date, and every one was
    # forced to Unknown despite carrying EPS/guidance/analyst evidence.
    # Thin evidence is NO_DATA's and PARTIAL's job to judge, below -- not this
    # function's.
    if not model_earnings_date:
        return True
    try:
        model_date = datetime.strptime(model_earnings_date, "%Y-%m-%d").date()
    except Exception:
        return True

    # Only flag when we can POSITIVELY show an older quarter was used. The old
    # test was abs(delta) <= 10, which encodes the US convention of announcing
    # within ~2 weeks of quarter-end. Indian companies report Q1 FY27 (ending
    # Jun 30) between late July and mid-August, so a model citing the quarter
    # END -- 2026-06-30 against a 2026-07-24 release -- could never pass, and
    # 5 more tickers died that way. Anything inside one reporting period of the
    # real release belongs to that release.
    return model_date >= real_last_earnings_date - timedelta(days=QUARTER_SPAN_DAYS)

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
    market = row_data.get("market", "us_invested")
    company_name = row_data.get("company_name", ticker)

    if news_text is None:
        try:
            news_text, news_source = fetch_fundamental_news(client, ticker, market, company_name, is_retry=is_retry)
        except TimeoutError:
            # Let the requeue signal through. fetch_fundamental_news raises this
            # on a first-pass ladder exhaustion specifically so the caller can
            # retry it later -- but TimeoutError subclasses OSError subclasses
            # Exception, so the blanket handler below used to swallow it. The
            # result: refresh_fundamentals' retry_queue was always empty and its
            # entire retry phase (~50 lines, including a 30s backoff) had never
            # once executed.
            raise
        except Exception as e:
            print(f"  [fundamental news fetch failed/timeout] {ticker}: {e} -> Proceeding with fallback")
            news_text, news_source = "No recent fundamental news found.", "⚪ No Source"

    window = search_window_days(market, ticker)
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
4. "earnings_report_date" is the date the results were ANNOUNCED (YYYY-MM-DD), not the date the quarter ended. For example, an Indian company reporting Q1 FY27 (quarter ending 2026-06-30) in late July announces on roughly 2026-07-24 — use the announcement date. If the news does not state one, set it to null; do NOT guess, and do NOT substitute the quarter-end date.
5. Setting "earnings_report_date" to null does not invalidate the rest of your answer. Judge "sentiment" from the facts you actually found, using rules 1-3 above. Report only what the news supports.

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
        data["quarter_verified"] = _check_quarter_freshness(data.get("earnings_report_date"), real_date, market, ticker)
        data["real_earnings_date"] = real_date.isoformat() if real_date else None
        return data

    from stock_data import load_settings
    settings = load_settings()
    model = settings.get("sentiment_reasoning_model", "models/gemini-3.5-flash-lite")
    budget = settings.get("sentiment_thinking_budget", 8192)

    if not model.startswith("models/"):
        model = f"models/{model}"

    def _config_for(m):
        kwargs = {"response_mime_type": "application/json"}
        # Gemma models reject a thinking config.
        if "gemma" not in m:
            if isinstance(budget, str):
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=budget)
            else:
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
        return types.GenerateContentConfig(**kwargs)

    # The good model, the good model again after a short backoff, then Gemma.
    # The middle tier is new: this used to drop to the fallback on the FIRST
    # exception, so one transient 429 -- the most common failure on a serial
    # ~110-ticker run -- permanently demoted that ticker for the night. The
    # ladder also now stops early on a terminal error (bad key, exhausted
    # quota) instead of burning every tier on something a retry cannot fix.
    data, used = llm_util.run_model_ladder(
        client, prompt,
        llm_util.standard_tiers(model, "models/gemma-4-31b-it"),
        _config_for, label="sentiment", subject=ticker,
        on_success=lambda resp: json.loads(_clean_json_text(resp.text)),
    )
    if used is None:
        return _pending_fallback("reasoning ladder exhausted")
    label = model.split("/")[-1] if used == model else f"{used.split('/')[-1]} (Fallback)"
    return _finalize(data, label)


def _search_failed_unknown(view):
    """True if this view is Unknown ONLY because the news search stage failed.

    "No Source" is written exactly where fetch_fundamental_news exhausted both
    of its search models -- distinct from a successful search that legitimately
    found nothing, which still carries a Gemma source label.
    """
    return (
        (view or {}).get("sentiment") == "Unknown"
        and "No Source" in str((view or {}).get("news_source", ""))
    )


def _prior_worth_keeping(old_view):
    """True if the previously stored view is valid and not yet stale."""
    if not _is_valid_view(old_view):
        return False
    age = _view_age_days(old_view)
    return age is None or age <= SENTIMENT_STALE_DAYS


def analyze_single_ticker_sentiment(ticker, row_data, api_key, is_retry=True):
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
    old_view = load_fundamentals().get(ticker)
    view = generate_fundamental_view(client, row_data, is_retry=is_retry)
    if not _is_valid_view(view):
        return None
    if _search_failed_unknown(view) and _prior_worth_keeping(old_view):
        # The search stage came back empty because it FAILED, not because the
        # company has no news -- this path passes is_retry=True, so
        # fetch_fundamental_news swallows its own ladder exhaustion and returns
        # "No recent fundamental news found." instead of raising the requeue
        # signal the batch loop relies on. The model then answers "Unknown",
        # which is a perfectly valid view by _is_valid_view, and writing it
        # replaced a fresh Positive/Negative with Unknown for the rest of the
        # day. The batch path is covered by refresh_fundamentals' retry queue;
        # this one had nothing, so guard it here.
        return None
    # Re-read rather than reusing the copy loaded above: generate_fundamental_view
    # can take minutes, and the nightly refresh writes this same file, so holding
    # a pre-call snapshot across the LLM call and writing it back would silently
    # revert every other ticker the batch job finished in the meantime.
    all_views = load_fundamentals()
    all_views[ticker] = view
    save_fundamentals(all_views)
    return view
