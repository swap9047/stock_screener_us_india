"""The News tab's market reads (2026-10-31), offline: invented breadth, prices and a fake
Gemini client; nothing reads the real JSON.

  M1  facts: each chart's numbers match an independent calculation (zone, change,
      last cross of 50, "lowest since", streaks, the equal-weight curve and its
      windows), and a chart with too little history has no facts;
  M1b the context the weak first reads lacked (2026-10-31): "at the year's extreme" flags that are true only when
      it is, and whether breadth and the index agree or diverge over 5 and 20 sessions;
  M2  the number check: a read may quote what the facts hold (rounded as given,
      dates' digits, the charts' own lines) and nothing else; advice, markdown
      lengths, a chart without facts and an empty read are refused;
  M3  the prompt carries the facts and only the last three earlier reads, no ticker;
  M4  the ladder: 3.8-flash four times each on another key, then 3.6-flash, then
      3.5-flash-lite, every rung at a 4096 thinking budget in JSON mode; a read that
      fails the check moves on like an error;
  M5  the job: only Nifty 500 and the S&P 500 are read (the other four indexes never),
      a reading still forming waits, a final one is written once (a second run writes
      nothing), force and backfill, earlier reads reach the prompt, each read is saved as
      it is written, a failed ladder saves nothing for that index;
  M6  storage: one read a session, the newest ten kept, a torn file reads as empty,
      the file is stamped for the freshness guard;
  M7  the box text: dollar signs escaped, an empty chart skipped, no read yet says when;
  M8  market-read.yml: 7 and 17 ET on weekdays at :15, waits for a running breadth
      refresh before loading data, redacted, commits only market_read.json, and the
      file is pulled by the app but never pushed by it.
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO)

import github_sync
import index_registry
import llm_util
import market_read as mr

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


llm_util.RETRY_BACKOFF_SECONDS = 0

# --- fixtures ------------------------------------------------------------------------------
DAYS = pd.bdate_range(end="2026-10-30", periods=300)
D = [d.strftime("%Y-%m-%d") for d in DAYS]
N = len(D)

# 200-day breadth: flat 60, then 30 sessions falling 1.5 a day to 15.
H200 = [60.0] * (N - 30) + [round(60 - 1.5 * k, 1) for k in range(1, 31)]
H50 = [v - 8 for v in H200]
HIGHS = [5.0] * N
LOWS = [2.0] * (N - 10) + [12.0] * 10
DD20 = [30.0] * (N - 30) + [round(30 + k, 1) for k in range(1, 31)]


def block(live=False, partial=False, drop=0):
    n = N - drop
    return {"as_of": "2026-10-30 12:00", "total": 480, "live": live, "partial": partial,
            "history": dict(zip(D[:n], H200[:n])), "history_50": dict(zip(D[:n], H50[:n])),
            "highs_history": dict(zip(D[:n], HIGHS[:n])), "lows_history": dict(zip(D[:n], LOWS[:n])),
            "drawdown_history": {"10": dict(zip(D[:n], [v + 20 for v in DD20[:n]])), "20": dict(zip(D[:n], DD20[:n]))}}


def prices(daily):
    return {d: round(100 * (1 + daily) ** i, 4) for i, d in enumerate(D)}


PERF = {"markets": {"india_invested": {"AAA.NS": prices(0.001), "BBB.NS": prices(-0.001)},
                    "us_invested": {"CCC": prices(0.002), "DDD": prices(0.0)}},
        "benchmarks": {b: prices(0.0005) for b in index_registry.BENCHMARK_TICKERS}}
LISTS = {"india_invested": ["AAA.NS", "BBB.NS"], "us_invested": ["CCC", "DDD"]}
IX = index_registry.BY_KEY["INDIA"]
US = index_registry.BY_KEY["US"]

# --- M1 facts ------------------------------------------------------------------------------
f = mr.build_facts(IX, block(), PERF, LISTS["india_invested"])
b = f["breadth"]["pct_above_200d"]
check(f["session"] == D[-1] == f["breadth"]["session"], "M1: the session is the block's latest day")
check(b["now"] == 15.0 and b["zone"] == "below 20" and b["sessions_in_zone"] == 4,
      f"M1: 15% sits in the below-20 zone for the 4 sessions under 20 ({b['now']}, {b['zone']}, {b['sessions_in_zone']})")
check(b["change_5d_points"] == round(15.0 - H200[-6], 1) == -7.5 and b["change_20d_points"] == round(15.0 - H200[-21], 1),
      f"M1: changes are points against 5 and 20 sessions ago ({b['change_5d_points']}, {b['change_20d_points']})")
cross = b["last_cross_of_50"]
want_cross = max(i for i in range(1, N) if (H200[i] >= 50) != (H200[i - 1] >= 50))
check(cross == {"date": D[want_cross], "direction": "down", "sessions_ago": N - 1 - want_cross},
      f"M1: it last crossed 50 down on {D[want_cross]} ({cross})")
check(b["lowest_since"] == "the whole stored history" and b["highest_since"] is None
      and b["1y_low"] == {"value": 15.0, "date": D[-1]},
      f"M1: lowest in the whole history; no 'highest since' for a one-session gap ({b['lowest_since']}, {b['highest_since']})")
check(f["breadth"]["pct_above_50d"]["now"] == 7.0 and f["breadth"]["gap_50d_minus_200d_points"] == -8.0
      and f["breadth"]["stocks_counted"] == 480, "M1: the 50-day line and the gap to the 200-day")
h = f["highs_lows"]
check(h["highs_pct"] == 5.0 and h["lows_pct"] == 12.0 and h["net_highs_minus_lows_points"] == -7.0
      and h["sessions_in_a_row_lows_exceed_highs"] == 10 and h["sessions_in_a_row_highs_exceed_lows"] == 0
      and h["1y_most_lows"] == {"value": 12.0, "date": D[-10]},
      f"M1: highs/lows: net, the 10-session streak, the year's worst day ({h['net_highs_minus_lows_points']}, {h['sessions_in_a_row_lows_exceed_highs']})")
dd = f["drawdown"]["share_of_stocks"]
check(dd["down_20pct_or_more"]["now"] == 60.0 and dd["down_10pct_or_more"]["now"] == 80.0
      and dd["down_20pct_or_more"]["change_5d_points"] == 5.0 and f["drawdown"]["session"] == D[-1],
      f"M1: drawdown shares per level and their change ({dd['down_20pct_or_more']['now']})")

# The list: equal weight, rebalanced daily, against the index; an independent calculation.
pa, pb = prices(0.001), prices(-0.001)
va, vb = [pa[d] for d in D], [pb[d] for d in D]
curve = [100.0]
for i in range(1, N):
    curve.append(curve[-1] * (1 + ((va[i] / va[i - 1] - 1) + (vb[i] / vb[i - 1] - 1)) / 2))
bench = [prices(0.0005)[d] for d in D]
r = f["return"]
w = r["windows"]["3m"]
want_list = round((curve[-1] / curve[-1 - 63] - 1) * 100, 1)
want_idx = round((bench[-1] / bench[-1 - 63] - 1) * 100, 1)
check(w["list_pct"] == want_list and w["index_pct"] == want_idx and w["list_minus_index_points"] == round(want_list - want_idx, 1)
      and r["stocks_in_list"] == 2 and set(r["windows"]) == {"1m", "3m", "6m", "1y"},
      f"M1: 3-month list {w['list_pct']}% vs index {w['index_pct']}% match an independent curve ({want_list}, {want_idx})")
check(r["list_below_1y_peak_pct"] == round((curve[-1] / max(curve[-252:]) - 1) * 100, 1)
      and f["index_price"]["change_20d_pct"] == round((bench[-1] / bench[-21] - 1) * 100, 1),
      "M1: drawdown from the 1-year peak and the index's own change")
us = mr.build_facts(US, block(), PERF, LISTS["us_invested"])
check(us["return"]["stocks_in_list"] == 2 and us["return"]["windows"]["1m"]["list_pct"]
      != f["return"]["windows"]["1m"]["list_pct"], "M1: a US index reads the US list, an Indian one the India list")
short = mr.build_facts(IX, {"history": dict(list(block()["history"].items())[:10])}, PERF, LISTS["india_invested"])
check(short["breadth"] is None and short["highs_lows"] is None and short["drawdown"] is None,
      "M1: ten days of history is too little for any chart's facts")
past = mr.build_facts(IX, block(), PERF, LISTS["india_invested"], asof=D[-31])
check(past["session"] == D[-31] and past["breadth"]["pct_above_200d"]["now"] == 60.0,
      "M1: as of an earlier session the facts stop there (backfill)")
none = mr.build_facts(IX, block(), {"markets": {}, "benchmarks": {}}, LISTS["india_invested"])
check(none["return"] is None and none["index_price"] is None and none["breadth"] is not None,
      "M1: no stored prices -> no return facts, the other charts unaffected")

# --- M1b context facts ----------------------------------------------------------------------------
check(b["at_1y_low"] is True and b["at_1y_high"] is False and f["breadth"]["pct_above_50d"]["at_1y_low"] is True,
      "M1b: today is the year's low for the 200-day breadth (15 against a year at 60), so the flag is true, and the high flag false")
check(h["lows_at_1y_most"] is True and f["drawdown"]["share_of_stocks"]["down_20pct_or_more"]["at_1y_high"] is True,
      "M1b: lows at their 12-month most, and the 20% drawdown share at its 12-month high")
# By hand: the index (+0.05% a session) is +0.25% over 5 sessions (flat, under the 0.5% band) and +1.0% over 20 (up);
# 200-day breadth is -7.5 points over 5 (down) and -30 over 20 (down).
check(f["breadth_vs_index"] == {"last_5_sessions": "index flat, breadth down: mixed",
                                "last_20_sessions": "index up, breadth down: diverging"},
      f"M1b: breadth against the index, named: {f['breadth_vs_index']}")
check(mr.breadth_vs_index({"change_5d_pct": -2.0}, {"pct_above_200d": {"change_5d_points": -6.0}}) == {"last_5_sessions": "index down, breadth down: confirming"}
      and mr.breadth_vs_index({"change_5d_pct": 3.0}, {"pct_above_200d": {"change_5d_points": 1.0}}) == {"last_5_sessions": "index up, breadth flat: mixed"}
      and mr.breadth_vs_index({"change_20d_pct": -1.0}, {"pct_above_200d": {"change_20d_points": 9.0}}) == {"last_20_sessions": "index down, breadth up: diverging"}
      and mr.breadth_vs_index(None, None) is None,
      "M1b: confirming, mixed and diverging, and nothing when either side is missing")

# --- M2 the number check -------------------------------------------------------------------
facts = f
ok_read = {"headline": "Breadth fell to 15.0%, washed out; lows at 12% since Oct 16.",
           "breadth": "Only 15% of stocks are above the 200-day average, down 7.5 points in 5 sessions.",
           "highs_lows": None, "drawdown": None, "return": "The list trails by about -0.4 points over 3 months."}
try:
    out = mr.validate_read(ok_read, facts)
    check(out["headline"] and out["highs_lows"] is None, "M2: a read quoting the facts passes")
except ValueError as e:
    check(False, f"M2: a read quoting the facts passes ({e})")


def refused(read, why=""):
    try:
        mr.validate_read({**ok_read, **read}, facts)
        return False
    except ValueError:
        return True


check(refused({"breadth": "Breadth is at 23.4% now."}), "M2: a number the facts do not hold is refused")
check(not refused({"breadth": "Breadth is 15% (15.0 rounded)."}) and not refused({"breadth": "It last crossed the 50% line in September, 200-day."}),
      "M2: a rounded fact and the charts' own lines (50, 200) are fine")
check(refused({"headline": "Time to buy the dip."}) and refused({"breadth": "A good place to sell into strength."})
      and refused({"breadth": "Investors should consider adding here."}) and refused({"breadth": "Stocks will rally from here."})
      and refused({"headline": "Set a stop-loss."}) and refused({"headline": "We recommend patience."}),
      "M2: advice and promised outcomes are refused")
check(not refused({"breadth": "Selling pressure is easing in the short-term; buyers are returning (15%)."})
      and not refused({"headline": "Short covering can lift a washed-out market."}),
      "M2: ordinary market language (selling pressure, short-term, buyers) is not advice")
check(refused({"breadth": "x" * 800}) and refused({"breadth": 12}), "M2: too long or not text is refused")
check(refused({"breadth": "Highs remain below the 1y most high of 15%."}) and refused({"highs_lows": "Lows hit the 1-year most lows level (12%)."})
      and refused({"breadth": "breadth_vs_index says diverging."}) and not refused({"breadth": "Breadth is 15% against a 12-month high of 60%."}),
      "M2: the facts' own field names (\"1y most high\", breadth_vs_index) must not appear in the prose")
check(refused({"headline": " ".join(["word"] * 40)}) and not refused({"headline": " ".join(["word"] * 28)}),
      "M2: a headline of more than about 30 words is refused, one of 28 is fine")
no_dd = dict(facts, drawdown=None)
try:
    mr.validate_read({"headline": "h", "drawdown": "Damage is spreading."}, no_dd)
    check(False, "M2: a chart without facts must be null")
except ValueError:
    check(True, "M2: a chart without facts must be null")
check(refused({"headline": "", "breadth": None, "highs_lows": None, "return": None, "drawdown": None}),
      "M2: an empty read is refused")
check(mr.unknown_numbers("up 12.5%, 3 days", {"a": 12.54}) == [],
      "M2: a one-decimal number matches the fact that rounds to it")
check(mr.unknown_numbers("up 12.9%", {"a": 12.54}) == ["12.9"], "M2: one that does not match is returned")

# --- M3 the prompt -------------------------------------------------------------------------
prior = [{"session": f"2026-10-{20 + i}", "headline": f"headline {i}", "breadth": f"breadth {i}", "highs_lows": None,
          "drawdown": None, "return": None, "model": "m", "written": "w"} for i in range(5)]
p = mr.build_prompt("Nifty 500", f, prior)
check("headline 4" in p and "headline 3" in p and "headline 2" in p and "headline 1" not in p and "headline 0" not in p,
      "M3: only the last three earlier reads reach the prompt")
check('"now": 15.0' in p and "INDEX: Nifty 500" in p and "AAA" not in p and "BBB" not in p
      and "washed out" in p and "Never say buy" in p, "M3: the facts, the rules, and no ticker")
check("none yet" in mr.build_prompt("Nifty 500", f, []), "M3: a first read is told it is the first")
check(all(w in p for w in ("breadth_vs_index", "one-day move", "12-month", "at_1y_high", "never \"cycle\"", "ordinary or unchanged one is null",
                           "Do not repeat a figure")) and '"breadth_vs_index"' in p,
      "M3: the prompt asks for the year's range as context, the breadth-vs-index verdict, no trend from one day, null for an ordinary chart")

# --- M4 the ladder -------------------------------------------------------------------------


class Err(Exception):
    def __init__(self, code, msg):
        super().__init__(f"{code} {msg}")
        self.code = code


class Resp:
    def __init__(self, text):
        self.text = text


class FakeClient:
    """Fails per `plan` (an exception, a text, or a callable of the prompt), recording each call."""

    key_names = ["K1", "K2", "K3"]

    def __init__(self, plan):
        self.plan, self.calls = list(plan), []
        self.models = self

    def generate_content(self, model, contents, config, **extra):
        avoid = extra.get("_avoid_key")
        key = next(k for k in self.key_names if k != avoid)
        extra["_key_out"]["key"] = key
        self.calls.append({"model": model, "key": key, "avoid": avoid, "config": config, "prompt": contents})
        step = self.plan.pop(0) if self.plan else Err(500, "out of plan")
        if isinstance(step, Exception):
            raise step
        return Resp(step(contents) if callable(step) else step)


GOOD = json.dumps({"headline": "Breadth is washed out at 15.0%.", "breadth": None, "highs_lows": None,
                   "drawdown": None, "return": None})
BAD_NUMBER = json.dumps({"headline": "Breadth is 33.3%.", "breadth": None, "highs_lows": None, "drawdown": None, "return": None})

c = FakeClient([Err(503, "overloaded")] * 4 + [BAD_NUMBER, GOOD])
read, model = mr.generate_read(c, "Nifty 500", f, [])
seq = [x["model"] for x in c.calls]
check(seq[:4] == ["models/gemini-3.8-flash"] * 4, f"M4: four 3.8-flash attempts first ({seq})")
check(model == "models/gemini-3.5-flash-lite" and read["headline"].startswith("Breadth is washed out"),
      f"M4: 3.6-flash's reply quoting a number the facts lack is refused, flash-lite's good one is used ({model})")
check(seq[4] == "models/gemini-3.6-flash" and len(seq) == 6, f"M4: then 3.6-flash, then 3.5-flash-lite ({seq})")
check(all(c.calls[i]["avoid"] == c.calls[i - 1]["key"] and c.calls[i]["key"] != c.calls[i - 1]["key"] for i in range(1, 4)),
      f"M4: each retry is on a different key ({[x['key'] for x in c.calls[:4]]})")
cfg = c.calls[0]["config"]
check(cfg.thinking_config.thinking_budget == 4096 and cfg.response_mime_type == "application/json"
      and all(x["config"].thinking_config.thinking_budget == 4096 for x in c.calls),
      "M4: every rung asks for JSON at a 4096 thinking budget")
c = FakeClient([GOOD])
read, model = mr.generate_read(c, "Nifty 500", f, [])
check(model == "models/gemini-3.8-flash" and len(c.calls) == 1, "M4: a good first reply costs one call")
c = FakeClient([Err(503, "overloaded")] * 6)
check(mr.generate_read(c, "Nifty 500", f, []) == (None, None) and len(c.calls) == 6,
      "M4: every rung failing gives (None, None) after six calls")
c = FakeClient(["not json"] * 6)
check(mr.generate_read(c, "Nifty 500", f, []) == (None, None), "M4: unparseable replies are failures too")

# --- M5 the job ----------------------------------------------------------------------------
written_calls = []


def stub_generate(client, label, facts, prior, **kw):
    written_calls.append((label, facts["session"], [e["session"] for e in prior]))
    return {"headline": f"{label} headline", "breadth": "b", "highs_lows": None, "drawdown": None, "return": None}, "models/x"


real_generate = mr.generate_read
mr.generate_read = stub_generate
BREADTH = {"markets": {"INDIA": block(), "US": block(), "NIFTY50": block(), "NASDAQ100": block()}, "status": {}}
data = {}
logs = []
saves = []
res = mr.run(None, BREADTH, PERF, LISTS, data, log=logs.append, on_write=lambda: saves.append(len(mr.entries_for(data, "INDIA"))))
check(res == (2, 0, 0) and {k for k in data["indexes"]} == {"INDIA", "US"}
      and data["indexes"]["INDIA"]["entries"][0]["session"] == D[-1],
      f"M5: Nifty 500 and the S&P 500 are written; Nifty 50 and Nasdaq 100, though their breadth is stored, are not ({res})")
check(mr.READ_INDEX_KEYS == ("INDIA", "US") and not any("Nifty 50 2" in l or "Nifty 50:" in l or "Nasdaq" in l for l in logs)
      and not any("AAA" in l for l in logs), "M5: the log names only the two indexes, never a ticker")
check(saves == [1, 1], f"M5: on_write runs after each read, so a run cut short keeps what it wrote ({saves})")
live = {"markets": {"INDIA": block(), "US": block(live=True)}, "status": {"US": "live"}}
d_live = {}
res = mr.run(None, live, PERF, LISTS, d_live, log=logs.append)
check(res == (1, 1, 0) and set(d_live["indexes"]) == {"INDIA"} and any("S&P 500" in l and "not final" in l for l in logs),
      f"M5: a reading still forming waits and is counted, the other index is written ({res})")
written_calls.clear()
res = mr.run(None, BREADTH, PERF, LISTS, data, log=lambda *_: None)
check(res == (0, 0, 0) and not written_calls, "M5: a second run writes nothing for a session that has its read")
res = mr.run(None, BREADTH, PERF, LISTS, data, force=True, log=lambda *_: None)
check(res[0] == 2 and len(data["indexes"]["INDIA"]["entries"]) == 1, "M5: force writes the latest again, replacing it")
data2, written_calls[:] = {}, []
res = mr.run(None, BREADTH, PERF, LISTS, data2, backfill=2, log=lambda *_: None)
india = [x for x in written_calls if x[0] == "Nifty 500"]
check(res[0] == 6 and [x[1] for x in india] == D[-3:] and india[2][2] == D[-3:-1] and india[0][2] == [],
      f"M5: backfill 2 writes the two earlier sessions first, and each read sees the ones before it ({india})")
mr.generate_read = lambda *a, **k: (None, None)
data3 = {}
res = mr.run(None, BREADTH, PERF, LISTS, data3, log=lambda *_: None)
check(res == (0, 0, 2) and not data3.get("indexes"), f"M5: a failed ladder is counted and nothing is saved for it ({res})")
mr.generate_read = real_generate

# --- M6 storage ----------------------------------------------------------------------------
store = {}
for i in range(12):
    mr.add_entry(store, "INDIA", "Nifty 500", {"session": f"2026-10-{i + 1:02d}", "headline": str(i)})
mr.add_entry(store, "INDIA", "Nifty 500", {"session": "2026-10-12", "headline": "again"})
es = mr.entries_for(store, "INDIA")
check(len(es) == mr.KEEP_ENTRIES == 10 and es[0]["session"] == "2026-10-03" and es[-1]["headline"] == "again"
      and mr.latest_entry(store, "INDIA")["headline"] == "again" and mr.latest_entry(store, "US") is None,
      "M6: one read a session (replaced), the newest ten kept, oldest first")
tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "market_read.json")
mr.save_market_read(store, path)
back = mr.load_market_read(path)
check(back["indexes"]["INDIA"]["entries"][-1]["headline"] == "again" and re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", back["generated_at"]),
      "M6: saved atomically and stamped generated_at (the guard against a backwards pull)")
open(path, "w").write('{"indexes": {')
check(mr.load_market_read(path) == {} and mr.load_market_read(os.path.join(tmp, "none.json")) == {},
      "M6: a torn or missing file reads as empty")

# --- M7 the box ----------------------------------------------------------------------------
box = mr.box_markdown({"session": "2026-10-30", "written": "2026-10-31 11:20 UTC", "model": "models/gemini-3.8-flash",
                       "headline": "Up $5 here", "breadth": "Broad", "highs_lows": None, "drawdown": "Healing", "return": None},
                      "Nifty 500", "7:15 AM ET")
check("Up \\$5 here" in box and "**Breadth.** Broad" in box and "52-week highs" not in box and "Healing" in box
      and box.index("Breadth.") < box.index("Stocks far below") and "session of 2026-10-30" in box
      and "written 2026-10-31" in box and "gemini-3.8-flash" in box and "models/" not in box,
      "M7: dollar signs escaped, empty charts skipped, charts in drawing order, the footer names session and model")
check("7:15 AM ET" in mr.box_markdown(None, "Nifty 500", "7:15 AM ET")
      and "Nothing stood out" in mr.box_markdown({"session": "2026-10-30"}, "Nifty 500", "x"),
      "M7: no read yet says when it comes; a read with nothing says so")

# --- M8 the workflow -----------------------------------------------------------------------
text = open(f"{REPO}/.github/workflows/market-read.yml").read()
wf = yaml.safe_load(text)
on = wf.get(True) or wf.get("on")
check(on["schedule"] == [{"cron": "15 * * * *"}] and "backfill" in on["workflow_dispatch"]["inputs"]
      and "force" in on["workflow_dispatch"]["inputs"], "M8: wakes hourly at :15; backfill and force are dispatch inputs")
gate = next(s for s in wf["jobs"]["gate"]["steps"] if s.get("id") == "slot")["with"]
check(gate["slots"] == "7,17" and gate["days"] == "MON,TUE,WED,THU,FRI" and gate["grace-hours"] == "23"
      and gate["work-job"] == "write_reads"
      and wf["jobs"]["gate"]["steps"][-1]["with"]["workflow-file"] == "market-read.yml", "M8: slots 7 and 17 ET on weekdays, a missed one still done up to 23 hours later")
steps = wf["jobs"]["write_reads"]["steps"]
uses = [s.get("uses", "") for s in steps]
wait = next(i for i, s in enumerate(steps) if "market-breadth.yml" in s.get("run", ""))
load = uses.index("./.github/actions/load-data")
py = next(i for i, s in enumerate(steps) if "refresh_market_read.py" in s.get("run", ""))
check(wait < load < py and "python" not in steps[wait]["run"], "M8: waits for a breadth refresh before the data is loaded")
check(steps[py].get("shell") == "bash" and "| python -u log_redact.py" in steps[py]["run"], "M8: redacted, bash (pipefail)")
ci = next(i for i, st in enumerate(steps) if st.get("uses") == "./.github/actions/commit-data")
com = steps[ci]
check(com["with"]["files"] == "market_read.json" and com.get("if") == "always()",
      "M8: commits only market_read.json, even after a failure")
pub = steps[-1]
check(steps.index(pub) > ci and "publish_market.py reads" in pub["run"] and pub.get("continue-on-error") is True and pub.get("if") == "always()"
      and pub.get("shell") == "bash" and "| python -u log_redact.py" in pub["run"] and pub["env"] == {"MARKET_DB_URL": "${{ secrets.MARKET_DB_URL }}"},
      "M8: after the commit the reads are published to the research library, redacted, never failing the run")
keys = re.findall(r"(GEMINI_API_KEY\w*):", steps[py].get("env") and text[text.index("Write the market reads"):])
check({"GEMINI_API_KEY", "GEMINI_API_KEY_BACKUP", "GEMINI_API_KEY_BACKUP_B", "GEMINI_API_KEY_BACKUP_B2",
       "GEMINI_API_KEY_BACKUP_B3", "GEMINI_API_KEY_BACKUP_B4"} <= set(keys), "M8: every Gemini key secret is passed to the step")
check("market_read.json" in github_sync.PULLABLE_FILES and "market_read.json" not in [n for n, _ in github_sync.SYNCABLE_FILES]
      and "market_read.json" in github_sync.DATA_FILES, "M8: the app pulls market_read.json and never pushes it")

print("FAILURES:", fails)
sys.exit(fails)
