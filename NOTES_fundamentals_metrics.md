# Fundamentals metrics — exploration notes (to revisit)

Context: exploring adding screener.in-style fundamental ratios to the watchlist app.
Checked what's actually available from yfinance for a live Indian ticker (RELIANCE.NS)
before writing this up. Not yet built — just scoping for when we pick this up.

## Tier 1 — available directly from yfinance (`.info` dict, no computation)

- Market Cap
- Current Price
- 52W High / Low (already in the app)
- Stock P/E (`trailingPE`)
- Book Value (`bookValue`)
- Dividend Yield (`dividendYield`)
- Debt to Equity (`debtToEquity`)
- EV/EBITDA (`enterpriseToEbitda`)

PEG Ratio (`pegRatio`) and Price/Sales (`priceToSalesTrailing12Months`) are technically
`.info` fields too, but came back missing for the test ticker — need the Tier 2 fallback
computation for these in practice, not a reliable single-field pull.

## Tier 2 — computable from yfinance's financial statements

Needs `.balance_sheet`, `.financials`, `.cashflow`, and the quarterly equivalents.
yfinance typically returns ~5 fiscal years of annual data and ~5 quarters, with
occasional gaps (confirmed: quarterly income statement returned 2026-06-30, 2026-03-31,
2025-12-31, 2025-06-30, 2025-03-31 — Sept 2025 quarter was missing for this ticker).

- ROE, ROCE — compute from income statement ÷ balance sheet lines. More reliable
  computed this way than via `.info["returnOnEquity"]`, which was missing outright
  for the test ticker.
- ROCE 3Yr, ROE 3Yr — 3-year average of the above.
- Market Cap to Sales — `marketCap / Total Revenue`.
- Qtr Profit Var, Qtr Sales Var — QoQ % change from quarterly income statement.
- Sales growth 5Years, EPS growth 5Years — CAGR from annual statements, capped by
  however many years yfinance actually returns for that ticker (sometimes fewer than 5).
- OPM latest quarter — Operating Income / Revenue from quarterly income statement.
- CFO to OP — Operating Cash Flow / Operating Profit.
- Net debt to CFO — `(Total Debt − Cash) / CFO`.
- WC to Sales — Working Capital / Revenue (Working Capital is a direct balance-sheet
  line item, no need to derive it from current assets/liabilities separately).
- Free Cash Flow 3Yrs — "Free Cash Flow" is also a direct cashflow-statement line item;
  just sum the last 3 fiscal years.
- Price to Cash Flow — `Market Cap / Operating Cash Flow`.

## Tier 3 — not really available via yfinance

- **Face Value** — India-specific par-value concept tied to share certificates (₹1, ₹2,
  ₹10, etc). Yahoo Finance doesn't track this at all. Would need an NSE/BSE-specific
  data source, not yfinance.
- **5Yrs PE** — a 5-year rolling average of price/earnings. Doable in theory by combining
  historical daily prices with historical EPS, but fiddly to align correctly, and
  screener.in's exact averaging methodology isn't published — a computed version likely
  wouldn't match their displayed number precisely.

## Bottom line

Roughly 8 of the 26 screener.in metrics shown are free (Tier 1), another ~13 are a
genuine "pull the statements and do the math" job (Tier 2), and 2 are out of reach
without a different data source (Tier 3).

## Next steps (when we pick this up)

- Decide which Tier 2 metrics are worth building first (probably ROE/ROCE, OPM, and the
  growth/variance metrics are highest value).
- Decide whether this becomes its own "Fundamentals" tab/section, or gets folded into
  the existing custom columns feature (Tier 1 fields could plausibly be exposed as
  built-in metrics available to custom column formulas, same as `week52_high` etc).
- Note: statement-based (Tier 2) metrics require an extra yfinance call per ticker
  (`.balance_sheet` / `.financials` / `.cashflow`, and quarterly versions) beyond what
  `fetch_all_markets()` currently pulls — will add real latency across the full 73-ticker
  watchlist, worth checking rate limits / considering caching before building.
