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
check(imports <= {"json", "os", "secrets", "sys"}, f"E6: the script is stdlib only and imports nothing that could echo a secret ({sorted(imports)})")
check("print(f\"::add-mask::{value}\")" in SCRIPT.read_text() and SCRIPT.read_text().count("print(") == 4,
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

print("FAILURES:", fails)
sys.exit(fails)
