-- Bolt-on pivot, step one: `orders` stops being exclusively "a ticket
-- TripDifference bought". `source` records how the reservation actually
-- entered the system. Every pre-pivot row was a real TD-funded Duffel
-- purchase — that is the honest default for the backfill, not a guess.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'td_rebook'
    CHECK (source IN ('email_import', 'manual', 'forward', 'td_rebook'));

-- SavingsEvent: which delivery path actually fired on a given execution, so
-- the UI can say "refunded to your card" or "credited to your airline
-- account" and mean it, rather than a single generic message. Deliberately
-- separate from execution_attempts (the idempotency guard for the Duffel
-- call) — this is the customer/commission-facing fact the call produced.
--
-- commission_rate is stored per-row rather than read from a global constant
-- so a future PRO tier (different rate per account) doesn't have to fight
-- this table later.
CREATE TABLE IF NOT EXISTS savings_events (
    id                     bigserial PRIMARY KEY,
    order_id               text NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    execution_attempt_id   bigint REFERENCES execution_attempts(id) ON DELETE SET NULL,
    old_amount             numeric(12,2) NOT NULL,
    new_amount             numeric(12,2) NOT NULL,
    realized_savings       numeric(12,2) NOT NULL,
    currency               text NOT NULL DEFAULT '',
    delivery_type          text NOT NULL CHECK (delivery_type IN ('refund_to_card', 'airline_credit')),
    delivery_detail        text NOT NULL DEFAULT '',
    commission_rate        numeric(5,4) NOT NULL DEFAULT 0.25,
    commission_amount      numeric(12,2) NOT NULL,
    -- NULL until a real charge happens. No Stripe integration exists yet, so
    -- this stays NULL rather than claiming a commission was charged that
    -- wasn't — see business rule: bill only after status = completed, never
    -- speculatively.
    commission_charged_at  timestamptz,
    status                 text NOT NULL DEFAULT 'completed'
                            CHECK (status IN ('pending', 'completed', 'failed', 'disputed')),
    created_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS savings_events_order_idx ON savings_events (order_id, created_at DESC);
