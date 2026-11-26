#!/usr/bin/env python3
"""Export every GEMINI_API_KEY* secret to the job's environment ($GITHUB_ENV), and nothing else.

Reads the calling job's `${{ toJSON(secrets) }}` from ALL_SECRETS. That blob holds EVERY secret of the
repository (DATA_REPO_TOKEN, MARKET_DB_URL, ...), so this script is deliberately small and does three
things with it: keep the entries whose name starts with GEMINI_API_KEY (any case; Actions may hand the
names back in either), mask each value, and append it to $GITHUB_ENV under the upper-cased name
(llm_util discovers keys by that prefix, case-sensitively). Nothing else is written or printed: the log
gets the names (llm_util prints them too) and a count, never a value.

A secret may hold several keys joined by commas (GEMINI_API_KEYS, owner 2026-11-26). The runner masks only a
secret's WHOLE value, so a single key printed on its own (an SDK error quoting its request) would show in this public
repo's log: each key of a joined value is masked separately here. llm_util splits the value itself.

Stdlib only; checks/test_gemini_keys_export.py runs it with a fake secrets blob.
"""

import json
import os
import re
import secrets
import sys

PREFIX = "GEMINI_API_KEY"


def keys_from(blob):
    """{NAME: value} for the GEMINI_API_KEY* entries of a toJSON(secrets) string; {} when it is not a JSON
    object. A blank value (a secret that is declared but empty) is skipped."""
    try:
        data = json.loads(blob or "{}")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    found = {}
    for name, value in data.items():
        upper = str(name).upper()
        if upper.startswith(PREFIX) and isinstance(value, str) and value.strip():
            found[upper] = value.strip()
    return found


def split_keys(value):
    """The keys one secret holds: comma (or line break) separated, stripped, blanks dropped. The same rule as
    llm_util.split_key_list -- checks/test_gemini_keys_export.py holds the two to the same answers."""
    return [part.strip() for part in re.split(r"[,\n]", value or "") if part.strip()]


def env_file_block(name, value):
    """The $GITHUB_ENV text for one variable, in the multi-line-safe form with a random delimiter (a plain
    NAME=value line would let a value with a newline define other variables)."""
    delimiter = "ghadelim_" + secrets.token_hex(16)
    while delimiter in value:
        delimiter = "ghadelim_" + secrets.token_hex(16)
    return f"{name}<<{delimiter}\n{value}\n{delimiter}\n"


def main():
    keys = keys_from(os.environ.get("ALL_SECRETS", ""))
    env_file = os.environ.get("GITHUB_ENV")
    if keys and not env_file:
        print("::error::GITHUB_ENV is not set -- cannot export the Gemini keys")
        return 1
    if keys:
        with open(env_file, "a", encoding="utf-8") as fh:
            for name, value in sorted(keys.items()):
                parts = split_keys(value)
                # A mask command is ONE line: a value with a line break cannot be masked whole (its later lines
                # would print as plain log text), so it is masked key by key. A joined list is masked whole AND key by key.
                masks = [value] if "\n" not in value else []
                if len(parts) > 1:
                    masks += parts
                for mask in masks:
                    print(f"::add-mask::{mask}")
                fh.write(env_file_block(name, value))
    names = ", ".join(sorted(keys))
    total = sum(len(split_keys(v)) for v in keys.values())
    print(f"Gemini keys exported to the job: {len(keys)}" + (f" ({names})" if keys else "")
          + (f"; {total} key(s) in all" if total != len(keys) else ""))
    if not keys:
        print("::warning::no GEMINI_API_KEY* secret found -- the AI steps will have no key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
