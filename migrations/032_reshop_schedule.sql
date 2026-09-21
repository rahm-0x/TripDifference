-- Scheduler cursor for the reshop cron (app.py /cron/reshop).
--
-- A cron invocation has ~60s (vercel.json maxDuration) and a single live
-- market search measures 9-12s, so only a few orders fit per run. The cron
-- takes the least-recently-checked monitored orders first and stamps each one
-- as it goes, so the next invocation continues where this one stopped rather
-- than starving everything after the first few.
--
-- NULL means never checked, and sorts first: a newly booked order is looked at
-- before one checked an hour ago.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS last_checked_at timestamptz;

-- The cron's own query: monitored orders, oldest check first. Partial, because
-- nothing else ever asks this question and most rows are not monitored.
CREATE INDEX IF NOT EXISTS orders_last_checked_at_idx
    ON orders (last_checked_at NULLS FIRST)
    WHERE monitoring;

-- Migration 014 retroactively enabled RLS and changed this project's default
-- privileges, but that only covers grants — every migration since has had to
-- declare RLS itself, and there is no event trigger doing it. orders already
-- has RLS on; this is the explicit no-op that keeps the habit intact, since
-- forgetting it on a genuinely new table is silent and untested for.
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
