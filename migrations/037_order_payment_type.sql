-- How each order was paid for at Duffel: from TD's Duffel balance, or with
-- the customer's card (Duffel card payments — not built yet).
--
-- Duffel says where a refund went in refund_to, and its
-- original_form_of_payment means "the form of payment used to create the
-- order". On a balance-paid order that is TD's balance, and the app passes the
-- money on to the card through Stripe. On a card-paid order the airline
-- refunds the card itself, and passing it on as well would pay the customer
-- twice. Nothing recorded which kind an order was; app._refund_lands_as reads
-- it from here.
--
-- Set at creation (app.book()). An exchange keeps the order and this value:
-- original_form_of_payment refers to how the order was created, not to how a
-- later change was paid. Every order so far was paid from the balance.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS duffel_payment_type text NOT NULL DEFAULT 'balance';

ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_duffel_payment_type_check;
ALTER TABLE orders ADD CONSTRAINT orders_duffel_payment_type_check
    CHECK (duffel_payment_type IN ('balance', 'card'));

-- The default has already filled every existing row; stated outright so the
-- backfill does not rest on it.
UPDATE orders SET duffel_payment_type = 'balance' WHERE duffel_payment_type <> 'balance';

-- orders already has RLS on; the explicit no-op that keeps the habit intact
-- (see migration 032).
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
