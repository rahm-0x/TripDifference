"""
Live-order observation harness for the reshop-economics validation effort.

Duffel's sandbox hardcodes change_total_amount at +125.00 no matter what
(docs/architecture.md's Duffel sandbox section), so the decision engine's
economics have never been checked against a real quote. This script is the
first step towards that: for every live, monitored order, it runs the exact
same engine.evaluate() decision the app already makes, against the real
Duffel price source, and records what it saw — including a positive
change_total that a later gate would have skipped anyway, since a +40 quote
and a +400 quote are different data points and both matter for validating
the model. See validation/report.py for the summary this data feeds.

It records data and executes nothing. Concretely:

  - Refuses to run at all unless OBSERVE_ONLY=1 is set in the environment.
  - Never imports app.py, so it has no path to the confirmation step app.py
    performs for a real exchange or cancellation. The only Duffel calls made
    here are the two prices.DuffelPriceSource already makes for every
    ordinary reshop cycle: a fresh market search, and an order-change
    *quote* request — Duffel prices the exchange without committing to it.
    Turning a quote into a real change is a separate, later call this file
    never makes.
  - validation/test_observe_safety.py scans this file's own source for any
    reference to that later call or to the route that makes it, and fails
    outright if one appears. That test is the actual interlock — keep this
    file free of both regardless of how safe any particular line looks in
    isolation.
  - Does not change engine.py, any gate, any threshold, or any ReshopPolicy
    default. GATE_BY_REASON below is read entirely off the Decision object
    evaluate() already returns, plus OrderSnapshot fields (.eligibility,
    .changeable) it already exposes publicly — no new engine hook exists or
    was needed for this.

Usage:
    OBSERVE_ONLY=1 python validation/observe.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _require_observe_only():
    if os.environ.get("OBSERVE_ONLY") != "1":
        sys.exit(
            "Refusing to run: this script prices a real exchange quote from "
            "Duffel for every monitored order, and must only run when that's "
            "deliberate. Set OBSERVE_ONLY=1 to confirm and run again."
        )


# Checked before any project import — including db (psycopg) and duffel_http
# — so a missing OBSERVE_ONLY=1 is the first thing this script reports even
# in a partially-configured environment, not something that can get lost
# behind an unrelated import error.
_require_observe_only()

from datetime import datetime, timezone  # noqa: E402

import db  # noqa: E402
import duffel_http  # noqa: E402
from engine import OrderSnapshot, Reason, ReshopPolicy, evaluate  # noqa: E402
from prices import DuffelPriceSource  # noqa: E402

# Untouched dataclass defaults — this tool observes the existing policy, it
# does not get to tune it.
POLICY = ReshopPolicy()

# Where each Reason sits in engine.evaluate()'s fixed sequence of checks.
# This is a position, not a piece of engine state — engine.py was not
# changed to produce it; it's read off the order the `if ...: return
# decide(...)` statements appear in evaluate(), top to bottom:
#   1 eligibility (should_poll)      6 change offers exist
#   2 order.changeable               7 identical itinerary found
#   3 void window                    8 change_total >= 0 check
#   4 departure buffer               9 profitability floor
#   5 has_duffel_order (execution)  10 reshop decision
#
# FARE_NOT_CHANGEABLE is the one Reason reachable from two different checks
# (gate 1's eligibility assessment, or gate 2's raw order.changeable flag)
# that happen to share a single enum value. In practice gate 2 can only
# fire when gate 1 didn't, so _gate() below disambiguates using the
# snapshot's own already-public fields rather than assuming which one it
# was from the Reason alone.
GATE_BY_REASON = {
    Reason.ELIGIBILITY_UNKNOWN: 1,
    Reason.PENALTY_TOO_HIGH: 1,
    Reason.IN_VOID_WINDOW: 3,
    Reason.TOO_CLOSE_TO_DEPARTURE: 4,
    Reason.NO_EXECUTION_MECHANISM: 5,
    Reason.NO_CHANGE_OFFERS: 6,
    Reason.NO_IDENTICAL_ITINERARY: 7,
    Reason.CHANGE_TOTAL_NOT_NEGATIVE: 8,
    Reason.BELOW_FLOOR: 9,
    Reason.PROFITABLE_DROP: 10,
}


def _gate(snapshot, decision):
    if decision.reason is Reason.FARE_NOT_CHANGEABLE:
        if snapshot.eligibility is not None and not snapshot.eligibility.should_poll:
            return 1
        return 2
    return GATE_BY_REASON[decision.reason]


def _snapshot_of(record):
    """Read-only equivalent of app.py's snapshot_of(). Reimplemented here,
    rather than imported, so this module has no import path to app.py at
    all — see the module docstring."""
    has_card = bool(db.account_card(record.get("account_id")))
    return OrderSnapshot.from_duffel(record["raw"], fare_type=record.get("fare_type") or "cash",
                                      has_card=has_card, fallback=record)


def _days_to_departure(snapshot, now):
    dep = snapshot.departing_at
    if dep is None and snapshot.departure_date:
        try:
            dep = datetime.strptime(snapshot.departure_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            dep = None
    if dep is None:
        return None
    return round((dep - now).total_seconds() / 86400.0)


def _eligible_orders():
    """Every live, monitored order across every account. 'Live' means a real
    Duffel order backs it (raw is non-empty) — a manual/imported reservation
    has no order-change mechanism to quote against at all (engine.evaluate's
    own NO_EXECUTION_MECHANISM gate), so there is nothing here to observe."""
    orders = []
    for acct in db.q("SELECT id FROM accounts", fetch="all"):
        for record in db.load_orders(acct["id"]):
            if record.get("monitoring") and record.get("raw"):
                orders.append(record)
    return orders


def _insert(row):
    db.q(
        """INSERT INTO reshop_observations
               (order_id, carrier, route, days_to_departure, original_fare,
                currency, change_total_amount, market_best, market_delta,
                gate, skip_reason)
           VALUES (%(order_id)s, %(carrier)s, %(route)s, %(days_to_departure)s,
                   %(original_fare)s, %(currency)s, %(change_total_amount)s,
                   %(market_best)s, %(market_delta)s, %(gate)s, %(skip_reason)s)""",
        row,
    )


def observe_one(record, source, now):
    """Evaluate one order and append its observation row. Calls
    engine.evaluate() exactly as the app's own reshop cycle does — same
    policy, same snapshot shape — with log=False so this doesn't also write
    into the app's decisions log or audit trail; reshop_observations is this
    tool's own record."""
    snap = _snapshot_of(record)
    decision = evaluate(snap, source, policy=POLICY, now=now, log=False)
    row = {
        "order_id": record["order_id"],
        "carrier": snap.itinerary.carrier_iata,
        "route": str(snap.route),
        "days_to_departure": _days_to_departure(snap, now),
        "original_fare": decision.paid,
        "currency": decision.currency,
        "change_total_amount": decision.change_total,
        "market_best": decision.market_best,
        "market_delta": decision.market_delta,
        "gate": _gate(snap, decision),
        "skip_reason": decision.reason.value,
    }
    _insert(row)
    return row


def main():
    duffel_http.token()  # fail fast with a clear message if DUFFEL_TOKEN is missing or wrong

    source = DuffelPriceSource()
    now = datetime.now(timezone.utc)

    orders = _eligible_orders()
    print(f"observing {len(orders)} live, monitored order(s) against {source.name}...")

    ok, failed = 0, 0
    for record in orders:
        try:
            row = observe_one(record, source, now)
        except Exception as exc:
            failed += 1
            print(f"  {record['order_id']}: FAILED — {type(exc).__name__}: {exc}")
            continue
        ok += 1
        print(f"  {record['order_id']}: gate {row['gate']} ({row['skip_reason']}), "
              f"change_total={row['change_total_amount']}")

    print(f"done — {ok} recorded, {failed} failed")


if __name__ == "__main__":
    main()
