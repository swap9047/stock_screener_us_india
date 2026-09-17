"""A timed-out Gemini call must not keep the process alive.

WHY: generate_with_timeout used a ThreadPoolExecutor and abandoned the worker on
timeout (`shutdown(wait=False)`). Its docstring said that stopped the job
hanging; it only moved the hang. Executor workers are non-daemon and
`concurrent.futures` registers an atexit hook that JOINS them, so the
interpreter could not exit while an abandoned call was still blocked -- and the
SDK had no request timeout, so "still blocked" had no upper bound.

news-summary on 2026-09-17 is the proof: tickers done 01:10, digest saved and
posted, then killed at its 180-minute cap at 03:09. Five timed-out calls; the
threads run concurrently, so the tail is the longest one, and at least one held
for roughly two hours.

Nothing caught it because every existing check calls the function and inspects
its RETURN value -- the defect is in what happens after main() returns. These
checks measure process exit time in a subprocess, which is the only place it is
observable.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import subprocess
import time

import llm_util

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- the defect is only visible at process exit ----------------------------
HANG_SECONDS = 12
CALL_TIMEOUT = 1

PROBE = f'''
import sys, time
sys.path.insert(0, {str(REPO)!r})
import llm_util

class _Hanging:
    class models:
        @staticmethod
        def generate_content(**kw):
            time.sleep({HANG_SECONDS})
            return "never reached"

for _ in range({{n}}):
    try:
        llm_util.generate_with_timeout(_Hanging(), "m", "p", None, timeout={CALL_TIMEOUT})
    except TimeoutError:
        pass
print("main done", flush=True)
'''


def exit_seconds(n):
    """Wall-clock from launch to process exit, with n timed-out calls."""
    t0 = time.time()
    r = subprocess.run([sys.executable, "-c", PROBE.format(n=n)],
                       capture_output=True, text=True, timeout=HANG_SECONDS * 4)
    return time.time() - t0, r.stdout


# One timed-out call: main() finishes after ~CALL_TIMEOUT. If an abandoned
# worker is joined at exit, the process instead lives for ~HANG_SECONDS.
elapsed, out = exit_seconds(1)
check("main done" in out, "the probe ran")
check(elapsed < HANG_SECONDS * 0.6,
      f"one timed-out call does not hold the process open ({elapsed:.1f}s, "
      f"worker sleeps {HANG_SECONDS}s)")

# Five, matching the run that was killed. Each call blocks its own caller for
# CALL_TIMEOUT, so ~5s of legitimate waiting -- but still no exit-time join.
elapsed5, _ = exit_seconds(5)
check(elapsed5 < HANG_SECONDS * 0.8,
      f"five timed-out calls do not hold it open either ({elapsed5:.1f}s)")

# --- semantics the fix must not change -------------------------------------
class _Fine:
    class models:
        @staticmethod
        def generate_content(**kw):
            return "OK"


class _Boom:
    class models:
        @staticmethod
        def generate_content(**kw):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")


check(llm_util.generate_with_timeout(_Fine(), "m", "p", None, timeout=5) == "OK",
      "a successful call still returns its response")
try:
    llm_util.generate_with_timeout(_Boom(), "m", "p", None, timeout=5)
    check(False, "an API error still propagates to the caller")
except RuntimeError as e:
    check("429" in str(e), "an API error still propagates to the caller, unwrapped")

try:
    llm_util.generate_with_timeout(
        type("H", (), {"models": type("M", (), {"generate_content": staticmethod(
            lambda **kw: __import__("time").sleep(5))})()})(), "m", "p", None, timeout=1)
    check(False, "a timeout still raises TimeoutError")
except TimeoutError as e:
    check("timed out after 1s" in str(e), "a timeout still raises TimeoutError with the same message")

# is_retryable must keep treating a timeout as worth another attempt.
check(llm_util.is_retryable(TimeoutError("API call to m timed out after 120s")),
      "a TimeoutError is still retryable")

# --- the root fix: the SDK gets its own request timeout --------------------
src = open(REPO / "llm_util.py").read()
check("HTTP_TIMEOUT_SECONDS" in src and "http_options=types.HttpOptions" in src,
      "clients are built with an SDK-level request timeout")
check(llm_util.HTTP_TIMEOUT_SECONDS > llm_util.CALL_TIMEOUT_SECONDS,
      "the HTTP timeout sits ABOVE the wrapper's, so the wrapper stays primary "
      f"({llm_util.HTTP_TIMEOUT_SECONDS}s vs {llm_util.CALL_TIMEOUT_SECONDS}s)")
# AST, not a substring: the module's own docstrings explain the executor bug,
# so a text search matches the explanation as well as the code.
import ast
tree = ast.parse(src)
imports_cf = any(a.name.split(".")[0] == "concurrent"
                 for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names) or \
             any((n.module or "").split(".")[0] == "concurrent"
                 for n in ast.walk(tree) if isinstance(n, ast.ImportFrom))
uses_pool = any(getattr(n, "attr", None) == "ThreadPoolExecutor" for n in ast.walk(tree))
check(not imports_cf and not uses_pool,
      "the ThreadPoolExecutor (whose workers atexit joins) is gone from the CODE")
check("daemon=True" in src, "the worker thread is a daemon")

# The SDK import must stay lazy -- checks/test_gate_imports.py blocks `google`.
top_level = {a.name for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in n.names} | {n.module for n in tree.body if isinstance(n, ast.ImportFrom)}
check(not any(str(m).startswith("google") for m in top_level if m),
      f"google is NOT imported at module level ({sorted(str(m) for m in top_level if m)})")

# --- all three AI jobs get the full 6h backstop ----------------------------
import yaml
for wf in ("news-summary", "fundamentals", "expert-views"):
    d = yaml.safe_load(open(REPO / f".github/workflows/{wf}.yml"))
    build = d["jobs"]["build"]
    check(build.get("timeout-minutes") == 360,
          f"{wf}.yml build gets GitHub's full 6h ({build.get('timeout-minutes')})")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
