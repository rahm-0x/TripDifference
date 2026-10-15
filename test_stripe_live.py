"""
Staging charging real cards: STRIPE_LIVE_ENABLED, and what it changes.

  - the flag is staging-only and needs a live key pair over a live Duffel token
    (billing.startup_check); without it staging stays on the test pair
  - which keys are in use is decided in one place (config.STRIPE_SECRET_KEY /
    STRIPE_PUBLISHABLE_KEY / STRIPE_LIVEMODE)
  - a customer and card saved under one Stripe mode are not on file under the
    other (migration 036), and the card page starts a new customer there
  - a test account is never charged for real, whatever is saved on it

Nothing here reaches Stripe: conftest makes billing._client unreachable, and a
test that needs a Stripe answer replaces it with a mock.
"""

from unittest.mock import MagicMock

import pytest

import billing
import config
import db
from test_money_path import CSRF, acct, client, link_fake_card, logged_in  # noqa: F401

TEST_TOKEN = "duffel_test_" + "0" * 32
LIVE_TOKEN = "duffel_live_" + "0" * 32

LIVE_STAGING = {"APP_ENV": "staging", "STRIPE_LIVE_ENABLED": "true", "DUFFEL_TOKEN": LIVE_TOKEN,
                "STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_PUBLISHABLE_KEY": "pk_test_x",
                "STRIPE_LIVE_SECRET_KEY": "sk_live_x", "STRIPE_LIVE_PUBLISHABLE_KEY": "pk_live_x"}


def set_env(monkeypatch, **changes):
    for name, value in {**LIVE_STAGING, **changes}.items():
        monkeypatch.setenv(name, value)


def stripe_mock(monkeypatch):
    stripe = MagicMock()
    monkeypatch.setattr(billing, "_client", lambda: stripe)
    return stripe


# ---------------------------------------------------------------------------
# which keys are in use
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("changes,secret,publishable,livemode", [
    ({}, "sk_live_x", "pk_live_x", True),
    ({"STRIPE_LIVE_ENABLED": "false"}, "sk_test_x", "pk_test_x", False),
    ({"STRIPE_LIVE_ENABLED": "TRUE"}, "sk_test_x", "pk_test_x", False),   # exactly "true", like every flag
    # the flag means nothing anywhere but staging (and startup refuses it there)
    ({"APP_ENV": "production"}, "sk_test_x", "pk_test_x", False),
    ({"APP_ENV": "dev"}, "sk_test_x", "pk_test_x", False),
])
def test_the_live_pair_is_used_only_on_staging_with_the_flag(monkeypatch, changes, secret, publishable, livemode):
    set_env(monkeypatch, **changes)
    assert config.STRIPE_SECRET_KEY == secret
    assert config.STRIPE_PUBLISHABLE_KEY == publishable
    assert config.STRIPE_LIVEMODE is livemode


# ---------------------------------------------------------------------------
# the startup assertion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("changes,refusal", [
    ({}, None),
    ({"APP_ENV": "production"}, "only allowed with APP_ENV=staging"),
    ({"APP_ENV": "dev"}, "only allowed with APP_ENV=staging"),
    ({"STRIPE_LIVE_SECRET_KEY": ""}, "STRIPE_LIVE_SECRET_KEY to be an sk_live_ key, and it is not set"),
    ({"STRIPE_LIVE_SECRET_KEY": "sk_test_x"}, "STRIPE_LIVE_SECRET_KEY to be an sk_live_ key"),
    ({"STRIPE_LIVE_PUBLISHABLE_KEY": "pk_test_x"}, "STRIPE_LIVE_PUBLISHABLE_KEY"),
    ({"STRIPE_LIVE_PUBLISHABLE_KEY": ""}, "STRIPE_LIVE_PUBLISHABLE_KEY"),
    # a real card for a sandbox ticket
    ({"DUFFEL_TOKEN": TEST_TOKEN}, "never really issued"),
    # flag off: the live pair sitting in the environment changes nothing ...
    ({"STRIPE_LIVE_ENABLED": "false"}, None),
    # ... and a live key in the ordinary variable is still refused. The flag is the only way.
    ({"STRIPE_LIVE_ENABLED": "false", "STRIPE_SECRET_KEY": "sk_live_x"}, "sk_test_"),
])
def test_startup_allows_live_stripe_only_as_a_whole(monkeypatch, changes, refusal):
    set_env(monkeypatch, **changes)
    if refusal is None:
        billing.startup_check()
    else:
        with pytest.raises(RuntimeError, match=refusal) as caught:
            billing.startup_check()
        assert "sk_live_x" not in str(caught.value)


# ---------------------------------------------------------------------------
# a card belongs to the mode it was saved under
# ---------------------------------------------------------------------------

def test_a_test_card_is_not_on_file_under_a_live_key(acct, monkeypatch):
    account_id = acct["account_id"]
    link_fake_card(account_id)
    assert db.account_card(account_id)["last4"] == "4242"

    set_env(monkeypatch)
    assert db.account_card(account_id) is None
    assert db.account_stripe_ids(account_id) is None

    # back on the test pair it is on file again: nothing was erased by looking
    set_env(monkeypatch, STRIPE_LIVE_ENABLED="false")
    assert db.account_card(account_id)["last4"] == "4242"
    assert db.account_stripe_ids(account_id)["stripe_customer_id"] == "cus_test_fake"


def test_the_card_page_starts_a_new_customer_in_the_mode_in_use(acct, monkeypatch):
    account_id = acct["account_id"]
    link_fake_card(account_id)
    set_env(monkeypatch)
    stripe = stripe_mock(monkeypatch)
    stripe.Customer.create.return_value.id = "cus_live_new"

    assert billing.ensure_customer(account_id, acct["email"]) == "cus_live_new"
    # the new customer has no card: the test one is gone rather than mislabelled as live
    assert db.account_stripe_ids(account_id) == {"stripe_customer_id": "cus_live_new",
                                                 "stripe_payment_method_id": None}
    assert db.account_card(account_id) is None

    # and it is reused from here on, not created again on every visit
    assert billing.ensure_customer(account_id, acct["email"]) == "cus_live_new"
    assert stripe.Customer.create.call_count == 1

    db.account_save_payment_method(account_id, stripe_payment_method_id="pm_live_new", brand="visa",
                                   last4="1111", exp_month=1, exp_year=2031)
    assert db.account_card(account_id)["last4"] == "1111"
    set_env(monkeypatch, STRIPE_LIVE_ENABLED="false")
    assert db.account_card(account_id) is None, "a live card must not read as a test one either"


def test_confirming_a_card_with_no_customer_in_this_mode_goes_back_to_the_card_page(logged_in, monkeypatch):
    client, account = logged_in
    link_fake_card(account["account_id"])
    set_env(monkeypatch, DUFFEL_LIVE_ORDERS_ENABLED="false", DUFFEL_LIVE_SEARCH_ENABLED="false")
    save = MagicMock()
    monkeypatch.setattr(billing, "save_payment_method", save)
    resp = client.post("/onboarding/payment/confirm", data={"payment_method_id": "pm_x", "_csrf": CSRF})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/onboarding/payment")
    save.assert_not_called()


# ---------------------------------------------------------------------------
# who a live key can charge
# ---------------------------------------------------------------------------

def test_a_test_account_is_never_charged_for_real(acct, monkeypatch):
    account_id = acct["account_id"]
    set_env(monkeypatch)
    db.account_set_stripe_customer(account_id, "cus_live_x")
    db.account_save_payment_method(account_id, stripe_payment_method_id="pm_live_x", brand="visa",
                                   last4="1111", exp_month=1, exp_year=2031)
    stripe = stripe_mock(monkeypatch)

    with pytest.raises(billing.CardError, match="test accounts"):
        billing.authorize_fare(account_id, amount="100.00", currency="USD", idempotency_key="k1")
    with pytest.raises(billing.CardError, match="test accounts"):
        billing.charge_commission(account_id, amount="10.00", currency="USD", idempotency_key="k2",
                                  description="fee")
    stripe.PaymentIntent.create.assert_not_called()

    # the same account under the test pair is charged as it always was
    set_env(monkeypatch, STRIPE_LIVE_ENABLED="false")
    link_fake_card(account_id)
    billing.authorize_fare(account_id, amount="100.00", currency="USD", idempotency_key="k3")
    assert stripe.PaymentIntent.create.call_count == 1


# ---------------------------------------------------------------------------
# what a deploy says about itself
# ---------------------------------------------------------------------------

def test_healthz_env_reports_stripe_as_a_mode_never_a_key(logged_in, monkeypatch):
    client, _ = logged_in
    set_env(monkeypatch, DUFFEL_LIVE_ORDERS_ENABLED="false", DUFFEL_LIVE_SEARCH_ENABLED="false")

    # Stripe could not be asked (the suite's default): the mode is still known
    resp = client.get("/healthz/env")
    body = resp.get_json()
    assert (body["stripe_live_enabled"], body["stripe_mode"], body["stripe_charges_enabled"]) == (True, "live", None)
    assert "sk_live_x" not in resp.get_data(as_text=True) and "pk_live_x" not in resp.get_data(as_text=True)

    stripe = stripe_mock(monkeypatch)
    stripe.Account.retrieve.return_value.charges_enabled = False
    assert client.get("/healthz/env").get_json()["stripe_charges_enabled"] is False
    stripe.Account.retrieve.return_value.charges_enabled = True
    assert client.get("/healthz/env").get_json()["stripe_charges_enabled"] is True

    # on the test pair Stripe is not asked at all
    set_env(monkeypatch, STRIPE_LIVE_ENABLED="false", DUFFEL_LIVE_ORDERS_ENABLED="false",
            DUFFEL_LIVE_SEARCH_ENABLED="false")
    stripe.Account.retrieve.reset_mock()
    body = client.get("/healthz/env").get_json()
    assert (body["stripe_live_enabled"], body["stripe_mode"], body["stripe_charges_enabled"]) == (False, "test", None)
    stripe.Account.retrieve.assert_not_called()


@pytest.mark.parametrize("stripe_live,says_so", [("true", True), ("false", False)])
def test_the_banner_says_when_the_cards_are_real(logged_in, monkeypatch, stripe_live, says_so):
    client, _ = logged_in
    set_env(monkeypatch, STRIPE_LIVE_ENABLED=stripe_live, DUFFEL_LIVE_ORDERS_ENABLED="true")
    page = client.get("/info").get_data(as_text=True)
    assert "STAGING — LIVE TICKETS, REAL MONEY" in page
    assert ("REAL CARDS CHARGED" in page) is says_so
