"""
Shared Duffel HTTP layer.

duffel.py (the CLI) keeps its own inlined copy on purpose — it prints and exits.
This one raises, so it can be used from the engine and the web app.

429 handling follows what Duffel actually documents: `ratelimit-reset` is an
RFC 2616 *date string*, not a seconds delta. `Retry-After` is checked first only
because proxies sometimes inject it; Duffel itself does not send it.
"""

import json
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

import paths

BASE = "https://api.duffel.com"
RESPONSES = paths.DATA_DIR / "responses"


class DuffelError(RuntimeError):
    """A non-2xx from Duffel, with the parsed error list attached."""

    def __init__(self, status, errors):
        self.status = status
        self.errors = errors or []
        parts = [
            f"[{e.get('type')}/{e.get('code')}] {e.get('title')}: {e.get('message')}"
            for e in self.errors
        ]
        super().__init__(f"HTTP {status} — " + ("; ".join(parts) or "no error body"))

    @property
    def codes(self):
        return [e.get("code") for e in self.errors]


def token():
    """Load and sanity-check the token. Test mode only, always."""
    load_dotenv()
    tok = os.environ.get("DUFFEL_TOKEN", "").strip()
    if not tok:
        raise RuntimeError("DUFFEL_TOKEN not set — copy .env.example to .env")
    if tok.startswith("duffel_live_"):
        raise RuntimeError("Refusing to run: DUFFEL_TOKEN is a LIVE token. Test mode only.")
    if not tok.startswith("duffel_test_"):
        raise RuntimeError(f"Refusing to run: token does not start with 'duffel_test_' (got '{tok[:14]}...')")
    return tok


def _rate_limit_wait(resp):
    if resp.headers.get("Retry-After"):
        try:
            return max(1.0, float(resp.headers["Retry-After"]))
        except ValueError:
            pass
    reset = resp.headers.get("ratelimit-reset")
    if reset:
        try:
            delta = (parsedate_to_datetime(reset) - datetime.now(timezone.utc)).total_seconds()
            return max(1.0, min(delta, 120.0))
        except (TypeError, ValueError):
            pass
    return 5.0


def dump(label, payload):
    if not paths.DUMP_RESPONSES:
        return
    RESPONSES.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    (RESPONSES / f"{stamp}-{label}.json").write_text(json.dumps(payload, indent=2, sort_keys=True))


def request(method, path, *, body=None, params=None, label=None):
    """One request. Retries a 429 once, then gives up. Raises DuffelError on non-2xx."""
    headers = {
        "Authorization": f"Bearer {token()}",
        "Duffel-Version": "v2",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    for attempt in (1, 2):
        resp = requests.request(method, f"{BASE}{path}", headers=headers,
                                json=body, params=params, timeout=60)
        if resp.status_code == 429 and attempt == 1:
            time.sleep(_rate_limit_wait(resp))
            continue

        try:
            payload = resp.json()
        except ValueError:
            raise DuffelError(resp.status_code, [{"title": "non-JSON body", "message": resp.text[:500]}])

        dump(label or path.strip("/").replace("/", "_"), payload)

        if not resp.ok:
            raise DuffelError(resp.status_code, payload.get("errors"))
        return payload["data"]

    raise DuffelError(429, [{"title": "rate limited", "message": "429 twice in a row"}])
