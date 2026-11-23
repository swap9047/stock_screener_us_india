"""Three-way, per-entry merge of a keyed data file ({ticker: view}) at commit time.

    python3 merge_keyed.py <branch copy> <this run's copy> <base ref>

Rewrites <branch copy> (the file at the branch tip, in the data repo checkout)
in place. <base ref> names the data-repo commit this job loaded at its start
(load-data tags it `data-loaded`); the base is `git show <base ref>:<file>`.
For each top-level key:

  - the run did not change it (same as the base)  -> the branch's value (or its absence);
  - the run changed it and the branch did not     -> the run's value, or deleted
                                                     when the run removed it (a prune);
  - both changed it, differently                   -> the entry with the newer `as_of`
                                                     (`generated_at`) stamp; a tie or a
                                                     missing stamp keeps the run's. An
                                                     entry one side deleted and the other
                                                     rewrote is kept: losing a view costs
                                                     hours of API time, a leftover one is
                                                     pruned by the next night's run.

An entry is replaced whole, never field by field: two views' fields mixed would
be a view nobody wrote.

Exit 0 when the branch copy was written (the caller stages it), 1 when this
run's copy cannot be read (the caller leaves the file out).

WHY (2026-11-22): expert-views.yml and fundamentals.yml committed their file
WHOLE. Neither file carries a top-level stamp, so newer_on_branch.py could not
tell, and the run's copy -- loaded hours earlier -- overwrote every entry
written to the branch meanwhile: the dashboard's per-ticker re-analyze
(github_sync.push_json_entry_changes) and the Sentiment an expert-views run
writes per entry (merge_entries.py). The run's own work and its prunes still
land; nothing it did not touch is reverted.

Stdlib only -- this runs in the commit step, which installs nothing.
"""
import json
import os
import subprocess
import sys
import tempfile

STAMP_KEYS = ("as_of", "generated_at")
_ABSENT = object()


def entry_stamp(value):
    if not isinstance(value, dict):
        return None
    for key in STAMP_KEYS:
        if value.get(key):
            return str(value[key])
    return None


def merge(base, ours, theirs):
    """The merged dict (see the module docstring). Key order follows the run's
    copy, then the branch's keys the run does not have."""
    merged = {}
    order = list(ours) + [k for k in theirs if k not in ours]
    for key in order:
        b = base.get(key, _ABSENT)
        o = ours.get(key, _ABSENT)
        t = theirs.get(key, _ABSENT)
        if o == b:
            pick = t
        elif t == b or t == o:
            pick = o
        elif o is _ABSENT:
            pick = t
        elif t is _ABSENT:
            pick = o
        else:
            so, st = entry_stamp(o), entry_stamp(t)
            # "YYYY-MM-DD HH:MM" stamps of one writer family order as strings.
            pick = t if (so and st and st > so) else o
        if pick is not _ABSENT:
            merged[key] = pick
    # Keys only the base had (deleted on both sides) stay deleted.
    return merged


def read_base(ref, path):
    """The file at `ref` as a dict; {} when the ref or the file is missing or
    not a JSON object -- every entry then counts as changed by the run, and a
    conflict is settled by the stamps."""
    if not ref:
        return {}
    try:
        out = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def main(branch_path, ours_path, base_ref=""):
    try:
        with open(ours_path, encoding="utf-8") as f:
            ours = json.load(f)
    except (OSError, ValueError):
        return 1
    if not isinstance(ours, dict):
        return 1
    try:
        with open(branch_path, encoding="utf-8") as f:
            theirs = json.load(f)
    except FileNotFoundError:
        theirs = {}
    except (OSError, ValueError):
        theirs = None
    if not isinstance(theirs, dict):
        # A branch copy that is not a keyed object has no entries to keep: the
        # run's copy replaces it, as the whole-file commit always did.
        merged = ours
    else:
        merged = merge(read_base(base_ref, branch_path), ours, theirs)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(branch_path)) or ".")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)  # as json_store.atomic_write_json writes it
    os.chmod(tmp, 0o644)
    os.replace(tmp, branch_path)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(1)
    sys.exit(main(*sys.argv[1:4]))
