"""The News tab's index pickers (2026-10-28), rendered: offline, from a throwaway
copy of the repo (as test_stage_render.py), over synthetic prices.

  X5  the right-hand index is a dropdown of every index but Nifty 500, S&P 500
      first and by default, with US Invested against it; picking Nifty 50 draws
      its breadth and compares India Invested (not US Invested) with ^NSEI; an
      index not stored yet says so for its breadth and its prices; both
      watchlist sides offer all six benchmarks, defaulting by exchange.
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


# --- X5 the News tab, rendered -------------------------------------------------------
APP = tempfile.mkdtemp(prefix="index_render_")
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

IND, US = ["ZED.NS", "QUX.NS"], ["ACME", "BOLT"]
json.dump({"india_invested": {"label": "India Invested", "benchmark": "^CRSLDX"},
           "us_invested": {"label": "US Invested", "benchmark": "SPY"}}, open("markets.json", "w"))
json.dump({"india_invested": IND, "us_invested": US}, open("watchlist.json", "w"))
json.dump({"all_invested": ["india_invested", "us_invested"], "all_watchlist": ["india_invested"]},
          open("watchlist_groups.json", "w"))
json.dump({**{t: {"index": "Nifty 500", "benchmark": "^CRSLDX"} for t in IND},
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


def render(right=None):
    at = AppTest.from_file(os.path.join(APP, "app.py"), default_timeout=600)
    at.session_state["authenticated"] = True
    at.session_state["main_tabs"] = "News"
    if right:
        at.session_state["breadth_us_index"] = right
    at.run()
    if at.exception:
        check(False, f"X5: render ({right}): {[str(e.value)[:300] for e in at.exception]}")
        return None
    return at


def chart_text(at):
    """The index chart's subplot titles and notes (annotation texts), joined: the
    first plotly chart on the tab (Watchlist Breadth's comes after it)."""
    charts = list(at.get("plotly_chart"))
    if not charts:
        return ""
    spec = json.loads(charts[0].proto.spec)
    return " | ".join(a.get("text", "") for a in spec.get("layout", {}).get("annotations", []))


at = render()
if at:
    pick = next((s for s in at.selectbox if s.key == "breadth_us_index"), None)
    check(pick is not None and pick.value == "S&P 500"
          and list(pick.options) == ["S&P 500", "Nasdaq 100", "Nifty 50", "Nifty Midcap 150", "Nifty Smallcap 250"],
          f"X5: the right-hand index is a dropdown of every index but Nifty 500, S&P 500 first "
          f"({pick and list(pick.options)})")
    texts = chart_text(at)
    check("S&P 500: % Above 200-Day SMA" in texts and "US Invested vs S&P 500" in texts,
          "X5: by default the right column is the S&P 500, with US Invested against it")
    benches = [s for s in at.selectbox if str(s.key).startswith("breadth_benchmark_")]
    check(len(benches) == 2 and all(list(b.options) == ir.LABELS for b in benches)
          and [b.value for b in benches] == ["Nifty 500", "S&P 500"],
          f"X5: both watchlist sides offer all six benchmarks, defaulting by exchange "
          f"({[b.value for b in benches]})")

at = render("Nifty 50")
if at:
    texts = chart_text(at)
    check("Nifty 50: % Above 200-Day SMA" in texts and "63.0%" in texts,
          "X5: picking Nifty 50 draws Nifty 50's breadth on the right")
    check("India Invested vs Nifty 50" in texts and "US Invested vs" not in texts,
          "X5: ...and compares India Invested with it, not US Invested")
    check("Watchlist: +" in texts.split("India Invested vs Nifty 50", 1)[1][:40]
          or "Watchlist: -" in texts.split("India Invested vs Nifty 50", 1)[1][:40],
          "X5: ...with a return worked out against ^NSEI")

at = render("Nifty Midcap 150")
if at:
    texts = chart_text(at)
    check("No Nifty Midcap 150 data yet - stored by the next breadth run" in texts
          and "No Nifty Midcap 150 prices yet - stored by the next breadth run" in texts,
          "X5: an index the job has not stored yet says so (breadth and prices), not 'refresh failed'")

shutil.rmtree(APP, ignore_errors=True)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
