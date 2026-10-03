# Deploying Stock Watchlist to the internet

This turns your local app into a URL you can open from your phone or any browser, free, via **Streamlit Community Cloud**. Daily Discord alerts need a second free piece (GitHub Actions) since Streamlit Cloud has no background scheduler.

## 1. Push the folder to GitHub

1. Create a new repo on GitHub (can be **public** or private — keeping the code repo public gives you unlimited free GitHub Actions minutes for the background workflows, while all personal data, watchlists, notes, and alerts live in the private data repo in step 1b).
2. From this folder:
   ```bash
   cd stock_alert_app
   git init
   git add .
   git commit -m "Stock watchlist app"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo-name>.git
   git push -u origin main
   ```
   `.gitignore` excludes every root `*.json` — your secrets (`discord_config.json`, `auth_config.json`) and all data files. The data lives in a separate private repo (next step).

## 1b. Create the private data repo

The code repo can stay **public** (free, unlimited GitHub Actions minutes — the AI workflows use far more than a private repo's 2,000 free minutes a month), because no data is ever committed to it. Watchlists, notes, alert rules, settings, snapshots, AI results and alert state all live in a **private** repo instead:

1. Create an empty private repo, e.g. `<you>/stock_screener_data`, and push this folder's root `*.json` files to it (except `auth_config.json` / `discord_config.json`).
2. Set `DATA_REPO_DEFAULT` in `github_sync.py` and the `repository` default in `.github/actions/load-data/action.yml` to that repo (or set a `DATA_REPO` secret/env var).
3. **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**, scoped to **only the data repo**, with **Contents: Read and write**.
4. Add it as `DATA_REPO_TOKEN` in two places: the Streamlit Cloud secrets (step 5) and this code repo's Actions secrets (repo → Settings → Secrets and variables → Actions).

On start, the app downloads any data file it doesn't have from the data repo, and shows an error instead of running if it can't (so it never creates empty files that a later save would push over your data). Workflows check the data repo out into `data/`, run, and commit their output there. Their logs — public, like the repo — pass through `log_redact.py`, which masks tickers, company names and watchlist names.

## 2. Deploy on Streamlit Community Cloud

1. Go to **share.streamlit.io**, sign in with GitHub, click **New app**.
2. Pick your repo, branch `main`, main file `app.py`. Deploy.
3. First boot installs `requirements.txt` automatically. Takes a minute or two.
4. You'll get a public URL like `https://<something>.streamlit.app`.

## 3. Connect Discord (the app UI)

Open the deployed app → **Alert Rules** tab → **Discord webhook**. To get a webhook URL: in Discord, go to your server → Settings → Integrations → Webhooks → New Webhook → copy the URL.

Don't paste it into the app's text box on a public deployment (it'd only save to that instance's disk, which Streamlit Cloud wipes on redeploy). Instead set it as a **secret** — see step 5.

## 4. Password-protect the app

The app already has a login gate built in (`require_login()` in `app.py`) — it's just inactive until you set credentials. Once set, anyone hitting your URL sees a sign-in form before any data loads.

This is a simple session-based gate suitable for keeping casual visitors out, not bank-grade security — good enough for a personal tool on a public URL.

## 5. Set secrets on Streamlit Cloud

In your app's dashboard: **⋮ menu → Settings → Secrets**, paste:

```toml
# Login gate (step 4)
AUTH_USERNAME = "yourname"
AUTH_PASSWORD = "choose-a-real-password"

# Private data repo sync (step 1b) — required for persisting UI edits across redeploys
DATA_REPO_TOKEN = "github_pat_xxxx"
DATA_REPO = "your-username/stock_screener_data"   # optional if matching DATA_REPO_DEFAULT
DATA_REPO_BRANCH = "main"                         # optional, defaults to main

# Code repo workflow dispatch (step 9) — required for Re-analyze All / Refresh news buttons
GITHUB_TOKEN = "github_pat_yyyy"
GITHUB_REPO = "your-username/your-code-repo"
GITHUB_BRANCH = "main"                            # optional, defaults to main

# Discord webhook for manual "Send test message" / UI alerts (step 3)
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/xxxx/yyyy"

# Gemini API key for single-ticker on-demand AI analysis in the app UI
GEMINI_API_KEY = "AIzaSy..."
```

Save — the app restarts automatically and picks these up (`get_discord_webhook()`, `get_auth_credentials()`, `get_data_repo_config()`, and `get_gemini_api_key()` all check `st.secrets` first). Choose your own username/password here.

For **local runs**, the equivalent is a `.streamlit/secrets.toml` file (same format, gitignored already) or local env vars / JSON files (`discord_config.json`, `auth_config.json`).

## 6. Schedule the alert check (Discord messages)

Streamlit Cloud only runs the interactive web app — it can't run `alert_check.py` on a timer. Free fix: a **GitHub Actions** workflow, already committed at `.github/workflows/daily-alerts.yml`.

The alert trigger cadence is managed by the GitHub Actions YAML, not by Streamlit. Like every scheduled workflow here, `daily-alerts.yml` wakes **hourly**, and a shared slot gate (`.github/actions/slot-gate`) lets the first wake-up after **9:00 PM ET** do that evening's check, up to 22 hours late. Slots are wall-clock ET hours, so daylight saving needs no arithmetic, and a cron run GitHub delays or drops no longer costs a day.

The app's Alert Rules tab lets you choose which **days** a scheduled rule should run. The hour picker is intentionally limited to the one hour the workflow actually supports: **9:00 PM ET**. If you ever want alerts at more times, update both places together:

1. Add the ET hour to the slot gate's `slots` input in `.github/workflows/daily-alerts.yml` (every workflow wakes hourly; the gate decides which run does a slot's work).
2. Add the corresponding ET hour(s) to `ALLOWED_HOURS` / `HOUR_LABELS` in `alerts.py`.

On each scheduled wakeup, a cheap "gate" job installs only `requests` and asks `alerts.is_rule_due()` whether any enabled rule is due at that ET day/hour. The full check job installs all app dependencies, fetches live prices, and runs `alert_check.py` only when the gate says something is due. `alert_state.json` tracks which rule/ticker pairs were already active so you don't get duplicate pings every day a condition remains true; the workflow commits it to the private data repo after each run (it used to live in `actions/cache`, where an eviction reset it and re-fired everything). If the file is ever missing, `alert_check.py` records the current state and sends nothing that run instead of flooding Discord.

`load_discord_webhook()` checks the `DISCORD_WEBHOOK_URL` environment variable first (falling back to `discord_config.json` for local runs), so you just need `DISCORD_WEBHOOK_URL` as a **repo secret** (repo → Settings → Secrets and variables → Actions → New repository secret) — separate from the Streamlit Cloud secret above, GitHub Actions doesn't share those.

Rough cost: an hourly gate run (a few seconds each) plus one full check per due slot, comfortably under GitHub's free 2,000 minutes/month for a private repo (or free either way on a public repo). One thing to know: GitHub auto-disables scheduled workflows after 60 days with no commits to the repo (it does email a heads-up first, sent to whoever last enabled the workflow) — a trivial commit (even just touching this README) resets that clock, so if you go quiet on the repo for ~2 months, either push something small or manually re-enable the workflow from the Actions tab. Also worth knowing: GitHub's scheduler is documented as best-effort, so scheduled runs can occasionally be delayed or skipped.

## 7. News digest (Discord + News tab)

A second, independent GitHub Actions workflow, `.github/workflows/news-summary.yml`, builds a daily news digest for every watchlist in scope (`news_watchlist_scope` in Settings — blank means the *All Invested* group; pick groups or individual watchlists on the News tab): for each ticker, it uses Gemini (with Google Search grounding, so it's real, cited web search — not the model's training data) to find important announcements, results, and stock moves from the last 24 hours, collates each watchlist into its own summary, saves the result to `news_summary.json`, and sends each summary to Discord. The app's **News** tab just displays that same `news_summary.json`.

It does its work once per **8:00 PM ET** slot: the workflow wakes hourly and the shared slot gate lets the first run after the slot through (same pattern as every other scheduled workflow — see AGENTS.md).

To enable it, add one more repo secret (repo → Settings → Secrets and variables → Actions → New repository secret):

```
GEMINI_API_KEY = <your key from aistudio.google.com>
```

Get a free key at [Google AI Studio](https://aistudio.google.com/apikey). The workflow reuses the same `DISCORD_WEBHOOK_URL` secret as the alerts workflow.

**Spreading the load over several keys.** A nightly AI run is ~250 calls against one project's free quota, so the three AI workflows rotate across every key they find, picking one per call. Any secret or env var whose **name starts with `GEMINI_API_KEY`** counts — `GEMINI_API_KEY_BACKUP`, `GEMINI_API_KEY_BACKUP_B`, `GEMINI_API_KEY_2`, whatever you like. Two steps per key: add the repo secret, then add a matching line to the `env:` block of the AI steps in `news-summary.yml`, `expert-views.yml` and `fundamentals.yml` (Actions only exposes secrets a step names explicitly). Each run logs `[key rotation] N key(s): ...` so you can confirm it sees them all. For local runs, put the keys in `.env`.

A few things worth knowing:

- **Free-tier quotas are account-specific.** Check your own limits at AI Studio's Rate Limit dashboard before changing models. Search runs on `gemma-4-26b-a4b-it` with Google Search grounding (one search per ticker, three attempts, each on a different key), because the Gemini 3.x models had **zero** free Search-grounding quota on the account this was built against; the reasoning and collation stages use Gemini models.
- **This workflow commits `news_summary.json` to the private data repo itself** (via `DATA_REPO_TOKEN`) — unlike the other config files, this one is machine-generated, not edited through the app UI, so there's nothing to push from the app's GitHub sync button for this file.
- If half or more of the day's searches fail (rate limits, an outage, dead keys), the run posts **nothing** to Discord and fails, so the slot gate retries it on a later wake-up; the retry posts the day's only digest. The failed run's `news_summary.json` is still saved, so the News tab's "search failed" count shows what happened.
- **Manual smoke tests:** The workflow accepts `markets` (comma-separated watchlist keys) and `limit` (max tickers to analyze) inputs on manual `workflow_dispatch`. When either is provided, the run is considered a partial test: it replaces only those watchlists' digests in `news_summary.json`, skips posting to Discord, and is titled "partial run" so the slot gate never counts it as that evening's digest (the same holds for `expert-views.yml` and `fundamentals.yml`, so a tab's **Re-analyze All** never stands in for the nightly full run).

## 8. Data refresh (faster page loads)

A third GitHub Actions workflow, `.github/workflows/data-refresh.yml`, fetches all watchlist tickers via yfinance and saves the result to `data_snapshot.json`. It runs **hourly, every hour, around the clock** — not just during US market hours, since India trades roughly 23:45-06:00 ET and a daytime-only window would sample that session exactly never. It's a single cron with no EDT/EST pair or gate job needed (an hourly cron fires correctly in both seasons), and idle hours cost nothing extra: the commit step no-ops when the snapshot is byte-identical to the last run. The app loads this snapshot on open instead of hitting yfinance live every session — much faster, and avoids every visitor re-fetching identical data.

No new secret needed beyond `DATA_REPO_TOKEN` — it commits `data_snapshot.json` to the private data repo.

A few things worth knowing:

- **The "Refresh Data" button in the sidebar still works exactly as before** — clicking it always fetches live data for that session, bypassing the snapshot entirely. The sidebar caption shows which one you're looking at: "(daily snapshot)" or "(live fetch)".
- **The snapshot is skipped automatically, falling back to a live fetch, if it's stale in a way that matters**: if you've added a ticker to the watchlist since the last scheduled refresh (the snapshot won't have it yet), or changed a calc parameter in Settings (EMA lengths, thresholds, etc. — the snapshot was computed with whatever settings were live at refresh time), or the calculation code in `stock_data.py` changed since the snapshot was made (comment- and docstring-only edits don't count). Any of these just means one live fetch until the next hourly refresh catches up — after deploying a change to `stock_data.py`, you can run the data refresh workflow by hand to skip the wait.
- GitHub's scheduler is best-effort, so a run can occasionally be delayed — with an hourly cron this self-heals within the hour. The once-a-day workflows (news digest, Expert Views, Fundamentals, alerts) wake hourly too, and a shared slot gate lets the first run after their ET slot do the work, up to 22 hours late, on the reasoning that a late run beats a skipped one (see AGENTS.md, "GitHub Actions").

## 8b. Expert Views generation

A fourth GitHub Actions workflow, `.github/workflows/expert-views.yml`, does its work once per **1:00 AM ET** slot. It refreshes `data_snapshot.json` first to guarantee same-day price data, then generates AI Expert Take verdicts (`ACCUMULATE`, `HOLD`, `CAUTION`) across all tickers and commits `expert_views.json` (and the updated snapshot) to the private data repo. Each verdict also reads that ticker's checked quarterly facts from `fundamentals.json`, which the 9:00 PM Sentiment run (8c) writes a few hours earlier.

If 10 tickers in a row fail (typically dead keys or an exhausted quota), the run stops, keeps every previous verdict, and fails, so it shows red in the Actions tab; the slot gate retries it at the next hourly wake-up. The same applies to 8c.

It uses `GEMINI_API_KEY` and `DATA_REPO_TOKEN` repo secrets. It supports `workflow_dispatch` with optional `markets` and `limit` inputs for scoped runs and smoke tests.

## 8c. Fundamental Views generation

A fifth GitHub Actions workflow, `.github/workflows/fundamentals.yml`, does its work once per **9:00 PM ET** slot. It reads the latest `data_snapshot.json` and evaluates quarterly earnings, guidance, management outlook and analyst coverage to produce fundamental sentiment (`fundamentals.json`), committing the results to the private data repo. Each ticker's news search reaches back to its last reported results (looked up from Yahoo; 100 days when Yahoo has no date), so the latest quarter stays in view between reporting seasons.

It uses `GEMINI_API_KEY` and `DATA_REPO_TOKEN` repo secrets. It supports `workflow_dispatch` with optional `markets` and `limit` inputs.

## 8d. Market breadth & dashboard performance

A sixth GitHub Actions workflow, `.github/workflows/market-breadth.yml`, does its work once per **10:00 AM** and **10:00 PM ET** slot, Monday to Saturday. It runs `refresh_market_breadth.py` and `refresh_dashboard_perf.py` to compute advance/decline breadth metrics (`market_breadth.json`) and portfolio performance metrics (`dashboard_perf.json`), committing both to the private data repo.

No new secret needed beyond `DATA_REPO_TOKEN`.

## 8e. Weekly wrap-up digest

A seventh GitHub Actions workflow, `.github/workflows/weekly-wrapup.yml`, runs once a week on **Sunday at 9:00 PM ET** (Monday 01:00 / 02:00 UTC, ~6:30 AM IST before Monday's India market open). It runs `weekly_wrapup_check.py` to evaluate all rules against Friday's weekly close, formats active alerts and roll-up metrics, sends the weekly wrap-up digest to Discord, and commits state (`weekly_wrapup_state.json`) to the private data repo.

It uses `DISCORD_WEBHOOK_URL` and `DATA_REPO_TOKEN` repo secrets.

## 9. Push config changes made through the deployed app back to GitHub

If you edit alert rules, the watchlist, custom filters, notes and flags, custom columns, the column layout or Settings through the **deployed** app's UI, that write first lands on that Streamlit Cloud instance's local disk, which a restart wipes and the GitHub Actions workflows never see. So the app commits every such edit to the **private data repo** via GitHub's REST API (no git/SSH needed — just HTTPS with `requests`), automatically: watchlist saves, adds and renames push as they happen, the AI re-analyze buttons push the tickers they changed, and everything else is pushed as one commit at the end of the click that changed it. A push that fails shows a sidebar warning and is retried on your next click; the Alert Rules tab's **☁️ Push config to GitHub** button remains as a manual fallback.

A config file is only auto-pushed when this container has a record of having pulled or pushed it (every file downloaded at startup has one), so a stale local copy can never overwrite newer data. Local and AppTest runs with `SKIP_GITHUB_PULL=1` never auto-push.

This uses the same `DATA_REPO_TOKEN` as step 1b — nothing else to set up. Each push is **one combined commit**, so a multi-file change (watchlist + interested + snapshot) is never half-applied.

The **Re-analyze All** and **Refresh news** buttons start GitHub Actions runs instead, which needs a second token on the *code* repo:

1. Fine-grained token scoped to this code repo, with **Actions: Read and write**.
2. Add to your Streamlit Cloud secrets:
   ```toml
   GITHUB_TOKEN = "github_pat_xxxxx"
   GITHUB_REPO = "your-username/your-code-repo"
   GITHUB_BRANCH = "main"
   ```

## Summary of what's free vs. what needs setup

| Piece | Status |
|---|---|
| Public URL for the dashboard | Free via Streamlit Community Cloud |
| Login gate | Built in, just needs `AUTH_USERNAME`/`AUTH_PASSWORD` secrets set |
| Discord alerts (manual "Send test message") | Works once webhook secret is set |
| Discord alerts (automatic, 9:00 PM ET) | Needs `DISCORD_WEBHOOK_URL` repo secret — GitHub Actions workflow is already committed |
| Data storage + push config edits (made on the deployed app) | Needs a private data repo and `DATA_REPO_TOKEN` (Streamlit + Actions secrets) |
| Re-analyze / Refresh news buttons | Needs `GITHUB_TOKEN`/`GITHUB_REPO` secrets (Actions permission on the code repo) |
| Daily news digest (News tab + Discord, 8:00 PM ET) | Needs `GEMINI_API_KEY` repo secret (free at aistudio.google.com) — GitHub Actions workflow is already committed |
| Hourly data refresh (around the clock) | No new secret needed — GitHub Actions workflow is already committed |
| Daily Expert Views (1:00 AM ET) | Needs `GEMINI_API_KEY` repo secret — GitHub Actions workflow is already committed |
| Daily Fundamental Views (9:00 PM ET) | Needs `GEMINI_API_KEY` repo secret — GitHub Actions workflow is already committed |
| Market Breadth & Performance (10 AM & 10 PM ET) | No new secret needed — GitHub Actions workflow is already committed |
| Weekly Wrap-up digest (Sunday 9:00 PM ET) | Uses `DISCORD_WEBHOOK_URL` repo secret — GitHub Actions workflow is already committed |
