import type { Filters } from "./filters";
import { positiveId } from "./filters";
import type { Catalog, SnapshotRow } from "./snapshot-types";
import type { Profile } from "./types";

export function createSnapshotStore(
  catalog: Catalog,
  load: (file: string) => Promise<SnapshotRow[]>,
) {
  function matchName(username: string, f: Filters) {
    return (
      !f.username ||
      (f.exact
        ? username.toLowerCase() === f.username.toLowerCase()
        : username.toLowerCase().includes(f.username.toLowerCase()))
    );
  }
  function groupMatches(group: Catalog["profiles"][number], f: Filters) {
    return (
      matchName(group.username, f) &&
      (!f.chat || group.chat_id === f.chat) &&
      (!f.geo || group.geo_count > 0)
    );
  }
  function indexes(f: Filters, geo: boolean) {
    const chunks = geo ? catalog.geoChunks : catalog.chunks;
    if (!f.username && !f.chat) return chunks.map((_, i) => i);
    const found = new Set<number>();
    for (const group of catalog.profiles)
      if (groupMatches(group, f))
        for (const i of group[geo ? "geoChunks" : "chunks"]) found.add(i);
    return Array.from(found).sort((a, b) => a - b);
  }
  async function rows(f: Filters, limit: number, geo: boolean, cursor = true) {
    const result: SnapshotRow[] = [];
    const chunks = geo ? catalog.geoChunks : catalog.chunks;
    for (const index of indexes(f, geo)) {
      const chunk = chunks[index];
      if (cursor && f.cursor && BigInt(chunk.min) >= BigInt(f.cursor)) continue;
      for (const row of await load(chunk.file)) {
        if (
          !matchName(row.username, f) ||
          (f.chat && row.chat_id !== f.chat) ||
          (cursor && f.cursor && BigInt(row.id) >= BigInt(f.cursor))
        )
          continue;
        result.push(row);
        if (result.length === limit) return result;
      }
    }
    return result;
  }
  return {
    async summary() {
      return { ...catalog.summary, snapshot_at: catalog.createdAt };
    },
    async mediaPage(f: Filters) {
      const found = await rows(f, 49, f.geo);
      const items = found.slice(0, 48);
      return { items, next: found.length > 48 ? items.at(-1)!.id : null };
    },
    async mapPoints(f: Filters) {
      const found = await rows({ ...f, geo: true }, 2001, true, false);
      return { items: found.slice(0, 2000), limited: found.length > 2000 };
    },
    async profilePage(f: Filters) {
      const grouped = new Map<string, Profile>();
      for (const group of catalog.profiles)
        if (groupMatches(group, f)) {
          const count = f.geo ? group.geo_count : group.count;
          const latest = f.geo ? group.geo_latest : group.latest;
          const image_url = f.geo ? group.geo_image_url : group.image_url;
          const existing = grouped.get(group.username);
          if (existing) {
            existing.count += count;
            if (BigInt(latest) > BigInt(existing.latest)) {
              existing.latest = latest;
              existing.image_url = image_url;
            }
          } else
            grouped.set(group.username, {
              username: group.username,
              count,
              latest,
              image_url,
            });
        }
      const found = Array.from(grouped.values())
        .filter((p) => !f.cursor || BigInt(p.latest) < BigInt(f.cursor))
        .sort((a, b) => (BigInt(a.latest) > BigInt(b.latest) ? -1 : 1))
        .slice(0, 37);
      const items = found.slice(0, 36);
      return { items, next: found.length > 36 ? items.at(-1)!.latest : null };
    },
    async mediaDetail(id: string) {
      positiveId(id);
      const number = BigInt(id);
      const chunk = catalog.chunks.find(
        (c) => number >= BigInt(c.min) && number <= BigInt(c.max),
      );
      if (!chunk) return null;
      return (await load(chunk.file)).find((r) => r.id === id) || null;
    },
  };
}
