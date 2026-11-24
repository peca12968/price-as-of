from contextlib import closing
import json
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from demo import run
from ledger import MAX_INT, append, initialize, page


class LedgerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "receipt log.db"
        self.writer = sqlite3.connect(self.path)
        self.addCleanup(self.writer.close)
        initialize(self.writer)

    def test_pages_match_the_original_rows_while_another_connection_appends(self):
        rng = random.Random(48219)
        with closing(sqlite3.connect(self.path)) as reader:
            for case in range(32):
                store = str(case)
                original = []
                for _ in range(rng.randint(3, 45)):
                    ts, cents = rng.randint(-2, 2), rng.randint(-2000, 2000)
                    key = append(self.writer, store, ts, cents)
                    original.append((ts, key, cents))
                seen = []
                cursor = None
                while True:
                    result = page(reader, store, rng.randint(1, 5), cursor)
                    self.assertEqual(result["through_id"], original[-1][1])
                    seen.extend((item["event_time"], item["id"], item["cents"]) for item in result["items"])
                    cursor = result["next"]
                    if cursor is None:
                        break
                    for channel in (store, "other"):
                        append(self.writer, channel, rng.randint(-5, 5), 100)
                self.assertEqual(seen, sorted(original, reverse=True))

    def test_terminal_pages_replay_and_cursor_scope(self):
        self.assertEqual(page(self.writer, "north")["items"], [])
        self.assertIsNone(page(self.writer, "north")["next"])
        for stamp in (-MAX_INT-1, MAX_INT, MAX_INT):
            append(self.writer, "north", stamp, -1)
        self.assertIsNone(page(self.writer, "north", 3)["next"])
        cursor = page(self.writer, "north", 1)["next"]
        result = page(self.writer, "north", 2, cursor)
        append(self.writer, "north", 0, 34)
        self.assertEqual(page(self.writer, "north", 2, cursor), result)
        self.assertEqual([r["id"] for r in result["items"]], [2, 1])
        with self.assertRaises(ValueError):
            page(self.writer, "south", 2, cursor)
        with closing(sqlite3.connect(":memory:")) as other:
            initialize(other)
            with self.assertRaises(ValueError):
                page(other, "north", 2, cursor)

    def test_cli_resumes_across_processes_and_rejects_bad_input(self):
        def cli(*args):
            return subprocess.run([sys.executable, Path(__file__).with_name("ledger.py"), *args],
                                  capture_output=True, text=True)

        fresh = self.path.with_name("新帳.db")
        self.assertEqual(cli("page", fresh, "東倉").returncode, 2)
        self.assertFalse(fresh.exists())
        self.assertEqual(cli("init", fresh).returncode, 0)
        for stamp in ("10", "10", "11"):
            self.assertEqual(cli("append", fresh, "東倉", stamp, "95").returncode, 0)
        first = json.loads(cli("page", fresh, "東倉", "--limit", "2").stdout)
        self.assertEqual([r["id"] for r in first["items"]], [3, 2])
        self.assertEqual(cli("init", fresh).returncode, 2)
        follow = json.loads(cli("page", fresh, "東倉", "--after", first["next"]).stdout)
        self.assertEqual([r["id"] for r in follow["items"]], [1])
        for flags in (["--after", "bad token"], ["--limit", "0"], ["--limit", "201"]):
            result = cli("page", fresh, "東倉", *flags)
            self.assertEqual((result.returncode, result.stdout), (2, ""))

    def test_writes_cannot_change_history_and_uncommitted_rows_do_not_advance_the_boundary(self):
        append(self.writer, "north", 0, 5)
        with self.writer:
            self.writer.execute("INSERT INTO receipts VALUES (4,'north',10,12)")
        for sql in ("DELETE FROM receipts", "UPDATE receipts SET cents=19",
                    "INSERT OR REPLACE INTO receipts VALUES (4,'north',10,99)",
                    "INSERT INTO receipts VALUES (2,'north',7,12)"):
            with self.assertRaises(sqlite3.IntegrityError), self.writer:
                self.writer.execute(sql)
        with closing(sqlite3.connect(self.path)) as reader:
            self.writer.execute("INSERT INTO receipts(store,event_time,cents) VALUES ('north',0,88)")
            result = page(reader, "north")
            self.assertEqual(result["through_id"], 4)
            self.assertEqual([r["cents"] for r in result["items"]], [12, 5])
            self.writer.rollback()
            self.assertEqual(page(reader, "north"), result)

    def test_live_offset_drift_and_deep_page_work(self):
        report = run(self.path.with_name("demo.db"))
        self.assertEqual(report["drift"], {"first": [4, 3], "live_offset_next": [3, 2],
                                         "fixed_next": [2, 1], "fresh_scan": [5, 4, 3, 2, 6, 1]})
        deep = report["deep_page"]
        self.assertGreater(deep["offset_query"]["progress_calls"], deep["cursor_query"]["progress_calls"] * 10)


if __name__ == "__main__":
    unittest.main()
