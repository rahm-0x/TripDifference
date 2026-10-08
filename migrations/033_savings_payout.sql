-- Passing a cash recovery on to the company's card.
--
-- TD buys every ticket from its own Duffel balance and charges the company's
-- card separately through Stripe, so when an exchange or a cancellation
-- returns cash, Duffel returns it to TD's balance — it has never heard of the
-- company's card. Until now savings_events recorded 'refund_to_card' and
-- nothing refunded the card. billing.refund_to_card() now does, against the
-- order's own PaymentIntent, and these columns are the record of it.
--
-- stripe_refund_id is NULL until the refund is issued. payout_failed_at is
-- NULL in the common case and set only when the refund could not be issued —
-- the saving was recovered and is still owed — with payout_error saying why.
-- A fact with a timestamp, the same shape as orders.payment_capture_failed_at
-- (migration 026): the exchange has already landed and is never undone
-- because paying it out hiccuped.
ALTER TABLE savings_events ADD COLUMN IF NOT EXISTS stripe_refund_id text;
ALTER TABLE savings_events ADD COLUMN IF NOT EXISTS payout_failed_at timestamptz;
ALTER TABLE savings_events ADD COLUMN IF NOT EXISTS payout_error text;

-- savings_events already has RLS on; the explicit no-op that keeps the habit
-- intact (see migration 032).
ALTER TABLE savings_events ENABLE ROW LEVEL SECURITY;
