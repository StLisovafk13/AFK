import { gzipSync } from "node:zlib";
import { base64, deriveKey, seal, ITERATIONS } from "../lib/snapshot-crypto";
import type {
  Catalog,
  Manifest,
  ProfileGroup,
  SnapshotRow,
} from "../lib/snapshot-types";
import type { Query } from "../lib/snapshot-types";

export async function buildSnapshot(
  query: Query,
  password: string,
  write: (path: string, bytes: Uint8Array) => Promise<void>,
  { batchSize = 1000, chunkSize = 256 } = {},
) {
  if (password.length < 20 || password.length > 1024)
    throw new Error("ARCHIVE_PASSWORD must contain 20–1024 characters");
  const id = crypto.randomUUID();
  const salt = base64(crypto.getRandomValues(new Uint8Array(16)));
  const key = await deriveKey(password, salt);
  let totalBytes = 0;
  async function encrypted(path: string, value: unknown) {
    const bytes = await seal(gzipSync(JSON.stringify(value)), key);
    totalBytes += bytes.length;
    if (totalBytes > 500 * 1024 * 1024)
      throw new Error("Encrypted snapshot exceeds 500 MiB");
    await write("data/" + id + "/" + path, bytes);
  }
  const catalog: Catalog = {
    version: 1,
    createdAt: new Date().toISOString(),
    summary: {
      items: 0,
      profiles: 0,
      comments: 0,
      links: 0,
      geo: 0,
      chats: [],
    },
    profiles: [],
    chunks: [],
    geoChunks: [],
  };
  const groups = new Map<string, ProfileGroup>();
  const chats = new Map<string, number>();
  const usernames = new Set<string>();
  const groupKey = (row: { chat_id: string; username: string }) =>
    JSON.stringify([row.chat_id, row.username]);
  let pending: SnapshotRow[] = [],
    pendingGeo: SnapshotRow[] = [];
  async function flush(geo: boolean) {
    const rows = geo ? pendingGeo : pending;
    if (!rows.length) return;
    const chunks = geo ? catalog.geoChunks : catalog.chunks;
    const index = chunks.length;
    const file = (geo ? "g" : "c") + index + ".bin";
    await encrypted(file, rows);
    chunks.push({ file, min: rows.at(-1)!.id, max: rows[0].id });
    const seen = new Set(rows.map(groupKey));
    for (const name of seen)
      groups.get(name)![geo ? "geoChunks" : "chunks"].push(index);
    if (geo) pendingGeo = [];
    else pending = [];
  }
  // A high-water mark excludes records added after this export began.
  const [{ id: boundary }] = await query<{ id: string }>(
    "SELECT COALESCE(MAX(id),0)::text AS id FROM items",
  );
  let cursor = boundary;
  let first = true;
  while (boundary !== "0") {
    const rows = await query<SnapshotRow>(
      `SELECT id::text,chat_id::text,COALESCE(username,'') AS username,
      COALESCE(image_url,'') AS image_url,COALESCE(profile_url,'') AS profile_url,
      latitude,longitude,created_at,COALESCE(source,'') AS source FROM items
      WHERE id ${first ? "<=" : "<"} $1::bigint ORDER BY items.id DESC LIMIT $2`,
      [cursor, batchSize],
    );
    if (!rows.length) break;
    const comments = await query<{
      id: string;
      item_id: string;
      comment: string;
      created_at: string;
    }>(
      "SELECT id::text,item_id::text,comment,created_at FROM comments WHERE item_id = ANY($1::bigint[]) ORDER BY comments.id",
      [rows.map((r) => r.id)],
    );
    const byItem = new Map<string, SnapshotRow["comments"]>();
    for (const c of comments) {
      const list = byItem.get(c.item_id) || [];
      if (list.length < 200)
        list.push({ id: c.id, comment: c.comment, created_at: c.created_at });
      byItem.set(c.item_id, list);
    }
    for (const raw of rows) {
      const row: SnapshotRow = {
        ...raw,
        source_file: "",
        added_by: "",
        camera: null,
        comments: byItem.get(raw.id) || [],
      };
      const name = groupKey(row);
      let group = groups.get(name);
      if (!group) {
        group = {
          username: row.username,
          chat_id: row.chat_id,
          count: 0,
          latest: row.id,
          image_url: "",
          geo_count: 0,
          geo_latest: "0",
          geo_image_url: "",
          chunks: [],
          geoChunks: [],
        };
        groups.set(name, group);
      }
      group.count++;
      if (!group.image_url) group.image_url = row.image_url;
      usernames.add(row.username);
      chats.set(row.chat_id, (chats.get(row.chat_id) || 0) + 1);
      catalog.summary.items++;
      catalog.summary.comments += row.comments.length;
      pending.push(row);
      if (
        row.latitude !== null &&
        row.longitude !== null &&
        row.latitude >= -90 &&
        row.latitude <= 90 &&
        row.longitude >= -180 &&
        row.longitude <= 180
      ) {
        catalog.summary.geo++;
        group.geo_count++;
        if (group.geo_latest === "0") group.geo_latest = row.id;
        if (!group.geo_image_url) group.geo_image_url = row.image_url;
        pendingGeo.push(row);
        if (pendingGeo.length >= chunkSize) await flush(true);
      }
      if (pending.length >= chunkSize) await flush(false);
    }
    const next = rows.at(-1)!.id;
    if (!first && BigInt(next) >= BigInt(cursor))
      throw new Error("Export cursor did not advance");
    cursor = next;
    first = false;
  }
  await flush(false);
  await flush(true);
  const [links] = await query<{ count: number }>(
    "SELECT COUNT(*)::integer AS count FROM links",
  );
  catalog.summary.links = links.count;
  catalog.summary.profiles = usernames.size;
  catalog.summary.chats = Array.from(chats, ([chat_id, count]) => ({
    chat_id,
    count,
  }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 100);
  catalog.profiles = Array.from(groups.values());
  await encrypted("catalog.bin", catalog);
  const manifest: Manifest = {
    version: 1,
    id,
    salt,
    iterations: ITERATIONS,
    catalog: "catalog.bin",
  };
  await write(
    "snapshot.json",
    new TextEncoder().encode(JSON.stringify(manifest)),
  );
  return { manifest, catalog };
}
