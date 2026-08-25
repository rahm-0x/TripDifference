-- Grouping and spend attribution. A table, not a free-text column on
-- orders, because the portal needs budget context ("Q3 budget: $195,000")
-- and a real code/name pair per cost centre, not a string.
CREATE TABLE IF NOT EXISTS cost_centers (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id     uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    code           text NOT NULL,
    name           text NOT NULL,
    budget_amount  numeric(12,2),
    budget_period  text CHECK (budget_period IN ('monthly','quarterly','annual')),
    active         boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, code)
);
CREATE INDEX IF NOT EXISTS cost_centers_account_idx ON cost_centers (account_id);
ALTER TABLE cost_centers ENABLE ROW LEVEL SECURITY;

-- Every order must be attributable eventually; the existing 10 rows and 2
-- travelers backfill NULL — no cost centre exists yet to assign them to.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS cost_center_id uuid
    REFERENCES cost_centers(id) ON DELETE SET NULL;
ALTER TABLE travelers ADD COLUMN IF NOT EXISTS default_cost_center_id uuid
    REFERENCES cost_centers(id) ON DELETE SET NULL;
