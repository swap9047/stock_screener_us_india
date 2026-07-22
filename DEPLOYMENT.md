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

## 7. Push config changes made through the deployed app back to GitHub

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
