import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

async function sourceModule(name) {
  const source = await readFile(new URL(`../src/console/backend/${name}.ts`, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } });
  return import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);
}

const { encodeSyncScope } = await sourceModule("normalize");
const { assertPublicOperation } = await sourceModule("public-scope");
const all = encodeSyncScope({ mode: "all" });
assert.deepEqual(all, { scope_mode: "selected", sources: '["um"]', consumers: '["ZCode"]' });
assertPublicOperation("model.sync_apply", "model", all);

// A Gemini model supplied by UM is part of UM's catalog and remains selectable.
const selected = encodeSyncScope({ mode: "selected", models: { um: ["gemini-2.5-pro"] } });
assert.deepEqual(selected.models, { um: ["gemini-2.5-pro"] });
assertPublicOperation("model.sync_apply", "model", selected);

assert.throws(() => encodeSyncScope({ mode: "selected" }));
assert.throws(() => encodeSyncScope({ mode: "selected", consumers: ["unregistered-client"] }));
assert.throws(() => encodeSyncScope({ mode: "selected", sources: ["unregistered-source"] }));
assert.throws(() => assertPublicOperation("service.start", "unregistered-service", {}));
assert.throws(() => assertPublicOperation("model.sync_apply", "model", { scope_mode: "all" }));
assert.throws(() => assertPublicOperation("model.settings_apply", "model", { selection: { "foreign|model": 1 } }));
console.log("Public scope checks passed: upstream models, ZCode writes, and rejected scope expansion.");
