from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest

from cohort import changes, ingest, instant, report, SCHEMA

DAY = 86400
WEEK = 7 * DAY
MONDAY = instant("2026-11-02T00:00:00Z")


def direct_count(events, through, as_of, horizon):
    visible = [e for e in events if e[3] < through and e[4] < as_of]
    groups = {}
    for person in {e[1] for e in visible}:
        signups = [e[3] for e in visible if e[1:3] == (person, "signup")]
        if not signups:
            continue
        start = min(signups)
        day = datetime.fromtimestamp(start, timezone.utc).date()
        monday = day - timedelta(days=day.weekday())
        groups.setdefault(monday, {})[person] = start
    rows = []
    for monday, people in sorted(groups.items()):
        for week in range(horizon + 1):
            start = (monday - date(1970, 1, 1)).days * DAY + week * WEEK
            active = None
            if start + WEEK <= through:
                active = len({person for _, person, kind, timestamp, _ in visible
                              if person in people and kind == "active" and timestamp >= people[person]
                              and start <= timestamp < start + WEEK})
            rows.append({"cohort": monday.isoformat(), "week": week, "members": len(people),
                         "active": active, "retention": None if active is None else active / len(people)})
    return rows


class RetentionTests(unittest.TestCase):
    def test_sql_agrees_with_direct_person_counts(self):
        rng = random.Random(12968)
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript(SCHEMA)
            for trial in range(40):
                base = MONDAY + trial * WEEK
                events = []
                for i in range(80):
                    when = base + rng.randrange(28) * DAY + rng.choice([-1, 0, 1, 43200])
                    events.append((str(i), str(rng.randrange(9)), rng.choice(["signup", "active", "active"]),
                                   when, when + rng.choice([0, 1, 3 * DAY, 10 * DAY])))
                db.execute("DELETE FROM events")
                db.executemany("INSERT INTO events VALUES (?,?,?,?,?)", events)
                for end in [base + 2 * WEEK, base + 3 * WEEK - 1, base + 4 * WEEK]:
                    for cutoff in [end, end + 5 * DAY]:
                        self.assertEqual(report(db, end, cutoff, 4), direct_count(events, end, cutoff, 4))

    def test_late_signup_moves_person_out_of_a_cohort(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript(SCHEMA)
            with db:
                db.executemany("INSERT INTO events VALUES (?,?,?,?,?)", [
                    ("one", "A", "signup", MONDAY + WEEK, MONDAY + WEEK),
                    ("two", "A", "active", MONDAY + WEEK, MONDAY + WEEK),
                    ("old", "A", "signup", MONDAY, MONDAY + 3 * WEEK),
                ])
            diff = changes(db, MONDAY + 2 * WEEK, MONDAY + 3 * WEEK, MONDAY + 3 * WEEK + 1, 1)
            self.assertEqual([(r["cohort"], r["week"]) for r in diff], [
                ("2026-11-02", 0), ("2026-11-02", 1), ("2026-11-09", 0), ("2026-11-09", 1)])
            self.assertIsNone(diff[0]["before"])
            self.assertEqual(diff[1]["after"]["active"], 1)
            self.assertEqual(diff[2]["before"]["active"], 1)
            self.assertIsNone(diff[3]["before"]["active"])
            self.assertTrue(all(row["after"] is None for row in diff[2:]))

    def test_overlapping_imports_and_atomic_conflict(self):
        record = {"id": "one", "person": "A", "kind": "signup", "occurred_at": "2026-11-02T08:00:00+08:00",
                  "recorded_at": "2026-11-02T00:00:00Z"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.sqlite"
            def load(_):
                with closing(sqlite3.connect(path)) as db:
                    return ingest(db, [json.dumps(record)])
            with ThreadPoolExecutor(max_workers=4) as workers:
                results = list(workers.map(load, range(4)))
            self.assertEqual(sum(r["inserted"] for r in results), 1)
            self.assertEqual(sum(r["repeated"] for r in results), 3)
            with closing(sqlite3.connect(path)) as db:
                equivalent = dict(record, occurred_at="2026-11-02T00:00:00Z")
                self.assertEqual(ingest(db, [json.dumps(equivalent)])["repeated"], 1)
                for invalid in [json.dumps(dict(record, person="B")), "{", json.dumps(dict(record, kind="other"))]:
                    with self.assertRaisesRegex(ValueError, "line 2"):
                        ingest(db, [json.dumps(dict(record, id="new")), invalid])
                    self.assertEqual(db.execute("SELECT id FROM events").fetchall(), [("one",)])

    def test_time_inputs(self):
        self.assertEqual(instant("2026-11-01T16:00:00-08:00"), MONDAY)
        for value in ["2026-11-02", "2026-11-02T08:00:00", "2026-02-30T12:00:00Z", "2026-11-02T08:00:00+01:70"]:
            with self.assertRaises(ValueError):
                instant(value)
        with closing(sqlite3.connect(":memory:")) as db:
            for end, as_of, last in [(2, 1, 2), (1, 2, -1), (1, 2, 105)]:
                with self.assertRaises(ValueError):
                    report(db, end, as_of, last)


if __name__ == "__main__":
    unittest.main()
