# Project briefing

TripDifference is a Flask app that acts as merchant of record for corporate travel: it books flights via Duffel (sandbox only), charges the company through Stripe, then monitors booked fares and exchanges to cheaper identical ones via Duffel's Order Change API, billing a commission on recovered savings plus a subscription. It runs on Postgres/Supabase with RLS, real auth, and an append-only audit trail. The last commit (0193f85, Aug 26) rewrote CLAUDE.md and docs/architecture.md from the code itself, verified by two independent passes, and explicitly logged two known-but-unfixed gaps rather than silently patching them. The working tree is clean and every file I checked still matches what those docs claim — nothing in the repo has changed since that commit 9 days ago.

Stack: Python/Flask + psycopg on Postgres (Supabase via Vercel Marketplace), Duffel (sandbox) for ticketing/exchange, Stripe for payment, deployed on Vercel as a single function.
Open thread: Item 4 (the system-map docs) just landed as the last commit; two deliberate gaps are logged and still open — hardcoded 25% commission in onboarding_payment.html/orders.html, and db.monthly_series() aggregating orders.refunded across orders for the Trips spend chart (the exact cross-order pattern the

## Direction set by the owner

- **Nothing in the repo has changed since Aug 26, but you're asking me to re-read it now — what actually prompted this? A new priority/'Item 5', or something external (a Duffel/Stripe/Vercel incident, stakeholder feedback, a real customer issue)?**
  Just re-verifying nothing drifted — no specific trigger

- **README says 'production tracks main, staging tracks the staging branch,' but the staging branch is still sitting at 35c1429 — from before Postgres, real auth, the corporate schema, or the money path existed. Is staging abandoned, or does it need to be brought current?**
  Fast-forward staging to main

- **Of the two gaps item 4 logged and deliberately left (hardcoded 25% commission text, orders.refunded cross-order aggregation in the spend chart), should either be the next thing fixed, or are both meant to stay parked?**
  Fix the 25% hardcoding now

- **Is validating the exchange economics against live Duffel credentials (instead of sandbox's hardcoded +125.00 change_total) on the near-term roadmap, or still explicitly out of scope?**
  Yes, live-credential testing is coming soon

## Rules

- Treat the owner's answers above as binding. Do not relitigate them.
- If a task conflicts with this briefing, say so in your final message instead of guessing.
