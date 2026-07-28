import os
from dotenv import load_dotenv
from google import genai
from expert_views import generate_expert_view

load_dotenv(".env")
api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

# Minimal test row
test_row = {
    "ticker": "AAPL", "market": "US", "last_close": 210.5,
    "ema10": 208, "ema20": 205, "ema40": 200,
    "rsi14_daily": 62, "rsi14_weekly": 58, "rsi14_monthly": 55,
    "trend": "Stage 2 Uptrend", "trend_rank": 4,
}

print("Testing full expert_view pipeline with gemini-3.5-flash...")
result = generate_expert_view(client, test_row)
print(f"Verdict: {result.get('verdict')}")
print(f"Headline: {result.get('headline')}")
print(f"As of: {result.get('as_of')}")
