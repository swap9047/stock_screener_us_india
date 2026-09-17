"""Is the copy of a data file already on the branch NEWER than this run's?

Exit 0  -> the branch copy is strictly newer; the caller should KEEP it and not
           overwrite it with this run's output.
Exit 1  -> anything else: ours is newer, the same age, one of them carries no
           timestamp, the file is new, or we could not tell.

The default is deliberately "push ours". Not pushing loses a run's work, which
is the worse failure; the guard exists only for the one case we can prove.

WHY: commit-data used `git rebase -X theirs origin/main`, and in a REBASE
"theirs" is the commit being replayed -- so this run's copy won every conflict.
That is fine for a file one workflow owns, but data_snapshot.json is committed
by BOTH data-refresh.yml and expert-views.yml (which runs refresh_data.py
first), and they sit in different concurrency groups, so they can overlap. The
loser's snapshot was silently discarded and an older as_of could land on top of
a newer one.

Same freshness keys and the same "never go backwards" rule the running app
already applies when it pulls these files (github_sync._FRESHNESS_KEYS).

Stdlib only -- this runs in the commit step, which installs nothing.
"""
import json
import subprocess
import sys

FRESHNESS_KEYS = ("generated_at", "as_of")


def stamp(text):
    """The file's own timestamp, or None if it hasn't one we can compare."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in FRESHNESS_KEYS:
        value = data.get(key)
        if value:
            return str(value)
    return None


def branch_text(path, ref="origin/main"):
    """The file's content at `ref`, or None if it isn't there."""
    try:
        out = subprocess.run(["git", "show", f"{ref}:{path}"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def main():
    if len(sys.argv) < 3:
        return 1
    repo_path, local_path = sys.argv[1], sys.argv[2]

    remote = branch_text(repo_path)
    if remote is None:
        return 1                      # not on the branch yet -- ours is the first
    try:
        with open(local_path) as f:
            ours = stamp(f.read())
    except OSError:
        return 1
    theirs = stamp(remote)

    # Both sides must carry a comparable stamp. These are ISO-8601 /
    # "YYYY-MM-DD HH:MM ET" strings, both lexicographically ordered, and a file
    # is only ever compared against itself -- so a string compare is the same
    # answer a parse would give, without a format list to keep in sync.
    if not ours or not theirs:
        return 1
    return 0 if theirs > ours else 1


if __name__ == "__main__":
    sys.exit(main())
