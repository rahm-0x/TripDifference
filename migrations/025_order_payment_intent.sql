-- Not covered by 024. book()'s new manual-capture Stripe sequence records
-- which authorization/charge paid for a given order — needed to trace a
-- purchase to its Stripe side without a live API call, the same reasoning
-- accounts.stripe_payment_method_id already gets cached for. Existing 10
-- orders backfill NULL — none of them were charged via Stripe; they were
-- booked before this phase, entirely off TD's Duffel balance.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS stripe_payment_intent_id text;
