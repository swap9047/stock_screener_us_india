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
| `test_py311_syntax.py` | Every tracked .py file parses on Python 3.11, which CI runs: compiled there, and on 3.12+ scanned for f-strings that reuse their own quote inside (valid only from 3.12) |
| `test_slot_gate.py` | Scheduling: one run per ET slot, late runs inside the window, both DST switchovers, a second fire, a previous run whose gate declined, breadth's two slots, and paging far enough back to see the run that did the work |
| `test_vercel_deploys.py` | Which pushes deploy to Vercel (`vercel.json`'s `ignoreCommand`, `scripts/vercel-ignore.sh`): only `main`, only when a file the two functions import changed. The watched list must cover their whole import closure, and the script is run in a throwaway git repo for each case (including the ones that must build) |
| `test_workflows.py` | Every data workflow loads data before its first Python step, pipes output through `log_redact.py` with `shell: bash`, commits only via `commit-data`, and never writes this repo |
| `test_data_repo_sync.py`, `test_github_sync.py` | The data-repo config and first-start download; per-entry pushes against an in-memory Git Data API (a newer remote entry survives, a concurrent commit is retried, two files go in one commit) |
| `test_log_redact.py` | Tickers, company names and watchlist names are masked in log output; exit status survives the pipe |
| `test_gemini_keys.py` | Every `GEMINI_API_KEY*` name joins the rotation, load spreads across all keys, a key that hits its quota is skipped |
| `test_refresh_limit.py` | The `limit` input analyses only the first N tickers; a partial news run replaces only its own digest and posts nothing |
| `test_fable_review.py` | The 2026-09-17 evening review's fixes: a subset settings save keeps the rest; `is_rule_due` judges the slot's day; substituted snapshot rows are re-enriched before the jobs judge them; the search ladders retry the primary model; a hung yfinance call leaves no non-daemon worker; the combined-tab keys cannot be minted as a watchlist; and the follow-ups a review of those fixes found -- the app's own snapshot-writing paths re-enrich too, a one-file force pull leaves the shared pull clock alone, a timeout raised by the call keeps its own message |
| `test_ta_rules.py` | TA Rules follows the weekly EMA flowchart node for node; support/resistance zones; only completed weeks are judged; the label stays on one line; the flowchart image is read from the private data repo, never stored here |
| `test_expert_take_100426.py` | Expert Take decided in code from Trend, Tech Uptrend, TA Rules and Sentiment; the news step (14 days, one step down, never up, expiry); refusals and "nothing found" prose are not news |
| `test_trend_volume_100426.py` | Median-day volume tests, Trend neutral bands, Tech Uptrend on unrounded prices with VStop Up |
| `test_breadth_charts.py` | News-tab charts: mobile User-Agents, no zoom or drag on a phone, the reference shapes actually drawn, an empty panel's note in that panel and the date axis not squashed, diverging highs/lows bars, the 50-day overlay, per-panel legends and full hover labels, Refresh Charts starting the breadth workflow, and that workflow refreshing the watchlist snapshot |
| `test_watchlist_breadth.py` | Watchlist breadth: the shared breadth maths (per-measure, per-day denominators; warmup), the job computing through it, `assemble_closes` (dedupe, benchmark from another block, missing tickers), the view's dropdown and per-watchlist benchmark, 6-year performance data, and a stored latest close kept when Yahoo blanks it |
| `test_breadth_100426.py` | Breadth: the latest session from each exchange's clock (from the open), and a market that already has it finally is skipped; live intraday readings redone until the final close replaces them; 6-month and 1-year chart views; a part-published day computed and marked partial, one under half held back, a holiday told apart; never replacing a stored reading with a staler one; Mon-Fri slots; 5 s between batches, failures retried one by one 5 s apart (up to 3 rounds); the survivorship-bias caption |
| `test_news_digest_100426.py` | News digest: editor ladder with 30 s waits and a code-formatted fallback; company headers set in code; filler dropped; funds skipped; dated by the 6 AM ET slot; repeats of the previous digest dropped (and never from a same-slot retry); end-to-end with stubbed models |
| `test_ticker_dot.py` | Signal is retired everywhere (no column, filter, alert metric, colours or row field); the ticker dot is Expert Take's colour unless you set a Flag; Flag is manual-only; ⚑ marks only a news downgrade |
| `test_expert_prompt_inputs.py` | Expert Take never sees its own previous verdict (no auto flag, no notes, no verdict-based alert rules); section 4 carries the quarter's checked facts, not Sentiment's label; an unreadable `fundamentals.json` does not stop the run |
| `test_sentiment_guidance.py`, `test_sentiment_window.py` | Sentiment rules 7-9 (guidance outranks the quarter, quoted outlook never decides alone, only named-firm analyst actions); the search window reaches back to the last reported results |
| `test_failure_fuse.py` | 10 consecutive failed tickers stop the Sentiment / Expert Take job and exit 1; scattered failures never do |
| `test_disclaimer.py` | Every Discord batch ends with the disclaimer without breaking the length limit; the sidebar shows it |
| `test_code_fingerprint.py` | The snapshot fingerprint ignores comments and docstrings, changes on any code edit, and is the same on every Python (a pinned sample) |
| `test_market_api.py` | What the screener shares with other apps: the catalog matches the screener's own labels, nothing held (watchlist, notes, flags, Interested, custom columns) is ever published, one live fetch per benchmark, and the Vercel endpoint's groups, errors and token |
| `test_market_lookup.py` | The live lookup and Claude chat's MCP server: names resolve as the research library resolves them (its `match_key`, decisions first, NSE before BSE and a US symbol, a unique prefix, only a clearly closest fuzzy match, else Yahoo), fresh rows are reused and stale ones computed once and saved with their AI write-ups and guarded Sentiment kept, and the MCP protocol (initialize, tools, errors, batches, notifications) over HTTP with the token in the path |
| `test_publish_watchlists.py` | `publish_market.py watchlists`: the tabs in order then the groups (stocks once each, a benchmark only when shared), index breadth as stored and each list's by `breadth_math`, two years of closes from the first block holding a ticker, the 10-day rewrite vs a full first write, deletes of gone lists and old closes, nothing written when no list loaded; and the Perf 1W %, Perf YTD % and 1D Ret vs Index columns through `fetch_snapshot` and every registry |
| `test_market_lists.py` | Claude chat's list tools: loose watchlist names (an ambiguous one lists the choices), a tab as a sorted, limited table whose summary covers every stock, breadth by index or tab with days capped, closes for a tab plus its benchmark or for names (an index name is its benchmark, a ticker not stored fetched from Yahoo), thinning that keeps the latest close, and errors instead of exceptions |
| `test_list_read.py` | The invested lists' reads: the holdings callouts (trend mix, below the 200-day, near a 52-week low, leaders and laggards) and the quarterly results row worked out by hand; the facts (the list's own breadth with the index's beside it, the return against the home index, the index read as market context, results and holdings only for the latest session); the prompt (names, market read, the last three earlier reads); the check on the answer (numbers only from the facts or the market read, two to five takeaways, no advice, a part with no facts must be null); the job (after the index reads, once a session, force, backfill, saved as it goes, a failed ladder, no name in the log); the box; the library copy and `get_market_read` serving a list by name |
| `test_market_read.py`, `test_market_read_render.py` | The News tab's market reads: each chart's facts against an independent calculation, the number check (only numbers the facts hold, no advice, null for a chart without facts), the prompt (last three earlier reads, no ticker), the ladder (3.8-flash x4 on different keys, then 3.6-flash, then flash-lite, 4096 thinking, JSON), the job (a forming reading waits, written once, force, backfill, a failed ladder saves nothing), storage, and `market-read.yml` (7:15 / 5:15 ET, waits for the breadth run, redacted); rendered: the (i) buttons, the box, the dropdown, no file, a phone |
| `test_model_settings.py`, `test_model_settings_render.py` | The sidebar's Settings · Models: with nothing saved every step's rungs equal the ladders `llm_util` builds (search, reasoning, collation's alternating pair and last resort, the market read); the old pickers' settings still decide a step; a saved chain's tries per model, the collation pair, junk entries ignored; saving, Default (the old keys go back too), the repository, the keys surviving settings.json and not being calculation settings; Test (a grounded search for a search step, thinking only on Gemini, failures reported); the pipelines taking their rungs from the chain; rendered: the closed expander builds nothing, the nine-step table, a picker and tries box per model, Add / Remove, Save, Default, the repository, Test without a key, and the old pickers gone |
| `test_unpublished_bars.py` | A daily bar Yahoo hasn't published yet (Volume only) is rebuilt from its 5-minute bars once the session has settled, with the quote's official close; never a forming session, a blank older than 5 days, a date without bars, or after a failed download; through `fetch_snapshot` the rebuilt day is the row's latest |
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
