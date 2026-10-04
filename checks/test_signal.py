"""Signal (Chart x News) replaced the automatic flag vote, and Flag is manual-only.

The vote counted the chart three times (Expert Take, Trend and Tech Uptrend
largely agreed) and the news once, painted 56% of tickers Green, and hid three
different situations inside Yellow. Signal uses the two independent inputs:

  Trend up   + Sentiment Positive          -> Confirmed
  Trend up   + Neutral / Unknown           -> Chart only
  Trend up   + Negative                    -> Chart up, news negative
  Trend down + Positive                    -> News divergence
  Trend down + anything else               -> Avoid

Offline: invented tickers and views, no data files, no network.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import filters
import stock_data as sd
import ticker_notes as tn

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


GRID = {
    ("Strong Uptrend", "Positive"): "Confirmed",
    ("Uptrend", "Positive"): "Confirmed",
    ("Uptrend", "Neutral"): "Chart only",
    ("Uptrend", "Unknown"): "Chart only",
    ("Uptrend", "Negative"): "Chart up, news negative",
    ("Downtrend", "Positive"): "News divergence",
    ("Strong Downtrend", "Positive"): "News divergence",
    ("Downtrend", "Neutral"): "Avoid",
    ("Downtrend", "Unknown"): "Avoid",
    ("Downtrend", "Negative"): "Avoid",
}
for (trend, sent), want in GRID.items():
    got = tn.compute_signal(trend, sent)[0]
    check(got == want, f"{trend} + {sent} -> {want} (got {got})")
check(tn.compute_signal(None, "Positive")[0] == "", "no Trend yet -> no Signal, not a guess")
label, reason = tn.compute_signal("Uptrend", "Positive", "Guidance ↑")
check(reason == "Chart: Uptrend · News: Positive (Guidance ↑)", f"the reason names both inputs: {reason!r}")

# --- apply_notes_to_rows: manual flag only, signal from the GUARDED sentiment --
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
fresh_pos = {"as_of": NOW, "sentiment": "Positive", "earnings_summary": "EPS beat", "future_guidance": "N/A",
             "analyst_coverage": "N/A", "eps_value": "$1.10 vs $1.00", "guidance_change": "raised",
             "analyst_action": None}
stale_pos = {**fresh_pos, "as_of": "2026-01-01 00:00"}
rows = [{"ticker": "ACME", "trend": "Uptrend"}, {"ticker": "ZED.NS", "trend": "Uptrend"},
        {"ticker": "OLD", "trend": "Downtrend"}]
tn.apply_notes_to_rows(rows, notes={"ZED.NS": {"note": "", "flag": "Blue"}},
                       fundamentals={"ACME": fresh_pos, "OLD": stale_pos})
acme, zed, old = rows
check(acme["signal"] == "Confirmed" and "Guidance ↑" in acme["signal_reason"], "Uptrend + fresh Positive -> Confirmed, with the guidance tag")
check(acme["flag"] == "" and acme["flag_reason"] == "", "no manual flag -> Flag is empty (never automatic)")
check(zed["flag"] == "Blue" and zed["flag_reason"] == tn.MANUAL_FLAG_REASON, "a manual flag is kept as set")
check(zed["signal"] == "Chart only", "...and the ticker still gets its Signal (no Sentiment on file -> Chart only)")
check(old["signal"] == "Avoid", "a STALE Positive is not news: Downtrend + stale -> Avoid, not News divergence")

# --- registration: filter, alerts, sort order, column, glossary ---------------
check(filters.CATEGORICAL_METRICS["signal"] == list(tn.SIGNAL_OUTCOMES), "the filter dropdown offers the five labels, best-first")
check("signal" in filters.TEXT_METRICS, "signal is a text metric (kept out of Metric B)")
check(sd.get_filterable_metrics(dict(sd.DEFAULT_SETTINGS)).get("Signal") == "signal", "Signal is filterable and alertable")
check(filters.passes_filter({"signal": "Confirmed"}, {"metric_a": "signal", "operator": "in", "compare_type": "value",
                                                     "value": ["Confirmed"]}),
      "a rule 'Signal in [Confirmed]' matches a Confirmed row")
check(set(tn.SIGNAL_EMOJI) == set(tn.SIGNAL_OUTCOMES), "every label has a dot colour")
src = (REPO / "app.py").read_text()
check('("signal", "Signal")' in src and '"Signal": (' in src and "SIGNAL_COLORS" in src,
      "app.py registers the column, its glossary entry and its colours")

# --- ⚑ on Expert Take where it differs from the chart -------------------------
import expert_views as ev

# Since 2026-10-04 the columns decide Expert Take (checks/test_expert_take_100426.py),
# so the only thing that can differ from them is news: ⚑ marks a news downgrade.
for row, want in (({"trend": "Uptrend", "tech_uptrend": 1, "ta_rules": "Maintain/Add"}, "ACCUMULATE"),
                  ({"trend": "Strong Uptrend", "tech_uptrend": 0, "ta_rules": "Maintain/Add"}, "ACCUMULATE"),
                  ({"trend": "Mixed", "tech_uptrend": 0, "ta_rules": "Maintain/Add"}, "HOLD"),
                  ({"trend": "Downtrend", "tech_uptrend": 1, "ta_rules": "Exit"}, "CAUTION"),
                  ({"trend": None}, "PENDING")):
    check(ev.decide_expert_verdict(row, "Neutral")[0] == want, f"columns' verdict: {row} -> {want}")
check('badge = f"{badge} ⚑"' in src and 'if take["news_lowered"]:' in src,
      "the Expert Take cell adds ⚑ only when news lowered the columns' verdict")
check('"Also used: this quarter' in src, "the hover text says when this quarter's facts were used")

print("TOTAL", "all passed" if not fails else "")
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
