import { where, positiveId, type Filters } from "./filters";
import type { Detail, Media, Profile, Summary } from "./types";
export type Query = <T>(text: string, values?: unknown[]) => Promise<T[]>;
export function createArchive(query: Query) {
  const columns = `i.id::text, i.chat_id::text, COALESCE(i.username, '') AS username,
  COALESCE(i.image_url, '') AS image_url, COALESCE(i.profile_url, '') AS profile_url,
  i.latitude, i.longitude, i.created_at`;

  async function mediaPage(filters: Filters) {
    const w = where(filters);
    const rows = await query<Media>(
      `SELECT ${columns} FROM items i ${w.sql} ORDER BY i.id DESC LIMIT 49`,
      w.values,
    );
    const more = rows.length > 48;
    const items = rows.slice(0, 48);
    return { items, next: more ? items.at(-1)!.id : null };
  }

  async function profilePage(filters: Filters) {
    const w = where(filters, false);
    // Latest record ID is the profile cursor: deterministic, with no OFFSET scans.
    const having = filters.cursor
      ? `HAVING MAX(i.id) < $${w.values.length + 1}::bigint`
      : "";
    const rows = await query<Profile>(
      `SELECT COALESCE(i.username, '') AS username, COUNT(*)::integer AS count,
    MAX(i.id)::text AS latest,
    (SELECT image_url FROM items t WHERE t.username IS NOT DISTINCT FROM i.username
      ${filters.chat ? "AND t.chat_id = i.chat_id" : ""} AND image_url <> '' ORDER BY t.id DESC LIMIT 1) AS image_url
    FROM items i ${w.sql} GROUP BY i.username ${filters.chat ? ", i.chat_id" : ""}
    ${having} ORDER BY MAX(i.id) DESC LIMIT 37`,
      [...w.values, ...(filters.cursor ? [filters.cursor] : [])],
    );
    const items = rows.slice(0, 36);
    return { items, next: rows.length > 36 ? items.at(-1)!.latest : null };
  }

  async function mapPoints(filters: Filters) {
    const w = where({ ...filters, geo: true }, false);
    const rows = await query<Media>(
      `SELECT ${columns} FROM items i ${w.sql} ORDER BY i.id DESC LIMIT 2001`,
      w.values,
    );
    return { items: rows.slice(0, 2000), limited: rows.length > 2000 };
  }

  let summaryCache: { expires: number; value: Summary } | undefined;
  let summaryPending: Promise<Summary> | undefined;
  async function summary(): Promise<Summary> {
    if (summaryCache && summaryCache.expires > Date.now())
      return summaryCache.value;
    if (summaryPending) return summaryPending;
    summaryPending = (async () => {
      const [totals, chats] = await Promise.all([
        query<
          Omit<Summary, "chats">
        >(`SELECT COUNT(*)::integer AS items, COUNT(DISTINCT username)::integer AS profiles,
        COUNT(*) FILTER (WHERE latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180)::integer AS geo,
        (SELECT COUNT(*)::integer FROM links) AS links, (SELECT COUNT(*)::integer FROM comments) AS comments FROM items`),
        query<{ chat_id: string; count: number }>(
          "SELECT chat_id::text, COUNT(*)::integer AS count FROM items GROUP BY chat_id ORDER BY COUNT(*) DESC LIMIT 100",
        ),
      ]);
      const value = { ...totals[0], chats };
      summaryCache = { expires: Date.now() + 60000, value };
      return value;
    })();
    try {
      return await summaryPending;
    } finally {
      summaryPending = undefined;
    }
  }

  function cameraFromMeta(raw: string | null): string | null {
    if (!raw) return null;
    try {
      const meta = JSON.parse(raw);
      const exif = meta.exiftool || meta.exif || meta;
      const camera =
        exif.Model || exif["EXIF:Model"] || exif.camera_model || meta.camera;
      return typeof camera === "string" ? camera.slice(0, 160) : null;
    } catch {
      return null;
    }
  }

  async function mediaDetail(id: string): Promise<Detail | null> {
    positiveId(id);
    const [item] = await query<
      Media & {
        source: string;
        source_file: string;
        added_by: string;
        meta: string | null;
      }
    >(
      `SELECT ${columns},
    i.source, i.source_file, i.added_by,
    CASE WHEN octet_length(i.meta_json) <= 131072 THEN i.meta_json ELSE NULL END AS meta
    FROM items i WHERE i.id = $1::bigint`,
      [id],
    );
    if (!item) return null;
    const comments = await query<Detail["comments"][number]>(
      `SELECT id::text, comment, created_at FROM comments WHERE item_id = $1::bigint ORDER BY id LIMIT 200`,
      [id],
    );
    const { meta, ...rest } = item;
    return { ...rest, camera: cameraFromMeta(meta), comments };
  }

  return { mediaPage, profilePage, mapPoints, summary, mediaDetail };
}
