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
.venv/bin/python -m pytest          # money-path and reshop-engine tests, real DB, mocked Duffel/Stripe
```

## Deployment

Production tracks `main`, staging tracks the `staging` branch. Vercel auto-detects the
Flask `app` in root `app.py` and routes every path to a single function.

- **Real auth, gating almost everything.** Every screen except the marketing landing
  page, login/signup, the logo route, and the Resend inbound webhook requires a
  session (`@auth.login_required`, `app.py`). Two open gaps: no email verification
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
