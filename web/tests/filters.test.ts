import { test } from "node:test";
import assert from "node:assert/strict";
import { parseFilters, where, positiveId } from "../lib/filters";
import { safeURL, mediaKind } from "../lib/types";
test("SQL keeps search input in bound parameters and escapes wildcards", () => {
  const w = where(
    parseFilters(
      new URLSearchParams({
        q: "x%' OR 1=1 --_",
        chat: "-100123",
        cursor: "999",
      }),
    ),
  );
  assert.ok(!w.sql.includes("OR 1=1"));
  assert.deepEqual(w.values, ["%x\\%' OR 1=1 --\\_%", "-100123", "999"]);
  assert.ok(w.sql.includes("i.chat_id = $2::bigint"));
  assert.ok(w.sql.includes("i.id < $3::bigint"));
});
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
test("coordinates use geographic bounds; profile queries ignore item cursor predicate", () => {
  const f = parseFilters(
    new URLSearchParams({ q: "@user", geo: "1", cursor: "123" }),
  );
  assert.equal(f.username, "user");
  const w = where(f, false);
  assert.ok(w.sql.includes("BETWEEN -90 AND 90"));
  assert.ok(!w.sql.includes("i.id <"));
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
