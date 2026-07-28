import json
from stock_data import load_settings, load_watchlists, MARKETS

def debug_snapshot_is_usable(snapshot, watchlists, settings):
    if not snapshot or not isinstance(snapshot.get("per_market"), dict):
        print("Failed: No snapshot or per_market is not a dict")
        return False
        
    snap_calc = {k: v for k, v in snapshot.get("settings", {}).items() if not k.startswith(("news_", "expert_"))}
    curr_calc = {k: v for k, v in settings.items() if not k.startswith(("news_", "expert_"))}
    
    if snap_calc != curr_calc:
        print("Failed: Settings mismatch")
        return False
        
    per_market = snapshot["per_market"]
    for market in MARKETS:
        snap_tickers = {r.get("ticker") for r in per_market.get(market, [])}
        wanted = set(watchlists.get(market, []))
        missing = wanted - snap_tickers
        if missing:
            print(f"Failed: Missing tickers in {market} snapshot: {missing}")
            return False
    return True

settings = load_settings()
watchlists = load_watchlists()
with open("data_snapshot.json", "r") as f:
    snapshot = json.load(f)

print("Result:", debug_snapshot_is_usable(snapshot, watchlists, settings))
