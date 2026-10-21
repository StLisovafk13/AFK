import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const required = [
  "CLOUDFLARE_API_TOKEN",
  "CLOUDFLARE_ACCOUNT_ID",
  "DATABASE_URL",
  "ARCHIVE_PASSWORD",
];
const missing = required.filter((key) => !process.env[key]);
if (missing.length)
  throw new Error("Добавьте GitHub Actions secrets: " + missing.join(", "));
if (process.env.ARCHIVE_PASSWORD.length < 20)
  throw new Error("ARCHIVE_PASSWORD должен содержать не менее 20 символов.");
const username = process.env.ARCHIVE_USERNAME || "archive";
if (!/^[a-zA-Z0-9_.-]{1,64}$/.test(username))
  throw new Error(
    "ARCHIVE_USERNAME: допустимы 1–64 латинских буквы, цифры, _, . и -",
  );
let databaseValid = false;
try {
  const url = new URL(process.env.DATABASE_URL);
  databaseValid =
    ["postgres:", "postgresql:"].includes(url.protocol) && !!url.hostname;
} catch {}
if (!databaseValid)
  throw new Error(
    "DATABASE_URL должен быть строкой подключения PostgreSQL из Neon Connect.",
  );
const name = process.env.ARCHIVE_WORKER_NAME || "afk-vsco-archive";
if (!/^[a-z][a-z0-9-]{0,62}$/.test(name))
  throw new Error("Некорректное имя Cloudflare Worker.");
const directory = await mkdtemp(join(tmpdir(), "afk-archive-secrets-"));
try {
  const path = join(directory, "secrets.json");
  await writeFile(
    path,
    JSON.stringify({
      DATABASE_URL: process.env.DATABASE_URL,
      ARCHIVE_PASSWORD: process.env.ARCHIVE_PASSWORD,
      ARCHIVE_USERNAME: username,
    }),
    { mode: 0o600 },
  );
  // Wrangler uploads these as runtime secrets with the same Worker version.
  // Their values never enter the Vite build, bundle, CLI arguments or artifacts.
  const cli = fileURLToPath(new URL("./wrangler.mjs", import.meta.url));
  const result = spawnSync(
    process.execPath,
    [cli, "deploy", "--name", name, "--secrets-file", path],
    { stdio: "inherit", env: process.env },
  );
  if (result.error) throw result.error;
  if (result.status !== 0)
    throw new Error(
      "Cloudflare deployment failed; check the Wrangler output above.",
    );
} finally {
  await rm(directory, { recursive: true, force: true });
}
