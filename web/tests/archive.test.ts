import { test } from "node:test";
import assert from "node:assert/strict";
import { PGlite } from "@electric-sql/pglite";
import { createArchive } from "../lib/archive-store";
import { parseFilters } from "../lib/filters";

test("PostgreSQL queries paginate, filter, aggregate and read detail without changing data", async () => {
  const db = new PGlite();
  try {
    await db.exec(`CREATE TABLE items(id BIGSERIAL PRIMARY KEY,chat_id BIGINT,username TEXT,image_url TEXT,profile_url TEXT,latitude DOUBLE PRECISION,longitude DOUBLE PRECISION,created_at TEXT,source TEXT,source_file TEXT,added_by TEXT,meta_json TEXT);
      CREATE TABLE links(id BIGSERIAL,username TEXT);
      CREATE TABLE comments(id BIGSERIAL,item_id BIGINT,comment TEXT,created_at TEXT);
      INSERT INTO items(chat_id,username,image_url,profile_url,latitude,longitude,created_at,source,source_file,added_by,meta_json)
      SELECT -100,CASE WHEN n%2=0 THEN 'alpha' ELSE 'beta' END,'https://example.com/'||n||'.jpg','https://vsco.co/alpha',CASE WHEN n%2=0 THEN 55.75 ELSE NULL END,37.61,'2026-10-21','test','','','{}' FROM generate_series(1,55) n;
      UPDATE items SET meta_json='{"exiftool":{"Model":"Test camera"}}' WHERE id=55;
      INSERT INTO comments(item_id,comment,created_at) VALUES(55,'Saved comment','2026-10-21');
      INSERT INTO links(username) VALUES('alpha');`);
    const archive = createArchive(
      async <T>(text: string, values: unknown[] = []) => {
        assert.match(text.trim(), /^SELECT\b/i);
        return (await db.query<T>(text, values)).rows;
      },
    );
    const filters = parseFilters(new URLSearchParams());
    const page = await archive.mediaPage(filters);
    assert.equal(page.items.length, 48);
    assert.equal(page.items[0].id, "55");
    assert.equal(page.next, "8");
    const next = await archive.mediaPage({ ...filters, cursor: page.next! });
    assert.equal(next.items.length, 7);
    assert.equal(next.items[0].id, "7");
    assert.equal(next.next, null);
    const matching = await archive.mediaPage({
      ...filters,
      username: "alpha",
      chat: "-100",
      geo: true,
    });
    assert.equal(matching.items.length, 27);
    assert.ok(matching.items.every((item) => item.username === "alpha"));
    const profiles = await archive.profilePage({ ...filters, chat: "-100" });
    assert.deepEqual(
      profiles.items.map((p) => [p.username, p.count]),
      [
        ["beta", 28],
        ["alpha", 27],
      ],
    );
    const moreProfiles = await archive.profilePage({
      ...filters,
      cursor: "55",
    });
    assert.deepEqual(
      moreProfiles.items.map((p) => p.username),
      ["alpha"],
    );
    assert.equal((await archive.mapPoints(filters)).items.length, 27);
    const stats = await archive.summary();
    assert.equal(stats.items, 55);
    assert.equal(stats.comments, 1);
    assert.equal(stats.links, 1);
    assert.equal(stats.profiles, 2);
    const detail = await archive.mediaDetail("55");
    assert.equal(detail?.camera, "Test camera");
    assert.equal(detail?.comments[0].comment, "Saved comment");
    assert.ok(!("meta" in detail!));
    assert.equal(await archive.mediaDetail("999"), null);
    assert.equal(
      (await archive.mediaPage({ ...filters, username: "%' OR 1=1 --" })).items
        .length,
      0,
    );
    assert.equal(
      (
        await db.query<{ count: number }>(
          "SELECT COUNT(*)::integer AS count FROM items",
        )
      ).rows[0].count,
      55,
    );
  } finally {
    await db.close();
  }
});
