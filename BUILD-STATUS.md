# TripDifference — Build Status

**Live:** https://tripdifference.vercel.app · **Repo:** rahm-0x/TripDifference · 21 commits, 34 files, +2,921 / −481

---

## Where it stands in one line

Phase 1 is built and deployed. The reshop engine (Phase 2's core IP) was already the strongest part of the codebase. What's left is a scheduler, two auth gaps, and the one thing no amount of engineering can settle — whether the economics work against real airline inventory.

---

## 1. Phase 1 — the invisible foundations

Everything below runs on Supabase Postgres, provisioned through the Vercel Marketplace.

| Piece | State | Detail |
|---|---|---|
| Database schema | Done | 9 tables, 6 migrations |
| Durable state | Done | Replaced `orders.json`; production used to lose every booking on instance recycle (`/tmp`) |
| Audit trail | Done | `audit_events`, append-only **enforced by trigger** — UPDATE and DELETE both raise |
| Idempotency | Done | `UNIQUE (order_id, action, change_offer_id)` — a duplicate exchange never reaches Duffel |
| Concurrency | Done | `SELECT … FOR UPDATE` in one transaction; the JSON file was losing concurrent writes |
| Auth | Done* | argon2id, opaque session tokens (only a SHA-256 stored), CSRF on all 13 forms, login throttling |

\* two gaps remain — see §5.

**Before:** production was publicly reachable. `/trips`, `/orders`, `/decisions` needed no session, and the login screen set a flag nothing ever read.

---

## 2. The app itself

28 routes. Everything below reads real data — no illustrative numbers anywhere.

- **Overview** — spend & recovered over time, weekly monitoring activity, spend by carrier, recent bookings, upcoming trips
- **Search → book** — group bookings up to 9 travelers, each with its own saved-profile picker
- **Travelers** — saved passenger profiles that prefill the booking flow
- **Wallet** — transaction ledger: charges, recoveries, the 25% fee
- **Ops console** — simulate a price drop and watch the engine decide
- **Decision log** — every decision, eligibility verdict and execution as separate events

**One rule held throughout:** every summary figure derives from the same query as the page it summarizes. A dashboard claiming 184 travelers over a roster of 8 was on the original audit; it can't recur by construction.

---

## 3. Bugs found and fixed

Ordered by how much they mattered.

1. **No CSRF anywhere.** Worst case was `/orders/<id>/execute` — its "type CONFIRM" step lives in the request body, so an attacker's page could supply it and fire an exchange on a signed-in operator's session.
2. **Booking failed after payment.** Saved travelers held phone numbers with no country code; Duffel requires E.164. The error named the field but not the person, which made a phone problem read as a date-of-birth problem.
3. **Group bookings would have duplicated one passenger.** `book()` spread a single person's details across every seat. Latent — nothing could request a second seat yet.
4. **Simulate did nothing.** The scenario lived in `/tmp`: per-instance, wiped on recycle. Gone before anyone pressed the button.
5. **Decision log was returning 500** on any audit row shaped unusually. Self-inflicted, from my own wallet test data.
6. **Back/forward gave "page cannot be reached."** `/search` was POST-only — `net::ERR_CACHE_MISS`. Now a GET route; searches are linkable.
7. **"Logins are not persisting"** — they never were. The sidebar sent you to the marketing page, which had no idea a session existed.
8. **Unlimited password guessing.** Twelve wrong passwords, twelve 401s, no backoff.
9. **Double-clicking Pay showed a raw 422** implying failure, right after the customer had successfully been charged.

---

## 4. How it's verified

- `test_engine.py` — **60 tests passing** throughout; the reshop pipeline was never modified
- Real bookings placed and cancelled against the Duffel sandbox at each stage (all test orders refunded, accounts deleted)
- Browser-driven checks in production for auth gating, navigation, group booking, charts and the simulate flow
- Chart palette run through a colorblind-safety validator — **the brand gold/green pair failed** (protanopia ΔE 5.4, normal-vision 14.3, below the 15 floor) and was replaced with a validated blue/amber at 27.4 / 30.7

---

## 5. Open — and honest about it

**Auth, two gaps.** No email verification (anyone can sign up as any address — I registered `ceo@delta.com` against production to prove it) and no password reset. Both need an email provider; that's a decision, not a build.

**No scheduler.** Nothing runs cycles unattended, so "monitors 24/7" is not yet true — cycles only run when someone clicks. This is the next build and it's small now that the engine is pure and state is durable.

**Simulated ≠ real, deliberately.** A simulated rebooking now flows through trips, wallet and overview so the product can be demonstrated end to end — but into separate `sim_*` columns, tagged everywhere it appears, and reversible in one click. No real total can absorb a simulated figure.

---

## 6. The one thing engineering cannot settle

Duffel's sandbox hardcodes `change_total_amount` at **exactly +125.00**. Never negative — verified across a 33× price range, two routes, two cabins, a business→economy downgrade, a four-month date shift and two carriers. Changing to *the flight you already booked* still costs +125.00.

So the sandbox proves the **plumbing** — price, select, execute, read the right fields. It cannot prove the **economics**.

Duffel's own docs say `change_total_amount` "may be negative to reflect a refund", and there is a defined behavioral branch for it ("if zero or negative, there is no need to pass a payment object"). An API wouldn't document that rule for a field that could never be negative. But documented is not measured.

> **This is the single largest unvalidated assumption in the plan.** It closes with production credentials and a real fare drop on a cheap refundable domestic route — nothing else.

One useful lever: the order-change-offers endpoint supports `sort=change_total_amount`, so ascending puts the most negative offer first.

Also worth knowing for demos: per the carrier survey, **Iberia advertises changeability and has no `change` action** — an IB booking can never be rebooked, no matter the price. Duffel Airways, BA, AA and TAP work.

---

## 7. Next, in order

1. **Scheduler** — a cron hitting a route that iterates monitored orders. Makes "24/7" true. Small.
2. **Email provider** — closes verification and password reset together.
3. **Live-mode validation harness** — book a cheap refundable domestic route, poll the real API, log every `change_total_amount` so a genuine negative is captured the moment it appears. This is the experiment that decides the business model.

Items 1 and 2 are engineering. Item 3 is the one that determines whether any of this earns money.
