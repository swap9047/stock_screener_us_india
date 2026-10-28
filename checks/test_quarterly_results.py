"""Quarterly results (2026-10-28): the maths behind the News tab's fourth chart
row, and the job that stores the statements.

  Q1  calendar quarters: a period is filed under the nearest quarter end (an
      Indian Q2 FY27 and a US Q3 are both Sep '26; a quarter ending 1 Nov is
      September's, one ending 31 Jan December's); shift and label.
  Q2  normalize takes Yahoo's rows with fallbacks, drops NaN, keeps the later of
      two periods in one bucket; merge lays a new fetch over the stored quarters
      without a blank erasing a figure, and keeps the last 12.
  Q3  is_due: never fetched, results expected this season, or 14 days old.
  Q4  summarize: the median of the companies' own growth rates, worked out here
      independently; a loss or zero base is left out; a company without
      operating income has no margin; YoY (the default) needs the quarter a year
      before; a bonus/split-sized jump in the share count drops that EPS growth;
      a quarter still being reported is flagged; the last 6 quarters.
  Q5  the job: fetches only what is due, keeps what a failed fetch or a failed
      member list would lose, stops on a time budget (exit 0) and when Yahoo
      refuses most of the run (exit 1, after saving).
  Q6  the workflow and the app's pull list.

Offline: invented tickers, stubbed fetches, no data files.
"""

import sys
from datetime import date
from pathlib import Path
from statistics import median

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import quarterly_results as qr
import refresh_quarterly_results as rqr

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- Q1 ------------------------------------------------------------------------------
check(qr.quarter_end("2026-09-30") == "2026-09-30" and qr.quarter_end("2026-11-01") == "2026-09-30"
      and qr.quarter_end("2027-01-31") == "2026-12-31" and qr.quarter_end("2026-06-28") == "2026-06-30"
      and qr.quarter_end("2026-02-28") == "2026-03-31" and qr.quarter_end(date(2026, 12, 27)) == "2026-12-31",
      "Q1: each period goes to its nearest calendar quarter end")
check(qr.shift("2026-09-30", 1) == "2026-12-31" and qr.shift("2026-03-31", -1) == "2025-12-31"
      and qr.shift("2026-09-30", -4) == "2025-09-30", "Q1: shift moves by whole quarters, across years")
check(qr.label("2026-09-30") == "Sep '26" and qr.label("2027-03-31") == "Mar '27", "Q1: labels read Sep '26")

# --- Q2 ------------------------------------------------------------------------------
nan = float("nan")
got = qr.normalize({
    "2026-09-30": {"Total Revenue": 120.0, "Operating Income": 24.0, "Net Income": 10.0, "Diluted EPS": nan, "Basic EPS": 1.1},
    "2026-06-30": {"Operating Revenue": 100.0, "Net Income Common Stockholders": 8.0},
    "2026-03-31": {"Total Revenue": nan, "Net Income": None},
})
check(got == {"2026-09-30": {"rev": 120.0, "op": 24.0, "ni": 10.0, "eps": 1.1, "sh": None},
              "2026-06-30": {"rev": 100.0, "op": None, "ni": 8.0, "eps": None, "sh": None}},
      f"Q2: fallbacks fill a missing row, NaN is blank, an all-blank period is dropped ({got})")
two = qr.normalize({"2026-09-27": {"Total Revenue": 1.0}, "2026-10-04": {"Total Revenue": 2.0}})
check(two == {"2026-09-30": {"rev": 2.0, "op": None, "ni": None, "eps": None, "sh": None}},
      "Q2: two periods in one quarter keep the later")
sh = qr.normalize({"2026-09-30": {"Net Income": 5.0, "Diluted Average Shares": 1e6},
                   "2026-06-30": {"Basic Average Shares": 9e5}})
check(sh == {"2026-09-30": {"rev": None, "op": None, "ni": 5.0, "eps": None, "sh": 1e6}},
      "Q2: the diluted share count is stored (Basic as the fallback); shares alone are not a quarter's results")
old = {"2026-03-31": {"rev": 90.0, "op": 9.0, "ni": 5.0, "eps": 0.5}, "2026-06-30": {"rev": 99.0, "op": 9.9, "ni": 7.0, "eps": 0.7}}
new = {"2026-06-30": {"rev": 100.0, "op": None, "ni": 8.0, "eps": None}, "2026-09-30": {"rev": 120.0, "op": 24.0, "ni": 10.0, "eps": 1.1}}
m = qr.merge(old, new)
check(m["2026-06-30"] == {"rev": 100.0, "op": 9.9, "ni": 8.0, "eps": 0.7} and m["2026-03-31"] == old["2026-03-31"]
      and "2026-09-30" in m,
      "Q2: a newer fetch wins per figure, a blank never erases a stored one, older quarters stay")
many = {qr.shift("2026-09-30", -i): {"rev": float(i)} for i in range(20)}
check(sorted(qr.merge(many, {})) == sorted(many)[-12:], "Q2: the last 12 quarters are kept")

# --- Q3 ------------------------------------------------------------------------------
T = date(2026, 10, 28)
check(qr.is_due(None, T) and qr.is_due({}, T), "Q3: never fetched -> due")
check(not qr.is_due({"fetched": "2026-10-28", "q": {}}, T), "Q3: fetched today -> not due")
check(qr.is_due({"fetched": "2026-10-27", "q": {"2026-06-30": {}}}, T),
      "Q3: the Sep quarter is expected (ended 28 days ago) and not stored -> due daily")
check(not qr.is_due({"fetched": "2026-10-27", "q": {"2026-09-30": {}}}, T),
      "Q3: the Sep quarter is stored -> not due until 14 days pass")
check(qr.is_due({"fetched": "2026-10-14", "q": {"2026-09-30": {}}}, T), "Q3: 14 days since the last fetch -> due")
check(not qr.is_due({"fetched": "2026-10-27", "q": {"2026-03-31": {}}}, T),
      "Q3: a company far behind (past the results wait) drops back to the 14-day cadence")
check(not qr.is_due({"fetched": "2026-10-27", "q": {}}, T), "Q3: a company with no statements (a fund): 14-day cadence")

# --- Q4 ------------------------------------------------------------------------------
def rec(rev, op, ni, eps):
    return {"rev": rev, "op": op, "ni": ni, "eps": eps}


Q = ["2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]
data = {
    "ACME": {"q": {Q[4]: rec(100, 20, 10, 1.0), Q[5]: rec(110, 23, 12, 1.2)}},
    "BOLT": {"q": {Q[4]: rec(50, 5, 4, 0.4), Q[5]: rec(45, 4, 5, 0.5)}},
    "CORE": {"q": {Q[4]: rec(200, None, -10, -1.0), Q[5]: rec(260, None, 20, 2.0)}},   # a bank; a loss base
    "DUSK": {"q": {Q[4]: rec(80, 8, 6, 0.6)}},                                         # not reported Sep yet
    "ZED.NS": {"q": {}},                                                               # a fund
}
rows, companies = qr.summarize({**data, "ZED.NS": {"q": {}}}, ["ACME", "BOLT", "CORE", "DUSK", "ZED.NS"], "qoq")
last = rows[-1]
want_rev = median([(110 / 100 - 1) * 100, (45 / 50 - 1) * 100, (260 / 200 - 1) * 100])
want_ni = median([(12 / 10 - 1) * 100, (5 / 4 - 1) * 100])          # CORE's loss base left out
want_mg = median([23 / 110 * 100, 4 / 45 * 100])                   # CORE has no operating income
check(companies == 4 and [r["q"] for r in rows] == [Q[5]],
      f"Q4: four companies have results (the fund none); only Sep has a quarter before it ({[r['q'] for r in rows]})")
check(last["rev"] == round(want_rev, 1) and last["ni"] == round(want_ni, 1) and last["margin"] == round(want_mg, 1),
      f"Q4: medians of the companies' own growth -- rev {last['rev']} vs {want_rev:.1f}, "
      f"profit {last['ni']} vs {want_ni:.1f}, margin {last['margin']} vs {want_mg:.1f}")
check(last["n"] == {"rev": 3, "ni": 2, "eps": 2, "margin": 2},
      f"Q4: a loss or zero base and a missing operating income are left out of their figure ({last['n']})")
check(last["reported"] == 3 and last["season"] is False,
      "Q4: 3 of the 4 reported Sep (DUSK has not) -- most of June's 4, so not 'still being reported'")
early = {t: {"q": {q: v for q, v in d["q"].items() if q != Q[5] or t == "ACME"}} for t, d in data.items()}
rows_e, _ = qr.summarize(early, list(data), "qoq")
check(rows_e[-1]["q"] == Q[5] and rows_e[-1]["season"] is True and rows_e[-1]["reported"] == 1,
      "Q4: one of four reported -> the quarter is flagged as still being reported")
rows_y, _ = qr.summarize(data, list(data), "yoy")
check(rows_y == [], "Q4: YoY with no quarter a year before -> no rows (the chart says why)")
yoy_data = {"ACME": {"q": {"2025-09-30": rec(100, 10, 10, 1.0), "2026-06-30": rec(130, 13, 13, 1.3),
                           "2026-09-30": rec(150, 15, 12, 1.2)}}}
rows_y, _ = qr.summarize(yoy_data, ["ACME"], "yoy")
check([r["q"] for r in rows_y] == ["2026-09-30"] and rows_y[0]["rev"] == 50.0 and rows_y[0]["ni"] == 20.0,
      f"Q4: YoY compares with the same quarter a year earlier, not the one before ({rows_y})")
bonus = {"ACME": {"q": {"2025-09-30": dict(rec(100, 10, 10, 2.0), sh=1e6),
                         "2026-09-30": dict(rec(120, 12, 12, 1.2), sh=2e6)}},     # a 1:1 bonus
         "BOLT": {"q": {"2025-09-30": dict(rec(100, 10, 10, 1.0), sh=1e6),
                        "2026-09-30": dict(rec(110, 11, 11, 1.1), sh=1.1e6)}}}   # 10% dilution
rows_b, _ = qr.summarize(bonus, ["ACME", "BOLT"])
check(rows_b[-1]["n"]["eps"] == 1 and rows_b[-1]["eps"] == 10.0 and rows_b[-1]["n"]["ni"] == 2,
      f"Q4: YoY by default; a 1:1 bonus's EPS (-40% unadjusted) is left out, ordinary dilution is kept "
      f"({rows_b[-1]['eps']}, n={rows_b[-1]['n']})")
check(qr.FIELDS == ("rev", "ni", "eps", "margin")
      and [qr.FIELD_LABELS[f] for f in qr.FIELDS] == ["Revenue growth", "Profit growth", "EPS growth", "OPM"],
      "Q4: the four fields, in display order")
long = {"ACME": {"q": {qr.shift("2026-09-30", -i): rec(100 + i, 10, 10, 1.0) for i in range(10)}}}
rows_l, _ = qr.summarize(long, ["ACME"], "qoq")
check(len(rows_l) == 6 and rows_l[-1]["q"] == "2026-09-30" and rows_l[0]["label"] == "Jun '25",
      "Q4: the last six quarters, oldest first")

# --- Q5 ------------------------------------------------------------------------------
STMT = {"2026-09-30": {"Total Revenue": 110.0, "Operating Income": 11.0, "Net Income": 9.0, "Diluted EPS": 0.9}}


def job(previous, watch, fetch, members=None, budget=None, clock=None):
    fetched = []

    def _fetch(t):
        fetched.append(t)
        return fetch(t)

    def _members(prev):
        return members(prev) if members else ({"INDIA": ["IN1.NS", "IN2.NS"], "US": ["U1"]}, [])
    saved = rqr.TIME_BUDGET_SECONDS
    if budget is not None:
        rqr.TIME_BUDGET_SECONDS = budget
    try:
        out, code = rqr.run(previous, watch, T, fetch=_fetch, members_fn=_members, sleep=lambda s: None,
                            clock=clock or (lambda: 0.0))
    finally:
        rqr.TIME_BUDGET_SECONDS = saved
    return out, code, fetched


prev = {"tickers": {"IN1.NS": {"fetched": "2026-10-27", "q": {"2026-09-30": {"rev": 1.0}}},
                    "U1": {"fetched": "2026-10-27", "q": {"2026-06-30": {"rev": 5.0}}}},
        "members": {"US": ["U1", "U9"]}}
out, code, fetched = job(prev, ["ACME", "U1"], lambda t: STMT)
check(sorted(fetched) == ["ACME", "IN2.NS", "U1"] and code == 0,
      f"Q5: only the due companies are fetched (IN1.NS has Sep stored) ({sorted(fetched)})")
check(set(out["tickers"]) == {"IN1.NS", "IN2.NS", "U1", "ACME"} and out["tickers"]["U1"]["q"]["2026-06-30"] == {"rev": 5.0}
      and out["tickers"]["U1"]["q"]["2026-09-30"]["rev"] == 110.0 and out["tickers"]["ACME"]["fetched"] == "2026-10-28",
      "Q5: a new quarter is added to the stored ones; index members and watchlist stocks are both covered")
out, code, _ = job(prev, [], lambda t: None if t == "U1" else STMT)
check(out["tickers"]["U1"] == prev["tickers"]["U1"] and code == 0,
      "Q5: a failed fetch keeps the stored quarters and the fetch date (so it is due again)")
out, _, _ = job(prev, [], lambda t: STMT, members=lambda p: ({"INDIA": ["IN1.NS"], "US": p["US"]}, ["US"]))
check(out["members"]["US"] == ["U1", "U9"] and out["status"]["members_failed"] == ["US"],
      "Q5: a member list that failed keeps the stored one")
many_prev = {"tickers": {}, "members": {}}
big = [f"T{i}" for i in range(100)]
out, code, fetched = job(many_prev, big, lambda t: None, members=lambda p: ({}, []))
check(code == 1 and len(fetched) == rqr.CHECK_AFTER and out["status"]["stopped"] == "Yahoo refusing",
      f"Q5: Yahoo refusing most of the run stops it after {rqr.CHECK_AFTER} and exits 1 ({len(fetched)})")
ticks = iter(range(0, 10_000, 30))
out, code, fetched = job(many_prev, big, lambda t: STMT, members=lambda p: ({}, []), budget=90,
                         clock=lambda: float(next(ticks)))
check(code == 0 and out["status"]["stopped"] == "time budget" and 0 < len(fetched) < 100
      and len(out["tickers"]) == len(fetched),
      f"Q5: the time budget stops the run cleanly, keeping what it fetched ({len(fetched)})")

cov = rqr.field_coverage({"A.NS": {"q": {"2026-06-30": rec(1, 1, 1, 1), "2026-09-30": rec(1, None, 1, None)}},
                          "B": {"q": {"2026-09-30": dict(rec(1, 1, 1, 1), sh=5.0)}}, "C": {"q": {}}})
check(cov["India"] == {"companies": 1, "rev": 1, "op": 0, "ni": 1, "eps": 0, "sh": 0}
      and cov["US"]["companies"] == 1 and cov["US"]["sh"] == 1,
      f"Q5: the log counts, per market, which fields each company's latest quarter has ({cov})")

# --- Q6 ------------------------------------------------------------------------------
WF = yaml.safe_load((REPO / ".github/workflows/quarterly-results.yml").read_text())
gate = next(s for s in WF["jobs"]["gate"]["steps"] if s.get("id") == "slot")["with"]
steps = WF["jobs"]["update_results"]["steps"]
check(gate["slots"] == "4" and gate["grace-hours"] == "22" and gate["work-job"] == "update_results"
      and gate["workflow-file"] == "quarterly-results.yml",
      "Q6: one 4 AM ET slot a day, gated like the other daily jobs")
commit = next(s for s in steps if s.get("uses") == "./.github/actions/commit-data")
check(commit["with"]["files"] == "quarterly_results.json" and commit.get("if") == "always()",
      "Q6: the file is committed even by a run that stopped early")
check(WF["jobs"]["update_results"]["timeout-minutes"] * 60 > rqr.TIME_BUDGET_SECONDS + 20 * 60,
      "Q6: the job's timeout leaves room after the script's own time budget")
import github_sync
check("quarterly_results.json" in github_sync.PULLABLE_FILES
      and "quarterly_results.json" not in [f for f, _ in github_sync.SYNCABLE_FILES],
      "Q6: the app pulls the file (generated data), never pushes it")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
