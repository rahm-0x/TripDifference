# TripDifference

A corporate travel portal that buys flights as merchant of record, then monitors
booked fares and exchanges to cheaper identical flights on the traveler's behalf,
billing a commission on what's actually recovered.

**Start with [`CLAUDE.md`](CLAUDE.md)** for the commercial model and the constraints
that shape the codebase, and **[`docs/architecture.md`](docs/architecture.md)** for
the four-layer system breakdown, the schema, and the known, deliberate gaps. This file
is just setup and deployment.

## Run it locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
vercel env pull .env.local      # Postgres, Stripe, Resend — needs Vercel project access
cp .env.example .env            # then put your own duffel_test_ token in it
.venv/bin/python app.py         # → http://localhost:8000
```

Sign up for an account through the UI (`/signup`) — there's no seed script. Real
Duffel sandbox bookings and real Stripe test-mode charges happen against whatever
`DUFFEL_TOKEN`/`STRIPE_SECRET_KEY` you have configured; nothing here is a mock.

```bash
.venv/bin/python scripts/migrate_staging.py   # once, and after adding a migration
.venv/bin/python -m pytest                    # money-path and reshop-engine tests, staging DB, mocked Duffel/Stripe
```

There are two databases: production and staging. The money-path tests write real rows,
so they run against staging: set `STAGING_POSTGRES_URL` (transaction pooler, 6543) and
`STAGING_POSTGRES_URL_NON_POOLING` (session pooler or direct, 5432) in
`.env.staging.local` (gitignored, and untouched by `vercel env pull`, which rewrites
`.env.local`). `conftest.py` refuses to run if either is missing or reaches the production
database, aborts if the connection it opens isn't the staging one, and warns when staging
holds live orders. Tests share that database with live staging orders, so each deletes
only the rows it created, every account it creates is named `__pytest__` (never
signable-into; excluded by `validation/`), and carrier data uses test-only codes.

## Deployment

Production tracks `main`, staging tracks the `staging` branch. Vercel auto-detects the
Flask `app` in root `app.py` and routes every path to a single function.

- **Production is sandbox-only; staging can search live, and book live if told to.**
  `APP_ENV` (`production` when unset) decides. On staging, a `duffel_live_` token with
  `DUFFEL_LIVE_SEARCH_ENABLED=true` runs real Duffel searches, capped at
  `STAGING_MAX_MONTHLY_SEARCHES` (default 1400) and logged in `live_search_log`; the
  `/eligibility` page and `scripts/eligibility_scan.py` use it. Orders, changes and cancels
  on staging are refused, live or sandbox, unless `DUFFEL_LIVE_ORDERS_ENABLED=true` (default
  false), and then only under `STAGING_ALLOWED_EMAILS`, `STAGING_MAX_ORDER_USD` and
  `STAGING_MAX_DAILY_USD` (`live_guard.py`), paid from TD's Duffel balance. Every page
  carries a banner: amber for live search, red for live orders. The app refuses to start
  on a Vercel Preview deployment without an explicit `APP_ENV`, if a live flag or live token
  is set outside staging, if a live flag is on over a test token, if staging's Stripe key
  isn't `sk_test_`, or if the database doesn't match `APP_ENV` (staging must be the staging
  Supabase project, production must not be). `GET /healthz/env` (signed in) shows what a
  deploy is running as: `APP_ENV`, commit, token mode, database project, both flags. Staging's env vars are scoped to the
  `staging` branch in Vercel's Preview environment. Apply migrations to staging with
  `scripts/migrate_staging.py` and to production with `scripts_migrate.py` **before**
  deploying code that needs them.

- **Real auth, default deny.** Every route requires a session except
  `app.PUBLIC_ENDPOINTS`: the landing page, login/signup, the Google auth callbacks, static
  files and the logo route, and the Resend inbound webhook (authenticated by its Svix
  signature). `app._require_login` enforces it before any view runs, including routes
  added later; `test_routes.py` walks the route map and fails on anything else reachable
  anonymously. Two open gaps: no email verification
  (anyone can sign up as any address) and no password reset — both need an email
  provider, which is a decision, not a build.
- **Durable state.** Bookings, the audit trail, and everything else live in Postgres
  (Supabase, via the Vercel Marketplace) — nothing is written to `/tmp`.
- **`.vercelignore` patterns match at any depth.** An entry of `index.html` would
  silently exclude `templates/index.html` and every page would 500 with
  `TemplateNotFound`. Root-only excludes need a leading `/`.
- **`responses/` must stay excluded** — raw Duffel API dumps from early spike work,
  large enough to blow the function bundle limit on their own.

Static assets live in `public/` (CDN-served); `/logo.png` has a local-dev route so the
same URL works in both environments.

## What's in this repo besides the app

`duffel.py` and `duffel_reshop_test.py` are earlier standalone scripts (a CLI spike
and a Duffel API validation harness) — not imported by `app.py`, not part of the
deployed app, kept for the sandbox-behaviour notes now folded into
`docs/architecture.md`. `server.py` and `index.html` are an even earlier
stdlib-only local UI, fully superseded by the Flask app.
