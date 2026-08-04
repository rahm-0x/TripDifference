#!/usr/bin/env python3
"""
Trip Difference — local reshop test rig.

Flask, server-rendered, no build step. localhost only, single operator.

    .venv/bin/python app.py     →  http://localhost:8000

Booked orders live in orders.json (no database, per the original brief).
Nothing that changes or cancels an order happens without an explicit
confirmation step — the habit matters more here than the sandbox money does.

Login / activate are presentation screens. They do not gate anything: this is a
single-operator rig and adding real auth would only get in the way.
"""

import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from flask import (Flask, redirect, render_template, request, send_from_directory,
                   session, url_for)

import duffel_http
import eligibility
import paths
from duffel_http import DuffelError
from engine import (DECISION_LOG, OrderSnapshot, Outcome, ReshopPolicy,
                    evaluate, log_decision, log_eligibility)
from prices import (DuffelPriceSource, Route, SimulatedPriceSource,
                    get_price_source)

HERE = Path(__file__).parent
ORDERS_FILE = paths.data_path("orders.json")
PORT = 8000

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "trip-difference-local-rig")

DEFAULT_POLICY = ReshopPolicy(min_saving=Decimal("20.00"), departure_buffer_hours=24)

# Stand-ins for data a real deployment would hold. Kept in one place so it is
# obvious what is fixture and what comes from Duffel.
USER = {"name": "Amelia Earhart", "email": "amelia@example.com",
        "company": "Northwind Ltd", "initials": "AE"}
CARD = {"brand": "Visa", "last4": "4242", "holder": "Northwind Ltd", "expiry": "09/29"}
INVITE = {"name": "Amelia Earhart", "email": "amelia@example.com",
          "company": "Northwind Ltd", "initials": "AE"}
PROFILE = {"title": "mr", "given_name": "Amelia", "family_name": "Earhart",
           "born_on": "1987-07-24", "gender": "f",
           "email": "amelia@example.com", "phone_number": "+442080160509"}
PASSENGER_FIELDS = ("title", "given_name", "family_name", "born_on", "gender",
                    "email", "phone_number")

# Landing-page shop window. PLACEHOLDER — not live pricing, not observed
# savings. Swap for real data before this page is shown to anyone outside
# the team; see the note in templates/landing.html.
PLACEHOLDER_DEALS = [
    {"from": "London",    "to": "New York",  "now": "389", "was": "620", "save": "231"},
    {"from": "Manchester","to": "Dublin",    "now": "78",  "was": "146", "save": "68"},
    {"from": "Edinburgh", "to": "Amsterdam", "now": "112", "was": "198", "save": "86"},
    {"from": "London",    "to": "Lisbon",    "now": "134", "was": "245", "save": "111"},
    {"from": "Bristol",   "to": "Geneva",    "now": "156", "was": "289", "save": "133"},
    {"from": "Glasgow",   "to": "Paris",     "now": "94",  "was": "173", "save": "79"},
]


@app.context_processor
def inject_globals():
    return {"user": USER, "card": CARD, "policy": DEFAULT_POLICY,
            "profile": PROFILE, "invite": INVITE}


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

def hhmm(iso):
    return iso[11:16] if iso and len(iso) >= 16 else ""


def datelabel(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso[:10]).strftime("%a %-d %b %Y")
    except ValueError:
        return iso[:10]


def minutes_between(a, b):
    try:
        return int((datetime.fromisoformat(b[:19]) - datetime.fromisoformat(a[:19])).total_seconds() // 60)
    except (ValueError, TypeError):
        return 0


def fmt_duration(mins):
    return f"{mins // 60}h {mins % 60:02d}m" if mins else ""


def slice_view(sl):
    """One slice → the shape every itinerary block in the templates expects."""
    segs = sl.get("segments") or []
    if not segs:
        return None
    first, last = segs[0], segs[-1]
    mins = minutes_between(first.get("departing_at"), last.get("arriving_at"))
    return {
        "origin": (sl.get("origin") or {}).get("iata_code", ""),
        "destination": (sl.get("destination") or {}).get("iata_code", ""),
        "depart": hhmm(first.get("departing_at")),
        "arrive": hhmm(last.get("arriving_at")),
        "depart_iso": first.get("departing_at", ""),
        "date": datelabel(first.get("departing_at")),
        "duration": fmt_duration(mins),
        "duration_min": mins,
        "stops": max(len(segs) - 1, 0),
        "next_day": (first.get("departing_at", "")[:10] != last.get("arriving_at", "")[:10]),
        "flight_numbers": " · ".join(
            f"{(s.get('marketing_carrier') or {}).get('iata_code','')}"
            f"{s.get('marketing_carrier_flight_number','')}" for s in segs),
        "fare_brand": sl.get("fare_brand_name"),
    }


def offer_view(offer):
    """
    Duffel offer → view model.

    Eligibility runs through the same assessor as orders, so a fare with a
    disproportionate or unquotable change penalty is never advertised as
    monitored at search time either. Offers carry no available_actions, so that
    gate is skipped here and re-checked once the order exists (FINDINGS.md §8) —
    treat the search-time tag as a prediction, not a guarantee.
    """
    slices = [v for v in (slice_view(s) for s in offer.get("slices", [])) if v]
    a = eligibility.assess(offer)
    first = slices[0] if slices else {}
    return {
        "id": offer["id"],
        "carrier": (offer.get("owner") or {}).get("name", ""),
        "amount": offer["total_amount"],
        "currency": offer["total_currency"],
        "slices": slices,
        "fare_brand": first.get("fare_brand"),
        "monitorable": a.should_poll,
        "eligibility_label": a.label,
        "eligibility_copy": a.customer_copy,
        # flattened, for the results row
        "origin": first.get("origin", ""), "destination": first.get("destination", ""),
        "depart": first.get("depart", ""), "arrive": first.get("arrive", ""),
        "duration": first.get("duration", ""), "duration_min": first.get("duration_min", 0),
        "stops": first.get("stops", 0), "next_day": first.get("next_day", False),
        "flight_numbers": first.get("flight_numbers", ""),
        "depart_sort": re.sub(r"\D", "", first.get("depart", "0")) or "0",
    }


# ---------------------------------------------------------------------------
# order store
# ---------------------------------------------------------------------------

def load_orders():
    return json.loads(ORDERS_FILE.read_text()) if ORDERS_FILE.exists() else []


def save_orders(orders):
    ORDERS_FILE.write_text(json.dumps(orders, indent=2))


def find_order(order_id):
    return next((o for o in load_orders() if o["order_id"] == order_id), None)


def upsert_order(record):
    orders = load_orders()
    for i, o in enumerate(orders):
        if o["order_id"] == record["order_id"]:
            orders[i] = {**o, **record}
            break
    else:
        orders.append(record)
    save_orders(orders)


def snapshot_of(record):
    return OrderSnapshot.from_duffel(record["raw"])


def trip_view(record):
    """
    Stored order record → the shape the employee-facing screens expect.

    Eligibility is recomputed from the raw payload on every read rather than
    trusted from storage, so orders booked before gating existed display
    correctly without a migration. `monitoring` is the AND of the operator
    toggle and eligibility — an ineligible fare can never read as monitored.
    """
    raw = record.get("raw") or {}
    legs = [v for v in (slice_view(s) for s in raw.get("slices", [])) if v]
    first = legs[0] if legs else {}
    snap = snapshot_of(record) if raw else None
    d = record.get("last_decision") or {}
    pax = (raw.get("passengers") or [{}])[0]
    a = eligibility.assess(raw) if raw else None
    return {
        "eligibility": {
            "state": a.state.value, "label": a.label, "reason": a.reason.value,
            "detail": a.detail, "customer_copy": a.customer_copy,
            "penalty": str(a.penalty) if a.penalty is not None else None,
            "penalty_currency": a.penalty_currency,
            "penalty_ratio": (f"{a.penalty_ratio:.1%}" if a.penalty_ratio is not None else None),
            "needs_attention": a.needs_attention, "should_poll": a.should_poll,
        } if a else None,
        "eligible": bool(a and a.should_poll),
        "order_id": record["order_id"],
        "booking_reference": record.get("booking_reference", ""),
        "paid": record.get("paid"), "currency": record.get("currency", "USD"),
        "carrier": record.get("carrier", ""), "itinerary": record.get("itinerary", ""),
        "origin": first.get("origin", ""), "destination": first.get("destination", ""),
        "date_label": first.get("date", record.get("departure_date", "")),
        "depart_iso": first.get("depart_iso", ""),
        "legs": legs,
        "monitoring": bool(record.get("monitoring", False) and a and a.should_poll),
        "changeable": bool(snap.changeable) if snap else False,
        "refunded": record.get("refunded"),
        "original_paid": record.get("original_paid"),
        "last_checked": (d.get("ts") or "")[:19].replace("T", " ") or None,
        "last_decision": d or None,
        "email": pax.get("email"),
        "passenger_name": f"{pax.get('given_name','')} {pax.get('family_name','')}".strip(),
    }


def price_history(order_id):
    """Market prices logged for this order, oldest first. Real data or nothing."""
    if not DECISION_LOG.exists():
        return []
    out = []
    for line in DECISION_LOG.read_text().strip().split("\n"):
        if not line:
            continue
        r = json.loads(line)
        if r.get("order_id") == order_id and r.get("market_best"):
            out.append((r["ts"], Decimal(r["market_best"])))
    return out


def build_chart(order_id, paid):
    """Tiny inline-SVG line chart. Coordinates computed here, drawn in the template."""
    pts = price_history(order_id)
    if len(pts) < 2:
        return None

    w, h, pad = 640, 150, 26
    values = [v for _, v in pts] + [Decimal(paid)]
    lo, hi = min(values), max(values)
    span = hi - lo or Decimal("1")

    def y_of(v):
        return round(float(h - pad - (Decimal(v) - lo) / span * (h - 2 * pad)), 1)

    step = (w - 2 * pad) / (len(pts) - 1)
    dots = [{"x": round(pad + i * step, 1), "y": y_of(v)} for i, (_, v) in enumerate(pts)]
    return {
        "w": w, "h": h, "pad": pad,
        "points": " ".join(f"{d['x']},{d['y']}" for d in dots),
        "dots": dots,
        "paid_y": y_of(paid),
        "first_ts": pts[0][0][5:16].replace("T", " "),
        "last_ts": pts[-1][0][5:16].replace("T", " "),
        "last": str(pts[-1][1]), "low": str(min(v for _, v in pts)), "n": len(pts),
    }


# ---------------------------------------------------------------------------
# auth screens (presentation only — nothing is gated)
# ---------------------------------------------------------------------------

@app.route("/info")
def info():
    """
    Standalone explainer of the booking → monitor → exchange → settle flow.

    Deliberately does not extend base.html — it carries its own type and colour
    system, so it is served whole rather than themed to match the app.
    """
    return render_template("info.html")


@app.route("/logo.png")
def logo():
    """
    Local-dev only. On Vercel, public/logo.png is served by the CDN before a
    request ever reaches this function (docs: don't use Flask's static_folder).
    """
    return send_from_directory(HERE / "public", "logo.png")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        session["signed_in"] = True
        return redirect(url_for("index"))
    return render_template("login.html", hide_nav=True, prefill_email=USER["email"])


@app.route("/activate", methods=["GET", "POST"])
def activate():
    if request.method == "POST":
        if request.form.get("password") != request.form.get("confirm"):
            return render_template("activate.html", hide_nav=True,
                                   error="Those passwords don't match.")
        return redirect(url_for("login"))
    return render_template("activate.html", hide_nav=True)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return render_template("landing.html", deals=PLACEHOLDER_DEALS)


@app.route("/search", methods=["POST"])
def search():
    form = {
        "origin": request.form.get("origin", "").strip().upper(),
        "destination": request.form.get("destination", "").strip().upper(),
        "date": request.form.get("date", "").strip(),
        "return_date": request.form.get("return_date", "").strip(),
        "cabin": request.form.get("cabin", "economy"),
        "trip_type": request.form.get("trip_type", "one_way"),
    }
    if not (form["origin"] and form["destination"] and form["date"]):
        return render_template("results.html", nav="search", offers=None, form=form,
                               error="Origin, destination and departure date are required.")

    slices = [{"origin": form["origin"], "destination": form["destination"],
               "departure_date": form["date"]}]
    if form["trip_type"] == "round_trip" and form["return_date"]:
        slices.append({"origin": form["destination"], "destination": form["origin"],
                       "departure_date": form["return_date"]})

    try:
        # Fresh offer request every search — they are single use (FINDINGS.md §4).
        data = duffel_http.request("POST", "/air/offer_requests", body={
            "data": {"slices": slices, "passengers": [{"type": "adult"}],
                     "cabin_class": form["cabin"]}
        }, params={"return_offers": "true"}, label="ui_search")
    except (DuffelError, RuntimeError) as exc:
        return render_template("results.html", nav="search", offers=None, form=form, error=str(exc))

    raw = sorted(data.get("offers", []), key=lambda x: Decimal(x["total_amount"]))
    offers = [offer_view(o) for o in raw[:20]]
    return render_template("results.html", nav="search", offers=offers, form=form,
                           total=len(raw),
                           unmonitorable=sum(1 for o in offers if not o["monitorable"]))


# ---------------------------------------------------------------------------
# booking flow: passenger → payment → book → confirmation
# ---------------------------------------------------------------------------

def fetch_offer_view(offer_id):
    return offer_view(duffel_http.request("GET", f"/air/offers/{offer_id}", label="ui_offer"))


@app.route("/book/passenger", methods=["POST"])
def passenger_step():
    offer_id = request.form["offer_id"]
    try:
        return render_template("passenger.html", nav="search", offer=fetch_offer_view(offer_id))
    except (DuffelError, RuntimeError) as exc:
        return render_template("results.html", nav="search", offers=None, form={},
                               error=f"{exc} — offers expire; search again.")


@app.route("/book/payment", methods=["POST"])
def payment_step():
    offer_id = request.form["offer_id"]
    passenger = {k: request.form.get(k, PROFILE[k]) for k in PASSENGER_FIELDS}
    try:
        return render_template("payment.html", nav="search",
                               offer=fetch_offer_view(offer_id), passenger=passenger)
    except (DuffelError, RuntimeError) as exc:
        return render_template("results.html", nav="search", offers=None, form={},
                               error=f"{exc} — offers expire; search again.")


@app.route("/book", methods=["POST"])
def book():
    offer_id = request.form["offer_id"]
    passenger = {k: request.form.get(k) or PROFILE[k] for k in PASSENGER_FIELDS}
    try:
        offer = duffel_http.request("GET", f"/air/offers/{offer_id}", label="ui_offer")
        order = duffel_http.request("POST", "/air/orders", body={
            "data": {
                "type": "instant",
                "selected_offers": [offer_id],
                "passengers": [{"id": p["id"], **passenger} for p in offer.get("passengers", [])],
                "payments": [{"type": "balance", "currency": offer["total_currency"],
                              "amount": offer["total_amount"]}],
            }
        }, label="ui_book")
    except (DuffelError, RuntimeError) as exc:
        hint = ""
        if isinstance(exc, DuffelError) and "offer_request_already_booked" in (exc.codes or []):
            hint = (" — an offer request is single use; this search has already been "
                    "booked from. Search again for fresh offers.")
        return render_template("results.html", nav="search", offers=None, form={},
                               error=str(exc) + hint)

    snap = OrderSnapshot.from_duffel(order)

    # Gate monitoring on fare conditions at booking time. Never default to on.
    assessment = snap.eligibility
    log_eligibility(order["id"], assessment)

    upsert_order({
        "order_id": order["id"],
        "booking_reference": order.get("booking_reference", ""),
        "paid": order["total_amount"], "currency": order["total_currency"],
        "route": str(snap.route), "itinerary": str(snap.itinerary),
        "carrier": snap.carrier_name, "departure_date": snap.departure_date,
        "monitoring": assessment.should_poll,
        "booked_at": datetime.now(timezone.utc).isoformat(),
        "last_decision": None, "raw": order,
    })

    # Seed the simulated scenario from reality, so simulation starts at the
    # sandbox constant (+125.00) rather than an accidental fake drop.
    SimulatedPriceSource().set_scenario(
        order["id"],
        carrier=snap.itinerary.carrier_iata,
        flight_numbers=list(snap.itinerary.flight_numbers),
        currency=order["total_currency"], route=str(snap.route),
        market_price=order["total_amount"], change_total="125.00",
        new_total=str(Decimal(order["total_amount"]) + Decimal("100.00")),
        penalty="25.00",
    )
    return redirect(url_for("trip_booked", order_id=order["id"]))


@app.route("/trips/<order_id>/booked")
def trip_booked(order_id):
    record = find_order(order_id)
    if not record:
        return redirect(url_for("trips"))
    return render_template("confirm.html", nav="trips", trip=trip_view(record))


# ---------------------------------------------------------------------------
# employee views
# ---------------------------------------------------------------------------

@app.route("/trips")
def trips():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    upcoming, past, saved, count = [], [], Decimal("0"), 0
    for record in load_orders():
        t = trip_view(record)
        (upcoming if (t["depart_iso"][:10] or "9999") >= today else past).append(t)
        if t["refunded"]:
            saved += Decimal(t["refunded"])
            count += 1
    upcoming.sort(key=lambda t: t["depart_iso"])
    past.sort(key=lambda t: t["depart_iso"], reverse=True)
    return render_template("trips.html", nav="trips", upcoming=upcoming, past=past,
                           saved_total=(str(saved) if count else None),
                           saved_currency=(upcoming + past)[0]["currency"] if (upcoming or past) else "USD",
                           saved_count=count)


@app.route("/trips/<order_id>")
def trip_detail(order_id):
    record = find_order(order_id)
    if not record:
        return redirect(url_for("trips"))
    trip = trip_view(record)
    return render_template("trip.html", nav="trips", trip=trip,
                           chart=build_chart(order_id, trip["paid"]))


# ---------------------------------------------------------------------------
# ops
# ---------------------------------------------------------------------------

@app.route("/orders")
def orders():
    records = load_orders()
    sim = SimulatedPriceSource()
    for r in records:
        sc = sim.scenario(r["order_id"]) or {}
        r["sim_market"] = sc.get("market_price")
        r["sim_change_total"] = sc.get("change_total")
    return render_template("orders.html", nav="ops", orders=records,
                           source_name=os.environ.get("PRICE_SOURCE", "simulated"))


@app.route("/orders/<order_id>/monitor", methods=["POST"])
def toggle_monitor(order_id):
    record = find_order(order_id)
    if record:
        # Monitoring can always be turned off, but never on for a fare that
        # cannot win — the toggle must not be able to re-create the bug.
        a = eligibility.assess(record.get("raw") or {})
        wanted = not record.get("monitoring", True)
        upsert_order({"order_id": order_id, "monitoring": wanted and a.should_poll})
    if request.form.get("next") == "trip":
        return redirect(url_for("trip_detail", order_id=order_id))
    return redirect(url_for("orders"))


def _run_cycle(order_id, source):
    record = find_order(order_id)
    if not record:
        return None
    d = evaluate(snapshot_of(record), source, policy=DEFAULT_POLICY)
    upsert_order({"order_id": order_id, "last_decision": {
        "ts": d.ts, "source": d.source, "outcome": d.outcome.value, "reason": d.reason.value,
        "detail": d.detail,
        "market_best": str(d.market_best) if d.market_best is not None else None,
        "market_delta": str(d.market_delta) if d.market_delta is not None else None,
        "change_total": str(d.change_total) if d.change_total is not None else None,
        "change_offer_id": d.change_offer_id,
        "saving": str(d.saving) if d.saving is not None else None,
        "floor": str(d.floor),
        # Execution is a separate axis from the decision.
        "execution": d.execution.value,
        "execution_detail": d.execution_detail,
        "recovered": str(d.recovered) if d.recovered is not None else None,
        "service_fee": str(d.service_fee) if d.service_fee is not None else None,
        "net_to_customer": str(d.net_to_customer) if d.net_to_customer is not None else None,
    }})
    return d


@app.route("/orders/<order_id>/simulate", methods=["POST"])
def simulate(order_id):
    if not find_order(order_id):
        return redirect(url_for("orders"))
    sim = SimulatedPriceSource()
    fields = {}
    for key in ("market_price", "change_total", "new_total", "penalty"):
        value = request.form.get(key, "").strip()
        if value:
            try:
                Decimal(value)
            except Exception:
                return redirect(url_for("orders"))
            fields[key] = value
    if fields:
        sim.set_scenario(order_id, **fields)
    _run_cycle(order_id, sim)
    return redirect(url_for("orders"))


@app.route("/orders/<order_id>/cycle", methods=["POST"])
def cycle(order_id):
    source = get_price_source(request.form.get("source") or "duffel")
    try:
        _run_cycle(order_id, source)
    except (DuffelError, RuntimeError) as exc:
        if find_order(order_id):
            upsert_order({"order_id": order_id, "last_decision": {
                "ts": datetime.now(timezone.utc).isoformat(), "source": source.name,
                "outcome": "error", "reason": "api_error", "detail": str(exc),
                "market_best": None, "market_delta": None, "change_total": None,
                "change_offer_id": "", "saving": None, "floor": str(DEFAULT_POLICY.min_saving),
            }})
    return redirect(url_for("orders"))


# ---------------------------------------------------------------------------
# execution — always two steps
# ---------------------------------------------------------------------------

@app.route("/orders/<order_id>/confirm/<action>", methods=["GET"])
def confirm_action(order_id, action):
    """Step 1 of 2. Nothing has happened at this point."""
    record = find_order(order_id)
    if not record or action not in ("exchange", "cancel"):
        return redirect(url_for("orders"))
    return render_template("confirm_action.html", nav="ops", order=record, action=action)


@app.route("/orders/<order_id>/execute/<action>", methods=["POST"])
def execute(order_id, action):
    """Step 2 of 2. Requires the typed confirmation from the previous page."""
    record = find_order(order_id)
    if not record:
        return redirect(url_for("orders"))

    def refuse(msg):
        return render_template("confirm_action.html", nav="ops", order=record,
                               action=action, error=msg)

    if request.form.get("confirm_text", "").strip().upper() != "CONFIRM":
        return refuse("Type CONFIRM exactly to proceed.")

    last = record.get("last_decision") or {}
    if last.get("source") == "simulated":
        return refuse("Last decision came from the simulated source. "
                      "Run a live cycle before executing anything real.")

    extra = {}
    try:
        if action == "exchange":
            offer_id = last.get("change_offer_id")
            if not offer_id:
                raise RuntimeError("no change offer on the last decision — run a live cycle first")
            change = duffel_http.request("POST", "/air/order_changes", body={
                "data": {"selected_order_change_offer": offer_id}}, label="ui_change_create")
            delta = Decimal(change["change_total_amount"])
            # Docs: no payment object needed when change_total <= 0.
            body = {"data": {}}
            if delta > 0:
                body = {"data": {"payment": {"type": "balance",
                                             "currency": change["change_total_currency"],
                                             "amount": str(delta)}}}
            result = duffel_http.request(
                "POST", f"/air/order_changes/{change['id']}/actions/confirm",
                body=body, label="ui_change_confirm")
            note = f"exchange confirmed at {result.get('confirmed_at')}, change_total {delta}"
            if delta < 0:
                extra = {"refunded": str(-delta), "original_paid": record.get("paid")}
        else:
            quote = duffel_http.request("POST", "/air/order_cancellations", body={
                "data": {"order_id": order_id}}, label="ui_cancel_quote")
            result = duffel_http.request(
                "POST", f"/air/order_cancellations/{quote['id']}/actions/confirm",
                body={"data": {}}, label="ui_cancel_confirm")
            note = (f"cancelled at {result.get('confirmed_at')}, "
                    f"refunded {result.get('refund_amount')} {result.get('refund_currency')}")
    except (DuffelError, RuntimeError) as exc:
        return refuse(str(exc))

    fresh = duffel_http.request("GET", f"/air/orders/{order_id}", label="ui_order_refresh")
    upsert_order({"order_id": order_id, "raw": fresh, "monitoring": False,
                  "executed": note, "paid": fresh["total_amount"], **extra})
    return redirect(url_for("trip_detail", order_id=order_id))


@app.route("/decisions")
def decisions():
    rows = []
    if DECISION_LOG.exists():
        rows = [json.loads(l) for l in DECISION_LOG.read_text().strip().split("\n") if l]
    return render_template("decisions.html", nav="log", rows=list(reversed(rows))[:200])


if __name__ == "__main__":
    print(f"Trip Difference → http://localhost:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False)
