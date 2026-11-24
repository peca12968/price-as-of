"""Export the example cohorts and summarize rows read back from Parquet."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb

from audit import ORDER_FIELDS, ROOT, audit, load_history, order, read_rows, timestamp


def demo():
    revisions = load_history(ROOT / "examples/revisions.csv")
    orders = list(read_rows(ROOT / "examples/orders.csv", ORDER_FIELDS, order))
    scenarios = [
        ("2026-04-13T00:00:00Z", "2026-04-23T00:00:00Z"),
        ("2026-04-23T00:00:00Z", "2026-04-25T00:00:00Z"),
    ]
    result = []
    with TemporaryDirectory() as temp, duckdb.connect() as db:
        for number, (start, end) in enumerate(scenarios):
            path = Path(temp) / f"comparison-{number}.parquet"
            summary = audit(revisions, orders, timestamp(end), path, from_cutoff=timestamp(start))
            query = db.execute("""SELECT order_id, sku, quantity, change, before_status, after_status, delta_cents
                                  FROM read_parquet(?) ORDER BY sku, order_id""", [str(path)])
            columns = [name for name, *_ in query.description]
            delta_type = str(query.description[-1][1])
            rows = [dict(zip(columns, row)) for row in query.fetchall()]
            for row in rows:
                if row["delta_cents"] is not None:
                    row["delta_cents"] = int(row["delta_cents"])
            del summary["parquet"]
            result.append({**summary, "delta_type": delta_type, "rows": rows})
    return result


if __name__ == "__main__":
    print(json.dumps(demo(), indent=2))
