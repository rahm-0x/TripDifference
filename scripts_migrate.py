"""Apply migrations/*.sql in order, over the DIRECT connection (DDL must not
go through the transaction pooler). Tracks what has run in schema_migrations."""
import os, sys, pathlib
from dotenv import load_dotenv
import psycopg

load_dotenv(".env.local"); load_dotenv(".env")

url = os.environ.get("POSTGRES_URL_NON_POOLING") or os.environ.get("DATABASE_URL_DIRECT")
if not url:
    sys.exit("POSTGRES_URL_NON_POOLING is not set")

here = pathlib.Path(__file__).parent / "migrations"
files = sorted(here.glob("*.sql"))

with psycopg.connect(url, autocommit=False) as conn:
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                         name text PRIMARY KEY,
                         applied_at timestamptz NOT NULL DEFAULT now())""")
        conn.commit()
        cur.execute("SELECT name FROM schema_migrations")
        done = {r[0] for r in cur.fetchall()}
        for f in files:
            if f.name in done:
                print(f"skip  {f.name}"); continue
            cur.execute(f.read_text())
            cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
            conn.commit()
            print(f"apply {f.name}")
print("migrations up to date")
