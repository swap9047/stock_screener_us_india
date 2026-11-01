"""The sidebar's Settings · Models (2026-10-31), offline: invented settings and fake clients.

  S1  with nothing saved, every step's rungs are EXACTLY the ladders the pipelines built
      before the panel existed (llm_util's own builders are the expectation);
  S2  the settings the old pickers wrote (news_reasoning_model, expert_thinking_budget, ...)
      still decide a step until it is saved in the panel;
  S3  a saved chain: each model's tries, in order, one call after another (no alternating between
      two models), each retry on another key after an exponential wait; a saved chain beats the
      old keys; junk is ignored;
  S4  saving and Default (the old keys go back to their defaults too), the model repository,
      and the new keys surviving settings.json's load/save and not counting as calculation settings;
  S5  Test: a grounded search for a search step, a thinking budget on Gemini only, a failure
      reported rather than raised;
  S7  a timeout per model (200 s by default and at most, 10-200): the rungs carry it, a saved one is kept and
      clamped, the ladder gives each call its own, the stage-1 search loop too, the panel shows it
      only when it is not the default;
  S6  the pipelines take their rungs from the chain: search (news, Expert Take, both Sentiment
      searches), the news filter and collation, the market read.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
TMP = tempfile.mkdtemp(prefix="model_settings_")
sys.path.insert(0, REPO)
os.chdir(TMP)

import llm_util
import market_read
import model_settings as ms
import stock_data

# settings.json is written beside stock_data.py by default -- point it at the throwaway folder, or this
# check would leave a saved chain in the repo's own settings for every other check to read.
stock_data.SETTINGS_FILE = os.path.join(TMP, "settings.json")

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


llm_util.RETRY_BACKOFF_SECONDS = 5


def two(rungs):
    """(model, wait) from (model, wait, timeout): llm_util's own builders have no timeout."""
    return [(r[0], r[1]) for r in rungs]


def timeouts(rungs):
    return [r[2] for r in rungs]

G, L, F36, F37, F38 = ms.GEMMA_26B, ms.FLASH_LITE, ms.FLASH_36, ms.FLASH_37, ms.FLASH_38
CLEAN = {"ai_models": {}}

# --- S1 defaults = the old ladders -----------------------------------------------------------
for step in ("N1", "E1", "F1", "F2"):
    check(two(ms.tiers(step, CLEAN)) == llm_util.same_model_tiers(G), f"S1: {step} is three tries on Gemma, as same_model_tiers built it")
for step in ("N2", "E2", "F3"):
    check(two(ms.tiers(step, CLEAN)) == llm_util.standard_tiers(L, G), f"S1: {step} is flash-lite twice then Gemma, as standard_tiers built it")
want = llm_util.sequential_tiers([(F37, 2), (F36, 2), (L, 2)])
check(two(ms.tiers("N3", CLEAN)) == want and [t[0] for t in ms.tiers("N3", CLEAN)] == [F37, F37, F36, F36, L, L],
      f"S1: N3 is 3.7, 3.6 and flash-lite twice each, one after the other (owner, 2026-11-01): {ms.tiers('N3', CLEAN)}")
want = llm_util.sequential_tiers([(F38, 2), (F36, 2)])
check(two(ms.tiers("R1", CLEAN)) == want, "S1: R1 is 3.8-flash x2, then 3.6-flash x2 (no flash-lite)")
check([t[0] for t in ms.tiers("R2", CLEAN)] == [F38] * 2 + [F36] * 2, f"S1: R2 is 3.8-flash x2, then 3.6-flash x2 (owner, 2026-11-01): {[t[0][-9:] for t in ms.tiers('R2', CLEAN)]}")
check(llm_util.RETRY_BACKOFF_SECONDS == 5 and [llm_util.retry_wait(n) for n in range(7)] == [0, 5, 10, 20, 30, 30, 30],
      "S1: the wait before a retry doubles from the base and stops at 30 s (the base is read at call time)")
llm_util.RETRY_BACKOFF_SECONDS = 2
check([llm_util.retry_wait(n) for n in range(7)] == [0, 2, 4, 8, 16, 30, 30], "S1: from the real base, 2 s: 2, 4, 8, 16, 30, 30")
llm_util.RETRY_BACKOFF_SECONDS = 5
check([ms.budget(s, CLEAN) for s in ("N1", "N2", "N3", "E2", "F3", "R1")] == [None, 8192, 8192, 8192, 8192, 4096],
      "S1: the default thinking budgets (none for a search)")
check(all(ms.resolve(s, CLEAN)["source"] == "default" for s in ms.STEPS) and len(ms.STEPS) == 10,
      "S1: ten steps, none custom")

# --- S2 the old keys still decide ------------------------------------------------------------------
old = {"ai_models": {}, "news_reasoning_model": "gemini-3.5-flash-lite", "news_reasoning_budget": 8192}
check(ms.resolve("N2", old)["source"] == "default", "S2: the old bare model id equals the default")
old = {"news_reasoning_model": G, "news_reasoning_budget": "HIGH"}
check(two(ms.tiers("N2", old)) == llm_util.standard_tiers(G, G) and ms.budget("N2", old) == "HIGH"
      and ms.resolve("N2", old)["source"] == "legacy", "S2: a Gemma filter model and its level carry over")
old = {"expert_reasoning_model": F36, "expert_thinking_budget": 4096}
check(two(ms.tiers("E2", old)) == llm_util.standard_tiers(F36, G) and ms.budget("E2", old) == 4096
      and two(ms.tiers("F3", old)) == two(ms.tiers("F3", CLEAN)), "S2: Expert Take's old model and budget carry over, Sentiment's don't change")
old = {"sentiment_reasoning_model": "models/gemini-3.5-flash", "sentiment_thinking_budget": "2048"}
check(two(ms.tiers("F3", old)) == llm_util.standard_tiers("models/gemini-3.5-flash", G) and ms.budget("F3", old) == 2048,
      "S2: a budget saved as text reads as a number")
old = {"news_search_model": F36}
check(two(ms.tiers("N1", old)) == llm_util.sequential_tiers([(F36, 3), (G, 1)]), "S2: another search model keeps its extra rung on the default, as before")
old = {"news_collation_model": F36, "news_collation_fallback_model": F37, "news_collation_thinking_budget": 4096,
       "news_reasoning_model": G}
check(two(ms.tiers("N3", old)) == llm_util.sequential_tiers([(F36, 2), (F37, 2), (G, 2)])
      and ms.budget("N3", old) == 4096, "S2: collation's old models, budget and last resort (the filter model) carry over")

# --- S3 a saved chain ------------------------------------------------------------------------------
saved = {"ai_models": {"E2": {"chain": [{"model": F38, "attempts": 3}, {"model": "gemini-3.6-flash", "attempts": 1},
                                         {"model": L, "attempts": 2}], "budget": 2048}},
         "expert_reasoning_model": G}
r = ms.resolve("E2", saved)
check(r["source"] == "saved" and r["budget"] == 2048 and [e["model"] for e in r["chain"]] == [F38, F36, L],
      "S3: a saved chain wins over the old key; a bare model id gets its models/ prefix")
check(two(ms.tiers("E2", saved)) == [(F38, 0), (F38, 5), (F38, 10), (F36, 20), (L, 30), (L, 30)],
      f"S3: each model's tries in a row, the first at once, every retry after a wait that doubles over the whole ladder (a model switch too): {ms.tiers('E2', saved)}")
seq = {"ai_models": {"N3": {"chain": [{"model": F37, "attempts": 3}, {"model": F36, "attempts": 1}, {"model": L, "attempts": 2}], "budget": 4096}}}
check(two(ms.tiers("N3", seq)) == [(F37, 0), (F37, 5), (F37, 10), (F36, 20), (L, 30), (L, 30)],
      f"S3: collation is a plain sequence too -- a model is finished before the next starts, no alternating, no second pass: {ms.tiers('N3', seq)}")
junk = {"ai_models": {"N2": {"chain": [{"model": "", "attempts": 2}, {"attempts": 3}, "x", {"model": G, "attempts": "many"},
                                        {"model": L, "attempts": 99}], "budget": 4096}}}
check([(e["model"], e["attempts"]) for e in ms.resolve("N2", junk)["chain"]] == [(G, 1), (L, ms.MAX_ATTEMPTS)],
      "S3: entries without a model are dropped, bad tries read as 1, too many are capped")
check(ms.resolve("N2", {"ai_models": {"N2": {"chain": []}}})["source"] == "default"
      and ms.resolve("N2", {"ai_models": {"N2": "nonsense"}})["source"] == "default"
      and ms.resolve("N1", {"ai_models": {"N1": {"chain": [{"model": G, "attempts": 2}], "budget": 8192}}})["budget"] is None,
      "S3: an empty or malformed saved chain falls back; a search never has a budget")
check(ms.describe(ms.resolve("N3", CLEAN)["chain"]) == "gemini-3.7-flash ×2 → gemini-3.6-flash ×2 → gemini-3.5-flash-lite ×2",
      "S3: the table text names each model and its tries")

# --- S4 saving, Default, repository, plumbing ------------------------------------------------------
settings = stock_data.load_settings()
check(settings["ai_models"] == {} and settings["ai_model_repository"] == [], "S4: both keys are declared (an undeclared one is dropped on load)")
models = ms.with_step(settings, "E2", [{"model": F38, "attempts": 2}], 4096)
stock_data.save_settings({"ai_models": models})
again = stock_data.load_settings()
check(again["ai_models"]["E2"]["chain"] == [{"model": F38, "attempts": 2, "timeout": 200}] and ms.resolve("E2", again)["source"] == "saved",
      "S4: a saved step survives settings.json's save and load")
stock_data.save_settings({"sma_length": again.get("sma_length", 200)})
check(stock_data.load_settings()["ai_models"] == again["ai_models"], "S4: the Settings dialog's own save keeps it")
check("ai_models" not in stock_data.calc_settings(again) and "ai_model_repository" not in stock_data.calc_settings(again),
      "S4: they are not calculation settings (saving a chain never makes the snapshot stale)")
try:
    ms.with_step(settings, "E2", [], 4096)
    check(False, "S4: an empty chain cannot be saved")
except ValueError:
    check(True, "S4: an empty chain cannot be saved")
changes = ms.reset_changes({"ai_models": {"E2": {"chain": [{"model": F38, "attempts": 2}]}, "N2": {"chain": [{"model": G, "attempts": 1}]}},
                            "expert_reasoning_model": F36}, "E2")
check(changes["ai_models"] == {"N2": {"chain": [{"model": G, "attempts": 1}]}} and changes["expert_reasoning_model"] == L
      and changes["expert_thinking_budget"] == 8192, "S4: Default drops that step only and puts its old keys back too")
check("news_reasoning_model" not in ms.reset_changes({}, "N3") and ms.reset_changes({}, "N3")["news_collation_model"] == F37,
      "S4: resetting collation leaves the filter's model alone (it is only collation's last resort)")
repo = ms.repository(CLEAN)
check([m["id"] for m in repo] == [F38, F37, F36, "models/gemini-3.5-flash", L, G] and not repo[-1]["supports_thinking"],
      "S4: the default repository, Gemma without thinking")
custom = {"ai_model_repository": [{"id": "gemini-9-flash", "label": "Nine"}, {"id": G, "supports_thinking": False}, {"nope": 1}]}
check([m["id"] for m in ms.repository(custom)] == ["models/gemini-9-flash", G] and ms.repository(custom)[0]["supports_thinking"],
      "S4: a saved repository replaces the default, ids get their prefix, junk is dropped")
check(ms.options_for(custom, [{"model": "models/old-one", "attempts": 1}])[-1] == "models/old-one",
      "S4: a model a chain already uses is still offered after it was dropped")


# --- S5 Test ---------------------------------------------------------------------------------------
class Resp:
    def __init__(self, text):
        self.text = text


class Recorder:
    def __init__(self, reply='{"ok": true}', fail=None):
        self.calls, self.reply, self.fail = [], reply, fail
        self.models = self

    def generate_content(self, model, contents, config, **kw):
        self.calls.append((model, contents, config))
        if self.fail:
            raise self.fail
        return Resp(self.reply)


c = Recorder("Paris is the capital.")
ok, detail, secs = ms.test_call(c, "N1", G)
cfg = c.calls[0][2]
check(ok and cfg.tools and cfg.tools[0].google_search is not None and "Paris" in detail,
      "S5: a search step is tested with a grounded search")
c = Recorder()
ok, detail, secs = ms.test_call(c, "E2", F38, 2048)
cfg = c.calls[0][2]
check(ok and cfg.thinking_config.thinking_budget == 2048 and cfg.response_mime_type == "application/json",
      "S5: a reasoning step is tested with its thinking budget, as JSON")
c = Recorder()
ms.test_call(c, "E2", G, 2048)
check(c.calls[0][2].thinking_config is None, "S5: Gemma gets no thinking config")
c = Recorder(fail=RuntimeError("503 UNAVAILABLE overloaded"))
ok, detail, secs = ms.test_call(c, "R1", F38, 4096)
check(not ok and "503" in detail, "S5: a failing model is reported, not raised")
c = Recorder("not json")
check(not ms.test_call(c, "R1", F38, 0)[0], "S5: a reasoning step's test needs JSON back")

# --- S6 the pipelines use the chain -------------------------------------------------------------------
CUSTOM = {"ai_models": {
    "N1": {"chain": [{"model": F36, "attempts": 2}, {"model": G, "attempts": 1}]},
    "E1": {"chain": [{"model": F36, "attempts": 4}]},
    "F1": {"chain": [{"model": F37, "attempts": 2}]},
    "F2": {"chain": [{"model": F38, "attempts": 3}]},
    "R1": {"chain": [{"model": F36, "attempts": 2}], "budget": 2048}}}
captured = []
real_ladder = llm_util.run_model_ladder


def capture(client, prompt, tiers, config_for, **kw):
    captured.append((kw.get("label"), list(tiers), config_for("models/gemini-x")))
    return None, None


llm_util.run_model_ladder = capture
stock_data.load_settings = lambda: CUSTOM
import expert_views
import fundamentals_eval
import news_summary

try:
    expert_views.fetch_gemma_expert_news(object(), "ACME", "us_invested", "Acme", is_retry=True)
    fundamentals_eval.fetch_fundamental_news(object(), "ACME", "us_invested", "Acme", is_retry=True) \
        if hasattr(fundamentals_eval, "fetch_fundamental_news") else None
    fundamentals_eval.fetch_targeted_earnings_numbers(object(), "ACME", "Acme", "us_invested", "2026-10-20")
finally:
    llm_util.run_model_ladder = real_ladder
labels = {lab: tiers for lab, tiers, _ in captured}
check(two(labels["expert-search"]) == [(F36, 0), (F36, 5), (F36, 10), (F36, 20)], f"S6: Expert Take's search uses E1 (4 tries, the wait doubling): {labels.get('expert-search')}")
check(two(labels["targeted-earnings"]) == [(F38, 0), (F38, 5), (F38, 10)], f"S6: Sentiment's targeted search uses F2: {labels.get('targeted-earnings')}")
if "fundamental-search" in labels:
    check(two(labels["fundamental-search"]) == [(F37, 0), (F37, 5)], f"S6: Sentiment's broad search uses F1: {labels['fundamental-search']}")

custom_tiers = [("models/a", 0), ("models/a", 5), ("models/b", 0)]
seen = []


def capture_news(client, prompt, tiers, config_for, **kw):
    seen.append((kw.get("label"), list(tiers)))
    return None, None


llm_util.run_model_ladder = capture_news
news_summary._generate_with_timeout = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503 stop"))
try:
    text, status = news_summary.filter_batch_with_reasoning(None, "**Acme (ACME)**:\nnews", ["ACME"], "us_invested", "2026-10-03",
                                                           tiers=custom_tiers)
    summary, st2 = news_summary.collate_market_summary(None, "us_invested", ["**Acme (ACME)**: a note"], as_of_date="2026-10-03",
                                                       tiers=custom_tiers)
    try:
        news_summary.fetch_single_raw_news(None, "ACME", "us_invested", "2026-10-03", tiers=[("models/a", 0)])
    except Exception:
        pass
finally:
    llm_util.run_model_ladder = real_ladder
check(("stage2", custom_tiers) in seen and ("stage3", custom_tiers) in seen,
      f"S6: the news filter and collation walk the rungs they are given: {[l for l, _ in seen]}")

read_tiers = []
llm_util.run_model_ladder = lambda client, prompt, tiers, config_for, **kw: (read_tiers.append((list(tiers), config_for("models/gemini-x"))) or (None, None))
try:
    market_read.generate_read(None, "Nifty 500", {"session": "2026-10-30"}, [], tiers=ms.tiers("R1", CUSTOM), budget=ms.budget("R1", CUSTOM))
    market_read.generate_read(None, "Nifty 500", {"session": "2026-10-30"}, [])
finally:
    llm_util.run_model_ladder = real_ladder
check(two(read_tiers[0][0]) == [(F36, 0), (F36, 5)] and read_tiers[0][1].thinking_config.thinking_budget == 2048,
      "S6: the market read uses R1's rungs and thinking budget")
check(read_tiers[1][0] == two(ms.tiers("R1", CLEAN)) and read_tiers[1][1].thinking_config.thinking_budget == 4096,
      "S6: with none given, the market read's own default is the same ladder")


# --- S7 a timeout per model ----------------------------------------------------------------------
check(ms.DEFAULT_TIMEOUT_S == 200 and ms.MAX_TIMEOUT_S == 200 and ms.MAX_TIMEOUT_S < llm_util.HTTP_TIMEOUT_SECONDS
      and llm_util.CALL_TIMEOUT_SECONDS == 200 and llm_util.SEARCH_TIMEOUT_SECONDS == 200,
      "S7: 200 s by default and at most (owner, 2026-11-01), under the client's own backstop")
check(all(set(timeouts(ms.tiers(s, CLEAN))) == {200} for s in ms.STEPS), "S7: every step's models default to 200 s")
tmo = {"ai_models": {"R1": {"chain": [{"model": F38, "attempts": 2, "timeout": 90}, {"model": F36, "attempts": 1},
                                       {"model": L, "attempts": 1, "timeout": 5000}, {"model": G, "attempts": 1, "timeout": "x"}], "budget": 4096}}}
check(timeouts(ms.tiers("R1", tmo)) == [90, 90, 200, 200, 200],
      f"S7: each model's rungs carry its own timeout; too long is capped, junk reads as the default ({timeouts(ms.tiers('R1', tmo))})")
check([e["timeout"] for e in ms.resolve("R1", {"ai_models": {"R1": {"chain": [{"model": F38, "attempts": 1, "timeout": 1}]}}})["chain"]] == [ms.MIN_TIMEOUT_S],
      "S7: too short is raised to the floor")
check(timeouts(ms.tiers("N3", seq)) == [200] * 6
      and timeouts(ms.build_tiers([{"model": F37, "attempts": 2, "timeout": 60}, {"model": F36, "attempts": 2, "timeout": 100}])) == [60, 60, 100, 100],
      "S7: each model keeps its own timeout through the sequence")
check(ms.describe(ms.normalize_chain([{"model": F38, "attempts": 4, "timeout": 90}, {"model": F36}])) == "gemini-3.8-flash ×4 @90s → gemini-3.6-flash",
      "S7: the panel names a timeout only when it is not the default")
models = ms.with_step(settings, "R1", [{"model": F38, "attempts": 2, "timeout": 75}], 4096)
check(models["R1"]["chain"] == [{"model": F38, "attempts": 2, "timeout": 75}], "S7: Save keeps it")

seen_timeouts = []
real_gwt = llm_util.generate_with_timeout


def fake_gwt(client, model, contents, config, timeout=None, avoid_key=None):
    seen_timeouts.append((model, timeout))
    raise RuntimeError("503 UNAVAILABLE")


llm_util.generate_with_timeout = fake_gwt
try:
    llm_util.run_model_ladder(None, "p", [(F38, 0, 90), (F36, 0, 40), (L, 0)], lambda m: None, label="t", timeout=120)
finally:
    llm_util.generate_with_timeout = real_gwt
check(seen_timeouts == [(F38, 90), (F36, 40), (L, 120)],
      f"S7: the ladder gives each call its rung's timeout, and the ladder's own to a rung without one ({seen_timeouts})")

seen_timeouts.clear()
news_summary._generate_with_timeout = lambda client, model, prompt, config, timeout=None, avoid_key=None: (
    seen_timeouts.append((model, timeout)) or (_ for _ in ()).throw(RuntimeError("503 stop")))
try:
    news_summary.fetch_single_raw_news(None, "ACME", "us_invested", "2026-10-03", tiers=[(F38, 0, 33), (F36, 0, 44)])
except Exception:
    pass
check(seen_timeouts == [(F38, 33), (F36, 44)], f"S7: the news search loop uses each rung's timeout too ({seen_timeouts})")

shutil.rmtree(TMP, ignore_errors=True)
print("FAILURES:", fails)
sys.exit(fails)
