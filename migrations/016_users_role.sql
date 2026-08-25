-- Backfill: every existing user is currently their own account's sole
-- owner, so 'admin' is the honest default, not a guess. Also the default
-- for NEW rows for now — db.create_account() isn't invite-aware yet (that
-- lands with the accept-invitation flow in a later phase), so every
-- signup between now and then is still a fresh solo account and 'admin'
-- remains correct for it too.
ALTER TABLE users ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'admin'
    CHECK (role IN ('admin','booker','approver','finance'));
