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

from flask import (Flask, redirect, render_template, request,
                   send_from_directory, url_for)

import auth
import db
import duffel_http
import eligibility
import paths
from duffel_http import DuffelError
from engine import (OrderSnapshot, Outcome, ReshopPolicy,
                    evaluate, log_decision, log_eligibility)
from prices import (DuffelPriceSource, Route, SimulatedPriceSource,
                    get_price_source)

HERE = Path(__file__).parent
PORT = 8000

# Divert the audit trail from decisions.log into Postgres. engine.py keeps its
# file behaviour when handed an explicit path, which is what the tests use.
import engine as _engine
_engine.SINK = db.audit_append

app = Flask(__name__)
# Session cookies are signed with this. A known fallback in production would
# let anyone forge a session, so outside local development its absence is fatal
# rather than quietly insecure.
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    if paths.ON_VERCEL:
        raise RuntimeError("SECRET_KEY must be set — refusing to sign sessions "
                           "with a publicly known key")
    _secret = "trip-difference-local-rig"
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    # Lax still sends the cookie on top-level GET navigation, so the
    # "sign in then land where you were going" flow keeps working, while
    # cross-site POSTs arrive without it.
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=paths.ON_VERCEL,
    PERMANENT_SESSION_LIFETIME=db.SESSION_TTL,
)

DEFAULT_POLICY = ReshopPolicy(min_saving=Decimal("20.00"), departure_buffer_hours=24)

# Stand-ins for data a real deployment would hold. Kept in one place so it is
# obvious what is fixture and what comes from Duffel.
CARD = {"brand": "Visa", "last4": "4242", "holder": "Northwind Ltd", "expiry": "09/29"}
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


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.before_request
def _csrf_guard():
    """Every state-changing request, without exception. A per-form hidden field
    is something you forget on the one form that matters."""
    if request.method in UNSAFE_METHODS and not auth.csrf_ok():
        return render_template("error.html",
                               hide_nav=True,
                               error="That form expired or came from somewhere "
                                     "else. Go back and try again."), 403


@app.context_processor
def inject_globals():
    user = auth.current_user()
    return {"user": auth.view_model(user), "card": CARD,
            "policy": DEFAULT_POLICY, "profile": auth.profile_of(user),
            "csrf_token": auth.csrf_token()}


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
        # the offer is priced for this many people; the passenger step renders
        # one card each and the order must name every one of them
        "passenger_count": len(offer.get("passengers", [])) or 1,
    }


# ---------------------------------------------------------------------------
# order store
# ---------------------------------------------------------------------------

def _account():
    """Every order and view is scoped to the signed-in user's account."""
    user = auth.current_user()
    return user["account_id"] if user else None


def load_orders():
    return db.load_orders(_account())


def find_order(order_id):
    return db.find_order(order_id, _account())


def upsert_order(record):
    return db.upsert_order(record, _account())


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
        # Every person on the booking. `pax` above is only the lead traveller,
        # which is why a group booking used to render as one name.
        "passengers": [{
            "name": " ".join(x for x in ((p.get("title") or "").capitalize(),
                                         p.get("given_name", ""),
                                         p.get("family_name", "")) if x).strip(),
            "born_on": p.get("born_on") or "",
            "email": p.get("email") or "",
            "phone_number": p.get("phone_number") or "",
        } for p in (raw.get("passengers") or [])],
    }


def price_history(order_id):
    """Market prices logged for this order, oldest first. Real data or nothing."""
    out = []
    for r in reversed(db.audit_rows(limit=500, order_id=order_id)):
        if r.get("market_best"):
            out.append((r["ts"], Decimal(r["market_best"])))
    return out


# --- dashboard chart geometry -------------------------------------------
# Computed here rather than in the template so the SVG stays declarative, and
# in the browser-free tests the numbers can be asserted directly.
#
# Series colours are the validated categorical pair, not the brand accents:
# TripDifference gold against its green fails colour-vision separation
# (protan dE 5.4, normal 14.3 — below the 15 floor). Blue/amber clears it at
# 27.4 / 30.7. Run scripts/validate_palette.js before changing either.
SERIES_SPEND = "#3987e5"
SERIES_SAVED = "#C98500"

CHART_W, CHART_H = 720, 190
PAD_L, PAD_R, PAD_T, PAD_B = 48, 14, 14, 34


def _nice_top(value):
    """A round axis maximum, so gridlines land on readable numbers."""
    if value <= 0:
        return 100
    import math
    mag = 10 ** int(math.floor(math.log10(value)))
    for step in (1, 2, 2.5, 5, 10):
        if value <= mag * step:
            return int(mag * step)
    return int(mag * 10)


def spend_chart(rows):
    """Two money series on ONE axis — both are the same currency, so a second
    scale would invent a relationship that is not in the data."""
    top = _nice_top(max([float(r["spend"]) for r in rows]
                        + [float(r["recovered"]) for r in rows] + [0]))
    inner_w = CHART_W - PAD_L - PAD_R
    inner_h = CHART_H - PAD_T - PAD_B
    n = max(len(rows) - 1, 1)

    def pts(key):
        out = []
        for i, r in enumerate(rows):
            x = PAD_L + inner_w * i / n
            y = PAD_T + inner_h * (1 - float(r[key]) / top)
            out.append({"x": round(x, 1), "y": round(y, 1),
                        "label": r["label"], "value": f"{float(r[key]):,.2f}"})
        return out

    grid = [{"y": round(PAD_T + inner_h * (1 - f), 1),
             "label": f"{int(top * f):,}"} for f in (0, .25, .5, .75, 1)]
    return {"spend": pts("spend"), "saved": pts("recovered"), "grid": grid,
            "top": top, "w": CHART_W, "h": CHART_H,
            "has_data": any(float(r["spend"]) or float(r["recovered"]) for r in rows)}


def activity_chart(rows, w=720, h=150):
    """One series, so one colour for every bar — a value ramp here would
    double-encode height as hue."""
    top = max([r["checks"] for r in rows] + [1])
    pad_l, pad_b, pad_t = 34, 24, 10
    inner_w, inner_h = w - pad_l - 10, h - pad_b - pad_t
    # 2px of surface between neighbours rather than a stroke around each bar
    slot = inner_w / max(len(rows), 1)
    bw = max(slot - 8, 6)
    bars = []
    for i, r in enumerate(rows):
        bh = inner_h * (r["checks"] / top)
        bars.append({"x": round(pad_l + slot * i + (slot - bw) / 2, 1),
                     "y": round(pad_t + inner_h - bh, 1),
                     "w": round(bw, 1), "h": round(max(bh, 0), 1),
                     "label": r["label"], "checks": r["checks"],
                     "reshops": r["reshops"]})
    return {"bars": bars, "top": top, "w": w, "h": h,
            "baseline": round(pad_t + inner_h, 1),
            "has_data": any(r["checks"] for r in rows)}


def carrier_chart(rows, w=720, row_h=30):
    """Horizontal bars, ranked. Not a donut: this is a comparison, and a pie of
    close values is unreadable — with a single carrier it would be one 100%
    slice, which is a stat tile, not a chart."""
    total = sum(float(r["spend"]) for r in rows) or 1.0
    biggest = max([float(r["spend"]) for r in rows] + [1.0])
    label_w, pad_r = 150, 96
    track = w - label_w - pad_r
    bars = []
    for i, r in enumerate(rows):
        v = float(r["spend"])
        bars.append({
            "y": i * row_h, "h": row_h - 10,
            "w": round(track * v / biggest, 1),
            "carrier": r["carrier"], "value": f"{v:,.2f}",
            "share": f"{v / total * 100:.0f}%",
            "bookings": r["bookings"],
        })
    return {"bars": bars, "w": w, "h": max(len(rows) * row_h, row_h),
            "label_w": label_w, "total": f"{total:,.2f}"}


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


def _safe_next(target):
    """Only ever redirect within this site."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("trips")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or request.remote_addr)

        if auth.throttled(email, ip):
            return render_template(
                "login.html", hide_nav=True, prefill_email=email,
                error="Too many failed attempts. Wait a few minutes and "
                      "try again."), 429

        user = db.user_by_email(email)
        if not user or not auth.check_password(user["password_hash"],
                                               request.form.get("password", "")):
            db.record_failure(email, ip)
            # One message for both cases — telling an attacker which half was
            # wrong turns the form into an account enumerator.
            return render_template("login.html", hide_nav=True, prefill_email=email,
                                   error="That email and password don't match."), 401
        db.clear_failures(email)
        auth.sign_in(user["id"])
        return redirect(_safe_next(request.args.get("next")))
    return render_template("login.html", hide_nav=True, prefill_email="")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in
                ("given_name", "family_name", "company", "email")}
        form["email"] = form["email"].lower()
        password = request.form.get("password", "")

        def again(msg):
            return render_template("signup.html", hide_nav=True, form=form, error=msg), 400

        problem = auth.password_problem(password, request.form.get("confirm", ""))
        if problem:
            return again(problem)
        if not all(form.values()):
            return again("Every field is required.")
        if db.email_taken(form["email"]):
            return again("An account already exists for that email.")

        user = db.create_account(form["company"], form["email"],
                                 auth.hash_password(password), form)
        auth.sign_in(user["id"])
        return redirect(url_for("index"))
    return render_template("signup.html", hide_nav=True, form={})


@app.route("/logout", methods=["GET", "POST"])
def logout():
    auth.sign_out()
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    # "/" is the marketing page. Signed in, it renders logged-out chrome and
    # reads as a dropped session, so send those visitors into the app instead.
    if auth.current_user():
        return redirect(url_for("overview"))
    return render_template("landing.html", deals=PLACEHOLDER_DEALS)


@app.route("/search", methods=["GET"])
def search():
    """The app's search page.

    GET with query parameters on purpose. As a POST the results page could not
    be returned to — the browser had nothing to replay and showed
    ERR_CACHE_MISS on back/forward — and a search could not be linked or
    bookmarked. Nothing here mutates state, so GET is also the honest verb.
    """
    form = {
        "origin": request.args.get("origin", "").strip().upper(),
        "destination": request.args.get("destination", "").strip().upper(),
        "date": request.args.get("date", "").strip(),
        "return_date": request.args.get("return_date", "").strip(),
        "cabin": request.args.get("cabin", "economy"),
        "trip_type": request.args.get("trip_type", "one_way"),
        "adults": _adults(request.args.get("adults")),
    }

    # Arriving from the sidebar with nothing filled in yet is not an error.
    if not any((form["origin"], form["destination"], form["date"])):
        return render_template("results.html", nav="search", offers=None, form=form)

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
            "data": {"slices": slices,
                     "passengers": [{"type": "adult"}] * form["adults"],
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

MAX_ADULTS = 9


def _adults(raw):
    """1..MAX_ADULTS. Duffel prices per passenger, so this decides the fare."""
    try:
        return max(1, min(MAX_ADULTS, int(raw or 1)))
    except (TypeError, ValueError):
        return 1


def passengers_from_form(count=None):
    """Every traveller on the booking, from repeated form fields.

    Repeated names rather than given_name_0/given_name_1: getlist keeps
    document order, so the cards line up with the offer's passenger ids
    without any index bookkeeping.
    """
    lists = {f: request.form.getlist(f) for f in PASSENGER_FIELDS}
    n = count or max((len(v) for v in lists.values()), default=1) or 1
    people = [{f: (lists[f][i] if i < len(lists[f]) else "").strip()
               for f in PASSENGER_FIELDS} for i in range(n)]
    for person in people:
        person["phone_number"] = normalise_phone(person["phone_number"])
    return people


# Duffel wants phone numbers in E.164 — a leading + then country code. It
# rejects anything else, and the message it returns names the field but not the
# person, which is what made "7025211089" look like a date-of-birth problem.
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def normalise_phone(raw):
    """Keep digits and one leading +. Formatting people actually type —
    '+44 20 8016 0509', '(702) 521-1089' — should not be an error."""
    s = re.sub(r"[^\d+]", "", raw or "")
    if s.startswith("+"):
        return "+" + re.sub(r"\D", "", s[1:])
    return s


def phone_problem(raw):
    if not raw:
        return "phone"
    if not _E164.match(raw):
        return "phone (include the country code, e.g. +15551234567)"
    return None


def dob_problem(raw):
    """An `adult` passenger must actually be one, or Duffel refuses the order."""
    if not raw:
        return "date of birth"
    try:
        born = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return "date of birth (use the date picker)"
    today = datetime.now(timezone.utc).date()
    years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    if years < 18:
        return "date of birth (adult fares need 18 or over)"
    if years > 120:
        return "date of birth (check the year)"
    return None


# Duffel requires every one of these on every passenger, and answers a missing
# one with an HTTP 422 that names the field but not the person. Check here so
# the traveller is named and nobody reaches a payment screen they cannot use.
_FIELD_LABEL = {"title": "title", "given_name": "first name",
                "family_name": "last name", "born_on": "date of birth",
                "gender": "gender", "email": "email", "phone_number": "phone"}


def passenger_problems(people):
    out = []
    for i, person in enumerate(people, 1):
        bad = [_FIELD_LABEL[f] for f in PASSENGER_FIELDS
               if f not in ("phone_number", "born_on") and not person.get(f)]
        for check, value in ((phone_problem, person.get("phone_number")),
                             (dob_problem, person.get("born_on"))):
            problem = check(value)
            if problem:
                bad.append(problem)
        if bad:
            out.append(f"Traveller {i}: check {', '.join(bad)}")
    return out


def fetch_offer_view(offer_id):
    return offer_view(duffel_http.request("GET", f"/air/offers/{offer_id}", label="ui_offer"))


@app.route("/book/passenger", methods=["POST"])
@auth.login_required
def passenger_step():
    offer_id = request.form["offer_id"]
    try:
        offer = fetch_offer_view(offer_id)
        prefill = auth.profile_of(auth.current_user())
        # The booker is usually traveller one; the rest start empty.
        people = [prefill] + [{} for _ in range(offer["passenger_count"] - 1)]
        return render_template("passenger.html", nav="search", offer=offer,
                               people=people,
                               # only the passenger fields reach the page —
                               # internal ids and timestamps have no business there
                               saved=[{k: t[k] for k in db.TRAVELER_FIELDS}
                                      for t in db.travelers(_account())])
    except (DuffelError, RuntimeError) as exc:
        return render_template("results.html", nav="search", offers=None, form={},
                               error=f"{exc} — offers expire; search again.")


@app.route("/book/payment", methods=["POST"])
@auth.login_required
def payment_step():
    offer_id = request.form["offer_id"]
    people = passengers_from_form()
    problems = passenger_problems(people)
    if problems:
        return render_template("passenger.html", nav="search",
                               offer=fetch_offer_view(offer_id), people=people,
                               saved=[{k: t[k] for k in db.TRAVELER_FIELDS}
                                      for t in db.travelers(_account())],
                               error=" · ".join(problems)), 400
    try:
        return render_template("payment.html", nav="search",
                               offer=fetch_offer_view(offer_id), people=people)
    except (DuffelError, RuntimeError) as exc:
        return render_template("results.html", nav="search", offers=None, form={},
                               error=f"{exc} — offers expire; search again.")


@app.route("/book", methods=["POST"])
@auth.login_required
def book():
    offer_id = request.form["offer_id"]
    try:
        offer = duffel_http.request("GET", f"/air/offers/{offer_id}", label="ui_offer")
        seats = offer.get("passengers", []) or [{}]
        people = passengers_from_form(len(seats))
        problems = passenger_problems(people)
        if problems:
            return render_template("passenger.html", nav="search",
                                   offer=offer_view(offer), people=people,
                                   saved=[{k: t[k] for k in db.TRAVELER_FIELDS}
                                          for t in db.travelers(_account())],
                                   error=" · ".join(problems)), 400
        order = duffel_http.request("POST", "/air/orders", body={
            "data": {
                "type": "instant",
                "selected_offers": [offer_id],
                # Each traveller is matched to one of the offer's passenger ids.
                # Sending the same details for every seat, which is what the
                # single-passenger version did, books several copies of one person.
                "passengers": [{"id": seat["id"], **person}
                               for seat, person in zip(seats, people)],
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
    # sandbox constant (+125.00) rather than an accidental fake drop. Stored on
    # the order rather than in a file: /tmp does not survive on Vercel, and a
    # scenario the engine cannot find matches nothing.
    upsert_order({"order_id": order["id"], "sim_scenario": {
        "carrier": snap.itinerary.carrier_iata,
        "flight_numbers": list(snap.itinerary.flight_numbers),
        "currency": order["total_currency"], "route": str(snap.route),
        "market_price": order["total_amount"], "change_total": "125.00",
        "new_total": str(Decimal(order["total_amount"]) + Decimal("100.00")),
        "penalty": "25.00",
    }})
    return redirect(url_for("trip_booked", order_id=order["id"]))


@app.route("/trips/<order_id>/booked")
@auth.login_required
def trip_booked(order_id):
    record = find_order(order_id)
    if not record:
        return redirect(url_for("trips"))
    return render_template("confirm.html", nav="trips", trip=trip_view(record))


# ---------------------------------------------------------------------------
# employee views
# ---------------------------------------------------------------------------

@app.route("/overview")
@auth.login_required
def overview():
    """Account numbers, all of them derived from db.account_summary so this
    page can never disagree with the pages it summarises."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    trips_ = [trip_view(r) for r in load_orders()]
    upcoming = sorted((t for t in trips_ if (t["depart_iso"][:10] or "9999") >= today),
                      key=lambda t: t["depart_iso"])
    past = sorted((t for t in trips_ if (t["depart_iso"][:10] or "9999") < today),
                  key=lambda t: t["depart_iso"], reverse=True)
    # Same cards as My trips, capped — this is a summary, not the full list.
    acct = _account()
    months = db.monthly_series(acct)
    weeks = db.weekly_activity(acct)
    carriers = db.spend_by_carrier(acct)
    return render_template("overview.html", nav="overview",
                           s=db.account_summary(acct),
                           upcoming=upcoming[:4], recent=past[:3],
                           months=months, weeks=weeks,
                           spend_chart=spend_chart(months),
                           activity=activity_chart(weeks),
                           bookings=db.recent_bookings(acct),
                           carriers=carriers,
                           carrier_chart=carrier_chart(carriers) if carriers else None,
                           c_spend=SERIES_SPEND, c_saved=SERIES_SAVED)


@app.route("/wallet")
@auth.login_required
def wallet():
    """A ledger, not a stored balance.

    Recovered fares go back to the card that paid, so there is no float being
    held here — the page shows what moved and where it went, and says so.
    """
    rows = db.wallet_transactions(_account())
    return render_template("wallet.html", nav="wallet", rows=rows,
                           totals=db.wallet_totals(rows), card=CARD)


# ---------------------------------------------------------------------------
# travellers — passenger profiles, not logins
# ---------------------------------------------------------------------------

def _traveler_problem(form):
    """Saved profiles go straight into a booking, so hold them to the same
    rules Duffel will apply — catching it here beats catching it after payment."""
    form["phone_number"] = normalise_phone(form.get("phone_number"))
    if not (form["given_name"] and form["family_name"]):
        return "First and last name are required."
    if form.get("phone_number"):
        problem = phone_problem(form["phone_number"])
        if problem:
            return f"Check the {problem}."
    if form.get("born_on"):
        problem = dob_problem(form["born_on"])
        if problem:
            return f"Check the {problem}."
    return None


@app.route("/travelers")
@auth.login_required
def travelers():
    return render_template("travelers.html", nav="travelers",
                           travelers=db.travelers(_account()), form={}, editing=None)


@app.route("/travelers/new", methods=["POST"])
@auth.login_required
def traveler_new():
    form = {f: request.form.get(f, "").strip() for f in db.TRAVELER_FIELDS}
    problem = _traveler_problem(form)
    if problem:
        return render_template("travelers.html", nav="travelers",
                               travelers=db.travelers(_account()), form=form,
                               editing=None, error=problem), 400
    db.traveler_save(form, _account())
    return redirect(url_for("travelers"))


@app.route("/travelers/<traveler_id>/edit", methods=["GET", "POST"])
@auth.login_required
def traveler_edit(traveler_id):
    existing = db.traveler(traveler_id, _account())
    if not existing:
        return redirect(url_for("travelers"))
    if request.method == "POST":
        form = {f: request.form.get(f, "").strip() for f in db.TRAVELER_FIELDS}
        problem = _traveler_problem(form)
        if problem:
            return render_template("travelers.html", nav="travelers",
                                   travelers=db.travelers(_account()), form=form,
                                   editing=traveler_id, error=problem), 400
        db.traveler_save(form, _account(), traveler_id)
        return redirect(url_for("travelers"))
    return render_template("travelers.html", nav="travelers",
                           travelers=db.travelers(_account()),
                           form=existing, editing=traveler_id)


@app.route("/travelers/<traveler_id>/delete", methods=["POST"])
@auth.login_required
def traveler_remove(traveler_id):
    db.traveler_delete(traveler_id, _account())
    return redirect(url_for("travelers"))


@app.route("/trips")
@auth.login_required
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
@auth.login_required
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
@auth.login_required
def orders():
    records = load_orders()
    for r in records:
        # Straight off the order, so the inputs show the scenario the next run
        # will actually use rather than whatever a local file happens to hold.
        sc = r.get("sim_scenario") or {}
        r["sim_market"] = sc.get("market_price")
        r["sim_change_total"] = sc.get("change_total")
    return render_template("orders.html", nav="ops", orders=records,
                           source_name=os.environ.get("PRICE_SOURCE", "simulated"))


@app.route("/orders/<order_id>/monitor", methods=["POST"])
@auth.login_required
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
@auth.login_required
def simulate(order_id):
    record = find_order(order_id)
    if not record:
        return redirect(url_for("orders"))

    scenario = dict(record.get("sim_scenario") or {})
    if not scenario:
        # An order booked before scenarios were stored, or one whose seed was
        # lost with an old instance. Rebuild it from the order itself so the
        # engine still has a matching carrier and flight number to work with.
        snap = snapshot_of(record)
        scenario = {
            "carrier": snap.itinerary.carrier_iata,
            "flight_numbers": list(snap.itinerary.flight_numbers),
            "currency": record.get("currency") or "USD",
            "route": str(snap.route),
            "market_price": record.get("paid"),
            "change_total": "125.00",
            "penalty": "25.00",
        }

    # A one-click drop button and the text input are in the same form, so both
    # arrive. The button is the deliberate act, so it wins.
    quick = request.form.get("quick_change_total", "").strip()

    for key in ("market_price", "change_total", "new_total", "penalty"):
        value = request.form.get(key, "").strip()
        if key == "change_total" and quick:
            value = quick
        if value:
            try:
                Decimal(value)
            except Exception:
                return redirect(url_for("orders"))
            scenario[key] = value

    # Feed the engine this order's scenario directly — no file, nothing shared
    # between instances, nothing to go missing between two requests.
    sim = SimulatedPriceSource(data={"orders": {order_id: scenario}})
    upsert_order({"order_id": order_id, "sim_scenario": scenario})
    _run_cycle(order_id, sim)
    return redirect(url_for("orders"))


@app.route("/orders/<order_id>/cycle", methods=["POST"])
@auth.login_required
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
@auth.login_required
def confirm_action(order_id, action):
    """Step 1 of 2. Nothing has happened at this point."""
    record = find_order(order_id)
    if not record or action not in ("exchange", "cancel"):
        return redirect(url_for("orders"))
    return render_template("confirm_action.html", nav="ops", order=record, action=action)


@app.route("/orders/<order_id>/execute/<action>", methods=["POST"])
@auth.login_required
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

    offer_id = last.get("change_offer_id") or ""
    if action == "exchange" and not offer_id:
        return refuse("no change offer on the last decision — run a live cycle first")

    # Reserve the right to call Duffel exactly once for this (order, action,
    # offer). A double submit loses the INSERT race and stops here rather than
    # exchanging the same ticket twice.
    try:
        attempt = db.claim_execution(order_id, action, offer_id)
    except db.AlreadyAttempted as dup:
        prior = dup.attempt
        if prior["status"] == "succeeded":
            return redirect(url_for("trip_detail", order_id=order_id))
        return refuse(f"This {action} was already submitted "
                      f"({prior['status']}). Check the order before retrying.")

    extra = {}
    duffel_change_id = None
    try:
        if action == "exchange":
            change = duffel_http.request("POST", "/air/order_changes", body={
                "data": {"selected_order_change_offer": offer_id}}, label="ui_change_create")
            duffel_change_id = change["id"]
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
        # The order-change *create* call is safe to retry; a failed confirm is
        # not, because the exchange may have landed anyway. Only release the
        # claim when nothing could have been confirmed.
        if duffel_change_id is None:
            db.release_execution(attempt["id"])
        else:
            db.finish_execution(attempt["id"], "failed", note=str(exc),
                                duffel_change_id=duffel_change_id)
        return refuse(str(exc))

    db.finish_execution(attempt["id"], "succeeded", note=note,
                        duffel_change_id=duffel_change_id, result=result)
    fresh = duffel_http.request("GET", f"/air/orders/{order_id}", label="ui_order_refresh")
    upsert_order({"order_id": order_id, "raw": fresh, "monitoring": False,
                  "executed": note, "paid": fresh["total_amount"], **extra})
    return redirect(url_for("trip_detail", order_id=order_id))


@app.route("/decisions")
@auth.login_required
def decisions():
    return render_template("decisions.html", nav="log", rows=db.audit_rows(limit=200))


if __name__ == "__main__":
    print(f"Trip Difference → http://localhost:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False)
