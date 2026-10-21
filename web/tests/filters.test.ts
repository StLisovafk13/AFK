import { test } from "node:test";
import assert from "node:assert/strict";
import { parseFilters, positiveId } from "../lib/filters";
import { safeURL, mediaKind } from "../lib/types";
test("IDs remain precise strings and reject invalid or overflowing inputs", () => {
  assert.equal(positiveId("9007199254740993"), "9007199254740993");
  for (const id of ["0", "-1", "abc", "1 OR 1=1", "9223372036854775808"])
    assert.throws(() => positiveId(id));
  assert.throws(() =>
    parseFilters(new URLSearchParams({ chat: "-9223372036854775809" })),
  );
  assert.equal(
    parseFilters(new URLSearchParams({ chat: "-100123" })).chat,
    "-100123",
  );
});
test("profile filters preserve cursor, coordinate and exact-match options", () => {
  const f = parseFilters(
    new URLSearchParams({ q: "@user", geo: "1", cursor: "123" }),
  );
  assert.equal(f.username, "user");
  assert.equal(f.geo, true);
  assert.equal(f.cursor, "123");
  assert.equal(parseFilters(new URLSearchParams({ exact: "1" })).exact, true);
  assert.throws(() =>
    parseFilters(new URLSearchParams({ q: "x".repeat(101) })),
  );
});
test("media links reject executable URLs", () => {
  for (const url of [
    "javascript:alert(1)",
    "data:text/html,test",
    "file:///secret",
  ])
    assert.equal(safeURL(url), undefined);
  assert.equal(
    safeURL("https://example.com/image.jpg"),
    "https://example.com/image.jpg",
  );
  assert.equal(mediaKind("https://example.com/clip.mp4?token=abc"), "video");
});
