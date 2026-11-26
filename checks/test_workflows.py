
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
NON_DATA = {"checks.yml", "keep-alive.yml", "gemini-keys-check.yml"}
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

# gemini-keys-check.yml (by hand) tries each Gemini key once: no data, no commits, and the only secrets it names
# are the Gemini keys (checks/test_gemini_keys_workflows.py holds the list identical to the AI workflows').
gk_text = open(f"{R}/workflows/gemini-keys-check.yml").read()
gk = yaml.safe_load(gk_text)
check("load-data" not in gk_text and "commit-data" not in gk_text and "DATA_REPO_TOKEN" not in gk_text,
      "gemini-keys-check.yml never touches the data repo")
check(set(re.findall(r"secrets\.(\w+)", gk_text)) and all(n.startswith("GEMINI_API_KEY") for n in re.findall(r"secrets\.(\w+)", gk_text))
      and "toJSON(secrets)" not in gk_text,
      "gemini-keys-check.yml names only Gemini key secrets and never serialises the secrets context")
check((gk.get("permissions") or {}).get("contents") == "read" and "git push" not in gk_text
      and list((gk.get("on") or gk.get(True)).keys()) == ["workflow_dispatch"],
      "gemini-keys-check.yml is by hand only and read-only on this repo")

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
# The research library copy runs after the snapshot is committed and can never fail the refresh: the
# screener's rows first, then the library's companies, hourly (--min-gap skips every later run in the hour).
dr = yaml.safe_load(open(f"{R}/workflows/data-refresh.yml"))["jobs"]["refresh"]["steps"]
pub = [i for i, s in enumerate(dr) if "publish_market.py" in s.get("run", "")]
com = [i for i, s in enumerate(dr) if s.get("uses") == "./.github/actions/commit-data"]
check(len(pub) == 2 and com and min(pub) > com[0]
      and all(dr[i].get("continue-on-error") is True and "secrets.MARKET_DB_URL" in str(dr[i].get("env")) for i in pub),
      "data-refresh.yml publishes to the research library after its commit, and those steps cannot fail the run")
check("publish_market.py screener" in dr[pub[0]]["run"] and "publish_market.py library --min-gap" in dr[pub[1]]["run"],
      "data-refresh.yml: the screener's rows, then the library's companies at most once in a window (--min-gap)")
# The watchlists, breadth and closes go to the research library after the breadth commit, before the verify
# step that can fail the run, and can never fail it themselves.
mb = yaml.safe_load(open(f"{R}/workflows/market-breadth.yml"))["jobs"]["update_breadth"]["steps"]
wpub = [i for i, s in enumerate(mb) if "publish_market.py watchlists" in s.get("run", "")]
wcom = [i for i, s in enumerate(mb) if s.get("uses") == "./.github/actions/commit-data"]
wver = [i for i, s in enumerate(mb) if s.get("name", "").startswith("Verify")]
check(len(wpub) == 1 and wcom and wver and wcom[0] < wpub[0] < wver[0] and mb[wpub[0]].get("continue-on-error") is True
      and mb[wpub[0]].get("env") == {"MARKET_DB_URL": "${{ secrets.MARKET_DB_URL }}"},
      "market-breadth.yml publishes watchlists/breadth/closes after its commit and before the verify step, never failing the run")
ml = open(f"{R}/workflows/market-library.yml").read()
check("(github.event_name == 'schedule' || github.event.inputs.scheduled == 'true') && '--min-gap" in ml,
      "market-library.yml: a scheduled run (GitHub's wake-up or the Worker's scheduled=true dispatch) skips when the hourly step just ran; a run by hand always publishes")
for a in ("load-data", "commit-data"):
    act = yaml.safe_load(open(f"{R}/actions/{a}/action.yml"))
    for s in act["runs"]["steps"]:
        if "run" in s:
            r = subprocess.run(["bash", "-n"], input=s["run"], text=True, capture_output=True)
            check(r.returncode == 0, f"{a}: bash -n {r.stderr.strip()}")
# Every job has its own timeout (2026-11-22): without one a hung Yahoo download or database call held the job,
# and with it the workflow's concurrency group, for GitHub's 6-hour default while later dispatches queued behind
# it. A gate job is a few API calls; data-refresh runs every 30 minutes and breadth hourly.
for path in sorted(glob.glob(f"{R}/workflows/*.yml")):
    name = path.rsplit("/", 1)[1]
    for jn, job in yaml.safe_load(open(path))["jobs"].items():
        t = job.get("timeout-minutes")
        check(isinstance(t, int) and 1 <= t <= 360, f"{name}/{jn}: declares timeout-minutes ({t})")
        if jn == "gate":
            check(isinstance(t, int) and t <= 10, f"{name}/gate: a small timeout ({t})")
for name, jn, most in (("data-refresh.yml", "refresh", 60), ("market-breadth.yml", "update_breadth", 90)):
    t = yaml.safe_load(open(f"{R}/workflows/{name}"))["jobs"][jn].get("timeout-minutes")
    check(isinstance(t, int) and 20 <= t <= most, f"{name}/{jn}: timeout {t} fits its schedule (20-{most})")
# The two nightly AI stores are committed per entry against the copy the job loaded (merge_keyed.py), never
# whole: a whole-file commit reverted every entry the app or another workflow wrote while the job ran.
EM = {"expert-views.yml": ("build", "expert_views.json"), "fundamentals.yml": ("build", "fundamentals.json")}
for name, (jn, f) in EM.items():
    steps = yaml.safe_load(open(f"{R}/workflows/{name}"))["jobs"][jn]["steps"]
    cd = [s for s in steps if s.get("uses") == "./.github/actions/commit-data"]
    w = (cd[0].get("with") or {}) if cd else {}
    check(f in w.get("files", "").split() and w.get("entries-merge", "").split() == [f],
          f"{name}: commits {f} with entries-merge ({w.get('entries-merge')!r})")
check(yaml.safe_load(open(f"{R}/workflows/expert-views.yml"))["jobs"]["build"]["steps"][-1]["with"].get("entries-only") == "fundamentals.json",
      "expert-views.yml: fundamentals.json, which it does not own, stays entries-only")
for path in sorted(glob.glob(f"{R}/workflows/*.yml")):
    name = path.rsplit("/", 1)[1]
    if name in EM:
        continue
    check("entries-merge" not in open(path).read(), f"{name}: no entries-merge (a file only one writer edits)")
cd_act = yaml.safe_load(open(f"{R}/actions/commit-data/action.yml"))
check("entries-merge" in cd_act["inputs"] and "ENTRIES_MERGE: ${{ inputs.entries-merge }}" in open(f"{R}/actions/commit-data/action.yml").read()
      and "merge_keyed.py" in open(f"{R}/actions/commit-data/action.yml").read(),
      "commit-data: the entries-merge input reaches merge_keyed.py")
check("tag -f data-loaded HEAD" in open(f"{R}/actions/load-data/action.yml").read(),
      "load-data: tags the commit it copied from (merge_keyed.py's base)")
print("FAILURES:", fails); sys.exit(fails)
