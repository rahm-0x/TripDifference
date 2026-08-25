-- Duffel confirmed (2026-08-25, after this schema was first drafted) that
-- an exchange's residual value has three possible outcomes, not two: "may
-- be forfeited, refunded to the original form of payment, or issued as a
-- future travel credit," depending on the purchased fare's own rules —
-- refundable fares generally return cash, nonrefundable ones tend to
-- produce a credit or nothing. A forfeited exchange is a real, recordable
-- event where the company ends up holding a cheaper ticket and receiving
-- nothing back, strictly worse than not acting, and it must be
-- distinguishable from the other two outcomes, not silently unrepresentable.
ALTER TABLE savings_events DROP CONSTRAINT savings_events_delivery_type_check;
ALTER TABLE savings_events ADD CONSTRAINT savings_events_delivery_type_check
    CHECK (delivery_type IN ('refund_to_card', 'airline_credit', 'forfeited'));

-- Links a recovery to the credit it created and the line it was billed on.
ALTER TABLE savings_events ADD COLUMN IF NOT EXISTS airline_credit_id uuid
    REFERENCES airline_credits(id) ON DELETE SET NULL;
ALTER TABLE savings_events ADD COLUMN IF NOT EXISTS invoice_line_id bigint
    REFERENCES invoice_lines(id) ON DELETE SET NULL;

-- The engine has to know disposition BEFORE it executes an exchange, not
-- infer it after — without this, eligibility.assess() has no way to tell
-- "this exchange returns cash" from "this exchange forfeits the
-- difference," and would happily recommend the second. Captured and
-- frozen at booking time from the purchased offer's own conditions (the
-- same conditions.change_before_departure/refund_before_departure shape
-- already buried in orders.raw — promoted to a stable top-level column so
-- it survives independent of whatever orders.raw's live conditions read
-- later). `refundable` is a fast derived flag for filtering; the full
-- object lives in `fare_conditions` for eligibility.assess() to reason
-- over. Existing 10 orders backfill NULL — none of them have this
-- captured, and NULL means "unknown," not "no."
ALTER TABLE orders ADD COLUMN IF NOT EXISTS refundable boolean;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS fare_conditions jsonb;
