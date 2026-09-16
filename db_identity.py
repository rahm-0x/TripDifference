"""
Which database a Postgres URL actually reaches — for guards that must refuse
to point tests or migrations at production.

String equality isn't enough, and neither is host + database name. On
Supabase, every project in a region shares one pooler host
(aws-0-<region>.pooler.supabase.com) and every project's database is named
`postgres`; the tenant is carried in the username (`postgres.<project-ref>`).
The same project is also reachable at its direct host (db.<ref>.supabase.co)
with a plain `postgres` user. So identity is:

  - Supabase: (project ref, database name), read off either URL form
  - anything else: (host, database name) — port ignored, deliberately, so two
    URLs on one host are treated as the same database (the stricter reading)
"""

from urllib.parse import unquote, urlsplit


def identity(url):
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    user = unquote(parts.username or "")
    dbname = parts.path.lstrip("/") or "postgres"
    if host.startswith("db.") and host.endswith(".supabase.co"):
        return ("supabase", host[len("db."):-len(".supabase.co")], dbname)
    if host.endswith(".pooler.supabase.com") and "." in user:
        return ("supabase", user.split(".", 1)[1], dbname)
    return ("postgres", host, dbname)


def same_database(a, b):
    """True when both URLs are set and reach the same database."""
    if not (a or "").strip() or not (b or "").strip():
        return False
    return a.strip() == b.strip() or identity(a) == identity(b)
