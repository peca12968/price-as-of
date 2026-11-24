"""Compare the effective price schedule for one SKU as known at two cutoffs."""
import argparse
import csv
import json
from pathlib import Path

import duckdb

from audit import ROOT, identifier, load_history, load_revisions, price_change, price_state, timestamp


SCHEDULE_QUESTIONS = """
    WITH inputs AS (
        SELECT ?::VARCHAR AS sku, ?::TIMESTAMP AS starts, ?::TIMESTAMP AS stops,
               ?::TIMESTAMP AS from_cutoff, ?::TIMESTAMP AS cutoff
    ), cutoffs AS (
        SELECT 'before' AS perspective, from_cutoff AS known_at FROM inputs
        UNION ALL
        SELECT 'after', cutoff FROM inputs
    ), visible AS (
        SELECT r.effective_at, r.action
        FROM revisions r CROSS JOIN cutoffs c
        WHERE r.recorded_at <= c.known_at
        QUALIFY row_number() OVER (PARTITION BY c.perspective, r.change_id ORDER BY r.revision DESC) = 1
    ), boundaries AS (
        SELECT starts AS placed_at FROM inputs
        UNION
        SELECT effective_at FROM visible, inputs
        WHERE action = 'set' AND effective_at > starts AND effective_at < stops
    )
    SELECT b.placed_at::VARCHAR AS order_id, i.sku, b.placed_at, 1::BIGINT AS quantity, c.*
    FROM boundaries b CROSS JOIN inputs i CROSS JOIN cutoffs c
"""


def compare_schedule(revisions, sku, effective_from, effective_until, from_cutoff, cutoff):
    sku = identifier(sku)
    if effective_from >= effective_until:
        raise ValueError("effective start must be before the exclusive end")
    if from_cutoff > cutoff:
        raise ValueError("comparison start must be at or before the audit cutoff")

    revisions = [row for row in revisions if row[2] == sku and row[5] <= cutoff]
    with duckdb.connect(":memory:") as db:
        load_revisions(db, revisions)
        db.execute("CREATE TEMP TABLE price_questions AS " + SCHEDULE_QUESTIONS,
                   [sku, effective_from, effective_until, from_cutoff, cutoff])
        rows = db.execute((ROOT / "prices.sql").read_text(encoding="utf-8")).fetchall()

    by_start = {}
    for _, _, start, _, view, status, unit, sources, _ in rows:
        by_start.setdefault(start, {})[view] = price_state(status, unit, sources)

    def utc(value):
        return value.isoformat() + "Z"

    segments = []
    points = [*sorted(by_start), effective_until]
    for start, end in zip(points, points[1:]):
        before, after = (by_start[start][view] for view in ("before", "after"))
        if segments and segments[-1]["before"] == before and segments[-1]["after"] == after:
            segments[-1]["effective_until"] = utc(end)
            continue
        old, new = before["unit_cents"], after["unit_cents"]
        segments.append({"effective_from": utc(start), "effective_until": utc(end),
                         "change": price_change(before, after),
                         "unit_delta_cents": new - old if old is not None and new is not None else None,
                         "before": before, "after": after})
    return {"sku": sku, "effective_from": utc(effective_from), "effective_until": utc(effective_until),
            "from_cutoff": utc(from_cutoff), "cutoff": utc(cutoff), "segments": segments}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revisions", type=Path)
    parser.add_argument("--sku", required=True)
    parser.add_argument("--effective-from", required=True)
    parser.add_argument("--effective-until", required=True)
    parser.add_argument("--from-as-of", required=True)
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args()
    try:
        result = compare_schedule(load_history(args.revisions), args.sku, timestamp(args.effective_from),
                                  timestamp(args.effective_until), timestamp(args.from_as_of), timestamp(args.as_of))
    except (ValueError, OverflowError, OSError, csv.Error, duckdb.Error) as exc:
        parser.exit(2, f"schedule failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
