"""
Offer-time eligibility for a search: the /eligibility page and
scripts/eligibility_scan.py.

Every verdict comes from eligibility.assess() — this module never reads change
conditions to decide anything. It only:
  - sends the search through live_search (so a live one is budgeted and logged),
  - fetches an offer again, once, when its change conditions came back null,
    and records whether they filled in (conditions_source, and the counts on
    the live_search_log row),
  - maps assess()'s result onto four display verdicts, and
  - pulls the raw conditions out for display.

Verdicts (verdict_for):
  Eligible               assess() would monitor it (should_poll) and its change penalty is 0
  Eligible with penalty  assess() would monitor it and its change penalty is above 0
  Unknown                assess() couldn't tell from what Duffel published:
                         CONDITIONS_MISSING, PENALTY_UNKNOWN, PENALTY_CURRENCY_MISMATCH
  Not eligible           anything else — not changeable, a change fee too high for a drop
                         to pay (UNLIKELY_TO_SAVE), or a carrier that has never honoured a
                         change on a real order

conditions_source:
  offer          change conditions were on the offer in the search response
  refetched      null in the search response, present after GET /air/offers/:id
  still_null     null in the search response and still null after that fetch
  refetch_failed null, and the single-offer fetch errored
  not_refetched  null, and not fetched again: past REFETCH_LIMIT for this search

assess() is called with has_card=True: this is about the fare, not the viewer's
card on file.
"""

from decimal import Decimal, InvalidOperation

import db
import duffel_http
import eligibility
import live_search
from eligibility import EligibilityReason

ELIGIBLE = "Eligible"
ELIGIBLE_WITH_PENALTY = "Eligible with penalty"
NOT_ELIGIBLE = "Not eligible"
UNKNOWN = "Unknown"
VERDICTS = (ELIGIBLE, ELIGIBLE_WITH_PENALTY, UNKNOWN, NOT_ELIGIBLE)  # display and sort order

_UNKNOWN_REASONS = {
    EligibilityReason.CONDITIONS_MISSING,
    EligibilityReason.PENALTY_UNKNOWN,
    EligibilityReason.PENALTY_CURRENCY_MISMATCH,
}

# Single-offer fetches per search, at most. Each is its own Duffel request;
# a search can return a couple of hundred offers.
REFETCH_LIMIT = 25

CABINS = ("economy", "premium_economy", "business", "first")
MAX_PASSENGERS = 9


def verdict_for(assessment):
    if assessment.reason in _UNKNOWN_REASONS:
        return UNKNOWN
    if assessment.should_poll:
        return ELIGIBLE_WITH_PENALTY if (assessment.penalty or 0) > 0 else ELIGIBLE
    return NOT_ELIGIBLE


def _change_conditions_null(offer):
    return (offer.get("conditions") or {}).get("change_before_departure") is None


def condition_view(conditions, key):
    """{'allowed', 'penalty', 'currency'} for one of an offer's conditions, as
    published — None where Duffel published nothing."""
    c = (conditions or {}).get(key)
    if c is None:
        return {"allowed": None, "penalty": None, "currency": ""}
    return {"allowed": c.get("allowed"), "penalty": c.get("penalty_amount"),
            "currency": (c.get("penalty_currency") or "").upper()}


def _decimal(value):
    try:
        return Decimal(str(value)) if value is not None else None
    except InvalidOperation:
        return None


def _row(offer, assessment, source):
    slices = offer.get("slices") or []
    first = slices[0] if slices else {}
    owner = offer.get("owner") or {}
    change = condition_view(offer.get("conditions"), "change_before_departure")
    return {
        "offer_id": offer.get("id", ""),
        "offer": offer,
        "carrier": owner.get("name", ""),
        "carrier_iata": owner.get("iata_code", ""),
        "fare_brand": first.get("fare_brand_name") or "",
        "price": offer.get("total_amount"),
        "currency": offer.get("total_currency", ""),
        "change": change,
        "refund": condition_view(offer.get("conditions"), "refund_before_departure"),
        "change_penalty": _decimal(change["penalty"]),
        "verdict": verdict_for(assessment),
        "reason": assessment.detail,
        "eligibility_state": assessment.state.value,
        "eligibility_reason": assessment.reason.value,
        "conditions_source": source,
        "offer_conditions": offer.get("conditions"),
        "slice_conditions": [{"slice": i + 1, "origin": (s.get("origin") or {}).get("iata_code", ""),
                              "destination": (s.get("destination") or {}).get("iata_code", ""),
                              "conditions": s.get("conditions")} for i, s in enumerate(slices)],
    }


def evaluate_offers(offers, *, capability_map, fetch_offer, refetch_limit=REFETCH_LIMIT):
    """(rows sorted eligible-first then by price, stats). fetch_offer(offer_id)
    returns a single offer; None disables refetching."""
    stats = {"null_conditions": 0, "refetched": 0, "conditions_filled": 0}
    rows = []
    for offer in offers:
        source = "offer"
        if _change_conditions_null(offer):
            stats["null_conditions"] += 1
            if fetch_offer is None or stats["refetched"] >= refetch_limit:
                source = "not_refetched"
            else:
                try:
                    fresh = fetch_offer(offer["id"])
                except (duffel_http.DuffelError, RuntimeError):
                    source = "refetch_failed"
                else:
                    stats["refetched"] += 1
                    if fresh and not _change_conditions_null(fresh):
                        offer, source = fresh, "refetched"
                        stats["conditions_filled"] += 1
                    else:
                        source = "still_null"
        iata = (offer.get("owner") or {}).get("iata_code")
        assessment = eligibility.assess(offer, fare_type="cash", has_card=True,
                                        carrier_capability=capability_map.get(iata))
        rows.append(_row(offer, assessment, source))
    rows.sort(key=lambda r: (VERDICTS.index(r["verdict"]), _decimal(r["price"]) or Decimal("Infinity")))
    return rows, stats


def search(*, origin, destination, departure_date, cabin, passengers, source, account_id=None,
           refetch_limit=REFETCH_LIMIT):
    """One offer request (budgeted when live) and its evaluated offers:
    {'rows', 'stats', 'offer_request_id', 'offers_returned', 'live'}.
    Raises live_search.SearchBudgetExceeded, DuffelError, RuntimeError."""
    body = {"data": {"slices": [{"origin": origin, "destination": destination,
                                 "departure_date": departure_date}],
                     "passengers": [{"type": "adult"}] * passengers,
                     "cabin_class": cabin}}
    data, log_id = live_search.send(body, source=source, account_id=account_id,
                                    params={"return_offers": "true"}, label=f"{source}_search")
    offers = data.get("offers") or []
    capability_map = db.carrier_capabilities_for({(o.get("owner") or {}).get("iata_code") for o in offers})
    rows, stats = evaluate_offers(
        offers, capability_map=capability_map,
        fetch_offer=lambda offer_id: duffel_http.request("GET", f"/air/offers/{offer_id}",
                                                         label=f"{source}_offer"),
        refetch_limit=refetch_limit)
    if log_id is not None:
        db.live_search_record(log_id, **stats)
    return {"rows": rows, "stats": stats, "offer_request_id": data.get("id"),
            "offers_returned": len(offers), "live": log_id is not None}


def verdict_counts(rows):
    counts = {v: 0 for v in VERDICTS}
    for r in rows:
        counts[r["verdict"]] += 1
    return counts
