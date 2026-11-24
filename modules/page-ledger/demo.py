"""Measure two ways to fetch the same deep page and reproduce drift between real connections."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile

from ledger import AFTER, FIRST, append, initialize, page


def measure(db, query, params):
    calls = 0

    def tick():
        nonlocal calls
        calls += 1
        return 0

    plan = [r[3] for r in db.execute("EXPLAIN QUERY PLAN " + query, params)]
    db.set_progress_handler(tick, 1)
    try:
        rows = db.execute(query, params).fetchall()
    finally:
        db.set_progress_handler(None, 0)
    return rows, {"progress_calls": calls, "plan": plan}


def run(path):
    with closing(sqlite3.connect(path)) as reader, closing(sqlite3.connect(path)) as writer:
        initialize(writer)
        for ts in [10, 20, 20, 30]:
            append(writer, "north", ts, 100)
        first = page(reader, "north", 2)
        append(writer, "north", 40, 300)
        append(writer, "north", 15, 200)
        naive = reader.execute("SELECT id FROM receipts WHERE store=? ORDER BY event_time DESC, id DESC LIMIT 2 OFFSET 2",
                               ("north",)).fetchall()
        second = page(reader, "north", 2, first["next"])
        drift = {"first": [r["id"] for r in first["items"]], "live_offset_next": [r[0] for r in naive],
                 "fixed_next": [r["id"] for r in second["items"]],
                 "fresh_scan": [r["id"] for r in page(reader, "north")["items"]]}

        total, offset, fetch = 20000, 18000, 21
        with writer:
            writer.executemany("INSERT INTO receipts(store,event_time,cents) VALUES ('bench',?,?)",
                               ((n // 8, 100 + n % 77) for n in range(total)))
        fence, = reader.execute("SELECT max(id) FROM receipts").fetchone()
        # Benchmark continuation from an already obtained boundary, as in ordinary paging.
        last_id, ts, _ = reader.execute(FIRST + " OFFSET ?", ("bench", fence, 1, offset - 1)).fetchone()
        by_offset, scan = measure(reader, FIRST + " OFFSET ?", ("bench", fence, fetch, offset))
        by_key, seek = measure(reader, AFTER, ("bench", fence, ts, last_id, fetch))
        if by_key != by_offset or len(by_key) != fetch:
            raise RuntimeError("The two queries returned different deep pages")
        return {"sqlite": sqlite3.sqlite_version, "drift": drift,
                "deep_page": {"store_rows": total, "offset": offset, "rows_including_lookahead": fetch,
                              "offset_query": scan, "cursor_query": seek}}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        print(json.dumps(run(Path(tmp) / "sample.db"), indent=2))
