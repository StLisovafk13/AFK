import { deriveKey, unseal } from "./snapshot-crypto";
import { createSnapshotStore } from "./snapshot-store";
import { parseFilters } from "./filters";
import type { Catalog, Manifest, SnapshotRow } from "./snapshot-types";

let password = "";
let active:
  | { id: string; store: ReturnType<typeof createSnapshotStore> }
  | undefined;
let pending: Promise<ReturnType<typeof createSnapshotStore>> | undefined;
const base = import.meta.env.BASE_URL;
async function bytes(path: string) {
  const response = await fetch(base + path, { cache: "no-store" });
  if (!response.ok)
    throw new Error(
      "Не удалось загрузить архив. Проверьте публикацию GitHub Pages и подключение к сети.",
    );
  return new Uint8Array(await response.arrayBuffer());
}
async function ensureSnapshot() {
  if (!password) throw new Error("Войдите по паролю архива.");
  if (pending) return pending;
  pending = (async () => {
    const manifest = JSON.parse(
      new TextDecoder().decode(await bytes("snapshot.json")),
    ) as Manifest;
    if (
      manifest.version !== 1 ||
      !/^[a-f0-9-]{36}$/.test(manifest.id) ||
      manifest.catalog !== "catalog.bin"
    )
      throw new Error("Неподдерживаемый формат архива.");
    if (active?.id === manifest.id) return active.store;
    const key = await deriveKey(password, manifest.salt, manifest.iterations);
    async function read<T>(file: string): Promise<T> {
      if (!/^(catalog|[cg]\d+)\.bin$/.test(file))
        throw new Error("Некорректная часть архива.");
      const packed = await unseal(
        await bytes("data/" + manifest.id + "/" + file),
        key,
      );
      const stream = new Blob([packed])
        .stream()
        .pipeThrough(new DecompressionStream("gzip"));
      return JSON.parse(await new Response(stream).text()) as T;
    }
    let catalog: Catalog;
    try {
      catalog = await read<Catalog>(manifest.catalog);
    } catch {
      throw new Error("Пароль не подходит или снимок архива повреждён.");
    }
    if (catalog.version !== 1)
      throw new Error("Неподдерживаемый формат каталога.");
    const cache = new Map<string, Promise<SnapshotRow[]>>();
    const store = createSnapshotStore(catalog, (file) => {
      let value = cache.get(file);
      if (!value) {
        value = read<SnapshotRow[]>(file);
        cache.set(file, value);
        value.catch(() => cache.delete(file));
        if (cache.size > 16) cache.delete(cache.keys().next().value!);
      }
      return value;
    });
    active = { id: manifest.id, store };
    return store;
  })();
  try {
    return await pending;
  } finally {
    pending = undefined;
  }
}
export async function unlockSnapshot(value: string) {
  password = value;
  active = undefined;
  try {
    await ensureSnapshot();
  } catch (error) {
    password = "";
    throw error;
  }
}
export function lockSnapshot() {
  password = "";
  active = undefined;
  pending = undefined;
}
export async function snapshotAPI<T>(
  params: URLSearchParams,
  signal?: AbortSignal,
): Promise<T> {
  try {
    const store = await ensureSnapshot();
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    const filters = parseFilters(params);
    let result: unknown;
    switch (params.get("view")) {
      case "summary":
        result = await store.summary();
        break;
      case "profiles":
        result = await store.profilePage(filters);
        break;
      case "map":
        result = await store.mapPoints(filters);
        break;
      case "detail":
        result = await store.mediaDetail(params.get("id") || "");
        if (!result) throw new Error("Запись не найдена в снимке архива.");
        break;
      default:
        result = await store.mediaPage(filters);
    }
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    return result as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw {
      code: "SNAPSHOT_UNAVAILABLE",
      message:
        error instanceof Error ? error.message : "Не удалось прочитать архив.",
    };
  }
}
