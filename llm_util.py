"""Shared Gemini-call plumbing for the three AI pipelines.

`news_summary.py`, `expert_views.py` and `fundamentals_eval.py` each grew their
own copy of the same three things -- a timeout wrapper, a notion of which
failures are worth retrying, and a model ladder. The copies drifted: only the
news pipeline ever gained a same-model retry and a terminal-error gate, so a
single transient 429 permanently demoted a ticker to the Gemma fallback in the
other two. This module is the one implementation they now share.
"""

import os
import random
import re
import threading
import time

# One call's cut-off (owner, 2026-11-01: 200 s for every model call). Every ladder rung carries
# its own (model_settings), which beats this; it is what a rung with none, and the direct
# call sites, use.
CALL_TIMEOUT_SECONDS = 200

# Request timeout handed to the Gemini SDK itself (HttpOptions, milliseconds).
# Without one the SDK waits forever, which is what let a hung grounded search
# outlive the run that made it -- see generate_with_timeout.
#
# This is the CLIENT's backstop, deliberately ABOVE CALL_TIMEOUT_SECONDS. Each call also
# carries its own request timeout equal to its cut-off (_with_request_timeout), so a call
# that has timed out is already dead on the wire when the ladder starts the next one.
HTTP_TIMEOUT_SECONDS = CALL_TIMEOUT_SECONDS + 60

# Ceiling for ONE grounded-search call. Named separately from
# CALL_TIMEOUT_SECONDS so the grounded searches -- the only stage that gets
# anywhere near a timeout -- can be tuned without touching the reasoning stages.
#
# Tried at 90s and reverted. The 03:55 UTC run (120s, serial) timed out on 0.44
# calls per ticker; the 15:55 UTC run (90s) on 0.67, and queued 0.25 tickers per
# dispatch against 0.13. That looks like the tighter ceiling cutting real
# searches -- but the midday run also drew a 503 "This model is currently
# experiencing high demand", so load and ceiling moved together and the two
# cannot be separated from those logs. 120s is the setting with a full, known
# run behind it; reducing it again wants an overnight A/B at the same hour.
SEARCH_TIMEOUT_SECONDS = 200

# The wait before a retry (owner, 2026-11-01): exponential. The first retry of a stock's
# ladder waits RETRY_BACKOFF_SECONDS, each one after it twice as long, never more than
# RETRY_BACKOFF_MAX_SECONDS -- 2, 4, 8, 16, 30, 30... s. Counted over the whole ladder, so
# a switch to the next model waits too. It buys a transient 429/503 time to clear instead of
# hitting it again in 5 s, and it was a fixed 5 s (30 s in collation) before. Read at CALL
# time, so a check that sets the base to 0 gets no waits at all.
RETRY_BACKOFF_SECONDS = 2
RETRY_BACKOFF_MAX_SECONDS = 30


def retry_wait(n, base=None):
    """Seconds to wait before the n-th retry of one ladder (n = 1 is the first retry; 0 = the
    first try, no wait): base, 2 x base, 4 x base ... capped at RETRY_BACKOFF_MAX_SECONDS."""
    base = RETRY_BACKOFF_SECONDS if base is None else base
    if n <= 0 or base <= 0:
        return 0
    return min(RETRY_BACKOFF_MAX_SECONDS, base * 2 ** (n - 1))

# Failures that re-running cannot fix. Everything else (rate limits, timeouts,
# 5xx, transport errors) is worth another attempt. Without this gate an expired
# key or an exhausted daily quota burns every tier of the ladder for every
# ticker, and -- where a retry queue exists -- gets re-attempted ~100 more times
# with a 30s sleep between each.
#
# Matched as WORDS in the error text, and HTTP status codes are read from the
# exception's `code` (google-genai's APIError has one) or the message's leading
# "NNN STATUS", never as substrings. These used to include "400"/"401"/"403" as
# plain substrings, so an ordinary rate limit -- "429 RESOURCE_EXHAUSTED ...
# Please retry in 14.400561298s" -- contained "400" and was treated as
# terminal: no retry, ladder abandoned.
TERMINAL_ERROR_MARKERS = (
    "unauthorized", "permission denied", "permission_denied", "api key", "api_key_invalid",
    "invalid argument", "invalid_argument",
)
TERMINAL_STATUS_CODES = {400, 401, 403}
RETRYABLE_STATUS_CODES = {408, 409, 425, 429}   # plus every 5xx

# Errors that belong to ONE API key on EVERY model: a revoked/invalid/expired
# key, or a project with the Gemini API switched off. RotatingGeminiClient
# retires that key for the rest of the run and retries the call on another one,
# instead of letting the ladder give up on a ticker the other key could have
# served.
#
# A 403 PERMISSION_DENIED is NOT one of them any more (2026-11-22). It used to be
# (any 401/403 killed the key for every model, and 403 is terminal, so the ladder
# broke): a preview model that refuses some keys with 403 then took those keys
# out of the Gemma search too, for the whole run. A 403 is about the (key,
# model) pair -- see is_permission_denied and RotatingGeminiClient._forbidden.
KEY_INVALID_MARKERS = ("api key not valid", "api_key_invalid", "api key expired", "api_key_expired",
                       "unauthenticated", "unauthorized", "service_disabled",
                       "has not been used in project")
PERMISSION_DENIED_MARKERS = ("permission denied", "permission_denied")
_LEADING_STATUS = re.compile(r"^\s*(\d{3})\b")


def status_code(exc):
    """HTTP status of an API exception, or None."""
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    m = _LEADING_STATUS.match(str(exc))
    return int(m.group(1)) if m else None


def is_key_invalid(exc):
    """True for a failure that is about the API KEY itself, whatever the model: a 401, or an invalid,
    expired or API-disabled key (Google sends those as 400 / 403 with the reason in the text)."""
    if status_code(exc) == 401:
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in KEY_INVALID_MARKERS)


def is_permission_denied(exc):
    """True for a 403 / PERMISSION_DENIED that is not a dead key: this key may not call this MODEL."""
    if is_key_invalid(exc):
        return False
    if status_code(exc) == 403:
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in PERMISSION_DENIED_MARKERS)


class ModelExhausted(RuntimeError):
    """Every usable key is retired for this MODEL for the rest of the run: its daily quota is spent
    (a PerDay 429) or Google refuses the key the model (403). Raised by RotatingGeminiClient without
    another API call; the ladders skip every remaining rung of the model, with no wait, and go on to
    the next model (is_dead_model). `code` is 429 for a quota, 403 for a refusal, so is_retryable reads
    it as it read the error it stands for."""

    def __init__(self, model, code, reason):
        super().__init__(f"{code} {'RESOURCE_EXHAUSTED' if code == 429 else 'PERMISSION_DENIED'}: "
                         f"every usable key {reason} for {model} this run")
        self.code = code
        self.model = model


def call_with_timeout(fn, timeout, name="call"):
    """Run `fn()` on a DAEMON thread and return its result, or raise
    TimeoutError after `timeout` seconds.

    A daemon thread, not a ThreadPoolExecutor. The executor version abandoned
    its worker on timeout (`shutdown(wait=False)`) and its docstring claimed
    that stopped the job hanging -- it only moved the hang. Executor workers are
    non-daemon and `concurrent.futures` registers an atexit hook that JOINS
    them, so the process could not exit while an abandoned call was still
    blocked. Measured: the wrapper returned after its 2s timeout, main() ended,
    and the interpreter then sat for the full 25s the worker was sleeping.

    That is not theoretical. news-summary on 2026-09-17 finished its tickers at
    01:10, saved, posted to Discord -- and was killed at its 180-minute cap at
    03:09. It had 5 timed-out calls; the threads ran concurrently, so the tail
    is the LONGEST hung call, and at least one held on for about two hours.

    Shared by the Gemini wrapper below and the two yfinance call sites
    (stock_data._download_with_retries, fundamentals_eval's earnings-date
    lookup), which carried their own copies of the executor form. Stdlib only,
    like everything in this module.
    """
    box = {}

    def _run():
        try:
            box["result"] = fn()
        except BaseException as e:      # noqa: BLE001 -- re-raised on the caller's thread
            box["error"] = e

    worker = threading.Thread(target=_run, name=name, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"{name} timed out after {timeout}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _with_request_timeout(config, timeout):
    """`config` with the SDK's own request timeout set to `timeout` seconds.

    The wrapper's cut-off alone only ABANDONS a slow call: its daemon thread (and the HTTP
    request under it) kept running until the client-wide backstop, 60 s later, while the
    ladder had already started the next try -- two calls to the model in flight at once.
    Giving the request the same timeout makes the SDK itself end it at the same moment (not 60 s
    later), so the calls of a ladder do not overlap (owner, 2026-11-01: all calls sequential). Imported
    lazily (llm_util must import without the SDK); anything that is not an SDK config (a
    check's stand-in) is returned as it was."""
    try:
        from google.genai import types
        http = types.HttpOptions(timeout=int(timeout * 1000))
        if config is None:
            return types.GenerateContentConfig(http_options=http)
        if hasattr(config, "model_copy"):
            return config.model_copy(update={"http_options": http})
    except Exception:
        pass
    return config


def generate_with_timeout(client, model, contents, config, timeout=CALL_TIMEOUT_SECONDS,
                          avoid_key=None, key_sink=None):
    """One generate_content call, bounded by `timeout` -- see call_with_timeout
    for why it is a daemon thread. The real defence is the SDK-level HTTP
    timeout on the client (see HTTP_TIMEOUT_SECONDS); this is the backstop for
    anything that slips past it.

    Nothing is re-wrapped here. A `except TimeoutError: raise TimeoutError(f"API
    call to {model} timed out after {timeout}s")` around this used to catch a
    timeout raised by the CALL as well as by the wrapper -- socket.timeout has
    been an alias of TimeoutError since Python 3.10 -- so an HTTP read timeout
    that failed in 2s was logged by run_model_ladder as having taken the full
    120s budget, with its own message discarded. The ladder's log line is how a
    nightly run gets diagnosed; it has to say what actually happened.

    `key_sink`, a dict, is given the key that served a call that ANSWERED (`key_sink["key"]`): a reply the
    caller then rejects (a search that did not search, SearchReplyCheck) is retried on another key, and only
    an exception carried the key before."""
    key_out = {} if hasattr(client, "key_names") else None
    extra = {}
    if key_out is not None:
        extra["_key_out"] = key_out
        if avoid_key:
            extra["_avoid_key"] = avoid_key
    config = _with_request_timeout(config, timeout)
    try:
        resp = call_with_timeout(
            lambda: client.models.generate_content(model=model, contents=contents, config=config, **extra),
            timeout, name=f"genai-{model}",
        )
        if key_sink is not None and key_out and key_out.get("key"):
            key_sink["key"] = key_out["key"]
        return resp
    except BaseException as e:
        if key_out and key_out.get("key"):
            try:
                e._gemini_key = key_out["key"]
            except Exception:
                pass          # some exceptions refuse attributes; attribution is a nicety
        raise


# Errors that are about THIS MODEL rather than the account: a wrong or retired
# model id, or one the key has no access to. They say nothing about the next
# tier, so the ladder should step past them rather than give up -- otherwise a
# single typo'd model id silently degrades the whole stage, including a
# fallback that would have worked perfectly.
MODEL_UNAVAILABLE_MARKERS = (
    "not found", "does not exist", "is not supported", "unsupported model",
    "no such model",
)


def is_model_unavailable(exc):
    if status_code(exc) == 404:
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in MODEL_UNAVAILABLE_MARKERS)


def is_dead_model(exc):
    """True when this MODEL cannot answer for the rest of the ladder, whichever key a retry would take: a
    wrong or retired id (404), every key retired for it (ModelExhausted: daily quota or 403 everywhere),
    or a 403 on a plain client. The ladders then skip the model's remaining rungs WITHOUT a wait and go
    on to the next model (2026-11-22): a retired id in an x6 chain used to cost 6 calls and ~60 s of
    waits per stock, and a model whose daily quota every key had spent still slept before each of its
    remaining rungs, only to fail at once."""
    return isinstance(exc, ModelExhausted) or is_model_unavailable(exc) or is_permission_denied(exc)


# A 500 INTERNAL that came back this fast is not the backend being slow to recover: Gemma's grounded
# search returns its 500s in ~2 s (2026-11-10/13), and the next try goes to another key anyway, so the
# exponential wait (2, 4, 8, 16, 30 s) bought nothing but minutes per stock. After one, the next rung waits
# only the base (RETRY_BACKOFF_SECONDS); a 429, a 503 or a timeout keeps the exponential wait.
FAST_500_SECONDS = 10


def next_wait(backoff, last_exc=None, last_elapsed=None):
    """Seconds to wait before a rung whose precomputed wait is `backoff`, given how the previous try
    failed (`last_exc`, None for no failure, and how long it took): none after a dead model (is_dead_model:
    there is nothing to wait out), at most the base after a fast 500, else `backoff`."""
    if not backoff:
        return 0
    if last_exc is None:
        return backoff
    if is_dead_model(last_exc):
        return 0
    if (status_code(last_exc) == 500 and last_elapsed is not None
            and last_elapsed < FAST_500_SECONDS):
        return min(backoff, RETRY_BACKOFF_SECONDS)
    return backoff


def is_retryable(exc):
    """Whether re-running the SAME call could plausibly succeed."""
    if isinstance(exc, TimeoutError):
        return True
    if is_model_unavailable(exc):
        return False          # retrying a missing model just burns time
    code = status_code(exc)
    if code is not None and (code in RETRYABLE_STATUS_CODES or code >= 500):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    if _is_quota_error(exc):
        return True
    if code in TERMINAL_STATUS_CODES:
        return False
    return not any(marker in text for marker in TERMINAL_ERROR_MARKERS)


def json_object(text):
    """json.loads that insists on a JSON OBJECT, for the ladders' on_success.

    Every structured stage asks for an object and then indexes the result like
    one. JSON mode does not guarantee the shape, and a top-level array parsed
    fine, left the ladder as a success, and then raised TypeError on
    `data["as_of"] = ...` -- outside the ladder, so no other rung was tried.
    Raising here instead makes a wrong shape what a parse error already was:
    a retryable failure that moves the ladder on."""
    import json
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object from the model, got {type(data).__name__}")
    return data


def run_model_ladder(client, prompt, tiers, config_for, label="llm", subject="",
                     timeout=CALL_TIMEOUT_SECONDS, on_success=None):
    """Try each (model, backoff[, timeout]) tier in order until one answers.

    `tiers` is a list of (model_id, sleep_seconds_before_attempt) or, since the sidebar's per-model
    timeout (model_settings, 2026-10-31), (model_id, sleep_seconds_before_attempt, call_timeout_seconds):
    a tier's own timeout beats the ladder's `timeout`, and a call that has not answered by then
    fails and the ladder moves on. `config_for`
    maps a model id to its GenerateContentConfig -- a callable because the
    thinking-budget config is only valid on non-Gemma models. `on_success`, if
    given, converts the raw response into the return value.

    Returns (result, model_used) on success, or (None, None) once the ladder is
    exhausted. Stops early on a terminal (non-retryable) error rather than
    burning the remaining tiers.
    """
    # The key the previous rung failed on, so the next one picks a different one.
    # With the grounded searches now running every rung on ONE model, the key is
    # the only thing that varies between attempts.
    avoid_key = None
    # A reply on_success held back for one recheck (RecheckReply): used when no later rung answers.
    held = None
    # Models that cannot answer for the rest of this ladder (is_dead_model): their rungs are skipped.
    dead = set()
    # How the previous try failed, for the wait before the next (next_wait).
    last_exc, last_elapsed = None, None
    for tier in tiers:
        model, backoff = tier[0], tier[1]
        tier_timeout = tier[2] if len(tier) > 2 and tier[2] else timeout
        if model in dead:
            continue
        wait = next_wait(backoff, last_exc, last_elapsed)
        if wait:
            time.sleep(wait)
        last_exc, last_elapsed = None, None
        served = {}
        started = time.monotonic()
        try:
            resp = generate_with_timeout(client, model, prompt, config_for(model), timeout=tier_timeout,
                                         avoid_key=avoid_key, key_sink=served)
            return (on_success(resp) if on_success else resp), model
        except RecheckReply as e:
            held = held or (e.result, model)
            avoid_key = served.get("key")
            via = f" via {avoid_key}" if avoid_key else ""
            print(f"  [{label} {model} rechecking{via}] {subject}: {e}")
            continue
        except Exception as e:
            # The KEY as well as the model: a 500 says nothing about which key
            # served it, and without this a run log could not tell a model-wide
            # outage from one bad key. Read off the EXCEPTION, which
            # generate_with_timeout tags with the key that made the call -- never
            # off client.last_key_name, which another worker may have moved on.
            # Absent on a plain genai.Client, and then nothing is claimed. A reply
            # on_success rejected carries no key: the call's own key (`served`) then.
            key = getattr(e, "_gemini_key", None) or served.get("key")
            avoid_key = key
            last_exc, last_elapsed = e, time.monotonic() - started
            via = f" via {key}" if key else ""
            print(f"  [{label} {model} failed{via}] {subject}: {e}")
            if is_dead_model(e):
                # This model is wrong/retired/refused, or out of quota on every key --
                # the next MODEL may still be fine, so skip this one's remaining rungs
                # (no wait) instead of abandoning the ladder or re-trying a dead model.
                dead.add(model)
                continue
            if not is_retryable(e):
                # Bad request or every key failing auth: no model will work. Stop.
                break
    return held if held else (None, None)


# --- grounded search replies that did not search (owner, 2026-11-15) ---------------------------------
# Gemma 26b, the search model, often answers WITHOUT searching: an empty reply, or "No news found." /
# "Empty Response", with no search in its grounding metadata. On 2026-11-13 it did so for 4 of 10 stocks
# that another model found real news for the same day. It also never records a search when a stock is
# genuinely quiet (7 of 7 empty replies over a weekend window), so a missing search alone cannot mean
# failure: the reply gets ONE more try on another key, and a second such reply is taken as quiet
# (owner's choice). Leftover tool-call text ('{"action": "google_search", ...}' instead of an answer)
# is never an answer and always moves the ladder on.
_UNSEARCHED_NOTHING_RE = re.compile(
    r"^[\W_]*(empty response|no (\w+ ){0,2}(news|updates|announcements|items|information|results)"
    r"|nothing|none|n/?a)\b", re.I)
_TOOL_CALL_RE = re.compile(
    r'"(action|tool|tool_name|tool_code|name|function)"\s*:\s*"(google_search|search|web_search)', re.I)
UNSEARCHED_MAX_CHARS = 200


class RecheckReply(Exception):
    """Raised by a reply check for a reply worth one more try; carries the reply (`result`), which
    run_model_ladder returns if no later rung answers. Not an error: never counted as a failure."""

    def __init__(self, reason, result):
        super().__init__(reason)
        self.result = result


class UnusableSearchReply(RuntimeError):
    """A grounded-search reply that is not an answer (tool-call text): a failed, retryable try."""


def reply_text(resp):
    try:
        return (getattr(resp, "text", None) or "").strip()
    except Exception:
        return ""


def search_query_count(resp):
    """How many web searches the reply's grounding metadata records (0 when it records none)."""
    try:
        gm = resp.candidates[0].grounding_metadata if resp.candidates else None
        return len(getattr(gm, "web_search_queries", None) or []) if gm else 0
    except Exception:
        return 0


def looks_like_tool_call(text):
    """True for leftover tool-call text: a JSON object naming the search tool, not an answer."""
    t = str(text or "").strip()
    head = t.lstrip("`").removeprefix("json").lstrip()[:300]
    return head.startswith("{") and bool(_TOOL_CALL_RE.search(head))


def unsearched_nothing(resp):
    """True when the reply recorded no search and says nothing: empty, or a short "no news" line."""
    if search_query_count(resp):
        return False
    text = reply_text(resp)
    return not text or (len(text) <= UNSEARCHED_MAX_CHARS and bool(_UNSEARCHED_NOTHING_RE.match(text)))


class SearchReplyCheck:
    """One search's reply check (a ladder's on_success, or called by hand): returns the reply, raises
    UnusableSearchReply for tool-call text, and RecheckReply the first `rechecks` times a reply recorded no
    search and says nothing. Make a new one per search: it counts."""

    def __init__(self, rechecks=1):
        self.rechecks = rechecks

    def __call__(self, resp):
        if looks_like_tool_call(reply_text(resp)):
            raise UnusableSearchReply("returned tool-call text instead of an answer")
        if self.rechecks > 0 and unsearched_nothing(resp):
            self.rechecks -= 1
            raise RecheckReply("said nothing without searching: one more try on another key", resp)
        return resp


def sequential_tiers(chain, base=None):
    """The rungs of a chain [(model, attempts), ...] in order, one call after another: a model's
    tries, then the next model's. Each rung is (model, seconds to wait first); the wait grows
    exponentially over the whole ladder (retry_wait), 0 before the first try. There is no
    alternating between two models and no second pass: a model is finished with before the
    next one starts (owner, 2026-11-01)."""
    out, n = [], 0
    for model, attempts in chain:
        for _ in range(attempts):
            out.append((model, retry_wait(n, base)))
            n += 1
    return out


def standard_tiers(primary, fallback):
    """The ladder every pipeline should use: the good model, the good model
    again after a backoff, then the fallback."""
    chain = [(primary, 2)]
    if fallback and fallback != primary:
        chain.append((fallback, 1))
    return sequential_tiers(chain)


def same_model_tiers(model, attempts=3, backoff=None):
    """Every rung on ONE model, each on a freshly rotated key.

    For the grounded searches. They used to end on gemma-4-31b-it, which looked
    like a fallback and was not one: it answered 0 of ~63 calls across four runs
    (2026-09-17/18), overwhelmingly 500 INTERNAL, while the 26b primary answered
    ~83%. A second Gemma on the same backend was never an independent failure
    domain. So the MODEL stops varying and the KEY varies instead -- see
    RotatingGeminiClient._pick's `avoid`.

    The trade this makes: if 26b itself goes down there is no other model to fall
    to. That is the honest position, because the model that was nominally there
    had not answered a single call in four runs.
    """
    # `backoff` is the base of the exponential wait (None: RETRY_BACKOFF_SECONDS, read at CALL
    # time, so a caller or check can change the pacing).
    return sequential_tiers([(model, max(1, attempts))], base=backoff)


# ---------------------------------------------------------------------------
# API key rotation
#
# Several Gemini keys are configured (GEMINI_API_KEY, GEMINI_API_KEY_BACKUP,
# GEMINI_API_KEY_BACKUP_B as of 2026-09-16), as repo secrets and in .env. The
# point is to divide the per-key load: a nightly run is ~250 calls against one
# project's quota, and the search stages are the slow, rate-limit-prone ones.
#
# Rotation is PER CALL, not per run or per script. A run picks a client once
# and then makes every call through it, so per-run rotation would still put a
# whole 117-ticker job on one key -- exactly the load this is meant to split.
# RotatingGeminiClient therefore stands in for the client object itself and
# chooses a key on each generate_content, which needs no changes at any of the
# call sites.
# ---------------------------------------------------------------------------

# Discovery is by NAME PREFIX: any secret or environment variable whose name
# starts with GEMINI_API_KEY is a key, whatever the suffix (_BACKUP, _BACKUP_B,
# _2, ...). It used to be a fixed list of names, which silently ignored
# GEMINI_API_KEY_BACKUP_B when that key was added -- the run kept splitting
# across two keys and nothing said so. These two names are ranked first, and
# everything else follows in name order, so rotation logs stay predictable.
KEY_NAME_PREFIX = "GEMINI_API_KEY"
PRIMARY_KEY_NAME = "GEMINI_API_KEY"
BACKUP_KEY_NAME = "GEMINI_API_KEY_BACKUP"

# How long a key that returned a quota/rate-limit error is skipped for. Without
# this, a key whose daily quota is exhausted keeps getting picked for half the
# remaining calls and fails every one of them. The pause is per (key, model), not
# per key (2026-11-23): Google counts per-minute limits per project AND model, and
# a key-wide pause let one model's 429s take the key out of every other model --
# on 2026-11-23 a search backup that answered 429 on all 123 calls also benched
# each key for 120 s for Gemma's own searches.
KEY_COOLDOWN_SECONDS = 120

# Local runs only: app.py loads .env for the dashboard, but the headless
# scripts (refresh_*.py, news_check.py, alert_check.py) had no way to see it, so
# testing key rotation locally meant exporting the keys by hand. Actions has no
# .env, and load_dotenv never overrides a variable that is already set, so this
# changes nothing in CI.
try:
    from dotenv import load_dotenv

    load_dotenv(".env")
except Exception:
    pass

QUOTA_ERROR_MARKERS = ("resource_exhausted", "resource exhausted", "quota", "rate limit", "too many requests")


def _is_quota_error(exc):
    if status_code(exc) == 429:
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(m in text for m in QUOTA_ERROR_MARKERS)


def _is_daily_quota_error(exc):
    """A 429 for a PER-DAY quota (Google names it in the error details, e.g.
    quotaId "GenerateRequestsPerDayPerProjectPerModel-FreeTier"), as opposed to
    the per-minute limits a 120 s cooldown is enough for.

    The distinction matters because a daily quota does not come back within the
    run. With only the cooldown, a key that had spent its day's quota rejoined
    the rotation every 120 s and failed again, and on 2026-10-13, -15 and -20 the
    nightly AI jobs spent hours that way before the FailureFuse stopped them.
    Matched with separators removed, so "PerDay", "per_day" and "per day" all
    count."""
    if not _is_quota_error(exc):
        return False
    squashed = re.sub(r"[^a-z]", "", f"{exc}".lower())
    return "perday" in squashed


def _key_names(st_secrets=None):
    """Every secret/env name that looks like a Gemini key, primary and backup
    first. GitHub upper-cases secret names (GEMINI_API_KEY_BACKUP_B) while a
    .env file keeps whatever case you typed, so match case-insensitively but
    return the name as configured -- that is what the rotation logs show."""
    names = []

    def consider(name):
        if isinstance(name, str) and name.upper().startswith(KEY_NAME_PREFIX) and name not in names:
            names.append(name)

    if st_secrets is not None:
        try:
            for name in st_secrets:
                consider(name)
        except Exception:
            pass   # a secrets object that will not iterate: fall back to env
    for name in os.environ:
        consider(name)

    def rank(name):
        upper = name.upper()
        return (0 if upper == PRIMARY_KEY_NAME else 1 if upper == BACKUP_KEY_NAME else 2, upper)

    return sorted(names, key=rank)


def gemini_api_keys(st_secrets=None, extra=None):
    """Every configured Gemini key as [(name, key), ...], primary first.

    Looks in a Streamlit secrets-like object first, then the environment (how
    GitHub Actions supplies them) -- the same order and shape as
    news_summary.get_gemini_api_key, which this replaces the plural half of.
    `extra` accepts a key or list of keys a caller already resolved (the
    dashboard passes what it read from st.secrets down into the analysis
    helpers). Duplicates are dropped, so passing the primary explicitly does
    not weight it double in the rotation.
    """
    found = []

    def add(name, value):
        if value and isinstance(value, str) and value not in {k for _, k in found}:
            found.append((name, value))

    # Named discovery first, so a key the caller also passed keeps its real
    # name in the rotation logs instead of showing up as "caller[0]".
    for name in _key_names(st_secrets):
        if st_secrets is not None:
            try:
                if name in st_secrets:
                    add(name, st_secrets[name])
                    continue
            except Exception:
                pass
        add(name, os.environ.get(name))

    if isinstance(extra, str):
        add("caller", extra)
    elif extra:
        for i, k in enumerate(extra):
            add(f"caller[{i}]", k)
    return found


class RotatingGeminiClient:
    """Stands in for genai.Client, picking a key at random on every call.

    Only `client.models.generate_content(...)` is proxied, because that is the
    entire surface this codebase uses (see generate_with_timeout). Anything
    else would raise AttributeError loudly rather than silently bypassing the
    rotation.

    Underlying clients are built once per key and reused -- construction is not
    free, and this is called ~250 times a night.
    """

    def __init__(self, keys):
        if not keys:
            raise ValueError("RotatingGeminiClient needs at least one API key")
        self._keys = list(keys)
        self._clients = {}
        # (key name, model) -> time the pair may be picked again (KEY_COOLDOWN_SECONDS).
        self._cooldown_until = {}
        # Keys that are dead on every model this run -- see KEY_INVALID_MARKERS.
        self._dead = set()
        # (key name, model) pairs that spent their DAILY quota this run -- see
        # _is_daily_quota_error. Per model, not per key: Google counts the daily
        # quota per project and model, so the key still serves the ladder's
        # other models.
        self._exhausted = set()
        # (key name, model) pairs Google refused with a 403 this run (is_permission_denied): the key still
        # serves every other model -- a preview model that refuses a key must not take it out of the search.
        self._forbidden = set()
        self._lock = threading.Lock()
        # Diagnostic only. NOT safe for per-call attribution: it is shared by
        # every thread, and generate_with_timeout's `_key_out` is what callers
        # must use -- see _RotatingModels.generate_content.
        self.last_key_name = None
        # Call and failure counts per key name, so a run can report how the load
        # actually split and whether one key is worse than the others. Counting
        # is separate from acting: a 500/503 is a MODEL condition and must not
        # disable or cool down the key it happened to land on -- only auth and
        # quota do that -- but it is still recorded here, because "is one key
        # failing more?" was previously unanswerable from a run log.
        self.call_counts = {name: 0 for name, _ in self._keys}
        self.failure_counts = {name: 0 for name, _ in self._keys}

    @property
    def key_names(self):
        return [name for name, _ in self._keys]

    def _client_for(self, key):
        client = self._clients.get(key)
        if client is None:
            from google import genai
            from google.genai import types
            # Imported lazily, like `genai` above: llm_util must stay importable
            # without the SDK (checks/test_gate_imports.py blocks it).
            client = genai.Client(
                api_key=key,
                http_options=types.HttpOptions(timeout=HTTP_TIMEOUT_SECONDS * 1000),
            )
            self._clients[key] = client
        return client

    def _pick(self, avoid=None, model=None):
        """A key, at random. `avoid` names one to skip if there is an
        alternative -- the ladder passes the key the previous rung just failed
        on, so a same-model retry genuinely changes something. Ignored when it
        is the only key left, because a retry on the same key still beats no
        retry. A key that spent its daily quota for `model` is never picked for
        that model again this run (None when every key has)."""
        now = time.time()
        with self._lock:
            usable = [(n, k) for n, k in self._keys if self._usable(n, model)]
            if not usable:
                return None, None
            live = [(n, k) for n, k in usable if self._cooldown_until.get((n, model), 0) <= now]
            # Every key cooling down: use them all rather than hard-failing --
            # a stale cooldown must never be the reason a run does nothing.
            choices = live or usable
            if avoid is not None:
                others = [(n, k) for n, k in choices if n != avoid]
                choices = others or choices
            name, key = random.choice(choices)
            self.call_counts[name] = self.call_counts.get(name, 0) + 1
            self.last_key_name = name
        return name, self._client_for(key)

    def _usable(self, name, model):
        """Whether key `name` may still be picked for `model` this run (call under the lock)."""
        return (name not in self._dead and (name, model) not in self._exhausted
                and (name, model) not in self._forbidden)

    def usable_count(self, model):
        with self._lock:
            return len([n for n, _ in self._keys if self._usable(n, model)])

    def _mark_forbidden(self, name, exc, model):
        with self._lock:
            first = (name, model) not in self._forbidden
            self._forbidden.add((name, model))
            left = len([n for n, _ in self._keys if self._usable(n, model)])
        if first:
            print(f"  [key rotation] {name} is refused {model} ({str(exc)[:80]}) -- retired for that model "
                  f"for the rest of this run; {left} key(s) left for it")
        return left

    def _mark_dead(self, name, exc):
        with self._lock:
            first = name not in self._dead
            self._dead.add(name)
            remaining = len([n for n, _ in self._keys if n not in self._dead])
        if first:
            print(f"  [key rotation] {name} failed authentication ({str(exc)[:80]}) -- "
                  f"disabled for this run; {remaining} key(s) left")
        return remaining

    def _mark_failure(self, name):
        with self._lock:
            self.failure_counts[name] = self.failure_counts.get(name, 0) + 1

    def usage_summary(self):
        """One line per key: calls, failures and failure rate, worst first.

        Printed at the end of each AI job. Without it the only key line in a run
        log was the startup roster, so a run with 48 model failures across 3 keys
        could not say whether they were spread evenly or concentrated on one."""
        with self._lock:
            rows = [(n, self.call_counts.get(n, 0), self.failure_counts.get(n, 0))
                    for n in self.key_names]
        rows.sort(key=lambda r: (-(r[2] / r[1]) if r[1] else 0, -r[2]))
        parts = [f"{n} {c} call(s), {f} failed ({(f / c * 100) if c else 0:.0f}%)"
                 for n, c, f in rows]
        return "  [key rotation] " + "; ".join(parts)

    def _mark_quota_error(self, name, exc=None, model=None):
        if exc is not None and _is_daily_quota_error(exc):
            with self._lock:
                self._exhausted.add((name, model))
                left = len([n for n, _ in self._keys if self._usable(n, model)])
            print(f"  [key rotation] {name} spent its daily quota for {model} -- retired for that model "
                  f"for the rest of this run; {left} key(s) left for it")
            return
        with self._lock:
            self._cooldown_until[(name, model)] = time.time() + KEY_COOLDOWN_SECONDS
        print(f"  [key rotation] {name} hit a quota/rate limit for {model} -- skipping that pair for "
              f"{KEY_COOLDOWN_SECONDS}s (the key still serves other models)")

    @property
    def models(self):
        return _RotatingModels(self)


class _RotatingModels:
    """The `.models` namespace of the proxy above."""

    def __init__(self, parent):
        self._parent = parent

    def generate_content(self, _key_out=None, _avoid_key=None, **kwargs):
        # `_key_out`, if given, receives the key name this call actually used, so
        # the caller can attribute a failure to it. It has to travel WITH the
        # call: last_key_name is one shared attribute and the call runs on
        # call_with_timeout's daemon thread, so with two tickers analysed at once
        # (refresh_fundamentals.MAX_CONCURRENT_TICKERS) reading it back named the
        # wrong key 41% of the time in a 24-call reproduction.
        #
        # A key-specific auth failure retries the SAME call on another key.
        # Previously it propagated, run_model_ladder treated it as terminal, and
        # with keys picked at random one bad key abandoned ~half of all
        # tickers (93/200 in a stubbed two-key test) though the other key worked.
        last_exc = None
        model = kwargs.get("model")
        for _ in range(len(self._parent._keys)):
            name, client = self._parent._pick(avoid=_avoid_key, model=model)
            if client is None:
                break
            if _key_out is not None:
                # Before the call, not after: a call that TIMES OUT never returns,
                # and timeouts were 68 of 86 failures on 2026-09-18.
                _key_out["key"] = name
            try:
                return client.models.generate_content(**kwargs)
            except Exception as e:
                last_exc = e
                self._parent._mark_failure(name)
                if is_key_invalid(e):
                    if self._parent._mark_dead(name, e):
                        continue
                    raise
                if is_permission_denied(e):
                    # This key may not call this model (a 403): retire the PAIR and try another key.
                    # The key keeps serving every other model; when no key is left for this one,
                    # ModelExhausted below steps the ladder to its next model.
                    self._parent._mark_forbidden(name, e, model)
                    continue
                if _is_quota_error(e):
                    # Retry on ANOTHER key, exactly as an auth failure does.
                    # This used to fall through to the raise below, so a 429
                    # aborted the call and cost a rung of the model ladder
                    # (standard_tiers: primary, primary again, fallback) even
                    # though a key with quota left was sitting right there --
                    # which is the whole point of rotating. _mark_quota_error
                    # has already put this key on cooldown, so _pick skips it;
                    # when every key is cooling _pick falls back to using them
                    # all rather than hard-failing. Bounded by the enclosing
                    # `for _ in range(len(keys))`.
                    self._parent._mark_quota_error(name, e, model)
                    continue
                raise
        parent = self._parent
        live = [n for n, _ in parent._keys if n not in parent._dead]
        if live and parent.usable_count(model) == 0:
            # Every live key is retired for THIS model (its daily quota spent, or refused with a 403).
            # ModelExhausted, not the last key's own error: the ladder then skips the model's remaining
            # rungs without waiting (is_dead_model). The daily-quota form used to be a plain 429 here,
            # so each remaining rung of the model still slept its backoff and then failed at once.
            quota = any((n, model) in parent._exhausted for n in live)
            refused = any((n, model) in parent._forbidden for n in live)
            reason = ("has spent its daily quota or is refused" if quota and refused
                      else "has spent its daily quota" if quota else "is refused")
            raise ModelExhausted(model, 429 if quota else 403, reason) from last_exc
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No usable Gemini API key left (every configured key failed authentication)")


def log_key_usage(client):
    """Print `client`'s per-key call/failure split, if it tracks one.

    Deliberately defensive. This runs at the END of an AI job, after the output
    is saved and pushed, and a diagnostic must never be the thing that fails a
    run which otherwise succeeded -- a plain genai.Client has no counters, and
    nor does a stub in a check. Printing the summary directly cost exactly that:
    checks/test_refresh_limit.py stubs make_client with a bare object() and the
    fundamentals job died on its last line, after a clean run.
    """
    summary = getattr(client, "usage_summary", None)
    if callable(summary):
        print(summary())


# How many tickers in a row may fail before a nightly AI job stops (FailureFuse).
CONSECUTIVE_FAILURE_LIMIT = 10


class FailureFuse:
    """Stops a nightly AI job early when its calls keep failing.

    A dead key or an exhausted quota fails EVERY ticker, but the jobs ground on
    for ~2 hours anyway (each failure keeps the ticker's previous view, so
    nothing is lost the first nights), then reported success -- so the slot gate
    counted the slot as done, nothing retried, and views quietly aged to Pending
    with no red run anywhere. Exiting non-zero alone would have made the gate
    re-run a 2-hour job several times.

    So: after `limit` CONSECUTIVE failed tickers the job skips the rest (their
    previous views stay as they are) and exits non-zero. The run shows red, and
    the gate's retry costs minutes, not hours. Isolated failures -- a slow search,
    one bad answer -- reset the streak and never trip it. Thread-safe: the
    Sentiment job analyses tickers in parallel."""

    def __init__(self, limit=None):
        self.limit = limit or CONSECUTIVE_FAILURE_LIMIT
        self.streak = 0
        self.tripped = False
        self._lock = threading.Lock()

    def record(self, failed):
        """Count one ticker's outcome; returns True once the fuse has tripped."""
        with self._lock:
            self.streak = self.streak + 1 if failed else 0
            if self.streak >= self.limit and not self.tripped:
                self.tripped = True
                print(f"[failure fuse] {self.streak} tickers failed in a row -- stopping this run early; "
                      "the rest keep their previous views.")
            return self.tripped

    def exit_if_tripped(self):
        """End the job with an error after a tripped fuse (call after saving)."""
        if self.tripped:
            print(f"::error::Stopped early after {self.limit} consecutive failures (likely dead keys or "
                  "exhausted quota). Previous views were kept; the slot gate will retry.")
            raise SystemExit(1)


def make_client(api_key=None, st_secrets=None):
    """The client every pipeline should use: rotates across all configured keys.

    `api_key` accepts a single key or a list, for callers that already resolved
    one (the dashboard reads st.secrets before calling into the analysis
    helpers). Returns None when nothing is configured, matching what the
    callers already check for.
    """
    keys = gemini_api_keys(st_secrets=st_secrets, extra=api_key)
    if not keys:
        return None
    # Log the names (not the values): discovery is by prefix, so this is the
    # only place a run says how many keys it is actually spreading load across.
    # A key added as a secret but not passed through the workflow's `env:` would
    # otherwise silently leave the run on fewer keys.
    print(f"  [key rotation] {len(keys)} key(s): {', '.join(n for n, _ in keys)}")
    return RotatingGeminiClient(keys)


def refresh_limit():
    """REFRESH_LIMIT as a positive int, or None (no limit).

    Caps how many tickers a workflow run analyses -- the first N in scope. It is
    for smoke-testing an AI workflow on a couple of tickers. The dispatch input is
    a number rather than ticker names on purpose: Actions logs of this public repo
    print each step's env verbatim, before log_redact.py ever sees the output.
    """
    value = os.environ.get("REFRESH_LIMIT", "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


def pending_only():
    """REFRESH_PENDING_ONLY: analyse only the tickers whose stored view is not a
    valid one (owner, 2026-11-08) -- the dashboard's "Retry Pending" button, which
    used to run inside the browser session and stopped when the browser closed
    (Streamlit stops a session's script when its websocket drops). A switch, not
    a list: the job works the pending tickers out itself from the data repo, so no
    ticker name goes into this public repo's Actions logs, which print each step's
    env verbatim."""
    return os.environ.get("REFRESH_PENDING_ONLY", "").strip().lower() == "true"


def kept_previous(store, before):
    """How many of `before`'s tickers ({ticker: the view object it held when the run
    started}) still hold that very object -- i.e. the run wrote nothing new for them.
    Judged by the outcome, not by which failure path a ticker took: a kept prior, a
    second timeout and a stock skipped after the fuse tripped all leave the object
    in place. A ticker that had no view before the run is not counted (a failure
    writes it a stub, which the job's own failure count covers)."""
    return sum(1 for tk, old in before.items() if old is not None and store.get(tk) is old)


def leftover_warning(job, analysed, kept, no_source=0, kept_what="view"):
    """The one line a nightly AI job ends with when some stocks did not get a fresh
    result, as a GitHub `::warning::` annotation (shown on the run's summary page);
    None when every stock did.

    Owner, 2026-11-16: on 2026-11-14 Google's search model failed hundreds of calls;
    12 of 136 Sentiment views and 2 Expert Take write-ups kept the previous night's
    text and 14 Expert Takes were written with no news, yet both runs were green and
    said so only in scattered per-stock lines. Counts only: this repo's logs are
    public."""
    parts = []
    if kept:
        parts.append(f"{kept} of {analysed} stock(s) kept their previous {kept_what} "
                     "(the search or the model failed for them)")
    if no_source:
        parts.append(f"{no_source} were written with no news (the search failed)")
    if not parts:
        return None
    return f"::warning::{job}: " + "; ".join(parts) + "."
