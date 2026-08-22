-- Settings' "General Information" panel needs an account-level display name
-- distinct from any Traveler's given/family name.
ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname text NOT NULL DEFAULT '';
