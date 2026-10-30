-- The research library's kg_breadth table: the drawdown breadth columns (2026-10-30), the share of a series'
-- stocks at least 10 / 20 / 30 / 40% below their 52-week high on each day (publish_market.py watchlists).
-- Run in the Supabase SQL editor as the table's owner: the publishing role (market_writer) can write rows
-- but cannot alter the table. Its table-level grant covers the new columns. Until a column exists, the job
-- writes the others and logs that the drawdown columns are missing; the next run after it is added writes
-- every series in full once (it counts the least-filled drawdown column).
ALTER TABLE public.kg_breadth
  ADD COLUMN IF NOT EXISTS pct_down_10 real,
  ADD COLUMN IF NOT EXISTS pct_down_20 real,
  ADD COLUMN IF NOT EXISTS pct_down_30 real,
  ADD COLUMN IF NOT EXISTS pct_down_40 real;

-- The 50% level became 40% the same day (owner, 2026-10-30): nothing writes or reads pct_down_50 any more.
ALTER TABLE public.kg_breadth DROP COLUMN IF EXISTS pct_down_50;
