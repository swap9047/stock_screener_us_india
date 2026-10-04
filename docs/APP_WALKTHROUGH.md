# Stock Watchlist: how the app works

This document walks through the whole app: what it does, how the data flows, how each
column and verdict is worked out, how the alert rules engine behaves, and what runs in
the background. It describes the code as of 2026-10-03 (`main` @ `d775371`).

For *changing* the code, read [AGENTS.md](../AGENTS.md) first: it has the contracts and
traps. For *deploying*, see [DEPLOYMENT.md](../DEPLOYMENT.md). This document is about
behaviour.

---

## 1. The app in one minute

It is a personal stock screener with three main parts.

- **The dashboard** (`app.py`, Streamlit) shows one tab per watchlist. Each tab has a
  wide table of technical indicators and fundamentals. Two AI columns sit beside them:
  **Expert Take** (a buy, hold or caution verdict) and **Sentiment** (a read of the
  latest earnings). Then come a few rule-based reads: **Trend**, **Tech Uptrend**,
  **TA Rules** and **Expert Take**. You can filter, sort, annotate and export the table, and
  build alert rules on any column.
- **The background jobs** (GitHub Actions) refresh prices every hour and run three AI
  pipelines each night. They evaluate your alert rules, post results to Discord, and
  send a weekly digest.
- **The data** is a set of JSON files in a **private** GitHub repo. The code repo is
  public, which keeps Actions minutes free, so no personal data is ever committed to it.

There is no database and no server of its own. The Streamlit app reads the JSON files
and writes them back through the GitHub API. The jobs check out the data repo, run, and
commit their output.

```
              ┌──────────────────────────── private data repo (JSON) ───────────────────────────┐
              │ watchlists · rules · notes · settings · snapshot · AI views · alert state · ... │
              └───────▲───────────────────────────▲─────────────────────────────────▲───────────┘
   pull every 5 min / │ push on save or button    │ load-data + commit-data         │
                      │                           │                                 │
        ┌─────────────┴──────────┐     ┌──────────┴───────────────────────┐         │
        │ Streamlit app (app.py) │     │ GitHub Actions (hourly wake-ups) │─────────┘
        │  tables · filters ·    │     │ data refresh · AI jobs · alerts ·│
        │  rule builder · AI     │     │ breadth · weekly wrap-up         │
        └─────┬───────────┬──────┘     └───┬────────────┬─────────────┬───┘
              │           │                │            │             │
          Yahoo Finance  Gemini        Yahoo Finance  Gemini       Discord
          (Refresh Data, (re-analyze   (prices,       (search +    (alerts, news,
           new tickers)   buttons)      statements)    reasoning)   wrap-up)
```

### A day in the life (all times US Eastern)

| When (slot) | Job | What it produces |
|---|---|---|
| Every hour, around the clock | Data refresh | `data_snapshot.json`: prices plus every indicator, for every ticker |
| 8:00 PM | News digest | `news_summary.json` + one Discord digest per watchlist in scope |
| 9:00 PM | Sentiment (fundamentals) | `fundamentals.json`: the AI earnings and guidance read per ticker |
| 9:00 PM | Alert check | Discord alerts for rules due that day; `alert_state.json` |
| 1:00 AM | Expert Take | A fresh snapshot, then `expert_views.json`: the AI verdict per ticker |
| 10:00 AM and 10:00 PM, Mon-Sat | Market breadth + performance | `market_breadth.json`, `dashboard_perf.json` |
| Sunday 9:00 PM | Weekly wrap-up | A Discord digest of every enabled rule; `weekly_wrapup_state.json` |

GitHub starts scheduled runs late, often by 2-5 hours, so the times above are when a
job *becomes due*. A shared "slot gate" lets the first wake-up after that time do the
work, up to 22 hours late (10 h for breadth). See §10.

---

## 2. Where everything lives

### Modules

| File | Role |
|---|---|
| `app.py` | The whole UI: login, sidebar, every tab, filters, sorting, editors, AI controls, the alert builder |
| `stock_data.py` | Yahoo fetching, all indicator maths, watchlist and registry I/O, the metric registry, snapshot save/load and staleness |
| `filters.py` | The condition engine shared by table filters **and** alerts, so the two always agree |
| `alerts.py` / `alert_check.py` | Rule normalisation, scheduling, edge-trigger state, Discord message building and sending / the nightly job |
| `weekly_wrapup.py` / `weekly_wrapup_check.py` | The Sunday digest: pure logic / the job |
| `ticker_notes.py` | Notes and manual flags |
| `custom_columns.py` | Your formula columns, with a safe arithmetic evaluator (no `eval`) |
| `fundamentals_eval.py` / `refresh_fundamentals.py` | The Sentiment pipeline / its nightly job |
| `expert_views.py` / `refresh_expert_views.py` | The Expert Take pipeline / its nightly job |
| `news_summary.py` / `news_check.py` | The 3-stage news digest / its nightly job |
| `llm_util.py` | Shared Gemini plumbing: key rotation, model ladders, timeouts, the failure fuse |
| `github_sync.py` | Data-repo bootstrap, the 5-minute pull, atomic pushes, per-entry AI pushes, workflow dispatch |
| `json_store.py` | Strict JSON reads and atomic writes (standard library only) |
| `refresh_data.py`, `refresh_market_breadth.py`, `refresh_dashboard_perf.py` | Thin job entry points |
| `log_redact.py` | Masks tickers, company names and watchlist names in the public Actions logs |
| `watchlist_labels.py` | Tab labels a watchlist may not use |

### The data files: three kinds

| Kind | Files | Who writes them |
|---|---|---|
| **Your configuration** (edited in the UI) | `watchlist.json`, `markets.json`, `watchlist_groups.json`, `interested.json`, `alerts_config.json`, `custom_filters.json`, `custom_columns.json`, `ticker_notes.json`, `settings.json`, `column_prefs.json`, `ticker_index.json` | The app |
| **Generated data** (written by jobs) | `data_snapshot.json`, `expert_views.json`, `fundamentals.json`, `news_summary.json`, `market_breadth.json`, `dashboard_perf.json`, `alert_state.json`, `weekly_wrapup_state.json` | The workflows. The app also writes the first three in narrow, guarded ways |
| **Local only** (never pushed) | `auth_config.json`, `discord_config.json`, `.data_sync_state.json`, `.env` | You / the container |

The table above shows `data_snapshot.json`, `expert_views.json` and `fundamentals.json`
as generated, but the app can write them too. It does so only in guarded ways, so it
never pushes them over a newer job commit (§9).

---

## 3. From prices to a table row

### 3.1 Fetching (`stock_data.fetch_all_markets` → `fetch_snapshot`)

1. **The universe.** Every ticker from every watchlist, de-duplicated.
2. **Benchmark per ticker.** Each ticker gets a permanent index the first time it is
   seen: INR or an NSE/BSE listing becomes **Nifty 500 (`^CRSLDX`)**, USD or a US
   exchange becomes **S&P 500 (`SPY`)**. The assignment is stored in
   `ticker_index.json` and never changes automatically. The watchlist's own benchmark
   in `markets.json` is used **only** when a ticker can't be classified. So RS and the
   relative returns are always measured against the ticker's own index, whichever
   watchlist it sits in.
3. **One bulk download per benchmark group.** It fetches 5 years of daily OHLCV with
   auto-adjusted prices: a 90 s timeout per attempt, 3 attempts, 30 s apart.
4. **Per-ticker extras.** For each ticker it reads Yahoo's `.info` (name, P/E, P/B,
   EV/EBITDA, market cap, operating cash flow, ROE, quarterly growth, most recent
   quarter) and four financial statements (annual cash flow, income and balance sheet,
   plus the quarterly income statement), with a 0.5 s pause between tickers.
5. **Too little history.** A ticker with fewer than **60 daily bars** gets no row and is
   recorded under `short_history` ("too new to compute"), so it doesn't make the
   snapshot look incomplete.

### 3.2 Guards against bad Yahoo data

| Problem | Guard |
|---|---|
| Yahoo serves an *older* series than what's stored (it happens in the evenings) | `reject_stale_rows` keeps the stored row while it is ≤5 days old, and fills in any field the stored row lacks |
| A whole benchmark group, or single tickers, come back empty | `fill_snapshot_gaps` keeps the last known row. The UI says so |
| A job that **judges** closes runs mid-session (often for India: NSE trades ~23:45-06:00 ET) | `completed_sessions_only=True` drops each ticker's still-forming bar by its own exchange's clock. Used by the alert and wrap-up jobs |
| A judging job gets a partial universe | A skipped group, or more than 10% of tickers missing, makes the job **fail** so the slot gate retries. A few missing tickers only warn |

### 3.3 The snapshot and when it counts as "out of date"

`data_snapshot.json` holds every market's rows. It also records the **calculation
settings** and a **code fingerprint** of `stock_data.py`. The fingerprint is a hash of
the code with comments and docstrings stripped out.

On page load the app always serves the snapshot. It never fetches from Yahoo unless you
click **Refresh Data** or save a watchlist. If the snapshot was computed with different
calculation settings, by different calculation code, or lacks a row for a watchlist
ticker, you see a warning that names the reason, plus last-known data.

### 3.4 Enrichment: fields attached on every page run

After loading, every row gets these fields, in this order:
1. **Custom columns**, evaluated from their current formula.
2. **Note and Flag**, from `ticker_notes.json`.
3. **Interested, Sentiment, Expert Take and Expert News?**, from `interested.json` and
   the two AI files, using the guarded values.
4. **Data age**, recomputed for the "haven't updated" caption.

The same functions run in the background jobs (`stock_data.enrich_rows`). That is what
guarantees an alert on Expert Take, Sentiment or a custom column evaluates the same way at
night as in the app's preview.

---

## 4. Indicator reference

Prices show 1 decimal under 1,000 and whole numbers above. "Weekly" bars are Friday-ended (`W-FRI`) and,
except for TA Rules, **include the current, still-forming week**.

### Moving averages
- **10 / 20 / 40 WEMA**: exponential moving averages of weekly closes. The periods can
  be changed in Settings.
- **10 / 50 / 200 DSMA**: *simple* moving averages of daily closes. (The internal keys
  are `ema10_daily`, `ema50` and `ema200`, but these are SMAs.) The colour shows whether
  the last close is above (green) or below (red).

### Momentum and strength
- **RSI-D / RSI-W / RSI-M**: Wilder RSI(14) on daily, weekly and monthly closes, seeded
  the way TradingView seeds it. ≤30 shows green (oversold) and ≥70 red (overbought).
  **RSI-M (12)** is the monthly RSI at period 12, kept separately for scans.
- **RS-D / RS-W / RS-M**: Mansfield relative strength against the ticker's index:
  `((stock/index ratio today ÷ SMA(ratio, n)) − 1) × 100`, with n = 63 daily, 26 weekly
  and 12 monthly bars. Above 0 means the stock is outperforming its index's trend.
- **ADX-W / ADX-M**: Wilder ADX at periods 14 (weekly) and 12 (monthly). It measures
  trend *strength* only; 25+ is "trending".

### Volatility stop
- **VStop-W**: a port of TradingView's *Volatility Stop* on weekly bars (length 10, ATR
  factor 2, source = close). The stop ratchets from the running max or min close since
  the last flip. It needs at least 52 weekly bars.
- **VStop Dir**: Up or Down.
- **VStop Weeks Ago**: weeks since the last flip. "N+" means it never flipped inside 5 years.
- **VStop-W (14)**: the same stop at length 14, used by scans.
- Settings can switch to a legacy engine, or to completed weeks only.

### Volume
- **Vol 10D / 20D / 100D**: average daily volume over those windows.
- **Vol Trend**: the **median** day over 10 sessions ÷ the median day over 100. ≥1.4 is
  **Exploding**, ≤0.7 is **Declining**, anything else is **In-line**.
- **Why medians for the volume tests.** One results-day spike can lift a 100-day
  *average* for five months, making normal trading look like volume "dried up". All three
  volume tests (Vol Trend, Trend's Strong, Tech Uptrend) compare median days; the Vol
  columns themselves still show averages.
- **Net Vol 10D**: the last 10 sessions' volume, counted positive on up-closes and
  negative on down-closes. The label is **Positive** or **Negative**, and the tooltip
  shows net as a % of total.

### Highs, lows and breakout geometry
- **52W High / Low**: the intraday extremes of the last 252 sessions.
- **52W High Age**: sessions since that high was set.
- **26WH / 52WH / 5Y High Distance**: how far **below** that high the close is, as a
  **positive** %. 0 means at the high.
- **5Y High**: the highest high in the 5-year history.
- **Breakout Window**: the number of trading days back to the overhead level price is
  now testing. A level is the most recent confirmed swing-high close (highest of ±10
  sessions) that is more than 5% above today. If there is none, the fallback is simply
  the last close at or above today. 0 means *blue sky*: no prior close was ever this high.
- **Overhead Supply**: the share of the last year's traded volume that changed hands at
  closes **above** today's. It measures how much stock is underwater.

### Returns
- **% Chg**: the 1-day change.
- **Perf 1M / 3M / 6M / 1Y / 3Y %**: returns over 22 / 63 / 126 / 252 / 756 sessions.
- **1W / 1M / 6M Ret vs Index**: the stock's return minus its index's return, in
  percentage points, over 5 / 22 / 126 sessions on a shared calendar.
- **10/30 W Golden Cross (weeks ago)**: weeks since the 10-week EMA crossed above the
  30-week EMA. It is blank while the 10-week EMA is below.

### Fundamentals (Yahoo)
- **P/E (TTM), P/E (Fwd), P/B, EV/EBITDA**: as Yahoo reports them.
- **P/Cashflow**: market cap ÷ operating cash flow.
- **ROE %**: Yahoo's TTM figure.
- **ROCE %**: operating income ÷ (equity + long-term debt), taken from **one** fiscal
  year (the latest with both figures). Blank when it can't be computed honestly.
- **CFO/OP 5Y**: cash from operations ÷ operating income, summed over up to 5 years.
  Above 1 means good earnings quality.
- **Qtr Profit / Revenue Growth %**: Yahoo's single-quarter year-on-year reads.
- **Qtr EPS Growth %**: diluted EPS year-on-year from the quarterly statement. It falls
  below Profit growth when shares were issued.
- **PAT / Revenue Growth TTM %**: these need 8 quarters. Yahoo gives about 5, so they
  are **blank by design**.
- **Reported Qtr**: calendar quarters for US listings ("Q2 2026"), and Indian fiscal
  quarters for India ("Q1 FY27" = Apr-Jun 2026).
- **Data Thru**: the date of the last bar. It shows red once 2 or more weekday sessions
  have passed with no newer bar, so Friday's close is not "stale" on Monday morning.

---

## 5. The rule-based reads

### 5.1 Trend (`compute_trend`)

**Direction.** **Uptrend** only if **all** of these hold:
1. The close is above the 40-week EMA.
2. The 40-week EMA is rising, judged by a least-squares slope over the last 3 weeks.
3. The 10-week EMA is above the 40-week EMA.
4. Weekly RS is above 0. This condition is skipped if there isn't enough history for it.

**Downtrend** only if **all four are bearish**. **Anything in between is Mixed**: Uptrend
has to be fully earned, but a split is not reported as a Downtrend either. A stock above
a rising 40-week EMA that merely lags its index is Mixed, not Downtrend. Hovering a Trend
cell shows a ✓/✗ for each condition.

**Neutral bands.** A condition too close to call doesn't vote either way:
- weekly RS within ±1;
- the 10-week EMA within 0.5% of the 40-week;
- the 40-week EMA moving less than 0.05% of its value per week.

Uptrend and Downtrend still need **at least 3 voting conditions, all agreeing**. So a
stock whose averages are flat *and* converged is Mixed. Price vs the 40-week EMA always
votes. Before the bands, an RS of −0.1 turned an otherwise clear Uptrend Mixed. The hover
marks a condition that sat out with "–".

**Live price.** Trend and Tech Uptrend use the current price and the forming week, by the
owner's choice. TA Rules uses the last completed weekly close.

**Strength.** A label becomes **Strong** only if **both** hold:
- the close is within 10% of the 52-week **high** (for an uptrend) or **low** (for a
  downtrend), **and**
- the 10-day median volume is at least 1.0 × the 100-day median.

Mixed is never Strong. That gives 5 labels: Strong Uptrend, Uptrend, Mixed, Downtrend,
Strong Downtrend.

### 5.2 Tech Uptrend (Yes/No)

**Yes** only if **all** of these hold:
- the close is above VStop-W, and VStop points **Up**;
- VStop has held that direction for **more than** 3 weeks;
- the close is above the 40-week EMA;
- the 10-day median volume is more than **0.3 ×** the 100-day median. 0.3 is the owner's
  choice: the leg asks whether volume has dried up, not whether it is surging.

The prices are compared **unrounded**. The VStop-W and 40W columns show values rounded
to 0.1, which is up to 1% on a low-priced stock.

### 5.3 TA Rules (a trader's weekly EMA flowchart)

The rules are applied **node for node**, to the **last completed week** only, so a
mid-week dip can't flip a holding to Exit.

```
Are the 10/20/40-week EMAs converging?   (max−min of the three ≤ 3% of the 40W)
 ├─ Yes → broken support?     → Exit
 │        broken resistance?  → Bullish Signal
 │        otherwise           → Wait/Watch
 └─ No  → broken 40W EMA?     → Exit
          broken 20W EMA?     → Be Cautious
          broken 10W EMA?     → Momentum Fading
          otherwise           → Maintain/Add
```

**What "broken" means.**
- For an EMA: the weekly close is more than **3%** below it.
- For a support or resistance zone: the close is more than 3% beyond the zone, **and**
  one of the previous 13 weekly closes was on the zone's other side. Without that
  second test, a level broken years ago would still count.

**How support and resistance zones are found** (`find_sr_zones`). The search covers the
last 156 weeks (3 years) of weekly **wicks**:
- **Turning points.** A turning point is the high or low of ±3 weeks, after which price
  moved at least 8% away within 8 weeks.
- **Zones.** Turning points within 3% of each other merge into one zone. A zone needs at
  least 2 touches.
- **Roles can swap.** Highs and lows count alike, so a broken ceiling that later acts
  as a floor is one zone.

Every threshold is a setting (`ta_*`). The flowchart image is in the private data repo
and opens from the sidebar ("TA Rules flowchart").

### 5.4 The ticker dot (Signal retired)

The dot next to every ticker is **Expert Take's colour**: 🟢 Accumulate, 🟡 Hold,
🔴 Caution, ⚪ Pending. Hovering it shows what decided the verdict. A manual Flag takes
its place.

It used to show **Signal**, a Trend × Sentiment label. The owner retired Signal on
2026-10-04, along with its two alert rules. Expert Take now covers both of Signal's
inputs, plus Tech Uptrend, TA Rules and the news step. The two also told different
stories: 15 tickers read Signal "News divergence" (yellow) while Expert Take said Caution
(red), because TA said Exit.

### 5.5 Flag and notes

**Flag** (Red, Yellow, Green or Blue) is only ever set by you, in the sidebar's *Ticker
Notes* panel. Notes are free text, and Settings can turn the note box into a dropdown
of your own options.

---

## 6. The AI pipelines

All three pipelines use Google Gemini through `llm_util.py`, and share the same
plumbing.

**Search is separate from reasoning.** A Gemma model with Google Search grounding
collects the facts, and a second model reasons over only those facts.

**Key rotation.** Every environment variable or secret whose name starts with
`GEMINI_API_KEY` joins the rotation. A key is picked at random for each call. A key
that fails authentication is retired for the rest of the run; a key that hits its quota
cools down for 120 s, and the call is retried on another key. Each run logs
`[key rotation] N key(s)` at the start and a per-key failure summary at the end.

**Ladders.** Searches make 3 attempts on `gemma-4-26b-a4b-it`, each on a different key.
Reasoning uses the configured model, then the same model again after 5 s, then the
26b model as a fallback. A terminal error (a bad request, or auth failing on every key)
stops the ladder at once.

**Timeouts.** Every call runs on a daemon thread with a 120 s ceiling, plus an HTTP
timeout in the SDK, so a hung call can't keep a job alive.

**Failure fuse.** If 10 tickers *in a row* fail, the Sentiment or Expert job stops.
Every remaining ticker keeps its previous view, and the job exits red so the slot gate
retries it.

### 6.1 Sentiment (`fundamentals_eval.py`, nightly 9 PM; 3 tickers analysed in parallel)

**The steps for one ticker:**
1. **Look up the last reported earnings date** from Yahoo, counting only dates with a
   reported EPS.
2. **Set the search window** to reach back to that report plus 7 days. It is never
   less than 50 days or more than 120, and 100 days when no date is known.
3. **Run the grounded search** for the most recent quarter's results (with the year-ago
   figures or the year-on-year change for revenue and EPS/PAT, and any beat or miss
   against published estimates), guidance, management's stated outlook, and analyst
   upgrades or downgrades. It asks for items newest first, each with its date.
4. **Run the reasoning stage to extract facts.** It returns structured JSON: earnings,
   guidance and analyst summaries; report date; EPS value; **sales and profit YoY %**
   (EPS, or PAT when EPS isn't reported); **beat/miss vs a stated consensus**; guidance
   change; analyst action and firm; outlook tone and quote; its own sentiment and
   reasoning. Its rules: no news means Unknown; the newest item wins a conflict; the report
   date is the announcement date; only a **named brokerage** counts as an analyst action
   (quant and rating sites are dropped, with a blocklist in code); a missing consensus is
   normal and is not a reason for Neutral by itself.
5. **Fill missing growth from Yahoo.** When the news gave the quarter's figures as
   absolutes ("PAT ₹32 cr") with no comparison, Yahoo's year-on-year profit (and sales)
   growth fill in, but only for the quarter announced on the report date.
6. **Decide in code** (`score_sentiment`). Sentiment is **forward-looking**. The model's
   own label is kept beside it for comparison, but this decides:

   | Signal | Weight |
   |---|---|
   | Guidance raised / lowered | ±15 (outweighs everything else together, 14) |
   | Management's quoted outlook improving / cautious | ±6 |
   | Guidance above / below analysts' consensus, as the news states it | ±3 |
   | Upgrade / downgrade by a named firm | ±3 |
   | Profit (EPS, else PAT) up / down **more than** 15% YoY | ±1 |
   | Beat / miss of a **stated** consensus | ±1 |

   - **Bullish** needs a positive total **and** at least one forward signal pointing up.
     **Bearish** is the mirror.
   - **Any forward signal decides on its own:** guidance, outlook, guidance vs consensus,
     or an analyst action. Opposite ones weigh against each other: the outlook (6) beats one
     analyst action or consensus comparison (3), but not both together.
   - **Guidance raised but still below consensus is Bullish** (the owner's call): the
     company's own revision outweighs the Street comparison.
   - **Guidance vs consensus counts only when the news itself makes the comparison**, quoted
     ("Q3 revenue guided to $54B, above the $52B consensus"). The model never works out a
     consensus on its own. It matters mostly for US names, which give numeric guidance rather
     than "raised/lowered".
   - **The reported quarter weighs much less.** With a forward signal present, it only
     breaks a tie between forward signals and can't outvote management: strong growth plus
     a beat plus a cautious outlook is **Bearish**.
   - **With no forward signal, the quarter decides**, at higher bars: profit up **more
     than 25%** YoY is **Bullish**, down **more than 20%** is **Bearish**. "No forward
     signal" includes a *steady* outlook and *maintained* guidance. A beat or miss alone
     never decides. The hover then reads "Decided by: PAT +30% YoY (no forward signal)".
   - **Sales growth** is recorded (and shown to Expert Take) but doesn't count.
   - **All three bars are settings** (Settings, items 32-34): `sentiment_profit_yoy_pct`
     (15, the tie-break), `sentiment_profit_alone_up_pct` (25) and
     `sentiment_profit_alone_down_pct` (20). All are strictly greater.
   - **Yahoo's figures are cross-checked.** When Yahoo's net-profit growth and the EPS
     growth from the statements point opposite ways, neither is used.
   - **What to watch:** management is upbeat about 4× as often as cautious, so outlook-led
     Bullish labels are the ones most exposed to a cheerful press release. Requiring
     management's own quoted words is the guard.
7. **Run a targeted retry** when the company *did* report inside the window but no
   figures were found. It makes one narrow search aimed at that announcement date, plus
   one more reasoning pass. The new result is kept only if it found hard evidence.

**The guard applied at every read.** A view is never shown raw. It is first checked by
`_validate_sentiment`:

| Flag | When | Shown as |
|---|---|---|
| STALE | the view is more than 4 days old | ⚪ Unknown (STALE) |
| STALE_QUARTER | a confirmed report exists that the cited date doesn't account for | ⚪ Unknown (QUARTER UNCONFIRMED) |
| NO_DATA | earnings, guidance and analyst text are all placeholders | ⚪ Unknown (NO DATA) |
| PARTIAL | Positive or Negative with none of: EPS, profit growth, beat/miss, guidance change, analyst action (older views) | capped to ⚖️ Neutral |
| NO_EVIDENCE | Neutral with no hard evidence | ⚖️ Neutral (no hard evidence) |
| (none) | the view passes | 🐂 Bullish / 🐻 Bearish / ⚖️ Neutral |

The cell adds a tag with the evidence, forward signals first, e.g. **Upgrade · Outlook ↑ · Profit +22%**,
**Guidance ↑**, **Upgrade** or **Beat est.**, and its hover text starts with
"Decided by: …". If the model's own call differed, the hover text says so too. A failed generation never overwrites a fresh earlier view; once the
earlier view is more than 4 days old it is replaced by an honest Unknown.

### 6.2 Expert Take (`expert_views.py`, nightly 1 AM, serial)

**The verdict is decided in code**, from the other columns, and recomputed live
(`expert_views.decide_expert_verdict`, used by the table, its filter and the alerts), so
it never disagrees with them. The owner made this change on 2026-10-04: the model's own
verdict matched this rule for about 80% of tickers, and most of the rest re-weighed the
same columns.

| Verdict | Rule |
|---|---|
| **Accumulate** | (Trend Up/Strong Up **or** Tech Uptrend Yes: one is enough) **and** TA Rules Maintain/Add or Bullish Signal **and** Sentiment not Bearish |
| **Accumulate** (breakout) | TA Rules **Bullish Signal** **and** Trend not down **and** Sentiment not Bearish. The breakout fires off converging EMAs, where Trend is usually Mixed. |
| **Caution** | **2+ points**: TA Exit **2** (the flowchart's own exit), TA Be Cautious 1, Downtrend/Strong Downtrend 1, Sentiment Bearish 1 |
| **Hold** | everything else. **Pending** only without a Trend (too little history). |

Overbought RSI is **not** a Caution point. In a trend it is strength; "add on a pullback"
goes in the trade plan.

**The news step.** The nightly AI reads the last **14 days** of material company news and
the events in the next 7 days. Results, guidance and ratings are left out, because
Sentiment decides those. It may report **one** negative event in a named category:
- fraud or accounting irregularities;
- regulatory or legal action;
- a lost major contract or customer;
- a management or auditor exit;
- dilution or a large capital raise;
- promoter or insider selling or pledging;
- a plant or operations disruption;
- a debt default or rating downgrade.

The event needs a date and the news's own words. It **lowers the verdict one step** until
it is more than 14 days old. News **never raises** the verdict: bad news can hit before
the chart reacts, while good news gets confirmed by the chart.

**What the AI writes** for the final verdict: a headline, a technical and volume note, a
catalyst note (this quarter's facts from Sentiment, plus the news), and a trade plan with
entry zone and stops. It is given the columns, key levels (EMAs, VStop, RSI, RS, volume,
52-week range, valuation, the quarter's growth as context), the alert rules currently
true (excluding rules on Expert Take itself), and the flag only if you set it by hand.

**The search** retries on another key when the model refuses ("I cannot access news from
the future"). A "nothing found" sentence is stored as no news. On 2026-10-04, 21 of 56
"news found" write-ups were really one or the other.

**What the cell shows:**
- **The badge** is the live verdict.
- **⚑** means news lowered it.
- **The hover** says what decided the verdict, then gives the write-up. It notes when the
  write-up is more than 4 days old, or was written for a different verdict than the
  columns now give.
- **Expert News?** is "Yes" when the write-up had material news or this quarter's facts
  behind it.

### 6.3 News digest (`news_summary.py`, nightly 8 PM)

**Scope.** The digest covers the watchlists in **News scope** (on the News tab).
Groups expand to their members; an empty scope means **All Invested**.

**For each ticker**, with a cache so a ticker that sits in two watchlists costs one search:
1. **Stage 1, search.** A grounded search for news in the last 24 hours plus events in
   the next 3-4 days.
2. **Stage 2, filter.** A strict recency rule (dated today or yesterday, or an upcoming
   event) and a materiality list:
   - **Keep:** earnings, M&A, regulatory approvals, major contracts, analyst actions,
     big institutional or promoter activity, ±3% moves.
   - **Drop:** routine meetings, dividends and filler.

**For each watchlist:**
3. **Stage 3, collation.** A stronger model edits the notes into one bullet per ticker.
   It does *not* re-filter them. Any ticker it drops is appended as a raw note, so
   nothing is lost.

Each ticker ends up **material**, **quiet**, **degraded** (the filter failed and the raw
text was forwarded) or **failed** (the search failed). The News tab shows those counts.
One Discord message (or more, if long) is sent per watchlist.

**If half or more of the searches fail**, the run posts **nothing** and fails, so the
slot gate retries it later; the retry posts the day's only digest. The failed run's file
is still saved, so the News tab shows the failure counts meanwhile.

A manual run with `markets` or `limit` is a **partial** smoke test: it replaces only
those watchlists' digests, posts nothing, and is titled "partial run" so it never counts
as that evening's digest.

---

## 7. The dashboard, tab by tab

### Login
- **What it checks.** `AUTH_USERNAME`/`AUTH_PASSWORD`, from secrets or
  `auth_config.json`. If neither is set, the app is open.
- **"Stay signed in".** This stores a signed 30-day token. Changing the password
  invalidates every token.

### Sidebar
- **Refresh Data.** A live Yahoo fetch of everything, with the same stale-row and
  gap guards as the job. It pushes the snapshot to the data repo.
- **⚙️ Settings.** Every calculation parameter (see §11), plus watchlist labels, fallback
  benchmarks, *Add Watchlist*, and the optional login.
- **Data age and sync status.** The data date and age, the sync status, and
  per-watchlist row counts. It warns once the snapshot is 6+ hours old.
- **Sort**, for the tab on screen only. Up to 6 levels, with the direction label suited
  to the column type: ↑/↓, A-Z, Old→New, or Top→Bottom for ranked categories. You can
  also **Copy sort from** another tab. The default sort is Index ↑, then Data Thru ↓,
  then % Chg ↓.
- **Columns to show / reorder.** Shared by every tab. Drag to reorder.
- **Category sort order.** Drag to rank each categorical column's values: Trend,
  Sentiment and so on. Every category list starts best-first.
- **Metric glossary.** Searchable. It also says when a metric can be filtered but isn't
  a table column.
- **TA Rules flowchart**, **Custom Columns**, **Ticker Notes**, and the disclaimer.

### Top of the page
A **Show fundamental columns** toggle and an **➕ Add Watchlist** popover.

### Watchlist tabs, one per watchlist in `markets.json` order

**Editing the list:**
- **Edit &lt;watchlist&gt;.** A table editor for tickers and the *Interested* box, plus
  bulk upload from `.csv` or `.txt`.
- **Validation.** New tickers are checked against Yahoo. A typo is dropped; a ticker is
  kept with a warning if Yahoo is simply not answering.
- **What Save does.** It fetches **only** the new tickers, rebuilds that watchlist's rows
  from the snapshot, and pushes to the data repo.

**Narrowing the table:**
- **Filters.** Above or below each EMA or SMA, sliders for RSI and RS, Trend, Vol Trend,
  Expert Take, and *Tech Uptrend only*.
- **Ticker search.**
- **Custom filters.** Condition chains (§8) saved per watchlist.
- **Filter by Saved Scans / Alerts.** Pick any rules and combine them with OR or AND.

**AI controls:**
- **🤖 AI Analysis Controls** has one section for Expert Take and one for Sentiment.
- In each: choose the reasoning model and thinking budget, then pick from **Re-analyze
  (selected)**, **Retry Pending** or **Re-analyze All**.
- *Re-analyze* and *Retry Pending* run right in the app.
- *Re-analyze All* starts the background workflow for just this tab's watchlists.

**The table** (raw HTML, so the header and Ticker column stay pinned while you scroll):
- **Ticker cell.** The flag or Expert Take dot, ★ if Interested, and a TradingView link.
- **Tooltips.** Hover or tap any Trend, TA Rules, Vol Trend, Tech Uptrend,
  Expert Take or Sentiment cell for the full reasoning.
- **Alerts column.** The numbers of the enabled rules currently true for that ticker,
  coloured by each rule's colour, with a legend under the table.

**Getting data out:**
- **⬇ Download table (CSV)** exports plain values: the guarded Sentiment, not the HTML.
- **🧠 Copy / download tickers for AI review** builds one markdown file for the selected
  tickers. It covers values, the per-signal breakdowns, the alerts that fired, the Expert
  Take and Sentiment text, column definitions, and how each AI verdict is guarded.

**🤖 AI Stock Expert Views** shows one card per ticker, each with a per-ticker
re-analyze button. That button refreshes Sentiment first, then Expert Take.

### Combined tabs: All Invested and All Watchlist
These are read-only merges of whichever watchlists you pick in **⚙️ Configure**, with
tickers de-duplicated. You edit tickers on the real tabs.

### News tab
This tab is rendered only while it is open.
- **Market breadth (3 or 5 years).** For the Nifty 500 and S&P 500: % of stocks above
  their 200-day SMA, and % at 52-week highs and lows.
- **Your two invested watchlists.** Each as an equal-weight, price-return curve against
  its index.
- **The news digest**, with the scope picker, model pickers, and **🔄 Refresh News**,
  which starts a full background run.

### Alert Rules tab
- **Add a rule.** Pick the scope (all, one watchlist, or one ticker), a name, a colour
  and the conditions. Then choose the **mode**: *Scheduled Discord alert* (with days)
  or *Scan only* (never pings). Then choose **what to send**: *Incremental* or *Full*.
- **Current rules.** Search (it matches names *and* the metrics a rule uses), auto-arrange
  green → red → disabled, enable or disable, reorder, duplicate (a duplicate starts
  disabled), delete, and edit every field.
- **Preview.** "What would fire right now", for the watchlists you select, with an
  optional **Send this preview to Discord**.
- **Weekly wrap-up.** Build it on demand. This is read-only and never moves the Wk
  counters. It can also be sent.
- **Discord webhook.** Save one locally, or send a test message.
- **☁️ Push config to GitHub.** A manual fallback. Every edit is already pushed
  automatically at the end of the click that made it (§9).

---

## 8. The alert rules engine (`filters.py`, `alerts.py`)

### Conditions
Each condition compares one metric with either a fixed value or another metric:

| Shape | Example | Meaning |
|---|---|---|
| metric vs value | `RSI-D > 45` | numbers, or words for yes/no fields (`Tech Uptrend == Yes`) |
| metric vs metric | `Vol 10D >= 1.4 × Vol 100D + 0` | Metric B × multiplier + offset; numeric metrics only |
| categorical "in" | `Trend in [Uptrend, Strong Uptrend]` | matches any of the picked values |
| rule reference | `Alert "early stage 2" matches` | true when that other rule is true for this ticker |

**Operators.**
- `>`, `<`, `>=`, `<=` compare as you'd expect.
- `==` allows a ±0.05 tolerance, because values are rounded to 1 decimal.
- Text comparisons ignore case.

**Chaining.** Conditions chain **left to right** with each one's own AND or OR, and there
is no operator precedence: `A AND B OR C` means `(A AND B) OR C`. An empty chain matches
everything.

**Failing closed.** A missing value fails its condition. A malformed or hand-edited
condition also just fails; it never crashes a tab or the nightly job.

**Cycles.** A cycle of rule references resolves to *false*, and you are warned about it.

### Rule fields
| Field | Values |
|---|---|
| `scope` | `ALL`, a watchlist key, or a single ticker |
| `schedule` | `scheduled` (days of the week; the time is always 9 PM ET) or `none` (scan only) |
| `notify_mode` | `incremental`: only tickers that **newly** became true (edge-triggered). `full`: every ticker matching on every due run |
| `color` | green, red or none. It colours the rule's number in the Alerts column |
| `enabled` | disabled rules aren't evaluated at night, but keep their edge state |

The rule's number in the Alerts column is its position among enabled rules that have
conditions, in file order. Auto-arrange and the ▲/▼ buttons change it.

### The nightly alert check (`alert_check.py`)
1. **Due rules.** Pick the enabled scheduled rules whose days include **the slot's
   day**. That is the day of the most recent 9 PM ET, even when the run starts after
   midnight.
2. **Fetch.** Get every watchlist, completed sessions only. Swap in stored rows where
   Yahoo's are older, then re-enrich.
3. **Coverage check.** Refuse a partial universe (exit 1, the gate retries). Log any rule
   leg that can never match: a retired metric, an empty metric, or a disabled
   referenced rule.
4. **No trustworthy state means seed only.** If `alert_state.json` is missing, torn or
   not an object, the run **records** what is currently true and **sends nothing**. That
   stops a flood of every currently-true rule.
5. **Evaluate.** Work out the rules, update `was_active` per rule and ticker, and prune
   state for deleted rules or tickers.
6. **Send.** A header, then one monospace table per rule: ticker, watchlist (for
   all-watchlist rules), and the metrics the rule uses. Messages are split under
   Discord's limit, paced, and end with the disclaimer. If the send fails, the
   newly-true keys are rolled back so the next run retries them.

### Weekly wrap-up (Sunday 9 PM)
- **What's in it.** Every **enabled rule with conditions**, scan-only rules included,
  each with its matching tickers and the metrics it uses.
- **Wk.** Weeks since that ticker *entered* that rule's list, rounded so a 6-8 day gap
  counts as one week.
- **Roll-up.** Ranks the stocks appearing in the most rules.
- **When counters move.** Only after the digest was actually delivered; the app's
  on-demand build never moves them.

---

## 9. Sync between the app and the data repo (`github_sync.py`)

**On a fresh container.** Any missing data file is downloaded before anything else
loads. If that fails, the app **stops**; it never renders empty data that a later save
could push over the real files.

**The pull, every 5 minutes.** One API call checks whether anything changed.
- **Generated files** are refreshed unless the local copy has a newer timestamp of its own.
- **Configuration** is refreshed only if you haven't edited the local copy since the
  last pull.

**What reaches the data repo, and how:**

| Action | Pushed? |
|---|---|
| Saving a watchlist; adding or renaming a watchlist; changing combined-tab members; Refresh Data | **Automatically**, one atomic commit |
| A re-analyze button | **Automatically**, only the tickers it changed, merged into the file as it is on GitHub now |
| Alert rules, custom filters, ticker notes and flags, Settings, custom columns, column layout and sort | **Automatically, at the end of the click that changed them**: every edited config file in one commit. A failed push shows a sidebar warning and is retried on your next click |

**How the app knows what changed.** The container records the blob of every file it pulls
or pushes. A config file whose local bytes no longer match that record was edited here,
and is pushed. A file with no record at all is **never** auto-pushed: without a baseline,
a local edit and a stale local copy look the same. Local and headless runs with
`SKIP_GITHUB_PULL=1` never auto-push.

**What "Push to GitHub" sends** (the manual fallback). Every configuration file, as one
commit. It includes the snapshot only if the app's copy is newer than GitHub's, and never
the two AI files wholesale.

**How jobs commit** (`.github/actions/commit-data`):
- Each attempt starts from the branch tip, re-applies only the job's own files, and
  never moves a timestamped file backwards.
- It retries up to 5 times and fails loudly if every attempt is rejected.

---

## 10. Scheduling (`.github/workflows`, `.github/actions/slot-gate`)

**How a job decides to run.** Every workflow wakes **hourly**, and a cheap `gate` job
decides whether this wake-up does the work:
1. It finds the most recent slot in New York time.
2. It skips the slot if the slot is older than the grace period: 22 h, or 10 h for
   breadth.
3. It skips the slot if it falls on a day the workflow doesn't run, such as the wrap-up
   on any day but Sunday.
4. It skips the slot if an earlier run already **completed the work job successfully**
   after the slot started.

The gate counts only a successful work job, because a run whose gate said "no" also
reports success. A manual run given `markets` or `limit` is titled **"partial run"** and
never counts, so one tab's *Re-analyze All* can't stand in for the nightly full run.

**Why it works this way.** Daylight-saving time needs no arithmetic, and a cron run that
GitHub drops or delays no longer costs a day.

| Workflow | Work | Slot | Notes |
|---|---|---|---|
| `data-refresh.yml` | `refresh_data.py` | every wake-up | no gate |
| `news-summary.yml` | `news_check.py` | 20:00 | `markets` and `limit` inputs make a partial, silent run |
| `fundamentals.yml` | `refresh_fundamentals.py` | 21:00 | `markets` and `limit` inputs |
| `daily-alerts.yml` | `alert_check.py` | 21:00 | the gate also asks whether any rule is due that day |
| `expert-views.yml` | `refresh_data.py`, then `refresh_expert_views.py` | 01:00 | `markets` and `limit` inputs |
| `market-breadth.yml` | breadth + dashboard performance | 10:00, 22:00, Mon-Sat | fails loudly if a market's breadth didn't refresh |
| `weekly-wrapup.yml` | `weekly_wrapup_check.py` | Sunday 21:00 | |
| `checks.yml` | `checks/run_all.py` (offline) | every push | needs no secrets or data |

**Every step that runs Python** in these jobs pipes its output through `log_redact.py`,
because the Actions logs are public.

---

## 11. Settings reference (`settings.json`)

| Setting | Default | Affects |
|---|---|---|
| `ema_weekly` | 10, 20, 40 | weekly EMA periods: WEMA columns, Trend, TA Rules |
| `ema_daily` | 10, 50, 200 | daily SMA periods |
| `rsi_period` | 14 | RSI-D/W/M |
| `rs_lookback_daily` / `_weekly` / `_monthly` | 63 / 26 / 12 | Mansfield RS windows |
| `vstop_length`, `vstop_factor` | 10, 2 | VStop-W |
| `vstop_mode` | `tv` | the TradingView engine, or the legacy one |
| `vstop_include_incomplete_week` | true | whether the forming week is included |
| `trend_slope_lookback` | 3 | weeks used for the 40W EMA slope |
| `trend_near_high_low_pct` | 0.10 | "Strong": within 10% of the 52W high or low |
| `trend_volume_ratio` | 1.0 | "Strong": minimum 10D/100D median volume |
| `trend_rs_neutral` / `trend_ma_neutral_pct` / `trend_slope_neutral_pct` | 1.0 / 0.5 / 0.05 | Trend's neutral bands |
| `volume_explode_ratio` / `volume_decline_ratio` | 1.4 / 0.7 | Vol Trend (median days) |
| `tech_uptrend_min_vstop_weeks` | 3 | Tech Uptrend: weeks VStop held (strictly more than this) |
| `tech_uptrend_volume_ratio` | 0.3 | Tech Uptrend volume leg (median days) |
| `ta_converge_pct`, `ta_break_pct` | 3, 3 | TA Rules: converging spread and break buffer |
| `ta_sr_lookback_weeks`, `ta_sr_pivot_weeks`, `ta_sr_reaction_pct`, `ta_sr_reaction_weeks`, `ta_sr_zone_pct`, `ta_sr_min_touches`, `ta_sr_recent_weeks` | 156, 3, 8, 8, 3, 2, 13 | support and resistance zones |
| `news_search_model`, `news_reasoning_model`, `news_reasoning_budget` | 26b, 3.5-flash-lite, 8192 | news stages 1-2 |
| `news_collation_model`, `news_collation_fallback_model`, `news_collation_thinking_budget` | 3.7-flash, 3.6-flash, 8192 | news stage 3 |
| `expert_reasoning_model`, `expert_thinking_budget` | 3.5-flash-lite, 8192 | Expert Take |
| `sentiment_reasoning_model`, `sentiment_thinking_budget`, `sentiment_targeted_retry` | 3.5-flash-lite, 8192, true | Sentiment |
| `news_watchlist_scope` | [] (= All Invested) | news digest coverage |
| `note_dropdown_options` | "" | turns the note box into a dropdown |
| `show_fundamental_columns` | true | hides the fundamental columns as a group |

Only the **calculation** settings (everything except `news_*`, `expert_*`,
`sentiment_*`, `note_*` and the display toggle) mark the snapshot out of date. Changing
one makes the app ask for **Refresh Data**.

---

## 12. When things go wrong: what you'll see

| Situation | Behaviour |
|---|---|
| Yahoo throttles the hourly refresh | Last-known rows are kept. "Data Thru" shows each row's real date |
| Yahoo throttles the alert or wrap-up job | The job fails and retries at the next wake-up, within 22 h |
| A data file is corrupt | The app stops and names the file. The jobs fail instead of writing an empty store |
| A Gemini key is revoked or out of quota | It is retired or cooled down; other keys carry on. If all fail, the fuse stops the job, previous views are kept, and the run shows red |
| The AI jobs stop running for days | After 4 days Sentiment reads ⚪ Unknown (STALE) and Expert Take reads ⚪ Pending; old verdicts aren't shown as current |
| `alert_state.json` is lost | The next run seeds state and sends nothing |
| A Discord send fails | Alerts are rolled back and retried at the next due run; wrap-up counters don't move |
| The snapshot is 6+ hours old | A sidebar warning appears |
| An edit can't be pushed to the data repo | A sidebar warning names the files; the push is retried on your next click |
| Half or more of a night's news searches fail | Nothing is posted; the run fails and the slot gate retries it |
| A rule leg can never match | The alert job logs a warning naming the rule |

---

## 13. How to…

- **Add tickers.** Open the watchlist tab, then *Edit*: add rows or bulk-upload, then
  **Save**. India tickers need `.NS` or `.BO`.
- **Add a watchlist.** Use ➕ Add Watchlist at the top, or Settings. Give it a label
  and a fallback benchmark. To order the tabs, reorder `markets.json`.
- **Make an alert.** In Alert Rules, add a rule, its conditions, the days and the
  notify mode, then **Save rule**. It is pushed to the data repo on that click, so that
  night's alert job sees it.
- **Make a scan.** Do the same as an alert but choose *Scan only*. Then use it from any
  tab under *Filter by Saved Scans / Alerts*.
- **Re-run the AI for some tickers.** In a tab, open 🤖 AI Analysis Controls, pick the
  tickers, and click *Re-analyze*. To re-run a whole watchlist in the background, use
  *Re-analyze All*.
- **Change an indicator setting.** Change it in Settings, then click **Refresh Data**.
  The setting is pushed automatically, so the hourly job uses it too. *Reset to defaults*
  resets the calculation settings only, after showing what will change.
- **Verify a code change.** Run `python3 checks/run_all.py`, then render headlessly
  (recipe in AGENTS.md, "Verifying a change") with `SKIP_GITHUB_PULL=1`.
- **Add a column.** Follow the six-place checklist in AGENTS.md, "If you add a column,
  touch all of these".

---

## 14. Glossary

- **Slot.** A job's scheduled ET time. The work is done once per slot.
- **Edge-triggered.** An alert fires when a condition goes from false to true, not on
  every day it stays true.
- **Guarded value.** An AI verdict after the deterministic checks of §6, which is what
  every table, filter and alert sees.
- **Forming bar.** Today's still-trading daily bar, or this week's still-trading weekly
  bar.
- **Snapshot.** `data_snapshot.json`: every ticker's computed row, with the settings and
  code fingerprint it was computed under.
- **Combined tab.** All Invested or All Watchlist: a read-only merge of chosen watchlists.
- **Scope (rule).** Which tickers a rule looks at: all of them, one watchlist, or one
  ticker.
- **Interested (★).** Your own marker from the watchlist editor. It can be filtered,
  sorted and used in alerts.
