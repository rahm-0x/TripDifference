#!/usr/bin/env python3
"""
Batch eligibility scan: one Duffel offer request per route, every offer
evaluated exactly as /eligibility does (offer_eligibility.search — verdicts
from eligibility.assess() only), written to CSV, with a summary by carrier and
fare brand.

  routes CSV   header row with origin, destination, date (YYYY-MM-DD); optional
               cabin (default economy) and passengers (default 1)
  output CSV   route, date, carrier, fare brand, price, change allowed,
               change penalty, refund allowed, verdict, conditions source

Every route is a live offer request when the token is live, so each counts
against STAGING_MAX_MONTHLY_SEARCHES and is recorded in live_search_log. When
the budget runs out the scan stops, keeps what it has, and says so.

Staging only. Loads .env and .env.staging.local (not .env.local, which holds
production's database), uses STAGING_POSTGRES_URL as POSTGRES_URL unless one is
already set, and runs the same startup checks as the app: APP_ENV=staging must
be on the staging database, live flags and token must be allowed.

Usage:
    APP_ENV=staging DUFFEL_LIVE_SEARCH_ENABLED=true DUFFEL_TOKEN=duffel_live_... \\
    python scripts/eligibility_scan.py --routes routes.csv --out scan.csv
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

COLUMNS = ("route", "date", "carrier", "fare brand", "price", "change allowed", "change penalty",
           "refund allowed", "verdict", "conditions source")


def read_routes(path):
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        missing = {"origin", "destination", "date"} - {(c or "").strip().lower() for c in reader.fieldnames or []}
        if missing:
            raise SystemExit(f"{path}: missing column(s) {', '.join(sorted(missing))}")
        routes = []
        for n, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            try:
                datetime.strptime(row["date"], "%Y-%m-%d")
            except ValueError:
                raise SystemExit(f"{path}:{n}: date must be YYYY-MM-DD, got {row['date']!r}")
            routes.append({"origin": row["origin"].upper(), "destination": row["destination"].upper(),
                           "date": row["date"], "cabin": row.get("cabin") or "economy",
                           "passengers": int(row.get("passengers") or 1)})
    return routes


def _allowed(value):
    return "" if value is None else ("yes" if value else "no")


def _penalty(c):
    return "" if c["penalty"] is None else f"{c['penalty']} {c['currency']}".strip()


def csv_rows(route, rows):
    return [{
        "route": f"{route['origin']}-{route['destination']}", "date": route["date"],
        "carrier": r["carrier_iata"] or r["carrier"], "fare brand": r["fare_brand"],
        "price": f"{r['price']} {r['currency']}", "change allowed": _allowed(r["change"]["allowed"]),
        "change penalty": _penalty(r["change"]), "refund allowed": _allowed(r["refund"]["allowed"]),
        "verdict": r["verdict"], "conditions source": r["conditions_source"],
    } for r in rows]


def summarize(scan_rows):
    """[(carrier, fare brand, offers, % eligible, % unknown)] — eligible counts
    both Eligible and Eligible with penalty."""
    import offer_eligibility

    groups = defaultdict(list)
    for r in scan_rows:
        groups[(r["carrier"], r["fare brand"] or "(none)")].append(r["verdict"])
    out = []
    for (carrier, brand), verdicts in sorted(groups.items()):
        n = len(verdicts)
        eligible = sum(v in (offer_eligibility.ELIGIBLE, offer_eligibility.ELIGIBLE_WITH_PENALTY) for v in verdicts)
        unknown = sum(v == offer_eligibility.UNKNOWN for v in verdicts)
        out.append((carrier, brand, n, 100.0 * eligible / n, 100.0 * unknown / n))
    return out


def format_summary(summary):
    headers = ("carrier", "fare brand", "offers", "% eligible", "% unknown")
    body = [(c, b, str(n), f"{e:.1f}", f"{u:.1f}") for c, b, n, e, u in summary]
    widths = [max(len(h), *(len(r[i]) for r in body)) if body else len(h) for i, h in enumerate(headers)]
    line = lambda cells: "  ".join(c.rjust(widths[i]) if i >= 2 else c.ljust(widths[i]) for i, c in enumerate(cells))
    return "\n".join([line(headers), "  ".join("-" * w for w in widths), *(line(r) for r in body)])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--routes", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.staging.local")
    if not os.environ.get("POSTGRES_URL") and os.environ.get("STAGING_POSTGRES_URL"):
        os.environ["POSTGRES_URL"] = os.environ["STAGING_POSTGRES_URL"]

    import config
    import db
    import duffel_http
    import live_search
    import offer_eligibility

    if config.APP_ENV != "staging":
        raise SystemExit(f"Refusing to run: the eligibility scan is a staging tool (APP_ENV={config.APP_ENV}).")
    duffel_http.startup_check()
    db.startup_check()
    mode = duffel_http.mode()

    routes = read_routes(args.routes)
    budget = live_search.budget()
    print(f"{len(routes)} route(s), Duffel {mode} token; searches this month: {budget['used']} / {budget['limit']}")

    scanned, stopped = [], None
    for route in routes:
        label = f"{route['origin']}-{route['destination']} {route['date']}"
        try:
            result = offer_eligibility.search(
                origin=route["origin"], destination=route["destination"], departure_date=route["date"],
                cabin=route["cabin"], passengers=route["passengers"], source="eligibility_scan")
        except live_search.SearchBudgetExceeded as exc:
            stopped = str(exc)
            print(f"  {label}: STOPPED — {exc}")
            break
        except (duffel_http.DuffelError, RuntimeError) as exc:
            print(f"  {label}: FAILED — {exc}")
            continue
        scanned.extend(csv_rows(route, result["rows"]))
        s = result["stats"]
        print(f"  {label}: {result['offers_returned']} offers, {s['null_conditions']} null conditions "
              f"({s['refetched']} fetched again, {s['conditions_filled']} filled in)")

    with open(args.out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(scanned)
    print(f"\nwrote {len(scanned)} offer row(s) to {args.out}\n")
    print(format_summary(summarize(scanned)) if scanned else "No offers scanned.")
    budget = live_search.budget()
    print(f"\nsearches this month: {budget['used']} / {budget['limit']}")
    if stopped:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
