"""
Price source abstraction.

The reshop engine talks to a PriceSource and must never know which
implementation it has. That is the whole point: Duffel sandbox hardcodes
change_total_amount to +125.00 (see FINDINGS.md §1), so the only way to test
the decision logic against a real price drop is to swap the source.

  DuffelPriceSource    — live sandbox calls. What ships.
  SimulatedPriceSource — reads simulated_prices.json, which you can edit to
                         force a drop of any size, including a negative
                         change_total.

Select with PRICE_SOURCE=duffel|simulated, or --source on the CLI.

All money is Decimal. Duffel returns amounts as strings; float would lose cents.
"""

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import duffel_http
import paths

SIM_FILE = paths.data_path("simulated_prices.json")


# ---------------------------------------------------------------------------
# value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Route:
    origin: str
    destination: str

    def __str__(self):
        return f"{self.origin}-{self.destination}"


@dataclass(frozen=True)
class Itinerary:
    """
    Identity of a physical itinerary: the full ordered segment signature.

    Matching on route alone is not enough — a different flight number on the same
    city pair is a different itinerary, and a one-segment direct is not the same
    product as a two-segment connection even between the same airports.

    `carriers` holds the marketing carrier per segment, so a codeshare that
    reuses a flight number under a different carrier cannot masquerade as a
    match. It defaults to carrier_iata repeated, which keeps single-carrier
    construction terse.
    """
    carrier_iata: str
    flight_numbers: tuple
    carriers: tuple = ()

    @property
    def key(self):
        numbers = tuple(str(f) for f in self.flight_numbers)
        carriers = tuple(str(c).upper() for c in self.carriers)
        if len(carriers) != len(numbers):
            carriers = (self.carrier_iata.upper(),) * len(numbers)
        # Ordered pairs: segment count, order, carrier and number all matter.
        return tuple(zip(carriers, numbers))

    def matches(self, other):
        return other is not None and self.key == other.key

    def __str__(self):
        return " + ".join(f"{c}{n}" for c, n in self.key) or "—"


@dataclass(frozen=True)
class SliceSpec:
    """The slice we want to fly instead — what gets sent as slices.add."""
    origin: str
    destination: str
    departure_date: str
    cabin: str = "economy"


@dataclass(frozen=True)
class MarketOffer:
    id: str
    itinerary: Itinerary
    carrier_name: str
    total: Decimal
    currency: str
    departing_at: str = ""


@dataclass(frozen=True)
class ChangeOffer:
    """
    A priced exchange. change_total is the only number that decides anything —
    negative means money comes back.
    """
    id: str
    itinerary: Itinerary
    change_total: Decimal
    new_total: Decimal
    penalty: Decimal
    currency: str
    expires_at: str = ""


# ---------------------------------------------------------------------------
# the interface
# ---------------------------------------------------------------------------

class PriceSource(ABC):
    name = "abstract"

    @abstractmethod
    def get_current_price(self, route: Route, date: str, cabin: str) -> list:
        """Market offers for a route/date/cabin. Informational only — see engine."""

    @abstractmethod
    def price_change(self, order_id: str, slice_id: str, new_slice: SliceSpec) -> list:
        """Priced exchange options for replacing slice_id with new_slice."""


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------

def _itinerary_from_segments(segments):
    carriers, numbers = [], []
    for seg in segments:
        mc = seg.get("marketing_carrier") or {}
        carriers.append(mc.get("iata_code") or "")
        numbers.append(str(seg.get("marketing_carrier_flight_number") or ""))
    return Itinerary(carriers[0] if carriers else "", tuple(numbers), tuple(carriers))


class DuffelPriceSource(PriceSource):
    name = "duffel"

    def get_current_price(self, route, date, cabin="economy"):
        # FINDINGS.md §4: offer requests are single-use once booked from, so this
        # always issues a fresh one rather than caching.
        data = duffel_http.request("POST", "/air/offer_requests", body={
            "data": {
                "slices": [{
                    "origin": route.origin,
                    "destination": route.destination,
                    "departure_date": date,
                }],
                "passengers": [{"type": "adult"}],
                "cabin_class": cabin,
            }
        }, params={"return_offers": "true"}, label="ps_market")

        offers = []
        for o in data.get("offers", []):
            segments = [s for sl in o.get("slices", []) for s in sl.get("segments", [])]
            offers.append(MarketOffer(
                id=o["id"],
                itinerary=_itinerary_from_segments(segments),
                carrier_name=(o.get("owner") or {}).get("name", ""),
                total=Decimal(o["total_amount"]),
                currency=o["total_currency"],
                departing_at=segments[0].get("departing_at", "") if segments else "",
            ))
        return sorted(offers, key=lambda o: o.total)

    def price_change(self, order_id, slice_id, new_slice):
        data = duffel_http.request("POST", "/air/order_change_requests", body={
            "data": {
                "order_id": order_id,
                "slices": {
                    "remove": [{"slice_id": slice_id}],
                    "add": [{
                        "origin": new_slice.origin,
                        "destination": new_slice.destination,
                        "departure_date": new_slice.departure_date,
                        "cabin_class": new_slice.cabin,
                    }],
                },
            }
        }, label="ps_change")

        offers = []
        for o in data.get("order_change_offers", []):
            segments = [s for sl in (o.get("slices") or {}).get("add", [])
                        for s in sl.get("segments", [])]
            offers.append(ChangeOffer(
                id=o["id"],
                itinerary=_itinerary_from_segments(segments),
                change_total=Decimal(o["change_total_amount"]),
                new_total=Decimal(o["new_total_amount"]),
                penalty=Decimal(o.get("penalty_total_amount") or "0"),
                currency=o["change_total_currency"],
                expires_at=o.get("expires_at", ""),
            ))
        return sorted(offers, key=lambda o: o.change_total)


# ---------------------------------------------------------------------------
# simulated
# ---------------------------------------------------------------------------

DEFAULT_SIM = {
    "_readme": [
        "Edit any order below to force a scenario, then run a reshop cycle.",
        "change_total is the ONLY field that decides anything — negative = refund.",
        "market_price is deliberately independent so you can reproduce the trap:",
        "  a cheaper market price with a positive change_total must NOT reshop.",
        "Amounts are strings and parsed as Decimal.",
    ],
    "orders": {},
}


class SimulatedPriceSource(PriceSource):
    """
    Reads scenarios from a JSON file. Per-order entry:

        "ord_123": {
          "carrier": "ZZ",
          "flight_numbers": ["2623"],
          "currency": "USD",
          "market_price": "180.00",
          "change_total": "-45.00",
          "new_total": "180.00",
          "penalty": "0.00",
          "offer_itineraries": [...]   # optional extra non-matching itineraries
        }

    Anything missing falls back to a sane default so an unseeded order still
    returns something rather than exploding.
    """
    name = "simulated"

    def __init__(self, path=SIM_FILE, data=None):
        self.path = Path(path)
        self._data = data  # in-memory override, used by tests

    # -- storage ----------------------------------------------------------
    def load(self):
        if self._data is not None:
            return self._data
        if not self.path.exists():
            self.path.write_text(json.dumps(DEFAULT_SIM, indent=2))
        return json.loads(self.path.read_text())

    def save(self, data):
        if self._data is not None:
            self._data = data
            return
        self.path.write_text(json.dumps(data, indent=2))

    def scenario(self, order_id):
        return self.load().get("orders", {}).get(order_id)

    def set_scenario(self, order_id, **fields):
        data = self.load()
        entry = data.setdefault("orders", {}).setdefault(order_id, {})
        for k, v in fields.items():
            entry[k] = str(v) if isinstance(v, Decimal) else v
        self.save(data)
        return entry

    # -- interface --------------------------------------------------------
    def get_current_price(self, route, date, cabin="economy"):
        """
        Market offers across every seeded order matching this route, so the UI
        can show a plausible board. Identity matching happens in the engine.
        """
        offers = []
        for order_id, sc in self.load().get("orders", {}).items():
            if sc.get("route") and sc["route"] != str(route):
                continue
            itin = Itinerary(sc.get("carrier", "ZZ"), tuple(sc.get("flight_numbers", ["0000"])),
                             tuple(sc.get("carriers", ())))
            offers.append(MarketOffer(
                id=f"off_sim_{order_id}",
                itinerary=itin,
                carrier_name=sc.get("carrier_name", "Simulated Airways"),
                total=Decimal(sc.get("market_price", "0.00")),
                currency=sc.get("currency", "USD"),
                departing_at=sc.get("departing_at", ""),
            ))
        return sorted(offers, key=lambda o: o.total)

    def price_change(self, order_id, slice_id, new_slice):
        sc = self.scenario(order_id)
        if sc is None:
            return []

        currency = sc.get("currency", "USD")
        itin = Itinerary(sc.get("carrier", "ZZ"), tuple(sc.get("flight_numbers", ["0000"])),
                         tuple(sc.get("carriers", ())))
        change_total = Decimal(sc.get("change_total", "125.00"))
        new_total = Decimal(sc.get("new_total", "0.00"))
        penalty = Decimal(sc.get("penalty", "0.00"))

        offers = [ChangeOffer(
            id=f"oco_sim_{order_id}",
            itinerary=itin,
            change_total=change_total,
            new_total=new_total,
            penalty=penalty,
            currency=currency,
            expires_at=sc.get("expires_at", ""),
        )]

        # Optional decoys on other flight numbers, so identity matching is
        # actually exercised rather than trivially passing.
        for i, extra in enumerate(sc.get("decoys", [])):
            offers.append(ChangeOffer(
                id=f"oco_sim_{order_id}_decoy{i}",
                itinerary=Itinerary(extra.get("carrier", "ZZ"),
                                    tuple(extra.get("flight_numbers", ["9999"])),
                                    tuple(extra.get("carriers", ()))),
                change_total=Decimal(extra.get("change_total", "0.00")),
                new_total=Decimal(extra.get("new_total", "0.00")),
                penalty=Decimal(extra.get("penalty", "0.00")),
                currency=currency,
            ))
        return sorted(offers, key=lambda o: o.change_total)


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------

def get_price_source(name=None):
    """PRICE_SOURCE env var, or explicit name. Defaults to simulated — the safe one."""
    name = (name or os.environ.get("PRICE_SOURCE") or "simulated").lower()
    if name == "duffel":
        return DuffelPriceSource()
    if name == "simulated":
        return SimulatedPriceSource()
    raise ValueError(f"unknown price source '{name}' (expected 'duffel' or 'simulated')")
