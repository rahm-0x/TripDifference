#!/usr/bin/env python3
"""
Dumb local UI for the Duffel spike.

stdlib http.server only — no Flask, no framework. Its whole job is to serve
index.html and proxy one search endpoint, so the DUFFEL_TOKEN stays server-side
and never reaches the browser.

    .venv/bin/python server.py
    open http://localhost:8000
"""

import json
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from duffel import BASE, token

HERE = Path(__file__).parent
PORT = 8000

# Validate the token once at boot rather than per request — token() exits the
# process on a bad token, which is what we want at startup and not mid-request.
TOKEN = token()

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Duffel-Version": "v2",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def search_offers(origin, destination, date, cabin):
    """One offer request, flattened into what the page needs."""
    body = {
        "data": {
            "slices": [{
                "origin": origin.upper(),
                "destination": destination.upper(),
                "departure_date": date,
            }],
            "passengers": [{"type": "adult"}],
            "cabin_class": cabin,
        }
    }
    resp = requests.post(f"{BASE}/air/offer_requests", headers=HEADERS,
                         params={"return_offers": "true"}, json=body, timeout=60)
    payload = resp.json()

    if not resp.ok:
        msgs = [e.get("message") or e.get("title") for e in payload.get("errors", [])]
        return {"error": "; ".join(m for m in msgs if m) or f"HTTP {resp.status_code}"}

    offers = payload["data"].get("offers", [])
    offers.sort(key=lambda o: Decimal(o["total_amount"]))

    out = []
    for o in offers[:20]:
        segments = []
        for sl in o.get("slices", []):
            for seg in sl.get("segments", []):
                carrier = seg.get("marketing_carrier") or {}
                segments.append({
                    "flight": f"{carrier.get('iata_code', '??')}{seg.get('marketing_carrier_flight_number', '')}",
                    "origin": (seg.get("origin") or {}).get("iata_code"),
                    "destination": (seg.get("destination") or {}).get("iata_code"),
                    "departing_at": seg.get("departing_at"),
                    "arriving_at": seg.get("arriving_at"),
                })
        out.append({
            "id": o["id"],
            "carrier": (o.get("owner") or {}).get("name"),
            "amount": o["total_amount"],          # string — the page must not do float maths
            "currency": o["total_currency"],
            "stops": max(len(segments) - 1, 0),
            "segments": segments,
        })

    return {"offers": out, "count": len(offers)}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)

        if url.path in ("/", "/index.html"):
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")

        if url.path == "/logo.png":
            logo = HERE / "logo.png"
            if logo.exists():
                return self._send(200, logo.read_bytes(), "image/png")
            return self._send(404, b"no logo.png", "text/plain")

        if url.path == "/api/search":
            q = parse_qs(url.query)
            origin = (q.get("origin") or [""])[0].strip()
            destination = (q.get("destination") or [""])[0].strip()
            date = (q.get("date") or [""])[0].strip()
            cabin = (q.get("cabin") or ["economy"])[0].strip()

            if not (origin and destination and date):
                return self._send(400, json.dumps({"error": "origin, destination and date are required"}).encode(),
                                  "application/json")
            try:
                result = search_offers(origin, destination, date, cabin)
            except Exception as exc:  # spike: surface it, don't swallow it
                result = {"error": f"{type(exc).__name__}: {exc}"}
            code = 400 if "error" in result else 200
            return self._send(code, json.dumps(result).encode(), "application/json")

        self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args):
        print(f"  {fmt % args}")


if __name__ == "__main__":
    print(f"Trip Difference UI  →  http://localhost:{PORT}")
    print("token loaded, test mode. ctrl-c to stop.\n")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
