"""
Push local config files to GitHub directly from the running app -- so alert
rules / watchlist / custom filters / settings edited through a DEPLOYED app
(e.g. Streamlit Community Cloud, which has no git access and no disk that
survives a redeploy) actually land in the repo, instead of only living on
that one instance's ephemeral filesystem until it's redeployed or restarted.

Uses GitHub's REST "Git Data" API (a plain HTTPS call via `requests`) rather
than the git CLI -- Streamlit Cloud containers don't have your SSH keys or
git configured, but they always have outbound network access and
`requests` is already a dependency (see alerts.py's Discord webhook calls).

WHERE THE DATA LIVES: every JSON data file is in the PRIVATE repo
DATA_REPO_DEFAULT, not in the public code repo the app is deployed from. The
code repo stays public for free Actions minutes, and its committed JSON used to
expose holdings, notes and alert rules to anyone. So there are two configs:
get_data_repo_config (DATA_REPO_TOKEN) for every data read and write here, and
get_github_config (GITHUB_TOKEN / GITHUB_REPO) only to dispatch workflows.

IMPORTANT: multiple files are pushed as ONE atomic commit (blobs -> one
tree -> one commit -> move the branch ref), not one commit per file. This
matters specifically because Streamlit Community Cloud auto-redeploys the
instant ANY commit lands on the branch it's watching -- pushing several
files as separate sequential commits creates a real race: the redeploy
triggered by the FIRST commit can tear down and restart the running
container before the loop reaches the LAST file, silently dropping
whatever hadn't been pushed yet (e.g. a newly-created alert rule that only
ever existed on that container's ephemeral disk). Bundling every changed
file into a single commit closes that race -- either everything lands
together, or nothing does, and there's no in-between state for a redeploy
to interrupt. Data commits now land in the data repo, which Streamlit doesn't
watch, so the race itself is gone; the single commit stays so a multi-file
change (watchlist + interested + snapshot) is never half-applied.

One-time setup (GitHub -> Settings -> Developer settings -> Fine-grained tokens):
  1. Data: a token with "Contents: Read and write" on the data repo only, set
     as DATA_REPO_TOKEN in Streamlit secrets AND in the code repo's Actions
     secrets. A fork points DATA_REPO ("owner/repo") at its own data repo.
  2. Background jobs: a token with "Actions: Read and write" on the code repo,
     set as GITHUB_TOKEN, plus GITHUB_REPO ("owner/code-repo") and optionally
     GITHUB_BRANCH (defaults to "main").
  Never paste a token into a text box on a public deployment -- same rule this
  app already follows for the Discord webhook.
"""

import base64
import hashlib
import json
import os
import time

import requests

GITHUB_API = "https://api.github.com"

# (local filename, human label) for every config file this app can push.
SYNCABLE_FILES = [
    ("watchlist.json", "Watchlist (tickers per market)"),
    ("markets.json", "Markets registry (labels, benchmarks per watchlist)"),
    ("interested.json", "Tickers marked Interested"),
    ("custom_filters.json", "Custom filters (per market)"),
    ("settings.json", "Calculation settings"),
    ("alerts_config.json", "Alert / scan rules"),
    ("column_prefs.json", "Column order / visibility"),
    ("custom_columns.json", "Custom computed columns"),
    ("ticker_notes.json", "Per-ticker notes and flags"),
    ("expert_views.json", "AI Expert Views"),
    ("fundamentals.json", "AI Fundamental Views"),
    ("ticker_index.json", "Per-ticker index assignment"),
    ("data_snapshot.json", "Data snapshot (prices + indicators)"),
    ("watchlist_groups.json", "Combined-tab membership"),
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# The JSON data lives in this PRIVATE repo, not in the public code repo -- the
# code repo is public for free Actions minutes, and its JSON exposed holdings,
# notes and alert rules to anyone. Not a secret; DATA_REPO only exists so a fork
# can point at its own data repo.
DATA_REPO_DEFAULT = "swap9047/stock_screener_data"


# Written by workflows (or by a dashboard action that pushes them itself, with
# their own freshness protection). The "Push to GitHub" button must never
# push these wholesale from the container's disk -- see app.py's button and
# push_json_entry_changes.
WORKFLOW_GENERATED_FILES = {"data_snapshot.json", "expert_views.json", "fundamentals.json"}

# Files the WORKFLOWS generate. Pulled at runtime with the "never go backwards"
# timestamp guard, since the app also writes some of them itself.
PULLABLE_FILES = [
    "data_snapshot.json",
    "expert_views.json",
    "fundamentals.json",
    "news_summary.json",
    "market_breadth.json",
    "dashboard_perf.json",
    "weekly_wrapup_state.json",   # read by the Alert Rules tab's wrap-up preview
]

# User config, edited in the UI. Also pulled at runtime, but only over a local
# copy that is still byte-identical to what was last pulled or pushed -- an
# unpushed local edit is never overwritten. This refresh used to be implicit:
# every config push landed as a code-repo commit that redeployed the app with
# fresh files. Data commits no longer redeploy anything, so without it a running
# container kept an old watchlist.json after an edit made elsewhere (a local run,
# the GitHub UI) and its next save pushed that old copy back over the edit.
CONFIG_PULLABLE_FILES = [name for name, _ in SYNCABLE_FILES if name not in WORKFLOW_GENERATED_FILES]

# Every file the app reads from the data repo. A fresh Streamlit Cloud container
# has none of them (the code repo tracks no JSON), so bootstrap_data_files
# downloads whichever are missing before anything loads.
DATA_FILES = sorted({name for name, _ in SYNCABLE_FILES} | set(PULLABLE_FILES))


def _git_blob_sha(path):
    """The SHA git (and GitHub's tree API) would give this file's bytes, or None."""
    try:
        with open(path, "rb") as f:
            body = f.read()
    except OSError:
        return None
    return hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest()

# Container-local, gitignored. Remembers the blob SHA of each file we last
# pulled plus when we last checked, so a rerun costs one small API call at
# most -- Streamlit re-executes the whole script on every interaction.
SYNC_STATE_FILE = os.path.join(SCRIPT_DIR, ".data_sync_state.json")
SYNC_MIN_INTERVAL_SECONDS = 300

# Timestamp fields used to refuse a backwards pull, in preference order.
_FRESHNESS_KEYS = ("generated_at", "as_of")


def _read_sync_state():
    try:
        with open(SYNC_STATE_FILE) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _write_sync_state(state):
    try:
        tmp = f"{SYNC_STATE_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, SYNC_STATE_FILE)
    except Exception:
        pass  # a lost marker only costs one redundant check


def _local_freshness(path):
    """The file's own timestamp, for refusing an older remote copy."""
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in _FRESHNESS_KEYS:
        if data.get(key):
            return str(data[key])
    return None


def _remote_tree(token, repo, branch):
    """({path: blob sha} for the branch's root files, None), or (None, error text)."""
    headers = _headers(token) if token else {"Accept": "application/vnd.github+json"}
    try:
        resp = requests.get(f"{GITHUB_API}/repos/{repo}/git/trees/{branch}", headers=headers, timeout=15)
        if resp.status_code != 200:
            return None, f"tree lookup failed: {_short(resp)}"
        return {e.get("path"): e.get("sha") for e in (resp.json().get("tree") or [])}, None
    except requests.RequestException as e:
        return None, f"tree lookup failed: {e}"


def pull_generated_files(token, repo, branch="main", files=None,
                         min_interval=SYNC_MIN_INTERVAL_SECONDS, force=False):
    """Refresh data files from the data repo. Returns (updated, note).

    Generated files (PULLABLE_FILES) are refreshed unless the local copy is
    newer by its own timestamp; user config (CONFIG_PULLABLE_FILES) only when
    the local copy is unchanged since it was last pulled or pushed.

    WHY this exists: on Streamlit Community Cloud the app reads these files
    from its container's checkout, which only changes when the app REDEPLOYS.
    When redeploys stop firing -- which happened between 2026-09-06 and
    2026-09-09 -- the dashboard silently freezes on whatever it last had,
    while every workflow keeps running green and committing. There is no error
    anywhere; the app simply shows three-day-old AI verdicts as current. This
    makes freshness the app's own responsibility instead of the platform's.

    Cheap by construction: one git-trees call returns every root file's blob
    SHA, so a check with nothing new costs a single small request, and only
    files whose SHA actually moved are downloaded. Content is fetched by BLOB
    SHA rather than from raw.githubusercontent.com because the blob is
    content-addressed -- the raw CDN caches for minutes and could hand back an
    older body than the tree we just read.

    Never raises: a failed sync must leave the app running on its local files.
    """
    files = list(files or (PULLABLE_FILES + CONFIG_PULLABLE_FILES))
    state = _read_sync_state()
    now = time.time()
    if not force and now - float(state.get("checked_at") or 0) < min_interval:
        return [], "skipped (checked recently)"
    if not repo:
        return [], "no data repo configured"

    headers = _headers(token) if token else {"Accept": "application/vnd.github+json"}
    root = SCRIPT_DIR
    entries, err = _remote_tree(token, repo, branch)
    if entries is None:
        return [], err

    known = dict(state.get("blobs") or {})
    updated, skipped, kept_edits = [], [], []
    for name in files:
        remote_sha = entries.get(name)
        if not remote_sha:
            continue
        path = os.path.join(root, name)
        if known.get(name) == remote_sha and os.path.exists(path):
            continue
        if name in CONFIG_PULLABLE_FILES and os.path.exists(path):
            local_sha = _git_blob_sha(path)
            if local_sha == remote_sha:        # e.g. this app just pushed it
                known[name] = remote_sha
                continue
            if local_sha != known.get(name):   # edited here and not (yet) pushed
                kept_edits.append(name)
                continue
        try:
            blob = requests.get(
                f"{GITHUB_API}/repos/{repo}/git/blobs/{remote_sha}",
                headers={**headers, "Accept": "application/vnd.github.raw"}, timeout=30,
            )
            if blob.status_code != 200:
                continue
            body = blob.content
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            # A truncated or non-JSON body must never land on disk -- the
            # loaders treat a parse error as "no data at all".
            continue

        # Never go backwards. The app writes data_snapshot.json itself (the
        # Refresh Data button) and pushes it; if that push failed, pulling
        # would quietly revert the user's own fresher data.
        local_stamp = _local_freshness(path)
        remote_stamp = next((str(parsed[k]) for k in _FRESHNESS_KEYS
                             if isinstance(parsed, dict) and parsed.get(k)), None)
        if local_stamp and remote_stamp and remote_stamp < local_stamp:
            skipped.append(name)
            known[name] = remote_sha   # seen and judged; don't re-download it
            continue

        try:
            tmp = f"{path}.tmp"
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, path)
        except Exception:
            continue
        known[name] = remote_sha
        updated.append(name)

    _write_sync_state({"checked_at": now, "blobs": known})
    note = f"updated {len(updated)}" if updated else "up to date"
    if skipped:
        note += f"; kept newer local copy of {', '.join(skipped)}"
    if kept_edits:
        note += f"; kept unpushed local edits to {', '.join(kept_edits)}"
    return updated, note


def _config_value(st_secrets, key, default=None):
    """Streamlit secrets first, then environment variables. `st_secrets` is
    passed in (rather than importing streamlit here) so this module has zero
    Streamlit dependency and stays importable from plain scripts."""
    if st_secrets is not None:
        try:
            if key in st_secrets:
                return st_secrets[key]
        except Exception:
            pass
    return os.environ.get(key, default)


def get_github_config(st_secrets=None):
    """(token, repo, branch) of the CODE repo, from GITHUB_TOKEN / GITHUB_REPO /
    GITHUB_BRANCH. Only for dispatching workflows and linking to their logs --
    data reads and writes use get_data_repo_config. Returns (None, None, "main")
    if nothing is configured."""
    return (_config_value(st_secrets, "GITHUB_TOKEN"),
            _config_value(st_secrets, "GITHUB_REPO"),
            _config_value(st_secrets, "GITHUB_BRANCH", "main"))


def get_data_repo_config(st_secrets=None):
    """(token, repo, branch) of the private DATA repo, from DATA_REPO_TOKEN,
    DATA_REPO (default DATA_REPO_DEFAULT) and DATA_REPO_BRANCH (default "main").
    Deliberately no fallback to GITHUB_TOKEN / GITHUB_REPO: without
    DATA_REPO_TOKEN that fallback would push holdings and notes straight back
    into the public code repo."""
    return (_config_value(st_secrets, "DATA_REPO_TOKEN"),
            _config_value(st_secrets, "DATA_REPO", DATA_REPO_DEFAULT),
            _config_value(st_secrets, "DATA_REPO_BRANCH", "main"))


def bootstrap_data_files(token, repo, branch="main", files=None):
    """Download every data file that is missing on disk. Returns (ok, missing, note).

    Runs before the app's first load_*(). Those loaders create an empty default
    when their file is absent, so on a fresh container with an unreadable data
    repo the app would render blank watchlists -- and the next save would push
    those blanks over the real data. `ok` is False exactly when that could
    happen: the tree lookup failed while something is missing, or a file that
    exists remotely still isn't on disk. A file the data repo doesn't have is
    fine; its loader creating the default is the right outcome.

    Ignores pull_generated_files' rate limit, which answers "checked recently"
    even when files are missing. Makes no network call when nothing is missing.
    """
    files = list(files or DATA_FILES)
    missing = [n for n in files if not os.path.exists(os.path.join(SCRIPT_DIR, n))]
    if not missing:
        return True, [], "all data files present"
    if not token or not repo:
        return False, missing, "DATA_REPO_TOKEN is not configured"
    entries, err = _remote_tree(token, repo, branch)
    if entries is None:
        return False, missing, err
    wanted = [n for n in missing if n in entries]
    if wanted:
        pull_generated_files(token, repo, branch, files=wanted, force=True)
    still = [n for n in wanted if not os.path.exists(os.path.join(SCRIPT_DIR, n))]
    if still:
        return False, still, f"could not download {', '.join(still)}"
    return True, [], f"downloaded {len(wanted)} file(s)"


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


def push_all_config(token, repo, branch="main", filenames=None, message=None):
    """Pushes every file in `filenames` (defaults to all of SYNCABLE_FILES)
    as ONE atomic commit -- see module docstring for why this matters on
    Streamlit Cloud. Returns (ok, detail_message). On any failure, nothing
    is pushed at all (GitHub never sees a partial commit -- the ref move is
    the last step and only happens if every prior step succeeded)."""
    targets = filenames if filenames is not None else [f for f, _ in SYNCABLE_FILES]
    if not targets:
        return False, "No files selected."
    if not token or not repo:
        return False, "DATA_REPO_TOKEN not configured (see Settings)."

    missing = [f for f in targets if not os.path.exists(os.path.join(SCRIPT_DIR, f))]
    if missing:
        return False, f"These files don't exist locally -- nothing to push: {', '.join(missing)}."

    headers = _headers(token)
    base_url = f"{GITHUB_API}/repos/{repo}"

    # 1. Current tip of the branch -> base commit -> base tree.
    try:
        ref_resp = requests.get(f"{base_url}/git/ref/heads/{branch}", headers=headers, timeout=15)
    except requests.RequestException as e:
        return False, f"Network error reading branch ref: {e}"
    if ref_resp.status_code != 200:
        return False, f"Couldn't read branch '{branch}' ({ref_resp.status_code}): {_short(ref_resp)}"
    base_commit_sha = ref_resp.json()["object"]["sha"]

    try:
        commit_resp = requests.get(f"{base_url}/git/commits/{base_commit_sha}", headers=headers, timeout=15)
    except requests.RequestException as e:
        return False, f"Network error reading base commit: {e}"
    if commit_resp.status_code != 200:
        return False, f"Couldn't read base commit ({commit_resp.status_code}): {_short(commit_resp)}"
    base_tree_sha = commit_resp.json()["tree"]["sha"]

    # 2. One blob per file.
    tree_entries = []
    for filename in targets:
        with open(os.path.join(SCRIPT_DIR, filename), "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("ascii")
        try:
            blob_resp = requests.post(
                f"{base_url}/git/blobs", headers=headers,
                json={"content": content_b64, "encoding": "base64"}, timeout=15,
            )
        except requests.RequestException as e:
            return False, f"Network error creating blob for {filename}: {e}"
        if blob_resp.status_code != 201:
            return False, f"Couldn't create blob for {filename} ({blob_resp.status_code}): {_short(blob_resp)}"
        tree_entries.append({
            "path": filename, "mode": "100644", "type": "blob",
            "sha": blob_resp.json()["sha"],
        })

    # 3. One new tree (layered on the base tree -- untouched files are
    # carried forward automatically, only `tree_entries` actually change).
    try:
        tree_resp = requests.post(
            f"{base_url}/git/trees", headers=headers,
            json={"base_tree": base_tree_sha, "tree": tree_entries}, timeout=15,
        )
    except requests.RequestException as e:
        return False, f"Network error creating tree: {e}"
    if tree_resp.status_code != 201:
        return False, f"Couldn't create tree ({tree_resp.status_code}): {_short(tree_resp)}"
    new_tree_sha = tree_resp.json()["sha"]

    # 4. One new commit pointing at that tree.
    commit_message = message or f"Update {', '.join(targets)} via app"
    try:
        new_commit_resp = requests.post(
            f"{base_url}/git/commits", headers=headers,
            json={"message": commit_message, "tree": new_tree_sha, "parents": [base_commit_sha]}, timeout=15,
        )
    except requests.RequestException as e:
        return False, f"Network error creating commit: {e}"
    if new_commit_resp.status_code != 201:
        return False, f"Couldn't create commit ({new_commit_resp.status_code}): {_short(new_commit_resp)}"
    new_commit_sha = new_commit_resp.json()["sha"]

    # 5. Move the branch pointer -- this is the single moment the change
    # actually becomes visible/pullable, and the only step Streamlit Cloud's
    # watcher can react to. Everything before this was invisible staging.
    try:
        move_resp = requests.patch(
            f"{base_url}/git/refs/heads/{branch}", headers=headers,
            json={"sha": new_commit_sha}, timeout=15,
        )
    except requests.RequestException as e:
        return False, f"Network error moving branch ref: {e}"
    if move_resp.status_code != 200:
        return False, f"Couldn't move branch ref ({move_resp.status_code}): {_short(move_resp)}"

    return True, f"Pushed {len(targets)} file(s) in one commit ({new_commit_sha[:7]}): {', '.join(targets)}."


def read_remote_json(token, repo, branch, filename):
    """The parsed JSON of `filename` at the tip of `branch`, or None if it is
    missing, unreadable, or not JSON. Content is fetched by blob SHA (not the
    raw CDN) for the same cache reason as pull_generated_files."""
    if not repo:
        return None
    headers = _headers(token) if token else {"Accept": "application/vnd.github+json"}
    try:
        tree = requests.get(f"{GITHUB_API}/repos/{repo}/git/trees/{branch}", headers=headers, timeout=15)
        if tree.status_code != 200:
            return None
        sha = {e.get("path"): e.get("sha") for e in (tree.json().get("tree") or [])}.get(filename)
        if not sha:
            return None
        blob = requests.get(f"{GITHUB_API}/repos/{repo}/git/blobs/{sha}",
                            headers={**headers, "Accept": "application/vnd.github.raw"}, timeout=30)
        if blob.status_code != 200:
            return None
        return json.loads(blob.content.decode("utf-8"))
    except (requests.RequestException, ValueError):
        return None


def push_json_entry_changes(token, repo, branch, changes, message, attempts=3, newer_than_field=None):
    """Commit per-KEY changes to keyed JSON files ({ticker: view} stores such
    as expert_views.json / fundamentals.json) on top of what is on the branch
    NOW, as one atomic commit. Returns (ok, detail, merged) where merged is
    {filename: dict} -- the content that was committed, for the caller to
    write back locally.

    `changes` is {filename: {"set": {key: value}, "delete": [key, ...]}}.

    WHY: the dashboard's AI actions (re-analyze a ticker, Retry Pending, the
    stale-ticker cleanup) used to push the WHOLE local file with
    push_all_config. The container's copy is only as fresh as its last
    pull_generated_files (up to SYNC_MIN_INTERVAL_SECONDS old, and these files
    carry no timestamp for that pull to compare), so re-analyzing one ticker
    could revert every verdict the nightly workflow had committed since --
    the same failure as audit finding A03 on the "Push to GitHub" button.
    Reading the file from the branch tip and changing only the touched keys
    means an unrelated ticker can never be reverted.

    If a workflow commits between our read and our ref move, GitHub rejects
    the non-fast-forward update; we re-read and re-apply, up to `attempts`.

    `newer_than_field` (e.g. "as_of") makes a "set" conditional: an entry is
    only written if the branch has no entry for that key, or ours has a
    strictly greater value in that field. Without it, an action that TRIED a
    ticker and failed (so its local entry was left as it was) still pushed that
    unchanged local entry, which could be older than what a workflow had
    committed since this container last pulled. If nothing is newer, no commit
    is made and the result is (True, "nothing newer ...", merged)."""
    if not token or not repo:
        return False, "DATA_REPO_TOKEN not configured (see Settings).", {}
    headers = _headers(token)
    base_url = f"{GITHUB_API}/repos/{repo}"
    last_detail = "no attempt made"
    for attempt in range(1, attempts + 1):
        try:
            ref_resp = requests.get(f"{base_url}/git/ref/heads/{branch}", headers=headers, timeout=15)
            if ref_resp.status_code != 200:
                return False, f"Couldn't read branch '{branch}' ({ref_resp.status_code}): {_short(ref_resp)}", {}
            base_commit_sha = ref_resp.json()["object"]["sha"]
            commit_resp = requests.get(f"{base_url}/git/commits/{base_commit_sha}", headers=headers, timeout=15)
            if commit_resp.status_code != 200:
                return False, f"Couldn't read base commit ({commit_resp.status_code}): {_short(commit_resp)}", {}
            base_tree_sha = commit_resp.json()["tree"]["sha"]
            tree_resp = requests.get(f"{base_url}/git/trees/{base_tree_sha}", headers=headers, timeout=15)
            if tree_resp.status_code != 200:
                return False, f"Couldn't read base tree ({tree_resp.status_code}): {_short(tree_resp)}", {}
            blob_sha_by_path = {e.get("path"): e.get("sha") for e in (tree_resp.json().get("tree") or [])}

            merged, tree_entries, applied = {}, [], 0
            for filename, change in changes.items():
                current = {}
                if blob_sha_by_path.get(filename):
                    blob = requests.get(
                        f"{base_url}/git/blobs/{blob_sha_by_path[filename]}",
                        headers={**headers, "Accept": "application/vnd.github.raw"}, timeout=30,
                    )
                    if blob.status_code != 200:
                        return False, f"Couldn't read {filename} from {branch} ({blob.status_code})", {}
                    # A body that isn't a JSON object must stop the push: merging
                    # into {} would commit a file holding only our keys.
                    current = json.loads(blob.content.decode("utf-8"))
                    if not isinstance(current, dict):
                        return False, f"{filename} on {branch} is not a JSON object -- not pushing", {}
                changed = False
                for key, value in (change.get("set") or {}).items():
                    if newer_than_field and key in current:
                        ours = str((value or {}).get(newer_than_field) or "")
                        theirs = str((current.get(key) or {}).get(newer_than_field) or "")
                        if not ours or ours <= theirs:
                            continue    # branch already has this entry or a newer one
                    current[key] = value
                    changed = True
                    applied += 1
                for key in change.get("delete") or []:
                    if key in current:
                        current.pop(key)
                        changed = True
                        applied += 1
                merged[filename] = current
                if not changed:
                    continue
                body = json.dumps(current, indent=2)   # same format as stock_data.atomic_write_json
                blob_resp = requests.post(
                    f"{base_url}/git/blobs", headers=headers,
                    json={"content": base64.b64encode(body.encode("utf-8")).decode("ascii"), "encoding": "base64"},
                    timeout=15,
                )
                if blob_resp.status_code != 201:
                    return False, f"Couldn't create blob for {filename} ({blob_resp.status_code}): {_short(blob_resp)}", {}
                tree_entries.append({"path": filename, "mode": "100644", "type": "blob",
                                     "sha": blob_resp.json()["sha"]})

            if not tree_entries:
                return True, "nothing newer than what is already on GitHub -- no commit made", merged
            new_tree = requests.post(f"{base_url}/git/trees", headers=headers,
                                     json={"base_tree": base_tree_sha, "tree": tree_entries}, timeout=15)
            if new_tree.status_code != 201:
                return False, f"Couldn't create tree ({new_tree.status_code}): {_short(new_tree)}", {}
            new_commit = requests.post(f"{base_url}/git/commits", headers=headers,
                                       json={"message": message, "tree": new_tree.json()["sha"],
                                             "parents": [base_commit_sha]}, timeout=15)
            if new_commit.status_code != 201:
                return False, f"Couldn't create commit ({new_commit.status_code}): {_short(new_commit)}", {}
            new_commit_sha = new_commit.json()["sha"]
            move = requests.patch(f"{base_url}/git/refs/heads/{branch}", headers=headers,
                                  json={"sha": new_commit_sha}, timeout=15)
            if move.status_code == 200:
                return True, (f"Pushed {applied} entr{'y' if applied == 1 else 'ies'} in "
                              f"{', '.join(changes)} ({new_commit_sha[:7]})."), merged
            last_detail = f"branch moved during push ({move.status_code}): {_short(move)}"
        except (requests.RequestException, ValueError) as e:
            last_detail = f"{type(e).__name__}: {e}"
        if attempt < attempts:
            time.sleep(1.5 * attempt)
    return False, f"Push failed after {attempts} attempts -- {last_detail}", {}


def _short(resp):
    """Best-effort short error string from a GitHub API error response."""
    try:
        return resp.json().get("message", resp.text[:200])
    except Exception:
        return resp.text[:200]


def trigger_github_workflow(token, repo, workflow_file="news-summary.yml", ref="main", inputs=None):
    """
    Triggers a workflow_dispatch event for the given workflow file using the GitHub API.

    `inputs` is an optional dict matching the workflow's own `workflow_dispatch.inputs`
    block (values must be strings -- GitHub rejects non-string input values). It's
    omitted from the payload entirely when empty, so callers targeting a workflow
    that declares no inputs are unaffected.
    """
    url = f"{GITHUB_API}/repos/{repo}/actions/workflows/{workflow_file}/dispatches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"ref": ref}
    if inputs:
        payload["inputs"] = {k: str(v) for k, v in inputs.items()}
    # Every other call in this module passes a timeout; this one didn't, and it
    # runs inside a Streamlit interaction (the per-tab "Re-analyze All" buttons),
    # so a hung connection blocked that session with no ceiling.
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    if resp.status_code == 204:
        return True, "Workflow triggered successfully."
    return False, f"Failed to trigger workflow ({resp.status_code}): {resp.text}"
