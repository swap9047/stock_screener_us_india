"""A nightly AI job says how many stocks it left stale (owner, 2026-11-16).

On 2026-11-14 Google's search model failed hundreds of calls. The Sentiment run
left 12 of 136 stocks with the previous night's view, the Expert Take run kept 2
write-ups and wrote 14 with no news -- and both runs were green, saying so only in
per-stock lines. Each job now ends with ONE `::warning::` annotation carrying the
counts (and nothing when every stock was refreshed):

  L1  llm_util.leftover_warning: the text, and None when there is nothing to say
  L2  llm_util.kept_previous: counted by what the store holds at the end
  L3  refresh_fundamentals.main prints it with the right counts
  L4  refresh_expert_views.main prints it, with the written-without-news count
  L5  the connector's watchlist description names the levels the data has (40%, not 50%)

Offline: invented tickers (ZQ01 ...), Gemini, the snapshot and the stores stubbed.
"""

import contextlib
import io
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import llm_util
import refresh_expert_views as rev
import refresh_fundamentals as rf

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- L1 the line itself ---------------------------------------------------------
w = llm_util.leftover_warning
check(w("Sentiment", 136, 0) is None and w("Expert Take", 134, 0, no_source=0) is None,
      "L1: nothing kept and nothing written without news -> no line")
line = w("Sentiment", 136, 12)
check(line == "::warning::Sentiment: 12 of 136 stock(s) kept their previous view "
              "(the search or the model failed for them).",
      f"L1: kept only ({line})")
line = w("Expert Take", 134, 2, no_source=14, kept_what="write-up")
check(line.startswith("::warning::Expert Take: 2 of 134 stock(s) kept their previous write-up")
      and line.endswith("; 14 were written with no news (the search failed)."),
      f"L1: kept and written without news ({line})")
line = w("Expert Take", 134, 0, no_source=3, kept_what="write-up")
check(line == "::warning::Expert Take: 3 were written with no news (the search failed).",
      f"L1: written without news only ({line})")
check("\n" not in w("Sentiment", 5, 5), "L1: one line (a GitHub annotation is one line)")

# --- L2 counted by the outcome ----------------------------------------------------
a, b, c = {"v": 1}, {"v": 2}, {"v": 3}
before = {"ZQ01": a, "ZQ02": b, "ZQ03": c, "ZQ04": None, "ZQ05": {"v": 5}}
after = {"ZQ01": a,                 # untouched: kept
         "ZQ02": {"v": 2},          # rewritten, even with the same content: not kept
         "ZQ03": c,                 # untouched: kept
         "ZQ04": {"v": 9}}          # had nothing before: never "kept"; ZQ05 is gone from the store
check(llm_util.kept_previous(after, before) == 2, "L2: only the views still in place are counted")
check(llm_util.kept_previous({}, {}) == 0 and llm_util.kept_previous(after, {"ZQ04": None}) == 0,
      "L2: an empty plan, or a stock with no earlier view, counts nothing")


def captured(fn):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn()
    return out.getvalue()


def warning_lines(text):
    return [l for l in text.splitlines() if l.startswith("::warning::")]


# --- L3 the Sentiment job ---------------------------------------------------------
WL = {"wl_a": ["ZQ01", "ZQ02", "ZQ03", "ZQ04"], "wl_b": ["ZQ02", "ZQ05", "ZQ06"]}
SNAP = {"per_market": {m: [{"ticker": t, "company_name": t} for t in tks] for m, tks in WL.items()}}


def run_sentiment(keep):
    for k in ("REFRESH_MARKETS", "REFRESH_LIMIT", "REFRESH_PENDING_ONLY"):
        os.environ.pop(k, None)
    store = {t: {"old": t} for tks in WL.values() for t in tks if t != "ZQ06"}   # ZQ06: no view yet

    def apply(st, tk, view, old, elapsed):
        if tk in keep:
            return 1, "FAILED (0.0s): news search failed, keeping prior result"
        st[tk] = view
        return 0, "OK"

    rf.TICKER_PAUSE_SECONDS = rf.RETRY_PAUSE_SECONDS = 0
    rf.get_gemini_api_key = lambda: "k"
    rf.llm_util.make_client = lambda key: object()
    rf.llm_util.log_key_usage = lambda client: None
    rf.load_data_snapshot = lambda: SNAP
    rf.load_watchlists = lambda: WL
    rf.load_fundamentals = lambda: store
    rf.save_fundamentals = lambda s: None
    rf.generate_fundamental_view = lambda client, row, *a, **kw: {"new": row["ticker"]}
    rf._apply_result = apply
    return captured(rf.main)


lines = warning_lines(run_sentiment(keep={"ZQ02", "ZQ05", "ZQ06"}))
check(lines == ["::warning::Sentiment: 2 of 6 stock(s) kept their previous view "
                "(the search or the model failed for them)."],
      f"L3: two stocks kept last night's view; the one with no earlier view is not 'kept' ({lines})")
check(warning_lines(run_sentiment(keep=set())) == [], "L3: every stock refreshed -> no warning line")
check(not any(t in l for l in lines for tks in WL.values() for t in tks),
      "L3: the line names no ticker (public logs)")

# --- L4 the Expert Take job --------------------------------------------------------
ESNAP = {"per_market": {m: [{"ticker": t, "company_name": t, "trend": "Uptrend", "stage": "S2 Advancing"}
                             for t in tks] for m, tks in WL.items()}}
NOW = datetime.now(timezone.utc)
STAMP = lambda dt: dt.strftime("%Y-%m-%d %H:%M")


def run_expert(fail, no_news):
    for k in ("REFRESH_MARKETS", "REFRESH_LIMIT", "GITHUB_EVENT_NAME", "SCHEDULED_DISPATCH",
              "REFRESH_PENDING_ONLY", "REFRESH_SENTIMENT_FIRST"):
        os.environ.pop(k, None)
    os.environ["GITHUB_EVENT_NAME"] = "workflow_dispatch"       # a run by hand: everything is analysed
    yesterday = STAMP(NOW - timedelta(days=1))
    store = {t: {"verdict": "HOLD", "headline": "old", "model_used": "m", "as_of": yesterday,
                 "news_source": "search"} for tks in WL.values() for t in tks}

    def gen(client, row, **kw):
        tk = row["ticker"]
        if tk in fail:                                           # a failure stub: the fresh prior is kept
            return {"verdict": "HOLD", "headline": "Analysis pending", "model_used": "Error", "as_of": STAMP(NOW)}
        return {"verdict": "HOLD", "headline": "new", "model_used": "m", "as_of": STAMP(NOW),
                "news_source": "⚪ No Source" if tk in no_news else "search"}

    rev.time.sleep = lambda s: None
    rev.get_gemini_api_key = lambda: "k"
    rev.llm_util.make_client = lambda key: object()
    rev.llm_util.log_key_usage = lambda client: None
    rev.load_data_snapshot = lambda: ESNAP
    rev.load_watchlists = lambda: WL
    rev.load_expert_views = lambda: store
    rev.save_expert_views = lambda s: None
    rev.load_fundamentals = lambda: {}
    rev.generate_expert_view = gen
    rev.active_alerts_for_prompt = lambda rows: None
    rev.alerts_text_for = lambda a, t: ""
    text = captured(rev.main)
    return warning_lines(text), store


lines, store = run_expert(fail={"ZQ03"}, no_news={"ZQ01", "ZQ05"})
check(store["ZQ03"]["headline"] == "old" and store["ZQ01"]["headline"] == "new",
      "L4 fixture: the failed stock kept its write-up, the others were rewritten")
check(lines == ["::warning::Expert Take: 1 of 6 stock(s) kept their previous write-up "
                "(the search or the model failed for them); 2 were written with no news (the search failed)."],
      f"L4: one kept, two written without news ({lines})")
lines, _ = run_expert(fail=set(), no_news=set())
check(lines == [], f"L4: a clean run prints no warning line ({lines})")

# --- L5 the connector's wording ----------------------------------------------------
import breadth_math

src = (REPO / "api" / "mcp.py").read_text()
levels = " / ".join(str(l) for l in breadth_math.DRAWDOWN_LEVELS)
check(f"{levels}%+ below their" in src and "30 / 50%+" not in src,
      f"L5: list_watchlists describes the drawdown levels the data has ({levels}%)")

print("FAILURES:", fails)
sys.exit(fails)
