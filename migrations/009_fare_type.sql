-- Cash vs. points eligibility gate. Every pre-pivot order was booked through
-- Duffel's cash-offer search, so 'cash' is the honest backfill default, not
-- a guess — this repo has never made a points/award booking.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS fare_type text NOT NULL DEFAULT 'cash'
    CHECK (fare_type IN ('cash', 'points'));
