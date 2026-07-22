"""
Push local config files to GitHub directly from the running app -- so alert
rules / watchlist / custom filters / settings edited through a DEPLOYED app
(e.g. Streamlit Community Cloud, which has no git access and no disk that
survives a redeploy) actually land in the repo, instead of only living on
that one instance's ephemeral filesystem until it's redeployed or restarted.

Uses GitHub's REST "Contents" API (a plain HTTPS call via `requests`)
rather than the git CLI -- Streamlit Cloud containers don't have your SSH
keys or git configured, but they always have outbound network access and
`requests` is already a dependency (see alerts.py's Discord webhook calls).

One-time setup:
  1. Create a GitHub Personal Access Token scoped to just this repo, with
     "Contents: Read and write" permission (GitHub -> Settings -> Developer
     settings -> Personal access tokens -> Fine-grained tokens).
  2. Set it as a Streamlit secret named GITHUB_TOKEN. Never paste a token
     into a text box on a public deployment -- same rule this app already
     follows for the Discord webhook.
  3. Set GITHUB_REPO ("your-username/your-repo-name") and optionally
     GITHUB_BRANCH (defaults to "main") -- these aren't secret, but the
     Streamlit secrets panel is the easiest place to set them alongside the
     token.

Each push is a separate GitHub Contents API call, so pushing "all" config
files creates one commit per file rather than a single combined commit --
fine for small JSON files on a personal repo, just something to know if you
go looking at the commit history.
"""

import base64
import os

import requests

GITHUB_API = "https://api.github.com"

# (local filename, human label) for every config file this app can push.
SYNCABLE_FILES = [
    ("watchlist.json", "Watchlist (tickers per market)"),
    ("custom_filters.json", "Custom filters (per market)"),
    ("settings.json", "Calculation settings"),
    ("alerts_config.json", "Alert / scan rules"),
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


def push_file_to_github(filename, token, repo, branch="main", message=None):
    """Pushes SCRIPT_DIR/filename to `repo` (owner/name) on `branch` via the
    GitHub Contents API -- creates the file if it doesn't exist there yet,
    updates it (using its current blob sha, which GitHub requires so a
    stale write can't silently clobber someone else's concurrent change) if
    it does. Returns (ok, detail_message) -- never raises for HTTP-level or
    network failures, only for local file-read errors that shouldn't
    happen (missing SCRIPT_DIR permissions etc.)."""
    path = os.path.join(SCRIPT_DIR, filename)
    if not os.path.exists(path):
        return False, f"{filename} doesn't exist locally -- nothing to push."
    if not token or not repo:
        return False, "GITHUB_TOKEN / GITHUB_REPO not configured (see Settings)."

    with open(path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("ascii")

    url = f"{GITHUB_API}/repos/{repo}/contents/{filename}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    sha = None
    try:
        get_resp = requests.get(url, headers=headers, params={"ref": branch}, timeout=15)
        if get_resp.status_code == 200:
            sha = get_resp.json().get("sha")
        elif get_resp.status_code == 404:
            pass  # file doesn't exist on that branch yet -- fine, this creates it
        else:
            return False, f"Couldn't check existing file ({get_resp.status_code}): {_short(get_resp)}"
    except requests.RequestException as e:
        return False, f"Network error checking existing file: {e}"

    payload = {
        "message": message or f"Update {filename} via app",
        "content": content_b64,
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    try:
        put_resp = requests.put(url, headers=headers, json=payload, timeout=15)
    except requests.RequestException as e:
        return False, f"Network error pushing {filename}: {e}"

    if put_resp.status_code in (200, 201):
        commit_sha = (put_resp.json().get("commit") or {}).get("sha", "")[:7]
        return True, f"Pushed {filename}" + (f" (commit {commit_sha})" if commit_sha else "") + "."
    return False, f"Failed to push {filename} ({put_resp.status_code}): {_short(put_resp)}"


def push_all_config(token, repo, branch="main", filenames=None):
    """Pushes each of `filenames` (defaults to all of SYNCABLE_FILES), one
    commit per file. Returns a list of (filename, ok, message) in the same
    order."""
    targets = filenames if filenames is not None else [f for f, _ in SYNCABLE_FILES]
    return [(f, *push_file_to_github(f, token, repo, branch)) for f in targets]


def _short(resp):
    """Best-effort short error string from a GitHub API error response."""
    try:
        return resp.json().get("message", resp.text[:200])
    except Exception:
        return resp.text[:200]
