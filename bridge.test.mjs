import assert from "node:assert/strict";
import test from "node:test";
import { backfill, phase, withDatabase } from "./drill.mjs";

test("two workers do disjoint work and a rolled-back batch becomes available again", () =>
  withDatabase(async (db, connect) => {
    await phase(db, "legacy");
    await db.query("INSERT INTO tickets SELECT n, 'A' || n FROM generate_series(1,7) n");
    await phase(db, "expand");
    const first = await connect();
    await first.query("BEGIN");
    assert.deepEqual(await backfill(first, 3), [1, 2, 3]);
    assert.deepEqual(await backfill(db, 3), [4, 5, 6]);
    assert.deepEqual(await backfill(db, 3), [7]);
    assert.deepEqual(await backfill(db, 3), []);
    await assert.rejects(phase(db, "cutover"), {code: "23514"});
    await first.query("ROLLBACK");
    assert.deepEqual(await backfill(db, 3), [1, 2, 3]);
    assert.deepEqual(await backfill(db, 3), []);
    await phase(db, "cutover");
    await phase(db, "cutover");
    assert.equal((await db.query("SELECT count(*)::int AS n FROM tickets WHERE bin_code=pick_slot")).rows[0].n, 7);
  }));

test("old and new insert and update paths coexist before and after switching the view", () =>
  withDatabase(async (db, connect) => {
    const old = await connect();
    const modern = await connect();
    await phase(db, "legacy");
    await old.query("INSERT INTO tickets VALUES (0,'PRE')");
    await phase(db, "expand");
    const historical = await modern.query("SELECT pick_slot FROM tickets_v2 WHERE id=0");
    assert.equal(historical.rows[0].pick_slot, "PRE");
    await old.query("UPDATE tickets SET bin_code='EDIT' WHERE id=0");
    assert.deepEqual(await backfill(db, 1), []);
    for (const id of [1, 3]) {
      if (id === 3) await phase(db, "cutover");
      await old.query("INSERT INTO tickets(id, bin_code) VALUES ($1,'OLD')", [id]);
      await modern.query("INSERT INTO tickets_v2(id, pick_slot) VALUES ($1,'NEW')", [id+1]);
      await modern.query("UPDATE tickets_v2 SET pick_slot='FROM_V2' WHERE id=$1", [id]);
      await old.query("UPDATE tickets SET bin_code='FROM_V1' WHERE id=$1", [id+1]);
      const values = await db.query("SELECT bin_code,pick_slot FROM tickets WHERE id >= $1 ORDER BY id", [id]);
      assert.deepEqual(values.rows, [{bin_code:"FROM_V2",pick_slot:"FROM_V2"}, {bin_code:"FROM_V1",pick_slot:"FROM_V1"}]);
    }
    await assert.rejects(db.query("UPDATE tickets SET bin_code='X',pick_slot='Y' WHERE id=0"), {code:"23514"});
    await assert.rejects(modern.query("UPDATE tickets_v2 SET pick_slot=NULL WHERE id=0"), {code:"23514"});
    assert.equal((await old.query("SELECT bin_code FROM tickets WHERE id=0")).rows[0].bin_code, "EDIT");
    await phase(db, "contract");
    await modern.query("INSERT INTO tickets_v2 VALUES (5,'FINAL')");
    assert.equal((await modern.query("SELECT pick_slot FROM tickets_v2 WHERE id=5")).rows[0].pick_slot, "FINAL");
    await assert.rejects(old.query("SELECT bin_code FROM tickets"), {code:"42703"});
    await assert.rejects(modern.query("INSERT INTO tickets_v2 VALUES (6,NULL)"), {code:"23502"});
  }));

test("early contract and lock timeout leave both interfaces usable for retry", () =>
  withDatabase(async (db, connect) => {
    await phase(db, "legacy");
    await phase(db, "expand");
    await db.query("INSERT INTO tickets VALUES (1,'A')");
    await assert.rejects(phase(db, "contract"), {code:"2BP01"});
    await db.query("UPDATE tickets SET bin_code='B' WHERE id=1");
    assert.equal((await db.query("SELECT pick_slot FROM tickets WHERE id=1")).rows[0].pick_slot, "B");
    await phase(db, "cutover");
    const reader = await connect();
    await reader.query("BEGIN");
    await reader.query("SELECT bin_code FROM tickets");
    await assert.rejects(phase(db, "contract"), {code:"55P03"});
    await reader.query("COMMIT");
    await db.query("UPDATE tickets_v2 SET pick_slot='C' WHERE id=1");
    assert.equal((await db.query("SELECT bin_code FROM tickets WHERE id=1")).rows[0].bin_code, "C");
    await phase(db, "contract");
  }));
