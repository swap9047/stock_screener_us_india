"""Stage Vol: the volume WORD of the Stage Analysis export's AvgVol cell (owner, 2026-11-05).

V1 parse_avg_vol_label keeps only the word: the figure and its unit (Cr / M / B, a rupee or
   dollar sign) and the arrows some labels carry are dropped; case does not matter.
V2 parse_stage_csv fills stage_vol per row; a word outside the vocabulary is reported under
   AVGVOL and left blank, not guessed at; an export without an AvgVol column still imports.
V3 The field travels like Stage Mom: stored by merge_upload, attached by apply_stage_to_rows
   (a ticker with no upload gets None, never a missing key).
V4 It is a column, a filterable categorical with the five values best-first, in the glossary
   and in the market_api stage group, so the screen and the research library agree.
V5 The cell is coloured by its word (the same map the table uses) and a blank is unstyled.

Offline: invented tickers and an invented export.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import stage_analysis as sa

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- V1 the word only ---------------------------------------------------------------------------
cases = {
    "281.83Cr Stable": "Stable", "85.13M Rising": "Rising", "1.42B Declining": "Declining",
    "12.4B Drying Up": "Drying Up", "66.97Cr Explosive ↑↑": "Explosive",
    "₹3.5Cr Rising": "Rising", "$12.2B  stable": "Stable", "Drying Up": "Drying Up",
    "5Cr Surging": None, "": None, None: None, "281.83Cr": None,
}
for text, want in cases.items():
    check(sa.parse_avg_vol_label(text) == want, f"V1: {text!r} -> {want!r} (got {sa.parse_avg_vol_label(text)!r})")
check(sa.STAGE_VOL_VALUES == ["Explosive", "Rising", "Stable", "Declining", "Drying Up"],
      "V1: the five words, best-first (a surge, growing, flat, shrinking, dried up)")

# --- V2 the export ------------------------------------------------------------------------------
HEADER = "Ticker,Exchange,Watchlists,Close,Stage,RS,Benchmark,MOM,AvgVol,MCap,Fast,Med,Slow,Beta,Bal,CE,PEAD\n"
CSV = HEADER + "\n".join([
    "ACME,NASDAQ,us_invested,10,S2 - S2 (Mature 20P),+1% Strong,SPY,Strong ↑,1.2B Rising,$1B,1%,2%,3%,1 Low,,No,No",
    "ZED,NSE,india_invested,20,S1 - S2 (Developing 8P),+1% Strong,CNX500,Weak ↓,66.97Cr Explosive ↑↑,₹1Cr,1%,2%,3%,1 Low,,No,Weak",
    "QUX,BSE,india_watchlist,30,S3 - S4 (New 2P),-1% Weak,CNX500,Weak ↓,3Cr Drying Up,₹1Cr,1%,2%,3%,1 Low,,No,No",
    "ODD,NSE,india_watchlist,40,S2 - S3 (New 1P),-1% Weak,CNX500,Weak ↓,3Cr Surging,₹1Cr,1%,2%,3%,1 Low,,No,No",
    "NOVOL,NSE,india_watchlist,50,S2 - S3 (New 1P),-1% Weak,CNX500,Weak ↓,,₹1Cr,1%,2%,3%,1 Low,,No,No",
]) + "\n"
records, report = sa.parse_stage_csv(CSV)
check(records[("ACME", "OTHER")]["stage_vol"] == "Rising" and records[("ZED", "NSE")]["stage_vol"] == "Explosive"
      and records[("QUX", "BSE")]["stage_vol"] == "Drying Up", "V2: the word is read off each row, arrows and figures dropped")
check(records[("ODD", "NSE")]["stage_vol"] is None and report["bad_values"].get("AVGVOL") == ["Surging"],
      f"V2: an unknown word is left blank and named in the preview ({report['bad_values']})")
check(records[("NOVOL", "NSE")]["stage_vol"] is None and "AVGVOL" in report["bad_values"] and report["bad_values"]["AVGVOL"] == ["Surging"],
      "V2: an empty AvgVol cell is blank and not reported as a bad value")
no_col = "Ticker,Exchange,Stage,MOM,CE,PEAD\nACME,NASDAQ,S2 - S2 (Mature 20P),Strong ↑,No,No\n"
rec2, rep2 = sa.parse_stage_csv(no_col)
check(rec2[("ACME", "OTHER")]["stage_vol"] is None and not rep2["bad_values"], "V2: an export without AvgVol still imports, Stage Vol blank")
check(not any(c.isdigit() for v in records.values() for c in str(v["stage_vol"] or "")),
      "V2: no digits survive in any stored Stage Vol")

# --- V3 stored and attached ---------------------------------------------------------------------
matched, _unmatched, _missing = sa.match_to_universe(records, ["ACME", "ZED.NS", "QUX.BO"])
stored = sa.merge_upload({"meta": {}, "tickers": {}}, matched, "x.csv")
check(stored["tickers"]["ZED.NS"]["stage_vol"] == "Explosive" and stored["tickers"]["QUX.BO"]["stage_vol"] == "Drying Up",
      "V3: merge_upload stores the word per ticker")
rows = [{"ticker": "ACME"}, {"ticker": "ZED.NS"}, {"ticker": "NOPE"}]
sa.apply_stage_to_rows(rows, stored)
check([r["stage_vol"] for r in rows] == ["Rising", "Explosive", None], "V3: apply_stage_to_rows attaches it, None for an uncovered ticker")
check("stage_vol" in sa.ROW_FIELDS, "V3: it is one of the stored row fields")

# --- V4 registered everywhere a column must be -----------------------------------------------------
import filters
import glossary
import market_api
from stock_data import get_filterable_metrics, DEFAULT_SETTINGS

check(("stage_vol", "Stage Vol") in sa.COLUMN_DEFS and "stage_vol" in sa.DEFAULT_VISIBLE_KEYS
      and "Stage Vol" not in sa.DEFAULT_HIDDEN_LABELS, "V4: a column, shown by default")
check(filters.CATEGORICAL_METRICS.get("stage_vol") == sa.STAGE_VOL_VALUES, "V4: a categorical filter with the five values")
metrics = get_filterable_metrics(DEFAULT_SETTINGS)
check(any(v == "stage_vol" for v in (metrics.values() if isinstance(metrics, dict) else [m[0] for m in metrics])) or "Stage Vol" in str(metrics),
      "V4: it is offered in the condition builder")
defs = glossary.column_definitions(dict(DEFAULT_SETTINGS), glossary.ema_col_labels(dict(DEFAULT_SETTINGS)))
check("Stage Vol" in defs and "without the figure" in defs["Stage Vol"], "V4: the glossary explains it")
grp = dict(market_api.METRIC_GROUPS)["stage"][1]
check(any(f == "stage_vol" and lbl == "Stage Vol" for f, lbl, *_ in grp), "V4: the research library's stage group carries it")

# --- V5 colours ---------------------------------------------------------------------------------
import re
src = (REPO / "app.py").read_text()
m = re.search(r"STAGE_VOL_COLORS = \{([^}]*)\}", src)
colors = dict(re.findall(r'"([^"]+)":\s*"(#[0-9a-fA-F]{6})"', m.group(1))) if m else {}
check(set(colors) == set(sa.STAGE_VOL_VALUES), f"V5: every word has a colour ({sorted(colors)})")
check(colors.get("Explosive") == "#1e8449" and colors.get("Drying Up") == "#c0392b", "V5: a surge is green and dried-up volume red")
check('col == "Stage Vol"' in src and "STAGE_VOL_COLORS.get(val)" in src, "V5: style_row colours the Stage Vol cell by its word")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
