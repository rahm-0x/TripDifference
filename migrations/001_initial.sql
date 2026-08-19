-- ---------------------------------------------------------------------------
-- Phase 1 foundations: identity, durable order state, append-only audit,
-- and idempotency for the one call that moves money.
--
-- Money is NUMERIC(12,2). Duffel returns amounts as strings like "221.51";
-- str(Decimal) round-trips those unchanged, so templates render identically.
-- The Duffel payload itself stays in `raw` jsonb as the source of truth.
-- ---------------------------------------------------------------------------

-- Extensions this schema depends on. gen_random_uuid() is core from PG13.
CREATE EXTENSION IF NOT EXISTS citext;

-- --- identity --------------------------------------------------------------

CREATE TABLE IF NOT EXISTS accounts (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id     uuid        NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    email          citext      NOT NULL,
    password_hash  text,           -- NULL until the invite is activated
    given_name     text        NOT NULL DEFAULT '',
    family_name    text        NOT NULL DEFAULT '',
    title          text        NOT NULL DEFAULT '',
    born_on        date,
    gender         text        NOT NULL DEFAULT '',
    phone_number   text        NOT NULL DEFAULT '',
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (email)
);

-- Opaque session tokens. We store only a SHA-256 of the cookie value, so a
-- database leak does not hand over live sessions.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  bytea PRIMARY KEY,
    user_id     uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    revoked_at  timestamptz
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions (user_id);

-- --- orders ----------------------------------------------------------------

CREATE TABLE IF NOT EXISTS orders (
    order_id           text PRIMARY KEY,          -- Duffel's ord_...
    account_id         uuid        NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    booking_reference  text        NOT NULL DEFAULT '',
    route              text        NOT NULL DEFAULT '',
    itinerary          text        NOT NULL DEFAULT '',
    carrier            text        NOT NULL DEFAULT '',
    departure_date     date,
    paid               numeric(12,2),
    original_paid      numeric(12,2),             -- set when a rebooking refunded
    refunded           numeric(12,2),
    currency           text        NOT NULL DEFAULT '',
    monitoring         boolean     NOT NULL DEFAULT false,
    executed           text,                      -- human note from the last execution
    raw                jsonb       NOT NULL,      -- the Duffel order, verbatim
    last_decision      jsonb,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS orders_account_idx    ON orders (account_id);
CREATE INDEX IF NOT EXISTS orders_monitoring_idx ON orders (monitoring) WHERE monitoring;

-- --- audit trail -----------------------------------------------------------

-- Replaces decisions.log. One row per event, same envelope the JSONL had:
-- `payload` is the verbatim dict the templates already render, and the
-- promoted columns exist only so the ops views can filter and sort.
--
-- decision / eligibility / execution stay distinct kinds — a blocked exchange
-- must never be folded into the decision that preceded it.
CREATE TABLE IF NOT EXISTS audit_events (
    id            bigserial PRIMARY KEY,
    ts            timestamptz NOT NULL DEFAULT now(),
    kind          text        NOT NULL CHECK (kind IN ('decision','eligibility','execution')),
    order_id      text        NOT NULL,
    source        text        NOT NULL DEFAULT '',
    outcome       text,
    reason        text,
    execution     text,
    market_best   numeric(12,2),
    market_delta  numeric(12,2),
    change_total  numeric(12,2),
    currency      text        NOT NULL DEFAULT '',
    detail        text        NOT NULL DEFAULT '',
    payload       jsonb       NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_ts_idx    ON audit_events (ts DESC);
CREATE INDEX IF NOT EXISTS audit_order_idx ON audit_events (order_id, ts DESC);

-- Append-only, enforced by the database rather than by convention.
CREATE OR REPLACE FUNCTION audit_events_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (attempted %)', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_events_no_mutate ON audit_events;
CREATE TRIGGER audit_events_no_mutate
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_immutable();

-- --- idempotency -----------------------------------------------------------

-- Guards the only calls that move money: confirming an exchange or a
-- cancellation against Duffel. The unique constraint is the guarantee —
-- a duplicate submit loses the INSERT race and never reaches Duffel.
--
-- change_offer_id is NOT NULL DEFAULT '' on purpose: NULLs do not collide in
-- a unique index, which would defeat the whole point for cancellations.
CREATE TABLE IF NOT EXISTS execution_attempts (
    id               bigserial PRIMARY KEY,
    order_id         text        NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    action           text        NOT NULL CHECK (action IN ('exchange','cancel')),
    change_offer_id  text        NOT NULL DEFAULT '',
    status           text        NOT NULL DEFAULT 'in_progress'
                                 CHECK (status IN ('in_progress','succeeded','failed')),
    duffel_change_id text,
    note             text,
    result           jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    UNIQUE (order_id, action, change_offer_id)
);
CREATE INDEX IF NOT EXISTS exec_order_idx ON execution_attempts (order_id, created_at DESC);
