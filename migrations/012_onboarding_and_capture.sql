-- Multi-step onboarding: profile fields collected in their own step, and
-- Google SSO account-linking (Supabase Auth is the OAuth broker only —
-- our own users/sessions stay the source of truth for the app itself).
ALTER TABLE users ADD COLUMN IF NOT EXISTS supabase_user_id text UNIQUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS middle_name text NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_source text NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS invite_code text NOT NULL DEFAULT '';

-- Email Capture Layer. `kind = 'forwarding'` is functional this pass;
-- `kind = 'google'` rows land as status='pending' until Google approves
-- the gmail.readonly scope — see docs/bolt-on-pivot.md.
CREATE TABLE IF NOT EXISTS email_import_sources (
    id             bigserial PRIMARY KEY,
    account_id     uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind           text NOT NULL CHECK (kind IN ('forwarding', 'google')),
    address        text NOT NULL DEFAULT '',
    status         text NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'active', 'disabled')),
    oauth_refresh_token text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_synced_at timestamptz
);
CREATE INDEX IF NOT EXISTS email_import_sources_account_idx ON email_import_sources (account_id);
