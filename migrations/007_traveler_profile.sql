-- Traveler profile fields the bolt-on model needs and the original booking
-- flow never did: loyalty programs, trusted-traveler numbers, and trip
-- preferences. Kept OFF TRAVELER_FIELDS in db.py on purpose — those feed the
-- Duffel passenger payload during booking, and none of this belongs there.
ALTER TABLE travelers ADD COLUMN IF NOT EXISTS nationality             text NOT NULL DEFAULT '';
ALTER TABLE travelers ADD COLUMN IF NOT EXISTS loyalty_programs        jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE travelers ADD COLUMN IF NOT EXISTS known_traveler_number   text NOT NULL DEFAULT '';
ALTER TABLE travelers ADD COLUMN IF NOT EXISTS redress_number          text NOT NULL DEFAULT '';
ALTER TABLE travelers ADD COLUMN IF NOT EXISTS canadian_travel_number  text NOT NULL DEFAULT '';
ALTER TABLE travelers ADD COLUMN IF NOT EXISTS clear_plus              boolean NOT NULL DEFAULT false;
ALTER TABLE travelers ADD COLUMN IF NOT EXISTS home_airport            text NOT NULL DEFAULT '';
ALTER TABLE travelers ADD COLUMN IF NOT EXISTS seat_preference         text NOT NULL DEFAULT '';
ALTER TABLE travelers ADD COLUMN IF NOT EXISTS preferred_airline       text NOT NULL DEFAULT '';
