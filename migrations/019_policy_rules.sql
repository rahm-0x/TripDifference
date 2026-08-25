-- Per-organization travel policy. `value` is jsonb rather than a typed
-- column because the value shape varies by rule_type (a cabin cap is a
-- string, a price ceiling a number, an advance-purchase minimum a day
-- count, a carrier restriction a list) — one polymorphic column beats four
-- mostly-null typed ones, matching how sim_scenario/last_decision already
-- store rule-shaped data untyped elsewhere in this schema.
--
-- `enforcement` is what connects policy to approvals: a rule doesn't just
-- pass or fail, it decides whether a booking proceeds, needs an approver,
-- or is refused outright. Evaluated advisory at /search, hard at book().
--
-- `ancillary_protection` is in the CHECK from day one, not added later:
-- Duffel confirmed seats and bags may transfer, require re-selection, or
-- be refunded on an exchange, depending on carrier — costing a traveler
-- their paid seat to recover a small fare difference is a bad trade a
-- policy needs to be able to forbid.
CREATE TABLE IF NOT EXISTS policy_rules (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id   uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    rule_type    text NOT NULL CHECK (rule_type IN
                 ('cabin_cap','price_ceiling','advance_purchase',
                  'carrier_restriction','ancillary_protection')),
    scope        jsonb NOT NULL DEFAULT '{}'::jsonb,
    value        jsonb NOT NULL,
    enforcement  text NOT NULL CHECK (enforcement IN ('advise','require_approval','block')),
    active       boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS policy_rules_account_idx ON policy_rules (account_id) WHERE active;
ALTER TABLE policy_rules ENABLE ROW LEVEL SECURITY;
