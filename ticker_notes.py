"""
Per-ticker free-text notes and a colored "flag" marker -- lets you jot down
a reminder ("earnings 8/14, watch guidance") or bookmark a ticker with a
color (Red/Yellow/Green/Blue) to keep track of things at a glance, without
that being a computed metric like custom_columns.py's formulas.

Stored in ticker_notes.json as {ticker: {"note": str, "flag": str}}, keyed
by the SAME ticker symbol used everywhere else in the app (e.g. "AAPL",
"TCS.NS") -- global across both markets, not per-market, since a
ticker symbol is already unique across the whole watchlist.

Flag is ONLY what you set by hand. The automatic read is a separate field,
`signal` (Chart x News, see compute_signal), which replaced a 4-signal flag
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
SIGNAL_OUTCOMES = ("Confirmed", "Chart only", "Mixed", "News divergence", "Chart up, news negative", "Avoid")
# The dot on the ticker when no manual flag is set. There is no light-green
# emoji, so "Chart only" is the white dot; the Signal column itself uses real
# shades (app.SIGNAL_COLORS).
SIGNAL_EMOJI = {"Confirmed": "🟢", "Chart only": "⚪", "Mixed": "🟣", "News divergence": "🟡",
                "Chart up, news negative": "🟠", "Avoid": "🔴"}


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


def compute_signal(trend, sentiment, tag=""):
    """Signal = Chart x News. Returns (label, reason), or ("", reason) when
    Trend is not computed yet (too little weekly history).

      Chart up (Trend Uptrend / Strong Uptrend) + news Positive  -> Confirmed
      Chart up + news Neutral or Unknown                         -> Chart only
      Chart up + news Negative                                   -> Chart up, news negative
      Chart Mixed + news Neutral or Unknown                      -> Mixed
      Chart not up (Mixed or down) + news Positive               -> News divergence
      Chart not up (Mixed or down) + news Negative               -> Avoid
      Chart down + news Neutral or Unknown                       -> Avoid

    "Mixed" is Trend's own split state (stock_data.compute_trend): the four
    trend conditions disagree. It used to be folded into Downtrend, which made
    a stock above a rising 40W EMA that merely lagged its index read "Avoid".

    `sentiment` must already be the GUARDED value (fundamentals_eval.
    _validate_sentiment): a stale or evidence-less view reads Unknown or
    Neutral, never a directional label. `tag` is the cell's guidance/outlook
    tag, carried into the reason only.

    WHY this replaced the flag vote: three of its four voters were the chart
    said three ways (Expert Take agreed with Trend 93% and with Tech Uptrend
    95% on 2026-09-26), so it counted the chart three times and the news once,
    painted 56% of tickers Green, and folded three unrelated situations into
    Yellow behind asymmetric vetoes. Two inputs, no vetoes, every ticker gets a
    label. Expert Take and Tech Uptrend stay columns to read, not votes."""
    news = f"{sentiment}" + (f" ({tag})" if tag else "")
    if not trend:
        return "", f"Chart: no Trend yet · News: {news}"
    up = trend in ("Uptrend", "Strong Uptrend")
    if up:
        label = {"Positive": "Confirmed", "Negative": "Chart up, news negative"}.get(sentiment, "Chart only")
    elif sentiment == "Positive":
        label = "News divergence"
    elif trend == "Mixed" and sentiment != "Negative":
        label = "Mixed"
    else:
        label = "Avoid"
    return label, f"Chart: {trend} · News: {news}"


def apply_notes_to_rows(rows, notes=None, min_vstop_weeks=3, expert_views=None, fundamentals=None):
    """Attaches `note`, `flag`, `flag_reason`, `signal` and `signal_reason`
    onto every row dict in place, from the shared ticker_notes.json (or an
    already-loaded `notes` dict, to avoid re-reading the file once per market).

    `flag` is the manual flag only ("" when none is set); `flag_reason` is
    MANUAL_FLAG_REASON or "". `signal` is compute_signal(Trend, guarded
    Sentiment) -- see there for why it replaced the automatic flag vote.

    `min_vstop_weeks` and `expert_views` are accepted for backward
    compatibility with existing call sites but no longer used (they fed the
    retired vote)."""
    if notes is None:
        notes = load_ticker_notes()

    from fundamentals_eval import load_fundamentals, _validate_sentiment, evidence_tag
    # Accepted pre-loaded for the same reason as `notes`: app.py calls this once
    # per watchlist per render and already holds the file, so re-reading it
    # here cost redundant parses of ~270 KB on every interaction.
    fundamentals = load_fundamentals() if fundamentals is None else fundamentals

    for row in rows:
        ticker = row.get("ticker")
        row["note"] = get_ticker_note(notes, ticker)
        manual_flag = get_ticker_flag(notes, ticker)
        row["flag"] = manual_flag or NO_FLAG
        row["flag_reason"] = MANUAL_FLAG_REASON if manual_flag else ""
        fund_view = fundamentals.get(ticker)
        sentiment, guard = _validate_sentiment(fund_view) if fund_view else ("Unknown", "NO_DATA")
        tag = "" if guard in ("STALE", "STALE_QUARTER", "NO_DATA") else evidence_tag(fund_view)
        row["signal"], row["signal_reason"] = compute_signal(row.get("trend"), sentiment, tag)
    return rows

