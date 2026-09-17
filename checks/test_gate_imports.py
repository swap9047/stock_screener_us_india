"""The lightweight workflow jobs must stay importable without the analysis stack.

WHY: daily-alerts.yml's `gate` job installs ONLY `requests` -- deliberately, so
the cheap "is any rule due today?" question doesn't pay for pandas/yfinance
before the real check job starts. On 2026-09-15 `alerts.load_rules()` started
calling `stock_data.read_json_strict`, and stock_data imports numpy at module
scope, so the gate died with:

    ModuleNotFoundError: No module named 'numpy'

A failing gate skips the work job, so NO alert fired for a full day and the
9 PM ET slot expired unserved. Nothing caught it: every other workflow installs
requirements.txt, and every local run has numpy on the path.

This runs the gate's real code with the heavy modules blocked at import time, so
CI reproduces the gate's environment on a machine that has them installed.
Add any other lightweight job's entry point to GATE_SNIPPETS.
"""

import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO)

import subprocess

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# Everything pip-installed by `gate` is `requests` plus the standard library.
# Anything else must not be reachable from the code that job runs.
BLOCKED = ("numpy", "pandas", "yfinance", "streamlit", "google", "dotenv",
           "streamlit_sortables")

BLOCKER = f'''
import sys
BLOCKED = {BLOCKED!r}


class _Blocker:
    """Make the heavy modules look uninstalled, exactly as they are on the gate."""

    def find_module(self, name, path=None):
        return self.find_spec(name, path)

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"No module named {{name.split('.')[0]!r}}")
        return None


sys.meta_path.insert(0, _Blocker())
sys.path.insert(0, {REPO!r})
'''

# (label, the code the workflow step actually runs). Keep these byte-comparable
# to the workflow, so a change there that needs a new dependency fails here.
GATE_SNIPPETS = [
    ("daily-alerts gate: load_rules + is_rule_due", """
from alerts import load_rules, is_rule_due
rules = load_rules()
due = any(r.get("enabled", True) and r.get("conditions") and is_rule_due(r) for r in rules)
print(f"due={'true' if due else 'false'}")
"""),
    ("alerts imports at all", "import alerts"),
    ("json_store is stdlib-only", "import json_store; json_store.read_json_strict('/nonexistent.json', [])"),
]

for label, snippet in GATE_SNIPPETS:
    proc = subprocess.run([sys.executable, "-c", BLOCKER + snippet],
                          capture_output=True, text=True, cwd=REPO)
    ok = proc.returncode == 0
    check(ok, f"{label} -- runs with {'/'.join(BLOCKED[:3])} unavailable")
    if not ok:
        print("     " + (proc.stderr.strip().splitlines() or ["(no stderr)"])[-1])

# The blocker itself must work, or every check above passes vacuously.
proc = subprocess.run([sys.executable, "-c", BLOCKER + "import numpy"],
                      capture_output=True, text=True, cwd=REPO)
check(proc.returncode != 0 and "No module named" in proc.stderr,
      "the blocker really does hide numpy (guards against a vacuous pass)")

# stock_data must keep re-exporting the primitives, or every existing
# `from stock_data import read_json_strict` call site breaks.
import stock_data
import json_store

check(stock_data.read_json_strict is json_store.read_json_strict,
      "stock_data re-exports read_json_strict")
check(stock_data.atomic_write_json is json_store.atomic_write_json,
      "stock_data re-exports atomic_write_json")
check(stock_data.DataFileError is json_store.DataFileError,
      "stock_data re-exports DataFileError (isinstance checks keep working)")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
