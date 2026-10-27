# TripDifference

TripDifference is **merchant of record**. A corporate travel manager books flights for
employees through the portal; we issue the ticket via Duffel, funded from TD's own
Duffel balance, charged to the company's card via Stripe (`app.py:book()`); we then
monitor the fare and exchange to cheaper identical flights, billing a commission on
what's actually recovered (`accounts.commission_rate`, default 25%). Revenue is a
percentage of realized savings only — there is no subscription
(`accounts.subscription_fee` is deprecated and never billed).

**Only the ticket's owner of record can exchange it.** That single constraint is why
we issue the ticket rather than bolting onto someone else's booking, and it's the
foundation of the entire architecture. A reservation added by hand or captured from a
forwarded confirmation email (`orders.source = 'manual'`, no real Duffel order behind
it) can be *watched* — a market price gets recorded — but can never be *exchanged*:
`engine.evaluate()` skips it with `NO_EXECUTION_MECHANISM` because there's no order to
quote a change against. An agent that doesn't understand this will propose the wrong
thing — e.g. "just cancel and rebook on the customer's behalf" for a booking TD never
issued.

See `docs/architecture.md` for the four-layer breakdown, the table-by-table schema
notes, and the list of known, deliberate gaps.

## Constraints that bite

### Duffel

- Production is sandbox-only; staging may search live and, only if told to, book live.
  `APP_ENV` (`config.py`) is `dev | staging | production`, and missing or unknown means
  production — except on a Vercel Preview deployment, which refuses to start without an
  explicit `APP_ENV` (`config.startup_check`). Two flags, both staging-only (either one
  true elsewhere fails startup), and either one on staging requires a `duffel_live_`
  token: `DUFFEL_LIVE_SEARCH_ENABLED` lets it make offer requests and fetch offers;
  `DUFFEL_LIVE_ORDERS_ENABLED` (default false) allows orders. While it is false,
  **staging creates, changes and cancels nothing, live or sandbox** — `book()` and
  `execute()` refuse up front and `duffel_http.request()` refuses every non-search call.
  `app.py` runs `config.startup_check()`, `duffel_http.startup_check()`,
  `billing.startup_check()` (staging needs an `sk_test_` Stripe key — or
  `STRIPE_LIVE_ENABLED=true`, staging only, which charges real cards through
  `STRIPE_LIVE_SECRET_KEY`/`STRIPE_LIVE_PUBLISHABLE_KEY` and needs that live pair over a
  live Duffel token; `config.STRIPE_SECRET_KEY` is the key in use, never read the
  environment variable directly) and
  `db.startup_check()` (staging must be on Supabase project `bcqzwnoifwkuimrrdysy`,
  production must not) at import. `GET /healthz/env` shows the result without secrets. Every offer request goes through `live_search`: a live
  one is counted against `STAGING_MAX_MONTHLY_SEARCHES` in `live_search_log` first, and
  `duffel_http` refuses a live offer request without that single-use authorization.
  Every live spend (booking, exchange top-up) goes through `live_guard.reserve()` —
  allowlist, USD-only, per-order and daily caps summed from the `live_spend` ledger,
  and never a `__pytest__` account — and `duffel_http.request()` refuses a live-token
  payment that no reservation covers.
  Live orders are `orders.duffel_mode = 'live'`, set once at creation; the database
  refuses to delete one, or the account that owns it (migration 030). The two
  standalone scripts, `duffel.py:48-51` and `duffel_reshop_test.py:173-183`, still reject
  anything but `duffel_test_` — independent guards, not layered on the app's; don't
  route around them.
- Sandbox hardcodes `change_total_amount` at **+125.00** regardless of fare, carrier,
  route, cabin, or date — verified across a 33× price range, a business→economy
  downgrade, and multiple carriers (see `docs/architecture.md`'s Duffel sandbox
  section for the full survey). The economic model cannot be validated in sandbox,
  and `engine.evaluate()`'s own gate
  (`change_total >= 0` → skip) means no real exchange can ever complete there. This is
  also why the difference band's dashed baseline has never been seen to step down
  outside a test — the machinery for it exists (`app.py:difference_band` /
  `execution_steps`) and is exercised only by mocked tests.
- Offer requests (`orq_`) are **single-use**. Booking any offer burns the whole
  request — Duffel returns `offer_request_already_booked` on reuse. Nothing may hold
  an offer for later purchase; `search()` issues a fresh request every time
  (`app.py:906-911`), and a policy-approval hold snapshots the itinerary and price
  rather than an offer_id (`app.py:1211-1225`).
- **Order Change, not cancel-then-rebook.** `execute()`'s exchange branch calls
  `POST /air/order_changes` then confirms it — atomic, no double-booking window.
- 429s return `ratelimit-reset` as an **RFC 2616 date**, not `Retry-After` as an
  integer. `duffel_http.py` checks `Retry-After` first (harmless if absent), falls
  back to date-parsing `ratelimit-reset`, retries once, then gives up
  (`duffel_http.py:59-109`).

### Stays (hotels)

- **Not enabled on this Duffel account, and nothing books a stay yet.** Every `/stays`
  call answers `403` with a plain-text body (`stays.StaysNotEnabled`); access is
  requested from Duffel. `stays.py` (calls and value objects) and `stays_engine.py` (the
  rebook decision) exist and are tested against payloads built from Duffel's published
  client types — **no real response has been seen**, and there is no route, table or
  execution path for a stay.
- **Sandbox-only.** `duffel_http._check_request` refuses any non-`/air` path made with a
  live token. A stay paid from the Duffel balance carries no `payment` object, so the
  live spend check (which reads the payment in the body) would otherwise pass it with no
  `live_guard` reservation. Lifting this needs a spend guard that understands a Stays
  booking, not a path added to an allowlist.
- **No exchange — book the cheaper rate, then cancel the old booking.** There is no
  change endpoint. The saving is `refund if cancelled now − new total`, read from the
  booked rate's `cancellation_timeline`, never `paid − new total`
  (`test_the_saving_is_measured_from_the_refund_not_from_what_was_paid`).
- **Same room on terms at least as good, or it is not a saving** (`stays_engine.not_comparable`):
  same room name (Duffel gives rooms no stable id), board no worse, cancellation no
  stricter, same loyalty programme, and payable in full from the balance — a `guarantee`,
  `deposit` or card-only rate is settled by the hotel, which makes the hotel merchant of
  record, not TD.
- A refund on a cancelled stay returns to **TD's Duffel balance**, not the company's
  card. Passing it on needs a Stripe refund, and `billing.py` has none.

### Eligibility

- `available_actions` is the truth about whether a ticket can be changed, and Duffel
  **only exposes it once an order exists**. There is no pre-purchase equivalent.
- `conditions.change_before_departure` **lies in both directions** — Iberia reports
  `allowed: true` with no `change` action and 422s; Duffel Airways and British Airways
  report `allowed: false` while changes work fine. Never trust it over
  `available_actions`; it's a fallback only when `available_actions` is absent
  entirely (`engine.py:191-210`). Locked in by `test_available_actions_beats_lying_conditions_block`
  and `test_conditions_false_does_not_veto_when_actions_allow`.
- Therefore an **offer can never reach `MONITORING`**. Pre-purchase the honest states
  are `LIKELY_MONITORING` and `NOT_ELIGIBLE` (`eligibility.py`). Any UI showing a
  confirmed "Monitored" tag on an offer is a bug — `offer_view()`'s `monitorable` is a
  derived `should_poll` boolean for ranking/filtering, not a stand-in for a confirmed
  state; templates must branch on `eligibility_state`/`eligibility_reason`.
- `carrier_change_capability` records observed carrier behaviour (real
  `available_actions` seen on real orders). It is **global and unscoped** — no
  `account_id` column, deliberately, since carrier behaviour isn't tenant-specific.
  `ZZ` is Duffel Airways, a synthetic sandbox carrier, flagged `is_synthetic` and not
  evidence about real airlines.
- Every route requires a signed-in session unless its endpoint is in
  `app.PUBLIC_ENDPOINTS` (landing, login, signup, Google auth callbacks, static, and the
  signature-authenticated Resend webhook); `app._require_login` enforces that before any
  view. `test_routes.py` fails on any other anonymously reachable route — adding a public
  one means editing both.
- Roles exist on `users.role` (`admin | booker | approver | finance`, migration 016)
  but nothing enforces them yet — `auth.role_required` is defined and unused by any
  route. Don't assume role gating is live anywhere in the UI.

### Money

- **Three exchange outcomes**, not two: cash to the original payment method
  (`refund_to_card`), credit **locked to the individual traveller's name**
  (`airline_credit`), or **forfeited**. Forfeiture is never chosen — it's what a
  `$0.00` refund from Duffel gets classified as, and it is never billable
  (`savings_events.delivery_type` CHECK constraint; a forfeited event's
  `commission_amount` is always `0.00`).
- **Commission is on rebooking only.** `savings_events.action` (`exchange | cancel`,
  migration 034) says which an event was. A cancellation is the customer's own money
  coming back: `db.savings_event_create` writes it at a rate of zero whatever the
  account's rate, it is never invoiced or charged, the wallet words it "Booking
  cancelled", and it is left out of every recovered/saved total. Don't count a
  `cancel` event as a saving anywhere.
- **The fee is charged to the card on file when a rebooking delivers its saving**
  (`app._charge_commission` → `billing.charge_commission`, migration 035): one Stripe
  charge of the event's `commission_amount`, keyed on the savings event. Only after the
  saving has reached the customer — for cash that means `_pay_out` succeeded; airline
  credit counts as delivered. It never raises: a charge that fails is recorded on the
  event (`commission_charge_failed_at`/`_error`) and stays invoiceable, and one that
  succeeds stamps `commission_charged_at`, which `db.generate_invoice` skips. The fee
  is never netted out of the refund.
- **Cash and credit never net against each other.** `invoice_lines.amount CHECK
  (amount >= 0)` makes that structurally impossible; don't work around it.
- **A cash refund does not reach the company's card by itself.** The ticket was bought
  from TD's Duffel balance, so that is where Duffel returns the money; `_pay_out()`
  (`app.py`) passes it on with `billing.refund_to_card` — a partial Stripe refund of
  the order's own PaymentIntent, the whole recovery, keyed on the savings event so a
  retry cannot pay twice. It runs after the order is marked executed and never raises:
  a payout that fails is recorded on the event (`payout_failed_at`/`payout_error`,
  migration 033) and the exchange stands. Airline credit and forfeiture send nothing.
  The refund **never appears as an invoice line** — only the commission on it does.
  Not yet seen against a real Duffel refund: that the money lands in the balance is
  read from how balance payments work, not from a live response.
- **A saved card belongs to the Stripe mode it was saved under**
  (`accounts.stripe_livemode`, migration 036). `db.account_stripe_ids` and
  `db.account_card` return only ids of the mode the key in use is in, so a test card
  reads as nothing on file under a live key; `billing.ensure_customer` then creates a
  customer in that mode, which clears the old card. Under a live key a `__pytest__`
  account is never charged (`billing._card_to_charge`).
- **`savings_events` is canonical for recovery reporting.** `orders.refunded` is a
  convenience field on that one order — never aggregate it across orders for a
  reported total.
- Commission reads from `accounts.commission_rate` at the point of billing. Never
  hardcode 25% in code or template copy. One deliberate exception: `engine.py`'s
  `SERVICE_FEE_RATE` constant (`0.25`) powers only the reshop-cycle's *preview* text
  in the decision log ("our fee 11.25 (25%)") — a diagnostic, not a bill. Threading
  the real per-account rate through that preview is a separate, not-yet-done change;
  the actual charge always reads the account's real rate (`execute()` →
  `db.account_commission_rate`).
- Ticket purchase is authorize (`app.py:1239`) → create Duffel order (`:1247`) →
  persist the order row (`:1299`) → capture (`:1359`). **Never charge-then-refund** —
  an authorization that's cancelled on Duffel failure never appears on the customer's
  statement. The order row is written *before* the capture attempt, so a capture
  failure (`payment_capture_failed_at`/`payment_capture_error`, migration 026) leaves
  a recoverable record rather than an untracked ticket.

### Schema and safety

- Every table has RLS enabled — but **not from an automated mechanism**. Migration
  014 retroactively enabled RLS (and revoked `anon`/`authenticated` grants) on every
  table that existed at the time, and changed this project's own `ALTER DEFAULT
  PRIVILEGES` so tables the `postgres` role creates from here on don't reopen those
  grants. That only covers *grants*, not RLS itself: **every migration since 014 has
  had to (and has) included its own explicit `ENABLE ROW LEVEL SECURITY`** — there is
  no event trigger or other automation that does this for a new table. Forgetting it
  in a future migration would silently leave that table's RLS off; nothing currently
  tests for this the way schema-drift is tested (below). Check before adding a table.
- `upsert_order()` filters writes through `_ORDER_COLS`, and `traveler_save()` through
  a combined field list including `default_cost_center_id`. **Adding a schema column
  is not enough** — a silent drop is the failure mode, and it has happened twice
  (`orders`, then `travelers.default_cost_center_id`). Both are covered by
  schema-drift tests (`test_order_cols_covers_every_writable_column`,
  `test_traveler_fields_covers_every_writable_column`) that derive their expected
  column list from `information_schema`, not a hand-written one.
- Functions that read tenant data take `account_id` as a **required** parameter.
  `db.audit_rows_for_account()` and `db.invoice_lines_for()` were both tightened this
  way after `/decisions` was found leaking every account's rows to any authenticated
  user. Don't reintroduce an unscoped mode.

### Scouts and referral codes

- **No code, no account.** Every signup — password (`/signup`) or Google (`/signup/google`,
  then the callback) — needs a real referral code (`referrals.resolve`); there is no public
  code. `accounts.referral_code_id`, `scout_id`, `scout_rate`, `referred_at` are `NOT NULL`
  (migration 038), and `db.create_account` takes `referral=` as a required keyword. Accounts
  from before 038 are on `LEGACY`; a scout applicant who isn't a customer is on
  `SCOUT-APPLICANT`. Both are internal codes of the one system scout, rate 0, refused at
  signup. A test account uses `db.internal_referral_code("LEGACY")`.
- **The `/r/<code>` lock is the server's, not the form's.** `td_ref` is a signed cookie; while
  it is present a different form code is refused and a cookie that doesn't verify is refused.
- **Attribution is fixed at signup by a trigger.** The only way past it is
  `db.account_attribution_override`, which writes its `audit_events` row in the same
  transaction. `audit_events` rows of kind `referral`/`scout` describe an account:
  `order_id = ''`, `account_id` set.
- **A scout earns only on commission actually collected** — `_charge_commission`'s success
  path calls `_accrue_scout_earning`, and `/cron/scouts` catches up any it missed
  (`db._EARNING_INSERT` is the rule: `exchange`, `commission_charged_at` set, rate > 0, not a
  terminated scout). One earning and at most one reversal per savings event and `kind`
  (unique key `(savings_event_id, kind, entry)`).
  A refunded or disputed commission (Stripe webhook, matched on
  `commission_payment_intent_id`) reverses an unpaid earning or writes a negative row that
  nets against the next payout.
- **Payouts are USD-only, and live ones are behind a switch.** `billing.transfer_to_scout`
  refuses under a live key unless `SCOUT_LIVE_PAYOUTS_ENABLED=true` (default false). Only
  staging can hold a live key (`STRIPE_LIVE_ENABLED`), so that is the only place it applies. `/cron/scouts` runs daily and pays on the 1st (UTC); like `/cron/reshop` it only fires
  on production. Connect has to be enabled in the Stripe dashboard — it isn't yet.
- **Staff is `users.is_staff`, set by hand in SQL** (`auth.staff_required`, `/admin/scouts`).
  `users.role = 'admin'` is every customer's role in their own account and grants nothing here.
- **A scout sees earnings, never the client's travel.** `db.scout_referrals` returns no trip,
  route, traveler or fare data; keep it that way.
- **Master scouts earn an override, one level deep** (migration 041). An approved scout's
  invite link is `/join/<INVITE-code>` (`scout_invite_codes`, a table of its own — an invite
  never opens a business account, a business code never attributes a scout; `td_scout_ref`
  cookie, whose code wins over the form's). The inviter is attributed only when the
  application is approved (`scouts.introduced_by_scout_id`, `override_rate` snapshotted from
  the inviter's `default_override_rate`, 20%); after that a trigger fixes it and only
  `db.scout_introduction_override` changes it, audited. The override is a `scout_earnings` row
  of `kind = 'override'` whose `parent_earning_id` is a `direct` earning — never another
  override — written beside it by `_accrue_scout_earning` and the `/cron/scouts` catch-up,
  only while `MASTER_OVERRIDE_ENABLED=true`, only while the parent is still `pending`. TD
  funds it: the scout's own earning never shrinks. A refund or dispute undoes each row by its
  own state. Override rows never name the business — it's the other scout's client.

### Behaviour

- **A human confirms every exchange and cancel, unless autopilot is on.** `execute()`
  requires a typed CONFIRM (`confirm_text == "CONFIRM"`). With
  `RESHOP_AUTOPILOT_ENABLED=true` (default false) every recheck — the scheduler and
  the "Recheck all fares" button — calls the same `_execute_action` with
  `unattended=True` for an order whose cycle just decided RESHOP.
- **An unattended exchange never pays.** It acts only on the engine's own RESHOP
  decision, and stops without confirming if the change Duffel creates is not a refund
  of at least `min_saving` (10.00). Paying to exchange stays a choice a person makes
  at CONFIRM. None is started after `CRON_EXECUTE_BY_SECONDS` in a run, because an
  exchange cut off by the function's time limit could land with nothing recorded.
- **The scheduler is `GET /cron/reshop`** (`vercel.json` `crons`, daily; a `CRON_SECRET`
  bearer token, and every request is refused while that is unset). It runs `_recheck`
  over `db.orders_due_a_check` — monitored, unexecuted orders across every real
  account, least-recently-checked first, at most `CRON_MAX_ORDERS` a run inside
  `CRON_BUDGET_SECONDS`. **Vercel fires crons on production deployments only**, so on
  staging (a Preview deployment) nothing calls it unless something external does.
- **"Recheck all fares"** (`POST /orders/recheck`; the button is on Reservations, each
  trip page and the ops console) runs the same loop for the signed-in account's orders,
  as the person who pressed it. With autopilot off it only decides, and the trip page
  links to the exchange's CONFIRM step once its last decision is a real (non-simulated)
  RESHOP. A booked ticket needs nothing from the ops console. There is no per-order
  cycle button.

## What's dormant, not deleted

`parsing.py`, `POST /webhooks/resend/inbound`, `email_import_sources`, and
`/auth/gmail/start` are bolt-on-era code from a pivot that was later reversed back to
the merchant-of-record model above. **They are still live, routed endpoints** — the
webhook verifies a real Svix signature and will parse and create a `manual`-sourced
order from a real forwarded email today, and Settings surfaces the forwarding address
to real users. What's actually not functional: `/auth/gmail/start` only records
"pending" intent (Google's sensitive-scope verification was never completed), and
Resend's inbound MX record was never enabled at the DNS level, so no real mail
currently reaches the webhook in production. `orders.source` still allows
`email_import` and `forward` in its CHECK constraint, but nothing ever writes those
two values — the webhook tags everything it creates as `manual`. Treat this whole path
as **not to be extended** until SSO/auth direction settles (it's tangled up with how
accounts get authenticated) — but don't describe it as unrouted; it isn't.
