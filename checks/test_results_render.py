"""The News tab's quarterly results rows (2026-10-28), rendered: offline, from a
throwaway copy of the repo (as test_index_render.py), over synthetic prices and
a synthetic quarterly_results.json.

  R1  a results row under the index charts and one under Watchlist Breadth;
  R2  YoY by default: the index row's titles carry the medians
      quarterly_results.summarize gives for the stored members (Nifty 500 left,
      the right-hand index right) -- Revenue / Profit / EPS growth bars plus OPM
      on a second axis;
  R3  a quarter still being reported is drawn faded, and its hover says so;
  R4  the watchlist row names its benchmark index's median in each hover, and a
      list with under a year stored says why its YoY panel is empty;
  R5  QoQ switches every row;
  R6  the field picker hides fields (bars, line and title), and with none picked
      the panels say so;
  R7  no file yet -> each panel says the first quarterly-results run stores it;
  R8  a phone stacks the panels and renders cleanly.
  R9  a deploy never mixes the new app.py with an old module (2026-10-28): with
      an old breadth_charts in memory (no style_results), the News tab raised
      AttributeError until the app was rebooted; once a module file changes on
      disk, the next run loads every module fresh.
"""

import json
import os
import shutil
import sys
import tempfile
import zlib
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


APP = tempfile.mkdtemp(prefix="results_render_")
shutil.rmtree(APP)
shutil.copytree(REPO, APP, ignore=shutil.ignore_patterns(
    ".git", "*.json", ".env", "__pycache__", "data", "checks", "docs",
    ".claude", ".agents", ".github", ".devcontainer"))
os.chdir(APP)
sys.path.insert(0, APP)
for k in ("DATA_REPO_TOKEN", "GITHUB_TOKEN", "DISCORD_WEBHOOK_URL", "GEMINI_API_KEY",
          "AUTH_USERNAME", "AUTH_PASSWORD"):
    os.environ[k] = ""
os.environ["SKIP_GITHUB_PULL"] = "1"

# Six stocks a side: a figure needs 5+ companies (quarterly_results.MIN_COMPANIES).
# TINY has two, for the "too few companies" message.
IND = [f"W{i}.NS" for i in range(6)]
US = [f"V{i}" for i in range(6)]
TINY = ["T0.NS", "T1.NS"]
json.dump({"india_invested": {"label": "India Invested", "benchmark": "^CRSLDX"},
           "us_invested": {"label": "US Invested", "benchmark": "SPY"},
           "tiny": {"label": "Tiny", "benchmark": "^CRSLDX"}}, open("markets.json", "w"))
json.dump({"india_invested": IND, "us_invested": US, "tiny": TINY}, open("watchlist.json", "w"))
json.dump({"all_invested": ["india_invested", "us_invested"], "all_watchlist": ["india_invested"]},
          open("watchlist_groups.json", "w"))
json.dump({**{t: {"index": "Nifty 500", "benchmark": "^CRSLDX"} for t in IND + TINY},
           **{t: {"index": "S&P 500", "benchmark": "SPY"} for t in US}}, open("ticker_index.json", "w"))

import yfinance as yf

PDAYS = pd.bdate_range(end=pd.Timestamp(date.today()) - pd.Timedelta(days=1), periods=400)


def _px(sym):
    rng = np.random.default_rng(zlib.crc32(sym.encode()))
    return 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.02, len(PDAYS))))


def _frame(sym):
    c = _px(sym)
    return pd.DataFrame({"Open": c * 0.995, "High": c * 1.01, "Low": c * 0.99, "Close": c,
                         "Volume": np.full(len(PDAYS), 1_000_000)}, index=PDAYS)


class _Ticker:
    def __init__(self, symbol):
        self.symbol = symbol

    @property
    def info(self):
        return {"longName": f"Company {self.symbol}", "quoteType": "EQUITY"}

    def history(self, **kw):
        return _frame(self.symbol)

    def __getattr__(self, name):
        return pd.DataFrame()


yf.download = lambda symbols, **kw: pd.concat(
    {s: _frame(s) for s in (symbols if isinstance(symbols, (list, tuple)) else symbols.split())}, axis=1)
yf.Ticker = _Ticker

import stock_data as sd

_settings = sd.load_settings()
_, _as_of, _per_market = sd.fetch_all_markets(settings=_settings)
sd.save_data_snapshot(_as_of, _per_market, settings=_settings)


def _series(sym):
    return {d.strftime("%Y-%m-%d"): round(float(v), 4) for d, v in zip(PDAYS, _px(sym))}


def _block(pct):
    days = [d.strftime("%Y-%m-%d") for d in PDAYS[-300:]]
    return {"as_of": "2026-10-28 12:00", "total": 48, "above": 20, "pct_above": pct,
            "history": {d: pct for d in days}, "history_50": {d: pct - 5 for d in days},
            "highs_history": {d: 3.0 for d in days}, "lows_history": {d: 1.0 for d in days}}


json.dump({"as_of": "2026-10-28 12:00", "status": {},
           "markets": {"INDIA": _block(41.0), "US": _block(52.0), "NIFTY50": _block(63.0)}},
          open("market_breadth.json", "w"))
json.dump({"as_of": "2026-10-28 12:00 UTC", "period": "6y",
           "markets": {"india_invested": {t: _series(t) for t in IND + ["^CRSLDX"]},
                       "us_invested": {t: _series(t) for t in US + ["SPY"]}},
           "benchmarks": {t: _series(t) for t in ("SPY", "QQQ", "^CRSLDX", "^NSEI")},
           "status": {}}, open("dashboard_perf.json", "w"))

import breadth_charts
import index_registry as ir
from streamlit.testing.v1 import AppTest

breadth_charts.is_mobile = lambda ua: False



import quarterly_results as qr


def _rec(rev, op, ni, eps):
    return {"rev": rev, "op": op, "ni": ni, "eps": eps}


Q = [qr.shift("2026-09-30", -i) for i in range(5, -1, -1)]   # Jun '25 .. Sep '26
IN_IDX = [f"IX{i}.NS" for i in range(12)]
US_IDX = [f"UX{i}" for i in range(8)]


def _company(seed, last=True, first=0):
    rng = np.random.default_rng(seed)
    out, rev = {}, 100.0
    for q in (Q if last else Q[:-1])[first:]:
        rev *= 1 + rng.normal(0.03, 0.05)
        out[q] = _rec(round(rev, 2), round(rev * 0.15, 2), round(rev * 0.08, 2), round(rev * 0.01, 3))
    return {"fetched": "2026-10-27", "q": out}


TICKERS = {t: _company(i) for i, t in enumerate(IN_IDX + US_IDX + IND + US + TINY)}
# The US watchlist's stocks have four quarters stored, Dec '25 on (no YoY yet).
for t in US:
    TICKERS[t] = _company(US.index(t) + 50, first=2)
# Five of the twelve India index companies have reported Sep: still being reported
# there (5 < half of June's 12), and still enough companies (5) to be drawn.
for t in IN_IDX[5:]:
    TICKERS[t] = _company(IN_IDX.index(t), last=False)
RESULTS = {"as_of": "2026-10-28 08:00 UTC", "tickers": TICKERS,
           "members": {"INDIA": IN_IDX, "US": US_IDX}, "status": {}}


def render(basis=None, fields=None, mobile=False, with_file=True):
    if with_file:
        json.dump(RESULTS, open("quarterly_results.json", "w"))
    elif os.path.exists("quarterly_results.json"):
        os.remove("quarterly_results.json")
    breadth_charts.is_mobile = lambda ua: mobile
    at = AppTest.from_file(os.path.join(APP, "app.py"), default_timeout=600)
    at.session_state["authenticated"] = True
    at.session_state["main_tabs"] = "News"
    if basis:
        at.session_state["results_growth_basis"] = basis
    if fields is not None:
        at.session_state["results_fields"] = fields
    at.run()
    if at.exception:
        check(False, f"render ({basis}, {fields}, mobile={mobile}): {[str(e.value)[:300] for e in at.exception]}")
        return None
    return at


def charts(at):
    return [json.loads(el.proto.spec) for el in at.get("plotly_chart")]


def titles(spec):
    return [a.get("text", "") for a in spec.get("layout", {}).get("annotations", [])]


def sig(v):
    return "--" if v is None else f"{v:+.1f}%"


def lvl(v):
    return "--" if v is None else f"{v:.1f}%"


def names(spec):
    return [tr.get("name") for tr in spec["data"] if tr.get("name")]


at = render()
if at:
    specs = charts(at)
    check(len(specs) == 4, f"R1: four charts -- index, its results, watchlist breadth, its results ({len(specs)})")
    idx_r = specs[1]
    t = titles(idx_r)
    rows_in, _ = qr.summarize(TICKERS, IN_IDX, "yoy")
    rows_us, _ = qr.summarize(TICKERS, US_IDX, "yoy")
    li, lu = rows_in[-1], rows_us[-1]
    want_in = (f"Nifty 500: Quarterly Results (YoY, median)<br>{li['label']}: Rev {sig(li['rev'])}, "
               f"Profit {sig(li['ni'])}, EPS {sig(li['eps'])}, OPM {lvl(li['margin'])}; {li['reported']} of 12 reported")
    want_us = f"S&P 500: Quarterly Results (YoY, median)<br>{lu['label']}: Rev {sig(lu['rev'])}"
    check(want_in in t and any(x.startswith(want_us) for x in t),
          f"R2: YoY by default, and the index row's titles carry summarize's medians ({t[:2]})")
    n = names(idx_r)
    check(all(n.count(x) == 2 for x in ("Revenue growth", "Profit growth", "EPS growth", "OPM"))
          and idx_r["layout"].get("barmode") == "group",
          f"R2: Revenue / Profit / EPS growth bars and the OPM line, in each panel ({n})")
    rev_in = next(tr for tr in idx_r["data"] if tr.get("name") == "Revenue growth")
    opm_in = next(tr for tr in idx_r["data"] if tr.get("name") == "OPM")
    check(opm_in.get("yaxis") != rev_in.get("yaxis"), "R2: OPM is on its own (second) axis")
    check(rev_in["x"] == ["Jun '26", "Sep '26"],
          f"R2: YoY shows only the quarters with the same quarter a year before stored ({rev_in['x']})")
    check(rev_in["marker"]["opacity"] == [1, 0.4] and rev_in["marker"]["line"]["width"] == [0, 1.5]
          and "pattern" not in rev_in["marker"]
          and "still being reported" in rev_in["hovertext"][-1] and "median of 5" in rev_in["hovertext"][-1],
          f"R3: Sep '26, reported by 5 of 12, is drawn light with an outline (no hatch, which rendered "
          f"white) and its hover says so ({rev_in['hovertext'][-1]})")
    wl_r = specs[3]
    hov = " ".join(h for tr in wl_r["data"] for h in (tr.get("hovertext") or []))
    wt = " | ".join(titles(wl_r))
    check("-- Nifty 500 " in hov and wt.startswith("India Invested: Quarterly Results (YoY"),
          "R4: the India watchlist's hover names Nifty 500's median beside its own")
    check("No YoY quarter yet - one fills in each results season as quarters are stored" in wt,
          "R4: a list with under a year stored (US Invested) says why its YoY panel is empty")
    check(any("Quarterly results growth" in str(getattr(e, "proto", "")) for e in at.get("button_group"))
          and any("Quarterly results fields" in str(getattr(e, "proto", "")) for e in at.get("button_group")),
          "R5/R6: the YoY / QoQ toggle and the field picker are drawn")

at = render("QoQ")
if at:
    specs = charts(at)
    t = " | ".join(titles(specs[1]) + titles(specs[3]))
    rev = next(tr for tr in specs[1]["data"] if tr.get("name") == "Revenue growth")
    rows_q, _ = qr.summarize(TICKERS, US_IDX, "qoq")
    hov = " ".join(h for tr in specs[3]["data"] for h in (tr.get("hovertext") or []))
    check(rev["x"] == ["Sep '25", "Dec '25", "Mar '26", "Jun '26", "Sep '26"]
          and "S&P 500: Quarterly Results (QoQ, median)<br>Sep '26: Rev " + sig(rows_q[-1]["rev"]) in t
          and "(YoY" not in t and "-- S&P 500 " in hov,
          f"R5: QoQ switches every row, US Invested included ({rev['x']})")

at = render(fields=["rev", "margin"])
if at:
    specs = charts(at)
    n = names(specs[1]) + names(specs[3])
    t = " | ".join(titles(specs[1]))
    check(set(n) == {"Revenue growth", "OPM"} and "Profit " not in t and "EPS " not in t and "OPM " in t,
          f"R6: hiding Profit and EPS growth drops their bars and their title figures ({sorted(set(n))})")

at = render(fields=[])
if at:
    specs = charts(at)
    t = " | ".join(titles(specs[1]) + titles(specs[3]))
    check(t.count("Pick a field to show above") == 4 and not names(specs[1]),
          "R6: with no field picked, every results panel says to pick one")

at = AppTest.from_file(os.path.join(APP, "app.py"), default_timeout=600)
at.session_state["authenticated"] = True
at.session_state["main_tabs"] = "News"
at.session_state["breadth_watchlist_left"] = "tiny"
json.dump(RESULTS, open("quarterly_results.json", "w"))
at.run()
if not at.exception:
    t = " | ".join(titles(charts(at)[3]))
    check("Too few companies per figure - each needs 5+ with both quarters stored" in t,
          f"R6b: a 2-stock list says its figures need 5+ companies, not 'no YoY quarter yet' ({t[:160]})")
else:
    check(False, f"R6b render: {[str(e.value)[:200] for e in at.exception]}")

at = render(with_file=False)
if at:
    specs = charts(at)
    t = " | ".join(titles(specs[1]) + titles(specs[3]))
    check(t.count("No quarterly results yet - stored by the first quarterly-results run") == 4,
          "R7: no file yet -> all four results panels say the first run stores it")

at = render(mobile=True)
if at:
    specs = charts(at)
    idx_r = specs[1]
    check(len(specs) == 4 and idx_r["layout"].get("dragmode") is False
          and idx_r["layout"]["yaxis"]["domain"][0] > idx_r["layout"].get("yaxis3", {}).get("domain", [1, 1])[1] - 0.01,
          "R8: on a phone the results panels stack, with no drag-to-zoom")

# --- R9: an old module left in memory by a deploy ----------------------------------
import time as _time

mod = sys.modules["breadth_charts"]
real = mod.style_results
del mod.style_results                       # the running process's old copy
at = AppTest.from_file(os.path.join(APP, "app.py"), default_timeout=600)
at.session_state["authenticated"] = True
at.session_state["main_tabs"] = "News"
at.run()
check(any("style_results" in str(e.value) for e in at.exception),
      "R9: reproduced -- the old module in memory makes the News tab raise AttributeError")
path = os.path.join(APP, "breadth_charts.py")
st_ = os.stat(path)
os.utime(path, (st_.st_atime, st_.st_mtime + 5))   # the deploy rewrites the file
at = render()
fresh = sys.modules.get("breadth_charts")
check(at is not None and fresh is not mod and hasattr(fresh, "style_results"),
      "R9: once the file changes on disk, the next run loads the modules fresh and renders")
same = sys.modules.get("breadth_charts")
render()
check(sys.modules.get("breadth_charts") is same, "R9: with nothing changed, nothing is reloaded")
mod.style_results = real

shutil.rmtree(APP, ignore_errors=True)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
