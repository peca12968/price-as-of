from pathlib import Path
import random
from tempfile import TemporaryDirectory
import unittest

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from scout import inspect
from layout_demo import write_layout


class ScoutTests(unittest.TestCase):
    def test_joint_bounds_without_assuming_independent_nulls(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "overlap.parquet"
            pq.write_table(pa.table({"a": [1, 1, 1, None] * 2,
                                     "b": [1, 1, None, 1, 1, 1, 1, None]}), path, row_group_size=4)
            r = inspect(path, "a", 1, 2, filters=[("b", 1, 2)])
            self.assertEqual([(g["lower"], g["upper"]) for g in r["groups"]], [(2, 3), (2, 3)])
            with duckdb.connect() as db:
                counts = db.execute("""SELECT file_row_number // 4, count(*) FROM read_parquet(?, file_row_number=true)
                                        WHERE a=1 AND b=1 GROUP BY 1 ORDER BY 1""", [str(path)]).fetchall()
            self.assertEqual(counts, [(0, 2), (1, 3)])
            pq.write_table(pa.table({"a": [1, 2]}), path, write_statistics=False)
            contradiction = inspect(path, "a", -5, 1, filters=[("a", 1, 5)])
            self.assertEqual(contradiction["candidate_groups"], 0)

    def test_joint_bounds_against_full_decoding(self):
        rng = random.Random(173)
        values = {name: [rng.choice([None, -3, -1, 0, 2, 7]) for _ in range(150)] for name in ("a", "b", "c")}
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.parquet"
            for recorded in (True, False, ["a", "c"]):
                pq.write_table(pa.table(values), path, row_group_size=13, write_statistics=recorded)
                for _ in range(80):
                    filters = []
                    for name in ["a", "b", rng.choice(["a", "c"])]:
                        lo = rng.randrange(-6, 9)
                        filters.append((name, lo, lo+rng.randrange(1, 12)))
                    report = inspect(path, *filters[0], filters=filters[1:])
                    start = 0
                    for group in report["groups"]:
                        stop = start + group["rows"]
                        actual = sum(all(values[n][i] is not None and lo <= values[n][i] < hi
                                         for n, lo, hi in filters) for i in range(start, stop))
                        self.assertLessEqual(group["lower"], actual)
                        self.assertGreaterEqual(group["upper"], actual)
                        start = stop

    def test_layout_changes_footer_candidates_but_not_actual_results(self):
        with TemporaryDirectory() as tmp, duckdb.connect() as db:
            for layout, expected in [("sorted", 1), ("interleaved", 6)]:
                path = Path(tmp) / (layout + ".parquet")
                write_layout(db, path, layout)
                report = inspect(path, "seq", 4096, 6144, ["label", "seq"])
                # Force full data decoding in a different reader and count matches in each physical row group.
                values = db.execute(
                    "SELECT seq FROM read_parquet(?, file_row_number=true) ORDER BY file_row_number", [str(path)]).fetchall()
                offset = found = 0
                for group in report["groups"]:
                    count = sum(4096 <= v < 6144 for (v,) in values[offset:offset+group["rows"]])
                    if group["state"] == "skip":
                        self.assertEqual(count, 0)
                    if group["state"] == "all":
                        self.assertEqual(count, group["rows"])
                    offset += group["rows"]
                    found += count
                self.assertEqual((offset, found, report["candidate_groups"]), (12288, 2048, expected))
                self.assertLessEqual(report["matching_rows_lower_bound"], found)
                self.assertGreaterEqual(report["matching_rows_upper_bound"], found)
                self.assertEqual(report["columns"], ["seq", "label"])

    def test_null_groups_unknown_statistics_and_half_open_boundary(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "nullable.parquet"
            table = pa.table({"amount": pa.array([None, None, 1, 2, 4, None], type=pa.int64())})
            pq.write_table(table, path, row_group_size=2)
            r = inspect(path, "amount", 1, 4)
            self.assertEqual([g["state"] for g in r["groups"]], ["skip", "all", "skip"])
            pq.write_table(table, path, row_group_size=2, write_statistics=False)
            r = inspect(path, "amount", 1, 4)
            self.assertEqual([g["state"] for g in r["groups"]], ["candidate"] * 3)
            self.assertEqual((r["matching_rows_lower_bound"], r["matching_rows_upper_bound"]), (0, 6))

    def test_uint64_projection_and_mixed_nulls(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "wide.parquet"
            base = 2**63 + 5
            pq.write_table(pa.table({"n": pa.array([base, None, base+1, base+2], type=pa.uint64()),
                                     "label": ["x", None, "y", "z"]}), path, row_group_size=2)
            r = inspect(path, "n", base, base+1)
            self.assertEqual([(g["state"], g["statistics"]["n"]["bounds"]) for g in r["groups"]],
                             [("all_nonnull", (base, base)), ("skip", (base+1, base+2))])
            self.assertEqual((r["matching_rows_lower_bound"], r["matching_rows_upper_bound"]), (1, 1))
            self.assertLess(r["total_compressed_bytes"], inspect(path, "n", base, base+1, ["label"])["total_compressed_bytes"])
            with self.assertRaisesRegex(ValueError, "integer"):
                inspect(path, "label", 0, 1)
