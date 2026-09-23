"""Key discovery and per-call rotation across three or more Gemini keys."""

import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
GATE = str(Path(__file__).resolve().parents[1] / ".github/actions/slot-gate")
sys.path.insert(0, REPO)
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, GATE)

import os, re, subprocess, sys, tempfile, threading, time, types

sys.path.insert(0, REPO)
import llm_util

fails = 0
def check(ok, label):
    global fails; fails += not ok; print("PASS" if ok else "FAIL", label)

for k in list(os.environ):
    if k.startswith("GEMINI"): os.environ.pop(k)

# --- discovery: any GEMINI_API_KEY* name, whatever the suffix or case
os.environ.update({
    "GEMINI_API_KEY": "k1",
    "GEMINI_API_KEY_BACKUP": "k2",
    "GEMINI_API_KEY_BACKUP_B": "k3",      # as GitHub Actions exposes it (upper-cased)
    "GEMINI_API_KEY_4": "k4",             # the older numbered pattern still works
    "GEMINI_API_KEY_EMPTY": "",           # blank is not a key
    "GEMINI_MODEL": "gemini-2.5-flash",   # not a key at all
})
found = llm_util.gemini_api_keys()
names = [n for n, _ in found]
keys = [k for _, k in found]
check(keys == ["k1", "k2", "k3", "k4"] or sorted(keys) == ["k1", "k2", "k3", "k4"], f"all four keys found: {keys}")
check(names[0] == "GEMINI_API_KEY" and names[1] == "GEMINI_API_KEY_BACKUP", f"primary then backup first: {names}")
check("GEMINI_MODEL" not in names and "" not in keys and "GEMINI_API_KEY_EMPTY" not in names, f"decoys and blanks ignored: {names}")

# Secrets and env are both scanned (Streamlit even exports secrets into env), so
# what matters is: a name in both takes the secret's value, a lowercase suffix is
# found, and the same key under two spellings is not weighted twice.
secrets = {"GEMINI_API_KEY": "s1", "GEMINI_API_KEY_BACKUP_b": "s3"}
found_s = llm_util.gemini_api_keys(st_secrets=secrets)
by_name = dict((n, k) for n, k in found_s)
check(by_name.get("GEMINI_API_KEY") == "s1", f"a name in both sources takes the secret's value: {by_name.get('GEMINI_API_KEY')}")
check(by_name.get("GEMINI_API_KEY_BACKUP_b") == "s3", "lowercase suffix in secrets is found")
dupe = llm_util.gemini_api_keys(st_secrets={"GEMINI_API_KEY_BACKUP_b": os.environ["GEMINI_API_KEY_BACKUP_B"]})
check(len([k for _, k in dupe if k == os.environ["GEMINI_API_KEY_BACKUP_B"]]) == 1,
      f"the same key under two spellings appears once: {[n for n, _ in dupe]}")
check(len(llm_util.gemini_api_keys(extra="k1")) == len(found), "a caller-supplied key already configured is not double-counted")

# --- rotation spreads calls across all three keys
os.environ.pop("GEMINI_API_KEY_4")
calls = []
class StubClient:
    def __init__(self, name): self.models = types.SimpleNamespace(generate_content=lambda **kw: calls.append(name) or "ok")
client = llm_util.make_client()
check(sorted(client.key_names) == sorted(["GEMINI_API_KEY", "GEMINI_API_KEY_BACKUP", "GEMINI_API_KEY_BACKUP_B"]), f"client rotates 3 keys: {client.key_names}")
client._client_for = lambda key: StubClient({"k1": "GEMINI_API_KEY", "k2": "GEMINI_API_KEY_BACKUP", "k3": "GEMINI_API_KEY_BACKUP_B"}[key])
for _ in range(300):
    client.models.generate_content(model="m", contents="c")
share = {n: calls.count(n) / len(calls) for n in set(calls)}
check(len(share) == 3 and all(v > 0.15 for v in share.values()), f"load split across all three keys: { {k: round(v, 2) for k, v in share.items()} }")
check(sum(client.call_counts.values()) == 300, f"call_counts totals the calls: {client.call_counts}")

# --- a key that hits its quota is skipped while it cools down
class Quota429(Exception):
    def __init__(self): super().__init__("429 RESOURCE_EXHAUSTED quota exceeded")
client2 = llm_util.make_client()
hot = client2.key_names[0]
def only_hot_fails(key):
    name = {v: n for n, v in client2._keys}[key] if False else None
    return None
mapping = {k: n for n, k in client2._keys}
def client_for(key):
    name = mapping[key]
    def gen(**kw):
        if name == hot: raise Quota429()
        return "ok"
    return types.SimpleNamespace(models=types.SimpleNamespace(generate_content=gen))
client2._client_for = client_for
raised = 0
for _ in range(40):
    try: client2.models.generate_content(model="m", contents="c")
    except Quota429: raised += 1
# A quota error now RECOVERS on another key instead of propagating -- it used to
# abort the call and cost a rung of the model ladder while a key with quota sat
# unused, which is the opposite of what rotating is for. The cooldown is still
# what this check is really about; `raised == 0` is the new half.
check(client2._cooldown_until.get(hot, 0) > 0, f"quota error puts {hot} on cooldown")
check(raised == 0, f"...and the call falls through to a key with quota ({raised} raised of 40)")
after = dict(client2.call_counts)
for _ in range(40):
    try: client2.models.generate_content(model="m", contents="c")
    except Quota429: pass
check(client2.call_counts[hot] == after[hot], f"cooling key not picked again: {client2.call_counts}")

# --- headless scripts read .env (only app.py used to)
d = tempfile.mkdtemp()
open(f"{d}/.env", "w").write("GEMINI_API_KEY=from_env_file\nGEMINI_API_KEY_BACKUP_b=from_env_file_2\n")
out = subprocess.run([sys.executable, "-c",
    "import llm_util, json; print(json.dumps([n for n, _ in llm_util.gemini_api_keys()]))"],
    cwd=d, capture_output=True, text=True,
    env={"PATH": os.environ["PATH"], "PYTHONPATH": REPO, "HOME": os.environ.get("HOME", "/tmp")})
check("GEMINI_API_KEY" in out.stdout and "GEMINI_API_KEY_BACKUP_b" in out.stdout,
      f"a headless run picks both keys out of .env: {out.stdout.strip() or out.stderr.strip()[:120]}")
# --- which key is failing, and how often ------------------------------------
# A 500/503 is logged by run_model_ladder with the MODEL but never the key, and
# call_counts was maintained and then read by nothing, so "is one key worse than
# the others?" was unanswerable from a run log. On 2026-09-17 the fundamentals
# run had 48 model failures across 3 keys and no way to attribute a single one.
import contextlib, io

class Boom(Exception):
    def __init__(self): super().__init__("500 INTERNAL. Internal error encountered.")

client3 = llm_util.make_client()
bad = client3.key_names[1]
mapping3 = {k: n for n, k in client3._keys}
def client_for3(key):
    name = mapping3[key]
    def gen(**kw):
        if name == bad: raise Boom()
        return "ok"
    return types.SimpleNamespace(models=types.SimpleNamespace(generate_content=gen))
client3._client_for = client_for3

raised3 = 0
for _ in range(120):
    try: client3.models.generate_content(model="m", contents="c")
    except Boom: raised3 += 1

check(hasattr(client3, "failure_counts"), "the client tracks failures per key")
fc = getattr(client3, "failure_counts", {})
check(fc.get(bad, 0) == raised3 and raised3 > 0,
      f"every failure is attributed to the key that made the call ({fc.get(bad)} vs {raised3} raised)")
check(all(fc.get(n, 0) == 0 for n in client3.key_names if n != bad),
      f"...and no failure is attributed to a healthy key: {fc}")
# A 500 is not a key problem, so it must NOT disable or cool down the key --
# only auth and quota do that. Counting must stay separate from acting.
check(bad not in client3._dead and client3._cooldown_until.get(bad, 0) == 0,
      "a 500 is counted against the key but does not disable or cool it down")

summary = client3.usage_summary() if hasattr(client3, "usage_summary") else ""
check(all(n in summary for n in client3.key_names), f"the summary names every key: {summary}")
check(str(fc.get(bad, 0)) in summary and "%" in summary,
      f"...with its failure count and rate, so the worst key is visible: {summary}")
# startswith, not index(): "GEMINI_API_KEY" is a PREFIX of
# "GEMINI_API_KEY_BACKUP", so a substring position comparison finds the short
# name inside the long one and reads the order backwards.
_first_entry = summary.split("] ", 1)[-1]
check(_first_entry.startswith(bad), f"worst key first: {summary}")

# A quota error is a failure too, and the most key-attributable kind there is.
client4 = llm_util.make_client()
hot4 = client4.key_names[0]
mapping4 = {k: n for n, k in client4._keys}
def client_for4(key):
    name = mapping4[key]
    def gen(**kw):
        if name == hot4: raise Quota429()
        return "ok"
    return types.SimpleNamespace(models=types.SimpleNamespace(generate_content=gen))
client4._client_for = client_for4
for _ in range(30):
    try: client4.models.generate_content(model="m", contents="c")
    except Quota429: pass
check(getattr(client4, "failure_counts", {}).get(hot4, 0) > 0,
      f"a quota error counts against its key: {getattr(client4, 'failure_counts', None)}")

# --- the ladder's failure line names the key --------------------------------
# Every key fails here, so the log line is deterministic -- _pick is random, and
# with only one bad key of three a single rung would usually miss it.
client5 = llm_util.make_client()
client5._client_for = lambda key: types.SimpleNamespace(
    models=types.SimpleNamespace(generate_content=lambda **kw: (_ for _ in ()).throw(Boom())))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    llm_util.run_model_ladder(client5, "p", [("models/x", 0)], lambda m: None,
                              label="probe", subject="ACME")
line = buf.getvalue()
check("probe" in line and "models/x" in line, f"the ladder still logs label and model: {line.strip()[:90]}")
check(any(n in line for n in client5.key_names),
      f"...and now the key that failed, so a 500 is attributable: {line.strip()[:130]}")

# --- every job that builds a client reports the split at the end ------------
for _job in ("refresh_fundamentals.py", "refresh_expert_views.py", "news_summary.py"):
    _src = (Path(REPO) / _job).read_text()
    check("log_key_usage(" in _src, f"{_job} prints the per-key split when the run ends")

# ...and reporting it can never be what fails a run. This print happens AFTER a
# 3.5-hour job has saved its output; a client without the counters (a plain
# genai.Client, or a stub in a check) must be a no-op, not an AttributeError.
buf2 = io.StringIO()
try:
    with contextlib.redirect_stdout(buf2):
        llm_util.log_key_usage(object())
    check(buf2.getvalue() == "", "a client with no counters reports nothing and does not raise")
except Exception as e:
    check(False, f"log_key_usage must not raise on a plain client ({type(e).__name__}: {e})")
buf3 = io.StringIO()
with contextlib.redirect_stdout(buf3):
    llm_util.log_key_usage(client3)
check("[key rotation]" in buf3.getvalue(), "a rotating client reports its split")

# --- the key on a failure line must be the key that actually served it -------
# last_key_name is ONE shared attribute, and the call runs on call_with_timeout's
# daemon thread while run_model_ladder reads it back on the worker thread. With
# refresh_fundamentals.MAX_CONCURRENT_TICKERS=2 that misattributed 41% of
# failures in a 24-call reproduction -- so the key has to travel WITH the call.
class Tagged(Exception):
    def __init__(self, key):
        super().__init__("500 INTERNAL. Internal error encountered.")
        self.key = key


client6 = llm_util.make_client()
map6 = {k: n for n, k in client6._keys}


def _slow_tagged(key):
    name = map6[key]
    def gen(**kw):
        time.sleep(0.2)          # a real search is slow; that is the race window
        raise Tagged(name)
    return types.SimpleNamespace(models=types.SimpleNamespace(generate_content=gen))


client6._client_for = _slow_tagged
mismatch = [0]
seen_total = [0]
_m_lock = threading.Lock()


def _hammer():
    for _ in range(10):
        try:
            llm_util.generate_with_timeout(client6, "models/x", "p", None, timeout=5)
        except Tagged as e:
            reported = getattr(e, "_gemini_key", None)
            with _m_lock:
                seen_total[0] += 1
                if reported != e.key:
                    mismatch[0] += 1


_threads = [threading.Thread(target=_hammer) for _ in range(2)]
[t.start() for t in _threads]
[t.join() for t in _threads]
check(seen_total[0] == 20, f"all 20 concurrent calls failed as set up ({seen_total[0]})")
check(mismatch[0] == 0,
      f"every failure names the key that served it, with 2 workers ({mismatch[0]} of {seen_total[0]} wrong)")

# A TIMEOUT never returns from the call, so the key has to be recorded before it
# starts -- this is the majority failure mode (68 of 86 on 2026-09-18).
client7 = llm_util.make_client()
client7._client_for = lambda key: types.SimpleNamespace(
    models=types.SimpleNamespace(generate_content=lambda **kw: time.sleep(3)))
try:
    llm_util.generate_with_timeout(client7, "models/x", "p", None, timeout=0.3)
    check(False, "a hung call raises TimeoutError")
except TimeoutError as e:
    check(getattr(e, "_gemini_key", None) in client7.key_names,
          f"a TIMEOUT also names its key (got {getattr(e, '_gemini_key', None)})")

# A plain client has no rotation: claim nothing rather than guess.
buf7 = io.StringIO()
plain = types.SimpleNamespace(models=types.SimpleNamespace(
    generate_content=lambda **kw: (_ for _ in ()).throw(RuntimeError("nope"))))
with contextlib.redirect_stdout(buf7):
    llm_util.run_model_ladder(plain, "p", [("models/x", 0)], lambda m: None, label="plain", subject="ACME")
check("via" not in buf7.getvalue(),
      f"a non-rotating client logs no key rather than a wrong one: {buf7.getvalue().strip()[:90]}")

# --- a retry goes to a DIFFERENT key ----------------------------------------
# _pick chose at random with no exclusion, so a same-model retry reused the key
# that just failed about 1/3 of the time with three keys. The grounded-search
# ladder is now three attempts on ONE model (gemma-4-31b-it answered 0 of ~63
# calls across four runs, so a second MODEL bought nothing), which makes the key
# the only thing that varies between rungs -- it has to actually vary.
client8 = llm_util.make_client()
names8 = client8.key_names
client8._client_for = lambda key: types.SimpleNamespace(
    models=types.SimpleNamespace(generate_content=lambda **kw: "ok"))
picked = [client8._pick(avoid=names8[0])[0] for _ in range(60)]
check(names8[0] not in picked, f"_pick(avoid=X) never returns X while others are live: {set(picked)}")
check(len(set(picked)) == len(names8) - 1, f"...and still spreads over the rest: {sorted(set(picked))}")

single = llm_util.RotatingGeminiClient([("ONLY", "k")])
single._client_for = lambda key: object()
check(single._pick(avoid="ONLY")[0] == "ONLY",
      "with one key configured, avoid is ignored rather than failing the call")

# The ladder feeds each rung the key that just failed, so consecutive rungs of
# the same model land on different keys.
class Always(Exception):
    def __init__(self, key):
        super().__init__("500 INTERNAL")
        self.key = key


client9 = llm_util.make_client()
map9 = {k: n for n, k in client9._keys}
client9._client_for = lambda key: types.SimpleNamespace(
    models=types.SimpleNamespace(
        generate_content=lambda **kw: (_ for _ in ()).throw(Always(map9[key]))))
repeats = 0
for _ in range(25):
    buf9 = io.StringIO()
    with contextlib.redirect_stdout(buf9):
        llm_util.run_model_ladder(client9, "p", llm_util.same_model_tiers("models/m", backoff=0),
                                  lambda m: None, label="probe", subject="ACME")
    used = re.findall(r"failed via ([A-Z_]+)\]", buf9.getvalue())
    repeats += sum(1 for a, b in zip(used, used[1:]) if a == b)
check(repeats == 0, f"no rung reuses the key the previous rung just failed on ({repeats} repeat(s))")

# --- the grounded-search ladder is three attempts on one model ---------------
tiers8 = llm_util.same_model_tiers("models/m")
check([m for m, _ in tiers8] == ["models/m"] * 3, f"same_model_tiers gives 3 rungs on one model: {tiers8}")
check([b for _, b in tiers8][0] == 0 and all(b > 0 for b in [b for _, b in tiers8][1:]),
      f"...first immediate, the rest after a backoff: {tiers8}")

for mod in ("fundamentals_eval.py", "expert_views.py"):
    src = (Path(REPO) / mod).read_text()
    check("same_model_tiers(SEARCH_MODEL)" in src, f"{mod}'s grounded search uses same_model_tiers")
    # The search fallback MODEL is gone entirely -- the key is what varies now.
    # Neither REASONING ladder names 31b any more either -- see
    # checks/test_review_091926.py T3.
    check("SEARCH_FALLBACK_MODEL" not in src,
          f"{mod} has no search fallback model left to drift")

print("FAILURES:", fails); sys.exit(fails)
