# Stock Screener & AI Alert Dashboard

A self-hosted Streamlit application for tracking equities across any number of watchlists.
It combines rule-based technical screening with a multi-tiered AI pipeline that generates
expert verdicts and fundamental sentiment, summarizes market-moving news, and sends
automated alerts to Discord.

## 🚀 Key Features

*   **Registry-driven watchlists:** Watchlists are defined in `markets.json`, each with its
    own benchmark — add one from the dashboard without touching code.
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
*   **Signal (Chart × News):** One automatic label per ticker from the two independent
    inputs — the chart (Trend) and the news (Sentiment): *Confirmed*, *Chart only*,
    *Mixed*, *News divergence*, *Chart up, news negative*, or *Avoid*. Trend itself is
    *Uptrend* only when all four of its conditions agree, *Downtrend* when all four
    disagree, and *Mixed* in between. Its colour is the dot next to
    the ticker symbol, and it is filterable, sortable and usable in alert rules.
*   **TA Rules:** A trader's weekly EMA flowchart (EMA convergence, support/resistance
    breaks, 10/20/40-week EMA breaks) applied node for node to the last completed week;
    the flowchart itself can be viewed from the sidebar.
*   **Ticker notes & flags:** Per-ticker free-text notes plus a colour flag that only you
    set — it takes the place of the Signal dot next to the ticker.
*   **Discord integrations:** GitHub Actions cron jobs evaluate your rules and ping a
    Discord webhook when they trigger, plus a weekly wrap-up digest. Every post ends with
    a short "not investment advice" footer.
*   **State persistence:** Configuration changed in the UI — watchlists, alert rules,
    notes and flags, settings, filters, custom columns, column layout — is committed back
    to the private data repository automatically, as one atomic commit at the end of the
    click that changed it. So the nightly jobs see it the same night, and it survives
    Streamlit Community Cloud redeploys, where the container filesystem is ephemeral.

## 🧠 AI Pipelines

Search and reasoning are deliberately separated, to keep verdicts grounded in retrieved
evidence rather than model recall.

### Expert Views (`expert_views.py`)
`ACCUMULATE`, `HOLD` or `CAUTION`, **decided in code from the other columns** and
recomputed live, so it never disagrees with them:
- **Accumulate:** Trend up **or** Tech Uptrend Yes, plus TA Rules Maintain/Add or Bullish
  Signal, and Sentiment not Bearish. Or a TA Bullish Signal, a breakout from a converging base, with
  Trend not down and Sentiment not Bearish.
- **Caution:** at least 2 points. TA Exit counts 2; TA Be Cautious, a Downtrend and a
  Bearish Sentiment count 1 each.
- **Hold:** everything else.

The nightly AI writes the explanation and a trade plan. It also reads the last 14 days of
material news, leaving results, guidance and ratings to Sentiment. A dated, quoted
negative event lowers the verdict **one step**, marked ⚑; examples are fraud, a
regulator's action, a lost contract, or dilution. News never raises the verdict. Search
refusals and "nothing found" sentences count as no news. Falls back through a shared
model ladder (`llm_util.py`) on rate limits.

### Fundamental Sentiment (`fundamentals_eval.py`)
A forward-looking `Positive` / `Neutral` / `Negative` read. The model extracts the facts and
a fixed rule decides. Forward signals carry the weight, and each decides on its own: guidance
raised or lowered (which outweighs everything), management's quoted outlook (improving or
cautious), guidance above or below analysts' consensus (only when the news states the
comparison), and upgrades or downgrades by named brokerages (not algorithmic rating sites).
They weigh 15, 6, 3 and 3: guidance outweighs the rest together, and the outlook outweighs an
analyst action or a consensus comparison on its own. Guidance raised but still below consensus
is Positive. The
quarter just reported weighs much less. When a forward signal is present, the quarter only
breaks a tie (profit up or down more than 15% year on year, or a beat or miss against a stated
consensus) and never outvotes it, so strong growth with a cautious outlook reads Negative. When
no forward signal is found, the quarter decides: profit up more than 25% reads Positive, down
more than 20% Negative. A beat or miss alone never decides. The search reaches back to each company's last reported results, and Yahoo's
year-on-year growth fills in when the news only gave absolute figures. The cell shows the
evidence, forward signals first (e.g. "Upgrade · Outlook ↑ · Profit +22%"), and the hover says
what decided it.

### Market News (`news_summary.py`)
A noise-free summary of material catalysts (FDA approvals, earnings surprises, M&A) from
the last 24 hours, plus events due in the next few days, filtered to strip out fluff.

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
*   `llm_util.py` — shared Gemini timeout/retry/model-ladder plumbing used by all three AI
    pipelines, including the fuse that stops a nightly job early when its calls keep
    failing.
*   `alerts.py` / `alert_check.py` — rule evaluation and the Discord cron job.
*   `ticker_notes.py` — notes, manual flags, and the Signal (Chart × News) label.
*   `github_sync.py` — atomic commits via the GitHub API, and workflow dispatch.
*   `refresh_*.py` — background entry points run by GitHub Actions.

There is no database: all state is stored as JSON in a separate private repository
(`swap9047/stock_screener_data`), keeping personal holdings, notes, and alerts private
while the code repository stays public for unlimited free GitHub Actions minutes. See
[AGENTS.md](AGENTS.md) for the full architecture, the row-dict contract, the
registration checklist for adding a column, and the known traps — read it before
contributing (or before pointing an AI agent at this codebase).

## ⚙️ Setup & Deployment

Designed to run for free on Streamlit Community Cloud, with GitHub Actions for the
background jobs. For API tokens and keys (`DATA_REPO_TOKEN`, `GITHUB_TOKEN`,
`GEMINI_API_KEY`, `DISCORD_WEBHOOK_URL`) and workflow setup, see the
[Deployment Guide](DEPLOYMENT.md).
