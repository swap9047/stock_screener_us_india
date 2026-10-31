"""The invested lists' reads (2026-10-31), offline: invented lists, closes and a fake model; nothing reads real JSON.

  L1  holdings callouts: trend mix, what is below the 200-day, what is near a 52-week low, leaders and laggards,
      each worked out by hand;
  L2  the quarterly results row: the list's median per quarter and the home index's median beside the newest;
  L3  the facts: the list's own breadth (the panel's maths) with the index's share beside it, the return against
      the home index, the index read as market context; results and holdings only for the latest session;
  L4  the prompt: the facts, the market read, only the last three earlier reads;
  L5  the check on the model's answer: numbers must come from the facts or the market read, a part with no facts
      must be null, two to five takeaways, no advice;
  L6  the job: after the index reads, each list once a session, the index read as its context, force, backfill,
      saved as it goes, a failed ladder saved nothing, no stock name in the log;
  L7  the box: a list read shows the market line, its parts, the takeaways as bullets and the benchmark in its
      footer; an index read looks as before;
  L8  the library copy: published with the index reads, and get_market_read serves a list by name.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO)
sys.path.insert(0, REPO + "/api")

import index_registry
import list_read as lr
import llm_util
import market_read as mr
import model_settings as ms
import publish_market as pm
from breadth_math import compute_breadth

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


llm_util.RETRY_BACKOFF_SECONDS = 0

# --- L1 holdings callouts ----------------------------------------------------------------------
ROWS = [
    {"ticker": "AAA.NS", "company_name": "Alpha Works Limited", "trend": "Strong Uptrend", "expert_take": "Accumulate", "last_close": 104.0,
     "week52_low": 100.0, "perf_1m": 12.0, "perf_3m": 30.0, "below_200": False},
    {"ticker": "BBB.NS", "company_name": "Beta Foods", "trend": "Downtrend", "expert_take": "Caution", "last_close": 50.0,
     "week52_low": 49.0, "perf_1m": -9.5, "perf_3m": -20.0, "below_200": True},
    {"ticker": "CCC.NS", "company_name": "Gamma Steel", "trend": "Uptrend", "expert_take": "Hold", "last_close": 200.0,
     "week52_low": 100.0, "perf_1m": 3.0, "perf_3m": 8.0, "below_200": False},
    {"ticker": "DDD.NS", "company_name": "Delta Pharma", "trend": "Downtrend", "expert_take": "Caution", "last_close": 70.0,
     "week52_low": 69.0, "perf_1m": -4.0, "perf_3m": -1.0, "below_200": True},
    {"ticker": "EEE.NS", "company_name": "Epsilon Tech", "trend": "Uptrend", "expert_take": "Hold", "last_close": 130.0,
     "week52_low": 120.0, "perf_1m": 7.0, "perf_3m": None, "below_200": False},
    {"ticker": "FFF.NS", "trend": "Mixed", "last_close": 90.0, "week52_low": None, "perf_1m": 1.0, "perf_3m": 2.0},
    "junk", {"company_name": "no ticker"},
]
c = lr.stock_callouts(ROWS)
check(c["stocks_in_list"] == 6 and c["trend_counts"] == {"Strong Uptrend": 1, "Downtrend": 2, "Uptrend": 2, "Mixed": 1}
      and c["expert_take_counts"] == {"Accumulate": 1, "Caution": 2, "Hold": 2},
      "L1: the trend and Expert Take mix; a row with no ticker, or not a row, is ignored")
check(c["below_200_day_average"] == {"count": 2, "examples": ["Beta Foods", "Delta Pharma"]},
      "L1: what is below its 200-day average, by name")
check([x["name"] for x in c["near_52_week_low"]] == ["Delta Pharma", "Beta Foods", "Alpha Works Limited"],
      "L1: nearest the low first, at most three")
check(c["leaders_1m"] == [{"name": "Alpha Works Limited", "return_pct": 12.0}, {"name": "Epsilon Tech", "return_pct": 7.0},
                          {"name": "Gamma Steel", "return_pct": 3.0}]
      and c["laggards_1m"] == [{"name": "Beta Foods", "return_pct": -9.5}, {"name": "Delta Pharma", "return_pct": -4.0},
                               {"name": "FFF.NS", "return_pct": 1.0}],
      "L1: the month's best and worst three (a name falls back to the ticker)")
check([x["name"] for x in c["laggards_3m"]] == ["Beta Foods", "Delta Pharma", "FFF.NS"] and "Epsilon Tech" not in
      [x["name"] for x in c["leaders_3m"] + c["laggards_3m"]], "L1: a stock with no 3-month figure is left out of that ranking")
check(lr.stock_callouts([]) is None and lr.stock_callouts(None) is None, "L1: no rows, no callouts")

# --- L2 quarterly results -----------------------------------------------------------------------


def _q(rev, ni):
    return {"rev": rev, "op": rev / 10, "ni": ni, "eps": ni / 10}


LIST_T = ["AAA.NS", "BBB.NS", "CCC.NS", "DDD.NS", "EEE.NS", "FFF.NS"]
IDX_T = [f"X{i}.NS" for i in range(8)]
QUARTERLY = {"as_of": "2026-10-30 08:00 UTC", "members": {"INDIA": IDX_T},
             "tickers": {**{t: {"q": {"2025-09-30": _q(100, 10), "2026-06-30": _q(110, 12), "2026-09-30": _q(120 + 2 * i, 13)}}
                            for i, t in enumerate(LIST_T)},
                         **{t: {"q": {"2025-09-30": _q(100, 10), "2026-06-30": _q(104, 11), "2026-09-30": _q(108, 12)}} for t in IDX_T}}}
rf = lr.results_facts(QUARTERLY, LIST_T, "INDIA")
# Sep '26 revenue growth on Sep '25 for the list: 20, 22, 24, 26, 28, 30 -> median 25; the index's: 8 for all.
check(rf["quarters"][-1]["quarter"] == "Sep '26" and rf["quarters"][-1]["revenue_growth_pct"] == 25.0
      and rf["quarters"][-1]["profit_growth_pct"] == 30.0 and rf["companies_with_results"] == 6
      and rf["index_median_same_quarter"]["revenue_growth_pct"] == 8.0,
      f"L2: the list's median revenue growth by hand (25.0), the index's beside it ({rf['quarters'][-1]}, {rf['index_median_same_quarter']})")
check(lr.results_facts(QUARTERLY, LIST_T[:3], "INDIA") is None and lr.results_facts({}, LIST_T, "INDIA") is None,
      "L2: three companies, or no results file, give no results (quarterly_results' own thresholds)")

# --- L3 the facts ----------------------------------------------------------------------------------
DAYS = pd.bdate_range(end="2026-10-30", periods=460)
D = [d.strftime("%Y-%m-%d") for d in DAYS]
N = len(D)
rng = np.random.default_rng(7)


def series(drift, vol=0.01, seed=0):
    r = np.random.default_rng(seed)
    px = 100 * np.exp(np.cumsum(r.normal(drift, vol, N)))
    return dict(zip(D, px.round(4).tolist()))


STOCKS = LIST_T
PERF = {"markets": {"india_invested": {t: series(0.0006 - 0.0002 * i, seed=i) for i, t in enumerate(STOCKS)},
                    "us_invested": {t: series(0.0004, seed=20 + i) for i, t in enumerate(["UA", "UB", "UC", "UD"])}},
        "benchmarks": {b: series(0.0002, 0.006, seed=40 + i) for i, b in enumerate(index_registry.BENCHMARK_TICKERS)}}


def block(h200, live=False):
    n = N
    return {"total": 480, "live": live, "history": dict(zip(D, [h200] * n)), "history_50": dict(zip(D, [h200 - 8] * n)),
            "highs_history": dict(zip(D, [3.0] * n)), "lows_history": dict(zip(D, [6.0] * n)),
            "drawdown_history": {"10": dict(zip(D, [60.0] * n)), "20": dict(zip(D, [40.0] * n))}}


INDEX_BLOCK = block(31.8)
READS = {"indexes": {"INDIA": {"label": "Nifty 500", "entries": [
    {"session": "2026-10-29", "headline": "Older index headline", "breadth": "old"},
    {"session": D[-1], "headline": "Nifty breadth is weak at 31.8% above the 200-day average.", "breadth": "Only 31.8% above.", "return": None}]}}}
facts = lr.build_list_facts("india_invested", "India Invested", PERF, STOCKS, ROWS[:6], QUARTERLY, INDEX_BLOCK, READS, D[-1])
closes = pd.DataFrame({t: pd.Series(PERF["markets"]["india_invested"][t]) for t in STOCKS})
closes.index = pd.to_datetime(closes.index)
want = compute_breadth(closes, since=pd.Timestamp(D[-1]) - pd.DateOffset(months=24))
check(facts["breadth"]["pct_above_200d"]["now"] == round(want["pct_above"], 1)
      and facts["breadth"]["stocks_counted"] == want["total"] == 6
      and facts["breadth"]["index_pct_above_200d_now"] == 31.8 and facts["breadth"]["index_pct_above_50d_now"] == 23.8,
      f"L3: the list's own breadth is the panel's maths over its closes, the index's share beside it ({facts['breadth']['pct_above_200d']['now']})")
check(facts["benchmark"] == "Nifty 500" and facts["return"]["stocks_in_list"] == 6 and set(facts["return"]["windows"]) == {"1m", "3m", "6m", "1y"}
      and facts["session"] == D[-1] and facts["highs_lows"]["session"] == D[-1] and facts["drawdown"] is not None,
      "L3: the return is against the home index (Nifty 500) over four windows")
check(facts["market_read"]["headline"].startswith("Nifty breadth is weak") and facts["market_read"]["session"] == D[-1]
      and facts["market_read"]["breadth"] == "Only 31.8% above." and "return" not in facts["market_read"],
      "L3: the index's latest read up to the session is the market context (empty parts left out)")
check(facts["results"] is not None and facts["stocks"]["stocks_in_list"] == 6, "L3: the latest session carries the results and the holdings")
past = lr.build_list_facts("india_invested", "India Invested", PERF, STOCKS, ROWS[:6], QUARTERLY, INDEX_BLOCK, READS, D[-2])
check(D[-2] == "2026-10-29" and past["session"] == D[-2] and past["results"] is None and past["stocks"] is None
      and past["market_read"]["session"] == "2026-10-29" and past["market_read"]["headline"] == "Older index headline",
      "L3: an earlier session has no results or holdings (only today's are stored) and the index read of its day")
early = lr.build_list_facts("india_invested", "India Invested", PERF, STOCKS, ROWS[:6], QUARTERLY, INDEX_BLOCK, READS, D[-30])
check(early["market_read"] is None, "L3: before any index read there is no market context")
check(lr.build_list_facts("india_invested", "x", {"markets": {}, "benchmarks": {}}, STOCKS, [], None, INDEX_BLOCK, READS, D[-1]) is None,
      "L3: no stored closes for the list -> no facts")
nofacts = lr.build_list_facts("india_invested", "x", PERF, STOCKS, [], None, INDEX_BLOCK, {}, D[-1])
check(nofacts["market_read"] is None and nofacts["results"] is not None or nofacts["market_read"] is None,
      "L3: no index read yet -> no market context")

# --- L4 the prompt ------------------------------------------------------------------------------
prior = [{"session": f"2026-10-{20 + i}", "headline": f"headline {i}", "takeaways": [f"take {i}"], "breadth": f"b{i}"} for i in range(5)]
p = lr.build_list_prompt("India Invested", facts, prior)
check("headline 4" in p and "headline 3" in p and "headline 2" in p and "headline 1" not in p and "headline 0" not in p,
      "L4: only the last three earlier reads reach the prompt")
check(all(w in p for w in ("12-month", "one-day move", "never \"matching\"", "ordinary or unchanged one is null", "Do not repeat a figure")),
      "L4: the prompt asks for the year's range, no trend from one day, no loose 'matching', null for an ordinary chart")
check("Alpha Works Limited" in p and "Nifty breadth is weak" in p and "Never say buy" in p and "key takeaways" in p.lower()
      and "LIST: India Invested" in p and "none yet" in lr.build_list_prompt("x", facts, []),
      "L4: the holdings' names, the market read, the rules; a first read is told so")

# --- L5 the check on the answer -------------------------------------------------------------------
GOOD = {"headline": "India Invested holds up far better than the market, with 83.9% above the 200-day average.",
        "market": "Nifty breadth is weak at 31.8% above the 200-day average, so the list diverges from it.",
        "breadth": None, "highs_lows": None, "drawdown": None, "return": None, "results": None,
        "stocks": "Beta Foods and Delta Pharma sit within 5% of their 52-week lows, and Alpha Works Limited leads the month at 12.0%.",
        "takeaways": ["Leadership is narrow: watch Beta Foods near its 52-week low.", "Results are still being reported."]}
try:
    ok = lr.validate_list_read({**GOOD, "headline": "India Invested holds up far better than the market."}, facts)
    check(ok["takeaways"] and ok["stocks"] and ok["market"], "L5: a read quoting the facts and the market read passes")
except ValueError as e:
    check(False, f"L5: a read quoting the facts and the market read passes ({e})")


def refused(change, f=facts):
    try:
        lr.validate_list_read({**GOOD, "headline": "Fine.", **change}, f)
        return False
    except ValueError:
        return True


check(refused({"market": "The market is at 77.7% above."}), "L5: a number in neither the facts nor the market read is refused")
check(refused({"takeaways": ["only one"]}) and refused({"takeaways": ["a", "b", "c", "d", "e", "f"]}) and refused({"takeaways": "text"}),
      "L5: two to five takeaways, as a list")
check(refused({"takeaways": ["Time to buy the dip.", "fine"]}) and refused({"stocks": "Consider adding Beta Foods."}) is False
      and refused({"takeaways": ["Stocks will rally from here.", "ok"]}), "L5: advice and promised outcomes are refused, in takeaways too")
check(refused({"market": "x"}, {**facts, "market_read": None}) and refused({"results": "Results are strong."}, {**facts, "results": None})
      and refused({"stocks": "Beta Foods."}, {**facts, "stocks": None}), "L5: a part whose facts are absent must be null")
check(refused({"headline": None}), "L5: a read needs a headline")
check(refused({"headline": " ".join(["word"] * 40)}), "L5: a headline of more than about 30 words is refused")

# --- L6 the job ----------------------------------------------------------------------------------
LISTS = {"india_invested": STOCKS, "us_invested": ["UA", "UB", "UC", "UD"]}
SNAP = {"per_market": {"india_invested": ROWS[:6], "us_invested": [{"ticker": "UA", "company_name": "Uni A", "trend": "Uptrend", "perf_1m": 1.0}]}}
BREADTH = {"markets": {"INDIA": block(31.8), "US": block(59.2)}, "status": {}}
captured = []


def stub(client, label, facts_, prior_, **kw):
    captured.append((label, facts_["session"], [e["session"] for e in prior_], facts_["market_read"] and facts_["market_read"]["headline"]))
    return {"headline": f"{label} headline", "market": None, "breadth": None, "highs_lows": None, "drawdown": None, "return": None,
            "results": None, "stocks": None, "takeaways": ["one", "two"]}, "models/x"


real = lr.generate_list_read
lr.generate_list_read = stub
data = {"indexes": {"INDIA": {"label": "Nifty 500", "entries": [{"session": D[-1], "headline": "Nifty read"}]},
                    "US": {"label": "S&P 500", "entries": [{"session": D[-1], "headline": "S&P read"}]}}}
saves, logs = [], []
w, f_ = lr.run_lists(None, BREADTH, PERF, LISTS, data, SNAP, QUARTERLY, labels={"india_invested": "India Invested", "us_invested": "US Invested"},
                     log=logs.append, on_write=lambda: saves.append(1))
check((w, f_) == (2, 0) and set(data["lists"]) == {"india_invested", "us_invested"} and len(saves) == 2
      and data["lists"]["india_invested"]["entries"][0]["benchmark"] == "Nifty 500" and data["lists"]["us_invested"]["entries"][0]["benchmark"] == "S&P 500",
      f"L6: both lists are read once, saved as they go, each against its home index ({w}, {f_})")
check(captured[0][3] == "Nifty read" and captured[1][3] == "S&P read", "L6: each list read sees its home index's read as the market context")
check(not any("Alpha" in l or "AAA" in l or "Beta" in l for l in logs) and all("invested read" in l for l in logs), "L6: the log names the list, never a holding")
captured.clear()
w, f_ = lr.run_lists(None, BREADTH, PERF, LISTS, data, SNAP, QUARTERLY, log=lambda *_: None)
check((w, f_) == (0, 0) and not captured, "L6: a second run writes nothing for a session that has its read")
w, f_ = lr.run_lists(None, BREADTH, PERF, LISTS, data, SNAP, QUARTERLY, force=True, log=lambda *_: None)
check(w == 2 and len(data["lists"]["india_invested"]["entries"]) == 1, "L6: force writes the latest again, replacing it")
d2, captured[:] = {}, []
w, _ = lr.run_lists(None, {"markets": {"INDIA": block(31.8), "US": block(59.2)}, "status": {}}, PERF, LISTS, d2, SNAP, QUARTERLY, backfill=2, log=lambda *_: None)
india = [c for c in captured if c[0] == "India Invested"]
check(w == 6 and [c[1] for c in india] == D[-3:] and india[2][2] == D[-3:-1] and india[0][2] == [],
      f"L6: backfill 2 writes the two earlier sessions first, each seeing the reads before it ({[c[1:3] for c in india]})")
live = {"markets": {"INDIA": block(31.8, live=True), "US": block(59.2)}, "status": {}}
d3 = {}
w, _ = lr.run_lists(None, live, PERF, LISTS, d3, SNAP, QUARTERLY, log=lambda *_: None)
check(w == 1 and set(d3["lists"]) == {"us_invested"}, "L6: a list whose home index reading is still forming waits")
d4 = {}
w, _ = lr.run_lists(None, BREADTH, PERF, {"india_invested": [], "us_invested": LISTS["us_invested"]}, d4, SNAP, QUARTERLY, log=lambda *_: None)
check(w == 1 and set(d4["lists"]) == {"us_invested"}, "L6: a list with no stocks is skipped")
lr.generate_list_read = lambda *a, **k: (None, None)
d5 = {}
w, f_ = lr.run_lists(None, BREADTH, PERF, LISTS, d5, SNAP, QUARTERLY, log=lambda *_: None)
check((w, f_) == (0, 2) and not d5.get("lists"), f"L6: a failed ladder is counted and saves nothing ({w}, {f_})")
lr.generate_list_read = real

# the real ladder: the fake client answers with a good list read
facts_t = lr.build_list_facts("india_invested", "India Invested", PERF, STOCKS, ROWS[:6], QUARTERLY, INDEX_BLOCK, READS, D[-1])


class Resp:
    def __init__(self, text):
        self.text = text


class Client:
    key_names = ["K1", "K2"]

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.models = self

    def generate_content(self, model, contents, config, **extra):
        extra["_key_out"]["key"] = "K1"
        self.calls.append((model, config))
        return Resp(self.replies.pop(0))


good = json.dumps({**GOOD, "headline": "India Invested holds up far better than the market."})
bad = json.dumps({**GOOD, "headline": "India Invested is up 99.9%."})
cl = Client([bad, good])
read, model = lr.generate_list_read(cl, "India Invested", facts_t, [], tiers=ms.tiers("R2", {"ai_models": {}}), budget=4096)
check(read and model == "models/gemini-3.8-flash" and len(cl.calls) == 2 and read["takeaways"],
      "L6: the real ladder: a reply with an invented number is refused and the next try answers")
check(cl.calls[0][1].thinking_config.thinking_budget == 4096, "L6: at the step's thinking budget")

# --- L7 the box -----------------------------------------------------------------------------------
entry = {"session": "2026-10-30", "written": "2026-10-31 11:20 UTC", "model": "models/gemini-3.8-flash", "benchmark": "Nifty 500",
         "headline": "Holds up $5 better", "market": "The market is weak.", "breadth": None, "stocks": "Beta Foods near its low.",
         "results": "Profit growth is 30.0%.", "takeaways": ["First point.", "Second point."]}
box = mr.box_markdown(entry, "India Invested", "7:15 AM ET")
check(box.index("The market.") < box.index("Quarterly results.") < box.index("Holdings.") < box.index("Key takeaways")
      and "- First point.\n- Second point." in box and "Holds up \\$5 better" in box and "against Nifty 500" in box
      and "Breadth." not in box and "gemini-3.8-flash" in box,
      "L7: a list read: the market line, its parts in order, takeaways as bullets, the benchmark in the footer, $ escaped")
idx = mr.box_markdown({"session": "2026-10-30", "headline": "H", "breadth": "B", "drawdown": "D"}, "Nifty 500", "x")
check("**Breadth.** B" in idx and "Stocks far below their highs." in idx and "Key takeaways" not in idx and "The market." not in idx,
      "L7: an index read looks as before")

# --- L8 the library copy -------------------------------------------------------------------------
data_pub = {"generated_at": "2026-10-31T04:36:40Z",
            "indexes": {"INDIA": {"label": "Nifty 500", "entries": [{"session": "2026-10-30", "headline": "h"}]}, "US": {"label": "S&P 500", "entries": []}},
            "lists": {"india_invested": {"label": "India Invested", "entries": [{"session": "2026-10-30", "headline": "l", "takeaways": ["t"]}, "junk"]},
                      "us_invested": "junk"}}
rp = pm.reads_payload(data_pub)
check(set(rp["indexes"]) == {"INDIA"} and set(rp["lists"]) == {"india_invested"} and rp["lists"]["india_invested"]["entries"][0]["takeaways"] == ["t"],
      "L8: the lists' reads are published beside the indexes', empty or malformed ones left out")
check(pm.reads_payload({"lists": {"india_invested": {"entries": [{"session": "s"}]}}}) is not None
      and pm.reads_payload({"lists": {}}) is None, "L8: a file with only list reads still publishes")

import market_lists as mls

STORED = {"generated_at": "2026-10-31T04:36:40Z", "indexes": rp["indexes"], "lists": {
    "in_hold": {"label": "India Invested", "entries": [
        {"session": "2026-10-29", "headline": "older", "benchmark": "Nifty 500"},
        {"session": "2026-10-30", "headline": "Holds up", "market": "Weak market.", "stocks": "Beta Foods near its low.",
         "takeaways": ["First.", "Second."], "benchmark": "Nifty 500", "model": "models/gemini-3.8-flash", "breadth": None}]}}}


class DB:
    def market_read(self):
        return STORED

    def watchlists(self):
        return [{"name": "in_hold", "label": "India Invested", "kind": "watchlist", "benchmark": "^CRSLDX", "stocks": 31},
                {"name": "all_hold", "label": "All Invested", "kind": "group", "benchmark": None, "stocks": 45}]


db = DB()
body = mls.market_read(db, "India Invested", history=2)
rd = body["invested_lists"][0]["reads"]
check(body["found"] and not body["indexes"] and body["invested_lists"][0]["list"] == "India Invested"
      and [r["session"] for r in rd] == ["2026-10-30", "2026-10-29"] and rd[0]["key_takeaways"] == ["First.", "Second."]
      and rd[0]["against"] == "Nifty 500" and rd[0]["stocks"].startswith("Beta Foods") and "breadth" not in rd[0],
      "L8: a list by name: its latest reads, key takeaways, the home index it is set against")
body = mls.market_read(db, "my india holdings")
check(body["found"] and body["invested_lists"], "L8: a list by loose name")
body = mls.market_read(db)
check(len(body["indexes"]) == 1 and len(body["invested_lists"]) == 1 and body["indexes"][0]["index"] == "Nifty 500",
      "L8: no name gives every index and list that has a read")
body = mls.market_read(db, "All Invested")
check(body["found"] is False and sorted(body["available"]) == ["India Invested", "Nifty 500"],
      "L8: a list with no read is found false, naming what has one")
check("key_takeaways" in mls.market_read(db, "India Invested")["note"] and "invested_lists" in mls.market_read(db),
      "L8: the reply explains the list reads")

print("FAILURES:", fails)
sys.exit(fails)
