"""
Every route requires a signed-in session except the ones app.PUBLIC_ENDPOINTS
names — landing, login, signup, the Google auth callbacks, static files, and the
signature-authenticated Resend webhook — and /healthz/env reports what a
deployment is running as without exposing a secret.

The route-map walk sends one anonymous request per rule and method, with
placeholder values for URL parameters. app._require_login answers before any
view runs, so nothing here reaches a handler, Duffel, Stripe, or a database
write.

    .venv/bin/python -m pytest test_routes.py -v
"""

import os
import re
from urllib.parse import urlsplit

import app as app_module
import config
from test_money_path import acct, client, logged_in  # noqa: F401

LIVE_TOKEN = "duffel_live_" + "0" * 32

# The allowlist, restated here on purpose: making a route public means editing
# both app.PUBLIC_ENDPOINTS and this set.
EXPECTED_PUBLIC = {
    "index", "login", "signup",
    "google_auth_start", "google_auth_callback",
    "static", "logo",
}


def _concrete(rule):
    return re.sub(r"<(?:[^:<>]+:)?([^<>]+)>", r"pytest-\1", rule.rule)


def test_public_endpoints_are_exactly_the_allowlist():
    assert app_module.PUBLIC_ENDPOINTS == EXPECTED_PUBLIC
    endpoints = {rule.endpoint for rule in app_module.app.url_map.iter_rules()}
    assert EXPECTED_PUBLIC <= endpoints, f"allowlisted endpoints that no longer exist: {EXPECTED_PUBLIC - endpoints}"


def test_every_route_off_the_allowlist_requires_login():
    anonymous = app_module.app.test_client()
    checked, unprotected = 0, []
    for rule in app_module.app.url_map.iter_rules():
        if rule.endpoint in EXPECTED_PUBLIC:
            continue
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            resp = anonymous.open(_concrete(rule), method=method)
            checked += 1
            if not (resp.status_code == 302 and "/login" in resp.headers.get("Location", "")):
                unprotected.append(f"{method} {rule.rule} (endpoint {rule.endpoint}) -> {resp.status_code}")
    # A floor, so a broken walk that checks nothing cannot pass. The real
    # assertion is `unprotected` below. Was >40 before the email-import
    # routes were removed took this from 45 to 40.
    assert checked >= 35, "the walk should cover the whole app"
    assert not unprotected, "reachable without signing in:\n  " + "\n  ".join(unprotected)


def test_the_routes_that_were_open_now_require_login():
    anonymous = app_module.app.test_client()
    for method, path in (("GET", "/search?origin=LAS&destination=LAX&date=2026-12-01"), ("GET", "/eligibility"),
                         ("GET", "/info"), ("GET", "/healthz/env"), ("POST", "/book"),
                         ("POST", "/orders/ord_x/execute/cancel")):
        resp = anonymous.open(path, method=method)
        assert resp.status_code == 302 and "/login" in resp.headers["Location"], (method, path)


def test_healthz_env_reports_identity_without_secrets(logged_in):
    client, _ = logged_in
    body = client.get("/healthz/env").get_json()
    assert set(body) == {"app_env", "vercel_git_commit_sha", "duffel_token_mode", "db_project_ref",
                         "duffel_live_search_enabled", "duffel_live_orders_enabled"}
    # the suite runs as dev, against the staging database, with a test token
    assert body["app_env"] == "dev"
    assert body["db_project_ref"] == config.STAGING_SUPABASE_REF
    assert body["duffel_token_mode"] == "test"
    assert body["duffel_live_search_enabled"] is False and body["duffel_live_orders_enabled"] is False

    raw = client.get("/healthz/env").get_data(as_text=True)
    secrets = [os.environ[name] for name in ("DUFFEL_TOKEN", "POSTGRES_URL", "POSTGRES_URL_NON_POOLING",
                                             "STRIPE_SECRET_KEY") if os.environ.get(name)]
    password = urlsplit(os.environ["POSTGRES_URL"]).password
    for value in secrets + ([password] if password else []):
        assert value not in raw
    for fragment in ("postgres://", "postgresql://", "pooler.supabase.com", "sk_test_", "sk_live_"):
        assert fragment not in raw, fragment
    assert not re.search(r"duffel_(test|live)_[A-Za-z0-9]{8}", raw), "token-shaped value in the response"


def test_healthz_env_on_a_staging_live_search_deploy(monkeypatch, logged_in):
    client, _ = logged_in
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DUFFEL_LIVE_SEARCH_ENABLED", "true")
    monkeypatch.setenv("DUFFEL_TOKEN", LIVE_TOKEN)
    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "abc1234")
    body = client.get("/healthz/env").get_json()
    assert body == {"app_env": "staging", "vercel_git_commit_sha": "abc1234", "duffel_token_mode": "live",
                    "db_project_ref": config.STAGING_SUPABASE_REF, "duffel_live_search_enabled": True,
                    "duffel_live_orders_enabled": False}
