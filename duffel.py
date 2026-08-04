#!/usr/bin/env python3
"""
Duffel API validation harness — test mode only.

A deliberately dumb spike. No abstractions, no retry framework, no client class
hierarchy. One request helper, one command per Duffel flow. Every raw response
is dumped to ./responses/ so the shapes can be inspected by hand.

Money is always Decimal. Duffel returns amounts as strings ("90.80"); float
would silently lose cents.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path

import click
import requests
from dotenv import load_dotenv

BASE = "https://api.duffel.com"
RESPONSES = Path(__file__).parent / "responses"

# Hardcoded test passenger. Duffel test mode does not validate these against
# anything real, but born_on must make the passenger an adult.
TEST_PASSENGER = {
    "title": "mr",
    "given_name": "Amelia",
    "family_name": "Earhart",
    "born_on": "1987-07-24",
    "gender": "f",
    "email": "amelia@example.com",
    "phone_number": "+442080160509",
}


def token():
    """Load and sanity-check the access token. Refuses live tokens outright."""
    load_dotenv()
    tok = os.environ.get("DUFFEL_TOKEN", "").strip()
    if not tok:
        die("DUFFEL_TOKEN not set. Copy .env.example to .env and add your test token.")
    if tok.startswith("duffel_live_"):
        die("Refusing to run: DUFFEL_TOKEN is a LIVE token. This harness is test mode only.")
    if not tok.startswith("duffel_test_"):
        die(f"Refusing to run: DUFFEL_TOKEN does not start with 'duffel_test_' (got '{tok[:14]}...').")
    return tok


def die(msg):
    click.echo(click.style(f"error: {msg}", fg="red"), err=True)
    sys.exit(1)


def dump(name, payload):
    """Write a raw response to ./responses/ for inspection."""
    RESPONSES.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RESPONSES / f"{stamp}-{name}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    click.echo(click.style(f"  ↳ raw response: {path.relative_to(Path.cwd())}", fg="bright_black"))
    return path


def api(method, path, *, body=None, params=None, label=None):
    """
    One request helper for the whole harness.

    Handles 429 by honouring Retry-After if present, else the ratelimit-reset
    date header that Duffel actually documents. Retries a 429 exactly once —
    this is a spike, not a resilience layer.
    """
    url = f"{BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token()}",
        "Duffel-Version": "v2",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    for attempt in (1, 2):
        resp = requests.request(method, url, headers=headers, json=body, params=params, timeout=60)

        if resp.status_code == 429 and attempt == 1:
            wait = _rate_limit_wait(resp)
            click.echo(click.style(f"  429 rate limited; sleeping {wait:.0f}s", fg="yellow"), err=True)
            time.sleep(wait)
            continue

        try:
            payload = resp.json()
        except ValueError:
            die(f"{method} {path} → HTTP {resp.status_code}, non-JSON body:\n{resp.text[:2000]}")

        dump(label or path.strip("/").replace("/", "_"), payload)

        if not resp.ok:
            click.echo(click.style(f"HTTP {resp.status_code} on {method} {path}", fg="red"), err=True)
            for err in payload.get("errors", []):
                click.echo(click.style(f"  [{err.get('type')}/{err.get('code')}] {err.get('title')}", fg="red"), err=True)
                click.echo(click.style(f"    {err.get('message')}", fg="red"), err=True)
                if err.get("source"):
                    click.echo(click.style(f"    source: {err['source']}", fg="red"), err=True)
                if err.get("documentation_url"):
                    click.echo(click.style(f"    docs: {err['documentation_url']}", fg="red"), err=True)
            sys.exit(1)

        return payload["data"]

    die("rate limited twice in a row; giving up")


def _rate_limit_wait(resp):
    """Duffel documents ratelimit-reset (an RFC 2616 date). Retry-After is checked first anyway."""
    if resp.headers.get("Retry-After"):
        try:
            return max(1.0, float(resp.headers["Retry-After"]))
        except ValueError:
            pass
    reset = resp.headers.get("ratelimit-reset")
    if reset:
        try:
            delta = (parsedate_to_datetime(reset) - datetime.now(timezone.utc)).total_seconds()
            return max(1.0, min(delta, 120.0))
        except (TypeError, ValueError):
            pass
    return 5.0


def money(amount, currency):
    """Duffel amounts are strings. Decimal, never float."""
    if amount is None:
        return None, None
    return Decimal(str(amount)), currency


def fmt(amount, currency, signed=False):
    dec, cur = money(amount, currency)
    if dec is None:
        return "n/a"
    sign = "+" if (signed and dec > 0) else ""
    return f"{sign}{dec} {cur}"


def describe_segments(slices):
    """Flatten an offer's slices into 'ZZ 4originflight LHR→JFK' style lines."""
    out = []
    for sl in slices:
        for seg in sl.get("segments", []):
            carrier = seg.get("marketing_carrier") or {}
            code = carrier.get("iata_code", "??")
            num = seg.get("marketing_carrier_flight_number", "?")
            origin = (seg.get("origin") or {}).get("iata_code", "???")
            dest = (seg.get("destination") or {}).get("iata_code", "???")
            dep = seg.get("departing_at", "")
            out.append(f"{code}{num} {origin}→{dest} {dep}")
    return out


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

@click.group()
def cli():
    """Duffel API validation harness (test mode only)."""


@cli.command()
@click.argument("origin")
@click.argument("destination")
@click.argument("departure_date")
@click.option("--cabin", default="economy", help="economy | premium_economy | business | first")
@click.option("--limit", default=10, help="How many offers to print.")
def search(origin, destination, departure_date, cabin, limit):
    """Search offers: search LAS LAX 2026-10-15"""
    body = {
        "data": {
            "slices": [{
                "origin": origin.upper(),
                "destination": destination.upper(),
                "departure_date": departure_date,
            }],
            "passengers": [{"type": "adult"}],
            "cabin_class": cabin,
        }
    }
    click.echo(f"POST /air/offer_requests  {origin.upper()}→{destination.upper()} {departure_date} ({cabin})")
    data = api("POST", "/air/offer_requests", body=body,
               params={"return_offers": "true"}, label="offer_request")

    offers = data.get("offers", [])
    click.echo(f"\noffer_request id: {data['id']}")
    click.echo(f"offers returned: {len(offers)}\n")
    if not offers:
        click.echo("No offers. Try a different date or route.")
        return

    offers = sorted(offers, key=lambda o: Decimal(o["total_amount"]))
    for o in offers[:limit]:
        segs = describe_segments(o.get("slices", []))
        owner = (o.get("owner") or {}).get("name", "?")
        click.echo(click.style(f"{o['id']}", fg="cyan"))
        click.echo(f"  {owner:<20} {fmt(o['total_amount'], o['total_currency']):>14}")
        for s in segs:
            click.echo(f"    {s}")
        click.echo()

    cheapest = offers[0]
    click.echo(click.style(f"cheapest: {cheapest['id']}  {fmt(cheapest['total_amount'], cheapest['total_currency'])}", fg="green"))


@cli.command()
@click.argument("offer_id")
def book(offer_id):
    """Create an order from an offer with the hardcoded test passenger."""
    click.echo(f"GET /air/offers/{offer_id}")
    offer = api("GET", f"/air/offers/{offer_id}", label="offer_detail")

    # Order passengers must carry the ids the offer assigned.
    passengers = []
    for p in offer.get("passengers", []):
        passengers.append({"id": p["id"], **TEST_PASSENGER})

    body = {
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
    }
    click.echo("POST /air/orders")
    order = api("POST", "/air/orders", body=body, label="order_create")

    click.echo(click.style(f"\norder id : {order['id']}", fg="green"))
    click.echo(click.style(f"PNR      : {order.get('booking_reference')}", fg="green", bold=True))
    click.echo(f"total    : {fmt(order['total_amount'], order['total_currency'])}")
    click.echo(f"owner    : {(order.get('owner') or {}).get('name')}")
    click.echo(f"actions  : {order.get('available_actions')}")
    click.echo("\nslices:")
    for sl in order.get("slices", []):
        click.echo(f"  slice_id: {sl['id']}")
        for s in describe_segments([sl]):
            click.echo(f"    {s}")


@cli.command()
@click.argument("order_id")
def show(order_id):
    """Full order detail."""
    click.echo(f"GET /air/orders/{order_id}")
    order = api("GET", f"/air/orders/{order_id}", label="order_show")

    click.echo(f"\nid              : {order['id']}")
    click.echo(f"PNR             : {order.get('booking_reference')}")
    click.echo(f"total           : {fmt(order['total_amount'], order['total_currency'])}")
    click.echo(f"owner           : {(order.get('owner') or {}).get('name')}")
    click.echo(f"live_mode       : {order.get('live_mode')}")
    click.echo(f"cancelled_at    : {order.get('cancelled_at')}")
    click.echo(f"available_actions: {order.get('available_actions')}")
    click.echo(f"conditions      : {json.dumps(order.get('conditions'), indent=2)}")
    click.echo("\nslices:")
    for sl in order.get("slices", []):
        click.echo(f"  slice_id: {sl['id']}  (changeable={((sl.get('conditions') or {}).get('change_before_departure') or {})})")
        for s in describe_segments([sl]):
            click.echo(f"    {s}")
    click.echo("\npassengers:")
    for p in order.get("passengers", []):
        click.echo(f"  {p['id']}  {p.get('given_name')} {p.get('family_name')}")


@cli.command()
@click.argument("order_id")
def reshop(order_id):
    """
    The one that matters.

    Re-search the order's own route, then actually price the change through
    order_change_requests so we see a real change_total_amount rather than a
    naive search-price delta.
    """
    click.echo(f"GET /air/orders/{order_id}")
    order = api("GET", f"/air/orders/{order_id}", label="reshop_order")

    paid, cur = money(order["total_amount"], order["total_currency"])
    click.echo(f"\norder paid: {fmt(order['total_amount'], order['total_currency'])}")

    sl = order["slices"][0]
    origin = sl["origin"]["iata_code"]
    dest = sl["destination"]["iata_code"]
    dep_date = sl["segments"][0]["departing_at"][:10]
    click.echo(f"route     : {origin}→{dest} {dep_date}  (slice {sl['id']})")

    # 1. naive market re-search, for reference only
    click.echo(f"\n--- market re-search {origin}→{dest} {dep_date} ---")
    search_data = api("POST", "/air/offer_requests", body={
        "data": {
            "slices": [{"origin": origin, "destination": dest, "departure_date": dep_date}],
            "passengers": [{"type": "adult"} for _ in order.get("passengers", [{}])],
            "cabin_class": "economy",
        }
    }, params={"return_offers": "true"}, label="reshop_search")

    offers = sorted(search_data.get("offers", []), key=lambda o: Decimal(o["total_amount"]))
    if offers:
        best = offers[0]
        best_amt, _ = money(best["total_amount"], best["total_currency"])
        click.echo(f"cheapest market offer: {best['id']}  {fmt(best['total_amount'], best['total_currency'])}")
        click.echo(f"naive market delta   : {fmt(best_amt - paid, cur, signed=True)}")
    else:
        click.echo("no market offers returned")

    # 2. the real question: what does the airline actually charge to change?
    click.echo(f"\n--- pricing the change via /air/order_change_requests ---")
    ocr = api("POST", "/air/order_change_requests", body={
        "data": {
            "order_id": order_id,
            "slices": {
                "remove": [{"slice_id": sl["id"]}],
                "add": [{
                    "origin": origin,
                    "destination": dest,
                    "departure_date": dep_date,
                    "cabin_class": "economy",
                }],
            },
        }
    }, label="reshop_order_change_request")

    _print_change_offers(ocr, paid, cur)


@cli.command()
@click.argument("order_id")
@click.argument("slice_id")
@click.option("--date", default=None, help="Departure date for the replacement slice (default: same day).")
@click.option("--cabin", default="economy")
def change(order_id, slice_id, date, cabin):
    """Create an order change request and list change offers."""
    click.echo(f"GET /air/orders/{order_id}")
    order = api("GET", f"/air/orders/{order_id}", label="change_order")
    paid, cur = money(order["total_amount"], order["total_currency"])

    sl = next((s for s in order["slices"] if s["id"] == slice_id), None)
    if sl is None:
        die(f"slice {slice_id} not found on order. Available: {[s['id'] for s in order['slices']]}")

    origin = sl["origin"]["iata_code"]
    dest = sl["destination"]["iata_code"]
    dep_date = date or sl["segments"][0]["departing_at"][:10]

    click.echo(f"POST /air/order_change_requests  remove {slice_id}, add {origin}→{dest} {dep_date}")
    ocr = api("POST", "/air/order_change_requests", body={
        "data": {
            "order_id": order_id,
            "slices": {
                "remove": [{"slice_id": slice_id}],
                "add": [{
                    "origin": origin,
                    "destination": dest,
                    "departure_date": dep_date,
                    "cabin_class": cabin,
                }],
            },
        }
    }, label="order_change_request")

    _print_change_offers(ocr, paid, cur)


def _print_change_offers(ocr, paid, cur):
    """Shared rendering for order change offers — the core output of this spike."""
    click.echo(f"\norder_change_request id: {ocr['id']}")
    offers = ocr.get("order_change_offers", [])
    click.echo(f"order_change_offers    : {len(offers)}\n")

    if not offers:
        click.echo(click.style("NO CHANGE OFFERS RETURNED — this carrier/route may not support changes.", fg="yellow"))
        return

    offers = sorted(offers, key=lambda o: Decimal(o["change_total_amount"] or "0"))
    negative = 0
    for o in offers:
        delta, dcur = money(o.get("change_total_amount"), o.get("change_total_currency"))
        colour = "green" if (delta is not None and delta < 0) else "white"
        if delta is not None and delta < 0:
            negative += 1
        click.echo(click.style(f"{o['id']}", fg="cyan"))
        click.echo(click.style(f"  change_total  : {fmt(o.get('change_total_amount'), dcur, signed=True)}", fg=colour, bold=True))
        click.echo(f"  new_total     : {fmt(o.get('new_total_amount'), o.get('new_total_currency'))}")
        click.echo(f"  penalty_total : {fmt(o.get('penalty_total_amount'), o.get('penalty_total_currency'))}")
        click.echo(f"  refund_to     : {o.get('refund_to')}")
        click.echo(f"  expires_at    : {o.get('expires_at')}")
        for s in describe_segments((o.get("slices") or {}).get("add", [])):
            click.echo(f"    add: {s}")
        click.echo()

    click.echo(f"originally paid : {fmt(paid, cur)}")
    click.echo(click.style(
        f"negative change_total_amount offers: {negative} of {len(offers)}",
        fg="green" if negative else "yellow", bold=True))


@cli.command("confirm-change")
@click.argument("change_offer_id")
def confirm_change(change_offer_id):
    """Execute the exchange for a given order change offer."""
    click.echo(f"GET /air/order_change_offers/{change_offer_id}")
    offer = api("GET", f"/air/order_change_offers/{change_offer_id}", label="change_offer_detail")
    delta, dcur = money(offer.get("change_total_amount"), offer.get("change_total_currency"))
    click.echo(f"change_total: {fmt(delta, dcur, signed=True)}")

    click.echo("POST /air/order_changes")
    oc = api("POST", "/air/order_changes",
             body={"data": {"selected_order_change_offer": change_offer_id}},
             label="order_change_create")
    click.echo(f"order_change id: {oc['id']}")

    # Docs: "If change_total_amount is zero or negative, there is no need to
    # pass a payment object."
    body = {"data": {}}
    if delta is not None and delta > 0:
        body = {"data": {"payment": {"type": "balance", "currency": dcur, "amount": str(delta)}}}
        click.echo(f"paying {fmt(delta, dcur)} to confirm")
    else:
        click.echo("no payment object required (change_total_amount <= 0)")

    click.echo(f"POST /air/order_changes/{oc['id']}/actions/confirm")
    confirmed = api("POST", f"/air/order_changes/{oc['id']}/actions/confirm",
                    body=body, label="order_change_confirm")

    click.echo(click.style(f"\nconfirmed_at : {confirmed.get('confirmed_at')}", fg="green"))
    click.echo(f"change_total : {fmt(confirmed.get('change_total_amount'), confirmed.get('change_total_currency'), signed=True)}")
    click.echo(f"new_total    : {fmt(confirmed.get('new_total_amount'), confirmed.get('new_total_currency'))}")
    click.echo(f"refund_to    : {confirmed.get('refund_to')}")


@cli.command()
@click.argument("order_id")
@click.option("-y", "--yes", is_flag=True, help="Actually confirm the cancellation.")
def cancel(order_id, yes):
    """Quote the refund first; only cancel for real with -y."""
    click.echo("POST /air/order_cancellations")
    quote = api("POST", "/air/order_cancellations",
                body={"data": {"order_id": order_id}}, label="cancellation_quote")

    click.echo(click.style(f"\ncancellation id: {quote['id']}", fg="cyan"))
    click.echo(f"refund_amount  : {fmt(quote.get('refund_amount'), quote.get('refund_currency'))}")
    click.echo(f"refund_to      : {quote.get('refund_to')}")
    click.echo(f"expires_at     : {quote.get('expires_at')}")
    click.echo(f"confirmed_at   : {quote.get('confirmed_at')}")

    if not yes:
        click.echo(click.style("\nquote only — re-run with -y to confirm the cancellation.", fg="yellow"))
        return

    click.echo(f"\nPOST /air/order_cancellations/{quote['id']}/actions/confirm")
    done = api("POST", f"/air/order_cancellations/{quote['id']}/actions/confirm",
               body={"data": {}}, label="cancellation_confirm")
    click.echo(click.style(f"confirmed_at: {done.get('confirmed_at')}", fg="green"))
    click.echo(f"refunded    : {fmt(done.get('refund_amount'), done.get('refund_currency'))}")


if __name__ == "__main__":
    cli()
