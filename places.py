"""
Where an airport is, for the searches that take a point rather than a code
(cars.search, stays.search_near).

Duffel's GET /places/suggestions gives an airport its coordinates. A city code
(LON, NYC) comes back with latitude and longitude null — only its airports carry
them — so only an airport code resolves here.
"""

import re
from dataclasses import dataclass

import duffel_http

_IATA = re.compile(r"^[A-Z]{3}$")


class PlaceNotFound(ValueError):
    pass


@dataclass(frozen=True)
class Airport:
    iata_code: str
    name: str
    latitude: float
    longitude: float


def airport(code):
    """The airport with this IATA code. Raises PlaceNotFound, with a message a
    person can act on, when the code is not one."""
    code = (code or "").strip().upper()
    if not _IATA.match(code):
        raise PlaceNotFound("Enter a three-letter airport code, like LHR.")
    places = duffel_http.request("GET", "/places/suggestions", params={"query": code},
                                 label="places_suggestions") or []
    for place in places:
        if (place.get("type") == "airport" and place.get("iata_code") == code
                and place.get("latitude") is not None and place.get("longitude") is not None):
            return Airport(code, place.get("name") or code, float(place["latitude"]), float(place["longitude"]))
    if any(p.get("type") == "city" and p.get("iata_code") == code for p in places):
        raise PlaceNotFound(f"{code} is a city code. Use one of its airports instead, like JFK for NYC.")
    raise PlaceNotFound(f"No airport has the code {code}.")
