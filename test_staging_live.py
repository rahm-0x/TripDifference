"""
Staging live-order guards: the environment model (config.py), the Duffel
token guard and live-payment backstop (duffel_http.py), the Stripe key
assertion (billing.py), the spend controls (live_guard.py) through book() and
execute(), the database protections (migration 030), the live-only
validation harness, and the staging banner.

Runs against the staging database like everything else (conftest.py). No test
here leaves a live order behind — migration 030 makes one undeletable — so:
  - every live-mode booking and top-up below is refused, by design: test
    accounts can never spend live money, and each test makes sure the rule it
    exercises is the one that refuses;
  - the database-protection tests insert live orders inside a transaction that
    is always rolled back;
  - live_spend rows a test inserts are deleted by captured id before its
    account is.

    .venv/bin/python -m pytest test_staging_live.py -v
"""

import importlib.util
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest

import billing
import config
import db
import duffel_http
import live_guard
import paths
# Shared fixtures and fake payloads. Importing the fixtures registers them for
# this module too, including the carrier-row cleanup.
from test_money_path import (CSRF, acct, book_form, client, delete_test_carrier_rows,  # noqa: F401
                             duffel_side_effect, fake_offer, fake_order, logged_in,
                             logged_in_carded, make_real_order)

ROOT = Path(__file__).resolve().parent
TEST_TOKEN = "duffel_test_" + "0" * 32
LIVE_TOKEN = "duffel_live_" + "0" * 32
BANNER = "STAGING — LIVE TICKETS, REAL MONEY"
SEARCH_BANNER = "STAGING — LIVE DUFFEL SEARCH · ORDERS DISABLED"


@pytest.fixture(autouse=True)
def no_response_dumps(monkeypatch):
    monkeypatch.setattr(paths, "DUMP_RESPONSES", False)


@pytest.fixture
def live_staging(monkeypatch):
    """Staging with live search and live orders on, a live token, and caps
    generous enough that only the rule a test sets up can refuse. Nobody is
    allowlisted until a test says so."""
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DUFFEL_LIVE_SEARCH_ENABLED", "true")
    monkeypatch.setenv("DUFFEL_LIVE_ORDERS_ENABLED", "true")
    monkeypatch.setenv("DUFFEL_TOKEN", LIVE_TOKEN)
    monkeypatch.setenv("STAGING_MAX_ORDER_USD", "1000")
    monkeypatch.setenv("STAGING_MAX_DAILY_USD", "100000000")
    monkeypatch.setenv("STAGING_ALLOWED_EMAILS", "")
    return monkeypatch


# ---------------------------------------------------------------------------
# config.py
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, "production"), ("", "production"), ("Staging", "production"), ("prod", "production"),
    ("dev", "dev"), ("staging", "staging"), ("production", "production"),
])
def test_app_env_missing_or_unknown_is_production(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", raw)
    assert config.APP_ENV == expected


@pytest.mark.parametrize("flag", ["DUFFEL_LIVE_SEARCH_ENABLED", "DUFFEL_LIVE_ORDERS_ENABLED"])
@pytest.mark.parametrize("raw,expected", [(None, False), ("", False), ("True", False),
                                          ("1", False), ("yes", False), ("true", True)])
def test_live_flags_only_for_exactly_true(monkeypatch, flag, raw, expected):
    if raw is None:
        monkeypatch.delenv(flag, raising=False)
    else:
        monkeypatch.setenv(flag, raw)
    assert getattr(config, flag) is expected


@pytest.mark.parametrize("raw,expected", [(None, 1400), ("", 1400), ("250", 250), ("0", 0),
                                          ("-1", 0), ("lots", 0), ("12.5", 0)])
def test_monthly_search_budget_defaults_to_1400_and_fails_closed(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("STAGING_MAX_MONTHLY_SEARCHES", raising=False)
    else:
        monkeypatch.setenv("STAGING_MAX_MONTHLY_SEARCHES", raw)
    assert config.STAGING_MAX_MONTHLY_SEARCHES == expected


def test_caps_and_allowlist_parse(monkeypatch):
    monkeypatch.setenv("STAGING_MAX_ORDER_USD", "250.50")
    monkeypatch.setenv("STAGING_MAX_DAILY_USD", "not a number")
    monkeypatch.setenv("STAGING_ALLOWED_EMAILS", " Ops@Acme.test , ,phoenix@example.com ")
    assert config.STAGING_MAX_ORDER_USD == Decimal("250.50")
    assert config.STAGING_MAX_DAILY_USD is None
    assert config.STAGING_ALLOWED_EMAILS == frozenset({"ops@acme.test", "phoenix@example.com"})
    for bad in ("0", "-5", "NaN", "Infinity"):
        monkeypatch.setenv("STAGING_MAX_ORDER_USD", bad)
        assert config.STAGING_MAX_ORDER_USD is None, bad


# ---------------------------------------------------------------------------
# the Duffel guard — {dev, staging, production} × {test, live} ×
# DUFFEL_LIVE_SEARCH_ENABLED {true, false} × DUFFEL_LIVE_ORDERS_ENABLED {true, false}
# ---------------------------------------------------------------------------

OFFER_REQUEST_BODY = {"data": {"slices": [{"origin": "LHR", "destination": "JFK", "departure_date": "2026-12-01"}],
                               "passengers": [{"type": "adult"}], "cabin_class": "economy"}}


def _flag(value):
    return "true" if value else "false"


@pytest.mark.parametrize("orders_enabled", [True, False])
@pytest.mark.parametrize("search_enabled", [True, False])
@pytest.mark.parametrize("token_kind", ["test", "live"])
@pytest.mark.parametrize("app_env", ["dev", "staging", "production"])
def test_duffel_guard_matrix(monkeypatch, app_env, token_kind, search_enabled, orders_enabled):
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("DUFFEL_LIVE_SEARCH_ENABLED", _flag(search_enabled))
    monkeypatch.setenv("DUFFEL_LIVE_ORDERS_ENABLED", _flag(orders_enabled))
    monkeypatch.setenv("DUFFEL_TOKEN", TEST_TOKEN if token_kind == "test" else LIVE_TOKEN)

    if app_env != "staging" and (search_enabled or orders_enabled):
        # a live flag anywhere but staging is refused at startup, whatever the token
        with pytest.raises(RuntimeError, match="only allowed with APP_ENV=staging"):
            duffel_http.startup_check()
        return
    if token_kind == "live" and not (app_env == "staging" and (search_enabled or orders_enabled)):
        with pytest.raises(RuntimeError, match="LIVE token"):
            duffel_http.startup_check()
        with pytest.raises(RuntimeError, match="LIVE token"):
            duffel_http.token()
        return

    duffel_http.startup_check()
    assert duffel_http.mode() == token_kind

    # what each kind of request may do once started
    with patch("duffel_http.requests.request", return_value=_duffel_response({"id": "x"})) as sent:
        with duffel_http.search_authorized(duffel_http.SearchAuthorization(0)):
            if token_kind == "test" or search_enabled:
                duffel_http.request("POST", "/air/offer_requests", body=OFFER_REQUEST_BODY)
            else:
                with pytest.raises(duffel_http.LiveRequestRefused, match="search is disabled"):
                    duffel_http.request("POST", "/air/offer_requests", body=OFFER_REQUEST_BODY)
        for method, path in (("POST", "/air/order_cancellations"), ("POST", "/air/order_change_requests"),
                             ("POST", "/air/order_changes"), ("GET", "/air/orders/ord_x")):
            if token_kind == "test" or orders_enabled:
                duffel_http.request(method, path, body={"data": {"order_id": "ord_x"}})
            else:
                with pytest.raises(duffel_http.LiveRequestRefused, match="Live Duffel orders are disabled"):
                    duffel_http.request(method, path, body={"data": {"order_id": "ord_x"}})
    expected_sent = (1 if token_kind == "test" or search_enabled else 0) + \
                    (4 if token_kind == "test" or orders_enabled else 0)
    assert sent.call_count == expected_sent


def test_unrecognised_token_is_refused_everywhere():
    for app_env in ("dev", "staging", "production"):
        with pytest.raises(RuntimeError, match="neither a duffel_test_ nor a duffel_live_"):
            duffel_http.check_token("sk_live_whatever", app_env, app_env == "staging", False)


def test_live_offer_request_needs_a_single_use_search_authorization(live_staging):
    with patch("duffel_http.requests.request", return_value=_duffel_response({"id": "orq_x"})) as sent:
        with pytest.raises(duffel_http.LiveRequestRefused, match="no unused search-budget authorization"):
            duffel_http.request("POST", "/air/offer_requests", body=OFFER_REQUEST_BODY)
        with duffel_http.search_authorized(duffel_http.SearchAuthorization(0)):
            duffel_http.request("POST", "/air/offer_requests", body=OFFER_REQUEST_BODY)
            with pytest.raises(duffel_http.LiveRequestRefused, match="no unused search-budget authorization"):
                duffel_http.request("POST", "/air/offer_requests", body=OFFER_REQUEST_BODY)
        duffel_http.request("GET", "/air/offers/off_x")  # a single-offer fetch needs no authorization
    assert sent.call_count == 2


# ---------------------------------------------------------------------------
# which database APP_ENV may run against
# ---------------------------------------------------------------------------

STAGING_POOLED = ("postgresql://postgres.bcqzwnoifwkuimrrdysy:pw@aws-0-us-west-1.pooler.supabase.com:6543/postgres")
STAGING_DIRECT = "postgresql://postgres:pw@db.bcqzwnoifwkuimrrdysy.supabase.co:5432/postgres"
PRODUCTION_POOLED = ("postgresql://postgres.uixgspesihupgcqzrzfw:pw@aws-0-us-west-1.pooler.supabase.com:6543/postgres")


@pytest.mark.parametrize("app_env,pooled,direct,refusal", [
    ("staging", STAGING_POOLED, STAGING_DIRECT, None),
    ("staging", PRODUCTION_POOLED, None, "POSTGRES_URL is not the staging database"),
    ("staging", STAGING_POOLED, PRODUCTION_POOLED, "POSTGRES_URL_NON_POOLING is not the staging database"),
    ("staging", None, None, "POSTGRES_URL is not set"),
    ("production", PRODUCTION_POOLED, None, None),
    ("production", STAGING_POOLED, None, "is the staging database"),
    ("production", PRODUCTION_POOLED, STAGING_DIRECT, "POSTGRES_URL_NON_POOLING is the staging database"),
    ("dev", STAGING_POOLED, None, None),
])
def test_database_must_match_app_env(monkeypatch, app_env, pooled, direct, refusal):
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name, value in (("POSTGRES_URL", pooled), ("POSTGRES_URL_NON_POOLING", direct)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    if refusal is None:
        db.startup_check()
    else:
        with pytest.raises(RuntimeError, match=refusal):
            db.startup_check()


@pytest.mark.parametrize("env,refusal", [
    ({"APP_ENV": "production", "DUFFEL_LIVE_SEARCH_ENABLED": "true", "DUFFEL_TOKEN": TEST_TOKEN,
      "STRIPE_SECRET_KEY": "sk_live_x"}, "only allowed with APP_ENV=staging"),
    ({"APP_ENV": "production", "DUFFEL_TOKEN": LIVE_TOKEN, "STRIPE_SECRET_KEY": "sk_live_x"}, "LIVE token"),
    ({"APP_ENV": "staging", "DUFFEL_LIVE_SEARCH_ENABLED": "true", "DUFFEL_TOKEN": LIVE_TOKEN,
      "STRIPE_SECRET_KEY": "sk_live_x"}, "sk_test_"),
    ({"APP_ENV": "production", "DUFFEL_TOKEN": TEST_TOKEN, "STRIPE_SECRET_KEY": "sk_live_x",
      "POSTGRES_URL": STAGING_POOLED, "POSTGRES_URL_NON_POOLING": ""}, "is the staging database"),
    ({"APP_ENV": "staging", "DUFFEL_TOKEN": TEST_TOKEN, "STRIPE_SECRET_KEY": "sk_test_x",
      "POSTGRES_URL": PRODUCTION_POOLED, "POSTGRES_URL_NON_POOLING": ""}, "not the staging database"),
    ({"APP_ENV": "staging", "DUFFEL_LIVE_SEARCH_ENABLED": "true", "DUFFEL_LIVE_ORDERS_ENABLED": "false",
      "DUFFEL_TOKEN": LIVE_TOKEN, "STRIPE_SECRET_KEY": "sk_test_x",
      "POSTGRES_URL": STAGING_POOLED, "POSTGRES_URL_NON_POOLING": STAGING_DIRECT}, None),
])
def test_guards_fail_at_import_not_on_a_request(env, refusal):
    """A fresh interpreter importing app.py — the function's cold start."""
    result = subprocess.run([sys.executable, "-c", "import app"], cwd=ROOT,
                            env={**os.environ, **env}, capture_output=True, text=True, timeout=120)
    if refusal is None:
        assert result.returncode == 0, result.stderr[-2000:]
    else:
        assert result.returncode != 0
        assert refusal in result.stderr


# ---------------------------------------------------------------------------
# the Stripe key assertion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("app_env,key,ok", [
    ("staging", "sk_test_abc", True),
    ("staging", "sk_live_abc", False),
    ("staging", "rk_test_abc", False),
    ("staging", "", False),
    ("production", "sk_live_abc", True),
    ("dev", "sk_live_abc", True),
])
def test_staging_requires_a_stripe_test_key(monkeypatch, app_env, key, ok):
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("STRIPE_SECRET_KEY", key)
    if ok:
        billing.startup_check()
    else:
        with pytest.raises(RuntimeError, match="sk_test_"):
            billing.startup_check()


# ---------------------------------------------------------------------------
# the live-payment backstop inside duffel_http.request
# ---------------------------------------------------------------------------

PAYMENT_BODY = {"data": {"type": "instant", "selected_offers": ["off_x"],
                         "payments": [{"type": "balance", "currency": "USD", "amount": "100.00"}]}}


def _duffel_response(data):
    return MagicMock(status_code=201, ok=True, headers={}, json=lambda: {"data": data})


def test_live_payment_without_authorization_never_leaves_the_process(live_staging):
    with patch("duffel_http.requests.request") as sent:
        with pytest.raises(duffel_http.LiveSpendBlocked, match="no spend authorization"):
            duffel_http.request("POST", "/air/orders", body=PAYMENT_BODY)
    sent.assert_not_called()


@pytest.mark.parametrize("authorization,refusal", [
    (duffel_http.SpendAuthorization(1, config.TEST_ACCOUNT_NAME, Decimal("100.00"), "USD"), "test accounts"),
    (duffel_http.SpendAuthorization(1, "Acme", Decimal("99.99"), "USD"), "exceeds"),
    (duffel_http.SpendAuthorization(1, "Acme", Decimal("100.00"), "GBP"), "currency"),
])
def test_live_payment_authorization_must_match(live_staging, authorization, refusal):
    with patch("duffel_http.requests.request") as sent, duffel_http.spend_authorized(authorization):
        with pytest.raises(duffel_http.LiveSpendBlocked, match=refusal):
            duffel_http.request("POST", "/air/orders", body=PAYMENT_BODY)
    sent.assert_not_called()


def test_live_payment_with_a_matching_authorization_is_sent(live_staging):
    authorization = duffel_http.SpendAuthorization(1, "Acme", Decimal("100.00"), "USD")
    with patch("duffel_http.requests.request", return_value=_duffel_response({"id": "ord_x"})) as sent, \
         duffel_http.spend_authorized(authorization):
        assert duffel_http.request("POST", "/air/orders", body=PAYMENT_BODY) == {"id": "ord_x"}
    sent.assert_called_once()


def test_live_quote_request_needs_no_authorization(live_staging):
    body = {"data": {"slices": [], "private_fares": {}}}
    with patch("duffel_http.requests.request", return_value=_duffel_response([])) as sent:
        duffel_http.request("POST", "/air/order_change_requests", body=body)
    sent.assert_called_once()


def test_test_token_payment_needs_no_authorization(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DUFFEL_TOKEN", TEST_TOKEN)
    with patch("duffel_http.requests.request", return_value=_duffel_response({"id": "ord_x"})) as sent:
        duffel_http.request("POST", "/air/orders", body=PAYMENT_BODY)
    sent.assert_called_once()


# ---------------------------------------------------------------------------
# live_guard.check — the rules, in order
# ---------------------------------------------------------------------------

ALLOWED = "ops@acme.test"


@pytest.mark.parametrize("overrides,env,reason", [
    ({}, {}, None),
    ({"email": "OPS@Acme.Test"}, {}, None),
    ({"email": "someone@else.test"}, {}, "not_allowlisted"),
    ({}, {"STAGING_ALLOWED_EMAILS": ""}, "not_allowlisted"),
    ({"currency": "GBP"}, {}, "non_usd"),
    ({}, {"STAGING_MAX_DAILY_USD": ""}, "caps_not_configured"),
    ({"amount": "500.00"}, {}, None),
    ({"amount": "500.01"}, {}, "over_order_cap"),
    ({"spent_today": Decimal("800.00"), "amount": "200.00"}, {}, None),
    ({"spent_today": Decimal("800.00"), "amount": "200.01"}, {}, "over_daily_cap"),
    ({"account_name": config.TEST_ACCOUNT_NAME}, {}, "test_account"),
])
def test_live_guard_rules(monkeypatch, overrides, env, reason):
    monkeypatch.setenv("STAGING_ALLOWED_EMAILS", ALLOWED)
    monkeypatch.setenv("STAGING_MAX_ORDER_USD", "500")
    monkeypatch.setenv("STAGING_MAX_DAILY_USD", "1000")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    kwargs = {"account_name": "Acme", "email": ALLOWED, "amount": "200.00", "currency": "USD",
              "spent_today": Decimal("0"), **overrides}
    if reason is None:
        live_guard.check(**kwargs)
    else:
        with pytest.raises(live_guard.LiveSpendRejected) as refused:
            live_guard.check(**kwargs)
        assert refused.value.reason == reason


# ---------------------------------------------------------------------------
# book() on staging with a live token — every booking here must be refused
# ---------------------------------------------------------------------------

def _book(client, offer):
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer)) as duffel, \
         patch("billing.authorize_fare") as authorize:
        resp = client.post("/book", data=book_form(offer["id"]), follow_redirects=False)
    return resp, duffel, authorize


def _assert_refused_before_any_spend(account_id, offer, resp, duffel, authorize, reason):
    assert resp.status_code == 403
    authorize.assert_not_called()
    assert not [c for c in duffel.call_args_list if c.args[:2] == ("POST", "/air/orders")]
    assert db.order_for_offer(account_id, offer["id"]) is None
    rows = db.q("""SELECT reason, source FROM audit_events
                    WHERE kind = 'live_guard' AND payload->>'reference' = %s""", (offer["id"],), fetch="all")
    assert [(r["reason"], r["source"]) for r in rows] == [(reason, "booking")]
    assert db.q("SELECT count(*) AS n FROM live_spend WHERE account_id = %s",
                (account_id,), fetch="one")["n"] == 0


def test_live_booking_refused_when_not_allowlisted(live_staging, logged_in_carded):
    client, account = logged_in_carded
    offer = fake_offer(amount="219.00")
    resp, duffel, authorize = _book(client, offer)
    _assert_refused_before_any_spend(account["account_id"], offer, resp, duffel, authorize, "not_allowlisted")


def test_live_booking_refused_over_the_per_order_cap(live_staging, logged_in_carded):
    client, account = logged_in_carded
    live_staging.setenv("STAGING_ALLOWED_EMAILS", account["email"])
    live_staging.setenv("STAGING_MAX_ORDER_USD", "200.00")
    offer = fake_offer(amount="219.00")
    resp, duffel, authorize = _book(client, offer)
    _assert_refused_before_any_spend(account["account_id"], offer, resp, duffel, authorize, "over_order_cap")


def test_live_booking_refused_over_the_daily_cap_summed_from_the_ledger(live_staging, logged_in_carded):
    """Today's live spend comes from live_spend: a 'spent' row counts, a
    'released' one doesn't. With the spent row, the booking goes over the cap;
    without it, the same booking clears the cap and is refused only because
    this is a test account."""
    client, account = logged_in_carded
    live_staging.setenv("STAGING_ALLOWED_EMAILS", account["email"])
    ledger_ids = []
    try:
        for status, amount in (("spent", "300.00"), ("released", "5000.00")):
            ledger_ids.append(db.q("""INSERT INTO live_spend (kind, status, account_id, amount, currency, reference)
                                      VALUES ('booking', %s, %s, %s, 'USD', 'pytest-ledger') RETURNING id""",
                                   (status, account["account_id"], amount), fetch="one")["id"])
        spent = db.live_spend_today()
        live_staging.setenv("STAGING_MAX_DAILY_USD", str(spent + Decimal("219.00") - Decimal("0.01")))

        offer = fake_offer(amount="219.00")
        resp, duffel, authorize = _book(client, offer)
        assert resp.status_code == 403
        authorize.assert_not_called()
        reasons = db.q("""SELECT reason FROM audit_events WHERE kind = 'live_guard'
                           AND payload->>'reference' = %s""", (offer["id"],), fetch="all")
        assert [r["reason"] for r in reasons] == ["over_daily_cap"]

        db.q("DELETE FROM live_spend WHERE id = %s", (ledger_ids.pop(0),))
        second = fake_offer(amount="219.00")
        resp, duffel, authorize = _book(client, second)
        assert resp.status_code == 403
        reasons = db.q("""SELECT reason FROM audit_events WHERE kind = 'live_guard'
                           AND payload->>'reference' = %s""", (second["id"],), fetch="all")
        assert [r["reason"] for r in reasons] == ["test_account"]
    finally:
        for ledger_id in ledger_ids:
            db.q("DELETE FROM live_spend WHERE id = %s", (ledger_id,))


def test_test_account_never_books_live_even_allowlisted_and_under_caps(live_staging, logged_in_carded):
    client, account = logged_in_carded
    live_staging.setenv("STAGING_ALLOWED_EMAILS", account["email"])
    offer = fake_offer(amount="219.00")
    resp, duffel, authorize = _book(client, offer)
    _assert_refused_before_any_spend(account["account_id"], offer, resp, duffel, authorize, "test_account")


def test_live_booking_refused_while_live_orders_are_disabled(live_staging, logged_in_carded):
    """Search-only staging: nothing is reserved, no card is authorized, no order
    call is made, and the page says why."""
    client, account = logged_in_carded
    live_staging.setenv("DUFFEL_LIVE_ORDERS_ENABLED", "false")
    live_staging.setenv("STAGING_ALLOWED_EMAILS", account["email"])
    offer = fake_offer(amount="219.00")
    resp, duffel, authorize = _book(client, offer)
    assert resp.status_code == 403
    assert "Live Duffel orders are disabled" in resp.get_data(as_text=True)
    authorize.assert_not_called()
    assert not [c for c in duffel.call_args_list if c.args[:2] == ("POST", "/air/orders")]
    assert db.q("SELECT count(*) AS n FROM live_spend WHERE account_id = %s",
                (account["account_id"],), fetch="one")["n"] == 0


def test_live_cancel_refused_while_live_orders_are_disabled(live_staging, logged_in_carded):
    """Through the real duffel_http.request: the cancel never leaves the process."""
    client, account = logged_in_carded
    live_staging.setenv("DUFFEL_LIVE_ORDERS_ENABLED", "false")
    order = make_real_order(account["account_id"])
    with patch("duffel_http.requests.request") as sent:
        resp = client.post(f"/orders/{order['order_id']}/execute/cancel",
                           data={"confirm_text": "CONFIRM", "_csrf": CSRF}, follow_redirects=False)
    sent.assert_not_called()
    assert resp.status_code == 200
    assert "Live Duffel orders are disabled" in resp.get_data(as_text=True)
    assert db.find_order(order["order_id"], account["account_id"])["executed"] is None


def test_staging_with_a_test_token_books_normally_as_duffel_mode_test(monkeypatch, logged_in_carded):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DUFFEL_LIVE_SEARCH_ENABLED", "true")
    monkeypatch.setenv("DUFFEL_TOKEN", TEST_TOKEN)
    client, account = logged_in_carded
    offer = fake_offer()
    order = fake_order(amount=offer["total_amount"], currency=offer["total_currency"])
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer, order=order)), \
         patch("billing.authorize_fare", return_value=MagicMock(id="pi_test_staging")), \
         patch("billing.capture_authorization"):
        resp = client.post("/book", data=book_form(offer["id"]), follow_redirects=False)
    assert resp.status_code == 302
    assert db.find_order(order["id"], account["account_id"])["duffel_mode"] == "test"
    assert db.q("SELECT count(*) AS n FROM live_spend WHERE account_id = %s",
                (account["account_id"],), fetch="one")["n"] == 0


def test_production_booking_records_duffel_mode_test(logged_in_carded):
    client, account = logged_in_carded
    offer = fake_offer()
    order = fake_order(amount=offer["total_amount"], currency=offer["total_currency"])
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer, order=order)), \
         patch("billing.authorize_fare", return_value=MagicMock(id="pi_test_prod_mode")), \
         patch("billing.capture_authorization"):
        client.post("/book", data=book_form(offer["id"]))
    assert db.find_order(order["id"], account["account_id"])["duffel_mode"] == "test"


# ---------------------------------------------------------------------------
# execute() exchange top-ups on staging with a live token
# ---------------------------------------------------------------------------

def _exchange_with_topup(client, order, change_total):
    def _mock(method, path, body=None, params=None, label=None):
        if method == "POST" and path == "/air/order_changes":
            return {"id": f"oce_test_{uuid.uuid4().hex[:10]}", "change_total_amount": change_total,
                    "change_total_currency": "USD"}
        raise AssertionError(f"unexpected duffel_http.request({method!r}, {path!r}) — "
                             "a refused top-up must never reach the confirm call")

    with patch("duffel_http.request", side_effect=_mock) as duffel:
        resp = client.post(f"/orders/{order['order_id']}/execute/exchange",
                           data={"confirm_text": "CONFIRM", "_csrf": CSRF}, follow_redirects=False)
    return resp, duffel


def _assert_topup_refused(order, resp, duffel, reason):
    assert resp.status_code == 200
    assert "Live exchange refused" in resp.get_data(as_text=True)
    assert [c.args[1] for c in duffel.call_args_list] == ["/air/order_changes"]
    rows = db.q("SELECT reason, source FROM audit_events WHERE kind = 'live_guard' AND order_id = %s",
                (order["order_id"],), fetch="all")
    assert [(r["reason"], r["source"]) for r in rows] == [(reason, "exchange_topup")]
    assert db.q("SELECT count(*) AS n FROM execution_attempts WHERE order_id = %s",
                (order["order_id"],), fetch="one")["n"] == 0, "the claim is released so a retry is possible"
    assert db.find_order(order["order_id"], order["account_id"])["executed"] is None


def test_live_topup_refused_over_the_per_order_cap(live_staging, logged_in_carded):
    client, account = logged_in_carded
    live_staging.setenv("STAGING_ALLOWED_EMAILS", account["email"])
    live_staging.setenv("STAGING_MAX_ORDER_USD", "30.00")
    order = make_real_order(account["account_id"],
                            last_decision={"source": "duffel", "change_offer_id": "oco_test_topup1"})
    resp, duffel = _exchange_with_topup(client, order, "40.00")
    _assert_topup_refused(order, resp, duffel, "over_order_cap")


def test_live_topup_refused_for_a_test_account_under_the_caps(live_staging, logged_in_carded):
    client, account = logged_in_carded
    live_staging.setenv("STAGING_ALLOWED_EMAILS", account["email"])
    order = make_real_order(account["account_id"],
                            last_decision={"source": "duffel", "change_offer_id": "oco_test_topup2"})
    resp, duffel = _exchange_with_topup(client, order, "40.00")
    _assert_topup_refused(order, resp, duffel, "test_account")


# ---------------------------------------------------------------------------
# migration 030's database protections — inside transactions that roll back
# ---------------------------------------------------------------------------

@contextmanager
def rolled_back():
    with db.pool().connection() as conn:
        with conn.transaction(force_rollback=True):
            yield conn


def _account(conn):
    return conn.execute("INSERT INTO accounts (name) VALUES (%s) RETURNING id",
                        (config.TEST_ACCOUNT_NAME,)).fetchone()["id"]


def _order(conn, account_id, mode):
    order_id = f"ord_test_{uuid.uuid4().hex[:16]}"
    conn.execute("INSERT INTO orders (order_id, account_id, raw, duffel_mode) VALUES (%s, %s, '{}', %s)",
                 (order_id, account_id, mode))
    return order_id


def test_live_order_cannot_be_deleted():
    with rolled_back() as conn:
        order_id = _order(conn, _account(conn), "live")
        with pytest.raises(psycopg.errors.RestrictViolation, match="live Duffel order"):
            with conn.transaction():
                conn.execute("DELETE FROM orders WHERE order_id = %s", (order_id,))


def test_account_owning_a_live_order_cannot_be_deleted():
    with rolled_back() as conn:
        account_id = _account(conn)
        _order(conn, account_id, "live")
        with pytest.raises(psycopg.errors.RestrictViolation, match="owns live Duffel orders"):
            with conn.transaction():
                conn.execute("DELETE FROM accounts WHERE id = %s", (account_id,))


def test_duffel_mode_cannot_change_after_creation():
    with rolled_back() as conn:
        account_id = _account(conn)
        for start, flipped in (("live", "test"), ("test", "live")):
            order_id = _order(conn, account_id, start)
            with pytest.raises(psycopg.errors.RestrictViolation, match="cannot change"):
                with conn.transaction():
                    conn.execute("UPDATE orders SET duffel_mode = %s WHERE order_id = %s", (flipped, order_id))


def test_test_mode_orders_and_their_accounts_still_delete():
    with rolled_back() as conn:
        account_id = _account(conn)
        _order(conn, account_id, "test")
        conn.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
        assert conn.execute("SELECT count(*) AS n FROM orders WHERE account_id = %s",
                            (account_id,)).fetchone()["n"] == 0


def test_deleting_an_observed_order_is_a_foreign_key_error():
    with rolled_back() as conn:
        order_id = _order(conn, _account(conn), "test")
        conn.execute("INSERT INTO reshop_observations (order_id, gate) VALUES (%s, 1)", (order_id,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation, match="reshop_observations_order_id_fkey"):
            with conn.transaction():
                conn.execute("DELETE FROM orders WHERE order_id = %s", (order_id,))


def test_live_spend_ledger_accepts_only_bookings_and_topups():
    with rolled_back() as conn:
        account_id = _account(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute("""INSERT INTO live_spend (kind, account_id, amount, currency)
                                VALUES ('refund', %s, 10, 'USD')""", (account_id,))


# ---------------------------------------------------------------------------
# validation/ — live orders only, and the report table
# ---------------------------------------------------------------------------

def _load(relative, monkeypatch, name):
    monkeypatch.setenv("OBSERVE_ONLY", "1")
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_observe_considers_only_monitored_live_orders(monkeypatch):
    observe = _load("validation/observe.py", monkeypatch, "observe_under_test")
    live = {"monitoring": True, "raw": {"id": "ord_x"}, "duffel_mode": "live"}
    assert observe._is_observable(live)
    assert not observe._is_observable({**live, "duffel_mode": "test"})
    assert not observe._is_observable({**live, "monitoring": False})
    assert not observe._is_observable({**live, "raw": {}})


def test_observe_refuses_to_run_with_a_test_token(monkeypatch):
    observe = _load("validation/observe.py", monkeypatch, "observe_under_test")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DUFFEL_TOKEN", TEST_TOKEN)
    with pytest.raises(SystemExit, match="live orders only"):
        observe.main()


def test_report_table_groups_by_carrier_and_departure_bucket(monkeypatch):
    report = _load("validation/report.py", monkeypatch, "report_under_test")
    rows = [
        {"carrier": "BA", "days_to_departure": 20, "change_total_amount": Decimal("-12.00"), "gate": 9},
        {"carrier": "BA", "days_to_departure": 25, "change_total_amount": Decimal("40.00"), "gate": 8},
        {"carrier": "BA", "days_to_departure": 25, "change_total_amount": None, "gate": 6},
        {"carrier": "AA", "days_to_departure": 3, "change_total_amount": Decimal("125.00"), "gate": 8},
    ]
    summary = report.summarize(rows)
    got = [(s["carrier"], s["bucket"], s["quotes"], s["negative"], s["median_change_total"]) for s in summary]
    assert got == [
        ("AA", "0-7 days", 1, 0, Decimal("125.00")),
        ("BA", "15-30 days", 2, 1, Decimal("14.00")),
        ("ALL", "all", 3, 1, Decimal("40.00")),
    ]
    table = report.format_table(summary)
    for header in ("carrier", "days to departure", "quotes observed", "change_total < 0", "median change_total"):
        assert header in table.splitlines()[0]


# ---------------------------------------------------------------------------
# the staging banner
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("search_enabled,orders_enabled,shown,hidden", [
    ("true", "true", BANNER, SEARCH_BANNER),
    ("false", "true", BANNER, SEARCH_BANNER),
    ("true", "false", SEARCH_BANNER, BANNER),
])
def test_staging_banner_on_every_full_page_template(monkeypatch, client, search_enabled, orders_enabled,
                                                    shown, hidden):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DUFFEL_LIVE_SEARCH_ENABLED", search_enabled)
    monkeypatch.setenv("DUFFEL_LIVE_ORDERS_ENABLED", orders_enabled)
    for path in ("/login", "/", "/info"):  # base.html, landing.html, info.html
        page = client.get(path).get_data(as_text=True)
        assert shown in page, path
        assert hidden not in page, path


@pytest.mark.parametrize("app_env,search_enabled,orders_enabled", [
    ("staging", "false", "false"), ("production", "true", "true"), ("dev", "true", "true"),
])
def test_no_banner_otherwise(monkeypatch, client, app_env, search_enabled, orders_enabled):
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("DUFFEL_LIVE_SEARCH_ENABLED", search_enabled)
    monkeypatch.setenv("DUFFEL_LIVE_ORDERS_ENABLED", orders_enabled)
    for path in ("/login", "/", "/info"):
        page = client.get(path).get_data(as_text=True)
        assert BANNER not in page and SEARCH_BANNER not in page, path
