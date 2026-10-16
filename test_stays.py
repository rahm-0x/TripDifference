"""
Stays tests: the value objects and Duffel calls (stays.py) and the rebook
decision (stays_engine.py).

No Duffel response here is real. This account does not have Stays enabled, so
there is nothing recorded to replay: payloads are built to Duffel's published
client types, and every shape below is unverified until a sandbox response
says otherwise.

    .venv/bin/python -m pytest test_stays.py -v
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

import duffel_http
import paths
import stays
import stays_engine
from engine import Outcome
from stays import Rate, Room, Stay
from stays_engine import Reason, StayPolicy, evaluate

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
ACC = "acc_test"
CHECK_IN, CHECK_OUT = "2026-12-08", "2026-12-10"
FREE_UNTIL = "2026-12-06T12:00:00Z"
KING = Room("Deluxe King Room", (("king", 1),))


@pytest.fixture(autouse=True)
def no_response_dumps(monkeypatch):
    monkeypatch.setattr(paths, "DUMP_RESPONSES", False)


def raw_rate(total="400.00", refundable_until=FREE_UNTIL, **over):
    """A pay-now rate, payable from the balance, fully refundable until `refundable_until`."""
    rate = dict(
        id="rat_new", total_amount=total, total_currency="USD", board_type="room_only",
        payment_type="pay_now", available_payment_methods=["balance", "card"],
        cancellation_timeline=([{"before": refundable_until, "refund_amount": total, "currency": "USD"}]
                               if refundable_until else []),
        supported_loyalty_programme=None, loyalty_programme_required=False,
        due_at_accommodation_amount="0.00", due_at_accommodation_currency="USD",
        source="duffel_hotel_group", code=None,
    )
    rate.update(over)
    return rate


def make_rate(total="400.00", room=KING, **over):
    return Rate.from_duffel(raw_rate(total, **over), room, ACC)


def make_stay(**over):
    """A refundable stay two months out. Override to break one thing."""
    base = dict(id="ord_stay", booking_id="bok_test", accommodation_id=ACC,
                accommodation_name="Duffel Test Hotel", check_in=CHECK_IN, check_out=CHECK_OUT,
                rate=make_rate(id="rat_booked"))
    base.update(over)
    return Stay(**base)


class Rates:
    name = "fixed"

    def __init__(self, *rates):
        self.rates = list(rates)

    def rates_for(self, stay):
        return self.rates


class ExplodingSource:
    """Any call is a bug — used to prove the cheap gates short-circuit."""
    name = "exploding"

    def rates_for(self, stay):
        raise AssertionError("rates_for must not be called")


def run(stay, source, policy=None, **kwargs):
    return evaluate(stay, source, policy=policy or StayPolicy(), now=NOW, **kwargs)


# ---------------------------------------------------------------------------
# value objects
# ---------------------------------------------------------------------------

def test_rate_reads_the_duffel_fields():
    rate = Rate.from_duffel(raw_rate("412.50", board_type="breakfast", supported_loyalty_programme="hilton_honors",
                                     due_at_accommodation_amount="39.95", code="CORP"),
                            {"name": "Deluxe King Room", "beds": [{"type": "king", "count": 1}]}, ACC)
    assert (rate.total, rate.currency, rate.board_type) == (Decimal("412.50"), "USD", "breakfast")
    assert (rate.loyalty_programme, rate.code, rate.accommodation_id) == ("hilton_honors", "CORP", ACC)
    assert rate.due_at_accommodation == Decimal("39.95")
    assert rate.room == KING
    assert rate.payable_from_balance


def test_refund_follows_the_timeline_one_step_at_a_time():
    first, second = NOW + timedelta(days=10), NOW + timedelta(days=20)
    rate = make_rate(cancellation_timeline=[
        {"before": second.isoformat(), "refund_amount": "200.00", "currency": "USD"},
        {"before": first.isoformat(), "refund_amount": "400.00", "currency": "USD"},
    ])
    assert rate.refund_at(NOW) == Decimal("400.00")
    assert rate.refund_deadline(NOW) == first
    assert rate.refund_at(first) == Decimal("200.00")      # the deadline itself is already too late
    assert rate.refund_at(second - timedelta(seconds=1)) == Decimal("200.00")
    assert rate.refund_at(second) == 0
    assert rate.refund_deadline(second) is None


@pytest.mark.parametrize("entry", [
    {"before": "not a date", "refund_amount": "400.00", "currency": "USD"},
    {"before": FREE_UNTIL, "refund_amount": None, "currency": "USD"},
    {"before": FREE_UNTIL, "refund_amount": "400.00", "currency": "GBP"},
])
def test_one_unreadable_timeline_entry_makes_the_rate_non_refundable(entry):
    good = {"before": "2026-11-01T00:00:00Z", "refund_amount": "400.00", "currency": "USD"}
    assert make_rate(cancellation_timeline=[good, entry]).refund_at(NOW) == 0


def test_no_timeline_is_non_refundable():
    assert make_rate(refundable_until=None).refund_at(NOW) == 0


@pytest.mark.parametrize("other,same", [
    (Room("  deluxe   KING room ", (("king", 1),)), True),
    (Room("Deluxe King Room"), True),                         # beds are optional in the payload
    (Room("Deluxe King Room", (("single", 2),)), False),
    (Room("Deluxe King Room with View", (("king", 1),)), False),
    (Room("Standard Room", (("king", 1),)), False),
    (None, False),
])
def test_room_identity_is_the_name_and_the_beds_when_both_list_them(other, same):
    assert KING.matches(other) is same


def test_a_room_with_no_name_matches_nothing():
    assert not Room("").matches(Room(""))


@pytest.mark.parametrize("payment_type,methods,payable", [
    ("pay_now", ["balance", "card"], True),
    ("pay_now", ["balance"], True),
    ("pay_now", ["card"], False),         # the hotel would be merchant of record
    ("guarantee", ["balance", "card"], False),
    ("deposit", ["balance"], False),
    ("", [], False),
])
def test_only_a_pay_now_balance_rate_is_payable_from_the_balance(payment_type, methods, payable):
    rate = make_rate(payment_type=payment_type, available_payment_methods=methods)
    assert rate.payable_from_balance is payable


def test_due_at_accommodation_null_is_unknown_not_zero():
    assert make_rate(due_at_accommodation_amount=None).due_at_accommodation is None
    assert make_rate(due_at_accommodation_currency="EUR").due_at_accommodation is None


def fake_booking():
    return {
        "id": "bok_test", "check_in_date": CHECK_IN, "check_out_date": CHECK_OUT, "rooms": 1,
        "guests": [{"given_name": "Ada", "family_name": "Lovelace"}],
        "guest_types": [{"type": "adult", "age": None}, {"type": "adult", "age": None}, {"type": "child", "age": 7}],
        "loyalty_programme_account_number": "H123",
        "accommodation": {"id": ACC, "name": "Duffel Test Hotel", "rooms": [
            {"name": "Deluxe King Room", "beds": [{"type": "king", "count": 1}],
             "rates": [raw_rate(id="rat_booked")]}]},
    }


def test_stay_from_a_booking_payload():
    stay = Stay.from_booking(fake_booking(), id="ord_stay")
    assert (stay.id, stay.booking_id, stay.accommodation_id) == ("ord_stay", "bok_test", ACC)
    assert (stay.check_in, stay.check_out, stay.rooms) == (CHECK_IN, CHECK_OUT, 1)
    assert (stay.adults, stay.child_ages, stay.loyalty_number) == (2, (7,), "H123")
    assert stay.rate.id == "rat_booked" and stay.rate.room == KING
    assert stay.rate.refund_at(NOW) == Decimal("400.00")


def test_stay_id_defaults_to_the_booking_id():
    assert Stay.from_booking(fake_booking()).id == "bok_test"


def test_rates_in_flattens_every_room_of_every_result():
    search = {"results": [{"accommodation": {"id": ACC, "rooms": [
        {"name": "Deluxe King Room", "rates": [raw_rate(id="rat_a"), raw_rate(id="rat_b")]},
        {"name": "Standard Room", "rates": [raw_rate(id="rat_c")]}]}}]}
    rates = stays.rates_in(search)
    assert [(r.id, r.room.name, r.accommodation_id) for r in rates] == [
        ("rat_a", "Deluxe King Room", ACC), ("rat_b", "Deluxe King Room", ACC), ("rat_c", "Standard Room", ACC)]
    # fetch_all_rates returns one result, not a list of them
    assert len(stays.rates_in(search["results"][0])) == 3


# ---------------------------------------------------------------------------
# the Duffel calls
# ---------------------------------------------------------------------------

def test_reprice_searches_one_property_and_asks_for_every_rate():
    with patch("stays.duffel_http.request", return_value={"results": []}) as sent:
        stays.search_accommodation(ACC, check_in=CHECK_IN, check_out=CHECK_OUT, adults=2, child_ages=(7,))
    (method, path), kwargs = sent.call_args
    assert (method, path) == ("POST", "/stays/search")
    assert kwargs["body"] == {"data": {
        "check_in_date": CHECK_IN, "check_out_date": CHECK_OUT, "rooms": 1,
        "guests": [{"type": "adult"}, {"type": "adult"}, {"type": "child", "age": 7}],
        "accommodation": {"ids": [ACC], "fetch_rates": True}}}


def test_search_near_sends_a_point_and_a_radius():
    with patch("stays.duffel_http.request", return_value={"results": []}) as sent:
        stays.search_near(51.5071, -0.1416, check_in=CHECK_IN, check_out=CHECK_OUT, radius_km=2)
    data = sent.call_args.kwargs["body"]["data"]
    assert data["location"] == {"radius": 2, "geographic_coordinates": {"latitude": 51.5071, "longitude": -0.1416}}
    assert data["guests"] == [{"type": "adult"}] and "accommodation" not in data


def test_a_booking_is_paid_from_the_balance_so_it_carries_no_payment():
    guests = [{"given_name": "Ada", "family_name": "Lovelace"}]
    with patch("stays.duffel_http.request", return_value={"id": "bok_x"}) as sent:
        stays.book("quo_x", guests=guests, email="ada@acme.test", phone_number="+15550100", loyalty_number="H123")
    (method, path), kwargs = sent.call_args
    assert (method, path) == ("POST", "/stays/bookings")
    assert kwargs["body"] == {"data": {"quote_id": "quo_x", "guests": guests, "email": "ada@acme.test",
                                       "phone_number": "+15550100", "loyalty_programme_account_number": "H123"}}


@pytest.mark.parametrize("call,method,path", [
    (lambda: stays.fetch_all_rates("srr_x"), "POST", "/stays/search_results/srr_x/actions/fetch_all_rates"),
    (lambda: stays.quote("rat_x"), "POST", "/stays/quotes"),
    (lambda: stays.booking("bok_x"), "GET", "/stays/bookings/bok_x"),
    (lambda: stays.cancel("bok_x"), "POST", "/stays/bookings/bok_x/actions/cancel"),
])
def test_each_call_hits_its_endpoint(call, method, path):
    with patch("stays.duffel_http.request", return_value={}) as sent:
        call()
    assert sent.call_args.args == (method, path)


NOT_ENABLED = "This feature is not enabled for your account. Please contact sales to get access"


def _plain_text_response(status, text):
    def no_json():
        raise ValueError("not JSON")
    return MagicMock(status_code=status, ok=False, headers={}, text=text, json=no_json)


def test_an_account_without_stays_raises_stays_not_enabled():
    """What this account answers today: 403 and a plain-text body."""
    with patch("duffel_http.requests.request", return_value=_plain_text_response(403, NOT_ENABLED)):
        with pytest.raises(stays.StaysNotEnabled, match="requested from Duffel"):
            stays.search_near(51.5, -0.14, check_in=CHECK_IN, check_out=CHECK_OUT)


def test_any_other_duffel_error_is_left_alone():
    with patch("duffel_http.requests.request", return_value=_plain_text_response(403, "Forbidden")):
        with pytest.raises(duffel_http.DuffelError) as raised:
            stays.quote("rat_x")
    assert not isinstance(raised.value, stays.StaysNotEnabled)


def test_duffel_source_reprices_the_booked_property_for_the_same_dates_and_guests():
    stay = make_stay(adults=2, child_ages=(7,), rooms=2)
    search = {"results": [{"accommodation": {"id": ACC, "rooms": [
        {"name": "Deluxe King Room", "rates": [raw_rate("340.00")]}]}}]}
    with patch("stays_engine.stays.search_accommodation", return_value=search) as sent:
        rates = stays_engine.DuffelStayRates().rates_for(stay)
    sent.assert_called_once_with(ACC, check_in=CHECK_IN, check_out=CHECK_OUT, adults=2, child_ages=(7,), rooms=2)
    assert [r.total for r in rates] == [Decimal("340.00")]


# ---------------------------------------------------------------------------
# the decision: gates that cost nothing
# ---------------------------------------------------------------------------

def test_cheaper_rate_for_the_same_room_rebooks():
    d = run(make_stay(), Rates(make_rate("340.00")))
    assert d.outcome is Outcome.RESHOP and d.should_rebook
    assert d.reason is Reason.CHEAPER_SAME_ROOM
    assert (d.refund_now, d.new_total, d.saving) == (Decimal("400.00"), Decimal("340.00"), Decimal("60.00"))
    assert d.new_rate_id == "rat_new"
    assert (d.candidates, d.same_room, d.comparable) == (1, 1, 1)


def test_non_refundable_stay_is_never_searched():
    d = run(make_stay(rate=make_rate(refundable_until=None)), ExplodingSource())
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.NOT_REFUNDABLE


def test_a_refund_window_that_has_closed_is_non_refundable():
    closed = (NOW - timedelta(hours=1)).isoformat()
    d = run(make_stay(rate=make_rate(refundable_until=closed)), ExplodingSource())
    assert d.reason is Reason.NOT_REFUNDABLE


def test_too_close_to_check_in_skips_before_searching():
    soon = (NOW + timedelta(hours=20)).date().isoformat()
    d = run(make_stay(check_in=soon), ExplodingSource())
    assert d.reason is Reason.TOO_CLOSE_TO_CHECK_IN


def test_refund_about_to_lapse_skips_before_searching():
    """Rebooking books first and cancels second; a cancel after the deadline pays for two rooms."""
    lapsing = (NOW + timedelta(hours=3)).isoformat()
    d = run(make_stay(rate=make_rate(refundable_until=lapsing)), ExplodingSource())
    assert d.reason is Reason.REFUND_DEADLINE_TOO_CLOSE
    assert d.refund_now == Decimal("400.00")


def test_refund_deadline_buffer_is_inclusive_of_its_edge():
    edge = (NOW + timedelta(hours=6)).isoformat()
    d = run(make_stay(rate=make_rate(refundable_until=edge)), Rates(make_rate("340.00")),
            policy=StayPolicy(refund_deadline_buffer_hours=6))
    assert d.reason is Reason.CHEAPER_SAME_ROOM


# ---------------------------------------------------------------------------
# the decision: is it the same product?
# ---------------------------------------------------------------------------

def test_no_rates_skips():
    d = run(make_stay(), Rates())
    assert d.reason is Reason.NO_RATES


def test_rates_for_another_property_are_ignored():
    elsewhere = Rate.from_duffel(raw_rate("100.00"), KING, "acc_other")
    d = run(make_stay(), Rates(elsewhere))
    assert d.reason is Reason.NO_RATES


def test_a_cheaper_different_room_is_not_a_saving():
    """Same hotel is not the same room, as same route is not the same flight."""
    d = run(make_stay(), Rates(make_rate("150.00", room=Room("Standard Twin Room", (("single", 2),)))))
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.NO_MATCHING_ROOM
    assert (d.candidates, d.same_room) == (1, 0)
    assert d.new_total is None


LATER = "2026-12-07T12:00:00Z"
EARLIER = "2026-11-01T12:00:00Z"


@pytest.mark.parametrize("booked,candidate,why", [
    ({}, dict(payment_type="guarantee"), "not payable in full from the Duffel balance"),
    ({}, dict(available_payment_methods=["card"]), "not payable in full from the Duffel balance"),
    ({}, dict(total_currency="GBP"), "priced in another currency"),
    ({}, dict(refundable_until=None), "stricter cancellation"),
    ({}, dict(refundable_until=EARLIER), "stricter cancellation"),
    (dict(board_type="breakfast"), dict(board_type="room_only"), "worse board"),
    (dict(supported_loyalty_programme="hilton_honors"), {}, "loses the loyalty programme"),
    (dict(supported_loyalty_programme="hilton_honors"), dict(supported_loyalty_programme="marriott_bonvoy"),
     "loses the loyalty programme"),
    ({}, dict(loyalty_programme_required=True), "needs a loyalty number"),
    ({}, dict(due_at_accommodation_amount=None), "fees at the property unknown on one side"),
    (dict(due_at_accommodation_amount=None), {}, "fees at the property unknown on one side"),
])
def test_the_same_room_on_worse_terms_is_not_comparable(booked, candidate, why):
    stay = make_stay(rate=make_rate(id="rat_booked", **booked))
    d = run(stay, Rates(make_rate("150.00", **candidate)))
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.NO_COMPARABLE_RATE
    assert f"1 {why}" in d.detail
    assert (d.same_room, d.comparable) == (1, 0)


@pytest.mark.parametrize("booked,candidate", [
    ({}, dict(board_type="breakfast")),
    ({}, dict(refundable_until=LATER)),
    ({}, dict(supported_loyalty_programme="hilton_honors")),
    (dict(supported_loyalty_programme="hilton_honors"), dict(supported_loyalty_programme="hilton_honors")),
    (dict(due_at_accommodation_amount=None), dict(due_at_accommodation_amount=None)),
])
def test_the_same_room_on_terms_at_least_as_good_is_comparable(booked, candidate):
    stay = make_stay(rate=make_rate(id="rat_booked", **booked))
    d = run(stay, Rates(make_rate("340.00", **candidate)))
    assert d.reason is Reason.CHEAPER_SAME_ROOM


def test_a_members_only_rate_is_comparable_when_the_guest_has_a_number():
    members = make_rate("340.00", supported_loyalty_programme="hilton_honors", loyalty_programme_required=True)
    d = run(make_stay(loyalty_number="H123"), Rates(members))
    assert d.reason is Reason.CHEAPER_SAME_ROOM


def test_worse_terms_are_skipped_in_favour_of_a_dearer_comparable_rate():
    """The cheapest rate in the room is not the answer if it is a different deal."""
    non_refundable = make_rate("250.00", id="rat_nonref", refundable_until=None)
    refundable = make_rate("360.00", id="rat_ref")
    d = run(make_stay(), Rates(non_refundable, refundable))
    assert d.reason is Reason.CHEAPER_SAME_ROOM
    assert (d.new_rate_id, d.saving) == ("rat_ref", Decimal("40.00"))
    assert (d.same_room, d.comparable) == (2, 1)


# ---------------------------------------------------------------------------
# the decision: the numbers
# ---------------------------------------------------------------------------

def test_same_price_is_not_cheaper():
    d = run(make_stay(), Rates(make_rate("400.00")))
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.NOT_CHEAPER
    assert d.saving == 0


def test_saving_below_the_floor_skips():
    d = run(make_stay(), Rates(make_rate("395.00")))
    assert d.reason is Reason.BELOW_FLOOR
    assert d.saving == Decimal("5.00")
    assert "not worth doing" in d.detail


def test_saving_exactly_at_the_floor_rebooks():
    d = run(make_stay(), Rates(make_rate("380.00")), policy=StayPolicy(min_saving=Decimal("20.00")))
    assert d.reason is Reason.CHEAPER_SAME_ROOM
    assert d.saving == Decimal("20.00")


def half_refundable_stay():
    """Paid 400; the full-refund window has closed and cancelling now returns 200."""
    return make_stay(rate=make_rate(id="rat_booked", cancellation_timeline=[
        {"before": (NOW - timedelta(days=1)).isoformat(), "refund_amount": "400.00", "currency": "USD"},
        {"before": FREE_UNTIL, "refund_amount": "200.00", "currency": "USD"},
    ]))


def test_the_saving_is_measured_from_the_refund_not_from_what_was_paid():
    """340 is cheaper than the 400 paid, and still a loss: cancelling only returns 200."""
    d = run(half_refundable_stay(), Rates(make_rate("340.00")))
    assert d.reason is Reason.NOT_CHEAPER
    assert (d.paid, d.refund_now, d.saving) == (Decimal("400.00"), Decimal("200.00"), Decimal("-140.00"))


def test_a_partial_refund_can_still_clear_the_floor():
    d = run(half_refundable_stay(), Rates(make_rate("150.00")))
    assert d.reason is Reason.CHEAPER_SAME_ROOM
    assert d.saving == Decimal("50.00")


def test_higher_fees_at_the_property_come_off_the_saving():
    d = run(make_stay(), Rates(make_rate("350.00", due_at_accommodation_amount="40.00")))
    assert d.reason is Reason.BELOW_FLOOR
    assert (d.extra_due_at_accommodation, d.saving) == (Decimal("40.00"), Decimal("10.00"))


def test_lower_fees_at_the_property_are_not_counted_as_recovered_money():
    booked = make_rate(id="rat_booked", due_at_accommodation_amount="40.00")
    d = run(make_stay(rate=booked), Rates(make_rate("395.00", due_at_accommodation_amount="0.00")))
    assert d.reason is Reason.BELOW_FLOOR
    assert (d.extra_due_at_accommodation, d.saving) == (0, Decimal("5.00"))


def test_the_best_rate_is_the_cheapest_all_in():
    resort_fee = make_rate("330.00", id="rat_fee", due_at_accommodation_amount="30.00")
    plain = make_rate("340.00", id="rat_plain")
    d = run(make_stay(), Rates(resort_fee, plain))
    assert (d.new_rate_id, d.saving) == ("rat_plain", Decimal("60.00"))


def test_fee_split_uses_the_accounts_own_rate():
    d = run(make_stay(), Rates(make_rate("340.00")), commission_rate=Decimal("0.30"))
    assert (d.recovered, d.service_fee, d.net_to_customer) == (Decimal("60.00"), Decimal("18.00"), Decimal("42.00"))
    assert "our fee 18.00" in d.detail


def test_no_fee_split_is_guessed_without_a_rate():
    d = run(make_stay(), Rates(make_rate("340.00")))
    assert (d.recovered, d.service_fee, d.net_to_customer) == (None, None, None)
    assert "our fee" not in d.detail


def test_a_rebook_decision_says_what_it_ties_up():
    d = run(make_stay(), Rates(make_rate("340.00")))
    assert "two rooms on the Duffel balance (340.00 USD more)" in d.detail


def test_decision_serialises_money_and_enums_as_strings():
    out = run(make_stay(), Rates(make_rate("340.00"))).to_json()
    assert (out["outcome"], out["reason"]) == ("reshop", "cheaper_same_room")
    assert (out["paid"], out["refund_now"], out["saving"]) == ("400.00", "400.00", "60.00")
    assert (out["order_id"], out["source"], out["room"]) == ("ord_stay", "fixed", "Deluxe King Room")
