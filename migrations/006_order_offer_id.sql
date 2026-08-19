-- Which offer produced this order. Duffel offer requests are single use, so a
-- double-submitted booking gets a 422 back — this lets us answer that with the
-- booking the customer already has, instead of an error page implying failure.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS offer_id text;
CREATE INDEX IF NOT EXISTS orders_offer_idx ON orders (account_id, offer_id);
