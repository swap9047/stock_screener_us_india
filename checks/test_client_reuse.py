"""A loop over tickers must build ONE RotatingGeminiClient, not one per ticker.

WHY: the dashboard's bulk Sentiment re-analysis passed client=None and let
fundamentals_eval.analyze_single_ticker_sentiment build its own per ticker. The
rotation state is per client instance, so that reset every time:

  * a revoked key was rediscovered and retried once PER TICKER instead of once
    per run (RotatingGeminiClient retires it after a single auth failure),
  * the 429 cooldown reset, so a rate-limited key was eligible again immediately,
  * the cached underlying genai.Client -- which exists precisely because
    construction is not free at ~250 calls a night -- was rebuilt each pass.

The batch scripts (refresh_fundamentals / refresh_expert_views) always did this
correctly; only the interactive paths did not. These checks pin the contract that
makes sharing possible -- the `client=` parameter and its precedence -- and assert
no module reintroduces a per-call client.
"""

import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO)

import ast
import inspect

import llm_util

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- the contract that makes reuse possible -------------------------------
import expert_views
import fundamentals_eval

for mod, fn_name in ((expert_views, "analyze_single_ticker"),
                     (fundamentals_eval, "analyze_single_ticker_sentiment")):
    sig = inspect.signature(getattr(mod, fn_name))
    check("client" in sig.parameters,
          f"{mod.__name__}.{fn_name} accepts a shared client=")
    check(sig.parameters["client"].default is None,
          f"{mod.__name__}.{fn_name}'s client= defaults to None (callers unchanged)")
    src = inspect.getsource(getattr(mod, fn_name))
    check("client = client or llm_util.make_client(" in src,
          f"{mod.__name__}.{fn_name} prefers the passed client over building one")


# --- a passed client is actually USED, not silently ignored ----------------
made = {"n": 0}
real_make = llm_util.make_client
llm_util.make_client = lambda *a, **kw: made.__setitem__("n", made["n"] + 1) or real_make(*a, **kw)
try:
    sentinel = object()
    seen = {}

    def _spy(client, row, **kw):
        seen["client"] = client
        return {}          # an invalid view -> the function returns early, writing nothing

    fundamentals_eval.generate_fundamental_view = _spy
    fundamentals_eval.analyze_single_ticker_sentiment("ACME", {"ticker": "ACME"}, ["k"], client=sentinel)
    check(seen.get("client") is sentinel, "the passed client reaches the generator untouched")
    check(made["n"] == 0, "passing a client builds no new one")
finally:
    llm_util.make_client = real_make


# --- no interactive loop rebuilds a client per ticker ----------------------
# app.py is too large to import here (Streamlit), so read its AST: inside the
# bulk loop and the per-ticker button, the analyze_* calls must pass client=.
app_src = open(f"{REPO}/app.py").read()
tree = ast.parse(app_src)
offenders = []
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    fn = node.func
    name = getattr(fn, "id", None) or getattr(fn, "attr", None)
    if name in ("analyze_single_ticker", "analyze_single_ticker_sentiment"):
        if not any(kw.arg == "client" for kw in node.keywords):
            offenders.append(f"{name} at app.py:{node.lineno}")
check(not offenders, f"every app.py analyze_* call passes client= (offenders: {offenders})")

# ...and each of those clients is built with st_secrets, or the rotation log
# shows "caller[N]" instead of real key names on Streamlit Cloud, where the keys
# live in st.secrets rather than os.environ.
mk = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
      and getattr(n.func, "attr", None) == "make_client"]
check(len(mk) >= 2, f"app.py builds a shared client in both interactive paths ({len(mk)} found)")
check(all(any(kw.arg == "st_secrets" for kw in c.keywords) for c in mk),
      "every app.py make_client passes st_secrets, so key names survive in the log")

# --- and the batch scripts still build exactly one, outside their loop -----
for script in ("refresh_fundamentals.py", "refresh_expert_views.py"):
    src = open(f"{REPO}/{script}").read()
    t = ast.parse(src)
    calls = [n for n in ast.walk(t) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "make_client"]
    check(len(calls) == 1, f"{script} builds exactly one client per run ({len(calls)})")
    in_loop = any(isinstance(d, (ast.For, ast.While))
                  for f in ast.walk(t) if isinstance(f, (ast.For, ast.While))
                  for d in ast.walk(f) if d in calls)
    check(not in_loop, f"{script}'s client is built outside its ticker loop")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
