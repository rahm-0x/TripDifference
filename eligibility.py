"""
Monitoring eligibility.

Decides whether an order is worth polling at all, from the fare conditions at
booking time. This is deliberately separate from the reshop decision: eligibility
answers "could this fare ever win?", the engine answers "has it won today?".

Four states, never a boolean. The bug this replaces defaulted every order to
monitored, so a SWISS Economy Light fare with a GBP 300 change penalty on a
573.33 USD ticket displayed as "Monitoring" while needing a ~66% market collapse
to break even.

MONITORING vs. LIKELY_MONITORING is a second, later bug of the same shape:
`available_actions` is the only reliable signal for whether an order can
actually be changed (FINDINGS.md §8 — `conditions` lies in both directions),
but an offer has no `available_actions` at all — Duffel doesn't expose it
before a ticket is issued. Search-time code passing an offer through this
function was silently getting the same "Monitoring" state a confirmed order
gets, on `conditions` alone. LIKELY_MONITORING is the honest name for that:
a real but unconfirmed claim, never conflated with a verified one.

Field names verified against ./responses/ dumps and
https://duffel.com/docs/api/orders/schema.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

# Tunable against pilot data. A change penalty above this share of the order
# total means the fare has to fall further than fares realistically do.
MAX_PENALTY_RATIO = Decimal("0.30")

# Real denials (zero confirms) needed before a carrier is excluded outright
# rather than just rank-penalized. 2, not 1: a single denial could be one
# restrictive fare brand, not the carrier as a whole (FINDINGS.md notes
# conditions varies by fare brand within a carrier) — one data point earns
# suspicion, not a verdict. A carrier that racks up even one confirm at any
# point moves to the untouched "mixed" case regardless of this threshold.
CARRIER_EXCLUSION_DENIAL_THRESHOLD = 2


class Eligibility(str, Enum):
    MONITORING = "monitoring"
    LIKELY_MONITORING = "likely_monitoring"
    UNLIKELY_TO_SAVE = "unlikely_to_save"
    NOT_ELIGIBLE = "not_eligible"


class EligibilityReason(str, Enum):
    POINTS_FARE = "points_fare"
    NO_PAYMENT_METHOD = "no_payment_method"
    CUSTOMER_SOURCED = "customer_sourced"
    CHANGES_ALLOWED = "changes_allowed"
    CHANGES_LIKELY_ALLOWED = "changes_likely_allowed"
    NO_CHANGE_ACTION = "no_change_action"
    CHANGE_NOT_ALLOWED = "change_not_allowed"
    CONDITIONS_MISSING = "conditions_missing"
    PENALTY_UNKNOWN = "penalty_unknown"
    PENALTY_CURRENCY_MISMATCH = "penalty_currency_mismatch"
    PENALTY_TOO_HIGH = "penalty_too_high"
    CARRIER_NEVER_CONFIRMED_CHANGE = "carrier_never_confirmed_change"
    CARRIER_SINGLE_DENIAL = "carrier_single_denial"


# Customer-facing copy. Keyed by reason so the UI never invents its own wording.
CUSTOMER_COPY = {
    EligibilityReason.POINTS_FARE:
        "This was booked with points, not cash, so there's no fare difference "
        "for us to recover.",
    EligibilityReason.NO_PAYMENT_METHOD:
        "Link a card and we'll start watching this fare for a price drop.",
    EligibilityReason.CUSTOMER_SOURCED:
        "We're watching this fare and will let you know if a cheaper option "
        "appears.",
    EligibilityReason.CHANGES_ALLOWED:
        "We're watching this fare and will rebook you if the price drops.",
    EligibilityReason.CHANGES_LIKELY_ALLOWED:
        "This fare's rules suggest it can be changed after ticketing, but "
        "we can only confirm that once it's booked — we'll know for certain "
        "as soon as you buy it.",
    EligibilityReason.CARRIER_NEVER_CONFIRMED_CHANGE:
        "This fare's rules say changes are allowed, but this airline has "
        "never actually let us change a ticket like this once issued, so "
        "we're not counting on being able to rebook it.",
    EligibilityReason.CARRIER_SINGLE_DENIAL:
        "This fare's rules suggest it can be changed, but the one real "
        "ticket we've seen from this airline wasn't — we're less confident "
        "about this one than usual.",
    EligibilityReason.NO_CHANGE_ACTION:
        "This fare can't be changed after ticketing, so it can't be rebooked.",
    EligibilityReason.CHANGE_NOT_ALLOWED:
        "This fare can't be changed after ticketing, so it can't be rebooked.",
    EligibilityReason.CONDITIONS_MISSING:
        "The airline didn't publish change rules for this fare, so we can't "
        "confirm a rebooking would be possible.",
    EligibilityReason.PENALTY_UNKNOWN:
        "The airline didn't publish a change fee for this fare, so we can't "
        "confirm a rebooking would be worthwhile.",
    EligibilityReason.PENALTY_CURRENCY_MISMATCH:
        "The airline quotes this fare's change fee in a different currency to "
        "the ticket, so we can't confirm a rebooking would be worthwhile.",
    EligibilityReason.PENALTY_TOO_HIGH:
        "This fare's change fee is too high for a price drop to be worth "
        "rebooking, so we're not monitoring it.",
}


@dataclass(frozen=True)
class Assessment:
    state: Eligibility
    reason: EligibilityReason
    detail: str                      # operator-facing, specific
    penalty: object = None           # Decimal or None
    penalty_currency: str = ""
    penalty_ratio: object = None     # Decimal or None, only when comparable
    needs_attention: bool = False    # unknown/mismatch — worth logging and reviewing

    @property
    def should_poll(self):
        # LIKELY_MONITORING is the pre-purchase-only state — an offer, or
        # (rare) an order Duffel returned without available_actions at all.
        # Treated the same as a confirmed MONITORING for whether to poll:
        # the alternative is not polling anything until it's confirmed,
        # which is never true before a ticket exists.
        return self.state in (Eligibility.MONITORING, Eligibility.LIKELY_MONITORING)

    @property
    def customer_copy(self):
        return CUSTOMER_COPY[self.reason]

    @property
    def label(self):
        return {
            Eligibility.MONITORING: "Monitoring",
            Eligibility.LIKELY_MONITORING: "Likely monitorable",
            Eligibility.UNLIKELY_TO_SAVE: "Unlikely to save",
            Eligibility.NOT_ELIGIBLE: "Not eligible",
        }[self.state]


def _decimal(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def assess(order, max_penalty_ratio=MAX_PENALTY_RATIO, fare_type="cash", has_card=True,
           carrier_capability=None):
    """
    `order` is a raw Duffel order payload, a raw Duffel *offer* payload
    (pre-purchase — carries no `available_actions`), or `{}` for a
    reservation with no Duffel order behind it (customer-sourced: email
    import / manual entry). `fare_type` is 'cash' or 'points' — every fare
    Duffel's cash-offer search can book is 'cash' by construction, so the
    default holds for every order this app has ever booked itself; only a
    customer-sourced reservation can actually be 'points'. `has_card`
    defaults True so nothing regresses ahead of real Stripe wiring —
    card-gating (business rule: no card on file, no active monitoring) is
    enforced here once a real value is passed in.

    `carrier_capability` is an optional `{"confirmed": int, "denied": int}`
    of real `available_actions` observations gathered from this carrier's
    past orders (db.carrier_capability_for) — see LIKELY_MONITORING below.

    Order of checks matters — the first failing gate is the one reported.
    """
    if fare_type == "points":
        return Assessment(
            Eligibility.NOT_ELIGIBLE, EligibilityReason.POINTS_FARE,
            "fare_type is 'points' — no cash fare difference to recover")

    if not has_card:
        return Assessment(
            Eligibility.NOT_ELIGIBLE, EligibilityReason.NO_PAYMENT_METHOD,
            "no payment method on file — card-gating blocks active monitoring")

    if not order:
        # No Duffel order to read changeability/penalty conditions from —
        # TD never wrote this reservation. The penalty-ratio check below is
        # about whether changing an *existing Duffel order* in place is
        # economical, which doesn't apply here; eligibility is fare_type +
        # card only, same as the target spec's model for a customer-sourced
        # reservation.
        return Assessment(
            Eligibility.MONITORING, EligibilityReason.CUSTOMER_SOURCED,
            "no Duffel order behind this reservation — eligible on fare_type "
            "and card-on-file alone")

    total = _decimal(order.get("total_amount"))
    total_currency = (order.get("total_currency") or "").upper()

    # 1. available_actions is authoritative for whether the API will accept a
    #    change at all (FINDINGS.md §8 — conditions lies in both directions).
    actions = order.get("available_actions")
    if actions is not None and "change" not in actions:
        return Assessment(
            Eligibility.NOT_ELIGIBLE, EligibilityReason.NO_CHANGE_ACTION,
            "order does not expose a 'change' action")

    conditions = order.get("conditions")
    change = (conditions or {}).get("change_before_departure")

    # 2. Unknown beats assumed. Real TAP orders return change_before_departure:
    #    null even though the docs imply it is always present.
    if not conditions or change is None:
        return Assessment(
            Eligibility.NOT_ELIGIBLE, EligibilityReason.CONDITIONS_MISSING,
            "conditions.change_before_departure is missing or null",
            needs_attention=True)

    if change.get("allowed") is not True:
        return Assessment(
            Eligibility.NOT_ELIGIBLE, EligibilityReason.CHANGE_NOT_ALLOWED,
            f"conditions.change_before_departure.allowed is {change.get('allowed')!r}")

    penalty = _decimal(change.get("penalty_amount"))
    penalty_currency = (change.get("penalty_currency") or "").upper()

    # 3. Changes allowed but no published fee. Could mean free changes, could
    #    mean unpublished — we do not guess in the customer's favour.
    if penalty is None:
        return Assessment(
            Eligibility.NOT_ELIGIBLE, EligibilityReason.PENALTY_UNKNOWN,
            "change allowed but penalty_amount is null",
            penalty_currency=penalty_currency, needs_attention=True)

    # 4. Never compare raw numbers across currencies. GBP 300 against a USD
    #    total is not 300 — this is exactly how YBVI8R slipped through. A zero
    #    penalty is zero in any currency, so it needs no conversion: compared as
    #    a Decimal, never by truthiness (Duffel sends amounts as strings, and
    #    "0.00" is truthy).
    if (penalty is not None and Decimal(str(penalty)) != 0
            and penalty_currency and total_currency
            and penalty_currency != total_currency):
        return Assessment(
            Eligibility.NOT_ELIGIBLE, EligibilityReason.PENALTY_CURRENCY_MISMATCH,
            f"penalty is {penalty} {penalty_currency} but order total is in "
            f"{total_currency} — not comparable without conversion",
            penalty=penalty, penalty_currency=penalty_currency, needs_attention=True)

    if not total or total <= 0:
        return Assessment(
            Eligibility.NOT_ELIGIBLE, EligibilityReason.CONDITIONS_MISSING,
            "order total missing or non-positive; cannot evaluate penalty ratio",
            penalty=penalty, penalty_currency=penalty_currency, needs_attention=True)

    ratio = penalty / total
    if ratio > max_penalty_ratio:
        return Assessment(
            Eligibility.UNLIKELY_TO_SAVE, EligibilityReason.PENALTY_TOO_HIGH,
            f"change penalty {penalty} {penalty_currency} is "
            f"{ratio:.1%} of the {total} {total_currency} total, above the "
            f"{max_penalty_ratio:.0%} threshold",
            penalty=penalty, penalty_currency=penalty_currency, penalty_ratio=ratio)

    # available_actions already confirmed 'change' is present (gate 1 would
    # have returned above otherwise) — this is a real order, verified.
    if actions is not None:
        return Assessment(
            Eligibility.MONITORING, EligibilityReason.CHANGES_ALLOWED,
            f"change penalty {penalty} {penalty_currency} is {ratio:.1%} of the total, "
            f"within the {max_penalty_ratio:.0%} threshold",
            penalty=penalty, penalty_currency=penalty_currency, penalty_ratio=ratio)

    # No available_actions to go on (an offer, pre-purchase — or, rarely, an
    # order Duffel returned without the field). conditions alone is not
    # reliable (FINDINGS.md §8), so a carrier's own real-order track record
    # is consulted instead — graduated by how much of one there is, not a
    # single denial turned into a permanent verdict. A carrier that's never
    # once confirmed 'change' can't earn its way out of a hard exclusion by
    # being booked less because of that exclusion — the one-denial case gets
    # a rank penalty, not a black mark, and FINDINGS.md already notes this
    # can vary by fare brand within one carrier, not just between carriers.
    confirmed = carrier_capability.get("confirmed", 0) if carrier_capability else 0
    denied = carrier_capability.get("denied", 0) if carrier_capability else 0

    if confirmed == 0 and denied >= CARRIER_EXCLUSION_DENIAL_THRESHOLD:
        return Assessment(
            Eligibility.NOT_ELIGIBLE, EligibilityReason.CARRIER_NEVER_CONFIRMED_CHANGE,
            f"conditions claim changes are allowed, but every real order seen from this "
            f"carrier ({denied} observed, 0 confirmed) came back without a 'change' "
            f"action — not trusting the claim",
            penalty=penalty, penalty_currency=penalty_currency, penalty_ratio=ratio,
            needs_attention=True)

    if confirmed == 0 and denied == 1:
        return Assessment(
            Eligibility.LIKELY_MONITORING, EligibilityReason.CARRIER_SINGLE_DENIAL,
            f"conditions claim changes are allowed, but the one real order seen from "
            f"this carrier came back without a 'change' action — one data point, not "
            f"enough to exclude outright, but not trusted at face value either",
            penalty=penalty, penalty_currency=penalty_currency, penalty_ratio=ratio,
            needs_attention=True)

    return Assessment(
        Eligibility.LIKELY_MONITORING, EligibilityReason.CHANGES_LIKELY_ALLOWED,
        f"change penalty {penalty} {penalty_currency} is {ratio:.1%} of the total, "
        f"within the {max_penalty_ratio:.0%} threshold, but available_actions isn't "
        f"known yet — conditions alone is not reliable (FINDINGS.md §8)",
        penalty=penalty, penalty_currency=penalty_currency, penalty_ratio=ratio)
