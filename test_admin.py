"""
The admin pages (/admin/*: analytics, accounts, issues, scouts) and what they
are built from: users.last_seen_at and the booking funnel (migration 040).

    .venv/bin/python -m pytest test_admin.py -v

Accounts come from test_money_path's fixtures and are deleted with them;
funnel_events rows go with their account (ON DELETE CASCADE). Stripe is
never reached: conftest makes it unreachable, and the one test that needs an
answer patches billing.stripe_summary.
"""

import re
from decimal import Decimal
from unittest.mock import MagicMock, patch

import app as app_module
import db
from test_money_path import (CSRF, acct, book_form, client, duffel_side_effect,  # noqa: F401
                             fake_offer, fake_order, logged_in, logged_in_carded, link_fake_card)


def _make_staff(account):
    db.q("UPDATE users SET is_staff = true WHERE id = %s", (account["id"],))


def _admin_pages():
    for rule in app_module.app.url_map.iter_rules():
        if rule.rule.startswith("/admin") and "GET" in rule.methods and not rule.arguments:
            yield rule.rule


def _steps(account_id):
    return [(r["step"], r["detail"]) for r in
            db.q("SELECT step, detail FROM funnel_events WHERE account_id = %s ORDER BY id",
                 (account_id,), fetch="all")]


def test_every_admin_page_is_staff_only(logged_in):
    client, account = logged_in
    pages = list(_admin_pages())
    assert {"/admin", "/admin/analytics", "/admin/accounts", "/admin/issues", "/admin/scouts"} <= set(pages)
    for page in pages:
        assert client.get(page).status_code == 403, page
    _make_staff(account)
    for page in pages:
        assert client.get(page).status_code in (200, 302), page


def test_the_sidebar_shows_admin_to_staff_only(logged_in):
    client, account = logged_in
    page = client.get("/overview").get_data(as_text=True)
    assert ">Admin<" not in page and "Operations" not in page
    _make_staff(account)
    page = client.get("/overview").get_data(as_text=True)
    assert ">Admin<" in page and "/admin/analytics" in page and "Operations" not in page


def test_analytics_renders_when_stripe_cannot_be_reached(logged_in):
    client, account = logged_in
    _make_staff(account)
    page = client.get("/admin/analytics").get_data(as_text=True)
    for heading in ("Customer signups", "Active users", "Purchase drop-off", "Onboarding", "Checkout",
                    "Our records", "Stripe couldn't be reached"):
        assert heading in page, heading


def test_analytics_shows_what_stripe_says(logged_in):
    client, account = logged_in
    _make_staff(account)
    summary = {"livemode": False, "available": {"USD": Decimal("3751.69")}, "pending": {},
               "charges": {"USD": {"succeeded": 7, "gross": Decimal("1234.50"), "failed": 1,
                                   "refunded": Decimal("10.00"), "disputed": 0}},
               "read": 8, "truncated": False, "days": 30}
    with patch("billing.stripe_summary", return_value=summary):
        page = client.get("/admin/analytics").get_data(as_text=True)
    assert "Test mode" in page and "3,751.69" in page and "1,234.50" in page


def test_a_page_load_records_when_the_user_was_last_seen(logged_in):
    client, account = logged_in
    assert db.q("SELECT last_seen_at FROM users WHERE id = %s", (account["id"],), fetch="one")["last_seen_at"] is None
    client.get("/overview")
    assert db.q("SELECT last_seen_at FROM users WHERE id = %s", (account["id"],), fetch="one")["last_seen_at"]


def test_a_purchase_with_no_card_is_recorded_as_an_attempt_that_failed(logged_in):
    client, account = logged_in
    offer = fake_offer()
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer)):
        assert client.post("/book", data=book_form(offer["id"])).status_code == 402
    assert _steps(account["account_id"]) == [("purchase_attempt", ""), ("purchase_failed", "no_card")]


def test_a_ticket_is_recorded_as_purchased(logged_in_carded):
    client, account = logged_in_carded
    offer = fake_offer()
    order = fake_order(amount=offer["total_amount"], currency=offer["total_currency"])
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer, order=order)), \
         patch("billing.authorize_fare", return_value=MagicMock(id="pi_test_funnel")), \
         patch("billing.capture_authorization"):
        assert client.post("/book", data=book_form(offer["id"])).status_code == 302
    assert _steps(account["account_id"]) == [("purchase_attempt", ""), ("purchased", "")]
    row = db.q("SELECT offer_ref, amount, currency FROM funnel_events WHERE account_id = %s AND step = 'purchased'",
               (account["account_id"],), fetch="one")
    assert (row["offer_ref"], row["amount"], row["currency"]) == (offer["id"], Decimal(offer["total_amount"]), "USD")


def test_a_declined_card_is_recorded_with_its_reason(logged_in_carded):
    import billing
    client, account = logged_in_carded
    offer = fake_offer()
    with patch("duffel_http.request", side_effect=duffel_side_effect(offer=offer)), \
         patch("billing.authorize_fare", side_effect=billing.CardError("declined")):
        assert client.post("/book", data=book_form(offer["id"])).status_code == 402
    assert _steps(account["account_id"])[-1] == ("purchase_failed", "card_declined")


def test_the_column_chart_has_a_clean_axis_and_capped_bars():
    from datetime import date, timedelta
    rows = [{"day": date(2026, 10, 1) + timedelta(days=i), "n": n} for i, n in enumerate([0, 3, 7, 2])]
    chart = app_module._column_chart(rows, "n", label="signups")
    assert [t["label"] for t in chart["ticks"]] == ["0", "5", "10"]
    assert chart["bars"][0]["path"] == ""           # a zero draws nothing, but keeps its hover target
    widths = {float(m) for b in chart["bars"] if b["path"]
              for m in [re.findall(r"H([\d.]+)", b["path"])[0]]}
    assert chart["total"] == 12 and widths
    assert app_module._nice_ceiling(0) == 1 and app_module._nice_ceiling(13) == 20 and app_module._nice_ceiling(51) == 100
