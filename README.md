# Stock Screener & AI Alert Dashboard

A self-hosted Streamlit application for tracking equities across any number of watchlists.
It combines rule-based technical screening with a multi-tiered AI pipeline that generates
expert verdicts and fundamental sentiment, summarizes market-moving news, and sends
automated alerts to Discord.

## 🚀 Key Features

*   **Registry-driven watchlists:** Watchlists are defined in `markets.json`, each with its
    own benchmark — add one from the dashboard without touching code.
    Currently: US Invested, India Invested, US Watchlist, India Watchlist,
    and Substack-OutperformingMarket.
*   **Combined views:** Two roll-up tabs, *All Invested* and *All Watchlist*, merge any
    watchlists you choose (membership is editable in the UI, stored in
    `watchlist_groups.json`) and de-duplicate tickers that appear in more than one.
*   **Custom filters & alert rules:** Build rule-chains over ~70 metrics (e.g. "Price >
    10-week EMA" AND "Expert Take == Accumulate"), with AND/OR logic and references to
    other rules. The same engine evaluates them in the UI and in the nightly Discord job,
    so a preview never disagrees with an alert.
*   **Per-watchlist sorting:** Up to 6 sort levels per tab, each saved independently.
    Direction labels adapt to the column type — `↑/↓` for numbers, `A-Z` for text,
    `Old→New` for dates, `Top→Bottom` for categorical columns, which sort by a ranking you
    drag into place rather than alphabetically. A sort setup can be copied between tabs.
*   **Interested flag:** Mark tickers you're watching in the watchlist editor; they show a
    ★ next to the symbol and are filterable and sortable like any other column.
*   **Custom columns:** User-defined formula columns, usable in filters and alerts the
    moment they're created.
*   **Ticker notes & flags:** Per-ticker free-text notes plus a colour flag, either set by
    hand or auto-assigned by a 4-signal majority vote (Expert Take, Trend, Tech Uptrend,
    Sentiment).
*   **Discord integrations:** GitHub Actions cron jobs evaluate your rules and ping a
    Discord webhook when they trigger, plus a weekly wrap-up digest.
*   **State persistence:** Configuration changed in the UI is committed straight back to
    the GitHub repository as a single atomic commit, so it survives Streamlit Community
    Cloud redeploys — where the filesystem is ephemeral.

## 🧠 AI Pipelines

Search and reasoning are deliberately separated, to keep verdicts grounded in retrieved
evidence rather than model recall.

### Expert Views (`expert_views.py`)
Actionable verdicts — `ACCUMULATE`, `HOLD`, `CAUTION` — combining pre-computed technical
indicators with a targeted search for analyst ratings and upgrades/downgrades. Falls back
down a 3-tier model chain on rate limits so an analysis is always produced.

### Fundamental Sentiment (`fundamentals_eval.py`)
A `Positive` / `Neutral` / `Negative` read on the most recent earnings, guidance and
analyst coverage. A deterministic post-hoc guard downgrades a verdict to `Neutral` or
`Unknown` when the underlying evidence is stale, missing, or predates a confirmed earnings
report — so "Unknown" means unproven, not neutral.

### Market News (`news_summary.py`)
A noise-free summary of material catalysts (FDA approvals, earnings surprises, M&A) from
the last 48 hours, filtered to strip out fluff.

Both AI columns can be regenerated from the dashboard — for selected tickers, for whatever
is currently pending, or for a whole watchlist in the background via GitHub Actions scoped
to just the tab you clicked from.

## 🛠️ Architecture & Core Files

*   `app.py` — the Streamlit dashboard: tables, filters, sorting, editors and AI controls.
*   `stock_data.py` — the quantitative engine. Fetches via `yfinance` and pre-computes the
    indicators (Wilder's volatility stops, RSI, Mansfield RS, EMAs) that keep the LLMs
    mathematically grounded.
*   `filters.py` — the shared boolean condition engine behind both UI filters and
    background alerts.
*   `alerts.py` / `alert_check.py` — rule evaluation and the Discord cron job.
*   `github_sync.py` — atomic commits via the GitHub API, and workflow dispatch.
*   `refresh_*.py` — background entry points run by GitHub Actions.

There is no database: all state is JSON in the repo root. See
[CLAUDE.md](CLAUDE.md) for the full architecture, the row-dict contract, the
registration checklist for adding a column, and the known traps — read it before
contributing (or before pointing an AI agent at this codebase).

## ⚙️ Setup & Deployment

Designed to run for free on Streamlit Community Cloud, with GitHub Actions for the
background jobs. For API keys (`GEMINI_API_KEY`, `GITHUB_TOKEN`, `DISCORD_WEBHOOK_URL`)
and workflow setup, see the [Deployment Guide](DEPLOYMENT.md).
