"""Browse an append-only receipt ledger by event time with a fixed ingestion boundary."""
import argparse
import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import uuid


MAX_INT = (1 << 63) - 1
SELECT = "SELECT id, event_time, cents FROM receipts WHERE store=? AND id<=?"
ORDER = " ORDER BY event_time DESC, id DESC LIMIT ?"
FIRST = SELECT + ORDER
AFTER = SELECT + " AND (event_time, id)<(?, ?)" + ORDER


def initialize(db):
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE ledger_info (identity TEXT NOT NULL, last_id INTEGER NOT NULL)")
        db.execute("INSERT INTO ledger_info VALUES (?, 0)", (uuid.uuid4().hex,))
        db.execute("""CREATE TABLE receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, store TEXT NOT NULL,
            event_time INTEGER NOT NULL, cents INTEGER NOT NULL
        ) STRICT""")
        db.execute("CREATE INDEX receipt_order ON receipts(store, event_time DESC, id DESC)")
        for operation in ("UPDATE", "DELETE"):
            db.execute(f"""CREATE TRIGGER immutable_{operation} BEFORE {operation} ON receipts BEGIN
                SELECT RAISE(ABORT, 'receipts are append only');
            END""")
        db.execute("""CREATE TRIGGER advance_boundary AFTER INSERT ON receipts BEGIN
            SELECT RAISE(ABORT, 'receipt IDs must advance') WHERE NEW.id <= (SELECT last_id FROM ledger_info);
            UPDATE ledger_info SET last_id=NEW.id;
        END""")


def integer(value):
    return type(value) is int and -MAX_INT - 1 <= value <= MAX_INT


def append(db, store, event_time, cents):
    if not store.strip() or not integer(event_time) or not integer(cents):
        raise ValueError("Expected a store and signed 64-bit integers for event time and cents")
    with db:
        row = db.execute("INSERT INTO receipts(store,event_time,cents) VALUES (?,?,?)", (store, event_time, cents))
    return row.lastrowid


def pack(identity, store, fence, event_time, last_id):
    data = json.dumps([1, identity, store, fence, event_time, last_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(data).decode("ascii")


def unpack(token, identity, store):
    try:
        data = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
        version, source, scope, fence, event_time, last_id = data
        if (not isinstance(data, list) or type(version) is not int or version != 1 or
                source != identity or scope != store or not all(map(integer, (fence, event_time, last_id))) or
                not 0 < last_id <= fence):
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid cursor for this ledger or store") from exc
    return fence, event_time, last_id


def page(db, store, limit=20, after=None):
    if not store.strip() or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Expected a store and page size between 1 and 200")
    identity, fence = db.execute("SELECT identity, last_id FROM ledger_info").fetchone()
    if after is None:
        rows = db.execute(FIRST, (store, fence, limit + 1)).fetchall()
    else:
        fence, event_time, last_id = unpack(after, identity, store)
        rows = db.execute(AFTER, (store, fence, event_time, last_id, limit + 1)).fetchall()
    more = len(rows) > limit
    items = [{"id": key, "event_time": ts, "cents": cents} for key, ts, cents in rows[:limit]]
    cursor = pack(identity, store, fence, items[-1]["event_time"], items[-1]["id"]) if more else None
    return {"store": store, "through_id": fence, "items": items, "next": cursor}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "append", "page"):
        cmd = sub.add_parser(name)
        cmd.add_argument("database", type=Path)
        if name != "init":
            cmd.add_argument("store")
        if name == "append":
            cmd.add_argument("event_time", type=int, help="Unix seconds")
            cmd.add_argument("cents", type=int)
        elif name == "page":
            cmd.add_argument("--limit", type=int, default=20)
            cmd.add_argument("--after")
    args = parser.parse_args()
    mode = {"init": "rwc", "append": "rw", "page": "ro"}[args.command]
    try:
        with closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=" + mode, uri=True)) as db:
            if args.command == "init":
                initialize(db)
                result = {"initialized": True}
            elif args.command == "append":
                result = {"id": append(db, args.store, args.event_time, args.cents)}
            else:
                result = page(db, args.store, args.limit, args.after)
        print(json.dumps(result))
        return 0
    except (sqlite3.Error, ValueError, OSError) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
