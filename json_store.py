"""Reading and writing the JSON data files -- stdlib only, deliberately.

WHY THIS IS ITS OWN MODULE: these three names used to live in stock_data.py,
which imports numpy, pandas and yfinance at module scope. daily-alerts.yml's
gate job installs ONLY `requests` -- the whole point of that job is to answer
"is any rule due today?" without paying for the heavy dependency set before the
real check job starts. When `alerts.load_rules()` started calling
stock_data.read_json_strict, importing alerts pulled numpy in through the back
door and the gate died with:

    ModuleNotFoundError: No module named 'numpy'

A failing gate skips the work job, so no alert could fire at all. Keeping the
file primitives here means `alerts` (and anything else a lightweight job needs)
can read and write config without dragging the analysis stack along.

stock_data re-exports all three, so every existing
`from stock_data import read_json_strict` keeps working unchanged.

Anything added here must stay importable with nothing but the standard library.
checks/test_gate_imports.py enforces that.
"""

import json
import os


class DataFileError(RuntimeError):
    """A user-data JSON file on disk is unreadable.

    Raised instead of returning an empty default, because these files ARE the
    user's data: an empty default is indistinguishable from real emptiness, and
    the next save would write that emptiness over the data repo.
    """


def read_json_strict(path, default=None):
    """Parse a user-data file; return `default` if it does not exist yet, and
    raise DataFileError -- naming the file -- if it exists but will not parse.
    A torn file is what a crash mid-write used to leave behind; every writer
    now goes through atomic_write_json, so this should only ever fire on a file
    damaged from outside the app."""
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        raise DataFileError(
            f"{os.path.basename(path)} is unreadable ({e}). Nothing was loaded or changed -- "
            "restore it from the data repo, or delete it to start that file from defaults."
        ) from e


def atomic_write_json(path, data, default=None, sort_keys=False):
    """Write JSON via temp file + os.replace.

    The plain truncate-and-write this replaces was called once PER TICKER by
    the refresh loops -- ~110 rewrites of a 150 KB file per run. A crash or a
    job timeout landing mid-dump left truncated JSON, and the loader's bare
    except then returned {} on the next run, so the whole store was silently
    rebuilt from empty with every prior view lost.

    Lives here because all three stores need it: it was copy-pasted privately
    into fundamentals_eval and expert_views, and missing entirely from
    news_summary, whose plain write could leave a torn file that
    load_news_summary's bare except then reported to the UI as "no digest yet".
    A crash or a cancelled workflow landing mid-dump is not hypothetical -- a
    fundamentals run was cancelled on 2026-09-03.

    `sort_keys` is for the files that are committed to the data repo and want a
    stable diff (weekly_wrapup_state.json); it defaults off so every existing
    caller's output is byte-identical to before.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=default, sort_keys=sort_keys)
    os.replace(tmp, path)
