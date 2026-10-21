export type Query = <T>(text: string, values?: unknown[]) => Promise<T[]>;
import type { Detail, Summary } from "./types";
export type Chunk = { file: string; min: string; max: string };
export type ProfileGroup = {
  username: string;
  chat_id: string;
  count: number;
  latest: string;
  image_url: string;
  geo_count: number;
  geo_latest: string;
  geo_image_url: string;
  chunks: number[];
  geoChunks: number[];
};
export type Catalog = {
  version: 1;
  createdAt: string;
  summary: Summary;
  profiles: ProfileGroup[];
  chunks: Chunk[];
  geoChunks: Chunk[];
};
export type Manifest = {
  version: 1;
  id: string;
  salt: string;
  iterations: number;
  catalog: string;
};
export type SnapshotRow = Detail;
