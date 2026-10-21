export type AuthSettings = {
  ARCHIVE_USERNAME?: string;
  ARCHIVE_PASSWORD?: string;
};
export function validAuthConfiguration(env: AuthSettings): boolean {
  return (
    /^[a-zA-Z0-9_.-]{1,64}$/.test(env.ARCHIVE_USERNAME || "archive") &&
    typeof env.ARCHIVE_PASSWORD === "string" &&
    env.ARCHIVE_PASSWORD.length >= 20
  );
}
export async function authorized(
  request: Request,
  env: AuthSettings,
): Promise<boolean> {
  if (!validAuthConfiguration(env)) return false;
  const header = request.headers.get("Authorization") || "";
  if (!/^Basic [A-Za-z0-9+/]+=*$/i.test(header) || header.length > 8192)
    return false;
  let decoded: string;
  try {
    decoded = new TextDecoder("utf-8", { fatal: true }).decode(
      Uint8Array.from(atob(header.slice(6)), (c) => c.charCodeAt(0)),
    );
  } catch {
    return false;
  }
  const expected =
    (env.ARCHIVE_USERNAME || "archive") + ":" + env.ARCHIVE_PASSWORD;
  const encode = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", encode.encode(decoded)),
    crypto.subtle.digest("SHA-256", encode.encode(expected)),
  ]);
  const left = new Uint8Array(a),
    right = new Uint8Array(b);
  let difference = 0;
  for (let i = 0; i < left.length; i++) difference |= left[i] ^ right[i];
  return difference === 0;
}
