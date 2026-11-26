"""The Gemini keys reach the AI jobs by name (owner, 2026-11-26).

Actions only exposes a secret to a step that names it. A first try (PR 64) found the keys by prefix from
`${{ toJSON(secrets) }}`, so a new key needed only its secret -- but GitHub HOLDS BACK any workflow that serialises
the whole secrets context (the run is parked as `action_required`, with no jobs, until someone approves it): the
by-hand key check never started, and the four AI workflows would have been held on their 9 PM slot. Probed on a
throwaway branch: the same trivial workflow ran with a plain step and was held with `toJSON(secrets)`. So every
workflow names its keys, one line each, with spare slots through _B9 (an unset secret comes through blank and
llm_util ignores it, so a key added under the next free name needs no workflow edit).

G1  the four AI workflows and the by-hand check pass the SAME key names, each `NAME: ${{ secrets.NAME }}`, to the
    step that runs the AI script (not to some other step): the primary, the backup and _B, _B2 .. _B9
G2  no workflow serialises the secrets context (toJSON(secrets)) -- GitHub would hold the run -- and no leftover
    gemini-keys action or export script remains
G3  end to end: the names the workflows pass are found by llm_util.gemini_api_keys, primary first
G4  the by-hand check (scripts/check_gemini_keys.py) names each key, never prints one, and exits 1 when none is
    found or any fails
"""

import os
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
WF = REPO / ".github" / "workflows"

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


EXPECTED = ["GEMINI_API_KEY", "GEMINI_API_KEY_BACKUP", "GEMINI_API_KEY_BACKUP_B"] + [
    f"GEMINI_API_KEY_BACKUP_B{i}" for i in range(2, 10)]
# workflow -> a word in the run line of the step that gets the keys
STEPS = {"news-summary.yml": "news_check.py", "fundamentals.yml": "refresh_fundamentals.py",
         "expert-views.yml": "refresh_expert_views.py", "market-read.yml": "refresh_market_read.py",
         "gemini-keys-check.yml": "check_gemini_keys.py"}

# --- G1 ---------------------------------------------------------------------------------------
for name, script in STEPS.items():
    wf = yaml.safe_load((WF / name).read_text())
    steps = [s for job in wf["jobs"].values() for s in job.get("steps", []) if script in s.get("run", "")]
    check(len(steps) == 1, f"G1: {name} has one step running {script}")
    env = (steps[0].get("env") or {}) if steps else {}
    passed = {k: v for k, v in env.items() if k.startswith("GEMINI_API_KEY")}
    check(sorted(passed) == sorted(EXPECTED), f"G1: {name} passes exactly the expected key names ({len(passed)})")
    check(all(v == "${{ secrets." + k + " }}" for k, v in passed.items()),
          f"G1: {name}: each key is its own secret, NAME: ${{{{ secrets.NAME }}}}")
    others = [s.get("name") for job in wf["jobs"].values() for s in job.get("steps", [])
              if script not in s.get("run", "") and any(k.startswith("GEMINI_API_KEY") for k in (s.get("env") or {}))]
    check(not others, f"G1: {name}: no other step gets the keys ({others})")

# --- G2 ---------------------------------------------------------------------------------------
for path in sorted(list(WF.glob("*.yml")) + list((REPO / ".github" / "actions").rglob("*.yml"))):
    text = path.read_text()
    check("toJSON(secrets)" not in text and "secrets: inherit" not in text,
          f"G2: {path.name} does not serialise the secrets context (GitHub holds such a run)")
check(not (REPO / ".github" / "actions" / "gemini-keys").exists(), "G2: the gemini-keys action is gone")

# --- G3 ---------------------------------------------------------------------------------------
import llm_util  # noqa: E402

saved = dict(os.environ)
try:
    for k in [k for k in os.environ if k.startswith("GEMINI_API_KEY")]:
        del os.environ[k]
    for i, n in enumerate(reversed(EXPECTED)):                     # set in an odd order on purpose
        os.environ[n] = f"value-{i}"
    os.environ["GEMINI_API_KEY_BACKUP_B7"] = ""                    # an unset secret arrives blank
    found = llm_util.gemini_api_keys()
finally:
    os.environ.clear()
    os.environ.update(saved)
check([n for n, _ in found][:2] == ["GEMINI_API_KEY", "GEMINI_API_KEY_BACKUP"] and len(found) == len(EXPECTED) - 1
      and "GEMINI_API_KEY_BACKUP_B7" not in [n for n, _ in found],
      "G3: llm_util finds every named key, primary and backup first, and ignores a blank one")

# --- G4 ---------------------------------------------------------------------------------------
import importlib.util
import io
import types as _types
from contextlib import redirect_stdout

spec = importlib.util.spec_from_file_location("check_gemini_keys", REPO / "scripts" / "check_gemini_keys.py")
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
      "G4: every key answers -> exit 0, each named, no value printed")
rc, out = run_check({"GEMINI_API_KEY": "SECRETVALUE1", "GEMINI_API_KEY_BACKUP_B5": "SECRETVALUE2"}, failing={"SECRETVALUE2"})
check(rc == 1 and "GEMINI_API_KEY_BACKUP_B5: FAILED 429 RESOURCE_EXHAUSTED" in out and "SECRETVALUE" not in out and "1 of 2" in out,
      "G4: a key that fails -> exit 1, named with the reason, no value printed")
rc, out = run_check({})
check(rc == 1 and "none" in out and "::error::no GEMINI_API_KEY" in out, "G4: no key at all -> exit 1")
rc, out = run_check({"GEMINI_API_KEY": "SAMEVALUE", "GEMINI_API_KEY_BACKUP": "SAMEVALUE"})
check("::warning::1 key(s) hold the same value" in out, "G4: a key pasted twice is called out")

print("FAILURES:", fails)
sys.exit(fails)
