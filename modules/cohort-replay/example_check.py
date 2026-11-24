"""Replay the same reporting window before and after a late activity arrives."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent


def cli(*args):
    return json.loads(subprocess.check_output([sys.executable, ROOT / "cohort.py", *args], text=True))


def replay():
    with TemporaryDirectory() as directory:
        db = Path(directory) / "events.db"
        feed = ROOT / "examples/events.jsonl"
        assert cli("import", db, feed) == {"inserted": 8, "repeated": 0}
        assert cli("import", db, feed) == {"inserted": 0, "repeated": 8}
        def report(as_of):
            rows = cli("report", db, "--through", "2026-11-16T00:00:00Z", "--as-of", as_of, "--last-week", "2")
            for row in rows:
                expected = None if row["active"] is None else row["active"] / row["members"]
                assert row["retention"] == expected
            return [(r["cohort"], r["week"], r["members"], r["active"]) for r in rows]

        initial = report("2026-11-16T00:00:00Z")
        revised = report("2026-11-18T00:00:00Z")
        assert initial == [
            ("2026-11-02", 0, 2, 1), ("2026-11-02", 1, 2, 0), ("2026-11-02", 2, 2, None),
            ("2026-11-09", 0, 1, 1), ("2026-11-09", 1, 1, None), ("2026-11-09", 2, 1, None),
        ]
        assert revised == [initial[0], ("2026-11-02", 1, 2, 1), *initial[2:]]
        delta = cli("diff", db, "--through", "2026-11-16T00:00:00Z", "--before", "2026-11-16T00:00:00Z",
                    "--after", "2026-11-18T00:00:00Z", "--last-week", "2")
        assert delta == [{"cohort": "2026-11-02", "week": 1,
                          "before": {"members": 2, "active": 0, "retention": 0},
                          "after": {"members": 2, "active": 1, "retention": 0.5}}]
        print("2026-11-02, week 1: initial 0/2, revised 1/2; week 2 remains unobserved")


if __name__ == "__main__":
    replay()
