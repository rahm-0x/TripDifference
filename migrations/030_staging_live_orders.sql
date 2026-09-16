-- Staging live orders: a staging deploy (APP_ENV=staging,
-- DUFFEL_LIVE_ORDERS_ENABLED=true) may book real Duffel tickets. See live_guard.py
-- and duffel_http.check_token. Production stays sandbox-only; this migration
-- is safe to apply there too (every existing order becomes duffel_mode='test').

-- --- which Duffel mode bought the ticket --------------------------------------
-- Set once at creation from the token (app.py:book). Never changes: flipping a
-- live order to 'test' would dodge the delete block below.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS duffel_mode text NOT NULL DEFAULT 'test'
    CHECK (duffel_mode IN ('test', 'live'));

CREATE OR REPLACE FUNCTION orders_live_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.duffel_mode = 'live' THEN
            RAISE EXCEPTION 'order % is a live Duffel order and cannot be deleted', OLD.order_id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.duffel_mode IS DISTINCT FROM OLD.duffel_mode THEN
        RAISE EXCEPTION 'orders.duffel_mode is set at creation and cannot change (order %: % -> %)',
            OLD.order_id, OLD.duffel_mode, NEW.duffel_mode
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS orders_live_guard ON orders;
CREATE TRIGGER orders_live_guard
    BEFORE UPDATE OF duffel_mode OR DELETE ON orders
    FOR EACH ROW EXECUTE FUNCTION orders_live_guard();

-- An account that owns a live order can't be deleted either — refused here,
-- by name, before the ON DELETE CASCADE to orders would hit the order-level
-- block with a less obvious message.
CREATE OR REPLACE FUNCTION accounts_live_guard() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM orders WHERE account_id = OLD.id AND duffel_mode = 'live') THEN
        RAISE EXCEPTION 'account % owns live Duffel orders and cannot be deleted', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS accounts_live_guard ON accounts;
CREATE TRIGGER accounts_live_guard
    BEFORE DELETE ON accounts
    FOR EACH ROW EXECUTE FUNCTION accounts_live_guard();

-- --- reshop_observations keep their order ---------------------------------------
-- Was ON DELETE SET NULL, which is an UPDATE the table's append-only trigger
-- refuses — deleting an observed order failed with a trigger error. RESTRICT
-- makes the refusal an ordinary foreign-key violation naming the constraint.
ALTER TABLE reshop_observations DROP CONSTRAINT IF EXISTS reshop_observations_order_id_fkey;
ALTER TABLE reshop_observations ADD CONSTRAINT reshop_observations_order_id_fkey
    FOREIGN KEY (order_id) REFERENCES orders(order_id) ON DELETE RESTRICT;

-- --- rejected live spends are audited --------------------------------------------
ALTER TABLE audit_events DROP CONSTRAINT IF EXISTS audit_events_kind_check;
ALTER TABLE audit_events ADD CONSTRAINT audit_events_kind_check
    CHECK (kind IN ('decision', 'eligibility', 'execution', 'live_guard'));

-- --- the live-spend ledger ------------------------------------------------------------
-- One row per live booking or live exchange top-up, reserved before Duffel is
-- asked to move money (live_guard.reserve) and settled or released after.
-- STAGING_MAX_DAILY_USD sums today's non-released rows. Test-mode orders
-- never write here.
CREATE TABLE IF NOT EXISTS live_spend (
    id          bigserial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    kind        text NOT NULL CHECK (kind IN ('booking', 'exchange_topup')),
    status      text NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved', 'spent', 'released')),
    account_id  uuid NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    order_id    text REFERENCES orders(order_id) ON DELETE RESTRICT,
    amount      numeric(12,2) NOT NULL CHECK (amount > 0),
    currency    text NOT NULL,
    reference   text NOT NULL DEFAULT '',   -- offer id (booking) or order change id (top-up)
    settled_at  timestamptz
);
CREATE INDEX IF NOT EXISTS live_spend_created_at_idx ON live_spend (created_at);
ALTER TABLE live_spend ENABLE ROW LEVEL SECURITY;
