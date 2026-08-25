-- A liability register, not a log. Credit from an exchange or cancellation
-- sits in an employee's own loyalty account and leaves with them if they
-- quit — it has to be queryable by expiry so the portal can surface what's
-- about to evaporate, not just recorded for history.
CREATE TABLE IF NOT EXISTS airline_credits (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id                uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    traveler_id               uuid REFERENCES travelers(id) ON DELETE SET NULL,
    airline                   text NOT NULL,
    loyalty_account_reference text NOT NULL DEFAULT '',
    order_id                  text REFERENCES orders(order_id) ON DELETE SET NULL,
    savings_event_id          bigint REFERENCES savings_events(id) ON DELETE SET NULL,
    amount_issued             numeric(12,2) NOT NULL,
    amount_remaining          numeric(12,2) NOT NULL,
    currency                  text NOT NULL,
    issued_at                 timestamptz NOT NULL DEFAULT now(),
    expires_at                timestamptz,
    status                    text NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active','redeemed','expired','forfeited')),
    created_at                timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS airline_credits_account_idx ON airline_credits (account_id);
-- What's about to evaporate: active credits ordered by expiry, cheaply.
CREATE INDEX IF NOT EXISTS airline_credits_expiring_idx
    ON airline_credits (expires_at) WHERE status = 'active';
ALTER TABLE airline_credits ENABLE ROW LEVEL SECURITY;
