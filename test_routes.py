"""
Every route requires a signed-in session except the ones app.PUBLIC_ENDPOINTS
names — landing, login, signup, the Google auth callbacks, static files, and the
CRON_SECRET-authenticated reshop scheduler — and /healthz/env reports what a
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
    "cron_reshop",
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


def test_cron_refuses_without_the_secret(monkeypatch):
    """The scheduler is the one endpoint with no session behind it, so the
    bearer check is the whole of its authentication. An unset CRON_SECRET must
    refuse everything rather than leaving it open."""
    anonymous = app_module.app.test_client()

    monkeypatch.delenv("CRON_SECRET", raising=False)
    assert anonymous.get("/cron/reshop").status_code == 401
    assert anonymous.get("/cron/reshop",
                         headers={"Authorization": "Bearer anything"}).status_code == 401

    monkeypatch.setenv("CRON_SECRET", "s3cret")
    assert anonymous.get("/cron/reshop").status_code == 401
    assert anonymous.get("/cron/reshop",
                         headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert anonymous.get("/cron/reshop",
                         headers={"Authorization": "s3cret"}).status_code == 401


def test_cron_runs_with_the_secret_and_touches_nothing_when_idle(monkeypatch):
    """With a good token and an empty queue it must do nothing at all — no
    Duffel call, no execution — and say so."""
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    monkeypatch.setattr(app_module.db, "orders_due_a_check", lambda limit: [])

    def _no_cycles(*a, **k):
        raise AssertionError("an empty queue must not run a cycle")
    monkeypatch.setattr(app_module, "_run_cycle", _no_cycles)

    resp = app_module.app.test_client().get(
        "/cron/reshop", headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["checked"] == [] and body["reshop_decided"] == [] and body["errors"] == []


def test_cron_keeps_going_when_one_order_fails(monkeypatch):
    """A single bad order must not block the queue behind it: it is stamped as
    checked anyway so the cursor moves past it, and the run continues."""
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    monkeypatch.setattr(app_module.db, "orders_due_a_check",
                        lambda limit: [{"order_id": "ord_bad", "account_id": "a"},
                                       {"order_id": "ord_ok", "account_id": "a"}])
    stamped = []
    monkeypatch.setattr(app_module, "upsert_order",
                        lambda rec, account_id=None: stamped.append(rec["order_id"]))

    def _cycle(order_id, source, account_id=None):
        if order_id == "ord_bad":
            raise RuntimeError("duffel exploded")
        return None
    monkeypatch.setattr(app_module, "_run_cycle", _cycle)

    body = app_module.app.test_client().get(
        "/cron/reshop", headers={"Authorization": "Bearer s3cret"}).get_json()
    assert body["checked"] == ["ord_ok"]
    assert [e["order_id"] for e in body["errors"]] == ["ord_bad"]
    assert stamped == ["ord_bad"], "the failing order still has to move the cursor"


def _reshop_cron(monkeypatch, executed_calls):
    """A cron run where the single queued order decides RESHOP. Records any
    _execute_action call into `executed_calls` instead of touching Duffel."""
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    monkeypatch.setattr(app_module.db, "orders_due_a_check",
                        lambda limit: [{"order_id": "ord_1", "account_id": "acct_1"}])
    monkeypatch.setattr(app_module, "upsert_order", lambda rec, account_id=None: None)
    monkeypatch.setattr(app_module, "find_order",
                        lambda order_id, account_id=None: {"order_id": order_id})
    monkeypatch.setattr(app_module.db, "account_actor_email", lambda a: "owner@example.com")

    class _D:
        outcome = app_module.Outcome.RESHOP
    monkeypatch.setattr(app_module, "_run_cycle", lambda *a, **k: _D())

    def _exec(record, action, actor_email):
        executed_calls.append((record["order_id"], action, actor_email))
        return True, "exchanged"
    monkeypatch.setattr(app_module, "_execute_action", _exec)

    return app_module.app.test_client().get(
        "/cron/reshop", headers={"Authorization": "Bearer s3cret"}).get_json()


def test_autopilot_off_decides_but_never_executes(monkeypatch):
    """The flag is the kill switch. Off means a RESHOP decision is recorded and
    left for a human, with nothing reaching the execution path at all."""
    monkeypatch.delenv("RESHOP_AUTOPILOT_ENABLED", raising=False)
    calls = []
    body = _reshop_cron(monkeypatch, calls)
    assert body["autopilot"] is False
    assert body["reshop_decided"] == ["ord_1"]
    assert body["executed"] == []
    assert calls == [], "autopilot off must not reach _execute_action"


def test_autopilot_on_executes_as_the_account_owner(monkeypatch):
    """On, the cron exchanges unattended — through the same _execute_action the
    CONFIRM path uses, acting as the account owner so live_guard's allowlist
    still applies rather than being bypassed."""
    monkeypatch.setenv("RESHOP_AUTOPILOT_ENABLED", "true")
    calls = []
    body = _reshop_cron(monkeypatch, calls)
    assert body["autopilot"] is True
    assert body["reshop_decided"] == ["ord_1"]
    assert [e["order_id"] for e in body["executed"]] == ["ord_1"]
    assert calls == [("ord_1", "exchange", "owner@example.com")]


def test_execute_action_refuses_an_already_executed_order():
    """The guard has to live in _execute_action, not just the route: the cron
    calls it directly and a second exchange on a finished order must be
    impossible from either caller."""
    ok, message = app_module._execute_action(
        {"order_id": "ord_1", "source": "td_rebook", "executed": "done earlier"},
        "exchange", "owner@example.com")
    assert ok is False and "already been executed" in message

    ok, message = app_module._execute_action(
        {"order_id": "ord_1", "source": "manual"}, "exchange", "owner@example.com")
    assert ok is False and "TD-issued" in message


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
