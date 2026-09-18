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

CALL_TIMEOUT_SECONDS = 120

# Request timeout handed to the Gemini SDK itself (HttpOptions, milliseconds).
# Without one the SDK waits forever, which is what let a hung grounded search
# outlive the run that made it -- see generate_with_timeout.
#
# Deliberately ABOVE CALL_TIMEOUT_SECONDS so generate_with_timeout stays the
# primary control and its message is what callers see; this only guarantees the
# abandoned call dies soon after, instead of never. Every call site passes 120.
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
SEARCH_TIMEOUT_SECONDS = 120

# Short pause before re-trying the SAME model. Buys a transient 429/503 a second
# chance on the good model before quality degrades to a fallback.
RETRY_BACKOFF_SECONDS = 5

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

# Errors that belong to ONE API key rather than the request: a revoked/invalid
# key, or a project without access. RotatingGeminiClient retires that key for
# the rest of the run and retries the call on another one, instead of letting
# the ladder give up on a ticker the other key could have served.
AUTH_ERROR_MARKERS = ("api key not valid", "api_key_invalid", "permission denied",
                      "permission_denied", "unauthorized", "unauthenticated")
_LEADING_STATUS = re.compile(r"^\s*(\d{3})\b")


def status_code(exc):
    """HTTP status of an API exception, or None."""
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    m = _LEADING_STATUS.match(str(exc))
    return int(m.group(1)) if m else None


def is_auth_error(exc):
    """True for a failure that is about the API KEY, not the request."""
    code = status_code(exc)
    text = f"{type(exc).__name__}: {exc}".lower()
    if code in (401, 403):
        return True
    return any(marker in text for marker in AUTH_ERROR_MARKERS)


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


def generate_with_timeout(client, model, contents, config, timeout=CALL_TIMEOUT_SECONDS,
                          avoid_key=None):
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
    nightly run gets diagnosed; it has to say what actually happened."""
    key_out = {} if hasattr(client, "key_names") else None
    extra = {}
    if key_out is not None:
        extra["_key_out"] = key_out
        if avoid_key:
            extra["_avoid_key"] = avoid_key
    try:
        return call_with_timeout(
            lambda: client.models.generate_content(model=model, contents=contents, config=config, **extra),
            timeout, name=f"genai-{model}",
        )
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


def run_model_ladder(client, prompt, tiers, config_for, label="llm", subject="",
                     timeout=CALL_TIMEOUT_SECONDS, on_success=None):
    """Try each (model, backoff) tier in order until one answers.

    `tiers` is a list of (model_id, sleep_seconds_before_attempt). `config_for`
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
    for model, backoff in tiers:
        if backoff:
            time.sleep(backoff)
        try:
            resp = generate_with_timeout(client, model, prompt, config_for(model), timeout=timeout,
                                         avoid_key=avoid_key)
            return (on_success(resp) if on_success else resp), model
        except Exception as e:
            # The KEY as well as the model: a 500 says nothing about which key
            # served it, and without this a run log could not tell a model-wide
            # outage from one bad key. Read off the EXCEPTION, which
            # generate_with_timeout tags with the key that made the call -- never
            # off client.last_key_name, which another worker may have moved on.
            # Absent on a plain genai.Client, and then nothing is claimed.
            key = getattr(e, "_gemini_key", None)
            avoid_key = key
            via = f" via {key}" if key else ""
            print(f"  [{label} {model} failed{via}] {subject}: {e}")
            if is_model_unavailable(e):
                # This model is wrong/retired/not enabled -- the next tier may
                # still be fine, so step past instead of abandoning the ladder.
                continue
            if not is_retryable(e):
                # Bad request or every key failing auth: no model will work. Stop.
                break
    return None, None


def standard_tiers(primary, fallback):
    """The ladder every pipeline should use: the good model, the good model
    again after a backoff, then the fallback."""
    tiers = [(primary, 0), (primary, RETRY_BACKOFF_SECONDS)]
    if fallback and fallback != primary:
        tiers.append((fallback, 0))
    return tiers


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
    # Read at CALL time, like standard_tiers does. As a default argument it was
    # bound at import and no caller (or check) could change the pacing.
    backoff = RETRY_BACKOFF_SECONDS if backoff is None else backoff
    return [(model, 0)] + [(model, backoff)] * max(0, attempts - 1)


def retry_pair_tiers(primary, fallback, backoff=RETRY_BACKOFF_SECONDS):
    """Try primary, then fallback, then BOTH again after a backoff.

    For a stage where both models are strong enough to trust, so the second
    pass is about riding out a transient 429/503 rather than degrading -- as
    opposed to standard_tiers, which retries the good model before conceding to
    a weaker one."""
    tiers = [(primary, 0)]
    if fallback and fallback != primary:
        tiers.append((fallback, 0))
    tiers.append((primary, backoff))
    if fallback and fallback != primary:
        tiers.append((fallback, 0))
    return tiers


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
# remaining calls and fails every one of them.
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
        self._cooldown_until = {}
        # Keys that returned an auth error this run -- see AUTH_ERROR_MARKERS.
        self._dead = set()
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

    def _pick(self, avoid=None):
        """A key, at random. `avoid` names one to skip if there is an
        alternative -- the ladder passes the key the previous rung just failed
        on, so a same-model retry genuinely changes something. Ignored when it
        is the only key left, because a retry on the same key still beats no
        retry."""
        now = time.time()
        with self._lock:
            usable = [(n, k) for n, k in self._keys if n not in self._dead]
            if not usable:
                return None, None
            live = [(n, k) for n, k in usable if self._cooldown_until.get(n, 0) <= now]
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

    def _mark_quota_error(self, name):
        with self._lock:
            self._cooldown_until[name] = time.time() + KEY_COOLDOWN_SECONDS
        print(f"  [key rotation] {name} hit a quota/rate limit -- skipping it for {KEY_COOLDOWN_SECONDS}s")

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
        for _ in range(len(self._parent._keys)):
            name, client = self._parent._pick(avoid=_avoid_key)
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
                if is_auth_error(e):
                    if self._parent._mark_dead(name, e):
                        continue
                    raise
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
                    self._parent._mark_quota_error(name)
                    continue
                raise
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
