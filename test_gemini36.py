import os
from dotenv import load_dotenv
from google import genai

load_dotenv(".env")
api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

print("Testing gemini-3.6-flash...")
try:
    resp = client.models.generate_content(
        model="gemini-3.6-flash", 
        contents="Hello, quick test."
    )
    print("SUCCESS!")
    print(resp.text)
except Exception as e:
    print("FAILED:", e)
