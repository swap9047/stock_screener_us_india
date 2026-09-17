"""Guards on how the generated data files are written, and one sort-bridge gap.

Four findings from the 2026-09-16 review:

  R6  refresh_dashboard_perf rebuilt dashboard_perf.json from scratch and wrote it
      unconditionally, so a throttled leg wrote an EMPTY market and commit-data
      pushed it -- blanking that market's 5-year chart until the next run, 12h
      later. refresh_market_breadth already had a coverage floor plus per-market
      preservation after one failing leg destroyed 1055 days of US breadth.
  R7  Three writers bypassed atomic_write_json, against AGENTS.md's explicit rule.
      All three files are committed to the data repo, so a cancelled job landing
      mid-dump pushes a torn file.
  R9  trigger_github_workflow was the only requests call in github_sync with no
      timeout, and it runs inside a Streamlit interaction.
  R10 "VStop Weeks Ago" was filterable but had no sort option: the label resolved
      to the display key (built after filtering, hence excluded from the picker)
      instead of the raw week count already on the row.

Offline: invented tickers, a temp output file, no network.
"""

import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO)

import ast
import json
import os
import tempfile

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- R6: a degraded leg must not blank a market ---------------------------
import refresh_dashboard_perf as rdp

WL = {"us_picks": ["ACME", "WIDG", "ZED"], "in_picks": ["A&B.NS", "QQQ.NS"]}


def fake_series(tickers, captured):
    """{ticker: series-like} for the first `captured` of `tickers`."""
    import pandas as pd
    idx = pd.to_datetime(["2026-09-14", "2026-09-15"])
    return {t: pd.Series([1.0, 2.0], index=idx) for t in tickers[:captured]}


def run_main(capture_per_market, out_file):
    rdp.OUT_FILE = out_file
    rdp.load_settings = lambda: {}
    rdp.load_watchlists = lambda: WL
    rdp.get_benchmarks = lambda s: {"us_picks": "SPY", "in_picks": "^CRSLDX"}
    calls = {"i": 0}

    def _dl(tickers):
        # markets are iterated in watchlist order
        market = list(WL)[calls["i"]]
        calls["i"] += 1
        return fake_series(tickers, capture_per_market[market])

    rdp._download_series = _dl
    rdp.main()
    with open(out_file) as f:
        return json.load(f)


tmp = os.path.join(tempfile.mkdtemp(), "dashboard_perf.json")

# Run 1: everything captured (4 and 3 series incl. benchmark).
good = run_main({"us_picks": 4, "in_picks": 3}, tmp)
check(len(good["markets"]["us_picks"]) == 4 and len(good["markets"]["in_picks"]) == 3,
      "a healthy run stores every series")
check(good.get("status") == {"us_picks": "ok", "in_picks": "ok"}, "healthy run reports status ok")

# Run 2: the US leg is throttled down to 1 of 4 -- below the 50% floor.
degraded = run_main({"us_picks": 1, "in_picks": 3}, tmp)
check(len(degraded["markets"]["us_picks"]) == 4,
      f"a throttled leg keeps the previous block ({len(degraded['markets']['us_picks'])} series, want 4)")
check(degraded["status"]["us_picks"] == "failed", "the throttled market is reported as failed")
check(len(degraded["markets"]["in_picks"]) == 3, "the healthy market in the same run still refreshes")
check(degraded["status"]["in_picks"] == "ok", "the healthy market is reported ok")

# Run 3: no previous file at all -- a failed leg stays ABSENT, never empty.
tmp2 = os.path.join(tempfile.mkdtemp(), "dashboard_perf.json")
fresh = run_main({"us_picks": 0, "in_picks": 3}, tmp2)
check("us_picks" not in fresh["markets"],
      "with nothing to fall back on, a failed market is absent rather than an empty block")

# The app reads perf_data["markets"][market]; an empty dict would render as
# "no history", which is exactly what must not happen silently.
check(fresh["markets"].get("us_picks") != {}, "a failed market is never written as {}")


# --- R7: every generated-data writer goes through atomic_write_json --------
WRITERS = ("weekly_wrapup.py", "refresh_market_breadth.py", "refresh_dashboard_perf.py",
           "stock_data.py", "news_summary.py", "expert_views.py", "fundamentals_eval.py")
for name in WRITERS:
    src = open(f"{REPO}/{name}").read()
    tree = ast.parse(src)
    bare = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) == "dump"
            and getattr(getattr(n.func, "value", None), "id", None) == "json"]
    check(not bare, f"{name}: no bare json.dump (lines {bare})")

import json_store
check(json_store.atomic_write_json.__module__ == "json_store", "atomic_write_json lives in json_store")
# sort_keys must not have changed anyone's existing output.
d = tempfile.mkdtemp()
p = os.path.join(d, "a.json")
json_store.atomic_write_json(p, {"b": 1, "a": 2})
check(open(p).read() == '{\n  "b": 1,\n  "a": 2\n}', "default output is unchanged (insertion order)")
json_store.atomic_write_json(p, {"b": 1, "a": 2}, sort_keys=True)
check(open(p).read() == '{\n  "a": 2,\n  "b": 1\n}', "sort_keys=True sorts, for the committed state file")
check(not os.path.exists(p + ".tmp"), "the temp file is renamed away, not left behind")


# --- N3: new-high/new-low must use the same history bar as breadth ---------
# calculate_breadth masks recent listings out of the 200DMA line
# (closes.notna().cumsum() >= 200 plus a matching ma_counts denominator) but the
# 52-week series had neither guard: a stock with 126 sessions could post a
# "52-week high" off a 126-day window, while stocks too new to qualify still
# sat in the denominator. Numerator too generous, denominator too large.
import numpy as np
import pandas as pd

_idx = pd.bdate_range("2023-01-02", periods=400)
# three mature names that peaked long ago and drifted down -> none at a 52w high
_mature = np.concatenate([np.linspace(100, 200, 200), np.linspace(200, 150, 200)])
_panel = pd.DataFrame({f"OLD{i}": pd.Series(_mature, index=_idx) for i in (1, 2, 3)})
# one recent listing: 130 sessions, rising, so it sits at its own 130-day peak
_newco = pd.Series(np.nan, index=_idx, dtype=float)
_newco.iloc[-130:] = np.linspace(100, 200, 130)
_panel["NEWCO"] = _newco


def _highs_pct(panel, masked):
    high = panel.rolling(252, min_periods=126).max()
    if masked:
        high = high.where(panel.notna().cumsum() >= 252)
    denom = (panel.notna() & high.notna()).sum(axis=1) if masked else panel.notna().sum(axis=1)
    return ((panel >= high).sum(axis=1) / denom.replace(0, pd.NA)) * 100


_last = _idx[-1]
check(round(float(_highs_pct(_panel, True)[_last]), 1) == 0.0,
      f"a 130-session listing is NOT counted as a 52w high ({_highs_pct(_panel, True)[_last]})")
check(round(float(_highs_pct(_panel, False)[_last]), 1) == 25.0,
      "...and the unmasked form this replaces reported 25%, confirming the fixture bites")
_p2 = _panel.copy()
_p2["OLD1"] = pd.Series(np.concatenate([np.linspace(100, 200, 200), np.linspace(200, 260, 200)]), index=_idx)
check(round(float(_highs_pct(_p2, True)[_last]), 1) == 33.3,
      "a mature name genuinely at a 52w high still registers (1 of 3 qualifying)")

_bsrc = open(f"{REPO}/refresh_market_breadth.py").read()
check("closes.notna().cumsum() >= 252" in _bsrc, "the 52w mask is applied in refresh_market_breadth")
check("hl_counts" in _bsrc and "(closes.notna() & high52.notna())" in _bsrc,
      "new-high/new-low use their own denominator, not valid_counts")
check("hl_counts.replace(0, pd.NA)" in _bsrc, "a zero denominator yields NA, not inf")


# --- R9: every requests call in github_sync has a timeout ------------------
gs = ast.parse(open(f"{REPO}/github_sync.py").read())
no_timeout = []
for n in ast.walk(gs):
    if not isinstance(n, ast.Call):
        continue
    f = n.func
    if getattr(getattr(f, "value", None), "id", None) == "requests" and \
            getattr(f, "attr", None) in ("get", "post", "patch", "put", "delete"):
        if not any(kw.arg == "timeout" for kw in n.keywords):
            no_timeout.append(f"requests.{f.attr} at github_sync.py:{n.lineno}")
check(not no_timeout, f"every github_sync requests call passes timeout= ({no_timeout})")


# --- R10: "VStop Weeks Ago" resolves to the raw, sortable field ------------
app_tree = ast.parse(open(f"{REPO}/app.py").read())
fn = next(n for n in app_tree.body if isinstance(n, ast.FunctionDef)
          and n.name == "_sort_label_to_field")
ns = {}
exec(compile(ast.Module(body=[fn], type_ignores=[]), "app.py", "exec"), ns)
resolve = ns["_sort_label_to_field"]
key_by_label = {"VStop Weeks Ago": "vstop_change", "RSI-D": "rsi14_daily"}
check(resolve("VStop Weeks Ago", key_by_label) == "vstop_weekly_weeks_since_change",
      "VStop Weeks Ago sorts on the raw week count, not the display cell")
check(resolve("RSI-D", key_by_label) == "rsi14_daily", "an ordinary column still resolves normally")
for label, field in (("Sentiment", "sentiment"), ("Tech Uptrend", "tech_uptrend"),
                     ("Interested", "interested")):
    check(resolve(label, {}) == field, f"{label} bridge intact")

# ...and its label is no longer excluded from the sort picker.
app_src = open(f"{REPO}/app.py").read()
check('"matched_alerts", "company_name", "index_name"' in app_src,
      "vstop_change is no longer dropped from the sort options")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
