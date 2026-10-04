"""Regression checks for the 2026-10-03 review, written before the fixes.

  L1  Trend: anything short of unanimous read "Downtrend", so a stock above a
      rising 40W EMA that merely lagged its index was "Avoid". Now "Mixed".
  F1  UI edits to rules/notes/settings/filters/custom columns stayed on the
      container until a manual push. Now every changed config file is pushed at
      the end of the run that changed it.
  F2  news_check posted a mostly-failed digest to Discord, then failed -- and
      the slot gate's retry posted a second one.
  F3  a partial (markets/limit) dispatch counted as the night's full run.
  F4  the slot gate listed runs with ?status=completed, which lagged and let a
      slot be worked twice.
  F5  the RS caption and the Expert Take prompt named the watchlist's
      benchmark; every ticker is actually measured against its own index.
  F6  "Data Thru" counted calendar days, so every Monday read stale.
  F7  "Reset to defaults" reset non-calculation settings too.
  D*  prompt/caption text that disagreed with the code.
  I3  with_disclaimer measured length in code points, not Discord's UTF-16.

Offline: invented tickers, temp dirs, an in-memory GitHub, no network.
"""

import ast
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / ".github/actions/slot-gate"))

import pandas as pd
import yaml

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


APP = (REPO / "app.py").read_text()


def _code_strings(path):
    """Every string literal in a module's code (not comments)."""
    tree = ast.parse(Path(path).read_text())
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


# --- L1 Trend gets a Mixed state ---------------------------------------------
import stock_data as sd
import filters
import ticker_notes as tn
import expert_views as ev

rising = pd.Series([90.0, 92.0, 94.0, 96.0])
falling = pd.Series([110.0, 108.0, 106.0, 104.0])


def trend(close, slow, rs, fast, hi=200.0, lo=1.0, v10=1.0, v100=1.0):
    return sd.compute_trend(close, slow, rs, hi, lo, v10, v100, 3, near_high_low_pct=0.10,
                            volume_ratio=1.0, ema_fast=fast)


lbl, rank, d = trend(100, rising, -2.0, 98)
check(lbl == "Mixed" and rank == 3, f"L1: above a rising 40W, 10>40, only RS<0 -> Mixed (got {lbl}, {rank})")
check(d and d["direction"] == "Mixed" and d["strong"] is False, "L1: the detail says Mixed and never Strong")
lbl, rank, _ = trend(100, rising, 2.0, 98)
check((lbl, rank) == ("Uptrend", 4), f"L1: all four bullish -> Uptrend, rank 4 (got {lbl}, {rank})")
lbl, rank, _ = trend(100, rising, 2.0, 98, hi=105.0, v10=2.0)
check((lbl, rank) == ("Strong Uptrend", 5), f"L1: ...near the high on rising volume -> Strong Uptrend, rank 5 (got {lbl}, {rank})")
lbl, rank, _ = trend(100, falling, -3.0, 101)
check((lbl, rank) == ("Downtrend", 2), f"L1: all four bearish -> Downtrend, rank 2 (got {lbl}, {rank})")
lbl, rank, _ = trend(100, falling, -3.0, 101, lo=95.0, v10=2.0)
check((lbl, rank) == ("Strong Downtrend", 1), f"L1: ...near the low on rising volume -> Strong Downtrend (got {lbl}, {rank})")
lbl, _, _ = trend(100, rising, None, 98)
check(lbl == "Uptrend", "L1: RS not computable is skipped, as before -> Uptrend")
lbl, _, _ = trend(100, falling, 2.0, 98)
check(lbl == "Mixed", f"L1: 2 bullish / 2 bearish -> Mixed (got {lbl})")

check(filters.CATEGORICAL_METRICS["trend"] == ["Strong Uptrend", "Uptrend", "Mixed", "Downtrend", "Strong Downtrend"],
      "L1: the Trend dropdown offers Mixed, best-first")
for (t, s), want in {("Mixed", "Neutral"): "Mixed", ("Mixed", "Unknown"): "Mixed",
                     ("Mixed", "Positive"): "News divergence", ("Mixed", "Negative"): "Avoid",
                     ("Downtrend", "Neutral"): "Avoid", ("Uptrend", "Neutral"): "Chart only"}.items():
    got = tn.compute_signal(t, s)[0]
    check(got == want, f"L1: Signal {t} + {s} -> {want} (got {got})")
check(tn.SIGNAL_OUTCOMES == ("Confirmed", "Chart only", "Mixed", "News divergence",
                             "Chart up, news negative", "Avoid"), "L1: Signal labels, best-first, include Mixed")
check(set(tn.SIGNAL_EMOJI) == set(tn.SIGNAL_OUTCOMES), "L1: every Signal label has a dot")
check(ev.chart_rule_verdict({"trend": "Mixed", "tech_uptrend": 1}) == "HOLD", "L1: the chart rule reads Mixed as HOLD")
v, flag = ev.validate_verdict({"verdict": "ACCUMULATE", "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")},
                              {"trend": "Mixed", "vstop_weekly_direction": "Up", "vstop_weekly_weeks_since_change": 9})
check((v, flag) == ("HOLD", "UNSUPPORTED_ACCUMULATE"), "L1: ACCUMULATE on a Mixed trend is demoted to HOLD")
check("Mixed" in ev.VERDICT_RULES, "L1: the verdict rules tell the model what Mixed means")
check('["Any", "Strong Uptrend", "Uptrend", "Mixed", "Downtrend", "Strong Downtrend"]' in APP,
      "L1: the table's Trend filter offers Mixed")
check('"Mixed": "#' in APP and APP.count('"Mixed"') >= 3, "L1: the Trend cell and the Signal cell colour Mixed")

# --- F1 every changed config file is pushed at the end of the run ------------
import github_sync as gs
from fake_github import FakeGitHub, use

tmp = tempfile.mkdtemp()
saved_attrs = (gs.SCRIPT_DIR, gs.SYNC_STATE_FILE)
gs.SCRIPT_DIR, gs.SYNC_STATE_FILE = tmp, os.path.join(tmp, ".data_sync_state.json")
try:
    files = {"alerts_config.json": [{"id": "r1"}], "ticker_notes.json": {}, "settings.json": {"rsi_period": 14}}
    for name, data in files.items():
        Path(tmp, name).write_text(json.dumps(data, indent=2))
    # As if just pulled: the sync state knows each file's blob.
    gs._write_sync_state({"checked_at": 0, "blobs": {n: gs._git_blob_sha(os.path.join(tmp, n)) for n in files}})
    check(gs.unpushed_config_files() == [], "F1: freshly pulled config files are not pending")
    Path(tmp, "alerts_config.json").write_text(json.dumps([{"id": "r1"}, {"id": "r2"}], indent=2))
    check(gs.unpushed_config_files() == ["alerts_config.json"], "F1: a locally edited rule file is pending")
    Path(tmp, "custom_filters.json").write_text("{}")
    check("custom_filters.json" not in gs.unpushed_config_files(),
          "F1: a file with no pull/push record is NOT auto-pushed (a stale local copy must not overwrite the repo)")
    Path(tmp, "data_snapshot.json").write_text('{"generated_at": "x"}')
    check("data_snapshot.json" not in gs.unpushed_config_files(), "F1: workflow-generated files are never auto-pushed")

    fk = FakeGitHub({"alerts_config.json": [{"id": "r1"}]}); use(fk)
    pending = gs.unpushed_config_files()
    ok, msg = gs.push_all_config("t", "o/r", "main", filenames=pending, message="m")
    check(ok and fk.file("alerts_config.json") == [{"id": "r1"}, {"id": "r2"}], f"F1: the pending files are pushed ({msg})")
    check(gs.unpushed_config_files() == [], "F1: a successful push clears them (no re-push next run)")

    Path(tmp, "ticker_notes.json").write_text(json.dumps({"ACME": {"note": "x", "flag": "Red"}}))
    fk.on_patch = lambda: fk.commit_file("other.json", {})      # a workflow commits mid-push -> 422
    ok, _ = gs.push_all_config("t", "o/r", "main", filenames=gs.unpushed_config_files(), message="m")
    check(not ok and gs.unpushed_config_files() == ["ticker_notes.json"], "F1: a failed push leaves the file pending for the next run")
except AttributeError as e:
    check(False, f"F1: github_sync.unpushed_config_files exists ({e})")
finally:
    gs.SCRIPT_DIR, gs.SYNC_STATE_FILE = saved_attrs

i_push = APP.find("unpushed_config_files(")
check(i_push > APP.find("with tab_alerts:") > 0, "F1: the app pushes pending config at the END of the run, after every tab")
check("SKIP_GITHUB_PULL" in APP[i_push - 600:i_push + 600], "F1: ...and not under SKIP_GITHUB_PULL (local and AppTest runs)")

# --- F2 a mostly-failed news run does not post, then fail, then post again ---
import news_check as nc

WL = {"us_invested": ["A1", "A2"]}


def run_news(totals):
    for k in ("REFRESH_MARKETS", "REFRESH_LIMIT"):
        os.environ.pop(k, None)
    got = {}
    nc.get_gemini_api_key = lambda: "k"
    nc.load_watchlists = lambda: WL
    nc.build_news_summary = lambda wl, key: {"as_of": "d", "totals": totals, "markets": {"us_invested": {"summary": "S"}}}
    nc.load_news_summary = lambda: None
    nc.save_news_summary = lambda d: got.setdefault("saved", d)
    nc.load_discord_webhook = lambda: "https://discord.example/hook"
    nc.build_discord_messages = lambda d: ["msg"]
    nc.send_discord_batch = lambda *a, **kw: got.setdefault("sent", True) and (True, "")
    try:
        nc.main()
        got["exit"] = 0
    except SystemExit as e:
        got["exit"] = e.code
    return got


g = run_news({"searched": 51, "failed": 25})
check(g["exit"] == 1 and not g.get("sent") and g.get("saved"),
      f"F2: 25/51 failed -> saved for diagnosis, NOT posted, exit 1 so the gate retries ({g.get('exit')}, sent={g.get('sent')})")
g = run_news({"searched": 51, "failed": 3})
check(g["exit"] == 0 and g.get("sent"), "F2: a normal run still posts and succeeds")

# --- F3 / F4 the slot gate -----------------------------------------------------
import slot_gate

for wf in ("expert-views.yml", "fundamentals.yml", "news-summary.yml"):
    text = (REPO / ".github/workflows" / wf).read_text()
    rn = str(yaml.safe_load(text).get("run-name", ""))
    check("partial run" in rn and "inputs.markets" in rn and "inputs.limit" in rn,
          f"F3: {wf} names a markets/limit run 'partial run'")

base = datetime(2026, 9, 30, 13, 0, tzinfo=timezone.utc)
seen_paths = []


def fake_api(runs, jobs_by_id):
    def _api(path, token):
        seen_paths.append(path)
        if "/runs?" in path:
            page = int(path.split("&page=")[1].split("&")[0]) if "&page=" in path else 1
            return {"workflow_runs": runs if page == 1 else []}
        rid = int(path.split("/runs/")[1].split("/jobs")[0])
        return {"jobs": jobs_by_id[rid]}
    return _api


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


runs = [
    {"id": 1, "run_started_at": iso(base - timedelta(hours=1)), "display_title": "partial run"},
    {"id": 2, "run_started_at": iso(base - timedelta(hours=2)), "display_title": "Daily Expert Views Generation"},
    {"id": 3, "run_started_at": iso(base - timedelta(hours=3)), "display_title": "Daily Expert Views Generation"},
]
jobs = {1: [{"name": "build", "conclusion": "success"}],
        2: [{"name": "build", "conclusion": None}],           # still running
        3: [{"name": "build", "conclusion": "skipped"}]}
real_api = slot_gate._api
try:
    slot_gate._api = fake_api(runs, jobs)
    worked = slot_gate.worked_runs("o/r", "expert-views.yml", "build", "t", base - timedelta(hours=46))
finally:
    slot_gate._api = real_api
check(worked == [], f"F3: a partial run's success does not count as the slot's work ({worked})")
check(not any("status=completed" in p for p in seen_paths), "F4: runs are listed without the lagging status filter")
runs[0]["display_title"] = "Daily Expert Views Generation"
try:
    slot_gate._api = fake_api(runs, jobs)
    worked = slot_gate.worked_runs("o/r", "expert-views.yml", "build", "t", base - timedelta(hours=46))
finally:
    slot_gate._api = real_api
check(len(worked) == 1, "F3: a full run's success still counts (and an in-progress one does not)")

# --- F5 the benchmark shown is the one the row was measured against ----------
if not hasattr(sd, "benchmark_display_for_row"):
    sd.benchmark_display_for_row = lambda row: None
    check(False, "F5: stock_data.benchmark_display_for_row exists")
check(sd.benchmark_display_for_row({"index_name": "S&P 500", "market": "custom"}) == "S&P 500 (SPY)",
      "F5: an S&P 500 row reads 'S&P 500 (SPY)', whatever its watchlist says")
check(sd.benchmark_display_for_row({"index_name": "Nifty 500", "market": "custom"}) == "Nifty 500 (^CRSLDX)",
      "F5: a Nifty 500 row reads 'Nifty 500 (^CRSLDX)'")
_reg = sd.load_markets_registry
sd.load_markets_registry = lambda: {"custom": {"label": "Custom", "benchmark": "^IXIC"},
                                    "uk": {"label": "UK", "benchmark": "^FTSE"}}
try:
    check(sd.benchmark_display_for_row({"index_name": None, "market": "uk"}) == "^FTSE",
          "F5: an unclassified row falls back to its watchlist's benchmark, as the fetch does")
    _ls = sd.load_settings
    sd.load_settings = lambda: dict(sd.DEFAULT_SETTINGS)
    try:
        p = ev.build_expert_prompt({"ticker": "ACME", "market": "custom", "index_name": "S&P 500",
                                    "last_close": 10.0, "trend": "Uptrend"}, "news", "")
    finally:
        sd.load_settings = _ls
    check("vs S&P 500 (SPY)" in p and "^IXIC" not in p, "F5: the Expert Take prompt names the benchmark RS was computed against")
finally:
    sd.load_markets_registry = _reg
check('rs_caption = f"Mansfield RS vs {bench}"' not in APP and "Fallback benchmark" in APP,
      "F5: the tab caption no longer names the watchlist benchmark; Settings calls it a fallback")

# --- F6 stale = missed sessions, not calendar days ----------------------------
if not hasattr(sd, "data_end_is_stale"):
    sd.data_end_is_stale = lambda end, today: None
    check(False, "F6: stock_data.data_end_is_stale exists")
for end, today, stale, why in (
        ("2026-09-25", date(2026, 9, 28), False, "Friday's close on Monday"),
        ("2026-09-25", date(2026, 9, 29), False, "Friday on Tuesday (one missed session)"),
        ("2026-09-25", date(2026, 9, 30), True, "Friday on Wednesday (two missed)"),
        ("2026-09-21", date(2026, 9, 24), True, "Monday on Thursday (two missed)"),
        ("2026-09-24", date(2026, 9, 27), False, "Thursday on Sunday (one missed)"),
        ("bad", date(2026, 9, 30), False, "unparseable")):
    check(sd.data_end_is_stale(end, today) is stale, f"F6: {why} -> stale={stale}")
rows = [{"ticker": "ACME", "data_end": "2026-09-25", "data_end_age_days": 0}]
sd.refresh_data_end_age(rows, today=date(2026, 9, 30))
check(rows[0]["data_end_age_days"] == 5 and rows[0].get("sessions_behind") == 2,
      "F6: refresh_data_end_age keeps the day count and adds missed sessions")
check("date.today() - datetime.strptime" not in APP, "F6: Data Thru colouring no longer uses the server's calendar date")
check('r.get("sessions_behind", 0) >=' in APP, "F6: the stale caption counts missed sessions")

# --- F7 Reset to defaults resets the calculation settings only ----------------
d = tempfile.mkdtemp()
_sf = sd.SETTINGS_FILE
sd.SETTINGS_FILE = os.path.join(d, "settings.json")
try:
    mine = dict(sd.DEFAULT_SETTINGS, rsi_period=9, tech_uptrend_volume_ratio=0.5,
                note_dropdown_options="a, b", news_watchlist_scope=["x"], expert_reasoning_model="m")
    Path(sd.SETTINGS_FILE).write_text(json.dumps(mine))
    sd.save_settings(sd.calc_settings(sd.DEFAULT_SETTINGS))
    back = sd.load_settings()
    check(back["rsi_period"] == 14 and back["tech_uptrend_volume_ratio"] == 0.3, "F7: calculation settings go back to defaults")
    check(back["note_dropdown_options"] == "a, b" and back["news_watchlist_scope"] == ["x"]
          and back["expert_reasoning_model"] == "m", "F7: note options, news scope and AI models are kept")
finally:
    sd.SETTINGS_FILE = _sf
check("save_settings(dict(DEFAULT_SETTINGS))" not in APP and "save_settings(calc_settings(DEFAULT_SETTINGS))" in APP,
      "F7: the dialog's reset uses only the calculation settings")
check("_confirm_settings_reset" in APP, "F7: and asks for confirmation, listing what changes")

# --- D* text that disagreed with the code --------------------------------------
_ls = sd.load_settings
sd.load_settings = lambda: dict(sd.DEFAULT_SETTINGS)
try:
    p = ev.build_expert_prompt({"ticker": "ACME", "market": "us_invested", "last_close": 10.0,
                                "trend": "Uptrend", "index_name": "S&P 500"}, "news", "",
                               {"as_of": "2026-01-01 00:00", "sentiment": "Positive"})
finally:
    sd.load_settings = _ls
check("DEMA" not in p and "Daily SMAs" in p and "10 DSMA=" in p, "D1: the prompt calls the daily averages SMAs")
# 0.3 and "median" since 2026-10-04 (checks/test_trend_volume_100426.py V1/V2).
check("VStop uptrend > 3 wks" in p and "median Vol 10D > 0.3x" in p, "D2: the prompt states Tech Uptrend's strict thresholds")
check("~50 days" not in p and "~50 days" not in ev._quarter_fundamentals_text({"as_of": "x"}),
      "D3: section 4 no longer claims a fixed ~50-day window")
check("DSMA = daily SMA" in APP and "DSMA = daily EMA" not in APP, "D4: the footer says DSMA is a daily SMA")
check("they populate alert_matches" not in APP, "D7: the tab-rendering comment no longer cites a cross-tab dependency")
check("models/gemma-4-31b-it" not in _code_strings(REPO / "app.py"), "D8: the UI no longer offers gemma-4-31b-it")

# --- B1 the two "Show fundamental columns" toggles don't undo each other ------
# Found while verifying F1 headlessly: the sidebar copy, still holding its old
# session value, wrote it straight back after the top copy changed the setting
# (and vice versa), so neither toggle stuck. Each must drop BOTH widgets' state.
for key in ("show_fundamental_columns_toggle", "dash_show_fundamental_columns_toggle"):
    i = APP.find(f'key="{key}"')
    handler = APP[i:i + 900]
    check(i > 0 and "_set_show_fundamentals(" in handler,
          f"B1: the {key} handler goes through the shared setter")
i = APP.find("def _set_show_fundamentals(")
body = APP[i:i + 900]
check(i > 0 and all(k in body for k in ("show_fundamental_columns_toggle", "dash_show_fundamental_columns_toggle"))
      and "st.session_state.pop" in body, "B1: the shared setter drops both widgets' state before rerunning")

# --- B2 the per-tab AI model / budget pickers don't undo each other -----------
# Every market tab renders its own copy of the Expert Take and Sentiment model
# and thinking-budget pickers, all bound to ONE setting. A change on one tab was
# written back by the next tab's stale copy (reproduced: saves went new -> old),
# so the pickers never stuck. A change must drop every tab's copy.
i = APP.find("def _render_ai_section(")
section = APP[i:APP.find("def render_expert_view_expander(")]
check(section.count("_save_setting_reset_widgets(") >= 2 and "st.rerun()" not in section.split("c1, c2, c3, c4")[0],
      "B2: the model and budget pickers save through the shared reset helper")
i = APP.find("def _save_setting_reset_widgets(")
body = APP[i:i + 1200]
check(i > 0 and "startswith(" in body and "st.session_state.pop" in body and "st.rerun()" in body,
      "B2: the helper drops every tab's copy of the widget before rerunning")

# --- I3 the disclaimer fits by Discord's own count -----------------------------
import alerts

astral = "📈" * 900                       # 900 code points, 1800 UTF-16 units
out = alerts.with_disclaimer([astral])
check(out == [astral, alerts.DISCORD_FOOTER], "I3: an emoji-heavy last message gets the footer as its own message")
check(all(alerts._discord_len(m) <= alerts._DISCORD_SAFE_LEN for m in out), "I3: no message exceeds the safe length")

print("TOTAL", "all passed" if not fails else "")
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
