"""The News tab's (i) market reads (2026-10-31), rendered: offline, from a throwaway copy of
the repo (as test_results_render.py), over synthetic prices, breadth and market_read.json.

  V1  an (i) button above the index charts for each column -- Nifty 500, and the
      right-hand index; nothing of a read is drawn until one is clicked;
  V2  a click opens that index's latest read (headline, then the charts in drawing order,
      the footer with session and model), the other column stays closed, the charts
      are all still drawn; a second click closes it;
  V3  only Nifty 500 and the S&P 500 get a read: any other right-hand index has no (i)
      button, just a line saying so; the S&P 500 reads follow the dropdown back;
  V6  the Watchlist Breadth panels have their own (i) buttons (India Invested, US Invested): a click opens the list's
      read with its market line, parts and key takeaways; a panel showing another list says reads are for the two;
  V4  no market_read.json at all (a fresh install) renders cleanly with the same message;
  V5  a phone renders the buttons and the box cleanly.
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


APP = tempfile.mkdtemp(prefix="market_read_render_")
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
            "highs_history": {d: 3.0 for d in days}, "lows_history": {d: 1.0 for d in days},
            "drawdown_history": {"10": {d: 40.0 for d in days}, "20": {d: 20.0 for d in days}}}


json.dump({"as_of": "2026-10-28 12:00", "status": {},
           "markets": {"INDIA": _block(41.0), "US": _block(52.0), "NIFTY50": _block(63.0)}},
          open("market_breadth.json", "w"))
json.dump({"as_of": "2026-10-28 12:00 UTC", "period": "6y",
           "markets": {"india_invested": {t: _series(t) for t in IND + ["^CRSLDX"]},
                       "us_invested": {t: _series(t) for t in US + ["SPY"]}},
           "benchmarks": {t: _series(t) for t in ("SPY", "QQQ", "^CRSLDX", "^NSEI")},
           "status": {}}, open("dashboard_perf.json", "w"))

READS = {"generated_at": "2026-10-31T11:20:00Z", "indexes": {
    "INDIA": {"label": "Nifty 500", "entries": [
        {"session": "2026-10-29", "written": "2026-10-30 11:20 UTC", "model": "models/gemini-3.8-flash",
         "headline": "OLDER Nifty headline", "breadth": "old", "highs_lows": None, "drawdown": None, "return": None},
        {"session": "2026-10-30", "written": "2026-10-31 11:20 UTC", "model": "models/gemini-3.8-flash",
         "headline": "Nifty breadth is washed out", "breadth": "Only 15% of stocks are above the 200-day average.",
         "highs_lows": None, "drawdown": "Damage is healing slowly.", "return": "The list trails the index."}]},
    "US": {"label": "S&P 500", "entries": [
        {"session": "2026-10-30", "written": "2026-10-30 21:20 UTC", "model": "models/gemini-3.6-flash",
         "headline": "S&P 500 is a narrow rally", "breadth": "Cost $5 of breadth.", "highs_lows": "Highs are thin.",
         "drawdown": None, "return": None}]}},
    "lists": {
        "india_invested": {"label": "India Invested", "entries": [
            {"session": "2026-10-30", "written": "2026-10-31 11:25 UTC", "model": "models/gemini-3.8-flash", "benchmark": "Nifty 500",
             "headline": "India Invested holds up far better than the market", "market": "The list diverges from a weak Nifty.",
             "breadth": "83.9% above the 200-day average.", "stocks": "Beta Foods sits near its 52-week low.",
             "takeaways": ["Leadership is narrow.", "Watch Beta Foods."]}]},
        "us_invested": {"label": "US Invested", "entries": [
            {"session": "2026-10-30", "written": "2026-10-30 21:25 UTC", "model": "models/gemini-3.6-flash", "benchmark": "S&P 500",
             "headline": "US Invested lags the S&P 500", "takeaways": ["Few holdings.", "One near its low."]}]}}}

import breadth_charts
from streamlit.testing.v1 import AppTest


def render(mobile=False, reads=True, right=None, clicks=()):
    if reads:
        json.dump(READS, open("market_read.json", "w"))
    elif os.path.exists("market_read.json"):
        os.remove("market_read.json")
    breadth_charts.is_mobile = lambda ua: mobile
    at = AppTest.from_file(os.path.join(APP, "app.py"), default_timeout=600)
    at.session_state["authenticated"] = True
    at.session_state["main_tabs"] = "News"
    if right:
        at.session_state["breadth_us_index"] = right
    at.run()
    for key in clicks:
        at.button(key=key).click().run()
    if at.exception:
        check(False, f"render (mobile={mobile}, reads={reads}, {right}, {clicks}): {[str(e.value)[:300] for e in at.exception]}")
        return None
    return at


def md(at):
    return "\n".join(m.value for m in at.markdown)


def read_buttons(at):
    """The index reads' buttons (the invested lists' have their own, below)."""
    return [b.label for b in at.button if b.label.startswith("ⓘ") and "Invested" not in b.label]


def list_buttons(at):
    return [b.label for b in at.button if b.label.startswith("ⓘ") and "Invested" in b.label]


L, R = "market_read_open_ind_button", "market_read_open_right_button"

at = render()
if at:
    check(read_buttons(at) == ["ⓘ Nifty 500 read", "ⓘ S&P 500 read"],
          f"V1: an (i) button for each column, the right one named after its index ({read_buttons(at)})")
    check("washed out" not in md(at) and "narrow rally" not in md(at), "V1: no read is drawn until one is clicked")
    n_charts = len(at.get("plotly_chart"))

at = render(clicks=[L])
if at:
    text = md(at)
    check("Nifty breadth is washed out" in text and "Only 15% of stocks are above the 200-day average." in text
          and "OLDER Nifty headline" not in text and "narrow rally" not in text,
          "V2: the Nifty button opens its latest read, not an older one nor the other index's")
    check(text.index("Breadth.") < text.index("Stocks far below their highs.") < text.index("Your list vs the index.")
          and "52-week highs vs lows." not in text and "session of 2026-10-30" in text
          and "gemini-3.8-flash" in text, "V2: charts in drawing order, empty ones skipped, the footer names session and model")
    check(len(at.get("plotly_chart")) == n_charts, "V2: every chart is still drawn under the box")
    at.button(key=L).click().run()
    check("Nifty breadth is washed out" not in md(at), "V2: a second click closes it")
    at.button(key=R).click().run()
    t2 = md(at)
    check("S&P 500 is a narrow rally" in t2 and "Cost \\$5 of breadth." in t2 and "Nifty breadth is washed out" not in t2,
          "V2: the S&P button opens the S&P read, with the dollar sign escaped")

for other in ("Nifty 50", "Nasdaq 100"):
    at = render(right=other)
    if at:
        check(read_buttons(at) == ["ⓘ Nifty 500 read"]
              and any("Reads are written for Nifty 500 and the S&P 500 only." in c.value for c in at.caption),
              f"V3: with {other} on the right there is no (i) button there, and a line says only the two get reads")
at = render(right="S&P 500", clicks=[R])
if at:
    check(read_buttons(at) == ["ⓘ Nifty 500 read", "ⓘ S&P 500 read"] and "S&P 500 is a narrow rally" in md(at),
          "V3: the S&P 500 on the right has its button and read")

at = render(reads=False, clicks=[L])
if at:
    check("No read for Nifty 500 yet" in md(at) and len(at.get("plotly_chart")) >= 1,
          "V4: with no market_read.json the box says so and the page still renders")

at = render(mobile=True, clicks=[L, R])
if at:
    check(read_buttons(at) == ["ⓘ Nifty 500 read", "ⓘ S&P 500 read"] and "Nifty breadth is washed out" in md(at)
          and "S&P 500 is a narrow rally" in md(at), "V5: a phone renders both buttons and both boxes")

# V6 the invested lists' reads
LL, LR = "list_read_open_left_button", "list_read_open_right_button"
at = render()
if at:
    check(list_buttons(at) == ["ⓘ India Invested read", "ⓘ US Invested read"], f"V6: a button per Watchlist Breadth panel ({list_buttons(at)})")
    check("holds up far better" not in md(at), "V6: nothing of a list read is drawn until one is clicked")
at = render(clicks=[LL])
if at:
    t = md(at)
    check("India Invested holds up far better than the market" in t and "**The market.** The list diverges from a weak Nifty." in t
          and "**Holdings.** Beta Foods sits near its 52-week low." in t and "**Key takeaways**" in t and "- Leadership is narrow." in t
          and "against Nifty 500" in t and "US Invested lags" not in t and "Nifty breadth is washed out" not in t,
          "V6: the left panel's button opens India Invested's read: market line, holdings, takeaways as bullets, its benchmark")
    at.button(key=LL).click().run()
    check("holds up far better" not in md(at), "V6: a second click closes it")
at = render(clicks=[LR])
if at:
    check("US Invested lags the S&P 500" in md(at) and "- Few holdings." in md(at), "V6: the right panel's button opens US Invested's read")
at = render()
if at:
    at.session_state["breadth_watchlist_left"] = "all_invested"
    at.run()
    check(not at.exception and list_buttons(at) == ["ⓘ US Invested read"]
          and any("Reads are written for India Invested and US Invested only." in c.value for c in at.caption),
          "V6: a panel showing another list has no button, and says reads are for the two")
at = render(mobile=True, clicks=[LL, LR])
if at:
    check("India Invested holds up far better" in md(at) and "US Invested lags" in md(at), "V6: a phone renders both list boxes")

print("FAILURES:", fails)
sys.exit(fails)
