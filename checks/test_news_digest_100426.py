"""News digest fixes from the 2026-10-04 review.

D1 The editing stage (Stage 3) failed in 4 of 18 digests on 503 "high demand",
   giving up after ~20 s, and the raw notes went to Discord. Now: wait 30 s
   between passes, end on the filter model, and if every model fails, format
   the bullets in code -- the digest never goes out unformatted.
D2 That failure was invisible: the News tab showed search/filter failures only.
D3 Company names were lost ("ACME (ACME)"): Stage 2 was told to write "bold
   ticker headers". It writes bullets only now; the code adds the header.
D4 ~30% of bullets were filler: price moves with no reason, conferences, law-firm
   class-action ads, rating-site "analysts".
D5 ETFs produced gold/macro commentary, not company news: they are skipped.
D6 The digest was dated by GitHub's start time (anywhere 8 PM-3 AM ET), so some
   dates appeared twice and others never. It is dated by its 8 PM ET slot.
D7 1-4 bullets a night repeated the previous digest: Stage 2 now sees what the
   previous digest said about the ticker and drops repeats.

Offline: invented tickers, stubbed models, no network, no data files.
"""

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import llm_util
import news_summary as ns
import stock_data as sd

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def fn(name):
    f = getattr(ns, name, None)
    if f is None:
        check(False, f"news_summary.{name} exists")
    return f


ET = ZoneInfo("America/New_York")
NAMES = {"ACME": "Acme Corp", "ZED.NS": "Zed Industries Limited", "FUNDX": "Example Gold Trust"}

# --- D1 the editing ladder, and the code-formatted fallback -------------------------
captured = {}
_real_ladder = llm_util.run_model_ladder


def ladder_fail(client, prompt, tiers, config_for, label="", subject="", timeout=None, on_success=None):
    captured.setdefault(label, []).append(list(tiers))
    return None, None


llm_util.run_model_ladder = ladder_fail
try:
    notes = ["**Acme Corp (ACME)**\n- Won a $2B defence contract.\n- Raised FY guidance.",
             "**Zed Industries Limited (ZED)**\n- Board approved a 1:2 bonus issue."]
    summary, status = ns.collate_market_summary(None, "us_invested", notes, as_of_date="2026-10-03")
finally:
    llm_util.run_model_ladder = _real_ladder
tiers = (captured.get("stage3") or [[]])[0]
check(tiers == [(ns.COLLATION_MODEL, 0), (ns.COLLATION_FALLBACK_MODEL, 0), (ns.COLLATION_MODEL, 30),
                (ns.COLLATION_FALLBACK_MODEL, 0), (ns.REASONING_MODEL, 30)],
      f"D1: editor ladder waits 30 s between passes and ends on the filter model ({tiers})")
check(status == "fallback", f"D1: every model failing -> status 'fallback', not raw notes ({status})")
check(summary.splitlines() == ["* **Acme Corp (ACME)** - Won a $2B defence contract.; Raised FY guidance.",
                               "* **Zed Industries Limited (ZED)** - Board approved a 1:2 bonus issue."],
      f"D1: the fallback is one bullet per company, in the editor's format ({summary!r})")
check("---" not in summary, "D1: no raw '---' separators reach the digest")

# --- D2 visible ------------------------------------------------------------------------------
APP = (REPO / "app.py").read_text()
check("editor failed" in APP and 'collate_status' in APP, "D2: the News tab says when the editor failed")
NC = (REPO / "news_check.py").read_text()
check("::warning::" in NC and "collate_status" in NC, "D2: the run logs a warning when the editor failed")

# --- D3 the company name is the code's, not the model's -------------------------------
p2 = []
llm_util.run_model_ladder = lambda client, prompt, *a, **k: (p2.append(prompt), ("- x", "m"))[1]
try:
    ns.filter_batch_with_reasoning(None, "**Acme Corp (ACME)**:\nsome news", ["ACME"], "us_invested", "2026-10-03",
                                   ticker_names=NAMES)
finally:
    llm_util.run_model_ladder = _real_ladder
check(p2 and "bold ticker headers" not in p2[0] and "no header" in p2[0].lower(),
      "D3: Stage 2 is told to write bullets only, no header")
head = fn("note_with_header")
if head:
    check(head("ACME", "- Won a contract.", NAMES) == "**Acme Corp (ACME)**\n- Won a contract.",
          "D3: the code adds '**Company (TICKER)**'")
    check(head("ACME", "**ACME**\n- Won a contract.", NAMES) == "**Acme Corp (ACME)**\n- Won a contract.",
          "D3: a header the model wrote anyway is replaced, not doubled")
    check(head("ZED.NS", "* Bonus issue.", NAMES) == "**Zed Industries Limited (ZED)**\n* Bonus issue.",
          "D3: Indian tickers show the bare symbol")

# --- D4 filler ---------------------------------------------------------------------------
check(p2 and all(s in p2[0].lower() for s in ("class-action", "conference", "simply wall st", "5%")),
      "D4: the filter drops class-action ads, conferences, rating sites, and unexplained moves under 5%")
drop = fn("drop_filler_lines")
if drop:
    text = ("- Won a $2B defence contract.\n"
            "- Simply Wall St estimates the stock is 37% undervalued.\n"
            "- The Rosen Law Firm announces a class action on behalf of investors who purchased shares.\n"
            "- Shareholder alert: lead plaintiff deadline is November 3.\n"
            "- Settled the FTC complaint for $40M.")
    kept = drop(text).splitlines()
    check(kept == ["- Won a $2B defence contract.", "- Settled the FTC complaint for $40M."],
          f"D4: rating-site and law-firm-ad lines are dropped in code too ({kept})")
    check(drop("- Simply Wall St note.") == "", "D4: a note that was only filler becomes empty (quiet)")

# --- D5 funds are skipped ----------------------------------------------------------------
is_fund = fn("is_fund")
if is_fund:
    check(is_fund({"quote_type": "ETF"}) and not is_fund({"quote_type": "EQUITY", "company_name": "X Trust"}),
          "D5: Yahoo's quoteType decides when present")
    check(is_fund({"company_name": "Example Gold Trust", "reported_qtr": None}),
          "D5: without quoteType, a fund-like name that reports no quarters is a fund")
    check(not is_fund({"company_name": "Example Northern Trust", "reported_qtr": "Q2 2026"}),
          "D5: ...but a company that reports quarters is not (a bank called 'Trust')")
SRC = (REPO / "stock_data.py").read_text()
check('"quote_type": quote_type' in SRC, "D5: the snapshot stores Yahoo's quoteType")

# --- D6 dated by the 8 PM ET slot ---------------------------------------------------------
slot = fn("news_slot_date")
if slot:
    for now, want in ((datetime(2026, 10, 3, 22, 25, tzinfo=ET), date(2026, 10, 3)),
                      (datetime(2026, 10, 4, 0, 2, tzinfo=ET), date(2026, 10, 3)),
                      (datetime(2026, 10, 4, 2, 53, tzinfo=ET), date(2026, 10, 3)),
                      (datetime(2026, 10, 3, 19, 59, tzinfo=ET), date(2026, 10, 2)),
                      (datetime(2026, 10, 3, 20, 0, tzinfo=ET), date(2026, 10, 3))):
        check(slot(now) == want, f"D6: a run at {now:%a %H:%M} ET belongs to the {want} slot (got {slot(now)})")
win = fn("slot_window_date")
if win:
    check(win("ACME", date(2026, 10, 3)) == "2026-10-03", "D6: a US ticker's window ends on the slot date")
    check(win("ZED.NS", date(2026, 10, 3)) == "2026-10-04",
          "D6: an Indian ticker's ends on the IST date at 8 PM ET (the next morning)")
WF = (REPO / ".github/workflows/news-summary.yml").read_text()
check(f'slots: "{ns.NEWS_SLOT_HOUR_ET}"' in WF, "D6: NEWS_SLOT_HOUR_ET matches the workflow's slot")

# --- D7 repeats -----------------------------------------------------------------------------
prev_note = fn("previous_note")
if prev_note:
    prev = {"as_of": "2026-10-02", "markets": {"us_invested": {
        "summary": "* **Acme Corp (ACME)** - Won a $2B defence contract.\n* **Zork Inc (ZORK)** - Other.",
        "tickers": {"ACME": {"status": "material", "note": "**Acme Corp (ACME)**\n- Won a $2B defence contract."}}}}}
    check(prev_note(prev, "ACME", date(2026, 10, 3)) == "**Acme Corp (ACME)**\n- Won a $2B defence contract.",
          "D7: the previous digest's stored note is found")
    old = {"as_of": "2026-10-02", "markets": {"us_invested": {"summary": prev["markets"]["us_invested"]["summary"]}}}
    check("Won a $2B defence contract" in (prev_note(old, "ACME", date(2026, 10, 3)) or ""),
          "D7: a digest from before notes were stored is read from its summary line")
    check(prev_note(prev, "ACME", date(2026, 10, 2)) is None,
          "D7: a digest from the SAME slot (a failed run being retried) is never used to drop items")
    check(prev_note(prev, "ZZZ", date(2026, 10, 3)) is None, "D7: no previous note -> None")
p2.clear()
llm_util.run_model_ladder = lambda client, prompt, *a, **k: (p2.append(prompt), ("- x", "m"))[1]
try:
    ns.filter_batch_with_reasoning(None, "**Acme Corp (ACME)**:\nnews", ["ACME"], "us_invested", "2026-10-03",
                                   ticker_names=NAMES, previous="- Won a $2B defence contract.")
finally:
    llm_util.run_model_ladder = _real_ladder
check(p2 and "ALREADY REPORTED" in p2[0] and "Won a $2B defence contract" in p2[0],
      "D7: Stage 2 sees the previous digest's note and is told to drop repeats")

# --- the whole digest, end to end (stubbed) ----------------------------------------------
_saved = (ns.fetch_single_raw_news, llm_util.run_model_ladder, llm_util.make_client, ns.time.sleep,
          sd.load_settings, sd.load_data_snapshot, ns.load_news_summary)
searched = []


def fake_search(client, ticker, market, as_of_date, ticker_names=None, model=None):
    searched.append((ticker, as_of_date))
    return {"ACME": "2026-10-03: Acme won a $2B defence contract. Simply Wall St says it is cheap.",
            "ZED.NS": ""}.get(ticker, "Gold rose 1%."), []


def fake_ladder(client, prompt, tiers, config_for, label="", subject="", timeout=None, on_success=None):
    if label == "stage2":
        return "**ACME**\n- Won a $2B defence contract.\n- Simply Wall St says it is cheap.", tiers[0][0]
    return None, None                          # the editor fails every model


ns.fetch_single_raw_news = fake_search
llm_util.run_model_ladder = fake_ladder
llm_util.make_client = lambda *a, **k: object()
ns.time.sleep = lambda s: None
sd.load_settings = lambda: {**sd.DEFAULT_SETTINGS, "news_watchlist_scope": ["us_invested", "india_invested"]}
sd.load_data_snapshot = lambda: {"per_market": {"us_invested": [
    {"ticker": "ACME", "company_name": "Acme Corp", "quote_type": "EQUITY"},
    {"ticker": "FUNDX", "company_name": "Example Gold Trust", "quote_type": "ETF"}],
    "india_invested": [{"ticker": "ZED.NS", "company_name": "Zed Industries Limited", "quote_type": "EQUITY"}]}}
ns.load_news_summary = lambda: None
try:
    out = ns.build_news_summary({"us_invested": ["ACME", "FUNDX"], "india_invested": ["ZED.NS"]}, "k",
                                now=datetime(2026, 10, 4, 1, 30, tzinfo=ET))
finally:
    (ns.fetch_single_raw_news, llm_util.run_model_ladder, llm_util.make_client, ns.time.sleep,
     sd.load_settings, sd.load_data_snapshot, ns.load_news_summary) = _saved
us = out["markets"]["us_invested"]
check(out["as_of"] == "2026-10-03", f"E2E: a 1:30 AM run is dated by its slot ({out['as_of']})")
check(("ACME", "2026-10-03") in searched and ("ZED.NS", "2026-10-04") in searched,
      f"E2E: search windows come from the slot ({searched})")
check("FUNDX" not in [t for t, _ in searched] and us["tickers"]["FUNDX"]["status"] == "fund_skipped",
      "E2E: the fund is not searched, and is recorded as skipped")
check(us["collate_status"] == "fallback" and us["summary"].startswith("* **Acme Corp (ACME)** - Won a $2B defence contract.")
      and "Simply Wall St" not in us["summary"],
      f"E2E: editor failed -> code-formatted bullet, company named, filler dropped ({us['summary']!r})")
check(us["tickers"]["ACME"].get("note", "").startswith("**Acme Corp (ACME)**"),
      "E2E: each ticker's note is stored for tomorrow's repeat check")

# --- P1 three stocks at a time, same output ---------------------------------------
# The digest ran one stock at a time (~40-55 min for 51). Stages 1-2 now run on
# NEWS_CONCURRENT_TICKERS workers (the Sentiment job's 3), and the digest is
# assembled in watchlist order afterwards, so the result does not depend on
# which worker finished first.
import threading

check(getattr(ns, "NEWS_CONCURRENT_TICKERS", None) == 3, "P1: three workers, like the Sentiment job")
live = {"now": 0, "max": 0}
lock = threading.Lock()
calls_by_ticker = {}
DELAY = {"T1": 0.30, "T2": 0.05, "T3": 0.20, "T4": 0.01, "T5": 0.15, "T6": 0.02}


class Throttled(Exception):
    pass


def slow_search(client, ticker, market, as_of_date, ticker_names=None, model=None):
    with lock:
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        calls_by_ticker[ticker] = calls_by_ticker.get(ticker, 0) + 1
        n = calls_by_ticker[ticker]
    try:
        # Not time.sleep: the run below stubs that out (it is the same module
        # object), which would make every search instant and never overlap.
        threading.Event().wait(DELAY.get(ticker, 0.01))
        if ticker == "T5" and n == 1:
            raise Throttled("429 once")              # retryable: comes back in the retry pass
        if ticker == "T6":
            raise ValueError("400 bad request")      # terminal: failed
        return f"2026-10-03: {ticker} won a contract.", []
    finally:
        with lock:
            live["now"] -= 1


def stage2(client, prompt, tiers, config_for, label="", subject="", timeout=None, on_success=None):
    if label == "stage2":
        return "- Won a contract.", tiers[0][0]
    return None, None


_saved = (ns.fetch_single_raw_news, llm_util.run_model_ladder, llm_util.make_client, ns.time.sleep,
          sd.load_settings, sd.load_data_snapshot, ns.load_news_summary, ns._is_retryable)
ns.fetch_single_raw_news = slow_search
llm_util.run_model_ladder = stage2
llm_util.make_client = lambda *a, **k: object()
ns.time.sleep = lambda s: None
ns._is_retryable = lambda e: isinstance(e, Throttled)
sd.load_settings = lambda: {**sd.DEFAULT_SETTINGS, "news_watchlist_scope": ["us_a", "us_b"]}
sd.load_data_snapshot = lambda: {"per_market": {}}
ns.load_news_summary = lambda: None
try:
    out = ns.build_news_summary({"us_a": ["T1", "T2", "T3", "T5", "T6"], "us_b": ["T4", "T2", "T1"]}, "k",
                                now=datetime(2026, 10, 3, 21, 0, tzinfo=ET))
finally:
    (ns.fetch_single_raw_news, llm_util.run_model_ladder, llm_util.make_client, ns.time.sleep,
     sd.load_settings, sd.load_data_snapshot, ns.load_news_summary, ns._is_retryable) = _saved
check(live["max"] == 3, f"P1: three searches ran at once ({live['max']})")
check(calls_by_ticker == {"T1": 1, "T2": 1, "T3": 1, "T4": 1, "T5": 2, "T6": 1},
      f"P1: each stock searched once across watchlists; the throttled one retried once ({calls_by_ticker})")
a = out["markets"]["us_a"]
check(list(a["tickers"]) == ["T1", "T2", "T3", "T5", "T6"],
      f"P1: records follow the watchlist order, not finishing order ({list(a['tickers'])})")
check([a["tickers"][t]["status"] for t in ("T1", "T5", "T6")] == ["material", "material", "failed"],
      "P1: a retryable failure recovers in the retry pass; a terminal one is failed")
check([l.split("**")[1] for l in a["summary"].splitlines()] == ["T1", "T2", "T3", "T5"],
      f"P1: the digest lists stocks in watchlist order ({a['summary']!r})")
check(list(out["markets"]["us_b"]["tickers"]) == ["T4", "T2", "T1"] and out["totals"]["cache_hits"] == 2,
      f"P1: the second watchlist reuses the shared stocks ({out['totals']['cache_hits']} cache hits)")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
