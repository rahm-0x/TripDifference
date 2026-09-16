#!/usr/bin/env python3
"""
One-off data cleanup (not a migration — no schema change) for rows the test
suite wrote on 2026-09-16, before conftest.py's database guard existed:

  production (uixgspesihupgcqzrzfw)
    - audit_events rows with ts in 2026-09-16 13:44:00Z–14:20:59Z (exactly 63)
    - carrier T1: its observations (all denials, order link NULL) and its
      carrier_change_capability row
    Aborts unless orders and accounts are both empty.

  staging (bcqzwnoifwkuimrrdysy)
    - carrier T1 only: 8 observations (denials, order link NULL) and its row
    audit_events is left alone.

Default is a dry run: a read-only transaction that checks every precondition
and prints what would be deleted, per table. --apply does the same checks,
then deletes inside one transaction. Append-only triggers on a table being
purged (audit_events_no_mutate) are disabled only inside that transaction and
re-enabled — and verified enabled — before commit. Every delete's row count
must equal the count the checks found, or the whole transaction rolls back.

Connection strings come straight from the files, not the shell environment:
production from .env.local (POSTGRES_URL_NON_POOLING), staging from
.env.staging.local (STAGING_POSTGRES_URL_NON_POOLING). Refuses if the two
reach the same database.

Usage:
    python scripts/purge_preguard_junk.py --db production|staging [--apply]
"""

import argparse
import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db_identity import same_database  # noqa: E402

AUDIT_WINDOW = ("2026-09-16T13:44:00Z", "2026-09-16T14:21:00Z")  # [start, end) = 13:44:00–14:20:59
EXPECTED_AUDIT_ROWS = 63
JUNK_CARRIER = "T1"
EXPECTED_T1_OBSERVATIONS = {"production": 24, "staging": 8}


class Abort(Exception):
    pass


def _urls():
    production = (dotenv_values(ROOT / ".env.local").get("POSTGRES_URL_NON_POOLING") or "").strip()
    staging = (dotenv_values(ROOT / ".env.staging.local").get("STAGING_POSTGRES_URL_NON_POOLING") or "").strip()
    if not production:
        raise Abort("POSTGRES_URL_NON_POOLING missing from .env.local")
    if not staging:
        raise Abort("STAGING_POSTGRES_URL_NON_POOLING missing from .env.staging.local")
    if same_database(production, staging):
        raise Abort("production and staging URLs reach the same database")
    return {"production": production, "staging": staging}


def _one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def survey(conn, db_name):
    """Checks every precondition and returns the plan: [(table, where-sql,
    params, expected_rows, description)]. Read-only."""
    plan = []

    if db_name == "production":
        orders = _one(conn, "SELECT count(*) FROM orders")[0]
        accounts = _one(conn, "SELECT count(*) FROM accounts")[0]
        print(f"precondition: orders={orders} accounts={accounts}", end=" ")
        if orders or accounts:
            raise Abort("production has orders or accounts — refusing to touch it")
        print("OK (both empty)")

        in_window = _one(conn, "SELECT count(*) FROM audit_events WHERE ts >= %s AND ts < %s", AUDIT_WINDOW)[0]
        if in_window != EXPECTED_AUDIT_ROWS:
            raise Abort(f"audit_events in window = {in_window}, expected exactly {EXPECTED_AUDIT_ROWS}")
        plan.append(("audit_events", "ts >= %s AND ts < %s", AUDIT_WINDOW, in_window,
                     "ts 2026-09-16 13:44:00Z–14:20:59Z"))

    expected_obs = EXPECTED_T1_OBSERVATIONS[db_name]
    total, null_link_denials = _one(conn, """
        SELECT count(*), count(*) FILTER (WHERE order_id IS NULL AND NOT change_allowed)
          FROM carrier_change_observations WHERE carrier_iata = %s""", (JUNK_CARRIER,))
    if total != null_link_denials:
        raise Abort(f"{total - null_link_denials} {JUNK_CARRIER} observation(s) are linked to an order or "
                    "are not denials — not junk this script recognises")
    if null_link_denials != expected_obs:
        raise Abort(f"{JUNK_CARRIER} observations = {null_link_denials}, expected exactly {expected_obs}")
    plan.append(("carrier_change_observations",
                 "carrier_iata = %s AND order_id IS NULL AND NOT change_allowed", (JUNK_CARRIER,),
                 null_link_denials, f"carrier {JUNK_CARRIER}, order link NULL, all denials"))

    row = _one(conn, """SELECT change_confirmed_count, change_denied_count
                          FROM carrier_change_capability WHERE carrier_iata = %s""", (JUNK_CARRIER,))
    if row is None:
        raise Abort(f"no carrier_change_capability row for {JUNK_CARRIER}")
    confirmed, denied = row
    if confirmed != 0 or denied != expected_obs:
        raise Abort(f"{JUNK_CARRIER} row has confirmed={confirmed} denied={denied}, "
                    f"expected confirmed=0 denied={expected_obs}")
    plan.append(("carrier_change_capability",
                 "carrier_iata = %s AND change_confirmed_count = 0 AND change_denied_count = %s",
                 (JUNK_CARRIER, expected_obs), 1, f"carrier {JUNK_CARRIER}: confirmed 0, denied {denied}"))
    return plan


def append_only_triggers(conn, tables):
    """Enabled user triggers whose function name ends in _immutable, on the
    tables being purged: [(table, trigger)]."""
    return [tuple(r) for r in conn.execute("""
        SELECT t.tgrelid::regclass::text, t.tgname
          FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
         WHERE NOT t.tgisinternal AND p.proname LIKE '%%\\_immutable'
           AND t.tgrelid::regclass::text = ANY(%s)
         ORDER BY 1, 2""", (list(tables),)).fetchall()]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, choices=["production", "staging"])
    ap.add_argument("--apply", action="store_true", help="delete (default is a read-only dry run)")
    args = ap.parse_args()

    try:
        url = _urls()[args.db]
        with psycopg.connect(url, autocommit=False, connect_timeout=15) as conn:
            info = conn.info
            print(f"target: {args.db}  {info.user}@{info.host}:{info.port}/{info.dbname}")
            print(f"mode:   {'APPLY' if args.apply else 'DRY RUN (read-only transaction)'}")

            if not args.apply:
                conn.read_only = True
                plan = survey(conn, args.db)
                triggers = append_only_triggers(conn, [t for t, *_ in plan])
                conn.rollback()
                print("would delete:")
                for table, _, _, n, desc in plan:
                    print(f"  {table:30} {n:>4}   {desc}")
                print("append-only triggers --apply would disable/re-enable inside its transaction: "
                      + (", ".join(f"{t}.{g}" for t, g in triggers) or "none"))
                print("DRY RUN — nothing deleted.")
                return

            with conn.transaction():
                plan = survey(conn, args.db)
                triggers = append_only_triggers(conn, [t for t, *_ in plan])
                for table, trigger in triggers:
                    conn.execute(f'ALTER TABLE "{table}" DISABLE TRIGGER "{trigger}"')
                    print(f"disabled trigger {table}.{trigger} (this transaction only)")

                for table, where, params, expected, desc in plan:
                    deleted = conn.execute(f'DELETE FROM "{table}" WHERE {where}', params).rowcount
                    if deleted != expected:
                        raise Abort(f"{table}: deleted {deleted}, expected {expected} — rolling back")
                    print(f"  deleted {table:30} {deleted:>4}   {desc}")

                for table, trigger in triggers:
                    conn.execute(f'ALTER TABLE "{table}" ENABLE TRIGGER "{trigger}"')
                    state = _one(conn, """SELECT tgenabled FROM pg_trigger
                                           WHERE tgrelid = %s::regclass AND tgname = %s""", (table, trigger))[0]
                    if state != "O":
                        raise Abort(f"trigger {table}.{trigger} not re-enabled (tgenabled={state!r}) — rolling back")
                    print(f"re-enabled trigger {table}.{trigger} (verified)")

                remaining = [(t, _one(conn, f'SELECT count(*) FROM "{t}" WHERE {w}', p)[0]) for t, w, p, *_ in plan]
                if any(n for _, n in remaining):
                    raise Abort(f"rows still match after delete: {remaining} — rolling back")
            print("COMMITTED.")
    except Abort as exc:
        sys.exit(f"ABORTED: {exc}")


if __name__ == "__main__":
    main()
