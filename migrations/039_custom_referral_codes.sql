-- Custom referral links: a scout can choose their own code (/r/acme-travel)
-- in Settings, alongside the SCOUT-XXXXX one issued at approval.
--
-- Every code a scout has had keeps working — anything they already shared
-- still credits them — so a change adds a code rather than replacing one.
-- `chosen_at` says which is the one to share now: the dashboard shows the
-- active code chosen most recently, and choosing an older one of their own
-- again just moves its chosen_at. `custom` marks the codes a scout picked, which
-- is what the per-scout limit counts (db.scout_choose_code).
ALTER TABLE referral_codes ADD COLUMN IF NOT EXISTS custom boolean NOT NULL DEFAULT false;
ALTER TABLE referral_codes ADD COLUMN IF NOT EXISTS chosen_at timestamptz;
UPDATE referral_codes SET chosen_at = created_at WHERE chosen_at IS NULL;
ALTER TABLE referral_codes ALTER COLUMN chosen_at SET DEFAULT now();
ALTER TABLE referral_codes ALTER COLUMN chosen_at SET NOT NULL;

-- referral_codes already has RLS on; the explicit no-op that keeps the habit
-- intact (see migration 032).
ALTER TABLE referral_codes ENABLE ROW LEVEL SECURITY;
