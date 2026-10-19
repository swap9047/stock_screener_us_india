"""Phone first-load trimming (2026-10-19): a rendered app, offline.

On a phone browser (Brave Android lagged on first load) the watchlist tabs skip
what is not on screen:
  P1  only the selected tab builds its table (the other 8 used to be ~90% of the
      page), combined tabs included;
  P2  filter / search / AI-review selections on a hidden tab survive the switch;
  P3  the Expert Views cards are built only while their expander is open.
Desktop must render exactly as before:
  D1  every tab's table is built, and the Expert Views cards with them.

The app is rendered with AppTest from a throwaway copy of the repo, over a
snapshot that the app's own fetch_all_markets computed from synthetic prices
(yfinance stubbed, no network). 7 watchlists x 3 invented tickers (T1X00 ...,
".NS" on the India ones). Phone vs desktop is chosen by stubbing
breadth_charts.is_mobile, which IS_MOBILE reads -- AppTest sends no User-Agent.

AppTest cannot click an expander open. Setting its key in session_state opens
it for the next run only (the flowchart expander behaves the same), which is
enough to see the cards get built.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- a throwaway app folder with invented watchlists ----------------------------
APP = tempfile.mkdtemp(prefix="phone_render_")
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

N = 3
lists = {f"wl{i}": {"label": f"List {i}", "benchmark": "SPY" if i % 2 else "^CRSLDX"} for i in range(1, 8)}
tickers = {k: [f"T{i}X{j:02d}{'' if i % 2 else '.NS'}" for j in range(N)] for i, k in enumerate(lists, 1)}
json.dump(lists, open("markets.json", "w"))
json.dump(tickers, open("watchlist.json", "w"))
json.dump({"all_invested": ["wl1", "wl2"], "all_watchlist": ["wl3", "wl4", "wl5"]},
          open("watchlist_groups.json", "w"))
json.dump({t: ({"index": "Nifty 500", "benchmark": "^CRSLDX"} if t.endswith(".NS")
               else {"index": "S&P 500", "benchmark": "SPY"})
           for ts in tickers.values() for t in ts}, open("ticker_index.json", "w"))
json.dump({t: {"verdict": "HOLD", "headline": "Invented headline", "technical_summary": "x",
               "catalyst_summary": "x", "actionable_take": "x", "as_of": "2026-10-16T00:00:00+00:00",
               "news_source": "test", "model_used": "test"}
           for ts in tickers.values() for t in ts}, open("expert_views.json", "w"))

# --- snapshot from synthetic prices, through the app's own fetch path ----------
import numpy as np
import pandas as pd
import yfinance as yf

DATES = pd.bdate_range(end="2026-10-16", periods=400)


def _series(symbol):
    rng = np.random.default_rng(zlib.crc32(symbol.encode()))
    close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.02, len(DATES))))
    return pd.DataFrame({"Open": close * 0.995, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": rng.integers(100_000, 5_000_000, len(DATES))},
                        index=DATES)


class _Ticker:
    def __init__(self, symbol):
        self.symbol = symbol

    @property
    def info(self):
        return {"longName": f"Company {self.symbol}", "quoteType": "EQUITY"}

    def history(self, **kw):
        return _series(self.symbol)

    def __getattr__(self, name):
        return pd.DataFrame()


yf.download = lambda symbols, **kw: pd.concat(
    {s: _series(s) for s in (symbols if isinstance(symbols, (list, tuple)) else symbols.split())}, axis=1)
yf.Ticker = _Ticker

import stock_data as sd

_settings = sd.load_settings()
_, _as_of, _per_market = sd.fetch_all_markets(settings=_settings)
sd.save_data_snapshot(_as_of, _per_market, settings=_settings)
check(sd.snapshot_is_usable(sd.load_data_snapshot(), sd.load_watchlists(), _settings),
      "fixture: the synthetic snapshot is usable (no live fetch during the render)")

# --- render helpers ---------------------------------------------------------------
import breadth_charts
from streamlit.testing.v1 import AppTest


def render(phone):
    breadth_charts.is_mobile = lambda ua: phone
    at = AppTest.from_file(os.path.join(APP, "app.py"), default_timeout=600)
    at.session_state["authenticated"] = True
    return at


def ok_run(at, label):
    at.run()
    if at.exception:
        check(False, f"{label}: {[str(e.value)[:200] for e in at.exception]}")
        return False
    return True


def table_lists(at):
    """Which watchlists' rows appear in a built table (1..7)."""
    return sorted({int(m[2]) for b in at.markdown if "<table" in b.value
                   for m in re.findall(r">T\dX\d\d", b.value)})


def table_rows(at):
    return sorted({m[1:] for b in at.markdown if "<table" in b.value
                   for m in re.findall(r">T\dX\d\d(?:\.NS)?", b.value)})


def cards(at):
    return sum("<b>Headline:</b>" in b.value for b in at.markdown)


# --- D1 desktop: unchanged ------------------------------------------------------
at = render(phone=False)
if ok_run(at, "D1 desktop render"):
    check(table_lists(at) == [1, 2, 3, 4, 5, 6, 7] and sum("<table" in b.value for b in at.markdown) == 9,
          "D1: desktop builds every tab's table (7 watchlists + 2 combined)")
    check(cards(at) == 7 * N + 5 * N,
          f"D1: desktop builds the Expert Views cards for every tab as before ({cards(at)})")

# --- P1-P3 phone ------------------------------------------------------------------
at = render(phone=True)
if ok_run(at, "P1 phone render"):
    check(table_lists(at) == [1] and sum("<table" in b.value for b in at.markdown) == 1,
          f"P1: a phone builds only the first tab's table on first load ({table_lists(at)})")
    check(cards(at) == 0, "P3: no Expert Views cards while every expander is closed")
    check(any("AI Stock Expert Views (wl1" in e.label for e in at.expander),
          "P3: the Expert Views header is still shown on the visible tab")

    at.text_input(key="search_wl1").set_value("T1X01")
    ok_run(at, "P2 search")
    check(table_rows(at) == ["T1X01"], f"P2: search filters the visible tab ({table_rows(at)})")
    at.multiselect(key="ai_review_tickers_wl1").set_value(["T1X01"])
    ok_run(at, "P2 ai-review pick")

    at.session_state["main_tabs"] = "List 4"
    if ok_run(at, "P1 switch to List 4"):
        check(table_lists(at) == [4], f"P1: switching to List 4 builds only List 4 ({table_lists(at)})")
        check(at.session_state["search_wl1"] == "T1X01"
              and at.session_state["ai_review_tickers_wl1"] == ["T1X01"],
              "P2: List 1's search and AI-review pick survive while its tab is hidden")

    at.session_state["main_tabs"] = "List 1"
    if ok_run(at, "P2 back to List 1"):
        check(table_rows(at) == ["T1X01"]
              and at.multiselect(key="ai_review_tickers_wl1").value == ["T1X01"],
              "P2: back on List 1, still filtered and the AI-review pick is kept")

    for label, members in (("All Invested", [1, 2]), ("All Watchlist", [3, 4, 5])):
        at.session_state["main_tabs"] = label
        if ok_run(at, f"P1 {label}"):
            check(table_lists(at) == members, f"P1: {label} builds only its members' table ({table_lists(at)})")

    at.session_state["main_tabs"] = "List 1"
    ok_run(at, "P3 back to List 1")
    at.session_state["ev_exp_wl1"] = True
    if ok_run(at, "P3 open expander"):
        # One card: List 1 is still searched down to T1X01, and the expander
        # lists the filtered rows.
        check(cards(at) == 1, f"P3: opening the expander builds the visible tab's cards ({cards(at)})")

shutil.rmtree(APP, ignore_errors=True)
print("FAILURES:", fails)
sys.exit(fails)
