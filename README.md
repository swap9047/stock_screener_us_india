# Stock Screener & AI Alert Dashboard

A powerful, self-hosted Streamlit application for tracking US and Indian stock markets. It combines rule-based technical screening with an advanced, multi-tiered AI pipeline to generate expert verdicts, summarize market-moving news, and send automated alerts directly to Discord.

## 🚀 Key Features

*   **Multi-Market Support:** Track both US (NYSE/NASDAQ) and Indian (NSE/BSE) equities in a single dashboard.
*   **Custom Filters & Alerts:** Build highly customizable rule-chains (e.g., "Price > 10-week EMA" AND "Expert Take == Accumulate").
*   **Discord Integrations:** Runs automated cron jobs via GitHub Actions to evaluate your custom rules and ping a Discord webhook when triggered.
*   **State Persistence:** Seamlessly syncs your UI-driven configuration changes (like updating your watchlist) directly back to your GitHub repository so they persist across Streamlit Community Cloud redeploys.

## 🧠 Advanced AI Pipelines

This application utilizes a robust, multi-tiered AI architecture, separating "Search" from "Reasoning" tasks to minimize hallucinations and maximize accuracy.

### 1. Expert Views (`expert_views.py`)
Generates actionable verdicts (`ACCUMULATE`, `HOLD`, `CAUTION`) for your watchlist by acting as a quantitative financial analyst. 
*   **Search Model:** Uses `gemma-4-26b-a4b-it` to perform highly targeted searches for institutional ratings and analyst upgrades/downgrades.
*   **Reasoning Model:** Uses `gemini-3.5-flash-lite` (with a high thinking budget) to synthesize pre-computed technical indicators (Trend, EMAs, Volatility Stops) with the targeted news search. 
*   **Resilience:** Features a 3-tier fallback chain. If Gemini rate-limits, it falls back to `gemma-4-31b-it`, and then to `gemma-4-26b-a4b-it`, ensuring an analysis is always generated.

### 2. Market News Summarization (`news_summary.py`)
Provides a concise, noise-free summary of material catalysts (FDA approvals, earnings surprises, mergers) from the last 48 hours.
*   **Search Model:** Uses `gemma-4-31b-it` (or DuckDuckGo/Yahoo Finance fallbacks) for broad market news aggregation.
*   **Reasoning Model:** Uses `gemini-3.5-flash-lite` to strictly filter the raw news dump, stripping out fluff and highlighting only material market-moving events.

## 🛠️ Architecture & Core Files

*   `app.py`: The main Streamlit dashboard UI. Handles rendering, rule-building, and triggering GitHub config syncs.
*   `stock_data.py`: The quantitative engine. Downloads data via `yfinance` and pre-computes complex technical indicators (like Wilder's Volatility Stops and RSIs) to keep the LLMs mathematically grounded.
*   `alerts.py` & `alert_check.py`: The rule-evaluation engine and background chron job. Evaluates custom metrics and pushes state-managed alerts to Discord.
*   `filters.py`: A shared boolean evaluation engine that ensures UI Watchlist filters and background Discord alerts evaluate using the exact same logic.
*   `github_sync.py`: Handles atomic commits via the GitHub API to persist ephemeral Streamlit configurations.

## ⚙️ Setup & Deployment

This application is designed to be hosted for free on Streamlit Community Cloud, using GitHub Actions for the background cron jobs (news gathering and Discord alerts).

For detailed instructions on configuring your API Keys (`GEMINI_API_KEY`, `GITHUB_TOKEN`, `DISCORD_WEBHOOK_URL`) and setting up the automated workflows, please see the [Deployment Guide](DEPLOYMENT.md).
