export type Filters = {
  username: string;
  chat: string;
  geo: boolean;
  cursor: string;
  exact: boolean;
};
export class InputError extends Error {}
export function positiveId(value: string): string {
  if (!/^[1-9]\d{0,18}$/.test(value) || BigInt(value) > 9223372036854775807n)
    throw new InputError("Некорректный идентификатор");
  return value;
}
export function parseFilters(params: URLSearchParams): Filters {
  const username = (params.get("q") || "").trim().replace(/^@/, "");
  if (username.length > 100)
    throw new InputError("Слишком длинный поисковый запрос");
  const chat = params.get("chat") || "";
  if (
    chat &&
    (!/^-?\d{1,19}$/.test(chat) ||
      BigInt(chat) < -9223372036854775808n ||
      BigInt(chat) > 9223372036854775807n)
  )
    throw new InputError("Некорректный чат");
  const cursor = params.get("cursor") || "";
  if (cursor) positiveId(cursor);
  return {
    username,
    chat,
    geo: params.get("geo") === "1",
    cursor,
    exact: params.get("exact") === "1",
  };
}
export function where(filters: Filters, cursor = true) {
  const values: string[] = [];
  const clauses: string[] = [];
  const add = (sql: string, value: string) => {
    values.push(value);
    clauses.push(sql.replace("?", "$" + values.length));
  };
  // Escape LIKE wildcards: typing '%' must never request the entire archive.
  if (filters.username) {
    if (filters.exact) add("LOWER(i.username) = LOWER(?)", filters.username);
    else
      add(
        "i.username ILIKE ? ESCAPE '\\'",
        "%" + filters.username.replace(/[\\%_]/g, "\\$&") + "%",
      );
  }
  if (filters.chat) add("i.chat_id = ?::bigint", filters.chat);
  if (filters.geo)
    clauses.push(
      "i.latitude BETWEEN -90 AND 90 AND i.longitude BETWEEN -180 AND 180",
    );
  if (cursor && filters.cursor) add("i.id < ?::bigint", filters.cursor);
  return {
    sql: clauses.length ? " WHERE " + clauses.join(" AND ") : "",
    values,
  };
}
