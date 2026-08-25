-- purchase_failed carried its distinction only in decision_note free text,
-- and Phase 2's purchase-time logic needs to branch on it, not parse
-- prose. decision_note stays as the human-readable detail; failure_reason
-- is what code checks.
ALTER TABLE booking_requests ADD COLUMN IF NOT EXISTS failure_reason text
    CHECK (failure_reason IN ('fare_above_ceiling','flight_unavailable','duffel_error'));
