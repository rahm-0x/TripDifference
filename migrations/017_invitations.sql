-- What makes many-users-per-account real. users.account_id has always
-- permitted more than one user per account structurally; nothing has ever
-- created a second one. token_hash follows sessions' own pattern — store
-- the hash, mail the raw token, a DB leak yields nothing usable.
CREATE TABLE IF NOT EXISTS invitations (
    id           bigserial PRIMARY KEY,
    account_id   uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    email        citext NOT NULL,
    role         text NOT NULL CHECK (role IN ('admin','booker','approver','finance')),
    token_hash   bytea NOT NULL UNIQUE,
    invited_by   uuid REFERENCES users(id) ON DELETE SET NULL,
    expires_at   timestamptz NOT NULL,
    accepted_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS invitations_account_idx ON invitations (account_id);
ALTER TABLE invitations ENABLE ROW LEVEL SECURITY;
