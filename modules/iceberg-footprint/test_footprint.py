import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.table import StaticTable

from demo import exercise
from footprint import footprint


class FileReachability(unittest.TestCase):
    def test_real_manifests_include_shared_files_and_exclude_deleted_entries(self):
        with TemporaryDirectory() as tmp:
            result = exercise(Path(tmp).resolve())
            self.assertEqual((result["snapshots"], result["all_parquet_files"], result["joint_files_released"]), (4, 3, 1))

    def test_v1_and_v2_frozen_metadata_empty_snapshots_and_named_heads(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            catalog = SqlCatalog("local", uri="sqlite:///" + (root / "catalog.db").as_posix(),
                                 warehouse=(root / "store").as_posix())
            try:
                catalog.create_namespace("shop")
                for version in ("1", "2"):
                    shape = pa.schema([("id", pa.int32())])
                    table = catalog.create_table(f"shop.test{version}", schema=shape, properties={"format-version": version})
                    self.assertEqual(footprint(table, [])["snapshots"], [])
                    table.append(pa.table({"id": [1]}, schema=shape))
                    first = table.current_snapshot().snapshot_id
                    frozen = StaticTable.from_metadata(table.metadata_location)
                    table.append(pa.table({"id": [2]}, schema=shape))
                    current = table.current_snapshot().snapshot_id
                    with self.assertRaisesRegex(ValueError, "Unknown snapshot"):
                        footprint(frozen, [current])
                    report = footprint(frozen, [first, first])
                    self.assertEqual(report["simulated_removal"], [])
                    self.assertEqual(report["protected"], [{"id": first, "refs": ["main"]}])
                    cli = subprocess.run([sys.executable, Path(__file__).with_name("footprint.py"),
                                          table.metadata_location, str(first)], capture_output=True, check=True)
                    later = json.loads(cli.stdout)
                    self.assertEqual(later["simulated_removal"], [first])
                    self.assertEqual(later["files"][0]["surviving_owners"], [current])
                    self.assertEqual(later["files"][0]["content"], "DATA")
                    self.assertEqual(later["unreferenced_file_bytes"], 0)
                    if version == "2":
                        with table.manage_snapshots() as update:
                            update.create_branch(first, "restock").create_tag(first, "quarter-end")
                        guarded = footprint(table, [first])
                        self.assertEqual(guarded["protected"], [{"id": first, "refs": ["quarter-end", "restock"]}])
            finally:
                catalog.engine.dispose()
