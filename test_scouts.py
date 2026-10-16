"""
Scouts and required referral codes (migration 038, referrals.py): no code, no
account; the /r/<code> lock is enforced by the server; attribution is fixed at
signup; a scout earns only on commission TD has actually collected; reversals,
payouts, the Stripe webhook, the scheduler, and who may reach which page.

    .venv/bin/python -m pytest test_scouts.py -v

Stripe is never reached: the billing calls each test is about are patched, and
conftest makes every other one fail. The database is real (staging), so every
test deletes what it created through the `ledger` fixture — in the order the
ledger's RESTRICT foreign keys require — and every account is renamed to
db.TEST_ACCOUNT_NAME as soon as it exists. Signup tests send a random
X-Forwarded-For so the code throttle counts per test, not per run.
"""

import json
import random
import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
import pytest
import stripe
from markupsafe import escape

import app as app_module
import billing
import db
import referrals
from test_money_path import CSRF, client, make_real_order, new_account  # noqa: F401


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

class Ledger:
    def __init__(self):
        self.accounts, self.scouts, self.ips = [], [], []

    def ip(self):
        address = f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"
        self.ips.append(address)
        return address

    def adopt_signup(self, email):
        """An account a signup created: renamed for the suite, and deleted after."""
        row = db.q("SELECT account_id FROM users WHERE email = %s", (email,), fetch="one")
        if row:
            db.q("UPDATE accounts SET name = %s WHERE id = %s", (db.TEST_ACCOUNT_NAME, row["account_id"]))
            self.accounts.append(row["account_id"])
        return row and row["account_id"]

    def purge(self):
        scouts, accounts = self.scouts or [-1], self.accounts or ["00000000-0000-0000-0000-000000000000"]
        db.q("DELETE FROM scout_earnings WHERE scout_id = ANY(%s) OR account_id = ANY(%s::uuid[])", (scouts, accounts))
        db.q("DELETE FROM scout_payouts WHERE scout_id = ANY(%s)", (scouts,))
        own = [r["account_id"] for r in db.q("""SELECT u.account_id FROM scouts s JOIN users u ON u.id = s.user_id
                                                 WHERE s.id = ANY(%s)""", (scouts,), fetch="all")]
        referred = [a for a in accounts if a not in own]
        db.q("DELETE FROM accounts WHERE id = ANY(%s::uuid[])", (referred or ["00000000-0000-0000-0000-000000000000"],))
        db.q("DELETE FROM accounts WHERE scout_id = ANY(%s)", (scouts,))  # anything else they referred
        db.q("DELETE FROM referral_codes WHERE scout_id = ANY(%s)", (scouts,))
        db.q("DELETE FROM scouts WHERE id = ANY(%s)", (scouts,))
        db.q("DELETE FROM accounts WHERE id = ANY(%s::uuid[])", (own or ["00000000-0000-0000-0000-000000000000"],))
        if self.ips:
            db.q("DELETE FROM referral_code_failures WHERE ip = ANY(%s::inet[])", (self.ips,))


@pytest.fixture
def ledger():
    led = Ledger()
    yield led
    led.purge()


def make_scout(ledger, *, status="approved", email=None, rate=None):
    """A scout on a test account of its own, approved with a code unless told
    otherwise."""
    user = new_account("scout")
    ledger.accounts.append(user["account_id"])
    if email:
        db.q("UPDATE users SET email = %s, given_name = 'Sam' WHERE id = %s", (email, user["id"]))
    else:
        db.q("UPDATE users SET given_name = 'Sam' WHERE id = %s", (user["id"],))
    scout = db.scout_apply(user["id"], company="Test Scouting", sourcing_plan="tests")
    ledger.scouts.append(scout["id"])
    code = None
    if status != "applied":
        code = db.scout_approve(scout["id"], user["id"])
        if rate is not None:
            db.scout_set_rate(scout["id"], Decimal(rate), user["id"])
        if status in ("suspended", "terminated"):
            db.scout_transition(scout["id"], "suspend" if status == "suspended" else "terminate", user["id"])
    return SimpleNamespace(user=user, id=scout["id"], code=code)


def referred_account(ledger, scout):
    """A test account attributed to the scout's code, as signup would make it."""
    email = f"biz-{uuid.uuid4().hex[:10]}@example.org"
    user = db.create_account(email, password_hash="x", referral=db.referral_code_lookup(scout.code))
    db.q("UPDATE accounts SET name = %s WHERE id = %s", (db.TEST_ACCOUNT_NAME, user["account_id"]))
    ledger.accounts.append(user["account_id"])
    return user


def charged_saving(account_id, *, saved="200.00", charge_id=None, action="exchange"):
    """A rebooking whose 25% commission was charged: a completed savings event
    with commission_charged_at set, as _charge_commission leaves one."""
    order = make_real_order(account_id, paid="500.00")
    event = db.savings_event_create(
        order["order_id"], action=action, execution_attempt_id=None, old_amount="500.00",
        new_amount=str(Decimal("500.00") - Decimal(saved)), realized_savings=saved, currency="USD",
        delivery_type="refund_to_card", delivery_detail="", commission_rate=Decimal("0.25"))
    if charge_id:
        db.savings_event_commission_charged(event["id"], charge_id)
    return order, event


def earnings_for(event_id):
    return db.q("SELECT * FROM scout_earnings WHERE savings_event_id = %s ORDER BY id", (event_id,), fetch="all")


def signup(client, ledger, *, code=None, email=None, ip=None, password="correct horse battery"):
    email = email or f"new-{uuid.uuid4().hex[:10]}@example.org"
    form = {"_csrf": CSRF, "email": email, "password": password, "confirm": password}
    if code is not None:
        form["referral_code"] = code
    with client.session_transaction() as sess:   # a successful signup signs in, which clears it
        sess["_csrf"] = CSRF
    resp = client.post("/signup", data=form, headers={"X-Forwarded-For": ip or ledger.ip()})
    return resp, email, ledger.adopt_signup(email)


def shows(resp, message):
    """The page carries `message` as rendered (escaped) text."""
    return str(escape(message)) in resp.get_data(as_text=True)


def sign_in_as(client, user):
    with client.session_transaction() as sess:
        sess["sid"] = db.start_session(user["id"])
        sess["_csrf"] = CSRF


# ---------------------------------------------------------------------------
# migration 038
# ---------------------------------------------------------------------------

def test_every_account_carries_a_referral_code_and_old_ones_are_legacy():
    assert db.q("SELECT count(*) AS n FROM accounts WHERE referral_code_id IS NULL", fetch="one")["n"] == 0
    legacy = db.referral_code_lookup("LEGACY")
    assert legacy["internal"] and legacy["is_system"] and legacy["default_rate"] == 0


def test_a_new_account_cannot_be_inserted_without_a_code():
    with db.pool().connection() as conn, conn.transaction(force_rollback=True):
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute("INSERT INTO accounts (name) VALUES (%s)", (db.TEST_ACCOUNT_NAME,))


def test_attribution_is_fixed_except_through_the_audited_override(ledger):
    scout = make_scout(ledger)
    other = make_scout(ledger)
    account = referred_account(ledger, scout)
    with pytest.raises(psycopg.errors.RaiseException, match="fixed at signup"):
        db.q("UPDATE accounts SET scout_rate = 0.5 WHERE id = %s", (account["account_id"],))

    before = db.account_attribution_override(account["account_id"], db.referral_code_lookup(other.code),
                                             reason="scout merger", staff_user_id=scout.user["id"])
    assert before == scout.code
    row = db.q("SELECT scout_id, scout_rate FROM accounts WHERE id = %s", (account["account_id"],), fetch="one")
    assert row["scout_id"] == other.id and row["scout_rate"] == Decimal("0.1000")
    audit = db.q("""SELECT detail, payload FROM audit_events WHERE kind = 'referral' AND account_id = %s
                     AND outcome = 'override'""", (account["account_id"],), fetch="one")
    assert "scout merger" in audit["detail"] and audit["payload"]["from_code"] == scout.code


def test_scout_id_cannot_disagree_with_the_code(ledger):
    scout = make_scout(ledger)
    other = make_scout(ledger)
    code = db.referral_code_lookup(scout.code)
    with db.pool().connection() as conn, conn.transaction(force_rollback=True):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("""INSERT INTO accounts (name, referral_code_id, scout_id, scout_rate, referred_at)
                            VALUES (%s, %s, %s, 0.1, now())""", (db.TEST_ACCOUNT_NAME, code["code_id"], other.id))


# ---------------------------------------------------------------------------
# signup: no code, no account
# ---------------------------------------------------------------------------

def test_signup_without_a_code_is_refused(client, ledger):
    resp, email, account = signup(client, ledger, code="")
    assert resp.status_code == 400 and account is None
    assert shows(resp, referrals.MISSING)


def test_signup_with_legacy_or_any_internal_code_is_refused(client, ledger):
    for code in ("LEGACY", "legacy", "SCOUT-APPLICANT"):
        resp, _, account = signup(client, ledger, code=code)
        assert resp.status_code == 400 and account is None, code
        assert shows(resp, referrals.INVALID)


def test_signup_refuses_unknown_inactive_and_unapproved_codes(client, ledger):
    resp, _, account = signup(client, ledger, code="SCOUT-NOPE9")
    assert resp.status_code == 400 and account is None

    suspended = make_scout(ledger, status="suspended")
    resp, _, account = signup(client, ledger, code=suspended.code)
    assert resp.status_code == 400 and account is None and shows(resp, referrals.INACTIVE)

    off = make_scout(ledger)
    db.referral_code_deactivate(db.referral_code_lookup(off.code)["code_id"], off.user["id"])
    resp, _, account = signup(client, ledger, code=off.code)
    assert resp.status_code == 400 and account is None


def test_signup_with_a_valid_code_records_attribution_and_audit(client, ledger):
    scout = make_scout(ledger)
    resp, email, account_id = signup(client, ledger, code=scout.code.lower())
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/onboarding/profile")
    row = db.q("SELECT * FROM accounts WHERE id = %s", (account_id,), fetch="one")
    assert row["scout_id"] == scout.id and row["scout_rate"] == Decimal("0.1000") and row["referred_at"]
    assert row["referral_code_id"] == db.referral_code_lookup(scout.code)["code_id"]
    audit = db.q("SELECT detail FROM audit_events WHERE kind = 'referral' AND account_id = %s",
                 (account_id,), fetch="one")
    assert audit["detail"] == f"Referred by scout {scout.id} via {scout.code}"


def test_a_rate_change_applies_to_future_signups_only(client, ledger):
    scout = make_scout(ledger)
    _, _, first = signup(client, ledger, code=scout.code)
    db.scout_set_rate(scout.id, Decimal("0.15"), scout.user["id"])
    _, _, second = signup(client, ledger, code=scout.code)
    rates = {r["id"]: r["scout_rate"] for r in db.q("SELECT id, scout_rate FROM accounts WHERE id = ANY(%s::uuid[])",
                                                     ([first, second],), fetch="all")}
    assert rates[first] == Decimal("0.1000") and rates[second] == Decimal("0.1500")


def test_code_guessing_is_throttled(client, ledger):
    ip = ledger.ip()
    for _ in range(referrals.MAX_FAILURES):
        db.referral_failure_record(ip, "SCOUT-GUESS")
    scout = make_scout(ledger)
    resp, _, account = signup(client, ledger, code=scout.code, ip=ip)
    assert resp.status_code == 429 and account is None


# ---------------------------------------------------------------------------
# the /r/<code> link and its lock
# ---------------------------------------------------------------------------

def test_referral_link_sets_a_signed_httponly_cookie_and_goes_to_signup(client, ledger):
    scout = make_scout(ledger)
    resp = client.get(f"/r/{scout.code}", headers={"X-Forwarded-For": ledger.ip()})
    assert resp.status_code == 302 and f"/signup?ref={scout.code}" in resp.headers["Location"]
    cookie = resp.headers["Set-Cookie"]
    assert cookie.startswith(f"{referrals.COOKIE}=") and "HttpOnly" in cookie and "Max-Age=7776000" in cookie

    page = client.get(f"/signup?ref={scout.code}").get_data(as_text=True)
    assert "readonly" in page and "Referred by Sam" in page


def test_an_inactive_link_shows_signup_with_the_link_refused(client, ledger):
    resp = client.get("/r/SCOUT-ZZZZ9", headers={"X-Forwarded-For": ledger.ip()})
    assert resp.status_code == 404 and shows(resp, referrals.INACTIVE)
    assert referrals.COOKIE not in resp.headers.get("Set-Cookie", "")


def test_the_lock_is_enforced_by_the_server_not_the_readonly_field(client, ledger):
    """Cookie present, form edited to another valid code: refused, and no
    account is attributed to either code."""
    locked = make_scout(ledger)
    other = make_scout(ledger)
    client.get(f"/r/{locked.code}", headers={"X-Forwarded-For": ledger.ip()})
    resp, _, account = signup(client, ledger, code=other.code)
    assert resp.status_code == 400 and account is None
    assert shows(resp, referrals.LOCKED.format(code=locked.code))

    # the field as rendered (the locked code) goes through, attributed to it
    resp, _, account = signup(client, ledger, code=locked.code)
    assert resp.status_code == 302
    assert db.q("SELECT scout_id FROM accounts WHERE id = %s", (account,), fetch="one")["scout_id"] == locked.id


def test_a_forged_referral_cookie_is_refused_and_dropped(client, ledger):
    scout = make_scout(ledger)
    client.set_cookie(referrals.COOKIE, "SCOUT-FORGED.not-a-signature")
    resp, _, account = signup(client, ledger, code=scout.code)
    assert resp.status_code == 400 and account is None
    assert shows(resp, referrals.UNVERIFIED)
    assert f"{referrals.COOKIE}=;" in resp.headers.get("Set-Cookie", "")


def test_self_referral_by_company_domain_is_refused_and_free_mail_is_flagged(client, ledger):
    company = make_scout(ledger, email=f"scout-{uuid.uuid4().hex[:8]}@acme-scouting.test")
    resp, _, account = signup(client, ledger, code=company.code,
                              email=f"colleague-{uuid.uuid4().hex[:8]}@acme-scouting.test")
    assert resp.status_code == 400 and account is None and shows(resp, "scout's own company")

    freemail = make_scout(ledger, email=f"scout-{uuid.uuid4().hex[:8]}@gmail.com")
    resp, _, account = signup(client, ledger, code=freemail.code, email=f"biz-{uuid.uuid4().hex[:8]}@gmail.com")
    assert resp.status_code == 302
    flag = db.q("SELECT referral_flag FROM accounts WHERE id = %s", (account,), fetch="one")["referral_flag"]
    assert "gmail.com" in flag
    assert any(f["id"] == account for f in db.referrals_flagged())


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------

def _google_callback(client, ledger, email):
    with client.session_transaction() as sess:
        sess["_google_pkce_verifier"] = "verifier"
    with patch("supabase_auth.exchange_code", return_value={"id": f"sb-{uuid.uuid4().hex}", "email": email}):
        resp = client.get("/auth/google/callback?code=abc", headers={"X-Forwarded-For": ledger.ip()})
    return resp, ledger.adopt_signup(email)


def test_google_cannot_create_an_account_without_a_code(client, ledger):
    resp, account = _google_callback(client, ledger, f"g-{uuid.uuid4().hex[:10]}@example.org")
    assert resp.status_code == 400 and account is None
    assert shows(resp, referrals.MISSING)


def test_google_signup_checks_the_code_first_then_attributes_the_account(client, ledger):
    scout = make_scout(ledger)
    resp = client.post("/signup/google", data={"_csrf": CSRF, "referral_code": "LEGACY"},
                       headers={"X-Forwarded-For": ledger.ip()})
    assert resp.status_code == 400

    resp = client.post("/signup/google", data={"_csrf": CSRF, "referral_code": scout.code},
                       headers={"X-Forwarded-For": ledger.ip()})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/auth/google/start")
    resp, account = _google_callback(client, ledger, f"g-{uuid.uuid4().hex[:10]}@example.org")
    assert resp.status_code == 302
    assert db.q("SELECT scout_id FROM accounts WHERE id = %s", (account,), fetch="one")["scout_id"] == scout.id


# ---------------------------------------------------------------------------
# earnings: only on commission actually collected
# ---------------------------------------------------------------------------

def test_a_collected_commission_on_200_saved_earns_the_scout_5_pending(ledger):
    """The worked example: 200 saved → 50 commission at 25% → 5.00 to the
    scout at 10%, pending until 30 days after the charge. Through the real
    hook: _charge_commission with Stripe answering 'succeeded'."""
    scout = make_scout(ledger)
    account = referred_account(ledger, scout)
    order, event = charged_saving(account["account_id"])
    record = {**order, "account_id": account["account_id"]}
    with patch.object(billing, "charge_commission",
                      return_value=SimpleNamespace(status="succeeded", id=f"pi_test_{uuid.uuid4().hex[:12]}")):
        app_module._charge_commission(record, event)
    [earning] = earnings_for(event["id"])
    charged_at = db.q("SELECT commission_charged_at FROM savings_events WHERE id = %s", (event["id"],),
                      fetch="one")["commission_charged_at"]
    assert (earning["commission_amount"], earning["rate"], earning["amount"]) == \
        (Decimal("50.00"), Decimal("0.1000"), Decimal("5.00"))
    assert earning["status"] == "pending" and earning["available_at"] == charged_at + timedelta(days=30)


def test_no_earning_when_the_commission_charge_fails(ledger):
    scout = make_scout(ledger)
    account = referred_account(ledger, scout)
    order, event = charged_saving(account["account_id"])
    with patch.object(billing, "charge_commission", side_effect=billing.CardError("declined")):
        app_module._charge_commission({**order, "account_id": account["account_id"]}, event)
    assert earnings_for(event["id"]) == []
    assert db.scout_earning_accrue(event["id"], 30) is None   # nor later, by the catch-up


def test_no_earning_on_a_cancellation_a_legacy_account_or_a_terminated_scout(ledger):
    scout = make_scout(ledger)
    account = referred_account(ledger, scout)
    _, cancel = charged_saving(account["account_id"], action="cancel", charge_id="pi_test_cancel")
    assert db.scout_earning_accrue(cancel["id"], 30) is None

    legacy = new_account("legacy")
    ledger.accounts.append(legacy["account_id"])
    _, event = charged_saving(legacy["account_id"], charge_id=f"pi_test_{uuid.uuid4().hex[:12]}")
    assert db.scout_earning_accrue(event["id"], 30) is None

    gone = make_scout(ledger)
    gone_account = referred_account(ledger, gone)
    db.scout_transition(gone.id, "terminate", gone.user["id"])
    _, event = charged_saving(gone_account["account_id"], charge_id=f"pi_test_{uuid.uuid4().hex[:12]}")
    assert db.scout_earning_accrue(event["id"], 30) is None


def test_a_suspended_scout_keeps_earning_and_accrual_is_idempotent(ledger):
    scout = make_scout(ledger)
    account = referred_account(ledger, scout)
    db.scout_transition(scout.id, "suspend", scout.user["id"])
    _, event = charged_saving(account["account_id"], charge_id=f"pi_test_{uuid.uuid4().hex[:12]}")
    assert db.scout_earning_accrue(event["id"], 30)["amount"] == Decimal("5.00")
    assert db.scout_earning_accrue(event["id"], 30) is None
    db.scout_earnings_accrue_missing(30)
    assert len(earnings_for(event["id"])) == 1


def test_the_catch_up_writes_an_earning_the_hook_missed_and_release_matures_it(ledger):
    scout = make_scout(ledger)
    account = referred_account(ledger, scout)
    _, event = charged_saving(account["account_id"], charge_id=f"pi_test_{uuid.uuid4().hex[:12]}")
    assert any(e["savings_event_id"] == event["id"] for e in db.scout_earnings_accrue_missing(30))
    db.scout_earnings_release()
    assert earnings_for(event["id"])[0]["status"] == "pending"   # still inside the hold
    db.q("UPDATE scout_earnings SET available_at = now() - interval '1 minute' WHERE savings_event_id = %s",
         (event["id"],))
    db.scout_earnings_release()
    assert earnings_for(event["id"])[0]["status"] == "available"


# ---------------------------------------------------------------------------
# payouts
# ---------------------------------------------------------------------------

def _payable_scout(ledger, *, available=("5.00",)):
    scout = make_scout(ledger)
    db.scout_set_connect_account(scout.id, f"acct_test_{uuid.uuid4().hex[:12]}")
    db.scout_sync_payouts_enabled(db.scout(scout.id)["stripe_connect_account_id"], True)
    account = referred_account(ledger, scout)
    events = []
    for amount in available:
        saved = str((Decimal(amount) / Decimal("0.025")).quantize(Decimal("0.01")))
        _, event = charged_saving(account["account_id"], saved=saved, charge_id=f"pi_test_{uuid.uuid4().hex[:12]}")
        db.scout_earning_accrue(event["id"], 0)
        events.append(event)
    db.scout_earnings_release()
    return scout, events


def test_a_balance_under_the_minimum_rolls_forward(ledger, monkeypatch):
    monkeypatch.delenv("SCOUT_PAYOUT_MINIMUM", raising=False)
    scout, [event] = _payable_scout(ledger)
    connect = db.scout(scout.id)["stripe_connect_account_id"]
    with patch.object(billing, "transfer_to_scout") as transfer:
        app_module._run_scout_payouts(date.today().replace(day=1))
    assert not [c for c in transfer.call_args_list if c.args[0] == connect]
    assert db.scout_payouts_for(scout.id) == []
    assert earnings_for(event["id"])[0]["status"] == "available"


def test_a_payout_transfers_the_balance_once_and_marks_it_paid(ledger, monkeypatch):
    monkeypatch.setenv("SCOUT_PAYOUT_MINIMUM", "5.00")
    scout, [event] = _payable_scout(ledger)
    connect = db.scout(scout.id)["stripe_connect_account_id"]
    period = date.today().replace(day=1)
    with patch.object(billing, "transfer_to_scout", return_value=SimpleNamespace(id="tr_test_1")) as transfer:
        app_module._run_scout_payouts(period)
        app_module._run_scout_payouts(period)   # a second run in the month pays nothing more
    mine = [c for c in transfer.call_args_list if c.args[0] == connect]
    assert len(mine) == 1 and mine[0].kwargs["amount"] == Decimal("5.00")
    [payout] = db.scout_payouts_for(scout.id)
    assert payout["status"] == "paid" and payout["stripe_transfer_id"] == "tr_test_1"
    assert earnings_for(event["id"])[0]["status"] == "paid"


def test_a_failed_transfer_returns_the_earnings_to_the_pool(ledger, monkeypatch):
    monkeypatch.setenv("SCOUT_PAYOUT_MINIMUM", "5.00")
    scout, [event] = _payable_scout(ledger)
    period = date.today().replace(day=1)
    with patch.object(billing, "transfer_to_scout", side_effect=billing.PayoutError("balance_insufficient")):
        app_module._run_scout_payouts(period)
    [payout] = db.scout_payouts_for(scout.id)
    assert payout["status"] == "failed" and "balance_insufficient" in payout["error"]
    earning = earnings_for(event["id"])[0]
    assert earning["status"] == "available" and earning["payout_id"] is None

    with patch.object(billing, "transfer_to_scout", return_value=SimpleNamespace(id="tr_test_2")):
        app_module._run_scout_payouts(period)   # the retry reopens the same month's payout
    [payout] = db.scout_payouts_for(scout.id)
    assert payout["status"] == "paid" and payout["attempts"] == 2


def test_transfers_refuse_a_live_key(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_not_a_real_key")
    with pytest.raises(billing.PayoutError, match="test mode only"):
        billing.transfer_to_scout("acct_x", amount=Decimal("30"), currency="USD", payout_id=1, attempt=1)


# ---------------------------------------------------------------------------
# reversals and the Stripe webhook
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = "whsec_test_scouts"


def _post_event(client, event, secret=WEBHOOK_SECRET):
    payload = json.dumps(event)
    return client.post("/webhooks/stripe", data=payload, content_type="application/json",
                       headers={"Stripe-Signature": stripe.WebhookSignature.generate_signature_header(payload, secret)})


@pytest.fixture
def webhook_secret(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.delenv("STRIPE_CONNECT_WEBHOOK_SECRET", raising=False)


def test_the_webhook_refuses_a_bad_signature_and_needs_no_csrf_token(webhook_secret):
    anonymous = app_module.app.test_client()
    event = {"type": "account.updated", "livemode": False, "data": {"object": {"id": "acct_x"}}}
    assert _post_event(anonymous, event, secret="whsec_wrong").status_code == 400
    assert _post_event(anonymous, event).status_code == 200


def test_a_refunded_commission_reverses_a_pending_earning(ledger, webhook_secret):
    scout = make_scout(ledger)
    account = referred_account(ledger, scout)
    intent = f"pi_test_{uuid.uuid4().hex[:12]}"
    _, event = charged_saving(account["account_id"], charge_id=intent)
    db.scout_earning_accrue(event["id"], 30)
    resp = _post_event(app_module.app.test_client(),
                       {"type": "charge.refunded", "livemode": False, "data": {"object": {"payment_intent": intent}}})
    assert resp.get_json() == {"scout_earning": "reversed"}
    assert earnings_for(event["id"])[0]["status"] == "reversed"


def test_a_disputed_commission_after_payout_nets_against_the_next_payout(ledger, webhook_secret, monkeypatch):
    monkeypatch.setenv("SCOUT_PAYOUT_MINIMUM", "5.00")
    scout, [event] = _payable_scout(ledger)
    with patch.object(billing, "transfer_to_scout", return_value=SimpleNamespace(id="tr_test_3")):
        app_module._run_scout_payouts(date.today().replace(day=1))
    intent = db.q("SELECT commission_payment_intent_id FROM savings_events WHERE id = %s", (event["id"],),
                  fetch="one")["commission_payment_intent_id"]
    resp = _post_event(app_module.app.test_client(), {"type": "charge.dispute.created", "livemode": False,
                                                      "data": {"object": {"payment_intent": intent}}})
    assert resp.get_json() == {"scout_earning": "clawback"}
    earning, clawback = earnings_for(event["id"])
    assert earning["status"] == "paid"
    assert (clawback["entry"], clawback["amount"], clawback["status"]) == ("reversal", Decimal("-5.00"), "available")
    assert db.scout_balances(scout.id)["available"] == {"USD": Decimal("-5.00")}


def test_a_refund_of_a_fare_never_touches_a_scout_earning(ledger, webhook_secret):
    resp = _post_event(app_module.app.test_client(), {"type": "charge.refunded", "livemode": False,
                                                      "data": {"object": {"payment_intent": "pi_some_fare"}}})
    assert resp.get_json() == {"scout_earning": None}


def test_account_updated_mirrors_whether_a_scout_can_be_paid(ledger, webhook_secret):
    scout = make_scout(ledger)
    connect = f"acct_test_{uuid.uuid4().hex[:12]}"
    db.scout_set_connect_account(scout.id, connect)
    obj = {"id": connect, "payouts_enabled": True, "capabilities": {"transfers": "active"}}
    resp = _post_event(app_module.app.test_client(),
                       {"type": "account.updated", "livemode": False, "account": connect, "data": {"object": obj}})
    assert resp.get_json() == {"scouts": 1} and db.scout(scout.id)["payouts_enabled"] is True


# ---------------------------------------------------------------------------
# the scheduler
# ---------------------------------------------------------------------------

def test_cron_scouts_needs_the_secret_and_releases_matured_earnings(ledger, monkeypatch):
    anonymous = app_module.app.test_client()
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    assert anonymous.get("/cron/scouts").status_code == 401
    scout = make_scout(ledger)
    account = referred_account(ledger, scout)
    _, event = charged_saving(account["account_id"], charge_id=f"pi_test_{uuid.uuid4().hex[:12]}")
    db.scout_earning_accrue(event["id"], 0)
    resp = anonymous.get("/cron/scouts", headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200 and resp.get_json()["released"] >= 1
    assert earnings_for(event["id"])[0]["status"] == "available"


# ---------------------------------------------------------------------------
# who reaches what
# ---------------------------------------------------------------------------

def test_applying_without_an_account_creates_a_gated_scout_only_login(client, ledger):
    email = f"applicant-{uuid.uuid4().hex[:10]}@example.org"
    resp = client.post("/scout/apply", data={
        "_csrf": CSRF, "given_name": "Ada", "family_name": "Scout", "email": email,
        "password": "correct horse battery", "confirm": "correct horse battery",
        "company": "Ada Partners", "sourcing_plan": "My network", "agree": "yes"})
    account_id = ledger.adopt_signup(email)
    scout_row = db.q("SELECT s.* FROM scouts s JOIN users u ON u.id = s.user_id WHERE u.email = %s",
                     (email,), fetch="one")
    ledger.scouts.append(scout_row["id"])
    assert resp.status_code == 302 and scout_row["status"] == "applied"
    assert db.q("""SELECT rc.code::text AS code FROM accounts a JOIN referral_codes rc ON rc.id = a.referral_code_id
                    WHERE a.id = %s""", (account_id,), fetch="one")["code"] == "SCOUT-APPLICANT"

    # applying is not a way round the referral code: no booking side until approved
    assert client.get("/search").headers["Location"].endswith("/scout")
    assert "Application received" in client.get("/scout").get_data(as_text=True)
    db.scout_approve(scout_row["id"], scout_row["user_id"])
    assert client.get("/search").status_code == 200


def test_admin_pages_are_staff_only_and_approval_issues_a_code(client, ledger):
    staff = new_account("staff")
    ledger.accounts.append(staff["account_id"])
    applicant = make_scout(ledger, status="applied")
    sign_in_as(client, staff)
    assert client.get("/admin/scouts").status_code == 403
    assert client.post(f"/admin/scouts/{applicant.id}/approve", data={"_csrf": CSRF}).status_code == 403

    db.q("UPDATE users SET is_staff = true WHERE id = %s", (staff["id"],))
    assert client.get("/admin/scouts").status_code == 200
    resp = client.post(f"/admin/scouts/{applicant.id}/approve", data={"_csrf": CSRF})
    assert resp.status_code == 302
    codes = db.scout_codes(applicant.id)
    assert len(codes) == 1 and codes[0]["code"].startswith("SCOUT-") and db.scout(applicant.id)["status"] == "approved"


def test_staff_whose_only_login_is_a_scout_application_still_reach_admin(client, ledger):
    """The way someone without a referral code gets a login is applying to be
    a scout; once made staff, the applicant gate must not keep them out."""
    email = f"staffer-{uuid.uuid4().hex[:10]}@example.org"
    client.post("/scout/apply", data={
        "_csrf": CSRF, "given_name": "Jo", "family_name": "Staff", "email": email,
        "password": "correct horse battery", "confirm": "correct horse battery",
        "company": "TD", "sourcing_plan": "n/a", "agree": "yes"})
    ledger.adopt_signup(email)
    ledger.scouts.append(db.q("SELECT s.id FROM scouts s JOIN users u ON u.id = s.user_id WHERE u.email = %s",
                              (email,), fetch="one")["id"])
    assert client.get("/admin/scouts").headers["Location"].endswith("/scout")   # not staff yet
    db.q("UPDATE users SET is_staff = true WHERE email = %s", (email,))
    assert client.get("/admin/scouts").status_code == 200


def test_a_scout_sees_earnings_but_never_the_clients_travel(client, ledger):
    scout = make_scout(ledger)
    account = referred_account(ledger, scout)
    db.q("UPDATE accounts SET legal_name = 'Globex Test Corp' WHERE id = %s", (account["account_id"],))
    charged_saving(account["account_id"], saved="200.00", charge_id=f"pi_test_{uuid.uuid4().hex[:12]}")
    sign_in_as(client, scout.user)
    page = client.get("/scout/referrals").get_data(as_text=True)
    assert "Globex Test Corp" in page
    for travel in ("LHR", "JFK", "ZZ123", "500.00", "300.00"):
        assert travel not in page, travel


def test_the_scout_dashboard_leads_with_payout_setup_and_shows_the_link(client, ledger):
    scout = make_scout(ledger)
    sign_in_as(client, scout.user)
    page = client.get("/scout").get_data(as_text=True)
    assert "Set up payouts to get paid" in page and f"/r/{scout.code}" in page


# ---------------------------------------------------------------------------
# custom links (migration 039)
# ---------------------------------------------------------------------------

def _choose(client, code):
    with client.session_transaction() as sess:
        sess["_csrf"] = CSRF
    return client.post("/scout/settings/link", data={"_csrf": CSRF, "code": code})


def test_a_scout_chooses_a_custom_link_and_it_works_at_signup(client, ledger):
    scout = make_scout(ledger)
    sign_in_as(client, scout.user)
    custom = f"Acme-{uuid.uuid4().hex[:6]}"
    resp = _choose(client, custom)
    assert resp.status_code == 302 and "m=" in resp.headers["Location"]
    assert app_module._scout_code(scout.id) == custom
    assert f"/r/{custom}" in client.get("/scout").get_data(as_text=True)

    business = app_module.app.test_client()
    with business.session_transaction() as sess:
        sess["_csrf"] = CSRF
    r = business.get(f"/r/{custom.lower()}", headers={"X-Forwarded-For": ledger.ip()})
    assert r.status_code == 302
    assert f"value=\"{custom}\"" in business.get(r.headers["Location"]).get_data(as_text=True)
    resp, _, account = signup(business, ledger, code=custom)
    assert resp.status_code == 302
    assert db.q("SELECT scout_id FROM accounts WHERE id = %s", (account,), fetch="one")["scout_id"] == scout.id


def test_the_old_link_keeps_working_after_a_change(client, ledger):
    scout = make_scout(ledger)
    sign_in_as(client, scout.user)
    _choose(client, f"first-{uuid.uuid4().hex[:6]}")
    _choose(client, f"second-{uuid.uuid4().hex[:6]}")
    for code in [c["code"] for c in db.scout_codes(scout.id)]:   # issued SCOUT- code + both custom ones
        row, problem = referrals.check(code)
        assert problem is None and row["scout_id"] == scout.id, code

    # choosing the issued code again puts it back on the dashboard
    _choose(client, scout.code)
    assert app_module._scout_code(scout.id) == scout.code


def test_custom_links_are_unique_case_insensitively_and_follow_the_rules(client, ledger):
    first, second = make_scout(ledger), make_scout(ledger)
    custom = f"Taken-{uuid.uuid4().hex[:6]}"
    sign_in_as(client, first.user)
    _choose(client, custom)
    sign_in_as(client, second.user)
    resp = _choose(client, custom.upper())
    assert "e=" in resp.headers["Location"] and app_module._scout_code(second.id) == second.code

    for bad in ("abc", "has space", "-edge", "double--hyphen", "LEGACY", "admin", "SCOUT-ABCDE", "x" * 33):
        assert referrals.custom_code_problem(bad), bad
    assert referrals.custom_code_problem("acme-travel-2026") is None


def test_a_scout_can_hold_only_so_many_custom_links(client, ledger):
    scout = make_scout(ledger)
    for i in range(referrals.CUSTOM_LIMIT):
        assert db.scout_choose_code(scout.id, f"cap{i}-{uuid.uuid4().hex[:6]}", referrals.CUSTOM_LIMIT) == "added"
    assert db.scout_choose_code(scout.id, f"over-{uuid.uuid4().hex[:6]}", referrals.CUSTOM_LIMIT) == "limit"


def test_a_suspended_scout_cannot_change_their_link(client, ledger):
    scout = make_scout(ledger, status="suspended")
    sign_in_as(client, scout.user)
    resp = _choose(client, f"nope-{uuid.uuid4().hex[:6]}")
    assert "e=" in resp.headers["Location"]
    assert [c["code"] for c in db.scout_codes(scout.id)] == [scout.code]


def test_the_scouts_admin_page_searches_and_filters(client, ledger):
    staff = new_account("staff")
    ledger.accounts.append(staff["account_id"])
    db.q("UPDATE users SET is_staff = true WHERE id = %s", (staff["id"],))
    found = make_scout(ledger, email=f"findme-{uuid.uuid4().hex[:8]}@example.org")
    other = make_scout(ledger, status="suspended")
    sign_in_as(client, staff)

    page = client.get(f"/admin/scouts?q={found.code.lower()}").get_data(as_text=True)
    assert found.code in page and other.code not in page and "1 match" in page

    page = client.get("/admin/scouts?status=suspended").get_data(as_text=True)
    assert other.code in page and found.code not in page

    page = client.get("/admin/scouts?q=no-such-scout-anywhere").get_data(as_text=True)
    assert "No scouts match." in page and "No applications match." in page
