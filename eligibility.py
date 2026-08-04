"""
Monitoring eligibility.

Decides whether an order is worth polling at all, from the fare conditions at
booking time. This is deliberately separate from the reshop decision: eligibility
answers "could this fare ever win?", the engine answers "has it won today?".

Three states, never a boolean. The bug this replaces defaulted every order to
monitored, so a SWISS Economy Light fare with a GBP 300 change penalty on a
573.33 USD ticket displayed as "Monitoring" while needing a ~66% market collapse
to break even.

Field names verified against ./responses/ dumps and
https://duffel.com/docs/api/orders/schema.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

# Tunable against pilot data. A change penalty above this share of the order
# total means the fare has to fall further than fares realistically do.
MAX_PENALTY_RATIO = Decimal("0.30")


class Eligibility(str, Enum):
    MONITORING = "monitoring"
    UNLIKELY_TO_SAVE = "unlikely_to_save"
    NOT_ELIGIBLE = "not_eligible"


class EligibilityReason(str, Enum):
    CHANGES_ALLOWED = "changes_allowed"
    NO_CHANGE_ACTION = "no_change_action"
    CHANGE_NOT_ALLOWED = "change_not_allowed"
    CONDITIONS_MISSING = "conditions_missing"
    PENALTY_UNKNOWN = "penalty_unknown"
    PENALTY_CURRENCY_MISMATCH = "penalty_currency_mismatch"
    PENALTY_TOO_HIGH = "penalty_too_high"


# Customer-facing copy. Keyed by reason so the UI never invents its own wording.
CUSTOMER_COPY = {
    EligibilityReason.CHANGES_ALLOWED:
        "We're watching this fare and will rebook you if the price drops.",
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
        return self.state is Eligibility.MONITORING

    @property
    def customer_copy(self):
        return CUSTOMER_COPY[self.reason]

    @property
    def label(self):
        return {
            Eligibility.MONITORING: "Monitoring",
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


def assess(order, max_penalty_ratio=MAX_PENALTY_RATIO):
    """
    `order` is a raw Duffel order payload.

    Order of checks matters — the first failing gate is the one reported.
    """
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
    #    total is not 300 — this is exactly how YBVI8R slipped through.
    if penalty_currency and total_currency and penalty_currency != total_currency:
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

    return Assessment(
        Eligibility.MONITORING, EligibilityReason.CHANGES_ALLOWED,
        f"change penalty {penalty} {penalty_currency} is {ratio:.1%} of the total, "
        f"within the {max_penalty_ratio:.0%} threshold",
        penalty=penalty, penalty_currency=penalty_currency, penalty_ratio=ratio)
