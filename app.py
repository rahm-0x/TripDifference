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

import hmac
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import (Flask, abort, flash, get_flashed_messages, redirect, render_template,
                   request, send_from_directory, session, url_for)

import requests

import auth
import billing
import config
import db
import duffel_http
import eligibility
import live_guard
import live_search
import offer_eligibility
import paths
import policy
import supabase_auth
from duffel_http import DuffelError
from engine import (OrderSnapshot, Outcome, ReshopPolicy,
                    evaluate, log_decision, log_eligibility)
from prices import (DuffelPriceSource, Route, SimulatedPriceSource,
                    get_price_source)

HERE = Path(__file__).parent
PORT = 8000

# Environment guards, at startup rather than on the first request that would
# touch the wrong thing: a Vercel Preview deployment without an explicit
# APP_ENV, a live Duffel flag or token anywhere but staging (production above
# all), a test token behind a live flag on staging, a non-sk_test_ Stripe key
# on staging, and a database that doesn't match APP_ENV (staging must use the
# staging project, production must not).
config.startup_check()
duffel_http.startup_check()
billing.startup_check()
db.startup_check()

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

# The only endpoints reachable without signing in. Everything else — every
# route that exists now and every route added later — requires a session,
# enforced by _require_login below before any view (or the CSRF guard) runs.
# test_routes.py walks the route map and fails on anything reachable that isn't
# listed here.
PUBLIC_ENDPOINTS = frozenset({
    "index",                                       # landing
    "login", "signup",
    "google_auth_start", "google_auth_callback",   # auth callbacks
    "static", "logo",                              # static
    # Server-to-server: Vercel Cron can't hold a session. Authenticated by a
    # CRON_SECRET bearer token instead, and refused without one.
    "cron_reshop",
})


@app.before_request
def _require_login():
    """Default deny. Registered before _csrf_guard, so an anonymous POST is
    sent to sign in rather than answered with a CSRF error."""
    if request.endpoint is None or request.endpoint in PUBLIC_ENDPOINTS:
        return None  # unknown URLs 404 as usual
    if auth.current_user() is None:
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    return None


@app.before_request
def _csrf_guard():
    """Every state-changing request, without exception. A per-form hidden field
    is something you forget on the one form that matters."""
    if request.method in UNSAFE_METHODS and not auth.csrf_ok():
        return render_template("error.html",
                               hide_nav=True,
                               error="That form expired or came from somewhere "
                                     "else. Go back and try again."), 403


def _onboarding_steps(user, card, counts):
    return [
        {"done": bool(user.get("given_name") and user.get("family_name")),
         "title": "Complete your profile", "sub": "Tell us your name so tickets match your ID.",
         "cta": "Finish profile", "url": url_for("onboarding_profile")},
        {"done": bool(card),
         "title": "Link a card", "sub": "Required to buy a ticket — charged when you book.",
         "cta": "Link a card", "url": url_for("onboarding_payment")},
        {"done": counts["travelers"] > 0,
         "title": "Add a traveler", "sub": "Saved passenger details speed up future bookings.",
         "cta": "Add traveler", "url": url_for("travelers")},
        {"done": counts["bookings"] > 0,
         "title": "Book your first flight", "sub": "We watch the fare and rebook you if it drops.",
         "cta": "Search flights", "url": url_for("search")},
    ]


@app.context_processor
def inject_globals():
    user = auth.current_user()
    card = db.account_card(user["account_id"]) if user else None
    onboarding = None
    if user:
        steps = _onboarding_steps(user, card, db.onboarding_counts(user["account_id"]))
        done = sum(1 for s in steps if s["done"])
        next_step = next((s for s in steps if not s["done"]), None)
        onboarding = {"done": done, "total": len(steps), "complete": next_step is None,
                      "next": next_step}
    return {"user": auth.view_model(user), "card": card, "onboarding": onboarding,
            "policy": DEFAULT_POLICY, "profile": auth.profile_of(user),
            "csrf_token": auth.csrf_token(),
            # templates/_live_banner.html, included by every full-page template:
            # 'orders' (real tickets, real money), 'search' (live fares, no
            # orders), or None
            "live_banner": _live_banner(),
            "app_env": config.APP_ENV}


def _live_banner():
    if config.APP_ENV != "staging":
        return None
    if config.DUFFEL_LIVE_ORDERS_ENABLED:
        return "orders"
    if config.DUFFEL_LIVE_SEARCH_ENABLED:
        return "search"
    return None


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


# How much ranking weight an *unconfirmed* recovery claim gets, in search()'s
# value-after-reshop sort. A LIKELY_MONITORING fare is ranked as if it cost
# this much less than its real price — enough to beat an ineligible fare at
# a similar price point, not enough to beat one that's materially cheaper.
# Deliberately smaller than eligibility.MAX_PENALTY_RATIO (0.30): this is a
# softer signal than the fare's own penalty math, since it's a prediction,
# not a verified fact. Tunable; not derived from anything.
LIKELY_MONITORING_RANK_DISCOUNT = Decimal("0.15")

# A fare whose change rules the airline never published can't be assessed at
# all, so no savings claim can be made about it. Ranked as if it cost this much
# more, to keep assessable fares above it at a similar price. Smaller than the
# discount above: not publishing rules is an absence of information, not
# evidence the fare is bad, so a materially cheaper one should still win.
UNPUBLISHED_CONDITIONS_RANK_PENALTY = Decimal("0.10")


def offer_view(offer, policy_rules=None, carrier_capability_map=None):
    """
    Duffel offer → view model.

    Eligibility runs through the same assessor as orders — no parallel
    approximation — but an offer carries no `available_actions` at all
    (Duffel doesn't expose it before a ticket is issued), so the one gate
    that's actually authoritative (FINDINGS.md §8) can never fire here.
    assess() reflects that honestly: LIKELY_MONITORING, not MONITORING,
    unless carrier_capability says this carrier has never once honoured
    that claim on a real order, in which case it's NOT_ELIGIBLE outright.

    policy_rules lets a caller checking many offers (search()) fetch the
    account's active rules once rather than once per offer; a single-offer
    caller (fetch_offer_view) leaves it None and this fetches for itself. carrier_capability_map is the same batching
    idea, keyed by carrier IATA code. policy_result is advisory only here —
    nothing renders policy_enforcement/policy_result yet (Phase 4 UI); the
    hard gate lives in book().
    """
    slices = [v for v in (slice_view(s) for s in offer.get("slices", [])) if v]
    carrier_iata = (offer.get("owner") or {}).get("iata_code")
    if carrier_capability_map is not None:
        capability = carrier_capability_map.get(carrier_iata)
    else:
        capability = db.carrier_capability_for(carrier_iata)
    a = eligibility.assess(offer, fare_type="cash", carrier_capability=capability)
    rules = policy_rules if policy_rules is not None else db.policy_rules_active(_account())
    decision = policy.evaluate_with_rules(rules, offer)
    first = slices[0] if slices else {}
    return {
        "id": offer["id"],
        "carrier": (offer.get("owner") or {}).get("name", ""),
        "amount": offer["total_amount"],
        "currency": offer["total_currency"],
        "slices": slices,
        "fare_brand": first.get("fare_brand"),
        "monitorable": a.should_poll,
        "eligibility_state": a.state.value,
        "eligibility_reason": a.reason.value,
        "eligibility_label": a.label,
        "eligibility_copy": a.customer_copy,
        # The airline's own published change fee, as assess() read it. Real
        # live data — showing it beats generic copy, and a 0.00 penalty
        # ("changes are free") is a different story to an unpublished one.
        "change_penalty": str(a.penalty) if a.penalty is not None else None,
        "change_penalty_currency": a.penalty_currency,
        "change_penalty_pct": (f"{a.penalty_ratio:.0%}"
                               if a.penalty_ratio is not None else None),
        "policy_enforcement": decision.enforcement,
        "policy_result": decision.to_json(),
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
    # `fallback` only matters when raw is empty (a manual/imported
    # reservation with no Duffel order behind it).
    return OrderSnapshot.from_duffel(record["raw"], fare_type=record.get("fare_type") or "cash",
                                     fallback=record)


def _manual_leg_view(record):
    """The single leg for a reservation with no Duffel raw payload — built
    from the stored segment columns (migration 010) instead of slice_view's
    Duffel-shaped parse. No arrival/departure clock-time is captured on
    manual entry, so depart/arrive stay blank; depart_iso still carries the
    date so upcoming/past sorting works."""
    date = record.get("departure_date") or ""
    return {
        "origin": record.get("seg_origin", ""), "destination": record.get("seg_destination", ""),
        "depart": "", "arrive": "", "depart_iso": f"{date}T00:00:00" if date else "",
        "date": datelabel(date), "duration": "", "duration_min": 0,
        "stops": 0, "next_day": False,
        "flight_numbers": f"{record.get('carrier','')} {record.get('seg_flight_number','')}".strip(),
        "fare_brand": None,
    }


def trip_view(record):
    """
    Stored order record → the shape the employee-facing screens expect.

    Eligibility is recomputed from the raw payload on every read rather than
    trusted from storage, so orders booked before gating existed display
    correctly without a migration. `monitoring` is the AND of the operator
    toggle and eligibility — an ineligible fare can never read as monitored.
    """
    raw = record.get("raw") or {}
    if raw:
        legs = [v for v in (slice_view(s) for s in raw.get("slices", [])) if v]
    else:
        # A manual/imported reservation — no Duffel slices to parse, so the
        # leg comes from the stored segment columns instead (migration 010).
        legs = [_manual_leg_view(record)] if record.get("seg_origin") else []
    first = legs[0] if legs else {}
    snap = snapshot_of(record)
    d = record.get("last_decision") or {}
    pax = (raw.get("passengers") or [{}])[0]
    # snapshot_of() already computed this assessment onto snap.eligibility,
    # reused here rather than assessed twice.
    a = snap.eligibility
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
        "source": record.get("source") or "td_rebook",
        "fare_type": record.get("fare_type") or "cash",
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
        # Customer-facing flag only — a boolean, not the Stripe error text
        # itself. The detail (payment_capture_error) is ops-facing, shown
        # on /orders instead; a customer doesn't need Stripe's own wording
        # for something they aren't being asked to act on.
        "payment_capture_failed": bool(record.get("payment_capture_failed_at")),
        # Was invisible outside the internal /orders ops console — the
        # worst instance of "our main feature is invisible" in the app: a
        # cancelled order's own detail page gave the customer no
        # indication it was cancelled.
        "executed": record.get("executed"),
        "refundable": record.get("refundable"),
        "fare_conditions": record.get("fare_conditions"),
        "cost_center_id": record.get("cost_center_id"),
        # A simulated rebooking shows alongside the real figures, never as one.
        "simulated": bool(record.get("simulated")),
        "sim_refunded": record.get("sim_refunded"),
        "sim_paid": record.get("sim_paid"),
        "last_checked": (d.get("ts") or "")[:19].replace("T", " ") or None,
        "last_decision": d or None,
        "email": pax.get("email"),
        "passenger_name": f"{pax.get('given_name','')} {pax.get('family_name','')}".strip(),
        # Every person on the booking. `pax` above is only the lead traveler,
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


def execution_steps(order_id, original_paid):
    """(ts, new_paid) for every exchange that actually landed on this order,
    oldest first — the difference band's baseline steps down at each one.
    Starts from original_paid at the dawn of time (an empty string sorts
    before any real ISO timestamp — db.audit_rows() hands back ts as an
    isoformat() string, not a datetime, so the sentinel has to sort the
    same way) so a checkpoint logged before the first exchange still
    resolves to what was actually paid then, not the post-exchange amount.
    Real data or nothing — an order with no successful execution just
    yields the one starting point, which difference_band renders as a flat
    baseline, identical to before this stepping existed."""
    out = [("", Decimal(str(original_paid or 0)))]
    for r in reversed(db.audit_rows(limit=500, order_id=order_id)):
        if r.get("kind") == "execution" and r.get("execution") == "executed" and r.get("new_paid"):
            out.append((r["ts"], Decimal(str(r["new_paid"]))))
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


def saved_chart(rows, w=720, h=170):
    """Home's headline trend — total saved over time, one dashed line.
    Same geometry as spend_chart but isolated to the 'recovered' series;
    kept separate rather than parameterising spend_chart since Home and the
    Reservations page (which keeps the two-series version) want different
    axis heights."""
    top = _nice_top(max([float(r["recovered"]) for r in rows] + [0]))
    pad_l, pad_r, pad_t, pad_b = 44, 14, 14, 30
    inner_w, inner_h = w - pad_l - pad_r, h - pad_t - pad_b
    n = max(len(rows) - 1, 1)
    pts = []
    for i, r in enumerate(rows):
        x = pad_l + inner_w * i / n
        y = pad_t + inner_h * (1 - float(r["recovered"]) / top) if top else pad_t + inner_h
        pts.append({"x": round(x, 1), "y": round(y, 1),
                    "label": r["label"], "value": f"{float(r['recovered']):,.2f}"})
    grid = [{"y": round(pad_t + inner_h * (1 - f), 1),
             "label": f"{int(top * f):,}"} for f in (0, .5, 1)]
    return {"points": pts, "grid": grid, "top": top, "w": w, "h": h,
            "has_data": any(float(r["recovered"]) for r in rows)}


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
    """Horizontal bars, ranked by spend. Not a donut: this is a comparison,
    and a pie of close values is unreadable — with a single carrier it
    would be one 100% slice, which is a stat tile, not a chart.

    Computes both spend and saved widths/values per bar (not just the
    ranking metric) so the Saved/Spent toggle can flip client-side with
    no server round trip — a real interaction, not two separate charts."""
    spend_total = sum(float(r["spend"]) for r in rows) or 1.0
    saved_total = sum(float(r["saved"]) for r in rows) or 1.0
    spend_biggest = max([float(r["spend"]) for r in rows] + [1.0])
    saved_biggest = max([float(r["saved"]) for r in rows] + [1.0])
    label_w, pad_r = 150, 96
    track = w - label_w - pad_r
    bars = []
    for i, r in enumerate(rows):
        sv, av = float(r["spend"]), float(r["saved"])
        bars.append({
            "y": i * row_h, "h": row_h - 10,
            "spend_w": round(track * sv / spend_biggest, 1),
            "saved_w": round(track * av / saved_biggest, 1),
            "carrier": r["carrier"],
            "spend_value": f"{sv:,.2f}", "saved_value": f"{av:,.2f}",
            "spend_share": f"{sv / spend_total * 100:.0f}%",
            "saved_share": f"{av / saved_total * 100:.0f}%" if av else "0%",
            "bookings": r["bookings"],
        })
    return {"bars": bars, "w": w, "h": max(len(rows) * row_h, row_h),
            "label_w": label_w, "spend_total": f"{spend_total:,.2f}",
            "saved_total": f"{saved_total:,.2f}"}


# --- the difference band (Item 3's signature element) -----------------------
#
# Paid as a dashed baseline, market checks as a solid line, the gap shaded
# green below the baseline (cheaper than paid — recoverable) and slate above
# it (bought well). Computed server-side into static paths, same convention
# as every other chart in this app — no client-side data generation.
#
# Real points only. Fewer than 2 checks is "nothing to plot yet", returned
# as None so the caller renders an honest empty state rather than stretching
# one dot into a line. Baseline is flat (paid, as it stands today) rather
# than stepped at each exchange — no real exchange has moved real money yet
# to justify the extra complexity of a genuinely stepped baseline; see the
# writeup.
BAND_W, BAND_H = 720, 180
BAND_PAD_T, BAND_PAD_B = 16, 20


def difference_band(points, baseline, currency, baseline_steps=None):
    """points: [(ts, Decimal), ...] market_best checks, oldest first.
    baseline: a single Decimal, used when baseline_steps is None — flat
    across the whole window. This is the only mode aggregate_difference_band
    ever uses: summed across multiple orders, there is no single execution
    timeline left to step at.

    baseline_steps: optional [(ts, Decimal), ...], oldest first — what was
    paid, and every timestamp it changed (each exchange execution actually
    landing). When given, the dashed datum line steps down at the market
    checkpoint on or after each step's timestamp, rather than sloping
    between the old and new paid amount as a flat baseline would. Resolution
    is the market-check series itself (points), not the step's exact
    timestamp — this is a real, honest approximation (snapped to the
    nearest checkpoint actually plotted), never an interpolated or invented
    value.
    """
    if len(points) < 2:
        return None
    n = len(points)
    xs = [i / (n - 1) * BAND_W for i in range(n)]
    series_vals = [float(v) for _, v in points]

    if baseline_steps:
        ordered = sorted(baseline_steps, key=lambda s: s[0])
        def baseline_at(ts):
            val = ordered[0][1]
            for step_ts, step_val in ordered:
                if step_ts <= ts:
                    val = step_val
                else:
                    break
            return val
        base_vals = [float(baseline_at(ts)) for ts, _ in points]
    else:
        base_vals = [float(baseline)] * n

    all_vals = series_vals + base_vals
    lo, hi = min(all_vals), max(all_vals)
    pad = (hi - lo) * 0.35 or max(hi * 0.1, 10)
    lo, hi = lo - pad, hi + pad
    span = hi - lo or 1

    def y(v):
        return BAND_PAD_T + (1 - (v - lo) / span) * (BAND_H - BAND_PAD_T - BAND_PAD_B)

    line_pts = [(round(xs[i], 1), round(y(series_vals[i]), 1)) for i in range(n)]
    base_pts = [(round(xs[i], 1), round(y(base_vals[i]), 1)) for i in range(n)]

    line_path = "M" + " L".join(f"{x},{yy}" for x, yy in line_pts)

    # A stepped baseline needs a horizontal jump between differing y values,
    # not a sloped line — the paid amount changed instantly at the exchange,
    # it did not drift there. Flat baselines never insert a corner (every
    # base_pts[i] shares one y), so this degrades to the plain point list —
    # same shape as before this function supported stepping at all.
    base_corner_pts = [base_pts[0]]
    for i in range(1, n):
        prev_y = base_pts[i - 1][1]
        x, yy = base_pts[i]
        if yy != prev_y:
            base_corner_pts.append((x, prev_y))
        base_corner_pts.append((x, yy))
    base_path = "M" + " L".join(f"{x},{yy}" for x, yy in base_corner_pts)
    # The fill's baseline edge reuses the same corner points (reversed), so
    # the shaded region's boundary matches the dashed line's step exactly —
    # not a diagonal cutting across it.
    area_path = line_path + " L" + " L".join(f"{x},{yy}" for x, yy in reversed(base_corner_pts)) + " Z"
    below_clip = base_path + f" L{BAND_W},{BAND_H} L0,{BAND_H} Z"
    above_clip = base_path + f" L{BAND_W},0 L0,0 Z"

    return {
        "w": BAND_W, "h": BAND_H, "currency": currency, "has_data": True,
        "line_path": line_path, "base_path": base_path, "area_path": area_path,
        "below_clip": below_clip, "above_clip": above_clip,
        "baseline": f"{base_vals[-1]:,.2f}", "latest": f"{series_vals[-1]:,.2f}",
        "n": n,
    }


def aggregate_difference_band(monitored, currency="USD"):
    """Home's band: sum of market_best across every currently-monitored
    order, at every distinct timestamp any of them was checked, against
    the flat sum of what was paid for them. An order with zero checks yet
    contributes its own `paid` as its stand-in for "no signal", so one
    fresh order doesn't collapse the whole aggregate — but if not one
    order has ever had a real check, there is nothing to plot, and this
    returns None rather than a flat, fabricated "you've saved nothing" line.

    Bucketed by exact timestamp, not calendar date — a burst of real
    checks run minutes apart during the same session is still real,
    plottable data, not "not enough history yet".
    """
    if not any(o["points"] for o in monitored):
        return None
    all_ts = sorted({p[0] for o in monitored for p in o["points"]})
    if len(all_ts) < 2:
        return None
    series = []
    for t in all_ts:
        total = Decimal("0")
        for o in monitored:
            known = [v for (ts, v) in o["points"] if ts <= t]
            total += known[-1] if known else Decimal(str(o["paid"] or 0))
        series.append((t, total))
    paid_total = sum((Decimal(str(o["paid"] or 0)) for o in monitored), Decimal("0"))
    return difference_band(series, paid_total, currency)


# ---------------------------------------------------------------------------
# auth screens (presentation only — nothing is gated)
# ---------------------------------------------------------------------------

@app.route("/info")
@auth.login_required
def info():
    """
    Standalone explainer of the booking → monitor → exchange → settle flow.

    Deliberately does not extend base.html — it carries its own type and colour
    system, so it is served whole rather than themed to match the app.
    """
    return render_template("info.html")


@app.route("/healthz/env")
@auth.login_required
def healthz_env():
    """What this deployment is running as, for checking a deploy. Never a
    secret: the token as a mode, the database as its Supabase project ref."""
    return {
        "app_env": config.APP_ENV,
        "vercel_git_commit_sha": os.environ.get("VERCEL_GIT_COMMIT_SHA") or None,
        "duffel_token_mode": duffel_http.configured_token_mode(),
        "db_project_ref": db.project_ref(),
        "duffel_live_search_enabled": config.DUFFEL_LIVE_SEARCH_ENABLED,
        "duffel_live_orders_enabled": config.DUFFEL_LIVE_ORDERS_ENABLED,
    }


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
    """Step 1 of onboarding: just email + password (or Google — see
    /auth/google/start). Name, DOB and the rest of the profile are
    collected in /onboarding/profile, not here."""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        def again(msg):
            return render_template("signup.html", hide_nav=True, email=email, error=msg), 400

        problem = auth.password_problem(password, request.form.get("confirm", ""))
        if problem:
            return again(problem)
        if not email:
            return again("Email is required.")
        if db.email_taken(email):
            return again("An account already exists for that email.")

        user = db.create_account(email, auth.hash_password(password))
        auth.sign_in(user["id"])
        return redirect(url_for("onboarding_profile"))
    return render_template("signup.html", hide_nav=True, email="")


# ---------------------------------------------------------------------------
# onboarding — profile, reservations, payment (each its own step/route so a
# signup can resume mid-flow rather than losing progress in one giant form)
# ---------------------------------------------------------------------------

@app.route("/onboarding/profile", methods=["GET", "POST"])
@auth.login_required
def onboarding_profile():
    user = auth.current_user()
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in
                ("given_name", "middle_name", "family_name", "born_on",
                 "referral_source", "invite_code")}
        if not (form["given_name"] and form["family_name"]):
            return render_template("onboarding_profile.html", hide_nav=True, form=form,
                                   error="First and last name are required."), 400
        db.complete_profile(user["id"], user["account_id"], **form)
        return redirect(url_for("onboarding_payment"))
    return render_template("onboarding_profile.html", hide_nav=True, form={
        "given_name": user["given_name"], "middle_name": user.get("middle_name", ""),
        "family_name": user["family_name"],
        "born_on": user["born_on"].isoformat() if user["born_on"] else "",
        "referral_source": user.get("referral_source", ""),
        "invite_code": user.get("invite_code", ""),
    })


@app.route("/onboarding/payment", methods=["GET"])
@auth.login_required
def onboarding_payment():
    acct = _account()
    user = auth.current_user()
    customer_id = billing.ensure_customer(acct, user["email"])
    intent = billing.create_setup_intent(customer_id)
    return render_template("onboarding_payment.html", hide_nav=True,
                           client_secret=intent.client_secret,
                           stripe_publishable_key=os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
                           commission_rate=db.account_commission_rate(acct))


@app.route("/onboarding/payment/confirm", methods=["POST"])
@auth.login_required
def onboarding_payment_confirm():
    acct = _account()
    payment_method_id = request.form.get("payment_method_id", "").strip()
    if payment_method_id:
        ids = db.account_stripe_ids(acct)
        billing.save_payment_method(acct, ids["stripe_customer_id"], payment_method_id)
    return redirect(url_for("overview"))


@app.route("/logout", methods=["GET", "POST"])
@auth.login_required
def logout():
    auth.sign_out()
    return redirect(url_for("index"))


@app.route("/auth/google/start")
def google_auth_start():
    verifier, challenge = supabase_auth.new_pkce_pair()
    session["_google_pkce_verifier"] = verifier
    redirect_to = url_for("google_auth_callback", _external=True)
    return redirect(supabase_auth.start_url(redirect_to, challenge))


@app.route("/auth/google/callback")
def google_auth_callback():
    code = request.args.get("code", "")
    verifier = session.pop("_google_pkce_verifier", None)
    if not code or not verifier:
        return redirect(url_for("login"))
    try:
        supa_user = supabase_auth.exchange_code(code, verifier)
    except (requests.RequestException, KeyError):
        return render_template("login.html", hide_nav=True, prefill_email="",
                               error="Google sign-in didn't complete. Try again."), 502

    supabase_user_id = supa_user["id"]
    email = (supa_user.get("email") or "").lower()

    user = db.user_by_supabase_id(supabase_user_id)
    if not user and email:
        # A matching verified email on an existing password account links
        # rather than duplicating — same person, a second way in.
        user = db.user_by_email(email)
        if user:
            db.link_supabase_id(user["id"], supabase_user_id)
    if not user:
        user = db.create_account(email, password_hash=None,
                                 supabase_user_id=supabase_user_id)

    auth.sign_in(user["id"])
    if not (user.get("given_name") and user.get("family_name")):
        return redirect(url_for("onboarding_profile"))
    return redirect(url_for("overview"))


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


# Duffel returns no change conditions in a bulk search response — they only
# come back from GET /air/offers/:id. Without refetching, every fare in search
# assesses as CONDITIONS_MISSING ("the airline didn't publish change rules for
# this fare"), whatever the carrier's real rules are. /eligibility has always
# refetched (offer_eligibility.evaluate_offers); search now does too.
#
# Bounded, because a search can return a couple of hundred offers and each
# refetch is its own request. Cheapest first: those are the ones that survive
# _offer_rank_price into the 20 actually shown. A single-offer fetch is not an
# offer request, so none of this counts against STAGING_MAX_MONTHLY_SEARCHES
# (live_search.py) — it costs latency, not budget.
# Duffel waits on every supplier before answering, and the slowest are NDC
# carriers that publish no structured change conditions anyway — so waiting on
# them costs seconds and returns offers eligibility can say nothing about.
# Measured on JFK->LHR 2026-11-19: uncapped, 12.0s for 295 offers of which 141
# (48%) carried conditions; at 4000ms, 9.3s for 103 offers of which 103 (100%)
# did. Fewer offers, all of them assessable, and a third off the wait.
#
# Refetching the null ones individually (GET /air/offers/:id) does not help:
# on that same search it filled in 0 of 154 and added 6.6s. Null in the search
# response means the airline published nothing, not that Duffel withheld it.
SUPPLIER_TIMEOUT_MS = "4000"


def _offer_rank_price(o):
    """search()'s value-after-reshop sort key: rank by price, but a fare
    that can never be recovered (Basic Economy and similar) ranks below one
    that can, even when it's cheaper up front. There is no real historical
    per-route recovery-rate data yet (nothing polls on a schedule until the
    scheduler phase), so this proxies on eligibility state plus price, not
    the richer "recovers $94 on average on this fare" ranking the mockup
    shows.

    An offer can never come back MONITORING — Duffel doesn't expose
    available_actions before a ticket exists (FINDINGS.md §8), so every
    offer here is LIKELY_MONITORING at best, an unconfirmed claim. It still
    ranks ahead of an ineligible fare (the claim is real, just not verified
    yet), but not with the same unconditional pull a confirmed order gets:
    ranked as if its price were LIKELY_MONITORING_RANK_DISCOUNT cheaper, so
    a materially cheaper ineligible fare can still win instead of being
    buried under an unconfirmed one.

    CARRIER_SINGLE_DENIAL (eligibility.py: one real order from this carrier
    came back without 'change', not yet enough to exclude outright) gets no
    discount at all — priced at face value, same as an ineligible fare for
    ranking purposes. That's the graduated penalty: it still shows, still
    counts as monitorable, still beats a pricier ineligible fare, but loses
    the optimism boost every other unconfirmed-but-untested claim gets, so a
    same-priced ordinary "likely monitorable" alternative wins instead.
    """
    amount = Decimal(o["amount"])
    if o["eligibility_state"] == "monitoring":
        return amount
    if o["eligibility_state"] == "likely_monitoring" \
            and o.get("eligibility_reason") != "carrier_single_denial":
        return amount * (1 - LIKELY_MONITORING_RANK_DISCOUNT)
    # Nothing published to assess — ranked below fares that can be judged.
    if o.get("eligibility_reason") in ("conditions_missing", "penalty_unknown",
                                       "penalty_currency_mismatch"):
        return amount * (1 + UNPUBLISHED_CONDITIONS_RANK_PENALTY)
    return amount


@app.route("/search", methods=["GET"])
@auth.login_required
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
        # Through live_search, so a live one counts against the staging budget.
        data = live_search.offer_request({
            "data": {"slices": slices,
                     "passengers": [{"type": "adult"}] * form["adults"],
                     "cabin_class": form["cabin"]}
        }, source="search", account_id=_account(),
            params={"return_offers": "true", "supplier_timeout": SUPPLIER_TIMEOUT_MS},
            label="ui_search")
    except (DuffelError, RuntimeError) as exc:
        return render_template("results.html", nav="search", offers=None, form=form, error=str(exc))

    raw = data.get("offers", [])
    rules = db.policy_rules_active(_account())
    capability_map = db.carrier_capabilities_for(
        (o.get("owner") or {}).get("iata_code") for o in raw)
    offers = [offer_view(o, policy_rules=rules,
                         carrier_capability_map=capability_map) for o in raw]

    # Sort by value after reshop, not raw price — see _offer_rank_price.
    offers.sort(key=_offer_rank_price)
    total = len(offers)
    offers = offers[:20]
    # The single best-value offer, flagged for display — the cheapest one
    # that can actually be monitored, if any can be.
    best_id = next((o["id"] for o in offers if o["monitorable"]), None)
    for o in offers:
        o["best_value"] = (o["id"] == best_id)
    return render_template("results.html", nav="search", offers=offers, form=form,
                           total=total,
                           unmonitorable=sum(1 for o in offers if not o["monitorable"]))


# ---------------------------------------------------------------------------
# staging: which live fares could be rebooked on a price drop
# ---------------------------------------------------------------------------

_IATA = re.compile(r"^[A-Z]{3}$")


@app.route("/eligibility")
@auth.login_required
def eligibility_page():
    """Staging only (404 elsewhere): search Duffel and list every offer with
    its change/refund conditions and the verdict offer_eligibility derives from
    eligibility.assess(). GET, like /search — nothing here writes anything but
    the search log."""
    if config.APP_ENV != "staging":
        abort(404)
    form = {
        "origin": request.args.get("origin", "").strip().upper(),
        "destination": request.args.get("destination", "").strip().upper(),
        "date": request.args.get("date", "").strip(),
        "cabin": request.args.get("cabin", "economy"),
        "passengers": _adults(request.args.get("passengers")),
    }
    try:
        mode = duffel_http.mode()
    except RuntimeError as exc:
        mode, mode_error = None, str(exc)
    else:
        mode_error = None
    context = {"nav": "eligibility", "form": form, "cabins": offer_eligibility.CABINS,
               "budget": live_search.budget(), "mode": mode, "error": mode_error,
               "results": None, "verdicts": offer_eligibility.VERDICTS}

    if mode_error or not any((form["origin"], form["destination"], form["date"])):
        return render_template("eligibility.html", **context)

    problems = []
    if not (_IATA.match(form["origin"]) and _IATA.match(form["destination"])):
        problems.append("origin and destination must be 3-letter IATA codes")
    try:
        datetime.strptime(form["date"], "%Y-%m-%d")
    except ValueError:
        problems.append("date must be YYYY-MM-DD")
    if form["cabin"] not in offer_eligibility.CABINS:
        problems.append("unknown cabin")
    if problems:
        return render_template("eligibility.html", **{**context, "error": "; ".join(problems).capitalize() + "."}), 400

    try:
        result = offer_eligibility.search(
            origin=form["origin"], destination=form["destination"], departure_date=form["date"],
            cabin=form["cabin"], passengers=form["passengers"], source="eligibility",
            account_id=_account())
    except (DuffelError, RuntimeError) as exc:
        return render_template("eligibility.html", **{**context, "budget": live_search.budget(),
                                                      "error": str(exc)})

    for row in result["rows"]:
        slices = [v for v in (slice_view(s) for s in row["offer"].get("slices", [])) if v]
        row["legs"] = slices
        row["raw_conditions"] = json.dumps({"offer": row["offer_conditions"],
                                            "slices": row["slice_conditions"]}, indent=2, sort_keys=True)
    carriers = sorted({r["carrier"] for r in result["rows"] if r["carrier"]})
    return render_template("eligibility.html", **{
        **context, "budget": live_search.budget(), "results": result,
        "counts": offer_eligibility.verdict_counts(result["rows"]), "carriers": carriers})


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
    """Every traveler on the booking, from repeated form fields.

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
# the traveler is named and nobody reaches a payment screen they cannot use.
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
            out.append(f"Traveler {i}: check {', '.join(bad)}")
    return out


def fetch_offer_view(offer_id):
    return offer_view(duffel_http.request("GET", f"/air/offers/{offer_id}", label="ui_offer"))


def _saved_travelers_for_picker(account_id):
    """Passenger-form fields plus default_cost_center_id, so picking a saved
    traveler can default the booking's cost center to theirs — the rest of
    a Traveler's profile has no business on this page."""
    return [{**{k: t[k] for k in db.TRAVELER_FIELDS}, "default_cost_center_id": t["default_cost_center_id"]}
            for t in db.travelers(account_id)]


@app.route("/book/passenger", methods=["POST"])
@auth.login_required
def passenger_step():
    offer_id = request.form["offer_id"]
    try:
        offer = fetch_offer_view(offer_id)
        prefill = auth.profile_of(auth.current_user())
        # The booker is usually traveler one; the rest start empty.
        people = [prefill] + [{} for _ in range(offer["passenger_count"] - 1)]
        return render_template("passenger.html", nav="search", offer=offer,
                               people=people,
                               # only the passenger fields reach the page —
                               # internal ids and timestamps have no business there
                               saved=_saved_travelers_for_picker(_account()),
                               cost_centers=db.cost_centers_for_account(_account(), active_only=True))
    except (DuffelError, RuntimeError) as exc:
        return render_template("results.html", nav="search", offers=None, form={},
                               error=f"{exc} — offers expire; search again.")


@app.route("/book/payment", methods=["POST"])
@auth.login_required
def payment_step():
    offer_id = request.form["offer_id"]
    cost_center_id = request.form.get("cost_center_id", "").strip()
    people = passengers_from_form()
    problems = passenger_problems(people)
    if problems:
        return render_template("passenger.html", nav="search",
                               offer=fetch_offer_view(offer_id), people=people,
                               saved=_saved_travelers_for_picker(_account()),
                               cost_centers=db.cost_centers_for_account(_account(), active_only=True),
                               error=" · ".join(problems)), 400
    try:
        return render_template("payment.html", nav="search",
                               offer=fetch_offer_view(offer_id), people=people,
                               cost_center_id=cost_center_id)
    except (DuffelError, RuntimeError) as exc:
        return render_template("results.html", nav="search", offers=None, form={},
                               error=f"{exc} — offers expire; search again.")


def _fare_disposition(order):
    """orders.refundable/fare_conditions, frozen at purchase time from the
    same `conditions` object eligibility.assess() already reads off an
    order. `refundable` is a fast derived flag for filtering; the full
    object is kept so a later assessment can reason over penalties and
    currency, not just a yes/no. None (not False) when the airline hasn't
    published refund conditions at all — matches how eligibility.py treats
    an unknown penalty as unknown, never as a silent no.

    This is the data eligibility.assess() would need to stop recommending
    an exchange that forfeits value instead of returning it — capturing it
    is this phase's job; teaching the decision engine to read it is not
    (out of scope here, flagged in the writeup).
    """
    conditions = order.get("conditions") or {}
    refund = conditions.get("refund_before_departure")
    refundable = bool(refund.get("allowed")) if isinstance(refund, dict) else None
    return refundable, conditions


@app.route("/book", methods=["POST"])
@auth.login_required
def book():
    offer_id = request.form["offer_id"]
    account_id = _account()
    # Validated against the account, not trusted as-is — a raw id from a
    # form field is exactly the shape of mistake this codebase has spent
    # a lot of effort closing elsewhere (see /decisions, invoice_lines_for).
    cost_center_id = request.form.get("cost_center_id", "").strip() or None
    if cost_center_id and not db.cost_center(cost_center_id, account_id):
        cost_center_id = None
    try:
        offer = duffel_http.request("GET", f"/air/offers/{offer_id}", label="ui_offer")
    except (DuffelError, RuntimeError) as exc:
        return render_template("results.html", nav="search", offers=None, form={},
                               error=f"{exc} — offers expire; search again.")

    seats = offer.get("passengers", []) or [{}]
    people = passengers_from_form(len(seats))
    saved = _saved_travelers_for_picker(account_id)
    problems = passenger_problems(people)
    if problems:
        return render_template("passenger.html", nav="search", offer=offer_view(offer),
                               people=people, saved=saved,
                               error=" · ".join(problems)), 400

    # Staging books nothing — live or sandbox — unless live orders are on.
    # duffel_http would refuse the order call anyway; refusing here means no
    # card is authorized and no spend is reserved first.
    if config.APP_ENV == "staging" and not config.DUFFEL_LIVE_ORDERS_ENABLED:
        return render_template("payment.html", nav="search", offer=offer_view(offer),
                               people=people, error=duffel_http.staging_orders_refusal()), 403

    # --- gates: everything that can stop a booking outright, checked here
    # before anything is created anywhere — same position passenger_problems()
    # already occupies, extended rather than bolted on beside it.

    card = db.account_card(account_id)
    if not card:
        return render_template("payment.html", nav="search", offer=offer_view(offer),
                               people=people,
                               error="No payment method on file — link a company card "
                                     "before booking."), 402

    decision = policy.evaluate(account_id, offer)
    if decision.blocked:
        detail = "; ".join(r.detail for r in decision.results if r.enforcement == "block")
        return render_template("payment.html", nav="search", offer=offer_view(offer),
                               people=people,
                               error=f"Blocked by travel policy: {detail}"), 403

    if decision.requires_approval:
        # Duffel is never called. Offer requests are single-use and offers
        # expire (FINDINGS.md §4), so an approval that sits overnight can't
        # hold a live offer to purchase later — the snapshot and the
        # offer's current price (the ceiling) are what a later purchase
        # step re-searches and matches against, not this offer_id.
        facts = policy.extract_itinerary_facts(offer)
        db.booking_request_create(
            account_id, requested_by=auth.current_user()["id"],
            itinerary_snapshot={"slices": offer.get("slices", []), **facts},
            amount=facts["amount"], currency=facts["currency"],
            policy_result=decision.to_json())
        flash("This booking needs manager approval before it can be purchased — "
              "you'll be notified once it's reviewed.", "reservation_error")
        return redirect(url_for("trips"))

    # A double-submitted Pay button, recovered before spending anything new.
    # The offer_request_already_booked handler below is the backstop for
    # the narrower race between this check and the Duffel call itself.
    existing = db.order_for_offer(account_id, offer_id)
    if existing:
        return redirect(url_for("trip_booked", order_id=existing["order_id"]))

    # --- staging live spend. The token decides the mode, read once and stored
    # on the order. A live ticket is allowlisted, capped, and recorded in the
    # live_spend ledger (live_guard.reserve) before the card is even
    # authorized; a refusal is audited there and nothing else happens.
    duffel_mode = duffel_http.mode()
    spend = None
    if duffel_mode == "live":
        try:
            spend = live_guard.reserve(
                "booking", account_id=account_id, email=auth.current_user()["email"],
                amount=offer["total_amount"], currency=offer["total_currency"], reference=offer_id)
        except live_guard.LiveSpendRejected as exc:
            return render_template("payment.html", nav="search", offer=offer_view(offer),
                                   people=people, error=f"Live booking refused: {exc.detail}"), 403

    # --- payment: authorize now, capture only once Duffel confirms the
    # order exists. idempotency_key means a retried request (network blip,
    # double click) returns the same authorization rather than creating a
    # second hold on the company's card.
    try:
        intent = billing.authorize_fare(
            account_id, amount=offer["total_amount"], currency=offer["total_currency"],
            idempotency_key=f"book-{offer_id}")
    except billing.CardError as exc:
        live_guard.release(spend)
        return render_template("payment.html", nav="search", offer=offer_view(offer),
                               people=people, error=f"Payment failed: {exc}"), 402

    order_body = {
        "data": {
            "type": "instant",
            "selected_offers": [offer_id],
            # Each traveler is matched to one of the offer's passenger ids.
            # Sending the same details for every seat, which is what the
            # single-passenger version did, books several copies of one person.
            "passengers": [{"id": seat["id"], **person}
                           for seat, person in zip(seats, people)],
            # TD's own Duffel balance still funds the actual purchase — a
            # working buffer topped up separately, not a float extended
            # to the customer. The company's card is charged above, in
            # the same request; this call is unchanged from before.
            "payments": [{"type": "balance", "currency": offer["total_currency"],
                          "amount": offer["total_amount"]}],
        }
    }
    try:
        # A live payment is refused inside duffel_http.request unless this
        # reservation covers it (None in test mode, which needs none).
        with live_guard.authorized(spend):
            order = duffel_http.request("POST", "/air/orders", body=order_body, label="ui_book")
    except (DuffelError, RuntimeError) as exc:
        # The hold must never become a charge for a ticket that doesn't
        # exist. Cancel, don't capture-then-refund: an authorization that
        # never captures never appears on the customer's statement; a
        # charge-then-refund is two lines and a support ticket for a
        # ticket that was never issued.
        billing.cancel_authorization(intent.id)
        # Duffel answered with an error (or the request was never sent), so no
        # ticket was issued on this attempt: the reservation is released.
        live_guard.release(spend)
        if isinstance(exc, DuffelError) and "offer_request_already_booked" in (exc.codes or []):
            # Almost always a double-submitted Pay button. The first attempt
            # succeeded, so show that booking rather than an error implying the
            # customer was not booked at all.
            existing = db.order_for_offer(account_id, offer_id)
            if existing:
                return redirect(url_for("trip_booked", order_id=existing["order_id"]))
            return render_template("results.html", nav="search", offers=None, form={},
                                   error="That search has already been booked from — "
                                         "offers are single use. Search again for fresh "
                                         "prices.")
        return render_template("results.html", nav="search", offers=None, form={},
                               error=str(exc))

    snap = OrderSnapshot.from_duffel(order)

    # Gate monitoring on fare conditions at booking time. Never default to on.
    assessment = snap.eligibility
    log_eligibility(order["id"], assessment)

    refundable, fare_conditions = _fare_disposition(order)

    # Persisted BEFORE the capture attempt, deliberately. A capture failure
    # after this point is a payment problem on a real, known order — a
    # human can resolve that. A capture failure before this point used to
    # mean a real Duffel order existed with no local record of it at all:
    # unrecoverable, because nothing in the system knew to look for it.
    upsert_order({
        "order_id": order["id"],
        "offer_id": offer_id,
        "booking_reference": order.get("booking_reference", ""),
        "paid": order["total_amount"], "currency": order["total_currency"],
        "route": str(snap.route), "itinerary": str(snap.itinerary),
        "carrier": snap.carrier_name, "departure_date": snap.departure_date,
        "monitoring": assessment.should_poll,
        "booked_at": datetime.now(timezone.utc).isoformat(),
        "last_decision": None, "raw": order,
        # This route purchases via Duffel on TD's own balance — under the
        # corporate model that's every booking, always, not an internal-only step.
        "source": "td_rebook",
        # Duffel's cash-offer search is the only thing this route can book —
        # there is no points/award path through it.
        "fare_type": "cash",
        "refundable": refundable,
        "fare_conditions": fare_conditions,
        "stripe_payment_intent_id": intent.id,
        "cost_center_id": cost_center_id,
        "duffel_mode": duffel_mode,
    })
    live_guard.settle(spend, order_id=order["id"])

    # A free observation of this carrier's real available_actions, every
    # time — the same near-zero-marginal-cost idea as fare-observation data.
    # Only recorded when Duffel actually returned the field; no signal, no
    # observation (see eligibility.py's carrier_capability docs). Must run
    # after upsert_order() above — the observations log's order_id is a real
    # foreign key into orders, and this order doesn't exist there yet
    # any earlier in this function.
    order_actions = order.get("available_actions")
    if order_actions is not None:
        order_slices = order.get("slices") or [{}]
        db.carrier_capability_record(
            (order.get("owner") or {}).get("iata_code"),
            (order.get("owner") or {}).get("name", ""),
            change_allowed="change" in order_actions,
            fare_brand=order_slices[0].get("fare_brand_name") or "",
            order_id=order["id"])

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

    # Capture: retry once, since transient Stripe errors are common and a
    # retry costs nothing. Never cancel/void the Duffel order from here on —
    # the ticket is real and issued; voiding it because payment capture
    # hiccuped is destructive, and the void call can itself fail, which
    # would leave a worse state than the one being fixed.
    capture_error = None
    for _attempt in (1, 2):
        try:
            billing.capture_authorization(intent.id)
            capture_error = None
            break
        except billing.CardError as exc:
            capture_error = exc

    if capture_error is not None:
        upsert_order({"order_id": order["id"],
                      "payment_capture_failed_at": datetime.now(timezone.utc).isoformat(),
                      "payment_capture_error": str(capture_error)})
        return render_template(
            "error.html", hide_nav=True, order_id=order["id"],
            error="Your booking is confirmed and the ticket is issued, but we couldn't "
                  "complete the card charge. Our team has been notified and will follow "
                  "up to resolve payment — no action is needed from you right now."), 200

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

def _activity_label(row):
    """One audit_events row → Activity Timeline entry (title + detail).
    Customer-facing copy, same spirit as eligibility.CUSTOMER_COPY but for
    the audit log — not a new decision system, just friendlier phrasing of
    what evaluate()/execute() already recorded."""
    kind = row.get("kind")
    if kind == "eligibility":
        title = ("Added to monitoring" if row.get("state") == "monitoring"
                 else "Not eligible for monitoring")
        return {"title": title, "detail": row.get("detail", "")}
    if kind == "execution":
        execution = row.get("execution")
        if execution == "awaiting_confirmation":
            title = "Lower fare found — awaiting confirmation"
        elif execution == "blocked_simulated":
            title = "Lower fare found (simulated)"
        elif execution == "failed":
            title = "Recovery attempt failed"
        elif execution == "executed":
            # Newly reachable — execute() now actually writes this. A bare
            # "Recovery executed" doesn't distinguish a forfeited exchange
            # (nothing came back) from a real one, so it reads the same
            # delivery_type distinction the wallet already surfaces.
            title = {
                "refund_to_card": "Recovery executed — refunded to card",
                "airline_credit": "Recovery executed — airline credit issued",
                "forfeited": "Recovery executed — value forfeited, nothing recovered",
            }.get(row.get("delivery_type"), "Recovery executed")
        else:
            title = "Recovery executed"
        return {"title": title, "detail": row.get("detail", "")}
    if kind == "live_guard":
        return {"title": "Live exchange blocked by staging spend controls",
                "detail": row.get("detail", "")}
    # decision
    if row.get("outcome") == "reshop":
        return {"title": "Lower fare found", "detail": row.get("reason", "")}
    return {"title": "Checked for a lower fare", "detail": "No drop found this check"}


@app.route("/overview")
@auth.login_required
def overview():
    """Account numbers, all of them derived from db.account_summary /
    db.savings_totals_for_account so this page can never disagree with the
    pages it summarises.

    Cash and credit recovered are kept as two separate figures throughout —
    never merged into one "Total Saved". They're different things: one is
    money back on the card, the other is value locked to one employee's
    name at one airline.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    trips_ = [trip_view(r) for r in load_orders()]
    upcoming = sorted((t for t in trips_ if (t["depart_iso"][:10] or "9999") >= today),
                      key=lambda t: t["depart_iso"])
    acct = _account()
    s = db.account_summary(acct)
    savings = db.savings_totals_for_account(acct)
    activity = [{**r, **_activity_label(r)} for r in db.audit_rows_for_account(acct, limit=12)]

    monitored = db.monitored_orders_with_history(acct)
    band = aggregate_difference_band(monitored, currency=s["currency"]) if monitored else None
    band_paid = sum((Decimal(str(o["paid"] or 0)) for o in monitored), Decimal("0"))
    # "Worth today" from each monitored order's own latest known check,
    # falling back to what was paid when no check has run yet — the same
    # forward-fill aggregate_difference_band uses, so the KPI headline and
    # the band it sits above can never disagree.
    band_worth = Decimal("0")
    for o in monitored:
        band_worth += o["points"][-1][1] if o["points"] else Decimal(str(o["paid"] or 0))

    return render_template("overview.html", nav="overview",
                           now_hour=datetime.now(timezone.utc).hour,
                           s=s, savings=savings,
                           upcoming=upcoming[:5],
                           activity=activity,
                           band=band, band_paid=band_paid, band_worth=band_worth,
                           band_recoverable=band_paid - band_worth,
                           monitored_count=len(monitored))


CREDIT_EXPIRY_WARNING_DAYS = 30


def _flag_expiring_credits(credits):
    """The credit ledger is a liability register — expiry is the point of
    it (migrations/021's own comment). Flags active credits expiring within
    CREDIT_EXPIRY_WARNING_DAYS so the template can render them distinctly
    rather than as one more date in a column."""
    now = datetime.now(timezone.utc)
    out = []
    for c in credits:
        c = dict(c)
        c["expiring_soon"] = bool(
            c["status"] == "active" and c["expires_at"]
            and c["expires_at"] - now <= timedelta(days=CREDIT_EXPIRY_WARNING_DAYS))
        out.append(c)
    return out


@app.route("/wallet")
@auth.login_required
def wallet():
    """A ledger, not a stored balance.

    Recovered fares go back to the card that paid, so there is no float being
    held here — the page shows what moved and where it went, and says so.
    """
    account_id = _account()
    rows = db.wallet_transactions(account_id)
    return render_template("wallet.html", nav="wallet", rows=rows,
                           totals=db.wallet_totals(rows),
                           commission_rate=db.account_commission_rate(account_id),
                           airline_credits=_flag_expiring_credits(
                               db.airline_credits_for_account(account_id)),
                           credit_expiry_warning_days=CREDIT_EXPIRY_WARNING_DAYS)


def _json_safe(row):
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in dict(row).items()}


@app.route("/invoices")
@auth.login_required
def invoices():
    """Customer-facing statements list. Generation itself stays a manual,
    ops-triggered action (generate_invoice_route below) — Phase 4's job is
    the real review/approval flow; customers don't self-generate their own
    bill. Real rows only: most accounts have none yet, so the honest state
    is an empty table, not a fabricated sample statement."""
    account_id = _account()
    return render_template("invoices.html", nav="invoices",
                           invoices=db.invoices_for_account(account_id),
                           savings=db.savings_totals_for_account(account_id),
                           commission_rate=db.account_commission_rate(account_id))


@app.route("/invoices/<invoice_id>")
@auth.login_required
def invoice_detail(invoice_id):
    account_id = _account()
    rows = db.invoices_for_account(account_id)
    invoice = next((i for i in rows if str(i["id"]) == invoice_id), None)
    if not invoice:
        return redirect(url_for("invoices"))
    return render_template("invoice_detail.html", nav="invoices", invoice=invoice,
                           lines=db.invoice_lines_for(invoice_id, account_id))


@app.route("/accounts/invoice/generate", methods=["POST"])
@auth.login_required
def generate_invoice_route():
    """Minimal proof this phase's invoice generation works end to end — no
    UI, no draft/review step, no approval; Phase 4 builds the real screen.
    Defaults to last full calendar month; period_start/period_end (YYYY-MM-DD)
    in the form override it. Returns the generated invoice and its lines as
    JSON, since there is nothing to render this phase.
    """
    today = datetime.now(timezone.utc).date()
    default_end = today.replace(day=1)
    default_start = (default_end - timedelta(days=1)).replace(day=1)
    period_start = request.form.get("period_start") or default_start.isoformat()
    period_end = request.form.get("period_end") or default_end.isoformat()

    invoice = db.generate_invoice(_account(), period_start, period_end)
    lines = db.invoice_lines_for(invoice["id"], _account())
    return {"invoice": _json_safe(invoice), "lines": [_json_safe(l) for l in lines]}


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@auth.login_required
def settings():
    """Account-level fields (name/phone/nickname) plus, now, the company's
    own profile — legal_name/billing address/employee_count, added in
    Phase 1 with no write path until this pass. Per-Traveler data (loyalty,
    trusted traveler, preferences) lives on travelers and is edited from
    Travelers, not here. Two independent forms on one page, told apart by
    `form_section` rather than guessing from which fields showed up."""
    user = auth.current_user()
    acct = _account()
    if request.method == "POST":
        if request.form.get("form_section") == "company":
            db.account_company_update(
                acct,
                legal_name=request.form.get("legal_name", "").strip(),
                employee_count=request.form.get("employee_count", "").strip() or None,
                billing_address_line1=request.form.get("billing_address_line1", "").strip(),
                billing_address_line2=request.form.get("billing_address_line2", "").strip(),
                billing_city=request.form.get("billing_city", "").strip(),
                billing_state=request.form.get("billing_state", "").strip(),
                billing_postal_code=request.form.get("billing_postal_code", "").strip(),
                billing_country=request.form.get("billing_country", "").strip())
        else:
            db.account_settings_update(user["id"],
                                       nickname=request.form.get("nickname", "").strip(),
                                       phone_number=request.form.get("phone_number", "").strip())
        return redirect(url_for("settings"))
    return render_template("settings.html", nav="settings", account_user=user,
                           company=db.account_company_fields(acct),
                           cost_centers=db.cost_centers_for_account(acct))


@app.route("/settings/cost-centers")
@auth.login_required
def cost_centers_page():
    """List + create + edit, matching Travelers' own combined list-and-form
    treatment rather than a separate page per action."""
    editing_id = request.args.get("edit")
    editing = db.cost_center(editing_id, _account()) if editing_id else None
    return render_template("cost_centers.html", nav="settings",
                           cost_centers=db.cost_centers_for_account(_account()),
                           editing=editing, form={})


@app.route("/settings/cost-centers/new", methods=["POST"])
@auth.login_required
def cost_center_new():
    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()
    budget_amount = request.form.get("budget_amount", "").strip() or None
    budget_period = request.form.get("budget_period") or None
    if not (code and name):
        flash("Code and name are required.", "reservation_error")
        return redirect(url_for("cost_centers_page"))
    try:
        db.cost_center_create(_account(), code=code, name=name,
                              budget_amount=budget_amount, budget_period=budget_period)
    except Exception:
        # UNIQUE(account_id, code) — a duplicate code is the only realistic
        # failure here, and the honest message is more useful than a 500.
        flash(f"A cost center with code '{code}' already exists.", "reservation_error")
    return redirect(url_for("cost_centers_page"))


@app.route("/settings/cost-centers/<cost_center_id>/edit", methods=["POST"])
@auth.login_required
def cost_center_edit(cost_center_id):
    existing = db.cost_center(cost_center_id, _account())
    if not existing:
        return redirect(url_for("cost_centers_page"))
    db.cost_center_update(
        cost_center_id, _account(),
        code=request.form.get("code", "").strip().upper() or existing["code"],
        name=request.form.get("name", "").strip() or existing["name"],
        budget_amount=request.form.get("budget_amount", "").strip() or None,
        budget_period=request.form.get("budget_period") or None,
        active=request.form.get("active") == "on")
    return redirect(url_for("cost_centers_page"))


# ---------------------------------------------------------------------------
# travelers — passenger profiles, not logins
# ---------------------------------------------------------------------------

def _traveler_form():
    """The full traveler form: booking-profile fields plus the bolt-on
    additions (loyalty, trusted traveler, preferences). Kept in one place so
    traveler_new and traveler_edit build an identical shape."""
    form = {f: request.form.get(f, "").strip() for f in db.TRAVELER_FIELDS}
    form.update({f: request.form.get(f, "").strip()
                for f in db.TRAVELER_TEXT_PROFILE_FIELDS})
    form["clear_plus"] = request.form.get("clear_plus") == "on"
    airlines = request.form.getlist("loyalty_airline")
    numbers = request.form.getlist("loyalty_number")
    form["loyalty_programs"] = [
        {"airline": a.strip(), "member_number": n.strip()}
        for a, n in zip(airlines, numbers) if a.strip() and n.strip()
    ]
    # Same ownership check as book()'s cost_center_id — a center id from
    # another account (or a stale/deleted one) is dropped, not attached.
    default_cost_center_id = request.form.get("default_cost_center_id", "").strip() or None
    if default_cost_center_id and not db.cost_center(default_cost_center_id, _account()):
        default_cost_center_id = None
    form["default_cost_center_id"] = default_cost_center_id
    return form


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
                           travelers=db.travelers(_account()), form={}, editing=None,
                           cost_centers=db.cost_centers_for_account(_account(), active_only=True))


@app.route("/travelers/new", methods=["POST"])
@auth.login_required
def traveler_new():
    form = _traveler_form()
    problem = _traveler_problem(form)
    if problem:
        return render_template("travelers.html", nav="travelers",
                               travelers=db.travelers(_account()), form=form,
                               editing=None, error=problem,
                               cost_centers=db.cost_centers_for_account(_account(), active_only=True)), 400
    db.traveler_save(form, _account())
    return redirect(url_for("travelers"))


@app.route("/travelers/<traveler_id>/edit", methods=["GET", "POST"])
@auth.login_required
def traveler_edit(traveler_id):
    existing = db.traveler(traveler_id, _account())
    if not existing:
        return redirect(url_for("travelers"))
    cost_centers = db.cost_centers_for_account(_account(), active_only=True)
    if request.method == "POST":
        form = _traveler_form()
        problem = _traveler_problem(form)
        if problem:
            return render_template("travelers.html", nav="travelers",
                                   travelers=db.travelers(_account()), form=form,
                                   editing=traveler_id, error=problem,
                                   cost_centers=cost_centers), 400
        db.traveler_save(form, _account(), traveler_id)
        return redirect(url_for("travelers"))
    return render_template("travelers.html", nav="travelers",
                           travelers=db.travelers(_account()),
                           form=existing, editing=traveler_id, cost_centers=cost_centers)


@app.route("/travelers/<traveler_id>/delete", methods=["POST"])
@auth.login_required
def traveler_remove(traveler_id):
    db.traveler_delete(traveler_id, _account())
    return redirect(url_for("travelers"))


@app.route("/trips")
@auth.login_required
def trips():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # trip_view() carries only the bare cost_center_id (a uuid) — resolved
    # to a human code here, once per account, rather than joining per row.
    cc_codes = {str(c["id"]): c["code"] for c in db.cost_centers_for_account(_account())}
    upcoming, past, saved, count = [], [], Decimal("0"), 0
    for record in load_orders():
        t = trip_view(record)
        t["cost_center_code"] = cc_codes.get(str(t["cost_center_id"])) if t["cost_center_id"] else None
        (upcoming if (t["depart_iso"][:10] or "9999") >= today else past).append(t)
        if t["refunded"]:
            saved += Decimal(t["refunded"])
            count += 1
    upcoming.sort(key=lambda t: t["depart_iso"])
    past.sort(key=lambda t: t["depart_iso"], reverse=True)
    acct = _account()
    months = db.monthly_series(acct)
    weeks = db.weekly_activity(acct)
    return render_template("trips.html", nav="trips", upcoming=upcoming, past=past,
                           saved_total=(str(saved) if count else None),
                           saved_currency=(upcoming + past)[0]["currency"] if (upcoming or past) else "USD",
                           saved_count=count,
                           s=db.account_summary(acct),
                           months=months, weeks=weeks,
                           spend_chart=spend_chart(months),
                           activity=activity_chart(weeks),
                           travelers=db.travelers(acct),
                           c_spend=SERIES_SPEND, c_saved=SERIES_SAVED)


@app.route("/trips/<order_id>")
@auth.login_required
def trip_detail(order_id):
    record = find_order(order_id)
    if not record:
        return redirect(url_for("trips"))
    trip = trip_view(record)
    points = price_history(order_id)
    # Baseline steps down at each exchange that actually landed — see
    # execution_steps(). No real exchange has moved real money yet (the
    # sandbox's own change_total_amount never goes negative; see the
    # writeup), so today this always resolves to one flat step, same as
    # before stepping existed — but it is real machinery, not a promise,
    # and needs no further change once a genuine exchange executes.
    original = trip["original_paid"] or trip["paid"]
    steps = execution_steps(order_id, original) if points else None
    band = difference_band(points, Decimal(str(trip["paid"] or 0)), trip["currency"],
                           baseline_steps=steps) if points else None
    return render_template("trip.html", nav="trips", trip=trip, band=band,
                           commission_rate=db.account_commission_rate(record["account_id"]),
                           savings_events=db.savings_events_for_order(order_id))


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
                           source_name=os.environ.get("PRICE_SOURCE", "simulated"),
                           commission_rate=db.account_commission_rate(_account()))


@app.route("/orders/<order_id>/monitor", methods=["POST"])
@auth.login_required
def toggle_monitor(order_id):
    record = find_order(order_id)
    if record:
        # Monitoring can always be turned off, but never on for a fare that
        # cannot win — the toggle must not be able to re-create the bug.
        # Same assessment trip_view() shows, not a second computation.
        a = snapshot_of(record).eligibility
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
    upsert_order({"order_id": order_id, "last_checked_at": datetime.now(timezone.utc),
                  "last_decision": {
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


# ---------------------------------------------------------------------------
# the scheduler — the only thing here that runs without someone clicking
# ---------------------------------------------------------------------------

# One invocation has 60s (vercel.json maxDuration) and a live market search
# measures 9-12s, so only a handful of orders fit. Stop well short of the
# ceiling: an invocation killed mid-order leaves that order's cursor unstamped
# and still first in the queue next time, starving everything behind it.
CRON_BUDGET_SECONDS = 45
CRON_MAX_ORDERS = 8


@app.route("/cron/reshop")
def cron_reshop():
    """Run a reshop cycle over the orders due one, least-recently-checked first.

    No session: Vercel Cron presents CRON_SECRET as a bearer token, which is why
    this endpoint is in PUBLIC_ENDPOINTS — and therefore in test_routes.py's
    EXPECTED_PUBLIC too, or the route walk fails. An unset secret refuses every
    request rather than leaving the scheduler open.

    Decides only. Acting on a RESHOP decision is execute()'s job and still needs
    a human; see RESHOP_AUTOPILOT_ENABLED for where that changes.
    """
    # Strictly the documented shape Vercel Cron sends. removeprefix() alone is a
    # no-op when the prefix is absent, which quietly accepted a bare secret in
    # the Authorization header — one accepted format, not two.
    secret = config.CRON_SECRET
    header = request.headers.get("Authorization", "")
    presented = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
    if not secret or not presented or not hmac.compare_digest(presented, secret):
        return {"error": "unauthorized"}, 401

    started = time.monotonic()
    source = get_price_source("duffel")
    checked, reshop_decided, errors = [], [], []

    for row in db.orders_due_a_check(CRON_MAX_ORDERS):
        if time.monotonic() - started > CRON_BUDGET_SECONDS:
            break
        order_id = row["order_id"]
        try:
            d = _run_cycle(order_id, source)
        except (DuffelError, RuntimeError) as exc:
            # One bad order must not block the queue behind it: stamp it anyway
            # so the cursor moves on and the rest get their turn.
            upsert_order({"order_id": order_id,
                          "last_checked_at": datetime.now(timezone.utc)})
            errors.append({"order_id": order_id, "error": str(exc)[:200]})
            continue
        checked.append(order_id)
        if d is not None and d.outcome is Outcome.RESHOP:
            reshop_decided.append(order_id)

    return {"checked": checked, "reshop_decided": reshop_decided, "errors": errors,
            "elapsed_seconds": round(time.monotonic() - started, 1)}


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
    d = _run_cycle(order_id, sim)

    # Write the outcome through so the rest of the app can be demonstrated —
    # into sim_* columns, never over `paid` or `refunded`. Clearing a
    # simulation therefore restores the truth exactly, not approximately.
    if d and d.should_reshop and d.recovered is not None:
        paid = Decimal(record.get("paid") or 0)
        upsert_order({"order_id": order_id, "simulated": True,
                      "sim_refunded": str(d.recovered),
                      "sim_paid": str(paid - d.recovered)})
    else:
        # A run that no longer reshops must not leave the last one standing.
        upsert_order({"order_id": order_id, "simulated": False,
                      "sim_refunded": None, "sim_paid": None})
    return redirect(url_for("orders"))


@app.route("/orders/<order_id>/reset-sim", methods=["POST"])
@auth.login_required
def reset_sim(order_id):
    if find_order(order_id):
        upsert_order({"order_id": order_id, "simulated": False,
                      "sim_refunded": None, "sim_paid": None})
    return redirect(request.form.get("back") or url_for("orders"))


@app.route("/orders/reset-sim", methods=["POST"])
@auth.login_required
def reset_sim_all():
    """Clear every simulated result on the account in one go — the real
    columns were never touched, so this is a complete undo."""
    for r in load_orders():
        if r.get("simulated"):
            upsert_order({"order_id": r["order_id"], "simulated": False,
                          "sim_refunded": None, "sim_paid": None})
    return redirect(request.form.get("back") or url_for("orders"))


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
    if record.get("source") != "td_rebook":
        # No real Duffel order behind this reservation to change or cancel —
        # book-new-before-cancel-old (the mechanism that would recover
        # savings on an imported reservation) isn't built yet.
        return redirect(url_for("orders"))
    if record.get("executed"):
        # Already in a terminal state — nothing left to confirm.
        return redirect(url_for("trip_detail", order_id=order_id))
    return render_template("confirm_action.html", nav="ops", order=record, action=action)


@app.route("/orders/<order_id>/execute/<action>", methods=["POST"])
@auth.login_required
def execute(order_id, action):
    """Step 2 of 2. Requires the typed confirmation from the previous page."""
    record = find_order(order_id)
    if not record:
        return redirect(url_for("orders"))
    if record.get("source") != "td_rebook":
        return redirect(url_for("orders"))
    if record.get("executed"):
        # Already in a terminal state — refuse rather than let a second
        # exchange or cancel reach Duffel for an order that's done.
        return redirect(url_for("trip_detail", order_id=order_id))

    def refuse(msg):
        return render_template("confirm_action.html", nav="ops", order=record,
                               action=action, error=msg)

    if config.APP_ENV == "staging" and not config.DUFFEL_LIVE_ORDERS_ENABLED:
        return refuse(duffel_http.staging_orders_refusal())

    if request.form.get("confirm_text", "").strip().upper() != "CONFIRM":
        return refuse("Type CONFIRM exactly to proceed.")

    # For the delivery_detail copy below — card-gating means this should
    # always exist by the time an exchange executes, but a card removed
    # after monitoring started shouldn't crash the confirm step.
    card = db.account_card(record.get("account_id")) or {"last4": "on file"}

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
    #
    # Cancel is never keyed on offer_id: there's no offer involved in
    # cancelling an order at all, and change_offer_id is purely an exchange
    # artifact — using it here meant a reshop cycle running between two
    # cancel attempts (which can change last_decision, and with it
    # change_offer_id) could change this key out from under the guard,
    # letting a second cancel reach Duffel. A cancel's claim key is always
    # the empty string instead: fixed, and untouched by anything a cycle does.
    claim_key = offer_id if action == "exchange" else ""
    try:
        attempt = db.claim_execution(order_id, action, claim_key)
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
            spend = None
            if delta > 0:
                body = {"data": {"payment": {"type": "balance",
                                             "currency": change["change_total_currency"],
                                             "amount": str(delta)}}}
                if duffel_http.mode() == "live":
                    # A live top-up is live spend: same allowlist and caps as a
                    # live booking, reserved before the confirm call.
                    spend = live_guard.reserve(
                        "exchange_topup", account_id=record["account_id"],
                        email=auth.current_user()["email"], amount=delta,
                        currency=change["change_total_currency"], order_id=order_id,
                        reference=change["id"])
            with live_guard.authorized(spend):
                result = duffel_http.request(
                    "POST", f"/air/order_changes/{change['id']}/actions/confirm",
                    body=body, label="ui_change_confirm")
            # A confirm that raised may still have landed, so a reservation is
            # only ever settled here, never released on that path.
            live_guard.settle(spend)
            note = f"exchange confirmed at {result.get('confirmed_at')}, change_total {delta}"
            savings = None
            if delta < 0:
                extra = {"refunded": str(-delta), "original_paid": record.get("paid")}
                paid = Decimal(record.get("paid") or 0)
                # refund_to is Duffel's own signal for which path fired, the
                # same field the cancel branch below already reads — Duffel
                # confirmed in writing that an exchange's residual value may
                # be forfeited, refunded to the original payment method, or
                # issued as a future travel credit depending on the original
                # fare's rules. Assuming cash-refund unconditionally (the
                # prior belief here) was wrong; a negative change_total this
                # engine only ever reaches after deciding to reshop always
                # means *something* comes back, so airline_credit is the
                # only other branch reachable at this point — forfeiture
                # would mean nothing was quoted to return in the first
                # place, which the engine's own gate (change_total >= 0 =
                # skip) already refuses to act on before execution.
                is_credit = (result.get("refund_to") or "").replace("_", "") \
                    in ("airlinecredit", "airlinecredits")
                delivery_type = "airline_credit" if is_credit else "refund_to_card"
                delivery_detail = (f"{record.get('carrier')} account" if is_credit
                                   else f"card ending in {card['last4']}")
                savings = {"old_amount": str(paid), "new_amount": str(paid + delta),
                          "realized_savings": str(-delta),
                          "delivery_type": delivery_type, "delivery_detail": delivery_detail}
        else:
            quote = duffel_http.request("POST", "/air/order_cancellations", body={
                "data": {"order_id": order_id}}, label="ui_cancel_quote")
            result = duffel_http.request(
                "POST", f"/air/order_cancellations/{quote['id']}/actions/confirm",
                body={"data": {}}, label="ui_cancel_confirm")
            note = (f"cancelled at {result.get('confirmed_at')}, "
                    f"refunded {result.get('refund_amount')} {result.get('refund_currency')}")
            paid = Decimal(record.get("paid") or 0)
            refunded = Decimal(result.get("refund_amount") or 0)
            is_credit = (result.get("refund_to") or "").replace("_", "") \
                in ("airlinecredit", "airlinecredits")
            if refunded > 0:
                delivery_type = "airline_credit" if is_credit else "refund_to_card"
                delivery_detail = (f"{record.get('carrier')} account" if is_credit
                                   else f"card ending in {card['last4']}")
            else:
                # Duffel confirmed the cancellation but nothing came back —
                # this fare's rules forfeit the residual value rather than
                # refunding or crediting it. A real, recordable event
                # (the ticket is gone and nothing was recovered for it),
                # not silently dropped just because no cash moved — the
                # old `if refunded > 0` gate used to skip this entirely.
                delivery_type = "forfeited"
                delivery_detail = "no refund or credit issued — fare rules forfeit the residual value"
            # Unlike exchange, this branch always reaches here once Duffel
            # confirms — 0.00 included, so a forfeited cancellation records
            # an explicit zero rather than leaving orders.refunded NULL the
            # way "no execution happened at all" would read.
            extra = {"refunded": str(refunded), "original_paid": record.get("paid")}
            savings = {"old_amount": str(paid), "new_amount": str(paid - refunded),
                      "realized_savings": str(refunded),
                      "delivery_type": delivery_type, "delivery_detail": delivery_detail}
    except live_guard.LiveSpendRejected as exc:
        # Refused before the confirm call: the order change was created but
        # never confirmed, so nothing landed. Release the claim so the exchange
        # can be retried once the caps allow; the refusal is already audited.
        db.release_execution(attempt["id"])
        return refuse(f"Live exchange refused: {exc.detail}")
    except (DuffelError, RuntimeError) as exc:
        # The order-change *create* call is safe to retry; a failed confirm is
        # not, because the exchange may have landed anyway. Only release the
        # claim when nothing could have been confirmed.
        if duffel_change_id is None:
            db.release_execution(attempt["id"])
        else:
            db.finish_execution(attempt["id"], "failed", note=str(exc),
                                duffel_change_id=duffel_change_id)
        # The audit trail gets a row even on failure — an attempted
        # execution that didn't land is exactly as audit-worthy as one
        # that did, maybe more so.
        db.audit_append({
            "kind": "execution", "order_id": order_id, "action": action,
            "source": last.get("source") or "operator",
            "execution": "failed", "detail": str(exc),
            "currency": record.get("currency") or "",
        })
        return refuse(str(exc))

    db.finish_execution(attempt["id"], "succeeded", note=note,
                        duffel_change_id=duffel_change_id, result=result)

    audit_payload = {
        "kind": "execution", "order_id": order_id, "action": action,
        "source": last.get("source") or "operator",
        "execution": "executed", "detail": note,
        "currency": record.get("currency") or "",
        "old_paid": record.get("paid"),
    }

    if savings:
        # The account's real rate, never the module-level 0.25 constant —
        # savings_events.commission_rate exists precisely so a per-account
        # rate (Phase 1's accounts.commission_rate) doesn't have to fight
        # a single global default. fee_split()'s own preview math during
        # the decision/cycle step still uses SERVICE_FEE_RATE — threading
        # a per-account rate through the decision engine's preview text is
        # a separate change from what actually gets billed, out of scope
        # here (the engine's tested decision logic is untouched).
        rate = db.account_commission_rate(record["account_id"])
        event = db.savings_event_create(order_id, execution_attempt_id=attempt["id"],
                                        currency=record.get("currency") or "",
                                        commission_rate=rate, **savings)
        audit_payload["delivery_type"] = savings["delivery_type"]
        audit_payload["recovered"] = savings["realized_savings"]
        audit_payload["service_fee"] = str(event["commission_amount"])

        if savings["delivery_type"] == "airline_credit":
            # The liability register: credit sits in the traveler's own
            # loyalty account and leaves with them if they quit. Duffel's
            # cancellation response carries no loyalty account number or
            # credit expiry to attach (FINDINGS.md's verified response
            # shape) — left blank/None, editable later once a booking is
            # linked to a traveler's own loyalty_programs.
            credit = db.airline_credit_create(
                record["account_id"], traveler_id=record.get("traveler_id"),
                airline=record.get("carrier") or "",
                loyalty_account_reference="",
                order_id=order_id, savings_event_id=event["id"],
                amount_issued=savings["realized_savings"],
                currency=record.get("currency") or "", expires_at=None)
            # Bidirectional: airline_credits already points back at the
            # savings_event that created it; this closes the other
            # direction so a recovery traces forward to the credit it
            # produced without a separate lookup by order_id.
            db.savings_event_link_credit(event["id"], credit["id"])

    # finish_execution and the savings/credit writes above are durable
    # regardless of what happens next — only old_paid needed data this
    # code already had. new_paid needs a fresh GET, fetched only now, right
    # before the row that carries it: audit_events is append-only (no
    # UPDATE), so this is the one chance to attach it, but a transient
    # failure here must not cost the execution/savings/credit trail
    # already committed above.
    fresh = duffel_http.request("GET", f"/air/orders/{order_id}", label="ui_order_refresh")
    audit_payload["new_paid"] = fresh.get("total_amount")

    # The audit trail — an execution is the single most audit-worthy thing
    # this system does, and until now it left no trace here at all.
    # Written once, after the fact; distinct from the
    # 'awaiting_confirmation'/'blocked_simulated' rows the decision cycle
    # already logs before a human ever confirms anything.
    db.audit_append(audit_payload)

    upsert_order({"order_id": order_id, "raw": fresh, "monitoring": False,
                  "executed": note, "paid": fresh["total_amount"], **extra})
    return redirect(url_for("trip_detail", order_id=order_id))


@app.route("/decisions")
@auth.login_required
def decisions():
    # Was db.audit_rows(limit=200) — ops-wide, no account filter at all, so
    # any authenticated user could read every other account's decisions,
    # eligibility verdicts, and executions. Scoped to the requesting
    # account, same as the Activity Timeline this already powers.
    return render_template("decisions.html", nav="log",
                           rows=db.audit_rows_for_account(_account(), limit=200))


if __name__ == "__main__":
    print(f"Trip Difference → http://localhost:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False)
