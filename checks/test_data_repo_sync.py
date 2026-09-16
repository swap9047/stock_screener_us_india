"""get_data_repo_config / bootstrap_data_files against an in-memory Git Data API."""

import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
GATE = str(Path(__file__).resolve().parents[1] / ".github/actions/slot-gate")
sys.path.insert(0, REPO)
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, GATE)

import os, sys, tempfile, types, json


import github_sync as gs
from fake_github import FakeGitHub, use

fails = 0
def check(ok, label):
    global fails; fails += not ok; print("PASS" if ok else "FAIL", label)

# config: never the code repo, never GITHUB_TOKEN
for k in ("DATA_REPO", "DATA_REPO_TOKEN", "DATA_REPO_BRANCH"): os.environ.pop(k, None)
os.environ["GITHUB_TOKEN"] = "code-token"; os.environ["GITHUB_REPO"] = "o/code"
check(gs.get_data_repo_config({}) == (None, gs.DATA_REPO_DEFAULT, "main"), "no token -> None, default repo, no GITHUB_* fallback")
check(gs.get_data_repo_config({"DATA_REPO_TOKEN": "d"}) == ("d", gs.DATA_REPO_DEFAULT, "main"), "secret token read")
os.environ["DATA_REPO"] = "o/r"
check(gs.get_data_repo_config({"DATA_REPO_TOKEN": "d"})[1] == "o/r", "DATA_REPO env override")
os.environ.pop("DATA_REPO")
check("watchlist.json" in gs.DATA_FILES and "news_summary.json" in gs.DATA_FILES and "auth_config.json" not in gs.DATA_FILES, "DATA_FILES = syncable + pullable only")

def in_tmp():
    d = tempfile.mkdtemp(); gs.SCRIPT_DIR = d; gs.SYNC_STATE_FILE = os.path.join(d, ".s.json")
    # pull_generated_files derives its root from __file__; the implementation must use SCRIPT_DIR
    return d

remote = {"watchlist.json": {"x": ["AAA"]}, "markets.json": {"x": {}}, "news_summary.json": {"as_of": "2026-09-14"}}
fk = FakeGitHub(remote); use(fk); d = in_tmp()
open(os.path.join(d, "markets.json"), "w").write('{"local": 1}')
json.dump({"checked_at": 9e18, "blobs": {}}, open(gs.SYNC_STATE_FILE, "w"))   # rate limit "just checked"
ok, missing, note = gs.bootstrap_data_files("t", "o/r", "main", files=["watchlist.json", "markets.json", "news_summary.json", "custom_columns.json"])
check(ok and json.load(open(os.path.join(d, "watchlist.json"))) == {"x": ["AAA"]}, f"missing file downloaded despite rate limit: {note}")
check(json.load(open(os.path.join(d, "markets.json"))) == {"local": 1}, "present file not overwritten")
check(not os.path.exists(os.path.join(d, "custom_columns.json")) and ok, "file absent remotely is not an error")

d = in_tmp()
ok, missing, note = gs.bootstrap_data_files(None, "o/r", "main", files=["watchlist.json"])
check(not ok and missing == ["watchlist.json"], f"no token + missing file -> not ok: {note}")

fk = FakeGitHub(remote); use(fk); d = in_tmp()
real_get = fk.get
gs.requests.get = lambda url, **kw: types.SimpleNamespace(status_code=401, json=lambda: {"message": "Bad credentials"}, text="Bad credentials", content=b"") if "/git/trees/" in url else real_get(url, **kw)
ok, missing, note = gs.bootstrap_data_files("bad", "o/r", "main", files=["watchlist.json"])
check(not ok and not os.path.exists(os.path.join(d, "watchlist.json")), f"tree lookup failure -> not ok, nothing written: {note}")

fk = FakeGitHub(remote); use(fk); d = in_tmp()
real_get = fk.get
gs.requests.get = lambda url, **kw: types.SimpleNamespace(status_code=500, json=lambda: None, text="x", content=b"") if "/git/blobs/" in url else real_get(url, **kw)
ok, missing, note = gs.bootstrap_data_files("t", "o/r", "main", files=["watchlist.json"])
check(not ok and missing == ["watchlist.json"], f"blob download failure -> not ok: {note}")

fk = FakeGitHub(remote); use(fk); d = in_tmp()
for n in remote: open(os.path.join(d, n), "w").write("{}")
calls = []; gs.requests.get = lambda url, **kw: calls.append(url)
ok, missing, note = gs.bootstrap_data_files("t", "o/r", "main", files=list(remote))
check(ok and calls == [], "all present -> no network call")
# --- user config refresh on a running container (data commits no longer redeploy) ---
import hashlib
class RealShaGitHub(FakeGitHub):
    """Blob ids are real git blob SHAs, as on GitHub, so a local file's hash can match."""
    def _blob(self, b):
        i = hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest(); self.blobs[i] = b; return i
cfg = {"watchlist.json": {"x": ["AAA"]}, "settings.json": {"ema": 10}, "weekly_wrapup_state.json": {"wk": 1}}
fk = RealShaGitHub(cfg); use(fk); d = in_tmp()
ok, _, note = gs.bootstrap_data_files("t", "o/r", "main", files=list(cfg))
check(ok and gs.DATA_FILES.count("weekly_wrapup_state.json") == 1, f"weekly_wrapup_state.json is a data file and bootstraps: {note}")
fk.commit_file("watchlist.json", {"x": ["AAA", "BBB"]})          # edited elsewhere (local app / GitHub UI)
up, note = gs.pull_generated_files("t", "o/r", "main", force=True)
check("watchlist.json" in up and json.load(open(f"{d}/watchlist.json")) == {"x": ["AAA", "BBB"]}, f"untouched local config refreshed from remote: {note}")

json.dump({"ema": 20}, open(f"{d}/settings.json", "w"))          # unpushed local edit
fk.commit_file("settings.json", {"ema": 30})                     # and a different remote edit
up, note = gs.pull_generated_files("t", "o/r", "main", force=True)
check("settings.json" not in up and json.load(open(f"{d}/settings.json")) == {"ema": 20} and "settings.json" in note, f"locally edited config kept, reported: {note}")

# local save pushed as-is -> local bytes == remote blob -> in sync again, and later remote edits flow
body = open(f"{d}/settings.json", "rb").read()
fk.commit_file("settings.json", {"ema": 20}); t = dict(fk.trees[fk.commits[fk.head]["tree"]]); t["settings.json"] = fk._blob(body); fk.head = fk._commit(fk._tree(t), [fk.head])
up, note = gs.pull_generated_files("t", "o/r", "main", force=True)
check("settings.json" not in up, f"pushed local copy recognised as in sync: {note}")
fk.commit_file("settings.json", {"ema": 50})
up, note = gs.pull_generated_files("t", "o/r", "main", force=True)
check("settings.json" in up and json.load(open(f"{d}/settings.json")) == {"ema": 50}, f"after sync, the next remote edit is pulled: {note}")
print("FAILURES:", fails); sys.exit(fails)
