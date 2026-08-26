# Architecture

The commercial model and the constraints that follow from it live in
[`CLAUDE.md`](../CLAUDE.md) at the repo root. This document is the system as it
actually works: the four layers, the schema, and — the part worth reading carefully —
the gaps that were found, examined, and deliberately left rather than fixed.

## Four layers

**1. Booking** — `app.py:search()` → `book/passenger` → `book/payment` → `book()`.
`book()` runs every gate before anything is created: card on file
(`db.account_card`), travel policy (`policy.evaluate`, which can block, require
approval, or just advise), then the payment/order sequence in `CLAUDE.md`'s Money
section. Policy approval never touches Duffel — offer requests are single-use and
offers expire, so a `booking_requests` row snapshots the itinerary and price instead
of holding an offer for later (`app.py:1211-1225`).

**2. The record model** — `accounts` is the company; `users` are people who log in,
with a `role` (`admin | booker | approver | finance` — stored, not yet enforced by any
route); `travelers` are the employees who fly and mostly never log in, each with an
optional `default_cost_center_id`; `orders` belong to both an account and (optionally)
a traveler, and carry a `cost_center_id`. `travelers.email`/`phone_number` are booking
contact details, not login credentials — a traveler with no `users` row at all is the
normal case.

**3. Reshop** — `engine.evaluate()`, given an `OrderSnapshot` and a `PriceSource`
(`prices.py`), runs a sequence of skip gates before ever reaching a reshop decision,
cheapest first: not eligible (`should_poll` false) → not `changeable` → inside the
void window → too close to departure → (market re-search here, diagnostic only,
never a gate) → no Duffel order behind this reservation at all
(`NO_EXECUTION_MECHANISM` — the manual/imported-reservation case) → no change offers
→ none matching the booked itinerary → `change_total >= 0` (a charge, not a refund)
→ below the `min_saving` floor → **reshop**. `change_total_amount` is the only number
that decides anything; the market re-search is logged for diagnostics and the engine
never branches on it (`test_cheaper_market_but_positive_change_total`,
`test_market_delta_never_flips_a_skip_to_a_reshop`). Execution is a second axis, kept
structurally separate from the decision (`engine.Execution`) so "decided not to
exchange" and "decided to exchange, blocked" can never be confused for the same
event — a decision that recommends a reshop from the simulated price source is
`BLOCKED_SIMULATED` and can never reach Duffel; a live one is
`AWAITING_CONFIRMATION` until a human types CONFIRM at `execute()`.

**4. Money out** — three delivery types (`CLAUDE.md`), commission on two of them
(never on `forfeited`), invoice generation (`db.generate_invoice`) that sums
`savings_events` into `commission_cash`/`commission_credit` lines plus a flat
`subscription` line, all `>= 0`, all additive.

## Tables and what each is for

- **`orders`** — one row per Duffel order (or, for `source = 'manual'`, one row with
  no Duffel order behind it at all — segment fields stored directly, `raw = {}`; see
  `db.create_manual_order`). No status column (see Known gaps).
- **`airline_credits`** — a **liability register**, not a rewards ledger. Credits are
  locked to one employee's name, can't be transferred, and expire
  (`airline_credits_expiring_idx`, a partial index on `status = 'active'`).
- **`carrier_change_capability`** / **`carrier_change_observations`** — observed
  carrier behaviour (real `available_actions` seen on real orders) feeding
  search-time ranking (`eligibility.assess`'s `carrier_capability` argument). Counts,
  not conclusions; `is_synthetic` marks Duffel Airways (`ZZ`) so it can be excluded
  from any read meant to answer what real carriers actually do.
- **`execution_attempts`** — the idempotency guard for the only two calls that move
  money (`exchange`, `cancel`), keyed `UNIQUE (order_id, action, change_offer_id)`.
- **`audit_events`** — append-only by trigger (`audit_events_immutable()`); `UPDATE`
  and `DELETE` both raise. One row per decision, eligibility verdict, or execution;
  `decisions.log`'s old JSONL shape lives on as this table's `payload` jsonb column.
- **`booking_requests`** — a policy-approval hold: itinerary snapshot + price ceiling,
  not a held offer (offers can't be held at all; see above).
- **`policy_rules`** — advisory or blocking travel-policy checks, evaluated onto every
  offer at search time (`offer_view()`'s `policy_enforcement`/`policy_result`) and
  enforced again, for real, in `book()`.

## Known gaps

These are deliberate deferrals, not oversights. Each was found, examined, and
consciously left. Treat them as decisions, not TODOs to silently "fix."

- **No order status column and no transition enforcement.** State is inferred from
  several independently-written fields (`monitoring`, `executed`, `refunded`,
  `simulated`, `payment_capture_failed_at`). Nothing enforces which combinations are
  legal. This works with two actions and a human in the loop; it gets more expensive
  to fix with every action added. Revisit when the scheduler lands.
- **Void-and-rebook is respected but unimplemented.** `engine.evaluate()` reads
  `void_window_ends_at` and correctly declines to recommend a paid exchange inside it
  (`Reason.IN_VOID_WINDOW`), reasoning a free void-and-rebook would be better. Nothing
  performs that void — there is no third action alongside `exchange`/`cancel`. Needs
  its own action type, route, and execution path, wrapped in the same
  `execution_attempts` idempotency guard.
- **Nothing resolves a failed capture automatically.** `payment_capture_failed_at`/
  `payment_capture_error` are visible on the order and on `/orders`; clearing them is
  a manual database write today. Fine at current volume, not at a hundred bookings a
  week.
- **Test isolation.** `carrier_change_capability` is global and unscoped by design
  (carrier behaviour isn't tenant-specific), and `book()`'s
  `carrier_capability_record()` write is real and unmocked in tests. This already
  corrupted live reference data once — a fixture defaulting to a real carrier code
  quietly inflated its denial count before it was caught; the fixture default is now a
  non-IATA marker (`T1`), which fixes that one instance and not the class. **Tests can
  still corrupt live ranking data** if a future fixture reuses a real IATA code.
  Resolve before CI exists.
- **`audit_events` grows permanently from test runs.** No FK to a test-scoped
  account that gets cleaned up, append-only by design, can't be pruned. Correct
  behaviour, compounding cost.
- **RLS is enabled per-table, not enforced by automation.** Migration 014
  retroactively enabled RLS (and revoked default grants) on every table that existed
  at the time, and closed the default-grants hole for tables created afterward — but
  RLS itself has no equivalent automatic default. Every migration since has had to
  remember its own `ENABLE ROW LEVEL SECURITY`; nothing tests that a new one did.
- **The exchange path is unverifiable in sandbox**, and so is the difference band's
  stepped baseline. Both are real, tested machinery (`app.py:execution_steps`,
  `difference_band`'s `baseline_steps`) that has only ever run against a mocked
  `change_total_amount`, because Duffel's sandbox never returns a negative one. Same
  root cause as the exchange path itself being unverifiable — see `CLAUDE.md`.
- **`CARRIER_SINGLE_DENIAL` is unexercised in practice.** The eligibility tier exists
  and is covered by tests (`test_rank_price_gives_no_boost_to_a_single_carrier_denial`),
  but as of this writing no real carrier in `carrier_change_capability` sits at
  exactly one denial with zero confirms — carriers seen so far are either clean
  (confirmed ≥ 1) or excluded outright (≥ 2 denials, 0 confirms, e.g. Iberia).
- **Commission-rate drift.** `templates/onboarding_payment.html` still hardcodes 25%
  in both copy and its client-side math, and doesn't receive the account's real
  `commission_rate` from `app.py:onboarding_payment()` at all.
  `templates/orders.html` prints a static "Our fee (25%)" label next to a correctly
  computed dollar figure. Both contradict the "never hardcode 25%" rule in
  `CLAUDE.md` and should be fixed the same way `trip.html`/`wallet.html` already were.
- **`orders.refunded` is aggregated across orders in one place.** `db.monthly_series()`
  sums `orders.refunded` for the Trips page's "Spend & recovered" chart's "Recovered"
  line — the exact cross-order aggregation `CLAUDE.md` says never happens.
  `savings_events` is canonical everywhere else (Overview, Wallet, Invoices); this
  chart is the one holdout and should be pointed at the same source.

## Retired infrastructure, deliberately left in place

`parsing.py`, `POST /webhooks/resend/inbound`, `email_import_sources`, and
`/auth/gmail/start` are bolt-on-era code from a pivot that was later reversed back to
merchant-of-record. See `CLAUDE.md`'s "What's dormant, not deleted" — they're live,
routed, and partly functional (the webhook), not dead code sitting unrouted.
`orders.source` retains `manual` (an off-platform reservation, watched but not
executable — see the "owner of record" constraint) and `td_rebook`; `email_import`
and `forward` are permitted by the CHECK constraint but have zero rows ever written,
since the one live capture path (the Resend webhook) tags everything it creates as
`manual`.

## Duffel sandbox behaviour worth knowing before touching the reshop path

Carried forward from the original API spike (`FINDINGS.md`, now retired — this is
the part of it still load-bearing):

- **`change_total_amount` is a hardcoded stub in test mode: always exactly
  `+125.00`**, regardless of fare, carrier, route, cabin, or date. Confirmed across a
  33× price range, a business→economy downgrade, a four-month date shift, and
  multiple carriers — even "changing" to the exact flight already booked returns
  `+125.00`. Duffel's own docs describe `change_total_amount` as possibly negative
  ("may be negative to reflect a refund") with a defined behavioural branch for it, so
  the mechanism is documented and structurally supported — sandbox just can't compute
  it. This is the single largest unvalidated assumption in the business model; closing
  it needs live credentials and a real fare drop on a cheap refundable domestic route.
- **Which carriers support the change flow in sandbox** (per-carrier, not
  universal — the field that tells you is `available_actions`, not `conditions`):
  Duffel Airways (`ZZ`), British Airways (`BA`), American Airlines (`AA`), and TAP Air
  Portugal (`TP`) all expose `change` and changes work. **Iberia (`IB`) does not
  expose a `change` action at all** and 422s on any change attempt, despite
  `conditions.change_before_departure.allowed: true`. `conditions` also varies
  between two orders on the same carrier and route — it appears to track fare brand,
  not carrier, which is a further reason not to build logic on it.
- Offer requests are single-use (`orq_...`); order-change offers expire in **3 days**,
  cancellation quotes in **1 hour**. `LHR → LTN` triggers *airline-initiated* changes,
  a different endpoint from the customer-initiated flow this app uses — don't conflate
  them. `LTN → SYD` is a documented sandbox trigger for cancellations refunding to
  `airline_credits` rather than the original payment method.
