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
| `test_fable_review.py` | The 2026-09-17 evening review's fixes: a subset settings save keeps the rest; `is_rule_due` judges the slot's day; substituted snapshot rows are re-enriched before the jobs judge them; the search ladders retry the primary model; a hung yfinance call leaves no non-daemon worker; the combined-tab keys cannot be minted as a watchlist; and the follow-ups a review of those fixes found -- the app's own snapshot-writing paths re-enrich too, a one-file force pull leaves the shared pull clock alone, a timeout raised by the call keeps its own message |
| `test_ta_rules.py` | TA Rules follows the weekly EMA flowchart node for node; support/resistance zones; only completed weeks are judged; the label stays on one line; the flowchart image is read from the private data repo, never stored here |
| `test_expert_take_100426.py` | Expert Take decided in code from Trend, Tech Uptrend, TA Rules and Sentiment; the news step (14 days, one step down, never up, expiry); refusals and "nothing found" prose are not news |
| `test_trend_volume_100426.py` | Median-day volume tests, Trend neutral bands, Tech Uptrend on unrounded prices with VStop Up |
| `test_breadth_100426.py` | Breadth: a market whose latest completed session is already stored is skipped (never on a failed check); Mon-Fri slots; 5 s between batches, failures retried one by one 5 s apart (up to 3 rounds); the survivorship-bias caption |
| `test_news_digest_100426.py` | News digest: editor ladder with 30 s waits and a code-formatted fallback; company headers set in code; filler dropped; funds skipped; dated by the 6 AM ET slot; repeats of the previous digest dropped (and never from a same-slot retry); end-to-end with stubbed models |
| `test_ticker_dot.py` | Signal is retired everywhere (no column, filter, alert metric, colours or row field); the ticker dot is Expert Take's colour unless you set a Flag; Flag is manual-only; ⚑ marks only a news downgrade |
| `test_expert_prompt_inputs.py` | Expert Take never sees its own previous verdict (no auto flag, no notes, no verdict-based alert rules); section 4 carries the quarter's checked facts, not Sentiment's label; an unreadable `fundamentals.json` does not stop the run |
| `test_sentiment_guidance.py`, `test_sentiment_window.py` | Sentiment rules 7-9 (guidance outranks the quarter, quoted outlook never decides alone, only named-firm analyst actions); the search window reaches back to the last reported results |
| `test_failure_fuse.py` | 10 consecutive failed tickers stop the Sentiment / Expert Take job and exit 1; scattered failures never do |
| `test_disclaimer.py` | Every Discord batch ends with the disclaimer without breaking the length limit; the sidebar shows it |
| `test_code_fingerprint.py` | The snapshot fingerprint ignores comments and docstrings, changes on any code edit, and is the same on every Python (a pinned sample) |
| `test_validate_ticker.py` | A ticker added while Yahoo throttles is kept with a warning, not dropped as a typo |
| `test_review_100326.py` | The 2026-10-03 review: Trend's Mixed state; edited config is pushed at the end of the run (never a file with no baseline, never under `SKIP_GITHUB_PULL`); a mostly-failed news run posts nothing; a partial manual run never counts as the slot's work; Data Thru counts missed sessions; Reset resets calculation settings only; the benchmark shown is the one RS used; one setting drawn by several widgets can't be reverted by a stale copy |

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
- `backtest_signals.py` -- replays the data repo's daily snapshots and measures
  each signal state's 5/20/60-day return against its benchmark. Reads holdings,
  so it stays local; useful from mid-October 2026, once 4-week returns exist.
