"""
Test-database guard. Tests run against the staging database — the same one
the staging deployment books live orders into — never production.

At import — before pytest imports any test module, and so before app.py or
db.py is imported — this loads .env.local, .env and .env.staging.local (none
with override), refuses to run unless STAGING_POSTGRES_URL is set and reaches
a different database than POSTGRES_URL / POSTGRES_URL_NON_POOLING /
DATABASE_URL (by identity, see db_identity.py), then repoints POSTGRES_URL and
POSTGRES_URL_NON_POOLING at the STAGING_ values. db.py opens its pool lazily
from POSTGRES_URL, and every other load_dotenv() in the codebase runs without
override=True, so nothing later can restore the production URL.

Sharing a database with live staging orders is safe only because every test
deletes exactly the rows it created (captured ids, test-only carrier codes)
and every account it creates is named db.TEST_ACCOUNT_NAME, which the app's
sign-in lookups and the validation harness exclude. Session start warns, but
does not block, when live orders are present.

STAGING_* live in .env.staging.local, not .env.local, because
`vercel env pull .env.local` rewrites .env.local.
"""

import os
from urllib.parse import unquote, urlsplit

import psycopg
import pytest
from dotenv import load_dotenv

from db_identity import same_database

load_dotenv(".env.local")
load_dotenv(".env")
load_dotenv(".env.staging.local")

_PRODUCTION_URLS = {name: os.environ.get(name, "")
                    for name in ("POSTGRES_URL", "POSTGRES_URL_NON_POOLING", "DATABASE_URL")}


def _point_at_staging_database():
    """Repoints the env at the staging database and returns None, or returns
    why it refused (leaving the env untouched)."""
    staging_url = os.environ.get("STAGING_POSTGRES_URL", "").strip()
    if not staging_url:
        return ("STAGING_POSTGRES_URL is not set. The suite writes real rows; set STAGING_POSTGRES_URL and "
                "STAGING_POSTGRES_URL_NON_POOLING in .env.staging.local.")
    for name, url in _PRODUCTION_URLS.items():
        if same_database(staging_url, url):
            return f"STAGING_POSTGRES_URL reaches the same database as {name}."

    staging_direct = os.environ.get("STAGING_POSTGRES_URL_NON_POOLING", "").strip()
    if not staging_direct:
        return "STAGING_POSTGRES_URL_NON_POOLING is not set."
    for name, url in _PRODUCTION_URLS.items():
        if same_database(staging_direct, url):
            return f"STAGING_POSTGRES_URL_NON_POOLING reaches the same database as {name}."
    if not same_database(staging_direct, staging_url):
        return "STAGING_POSTGRES_URL_NON_POOLING reaches a different database than STAGING_POSTGRES_URL."

    os.environ["POSTGRES_URL"] = staging_url
    os.environ["POSTGRES_URL_NON_POOLING"] = staging_direct
    os.environ.pop("DATABASE_URL", None)
    return None


_REFUSAL = _point_at_staging_database()

# The suite runs against the staging database, so it runs as staging:
# db.startup_check() refuses APP_ENV=production on the staging project when
# app.py is imported. Live Duffel stays off unless a test turns it on itself —
# the live flags are cleared, and every Duffel call is mocked, but book() and
# live_search read the configured token's mode (duffel_http.mode) unmocked, so
# anything other than a test token (none at all, or a live one from the shell)
# is replaced by a placeholder test token. A real duffel_test_ token is kept.
if not _REFUSAL:
    os.environ["APP_ENV"] = "staging"
for _flag in ("DUFFEL_LIVE_SEARCH_ENABLED", "DUFFEL_LIVE_ORDERS_ENABLED"):
    os.environ.pop(_flag, None)
if not os.environ.get("DUFFEL_TOKEN", "").strip().startswith("duffel_test_"):
    os.environ["DUFFEL_TOKEN"] = "duffel_test_placeholder_for_pytest"


def pytest_configure(config):
    # Raised here rather than at import so pytest reports it as a usage error,
    # not an ImportError. Still runs before collection imports any test module.
    if _REFUSAL:
        raise pytest.UsageError(f"Refusing to run tests: {_REFUSAL}")


def _live_order_count():
    """Live-ticket orders currently in staging, or None when orders has no
    duffel_mode column yet (nothing can be live before that migration)."""
    with psycopg.connect(os.environ["POSTGRES_URL_NON_POOLING"], connect_timeout=15) as conn:
        conn.read_only = True
        has_column = conn.execute(
            """SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'orders' AND column_name = 'duffel_mode'""").fetchone()
        if not has_column:
            return None
        return conn.execute("SELECT count(*) FROM orders WHERE duffel_mode = 'live'").fetchone()[0]


def pytest_sessionstart(session):
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")

    def warn(msg):
        if reporter:
            reporter.write_line(f"WARNING: {msg}", yellow=True, bold=True)
        else:
            print(f"WARNING: {msg}")

    try:
        live = _live_order_count()
    except psycopg.Error as exc:
        warn(f"could not check staging for live orders: {str(exc).strip().splitlines()[0]}")
        return
    if live:
        warn(f"staging holds {live} live order(s) (duffel_mode = 'live'). Tests delete only the rows "
             "they create, but this database has real tickets in it.")


@pytest.fixture(scope="session", autouse=True)
def assert_connected_to_staging_database():
    """Checks the connection db.py really opened, not just the env var: host,
    port, database and user (the user is what separates Supabase projects that
    share a pooler host) must all match STAGING_POSTGRES_URL."""
    import db

    expected = urlsplit(os.environ["STAGING_POSTGRES_URL"])
    want = {"host": (expected.hostname or "").lower(), "port": expected.port or 5432,
            "dbname": expected.path.lstrip("/") or "postgres", "user": unquote(expected.username or "")}
    with db.pool().connection() as conn:
        info = conn.info
        live = {"host": (info.host or "").lower(), "port": int(info.port),
                "dbname": info.dbname, "user": info.user}

    problems = [f"{k}: connected {live[k]!r}, expected {want[k]!r}" for k in want if live[k] != want[k]]
    for name, url in _PRODUCTION_URLS.items():
        if same_database(db._url(), url):
            problems.append(f"db.py's URL reaches the same database as production {name}")
    if problems:
        pytest.exit("NOT CONNECTED TO THE STAGING DATABASE — aborting before any test runs.\n  "
                    + "\n  ".join(problems), returncode=3)
    yield
