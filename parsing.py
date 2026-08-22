"""
Best-effort confirmation-email parser for the Email Capture Layer.

Airline/OTA confirmation emails have no common format — this extracts what
it confidently can via heuristics and leaves the rest blank rather than
guessing. A parsed reservation lands exactly like a manual one
(db.create_manual_order): the customer can fill any gaps in from the
Reservations page. Coverage will improve as real forwarded confirmations
are seen; this is not an exhaustive per-airline parser.
"""

import re
from decimal import Decimal, InvalidOperation

# Substring match against the email body/subject — commercial name, not
# IATA code, since that's what confirmation emails actually print.
_KNOWN_CARRIERS = (
    "Alaska Airlines", "Delta Air Lines", "Delta", "United Airlines", "United",
    "American Airlines", "Southwest Airlines", "Southwest", "JetBlue",
    "British Airways", "Air Canada", "Lufthansa", "Air France", "KLM",
    "Emirates", "Qatar Airways", "Turkish Airlines", "Iberia", "Duffel Airways",
)

_PNR_RE = re.compile(r"\b(?:confirmation|reference|record locator|booking)"
                     r"[^\w]{0,15}([A-Z0-9]{5,8})\b", re.I)
_PNR_FALLBACK_RE = re.compile(r"\b[A-Z0-9]{6}\b")
_AIRPORT_PAIR_RE = re.compile(r"\b([A-Z]{3})\s*(?:→|->|-|to)\s*([A-Z]{3})\b")
_FLIGHT_NUM_RE = re.compile(r"\b([A-Z]{2})\s?(\d{2,4})\b")
_PRICE_RE = re.compile(r"(?:USD|US\$|\$)\s?([\d,]+\.\d{2})")
_DATE_RES = (
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),                       # 2026-08-29
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),                   # 08/29/2026
    re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
              r"[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", re.I),          # Aug 29, 2026
)
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _find_carrier(text):
    for name in _KNOWN_CARRIERS:
        if name.lower() in text.lower():
            return name
    return ""


def _find_booking_reference(text):
    m = _PNR_RE.search(text)
    if m:
        return m.group(1).upper()
    # Fallback: the first isolated 6-char alphanumeric run that isn't a
    # common false positive (a year, "ECONOMY", etc.) — genuinely best-effort.
    for m in _PNR_FALLBACK_RE.finditer(text):
        candidate = m.group(0)
        if not candidate.isdigit() and not candidate.isalpha():
            return candidate.upper()
    return ""


def _find_airports(text):
    m = _AIRPORT_PAIR_RE.search(text)
    return (m.group(1).upper(), m.group(2).upper()) if m else ("", "")


def _find_flight_number(text, carrier_hint):
    m = _FLIGHT_NUM_RE.search(text)
    return f"{m.group(1)}{m.group(2)}" if m else ""


def _find_price(text):
    m = _PRICE_RE.search(text)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def _find_date(text):
    for pattern in _DATE_RES:
        m = pattern.search(text)
        if not m:
            continue
        g = m.groups()
        try:
            if g[0].isalpha():
                month = _MONTHS.get(g[0][:3].lower())
                if not month:
                    continue
                return f"{g[2]}-{month:02d}-{int(g[1]):02d}"
            if len(g[0]) == 4:  # YYYY-MM-DD
                return f"{g[0]}-{g[1]}-{g[2]}"
            return f"{g[2]}-{int(g[0]):02d}-{int(g[1]):02d}"  # MM/DD/YYYY
        except (ValueError, IndexError):
            continue
    return ""


def parse_confirmation(subject, body):
    """Returns the same field shape app.reservation_new()'s form does —
    blank string/None for anything not confidently found."""
    text = f"{subject}\n{body}"
    origin, destination = _find_airports(text)
    carrier = _find_carrier(text)
    return {
        "booking_reference": _find_booking_reference(text),
        "carrier": carrier,
        "seg_origin": origin,
        "seg_destination": destination,
        "seg_flight_number": _find_flight_number(text, carrier),
        "departure_date": _find_date(text),
        "paid": _find_price(text),
    }
