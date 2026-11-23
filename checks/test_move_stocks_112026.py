"""The "Move stocks between watchlists" grid (owner, 2026-11-20): every stock in one table, a tick box per
watchlist, ONE save.

  P  watchlist_moves.plan_changes / row_order: what a tick means (pure, offline)
  R  the rendered dialog (AppTest, invented stocks, synthetic prices): its rows and columns, ticks held in the
     session and NOT saved, the Save label counting the changes
  S  the save (`_apply_membership_changes`): lists, snapshot rows and what a fetch costs, run through AppTest
     with app.py executed as the script so its own functions are used

A dialog is a Streamlit fragment, and AppTest cannot rerun one: it can open the dialog (a click on the sidebar
button) and read what it drew, so the ticks are injected as the editor's own state on that opening run.
"""

import json
import os
import shutil
import sys
import tempfile
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import watchlist_moves as wm

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- P: the plan --------------------------------------------------------------------------------
M = ["in_hold", "us_hold", "in_watch", "sold"]
WL = {"in_hold": ["ZIN1.NS", "ZIN2.NS", "ZBO3.BO"], "us_hold": ["ZUS1", "ZUS2"],
      "in_watch": ["ZIN2.NS", "ZIN4.NS"], "sold": ["ZIN5.NS"], "extra_unregistered": ["ZX"]}
ticks0 = {t: set(ms) for t, ms in wm.current_ticks(WL, M).items()}

check(set(ticks0) == {"ZIN1.NS", "ZIN2.NS", "ZBO3.BO", "ZUS1", "ZUS2", "ZIN4.NS", "ZIN5.NS"}
      and ticks0["ZIN2.NS"] == {"in_hold", "in_watch"},
      "P1: every stock of the registered lists, with every list it is in (an unregistered key is not a column)")
check(wm.row_order(list(ticks0)) == ["ZBO3.BO", "ZIN1.NS", "ZIN2.NS", "ZIN4.NS", "ZIN5.NS", "ZUS1", "ZUS2"],
      "P2: India (.NS and .BO) first, then US, each alphabetical")
check(wm.row_order(list(ticks0) + ["ZNEW.NS"], new=["ZNEW.NS"])[0] == "ZNEW.NS",
      "P2: a stock added in this session is the first row")

p = wm.plan_changes(WL, M, ticks0)
check(p["changed"] == [] and p["moves"] == {} and p["orphans"] == [] and p["lists"] == WL,
      "P3 unchanged: ticking nothing changes nothing, and every other key passes through")

t = {k: set(v) for k, v in ticks0.items()}
t["ZIN1.NS"] = {"sold"}                       # moved: in_hold -> sold
t["ZIN2.NS"] = {"in_hold", "in_watch", "sold"}  # also ticked into sold
p = wm.plan_changes(WL, M, t)
check(p["lists"]["in_hold"] == ["ZIN2.NS", "ZBO3.BO"] and p["lists"]["sold"] == ["ZIN5.NS", "ZIN1.NS", "ZIN2.NS"],
      f"P4: a list keeps its order; stocks newly ticked go to the end in grid order ({p['lists']['in_hold']}, {p['lists']['sold']})")
# Parenthesised: `A and B is C or B == C` passed on the last test alone, so
# `changed` was never really checked.
check(p["changed"] == ["in_hold", "sold"]
      and (p["lists"]["in_watch"] is WL["in_watch"] or p["lists"]["in_watch"] == WL["in_watch"]),
      f"P5: only the lists that changed are named ({p['changed']}); the others are as they were")
check(p["moves"] == {"ZIN1.NS": {"add": ["sold"], "remove": ["in_hold"]}, "ZIN2.NS": {"add": ["sold"], "remove": []}},
      f"P6: each stock's move ({p['moves']})")
check(WL["in_hold"] == ["ZIN1.NS", "ZIN2.NS", "ZBO3.BO"] and ticks0["ZIN1.NS"] == {"in_hold"},
      "P7: the arguments are not modified")
check(p["lists"]["extra_unregistered"] == ["ZX"], "P7: a key outside the registry is carried through untouched")

t = {k: set(v) for k, v in ticks0.items()}
t["ZIN4.NS"] = set()
t["ZUS1"] = set()
t["ZUS2"] = {"us_hold"}
p = wm.plan_changes(WL, M, t)
check(p["orphans"] == ["ZIN4.NS", "ZUS1"] and "ZIN4.NS" not in p["lists"]["in_watch"] and "ZUS1" not in p["lists"]["us_hold"],
      f"P8: an existing stock ticked nowhere is an orphan (India first) and leaves its lists ({p['orphans']})")

t = {k: set(v) for k, v in ticks0.items()}
t["ZNEW1.NS"] = {"sold", "in_watch"}
t["ZNEW2"] = set()
p = wm.plan_changes(WL, M, t, new=["ZNEW1.NS", "ZNEW2"])
check(p["added"] == ["ZNEW1.NS"] and p["skipped_new"] == ["ZNEW2"] and p["orphans"] == []
      and p["lists"]["sold"][-1] == "ZNEW1.NS" and p["lists"]["in_watch"][-1] == "ZNEW1.NS"
      and not any("ZNEW2" in v for k, v in p["lists"].items() if k in M),
      f"P9: a new stock ticked somewhere joins those lists; one ticked nowhere is skipped, never an orphan ({p['added']}, {p['skipped_new']})")

t = {k: set(v) for k, v in ticks0.items()}
t["zin1.ns"] = {"sold"}
t["ZIN1.NS"] = {"sold"}
check(wm.plan_changes(WL, M, t)["lists"]["sold"].count("ZIN1.NS") == 1, "P10: a stock is never listed twice")
check(wm.parse_tickers(" znew1.ns, ZUS9;zbo3.bo\nznew1.NS ,, ") == ["ZNEW1.NS", "ZUS9", "ZBO3.BO"],
      "P11: the add box takes commas, semicolons, spaces and new lines; upper-case; no repeats")
check(wm.describe_move({"add": ["sold"], "remove": ["in_hold"]}, {"sold": "Sold", "in_hold": "India Hold"}) == "+ Sold  − India Hold",
      "P12: the review line")

# --- R / S: a throwaway app with invented stocks --------------------------------------------------
APP = tempfile.mkdtemp(prefix="move_stocks_")
shutil.rmtree(APP)
shutil.copytree(REPO, APP, ignore=shutil.ignore_patterns(
    ".git", "*.json", ".env", "__pycache__", "data", "checks", "docs", ".claude", ".agents", ".github", ".devcontainer"))
os.chdir(APP)
sys.path.insert(0, APP)
for k in ("DATA_REPO_TOKEN", "GITHUB_TOKEN", "DISCORD_WEBHOOK_URL", "GEMINI_API_KEY", "AUTH_USERNAME", "AUTH_PASSWORD"):
    os.environ[k] = ""
os.environ["SKIP_GITHUB_PULL"] = "1"

REG = {"in_hold": {"label": "India Hold", "benchmark": "^CRSLDX"}, "us_hold": {"label": "US Hold", "benchmark": "SPY"},
       "in_watch": {"label": "India Watch", "benchmark": "^CRSLDX"}, "sold": {"label": "Sold", "benchmark": "^CRSLDX"}}
LISTS = {"in_hold": ["ZIN1.NS", "ZIN2.NS", "ZBO3.BO"], "us_hold": ["ZUS1", "ZUS2"],
         "in_watch": ["ZIN2.NS", "ZIN4.NS"], "sold": ["ZIN5.NS"]}
ALL = sorted({t for v in LISTS.values() for t in v})
json.dump(REG, open("markets.json", "w"))
json.dump(LISTS, open("watchlist.json", "w"))
json.dump({"all_invested": ["in_hold", "us_hold"], "all_watchlist": ["in_watch", "sold"]}, open("watchlist_groups.json", "w"))
json.dump({**{t: {"index": "Nifty 500", "benchmark": "^CRSLDX"} for t in ALL + ["ZNEW1.NS"] if t.endswith((".NS", ".BO"))},
           **{t: {"index": "S&P 500", "benchmark": "SPY"} for t in ALL if not t.endswith((".NS", ".BO"))}},
          open("ticker_index.json", "w"))

import numpy as np
import pandas as pd
import yfinance as yf

DATES = pd.bdate_range(end="2026-10-16", periods=400)
FETCHED = []


def _series(symbol):
    rng = np.random.default_rng(zlib.crc32(symbol.encode()))
    close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.02, len(DATES))))
    return pd.DataFrame({"Open": close * 0.995, "High": close * 1.01, "Low": close * 0.99, "Close": close,
                         "Volume": rng.integers(100_000, 5_000_000, len(DATES))}, index=DATES)


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


def _download(symbols, **kw):
    symbols = symbols if isinstance(symbols, (list, tuple)) else symbols.split()
    FETCHED.extend(symbols)
    return pd.concat({s: _series(s) for s in symbols}, axis=1)


yf.download = _download
yf.Ticker = _Ticker

import stock_data as sd

_settings = sd.load_settings()
_, _as_of, _per_market = sd.fetch_all_markets(settings=_settings)
sd.save_data_snapshot(_as_of, _per_market, settings=_settings)
check(sd.snapshot_is_usable(sd.load_data_snapshot(), sd.load_watchlists(), _settings), "fixture: the snapshot is usable")

from streamlit.testing.v1 import AppTest


def opened(edits=None):
    """The app after a click on the sidebar button; `edits` = {ticker: {column label: bool}} is injected as the
    grid's own edit state (row numbers from the grid's order)."""
    at = AppTest.from_file(os.path.join(APP, "app.py"), default_timeout=600)
    at.session_state["authenticated"] = True
    at.run()
    if edits:
        order = wm.row_order(ALL)
        at.session_state["mv_editor_1000_"] = {"edited_rows": {order.index(t): e for t, e in edits.items()},
                                               "added_rows": [], "deleted_rows": []}
    at.sidebar.button(key="mv_open").click().run()
    return at


def grid(at):
    got = [e for e in at.get("dataframe") if str(e.key).startswith("mv_editor_")]
    return got[0].value if got else None


raw_before = Path("watchlist.json").read_bytes()
at = opened()
check(not at.exception, f"R1: the dialog opens without an error {[str(e.value)[:150] for e in at.exception]}")
df = grid(at)
check(df is not None and list(df["Ticker"]) == wm.row_order(ALL),
      f"R2: one row per stock, India first then US ({list(df['Ticker']) if df is not None else None})")
check(list(df.columns) == ["Ticker", "Company"] + [v["label"] for v in REG.values()],
      f"R3: a column per registered watchlist, in the registry's order, and no combined tab ({list(df.columns)})")
row = df[df["Ticker"] == "ZIN2.NS"].iloc[0]
check(bool(row["India Hold"]) and bool(row["India Watch"]) and not row["Sold"] and not row["US Hold"],
      "R4: a stock in two lists has both boxes ticked")
check(str(row["Company"]).startswith("Company"), "R4: the company name comes from the snapshot")
check([b.label for b in at.button if b.key == "mv_save"] == ["Save all changes (0)"]
      and [b.disabled for b in at.button if b.key == "mv_save"] == [True],
      "R5: with nothing changed, Save is disabled and says 0")
check([b.label for b in at.button if b.key and b.key.startswith("mv_add_btn")] == ["Add"]
      and any(t.key and t.key.startswith("mv_add_text") for t in at.text_input), "R6: there is an add box")

at = opened({"ZIN1.NS": {"India Hold": False, "Sold": True}, "ZUS2": {"US Hold": False}})
check(sorted(at.session_state["mv_ticks"]["ZIN1.NS"]) == ["sold"] and at.session_state["mv_ticks"]["ZUS2"] == set(),
      "R7: the ticks made in the grid are held in the session")
check([b.label for b in at.button if b.key == "mv_save"] == ["Save all changes (2)"]
      and [b.disabled for b in at.button if b.key == "mv_save"] == [False],
      "R8: Save counts the stocks changed, and is enabled")
check(Path("watchlist.json").read_bytes() == raw_before,
      "R9: ticking saved nothing -- the lists on disk are exactly as they were (no saving in the background)")
check(not any(e.key == "mv_confirm_go" for e in at.button), "R10: the removal confirmation is not shown before Save is pressed")

# a second visit starts from the saved lists, not from the first visit's ticks
at2 = opened()
check(sorted(at2.session_state["mv_ticks"]["ZIN1.NS"]) == ["in_hold"], "R11: opening the grid again starts from the saved lists")

# --- S: the save ------------------------------------------------------------------------------------
SAVE = '''
import os, json
g = {"__name__": "__main__", "__file__": os.path.join(os.getcwd(), "app.py")}
exec(compile(open(g["__file__"]).read(), g["__file__"], "exec"), g)
import watchlist_moves as wm
reg = g["load_markets_registry"](); wl = g["load_watchlists"]()
markets = [m for m in reg if m in wl]
ticks = {t: set(ms) for t, ms in wm.current_ticks(wl, markets).items()}
for t, ms in json.load(open("_ticks.json")).items():
    ticks[t] = set(ms)
plan = wm.plan_changes(wl, markets, ticks, json.load(open("_new.json")))
ok, text = g["_apply_membership_changes"](plan, {m: reg[m]["label"] for m in markets})
open("_result.json", "w").write(json.dumps({"ok": ok, "text": text, "changed": plan["changed"]}))
'''


def save(ticks, new=()):
    json.dump({t: sorted(ms) for t, ms in ticks.items()}, open("_ticks.json", "w"))
    json.dump(list(new), open("_new.json", "w"))
    FETCHED.clear()
    at = AppTest.from_string(SAVE, default_timeout=600)
    at.session_state["authenticated"] = True
    at.run()
    res = json.load(open("_result.json")) if os.path.exists("_result.json") else {}
    if os.path.exists("_result.json"):
        os.remove("_result.json")
    return at, res


def snap_rows():
    s = sd.load_data_snapshot()
    return {m: {r["ticker"]: r for r in rows} for m, rows in s["per_market"].items()}


at, res = save({"ZIN1.NS": {"sold"}, "ZIN2.NS": {"in_hold", "in_watch", "sold"}})
saved = json.load(open("watchlist.json"))
check(not at.exception and res.get("ok") is True, f"S1: the save runs {[str(e.value)[:150] for e in at.exception]} {res}")
check(saved["in_hold"] == ["ZIN2.NS", "ZBO3.BO"] and saved["sold"] == ["ZIN5.NS", "ZIN1.NS", "ZIN2.NS"]
      and saved["us_hold"] == LISTS["us_hold"] and saved["in_watch"] == LISTS["in_watch"],
      f"S2: the saved lists are the planned ones; lists nothing touched are as they were ({saved})")
rows = snap_rows()
check(set(rows["sold"]) == {"ZIN5.NS", "ZIN1.NS", "ZIN2.NS"} and set(rows["in_hold"]) == {"ZIN2.NS", "ZBO3.BO"}
      and all(r["market"] == "sold" for r in rows["sold"].values()),
      "S3: each changed list has its snapshot rows, tagged with that list (alerts scope a rule by the tag)")
check(set(rows["us_hold"]) == set(LISTS["us_hold"]) and set(rows["in_watch"]) == set(LISTS["in_watch"]),
      "S3: the lists that did not change keep their rows")
check(not FETCHED, f"S4: moving stocks that already have rows costs no Yahoo request ({FETCHED})")
check(sd.snapshot_is_usable(sd.load_data_snapshot(), sd.load_watchlists(), sd.load_settings()),
      "S5: the snapshot still covers every list, so no visitor gets a live fetch")

at, res = save({"ZIN4.NS": set(), "ZNEW1.NS": {"sold", "in_watch"}, "ZNEW2.NS": set()}, new=["ZNEW1.NS", "ZNEW2.NS"])
saved = json.load(open("watchlist.json"))
rows = snap_rows()
check(not at.exception and res.get("ok") is True and "ZIN4.NS" not in sum(saved.values(), []),
      f"S6: a stock ticked nowhere is removed from every list ({res.get('text')})")
check(saved["sold"][-1] == "ZNEW1.NS" and saved["in_watch"][-1] == "ZNEW1.NS" and "ZNEW2.NS" not in sum(saved.values(), []),
      "S7: a new stock joins the lists ticked for it; one ticked nowhere is not added")
check("ZNEW1.NS" in FETCHED and "ZNEW1.NS" in rows["sold"] and "ZNEW1.NS" in rows["in_watch"] and "ZIN4.NS" not in rows["in_watch"],
      "S8: only the new stock is fetched, and its row is in both lists' snapshot blocks; the removed stock's row is gone")
check("1 removed from the app" in res["text"] and "1 new" in res["text"], f"S9: the message counts what happened ({res['text']})")

# the saved world renders: every tab, no error
at = AppTest.from_file(os.path.join(APP, "app.py"), default_timeout=600)
at.session_state["authenticated"] = True
at.run()
tables = " ".join(b.value for b in at.markdown if "<table" in b.value)
check(not at.exception and "ZNEW1.NS" in tables and "ZIN4.NS" not in tables,
      f"S10: after saving, the tabs show the new stock and not the removed one {[str(e.value)[:120] for e in at.exception]}")

# --- static: the confirmation, the single save, the sidebar button -------------------------------------
src = (REPO / "app.py").read_text()
body = src.split("def move_stocks_dialog():", 1)[1].split("\nOPERATOR_CHOICES", 1)[0]
check("will be REMOVED from the app" in body and 'key="mv_confirm_go"' in body and "ss[\"mv_confirm\"] = True" in body,
      "C1: stocks ticked nowhere are confirmed before they are removed")
check(body.count("_apply_membership_changes(") == 1 and body.count("save_watchlists(") == 0,
      "C2: the only write is the Save button's (the dialog saves nothing on a tick)")
check('"mv_open"' in src and "move_stocks_dialog()" in src.split('key="mv_open"', 1)[1][:200],
      "C3: the sidebar button opens the dialog")

shutil.rmtree(APP, ignore_errors=True)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
