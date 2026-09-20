"""
Stripe: card-on-file, required to buy a ticket (book() refuses without one),
and the purchase charge itself under the merchant-of-record model. Eligibility
never consults it — that is a fact about the fare, not about the account.

Unlike duffel_http.py's raw-requests style, this uses Stripe's official
Python SDK — the supported integration path for a Vercel-Marketplace-
provisioned Stripe account, not a hand-rolled HTTP client.
"""

import os
from decimal import Decimal

import stripe

import config
import db
import duffel_http


def startup_check():
    """Run at app import. Stripe's mode has to match Duffel's, so a real card is
    never charged for a sandbox ticket and a real ticket is never bought without
    a real charge.

    Staging is the one deliberate exception: it may hold a live Duffel token, but
    its Stripe key must still be a test key — a staging deploy never charges a
    real card. DUFFEL_LIVE_ORDERS_ENABLED (default false) and live_guard's caps
    are what bound that mismatch.
    """
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()

    if config.APP_ENV == "staging":
        if not key.startswith("sk_test_"):
            raise RuntimeError("Refusing to start: APP_ENV=staging requires STRIPE_SECRET_KEY to start "
                               f"with 'sk_test_' (got '{key[:8]}...')" if key else
                               "Refusing to start: APP_ENV=staging requires STRIPE_SECRET_KEY "
                               "(an sk_test_ key), and it is not set")
        return

    # production and dev. 'missing'/'unrecognised' are duffel_http.startup_check's
    # to refuse, and it has already run by the time app.py reaches this one.
    duffel_mode = duffel_http.configured_token_mode()

    # Production is sandbox-only — duffel_http.check_token refuses a live token
    # anywhere but staging — so this is the direction that can bite today: a real
    # card charged for a ticket Duffel never really issued.
    if duffel_mode == "test" and key.startswith("sk_live_"):
        raise RuntimeError(
            "Refusing to start: DUFFEL_TOKEN is a sandbox token but STRIPE_SECRET_KEY is live "
            f"(APP_ENV={config.APP_ENV}) — a real card would be charged for a ticket that was "
            "never really issued.")

    # The other direction, which starts mattering the day production is allowed a
    # live token: a real ticket off TD's own Duffel balance, nothing real collected.
    if duffel_mode == "live" and not key.startswith("sk_live_"):
        raise RuntimeError(
            "Refusing to start: DUFFEL_TOKEN is a live token but STRIPE_SECRET_KEY is not live "
            f"(APP_ENV={config.APP_ENV}) — a real ticket would be bought with no real charge.")


class CardError(RuntimeError):
    """The company's card couldn't be authorized/captured/cancelled —
    declined, expired, no payment method on file, or a Stripe-side failure.
    Caught in app.book() the same way DuffelError already is."""


def _client():
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY not set — provision Stripe via "
                           "the Vercel Marketplace before calling billing.py")
    stripe.api_key = key
    return stripe


def _to_minor_units(amount):
    """Stripe wants an integer in the currency's smallest unit. This app's
    money is NUMERIC(12,2)/Decimal throughout, always 2 decimal places —
    every currency ever seen in responses/ (USD/GBP/EUR) — so a flat *100
    is correct here; a zero-decimal currency (JPY etc.) would need its own
    branch, and none has ever passed through this codebase."""
    return int((Decimal(str(amount)) * 100).to_integral_value())


def ensure_customer(account_id, email):
    """Idempotent — reuses the account's existing Stripe customer if one
    already exists rather than creating a duplicate on every onboarding hit."""
    existing = db.account_stripe_ids(account_id)
    if existing and existing.get("stripe_customer_id"):
        return existing["stripe_customer_id"]
    customer = _client().Customer.create(
        email=email, metadata={"account_id": str(account_id)})
    db.account_set_stripe_customer(account_id, customer.id)
    return customer.id


def create_setup_intent(customer_id):
    """Card details never touch this server — Stripe.js/Elements collects
    them client-side and confirms directly against Stripe using this
    intent's client_secret."""
    return _client().SetupIntent.create(customer=customer_id, payment_method_types=["card"])


def save_payment_method(account_id, customer_id, payment_method_id):
    """Called after Stripe.js confirms the SetupIntent and hands back a
    payment_method id. Attaches it to the customer, sets it default, and
    caches brand/last4/exp on the account row so card-gating and display
    never need a live Stripe call on every request.

    Uses pm.id throughout rather than the caller-supplied id after the
    first lookup: Stripe's reusable test tokens (pm_card_visa etc.) resolve
    to a fresh real PaymentMethod object on each API call, whose id differs
    from the token string — attach()/modify() need the resolved id or the
    default-payment-method call 400s ("customer does not have a payment
    method with that ID"). A real Stripe.js-issued id already equals its
    own pm.id, so this is a no-op change for the production path.
    """
    client = _client()
    pm = client.PaymentMethod.retrieve(payment_method_id)
    if pm.customer != customer_id:
        pm = client.PaymentMethod.attach(pm.id, customer=customer_id)
    client.Customer.modify(customer_id,
                           invoice_settings={"default_payment_method": pm.id})
    db.account_save_payment_method(
        account_id, stripe_payment_method_id=pm.id,
        brand=pm.card.brand, last4=pm.card.last4,
        exp_month=pm.card.exp_month, exp_year=pm.card.exp_year)


# ---------------------------------------------------------------------------
# the purchase charge — manual capture
# ---------------------------------------------------------------------------
#
# Sequence, and why it's this order and not charge-then-refund:
#   1. authorize_fare()       — PaymentIntent, capture_method='manual', off
#                                session, on the saved card. Puts a hold on
#                                the company's card; no money has moved yet.
#   2. (caller creates the Duffel order — unchanged, payments: balance;
#       TD's own Duffel balance still funds the actual purchase, a working
#       buffer topped up separately, not a float extended to the customer)
#   3a. capture_authorization() on Duffel success — the hold becomes a real
#       charge, now that a real ticket exists to charge for.
#   3b. cancel_authorization() on Duffel failure — the hold is released.
#       Never capture-then-refund: an authorization that never captures
#       never appears on the customer's statement; a charge-then-refund is
#       two lines and a support ticket for a ticket that was never issued.


def authorize_fare(account_id, *, amount, currency, idempotency_key):
    """Step 1. Raises CardError if there's no card on file or Stripe
    declines. idempotency_key should be derived from the offer_id — Duffel
    offer requests are already single-use (FINDINGS.md §4), so the same key
    naturally scopes to "this one purchase attempt," and a retried book()
    that reuses it gets back the same authorization instead of a second one.
    """
    ids = db.account_stripe_ids(account_id)
    if not ids or not ids.get("stripe_payment_method_id"):
        raise CardError("no payment method on file")
    client = _client()
    try:
        return client.PaymentIntent.create(
            amount=_to_minor_units(amount),
            currency=currency.lower(),
            customer=ids["stripe_customer_id"],
            payment_method=ids["stripe_payment_method_id"],
            off_session=True,
            confirm=True,
            capture_method="manual",
            idempotency_key=idempotency_key,
        )
    except stripe.error.CardError as exc:
        raise CardError(exc.user_message or str(exc)) from exc
    except stripe.error.StripeError as exc:
        raise CardError(str(exc)) from exc


def capture_authorization(payment_intent_id):
    """Step 3a — the ticket exists; the hold becomes a real charge."""
    return _client().PaymentIntent.capture(payment_intent_id)


def cancel_authorization(payment_intent_id):
    """Step 3b — Duffel failed; release the hold. Never captured, so
    there's nothing for the customer to see or dispute."""
    return _client().PaymentIntent.cancel(payment_intent_id)
