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


def audit_rows(limit=300, order_id=None):
    """Newest first, in the shape templates/decisions.html already renders."""
    if order_id:
        rows = q("""SELECT payload FROM audit_events WHERE order_id = %s
                    ORDER BY ts DESC, id DESC LIMIT %s""",
                 (order_id, limit), fetch="all")
    else:
        rows = q("""SELECT payload FROM audit_events
                    ORDER BY ts DESC, id DESC LIMIT %s""", (limit,), fetch="all")
    return [r["payload"] for r in rows]


# ---------------------------------------------------------------------------
# orders
# ---------------------------------------------------------------------------

_ORDER_COLS = ("booking_reference", "route", "itinerary", "carrier",
               "departure_date", "paid", "original_paid", "refunded",
               "currency", "monitoring", "executed", "raw", "last_decision")
_MONEY = {"paid", "original_paid", "refunded"}
_JSON = {"raw", "last_decision"}
# NOT NULL DEFAULT '' columns. We always pass every column, so a column's
# DEFAULT never fires — the coercion has to happen here instead.
_TEXT_NOT_NULL = {"booking_reference", "route", "itinerary", "carrier", "currency"}


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
            elif k == "monitoring":
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
# identity
# ---------------------------------------------------------------------------

def _hash_token(token):
    return hashlib.sha256(token.encode()).digest()


def create_account(company, email, password_hash, profile):
    """Open signup: one new account (the company) plus its first user.

    Both rows or neither — a user without an account has nothing to own.
    """
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name) VALUES (%s) RETURNING id",
                    (company,))
        account_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO users (account_id, email, password_hash,
                                  given_name, family_name, title, born_on,
                                  gender, phone_number)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (account_id, email, password_hash,
             profile.get("given_name", ""), profile.get("family_name", ""),
             profile.get("title", ""), profile.get("born_on") or None,
             profile.get("gender", ""), profile.get("phone_number", "")))
        return cur.fetchone()


def user_by_email(email):
    return q("SELECT * FROM users WHERE email = %s", (email,), fetch="one")


def email_taken(email):
    return q("SELECT 1 FROM users WHERE email = %s", (email,), fetch="one") is not None


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
# travellers (saved passenger profiles)
# ---------------------------------------------------------------------------

TRAVELER_FIELDS = ("title", "given_name", "family_name", "born_on", "gender",
                   "email", "phone_number")


def _traveler(row):
    if row is None:
        return None
    t = dict(row)
    t["born_on"] = t["born_on"].isoformat() if t.get("born_on") else ""
    t["id"] = str(t["id"])        # so the picker can serialise these to JSON
    return t


def travelers(account_id):
    rows = q("""SELECT * FROM travelers WHERE account_id = %s
                ORDER BY family_name, given_name""", (account_id,), fetch="all")
    return [_traveler(r) for r in rows]


def traveler(traveler_id, account_id):
    return _traveler(q("SELECT * FROM travelers WHERE id = %s AND account_id = %s",
                       (traveler_id, account_id), fetch="one"))


def traveler_save(data, account_id, traveler_id=None):
    vals = [data.get(f) or None if f == "born_on" else (data.get(f) or "")
            for f in TRAVELER_FIELDS]
    if traveler_id:
        sets = ", ".join(f"{f} = %s" for f in TRAVELER_FIELDS)
        return _traveler(q(f"""UPDATE travelers SET {sets}, updated_at = now()
                               WHERE id = %s AND account_id = %s RETURNING *""",
                           (*vals, traveler_id, account_id), fetch="one"))
    cols = ", ".join(TRAVELER_FIELDS)
    holders = ", ".join(["%s"] * len(TRAVELER_FIELDS))
    return _traveler(q(f"""INSERT INTO travelers (account_id, {cols})
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
    apart — a dashboard claiming 184 travellers over a roster of 8 is the
    failure mode this exists to prevent.
    """
    row = q("""SELECT
                 count(*)                                        AS bookings,
                 count(*) FILTER (WHERE monitoring)               AS monitoring,
                 count(*) FILTER (WHERE departure_date >= current_date)
                                                                  AS upcoming,
                 count(*) FILTER (WHERE refunded IS NOT NULL)     AS rebooked,
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
    return out
