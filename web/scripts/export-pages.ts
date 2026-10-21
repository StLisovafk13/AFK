import { neon } from "@neondatabase/serverless";
import { mkdir, writeFile, rm, access } from "node:fs/promises";
import { resolve, dirname } from "node:path";
import { buildSnapshot } from "./snapshot";

try {
  if (!process.env.DATABASE_URL || !process.env.ARCHIVE_PASSWORD)
    throw new Error("Missing settings");
  const output = resolve("dist/client");
  await access(resolve(output, "index.html"));
  await rm(resolve(output, "data"), { recursive: true, force: true });
  await rm(resolve(output, "snapshot.json"), { force: true });
  const sql = neon(process.env.DATABASE_URL);
  await buildSnapshot(
    async <T>(text: string, values: unknown[] = []) =>
      (await sql.query(text, values, {
        fetchOptions: { signal: AbortSignal.timeout(60000) },
      })) as T[],
    process.env.ARCHIVE_PASSWORD,
    async (path, bytes) => {
      const file = resolve(output, path);
      await mkdir(dirname(file), { recursive: true });
      await writeFile(file, bytes);
    },
  );
  console.log("Encrypted GitHub Pages snapshot created.");
} catch {
  console.error(
    "Snapshot export failed. Check DATABASE_URL, SELECT access to items/links/comments, ARCHIVE_PASSWORD (20–1024 characters), and the Pages size limit. No archive data was logged.",
  );
  process.exitCode = 1;
}
