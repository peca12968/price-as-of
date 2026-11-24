from datetime import datetime, timedelta
import json
import random
import subprocess
import sys
import unittest

from audit import timestamp
from price_schedule import compare_schedule
from test_audit import HISTORY, ROOT, replay


def cli(sku, start, stop, before, after):
    return subprocess.run([sys.executable, ROOT / "price_schedule.py", HISTORY, "--sku", sku,
                           "--effective-from", start, "--effective-until", stop,
                           "--from-as-of", before, "--as-of", after],
                          capture_output=True, text=True, timeout=20)


class ScheduleTests(unittest.TestCase):
    def test_published_sample_is_exact_cli_output(self):
        examples = json.loads((ROOT / "examples/effective-schedule.json").read_text(encoding="utf-8"))
        self.assertEqual([row["sku"] for row in examples], ["KIT", "GIFT", "KIT"])
        for example in examples:
            result = cli(*(example[key] for key in ("sku", "effective_from", "effective_until", "from_cutoff", "cutoff")))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), example)
        kit, gift, provenance = [row["segments"] for row in examples]
        self.assertEqual([row["unit_delta_cents"] for row in kit], [0, 150, -50, 50])
        self.assertEqual([row["change"] for row in gift], ["unchanged", "status_changed"])
        self.assertEqual([price["status"] for price in (gift[1]["before"], gift[1]["after"])], ["ambiguous", "priced"])
        self.assertIsNone(gift[1]["unit_delta_cents"])
        self.assertEqual([row["change"] for row in provenance], ["unchanged", "source_changed"])
        self.assertEqual([provenance[1][view]["sources"][0]["revision"] for view in ("before", "after")], [2, 3])
        self.assertEqual(provenance[1]["unit_delta_cents"], 0)

    def test_half_open_microseconds_with_exact_cutoff_and_moved_boundary(self):
        start = timestamp("2026-01-01T08:00:00+08:00")

        def at(microseconds):
            return start + timedelta(microseconds=microseconds)

        revisions = [
            ("free", 1, "X", "set", at(0), at(-3), 0),
            ("offer", 1, "X", "set", at(1), at(-2), 5),
            ("offer", 2, "X", "set", at(4), at(0), 7),
            ("unseen", 1, "X", "set", at(2), at(1), 99),
            ("other", 1, "Y", "set", at(3), at(-1), 80),
        ]
        result = compare_schedule(revisions[::-1], "X", at(0), at(4), at(-1), at(0))
        first, second = result["segments"]
        self.assertEqual(first["effective_from"], "2026-01-01T00:00:00Z")
        self.assertEqual(first["effective_until"], "2026-01-01T00:00:00.000001Z")
        self.assertEqual((first["change"], first["unit_delta_cents"]), ("unchanged", 0))
        self.assertEqual((second["before"]["unit_cents"], second["after"]["unit_cents"]), (5, 0))
        self.assertEqual(second["effective_until"], "2026-01-01T00:00:00.000004Z")

    def test_empty_schedule_and_invalid_intervals(self):
        day = "2026-04-01T00:00:00Z"
        later = "2026-04-02T00:00:00Z"
        result = cli("unknown", day, later, day, day)
        self.assertEqual(result.returncode, 0, result.stderr)
        segment, = json.loads(result.stdout)["segments"]
        self.assertEqual((segment["effective_from"], segment["effective_until"]), (day, later))
        self.assertEqual(segment["before"], segment["after"])
        self.assertEqual((segment["before"]["status"], segment["change"], segment["unit_delta_cents"]),
                         ("missing", "unchanged", None))
        for scope in [("X", day, day, day, later), ("X", day, later, later, day),
                      ("X", "2026-04-01", later, day, later), (" X ", day, later, day, later)]:
            result = cli(*scope)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")

    def test_entire_microsecond_range_matches_independent_replay(self):
        rng = random.Random(221)
        base = datetime(2026, 1, 1)

        def at(microseconds):
            return base + timedelta(microseconds=microseconds)

        revisions = []
        for index in range(24):
            recorded = rng.randrange(-15, 12)
            for version in range(1, 5):
                recorded += rng.randrange(1, 8)
                withdrawn = version > 1 and rng.randrange(5) == 0
                action = "withdraw" if withdrawn else "set"
                effective = None if withdrawn else at(rng.randrange(-10, 75))
                cents = None if withdrawn else rng.randrange(5)
                revisions.append((f"change-{index:02}", version, f"SKU{index % 3}", action, effective, at(recorded), cents))
        rng.shuffle(revisions)
        for sku in ("SKU0", "SKU1", "SKU2"):
            for before, after in ((-8, 0), (3, 25), (11, 40)):
                with self.subTest(sku=sku, before=before, after=after):
                    report = compare_schedule(revisions, sku, at(0), at(60), at(before), at(after))
                    segments = []
                    for step in range(60):
                        pair = tuple(replay(revisions, sku, at(step), at(cutoff)) for cutoff in (before, after))
                        if not segments or pair != segments[-1][1]:
                            segments.append((step, pair))
                    bounds = [start for start, _ in segments] + [60]
                    expected = [(at(start), at(stop), *pair) for (start, pair), stop in zip(segments, bounds[1:])]
                    actual = [(timestamp(row["effective_from"]), timestamp(row["effective_until"]), row["before"], row["after"])
                              for row in report["segments"]]
                    self.assertEqual(actual, expected)
