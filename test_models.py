import os
from dotenv import load_dotenv
from google import genai

load_dotenv(".env")
api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

print("Listing available models...")
try:
    for m in client.models.list():
        print(f"Model: {m.name}")
except Exception as e:
    print("FAILED:", e)
