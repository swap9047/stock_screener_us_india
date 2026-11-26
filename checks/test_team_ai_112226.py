"""AI-pipeline review fixes (2026-11-22).

A1 A Gemini backup in a SEARCH chain (N1 / E1 / F1 / F2): its call gets the google_search tool and a small
   explicit thinking budget (model_settings.SEARCH_THINKING_BUDGET, or a whole number saved by hand as the
   step's "budget"); Gemma gets the tool alone. Gemma 500 INTERNAL -> flash-lite answers -> the text comes
   back with flash-lite's label. A Gemini reply's recorded searches (web_search_queries) count as searching.
   The defaults are NOT changed (S1 in test_model_settings.py holds them to the old ladders).
A2 A dead model (404, or every key out of its daily quota: ModelExhausted) is skipped for the rest of the
   ladder with no wait: A x3, B x1, A 404 -> one call to A, no sleep, B answers -- in run_model_ladder and in
   the news search's own loop.
A3 A 403 retires the (key, model) pair, not the key: the key still serves the Gemma search; a 403 on every
   key steps the ladder to the next model; an invalid key (API_KEY_INVALID, 401) is still dead everywhere.
A4 After a 500 that came back fast, the next try waits only the base; 503s, 429s and slow 500s keep the
   exponential wait -- in run_model_ladder and the news search loop.
A5 A stock whose news filter (N2) failed on every model: its raw search text is kept out of the editor's
   prompt, the digest and the follow-up, it counts as failed (a leftover the slot's retry runs again), the
   digest says "could not be checked", and the retry filters the kept search text without searching again.
A6 Market read numbers: an ISO date's digits are not known numbers (only the date, next to a month name, or
   its year); "+3.4" / "up 3.4%" / "rose 3.4%" against only-negative facts is refused, "-3.4" against only
   positive ones too; "down 3.4%", "fell to 3.4%" and plain levels pass.
A7 A thinking budget of 0 ("Off") is sent as thinking_budget=0 by the market read and the panel's Test.
A8 Sentiment: a view written after a failed search ("No Source") never replaces a fresh prior, whatever
   its label (a "Neutral" did).
A9 Dead helpers are gone; the old skip_flash_38 key is still read through skipped_models.

Offline: invented tickers, fake clients, no network.
"""
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import llm_util
import model_settings as ms
import stock_data as sd

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


G, L, F36, F38 = ms.GEMMA_26B, ms.FLASH_LITE, ms.FLASH_36, ms.FLASH_38


class Resp:
    """A grounded reply: text, and (when searched) the queries and sources in its grounding metadata."""

    def __init__(self, text, queries=(), sources=()):
        self.text = text
        gm = types.SimpleNamespace(
            web_search_queries=list(queries),
            grounding_chunks=[types.SimpleNamespace(web=types.SimpleNamespace(uri=u, title=t)) for t, u in sources])
        self.candidates = [types.SimpleNamespace(grounding_metadata=gm)]


class Err(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        if code is not None:
            self.code = code


# The real sleep and clock, restored after each section: llm_util and news_summary share the `time` module.
REAL_SLEEP, REAL_MONO = time.sleep, time.monotonic
slept = []


def patch_sleep():
    slept.clear()
    time.sleep = lambda s: slept.append(s)


def restore_time():
    time.sleep, time.monotonic = REAL_SLEEP, REAL_MONO


llm_util.RETRY_BACKOFF_SECONDS = 2      # the real base (another check may have changed it in-process)

# --- A1 search config per model ----------------------------------------------------------------------
cfg_g = ms.search_config(G)
cfg_l = ms.search_config(L)
check(cfg_g.tools and cfg_g.tools[0].google_search is not None and cfg_g.thinking_config is None,
      "A1: Gemma's search config is the google_search tool alone, no thinking config")
check(cfg_l.tools and cfg_l.tools[0].google_search is not None
      and cfg_l.thinking_config.thinking_budget == ms.SEARCH_THINKING_BUDGET == 512,
      "A1: a Gemini model in a search chain gets the tool and a 512 thinking budget")
check(ms.search_config(L, 0).thinking_config.thinking_budget == 0, "A1: a saved 0 is sent as 0 (no thinking)")
saved_n1 = {"ai_models": {"N1": {"chain": [{"model": G, "attempts": 3}, {"model": L, "attempts": 1}], "budget": 256}}}
check(ms.search_budget("N1", saved_n1) == 256 and ms.search_budget("E1", saved_n1) == 512
      and ms.search_budget("N1", {"ai_models": {"N1": {"budget": "x"}}}) == 512
      and ms.resolve("N1", saved_n1)["budget"] is None,
      "A1: a budget saved by hand for a search step is used (resolve still reports none), else 512")
check(ms.search_source_label(G) == "🔍 Gemma-4-26B (Google Search)"
      and ms.search_source_label(L) == "🔍 gemini-3.5-flash-lite (Google Search)",
      "A1: source labels: Gemma's as before, a backup by its short name")
check([m for m, *_ in ms.tiers("N1", {})] == [G, G, G] and [m for m, *_ in ms.tiers("E1", {})] == [G, G, G],
      "A1: the defaults stay Gemma x3 (S1's contract: defaults reproduce the old ladders)")

OWNER_CHAIN = [{"model": G, "attempts": 3, "timeout": 200}, {"model": L, "attempts": 1, "timeout": 200},
               {"model": G, "attempts": 2, "timeout": 200}]
OWNER = {"ai_models": {s: {"chain": OWNER_CHAIN} for s in ("N1", "E1", "F1")}}
check([m for m, *_ in ms.tiers("N1", OWNER)] == [G, G, G, L, G, G], "A1: the owner's chain runs Gemma x3, lite, Gemma x2")


class PerModel:
    """generate_content stand-in (a plain client, no key rotation): Gemma raises 500, flash-lite answers searched."""

    def __init__(self):
        self.calls = []
        self.models = self

    def generate_content(self, model, contents, config, **kw):
        self.calls.append((model, config))
        if "gemma" in model:
            raise Err("500 INTERNAL. Internal error encountered.", 500)
        return Resp("2026-11-21: Acme Corp won a large order.", ["acme corp news"], [("Wire", "https://x.example/1")])


import news_summary as ns  # noqa: E402  (after the stdlib-only imports above)
import expert_views as ev  # noqa: E402
import fundamentals_eval as fe  # noqa: E402

patch_sleep()
try:
    fc = PerModel()
    text, sources = ns.fetch_single_raw_news(fc, "ACME", "us_invested", "2026-11-21", tiers=ms.tiers("N1", OWNER))
    lite_cfg = [c for m, c in fc.calls if m == L]
    gem_cfg = [c for m, c in fc.calls if m == G]
    check(text.startswith("2026-11-21: Acme Corp won") and sources == [{"title": "Wire", "url": "https://x.example/1"}],
          f"A1: N1 -- Gemma 500 x3, then flash-lite's grounded answer is returned with its sources ({text!r})")
    check([m for m, _ in fc.calls] == [G, G, G, L], f"A1: N1 -- the calls in order ({[m[-12:] for m, _ in fc.calls]})")
    check(lite_cfg and lite_cfg[0].thinking_config.thinking_budget == 512 and lite_cfg[0].tools[0].google_search is not None
          and all(c.thinking_config is None and c.tools[0].google_search is not None for c in gem_cfg),
          "A1: N1 -- flash-lite's call carries the tool and 512 thinking, Gemma's the tool alone")

    real_load = sd.load_settings
    sd.load_settings = lambda: OWNER
    try:
        fc = PerModel()
        news, source = ev.fetch_gemma_expert_news(fc, "ACME", "us_invested", "Acme Corp")
        check("won a large order" in news and source == "🔍 gemini-3.5-flash-lite (Google Search)"
              and [m for m, _ in fc.calls] == [G, G, G, L]
              and next(c for m, c in fc.calls if m == L).thinking_config.thinking_budget == 512,
              f"A1: E1 -- the backup answers, labelled by its short name ({source})")
        fc = PerModel()
        news, source = fe.fetch_fundamental_news(fc, "ACME", "us_invested", "Acme Corp", window_days=30)
        check("won a large order" in news and source == "🔍 gemini-3.5-flash-lite (Google Search)"
              and next(c for m, c in fc.calls if m == L).thinking_config.thinking_budget == 512,
              f"A1: F1 -- the backup answers, labelled by its short name ({source})")
        # A Gemini reply that records its searches and says "No news found." is quiet, not rechecked.
        class QuietLite(PerModel):
            def generate_content(self, model, contents, config, **kw):
                self.calls.append((model, config))
                if "gemma" in model:
                    raise Err("500 INTERNAL", 500)
                return Resp("No news found.", ["acme corp news"])
        fc = QuietLite()
        news, source = ev.fetch_gemma_expert_news(fc, "ACME", "us_invested", "Acme Corp")
        check(news == "No recent news found." and [m for m, _ in fc.calls] == [G, G, G, L],
              "A1: a Gemini 'no news' reply that recorded a search is taken as quiet at once (SearchReplyCheck)")
    finally:
        sd.load_settings = real_load
finally:
    restore_time()


# --- A2 dead models -------------------------------------------------------------------------------------
class Scripted:
    """Plain client: `script` maps a model to an exception class/instance to raise, else answers."""

    def __init__(self, script):
        self.script, self.calls = script, []
        self.models = self

    def generate_content(self, model, contents, config, **kw):
        self.calls.append(model)
        err = self.script.get(model)
        if err is not None:
            raise err
        return Resp(f"answer from {model}", ["q"])


A, B = "models/gemini-a", "models/gemini-b"
tiers_ab = ms.build_tiers([{"model": A, "attempts": 3, "timeout": 200}, {"model": B, "attempts": 1, "timeout": 200}])
check([w for _m, w, _t in tiers_ab] == [0, 2, 4, 8], "A2: the rungs' precomputed waits are 0, 2, 4, 8")
patch_sleep()
try:
    fc = Scripted({A: Err("404 NOT_FOUND. models/gemini-a is not found for API version v1beta", 404)})
    out = llm_util.run_model_ladder(fc, "p", tiers_ab, lambda m: None, label="t")
    check(out[1] == B and fc.calls == [A, B] and slept == [],
          f"A2: run_model_ladder -- A 404 once, no sleep, B answers ({fc.calls}, slept {slept})")
    patch_sleep()
    fc = Scripted({A: Err("404 NOT_FOUND. models/gemini-a is not found", 404)})
    text, _ = ns.fetch_single_raw_news(fc, "ACME", "us_invested", "2026-11-21", tiers=tiers_ab)
    check(text == f"answer from {B}" and fc.calls == [A, B] and slept == [],
          f"A2: the news search loop -- the same ({fc.calls}, slept {slept})")

    # Every key spent its daily quota for A: one call per key, then ModelExhausted, A's other rungs skipped unslept.
    DAILY = ("429 RESOURCE_EXHAUSTED. {'details': [{'violations': [{'quotaId': "
             "'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}]}")

    class KeyStub:
        def __init__(self, key, log):
            self.key, self.log = key, log
            self.models = self

        def generate_content(self, **kw):
            self.log.append((self.key, kw["model"]))
            if kw["model"] == A:
                raise Err(DAILY, 429)
            return Resp("b answers", ["q"])

    log = []
    rc = llm_util.RotatingGeminiClient([("K1", "k1"), ("K2", "k2")])
    rc._client_for = lambda key: KeyStub(key, log)
    patch_sleep()
    out = llm_util.run_model_ladder(rc, "p", tiers_ab, lambda m: None, label="t")
    check(out[1] == B and sorted(k for k, m in log if m == A) == ["k1", "k2"] and slept == [],
          f"A2: every key out of A's daily quota -> one call per key, no wait, B answers ({log}, slept {slept})")
    try:
        rc.models.generate_content(model=A, contents="p")
        check(False, "A2: a further call on A raises")
    except llm_util.ModelExhausted as e:
        check(llm_util.is_dead_model(e) and llm_util.is_retryable(e) and len([1 for k, m in log if m == A]) == 2,
              f"A2: ...ModelExhausted, at once and with no API call ({e})")
finally:
    restore_time()


# --- A3 403 per (key, model) ------------------------------------------------------------------------------
DENIED = "403 PERMISSION_DENIED. {'error': {'code': 403, 'message': 'Permission denied on resource', 'status': 'PERMISSION_DENIED'}}"
INVALID = "400 INVALID_ARGUMENT. API key not valid. Please pass a valid API key. reason: API_KEY_INVALID"
check(llm_util.is_permission_denied(Err(DENIED, 403)) and not llm_util.is_key_invalid(Err(DENIED, 403)),
      "A3: a 403 is a permission refusal, not a dead key")
check(llm_util.is_key_invalid(Err(INVALID, 400)) and llm_util.is_key_invalid(Err("401 UNAUTHENTICATED", 401))
      and not llm_util.is_permission_denied(Err(INVALID, 400)),
      "A3: an invalid key (API_KEY_INVALID) and a 401 are dead keys")


class PerKey:
    def __init__(self, key, log, denied, invalid=()):
        self.key, self.log, self.denied, self.invalid = key, log, denied, invalid
        self.models = self

    def generate_content(self, **kw):
        self.log.append((self.key, kw["model"]))
        if self.key in self.invalid:
            raise Err(INVALID, 400)
        if (self.key, kw["model"]) in self.denied:
            raise Err(DENIED, 403)
        return Resp(f"{kw['model']} via {self.key}", ["q"])


PREVIEW = "models/gemini-preview"
log = []
rc = llm_util.RotatingGeminiClient([("K1", "k1"), ("K2", "k2")])
rc._client_for = lambda key: PerKey(key, log, {("k1", PREVIEW)})
for _ in range(12):
    rc.models.generate_content(model=PREVIEW, contents="p")
for _ in range(40):
    rc.models.generate_content(model=G, contents="p")
check("K1" not in rc._dead and ("K1", PREVIEW) in rc._forbidden,
      "A3: a 403 on the preview model retires (K1, preview), not K1")
check(any(k == "k1" and m == G for k, m in log) and len([1 for k, m in log if k == "k1" and m == PREVIEW]) == 1,
      "A3: K1 still serves the Gemma search, and is not tried on the preview model again")
log = []
rc = llm_util.RotatingGeminiClient([("K1", "k1"), ("K2", "k2")])
rc._client_for = lambda key: PerKey(key, log, {("k1", PREVIEW), ("k2", PREVIEW)})
patch_sleep()
try:
    tiers_pg = ms.build_tiers([{"model": PREVIEW, "attempts": 3, "timeout": 200}, {"model": G, "attempts": 1, "timeout": 200}])
    out = llm_util.run_model_ladder(rc, "p", tiers_pg, lambda m: None, label="t")
finally:
    restore_time()
check(out[1] == G and len([1 for k, m in log if m == PREVIEW]) == 2 and slept == [] and not rc._dead,
      f"A3: a 403 on every key steps the ladder to the next model at once ({log}, slept {slept})")
log = []
rc = llm_util.RotatingGeminiClient([("K1", "k1"), ("K2", "k2")])
rc._client_for = lambda key: PerKey(key, log, set(), invalid=("k1",))
for _ in range(20):
    rc.models.generate_content(model=G, contents="p")
    rc.models.generate_content(model=PREVIEW, contents="p")
check("K1" in rc._dead and len([1 for k, m in log if k == "k1"]) == 1,
      "A3: an invalid key is still dead on every model after one call")


# --- A4 waits after a fast 500 ----------------------------------------------------------------------------
G4 = ms.build_tiers([{"model": G, "attempts": 4, "timeout": 200}])


def run_waits(err, seconds_per_call=0.0, loop="ladder"):
    clock = [1000.0]
    time.monotonic = lambda: clock[0]

    class C:
        models = None

        def generate_content(self, model, contents, config, **kw):
            clock[0] += seconds_per_call
            raise err

    c = C()
    c.models = c
    patch_sleep()
    try:
        if loop == "ladder":
            llm_util.run_model_ladder(c, "p", G4, lambda m: None, label="t")
        else:
            try:
                ns.fetch_single_raw_news(c, "ACME", "us_invested", "2026-11-21", tiers=G4)
            except Exception:
                pass
        return list(slept)
    finally:
        restore_time()


E500, E503, E429 = Err("500 INTERNAL", 500), Err("503 UNAVAILABLE", 503), Err("429 RESOURCE_EXHAUSTED quota", 429)
check(run_waits(E500, 2.0) == [2, 2, 2], f"A4: fast 500s wait only the base ({run_waits(E500, 2.0)})")
check(run_waits(E503, 2.0) == [2, 4, 8] and run_waits(E429, 2.0) == [2, 4, 8],
      "A4: 503s and 429s keep the exponential wait")
check(run_waits(E500, 15.0) == [2, 4, 8], "A4: a slow 500 (15 s) keeps the exponential wait")
check(run_waits(E500, 2.0, loop="news") == [2, 2, 2] and run_waits(E503, 2.0, loop="news") == [2, 4, 8],
      "A4: the news search loop paces the same way")
check(llm_util.next_wait(8, None) == 8 and llm_util.next_wait(0, E500, 1) == 0
      and llm_util.next_wait(8, Err("404 NOT_FOUND", 404), 1) == 0,
      "A4: next_wait -- no failure keeps the wait, no wait stays none, a dead model waits none")


# --- A5 a failed filter is a leftover, never posted ------------------------------------------------------
ET = ZoneInfo("America/New_York")
NAMES = {"ACME": "Acme Corp", "ZORP": "Zorp Inc."}
RAW = "2026-11-02: SECRETRAW Acme Corp rumour from a forum, undated items, a class-action ad."
searched, collation_prompts, stage2 = [], [], {"fail": True, "prompts": []}


def fake_search(client, ticker, market, as_of_date, ticker_names=None, model=None, **kw):
    searched.append(ticker)
    return ({"ACME": RAW, "ZORP": "2026-11-02: Zorp Inc. won a defence order."}[ticker],
            [{"title": "src", "url": f"https://x.example/{ticker}"}])


def fake_gwt(client, model, contents, config, timeout=None, avoid_key=None, key_sink=None):
    if "senior financial analyst" in contents:
        stage2["prompts"].append(contents)
        if stage2["fail"] and "Acme" in contents:
            raise Err("503 UNAVAILABLE. The model is overloaded.", 503)
        return Resp("- Won a defence order." if "Zorp" in contents else "- Acme Corp won a new contract.")
    collation_prompts.append(contents)
    return Resp("* edited digest")


_saved = (ns.fetch_single_raw_news, llm_util.generate_with_timeout, llm_util.make_client, sd.load_settings,
          sd.load_data_snapshot, ns.load_news_summary, ns._load_dashboard_perf)
ns.fetch_single_raw_news = fake_search
llm_util.generate_with_timeout = fake_gwt
llm_util.make_client = lambda *a, **k: object()
sd.load_settings = lambda: {**sd.DEFAULT_SETTINGS, "news_watchlist_scope": ["us_invested"]}
sd.load_data_snapshot = lambda: {"per_market": {"us_invested": [
    {"ticker": t, "company_name": NAMES[t], "quote_type": "EQUITY"} for t in NAMES]}}
ns.load_news_summary = lambda: None
ns._load_dashboard_perf = lambda: None
NOW = datetime(2026, 11, 3, 17, 0, tzinfo=ET)   # after the 4 PM slot (6 AM until 2026-11-26)
patch_sleep()
try:
    first = ns.build_news_summary({"us_invested": ["ACME", "ZORP"]}, "k", now=NOW)
    first_collation = list(collation_prompts)
    acme = first["markets"]["us_invested"]["tickers"]["ACME"]
    us = first["markets"]["us_invested"]
    check(acme["status"] == ns.STATUS_FAILED and acme.get("failed_stage") == "filter" and acme.get("search_text") == RAW
          and "note" not in acme,
          f"A5: every N2 rung failing -> ACME recorded failed (filter), its search kept, no note ({acme})")
    check(first_collation and all("SECRETRAW" not in p for p in first_collation) and "SECRETRAW" not in us["summary"],
          "A5: the raw search text is not in the editor's prompt nor the digest")
    check(us["counts"]["failed"] == 1 and us["counts"]["filter_failed"] == 1 and us["counts"]["degraded"] == 0
          and first["totals"]["failed"] == 1 and ns.failed_tickers(first) == {"ACME"},
          f"A5: it counts as failed (news_check's leftovers) ({us['counts']})")
    msgs = "\n".join(ns.build_discord_messages(first))
    check("1 stock could not be checked yet (search or filter errors)" in msgs and "SECRETRAW" not in msgs,
          "A5: the digest says it could not be checked; no raw text in Discord")
    # An older digest's "degraded" note is not posted as a follow-up either.
    old = {"as_of": "2026-11-03", "markets": {"us_invested": {"tickers": {
        "ACME": {"status": "degraded", "note": "**Acme Corp (ACME)**:\nSECRETRAW"}}}}}
    check(ns.build_followup_messages(old, {"ACME"}) == [], "A5: a degraded note is never a follow-up")

    # The slot's retry (news_check saved the run retry_pending): ACME is filtered from its kept search, not searched.
    searched.clear()
    collation_prompts.clear()
    stage2["fail"] = False
    stage2["prompts"].clear()
    prev = {**first, "retry_pending": True, "posted": True}
    check(set(ns.reusable_searches(prev, NOW.date())) == {"ACME"} and set(ns.reusable_outcomes(prev, NOW.date())) == {"ZORP"},
          "A5: the retry reuses ZORP's outcome and ACME's search")
    ns.load_news_summary = lambda: prev
    second = ns.build_news_summary({"us_invested": ["ACME", "ZORP"]}, "k", now=NOW)
    acme2 = second["markets"]["us_invested"]["tickers"]["ACME"]
    check(searched == [] and any("SECRETRAW" in p for p in stage2["prompts"]),
          f"A5: the retry searched nothing and filtered ACME's kept search text ({searched})")
    check(acme2["status"] == ns.STATUS_MATERIAL and "won a new contract" in acme2.get("note", "")
          and "SECRETRAW" not in acme2.get("note", ""),
          f"A5: ...and ACME is material now, with the filtered note ({acme2.get('status')})")
    follow = "\n".join(ns.build_followup_messages(second, ns.failed_tickers(prev) - ns.failed_tickers(second)))
    check("won a new contract" in follow and "SECRETRAW" not in follow,
          "A5: the follow-up posts the filtered note only")
    check(not ns.reusable_searches({**first}, NOW.date()), "A5: a run not marked for retry reuses no search")
finally:
    (ns.fetch_single_raw_news, llm_util.generate_with_timeout, llm_util.make_client, sd.load_settings,
     sd.load_data_snapshot, ns.load_news_summary, ns._load_dashboard_perf) = _saved
    restore_time()


# --- A6 market read numbers ----------------------------------------------------------------------------------
import market_read as mr  # noqa: E402
import list_read as lr  # noqa: E402

FACTS = {"session": "2026-11-17", "low": {"value": 12.5, "date": "2026-10-03"}, "change_5d_points": -3.4,
         "change_20d_points": 2.6, "index": "Nifty 500"}


def bad(text, facts=FACTS):
    return mr.unknown_numbers(text, facts)


check(bad("17% of stocks are above the line.") == ["17"] and bad("26% of stocks.") == ["26"],
      "A6: a date's day (17) or year digits (26) are not known numbers")
check(bad("Lows have held since Oct 3, and since November 17th, 2026.") == []
      and bad("The 12.5% low on 3 October 2026; the lowest since 2026-10-03.") == []
      and bad("Weakest in 2026; since October 2026.") == [],
      "A6: a date the facts hold passes, written with a month name, as ISO, or as a year / month")
check(bad("Lows since Oct 17.") == ["Oct 17"] and bad("Weakest since 2024.") == ["2024"]
      and bad("Since September 2026.") == ["September 2026"],
      "A6: a date the facts do not hold is refused")
check(bad("Breadth rose 3.4 points.") == ["+3.4"] and bad("Breadth is up 3.4 points.") == ["+3.4"]
      and bad("A +3.4 point move.") == ["+3.4"] and bad("It gained about 3.4 points.") == ["+3.4"],
      "A6: a rise against an only-negative fact is refused")
check(bad("Breadth fell 3.4 points.") == [] and bad("Breadth is down 3.4 points.") == []
      and bad("A -3.4 point move.") == [] and bad("It fell to 3.4 points of change.") == []
      and bad("It moved 3.4 points.") == [],
      "A6: 'down 3.4', 'fell 3.4', '-3.4', 'fell to 3.4' and a plain 3.4 pass for the -3.4 fact")
check(bad("A -2.6 point move.") == ["-2.6"] and bad("Down 2.6 points over 20 sessions.") == []
      and bad("Up 2.6 points over 20 sessions, in the 10-40% range.") == [],
      "A6: a minus against an only-positive fact is refused; a falling word is not (sizes are quoted positive)")
check(bad("Nifty 500 is up 2.6 points; 12.5% at its low.") == [], "A6: label numbers and rounded facts still pass")
check(bad("May 2026 saw lows.") != [] and bad("Breadth may 12.5 be read as weak.") == [],
      "A6: 'May' is a month, lower-case 'may' is not")
lfacts = {"market_read": {"headline": "Nifty lows have climbed since Oct 16."}, "x": -1.5}
check(mr.unknown_numbers("Lows have climbed since Oct 16; the list fell 1.5%.", lfacts) == []
      and mr.unknown_numbers("16% of the list.", lfacts) == ["16"],
      "A6: a date the index read writes in prose is known as a date for the list read, not as a number")
try:
    lr.validate_list_read({"headline": "Fine.", "takeaways": ["The list rose 1.5%.", "ok"], "market": None}, {**lfacts})
    check(False, "A6: list_read refuses a rise against a negative fact")
except ValueError as e:
    check("+1.5" in str(e), f"A6: list_read refuses a rise against a negative fact ({e})")


# --- A7 thinking 0 -------------------------------------------------------------------------------------------
seen = []
real_ladder = llm_util.run_model_ladder
llm_util.run_model_ladder = lambda client, prompt, tiers, config_for, **kw: (seen.append(config_for(F38)) or (None, None))
try:
    mr.generate_read(None, "Nifty 500", {"session": "2026-11-17"}, [], tiers=[(F38, 0)], budget=0)
    mr.generate_read(None, "Nifty 500", {"session": "2026-11-17"}, [], tiers=[(F38, 0)], budget=None)
finally:
    llm_util.run_model_ladder = real_ladder
check(seen[0].thinking_config is not None and seen[0].thinking_config.thinking_budget == 0
      and seen[1].thinking_config.thinking_budget == mr.THINKING_BUDGET,
      "A7: the market read sends a 0 budget as 0 (and None as its default)")


class Recorder:
    def __init__(self):
        self.calls = []
        self.models = self

    def generate_content(self, model, contents, config, **kw):
        self.calls.append(config)
        return Resp('{"ok": true}')


rec = Recorder()
ms.test_call(rec, "E2", F38, 0)
check(rec.calls[0].thinking_config is not None and rec.calls[0].thinking_config.thinking_budget == 0,
      "A7: the panel's Test sends 0 as thinking_budget=0")
rec = Recorder()
ms.test_call(rec, "N1", L, None)
check(rec.calls[0].tools[0].google_search is not None and rec.calls[0].thinking_config.thinking_budget == 512,
      "A7: a search step's Test of a Gemini model is shaped like the run's call (tool + 512)")


# --- A8 Sentiment after a failed search -------------------------------------------------------------------------
def view(sentiment, source, days_old=0):
    return {"sentiment": sentiment, "news_source": source, "model_used": "gemini-3.5-flash-lite",
            "as_of": (datetime.now(timezone.utc) - timedelta(days=days_old)).strftime("%Y-%m-%d %H:%M")}


check(fe._search_failed_unknown(view("Neutral", "⚪ No Source")) and fe._search_failed_unknown(view("Unknown", "⚪ No Source")),
      "A8: a view written after a failed search is flagged whatever its label")
check(not fe._search_failed_unknown(view("Neutral", "🔍 Gemma-4-26B (Google Search)"))
      and not fe._search_failed_unknown(view("Positive", "⚪ No Source + 🎯 targeted")),
      "A8: a searched view, or one the targeted search found numbers for, is not")
prior = view("Positive", "🔍 Gemma-4-26B (Google Search)", days_old=1)
saved = []
_fe = (fe.load_fundamentals, fe.generate_fundamental_view, fe.save_fundamentals, llm_util.make_client)
fe.load_fundamentals = lambda: {"ACME": prior}
fe.generate_fundamental_view = lambda client, row, is_retry=True: view("Neutral", "⚪ No Source")
fe.save_fundamentals = lambda data: saved.append(data)
llm_util.make_client = lambda *a, **k: object()
try:
    out = fe.analyze_single_ticker_sentiment("ACME", {"ticker": "ACME"}, "k")
finally:
    fe.load_fundamentals, fe.generate_fundamental_view, fe.save_fundamentals, llm_util.make_client = _fe
check(out is None and saved == [], "A8: Neutral + No Source over a 1-day-old Positive -> the prior is kept, nothing saved")
import refresh_fundamentals as rf  # noqa: E402
inc, detail = rf._apply_result({"ACME": prior}, "ACME", view("Neutral", "⚪ No Source"), prior, 1.0)
check(inc == 1 and "keeping prior" in detail, f"A8: the nightly job keeps the prior too ({detail})")


# --- A9 dead code ----------------------------------------------------------------------------------------------
check(not any(hasattr(ms, n) for n in ("is_custom", "skipping_38", "with_skip_38", "skipped_38_in"))
      and not hasattr(ns, "ticker_window_date"),
      "A9: the unused helpers are gone")
check(ms.skipped_models({"ai_models": {ms.SKIP_38_KEY: True}}) == {F38}
      and F38 not in [m for m, *_ in ms.tiers("R1", {"ai_models": {ms.SKIP_38_KEY: True}})],
      "A9: the old skip_flash_38 key is still read through skipped_models")
src = (REPO / "llm_util.py").read_text()
check("is_auth_error(" not in src, "A9: is_auth_error (every 401/403 a dead key) is gone")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
