export const ITERATIONS = 600000;
export function base64(bytes: Uint8Array): string {
  return btoa(Array.from(bytes, (b) => String.fromCharCode(b)).join(""));
}
export function unbase64(text: string): Uint8Array<ArrayBuffer> {
  return Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
}
export async function deriveKey(
  password: string,
  salt: string,
  iterations = ITERATIONS,
) {
  if (iterations < 100000 || iterations > 2000000)
    throw new Error("Unsupported snapshot settings");
  const material = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    "PBKDF2",
    false,
    ["deriveKey"],
  );
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", hash: "SHA-256", salt: unbase64(salt), iterations },
    material,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}
export async function seal(
  bytes: Uint8Array,
  key: CryptoKey,
): Promise<Uint8Array> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const encrypted = new Uint8Array(
    await crypto.subtle.encrypt(
      { name: "AES-GCM", iv },
      key,
      Uint8Array.from(bytes),
    ),
  );
  const result = new Uint8Array(iv.length + encrypted.length);
  result.set(iv);
  result.set(encrypted, iv.length);
  return result;
}
export async function unseal(
  bytes: Uint8Array,
  key: CryptoKey,
): Promise<Uint8Array<ArrayBuffer>> {
  if (bytes.length < 28) throw new Error("Invalid encrypted snapshot");
  return new Uint8Array(
    await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: Uint8Array.from(bytes.subarray(0, 12)) },
      key,
      Uint8Array.from(bytes.subarray(12)),
    ),
  );
}
