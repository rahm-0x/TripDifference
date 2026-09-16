-- Every live Duffel offer request, recorded before it is sent (live_search.py).
-- STAGING_MAX_MONTHLY_SEARCHES counts this month's (UTC) rows; a request that
-- errors still counts — it was sent. Test-token searches are not recorded.
--
-- The *_conditions columns record, per search, how many offers came back with
-- null change conditions, how many were fetched again individually
-- (GET /air/offers/:id — not an offer request, not budgeted), and how many of
-- those came back with conditions filled in.
CREATE TABLE IF NOT EXISTS live_search_log (
    id                  bigserial PRIMARY KEY,
    created_at          timestamptz NOT NULL DEFAULT now(),
    source              text NOT NULL DEFAULT '',     -- eligibility | eligibility_scan | search | market_price
    account_id          uuid REFERENCES accounts(id) ON DELETE SET NULL,
    origin              text NOT NULL DEFAULT '',
    destination         text NOT NULL DEFAULT '',
    departure_date      date,
    cabin               text NOT NULL DEFAULT '',
    passengers          integer,
    offer_request_id    text,
    offers_returned     integer,
    null_conditions     integer,
    refetched           integer,
    conditions_filled   integer,
    error               text
);
CREATE INDEX IF NOT EXISTS live_search_log_created_at_idx ON live_search_log (created_at);
ALTER TABLE live_search_log ENABLE ROW LEVEL SECURITY;
