#!/usr/bin/env python3
"""Run every offline check. Exits non-zero if any fails.

    python3 checks/run_all.py            # everything
    python3 checks/run_all.py slot_gate  # only suites whose name contains this

These need no secrets, no network and no data files -- that is the point: CI can
run them on every push (see .github/workflows/checks.yml), and so can you before
one. The checks that DO need real data or the network live outside the repo; see
README.md.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
pattern = sys.argv[1] if len(sys.argv) > 1 else ""
suites = sorted(p for p in HERE.glob("test_*.py") if pattern in p.name)

width = max(len(p.name) for p in suites) if suites else 0
failed, total_checks = [], 0
for suite in suites:
    proc = subprocess.run([sys.executable, str(suite)], capture_output=True, text=True)
    passes = proc.stdout.count("\nPASS") + proc.stdout.startswith("PASS")
    total_checks += passes
    last = [l for l in proc.stdout.strip().splitlines() if l.startswith(("FAILURES", "TOTAL"))]
    print(f"{suite.name:<{width}}  {passes:>3} passed  {last[-1] if last else 'no summary line'}")
    if proc.returncode != 0:
        failed.append(suite.name)
        print(proc.stdout[-3000:])
        print(proc.stderr[-2000:], file=sys.stderr)

print(f"\n{len(suites)} suite(s), {total_checks} checks, {len(failed)} failing")
if failed:
    print("FAILED: " + ", ".join(failed))
sys.exit(1 if failed else 0)
