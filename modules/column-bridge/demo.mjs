import assert from "node:assert/strict";
import { backfill, phase, withDatabase } from "./drill.mjs";

await withDatabase(async (migration, connect) => {
  const oldApp = await connect();
  const newApp = await connect();
  await phase(migration, "legacy");
  await oldApp.query("INSERT INTO tickets (id, bin_code) VALUES (1,'A1'),(2,'A2'),(3,'A3'),(4,'A4')");
  await phase(migration, "expand");
  await oldApp.query("BEGIN");
  await oldApp.query("SELECT id FROM tickets WHERE id=1 FOR UPDATE");
  const first = await backfill(migration, 10);
  const skipped = await backfill(migration, 10);
  assert.deepEqual(first, [2, 3, 4]);
  assert.deepEqual(skipped, []);
  await assert.rejects(phase(migration, "cutover"), {code: "23514"});
  const early = await newApp.query("SELECT pick_slot FROM tickets_v2 WHERE id=1");
  assert.equal(early.rows[0].pick_slot, "A1");
  console.log(JSON.stringify({backfilled: first, second_batch: skipped, cutover: "blocked", v2_reads: "A1"}));

  await oldApp.query("COMMIT");
  const resumed = await backfill(migration, 10);
  assert.deepEqual(resumed, [1]);
  await phase(migration, "cutover");
  await newApp.query("UPDATE tickets_v2 SET pick_slot='B7' WHERE id=1");
  const legacyRead = await oldApp.query("SELECT bin_code FROM tickets WHERE id=1");
  assert.equal(legacyRead.rows[0].bin_code, "B7");
  await oldApp.query("UPDATE tickets SET bin_code='C9' WHERE id=1");
  console.log(JSON.stringify({resumed, cutover: "passed", old_app_read_after_v2_write: "B7"}));

  await phase(migration, "contract");
  const final = await newApp.query("SELECT pick_slot FROM tickets_v2 WHERE id=1");
  assert.equal(final.rows[0].pick_slot, "C9");
  console.log(JSON.stringify({legacy_column: "retired", final_slot: final.rows[0].pick_slot}));
});
