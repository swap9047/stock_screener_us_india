# Deploying Stock Watchlist to the internet

This turns your local app into a URL you can open from your phone or any browser, free, via **Streamlit Community Cloud**. Daily Discord alerts need a second free piece (GitHub Actions) since Streamlit Cloud has no background scheduler.

## 1. Push the folder to GitHub

1. Create a new **private** repo on GitHub (Settings → your data stays out of search engines; private repos deploy fine on Streamlit Cloud).
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
   `.gitignore` already excludes `discord_config.json`, `auth_config.json`, and `alert_state.json` — your secrets and local state never get committed.

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
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/xxxx/yyyy"
AUTH_USERNAME = "yourname"
AUTH_PASSWORD = "choose-a-real-password"
```

Save — the app restarts automatically and picks these up (`get_discord_webhook()` and `get_auth_credentials()` both check `st.secrets` first). Choose your own username/password here; nothing to send back to me.

For **local runs**, the equivalent is a `.streamlit/secrets.toml` file (same format, gitignored already) or the two local JSON files: `discord_config.json` (`{"webhook_url": "..."}`) and `auth_config.json` (`{"username": "...", "password": "..."}`).

## 6. Schedule the alert check (Discord messages)

Streamlit Cloud only runs the interactive web app — it can't run `alert_check.py` on a timer. Free fix: a **GitHub Actions** workflow, already committed at `.github/workflows/daily-alerts.yml`.

The alert trigger cadence is managed by the GitHub Actions YAML, not by Streamlit. The current workflow wakes up only around **10:00 PM ET**, using two UTC cron lines so daylight saving time is handled safely:

```yaml
- cron: "0 2 * * *"   # 10:00 PM ET during EDT
- cron: "0 3 * * *"   # 10:00 PM ET during EST
```

The app's Alert Rules tab lets you choose which **days** a scheduled rule should run. The hour picker is intentionally limited to the one hour the workflow actually supports: **10:00 PM ET**. If you ever want alerts at more times, update both places together:

1. Add the additional UTC cron trigger(s) in `.github/workflows/daily-alerts.yml`.
2. Add the corresponding ET hour(s) to `ALLOWED_HOURS` / `HOUR_LABELS` in `alerts.py`.

On each scheduled wakeup, a cheap "gate" job installs only `requests` and asks `alerts.is_rule_due()` whether any enabled rule is due at that ET day/hour. The full check job installs all app dependencies, fetches live prices, and runs `alert_check.py` only when the gate says something is due. `alert_state.json` tracks which rule/ticker pairs were already active so you don't get duplicate pings every day a condition remains true; GitHub Actions persists that file between runs via `actions/cache`.

`load_discord_webhook()` checks the `DISCORD_WEBHOOK_URL` environment variable first (falling back to `discord_config.json` for local runs), so you just need `DISCORD_WEBHOOK_URL` as a **repo secret** (repo → Settings → Secrets and variables → Actions → New repository secret) — separate from the Streamlit Cloud secret above, GitHub Actions doesn't share those.

Rough cost: two lightweight gate runs/day (a few seconds each) plus one full check on due days, comfortably under GitHub's free 2,000 minutes/month for a private repo (or free either way on a public repo). One thing to know: GitHub auto-disables scheduled workflows after 60 days with no commits to the repo (it does email a heads-up first, sent to whoever last enabled the workflow) — a trivial commit (even just touching this README) resets that clock, so if you go quiet on the repo for ~2 months, either push something small or manually re-enable the workflow from the Actions tab. Also worth knowing: GitHub's scheduler is documented as best-effort, so scheduled runs can occasionally be delayed or skipped.

## 7. News digest (Discord + News tab)

A second, independent GitHub Actions workflow, `.github/workflows/news-summary.yml`, builds a daily news digest for both watchlists: for each ticker, it uses Gemini (with Google Search grounding, so it's real, cited web search — not the model's training data) to find important announcements, results, and stock moves from the last 24 hours, collates each watchlist into one summary, saves the result to `news_summary.json`, and sends both summaries to Discord. The app's **News** tab just displays that same `news_summary.json`.

It runs once a day at **7:00 AM ET** (before market open), using the same two-UTC-cron-lines-plus-runtime-check pattern as the alerts workflow to handle daylight saving time correctly.

To enable it, add one more repo secret (repo → Settings → Secrets and variables → Actions → New repository secret):

```
GEMINI_API_KEY = <your key from aistudio.google.com>
```

Get a free key at [Google AI Studio](https://aistudio.google.com/apikey). The workflow reuses the same `DISCORD_WEBHOOK_URL` secret as the alerts workflow.

A few things worth knowing:

- **Free-tier quotas are account-specific.** Check your own limits at AI Studio's Rate Limit dashboard before changing the model or batch size — this project's `gemini-2.5-flash` + 13-tickers-per-batch choice was tuned to fit comfortably under a 20-requests/day cap that's tighter than Google's generic published numbers, and the entire Gemini 3.x model family (3, 3.1, 3.5, 3.6, Lite or not) had **zero** free Search-grounding quota on the account this was built against.
- **This workflow commits `news_summary.json` directly to the repo itself** (it needs `contents: write` permission, already set) — unlike the other config files, this one is machine-generated, not edited through the app UI, so there's nothing to push from the app's GitHub sync button for this file.
- If the Gemini API call fails for a given day (rate limit, outage, etc.), that day's digest is simply skipped — no Discord message, no `news_summary.json` update, and the app's News tab keeps showing the last successful run until the next one succeeds.

## 8. Daily data refresh (faster page loads)

A third GitHub Actions workflow, `.github/workflows/data-refresh.yml`, fetches all watchlist tickers via yfinance once a day (also ~7:00 AM ET, same DST-safe pattern) and saves the result to `data_snapshot.json`. The app loads this snapshot on open instead of hitting yfinance live every session — much faster, and avoids every visitor re-fetching identical data.

No new secret needed — it only needs `contents: write` (already set) to commit `data_snapshot.json` back to the repo.

A few things worth knowing:

- **The "Refresh Data" button in the sidebar still works exactly as before** — clicking it always fetches live data for that session, bypassing the snapshot entirely. The sidebar caption shows which one you're looking at: "(daily snapshot)" or "(live fetch)".
- **The snapshot is skipped automatically, falling back to a live fetch, if it's stale in a way that matters**: if you've added a ticker to the watchlist since the last scheduled refresh (the snapshot won't have it yet), or changed a calc parameter in Settings (EMA lengths, thresholds, etc. — the snapshot was computed with whatever settings were live at refresh time). Either case just means one live fetch until tomorrow's 7 AM refresh catches up.
- Same edge case as the other two workflows: GitHub's scheduler is best-effort, so a run can occasionally be delayed or skipped — the gate for this one (and the news digest) uses a ±1 hour tolerance window to absorb realistic scheduler delay, the same fix applied to the alerts workflow after it silently missed a day from an exact-hour check with zero grace period.

## 9. Push config changes made through the deployed app back to GitHub

Here's a gap worth knowing about: if you edit alert rules, the watchlist, custom filters, or Settings through the **deployed** app's UI, that write only lands on that Streamlit Cloud instance's local disk. It does **not** reach your GitHub repo — so the GitHub Actions workflow above (which always checks out the repo's committed version of `alerts_config.json`) won't see those edits, and a redeploy wipes them.

The Alert Rules tab's **☁️ Push config to GitHub** section closes this gap: it commits the selected app-managed config files straight to your repo via GitHub's REST API (no git/SSH needed — just an HTTPS call using `requests`, which is already a dependency). The pushable files are:

- `watchlist.json`
- `custom_filters.json`
- `settings.json`
- `alerts_config.json`
- `column_prefs.json`

To enable it:

1. On GitHub: **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**. Scope it to just this repo, with **Contents: Read and write** permission (nothing else needed).
2. Add to your Streamlit Cloud secrets (same panel as step 5):
   ```toml
   GITHUB_TOKEN = "github_pat_xxxxx"
   GITHUB_REPO = "your-username/your-repo-name"
   GITHUB_BRANCH = "main"
   ```
3. Reload the app — the Alert Rules tab's GitHub push section will show which files it can push instead of the setup instructions.

Each push creates **one combined commit** containing all selected files. That matters on Streamlit Cloud because any commit can trigger a redeploy; bundling the files together avoids a partial update where the app restarts after the first file lands but before the rest are pushed.

## Summary of what's free vs. what needs setup

| Piece | Status |
|---|---|
| Public URL for the dashboard | Free via Streamlit Community Cloud |
| Login gate | Built in, just needs `AUTH_USERNAME`/`AUTH_PASSWORD` secrets set |
| Discord alerts (manual "Send test message") | Works once webhook secret is set |
| Discord alerts (automatic, per-rule schedule) | Needs `DISCORD_WEBHOOK_URL` repo secret — GitHub Actions workflow is already committed |
| Push config edits (made on the deployed app) back to GitHub | Needs `GITHUB_TOKEN`/`GITHUB_REPO` secrets — sidebar button already built |
| Daily news digest (News tab + Discord, 7 AM ET) | Needs `GEMINI_API_KEY` repo secret (free at aistudio.google.com) — GitHub Actions workflow is already committed |
| Daily data refresh (faster page loads, 7 AM ET) | No new secret needed — GitHub Actions workflow is already committed |
