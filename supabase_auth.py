"""
Google SSO, brokered through Supabase Auth.

Supabase Auth (GoTrue) handles the actual Google OAuth roundtrip; we only
use it to get back a verified Google identity (id + email). It is NOT
adopted as this app's session system — auth.py's argon2id/opaque-session
machinery stays the source of truth for every @auth.login_required route.
Raw requests, no supabase-py, no build step — same style as duffel_http.py.

PKCE, not the implicit flow: this is a server backend, not a SPA, so the
code exchange happens here rather than a token landing in a URL fragment
a browser-only flow would need JS to read.

The exact `/auth/v1/token?grant_type=pkce` request/response shape here is
written against GoTrue's documented API, not verified against a live
exchange — there's no way to do that until the Google provider is actually
enabled in the Supabase dashboard (a step only the account owner can do).
Confirm the field names on the first real callback.
"""

import base64
import hashlib
import os
import secrets
from urllib.parse import urlencode

import requests


def _base_url():
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("SUPABASE_URL not set")
    return url


def _anon_key():
    key = (os.environ.get("SUPABASE_ANON_KEY")
          or os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY", ""))
    if not key:
        raise RuntimeError("SUPABASE_ANON_KEY not set")
    return key


def new_pkce_pair():
    """(code_verifier, code_challenge) — verifier goes in the session until
    the callback, challenge goes in the authorize URL. S256 per RFC 7636."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def start_url(redirect_to, code_challenge):
    params = {
        "provider": "google", "redirect_to": redirect_to,
        "code_challenge": code_challenge, "code_challenge_method": "s256",
    }
    return f"{_base_url()}/auth/v1/authorize?{urlencode(params)}"


def exchange_code(code, verifier):
    """Returns the Supabase user object: {id, email, user_metadata, ...}."""
    resp = requests.post(
        f"{_base_url()}/auth/v1/token?grant_type=pkce",
        headers={"apikey": _anon_key(), "Content-Type": "application/json"},
        json={"auth_code": code, "code_verifier": verifier}, timeout=15)
    resp.raise_for_status()
    return resp.json()["user"]
