"""Which models every AI step calls, how many times each, and at what thinking budget.

The sidebar's "Settings · Models" edits these (owner, 2026-10-31); the scheduled jobs and the
dashboard's re-analyze buttons read them, so a change applies from each pipeline's next run.
One table, STEPS, names every place the codebase calls a model:

    N1 News grounded search          N2 News significance filter        N3 News collation
    E1 Expert Take search            E2 Expert Take write-up
    F1 Sentiment broad search        F2 Sentiment targeted-retry search F3 Sentiment reasoning
    R1 Market read                  R2 Market read, invested lists

A step's CHAIN is an ordered list of {"model", "attempts", "timeout"}: each model is tried `attempts`
times (on a freshly rotated key, llm_util's `avoid_key`) before the next one, and a call that has not
answered within `timeout` seconds (200 by default and at most; owner, 2026-11-01) fails and the ladder
moves on. Every call is SEQUENTIAL (owner, 2026-11-01): a try starts only after the one before it has
ended, the SDK itself ends a timed-out request (llm_util._with_request_timeout), a model is finished with
before the next starts (no alternating between two models, no second pass), and the wait before each
retry is exponential over the whole ladder -- 2, 4, 8, 16, 30, 30... s (llm_util.retry_wait). The
defaults:

    search      gemma-26b x3                                  (llm_util.same_model_tiers)
    reasoning   3.5-flash-lite x2, then gemma-26b x1          (llm_util.standard_tiers)
    collation   3.7-flash x2, 3.6-flash x2, then 3.5-flash-lite x2
    market read 3.8-flash x2, then 3.6-flash x2 (no flash-lite: owner, 2026-10-31); the invested
                lists' read (R2) is the same

The settings that used to be edited by the News tab's and AI Analysis Controls' pickers
(news_reasoning_model, expert_thinking_budget, ...) are still honored when a step has no saved
chain, so a choice made before this module existed keeps applying. A saved chain lives in
settings.json under `ai_models` ({step: {"chain": [...], "budget": ...}}), the model list the
panel offers under `ai_model_repository`; both are declared in stock_data.DEFAULT_SETTINGS (an
undeclared key is dropped on load) and are not calculation settings (the "ai_" prefix).

Stdlib only here; the one real call (test_call) imports the Gemini SDK when it runs.
"""

import copy
import re
import time

import llm_util

GEMMA_26B = "models/gemma-4-26b-a4b-it"
FLASH_LITE = "models/gemini-3.5-flash-lite"
FLASH_36 = "models/gemini-3.6-flash"
FLASH_37 = "models/gemini-3.7-flash"
FLASH_38 = "models/gemini-3.8-flash"

MAX_ATTEMPTS = 10
# Seconds one call may take before the ladder gives up on it (owner, 2026-11-01: 200 for every model,
# then on to the next; it was 150). Each call's own SDK request timeout is set to it, so the call is
# ended, not just abandoned, when it runs out (llm_util._with_request_timeout).
DEFAULT_TIMEOUT_S = 200
MIN_TIMEOUT_S = 10
MAX_TIMEOUT_S = 200
# What the budget picker offers. 0 = no thinking. Collation clamps to its own band
# (news_summary._collation_thinking_budget), so it only offers that band.
BUDGET_CHOICES = [0, 1024, 2048, 4096, 8192, 16384]
COLLATION_BUDGET_CHOICES = [4096, 8192]

# kind: search (grounded Google search, no thinking), reasoning/collation/read (JSON or text out,
# a thinking budget). A chain always runs in order, one try after another (see the module docstring);
# the alternating "pairs" shape collation used to have was removed 2026-11-01 (owner).
# Owner, 2026-11-01: 3.8-flash and 3.6-flash twice each wherever they are in a chain, 3.5-flash-lite twice.
STEPS = {
    "N1": {"group": "News digest", "label": "News · grounded search", "kind": "search",
           "chain": [(GEMMA_26B, 3)], "budget": None},
    "N2": {"group": "News digest", "label": "News · significance filter (per stock)", "kind": "reasoning",
           "chain": [(FLASH_LITE, 2), (GEMMA_26B, 1)], "budget": 8192},
    "N3": {"group": "News digest", "label": "News · collation (one per watchlist)", "kind": "collation",
           "chain": [(FLASH_37, 2), (FLASH_36, 2), (FLASH_LITE, 2)], "budget": 8192},
    "E1": {"group": "Expert Take", "label": "Expert Take · grounded search", "kind": "search",
           "chain": [(GEMMA_26B, 3)], "budget": None},
    "E2": {"group": "Expert Take", "label": "Expert Take · write-up", "kind": "reasoning",
           "chain": [(FLASH_LITE, 2), (GEMMA_26B, 1)], "budget": 8192},
    "F1": {"group": "Sentiment", "label": "Sentiment · broad search", "kind": "search",
           "chain": [(GEMMA_26B, 3)], "budget": None},
    "F2": {"group": "Sentiment", "label": "Sentiment · targeted-retry search", "kind": "search",
           "chain": [(GEMMA_26B, 3)], "budget": None},
    "F3": {"group": "Sentiment", "label": "Sentiment · reasoning (both passes)", "kind": "reasoning",
           "chain": [(FLASH_LITE, 2), (GEMMA_26B, 1)], "budget": 8192},
    "R1": {"group": "Market read", "label": "Market read · the index charts' summary", "kind": "read",
           "chain": [(FLASH_38, 2), (FLASH_36, 2)], "budget": 4096},
    "R2": {"group": "Market read", "label": "Market read · invested lists and takeaways", "kind": "read",
           "chain": [(FLASH_38, 2), (FLASH_36, 2)], "budget": 4096},
}

# The settings.json keys each step's model and budget were edited under before the sidebar panel.
LEGACY = {
    "N1": {"model": "news_search_model"},
    "N2": {"model": "news_reasoning_model", "budget": "news_reasoning_budget"},
    "N3": {"model": "news_collation_model", "fallback": "news_collation_fallback_model",
           "budget": "news_collation_thinking_budget", "last": "news_reasoning_model"},
    "E2": {"model": "expert_reasoning_model", "budget": "expert_thinking_budget"},
    "F3": {"model": "sentiment_reasoning_model", "budget": "sentiment_thinking_budget"},
}

DEFAULT_REPOSITORY = [
    {"id": FLASH_38, "label": "Gemini 3.8 Flash", "supports_thinking": True, "default_budget": 4096},
    {"id": FLASH_37, "label": "Gemini 3.7 Flash", "supports_thinking": True, "default_budget": 4096},
    {"id": FLASH_36, "label": "Gemini 3.6 Flash", "supports_thinking": True, "default_budget": 4096},
    {"id": "models/gemini-3.5-flash", "label": "Gemini 3.5 Flash", "supports_thinking": True, "default_budget": 4096},
    {"id": FLASH_LITE, "label": "Gemini 3.5 Flash Lite", "supports_thinking": True, "default_budget": 4096},
    {"id": GEMMA_26B, "label": "Gemma 4 26B (no thinking)", "supports_thinking": False, "default_budget": None},
]


def full_id(model):
    """'gemini-3.5-flash' and 'models/gemini-3.5-flash' are the same model; the chains use the second."""
    model = (model or "").strip()
    return model if not model or model.startswith("models/") else f"models/{model}"


def short(model):
    return (model or "").split("/")[-1]


def normalize_chain(raw):
    """A clean [{"model", "attempts"}] list from a saved or typed one; None when nothing usable."""
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("model"), str) or not item["model"].strip():
            continue
        try:
            attempts = int(item.get("attempts", 1))
        except (TypeError, ValueError):
            attempts = 1
        out.append({"model": full_id(item["model"]), "attempts": max(1, min(MAX_ATTEMPTS, attempts)),
                    "timeout": _clean_timeout(item.get("timeout"))})
    return out or None


def _clean_timeout(value):
    try:
        return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


def _entries(pairs):
    """[{"model", "attempts", "timeout"}] from the (model, attempts) pairs the defaults and the old keys give."""
    return [{"model": m, "attempts": n, "timeout": DEFAULT_TIMEOUT_S} for m, n in pairs]


def _default_chain(step):
    return _entries(STEPS[step]["chain"])


def _legacy(step, settings):
    """(chain, budget) from the pre-panel settings keys when they differ from the defaults, else None."""
    keys = LEGACY.get(step)
    if not keys:
        return None
    spec = STEPS[step]
    default_chain = spec["chain"]
    model = full_id(settings.get(keys["model"])) or default_chain[0][0]
    budget = settings.get(keys.get("budget")) if keys.get("budget") else None
    if budget is None:
        budget = spec["budget"]
    if isinstance(budget, str) and budget.isdigit():
        budget = int(budget)
    if step == "N1":
        # The old loop: three tries on the configured model, plus one on the default when it differs.
        chain = [(model, default_chain[0][1])] + ([(default_chain[0][0], 1)] if model != default_chain[0][0] else [])
    elif step == "N3":
        fallback = full_id(settings.get(keys["fallback"])) or default_chain[1][0]
        last = full_id(settings.get(keys["last"])) or default_chain[2][0]
        chain = [(model, 2), (fallback, 2)] + ([(last, 2)] if last not in (model, fallback) else [])
    else:
        # standard_tiers(model, gemma): the model twice, then gemma once (just twice when it IS gemma).
        fallback = default_chain[1][0]
        chain = [(model, 2)] + ([(fallback, 1)] if model != fallback else [])
    if chain == list(default_chain) and budget == spec["budget"]:
        return None
    return _entries(chain), budget


def resolve(step, settings=None):
    """{"chain", "budget", "source"} for a step: the saved chain, else the old settings keys, else today's default.
    `source` is "saved", "legacy" or "default"."""
    settings = settings if settings is not None else _current_settings()
    spec = STEPS[step]
    saved = (settings.get("ai_models") or {}).get(step)
    if isinstance(saved, dict):
        chain = normalize_chain(saved.get("chain"))
        if chain:
            budget = saved.get("budget", spec["budget"])
            return {"chain": chain, "budget": _clean_budget(step, budget), "source": "saved"}
    old = _legacy(step, settings)
    if old:
        return {"chain": old[0], "budget": _clean_budget(step, old[1]), "source": "legacy"}
    return {"chain": _default_chain(step), "budget": spec["budget"], "source": "default"}


def _clean_budget(step, value):
    if STEPS[step]["kind"] == "search":
        return None
    if isinstance(value, bool):
        return STEPS[step]["budget"]
    if isinstance(value, str) and not value.isdigit():
        return value          # a Gemma-style level kept from the old picker; the pipelines pass it through
    try:
        return int(value)
    except (TypeError, ValueError):
        return STEPS[step]["budget"]


def _current_settings():
    from stock_data import load_settings
    return load_settings()


def budget(step, settings=None):
    return resolve(step, settings)["budget"]


def tiers(step, settings=None):
    """The (model, seconds to wait first, call timeout) rungs llm_util.run_model_ladder walks, from the step's chain."""
    return build_tiers(resolve(step, settings)["chain"])


def build_tiers(chain):
    """(model, seconds to wait first, call timeout) for each try, in the order they are made: one model's
    tries, then the next model's, the wait before each retry exponential over the whole chain
    (llm_util.retry_wait, read at call time)."""
    out, n = [], 0
    for entry in chain:
        for _ in range(entry["attempts"]):
            out.append((entry["model"], llm_util.retry_wait(n), entry["timeout"]))
            n += 1
    return out


def describe(chain):
    """'gemma-4-26b-a4b-it ×3 → gemini-3.5-flash-lite' for the steps list; a timeout is named only
    when it is not the default."""
    return " → ".join(short(e["model"]) + (f" ×{e['attempts']}" if e["attempts"] > 1 else "")
                      + (f" @{e['timeout']}s" if e.get("timeout", DEFAULT_TIMEOUT_S) != DEFAULT_TIMEOUT_S else "")
                      for e in chain)


def is_custom(step, settings=None):
    return resolve(step, settings)["source"] != "default"


# --- saving ----------------------------------------------------------------------------------------

def with_step(settings, step, chain, budget):
    """The `ai_models` value with `step` set to `chain`/`budget` (ValueError for an empty chain)."""
    clean = normalize_chain(chain)
    if not clean:
        raise ValueError("a step needs at least one model")
    models = copy.deepcopy(settings.get("ai_models") or {})
    models[step] = {"chain": clean, "budget": _clean_budget(step, budget)}
    return models


def reset_changes(settings, step):
    """The settings keys that put `step` back to today's default: its saved chain dropped and the
    old keys it used to be edited under set back to their defaults."""
    models = copy.deepcopy(settings.get("ai_models") or {})
    models.pop(step, None)
    changes = {"ai_models": models}
    keys = LEGACY.get(step) or {}
    spec = STEPS[step]
    defaults = {"model": spec["chain"][0][0], "budget": spec["budget"]}
    if step == "N3":
        defaults["fallback"] = spec["chain"][1][0]
    for field, key in keys.items():
        if field in defaults:
            changes[key] = defaults[field]
    return changes


# --- the model repository ----------------------------------------------------------------------------

def repository(settings=None):
    settings = settings if settings is not None else _current_settings()
    saved = settings.get("ai_model_repository")
    out = []
    if isinstance(saved, list):
        for item in saved:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
                mid = full_id(item["id"])
                thinks = bool(item.get("supports_thinking", "gemma" not in mid))
                out.append({"id": mid, "label": str(item.get("label") or short(mid)), "supports_thinking": thinks,
                            "default_budget": item.get("default_budget", 4096 if thinks else None)})
    return out or [dict(m) for m in DEFAULT_REPOSITORY]


def options_for(settings, chain):
    """Model ids the picker offers: the repository plus any model a chain already uses."""
    ids = [m["id"] for m in repository(settings)]
    for entry in chain:
        if entry["model"] not in ids:
            ids.append(entry["model"])
    return ids


def supports_thinking(settings, model):
    for m in repository(settings):
        if m["id"] == model:
            return m["supports_thinking"]
    return "gemma" not in model


# --- one real call ---------------------------------------------------------------------------------

TEST_TIMEOUT_S = 60


def test_call(client, step, model, budget=None, timeout=TEST_TIMEOUT_S):
    """(ok, detail, seconds) for one real call on `model`, shaped like the step's own calls: a grounded
    search for a search step, a small JSON answer at the step's thinking budget for the others."""
    from google.genai import types
    kind = STEPS[step]["kind"] if step in STEPS else "reasoning"
    t0 = time.time()
    try:
        if kind == "search":
            prompt = "Search the web: what is the capital of France? Answer in one short sentence."
            config = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())])
        else:
            prompt = 'Reply with exactly this JSON and nothing else: {"ok": true}'
            kwargs = {"response_mime_type": "application/json"}
            if "gemma" not in model and budget not in (None, 0):
                if isinstance(budget, str):
                    kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=budget)
                else:
                    kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=int(budget))
            config = types.GenerateContentConfig(**kwargs)
        resp = llm_util.generate_with_timeout(client, model, prompt, config, timeout=timeout)
        text = (resp.text or "").strip()
        if not text:
            return False, "empty reply", time.time() - t0
        if kind != "search":
            llm_util.json_object(re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip())
        return True, text[:80].replace("\n", " "), time.time() - t0
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:140]}", time.time() - t0
