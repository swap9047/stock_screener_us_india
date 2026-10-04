"""The dot on the ticker is Expert Take's colour; Signal is retired; Flag is manual.

Signal (Trend x Sentiment) was retired on 2026-10-04 at the owner's request:
Expert Take now carries both of its inputs plus Tech Uptrend, TA Rules and the
news step, and the two told different stories -- 15 tickers read Signal "News
divergence" (yellow) while Expert Take said Caution (red), because TA said Exit.
One verdict, one colour: the dot next to the ticker is Expert Take's.

  🟢 Accumulate   🟡 Hold   🔴 Caution   ⚪ Pending -- your manual Flag wins the dot.

Offline: invented tickers and views, no data files, no network.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import expert_views as ev
import filters
import stock_data as sd
import ticker_notes as tn

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


src = (REPO / "app.py").read_text()

# --- Signal is gone everywhere ------------------------------------------------
check(not hasattr(tn, "compute_signal") and not hasattr(tn, "SIGNAL_OUTCOMES") and not hasattr(tn, "SIGNAL_EMOJI"),
      "ticker_notes no longer computes a Signal")
check("signal" not in filters.CATEGORICAL_METRICS and "signal" not in filters.TEXT_METRICS,
      "no Signal filter dropdown")
check("Signal" not in sd.get_filterable_metrics(dict(sd.DEFAULT_SETTINGS)), "Signal is not filterable or alertable")
check('("signal", "Signal")' not in src and '"Signal": (' not in src and "SIGNAL_COLORS" not in src
      and 'r.get("signal")' not in src, "app.py has no Signal column, glossary entry, colours or dot")

# --- apply_notes_to_rows: manual flag only, no signal ----------------------------
rows = [{"ticker": "ACME", "trend": "Uptrend"}, {"ticker": "ZED.NS", "trend": "Uptrend"}]
tn.apply_notes_to_rows(rows, notes={"ZED.NS": {"note": "", "flag": "Blue"}}, fundamentals={})
acme, zed = rows
check(acme["flag"] == "" and acme["flag_reason"] == "", "no manual flag -> Flag is empty (never automatic)")
check(zed["flag"] == "Blue" and zed["flag_reason"] == tn.MANUAL_FLAG_REASON, "a manual flag is kept as set")
check("signal" not in acme and "signal_reason" not in acme, "rows carry no signal field")

# --- the dot -----------------------------------------------------------------------
emoji = getattr(ev, "EXPERT_TAKE_EMOJI", None)
check(emoji == {"Accumulate": "🟢", "Hold": "🟡", "Caution": "🔴", "Pending": "⚪"},
      f"Expert Take colours: green / yellow / red / white ({emoji})")
check(set(emoji or {}) == set(filters.CATEGORICAL_METRICS["expert_take"]), "every Expert Take value has a dot")
cell = src.split("def _ticker_cell(r):")[1][:1600]
check("EXPERT_TAKE_EMOJI.get(" in cell and 'r.get("expert_take")' in cell and "FLAG_EMOJI" in cell,
      "the ticker dot is your Flag if set, otherwise Expert Take's colour")
check("Expert Take: " in cell and "expert_take_for_row" in cell, "the dot's hover names the verdict and its reasons")

# --- ⚑ marks only a news downgrade ------------------------------------------------
for row, want in (({"trend": "Uptrend", "tech_uptrend": 1, "ta_rules": "Maintain/Add"}, "ACCUMULATE"),
                  ({"trend": "Strong Uptrend", "tech_uptrend": 0, "ta_rules": "Maintain/Add"}, "ACCUMULATE"),
                  ({"trend": "Mixed", "tech_uptrend": 0, "ta_rules": "Maintain/Add"}, "HOLD"),
                  ({"trend": "Downtrend", "tech_uptrend": 1, "ta_rules": "Exit"}, "CAUTION"),
                  ({"trend": None}, "PENDING")):
    check(ev.decide_expert_verdict(row, "Neutral")[0] == want, f"columns' verdict: {row} -> {want}")
check('badge = f"{badge} ⚑"' in src and 'if take["news_lowered"]:' in src,
      "the Expert Take cell adds ⚑ only when news lowered the columns' verdict")
check('"Also used: this quarter' in src, "the hover text says when this quarter's facts were used")
check("Its colour is the dot next to the ticker" in src, "the Expert Take glossary says the dot is its colour")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
