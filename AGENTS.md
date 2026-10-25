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
missing. A key that hits a per-minute limit sits out 120 s; one that spends its DAILY quota
for a model (Google's `...PerDay...` quota id) is retired for that model for the rest of the
run, and once every key is out the ladder steps to its fallback model at once. Before
2026-10-23 a daily-exhausted key rejoined every 120 s and failed again.

**There is no test framework, but there are checks.** `python3 checks/run_all.py` runs 1600
offline regression checks in ~60 s (no secrets, no network, no data files), and
`.github/workflows/checks.yml` runs them on every push. Anything needing real prices, the
private data repo or a live API is run by hand — see `checks/README.md`. Changes are also
verified by rendering the app headlessly; recipe at the bottom.

---

## Module map

~21.2k lines total (the app itself; checks/ adds ~8.7k). The weighting matters: `app.py` is over a third of it.

| File | Lines | What it owns |
|---|---:|---|
| `app.py` | 6596 | The entire UI: tabs, tables, sidebar, filters, sort, editors, AI control bars, News + Alert Rules tabs |
| `stock_data.py` | 3347 | yfinance fetching, all indicator maths (TradingView-exact RSI/ATR/ADX/VStop, held to bar-by-bar Pine ports by `checks/test_tv_parity.py`), watchlist/markets registry IO, `get_filterable_metrics` |
| `alerts.py` | 1127 | Alert rule evaluation + Discord message building; every Discord post goes through `send_discord_batch`, which appends the disclaimer |
| `news_summary.py` | 1022 | The daily news digest (6 AM ET): search and filter (3 stocks at a time, `NEWS_CONCURRENT_TICKERS`), then edit. Dated by its 6 AM ET slot (`news_slot_date`), funds skipped (`is_fund`), the code sets each company header and drops filler lines, the filter drops repeats of the previous digest, and a failed editor falls back to code-formatted bullets |
| `fundamentals_eval.py` | 1191 | Sentiment ("fundamental view") generation + validation. The model only extracts facts; `score_sentiment` decides the label, weighted toward forward signals (guidance ±15, quoted outlook ±6, guidance vs consensus ±3, named-firm action ±3; the quarter's profit >15% YoY or a beat/miss ±1 only breaks ties; with no forward signal, profit >+25% / <-20% YoY decides alone). The search window is anchored to each company's last results (`search_window_for`) |
| `github_sync.py` | 715 | Atomic config push, the end-of-run push of edited config (`unpushed_config_files`) + `workflow_dispatch` trigger |
| `expert_views.py` | 936 | Expert Take. The verdict is decided in code, live, from Trend, Tech Uptrend, TA Rules and Sentiment (`decide_expert_verdict`, `expert_take_for_row`); the nightly model writes the explanation and trade plan, and may lower the verdict one step for a dated material negative event in the last 14 days (`news_risk_active`). Its prompt also gets Sentiment's checked facts for the quarter (section 4) |
| `filters.py` | 485 | The boolean condition engine — shared by UI filters **and** background alerts |
| `breadth_charts.py` | 222 | Plotly building blocks for the News tab's breadth / highs-lows / return panels (both views), and the phone behaviour (`is_mobile`: stacked panels, no zoom or drag) |
| `breadth_math.py` | 167 | Breadth maths (% above the 200-day and 50-day SMAs, 52-week highs/lows, per-day denominators), each over a stock's OWN sessions, so a mixed India + US group counts right — shared by the index-breadth job **and** the app's per-watchlist breadth view; `assemble_closes` builds a watchlist's panel from `dashboard_perf.json` |
| `llm_util.py` | 716 | Shared Gemini-call plumbing (timeout wrapper, retry/model-ladder logic, `FailureFuse`, key rotation) for the three AI pipelines |
| `weekly_wrapup.py` | 365 | Weekly Discord digest |
| `custom_columns.py` | 294 | User-defined formula columns |
| `ticker_notes.py` | 117 | Per-ticker notes and manual flags. (`signal` was retired on 2026-10-04; the ticker dot is Expert Take's colour) |
| `stage_analysis.py` | 243 | TheWrap Stage Analysis, uploaded not computed: the sidebar's "Stage Analysis upload" takes the indicator's CSV export (`Ticker, Exchange, Stage, MOM, CE, PEAD, ...`) into `stage_analysis.json` and the Stage / Stage Mom / CE / PEAD columns (plus Prev Stage, Stage Conf, Stage Weeks, hidden). The `Stage` cell `S1 - S2 (Developing 8P)` reads previous - current (confidence, weeks; every value is the latest FULL CLOSED week, so upload weekly): on every row it agrees with Trend, and `S? - S?` is Unknown. The export has bare symbols, so rows are matched to the tickers the watchlists hold through its Exchange column (NSE <-> `.NS`, BSE <-> `.BO`, else no suffix); the preview names what matched nothing. An upload MERGES (tickers in the file are replaced, the rest kept, each dated). Stdlib-only (`json_store`): `filters.py` imports its value lists, so the alert gate must be able to import it. `COLUMN_DEFS` is the one list `build_column_defs` and `get_filterable_metrics` both read |
| `market_api.py` | 319 | What the screener shares with other apps (the research library's Q&A, Claude chat): the metric catalog by group, the public part of a row (a whitelist: never notes, flags, Interested or custom columns; which tabs hold a ticker is published separately, by `publish_market.py watchlists`) and `compute_rows`, a live `fetch_snapshot` for any ticker. Served live on Vercel through `market_lookup.py` (`vercel.json`, its own slim `api/requirements.txt`) |
| `market_lists.py` | 260 | The screener's lists for Claude chat's connector (`api/mcp.py`'s `list_watchlists`, `get_watchlist`, `get_market_breadth`, `get_price_history`), read from what `publish_market.py watchlists` stores: a tab as a table (a row per stock with the groups asked for, sortable, a summary of each return over the whole list), an index's or a tab's breadth day by day, daily closes (a ticker not stored is fetched from Yahoo, its still-trading bar dropped). Loose names (`find_watchlist`: "my India holdings"; an ambiguous one answers with the choices); a big reply is thinned to every Nth session |
| `market_lookup.py` | 504 | One lookup for every live caller: a company name or ticker -> a ticker (a suffix as typed, a bare six-digit BSE code, a known symbol, the research library's name decisions and listings by its `match_key`, the clearly closest listing, then Yahoo's symbol search) -> the stored row while fresh, else computed with the screener's saved settings and stored. **Yahoo prices BSE stocks as `SYMBOL.BO`, never the `<scrip code>.BO` the library's BSE listings carry**: `compute_priced` prices those under the symbol Yahoo's search gives for the ISIN (remembered in `kg_market_meta.yahoo_symbols`) and keeps the code as the row's key, and tries an NSE listing too new for the indicators on BSE. A stored row is recomputed after `STALE_HOURS` (20), or after an hour when its prices end before its exchange's last completed session (`last_completed_session`, `BEHIND_RECHECK_MINUTES`). A ticker whose Yahoo company details failed (named by its bare symbol) is computed once more after 5 s; what is still blank is filled from the stored row (`keep_previous`: the name and info fields only when the details failed, statement figures whenever blank), in the live lookup and both publish modes. The library job uses it too. All database access goes through `MarketDB`, which checks replace with a fake |
| `market_http.py` | 65 | What the two Vercel functions share: the token check (bearer, `?token=`, or the `/mcp/<token>` path a Claude connector URL carries; fails closed -- no `MARKET_API_TOKEN`, no access), `api/mcp.py`'s batch cap (`MAX_BATCH`) and capturing Yahoo's printed and logged messages. `api/market.py` is REST for the transcription app; `api/mcp.py` is a stateless MCP server (JSON-RPC over POST) for Claude chat |
| `glossary.py` | 394 | The glossary (`column_definitions`, each column's header tooltip) and the per-signal "why" text (`trend_tooltip`, `tech_uptrend_tooltip`, `ta_rules_tooltip`, `vol_trend_tooltip`). Pure functions, no Streamlit: `app.py` imports them, and `market_api` publishes the same words as metric definitions and its `explain` group, so the screen and the research library never explain a number differently |
| `refresh_schedule.py` | 58 | When the data is refreshed (the Cloudflare Worker's window, as one rule): the sidebar's stale-data warning counts only scheduled hours (`refresh_hours_since`). `checks/test_refresh_schedule.py` holds it to the breadth gate's real decisions |
| `watchlist_labels.py` | 46 | Tab labels a watchlist may not take. Kept out of `stock_data.py`, whose code fingerprint (comments and docstrings excluded) marks the snapshot stale on any code edit |

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
**Every edit is pushed automatically** at the end of the run that made it: the last block
of `app.py` pushes whatever `github_sync.unpushed_config_files()` reports (local bytes no
longer match the blob this container last pulled or pushed) as one commit, and retries a
failed push on the next run. A file with no pull/push record is never auto-pushed -- a
stale copy must not overwrite the repo. The "Push to GitHub" button is the manual fallback.
The authoritative list is `SYNCABLE_FILES` in `github_sync.py`:
`watchlist.json`, `markets.json`, `interested.json`, `custom_filters.json`,
`settings.json`, `alerts_config.json`, `column_prefs.json`, `custom_columns.json`,
`ticker_notes.json`, `stage_analysis.json`, `expert_views.json`, `fundamentals.json`,
`ticker_index.json`, `data_snapshot.json`, `watchlist_groups.json`.

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

- **Every tab body executes on every run**, not just the visible one -- except News, which
  is gated on `tab_news.open` because nothing else reads it. Anything expensive
  in a tab body costs you 11× (7 watchlists + 2 combined tabs + News + Alert Rules —
  recount against `markets.json` and the `st.tabs(...)` call in `app.py` if that drifts).
  **On a phone** (`IS_MOBILE`, from the User-Agent via `breadth_charts.is_mobile`) a
  hidden watchlist tab skips its table, AI-review box and Expert Views cards
  (`render_market_tab(..., hidden_on_phone=True)`), and the Expert Views expander
  builds its cards only while open. That took a phone's first load from ~5.4 MB to
  ~0.4 MB of HTML (7 x 40 synthetic tickers). The tab's widgets still run, and the
  keys of the skipped ones are re-assigned so they survive. Desktop is untouched;
  `checks/test_phone_render.py` renders both.
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
| Attached in the enrichment loop (module level, before tabs **and** before the sidebar) | custom columns, notes/flags, `interested`, `sentiment`, `expert_take` (live, from the columns), the Stage Analysis fields (`stage`, `stage_mom`, `cheat_entry`, `pead`, ...; from `stage_analysis.json`) | filter, sort, display |
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
5. **`column_definitions`** (`glossary.py`) — the glossary entry and header tooltip. It is also the definition the research library's Q&A and Claude chat get for the metric (`market_api.catalog`); a column they should see needs a `market_api.METRIC_GROUPS` entry too.
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

- **Yahoo publishes some days' daily bars hours late.** On 2026-10-22 most NSE/BSE
  stocks' 22 Oct row held only Volume until the next morning, while the quote and the
  5-minute bars had the session; every consumer drops a row without a close, so the
  dashboard, the alerts and the research library all read 21 Oct.
  `fill_unpublished_daily_bars` (in `fetch_snapshot`, after `drop_forming_daily_bars`)
  rebuilds such a trailing bar from the 5-minute bars once its session has settled, and
  `official_close` takes its close from the quote (NSE/BSE's official close is a half-hour
  average, not the last trade). Anything else -- no bars, a failed download, a blank older
  than `UNPUBLISHED_BAR_MAX_DAYS` -- leaves the row as it was.
- **A corrupt user-data file is loud, not empty.** `stock_data.read_json_strict` raises
  `DataFileError` for `watchlist.json`, `markets.json`, `settings.json`, `interested.json`,
  `watchlist_groups.json`, `ticker_index.json`, `alerts_config.json`, `custom_filters.json`,
  `custom_columns.json`, `ticker_notes.json`, `stage_analysis.json` -- an empty default is indistinguishable from
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
  feature pops `sort_field_*`/`sort_dir_*`. **The same goes for one setting rendered by
  several widgets** -- the two "Show fundamental columns" boxes, and the AI model and
  budget pickers drawn on every market tab: each copy saved its stale value back, so none
  of them stuck until 2026-10-03. Save such a setting through `_save_setting_reset_widgets`
  (or `_set_show_fundamentals`), which drops every copy's state before rerunning.
- **Widget keys must be namespaced per market** (`f"...{market}..."`), and per section
  where a control appears twice. A duplicate key raises and takes down the whole app.
- **Combined tabs must de-duplicate by ticker.** They concatenate member watchlists, and a
  ticker in two of them rendered twice — colliding on a per-ticker widget key and killing
  every tab, not just that one.
- **A `workflow_dispatch` input must be on `main` before it can be dispatched.** GitHub
  reads the input definition from the branch, so dispatching before pushing fails with
  "unexpected input".
- **Expert Take must never see its own previous verdict.** Since 2026-10-04 the columns
  decide the verdict, so this now guards the write-up and the news step. It used to, twice over: the
  prompt showed the row's flag as a "user flag" while the flag was an auto-vote that counted
  the last verdict, and it listed alert rules that fire on that flag. Now Flag is set only by
  hand, the prompt shows a flag only when `flag_reason == MANUAL_FLAG_REASON`, and
  `alerts.PRIOR_VERDICT_METRICS` keeps rules on `expert_take`/`expert_news_backed` out of
  it. A new field derived from the verdict belongs in that set.
- **Post to Discord only through `alerts.send_discord_batch`.** It appends the disclaimer
  footer (`with_disclaimer`) to every batch; a direct webhook post would skip it.
- **Don't rename the watchlist keys.** `us_invested` / `india_invested` / `all_invested`
  are registry keys and group keys, unrelated to the `interested` flag.

---

## GitHub Actions

| Workflow | Runs | Commits (to the data repo) | Slot (ET) | Stale after |
|---|---|---|---|---|
| `data-refresh.yml` | `refresh_data.py`, then `publish_market.py screener` and (first run each hour) `publish_market.py library --min-gap 50` | `data_snapshot.json` | every 15 minutes, no gate (GitHub throttles it far below that) | n/a |
| `news-summary.yml` | `news_check.py` | `news_summary.json` | 6:00 AM | 23 h |
| `fundamentals.yml` | `refresh_fundamentals.py` | `fundamentals.json` | 9:00 PM | 22 h |
| `daily-alerts.yml` | `alert_check.py` | `alert_state.json` | 9:00 PM | 22 h |
| `expert-views.yml` | `refresh_data.py`, `refresh_expert_views.py` | `data_snapshot.json`, `expert_views.json` | 1:00 AM | 22 h |
| `market-breadth.yml` | `refresh_market_breadth.py` (S&P 500, Nifty 500, Nasdaq 100), `refresh_dashboard_perf.py`, `refresh_data.py`, then `publish_market.py watchlists` | `market_breadth.json`, `dashboard_perf.json`, `data_snapshot.json` | Every hour from Sunday 10 PM ET to Friday 5 PM ET, none 6-9 PM ET (the gate's `window` plus a slot list without hours 18-21; no Saturday slot) (a market with no new session is skipped; a mid-session run stores today's forming bar as "live", redone next run; a latest day Yahoo has only part-published is "partial"/"awaiting" and fails the run, so the gate retries) | 1 h (the next hourly slot takes over) |
| `weekly-wrapup.yml` | `weekly_wrapup_check.py` | `weekly_wrapup_state.json` | Sunday 9:00 PM | 22 h |
| `market-library.yml` | `publish_market.py library` (`--min-gap 50` on its schedule) | nothing (rows in the research library's database) | 7:00 AM, 5:00 PM (the fallback; data-refresh publishes the library hourly) | 9 h |

**The research library's copy.** `data-refresh.yml` ends with `publish_market.py screener` (after its commit, `continue-on-error`): the snapshot's tickers and their AI write-ups go into the transcription app's knowledge database (Supabase), where its Q&A and Claude chat read them. The companies that library discusses and no watchlist tracks are computed hourly by the same workflow's last step (`publish_market.py library --min-gap 50`: the first run in an hour publishes, every later one sees the database's own timestamp and skips), and at 7 AM / 5 PM ET by `market-library.yml`, the fallback for when the Worker is down or quiet (it skips too when the hourly step just ran; a run by hand always publishes). Both write as the `market_writer` role (`MARKET_DB_URL` secret; unset = skipped), which can touch only the `kg_market*` tables there, and print counts only: library tickers are not in `log_redact.py`'s lists, so `publish_market.py` swallows the computation's per-ticker lines. What may be published in a row is `market_api.py`'s whitelist -- never notes, flags, Interested or custom columns. **The tabs themselves** (owner, 2026-10-23: holdings included, so a chat can answer "how did my India watchlist do this week?") go in from `market-breadth.yml`, after its commit and before its verify step (`publish_market.py watchlists`, `continue-on-error`): each tab and combined group with its stocks (`kg_watchlists`, `kg_watchlist_members`, replaced whole), breadth per index and per tab (`kg_breadth_series`, `kg_breadth`; a tab's by `breadth_math`, the app's Watchlist Breadth numbers) and two years of daily closes of every tab stock and benchmark (`kg_prices`). A stored series gets only its last `REWRITE_DAYS` (10) rewritten each run, a new or short one in full; an empty set of tabs (data files not loaded) publishes nothing. `test_workflows.py` exempts `market-library.yml` (`DB_ONLY`) from commit-data and pins its secrets.

`checks.yml` is not in this table: it runs `checks/run_all.py` on every push, touches no
data and needs no secrets (so it works from a fork). Keep it that way — `test_workflows.py`
asserts it.

`keep-alive.yml` is not in the table either: every 4 hours (`17 */4 * * *`, no gate) it runs
`keepalive.py`, which opens the app in headless Chromium so Streamlit Community Cloud does not
put it to sleep after ~12 h without a visitor (a plain HTTP ping does not count; the timer
follows the websocket). It clicks the wake-up button if the app already slept, and needs one
secret, `APP_URL` (a secret because this repo and its logs are public). It loads and commits no
data and does not log in -- the login form already counts as a visit -- so `test_workflows.py`
exempts it from the load-data / commit-data rules and asserts it uses only that secret.

**Every workflow wakes hourly (`cron: "0 * * * *"`) and a `gate` job decides whether this
run does the work** (`data-refresh.yml` every 15 minutes) -- `.github/actions/slot-gate`, inputs `slots` (ET hours), `grace-hours`,
optional `days`, and `work-job`. It finds the most recent slot in New York time, skips it
when it is older than `grace-hours`, and otherwise asks the GitHub API whether an earlier
run of the same workflow has already done it. `data-refresh.yml` needs no gate: every hour
is a real run.

Three things about it you cannot guess:

- **"Done" means the WORK JOB succeeded**, not the run. A run whose gate said "no" also
  reports success, and counting those would skip the slot forever. Hence `work-job`, and
  hence the gate needs `actions: read`. A **partial** manual run (`markets` or `limit`
  given) is titled "partial run" by the workflow's `run-name` and never counts -- one tab's
  "Re-analyze All" must not stand in for the nightly full run. Runs are listed without
  `?status=completed`, a filter that lagged and let the alerts slot be worked twice.
- **A history the gate cannot read is a RED gate, never a guess.** Each API call is
  retried 3 times; then the gate exits 1 and the next wake-up asks again. It used to treat
  an unreadable run as not done -- one 502 on 2026-10-13 re-ran the alerts slot (run #254),
  re-posting every "full"-mode rule to Discord. A `workflow_dispatch` run is not gated at
  all (every work job runs on one anyway), so an API blip never blocks the Worker's runs.
- **The gate always works the LATEST slot**, so a `grace-hours` longer than the gap
  between slots never lets a run do an older one -- the window just ends when the next
  slot starts. Breadth's hourly slots therefore use a 1 h grace: a run only ever works
  the hour it lands in, and the next run refreshes every market with a new session. For
  a one-slot-a-day workflow, keep the window under 24 h (news uses 23, the rest 22).
- **`days` filters the SLOT's weekday, not the run's.** The Sunday wrap-up slot is still
  Sunday's when the run starts on Monday morning.
- **`window` (`SUN:22-FRI:17` for breadth) limits slots by their start, in ET, both ends
  inclusive.** Hourly slots plus `days` cannot say "Sunday from 22:00", which breadth
  needs. Like `days`, it judges the slot, so a run landing after Friday 17:59 ET finds a
  slot outside the window and does nothing -- Friday 17:00's work is only done by a run
  that starts inside that hour.

**An "hourly" cron is not hourly.** GitHub throttles it to 4-6 runs a day, 2-5 hours
apart (measured on `data-refresh.yml`, 2026-09-07..10). That is still several chances per
slot, which is the point, but it means a slot's work can start hours after the slot -- so
`grace-hours` has to be generous (22-23 h). The data refresh no longer depends on it (the
Worker, below), and `SNAPSHOT_STALE_WARN_HOURS = 6` counts only hours inside the Worker's
schedule (`refresh_schedule.py`), so a weekend or the evening pause does not warn.

**A Cloudflare Worker is the real clock for `data-refresh.yml` and `market-breadth.yml`**
(owner, 2026-10-21; deployed by hand in the Cloudflare dashboard, its code is not in this
repo). Its cron trigger (`0,30 * * * *`) fires at :00 and :30 UTC and it calls `workflow_dispatch` through a
fine-grained token scoped to THIS repo with Actions: write (never the data repo). Only
inside Sunday 22:00 ET to Friday 17:00 ET, and not between 17:30 and 21:30 ET on any day:
`data-refresh.yml` every 30 minutes, `market-breadth.yml` at :00 only. A dispatch skips `market-breadth.yml`'s gate
(`github.event_name == 'workflow_dispatch'`), so the Worker enforces the window itself;
keep it equal to the gate's `window` and `slots`, which are the fallback if the Worker is down. The
GitHub `*/15` cron on `data-refresh.yml` stays as a throttled backup. Every successful
snapshot write is stamped `generated_at = now`, so every run commits to the data repo --
that is why the Worker is limited to the window and not left running around the clock.

This replaced a cron pair per workflow (one line per DST season) plus a gate that rejected
the line belonging to the other season. It only worked while GitHub fired the right line:
on 2026-09-14 it fired only the EST line for the 9:15 PM ET alert slot, the gate rejected
it, and that day's alerts never went out. **GitHub starts scheduled runs hours late**
(observed ~4-5 h) **and drops lines outright**, so no single cron can be load-bearing.

A late evening job lands inside NSE's ~23:45-06:00 ET session and gets a forming India
bar. Jobs that judge closes pass `completed_sessions_only=True` to `fetch_all_markets`
(`alert_check.py`, `weekly_wrapup_check.py`), which drops each ticker's unfinished bar
by its own exchange. The dashboard and `refresh_data.py` leave it off to show live prices.

**The two nightly AI jobs stop early on a run of failures.** `refresh_fundamentals.py` and
`refresh_expert_views.py` use `llm_util.FailureFuse`: after 10 consecutive failed tickers they
skip the rest (those keep their previous views) and exit 1. A red Sentiment or Expert Take
run with `::error::Stopped early` almost always means dead keys or an exhausted quota; the
gate retries the slot, and each retry now costs minutes rather than a 2-hour grind.
Expert Views analyses 3 tickers at a time (`MAX_CONCURRENT_TICKERS`, like Sentiment and the
news digest) -- serial runs had reached 4-6 h against GitHub's 360-minute job limit -- and a
SCHEDULED full run resumes: it skips tickers with a valid view written since tonight's 1 AM
ET slot (`slot_start_utc`, `written_since`), so a retry after the fuse or the 6-hour cap does
not redo finished work. Manual, scoped and limited runs redo everything.

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
your folder and doesn't overwrite it with the data repo's copies, and it turns off the
end-of-run push of edited config. Other writes still reach the data repo when a token is
set -- `app.py` loads `.env`, which holds `DATA_REPO_TOKEN` -- so for a headless render that
clicks anything, also blank `DATA_REPO_TOKEN`, `GITHUB_TOKEN` and `DISCORD_WEBHOOK_URL` in
the environment (`load_dotenv` never overrides a variable that is already set), or render a
copy of the app in a scratch directory with no `.env`.

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
