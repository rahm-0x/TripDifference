"""
Session auth. Passwords are argon2id; the cookie holds an opaque token whose
SHA-256 is what the sessions table stores, so a database leak yields no usable
sessions.

Signup is open — anyone can create an account. The first user of a signup owns
a fresh `accounts` row, and every order is scoped to that account.
"""

import functools
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from flask import g, redirect, request, session, url_for

import db

_ph = PasswordHasher()
COOKIE_KEY = "sid"

MIN_PASSWORD = 10


def hash_password(raw):
    return _ph.hash(raw)


def check_password(stored_hash, raw):
    if not stored_hash:
        return False
    try:
        return _ph.verify(stored_hash, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problem(raw, confirm=None):
    """None when acceptable, else the message to show. Length beats character
    classes: a long passphrase is stronger than 'P@ss1'."""
    if confirm is not None and raw != confirm:
        return "Those passwords don't match."
    if len(raw or "") < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    return None


def sign_in(user_id):
    session.clear()
    session[COOKIE_KEY] = db.start_session(user_id)
    session.permanent = True


def sign_out():
    db.end_session(session.get(COOKIE_KEY))
    session.clear()


def current_user():
    """Resolved once per request and cached on `g`."""
    if "user" not in g:
        g.user = db.session_user(session.get(COOKIE_KEY))
    return g.user


def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if current_user() is None:
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return view(*a, **kw)
    return wrapped


def view_model(user):
    """The `user` dict the templates already expect."""
    if user is None:
        return None
    given, family = user["given_name"], user["family_name"]
    name = " ".join(p for p in (given, family) if p) or user["email"]
    initials = "".join(p[0] for p in (given, family) if p).upper() or user["email"][0].upper()
    return {"name": name, "email": user["email"], "company": user["company"],
            "initials": initials}


def profile_of(user):
    """The passenger prefill the booking flow reads."""
    if user is None:
        return {}
    return {"title": user["title"], "given_name": user["given_name"],
            "family_name": user["family_name"],
            "born_on": user["born_on"].isoformat() if user["born_on"] else "",
            "gender": user["gender"], "email": user["email"],
            "phone_number": user["phone_number"]}


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
#
# Enforced centrally in app.py by a before_request hook, not by remembering to
# add a hidden field to each form. The route that matters is the one that
# confirms an exchange: its "type CONFIRM" step lives in the request body, so
# an attacker's page can supply it just as easily as the operator can.

CSRF_FIELD = "_csrf"


def csrf_token():
    """Per-session token, minted on first use."""
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def csrf_ok():
    sent = request.form.get(CSRF_FIELD) or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("_csrf") or ""
    # compare_digest, not ==, so a mismatch cannot be timed character by character.
    return bool(expected) and secrets.compare_digest(sent, expected)


# ---------------------------------------------------------------------------
# login throttling
# ---------------------------------------------------------------------------

MAX_FAILURES = 8          # per identity per window
FAILURE_WINDOW_MIN = 15


def throttled(email, ip):
    """True when this email or IP has failed too often too recently.

    Counted in Postgres rather than in memory: serverless instances do not
    share state, so an in-process counter would reset on every cold start and
    protect nothing.
    """
    return db.recent_failures(email, ip, FAILURE_WINDOW_MIN) >= MAX_FAILURES
