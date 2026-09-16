"""
Durable state. Replaces orders.json and decisions.log.

Two connection strings, and the difference matters:

  POSTGRES_URL              Supabase's transaction pooler (6543). Everything the
                            request path does goes here. Transaction-mode
                            pooling cannot hold server-side prepared statements,
                            so `prepare_threshold=None` is not optional.
  POSTGRES_URL_NON_POOLING  Direct (5432). DDL and migrations only.

Money is NUMERIC in the database and Decimal in Python; the string forms the
templates render come from str(Decimal), which round-trips Duffel's "221.51".
"""

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

import config
from db_identity import identity

SESSION_TTL = timedelta(days=14)

# Every account the test suite creates carries this name, because tests share
# the staging database with live staging orders. Sign-in never resolves to
# one (user_by_email / user_by_supabase_id), signup never produces the name
# for a real account (create_account), validation/observe.py and
# validation/report.py leave them out, and live_guard never lets one spend.
TEST_ACCOUNT_NAME = config.TEST_ACCOUNT_NAME

_pool = None


# libpq rejects query parameters it does not recognise, and Supabase tags its
# pooler URL with `supa=base-pooler.x`. Keep only real connection parameters.
_LIBPQ_PARAMS = {
    "host", "hostaddr", "port", "dbname", "user", "password", "passfile",
    "service", "options", "application_name", "fallback_application_name",
    "connect_timeout", "client_encoding", "keepalives", "keepalives_idle",
    "keepalives_interval", "keepalives_count", "tcp_user_timeout",
    "replication", "gssencmode", "target_session_attrs", "load_balance_hosts",
    "channel_binding", "require_auth", "sslmode", "sslcert", "sslkey",
    "sslpassword", "sslrootcert", "sslcrl", "sslcrldir", "sslsni",
    "ssl_min_protocol_version", "ssl_max_protocol_version",
}


def _clean(url):
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k in _LIBPQ_PARAMS]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def _url():
    url = os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("POSTGRES_URL is not set — run `vercel env pull .env.local`")
    return _clean(url)


def pool():
    """Lazy so importing this module never opens a socket (and never breaks a
    build or a test run that has no database)."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            _url(),
            min_size=0, max_size=4, timeout=10,
            # pgbouncer transaction mode: no server-side prepared statements.
            kwargs={"prepare_threshold": None, "row_factory": dict_row},
            open=True,
        )
    return _pool


def _is_staging_database(url):
    return identity(url)[:2] == ("supabase", config.STAGING_SUPABASE_REF)


def project_ref():
    """The Supabase project POSTGRES_URL reaches, or None (unset, or not
    Supabase). Never any other part of the URL."""
    url = (os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        return None
    ident = identity(url)
    return ident[1] if ident[0] == "supabase" else None


def startup_check():
    """Run at app import. APP_ENV=staging must run against the staging
    Supabase project (config.STAGING_SUPABASE_REF); APP_ENV=production must
    never. Both URLs are checked when set. dev is not checked."""
    urls = {name: os.environ.get(name, "").strip() for name in ("POSTGRES_URL", "POSTGRES_URL_NON_POOLING")}
    if not urls["POSTGRES_URL"] and os.environ.get("DATABASE_URL", "").strip():
        urls["POSTGRES_URL"] = os.environ["DATABASE_URL"].strip()
    if config.APP_ENV == "staging":
        if not urls["POSTGRES_URL"]:
            raise RuntimeError("Refusing to start: APP_ENV=staging but POSTGRES_URL is not set")
        for name, url in urls.items():
            if url and not _is_staging_database(url):
                raise RuntimeError(f"Refusing to start: APP_ENV=staging but {name} is not the staging "
                                   f"database (Supabase project {config.STAGING_SUPABASE_REF})")
    elif config.APP_ENV == "production":
        for name, url in urls.items():
            if url and _is_staging_database(url):
                raise RuntimeError(f"Refusing to start: APP_ENV=production but {name} is the staging "
                                   f"database (Supabase project {config.STAGING_SUPABASE_REF})")


def q(sql, params=(), *, fetch=None):
    """One statement, one transaction. fetch: None | 'one' | 'all'."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        return None


def _num(v):
    if v is None or v == "":
        return None
    return Decimal(str(v))


# ---------------------------------------------------------------------------
# audit trail  (append-only; the table refuses UPDATE and DELETE)
# ---------------------------------------------------------------------------

def audit_append(payload):
    """Sink for engine.log_decision / log_eligibility / log_execution.

    `payload` is the same dict the JSONL carried. It is stored verbatim in
    `payload`; the promoted columns exist only so ops views can sort and filter.
    """
    ts = payload.get("ts")
    q("""INSERT INTO audit_events
           (ts, kind, order_id, source, outcome, reason, execution,
            market_best, market_delta, change_total, currency, detail, payload)
         VALUES (COALESCE(%s::timestamptz, now()), %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s, %s)""",
      (ts,
       payload.get("kind", "decision"),
       payload.get("order_id", ""),
       payload.get("source", "") or "",
       payload.get("outcome"),
       payload.get("reason") or payload.get("state"),
       payload.get("execution"),
       _num(payload.get("market_best")),
       _num(payload.get("market_delta")),
       _num(payload.get("change_total")),
       payload.get("currency", "") or "",
       payload.get("detail", "") or "",
       Jsonb(payload)))


_AUDIT_COLS = """id, ts, kind, order_id, source, outcome, reason, execution,
                 market_best, market_delta, change_total, currency, detail, payload"""


def _audit_payload(r):
    """One audit_events row → the dict shape templates/decisions.html already
    renders. The promoted columns win over the payload. The payload is
    whatever the writer chose to record and a row can predate a field or
    omit it entirely; ts, kind and order_id are always present on the row
    itself, and the log must not fail to render because one entry is shaped
    differently."""
    payload = dict(r["payload"] or {})
    payload["ts"] = r["ts"].isoformat()
    payload["kind"] = r["kind"]
    payload["order_id"] = r["order_id"]
    for k in ("source", "outcome", "reason", "execution", "detail"):
        if not payload.get(k) and r[k]:
            payload[k] = r[k]
    for k in ("market_best", "market_delta", "change_total"):
        if not payload.get(k) and r[k] is not None:
            payload[k] = str(r[k])
    return payload


def audit_rows(order_id, limit=500):
    """Newest first, for one order. order_id is required on purpose — this
    used to also support an unscoped "every account's rows" mode with no
    caller-side check at all, which /decisions called directly and handed
    to any authenticated user regardless of account. The only remaining
    legitimate use (price_history(), gated by find_order() before it ever
    gets here) only ever needed the single-order form; removing the other
    one removes the unscoped query from existing at all, rather than
    leaving it sitting here for the next caller who wants "all the rows."
    See audit_rows_for_account for the account-scoped equivalent."""
    rows = q(f"""SELECT {_AUDIT_COLS} FROM audit_events WHERE order_id = %s
                 ORDER BY ts DESC, id DESC LIMIT %s""",
             (order_id, limit), fetch="all")
    return [_audit_payload(r) for r in rows]


def audit_rows_for_account(account_id, limit=20):
    """Newest first, scoped to one account's own orders — the Activity
    Timeline's data source, straight from audit_events (same table
    /decisions already reads), no separate display list."""
    rows = q(f"""SELECT {_AUDIT_COLS} FROM audit_events
                  WHERE order_id IN (SELECT order_id FROM orders WHERE account_id = %s)
                  ORDER BY ts DESC, id DESC LIMIT %s""",
             (account_id, limit), fetch="all")
    return [_audit_payload(r) for r in rows]


# ---------------------------------------------------------------------------
# carrier change-capability — real available_actions observations, not a
# score (see migration 027 and eligibility.assess's carrier_capability arg)
# ---------------------------------------------------------------------------

def carrier_capability_for(carrier_iata):
    """None means never observed — the caller (offer_view, single-offer
    path) should treat that exactly like today's behaviour: no override.
    is_synthetic flags a sandbox-only carrier (Duffel Airways/ZZ) whose
    confirm is real in the sense that it happened, but isn't evidence about
    real airline behaviour — ranking still reads it (sandbox needs a sane
    result), but a future read answering "what do real carriers do" should
    filter it out."""
    if not carrier_iata:
        return None
    row = q("""SELECT change_confirmed_count, change_denied_count, is_synthetic
              FROM carrier_change_capability WHERE carrier_iata = %s""",
           (carrier_iata,), fetch="one")
    return {"confirmed": row["change_confirmed_count"], "denied": row["change_denied_count"],
           "is_synthetic": row["is_synthetic"]} if row else None


def carrier_capabilities_for(carrier_iatas):
    """Batch form — one query for every carrier in a search's results
    rather than one per offer. Same shape as carrier_capability_for's
    return value, keyed by carrier_iata."""
    codes = sorted({c for c in carrier_iatas if c})
    if not codes:
        return {}
    rows = q("""SELECT carrier_iata, change_confirmed_count, change_denied_count, is_synthetic
               FROM carrier_change_capability WHERE carrier_iata = ANY(%s)""",
            (codes,), fetch="all")
    return {r["carrier_iata"]: {"confirmed": r["change_confirmed_count"],
                                "denied": r["change_denied_count"],
                                "is_synthetic": r["is_synthetic"]} for r in rows}


def carrier_capability_record(carrier_iata, carrier_name, change_allowed, fare_brand="", order_id=None):
    """One real order is one free observation of whether this carrier's
    orders actually carry 'change' in available_actions. Call only when
    available_actions was present on the order at all — no field, no
    observation, nothing to record.

    Writes two things: the aggregate counts ranking actually reads, and a
    raw per-order log (carrier_change_observations) nobody reads yet —
    kept anyway so "do denials cluster by fare brand rather than by
    carrier" (FINDINGS.md already suspects this) is a question that can
    still be asked once there's enough data to answer it.
    """
    if not carrier_iata:
        return
    confirmed, denied = (1, 0) if change_allowed else (0, 1)
    q("""INSERT INTO carrier_change_capability
             (carrier_iata, carrier_name, change_confirmed_count, change_denied_count, last_observed_at)
         VALUES (%s, %s, %s, %s, now())
         ON CONFLICT (carrier_iata) DO UPDATE SET
             carrier_name = CASE WHEN carrier_change_capability.carrier_name = ''
                                  THEN EXCLUDED.carrier_name
                                  ELSE carrier_change_capability.carrier_name END,
             change_confirmed_count = carrier_change_capability.change_confirmed_count
                                       + EXCLUDED.change_confirmed_count,
             change_denied_count = carrier_change_capability.change_denied_count
                                    + EXCLUDED.change_denied_count,
             last_observed_at = now(), updated_at = now()""",
      (carrier_iata, carrier_name or "", confirmed, denied))
    q("""INSERT INTO carrier_change_observations
             (carrier_iata, fare_brand, change_allowed, order_id)
         VALUES (%s, %s, %s, %s)""",
      (carrier_iata, fare_brand or "", bool(change_allowed), order_id))


# ---------------------------------------------------------------------------
# orders
# ---------------------------------------------------------------------------

_ORDER_COLS = ("booking_reference", "route", "itinerary", "carrier",
               "departure_date", "paid", "original_paid", "refunded",
               "currency", "monitoring", "executed", "raw", "last_decision",
               "sim_scenario", "simulated", "sim_paid", "sim_refunded",
               "offer_id", "source", "fare_type", "traveler_id",
               "seg_origin", "seg_destination", "seg_flight_number", "seg_cabin",
               "cost_center_id", "refundable", "fare_conditions",
               "stripe_payment_intent_id", "payment_capture_failed_at",
               "payment_capture_error", "duffel_mode")
_MONEY = {"paid", "original_paid", "refunded", "sim_paid", "sim_refunded"}
_JSON = {"raw", "last_decision", "sim_scenario", "fare_conditions"}
# NOT NULL DEFAULT '' columns. We always pass every column, so a column's
# DEFAULT never fires — the coercion has to happen here instead.
_TEXT_NOT_NULL = {"booking_reference", "route", "itinerary", "carrier", "currency",
                  "seg_origin", "seg_destination", "seg_flight_number", "seg_cabin"}
# Columns with their own DB default and a CHECK constraint, so an
# absent/blank value here must fall through to the column default rather
# than being coerced to '' like the plain text fields above.
# duffel_mode is set once, at creation, from the token that bought the ticket
# (migration 030 refuses to change it afterwards); an order created without
# one is a test-mode order.
_ORDER_DEFAULTS = {"source": "td_rebook", "fare_type": "cash", "duffel_mode": "test"}
# Nullable uuid FK — a blank string must stay NULL, not become '' (invalid
# uuid input), unlike the plain text fields above.
_NULLABLE_UUID = {"traveler_id", "cost_center_id"}
# refundable (nullable boolean), stripe_payment_intent_id, and the
# payment_capture_* columns need no coercion at all — None must stay None
# (unknown disposition, no charge yet, no capture problem), not become
# False/''. They fall through the loop below untouched, same as any column
# not named in one of these sets.


def _to_record(row):
    """DB row → the dict shape app.py and the templates already expect."""
    if row is None:
        return None
    rec = dict(row)
    for k in _MONEY:
        rec[k] = str(rec[k]) if rec.get(k) is not None else None
    d = rec.get("departure_date")
    rec["departure_date"] = d.isoformat() if d else None
    return rec


def load_orders(account_id):
    rows = q("""SELECT * FROM orders WHERE account_id = %s
                ORDER BY created_at""", (account_id,), fetch="all")
    return [_to_record(r) for r in rows]


def find_order(order_id, account_id=None):
    if account_id:
        row = q("SELECT * FROM orders WHERE order_id=%s AND account_id=%s",
                (order_id, account_id), fetch="one")
    else:
        row = q("SELECT * FROM orders WHERE order_id=%s", (order_id,), fetch="one")
    return _to_record(row)


def upsert_order(record, account_id):
    """Read-modify-write, but inside one transaction with the row locked.

    The JSON-file version this replaces lost updates whenever two requests
    touched the same order: both read, both wrote, last writer won.
    """
    order_id = record["order_id"]
    fields = {k: v for k, v in record.items() if k in _ORDER_COLS}

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM orders WHERE order_id=%s FOR UPDATE",
                    (order_id,))
        existing = cur.fetchone()

        merged = dict(existing) if existing else {}
        merged.update(fields)

        vals = {}
        for k in _ORDER_COLS:
            v = merged.get(k)
            if k in _MONEY:
                v = _num(v)
            elif k in _JSON:
                v = Jsonb(v) if v is not None else None
            elif k in _TEXT_NOT_NULL:
                v = v or ""
            elif k in _ORDER_DEFAULTS:
                v = v or _ORDER_DEFAULTS[k]
            elif k in _NULLABLE_UUID:
                v = v or None
            elif k in ("monitoring", "simulated"):
                v = bool(v)
            vals[k] = v

        cols = ", ".join(_ORDER_COLS)
        holders = ", ".join(["%s"] * len(_ORDER_COLS))
        updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in _ORDER_COLS)
        cur.execute(
            f"""INSERT INTO orders (order_id, account_id, {cols})
                VALUES (%s, %s, {holders})
                ON CONFLICT (order_id) DO UPDATE
                  SET {updates}, updated_at = now()
                RETURNING *""",
            (order_id, account_id, *[vals[c] for c in _ORDER_COLS]))
        return _to_record(cur.fetchone())


def create_manual_order(account_id, *, traveler_id, seg_origin, seg_destination,
                         seg_flight_number, seg_cabin, carrier, booking_reference,
                         departure_date, paid, currency, fare_type):
    """A reservation with no Duffel order behind it — booked with the airline
    directly, added by hand. No `raw` payload exists to derive display fields
    from, so the segment is stored for real instead (see migration 010).
    """
    order_id = "manual_" + secrets.token_urlsafe(8)
    return upsert_order({
        "order_id": order_id, "source": "manual", "fare_type": fare_type,
        "raw": {}, "monitoring": False,
        "traveler_id": traveler_id or None,
        "seg_origin": seg_origin, "seg_destination": seg_destination,
        "seg_flight_number": seg_flight_number, "seg_cabin": seg_cabin,
        "carrier": carrier, "booking_reference": booking_reference,
        "route": f"{seg_origin}-{seg_destination}", "itinerary": seg_flight_number,
        "departure_date": departure_date or None,
        "paid": paid, "currency": currency,
    }, account_id)


# ---------------------------------------------------------------------------
# idempotency for the calls that move money
# ---------------------------------------------------------------------------

class AlreadyAttempted(Exception):
    """A matching attempt exists. Carries it so the caller can report status."""

    def __init__(self, attempt):
        super().__init__("this execution has already been attempted")
        self.attempt = attempt


def claim_execution(order_id, action, change_offer_id=""):
    """Reserve the right to call Duffel exactly once for this (order, action,
    offer). The UNIQUE constraint is the guarantee — a duplicate submit loses
    the INSERT race and never reaches the network.
    """
    row = q("""INSERT INTO execution_attempts (order_id, action, change_offer_id)
               VALUES (%s, %s, %s)
               ON CONFLICT (order_id, action, change_offer_id) DO NOTHING
               RETURNING *""",
            (order_id, action, change_offer_id or ""), fetch="one")
    if row is None:
        existing = q("""SELECT * FROM execution_attempts
                        WHERE order_id=%s AND action=%s AND change_offer_id=%s""",
                     (order_id, action, change_offer_id or ""), fetch="one")
        raise AlreadyAttempted(existing)
    return row


def finish_execution(attempt_id, status, note=None, duffel_change_id=None, result=None):
    q("""UPDATE execution_attempts
           SET status=%s, note=%s, duffel_change_id=%s, result=%s,
               finished_at=now()
         WHERE id=%s""",
      (status, note, duffel_change_id,
       Jsonb(result) if result is not None else None, attempt_id))


def release_execution(attempt_id):
    """Drop a claim that never reached Duffel, so a genuine retry is possible.
    Only safe when the failure happened strictly before the network call."""
    q("DELETE FROM execution_attempts WHERE id=%s AND status='in_progress'",
      (attempt_id,))


# ---------------------------------------------------------------------------
# savings events — which delivery path an execution actually produced
# ---------------------------------------------------------------------------

def savings_event_create(order_id, *, execution_attempt_id, old_amount, new_amount,
                          realized_savings, currency, delivery_type, delivery_detail,
                          commission_rate):
    """Record what an execution actually delivered, once — called from the
    same branch in app.execute() that already knows the Duffel result, not a
    second decision engine re-deriving it."""
    commission_amount = (Decimal(realized_savings) * Decimal(commission_rate)
                         ).quantize(Decimal("0.01"))
    return q("""INSERT INTO savings_events
                   (order_id, execution_attempt_id, old_amount, new_amount,
                    realized_savings, currency, delivery_type, delivery_detail,
                    commission_rate, commission_amount, status)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'completed')
                 RETURNING *""",
            (order_id, execution_attempt_id, _num(old_amount), _num(new_amount),
             _num(realized_savings), currency, delivery_type, delivery_detail,
             _num(commission_rate), commission_amount), fetch="one")


def savings_events_for_order(order_id):
    rows = q("""SELECT * FROM savings_events WHERE order_id = %s
                ORDER BY created_at DESC""", (order_id,), fetch="all")
    for r in rows:
        for k in ("old_amount", "new_amount", "realized_savings",
                  "commission_rate", "commission_amount"):
            r[k] = str(r[k]) if r.get(k) is not None else None
    return rows


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

def _hash_token(token):
    return hashlib.sha256(token.encode()).digest()


def create_account(email, password_hash=None, supabase_user_id=None):
    """Open signup: one new account plus its first user. Just email/password
    (or a linked Google identity) — name, DOB and the rest of the profile
    are collected in the onboarding profile step (complete_profile), not
    here. `accounts.name` gets a placeholder from the email's local part
    until complete_profile fills in the real name.

    Both rows or neither — a user without an account has nothing to own.
    """
    placeholder = email.split("@")[0]
    if placeholder == TEST_ACCOUNT_NAME:
        placeholder = "account"  # reserved for test accounts; see TEST_ACCOUNT_NAME
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name) VALUES (%s) RETURNING id", (placeholder,))
        account_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO users (account_id, email, password_hash, supabase_user_id)
               VALUES (%s, %s, %s, %s) RETURNING *""",
            (account_id, email, password_hash, supabase_user_id))
        return cur.fetchone()


def complete_profile(user_id, account_id, *, given_name, family_name, middle_name,
                     born_on, referral_source, invite_code):
    """Onboarding step 2 — the 'let's get to know you' fields."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE users SET given_name = %s, family_name = %s, middle_name = %s,
                                born_on = %s, referral_source = %s, invite_code = %s
                WHERE id = %s RETURNING *""",
            (given_name, family_name, middle_name, born_on or None,
             referral_source, invite_code, user_id))
        return cur.fetchone()


def user_by_email(email):
    """Sign-in lookup. Never resolves to a test account (TEST_ACCOUNT_NAME)."""
    return q("""SELECT u.* FROM users u JOIN accounts a ON a.id = u.account_id
                 WHERE u.email = %s AND a.name <> %s""",
             (email, TEST_ACCOUNT_NAME), fetch="one")


def user_by_supabase_id(supabase_user_id):
    """Google sign-in lookup. Never resolves to a test account."""
    return q("""SELECT u.* FROM users u JOIN accounts a ON a.id = u.account_id
                 WHERE u.supabase_user_id = %s AND a.name <> %s""",
             (supabase_user_id, TEST_ACCOUNT_NAME), fetch="one")


def link_supabase_id(user_id, supabase_user_id):
    q("UPDATE users SET supabase_user_id = %s WHERE id = %s",
      (supabase_user_id, user_id))


def email_taken(email):
    return q("SELECT 1 FROM users WHERE email = %s", (email,), fetch="one") is not None


def account_settings_update(user_id, *, nickname, phone_number):
    """Settings' General Information panel. Account-level fields only —
    per-Traveler data (loyalty, trusted traveler, preferences) lives on
    travelers and is edited from Travelers, not here."""
    q("""UPDATE users SET nickname = %s, phone_number = %s WHERE id = %s""",
      (nickname or "", phone_number or "", user_id))


# ---------------------------------------------------------------------------
# stripe — card on file
# ---------------------------------------------------------------------------

def account_stripe_ids(account_id):
    return q("""SELECT stripe_customer_id, stripe_payment_method_id
                FROM accounts WHERE id = %s""", (account_id,), fetch="one")


def account_set_stripe_customer(account_id, stripe_customer_id):
    q("UPDATE accounts SET stripe_customer_id = %s WHERE id = %s",
      (stripe_customer_id, account_id))


def account_save_payment_method(account_id, *, stripe_payment_method_id, brand,
                                last4, exp_month, exp_year):
    q("""UPDATE accounts SET stripe_payment_method_id = %s, card_brand = %s,
                             card_last4 = %s, card_exp_month = %s, card_exp_year = %s
         WHERE id = %s""",
      (stripe_payment_method_id, brand or "", last4 or "", exp_month, exp_year, account_id))


def account_card(account_id):
    """None until a card is on file — the shape eligibility.assess's
    has_card gate and every template that shows the card both key off."""
    row = q("""SELECT card_brand, card_last4, card_exp_month, card_exp_year
                FROM accounts WHERE id = %s AND stripe_payment_method_id IS NOT NULL""",
            (account_id,), fetch="one")
    if not row:
        return None
    return {
        "brand": row["card_brand"].title(), "last4": row["card_last4"],
        "expiry": f"{row['card_exp_month']:02d}/{str(row['card_exp_year'])[-2:]}"
                  if row["card_exp_month"] and row["card_exp_year"] else "",
        "holder": "",
    }


# ---------------------------------------------------------------------------
# email capture layer
# ---------------------------------------------------------------------------

def email_import_sources(account_id):
    return q("""SELECT * FROM email_import_sources WHERE account_id = %s
                ORDER BY created_at""", (account_id,), fetch="all")


def email_import_source_create(account_id, kind, address, status="pending"):
    return q("""INSERT INTO email_import_sources (account_id, kind, address, status)
                VALUES (%s, %s, %s, %s) RETURNING *""",
            (account_id, kind, address, status), fetch="one")


def email_import_source_authorized(account_id, from_address):
    """Is `from_address` on this account's allowlist of forwarding senders?
    Routing (which account a forwarded email belongs to) comes from the
    `to` address's plus-addressed account_id, decoded before this is
    called — this only answers whether that account has vouched for the
    sender, so a stranger can't forward junk into someone else's reservations."""
    return q("""SELECT 1 FROM email_import_sources
                WHERE account_id = %s AND kind = 'forwarding'
                  AND lower(address) = lower(%s) AND status = 'active'""",
            (account_id, from_address), fetch="one") is not None


def start_session(user_id):
    """Returns the raw cookie value. Only its hash is stored."""
    token = secrets.token_urlsafe(32)
    q("""INSERT INTO sessions (token_hash, user_id, expires_at)
         VALUES (%s, %s, %s)""",
      (_hash_token(token), user_id, datetime.now(timezone.utc) + SESSION_TTL))
    return token


def session_user(token):
    if not token:
        return None
    return q("""SELECT u.*, a.name AS company
                  FROM sessions s
                  JOIN users u ON u.id = s.user_id
                  JOIN accounts a ON a.id = u.account_id
                 WHERE s.token_hash = %s
                   AND s.revoked_at IS NULL
                   AND s.expires_at > now()""",
             (_hash_token(token),), fetch="one")


def end_session(token):
    if token:
        q("UPDATE sessions SET revoked_at = now() WHERE token_hash = %s",
          (_hash_token(token),))


# ---------------------------------------------------------------------------
# login throttling
# ---------------------------------------------------------------------------

def record_failure(email, ip):
    q("INSERT INTO auth_failures (email, ip) VALUES (%s, %s::inet)",
      (email or "", ip or None))


def recent_failures(email, ip, window_minutes):
    """Worst of the two counts. Throttling only by email lets one attacker
    spray many accounts from one host; only by IP lets a botnet grind one
    account."""
    row = q("""SELECT
                 count(*) FILTER (WHERE email = %s) AS by_email,
                 count(*) FILTER (WHERE ip = %s::inet) AS by_ip
               FROM auth_failures
               WHERE ts > now() - make_interval(mins => %s)""",
            (email or "", ip or None, window_minutes), fetch="one")
    return max(row["by_email"], row["by_ip"])


def clear_failures(email):
    q("DELETE FROM auth_failures WHERE email = %s", (email or "",))


# ---------------------------------------------------------------------------
# travelers (saved passenger profiles)
# ---------------------------------------------------------------------------

TRAVELER_FIELDS = ("title", "given_name", "family_name", "born_on", "gender",
                   "email", "phone_number")

# Profile fields the bolt-on model needs — loyalty, trusted-traveler,
# preferences. Kept apart from TRAVELER_FIELDS on purpose: that tuple feeds
# the passenger dict a booking sends straight to Duffel, and none of this
# belongs in that payload.
TRAVELER_TEXT_PROFILE_FIELDS = ("nationality", "known_traveler_number",
                                "redress_number", "canadian_travel_number",
                                "home_airport", "seat_preference", "preferred_airline")


def _traveler(row):
    if row is None:
        return None
    t = dict(row)
    t["born_on"] = t["born_on"].isoformat() if t.get("born_on") else ""
    t["id"] = str(t["id"])        # so the picker can serialise these to JSON
    t["default_cost_center_id"] = str(t["default_cost_center_id"]) if t.get("default_cost_center_id") else ""
    t["loyalty_programs"] = t.get("loyalty_programs") or []
    return t


def travelers(account_id):
    rows = q("""SELECT * FROM travelers WHERE account_id = %s
                ORDER BY family_name, given_name""", (account_id,), fetch="all")
    return [_traveler(r) for r in rows]


def onboarding_counts(account_id):
    """The two real counts the sidebar's onboarding-progress widget needs —
    one query, not account_summary()'s full aggregate set."""
    return q("""SELECT
                  (SELECT count(*) FROM orders WHERE account_id = %s)    AS bookings,
                  (SELECT count(*) FROM travelers WHERE account_id = %s) AS travelers""",
            (account_id, account_id), fetch="one")


def traveler(traveler_id, account_id):
    return _traveler(q("SELECT * FROM travelers WHERE id = %s AND account_id = %s",
                       (traveler_id, account_id), fetch="one"))


def traveler_save(data, account_id, traveler_id=None):
    """`data` may carry TRAVELER_FIELDS only (the original booking-profile
    form) or also TRAVELER_TEXT_PROFILE_FIELDS/clear_plus/loyalty_programs
    (the fuller bolt-on profile) — fields not present just keep their
    existing value on update, or the column default on insert.

    `default_cost_center_id` is optional and, when present, is trusted to
    have already been ownership-checked by the caller (same pattern as
    book()'s cost_center_id) — this function does no FK validation itself.
    """
    cols = (list(TRAVELER_FIELDS) + list(TRAVELER_TEXT_PROFILE_FIELDS)
            + ["clear_plus", "loyalty_programs", "default_cost_center_id"])
    vals = []
    for f in cols:
        if f == "born_on":
            vals.append(data.get(f) or None)
        elif f == "clear_plus":
            vals.append(bool(data.get(f)))
        elif f == "loyalty_programs":
            vals.append(Jsonb(data.get(f) or []))
        elif f == "default_cost_center_id":
            vals.append(data.get(f) or None)
        else:
            vals.append(data.get(f) or "")

    if traveler_id:
        sets = ", ".join(f"{f} = %s" for f in cols)
        return _traveler(q(f"""UPDATE travelers SET {sets}, updated_at = now()
                               WHERE id = %s AND account_id = %s RETURNING *""",
                           (*vals, traveler_id, account_id), fetch="one"))
    col_list = ", ".join(cols)
    holders = ", ".join(["%s"] * len(cols))
    return _traveler(q(f"""INSERT INTO travelers (account_id, {col_list})
                           VALUES (%s, {holders}) RETURNING *""",
                       (account_id, *vals), fetch="one"))


def traveler_delete(traveler_id, account_id):
    q("DELETE FROM travelers WHERE id = %s AND account_id = %s",
      (traveler_id, account_id))


# ---------------------------------------------------------------------------
# account summary
# ---------------------------------------------------------------------------

def account_summary(account_id):
    """The numbers Overview shows.

    Computed here, once, so Overview and the pages it summarises cannot drift
    apart — a dashboard claiming 184 travelers over a roster of 8 is the
    failure mode this exists to prevent.

    Deliberately carries no recovered/rebooked figure of its own: those used
    to sum orders.refunded, which only execute()'s exchange branch ever set
    — a real cancellation-driven recovery (cash or credit) left it NULL, so
    the figure silently undercounted and was, on inspection, rendered in no
    template anyway. savings_events is the canonical source for recovery
    reporting now (see spend_by_carrier); nothing here duplicates it.
    """
    row = q("""SELECT
                 count(*)                                        AS bookings,
                 count(*) FILTER (WHERE monitoring)               AS monitoring,
                 count(*) FILTER (WHERE departure_date >= current_date)
                                                                  AS upcoming,
                 count(*) FILTER (WHERE simulated)                 AS simulated,
                 COALESCE(sum(sim_refunded) FILTER (WHERE simulated), 0) AS sim_recovered,
                 COALESCE(sum(paid), 0)                           AS spend,
                 max(currency) FILTER (WHERE currency <> '')      AS currency
               FROM orders WHERE account_id = %s""", (account_id,), fetch="one")
    out = dict(row)
    out["currency"] = out["currency"] or "USD"
    out["travelers"] = q("SELECT count(*) AS n FROM travelers WHERE account_id = %s",
                         (account_id,), fetch="one")["n"]
    out["decisions"] = q("""SELECT count(*) AS n FROM audit_events
                            WHERE order_id IN (SELECT order_id FROM orders
                                               WHERE account_id = %s)""",
                         (account_id,), fetch="one")["n"]
    out["savings_events"] = q("""SELECT count(*) AS n FROM savings_events
                                 WHERE order_id IN (SELECT order_id FROM orders
                                                    WHERE account_id = %s)""",
                              (account_id,), fetch="one")["n"]
    return out


# ---------------------------------------------------------------------------
# dashboard series
# ---------------------------------------------------------------------------
#
# Every one of these reads real rows. A dashboard that invents a trend is worse
# than an empty one, so a quiet month is a zero and an account with no history
# gets an empty state rather than a shape.

def monthly_series(account_id, months=6):
    """Spend and recovered per calendar month, oldest first, gaps filled."""
    rows = q("""WITH span AS (
                  SELECT generate_series(
                    date_trunc('month', now()) - make_interval(months => %s - 1),
                    date_trunc('month', now()), '1 month') AS m)
                SELECT to_char(span.m, 'Mon')            AS label,
                       to_char(span.m, 'YYYY-MM')        AS ym,
                       COALESCE(sum(o.paid), 0)          AS spend,
                       COALESCE(sum(o.refunded), 0)      AS recovered
                  FROM span
                  LEFT JOIN orders o
                    ON o.account_id = %s
                   AND date_trunc('month', o.created_at) = span.m
                 GROUP BY span.m
                 ORDER BY span.m""", (months, account_id), fetch="all")
    return [dict(r) for r in rows]


def weekly_activity(account_id, weeks=8):
    """Monitoring checks per week — how much work the engine actually did."""
    rows = q("""WITH span AS (
                  SELECT generate_series(
                    date_trunc('week', now()) - make_interval(weeks => %s - 1),
                    date_trunc('week', now()), '1 week') AS w)
                SELECT to_char(span.w, 'DD Mon')  AS label,
                       count(a.id)                AS checks,
                       count(a.id) FILTER (WHERE a.outcome = 'reshop') AS reshops
                  FROM span
                  LEFT JOIN audit_events a
                    ON date_trunc('week', a.ts) = span.w
                   AND a.order_id IN (SELECT order_id FROM orders WHERE account_id = %s)
                 GROUP BY span.w
                 ORDER BY span.w""", (weeks, account_id), fetch="all")
    return [dict(r) for r in rows]


def recent_bookings(account_id, limit=5):
    rows = q("""SELECT order_id, booking_reference, route, carrier, departure_date,
                       paid, refunded, currency, monitoring, raw
                  FROM orders WHERE account_id = %s
                 ORDER BY created_at DESC LIMIT %s""",
             (account_id, limit), fetch="all")
    out = []
    for r in rows:
        pax = (r["raw"] or {}).get("passengers") or []
        lead = pax[0] if pax else {}
        out.append({
            "order_id": r["order_id"],
            "reference": r["booking_reference"],
            "route": r["route"],
            "carrier": r["carrier"],
            "departure_date": r["departure_date"].isoformat() if r["departure_date"] else "",
            "traveler": f"{lead.get('given_name','')} {lead.get('family_name','')}".strip() or "—",
            "party": len(pax),
            "paid": str(r["paid"]) if r["paid"] is not None else "—",
            "saved": str(r["refunded"]) if r["refunded"] is not None else None,
            "currency": r["currency"],
            "monitoring": r["monitoring"],
        })
    return out


def spend_by_carrier(account_id, top=5):
    """Spend per airline, biggest first, with the tail folded into "Other".

    Folded rather than cycled: past a handful of slots the categories stop
    being tellable apart, and this is one measure across categories anyway.

    "saved" sums savings_events.realized_savings, not orders.refunded —
    that column is only ever written by execute()'s exchange branch, so a
    cancellation-driven recovery (cash or credit) used to be invisible
    here. savings_events is the canonical source for recovery reporting;
    orders.refunded is a convenience field on the order itself, not a
    reporting source. Pre-aggregated per order before joining so an order
    with more than one savings_event doesn't multiply its own `paid` in
    the sum.
    """
    rows = q("""WITH per_order_savings AS (
                  SELECT order_id, sum(realized_savings) AS saved
                    FROM savings_events GROUP BY order_id)
                SELECT COALESCE(NULLIF(o.carrier, ''), 'Unknown') AS carrier,
                       sum(o.paid)                  AS spend,
                       COALESCE(sum(pos.saved), 0)  AS saved,
                       count(*)                     AS bookings
                  FROM orders o
                  LEFT JOIN per_order_savings pos ON pos.order_id = o.order_id
                 WHERE o.account_id = %s AND o.paid IS NOT NULL
                 GROUP BY 1 ORDER BY 2 DESC""", (account_id,), fetch="all")
    rows = [dict(r) for r in rows]
    if len(rows) <= top:
        return rows
    head, tail = rows[:top], rows[top:]
    head.append({"carrier": "Other",
                 "spend": sum(r["spend"] for r in tail),
                 "saved": sum(r["saved"] for r in tail),
                 "bookings": sum(r["bookings"] for r in tail)})
    return head


# ---------------------------------------------------------------------------
# wallet ledger
# ---------------------------------------------------------------------------

def wallet_transactions(account_id, limit=100):
    """Every real movement of money on this account, newest first.

    Two sources, deliberately:

      charges     one per booking, from `orders`
      recoveries  one per real execution, from `savings_events` — the
                  actual customer/commission-facing fact table a real
                  execution populates, not the decision/audit trail

    Real recoveries come from `savings_events`, not `audit_events`: no code
    path in this app has ever set `audit_events.execution = 'executed'`
    (only 'awaiting_confirmation'/'blocked_simulated' are ever logged, at
    decision time, before an execute route runs) — the old query's
    `a.execution = 'executed'` branch was dead code, so a real recovery
    could never have appeared here before this fix, only a simulated one.
    Simulated recoveries still come from `audit_events` (savings_events is
    never populated for them, by design) and are included so the flow can
    be demonstrated, but arrive flagged and are never added into a real
    total — only the most recent run per order shows, since the audit
    trail keeps every attempt but a ledger showing five superseded
    what-ifs is noise.

    A 'forfeited' delivery — the ticket got cheaper and nothing came
    back — is a real, visible row, not silently absent: it renders as a
    recovery of the account's own commission_rate-based zero (nothing was
    realized, so nothing is owed on it either) rather than being dropped
    because no cash moved.
    """
    charges = q("""SELECT created_at AS ts, order_id, booking_reference, route,
                          paid AS amount, currency, source
                     FROM orders
                    WHERE account_id = %s AND paid IS NOT NULL""",
                (account_id,), fetch="all")

    real_execs = q("""SELECT se.created_at AS ts, se.order_id, o.booking_reference, o.route,
                             se.realized_savings, se.commission_amount, se.delivery_type,
                             COALESCE(NULLIF(se.currency, ''), o.currency) AS currency
                        FROM savings_events se
                        JOIN orders o ON o.order_id = se.order_id
                       WHERE o.account_id = %s
                       ORDER BY se.created_at DESC""",
                   (account_id,), fetch="all")

    sim_execs = q("""SELECT * FROM (
                       SELECT DISTINCT ON (a.order_id, a.execution)
                              a.ts, a.order_id, o.booking_reference, o.route,
                              a.payload->>'recovered'   AS recovered,
                              a.payload->>'service_fee' AS service_fee,
                              COALESCE(NULLIF(a.currency, ''), o.currency) AS currency
                         FROM audit_events a
                         JOIN orders o ON o.order_id = a.order_id
                        WHERE o.account_id = %s
                          AND a.kind = 'execution'
                          AND a.execution = 'blocked_simulated'
                          AND o.simulated
                          AND a.payload->>'recovered' IS NOT NULL
                        ORDER BY a.order_id, a.execution, a.ts DESC) x
                     ORDER BY x.ts DESC""",
                  (account_id,), fetch="all")

    _DELIVERY_LABEL = {
        "refund_to_card": "Fare drop recovered — refunded to card",
        "airline_credit": "Fare drop recovered — airline credit issued",
        "forfeited": "Fare drop found — value forfeited, nothing recovered",
    }
    # Real, not yet moved by us: the commission is billed on the account's
    # next invoice (see generate_invoice), not deducted here — this ledger
    # must not read as a completed deduction against money that already
    # moved, since under the merchant-of-record model the full recovery
    # goes straight to the company's card via Duffel, untouched by TD.
    FEE_LABEL = "Service fee — billed on your next invoice, not yet charged"

    rows = []
    for c in charges:
        # td_rebook: TD funded the Duffel purchase — a real debit. Anything
        # else (email_import/manual/forward) is a reservation the customer
        # already paid for elsewhere; recording it moves no money.
        td_funded = c["source"] == "td_rebook"
        rows.append({"ts": c["ts"], "kind": "charge" if td_funded else "intake",
                     "simulated": False,
                     "label": "Flight booked" if td_funded else "Reservation added",
                     "order_id": c["order_id"],
                     "reference": c["booking_reference"], "route": c["route"],
                     "amount": -Decimal(c["amount"]) if td_funded else Decimal("0"),
                     "currency": c["currency"]})
    for e in real_execs:
        recovered = Decimal(e["realized_savings"])
        fee = Decimal(e["commission_amount"] or 0)
        rows.append({"ts": e["ts"], "kind": "recovery", "simulated": False,
                     "delivery_type": e["delivery_type"],
                     "label": _DELIVERY_LABEL[e["delivery_type"]],
                     "order_id": e["order_id"],
                     "reference": e["booking_reference"], "route": e["route"],
                     "amount": recovered, "currency": e["currency"]})
        if fee:
            rows.append({"ts": e["ts"], "kind": "fee", "simulated": False,
                         "label": FEE_LABEL, "order_id": e["order_id"],
                         "reference": e["booking_reference"], "route": e["route"],
                         "amount": -fee, "currency": e["currency"]})
    for e in sim_execs:
        recovered = Decimal(e["recovered"])
        fee = Decimal(e["service_fee"] or 0)
        rows.append({"ts": e["ts"], "kind": "recovery", "simulated": True,
                     "delivery_type": "refund_to_card",
                     "label": "Fare drop recovered", "order_id": e["order_id"],
                     "reference": e["booking_reference"], "route": e["route"],
                     "amount": recovered, "currency": e["currency"]})
        if fee:
            rows.append({"ts": e["ts"], "kind": "fee", "simulated": True,
                         "label": FEE_LABEL, "order_id": e["order_id"],
                         "reference": e["booking_reference"], "route": e["route"],
                         "amount": -fee, "currency": e["currency"]})

    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows[:limit]


def wallet_totals(rows):
    """Charged, recovered and fees — derived from the same rows the table
    shows, so the summary can never disagree with the list under it."""
    real = [r for r in rows if not r["simulated"]]
    sim = [r for r in rows if r["simulated"]]
    charged = sum(-r["amount"] for r in real if r["kind"] == "charge")
    recovered = sum(r["amount"] for r in real if r["kind"] == "recovery")
    fees = sum(-r["amount"] for r in real if r["kind"] == "fee")
    return {"charged": charged, "recovered": recovered, "fees": fees,
            "net_back": recovered - fees,
            # kept apart on purpose — a simulated recovery is not money
            "sim_recovered": sum(r["amount"] for r in sim if r["kind"] == "recovery"),
            "sim_fees": sum(-r["amount"] for r in sim if r["kind"] == "fee"),
            "has_simulated": bool(sim),
            "currency": rows[0]["currency"] if rows else "USD"}


def order_for_offer(account_id, offer_id):
    """The order a given offer already produced, if any."""
    return _to_record(q("""SELECT * FROM orders
                            WHERE account_id = %s AND offer_id = %s
                            ORDER BY created_at LIMIT 1""",
                        (account_id, offer_id), fetch="one"))


def account_commission_rate(account_id):
    """The account's real rate — never the hardcoded 0.25 module constant.
    Falls back to 0.25 only if the account itself can't be found, which
    should not happen in practice."""
    row = q("SELECT commission_rate FROM accounts WHERE id = %s",
           (account_id,), fetch="one")
    return row["commission_rate"] if row else Decimal("0.25")


# ---------------------------------------------------------------------------
# travel policy
# ---------------------------------------------------------------------------

def policy_rules_active(account_id):
    return q("""SELECT id, rule_type, scope, value, enforcement
                FROM policy_rules WHERE account_id = %s AND active""",
             (account_id,), fetch="all")


# ---------------------------------------------------------------------------
# booking requests — approval lifecycle, pre-purchase
# ---------------------------------------------------------------------------

def booking_request_create(account_id, *, requested_by, itinerary_snapshot, amount,
                           currency, policy_result, traveler_id=None, cost_center_id=None):
    return q("""INSERT INTO booking_requests
                   (account_id, traveler_id, cost_center_id, requested_by,
                    itinerary_snapshot, amount, currency, policy_result)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *""",
            (account_id, traveler_id, cost_center_id, requested_by,
             Jsonb(itinerary_snapshot), _num(amount), currency, Jsonb(policy_result)),
            fetch="one")


# ---------------------------------------------------------------------------
# airline credits — a liability register, not a log
# ---------------------------------------------------------------------------

def airline_credit_create(account_id, *, traveler_id, airline, loyalty_account_reference,
                          order_id, savings_event_id, amount_issued, currency, expires_at=None):
    return q("""INSERT INTO airline_credits
                   (account_id, traveler_id, airline, loyalty_account_reference,
                    order_id, savings_event_id, amount_issued, amount_remaining,
                    currency, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *""",
            (account_id, traveler_id, airline, loyalty_account_reference or "",
             order_id, savings_event_id, _num(amount_issued), _num(amount_issued),
             currency, expires_at), fetch="one")


def savings_event_link_credit(savings_event_id, airline_credit_id):
    """The other direction of the link airline_credit_create's own
    savings_event_id already provides — a bidirectional pointer on a
    liability register is worth the extra statement."""
    q("UPDATE savings_events SET airline_credit_id = %s WHERE id = %s",
      (airline_credit_id, savings_event_id))


# ---------------------------------------------------------------------------
# invoicing
# ---------------------------------------------------------------------------

def generate_invoice(account_id, period_start, period_end):
    """One invoice for one account over [period_start, period_end).

    Built from savings_events not yet billed (invoice_line_id IS NULL) plus
    the account's subscription_fee. 'forfeited' events are excluded by a
    positive allowlist (delivery_type IN ('refund_to_card','airline_credit'))
    rather than relied on to never carry a nonzero commission_amount —
    forfeited recoveries are never billable, no commission on value that
    was destroyed.

    Every line is a positive charge; invoice_lines.amount's own CHECK
    (amount >= 0) makes a credit line netting against a cash line
    structurally impossible, not just a convention this function follows.
    Cash refunds never appear here at all — Duffel refunds the company's
    card directly, TD's books never see that money.

    invoices.total is stored, computed once here, and frozen from that
    point on — an issued invoice must not change if a savings_event or the
    account's commission_rate is edited afterward.
    """
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT subscription_fee FROM accounts WHERE id = %s", (account_id,))
        acct = cur.fetchone()
        subscription_fee = acct["subscription_fee"] if acct else None

        cur.execute("""SELECT se.id, se.realized_savings, se.commission_amount,
                              se.commission_rate, se.delivery_type,
                              COALESCE(NULLIF(se.currency, ''), o.currency) AS currency
                         FROM savings_events se
                         JOIN orders o ON o.order_id = se.order_id
                        WHERE o.account_id = %s
                          AND se.invoice_line_id IS NULL
                          AND se.delivery_type IN ('refund_to_card', 'airline_credit')
                          AND se.created_at >= %s AND se.created_at < %s
                        FOR UPDATE OF se""",
                    (account_id, period_start, period_end))
        events = cur.fetchall()

        cur.execute("""INSERT INTO invoices
                          (account_id, period_start, period_end, issued_at, due_at, status, total)
                       VALUES (%s, %s, %s, now(), now() + interval '30 days', 'issued', 0)
                       RETURNING *""",
                    (account_id, period_start, period_end))
        invoice = cur.fetchone()

        total = Decimal("0")

        if subscription_fee:
            cur.execute("""INSERT INTO invoice_lines
                              (invoice_id, line_type, description, basis_amount, rate, amount)
                            VALUES (%s, 'subscription', 'Monthly platform fee', NULL, NULL, %s)""",
                        (invoice["id"], _num(subscription_fee)))
            total += Decimal(str(subscription_fee))

        for delivery_type, line_type in (("refund_to_card", "commission_cash"),
                                         ("airline_credit", "commission_credit")):
            matching = [e for e in events if e["delivery_type"] == delivery_type]
            amount = sum((Decimal(str(e["commission_amount"])) for e in matching), Decimal("0"))
            if amount <= 0:
                continue
            basis = sum((Decimal(str(e["realized_savings"])) for e in matching), Decimal("0"))
            rate = matching[0]["commission_rate"]
            noun = "cash recovery" if line_type == "commission_cash" else "airline-credit recovery"
            plural = "" if len(matching) == 1 else "s"
            desc = f"Commission on {len(matching)} {noun}{plural}"
            cur.execute("""INSERT INTO invoice_lines
                              (invoice_id, line_type, description, basis_amount, rate, amount)
                            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                        (invoice["id"], line_type, desc, _num(basis), _num(rate), _num(amount)))
            line_id = cur.fetchone()["id"]
            cur.execute("UPDATE savings_events SET invoice_line_id = %s WHERE id = ANY(%s)",
                        (line_id, [e["id"] for e in matching]))
            total += amount

        cur.execute("UPDATE invoices SET total = %s WHERE id = %s RETURNING *",
                    (_num(total), invoice["id"]))
        return cur.fetchone()


def invoice_lines_for(invoice_id, account_id):
    """account_id is required, not optional — same reasoning as
    audit_rows()'s order_id: this was previously safe only because its one
    caller always passed an invoice id it had just created itself, never
    one from user input. The next route that fetches an existing invoice
    by id (from a URL) would otherwise read straight across accounts.
    Joined through invoices rather than trusting a stored account_id on
    invoice_lines itself, since it doesn't carry one."""
    return q("""SELECT il.* FROM invoice_lines il
                JOIN invoices i ON i.id = il.invoice_id
               WHERE il.invoice_id = %s AND i.account_id = %s
               ORDER BY il.id""",
             (invoice_id, account_id), fetch="all")


def invoices_for_account(account_id, limit=24):
    return q("""SELECT * FROM invoices WHERE account_id = %s
               ORDER BY period_start DESC LIMIT %s""",
             (account_id, limit), fetch="all")


# ---------------------------------------------------------------------------
# cost centers
# ---------------------------------------------------------------------------

def cost_centers_for_account(account_id, active_only=False):
    clause = "AND active" if active_only else ""
    return q(f"""SELECT * FROM cost_centers WHERE account_id = %s {clause}
               ORDER BY code""", (account_id,), fetch="all")


def cost_center(cost_center_id, account_id):
    return q("SELECT * FROM cost_centers WHERE id = %s AND account_id = %s",
             (cost_center_id, account_id), fetch="one")


def cost_center_create(account_id, *, code, name, budget_amount=None, budget_period=None):
    return q("""INSERT INTO cost_centers (account_id, code, name, budget_amount, budget_period)
               VALUES (%s, %s, %s, %s, %s) RETURNING *""",
             (account_id, code, name, _num(budget_amount), budget_period or None), fetch="one")


def cost_center_update(cost_center_id, account_id, *, code, name, budget_amount,
                       budget_period, active):
    return q("""UPDATE cost_centers SET code = %s, name = %s, budget_amount = %s,
                                       budget_period = %s, active = %s
               WHERE id = %s AND account_id = %s RETURNING *""",
             (code, name, _num(budget_amount), budget_period or None, bool(active),
              cost_center_id, account_id), fetch="one")


# ---------------------------------------------------------------------------
# company profile (accounts) — the fields Phase 1 added with no write path
# ---------------------------------------------------------------------------

def account_company_fields(account_id):
    return q("""SELECT name, legal_name, employee_count, billing_address_line1,
                      billing_address_line2, billing_city, billing_state,
                      billing_postal_code, billing_country, subscription_fee, commission_rate
               FROM accounts WHERE id = %s""", (account_id,), fetch="one")


def account_company_update(account_id, *, legal_name, employee_count, billing_address_line1,
                           billing_address_line2, billing_city, billing_state,
                           billing_postal_code, billing_country):
    q("""UPDATE accounts SET legal_name = %s, employee_count = %s,
                             billing_address_line1 = %s, billing_address_line2 = %s,
                             billing_city = %s, billing_state = %s,
                             billing_postal_code = %s, billing_country = %s
        WHERE id = %s""",
      (legal_name or "", employee_count or None, billing_address_line1 or "",
       billing_address_line2 or "", billing_city or "", billing_state or "",
       billing_postal_code or "", billing_country or "", account_id))


# ---------------------------------------------------------------------------
# savings screen — recovery events and the airline-credit liability register
# ---------------------------------------------------------------------------

def savings_events_for_account(account_id, limit=200):
    return q("""SELECT se.*, o.route, o.carrier, o.booking_reference
               FROM savings_events se
               JOIN orders o ON o.order_id = se.order_id
              WHERE o.account_id = %s
              ORDER BY se.created_at DESC LIMIT %s""",
             (account_id, limit), fetch="all")


def savings_totals_for_account(account_id):
    """Cash and credit are kept apart throughout — different currencies in
    the literal sense (one is money, one is a claim on a specific airline)
    and merging them into one 'Total Saved' number was the exact mistake
    the SOW called out to avoid."""
    row = q("""SELECT
                 COALESCE(sum(realized_savings) FILTER (WHERE delivery_type = 'refund_to_card'), 0)
                     AS cash_recovered,
                 COALESCE(sum(commission_amount) FILTER (WHERE delivery_type = 'refund_to_card'), 0)
                     AS cash_fee,
                 COALESCE(sum(realized_savings) FILTER (WHERE delivery_type = 'airline_credit'), 0)
                     AS credit_recovered,
                 COALESCE(sum(commission_amount) FILTER (WHERE delivery_type = 'airline_credit'), 0)
                     AS credit_fee
               FROM savings_events se JOIN orders o ON o.order_id = se.order_id
              WHERE o.account_id = %s""", (account_id,), fetch="one")
    out = dict(row)
    out["cash_net"] = out["cash_recovered"] - out["cash_fee"]
    return out


def airline_credits_for_account(account_id):
    return q("""SELECT ac.*, o.route, o.carrier AS order_carrier
               FROM airline_credits ac
               LEFT JOIN orders o ON o.order_id = ac.order_id
              WHERE ac.account_id = %s
              ORDER BY (ac.status = 'active') DESC, ac.expires_at NULLS LAST, ac.issued_at DESC""",
             (account_id,), fetch="all")


# ---------------------------------------------------------------------------
# the difference band — real price history, never a fabricated curve
# ---------------------------------------------------------------------------

def monitored_orders_with_history(account_id):
    """Every currently-monitored order, each with its own market_best time
    series (oldest first) — the raw material for the difference band.
    Orders with no checks yet still appear, with an empty points list, so
    the caller can tell 'nothing to show' from 'not monitoring anything'."""
    orders = q("""SELECT order_id, paid, original_paid, currency, route, carrier
               FROM orders WHERE account_id = %s AND monitoring""",
             (account_id,), fetch="all")
    out = []
    for o in orders:
        rows = q("""SELECT ts, market_best FROM audit_events
                   WHERE order_id = %s AND market_best IS NOT NULL
                   ORDER BY ts""", (o["order_id"],), fetch="all")
        out.append({**o, "points": [(r["ts"], r["market_best"]) for r in rows]})
    return out


# ---------------------------------------------------------------------------
# live spend — the staging live-ticket ledger (migration 030, live_guard.py)
# ---------------------------------------------------------------------------

# One fixed key: every reservation takes the same transaction-scoped advisory
# lock, so two live bookings can't both read "under the daily cap" and both
# spend. Transaction-scoped, not session-scoped — safe through the pooler.
_LIVE_SPEND_LOCK = 7_302_030


def account_name(account_id):
    row = q("SELECT name FROM accounts WHERE id = %s", (account_id,), fetch="one")
    return row["name"] if row else None


def live_spend_today():
    """Today's (UTC) live spend: every booking and exchange top-up recorded in
    live_spend that wasn't released. Reserved-but-unsettled rows count — a
    ticket may exist even if the settle step never ran."""
    row = q("""SELECT COALESCE(sum(amount), 0) AS spent FROM live_spend
                WHERE status <> 'released' AND created_at >= date_trunc('day', now(), 'UTC')""",
            fetch="one")
    return row["spent"]


def live_spend_reserve(*, kind, account_id, amount, currency, reference="", order_id=None, check):
    """Under the advisory lock: read today's spend, call check(spent_today)
    — which raises to refuse — then record a 'reserved' row. Returns its id.
    An exception from check() rolls the transaction back; nothing is written."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LIVE_SPEND_LOCK,))
        cur.execute("""SELECT COALESCE(sum(amount), 0) AS spent FROM live_spend
                        WHERE status <> 'released' AND created_at >= date_trunc('day', now(), 'UTC')""")
        check(cur.fetchone()["spent"])
        cur.execute("""INSERT INTO live_spend (kind, account_id, order_id, amount, currency, reference)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                    (kind, account_id, order_id, _num(amount), currency, reference or ""))
        return cur.fetchone()["id"]


def live_spend_settle(ledger_id, *, order_id=None):
    q("""UPDATE live_spend SET status = 'spent', settled_at = now(),
                order_id = COALESCE(%s, order_id)
          WHERE id = %s AND status = 'reserved'""", (order_id, ledger_id))


def live_spend_release(ledger_id):
    """Only for a spend that provably never happened (Duffel refused it)."""
    q("""UPDATE live_spend SET status = 'released', settled_at = now()
          WHERE id = %s AND status = 'reserved'""", (ledger_id,))


# ---------------------------------------------------------------------------
# live search log — the staging monthly search budget (migration 031, live_search.py)
# ---------------------------------------------------------------------------

_LIVE_SEARCH_LOCK = 7_302_031


def live_searches_this_month():
    row = q("""SELECT count(*) AS n FROM live_search_log
                WHERE created_at >= date_trunc('month', now(), 'UTC')""", fetch="one")
    return row["n"]


def live_search_reserve(*, limit, source, account_id, origin, destination, departure_date, cabin, passengers):
    """Under an advisory lock: count this month's live searches and, if one
    more stays within `limit`, record it and return (log_id, count including
    it). Returns (None, count) when the budget is spent; nothing is written."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LIVE_SEARCH_LOCK,))
        cur.execute("""SELECT count(*) AS n FROM live_search_log
                        WHERE created_at >= date_trunc('month', now(), 'UTC')""")
        used = cur.fetchone()["n"]
        if used >= limit:
            return None, used
        cur.execute("""INSERT INTO live_search_log
                           (source, account_id, origin, destination, departure_date, cabin, passengers)
                       VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (source, account_id, origin or "", destination or "", departure_date or None,
                     cabin or "", passengers))
        return cur.fetchone()["id"], used + 1


def live_search_record(log_id, **fields):
    """Fill in what a logged search returned: offer_request_id, offers_returned,
    null_conditions, refetched, conditions_filled, error."""
    allowed = ("offer_request_id", "offers_returned", "null_conditions", "refetched",
               "conditions_filled", "error")
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    sets = ", ".join(f"{k} = %s" for k in updates)
    q(f"UPDATE live_search_log SET {sets} WHERE id = %s", (*updates.values(), log_id))

