#!/usr/bin/env python3
import sys
import os
import requests

def test_key(api_key):
    print(f"Testing Gemini API Key: {api_key[:6]}...{api_key[-4:] if len(api_key)>10 else ''}")
    
    # 1. List available models
    url_list = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    try:
        res = requests.get(url_list, timeout=10)
        if res.status_code == 200:
            models = res.json().get("models", [])
            model_names = [m.get("name", "").replace("models/", "") for m in models]
            print(f"\n[+] API Key is VALID! Total models available: {len(model_names)}")
            
            gemini_3_models = [m for m in model_names if "3." in m or "3-" in m or "3_0" in m]
            print("\nGemini 3.x models found in your model list:")
            if gemini_3_models:
                for m in gemini_3_models:
                    print(f"  - {m}")
            else:
                print("  (No Gemini 3.x models returned in model list endpoint)")
        else:
            print(f"[-] API Key check failed: HTTP {res.status_code}")
            print(res.text)
            return
    except Exception as e:
        print(f"[-] Error querying model list: {e}")
        return

    # 2. Test specific Gemini 3.x and 2.5 models
    test_models = [
        "gemini-2.5-flash",
        "gemini-3.0-flash",
        "gemini-3.5-flash",
        "gemini-3.6-flash",
        "gemini-1.5-flash"
    ]
    
    print("\n=== Testing Generation & Grounding per Model ===")
    for model in test_models:
        # Test standard generation
        gen_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload_simple = {
            "contents": [{"parts": [{"text": "Hello, respond with OK."}]}]
        }
        try:
            r = requests.post(gen_url, json=payload_simple, timeout=10)
            if r.status_code == 200:
                print(f"  ✅ {model:<20}: Basic Generation SUCCESS")
            else:
                err = r.json().get("error", {}).get("message", r.text[:80])
                print(f"  ❌ {model:<20}: Failed ({r.status_code}) -> {err}")
        except Exception as e:
            print(f"  ❌ {model:<20}: Exception -> {e}")

if __name__ == "__main__":
    key = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GEMINI_API_KEY")
    if not key:
        print("Usage: python3 test_gemini_key.py YOUR_API_KEY")
        print("   OR set GEMINI_API_KEY in environment.")
    else:
        test_key(key)
