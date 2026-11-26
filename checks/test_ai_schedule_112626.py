"""Sentiment twice a week, Expert Take twice a week (owner, 2026-11-26).

Sentiment's FULL run is Tuesday and Friday 21:00 ET; Expert Take's is Wednesday and Saturday, also at 21:00 ET (the
evening after a Sentiment full run). The Worker still dispatches both slots every night; on any other night the job is a
TOP-UP that analyses only the stocks the last full run missed, so a stock Google's search model failed on is retried
the next night as before. A view may therefore be ~4 days old when the next full run replaces it: the stale limits
are 5 days (they were 4).

S1  the last full slot: Sentiment (Tue/Fri 21:00 ET) and Expert Take (Wed/Sat 21:00 ET), every weekday, both sides
    of a clock change -- expectations built from the ET calendar by hand, not by the code's own helper
S2  the longest gap between two full runs stays inside the stale limit with a margin for the run itself
S3  Sentiment's run on a full night works the whole list, on a top-up night only what the last full run missed
S4  the same for Expert Take
S5  the stale limits are 5 days and bite where they say (a view 4.5 days old is shown, one 5.5 days old is stale)
S6  nothing else moved: the gates' slots in the workflows and the Worker still dispatch every night
"""

import contextlib
import io
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import yaml

import refresh_expert_views as rev
import refresh_fundamentals as rf
import fundamentals_eval as fe
import expert_views as ev

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def captured(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*a, **kw)
    return out, buf.getvalue()


ET = ZoneInfo("America/New_York")
utc = lambda *a: datetime(*a, tzinfo=timezone.utc)

# --- S1 -----------------------------------------------------------------------------------------------
# 2026-11-23 is a Monday. EST from Sunday 2026-11-01; EDT before it. A slot is "the ET date's 21:00 / 01:00".
def et_slot(y, m, d, hour):
    return datetime(y, m, d, hour, 0, tzinfo=ET).astimezone(timezone.utc)


# Sentiment: slot date -> the last Tue/Fri 21:00 ET at or before it
SENT = {  # slot date -> date of the last full slot
    (2026, 11, 23): (2026, 11, 20),   # Mon -> Fri
    (2026, 11, 24): (2026, 11, 24),   # Tue is a full night
    (2026, 11, 25): (2026, 11, 24),   # Wed
    (2026, 11, 26): (2026, 11, 24),   # Thu
    (2026, 11, 27): (2026, 11, 27),   # Fri is a full night
    (2026, 11, 28): (2026, 11, 27),   # Sat
    (2026, 11, 29): (2026, 11, 27),   # Sun
    (2026, 11, 1): (2026, 10, 30),    # Sun, the day the clocks go back -> Fri 30 Oct (EDT)
    (2026, 11, 3): (2026, 11, 3),     # Tue 3 Nov (EST) is full
}
for day, last in SENT.items():
    slot = rf.slot_start_utc(et_slot(*day, 21) + timedelta(minutes=30))
    got = rf.last_full_slot_start_utc(slot)
    want = et_slot(*last, 21)
    check(slot == et_slot(*day, 21) and got == want,
          f"S1: Sentiment, slot of {day} -> last full slot {last} 21:00 ET ({got:%Y-%m-%d %H:%M} UTC)")
# a retry after midnight still belongs to the night before
retry = utc(2026, 11, 25, 6, 30)   # Wed 01:30 EST: the Tue 21:00 slot's retry
check(rf.last_full_slot_start_utc(rf.slot_start_utc(retry)) == et_slot(2026, 11, 24, 21),
      "S1: a 01:30 retry of Tuesday's slot is still Tuesday's full run")

EXP = {  # slot date (21:00 ET) -> last full (Wed/Sat) date
    (2026, 11, 23): (2026, 11, 21),   # Mon -> Sat
    (2026, 11, 24): (2026, 11, 21),   # Tue
    (2026, 11, 25): (2026, 11, 25),   # Wed is full
    (2026, 11, 26): (2026, 11, 25),   # Thu
    (2026, 11, 27): (2026, 11, 25),   # Fri
    (2026, 11, 28): (2026, 11, 28),   # Sat is full
    (2026, 11, 29): (2026, 11, 28),   # Sun
    (2026, 11, 1): (2026, 10, 31),    # Sun 1 Nov 21:00 EST (the clocks went back that morning) -> Sat 31 Oct 21:00 EDT
    (2026, 11, 4): (2026, 11, 4),     # Wed 4 Nov (EST)
}
for day, last in EXP.items():
    slot = rev.slot_start_utc(et_slot(*day, 21) + timedelta(minutes=30))
    got = rf.last_full_slot_start_utc(slot, rev.FULL_RUN_WEEKDAYS, rev.SLOT_HOUR_ET)
    want = et_slot(*last, 21)
    check(slot == et_slot(*day, 21) and got == want,
          f"S1: Expert Take, slot of {day} -> last full slot {last} 21:00 ET ({got:%Y-%m-%d %H:%M} UTC)")
check(set(rf.FULL_RUN_WEEKDAYS) == {1, 4} and set(rev.FULL_RUN_WEEKDAYS) == {2, 5},
      "S1: Sentiment's full weekdays are Tue + Fri, Expert Take's Wed + Sat")

# --- S2 -----------------------------------------------------------------------------------------------
def gaps(weekdays, hour, start=datetime(2026, 9, 1), weeks=26):
    slots = []
    d = start
    for _ in range(weeks * 7):
        if d.weekday() in weekdays:
            slots.append(datetime(d.year, d.month, d.day, hour, tzinfo=ET).astimezone(timezone.utc))
        d += timedelta(days=1)
    return [(b - a).total_seconds() / 3600 for a, b in zip(slots, slots[1:])]


for name, wk, hr, limit in (("Sentiment", rf.FULL_RUN_WEEKDAYS, rf.SLOT_HOUR_ET, fe.SENTIMENT_STALE_DAYS),
                            ("Expert Take", rev.FULL_RUN_WEEKDAYS, rev.SLOT_HOUR_ET, ev.EXPERT_STALE_DAYS)):
    g = gaps(wk, hr)
    check(max(g) + 12 < limit * 24,
          f"S2: {name}: the longest gap between full runs ({max(g):.0f} h, across both clock changes) plus 12 h for the run "
          f"is inside the {limit}-day stale limit")
    check(min(g) >= 71, f"S2: {name}: the shortest gap is ~3 days ({min(g):.0f} h), so it really is twice a week")

# --- shared fixtures for S3 / S4 ----------------------------------------------------------------------
WL = {"wl_a": ["A0", "A1"], "wl_b": ["B0", "B1"], "wl_c": ["C0"]}
SNAP = {"per_market": {m: [{"ticker": t, "company_name": t, "stage": "S2 Advancing"} for t in tks] for m, tks in WL.items()}}
ALL = sorted(t for tks in WL.values() for t in tks)
ENV_KEYS = ("REFRESH_MARKETS", "REFRESH_LIMIT", "REFRESH_PENDING_ONLY", "GITHUB_EVENT_NAME", "SCHEDULED_DISPATCH",
            "REFRESH_SENTIMENT_FIRST")


def set_env(env):
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update(env)


def stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


# last full run of Sentiment before the pinned nights: Tue 24 Nov 21:00 EST
SENT_FULL = et_slot(2026, 11, 24, 21)
sent_good = {"sentiment": "Positive", "reasoning": "r", "model_used": "m", "news_source": "g"}


def run_sentiment(slot_date, store):
    """Run refresh_fundamentals.main as a scheduled dispatch for the slot of `slot_date` (21:00 ET); returns (calls, text)."""
    set_env({"GITHUB_EVENT_NAME": "workflow_dispatch", "SCHEDULED_DISPATCH": "true"})
    calls = []

    def gen(client, row, is_retry=False):
        calls.append(row["ticker"])
        return {**sent_good, "as_of": stamp(datetime.now(timezone.utc))}

    rf.time.sleep = lambda s: None
    rf.get_gemini_api_key = lambda: "k"
    rf.llm_util.make_client = lambda key: object()
    rf.llm_util.log_key_usage = lambda client: None
    rf.load_data_snapshot = lambda: SNAP
    rf.load_watchlists = lambda: WL
    rf.load_fundamentals = lambda: store
    rf.save_fundamentals = lambda s: None
    rf.generate_fundamental_view = gen
    rf.slot_start_utc = lambda now=None: et_slot(*slot_date, 21)
    _, text = captured(rf.main)
    return sorted(calls), text


def sent_store():
    """After Tuesday's full run: A0, B0, B1 written right after it; A1 written before it; C0 a search-failed Unknown."""
    return {
        "A0": {**sent_good, "as_of": stamp(SENT_FULL + timedelta(hours=1))},
        "A1": {**sent_good, "as_of": stamp(SENT_FULL - timedelta(days=3))},
        "B0": {**sent_good, "as_of": stamp(SENT_FULL + timedelta(hours=2))},
        "B1": {**sent_good, "as_of": stamp(SENT_FULL + timedelta(hours=3))},
        "C0": {**sent_good, "sentiment": "Unknown", "news_source": "⚪ No Source", "as_of": stamp(SENT_FULL + timedelta(hours=1))},
    }


# --- S3 -----------------------------------------------------------------------------------------------
calls, text = run_sentiment((2026, 11, 26), sent_store())          # Thursday: a top-up night
check(calls == ["A1", "C0"] and "Top-up night" in text and "3 ticker(s) already done since the last full run" in text,
      f"S3: Sentiment on a Thursday retries only what Tuesday's full run missed ({calls})")
calls, text = run_sentiment((2026, 11, 25), sent_store())          # Wednesday
check(calls == ["A1", "C0"], f"S3: ...and on a Wednesday ({calls})")
calls, text = run_sentiment((2026, 11, 27), sent_store())          # Friday: full, works the whole list
check(calls == ALL and "Top-up" not in text, f"S3: Sentiment on a Friday works every stock ({calls})")
fri = sent_store()
fri_slot = et_slot(2026, 11, 27, 21)
for t in ("A0", "B1"):
    fri[t] = {**sent_good, "as_of": stamp(fri_slot + timedelta(hours=1))}
calls, text = run_sentiment((2026, 11, 27), fri)                   # a retry of Friday's full run resumes
check(calls == ["A1", "B0", "C0"] and "2 ticker(s) already done this slot" in text,
      f"S3: a retry of a full night skips what that slot already wrote ({calls})")
calls, _ = run_sentiment((2026, 11, 28), {t: {**sent_good, "as_of": stamp(fri_slot + timedelta(hours=1))} for t in ALL})
check(calls == [], f"S3: a top-up night with nothing missed analyses nothing ({calls})")
calls, _ = run_sentiment((2026, 11, 28), {})
check(calls == ALL, f"S3: with no views at all a top-up night works every stock, as a first run must ({calls})")

# a manual dispatch still redoes everything, on any night
set_env({"GITHUB_EVENT_NAME": "workflow_dispatch"})
calls = []
rf.generate_fundamental_view = lambda client, row, is_retry=False: calls.append(row["ticker"]) or {
    **sent_good, "as_of": stamp(datetime.now(timezone.utc))}
rf.slot_start_utc = lambda now=None: et_slot(2026, 11, 26, 21)
rf.load_fundamentals = lambda: sent_store()
captured(rf.main)
check(sorted(calls) == ALL, f"S3: a dispatch by hand redoes everything on a top-up night ({sorted(calls)})")

# --- S4 -----------------------------------------------------------------------------------------------
EXP_FULL = et_slot(2026, 11, 25, 21)         # Wed 25 Nov 21:00 EST
exp_good = {"verdict": "HOLD", "headline": "h", "model_used": "m"}


def exp_store():
    return {
        "A0": {**exp_good, "as_of": stamp(EXP_FULL + timedelta(hours=1))},
        "A1": {"verdict": "PENDING", "headline": "Analysis pending", "as_of": stamp(EXP_FULL + timedelta(hours=1))},  # failed
        "B0": {**exp_good, "as_of": stamp(EXP_FULL + timedelta(hours=2))},
        "B1": {**exp_good, "as_of": stamp(EXP_FULL - timedelta(days=2))},   # written before the full run
        "C0": {**exp_good, "as_of": stamp(EXP_FULL + timedelta(hours=3))},
    }


def run_expert(slot_date, store):
    set_env({"GITHUB_EVENT_NAME": "workflow_dispatch", "SCHEDULED_DISPATCH": "true"})
    calls = []
    rev.time.sleep = lambda s: None
    rev.get_gemini_api_key = lambda: "k"
    rev.llm_util.make_client = lambda key: object()
    rev.llm_util.log_key_usage = lambda client: None
    rev.load_data_snapshot = lambda: SNAP
    rev.load_watchlists = lambda: WL
    rev.load_expert_views = lambda: store
    rev.save_expert_views = lambda s: None
    rev.load_fundamentals = lambda: {}
    rev.active_alerts_for_prompt = lambda rows: None
    rev.alerts_text_for = lambda a, t: ""
    rev.generate_expert_view = lambda client, row, **kw: calls.append(row["ticker"]) or {
        **exp_good, "as_of": stamp(datetime.now(timezone.utc))}
    rev.slot_start_utc = lambda now=None: et_slot(*slot_date, 21)
    _, text = captured(rev.main)
    return sorted(calls), text


calls, text = run_expert((2026, 11, 27), exp_store())                # Friday 21:00: a top-up night
check(calls == ["A1", "B1"] and "Top-up night" in text and "3 ticker(s) already done since the last full run" in text,
      f"S4: Expert Take on a Friday retries only what Wednesday's full run missed ({calls})")
calls, text = run_expert((2026, 11, 28), exp_store())                # Saturday: a full night
check(calls == ALL and "Top-up" not in text, f"S4: Expert Take on a Saturday works every stock ({calls})")
sat = exp_store()
sat_slot = et_slot(2026, 11, 28, 21)
for t in ("A0", "C0"):
    sat[t] = {**exp_good, "as_of": stamp(sat_slot + timedelta(hours=1))}
calls, text = run_expert((2026, 11, 28), sat)
check(calls == ["A1", "B0", "B1"] and "2 ticker(s) already done this slot" in text,
      f"S4: a retry of a full night skips what that slot already wrote ({calls})")
calls, _ = run_expert((2026, 11, 29), {})
check(calls == ALL, f"S4: with no views at all a top-up night works every stock ({calls})")

# --- S5 -----------------------------------------------------------------------------------------------
check(fe.SENTIMENT_STALE_DAYS == 5 and ev.EXPERT_STALE_DAYS == 5, "S5: both stale limits are 5 days")
now = datetime.now(timezone.utc)
full_view = {"sentiment": "Positive", "earnings_summary": "e", "future_guidance": "g", "analyst_coverage": "a",
             "quarter_verified": True, "model_used": "m", "reasoning": "r"}
aged = lambda days: {**full_view, "as_of": stamp(now - timedelta(days=days))}
check(fe._validate_sentiment(aged(4.5))[1] != "STALE", "S5: a Sentiment view 4.5 days old is still shown")
check(fe._validate_sentiment(aged(5.5)) == ("Unknown", "STALE"), "S5: one 5.5 days old is stale")
check(fe._prior_worth_keeping(aged(4.5)) and not fe._prior_worth_keeping(aged(5.5)),
      "S5: a failed night keeps a prior up to 5 days old and no older")
prior = {**exp_good, "as_of": stamp(now - timedelta(days=4.5))}
check(ev.resolve_persisted_view({"verdict": "PENDING", "headline": "Analysis pending"}, prior) is None,
      "S5: a failed Expert Take regeneration keeps a 4.5-day-old write-up")
old_prior = {**exp_good, "as_of": stamp(now - timedelta(days=5.5))}
check(ev.resolve_persisted_view({"verdict": "PENDING", "headline": "Analysis pending"}, old_prior) not in (None, old_prior),
      "S5: ...and replaces a 5.5-day-old one with the honest placeholder")

# --- S6 -----------------------------------------------------------------------------------------------
fwf = yaml.safe_load((REPO / ".github/workflows/fundamentals.yml").read_text())
ewf = yaml.safe_load((REPO / ".github/workflows/expert-views.yml").read_text())
gate = lambda wf: next(s for s in wf["jobs"]["gate"]["steps"] if "slot-gate" in str(s.get("uses", "")))["with"]
check(gate(fwf).get("slots") == str(rf.SLOT_HOUR_ET) and "days" not in gate(fwf),
      "S6: fundamentals.yml's gate is still the nightly 21:00 slot (the code decides full run or top-up)")
check(gate(ewf).get("slots") == str(rev.SLOT_HOUR_ET) and "days" not in gate(ewf),
      "S6: expert-views.yml's gate is the nightly 21:00 slot")
worker = (REPO / "cloudflare/worker.js").read_text()
check(re.search(r'workflow: "fundamentals.yml", slots: \[\[21, 0\]\] \}', worker) is not None
      and re.search(r'workflow: "expert-views.yml", slots: \[\[21, 0\]\] \}', worker) is not None,
      "S6: the Worker still dispatches both slots every night (no Cloudflare change needed)")

print("FAILURES:", fails)
sys.exit(fails)
