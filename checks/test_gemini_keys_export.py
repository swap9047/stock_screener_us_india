"""Every GEMINI_API_KEY* secret reaches the AI jobs by name (owner, 2026-11-26).

Before, each AI workflow listed every key secret in its step's `env:` (Actions only exposes a secret to a
step that names it), so a new key needed a line in four files and two of eight keys added on 2026-11-26 were
never passed. Now `.github/actions/gemini-keys` takes the job's `toJSON(secrets)` and exports ONLY the
GEMINI_API_KEY* entries to $GITHUB_ENV, which llm_util then finds by name prefix as before.

E1  only the Gemini keys are exported (any case of the name, upper-cased on the way out); no other secret's value
    is written or printed, nor a key's value in the log (only its name and a count); a blank key is skipped
E2  the $GITHUB_ENV text is the multi-line-safe form: read back with GitHub's own rule it returns each value, and
    a value carrying a newline cannot define another variable
E3  a missing or malformed secrets blob exports nothing, warns, and does not fail the job
E4  end to end: the variables written are found by llm_util.gemini_api_keys (a seventh, eighth, ninth key needs
    nothing but its secret)
E5  the four AI workflows run the step before their AI step and no longer name a Gemini key themselves; no other
    workflow serialises the secrets context
E6  the action is a composite that runs the script, passing the blob through the environment; the script is stdlib only
E7  the by-hand check (check_keys.py) names each key, never prints one, and exits 1 when none is found or any fails
E8  one secret may hold every key joined by commas (GEMINI_API_KEYS, owner 2026-11-26): exported whole, the whole value
    and each key masked on its own (the runner masks only a secret's whole value), no key printed, all of them found
    by llm_util; a value with line breaks is masked key by key (a mask command is one line)
E9  the export script's split and llm_util's give the same answers; the by-hand check tries each key of a joined
    secret on its own, named by position (GEMINI_API_KEYS[2]), one call each
"""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
ACTION = REPO / ".github" / "actions" / "gemini-keys"
SCRIPT = ACTION / "export_keys.py"

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def run_export(blob, with_env_file=True):
    """Run the script as the action does. Returns (returncode, stdout, text written to $GITHUB_ENV)."""
    tmp = tempfile.mkdtemp(prefix="gkeys_")
    env_file = os.path.join(tmp, "github_env")
    open(env_file, "w").close()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GEMINI_API_KEY")}
    env["ALL_SECRETS"] = blob if isinstance(blob, str) else json.dumps(blob)
    if with_env_file:
        env["GITHUB_ENV"] = env_file
    else:
        env.pop("GITHUB_ENV", None)
    r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr, open(env_file).read()


def parse_github_env(text):
    """GitHub's parser for the file: NAME=value lines, and NAME<<DELIM ... DELIM blocks."""
    out, lines, i = {}, text.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line and "=" not in line.split("<<")[0]:
            name, delim = line.split("<<", 1)
            body = []
            i += 1
            while i < len(lines) and lines[i] != delim:
                body.append(lines[i])
                i += 1
            out[name] = "\n".join(body)
        elif "=" in line:
            name, value = line.split("=", 1)
            out[name] = value
        i += 1
    return out


# --- E1 ---------------------------------------------------------------------------------------
KEYS = {"GEMINI_API_KEY": "AIzaPRIMARY0000000000000000000000000001",
        "GEMINI_API_KEY_BACKUP_B5": "AIzaFIVE000000000000000000000000000005",
        "gemini_api_key_backup_b6": "AIzaSIX0000000000000000000000000000006"}
OTHERS = {"DATA_REPO_TOKEN": "ghp_dataRepoTokenValue123456", "MARKET_DB_URL": "postgresql://u:p@h/db",
          "DISCORD_WEBHOOK_URL": "https://discord.example/api/webhooks/1/xyz", "github_token": "ghs_runnerToken987",
          "MARKET_API_TOKEN": "marketApiTokenValue", "GEMINI_API_KEY_BACKUP_B9": "   "}
rc, out, envtext = run_export({**KEYS, **OTHERS})
got = parse_github_env(envtext)
check(rc == 0 and set(got) == {"GEMINI_API_KEY", "GEMINI_API_KEY_BACKUP_B5", "GEMINI_API_KEY_BACKUP_B6"},
      f"E1: exactly the Gemini keys are exported, names upper-cased ({sorted(got)})")
check(got["GEMINI_API_KEY_BACKUP_B6"] == KEYS["gemini_api_key_backup_b6"] and got["GEMINI_API_KEY"] == KEYS["GEMINI_API_KEY"],
      "E1: ...with their values")
leaked = [n for n, v in OTHERS.items() if v.strip() and (v in envtext or v in out)]
check(not leaked, f"E1: no other secret's value is written or printed ({leaked})")
printed = [n for n, v in KEYS.items() if v in re.sub(r"::add-mask::\S+", "", out)]
check(not printed, f"E1: no key's value is printed (apart from the ::add-mask:: command the runner consumes) ({printed})")
check(out.count("::add-mask::") == 3 and "exported to the job: 3" in out and "GEMINI_API_KEY_BACKUP_B5" in out,
      "E1: each value is masked, and the log says how many and which names")
check("B9" not in envtext, "E1: a blank key secret is skipped")

# --- E2 ---------------------------------------------------------------------------------------
tricky = {"GEMINI_API_KEY_X": "abc\nINJECTED=1\nPATH=/evil", "GEMINI_API_KEY_Y": "plain"}
rc, out, envtext = run_export(tricky)
got = parse_github_env(envtext)
check(got.get("GEMINI_API_KEY_X") == tricky["GEMINI_API_KEY_X"] or got.get("GEMINI_API_KEY_X") == tricky["GEMINI_API_KEY_X"].strip(),
      "E2: a multi-line value is read back whole")
check("INJECTED" not in got and "PATH" not in got and set(got) == {"GEMINI_API_KEY_X", "GEMINI_API_KEY_Y"},
      f"E2: ...and cannot define other variables ({sorted(got)})")

# --- E3 ---------------------------------------------------------------------------------------
for label, blob in (("empty", ""), ("not JSON", "{oops"), ("a JSON list", "[1, 2]"), ("no Gemini secret", {"DATA_REPO_TOKEN": "x"})):
    rc, out, envtext = run_export(blob)
    check(rc == 0 and envtext == "" and "::warning::" in out and "exported to the job: 0" in out,
          f"E3: {label} -> nothing exported, a warning, the job continues")
rc, out, _ = run_export({"GEMINI_API_KEY": "k"}, with_env_file=False)
check(rc == 1 and "::error::" in out, "E3: keys found but no $GITHUB_ENV is a loud failure, never a silent drop")

# --- E4 ---------------------------------------------------------------------------------------
import llm_util

many = {f"GEMINI_API_KEY_BACKUP_B{n}": f"AIzaKEY{n:02d}" + "x" * 30 for n in range(2, 12)}
many["GEMINI_API_KEY"] = "AIzaPRIMARY" + "y" * 30
rc, out, envtext = run_export({**many, **{k: v for k, v in OTHERS.items() if k != "GEMINI_API_KEY_BACKUP_B9"}})
exported = parse_github_env(envtext)
saved = {k: v for k, v in os.environ.items()}
try:
    for k in [k for k in os.environ if k.startswith("GEMINI_API_KEY")]:
        del os.environ[k]
    os.environ.update(exported)
    pool = llm_util.gemini_api_keys()
finally:
    for k in [k for k in os.environ if k.startswith("GEMINI_API_KEY")]:
        del os.environ[k]
    os.environ.update({k: v for k, v in saved.items() if k.startswith("GEMINI_API_KEY")})
check(len(pool) == 11 and pool[0][0] == "GEMINI_API_KEY",
      f"E4: llm_util finds all {len(pool)} exported keys, the primary first (a 7th, 8th ... 11th key needs only its secret)")

# --- E5 ---------------------------------------------------------------------------------------
WF = REPO / ".github" / "workflows"
AI = {"news-summary.yml": "Build news summary and send to Discord", "fundamentals.yml": "Generate AI Fundamental Views",
      "expert-views.yml": "Generate AI Expert Views", "market-read.yml": "Write the market reads"}
for wf, ai_step in AI.items():
    doc = yaml.safe_load((WF / wf).read_text())
    steps = next(j["steps"] for j in doc["jobs"].values() if any(s.get("name") == ai_step for s in j["steps"]))
    names = [s.get("name") for s in steps]
    exp = next((s for s in steps if s.get("uses") == "./.github/actions/gemini-keys"), None)
    check(exp is not None and steps.index(exp) < names.index(ai_step) and exp["with"] == {"secrets": "${{ toJSON(secrets) }}"},
          f"E5: {wf}: the keys are exported before '{ai_step}'")
    check(not re.search(r"secrets\.GEMINI_API_KEY", (WF / wf).read_text()),
          f"E5: {wf}: no Gemini key is named any more")
users = sorted(p.name for p in WF.glob("*.yml") if "toJSON(secrets)" in p.read_text())
check(users == sorted(list(AI) + ["gemini-keys-check.yml"]),
      f"E5: only the four AI workflows and the by-hand key check serialise the secrets context ({users})")

# --- E6 ---------------------------------------------------------------------------------------
act = yaml.safe_load((ACTION / "action.yml").read_text())
step = act["runs"]["steps"][0]
check(act["runs"]["using"] == "composite" and "export_keys.py" in step["run"] and step["shell"] == "bash"
      and step["env"]["ALL_SECRETS"] == "${{ inputs.secrets }}" and act["inputs"]["secrets"]["required"] is True,
      "E6: a composite action runs the script, the blob arrives through the environment")
imports = {n.names[0].name.split(".")[0] for n in ast.walk(ast.parse(SCRIPT.read_text())) if isinstance(n, ast.Import)}
imports |= {n.module.split(".")[0] for n in ast.walk(ast.parse(SCRIPT.read_text())) if isinstance(n, ast.ImportFrom)}
check(imports <= {"json", "os", "re", "secrets", "sys"}, f"E6: the script is stdlib only and imports nothing that could echo a secret ({sorted(imports)})")
check("print(f\"::add-mask::{mask}\")" in SCRIPT.read_text() and SCRIPT.read_text().count("print(") == 4,
      "E6: the script prints only the mask commands, the count line and the two notices (no value anywhere else)")

# --- E7 ---------------------------------------------------------------------------------------
import importlib.util
import io
import types as _types
from contextlib import redirect_stdout

spec = importlib.util.spec_from_file_location("check_keys", ACTION / "check_keys.py")
ck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ck)


def run_check(env_keys, failing=()):
    class FakeModels:
        def __init__(self, key): self.key = key
        def generate_content(self, **kw):
            if self.key in failing:
                raise RuntimeError("429 RESOURCE_EXHAUSTED. quota")
            return _types.SimpleNamespace(text="OK")
    fake_genai = _types.ModuleType("google.genai")
    fake_genai.Client = lambda api_key, http_options=None: _types.SimpleNamespace(models=FakeModels(api_key))
    fake_genai.types = _types.SimpleNamespace(HttpOptions=lambda **k: None, GenerateContentConfig=lambda **k: None)
    google = _types.ModuleType("google"); google.genai = fake_genai
    saved_mods = {m: sys.modules.get(m) for m in ("google", "google.genai", "google.genai.types")}
    sys.modules.update({"google": google, "google.genai": fake_genai, "google.genai.types": fake_genai.types})
    saved_env = dict(os.environ)
    for k in [k for k in os.environ if k.startswith("GEMINI_API_KEY")]:
        del os.environ[k]
    os.environ.update(env_keys)
    ck.time.sleep = lambda s: None
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = ck.main()
    finally:
        os.environ.clear(); os.environ.update(saved_env)
        for m, v in saved_mods.items():
            if v is None: sys.modules.pop(m, None)
            else: sys.modules[m] = v
    return rc, buf.getvalue()


rc, out = run_check({"GEMINI_API_KEY": "SECRETVALUE1", "GEMINI_API_KEY_BACKUP_B5": "SECRETVALUE2"})
check(rc == 0 and "2 Gemini key(s)" in out and "GEMINI_API_KEY_BACKUP_B5: ok" in out and "SECRETVALUE" not in out,
      "E7: every key answers -> exit 0, each named, no value printed")
rc, out = run_check({"GEMINI_API_KEY": "SECRETVALUE1", "GEMINI_API_KEY_BACKUP_B5": "SECRETVALUE2"}, failing={"SECRETVALUE2"})
check(rc == 1 and "GEMINI_API_KEY_BACKUP_B5: FAILED 429 RESOURCE_EXHAUSTED" in out and "SECRETVALUE" not in out and "1 of 2" in out,
      "E7: a key that fails -> exit 1, named with the reason, no value printed")
rc, out = run_check({})
check(rc == 1 and "none" in out and "::error::no GEMINI_API_KEY" in out, "E7: no key at all -> exit 1")
rc, out = run_check({"GEMINI_API_KEY": "SAMEVALUE", "GEMINI_API_KEY_BACKUP": "SAMEVALUE"})
check("::warning::1 key(s) hold the same value" in out, "E7: a key pasted twice is called out")

# --- E8 ---------------------------------------------------------------------------------------
JOINED_KEYS = [f"AIzaJOINED{n:02d}" + "q" * 28 for n in range(1, 9)]
joined = ",".join(JOINED_KEYS)
visible = lambda text: re.sub(r"::add-mask::\S+", "", text)
rc, out, envtext = run_export({"GEMINI_API_KEYS": joined, **OTHERS})
got = parse_github_env(envtext)
check(rc == 0 and got == {"GEMINI_API_KEYS": joined}, "E8: a joined secret is exported whole, under its name")
masks = re.findall(r"::add-mask::(\S+)", out)
check(joined in masks and set(JOINED_KEYS) <= set(masks),
      "E8: the whole value AND each of the 8 keys is masked (the runner masks only a secret's whole value)")
check(not any(k in visible(out) for k in JOINED_KEYS) and joined not in visible(out),
      "E8: no key is printed other than inside a mask command")
check("exported to the job: 1 (GEMINI_API_KEYS); 8 key(s) in all" in out, "E8: the log says one secret, 8 keys, never a value")
check(not [n for n, v in OTHERS.items() if v.strip() and (v in envtext or v in out)], "E8: no other secret's value is written or printed")

# a pasted list with line breaks: a mask command is one line, so only the keys are masked, never the whole value
lines = "\n".join(JOINED_KEYS[:3])
rc, out, envtext = run_export({"GEMINI_API_KEYS": lines})
check(rc == 0 and re.findall(r"::add-mask::(\S+)", out) == JOINED_KEYS[:3] and not any(k in visible(out) for k in JOINED_KEYS[:3]),
      "E8: a value with line breaks is masked key by key and no key reaches the log as plain text")

# all of them, end to end, from ONE secret (and a key also listed on its own counts once)
exported = parse_github_env(run_export({"GEMINI_API_KEYS": joined, "GEMINI_API_KEY": JOINED_KEYS[2]})[2])
saved = {k: v for k, v in os.environ.items() if k.startswith("GEMINI_API_KEY")}
try:
    for k in [k for k in os.environ if k.startswith("GEMINI_API_KEY")]:
        del os.environ[k]
    os.environ.update(exported)
    pool2 = llm_util.gemini_api_keys()
finally:
    for k in [k for k in os.environ if k.startswith("GEMINI_API_KEY")]:
        del os.environ[k]
    os.environ.update(saved)
check([k for _, k in pool2].count(JOINED_KEYS[2]) == 1 and sorted(k for _, k in pool2) == sorted(JOINED_KEYS)
      and pool2[0] == ("GEMINI_API_KEY", JOINED_KEYS[2]) and len({n for n, _ in pool2}) == 8,
      f"E8: llm_util finds all 8 keys from the one secret, a key also listed on its own counted once ({[n for n, _ in pool2]})")

# --- E9 ---------------------------------------------------------------------------------------
sys.path.insert(0, str(ACTION))
import export_keys

SPLIT_CASES = ["", "  ", "k", " k ", "a,b", " a , b ,, c ,", "a\nb", "a, b\n c,\n\nd", ",", "a,,b", "\n", None]
disagree = [c for c in SPLIT_CASES if export_keys.split_keys(c) != llm_util.split_key_list(c)]
check(not disagree, f"E9: export_keys.split_keys and llm_util.split_key_list agree on every case ({disagree})")
check(llm_util.split_key_list(" a , b ,, c ,") == ["a", "b", "c"] and llm_util.split_key_list("k") == ["k"]
      and llm_util.split_key_list("") == [] and llm_util.split_key_list(None) == [],
      "E9: spaces, blanks and a trailing comma are ignored; a plain key is itself; empty is none")

calls_made = []
# the by-hand check on a joined secret: each key tried on its own, one call each, named by position
def run_check2(env_keys, failing=()):
    class FakeModels:
        def __init__(self, key): self.key = key
        def generate_content(self, **kw):
            calls_made.append((self.key, kw.get("model")))
            if self.key in failing:
                raise RuntimeError("400 INVALID_ARGUMENT. API key not valid. [reason: API_KEY_INVALID]")
            return _types.SimpleNamespace(text="OK")
    fake_genai = _types.ModuleType("google.genai")
    fake_genai.Client = lambda api_key, http_options=None: _types.SimpleNamespace(models=FakeModels(api_key))
    fake_genai.types = _types.SimpleNamespace(HttpOptions=lambda **k: None, GenerateContentConfig=lambda **k: None)
    google = _types.ModuleType("google"); google.genai = fake_genai
    saved_mods = {m: sys.modules.get(m) for m in ("google", "google.genai", "google.genai.types")}
    sys.modules.update({"google": google, "google.genai": fake_genai, "google.genai.types": fake_genai.types})
    saved_env = dict(os.environ)
    for k in [k for k in os.environ if k.startswith("GEMINI_API_KEY")]:
        del os.environ[k]
    os.environ.update(env_keys)
    ck.time.sleep = lambda s: None
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = ck.main()
    finally:
        os.environ.clear(); os.environ.update(saved_env)
        for m, v in saved_mods.items():
            if v is None: sys.modules.pop(m, None)
            else: sys.modules[m] = v
    return rc, buf.getvalue()

three = ["SECRETKEYONE", "SECRETKEYTWO", "SECRETKEYTHREE"]
calls_made.clear()
rc, out = run_check2({"GEMINI_API_KEYS": " , ".join(three)})
check(rc == 0 and "3 Gemini key(s) in this job: GEMINI_API_KEYS[1], GEMINI_API_KEYS[2], GEMINI_API_KEYS[3]" in out
      and "GEMINI_API_KEYS[3]: ok" in out and "SECRETKEY" not in out and "All 3 keys answered" in out,
      "E9: a joined secret's keys are each tried and named by position; no value printed")
check([k for k, _ in calls_made] == three and {m for _, m in calls_made} == {"models/gemini-3.5-flash-lite"},
      f"E9: exactly one call per key, each on gemini-3.5-flash-lite ({len(calls_made)} calls)")
calls_made.clear()
rc, out = run_check2({"GEMINI_API_KEYS": ",".join(three)}, failing={"SECRETKEYTWO"})
check(rc == 1 and "GEMINI_API_KEYS[2]: FAILED 400 API_KEY_INVALID" in out and "1 of 3" in out and "SECRETKEY" not in out
      and len(calls_made) == 3, "E9: the key that fails is named by its place in the list, one call each, exit 1")
rc, out = run_check2({"GEMINI_API_KEYS": "SECRETKEYONE", "GEMINI_API_KEY": "SECRETKEYONE"})
check("2 Gemini key(s)" in out and "::warning::1 key(s) hold the same value" in out,
      "E9: a key listed on its own and in the joined secret is called out as a repeat")
rc, out = run_check2({"GEMINI_API_KEYS": " , ,"})
check(rc == 1 and "none" in out and "::error::no GEMINI_API_KEY" in out, "E9: a joined secret with no key in it counts as none")

print("FAILURES:", fails)
sys.exit(fails)
