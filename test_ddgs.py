import sys
from duckduckgo_search import DDGS
try:
    with DDGS(timeout=4) as ddgs:
        results = list(ddgs.news("AAPL stock news", max_results=4))
        print("Success:", len(results))
except Exception as e:
    print("Error:", e)
