-- Which Stripe mode an account's saved customer and card belong to.
--
-- A Stripe customer or payment method exists in one mode only: an id saved
-- with a test key does not exist under a live key, and the reverse. Every key
-- this app has run on so far was a test key, so nothing recorded the mode.
--
-- Staging can now be told to charge real cards (STRIPE_LIVE_ENABLED,
-- billing.startup_check). An account that saved a test card before that
-- switch would still look as if it had a card on file, and then fail at its
-- first booking with "No such customer".
--
-- db.account_stripe_ids and db.account_card read only ids of the mode the
-- deployment's key is in. billing.ensure_customer creates a customer when
-- there is none in that mode, which replaces the ids and clears the card
-- (db.account_set_stripe_customer) — so switching modes costs an account its
-- saved card, once, and it adds one again on the card page.
--
-- Every existing row was saved under a test key, so false is the backfill.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS stripe_livemode boolean NOT NULL DEFAULT false;

-- accounts already has RLS on; the explicit no-op that keeps the habit
-- intact (see migration 032).
ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
