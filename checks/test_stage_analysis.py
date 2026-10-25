"""TheWrap Stage Analysis upload: parsing the indicator's CSV export, matching it to
the screener's tickers, storing it, and the columns it feeds.

The fixture has the real export's header and cell shapes with invented tickers
(ACME, ZED.NS, KAPPA.BO ...). What it pins:

  - "S1 - S2 (Developing 8P)" is previous - current (confidence, periods); "S? - S?"
    is Unknown with no confidence or periods, whatever "(Mature 0P)" follows it.
  - The export's bare symbols are matched to the tickers the watchlists hold through
    the Exchange column (NSE <-> .NS, BSE <-> .BO, anything else <-> no suffix), so one
    symbol on two exchanges is two different tickers.
  - An upload MERGES: tickers it lists are replaced, the rest are kept.
  - A value outside a column's vocabulary is blank and reported, never stored.
  - Every column is filterable, alertable, sortable best-first and has a definition.

Offline: no data files, no network.
"""

import ast
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import filters
import glossary
import github_sync
import stage_analysis as sa
import stock_data as sd

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


HEADER = "Ticker,Exchange,Watchlists,Close,Stage,RS,Benchmark,MOM,AvgVol,MCap,Fast,Med,Slow,Beta,Bal,CE,PEAD"
ROWS = [
    # Ticker, Exchange, Stage, MOM, CE, PEAD
    ("ACME", "NASDAQ", "S4 - S2 (Mature 24P)", "Weakening ↓", "No", "Strong"),
    ("ZED", "NSE", "S1 - S2 (Developing 8P)", "Weak ↓", "Yes", "Weak"),
    ("KAPPA", "BSE", "S3 - S2 (Established 11P)", "Strong ↑", "No", "Mod"),
    ("NEWCO", "NSE", "S? - S? (Mature 0P)", "Weak ↓", "No", "No"),          # too little history
    ("DUP", "NSE", "S2 - S3 (New 2P)", "Recovering ↑", "No", "No"),          # same symbol, two exchanges
    ("DUP", "NASDAQ", "S4 - S1 (New 1P)", "Weak ↓", "Yes", "No"),
    ("STRANGE", "NYSE", "S1 - S4 (Mature 40P)", "Sideways", "Maybe", "Huge"),  # labels the indicator never used
    ("BROKEN", "NYSE", "not a stage", "Weak ↓", "No", "No"),
    ("GHOST", "NSE", "S2 - S3 (New 2P)", "Weak ↓", "No", "No"),              # held on no watchlist
]


def csv_text(rows=ROWS, bom=False):
    lines = [HEADER]
    for sym, ex, stage, mom, ce, pead in rows:
        lines.append(f'{sym},{ex},"wl_a,wl_b",10.00,{stage},+1% Strong,CNX500,{mom},1Cr Rising,₹1Cr,+1%,+1%,+1%,1 Low,,{ce},{pead}')
    return ("﻿" if bom else "") + "\n".join(lines) + "\n"


UNIVERSE = ["ACME", "ZED.NS", "KAPPA.BO", "NEWCO.NS", "DUP.NS", "DUP", "STRANGE", "BROKEN", "HELD.NS"]

# --- parsing ------------------------------------------------------------------
cell = sa.parse_stage_cell("S1 - S2 (Developing 8P)")
check(cell == {"stage": "S2 Advancing", "stage_prev": "S1 Accumulation", "stage_conf": "Developing",
               "stage_periods": 8}, f"previous - current: the SECOND stage is the current one ({cell})")
cell = sa.parse_stage_cell("S? - S? (Mature 0P)")
check(cell["stage"] == "Unknown" and cell["stage_conf"] is None and cell["stage_periods"] is None,
      "S? - S? is Unknown with no confidence and no periods")
check(sa.parse_stage_cell("S3 - S4  ( New   1P )")["stage"] == "S4 Decline", "whitespace inside the cell is tolerated")
check(sa.parse_stage_cell("S5 - S2 (New 1P)") is None and sa.parse_stage_cell("") is None
      and sa.parse_stage_cell(None) is None, "a cell that is not a stage is None, never a guess")
check(sa.parse_stage_cell("S2 - S3 (Veteran 9P)")["stage_conf"] is None,
      "a confidence word outside the four known ones is left blank")

records, report = sa.parse_stage_csv(csv_text(bom=True).encode("utf-8"))
check(report["rows"] == len(ROWS) and report["bad_stage"] == 1,
      f"9 data rows read, the one unreadable Stage cell counted ({report})")
check(("BROKEN", "OTHER") not in records and len(records) == len(ROWS) - 1, "an unreadable Stage row is skipped")
check(report["bad_values"] == {"MOM": ["Sideways"], "CE": ["Maybe"], "PEAD": ["Huge"]},
      f"unknown MOM / CE / PEAD labels are reported ({report['bad_values']})")
strange = records[("STRANGE", "OTHER")]
check(strange["stage_mom"] is None and strange["cheat_entry"] is None and strange["pead"] is None
      and strange["stage"] == "S4 Decline", "...and stored blank, the rest of that row kept")
check(records[("ACME", "OTHER")]["pead"] == "Strong" and records[("ZED", "NSE")]["cheat_entry"] == "Yes",
      "a UTF-8 BOM on the first header does not hide the Ticker column")

for bad in ("Symbol,Stage\nACME,S1 - S2 (New 1P)\n", "Ticker,Close\nACME,1\n", ""):
    try:
        sa.parse_stage_csv(bad)
        check(False, f"a file without Ticker and Stage is refused ({bad[:20]!r})")
    except ValueError as e:
        check("Ticker" in str(e) and "Stage" in str(e), f"a file without Ticker and Stage is refused ({bad[:20]!r})")

# --- matching -------------------------------------------------------------------
matched, unmatched, missing = sa.match_to_universe(records, UNIVERSE)
check(matched["ZED.NS"]["stage"] == "S2 Advancing" and matched["KAPPA.BO"]["stage_conf"] == "Established"
      and matched["ACME"]["stage_prev"] == "S4 Decline", "NSE -> .NS, BSE -> .BO, NASDAQ -> no suffix")
check(matched["DUP.NS"]["stage"] == "S3 Distribution" and matched["DUP"]["stage"] == "S1 Accumulation",
      "one symbol on two exchanges stays two tickers")
check(unmatched == ["GHOST"], f"a symbol on no watchlist is reported ({unmatched})")
check(missing == ["BROKEN", "HELD.NS"] or set(missing) == {"BROKEN", "HELD.NS"},
      f"a held ticker the file lacks is reported ({missing})")
check(sa.ticker_region("zed.ns") == ("ZED", "NSE") and sa.ticker_region("KAPPA.BO") == ("KAPPA", "BSE")
      and sa.ticker_region("BRK-B") == ("BRK-B", "OTHER"), "ticker_region")

# --- storage: merge, load, save -----------------------------------------------------
d = tempfile.mkdtemp()
sa.STAGE_ANALYSIS_FILE = os.path.join(d, "stage_analysis.json")
check(sa.load_stage_analysis() == {"meta": {}, "tickers": {}}, "no file yet -> empty, not an error")
t1 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
first = sa.merge_upload(sa.load_stage_analysis(), matched, "all.csv", now=t1)
sa.save_stage_analysis(first)
check(sa.load_stage_analysis() == first and first["tickers"]["ZED.NS"]["uploaded"] == "2026-10-25"
      and first["meta"]["tickers_updated"] == len(matched), "saved and read back, each entry dated")

only_acme = {"ACME": {**matched["ACME"], "stage": "S3 Distribution"}}
t2 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
second = sa.merge_upload(first, only_acme, "one-list.csv", now=t2)
check(second["tickers"]["ACME"]["stage"] == "S3 Distribution" and second["tickers"]["ACME"]["uploaded"] == "2026-11-02",
      "a re-uploaded ticker is replaced and re-dated")
check(second["tickers"]["ZED.NS"] == first["tickers"]["ZED.NS"] and len(second["tickers"]) == len(first["tickers"]),
      "...and a ticker the new file lacks is kept untouched (a one-list export blanks nothing)")
check(first["tickers"]["ACME"]["stage"] == "S2 Advancing", "merge_upload does not mutate what it was given")

Path(sa.STAGE_ANALYSIS_FILE).write_text("{not json")
try:
    sa.load_stage_analysis()
    check(False, "a corrupt file raises")
except sd.DataFileError as e:
    check("stage_analysis.json" in str(e), "a corrupt file raises DataFileError naming it (never an empty default)")
Path(sa.STAGE_ANALYSIS_FILE).write_text(json.dumps([1, 2]))
check(sa.load_stage_analysis() == {"meta": {}, "tickers": {}}, "a file of the wrong shape reads as empty")

# --- rows ---------------------------------------------------------------------------
stored = second
rows = [{"ticker": "ACME"}, {"ticker": "ZED.NS"}, {"ticker": "HELD.NS"}]
sa.apply_stage_to_rows(rows, stored)
check(rows[1]["stage"] == "S2 Advancing" and rows[1]["stage_mom"] == "Weak ↓" and rows[1]["cheat_entry"] == "Yes"
      and rows[1]["pead"] == "Weak" and rows[1]["stage_periods"] == 8, "a stored ticker gets every field")
check(all(rows[2][f] is None for f in sa.ROW_FIELDS) and "stage" in rows[2],
      "an unstored ticker gets the keys with None, never a missing key")

rows = [{"ticker": "ZED.NS", "trend": "Uptrend"}]
sd.apply_view_fields_to_rows(rows, fundamentals={}, expert_views={}, interested=set(), stage=stored)
check(rows[0]["stage"] == "S2 Advancing" and "expert_take" in rows[0],
      "apply_view_fields_to_rows attaches them, so alerts see them like the app does")

# --- filters, alerts, sorting -------------------------------------------------------
metrics = sd.get_filterable_metrics(dict(sd.DEFAULT_SETTINGS))
for key, label in sa.COLUMN_DEFS:
    check(metrics.get(label) == key, f"{label!r} is filterable as {key}")
check(set(sa.CATEGORICAL_FIELDS) <= set(filters.CATEGORICAL_METRICS)
      and all(filters.CATEGORICAL_METRICS[k] == v for k, v in sa.CATEGORICAL_FIELDS.items()),
      "every categorical Stage field has its dropdown")
check("stage_periods" not in filters.CATEGORICAL_METRICS and "stage" in filters.TEXT_METRICS
      and "stage_periods" not in filters.TEXT_METRICS, "Stage Periods stays numeric, the labels stay text")
check(filters.CATEGORICAL_METRICS["stage"][0] == "S2 Advancing" and filters.CATEGORICAL_METRICS["stage"][-1] == "Unknown"
      and filters.CATEGORICAL_METRICS["stage_mom"][0] == "Strong ↑" and filters.CATEGORICAL_METRICS["pead"][0] == "Strong"
      and filters.CATEGORICAL_METRICS["cheat_entry"][0] == "Yes", "every categorical is declared best-first")

row = rows[0]
check(filters.passes_filter(row, {"metric_a": "stage", "operator": "in", "compare_type": "value",
                                  "value": ["S2 Advancing", "S1 Accumulation"]}), "Stage in [S2, S1] matches an S2 row")
check(not filters.passes_filter(row, {"metric_a": "stage", "operator": "in", "compare_type": "value",
                                      "value": ["S4 Decline"]}), "...and not an S4 filter")
check(filters.passes_filter(row, {"metric_a": "stage_periods", "operator": ">=", "compare_type": "value", "value": 5}),
      "Stage Periods >= 5 compares as a number")
blank = {"ticker": "HELD.NS", "stage": None, "stage_periods": None}
check(not filters.passes_filter(blank, {"metric_a": "stage", "operator": "in", "compare_type": "value",
                                        "value": ["S2 Advancing"]})
      and not filters.passes_filter(blank, {"metric_a": "stage_periods", "operator": ">", "compare_type": "value",
                                            "value": 0}), "a ticker with no upload fails a Stage filter closed, never raises")

# --- the rest of the registry --------------------------------------------------------
check(("stage_analysis.json" in dict(github_sync.SYNCABLE_FILES)
       and "stage_analysis.json" not in github_sync.WORKFLOW_GENERATED_FILES
       and "stage_analysis.json" in github_sync.CONFIG_PULLABLE_FILES
       and "stage_analysis.json" in github_sync.DATA_FILES),
      "the file is synced, pulled and bootstrapped like the other user data, and never treated as workflow output")

defs = glossary.column_definitions(dict(sd.DEFAULT_SETTINGS), {"w_fast": "10 WEMA", "w_mid": "20 WEMA", "w_slow": "40 WEMA",
                                                                "d_fast": "10 DSMA", "d_mid": "50 DSMA", "d_slow": "200 DSMA"})
check(all(label in defs and len(defs[label]) > 20 for _key, label in sa.COLUMN_DEFS),
      "every column has a glossary definition")
tip = glossary.stage_tooltip({"stage": "S2 Advancing", "stage_prev": "S1 Accumulation", "stage_conf": "Developing",
                              "stage_periods": 8, "stage_uploaded": "2026-10-25"})
check("S1 Accumulation" in tip and "8 periods (Developing)" in tip and "2026-10-25" in tip, f"the Stage tooltip: {tip!r}")
check("No Stage Analysis upload" in glossary.stage_tooltip({"stage": None})
      and "too little history" in glossary.stage_tooltip({"stage": "Unknown"}), "...and its two empty states")

app_tree = ast.parse((REPO / "app.py").read_text())
fn = next(n for n in ast.walk(app_tree) if isinstance(n, ast.FunctionDef) and n.name == "build_column_defs")
check(any(isinstance(n, ast.Starred) and getattr(n.value, "id", "") == "STAGE_COLUMN_DEFS" for n in ast.walk(fn)),
      "build_column_defs lists the Stage columns from the same list the filters use")
check(sa.DEFAULT_VISIBLE_KEYS == ("stage", "stage_mom", "cheat_entry", "pead")
      and sa.DEFAULT_HIDDEN_LABELS == {"Prev Stage", "Stage Conf", "Stage Periods"}
      and [k for k, _l in sa.COLUMN_DEFS[:4]] == list(sa.DEFAULT_VISIBLE_KEYS),
      "stage, mom, CE and PEAD show by default; the three parts of the Stage cell start hidden")

# a lightweight job (the alert gate) must still import filters without numpy
imports = {n.module for n in ast.walk(ast.parse((REPO / "stage_analysis.py").read_text()))
           if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(ast.parse((REPO / "stage_analysis.py").read_text()))
                                                if isinstance(n, ast.Import) for a in n.names}
check(imports <= {"csv", "io", "os", "re", "datetime", "json_store"}, f"stage_analysis stays stdlib-only ({sorted(imports)})")

print(f"\nFAILURES: {fails}")
sys.exit(1 if fails else 0)
