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
