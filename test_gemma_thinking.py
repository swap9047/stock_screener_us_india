import os
from dotenv import load_dotenv
load_dotenv('.env')

from google import genai
client = genai.Client()

from news_summary import filter_batch_with_reasoning

print("Testing with gemini-3.5-flash-lite (Thinking Budget = 1024)...")
res1 = filter_batch_with_reasoning(
    client, 
    "**AAPL**: Apple announced record earnings.", 
    ["AAPL"], 
    "US", 
    "2026-07-27", 
    model="gemini-3.5-flash-lite", 
    budget=1024
)
print("Result 1 snippet:", res1[:100].replace('\n', ' '))

print("\nTesting with gemma-4-31b-it (Thinking Budget = 1024, should be ignored by backend)...")
res2 = filter_batch_with_reasoning(
    client, 
    "**AAPL**: Apple announced record earnings.", 
    ["AAPL"], 
    "US", 
    "2026-07-27", 
    model="models/gemma-4-31b-it", 
    budget=1024
)
print("Result 2 snippet:", res2[:100].replace('\n', ' '))
