-- Observation log for validation/observe.py: one row per live order per run
-- of engine.evaluate() against the real Duffel price source. Records
-- change_total_amount even on a decision that skipped for an unrelated
-- reason downstream, so a +40 quote and a +400 quote both survive as data —
-- this table exists to answer "how often does change_total_amount go
-- negative on real orders", not to re-decide anything.  Nothing that writes
-- here ever executes an exchange; see validation/observe.py's own docstring
-- and its safety-interlock test.
--
-- Append-only, enforced by the database rather than by convention, same
-- pattern as audit_events (migration 001).
CREATE TABLE IF NOT EXISTS reshop_observations (
    id                  bigserial PRIMARY KEY,
    observed_at         timestamptz NOT NULL DEFAULT now(),
    order_id            text REFERENCES orders(order_id) ON DELETE SET NULL,
    carrier             text NOT NULL DEFAULT '',
    route               text NOT NULL DEFAULT '',
    days_to_departure   integer,
    original_fare       numeric(12,2),
    currency            text NOT NULL DEFAULT '',
    change_total_amount numeric(12,2),   -- NULL until engine.evaluate() reaches the pricing gate
    market_best         numeric(12,2),
    market_delta        numeric(12,2),
    -- Position (1-10) in engine.evaluate()'s fixed gate sequence that this
    -- decision stopped at — see validation/observe.py's GATE_BY_REASON.
    -- Not a column engine.py exposes; derived read-only from Decision.reason.
    gate                smallint NOT NULL,
    skip_reason         text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS reshop_observations_carrier_idx      ON reshop_observations (carrier);
CREATE INDEX IF NOT EXISTS reshop_observations_gate_idx         ON reshop_observations (gate);
CREATE INDEX IF NOT EXISTS reshop_observations_observed_at_idx  ON reshop_observations (observed_at DESC);
ALTER TABLE reshop_observations ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION reshop_observations_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'reshop_observations is append-only (attempted %)', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS reshop_observations_no_mutate ON reshop_observations;
CREATE TRIGGER reshop_observations_no_mutate
    BEFORE UPDATE OR DELETE ON reshop_observations
    FOR EACH ROW EXECUTE FUNCTION reshop_observations_immutable();
