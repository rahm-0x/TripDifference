"""
The one way this app sends a Duffel offer request, test or live.

With a test token this is a plain duffel_http.request. With a live token
(staging, DUFFEL_LIVE_SEARCH_ENABLED=true) every offer request is first
counted: under an advisory lock, this month's (UTC) rows in live_search_log
are counted, and if one more would pass STAGING_MAX_MONTHLY_SEARCHES the
search is refused with SearchBudgetExceeded and nothing is sent. Otherwise a
row (timestamp, route, date, cabin, passengers, source) is written first, and
the request goes out under a single-use duffel_http.SearchAuthorization —
without one, duffel_http refuses a live offer request outright. What came
back (offer request id, offer count, or the error) is filled in afterwards.

Single-offer fetches (GET /air/offers/:id) are not offer requests and are not
budgeted; offer_eligibility.py records its refetches on the same log row.
"""

import config
import db
import duffel_http


class SearchBudgetExceeded(RuntimeError):
    pass


def budget():
    """{'used': this month's live searches, 'limit': STAGING_MAX_MONTHLY_SEARCHES}."""
    return {"used": db.live_searches_this_month(), "limit": config.STAGING_MAX_MONTHLY_SEARCHES}


def send(body, *, source, account_id=None, params=None, label=None):
    """(offer request data, live_search_log id). The id is None in test mode."""
    if duffel_http.mode() != "live":
        return duffel_http.request("POST", "/air/offer_requests", body=body, params=params, label=label), None

    data = (body or {}).get("data") or {}
    first = (data.get("slices") or [{}])[0]
    limit = config.STAGING_MAX_MONTHLY_SEARCHES
    log_id, used = db.live_search_reserve(
        limit=limit, source=source, account_id=account_id,
        origin=first.get("origin"), destination=first.get("destination"),
        departure_date=first.get("departure_date"), cabin=data.get("cabin_class"),
        passengers=len(data.get("passengers") or []) or None)
    if log_id is None:
        raise SearchBudgetExceeded(f"Live search budget reached: {used} / {limit} searches this month "
                                   "(STAGING_MAX_MONTHLY_SEARCHES). Nothing was sent to Duffel.")
    try:
        with duffel_http.search_authorized(duffel_http.SearchAuthorization(log_id)):
            result = duffel_http.request("POST", "/air/offer_requests", body=body, params=params, label=label)
    except Exception as exc:
        db.live_search_record(log_id, error=str(exc)[:500])
        raise
    db.live_search_record(log_id, offer_request_id=result.get("id"),
                          offers_returned=len(result.get("offers") or []))
    return result, log_id


def offer_request(body, *, source, account_id=None, params=None, label=None):
    """The offer request data alone, for callers with no use for the log id."""
    return send(body, source=source, account_id=account_id, params=params, label=label)[0]
