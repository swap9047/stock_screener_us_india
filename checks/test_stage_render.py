"""Stage Analysis columns in the rendered app: offline, from a throwaway copy of the repo.

Rendered with AppTest over a snapshot the app's own fetch_all_markets computed from
synthetic prices (yfinance stubbed, as in test_phone_render.py), with a stage_analysis.json
that covers all but one ticker:

  R1  the Stage, Stage Mom, CE and PEAD columns show by default, in that order, right
      after Sentiment; the three parts of the Stage cell (Prev Stage, Stage Conf, Stage
      Periods) are offered but hidden;
  R2  the cells carry the stored values, the Stage cell its hover text, and the ticker
      the upload does not cover shows dashes rather than "None";
  R3  a layout saved BEFORE these columns existed gets them once, after Sentiment -- and
      a column hidden afterwards stays hidden (the placement is recorded, not repeated);
  R4  the sidebar upload panel renders, and says what is stored.

AppTest has no file_uploader, so picking a file and pressing Import is covered by the
parse/match/merge checks in test_stage_analysis.py and by hand.
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


APP = tempfile.mkdtemp(prefix="stage_render_")
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

TICKERS = ["ACME", "ZED", "QUX"]
json.dump({"wl1": {"label": "List 1", "benchmark": "SPY"}}, open("markets.json", "w"))
json.dump({"wl1": TICKERS}, open("watchlist.json", "w"))
json.dump({"all_invested": ["wl1"], "all_watchlist": ["wl1"]}, open("watchlist_groups.json", "w"))
json.dump({t: {"index": "S&P 500", "benchmark": "SPY"} for t in TICKERS}, open("ticker_index.json", "w"))
json.dump({"meta": {"uploaded_at": "2026-10-25T12:00:00Z", "source": "all.csv"},
           "tickers": {
               "ACME": {"stage": "S2 Advancing", "stage_prev": "S4 Decline", "stage_conf": "Mature",
                        "stage_weeks": 24, "stage_mom": "Weakening ↓", "cheat_entry": "No", "pead": "Strong",
                        "uploaded": "2026-10-25"},
               "ZED": {"stage": "Unknown", "stage_prev": "Unknown", "stage_conf": None, "stage_weeks": None,
                       "stage_mom": "Weak ↓", "cheat_entry": "Yes", "pead": "No", "uploaded": "2026-10-25"},
           }}, open("stage_analysis.json", "w"))

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

import breadth_charts
from streamlit.testing.v1 import AppTest

breadth_charts.is_mobile = lambda ua: False


def render():
    at = AppTest.from_file(os.path.join(APP, "app.py"), default_timeout=600)
    at.session_state["authenticated"] = True
    return at


def run(at, label):
    at.run()
    if at.exception:
        check(False, f"{label}: {[str(e.value)[:300] for e in at.exception]}")
        return False
    return True


def first_table(at):
    return next(b.value for b in at.markdown if "<table" in b.value)


def headers(html):
    return [re.sub(r"<[^>]+>", "", h).strip() for h in re.findall(r"<th[^>]*>(.*?)</th>", html, re.S)]


def row_cells(html, ticker):
    row = next(r for r in re.findall(r"<tr>(.*?)</tr>", html, re.S) if re.search(rf">{ticker}<", r))
    return re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)


# --- R1 / R2 / R4: a fresh layout -------------------------------------------------
at = render()
if run(at, "fresh render"):
    html = first_table(at)
    cols = headers(html)
    # Header cells may carry an info icon after the label, so match on the start.
    def col_index(label):
        return next(i for i, h in enumerate(cols) if h == label or h.startswith(label + " "))
    s = col_index("Sentiment")
    order = [c for c in cols[s + 1:s + 5]]
    check(order[0].startswith("Stage") and order[1].startswith("Stage Mom") and order[2].startswith("CE")
          and order[3].startswith("PEAD"), f"R1: Stage, Stage Mom, CE, PEAD follow Sentiment ({[o.split(chr(32))[0] + (chr(32) + o.split(chr(32))[1] if o.startswith('Stage Mom') else '') for o in order]})")
    check(not any(c.startswith(("Prev Stage", "Stage Conf", "Stage Weeks")) for c in cols),
          "R1: Prev Stage / Stage Conf / Stage Weeks are hidden by default")

    # A cell's visible text: the <summary> of a hover-wrapped cell, else the cell itself.
    def shown(cell):
        m = re.search(r"<summary[^>]*>(.*?)</summary>", cell, re.S)
        return re.sub(r"<[^>]+>", "", m.group(1) if m else cell).strip()

    def stage_cells(ticker):
        """The four default Stage Analysis cells of a row, as displayed."""
        return [shown(c) for c in row_cells(html, ticker)[s + 1:s + 5]]

    check(stage_cells("ACME") == ["S2 Advancing", "Weakening ↓", "No", "Strong"],
          f"R2: ACME shows its stored stage, momentum, CE and PEAD ({stage_cells('ACME')})")
    check(stage_cells("ZED") == ["Unknown", "Weak ↓", "Yes", "No"],
          f"R2: an Unknown stage, and CE Yes, render ({stage_cells('ZED')})")
    check(stage_cells("QUX") == ["—", "—", "—", "—"],
          f"R2: the ticker no upload covers shows dashes, not 'None' or 'nan' ({stage_cells('QUX')})")
    raw_acme = " ".join(row_cells(html, "ACME")).replace("&#10;", "\n")
    check("Previous stage: S4 Decline" in raw_acme and "24 weeks (Mature)" in raw_acme
          and "Uploaded: 2026-10-25" in raw_acme,
          "R2: the Stage cell's hover carries the previous stage, how long it has run and the upload date")

    labels = [e.label for e in at.sidebar.expander]
    check("Stage Analysis upload" in labels, f"R4: the sidebar has the upload panel ({labels})")
    caption = " ".join(c.value for c in at.sidebar.caption)
    check("2 tickers stored" in caption and "2026-10-25" in caption and "all.csv" in caption,
          "R4: the panel says what is stored and from which upload")
    prefs = json.load(open("column_prefs.json"))
    check(prefs.get("stage_columns_added") is True, "R3: a fresh install records that the columns are placed")

# --- R3: a layout saved before the columns existed ---------------------------------------
old_order = ["company_name", "expert_take", "trend", "fundamentals", "pct_change_1d"]
json.dump({"order": old_order}, open("column_prefs.json", "w"))
at = render()
if run(at, "old layout render"):
    saved = json.load(open("column_prefs.json"))
    # The fundamentals loop adds its own columns after Sentiment too; the Stage ones go
    # in directly behind Sentiment, ahead of them, and nothing else moves.
    order = saved["order"]
    at_s = order.index("fundamentals")
    check(order[at_s + 1:at_s + 5] == ["stage", "stage_mom", "cheat_entry", "pead"]
          and [k for k in order if k not in ("stage", "stage_mom", "cheat_entry", "pead")][:4] == old_order[:4]
          and order[-1] == "pct_change_1d",
          f"R3: an old layout gets the four columns once, right after Sentiment ({order[:10]})")
    check(saved.get("stage_columns_added") is True, "R3: ...and the placement is recorded")

    picker = next(m for m in at.sidebar.multiselect if m.label == "Columns to show")
    picker.set_value([v for v in picker.value if v != "PEAD"])
    if run(at, "hide PEAD"):
        check("pead" not in json.load(open("column_prefs.json"))["order"], "R3: hiding PEAD saves")
    at2 = render()
    if run(at2, "render after hiding PEAD"):
        check("pead" not in json.load(open("column_prefs.json"))["order"]
              and not any(c.startswith("PEAD") for c in headers(first_table(at2))),
              "R3: a column hidden on purpose is not put back on the next run")

shutil.rmtree(APP, ignore_errors=True)
print("FAILURES:", fails)
sys.exit(fails)
