-- What /admin/analytics needs that nothing recorded until now.
--
-- users.last_seen_at: when the user last loaded a page, refreshed at most once
-- an hour (app._note_seen). Sessions only record a sign-in, and a session
-- lasts 14 days, so "active users" could not be counted from them.
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_at timestamptz;
CREATE INDEX IF NOT EXISTS users_last_seen_idx ON users (last_seen_at) WHERE last_seen_at IS NOT NULL;

-- funnel_events: one row per step of a booking, so checkout conversion and
-- drop-off can be measured. Written by app._funnel, which never raises, from
-- search, the passenger and payment steps, and book()'s outcome:
--
--   search            a search that returned results (detail: how many fares)
--   passenger         an offer chosen — checkout started
--   payment           passenger details accepted, payment page shown
--   purchase_attempt  Pay pressed
--   purchased         a ticket issued (detail 'capture_failed' when the card
--                     charge did not go through afterwards)
--   purchase_failed   Pay pressed and no ticket; detail says why
--
-- offer_ref is the Duffel offer id, so a refreshed page counts once per step.
-- Starts empty: there is no history from before this migration.
CREATE TABLE IF NOT EXISTS funnel_events (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    account_id  uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    user_id     uuid REFERENCES users(id) ON DELETE SET NULL,
    step        text NOT NULL CHECK (step IN ('search', 'passenger', 'payment', 'purchase_attempt',
                                              'purchased', 'purchase_failed')),
    offer_ref   text,
    detail      text NOT NULL DEFAULT '',
    amount      numeric(12,2),
    currency    text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS funnel_events_step_ts_idx ON funnel_events (step, ts DESC);
CREATE INDEX IF NOT EXISTS funnel_events_account_idx ON funnel_events (account_id);
ALTER TABLE funnel_events ENABLE ROW LEVEL SECURITY;

-- users already has RLS on; the explicit no-op that keeps the habit intact
-- (see migration 032).
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
