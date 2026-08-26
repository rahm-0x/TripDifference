-- ZZ (Duffel Airways) is Duffel's own sandbox test carrier — it supports
-- 'change' because Duffel built it to, not because of anything a real
-- airline decided. It has to stay in the table (sandbox bookings need a
-- sane read), but it is not evidence about real carrier behaviour and must
-- be excludable from any read meant to answer that question.
ALTER TABLE carrier_change_capability ADD COLUMN IF NOT EXISTS is_synthetic boolean NOT NULL DEFAULT false;
UPDATE carrier_change_capability SET is_synthetic = true WHERE carrier_iata = 'ZZ';

-- A raw, append-only log alongside the aggregate counts. The aggregate
-- table is what ranking reads (fast, one row per carrier); this is what a
-- later question reads — specifically "do denials cluster by fare brand
-- rather than by carrier" (FINDINGS.md already suspects this). Not acted
-- on yet, but unrecoverable later if the fare_brand isn't captured now.
CREATE TABLE IF NOT EXISTS carrier_change_observations (
    id             bigserial PRIMARY KEY,
    carrier_iata   text NOT NULL,
    fare_brand     text NOT NULL DEFAULT '',
    change_allowed boolean NOT NULL,
    order_id       text REFERENCES orders(order_id) ON DELETE SET NULL,
    observed_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS carrier_change_observations_carrier_brand_idx
    ON carrier_change_observations (carrier_iata, fare_brand);
ALTER TABLE carrier_change_observations ENABLE ROW LEVEL SECURITY;
