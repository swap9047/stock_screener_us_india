"""Standalone logic simulation of the fundamentals refresh decision paths.

No API calls, no google.genai import needed. Verifies the behavior of
_apply_result / _validate_sentiment / _view_age_days against the scenarios
from the review. Mirrors the real functions (they live in modules that
import google.genai at top level, which is not installed locally).

_fetch_last_reported_earnings_date does real yfinance I/O and is NOT mirrored
here; _check_quarter_freshness (the pure comparison it feeds) is, so the
STALE_QUARTER decision logic is still covered without network access.
"""
import sys, json
from datetime import datetime, timedelta, timezone
sys.path.insert(0, ".")

# ---- Mirrors of the edited functions (kept byte-for-byte identical) ----
SENTIMENT_STALE_DAYS = 4
SEARCH_WINDOW_DAYS = {"INDIA": 45, "US": 25}
VALID = ("Positive", "Neutral", "Negative", "Unknown")

def _field_has_data(field_val):
    val = (field_val or "").strip()
    if not val:
        return False
    low = val.lower()
    if low in ("n/a", "none", "nil", "na"):
        return False
    return not any(m in low for m in ("n/a", "not available", "no news", "no data"))

def _structured_field_is_set(val):
    val = (val or "").strip().lower()
    return bool(val) and val not in ("none", "n/a", "na", "null")

def _field_is_placeholder(field_val):
    v = (field_val or "").strip().lower()
    return v in ("", "n/a", "na", "none", "nil", "not available", "no data", "no news")

def _has_hard_evidence(view):
    if any(k in view for k in ("eps_value", "guidance_change", "analyst_action")):
        return (
            _structured_field_is_set(view.get("eps_value"))
            or _structured_field_is_set(view.get("guidance_change"))
            or _structured_field_is_set(view.get("analyst_action"))
        )
    if _field_has_data(view.get("future_guidance")) or _field_has_data(view.get("analyst_coverage")):
        return True
    earnings = (view.get("earnings_summary") or "").lower()
    if earnings and "eps" in earnings and "eps n/a" not in earnings and "eps not" not in earnings:
        return True
    return False

def _check_quarter_freshness(model_earnings_date, real_last_earnings_date, market):
    if real_last_earnings_date is None:
        return True
    window = SEARCH_WINDOW_DAYS.get(market, SEARCH_WINDOW_DAYS["US"])
    today = datetime.now(timezone.utc).date()
    if (today - real_last_earnings_date).days > window:
        return True
    if not model_earnings_date:
        return False
    try:
        model_date = datetime.strptime(model_earnings_date, "%Y-%m-%d").date()
    except Exception:
        return False
    return abs((model_date - real_last_earnings_date).days) <= 10

def _is_valid_view(view):
    if not view:
        return False
    sentiment = view.get("sentiment")
    if sentiment not in VALID:
        return False
    if "error" in str(view).lower() or "pending" in str(view).lower():
        return False
    return True

def _view_age_days(view):
    as_of = (view or {}).get("as_of")
    if not as_of:
        return None
    try:
        ts = datetime.fromisoformat(as_of.replace(" ", "T", 1))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400
    except Exception:
        return None

def _validate_sentiment(view):
    if not view:
        return "Unknown", "NO_DATA"
    age = _view_age_days(view)
    if age is not None and age > SENTIMENT_STALE_DAYS:
        return "Unknown", "STALE"
    if view.get("quarter_verified") is False:
        return "Unknown", "STALE_QUARTER"
    fields = ["earnings_summary", "future_guidance", "analyst_coverage"]
    if not any(not _field_is_placeholder(view.get(f)) for f in fields):
        return "Unknown", "NO_DATA"
    sentiment = view.get("sentiment", "Unknown")
    if sentiment in ("Positive", "Negative") and not _has_hard_evidence(view):
        return "Neutral", "PARTIAL"
    return sentiment, ""

def _unknown_fallback(reason):
    return {
        "earnings_summary": "N/A", "future_guidance": "N/A", "analyst_coverage": "N/A",
        "sentiment": "Unknown", "reasoning": f"Analysis unavailable -- {reason}",
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "news_source": "⚪ No Source", "model_used": "Error",
    }

def _apply_result(fundamentals, tk, view, old_view, elapsed):
    if _is_valid_view(view):
        sentiment, flag = _validate_sentiment(view)
        if flag:
            view["sentiment"] = "Neutral" if flag == "PARTIAL" else "Unknown"
            view["reasoning"] = f"{view.get('reasoning', '')}\n[auto-downgraded: {flag}]"
        fundamentals[tk] = view
        return 0, f"OK ({elapsed:.1f}s) sentiment={view.get('sentiment')}"
    if _is_valid_view(old_view):
        age = _view_age_days(old_view)
        if age is not None and age > SENTIMENT_STALE_DAYS:
            fundamentals[tk] = _unknown_fallback(f"previous view is {age:.1f} days old")
            return 1, f"FAILED ({elapsed:.1f}s); prior stale ({age:.1f}d) -> wrote Unknown"
        return 0, f"FAILED ({elapsed:.1f}s), keeping prior result"
    reason = str(view.get("reasoning", view.get("sentiment", "no valid result")))[:160]
    fundamentals[tk] = _unknown_fallback(reason)
    return 1, f"FAILED ({elapsed:.1f}s): {view.get('sentiment')} -> wrote Unknown"
# ---------------------------------------------------------------------

NOW = datetime.now(timezone.utc)
def ts(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M")

results = []
def check(name, cond):
    results.append((name, cond))

# S1: Model correctly returns Unknown on no-news (the fix's own output) -> must persist
view = {"earnings_summary": "N/A", "future_guidance": "N/A", "analyst_coverage": "N/A",
        "sentiment": "Unknown", "reasoning": "No recent fundamental news found.",
        "as_of": ts(0), "news_source": "🔍 Gemma-4-26B (Google Search)", "model_used": "gemini-3.5-flash-lite"}
old = {"sentiment": "Positive", "earnings_summary": "EPS N/A", "as_of": ts(1)}  # hallucinated old entry
f = {}; inc, detail = _apply_result(f, "T1", dict(view), old, 42.0)
check("S1 Unknown accepted (not treated as failure)", _is_valid_view(view) and f["T1"]["sentiment"] == "Unknown")
check("S1 old hallucinated Positive overwritten", f["T1"]["sentiment"] == "Unknown" and inc == 0)

# S2: Model hallucinates Positive with ALL fields N/A -> deterministic downgrade
view = {"earnings_summary": "N/A", "future_guidance": "N/A", "analyst_coverage": "N/A",
        "sentiment": "Positive", "reasoning": "Revenue momentum looks strong.", "as_of": ts(0)}
f = {}; inc, _ = _apply_result(f, "T2", dict(view), None, 5.0)
check("S2 NO_DATA downgrade to Unknown", f["T2"]["sentiment"] == "Unknown")
check("S2 downgrade annotated", "auto-downgraded: NO_DATA" in f["T2"]["reasoning"])

# S3: AARTIPHARM pattern - revenue-only, EPS+guidance+analyst N/A, model says Positive -> capped at Neutral
view = {"earnings_summary": "Revenue Rs 582.64 cr (+34.79% QoQ), EPS N/A",
        "future_guidance": "N/A", "analyst_coverage": "N/A",
        "sentiment": "Positive", "reasoning": "Revenue growth is strong.", "as_of": ts(0)}
f = {}; inc, _ = _apply_result(f, "AARTIPHARM.NS", dict(view), None, 5.0)
check("S3 AARTIPHARM revenue-only Positive capped at Neutral", f["AARTIPHARM.NS"]["sentiment"] == "Neutral")
check("S3 downgrade annotated PARTIAL", "auto-downgraded: PARTIAL" in f["AARTIPHARM.NS"]["reasoning"])

# S3b: Same revenue-only data but model already says Neutral -> unchanged
view = {"earnings_summary": "Revenue Rs 582.64 cr (+34.79% QoQ), EPS N/A",
        "future_guidance": "N/A", "analyst_coverage": "N/A",
        "sentiment": "Neutral", "reasoning": "Revenue strong but no EPS.", "as_of": ts(0)}
f = {}; inc, _ = _apply_result(f, "T3B", dict(view), None, 5.0)
check("S3b revenue-only Neutral kept", f["T3B"]["sentiment"] == "Neutral" and inc == 0)

# S3c: Revenue + real EPS -> Positive allowed
view = {"earnings_summary": "Q1 EPS Rs 12.4 (beat), Revenue Rs 582 cr (+34.79% QoQ)",
        "future_guidance": "N/A", "analyst_coverage": "N/A",
        "sentiment": "Positive", "reasoning": "EPS beat.", "as_of": ts(0)}
f = {}; inc, _ = _apply_result(f, "T3C", dict(view), None, 5.0)
check("S3c EPS present -> Positive kept", f["T3C"]["sentiment"] == "Positive" and inc == 0)

# S3d: Revenue-only Negative -> capped at Neutral too
view = {"earnings_summary": "Revenue fell 5% YoY, EPS N/A",
        "future_guidance": "N/A", "analyst_coverage": "N/A",
        "sentiment": "Negative", "reasoning": "Revenue down.", "as_of": ts(0)}
f = {}; inc, _ = _apply_result(f, "T3D", dict(view), None, 5.0)
check("S3d revenue-only Negative capped at Neutral", f["T3D"]["sentiment"] == "Neutral")

# S4: Real data present, fresh -> kept as-is
view = {"earnings_summary": "Q2 EPS beat by $0.05, Revenue $1.2B (+10% YoY)",
        "future_guidance": "Raised full-year EPS guidance", "analyst_coverage": "Upgraded by MS to Overweight",
        "sentiment": "Positive", "reasoning": "EPS beat + guidance raise + upgrade.", "as_of": ts(0)}
f = {}; inc, _ = _apply_result(f, "T4", dict(view), None, 5.0)
check("S4 real data kept Positive", f["T4"]["sentiment"] == "Positive" and inc == 0)

# S5: Neutral with real data kept
view = {"earnings_summary": "EPS in line", "future_guidance": "In-line guidance", "analyst_coverage": "N/A",
        "sentiment": "Neutral", "reasoning": "In line with expectations.", "as_of": ts(0)}
f = {}; inc, _ = _apply_result(f, "T5", dict(view), None, 5.0)
check("S5 Neutral kept", f["T5"]["sentiment"] == "Neutral")

# S6: Generation fails (invalid dict), fresh valid prior -> keep prior
new_bad = {"sentiment": "garbage", "reasoning": "x"}
old = {"sentiment": "Negative", "earnings_summary": "EPS miss", "as_of": ts(1)}
f = {"T6": dict(old)}; inc, detail = _apply_result(f, "T6", dict(new_bad), old, 3.0)
check("S6 fresh prior kept on failure", f["T6"]["sentiment"] == "Negative" and inc == 0)

# S7: Generation fails, stale valid prior (7 days) -> must NOT keep forever
old = {"sentiment": "Positive", "earnings_summary": "EPS beat", "as_of": ts(7)}
f = {"T7": dict(old)}; inc, detail = _apply_result(f, "T7", dict(new_bad), old, 3.0)
check("S7 stale prior overwritten with Unknown", f["T7"]["sentiment"] == "Unknown" and inc == 1)
check("S7 fallback is full schema", all(k in f["T7"] for k in ("earnings_summary", "future_guidance", "analyst_coverage", "reasoning", "as_of")))

# S8: Error path equivalent - partial dict no longer written
check("S8 fallback has all schema keys + model_used Error", f["T7"]["model_used"] == "Error")

# S9: _view_age_days parsing of "YYYY-MM-DD HH:MM" (space separator, Python 3.9-safe)
check("S9 age parse ok", abs(_view_age_days({"as_of": ts(2)}) - 2) < 0.01)
check("S9 missing as_of -> None", _view_age_days({}) is None)
check("S9 garbage as_of -> None", _view_age_days({"as_of": "not-a-date"}) is None)

# S10: app-side cell badge logic (mirror of _fundamentals_cell flag branch)
_, flag = _validate_sentiment({"as_of": ts(7), "earnings_summary": "EPS", "future_guidance": "x", "analyst_coverage": "y"})
check("S10 STALE flag detected", flag == "STALE")
_, flag = _validate_sentiment({})
check("S10 empty dict -> NO_DATA", flag == "NO_DATA")
_, flag = _validate_sentiment({"as_of": ts(0), "earnings_summary": "N/A", "future_guidance": "N/A", "analyst_coverage": "N/A"})
check("S10 all-N/A -> NO_DATA", flag == "NO_DATA")
# S11: schema-example noise in guidance ("(or 'N/A if no news')") is not hard evidence
_, flag = _validate_sentiment({"as_of": ts(0), "earnings_summary": "Revenue +10% YoY, EPS N/A",
                               "future_guidance": "Maintained guidance (or 'N/A if no news')",
                               "analyst_coverage": "N/A", "sentiment": "Positive"})
check("S11 schema-example guidance not evidence -> PARTIAL", flag == "PARTIAL")

# S12: the "EPS: N/A (pending)" bypass the review found -- LEGACY free-text
# path (no structured keys present) still has the substring-match gap, since
# it exists only to support pre-migration cached views. This documents the
# known limitation rather than a regression.
view = {"earnings_summary": "EPS: N/A (pending)", "future_guidance": "N/A", "analyst_coverage": "N/A"}
check("S12 legacy free-text still bypassable (documents pre-migration gap)", _has_hard_evidence(view))

# S13: same vague text, but with the NEW structured fields present and explicitly
# null -- this is what a real (non-legacy) response looks like, and it must NOT
# be treated as hard evidence. This is the actual fix for S12's bypass.
view = {"earnings_summary": "EPS: N/A (pending)", "future_guidance": "N/A", "analyst_coverage": "N/A",
        "eps_value": None, "guidance_change": None, "analyst_action": None,
        "sentiment": "Positive", "as_of": ts(0)}
_, flag = _validate_sentiment(view)
check("S13 structured null fields close the bypass -> PARTIAL", flag == "PARTIAL")

# S14: structured field with a real value -> hard evidence recognized, Positive kept
view = {"earnings_summary": "Q1 EPS beat", "future_guidance": "N/A", "analyst_coverage": "N/A",
        "eps_value": "$2.02 actual vs $1.89 est.", "guidance_change": None, "analyst_action": None,
        "sentiment": "Positive", "as_of": ts(0)}
_, flag = _validate_sentiment(view)
check("S14 structured eps_value recognized as hard evidence -> kept Positive", flag == "")

# S15: _check_quarter_freshness -- no real date available -> fail open (can't verify)
check("S15 no real earnings date -> fail open (True)", _check_quarter_freshness(None, None, "INDIA") is True)

# S16: real earnings date outside the search window -> nothing new expected, pass
old_real_date = (NOW - timedelta(days=100)).date()
check("S16 real date outside window -> True", _check_quarter_freshness(None, old_real_date, "INDIA") is True)

# S17: real earnings date inside window, model cited no date -> should have found it, fail
recent_real_date = (NOW - timedelta(days=10)).date()
check("S17 recent real report, model cited nothing -> False", _check_quarter_freshness(None, recent_real_date, "INDIA") is False)

# S18: real earnings date inside window, model date matches within tolerance -> pass
matching_model_date = (NOW - timedelta(days=8)).strftime("%Y-%m-%d")
check("S18 model date matches real date -> True", _check_quarter_freshness(matching_model_date, recent_real_date, "INDIA") is True)

# S19: real earnings date inside window, model cited a stale/unrelated date -> fail
stale_model_date = (NOW - timedelta(days=90)).strftime("%Y-%m-%d")
check("S19 model date doesn't match recent real report -> False", _check_quarter_freshness(stale_model_date, recent_real_date, "INDIA") is False)

# S20: end-to-end via _validate_sentiment -- quarter_verified False forces Unknown
# even when the view otherwise has strong structured evidence (the deterministic
# earnings-date check overrides everything else, same precedence as STALE).
view = {"earnings_summary": "Q1 EPS beat", "future_guidance": "N/A", "analyst_coverage": "N/A",
        "eps_value": "$2.02 beat", "guidance_change": None, "analyst_action": None,
        "sentiment": "Positive", "as_of": ts(0), "quarter_verified": False}
sentiment, flag = _validate_sentiment(view)
check("S20 quarter_verified False forces Unknown/STALE_QUARTER", sentiment == "Unknown" and flag == "STALE_QUARTER")

# S21: same, via _apply_result end-to-end (the actual refresh code path)
f = {}; inc, detail = _apply_result(f, "T21", dict(view), None, 5.0)
check("S21 STALE_QUARTER downgrade applied through _apply_result", f["T21"]["sentiment"] == "Unknown")
check("S21 downgrade annotated STALE_QUARTER", "auto-downgraded: STALE_QUARTER" in f["T21"]["reasoning"])

failed = [n for n, ok in results if not ok]
for n, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
