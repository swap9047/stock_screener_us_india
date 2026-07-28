import os
import json
from dotenv import load_dotenv
from google import genai

load_dotenv(".env")
api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

print("Testing gemini-3.5-flash-lite...")
try:
    resp = client.models.generate_content(
        model="gemini-3.5-flash-lite", 
        contents="Hello, testing lite model limits."
    )
    print("SUCCESS lite!")
    print(resp.text)
except Exception as e:
    print("FAILED lite:", e)
