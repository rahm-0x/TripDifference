-- The simulated price scenario per order.
--
-- It used to live in a JSON file under DATA_DIR, which on Vercel is /tmp:
-- per-instance and wiped on recycle. The scenario seeded when the order was
-- booked was therefore usually missing by the time anyone pressed Simulate,
-- the engine could not match the itinerary, and the run came back "no
-- identical itinerary" instead of the drop the operator asked for.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS sim_scenario jsonb;
