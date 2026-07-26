"""
AI Stock Expert View Engine powered by gemini-3.6-flash.
Evaluates rich quantitative indicators, trend rules, active alert conditions,
and free web news catalysts to produce actionable investor takes.
"""

import json
import os
from datetime import datetime, timezone
from news_search import get_stock_news

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXPERT_VIEWS_FILE = os.path.join(SCRIPT_DIR, "expert_views.json")


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


def build_expert_prompt(row_data, news_text, active_alerts_text="None"):
    ticker = row_data.get("ticker", "UNKNOWN")
    company_name = row_data.get("company_name", ticker)
    market = row_data.get("market", "US")
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
    bench = "S&P 500 (SPY)" if market == "US" else "Nifty 500 (^CRSLDX)"
    
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
4. RECENT WEB NEWS & ANNOUNCEMENTS (Last 24-48 hours via Free Search)
======================================================================
{news_text}

======================================================================
EXPERT INSTRUCTIONS:
======================================================================
Evaluate this stock from a disciplined growth-and-momentum investor perspective:
1. Determine the overall Verdict. Must be EXACTLY ONE of:
   - "ACCUMULATE" (Strong trend, outperforming benchmark, positive accumulation, good entry/add risk-reward)
   - "HOLD" (In consolidation, mixed technicals, or awaiting catalyst breakout)
   - "CAUTION" (Downtrend, broken VStop, severe underperformance, heavy distribution, or negative catalyst)
2. Provide a 1-line headline summarizing the setup.
3. Provide a concise Technical & Volume Assessment.
4. Provide a concise Catalyst Assessment (or note absence of news).
5. Provide a 2-3 sentence Actionable Take specifying ideal buy/add zones, trailing stop levels, or exit triggers.

Return ONLY a valid JSON object matching this schema:
{{
  "verdict": "ACCUMULATE" | "HOLD" | "CAUTION",
  "headline": "Short 1-line summary statement",
  "technical_summary": "Concise technical/volume takeaway",
  "catalyst_summary": "Concise news/catalyst takeaway",
  "actionable_take": "Clear actionable advice for an investor"
}}"""
    return prompt


def generate_expert_view(client, row_data, news_text=None, news_source=None, active_alerts_text=None, nvidia_api_key=None):
    from google.genai import types
    import json
    from datetime import datetime, timezone

    ticker = row_data.get("ticker", "UNKNOWN")
    market = row_data.get("market", "US")
    company_name = row_data.get("company_name", ticker)

    if news_text is None:
        news_text, news_source = get_stock_news(ticker, market=market, company_name=company_name)

    prompt = build_expert_prompt(row_data, news_text, active_alerts_text)
    
    # 1. Try Gemini 3.5 Flash Lite (Primary) with High Thinking
    try:
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            thinking_config=types.ThinkingConfig(thinking_budget=4096)
        )
        resp = client.models.generate_content(model="gemini-3.5-flash-lite", contents=prompt, config=config)
        data = json.loads(resp.text)
        data["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        data["news_used"] = news_text
        data["news_source"] = news_source or "⚪ Unknown"
        data["model_used"] = "gemini-3.5-flash-lite"
        return data
    except Exception as e:
        print(f"  [gemini fallback] {ticker}: {e} -> Falling back to DeepSeek/Error")

    # 2. Fallback to DeepSeek V4 Flash via NVIDIA API if configured
    if nvidia_api_key:
        try:
            from openai import OpenAI
            nv_client = OpenAI(
                base_url="https://integrate.api.nvidia.com/v1",
                api_key=nvidia_api_key
            )
            completion = nv_client.chat.completions.create(
                model="deepseek-ai/deepseek-v4-flash",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2, # Low temp for structured JSON
                top_p=0.95,
                max_tokens=1024,
                response_format={"type": "json_object"},
                extra_body={"chat_template_kwargs": {"thinking": True, "reasoning_effort": "low"}},
                stream=False
            )
            raw_text = completion.choices[0].message.content.strip()
            if raw_text.startswith("```json"):
                raw_text = raw_text.split("```json", 1)[1]
            elif raw_text.startswith("```"):
                raw_text = raw_text.split("```", 1)[1]
            if raw_text.endswith("```"):
                raw_text = raw_text.rsplit("```", 1)[0]
            raw_text = raw_text.strip()
            data = json.loads(raw_text)
            data["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            data["news_used"] = news_text
            data["news_source"] = news_source or "⚪ Unknown"
            data["model_used"] = "deepseek-v4-flash"
            return data
        except Exception as e:
            print(f"  [deepseek error] {ticker}: {e}")
        return {
            "verdict": "HOLD",
            "headline": f"Analysis pending -- {e}",
            "technical_summary": "Technical data available in table.",
            "catalyst_summary": news_text,
            "actionable_take": "Review technical indicators in table.",
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "news_source": news_source or "⚪ Unknown",
            "model_used": "Error"
        }


def analyze_single_ticker(ticker, row_data, api_key, active_alerts_text=None, nvidia_api_key=None):
    from google import genai

    client = genai.Client(api_key=api_key)
    view = generate_expert_view(client, row_data, active_alerts_text=active_alerts_text, nvidia_api_key=nvidia_api_key)
    all_views = load_expert_views()
    all_views[ticker] = view
    save_expert_views(all_views)
    return view
