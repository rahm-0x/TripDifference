-- Real available_actions observations per carrier, gathered for free from
-- every order this app actually creates — the same near-zero-marginal-cost
-- idea as fare-observation data. Not a score: two raw counts, so ranking
-- logic can tell "one denial ever seen" from "fifty," which a single
-- derived percentage would flatten. Global, not account-scoped — a
-- carrier's willingness to expose 'change' on a real order is a fact about
-- the carrier, not about who booked it.
--
-- Seeded from FINDINGS.md §8's carrier survey (one real sandbox order per
-- carrier, LHR→JFK) — including the Iberia result this table exists to
-- act on: conditions.change_before_departure.allowed=True, but
-- available_actions never included 'change'.
CREATE TABLE IF NOT EXISTS carrier_change_capability (
    carrier_iata           text PRIMARY KEY,
    carrier_name           text NOT NULL DEFAULT '',
    change_confirmed_count integer NOT NULL DEFAULT 0,
    change_denied_count    integer NOT NULL DEFAULT 0,
    last_observed_at       timestamptz,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE carrier_change_capability ENABLE ROW LEVEL SECURITY;

INSERT INTO carrier_change_capability
    (carrier_iata, carrier_name, change_confirmed_count, change_denied_count, last_observed_at)
VALUES
    ('ZZ', 'Duffel Airways',    1, 0, now()),
    ('BA', 'British Airways',   1, 0, now()),
    ('AA', 'American Airlines', 1, 0, now()),
    ('TP', 'TAP Air Portugal',  1, 0, now()),
    ('IB', 'Iberia',            0, 1, now())
ON CONFLICT (carrier_iata) DO NOTHING;
