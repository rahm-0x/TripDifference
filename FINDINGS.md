# Duffel API spike — FINDINGS

**Status: complete. All 7 commands ran successfully against the live test API.**

Every endpoint in the brief works. The harness is functional end to end: search →
book → show → reshop → change → confirm-change → cancel all returned 2xx and did
what they claim.

**But the thing the spike was built to prove cannot be proven in test mode.**
Read §1.

---

## 1. The headline: `change_total_amount` is hardcoded in test mode

**It never goes negative. It is always exactly `+125.00 USD`.**

Duffel's sandbox does not compute change prices from fares at all. It applies a
fixed formula:

```
new_total_amount    = original_order_total + 100.00
penalty_total_amount = 25.00
change_total_amount  = +125.00      (i.e. 100.00 fare delta + 25.00 penalty)
```

I confirmed this is invariant across every axis I could vary:

| Test | Order total | `new_total` | `change_total` |
|---|---|---|---|
| Economy LHR→JFK (ZZ) | 216.93 | 316.93 | **+125.00** |
| Business LHR→JFK (ZZ) → economy | 1421.56 | 1521.56 | **+125.00** |
| Economy LAS→LAX (ZZ) | 42.99 | 142.99 | **+125.00** |
| Economy LAS→LAX (**American Airlines**) | 43.48 | 143.48 | **+125.00** |
| LAS→LAX, departure shifted 4 months out | 42.99 | 142.99 | **+125.00** |

It held across three price points spanning 33×, two routes, two cabin classes, a
business→economy *downgrade*, a four-month date shift, and two different
carriers. In every single order change request, **all 9–12 returned offers had
identical money fields** — they differ only by flight number and departure time.

Two details make it unambiguous that this is a stub, not pricing:

1. **Changing to the flight you already booked still costs +125.00.** On the
   first order, one returned change offer was `ZZ2623 LHR→JFK 10:50` — the exact
   segment already ticketed. Same +125.00.
2. **On the 42.99 USD LAS→LAX ticket, the change costs +125.00 — nearly 3× the
   price of the ticket itself.** No real fare engine produces that.

Downgrading a **1421.56 USD business** ticket to a ~216 USD economy seat returned
`+125.00`, not a credit. If sandbox pricing had any relationship to fares, that
one case alone would have gone negative.

### What this means for the business model

**This is not evidence against your model.** The docs are clear that negative
values are real and supported, in two independent places:

> From the [order change offer schema](https://duffel.com/docs/api/order-change-offers/schema),
> verbatim on `change_total_amount`:
> *"The amount that will be charged or returned to the original payment method if
> refunded, determined according to the fare conditions. **This may be negative
> to reflect a refund.**"*

> From [create order change](https://duffel.com/docs/api/order-changes/create-order-change):
> *"If `change_total_amount` is zero or negative, there is no need to pass a
> `payment` object."*

That second quote is the stronger signal: the API has a **defined behavioural
branch** for negative change totals. A field that could never be negative would
not need that rule, and Duffel would not document it.

**The plain statement you asked for:** the mechanism your model depends on is
documented and structurally supported, but **this spike cannot confirm it
works**, because Duffel test mode returns a constant instead of a computed fare
difference. Sandbox can prove the *plumbing* — that you can price, select, and
execute an exchange, and that the harness reads the right fields. It cannot
prove the *economics*.

**Recommendation:** the negative-delta behaviour has to be validated in live
mode against a real carrier and a real fare drop, on a cheap refundable domestic
route, before you commit engineering to the model. Budget for that as a separate
de-risking step — it is currently the single largest unvalidated assumption in
the plan, and no amount of sandbox work will close it.

One useful signal for that live test: the list endpoint supports
`sort=change_total_amount`, so ascending sort puts the most negative offer first.
The harness already sorts client-side by the same field.

---

## 2. Which carriers support the change flow ⚠️ contradicts the brief

**The brief's assumption was wrong in this sandbox.** It expected real carriers
to fail on order changes and only deterministic test airlines to work. In
practice:

- **Duffel Airways (`ZZ`)** — supports the full change flow. ✅
- **American Airlines (`AA`)** — also supports it, returning **11 change offers
  with identical +125.00 pricing**. ✅

Both orders came back with `available_actions: ['cancel', 'change', 'update']`.
Duffel's sandbox appears to serve the *same synthetic change stub* regardless of
carrier, so there is no false-negative risk to avoid — and no carrier that gives
you more realistic change pricing than any other.

Carriers seen in sandbox search results: Duffel Airways (ZZ), American Airlines
(AA), British Airways (BA), Iberia (IB), TAP Air Portugal (TP), Frontier (F9).

> ⚠️ **Corrected in §8.** "Real carriers work fine" is too broad — I had only
> tested AA. A later survey found **Iberia does not expose a `change` action at
> all** and 422s on any change attempt. Support is per-carrier, and the field
> that tells you is not the obvious one. Read §8 before relying on this section.

**`LAS → LAX` works fine** — 29 offers, including Duffel Airways. My earlier
concern that it was an undocumented route with no sandbox behaviour was wrong;
Duffel synthesises offers for arbitrary routes. Use whatever route you like.

The documented magic routes still exist but are narrower than they sound:
`LHR→LTN` triggers **airline-initiated** changes (`/air/airline_initiated_changes`
— a *different* endpoint from the customer-initiated flow your model uses; don't
conflate them), and `LTN→SYD` makes cancellations refund to `airline_credits`.

---

## 3. Verified response shapes

Top-level keys, taken from the actual dumps in `./responses/`.

**Offer** (`GET /air/offers/{id}`)
```
id, owner, slices, passengers, total_amount, total_currency, base_amount,
base_currency, tax_amount, tax_currency, conditions, expires_at, created_at,
updated_at, live_mode, partial, private_fares, payment_requirements,
available_services, intended_services, intended_payment_methods,
total_emissions_kg, supported_loyalty_programmes,
supported_passenger_identity_document_types,
passenger_identity_documents_required, available_airline_credit_ids
```

**Order** (`POST /air/orders`)
```
id, booking_reference, booking_references, owner, slices, passengers,
total_amount, total_currency, base_amount, base_currency, tax_amount,
tax_currency, type, conditions, available_actions, payment_status, cancellation,
cancelled_at, changes, airline_initiated_changes, documents, services, users,
content, metadata, offer_id, created_at, synced_at, live_mode,
void_window_ends_at
```
PNR is `booking_reference` (e.g. `ZC7Q7A`). Real PNRs observed: `ZC7Q7A`,
`5RBUEC`, `TLBRGV`.

**Order change offer** (inside `POST /air/order_change_requests`)
```
id, change_total_amount, change_total_currency, new_total_amount,
new_total_currency, penalty_total_amount, penalty_total_currency, refund_to,
conditions, slices, order_change_id, available_payment_types, private_fares,
expires_at, created_at, updated_at, live_mode
```
`slices` is `{"add": [...], "remove": [...]}`. Each `add` entry has
`id, origin, destination, origin_type, destination_type, duration,
fare_brand_name, segments`.

**Order change, confirmed** (`POST /air/order_changes/{id}/actions/confirm`)
```
id, order_id, confirmed_at, change_total_amount, change_total_currency,
new_total_amount, new_total_currency, penalty_total_amount,
penalty_total_currency, refund_to, slices, selected_services,
available_payment_types, expires_at, created_at, updated_at, live_mode
```

Observed confirmed exchange: `confirmed_at: 2026-08-04T10:35:45Z`,
`change_total: +125.00 USD`, `new_total: 1521.56 USD`, `refund_to: null`.

**Cancellation quote** (`POST /air/order_cancellations`)
```
id, order_id, refund_amount, refund_currency, refund_to, expires_at,
confirmed_at, created_at, live_mode
```
Observed: full refund (216.93 of 216.93), `refund_to: "balance"`, quote expires
**1 hour** after issue. Confirming set `confirmed_at` and refunded 43.48.

---

## 4. Errors hit

**`422 offer_request_already_booked`** — the only real error encountered.
```
[validation_error/offer_request_already_booked] Can't book multiple offers from the same offer request
  Field 'selected_offers' has offers included in a offer request that has already been booked; please perform a new search
  source: {'field': 'selected_offers', 'pointer': '/selected_offers'}
```
**Cause:** an offer request is single-use. Once you book *any* offer from it, the
whole `orq_` is burned and every other offer in it is dead. You must issue a
fresh `POST /air/offer_requests` per booking. This matters for reshop-style
workflows that search once and book repeatedly — that pattern will not work.

The error body was specific and immediately diagnostic, as the brief predicted.
Duffel's errors carry `type`, `code`, `title`, `message`, `source.pointer`, and
`documentation_url`; worth surfacing all of them.

**No 429 was hit**, across ~25 requests in a few minutes. Rate limiting is not a
practical constraint at spike volumes.

---

## 5. Auth and transport ✅

Confirmed working exactly as the brief specified:

| Header | Value |
|---|---|
| `Authorization` | `Bearer duffel_test_...` |
| `Duffel-Version` | `v2` |
| `Accept` | `application/json` |
| `Content-Type` | `application/json` |

Base URL `https://api.duffel.com`, all endpoints under `/air/`. No contradiction.

---

## 6. Contradictions with the brief

1. **`change_total_amount` never goes negative in test mode.** It is a hardcoded
   `+125.00`. The brief's assumption is documented as correct but is
   **unverifiable in sandbox**. This is the most important finding. §1
2. **Real carriers *do* support order changes in sandbox.** American Airlines
   returned change offers identically to Duffel Airways. There is no
   false-negative risk from picking a real carrier. §2
3. **`LAS → LAX` is fine.** Works, returns 29 offers including ZZ. §2
4. **429 uses `ratelimit-reset`, not `Retry-After`.** Duffel's
   [error docs](https://duffel.com/docs/api/overview/errors) document
   `ratelimit-limit`, `ratelimit-remaining`, and `ratelimit-reset` — the last
   being an **RFC 2616 date string** (`"Tue, 24 Nov 2020 08:22:00 GMT"`), not a
   seconds delta. Parsing it as an integer yields nonsense. The harness checks
   `Retry-After` first (harmless if absent) then falls back to date-parsing
   `ratelimit-reset`. Never exercised in practice.
5. **Offer requests are single-use** — not mentioned in the brief, and it
   constrains any search-once-book-many design. §4

---

## 7. Incidental observations

- **Sandbox search prices do drift slightly between calls.** Re-searching the
  same route seconds apart moved 216.93 → 216.45 (−0.48) and 42.99 → 42.65
  (−0.34). So the *search* side has some jitter — but the change-pricing side
  ignores it entirely, which is further evidence the +125 is a stub.
- The naive "re-search the market and diff against what you paid" delta **does**
  go negative (−0.48, −0.34 above). If you ever see a negative number in reshop
  output, check which of the two it is — market delta is not `change_total_amount`
  and proves nothing about exchange economics.
- All 231 LHR→JFK offers shared the same departure time (10:50) across different
  carriers, another sign of synthetic generation.
- `refund_to` on the confirmed change was `null`; on cancellations it was
  `balance`.
- Order change offers expire **3 days** out; cancellation quotes expire in **1
  hour**.

---

## 8. `available_actions` is the only reliable changeability signal ⚠️ new

**The single most useful thing learned building the simulator.**

`conditions.change_before_departure.allowed` is **wrong in both directions** and
must never be used to decide whether an order can be changed.

Surveyed by booking a real sandbox order per carrier on LHR→JFK:

| Carrier | `change` in `available_actions` | `conditions...allowed` | Changes actually work? |
|---|---|---|---|
| Duffel Airways (ZZ) | **yes** | `False` | **yes** — verified, exchange executed |
| British Airways (BA) | **yes** | `False` | yes |
| American Airlines (AA) | yes | `True` | yes |
| TAP Air Portugal (TP) | yes | `None` | yes |
| **Iberia (IB)** | **no** | **`True`** | **no — 422** |

Two independent failure modes:

1. **Iberia says `allowed: True`, with a `penalty_amount` of `40.00 USD`, and
   there is no `change` action.** Attempting the change returns:
   ```
   422 [invalid_state_error/order_not_changeable]
   Order not changeable: This order cannot be changed through the API.
   ```
   Trusting `conditions` here means an API round-trip and a 422 on every cycle
   for an order that can never be reshopped.

2. **Duffel Airways and BA say `allowed: False` but changes work fine.** This is
   the more dangerous direction: treating `conditions` as a veto silently
   suppresses every real reshop opportunity on those carriers, and it fails
   *quietly* — no error, just an order that never reshops.

I hit both while building. My first fix required the two fields to agree, which
would have disabled reshop on Duffel Airways and BA. `available_actions` alone is
correct; `conditions` is a fallback only when `available_actions` is absent.
Locked in by `test_available_actions_beats_lying_conditions_block` and
`test_conditions_false_does_not_veto_when_actions_allow`.

Also worth noting: `conditions.change_before_departure.allowed` varied **between
two Duffel Airways orders** on the same route (`False` in the survey, `True` on a
later booking). It appears to track fare brand, not carrier, which is a further
reason not to build logic on it.

**Implication for the live build:** filter monitorable orders on
`'change' in available_actions` at booking time. Roughly 1 in 5 sandbox carriers
is not reshoppable at all, and you want to know that before you start paying to
poll it.

---

## 9. Reshop simulator — what is now verified

Duffel sandbox still cannot produce a negative `change_total_amount` (§1), so the
price source is an interface with two implementations and the engine cannot tell
them apart:

```
PriceSource                     prices.py
├── DuffelPriceSource           live sandbox calls — what ships
└── SimulatedPriceSource        reads simulated_prices.json, forces any delta
```

Everything downstream of that interface is tested for real: **28 unit tests**,
all against the simulated source.

**The trap is verified live, not just in tests.** A real cycle against sandbox
produced a genuinely cheaper market price and a positive change total:

```
market=216.12  delta=-1.06  change_total=+125.00  → SKIP change_total_not_negative
"the market looks 1.06 USD cheaper, but that is not the exchange price
 and must not be acted on"
```

That is the failure mode the business model has to survive, and it occurs
naturally in sandbox — market re-search drifts a dollar or two between calls
(§7) while the exchange price stays pinned at +125.00. Anything keying off market
delta would have fired on that.

**Tests were mutation-checked.** Injecting `if market_delta < 0: reshop` into the
engine fails exactly `test_cheaper_market_but_positive_change_total` and
`test_market_delta_never_flips_a_skip_to_a_reshop`, and nothing else. The guard
has teeth.

Decision rules, in order (the first three gate before any API call):

| # | Gate | Outcome |
|---|---|---|
| 1 | `change` not in `available_actions` | `fare_not_changeable` |
| 2 | inside void window | `in_void_window` — void+rebook is free, strictly better |
| 3 | departure < buffer (default 24h) | `too_close_to_departure` |
| 4 | no change offers | `no_change_offers` |
| 5 | no offer matching booked carrier **and** flight numbers | `no_identical_itinerary` |
| 6 | `change_total >= 0` | `change_total_not_negative` |
| 7 | `-change_total < floor` | `below_floor` |
| 8 | otherwise | **`reshop`** |

Every cycle appends one JSON line to `decisions.log` with `source`, `paid`,
`market_best`, `market_delta`, `change_total`, `floor`, `outcome`, `reason` — so
simulated and live runs over identical logic can be diffed directly when live
access lands.

### Still unproven

The negative path has **only ever run against simulated data**. Sandbox proves
the plumbing, the gates, and the arithmetic. It cannot prove that a real airline
returns a negative `change_total_amount` when a fare drops. That remains the
single largest unvalidated assumption, exactly as it was in §1 — the simulator
narrows the untested surface to one API behaviour, but does not eliminate it.

---

## 10. Monitoring eligibility — gating on fare conditions

Monitoring is no longer a boolean defaulted to on. It is assessed from fare
conditions at booking time (`eligibility.py`) into three states:

| State | Poll? | Trigger |
|---|---|---|
| **Monitoring** | yes | changes allowed, penalty ≤ `MAX_PENALTY_RATIO` (30%) of the total |
| **Unlikely to save** | no | changes allowed but penalty above the threshold |
| **Not eligible** | no | changes not allowed, or conditions unknown/incomparable |

`MAX_PENALTY_RATIO` is a named constant in `eligibility.py`, tunable against
pilot data. The boundary is inclusive — exactly 30% still monitors.

### Field shapes, verified

Confirmed against `./responses/` dumps and
[the order schema](https://duffel.com/docs/api/orders/schema):

```
order.conditions.change_before_departure.{allowed, penalty_amount, penalty_currency}
order.conditions.refund_before_departure.{allowed, penalty_amount, penalty_currency}
```

`penalty_amount` is a **string** (`"300"`, `"40.00"` — inconsistent decimal
places, so parse as Decimal). Order-level `conditions` assume the condition
applies to every slice; the docs direct you to `slices[].conditions` for
per-slice granularity, which this build does not yet use.

> ⚠️ **The docs are wrong about nullability.** The schema page shows no nullable
> marker on `change_before_departure`, but a real TAP Air Portugal order
> (`AALDZE`) returned:
> ```json
> {"change_before_departure": null, "refund_before_departure": null}
> ```
> Unknown conditions are therefore a real runtime case, not a defensive
> hypothetical. They resolve to **Not eligible** and are logged with
> `needs_attention: true` rather than defaulting to monitored.

### Currency mismatch is real and it was the reported bug

`YBVI8R` (SWISS, LHR→JFK via ZRH, `LX339 + LX16`) carried:

```json
{"allowed": true, "penalty_amount": "300", "penalty_currency": "GBP"}
```

against a **573.33 USD** total. Comparing the raw numbers gives 52%; converting
gives roughly 66%. Either way it can never win, but the raw comparison is
meaningless and must not be made. The assessor compares **only when currencies
match** and otherwise returns Not eligible with `penalty_currency_mismatch`,
logged for review. No FX conversion is performed — adding one would move this
fare into a properly-evaluated state, where it would still fail the threshold.

### ⚠️ This partially contradicts §8, deliberately

§8 established that `available_actions` is authoritative and that
`conditions.change_before_departure.allowed` lies **in both directions** —
notably that Duffel Airways and BA report `allowed: false` while changes work.

Eligibility now treats `allowed: false` as **Not eligible**, which is the
opposite of §8's advice. Both gates are applied: `available_actions` first,
then `conditions`. Measured across the 8 real sandbox orders:

| Reason | Orders |
|---|---|
| `changes_allowed` → Monitoring | 3 |
| `no_change_action` | 2 |
| `change_not_allowed` | 2 |
| `penalty_currency_mismatch` | 1 |

**Two orders are gated out by `conditions` alone despite exposing a working
`change` action** — `C26PN2` (Duffel Airways) and `IRI7G2` (British Airways).
In sandbox these would in fact have accepted an exchange.

This is the conservative direction and the correct default for the reported bug
(over-monitoring), but it is a live trade-off: if production carriers report
`allowed: false` as loosely as sandbox does, this suppresses real opportunities
silently. Worth re-measuring against live data before the threshold is tuned.

---

## 11. Decision vs execution are separate axes

A forced negative change total previously looked like the engine had done
nothing. It had decided to exchange; execution was blocked. Those are now
distinct fields and distinct log lines.

```
Outcome    reshop | skip                     — what the engine decided
Execution  not_applicable | awaiting_confirmation
           | blocked_simulated | executed | failed   — what happened next
```

`decisions.log` lines carry a `kind` discriminator (`decision`, `execution`,
`eligibility`); lines written before this change have no `kind` and are read as
`decision`. A reshop writes two lines — the decision, then the execution outcome
with its own reason.

Reshop decisions also carry the fee split, so it is verifiable without
recomputation:

```
recovered 45.00 · service_fee 11.25 (25%) · net_to_customer 33.75
```

`SERVICE_FEE_RATE` is a named constant. The split always reconciles exactly —
the customer's net absorbs rounding, so fee + net == recovered at cent
precision, asserted across odd amounts in `test_fee_split_always_reconciles`.

**Open question:** `min_saving` currently applies to the **gross** recovery, not
the customer's net. A 20.00 floor passes a 20.00 recovery that nets the customer
15.00. Left as-is because changing it is a product decision, not a bug fix.

### Segment identity

`Itinerary.key` is now an ordered tuple of `(carrier, flight_number)` pairs
rather than a carrier plus a list of numbers. Segment count, order, per-segment
carrier and number all participate in matching, so:

- `LX339 + LX16` does not match a direct `LX339` on the same city pair
- `LX339 + LX16` does not match `LX16 + LX339`
- `LX339 + LX16` does not match `LX339 + UA16` (codeshare reusing a number)

The last case is the one the old key could not catch. Mutation-checked: removing
per-segment carriers from the key fails exactly
`test_codeshare_same_numbers_different_carrier_does_not_match` and nothing else.
