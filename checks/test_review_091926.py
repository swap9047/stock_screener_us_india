"""Regression checks for the 2026-09-19 review and the re-analyze crash.

Written before the fixes and watched failing. Record: docs/screener-review-*.md
(local only). Offline: invented tickers, temp files, stubbed clients, no network.

  T0  app.py used save_fundamentals without importing it, so every UI
      re-analyze crashed with NameError after the model calls had finished.
  T1  the fundamentals batch wrote a search-failure Unknown over a fresh prior.
  T2  news Stage 1 retried a rate-limited call on the SAME key.
  T3  two ladders still held gemma-4-31b-it, which answered 0 of ~63 calls.
  T4  a JSON array from the model was indexed like an object; a lower-case
      verdict discarded a complete analysis.
  T5  two alertable metrics had no table column.
  T6  net_volume_10d_dir had a fixed value set but was not categorical.
  T7  a rule leg that can never match was indistinguishable from a quiet rule.
  T8  a non-numeric multiplier/offset raised out of passes_filter.
  T9  a deeply nested formula raised RecursionError out of the render path.
  T10 a missing tech_uptrend voted Red in the auto-flag.
  T11 booleans rendered as 1/0 in Discord tables.
  T12 alert_state.json kept keys for deleted rules and removed tickers.
  T13 watchlist labels could duplicate another tab's label.
  T14 the column picker keyed a widget on the salted builtin hash().
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ast
import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def redirect(pairs):
    """[(module, attr, value)] -> restore callable."""
    saved = [(m, a, getattr(m, a)) for m, a, _ in pairs]
    for m, a, v in pairs:
        setattr(m, a, v)
    return lambda: [setattr(m, a, v) for m, a, v in saved]


# --- T0 no undefined names anywhere ------------------------------------------
# The re-analyze crash was a name used and never imported. A dict literal
# evaluates it on entry, so the function raised on every call -- and no check
# clicked a re-analyze button, because that calls Gemini. pyflakes finds the
# whole class offline. Only "undefined name" fails: the codebase has unused
# imports that are not bugs.
targets = sorted(str(p) for p in REPO.glob("*.py")) + sorted(str(p) for p in REPO.glob(".github/actions/*/*.py"))
try:
    proc = subprocess.run([sys.executable, "-m", "pyflakes", *targets], capture_output=True, text=True)
    if "No module named pyflakes" in proc.stderr:
        check(False, "T0: pyflakes is installed (checks/requirements.txt)")
    else:
        undefined = [ln for ln in proc.stdout.splitlines() if "undefined name" in ln]
        check(not undefined, f"T0: no undefined names in any module {undefined}")
except OSError as e:
    check(False, f"T0: pyflakes runs ({e})")


# --- T1 the fundamentals retry pass keeps a fresh prior over a failed search --
import fundamentals_eval
import refresh_fundamentals

fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
prior = {"sentiment": "Positive", "reasoning": "beat and raise", "model_used": "m", "as_of": fresh,
         "news_source": "🔍 Gemma-4-26B (Google Search)"}
search_failed = {"sentiment": "Unknown", "reasoning": "no news", "model_used": "m", "as_of": fresh,
                 "news_source": "⚪ No Source"}
genuine_unknown = dict(search_failed, news_source="🔍 Gemma-4-26B (Google Search)")

store = {"ACME": dict(prior)}
failed_inc, detail = refresh_fundamentals._apply_result(store, "ACME", dict(search_failed), dict(prior), 1.0)
check(store["ACME"]["sentiment"] == "Positive" and failed_inc == 1,
      f"T1: a search-failure Unknown does not replace a fresh Positive ({store['ACME']['sentiment']}, {detail})")

store = {"ACME": dict(prior)}
refresh_fundamentals._apply_result(store, "ACME", dict(genuine_unknown), dict(prior), 1.0)
check(store["ACME"]["sentiment"] == "Unknown",
      "T1: ...but a genuine Unknown, from a search that worked, still replaces it")

stale_prior = dict(prior, as_of="2026-01-01 00:00")
store = {"ACME": dict(stale_prior)}
refresh_fundamentals._apply_result(store, "ACME", dict(search_failed), dict(stale_prior), 1.0)
check(store["ACME"]["sentiment"] == "Unknown",
      "T1: ...and a search-failure Unknown still replaces a STALE prior")

src = (REPO / "fundamentals_eval.py").read_text()
check("covered by refresh_fundamentals' retry queue" not in src,
      "T1: the comment claiming the retry queue covers the batch is corrected")


# --- T2/T3 news Stage 1: rotate keys, no dead rung, still survives a bad model -
import llm_util
import news_summary


class _Exc(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class _Resp:
    def __init__(self, text):
        self.text = text
        self.candidates = []


class FakeClient:
    """Mimics RotatingGeminiClient's surface: key_names, and generate_content
    taking _key_out/_avoid_key. `script` is a list of (outcome) consumed per
    call: an exception to raise, or a string to return as the response text."""

    def __init__(self, script, keys=("K1", "K2", "K3")):
        self.key_names = list(keys)
        self.script = list(script)
        self.calls = []
        self.models = self

    def generate_content(self, model, contents, config, _key_out=None, _avoid_key=None):
        key = next(k for k in self.key_names if k != _avoid_key)
        if _key_out is not None:
            _key_out["key"] = key
        self.calls.append({"model": model, "key": key, "avoid": _avoid_key})
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Resp(outcome)


restore = redirect([(llm_util, "RETRY_BACKOFF_SECONDS", 0)])
try:
    client = FakeClient([_Exc(429, "429 RESOURCE_EXHAUSTED"), "some news"])
    text, _ = news_summary.fetch_single_raw_news(client, "ACME", "us_picks", "2026-09-20",
                                                 model=news_summary.SEARCH_MODEL)
    keys = [c["key"] for c in client.calls]
    check(text == "some news" and len(keys) == 2 and keys[0] != keys[1],
          f"T2: Stage 1 retries a rate-limited call on a different key ({keys})")

    client = FakeClient([_Exc(500, "500 INTERNAL")] * 5)
    try:
        news_summary.fetch_single_raw_news(client, "ACME", "us_picks", "2026-09-20",
                                           model=news_summary.SEARCH_MODEL)
    except Exception:
        pass
    models = [c["model"] for c in client.calls]
    check(models and all("gemma-4-31b" not in m for m in models),
          f"T3: Stage 1 never calls the dead 31b model ({models})")
    check(len(models) == 3, f"T3: ...and makes three attempts on the search model ({len(models)})")

    client = FakeClient([_Exc(404, "404 models/typo is not found"), "found it"])
    text, _ = news_summary.fetch_single_raw_news(client, "ACME", "us_picks", "2026-09-20", model="models/typo")
    check(text == "found it" and client.calls[-1]["model"] == news_summary.SEARCH_MODEL,
          f"T3: a typo'd news_search_model still falls back to the default search model "
          f"({[c['model'] for c in client.calls]})")
finally:
    restore()


def _code_strings(path):
    """String constants in a module that are NOT docstrings."""
    tree = ast.parse(Path(path).read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]


for mod in ("news_summary.py", "expert_views.py", "fundamentals_eval.py"):
    dead = [s for s in _code_strings(REPO / mod) if s == "models/gemma-4-31b-it"]
    check(not dead, f"T3: {mod} has no gemma-4-31b-it rung in code")


# --- T4 JSON that is not an object moves the ladder on; verdict case normalized -
try:
    llm_util.json_object('[{"verdict": "HOLD"}]')
    check(False, "T4: json_object rejects a JSON array")
except ValueError:
    check(True, "T4: json_object rejects a JSON array")
except AttributeError as e:
    check(False, f"T4: llm_util.json_object exists ({e})")
try:
    check(llm_util.json_object('{"a": 1}') == {"a": 1}, "T4: json_object returns an object unchanged")
except AttributeError:
    check(False, "T4: json_object returns an object unchanged")

restore = redirect([(llm_util, "RETRY_BACKOFF_SECONDS", 0)])
try:
    client = FakeClient(['["not", "an", "object"]', '{"verdict": "HOLD"}'])
    data, used = llm_util.run_model_ladder(
        client, "p", llm_util.standard_tiers("models/a", "models/b"), lambda m: None,
        on_success=lambda resp: llm_util.json_object(resp.text))
    check(data == {"verdict": "HOLD"} and len(client.calls) == 2,
          f"T4: an array answer is retried on the next rung ({data}, {len(client.calls)} calls)")
except AttributeError as e:
    check(False, f"T4: an array answer is retried on the next rung ({e})")
finally:
    restore()

import expert_views

try:
    v = expert_views.normalize_view({"verdict": " accumulate ", "headline": "h"})
    check(v["verdict"] == "ACCUMULATE", f"T4: a lower-case verdict is normalized ({v['verdict']!r})")
    check(expert_views.normalize_view(["x"]) == ["x"], "T4: expert normalize_view leaves a non-dict alone")
except AttributeError as e:
    check(False, f"T4: expert_views.normalize_view exists ({e})")
try:
    v = fundamentals_eval.normalize_view({"sentiment": "POSITIVE "})
    check(v["sentiment"] == "Positive", f"T4: an upper-case sentiment is normalized ({v['sentiment']!r})")
except AttributeError as e:
    check(False, f"T4: fundamentals_eval.normalize_view exists ({e})")


# --- T5 every filterable metric has a table column ---------------------------
import stock_data as sd

app_src = (REPO / "app.py").read_text()
fn = next(n for n in ast.walk(ast.parse(app_src))
          if isinstance(n, ast.FunctionDef) and n.name == "build_column_defs")
column_keys = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
# Fields shown under a DIFFERENT column key (the display cell is built in the
# render path and bridged back by _sort_label_to_field) -- see AGENTS.md. Each
# must still map to a real column. last_close is the fixed "Last" column.
DISPLAY_KEY = {"sentiment": "fundamentals", "tech_uptrend": "tech_uptrend_label",
               "vstop_weekly_weeks_since_change": "vstop_change", "interested": "interested_label"}
FIXED = {"last_close"}
orphans = []
for label, field in sd.get_filterable_metrics(dict(sd.DEFAULT_SETTINGS)).items():
    if field in FIXED:
        continue
    if DISPLAY_KEY.get(field, field) not in column_keys:
        orphans.append(field)
check(not orphans, f"T5: every filterable metric has a table column (missing: {orphans})")
rel_pct = ast.literal_eval(app_src.split("REL_PCT_COLS = ", 1)[1].split("\n", 1)[0])
check("1W Ret vs Index" in rel_pct, "T5: 1W Ret vs Index is formatted as a signed relative return")


# --- T6 net_volume_10d_dir is categorical ------------------------------------
import filters

check(filters.CATEGORICAL_METRICS.get("net_volume_10d_dir") == ["Positive", "Negative"],
      "T6: net_volume_10d_dir is categorical, best first")
check("net_volume_10d_dir" in filters.TEXT_METRICS, "T6: ...and still excluded from Metric B")
check(filters.passes_filter({"net_volume_10d_dir": "Positive"},
                            {"metric_a": "net_volume_10d_dir", "operator": "in", "compare_type": "value",
                             "value": ["Positive"]}),
      "T6: ...and matches with the in operator")


# --- T7 dead rule legs are named ---------------------------------------------
import alerts

rows = [{"ticker": "ACME", "last_close": 10.0, "ttm_profit_growth": None},
        {"ticker": "ZED.NS", "last_close": 20.0, "ttm_profit_growth": None}]
rules = [
    {"id": "r1", "name": "Stale key", "enabled": True,
     "conditions": [{"metric_a": "old_metric", "operator": ">", "compare_type": "value", "value": 1}]},
    {"id": "r2", "name": "Empty metric", "enabled": True,
     "conditions": [{"metric_a": "ttm_profit_growth", "operator": ">", "compare_type": "value", "value": 1}]},
    {"id": "r3", "name": "Refs", "enabled": True,
     "conditions": [{"type": "rule", "rule_id": "r4"}, {"type": "rule", "rule_id": "gone", "logic": "OR"}]},
    {"id": "r4", "name": "Off", "enabled": False,
     "conditions": [{"metric_a": "last_close", "operator": ">", "compare_type": "value", "value": 1}]},
    {"id": "r5", "name": "Fine", "enabled": True,
     "conditions": [{"metric_a": "last_close", "operator": ">", "compare_type": "metric",
                     "metric_b": "missing_b"}]},
]
try:
    warnings = alerts.dead_rule_legs(rules, rows)
    text = "\n".join(warnings)
    check("Stale key" in text and "old_metric" in text, "T7: a metric no row carries is named")
    check("Empty metric" in text and "ttm_profit_growth" in text, "T7: a metric empty on every row is named")
    check("Refs" in text and "r4" in text and "gone" in text, "T7: references to disabled or missing rules are named")
    check("missing_b" in text, "T7: a dead Metric B is named too")
    check(not any(w.startswith('Rule "Off"') for w in warnings), "T7: a disabled rule is not itself audited")
    check(not any(w.startswith('Rule "Fine"') and "last_close" in w for w in warnings),
          "T7: a metric that rows carry is not flagged")
except AttributeError as e:
    check(False, f"T7: alerts.dead_rule_legs exists ({e})")


# --- T8 a non-numeric multiplier/offset fails closed -------------------------
row = {"a": 10.0, "b": 5.0}
base = {"metric_a": "a", "operator": ">", "compare_type": "metric", "metric_b": "b"}
for bad in ({"multiplier": "abc"}, {"offset": "x"}, {"multiplier": [2]}):
    try:
        got = filters.passes_filter(row, {**base, **bad})
        check(got is False, f"T8: {bad} fails closed")
    except Exception as e:
        check(False, f"T8: {bad} fails closed instead of raising {type(e).__name__}")
check(filters.passes_filter(row, {**base, "multiplier": "1.5"}) is True,
      "T8: a numeric string multiplier still works (10 > 7.5)")


# --- T9 a deeply nested formula fails validation instead of raising ----------
import custom_columns

deep = "+".join(["x"] * 60000)
try:
    ok, msg = custom_columns.validate_formula(deep, {"x"})
    check(ok is False, f"T9: validate_formula rejects a pathologically deep formula ({msg[:60]!r})")
except RecursionError:
    check(False, "T9: validate_formula rejects a pathologically deep formula (raised RecursionError)")
try:
    check(custom_columns.safe_eval_formula(deep, {"x": 1.0}) is None,
          "T9: ...and safe_eval_formula returns None")
except RecursionError:
    check(False, "T9: ...and safe_eval_formula returns None (raised RecursionError)")


# --- T10 a missing tech_uptrend abstains -------------------------------------
import ticker_notes

flag, reason = ticker_notes.compute_auto_flag({"trend": "Downtrend", "tech_uptrend": None}, expert_verdict="CAUTION")
check(flag != "Red" and "Tech Uptrend" not in reason,
      f"T10: a missing tech_uptrend casts no vote ({flag!r}, {reason!r})")
flag, reason = ticker_notes.compute_auto_flag({"trend": "Downtrend", "tech_uptrend": 0}, expert_verdict="CAUTION")
check(flag == "Red", f"T10: ...while a real No still votes Red ({flag!r})")


# --- T11 booleans render as Yes/No in Discord tables -------------------------
check(alerts._format_cell("interested", True) == "Yes" and alerts._format_cell("interested", False) == "No",
      f"T11: booleans render Yes/No ({alerts._format_cell('interested', True)!r})")
check(alerts._format_cell("gc_weeks_10_30", 3) == "3", "T11: ...and integers still render as numbers")


# --- T12 alert state is pruned to live rules and watchlist members -----------
state = {"r1:ACME": {"was_active": True}, "r1:GONE": {"was_active": True},
         "deleted:ACME": {"was_active": True}, "r2:ZED.NS": {"was_active": False}}
try:
    pruned = alerts.prune_state(state, [{"id": "r1"}, {"id": "r2", "enabled": False}],
                                {"us_picks": ["ACME"], "in_picks": ["ZED.NS"]})
    check(set(pruned) == {"r1:ACME", "r2:ZED.NS"},
          f"T12: keys for deleted rules and removed tickers are dropped ({sorted(pruned)})")
    check(state.get("r1:GONE") is not None, "T12: ...without mutating the input")
except AttributeError as e:
    check(False, f"T12: alerts.prune_state exists ({e})")


# --- T13 watchlist labels cannot duplicate another tab's label ---------------
import filters as _filters

d = tempfile.mkdtemp()
restore = redirect([
    (sd, "MARKETS_FILE", os.path.join(d, "markets.json")),
    (sd, "WATCHLIST_FILE", os.path.join(d, "watchlist.json")),
    (sd, "SETTINGS_FILE", os.path.join(d, "settings.json")),
    (_filters, "CUSTOM_FILTERS_FILE", os.path.join(d, "custom_filters.json")),
])
try:
    json.dump({"us_picks": {"label": "US Picks", "benchmark": "SPY"}}, open(sd.MARKETS_FILE, "w"))
    reg = sd.load_markets_registry()
    for bad in ("US Picks", "us  picks", "News", "alert rules", "All Invested", "ALL WATCHLIST"):
        check(bool(sd.watchlist_label_error(bad, reg)), f"T13: label {bad!r} is rejected")
    check(sd.watchlist_label_error("US Picks", reg, own_key="us_picks") == "",
          "T13: re-saving a watchlist under its own label is allowed")
    check(sd.watchlist_label_error("UK Picks", reg) == "", "T13: a new unique label is allowed")
    try:
        sd.add_watchlist("News", "SPY")
        check(False, "T13: add_watchlist refuses a reserved label")
    except ValueError:
        check(True, "T13: add_watchlist refuses a reserved label")
    check(set(sd.load_markets_registry()) == {"us_picks"}, "T13: ...and registers nothing")
except AttributeError as e:
    check(False, f"T13: stock_data.watchlist_label_error exists ({e})")
finally:
    restore()

check("list(COMBINED_TAB_LABELS.items())" in app_src,
      "T13: app.py builds its combined tabs from the one label list stock_data checks against")


# --- T14 no salted hash() in widget keys -------------------------------------
check("hash(tuple(" not in app_src, "T14: the column picker key does not use the salted builtin hash()")


print(f"\nFAILURES: {fails}")
sys.exit(1 if fails else 0)
