"""Which pushes deploy to Vercel (vercel.json's ignoreCommand, scripts/vercel-ignore.sh), 2026-10-29.

Vercel's Git integration deployed every commit on every branch: 27 PRs in 11 days, each push a copy of the
two Python functions (pandas, numpy, yfinance, ...), until the free team's Function Storage hit 75% of 10 GB
(and past 10 GB Vercel deletes old deployments by itself). Now a deployment is made only for `main`, and only
when a file the two functions need changed. The live data (snapshot, breadth, prices) never goes through
Vercel -- it is committed to the data repo and written to the research library's database -- so skipping a
deployment never holds data back; it only matters for code the functions run.

This holds the rule to two things: the list of files it watches covers everything api/market.py and
api/mcp.py import (a module added to their imports without being added to the list would make a push
that changes it skip the deployment it needs), and the command itself, run with sh in a throwaway git
repo, decides as intended -- including the cases where the safe answer is to build.
"""

import ast
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

fails = 0
def check(ok, label):
    global fails; fails += not ok; print("PASS" if ok else "FAIL", label)

cfg = json.loads((REPO / "vercel.json").read_text())
cmd = cfg.get("ignoreCommand", "")
SCRIPT = REPO / "scripts" / "vercel-ignore.sh"
check(cmd == "sh scripts/vercel-ignore.sh" and SCRIPT.exists() and [b["src"] for b in cfg["builds"]] == ["api/market.py", "api/mcp.py"],
      "vercel.json's ignoreCommand runs the script (not a long one-liner) and still builds only the two functions")

# --- the watched paths cover everything the functions import ---------------------------------
import re
_w = re.search(r'^WATCHED="([^"]*)"', SCRIPT.read_text(), re.M)
watched = shlex.split(_w.group(1)) if _w else []
local = {p.stem for p in REPO.glob("*.py")}

def imported(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names & local

needed, todo = set(), ["api/market.py", "api/mcp.py"]
while todo:
    rel = todo.pop()
    if rel in needed:
        continue
    needed.add(rel)
    todo += [m + ".py" for m in imported(REPO / rel)]
missing = sorted(f for f in needed if f not in watched and not f.startswith("api/"))
check("api" in watched and not missing,
      f"the watched files cover every module the two functions import ({len(needed) - 2} of them)" + (f"; missing {missing}" if missing else ""))
check(len(cmd) <= 256, f"the ignoreCommand is {len(cmd)} characters: Vercel rejects one over 256 and fails the deployment")
check(all(f in watched for f in ("vercel.json", ".vercelignore", "requirements.txt")),
      "a change to the Vercel config or the dependency list deploys")
check(all((REPO / f).exists() for f in watched), "every watched path exists (a renamed file would silently stop deploying)")
check("app.py" not in watched and "refresh_data.py" not in watched and "publish_market.py" not in watched,
      "the dashboard and the jobs, which the functions never import, are not watched")

# --- the command's decisions, in a throwaway git repo ------------------------------------------
def run(*args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", **(env or {})})

def commit(cwd, files, msg):
    for rel, text in files.items():
        p = Path(cwd, rel); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
    run("git", "add", "-A", cwd=cwd); run("git", "commit", "-q", "-m", msg, cwd=cwd)
    return run("git", "rev-parse", "HEAD", cwd=cwd).stdout.strip()

def decide(cwd, ref, previous=None):
    """0 = Vercel skips the build, anything else = it builds."""
    env = {k: v for k, v in os.environ.items() if k != "VERCEL_GIT_PREVIOUS_SHA"}
    env["VERCEL_GIT_COMMIT_REF"] = ref
    if previous:
        env["VERCEL_GIT_PREVIOUS_SHA"] = previous
    return subprocess.run(["sh", "-c", cmd], cwd=cwd, env=env, capture_output=True, text=True).returncode

def with_script(cwd):
    Path(cwd, "scripts").mkdir(exist_ok=True)
    Path(cwd, "scripts", "vercel-ignore.sh").write_text(SCRIPT.read_text())

with tempfile.TemporaryDirectory() as d:
    run("git", "init", "-q", "-b", "main", cwd=d)
    with_script(d)
    base = commit(d, {"api/market.py": "1", "market_lookup.py": "1", "app.py": "1", "README.md": "1", "checks/t.py": "1",
                      "vercel.json": "{}", "requirements.txt": "x"}, "base")
    c_app = commit(d, {"app.py": "2"}, "dashboard only")
    check(decide(d, "main") == 0, "main, only the dashboard changed: skipped")
    c_doc = commit(d, {"README.md": "2", "checks/t.py": "2"}, "docs and checks")
    check(decide(d, "main") == 0, "main, only docs and checks changed: skipped")
    check(decide(d, "ccr-some-branch") == 0 and decide(d, "feature/x") == 0, "any other branch: skipped, whatever it changed")
    c_lookup = commit(d, {"market_lookup.py": "2"}, "lookup")
    check(decide(d, "main") == 1, "main, a module the functions import changed: deploys")
    check(decide(d, "ccr-some-branch") == 0, "the same change on a branch: skipped")
    c_api = commit(d, {"api/mcp.py": "new"}, "api")
    check(decide(d, "main") == 1, "main, a file in api/ changed: deploys")
    c_cfg = commit(d, {"vercel.json": "{ }"}, "config")
    check(decide(d, "main") == 1, "main, vercel.json changed: deploys")
    c_req = commit(d, {"requirements.txt": "y"}, "deps")
    check(decide(d, "main") == 1, "main, the dependency list changed: deploys")
    # a push of several commits at once: the last one is only the dashboard, an earlier one needs a deployment
    last = commit(d, {"app.py": "3"}, "dashboard again")
    check(decide(d, "main") == 0 and decide(d, "main", previous=c_doc) == 1,
          "a push of several commits compares against the last deployed one, so an earlier relevant commit still deploys")
    check(decide(d, "main", previous=last) == 0, "nothing new since the last deployed commit: skipped")
    check(decide(d, "main", previous="0" * 40) == 1, "a previous commit the clone doesn't have: deploys (the safe answer)")
with tempfile.TemporaryDirectory() as d:
    run("git", "init", "-q", "-b", "main", cwd=d)
    with_script(d)
    commit(d, {"app.py": "1"}, "first commit")
    check(decide(d, "main") == 1, "main with no parent commit to compare to: deploys (the safe answer)")
    check(decide(d, "") == 0, "a build with no branch name is not main: skipped")

print("FAILURES:", fails); sys.exit(fails)
