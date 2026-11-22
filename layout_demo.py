"""Compare the same parcel scan query under two Parquet row layouts."""
import argparse
import json
from pathlib import Path

import duckdb

from scout import inspect


def write_layout(db, path, layout):
    order = {"sorted": "i", "interleaved": "i % 6, i"}[layout]
    db.execute(f"""COPY (SELECT i AS seq, 'parcel-' || i AS label FROM range(12288) t(i)
                         ORDER BY {order}) TO ? (FORMAT PARQUET, ROW_GROUP_SIZE 2048)""", [str(path)])


def demo(directory):
    directory.mkdir(parents=True)
    summary = {}
    with duckdb.connect() as db:
        for layout in ("sorted", "interleaved"):
            path = directory / (layout + ".parquet")
            write_layout(db, path, layout)
            report = inspect(path, "seq", 4096, 6144, ["label"])
            count, total = db.execute("""SELECT count(*), sum(seq) FROM read_parquet(?)
                                         WHERE seq >= 4096 AND seq < 6144""", [str(path)]).fetchone()
            summary[layout] = {"candidate_groups": report["candidate_groups"], "groups": len(report["groups"]),
                               "candidate_compressed_bytes": report["candidate_compressed_bytes"],
                               "total_compressed_bytes": report["total_compressed_bytes"],
                               "matched_rows": count, "sum_seq": total}
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="new directory for both Parquet files")
    print(json.dumps(demo(parser.parse_args().directory), indent=2))
