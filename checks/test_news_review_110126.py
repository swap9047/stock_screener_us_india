"""News digest changes from the 2026-11-01 review (owner, same day).

N1 A failed run's retry searches only the stocks that failed. On 2026-10-26 Gemma's
   search returned 500 for 29 of 50 stocks; the slot's retry searched all 50 again.
N2 Results dates go into ONE "Results due" line per watchlist, each shown once.
   On Oct 31, 7 of 16 bullets were "board meeting on X to approve Q2 results".
N3 Big moves come from stored closes, one "Big moves (5%+)" line per watchlist,
   each session once; Stage 2 keeps a move only with a company-specific reason.
   On Oct 31, 5 of 16 bullets were a bare "stock fell 5%".
N4 Items about a different company are dropped (one India bullet carried another
   company's stake purchase).
N5 The Discord digest says how many stocks could not be checked (it was silent
   under news_check's 50% rule).
N6 The News tab shows each bullet's sources (not Discord).

Offline: invented tickers, stubbed models, no network, no data files.
"""

import sys
from datetime import date, datetime, timezone
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


ET = ZoneInfo("America/New_York")
UTC = timezone.utc
NAMES = {"ACME": "Acme Corp", "ZORP": "Zorp Inc.", "ZED.NS": "Zed Industries Limited", "CORE": "Core Corporation"}

# --- N2 the RESULTS tag -------------------------------------------------------------------
split = ns.split_results_tag
d, body = split("- Won a $2B order.\nRESULTS: 2026-11-04\n- Bonus issue 1:1 to be considered.", "2026-11-01")
check(d == "2026-11-04" and body == "- Won a $2B order.\n- Bonus issue 1:1 to be considered.",
      f"N2: the RESULTS line leaves the note and its date is kept ({d!r}, {body!r})")
check(split("* **RESULTS:** 2026-11-03", "2026-11-01") == ("2026-11-03", ""),
      "N2: a bulleted / bold tag is read too, and a note that was only the tag is empty (quiet)")
check(split("RESULTS: 2026-10-30\n- Order win.", "2026-11-01") == (None, "- Order win."),
      "N2: a past date (results already out, or a misread) is dropped, not shown")
check(split("RESULTS: 2026-12-30", "2026-11-01")[0] is None, "N2: ...and so is one far beyond the next few days")
check(split("RESULTS: 2026-11-05\nRESULTS: 2026-11-03", "2026-11-01")[0] == "2026-11-03",
      "N2: two tags -> the earliest date")
check(split("- Results beat estimates: revenue up 20%.", "2026-11-01") == (None, "- Results beat estimates: revenue up 20%."),
      "N2: a bullet that merely mentions results is untouched")

line = ns.results_line({"ZED.NS": "2026-11-04", "ACME": "2026-11-03", "ZORP": "2026-11-03"}, NAMES)
check(line == "**Results due** · Acme (ACME) 3 Nov · Zorp (ZORP) 3 Nov · Zed Industries (ZED) 4 Nov",
      f"N2: one line, by date, names without Corp / Inc. / Limited ({line!r})")
check(ns.results_line({}, NAMES) == "", "N2: no dates -> no line")

# --- N3 big moves from stored closes ---------------------------------------------------------
# A US stock on Tue 2026-11-03 (EST): Mon 11-02 closed and settled at 16:30 ET (21:30Z).
PERF = {"as_of": "2026-11-03 15:05 UTC", "markets": {"us_invested": {
    "ACME": {"2026-10-29": 100.0, "2026-10-30": 100.0, "2026-11-02": 107.2, "2026-11-03": 90.0},
    "ZORP": {"2026-10-30": 50.0, "2026-11-02": 47.4},
    "CORE": {"2026-10-30": 10.0, "2026-11-02": 10.3}},
    "india_invested": {"ZED.NS": {"2026-10-30": 200.0, "2026-11-02": 188.0, "2026-11-03": 212.0},
                       "ACME": {"2026-11-02": 1.0}}}}
now = datetime(2026, 11, 3, 16, 0, tzinfo=UTC)            # Tue 11 AM ET, mid US session
mv, checked = ns.session_moves("ACME", PERF, "2026-11-03", now)
# The window is NEWS_WINDOW_DAYS (7, since 2026-11-26): the older sessions in the fixture are looked at too (and
# checked, so no later digest looks again); only a 5%+ move is listed.
check(mv == {"2026-11-02": 7.2} and checked == ["2026-10-29", "2026-10-30", "2026-11-02"],
      f"N3: a run during the US session lists Monday's move, not today's live close ({mv}, {checked})")
check(ns.session_moves("ZORP", PERF, "2026-11-03", now)[0] == {"2026-11-02": -5.2},
      "N3: falls count too, from the previous stored close (Friday, over a weekend)")
check(ns.session_moves("CORE", PERF, "2026-11-03", now) == ({}, ["2026-10-30", "2026-11-02"]),
      "N3: a 3% move is not listed, but its session is checked (so it is not looked at again)")
check(ns.session_moves("ACME", PERF, "2026-11-03", now, already_checked=["2026-11-02"]) == ({}, ["2026-10-29", "2026-10-30"]),
      "N3: a session an earlier digest checked is not listed again")
# India, the same day: Tue 11-03 closed 15:30 IST, settled 16:00 IST = 10:30Z; stored 15:05Z.
mv, checked = ns.session_moves("ZED.NS", PERF, "2026-11-03", now)
check(mv == {"2026-11-02": -6.0, "2026-11-03": 12.8} and checked == ["2026-10-30", "2026-11-02", "2026-11-03"],
      f"N3: India's settled session today is in the window too ({mv})")
early = datetime(2026, 11, 3, 10, 10, tzinfo=UTC)           # 15:40 IST: before NSE settles
check(ns.session_moves("ZED.NS", PERF, "2026-11-03", early)[1] == ["2026-10-30", "2026-11-02"],
      "N3: a session not settled at run time is not checked (the next digest lists it)")
stale = {**PERF, "as_of": "2026-11-03 09:00 UTC"}           # stored mid NSE session
check(ns.session_moves("ZED.NS", stale, "2026-11-03", now)[1] == ["2026-10-30", "2026-11-02"],
      "N3: nor one whose close was stored before it settled (a live close)")
sunday = ns.session_moves("ACME", PERF, "2026-11-01", datetime(2026, 11, 1, 16, 0, tzinfo=UTC))
check(sunday == ({}, ["2026-10-29", "2026-10-30"]),
      "N3: a Sunday digest looks at the week's sessions up to Friday's (a 7-day window), none moved 5%")
check(ns.session_moves("ACME", PERF, "2026-10-12", datetime(2026, 10, 12, 16, 0, tzinfo=UTC)) == ({}, []),
      "N3: a window before the first stored close holds no session -> nothing")
week = {"as_of": "2026-11-03 15:05 UTC", "markets": {"x": {"ACME": {"2026-10-28": 100.0, "2026-10-29": 110.0, "2026-10-30": 110.0}}}}
check(ns.session_moves("ACME", week, "2026-11-03", now)[0] == {"2026-10-29": 10.0},
      "N3: a 5%+ move five days back is inside the 7-day window and listed (a twice-a-week digest must not miss it)")
check(ns.session_moves("ACME", {**week, "markets": {"x": {"ACME": {"2026-10-20": 100.0, "2026-10-21": 110.0}}}}, "2026-11-03", now)[0] == {},
      "N3: ...and one from before the window (13 days back) is not")
check(ns.session_moves("ACME", {"markets": PERF["markets"]}, "2026-11-03", now) == ({}, []),
      "N3: no stored-at time -> nothing (cannot tell a live close from a final one)")
gap = {"as_of": "2026-11-03 15:05 UTC", "markets": {"x": {"ACME": {"2026-10-20": 50.0, "2026-11-02": 100.0}}}}
check(ns.session_moves("ACME", gap, "2026-11-03", now)[0] == {},
      "N3: a previous close more than MOVE_MAX_GAP_DAYS back is a data hole, not a move")
check(ns.session_moves("NOPE", PERF, "2026-11-03", now) == ({}, []), "N3: no stored closes -> nothing")
# The digest slot is 18:00 ET (4 PM until the owner's second change on 2026-11-26): Wednesday's US session closed at 16:00,
# settled at 16:30 ET (21:30Z) and the hourly perf run at 17:00 ET stored its close (22:05Z), so a run at 18:00 ET
# (23:00Z) lists that day's move; at 16:00 ET (21:00Z) the session had not settled and its move would wait for the next digest.
WED = {"as_of": "2026-11-04 22:05 UTC", "markets": {"us_invested": {"ACME": {"2026-11-03": 100.0, "2026-11-04": 108.0}}}}
check(ns.session_moves("ACME", WED, "2026-11-04", datetime(2026, 11, 4, 23, 0, tzinfo=UTC))[0] == {"2026-11-04": 8.0},
      "N3: a 6 PM ET digest lists the same day's US session (settled 16:30, close stored 17:05)")
check(ns.session_moves("ACME", WED, "2026-11-04", datetime(2026, 11, 4, 21, 0, tzinfo=UTC)) == ({}, ["2026-11-03"]),
      "N3: ...which a 4 PM ET run could not (the session had not settled: its move would wait for the next digest)")
line = ns.moves_line({"ACME": {"2026-11-02": 7.2}, "ZORP": {"2026-11-02": -5.2},
                      "ZED.NS": {"2026-11-02": -6.0, "2026-11-03": 12.8}}, NAMES)
check(line == "**Big moves (5%+)** · 2 Nov: ▲ Acme (ACME) +7.2% · ▼ Zed Industries (ZED) -6.0% · "
              "▼ Zorp (ZORP) -5.2% | 3 Nov: ▲ Zed Industries (ZED) +12.8%",
      f"N3: rises then falls, largest first, one group per session ({line!r})")
check(ns.moves_line({}, NAMES) == "", "N3: no moves -> no line")

# --- N2-N4 the prompts ------------------------------------------------------------------------
p2 = []
_real_ladder = llm_util.run_model_ladder
llm_util.run_model_ladder = lambda client, prompt, *a, **k: (p2.append(prompt), ("- x", "m"))[1]
try:
    ns.filter_batch_with_reasoning(None, "**Acme Corp (ACME)**:\nnews", ["ACME"], "us_invested", "2026-11-01",
                                   ticker_names=NAMES)
finally:
    llm_util.run_model_ladder = _real_ladder
P = p2[0] if p2 else ""
check("RESULTS: YYYY-MM-DD" in P and "do NOT write a bullet" in P, "N2: Stage 2 tags a scheduled results date")
check("bonus issue" in P.lower(), "N2: ...but still writes a bullet for anything else material at that meeting")
check("no company-specific reason" in P and "lists every move of 5% or more" in P,
      "N3: Stage 2 drops a bare move; the code lists every 5%+ move")
check("DIFFERENT company" in P and "Acme Corp (ACME) itself" in P, "N4: Stage 2 drops another company's news")
S1 = (REPO / "news_summary.py").read_text().split("def fetch_single_raw_news", 1)[1].split("grounding_tool", 1)[0]
check("not about other" in S1, "N4: the search asks only for the company's own news")

# --- N6 source links in the News tab ------------------------------------------------------------
entry = {"summary": "* **Acme Corp (ACME)** - Won an order.\n* **Zed Industries Limited (ZED)** - Bonus.\n"
                    "**Results due** · Acme Corp (ACME) 3 Nov",
         "tickers": {"ACME": {"sources": [{"title": "example.com", "url": "https://r.example/1"},
                                          {"title": "example.com", "url": "https://r.example/2"},
                                          {"title": "news.example", "url": "https://r.example/3"},
                                          {"title": "third.example", "url": "https://r.example/4"}]},
                     "ZED.NS": {"sources": []}}}
out = ns.summary_with_source_links(entry).splitlines()
check(out[0] == "* **Acme Corp (ACME)** - Won an order. · [example.com](https://r.example/1) · "
                "[news.example](https://r.example/3)",
      f"N6: a bullet gets up to two links, one per site ({out[0]!r})")
check(out[1:] == entry["summary"].splitlines()[1:],
      "N6: a stock with no sources, and the code's own lines, are unchanged")
APP = (REPO / "app.py").read_text()
check("summary_with_source_links(" in APP, "N6: the News tab renders the summary with its links")
msgs = ns.build_discord_messages({"as_of": "2026-11-01", "markets": {"us_invested": {**entry, "counts": {}}}})
check(msgs and "r.example" not in "".join(msgs), "N6: ...and Discord does not")

# --- N5 Discord says how many were not searched ----------------------------------------------------
msgs = ns.build_discord_messages({"as_of": "2026-11-01", "markets": {
    "us_invested": {"summary": "* **Acme Corp (ACME)** - Won an order.", "counts": {"failed": 3}},
    "india_invested": {"summary": "* x", "counts": {"failed": 1}},
    "other": {"summary": "* y", "counts": {"failed": 0}}}})
text = "\n".join(msgs)
# Since 2026-11-15 the slot's next hourly retry searches them again (news_check retry_pending).
# "checked", not "searched", since 2026-11-22: a stock whose filter failed is counted too.
check("3 stocks could not be checked yet (search or filter errors), so they are not in this digest; the next hourly "
      "retry checks them again and posts any news." in text
      and "1 stock could not be checked yet (search or filter errors), so it is not in this digest; the next hourly "
      "retry checks it again and posts any news." in text,
      f"N5: the count of stocks not searched is in the digest ({text!r})")
check(text.count("could not be checked") == 2, "N5: ...and only where some failed")

# --- N1 + N2 + N3 end to end: the retry of a failed run ------------------------------------------
check(ns.reusable_outcomes({"as_of": "2026-11-03", "markets": {"m": {"tickers": {"A": {"status": "quiet"}}}}},
                           date(2026, 11, 3)) == {},
      "N1: nothing is reused from a run that did not fail (a manual re-run redoes everything)")
check(ns.reusable_outcomes({"as_of": "2026-11-02", "run_failed": True,
                            "markets": {"m": {"tickers": {"A": {"status": "quiet"}}}}}, date(2026, 11, 3)) == {},
      "N1: ...nor from another slot's failed run")
PRIOR = {"ZORP": {"results_date": "2026-11-05"}, "ACME": {"note": "**Acme Corp (ACME)**\n- Old item."}}
PREV = {"as_of": "2026-11-03", "run_failed": True, "prior_slot": PRIOR, "markets": {"us_invested": {"tickers": {
    "ACME": {"status": "material", "sources": [{"title": "s", "url": "u"}],
             "note": "**Acme Corp (ACME)**\n- Won a $2B order."},
    "ZORP": {"status": "quiet", "sources": [], "results_date": "2026-11-05"},
    "CORE": {"status": "failed", "sources": []},
    "ZED.NS": {"status": "degraded", "sources": [], "note": "raw"}}}}}
check(set(ns.reusable_outcomes(PREV, date(2026, 11, 3))) == {"ACME", "ZORP"},
      "N1: a failed run's material and quiet stocks are reusable; failed and degraded are redone")
check(ns.earlier_slot_info(PREV, date(2026, 11, 3)) == PRIOR,
      "N1: the retry's repeat checks use the EARLIER slot's info the failed run carried forward")
earlier = ns.earlier_slot_info({"as_of": "2026-11-02", "markets": {"m": {"tickers": {
    "ACME": {"status": "material", "note": "n", "results_date": "2026-11-04", "moves_checked": ["2026-11-02"],
             "sources": [1]}, "ZORP": {"status": "quiet"}}}}}, date(2026, 11, 3))
check(earlier == {"ACME": {"note": "n", "results_date": "2026-11-04", "moves_checked": ["2026-11-02"]}},
      f"N1: from an earlier slot's digest: each stock's note, results date and checked sessions ({earlier})")

searched, s2_prompts = [], []


def fake_search(client, ticker, market, as_of_date, ticker_names=None, model=None, **kw):
    searched.append(ticker)
    return {"CORE": "2026-11-03: Core to announce Q3 results on Nov 5.",
            "ZED.NS": "2026-11-03: Zed board to meet Nov 4 for Q2 results."}.get(ticker, ""), []


def fake_ladder(client, prompt, tiers, config_for, label="", subject="", timeout=None, on_success=None):
    if label == "stage2":
        s2_prompts.append(prompt)
        return ("RESULTS: 2026-11-05" if "Core" in prompt else "RESULTS: 2026-11-04\n- Zed won a defence order."), "m"
    return None, None                                       # the editor fails: code formats


_saved = (ns.fetch_single_raw_news, llm_util.run_model_ladder, llm_util.make_client, ns.time.sleep,
          sd.load_settings, sd.load_data_snapshot, ns.load_news_summary, ns._load_dashboard_perf)
ns.fetch_single_raw_news = fake_search
llm_util.run_model_ladder = fake_ladder
llm_util.make_client = lambda *a, **k: object()
ns.time.sleep = lambda s: None
sd.load_settings = lambda: {**sd.DEFAULT_SETTINGS, "news_watchlist_scope": ["us_invested"]}
sd.load_data_snapshot = lambda: {"per_market": {"us_invested": [
    {"ticker": t, "company_name": NAMES[t], "quote_type": "EQUITY"} for t in ("ACME", "ZORP", "CORE", "ZED.NS")]}}
ns.load_news_summary = lambda: PREV
ns._load_dashboard_perf = lambda: PERF
try:
    res = ns.build_news_summary({"us_invested": ["ACME", "ZORP", "CORE", "ZED.NS"]}, "k",
                                now=datetime(2026, 11, 3, 19, 0, tzinfo=ET))
    full = ns.build_news_summary({"us_invested": ["ACME", "ZORP", "CORE", "ZED.NS"]}, "k",
                                 now=datetime(2026, 11, 3, 19, 0, tzinfo=ET), reuse_failed_run=False)
finally:
    (ns.fetch_single_raw_news, llm_util.run_model_ladder, llm_util.make_client, ns.time.sleep,
     sd.load_settings, sd.load_data_snapshot, ns.load_news_summary, ns._load_dashboard_perf) = _saved
check(searched[:2] == ["CORE", "ZED.NS"] and sorted(searched[2:]) == sorted(["ACME", "ZORP", "CORE", "ZED.NS"]),
      f"N1: the retry searched only the failed and degraded stocks; a partial run searches all ({searched})")
us = res["markets"]["us_invested"]
summary = us["summary"]
check(summary.startswith("* **Acme Corp (ACME)** - Won a $2B order.\n* **Zed Industries Limited (ZED)** - Zed won"),
      f"N1: the reused stock's note is in the digest with the newly searched one's ({summary!r})")
check(us["tickers"]["ACME"]["status"] == "material" and us["tickers"]["ACME"]["sources"] == [{"title": "s", "url": "u"}]
      and us["tickers"]["CORE"]["status"] == "quiet",
      "N1: a reused stock keeps its status and sources; a results-only stock is quiet")
check("**Results due** · Core (CORE) 5 Nov · Zed Industries (ZED) 4 Nov" not in summary
      and "**Results due** · Zed Industries (ZED) 4 Nov · Core (CORE) 5 Nov" in summary
      and "Zorp (ZORP)" not in summary.split("Results due", 1)[1].split("\n")[0],
      f"N2: the Results due line has the new dates by date; ZORP's, shown by the earlier slot, is not repeated")
check(us["tickers"]["CORE"].get("results_date") == "2026-11-05" and "RESULTS" not in summary,
      "N2: the date is stored on the stock (for tomorrow's 'shown once'), and no tag reaches the digest")
check("**Big moves (5%+)** · 2 Nov: ▲ Acme (ACME) +7.2% · ▼ Zed Industries (ZED) -6.0% · ▼ Zorp (ZORP) -5.2%"
      " | 3 Nov: ▲ Zed Industries (ZED) +12.8%" in summary,
      f"N3: the Big moves line is in the digest ({summary!r})")
check(us["tickers"]["CORE"].get("moves_checked") == ["2026-10-30", "2026-11-02"] and us["tickers"]["ACME"].get("moves") == {"2026-11-02": 7.2},
      "N3: each stock's checked sessions (and listed moves) are stored for the next digest")
check(res["prior_slot"] == PRIOR, "N1: the retry carries the earlier slot's info forward again")
check(any("ALREADY REPORTED" not in p for p in s2_prompts),
      "N1: a stock the earlier slot had no note for gets no 'already reported' block")
check(us["counts"]["material"] == 2 and us["counts"]["quiet"] == 2 and us["collation_dropped"] == [],
      f"N1: counts cover reused and new stocks alike ({us['counts']})")

# --- news_check saves the flag and keeps it only for the same slot ----------------------------------
import news_check as nc

saved = []
_nc = (nc.get_gemini_api_key, nc.load_watchlists, nc.build_news_summary, nc.save_news_summary,
       nc.load_discord_webhook, nc.load_news_summary)
nc.get_gemini_api_key = lambda: "k"
nc.load_watchlists = lambda: {"us_invested": ["ACME", "ZORP"]}
nc.save_news_summary = lambda data: saved.append(data)
nc.load_discord_webhook = lambda: None
calls = []
try:
    def run(totals, as_of="2026-11-03", env=None, prev=None):
        import os
        saved.clear()
        nc.build_news_summary = lambda w, k, reuse_failed_run=True: (calls.append(reuse_failed_run), {
            "as_of": as_of, "totals": totals, "markets": {"us_invested": {"summary": "x"}}})[1]
        nc.load_news_summary = lambda: prev
        old = {k: os.environ.get(k) for k in ("REFRESH_MARKETS", "REFRESH_LIMIT")}
        os.environ.update(env or {"REFRESH_MARKETS": "", "REFRESH_LIMIT": ""})
        try:
            nc.main()
            code = 0
        except SystemExit as e:
            code = e.code
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return code, saved[-1] if saved else None

    code, data = run({"searched": 10, "failed": 5})
    check(code == 1 and data and data["run_failed"] is True and calls[-1] is True,
          "N1: a run with half its searches failed is saved marked run_failed (and fails)")
    code, data = run({"searched": 10, "failed": 4})
    # Since 2026-11-15 any failed stock leaves the slot to its next hourly retry (retry_pending, exit 1).
    check(code == 1 and data["run_failed"] is False and data["retry_pending"] is True,
          "N1: a run under the limit is saved as not failed, but with its leftovers pending (exit 1)")
    code, data = run({"searched": 10, "failed": 0})
    check(code == 0 and data["run_failed"] is False and data["retry_pending"] is False,
          "N1: a run with nothing failed is saved clean and succeeds")
    code, data = run({"searched": 1, "failed": 0}, env={"REFRESH_LIMIT": "1", "REFRESH_MARKETS": ""},
                     prev={"as_of": "2026-11-03", "run_failed": True, "markets": {}})
    check(data["run_failed"] is True and calls[-1] is False,
          "N1: a partial run never reuses, and keeps the slot's failed flag")
    code, data = run({"searched": 1, "failed": 0}, as_of="2026-11-04", env={"REFRESH_LIMIT": "1", "REFRESH_MARKETS": ""},
                     prev={"as_of": "2026-11-03", "run_failed": True, "markets": {}})
    check(data["run_failed"] is False, "N1: ...but not onto a new slot's date")
finally:
    (nc.get_gemini_api_key, nc.load_watchlists, nc.build_news_summary, nc.save_news_summary,
     nc.load_discord_webhook, nc.load_news_summary) = _nc

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
