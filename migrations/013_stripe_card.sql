-- Card-on-file. brand/last4/exp are cached here from Stripe at save time so
-- card-gating checks (eligibility.assess's has_card) and every template that
-- displays the card never need a live Stripe call.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS stripe_customer_id text;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS stripe_payment_method_id text;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS card_brand text NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS card_last4 text NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS card_exp_month smallint;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS card_exp_year smallint;
