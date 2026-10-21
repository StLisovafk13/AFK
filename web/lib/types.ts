export type Media = {
  id: string;
  chat_id: string;
  username: string;
  image_url: string;
  profile_url: string;
  latitude: number | null;
  longitude: number | null;
  created_at: string;
};
export type Profile = {
  username: string;
  count: number;
  latest: string;
  image_url: string;
};
export type Summary = {
  items: number;
  profiles: number;
  links: number;
  comments: number;
  geo: number;
  chats: { chat_id: string; count: number }[];
};
export type Detail = Media & {
  source: string;
  source_file: string;
  added_by: string;
  camera: string | null;
  comments: { id: string; comment: string; created_at: string }[];
};
export type Page = { items: Media[]; next: string | null };
export function safeURL(value: string | null | undefined): string | undefined {
  if (!value) return;
  try {
    const url = new URL(value);
    if (url.protocol === "https:" || url.protocol === "http:") return url.href;
  } catch {}
}
export function mediaKind(url: string) {
  return /\.(mp4|webm|mov)(?:[?#]|$)/i.test(url) ? "video" : "image";
}
