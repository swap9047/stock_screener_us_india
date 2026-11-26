"""The sidebar's Settings · Models (2026-10-31), rendered: offline, from a throwaway copy of the
repo (as test_market_read_render.py), over synthetic prices.

  P1  the expander is there but builds nothing while closed; open, it lists all ten steps, by
      group, with today's chains and thinking budgets, none marked custom;
  P2  picking a step shows one model picker and tries box per model (seeded from the step's chain),
      a thinking budget for a reasoning step (collation only 4k/8k), none for a search;
  P3  Add a model / Remove last change the number of rows; changing a model, its tries, its
      timeout (200 s unless changed) and the budget and pressing Save writes the chain to
      settings.json, marks the step custom and says it applies from the next run;
  P4  Default puts it back (the step no longer custom, the old keys at their defaults);
  P5  the old pickers are gone from the News tab and AI Analysis Controls, which point here;
  P6  the repository lists the models and Add puts a new one in the pickers;
  P7  Test with no Gemini key says so instead of failing.
  P9  "Calls per key today" is off until ticked; then the shared per-key table read through the Vercel function (the
      request carries the token and asks for today), with its quota day under it; without MARKET_API_URL and
      MARKET_API_TOKEN, or when the endpoint refuses, the panel says what is missing instead of failing (owner, 2026-11-26).
  P8  the "Skip Gemini 3.8 Flash calls" switch is the panel's first control; ticking it saves it in
      settings.json (inside ai_models, the chains untouched) and notes the skip on the two market read
      steps only; unticking takes it out again (owner, 2026-11-07).
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
    check(lines[("News digest", "collation (one per watchlist)")] == "- collation (one per watchlist): `gemini-3.7-flash ×2 → gemini-3.6-flash ×2 → gemini-3.5-flash-lite ×2` · thinking 8k"
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
    check(at.sidebar.number_input(key="ms_E2_timeout_0").value == 200, "P3: each model's timeout starts at 200 seconds")
    at.sidebar.number_input(key="ms_E2_timeout_2").set_value(90).run()
    at.sidebar.selectbox(key="ms_E2_budget").set_value(2048).run()
    at.sidebar.button(key="ms_save_E2").click().run()
    saved = settings_file()["ai_models"].get("E2")
    check(saved == {"chain": [{"model": "models/gemini-3.5-flash-lite", "attempts": 4, "timeout": 200},
                              {"model": "models/gemma-4-26b-a4b-it", "attempts": 1, "timeout": 200},
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
    check(repo_tables and len(repo_tables[0]) == 7, "P6: the repository lists the seven default models (Hydrogen since 2026-11-15)")
    at.sidebar.text_input(key="ms_repo_add_id").set_value("gemini-9-flash").run()
    at.sidebar.button(key="ms_repo_add_btn").click().run()
    check(any(m["id"] == "models/gemini-9-flash" for m in settings_file()["ai_model_repository"]) and len(settings_file()["ai_model_repository"]) == 8,
          "P6: Add puts the new model (with its models/ prefix) into the saved repository next to the defaults")
    at.sidebar.selectbox(key="model_settings_step").set_value("R1").run()
    opts = next(s for s in at.sidebar.selectbox if s.key == "ms_R1_model_0").options
    check("Gemini 9 Flash" in " ".join(opts) or any("gemini-9-flash" in o for o in opts), f"P6: a step's picker offers it ({list(opts)[-2:]})")

    # P7 Test without a key
    at.sidebar.button(key="ms_test_btn_R1").click().run()
    warn = " ".join(w.value for w in at.sidebar.warning)
    check("No Gemini API key" in warn, f"P7: Test with no key says so ({warn[:80]})")

# P8 the skip list (owner, 2026-11-07 a 3.8 Flash checkbox; 2026-11-15 a pick-list of any models)
at = render()
if at:
    boxes = [c for c in at.sidebar.multiselect if c.key == "model_settings_skip_models"]
    check(len(boxes) == 1 and list(boxes[0].value) == [] and boxes[0].label == "Skip these models"
          and "Gemini 4 Hydrogen" in boxes[0].options and "Gemini 3.8 Flash" in boxes[0].options,
          f"P8: the panel has the pick-list, nothing skipped by default, every repository model offered ({list(boxes[0].options) if boxes else None})")
    _before = json.dumps((settings_file().get("ai_models") or {}), sort_keys=True)
    boxes[0].set_value(["models/gemini-3.8-flash", "models/gemini-4-hydrogen"]).run()
    _after = settings_file().get("ai_models") or {}
    lines, text = step_lines(at)
    skipped = sorted(k[1] for k, l in lines.items() if "skipped: Gemini 3.8 Flash, Gemini 4 Hydrogen" in l)
    check(_after.get("skip_models") == ["models/gemini-3.8-flash", "models/gemini-4-hydrogen"]
          and json.dumps({k: v for k, v in _after.items() if k != "skip_models"}, sort_keys=True) == _before,
          f"P8: picking two saves the list inside ai_models and leaves every chain as it was ({_after.get('skip_models')})")
    check(len(skipped) == 2 and all(("Market read", n) in lines for n in skipped),
          f"P8: the steps list names both skipped models on the two market read steps only ({skipped})")
    check(any("Skipped from the next run" in s.value for s in at.sidebar.success), "P8: it says the skip applies from the next run")
    [c for c in at.sidebar.multiselect if c.key == "model_settings_skip_models"][0].set_value([]).run()
    lines, text = step_lines(at)
    check("skip_models" not in (settings_file().get("ai_models") or {}) and "skipped:" not in text,
          "P8: clearing the list takes it out again")

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


# --- P9: Calls per key today ------------------------------------------------------------------------------------
import types as _types
import requests as _requests
import llm_util as _llm

_posts_seen = []
_real_get = _requests.get
_rows = [{"key_id": _llm.key_fingerprint("AIzaSyDUMMY0001"), "model": "gemma-9-test", "calls": 6},
         {"key_id": _llm.key_fingerprint("AIzaSyDUMMY0000"), "model": "gemma-9-test", "calls": 4},
         {"key_id": _llm.key_fingerprint("AIzaSyDUMMY0000"), "model": "gemini-9-test", "calls": 1},
         {"key_id": "deadbeef", "model": "gemma-9-test", "calls": 2}]


def _fake_get(url, params=None, headers=None, timeout=None):
    _posts_seen.append((url, params, headers))
    return _types.SimpleNamespace(status_code=_fake_get.status, json=lambda: {"day": "2026-11-26", "rows": _rows})


def _key_tables(a):
    return [d for d in sidebar_df(a) if "Key" in d.columns]      # the panel also draws the model repository


_fake_get.status = 200
_requests.get = _fake_get
os.environ.update({"MARKET_API_URL": "https://usage.example/api/market", "MARKET_API_TOKEN": "tok-test",
                   "GEMINI_API_KEY": "AIzaSyDUMMY0000", "GEMINI_API_KEY_BACKUP": "AIzaSyDUMMY0001"})
at = render()
check(at is not None, "P9: the page renders with the usage endpoint configured")
if at:
    off = not _key_tables(at) and not _posts_seen
    at.sidebar.checkbox(key="ms_key_usage").set_value(True).run()
    tables = _key_tables(at)
    rows = tables[-1].to_dict("records") if tables else []
    check(off and not at.exception and [r["Key"] for r in rows] == [f"key 1 · {_llm.key_fingerprint('AIzaSyDUMMY0000')}",
                                                                    f"key 2 · {_llm.key_fingerprint('AIzaSyDUMMY0001')}",
                                                                    "not in this app's pool · deadbeef", "All keys"]
          and rows[0]["gemma-9-test"] == 4 and rows[-1]["Total"] == 13 and list(tables[-1].columns)[1] == "gemma-9-test"
          and any("2026-11-26" in c.value and "Pacific" in c.value for c in at.sidebar.caption),
          "P9: Calls per key today is off until ticked (nothing fetched); then the table, a row per key by its place in this app's pool "
          "(a key it doesn't hold still shown), models most-called first, an All keys row, the quota day and Pacific said under it")
    check(bool(_posts_seen) and _posts_seen[-1][0] == "https://usage.example/api/market" and _posts_seen[-1][1] == {"usage": "today"}
          and _posts_seen[-1][2] == {"Authorization": "Bearer tok-test"},
          "P9: the request goes to the configured address with the token as a Bearer header and asks for today")
    _fake_get.status = 503
    at.sidebar.checkbox(key="ms_key_usage").set_value(False).run()
    at.sidebar.checkbox(key="ms_key_usage").set_value(True).run()
    check(not at.exception and any("answered 503" in c.value for c in at.sidebar.caption) and not _key_tables(at),
          "P9: an endpoint that refuses is said so, with no table and no crash")
for k in ("MARKET_API_URL", "MARKET_API_TOKEN"):
    os.environ.pop(k, None)
at = render()
if at:
    at.sidebar.checkbox(key="ms_key_usage").set_value(True).run()
    check(not at.exception and any("MARKET_API_URL" in c.value and "MARKET_API_TOKEN" in c.value for c in at.sidebar.caption),
          "P9: without MARKET_API_URL and MARKET_API_TOKEN the panel says what to add")
_requests.get = _real_get

print("FAILURES:", fails)
sys.exit(fails)
