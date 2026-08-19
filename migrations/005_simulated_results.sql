-- A simulated rebooking writes through to the rest of the app so the flow can
-- be demonstrated end to end — but into its OWN columns. The real `paid` and
-- `refunded` are never overwritten, so clearing a simulation restores the
-- truth exactly rather than approximately.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS simulated    boolean NOT NULL DEFAULT false;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS sim_paid     numeric(12,2);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS sim_refunded numeric(12,2);
CREATE INDEX IF NOT EXISTS orders_simulated_idx ON orders (account_id) WHERE simulated;
