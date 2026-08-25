-- Lockdown, 2026-08-25. Confirmed live, not theoretical: with RLS off and
-- Supabase's default project grants never narrowed, the anon key could
-- SELECT real rows (users.id/email) through the Data API with zero auth —
-- verified with an actual unauthenticated request before this ran. Every
-- table in public also granted anon/authenticated full INSERT/UPDATE/DELETE,
-- so the same path could forge a `sessions` row (self-computed token_hash)
-- and take over any account, or read password_hash / stripe_customer_id /
-- every passenger's PII in orders.raw.
--
-- This app never needs anon/authenticated to touch Postgres directly:
--   - the app connects as `postgres` via the pooler (POSTGRES_URL), which
--     has rolbypassrls=true — confirmed by querying pg_roles before this
--     migration was written, so ENABLE ROW LEVEL SECURITY with zero
--     policies is invisible to every query this app makes.
--   - supabase_auth.py (Google SSO) only makes HTTP calls to GoTrue's
--     /auth/v1/* endpoints with the anon key as an API header — it never
--     opens a Postgres connection as anon/authenticated. Revoking their
--     table grants does not touch that path.
--
-- Default-deny: RLS enabled, no policies created. anon/authenticated can
-- see the schema (USAGE, untouched — table privileges are the exposure,
-- not schema visibility) but cannot read or write a single row anywhere.

DO $$
DECLARE
    t text;
BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
    LOOP
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM anon, authenticated', t);
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
    END LOOP;
END $$;

-- Sequences (bigserial PKs) held the same blanket grant; closed for
-- completeness even though it's moot once the owning table's grants are gone.
DO $$
DECLARE
    s text;
BEGIN
    FOR s IN SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'
    LOOP
        EXECUTE format('REVOKE ALL ON SEQUENCE public.%I FROM anon, authenticated', s);
    END LOOP;
END $$;

-- Stop this project's own future migrations from reopening the hole: any
-- table/sequence/function this `postgres` role creates from here on no
-- longer defaults to granting anon/authenticated anything.
--
-- Known residual gap, not closeable from here: Supabase's platform-level
-- default ACL (granted by `supabase_admin`, a role this project's
-- non-superuser `postgres` role has no privilege to alter) will still hand
-- anon/authenticated full grants on any brand-new table the moment it's
-- created — confirmed present via pg_default_acl before writing this
-- migration. RLS itself defaults to OFF on new tables regardless of grants.
-- Every future migration that adds a table MUST include an explicit
-- ENABLE ROW LEVEL SECURITY for that table; this migration cannot enforce
-- that going forward, only reduce today's surface and this role's own
-- default going forward.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL ON FUNCTIONS FROM anon, authenticated;
