import { neon } from "@neondatabase/serverless";
import { createArchive } from "../lib/archive-store";
import { InputError, parseFilters } from "../lib/filters";
import { authorized, validAuthConfiguration, type AuthSettings } from "./auth";

export type Env = AuthSettings & {
  DATABASE_URL?: string;
  ASSETS: { fetch(request: Request): Promise<Response> };
};
class ConfigurationError extends Error {}
type ArchiveStore = ReturnType<typeof createArchive>;
let lastURL: string | undefined;
let lastStore: ArchiveStore | undefined;
function archiveFor(env: Env): ArchiveStore {
  if (!env.DATABASE_URL) throw new ConfigurationError();
  if (!lastStore || env.DATABASE_URL !== lastURL) {
    const sql = neon(env.DATABASE_URL);
    lastStore = createArchive(
      async <T>(text: string, values: unknown[] = []) =>
        (await sql.query(text, values, {
          fetchOptions: { signal: AbortSignal.timeout(20000) },
        })) as T[],
    );
    lastURL = env.DATABASE_URL;
  }
  return lastStore;
}
const json = (data: unknown, status = 200) => Response.json(data, { status });
function privateResponse(response: Response): Response {
  const result = new Response(response.body, response);
  result.headers.set("Cache-Control", "private, no-store");
  result.headers.set("Vary", "Authorization");
  result.headers.set("X-Content-Type-Options", "nosniff");
  result.headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  result.headers.set("X-Frame-Options", "DENY");
  result.headers.set(
    "Content-Security-Policy",
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com data:; img-src 'self' https: http: data: blob:; media-src https: http: blob:; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'",
  );
  return result;
}
export function createHandler(
  getArchive: (env: Env) => ArchiveStore = archiveFor,
) {
  return async function fetch(request: Request, env: Env): Promise<Response> {
    // Every request, including static assets, reaches this guard via run_worker_first.
    if (!validAuthConfiguration(env))
      return privateResponse(
        json(
          {
            code: "AUTH_NOT_CONFIGURED",
            error:
              "Задайте ARCHIVE_PASSWORD длиной не менее 20 символов и допустимый ARCHIVE_USERNAME в секретах Worker.",
          },
          503,
        ),
      );
    if (!(await authorized(request, env))) {
      const response = json(
        {
          code: "UNAUTHORIZED",
          error: "Для просмотра архива войдите с логином и паролем.",
        },
        401,
      );
      response.headers.set(
        "WWW-Authenticate",
        'Basic realm="VSCO Archive", charset="UTF-8"',
      );
      return privateResponse(response);
    }
    const path = new URL(request.url).pathname;
    if (!["GET", "HEAD"].includes(request.method)) {
      const response = json({ error: "Метод не поддерживается" }, 405);
      response.headers.set("Allow", "GET, HEAD");
      return privateResponse(response);
    }
    let response: Response;
    try {
      if (path === "/api/archive") {
        const p = new URL(request.url).searchParams;
        const filters = parseFilters(p);
        const archive = getArchive(env);
        switch (p.get("view")) {
          case "summary":
            response = json(await archive.summary());
            break;
          case "profiles":
            response = json(await archive.profilePage(filters));
            break;
          case "map":
            response = json(await archive.mapPoints(filters));
            break;
          case "detail": {
            const item = await archive.mediaDetail(p.get("id") || "");
            response = item
              ? json(item)
              : json({ error: "Запись не найдена" }, 404);
            break;
          }
          default:
            response = json(await archive.mediaPage(filters));
        }
      } else if (path.startsWith("/api/"))
        response = json({ error: "Маршрут не найден" }, 404);
      else response = await env.ASSETS.fetch(request);
    } catch (error) {
      if (error instanceof InputError)
        response = json({ error: error.message }, 400);
      else if (error instanceof ConfigurationError)
        response = json(
          {
            code: "DATABASE_NOT_CONFIGURED",
            error: "Подключите базу Neon в секретах Cloudflare Worker",
          },
          503,
        );
      else
        response = json(
          {
            code: "DATABASE_UNAVAILABLE",
            error:
              "Не удалось прочитать архив. Проверьте подключение к Neon и доступ к таблицам items, links, comments.",
          },
          503,
        );
    }
    if (request.method === "HEAD") response = new Response(null, response);
    return privateResponse(response);
  };
}
export default { fetch: createHandler() };
