#!/usr/bin/env python3
"""Export every GEMINI_API_KEY* secret to the job's environment ($GITHUB_ENV), and nothing else.

Reads the calling job's `${{ toJSON(secrets) }}` from ALL_SECRETS. That blob holds EVERY secret of the
repository (DATA_REPO_TOKEN, MARKET_DB_URL, ...), so this script is deliberately small and does three
things with it: keep the entries whose name starts with GEMINI_API_KEY (any case; Actions may hand the
names back in either), mask each value, and append it to $GITHUB_ENV under the upper-cased name
(llm_util discovers keys by that prefix, case-sensitively). Nothing else is written or printed: the log
gets the names (llm_util prints them too) and a count, never a value.

Stdlib only; checks/test_gemini_keys_export.py runs it with a fake secrets blob.
"""

import json
import os
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
                print(f"::add-mask::{value}")
                fh.write(env_file_block(name, value))
    names = ", ".join(sorted(keys))
    print(f"Gemini keys exported to the job: {len(keys)}" + (f" ({names})" if keys else ""))
    if not keys:
        print("::warning::no GEMINI_API_KEY* secret found -- the AI steps will have no key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
