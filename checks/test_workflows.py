
import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
GATE = str(Path(__file__).resolve().parents[1] / ".github/actions/slot-gate")
sys.path.insert(0, REPO)
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, GATE)

import glob, re, subprocess, sys, yaml
fails = 0
def check(ok, label):
    global fails; fails += not ok; print("PASS" if ok else "FAIL", label)
R = REPO + "/.github"
# checks.yml is the CI suite: it loads no data, commits nothing and prints only
# invented fixtures, so the data rules below do not apply to it. Its exemption is
# asserted rather than assumed, just below.
NON_DATA = {"checks.yml", "keep-alive.yml"}
# Workflows whose only output is rows in the research library's database (publish_market.py):
# they load data and redact like the rest, but have nothing to commit.
DB_ONLY = {"market-library.yml"}

ci = yaml.safe_load(open(f"{R}/workflows/checks.yml"))
ci_text = open(f"{R}/workflows/checks.yml").read()
check("secrets." not in ci_text, "checks.yml uses no secrets (so it runs on forks too)")
check("stock_screener_data" not in ci_text and "load-data" not in ci_text,
      "checks.yml never touches the private data repo")
check((ci.get("permissions") or {}).get("contents") == "read", "checks.yml is read-only on this repo")

# keep-alive.yml only opens the app in a browser: no data, no commits. Asserted, not assumed.
ka_text = open(f"{R}/workflows/keep-alive.yml").read()
ka = yaml.safe_load(ka_text)
check("load-data" not in ka_text and "commit-data" not in ka_text and "DATA_REPO_TOKEN" not in ka_text,
      "keep-alive.yml never touches the data repo")
check(re.findall(r"secrets\.(\w+)", ka_text) == ["APP_URL"], "keep-alive.yml uses only the APP_URL secret")
check((ka.get("permissions") or {}).get("contents") == "read" and "git push" not in ka_text,
      "keep-alive.yml is read-only on this repo")

for path in sorted(glob.glob(f"{R}/workflows/*.yml")):
    if path.rsplit("/", 1)[1] in NON_DATA:
        continue
    name = path.rsplit("/", 1)[1]; text = open(path).read(); wf = yaml.safe_load(text)
    check((wf.get("permissions") or {}).get("contents") == "read" and all((j.get("permissions") or {}).get("contents", "read") == "read" for j in wf["jobs"].values()), f"{name}: contents read")
    check("git push" not in text and "git add" not in text and "git commit" not in text, f"{name}: no git writes in the code repo")
    for jn, job in wf["jobs"].items():
        steps = job.get("steps") or []
        runs_py = [s for s in steps if re.search(r"python(3)? (-u )?\S+\.py|load_rules", s.get("run", ""))]
        if not runs_py: continue
        uses = [s.get("uses", "") for s in steps]
        li = next((i for i, u in enumerate(uses) if u == "./.github/actions/load-data"), None)
        first_py = steps.index(runs_py[0])
        check(li is not None and li < first_py, f"{name}/{jn}: load-data before first Python step")
        for s in runs_py:
            if re.search(r"\S+\.py", s["run"]) and "<<" not in s["run"]:
                check("| python -u log_redact.py" in s["run"] and s.get("shell") == "bash", f"{name}/{jn}: '{s.get('name', s['run'][:30])}' piped through log_redact with shell: bash")
    if name in DB_ONLY:
        check("commit-data" not in text and sorted(set(re.findall(r"secrets\.(\w+)", text))) == ["DATA_REPO_TOKEN", "MARKET_DB_URL"],
              f"{name}: commits nothing; secrets are only the data repo's token and the market writer's URL")
        continue
    check("./.github/actions/commit-data" in text, f"{name}: commits via commit-data")
# The research library copy runs after the snapshot is committed and can never fail the refresh.
dr = yaml.safe_load(open(f"{R}/workflows/data-refresh.yml"))["jobs"]["refresh"]["steps"]
pub = [i for i, s in enumerate(dr) if "publish_market.py" in s.get("run", "")]
com = [i for i, s in enumerate(dr) if s.get("uses") == "./.github/actions/commit-data"]
check(len(pub) == 1 and com and pub[0] > com[0] and dr[pub[0]].get("continue-on-error") is True
      and "secrets.MARKET_DB_URL" in str(dr[pub[0]].get("env")),
      "data-refresh.yml publishes to the research library after its commit, and that step cannot fail the run")
for a in ("load-data", "commit-data"):
    act = yaml.safe_load(open(f"{R}/actions/{a}/action.yml"))
    for s in act["runs"]["steps"]:
        if "run" in s:
            r = subprocess.run(["bash", "-n"], input=s["run"], text=True, capture_output=True)
            check(r.returncode == 0, f"{a}: bash -n {r.stderr.strip()}")
print("FAILURES:", fails); sys.exit(fails)
