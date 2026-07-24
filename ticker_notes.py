"""
Per-ticker free-text notes and a colored "flag" marker -- lets you jot down
a reminder ("earnings 8/14, watch guidance") or bookmark a ticker with a
color (Red/Yellow/Green/Blue) to keep track of things at a glance, without
that being a computed metric like custom_columns.py's formulas.

Stored in ticker_notes.json as {ticker: {"note": str, "flag": str}}, keyed
by the SAME ticker symbol used everywhere else in the app (e.g. "AAPL",
"RELIANCE.NS") -- global across both markets, not per-market, since a
ticker symbol is already unique across the whole watchlist.

Both fields flow into every row dict via apply_notes_to_rows(), called from
stock_data.fetch_all_markets() (same pattern as custom_columns.py), so a
note/flag is available to the table, the column picker, custom filters,
and alert conditions -- and to the headless alert_check.py/refresh_data.py
scripts too, not just the interactive app.
"""

import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TICKER_NOTES_FILE = os.path.join(SCRIPT_DIR, "ticker_notes.json")

# Fixed, small palette rather than free-form colors/labels -- keeps the
# marker compact next to the ticker symbol and keeps the categorical
# filter/alert-condition picker (see filters.CATEGORICAL_METRICS) a short,
# stable multiselect instead of an ever-growing list of one-off labels.
# The stored/filtered value IS the label ("Red", not "red") -- same
# convention as Trend/Vol Trend/etc in stock_data.get_filterable_metrics,
# so it shows up consistently in the table, the filter picker, and the
# Flag column all using the exact same string.
FLAG_CHOICES = ["Red", "Yellow", "Green", "Blue"]
FLAG_EMOJI = {"Red": "🔴", "Yellow": "🟡", "Green": "🟢", "Blue": "🔵"}
NO_FLAG = ""  # stored value for "no flag set"


def load_ticker_notes():
    if not os.path.exists(TICKER_NOTES_FILE):
        return {}
    try:
        with open(TICKER_NOTES_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_ticker_notes(notes):
    with open(TICKER_NOTES_FILE, "w") as f:
        json.dump(notes, f, indent=2)


def get_ticker_note(notes, ticker):
    entry = notes.get(ticker) or {}
    return entry.get("note", "")


def get_ticker_flag(notes, ticker):
    entry = notes.get(ticker) or {}
    flag = entry.get("flag", "")
    return flag if flag in FLAG_CHOICES else NO_FLAG


def set_ticker_note(notes, ticker, note, flag):
    """Updates (or removes) one ticker's entry in `notes` in place. An
    entirely empty entry (no note text AND no flag) is deleted rather than
    kept as {"note": "", "flag": ""} clutter in the JSON file."""
    note = (note or "").strip()
    flag = flag if flag in FLAG_CHOICES else NO_FLAG
    if not note and not flag:
        notes.pop(ticker, None)
    else:
        notes[ticker] = {"note": note, "flag": flag}
    return notes


def flag_marker_html(flag):
    """Emoji + trailing space to prepend to a ticker's display text, or
    "" if unflagged -- used to satisfy 'flag the ticker symbol within the
    ticker column' rather than only via a separate Flag column."""
    emoji = FLAG_EMOJI.get(flag)
    return f"{emoji} " if emoji else ""


def compute_auto_flag(row):
    """Evaluates technical criteria to auto-assign a Green or Red flag.
    Returns (flag_color, reason_string) or ("", "") for neutral.

    Rules (first match wins):
      GREEN:
        1. Trend = "Strong Uptrend"
        2. Trend = "Uptrend" AND Vol = "Exploding" AND Near 52w High (within 5%)
        3. Tech Uptrend = Yes AND Trend = "Uptrend" AND Net Vol = "Positive" AND Near High (within 20%)
        4. Trend = "Uptrend" AND Vol = "Exploding" AND VStop Dir = "Up" >= 2w AND Net Vol = "Pos" AND Near High (within 20%)
      RED:
        1. Trend = "Strong Downtrend" AND Tech Uptrend = No
        2. Deep Drawdown (>30% off high) AND Volume Drying ("Declining" or "In-line")
        3. Trend = "Downtrend" AND Vol = "Exploding" AND Net Vol = "Negative" AND Near 52w Low (within 5%)
        4. Trend = "Downtrend" AND Vol in ["Declining", "In-line", "Exploding(Neg)"] AND VStop Dir = "Down" >= 2w
    """
    trend = row.get("trend", "")
    tech_uptrend = row.get("tech_uptrend", False)
    vol_trend = row.get("volume_trend", "")
    vstop_dir = row.get("vstop_weekly_direction", "")
    vstop_weeks = row.get("vstop_weekly_weeks_since_change")
    net_vol_dir = row.get("net_volume_10d_dir", "")
    last_close = row.get("last_close")
    week52_high = row.get("week52_high")
    week52_low = row.get("week52_low")
    
    near_high_20 = False
    near_high_5 = False
    drawdown_30 = False
    near_low_5 = False
    
    if last_close is not None and week52_high is not None and week52_high > 0:
        near_high_20 = last_close >= week52_high * 0.80
        near_high_5 = last_close >= week52_high * 0.95
        drawdown_30 = last_close <= week52_high * 0.70
        
    if last_close is not None and week52_low is not None and week52_low > 0:
        near_low_5 = last_close <= week52_low * 1.05

    # --- GREEN rules (first match wins) ---
    if trend == "Strong Uptrend":
        return "Green", "Strong Uptrend"
        
    if (trend == "Uptrend"
            and vol_trend == "Exploding"
            and near_high_5):
        return "Green", "Uptrend + Exploding vol + Near 52w High (5%)"

    if (tech_uptrend
            and trend == "Uptrend"
            and net_vol_dir == "Positive"
            and near_high_20):
        return "Green", "Tech Uptrend + Uptrend + Positive Vol + Near High (20%)"

    if (trend == "Uptrend"
            and vol_trend == "Exploding"
            and vstop_dir == "Up"
            and vstop_weeks is not None
            and vstop_weeks >= 2
            and net_vol_dir == "Positive"
            and near_high_20):
        return "Green", "Uptrend + Exploding(Pos) vol + VStop Up ≥2w + Near High (20%)"

    # --- RED rules (first match wins) ---
    if trend == "Strong Downtrend" and not tech_uptrend:
        return "Red", "Strong Downtrend + No Tech Uptrend"
        
    if drawdown_30 and vol_trend in ("Declining", "In-line"):
        return "Red", "Deep Drawdown (>30%) + Volume Drying"
        
    if (trend == "Downtrend"
            and vol_trend == "Exploding" 
            and net_vol_dir == "Negative"
            and near_low_5):
        return "Red", "Downtrend + Exploding(Neg) vol + Near 52w Low (5%)"

    if (trend == "Downtrend"
            and (vol_trend in ("Declining", "In-line") or (vol_trend == "Exploding" and net_vol_dir == "Negative"))
            and vstop_dir == "Down"
            and vstop_weeks is not None
            and vstop_weeks >= 2):
        if vol_trend == "Exploding":
            reason_vol = "Exploding(Neg)"
        else:
            reason_vol = vol_trend
        return "Red", f"Downtrend + {reason_vol} volume + VStop Down ≥2w"

    return "", ""


def apply_notes_to_rows(rows, notes=None):
    """Attaches `note`, `flag`, and `flag_reason` fields onto every row dict
    in place, from the shared ticker_notes.json (or an already-loaded `notes`
    dict, to avoid re-reading the file once per market).

    Flag priority:
      1. Manual flag from ticker_notes.json -- never overridden.
      2. Auto-computed flag from compute_auto_flag() -- applied only when
         no manual flag is set.
      3. No flag -- neutral.

    `flag_reason` is set on every row:
      - "Manually assigned" if a manual flag is present.
      - The rule description (e.g. "Strong Uptrend") for auto-flags.
      - "" if no flag at all."""
    if notes is None:
        notes = load_ticker_notes()
    for row in rows:
        ticker = row.get("ticker")
        row["note"] = get_ticker_note(notes, ticker)
        manual_flag = get_ticker_flag(notes, ticker)
        if manual_flag:
            row["flag"] = manual_flag
            row["flag_reason"] = "Manually assigned"
        else:
            auto_flag, auto_reason = compute_auto_flag(row)
            row["flag"] = auto_flag
            row["flag_reason"] = auto_reason
    return rows

