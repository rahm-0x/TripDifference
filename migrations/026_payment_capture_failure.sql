-- A capture failure after a successful Duffel order used to be
-- unrecoverable: the order was never persisted, so nothing in the system
-- knew a real ticket existed. book() now persists the order before
-- attempting capture and retries once; if that also fails, these two
-- columns are what make the problem visible instead of silent.
-- payment_capture_failed_at is NULL in the overwhelming common case
-- (captured fine) and only set to flag an order needing manual payment
-- resolution -- no order status column, no state machine, just a fact
-- with a timestamp, matching commission_charged_at/decided_at elsewhere.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_capture_failed_at timestamptz;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_capture_error text;
