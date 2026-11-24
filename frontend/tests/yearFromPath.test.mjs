import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { yearFromPath, resolveHistEpoch } from "../src/api/yearFromPath.ts";

const vectors = JSON.parse(
  readFileSync(new URL("../../backend/tests/data/year_from_path_vectors.json", import.meta.url), "utf8")
);

test("yearFromPath matches the shared vectors", () => {
  assert.ok(vectors.length > 0);
  for (const { path, year } of vectors) {
    assert.equal(yearFromPath(path), year, path);
  }
});

test("resolveHistEpoch", () => {
  assert.equal(resolveHistEpoch({ hist_epoch: 2 }, "event/2026mmr"), 2);
  const m = { hist_epoch: 2, hist_epochs: { "2026": 3 } };
  assert.equal(resolveHistEpoch(m, "event/2026mmr"), 3);
  assert.equal(resolveHistEpoch(m, "event/2025cc"), 2);
  assert.equal(resolveHistEpoch(m, "teams/all"), 2);
  assert.equal(resolveHistEpoch({ hist_epoch: 4, hist_epochs: { "2026": 3 } }, "event/2026mmr"), 4);
});
