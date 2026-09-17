"""Key discovery and per-call rotation across three or more Gemini keys."""

import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
GATE = str(Path(__file__).resolve().parents[1] / ".github/actions/slot-gate")
sys.path.insert(0, REPO)
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, GATE)

import os, subprocess, sys, tempfile, types

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
print("FAILURES:", fails); sys.exit(fails)
