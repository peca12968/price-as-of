from datetime import timedelta
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import duckdb
import audit
from test_audit import ROOT, HISTORY, ORDERS, FINAL


class ExportTests(unittest.TestCase):
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
                target = Path(tmp) / (name + ".parquet")
                audit.audit(history, orders, later, target)
                data = db.execute("SELECT delta_cents FROM read_parquet(?) ORDER BY delta_cents", [str(target)]).fetchall()
                self.assertEqual(data, [(-limit**2,), (limit**2,)] if orders else [])
