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

## 6. Schedule the daily alert check (Discord messages)

Streamlit Cloud only runs the interactive web app — it can't run `alert_check.py` on a timer. Free fix: a **GitHub Actions** workflow that checks out your repo daily and runs the script.

Add `.github/workflows/daily-alerts.yml`:

```yaml
name: Daily Stock Alerts
on:
  schedule:
    - cron: "30 14 * * 1-5"   # 14:30 UTC = 8:00pm IST / after US market close-ish; adjust as you like
  workflow_dispatch: {}         # lets you trigger it manually from the Actions tab too

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: python alert_check.py
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
```

`load_discord_webhook()` already checks the `DISCORD_WEBHOOK_URL` environment variable first (falling back to `discord_config.json` for local runs), so the workflow above will work as-is once you add `DISCORD_WEBHOOK_URL` as a **repo secret** (repo → Settings → Secrets and variables → Actions → New repository secret) — separate from the Streamlit Cloud secret, GitHub Actions doesn't share those.

One catch: `alert_state.json` (tracks what's already fired, so you don't get duplicate pings) is gitignored, so a stateless Actions run starts fresh each time unless the workflow also commits/restores it. Let me know when you're at this step and I'll wire up state persistence (commit it back to the repo after each run, or use Actions cache) — better to get the web app and Discord connection working first, then layer in scheduling.

Alternative to GitHub Actions: Cowork's own scheduler can run `alert_check.py` for you, no GitHub Actions needed, but it runs from your Cowork session's environment, not from the deployed cloud app. Say the word and I'll set that up instead — it's simpler if you don't want to touch GitHub Actions YAML at all.

## Summary of what's free vs. what needs setup

| Piece | Status |
|---|---|
| Public URL for the dashboard | Free via Streamlit Community Cloud |
| Login gate | Built in, just needs `AUTH_USERNAME`/`AUTH_PASSWORD` secrets set |
| Discord alerts (manual "Send test message") | Works once webhook secret is set |
| Discord alerts (automatic daily) | Needs a scheduler — GitHub Actions (free) or Cowork's scheduler |
