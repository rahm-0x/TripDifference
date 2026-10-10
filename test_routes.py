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
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlsplit

import app as app_module
import config
import db
from test_money_path import (CSRF, acct, client, fake_order, logged_in,  # noqa: F401
                             make_real_order, new_account)

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

    def _exec(record, action, actor_email, unattended=False):
        executed_calls.append((record["order_id"], action, actor_email, unattended))
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
    # unattended: nobody looked at the quote, so it is held to a refund
    assert calls == [("ord_1", "exchange", "owner@example.com", True)]


def _monitored(account_id, **fields):
    order = make_real_order(account_id)
    return db.upsert_order({"order_id": order["order_id"], "monitoring": True, **fields}, account_id)


def test_recheck_all_covers_every_monitored_order_on_the_account_and_no_other(logged_in, monkeypatch):
    """One click, every monitored order — never-checked first, then the oldest
    check — and nothing that is paused, finished, or another account's."""
    client, account = logged_in
    mine = account["account_id"]
    checked_before = _monitored(mine, last_checked_at="2026-09-20T08:00:00+00:00")
    never_checked = _monitored(mine)
    make_real_order(mine)                                           # paused
    _monitored(mine, executed="exchange confirmed earlier")         # finished
    other = new_account("other")
    _monitored(other["account_id"])

    cycled = []
    monkeypatch.setattr(app_module, "_run_cycle",
                        lambda order_id, source, account_id=None: cycled.append((order_id, str(account_id))))
    try:
        resp = client.post("/orders/recheck", data={"_csrf": CSRF})
    finally:
        db.q("DELETE FROM accounts WHERE id = %s", (other["account_id"],))

    assert resp.status_code == 302 and resp.headers["Location"].endswith("/orders")
    assert cycled == [(never_checked["order_id"], str(mine)), (checked_before["order_id"], str(mine))]

    page = client.get("/orders").get_data(as_text=True)
    assert "Rechecked 2 fares. No exchange would pay right now." in page
    assert "Recheck all fares" in page and "Run live cycle" not in page


class _Reshop:
    outcome = app_module.Outcome.RESHOP


def test_recheck_all_with_autopilot_off_decides_and_leaves_the_exchange_for_confirm(logged_in, monkeypatch):
    client, account = logged_in
    _monitored(account["account_id"])
    monkeypatch.delenv("RESHOP_AUTOPILOT_ENABLED", raising=False)
    monkeypatch.setattr(app_module, "_run_cycle", lambda *a, **k: _Reshop())

    def _no_execution(*a, **k):
        raise AssertionError("autopilot off must not reach _execute_action")
    monkeypatch.setattr(app_module, "_execute_action", _no_execution)

    client.post("/orders/recheck", data={"_csrf": CSRF})
    assert "Rechecked 1 fare. 1 can be exchanged for a refund" in client.get("/orders").get_data(as_text=True)


def test_recheck_all_with_autopilot_on_rebooks_as_the_person_who_pressed_it(logged_in, monkeypatch):
    """The same switch the scheduler obeys. On, a refund that clears the floor
    is exchanged in the same request — unattended, so held to a refund."""
    client, account = logged_in
    order = _monitored(account["account_id"])
    monkeypatch.setenv("RESHOP_AUTOPILOT_ENABLED", "true")
    monkeypatch.setattr(app_module, "_run_cycle", lambda *a, **k: _Reshop())
    calls = []

    def _exec(record, action, actor_email, unattended=False):
        calls.append((record["order_id"], action, actor_email, unattended))
        return True, "exchange confirmed"
    monkeypatch.setattr(app_module, "_execute_action", _exec)

    client.post("/orders/recheck", data={"_csrf": CSRF})
    assert calls == [(order["order_id"], "exchange", account["email"], True)]
    page = client.get("/orders").get_data(as_text=True)
    assert "Rechecked 1 fare. 1 rebooked automatically." in page and "can be exchanged" not in page


def test_recheck_all_reports_an_automatic_rebooking_that_did_not_go_through(logged_in, monkeypatch):
    client, account = logged_in
    _monitored(account["account_id"])
    monkeypatch.setenv("RESHOP_AUTOPILOT_ENABLED", "true")
    monkeypatch.setattr(app_module, "_run_cycle", lambda *a, **k: _Reshop())
    monkeypatch.setattr(app_module, "_execute_action",
                        lambda *a, **k: (False, "automatic rebooking stopped: the airline now quotes 40.00"))

    client.post("/orders/recheck", data={"_csrf": CSRF})
    page = client.get("/orders").get_data(as_text=True)
    assert "1 automatic rebooking did not go through: automatic rebooking stopped" in page
    assert "still to go" not in page and "could not be checked" not in page


def test_no_automatic_exchange_is_started_late_in_a_run(logged_in, monkeypatch):
    """An exchange cut off by the function's time limit could land at the
    airline with nothing recorded here. Past the cut-off the decision is left
    standing for a later run or a person."""
    client, account = logged_in
    _monitored(account["account_id"])
    monkeypatch.setenv("RESHOP_AUTOPILOT_ENABLED", "true")
    clock = [0.0]
    monkeypatch.setattr(app_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def _slow_cycle(*a, **k):
        clock[0] += app_module.CRON_EXECUTE_BY_SECONDS + 1
        return _Reshop()
    monkeypatch.setattr(app_module, "_run_cycle", _slow_cycle)

    def _no_execution(*a, **k):
        raise AssertionError("too late in the run to start an exchange")
    monkeypatch.setattr(app_module, "_execute_action", _no_execution)

    client.post("/orders/recheck", data={"_csrf": CSRF})
    assert "1 can be exchanged for a refund" in client.get("/orders").get_data(as_text=True)


def test_recheck_all_stops_on_its_budget_and_says_what_is_left(logged_in, monkeypatch):
    """A list longer than one request can cover is worked through in turns: a
    failing order is stamped so it goes to the back instead of blocking the
    queue, and whatever wasn't reached is reported."""
    client, account = logged_in
    mine = account["account_id"]
    failing, fine, unreached = (_monitored(mine, last_checked_at=f"2026-09-2{day}T08:00:00+00:00")
                                for day in (1, 2, 3))

    # Each cycle "takes" 30s against the 45s budget, so the third never starts.
    clock = [0.0]
    monkeypatch.setattr(app_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def _cycle(order_id, source, account_id=None):
        clock[0] += 30
        if order_id == failing["order_id"]:
            raise RuntimeError("duffel exploded")
    monkeypatch.setattr(app_module, "_run_cycle", _cycle)

    client.post("/orders/recheck", data={"_csrf": CSRF})
    page = client.get("/orders").get_data(as_text=True)
    assert "Rechecked 1 fare." in page
    assert "1 fare could not be checked: duffel exploded" in page
    assert "1 fare still to go" in page

    queue = [row["order_id"] for row in db.account_orders_due_a_check(mine)]
    assert queue == [fine["order_id"], unreached["order_id"], failing["order_id"]]


def test_recheck_all_with_nothing_monitored_runs_nothing(logged_in, monkeypatch):
    client, account = logged_in
    make_real_order(account["account_id"])                          # paused

    def _no_cycles(*a, **k):
        raise AssertionError("nothing monitored must not run a cycle")
    monkeypatch.setattr(app_module, "_run_cycle", _no_cycles)

    client.post("/orders/recheck", data={"_csrf": CSRF})
    assert "nothing to recheck" in client.get("/orders").get_data(as_text=True)


def test_recheck_all_returns_to_the_page_it_was_pressed_on_and_never_off_site(logged_in, monkeypatch):
    client, account = logged_in
    _monitored(account["account_id"])
    monkeypatch.setattr(app_module, "_run_cycle", lambda *a, **k: None)
    for wanted, expected in ((None, "/orders"), ("/trips", "/trips"), ("/trips/ord_x", "/trips/ord_x"),
                             ("https://evil.example/", "/trips"), ("//evil.example/", "/trips")):
        data = {"_csrf": CSRF, **({"next": wanted} if wanted else {})}
        where = urlsplit(client.post("/orders/recheck", data=data).headers["Location"])
        assert where.path == expected and where.netloc in ("", "localhost"), wanted


def test_trip_page_offers_the_recheck_and_the_exchange_without_the_ops_console(logged_in):
    """Everything a booked ticket needs is on the customer's own pages: recheck
    from the trip, and review the exchange once the airline's own quote is a
    refund — and only then."""
    client, account = logged_in
    raw = fake_order(change_allowed=True, carrier_iata="T1")
    exchange = f"/orders/{raw['id']}/confirm/exchange"

    def trip_page(last_decision):
        db.upsert_order({"order_id": raw["id"], "source": "td_rebook", "fare_type": "cash",
                         "paid": "219.00", "currency": "USD", "carrier": "Test Airways",
                         "route": "LHR-JFK", "itinerary": "ZZ123", "monitoring": True,
                         "raw": raw, "last_decision": last_decision}, account["account_id"])
        return client.get(f"/trips/{raw['id']}").get_data(as_text=True)

    never_checked = trip_page(None)
    assert "Recheck all fares" in never_checked and exchange not in never_checked
    assert f"/orders/{raw['id']}/confirm/cancel" in never_checked, "cancelling starts from the trip too"

    skip = {"ts": "2026-10-08T09:00:00+00:00", "source": "duffel", "outcome": "skip",
            "reason": "change_total_not_negative", "detail": "Airline quoted 40.00 USD to exchange.",
            "change_total": "40.00", "change_offer_id": "oco_test", "execution": "not_applicable"}
    assert exchange not in trip_page(skip), "a quote that costs money is not an exchange to offer"

    reshop = {**skip, "outcome": "reshop", "reason": "profitable_drop", "change_total": "-45.00",
              "execution": "awaiting_confirmation", "execution_detail": "Awaiting confirmation."}
    assert exchange in trip_page(reshop)
    assert exchange not in trip_page({**reshop, "source": "simulated"}), "a simulated price never reaches Duffel"

    assert "Recheck all fares" in client.get("/trips").get_data(as_text=True)


def test_the_app_rebooks_at_the_same_floor_as_the_engine_default():
    """app.DEFAULT_POLICY is what every cycle actually runs with; it and the
    engine's own default are two literals that must not drift."""
    assert app_module.DEFAULT_POLICY.min_saving == app_module.ReshopPolicy().min_saving == Decimal("10.00")


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


def test_healthz_env_reports_identity_without_secrets(logged_in, monkeypatch):
    client, account = logged_in
    monkeypatch.setenv("STAGING_MAX_ORDER_USD", "250.50")
    monkeypatch.delenv("STAGING_MAX_DAILY_USD", raising=False)
    monkeypatch.setenv("STAGING_ALLOWED_EMAILS", f"someone.else@example.com, {account['email'].upper()}")
    monkeypatch.delenv("RESHOP_AUTOPILOT_ENABLED", raising=False)
    monkeypatch.setenv("CRON_SECRET", "s3cret-cron-value")
    body = client.get("/healthz/env").get_json()
    assert set(body) == {"app_env", "vercel_git_commit_sha", "duffel_token_mode", "db_project_ref",
                         "duffel_live_search_enabled", "duffel_live_orders_enabled",
                         "staging_max_order_usd", "staging_max_daily_usd", "viewer_on_live_allowlist",
                         "reshop_autopilot_enabled", "cron_secret_set"}
    # the suite runs as dev, against the staging database, with a test token
    assert body["app_env"] == "dev"
    assert body["db_project_ref"] == config.STAGING_SUPABASE_REF
    assert body["duffel_token_mode"] == "test"
    assert body["duffel_live_search_enabled"] is False and body["duffel_live_orders_enabled"] is False
    # the caps as numbers (an unset one is null), the allowlist and the cron
    # secret only as yes or no
    assert body["staging_max_order_usd"] == "250.50" and body["staging_max_daily_usd"] is None
    assert body["viewer_on_live_allowlist"] is True
    assert body["reshop_autopilot_enabled"] is False and body["cron_secret_set"] is True

    raw = client.get("/healthz/env").get_data(as_text=True)
    assert "someone.else@example.com" not in raw and "s3cret-cron-value" not in raw
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
    for name in ("STAGING_MAX_ORDER_USD", "STAGING_MAX_DAILY_USD", "STAGING_ALLOWED_EMAILS", "CRON_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RESHOP_AUTOPILOT_ENABLED", "true")
    body = client.get("/healthz/env").get_json()
    assert body == {"app_env": "staging", "vercel_git_commit_sha": "abc1234", "duffel_token_mode": "live",
                    "db_project_ref": config.STAGING_SUPABASE_REF, "duffel_live_search_enabled": True,
                    "duffel_live_orders_enabled": False,
                    # nothing configured: no caps, nobody allowlisted, no cron secret
                    "staging_max_order_usd": None, "staging_max_daily_usd": None,
                    "viewer_on_live_allowlist": False,
                    "reshop_autopilot_enabled": True, "cron_secret_set": False}
