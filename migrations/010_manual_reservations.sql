-- Add Reservation (manual entry): a reservation with no Duffel order behind
-- it at all — booked with the airline directly, not through TD. `raw` stays
-- NOT NULL (existing column); these rows store `raw = '{}'`, which app.py
-- already treats identically to a missing payload everywhere it reads it.
--
-- Segment data lives in real stored columns rather than being derived from
-- `raw`, since there is no Duffel payload to derive it from for this source.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS traveler_id uuid REFERENCES travelers(id) ON DELETE SET NULL;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS seg_origin text NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS seg_destination text NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS seg_flight_number text NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS seg_cabin text NOT NULL DEFAULT '';
