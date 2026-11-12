import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { modelDemoOps } from "../console-gate/model-demo-ops.mjs";
const fixture = JSON.parse(
  readFileSync(
    new URL(
      "../../../tests/fixtures/frontend_contract_rev25.json",
      import.meta.url
    ),
    "utf8"
  )
);
const scope = { scope_mode: "all" };
test("offline demo preserves edits, binds exact versions, and consumes drafts only once", () => {
  const ops = modelDemoOps(fixture),
    key = "um|fixture-model";
  const row = () =>
    ops["model.context_overrides"]().overrides.find(
      (r) => `${r.source}|${r.model_id}` === key
    );
  const original = row(),
    version = original.settings_draft_version;
  ops["model.settings_save"]({
    source: "um",
    model_id: "fixture-model",
    expected_draft_version: version,
    alias: "评估名称",
    contextSource: "manual",
    contextTokens: 65536,
  });
  assert.equal(row().settings_draft.contextTokens, 65536);
  assert.equal(row().settings_draft.alias, "评估名称");
  assert.equal(
    row().settings_draft.defaultEffort,
    original.settings_draft.defaultEffort
  );
  assert.throws(() =>
    ops["model.settings_save"]({
      source: "um",
      model_id: "fixture-model",
      expected_draft_version: version,
      alias: "旧响应",
    })
  );
  const p = { ...scope, selection: { [key]: version + 1 } };
  const preview = ops["model.sync_preview"](p);
  assert.equal(preview.preview.changes[0].draft.contextTokens, 65536);
  assert.throws(() =>
    ops["model.sync_apply"]({
      ...p,
      scope_mode: "selected",
      preview_token: preview.preview_token,
    })
  );
  const applied = ops["model.sync_apply"]({
    ...p,
    preview_token: preview.preview_token,
  });
  assert.deepEqual(applied.settingsApplied, [key]);
  assert.equal(row().settings_draft, null);
  assert.equal(row().settings_applied.contextTokens, 65536);
  assert.equal(applied.settingsReadback.clientReadbackAvailable, false);
  assert.equal(applied.applicationBoundary.published, false);
  assert.deepEqual(applied.appliedDetails, []);
  assert.throws(() =>
    ops["model.sync_apply"]({ ...p, preview_token: preview.preview_token })
  );
});
test("ordinary preview binds without consuming unrelated drafts; scoped catalog excludes stopped sources", () => {
  const ops = modelDemoOps(fixture),
    before = ops["model.context_overrides"]();
  assert.deepEqual(
    Object.keys(ops["model.catalog"]({ sources: '["um"]' }).catalogs),
    ["um"]
  );
  assert.throws(() => ops["model.sync_preview"]({ scope_mode: "selected" }));
  const preview = ops["model.sync_preview"](scope);
  const result = ops["model.sync_apply"]({
    ...scope,
    preview_token: preview.preview_token,
  });
  assert.deepEqual(result.settingsApplied, []);
  assert.deepEqual(ops["model.context_overrides"](), before);
});
