-- A booking someone has asked for but not yet bought — the state
-- `orders` structurally cannot represent, since orders.order_id is
-- Duffel's own ord_... id and only exists after a real purchase.
--
-- Own uuid PK, fully decoupled from Duffel's id space. order_id is
-- nullable and is set exactly once, at the moment a real Duffel order is
-- created from this request — never a synthetic/placeholder value.
--
-- Duffel offer requests are single-use and offers expire, so a request
-- that sits overnight awaiting approval cannot hold a live offer to
-- purchase later. itinerary_snapshot carries enough fidelity to re-find
-- the same flights at purchase time (carrier, flight numbers, dates,
-- times, cabin, origin/destination per segment) and amount is the
-- approved price ceiling, not a durable offer_id.
--
-- 'purchase_failed' covers the case where, at purchase time, the flight
-- is gone entirely or the re-priced fare has moved above the approved
-- ceiling — an approved request that cannot honestly be bought is a
-- different state from one still awaiting purchase, with the reason
-- recorded in decision_note.
CREATE TABLE IF NOT EXISTS booking_requests (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    traveler_id         uuid REFERENCES travelers(id) ON DELETE SET NULL,
    cost_center_id      uuid REFERENCES cost_centers(id) ON DELETE SET NULL,
    requested_by        uuid REFERENCES users(id) ON DELETE SET NULL,
    approver_id         uuid REFERENCES users(id) ON DELETE SET NULL,
    itinerary_snapshot  jsonb NOT NULL,
    amount              numeric(12,2) NOT NULL,
    currency            text NOT NULL,
    policy_result       jsonb NOT NULL DEFAULT '[]'::jsonb,
    status              text NOT NULL DEFAULT 'requested'
                        CHECK (status IN
                        ('requested','approved','rejected','expired','purchased','purchase_failed')),
    decision_note       text NOT NULL DEFAULT '',
    decided_at          timestamptz,
    order_id            text REFERENCES orders(order_id) ON DELETE SET NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS booking_requests_account_status_idx
    ON booking_requests (account_id, status);
CREATE INDEX IF NOT EXISTS booking_requests_order_idx ON booking_requests (order_id);
ALTER TABLE booking_requests ENABLE ROW LEVEL SECURITY;
