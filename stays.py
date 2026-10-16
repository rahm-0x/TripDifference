"""
Duffel Stays: the HTTP calls, and the value objects the reshop decision reads.

Sandbox only. duffel_http refuses every /stays call made with a live token,
and this Duffel account does not have Stays enabled yet — every call answers
403 with a plain-text body, raised here as StaysNotEnabled. Nothing in this
module has run against a real response: field names follow Duffel's published
client types (duffel-api-javascript, src/Stays/StaysTypes.ts), and the first
sandbox payloads should be checked against them before anything is built on top.

A booking cannot be changed in place. A cheaper rate is taken by booking it
and cancelling the old booking, so what makes two rates "the same room on terms
at least as good" has to be modelled here rather than left in raw payloads —
see Room and Rate, and stays_engine for the decision.

All money is Decimal. Duffel returns amounts as strings.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import duffel_http

# Each board includes the ones before it, so a higher rank is never worse.
BOARD_RANK = {"room_only": 0, "breakfast": 1, "half_board": 2, "full_board": 3, "all_inclusive": 4}

ZERO = Decimal("0")


class StaysNotEnabled(RuntimeError):
    """Duffel has not turned Stays on for this account. Access is requested
    from Duffel; nothing in the app can change it."""


# ---------------------------------------------------------------------------
# value objects
# ---------------------------------------------------------------------------

def _money(value):
    """A Decimal, or None when Duffel sent null or something unreadable."""
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def _parse_dt(value):
    """Assumes UTC when the stamp is naive, as engine._parse_dt does for flights."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Room:
    """
    Identity of a room type within one accommodation.

    Duffel gives a room a name and a bed list, and no id that survives from one
    search to the next, so the name is the identity: compared case-insensitively
    with whitespace collapsed, and nothing looser. Beds must agree when both
    sides list them; a side that lists none is not treated as a different room,
    because `beds` is optional in the payload.
    """
    name: str
    beds: tuple = ()

    @classmethod
    def from_duffel(cls, room):
        room = room or {}
        beds = sorted((str(b.get("type") or ""), int(b.get("count") or 0)) for b in room.get("beds") or [])
        return cls(name=str(room.get("name") or ""), beds=tuple(beds))

    @property
    def key(self):
        return " ".join(self.name.casefold().split())

    def matches(self, other):
        if other is None or not self.key or self.key != other.key:
            return False
        return not self.beds or not other.beds or self.beds == other.beds

    def __str__(self):
        return self.name or "—"


def _timeline(rate, total_currency):
    """((before, refund_amount), ...) ascending, or () for a rate that refunds
    nothing. One entry that can't be read empties the whole timeline: a rate
    whose refund terms are partly unknown is treated as non-refundable."""
    steps = []
    for entry in rate.get("cancellation_timeline") or []:
        before, amount = _parse_dt(entry.get("before")), _money(entry.get("refund_amount"))
        if before is None or amount is None or (entry.get("currency") or total_currency) != total_currency:
            return ()
        steps.append((before, amount))
    return tuple(sorted(steps))


@dataclass(frozen=True)
class Rate:
    """One bookable price for one room, with the terms that decide whether it
    can stand in for another rate."""
    id: str
    accommodation_id: str
    room: Room
    total: Decimal
    currency: str
    board_type: str = "room_only"
    payment_type: str = ""
    payment_methods: tuple = ()
    cancellation_timeline: tuple = ()
    loyalty_programme: str = ""
    loyalty_required: bool = False
    # Fees the guest pays at the property, outside `total`. None means Duffel
    # did not know, which is not the same as zero.
    due_at_accommodation: object = None
    source: str = ""
    code: str = ""

    @classmethod
    def from_duffel(cls, rate, room=None, accommodation_id=""):
        currency = rate.get("total_currency") or ""
        due = _money(rate.get("due_at_accommodation_amount"))
        if due is not None and (rate.get("due_at_accommodation_currency") or currency) != currency:
            due = None
        return cls(
            id=rate.get("id") or "",
            accommodation_id=accommodation_id,
            room=room if isinstance(room, Room) else Room.from_duffel(room),
            total=_money(rate.get("total_amount")) or ZERO,
            currency=currency,
            board_type=rate.get("board_type") or "room_only",
            payment_type=rate.get("payment_type") or "",
            payment_methods=tuple(rate.get("available_payment_methods") or ()),
            cancellation_timeline=_timeline(rate, currency),
            loyalty_programme=rate.get("supported_loyalty_programme") or "",
            loyalty_required=bool(rate.get("loyalty_programme_required")),
            due_at_accommodation=due,
            source=rate.get("source") or "",
            code=rate.get("code") or "",
        )

    @property
    def payable_from_balance(self):
        """Paid in full, now, from the Duffel balance — the only kind of rate
        TripDifference can be merchant of record for. A guarantee or deposit
        rate is settled at the property, and a card-only rate makes the hotel
        the merchant."""
        return self.payment_type == "pay_now" and "balance" in self.payment_methods

    def refund_at(self, when):
        """What cancelling at `when` returns. Each step applies until its
        `before`; after the last one, nothing does."""
        for before, amount in self.cancellation_timeline:
            if when < before:
                return amount
        return ZERO

    def refund_deadline(self, when):
        """When the refund that applies at `when` lapses, or None if there is none."""
        for before, _ in self.cancellation_timeline:
            if when < before:
                return before
        return None

    def at_least_as_refundable_as(self, other, when):
        """True when, from `when` onwards, this rate never refunds a smaller
        share of its price than `other` does of its own."""
        if self.total <= 0 or other.total <= 0:
            return False
        moments = {when} | {before for before, _ in self.cancellation_timeline + other.cancellation_timeline
                            if before > when}
        return all(self.refund_at(m) / self.total >= other.refund_at(m) / other.total for m in moments)


@dataclass(frozen=True)
class Stay:
    """A booked stay, as the reshop decision sees it. `id` is TripDifference's
    own, stable across a rebooking; `booking_id` is whichever Duffel booking
    currently holds the room."""
    id: str
    booking_id: str
    accommodation_id: str
    accommodation_name: str
    check_in: str
    check_out: str
    rate: Rate
    rooms: int = 1
    adults: int = 1
    child_ages: tuple = ()
    loyalty_number: str = ""

    @classmethod
    def from_booking(cls, booking, id=None):
        """From a Duffel booking payload. The booked room and rate are read as
        the accommodation's first room and that room's first rate."""
        accommodation = booking.get("accommodation") or {}
        room = (accommodation.get("rooms") or [{}])[0]
        rate = (room.get("rates") or [{}])[0]
        guest_types = booking.get("guest_types") or []
        adults = sum(1 for g in guest_types if g.get("type") == "adult")
        return cls(
            id=id or booking.get("id") or "",
            booking_id=booking.get("id") or "",
            accommodation_id=accommodation.get("id") or "",
            accommodation_name=accommodation.get("name") or "",
            check_in=booking.get("check_in_date") or "",
            check_out=booking.get("check_out_date") or "",
            rate=Rate.from_duffel(rate, room, accommodation.get("id") or ""),
            rooms=int(booking.get("rooms") or 1),
            adults=adults or len(booking.get("guests") or []) or 1,
            child_ages=tuple(g.get("age") for g in guest_types if g.get("type") == "child"),
            loyalty_number=booking.get("loyalty_programme_account_number") or "",
        )


def rates_in(search):
    """Every rate of every room in a search response (or a single search result)."""
    results = search.get("results") if "results" in search else [search]
    rates = []
    for result in results or []:
        accommodation = result.get("accommodation") or {}
        for room in accommodation.get("rooms") or []:
            identity = Room.from_duffel(room)
            rates.extend(Rate.from_duffel(rate, identity, accommodation.get("id") or "")
                         for rate in room.get("rates") or [])
    return rates


# ---------------------------------------------------------------------------
# the calls
# ---------------------------------------------------------------------------

def _request(method, path, **kwargs):
    try:
        return duffel_http.request(method, path, **kwargs)
    except duffel_http.DuffelError as exc:
        if exc.status == 403 and "not enabled" in str(exc).lower():
            raise StaysNotEnabled("Duffel Stays is not enabled for this Duffel account — access has to be "
                                  "requested from Duffel (https://duffel.com/contact-us).") from exc
        raise


def _occupancy(check_in, check_out, adults, child_ages, rooms):
    guests = [{"type": "adult"}] * adults + [{"type": "child", "age": age} for age in child_ages]
    return {"check_in_date": check_in, "check_out_date": check_out, "rooms": rooms, "guests": guests}


def search_near(latitude, longitude, *, check_in, check_out, adults=1, child_ages=(), rooms=1, radius_km=5):
    """Accommodation around a point, each with its cheapest rate only — rooms
    and rates come from fetch_all_rates on the chosen result."""
    body = {"data": {**_occupancy(check_in, check_out, adults, child_ages, rooms),
                     "location": {"radius": radius_km,
                                  "geographic_coordinates": {"latitude": latitude, "longitude": longitude}}}}
    return _request("POST", "/stays/search", body=body, label="stays_search")


def search_accommodation(accommodation_id, *, check_in, check_out, adults=1, child_ages=(), rooms=1):
    """One property with every room and rate: the re-price a reshop needs."""
    body = {"data": {**_occupancy(check_in, check_out, adults, child_ages, rooms),
                     "accommodation": {"ids": [accommodation_id], "fetch_rates": True}}}
    return _request("POST", "/stays/search", body=body, label="stays_search_accommodation")


def fetch_all_rates(search_result_id):
    return _request("POST", f"/stays/search_results/{search_result_id}/actions/fetch_all_rates",
                    label="stays_fetch_all_rates")


def quote(rate_id):
    """Confirms the rate is still available at its price. A booking is made from the quote."""
    return _request("POST", "/stays/quotes", body={"data": {"rate_id": rate_id}}, label="stays_quote")


def book(quote_id, *, guests, email, phone_number, loyalty_number="", special_requests="", metadata=None):
    """Books the quote, paid from the Duffel balance: the body carries no
    `payment`, which is how Duffel is told to use the balance. `guests` is a
    list of {"given_name", "family_name"}."""
    data = {"quote_id": quote_id, "guests": list(guests), "email": email, "phone_number": phone_number}
    if loyalty_number:
        data["loyalty_programme_account_number"] = loyalty_number
    if special_requests:
        data["accommodation_special_requests"] = special_requests
    if metadata:
        data["metadata"] = metadata
    return _request("POST", "/stays/bookings", body={"data": data}, label="stays_booking")


def booking(booking_id):
    return _request("GET", f"/stays/bookings/{booking_id}", label="stays_booking_get")


def cancel(booking_id):
    """Cancels the booking. What comes back is whatever the rate's cancellation
    timeline allows at this moment, returned to the Duffel balance."""
    return _request("POST", f"/stays/bookings/{booking_id}/actions/cancel", label="stays_booking_cancel")
