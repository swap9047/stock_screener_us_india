"""
Per-ticker free-text notes and a colored "flag" marker -- lets you jot down
a reminder ("earnings 8/14, watch guidance") or bookmark a ticker with a
color (Red/Yellow/Green/Blue) to keep track of things at a glance, without
that being a computed metric like custom_columns.py's formulas.

Stored in ticker_notes.json as {ticker: {"note": str, "flag": str}}, keyed
by the SAME ticker symbol used everywhere else in the app (e.g. "AAPL",
"TCS.NS") -- global across both markets, not per-market, since a
ticker symbol is already unique across the whole watchlist.

Flag is ONLY what you set by hand. The automatic read is Expert Take, whose
colour is the ticker's dot when no flag is set (app._ticker_cell). It replaced
`signal` (Trend x Sentiment) on 2026-10-04, which had replaced a 4-signal flag
vote on 2026-10-02.

All three fields flow into every row dict via apply_notes_to_rows(), called from
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
# flag_reason of a flag the user set by hand -- how expert_views tells it
# apart from the auto-vote, which it must not show the model.
MANUAL_FLAG_REASON = "Manually assigned"

# Signal: the automatic read, from the two independent inputs only -- the chart
# (Trend) and the news (Sentiment). Best-first, which is also
# filters.CATEGORICAL_METRICS' declaration (sort) order.
def load_ticker_notes():
    """Notes and flags per ticker. Raises stock_data.DataFileError on a corrupt
    file rather than returning {} -- hand-written notes are the least
    replaceable data here, and an empty default would render none and let the
    next save write that emptiness back."""
    from stock_data import read_json_strict
    data = read_json_strict(TICKER_NOTES_FILE, {})
    return data if isinstance(data, dict) else {}


def save_ticker_notes(notes):
    from stock_data import atomic_write_json
    atomic_write_json(TICKER_NOTES_FILE, notes)


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


def apply_notes_to_rows(rows, notes=None, min_vstop_weeks=3, expert_views=None, fundamentals=None):
    """Attaches `note`, `flag` and `flag_reason` onto every row dict in place,
    from the shared ticker_notes.json (or an already-loaded `notes` dict, to
    avoid re-reading the file once per market).

    `flag` is the manual flag only ("" when none is set); `flag_reason` is
    MANUAL_FLAG_REASON or "". The automatic read is Expert Take (attached by
    stock_data.apply_view_fields_to_rows); this used to attach `signal` too,
    retired on 2026-10-04.

    `min_vstop_weeks`, `expert_views` and `fundamentals` are accepted for
    backward compatibility with existing call sites but no longer used."""
    if notes is None:
        notes = load_ticker_notes()

    for row in rows:
        ticker = row.get("ticker")
        row["note"] = get_ticker_note(notes, ticker)
        manual_flag = get_ticker_flag(notes, ticker)
        row["flag"] = manual_flag or NO_FLAG
        row["flag_reason"] = MANUAL_FLAG_REASON if manual_flag else ""
    return rows

