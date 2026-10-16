"""
Referral codes at signup. Every new account needs a real one (migration 038):
there is no public code, and no code means no account — by email and password
(app.signup) or by Google (app.signup_google, then the callback).

  check(code)           whether a signup may use this code: it exists, it is not
                        internal (LEGACY, SCOUT-APPLICANT), it is active, and its
                        scout is approved
  resolve(...)          the code a signup is attributed to, after the link lock,
                        the throttle and check() — or why not
  self_referral(...)    whether the scout is referring themselves

The link lock. /r/<code> sets td_ref, a signed cookie holding the code. While it
is present the signup is attributed to that code and nothing else: a form value
that differs is refused, not quietly swapped, and a cookie whose signature does
not verify is refused too. The `readonly` field on the form is a courtesy; this
is the enforcement.
"""

from datetime import timedelta

from flask import current_app
from itsdangerous import BadSignature, URLSafeTimedSerializer

import db

COOKIE = "td_ref"
COOKIE_MAX_AGE = timedelta(days=90)

# Wrong codes per address before it has to wait — the same shape as the login
# throttle (auth.throttled), counted in Postgres for the same reason.
MAX_FAILURES = 10
FAILURE_WINDOW_MIN = 15

MISSING = "A referral code is required to create an account."
INVALID = "That referral code isn't valid."
INACTIVE = "This referral link is no longer active."
LOCKED = "This signup is locked to the referral link you arrived with ({code})."
UNVERIFIED = ("Your referral link couldn't be verified. Open the link again, or clear this "
              "site's cookies and enter the code yourself.")
THROTTLED = "Too many referral codes tried. Wait a few minutes and try again."

# A shared domain here says nothing about who someone works for, so a signup
# sharing one with its scout is let through and flagged for review instead of
# refused.
FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com",
    "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me",
    "protonmail.com", "pm.me", "gmx.com", "gmx.net", "mail.com", "zoho.com", "fastmail.com",
    "hey.com", "yandex.com", "tutanota.com",
})


def normalize(code):
    return (code or "").strip().upper()


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="td-ref")


def sign(code):
    return _serializer().dumps(normalize(code))


def unsign(value):
    """The code in a td_ref cookie, or None when its signature does not verify
    or it is older than COOKIE_MAX_AGE."""
    try:
        return _serializer().loads(value, max_age=int(COOKIE_MAX_AGE.total_seconds()))
    except BadSignature:  # SignatureExpired included
        return None


def check(code):
    """(row, None) when a signup may use the code, else (None, message).
    Unknown and internal codes get the same answer, so the form says nothing
    about which internal codes exist."""
    code = normalize(code)
    if not code:
        return None, MISSING
    row = db.referral_code_lookup(code)
    if not row or row["internal"]:
        return None, INVALID
    if not row["active"] or row["scout_status"] != "approved":
        return None, INACTIVE
    return row, None


def locked_code(cookie_value):
    """(code, verified): the code a td_ref cookie locks signup to. (None, True)
    when there is no cookie, (None, False) when there is one that does not
    verify."""
    if not cookie_value:
        return None, True
    code = unsign(cookie_value)
    return (code, True) if code else (None, False)


def resolve(*, cookie_value, form_code, ip):
    """The referral a signup is attributed to: (row, None), or (None, (message,
    status)). In order: the link lock, the throttle, then check(). Every wrong
    code counts against the address."""
    locked, verified = locked_code(cookie_value)
    if not verified:
        return None, (UNVERIFIED, 400)
    submitted = normalize(form_code)
    if locked and submitted and submitted != normalize(locked):
        return None, (LOCKED.format(code=locked), 400)
    code = normalize(locked) or submitted
    if not code:
        return None, (MISSING, 400)
    if db.referral_recent_failures(ip, FAILURE_WINDOW_MIN) >= MAX_FAILURES:
        return None, (THROTTLED, 429)
    row, problem = check(code)
    if problem:
        db.referral_failure_record(ip, code)
        return None, (problem, 400)
    return row, None


def self_referral(email, referral):
    """('block', message) when the signup is the scout's own: the same email,
    or the same company domain. ('flag', reason) when it shares only a
    free-mail domain with the scout — let through, and listed for an admin
    (accounts.referral_flag). (None, None) otherwise."""
    email = (email or "").strip().lower()
    scout_email = (referral.get("scout_email") or "").strip().lower()
    if not email or not scout_email:
        return None, None
    if email == scout_email:
        return "block", "You can't sign up with your own referral code."
    domain, scout_domain = email.rpartition("@")[2], scout_email.rpartition("@")[2]
    if domain and domain == scout_domain:
        if domain in FREE_MAIL:
            return "flag", f"Signup email shares the scout's free-mail domain ({domain})."
        return "block", "This referral code can't be used by someone at the scout's own company."
    return None, None


def referrer_name(referral):
    """How the signup form names the scout: their first name, or a fallback."""
    return (referral.get("scout_given_name") or "").strip() or "a Trip Difference scout"
