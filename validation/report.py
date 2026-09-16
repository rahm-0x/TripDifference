"""
Summarizes validation/observe.py's reshop_observations table.

Read-only: no Duffel calls, no writes, nothing to gate behind OBSERVE_ONLY.
For the whole table, and broken out by carrier and by days-to-departure
bucket, prints:

  - total observations
  - how many reached gate 9 (the profitability-floor check — reachable only
    once a matched change offer priced out with change_total_amount < 0)
  - count and percentage of observations with change_total_amount < 0
  - min / median / max of change_total_amount, across every observation
    that was actually priced (gate >= 8), positive or negative — a +40 quote
    that a later gate skipped is still a real data point

The gate-9 count and the change_total<0 count are computed two independent
ways (the stored `gate` column, and a direct threshold on
change_total_amount) specifically so they can be cross-checked against each
other — they should always match; a mismatch would mean the gate
attribution in observe.py has drifted from engine.py's actual gate order.

Usage:
    python validation/report.py
"""

import statistics
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db

GATE_FLOOR = 9  # engine.evaluate()'s profitability-floor check — see observe.py's GATE_BY_REASON

DAY_BUCKETS = (
    (0, 7, "0-7 days"),
    (8, 14, "8-14 days"),
    (15, 30, "15-30 days"),
    (31, 60, "31-60 days"),
    (61, 90, "61-90 days"),
    (91, None, "91+ days"),
)


def _day_bucket(days):
    if days is None:
        return "unknown"
    if days < 0:
        return "unknown (past departure)"
    for lo, hi, label in DAY_BUCKETS:
        if days >= lo and (hi is None or days <= hi):
            return label
    return "unknown"


def _fetch_all():
    """Every observation except those on test-suite accounts
    (db.TEST_ACCOUNT_NAME). An observation whose order row is gone has no
    account to judge by and is kept."""
    return db.q("""SELECT ro.* FROM reshop_observations ro
                     LEFT JOIN orders o ON o.order_id = ro.order_id
                     LEFT JOIN accounts a ON a.id = o.account_id
                    WHERE a.name IS DISTINCT FROM %s""",
                (db.TEST_ACCOUNT_NAME,), fetch="all")


def _pct(part, whole):
    return (100.0 * part / whole) if whole else 0.0


def _print_bucket(label, rows):
    total = len(rows)
    print(f"\n{label} — {total} observation(s)")
    if total == 0:
        print("  (no observations)")
        return

    reached_floor = [r for r in rows if r["gate"] >= GATE_FLOOR]
    negative = [r for r in rows if r["change_total_amount"] is not None and r["change_total_amount"] < 0]
    priced = [r for r in rows if r["change_total_amount"] is not None]

    print(f"  total observations:        {total}")

    if not reached_floor:
        print(f"  reached gate {GATE_FLOOR} (profitability floor): 0 — no observation ever priced "
              f"a matching identical-itinerary change offer below $0")
    else:
        print(f"  reached gate {GATE_FLOOR} (profitability floor): "
              f"{len(reached_floor)} ({_pct(len(reached_floor), total):.1f}%)")

    if not negative:
        print("  change_total_amount < 0:   0 — no negative quote observed")
    else:
        print(f"  change_total_amount < 0:   {len(negative)} ({_pct(len(negative), total):.1f}%)")

    if len(reached_floor) != len(negative):
        print(f"  ** MISMATCH: gate>={GATE_FLOOR} count ({len(reached_floor)}) != "
              f"change_total<0 count ({len(negative)}) — gate attribution may be wrong")

    if not priced:
        print("  change_total_amount stats: no observation was ever priced (nothing reached gate 8)")
    else:
        amounts = sorted(r["change_total_amount"] for r in priced)
        print(f"  change_total_amount stats (n={len(amounts)}, all priced quotes, "
              f"positive and negative): min={amounts[0]} median={statistics.median(amounts)} "
              f"max={amounts[-1]}")


def main():
    rows = _fetch_all()

    _print_bucket("ALL OBSERVATIONS", rows)

    print("\n=== by carrier ===")
    carriers = sorted({r["carrier"] or "(unknown)" for r in rows})
    for carrier in carriers:
        _print_bucket(f"carrier {carrier}", [r for r in rows if (r["carrier"] or "(unknown)") == carrier])

    print("\n=== by days-to-departure bucket ===")
    bucketed = {}
    for r in rows:
        bucketed.setdefault(_day_bucket(r["days_to_departure"]), []).append(r)
    order = [label for _, _, label in DAY_BUCKETS] + ["unknown (past departure)", "unknown"]
    for label in order:
        if label in bucketed:
            _print_bucket(label, bucketed[label])


if __name__ == "__main__":
    main()
