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
to interrupt.

One-time setup:
  1. Create a GitHub Personal Access Token scoped to just this repo, with
     "Contents: Read and write" AND "Actions: Read and write" permissions 
     (GitHub -> Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens).
  2. Set it as a Streamlit secret named GITHUB_TOKEN. Never paste a token
     into a text box on a public deployment -- same rule this app already
     follows for the Discord webhook.
  3. Set GITHUB_REPO ("your-username/your-repo-name") and optionally
     GITHUB_BRANCH (defaults to "main") -- these aren't secret, but the
     Streamlit secrets panel is the easiest place to set them alongside the
     token.
"""

import base64
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


# Files the WORKFLOWS generate and the app only ever reads. These are the ones
# safe to pull at runtime. User config (watchlist.json, settings.json, notes,
# ...) is deliberately NOT here: it is edited in the UI and pushed from there,
# so pulling it could overwrite an edit the user is in the middle of making.
PULLABLE_FILES = [
    "data_snapshot.json",
    "expert_views.json",
    "fundamentals.json",
    "news_summary.json",
    "market_breadth.json",
    "dashboard_perf.json",
]

# Container-local, gitignored. Remembers the blob SHA of each file we last
# pulled plus when we last checked, so a rerun costs one small API call at
# most -- Streamlit re-executes the whole script on every interaction.
SYNC_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".data_sync_state.json")
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


def pull_generated_files(token, repo, branch="main", files=None,
                         min_interval=SYNC_MIN_INTERVAL_SECONDS, force=False):
    """Refresh the generated JSON files from GitHub. Returns (updated, note).

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
    files = list(files or PULLABLE_FILES)
    state = _read_sync_state()
    now = time.time()
    if not force and now - float(state.get("checked_at") or 0) < min_interval:
        return [], "skipped (checked recently)"
    if not repo:
        return [], "no GITHUB_REPO configured"

    headers = _headers(token) if token else {"Accept": "application/vnd.github+json"}
    root = os.path.dirname(os.path.abspath(__file__))
    try:
        tree_resp = requests.get(
            f"{GITHUB_API}/repos/{repo}/git/trees/{branch}",
            headers=headers, timeout=15,
        )
        if tree_resp.status_code != 200:
            return [], f"tree lookup failed: {_short(tree_resp)}"
        entries = {e.get("path"): e.get("sha") for e in (tree_resp.json().get("tree") or [])}
    except requests.RequestException as e:
        return [], f"tree lookup failed: {e}"

    known = dict(state.get("blobs") or {})
    updated, skipped = [], []
    for name in files:
        remote_sha = entries.get(name)
        if not remote_sha:
            continue
        path = os.path.join(root, name)
        if known.get(name) == remote_sha and os.path.exists(path):
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
    return updated, note


def get_github_config(st_secrets=None):
    """Returns (token, repo, branch), reading GITHUB_TOKEN / GITHUB_REPO /
    GITHUB_BRANCH from Streamlit secrets first, then environment variables.
    `st_secrets` is passed in (rather than importing streamlit here) so
    this module has zero Streamlit dependency and stays independently
    testable/importable from alert_check.py or a plain script if ever
    needed. Returns (None, None, "main") if nothing is configured."""
    def _get(key, default=None):
        if st_secrets is not None:
            try:
                if key in st_secrets:
                    return st_secrets[key]
            except Exception:
                pass
        return os.environ.get(key, default)

    token = _get("GITHUB_TOKEN")
    repo = _get("GITHUB_REPO")
    branch = _get("GITHUB_BRANCH", "main")
    return token, repo, branch


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
        return False, "GITHUB_TOKEN / GITHUB_REPO not configured (see Settings)."

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
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code == 204:
        return True, "Workflow triggered successfully."
    return False, f"Failed to trigger workflow ({resp.status_code}): {resp.text}"
