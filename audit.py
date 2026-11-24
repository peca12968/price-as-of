"""Reconstruct order prices as originally known and as known at a later cutoff."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

import duckdb


ROOT = Path(__file__).resolve().parent
REVISION_FIELDS = "change_id revision sku action effective_at recorded_at unit_cents".split()
ORDER_FIELDS = "order_id sku placed_at quantity".split()
TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])")

AUDIT_QUESTIONS = """
    SELECT *, 'at_order' AS perspective, placed_at AS known_at FROM orders
    UNION ALL
    SELECT *, 'at_audit', CAST(? AS TIMESTAMP) FROM orders
"""
HISTORY_QUESTIONS = """
    SELECT *, 'history' AS perspective, placed_at AS known_at FROM orders
    UNION ALL
    SELECT DISTINCT o.*, 'history', r.recorded_at
    FROM orders o JOIN revisions r ON o.sku = r.sku
    WHERE o.placed_at < r.recorded_at AND r.recorded_at <= CAST(? AS TIMESTAMP)
"""
COMPARISON_QUESTIONS = """
    SELECT *, 'before' AS perspective, CAST(? AS TIMESTAMP) AS known_at FROM orders
    UNION ALL
    SELECT *, 'after', CAST(? AS TIMESTAMP) FROM orders
"""
CHANGE_TYPES = ("unchanged", "status_changed", "unit_price_changed", "source_changed")


def timestamp(text):
    if not TIME.fullmatch(text):
        raise ValueError(f"expected timestamp with timezone and at most six fractional digits: {text!r}")
    return datetime.fromisoformat(text).astimezone(timezone.utc).replace(tzinfo=None)


def integer(text, minimum):
    if not re.fullmatch(r"[0-9]+", text) or not minimum <= int(text) <= 2**63 - 1:
        raise ValueError(f"expected an integer from {minimum} to 2^63-1: {text!r}")
    return int(text)


def identifier(text):
    if not text or text.strip() != text:
        raise ValueError("identifier must be nonempty with no surrounding whitespace")
    return text


def revision(row):
    change, version, sku, action, effective, recorded, cents = row
    if action == "withdraw" and not effective and not cents:
        effective, cents = None, None
    elif action == "set":
        effective, cents = timestamp(effective), integer(cents, 0)
    else:
        raise ValueError("expected set or a withdrawal with blank effective_at and unit_cents")
    return identifier(change), integer(version, 1), identifier(sku), action, effective, timestamp(recorded), cents


def order(row):
    key, sku, placed, qty = row
    return identifier(key), identifier(sku), timestamp(placed), integer(qty, 1)


def read_rows(path, fields, convert):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, strict=True)
        if next(reader, None) != fields:
            raise ValueError(f"{path}: expected header {','.join(fields)}")
        for row in reader:
            try:
                if len(row) != len(fields):
                    raise ValueError(f"expected {len(fields)} fields")
                yield convert(row)
            except (ValueError, OverflowError) as exc:
                raise ValueError(f"{path}, line {reader.line_num}: {exc}") from exc


def load_history(path):
    history = {}
    for row in read_rows(path, REVISION_FIELDS, revision):
        key = row[:2]
        if history.setdefault(key, row) != row:
            raise ValueError(f"conflicting content for revision {key}")
    previous = None
    for row in sorted(history.values()):
        change, version, sku, action, _, recorded, _ = row
        if previous is None or previous[0] != change:
            if version != 1 or action != "set":
                raise ValueError(f"{change}: history must begin with set revision 1")
        elif version != previous[1] + 1 or sku != previous[2] or recorded <= previous[5]:
            raise ValueError(f"{change}: expected consecutive versions, stable SKU and increasing recorded_at")
        previous = row
    return list(history.values())


def export_parquet(db, destination, cutoff):
    # Stage on the same filesystem so readers only see a complete footer and an
    # existing report wins even if two exporters choose the same destination.
    with TemporaryDirectory(dir=destination.parent) as directory:
        stage = Path(directory) / "export.parquet"
        db.execute("""
            COPY (
                SELECT a.order_id, a.sku, a.placed_at AT TIME ZONE 'UTC' AS placed_at,
                       CAST(? AS TIMESTAMP) AT TIME ZONE 'UTC' AS cutoff, a.quantity,
                       a.status AS at_order_status, b.status AS at_audit_status,
                       a.price AS at_order_unit_cents, b.price AS at_audit_unit_cents,
                       CAST((b.price::HUGEINT - a.price) * a.quantity AS DECIMAL(38,0)) AS delta_cents,
                       coalesce(a.sources, []) AS at_order_sources, coalesce(b.sources, []) AS at_audit_sources
                FROM perspectives a JOIN perspectives b USING(order_id)
                WHERE a.perspective='at_order' AND b.perspective='at_audit'
                ORDER BY b.price ASC NULLS LAST, a.order_id
            ) TO ? (FORMAT PARQUET, ROW_GROUP_SIZE 2048, COMPRESSION ZSTD)
        """, [cutoff, str(stage)])
        os.link(stage, destination)


def price_state(status, unit, sources):
    for source in sources or []:
        for field in ("effective_at", "recorded_at"):
            source[field] = source[field].isoformat() + "Z"
    return {"status": status, "unit_cents": unit, "sources": sources or []}


def price_history(rows, revisions, selected, cutoff):
    key, sku, placed, quantity = selected
    recorded = {}
    for change, version, product, action, _, when, _ in sorted(revisions):
        if product == sku and placed < when <= cutoff:
            recorded.setdefault(when, []).append({"change_id": change, "revision": version, "action": action})
    steps = []
    previous = None
    for _, _, _, _, _, status, unit, sources, when in rows:
        state = price_state(status, unit, sources)
        if state != previous:
            steps.append({"known_at": when.isoformat() + "Z", "recorded_revisions": recorded.get(when, []), **state})
            previous = state
    return {"order_id": key, "sku": sku, "placed_at": placed.isoformat() + "Z", "quantity": quantity,
            "cutoff": cutoff.isoformat() + "Z", "history": steps}


def comparison_totals(items):
    changes = Counter(item["change"] for item in items)
    deltas = [item["delta_cents"] for item in items if item["delta_cents"] is not None]
    return {"cohort_orders": len(items), "comparable_orders": len(deltas), "excluded_orders": len(items) - len(deltas),
            "counts": {kind: changes[kind] for kind in CHANGE_TYPES},
            "net_delta_cents": sum(deltas) if deltas else None,
            "increase_cents": sum(delta for delta in deltas if delta > 0) if deltas else None,
            "decrease_cents": -sum(delta for delta in deltas if delta < 0) if deltas else None}


def compare_prices(items, from_cutoff, cutoff, orders_after_from_cutoff):
    by_sku = defaultdict(list)
    for item in items:
        before, after = item["before"], item["after"]
        old, new = before["unit_cents"], after["unit_cents"]
        if before["status"] != after["status"]:
            kind = "status_changed"
        elif old != new:
            kind = "unit_price_changed"
        elif before["sources"] != after["sources"]:
            kind = "source_changed"
        else:
            kind = "unchanged"
        item["change"] = kind
        item["delta_cents"] = (new - old) * item["quantity"] if old is not None and new is not None else None
        by_sku[item["sku"]].append(item)
    return {"from_cutoff": from_cutoff.isoformat() + "Z", "cutoff": cutoff.isoformat() + "Z",
            "orders_after_from_cutoff": orders_after_from_cutoff, **comparison_totals(items),
            "by_sku": [{"sku": sku, **comparison_totals(group)} for sku, group in sorted(by_sku.items())],
            "changes": [item for item in items if item["change"] != "unchanged"]}


def audit(revisions, orders, cutoff, parquet=None, *, history_order=None, from_cutoff=None):
    if len({row[0] for row in orders}) != len(orders):
        raise ValueError("duplicate order_id")
    if from_cutoff is not None and (parquet is not None or history_order is not None):
        raise ValueError("cutoff comparison is mutually exclusive with history and Parquet export")
    if history_order is not None:
        if parquet is not None:
            raise ValueError("history and Parquet export are mutually exclusive")
        orders = [row for row in orders if row[0] == history_order]
        if not orders:
            raise ValueError(f"unknown order_id: {history_order}")
        sku = orders[0][1]
        revisions = [row for row in revisions if row[2] == sku and row[5] <= cutoff]
    if any(row[2] > cutoff for row in orders):
        raise ValueError("audit cutoff precedes an order")
    questions, parameters = AUDIT_QUESTIONS, [cutoff]
    orders_after_from_cutoff = 0
    if history_order is not None:
        questions = HISTORY_QUESTIONS
    elif from_cutoff is not None:
        if from_cutoff > cutoff:
            raise ValueError("comparison start must be at or before the audit cutoff")
        cohort = [row for row in orders if row[2] <= from_cutoff]
        orders_after_from_cutoff = len(orders) - len(cohort)
        orders = cohort
        questions, parameters = COMPARISON_QUESTIONS, [from_cutoff, cutoff]
    with duckdb.connect(":memory:") as db:
        db.execute("""CREATE TABLE revisions(change_id VARCHAR, revision BIGINT, sku VARCHAR, action VARCHAR,
                      effective_at TIMESTAMP, recorded_at TIMESTAMP, unit_cents BIGINT)""")
        db.execute("CREATE TABLE orders(order_id VARCHAR, sku VARCHAR, placed_at TIMESTAMP, quantity BIGINT)")
        if revisions:
            db.executemany("INSERT INTO revisions VALUES (?,?,?,?,?,?,?)", revisions)
        if orders:
            db.executemany("INSERT INTO orders VALUES (?,?,?,?)", orders)
        db.execute("CREATE TEMP TABLE price_questions AS " + questions, parameters)
        query = (ROOT / "prices.sql").read_text(encoding="utf-8")
        if parquet is not None:
            db.execute("CREATE TEMP TABLE perspectives AS " + query)
            export_parquet(db, parquet, cutoff)
            return {"cutoff": cutoff.isoformat() + "Z", "orders_written": len(orders), "parquet": str(parquet)}
        rows = db.execute(query).fetchall()
    if history_order is not None:
        return price_history(rows, revisions, orders[0], cutoff)
    by_order = {}
    for key, sku, placed, qty, perspective, status, unit, sources, _ in rows:
        item = by_order.setdefault(key, {"order_id": key, "sku": sku, "placed_at": placed.isoformat() + "Z",
                                         "quantity": qty})
        item[perspective] = price_state(status, unit, sources)
    if from_cutoff is not None:
        return compare_prices(list(by_order.values()), from_cutoff, cutoff, orders_after_from_cutoff)
    deltas = []
    for item in by_order.values():
        old, new = (item[view]["unit_cents"] for view in ("at_order", "at_audit"))
        item["delta_cents"] = None
        if old is not None and new is not None:
            item["delta_cents"] = (new - old) * item["quantity"]
            deltas.append(item["delta_cents"])
    return {"cutoff": cutoff.isoformat() + "Z", "orders": list(by_order.values()),
            "comparable_orders": len(deltas), "excluded_orders": len(orders) - len(deltas),
            "comparable_delta_cents": sum(deltas) if deltas else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revisions", type=Path)
    parser.add_argument("orders", type=Path)
    parser.add_argument("--as-of", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--parquet", type=Path, help="write one row per order to a new Parquet file")
    mode.add_argument("--history", metavar="ORDER_ID", help="explain changes after a specific order was placed")
    mode.add_argument("--from-as-of", metavar="TIMESTAMP", help="compare two cutoffs over orders placed by this instant")
    args = parser.parse_args()
    try:
        result = audit(load_history(args.revisions), list(read_rows(args.orders, ORDER_FIELDS, order)),
                       timestamp(args.as_of), args.parquet, history_order=args.history,
                       from_cutoff=timestamp(args.from_as_of) if args.from_as_of is not None else None)
    except (ValueError, OverflowError, OSError, csv.Error, duckdb.Error) as exc:
        parser.exit(2, f"audit failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
