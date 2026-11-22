"""Export a real price audit, inspect its footer, then query the matching orders."""
import csv
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import duckdb
from audit import ORDER_FIELDS, REVISION_FIELDS

ROOT = Path(__file__).resolve().parent


def run(scout, directory):
    history, orders, ledger = [directory / name for name in ("history.csv", "orders.csv", "audit.parquet")]
    with history.open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(REVISION_FIELDS)
        for price in (400, 800, 1200):
            writer.writerow([f"p{price}", 1, f"SKU-{price}", "set", "2026-04-01T00:00:00Z", "2026-04-01T00:00:00Z", price])
    with orders.open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(ORDER_FIELDS)
        for i in range(6144):
            writer.writerow([f"o{i:04}", f"SKU-{(i % 3 + 1) * 400}", "2026-04-02T00:00:00Z", 1 + i % 2])
    subprocess.run([sys.executable, ROOT / "audit.py", history, orders, "--as-of", "2026-04-03T00:00:00Z",
                    "--parquet", ledger], check=True)
    result = subprocess.check_output([sys.executable, scout / "scout.py", ledger, "at_audit_unit_cents", "800", "801",
                                      "--and", "quantity", "2", "3", "--project", "order_id"])
    report = json.loads(result)
    with duckdb.connect() as db:
        hits = db.execute("""SELECT order_id FROM read_parquet(?) WHERE at_audit_unit_cents=800 AND quantity=2
                             ORDER BY order_id""", [str(ledger)]).fetchall()
    assert hits == [(f"o{i:04}",) for i in range(1, 6144, 6)]
    assert len(report["groups"]) == 3 and report["candidate_groups"] == 1
    print(f"Kept 1/3 groups and verified all {len(hits)} matching order IDs")


if __name__ == "__main__":
    with TemporaryDirectory() as tmp:
        run(Path(sys.argv[1]).resolve(), Path(tmp))
