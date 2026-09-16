"""
Summarizes validation/observe.py's reshop_observations table.

Read-only: no Duffel calls, no writes, nothing to gate behind OBSERVE_ONLY.
Prints one table, a row per (carrier, days-to-departure bucket) plus an ALL
row:

  carrier | days to departure | quotes observed | change_total < 0 | median change_total

A quote is an observation that was actually priced (change_total_amount not
NULL, i.e. it reached gate 8); the median is over those quotes, positive and
negative alike — a +40 quote that a later gate skipped is still a real data
point. Observations on test-suite accounts are excluded.

Below the table: an explicit line when no quote has ever come back below $0,
and a cross-check that the gate>=9 count (the stored `gate` column) equals
the change_total<0 count (a direct threshold) — a mismatch would mean the
gate attribution in observe.py has drifted from engine.py's gate order.

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
    (db.TEST_ACCOUNT_NAME). An observation with no order link (possible only
    for rows written before migration 030 made the link ON DELETE RESTRICT)
    has no account to judge by and is kept."""
    return db.q("""SELECT ro.* FROM reshop_observations ro
                     LEFT JOIN orders o ON o.order_id = ro.order_id
                     LEFT JOIN accounts a ON a.id = o.account_id
                    WHERE a.name IS DISTINCT FROM %s""",
                (db.TEST_ACCOUNT_NAME,), fetch="all")


def summarize(rows):
    """One row per (carrier, days-to-departure bucket), sorted by carrier then
    bucket order, plus an ALL row last. A quote is an observation that was
    actually priced (change_total_amount not NULL); median_change_total is
    over those quotes, positive and negative alike."""
    bucket_order = [label for _, _, label in DAY_BUCKETS] + ["unknown (past departure)", "unknown"]
    groups = {}
    for r in rows:
        key = (r["carrier"] or "(unknown)", _day_bucket(r["days_to_departure"]))
        groups.setdefault(key, []).append(r)

    def line(carrier, bucket, members):
        quotes = [Decimal(str(r["change_total_amount"])) for r in members
                  if r["change_total_amount"] is not None]
        return {
            "carrier": carrier, "bucket": bucket,
            "observations": len(members),
            "quotes": len(quotes),
            "negative": sum(1 for q in quotes if q < 0),
            "median_change_total": statistics.median(quotes) if quotes else None,
            "reached_floor": sum(1 for r in members if r["gate"] >= GATE_FLOOR),
        }

    out = [line(c, b, groups[(c, b)])
           for c, b in sorted(groups, key=lambda k: (k[0], bucket_order.index(k[1])))]
    out.append(line("ALL", "all", rows))
    return out


def format_table(summary):
    headers = ("carrier", "days to departure", "quotes observed", "change_total < 0", "median change_total")
    body = [(s["carrier"], s["bucket"], str(s["quotes"]), str(s["negative"]),
             "—" if s["median_change_total"] is None else str(s["median_change_total"]))
            for s in summary]
    widths = [max(len(h), *(len(row[i]) for row in body)) for i, h in enumerate(headers)]
    numeric = {2, 3, 4}

    def fmt(cells):
        return "  ".join(c.rjust(widths[i]) if i in numeric else c.ljust(widths[i])
                         for i, c in enumerate(cells))

    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    for i, row in enumerate(body):
        if i == len(body) - 1:
            lines.append("  ".join("-" * w for w in widths))
        lines.append(fmt(row))
    return "\n".join(lines)


def main():
    summary = summarize(_fetch_all())
    total = summary[-1]
    print(format_table(summary))
    print()
    if total["observations"] == 0:
        print("No observations recorded yet — nothing has been quoted.")
    elif total["negative"] == 0:
        print(f"No quote has come back below $0 across {total['quotes']} quote(s) — "
              f"no observation reached gate {GATE_FLOOR} (the profitability floor).")
    if total["reached_floor"] != total["negative"]:
        print(f"** MISMATCH: gate>={GATE_FLOOR} count ({total['reached_floor']}) != change_total<0 count "
              f"({total['negative']}) — gate attribution in observe.py may have drifted from engine.py")


if __name__ == "__main__":
    main()
