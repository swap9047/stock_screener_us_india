"""UI fixes of 2026-11-22, offline: a rendered app copy plus the filter engine.

  C1  a snapshot row without rsi14_daily no longer raises in the filter loop.
  C2  a tab body that raises shows an error in that tab; the other tabs, Alert
      Rules and the end-of-run code still run (_Contained). st.rerun() and
      st.stop() (BaseException subclasses) pass through, any Exception stops.
  C3  a sidebar panel that raises shows an error in the sidebar; the tabs render.
  N1  Ticker Notes Clear re-seeds its widgets, so a following Save cannot write
      the cleared note and flag back.
  S1  a saved sort whose label is not an option right now (fundamental columns
      hidden) is kept, and shows again once the label is back; a first visit
      writes no sort keys; a change by the user is saved.
  S2  opening the News tab on a fresh setup does not write news_watchlist_scope.
  M1  a save's messages ("doesn't return any price data -- dropping it") are on
      the page after the save's rerun.
  R1  Delete rule asks first: alerts_config.json is unchanged until Confirm, and
      the confirm names a rule that refers to it; Cancel keeps the rule.
  E1  a render no longer deletes (or pushes the deletion of) an Expert View for
      a stock missing from this container's watchlist.json.
  B7  a torn market_breadth.json shows a warning, not a crashed News tab.
  O8  the dashboard controls sit above the tab strip.
  F*  typed condition values: 1,000,000 / 5% / 10L / 2Cr / 3M are numbers, both in
      the builder's parser and in filters at compare time, and a numeric metric
      against text that is not a number fails closed instead of comparing as text.

The app is rendered with AppTest from a throwaway copy of the repo, over a
snapshot that the app's own fetch_all_markets computed from synthetic prices
(yfinance stubbed, no network), as test_phone_render.py does: 7 watchlists x 3
invented tickers (T1X00 ..., ".NS" on the India ones).
"""

import ast
import glob
import json
import os
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


# --- F: the filter engine (no app needed) ------------------------------------------
sys.path.insert(0, str(REPO))
import filters  # noqa: E402

# Independent expectations: what each text means as a number.
_EXPECT = {
    "45": 45.0, "-1.5": -1.5, "1,000,000": 1e6, "10,00,000": 1e6, "1,00,000": 1e5,
    "5%": 5.0, "-2.5 %": -2.5, "10L": 1e6, "10 lakh": 1e6, "2Cr": 2e7, "1.5 crore": 1.5e7,
    "3K": 3e3, "3M": 3e6, "2B": 2e9, "$1,000": 1e3,
    "abc": None, "1,2": None, "12,34": None, "10Lx": None, "": None, "yes": None, "2026-11-01": None,
}
_got = {t: filters.parse_number_text(t) for t in _EXPECT}
check(all((_got[t] is None) == (v is None) and (v is None or abs(_got[t] - v) < 1e-6)
          for t, v in _EXPECT.items()),
      f"F1: parse_number_text reads separators, %, K/M/B, L/lakh, Cr/crore and refuses the rest "
      f"({ {t: g for t, g in _got.items() if (g is None) != (_EXPECT[t] is None) or (g is not None and abs(g - _EXPECT[t]) > 1e-6)} })")


def _cond(metric, opr, value):
    return {"metric_a": metric, "operator": opr, "compare_type": "value", "value": value}


_row = {"avg_volume_10d": 2_500_000, "perf_1m": 7.5, "tech_uptrend": True, "trend": "Uptrend",
        "data_end": "2026-11-20", "interested": False}
_cases = [
    # (condition, expected) -- expected worked out by hand from _row.
    (_cond("avg_volume_10d", ">", "10L"), True),          # 2.5M > 1M
    (_cond("avg_volume_10d", ">", "1Cr"), False),         # 2.5M > 10M
    (_cond("avg_volume_10d", ">", "1,000,000"), True),
    (_cond("avg_volume_10d", "<", "3M"), True),
    (_cond("perf_1m", ">", "5%"), True),                  # stored as percent: 7.5 > 5
    (_cond("perf_1m", ">", "10%"), False),
    # Lexicographically "2500000" > "abc" is False and "2500000" < "abc" True: both
    # must now be False (fail closed), not whatever string order says.
    (_cond("avg_volume_10d", "<", "abc"), False),
    (_cond("avg_volume_10d", ">", "abc"), False),
    (_cond("avg_volume_10d", "==", "abc"), False),
    # Unchanged behaviour: yes/no words, categorical text, ISO dates as text.
    (_cond("tech_uptrend", "==", "Yes"), True),
    (_cond("tech_uptrend", "==", "No"), False),
    (_cond("interested", "==", "No"), True),
    (_cond("trend", "==", "uptrend"), True),
    (_cond("data_end", ">", "2026-11-01"), True),
    (_cond("data_end", "<", "2026-11-01"), False),
    ({"metric_a": "trend", "operator": "in", "compare_type": "value", "value": ["Uptrend", "Mixed"]}, True),
]
_bad = [(c["metric_a"], c["operator"], c["value"]) for c, want in _cases if filters.passes_filter(_row, c) != want]
check(not _bad, f"F2: passes_filter on typed values matches the hand-worked answers ({_bad})")

# The builder's parser in app.py, lifted out by AST so it runs without Streamlit.
_app_src = (REPO / "app.py").read_text()
_tree = ast.parse(_app_src)


def _lift(name, env):
    node = next(n for n in _tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), env)
    return env[name]


_pfvt = _lift("parse_filter_value_text", {"parse_number_text": filters.parse_number_text})
check(_pfvt("10L") == 1e6 and _pfvt("5%") == 5.0 and _pfvt(" 1,000 ") == 1000.0
      and _pfvt("Yes") == "Yes" and _pfvt("") == "",
      "F3: the condition builder saves typed numbers with units as numbers, words as words")


# --- C2 unit: _Contained lets st.rerun()/st.stop() through and stops Exception ----
class _StubSt:
    def __init__(self):
        self.errors = []

    def error(self, msg):
        self.errors.append(msg)

    def expander(self, *a, **k):
        class _Cm:
            def __enter__(s):
                return s

            def __exit__(s, *e):
                return False
        return _Cm()

    def code(self, *a, **k):
        pass


import traceback  # noqa: E402

from streamlit.runtime.scriptrunner_utils.exceptions import RerunException, StopException  # noqa: E402

_stub = _StubSt()
_Contained = _lift("_Contained", {"st": _stub, "traceback": traceback, "print": lambda *a, **k: None})
for _exc in (RerunException(None), StopException()):
    try:
        with _Contained("x"):
            raise _exc
        check(False, f"C2u: {type(_exc).__name__} passes through _Contained")
    except type(_exc):
        check(True, f"C2u: {type(_exc).__name__} passes through _Contained")
with _Contained("The X tab") as _c:
    raise KeyError("rsi14_daily")
check(_c.failed and _stub.errors and "The X tab" in _stub.errors[0],
      "C2u: an Exception is stopped and shown as an error naming the part")

# The end-of-run config push sits at module level AFTER every contained tab loop,
# so a tab that fails can no longer cut the run short before it.
_top = _tree.body
_push_at = max(i for i, n in enumerate(_top) if isinstance(n, ast.If) and "unpushed_config_files" in ast.unparse(n))


def _head(n):
    """The with-statement line of a top-level tab block (inside the loop for a for)."""
    w = n.body[0] if isinstance(n, ast.For) else n
    return ast.unparse(w).split("\n")[0] if isinstance(w, ast.With) else ""


_tab_blocks = [i for i, n in enumerate(_top) if isinstance(n, (ast.With, ast.For))
               and any(t in _head(n) for t in ("tab_news", "tab_alerts", "combined_tabs[", "market_tabs["))]
check(len(_tab_blocks) == 4 and all(i < _push_at for i in _tab_blocks)
      and all("_Contained(" in _head(_top[i]) for i in _tab_blocks),
      f"C2s: the 2 tab loops, News and Alert Rules are each contained, before the end-of-run push "
      f"({[_head(_top[i]) for i in _tab_blocks]})")


# --- the app fixture ---------------------------------------------------------------
APP = tempfile.mkdtemp(prefix="team_ui_")
shutil.rmtree(APP)
shutil.copytree(REPO, APP, ignore=shutil.ignore_patterns(
    ".git", "*.json", ".env", "__pycache__", "data", "checks", "docs",
    ".claude", ".agents", ".github", ".devcontainer"))
os.chdir(APP)
sys.path.remove(str(REPO))
sys.path.insert(0, APP)
for _m in [m for m, mod in list(sys.modules.items())
           if getattr(mod, "__file__", None) and os.path.dirname(os.path.abspath(mod.__file__)) == str(REPO)]:
    sys.modules.pop(_m)      # filters (and what it imported) from the repo, not the copy
import filters  # noqa: E402,F811  -- the copy's module, the one the app imports
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
_views = {t: {"verdict": "HOLD", "headline": "Invented headline", "technical_summary": "x",
              "catalyst_summary": "x", "actionable_take": "x", "as_of": "2026-11-20T00:00:00+00:00",
              "news_source": "test", "model_used": "test"}
          for ts in tickers.values() for t in ts}
_views["ACME"] = dict(_views["T1X00"])      # E1: a view for a stock in no local watchlist
json.dump(_views, open("expert_views.json", "w"))
_rules = [
    {"id": "r0000001", "name": "Base rule", "scope": "ALL", "enabled": True,
     "conditions": [{"metric_a": "rsi14_daily", "operator": ">", "compare_type": "value", "value": 30, "logic": "AND"}],
     "schedule": {"type": "scheduled", "days": ["mon"], "time_et": "21:00"}},
    {"id": "r0000002", "name": "Depends on base", "scope": "ALL", "enabled": True,
     "conditions": [{"type": "rule", "rule_id": "r0000001", "logic": "AND"}],
     "schedule": {"type": "scheduled", "days": ["mon"], "time_et": "21:00"}},
]
json.dump(_rules, open("alerts_config.json", "w"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

DATES = pd.bdate_range(end="2026-11-20", periods=400)


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

import stock_data as sd  # noqa: E402

_settings = sd.load_settings()
_, _as_of, _per_market = sd.fetch_all_markets(settings=_settings)
sd.save_data_snapshot(_as_of, _per_market, settings=_settings)
check(sd.snapshot_is_usable(sd.load_data_snapshot(), sd.load_watchlists(), _settings),
      "fixture: the synthetic snapshot is usable (no live fetch during the render)")
BASE = os.path.join(APP, "_base")
os.makedirs(BASE)
for _f in glob.glob("*.json"):
    shutil.copy(_f, BASE)


def reset():
    """Every scenario starts from the same data files."""
    for f in glob.glob("*.json"):
        os.remove(f)
    for f in glob.glob(os.path.join(BASE, "*.json")):
        shutil.copy(f, ".")


import breadth_charts  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

breadth_charts.is_mobile = lambda ua: False


def render(tab=None):
    at = AppTest.from_file(os.path.join(APP, "app.py"), default_timeout=600)
    at.session_state["authenticated"] = True
    if tab:
        at.session_state["main_tabs"] = tab
    return at


def ok_run(at, label):
    at.run()
    if at.exception:
        check(False, f"{label}: {[str(e.value)[:200] for e in at.exception]}")
        return False
    return True


def tables(at):
    return sum("<table" in b.value for b in at.markdown)


def messages(at, where=None):
    root = at.sidebar if where == "sidebar" else at
    return [e.value for e in root.error] + [w.value for w in root.warning] + [s.value for s in root.success]


def jload(name, default=None):
    return json.load(open(name)) if os.path.exists(name) else default


# --- C1 a row without rsi14_daily --------------------------------------------------
reset()
_snap = jload("data_snapshot.json")
_snap["per_market"]["wl3"][0].pop("rsi14_daily")
json.dump(_snap, open("data_snapshot.json", "w"))
at = render("List 3")
if ok_run(at, "C1 render"):
    check(tables(at) == 1 and not at.error,
          f"C1: the tab with a row lacking rsi14_daily builds its table, no error ({tables(at)}, {len(at.error)})")

# --- C2 a tab that raises is contained -------------------------------------------------
reset()
_orig_gmf = filters.get_market_filters


def _boom(market):
    if market == "wl3":
        raise KeyError("invented failure")
    return _orig_gmf(market)


filters.get_market_filters = _boom
try:
    at = render("List 4")
    if ok_run(at, "C2 render"):
        _errs = [e.value for e in at.error]
        check(any("The List 3 tab could not be shown" in e for e in _errs),
              f"C2: the failing tab shows an error naming it ({_errs[:2]})")
        check(tables(at) == 1, f"C2: the tab on screen (List 4) still builds its table ({tables(at)})")
        check(any("Alert rules" in s.value for s in at.subheader),
              "C2: Alert Rules, after the failing tab, still renders")
        check(len(at.tabs) >= 11, "C2: every tab is still there")
finally:
    filters.get_market_filters = _orig_gmf

# --- C3 a sidebar panel that raises is contained ---------------------------------------
reset()
import model_settings  # noqa: E402

_orig_skip = model_settings.skipped_models


def _boom2(*a, **k):
    raise ValueError("invented panel failure")


model_settings.skipped_models = _boom2
try:
    at = render("List 1")
    at.session_state["model_settings_exp"] = True
    if ok_run(at, "C3 render"):
        check(any("Settings · Models could not be shown" in e.value for e in at.sidebar.error),
              "C3: the failing sidebar panel shows its error in the sidebar")
        check(tables(at) == 1 and any("Alert rules" in s.value for s in at.subheader),
              "C3: the tabs render below a failed sidebar panel")
finally:
    model_settings.skipped_models = _orig_skip

# --- N1 Ticker Notes Clear then Save ------------------------------------------------------
reset()
at = render()
if ok_run(at, "N1 render"):
    at.sidebar.selectbox(key="tn_picked_ticker").set_value("T1X01")
    ok_run(at, "N1 pick")
    at.sidebar.text_area(key="tn_note_T1X01").set_value("invented note")
    at.sidebar.selectbox(key="tn_flag_T1X01").set_value("Red")
    ok_run(at, "N1 type")
    at.sidebar.button(key="tn_save_T1X01").click()
    ok_run(at, "N1 save")
    check(jload("ticker_notes.json", {}).get("T1X01") == {"note": "invented note", "flag": "Red"},
          "N1 fixture: Save stores the note and flag")
    at.sidebar.button(key="tn_clear_T1X01").click()
    ok_run(at, "N1 clear")
    check(at.sidebar.text_area(key="tn_note_T1X01").value == ""
          and at.sidebar.selectbox(key="tn_flag_T1X01").value == "(none)",
          "N1: after Clear the note box and the flag show the cleared values")
    at.sidebar.button(key="tn_save_T1X01").click()
    ok_run(at, "N1 save again")
    check("T1X01" not in jload("ticker_notes.json", {}),
          f"N1: a Save after Clear does not write the old note back ({jload('ticker_notes.json', {})})")

# --- S1 a saved sort survives its label disappearing -----------------------------------
reset()
_prefs = {"order": None, "sort_by_wl1_1": "Sentiment", "sort_dir_wl1_1": "↓",
          "sort_by_wl1_2": "% Chg", "sort_dir_wl1_2": "↓"}
_prefs.pop("order")
json.dump(_prefs, open("column_prefs.json", "w"))
_s = sd.load_settings()
_s["show_fundamental_columns"] = False
sd.save_settings(_s)
at = render("List 1")
if ok_run(at, "S1 render, fundamentals off"):
    _p = jload("column_prefs.json", {})
    check(_p.get("sort_by_wl1_1") == "Sentiment" and _p.get("sort_by_wl1_2") == "% Chg",
          f"S1: hiding the Sentiment column keeps the saved sort ({_p.get('sort_by_wl1_1')}, {_p.get('sort_by_wl1_2')})")
    check(at.sidebar.selectbox(key="sort_field_wl1_1").value == "(default order)",
          "S1: the control shows the empty option while Sentiment is not a column")
    # Back on, in the SAME session: the widget must pick the saved label up again.
    _s["show_fundamental_columns"] = True
    sd.save_settings(_s)
    for _k in ("show_fundamental_columns_toggle", "dash_show_fundamental_columns_toggle"):
        at.session_state[_k] = True
    ok_run(at, "S1 fundamentals back on")
    _p = jload("column_prefs.json", {})
    check(_p.get("sort_by_wl1_1") == "Sentiment"
          and at.sidebar.selectbox(key="sort_field_wl1_1").value == "Sentiment",
          f"S1: with the column back, the control shows the saved sort again ({at.sidebar.selectbox(key='sort_field_wl1_1').value})")

reset()
at = render("List 2")
if ok_run(at, "S1 first visit"):
    _p = jload("column_prefs.json", {})
    check(not [k for k in _p if k.startswith("sort_")],
          f"S1: a first visit writes no sort keys ({[k for k in _p if k.startswith('sort_')]})")
    at.sidebar.selectbox(key="sort_field_wl2_1").set_value("Ticker")
    ok_run(at, "S1 change")
    _p = jload("column_prefs.json", {})
    check(_p.get("sort_by_wl2_1") == "Ticker" and _p.get("sort_dir_wl2_1") == "↑",
          f"S1: a sort the user picks is saved ({_p.get('sort_by_wl2_1')}, {_p.get('sort_dir_wl2_1')})")

# --- S2 the News scope is not written on a first visit -----------------------------------
reset()
_before = jload("settings.json", {})
at = render("News")
if ok_run(at, "S2 News"):
    check("news_watchlist_scope" not in jload("settings.json", {}) or
          jload("settings.json", {}).get("news_watchlist_scope") == _before.get("news_watchlist_scope"),
          "S2: opening the News tab does not save the default scope")
    _sel = at.multiselect(key="news_scope_select")
    check(bool(_sel.value), f"S2: the scope box still shows the default group ({_sel.value})")
    # B7 is checked below on its own fixture.

# --- B7 a torn market_breadth.json ------------------------------------------------------------
reset()
open("market_breadth.json", "w").write("{")
at = render("News")
if ok_run(at, "B7 News with a torn breadth file"):
    check(any("market_breadth.json" in w.value and "could not be read" in w.value for w in at.warning),
          "B7: the News tab says the breadth file could not be read")
    check(not any("could not be shown" in e.value for e in at.error),
          "B7: the News tab is not a contained crash")

# --- M1 save messages survive the rerun ------------------------------------------------------
reset()
import stock_data  # noqa: E402

_orig_vt = stock_data.validate_ticker
stock_data.validate_ticker = lambda t: False if t == "BADX" else True
try:
    at = render("List 1")
    if ok_run(at, "M1 render"):
        at.session_state["watchlist_editor_wl1"] = {"edited_rows": {}, "added_rows": [{"Ticker": "BADX"}],
                                                    "deleted_rows": []}
        at.button(key="save_watchlist_wl1").click()
        ok_run(at, "M1 save")
        check("BADX" not in jload("watchlist.json")["wl1"], "M1 fixture: the bad ticker is dropped")
        _m = messages(at)
        check(any("BADX" in m and "dropping it" in m for m in _m),
              f"M1: the 'dropping it' error is on the page after the rerun ({_m[:3]})")
        check(any(m.startswith("Saved List 1") for m in _m), "M1: the 'Saved' message is too")
        ok_run(at, "M1 next run")
        check(not any("BADX" in m for m in messages(at)), "M1: the messages are shown once")
finally:
    stock_data.validate_ticker = _orig_vt

# --- R1 Delete rule asks first ---------------------------------------------------------------
reset()
at = render("Alert Rules")
at.session_state["rule_exp_r0000001"] = True
if ok_run(at, "R1 render"):
    _before = open("alerts_config.json").read()
    at.button(key="del_r0000001").click()
    ok_run(at, "R1 first click")
    check(open("alerts_config.json").read() == _before, "R1: the first click deletes nothing")
    _w = [w.value for w in at.warning if "Delete" in w.value]
    check(_w and "Depends on base" in _w[0], f"R1: the confirm names the rule that refers to it ({_w[:1]})")
    at.button(key="del_no_r0000001").click()
    ok_run(at, "R1 cancel")
    check(open("alerts_config.json").read() == _before
          and not any("cannot be undone" in w.value for w in at.warning),
          "R1: Cancel keeps the rule and closes the confirm")
    at.button(key="del_r0000001").click()
    ok_run(at, "R1 ask again")
    at.button(key="del_yes_r0000001").click()
    ok_run(at, "R1 confirm")
    check([r["id"] for r in jload("alerts_config.json")] == ["r0000002"], "R1: Confirm delete deletes it")

# --- E1 no render-time expert-views cleanup --------------------------------------------------
reset()
import github_sync  # noqa: E402

_calls = []
_orig_pjec = github_sync.push_json_entry_changes
github_sync.push_json_entry_changes = lambda *a, **k: _calls.append((a, k)) or (True, "")
try:
    at = render("List 1")
    if ok_run(at, "E1 render"):
        check("ACME" in jload("expert_views.json", {}),
              "E1: a view for a stock missing from the local watchlist.json is kept")
        check(not _calls, f"E1: no expert_views change is pushed by a render ({len(_calls)})")
finally:
    github_sync.push_json_entry_changes = _orig_pjec

# --- O8 the dashboard controls sit above the tabs ---------------------------------------------
reset()
at = render()
if ok_run(at, "O8 render"):
    _kids = list(at.main.children.values())
    _tab_at = next(i for i, c in enumerate(_kids) if c.type == "tab_container")
    _dash_at = next((i for i, c in enumerate(_kids)
                     if any(getattr(w, "key", None) == "dash_show_fundamental_columns_toggle"
                            for w in c.get("checkbox"))), None)
    check(_dash_at is not None and _dash_at < _tab_at,
          f"O8: 'Show fundamental columns' is drawn above the tab strip ({_dash_at} vs {_tab_at})")

shutil.rmtree(APP, ignore_errors=True)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
