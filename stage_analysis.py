"""
TheWrap Stage Analysis: the sidebar CSV upload and the columns it feeds.

The indicator itself runs on TradingView; its screener export is uploaded here and
stored per ticker in stage_analysis.json. Nothing is computed -- the file is the
source, so every value is exactly what the indicator showed.

The export's header is: Ticker, Exchange, Watchlists, Close, Stage, RS, Benchmark,
MOM, AvgVol, MCap, Fast, Med, Slow, Beta, Bal, CE, PEAD. Four of those become
columns (plus three parts of the Stage cell, offered hidden):

  Stage  "S1 - S2 (Developing 8P)"  = previous stage - current stage (confidence,
         periods in that stage). Read as previous-then-current because on every row
         it agrees with the screener's own Trend (an Uptrend row ends in S2 and a
         Strong Downtrend row in S4). "S? - S?" is a ticker with too little history.
  MOM    Strong / Recovering / Weakening / Weak, with an arrow.
  CE     Cheat Entry, Yes / No.
  PEAD   Post-earnings announcement drift: Strong / Mod / Weak / No.

The export carries bare symbols ("AARTIPHARM", "KMEW") and an exchange, while the
screener's tickers carry Yahoo suffixes ("AARTIPHARM.NS", "KMEW.BO"). A row is matched
against the tickers the watchlists actually hold -- NSE <-> .NS, BSE <-> .BO, any
other exchange <-> no suffix -- instead of guessing a suffix, and what matches
nothing is reported back, never dropped silently.

Stdlib only (json_store, csv, re): the nightly alert job imports this through
stock_data.apply_view_fields_to_rows.
"""

import csv
import io
import os
import re
from datetime import datetime, timezone

from json_store import atomic_write_json, read_json_strict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STAGE_ANALYSIS_FILE = os.path.join(SCRIPT_DIR, "stage_analysis.json")

STAGE_NAMES = {"1": "Accumulation", "2": "Advancing", "3": "Distribution", "4": "Decline"}

# Every categorical is declared best-first (AGENTS.md, "Sorting"), so Top->Bottom
# reads the same on every column. Stages run in the order a stock is worth holding:
# advancing, then basing, then topping, then declining.
STAGE_VALUES = ["S2 Advancing", "S1 Accumulation", "S3 Distribution", "S4 Decline", "Unknown"]
STAGE_CONF_VALUES = ["Mature", "Established", "Developing", "New"]
STAGE_MOM_VALUES = ["Strong ↑", "Recovering ↑", "Weakening ↓", "Weak ↓"]
CE_VALUES = ["Yes", "No"]
PEAD_VALUES = ["Strong", "Mod", "Weak", "No"]

# row field -> the values it may hold, for filters.CATEGORICAL_METRICS. stage_periods
# is a count, so it is not here.
CATEGORICAL_FIELDS = {
    "stage": STAGE_VALUES,
    "stage_prev": STAGE_VALUES,
    "stage_conf": STAGE_CONF_VALUES,
    "stage_mom": STAGE_MOM_VALUES,
    "cheat_entry": CE_VALUES,
    "pead": PEAD_VALUES,
}

# The fields a stored ticker entry carries, in the order they are attached to rows.
ROW_FIELDS = ("stage", "stage_prev", "stage_conf", "stage_periods", "stage_mom", "cheat_entry", "pead")

# (row field, column label) in display order. ONE list, so the table's column
# registry (app.build_column_defs) and the filter registry
# (stock_data.get_filterable_metrics) cannot spell a label differently -- the glossary
# and the condition builder's captions resolve through those labels. The first four
# show by default, right after Sentiment; the other three are the parts of the Stage
# cell, offered hidden.
COLUMN_DEFS = [
    ("stage", "Stage"),
    ("stage_mom", "Stage Mom"),
    ("cheat_entry", "CE"),
    ("pead", "PEAD"),
    ("stage_prev", "Prev Stage"),
    ("stage_conf", "Stage Conf"),
    ("stage_periods", "Stage Periods"),
]
DEFAULT_VISIBLE_KEYS = ("stage", "stage_mom", "cheat_entry", "pead")
DEFAULT_HIDDEN_LABELS = {"Prev Stage", "Stage Conf", "Stage Periods"}
METRIC_LABELS = {label: key for key, label in COLUMN_DEFS}

_STAGE_CELL = re.compile(r"^S([1-4?])\s*-\s*S([1-4?])\s*\(\s*([A-Za-z]+)\s+(\d+)\s*P\s*\)$")


def _stage_label(digit):
    return f"S{digit} {STAGE_NAMES[digit]}" if digit in STAGE_NAMES else "Unknown"


def _clean(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def parse_stage_cell(text):
    """{"stage", "stage_prev", "stage_conf", "stage_periods"} from a cell like
    "S1 - S2 (Developing 8P)", or None if it is not that shape. A "S? - S?" cell is
    an Unknown stage with no confidence or periods: the "(Mature 0P)" the export
    prints beside it describes nothing."""
    m = _STAGE_CELL.match(_clean(text))
    if not m:
        return None
    prev, cur, conf, periods = m.groups()
    if cur == "?":
        return {"stage": "Unknown", "stage_prev": _stage_label(prev),
                "stage_conf": None, "stage_periods": None}
    conf = conf.capitalize()
    return {
        "stage": _stage_label(cur),
        "stage_prev": _stage_label(prev),
        "stage_conf": conf if conf in STAGE_CONF_VALUES else None,
        "stage_periods": int(periods),
    }


def ticker_region(ticker):
    """(bare symbol, region) of a screener ticker: ".NS" is NSE, ".BO" is BSE and a
    ticker with neither suffix is a US/other listing."""
    t = str(ticker or "").strip().upper()
    if t.endswith(".NS"):
        return t[:-3], "NSE"
    if t.endswith(".BO"):
        return t[:-3], "BSE"
    return t, "OTHER"


def _export_region(exchange):
    ex = _clean(exchange).upper()
    return ex if ex in ("NSE", "BSE") else "OTHER"


def parse_stage_csv(raw):
    """Parse an uploaded export.

    Returns (records, report). `records` maps (bare symbol, region) -> the fields of
    ROW_FIELDS; `report` is {"rows", "bad_stage", "bad_values"}: how many data rows
    the file held, how many had a Stage cell that was not understood (skipped), and
    the cells of MOM / CE / PEAD outside their known vocabulary (kept as None, so a
    renamed label upstream shows up here instead of silently matching nothing).

    Raises ValueError when the file cannot be this export (no Ticker or Stage column).
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    names = {str(h).strip().lower(): h for h in (reader.fieldnames or [])}
    if "ticker" not in names or "stage" not in names:
        raise ValueError(
            "This is not a Stage Analysis export: it needs a 'Ticker' and a 'Stage' column "
            f"(found: {', '.join(str(h) for h in (reader.fieldnames or [])) or 'no header'})."
        )

    def cell(row, name):
        key = names.get(name)
        return _clean(row.get(key)) if key else ""

    records, report = {}, {"rows": 0, "bad_stage": 0, "bad_values": {}}
    for row in reader:
        symbol = cell(row, "ticker").upper()
        if not symbol:
            continue
        report["rows"] += 1
        parsed = parse_stage_cell(cell(row, "stage"))
        if parsed is None:
            report["bad_stage"] += 1
            continue
        for field, column, vocab in (("stage_mom", "mom", STAGE_MOM_VALUES),
                                     ("cheat_entry", "ce", CE_VALUES),
                                     ("pead", "pead", PEAD_VALUES)):
            value = cell(row, column)
            if value in vocab:
                parsed[field] = value
            else:
                parsed[field] = None
                if value:
                    report["bad_values"].setdefault(column.upper(), set()).add(value)
        records[(symbol, _export_region(cell(row, "exchange")))] = parsed
    report["bad_values"] = {k: sorted(v) for k, v in report["bad_values"].items()}
    return records, report


def match_to_universe(records, universe):
    """Pair parsed records with the tickers the watchlists hold.

    Returns ({ticker: fields}, unmatched, missing): `unmatched` are the export's
    symbols no watchlist holds, `missing` the watchlist tickers the export lacks.
    """
    by_key = {}
    for ticker in universe:
        by_key.setdefault(ticker_region(ticker), ticker)
    matched = {}
    for key, fields in records.items():
        ticker = by_key.get(key)
        if ticker:
            matched[ticker] = dict(fields)
    unmatched = sorted(sym for (sym, _region) in records if (sym, _region) not in by_key)
    missing = sorted(t for k, t in by_key.items() if t not in matched)
    return matched, unmatched, missing


def load_stage_analysis():
    """{"meta": {...}, "tickers": {ticker: fields}}. Raises stock_data.DataFileError on
    a file that exists but will not parse -- the app's preflight names it and stops
    rather than render empty columns and let the next upload overwrite the file."""
    data = read_json_strict(STAGE_ANALYSIS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    tickers = data.get("tickers")
    return {"meta": data.get("meta") if isinstance(data.get("meta"), dict) else {},
            "tickers": tickers if isinstance(tickers, dict) else {}}


def save_stage_analysis(data):
    atomic_write_json(STAGE_ANALYSIS_FILE, data)


def merge_upload(existing, matched, filename="", now=None):
    """The file's contents after an upload: every matched ticker replaced, every other
    one kept as it was. Merged rather than replaced so a CSV exported from ONE
    watchlist does not blank the rest; each entry carries the date it was uploaded, so
    an old one is visible as old."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d")
    tickers = dict(existing.get("tickers") or {})
    for ticker, fields in matched.items():
        tickers[ticker] = {**fields, "uploaded": stamp}
    meta = {"uploaded_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": _clean(filename)[:120],
            "tickers_updated": len(matched)}
    return {"meta": meta, "tickers": tickers}


def apply_stage_to_rows(rows, data=None):
    """Attach the Stage Analysis fields to every row, in place. A ticker with no stored
    entry gets None for each (the cell shows a dash and a filter on it fails closed),
    like every other derived field -- never a missing key."""
    tickers = (load_stage_analysis() if data is None else data).get("tickers") or {}
    for row in rows:
        entry = tickers.get(row.get("ticker")) or {}
        for field in ROW_FIELDS:
            row[field] = entry.get(field)
        row["stage_uploaded"] = entry.get("uploaded")
    return rows
