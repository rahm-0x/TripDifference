"""
Duffel Cars: the search call, and the value object a results page reads.

Sandbox only. duffel_http refuses every /cars call made with a live token.
Unlike Stays, Cars is enabled on this Duffel account: what follows was seen
against the sandbox on 2026-10-21, and test_fixtures/duffel_cars_search.json is
a real search response.

  - Search is POST /cars/search around a point: pick-up and drop-off each take
    geographic_coordinates and a radius of at most 10. Sandbox returns no rates
    anywhere real (LHR, JFK, LAX, MIA, CDG all came back empty); only Duffel
    Test Drive, at TEST_DRIVE, does — one rate per scenario, named for it
    ("Successful Prepaid Booking", "Rate Unavailable", "Payment Declined", ...).
  - Quote (POST /cars/quotes, {"rate_id"}) adds `charges` and `conditions`
    (cancellation policy as free text). The same rate can be quoted twice.
  - Book (POST /cars/bookings) takes `driver` as an object, not the list the
    getting-started guide shows. A postpaid rate books with no payment; a
    prepaid one answers payment_required without one, and its `payment` takes
    only method "card" with a card_id or three_d_secure_session_id — there is
    no balance option.
  - Cancel (POST /cars/bookings/{id}/actions/cancel) sets status "cancelled"
    and cancelled_at, and says nothing about a refund. There is no change
    endpoint. GET /cars/bookings lists bookings, though the reference doesn't
    document it.

So nothing books a car here. TripDifference is merchant of record for what it
buys from its Duffel balance, and a car can't be bought that way: a postpaid or
guarantee rate is paid at the counter, and a prepaid one is charged to a card
Duffel holds.

All money is Decimal. Duffel returns amounts as strings.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import duffel_http

# Duffel Test Drive, the sandbox's only car supplier. Rates appear nowhere else.
TEST_DRIVE = (-24.38, -128.32)
MAX_RADIUS = 10

PAYMENT_LABELS = {
    "prepaid": "Paid when booked",
    "postpaid": "Pay at the counter",
    "guarantee": "Card held · pay at the counter",
}


def _money(value):
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Rate:
    """One car at one price from one supplier."""
    id: str
    total: Decimal
    currency: str
    payment_type: str
    car_name: str
    category: str = ""
    code: str = ""
    transmission: str = ""
    max_passengers: object = None
    large_bags: object = None
    small_bags: object = None
    air_conditioning: bool = False
    image_url: str = ""
    supplier: str = ""
    supplier_logo_url: str = ""
    mileage: str = ""
    pickup_name: str = ""

    @classmethod
    def from_duffel(cls, rate):
        car = rate.get("car") or {}
        supplier = rate.get("supplier") or {}
        baggage = car.get("baggage") or {}
        images = car.get("images") or []
        return cls(
            id=rate.get("id") or "",
            total=_money(rate.get("total_amount")) or Decimal("0"),
            currency=rate.get("total_currency") or "",
            payment_type=rate.get("payment_type") or "",
            car_name=car.get("name") or "",
            category=car.get("category") or "",
            code=car.get("code") or "",
            transmission=car.get("transmission") or "",
            max_passengers=_int(car.get("max_passengers")),
            large_bags=_int(baggage.get("large")),
            small_bags=_int(baggage.get("small")),
            air_conditioning=bool(car.get("air_conditioning")),
            image_url=((images[0] or {}).get("url") if images else None) or "",
            supplier=supplier.get("name") or "",
            supplier_logo_url=supplier.get("logo_url") or "",
            mileage=(rate.get("mileage") or {}).get("type") or "",
            pickup_name=(rate.get("pickup_location") or {}).get("name") or "",
        )

    @property
    def payment_label(self):
        return PAYMENT_LABELS.get(self.payment_type, self.payment_type.replace("_", " ").capitalize())


def rates_in(search):
    """Every rate in a search response, cheapest first."""
    rates = [Rate.from_duffel(r) for r in search.get("rates") or []]
    return sorted(rates, key=lambda r: (r.currency, r.total))


def search(latitude, longitude, *, pickup_date, pickup_time, dropoff_date, dropoff_time,
           driver_age, residence_country, radius=5):
    """Cars to pick up and return around one point."""
    location = {"radius": min(radius, MAX_RADIUS),
                "geographic_coordinates": {"latitude": latitude, "longitude": longitude}}
    body = {"data": {"pickup_date": pickup_date, "pickup_time": pickup_time,
                     "dropoff_date": dropoff_date, "dropoff_time": dropoff_time,
                     "pickup_location": location, "dropoff_location": location,
                     "driver": {"age": driver_age, "residence_country_code": residence_country}}}
    return duffel_http.request("POST", "/cars/search", body=body, label="cars_search")
