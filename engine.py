"""
Reshop decision engine.

Given an order and a PriceSource, decide whether to exchange the ticket.

The one rule that matters, and the one this engine exists to enforce:

    change_total_amount is the ONLY number that decides anything.

A market re-search showing a cheaper fare proves nothing. It is a different
number computed a different way, and acting on it books a loss. The engine
records the market delta for diagnostics and never branches on it. There is a
test asserting exactly that (test_cheaper_market_but_positive_change_total).
"""

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from pathlib import Path

import eligibility
import paths
from eligibility import Eligibility, EligibilityReason
from prices import Itinerary, Route, SliceSpec

DECISION_LOG = paths.data_path("decisions.log")

# Our cut of whatever we recover for the customer. Named so it can be tuned.
SERVICE_FEE_RATE = Decimal("0.25")
CENTS = Decimal("0.01")


class Outcome(str, Enum):
    """What the engine decided. Independent of whether anything was executed."""
    RESHOP = "reshop"
    SKIP = "skip"


class Execution(str, Enum):
    """
    What happened *after* the decision. Kept separate from Outcome so that
    'decided not to exchange' and 'decided to exchange, execution blocked'
    can never be confused for one another.
    """
    NOT_APPLICABLE = "not_applicable"              # decision was skip
    AWAITING_CONFIRMATION = "awaiting_confirmation"  # live, needs the two-step confirm
    BLOCKED_SIMULATED = "blocked_simulated"        # simulated price must never hit Duffel
    EXECUTED = "executed"
    FAILED = "failed"


EXECUTION_COPY = {
    Execution.NOT_APPLICABLE: "",
    Execution.AWAITING_CONFIRMATION:
        "Exchange not executed — awaiting operator confirmation.",
    Execution.BLOCKED_SIMULATED:
        "Exchange not executed — the decision came from the simulated price "
        "source, which must never trigger a real Duffel call.",
    Execution.EXECUTED: "Exchange executed.",
    Execution.FAILED: "Exchange attempted but failed.",
}


class Reason(str, Enum):
    PROFITABLE_DROP = "profitable_drop"
    FARE_NOT_CHANGEABLE = "fare_not_changeable"
    PENALTY_TOO_HIGH = "penalty_too_high"
    ELIGIBILITY_UNKNOWN = "eligibility_unknown"
    IN_VOID_WINDOW = "in_void_window"
    TOO_CLOSE_TO_DEPARTURE = "too_close_to_departure"
    NO_CHANGE_OFFERS = "no_change_offers"
    NO_IDENTICAL_ITINERARY = "no_identical_itinerary"
    CHANGE_TOTAL_NOT_NEGATIVE = "change_total_not_negative"
    BELOW_FLOOR = "below_floor"


# Eligibility reasons map onto engine skip reasons.
_ELIGIBILITY_TO_REASON = {
    EligibilityReason.NO_CHANGE_ACTION: Reason.FARE_NOT_CHANGEABLE,
    EligibilityReason.CHANGE_NOT_ALLOWED: Reason.FARE_NOT_CHANGEABLE,
    EligibilityReason.PENALTY_TOO_HIGH: Reason.PENALTY_TOO_HIGH,
    EligibilityReason.CONDITIONS_MISSING: Reason.ELIGIBILITY_UNKNOWN,
    EligibilityReason.PENALTY_UNKNOWN: Reason.ELIGIBILITY_UNKNOWN,
    EligibilityReason.PENALTY_CURRENCY_MISMATCH: Reason.ELIGIBILITY_UNKNOWN,
}


def fee_split(recovered, rate=SERVICE_FEE_RATE):
    """
    Split a recovered amount into our fee and the customer's net.

    Rounded to cents, and the two always sum back to `recovered` exactly — the
    net absorbs the rounding so the arithmetic is verifiable by hand.
    """
    recovered = Decimal(recovered).quantize(CENTS, rounding=ROUND_HALF_UP)
    fee = (recovered * rate).quantize(CENTS, rounding=ROUND_HALF_UP)
    return recovered, fee, recovered - fee


def _parse_dt(value):
    """Duffel mixes '...Z', '+00:00' and naive local-ish stamps. Assume UTC when naive."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class ReshopPolicy:
    """
    min_saving is the profitability floor: the refund must clear it or we do
    nothing. Set it above your own cost of executing the change.
    """
    min_saving: Decimal = Decimal("20.00")
    departure_buffer_hours: int = 24
    respect_void_window: bool = True


@dataclass
class OrderSnapshot:
    id: str
    total: Decimal
    currency: str
    slice_id: str
    route: Route
    itinerary: Itinerary
    departure_date: str
    departing_at: object = None
    cabin: str = "economy"
    changeable: bool = True
    void_window_ends_at: object = None
    booking_reference: str = ""
    carrier_name: str = ""
    eligibility: object = None      # eligibility.Assessment, when built from a payload

    @classmethod
    def from_duffel(cls, order):
        """Build from a raw Duffel order payload. Tolerant of missing fields."""
        sl = (order.get("slices") or [{}])[0]
        segments = sl.get("segments") or []

        carriers, numbers = [], []
        for seg in segments:
            mc = seg.get("marketing_carrier") or {}
            carriers.append(mc.get("iata_code") or "")
            numbers.append(str(seg.get("marketing_carrier_flight_number") or ""))
        carrier = carriers[0] if carriers else ""

        departing_at = _parse_dt(segments[0].get("departing_at")) if segments else None

        # available_actions is authoritative. conditions.change_before_departure
        # is unreliable in BOTH directions and must never be used to decide:
        #
        #   Iberia  — conditions.allowed=True,  no 'change' action → API 422s
        #   Duffel Airways / BA — conditions.allowed=False, 'change' action
        #                          present → changes work fine (verified)
        #
        # So conditions cannot grant permission and cannot veto it either. It is
        # only a fallback when available_actions is absent entirely.
        conditions = order.get("conditions") or {}
        cbd = conditions.get("change_before_departure") or {}
        cond_allowed = cbd.get("allowed")
        actions = order.get("available_actions")

        if actions is not None:
            allowed = "change" in actions
        elif cond_allowed is not None:
            allowed = bool(cond_allowed)
        else:
            allowed = True  # nothing to go on; let the API be the judge

        return cls(
            id=order["id"],
            total=Decimal(str(order["total_amount"])),
            currency=order["total_currency"],
            slice_id=sl.get("id", ""),
            route=Route((sl.get("origin") or {}).get("iata_code", ""),
                        (sl.get("destination") or {}).get("iata_code", "")),
            itinerary=Itinerary(carrier, tuple(numbers), tuple(carriers)),
            departure_date=(segments[0].get("departing_at", "")[:10] if segments else ""),
            departing_at=departing_at,
            cabin=(segments[0].get("passengers") or [{}])[0].get("cabin_class", "economy") if segments else "economy",
            changeable=bool(allowed),
            void_window_ends_at=_parse_dt(order.get("void_window_ends_at")),
            booking_reference=order.get("booking_reference", ""),
            carrier_name=(order.get("owner") or {}).get("name", ""),
            eligibility=eligibility.assess(order),
        )


@dataclass
class Decision:
    order_id: str
    source: str
    outcome: Outcome
    reason: Reason
    detail: str
    paid: Decimal
    currency: str
    floor: Decimal
    route: str = ""
    itinerary: str = ""
    market_best: object = None
    market_delta: object = None
    change_total: object = None
    new_total: object = None
    penalty: object = None
    change_offer_id: str = ""
    candidates: int = 0
    matched: int = 0

    # Execution is a separate axis from the decision. A skip and a blocked
    # exchange are not the same event and must not render the same way.
    execution: Execution = Execution.NOT_APPLICABLE
    execution_detail: str = ""

    # Fee breakdown, populated only on a reshop decision so the split is
    # verifiable without recomputing it.
    recovered: object = None
    service_fee: object = None
    net_to_customer: object = None

    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def should_reshop(self):
        return self.outcome == Outcome.RESHOP

    @property
    def executed(self):
        return self.execution is Execution.EXECUTED

    @property
    def blocked(self):
        return self.execution in (Execution.BLOCKED_SIMULATED, Execution.FAILED)

    @property
    def saving(self):
        """Positive number when money comes back. None when we never priced a change."""
        if self.change_total is None:
            return None
        return -self.change_total

    def to_json(self):
        out = {}
        for k, v in asdict(self).items():
            if isinstance(v, Decimal):
                out[k] = str(v)
            elif isinstance(v, Enum):
                out[k] = v.value
            else:
                out[k] = v
        return out


# The app installs a durable sink here (see db.audit_append). Left as None the
# module keeps its original file behaviour, which is what the tests exercise —
# they pass an explicit `path=`, and an explicit path always wins.
SINK = None


def _append(path, payload):
    if SINK is not None and path == DECISION_LOG:
        SINK(payload)
        return
    with open(path, "a") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def log_decision(decision, path=DECISION_LOG):
    """
    One JSON object per line. Append-only, so simulated and live runs can be
    diffed. Lines carry kind="decision"; execution outcomes are logged
    separately by log_execution so a blocked exchange is visibly its own event
    rather than being folded into the decision that preceded it.
    """
    _append(path, {**decision.to_json(), "kind": "decision"})
    return decision


def log_eligibility(order_id, assessment, path=DECISION_LOG):
    """
    Record an eligibility verdict at booking time.

    Unknown and mismatched conditions are logged rather than silently defaulted
    to monitored — that silent default is what put a GBP-penalty SWISS fare on
    the watch list.
    """
    _append(path, {
        "kind": "eligibility", "ts": datetime.now(timezone.utc).isoformat(),
        "order_id": order_id, "state": assessment.state.value,
        "reason": assessment.reason.value, "detail": assessment.detail,
        "penalty": str(assessment.penalty) if assessment.penalty is not None else None,
        "penalty_currency": assessment.penalty_currency or None,
        "penalty_ratio": (f"{assessment.penalty_ratio:.4f}"
                          if assessment.penalty_ratio is not None else None),
        "needs_attention": assessment.needs_attention,
        "threshold": str(eligibility.MAX_PENALTY_RATIO),
    })
    return assessment


def log_execution(decision, path=DECISION_LOG):
    """The execution outcome for a decision, as its own line."""
    _append(path, {
        "kind": "execution", "ts": datetime.now(timezone.utc).isoformat(),
        "order_id": decision.order_id, "source": decision.source,
        "decision_outcome": decision.outcome.value,
        "execution": decision.execution.value,
        "detail": decision.execution_detail or EXECUTION_COPY[decision.execution],
        "change_offer_id": decision.change_offer_id,
        "change_total": str(decision.change_total) if decision.change_total is not None else None,
        "recovered": str(decision.recovered) if decision.recovered is not None else None,
        "service_fee": str(decision.service_fee) if decision.service_fee is not None else None,
        "net_to_customer": str(decision.net_to_customer) if decision.net_to_customer is not None else None,
        "currency": decision.currency,
    })
    return decision


def evaluate(order, source, policy=None, now=None, log=True):
    """
    Decide whether to reshop `order` using `source`.

    `order` is an OrderSnapshot. `source` is any PriceSource — the engine never
    inspects which implementation it received.
    """
    policy = policy or ReshopPolicy()
    now = now or datetime.now(timezone.utc)

    def decide(outcome, reason, detail, **extra):
        d = Decision(
            order_id=order.id, source=source.name, outcome=outcome, reason=reason,
            detail=detail, paid=order.total, currency=order.currency,
            floor=policy.min_saving, route=str(order.route), itinerary=str(order.itinerary),
            **extra,
        )
        if outcome is Outcome.RESHOP:
            # Decided to exchange. Whether it actually happens is a separate
            # question — and a simulated price may never reach Duffel.
            d.execution = (Execution.BLOCKED_SIMULATED if source.name == "simulated"
                           else Execution.AWAITING_CONFIRMATION)
            d.execution_detail = EXECUTION_COPY[d.execution]
        if log:
            log_decision(d)
            if outcome is Outcome.RESHOP:
                log_execution(d)
        return d

    # --- eligibility: is this fare capable of winning at all? ------------
    # Assessed from fare conditions at booking time. Ineligible orders are never
    # polled, so we neither spend search calls nor tell the customer we are
    # working on something that cannot pay off.
    assessment = order.eligibility
    if assessment is not None and not assessment.should_poll:
        return decide(Outcome.SKIP,
                      _ELIGIBILITY_TO_REASON.get(assessment.reason, Reason.ELIGIBILITY_UNKNOWN),
                      assessment.detail)

    if not order.changeable:
        return decide(Outcome.SKIP, Reason.FARE_NOT_CHANGEABLE,
                      "fare conditions do not permit a change before departure")

    if policy.respect_void_window and order.void_window_ends_at and now < order.void_window_ends_at:
        return decide(Outcome.SKIP, Reason.IN_VOID_WINDOW,
                      f"inside void window until {order.void_window_ends_at.isoformat()} — "
                      "void and rebook is free and strictly better than paying to change")

    if order.departing_at:
        hours_out = (order.departing_at - now).total_seconds() / 3600
        if hours_out < policy.departure_buffer_hours:
            return decide(Outcome.SKIP, Reason.TOO_CLOSE_TO_DEPARTURE,
                          f"departs in {hours_out:.1f}h, buffer is {policy.departure_buffer_hours}h")

    # --- market re-search: DIAGNOSTIC ONLY, never a branch condition -----
    market_best = market_delta = None
    try:
        market = source.get_current_price(order.route, order.departure_date, order.cabin)
        identical = [m for m in market if order.itinerary.matches(m.itinerary)]
        pool = identical or market
        if pool:
            market_best = min(m.total for m in pool)
            market_delta = market_best - order.total
    except Exception as exc:  # never let diagnostics kill a decision
        market_delta = None
        market_best = None
        print(f"  market lookup failed for {order.id}: {type(exc).__name__}: {exc}")

    # --- the number that actually decides --------------------------------
    new_slice = SliceSpec(order.route.origin, order.route.destination,
                          order.departure_date, order.cabin)
    change_offers = source.price_change(order.id, order.slice_id, new_slice)

    common = dict(market_best=market_best, market_delta=market_delta,
                  candidates=len(change_offers))

    if not change_offers:
        return decide(Outcome.SKIP, Reason.NO_CHANGE_OFFERS,
                      "price source returned no change offers", **common)

    # Same carrier AND same flight numbers. Same route is not the same flight.
    matched = [o for o in change_offers if order.itinerary.matches(o.itinerary)]
    common["matched"] = len(matched)

    if not matched:
        return decide(Outcome.SKIP, Reason.NO_IDENTICAL_ITINERARY,
                      f"no change offer matches booked itinerary {order.itinerary} "
                      f"({len(change_offers)} offers on other flights)", **common)

    best = min(matched, key=lambda o: o.change_total)
    common.update(change_total=best.change_total, new_total=best.new_total,
                  penalty=best.penalty, change_offer_id=best.id)

    # The market-vs-exchange distinction is the one people get wrong. Keep it.
    note = ""
    if market_delta is not None and market_delta < 0:
        note = (f" Note the market looks {abs(market_delta)} {order.currency} cheaper, "
                "but that is not the exchange price and must not be acted on.")

    if best.change_total >= 0:
        return decide(Outcome.SKIP, Reason.CHANGE_TOTAL_NOT_NEGATIVE,
                      f"Airline quoted {best.change_total} {best.currency} to exchange — "
                      f"a charge, not a refund. Checked and declined; booking left as it is."
                      f"{note}", **common)

    saving = -best.change_total
    if saving < policy.min_saving:
        return decide(Outcome.SKIP, Reason.BELOW_FLOOR,
                      f"Airline quoted a {saving} {best.currency} refund to exchange — "
                      f"below the {policy.min_saving} {best.currency} minimum, so it was "
                      f"not worth doing. Checked and declined.{note}", **common)

    recovered, fee, net = fee_split(saving)
    return decide(Outcome.RESHOP, Reason.PROFITABLE_DROP,
                  f"Airline quoted a {recovered} {best.currency} refund to exchange onto "
                  f"{best.itinerary} — clears the {policy.min_saving} {best.currency} minimum. "
                  f"Recovered {recovered}, our fee {fee}, {net} net to customer.",
                  recovered=recovered, service_fee=fee, net_to_customer=net, **common)
