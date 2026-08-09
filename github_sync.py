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
import os

import requests

GITHUB_API = "https://api.github.com"

# (local filename, human label) for every config file this app can push.
SYNCABLE_FILES = [
    ("watchlist.json", "Watchlist (tickers per market)"),
    ("markets.json", "Markets registry (labels, benchmarks per watchlist)"),
    ("invested.json", "Invested positions and weights"),
    ("custom_filters.json", "Custom filters (per market)"),
    ("settings.json", "Calculation settings"),
    ("alerts_config.json", "Alert / scan rules"),
    ("column_prefs.json", "Column order / visibility"),
    ("custom_columns.json", "Custom computed columns"),
    ("ticker_notes.json", "Per-ticker notes and flags"),
    ("expert_views.json", "AI Expert Views"),
    ("fundamentals.json", "AI Fundamental Views"),
    ("ticker_index.json", "Per-ticker index assignment"),
    ("watchlist_groups.json", "Combined-tab membership"),
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


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


def trigger_github_workflow(token, repo, workflow_file="news-summary.yml", ref="main"):
    """
    Triggers a workflow_dispatch event for the given workflow file using the GitHub API.
    """
    url = f"{GITHUB_API}/repos/{repo}/actions/workflows/{workflow_file}/dispatches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"ref": ref}
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code == 204:
        return True, "Workflow triggered successfully."
    return False, f"Failed to trigger workflow ({resp.status_code}): {resp.text}"
