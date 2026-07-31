"""Live smoke test for the 2026-07-31 sentiment-fix (structured evidence
fields + deterministic earnings-date cross-check) against a couple of real
tickers. Uses the real Gemini API + real yfinance data -- NOT a unit test.

Does not call save_fundamentals(); fundamentals.json is left untouched.
"""
import os
from dotenv import load_dotenv
load_dotenv(".env")

from google import genai
from fundamentals_eval import generate_fundamental_view, _validate_sentiment

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise SystemExit("GEMINI_API_KEY not found after load_dotenv('.env')")

client = genai.Client(api_key=api_key)

TICKERS = [
    {"ticker": "NH.NS", "market": "INDIA", "company_name": "Narayana Hrudayalaya"},
    {"ticker": "SANSERA.NS", "market": "INDIA", "company_name": "Sansera Engineering"},
    {"ticker": "TWST", "market": "US", "company_name": "Twist Bioscience"},
]

for row in TICKERS:
    print(f"\n{'='*70}\n{row['ticker']} ({row['market']})\n{'='*70}")
    try:
        view = generate_fundamental_view(client, row)
    except Exception as e:
        print(f"  EXCEPTION during generate_fundamental_view: {e!r}")
        continue

    for key in ("model_used", "news_source", "sentiment", "earnings_report_date",
                "eps_value", "guidance_change", "analyst_action",
                "quarter_verified", "real_earnings_date"):
        print(f"  {key}: {view.get(key)!r}")
    print(f"  earnings_summary: {view.get('earnings_summary')!r}")
    print(f"  future_guidance: {view.get('future_guidance')!r}")
    print(f"  analyst_coverage: {view.get('analyst_coverage')!r}")
    print(f"  reasoning: {view.get('reasoning')!r}")

    final_sentiment, flag = _validate_sentiment(view)
    print(f"  --> _validate_sentiment: sentiment={final_sentiment!r} flag={flag!r}")

print("\nDone. fundamentals.json was NOT modified by this script.")
