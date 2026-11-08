"""Commit only the ENTRIES a run wrote into a keyed data file, not the whole file.

    python3 merge_entries.py <branch copy> <this run's copy> <entries list>

Rewrites <branch copy> in place: each key named in <entries list> (a JSON list,
`<file>.entries`, written by the job next to its output) takes this run's value;
every other key keeps the branch's. Exit 0 when something was merged, 1 when
there is nothing to merge (no list, an empty list, or a file we cannot read), so
the caller leaves the file out of the commit.

WHY (owner, 2026-11-08): expert-views.yml now writes a stock's missing Sentiment
before its Expert Take on a run someone asked for. fundamentals.json is owned by
fundamentals.yml, a different concurrency group, so the two can overlap; copying
the expert job's whole fundamentals.json -- loaded when that job started -- over
the branch would revert every Sentiment the other run finished meanwhile. The
dashboard pushes its AI edits per entry for the same reason
(github_sync.push_json_entry_changes).

Stdlib only -- this runs in the commit step, which installs nothing.
"""
import json
import os
import sys
import tempfile


def merge(branch_path, ours_path, entries_path):
    try:
        with open(entries_path, encoding="utf-8") as f:
            keys = json.load(f)
        with open(ours_path, encoding="utf-8") as f:
            ours = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(keys, list) or not keys or not isinstance(ours, dict):
        return False
    try:
        with open(branch_path, encoding="utf-8") as f:
            branch = json.load(f)
    except FileNotFoundError:
        branch = {}
    except (OSError, ValueError):
        # A branch copy we cannot parse is not ours to rewrite.
        return False
    if not isinstance(branch, dict):
        return False
    for key in keys:
        if key in ours:
            branch[key] = ours[key]
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(branch_path)) or ".")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(branch, f, indent=2)  # as json_store.atomic_write_json writes it
    os.replace(tmp, branch_path)
    return True


if __name__ == "__main__":
    sys.exit(0 if merge(*sys.argv[1:4]) else 1)
