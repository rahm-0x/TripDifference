-- Collecting the commission on a rebooking.
--
-- savings_events has carried commission_amount since migration 008 and a
-- commission_charged_at that was "NULL until a real charge happens" — and no
-- charge ever did: the fee was computed, shown as "billed on your next
-- invoice", optionally put on an invoice, and never collected.
--
-- It is now charged to the account's card on file as soon as a rebooking has
-- delivered its saving (billing.charge_commission, app._charge_commission).
-- commission_charged_at is set when that charge succeeds, and these columns
-- are the rest of the record:
--
--   commission_payment_intent_id   the Stripe charge, for reconciliation
--   commission_charge_failed_at    set only when the charge could not be made;
--   commission_charge_error        the fee is still owed, and says why
--
-- A fee that was not charged (failed, or the saving has not reached the
-- customer yet) stays invoiceable; one that was charged never is
-- (db.generate_invoice), so nothing is collected twice.
ALTER TABLE savings_events ADD COLUMN IF NOT EXISTS commission_payment_intent_id text;
ALTER TABLE savings_events ADD COLUMN IF NOT EXISTS commission_charge_failed_at timestamptz;
ALTER TABLE savings_events ADD COLUMN IF NOT EXISTS commission_charge_error text;

-- savings_events already has RLS on; the explicit no-op that keeps the habit
-- intact (see migration 032).
ALTER TABLE savings_events ENABLE ROW LEVEL SECURITY;
