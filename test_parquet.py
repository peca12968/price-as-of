from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import duckdb
import audit
from cutoff_parquet_demo import demo
from test_audit import ROOT, HISTORY, ORDERS, FINAL, cli


class ExportTests(unittest.TestCase):
    def test_public_demo_is_a_fresh_parquet_readback(self):
        fixture = ROOT / "examples/cutoff-parquet.json"
        self.assertEqual(demo(), json.loads(fixture.read_text(encoding="utf-8")))

    def test_complete_ledger_and_no_clobber(self):
        with TemporaryDirectory() as tmp, duckdb.connect() as db:
            dest = Path(tmp) / "ledger.parquet"
            cmd = [sys.executable, ROOT / "audit.py", HISTORY, ORDERS, "--as-of", FINAL, "--parquet", dest]
            subprocess.run(cmd, check=True, capture_output=True)
            before = dest.read_bytes()
            conflict = subprocess.run(cmd, capture_output=True)
            self.assertEqual((conflict.returncode, conflict.stdout, dest.read_bytes()), (2, b"", before))
            self.assertEqual(list(Path(tmp).iterdir()), [dest])
            report = audit.audit(audit.load_history(HISTORY), list(audit.read_rows(ORDERS, audit.ORDER_FIELDS, audit.order)),
                                 audit.timestamp(FINAL))
            rows = db.execute("""SELECT * REPLACE(placed_at AT TIME ZONE 'UTC' AS placed_at,
                                                  cutoff AT TIME ZONE 'UTC' AS cutoff)
                                 FROM read_parquet(?) ORDER BY order_id""", [str(dest)])
            fields = [c[0] for c in rows.description]
            for values, order in zip(rows.fetchall(), report["orders"], strict=True):
                row = dict(zip(fields, values))
                for field in ("order_id", "sku", "quantity", "delta_cents"):
                    self.assertEqual(row[field], order[field])
                for field, value in (("cutoff", FINAL), ("placed_at", order["placed_at"])):
                    self.assertEqual(row[field].isoformat() + "Z", value)
                for view in ("at_order", "at_audit"):
                    for field in ("status", "unit_cents", "sources"):
                        value = row[f"{view}_{field}"]
                        if field == "sources":
                            for src in value:
                                for t in ("effective_at", "recorded_at"):
                                    src[t] = src[t].isoformat() + "Z"
                        self.assertEqual(value, order[view][field])

    def test_empty_and_maximum_positive_and_negative_delta(self):
        placed = audit.timestamp(FINAL)
        old, later = placed - timedelta(seconds=2), placed + timedelta(seconds=1)
        limit = 2**63 - 1
        history = [(sku, version, sku, "set", old, old if version == 1 else later, price)
                   for sku, series in [("rise", (0, limit)), ("fall", (limit, 0))]
                   for version, price in enumerate(series, 1)]
        with TemporaryDirectory() as tmp, duckdb.connect() as db:
            for name, orders in [("empty", []), ("full", [(k, k, placed, limit) for k in ("rise", "fall")])]:
                for variant, start in (("single", None), ("comparison", placed)):
                    target = Path(tmp) / f"{name}-{variant}.parquet"
                    audit.audit(history, orders, later, target, from_cutoff=start)
                    data = db.execute("SELECT delta_cents FROM read_parquet(?) ORDER BY delta_cents", [str(target)]).fetchall()
                    self.assertEqual(data, [(-limit**2,), (limit**2,)] if orders else [])

    def test_comparison_export_preserves_cohort_rows_sources_and_empty_case(self):
        history = audit.load_history(HISTORY)
        orders = list(audit.read_rows(ORDERS, audit.ORDER_FIELDS, audit.order))
        scenarios = json.loads((ROOT / "examples/cutoff-diff.json").read_text(encoding="utf-8"))
        scenarios.append({"from_cutoff": "2026-04-01T00:00:00Z", "cutoff": FINAL,
                          "cohort_orders": 0, "orders_after_from_cutoff": len(orders), "changes": []})
        with TemporaryDirectory() as tmp, duckdb.connect() as db:
            for index, expected in enumerate(scenarios):
                with self.subTest(index=index):
                    dest = Path(tmp) / f"cutoff {index}.parquet"
                    start, end = (audit.timestamp(expected[key]) for key in ("from_cutoff", "cutoff"))
                    args = ("--from-as-of", expected["from_cutoff"], "--parquet", str(dest))
                    process = cli(cutoff=expected["cutoff"], flags=args)
                    self.assertEqual(process.returncode, 0, process.stderr)
                    self.assertEqual(json.loads(process.stdout), {"from_cutoff": expected["from_cutoff"],
                                     "cutoff": expected["cutoff"], "orders_written": expected["cohort_orders"],
                                     "orders_after_from_cutoff": expected["orders_after_from_cutoff"], "parquet": str(dest)})
                    cohort = [row for row in orders if row[2] <= start]
                    changes = {row["order_id"]: row for row in expected["changes"]}
                    snapshots = [{row["order_id"]: row["at_audit"] for row in audit.audit(history, cohort, instant)["orders"]}
                                 for instant in (start, end)]
                    result = db.execute("""SELECT * REPLACE (placed_at AT TIME ZONE 'UTC' AS placed_at,
                                                            from_cutoff AT TIME ZONE 'UTC' AS from_cutoff,
                                                            cutoff AT TIME ZONE 'UTC' AS cutoff)
                                         FROM read_parquet(?)""", [str(dest)])
                    columns = {column[0]: str(column[1]) for column in result.description}
                    self.assertEqual(columns["delta_cents"], "DECIMAL(38,0)")
                    records = [dict(zip(columns, row)) for row in result.fetchall()]
                    self.assertEqual([(row["sku"], row["order_id"]) for row in records],
                                     sorted((sku, key) for key, sku, _, _ in cohort))
                    for row in records:
                        key = row["order_id"]
                        self.assertEqual(row["from_cutoff"], start)
                        self.assertEqual(row["cutoff"], end)
                        before, after = (snapshot[key] for snapshot in snapshots)
                        for prefix, state in (("before", before), ("after", after)):
                            self.assertEqual(audit.price_state(row[f"{prefix}_status"], row[f"{prefix}_unit_cents"],
                                                               row[f"{prefix}_sources"]), state)
                        self.assertEqual(row["change"], changes.get(key, {}).get("change", "unchanged"))
                        self.assertEqual(row["delta_cents"], changes.get(key, {}).get("delta_cents",
                                         0 if before["status"] == after["status"] == "priced" else None))

    def test_comparison_export_retries_cannot_overwrite_a_complete_file(self):
        with TemporaryDirectory() as tmp, duckdb.connect() as db:
            dest = Path(tmp) / "comparison.parquet"
            command = [sys.executable, ROOT / "audit.py", HISTORY, ORDERS, "--as-of", FINAL,
                       "--from-as-of", "2026-04-13T00:00:00Z", "--parquet", dest]
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as first:
                second = subprocess.run(command, capture_output=True, timeout=30)
                first_stdout, first_stderr = first.communicate(timeout=30)
            self.assertEqual(sorted([first.returncode, second.returncode]), [0, 2], (first_stderr, second.stderr))
            self.assertEqual(first_stdout if first.returncode else second.stdout, b"")
            self.assertEqual(db.execute("SELECT count(*) FROM read_parquet(?)", [str(dest)]).fetchone(), (5,))
            self.assertEqual(list(Path(tmp).iterdir()), [dest])
