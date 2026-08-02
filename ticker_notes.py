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


def compute_auto_flag(row, expert_verdict=None, sentiment=None):
    """Auto-assigns a Green/Red/Yellow flag via a 4-signal majority vote:
    Expert Take, Trend, Tech Uptrend, and Sentiment. Returns (flag_color,
    reason_string), or ("", "") if neither threshold below is met.

    Each signal casts a vote for at most one side (never both) -- Green
    requires >=3 of 4 bullish votes, Red requires >=3 of 4 bearish votes.
    Missing/unclear data (Trend not yet computed, Sentiment "Unknown" or
    "Neutral", Expert Take not yet generated) simply abstains rather than
    shrinking the pass threshold below 3. Tech Uptrend is the one exception:
    it's a plain boolean (never "unknown" by construction in stock_data.py),
    so it always casts a vote one way or the other.

    Vote definitions (deliberately asymmetric, not a copy-paste mirror):
      GREEN vote: Expert Take in (ACCUMULATE, HOLD) -- HOLD is the model's
        stated "mixed/low-confidence" default, not a bearish read, so it
        counts here but has no bearish counterpart below.
      RED vote:   Expert Take == CAUTION specifically.
      GREEN vote: Trend in (Uptrend, Strong Uptrend).
      RED vote:   Trend in (Downtrend, Strong Downtrend).
      GREEN vote: Tech Uptrend is True.
      RED vote:   Tech Uptrend is False.
      GREEN vote: Sentiment == "Positive".
      RED vote:   Sentiment == "Negative".

    Veto layer: even when a threshold is met, a specific contradicting
    signal downgrades the result to "Yellow" (further study) instead --
    reuses the existing manual Red/Yellow/Green/Blue flag palette rather
    than inventing a 5th color, since "further study" is exactly what
    Yellow already means there. The veto sets are intentionally NOT
    symmetric: Green has 2 veto conditions, Red has 4 -- Red is harder to
    trigger than Green (any single strong contradiction blocks it, while
    Green tolerates Trend/Tech Uptrend disagreeing), consistent with the
    HOLD/CAUTION asymmetry above.
      GREEN is vetoed by: Expert Take == CAUTION, or Sentiment == Negative.
      RED is vetoed by: Expert Take == ACCUMULATE, or Tech Uptrend == True,
        or Trend == "Strong Uptrend" specifically (plain "Uptrend" does NOT
        veto Red), or Sentiment == Positive.

    The reason string always states the vote tally and contributing
    signals, and additionally names the veto(s) when downgraded to Yellow --
    so the hover tooltip explains both the vote AND the veto, not just the
    final color."""
    trend = row.get("trend")
    tech_uptrend = bool(row.get("tech_uptrend"))
    green_hits, red_hits = [], []

    if expert_verdict in ("ACCUMULATE", "HOLD"):
        green_hits.append(f"Expert Take={expert_verdict.title()}")
    if expert_verdict == "CAUTION":
        red_hits.append("Expert Take=Caution")

    if trend in ("Uptrend", "Strong Uptrend"):
        green_hits.append(f"Trend={trend}")
    if trend in ("Downtrend", "Strong Downtrend"):
        red_hits.append(f"Trend={trend}")

    (green_hits if tech_uptrend else red_hits).append(f"Tech Uptrend={'Yes' if tech_uptrend else 'No'}")

    if sentiment == "Positive":
        green_hits.append("Sentiment=Bullish")
    if sentiment == "Negative":
        red_hits.append("Sentiment=Bearish")

    if len(green_hits) >= 3:
        vetoes = []
        if expert_verdict == "CAUTION":
            vetoes.append("Expert Take=Caution")
        if sentiment == "Negative":
            vetoes.append("Sentiment=Bearish")
        vote_desc = f"{len(green_hits)}/4 bullish votes ({', '.join(green_hits)})"
        if vetoes:
            return "Yellow", f"{vote_desc} -- further study: vetoed by {', '.join(vetoes)}"
        return "Green", vote_desc

    if len(red_hits) >= 3:
        vetoes = []
        if expert_verdict == "ACCUMULATE":
            vetoes.append("Expert Take=Accumulate")
        if tech_uptrend:
            vetoes.append("Tech Uptrend=Yes")
        if trend == "Strong Uptrend":
            vetoes.append("Trend=Strong Uptrend")
        if sentiment == "Positive":
            vetoes.append("Sentiment=Bullish")
        vote_desc = f"{len(red_hits)}/4 bearish votes ({', '.join(red_hits)})"
        if vetoes:
            return "Yellow", f"{vote_desc} -- further study: vetoed by {', '.join(vetoes)}"
        return "Red", vote_desc

    return "", ""


def apply_notes_to_rows(rows, notes=None, min_vstop_weeks=3):
    """Attaches `note`, `flag`, and `flag_reason` fields onto every row dict
    in place, from the shared ticker_notes.json (or an already-loaded `notes`
    dict, to avoid re-reading the file once per market).

    `min_vstop_weeks` is accepted for backward compatibility with existing
    call sites but no longer used -- compute_auto_flag()'s vote-based rules
    don't reference VStop duration at all.

    Expert Take verdict and Sentiment (needed by compute_auto_flag()'s vote
    but not present on the row itself, since those live in
    expert_views.json/fundamentals.json rather than the technical snapshot)
    are loaded once per call here, not once per row, same reasoning as the
    `notes` load above.

    Flag priority:
      1. Manual flag from ticker_notes.json -- never overridden.
      2. Auto-computed flag from compute_auto_flag() -- applied only when
         no manual flag is set.
      3. No flag -- neutral.

    `flag_reason` is set on every row:
      - "Manually assigned" if a manual flag is present.
      - The vote tally (and veto, if any) for auto-flags -- see
        compute_auto_flag()'s docstring.
      - "" if no flag at all."""
    if notes is None:
        notes = load_ticker_notes()

    from expert_views import load_expert_views
    from fundamentals_eval import load_fundamentals, _validate_sentiment
    expert_views = load_expert_views()
    fundamentals = load_fundamentals()

    for row in rows:
        ticker = row.get("ticker")
        row["note"] = get_ticker_note(notes, ticker)
        manual_flag = get_ticker_flag(notes, ticker)
        if manual_flag:
            row["flag"] = manual_flag
            row["flag_reason"] = "Manually assigned"
        else:
            verdict = expert_views.get(ticker, {}).get("verdict")
            fund_view = fundamentals.get(ticker)
            sentiment = _validate_sentiment(fund_view)[0] if fund_view else "Unknown"
            auto_flag, auto_reason = compute_auto_flag(row, expert_verdict=verdict, sentiment=sentiment)
            row["flag"] = auto_flag
            row["flag_reason"] = auto_reason
    return rows

