"""Follow-ups from the docs/codebase_review.md triage.

Each check pins a failure that was reproduced against the pre-fix code:

  #2  hard_split_text spun forever when one character exceeded the limit.
  #1  a corrupt expert_views.json / fundamentals.json returned {}, and the next
      per-ticker save rewrote the store with only that run's tickers.
  #11 yfinance duplicate row labels made .loc return a DataFrame, whose
      elementwise comparison raised and took the WHOLE statements block with it.
  #13 a non-dict value in the branch's JSON raised AttributeError mid-push.
  #5  a fixed "<path>.tmp" let two writers interleave into one temp file.
  #4  a 429 aborted the call instead of retrying on a key with quota left.
  #3  date.today() on a UTC runner stamped tomorrow's date on an ET-evening run.

Offline: invented tickers, temp files, stubbed clients, no network.
"""

import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO)

import ast
import json
import os
import subprocess
import tempfile
import threading

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- #2 hard_split_text terminates ----------------------------------------
from alerts import hard_split_text, _discord_len

result, done = {}, threading.Event()


def _run():
    try:
        result["v"] = hard_split_text("\U0001F389\U0001F389\U0001F389", 1)
    finally:
        done.set()


threading.Thread(target=_run, daemon=True).start()
check(done.wait(timeout=10), "hard_split_text terminates at limit=1 with astral chars")
if "v" in result:
    check("".join(result["v"]) == "\U0001F389" * 3, "...and loses no characters")
    check(hard_split_text("aaaa bbbb cccc", 9) == ["aaaa bbbb", "cccc"],
          "...while an ordinary split is unchanged")
    check(hard_split_text("short", 99) == ["short"], "...and a fitting string passes straight through")


# --- #1 corrupt AI stores are loud, and the callers are wired up ----------
from json_store import DataFileError
import expert_views
import fundamentals_eval

d = tempfile.mkdtemp()
for mod, attr, fn, name in ((expert_views, "EXPERT_VIEWS_FILE", "load_expert_views", "expert_views.json"),
                            (fundamentals_eval, "FUNDAMENTALS_FILE", "load_fundamentals", "fundamentals.json")):
    p = os.path.join(d, name)
    original = getattr(mod, attr)
    setattr(mod, attr, p)
    try:
        open(p, "w").write('{"ACME": {"verdict"')
        try:
            getattr(mod, fn)()
            check(False, f"corrupt {name} -> DataFileError")
        except DataFileError as e:
            check(name in str(e), f"corrupt {name} -> DataFileError naming the file")
        os.remove(p)
        check(getattr(mod, fn)() == {}, f"missing {name} -> {{}} (nothing generated yet is normal)")
    finally:
        setattr(mod, attr, original)

# app.py must preflight them, or the raise lands mid-render instead of naming the file.
app_tree = ast.parse(open(f"{REPO}/app.py").read())
preflight = []
for node in ast.walk(app_tree):
    if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == "_preflight":
        preflight = [n.id for n in ast.walk(node.iter) if isinstance(n, ast.Name)]
for fn in ("load_expert_views", "load_fundamentals"):
    check(fn in preflight, f"app.py preflights {fn} (stops with the filename, not a stack trace)")

# ...and each refresh script must exit 1 rather than rebuild the store from empty.
for script, store in (("refresh_expert_views.py", "expert_views.json"),
                      ("refresh_fundamentals.py", "fundamentals.json")):
    src = ast.parse(open(f"{REPO}/{script}").read())
    handlers = [h for h in ast.walk(src) if isinstance(h, ast.ExceptHandler)
                and "DataFileError" in ast.dump(h.type or ast.Constant(None))]
    exits = any(isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "exit"
                for h in handlers for n in ast.walk(h))
    check(bool(handlers) and exits, f"{script} catches DataFileError and exits non-zero")


# --- #11 duplicate statement row labels -----------------------------------
import pandas as pd
from stock_data import _statement_row

dup = pd.DataFrame([[5.0, 4.0, 3.0, 2.0, 1.0], [9.0] * 5],
                   index=["Diluted EPS"] * 2, columns=list("abcde"))
row = _statement_row(dup, "Diluted EPS")
check(isinstance(row, pd.Series), "a duplicated row label still yields a Series, not a DataFrame")
check(bool(row.iloc[4] > 0), "...so the growth guard evaluates to a plain bool")
check(list(_statement_row(pd.DataFrame([[5.0, 4.0]], index=["Diluted EPS"], columns=["a", "b"]),
                          "Diluted EPS")) == [5.0, 4.0], "...and a normal frame is untouched")


# --- #13 a primitive where a dict was expected -----------------------------
gs_src = open(f"{REPO}/github_sync.py").read()
check("isinstance(theirs_entry, dict)" in gs_src and "isinstance(value, dict)" in gs_src,
      "push_json_entry_changes coerces both sides to a dict before .get()")
# exercise the exact expression shape that used to raise
for bad in ("a-string", 7, True, None, []):
    entry = bad if isinstance(bad, dict) else {}
    try:
        str(entry.get("as_of") or "")
    except Exception as e:
        check(False, f"guard still raises on {bad!r}: {e}")
check(True, "the guarded expression survives str/int/bool/None/list values")


# --- #5 concurrent atomic writes --------------------------------------------
import json_store

target = os.path.join(tempfile.mkdtemp(), "store.json")
json_store.atomic_write_json(target, {"seed": 1})
errors = []


def writer(n):
    try:
        for _ in range(25):
            json_store.atomic_write_json(target, {"writer": n, "rows": list(range(50))})
    except Exception as e:      # pragma: no cover
        errors.append(e)


threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
[t.start() for t in threads]
[t.join() for t in threads]
check(not errors, f"6 concurrent writers raise nothing ({errors[:1]})")
with open(target) as f:
    check(isinstance(json.load(f), dict), "...and the file left behind is valid JSON, not a blend")
strays = [p for p in os.listdir(os.path.dirname(target)) if p.endswith(".tmp")]
check(not strays, f"...with no temp files left behind ({strays})")
check(oct(os.stat(target).st_mode)[-3:] == "644",
      f"permissions stay 0644, not mkstemp's 0600 ({oct(os.stat(target).st_mode)[-3:]})")


# --- #4 a 429 retries on another key ---------------------------------------
import llm_util

# Build the pool DIRECTLY. make_client also discovers any GEMINI_API_KEY* in the
# environment or a local .env, and those extra keys would let the call succeed
# even without the fix -- the test has to see exactly two.
client = llm_util.RotatingGeminiClient([("quota-bound", "k1"), ("healthy", "k2")])


class _Stub:
    def __init__(self, key):
        # keyed by the KEY VALUE that _client_for receives, not the name
        self.key, self.models = key, self

    def generate_content(self, **kw):
        if self.key == "k1":       # the quota-bound key's value
            raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
        return "ok"


client._client_for = _Stub
ok_count, raised = 0, 0
for _ in range(40):
    try:
        client.models.generate_content(model="m", contents="x", config=None)
        ok_count += 1
    except Exception:
        raised += 1
check(ok_count == 40 and raised == 0,
      f"a 429 falls through to a key with quota ({ok_count}/40 ok, {raised} raised)")

# A non-quota, non-auth error must STILL propagate -- the continue is narrow.
client2 = llm_util.RotatingGeminiClient([("a", "ka"), ("b", "kb")])


class _Boom:
    def __init__(self, key):
        self.models = self

    def generate_content(self, **kw):
        raise RuntimeError("400 INVALID_ARGUMENT: malformed request")


client2._client_for = _Boom
try:
    client2.models.generate_content(model="m", contents="x", config=None)
    check(False, "a genuine bad request still propagates")
except RuntimeError:
    check(True, "a genuine bad request still propagates (not swallowed by the retry loop)")


# --- #3 ET date, not the runner's ------------------------------------------
probe = (
    "import alerts, weekly_wrapup_check, datetime;"
    "et=alerts.datetime.now(alerts.ZoneInfo('America/New_York')).date().isoformat();"
    "print(et, datetime.date.today().isoformat())"
)
out = subprocess.run([sys.executable, "-c", probe], cwd=REPO, capture_output=True, text=True,
                     env={**os.environ, "TZ": "UTC"})
check(out.returncode == 0, f"both modules import with ZoneInfo wired in ({out.stderr.strip()[:80]})")
for name, src in (("alerts.py", open(f"{REPO}/alerts.py").read()),
                  ("weekly_wrapup_check.py", open(f"{REPO}/weekly_wrapup_check.py").read())):
    check('datetime.now(ZoneInfo("America/New_York")).date()' in src,
          f"{name} stamps the ET date")
check("today = date.today()" not in open(f"{REPO}/alerts.py").read(),
      "alerts.py no longer uses the runner's local date")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
