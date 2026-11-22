"""Import a timestamped event feed and reproduce weekly signup-cohort retention."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3

RETENTION = Path(__file__).with_name("retention.sql").read_text(encoding="utf-8")
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY NOT NULL,
    person TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('signup', 'active')),
    occurred_at INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL CHECK (recorded_at >= occurred_at)
);
CREATE INDEX IF NOT EXISTS event_timeline ON events(kind, person, occurred_at, recorded_at);
"""


def instant(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value):
        raise ValueError(f"expected timestamp with seconds and explicit offset, got {value!r}")
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def event_row(raw):
    if not isinstance(raw, dict):
        raise ValueError("expected an event object")
    for key in ("id", "person"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ValueError(f"expected nonempty {key}")
    if raw.get("kind") not in ("signup", "active"):
        raise ValueError("kind must be signup or active")
    occurred, recorded = instant(raw.get("occurred_at")), instant(raw.get("recorded_at"))
    if recorded < occurred:
        raise ValueError("recorded_at precedes occurred_at")
    return (raw["id"], raw["person"], raw["kind"], occurred, recorded)


def ingest(db, stream):
    inserted = repeated = 0
    db.executescript(SCHEMA)
    with db:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = event_row(json.loads(line))
                changed = db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING", row)
                if changed.rowcount:
                    inserted += 1
                else:
                    saved = db.execute("SELECT id, person, kind, occurred_at, recorded_at FROM events WHERE id = ?", row[:1]).fetchone()
                    if saved != row:
                        raise ValueError(f"conflicting copies of event {row[0]!r}")
                    repeated += 1
            except ValueError as error:
                raise ValueError(f"line {number}: {error}") from error
    return {"inserted": inserted, "repeated": repeated}


def report(db, through, as_of, last_week):
    if as_of < through or not 0 <= last_week <= 104:
        raise ValueError("as-of must be at or after through, and last-week must be 0 to 104")
    result = db.execute(RETENTION, {"through": through, "as_of": as_of, "last_week": last_week})
    return [{"cohort": cohort, "week": week, "members": members, "active": active,
             "retention": active / members if active is not None else None}
            for cohort, week, members, active in result]


def changes(db, through, before, after, last_week):
    if after < before:
        raise ValueError("after must be at or after before")
    # Both SELECTs must see the same imported feed, including during concurrent imports.
    with db:
        db.execute("BEGIN")
        initial = report(db, through, before, last_week)
        revised = report(db, through, after, last_week)

    def by_cell(rows):
        return {(r["cohort"], r["week"]): {k: r[k] for k in ("members", "active", "retention")} for r in rows}

    old, new = by_cell(initial), by_cell(revised)
    result = []
    for key in sorted(old.keys() | new.keys()):
        previous, current = old.get(key), new.get(key)
        if previous != current:
            result.append({"cohort": key[0], "week": key[1], "before": previous, "after": current})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("import")
    load.add_argument("database", type=Path)
    load.add_argument("feed", type=Path)
    read = commands.add_parser("report")
    diff = commands.add_parser("diff")
    for cmd in (read, diff):
        cmd.add_argument("database", type=Path)
        cmd.add_argument("--through", required=True, type=instant)
        cmd.add_argument("--last-week", type=int, default=4)
    read.add_argument("--as-of", required=True, type=instant)
    diff.add_argument("--before", required=True, type=instant)
    diff.add_argument("--after", required=True, type=instant)
    args = parser.parse_args()
    try:
        if args.command == "import":
            with args.feed.open(encoding="utf-8-sig") as feed, sqlite3.connect(args.database) as db:
                result = ingest(db, feed)
        else:
            uri = args.database.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as db:
                if args.command == "report":
                    result = report(db, args.through, args.as_of, args.last_week)
                else:
                    result = changes(db, args.through, args.before, args.after, args.last_week)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
