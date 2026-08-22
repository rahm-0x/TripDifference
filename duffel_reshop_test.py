#!/usr/bin/env python3
"""
Duffel Order Change end-to-end validation — TEST MODE ONLY.

Raw HTTP, requests only, no SDK. Every request and response is echoed to stdout
and to a timestamped transcript file so the output can be handed to Duffel
support or a vendor conversation without editing.

Sequence:
  1. Search        POST /air/offer_requests?return_offers=true   (round trip, ZZ)
  2. Select+price  GET  /air/offers/{id}                         (revalidate)
  3. Create order  POST /air/orders                              (payment: balance)
  4. Order Change  POST /air/order_change_requests
                   GET  /air/order_change_offers?order_change_request_id=...
                   POST /air/order_changes
                   POST /air/order_changes/{id}/actions/confirm
  5. Order Cancel  POST /air/order_cancellations                 (fresh order)
                   POST /air/order_cancellations/{id}/actions/confirm

Endpoints and headers verified against docs.duffel.com (v2) before writing.
Nothing is hardcoded between runs — every id is chained from the prior response.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import requests

BASE = "https://api.duffel.com"
DUFFEL_VERSION = "v2"

# Duffel Airways — the sandbox carrier. FINDINGS §8: this carrier reports
# conditions.change_before_departure.allowed == False while changes actually
# work, so we select on available_actions, never on conditions.
SANDBOX_CARRIER = "ZZ"

PRIMARY_ROUTE = ("LHR", "JFK")
DAYS_OUT = 60
TRIP_LENGTH = 7
CABIN = "economy"

# The cancel probe runs on LTN→SYD on purpose: that is Duffel's documented
# sandbox trigger for refunds landing in `airline_credits` rather than going
# back to the original payment method. Using the primary route here would
# answer "does cancel refund" but not "can cancel ever produce a credit".
CANCEL_ROUTE = ("LTN", "SYD")

TEST_PASSENGER = {
    "title": "mr",
    "given_name": "Amelia",
    "family_name": "Earhart",
    "born_on": "1985-04-16",
    "gender": "f",
    "email": "amelia@example.com",
    "phone_number": "+442080160509",
}

# What the final summary reports on.
RESULT = {
    "order_id": None,
    "pnr": None,
    "available_actions": None,
    "conditions_change_allowed": None,
    "change_request_id": None,
    "change_offers_returned": 0,
    "change_totals_seen": [],
    "change_confirmed": False,
    "change_confirm_detail": None,
    "cancel_order_id": None,
    "cancel_refund_to": None,
    "cancel_airline_credits": None,
    "failures": [],
}


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

class Log:
    """Tee to stdout and a transcript file. The transcript is the deliverable."""

    def __init__(self, path):
        self.path = path
        self.fh = open(path, "w", encoding="utf-8")

    def __call__(self, msg=""):
        print(msg)
        self.fh.write(str(msg) + "\n")
        self.fh.flush()

    def rule(self, title):
        self("")
        self("=" * 78)
        self(title)
        self("=" * 78)

    def step(self, n, title):
        self("")
        self("-" * 78)
        self(f"STEP {n} — {title}")
        self("-" * 78)


LOG = None  # set in main()


def jd(obj):
    return json.dumps(obj, indent=2, sort_keys=True, default=str)


# A search with return_offers=true returns hundreds of fully-expanded offers —
# ~78 MB of JSON, which makes the transcript unusable as a document. That one
# array is condensed for logging only. Every other body, and everything the
# script acts on, is verbatim and untouched.
LOG_OFFERS_SHOWN = 3


def for_log(payload):
    if not isinstance(payload, dict):
        return payload
    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    offers = data.get("offers")
    if not isinstance(offers, list) or len(offers) <= LOG_OFFERS_SHOWN:
        return payload
    shown = offers[:LOG_OFFERS_SHOWN]
    return {
        **payload,
        "data": {
            **data,
            "offers": shown + [
                f"<<< {len(offers) - LOG_OFFERS_SHOWN} further offers omitted from this "
                f"LOG ONLY — {len(offers)} were returned and all were considered. "
                f"Search-result bulk only; no other response in this transcript is "
                f"abridged. >>>"
            ],
        },
    }


# --------------------------------------------------------------------------
# token
# --------------------------------------------------------------------------

def load_token():
    """
    Env first, then .env alongside this script. Refuses anything that is not
    unambiguously a test token — this script books, changes and cancels orders.
    """
    tok = os.environ.get("DUFFEL_TOKEN", "").strip()

    if not tok:
        envfile = Path(__file__).resolve().parent / ".env"
        if envfile.exists():
            for line in envfile.read_text().splitlines():
                m = re.match(r'^\s*(?:export\s+)?DUFFEL_TOKEN\s*=\s*(.*)$', line)
                if m:
                    tok = m.group(1).strip().strip('"').strip("'")
                    break

    if not tok:
        print("FATAL: DUFFEL_TOKEN is not set (checked environment and ./.env).")
        sys.exit(2)

    if tok.startswith("duffel_live_"):
        print("=" * 78)
        print("REFUSING TO RUN — DUFFEL_TOKEN IS A LIVE TOKEN.")
        print("This script creates, changes and cancels real orders. Test mode only.")
        print("=" * 78)
        sys.exit(2)

    if not tok.startswith("duffel_test_"):
        print("=" * 78)
        print("REFUSING TO RUN — token does not look like a Duffel test token.")
        print(f"Expected a 'duffel_test_' prefix, got '{tok[:12]}...'.")
        print("=" * 78)
        sys.exit(2)

    return tok


TOKEN = None  # set in main()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class DuffelError(RuntimeError):
    """Non-2xx from Duffel, carrying the full parsed body."""

    def __init__(self, method, path, status, body, raw_text=None):
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        self.raw_text = raw_text
        errs = (body or {}).get("errors", []) if isinstance(body, dict) else []
        head = "; ".join(
            f"[{e.get('type')}/{e.get('code')}] {e.get('title')}" for e in errs
        ) or "no error array in body"
        super().__init__(f"HTTP {status} on {method} {path} — {head}")

    @property
    def codes(self):
        errs = (self.body or {}).get("errors", []) if isinstance(self.body, dict) else []
        return [e.get("code") for e in errs]

    def render(self):
        out = [
            "",
            "!!! REQUEST FAILED " + "!" * 59,
            f"  {self.method} {self.path}",
            f"  HTTP status: {self.status}",
            "  full response body:",
        ]
        if isinstance(self.body, (dict, list)):
            out.append(jd(self.body))
        else:
            out.append(str(self.raw_text)[:4000])
        out.append("!" * 78)
        return "\n".join(out)


def api(method, path, *, body=None, params=None):
    """
    Single request helper. Logs the full request and full response every time —
    that verbatim transcript is the point of this script.

    Retries a 429 once, honouring Retry-After if present, else Duffel's
    documented `ratelimit-reset` (an RFC 2616 *date*, not a seconds delta).
    """
    url = f"{BASE}{path}"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Duffel-Version": DUFFEL_VERSION,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    for attempt in (1, 2):
        LOG("")
        LOG(f">>> {method} {url}")
        if params:
            LOG(f"    params: {json.dumps(params)}")
        LOG("    headers: " + json.dumps({
            **headers,
            "Authorization": "Bearer duffel_test_***REDACTED***",
        }, indent=2))
        if body is not None:
            LOG("    request body:")
            LOG(jd(body))

        t0 = time.time()
        resp = requests.request(
            method, url, headers=headers, json=body, params=params, timeout=60
        )
        ms = (time.time() - t0) * 1000

        LOG(f"<<< HTTP {resp.status_code}  ({ms:.0f} ms)")

        if resp.status_code == 429 and attempt == 1:
            wait = _rate_limit_wait(resp)
            LOG(f"    429 rate limited — sleeping {wait:.0f}s and retrying once")
            time.sleep(wait)
            continue

        try:
            payload = resp.json()
        except ValueError:
            LOG("    response body was not JSON:")
            LOG(resp.text[:4000])
            raise DuffelError(method, path, resp.status_code, None, resp.text)

        LOG("    response body:")
        LOG(jd(for_log(payload)))

        if not resp.ok:
            raise DuffelError(method, path, resp.status_code, payload)

        return payload.get("data")

    raise DuffelError(method, path, 429, {"errors": [{"code": "rate_limited",
                                                     "title": "rate limited twice"}]})


def _rate_limit_wait(resp):
    if resp.headers.get("Retry-After"):
        try:
            return max(1.0, float(resp.headers["Retry-After"]))
        except ValueError:
            pass
    reset = resp.headers.get("ratelimit-reset")
    if reset:
        try:
            from email.utils import parsedate_to_datetime
            delta = (parsedate_to_datetime(reset) - datetime.now(timezone.utc)).total_seconds()
            return max(1.0, min(delta, 120.0))
        except (TypeError, ValueError):
            pass
    return 5.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def money(amount, currency, signed=False):
    if amount is None:
        return "—"
    d = Decimal(str(amount))
    s = f"{d:+.2f}" if signed else f"{d:.2f}"
    return f"{s} {currency or ''}".strip()


def dates():
    base = datetime.now(timezone.utc).date() + timedelta(days=DAYS_OUT)
    return base.isoformat(), (base + timedelta(days=TRIP_LENGTH)).isoformat()


def segments_of(slices):
    out = []
    for sl in slices or []:
        for sg in sl.get("segments", []):
            mc = sg.get("marketing_carrier") or {}
            out.append(
                f"{mc.get('iata_code', '??')}{sg.get('marketing_carrier_flight_number', '?')} "
                f"{(sg.get('origin') or {}).get('iata_code')}→"
                f"{(sg.get('destination') or {}).get('iata_code')} "
                f"dep {sg.get('departing_at')}"
            )
    return out


def search_offers(origin, destination, dep, ret=None, label=""):
    """One offer request. Duffel burns an offer request once any offer in it is
    booked (FINDINGS §4), so every booking in this script gets its own search."""
    slices = [{"origin": origin, "destination": destination, "departure_date": dep}]
    if ret:
        slices.append({"origin": destination, "destination": origin, "departure_date": ret})

    data = api("POST", "/air/offer_requests", params={"return_offers": "true"}, body={
        "data": {
            "slices": slices,
            "passengers": [{"type": "adult"}],
            "cabin_class": CABIN,
        }
    })
    offers = data.get("offers", [])
    LOG("")
    LOG(f"  offer_request id : {data['id']}   {label}")
    LOG(f"  offers returned  : {len(offers)}")
    return data, offers


def pick_full_fare(offers, carrier=SANDBOX_CARRIER):
    """
    Most expensive offer on the sandbox carrier — the closest sandbox analogue
    to a full/flexible fare, which is the fare most likely to be changeable.
    Falls back to any carrier if the sandbox carrier returned nothing.
    """
    ours = [o for o in offers if (o.get("owner") or {}).get("iata_code") == carrier]
    pool = ours or offers
    if not ours:
        LOG(f"  NOTE: no offers from carrier {carrier}; falling back to all carriers")
    return sorted(pool, key=lambda o: Decimal(o["total_amount"]), reverse=True)[0]


def book(offer_id):
    offer = api("GET", f"/air/offers/{offer_id}")
    passengers = [{"id": p["id"], **TEST_PASSENGER} for p in offer.get("passengers", [])]
    return api("POST", "/air/orders", body={
        "data": {
            "type": "instant",
            "selected_offers": [offer_id],
            "passengers": passengers,
            "payments": [{
                "type": "balance",
                "currency": offer["total_currency"],
                "amount": offer["total_amount"],
            }],
        }
    }), offer


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

def step_1_search():
    LOG.step(1, "SEARCH — round trip, one adult, economy, Duffel Airways")
    dep, ret = dates()
    o, d = PRIMARY_ROUTE
    LOG(f"  route: {o} → {d} → {o}   out {dep}   back {ret}   cabin {CABIN}")
    req, offers = search_offers(o, d, dep, ret, label="(primary booking)")
    if not offers:
        raise RuntimeError("search returned zero offers — cannot continue")
    return req, offers


def step_2_price(offers):
    LOG.step(2, "SELECT + PRICE — revalidate the single offer before booking")
    chosen = pick_full_fare(offers)
    LOG(f"  selected from search : {chosen['id']}")
    LOG(f"  carrier              : {(chosen.get('owner') or {}).get('name')} "
        f"({(chosen.get('owner') or {}).get('iata_code')})")
    LOG(f"  price at search time : {money(chosen['total_amount'], chosen['total_currency'])}")
    LOG("")
    LOG("  re-fetching the offer on its own to confirm the live price...")

    live = api("GET", f"/air/offers/{chosen['id']}")

    LOG("")
    LOG(f"  base   : {money(live.get('base_amount'), live.get('base_currency'))}")
    LOG(f"  tax    : {money(live.get('tax_amount'), live.get('tax_currency'))}")
    LOG(f"  TOTAL  : {money(live['total_amount'], live['total_currency'])}")
    if Decimal(live["total_amount"]) != Decimal(chosen["total_amount"]):
        LOG(f"  ** price moved between search and revalidation: "
            f"{chosen['total_amount']} → {live['total_amount']} **")
    else:
        LOG("  price unchanged since search")
    LOG("  itinerary:")
    for s in segments_of(live.get("slices", [])):
        LOG(f"    {s}")
    return live


def step_3_order(offer):
    LOG.step(3, "CREATE ORDER — book the revalidated offer (payment type: balance)")
    order, _ = book(offer["id"])

    RESULT["order_id"] = order["id"]
    RESULT["pnr"] = order.get("booking_reference")
    RESULT["available_actions"] = order.get("available_actions")

    LOG("")
    LOG(f"  order id          : {order['id']}")
    LOG(f"  booking reference : {order.get('booking_reference')}   <-- PNR")
    LOG(f"  total             : {money(order['total_amount'], order['total_currency'])}")
    LOG(f"  owner             : {(order.get('owner') or {}).get('name')}")
    LOG(f"  live_mode         : {order.get('live_mode')}")
    LOG(f"  available_actions : {order.get('available_actions')}")
    LOG("  slice ids:")
    for sl in order.get("slices", []):
        LOG(f"    {sl['id']}  {(sl.get('origin') or {}).get('iata_code')}→"
            f"{(sl.get('destination') or {}).get('iata_code')}")

    LOG("")
    LOG("  --- RAW order.conditions (does the order report itself changeable?) ---")
    LOG(jd(order.get("conditions")))

    cbd = (order.get("conditions") or {}).get("change_before_departure")
    RESULT["conditions_change_allowed"] = (cbd or {}).get("allowed")

    LOG("")
    LOG("  READ THIS CAREFULLY — the two changeability signals can disagree:")
    LOG(f"    conditions.change_before_departure.allowed : {(cbd or {}).get('allowed')}")
    if cbd:
        LOG(f"    conditions penalty                        : "
            f"{money(cbd.get('penalty_amount'), cbd.get('penalty_currency'))}")
    LOG(f"    'change' in available_actions              : "
        f"{'change' in (order.get('available_actions') or [])}")
    LOG("    On Duffel Airways these routinely disagree (conditions says False,")
    LOG("    the change still works). available_actions is the reliable signal.")

    for key in ("changes", "airline_initiated_changes", "cancellation",
                "available_airline_credit_ids"):
        if key in order:
            LOG(f"  order.{key} = {json.dumps(order.get(key), default=str)}")

    return order


def step_4_order_change(order):
    LOG.step(4, "ORDER CHANGE — the critical step")

    if "change" not in (order.get("available_actions") or []):
        LOG("  WARNING: 'change' is absent from available_actions. Attempting anyway "
            "so the exact rejection body is captured.")

    slice_to_remove = order["slices"][0]
    origin = (slice_to_remove.get("origin") or {}).get("iata_code")
    dest = (slice_to_remove.get("destination") or {}).get("iata_code")
    new_dep = (datetime.now(timezone.utc).date() + timedelta(days=DAYS_OUT + 3)).isoformat()

    LOG(f"  removing slice {slice_to_remove['id']} ({origin}→{dest})")
    LOG(f"  adding      {origin}→{dest} departing {new_dep} ({CABIN})")

    # --- 4a: create the order change request -------------------------------
    LOG("")
    LOG("  [4a] POST /air/order_change_requests")
    ocr = api("POST", "/air/order_change_requests", body={
        "data": {
            "order_id": order["id"],
            "slices": {
                "remove": [{"slice_id": slice_to_remove["id"]}],
                "add": [{
                    "origin": origin,
                    "destination": dest,
                    "departure_date": new_dep,
                    "cabin_class": CABIN,
                }],
            },
        }
    })
    RESULT["change_request_id"] = ocr["id"]
    LOG(f"  order_change_request id: {ocr['id']}")

    # --- 4b: list the change offers ----------------------------------------
    LOG("")
    LOG("  [4b] GET /air/order_change_offers  (list endpoint, sorted by change_total_amount)")
    listed = []
    try:
        listed = api("GET", "/air/order_change_offers", params={
            "order_change_request_id": ocr["id"],
            "sort": "change_total_amount",
            "limit": 50,
        })
    except DuffelError as e:
        LOG(e.render())
        LOG("  list endpoint failed — falling back to the offers embedded in the "
            "change request response")

    embedded = ocr.get("order_change_offers", []) or []
    offers = listed if listed else embedded
    RESULT["change_offers_returned"] = len(offers)

    LOG("")
    LOG(f"  change offers returned: {len(offers)}  "
        f"(embedded in request: {len(embedded)}, from list endpoint: {len(listed or [])})")

    if not offers:
        LOG("  EMPTY SET — no order change offers were returned.")
        LOG("  Full order_change_request response is above; nothing was swallowed.")
        RESULT["failures"].append("order change request returned zero offers")
        return None

    paid = Decimal(order["total_amount"])
    LOG("")
    LOG("  " + "-" * 74)
    for i, co in enumerate(offers, 1):
        chg = co.get("change_total_amount")
        RESULT["change_totals_seen"].append(chg)
        LOG(f"  offer {i}: {co['id']}")
        LOG(f"    change_total_amount  (fare difference + penalty) : "
            f"{money(chg, co.get('change_total_currency'), signed=True)}")
        LOG(f"    penalty_total_amount (airline fee)               : "
            f"{money(co.get('penalty_total_amount'), co.get('penalty_total_currency'))}")
        LOG(f"    new_total_amount     (ticket after the change)   : "
            f"{money(co.get('new_total_amount'), co.get('new_total_currency'))}")
        LOG(f"    original order total                             : {money(paid, order['total_currency'])}")
        LOG(f"    refund_to                                        : {co.get('refund_to')}")
        LOG(f"    expires_at                                       : {co.get('expires_at')}")
        LOG("    fare conditions:")
        LOG("      " + jd(co.get("conditions")).replace("\n", "\n      "))
        for s in segments_of((co.get("slices") or {}).get("add", [])):
            LOG(f"    add: {s}")
        LOG("  " + "-" * 74)

    uniq = sorted({str(c) for c in RESULT["change_totals_seen"]})
    LOG("")
    LOG(f"  distinct change_total_amount values across all {len(offers)} offers: {uniq}")
    if len(uniq) == 1:
        LOG("  ** Every offer carries an IDENTICAL change total. In sandbox this is a")
        LOG("     stub, not computed pricing — the offers differ only by flight/time. **")

    # --- 4c: create + confirm the change -----------------------------------
    best = sorted(offers, key=lambda c: Decimal(str(c.get("change_total_amount", "0"))))[0]
    LOG("")
    LOG(f"  [4c] cheapest change offer selected: {best['id']} "
        f"({money(best.get('change_total_amount'), best.get('change_total_currency'), signed=True)})")
    LOG("  POST /air/order_changes")

    oc = api("POST", "/air/order_changes", body={
        "data": {"selected_order_change_offer": best["id"]}
    })
    LOG(f"  pending order_change id: {oc['id']}")

    delta = Decimal(str(oc.get("change_total_amount", "0")))
    LOG("")
    LOG(f"  [4d] POST /air/order_changes/{oc['id']}/actions/confirm")

    # Docs: if change_total_amount is zero or negative, no payment object is needed.
    if delta > 0:
        payload = {"data": {"payment": {
            "type": "balance",
            "currency": oc["change_total_currency"],
            "amount": str(oc["change_total_amount"]),
        }}}
        LOG(f"  change total is POSITIVE ({money(delta, oc['change_total_currency'], signed=True)}) "
            f"→ sending a balance payment object")
    else:
        payload = {"data": {}}
        LOG(f"  change total is ZERO OR NEGATIVE "
            f"({money(delta, oc.get('change_total_currency'), signed=True)}) "
            f"→ omitting the payment object, per the docs")

    confirmed = api("POST", f"/air/order_changes/{oc['id']}/actions/confirm", body=payload)

    RESULT["change_confirmed"] = bool(confirmed.get("confirmed_at"))
    RESULT["change_confirm_detail"] = {
        "order_change_id": confirmed.get("id"),
        "confirmed_at": confirmed.get("confirmed_at"),
        "change_total": money(confirmed.get("change_total_amount"),
                              confirmed.get("change_total_currency"), signed=True),
        "new_total": money(confirmed.get("new_total_amount"),
                           confirmed.get("new_total_currency")),
        "penalty": money(confirmed.get("penalty_total_amount"),
                         confirmed.get("penalty_total_currency")),
        "refund_to": confirmed.get("refund_to"),
    }

    LOG("")
    LOG("  *** ORDER CHANGE CONFIRMED ***")
    for k, v in RESULT["change_confirm_detail"].items():
        LOG(f"    {k:<16}: {v}")

    # --- 4e: resulting order state -----------------------------------------
    LOG("")
    LOG("  [4e] GET the order back to show the resulting state")
    after = api("GET", f"/air/orders/{order['id']}")
    LOG("")
    LOG(f"  order id          : {after['id']}")
    LOG(f"  booking reference : {after.get('booking_reference')}")
    LOG(f"  total now         : {money(after['total_amount'], after['total_currency'])} "
        f"(was {money(order['total_amount'], order['total_currency'])})")
    LOG(f"  available_actions : {after.get('available_actions')}")
    LOG(f"  changes recorded  : {len(after.get('changes') or [])}")
    LOG("  itinerary after the change:")
    for s in segments_of(after.get("slices", [])):
        LOG(f"    {s}")
    return confirmed


def step_5_cancel():
    LOG.step(5, "ORDER CANCEL — fallback path, on a FRESH order")
    o, d = CANCEL_ROUTE
    dep, ret = dates()
    LOG(f"  route: {o} → {d}   {dep}")
    LOG("  NOTE: this route is Duffel's documented sandbox trigger for refunds")
    LOG("        landing in airline_credits rather than the original payment method.")
    LOG("  NOTE: a fresh offer request is required — an offer request is single-use,")
    LOG("        so the primary search above cannot be reused for a second booking.")

    _, offers = search_offers(o, d, dep, label="(cancel probe)")
    if not offers:
        raise RuntimeError("cancel probe search returned zero offers")

    chosen = pick_full_fare(offers)
    LOG(f"  booking {chosen['id']} "
        f"({money(chosen['total_amount'], chosen['total_currency'])})")
    order, _ = book(chosen["id"])
    RESULT["cancel_order_id"] = order["id"]
    LOG(f"  fresh order id    : {order['id']}")
    LOG(f"  booking reference : {order.get('booking_reference')}")
    LOG(f"  available_actions : {order.get('available_actions')}")

    LOG("")
    LOG("  [5a] POST /air/order_cancellations  (quote)")
    quote = api("POST", "/air/order_cancellations", body={"data": {"order_id": order["id"]}})
    LOG("")
    LOG(f"  cancellation id : {quote['id']}")
    LOG(f"  refund_amount   : {money(quote.get('refund_amount'), quote.get('refund_currency'))}")
    LOG(f"  refund_to       : {quote.get('refund_to')}   <-- 'airline_credit' here means a credit, "
        f"not cash back")
    LOG(f"  expires_at      : {quote.get('expires_at')}")

    LOG("")
    LOG(f"  [5b] POST /air/order_cancellations/{quote['id']}/actions/confirm")
    done = api("POST", f"/air/order_cancellations/{quote['id']}/actions/confirm", body={"data": {}})
    RESULT["cancel_refund_to"] = done.get("refund_to")
    LOG("")
    LOG(f"  confirmed_at  : {done.get('confirmed_at')}")
    LOG(f"  refund_amount : {money(done.get('refund_amount'), done.get('refund_currency'))}")
    LOG(f"  refund_to     : {done.get('refund_to')}")

    LOG("")
    LOG("  [5c] GET the order back — looking for an airline credit entity")
    after = api("GET", f"/air/orders/{order['id']}")
    LOG("")
    LOG(f"  cancelled_at                 : {after.get('cancelled_at')}")
    LOG(f"  available_airline_credit_ids : "
        f"{json.dumps(after.get('available_airline_credit_ids'), default=str)}")

    LOG("")
    LOG("  [5d] GET /air/airline_credits — credits are a SEPARATE entity, not")
    LOG("       embedded on the order. The list endpoint filters only by user_id,")
    LOG("       so we pull the page and match on the credit's own order_id field.")
    credits = []
    try:
        all_credits = api("GET", "/air/airline_credits", params={"limit": 200}) or []
        credits = [c for c in all_credits if c.get("order_id") == order["id"]]
        LOG("")
        LOG(f"  airline credits on the account : {len(all_credits)}")
        LOG(f"  matching this order            : {len(credits)}")
        for c in credits:
            LOG(f"    credit id     : {c.get('id')}")
            LOG(f"    credit_code   : {c.get('credit_code')}")
            LOG(f"    credit_name   : {c.get('credit_name')}")
            LOG(f"    credit_amount : {money(c.get('credit_amount'), c.get('credit_currency'))}")
            LOG(f"    issued_on     : {c.get('issued_on')}   expires_at: {c.get('expires_at')}")
            LOG(f"    passenger_id  : {c.get('passenger_id')}   user_id: {c.get('user_id')}")
    except DuffelError as e:
        LOG(e.render())
        RESULT["failures"].append(f"airline credits lookup: HTTP {e.status} {e.codes or ''}".strip())

    RESULT["cancel_airline_credits"] = credits

    if credits:
        LOG("")
        LOG("  ** AN AIRLINE CREDIT ENTITY WAS GENERATED. **")
    elif RESULT["cancel_refund_to"] in ("airline_credit", "airline_credits"):
        LOG("")
        LOG("  ** refund_to == 'airline_credits', so the refund WAS issued as a credit")
        LOG("     rather than cash — but no retrievable credit entity is attached.")
        LOG("     Credits are indexed by customer user_id, and this order was booked")
        LOG("     without one (order.users is empty), so nothing is addressable.")
        LOG("     To actually spend a credit you must book with a customer user. **")
    else:
        LOG("")
        LOG(f"  No airline credit. Refund went to '{RESULT['cancel_refund_to']}'.")
    return done


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------

def summary():
    LOG.rule("PLAIN-ENGLISH SUMMARY")

    LOG("")
    LOG(f"Primary order : {RESULT['order_id']}  (PNR {RESULT['pnr']})")
    LOG(f"available_actions on that order: {RESULT['available_actions']}")
    LOG(f"conditions.change_before_departure.allowed: {RESULT['conditions_change_allowed']}")
    if (RESULT["available_actions"]
            and ("change" in RESULT["available_actions"])
            and RESULT["conditions_change_allowed"] is False):
        LOG("  ^ These disagree. The order says it is NOT changeable in `conditions`,")
        LOG("    but exposes a `change` action and accepted a change anyway.")
        LOG("    Trust available_actions; conditions is unreliable on this carrier.")

    LOG("")
    LOG("1. DID ORDER CHANGE WORK?")
    if RESULT["change_confirmed"]:
        d = RESULT["change_confirm_detail"]
        LOG("   YES — the full flow completed: change request → change offers →")
        LOG("   order change created → confirmed.")
        LOG(f"   {RESULT['change_offers_returned']} change offers were returned.")
        LOG(f"   Confirmed at {d['confirmed_at']}, change total {d['change_total']}, "
            f"new ticket total {d['new_total']}, penalty {d['penalty']}.")
    elif RESULT["change_offers_returned"]:
        LOG(f"   PARTIALLY — {RESULT['change_offers_returned']} change offers came back, "
            f"but the change was not confirmed.")
        for f in RESULT["failures"]:
            LOG(f"   blocker: {f}")
    else:
        LOG("   NO — the Order Change flow did not produce a usable result.")
        for f in RESULT["failures"]:
            LOG(f"   blocker: {f}")

    uniq = sorted({str(c) for c in RESULT["change_totals_seen"]})
    if uniq:
        LOG("")
        LOG("   ON THE PRICING ITSELF — read this before drawing business conclusions:")
        LOG(f"   distinct change_total_amount values seen: {uniq}")
        if len(uniq) == 1:
            LOG("   Every single change offer priced identically. That is a sandbox stub,")
            LOG("   not a fare calculation. Duffel test mode does not compute change")
            LOG("   prices from fares, so a NEGATIVE change_total_amount (the refund")
            LOG("   case) cannot be produced or validated here at all.")
        negs = [c for c in RESULT["change_totals_seen"]
                if Decimal(str(c)) < 0]
        LOG(f"   negative change totals seen: {len(negs)}")
        if not negs:
            LOG("   No negative change total was observed. The docs state the field")
            LOG("   'may be negative to reflect a refund', and the confirm endpoint has")
            LOG("   a documented branch for it — but that path is unproven in sandbox")
            LOG("   and needs a live-mode test against a real carrier and a real fare drop.")

    LOG("")
    LOG("2. DID ORDER CANCEL PRODUCE A CREDIT?")
    credit_refund = RESULT["cancel_refund_to"] in ("airline_credit", "airline_credits")
    if RESULT["cancel_order_id"] is None:
        LOG("   NOT TESTED — the cancel step did not complete.")
    elif RESULT["cancel_airline_credits"]:
        ids = [c.get("id") for c in RESULT["cancel_airline_credits"]]
        LOG(f"   YES — cancelling order {RESULT['cancel_order_id']} generated an airline")
        LOG(f"   credit entity: {json.dumps(ids, default=str)}")
        LOG(f"   refund_to was '{RESULT['cancel_refund_to']}'.")
    elif credit_refund:
        LOG(f"   YES, AS A REFUND TYPE — but NO RETRIEVABLE ENTITY.")
        LOG(f"   The cancellation on order {RESULT['cancel_order_id']} settled with")
        LOG(f"   refund_to = '{RESULT['cancel_refund_to']}', so the money came back as a")
        LOG("   credit rather than cash. However no airline credit object was")
        LOG("   retrievable for it: GET /air/airline_credits indexes by customer")
        LOG("   user_id, and this order was booked without a customer user.")
        LOG("   Practical consequence: to build cancel-then-rebook you must create")
        LOG("   the order against a customer user, or the credit is unspendable.")
    else:
        LOG(f"   NO — cancellation succeeded but refunded to "
            f"'{RESULT['cancel_refund_to']}', not to a credit.")

    LOG("")
    LOG("3. WHICH PATH DOES THIS ORDER SUPPORT?")
    can_change = RESULT["change_confirmed"]
    can_credit = bool(RESULT["cancel_airline_credits"]) or credit_refund
    if can_change and can_credit:
        LOG("   BOTH. Order Change executes end to end, and cancellation can produce")
        LOG("   an airline credit. Order Change is the better path — it keeps the")
        LOG("   booking intact and settles the difference directly.")
    elif can_change:
        LOG("   ORDER CHANGE. The exchange path works end to end on this carrier.")
        LOG("   Cancellation refunds to the original payment method rather than")
        LOG("   issuing a credit, so cancel-then-rebook carries full re-purchase risk.")
    elif can_credit:
        LOG("   CANCEL-THEN-REBOOK ONLY. Order Change did not complete, but")
        LOG("   cancellation yields an airline credit that can fund a rebooking.")
    else:
        LOG("   NEITHER completed cleanly in this run. See the failures above and the")
        LOG("   full error bodies in the transcript.")

    if RESULT["failures"]:
        LOG("")
        LOG("FAILURES CAPTURED THIS RUN:")
        for f in RESULT["failures"]:
            LOG(f"  - {f}")

    LOG("")
    LOG(f"Full request/response transcript: {LOG.path}")


# --------------------------------------------------------------------------

def run_step(name, fn, *args):
    """Every step is isolated: a failure prints the full body and does not abort
    the remaining steps."""
    try:
        return fn(*args)
    except DuffelError as e:
        LOG(e.render())
        RESULT["failures"].append(f"{name}: HTTP {e.status} {e.codes or ''}".strip())
        return None
    except Exception as e:  # noqa: BLE001 — a spike harness; nothing may escape
        LOG("")
        LOG(f"!!! {name} FAILED (non-HTTP): {type(e).__name__}: {e}")
        RESULT["failures"].append(f"{name}: {type(e).__name__}: {e}")
        return None


def main():
    global LOG, TOKEN

    tok = load_token()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logpath = Path(__file__).resolve().parent / f"duffel_reshop_test-{stamp}.log"
    LOG = Log(logpath)
    TOKEN = tok

    LOG.rule("DUFFEL ORDER CHANGE — END-TO-END TEST MODE VALIDATION")
    LOG(f"started        : {datetime.now(timezone.utc).isoformat()}")
    LOG(f"base url       : {BASE}")
    LOG(f"Duffel-Version : {DUFFEL_VERSION}")
    LOG(f"token          : {tok[:17]}...  (TEST MODE CONFIRMED)")
    LOG(f"transcript     : {logpath}")

    search = run_step("step 1 search", step_1_search)
    if not search:
        summary()
        return 1
    _, offers = search

    offer = run_step("step 2 price", step_2_price, offers)
    order = run_step("step 3 create order", step_3_order, offer) if offer else None
    if order:
        run_step("step 4 order change", step_4_order_change, order)
    run_step("step 5 cancel", step_5_cancel)

    summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
