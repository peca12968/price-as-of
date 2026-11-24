"""Estimate integer-range cardinality across the data files of a pinned Iceberg snapshot."""
import argparse
import json

import pyarrow.parquet as pq
from pyiceberg.manifest import DataFileContent, FileFormat
from pyiceberg.table import StaticTable
from pyiceberg.types import IntegerType, LongType
from scout import inspect


def logical_bounds(lower, upper, deletes):
    if any(f.content != DataFileContent.POSITION_DELETES for f in deletes):
        return 0, upper
    return max(0, lower - sum(f.record_count for f in deletes)), upper


def estimate(table, snapshot_id, field_id, lo, hi):
    if table.metadata.format_version not in (1, 2) or lo >= hi:
        raise ValueError("Expected Iceberg v1/v2 metadata and a nonempty half-open range")
    snapshot = table.snapshot_by_id(snapshot_id)
    if snapshot is None:
        raise ValueError("Unknown snapshot")
    schemas = table.schemas()
    schema = schemas.get(snapshot.schema_id)
    if schema is None and len(schemas) == 1:
        schema = next(iter(schemas.values()))
    if schema is None:
        raise ValueError("Could not identify the schema at this snapshot")
    field = next((f for f in schema.fields if f.field_id == field_id), None)
    if field is None or not isinstance(field.field_type, (IntegerType, LongType)):
        raise ValueError("Expected a top-level integer field ID in the selected snapshot")
    rows = []
    for task in table.scan(snapshot_id=snapshot_id).plan_files():
        if task.file.file_format != FileFormat.PARQUET:
            raise ValueError("Snapshot contains a non-Parquet data file")
        with table.io.new_input(task.file.file_path).open() as source, pq.ParquetFile(source) as file:
            fields = file.schema_arrow
            ids = [(f.metadata or {}).get(b"PARQUET:field_id") for f in fields]
            matches = [f.name for f, value in zip(fields, ids) if value == str(field_id).encode("ascii")]
            count = file.metadata.num_rows
            if len(matches) > 1:
                raise ValueError("Multiple physical columns carry the same field ID")
            if matches:
                physical, = matches
                stats = inspect(file, physical, lo, hi)
                low, high = stats["matching_rows_lower_bound"], stats["matching_rows_upper_bound"]
                groups, basis = stats["candidate_groups"], "footer"
            elif all(value is not None and value.isdigit() and int(value) > 0 for value in ids):
                physical = None
                default = field.initial_default
                match = default is not None and lo <= default < hi
                low = high = count if match else 0
                groups, basis = file.num_row_groups if match else 0, "missing_field"
            else:
                # Untagged external files may rely on an Iceberg name mapping. Avoid guessing a column.
                low, high = 0, count
                physical, groups, basis = None, file.num_row_groups, "unknown_field_ids"
            logical_low, logical_high = logical_bounds(low, high, task.delete_files)
            rows.append({"file": task.file.file_path, "physical_name": physical, "basis": basis,
                         "row_groups": file.num_row_groups, "candidate_groups": groups,
                         "delete_files": len(task.delete_files), "before_deletes": [low, high],
                         "matches": [logical_low, logical_high]})
    return {"snapshot": snapshot_id, "field_id": field_id, "field_name": field.name, "range": [lo, hi],
            "matches": [sum(r["matches"][i] for r in rows) for i in (0, 1)],
            "files": sorted(rows, key=lambda r: r["file"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata")
    for name in ("snapshot", "field_id", "lower", "upper"):
        parser.add_argument(name, type=int)
    args = parser.parse_args()
    try:
        result = estimate(StaticTable.from_metadata(args.metadata), args.snapshot, args.field_id,
                          args.lower, args.upper)
    except (OSError, ValueError) as error:
        parser.exit(2, f"{error}\n")
    print(json.dumps(result, indent=2))
