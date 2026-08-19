-- Passenger profiles a booker fills the flow with. These are people you book
-- FOR, not people who sign in — logins live in `users`.
CREATE TABLE IF NOT EXISTS travelers (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id    uuid        NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    title         text        NOT NULL DEFAULT '',
    given_name    text        NOT NULL,
    family_name   text        NOT NULL,
    born_on       date,
    gender        text        NOT NULL DEFAULT '',
    email         citext      NOT NULL DEFAULT '',
    phone_number  text        NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS travelers_account_idx ON travelers (account_id, family_name, given_name);
