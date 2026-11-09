"""Qtr EPS Growth % compares the same quarter a year earlier, found by date (owner, 2026-11-09).

It took the 5th column Yahoo returned as "a year ago". Yahoo leaves holes -- most Indian companies had no
Sep '25 quarter on 2026-11-09 -- so Sep '26 was compared with Jun '25 in 35 of 118 rows. Without a comparable
year-ago EPS (the quarter missing, or a bonus-sized share-count jump) the quarter's profit growth stands in,
marked by qtr_eps_growth_basis "profit" (owner's choice).

E1 A hole where the year-ago quarter should be: profit growth stands in (basis "profit"); none to stand in -> blank.
E2 Consecutive quarters: EPS against the same quarter a year earlier, whatever order Yahoo's columns are in.
E3 A share count that moved more than 1.5x: profit growth stands in (an unadjusted bonus read as EPS halving).
E4 A loss a year earlier: blank, even with profit growth (no percentage off a loss, as before).
E5 No statement (a failed fetch) or an empty one: blank, and no stand-in.
E6 TTM growth needs 8 consecutive quarters; a hole blanks it.
E7 market_lookup.keep_previous: a blank EPS growth from a statement that came back is NOT refilled from the stored
   row; a failed statement or failed company details still is.
E8 The wiring: fetch_snapshot writes qtr_eps_growth, its basis and eps_history_loaded; the catalog publishes the
   basis; both publishes and the live lookup pass the flag; the Expert prompt says when profit growth stands in.

Offline: invented tickers and figures.
"""

import inspect
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import stock_data as sd

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def statement(quarters, newest_first=True):
    """A quarterly_income_stmt-shaped frame: rows are line items, columns period ends.
    `quarters` is {"YYYY-MM-DD": {"eps": x, "sh": n, "ni": y, "rev": z}}."""
    names = {"eps": "Diluted EPS", "sh": "Diluted Average Shares", "ni": "Net Income", "rev": "Total Revenue"}
    cols = sorted(quarters, reverse=newest_first)
    data = {pd.Timestamp(q): {names[k]: v for k, v in quarters[q].items()} for q in cols}
    return pd.DataFrame(data)


# --- E1 ---------------------------------------------------------------------------------------------
hole = statement({"2025-06-30": {"eps": 6.95, "sh": 409.6}, "2025-12-31": {"eps": 7.82, "sh": 418.5},
                  "2026-03-31": {"eps": 6.85, "sh": 421.5}, "2026-06-30": {"eps": 7.84, "sh": 421.6},
                  "2026-09-30": {"eps": 7.09, "sh": 423.3}})
check(sd.quarterly_eps_growth(hole, 12.4) == (12.4, "profit"),
      f"E1: no Sep '25 -> the profit growth stands in, not Sep '26 vs Jun '25 ({sd.quarterly_eps_growth(hole, 12.4)})")
check(sd.quarterly_eps_growth(hole, None) == (None, None), "E1: ...and with no profit growth either, blank")

late_eps = statement({**{q: dict(v) for q, v in {"2025-06-30": {"eps": 5.0}, "2025-09-30": {"eps": 5.2},
                      "2025-12-31": {"eps": 5.5}, "2026-03-31": {"eps": 6.0}, "2026-06-30": {"eps": 6.2}}.items()},
                      "2026-09-30": {"ni": 70.0}})
check(sd.quarterly_eps_growth(late_eps, 8.0) == (8.0, "profit"),
      "E1: the newest quarter reported without EPS -> profit stands in, not an older quarter's EPS growth")

# --- E2 ---------------------------------------------------------------------------------------------
five = {"2025-09-30": {"eps": 5.0, "sh": 100}, "2025-12-31": {"eps": 5.5, "sh": 100},
        "2026-03-31": {"eps": 6.0, "sh": 100}, "2026-06-30": {"eps": 6.2, "sh": 101}, "2026-09-30": {"eps": 6.5, "sh": 101}}
want = round((6.5 / 5.0 - 1) * 100, 1)
check(sd.quarterly_eps_growth(statement(five), 99.0) == (want, "eps"),
      f"E2: Sep '26 against Sep '25 ({sd.quarterly_eps_growth(statement(five), 99.0)}, want {want})")
check(sd.quarterly_eps_growth(statement(five, newest_first=False), 99.0) == (want, "eps"),
      "E2: the same with Yahoo's columns oldest first")
six = dict(five, **{"2025-06-30": {"eps": 1.0, "sh": 100}})
check(sd.quarterly_eps_growth(statement(six), None) == (want, "eps"), "E2: a sixth, older quarter changes nothing")
us = statement({"2025-08-02": {"eps": 2.0, "sh": 50}, "2025-11-01": {"eps": 2.1, "sh": 50}, "2026-01-31": {"eps": 2.2, "sh": 50},
                "2026-05-02": {"eps": 2.3, "sh": 50}, "2026-08-01": {"eps": 2.5, "sh": 50}})
check(sd.quarterly_eps_growth(us, None) == (25.0, "eps"),
      "E2: a retailer's 52-53 week quarters land on their nearest quarter ends (Aug '26 vs Aug '25)")

# --- E3 ---------------------------------------------------------------------------------------------
bonus = {q: dict(v) for q, v in five.items()}
for q in ("2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"):
    bonus[q]["sh"] = 200
    bonus[q]["eps"] = five[q]["eps"] / 2       # a 1:1 bonus: newer EPS on twice the shares
check(sd.quarterly_eps_growth(statement(bonus), 30.0) == (30.0, "profit"),
      "E3: a share count that doubled -> the profit growth stands in (not EPS 'halving')")
mild = {q: dict(v) for q, v in five.items()}
mild["2026-09-30"]["sh"] = 140
check(sd.quarterly_eps_growth(statement(mild), 30.0)[1] == "eps", "E3: a 1.4x share count is real dilution, EPS kept")

# --- E4 ---------------------------------------------------------------------------------------------
loss = {q: dict(v) for q, v in five.items()}
loss["2025-09-30"]["eps"] = -1.2
check(sd.quarterly_eps_growth(statement(loss), 55.0) == (None, None), "E4: a loss a year earlier -> blank")

# --- E5 ---------------------------------------------------------------------------------------------
check(sd.quarterly_eps_growth(None, 10.0) == (None, None), "E5: no statement (a failed fetch) -> blank, no stand-in")
check(sd.quarterly_eps_growth(pd.DataFrame(), 10.0) == (None, None), "E5: an empty statement -> blank")
no_eps = statement({"2026-06-30": {"ni": 5.0}, "2026-09-30": {"ni": 6.0}})
check(sd.quarterly_eps_growth(no_eps, 10.0) == (10.0, "profit"), "E5: a statement with no EPS line -> profit stands in")

# --- E6 ---------------------------------------------------------------------------------------------
q8 = {}
day = pd.Timestamp("2026-09-30")
for i in range(8):
    end = (day - pd.offsets.QuarterEnd(i)).strftime("%Y-%m-%d")
    q8[end] = {"ni": 10.0 if i < 4 else 8.0, "rev": 100.0}
check(sd.quarterly_ttm_growth(statement(q8), "Net Income") == 25.0, "E6: 8 consecutive quarters -> TTM growth")
gap8 = dict(q8)
gap8.pop("2025-09-30")
gap8["2024-06-30"] = {"ni": 8.0, "rev": 100.0}
check(sd.quarterly_ttm_growth(statement(gap8), "Net Income") is None, "E6: a hole in the 8 -> blank, not a sum across it")
late = dict(q8, **{"2026-12-31": {"rev": 120.0}})
check(sd.quarterly_ttm_growth(statement(late), "Net Income") is None,
      "E6: the newest quarter without that line -> blank, not an older window")
check(sd.quarterly_ttm_growth(None, "Net Income") is None and sd.quarterly_ttm_growth(statement(q8), "Missing Row") is None,
      "E6: no statement or no such line -> blank")

# --- E7 ---------------------------------------------------------------------------------------------
import market_lookup as ml

stored = {"ticker": "ACME.NS", "company_name": "Acme Ltd",
          "metrics": {"qtr_eps_growth": 170.2, "qtr_eps_growth_basis": None, "roce": 12.0, "qtr_profit_growth": 9.0}}


def fresh():
    return {"ticker": "ACME.NS", "company_name": "Acme Ltd",
            "metrics": {"qtr_eps_growth": None, "qtr_eps_growth_basis": None, "roce": None, "qtr_profit_growth": None}}


r = fresh()
kept = ml.keep_previous(r, stored, False, eps_loaded=True)
check(r["metrics"]["qtr_eps_growth"] is None and "qtr_eps_growth" not in kept and "roce" in kept,
      f"E7: the statement came back -> a blank EPS growth stays blank (other statement fields still kept: {kept})")
r = fresh()
check("qtr_eps_growth" in ml.keep_previous(r, stored, False, eps_loaded=False) and r["metrics"]["qtr_eps_growth"] == 170.2,
      "E7: the statement failed -> the stored figure is kept, as before")
r = fresh()
check("qtr_eps_growth" in ml.keep_previous(r, stored, True, eps_loaded=True),
      "E7: the company details failed (no profit growth to stand in) -> the stored figure is kept")
r = fresh()
check("qtr_eps_growth" in ml.keep_previous(r, stored, False),
      "E7: a caller that passes no flag (an older row) keeps the old behaviour")

# --- E8 wiring --------------------------------------------------------------------------------------
n = 300
idx = pd.bdate_range("2025-08-01", periods=n)
p = pd.Series(np.linspace(100, 130, n), index=idx)
frame = pd.DataFrame({"Open": p, "High": p * 1.01, "Low": p * 0.99, "Close": p, "Volume": 1e5})
fixture = pd.concat({"ACME.NS": frame, "^CRSLDX": frame}, axis=1)


class FakeTicker:
    def __init__(self, t):
        self.quarterly_income_stmt = hole
        self.cash_flow = self.income_stmt = self.balance_sheet = None


saved = {k: getattr(sd, k) for k in ("_download_with_retries", "fill_unpublished_daily_bars",
                                     "_fetch_info_with_retry", "_safe_fetch")}
saved_ticker = sd.yf.Ticker
try:
    sd._download_with_retries = lambda t, per, **k: fixture.copy()
    sd.fill_unpublished_daily_bars = lambda raw, t, **k: (raw, [])
    sd._fetch_info_with_retry = lambda y, t: {"longName": "Acme Ltd", "earningsQuarterlyGrowth": 0.124}
    sd._safe_fetch = lambda fn, label="": fn()
    sd.yf.Ticker = FakeTicker
    sd.time.sleep, _sleep = (lambda s: None), sd.time.sleep
    rows, _ = sd.fetch_snapshot(["ACME.NS"], benchmark="^CRSLDX", settings=dict(sd.DEFAULT_SETTINGS))
finally:
    for k, v in saved.items():
        setattr(sd, k, v)
    sd.yf.Ticker = saved_ticker
    sd.time.sleep = _sleep
row = rows[0] if rows else {}
check(row.get("qtr_eps_growth") == 12.4 and row.get("qtr_eps_growth_basis") == "profit" and row.get("eps_history_loaded") is True,
      f"E8: fetch_snapshot's row ({row.get('qtr_eps_growth')}, {row.get('qtr_eps_growth_basis')}, {row.get('eps_history_loaded')})")

import market_api

fields = [f for f, *_ in market_api.METRIC_GROUPS["quality_growth"][1]]
check("qtr_eps_growth_basis" in fields and fields.index("qtr_eps_growth_basis") == fields.index("qtr_eps_growth") + 1,
      "E8: the catalog publishes the basis right after EPS growth")
check(market_api.public_metrics(row).get("qtr_eps_growth_basis") == "profit"
      and "eps_history_loaded" not in market_api.public_metrics(row),
      "E8: a published record carries the basis, never the internal flag")
src_pub = (REPO / "publish_market.py").read_text()
check(src_pub.count('row.get("eps_history_loaded")') == 2,
      "E8: the screener and library publishes both pass the flag to keep_previous")
check('row.get("eps_history_loaded"))' in inspect.getsource(ml.lookup), "E8: ...and so does the live lookup")

import expert_views

prompt_src = inspect.getsource(expert_views.build_expert_prompt)
check("qtr_eps_growth_basis') == 'profit'" in prompt_src and "this is the profit growth" in prompt_src,
      "E8: the Expert prompt says when profit growth stands in for EPS")
check(" quarterly_results.py " in (REPO / "scripts/vercel-ignore.sh").read_text(),
      "E8: the Vercel functions redeploy when quarterly_results.py changes (stock_data imports it)")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
