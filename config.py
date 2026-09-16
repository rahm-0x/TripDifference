"""
Environment model.

APP_ENV is one of dev | staging | production. Missing or anything else means
production — the most restrictive reading, so a misconfigured deploy fails
closed rather than open.

The names below are read from the environment on every access (module
__getattr__), never cached at import, so a test that changes the environment
and a long-lived process both see the current value. Use them as
`config.APP_ENV`, not `from config import APP_ENV` (which would freeze one
read).

  APP_ENV                       "dev" | "staging" | "production"
  DUFFEL_LIVE_SEARCH_ENABLED    True only when exactly "true". Staging only: lets a
                                live Duffel token make offer requests and fetch offers.
  DUFFEL_LIVE_ORDERS_ENABLED    True only when exactly "true" (default false). Staging
                                only: lets a live token create, change or cancel orders.
  STAGING_MAX_MONTHLY_SEARCHES  int >= 0; 1400 when unset, 0 (no live searches) when
                                set to anything that isn't a whole number
  STAGING_MAX_ORDER_USD         Decimal > 0, or None when unset/invalid
  STAGING_MAX_DAILY_USD         Decimal > 0, or None when unset/invalid
  STAGING_ALLOWED_EMAILS        frozenset of lowercased emails (comma-separated);
                                empty means nobody
"""

import os
from decimal import Decimal, InvalidOperation

APP_ENVS = ("dev", "staging", "production")

# Every account the test suite creates carries this name (tests share the
# staging database with live staging orders). Sign-in, validation/, and the
# live-spend guard all treat it as untouchable.
TEST_ACCOUNT_NAME = "__pytest__"

# The staging database's Supabase project. db.startup_check() requires
# APP_ENV=staging to run against it, and APP_ENV=production never to.
STAGING_SUPABASE_REF = "bcqzwnoifwkuimrrdysy"

DEFAULT_MAX_MONTHLY_SEARCHES = 1400


def _app_env():
    value = os.environ.get("APP_ENV", "").strip()
    return value if value in APP_ENVS else "production"


def _flag(name):
    return os.environ.get(name, "").strip() == "true"


def _monthly_searches():
    raw = os.environ.get("STAGING_MAX_MONTHLY_SEARCHES")
    if raw is None or not raw.strip():
        return DEFAULT_MAX_MONTHLY_SEARCHES
    raw = raw.strip()
    return int(raw) if raw.isdigit() else 0


def _usd(name):
    raw = os.environ.get(name, "").strip()
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def _allowed_emails():
    raw = os.environ.get("STAGING_ALLOWED_EMAILS", "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def startup_check():
    """Run at app import. A Vercel Preview deployment must say what it is:
    with VERCEL_ENV=preview, APP_ENV has to be set explicitly to one of
    APP_ENVS. Anywhere else a missing APP_ENV still means production."""
    if os.environ.get("VERCEL_ENV", "").strip() != "preview":
        return
    raw = os.environ.get("APP_ENV", "").strip()
    if not raw:
        raise RuntimeError("Refusing to start: this is a Vercel Preview deployment (VERCEL_ENV=preview) and "
                           "APP_ENV is not set — set APP_ENV for this branch rather than falling back to "
                           "production mode.")
    if raw not in APP_ENVS:
        raise RuntimeError(f"Refusing to start: APP_ENV={raw!r} is not one of {', '.join(APP_ENVS)} "
                           "(VERCEL_ENV=preview).")


_READERS = {
    "APP_ENV": _app_env,
    "DUFFEL_LIVE_SEARCH_ENABLED": lambda: _flag("DUFFEL_LIVE_SEARCH_ENABLED"),
    "DUFFEL_LIVE_ORDERS_ENABLED": lambda: _flag("DUFFEL_LIVE_ORDERS_ENABLED"),
    "STAGING_MAX_MONTHLY_SEARCHES": _monthly_searches,
    "STAGING_MAX_ORDER_USD": lambda: _usd("STAGING_MAX_ORDER_USD"),
    "STAGING_MAX_DAILY_USD": lambda: _usd("STAGING_MAX_DAILY_USD"),
    "STAGING_ALLOWED_EMAILS": _allowed_emails,
}


def __getattr__(name):
    try:
        return _READERS[name]()
    except KeyError:
        raise AttributeError(f"module 'config' has no attribute {name!r}") from None
