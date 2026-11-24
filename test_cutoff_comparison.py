from datetime import datetime, timedelta
import json
import random
import unittest

from audit import audit
from test_audit import ROOT, cli, replay


class CutoffComparisonTests(unittest.TestCase):
    def test_example_uses_same_orders_at_both_cutoffs(self):
        data = json.loads((ROOT / "examples/cutoff-diff.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data), 3)
        for expected in data:
            result = cli(cutoff=expected["cutoff"], flags=("--from-as-of", expected["from_cutoff"]))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), expected)
        early, later, source_only = data
        self.assertEqual((early["cohort_orders"], early["orders_after_from_cutoff"], early["net_delta_cents"]), (5, 1, 450))
        self.assertEqual((later["cohort_orders"], later["orders_after_from_cutoff"], later["net_delta_cents"]), (6, 0, 600))
        self.assertEqual(early["counts"], {"unchanged": 1, "status_changed": 2, "unit_price_changed": 2, "source_changed": 0})
        changes = {row["order_id"]: row for row in later["changes"]}
        self.assertEqual([changes[key]["delta_cents"] for key in ("G07", "B04")], [None, None])
        self.assertEqual((changes["G07"]["before"]["status"], changes["G07"]["after"]["status"]), ("ambiguous", "priced"))
        change, = source_only["changes"]
        self.assertEqual((change["order_id"], change["change"], change["delta_cents"]), ("K16", "source_changed", 0))
        self.assertEqual([change[view]["sources"][0]["revision"] for view in ("before", "after")], [2, 3])

    def test_gross_movements_do_not_cancel_and_source_only_changes_are_kept(self):
        start = datetime(2026, 1, 2)
        early = start - timedelta(days=1)
        end = start + timedelta(microseconds=1)
        limit = 2**63 - 1
        revisions = [
            ("up", 1, "A", "set", early, early, 0), ("up", 2, "A", "set", early, end, limit),
            ("down", 1, "B", "set", early, early, limit), ("down", 2, "B", "set", early, end, 0),
            ("flat", 1, "C", "set", early, early, 0), ("flat", 2, "C", "set", early, end, 0),
            ("conflict-a", 1, "D", "set", early, early, 100), ("conflict-b", 1, "D", "set", early, early, 200),
            ("conflict-a", 2, "D", "set", early, end, 110),
        ]
        orders = [(f"{key}-{i}", key, start, limit) for key in "ABC" for i in range(2)]
        orders.extend([("D-0", "D", start, 1), ("fresh", "C", end, 1)])
        report = audit(revisions[::-1], orders[::-1], end, from_cutoff=start)
        self.assertEqual(report["orders_after_from_cutoff"], 1)
        self.assertEqual((report["increase_cents"], report["decrease_cents"], report["net_delta_cents"]),
                         (2 * limit**2, 2 * limit**2, 0))
        self.assertEqual((report["comparable_orders"], report["excluded_orders"]), (6, 1))
        self.assertEqual(report["counts"], {"unchanged": 0, "status_changed": 0, "unit_price_changed": 4, "source_changed": 3})
        self.assertEqual([row["sku"] for row in report["by_sku"]], ["A", "B", "C", "D"])
        flat, conflict = (next(row for row in report["changes"] if row["order_id"] == key) for key in ("C-0", "D-0"))
        self.assertEqual((flat["delta_cents"], flat["after"]["sources"][0]["revision"]), (0, 2))
        self.assertEqual(conflict["change"], "source_changed")
        self.assertIsNone(conflict["delta_cents"])
        self.assertEqual([source["revision"] for source in conflict["after"]["sources"]], [2, 1])

    def test_two_cutoffs_match_independent_replay_in_random_placements(self):
        rng = random.Random(412)
        start = datetime(2026, 3, 1)

        def at(hour):
            return start + timedelta(hours=hour)

        revisions = []
        for index in range(32):
            recorded = rng.randrange(20)
            for version in range(1, 4):
                recorded += rng.randrange(1, 9)
                withdrawn = version > 1 and rng.randrange(5) == 0
                revisions.append((f"P{index}", version, f"SKU{index % 4}", "withdraw" if withdrawn else "set",
                                  None if withdrawn else at(rng.randrange(24)), at(recorded),
                                  None if withdrawn else rng.randrange(6) * 50))
        orders = [(f"O{index:03}", f"SKU{rng.randrange(5)}", at(rng.randrange(32)), rng.randrange(1, 8))
                  for index in range(120)]
        rng.shuffle(revisions)
        for lower, upper in ((0, 31), (13, 31), (22, 40), (31, 31)):
            with self.subTest(lower=lower, upper=upper):
                report = audit(revisions, orders, at(upper), from_cutoff=at(lower))
                expected_changes, deltas = {}, []
                for key, sku, placed, qty in orders:
                    if placed > at(lower):
                        continue
                    before, after = (replay(revisions, sku, placed, at(hour)) for hour in (lower, upper))
                    old, new = before["unit_cents"], after["unit_cents"]
                    delta = (new - old) * qty if old is not None and new is not None else None
                    if delta is not None:
                        deltas.append(delta)
                    if before != after:
                        expected_changes[key] = before, after, delta
                actual = {row["order_id"]: (row["before"], row["after"], row["delta_cents"]) for row in report["changes"]}
                self.assertEqual(actual, expected_changes)
                self.assertEqual(report["orders_after_from_cutoff"], sum(placed > at(lower) for _, _, placed, _ in orders))
                self.assertEqual(report["comparable_orders"], len(deltas))
                self.assertEqual(report["net_delta_cents"], sum(deltas) if deltas else None)

    def test_empty_cohort_and_invalid_boundaries(self):
        result = cli(flags=("--from-as-of", "2026-04-01T00:00:00Z"))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["cohort_orders"], report["orders_after_from_cutoff"], report["by_sku"]), (0, 6, []))
        self.assertIsNone(report["net_delta_cents"])
        result = cli(flags=("--from-as-of", "2026-04-24T08:00:00+08:00"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("comparison start must be at or before", result.stderr)
        result = cli(cutoff="2026-04-15T00:00:00Z", flags=("--from-as-of", "2026-04-13T00:00:00Z"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("audit cutoff precedes an order", result.stderr)
        self.assertEqual(cli(flags=("--from-as-of", "2026-04-13T00:00:00Z", "--history", "K13")).returncode, 2)


if __name__ == "__main__":
    unittest.main()
