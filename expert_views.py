"""
AI Stock Expert View Engine powered by gemini-3.6-flash.
Evaluates rich quantitative indicators, trend rules, active alert conditions,
and free web news catalysts to produce actionable investor takes.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from google.genai import types

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXPERT_VIEWS_FILE = os.path.join(SCRIPT_DIR, "expert_views.json")

import concurrent.futures

def _generate_with_timeout(client, model, contents, config, timeout=120):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(client.models.generate_content, model=model, contents=contents, config=config)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise TimeoutError(f"API call to {model} timed out after {timeout}s")
    finally:
        executor.shutdown(wait=False)

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


def fetch_gemma_expert_news(client, ticker, market, company_name, is_retry=False):
    """Fetches news specifically for Expert Views using gemma-4-26b-a4b-it with Google Search.
    Falls back to 31b."""
    from stock_data import get_exchange_label

    as_of_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    exchange = get_exchange_label(market, ticker)

    bare = ticker.rsplit(".", 1)[0] if ticker.endswith(".NS") or ticker.endswith(".BO") else ticker
    name = f"{company_name} ({bare})" if company_name and company_name != ticker else bare

    prompt = (
        f"You are a financial news researcher. For the {exchange} stock {name} -- "
        f"search for recent institutional analyst ratings, upgrades/downgrades, press releases, "
        f"and major upcoming catalysts (e.g., earnings (latest quarter only), product launches) between "
        f"{cutoff_date} and {as_of_date} (the last 24 hours for news, next 3-4 days for events). "
        "Report any material items you find, specifying the exact date of each item. Be extremely concise. "
        "If there is no material news, output nothing."
    )
    
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(tools=[grounding_tool])
    
    try:
        resp = _generate_with_timeout(client, "models/gemma-4-26b-a4b-it", prompt, config, timeout=120)
        text = resp.text or ""
        if not text.strip():
            return "No recent news found.", "🔍 Gemma-4-26B (Google Search)"
        return text, "🔍 Gemma-4-26B (Google Search)"
    except Exception as e:
        print(f"  [expert gemma 26b search failed/timeout] {ticker}: {e} -> Falling back to 31b")
        try:
            resp = _generate_with_timeout(client, "models/gemma-4-31b-it", prompt, config, timeout=120)
            text = resp.text or ""
            if not text.strip():
                return "No recent news found.", "🔍 Gemma-4-31B (Google Search)"
            return text, "🔍 Gemma-4-31B (Google Search)"
        except Exception as e2:
            print(f"  [expert gemma 31b search failed/timeout] {ticker}: {e2}")
            if not is_retry:
                raise TimeoutError("Search timed out. Add to retry queue.")
            print(f"  [expert search final fallback] {ticker} -> No source available")
            return "No recent news found.", "⚪ No Source"


def load_expert_views():
    if not os.path.exists(EXPERT_VIEWS_FILE):
        return {}
    try:
        with open(EXPERT_VIEWS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_expert_views(data):
    with open(EXPERT_VIEWS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def stale_view_fallback(reason):
    """Full-schema 'HOLD/pending' view for a prior verdict that's gone stale
    (regeneration has failed for more than EXPERT_STALE_DAYS). Mirrors
    fundamentals_eval._unknown_fallback so a stuck pipeline stops silently
    displaying an unverified old ACCUMULATE/CAUTION call as current."""
    return {
        "verdict": "HOLD",
        "headline": f"Analysis pending -- {reason}",
        "technical_summary": "Technical data available in table.",
        "catalyst_summary": "N/A",
        "actionable_take": "Review technical indicators in table.",
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "news_source": "⚪ Unknown",
        "model_used": "Error",
    }


def _is_valid_view(view):
    """Returns True if a view is a real successful analysis (not a 429/error fallback)."""
    if not view:
        return False
    verdict = view.get("verdict")
    if verdict not in ("ACCUMULATE", "HOLD", "CAUTION"):
        return False
    headline = (view.get("headline") or "").lower()
    if "429" in headline or "resource_exhausted" in headline or "analysis pending" in headline or "error" in headline:
        return False
    return True


EXPERT_STALE_DAYS = 4

def _view_age_days(view):
    """Age of a view in days, or None if as_of is missing/unparseable.

    This is a pipeline-health circuit breaker, not a "has the market moved
    on" freshness check: as_of is refreshed every time generation succeeds
    (even if the verdict is unchanged), so a healthy nightly run keeps this
    at ~0 regardless of how long the same verdict has held. It only climbs
    past EXPERT_STALE_DAYS when regeneration has been failing for several
    consecutive nights, at which point the stale verdict should stop being
    displayed as current."""
    as_of = (view or {}).get("as_of")
    if not as_of:
        return None
    try:
        ts = datetime.fromisoformat(as_of.replace(" ", "T", 1))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400
    except Exception:
        return None


# The decision rules the model must follow when picking a verdict. Kept as a
# module constant rather than inline in build_expert_prompt because the
# copy-for-AI payload in app.py quotes these same rules back to the user --
# two copies would silently drift the moment the prompt is tuned, and the
# payload would then be describing a decision rule the model never saw.
VERDICT_RULES = """MANDATORY VERDICT RULES — apply these strictly before choosing a verdict:
- HOLD is the DEFAULT. Use it whenever the picture is mixed, data is thin, or confidence is low.
- ACCUMULATE requires ALL of: (a) Trend is "Uptrend" or "Strong Uptrend", (b) VStop direction is UP held ≥ 3 weeks, (c) RS is positive or N/A for very new data, (d) No negative news catalyst. If news is ABSENT, you may still give ACCUMULATE ONLY if ALL technical conditions above are clearly met — never give ACCUMULATE just because news is absent.
- CAUTION requires AT LEAST TWO of the following five signals to agree — a single isolated signal (e.g. trend just not yet confirmed as an uptrend, with everything else neutral or positive) is NOT enough on its own and must fall through to HOLD instead: (1) Trend is Downtrend, (2) VStop flipped DOWN, (3) RSI > 80 on weekly or monthly (severely overbought), (4) heavy distribution (Net Volume 10D Negative with large ratio), (5) a clearly negative news catalyst.
- NEVER give ACCUMULATE when news shows a negative catalyst (earnings miss, downgrade, regulatory issue, fraud, etc.).
- NEVER give ACCUMULATE solely because news is absent or minimal — absent news → lean HOLD unless technicals fully satisfy the ACCUMULATE criteria above."""


def build_expert_prompt(row_data, news_text, active_alerts_text="None"):
    ticker = row_data.get("ticker", "UNKNOWN")
    company_name = row_data.get("company_name", ticker)
    market = row_data.get("market", "us_invested")
    last_close = row_data.get("last_close", "N/A")
    ema10 = row_data.get("ema10", "N/A")
    ema20 = row_data.get("ema20", "N/A")
    ema40 = row_data.get("ema40", "N/A")
    ema10_daily = row_data.get("ema10_daily", "N/A")
    ema50 = row_data.get("ema50", "N/A")
    ema200 = row_data.get("ema200", "N/A")
    rsi_d = row_data.get("rsi14_daily", "N/A")
    rsi_w = row_data.get("rsi14_weekly", "N/A")
    rsi_m = row_data.get("rsi14_monthly", "N/A")
    rs_d = row_data.get("rs_daily", "N/A")
    rs_w = row_data.get("rs_weekly", "N/A")
    rs_m = row_data.get("rs_monthly", "N/A")
    trend = row_data.get("trend", "N/A")
    trend_rank = row_data.get("trend_rank", "N/A")
    trend_detail = row_data.get("trend_detail") or {}
    vstop_weekly = row_data.get("vstop_weekly", "N/A")
    vstop_dir = row_data.get("vstop_weekly_direction", "N/A")
    vstop_wks = row_data.get("vstop_weekly_weeks_since_change", "N/A")
    tech_uptrend = "YES" if row_data.get("tech_uptrend") else "NO"
    vol_10d = row_data.get("avg_volume_10d", "N/A")
    vol_100d = row_data.get("avg_volume_100d", "N/A")
    vol_trend = row_data.get("volume_trend", "N/A")
    net_vol_dir = row_data.get("net_volume_10d_dir", "N/A")
    net_vol_ratio = row_data.get("net_volume_10d_ratio", "N/A")
    h52 = row_data.get("week52_high", "N/A")
    l52 = row_data.get("week52_low", "N/A")
    flag = row_data.get("flag", "None")
    note = row_data.get("note", "None")
    from stock_data import get_benchmark_display
    bench = get_benchmark_display(market)
    
    # Flag whether news is genuinely absent
    news_absent = not news_text or news_text.strip().lower() in (
        "no recent news found.", "no recent news found", "", "none"
    )
    news_quality_note = (
        "⚠️ NEWS DATA: ABSENT — no material news was found for this ticker. "
        "This MUST constrain the verdict (see rules below)."
        if news_absent else ""
    )

    prompt = f"""You are an elite equity portfolio manager combining Stan Weinstein stage analysis, trend momentum,
volume accumulation/distribution analysis, and fundamental catalyst evaluation.

Analyze the stock {company_name} (Ticker: {ticker}) ({market} market) using the structured quantitative metrics and
recent web news findings provided below.

======================================================================
1. QUANTITATIVE & TECHNICAL METRICS
======================================================================
- Last Close: {last_close}
- Weekly EMAs (Fast/Mid/Slow): 10 WEMA={ema10}, 20 WEMA={ema20}, 40 WEMA={ema40}
- Daily EMAs (Fast/Mid/Slow): 10 DEMA={ema10_daily}, 50 DEMA={ema50}, 200 DEMA={ema200}
- Momentum RSI: Daily={rsi_d}, Weekly={rsi_w}, Monthly={rsi_m}
- Mansfield Relative Strength (vs {bench}): Daily={rs_d}, Weekly={rs_w}, Monthly={rs_m}
- Trend Status: {trend} (Rank: {trend_rank})
  └ Trend Detail: Price > 40 WEMA: {trend_detail.get('price_above_ma')}, 40 WEMA Slope Rising: {trend_detail.get('slope_rising')}, Fast > Slow WEMA: {trend_detail.get('ema_aligned')}, RS Positive: {trend_detail.get('rs_positive')}, Near 52W High/Low: {trend_detail.get('near_high_low_pass')}
- Volatility Stop (VStop-W): Direction={vstop_dir}, Stop Level={vstop_weekly}, Weeks Held={vstop_wks}
- Tech Uptrend: {tech_uptrend} (Requires VStop uptrend > 3 wks, Price > 40 WEMA, Vol 10D >= 1.4x Vol 100D)
- Volume Analysis: Vol 10D={vol_10d}, Vol 100D={vol_100d}, Vol Trend={vol_trend}
- Net Volume 10D (Accumulation vs Distribution): Direction={net_vol_dir}, Ratio={net_vol_ratio}%
- 52-Week Range: High={h52}, Low={l52}

======================================================================
2. ACTIVE ALERT RULES TRIGGERED
======================================================================
{active_alerts_text or 'None'}

======================================================================
3. USER FLAGS & NOTES
======================================================================
- Flag: {flag} | Note: {note}

======================================================================
4. RECENT WEB NEWS & ANNOUNCEMENTS (Last 24-48 hours via Grounded Search)
======================================================================
{news_quality_note}
{news_text}

======================================================================
EXPERT INSTRUCTIONS:
======================================================================
Evaluate this stock from a disciplined growth-and-momentum investor perspective.

{VERDICT_RULES}

Then:
1. State the Verdict (ACCUMULATE / HOLD / CAUTION).
2. Provide a 1-line headline summarizing the key reason.
3. Concise Technical & Volume Assessment (2-3 sentences).
4. Concise Catalyst Assessment — if no news, explicitly state "No material news found; verdict based on technicals only."
5. Actionable Take (2-3 sentences): entry/add zones, trailing stop levels, or exit triggers.

Return ONLY a valid JSON object matching this schema:
{{
  "verdict": "ACCUMULATE" | "HOLD" | "CAUTION",
  "headline": "Short 1-line summary statement",
  "technical_summary": "Concise technical/volume takeaway",
  "catalyst_summary": "Concise news/catalyst takeaway",
  "actionable_take": "Clear actionable advice for an investor"
}}"""
    return prompt


def generate_expert_view(client, row_data, news_text=None, news_source=None, active_alerts_text=None, nvidia_api_key=None, is_retry=False):
    from google.genai import types
    import json
    from datetime import datetime, timezone

    ticker = row_data.get("ticker", "UNKNOWN")
    market = row_data.get("market", "us_invested")
    company_name = row_data.get("company_name", ticker)

    if news_text is None:
        try:
            news_text, news_source = fetch_gemma_expert_news(client, ticker, market, company_name, is_retry=is_retry)
        except Exception as e:
            print(f"  [expert news fetch failed/timeout] {ticker}: {e} -> Proceeding with technical evaluation only")
            news_text, news_source = "No recent news found.", "⚪ No Source"

    prompt = build_expert_prompt(row_data, news_text, active_alerts_text)

    from stock_data import load_settings
    settings = load_settings()
    model = settings.get("expert_reasoning_model", "models/gemini-3.5-flash-lite")
    budget = settings.get("expert_thinking_budget", 8192)

    def _pending_fallback(reason, used_model="Error"):
        return {
            "verdict": "HOLD",
            "headline": f"Analysis pending -- {reason}",
            "technical_summary": "Technical data available in table.",
            "catalyst_summary": news_text,
            "actionable_take": "Review technical indicators in table.",
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "news_source": news_source or "⚪ Unknown",
            "model_used": used_model,
        }

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
        data = json.loads(_clean_json_text(resp.text))
        data["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        data["news_used"] = news_text
        data["news_source"] = news_source or "⚪ Unknown"
        data["model_used"] = model.split("/")[-1]
        return data
    except Exception as e:
        print(f"  [{model} reasoning failed] {ticker}: {e} -> Falling back to 31b")

    # 2. Fallback: gemma-4-31b-it
    try:
        config = types.GenerateContentConfig(response_mime_type="application/json")
        resp = _generate_with_timeout(client, "models/gemma-4-31b-it", prompt, config, timeout=120)
        data = json.loads(_clean_json_text(resp.text))
        data["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        data["news_used"] = news_text
        data["news_source"] = news_source or "⚪ Unknown"
        data["model_used"] = "gemma-4-31b-it (Fallback)"
        return data
    except Exception as e2:
        print(f"  [gemma-4-31b reasoning failed] {ticker}: {e2} -> Falling back to 26b")

    # 3. Fallback: gemma-4-26b-a4b-it
    try:
        config = types.GenerateContentConfig(response_mime_type="application/json")
        resp = _generate_with_timeout(client, "models/gemma-4-26b-a4b-it", prompt, config, timeout=120)
        data = json.loads(_clean_json_text(resp.text))
        data["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        data["news_used"] = news_text
        data["news_source"] = news_source or "⚪ Unknown"
        data["model_used"] = "gemma-4-26b-a4b-it (Fallback)"
        return data
    except Exception as e3:
        print(f"  [gemma-4-26b reasoning failed] {ticker}: {e3} -> Giving up")
        return _pending_fallback(str(e3))


def analyze_single_ticker(ticker, row_data, api_key, active_alerts_text=None, nvidia_api_key=None, is_retry=True):
    from google import genai

    client = genai.Client(api_key=api_key)
    view = generate_expert_view(client, row_data, active_alerts_text=active_alerts_text, nvidia_api_key=nvidia_api_key, is_retry=is_retry)
    all_views = load_expert_views()
    all_views[ticker] = view
    save_expert_views(all_views)
    return view

# Trigger Streamlit Cloud hot-reload
