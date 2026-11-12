import test from "node:test";
import assert from "node:assert/strict";
import { logOverviewFixture } from "../console-gate/log-overview-fixture.mjs";
const fixtures = {
  "/api/console/log-sources": {
    body: {
      sources: [
        {
          source: "a",
          categories: [{ category: "service" }, { category: "errors" }],
        },
        { source: "b", categories: [{ category: "service" }] },
        { source: "empty", categories: [] },
      ],
    },
  },
  "/api/console/log-entries?source=a&category=service": {
    body: {
      status: "ok",
      rows: [{ ts: "2026-10-11T00:00:00Z", message: "event", parse_ok: true }],
      file_key: "test-a",
    },
  },
};
test("aggregate fixture does not duplicate a captured file into uncaptured targets", () => {
  const result = logOverviewFixture(fixtures);
  assert.equal(result.rows.length, 1);
  assert.equal(result.rows[0].source, "a");
  assert.equal(result.rows[0].category, "service");
  assert.equal(result.rows[0].ts_norm, 1791676800);
  assert.equal(result.coverage.full_history, false);
  assert.equal(result.coverage.targets_read, 1);
  assert.equal(result.targets.find((t) => t.source === "b").rows, null);
  assert.equal(
    result.sources.find((s) => s.source === "empty").status,
    "no_targets"
  );
});
test("aggregate fixture includes a second category without replacing the first", () => {
  const result = logOverviewFixture(fixtures, {
    "a/errors": {
      status: "ok",
      rows: [{ ts: null, message: "warning", parse_ok: false }],
    },
  });
  assert.deepEqual(
    result.rows.map((r) => r.category),
    ["service", "errors"]
  );
  assert.equal(result.rows[1].ts_norm, null);
  assert.equal(result.sources[0].status, "ok");
});
test("failed aggregate target stays unread and cannot supply stale rows", () => {
  const result = logOverviewFixture(fixtures, {
    "a/service": { status: "error", rows: [{ message: "stale" }] },
  });
  assert.equal(result.status, "unavailable");
  assert.equal(result.rows.length, 0);
  assert.equal(result.targets[0].rows, null);
});
