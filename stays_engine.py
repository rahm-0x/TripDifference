"""
Reshop decision for a booked stay.

A flight is exchanged in place and the airline quotes one number for it. A
stay has no exchange: the cheaper rate is booked and the old booking is
cancelled, so the saving is two numbers that have to be read together —

    what cancelling the booked rate refunds right now
  − what the replacement costs

and it only counts when the replacement is the same room on terms at least as
good. A lower price for a different room, a stricter cancellation policy, a
rate the hotel collects itself, or one that drops the guest's loyalty
programme is a different product, not a saving.

This module only decides. Nothing books or cancels on the strength of a
decision yet, and nothing here writes to the audit trail.
"""

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

import stays
from engine import Outcome, fee_split
from stays import BOARD_RANK, ZERO


class Reason(str, Enum):
    CHEAPER_SAME_ROOM = "cheaper_same_room"
    NOT_REFUNDABLE = "not_refundable"
    TOO_CLOSE_TO_CHECK_IN = "too_close_to_check_in"
    REFUND_DEADLINE_TOO_CLOSE = "refund_deadline_too_close"
    NO_RATES = "no_rates"
    NO_MATCHING_ROOM = "no_matching_room"
    NO_COMPARABLE_RATE = "no_comparable_rate"
    NOT_CHEAPER = "not_cheaper"
    BELOW_FLOOR = "below_floor"


@dataclass
class StayPolicy:
    """
    min_saving is the profitability floor, as it is for flights.

    refund_deadline_buffer_hours is the margin a rebooking needs: the new room
    is booked before the old one is cancelled, and a cancel that lands after
    the refund lapses pays for two rooms.
    """
    min_saving: Decimal = Decimal("20.00")
    check_in_buffer_hours: int = 24
    refund_deadline_buffer_hours: int = 6


@dataclass
class Decision:
    order_id: str
    source: str
    outcome: Outcome
    reason: Reason
    detail: str
    paid: Decimal
    currency: str
    floor: Decimal
    accommodation: str = ""
    room: str = ""
    refund_now: object = None
    new_total: object = None
    new_rate_id: str = ""
    # Extra the guest would pay at the property under the new rate. Already
    # taken off `saving`.
    extra_due_at_accommodation: object = None
    saving: object = None
    candidates: int = 0
    same_room: int = 0
    comparable: int = 0

    # Populated only on a rebook decision, and only when the caller supplied
    # the account's commission rate.
    recovered: object = None
    service_fee: object = None
    net_to_customer: object = None

    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def should_rebook(self):
        return self.outcome == Outcome.RESHOP

    def to_json(self):
        out = {}
        for k, v in asdict(self).items():
            if isinstance(v, Decimal):
                out[k] = str(v)
            elif isinstance(v, Enum):
                out[k] = v.value
            else:
                out[k] = v
        return out


class DuffelStayRates:
    """Re-prices the booked property for the same dates and occupancy."""
    name = "duffel"

    def rates_for(self, stay):
        return stays.rates_in(stays.search_accommodation(
            stay.accommodation_id, check_in=stay.check_in, check_out=stay.check_out,
            adults=stay.adults, child_ages=stay.child_ages, rooms=stay.rooms))


def _check_in_at(stay):
    """Midnight UTC on the check-in date. The property's timezone is not in the
    payload, so this can be out by half a day either way; the buffer it is
    compared against is a day."""
    try:
        return datetime.fromisoformat(stay.check_in[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _extra_due(booked, candidate):
    """How much more the guest pays at the property under `candidate`, never negative."""
    if booked.due_at_accommodation is None or candidate.due_at_accommodation is None:
        return ZERO
    return max(candidate.due_at_accommodation - booked.due_at_accommodation, ZERO)


def not_comparable(stay, candidate, now):
    """Why `candidate` cannot stand in for the booked rate, or None when it can."""
    booked = stay.rate
    if candidate.currency != booked.currency:
        return "priced in another currency"
    if not candidate.payable_from_balance:
        return "not payable in full from the Duffel balance"
    if BOARD_RANK.get(candidate.board_type, -1) < BOARD_RANK.get(booked.board_type, 0):
        return "worse board"
    if booked.loyalty_programme and candidate.loyalty_programme != booked.loyalty_programme:
        return "loses the loyalty programme"
    if candidate.loyalty_required and not stay.loyalty_number:
        return "needs a loyalty number"
    if (booked.due_at_accommodation is None) != (candidate.due_at_accommodation is None):
        return "fees at the property unknown on one side"
    if not candidate.at_least_as_refundable_as(booked, now):
        return "stricter cancellation"
    return None


def evaluate(stay, source, policy=None, now=None, commission_rate=None):
    """
    Decide whether to rebook `stay` using `source`.

    `source` is anything with a `name` and `rates_for(stay)` returning
    stays.Rate objects for the same property, dates and occupancy.
    `commission_rate` is the account's own; without it the decision carries no
    fee split rather than a guessed one.
    """
    policy = policy or StayPolicy()
    now = now or datetime.now(timezone.utc)
    booked = stay.rate

    def decide(outcome, reason, detail, **extra):
        return Decision(order_id=stay.id, source=source.name, outcome=outcome, reason=reason, detail=detail,
                        paid=booked.total, currency=booked.currency, floor=policy.min_saving,
                        accommodation=stay.accommodation_name, room=str(booked.room), **extra)

    # --- can the booked rate pay anything back at all? -------------------
    # Read from the terms stored at booking. A stay that refunds nothing is
    # never searched for.
    refund_now = booked.refund_at(now)
    if refund_now <= 0:
        return decide(Outcome.SKIP, Reason.NOT_REFUNDABLE,
                      "cancelling the booked rate now refunds nothing, so a cheaper rate would be paid for "
                      "on top of this one", refund_now=refund_now)

    common = dict(refund_now=refund_now)

    check_in_at = _check_in_at(stay)
    if check_in_at is not None:
        hours_out = (check_in_at - now).total_seconds() / 3600
        if hours_out < policy.check_in_buffer_hours:
            return decide(Outcome.SKIP, Reason.TOO_CLOSE_TO_CHECK_IN,
                          f"check-in is in {hours_out:.1f}h, buffer is {policy.check_in_buffer_hours}h", **common)

    deadline = booked.refund_deadline(now)
    if deadline - now < timedelta(hours=policy.refund_deadline_buffer_hours):
        return decide(Outcome.SKIP, Reason.REFUND_DEADLINE_TOO_CLOSE,
                      f"the {refund_now} {booked.currency} refund lapses at {deadline.isoformat()}, inside the "
                      f"{policy.refund_deadline_buffer_hours}h a rebooking is allowed to take", **common)

    # --- re-price the same property ---------------------------------------
    rates = [r for r in source.rates_for(stay) if r.accommodation_id == stay.accommodation_id]
    common["candidates"] = len(rates)
    if not rates:
        return decide(Outcome.SKIP, Reason.NO_RATES,
                      "the property returned no rates for these dates and guests", **common)

    # Same room, not just same hotel. A cheaper room is a downgrade.
    same_room = [r for r in rates if booked.room.matches(r.room)]
    common["same_room"] = len(same_room)
    if not same_room:
        return decide(Outcome.SKIP, Reason.NO_MATCHING_ROOM,
                      f"none of {len(rates)} rates is for the booked room ({booked.room})", **common)

    rejected = Counter()
    comparable = []
    for rate in same_room:
        why = not_comparable(stay, rate, now)
        if why:
            rejected[why] += 1
        else:
            comparable.append(rate)
    common["comparable"] = len(comparable)
    if not comparable:
        reasons = ", ".join(f"{n} {why}" for why, n in rejected.most_common())
        return decide(Outcome.SKIP, Reason.NO_COMPARABLE_RATE,
                      f"{len(same_room)} rates for the booked room, none on terms at least as good ({reasons})",
                      **common)

    # --- the numbers that decide ------------------------------------------
    best = min(comparable, key=lambda r: r.total + _extra_due(booked, r))
    extra_due = _extra_due(booked, best)
    saving = refund_now - best.total - extra_due
    common.update(new_total=best.total, new_rate_id=best.id, extra_due_at_accommodation=extra_due, saving=saving)

    at_property = f" and {extra_due} {booked.currency} more at the property" if extra_due else ""
    priced = (f"Cancelling now refunds {refund_now} {booked.currency}; the same room costs "
              f"{best.total} {best.currency}{at_property}")

    if saving <= 0:
        return decide(Outcome.SKIP, Reason.NOT_CHEAPER,
                      f"{priced} — not cheaper. Checked and declined; booking left as it is.", **common)

    if saving < policy.min_saving:
        return decide(Outcome.SKIP, Reason.BELOW_FLOOR,
                      f"{priced} — a {saving} {booked.currency} saving, below the {policy.min_saving} "
                      f"{booked.currency} minimum, so it was not worth doing. Checked and declined.", **common)

    split = {}
    fee_note = ""
    if commission_rate is not None:
        recovered, fee, net = fee_split(saving, Decimal(str(commission_rate)))
        split = dict(recovered=recovered, service_fee=fee, net_to_customer=net)
        fee_note = f" Recovered {recovered}, our fee {fee}, {net} net to customer."
    return decide(Outcome.RESHOP, Reason.CHEAPER_SAME_ROOM,
                  f"{priced} — a {saving} {booked.currency} saving, clears the {policy.min_saving} "
                  f"{booked.currency} minimum. Rebooking holds two rooms on the Duffel balance "
                  f"({best.total} {best.currency} more) until the old one is cancelled.{fee_note}",
                  **split, **common)
