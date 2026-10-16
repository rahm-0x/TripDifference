-- Scouts: approved referral partners, a referral code on every account, and
-- the ledger of what scouts are owed.
--
-- A scout earns a share (scouts.default_rate, 10% unless changed) of the
-- commission TD actually collects from every business they referred, for the
-- life of that account. The rate an account was referred at is snapshotted
-- onto the account (accounts.scout_rate), the same principle as never
-- hardcoding the commission rate: changing a scout's rate changes future
-- signups only.
--
-- Every account now carries the code it signed up with. There is no public
-- code: signup refuses without a real one (referrals.py). Accounts that
-- existed before this migration are attributed to LEGACY, an internal code
-- owned by a system scout at a rate of zero, which signup never accepts.
-- SCOUT-APPLICANT is the second internal code: the account a scout gets when
-- they apply without already being a customer. Both earn nothing.

-- --- who runs the program --------------------------------------------------
-- users.role is a role inside a customer's own account, and every customer is
-- the 'admin' of theirs. TD's own staff need a flag of their own; it is set by
-- hand in SQL and nothing in the app grants it.
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_staff boolean NOT NULL DEFAULT false;

-- --- scouts ----------------------------------------------------------------
-- user_id is NULL only for the one system scout, which owns the internal
-- codes and is never paid. RESTRICT everywhere below: a ledger of money owed
-- to a third party does not disappear because a row it points at is deleted.
CREATE TABLE IF NOT EXISTS scouts (
    id                        bigserial PRIMARY KEY,
    user_id                   uuid UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
    is_system                 boolean NOT NULL DEFAULT false,
    status                    text NOT NULL DEFAULT 'applied'
                              CHECK (status IN ('applied', 'approved', 'rejected', 'suspended', 'terminated')),
    default_rate              numeric(5,4) NOT NULL DEFAULT 0.10
                              CHECK (default_rate >= 0 AND default_rate <= 1),
    company                   text NOT NULL DEFAULT '',
    sourcing_plan             text NOT NULL DEFAULT '',
    stripe_connect_account_id text,
    -- A connected account exists in one Stripe mode only, like a customer
    -- (accounts.stripe_livemode, migration 036).
    stripe_connect_livemode   boolean NOT NULL DEFAULT false,
    payouts_enabled           boolean NOT NULL DEFAULT false,
    applied_at                timestamptz NOT NULL DEFAULT now(),
    approved_at               timestamptz,
    approved_by               uuid REFERENCES users(id) ON DELETE SET NULL,
    agreement_accepted_at     timestamptz,
    status_changed_at         timestamptz,
    CHECK (is_system OR user_id IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS scouts_one_system_idx ON scouts ((true)) WHERE is_system;
CREATE INDEX IF NOT EXISTS scouts_connect_idx ON scouts (stripe_connect_account_id)
    WHERE stripe_connect_account_id IS NOT NULL;
ALTER TABLE scouts ENABLE ROW LEVEL SECURITY;

-- --- referral codes --------------------------------------------------------
-- `internal` codes (LEGACY, SCOUT-APPLICANT) are attribution records only:
-- signup refuses them whatever their `active` says.
CREATE TABLE IF NOT EXISTS referral_codes (
    id              bigserial PRIMARY KEY,
    scout_id        bigint NOT NULL REFERENCES scouts(id) ON DELETE RESTRICT,
    code            citext NOT NULL UNIQUE,
    internal        boolean NOT NULL DEFAULT false,
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    deactivated_at  timestamptz,
    -- The target of accounts' composite foreign key below.
    UNIQUE (id, scout_id)
);
CREATE INDEX IF NOT EXISTS referral_codes_scout_idx ON referral_codes (scout_id);
ALTER TABLE referral_codes ENABLE ROW LEVEL SECURITY;

INSERT INTO scouts (is_system, status, default_rate, company, approved_at)
SELECT true, 'approved', 0, 'Trip Difference (internal)', now()
 WHERE NOT EXISTS (SELECT 1 FROM scouts WHERE is_system);

INSERT INTO referral_codes (scout_id, code, internal)
SELECT s.id, c.code, true
  FROM scouts s CROSS JOIN (VALUES ('LEGACY'), ('SCOUT-APPLICANT')) AS c(code)
 WHERE s.is_system
ON CONFLICT (code) DO NOTHING;

-- --- failed code entries, for throttling -----------------------------------
-- Same reasoning as auth_failures (migration 002): serverless instances share
-- no memory. A table of its own so guessing codes cannot lock anyone out of
-- signing in, and signing in wrong cannot use up a signup's attempts.
CREATE TABLE IF NOT EXISTS referral_code_failures (
    id    bigserial PRIMARY KEY,
    ip    inet,
    code  text NOT NULL DEFAULT '',
    ts    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS referral_code_failures_ip_idx ON referral_code_failures (ip, ts DESC);
ALTER TABLE referral_code_failures ENABLE ROW LEVEL SECURITY;

-- --- attribution on the account --------------------------------------------
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS referral_code_id bigint;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS scout_id bigint;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS scout_rate numeric(5,4);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS referred_at timestamptz;
-- Set at signup when the signup looked like the scout referring themselves
-- but was not refused (a shared free-mail domain); cleared from view, never
-- erased, by referral_flag_reviewed_at.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS referral_flag text;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS referral_flag_reviewed_at timestamptz;

UPDATE accounts a
   SET referral_code_id = rc.id, scout_id = rc.scout_id, scout_rate = 0,
       referred_at = a.created_at
  FROM referral_codes rc
 WHERE rc.code = 'LEGACY' AND a.referral_code_id IS NULL;

ALTER TABLE accounts ALTER COLUMN referral_code_id SET NOT NULL;
ALTER TABLE accounts ALTER COLUMN scout_id SET NOT NULL;
ALTER TABLE accounts ALTER COLUMN scout_rate SET NOT NULL;
ALTER TABLE accounts ALTER COLUMN referred_at SET NOT NULL;

-- scout_id is denormalized from the code; the composite key means the two
-- can never disagree.
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_referral_code_fkey;
ALTER TABLE accounts ADD CONSTRAINT accounts_referral_code_fkey
    FOREIGN KEY (referral_code_id, scout_id) REFERENCES referral_codes (id, scout_id) ON DELETE RESTRICT;
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_scout_rate_check;
ALTER TABLE accounts ADD CONSTRAINT accounts_scout_rate_check CHECK (scout_rate >= 0 AND scout_rate <= 1);
CREATE INDEX IF NOT EXISTS accounts_scout_idx ON accounts (scout_id);

-- Attribution is fixed at signup. The one way to change it is the admin
-- override (db.account_attribution_override), which sets td.attribution_override
-- for its own transaction and writes the audit row in that same transaction.
CREATE OR REPLACE FUNCTION accounts_attribution_fixed() RETURNS trigger AS $$
BEGIN
    IF (NEW.referral_code_id, NEW.scout_id, NEW.scout_rate, NEW.referred_at)
       IS DISTINCT FROM (OLD.referral_code_id, OLD.scout_id, OLD.scout_rate, OLD.referred_at)
       AND coalesce(current_setting('td.attribution_override', true), '') <> 'on' THEN
        RAISE EXCEPTION 'an account''s referral attribution is fixed at signup; change it through the audited admin override';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS accounts_attribution_fixed ON accounts;
CREATE TRIGGER accounts_attribution_fixed
    BEFORE UPDATE OF referral_code_id, scout_id, scout_rate, referred_at ON accounts
    FOR EACH ROW EXECUTE FUNCTION accounts_attribution_fixed();

ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;

-- --- the scout ledger ------------------------------------------------------
-- One payout per scout, per month, per currency. The unique key is the
-- idempotency guard for the transfer, the same way execution_attempts guards
-- an exchange: a second run for the same month loses the INSERT and never
-- reaches Stripe.
CREATE TABLE IF NOT EXISTS scout_payouts (
    id                  bigserial PRIMARY KEY,
    scout_id            bigint NOT NULL REFERENCES scouts(id) ON DELETE RESTRICT,
    period              date NOT NULL,
    currency            text NOT NULL,
    amount              numeric(12,2) NOT NULL CHECK (amount > 0),
    status              text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid', 'failed')),
    stripe_transfer_id  text,
    attempts            integer NOT NULL DEFAULT 0,
    error               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    -- when the current attempt began; a payout reopened after a failure gets a new one
    opened_at           timestamptz NOT NULL DEFAULT now(),
    paid_at             timestamptz,
    UNIQUE (scout_id, period, currency)
);
ALTER TABLE scout_payouts ENABLE ROW LEVEL SECURITY;

-- One earning per collected commission, and at most one reversal of it.
-- (savings_event_id, entry) is the idempotency key: the hook that runs when a
-- commission is charged and the daily catch-up can both try to write the same
-- earning, and only one row results. A reversal of an earning that was
-- already paid is a negative row of its own, which nets against the scout's
-- next payout; nothing is clawed back from Stripe.
CREATE TABLE IF NOT EXISTS scout_earnings (
    id                  bigserial PRIMARY KEY,
    scout_id            bigint NOT NULL REFERENCES scouts(id) ON DELETE RESTRICT,
    account_id          uuid NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    savings_event_id    bigint NOT NULL REFERENCES savings_events(id) ON DELETE RESTRICT,
    entry               text NOT NULL DEFAULT 'earning' CHECK (entry IN ('earning', 'reversal')),
    commission_amount   numeric(12,2) NOT NULL,
    rate                numeric(5,4) NOT NULL,
    amount              numeric(12,2) NOT NULL,
    currency            text NOT NULL,
    status              text NOT NULL CHECK (status IN ('pending', 'available', 'paid', 'reversed')),
    available_at        timestamptz NOT NULL,
    payout_id           bigint REFERENCES scout_payouts(id) ON DELETE RESTRICT,
    stripe_transfer_id  text,
    reversed_at         timestamptz,
    reversal_reason     text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (savings_event_id, entry),
    CHECK ((entry = 'earning' AND amount >= 0) OR (entry = 'reversal' AND amount <= 0))
);
CREATE INDEX IF NOT EXISTS scout_earnings_scout_idx ON scout_earnings (scout_id, status);
CREATE INDEX IF NOT EXISTS scout_earnings_account_idx ON scout_earnings (account_id);
CREATE INDEX IF NOT EXISTS scout_earnings_payout_idx ON scout_earnings (payout_id) WHERE payout_id IS NOT NULL;
ALTER TABLE scout_earnings ENABLE ROW LEVEL SECURITY;

-- --- the audit trail learns about accounts ---------------------------------
-- audit_events has only ever described orders. A referral (written at signup,
-- or by an override) and a scout's lifecycle (approve, suspend, terminate,
-- rate change) describe an account or a scout instead: order_id is '' on
-- those rows and account_id says which account. No foreign key — the log
-- outlives what it describes.
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS account_id uuid;
ALTER TABLE audit_events DROP CONSTRAINT IF EXISTS audit_events_kind_check;
ALTER TABLE audit_events ADD CONSTRAINT audit_events_kind_check
    CHECK (kind IN ('decision', 'eligibility', 'execution', 'live_guard', 'referral', 'scout'));
CREATE INDEX IF NOT EXISTS audit_account_idx ON audit_events (account_id, ts DESC)
    WHERE account_id IS NOT NULL;

-- users and audit_events already have RLS on; the explicit no-op that keeps
-- the habit intact (see migration 032).
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
