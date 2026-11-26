#!/usr/bin/env python3
"""Try every Gemini key the job has (the gemini-keys action exported them) with one tiny call each.

Run by gemini-keys-check.yml, by hand: "are all my keys loaded, and does each one answer?". One plain reply from
gemini-3.5-flash-lite per key -- a few dozen tokens -- and a line per key naming it (never printing it). Exits 1
when no key is found or any key fails, so the run goes red. The key names are the only identifying text printed.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from export_keys import split_keys  # noqa: E402  (stdlib only; the same split llm_util uses)

MODEL = "models/gemini-3.5-flash-lite"


def short_error(exc):
    text = str(exc).replace("\n", " ")
    code = next((c for c in ("429", "503", "500", "400", "401", "403", "404", "504") if text.lstrip().startswith(c)), "")
    reason = next((r for r in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "PERMISSION_DENIED", "API_KEY_INVALID",
                               "INVALID_ARGUMENT", "NOT_FOUND", "DEADLINE_EXCEEDED") if r in text), "")
    return f"{code} {reason}".strip() or type(exc).__name__


def main():
    from google import genai
    from google.genai import types

    secret_names = sorted((n for n in os.environ if n.startswith("GEMINI_API_KEY") and os.environ[n].strip()),
                          key=lambda n: (n != "GEMINI_API_KEY", n != "GEMINI_API_KEY_BACKUP", n))
    # One secret may hold several keys joined by commas (GEMINI_API_KEYS): each is tried on its own, named NAME[1],
    # NAME[2], ... by its place in the list, so a failure says which one to look at.
    keys = []
    for secret in secret_names:
        parts = split_keys(os.environ[secret])
        keys += [(secret, parts[0])] if len(parts) == 1 else [(f"{secret}[{n}]", part) for n, part in enumerate(parts, 1)]
    names = [name for name, _ in keys]
    print(f"{len(names)} Gemini key(s) in this job: {', '.join(names) or 'none'}")
    distinct = len({value for _, value in keys})
    if distinct != len(names):
        print(f"::warning::{len(names) - distinct} key(s) hold the same value as another (a key pasted twice rotates as one)")
    bad = 0
    for name, value in keys:
        client = genai.Client(api_key=value, http_options=types.HttpOptions(timeout=60_000))
        started = time.time()
        try:
            reply = client.models.generate_content(
                model=MODEL, contents="Reply with exactly the word: OK",
                config=types.GenerateContentConfig(max_output_tokens=20))
            ok = bool((reply.text or "").strip())
            print(f"  {name}: {'ok' if ok else 'EMPTY REPLY'} ({time.time() - started:.1f}s)")
            bad += not ok
        except Exception as exc:                         # noqa: BLE001 -- report, never raise
            print(f"  {name}: FAILED {short_error(exc)} ({time.time() - started:.1f}s)")
            bad += 1
        time.sleep(0.5)
    if not names:
        print("::error::no GEMINI_API_KEY* secret reached the job")
    elif bad:
        print(f"::error::{bad} of {len(names)} key(s) did not answer")
    else:
        print(f"All {len(names)} keys answered on {MODEL.split('/')[-1]}.")
    return 1 if (not names or bad) else 0


if __name__ == "__main__":
    sys.exit(main())
