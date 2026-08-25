-- Splits "the person who signed up" (users.given_name/family_name — already
-- exists, migration 001/012) from "the company" (accounts). accounts.name
-- already means "company display name" everywhere it's read
-- (auth.view_model's `company`, sourced from db.session_user's `a.name AS
-- company`) but db.complete_profile() currently stomps it with the
-- signer's own name every time someone finishes onboarding — fixed
-- alongside this migration by deleting that line in db.py, not by SQL.
-- No template currently renders accounts.name/user.company (checked), so
-- that fix has no visible effect today.
--
-- No backfill of real values into the new columns: existing accounts keep
-- whatever accounts.name they already have (a mix of real company names
-- and leftover person-names from the bug above — not invented, not
-- touched), and every new company field starts NULL, prompted for later.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS legal_name text;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS employee_count integer;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS billing_address_line1 text;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS billing_address_line2 text;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS billing_city text;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS billing_state text;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS billing_postal_code text;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS billing_country text;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS subscription_fee numeric(12,2);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS commission_rate numeric(5,4) NOT NULL DEFAULT 0.25;
