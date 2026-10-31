"""The sidebar's Settings · Models (2026-10-31), rendered: offline, from a throwaway copy of the
repo (as test_market_read_render.py), over synthetic prices.

  P1  the expander is there but builds nothing while closed; open, it lists all ten steps, by
      group, with today's chains and thinking budgets, none marked custom;
  P2  picking a step shows one model picker and tries box per model (seeded from the step's chain),
      a thinking budget for a reasoning step (collation only 4k/8k), none for a search;
  P3  Add a model / Remove last change the number of rows; changing a model, its tries, its
      timeout (150 s unless changed) and the budget and pressing Save writes the chain to
      settings.json, marks the step custom and says it applies from the next run;
  P4  Default puts it back (the step no longer custom, the old keys at their defaults);
  P5  the old pickers are gone from the News tab and AI Analysis Controls, which point here;
  P6  the repository lists the models and Add puts a new one in the pickers;
  P7  Test with no Gemini key says so instead of failing.
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
for k in [k for k in os.environ if k.startswith("GEMINI_API_KEY")]:
    os.environ[k] = ""          # every key name, not just the first: a key set here would make Test a real call
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


from streamlit.testing.v1 import AppTest

OPEN = "model_settings_exp"


def settings_file():
    return json.load(open(os.path.join(APP, "settings.json")))


def render(open_panel=True, tab=None):
    at = AppTest.from_file(os.path.join(APP, "app.py"), default_timeout=600)
    at.session_state["authenticated"] = True
    if tab:
        at.session_state["main_tabs"] = tab
    if open_panel:
        at.session_state[OPEN] = True
    at.run()
    if at.exception:
        check(False, f"render: {[str(e.value)[:300] for e in at.exception]}")
        return None
    return at


def sidebar_df(at):
    return [d.value for d in at.sidebar.dataframe]


at = render(open_panel=False)
if at:
    check("⚙️ Settings · Models" in [e.label for e in at.sidebar.expander]
          and not any(s.key == "model_settings_step" for s in at.sidebar.selectbox),
          "P1: the expander is in the sidebar and builds nothing while closed")

def step_lines(at):
    """The steps list as {(group, step name): its line}, and the whole text."""
    text = "\n".join(m.value for m in at.sidebar.markdown if m.value.startswith("**News digest**"))
    out, group = {}, None
    for l in text.splitlines():
        if l.startswith("**") and l.endswith("**"):
            group = l.strip("*")
        elif l.startswith("- ") and group:
            out[(group, l[2:].split(":", 1)[0])] = l
    return out, text


at = render()
if at:
    lines, text = step_lines(at)
    check(len(lines) == 10 and "custom" not in text and all(g in text for g in ("**News digest**", "**Expert Take**", "**Sentiment**", "**Market read**")),
          f"P1: open, ten steps in four groups, none custom ({len(lines)})")
    check(lines[("News digest", "collation (one per watchlist)")] == "- collation (one per watchlist): `gemini-3.7-flash ×2 → gemini-3.6-flash ×2 → gemini-3.5-flash-lite` · thinking 8k"
          and lines[("Market read", "the index charts' summary")].endswith("· thinking 4k")
          and lines[("News digest", "grounded search")] == "- grounded search: `gemma-4-26b-a4b-it ×3`"
          and "gemini-3.5-flash-lite ×2 → gemma-4-26b-a4b-it" in lines[("Expert Take", "write-up")],
          "P1: today's chains and budgets are what the list shows")

    step = next(s for s in at.sidebar.selectbox if s.key == "model_settings_step")
    step.set_value("E2").run()
    keys = [s.key for s in at.sidebar.selectbox]
    check(keys.count("ms_E2_model_0") == 1 and "ms_E2_model_1" in keys and "ms_E2_model_2" not in keys and "ms_E2_budget" in keys,
          f"P2: a picker per model in the chain (2) and a thinking budget ({[k for k in keys if k.startswith('ms_')]})")
    tries = {n.key: n.value for n in at.sidebar.number_input if n.key.startswith("ms_E2_attempts")}
    check(tries == {"ms_E2_attempts_0": 2, "ms_E2_attempts_1": 1}, f"P2: each model's tries, seeded from the default ({tries})")
    check(next(s for s in at.sidebar.selectbox if s.key == "ms_E2_budget").value == 8192, "P2: the budget is today's 8k")
    at.sidebar.selectbox(key="model_settings_step").set_value("N1").run()
    check(not any(s.key == "ms_N1_budget" for s in at.sidebar.selectbox) and any(s.key == "ms_N1_model_0" for s in at.sidebar.selectbox),
          "P2: a search step has no thinking budget")
    at.sidebar.selectbox(key="model_settings_step").set_value("N3").run()
    bud = next(s for s in at.sidebar.selectbox if s.key == "ms_N3_budget")
    check(list(bud.options) == ["4,096 (4k)", "8,192 (8k)"], f"P2: collation's budget is only its 4k-8k band ({list(bud.options)})")

    # P3 add a model, change tries and budget, save
    at.sidebar.selectbox(key="model_settings_step").set_value("E2").run()
    at.sidebar.button(key="ms_add_E2").click().run()
    check(any(s.key == "ms_E2_model_2" for s in at.sidebar.selectbox), "P3: Add a model adds a row")
    at.sidebar.selectbox(key="ms_E2_model_2").set_value("models/gemini-3.6-flash").run()
    at.sidebar.number_input(key="ms_E2_attempts_0").set_value(4).run()
    check(at.sidebar.number_input(key="ms_E2_timeout_0").value == 150, "P3: each model's timeout starts at 150 seconds")
    at.sidebar.number_input(key="ms_E2_timeout_2").set_value(90).run()
    at.sidebar.selectbox(key="ms_E2_budget").set_value(2048).run()
    at.sidebar.button(key="ms_save_E2").click().run()
    saved = settings_file()["ai_models"].get("E2")
    check(saved == {"chain": [{"model": "models/gemini-3.5-flash-lite", "attempts": 4, "timeout": 150},
                              {"model": "models/gemma-4-26b-a4b-it", "attempts": 1, "timeout": 150},
                              {"model": "models/gemini-3.6-flash", "attempts": 1, "timeout": 90}], "budget": 2048},
          f"P3: Save writes the chain, each model's tries and the budget to settings.json ({saved})")
    notice = " ".join(m.value for m in at.sidebar.success)
    check("Expert Take · write-up" in notice and "Applies from its next run" in notice, f"P3: it says when it applies ({notice[:90]})")
    lines, text = step_lines(at)
    check(lines[("Expert Take", "write-up")] == "- write-up: `gemini-3.5-flash-lite ×4 → gemma-4-26b-a4b-it → gemini-3.6-flash @90s` · thinking 2k · ✎ custom",
          f"P3: the list shows the step custom with its new chain ({lines[('Expert Take', 'write-up')]})")
    at.sidebar.button(key="ms_del_E2").click().run()
    check(not any(s.key == "ms_E2_model_2" for s in at.sidebar.selectbox), "P3: Remove last removes a row")

    # P4 Default
    at.sidebar.button(key="ms_reset_E2").click().run()
    s = settings_file()
    check("E2" not in s["ai_models"] and s["expert_reasoning_model"] == "models/gemini-3.5-flash-lite" and s["expert_thinking_budget"] == 8192,
          "P4: Default drops the saved chain and sets the old keys to their defaults")
    check("custom" not in step_lines(at)[1], "P4: no step is custom again")

    # P6 the repository
    at.sidebar.toggle(key="model_settings_repo_open").set_value(True).run()
    repo_tables = [t for t in sidebar_df(at) if "Model ID" in getattr(t, "columns", [])]
    check(repo_tables and len(repo_tables[0]) == 6, "P6: the repository lists the six default models")
    at.sidebar.text_input(key="ms_repo_add_id").set_value("gemini-9-flash").run()
    at.sidebar.button(key="ms_repo_add_btn").click().run()
    check(any(m["id"] == "models/gemini-9-flash" for m in settings_file()["ai_model_repository"]) and len(settings_file()["ai_model_repository"]) == 7,
          "P6: Add puts the new model (with its models/ prefix) into the saved repository next to the defaults")
    at.sidebar.selectbox(key="model_settings_step").set_value("R1").run()
    opts = next(s for s in at.sidebar.selectbox if s.key == "ms_R1_model_0").options
    check("Gemini 9 Flash" in " ".join(opts) or any("gemini-9-flash" in o for o in opts), f"P6: a step's picker offers it ({list(opts)[-2:]})")

    # P7 Test without a key
    at.sidebar.button(key="ms_test_btn_R1").click().run()
    warn = " ".join(w.value for w in at.sidebar.warning)
    check("No Gemini API key" in warn, f"P7: Test with no key says so ({warn[:80]})")

# P5 the old pickers are gone
at = render(open_panel=False, tab="News")
if at:
    allkeys = [s.key for s in at.selectbox]
    check(not any(k and ("news_search_model_select" in k or "news_reasoning_model_select" in k or "news_reasoning_budget_select" in k) for k in allkeys)
          and any("Settings · Models" in c.value for c in at.caption),
          "P5: the News tab has no model pickers and points to the sidebar")
at = render(open_panel=False)
if at:
    allkeys = [s.key for s in at.selectbox]
    check(not any(k and ("reasoning_model_select" in k or "reasoning_budget_select" in k) for k in allkeys)
          and any("Settings · Models" in c.value for c in at.caption),
          "P5: AI Analysis Controls has no model pickers either")

print("FAILURES:", fails)
sys.exit(fails)
