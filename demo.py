"""Check file ownership against real Parquet files and independent time-travel reads."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.table import StaticTable

from footprint import footprint


def exercise(root):
    catalog = SqlCatalog("local", uri="sqlite:///" + (root / "catalog.db").as_posix(),
                         warehouse=(root / "warehouse").as_posix())
    try:
        catalog.create_namespace("shop")
        shape = pa.schema([("id", pa.int64()), ("sku", pa.string())])
        table = catalog.create_table("shop.receipts", schema=shape, properties={"format-version": "2"})
        def append(keys):
            table.append(pa.table({"id": keys, "sku": [f"kit-{i}" for i in keys]}, schema=shape))
            return table.current_snapshot().snapshot_id
        first = append([1, 2])
        second = append([3, 4])
        table.manage_snapshots().create_tag(first, "audit").commit()
        table.delete("id <= 2")
        third = table.current_snapshot().snapshot_id
        latest = append([5])
        pinned = StaticTable.from_metadata(table.metadata_location)
        protected = footprint(pinned, [first, second, latest])
        assert protected["unreferenced_file_bytes"] == 0
        assert {tuple(row["refs"]) for row in protected["protected"]} == {("audit",), ("main",)}
        table.manage_snapshots().remove_tag("audit").commit()
        unpinned = StaticTable.from_metadata(table.metadata_location)
        assert footprint(unpinned, [first])["unreferenced_file_bytes"] == 0
        assert footprint(unpinned, [second])["unreferenced_file_bytes"] == 0
        both = footprint(unpinned, [first, second])
        assert footprint(pinned, [first, second])["unreferenced_file_bytes"] == 0
        actual_rows = {s: sorted(unpinned.scan(snapshot_id=s).to_arrow()["id"].to_pylist())
                       for s in (first, second, third, latest)}
        assert list(actual_rows.values()) == [[1, 2], [1, 2, 3, 4], [3, 4], [3, 4, 5]]
        paths = {p.as_posix(): p for p in root.rglob("*.parquet")}
        # Discover owners from stored row IDs and SQL-equivalent snapshot reads, independently of manifest iteration.
        for row in both["files"]:
            path = paths[row["path"]]
            keys = set(pq.read_table(path)["id"].to_pylist())
            assert set(row["owners"]) == {s for s, values in actual_rows.items() if keys.issubset(values)}
            assert row["bytes"] == path.stat().st_size
        freed, = [r for r in both["files"] if not r["surviving_owners"]]
        assert set(pq.read_table(paths[freed["path"]])["id"].to_pylist()) == {1, 2}
        assert both["unreferenced_file_bytes"] == freed["bytes"] > 0
        return {"snapshots": len(actual_rows), "all_parquet_files": len(paths),
                "pinned_bytes_released": protected["unreferenced_file_bytes"],
                "joint_files_released": 1, "joint_bytes_released": freed["bytes"], "latest_ids": actual_rows[latest]}
    finally:
        catalog.engine.dispose()


if __name__ == "__main__":
    with TemporaryDirectory() as temp:
        print(json.dumps(exercise(Path(temp).resolve()), indent=2))
