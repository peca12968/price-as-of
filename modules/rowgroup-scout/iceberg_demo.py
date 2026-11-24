"""Verify pruning across a rename and a new column reusing the historical name."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.table import StaticTable
from pyiceberg.types import LongType
from snapshot_scout import estimate


def exercise(footprint_cli):
    with TemporaryDirectory() as temp:
        root = Path(temp).resolve()
        catalog = SqlCatalog("local", uri="sqlite:///" + (root / "catalog.db").as_posix(),
                             warehouse=(root / "warehouse").as_posix())
        try:
            catalog.create_namespace("shop")
            original = pa.schema([("qty", pa.int64()), ("bin", pa.string())])
            table = catalog.create_table("shop.stock", schema=original)
            table.append(pa.table({"qty": [10, 12, 15], "bin": ["a", "b", "c"]}, schema=original))
            first = table.current_snapshot().snapshot_id
            old_id = table.schema().find_field("qty").field_id
            with table.update_schema() as update:
                update.rename_column("qty", "available")
            with table.update_schema() as update:
                update.add_column("qty", LongType())
            new_id = table.schema().find_field("qty").field_id
            table.append(pa.table({"available": [22, 23], "bin": ["d", "e"], "qty": [10, 11]}))
            second = table.current_snapshot().snapshot_id
            table.delete("available < 20")
            current = table.current_snapshot().snapshot_id
            frozen = StaticTable.from_metadata(table.metadata_location)
            results = []
            for sid, field_id, name, expected in [(first, old_id, "qty", 3), (second, old_id, "available", 3),
                                                   (second, new_id, "qty", 2), (current, old_id, "available", 0)]:
                report = estimate(frozen, sid, field_id, 10, 20)
                assert report["field_name"] == name
                values = frozen.scan(snapshot_id=sid).to_arrow()[name].to_pylist()
                actual = sum(1 for x in values if x is not None and 10 <= x < 20)
                assert report["matches"] == [actual, actual] and expected == actual
                results.append(report)
            assert {f["physical_name"] for f in results[1]["files"]} == {"qty", "available"}
            assert {f["basis"] for f in results[2]["files"]} == {"footer", "missing_field"}
            ownership = json.loads(subprocess.check_output([sys.executable, footprint_cli, frozen.metadata_location,
                                                            str(first), str(second)]))
            released, = [r for r in ownership["files"] if not r["surviving_owners"]]
            legacy, = [f for f in results[2]["files"] if f["basis"] == "missing_field"]
            assert legacy["file"] == released["path"]
            try:
                estimate(frozen, first, new_id, 10, 20)
            except ValueError as error:
                assert "field ID" in str(error)
            else:
                raise AssertionError("Historical schema accepted a field introduced later")
            imported = root / "imported.parquet"
            pq.write_table(pa.table({"available": [0, 1], "bin": ["f", "g"], "qty": [11, 30]}), imported)
            table.add_files([imported.as_posix()])
            latest = StaticTable.from_metadata(table.metadata_location)
            report = estimate(latest, latest.current_snapshot().snapshot_id, new_id, 10, 20)
            assert report["matches"] == [2, 4]
            assert len([f for f in report["files"] if f["basis"] == "unknown_field_ids"]) == 1
            actual = len(latest.scan(row_filter="qty >= 10 and qty < 20").to_arrow())
            assert actual == 3
            # A physical-name-only scan would combine old and new meanings of qty.
            return {"historical_qty": results[0]["matches"], "renamed_available": results[1]["matches"],
                    "replacement_qty": results[2]["matches"], "latest_available": results[3]["matches"],
                    "old_field_id": old_id, "new_field_id": new_id, "old_file_bytes": released["bytes"],
                    "after_external_import": report["matches"], "decoded_after_import": actual}
        finally:
            catalog.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("footprint_cli", type=Path)
    print(json.dumps(exercise(parser.parse_args().footprint_cli.resolve()), indent=2))
