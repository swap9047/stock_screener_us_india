import os
import json
from dotenv import load_dotenv
load_dotenv('.env')

from google import genai
client = genai.Client()
from news_summary import fetch_single_raw_news, filter_batch_with_reasoning

print("=== TESTING SEARCH MODELS ===")
search_models = ["models/gemma-4-31b-it", "models/gemma-4-26b-a4b-it"]
for m in search_models:
    try:
        print(f"Testing fetch_single_raw_news with {m}...")
        raw_text, sources = fetch_single_raw_news(client, "AAPL", "US", "2026-07-27", model=m)
        print(f"  SUCCESS. Result length: {len(raw_text)}, Sources count: {len(sources)}")
    except Exception as e:
        print(f"  FAILED for {m}: {e}")

print("\n=== TESTING REASONING MODELS ===")
reasoning_configs = [
    ("models/gemini-3.5-flash-lite", 1024),
    ("models/gemma-4-31b-it", "HIGH"),
    ("models/gemma-4-26b-a4b-it", "LOW")
]
dummy_text = "**AAPL**: Apple announced record earnings and acquired a small AI startup. Released 10 hours ago."

for m, budget in reasoning_configs:
    try:
        print(f"Testing filter_batch_with_reasoning with {m} (budget/level={budget})...")
        clean = filter_batch_with_reasoning(client, dummy_text, ["AAPL"], "US", "2026-07-27", model=m, budget=budget)
        print(f"  SUCCESS. Result snippet: {clean[:60].replace(chr(10), ' ')}")
    except Exception as e:
        print(f"  FAILED for {m}: {e}")
