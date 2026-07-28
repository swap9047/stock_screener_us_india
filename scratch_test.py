import stock_data
from github_sync import push_all_config
import os, json

# 1. load
wl = stock_data.load_watchlists()
print("Initial:", wl["US"][-1:])

# 2. Add ticker
stock_data.save_watchlist("US", wl["US"] + ["TEST_TICKER"])

# 3. Read back
wl2 = stock_data.load_watchlists()
print("After Add:", wl2["US"][-1:])

# 4. Simulate push_all_config reading it
with open(stock_data.WATCHLIST_FILE, "rb") as f:
    content = f.read().decode("utf-8")
    parsed = json.loads(content)
    print("Disk content:", parsed["US"][-1:])

# 5. Clean up
stock_data.save_watchlist("US", wl["US"])
