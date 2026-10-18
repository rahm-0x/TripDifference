-- Master scouts: an approved scout invites other scouts, and earns an override
-- on what the scouts they introduced earn directly.
--
-- The override is a share (scouts.override_rate, 20% unless changed) of the
-- introduced scout's own earning — 2% of TD's commission at the default
-- rates — paid out of TD's share: the introduced scout's earning never
-- shrinks. One level only. An override is written from a direct earning,
-- never from another override, so nobody earns on a recruit's recruits. It
-- exists only where a referred business's commission was actually charged;
-- nothing is ever paid for recruiting itself. Writing them is switched by
-- MASTER_OVERRIDE_ENABLED (config.py); the schema is the same either way.

-- --- the introduction, on the introduced scout --------------------------------
-- introduced_by_scout_id, override_rate and introduced_at are written
-- together when the application is approved (db.scout_approve), from the
-- invite it was made with (invite_code_id). After that only the audited staff
-- override (db.scout_introduction_override) changes them: the trigger below.
--
-- The foreign keys here are NO ACTION, not RESTRICT: both refuse to delete a
-- scout another row still points at, but NO ACTION checks at the end of the
-- statement, so one DELETE can remove a master and the scout they introduced
-- together (the test suite's cleanup does exactly that).
ALTER TABLE scouts ADD COLUMN IF NOT EXISTS default_override_rate numeric(5,4) NOT NULL DEFAULT 0.20;
ALTER TABLE scouts ADD COLUMN IF NOT EXISTS introduced_by_scout_id bigint REFERENCES scouts(id);
ALTER TABLE scouts ADD COLUMN IF NOT EXISTS override_rate numeric(5,4);
ALTER TABLE scouts ADD COLUMN IF NOT EXISTS introduced_at timestamptz;

ALTER TABLE scouts DROP CONSTRAINT IF EXISTS scouts_default_override_rate_check;
ALTER TABLE scouts ADD CONSTRAINT scouts_default_override_rate_check
    CHECK (default_override_rate >= 0 AND default_override_rate <= 1);
ALTER TABLE scouts DROP CONSTRAINT IF EXISTS scouts_override_rate_check;
ALTER TABLE scouts ADD CONSTRAINT scouts_override_rate_check
    CHECK (override_rate >= 0 AND override_rate <= 1);
-- All three or none of them, and never a scout's own master.
ALTER TABLE scouts DROP CONSTRAINT IF EXISTS scouts_introduction_whole_check;
ALTER TABLE scouts ADD CONSTRAINT scouts_introduction_whole_check
    CHECK ((introduced_by_scout_id IS NULL) = (override_rate IS NULL)
           AND (introduced_by_scout_id IS NULL) = (introduced_at IS NULL));
ALTER TABLE scouts DROP CONSTRAINT IF EXISTS scouts_not_own_master_check;
ALTER TABLE scouts ADD CONSTRAINT scouts_not_own_master_check
    CHECK (introduced_by_scout_id IS DISTINCT FROM id);
CREATE INDEX IF NOT EXISTS scouts_introduced_by_idx ON scouts (introduced_by_scout_id)
    WHERE introduced_by_scout_id IS NOT NULL;

-- --- scout-invite codes ------------------------------------------------------
-- A table of their own, not rows in referral_codes: signup looks codes up in
-- referral_codes only and the scout application in scout_invite_codes only, so
-- an invite code can never open a business account and a business code can
-- never attribute a scout. The INVITE- prefix belongs to this table alone
-- (the CHECK on referral_codes below), so the two never look alike either.
-- One active code per scout, issued at approval.
CREATE TABLE IF NOT EXISTS scout_invite_codes (
    id              bigserial PRIMARY KEY,
    scout_id        bigint NOT NULL REFERENCES scouts(id) ON DELETE RESTRICT,
    code            citext NOT NULL UNIQUE
                    CHECK (code::text = upper(code::text) AND code::text LIKE 'INVITE-%'),
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    deactivated_at  timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS scout_invite_codes_one_active_idx ON scout_invite_codes (scout_id) WHERE active;
ALTER TABLE scout_invite_codes ENABLE ROW LEVEL SECURITY;

ALTER TABLE referral_codes DROP CONSTRAINT IF EXISTS referral_codes_not_an_invite_check;
ALTER TABLE referral_codes ADD CONSTRAINT referral_codes_not_an_invite_check
    CHECK (code::text NOT ILIKE 'INVITE-%');

-- Every scout who could already invite gets their code now, as approval
-- issues one from here on. The subquery mentions s.id so it is evaluated per
-- scout rather than once for all of them.
INSERT INTO scout_invite_codes (scout_id, code)
SELECT s.id, 'INVITE-' || (SELECT string_agg(substr('ABCDEFGHJKMNPQRSTUVWXYZ23456789',
                                                    1 + floor(random() * 31)::int, 1), '')
                             FROM generate_series(1, 5) WHERE s.id IS NOT NULL)
  FROM scouts s
 WHERE NOT s.is_system AND s.status IN ('approved', 'suspended')
   AND NOT EXISTS (SELECT 1 FROM scout_invite_codes i WHERE i.scout_id = s.id);

-- --- the application's pending inviter ----------------------------------------
-- Which invite an application came in on, and what the self-referral check
-- found that a person should look at (a shared free-mail domain). Nothing is
-- attributed until approval.
ALTER TABLE scouts ADD COLUMN IF NOT EXISTS invite_code_id bigint REFERENCES scout_invite_codes(id);
ALTER TABLE scouts ADD COLUMN IF NOT EXISTS invite_flag text;

-- --- the introduction is fixed ----------------------------------------------
-- Written once, in the UPDATE that approves the application; changed after
-- that only inside db.scout_introduction_override, which sets
-- td.introduction_override for its own transaction and writes the audit row in
-- that same transaction.
CREATE OR REPLACE FUNCTION scouts_introduction_fixed() RETURNS trigger AS $$
BEGIN
    IF (NEW.introduced_by_scout_id, NEW.override_rate, NEW.introduced_at)
       IS DISTINCT FROM (OLD.introduced_by_scout_id, OLD.override_rate, OLD.introduced_at)
       AND NOT (OLD.status = 'applied' AND NEW.status = 'approved' AND OLD.introduced_by_scout_id IS NULL)
       AND coalesce(current_setting('td.introduction_override', true), '') <> 'on' THEN
        RAISE EXCEPTION 'a scout''s introduction is fixed at approval; change it through the audited admin override';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS scouts_introduction_fixed ON scouts;
CREATE TRIGGER scouts_introduction_fixed
    BEFORE UPDATE OF introduced_by_scout_id, override_rate, introduced_at ON scouts
    FOR EACH ROW EXECUTE FUNCTION scouts_introduction_fixed();

-- --- the ledger learns about overrides ---------------------------------------
-- kind says whose earning a row is: 'direct' is a scout's share of a
-- commission; 'override' is a master's share of a direct earning, pointing at
-- it through parent_earning_id (an override's reversal points at the same
-- direct earning). The idempotency key widens from (savings_event_id, entry)
-- to (savings_event_id, kind, entry): still one earning and at most one
-- reversal per savings event, now for each kind, so at most one override.
ALTER TABLE scout_earnings ADD COLUMN IF NOT EXISTS kind text;
UPDATE scout_earnings SET kind = 'direct' WHERE kind IS NULL;
ALTER TABLE scout_earnings ALTER COLUMN kind SET DEFAULT 'direct';
ALTER TABLE scout_earnings ALTER COLUMN kind SET NOT NULL;
ALTER TABLE scout_earnings DROP CONSTRAINT IF EXISTS scout_earnings_kind_check;
ALTER TABLE scout_earnings ADD CONSTRAINT scout_earnings_kind_check CHECK (kind IN ('direct', 'override'));

ALTER TABLE scout_earnings ADD COLUMN IF NOT EXISTS parent_earning_id bigint REFERENCES scout_earnings(id);
ALTER TABLE scout_earnings DROP CONSTRAINT IF EXISTS scout_earnings_parent_check;
ALTER TABLE scout_earnings ADD CONSTRAINT scout_earnings_parent_check
    CHECK ((kind = 'direct') = (parent_earning_id IS NULL));
CREATE INDEX IF NOT EXISTS scout_earnings_parent_idx ON scout_earnings (parent_earning_id)
    WHERE parent_earning_id IS NOT NULL;

ALTER TABLE scout_earnings DROP CONSTRAINT IF EXISTS scout_earnings_savings_event_id_entry_key;
ALTER TABLE scout_earnings DROP CONSTRAINT IF EXISTS scout_earnings_event_kind_entry_key;
ALTER TABLE scout_earnings ADD CONSTRAINT scout_earnings_event_kind_entry_key
    UNIQUE (savings_event_id, kind, entry);

-- --- audit kinds --------------------------------------------------------------
-- scout_introduced: written when an application made through an invite is
-- approved. introduction_override: staff changed or removed a master. Both
-- describe the introduced scout's own account (order_id '', account_id set).
ALTER TABLE audit_events DROP CONSTRAINT IF EXISTS audit_events_kind_check;
ALTER TABLE audit_events ADD CONSTRAINT audit_events_kind_check
    CHECK (kind IN ('decision', 'eligibility', 'execution', 'live_guard', 'referral', 'scout',
                    'scout_introduced', 'introduction_override'));

-- scouts, referral_codes, scout_earnings and audit_events already have RLS
-- on; the explicit no-op that keeps the habit intact (see migration 032).
ALTER TABLE scouts ENABLE ROW LEVEL SECURITY;
ALTER TABLE referral_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE scout_earnings ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
