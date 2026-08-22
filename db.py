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

SESSION_TTL = timedelta(days=14)

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


def audit_rows(limit=300, order_id=None):
    """Newest first. Ops-wide (no account scoping) — see audit_rows_for_account
    for the customer-facing, account-scoped Activity Timeline."""
    if order_id:
        rows = q(f"""SELECT {_AUDIT_COLS} FROM audit_events WHERE order_id = %s
                     ORDER BY ts DESC, id DESC LIMIT %s""",
                 (order_id, limit), fetch="all")
    else:
        rows = q(f"""SELECT {_AUDIT_COLS} FROM audit_events
                     ORDER BY ts DESC, id DESC LIMIT %s""", (limit,), fetch="all")
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


def price_history_by_flight(account_id, query, limit_orders=8):
    """Every one of this account's own orders matching a flight-number/
    carrier/route search, each with its own market_best price-check
    history — the only real historical-price source that exists today.

    A shared, cross-account, flight-number-keyed table (so a brand-new
    booking could show history from *other* customers' watches of the
    same flight) was discussed and deliberately deferred — see
    docs/bolt-on-pivot.md. This only ever shows the signed-in account's
    own monitoring history, never fabricated or borrowed data.
    """
    like = f"%{query}%"
    orders = q("""SELECT order_id, booking_reference, route, itinerary, carrier,
                        departure_date, paid, currency
                    FROM orders
                   WHERE account_id = %s
                     AND (itinerary ILIKE %s OR carrier ILIKE %s OR route ILIKE %s)
                   ORDER BY departure_date DESC NULLS LAST
                   LIMIT %s""",
             (account_id, like, like, like, limit_orders), fetch="all")

    # Matched orders are kept even with zero price checks — the caller needs
    # to tell "nothing matched this search" apart from "matched, but
    # monitoring hasn't produced a check yet," which collapsing empty
    # series here would otherwise hide.
    series = []
    for o in orders:
        rows = q("""SELECT ts, market_best FROM audit_events
                     WHERE order_id = %s AND market_best IS NOT NULL
                     ORDER BY ts""", (o["order_id"],), fetch="all")
        points = []
        for r in rows:
            days_out = (o["departure_date"] - r["ts"].date()).days if o["departure_date"] else None
            points.append({"ts": r["ts"].isoformat(), "days_out": days_out,
                           "price": float(r["market_best"])})
        series.append({
            "order_id": o["order_id"], "carrier": o["carrier"],
            "label": f"{o['route']} · {o['itinerary'] or o['booking_reference']}"
                    f"{' · ' + o['departure_date'].isoformat() if o['departure_date'] else ''}",
            "currency": o["currency"], "points": points,
        })
    return series


# ---------------------------------------------------------------------------
# orders
# ---------------------------------------------------------------------------

_ORDER_COLS = ("booking_reference", "route", "itinerary", "carrier",
               "departure_date", "paid", "original_paid", "refunded",
               "currency", "monitoring", "executed", "raw", "last_decision",
               "sim_scenario", "simulated", "sim_paid", "sim_refunded",
               "offer_id", "source", "fare_type", "traveler_id",
               "seg_origin", "seg_destination", "seg_flight_number", "seg_cabin")
_MONEY = {"paid", "original_paid", "refunded", "sim_paid", "sim_refunded"}
_JSON = {"raw", "last_decision", "sim_scenario"}
# NOT NULL DEFAULT '' columns. We always pass every column, so a column's
# DEFAULT never fires — the coercion has to happen here instead.
_TEXT_NOT_NULL = {"booking_reference", "route", "itinerary", "carrier", "currency",
                  "seg_origin", "seg_destination", "seg_flight_number", "seg_cabin"}
# Columns with their own DB default and a CHECK constraint, so an
# absent/blank value here must fall through to the column default rather
# than being coerced to '' like the plain text fields above.
_ORDER_DEFAULTS = {"source": "td_rebook", "fare_type": "cash"}
# Nullable uuid FK — a blank string must stay NULL, not become '' (invalid
# uuid input), unlike the plain text fields above.
_NULLABLE_UUID = {"traveler_id"}


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
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name) VALUES (%s) RETURNING id",
                    (email.split("@")[0],))
        account_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO users (account_id, email, password_hash, supabase_user_id)
               VALUES (%s, %s, %s, %s) RETURNING *""",
            (account_id, email, password_hash, supabase_user_id))
        return cur.fetchone()


def complete_profile(user_id, account_id, *, given_name, family_name, middle_name,
                     born_on, referral_source, invite_code):
    """Onboarding step 2 — the 'let's get to know you' fields. Also updates
    accounts.name from its email-local-part placeholder to the real name."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE users SET given_name = %s, family_name = %s, middle_name = %s,
                                born_on = %s, referral_source = %s, invite_code = %s
                WHERE id = %s RETURNING *""",
            (given_name, family_name, middle_name, born_on or None,
             referral_source, invite_code, user_id))
        user = cur.fetchone()
        cur.execute("UPDATE accounts SET name = %s WHERE id = %s",
                    (f"{given_name} {family_name}".strip() or user["email"], account_id))
        return user


def user_by_email(email):
    return q("SELECT * FROM users WHERE email = %s", (email,), fetch="one")


def user_by_supabase_id(supabase_user_id):
    return q("SELECT * FROM users WHERE supabase_user_id = %s",
             (supabase_user_id,), fetch="one")


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
    """
    cols = list(TRAVELER_FIELDS) + list(TRAVELER_TEXT_PROFILE_FIELDS) + ["clear_plus", "loyalty_programs"]
    vals = []
    for f in cols:
        if f == "born_on":
            vals.append(data.get(f) or None)
        elif f == "clear_plus":
            vals.append(bool(data.get(f)))
        elif f == "loyalty_programs":
            vals.append(Jsonb(data.get(f) or []))
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
    """
    row = q("""SELECT
                 count(*)                                        AS bookings,
                 count(*) FILTER (WHERE monitoring)               AS monitoring,
                 count(*) FILTER (WHERE departure_date >= current_date)
                                                                  AS upcoming,
                 count(*) FILTER (WHERE refunded IS NOT NULL)     AS rebooked,
                 count(*) FILTER (WHERE simulated)                 AS simulated,
                 COALESCE(sum(sim_refunded) FILTER (WHERE simulated), 0) AS sim_recovered,
                 COALESCE(sum(paid), 0)                           AS spend,
                 COALESCE(sum(refunded), 0)                       AS recovered,
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
    """
    rows = q("""SELECT COALESCE(NULLIF(carrier, ''), 'Unknown') AS carrier,
                       sum(paid)               AS spend,
                       COALESCE(sum(refunded), 0) AS saved,
                       count(*)                AS bookings
                  FROM orders
                 WHERE account_id = %s AND paid IS NOT NULL
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
      recoveries  one per *executed* exchange, from the audit trail, split into
                  the amount recovered and our share of it

    Only execution = 'executed' counts. A simulated or blocked exchange is a
    decision the engine reached, not money that moved, and putting one in a
    ledger would be a lie about the balance.
    """
    charges = q("""SELECT created_at AS ts, order_id, booking_reference, route,
                          paid AS amount, currency, source
                     FROM orders
                    WHERE account_id = %s AND paid IS NOT NULL""",
                (account_id,), fetch="all")

    # Simulated recoveries are included so the flow can be demonstrated, but
    # they arrive flagged and every view that shows them says so. They are
    # never added into a real total.
    # Executed rows are history and always show. Simulated ones reflect current
    # state instead: only while the order is still flagged, and only the most
    # recent run — the audit trail keeps every attempt, but a ledger showing
    # five superseded what-ifs, and still showing them after a reset, is noise.
    execs = q("""SELECT * FROM (
                   SELECT DISTINCT ON (a.order_id, a.execution)
                          a.ts, a.order_id, o.booking_reference, o.route,
                          a.execution,
                          a.payload->>'recovered'   AS recovered,
                          a.payload->>'service_fee' AS service_fee,
                          COALESCE(NULLIF(a.currency, ''), o.currency) AS currency
                     FROM audit_events a
                     JOIN orders o ON o.order_id = a.order_id
                    WHERE o.account_id = %s
                      AND a.kind = 'execution'
                      AND a.payload->>'recovered' IS NOT NULL
                      AND (a.execution = 'executed'
                           OR (a.execution = 'blocked_simulated' AND o.simulated))
                    ORDER BY a.order_id, a.execution, a.ts DESC) x
                 ORDER BY x.ts DESC""",
              (account_id,), fetch="all")

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
    for e in execs:
        recovered = Decimal(e["recovered"])
        fee = Decimal(e["service_fee"] or 0)
        sim = e["execution"] != "executed"
        rows.append({"ts": e["ts"], "kind": "recovery", "simulated": sim,
                     "label": "Fare drop recovered", "order_id": e["order_id"],
                     "reference": e["booking_reference"], "route": e["route"],
                     "amount": recovered, "currency": e["currency"]})
        if fee:
            rows.append({"ts": e["ts"], "kind": "fee", "simulated": sim,
                         "label": "Service fee (25% of recovery)",
                         "order_id": e["order_id"],
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
