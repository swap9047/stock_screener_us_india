"""Every model call is sequential (owner, 2026-11-01).

Q1 3.8-flash and 3.6-flash twice each wherever they are in a chain, 3.5-flash-lite twice.
Q2 The wait before a retry is exponential over the whole ladder: 2, 4, 8, 16, 30, 30... s.
Q3 No alternating between two models and no second pass: collation's "pairs" shape (a 3.8 call
   30 s after the first round) is gone from the code and the panel.
Q4 A call has ONE cut-off, 200 s, and the SDK request carries the same timeout: before, the
   wrapper only abandoned a slow call and its request kept running until the client-wide
   backstop (60 s later) while the ladder had already started the next try.
Q5 The same, against the real SDK and a slow local server.

Offline: invented prompts, fake clients, a server on localhost, no network.
"""

import json
import sys
import threading
import time
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


F38, F37, F36, L, G = ms.FLASH_38, ms.FLASH_37, ms.FLASH_36, ms.FLASH_LITE, ms.GEMMA_26B

# --- Q1 the chains ----------------------------------------------------------------------------
for step, spec in ms.STEPS.items():
    for model, attempts in spec["chain"]:
        if model in (F38, F36, L):
            check(attempts == 2, f"Q1: {step}: {ms.short(model)} is tried twice (got {attempts})")
check([(ms.short(m), n) for m, n in ms.STEPS["N3"]["chain"]]
      == [("gemini-3.7-flash", 2), ("gemini-3.6-flash", 2), ("gemini-3.5-flash-lite", 2)],
      "Q1: collation is 3.7 x2, 3.6 x2, flash-lite x2")
check(ms.STEPS["R1"]["chain"] == ms.STEPS["R2"]["chain"] == [(F38, 2), (F36, 2)], "Q1: the market reads are 3.8 x2, 3.6 x2")
check(all(ms.STEPS[s]["chain"] == [(G, 3)] for s in ("N1", "E1", "F1", "F2")), "Q1: Gemma's searches are untouched (3 tries)")
check([ms.STEPS[s]["budget"] for s in ("N2", "N3", "E2", "F3", "R1", "R2")] == [8192, 8192, 8192, 8192, 4096, 4096],
      "Q1: the thinking budgets are as they were")
check(llm_util.sequential_tiers([(F38, 2), (F36, 2)]) == [(F38, 0), (F38, 2), (F36, 4), (F36, 8)],
      "Q1: llm_util's own builder is the same sequence")
import market_read
check(llm_util.sequential_tiers([(market_read.PRIMARY_MODEL, market_read.PRIMARY_ATTEMPTS)]
                                + [(m, market_read.FALLBACK_ATTEMPTS) for m in market_read.FALLBACK_MODELS])
      == [(F38, 0), (F38, 2), (F36, 4), (F36, 8)], "Q1: the market read's own default ladder (no saved chain) is 3.8 x2, 3.6 x2")

# --- Q2 exponential waits, in the ladder as it runs --------------------------------------------
check(llm_util.RETRY_BACKOFF_SECONDS == 2 and llm_util.RETRY_BACKOFF_MAX_SECONDS == 30,
      "Q2: base 2 s, at most 30 s")
check([llm_util.retry_wait(n) for n in range(8)] == [0, 2, 4, 8, 16, 30, 30, 30], "Q2: 0, 2, 4, 8, 16, 30, 30, 30")
check(llm_util.retry_wait(3, base=0) == 0, "Q2: a zero base gives no waits (how the checks run fast)")

slept, called = [], []
_sleep, _gwt = llm_util.time.sleep, llm_util.generate_with_timeout
llm_util.time.sleep = lambda s: slept.append(s)


def failing(client, model, prompt, config, timeout=None, avoid_key=None):
    called.append((model, timeout))
    raise RuntimeError("503 UNAVAILABLE")


llm_util.generate_with_timeout = failing
try:
    chain = [{"model": F38, "attempts": 2, "timeout": 200}, {"model": F36, "attempts": 2, "timeout": 200},
             {"model": L, "attempts": 2, "timeout": 200}]
    out = llm_util.run_model_ladder(None, "p", ms.build_tiers(chain), lambda m: None, label="t")
finally:
    llm_util.time.sleep, llm_util.generate_with_timeout = _sleep, _gwt
check(out == (None, None), "Q2: every rung failing gives (None, None)")
check([m for m, _ in called] == [F38, F38, F36, F36, L, L], "Q2: the six calls go in order, a model finished before the next starts")
check(slept == [2, 4, 8, 16, 30], f"Q2: the waits before them: {slept}")
check(all(t == 200 for _, t in called), "Q2: each call is given the 200 s cut-off")

# --- Q3 no alternating, no second pass ----------------------------------------------------------
check(not hasattr(llm_util, "retry_pair_tiers"), "Q3: retry_pair_tiers (first two models, then both again) is gone")
check(all("shape" not in spec for spec in ms.STEPS.values()), "Q3: no step has a 'pairs' shape any more")
for f in ("llm_util.py", "model_settings.py", "news_summary.py", "app.py"):
    src = (REPO / f).read_text()
    check("retry_pair_tiers(" not in src and 'pair_backoff' not in src and "take turns" not in src,
          f"Q3: {f} has no alternating-pair code or text")
check("COLLATION_RETRY_WAIT_SECONDS" not in (REPO / "news_summary.py").read_text().replace("# Stage 3 used to", ""),
      "Q3: collation's fixed 30 s wait is not used")
check(ms.build_tiers([{"model": F37, "attempts": 2, "timeout": 60}, {"model": F36, "attempts": 2, "timeout": 100}])
      == [(F37, 0, 60), (F37, 2, 60), (F36, 4, 100), (F36, 8, 100)], "Q3: a chain is its first model's tries, then the next's")

# --- Q4 one cut-off, and the SDK request carries it ----------------------------------------------
check(ms.DEFAULT_TIMEOUT_S == ms.MAX_TIMEOUT_S == llm_util.CALL_TIMEOUT_SECONDS == llm_util.SEARCH_TIMEOUT_SECONDS == 200,
      "Q4: 200 s everywhere a call's cut-off is set")
check(ms.normalize_chain([{"model": F38, "attempts": 2, "timeout": 500}])[0]["timeout"] == 200, "Q4: a longer saved timeout is capped at 200")
seen = []


class Fake:
    class models:
        @staticmethod
        def generate_content(model, contents, config, **kw):
            seen.append(config)
            return "ok"


try:
    from google.genai import types
    have_sdk = True
except Exception:
    have_sdk = False
if have_sdk:
    cfg = types.GenerateContentConfig(temperature=0.3, response_mime_type="application/json")
    llm_util.generate_with_timeout(Fake(), "m", "p", cfg, timeout=200)
    llm_util.generate_with_timeout(Fake(), "m", "p", None, timeout=90)
    check(seen[0].http_options.timeout == 200_000 and seen[0].temperature == 0.3
          and seen[0].response_mime_type == "application/json" and cfg.http_options is None,
          "Q4: the config the SDK gets carries a 200 s request timeout, the rest kept, the caller's own untouched")
    check(seen[1].http_options.timeout == 90_000, "Q4: with no config, one is made with the request timeout")
stand_in = object()
seen.clear()
llm_util.generate_with_timeout(Fake(), "m", "p", stand_in, timeout=5)
check(seen == [stand_in], "Q4: anything that is not an SDK config is passed on as it was")

# --- Q5 against the real SDK and a slow server --------------------------------------------------------
if have_sdk:
    import http.server
    from google import genai

    class Slow(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                time.sleep(4)
                body = json.dumps({"candidates": [{"content": {"parts": [{"text": "late"}]}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                pass

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # The client-wide timeout is the long backstop, as in production.
    client = genai.Client(api_key="k", http_options=types.HttpOptions(
        base_url=f"http://127.0.0.1:{srv.server_address[1]}", timeout=30_000))
    t0 = time.time()
    try:
        llm_util.generate_with_timeout(client, "models/gemini-3.8-flash", "hi", types.GenerateContentConfig(), timeout=1)
        raised = None
    except BaseException as e:      # noqa: BLE001
        raised = e
    took = time.time() - t0
    time.sleep(0.7)
    alive = [t for t in threading.enumerate() if t.name.startswith("genai-")]
    check(isinstance(raised, TimeoutError) and 0.9 <= took < 2.0,
          f"Q5: a call that outlives its cut-off fails at the cut-off ({took:.1f}s, {type(raised).__name__})")
    check(not alive, "Q5: ...and its request is already dead, not still running under the next try")
    srv.shutdown()

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
