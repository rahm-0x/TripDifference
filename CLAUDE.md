# TripDifference

TripDifference is **merchant of record**. A corporate travel manager books flights for
employees through the portal; we issue the ticket via Duffel, funded from TD's own
Duffel balance, charged to the company's card via Stripe (`app.py:book()`); we then
monitor the fare and exchange to cheaper identical flights, billing a commission on
what's actually recovered (`accounts.commission_rate`, default 25%) plus a monthly
subscription (`accounts.subscription_fee`).

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
  production. Two flags, both staging-only (either one true elsewhere fails startup):
  `DUFFEL_LIVE_SEARCH_ENABLED` lets a `duffel_live_` token make offer requests and fetch
  offers; `DUFFEL_LIVE_ORDERS_ENABLED` (default false) lets it create, change or cancel
  orders — while false, `duffel_http.request()` refuses every non-search live request
  with a clear error. `app.py` runs `duffel_http.startup_check()`,
  `billing.startup_check()` (staging needs an `sk_test_` Stripe key) and
  `db.startup_check()` (staging must be on Supabase project `bcqzwnoifwkuimrrdysy`,
  production must not) at import. Every offer request goes through `live_search`: a live
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
- **Cash and credit never net against each other.** `invoice_lines.amount CHECK
  (amount >= 0)` makes that structurally impossible; don't work around it.
- Cash refunds return to the company's card directly via Duffel and **never appear as
  an invoice line** — only the commission on them does.
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

### Behaviour

- **Nothing executes autonomously.** Every exchange and cancel requires a human to
  type CONFIRM (`execute()` checks `confirm_text == "CONFIRM"`). There is no
  scheduler — `vercel.json` has no `crons` entry; cycles only run when someone clicks.

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
