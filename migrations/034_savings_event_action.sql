-- Commission is on rebooking only. A cancellation is the customer taking their
-- own money back; it is not a saving TD found, and it carries no fee.
--
-- savings_events has recorded both since migration 008, because both come out
-- of the same execution path and both need the same facts kept (what came
-- back, how, and whether it reached the card). What it could not say was
-- which of the two an event was, so every reader treated a cancellation as a
-- recovered fare drop: commission was computed on it, it was invoiced, the
-- wallet called it "Fare drop recovered", and it counted as a saving.
--
-- `action` is that missing fact, the same two values execution_attempts.action
-- already uses. Existing rows are backfilled from the attempt that produced
-- them; a row whose attempt is gone stays 'exchange', which is what every row
-- was treated as until now.
ALTER TABLE savings_events ADD COLUMN IF NOT EXISTS action text NOT NULL DEFAULT 'exchange';

ALTER TABLE savings_events DROP CONSTRAINT IF EXISTS savings_events_action_check;
ALTER TABLE savings_events ADD CONSTRAINT savings_events_action_check
    CHECK (action IN ('exchange', 'cancel'));

UPDATE savings_events se
   SET action = ea.action
  FROM execution_attempts ea
 WHERE ea.id = se.execution_attempt_id
   AND ea.action = 'cancel'
   AND se.action <> 'cancel';

-- The fee that was computed on a cancellation was never owed. Cleared where
-- it has not been invoiced; an issued invoice is frozen and is left alone.
UPDATE savings_events
   SET commission_amount = 0, commission_rate = 0
 WHERE action = 'cancel'
   AND invoice_line_id IS NULL
   AND commission_amount <> 0;

-- savings_events already has RLS on; the explicit no-op that keeps the habit
-- intact (see migration 032).
ALTER TABLE savings_events ENABLE ROW LEVEL SECURITY;
