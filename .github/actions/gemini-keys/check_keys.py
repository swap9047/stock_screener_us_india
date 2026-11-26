#!/usr/bin/env python3
"""Try every Gemini key the job has (the gemini-keys action exported them) with one tiny call each.

Run by gemini-keys-check.yml, by hand: "are all my keys loaded, and does each one answer?". One plain reply from
gemini-3.5-flash-lite per key -- a few dozen tokens -- and a line per key naming it (never printing it). Exits 1
when no key is found or any key fails, so the run goes red. The key names are the only identifying text printed.
"""

import os
import sys
import time

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

    names = sorted((n for n in os.environ if n.startswith("GEMINI_API_KEY") and os.environ[n].strip()),
                   key=lambda n: (n != "GEMINI_API_KEY", n != "GEMINI_API_KEY_BACKUP", n))
    print(f"{len(names)} Gemini key(s) in this job: {', '.join(names) or 'none'}")
    distinct = len({os.environ[n].strip() for n in names})
    if distinct != len(names):
        print(f"::warning::{len(names) - distinct} key(s) hold the same value as another (a key pasted twice rotates as one)")
    bad = 0
    for name in names:
        client = genai.Client(api_key=os.environ[name].strip(), http_options=types.HttpOptions(timeout=60_000))
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
