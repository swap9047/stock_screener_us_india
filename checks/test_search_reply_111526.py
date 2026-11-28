"""Grounded searches that did not search, and the multi-model skip list (owner, 2026-11-15).

Gemma 26b, the search model for the news digest (N1), Expert Take (E1) and Sentiment (F1, F2), often answers
WITHOUT searching: an empty reply or a "No news found." line, with no search in its grounding metadata. On
2026-11-13 it did that for 4 of 10 stocks another model found real news for the same day. It also never records a
search when a stock is really quiet (7 of 7 empty replies over a weekend), so a missing search cannot simply be a
failure: such a reply gets ONE more try on another key, and a second one is taken as quiet (owner's choice).
Leftover tool-call text ('{"action": "google_search", ...}') is never an answer: always a failed, retryable try.

The same day Gemini 4 Hydrogen went in right after 3.8 Flash (two tries, the step's thinking budget), and the
Settings · Models "Skip Gemini 3.8 Flash calls" checkbox became a pick-list of models to skip.

R1 The reply tests: tool-call text, "nothing" without a search, a searched empty reply, news without a search record.
R2 SearchReplyCheck: tool-call text raises UnusableSearchReply (retryable); "nothing" unsearched is rechecked once,
   then accepted; one instance per search.
R3 run_model_ladder: a rechecked reply moves to the next rung on ANOTHER key; with no later answer the held reply
   is returned (quiet), never (None, None); a good second reply wins.
R4 The news search (N1, its own loop): the same rules end to end, and "No news found." counts as quiet text ("").
R5 Expert Take (E1) and Sentiment (F1, F2) searches go through the check; F1 and F2 turn an unsearched "no news"
   into their own no-news value.
R6 The skip list: any models, the legacy 3.8 switch still read and folded in, never an empty chain, each skipped
   model named once.
R7 Hydrogen: right after 3.8 Flash in the market reads, two tries, in the repository with thinking.

Offline: invented tickers, fake clients.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import llm_util
import model_settings as ms

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


class _GM:
    def __init__(self, queries):
        self.web_search_queries = queries
        self.grounding_chunks = []


class _Cand:
    def __init__(self, queries):
        self.grounding_metadata = _GM(queries) if queries is not None else None


class Resp:
    def __init__(self, text, queries=None):
        self.text = text
        self.candidates = [_Cand(queries)]


class Err(Exception):
    def __init__(self, code):
        super().__init__(f"{code} INTERNAL")
        self.code = code


class FakeClient:
    """Answers per `plan` (an exception or a Resp), recording the model and key of each call."""

    key_names = ["K1", "K2", "K3"]

    def __init__(self, plan):
        self.plan, self.calls = list(plan), []
        self.models = self

    def generate_content(self, model, contents, config, **extra):
        avoid = extra.get("_avoid_key")
        key = next(k for k in self.key_names if k != avoid)
        extra["_key_out"]["key"] = key
        self.calls.append({"model": model, "key": key, "avoid": avoid})
        step = self.plan.pop(0) if self.plan else Err(500)
        if isinstance(step, Exception):
            raise step
        return step


JUNK = Resp('{ "action": "google_search", "action_input": "ACME news" }', [])
EMPTY = Resp("", None)
NO_NEWS = Resp("*No news found for the specified period.*", [])
SEARCHED_EMPTY = Resp("", ["ACME news"])
NEWS = Resp("- 2026-11-13: ACME won an order.", ["ACME news"])
NEWS_NO_RECORD = Resp("- 2026-11-13: ACME won an order.", None)

# --- R1 the reply tests ----------------------------------------------------------------------------
check(llm_util.looks_like_tool_call(JUNK.text) and llm_util.looks_like_tool_call('```json\n{"tool_code": "google_search(q)"}')
      and not llm_util.looks_like_tool_call(NEWS.text) and not llm_util.looks_like_tool_call('{"headline": "x"}'),
      "R1: tool-call text is recognised, an answer or other JSON is not")
check(llm_util.unsearched_nothing(EMPTY) and llm_util.unsearched_nothing(NO_NEWS)
      and llm_util.unsearched_nothing(Resp("Empty Response (No news found)", [])),
      "R1: an empty or \"no news\" reply with no search recorded says nothing unsearched")
check(not llm_util.unsearched_nothing(SEARCHED_EMPTY) and not llm_util.unsearched_nothing(NEWS_NO_RECORD)
      and not llm_util.unsearched_nothing(Resp("Nonetheless shares rose 3% on 2026-11-13.", [])),
      "R1: a searched empty reply, news without a search record, or a sentence starting 'None...' are not")
check(not llm_util.unsearched_nothing(Resp("No news. " + "x" * 300, [])), "R1: a long reply is never taken for 'no news'")

# --- R2 SearchReplyCheck -----------------------------------------------------------------------------
c = llm_util.SearchReplyCheck()
try:
    c(JUNK)
    check(False, "R2: tool-call text raises")
except llm_util.UnusableSearchReply as e:
    check(llm_util.is_retryable(e), "R2: tool-call text raises UnusableSearchReply, a retryable failure")
try:
    c(EMPTY)
    check(False, "R2: the first unsearched 'nothing' is rechecked")
except llm_util.RecheckReply as e:
    check(e.result is EMPTY, "R2: the first unsearched 'nothing' raises RecheckReply carrying the reply")
check(c(EMPTY) is EMPTY and c(NEWS) is NEWS and c(SEARCHED_EMPTY) is SEARCHED_EMPTY,
      "R2: the second is accepted (quiet), and news or a searched empty reply always pass")
try:
    c(JUNK)
    check(False, "R2: tool-call text raises even after the recheck")
except llm_util.UnusableSearchReply:
    check(True, "R2: tool-call text raises even after the recheck is spent")

# --- R3 run_model_ladder ------------------------------------------------------------------------------
_sleep = llm_util.time.sleep
llm_util.time.sleep = lambda s: None
G = ms.GEMMA_26B
try:
    fc = FakeClient([EMPTY, NEWS])
    out = llm_util.run_model_ladder(fc, "p", [(G, 0)] * 3, lambda m: None, label="t", on_success=llm_util.SearchReplyCheck())
    check(out == (NEWS, G) and len(fc.calls) == 2, "R3: an unsearched empty reply is tried once more and the news wins")
    check(fc.calls[1]["avoid"] == fc.calls[0]["key"] and fc.calls[1]["key"] != fc.calls[0]["key"],
          f"R3: the recheck goes to another key ({[x['key'] for x in fc.calls]})")
    fc = FakeClient([EMPTY, Err(500), Err(500)])
    out = llm_util.run_model_ladder(fc, "p", [(G, 0)] * 3, lambda m: None, label="t", on_success=llm_util.SearchReplyCheck())
    check(out == (EMPTY, G) and len(fc.calls) == 3, "R3: nothing better later -> the held reply is returned (quiet), not (None, None)")
    fc = FakeClient([EMPTY, EMPTY, NEWS])
    out = llm_util.run_model_ladder(fc, "p", [(G, 0)] * 3, lambda m: None, label="t", on_success=llm_util.SearchReplyCheck())
    check(out == (EMPTY, G) and len(fc.calls) == 2, "R3: a second unsearched 'nothing' is accepted: one recheck only")
    fc = FakeClient([JUNK, JUNK, NEWS])
    out = llm_util.run_model_ladder(fc, "p", [(G, 0)] * 3, lambda m: None, label="t", on_success=llm_util.SearchReplyCheck())
    check(out == (NEWS, G) and fc.calls[1]["avoid"] == fc.calls[0]["key"],
          "R3: tool-call text is a failed try, each retry on another key")
    fc = FakeClient([JUNK, JUNK, JUNK])
    out = llm_util.run_model_ladder(fc, "p", [(G, 0)] * 3, lambda m: None, label="t", on_success=llm_util.SearchReplyCheck())
    check(out == (None, None), "R3: only tool-call text -> the ladder is exhausted (None, None)")

    # --- R4 the news search -------------------------------------------------------------------------
    import news_summary as ns
    T = [(G, 0, 30)] * 6
    fc = FakeClient([NO_NEWS, NEWS])
    text, _src = ns.fetch_single_raw_news(fc, "ACME", "us_invested", "2026-11-13", tiers=T)
    check(text == NEWS.text and len(fc.calls) == 2, "R4: an unsearched 'No news found.' gets one more try, and its news is kept")
    fc = FakeClient([EMPTY, NO_NEWS])
    text, _src = ns.fetch_single_raw_news(fc, "ACME", "us_invested", "2026-11-13", tiers=T)
    check(text == "" and len(fc.calls) == 2, "R4: two unsearched 'nothing' replies -> quiet, and the 'No news' line becomes ''")
    fc = FakeClient([EMPTY] + [Err(500)] * 5)
    text, _src = ns.fetch_single_raw_news(fc, "ACME", "us_invested", "2026-11-13", tiers=T)
    check(text == "" and len(fc.calls) == 6, "R4: the recheck then only 500s -> quiet (the held reply), not a failed stock")
    fc = FakeClient([JUNK] + [Err(500)] * 5)
    try:
        ns.fetch_single_raw_news(fc, "ACME", "us_invested", "2026-11-13", tiers=T)
        check(False, "R4: tool-call text then 500s raises")
    except Exception as e:
        check(llm_util.is_retryable(e) and len(fc.calls) == 6, f"R4: tool-call text then 500s -> the stock fails, retryably ({e})")
    fc = FakeClient([JUNK, NEWS])
    text, _src = ns.fetch_single_raw_news(fc, "ACME", "us_invested", "2026-11-13", tiers=T)
    check(text == NEWS.text and fc.calls[1]["key"] != fc.calls[0]["key"], "R4: after tool-call text, the next try (another key) answers")

    # --- R5 Expert Take and Sentiment -----------------------------------------------------------------
    import expert_views as ev
    import fundamentals_eval as fe
    _tiers = ms.tiers
    ms.tiers = lambda step, settings=None: [(G, 0, 30)] * 3
    try:
        fc = FakeClient([EMPTY, NEWS])
        text, _label = ev.fetch_gemma_expert_news(fc, "ACME", "us_invested", "Acme Corp")
        check(text == NEWS.text and len(fc.calls) == 2, "R5: E1 rechecks an unsearched empty reply")
        fc = FakeClient([JUNK, JUNK, JUNK])
        try:
            ev.fetch_gemma_expert_news(fc, "ACME", "us_invested", "Acme Corp")
            check(False, "R5: E1 with only tool-call text goes to the retry queue")
        except TimeoutError:
            check(True, "R5: E1 with only tool-call text goes to the retry queue (TimeoutError)")
        fc = FakeClient([NO_NEWS, NO_NEWS])
        text, _label = fe.fetch_fundamental_news(fc, "ACME", "us_invested", "Acme Corp", window_days=50)
        check(text == "No recent fundamental news found." and len(fc.calls) == 2,
              f"R5: F1 turns a twice-unsearched 'no news' into its own no-news value ({text[:40]})")
        fc = FakeClient([EMPTY, NEWS])
        text, _label = fe.fetch_fundamental_news(fc, "ACME", "us_invested", "Acme Corp", window_days=50)
        check(text == NEWS.text, "R5: F1 rechecks and keeps the news")
        fc = FakeClient([NO_NEWS, NO_NEWS])
        check(fe.fetch_targeted_earnings_numbers(fc, "ACME", "Acme Corp", "us_invested", "2026-11-01") == "",
              "R5: F2 returns '' for an unsearched 'no news'")
        fc = FakeClient([JUNK, NEWS])
        check(fe.fetch_targeted_earnings_numbers(fc, "ACME", "Acme Corp", "us_invested", "2026-11-01") == NEWS.text,
              "R5: F2 treats tool-call text as a failed try")
    finally:
        ms.tiers = _tiers
finally:
    llm_util.time.sleep = _sleep

# --- R6 the skip list ---------------------------------------------------------------------------------
F38, F36, H, L = ms.FLASH_38, ms.FLASH_36, ms.HYDROGEN, ms.FLASH_LITE
chain = [{"model": F38, "attempts": 2, "timeout": 200}, {"model": H, "attempts": 2, "timeout": 200},
         {"model": F36, "attempts": 2, "timeout": 200}, {"model": F38, "attempts": 1, "timeout": 200}]
saved = {"R1": {"chain": chain, "budget": 4096}}
both = {"ai_models": ms.with_skipped({"ai_models": saved}, ["gemini-3.8-flash", H, H])}
check(both["ai_models"][ms.SKIP_MODELS_KEY] == sorted([F38, H]) and both["ai_models"]["R1"] == saved["R1"],
      "R6: the list is stored with full ids, once each, every chain kept")
check([m for m, *_ in ms.tiers("R1", both)] == [F36, F36], "R6: both skipped models are left out of the run")
check(ms.skipped_in(ms.resolve("R1", both)["chain"], both) == [F38, H], "R6: the note names each skipped model once, in chain order")
legacy = {"ai_models": {**saved, ms.SKIP_38_KEY: True}}
check(ms.skipped_models(legacy) == {F38} and [m for m, *_ in ms.tiers("R1", legacy)] == [H, H, F36, F36],
      "R6: a setting saved with the old 3.8 switch still skips 3.8")
moved = ms.with_skipped(legacy, [H])
check(ms.SKIP_38_KEY not in moved and moved[ms.SKIP_MODELS_KEY] == [H], "R6: saving the list folds the old switch in (it goes)")
check(ms.SKIP_MODELS_KEY not in ms.with_skipped(both, []), "R6: an empty list removes the key")
allskip = {"ai_models": {**saved, ms.SKIP_MODELS_KEY: [F38, H, F36]}}
check([m for m, *_ in ms.tiers("R1", allskip)] == [F38, F38, H, H, F36, F36, F38] and ms.skipped_in(chain, allskip) == [],
      "R6: a chain made only of skipped models runs as saved (never an empty step), and is not noted as skipped")
check(ms.SKIP_MODELS_KEY not in ms.STEPS, "R6: the list's key is not a step id")

# --- R7 Hydrogen ----------------------------------------------------------------------------------------
check(ms.STEPS["R1"]["chain"] == ms.STEPS["R2"]["chain"] == [(F38, 1), (H, 1), (F36, 2)],
      "R7: the market reads are 3.8 x1, Hydrogen x1 (owner, 2026-11-27), 3.6 x2")
check(all(spec["chain"][i + 1] == (H, 1) for spec in ms.STEPS.values() for i, (m, _n) in enumerate(spec["chain"]) if m == F38),
      "R7: every default chain with 3.8 Flash has Hydrogen x1 right after it")
repo = {m["id"]: m for m in ms.repository({})}
check(H in repo and repo[H]["supports_thinking"] and repo[H]["label"] == "Gemini 4 Hydrogen",
      "R7: Hydrogen is in the model repository, with thinking")
check(ms.budget("R1", {}) == 4096 and H in [t[0] for t in ms.tiers("R1", {})],
      "R7: Hydrogen runs in R1, at the step's own thinking budget (4096, one budget for every rung)")
import market_read as mr
check(mr.FALLBACK_MODELS[0] == H and mr.FALLBACK_ATTEMPTS == {H: 1, F36: 2} and mr.PRIMARY_ATTEMPTS == 1, "R7: the market read's built-in ladder has Hydrogen after 3.8")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
