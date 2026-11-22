"""Reconstruct order prices as originally known and as known at a later cutoff."""
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

import duckdb


ROOT = Path(__file__).resolve().parent
REVISION_FIELDS = "change_id revision sku action effective_at recorded_at unit_cents".split()
ORDER_FIELDS = "order_id sku placed_at quantity".split()
TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])")


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


def audit(revisions, orders, cutoff):
    if any(row[2] > cutoff for row in orders):
        raise ValueError("audit cutoff precedes an order")
    if len({row[0] for row in orders}) != len(orders):
        raise ValueError("duplicate order_id")
    with duckdb.connect(":memory:") as db:
        db.execute("""CREATE TABLE revisions(change_id VARCHAR, revision BIGINT, sku VARCHAR, action VARCHAR,
                      effective_at TIMESTAMP, recorded_at TIMESTAMP, unit_cents BIGINT)""")
        db.execute("CREATE TABLE orders(order_id VARCHAR, sku VARCHAR, placed_at TIMESTAMP, quantity BIGINT)")
        if revisions:
            db.executemany("INSERT INTO revisions VALUES (?,?,?,?,?,?,?)", revisions)
        if orders:
            db.executemany("INSERT INTO orders VALUES (?,?,?,?)", orders)
        rows = db.execute((ROOT / "prices.sql").read_text(encoding="utf-8"), [cutoff]).fetchall()
    by_order = {}
    for key, sku, placed, qty, perspective, status, unit, sources in rows:
        item = by_order.setdefault(key, {"order_id": key, "sku": sku, "placed_at": placed.isoformat() + "Z",
                                         "quantity": qty})
        for source in sources or []:
            for field in ("effective_at", "recorded_at"):
                source[field] = source[field].isoformat() + "Z"
        item[perspective] = {"status": status, "unit_cents": unit, "sources": sources or []}
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
    args = parser.parse_args()
    try:
        result = audit(load_history(args.revisions), list(read_rows(args.orders, ORDER_FIELDS, order)),
                       timestamp(args.as_of))
    except (ValueError, OverflowError, OSError, csv.Error, duckdb.Error) as exc:
        parser.exit(2, f"audit failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
