// Cloud console write contracts: operation outcome wording, pending-intent
// identity and bounded persistence. The single-page reads are covered by
// scripts/check-cloud-console.mjs.
//
// The module is bundled with esbuild because the console sources use
// extensionless relative imports that Node ESM cannot resolve directly.
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const require = createRequire(new URL("../../package.json", import.meta.url));
const { buildSync } = require("esbuild");

const bundleCloud = (entry) =>
  buildSync({
    entryPoints: [fileURLToPath(new URL(entry, import.meta.url))],
    bundle: true,
    format: "cjs",
    platform: "browser",
    write: false,
    loader: { ".css": "empty" },
    define: {
      "import.meta.env.MODE": '"test"',
      __CONSOLE_ROLE__: '"cloud"',
      __CONSOLE_CANDIDATE__: "false",
    },
  }).outputFiles[0].text;
const loadCloud = (code) => {
  const module = { exports: {} };
  vm.runInNewContext(code, {
    module,
    exports: module.exports,
    console,
    Intl,
    Date,
    Map,
    Set,
    Math,
    Number,
    String,
    Array,
    Object,
    JSON,
    Uint8Array,
  });
  return module.exports;
};
const intent = loadCloud(bundleCloud("../../src/cloud/operation-intent.ts"));

const op = (overrides = {}) => ({
  id: "op-1",
  action: "account.login",
  target: "share",
  state: "succeeded",
  created_at: 1793413300,
  finished_at: 1793413301,
  result: {
    triggered: true,
    state: "logging_in",
    label: "登录进行中",
    started_here: true,
    restored: null,
    note: "登录进行中——请在弹出的专用实例浏览器窗口完成 UM 登录",
  },
  error: null,
  steps: [],
  idempotency_key: "idem-op-1",
  ...overrides,
});

test("A1: a login trigger is 已触发/等待本人登录, never 已完成", () => {
  const loggingIn = intent.operationOutcome(op());
  assert.equal(loggingIn.word, "已触发 · 等待本人登录");
  assert.equal(loggingIn.tone, "warn");
  assert.notEqual(loggingIn.word, "已完成");
  assert.equal(
    intent.operationOutcome(
      op({
        result: { triggered: true, state: "needs_login", label: "需要登录" },
      })
    ).word,
    "已受理 · 未观察到登录会话"
  );
  assert.equal(
    intent.operationOutcome(
      op({ result: { triggered: true, state: "failed", label: "失败" } })
    ).tone,
    "err"
  );
  assert.equal(
    intent.operationOutcome(
      op({ result: { triggered: true, state: "unknown" } })
    ).word,
    "已受理 · 认证状态未确认"
  );
  assert.equal(
    intent.operationOutcome(
      op({ result: { triggered: true, state: "valid", label: "认证有效" } })
    ).word,
    "认证有效"
  );
  assert.equal(
    intent.operationOutcome(
      op({ result: { triggered: false, state: "valid", label: "认证有效" } })
    ).word,
    "认证已有效（无需触发）"
  );
});

test("A1: a normal window close is not a disconnect or a success", () => {
  const result = intent.operationOutcome(
    op({
      result: { triggered: true, state: "logging_in", note: "请完成 UM 登录" },
    })
  );
  assert.match(result.text, /完成 UM 登录/);
  assert.match(result.text, /正常关闭窗口不等于断线或失败/);
  assert.doesNotMatch(result.text, /已完成|认证成功/);
});

test("A2: backend resource contention is reported with its own code", () => {
  const outcome = intent.operationOutcome(
    op({
      state: "failed",
      result: null,
      error: {
        code: "resource_busy_timeout",
        message: "resource lock wait exceeded 15.0s",
      },
    })
  );
  assert.equal(outcome.tone, "err");
  assert.match(outcome.text, /resource_busy_timeout/);
  assert.match(outcome.text, /资源互斥等待超时/);
  const superseded = intent.operationOutcome(
    op({
      state: "cancelled",
      result: null,
      error: {
        code: "superseded",
        message: "resource changed; late result discarded",
      },
    })
  );
  assert.match(superseded.text, /superseded/);
  assert.match(superseded.text, /同一资源上的新操作取代/);
});

test("A2: the original identity finds the operation by key, action and target", () => {
  const rows = [
    op({ id: "a", idempotency_key: "idem-a" }),
    op({ id: "b", idempotency_key: "idem-b" }),
  ];
  assert.equal(
    intent.findOperationByIdentity(
      rows,
      "account.login",
      "share",
      "idem-b"
    ).id,
    "b"
  );
  assert.equal(
    intent.findOperationByIdentity(
      rows,
      "account.login",
      "other",
      "idem-b"
    ),
    null
  );
  assert.equal(
    intent.findOperationByIdentity(rows, "share.status", "share", "idem-b"),
    null
  );
  assert.equal(intent.findOperationById(rows, "a").id, "a");
});

test("A2: the submit body carries the stable non-secret idempotency key", () => {
  const body = intent.submitBody({
    key: "idem-x",
    action: "share.status",
    target: "share",
    at: 1,
    opId: null,
  });
  assert.equal(body.action, "share.status");
  assert.equal(body.target, "share");
  assert.equal(body.idempotency_key, "idem-x");
  assert.equal(Object.keys(body).length, 3);
});

test("A2: persisted intents are bounded and malformed rows are dropped", () => {
  assert.equal(intent.parsePendingIntents(null).length, 0);
  assert.equal(intent.parsePendingIntents("{").length, 0);
  const parsed = intent.parsePendingIntents(
    JSON.stringify([
      { key: "k1", action: "share.status", target: "share", at: 1, opId: null },
      { key: "", action: "share.status", target: "share", at: 1 },
      {
        key: "k2",
        action: "share.status",
        target: "share",
        at: 2,
        opId: "op-2",
      },
    ])
  );
  assert.equal(parsed.length, 2);
  assert.equal(parsed[1].opId, "op-2");
  const many = Array.from({ length: 12 }, (_, i) => ({
    key: `k${i}`,
    action: "a",
    target: "t",
    at: i,
    opId: null,
  }));
  assert.equal(
    intent.parsePendingIntents(JSON.stringify(many)).length,
    intent.MAX_PENDING_INTENTS
  );
  assert.match(intent.newIntentKey(), /^idem-/);
});
