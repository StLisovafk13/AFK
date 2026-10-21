import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
// Use the same local CLI and checkout-local state on Windows, Linux and Actions.
process.env.WRANGLER_SEND_METRICS ??= "false";
process.env.WRANGLER_WRITE_LOGS ??= "false";
process.env.WRANGLER_LOG_PATH ??= ".wrangler/logs";
process.env.WRANGLER_REGISTRY_PATH ??= ".wrangler/registry";
process.env.MINIFLARE_REGISTRY_PATH ??= ".wrangler/miniflare-registry";
process.env.CLOUDFLARE_CF_FETCH_ENABLED ??= "false";
const cli = fileURLToPath(
  new URL("../node_modules/wrangler/bin/wrangler.js", import.meta.url),
);
const result = spawnSync(process.execPath, [cli, ...process.argv.slice(2)], {
  stdio: "inherit",
  env: process.env,
});
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
