"""Estimate integer range-filter pruning using a Parquet footer."""
import argparse
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def column_estimate(chunk, rows, lo, hi):
    stats = chunk.statistics
    nulls = stats.null_count if stats and stats.has_null_count else None
    bounds = (stats.min, stats.max) if stats and stats.has_min_max else None
    upper = rows - (nulls or 0)
    lower = 0
    covers_nonnull = bool(bounds and lo <= bounds[0] and bounds[1] < hi)
    if lo >= hi or (bounds and (bounds[1] < lo or bounds[0] >= hi)):
        upper = 0
    elif covers_nonnull and nulls is not None:
        lower = upper
    return {"bounds": bounds, "nulls": nulls, "lower": lower, "upper": upper,
            "covers_nonnull": covers_nonnull}


def inspect(path, column, lower, upper, project=(), filters=()):
    ranges = {}
    for name, lo, hi in [(column, lower, upper), *filters]:
        if type(lo) is not int or type(hi) is not int or lo >= hi:
            raise ValueError("expected a nonempty half-open integer range")
        old_lo, old_hi = ranges.get(name, (lo, hi))
        ranges[name] = max(old_lo, lo), min(old_hi, hi)
    wanted = list(dict.fromkeys((*ranges, *project)))
    with pq.ParquetFile(path) as file:
        schema = file.schema_arrow
        leaves = {}
        for name in wanted:
            index = schema.get_field_index(name)
            matches = [i for i in range(len(file.schema)) if file.schema.column(i).path == name]
            if index < 0 or len(matches) != 1 or pa.types.is_nested(schema.field(index).type):
                raise ValueError(f"expected one flat, top-level column named {name!r}")
            leaves[name] = matches[0]
        for name in ranges:
            if not pa.types.is_integer(schema.field(name).type):
                raise ValueError(f"the filter column {name!r} must have an integer type")
        groups = []
        min_rows = max_rows = candidate_bytes = total_bytes = 0
        for i in range(file.num_row_groups):
            group = file.metadata.row_group(i)
            rows = group.num_rows
            size = sum(group.column(index).total_compressed_size for index in leaves.values())
            details = {name: column_estimate(group.column(leaves[name]), rows, *limits)
                       for name, limits in ranges.items()}
            # The union of the failing rows is no larger than the sum of its per-column upper bounds.
            at_least = max(0, rows - sum(rows - d["lower"] for d in details.values()))
            at_most = min(d["upper"] for d in details.values())
            if at_most:
                candidate_bytes += size
            state = "candidate"
            if at_most == 0:
                state = "skip"
            elif at_least == rows:
                state = "all"
            elif all(d["covers_nonnull"] for d in details.values()):
                state = "all_nonnull"
            min_rows += at_least
            max_rows += at_most
            total_bytes += size
            groups.append({"group": i, "rows": rows, "state": state, "statistics": details,
                           "lower": at_least, "upper": at_most, "projected_compressed_bytes": size})
    return {"columns": wanted, "ranges": ranges, "groups": groups,
            "candidate_groups": sum(g["state"] != "skip" for g in groups),
            "matching_rows_lower_bound": min_rows, "matching_rows_upper_bound": max_rows,
            "candidate_compressed_bytes": candidate_bytes, "total_compressed_bytes": total_bytes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("column")
    parser.add_argument("lower", type=int)
    parser.add_argument("upper", type=int)
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--and", dest="filters", nargs=3, metavar=("COLUMN", "LOWER", "UPPER"),
                        action="append", default=[])
    args = parser.parse_args()
    try:
        filters = [(name, int(lo), int(hi)) for name, lo, hi in args.filters]
        report = inspect(args.file, args.column, args.lower, args.upper, args.project, filters)
    except (OSError, ValueError, pa.ArrowException) as error:
        parser.exit(2, f"{error}\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
