#!/usr/bin/env python3
"""
Apply migrations/*.sql, in filename order, to the staging database at
STAGING_POSTGRES_URL_NON_POOLING (DDL must not go through the transaction
pooler — use the session pooler on 5432 or the direct host). Tests run
against this database too.

Refuses to run if that URL reaches the same database as POSTGRES_URL,
POSTGRES_URL_NON_POOLING, DATABASE_URL or DATABASE_URL_DIRECT (by identity,
see db_identity.py), or a different database than STAGING_POSTGRES_URL.

Tracks applied files in schema_migrations exactly as scripts_migrate.py does,
one transaction per file, so re-running is safe. Stops at the first failing
migration, rolls it back, and names it.

Reads .env.local, .env and .env.staging.local, none with override.

Usage:
    python scripts/migrate_staging.py
"""

import os
import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db_identity import same_database  # noqa: E402

load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.staging.local")


def main():
    target = os.environ.get("STAGING_POSTGRES_URL_NON_POOLING", "").strip()
    if not target:
        sys.exit("Refusing: STAGING_POSTGRES_URL_NON_POOLING is not set.")

    # Production URLs straight from the files too, in case the shell has
    # already been pointed somewhere else.
    file_values = {**dotenv_values(ROOT / ".env"), **dotenv_values(ROOT / ".env.local")}
    for name in ("POSTGRES_URL", "POSTGRES_URL_NON_POOLING", "DATABASE_URL", "DATABASE_URL_DIRECT"):
        for url in {os.environ.get(name, ""), file_values.get(name) or ""}:
            if same_database(target, url):
                sys.exit(f"Refusing: STAGING_POSTGRES_URL_NON_POOLING reaches the same database as {name}.")

    staging_pooled = os.environ.get("STAGING_POSTGRES_URL", "").strip()
    if staging_pooled and not same_database(target, staging_pooled):
        sys.exit("Refusing: STAGING_POSTGRES_URL_NON_POOLING and STAGING_POSTGRES_URL reach different databases.")

    files = sorted((ROOT / "migrations").glob("*.sql"))
    with psycopg.connect(target, autocommit=False, connect_timeout=15) as conn:
        info = conn.info
        print(f"target: {info.user}@{info.host}:{info.port}/{info.dbname}")
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                             name text PRIMARY KEY,
                             applied_at timestamptz NOT NULL DEFAULT now())""")
            conn.commit()
            cur.execute("SELECT name FROM schema_migrations")
            done = {r[0] for r in cur.fetchall()}
            applied = 0
            for f in files:
                if f.name in done:
                    print(f"skip  {f.name}")
                    continue
                try:
                    cur.execute(f.read_text())
                    cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
                    conn.commit()
                except psycopg.Error as exc:
                    conn.rollback()
                    first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
                    sys.exit(f"FAILED {f.name}: {first}\n{applied} migration(s) applied before the failure.")
                applied += 1
                print(f"apply {f.name}")
    print(f"staging database up to date ({applied} applied, {len(files) - applied} already present)")


if __name__ == "__main__":
    main()
