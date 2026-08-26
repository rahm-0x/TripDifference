"""
Money-path regression tests: book()'s gates, the Stripe authorize/capture/
cancel sequence, policy enforcement, delivery-type classification, and the
execution-bookkeeping fixes from item 1.

engine.py's decision math is covered by test_engine.py and untouched here.
Duffel and Stripe are mocked so these run offline and fast — the live
end-to-end runs already done against sandbox are the integration proof,
this is the regression net underneath. The database is real (the same
Postgres this app always uses): there is no way to derive a live column
list from a mock, and the persistence tests below exist specifically to
catch drift between the schema and db.py's allowlists.

    .venv/bin/python -m pytest test_money_path.py -v

Every test creates and tears down its own account.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv(".env")

import app as app_module
import billing
import db
import policy
from duffel_http import DuffelError

CSRF = "test-csrf-token"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def acct():
    """A real account + user, deleted afterward. accounts.commission_rate
    stays at its schema default (0.25) unless a test overrides it."""
    email = f"test-{uuid.uuid4().hex[:12]}@example.com"
    user = db.create_account(email, password_hash="x")
    yield user
    db.q("DELETE FROM accounts WHERE id = %s", (user["account_id"],))


def link_fake_card(account_id):
    """Puts a card on file without touching Stripe at all — book()'s card
    gate only ever checks account_card()'s DB-backed result."""
    db.account_set_stripe_customer(account_id, "cus_test_fake")
    db.account_save_payment_method(account_id, stripe_payment_method_id="pm_test_fake",
                                   brand="visa", last4="4242", exp_month=12, exp_year=2030)


@pytest.fixture
def client():
    with app_module.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["_csrf"] = CSRF
        yield c


@pytest.fixture
def logged_in(acct, client):
    token = db.start_session(acct["id"])
    with client.session_transaction() as sess:
        sess["sid"] = token
        sess["_csrf"] = CSRF
    return client, acct


@pytest.fixture
def logged_in_carded(logged_in):
    client, account = logged_in
    link_fake_card(account["account_id"])
    return client, account


def insert_policy_rule(account_id, rule_type, value, enforcement, scope="{}"):
    return db.q("""INSERT INTO policy_rules (account_id, rule_type, scope, value, enforcement)
                   VALUES (%s, %s, %s::jsonb, %s::jsonb, %s) RETURNING id""",
               (account_id, rule_type, scope, value, enforcement), fetch="one")


def make_real_order(account_id, order_id=None, paid="219.00", currency="USD",
                    carrier="Test Airways", last_decision=None):
    """Bypasses book()/Duffel entirely — for tests exercising execute()'s
    own logic, not the purchase path."""
    order_id = order_id or f"ord_test_{uuid.uuid4().hex[:16]}"
    return db.upsert_order({
        "order_id": order_id, "source": "td_rebook", "fare_type": "cash",
        "paid": paid, "currency": currency, "carrier": carrier,
        "route": "LHR-JFK", "itinerary": "ZZ123", "monitoring": False,
        "raw": {"id": order_id}, "last_decision": last_decision,
    }, account_id)


# ---------------------------------------------------------------------------
# fake Duffel payloads
# ---------------------------------------------------------------------------

def fake_offer(offer_id=None, amount="219.00", currency="USD", cabin="economy"):
    offer_id = offer_id or f"off_test_{uuid.uuid4().hex[:16]}"
    return {
        "id": offer_id, "total_amount": amount, "total_currency": currency,
        "passengers": [{"id": "pas_test1"}],
        "slices": [{
            "id": "sli_test1",
            "origin": {"iata_code": "LHR"}, "destination": {"iata_code": "JFK"},
            "segments": [{
                "marketing_carrier": {"iata_code": "ZZ"},
                "marketing_carrier_flight_number": "123",
                "departing_at": "2026-12-01T10:00:00Z",
                "arriving_at": "2026-12-01T18:00:00Z",
                "passengers": [{"cabin_class": cabin}],
            }],
            "fare_brand_name": "Basic",
        }],
    }


def fake_order(order_id=None, amount="219.00", currency="USD",
              refundable=True, change_allowed=False, carrier_name="Test Airways",
              carrier_iata="T1", fare_brand="Basic"):
    # T1, not a real IATA code — book()'s carrier_capability_record() write
    # is real and unmocked (only duffel_http.request is patched here), so a
    # fixture default that collided with a seeded carrier (ZZ/BA/AA/TP/IB)
    # would let every test in this file quietly pollute that carrier's real
    # observation counts on every run.
    order_id = order_id or f"ord_test_{uuid.uuid4().hex[:16]}"
    return {
        "id": order_id, "booking_reference": "TEST123",
        "total_amount": amount, "total_currency": currency,
        "slices": [{
            "id": "sli_test1",
            "origin": {"iata_code": "LHR"}, "destination": {"iata_code": "JFK"},
            "segments": [{
                "marketing_carrier": {"iata_code": "ZZ"},
                "marketing_carrier_flight_number": "123",
                "departing_at": "2026-12-01T10:00:00Z",
                "arriving_at": "2026-12-01T18:00:00Z",
                "passengers": [{"cabin_class": "economy"}],
            }],
            "fare_brand_name": fare_brand,
        }],
        "conditions": {
            "change_before_departure": {"allowed": change_allowed, "penalty_amount": "50.00",
                                        "penalty_currency": currency},
            "refund_before_departure": {"allowed": refundable, "penalty_amount": "0.00",
                                        "penalty_currency": currency},
        },
        "available_actions": ["cancel", "change"] if change_allowed else ["cancel"],
        "owner": {"name": carrier_name, "iata_code": carrier_iata},
        "void_window_ends_at": None,
    }


def duffel_side_effect(offer=None, order=None, order_error=None):
    """One offer-fetch, one order-create — the exact sequence book() makes.
    A callable rather than a fixed list, since intermediate steps
    (/book/passenger, /book/payment) aren't exercised here — book() reads
    everything fresh from the form and re-fetches the offer itself."""
    offer = offer or fake_offer()

    def _mock(method, path, body=None, params=None, label=None):
        if method == "GET" and path.startswith("/air/offers/"):
            return offer
        if method == "POST" and path == "/air/orders":
            if order_error:
                raise order_error
            return order or fake_order(order_id=None, amount=offer["total_amount"],
                                       currency=offer["total_currency"])
        raise AssertionError(f"unexpected duffel_http.request({method!r}, {path!r})")
    return _mock


BOOK_FORM = {"title": "mr", "given_name": "Amelia", "family_name": "Earhart",
            "born_on": "1985-04-16", "gender": "f", "email": "amelia@example.com",
            "phone_number": "+442080160509"}


def book_form(offer_id, **overrides):
    form = {"offer_id": offer_id, "_csrf": CSRF, **BOOK_FORM}
    form.update(overrides)
    return form


# ---------------------------------------------------------------------------
# persistence round-trip — schema-derived, not hand-written
# ---------------------------------------------------------------------------

def _live_columns(table):
    rows = db.q("""SELECT column_name, data_type FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s
                   ORDER BY ordinal_position""", (table,), fetch="all")
    return {r["column_name"]: r["data_type"] for r in rows}


def test_order_cols_covers_every_writable_column():
    """The bug item 1 exists because of: a column present in the live
    schema but absent from _ORDER_COLS is silently dropped by
    upsert_order() before it ever reaches SQL. This asserts set equality
    against the live schema, not a hand-written expectation — a future
    migration that adds an orders column without touching _ORDER_COLS
    fails this test immediately."""
    live = set(_live_columns("orders")) - {"order_id", "account_id", "created_at", "updated_at"}
    assert live == set(db._ORDER_COLS), (
        f"orders columns not in _ORDER_COLS: {live - set(db._ORDER_COLS)} | "
        f"_ORDER_COLS entries with no matching column: {set(db._ORDER_COLS) - live}")


def test_traveler_fields_covers_every_writable_column():
    """Same check for traveler_save()'s combined field list — Item 3 wired
    default_cost_center_id into the Travelers form, so it now belongs in
    the allowlist same as everything else. Anything missing should fail
    this test."""
    live = set(_live_columns("travelers")) - {"id", "account_id", "created_at", "updated_at"}
    combined = set(db.TRAVELER_FIELDS) | set(db.TRAVELER_TEXT_PROFILE_FIELDS) | \
        {"clear_plus", "loyalty_programs", "default_cost_center_id"}
    missing = live - combined
    assert missing == set(), (
        f"unexpected gap between travelers schema and TRAVELER_FIELDS/"
        f"TRAVELER_TEXT_PROFILE_FIELDS: {missing}")


def test_upsert_order_round_trips_every_writable_value(acct):
    """Structural coverage (above) catches a column missing from the
    allowlist entirely. This catches the other failure mode: present in
    the allowlist, but miscoerced (e.g. a jsonb column never added to
    _JSON, a money column never added to _MONEY) so it round-trips wrong."""
    traveler = db.traveler_save({"given_name": "Rt", "family_name": "Trip"}, acct["account_id"])
    cost_center = db.q("""INSERT INTO cost_centers (account_id, code, name)
                          VALUES (%s, 'ENG', 'Engineering') RETURNING id""",
                       (acct["account_id"],), fetch="one")

    order_id = f"ord_test_{uuid.uuid4().hex[:16]}"
    values = {
        "booking_reference": "ABC123", "route": "LHR-JFK", "itinerary": "ZZ123 + ZZ456",
        "carrier": "Test Airways", "departure_date": (date.today() + timedelta(days=60)).isoformat(),
        "paid": "219.26", "original_paid": "250.00", "refunded": "30.74",
        "currency": "USD", "monitoring": True, "executed": "test note",
        "raw": {"id": order_id, "test": True}, "last_decision": {"outcome": "skip"},
        "sim_scenario": {"market_price": "200.00"}, "simulated": True,
        "sim_paid": "180.00", "sim_refunded": "39.26",
        "offer_id": "off_test_xyz", "source": "manual", "fare_type": "points",
        "traveler_id": traveler["id"],
        "seg_origin": "LHR", "seg_destination": "JFK", "seg_flight_number": "123",
        "seg_cabin": "business",
        "cost_center_id": cost_center["id"],
        "refundable": True, "fare_conditions": {"refund_before_departure": {"allowed": True}},
        "stripe_payment_intent_id": "pi_test_abc",
        "payment_capture_failed_at": "2026-08-25T12:00:00+00:00",
        "payment_capture_error": "test capture error",
    }
    assert set(values) == set(db._ORDER_COLS), "test fixture drifted from _ORDER_COLS"

    db.upsert_order({"order_id": order_id, **values}, acct["account_id"])
    row = db.find_order(order_id, acct["account_id"])

    for col in db._ORDER_COLS:
        if col in db._MONEY:
            assert row[col] == values[col], col
        elif col in db._JSON:
            assert row[col] == values[col], col
        elif col == "departure_date":
            assert row[col] == values[col], col
        elif col in ("traveler_id", "cost_center_id"):
            assert str(row[col]) == str(values[col]), col
        elif col == "payment_capture_failed_at":
            # timestamptz round-trips as a datetime, not the ISO string
            # that was written — presence is what this test cares about.
            assert row[col] is not None, col
        else:
            assert row[col] == values[col], col

    db.q("DELETE FROM orders WHERE order_id = %s", (order_id,))
    db.q("DELETE FROM cost_centers WHERE id = %s", (cost_center["id"],))


# ---------------------------------------------------------------------------
# cost centres (item 3)
# ---------------------------------------------------------------------------

def test_cost_center_create_list_update(acct):
    account_id = acct["account_id"]
    created = db.cost_center_create(account_id, code="ENG", name="Engineering",
                                    budget_amount="5000.00", budget_period="monthly")
    assert created["code"] == "ENG"
    assert created["active"] is True

    listed = db.cost_centers_for_account(account_id)
    assert [c["id"] for c in listed] == [created["id"]]

    updated = db.cost_center_update(created["id"], account_id, code="ENG", name="Engineering & IT",
                                    budget_amount="6000.00", budget_period="quarterly", active=False)
    assert updated["name"] == "Engineering & IT"
    assert updated["budget_period"] == "quarterly"
    assert updated["active"] is False

    # active_only excludes it once deactivated, same query the booking-flow
    # picker uses — a stale/retired cost centre should stop showing up there.
    assert db.cost_centers_for_account(account_id, active_only=True) == []

    db.q("DELETE FROM cost_centers WHERE id = %s", (created["id"],))


def test_cost_center_update_scoped_to_account(acct):
    """cost_center_update() takes account_id for the same reason
    audit_rows()/invoice_lines_for() do — a bare id from a URL/form must not
    let one account edit another's row."""
    other = db.create_account(f"other-{uuid.uuid4().hex[:12]}@example.com", password_hash="x")
    theirs = db.cost_center_create(other["account_id"], code="ENG", name="Engineering")
    try:
        assert db.cost_center(theirs["id"], acct["account_id"]) is None
        result = db.cost_center_update(theirs["id"], acct["account_id"], code="HACKED",
                                       name="Hacked", budget_amount=None, budget_period=None,
                                       active=True)
        assert result is None
        untouched = db.cost_center(theirs["id"], other["account_id"])
        assert untouched["code"] == "ENG"
    finally:
        db.q("DELETE FROM cost_centers WHERE id = %s", (theirs["id"],))
        db.q("DELETE FROM accounts WHERE id = %s", (other["account_id"],))


def test_traveler_save_writes_default_cost_center(acct):
    """default_cost_center_id was schema-only until Item 3 wired a form
    field for it — this is the round-trip proof, same shape as
    test_upsert_order_round_trips_every_writable_value's per-column checks."""
    account_id = acct["account_id"]
    cc = db.cost_center_create(account_id, code="ENG", name="Engineering")
    try:
        traveler = db.traveler_save(
            {"given_name": "Cost", "family_name": "Center", "default_cost_center_id": cc["id"]},
            account_id)
        assert str(traveler["default_cost_center_id"]) == str(cc["id"])

        fetched = db.traveler(traveler["id"], account_id)
        assert fetched["default_cost_center_id"] == str(cc["id"])  # _traveler() stringifies for JSON
    finally:
        db.q("DELETE FROM cost_centers WHERE id = %s", (cc["id"],))


def test_traveler_new_ignores_cost_center_from_another_account(logged_in):
    """Same account-scoping shape as book()'s cost_center_id handling
    (test_book_ignores_cost_center_from_another_account) — _traveler_form()
    must drop a default_cost_center_id that doesn't belong to the caller's
    own account rather than trust a bare id from the form."""
    client, account = logged_in
    other = db.create_account(f"other-{uuid.uuid4().hex[:12]}@example.com", password_hash="x")
    theirs = db.cost_center_create(other["account_id"], code="ENG", name="Engineering")
    try:
        resp = client.post("/travelers/new", data={
            "_csrf": CSRF, "given_name": "Cost", "family_name": "Center",
            "default_cost_center_id": str(theirs["id"]),
        })
        assert resp.status_code == 302
        travelers = db.travelers(account["account_id"])
        assert len(travelers) == 1
        assert travelers[0]["default_cost_center_id"] == ""
    finally:
        db.q("DELETE FROM travelers WHERE account_id = %s", (account["account_id"],))
        db.q("DELETE FROM cost_centers WHERE id = %s", (theirs["id"],))
        db.q("DELETE FROM accounts WHERE id = %s", (other["account_id"],))


def test_book_attaches_cost_center_from_same_account(logged_in_carded):
    client, account = logged_in_carded
    cc = db.cost_center_create(account["account_id"], code="ENG", name="Engineering")
    offer = fake_offer()
    order = fake_order(amount=offer["total_amount"], currency=offer["total_currency"])
    try:
        with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer, order=order)), \
             patch("billing.authorize_fare", return_value=MagicMock(id="pi_test_cc1")), \
             patch("billing.capture_authorization"):
            resp = client.post("/book", data=book_form(offer["id"], cost_center_id=str(cc["id"])))
        assert resp.status_code == 302
        row = db.find_order(order["id"], account["account_id"])
        assert str(row["cost_center_id"]) == str(cc["id"])
    finally:
        db.q("DELETE FROM cost_centers WHERE id = %s", (cc["id"],))


def test_book_ignores_cost_center_from_another_account(logged_in_carded):
    """Same shape as the account-scoping bugs fixed elsewhere: a
    cost_center_id is a bare id from a form field, and book() must not
    trust it just because it looks well-formed. One belonging to a
    different account must be dropped, not attached."""
    client, account = logged_in_carded
    other = db.create_account(f"other-{uuid.uuid4().hex[:12]}@example.com", password_hash="x")
    theirs = db.cost_center_create(other["account_id"], code="ENG", name="Engineering")
    offer = fake_offer()
    order = fake_order(amount=offer["total_amount"], currency=offer["total_currency"])
    try:
        with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer, order=order)), \
             patch("billing.authorize_fare", return_value=MagicMock(id="pi_test_cc2")), \
             patch("billing.capture_authorization"):
            resp = client.post("/book", data=book_form(offer["id"], cost_center_id=str(theirs["id"])))
        assert resp.status_code == 302
        row = db.find_order(order["id"], account["account_id"])
        assert row["cost_center_id"] is None
    finally:
        db.q("DELETE FROM cost_centers WHERE id = %s", (theirs["id"],))
        db.q("DELETE FROM accounts WHERE id = %s", (other["account_id"],))


# ---------------------------------------------------------------------------
# the card gate
# ---------------------------------------------------------------------------

def test_book_refuses_with_no_card(logged_in):
    client, account = logged_in
    offer = fake_offer()
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer)), \
         patch("billing.authorize_fare") as mock_authorize:
        resp = client.post("/book", data=book_form(offer["id"]))
    assert resp.status_code == 402
    mock_authorize.assert_not_called()
    assert db.order_for_offer(account["account_id"], offer["id"]) is None


def test_book_proceeds_with_card(logged_in_carded):
    client, account = logged_in_carded
    offer = fake_offer()
    order = fake_order(amount=offer["total_amount"], currency=offer["total_currency"])
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer, order=order)), \
         patch("billing.authorize_fare", return_value=MagicMock(id="pi_test_1")), \
         patch("billing.capture_authorization") as mock_capture:
        resp = client.post("/book", data=book_form(offer["id"]), follow_redirects=False)
    assert resp.status_code == 302
    mock_capture.assert_called_once_with("pi_test_1")
    row = db.find_order(order["id"], account["account_id"])
    assert row is not None
    assert row["stripe_payment_intent_id"] == "pi_test_1"


# ---------------------------------------------------------------------------
# carrier change-capability observations (eligibility bug fix)
# ---------------------------------------------------------------------------

def _clear_carrier(carrier_iata):
    db.q("DELETE FROM carrier_change_capability WHERE carrier_iata = %s", (carrier_iata,))
    db.q("DELETE FROM carrier_change_observations WHERE carrier_iata = %s", (carrier_iata,))


def test_carrier_capability_record_and_read():
    _clear_carrier("QQ")
    try:
        assert db.carrier_capability_for("QQ") is None
        db.carrier_capability_record("QQ", "Test Air", change_allowed=True, fare_brand="Flex")
        assert db.carrier_capability_for("QQ") == {"confirmed": 1, "denied": 0, "is_synthetic": False}
        db.carrier_capability_record("QQ", "Test Air", change_allowed=False, fare_brand="Basic")
        db.carrier_capability_record("QQ", "Test Air", change_allowed=False, fare_brand="Basic")
        assert db.carrier_capability_for("QQ") == {"confirmed": 1, "denied": 2, "is_synthetic": False}
        assert db.carrier_capabilities_for(["QQ", "NOPE"]) == \
            {"QQ": {"confirmed": 1, "denied": 2, "is_synthetic": False}}

        # The raw log — not read by anything yet, but the fare_brand has to
        # actually be there for the "does it cluster by fare brand" question
        # to be answerable later.
        rows = db.q("""SELECT fare_brand, change_allowed FROM carrier_change_observations
                       WHERE carrier_iata = 'QQ' ORDER BY id""", fetch="all")
        assert [dict(r) for r in rows] == [
            {"fare_brand": "Flex", "change_allowed": True},
            {"fare_brand": "Basic", "change_allowed": False},
            {"fare_brand": "Basic", "change_allowed": False},
        ]
    finally:
        _clear_carrier("QQ")


def test_zz_is_seeded_as_synthetic():
    """Duffel Airways is Duffel's own sandbox carrier — real in the sense
    that sandbox orders really do come back this way, but not evidence
    about how a real airline behaves. Must stay readable (sandbox ranking
    needs it) but flagged so a future real-carrier-only read can skip it."""
    assert db.carrier_capability_for("ZZ")["is_synthetic"] is True


def test_book_records_a_carrier_capability_observation(logged_in_carded):
    """Every real order is a free observation — book() must record one
    whenever Duffel actually returned available_actions, using this exact
    order's carrier and its real change_allowed fact, not a guess."""
    client, account = logged_in_carded
    offer = fake_offer()
    order = fake_order(amount=offer["total_amount"], currency=offer["total_currency"],
                       change_allowed=False, carrier_iata="QQ", fare_brand="Basic")
    _clear_carrier("QQ")
    try:
        with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer, order=order)), \
             patch("billing.authorize_fare", return_value=MagicMock(id="pi_test_carrier1")), \
             patch("billing.capture_authorization"):
            resp = client.post("/book", data=book_form(offer["id"]), follow_redirects=False)
        assert resp.status_code == 302
        assert db.carrier_capability_for("QQ") == {"confirmed": 0, "denied": 1, "is_synthetic": False}
        logged = db.q("""SELECT fare_brand, change_allowed, order_id
                         FROM carrier_change_observations WHERE carrier_iata = 'QQ'""", fetch="one")
        assert logged["fare_brand"] == "Basic"
        assert logged["change_allowed"] is False
        assert logged["order_id"] == order["id"]
    finally:
        _clear_carrier("QQ")


def test_rank_price_discounts_likely_monitoring_not_ineligible():
    """The sort weighting Part B asked to be stated plainly: a
    likely-monitorable fare ranks as if LIKELY_MONITORING_RANK_DISCOUNT
    cheaper — enough to beat a similarly-priced ineligible fare, not enough
    to beat one that's materially cheaper."""
    discount = app_module.LIKELY_MONITORING_RANK_DISCOUNT
    likely = {"amount": "230.00", "eligibility_state": "likely_monitoring",
             "eligibility_reason": "changes_likely_allowed"}
    ineligible_close = {"amount": "220.00", "eligibility_state": "not_eligible",
                        "eligibility_reason": "carrier_never_confirmed_change"}
    ineligible_far = {"amount": "150.00", "eligibility_state": "not_eligible",
                      "eligibility_reason": "change_not_allowed"}

    assert app_module._offer_rank_price(likely) == Decimal("230.00") * (1 - discount)
    # A materially cheaper ineligible fare beats it...
    assert app_module._offer_rank_price(ineligible_far) < app_module._offer_rank_price(likely)
    # ...but a merely-somewhat-cheaper one, within the discount, does not.
    assert app_module._offer_rank_price(likely) < app_module._offer_rank_price(ineligible_close)


def test_rank_price_never_discounts_a_confirmed_monitoring_state():
    """Kept for completeness — an offer can never actually reach this state
    (FINDINGS.md §8: available_actions doesn't exist pre-purchase), but if
    it ever did, a confirmed claim gets no discount at all, same as today."""
    confirmed = {"amount": "230.00", "eligibility_state": "monitoring",
                "eligibility_reason": "changes_allowed"}
    assert app_module._offer_rank_price(confirmed) == Decimal("230.00")


def test_rank_price_gives_no_boost_to_a_single_carrier_denial():
    """The graduated-penalty half of Part B's follow-up: a carrier with one
    real denial and zero confirms stays LIKELY_MONITORING (still shown,
    still counts as monitorable) but gets none of the usual optimism
    discount — priced at face value, same as an outright ineligible fare,
    so an ordinary likely-monitorable alternative at the same price wins."""
    single_denial = {"amount": "230.00", "eligibility_state": "likely_monitoring",
                     "eligibility_reason": "carrier_single_denial"}
    ordinary_likely = {"amount": "230.00", "eligibility_state": "likely_monitoring",
                       "eligibility_reason": "changes_likely_allowed"}

    assert app_module._offer_rank_price(single_denial) == Decimal("230.00")
    assert app_module._offer_rank_price(ordinary_likely) < app_module._offer_rank_price(single_denial)


# ---------------------------------------------------------------------------
# the Stripe sequence
# ---------------------------------------------------------------------------

def test_duffel_failure_cancels_authorization_not_captures(logged_in_carded):
    client, account = logged_in_carded
    offer = fake_offer()
    with patch("duffel_http.request",
              side_effect=duffel_side_effect(offer=offer, order_error=DuffelError(422, []))), \
         patch("billing.authorize_fare", return_value=MagicMock(id="pi_test_2")), \
         patch("billing.cancel_authorization") as mock_cancel, \
         patch("billing.capture_authorization") as mock_capture:
        resp = client.post("/book", data=book_form(offer["id"]))
    mock_cancel.assert_called_once_with("pi_test_2")
    mock_capture.assert_not_called()
    assert db.order_for_offer(account["account_id"], offer["id"]) is None


def test_duffel_success_captures_exactly_once(logged_in_carded):
    client, account = logged_in_carded
    offer = fake_offer()
    order = fake_order(amount=offer["total_amount"], currency=offer["total_currency"])
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer, order=order)), \
         patch("billing.authorize_fare", return_value=MagicMock(id="pi_test_3")), \
         patch("billing.capture_authorization") as mock_capture, \
         patch("billing.cancel_authorization") as mock_cancel:
        client.post("/book", data=book_form(offer["id"]))
    mock_capture.assert_called_once_with("pi_test_3")
    mock_cancel.assert_not_called()


def test_retried_book_reuses_idempotency_key_and_recovers_without_reauthorizing(logged_in_carded):
    """Two things guard a retried book(): a Stripe idempotency_key derived
    from offer_id (so a real retried Stripe call returns the same
    authorization instead of a new one — only provable against real
    Stripe, see report) and, at the app level, a proactive
    order_for_offer() check that recovers before spending anything new at
    all. This proves the second, and proves the key passed to Stripe is
    deterministic across two calls."""
    client, account = logged_in_carded
    offer = fake_offer()
    order = fake_order(amount=offer["total_amount"], currency=offer["total_currency"])
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer, order=order)), \
         patch("billing.authorize_fare", return_value=MagicMock(id="pi_test_4")) as mock_authorize, \
         patch("billing.capture_authorization"):
        client.post("/book", data=book_form(offer["id"]))
    first_key = mock_authorize.call_args.kwargs["idempotency_key"]

    # Second attempt, same offer_id: book() always re-fetches the offer
    # first (needed for passenger validation regardless of outcome), but
    # order_for_offer() recovers right after — before authorize_fare is
    # ever called again, and before a second /air/orders POST.
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer)) as mock_duffel, \
         patch("billing.authorize_fare") as mock_authorize2:
        resp = client.post("/book", data=book_form(offer["id"]), follow_redirects=False)
    assert resp.status_code == 302
    assert order["id"] in resp.headers["Location"]
    mock_authorize2.assert_not_called()
    assert mock_duffel.call_count == 1, "only the offer GET, no second /air/orders POST"

    # And independently: if authorize_fare *were* called twice (e.g. a true
    # concurrent race that both lose the order_for_offer check), the key
    # it would be called with is stable, not per-attempt-random.
    assert first_key == f"book-{offer['id']}"


def test_capture_failure_persists_order_and_flags_for_resolution(logged_in_carded):
    """item 2b's fix: the order is now persisted before capture is even
    attempted, so a capture failure is a payment problem on a known order,
    not an untracked ticket. Both attempts fail (the retry doesn't save
    it) -> order exists, flagged, honest non-500 response, Duffel order
    never voided."""
    client, account = logged_in_carded
    offer = fake_offer()
    order = fake_order(amount=offer["total_amount"], currency=offer["total_currency"])
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer, order=order)), \
         patch("billing.authorize_fare", return_value=MagicMock(id="pi_test_5")), \
         patch("billing.capture_authorization",
              side_effect=billing.CardError("capture failed")) as mock_capture, \
         patch("billing.cancel_authorization") as mock_cancel:
        resp = client.post("/book", data=book_form(offer["id"]))

    assert resp.status_code == 200, "an honest error, not a 500"
    assert mock_capture.call_count == 2, "one retry, exactly"
    mock_cancel.assert_not_called(), "the ticket must never be voided over a payment hiccup"

    row = db.order_for_offer(account["account_id"], offer["id"])
    assert row is not None, "the order must exist even though capture failed"
    assert row["payment_capture_failed_at"] is not None
    assert "capture failed" in row["payment_capture_error"]


def test_capture_retry_then_success_leaves_no_flag(logged_in_carded):
    client, account = logged_in_carded
    offer = fake_offer()
    order = fake_order(amount=offer["total_amount"], currency=offer["total_currency"])
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer, order=order)), \
         patch("billing.authorize_fare", return_value=MagicMock(id="pi_test_5b")), \
         patch("billing.capture_authorization",
              side_effect=[billing.CardError("transient"), None]) as mock_capture:
        resp = client.post("/book", data=book_form(offer["id"]), follow_redirects=False)

    assert resp.status_code == 302, "a retry that succeeds proceeds normally"
    assert mock_capture.call_count == 2
    row = db.order_for_offer(account["account_id"], offer["id"])
    assert row["payment_capture_failed_at"] is None
    assert row["payment_capture_error"] is None


# ---------------------------------------------------------------------------
# the policy gate
# ---------------------------------------------------------------------------

def test_policy_block_refuses_with_no_external_calls(logged_in_carded):
    client, account = logged_in_carded
    insert_policy_rule(account["account_id"], "price_ceiling", "1.00", "block")
    offer = fake_offer(amount="219.00")
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer)) as mock_duffel, \
         patch("billing.authorize_fare") as mock_authorize:
        resp = client.post("/book", data=book_form(offer["id"]))
    assert resp.status_code == 403
    mock_authorize.assert_not_called()
    # duffel_http.request was called once, for the offer GET, and no
    # further (no /air/orders call).
    assert mock_duffel.call_count == 1
    assert db.order_for_offer(account["account_id"], offer["id"]) is None


def test_policy_require_approval_creates_one_booking_request(logged_in_carded):
    client, account = logged_in_carded
    insert_policy_rule(account["account_id"], "price_ceiling", "1.00", "require_approval")
    offer = fake_offer(amount="342.50", currency="USD")
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer)), \
         patch("billing.authorize_fare") as mock_authorize:
        resp = client.post("/book", data=book_form(offer["id"]), follow_redirects=False)
    assert resp.status_code == 302
    mock_authorize.assert_not_called()
    assert db.order_for_offer(account["account_id"], offer["id"]) is None

    rows = db.q("SELECT * FROM booking_requests WHERE account_id = %s",
               (account["account_id"],), fetch="all")
    assert len(rows) == 1
    assert rows[0]["amount"] == Decimal("342.50")
    assert rows[0]["policy_result"][0]["enforcement"] == "require_approval"


def test_policy_advise_proceeds(logged_in_carded):
    client, account = logged_in_carded
    insert_policy_rule(account["account_id"], "price_ceiling", "1.00", "advise")
    offer = fake_offer()
    order = fake_order(amount=offer["total_amount"], currency=offer["total_currency"])
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer, order=order)), \
         patch("billing.authorize_fare", return_value=MagicMock(id="pi_test_6")), \
         patch("billing.capture_authorization"):
        resp = client.post("/book", data=book_form(offer["id"]), follow_redirects=False)
    assert resp.status_code == 302
    assert db.order_for_offer(account["account_id"], offer["id"]) is not None


# ---------------------------------------------------------------------------
# delivery types — execute()'s cancel branch
# ---------------------------------------------------------------------------

def fake_cancel_sequence(refund_amount, refund_to=None, refund_currency="USD"):
    def _mock(method, path, body=None, params=None, label=None):
        if method == "POST" and path == "/air/order_cancellations":
            return {"id": "orc_test1"}
        if method == "POST" and path.endswith("/actions/confirm"):
            return {"confirmed_at": "2026-12-01T00:00:00Z", "refund_amount": refund_amount,
                    "refund_currency": refund_currency, "refund_to": refund_to}
        if method == "GET" and path.startswith("/air/orders/"):
            return fake_order(order_id=path.rsplit("/", 1)[-1])
        raise AssertionError(f"unexpected duffel_http.request({method!r}, {path!r})")
    return _mock


def execute_cancel(client, order_id, refund_amount, refund_to=None):
    with patch("duffel_http.request", side_effect=fake_cancel_sequence(refund_amount, refund_to)):
        return client.post(f"/orders/{order_id}/execute/cancel",
                           data={"confirm_text": "CONFIRM", "_csrf": CSRF}, follow_redirects=False)


def test_cash_refund_produces_refund_to_card(logged_in_carded):
    client, account = logged_in_carded
    order = make_real_order(account["account_id"])
    resp = execute_cancel(client, order["order_id"], "219.00", refund_to=None)
    assert resp.status_code == 302
    events = db.savings_events_for_order(order["order_id"])
    assert len(events) == 1
    assert events[0]["delivery_type"] == "refund_to_card"


def test_credit_produces_airline_credit_and_forward_link(logged_in_carded):
    client, account = logged_in_carded
    order = make_real_order(account["account_id"], carrier="Duffel Airways")
    resp = execute_cancel(client, order["order_id"], "219.00", refund_to="airline_credits")
    assert resp.status_code == 302
    events = db.savings_events_for_order(order["order_id"])
    assert len(events) == 1
    assert events[0]["delivery_type"] == "airline_credit"

    credit = db.q("SELECT id FROM airline_credits WHERE order_id = %s",
                 (order["order_id"],), fetch="one")
    assert credit is not None
    assert str(events[0]["airline_credit_id"]) == str(credit["id"])


def test_zero_refund_produces_forfeited_not_a_dropped_row(logged_in_carded):
    """The bug item 1 fixed: `if refunded > 0:` used to skip creating any
    savings_events row at all for a $0 outcome. If that gate ever comes
    back, this fails on the missing row, not just the wrong label."""
    client, account = logged_in_carded
    order = make_real_order(account["account_id"])
    resp = execute_cancel(client, order["order_id"], "0.00", refund_to=None)
    assert resp.status_code == 302
    events = db.savings_events_for_order(order["order_id"])
    assert len(events) == 1, "a forfeited cancellation must still record a savings_events row"
    assert events[0]["delivery_type"] == "forfeited"
    assert events[0]["commission_amount"] == "0.00"

    row = db.find_order(order["order_id"], account["account_id"])
    assert row["refunded"] == "0.00", "orders.refunded must be an explicit 0.00, not left NULL"


def test_exchange_negative_delta_reads_refund_to(logged_in_carded):
    """Mocked, not proof of the live path: Duffel sandbox hardcodes
    change_total_amount at +125.00 and engine.evaluate()'s own gate skips
    every non-negative delta, so no real exchange has ever reached this
    branch with a real negative value. This only proves the refund_to
    read is wired correctly against a shape Duffel's docs describe."""
    client, account = logged_in_carded
    order = make_real_order(account["account_id"], carrier="Duffel Airways",
                            last_decision={"source": "duffel", "change_offer_id": "oco_test1"})

    def _mock(method, path, body=None, params=None, label=None):
        if method == "POST" and path == "/air/order_changes":
            return {"id": "chg_test1", "change_total_amount": "-45.00",
                    "change_total_currency": "USD"}
        if method == "POST" and path.endswith("/actions/confirm"):
            return {"confirmed_at": "2026-12-01T00:00:00Z", "refund_to": "airline_credits"}
        if method == "GET" and path.startswith("/air/orders/"):
            return fake_order(order_id=path.rsplit("/", 1)[-1])
        raise AssertionError(f"unexpected duffel_http.request({method!r}, {path!r})")

    with patch("duffel_http.request", side_effect=_mock):
        resp = client.post(f"/orders/{order['order_id']}/execute/exchange",
                           data={"confirm_text": "CONFIRM", "_csrf": CSRF}, follow_redirects=False)
    assert resp.status_code == 302
    events = db.savings_events_for_order(order["order_id"])
    assert events[0]["delivery_type"] == "airline_credit"
    assert events[0]["realized_savings"] == "45.00"


# ---------------------------------------------------------------------------
# difference-band baseline stepping (item 3) — the dashed "paid" line steps
# down at the moment an exchange actually lands, instead of sloping between
# the old and new paid amount.
# ---------------------------------------------------------------------------

def test_execute_records_old_and_new_paid_on_the_audit_row(logged_in_carded):
    """execute()'s audit row is append-only (audit_events forbids UPDATE),
    so old_paid/new_paid have to be captured in the same insert that logs
    the execution — this is the only data source execution_steps() has."""
    client, account = logged_in_carded
    order = make_real_order(account["account_id"], carrier="Duffel Airways",
                            paid="250.00",
                            last_decision={"source": "duffel", "change_offer_id": "oco_test2"})

    def _mock(method, path, body=None, params=None, label=None):
        if method == "POST" and path == "/air/order_changes":
            return {"id": "chg_test2", "change_total_amount": "-31.00",
                    "change_total_currency": "USD"}
        if method == "POST" and path.endswith("/actions/confirm"):
            return {"confirmed_at": "2026-12-01T00:00:00Z", "refund_to": "airline_credits"}
        if method == "GET" and path.startswith("/air/orders/"):
            return fake_order(order_id=path.rsplit("/", 1)[-1], amount="219.00")
        raise AssertionError(f"unexpected duffel_http.request({method!r}, {path!r})")

    with patch("duffel_http.request", side_effect=_mock):
        resp = client.post(f"/orders/{order['order_id']}/execute/exchange",
                           data={"confirm_text": "CONFIRM", "_csrf": CSRF}, follow_redirects=False)
    assert resp.status_code == 302

    rows = db.audit_rows(order["order_id"])
    execution = next(r for r in rows if r["kind"] == "execution")
    assert execution["old_paid"] == "250.00"
    assert execution["new_paid"] == "219.00"

    steps = app_module.execution_steps(order["order_id"], "250.00")
    assert [v for _, v in steps] == [Decimal("250.00"), Decimal("219.00")]
    assert steps[0][0] < steps[1][0], "the starting point must sort before the execution's real timestamp"


def test_difference_band_steps_at_the_execution_timestamp():
    """Pure geometry, no DB: with baseline_steps given, the dashed baseline
    must jump exactly at the point-in-time the step occurs, not slope
    between the old and new value — and with none given, it must degrade to
    the plain flat line this function always drew before stepping existed."""
    points = [("2026-01-01T00:00:00+00:00", Decimal("200")),
             ("2026-01-02T00:00:00+00:00", Decimal("180")),
             ("2026-01-03T00:00:00+00:00", Decimal("210"))]

    flat = app_module.difference_band(points, Decimal("250"), "USD")
    assert flat["base_path"].count("L") == 2, "a flat baseline draws one segment per remaining point, no corners"

    stepped = app_module.difference_band(
        points, Decimal("250"), "USD",
        baseline_steps=[("", Decimal("250")), ("2026-01-02T00:00:00+00:00", Decimal("200"))])
    # One extra corner vs. the flat case: the horizontal run at the old
    # value, then the vertical jump down, both inserted at the same x.
    assert stepped["base_path"].count("L") == 3
    assert stepped["baseline"] == "200.00", "the displayed baseline is the *current* step, not the first one"


def test_execution_steps_with_no_executions_yields_flat_baseline(acct):
    """An order that was never rebooked has nothing to step at — real data
    or nothing, never a fabricated step."""
    order = make_real_order(acct["account_id"], paid="199.00")
    steps = app_module.execution_steps(order["order_id"], "199.00")
    assert steps == [("", Decimal("199.00"))]


# ---------------------------------------------------------------------------
# item 1's fixes, locked down
# ---------------------------------------------------------------------------

def test_completed_execution_writes_exactly_one_audit_row(logged_in_carded):
    client, account = logged_in_carded
    order = make_real_order(account["account_id"])
    execute_cancel(client, order["order_id"], "150.00")
    rows = db.q("""SELECT execution, payload FROM audit_events
                   WHERE order_id = %s AND kind = 'execution'""",
              (order["order_id"],), fetch="all")
    assert len(rows) == 1
    assert rows[0]["execution"] == "executed"


def test_failed_execution_writes_audit_row_with_failed(logged_in_carded):
    client, account = logged_in_carded
    order = make_real_order(account["account_id"])
    with patch("duffel_http.request", side_effect=DuffelError(422, [])):
        client.post(f"/orders/{order['order_id']}/execute/cancel",
                   data={"confirm_text": "CONFIRM", "_csrf": CSRF})
    rows = db.q("""SELECT execution FROM audit_events
                   WHERE order_id = %s AND kind = 'execution'""",
              (order["order_id"],), fetch="all")
    assert len(rows) == 1
    assert rows[0]["execution"] == "failed"


@pytest.mark.parametrize("delivery_type,expected_title", [
    ("refund_to_card", "Recovery executed — refunded to card"),
    ("airline_credit", "Recovery executed — airline credit issued"),
    ("forfeited", "Recovery executed — value forfeited, nothing recovered"),
])
def test_activity_label_distinguishes_delivery_type(delivery_type, expected_title):
    row = {"kind": "execution", "execution": "executed", "delivery_type": delivery_type,
          "detail": "test"}
    assert app_module._activity_label(row)["title"] == expected_title


def test_spend_by_carrier_counts_cancellation_recovery(logged_in_carded):
    client, account = logged_in_carded
    order = make_real_order(account["account_id"], carrier="Cancel Airways", paid="300.00")
    execute_cancel(client, order["order_id"], "300.00")
    rows = db.spend_by_carrier(account["account_id"])
    row = next(r for r in rows if r["carrier"] == "Cancel Airways")
    assert row["saved"] == Decimal("300.00")


def test_spend_by_carrier_does_not_multiply_paid_with_two_savings_events(logged_in_carded):
    account_id = None
    with app_module.app.test_client():
        pass
    # Build via fixtures manually since this needs two savings_events on
    # one order, which no single execute() call produces.
    email = f"test-{uuid.uuid4().hex[:12]}@example.com"
    user = db.create_account(email, password_hash="x")
    account_id = user["account_id"]
    try:
        order = make_real_order(account_id, carrier="Multi Airways", paid="500.00")
        for _ in range(2):
            db.savings_event_create(
                order["order_id"], execution_attempt_id=None,
                old_amount="500.00", new_amount="480.00", realized_savings="20.00",
                currency="USD", delivery_type="refund_to_card", delivery_detail="test",
                commission_rate="0.25")
        rows = db.spend_by_carrier(account_id)
        row = next(r for r in rows if r["carrier"] == "Multi Airways")
        assert row["spend"] == Decimal("500.00"), "paid must not be multiplied by the join"
        assert row["saved"] == Decimal("40.00")
    finally:
        db.q("DELETE FROM accounts WHERE id = %s", (account_id,))


def test_second_cancel_refused_at_confirm_and_execute(logged_in_carded):
    client, account = logged_in_carded
    order = make_real_order(account["account_id"])
    resp1 = execute_cancel(client, order["order_id"], "100.00")
    assert resp1.status_code == 302

    resp2 = client.get(f"/orders/{order['order_id']}/confirm/cancel", follow_redirects=False)
    assert resp2.status_code == 302
    assert "booked" not in resp2.headers.get("Location", "")

    with patch("duffel_http.request") as mock_duffel:
        resp3 = client.post(f"/orders/{order['order_id']}/execute/cancel",
                           data={"confirm_text": "CONFIRM", "_csrf": CSRF}, follow_redirects=False)
    assert resp3.status_code == 302
    mock_duffel.assert_not_called()

    events = db.savings_events_for_order(order["order_id"])
    assert len(events) == 1, "a refused repeat must not create a second savings_events row"


# ---------------------------------------------------------------------------
# commission and invoicing
# ---------------------------------------------------------------------------

def test_commission_reads_account_rate_not_the_constant(logged_in_carded):
    client, account = logged_in_carded
    db.q("UPDATE accounts SET commission_rate = 0.30 WHERE id = %s", (account["account_id"],))
    order = make_real_order(account["account_id"])
    execute_cancel(client, order["order_id"], "100.00")
    events = db.savings_events_for_order(order["order_id"])
    assert events[0]["commission_rate"] == "0.3000"
    assert events[0]["commission_amount"] == "30.00"


def test_invoice_generation_is_idempotent_per_period(logged_in_carded):
    client, account = logged_in_carded
    order = make_real_order(account["account_id"])
    execute_cancel(client, order["order_id"], "200.00")

    today = datetime.now(timezone.utc).date()  # matches generate_invoice_route()
    invoice1 = db.generate_invoice(account["account_id"], today, today + timedelta(days=1))
    lines1 = db.invoice_lines_for(invoice1["id"], account["account_id"])
    assert len(lines1) == 1
    assert lines1[0]["line_type"] == "commission_cash"
    assert invoice1["total"] == Decimal("50.00")

    invoice2 = db.generate_invoice(account["account_id"], today, today + timedelta(days=1))
    lines2 = db.invoice_lines_for(invoice2["id"], account["account_id"])
    assert lines2 == [], "a second run over the same period must not re-bill the same event"
    assert invoice2["total"] == Decimal("0.00")


def test_forfeited_events_produce_no_invoice_line(logged_in_carded):
    client, account = logged_in_carded
    order = make_real_order(account["account_id"])
    execute_cancel(client, order["order_id"], "0.00")  # forfeited

    today = datetime.now(timezone.utc).date()  # matches generate_invoice_route()
    invoice = db.generate_invoice(account["account_id"], today, today + timedelta(days=1))
    lines = db.invoice_lines_for(invoice["id"], account["account_id"])
    assert lines == []
    assert invoice["total"] == Decimal("0.00")


def test_invoice_lines_for_refuses_a_foreign_account():
    """The same shape /decisions leaked: a lookup safe only because its one
    caller happened to pass a trusted id. Proves the fix, not just that it
    compiles -- account B must get nothing back for account A's invoice,
    even though the invoice_id itself is valid."""
    email_a = f"test-{uuid.uuid4().hex[:12]}@example.com"
    email_b = f"test-{uuid.uuid4().hex[:12]}@example.com"
    a = db.create_account(email_a, password_hash="x")
    b = db.create_account(email_b, password_hash="x")
    try:
        order = make_real_order(a["account_id"])
        today = datetime.now(timezone.utc).date()  # matches generate_invoice_route()
        invoice = db.generate_invoice(a["account_id"], today, today + timedelta(days=1))
        db.q("""INSERT INTO invoice_lines (invoice_id, line_type, description, amount)
               VALUES (%s, 'subscription', 'test', 10.00)""", (invoice["id"],))

        assert len(db.invoice_lines_for(invoice["id"], a["account_id"])) == 1
        assert db.invoice_lines_for(invoice["id"], b["account_id"]) == []
    finally:
        db.q("DELETE FROM accounts WHERE id = %s", (a["account_id"],))
        db.q("DELETE FROM accounts WHERE id = %s", (b["account_id"],))
