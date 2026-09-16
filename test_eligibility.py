"""
Offer-time eligibility on staging: offer_eligibility.py's verdicts (from
eligibility.assess() alone), the single-offer refetch for null conditions, the
live search budget (live_search.py, live_search_log), the /eligibility page,
and scripts/eligibility_scan.py.

Offers come from test_fixtures/duffel_offers.json — Duffel sandbox offers
recorded 2026-08-04 and trimmed, plus one mocked variant (see each offer's
_source). HTTP to Duffel is always mocked; where a test says "through the real
client", only requests.request is patched, so duffel_http's live guards and
live_search's budget run for real against the staging database. Every
live_search_log row a test causes is deleted by captured id.

    .venv/bin/python -m pytest test_eligibility.py -v
"""

import copy
import csv
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import db
import duffel_http
import live_search
import offer_eligibility as oe
import paths
from test_money_path import acct, client, logged_in  # noqa: F401

ROOT = Path(__file__).resolve().parent
OFFERS = json.loads((ROOT / "test_fixtures" / "duffel_offers.json").read_text())
LIVE_TOKEN = "duffel_live_" + "0" * 32


def offer(name):
    return copy.deepcopy(OFFERS[name])


@pytest.fixture(autouse=True)
def no_response_dumps(monkeypatch):
    monkeypatch.setattr(paths, "DUMP_RESPONSES", False)


@pytest.fixture
def live_search_staging(monkeypatch):
    """Staging, live search on, live orders off, a live token."""
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DUFFEL_LIVE_SEARCH_ENABLED", "true")
    monkeypatch.setenv("DUFFEL_LIVE_ORDERS_ENABLED", "false")
    monkeypatch.setenv("DUFFEL_TOKEN", LIVE_TOKEN)
    monkeypatch.delenv("STAGING_MAX_MONTHLY_SEARCHES", raising=False)
    return monkeypatch


def _response(payload, status=200):
    return MagicMock(status_code=status, ok=200 <= status < 300, headers={}, json=lambda: payload)


def fake_duffel(offers, refetched=None):
    """requests.request stand-in: the offer request returns `offers`; a
    single-offer fetch returns refetched[offer_id] (or the offer unchanged)."""
    by_id = {o["id"]: o for o in offers}
    calls = []

    def _request(method, url, **kwargs):
        path = url.replace(duffel_http.BASE, "")
        calls.append((method, path))
        if method == "POST" and path == "/air/offer_requests":
            return _response({"data": {"id": "orq_test_eligibility", "offers": copy.deepcopy(offers)}})
        if method == "GET" and path.startswith("/air/offers/"):
            offer_id = path.rsplit("/", 1)[-1]
            return _response({"data": copy.deepcopy((refetched or {}).get(offer_id, by_id[offer_id]))})
        raise AssertionError(f"unexpected Duffel call {method} {path}")
    return _request, calls


@pytest.fixture
def log_ids():
    """live_search_log ids a test caused, deleted afterwards."""
    ids = []
    yield ids
    for log_id in ids:
        db.q("DELETE FROM live_search_log WHERE id = %s", (log_id,))


# ---------------------------------------------------------------------------
# verdicts — eligibility.assess() on recorded offers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,verdict", [
    ("changeable_no_fee", oe.ELIGIBLE),
    ("changeable_with_penalty", oe.ELIGIBLE_WITH_PENALTY),
    ("non_changeable_basic_economy", oe.NOT_ELIGIBLE),
    ("null_conditions", oe.UNKNOWN),
    # 0.00 GBP on a USD fare: a zero penalty needs no currency conversion.
    ("zero_fee_other_currency", oe.ELIGIBLE),
])
def test_verdict_for_recorded_offers(name, verdict):
    rows, _ = oe.evaluate_offers([offer(name)], capability_map={}, fetch_offer=None)
    assert rows[0]["verdict"] == verdict
    assert rows[0]["reason"]


def _usd_fare_with_change_penalty(amount, currency):
    changed = offer("changeable_with_penalty")  # a recorded 43.67 USD fare
    for conditions in (changed["conditions"], changed["slices"][0]["conditions"]):
        conditions["change_before_departure"].update(
            {"allowed": True, "penalty_amount": amount, "penalty_currency": currency})
    return changed


@pytest.mark.parametrize("amount", ["0.00", "0", Decimal("0.00")])
def test_zero_penalty_in_another_currency_is_not_unknown(amount):
    """Duffel sends "0.00" (a truthy string); a Decimal zero must behave the same."""
    rows, _ = oe.evaluate_offers([_usd_fare_with_change_penalty(amount, "GBP")], capability_map={},
                                 fetch_offer=None)
    assert rows[0]["verdict"] == oe.ELIGIBLE
    assert rows[0]["eligibility_reason"] != "penalty_currency_mismatch"


def test_nonzero_penalty_in_another_currency_is_unknown():
    rows, _ = oe.evaluate_offers([_usd_fare_with_change_penalty("150.00", "GBP")], capability_map={},
                                 fetch_offer=None)
    assert rows[0]["verdict"] == oe.UNKNOWN
    assert rows[0]["eligibility_reason"] == "penalty_currency_mismatch"


def test_null_penalty_is_unknown():
    rows, _ = oe.evaluate_offers([_usd_fare_with_change_penalty(None, None)], capability_map={},
                                 fetch_offer=None)
    assert rows[0]["verdict"] == oe.UNKNOWN
    assert rows[0]["eligibility_reason"] == "penalty_unknown"


def test_rows_show_conditions_as_published():
    rows, _ = oe.evaluate_offers([offer("changeable_with_penalty"), offer("null_conditions")],
                                 capability_map={}, fetch_offer=None)
    by_id = {r["offer_id"]: r for r in rows}
    penalty = by_id[OFFERS["changeable_with_penalty"]["id"]]
    assert penalty["change"] == {"allowed": True, "penalty": "10.00", "currency": "USD"}
    assert penalty["refund"]["allowed"] is False
    assert (penalty["carrier_iata"], penalty["fare_brand"], penalty["price"]) == ("ZZ", "Basic", "43.67")
    missing = by_id[OFFERS["null_conditions"]["id"]]
    assert missing["change"] == {"allowed": None, "penalty": None, "currency": ""}
    assert missing["slice_conditions"][0]["origin"]


def test_rows_sort_eligible_first_then_by_price():
    names = ["non_changeable_basic_economy", "zero_fee_other_currency", "null_conditions",
             "changeable_with_penalty", "changeable_no_fee"]
    rows, _ = oe.evaluate_offers([offer(n) for n in names], capability_map={}, fetch_offer=None)
    assert [r["verdict"] for r in rows] == [oe.ELIGIBLE, oe.ELIGIBLE, oe.ELIGIBLE_WITH_PENALTY, oe.UNKNOWN,
                                            oe.NOT_ELIGIBLE]
    assert [r["price"] for r in rows if r["verdict"] == oe.ELIGIBLE] == ["43.67", "819.48"]
    assert oe.verdict_counts(rows) == {oe.ELIGIBLE: 2, oe.ELIGIBLE_WITH_PENALTY: 1, oe.UNKNOWN: 1,
                                       oe.NOT_ELIGIBLE: 1}


def test_carrier_history_comes_from_eligibility_py():
    """Same conditions, but a carrier with two real denials and no confirms:
    eligibility.assess() refuses to trust the claim, and so does the page."""
    rows, _ = oe.evaluate_offers([offer("changeable_no_fee")],
                                 capability_map={"ZZ": {"confirmed": 0, "denied": 2}}, fetch_offer=None)
    assert rows[0]["verdict"] == oe.NOT_ELIGIBLE
    assert rows[0]["eligibility_reason"] == "carrier_never_confirmed_change"


# ---------------------------------------------------------------------------
# null conditions — fetched again once, and whether they filled in recorded
# ---------------------------------------------------------------------------

def _filled_version(name):
    filled = offer(name)
    filled["conditions"] = copy.deepcopy(OFFERS["changeable_with_penalty"]["conditions"])
    filled["total_currency"] = "USD"
    return filled


def test_null_conditions_refetched_once_and_filled_in():
    calls = []
    null = offer("null_conditions")

    def fetch(offer_id):
        calls.append(offer_id)
        return _filled_version("null_conditions")

    rows, stats = oe.evaluate_offers([null, offer("changeable_no_fee")], capability_map={}, fetch_offer=fetch)
    assert calls == [null["id"]], "only the offer with null conditions, and only once"
    row = next(r for r in rows if r["offer_id"] == null["id"])
    assert row["conditions_source"] == "refetched"
    assert row["verdict"] == oe.ELIGIBLE_WITH_PENALTY
    assert stats == {"null_conditions": 1, "refetched": 1, "conditions_filled": 1}


def test_null_conditions_still_null_failed_and_past_the_limit():
    first, second, third = offer("null_conditions"), offer("null_conditions"), offer("null_conditions")
    second["id"], third["id"] = "off_null_second", "off_null_third"

    def fetch(offer_id):
        if offer_id == second["id"]:
            raise duffel_http.DuffelError(404, [{"title": "not found"}])
        return offer("null_conditions")

    rows, stats = oe.evaluate_offers([first, second, third], capability_map={}, fetch_offer=fetch,
                                     refetch_limit=1)
    sources = {r["offer_id"]: r["conditions_source"] for r in rows}
    assert sources == {first["id"]: "still_null", second["id"]: "not_refetched", third["id"]: "not_refetched"}
    assert stats == {"null_conditions": 3, "refetched": 1, "conditions_filled": 0}

    rows, _ = oe.evaluate_offers([second], capability_map={}, fetch_offer=fetch)
    assert rows[0]["conditions_source"] == "refetch_failed"
    assert all(r["verdict"] == oe.UNKNOWN for r in rows)


# ---------------------------------------------------------------------------
# the live search budget — through the real client
# ---------------------------------------------------------------------------

def _log_row(log_id):
    return db.q("SELECT * FROM live_search_log WHERE id = %s", (log_id,), fetch="one")


def test_live_search_is_logged_and_counted(live_search_staging, log_ids):
    fake, calls = fake_duffel([offer("changeable_no_fee")])
    before = live_search.budget()["used"]
    with patch("duffel_http.requests.request", side_effect=fake):
        data, log_id = live_search.send(
            {"data": {"slices": [{"origin": "LHR", "destination": "JFK", "departure_date": "2026-12-01"}],
                      "passengers": [{"type": "adult"}] * 2, "cabin_class": "business"}},
            source="eligibility", params={"return_offers": "true"})
    log_ids.append(log_id)
    assert calls == [("POST", "/air/offer_requests")]
    assert live_search.budget()["used"] == before + 1
    row = _log_row(log_id)
    assert (row["origin"], row["destination"], str(row["departure_date"]), row["cabin"], row["passengers"]) == \
        ("LHR", "JFK", "2026-12-01", "business", 2)
    assert (row["offer_request_id"], row["offers_returned"], row["error"]) == ("orq_test_eligibility", 1, None)


def test_live_search_refused_past_the_monthly_budget(live_search_staging):
    used = live_search.budget()["used"]
    live_search_staging.setenv("STAGING_MAX_MONTHLY_SEARCHES", str(used))
    with patch("duffel_http.requests.request") as sent:
        with pytest.raises(live_search.SearchBudgetExceeded, match=f"{used} / {used}"):
            oe.search(origin="LHR", destination="JFK", departure_date="2026-12-01", cabin="economy",
                      passengers=1, source="eligibility")
    sent.assert_not_called()
    assert live_search.budget()["used"] == used


def test_test_token_searches_are_not_logged(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DUFFEL_TOKEN", "duffel_test_" + "0" * 32)
    fake, _ = fake_duffel([offer("changeable_no_fee")])
    before = live_search.budget()["used"]
    with patch("duffel_http.requests.request", side_effect=fake):
        result = oe.search(origin="LHR", destination="JFK", departure_date="2026-12-01", cabin="economy",
                           passengers=1, source="eligibility")
    assert result["live"] is False
    assert live_search.budget()["used"] == before


def test_live_eligibility_search_records_the_refetch_on_its_log_row(live_search_staging, log_ids):
    null = offer("null_conditions")
    fake, calls = fake_duffel([null, offer("changeable_with_penalty")],
                              refetched={null["id"]: _filled_version("null_conditions")})
    with patch("duffel_http.requests.request", side_effect=fake):
        result = oe.search(origin="LHR", destination="JFK", departure_date="2026-12-01", cabin="economy",
                           passengers=1, source="eligibility")
    log_id = db.q("SELECT id FROM live_search_log WHERE offer_request_id = 'orq_test_eligibility' "
                  "ORDER BY id DESC LIMIT 1", fetch="one")["id"]
    log_ids.append(log_id)
    assert calls == [("POST", "/air/offer_requests"), ("GET", f"/air/offers/{null['id']}")]
    row = _log_row(log_id)
    assert (row["offers_returned"], row["null_conditions"], row["refetched"], row["conditions_filled"]) == (2, 1, 1, 1)
    assert result["stats"] == {"null_conditions": 1, "refetched": 1, "conditions_filled": 1}


# ---------------------------------------------------------------------------
# the /eligibility page
# ---------------------------------------------------------------------------

def test_eligibility_page_is_404_outside_staging(monkeypatch, client):
    for app_env in ("production", "dev"):
        monkeypatch.setenv("APP_ENV", app_env)
        assert client.get("/eligibility").status_code == 404


def test_eligibility_page_requires_sign_in_on_staging(monkeypatch, client):
    monkeypatch.setenv("APP_ENV", "staging")
    resp = client.get("/eligibility")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_eligibility_page_lists_live_offers_eligible_first(live_search_staging, logged_in, log_ids):
    client, account = logged_in
    names = ["non_changeable_basic_economy", "null_conditions", "zero_fee_other_currency",
             "changeable_with_penalty", "changeable_no_fee"]
    fake, calls = fake_duffel([offer(n) for n in names])
    with patch("duffel_http.requests.request", side_effect=fake):
        resp = client.get("/eligibility?origin=lhr&destination=jfk&date=2026-12-01&cabin=economy&passengers=1")
    log_ids.extend(r["id"] for r in db.q("SELECT id FROM live_search_log WHERE account_id = %s",
                                         (account["account_id"],), fetch="all"))
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)

    positions = [page.index(OFFERS[n]["id"]) for n in
                 ("changeable_no_fee", "zero_fee_other_currency", "changeable_with_penalty",
                  "null_conditions", "non_changeable_basic_economy")]
    assert positions == sorted(positions), "eligible first, then with penalty, unknown, not eligible"
    for text in ("Eligible: 2", "Eligible with penalty: 1", "Unknown: 1", "Not eligible: 1",
                 f"searches this month: <b>{live_search.budget()['used']} / 1400</b>",
                 'id="f-verdict"', 'id="f-carrier"', 'id="f-penalty"',
                 "Raw conditions (offer and slices)", "conditions: still null",
                 "penalty <span class=\"mono\">10.00 USD</span>"):
        assert text in page, text
    assert ("GET", f"/air/offers/{OFFERS['null_conditions']['id']}") in calls
    assert len(log_ids) == 1


def test_eligibility_page_validates_before_searching(live_search_staging, logged_in):
    client, account = logged_in
    with patch("duffel_http.requests.request") as sent:
        resp = client.get("/eligibility?origin=LONDON&destination=JFK&date=tomorrow")
    assert resp.status_code == 400
    sent.assert_not_called()
    assert db.q("SELECT count(*) AS n FROM live_search_log WHERE account_id = %s",
                (account["account_id"],), fetch="one")["n"] == 0


def test_eligibility_page_shows_the_budget_refusal(live_search_staging, logged_in):
    client, account = logged_in
    used = live_search.budget()["used"]
    live_search_staging.setenv("STAGING_MAX_MONTHLY_SEARCHES", str(used))
    with patch("duffel_http.requests.request") as sent:
        resp = client.get("/eligibility?origin=LHR&destination=JFK&date=2026-12-01")
    sent.assert_not_called()
    assert "Live search budget reached" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# scripts/eligibility_scan.py
# ---------------------------------------------------------------------------

def _scan_module():
    spec = importlib.util.spec_from_file_location("eligibility_scan_under_test",
                                                  ROOT / "scripts" / "eligibility_scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scan_writes_csv_and_summarizes_by_carrier_and_fare_brand(monkeypatch, tmp_path, capsys):
    scan = _scan_module()
    routes = tmp_path / "routes.csv"
    routes.write_text("origin,destination,date\nlhr,jfk,2026-12-01\nLHR,JFK,2026-12-08\n")
    out = tmp_path / "scan.csv"
    names = ["changeable_no_fee", "changeable_with_penalty", "null_conditions",
             "zero_fee_other_currency", "non_changeable_basic_economy"]
    rows, stats = oe.evaluate_offers([offer(n) for n in names], capability_map={}, fetch_offer=None)
    fake_result = {"rows": rows, "stats": stats, "offer_request_id": "orq_x", "offers_returned": len(rows),
                   "live": False}

    monkeypatch.setattr("sys.argv", ["eligibility_scan.py", "--routes", str(routes), "--out", str(out)])
    with patch("offer_eligibility.search", return_value=fake_result) as searched:
        scan.main()
    assert searched.call_count == 2
    assert searched.call_args_list[0].kwargs["origin"] == "LHR"

    with open(out, newline="") as fh:
        written = list(csv.DictReader(fh))
    assert list(written[0]) == list(scan.COLUMNS)
    assert len(written) == 10
    zz = [r for r in written if r["carrier"] == "ZZ" and r["date"] == "2026-12-01"]
    assert {(r["change allowed"], r["change penalty"], r["verdict"]) for r in zz} == {
        ("yes", "0.00 USD", oe.ELIGIBLE), ("yes", "10.00 USD", oe.ELIGIBLE_WITH_PENALTY)}

    summary = {(c, b): (n, e, u) for c, b, n, e, u in scan.summarize(written)}
    assert summary[("ZZ", "Basic")] == (4, 100.0, 0.0)
    assert summary[("TP", "Discount")] == (2, 0.0, 100.0)
    assert summary[("AA", "Basic Economy")] == (2, 0.0, 0.0)
    printed = capsys.readouterr().out
    assert "% eligible" in printed and "% unknown" in printed


def test_scan_rejects_a_bad_routes_file(tmp_path):
    scan = _scan_module()
    bad = tmp_path / "routes.csv"
    bad.write_text("from,to,when\nLHR,JFK,2026-12-01\n")
    with pytest.raises(SystemExit, match="missing column"):
        scan.read_routes(bad)
    bad.write_text("origin,destination,date\nLHR,JFK,01/12/2026\n")
    with pytest.raises(SystemExit, match="YYYY-MM-DD"):
        scan.read_routes(bad)
