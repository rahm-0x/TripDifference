"""
Travel policy evaluation. One function, two callers: advisory at /search
(offer_view flags a non-compliant offer before anyone picks it — the data is
computed and attached to each offer; nothing renders it yet, that's Phase 4)
and a hard gate in book(), in the same position passenger_problems() already
occupies, before anything is created anywhere.

policy_rules.enforcement carries three levels:
    advise           — proceed, record what fired
    require_approval — book() must not call Duffel; a booking_requests row
                       captures the ask instead
    block             — refused outright

A single attempt can trigger more than one rule. The overall decision is the
strictest enforcement among everything that fired: block > require_approval
> advise > none — never the other way around, since a non-compliant offer
must not slip through because a lesser rule also happened to match.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import db

_STRICTNESS = {"advise": 1, "require_approval": 2, "block": 3}
_CABIN_RANK = {"economy": 0, "premium_economy": 1, "business": 2, "first": 3}


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    rule_type: str
    enforcement: str
    detail: str


@dataclass
class PolicyDecision:
    results: list = field(default_factory=list)

    @property
    def enforcement(self):
        """The strictest enforcement level among everything that fired, or
        None if nothing did."""
        if not self.results:
            return None
        return max((r.enforcement for r in self.results), key=lambda e: _STRICTNESS[e])

    @property
    def blocked(self):
        return self.enforcement == "block"

    @property
    def requires_approval(self):
        return self.enforcement == "require_approval"

    def to_json(self):
        return [{"rule_id": r.rule_id, "rule_type": r.rule_type,
                 "enforcement": r.enforcement, "detail": r.detail} for r in self.results]


def extract_itinerary_facts(offer):
    """Raw Duffel offer (or order) payload -> the primitives rules check
    against. First slice/segment only, matching this codebase's existing
    round-trip limitation (see README: OrderSnapshot also only reads
    slices[0]) — fixing that is a separate concern from adding policy.
    """
    sl = (offer.get("slices") or [{}])[0]
    segments = sl.get("segments") or []
    first = segments[0] if segments else {}
    mc = first.get("marketing_carrier") or {}
    cabin = (first.get("passengers") or [{}])[0].get("cabin_class", "economy")
    return {
        "amount": offer.get("total_amount"),
        "currency": offer.get("total_currency"),
        "cabin": cabin,
        "origin": (sl.get("origin") or {}).get("iata_code", ""),
        "destination": (sl.get("destination") or {}).get("iata_code", ""),
        "carrier": mc.get("iata_code", ""),
        "departure_date": (first.get("departing_at") or "")[:10],
    }


def _check(rule, *, amount, currency, cabin, origin, destination, carrier, departure_date):
    """One rule against one itinerary. Returns (fired, detail)."""
    scope = rule.get("scope") or {}
    value = rule.get("value")

    if scope.get("route") and scope["route"] != f"{origin}-{destination}":
        return False, ""

    rule_type = rule["rule_type"]

    if rule_type == "price_ceiling":
        ceiling = Decimal(str(value))
        amt = Decimal(str(amount))
        if amt > ceiling:
            return True, f"price {amt} {currency} exceeds the {ceiling} {currency} ceiling"
        return False, ""

    if rule_type == "cabin_cap":
        cap = str(value)
        if _CABIN_RANK.get(cabin, 0) > _CABIN_RANK.get(cap, 0):
            return True, f"cabin '{cabin}' exceeds the '{cap}' cap"
        return False, ""

    if rule_type == "advance_purchase":
        min_days = int(value)
        if not departure_date:
            return False, ""
        try:
            dep = datetime.strptime(departure_date, "%Y-%m-%d").date()
        except ValueError:
            return False, ""
        days_out = (dep - datetime.now(timezone.utc).date()).days
        if days_out < min_days:
            return True, f"booked {days_out} day(s) out, policy requires {min_days}+"
        return False, ""

    if rule_type == "carrier_restriction":
        restricted = value if isinstance(value, list) else [value]
        if carrier in restricted:
            return True, f"carrier '{carrier}' is restricted"
        return False, ""

    if rule_type == "ancillary_protection":
        # Protects seats/bags from being lost to an *exchange*, not the
        # initial purchase — there's nothing to check at book() time. The
        # exchange/reshop execution path this would actually gate
        # (engine.py/execute()) is not part of this phase; the rule type
        # exists in the schema now so it's cheaper to wire in later than to
        # alter the CHECK constraint again.
        return False, ""

    return False, ""


def evaluate_with_rules(rules, offer):
    """Evaluate one offer against an already-fetched rule list — lets
    /search fetch active rules once and check twenty offers against them,
    rather than once per offer."""
    facts = extract_itinerary_facts(offer)
    decision = PolicyDecision()
    for rule in rules:
        fired, detail = _check(rule, **facts)
        if fired:
            decision.results.append(RuleResult(
                rule_id=str(rule["id"]), rule_type=rule["rule_type"],
                enforcement=rule["enforcement"], detail=detail))
    return decision


def evaluate(account_id, offer):
    """Single-offer convenience wrapper — what book()'s hard gate calls,
    where there's only ever one offer to check."""
    return evaluate_with_rules(db.policy_rules_active(account_id), offer)
