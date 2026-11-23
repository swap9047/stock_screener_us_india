#!/usr/bin/env python3
"""Run every offline check. Exits non-zero if any fails.

    python3 checks/run_all.py            # everything
    python3 checks/run_all.py slot_gate  # only suites whose name contains this
    CHECKS_JOBS=1 python3 checks/run_all.py   # one suite at a time

These need no secrets, no network and no data files -- that is the point: CI can
run them on every push (see .github/workflows/checks.yml), and so can you before
one. The checks that DO need real data or the network live outside the repo; see
README.md.

Suites run in parallel, CHECKS_JOBS at a time (default: the machine's CPU count),
each in its own process, its output held back and printed in the suites' sorted
order, so the report reads the same as a serial run. Serial, the suite took ~220 s
on 4 cores (2026-11-22), most of it in a handful of AppTest renders; each suite
already works in its own temp folder, so they do not share files -- with two
exceptions handled here: the default data files several suites bootstrap in the
repo folder are created once before the pool starts (bootstrap_repo_files), and
a suite in SERIAL_SUITES runs alone, after the parallel ones.
"""
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Suites that must not overlap any other: one that writes into the repo folder
# itself, say. None today -- running each suite alone and diffing the folder
# (2026-11-22) found only the bootstrapped defaults below, which are made first.
SERIAL_SUITES = set()

# stock_data's loaders write a default markets.json / watchlist_groups.json into
# the repo folder when it is missing (ten suites do, serially the first one made
# it). In parallel that write would land while the AppTest renders copy the repo
# folder, and atomic_write_json's temp file, listed and then gone, fails the copy.
# So they are made once, before any suite runs -- what a serial run did in its first
# second. A folder that has them (a real app folder) is left alone.
BOOTSTRAP = "import stock_data; stock_data.load_markets_registry(); stock_data.load_watchlist_groups()"


def bootstrap_repo_files():
    proc = subprocess.run([sys.executable, "-c", BOOTSTRAP], cwd=HERE.parent, capture_output=True, text=True)
    if proc.returncode != 0:
        # Not fatal: the suites that read them report it themselves.
        print("(could not create the default data files first: "
              f"{(proc.stderr.strip().splitlines() or ['?'])[-1]})")


def _jobs():
    try:
        n = int(os.environ.get("CHECKS_JOBS", "") or 0)
    except ValueError:
        n = 0
    return max(1, n or os.cpu_count() or 1)


def _run(suite):
    return subprocess.run([sys.executable, str(suite)], capture_output=True, text=True)


def main():
    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    suites = sorted(p for p in HERE.glob("test_*.py") if pattern in p.name)
    width = max(len(p.name) for p in suites) if suites else 0
    jobs = _jobs()
    started = time.time()

    pooled = [s for s in suites if s.name not in SERIAL_SUITES] if jobs > 1 else []
    if pooled:
        bootstrap_repo_files()
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {s: pool.submit(_run, s) for s in pooled}

        def result(suite):
            # A serial suite starts only once every pooled one has finished, so it
            # never overlaps them; with CHECKS_JOBS=1 every suite runs here, one by one.
            if suite in futures:
                return futures[suite].result()
            for f in futures.values():
                f.result()
            return _run(suite)

        failed, total_checks = [], 0
        # Printed in sorted order as each becomes available: the report is the
        # serial one, line for line, whatever order the suites finished in. A
        # serial suite's turn waits for the whole pool (result above), so the
        # pooled suites after it are already done by the time it has run.
        for suite in suites:
            proc = result(suite)
            passes = proc.stdout.count("\nPASS") + proc.stdout.startswith("PASS")
            total_checks += passes
            last = [l for l in proc.stdout.strip().splitlines() if l.startswith(("FAILURES", "TOTAL"))]
            print(f"{suite.name:<{width}}  {passes:>3} passed  {last[-1] if last else 'no summary line'}", flush=True)
            if proc.returncode != 0:
                failed.append(suite.name)
                print(proc.stdout[-3000:])
                print(proc.stderr[-2000:], file=sys.stderr)

    print(f"\n({jobs} at a time, {time.time() - started:.0f} s)")
    print(f"{len(suites)} suite(s), {total_checks} checks, {len(failed)} failing")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
