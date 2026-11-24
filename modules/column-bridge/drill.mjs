import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import pg from "pg";

const scripts = Object.fromEntries(["legacy", "expand", "backfill", "cutover", "contract"].map(name =>
  [name, readFileSync(new URL("./sql/" + name + ".sql", import.meta.url), "utf8")]));

export async function phase(client, name) {
  if (!["legacy", "expand", "cutover", "contract"].includes(name)) throw new Error("Unknown phase");
  await client.query("BEGIN");
  try {
    await client.query("SET LOCAL lock_timeout = '1s'");
    await client.query(scripts[name]);
    await client.query("COMMIT");
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  }
}

export async function backfill(client, limit) {
  if (!Number.isSafeInteger(limit) || limit < 1) throw new RangeError("Batch size must be positive");
  const result = await client.query(scripts.backfill, [limit]);
  return result.rows.map(row => row.id).sort((a, b) => a - b);
}

// Each drill owns its generated schema and all connections that use it.
export async function withDatabase(run) {
  if (!process.env.DATABASE_URL) throw new Error("Set DATABASE_URL to the rehearsal database");
  const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });
  const schema = "bridge_" + randomUUID().replaceAll("-", "");
  const clients = [];
  async function connect() {
    const client = await pool.connect();
    clients.push(client);
    await client.query("SET search_path TO " + schema);
    return client;
  }
  try {
    await pool.query("CREATE SCHEMA " + schema);
    try {
      await run(await connect(), connect);
    } finally {
      for (const client of clients) client.release(true);
      await pool.query("DROP SCHEMA " + schema + " CASCADE");
    }
  } finally {
    await pool.end();
  }
}
