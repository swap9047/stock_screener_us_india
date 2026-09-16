"""REFRESH_LIMIT / partial news runs, offline (Gemini, Discord and file IO stubbed)."""

import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
GATE = str(Path(__file__).resolve().parents[1] / ".github/actions/slot-gate")
sys.path.insert(0, REPO)
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, GATE)

import os, sys, types, time

import llm_util, refresh_fundamentals as rf, refresh_expert_views as rev, news_check as nc

fails = 0
def check(ok, label):
    global fails; fails += not ok; print("PASS" if ok else "FAIL", label)

WL = {"us_invested": ["A1", "A2", "A3"], "us_watchlist": ["A2", "B1", "B2"], "india_invested": ["C1.NS"]}
SNAP = {"per_market": {m: [{"ticker": t, "company_name": t} for t in tks if t != "A1"] for m, tks in WL.items()}}
time.sleep = lambda s: None

def run_script(mod, env, gen_name, apply_ret):
    for k in ("REFRESH_MARKETS", "REFRESH_LIMIT"): os.environ.pop(k, None)
    os.environ.update(env)
    calls, store = [], {}
    mod.get_gemini_api_key = lambda: "k"
    mod.llm_util.make_client = lambda key: object()
    mod.load_data_snapshot = lambda: SNAP
    mod.load_watchlists = lambda: WL
    setattr(mod, gen_name, lambda client, row, *a, **kw: calls.append(row["ticker"]) or {"ok": True})
    mod._apply_result = lambda st, tk, view, old, el: (st.__setitem__(tk, view), apply_ret)[1]
    for name in ("load_fundamentals", "load_expert_views"):
        if hasattr(mod, name): setattr(mod, name, lambda: store)
    for name in ("save_fundamentals", "save_expert_views"):
        if hasattr(mod, name): setattr(mod, name, lambda s: None)
    if hasattr(mod, "active_alerts_for_prompt"):
        mod.active_alerts_for_prompt = lambda rows: None
        mod.alerts_text_for = lambda a, t: ""
    mod.main()
    return calls

for mod, gen, ret in ((rf, "generate_fundamental_view", (0, "ok")), (rev, "generate_expert_view", (0, 0, "ok"))):
    n = mod.__name__
    check(run_script(mod, {}, gen, ret) == ["A2", "A3", "B1", "B2", "C1.NS"], f"{n}: no limit -> every ticker with a row, duplicates once")
    check(run_script(mod, {"REFRESH_LIMIT": "2"}, gen, ret) == ["A2", "A3"], f"{n}: limit 2 -> first 2 analysed (a no-row ticker doesn't use a slot)")
    check(run_script(mod, {"REFRESH_MARKETS": "us_watchlist", "REFRESH_LIMIT": "2"}, gen, ret) == ["A2", "B1"], f"{n}: markets + limit 2")
    check(run_script(mod, {"REFRESH_LIMIT": "abc"}, gen, ret) == ["A2", "A3", "B1", "B2", "C1.NS"], f"{n}: non-numeric limit ignored")

# news_check
def run_news(env):
    for k in ("REFRESH_MARKETS", "REFRESH_LIMIT"): os.environ.pop(k, None)
    os.environ.update(env)
    got = {}
    nc.get_gemini_api_key = lambda: "k"
    nc.load_watchlists = lambda: WL
    nc.build_news_summary = lambda wl, key: got.setdefault("built", wl) and {"as_of": "new", "totals": {"searched": 2, "failed": 0},
                                                                         "markets": {m: {"summary": "NEW"} for m in wl}}
    nc.load_news_summary = lambda: {"as_of": "old", "markets": {"us_invested": {"summary": "OLD"}, "india_invested": {"summary": "OLD"}}}
    nc.save_news_summary = lambda d: got.setdefault("saved", d)
    nc.load_discord_webhook = lambda: "https://discord.example/hook"
    nc.build_discord_messages = lambda d: ["msg"]
    nc.send_discord_batch = lambda *a, **kw: got.setdefault("sent", True) and (True, "")
    nc.main()
    return got

g = run_news({})
check(g["built"] == WL and g.get("sent") and set(g["saved"]["markets"]) == set(WL), "news full run: all watchlists, sent to Discord, file replaced")
g = run_news({"REFRESH_MARKETS": "us_invested", "REFRESH_LIMIT": "2"})
check(g["built"] == {"us_invested": ["A1", "A2"]}, f"news partial: only us_invested's first 2 ({g['built']})")
check(not g.get("sent"), "news partial: nothing sent to Discord")
check(g["saved"]["markets"] == {"us_invested": {"summary": "NEW"}, "india_invested": {"summary": "OLD"}}, "news partial: only that digest replaced, others kept")
g = run_news({"REFRESH_LIMIT": "4"})
check(g["built"] == {"us_invested": ["A1", "A2", "A3"], "us_watchlist": ["A2"]}, f"news limit spans watchlists in order ({g['built']})")
os.environ.pop("REFRESH_LIMIT", None); os.environ.pop("REFRESH_MARKETS", None)
print("FAILURES:", fails); sys.exit(fails)
