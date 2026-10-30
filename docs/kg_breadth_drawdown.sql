-- The research library's kg_breadth table: the drawdown breadth columns (2026-10-30), the share of a series'
-- stocks at least 10 / 20 / 30 / 50% below their 52-week high on each day (publish_market.py watchlists).
-- Run once in the Supabase SQL editor as the table's owner: the publishing role (market_writer) can write rows
-- but cannot alter the table. Its table-level grant covers the new columns. Until this runs, the job writes
-- the other columns and logs that these are missing; the next run after it writes every series in full once.
ALTER TABLE public.kg_breadth
  ADD COLUMN IF NOT EXISTS pct_down_10 real,
  ADD COLUMN IF NOT EXISTS pct_down_20 real,
  ADD COLUMN IF NOT EXISTS pct_down_30 real,
  ADD COLUMN IF NOT EXISTS pct_down_50 real;
