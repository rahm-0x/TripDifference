"""
Staging live-spend controls. Only in play when the Duffel token is live and
live orders are on — duffel_http allows a live token only with APP_ENV=staging,
and refuses order calls unless DUFFEL_LIVE_ORDERS_ENABLED=true.

Every live spend — a ticket purchase in book(), an exchange top-up in
execute() — goes through reserve() before Duffel is asked to move money:

  1. the acting user's email is in STAGING_ALLOWED_EMAILS
  2. the spend is in USD (the caps are USD; nothing converts)
  3. both caps are configured
  4. amount <= STAGING_MAX_ORDER_USD
  5. today's live spend + amount <= STAGING_MAX_DAILY_USD, summed from the
     live_spend ledger under an advisory lock (so concurrent bookings can't
     both slip under the cap)
  6. the account is not a test account (config.TEST_ACCOUNT_NAME) — last, so
     a test account passing 1-5 is still refused

Any failure is written to audit_events (kind 'live_guard') and raised as
LiveSpendRejected. Passing records a 'reserved' live_spend row and returns
the duffel_http.SpendAuthorization that duffel_http.request() requires before
it will send a live payment. The caller settles it once Duffel confirms, or
releases it when Duffel provably refused.

The daily cap counts only this ledger, whose rows are exactly live bookings
and live exchange top-ups; test-mode orders never write to it. orders.paid
isn't summed because execute() overwrites it with the post-exchange total.
"""

from contextlib import nullcontext
from decimal import Decimal, InvalidOperation

import config
import db
import duffel_http

KINDS = ("booking", "exchange_topup")


class LiveSpendRejected(RuntimeError):
    def __init__(self, reason, detail):
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


def check(*, account_name, email, amount, currency, spent_today):
    """Raises LiveSpendRejected for the first rule that fails, else None.
    No I/O — the caps and allowlist are read from config at call time."""
    amount = Decimal(str(amount))
    email = (email or "").strip().lower()

    if email not in config.STAGING_ALLOWED_EMAILS:
        raise LiveSpendRejected("not_allowlisted", f"{email or 'this user'} is not in STAGING_ALLOWED_EMAILS")
    if (currency or "").upper() != "USD":
        raise LiveSpendRejected("non_usd", f"live spend must be USD to be capped; this is {currency or 'unknown'}")
    max_order, max_daily = config.STAGING_MAX_ORDER_USD, config.STAGING_MAX_DAILY_USD
    if max_order is None or max_daily is None:
        raise LiveSpendRejected("caps_not_configured",
                                "STAGING_MAX_ORDER_USD and STAGING_MAX_DAILY_USD must both be set")
    if amount > max_order:
        raise LiveSpendRejected("over_order_cap", f"{amount} USD is over the per-order cap of {max_order} USD")
    if spent_today + amount > max_daily:
        raise LiveSpendRejected("over_daily_cap",
                                f"{amount} USD would bring today's live spend to {spent_today + amount} USD, "
                                f"over the daily cap of {max_daily} USD")
    if account_name == config.TEST_ACCOUNT_NAME:
        raise LiveSpendRejected("test_account", "test accounts can never spend live money")


def _audit_rejection(exc, *, kind, account_id, email, amount, currency, reference, order_id):
    db.audit_append({
        "kind": "live_guard", "order_id": order_id or "", "source": kind,
        "outcome": "rejected", "reason": exc.reason, "detail": exc.detail,
        "currency": currency or "", "account_id": str(account_id), "email": email or "",
        "amount": str(amount), "reference": reference or "",
        "app_env": config.APP_ENV,
    })


def reserve(kind, *, account_id, email, amount, currency, reference="", order_id=None):
    """Checks and records one live spend. Returns a SpendAuthorization, or
    raises LiveSpendRejected after auditing the rejection."""
    if kind not in KINDS:
        raise ValueError(f"unknown live spend kind {kind!r}")
    account_name = db.account_name(account_id)
    context = dict(kind=kind, account_id=account_id, email=email, amount=amount,
                   currency=currency, reference=reference, order_id=order_id)
    try:
        try:
            value = Decimal(str(amount))
        except InvalidOperation:
            raise LiveSpendRejected("bad_amount", f"live spend amount {amount!r} is not a number")
        ledger_id = db.live_spend_reserve(
            kind=kind, account_id=account_id, amount=value, currency=(currency or "").upper(),
            reference=reference, order_id=order_id,
            check=lambda spent: check(account_name=account_name, email=email, amount=value,
                                      currency=currency, spent_today=spent))
    except LiveSpendRejected as exc:
        _audit_rejection(exc, **context)
        raise
    return duffel_http.SpendAuthorization(ledger_id=ledger_id, account_name=account_name or "",
                                          amount=value, currency=(currency or "").upper())


def authorized(authorization):
    """Context manager for the Duffel call a reservation covers (a no-op for None)."""
    return duffel_http.spend_authorized(authorization) if authorization else nullcontext()


def settle(authorization, *, order_id=None):
    if authorization:
        db.live_spend_settle(authorization.ledger_id, order_id=order_id)


def release(authorization):
    if authorization:
        db.live_spend_release(authorization.ledger_id)
