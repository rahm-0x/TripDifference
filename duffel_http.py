"""
Shared Duffel HTTP layer.

duffel.py (the CLI) keeps its own inlined copy on purpose — it prints and exits.
This one raises, so it can be used from the engine and the web app.

429 handling follows what Duffel actually documents: `ratelimit-reset` is an
RFC 2616 *date string*, not a seconds delta. `Retry-After` is checked first only
because proxies sometimes inject it; Duffel itself does not send it.

Environment guard (check_environment, check_token). A duffel_test_ token is
allowed everywhere. The two live flags — DUFFEL_LIVE_SEARCH_ENABLED and
DUFFEL_LIVE_ORDERS_ENABLED — may only be true with APP_ENV=staging, and a
duffel_live_ token is only allowed there with at least one of them on. app.py
calls startup_check() at import, so production with either flag or a live
token fails the process at startup rather than on a request. duffel.py and
duffel_reshop_test.py keep their own test-only guards, untouched.

Per-request guard, live token only (_check_live_request):
  - search requests (POST /air/offer_requests, GET /air/offer_requests…,
    GET /air/offers…) need DUFFEL_LIVE_SEARCH_ENABLED, and a live offer request
    additionally needs a single-use SearchAuthorization from live_search, which
    is how every live search is counted against the monthly budget;
  - everything else — order create, order change, cancel — needs
    DUFFEL_LIVE_ORDERS_ENABLED (default false) and is refused with a clear error
    otherwise; with it on, a request carrying a payment still needs a
    SpendAuthorization from live_guard (right account, never a test account,
    same currency, amount within what was authorized).
Nothing reaches Duffel with a live token by calling request() around these.
"""

import json
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

import config
import paths

BASE = "https://api.duffel.com"
RESPONSES = paths.DATA_DIR / "responses"

LIVE_FLAGS = ("DUFFEL_LIVE_SEARCH_ENABLED", "DUFFEL_LIVE_ORDERS_ENABLED")


class DuffelError(RuntimeError):
    """A non-2xx from Duffel, with the parsed error list attached."""

    def __init__(self, status, errors):
        self.status = status
        self.errors = errors or []
        parts = [
            f"[{e.get('type')}/{e.get('code')}] {e.get('title')}: {e.get('message')}"
            for e in self.errors
        ]
        super().__init__(f"HTTP {status} — " + ("; ".join(parts) or "no error body"))

    @property
    def codes(self):
        return [e.get("code") for e in self.errors]


class LiveRequestRefused(RuntimeError):
    """A live-token request this deployment's flags or authorizations don't allow."""


class LiveSpendBlocked(LiveRequestRefused):
    """A live-token request that would spend money had no matching authorization."""


# ---------------------------------------------------------------------------
# environment and token guard
# ---------------------------------------------------------------------------

def token_mode(tok):
    """'test' or 'live'. Anything else is refused outright — an unrecognised
    token can't be classified, so it can't be allowed."""
    if tok.startswith("duffel_test_"):
        return "test"
    if tok.startswith("duffel_live_"):
        return "live"
    raise RuntimeError(f"Refusing to run: DUFFEL_TOKEN is neither a duffel_test_ nor a duffel_live_ "
                       f"token (got '{tok[:14]}...')")


def check_environment(app_env, search_enabled, orders_enabled):
    """The live flags only mean something on staging; set anywhere else they
    are a misconfiguration, and production stays sandbox-only."""
    if app_env == "staging":
        return
    on = [name for name, value in zip(LIVE_FLAGS, (search_enabled, orders_enabled)) if value]
    if on:
        raise RuntimeError(f"Refusing to run: {' and '.join(n + '=true' for n in on)} is only allowed with "
                           f"APP_ENV=staging (APP_ENV={app_env}) — production stays sandbox-only.")


def check_token(tok, app_env, search_enabled, orders_enabled):
    """The token's mode, or raises if this environment may not use it."""
    check_environment(app_env, search_enabled, orders_enabled)
    mode = token_mode(tok)
    if mode == "live" and not (app_env == "staging" and (search_enabled or orders_enabled)):
        raise RuntimeError(
            "Refusing to run: DUFFEL_TOKEN is a LIVE token, which is only allowed with APP_ENV=staging and "
            f"DUFFEL_LIVE_SEARCH_ENABLED or DUFFEL_LIVE_ORDERS_ENABLED set to true (APP_ENV={app_env}, "
            f"DUFFEL_LIVE_SEARCH_ENABLED={'true' if search_enabled else 'false'}, "
            f"DUFFEL_LIVE_ORDERS_ENABLED={'true' if orders_enabled else 'false'}).")
    return mode


def _flags():
    return config.APP_ENV, config.DUFFEL_LIVE_SEARCH_ENABLED, config.DUFFEL_LIVE_ORDERS_ENABLED


def _raw_token():
    load_dotenv()
    return os.environ.get("DUFFEL_TOKEN", "").strip()


def token():
    """The configured token, checked against the current environment."""
    tok = _raw_token()
    if not tok:
        raise RuntimeError("DUFFEL_TOKEN not set — copy .env.example to .env")
    check_token(tok, *_flags())
    return tok


def mode():
    """'test' or 'live' for the configured token (raises exactly as token() does)."""
    return token_mode(token())


def startup_check():
    """Run at app import. A live flag outside staging, or a token this
    environment may not use, fails here at startup. A missing token is still
    left to request time, as before."""
    app_env, search_enabled, orders_enabled = _flags()
    check_environment(app_env, search_enabled, orders_enabled)
    tok = _raw_token()
    if tok:
        check_token(tok, app_env, search_enabled, orders_enabled)


def live_orders_refusal():
    return ("Live Duffel orders are disabled on this deployment (DUFFEL_LIVE_ORDERS_ENABLED is not 'true'): "
            "searches can be live, but no ticket can be booked, changed or cancelled with the live token.")


# ---------------------------------------------------------------------------
# request classification and authorizations (live token only)
# ---------------------------------------------------------------------------

def is_search_request(method, path):
    method, path = method.upper(), path.split("?")[0].rstrip("/")
    if method == "POST":
        return path == "/air/offer_requests"
    if method == "GET":
        return any(path == p or path.startswith(p + "/") for p in ("/air/offer_requests", "/air/offers"))
    return False


class SearchAuthorization:
    """Issued by live_search once a live offer request is counted in
    live_search_log. Good for exactly one offer request."""

    def __init__(self, log_id):
        self.log_id = log_id
        self.used = False


@dataclass(frozen=True)
class SpendAuthorization:
    """Issued by live_guard.reserve() once every check has passed and the
    spend is recorded in live_spend."""
    ledger_id: int
    account_name: str
    amount: Decimal
    currency: str


_SEARCH_AUTHORIZATION = ContextVar("duffel_live_search_authorization", default=None)
_AUTHORIZATION = ContextVar("duffel_live_spend_authorization", default=None)


@contextmanager
def search_authorized(authorization):
    """Scope in which request() may send the one live offer request
    `authorization` covers. None authorizes nothing (test mode needs none)."""
    handle = _SEARCH_AUTHORIZATION.set(authorization)
    try:
        yield
    finally:
        _SEARCH_AUTHORIZATION.reset(handle)


@contextmanager
def spend_authorized(authorization):
    """Scope in which request() may make the payment `authorization` covers.
    None is allowed and authorizes nothing (test mode needs no authorization)."""
    handle = _AUTHORIZATION.set(authorization)
    try:
        yield
    finally:
        _AUTHORIZATION.reset(handle)


def _payment_in(body):
    """(amount, currency) of the payment a request body carries, else (None, None).
    Covers /air/orders' `payments` list and a change confirm's `payment`."""
    data = (body or {}).get("data") or {}
    payments = data.get("payments") or ([data["payment"]] if data.get("payment") else [])
    if not payments:
        return None, None
    try:
        amount = sum(Decimal(str(p.get("amount"))) for p in payments)
    except (InvalidOperation, TypeError):
        raise LiveSpendBlocked("live payment amount could not be read — refusing to send it")
    currencies = {str(p.get("currency") or "").upper() for p in payments}
    return amount, (currencies.pop() if len(currencies) == 1 else "")


def _check_live_spend(method, path, body):
    if method.upper() != "POST":
        return
    amount, currency = _payment_in(body)
    if amount is None or amount <= 0:
        return
    auth = _AUTHORIZATION.get()
    if auth is None:
        raise LiveSpendBlocked(f"live payment of {amount} {currency} to {path} has no spend authorization")
    if auth.account_name == config.TEST_ACCOUNT_NAME:
        raise LiveSpendBlocked("test accounts can never spend live money")
    if not currency or currency != auth.currency.upper():
        raise LiveSpendBlocked(f"live payment currency {currency or '?'} does not match the "
                               f"authorized {auth.currency}")
    if amount > auth.amount:
        raise LiveSpendBlocked(f"live payment of {amount} exceeds the authorized {auth.amount}")


def _check_live_request(method, path, body):
    if is_search_request(method, path):
        if not config.DUFFEL_LIVE_SEARCH_ENABLED:
            raise LiveRequestRefused(f"Live Duffel search is disabled on this deployment "
                                     f"(DUFFEL_LIVE_SEARCH_ENABLED is not 'true'): refused {method} {path}.")
        if method.upper() == "POST":
            auth = _SEARCH_AUTHORIZATION.get()
            if auth is None or auth.used:
                raise LiveRequestRefused(f"live offer request has no unused search-budget authorization "
                                         f"(go through live_search.offer_request): refused {method} {path}.")
            auth.used = True
        return
    if not config.DUFFEL_LIVE_ORDERS_ENABLED:
        raise LiveRequestRefused(f"{live_orders_refusal()} Refused {method} {path}.")
    _check_live_spend(method, path, body)


# ---------------------------------------------------------------------------
# the request itself
# ---------------------------------------------------------------------------

def _rate_limit_wait(resp):
    if resp.headers.get("Retry-After"):
        try:
            return max(1.0, float(resp.headers["Retry-After"]))
        except ValueError:
            pass
    reset = resp.headers.get("ratelimit-reset")
    if reset:
        try:
            delta = (parsedate_to_datetime(reset) - datetime.now(timezone.utc)).total_seconds()
            return max(1.0, min(delta, 120.0))
        except (TypeError, ValueError):
            pass
    return 5.0


def dump(label, payload):
    if not paths.DUMP_RESPONSES:
        return
    RESPONSES.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    (RESPONSES / f"{stamp}-{label}.json").write_text(json.dumps(payload, indent=2, sort_keys=True))


def request(method, path, *, body=None, params=None, label=None):
    """One request. Retries a 429 once, then gives up. Raises DuffelError on non-2xx."""
    tok = token()
    if token_mode(tok) == "live":
        _check_live_request(method, path, body)
    headers = {
        "Authorization": f"Bearer {tok}",
        "Duffel-Version": "v2",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    for attempt in (1, 2):
        resp = requests.request(method, f"{BASE}{path}", headers=headers,
                                json=body, params=params, timeout=60)
        if resp.status_code == 429 and attempt == 1:
            time.sleep(_rate_limit_wait(resp))
            continue

        try:
            payload = resp.json()
        except ValueError:
            raise DuffelError(resp.status_code, [{"title": "non-JSON body", "message": resp.text[:500]}])

        dump(label or path.strip("/").replace("/", "_"), payload)

        if not resp.ok:
            raise DuffelError(resp.status_code, payload.get("errors"))
        return payload["data"]

    raise DuffelError(429, [{"title": "rate limited", "message": "429 twice in a row"}])
