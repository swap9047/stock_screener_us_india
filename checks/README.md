# Checks

There is no test framework here, and this app's failures are usually silent: a
workflow reports success while the data quietly goes wrong. These are the
regression checks that stand in for one, written as plain scripts so they need
nothing installed beyond `requirements.txt`.

```bash
python3 checks/run_all.py              # all of it, ~30 s
python3 checks/run_all.py slot_gate    # one suite
```

`.github/workflows/checks.yml` runs exactly this on every push and pull request.
It needs no secrets, so it works from a fork.

| Suite | Covers |
|---|---|
| `test_data_integrity.py` | A malformed alert condition fails closed instead of crashing every tab and the nightly job; a corrupt user-data file is loud (`DataFileError`) rather than an empty default the next save would push; every root-JSON writer is atomic; the headless jobs refuse a partial universe but tolerate a few missing tickers |
| `test_slot_gate.py` | Scheduling: one run per ET slot, late runs inside the window, both DST switchovers, a second fire, a previous run whose gate declined, breadth's two slots, and paging far enough back to see the run that did the work |
| `test_workflows.py` | Every data workflow loads data before its first Python step, pipes output through `log_redact.py` with `shell: bash`, commits only via `commit-data`, and never writes this repo |
| `test_data_repo_sync.py`, `test_github_sync.py` | The data-repo config and first-start download; per-entry pushes against an in-memory Git Data API (a newer remote entry survives, a concurrent commit is retried, two files go in one commit) |
| `test_log_redact.py` | Tickers, company names and watchlist names are masked in log output; exit status survives the pipe |
| `test_gemini_keys.py` | Every `GEMINI_API_KEY*` name joins the rotation, load spreads across all keys, a key that hits its quota is skipped |
| `test_refresh_limit.py` | The `limit` input analyses only the first N tickers; a partial news run replaces only its own digest and posts nothing |

Fixtures are invented (`ACME`, `ZED.NS`, "Newsletter Picks"). Nothing here reads
or writes the real JSON: every path is redirected to a temp directory. Keep it
that way -- this repo is public.

## Not here, and why

These need real prices, the private data repo, or a live GitHub API, so they
cannot run in CI. They live outside the repo in `docs/private/checks/`
(gitignored) and are run by hand when the matching area changes:

- `test_findings.py` -- one assertion per finding from the 2026-09-14 review
  (36). Asserts against the real snapshot, so it stays local.
- `apptest_bootstrap.py` -- renders the app from a copy with no JSON at all:
  with a valid token it downloads and renders, with a bad one it stops.
- `apptest_residuals.py` -- the webhook is never echoed to the page; a clashing
  custom column cannot take over a built-in.
- `e2e_edits.py` -- watchlist edits against a throwaway branch of the real data
  repo.
- `simulate_jobs.sh` -- the alert and wrap-up jobs end to end against live
  Yahoo, pushing only to a local mirror.
- `scan_run_log.py` -- scans a finished Actions run's log for anything personal.
