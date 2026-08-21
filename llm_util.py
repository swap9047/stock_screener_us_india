"""Shared Gemini-call plumbing for the three AI pipelines.

`news_summary.py`, `expert_views.py` and `fundamentals_eval.py` each grew their
own copy of the same three things -- a timeout wrapper, a notion of which
failures are worth retrying, and a model ladder. The copies drifted: only the
news pipeline ever gained a same-model retry and a terminal-error gate, so a
single transient 429 permanently demoted a ticker to the Gemma fallback in the
other two. This module is the one implementation they now share.
"""

import concurrent.futures
import time

CALL_TIMEOUT_SECONDS = 120

# Short pause before re-trying the SAME model. Buys a transient 429/503 a second
# chance on the good model before quality degrades to a fallback.
RETRY_BACKOFF_SECONDS = 5

# Failures that re-running cannot fix. Everything else (rate limits, timeouts,
# 5xx, transport errors) is worth another attempt. Without this gate an expired
# key or an exhausted daily quota burns every tier of the ladder for every
# ticker, and -- where a retry queue exists -- gets re-attempted ~100 more times
# with a 30s sleep between each.
TERMINAL_ERROR_MARKERS = (
    "401", "403", "400", "unauthorized", "permission", "api key", "invalid argument",
)


def generate_with_timeout(client, model, contents, config, timeout=CALL_TIMEOUT_SECONDS):
    """One generate_content call, bounded by `timeout`.

    The worker thread is abandoned rather than joined on timeout (see commit
    33cac85) -- waiting here is what used to hang the whole GitHub Actions job.
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(client.models.generate_content, model=model, contents=contents, config=config)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise TimeoutError(f"API call to {model} timed out after {timeout}s")
    finally:
        executor.shutdown(wait=False)


# Errors that are about THIS MODEL rather than the account: a wrong or retired
# model id, or one the key has no access to. They say nothing about the next
# tier, so the ladder should step past them rather than give up -- otherwise a
# single typo'd model id silently degrades the whole stage, including a
# fallback that would have worked perfectly.
MODEL_UNAVAILABLE_MARKERS = (
    "not found", "does not exist", "is not supported", "unsupported model",
    "no such model", "404",
)


def is_model_unavailable(exc):
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in MODEL_UNAVAILABLE_MARKERS)


def is_retryable(exc):
    """Whether re-running the SAME call could plausibly succeed."""
    if isinstance(exc, TimeoutError):
        return True
    if is_model_unavailable(exc):
        return False          # retrying a missing model just burns time
    text = f"{type(exc).__name__}: {exc}".lower()
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
    for model, backoff in tiers:
        if backoff:
            time.sleep(backoff)
        try:
            resp = generate_with_timeout(client, model, prompt, config_for(model), timeout=timeout)
            return (on_success(resp) if on_success else resp), model
        except Exception as e:
            print(f"  [{label} {model} failed] {subject}: {e}")
            if is_model_unavailable(e):
                # This model is wrong/retired/not enabled -- the next tier may
                # still be fine, so step past instead of abandoning the ladder.
                continue
            if not is_retryable(e):
                # Bad key, exhausted quota: no model will work. Stop.
                break
    return None, None


def standard_tiers(primary, fallback):
    """The ladder every pipeline should use: the good model, the good model
    again after a backoff, then the fallback."""
    tiers = [(primary, 0), (primary, RETRY_BACKOFF_SECONDS)]
    if fallback and fallback != primary:
        tiers.append((fallback, 0))
    return tiers


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
