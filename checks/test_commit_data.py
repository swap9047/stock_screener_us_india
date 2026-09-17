"""commit-data must not move a timestamped data file backwards.

WHY: the action used `git rebase -X theirs origin/main`. In a REBASE "theirs" is
the commit being replayed, so this run's copy won every conflict. Fine for a
file one workflow owns -- but data_snapshot.json is committed by BOTH
data-refresh.yml and expert-views.yml (which runs refresh_data.py first), and
they sit in different concurrency groups, so they can overlap. The loser's
snapshot was silently discarded and an older as_of could land over a newer one.

The action now re-copies onto the current tip each attempt and asks
newer_on_branch.py whether the branch already has something fresher. These
checks cover that helper's decision table and assert the action no longer
rebases. The end-to-end bash was exercised against real git repos by hand; what
CI can protect cheaply is the decision logic and the shape of the step.

Offline: a temp git repo, no network.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import json
import os
import subprocess
import tempfile

import yaml

ACTION_DIR = REPO / ".github/actions/commit-data"
HELPER = ACTION_DIR / "newer_on_branch.py"

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


# --- a repo whose origin/main holds a known copy of each file --------------
work = tempfile.mkdtemp()
bare = os.path.join(work, "remote.git")
clone = os.path.join(work, "clone")
subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
subprocess.run(["git", "clone", "-q", bare, clone], check=True)
git("config", "user.email", "t@t.t", cwd=clone)
git("config", "user.name", "t", cwd=clone)

ON_BRANCH = {
    "data_snapshot.json": {"as_of": "2026-09-17 10:00 ET", "rows": "branch"},
    "market_breadth.json": {"generated_at": "2026-09-17T10:00:00+00:00"},
    "alert_state.json": {"r1:ACME": {"was_active": True}},          # no stamp at all
}
for name, body in ON_BRANCH.items():
    with open(os.path.join(clone, name), "w") as f:
        json.dump(body, f)
git("add", "-A", cwd=clone)
git("commit", "-qm", "seed", cwd=clone)
git("branch", "-M", "main", cwd=clone)
git("push", "-q", "origin", "main", cwd=clone)
git("fetch", "-q", "origin", "main", cwd=clone)


def decides_keep_branch(repo_file, local_body):
    """True when the helper says 'the branch copy is newer, keep it'."""
    local = os.path.join(work, "local.json")
    with open(local, "w") as f:
        if isinstance(local_body, str):
            f.write(local_body)
        else:
            json.dump(local_body, f)
    r = subprocess.run([sys.executable, str(HELPER), repo_file, local],
                       cwd=clone, capture_output=True, text=True)
    return r.returncode == 0


# --- the decision table ----------------------------------------------------
check(decides_keep_branch("data_snapshot.json", {"as_of": "2026-09-17 09:00 ET"}),
      "an OLDER as_of is held back (this is the race that lost a snapshot)")
check(not decides_keep_branch("data_snapshot.json", {"as_of": "2026-09-17 11:00 ET"}),
      "a NEWER as_of is pushed")
check(not decides_keep_branch("data_snapshot.json", {"as_of": "2026-09-17 10:00 ET"}),
      "an EQUAL as_of is pushed (same run re-committing must not be blocked)")
check(decides_keep_branch("market_breadth.json", {"generated_at": "2026-09-17T09:00:00+00:00"}),
      "generated_at is compared too, not just as_of")

# Everything we cannot prove stale must still push -- losing a run's work is the
# worse failure, so the helper fails toward committing.
check(not decides_keep_branch("alert_state.json", {"r1:ACME": {"was_active": False}}),
      "a file with no timestamp on either side always pushes")
check(not decides_keep_branch("data_snapshot.json", {"rows": "no stamp here"}),
      "ours carrying no stamp pushes")
check(not decides_keep_branch("brand_new.json", {"as_of": "2026-01-01 00:00 ET"}),
      "a file not yet on the branch pushes")
check(not decides_keep_branch("data_snapshot.json", "{not json"),
      "unparseable local content pushes rather than being silently dropped")
check(not decides_keep_branch("data_snapshot.json", {"as_of": ""}),
      "an empty stamp is treated as no stamp")

r = subprocess.run([sys.executable, str(HELPER)], cwd=clone, capture_output=True, text=True)
check(r.returncode == 1, "missing arguments -> push (never a crash that fails the step)")

# --- the helper must stay stdlib-only: the commit step pip-installs nothing --
blocked = "numpy,pandas,yfinance,streamlit,requests,google,yaml"
probe = (f"import sys\n"
         f"class B:\n"
         f"    def find_spec(self, name, path=None, target=None):\n"
         f"        if name.split('.')[0] in {blocked.split(',')!r}:\n"
         f"            raise ImportError(name)\n"
         f"        return None\n"
         f"sys.meta_path.insert(0, B())\n"
         f"sys.argv = ['x', 'data_snapshot.json', '/nonexistent']\n"
         f"exec(open({str(HELPER)!r}).read())\n")
r = subprocess.run([sys.executable, "-c", probe], cwd=clone, capture_output=True, text=True)
check("ImportError" not in r.stderr, f"newer_on_branch.py is stdlib-only ({r.stderr.strip()[:60]})")

# --- the action itself ------------------------------------------------------
action = yaml.safe_load(open(ACTION_DIR / "action.yml"))
body = action["runs"]["steps"][0]["run"]
code_lines = [ln for ln in body.splitlines() if not ln.strip().startswith("#")]
code = "\n".join(code_lines)
check("git rebase" not in code, "the action no longer rebases (-X theirs made ours win every conflict)")
check("git reset --hard origin/main" in code, "each attempt restarts from the current branch tip")
check("newer_on_branch.py" in code, "each attempt re-checks freshness against the new tip")
check("::error::" in code and "exit 1" in code, "a push that never lands still fails the step")
check(action["runs"]["steps"][0].get("shell") == "bash", "runs under bash (pipefail keeps exit status)")
check("ACTION_PATH" in (action["runs"]["steps"][0].get("env") or {}),
      "the step exports ACTION_PATH so it can find the helper")

# the embedded bash must at least parse
sh = os.path.join(work, "step.sh")
open(sh, "w").write("set -eo pipefail\n" + body)
r = subprocess.run(["bash", "-n", sh], capture_output=True, text=True)
check(r.returncode == 0, f"the embedded bash parses ({r.stderr.strip()[:80]})")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
