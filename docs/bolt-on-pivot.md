# The bolt-on pivot

TripDifference pivoted on 2026-08-20 from being the original booking agent
(buy the ticket via Duffel, front the cost, TD-card funding) to a bolt-on
model: the customer books wherever they normally do, we capture the
confirmation (Gmail OAuth or forward-to-address), monitor the fare, and when
it drops on an eligible cash/non-basic-economy fare, we execute recovery —
either cancel-old-and-rebook-new (netting a refund to the customer's card) or
a cancel-for-credit against the airline's own policy (netting a credit to the
customer's loyalty account). We take 25% of realized savings, billed only
after savings are confirmed.

This doc exists because that pivot happened in planning conversations, not in
this repo, and a prior session had to reconstruct the target model from pasted
text with no durable copy anywhere in the codebase. Keeping it here means the
next session doesn't depend on a relay through another conversation.

It has three parts: the original target spec, the reconciliation against what
this codebase actually had at pivot time, and the resulting keep/repurpose/
net-new plan. Treat all three as a snapshot of 2026-08-20 reasoning, not a
live source of truth — once code and schema diverge from what's described
here, the code wins.

---

## 1. Target spec (bolt-on data model and UI)

*(Originally an adaptation of a fresh-build brief modeled on a competitor's
screenshotted UI/UX, before this repo's existing state was known.)*

**Data model** — first-class entities (names may not match this repo's
column/table names 1:1; see §2 for the mapping):

- **Account** — the login/owner. One primary payment method (Stripe
  SetupIntent, off-session-enabled), zero-or-more Travelers.
- **Traveler** — belongs to an Account. Full name, DOB, nationality/passport
  country, email, phone. `loyalty_programs` (airline + member number, array).
  `trusted_traveler` block: KTN, Redress Number, Canadian Travel Number,
  CLEAR Plus (bool). `preferences` block: home airport, seat preference,
  preferred airline. Computed stat rollups: flight count, hotel count.
- **EmailImportSource** — belongs to an Account. OAuth-connected Gmail
  address, or a manually-added forwarding address authorized to send to the
  account's forward-to address (`trips+{account_id}@tripdifference.com`).
- **Reservation** — belongs to an Account and one Traveler. type
  (flight/hotel), booking reference/PNR, airline/chain, amount_paid,
  currency, added_at, source (`email_import | manual | forward`). Active
  repricing requires a linked payment method.
- **FlightSegment** — belongs to a Reservation. Airline, flight number,
  cabin, fare_type (`cash | points` — only cash is eligible), origin/dest,
  departure/arrival, stop count.
- **PriceObservation** — belongs to a Reservation (ideally reusable by
  flight-number/route). Timestamp, price, source (`live_check |
  historical_api`). Feeds the price chart and eligibility.
- **SavingsEvent** — belongs to a Reservation. old_amount, new_amount,
  realized_savings, delivery_type (`refund_to_card | airline_credit`),
  delivery_detail, commission_amount (25% of realized_savings, rate not
  hardcoded globally — see PRO-tier note below), commission_charged_at,
  status (`pending | completed | failed | disputed`).
- **ActivityLogEntry** — belongs to a Reservation. timestamp, title,
  description. The audit trail; the Activity Timeline UI reads directly from
  it, not from a separate display list.

**Core business rules**

1. **Card-gating** — a Reservation cannot have active repricing until the
   Account has a valid payment method on file. Enforced everywhere the state
   is shown, never silently bypassed.
2. **Eligibility** — only cash-fare, non-basic-economy segments are
   repricing-eligible. Real logic tied to `FlightSegment.fare_type`, not a
   cosmetic tag.
3. **Dual savings delivery** — the UI must say which mode applies, never a
   generic "you saved $X":
   - `refund_to_card`: cancel-old + rebook-new, netting a card refund. Copy:
     *"Refunded $X to your card ending in {last4}."*
   - `airline_credit`: cancel-for-credit against airline policy, netting a
     loyalty-account credit. Copy: *"Credited $X to your {Airline} account."*
   - Which path applies is decided by execution logic at SavingsEvent time —
     the UI surfaces whichever one actually happened, per event, accurately.
4. **Billing** — the 25% commission charges only after a SavingsEvent reaches
   `completed` status. Never on `pending`, never speculatively.

**Screens** (nav: Home, Reservations, Savings, Travelers, Settings; account
switcher + onboarding-progress widget pinned in the sidebar):

- **Home** — greeting; dominant "Link a card to start saving" headline when
  no payment method; KPI row (Bookings/Total Spent/Total Saved); saved-over-
  time trend; Upcoming Reservations; Activity Timeline; Airline/Hotel
  Breakdown panels; Add Reservation modal (Flights/Hotels tabs, booking
  number, traveler picker, airline — no payment setup in this modal).
- **Reservations list** — KPI row, filters, Upcoming/Past/All tabs + search,
  table with a status tooltip ("Repricing unavailable — Link a card...")
  when ungated.
- **Reservation detail** — title/subline, repricing toggle (disabled without
  a card), Refresh button, ungated banner, price panel (masked pre-card),
  Flight Details with tag chips (stops/cabin/fare type), explicit
  SavingsEvents with delivery-type-specific copy, Activity Timeline.
- **Travelers list** — one card per Traveler (avatar, DOB, stat counters,
  condensed Trusted Traveler + Loyalty summary, Total Savings), "Add
  Traveler" button (a deliberate addition vs. the reference product).
- **Traveler detail** — header + left summary card; four independently
  editable panels (pencil icon each, not one page-level edit mode):
  Personal Information, Loyalty Information, Trusted Traveler, Preferences.
- **Settings** — Account-level fields (email/phone/nickname, distinct from
  per-Traveler data); Manage Reservation Imports (Google OAuth status +
  resync, forwarding addresses).

**Explicitly out of scope for the pass this spec described**: PRO/subscription
tier upsell flow (but the data model — e.g. commission rate per-row, not a
single global constant — shouldn't fight adding it later); a defined brand/
visual system (neutral components only); airline-side execution automation
beyond what's stubbed/mocked behind the SavingsEvent model.

---

## 2. Reconciliation against this repo, as of 2026-08-20

This repo is a mature Flask app (Postgres via Supabase/Vercel Marketplace,
real Duffel integration, argon2id auth, CSRF, 28 routes at pivot time) built
for the *original* merchant-of-record model — TD buys and holds the ticket.
The vocabulary (`orders`/`trips`/`wallet`/`decisions`) reflects that.

**Schema at pivot time**: `accounts`, `users`, `sessions`, `orders` (route/
carrier/paid/monitoring/`raw` jsonb Duffel payload/etc.), `audit_events`
(append-only, DB-trigger-enforced), `execution_attempts` (idempotency via
`UNIQUE(order_id, action, change_offer_id)`), `travelers` (name/DOB/email/
phone only). No hotel support anywhere. No Stripe dependency — `CARD` in
`app.py` was a hardcoded fake constant standing in for a card-on-file that
never actually existed.

**Entity mapping**:

| Target | Status at pivot |
|---|---|
| Account | Exists (`accounts`) as-is |
| Traveler | Partial — name/DOB/email/phone only; no loyalty/trusted-traveler/preferences |
| Reservation | Exists as `orders`, semantically inverted (TD-purchased, not customer-sourced); flights only |
| FlightSegment | Not a table — derived live from `orders.raw` (only works because every order has a real Duffel payload) |
| PriceObservation | Informal — `audit_events.market_best`, 1:1 with one order, no reusable table |
| SavingsEvent | No table, but the *mechanism* is proven: `duffel_reshop_test.py` already validates the `refund_to`/`airline_credits` branch on Duffel's cancellation response that maps directly onto `delivery_type` |
| ActivityLogEntry | Exists (`audit_events`) — already the audit trail, minimal reshape needed |
| EmailImportSource | Doesn't exist — no Gmail OAuth, no forwarding, no parser |

**Key finding**: the dual-delivery-type distinction (`refund_to_card` vs.
`airline_credit`) is not hypothetical. `duffel_reshop_test.py` deliberately
tests a cancellation route (`LTN→SYD`) specifically because it's "Duffel's
documented sandbox trigger for refunds landing in `airline_credits` rather
than going back to the original payment method." `execute()` in `app.py`
already branches on the Duffel primitives (`order_changes` for exchange,
`order_cancellations` for cancel) that this distinction rides on — what was
missing was recording *which* one fired as a typed fact.

**What `wallet` represents**: already a correct customer-facing savings
ledger, not a TD-funded balance — `wallet_transactions()`'s own comment says
so ("A ledger, not a stored balance... there is no float being held here").
Only the `"Flight booked"` charge-row kind was pivot-stale, since it recorded
TD paying Duffel for the original ticket.

**What `orders` represents**: a ticket TD purchased (merchant-of-record).
Needed to become: the customer's existing booking that TD monitors, and —
when the internal rebook mechanism runs — the record of a fresh, cheaper
ticket TD purchases against the *customer's* stored card, not TD's balance.

**Gaps with nothing bolt-on-shaped yet**: real payment method on file (no
Stripe anywhere), the Email Capture Layer, `fare_type` (cash/points)
eligibility (every order was axiomatically cash pre-pivot), hotels, Traveler
loyalty/trusted-traveler/preferences fields, SavingsEvent as a queryable
typed fact, Settings/onboarding-widget/account-switcher screens, and the
book-new-before-cancel-old execution path (current `execute()` only does an
in-place Duffel order *change* or a straight *cancellation* — never book a
fresh order on the customer's card and cancel the old one).

---

## 3. Keep / repurpose / net-new plan

**Keep essentially as-is**: `wallet`/`wallet_transactions()` (correct
ledger); `audit_events` (already the ActivityLogEntry equivalent — UI should
read it directly, not rebuild it); `execution_attempts` idempotency guard
(should gate any new execution paths too); the Duffel balance/`duffel_http`
plumbing (repurposed, not discarded).

**Repurpose, don't delete**: `/search`, `/book/passenger`, `/book/payment`,
`/book` become the mechanism for purchasing the new, cheaper ticket during
rebook execution — not a customer-facing "book with us" entry point.
Eventual payment source: charge the customer's stored card first (once
Stripe exists), then fund the actual Duffel purchase via TD's existing
balance as a transient pass-through, not a standing float. Gate this flow to
internal-only once the book-new-before-cancel-old execution path exists to
call it — **not done yet**; see open items below. `orders` keeps its shape
(route/carrier/paid/monitoring/`raw`), with provenance now distinguished by
`source` (`email_import | manual | forward | td_rebook`).

**Net-new, in priority order**:

1. Stripe integration (SetupIntent, off-session charge, replace the `CARD`
   constant) — the core blocker for card-gating. **Not started**; requires
   provisioning via the Vercel Marketplace before any code lands.
2. Email Capture Layer (Gmail OAuth + forwarding address + parser) — as
   load-bearing as Stripe, just less visible since nothing depends on it
   yet. **Provisioning started, build explicitly held 2026-08-21**: Resend
   (`resend/resend-email`) is installed against `tripdifference.com` on
   GoDaddy (sending DNS records issued, receiving/inbound not yet enabled —
   that requires a dashboard step in Resend to generate the MX record, which
   also needs a subdomain decision to avoid colliding with any future
   company email on the root domain). No webhook endpoint, `EmailImportSource`
   table, or parsing code has been built. **Explicitly paused**: building the
   forward-to-address/account-matching pipeline now was judged premature
   ahead of settling SSO/auth direction, since forward-to addresses and
   email-based account identity are tangled up with how accounts get
   authenticated. Revisit once that's decided — don't resume this build
   without re-confirming it doesn't fight whatever SSO approach gets chosen.
3. `fare_type` (cash vs. points) wired into the existing `eligibility.py`
   check, not a parallel eligibility system. **Shipped 2026-08-20**:
   migration 009 (`orders.fare_type`, default `'cash'`), `eligibility.assess()`
   takes a `fare_type` kwarg and gates on it first (a points fare is
   `NOT_ELIGIBLE`/`POINTS_FARE` regardless of change conditions), threaded
   through `OrderSnapshot.from_duffel()`, `snapshot_of()`, `trip_view()`, and
   `toggle_monitor()`. `book()` sets `fare_type: "cash"` explicitly, since
   Duffel's cash-offer search is the only thing that route can ever produce.
   The gate is real today even though nothing points-fare-shaped can enter
   the system until the Email Capture Layer exists — it's ready for when it
   does, per the "build now" instruction rather than waiting on #2.
4. `SavingsEvent` as a typed, queryable fact — populated from the branch
   logic `execute()` already has. **Shipped 2026-08-20**: `savings_events`
   table (migration 008), `db.savings_event_create`/`savings_events_for_order`,
   wired into `execute()`'s exchange and cancel branches, rendered on the
   trip detail page with the delivery-type-specific copy from business rule 3.
5. Traveler fields — loyalty_programs, trusted-traveler block, preferences,
   nationality. **Shipped 2026-08-20**: migration 007, `db.py` CRUD, and
   the traveler list/edit form in `templates/travelers.html`. The full
   spec'd four-panel Traveler *detail* page (pencil-icon-per-panel editing,
   as its own route) is not built — the fields currently live on the
   existing combined list+form page.
6. Missing screens — Settings (incl. Manage Reservation Imports once the
   Capture Layer exists), onboarding-progress widget, account switcher,
   Traveler detail page. **Not started.**

Also shipped 2026-08-20 as part of item 4/repurposing: `orders.source`
column (migration 008, backfilled `td_rebook` for every pre-pivot row); the
wallet's `"Flight booked"` charge row now only fires real money movement for
`source = 'td_rebook'` rows, and shows a zero-amount `"Reservation added"`
row otherwise.

**Explicitly deferred, not forgotten**: hotels (zero support — no table, no
routes, no templates; recommended to defer rather than half-build); promoting
`PriceObservation` to a cross-reservation, reusable-by-flight-number table
(only matters once the flight-number historical-data ambition is actually
prioritized).

**Open before the repurposed `/book*` flow can actually be gated
internal-only**: the book-new-before-cancel-old execution path doesn't exist
yet. Hiding the current `/book*` routes from navigation now — before that
internal caller exists — would leave the app with no way to book anything.
This needs its own build (a third execution action alongside `exchange`/
`cancel`, wrapped in the same `execution_attempts` idempotency guard) before
the routes can be safely regated.

---

## 4. How we're building it (confirmed plan, 2026-08-20)

This is the standing build plan, superseding any Duffel-as-purchaser /
TD-card-funding framing in earlier docs (`td-card-and-fare-timing-approach.md`'s
TD-card section is retired; its fare-timing section still applies and is
folded into the reshop engine work above). Superseded reasoning is kept in
git/doc history rather than deleted.

**Three cooperating systems**: the App (customer dashboard — no booking
intake, since we don't buy tickets), the Capture Layer (new — Gmail OAuth
and/or forward-to-address ingestion, confirmation parsing, and the seed
source for flight-number-level historical fare data at near-zero marginal
cost), the Reshop/Repricing Engine (existing IP — `engine.py`/`execute()`
already have the decision/execution branching; §3 item 4 closed the gap of
recording which delivery path fired), and Billing (Stripe only, one money
rail — TD never moves money to an airline under this model).

**90-day shape**: Phase 1 (Weeks 1-4) — Stripe, Capture Layer, fare-type
eligibility, traveler field extensions. Phase 2 (Weeks 5-8) — promote the
execution mechanism into typed `SavingsEvent` records, build
book-new-before-cancel-old by repurposing `/book` with its payment source
changed to the customer's card. Phase 3 (Weeks 9-12) — dry-run validation,
hardening, live pilot across 2-3 accounts.

As of 2026-08-20, from Phase 1: fare-type eligibility and traveler field
extensions are shipped (§3 items 3 and 5); Stripe and the Capture Layer are
not started. From Phase 2: the SavingsEvent promotion is shipped (§3 item 4);
book-new-before-cancel-old is not started.

**Pre-build gates** (must clear before heavy build on the remaining items):

1. Partner alignment on the three buckets.
2. Per-airline execution policy research in *production* — which airlines
   permit third-party price-drop claims/rebooking in practice, not just in
   Duffel's sandbox (the dual-delivery mechanism is sandbox-validated per §2,
   which de-risks this but does not clear it).
3. Entity/domain confirmed (TripDifference vs. FareDifference; ownership).
4. 83(b) mechanics locked before any equity shares are issued.
5. Fare-history API vendor selected, for route-level cold-start data —
   flight-number-level data now comes primarily from captured confirmations.
6. Email-parsing approach settled: Gmail OAuth verification requirements for
   sensitive scopes (Google's review process takes real time — start early)
   plus confirmation-email parsing coverage across major airlines/OTAs.

Retired: TD-card float sizing/reconciliation gate — moot under the bolt-on
model, independently confirmed moot by `wallet`'s ledger design (§2), which
never held a float in the first place.

**Cut, not deleted**: the Duffel-as-purchaser/TD-card-funding model as a
*customer-facing* flow. The underlying booking mechanism is repurposed
internally (§3), not removed.

**Still deferred**: hotels — no existing support, don't half-build alongside
flight-focused work.
