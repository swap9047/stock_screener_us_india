# AGENTS.md — working in this codebase

> Canonical guide for any coding agent (Antigravity, Cursor, Codex, …). `CLAUDE.md`
> imports this file rather than copying it, so there is one source of truth — edit here.

A self-hosted Streamlit stock screener: technical indicators + AI verdicts over several
watchlists, with GitHub Actions cron jobs for the background work and Discord alerts.

Read this before changing anything. It is deliberately a **contract plus traps**, not a
tour — most bugs this repo has hit were registration or ordering mistakes that the
sections below would have prevented.

---

## Running it

```bash
streamlit run app.py
```

There is a login gate (`get_auth_credentials`, `app.py`). It's skipped entirely when no
credentials are configured, and in headless tests you bypass it by seeding
`session_state["authenticated"] = True` (see **Verifying a change**).

Secrets come from Streamlit secrets first, then env vars: `DATA_REPO_TOKEN`,
`GEMINI_API_KEY`, `GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_BRANCH`, `DISCORD_WEBHOOK_URL`,
`AUTH_USERNAME`, `AUTH_PASSWORD`. See `DEPLOYMENT.md`. Locally, `llm_util` and `app.py`
also load `.env`.

**Gemini keys are discovered by name prefix:** every secret or env var starting with
`GEMINI_API_KEY` joins the rotation (`llm_util.gemini_api_keys`), and
`RotatingGeminiClient` picks one per call to divide the ~250-call nightly load. Adding a
key is a config change here, but a workflow change too -- Actions only exposes secrets a
step names, so each key needs a line in the AI steps' `env:` blocks. Every run logs
`[key rotation] N key(s): ...`; if that number is lower than expected, that line is
missing.

**There is no test framework, but there are checks.** `python3 checks/run_all.py` runs 507
offline regression checks in ~60 s (no secrets, no network, no data files), and
`.github/workflows/checks.yml` runs them on every push. Anything needing real prices, the
private data repo or a live API is run by hand — see `checks/README.md`. Changes are also
verified by rendering the app headlessly; recipe at the bottom.

---

## Module map

~16.4k lines total (the app itself; checks/ adds ~2.1k). The weighting matters: `app.py` is nearly 40% of it.

| File | Lines | What it owns |
|---|---:|---|
| `app.py` | 6222 | The entire UI: tabs, tables, sidebar, filters, sort, editors, AI control bars, News + Alert Rules tabs |
| `stock_data.py` | 2741 | yfinance fetching, all indicator maths, watchlist/markets registry IO, `get_filterable_metrics` |
| `alerts.py` | 989 | Alert rule evaluation + Discord message building |
| `news_summary.py` | 840 | News gathering + LLM summarisation |
| `fundamentals_eval.py` | 685 | Sentiment ("fundamental view") generation + validation |
| `github_sync.py` | 657 | Atomic config push + `workflow_dispatch` trigger |
| `expert_views.py` | 602 | Expert Take verdict generation |
| `filters.py` | 456 | The boolean condition engine — shared by UI filters **and** background alerts |
| `llm_util.py` | 496 | Shared Gemini-call plumbing (timeout wrapper, retry/model-ladder logic) for the three AI pipelines |
| `weekly_wrapup.py` | 365 | Weekly Discord digest |
| `custom_columns.py` | 285 | User-defined formula columns |
| `ticker_notes.py` | 259 | Per-ticker notes/flags + auto-flag voting |
| `watchlist_labels.py` | 46 | Tab labels a watchlist may not take. Kept out of `stock_data.py`, whose whole-file code fingerprint marks the snapshot stale on any edit |

`refresh_*.py` and `*_check.py` are thin entry points that exist only to be run by GitHub
Actions. They contain no logic worth duplicating — they call into the modules above.

`filters.py` being shared is load-bearing: a rule must evaluate identically in the UI
preview and in the nightly Discord job. Don't fork that logic.

`llm_util.py` being shared is load-bearing too: `news_summary.py`, `expert_views.py` and
`fundamentals_eval.py` each used to carry their own copy of the timeout wrapper and retry
logic, and the copies drifted (only one pipeline ever gained a same-model retry before
falling back). Add a new AI pipeline stage on top of `run_model_ladder`/`standard_tiers`,
not a fourth copy.

---

## JSON files are the database

There is no DB. Everything is JSON, and **none of it is committed to this repo**. This code
repo is public (free Actions minutes), so the data lives in the private repo
`github_sync.DATA_REPO_DEFAULT`. The code repo ignores root `*.json`.
- At runtime the files still sit in the app folder, so every `*_FILE` path is unchanged.
- A running app re-pulls data files every 5 minutes (`pull_generated_files`). Generated
  files never go backwards in time. User config is only overwritten while the local copy
  is still byte-identical to the last pull or push. Data commits don't redeploy the app,
  so without this a running container would keep stale config and push it back.
- The app downloads missing files at startup with `bootstrap_data_files`, and reads and
  writes through `get_data_repo_config` (`DATA_REPO_TOKEN`). `get_github_config`
  (`GITHUB_TOKEN`) is only for dispatching workflows. Never use it for data: that would
  push holdings back into the public repo.
- Workflows use `.github/actions/load-data` (checks the data repo out into `data/` and
  copies its JSON in) and `.github/actions/commit-data` (copies named outputs back and
  pushes them).

The files fall into **three classes that must not be conflated**:

**1. User config** — edited in the UI, pushed to the data repo by `github_sync.push_all_config`.
The authoritative list is `SYNCABLE_FILES` in `github_sync.py`:
`watchlist.json`, `markets.json`, `interested.json`, `custom_filters.json`,
`settings.json`, `alerts_config.json`, `column_prefs.json`, `custom_columns.json`,
`ticker_notes.json`, `expert_views.json`, `fundamentals.json`, `ticker_index.json`,
`data_snapshot.json`, `watchlist_groups.json`.

Three of those are also class 2 (`WORKFLOW_GENERATED_FILES`), and the app must never push
them wholesale from its own disk. The container's copy can be older than a workflow
commit, and a whole-file push reverts it. Instead:
- The "Push to GitHub" button skips them. It includes `data_snapshot.json` only when the
  local `generated_at` is newer than GitHub's.
- Dashboard AI actions push only the tickers they changed, via
  `push_json_entry_changes` (it reads the file from `main` and edits only those keys).

**2. Generated data** — written by workflows and committed to the data repo, not by hand:
`data_snapshot.json` (prices + indicators), `expert_views.json`, `fundamentals.json`,
`news_summary.json`, `market_breadth.json`, `dashboard_perf.json`, and the two alert state
files `alert_state.json` (daily edge-trigger dedup) and `weekly_wrapup_state.json`. Both
state files are committed rather than cached: losing `alert_state.json` used to re-fire every
currently-true alert at once. `alert_check.py` now seeds a missing file without sending.

**3. Local only** — gitignored, never pushed: `auth_config.json`, `discord_config.json`.

Why it's built this way: on Streamlit Community Cloud the filesystem is ephemeral, so a
config edited in the UI only survives if it's committed to the data repo. Hence
`push_all_config`, which writes **one atomic commit**. That used to be about racing the
auto-redeploy a code-repo commit triggers; data commits no longer redeploy anything, but a
multi-file change still must not be half-applied.

---

## Execution model

`app.py` is one long script, re-executed top to bottom on every interaction. Module-level
spine, in order:

1. Auth gate.
2. **Data load** — `data_snapshot.json` if fresh, else a live `fetch_all_markets`.
   Produces `per_market = {market_key: [row dicts]}`.
3. **Enrichment loop** over `per_market` — custom columns, notes/flags, then
   `interested`, `sentiment`, `expert_take` attached to every row.
4. **`st.tabs(...)`** with `key="main_tabs"`.
5. **Sidebar** — column picker, sort control, category order, glossary, custom columns,
   ticker notes.
6. **Tab bodies** — combined tabs, then market tabs, then News, then Alert Rules.
   Note this is *execution* order and deliberately does **not** match display order
   (watchlists first, roll-ups after). Content lands in whichever container `with <tab>:`
   names, so the two are independent — the tab strip's order is set solely by the list
   passed to `st.tabs`.

Two things you cannot guess and will get wrong:

- **Every tab body executes on every run**, not just the visible one. Anything expensive
  in a tab body costs you 11× (7 watchlists + 2 combined tabs + News + Alert Rules —
  recount against `markets.json` and the `st.tabs(...)` call in `app.py` if that drifts).
- **`st.tabs` is instantiated early on purpose.** A keyed widget's `session_state` only
  survives a rerun if the widget was re-instantiated on the run before it — so any widget
  that calls `st.rerun()` *before* `st.tabs` would orphan the tab selection and bounce you
  back to the first tab. Don't move it down.

Tabs are registry-driven (`markets.json`), currently 7 watchlists shown in that file's key
order — reorder the tabs by reordering the JSON, not by hardcoding a list — followed by two
**combined tabs** (`all_invested`, `all_watchlist`) whose membership lives in
`watchlist_groups.json`.
Combined tabs use synthetic market keys for widget namespacing and reuse their members'
row dicts.

---

## The row-dict contract

**Most bugs in this repo have come from getting this wrong.** A "row" is a plain dict per
ticker, and *when* a field lands on it decides what you can do with it.

| Stage | Fields | Usable for |
|---|---|---|
| From the snapshot | prices, all indicators, `index_name`, `company_name`, `data_end`, `reported_qtr`, valuation metrics (`trailing_pe`, `roce`, …), `trend`, `volume_trend`, `tech_uptrend`, `flag`, `note` | filter, sort, display |
| Attached in the enrichment loop (module level, before tabs **and** before the sidebar) | custom columns, notes/flags, `interested`, `sentiment`, `expert_take` | filter, sort, display |
| Built inside `render_market_tab`, **after** filtering | `matched_alerts`, the `fundamentals` display cell, `tech_uptrend_label` | display only |

**The rule: if you want a field filterable or sortable, attach it in the enrichment loop.**
Not in the render path.

(`flag` and `note` appear in both of the first two rows on purpose: they get persisted
into the snapshot, but are also re-applied live every run so a note or flag you just saved
shows up immediately instead of waiting for the next refresh.)

Both `Sentiment` and `Tech Uptrend` were once display-only for exactly this reason. The fix
in each case was to attach the raw value up front and keep the fancy HTML cell separate —
note that the sortable field (`sentiment`, `tech_uptrend`) and the column key
(`fundamentals`, `tech_uptrend_label`) are deliberately different, bridged by
`_sort_label_to_field`.

---

## If you add a column, touch all of these

Skipping any of these is silent — the column renders fine and simply isn't available
somewhere. 14 columns were unfilterable for months this way.
The reverse gap is now caught: `checks/test_review_091926.py` (T5) fails when a
filterable metric has no `build_column_defs` entry. Two alertable metrics, one of
them driving a live nightly rule, had no column until 2026-09-22.

1. **`build_column_defs`** (`app.py`) — registers the column and its label.
2. **`get_filterable_metrics`** (`stock_data.py`) — makes it usable in custom filters and
   alert rules. Labels must match `build_column_defs` **character for character**; the
   glossary and condition-builder captions resolve through them.
3. **`CATEGORICAL_METRICS`** (`filters.py`) — only if it has a fixed value set. This also
   makes it rank-sortable (below) and gives it a dropdown instead of a typed value.
   **`TEXT_METRICS`** (same file) — if its value is a string. That keeps it out of the
   Metric B picker. If you miss it the column is still offered as Metric B but never
   matches; the engine fails closed, it doesn't crash.
4. **`_sort_label_to_field`** (`app.py`) — only when the sortable field differs from the
   column key.
5. **`column_definitions`** (`app.py`) — the glossary entry and header tooltip.
6. **`SYNCABLE_FILES`** (`github_sync.py`) — only if it introduces a new JSON file, and see
   the atomic-push trap below.

Deliberately *not* filterable: `matched_alerts`, which is derived from rules evaluated
against already-filtered rows, so filtering on it would be circular.

---

## Sorting

Per-watchlist, up to 6 levels, stored in `column_prefs.json` as
`sort_by_<market>_<n>` / `sort_dir_<market>_<n>`. Direction is **always** stored as the
canonical `↑`/`↓`; the friendlier labels (`A-Z`, `Old→New`, `Top→Bottom`) are display-only
via `format_func`, so saved prefs never need migrating.

Categorical columns sort by a **rank order**, not alphabetically — alphabetical is
meaningless for these (Trend would run Downtrend, Strong Downtrend, Strong Uptrend,
Uptrend). Defaults come from `CATEGORICAL_METRICS`' declaration order, overridden by
`CATEGORY_ORDER_DEFAULTS` where that order isn't quality-ranked, and by the user's dragged
order in the sidebar. Every categorical is declared **best-first**, so `Top→Bottom` means
the same thing on every column — preserve that when adding one.

---

## Traps

Each of these has actually bitten this codebase.

- **A name used but never imported crashes only when that line runs.** `app.py` used
  `save_fundamentals` without importing it, so every re-analyze button raised NameError
  after the model calls finished, and no check noticed because none clicks a button that
  calls Gemini. `checks/test_review_091926.py` (T0) now runs pyflakes over every module
  and fails on any undefined name.
- **Nothing in `app.py` may load a data file before the `bootstrap_data_files` block.**
  The loaders create an empty default for a missing file. On a fresh container, the next
  save would then push those blanks over the real data. The block stops the app instead.
- **This repo and its Actions logs are public.**
  - A new workflow step that runs Python must use `shell: bash` and pipe through
    `2>&1 | python -u log_redact.py`, which masks tickers, company names and watchlist
    names.
  - A script that lists members of a *public* index prints counts, not symbols. Masking
    only the held symbols among them would reveal which are held.
  - Don't put real tickers in comments, docs or commit messages; use neutral examples.

- **A corrupt user-data file is loud, not empty.** `stock_data.read_json_strict` raises
  `DataFileError` for `watchlist.json`, `markets.json`, `settings.json`, `interested.json`,
  `watchlist_groups.json`, `ticker_index.json`, `alerts_config.json`, `custom_filters.json`,
  `custom_columns.json`, `ticker_notes.json` -- an empty default is indistinguishable from
  real emptiness, and the next save (or "Push to GitHub") would write it over the data repo.
  `app.py` reads them all once in a preflight block and stops with the filename. Regenerable
  state (`alert_state.json`, `weekly_wrapup_state.json`) is the opposite: it falls back to
  empty on purpose. **Every writer must go through `atomic_write_json`** -- a torn file is
  what a crash mid-write leaves behind.
- **`passes_filter` fails closed, it never raises.** A condition missing `metric_a` /
  `operator` / `compare_type`, or carrying an operator this engine does not implement,
  returns False. There is no `try` above it (`passes_filter_chain`, `compute_rule_truth`,
  `evaluate_and_fire`), so a raise there takes down every tab AND the nightly job. Note
  `"in"` is implemented inline and is NOT a key in `OPERATORS` -- check against
  `VALID_OPERATORS`, or you silently reject every categorical condition.
- **A job that JUDGES prices needs the whole universe.** `fetch_all_markets` drops a
  benchmark group whose fetch raised (it reports them via `skipped_groups=`) and any ticker
  that came back empty. `refresh_data.py` absorbs that with `reject_stale_rows` /
  `fill_snapshot_gaps`; `alert_check.py` and `weekly_wrapup_check.py` cannot, so they fail
  the run (`MAX_MISSING_FRACTION`) and let the slot gate retry the slot. A handful of
  individual misses only warns -- failing on those would let one delisted ticker block
  alerts every slot forever.
- **Streamlit strips `<style>` and `<script>` from markdown**, even with
  `unsafe_allow_html=True`. Inline `style="..."` *attributes* survive. That's why all
  sticky-table CSS is regex-injected onto each tag in `sticky_header_html` (`app.py`).
  A `<style>` block will silently do nothing.
- **`push_all_config` is atomic and fails the entire push if any `SYNCABLE_FILES` entry is
  missing on disk.** A new config file must be created eagerly on first render, never
  lazily on first write.
- **`load_*()` helpers are uncached file reads.** `load_expert_views()` was once called
  once per row inside the filter loop — an 89 KB parse ~760 times per render. Hoist them.
- **`session_state` is seeded only when a widget key is absent.** Any code that rewrites
  prefs behind a live widget must `pop` that widget's keys, or the widget re-renders its
  old value and writes it straight back over your change. This is why the copy-sort
  feature pops `sort_field_*`/`sort_dir_*`.
- **Widget keys must be namespaced per market** (`f"...{market}..."`), and per section
  where a control appears twice. A duplicate key raises and takes down the whole app.
- **Combined tabs must de-duplicate by ticker.** They concatenate member watchlists, and a
  ticker in two of them rendered twice — colliding on a per-ticker widget key and killing
  every tab, not just that one.
- **A `workflow_dispatch` input must be on `main` before it can be dispatched.** GitHub
  reads the input definition from the branch, so dispatching before pushing fails with
  "unexpected input".
- **Don't rename the watchlist keys.** `us_invested` / `india_invested` / `all_invested`
  are registry keys and group keys, unrelated to the `interested` flag.

---

## GitHub Actions

| Workflow | Runs | Commits (to the data repo) | Slot (ET) | Stale after |
|---|---|---|---|---|
| `data-refresh.yml` | `refresh_data.py` | `data_snapshot.json` | every hour, no gate | n/a |
| `news-summary.yml` | `news_check.py` | `news_summary.json` | 8:00 PM | 22 h |
| `fundamentals.yml` | `refresh_fundamentals.py` | `fundamentals.json` | 9:00 PM | 22 h |
| `daily-alerts.yml` | `alert_check.py` | `alert_state.json` | 9:00 PM | 22 h |
| `expert-views.yml` | `refresh_data.py`, `refresh_expert_views.py` | `data_snapshot.json`, `expert_views.json` | 1:00 AM | 22 h |
| `market-breadth.yml` | `refresh_market_breadth.py`, `refresh_dashboard_perf.py` | `market_breadth.json`, `dashboard_perf.json` | 10:00 AM + 10:00 PM | 10 h |
| `weekly-wrapup.yml` | `weekly_wrapup_check.py` | `weekly_wrapup_state.json` | Sunday 9:00 PM | 22 h |

`checks.yml` is not in this table: it runs `checks/run_all.py` on every push, touches no
data and needs no secrets (so it works from a fork). Keep it that way — `test_workflows.py`
asserts it.

**Every workflow wakes hourly (`cron: "0 * * * *"`) and a `gate` job decides whether this
run does the work** -- `.github/actions/slot-gate`, inputs `slots` (ET hours), `grace-hours`,
optional `days`, and `work-job`. It finds the most recent slot in New York time, skips it
when it is older than `grace-hours`, and otherwise asks the GitHub API whether an earlier
run of the same workflow has already done it. `data-refresh.yml` needs no gate: every hour
is a real run.

Three things about it you cannot guess:

- **"Done" means the WORK JOB succeeded**, not the run. A run whose gate said "no" also
  reports success, and counting those would skip the slot forever. Hence `work-job`, and
  hence the gate needs `actions: read`.
- **`grace-hours` must stay below the gap between slots.** Breadth has two slots 12 h
  apart, so it keeps 10 h; the others have one slot a day and use 22 h.
- **`days` filters the SLOT's weekday, not the run's.** The Sunday wrap-up slot is still
  Sunday's when the run starts on Monday morning.

**An "hourly" cron is not hourly.** GitHub throttles it to 4-6 runs a day, 2-5 hours
apart (measured on `data-refresh.yml`, 2026-09-07..10). That is still several chances per
slot, which is the point, but it means a slot's work can start hours after the slot -- so
`grace-hours` has to be generous (22 h, or 10 h where slots are 12 h apart) and
`SNAPSHOT_STALE_WARN_HOURS = 6` sits close to the real gap between data refreshes.

This replaced a cron pair per workflow (one line per DST season) plus a gate that rejected
the line belonging to the other season. It only worked while GitHub fired the right line:
on 2026-09-14 it fired only the EST line for the 9:15 PM ET alert slot, the gate rejected
it, and that day's alerts never went out. **GitHub starts scheduled runs hours late**
(observed ~4-5 h) **and drops lines outright**, so no single cron can be load-bearing.

A late evening job lands inside NSE's ~23:45-06:00 ET session and gets a forming India
bar. Jobs that judge closes pass `completed_sessions_only=True` to `fetch_all_markets`
(`alert_check.py`, `weekly_wrapup_check.py`), which drops each ticker's unfinished bar
by its own exchange. The dashboard and `refresh_data.py` leave it off to show live prices.

`alerts.ALLOWED_HOURS` must match the alert gate's `slots`, or the app's schedule picker
would offer an hour nothing wakes up for. `daily-alerts.yml`'s gate also asks
`is_rule_due()` whether any rule is due for that slot's day before starting the heavy job.

`expert-views.yml` and `fundamentals.yml` accept a **`markets`** input (comma-separated
market keys) which the app's per-tab "Re-analyze All" button uses to scope a run to one
watchlist. The scripts read it back as `REFRESH_MARKETS`; blank means all watchlists,
which is what every scheduled run gets.

For smoke tests, those two workflows plus `news-summary.yml` also accept **`limit`**
(`REFRESH_LIMIT`): analyse only the first N tickers in scope. `news-summary.yml` also takes
`markets`. A news run with either input is partial: it replaces only those watchlists'
digests and posts nothing to Discord.

Pass a number, never ticker names. This repo's Actions logs are public, and they print
each step's env before `log_redact.py` sees any output.

---

## Verifying a change

No test framework. Render the app headlessly and assert against the output:

```python
from streamlit.testing.v1 import AppTest

at = AppTest.from_file("app.py", default_timeout=300)
at.session_state["authenticated"] = True          # bypass the login gate
at.session_state["main_tabs"] = "India Watchlist" # optional: pick the active tab
at.run()

assert not at.exception, [str(e.value) for e in at.exception]
```

Useful handles: `at.sidebar.selectbox(key=...)`, `at.button(key=...).click().run()`,
`at.multiselect(key=...).set_value([...]).run()`, and the rendered tables, which are HTML
inside `at.markdown` blocks (search for `"<table"`) since the tables are built as raw HTML,
not `st.dataframe`.

Set **`SKIP_GITHUB_PULL=1`** in the environment for local and `AppTest` runs. It skips
both the startup download and `pull_generated_files`, so the run uses the JSON already in
your folder and doesn't overwrite it with the data repo's copies.

Two things that will waste your time otherwise:

- **`AppTest` runs write to `column_prefs.json`** (the sort control persists on render).
  Back it up before, restore it after, or you'll commit test state.
- **`apply_sort` lower-cases string keys.** An independently computed "expected" ordering
  that doesn't do the same will disagree with a correct implementation. When your check
  disagrees with the app, suspect the check first.

Prefer comparing against an **independently computed** expectation over asserting the code
agrees with itself.

When you add a check, put it in `checks/` if it can run offline with invented fixtures
(`ACME`, `ZED.NS`) — that is what CI can protect. Anything that reads the real snapshot or
hits the network belongs in `docs/private/checks/`, which is gitignored.

---

## Conventions

- Comments here explain **why**, often at length, and that is deliberate. When you change
  something a comment describes, update the comment. When you fix a non-obvious bug, leave
  a comment saying what the failure mode was — most of the long comments in this codebase
  are exactly that, and they're why the traps above are known.
- ASCII `--` rather than em dashes inside code comments.
- Match the surrounding style; don't reformat code you aren't changing.
