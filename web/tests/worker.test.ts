import { test } from "node:test";
import assert from "node:assert/strict";
import { authorized, validAuthConfiguration } from "../worker/auth";
import { createHandler, type Env } from "../worker/index";
import { createArchive } from "../lib/archive-store";

const password = "a-unique-test-password:with-colon";
function environment(): Env {
  return {
    ARCHIVE_PASSWORD: password,
    ASSETS: {
      fetch: async () =>
        new Response("<h1>Private archive</h1>", {
          headers: { "Content-Type": "text/html" },
        }),
    },
  };
}
function request(
  path: string,
  credentials = "archive:" + password,
  method = "GET",
) {
  return new Request("https://archive.example" + path, {
    method,
    headers: credentials ? { Authorization: "Basic " + btoa(credentials) } : {},
  });
}
test("authentication validates credentials, rejects forged identity and fails closed when missing", async () => {
  const env = environment();
  assert.equal(validAuthConfiguration({}), false);
  assert.equal(validAuthConfiguration({ ARCHIVE_PASSWORD: "short" }), false);
  assert.equal(
    validAuthConfiguration({ ...env, ARCHIVE_USERNAME: "with:colon" }),
    false,
  );
  assert.equal(await authorized(request("/"), env), true);
  assert.equal(await authorized(request("/", "wrong:" + password), env), false);
  assert.equal(await authorized(request("/", "archive:wrong"), env), false);
  assert.equal(
    await authorized(
      new Request("https://archive.example", {
        headers: {
          "oai-authenticated-user-id": "forged",
          "oai-authenticated-user-email": "forged@example.com",
        },
      }),
      env,
    ),
    false,
  );
  assert.equal(
    await authorized(
      new Request("https://archive.example", {
        headers: { Authorization: "Basic %invalid%" },
      }),
      env,
    ),
    false,
  );
});
test("all asset and API paths require authentication before accessing data or files", async () => {
  let reads = 0,
    assets = 0;
  const store = createArchive(async <T>() => {
    reads++;
    return [] as T[];
  });
  const handler = createHandler(() => store);
  const env = environment();
  env.ASSETS.fetch = async () => {
    assets++;
    return new Response("asset");
  };
  for (const path of [
    "/",
    "/assets/archive.js",
    "/favicon.svg",
    "/api/archive?view=summary",
  ]) {
    const response = await handler(request(path, ""), env);
    assert.equal(response.status, 401);
    assert.match(response.headers.get("WWW-Authenticate") || "", /^Basic/);
    assert.equal(response.headers.get("Cache-Control"), "private, no-store");
  }
  assert.equal(reads, 0);
  assert.equal(assets, 0);
  const unconfigured = await handler(request("/"), {
    ...env,
    ARCHIVE_PASSWORD: "",
  });
  assert.equal(unconfigured.status, 503);
  const page = await handler(request("/"), env);
  assert.equal(page.status, 200);
  assert.equal(assets, 1);
  assert.equal((await handler(request("/api/unknown"), env)).status, 404);
  const post = await handler(
    request("/api/archive", "archive:" + password, "POST"),
    env,
  );
  assert.equal(post.status, 405);
  assert.equal(post.headers.get("Allow"), "GET, HEAD");
});
test("API filters, keyset pagination, HEAD and errors preserve private data boundaries", async () => {
  let text = "",
    values: unknown[] = [];
  const store = createArchive(
    async <T>(sql: string, params: unknown[] = []) => {
      text = sql;
      values = params;
      return [] as T[];
    },
  );
  const handler = createHandler(() => store);
  const env = environment();
  const result = await handler(
    request("/api/archive?q=alpha&chat=-100&cursor=55"),
    env,
  );
  assert.equal(result.status, 200);
  assert.deepEqual(await result.json(), { items: [], next: null });
  assert.ok(text.includes("i.id < $3::bigint"));
  assert.deepEqual(values, ["%alpha%", "-100", "55"]);
  assert.equal(
    (await handler(request("/api/archive?cursor=bad"), env)).status,
    400,
  );
  assert.equal(
    (await handler(request("/api/archive?view=detail&id=999"), env)).status,
    404,
  );
  const head = await handler(request("/", "archive:" + password, "HEAD"), env);
  assert.equal(head.status, 200);
  assert.equal(await head.text(), "");
  const failure = createHandler(() => {
    throw new Error(
      "postgresql://secret-user:secret-password@private.neon.tech/db",
    );
  });
  const error = await failure(request("/api/archive"), env);
  assert.equal(error.status, 503);
  assert.ok(!(await error.text()).includes("secret-password"));
  const defaultHandler = createHandler();
  const missingDB = await defaultHandler(request("/api/archive"), env);
  assert.equal(
    ((await missingDB.json()) as { code: string }).code,
    "DATABASE_NOT_CONFIGURED",
  );
});
