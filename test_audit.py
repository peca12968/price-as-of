import json
import random
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import audit


ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "examples/revisions.csv"
ORDERS = ROOT / "examples/orders.csv"
FINAL = "2026-04-23T00:00:00Z"


def cli(history=HISTORY, orders=ORDERS, cutoff=FINAL, *, flags=()):
    return subprocess.run([sys.executable, ROOT / "audit.py", history, orders, "--as-of", cutoff, *flags],
                          capture_output=True, text=True, timeout=20)


def replay(history, sku, placed, known):
    """Replay the visible edits in recording order, then examine the active prices."""
    active = {}
    for change, version, product, action, effective, recorded, cents in sorted(history, key=lambda r: r[5]):
        if product == sku and recorded <= known:
            if action == "withdraw":
                active.pop(change, None)
            else:
                active[change] = (effective, version, cents, recorded)
    eligible = {change: row for change, row in active.items() if row[0] <= placed}
    latest = max((row[0] for row in eligible.values()), default=None)
    sources = [{"change_id": key, "revision": row[1], "unit_cents": row[2],
                "effective_at": row[0].isoformat() + "Z", "recorded_at": row[3].isoformat() + "Z"}
               for key, row in sorted(eligible.items()) if row[0] == latest]
    status = "missing" if not sources else "priced" if len(sources) == 1 else "ambiguous"
    return {"status": status, "sources": sources,
            "unit_cents": sources[0]["unit_cents"] if len(sources) == 1 else None}


class PriceTests(unittest.TestCase):
    def test_fixture_and_distinct_provenance(self):
        result = cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["comparable_orders"], report["excluded_orders"], report["comparable_delta_cents"]),
                         (3, 3, 450))
        rows = {r["order_id"]: r for r in report["orders"]}
        self.assertEqual([rows[key]["delta_cents"] for key in ["K11", "K13", "K16"]], [0, 300, 150])
        for key, original, restated in [("K13", ("spring", 1), ("base", 1)),
                                        ("K16", ("flash", 1), ("spring", 2))]:
            for view, expected in [("at_order", original), ("at_audit", restated)]:
                source, = rows[key][view]["sources"]
                self.assertEqual((source["change_id"], source["revision"]), expected)
        self.assertEqual([s["change_id"] for s in rows["G07"]["at_order"]["sources"]], ["gift-a", "gift-b"])
        self.assertEqual(rows["B04"]["at_order"]["status"], "missing")
        self.assertEqual(rows["B04"]["at_audit"]["unit_cents"], 500)
        self.assertIsNone(rows["X04"]["delta_cents"])

    def test_boundaries_zero_price_and_equal_price_conflicts(self):
        moment = audit.timestamp("2026-01-01T08:00:00+08:00")
        prior = moment - timedelta(microseconds=1)
        later = moment + timedelta(microseconds=1)
        history = [("A", 1, "X", "set", prior, prior, 0), ("A", 2, "X", "set", moment, moment, 5),
                   ("B", 1, "X", "set", moment, moment, 5), ("B", 2, "X", "withdraw", None, later, None)]
        orders = [("before", "X", prior, 1), ("at", "X", moment, 1)]
        rows = {r["order_id"]: r for r in audit.audit(history, orders, later)["orders"]}
        self.assertEqual(rows["before"]["at_order"]["unit_cents"], 0)
        self.assertEqual(rows["before"]["at_audit"]["status"], "missing")
        self.assertEqual(rows["at"]["at_order"]["status"], "ambiguous")
        self.assertEqual(rows["at"]["at_audit"]["unit_cents"], 5)
        self.assertIsNone(audit.audit([], orders, later)["comparable_delta_cents"])
        self.assertEqual(audit.audit(history, [], later)["orders"], [])

    def test_history_groups_same_instant_and_keeps_source_only_changes(self):
        placed = datetime(2026, 1, 3)
        early = placed - timedelta(days=2)
        promotion = placed - timedelta(days=1)
        conflict, skipped, restored, refreshed, later = (
            placed + timedelta(microseconds=i) for i in range(1, 6)
        )
        revisions = [
            ("base", 1, "KIT", "set", early, early, 100),
            ("a", 1, "KIT", "set", promotion, conflict, 0),
            ("b", 1, "KIT", "set", promotion, conflict, 0),
            ("future", 1, "KIT", "set", placed + timedelta(days=1), skipped, 3),
            ("b", 2, "KIT", "withdraw", None, restored, None),
            ("a", 2, "KIT", "withdraw", None, restored, None),
            ("base", 2, "KIT", "set", early, refreshed, 100),
            ("base", 3, "KIT", "set", early, later, 200),
        ]
        orders = [("past", "KIT", placed, 2), ("future", "KIT", later, 2)]
        report = audit.audit(revisions[::-1], orders, refreshed, history_order="past")
        self.assertEqual(report["order_id"], "past")
        steps = report["history"]
        moments = [placed, conflict, restored, refreshed]
        self.assertEqual([s["known_at"] for s in steps], [t.isoformat() + "Z" for t in moments])
        for step, time in zip(steps, moments):
            self.assertEqual({field: step[field] for field in ("status", "unit_cents", "sources")},
                             replay(revisions, "KIT", placed, time))
        self.assertEqual(steps[0]["recorded_revisions"], [])
        self.assertEqual(steps[1]["recorded_revisions"], [
            {"change_id": key, "revision": 1, "action": "set"} for key in ("a", "b")
        ])
        self.assertEqual(steps[2]["recorded_revisions"], [
            {"change_id": key, "revision": 2, "action": "withdraw"} for key in ("a", "b")
        ])

    def test_history_demo_matches_cli(self):
        data = json.loads((ROOT / "examples/history.json").read_text(encoding="utf-8"))
        self.assertEqual([row["order_id"] for row in data], ["K13", "K16", "G07", "B04"])
        for sample in data:
            result = cli(flags=("--history", sample["order_id"]))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), sample)
        self.assertEqual(cli(flags=("--history", "unknown")).returncode, 2)
        self.assertEqual(cli(flags=("--history", "K16", "--parquet", "unused.parquet")).returncode, 2)

    def test_randomized_sql_against_ordered_replay(self):
        rng = random.Random(7401)
        epoch = datetime(2026, 1, 1)
        def hour(n):
            return epoch + timedelta(hours=n)
        history = []
        for i in range(45):
            recorded = rng.randrange(12)
            sku = f"SKU-{i % 5}"
            for v in range(1, 5):
                recorded += rng.randint(1, 16)
                withdrawn = v > 1 and rng.random() < 0.2
                history.append((f"C{i}", v, sku, "withdraw" if withdrawn else "set",
                                None if withdrawn else hour(rng.randrange(36)), hour(recorded),
                                None if withdrawn else rng.randrange(1001)))
        rng.shuffle(history)
        orders = [(f"O{i}", f"SKU-{rng.randrange(6)}", hour(rng.randrange(36)), rng.randrange(1, 10))
                  for i in range(240)]
        states = set()
        for cutoff in [hour(35), hour(45), hour(72)]:
            observed = {r["order_id"]: r for r in audit.audit(history, orders, cutoff)["orders"]}
            for key, sku, placed, qty in orders:
                original = replay(history, sku, placed, placed)
                restated = replay(history, sku, placed, cutoff)
                self.assertEqual(observed[key]["at_order"], original)
                self.assertEqual(observed[key]["at_audit"], restated)
                prices = original["unit_cents"], restated["unit_cents"]
                delta = None if None in prices else (prices[1] - prices[0])*qty
                self.assertEqual(observed[key]["delta_cents"], delta)
                states.update([original["status"], restated["status"]])
        self.assertEqual(states, {"priced", "ambiguous", "missing"})

    def test_reordered_replayed_csv_and_invalid_inputs(self):
        header, *rows = HISTORY.read_text().splitlines()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.csv"
            path.write_text("\n".join([header, *reversed(rows), rows[0]]))
            self.assertEqual(cli(path).stdout, cli().stdout)
            for altered in [
                rows[0].replace("1000", "1001"), rows[0].replace("base,1", "base,3"),
                rows[0].replace("base,1,KIT", "base,2,OTHER"), rows[0].replace("base,1", "base,2"),
                rows[0].replace("1000", "-1"), rows[0].replace("00:00:00Z", "00:00:00.0000001Z"),
                rows[0].replace(",set,", ",withdraw,"),
            ]:
                path.write_text("\n".join([header, *rows, altered]))
                result = cli(path)
                self.assertEqual((result.returncode, result.stdout), (2, ""), result.stderr)
            path.write_text(ORDERS.read_text() + ORDERS.read_text().splitlines()[1] + "\n")
            self.assertEqual(cli(orders=path).returncode, 2)
            self.assertEqual(cli(cutoff="2026-04-15T00:00:00Z").returncode, 2)


if __name__ == "__main__":
    unittest.main()
