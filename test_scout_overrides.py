"""
Master scouts (migration 041): an approved scout invites scouts through
/join/<code>, and once one they invited is approved they earn an override — a
share (20% unless changed) of that scout's direct earnings, paid out of TD's
share, one level deep, only on commission actually collected, and only while
MASTER_OVERRIDE_ENABLED is on.

    .venv/bin/python -m pytest test_scout_overrides.py -v

Same ground rules as test_scouts.py, whose fixtures this uses: the database is
real (staging) and every test deletes what it made through `ledger`; Stripe is
never reached — the charge, the transfers and the webhook's events are the
test's own, signed with a test secret.
"""

import uuid
from datetime import date
from urllib.parse import unquote_plus
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
import pytest

import app as app_module
import billing
import db
import referrals
from test_money_path import CSRF, client, new_account  # noqa: F401
from test_scouts import (WEBHOOK_SECRET, _post_event, charged_saving, earnings_for, ledger,  # noqa: F401
                         make_scout, referred_account, shows, sign_in_as, signup, webhook_secret)


@pytest.fixture
def overrides_on(monkeypatch):
    monkeypatch.setenv("MASTER_OVERRIDE_ENABLED", "true")


@pytest.fixture
def overrides_off(monkeypatch):
    monkeypatch.delenv("MASTER_OVERRIDE_ENABLED", raising=False)


def _intent():
    return f"pi_test_{uuid.uuid4().hex[:12]}"


def by_kind(event_id):
    """{(kind, entry): row} for one savings event."""
    return {(r["kind"], r["entry"]): r for r in earnings_for(event_id)}


def collect(account, *, saved="200.00", intent=None):
    """A rebooking whose commission _charge_commission collects — Stripe
    answering 'succeeded' — so the real hook writes the earnings."""
    order, event = charged_saving(account["account_id"], saved=saved)
    intent = intent or _intent()
    with patch.object(billing, "charge_commission", return_value=SimpleNamespace(status="succeeded", id=intent)):
        app_module._charge_commission({**order, "account_id": account["account_id"]}, event)
    return event, intent


def cookies_set(resp):
    return " ".join(resp.headers.getlist("Set-Cookie"))


def admin_message(resp):
    return unquote_plus(resp.headers["Location"])


def apply_form(email=None, **extra):
    # example.net: a domain neither the scouts (example.com) nor the businesses
    # (example.org) that test_scouts makes use, so no self-referral rule trips
    email = email or f"applicant-{uuid.uuid4().hex[:10]}@example.net"
    return {"_csrf": CSRF, "given_name": "Bea", "family_name": "Recruit", "email": email,
            "password": "correct horse battery", "confirm": "correct horse battery",
            "company": "Bea Partners", "sourcing_plan": "My network", "agree": "yes", **extra}


def apply(client, ledger, form):
    """POST /scout/apply as `client`; adopts what it created into the ledger."""
    with client.session_transaction() as sess:
        sess["_csrf"] = CSRF
    resp = client.post("/scout/apply", data=form, headers={"X-Forwarded-For": ledger.ip()})
    ledger.adopt_signup(form["email"])
    row = db.scout_by_email(form["email"])
    if row and row["id"] not in ledger.scouts:
        ledger.scouts.append(row["id"])
    return resp, row


def browser():
    """A test client of its own, outside a `with` block. The `client` fixture
    keeps its last request's context open, and auth.current_user() caches on
    `g`, so a second client's request would see the first one's user."""
    c = app_module.app.test_client()
    with c.session_transaction() as sess:
        sess["_csrf"] = CSRF
    return c


def staff(ledger, client):
    person = new_account("staff")
    ledger.accounts.append(person["account_id"])
    db.q("UPDATE users SET is_staff = true WHERE id = %s", (person["id"],))
    sign_in_as(client, person)
    return person


def payable(scout_id):
    db.scout_set_connect_account(scout_id, f"acct_test_{uuid.uuid4().hex[:12]}")
    db.scout_sync_payouts_enabled(db.scout(scout_id)["stripe_connect_account_id"], True)
    return db.scout(scout_id)["stripe_connect_account_id"]


# ---------------------------------------------------------------------------
# migration 041
# ---------------------------------------------------------------------------

def test_existing_earnings_are_direct_and_the_key_allows_one_override_per_event():
    assert db.q("SELECT count(*) AS n FROM scout_earnings WHERE kind IS DISTINCT FROM 'direct' "
                "AND parent_earning_id IS NULL", fetch="one")["n"] == 0
    key = db.q("""SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint
                   WHERE conname = 'scout_earnings_event_kind_entry_key'""", fetch="one")
    assert key["d"] == "UNIQUE (savings_event_id, kind, entry)"


def test_approval_issues_an_invite_code_kept_apart_from_business_codes(ledger):
    a = make_scout(ledger)
    assert a.invite.startswith("INVITE-") and a.code.startswith("SCOUT-")
    assert db.referral_code_lookup(a.invite) is None          # not a business code
    assert db.scout_invite_lookup(a.code) is None             # nor the other way round
    assert referrals.custom_code_problem("INVITE-MINE1")      # and a scout can't make one look like it
    with db.pool().connection() as conn, conn.transaction(force_rollback=True):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO referral_codes (scout_id, code) VALUES (%s, 'INVITE-SNEAK')", (a.id,))


# ---------------------------------------------------------------------------
# the build prompt's end-to-end run, with the switch on
# ---------------------------------------------------------------------------

def test_end_to_end_invite_approve_refer_earn_pay_and_refund(ledger, overrides_on, webhook_secret, monkeypatch):
    # 1. Scout A applies and staff approve them: a business code and an invite code.
    a_user = new_account("scout-a")
    ledger.accounts.append(a_user["account_id"])
    db.q("UPDATE users SET given_name = 'Ada', family_name = 'Master' WHERE id = %s", (a_user["id"],))
    a_id = db.scout_apply(a_user["id"], company="A Co", sourcing_plan="tests")["id"]
    ledger.scouts.append(a_id)
    desk = browser()
    staff(ledger, desk)
    assert desk.post(f"/admin/scouts/{a_id}/approve", data={"_csrf": CSRF}).status_code == 302
    a_invite = db.scout_invite_code(a_id)
    assert a_invite.startswith("INVITE-")

    # 2. B opens A's /join link — the application is locked to it — applies, and is approved.
    client = browser()
    resp = client.get(f"/join/{a_invite}", headers={"X-Forwarded-For": ledger.ip()})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/scout/apply")
    cookie = resp.headers["Set-Cookie"]
    assert cookie.startswith(f"{referrals.INVITE_COOKIE}=") and "HttpOnly" in cookie and "Max-Age=7776000" in cookie
    page = client.get("/scout/apply").get_data(as_text=True)
    assert f'value="{a_invite}" readonly' in page and "Invited by Ada" in page
    resp, b_row = apply(client, ledger, apply_form(invite_code=a_invite))
    assert resp.status_code == 302 and b_row["status"] == "applied"
    assert f"{referrals.INVITE_COOKIE}=;" in cookies_set(resp)
    # pending inviter only: nothing is attributed before approval
    assert b_row["invite_code_id"] == db.scout_invite_lookup(a_invite)["invite_code_id"]
    assert b_row["introduced_by_scout_id"] is None
    assert desk.post(f"/admin/scouts/{b_row['id']}/approve", data={"_csrf": CSRF}).status_code == 302
    b = db.scout(b_row["id"])
    assert (b["introduced_by_scout_id"], b["override_rate"]) == (a_id, Decimal("0.2000")) and b["introduced_at"]
    audit = db.q("""SELECT payload FROM audit_events WHERE kind = 'scout_introduced' AND account_id = %s""",
                 (b["own_account_id"],), fetch="one")
    assert audit["payload"]["introduced_by_scout_id"] == a_id and audit["payload"]["code"] == a_invite

    # 3. A business signs up through B's /r/<code>.
    b_code = db.scout_codes(b["id"])[0]["code"]
    business = browser()
    assert business.get(f"/r/{b_code}", headers={"X-Forwarded-For": ledger.ip()}).status_code == 302
    resp, _, account_id = signup(business, ledger, code=b_code)
    assert resp.status_code == 302
    assert db.q("SELECT scout_id FROM accounts WHERE id = %s", (account_id,), fetch="one")["scout_id"] == b["id"]

    # 4. $200 saved, $50 commission charged.
    event, intent = collect({"account_id": account_id})

    # 5. B: 5.00 direct; A: 1.00 override — both pending, the same available_at.
    rows = by_kind(event["id"])
    direct, override = rows[("direct", "earning")], rows[("override", "earning")]
    assert (direct["scout_id"], direct["commission_amount"], direct["amount"]) == (b["id"], Decimal("50.00"), Decimal("5.00"))
    assert (override["scout_id"], override["rate"], override["amount"]) == (a_id, Decimal("0.2000"), Decimal("1.00"))
    assert override["parent_earning_id"] == direct["id"]
    assert direct["status"] == override["status"] == "pending"
    assert direct["available_at"] == override["available_at"]

    # 6. Recording the same event again, and the daily catch-up, write nothing more.
    app_module._accrue_scout_earning(event)
    app_module._accrue_scout_earning(event)
    app_module._scout_release()
    assert len(earnings_for(event["id"])) == 2

    # 7. Both mature together; this month's payout run includes A's 1.00.
    db.q("UPDATE scout_earnings SET available_at = now() - interval '1 minute' WHERE savings_event_id = %s",
         (event["id"],))
    app_module._scout_release()
    assert {r["status"] for r in earnings_for(event["id"])} == {"available"}
    monkeypatch.setenv("SCOUT_PAYOUT_MINIMUM", "1.00")
    a_connect, b_connect = payable(a_id), payable(b["id"])
    with patch.object(billing, "transfer_to_scout", side_effect=lambda dest, **kw: SimpleNamespace(
            id=f"tr_test_{dest[-6:]}")) as transfer:
        app_module._run_scout_payouts(date.today().replace(day=1))
    sent = {c.args[0]: c.kwargs["amount"] for c in transfer.call_args_list}
    assert sent[a_connect] == Decimal("1.00") and sent[b_connect] == Decimal("5.00")
    assert {r["status"] for r in earnings_for(event["id"])} == {"paid"}

    # 8. The commission is refunded: both already paid, so both get negative rows.
    resp = _post_event(app_module.app.test_client(), {
        "type": "charge.refunded", "livemode": False, "data": {"object": {"payment_intent": intent}}})
    assert resp.get_json() == {"scout_earning": "clawback", "override": "clawback"}
    rows = by_kind(event["id"])
    assert rows[("direct", "reversal")]["amount"] == Decimal("-5.00")
    assert rows[("override", "reversal")]["amount"] == Decimal("-1.00")
    assert rows[("override", "reversal")]["parent_earning_id"] == direct["id"]
    assert db.scout_balances(a_id, kind="override")["available"] == {"USD": Decimal("-1.00")}


def test_a_refund_before_payout_reverses_both(ledger, overrides_on, webhook_secret):
    a = make_scout(ledger)
    b = make_scout(ledger, invited_by=a)
    event, intent = collect(referred_account(ledger, b))
    resp = _post_event(app_module.app.test_client(), {
        "type": "charge.dispute.created", "livemode": False, "data": {"object": {"payment_intent": intent}}})
    assert resp.get_json() == {"scout_earning": "reversed", "override": "reversed"}
    assert {(r["kind"], r["status"]) for r in earnings_for(event["id"])} == {("direct", "reversed"),
                                                                          ("override", "reversed")}
    # and again changes nothing
    resp = _post_event(app_module.app.test_client(), {
        "type": "charge.refunded", "livemode": False, "data": {"object": {"payment_intent": intent}}})
    assert resp.get_json() == {"scout_earning": "already", "override": "already"}


def test_each_row_is_undone_by_its_own_state_when_only_one_scout_was_paid(ledger, overrides_on, webhook_secret,
                                                                         monkeypatch):
    """The two scouts are paid separately: B paid, A not yet (no payouts set up)."""
    monkeypatch.setenv("SCOUT_PAYOUT_MINIMUM", "1.00")
    a = make_scout(ledger)
    b = make_scout(ledger, invited_by=a)
    payable(b.id)
    event, intent = collect(referred_account(ledger, b))
    db.q("UPDATE scout_earnings SET available_at = now() - interval '1 minute' WHERE savings_event_id = %s",
         (event["id"],))
    db.scout_earnings_release()
    with patch.object(billing, "transfer_to_scout", return_value=SimpleNamespace(id="tr_test_b")):
        app_module._run_scout_payouts(date.today().replace(day=1))
    resp = _post_event(app_module.app.test_client(), {
        "type": "charge.refunded", "livemode": False, "data": {"object": {"payment_intent": intent}}})
    assert resp.get_json() == {"scout_earning": "clawback", "override": "reversed"}


# ---------------------------------------------------------------------------
# the override rule
# ---------------------------------------------------------------------------

def test_with_the_switch_off_no_override_is_written(ledger, overrides_off):
    a = make_scout(ledger)
    b = make_scout(ledger, invited_by=a)
    event, _ = collect(referred_account(ledger, b))
    app_module._scout_release()
    assert [r["kind"] for r in earnings_for(event["id"])] == ["direct"]
    assert db.scout_override_accrue(event["id"]) is None and db.scout_overrides_accrue_missing() == []


def test_one_level_only_a_recruits_recruit_earns_their_masters_master_nothing(ledger, overrides_on):
    a = make_scout(ledger, name="Ada")
    b = make_scout(ledger, name="Bea", invited_by=a)
    c = make_scout(ledger, name="Cal", invited_by=b)
    event, _ = collect(referred_account(ledger, c))
    app_module._scout_release()
    assert {(r["kind"], r["scout_id"]) for r in earnings_for(event["id"])} == {("direct", c.id), ("override", b.id)}
    assert not db.q("SELECT 1 FROM scout_earnings WHERE scout_id = %s", (a.id,), fetch="one")

    # B's own business still earns A an override on B's direct earning
    event, _ = collect(referred_account(ledger, b))
    assert {(r["kind"], r["scout_id"]) for r in earnings_for(event["id"])} == {("direct", b.id), ("override", a.id)}


def test_no_override_on_a_cancellation_an_uncharged_fee_or_a_scout_with_no_master(ledger, overrides_on):
    a = make_scout(ledger)
    b = make_scout(ledger, invited_by=a)
    account = referred_account(ledger, b)
    _, cancel = charged_saving(account["account_id"], action="cancel", charge_id=_intent())
    app_module._accrue_scout_earning(cancel)
    assert earnings_for(cancel["id"]) == []

    order, owed = charged_saving(account["account_id"])
    with patch.object(billing, "charge_commission", side_effect=billing.CardError("declined")):
        app_module._charge_commission({**order, "account_id": account["account_id"]}, owed)
    app_module._scout_release()
    assert earnings_for(owed["id"]) == []

    loner = make_scout(ledger)
    event, _ = collect(referred_account(ledger, loner))
    assert [r["kind"] for r in earnings_for(event["id"])] == ["direct"]


def test_an_override_that_rounds_to_nothing_is_skipped(ledger, overrides_on):
    """0.80 saved → 0.20 fee → 0.02 to the scout → 0.004 override: no row."""
    a = make_scout(ledger)
    b = make_scout(ledger, invited_by=a)
    event, _ = collect(referred_account(ledger, b), saved="0.80")
    assert [(r["kind"], r["amount"]) for r in earnings_for(event["id"])] == [("direct", Decimal("0.02"))]


def test_a_terminated_master_earns_nothing_and_a_suspended_one_still_accrues(ledger, overrides_on):
    a = make_scout(ledger)
    b = make_scout(ledger, invited_by=a)
    account = referred_account(ledger, b)
    db.scout_transition(a.id, "suspend", a.user["id"])
    event, _ = collect(account)
    assert by_kind(event["id"])[("override", "earning")]["amount"] == Decimal("1.00")

    db.scout_transition(a.id, "terminate", a.user["id"])
    event, _ = collect(account)
    assert [r["kind"] for r in earnings_for(event["id"])] == ["direct"]


def test_an_override_never_shrinks_the_scouts_own_share(ledger, overrides_on):
    a = make_scout(ledger)
    b = make_scout(ledger, invited_by=a)
    loner = make_scout(ledger)
    with_master, _ = collect(referred_account(ledger, b))
    without, _ = collect(referred_account(ledger, loner))
    assert by_kind(with_master["id"])[("direct", "earning")]["amount"] == \
        by_kind(without["id"])[("direct", "earning")]["amount"] == Decimal("5.00")


def test_a_failed_override_leaves_the_direct_earning_and_the_catch_up_writes_it(ledger, overrides_on):
    a = make_scout(ledger)
    b = make_scout(ledger, invited_by=a)
    account = referred_account(ledger, b)
    with patch.object(db, "scout_override_accrue", side_effect=psycopg.OperationalError("connection lost")):
        event, _ = collect(account)
    assert [r["kind"] for r in earnings_for(event["id"])] == ["direct"]
    assert db.q("SELECT commission_charged_at FROM savings_events WHERE id = %s", (event["id"],),
                fetch="one")["commission_charged_at"]
    assert app_module._scout_release()["overrides"] >= 1
    assert by_kind(event["id"])[("override", "earning")]["amount"] == Decimal("1.00")


def test_no_override_is_written_once_its_parent_is_no_longer_pending(ledger, overrides_on, webhook_secret):
    a = make_scout(ledger)
    b = make_scout(ledger, invited_by=a)
    account = referred_account(ledger, b)
    # a refund that arrives before the earning was ever written: recorded reversed, and never an override
    order, event = charged_saving(account["account_id"], charge_id=_intent())
    intent = db.q("SELECT commission_payment_intent_id FROM savings_events WHERE id = %s", (event["id"],),
                  fetch="one")["commission_payment_intent_id"]
    resp = _post_event(app_module.app.test_client(), {
        "type": "charge.refunded", "livemode": False, "data": {"object": {"payment_intent": intent}}})
    assert resp.get_json() == {"scout_earning": "never_accrued"}
    app_module._scout_release()
    assert [(r["kind"], r["status"]) for r in earnings_for(event["id"])] == [("direct", "reversed")]

    # a direct earning that matured while the switch was off gets no late override
    with patch.dict("os.environ", {"MASTER_OVERRIDE_ENABLED": ""}):
        later, _ = collect(account)
    db.q("UPDATE scout_earnings SET available_at = now() - interval '1 minute' WHERE savings_event_id = %s",
         (later["id"],))
    db.scout_earnings_release()
    app_module._scout_release()
    assert [r["kind"] for r in earnings_for(later["id"])] == ["direct"]


# ---------------------------------------------------------------------------
# rejection and boundary proof
# ---------------------------------------------------------------------------

def test_a_tampered_invite_field_loses_to_the_cookie(client, ledger):
    locked = make_scout(ledger, name="Lou")
    other = make_scout(ledger, name="Oz")
    client.get(f"/join/{locked.invite}", headers={"X-Forwarded-For": ledger.ip()})
    resp, row = apply(client, ledger, apply_form(invite_code=other.invite))
    assert resp.status_code == 302
    assert row["invite_code_id"] == db.scout_invite_lookup(locked.invite)["invite_code_id"]

    db.scout_approve(row["id"], locked.user["id"])
    assert db.scout(row["id"])["introduced_by_scout_id"] == locked.id


def test_a_forged_invite_cookie_is_refused_and_dropped(client, ledger):
    a = make_scout(ledger)
    client.set_cookie(referrals.INVITE_COOKIE, f"{a.invite}.not-a-signature")
    resp, row = apply(client, ledger, apply_form())
    assert resp.status_code == 400 and row is None and shows(resp, referrals.INVITE_UNVERIFIED)
    assert f"{referrals.INVITE_COOKIE}=;" in cookies_set(resp)


def test_an_existing_scout_cannot_reapply_under_an_invite(client, ledger):
    a = make_scout(ledger)
    existing = make_scout(ledger, email=f"already-{uuid.uuid4().hex[:8]}@example.org")

    # signed out, with the existing scout's email
    client.get(f"/join/{a.invite}", headers={"X-Forwarded-For": ledger.ip()})
    form = apply_form(email=db.scout(existing.id)["email"])
    with client.session_transaction() as sess:
        sess["_csrf"] = CSRF
    resp = client.post("/scout/apply", data=form, headers={"X-Forwarded-For": ledger.ip()})
    assert resp.status_code == 400 and shows(resp, referrals.ALREADY_A_SCOUT.format(email=form["email"]))
    assert db.scout(existing.id)["introduced_by_scout_id"] is None

    # signed in, approved: never reaches the form, changes nothing
    sign_in_as(client, existing.user)
    client.get(f"/join/{a.invite}", headers={"X-Forwarded-For": ledger.ip()})
    resp = client.post("/scout/apply", data={"_csrf": CSRF, "company": "x", "sourcing_plan": "y", "agree": "yes",
                                             "invite_code": a.invite})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/scout")
    assert db.scout(existing.id)["invite_code_id"] is None

    # signed in, rejected: may apply again — but not under an invite
    rejected = make_scout(ledger, status="applied")
    db.scout_transition(rejected.id, "reject", rejected.user["id"])
    sign_in_as(client, rejected.user)
    client.get(f"/join/{a.invite}", headers={"X-Forwarded-For": ledger.ip()})
    form = {"_csrf": CSRF, "company": "Again", "sourcing_plan": "again", "agree": "yes"}
    resp = client.post("/scout/apply", data=form, headers={"X-Forwarded-For": ledger.ip()})
    assert resp.status_code == 400 and shows(resp, "an invite can't be added to an existing scout")
    assert db.scout(rejected.id)["status"] == "rejected"
    resp = client.post("/scout/apply", data=form, headers={"X-Forwarded-For": ledger.ip()})   # cookie now dropped
    assert resp.status_code == 302
    assert db.scout(rejected.id)["status"] == "applied" and db.scout(rejected.id)["invite_code_id"] is None


def test_a_business_code_is_not_an_invite(client, ledger):
    a = make_scout(ledger)
    resp, row = apply(client, ledger, apply_form(invite_code=a.code))
    assert resp.status_code == 400 and row is None and shows(resp, referrals.INVITE_INVALID)

    resp = client.get(f"/join/{a.code}", headers={"X-Forwarded-For": ledger.ip()})
    assert resp.status_code == 404 and shows(resp, referrals.INVITE_INACTIVE)
    assert referrals.INVITE_COOKIE not in cookies_set(resp)


def test_an_invite_code_cannot_open_a_business_account(client, ledger):
    a = make_scout(ledger)
    resp, _, account = signup(client, ledger, code=a.invite)
    assert resp.status_code == 400 and account is None and shows(resp, referrals.INVITE_AT_SIGNUP)

    resp = client.get(f"/r/{a.invite}", headers={"X-Forwarded-For": ledger.ip()})
    assert resp.status_code == 404 and f"{referrals.COOKIE}=" not in cookies_set(resp)


def test_an_inactive_or_suspended_inviters_link_is_refused(client, ledger):
    suspended = make_scout(ledger, status="suspended")
    resp = client.get(f"/join/{suspended.invite}", headers={"X-Forwarded-For": ledger.ip()})
    assert resp.status_code == 404 and shows(resp, referrals.INVITE_INACTIVE)
    resp, row = apply(client, ledger, apply_form(invite_code=suspended.invite))
    assert resp.status_code == 400 and row is None


def test_applying_with_no_invite_is_allowed_and_has_no_master(client, ledger):
    resp, row = apply(client, ledger, apply_form())
    assert resp.status_code == 302 and row["invite_code_id"] is None
    db.scout_approve(row["id"], row["user_id"])
    assert db.scout(row["id"])["introduced_by_scout_id"] is None


def test_self_invite_rules(client, ledger):
    company = make_scout(ledger, email=f"boss-{uuid.uuid4().hex[:8]}@acme-scouting.test")
    resp, row = apply(client, ledger, apply_form(email=f"colleague-{uuid.uuid4().hex[:8]}@acme-scouting.test",
                                                 invite_code=company.invite))
    assert resp.status_code == 400 and row is None and shows(resp, "inviting scout's own company")

    freemail = make_scout(ledger, email=f"scout-{uuid.uuid4().hex[:8]}@gmail.com")
    resp, row = apply(client, ledger,
                      apply_form(email=f"friend-{uuid.uuid4().hex[:8]}@gmail.com", invite_code=freemail.invite))
    assert resp.status_code == 302 and "gmail.com" in row["invite_flag"]


# ---------------------------------------------------------------------------
# the introduction is fixed; staff can override it, audited
# ---------------------------------------------------------------------------

def test_the_introduction_is_fixed_except_through_the_audited_override(ledger, overrides_on):
    a = make_scout(ledger)
    x = make_scout(ledger)
    b = make_scout(ledger, invited_by=a)
    with pytest.raises(psycopg.errors.RaiseException, match="fixed at approval"):
        db.q("UPDATE scouts SET introduced_by_scout_id = %s WHERE id = %s", (x.id, b.id))
    with pytest.raises(psycopg.errors.RaiseException, match="fixed at approval"):
        db.q("UPDATE scouts SET override_rate = 0.5 WHERE id = %s", (b.id,))

    first, _ = collect(referred_account(ledger, b))   # A's override, before the change

    desk = browser()
    staff(ledger, desk)
    form = {"_csrf": CSRF, "email": db.scout(b.id)["email"], "master_email": db.scout(x.id)["email"]}
    assert "needs a reason" in admin_message(desk.post("/admin/introductions", data=form))
    assert db.scout(b.id)["introduced_by_scout_id"] == a.id
    desk.post("/admin/introductions", data={**form, "reason": "merged teams"})
    moved = db.scout(b.id)
    assert moved["introduced_by_scout_id"] == x.id
    audit = db.q("""SELECT payload FROM audit_events WHERE kind = 'introduction_override' AND account_id = %s""",
                 (moved["own_account_id"],), fetch="one")
    assert audit["payload"]["from_scout_id"] == a.id and audit["payload"]["to_scout_id"] == x.id
    assert audit["payload"]["reason"] == "merged teams"

    # the override already written stays with A; the next goes to X
    assert by_kind(first["id"])[("override", "earning")]["scout_id"] == a.id
    second, _ = collect(referred_account(ledger, b))
    assert by_kind(second["id"])[("override", "earning")]["scout_id"] == x.id

    desk.post("/admin/introductions", data={"_csrf": CSRF, "email": moved["email"], "master_email": "",
                                            "reason": "left the team"})
    gone = db.scout(b.id)
    assert (gone["introduced_by_scout_id"], gone["override_rate"], gone["introduced_at"]) == (None, None, None)


def test_a_scout_cannot_be_their_own_master(ledger):
    a = make_scout(ledger)
    desk = browser()
    staff(ledger, desk)
    email = db.scout(a.id)["email"]
    resp = desk.post("/admin/introductions", data={"_csrf": CSRF, "email": email, "master_email": email,
                                                    "reason": "test"})
    assert "own master" in admin_message(resp) and db.scout(a.id)["introduced_by_scout_id"] is None


def test_the_override_rate_is_snapshotted_at_approval(ledger):
    a = make_scout(ledger)
    first = make_scout(ledger, invited_by=a)
    desk = browser()
    staff(ledger, desk)
    desk.post(f"/admin/scouts/{a.id}/override-rate", data={"_csrf": CSRF, "override_percent": "15"})
    assert db.scout(a.id)["default_override_rate"] == Decimal("0.1500")
    second = make_scout(ledger, invited_by=a)
    assert db.scout(first.id)["override_rate"] == Decimal("0.2000")
    assert db.scout(second.id)["override_rate"] == Decimal("0.1500")


# ---------------------------------------------------------------------------
# what a master sees, and what staff see
# ---------------------------------------------------------------------------

def test_a_master_sees_their_team_and_overrides_but_never_the_businesses(client, ledger, overrides_on):
    a = make_scout(ledger, name="Ada")
    b = make_scout(ledger, name="Bea", invited_by=a)
    account = referred_account(ledger, b)
    db.q("UPDATE accounts SET legal_name = 'Initech Test Corp' WHERE id = %s", (account["account_id"],))
    collect(account)
    sign_in_as(client, a.user)

    dashboard = client.get("/scout").get_data(as_text=True)
    assert "Invite a business" in dashboard and "Invite a scout" in dashboard
    assert f"/r/{a.code}" in dashboard and f"/join/{a.invite}" in dashboard
    assert "Scouts introduced" in dashboard and "Override earned" in dashboard

    team = client.get("/scout/team").get_data(as_text=True)
    assert "Bea" in team and "20%" in team and "1.00 USD" in team
    earnings = client.get("/scout/earnings").get_data(as_text=True)
    assert "Override" in earnings and "from Bea" in earnings and "their earning" in earnings
    for page in (dashboard, team, earnings):
        for secret in ("Initech Test Corp", "LHR", "JFK", "ZZ123"):
            assert secret not in page, secret

    sign_in_as(client, b.user)
    assert "Initech Test Corp" in client.get("/scout/earnings").get_data(as_text=True)   # B's own client


def test_the_admin_page_shows_the_switch_who_introduced_whom_and_the_override_rate(client, ledger, monkeypatch):
    a = make_scout(ledger, name="Ada")
    b = make_scout(ledger, name="Bea", invited_by=a)
    pending = make_scout(ledger, name="Pat", status="applied", invited_by=a)
    staff(ledger, client)

    monkeypatch.delenv("MASTER_OVERRIDE_ENABLED", raising=False)
    page = client.get(f"/admin/scouts?q={db.scout(a.id)['email']}").get_data(as_text=True)
    assert "Master scout overrides" in page and ">Off<" in page
    assert 'name="override_percent" value="20' in page

    monkeypatch.setenv("MASTER_OVERRIDE_ENABLED", "true")
    page = client.get("/admin/scouts?q=Bea").get_data(as_text=True)
    assert ">On<" in page and "Introduced by" in page and "Ada" in page
    page = client.get(f"/admin/scouts?q={a.invite}").get_data(as_text=True)
    assert a.invite in page
    apps = client.get("/admin/scouts?status=applied").get_data(as_text=True)
    assert "Invited by" in apps and a.invite in apps
    listed = {s["id"]: s for s in db.scouts_admin_list(db.scout(a.id)["email"])}
    assert listed[a.id]["introduced"] == 1 and db.scout(pending.id)["introduced_by_scout_id"] is None
    assert db.scout(b.id)["introduced_by_scout_id"] == a.id
