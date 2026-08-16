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

Secrets come from Streamlit secrets first, then env vars: `GEMINI_API_KEY`,
`NVIDIA_API_KEY`, `GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_BRANCH`, `DISCORD_WEBHOOK_URL`.
See `DEPLOYMENT.md`.

**There is no test suite.** Changes are verified by rendering the app headlessly — recipe
at the bottom.

---

## Module map

~12k lines total. The weighting matters: `app.py` is more than half of it.

| File | Lines | What it owns |
|---|---:|---|
| `app.py` | 5496 | The entire UI: tabs, tables, sidebar, filters, sort, editors, AI control bars, News + Alert Rules tabs |
| `stock_data.py` | 2071 | yfinance fetching, all indicator maths, watchlist/markets registry IO, `get_filterable_metrics` |
| `alerts.py` | 686 | Alert rule evaluation + Discord message building |
| `news_summary.py` | 473 | News gathering + LLM summarisation |
| `fundamentals_eval.py` | 390 | Sentiment ("fundamental view") generation + validation |
| `expert_views.py` | 369 | Expert Take verdict generation |
| `filters.py` | 350 | The boolean condition engine — shared by UI filters **and** background alerts |
| `weekly_wrapup.py` | 349 | Weekly Discord digest |
| `ticker_notes.py` | 230 | Per-ticker notes/flags + auto-flag voting |
| `custom_columns.py` | 229 | User-defined formula columns |
| `github_sync.py` | 222 | Atomic config push + `workflow_dispatch` trigger |

`refresh_*.py` and `*_check.py` are thin entry points that exist only to be run by GitHub
Actions. They contain no logic worth duplicating — they call into the modules above.

`filters.py` being shared is load-bearing: a rule must evaluate identically in the UI
preview and in the nightly Discord job. Don't fork that logic.

---

## JSON files are the database

There is no DB. Everything is JSON in the repo root, in **three classes that must not be
conflated**:

**1. User config** — edited in the UI, pushed to GitHub by `github_sync.push_all_config`.
The authoritative list is `SYNCABLE_FILES` in `github_sync.py`:
`watchlist.json`, `markets.json`, `interested.json`, `custom_filters.json`,
`settings.json`, `alerts_config.json`, `column_prefs.json`, `custom_columns.json`,
`ticker_notes.json`, `expert_views.json`, `fundamentals.json`, `ticker_index.json`,
`data_snapshot.json`, `watchlist_groups.json`.

**2. Generated data** — written and committed by workflows, not by hand:
`data_snapshot.json` (prices + indicators), `expert_views.json`, `fundamentals.json`,
`news_summary.json`, `market_breadth.json`, `dashboard_perf.json`.

**3. Local only** — gitignored, never pushed: `auth_config.json`, `discord_config.json`,
`alert_state.json`.

Why it's built this way: on Streamlit Community Cloud the filesystem is ephemeral, so a
config edited in the UI only survives if it's committed back to the repo. Hence
`push_all_config`, which writes **one atomic commit** — several sequential commits would
race the auto-redeploy that any commit triggers.

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
  in a tab body costs you 7×.
- **`st.tabs` is instantiated early on purpose.** A keyed widget's `session_state` only
  survives a rerun if the widget was re-instantiated on the run before it — so any widget
  that calls `st.rerun()` *before* `st.tabs` would orphan the tab selection and bounce you
  back to the first tab. Don't move it down.

Tabs are registry-driven (`markets.json`), currently 5 watchlists shown in that file's key
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

1. **`build_column_defs`** (`app.py`) — registers the column and its label.
2. **`get_filterable_metrics`** (`stock_data.py`) — makes it usable in custom filters and
   alert rules. Labels must match `build_column_defs` **character for character**; the
   glossary and condition-builder captions resolve through them.
3. **`CATEGORICAL_METRICS`** (`filters.py`) — only if it has a fixed value set. This also
   makes it rank-sortable (below) and gives it a dropdown instead of a typed value.
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

| Workflow | Runs | Commits | Schedule (UTC) |
|---|---|---|---|
| `data-refresh.yml` | `refresh_data.py` | `data_snapshot.json` | every ~2h across the trading day |
| `expert-views.yml` | `refresh_data.py`, `refresh_expert_views.py` | `data_snapshot.json`, `expert_views.json` | 03:00 / 04:00 (11 PM ET) |
| `fundamentals.yml` | `refresh_fundamentals.py` | `fundamentals.json` | 07:00 / 08:00 (3 AM ET) |
| `news-summary.yml` | `news_check.py` | `news_summary.json` | 00:00 / 01:00 (8 PM ET) |
| `daily-alerts.yml` | `alert_check.py` | — (Discord only) | 01:15 / 02:15 (9:15 PM ET) |
| `market-breadth.yml` | `refresh_market_breadth.py`, `refresh_dashboard_perf.py` | `market_breadth.json`, `dashboard_perf.json` | 02,03,14,15 |
| `weekly-wrapup.yml` | `weekly_wrapup_check.py` | `weekly_wrapup_state.json` | Mon 01:00 / 02:00 |

Two crons per workflow is the EDT/EST pair; a gate job checks the real ET hour and skips
the wrong one, so a run isn't double-fired.

`expert-views.yml` and `fundamentals.yml` accept a **`markets`** input (comma-separated
market keys) which the app's per-tab "Re-analyze All" button uses to scope a run to one
watchlist. The scripts read it back as `REFRESH_MARKETS`; blank means all watchlists,
which is what every scheduled run gets.

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

Two things that will waste your time otherwise:

- **`AppTest` runs write to `column_prefs.json`** (the sort control persists on render).
  Back it up before, restore it after, or you'll commit test state.
- **`apply_sort` lower-cases string keys.** An independently computed "expected" ordering
  that doesn't do the same will disagree with a correct implementation. When your check
  disagrees with the app, suspect the check first.

Prefer comparing against an **independently computed** expectation over asserting the code
agrees with itself.

---

## Conventions

- Comments here explain **why**, often at length, and that is deliberate. When you change
  something a comment describes, update the comment. When you fix a non-obvious bug, leave
  a comment saying what the failure mode was — most of the long comments in this codebase
  are exactly that, and they're why the traps above are known.
- ASCII `--` rather than em dashes inside code comments.
- Match the surrounding style; don't reformat code you aren't changing.
