"""
Reshop engine tests.

Every one of these runs against SimulatedPriceSource, because Duffel sandbox
cannot produce a negative change_total (FINDINGS.md §1). The engine cannot tell
the difference — that is the point of the abstraction.

    .venv/bin/python -m pytest test_engine.py -v
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from engine import (Decision, Outcome, Reason, OrderSnapshot, ReshopPolicy,
                    evaluate, log_decision)
from prices import Itinerary, Route, SimulatedPriceSource

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)
PAID = Decimal("216.93")


def make_order(**over):
    """A clean, changeable, far-from-departure order. Override to break one thing."""
    base = dict(
        id="ord_test", total=PAID, currency="USD", slice_id="sli_test",
        route=Route("LHR", "JFK"), itinerary=Itinerary("ZZ", ("2623",)),
        departure_date="2026-10-15",
        departing_at=NOW + timedelta(days=72),
        cabin="economy", changeable=True, void_window_ends_at=None,
    )
    base.update(over)
    return OrderSnapshot(**base)


def make_source(**scenario):
    """SimulatedPriceSource held in memory — no file, no cleanup."""
    entry = dict(carrier="ZZ", flight_numbers=["2623"], currency="USD",
                 market_price="216.93", change_total="125.00",
                 new_total="341.93", penalty="25.00")
    entry.update({k: (str(v) if isinstance(v, Decimal) else v) for k, v in scenario.items()})
    return SimulatedPriceSource(data={"orders": {"ord_test": entry}})


class ExplodingSource:
    """Any call is a bug — used to prove the cheap gates short-circuit."""
    name = "exploding"

    def get_current_price(self, *a, **k):
        raise AssertionError("get_current_price must not be called")

    def price_change(self, *a, **k):
        raise AssertionError("price_change must not be called")


def run(order, source, policy=None):
    return evaluate(order, source, policy=policy or ReshopPolicy(), now=NOW, log=False)


# ---------------------------------------------------------------------------
# the floor
# ---------------------------------------------------------------------------

def test_drop_above_floor_reshops():
    d = run(make_order(), make_source(change_total="-45.00", new_total="171.93"))
    assert d.outcome is Outcome.RESHOP
    assert d.reason is Reason.PROFITABLE_DROP
    assert d.change_total == Decimal("-45.00")
    assert d.saving == Decimal("45.00")


def test_drop_below_floor_skips():
    d = run(make_order(), make_source(change_total="-5.00", new_total="211.93"))
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.BELOW_FLOOR
    assert d.saving == Decimal("5.00")
    # Copy must report the decision, not read as a failure to find anything.
    assert "5.00 USD refund" in d.detail
    assert "not worth doing" in d.detail


def test_saving_exactly_at_floor_reshops():
    """Floor is inclusive: saving == floor clears it. Pinning the boundary."""
    d = run(make_order(), make_source(change_total="-20.00"),
            policy=ReshopPolicy(min_saving=Decimal("20.00")))
    assert d.outcome is Outcome.RESHOP
    assert d.saving == Decimal("20.00")


def test_floor_is_configurable():
    src = make_source(change_total="-45.00")
    assert run(make_order(), src, ReshopPolicy(min_saving=Decimal("50.00"))).outcome is Outcome.SKIP
    assert run(make_order(), src, ReshopPolicy(min_saving=Decimal("10.00"))).outcome is Outcome.RESHOP


# ---------------------------------------------------------------------------
# sign of change_total
# ---------------------------------------------------------------------------

def test_positive_change_total_skips():
    """The real sandbox constant: +125.00."""
    d = run(make_order(), make_source(change_total="125.00"))
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.CHANGE_TOTAL_NOT_NEGATIVE
    assert d.change_total == Decimal("125.00")


def test_zero_change_total_skips():
    """Exactly zero is not a saving. Nothing comes back, so there is nothing to win."""
    d = run(make_order(), make_source(change_total="0.00"))
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.CHANGE_TOTAL_NOT_NEGATIVE
    assert d.saving == Decimal("0.00")


# ---------------------------------------------------------------------------
# THE TRAP
# ---------------------------------------------------------------------------

def test_cheaper_market_but_positive_change_total():
    """
    The trap the whole engine exists to avoid.

    Market re-search says the fare dropped 66.93. The airline still wants +125.00
    to exchange. These are different numbers computed different ways, and only
    change_total decides. Acting on the market delta books a guaranteed loss.
    """
    d = run(make_order(), make_source(market_price="150.00", change_total="125.00"))

    # the market genuinely looks cheaper — this is not a rigged setup
    assert d.market_best == Decimal("150.00")
    assert d.market_delta == Decimal("-66.93")
    assert d.market_delta < 0

    # ...and it changes nothing
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.CHANGE_TOTAL_NOT_NEGATIVE
    assert d.change_total == Decimal("125.00")
    assert "must not be acted on" in d.detail


def test_market_delta_never_flips_a_skip_to_a_reshop():
    """Sweep the market price across a wide range; a positive change_total always skips."""
    for market in ["500.00", "216.93", "150.00", "0.01"]:
        d = run(make_order(), make_source(market_price=market, change_total="10.00"))
        assert d.outcome is Outcome.SKIP, f"market={market} wrongly reshopped"
        assert d.reason is Reason.CHANGE_TOTAL_NOT_NEGATIVE


def test_expensive_market_does_not_block_a_real_refund():
    """Converse: market looking expensive must not veto a genuinely negative change_total."""
    d = run(make_order(), make_source(market_price="900.00", change_total="-45.00"))
    assert d.market_delta > 0
    assert d.outcome is Outcome.RESHOP


# ---------------------------------------------------------------------------
# eligibility gates
# ---------------------------------------------------------------------------

def test_non_changeable_fare_skips_without_pricing():
    d = run(make_order(changeable=False), ExplodingSource())
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.FARE_NOT_CHANGEABLE


def test_inside_void_window_skips_without_pricing():
    """Inside the void window, cancel+rebook is free — paying to change is strictly worse."""
    d = run(make_order(void_window_ends_at=NOW + timedelta(hours=12)), ExplodingSource())
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.IN_VOID_WINDOW


def test_expired_void_window_does_not_block():
    d = run(make_order(void_window_ends_at=NOW - timedelta(hours=1)),
            make_source(change_total="-45.00"))
    assert d.outcome is Outcome.RESHOP


def test_inside_departure_buffer_skips_without_pricing():
    d = run(make_order(departing_at=NOW + timedelta(hours=6)), ExplodingSource())
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.TOO_CLOSE_TO_DEPARTURE


def test_departure_buffer_is_configurable():
    order = make_order(departing_at=NOW + timedelta(hours=6))
    d = run(order, make_source(change_total="-45.00"),
            ReshopPolicy(departure_buffer_hours=2))
    assert d.outcome is Outcome.RESHOP


# ---------------------------------------------------------------------------
# itinerary identity
# ---------------------------------------------------------------------------

def test_different_flight_number_is_not_identical():
    d = run(make_order(), make_source(flight_numbers=["9999"], change_total="-45.00"))
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.NO_IDENTICAL_ITINERARY


def test_same_flight_number_different_carrier_is_not_identical():
    d = run(make_order(), make_source(carrier="AA", change_total="-45.00"))
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.NO_IDENTICAL_ITINERARY


def test_decoy_on_another_flight_is_ignored_even_if_cheaper():
    """A wildly better price on a flight the passenger did not book must not win."""
    d = run(make_order(), make_source(
        change_total="-45.00",
        decoys=[{"carrier": "ZZ", "flight_numbers": ["9999"], "change_total": "-500.00"}],
    ))
    assert d.outcome is Outcome.RESHOP
    assert d.change_total == Decimal("-45.00")   # not -500.00
    assert d.candidates == 2 and d.matched == 1


def test_multi_segment_itinerary_matches_in_order():
    order = make_order(itinerary=Itinerary("ZZ", ("2623", "4410")))
    ok = run(order, make_source(flight_numbers=["2623", "4410"], change_total="-45.00"))
    assert ok.outcome is Outcome.RESHOP
    # same flights, wrong order is a different routing
    bad = run(order, make_source(flight_numbers=["4410", "2623"], change_total="-45.00"))
    assert bad.reason is Reason.NO_IDENTICAL_ITINERARY


def test_no_change_offers_at_all():
    d = run(make_order(), SimulatedPriceSource(data={"orders": {}}))
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.NO_CHANGE_OFFERS


# ---------------------------------------------------------------------------
# money handling
# ---------------------------------------------------------------------------

def test_money_is_decimal_never_float():
    d = run(make_order(), make_source(change_total="-45.00"))
    for value in (d.paid, d.change_total, d.market_best, d.floor, d.saving):
        assert isinstance(value, Decimal), f"{value!r} is {type(value)}, expected Decimal"


def test_cent_precision_survives():
    """0.1 + 0.2 territory. A float pipeline fails this; Decimal does not."""
    d = run(make_order(total=Decimal("100.10")),
            make_source(market_price="100.30", change_total="-20.20"))
    assert d.market_delta == Decimal("0.20")
    assert d.saving == Decimal("20.20")


def test_fractional_cent_below_floor_still_skips():
    d = run(make_order(), make_source(change_total="-19.99"),
            ReshopPolicy(min_saving=Decimal("20.00")))
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.BELOW_FLOOR


# ---------------------------------------------------------------------------
# order parsing + logging
# ---------------------------------------------------------------------------

def test_order_snapshot_from_real_duffel_payload():
    """Shape taken from a real sandbox order — see responses/*order_create.json."""
    order = OrderSnapshot.from_duffel({
        "id": "ord_0000B91gcGDqomV7shHSSW",
        "booking_reference": "ZC7Q7A",
        "total_amount": "216.93",
        "total_currency": "USD",
        "available_actions": ["cancel", "change", "update"],
        "void_window_ends_at": "2026-08-05T10:34:12Z",
        "owner": {"name": "Duffel Airways", "iata_code": "ZZ"},
        "conditions": {"change_before_departure": {"allowed": True, "penalty_amount": "25.00"}},
        "slices": [{
            "id": "sli_0000B91gbDV7RAkUTE0mNB",
            "origin": {"iata_code": "LHR"},
            "destination": {"iata_code": "JFK"},
            "segments": [{
                "marketing_carrier": {"iata_code": "ZZ"},
                "marketing_carrier_flight_number": "2623",
                "departing_at": "2026-10-15T10:50:00",
            }],
        }],
    })
    assert order.id == "ord_0000B91gcGDqomV7shHSSW"
    assert order.total == Decimal("216.93") and isinstance(order.total, Decimal)
    assert order.itinerary.key == (("ZZ", "2623"),)
    assert str(order.route) == "LHR-JFK"
    assert order.departure_date == "2026-10-15"
    assert order.changeable is True
    assert order.void_window_ends_at.tzinfo is not None


def test_changeable_falls_back_to_available_actions():
    payload = {
        "id": "ord_x", "total_amount": "10.00", "total_currency": "USD",
        "available_actions": ["cancel"], "conditions": {}, "slices": [],
    }
    assert OrderSnapshot.from_duffel(payload).changeable is False
    payload["available_actions"] = ["cancel", "change"]
    assert OrderSnapshot.from_duffel(payload).changeable is True


def test_available_actions_beats_lying_conditions_block():
    """
    Regression, from a real sandbox Iberia order.

    conditions.change_before_departure.allowed was true with a 40.00 penalty,
    but available_actions was ['cancel', 'update'] and the API rejected the
    change with 422 order_not_changeable. available_actions is the truth.
    """
    payload = {
        "id": "ord_0000B91jvkxerPcuhJozNw",
        "total_amount": "221.51", "total_currency": "USD",
        "available_actions": ["cancel", "update"],
        "owner": {"name": "Iberia", "iata_code": "IB"},
        "conditions": {"change_before_departure":
                       {"allowed": True, "penalty_amount": "40.00", "penalty_currency": "USD"}},
        "slices": [{"id": "sli_x", "origin": {"iata_code": "LHR"},
                    "destination": {"iata_code": "JFK"},
                    "segments": [{"marketing_carrier": {"iata_code": "IB"},
                                  "marketing_carrier_flight_number": "3167",
                                  "departing_at": "2026-10-15T10:50:00"}]}],
    }
    order = OrderSnapshot.from_duffel(payload)
    assert order.changeable is False, "conditions must not override available_actions"

    # ...and the engine must gate it out without ever calling the API
    d = run(order, ExplodingSource())
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.FARE_NOT_CHANGEABLE


def test_conditions_false_does_not_veto_when_actions_allow():
    """
    The other direction, and the reason conditions must never be a veto.

    Real sandbox Duffel Airways and British Airways orders both carry
    conditions.change_before_departure.allowed = False while exposing a 'change'
    action — and changes on them work. Vetoing on conditions would silently
    suppress every real reshop opportunity on those carriers.
    """
    payload = {
        "id": "ord_y", "total_amount": "10.00", "total_currency": "USD",
        "available_actions": ["cancel", "change", "update"],
        "conditions": {"change_before_departure": {"allowed": False}},
        "slices": [],
    }
    assert OrderSnapshot.from_duffel(payload).changeable is True


def test_decision_log_is_valid_jsonl_with_decimals_as_strings(tmp_path):
    path = tmp_path / "decisions.log"
    d = run(make_order(), make_source(change_total="-45.00"))
    log_decision(d, path=path)
    log_decision(run(make_order(), make_source(change_total="125.00")), path=path)

    lines = path.read_text().strip().split("\n")
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["outcome"] == "reshop"
    assert first["reason"] == "profitable_drop"
    assert first["change_total"] == "-45.00"      # string, not 45.0
    assert first["source"] == "simulated"
    for key in ("order_id", "paid", "floor", "market_best", "market_delta", "ts", "route"):
        assert key in first, f"{key} missing from decision log"


def test_engine_is_blind_to_source_implementation():
    """
    The engine must not branch on source type. Same scenario through a source
    with a different `name` must produce an identical decision.
    """
    class Renamed(SimulatedPriceSource):
        name = "pretend-live"

    entry = {"orders": {"ord_test": {"carrier": "ZZ", "flight_numbers": ["2623"],
                                     "currency": "USD", "market_price": "216.93",
                                     "change_total": "-45.00", "new_total": "171.93",
                                     "penalty": "0.00"}}}
    a = run(make_order(), SimulatedPriceSource(data=entry))
    b = run(make_order(), Renamed(data=entry))
    assert a.outcome == b.outcome and a.reason == b.reason
    assert a.change_total == b.change_total
    assert a.source != b.source          # only the label differs


# ---------------------------------------------------------------------------
# eligibility gating (bug 1)
# ---------------------------------------------------------------------------

import eligibility as elig
from eligibility import Eligibility, EligibilityReason, MAX_PENALTY_RATIO
from engine import Execution, fee_split


def order_payload(**over):
    """A minimal but realistically-shaped Duffel order payload."""
    base = {
        "id": "ord_e", "total_amount": "500.00", "total_currency": "USD",
        "available_actions": ["cancel", "change", "update"],
        "conditions": {
            "change_before_departure": {"allowed": True, "penalty_amount": "40.00",
                                        "penalty_currency": "USD"},
            "refund_before_departure": {"allowed": False, "penalty_amount": None,
                                        "penalty_currency": None},
        },
        "slices": [{"id": "sli_e", "origin": {"iata_code": "LHR"},
                    "destination": {"iata_code": "JFK"},
                    "segments": [{"marketing_carrier": {"iata_code": "LX"},
                                  "marketing_carrier_flight_number": "339",
                                  "departing_at": "2026-10-15T10:50:00"}]}],
    }
    base.update(over)
    return base


def test_eligible_fare_is_monitored():
    a = elig.assess(order_payload())
    assert a.state is Eligibility.MONITORING
    assert a.reason is EligibilityReason.CHANGES_ALLOWED
    assert a.should_poll is True
    assert a.penalty_ratio == Decimal("40.00") / Decimal("500.00")


def test_change_not_allowed_is_not_eligible():
    a = elig.assess(order_payload(conditions={
        "change_before_departure": {"allowed": False, "penalty_amount": None,
                                    "penalty_currency": None}}))
    assert a.state is Eligibility.NOT_ELIGIBLE
    assert a.reason is EligibilityReason.CHANGE_NOT_ALLOWED
    assert a.should_poll is False


def test_penalty_above_threshold_is_unlikely_to_save():
    """31% of the total — just over the line, so we do not poll it."""
    a = elig.assess(order_payload(conditions={
        "change_before_departure": {"allowed": True, "penalty_amount": "155.00",
                                    "penalty_currency": "USD"}}))
    assert a.state is Eligibility.UNLIKELY_TO_SAVE
    assert a.reason is EligibilityReason.PENALTY_TOO_HIGH
    assert a.should_poll is False


def test_penalty_exactly_at_threshold_still_monitors():
    """Boundary is inclusive: 30% of 500 is 150 and must not be excluded."""
    a = elig.assess(order_payload(conditions={
        "change_before_departure": {"allowed": True, "penalty_amount": "150.00",
                                    "penalty_currency": "USD"}}))
    assert a.penalty_ratio == MAX_PENALTY_RATIO
    assert a.state is Eligibility.MONITORING


def test_threshold_is_tunable():
    payload = order_payload(conditions={
        "change_before_departure": {"allowed": True, "penalty_amount": "100.00",
                                    "penalty_currency": "USD"}})
    assert elig.assess(payload, Decimal("0.30")).state is Eligibility.MONITORING
    assert elig.assess(payload, Decimal("0.10")).state is Eligibility.UNLIKELY_TO_SAVE


def test_swiss_ybvi8r_currency_mismatch_is_not_monitored():
    """
    Regression for the reported bug, using the real order's values.

    GBP 300 against a USD 573.33 total. Comparing the raw numbers gives 52%,
    and converting gives roughly 66% — but we must not do either silently.
    """
    a = elig.assess(order_payload(
        total_amount="573.33", total_currency="USD",
        conditions={"change_before_departure": {"allowed": True, "penalty_amount": "300",
                                                "penalty_currency": "GBP"},
                    "refund_before_departure": {"allowed": False}}))
    assert a.state is Eligibility.NOT_ELIGIBLE
    assert a.reason is EligibilityReason.PENALTY_CURRENCY_MISMATCH
    assert a.should_poll is False
    assert a.needs_attention is True
    assert a.penalty_ratio is None, "must not produce a ratio across currencies"
    assert "GBP" in a.detail and "USD" in a.detail


def test_null_conditions_are_not_eligible_and_flagged():
    """Real TAP orders return change_before_departure: null despite the docs."""
    a = elig.assess(order_payload(conditions={"change_before_departure": None,
                                              "refund_before_departure": None}))
    assert a.state is Eligibility.NOT_ELIGIBLE
    assert a.reason is EligibilityReason.CONDITIONS_MISSING
    assert a.needs_attention is True


def test_missing_conditions_key_is_not_eligible():
    a = elig.assess(order_payload(conditions=None))
    assert a.state is Eligibility.NOT_ELIGIBLE
    assert a.needs_attention is True


def test_allowed_with_null_penalty_is_not_eligible():
    """Unknown fee is not assumed to be zero in the customer's favour."""
    a = elig.assess(order_payload(conditions={
        "change_before_departure": {"allowed": True, "penalty_amount": None,
                                    "penalty_currency": None}}))
    assert a.state is Eligibility.NOT_ELIGIBLE
    assert a.reason is EligibilityReason.PENALTY_UNKNOWN
    assert a.needs_attention is True


def test_no_change_action_beats_permissive_conditions():
    """FINDINGS.md §8 — available_actions is authoritative."""
    a = elig.assess(order_payload(available_actions=["cancel", "update"]))
    assert a.state is Eligibility.NOT_ELIGIBLE
    assert a.reason is EligibilityReason.NO_CHANGE_ACTION


def test_every_reason_has_customer_copy():
    for reason in EligibilityReason:
        assert elig.CUSTOMER_COPY[reason].strip(), f"{reason} has no customer copy"


def test_engine_skips_ineligible_without_pricing():
    order = make_order(eligibility=elig.assess(order_payload(
        total_amount="573.33",
        conditions={"change_before_departure": {"allowed": True, "penalty_amount": "300",
                                                "penalty_currency": "GBP"}})))
    d = run(order, ExplodingSource())
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.ELIGIBILITY_UNKNOWN


def test_engine_skips_high_penalty_without_pricing():
    order = make_order(eligibility=elig.assess(order_payload(conditions={
        "change_before_departure": {"allowed": True, "penalty_amount": "400.00",
                                    "penalty_currency": "USD"}})))
    d = run(order, ExplodingSource())
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.PENALTY_TOO_HIGH


def test_eligible_order_still_polls():
    order = make_order(eligibility=elig.assess(order_payload()))
    d = run(order, make_source(change_total="-45.00"))
    assert d.outcome is Outcome.RESHOP


# ---------------------------------------------------------------------------
# connecting itineraries (bug 3)
# ---------------------------------------------------------------------------

LX_CONNECTION = Itinerary("LX", ("339", "16"))   # LHR→ZRH→JFK, as booked on YBVI8R


def test_two_segment_order_rejects_one_segment_candidate():
    """
    The case from YBVI8R.

    A direct LX339 LHR→JFK is the same city pair and the same carrier, but it is
    not the itinerary the passenger booked. It must never match.
    """
    order = make_order(itinerary=LX_CONNECTION)
    d = run(order, make_source(carrier="LX", flight_numbers=["339"],
                               change_total="-200.00"))
    assert d.outcome is Outcome.SKIP
    assert d.reason is Reason.NO_IDENTICAL_ITINERARY


def test_one_segment_order_rejects_two_segment_candidate():
    """The converse: a connection is not a substitute for a direct."""
    order = make_order(itinerary=Itinerary("LX", ("339",)))
    d = run(order, make_source(carrier="LX", flight_numbers=["339", "16"],
                               change_total="-200.00"))
    assert d.reason is Reason.NO_IDENTICAL_ITINERARY


def test_connection_matches_only_full_signature():
    order = make_order(itinerary=LX_CONNECTION)
    d = run(order, make_source(carrier="LX", flight_numbers=["339", "16"],
                               change_total="-45.00"))
    assert d.outcome is Outcome.RESHOP
    assert d.itinerary == "LX339 + LX16"


def test_connection_rejects_reordered_segments():
    order = make_order(itinerary=LX_CONNECTION)
    d = run(order, make_source(carrier="LX", flight_numbers=["16", "339"],
                               change_total="-45.00"))
    assert d.reason is Reason.NO_IDENTICAL_ITINERARY


def test_connection_rejects_wrong_middle_segment():
    """Same first and last flight, different connecting flight."""
    order = make_order(itinerary=Itinerary("LX", ("339", "16", "22")))
    d = run(order, make_source(carrier="LX", flight_numbers=["339", "99", "22"],
                               change_total="-45.00"))
    assert d.reason is Reason.NO_IDENTICAL_ITINERARY


def test_codeshare_same_numbers_different_carrier_does_not_match():
    """Per-segment carriers matter: LX339+LX16 is not LX339+UA16."""
    order = make_order(itinerary=Itinerary("LX", ("339", "16"), ("LX", "LX")))
    d = run(order, make_source(carrier="LX", flight_numbers=["339", "16"],
                               carriers=["LX", "UA"], change_total="-45.00"))
    assert d.reason is Reason.NO_IDENTICAL_ITINERARY


def test_itinerary_key_is_ordered_pairs():
    assert LX_CONNECTION.key == (("LX", "339"), ("LX", "16"))
    assert not LX_CONNECTION.matches(Itinerary("LX", ("339",)))
    assert LX_CONNECTION.matches(Itinerary("LX", ("339", "16")))


def test_multi_segment_snapshot_captures_every_segment():
    order = OrderSnapshot.from_duffel({
        "id": "ord_lx", "total_amount": "573.33", "total_currency": "USD",
        "available_actions": ["cancel", "change"],
        "conditions": {"change_before_departure": {"allowed": True,
                                                   "penalty_amount": "300",
                                                   "penalty_currency": "GBP"}},
        "slices": [{"id": "sli_lx", "origin": {"iata_code": "LHR"},
                    "destination": {"iata_code": "JFK"},
                    "segments": [
                        {"marketing_carrier": {"iata_code": "LX"},
                         "marketing_carrier_flight_number": "339",
                         "departing_at": "2026-10-15T10:50:00"},
                        {"marketing_carrier": {"iata_code": "LX"},
                         "marketing_carrier_flight_number": "16",
                         "departing_at": "2026-10-15T14:20:00"}]}],
    })
    assert order.itinerary.key == (("LX", "339"), ("LX", "16"))
    assert order.eligibility.reason is EligibilityReason.PENALTY_CURRENCY_MISMATCH


# ---------------------------------------------------------------------------
# decision vs execution, and the fee split (bug 2)
# ---------------------------------------------------------------------------

def test_simulated_reshop_is_a_decision_with_execution_blocked():
    """
    The reported confusion: a forced negative change total looked like the
    engine did nothing. It decided to exchange; execution was blocked.
    """
    d = run(make_order(), make_source(change_total="-45.00"))
    assert d.outcome is Outcome.RESHOP           # the engine DID decide
    assert d.reason is Reason.PROFITABLE_DROP
    assert d.execution is Execution.BLOCKED_SIMULATED
    assert d.blocked is True and d.executed is False
    assert "simulated" in d.execution_detail
    assert "must never trigger a real Duffel call" in d.execution_detail


def test_live_reshop_awaits_confirmation_rather_than_blocking():
    class Liveish(SimulatedPriceSource):
        name = "duffel"
    src = Liveish(data={"orders": {"ord_test": {
        "carrier": "ZZ", "flight_numbers": ["2623"], "currency": "USD",
        "market_price": "216.93", "change_total": "-45.00"}}})
    d = run(make_order(), src)
    assert d.outcome is Outcome.RESHOP
    assert d.execution is Execution.AWAITING_CONFIRMATION
    assert d.blocked is False


def test_skip_has_no_execution_status():
    d = run(make_order(), make_source(change_total="125.00"))
    assert d.outcome is Outcome.SKIP
    assert d.execution is Execution.NOT_APPLICABLE
    assert d.execution_detail == ""


def test_fee_breakdown_on_reshop():
    d = run(make_order(), make_source(change_total="-45.00"))
    assert d.recovered == Decimal("45.00")
    assert d.service_fee == Decimal("11.25")        # 25%
    assert d.net_to_customer == Decimal("33.75")
    assert d.service_fee + d.net_to_customer == d.recovered


def test_fee_breakdown_absent_on_skip():
    d = run(make_order(), make_source(change_total="125.00"))
    assert d.recovered is None and d.service_fee is None and d.net_to_customer is None


def test_fee_split_always_reconciles_including_odd_cents():
    for amount in ["45.00", "0.01", "33.33", "19.99", "100.10", "7.77"]:
        recovered, fee, net = fee_split(Decimal(amount))
        assert fee + net == recovered, f"{amount} does not reconcile"
        assert all(isinstance(v, Decimal) for v in (recovered, fee, net))


def test_fee_breakdown_appears_in_decision_copy():
    d = run(make_order(), make_source(change_total="-45.00"))
    assert "Recovered 45.00" in d.detail
    assert "our fee 11.25" in d.detail
    assert "33.75 net to customer" in d.detail


def test_execution_logged_as_separate_line(tmp_path):
    """A blocked exchange must be its own log event, not folded into the decision."""
    path = tmp_path / "decisions.log"
    d = evaluate(make_order(), make_source(change_total="-45.00"),
                 policy=ReshopPolicy(), now=NOW, log=False)
    log_decision(d, path=path)
    from engine import log_execution
    log_execution(d, path=path)

    rows = [json.loads(l) for l in path.read_text().strip().split("\n")]
    assert [r["kind"] for r in rows] == ["decision", "execution"]
    assert rows[0]["outcome"] == "reshop"
    assert rows[1]["execution"] == "blocked_simulated"
    assert rows[1]["decision_outcome"] == "reshop"
    assert rows[1]["net_to_customer"] == "33.75"
    assert rows[1]["service_fee"] == "11.25"


# ---------------------------------------------------------------------------
# copy (bug 4)
# ---------------------------------------------------------------------------

def test_declined_copy_reports_the_quote_not_a_miss():
    d = run(make_order(), make_source(change_total="125.00"))
    assert "Airline quoted 125.00 USD" in d.detail
    assert "a charge, not a refund" in d.detail
    assert "Checked and declined" in d.detail
    assert "nothing comes back" not in d.detail


def test_market_vs_exchange_note_is_retained():
    """This distinction is the one people get wrong — it must survive rewording."""
    d = run(make_order(), make_source(market_price="150.00", change_total="125.00"))
    assert "not the exchange price and must not be acted on" in d.detail
    assert "66.93" in d.detail
