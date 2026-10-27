"""
Cars and the cars & stays search screens: cars.py, places.py, /cars, /stays,
and the panel on Home.

test_fixtures/duffel_cars_search.json is a real sandbox response (Duffel Test
Drive, 2026-10-21). The place and Stays payloads are not: Stays isn't enabled
on the Duffel account, so its result shape is Duffel's published client types.

    .venv/bin/python -m pytest test_cars.py -v
"""

import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

import cars
import duffel_http
import paths
import places
import stays
from test_money_path import CSRF, acct, client, logged_in  # noqa: F401

SEARCH = json.loads((Path(__file__).parent / "test_fixtures" / "duffel_cars_search.json").read_text())["data"]
LIVE_TOKEN = "duffel_live_" + "0" * 32
PICKUP = (date.today() + timedelta(days=30)).isoformat()
DROPOFF = (date.today() + timedelta(days=33)).isoformat()

LHR = {"type": "airport", "iata_code": "LHR", "name": "Heathrow Airport",
       "latitude": 51.470311, "longitude": -0.458118}
LONDON = {"type": "city", "iata_code": "LON", "name": "London", "latitude": None, "longitude": None}


@pytest.fixture(autouse=True)
def no_response_dumps(monkeypatch):
    monkeypatch.setattr(paths, "DUMP_RESPONSES", False)


# ---------------------------------------------------------------------------
# cars.py
# ---------------------------------------------------------------------------

def test_every_test_drive_rate_reads_cheapest_first():
    rates = cars.rates_in(SEARCH)
    assert len(rates) == 7
    assert [r.total for r in rates] == sorted(r.total for r in rates)
    first = rates[0]
    assert first.car_name == "Successful Prepaid Booking"
    assert first.total == Decimal("94.62") and first.currency == "GBP"
    assert first.supplier == "Duffel Test Drive"
    assert (first.category, first.code, first.transmission) == ("compact", "CDAV", "automatic")
    assert (first.max_passengers, first.large_bags, first.small_bags) == (5, 3, 2)
    assert first.mileage == "unlimited"
    assert first.image_url.startswith("https://")
    assert first.pickup_name == "Duffel Test Drive Pickup"


@pytest.mark.parametrize("payment_type,label", [
    ("prepaid", "Paid when booked"),
    ("postpaid", "Pay at the counter"),
    ("guarantee", "Card held · pay at the counter"),
    ("something_new", "Something new"),
])
def test_payment_type_reads_as_who_takes_the_money(payment_type, label):
    assert cars.Rate.from_duffel({"payment_type": payment_type}).payment_label == label


def test_a_rate_with_nothing_in_it_still_reads():
    rate = cars.Rate.from_duffel({"total_amount": "not money"})
    assert rate.total == Decimal("0") and rate.image_url == "" and rate.max_passengers is None


def test_search_sends_one_point_for_pick_up_and_drop_off_and_caps_the_radius():
    with patch("duffel_http.request", return_value=SEARCH) as sent:
        cars.search(1.5, 2.5, pickup_date=PICKUP, pickup_time="10:00", dropoff_date=DROPOFF,
                    dropoff_time="09:00", driver_age=40, residence_country="US", radius=50)
    method, path = sent.call_args.args
    data = sent.call_args.kwargs["body"]["data"]
    assert (method, path) == ("POST", "/cars/search")
    assert data["pickup_location"] == data["dropoff_location"] == {
        "radius": 10, "geographic_coordinates": {"latitude": 1.5, "longitude": 2.5}}
    assert data["driver"] == {"age": 40, "residence_country_code": "US"}
    assert (data["pickup_date"], data["dropoff_time"]) == (PICKUP, "09:00")


# ---------------------------------------------------------------------------
# places.py
# ---------------------------------------------------------------------------

def test_an_airport_code_resolves_to_its_coordinates():
    with patch("duffel_http.request", return_value=[LHR]) as sent:
        airport = places.airport(" lhr ")
    assert airport == places.Airport("LHR", "Heathrow Airport", 51.470311, -0.458118)
    assert sent.call_args.kwargs["params"] == {"query": "LHR"}


def test_a_city_code_says_to_use_an_airport():
    with patch("duffel_http.request", return_value=[LONDON, {**LHR, "iata_code": "STN"}]):
        with pytest.raises(places.PlaceNotFound, match="city code"):
            places.airport("LON")


def test_an_unknown_code_is_not_found():
    with patch("duffel_http.request", return_value=[]):
        with pytest.raises(places.PlaceNotFound, match="No airport"):
            places.airport("QQQ")


@pytest.mark.parametrize("code", ["", "LH", "LHRX", "L1R"])
def test_a_malformed_code_is_refused_before_asking_duffel(code):
    with patch("duffel_http.request") as sent:
        with pytest.raises(places.PlaceNotFound, match="three-letter"):
            places.airport(code)
    sent.assert_not_called()


# ---------------------------------------------------------------------------
# /cars
# ---------------------------------------------------------------------------

def test_cars_page_with_nothing_asked_searches_nothing(logged_in):
    client, _ = logged_in
    with patch("duffel_http.request") as sent:
        resp = client.get("/cars")
    assert resp.status_code == 200
    assert b"Search cars" in resp.data and b"Duffel Test Drive instead" in resp.data
    sent.assert_not_called()


def test_cars_at_test_drive_lists_every_rate_and_offers_no_booking(logged_in):
    client, _ = logged_in
    with patch("cars.search", return_value=SEARCH) as searched, patch("places.airport") as looked_up:
        resp = client.get("/cars", query_string={"test_drive": "1", "pickup_date": PICKUP,
                                                 "dropoff_date": DROPOFF})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert (searched.call_args.args, searched.call_args.kwargs["radius"]) == (cars.TEST_DRIVE, 1)
    looked_up.assert_not_called()
    assert "7 cars at Duffel Test Drive (sandbox)" in body
    assert "94.62" in body and "Pay at the counter" in body
    assert "booking a car isn't available yet" in body
    # Test Drive's rate names say "Booking" ("Successful Prepaid Booking"), so
    # look for a control that books, not for the word.
    assert not re.search(r"<(a|button)\b[^>]*>[^<]*\bBook\b", body)
    assert "/cars/book" not in body and "cars/quotes" not in body


def test_cars_at_an_airport_searches_around_it(logged_in):
    client, _ = logged_in
    with patch("places.airport", return_value=places.Airport("LHR", "Heathrow Airport", 51.47, -0.45)), \
         patch("cars.search", return_value={"rates": []}) as searched:
        resp = client.get("/cars", query_string={"location": "lhr", "pickup_date": PICKUP,
                                                 "dropoff_date": DROPOFF, "pickup_time": "08:30"})
    body = resp.get_data(as_text=True)
    assert searched.call_args.args == (51.47, -0.45)
    assert searched.call_args.kwargs["pickup_time"] == "08:30"
    assert "0 cars at Heathrow Airport (LHR)" in body
    assert "search there with the same dates" in body


@pytest.mark.parametrize("args,message", [
    ({"pickup_date": "2020-01-01", "dropoff_date": "2020-01-03"}, "in the past"),
    ({"pickup_date": DROPOFF, "dropoff_date": PICKUP}, "before the pick-up date"),
    ({"pickup_date": PICKUP, "dropoff_date": PICKUP, "pickup_time": "10:00", "dropoff_time": "09:00"},
     "Drop-off is before pick-up"),
    ({"pickup_date": PICKUP, "dropoff_date": DROPOFF, "pickup_time": "25:00"}, "HH:MM"),
    ({"pickup_date": PICKUP, "dropoff_date": DROPOFF, "country": "USA"}, "two-letter"),
    ({"pickup_date": "soon"}, "dates are required"),
])
def test_cars_search_that_cant_run_says_why_and_sends_nothing(logged_in, args, message):
    client, _ = logged_in
    with patch("duffel_http.request") as sent:
        resp = client.get("/cars", query_string={"location": "LHR", **args})
    assert message in resp.get_data(as_text=True)
    sent.assert_not_called()


def test_cars_shows_a_place_it_cant_find(logged_in):
    client, _ = logged_in
    with patch("duffel_http.request", return_value=[LONDON]):
        resp = client.get("/cars", query_string={"location": "LON", "pickup_date": PICKUP, "dropoff_date": DROPOFF})
    assert "LON is a city code" in resp.get_data(as_text=True)


def test_test_drive_is_ignored_without_a_sandbox_token(logged_in, monkeypatch):
    """With a live token the page refuses up front, so the box is never offered either."""
    client, _ = logged_in
    monkeypatch.setenv("DUFFEL_TOKEN", LIVE_TOKEN)
    with patch("duffel_http.request") as sent:
        resp = client.get("/cars", query_string={"test_drive": "1", "pickup_date": PICKUP, "dropoff_date": DROPOFF})
    body = resp.get_data(as_text=True)
    assert "sandbox-only" in body and "Duffel Test Drive instead" not in body
    sent.assert_not_called()


# ---------------------------------------------------------------------------
# /stays
# ---------------------------------------------------------------------------

def test_stays_says_hotels_arent_on_yet(logged_in):
    client, _ = logged_in
    with patch("places.airport", return_value=places.Airport("JFK", "JFK Airport", 40.64, -73.78)), \
         patch("stays.search_near", side_effect=stays.StaysNotEnabled("off")):
        resp = client.get("/stays", query_string={"location": "JFK", "check_in": PICKUP, "check_out": DROPOFF})
    assert resp.status_code == 200
    assert "Hotel search isn't on yet" in resp.get_data(as_text=True)


def test_stays_lists_each_hotel_with_its_cheapest_rate(logged_in):
    client, _ = logged_in
    result = {"accommodation": {"name": "The Jet Hotel", "rating": 4, "review_score": 8.7,
                                "location": {"address": {"line_one": "1 Terminal Way", "city_name": "Queens"}},
                                "photos": [{"url": "https://example.test/p.jpg"}]},
              "cheapest_rate_total_amount": "412.50", "cheapest_rate_currency": "USD"}
    with patch("places.airport", return_value=places.Airport("JFK", "JFK Airport", 40.64, -73.78)), \
         patch("stays.search_near", return_value={"results": [result]}) as searched:
        resp = client.get("/stays", query_string={"location": "JFK", "check_in": PICKUP, "check_out": DROPOFF,
                                                  "adults": "2", "rooms": "1"})
    body = resp.get_data(as_text=True)
    assert searched.call_args.args == (40.64, -73.78)
    assert searched.call_args.kwargs == {"check_in": PICKUP, "check_out": DROPOFF, "adults": 2, "rooms": 1}
    assert "1 hotel near JFK Airport (JFK)" in body
    assert "The Jet Hotel" in body and "412.50" in body and "1 Terminal Way, Queens" in body


@pytest.mark.parametrize("args,message", [
    ({"check_in": PICKUP, "check_out": PICKUP}, "at least a night"),
    ({"check_in": PICKUP, "check_out": DROPOFF, "adults": "1", "rooms": "2"}, "at least one adult"),
    ({"check_in": DROPOFF, "check_out": PICKUP}, "before the check-in date"),
])
def test_stays_search_that_cant_run_says_why_and_sends_nothing(logged_in, args, message):
    client, _ = logged_in
    with patch("duffel_http.request") as sent:
        resp = client.get("/stays", query_string={"location": "JFK", **args})
    assert message in resp.get_data(as_text=True)
    sent.assert_not_called()


def test_stays_with_a_live_token_refuses_up_front(logged_in, monkeypatch):
    client, _ = logged_in
    monkeypatch.setenv("DUFFEL_TOKEN", LIVE_TOKEN)
    with patch("duffel_http.request") as sent:
        resp = client.get("/stays", query_string={"location": "JFK", "check_in": PICKUP, "check_out": DROPOFF})
    assert "sandbox-only" in resp.get_data(as_text=True)
    sent.assert_not_called()


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

def test_home_has_a_cars_and_stays_panel_that_searches_nothing(logged_in):
    client, _ = logged_in
    with patch("duffel_http.request") as sent:
        resp = client.get("/overview")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Cars &amp; stays" in body
    assert 'action="/cars"' in body and 'action="/stays"' in body
    sent.assert_not_called()


def test_home_says_cars_and_stays_are_off_with_a_live_token(logged_in, monkeypatch):
    client, _ = logged_in
    monkeypatch.setenv("DUFFEL_TOKEN", LIVE_TOKEN)
    body = client.get("/overview").get_data(as_text=True)
    assert "sandbox-only" in body and 'action="/cars"' not in body
