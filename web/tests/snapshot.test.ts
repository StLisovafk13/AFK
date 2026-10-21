import { test } from "node:test";
import assert from "node:assert/strict";
import { gunzipSync } from "node:zlib";
import { PGlite } from "@electric-sql/pglite";
import { buildSnapshot } from "../scripts/snapshot";
import { deriveKey, unseal } from "../lib/snapshot-crypto";
import { createSnapshotStore } from "../lib/snapshot-store";
import { parseFilters } from "../lib/filters";
import type { Catalog, SnapshotRow } from "../lib/snapshot-types";

test("Neon export publishes encrypted chunks; browser store preserves pagination, filters, comments and map", async () => {
  const db = new PGlite();
  try {
    await db.exec(`CREATE TABLE items(id BIGSERIAL PRIMARY KEY,chat_id BIGINT,username TEXT,image_url TEXT,profile_url TEXT,latitude DOUBLE PRECISION,longitude DOUBLE PRECISION,created_at TEXT,source TEXT);
      CREATE TABLE links(id BIGSERIAL);CREATE TABLE comments(id BIGSERIAL,item_id BIGINT,comment TEXT,created_at TEXT);
      INSERT INTO items(chat_id,username,image_url,profile_url,latitude,longitude,created_at,source)
      SELECT CASE WHEN n%3=0 THEN -200 ELSE -100 END,CASE WHEN n%2=0 THEN 'alpha_private' ELSE 'beta_private' END,'https://private.example/'||n||'.jpg','https://vsco.co/private',CASE WHEN n%2=0 THEN 55.75 ELSE NULL END,37.61,'2026-10-21','test' FROM generate_series(1,105) n;
      INSERT INTO comments(item_id,comment,created_at) VALUES(105,'private comment','2026-10-21');INSERT INTO links DEFAULT VALUES;`);
    const files = new Map<string, Uint8Array>();
    const password = "a-random-test-password-with-entropy";
    const { manifest } = await buildSnapshot(
      async <T>(sql: string, values: unknown[] = []) => {
        assert.match(sql.trim(), /^SELECT\b/);
        return (await db.query<T>(sql, values)).rows;
      },
      password,
      async (path, bytes) => {
        files.set(path, bytes);
      },
      { batchSize: 17, chunkSize: 12 },
    );
    for (const [path, bytes] of files) {
      assert.ok(
        !new TextDecoder().decode(bytes).includes("alpha_private"),
        path,
      );
      assert.ok(
        !new TextDecoder().decode(bytes).includes("private comment"),
        path,
      );
    }
    const key = await deriveKey(password, manifest.salt, manifest.iterations);
    const read = async <T>(name: string): Promise<T> =>
      JSON.parse(
        gunzipSync(
          await unseal(files.get("data/" + manifest.id + "/" + name)!, key),
        ).toString(),
      );
    const catalog = await read<Catalog>("catalog.bin");
    const store = createSnapshotStore(catalog, (name) =>
      read<SnapshotRow[]>(name),
    );
    const f = parseFilters(new URLSearchParams());
    assert.equal((await store.summary()).items, 105);
    assert.equal((await store.summary()).profiles, 2);
    const first = await store.mediaPage(f);
    assert.equal(first.items.length, 48);
    assert.equal(first.items[0].id, "105");
    assert.equal(first.next, "58");
    const next = await store.mediaPage({ ...f, cursor: first.next! });
    assert.equal(next.items.length, 48);
    assert.equal(next.items[0].id, "57");
    const last = await store.mediaPage({ ...f, cursor: next.next! });
    assert.equal(last.items.length, 9);
    assert.equal(last.next, null);
    const alpha = await store.mediaPage({
      ...f,
      username: "alpha",
      geo: true,
      chat: "-200",
    });
    assert.equal(alpha.items.length, 17);
    assert.ok(
      alpha.items.every(
        (i) => i.username === "alpha_private" && i.chat_id === "-200",
      ),
    );
    assert.equal(
      (await store.mediaPage({ ...f, username: "alpha", exact: true })).items
        .length,
      0,
    );
    assert.equal(
      (await store.mediaPage({ ...f, username: "%' OR 1=1 --" })).items.length,
      0,
    );
    assert.deepEqual(
      (await store.profilePage(f)).items.map((i) => [i.username, i.count]),
      [
        ["beta_private", 53],
        ["alpha_private", 52],
      ],
    );
    assert.deepEqual(
      (await store.profilePage({ ...f, geo: true })).items.map((i) => [
        i.username,
        i.count,
      ]),
      [["alpha_private", 52]],
    );
    assert.equal((await store.mapPoints(f)).items.length, 52);
    assert.equal(
      (await store.mediaDetail("105"))?.comments[0].comment,
      "private comment",
    );
    assert.equal(await store.mediaDetail("999"), null);
    const wrongKey = await deriveKey(
      "wrong password",
      manifest.salt,
      manifest.iterations,
    );
    await assert.rejects(
      unseal(files.get("data/" + manifest.id + "/catalog.bin")!, wrongKey),
    );
    const corrupted = files
      .get("data/" + manifest.id + "/catalog.bin")!
      .slice();
    corrupted[15] ^= 1;
    await assert.rejects(unseal(corrupted, key));
    await assert.rejects(
      buildSnapshot(
        async <T>() => [] as T[],
        "short",
        async () => {},
      ),
    );
    assert.equal(
      (
        await db.query<{ count: number }>(
          "SELECT COUNT(*)::integer AS count FROM items",
        )
      ).rows[0].count,
      105,
    );
  } finally {
    await db.close();
  }
});
