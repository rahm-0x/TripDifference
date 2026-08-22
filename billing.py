"""
Stripe: card-on-file for card-gating (eligibility.assess's has_card), and
later off-session commission charges once a SavingsEvent completes.

Unlike duffel_http.py's raw-requests style, this uses Stripe's official
Python SDK — the supported integration path for a Vercel-Marketplace-
provisioned Stripe account, not a hand-rolled HTTP client.
"""

import os

import stripe

import db


def _client():
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY not set — provision Stripe via "
                           "the Vercel Marketplace before calling billing.py")
    stripe.api_key = key
    return stripe


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
