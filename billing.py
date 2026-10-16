"""
Stripe: card-on-file, required to buy a ticket (book() refuses without one),
and the purchase charge itself under the merchant-of-record model. Eligibility
never consults it — that is a fact about the fare, not about the account.

Unlike duffel_http.py's raw-requests style, this uses Stripe's official
Python SDK — the supported integration path for a Vercel-Marketplace-
provisioned Stripe account, not a hand-rolled HTTP client.
"""

import json
from decimal import Decimal

import stripe

import config
import db
import duffel_http


def startup_check():
    """Run at app import. Stripe's mode has to match Duffel's, so a real card is
    never charged for a sandbox ticket and a real ticket is never bought without
    a real charge.

    Staging is the one deliberate exception: it may hold a live Duffel token over
    a Stripe test key, so a staging deploy charges no real card unless it is told
    to. DUFFEL_LIVE_ORDERS_ENABLED (default false) and live_guard's caps are what
    bound that mismatch.

    STRIPE_LIVE_ENABLED (default false, staging only) is that instruction. Stripe
    then runs on STRIPE_LIVE_SECRET_KEY / STRIPE_LIVE_PUBLISHABLE_KEY, which must
    be a live pair over a live Duffel token. Who is charged and how much is still
    live_guard's: book() reserves there — allowlist, caps — before the card is
    authorized.
    """
    if config.STRIPE_LIVE_ENABLED and config.APP_ENV != "staging":
        raise RuntimeError("Refusing to start: STRIPE_LIVE_ENABLED=true is only allowed with APP_ENV=staging "
                           f"(APP_ENV={config.APP_ENV}).")
    key = config.STRIPE_SECRET_KEY

    if config.APP_ENV == "staging" and config.STRIPE_LIVE_ENABLED:
        if not key.startswith("sk_live_"):
            raise RuntimeError("Refusing to start: STRIPE_LIVE_ENABLED=true requires STRIPE_LIVE_SECRET_KEY "
                               "to be an sk_live_ key" + ("" if key else ", and it is not set"))
        if not config.STRIPE_PUBLISHABLE_KEY.startswith("pk_live_"):
            raise RuntimeError("Refusing to start: STRIPE_LIVE_ENABLED=true requires STRIPE_LIVE_PUBLISHABLE_KEY "
                               "to be the pk_live_ key that goes with it — a card saved through a test "
                               "publishable key cannot be charged with a live secret key.")
        if duffel_http.configured_token_mode() != "live":
            raise RuntimeError("Refusing to start: STRIPE_LIVE_ENABLED=true over a Duffel token that is not "
                               "live — a real card would be charged for a ticket that was never really issued.")
        return

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
    key = config.STRIPE_SECRET_KEY
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


def charges_enabled():
    """Whether the Stripe account behind a live key can take a charge yet, for
    /healthz/env: True or False from Stripe, None when it could not be asked.
    Not asked in test mode, where a charge goes through either way."""
    if not config.STRIPE_LIVEMODE:
        return None
    try:
        return bool(_client().Account.retrieve().charges_enabled)
    except Exception:
        return None


def _card_to_charge(account_id):
    """The account's customer and card, for a charge. Raises CardError when
    there is no card on file — and, under a live key, for one of the test
    suite's accounts, which never reaches a real card whatever is saved on it
    (live_guard refuses the same accounts a live booking)."""
    if config.STRIPE_LIVEMODE and db.account_name(account_id) == config.TEST_ACCOUNT_NAME:
        raise CardError("test accounts can never be charged for real")
    ids = db.account_stripe_ids(account_id)
    if not ids or not ids.get("stripe_payment_method_id"):
        raise CardError("no payment method on file")
    return ids


def ensure_customer(account_id, email):
    """Idempotent — reuses the account's existing Stripe customer if one
    already exists rather than creating a duplicate on every onboarding hit.

    "Existing" means under the mode this deployment's key is in
    (db.account_stripe_ids): an account that saved a test card and is then
    served with a live key has no customer there, so one is created, and
    saving it clears the card that belonged to the other mode."""
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
    intent's client_secret.

    Which methods the intent accepts is left to the set managed in Stripe's
    dashboard rather than named here: newer Stripe API versions reject
    `payment_method_types` outright, which is what took the card page down.
    Redirect-based methods are excluded — the page offers a card field only,
    and nothing here could send a customer away and bring them back."""
    return _client().SetupIntent.create(
        customer=customer_id,
        automatic_payment_methods={"enabled": True, "allow_redirects": "never"})


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
    ids = _card_to_charge(account_id)
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


# ---------------------------------------------------------------------------
# passing a recovery on — a partial refund of the purchase charge
# ---------------------------------------------------------------------------

def refund_to_card(payment_intent_id, *, amount, idempotency_key):
    """Refund `amount` of a captured fare to the card that paid it. This is
    how a cash recovery reaches the company: the airline's refund lands in
    TD's Duffel balance (which paid for the ticket), never on their card.

    idempotency_key should be derived from the savings event, so a retry of
    the same recovery returns the same refund instead of paying it twice.
    Stripe itself refuses a refund larger than what was captured.
    """
    try:
        return _client().Refund.create(payment_intent=payment_intent_id,
                                       amount=_to_minor_units(amount),
                                       idempotency_key=idempotency_key)
    except stripe.error.StripeError as exc:
        raise CardError(str(exc)) from exc


# ---------------------------------------------------------------------------
# the commission — charged once a rebooking has delivered its saving
# ---------------------------------------------------------------------------

def charge_commission(account_id, *, amount, currency, idempotency_key, description, metadata=None):
    """Charge TD's fee on a rebooking to the account's card on file, in one
    step (no hold: there is nothing left that could fail and need it released).

    idempotency_key should be derived from the savings event, so a retry of
    the same rebooking returns the same charge instead of taking the fee twice.
    Raises CardError if there is no card on file or Stripe declines.
    """
    ids = _card_to_charge(account_id)
    try:
        return _client().PaymentIntent.create(
            amount=_to_minor_units(amount),
            currency=currency.lower(),
            customer=ids["stripe_customer_id"],
            payment_method=ids["stripe_payment_method_id"],
            off_session=True,
            confirm=True,
            description=description,
            metadata=metadata or {},
            idempotency_key=idempotency_key,
        )
    except stripe.error.CardError as exc:
        raise CardError(exc.user_message or str(exc)) from exc
    except stripe.error.StripeError as exc:
        raise CardError(str(exc)) from exc


# ---------------------------------------------------------------------------
# scouts — Stripe Connect Express accounts and monthly transfers
# ---------------------------------------------------------------------------
#
# A scout is paid by a transfer from TD's Stripe balance to their own Express
# account. Stripe does the onboarding, identity checks, bank details and the
# 1099-NEC; TD only ever holds the connected account's id
# (scouts.stripe_connect_account_id). Connect and 1099 reporting have to be
# switched on in the Stripe dashboard first — nothing here works around that.


class PayoutError(RuntimeError):
    """A transfer to a scout was refused or could not be made."""


class WebhookRefused(RuntimeError):
    """A request to /webhooks/stripe that is not a verifiable Stripe event."""


def _field(obj, key):
    """obj[key] for a webhook's plain dict and a StripeObject alike (which is
    no longer a dict subclass), None when absent."""
    try:
        return obj[key]
    except (KeyError, TypeError):
        return None


def connect_create_account(email, scout_id):
    """A new Express account for the scout, asking for the transfers capability."""
    return _client().Account.create(
        type="express", email=email,
        capabilities={"transfers": {"requested": True}},
        metadata={"scout_id": str(scout_id)})


def connect_onboarding_url(connect_account_id, *, refresh_url, return_url):
    """Stripe's hosted onboarding. Single-use and short-lived: Stripe sends the
    scout to refresh_url when it has expired, which makes a fresh one."""
    return _client().AccountLink.create(account=connect_account_id, refresh_url=refresh_url,
                                        return_url=return_url, type="account_onboarding").url


def connect_dashboard_url(connect_account_id):
    """A one-time sign-in to the scout's Express dashboard (bank, tax forms)."""
    return _client().Account.create_login_link(connect_account_id).url


def connect_can_receive(account):
    """Whether a connected account can be paid: Stripe says payouts are enabled
    and the transfers capability is active."""
    capabilities = _field(account, "capabilities") or {}
    return bool(_field(account, "payouts_enabled")) and _field(capabilities, "transfers") == "active"


def connect_status(connect_account_id):
    return connect_can_receive(_client().Account.retrieve(connect_account_id))


def _transfer_group(payout_id):
    return f"scout-payout-{payout_id}"


def transfer_to_scout(connect_account_id, *, amount, currency, payout_id, attempt):
    """One scout payout as one transfer. The transfer group names the payout,
    so a run that stopped before recording it can ask Stripe whether it was
    made (scout_transfer_for); the idempotency key names the attempt, so a
    retry of the same attempt never pays twice.

    Test mode only, for now: under a live key this refuses. Real money to a
    real scout is switched on deliberately, not by a staging deploy that
    happens to hold a live key."""
    if config.STRIPE_LIVEMODE:
        raise PayoutError("scout payouts run in Stripe test mode only; live transfers are not switched on")
    try:
        return _client().Transfer.create(
            amount=_to_minor_units(amount), currency=currency.lower(),
            destination=connect_account_id, transfer_group=_transfer_group(payout_id),
            metadata={"scout_payout_id": str(payout_id)},
            idempotency_key=f"scout-payout-{payout_id}-{attempt}")
    except stripe.error.StripeError as exc:
        raise PayoutError(str(exc)) from exc


def scout_transfer_for(payout_id):
    """The transfer made for a payout, or None when there is none."""
    found = _client().Transfer.list(transfer_group=_transfer_group(payout_id), limit=1)
    return found.data[0] if found.data else None


def webhook_event(payload, signature):
    """The event, as plain JSON, once its signature verifies against one of
    config.STRIPE_WEBHOOK_SECRETS (TD's own endpoint, and the Connect one for
    scouts' accounts). Raises WebhookRefused otherwise — including when no
    secret is configured, so an unconfigured endpoint accepts nothing."""
    secrets = config.STRIPE_WEBHOOK_SECRETS
    if not secrets:
        raise WebhookRefused("no webhook signing secret is configured")
    try:
        # As text: verify_header signs "%d.%s" % (timestamp, payload), so bytes
        # would be signed as "b'...'" and no real event would ever verify.
        payload = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    except UnicodeDecodeError as exc:
        raise WebhookRefused("payload is not UTF-8") from exc
    for secret in secrets:
        try:
            stripe.WebhookSignature.verify_header(payload, signature or "", secret, tolerance=300)
        except stripe.error.SignatureVerificationError:
            continue
        try:
            return json.loads(payload)
        except ValueError as exc:
            raise WebhookRefused("payload is not JSON") from exc
    raise WebhookRefused("signature does not verify")


# ---------------------------------------------------------------------------
# /admin/analytics — what Stripe itself says
# ---------------------------------------------------------------------------

def stripe_summary(days=30, max_charges=1000):
    """Stripe's view, for the admin analytics page: the balance, and the last
    `days` of charges by currency (succeeded, failed, refunded, disputed).
    Raises if Stripe can't be asked; the page shows why instead. Reads at most
    `max_charges` charges and says so when it stopped there."""
    import time
    client = _client()
    balance = client.Balance.retrieve()
    since = int(time.time()) - days * 86400
    by_currency, read, truncated = {}, 0, False
    for charge in client.Charge.list(created={"gte": since}, limit=100).auto_paging_iter():
        if read >= max_charges:
            truncated = True
            break
        read += 1
        row = by_currency.setdefault(charge.currency.upper(), {
            "succeeded": 0, "gross": Decimal("0"), "failed": 0, "refunded": Decimal("0"), "disputed": 0})
        if charge.status == "succeeded":
            row["succeeded"] += 1
            row["gross"] += Decimal(charge.amount) / 100
        elif charge.status == "failed":
            row["failed"] += 1
        row["refunded"] += Decimal(charge.amount_refunded or 0) / 100
        if charge.disputed:
            row["disputed"] += 1
    return {
        "livemode": config.STRIPE_LIVEMODE,
        "available": {b.currency.upper(): Decimal(b.amount) / 100 for b in balance.available},
        "pending": {b.currency.upper(): Decimal(b.amount) / 100 for b in balance.pending},
        "charges": by_currency, "read": read, "truncated": truncated, "days": days,
    }
