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

and, for the scout application's invite (migration 041), invite_check,
invite_resolve and self_invite, the same three for /join/<code>.

The link lock. /r/<code> sets td_ref, a signed cookie holding the code. While it
is present the signup is attributed to that code and nothing else: a form value
that differs is refused, not quietly swapped, and a cookie whose signature does
not verify is refused too. The `readonly` field on the form is a courtesy; this
is the enforcement.
"""

import re
from datetime import timedelta

from flask import current_app
from itsdangerous import BadSignature, URLSafeTimedSerializer

import db

COOKIE = "td_ref"
COOKIE_MAX_AGE = timedelta(days=90)

# Scout-invite codes (migration 041) and nothing else start with this.
INVITE_PREFIX = "INVITE-"

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
INVITE_AT_SIGNUP = ("Codes starting with INVITE- are for applying to be a scout, not for opening a business "
                    "account. Use the referral code a scout gave you.")

# A shared domain here says nothing about who someone works for, so a signup
# sharing one with its scout is let through and flagged for review instead of
# refused.
FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com",
    "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me",
    "protonmail.com", "pm.me", "gmx.com", "gmx.net", "mail.com", "zoho.com", "fastmail.com",
    "hey.com", "yandex.com", "tutanota.com",
})


# A scout's own link, chosen in Settings (db.scout_choose_code). Letters,
# numbers and single hyphens, starting and ending with a letter or number. At
# least 4 characters: anyone who types a scout's code can sign up as their
# referral, so a code should not be a guess like "sam".
CUSTOM_MIN, CUSTOM_MAX = 4, 32
CUSTOM_LIMIT = 5            # custom codes per scout; every one keeps working
_CUSTOM_SHAPE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_RESERVED = frozenset({
    "LEGACY", "SCOUT-APPLICANT", "ADMIN", "STAFF", "SCOUT", "SCOUTS", "SIGNUP", "LOGIN", "LOGOUT",
    "SUPPORT", "HELP", "BILLING", "PAYMENTS", "SECURITY", "OFFICIAL", "TRIPDIFFERENCE",
    "TRIP-DIFFERENCE", "TEST", "NULL", "NONE", "API", "WWW",
})


def custom_code_problem(code):
    """None when a scout may choose `code` as their link, else why not.
    Uniqueness is the database's to answer (referral_codes.code is unique,
    case-insensitively)."""
    code = (code or "").strip()
    if not (CUSTOM_MIN <= len(code) <= CUSTOM_MAX):
        return f"Use {CUSTOM_MIN} to {CUSTOM_MAX} characters."
    if not _CUSTOM_SHAPE.match(code):
        return "Use letters, numbers and single hyphens only, starting and ending with a letter or number."
    if code.upper() in _RESERVED:
        return "That name is reserved. Pick another."
    for issued in ("SCOUT-", INVITE_PREFIX):
        if code.upper().startswith(issued):
            return f"Codes starting with {issued} are the ones Trip Difference issues. Pick another."
    return None


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
    if code.startswith(INVITE_PREFIX):   # an invite is never a business code (migration 041)
        return None, INVITE_AT_SIGNUP
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


def _shared_identity(email, scout_email):
    """What `email` shares with the scout's: 'email', 'company' (a domain that
    isn't free mail), 'free_mail', or None — with the domain."""
    email, scout_email = (email or "").strip().lower(), (scout_email or "").strip().lower()
    if not email or not scout_email:
        return None, None
    if email == scout_email:
        return "email", None
    domain, scout_domain = email.rpartition("@")[2], scout_email.rpartition("@")[2]
    if domain and domain == scout_domain:
        return ("free_mail" if domain in FREE_MAIL else "company"), domain
    return None, None


def self_referral(email, referral):
    """('block', message) when the signup is the scout's own: the same email,
    or the same company domain. ('flag', reason) when it shares only a
    free-mail domain with the scout — let through, and listed for an admin
    (accounts.referral_flag). (None, None) otherwise."""
    shared, domain = _shared_identity(email, referral.get("scout_email"))
    if shared == "email":
        return "block", "You can't sign up with your own referral code."
    if shared == "company":
        return "block", "This referral code can't be used by someone at the scout's own company."
    if shared == "free_mail":
        return "flag", f"Signup email shares the scout's free-mail domain ({domain})."
    return None, None


def referrer_name(referral):
    """How the signup form names the scout: their first name, or a fallback."""
    return (referral.get("scout_given_name") or "").strip() or "a Trip Difference scout"


# ---------------------------------------------------------------------------
# scout invites (migration 041)
# ---------------------------------------------------------------------------
#
# /join/<code> sets td_scout_ref, signed under a salt of its own, so a
# business link's cookie can never stand in for an invite or the other way
# round. Invite codes live in their own table (db.scout_invite_lookup), so a
# business code is never found here and an invite never at signup. Unlike
# signup's lock, this one does not refuse a form value that differs: the
# invite is optional, so with the cookie present the application uses the
# cookie's code and ignores the field. Either way the server decides, not the
# readonly field.

INVITE_COOKIE = "td_scout_ref"

INVITE_INVALID = "That isn't a valid scout invite code. Scout invite codes start with INVITE-."
INVITE_INACTIVE = "This invite link is no longer active."
INVITE_UNVERIFIED = ("Your invite link couldn't be verified. Open the link again, or apply without an invite.")
ALREADY_A_SCOUT = ("{email} already has a scout application or scout account, and an invite can't be added "
                   "to an existing scout. Apply without the invite, or sign in.")


def _invite_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="td-scout-ref")


def invite_sign(code):
    return _invite_serializer().dumps(normalize(code))


def invite_locked_code(cookie_value):
    """(code, verified) for a td_scout_ref cookie, like locked_code."""
    if not cookie_value:
        return None, True
    try:
        code = _invite_serializer().loads(cookie_value, max_age=int(COOKIE_MAX_AGE.total_seconds()))
    except BadSignature:  # SignatureExpired included
        return None, False
    return code, True


def invite_check(code):
    """(row, None) when an application may come in on this invite: it exists,
    is active, and its scout is approved. Else (None, message)."""
    code = normalize(code)
    row = db.scout_invite_lookup(code) if code.startswith(INVITE_PREFIX) else None
    if not row or row["is_system"]:
        return None, INVITE_INVALID
    if not row["active"] or row["scout_status"] != "approved":
        return None, INVITE_INACTIVE
    return row, None


def invite_resolve(*, cookie_value, form_code, ip):
    """The invite an application is made through: (row, None); (None, None)
    when it has none, which is allowed; or (None, (message, status)). The
    cookie's code wins over the form's, then the throttle (shared with
    referral codes: guessing either kind counts) and invite_check()."""
    locked, verified = invite_locked_code(cookie_value)
    if not verified:
        return None, (INVITE_UNVERIFIED, 400)
    code = normalize(locked) or normalize(form_code)
    if not code:
        return None, None
    if db.referral_recent_failures(ip, FAILURE_WINDOW_MIN) >= MAX_FAILURES:
        return None, (THROTTLED, 429)
    row, problem = invite_check(code)
    if problem:
        db.referral_failure_record(ip, code)
        return None, (problem, 400)
    return row, None


def self_invite(email, invite):
    """self_referral's rules for an invite: ('block', message) for the
    inviting scout's own email or company domain, ('flag', reason) for a
    shared free-mail domain — allowed, and shown to whoever reviews the
    application (scouts.invite_flag) — else (None, None)."""
    shared, domain = _shared_identity(email, invite.get("scout_email"))
    if shared == "email":
        return "block", "You can't apply with your own invite link."
    if shared == "company":
        return "block", "This invite can't be used by someone at the inviting scout's own company."
    if shared == "free_mail":
        return "flag", f"Applicant shares the inviting scout's free-mail domain ({domain})."
    return None, None
