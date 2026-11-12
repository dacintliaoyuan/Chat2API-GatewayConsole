// Cloud-console regressions for the A2 submit answer, the stale-read wording,
// the R2 current-vs-historical auth reading, the R4 plaintext_viewable wording
// and the U first-screen/fold layout. The shipped modules are bundled with the
// project's own esbuild, so these checks bind the code the page runs rather than
// a copy of it.
//
//   node --test scripts/check-cloud-console.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { loadPlaywright } from "./console-gate/core.mjs";

const WEB = fileURLToPath(new URL("..", import.meta.url));
const ORIGIN = "http://cloud-intent.invalid";
const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");

function load(contents, sourcefile) {
  const code = buildSync({
    stdin: { contents, resolveDir: WEB, sourcefile, loader: "ts" },
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
  const module = { exports: {} };
  vm.runInContext(
    code,
    vm.createContext({
      module,
      exports: module.exports,
      console,
      URL,
      window: {},
      document: {},
      location: { origin: "http://cloud-console.invalid" },
    })
  );
  return module.exports;
}

const cloud = load(
  `export { submitAnswer, plainNotice, CREATE_PLAIN_NOTE, PLAIN_DIALOG_NOTE, submitCreate, submitConcurrency, revokeKey } from "./src/cloud/cloud-console.ts";`,
  "cloud-submit-entry.ts"
);
const overview = load(
  `export { umState, authHeadline, overviewHtml, cnyBound, when, clock } from "./src/cloud/overview.ts";`,
  "cloud-overview-entry.ts"
);
const intent = load(
  `export { loginOutcome } from "./src/cloud/operation-intent.ts";`,
  "cloud-intent-entry.ts"
);

const operation = (id, state = "running") => ({
  id,
  action: "share.status",
  target: "share",
  state,
});

test("A2: only an explicit 4xx without an operation id is a refusal", () => {
  for (const status of [400, 401, 403, 404, 409, 410, 422]) {
    assert.equal(
      cloud.submitAnswer(status, { error: "refused" }, "share.status", "share")
        .kind,
      "refused",
      `HTTP ${status}`
    );
  }
  for (const status of [408, 429, 500, 502, 503, 504]) {
    assert.equal(
      cloud.submitAnswer(status, { error: "unknown" }, "share.status", "share")
        .kind,
      "unconfirmed",
      `HTTP ${status}`
    );
  }
  assert.equal(
    cloud.submitAnswer(200, {}, "share.status", "share").kind,
    "unconfirmed",
    "a 200 without an operation id"
  );
  assert.equal(
    cloud.submitAnswer(200, null, "share.status", "share").kind,
    "unconfirmed",
    "an unparseable answer"
  );
  assert.equal(
    cloud.submitAnswer(
      200,
      { operation: operation("op-1") },
      "share.status",
      "share"
    ).kind,
    "accepted"
  );
});

test("A2: an operation id for another intent is not accepted as this submit", () => {
  const answer = cloud.submitAnswer(
    200,
    {
      operation: {
        id: "op-2",
        action: "share.stop",
        target: "share",
        state: "running",
      },
    },
    "share.status",
    "share"
  );
  assert.equal(answer.kind, "unconfirmed");
});

test("A1: non-credential failed states are not an auth failure", () => {
  for (const error of [
    "superseded",
    "BrowserDisabled",
    "manual_takeover:umpass_prefill_missing",
    "umpass_mfa_required",
    "umpass_prefill_missing",
    "timeout",
    "window_closed",
    "attempts_exhausted",
  ]) {
    const outcome = intent.loginOutcome({
      state: "failed",
      error,
      note: "instance note",
      recovery: { action: "operator", detail: "instance recovery" },
    });
    assert.equal(outcome.tone, "warn", error);
    assert.notEqual(outcome.word, "认证失败", error);
    assert.match(outcome.text, /instance recovery/, error);
  }
  const failed = intent.loginOutcome({
    state: "failed",
    error: "login_failed",
  });
  assert.equal(failed.tone, "err");
  assert.equal(failed.word, "认证失败");
});

test("A2: an answer that carries an operation id is accepted even with a non-2xx status", () => {
  for (const status of [500, 408, 429]) {
    const answer = cloud.submitAnswer(
      status,
      { operation: operation("op-1") },
      "share.status",
      "share"
    );
    assert.equal(answer.kind, "accepted", `HTTP ${status}`);
    assert.equal(answer.operation.id, "op-1");
  }
});

test("R2: a stale status read is not a current green UM service/auth on the single page", () => {
  const staleStatus = {
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at: Date.now(),
    last: {
      at: Date.now() - 60_000,
      value: {
        account: { running: true, valid: true },
        last_check_at: Math.floor((Date.now() - 60_000) / 1000),
      },
    },
  };
  const um = overview.umState(staleStatus, { state: "ready", value: [] });
  assert.equal(um.stale, true);
  const html = overview.overviewHtml(overviewModel({ status: staleStatus }));
  assert.doesNotMatch(html, /认证有效（当前读数）/);
  assert.doesNotMatch(html, /已登录/);
  assert.doesNotMatch(html, /UM 后端/);
});

test("A2: a failed operations read marks only the dated operations evidence stale", () => {
  const status = {
    state: "ready",
    at: 1_700_000_000_000,
    value: {
      account: { running: true, valid: true },
      last_check_at: 1_700_000_000,
    },
  };
  const operationRows = [
    {
      id: "op-1",
      action: "share.status",
      target: "share",
      state: "succeeded",
      created_at: 1_699_999_000,
      finished_at: 1_699_999_001,
      result: {
        layers: { share_backend: "running" },
        backend_auth: { state: "valid", label: "认证有效" },
      },
      error: null,
      steps: [],
      idempotency_key: null,
    },
  ];
  const freshOps = {
    state: "ready",
    at: 1_700_000_000_000,
    value: operationRows,
  };
  const staleOps = {
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at: 1_700_000_000_000,
    last: { at: 1_699_999_900_000, value: operationRows },
  };
  assert.equal(overview.umState(status, freshOps).stale, false);
  // A failed operations read only makes the dated operations evidence stale.
  // The status read is still current, so its backend/auth words are NOT stale.
  assert.equal(overview.umState(status, staleOps).stale, false);
  assert.equal(overview.umState(status, staleOps).opsStale, true);
  // The current status carries the account layer, so its valid word is current;
  // the stale operations read only affects the dated history.
  assert.equal(overview.umState(status, staleOps).auth.word, "有效");
  assert.equal(overview.umState(status, staleOps).authReported, true);
});

test("R2: a stale operations read never hides a current invalid auth", () => {
  const readAt = 1_700_000_000;
  const status = statusRead({
    account: { running: true, valid: false },
    last_check_at: readAt,
  });
  const staleOps = {
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at: readAt * 1000,
    // The last operations read still carries an older valid observation.
    last: {
      at: (readAt - 60) * 1000,
      value: [statusOp("valid", readAt - 120)],
    },
  };
  const um = overview.umState(status, staleOps);
  assert.equal(um.stale, false, "a fresh status read is not stale");
  assert.equal(um.opsStale, true);
  assert.equal(
    um.auth.word,
    "无效",
    "the fresh account layer is the current word"
  );
  const headline = overview.authHeadline(um);
  assert.ok(headline, "a current invalid auth must still be reported");
  assert.equal(headline.title, "UM 认证无效");
  const html = overview.overviewHtml(
    overviewModel({ status, operations: staleOps })
  );
  assert.doesNotMatch(html, /UM 认证无效/);
  assert.doesNotMatch(html, /当前认证：无效/);
  assert.doesNotMatch(
    html,
    /无效（读数已过时）/,
    "the deleted status card must not keep the current invalid word"
  );
  assert.doesNotMatch(
    html,
    /分层检查\/历史认证来自过时读数/,
    "the deleted evidence fold must not mark operations evidence stale"
  );
});

test("R17/R9: a stale logging_in observation is history, not the current login progress", () => {
  const readAt = 1_700_000_000;
  const observedAt = readAt - 86_400; // the observation is a day older than the status read
  const status = statusRead({
    account: { running: true, valid: false },
    last_check_at: readAt,
  });
  const staleOps = {
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at: readAt * 1000,
    // The last operations read still carries a day-old logging_in observation.
    last: { at: observedAt * 1000, value: [loginOp("logging_in", observedAt)] },
  };
  const um = overview.umState(status, staleOps);
  assert.equal(um.opsStale, true);
  assert.equal(um.authReported, true);
  assert.equal(um.auth.word, "无效", "过期 logging_in 不得替代当前失效");
  const headline = overview.authHeadline(um);
  assert.ok(headline, "当前失效仍需首屏提示");
  assert.equal(
    headline.title,
    "UM 认证无效",
    "标题不得采用过期 observation 的登录进度"
  );
  assert.match(headline.detail, /当前认证：无效/);
  assert.doesNotMatch(headline.detail, /当前认证：登录进行中/);
  const historyAt = `历史认证证据：登录动作结果 ${overview.when(observedAt * 1000)}报告登录进行中（读数已过时）`;
  assert.ok(
    headline.detail.includes(historyAt),
    `历史行应保留带自身时间的旧 logging_in 并标过时：${headline.detail}`
  );

  const html = overview.overviewHtml(
    overviewModel({ status, operations: staleOps })
  );
  assert.doesNotMatch(html, /UM 登录已触发，等待本人登录/);
  assert.doesNotMatch(html, /当前认证：登录进行中/);
  assert.doesNotMatch(html, /登录动作/);
  assert.equal(
    um.observation?.state,
    "logging_in",
    "过时读数仍保留旧记录作为历史证据"
  );
  assert.equal(um.currentLogin, null, "过时读数不能证明登录仍在进行");
});

test("R17/R9: a successful operations read with a day-old terminal logging_in stays history", () => {
  const readAt = 1_700_000_000;
  const observedAt = readAt - 86_400; // the terminal login ran a day before this read
  const status = statusRead({
    account: { running: true, valid: false },
    last_check_at: readAt,
  });
  const ops = {
    state: "ready",
    at: readAt * 1000,
    value: [loginOp("logging_in", observedAt)],
  };
  const um = overview.umState(status, ops);
  assert.equal(um.opsStale, false, "成功的 operations 读数不是过时");
  assert.equal(um.authReported, true);
  assert.equal(um.auth.word, "无效", "终态 logging_in 不得替代当前失效");
  assert.equal(um.currentLogin, null, "终态 logging_in 不是当前登录进度");
  const headline = overview.authHeadline(um);
  assert.ok(headline, "当前失效仍需首屏提示");
  assert.equal(
    headline.title,
    "UM 认证无效",
    "标题不得采用终态 observation 的登录进度"
  );
  assert.match(headline.detail, /当前认证：无效/);
  assert.doesNotMatch(headline.detail, /当前登录进度/);
  assert.doesNotMatch(headline.detail, /当前认证：登录进行中/);
  const historyAt = `历史认证证据：登录动作结果 ${overview.when(observedAt * 1000)}报告登录进行中`;
  assert.ok(
    headline.detail.includes(historyAt),
    `历史行应保留带自身时间的旧 logging_in：${headline.detail}`
  );

  const html = overview.overviewHtml(
    overviewModel({ status, operations: ops })
  );
  assert.doesNotMatch(html, /UM 认证无效/);
  assert.doesNotMatch(html, /UM 登录进行中/);
  assert.doesNotMatch(html, /当前登录进度/);
  assert.doesNotMatch(html, /登录动作/);
});

test("R17/R9: a missing account layer stays unreported even with a terminal logging_in", () => {
  const readAt = 1_700_000_000;
  const observedAt = readAt - 86_400;
  const status = statusRead({ last_check_at: readAt }); // no account layer
  const ops = {
    state: "ready",
    at: readAt * 1000,
    value: [loginOp("logging_in", observedAt)],
  };
  const um = overview.umState(status, ops);
  assert.equal(um.authReported, false);
  assert.equal(um.auth.word, "未上报");
  assert.equal(um.auth.tone, "neutral");
  assert.equal(um.currentLogin, null);
  const headline = overview.authHeadline(um);
  assert.equal(headline.title, "UM 认证未上报");
  assert.doesNotMatch(headline.title, /登录进行中|等待本人登录/);
  assert.match(headline.detail, /当前认证未上报/);
  assert.match(headline.detail, /历史认证证据：登录动作结果 .*报告登录进行中/);
  assert.doesNotMatch(
    headline.detail.replace(/历史认证证据：.*/, ""),
    /登录进行中|等待本人登录/
  );
});

test("R17/R9: a current valid account is not overridden by a historical logging_in", () => {
  const readAt = 1_700_000_000;
  const status = statusRead({
    account: { running: true, valid: true },
    last_check_at: readAt,
  });
  const ops = {
    state: "ready",
    at: readAt * 1000,
    value: [loginOp("logging_in", readAt - 86_400)],
  };
  const um = overview.umState(status, ops);
  assert.equal(um.auth.word, "有效");
  assert.equal(um.auth.tone, "ok");
  assert.equal(um.currentLogin, null);
  assert.equal(
    overview.authHeadline(um),
    null,
    "当前有效不因历史登录进度报问题"
  );
  const html = overview.overviewHtml(
    overviewModel({ status, operations: ops })
  );
  assert.doesNotMatch(html, /UM 认证有效/);
  assert.doesNotMatch(html, /登录进行中|等待本人登录/);
});

test("R17/R9: only a queued or running login operation is current login progress", () => {
  const readAt = 1_700_000_000;
  const status = statusRead({
    account: { running: true, valid: false },
    last_check_at: readAt,
  });
  const loginAt = readAt - 30;
  const ops = {
    state: "ready",
    at: readAt * 1000,
    value: [runningLoginOp("running", loginAt)],
  };
  const um = overview.umState(status, ops);
  assert.equal(um.currentLogin?.state, "running");
  assert.equal(um.currentLogin?.at, loginAt * 1000);
  assert.equal(um.currentLogin?.action, "share.backend_login");
  assert.equal(um.auth.word, "无效", "当前登录进度不改写当前认证");
  const headline = overview.authHeadline(um);
  assert.equal(headline.title, "UM 登录进行中");
  assert.match(headline.detail, /当前认证：无效/);
  assert.match(headline.detail, /当前登录进度：执行中 · 启动 UM 登录流程/);
  const html = overview.overviewHtml(
    overviewModel({ status, operations: ops })
  );
  assert.doesNotMatch(html, /UM 登录进行中/);
  assert.doesNotMatch(html, /当前登录进度：执行中/);
  assert.doesNotMatch(html, /登录动作/);

  // queued is also in-flight progress, and re-auth uses its own action word.
  const queued = overview.umState(status, {
    state: "ready",
    at: readAt * 1000,
    value: [runningLoginOp("queued", loginAt, "share.backend_reauthenticate")],
  });
  assert.equal(queued.currentLogin?.state, "queued");
  assert.equal(queued.currentLogin?.at, loginAt * 1000);
  assert.equal(queued.currentLogin?.action, "share.backend_reauthenticate");
  assert.equal(overview.authHeadline(queued).title, "UM 登录进行中");
  assert.match(
    overview.authHeadline(queued).detail,
    /当前登录进度：排队中 · 重新认证 UM/
  );
});

test("R2: a stale status hero keeps the status read time, not the operations read time", () => {
  const statusAt = Date.now() - 7_200_000;
  const opsAt = Date.now() - 3_600_000;
  const lastCheckAt = statusAt - 3_600_000;
  const staleStatus = {
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at: Date.now(),
    last: {
      at: statusAt,
      value: {
        account: { running: true, valid: true },
        last_check_at: Math.floor(lastCheckAt / 1000),
      },
    },
  };
  const um = overview.umState(staleStatus, ready([], opsAt));
  assert.equal(um.stale, true);
  assert.equal(
    um.at,
    Math.floor(lastCheckAt / 1000) * 1000,
    "the stale status reading keeps its own last_check_at"
  );
  assert.notEqual(um.at, opsAt);
  const html = overview.overviewHtml(
    overviewModel({
      status: staleStatus,
      operations: ready([], opsAt),
    })
  );
  assert.doesNotMatch(html, /cloud-hero/);
  assert.doesNotMatch(html, /状态读数保留/);
  const statusClock = overview.clock(statusAt);
  const lastCheckClock = overview.clock(lastCheckAt);
  const opsClock = overview.clock(opsAt);
  assert.match(html, new RegExp(`数据更新于 ${statusClock}`));
  assert.doesNotMatch(html, new RegExp(`数据更新于 ${lastCheckClock}`));
  assert.doesNotMatch(html, new RegExp(`数据更新于 ${opsClock}`));
});

test("R2: stale operations and keys reads are not current first-screen issues", () => {
  const at = Date.now();
  const staleOps = {
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at,
    last: {
      at: at - 60_000,
      value: {
        available: true,
        readAt: Math.floor(at / 1000),
        security: {
          configured: true,
          available: true,
          status: null,
          reason: null,
          remote: null,
          generationFloor: 7,
          records: 12,
          pendingRemote: 2,
          exportedAt: "2026-11-02T10:00:00+08:00",
        },
        reasons: [],
        unresolved: null,
      },
    },
  };
  const staleKeys = {
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at,
    last: {
      at: at - 60_000,
      value: [
        { id: "k1", name: "alice", masked: "um-share-****", enabled: false },
      ],
    },
  };
  const html = overview.overviewHtml(
    overviewModel({
      security: staleOps,
      keys: staleKeys,
    })
  );
  assert.match(html, /未得到应答/);
  assert.doesNotMatch(html, /API Key 读数已过时/);
  assert.doesNotMatch(html, /安全读数已过时/);
  assert.doesNotMatch(html, /安全状态仍有待上传记录/);
  assert.doesNotMatch(html, /还没有启用的 API Key/);
  assert.doesNotMatch(html, /需要处理的问题/);
});

test("R2: the deleted services page no longer consumes the health read", () => {
  const health = {
    state: "failed",
    kind: "no-answer",
    reason: "连接被拒绝",
    status: null,
    at: Date.now(),
    last: null,
  };
  const html = overview.overviewHtml(overviewModel({ health }));
  assert.doesNotMatch(html, /进程快照/);
  assert.doesNotMatch(html, /连接被拒绝/);
  assert.doesNotMatch(html, /需要处理的问题/);
});

test("R17: the deleted ops page no longer consumes the security read", () => {
  const security = {
    state: "failed",
    kind: "no-answer",
    reason: "连接被拒绝",
    status: null,
    at: Date.now(),
    last: null,
  };
  const html = overview.overviewHtml(overviewModel({ security }));
  assert.doesNotMatch(html, /安全读数/);
  assert.doesNotMatch(html, /连接被拒绝/);
  assert.doesNotMatch(html, /需要处理的问题/);
  assert.doesNotMatch(html, /data-cloud-go="ops"/);
});

test("R2: stale status and keys reads do not become current KPI values", () => {
  const at = Date.now();
  const staleStatus = {
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at,
    last: {
      at: at - 60_000,
      value: {
        share_max_concurrency: 64,
        capacity: { in_flight: 3, queued: 2 },
      },
    },
  };
  const staleKeys = {
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at,
    last: {
      at: at - 60_000,
      value: [
        { id: "k1", name: "alice", masked: "um-share-****", enabled: true },
      ],
    },
  };
  const html = overview.overviewHtml(
    overviewModel({
      status: staleStatus,
      keys: staleKeys,
    })
  );
  const start = html.indexOf("cloud-key-concurrency");
  assert.ok(start >= 0, "Key 列表头部没有全局并发读数");
  const card = html.slice(start, html.indexOf("</section>", start));
  assert.match(card, /并发 未上报 \/ 未上报/);
  assert.match(card, /Key 读数已过时/);
  assert.match(card, /列表与已启用 Key 数当前未知/);
  assert.doesNotMatch(card, /64/);
  assert.doesNotMatch(card, /在途 3/);
  assert.doesNotMatch(card, /已启用 Key 1/);
});

test("R2: stale status and operations reads are not current on the services page", () => {
  const at = Date.now();
  const staleStatus = {
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at,
    last: {
      at: at - 60_000,
      value: {
        account: { running: true, valid: true },
        upstream: { reachable: true },
        last_check_at: Math.floor((at - 60_000) / 1000),
      },
    },
  };
  const staleOps = {
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at,
    last: {
      at: at - 60_000,
      value: [
        {
          id: "op-1",
          action: "share.backend_login",
          target: "share",
          state: "succeeded",
          created_at: Math.floor((at - 60_000) / 1000),
          result: { state: "valid" },
        },
      ],
    },
  };
  const um = overview.umState(staleStatus, staleOps);
  assert.equal(um.stale, true);
  assert.equal(um.opsStale, true);
  const html = overview.overviewHtml(
    overviewModel({
      status: staleStatus,
      operations: staleOps,
    })
  );
  assert.doesNotMatch(html, /上游认证/);
  assert.doesNotMatch(html, /登录动作/);
  assert.doesNotMatch(html, /读数已过时，当前未知/);
});

const statusRead = (value, at = 1_700_000_000_000) => ({
  state: "ready",
  at,
  value,
});
const statusOp = (authState, at) => ({
  id: `op-${authState}`,
  action: "share.status",
  target: "share",
  state: "succeeded",
  created_at: at,
  finished_at: at + 1,
  result: {
    layers: { share_backend: "running" },
    backend_auth: {
      state: authState,
      label: `认证${authState}`,
      observed_at: at,
    },
  },
  error: null,
  steps: [],
  idempotency_key: null,
});
const loginOp = (authState, at) => ({
  id: `op-login-${authState}`,
  action: "share.backend_login",
  target: "share",
  state: "succeeded",
  created_at: at,
  finished_at: at,
  result: { state: authState },
  error: null,
  steps: [],
  idempotency_key: null,
});
const runningLoginOp = (state, at, action = "share.backend_login") => ({
  id: `op-login-${state}-${action}`,
  action,
  target: "share",
  state,
  created_at: at,
  finished_at: null,
  result: null,
  error: null,
  steps: [],
  idempotency_key: null,
});

test("R2: a historical valid observation is not the current green auth", () => {
  const readAt = 1_700_000_000; // last_check_at stamps THIS status read
  const observedAt = 1_699_999_000; // the older auth observation's own time
  const um = overview.umState(statusRead({ last_check_at: readAt }), {
    state: "ready",
    at: readAt * 1000,
    value: [statusOp("valid", observedAt)],
  });
  assert.equal(um.authReported, false, "缺 account 不得声称当前认证已确认");
  assert.equal(um.auth.word, "未上报");
  assert.equal(um.auth.tone, "neutral");
  assert.equal(um.observation?.state, "valid", "历史观察仍需保留为证据");
  assert.equal(um.authAt, observedAt * 1000, "历史证据时间应是观察时间");
  assert.equal(um.at, readAt * 1000, "状态读取时间仍是 last_check_at");
  assert.notEqual(um.authAt, um.at, "last_check_at 不得冒充旧认证的新鲜时间");
});

test("R2: a current account layer confirms valid, invalid and unknown separately", () => {
  const readAt = 1_700_000_000;
  const current = (account, ops = []) =>
    overview.umState(statusRead({ account, last_check_at: readAt }), {
      state: "ready",
      at: readAt * 1000,
      value: ops,
    });
  const valid = current({ running: true, valid: true });
  assert.equal(valid.auth.word, "有效");
  assert.equal(valid.auth.tone, "ok");
  assert.equal(valid.authReported, true);
  // R17/R9: a terminal observation is history, never the current word. Only the
  // account layer decides that this read is 无效.
  const needsLogin = current({ running: true, valid: false }, [
    statusOp("needs_login", readAt - 60),
  ]);
  assert.equal(
    needsLogin.auth.word,
    "无效",
    "终态 needs_login 不得改写当前失效"
  );
  assert.equal(
    needsLogin.currentLogin,
    null,
    "终态 observation 不是当前登录进度"
  );
  assert.notEqual(needsLogin.auth.tone, "ok");
  const needsLoginHeadline = overview.authHeadline(needsLogin);
  assert.equal(needsLoginHeadline.title, "UM 认证无效");
  assert.match(
    needsLoginHeadline.detail,
    /历史认证证据：状态读数 .*报告需要登录/
  );
  const invalid = current({ running: true, valid: false });
  assert.equal(invalid.auth.word, "无效");
  assert.notEqual(invalid.auth.tone, "ok");
  const unreported = current({ running: true });
  assert.equal(unreported.auth.word, "未上报");
  assert.equal(unreported.auth.tone, "neutral");
  assert.equal(unreported.authReported, false);
  const stopped = current({ running: false });
  assert.equal(stopped.auth.word, "无法检查");
});

test("R2: a historical valid never hides a current invalid account layer", () => {
  const readAt = 1_700_000_000;
  const um = overview.umState(
    statusRead({
      account: { running: true, valid: false },
      last_check_at: readAt,
    }),
    {
      state: "ready",
      at: readAt * 1000,
      value: [statusOp("valid", readAt - 60)],
    }
  );
  assert.equal(um.authReported, true);
  assert.equal(um.auth.word, "无效");
  assert.notEqual(um.auth.tone, "ok");
});

test("R4: plaintext wording follows the runtime plaintext_viewable capability", () => {
  const viewable = cloud.plainNotice("created", true);
  assert.match(viewable, /再点开该行查看/);
  assert.doesNotMatch(viewable, /关闭后无法再次查看/);
  const rotateOnly = cloud.plainNotice("rotated", false);
  assert.match(rotateOnly, /关闭后无法再次查看/);
  assert.match(rotateOnly, /只能轮换/);
  const unreported = cloud.plainNotice("created", null);
  assert.match(unreported, /未上报/);
  assert.match(unreported, /不保证/);
  assert.doesNotMatch(unreported, /关闭后无法再次查看/);
  assert.match(cloud.CREATE_PLAIN_NOTE, /弹层直接显示/);
  assert.match(cloud.CREATE_PLAIN_NOTE, /再点开该行查看/);
  assert.match(cloud.CREATE_PLAIN_NOTE, /轮换/);
  assert.doesNotMatch(cloud.CREATE_PLAIN_NOTE, /默认遮蔽/);
  assert.doesNotMatch(cloud.CREATE_PLAIN_NOTE, /详情/);
  assert.doesNotMatch(cloud.CREATE_PLAIN_NOTE, /关闭后无法再次查看/);
  assert.doesNotMatch(cloud.PLAIN_DIALOG_NOTE, /关闭后无法再次查看/);
  assert.doesNotMatch(cloud.PLAIN_DIALOG_NOTE, /默认遮蔽/);
  assert.doesNotMatch(cloud.PLAIN_DIALOG_NOTE, /详情/);
});

test("D18-3: a revoked Key is absent from the list and the page never shows 已停用 or enable/disable", () => {
  // The backend's list_keys filters revoked rows out. The single page renders
  // only the active rows it receives and has no "已停用" state or toggle.
  const html = overview.overviewHtml(
    overviewModel({
      keys: ready([
        {
          id: "k1",
          name: "alice",
          masked: "um-share-****",
          enabled: true,
          max_concurrency: 16,
          row_version: 1,
          plaintext_viewable: true,
        },
      ]),
    })
  );
  assert.match(html, /data-cloud-key-row="k1"/);
  assert.doesNotMatch(html, /data-cloud-key-row="k2"/, "已撤销 Key 仍在列表");
  assert.doesNotMatch(html, /已停用/);
  assert.doesNotMatch(html, /data-cloud-key-action="toggle:/);
  assert.doesNotMatch(html, /启用后才能测试/);
  assert.match(html, /1–48/);
  assert.doesNotMatch(html, /1–64/);
});

test("status detail: stale status and operations readings are marked stale, not current", () => {
  const at = Date.now();
  const staleStatus = {
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at,
    last: {
      at: at - 60_000,
      value: {
        account: { running: true, valid: true },
        upstream: { reachable: true },
        last_check_at: Math.floor((at - 60_000) / 1000),
      },
    },
  };
  const staleOps = {
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at,
    last: {
      at: at - 120_000,
      value: [
        {
          id: "op-old",
          action: "share.backend_restart",
          target: "share",
          state: "succeeded",
          created_at: Math.floor((at - 120_000) / 1000),
        },
      ],
    },
  };
  const html = overview.overviewHtml(
    overviewModel({
      statusOpen: true,
      status: staleStatus,
      operations: staleOps,
    })
  );
  const detail = html.slice(html.indexOf("data-cloud-status-detail"));
  assert.match(detail, /可达 · 读数已过时，不当作当前状态/);
  assert.match(detail, /操作读数已过时，无法确认最近一次分层检查/);
  assert.match(detail, /操作读数已过时，无法确认登录动作/);
  assert.doesNotMatch(detail, /未读取到最近一次分层检查/);
  assert.doesNotMatch(detail, /未读取到登录动作/);
});

test("today usage: a stale usage read is unknown, never the previous figures", () => {
  const model = overviewModel();
  const staleUsage = {
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at: Date.now(),
    last: { at: Date.now() - 60_000, value: model.usage.value },
  };
  const html = overview.overviewHtml(overviewModel({ usage: staleUsage }));
  assert.match(html, /data-cloud-usage-value="requests">未知</);
  assert.match(html, /用量读数已过时/);
  assert.match(html, /今日用量未知/);
});

test("Key list header: the one read-only global concurrency line is 并发 12 / 64", () => {
  const html = overview.overviewHtml(
    overviewModel({
      status: ready({
        account: { running: true, valid: true },
        share_max_concurrency: 64,
        capacity: { in_flight: 12, queued: null },
        last_check_at: Math.floor(Date.now() / 1000),
      }),
    })
  );
  assert.match(html, /并发 12 \/ 64/);
  assert.doesNotMatch(html, /配置并发上限/);
});

test("requests: a loading read is 读取中, never 读取失败 or an empty result", () => {
  const html = overview.overviewHtml(
    overviewModel({ usageRequests: { state: "loading" } })
  );
  const card = html.slice(html.indexOf("data-cloud-requests"));
  assert.match(card, /读取中/);
  assert.match(card, /请求明细读取中/);
  assert.doesNotMatch(card, /读取失败/);
  assert.doesNotMatch(card, /当前筛选没有请求/);
});

test("requests: the 耗时 column reads the ledger duration_ms and marks a missing value", () => {
  const html = overview.overviewHtml(
    overviewModel({
      usageRequests: ready(
        requestsPage([
          requestRow(),
          requestRow({ requestId: "r2", durationMs: null }),
        ])
      ),
    })
  );
  const card = requestsCardOf(html);
  const first = requestRowOf(card, "r1");
  const second = requestRowOf(card, "r2");
  assert.match(first, /1,200 ms/);
  assert.match(second, /未上报/);
  assert.doesNotMatch(first, /后端未提供/);
});

test("D18-4: the create dialog defaults to 16 and caps at 48", async () => {
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ channel: "chrome" });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: "zh-CN",
  });
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== ORIGIN) return route.abort();
    if (url.pathname === "/")
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: HTML,
      });
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({}),
    });
  });
  const page = await context.newPage();
  try {
    await page.goto(`${ORIGIN}/`, { waitUntil: "domcontentloaded" });
    await page.addScriptTag({ content: browserBundle() });
    await page.evaluate(() => {
      window.CloudConsole.mountCloudConsole(document.querySelector("#stage"), {
        verdict: () => ({ state: "confirmed", reason: "" }),
        onChange: () => {},
      });
    });
    await page.waitForSelector("[data-cloud-key-create-open]", {
      timeout: 10000,
    });
    await page.locator("[data-cloud-key-create-open]").first().click();
    const cap = page.locator('.cloud-key-create input[name="max_concurrency"]');
    assert.equal(await cap.inputValue(), "16", "新建并发默认不是 16");
    assert.equal(await cap.getAttribute("max"), "48", "新建并发上限不是 48");
    assert.equal(await cap.getAttribute("min"), "1", "新建并发下限不是 1");
  } finally {
    await browser.close();
  }
});

test("D18-4: the create request is name plus concurrency, default 16 and 1–48 only", () => {
  const defaults = cloud.submitCreate("  alice  ", "16");
  assert.equal(defaults.kind, "ready");
  assert.equal(defaults.body.name, "alice");
  assert.equal(defaults.body.max_concurrency, 16);
  assert.deepEqual(Object.keys(defaults.body).sort(), [
    "max_concurrency",
    "name",
  ]);
  const cleared = cloud.submitCreate("alice", "");
  assert.equal(cleared.kind, "invalid", "清空的并发被静默改成默认值");
  assert.equal(cleared.field, "max_concurrency");
  assert.equal(cloud.submitCreate("alice", "1").body.max_concurrency, 1);
  assert.equal(cloud.submitCreate("alice", "48").body.max_concurrency, 48);
  for (const raw of ["0", "49", "1.5", "abc", "-1"]) {
    const answer = cloud.submitCreate("alice", raw);
    assert.equal(answer.kind, "invalid", `并发 ${raw} 未被拒绝`);
    assert.equal(answer.field, "max_concurrency", `并发 ${raw} 拒绝字段`);
  }
  const unnamed = cloud.submitCreate("   ", "16");
  assert.equal(unnamed.kind, "invalid");
  assert.equal(unnamed.field, "name");
});

test("D18-5: an inline concurrency edit is a 1–48 integer, unchanged is not a write", () => {
  const changed = cloud.submitConcurrency("24", 2);
  assert.equal(changed.kind, "ready");
  assert.equal(changed.value, 24);
  assert.equal(changed.unchanged, false);
  const same = cloud.submitConcurrency("2", 2);
  assert.equal(same.kind, "ready");
  assert.equal(same.value, 2);
  assert.equal(same.unchanged, true);
  for (const raw of ["", "0", "49", "1.5", "abc"]) {
    const answer = cloud.submitConcurrency(raw, 2);
    assert.equal(answer.kind, "invalid", `并发 ${raw} 未被拒绝`);
  }
});

test("D18-6: revoke targets the tombstone route with the row version only", () => {
  const answer = cloud.revokeKey("k1", 3);
  assert.equal(answer.kind, "ready");
  assert.equal(
    answer.path,
    "/api/console/share-management/keys/k1/revoke-and-terminate"
  );
  assert.deepEqual(Object.keys(answer.body), ["expected_row_version"]);
  assert.equal(answer.body.expected_row_version, 3);
  assert.equal(cloud.revokeKey("k1", undefined).kind, "invalid");
  assert.equal(cloud.revokeKey("", 3).kind, "invalid");
});

test("D18-6: clicking 撤销 opens the irreversible confirm; cancel sends nothing and confirm posts once", async () => {
  const state = {
    posts: [],
    keys: [
      {
        id: "k1",
        name: "alice",
        masked: "um-share-****",
        enabled: true,
        max_concurrency: 2,
        row_version: 1,
        plaintext_viewable: true,
      },
    ],
  };
  const { browser, page, errors } = await mountKeysPage((route, req, path) => {
    if (
      path === "/api/console/share-management/keys/k1/revoke-and-terminate" &&
      req.method() === "POST"
    ) {
      state.posts.push(JSON.parse(req.postData() || "{}"));
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          key: { ...state.keys[0], deleted_at: "2026-11-11T00:00:00Z" },
          revoked: true,
          terminated: 1,
          released: 1,
          aborted: 0,
          upstream_closed: 1,
          note: "已撤销",
        }),
      });
    }
    if (path === "/api/console/share-management/keys")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ keys: state.keys }),
      });
    return cloudBaseReads(route, path);
  });
  try {
    const row = page.locator('[data-cloud-key-row="k1"]');
    await row.waitFor({ timeout: 10000 });
    const dialog = page.locator(".shell-confirm");
    await row.locator('[data-cloud-key-action="revoke:k1"]').click();
    await dialog.waitFor({ state: "visible", timeout: 10000 });
    const text = (await dialog.innerText()).replace(/\s+/g, " ");
    assert.match(text, /撤销后不可恢复/);
    assert.match(text, /正在使用该 Key 的客户端会立即失败/);
    await dialog.locator('button[value="cancel"]').click();
    await dialog.waitFor({ state: "hidden", timeout: 10000 });
    assert.deepEqual(state.posts, [], "取消撤销仍提交了请求");
    await row.locator('[data-cloud-key-action="revoke:k1"]').click();
    await dialog.waitFor({ state: "visible", timeout: 10000 });
    await dialog.locator('button[value="confirm"]').click();
    await dialog.waitFor({ state: "hidden", timeout: 10000 });
    await page.waitForTimeout(200);
    assert.equal(state.posts.length, 1, "撤销没有提交一次");
    assert.deepEqual(Object.keys(state.posts[0]), ["expected_row_version"]);
    assert.equal(state.posts[0].expected_row_version, 1);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test("D18-7: each Key row is name over mask, inline concurrency and 撤销", () => {
  const html = overview.overviewHtml(overviewModel());
  const start = html.indexOf('data-cloud-key-row="k1"');
  assert.ok(start >= 0, "单页没有 Key 行");
  const row = html.slice(start, html.indexOf("</li>", start));
  // 只测可见文本：名字在上、掩码是主视觉。不测 CSS class 名。
  assert.match(row, />alice</);
  assert.match(row, />um-share-\*\*\*\*</);
  const cap = row.match(/<input[^>]*data-cloud-key-cap-input="k1"[^>]*>/);
  assert.ok(cap, "Key 行没有并发输入");
  assert.match(cap[0], /value="2"/);
  assert.match(cap[0], /min="1"/);
  assert.match(cap[0], /max="48"/);
  assert.match(row, /data-cloud-key-action="revoke:k1"[^>]*>撤销</);
  assert.doesNotMatch(row, /显示明文/);
  assert.doesNotMatch(row, /保存并发/);
  const cardStart = html.indexOf('aria-label="API Key"');
  const card = html.slice(cardStart, html.indexOf("</section>", cardStart));
  assert.doesNotMatch(card, /近 14 天请求/);
  assert.doesNotMatch(card, /累计费用/);
  assert.doesNotMatch(html, /API Key 页/);
});

test("D18-8: a revealed Key row shows the full key and a copy action in place", () => {
  const html = overview.overviewHtml(
    overviewModel({
      keySecret: (id) =>
        id === "k1"
          ? {
              secret: "um-share-FULLKEY",
              rowVersion: 1,
              at: Date.now(),
              revealed: true,
            }
          : undefined,
    })
  );
  const start = html.indexOf('data-cloud-key-row="k1"');
  const row = html.slice(start, html.indexOf("</li>", start));
  assert.match(row, /um-share-FULLKEY/);
  assert.doesNotMatch(row, /um-share-\*\*\*\*/);
  assert.match(row, /data-cloud-key-action="copy:k1"[^>]*>复制</);
  assert.match(
    row,
    /data-cloud-key-action="reveal:k1"[^>]*aria-expanded="true"/
  );
});

test("D18-9: no Key shows guidance and one 新建 Key button", () => {
  const html = overview.overviewHtml(overviewModel({ keys: ready([]) }));
  assert.match(html, /还没有 Key/);
  assert.equal(
    (html.match(/data-cloud-key-create-open/g) || []).length,
    1,
    "空列表的「新建 Key」按钮不唯一"
  );
});

test("ticket #10: the inline test button is 测试, 测试中…, 通过 · 耗时 or 失败 · 原因", () => {
  const rowOf = (model) => {
    const html = overview.overviewHtml(model);
    const start = html.indexOf('data-cloud-key-row="k1"');
    assert.ok(start >= 0, "单页没有 Key 行");
    return html.slice(start, html.indexOf("</li>", start));
  };
  const buttonOf = (row) => {
    const match = row.match(
      /<button[^>]*data-cloud-key-action="test:k1"[^>]*>[^<]*<\/button>/
    );
    assert.ok(match, "Key 行没有行内「测试」按钮");
    return match[0];
  };
  const idle = buttonOf(rowOf(overviewModel()));
  assert.match(idle, />测试</);
  assert.doesNotMatch(idle, /测试中/);
  const busy = buttonOf(
    rowOf(
      overviewModel({
        keyTest: () => ({ state: "busy", rowVersion: 1, from: "public" }),
      })
    )
  );
  assert.match(busy, />测试中…</);
  assert.match(busy, / disabled/, "测试进行中按钮仍可再次点击");
  const ok = buttonOf(
    rowOf(
      overviewModel({
        keyTest: () => ({
          state: "ok",
          rowVersion: 1,
          from: "public",
          elapsedMs: 1234,
          model: "DeepSeek-V4.1-Flash-1017",
          at: Date.now(),
        }),
      })
    )
  );
  assert.match(ok, />通过 · 1\.2 s</);
  const failed = buttonOf(
    rowOf(
      overviewModel({
        keyTest: () => ({
          state: "error",
          rowVersion: 1,
          from: "public",
          reason: "Key 明文不可读取，无法测试",
          at: Date.now(),
        }),
      })
    )
  );
  assert.match(failed, />失败 · Key 明文不可读取，无法测试</);
});

function cloudBaseReads(route, path) {
  if (path === "/api/summary")
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ role: "cloud" }),
    });
  if (path === "/api/console/health")
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        checked_at: Math.floor(Date.now() / 1000),
        services: [{ key: "share-gateway", state: "Running", healthy: true }],
      }),
    });
  if (path === "/api/console/actions")
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ actions: [] }),
    });
  if (path.startsWith("/api/console/operations"))
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ operations: [] }),
    });
  if (path === "/api/console/share-management/status")
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        account: { running: true, valid: true },
        last_check_at: Math.floor(Date.now() / 1000),
      }),
    });
  if (path === "/api/console/share-usage")
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        keys: [],
        unattributed: {},
        coverage: {},
        checkedAt: null,
        requests: null,
        filters: null,
      }),
    });
  if (path === "/api/console/cloud-ops")
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({}),
    });
  if (path === "/api/console/share-management/display-session")
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ available: false, reason: "测试" }),
    });
  if (path === "/api/console/share-management/display-state")
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        available: true,
        state: "not_started",
        label: "未开始登录",
      }),
    });
  return route.fulfill({
    status: 404,
    contentType: "application/json",
    body: "{}",
  });
}

async function mountKeysPage(handler, options = {}) {
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ channel: "chrome" });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    serviceWorkers: "block",
    locale: "zh-CN",
    ...options,
  });
  await context.addInitScript(() => {
    const store = { text: "" };
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (text) => {
          store.text = String(text);
        },
        readText: async () => store.text,
      },
    });
  });
  await context.route("**/*", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== ORIGIN) return route.abort();
    const path = url.pathname.replace(/^\/gw\/usagesrv/, "");
    if (path === "/")
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: HTML,
      });
    return handler(route, request, path);
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error.message)));
  await page.goto(`${ORIGIN}/`, { waitUntil: "domcontentloaded" });
  await page.addStyleTag({
    content: readFileSync(join(WEB, "src/shell/tokens.css"), "utf8"),
  });
  await page.addStyleTag({
    content: readFileSync(join(WEB, "src/shell/shell.css"), "utf8"),
  });
  await page.addStyleTag({
    content: readFileSync(join(WEB, "src/cloud/cloud.css"), "utf8"),
  });
  await page.addScriptTag({ content: browserBundle() });
  await page.evaluate((realGate) => {
    const stage = document.querySelector("#stage");
    if (realGate) {
      // The shipped boot: the real gate plus its fetch interceptor.
      const gate = new window.CloudConsole.RoleGate("cloud");
      window.CloudConsole.installRoleGate(gate);
      window.CloudConsole.mountCloudConsole(stage, gate);
      return;
    }
    window.CloudConsole.mountCloudConsole(stage, {
      verdict: () => ({ state: "confirmed", reason: "" }),
      onChange: () => {},
    });
  }, options.realGate === true);
  return { browser, page, errors };
}

test("D18-4: creating a Key shows the full plaintext once with a copy action", async () => {
  const state = { posts: 0, body: null };
  const { browser, page, errors } = await mountKeysPage((route, req, path) => {
    if (
      path === "/api/console/share-management/keys" &&
      req.method() === "POST"
    ) {
      state.posts += 1;
      state.body = JSON.parse(req.postData() || "{}");
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          key: {
            id: "k2",
            name: "newkey",
            masked: "um-share-NEW****",
            plaintext_viewable: true,
            row_version: 1,
          },
          plaintext: "um-share-NEWSECRET",
        }),
      });
    }
    if (path === "/api/console/share-management/keys")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ keys: [] }),
      });
    return cloudBaseReads(route, path);
  });
  try {
    await page.waitForSelector("[data-cloud-key-create-open]", {
      timeout: 10000,
    });
    await page.locator("[data-cloud-key-create-open]").first().click();
    const form = page.locator("dialog.cloud-key-create");
    assert.equal(
      await form.locator("input").count(),
      2,
      "新建对话框不止两个字段"
    );
    assert.equal(await form.locator('input[name="name"]').count(), 1);
    assert.equal(
      await form.locator('input[name="max_concurrency"]').count(),
      1
    );
    for (const raw of ["0", "49", "1.5"]) {
      await form.locator('input[name="max_concurrency"]').fill(raw);
      await page.locator("[data-cloud-key-create-submit]").click();
      await page.waitForTimeout(150);
      assert.equal(state.posts, 0, `非法并发 ${raw} 被提交`);
      assert.match(await form.innerText(), /1–48/, `非法并发 ${raw} 没有提示`);
    }
    await form.locator('input[name="name"]').fill("newkey");
    await form.locator('input[name="max_concurrency"]').fill("16");
    await page.locator("[data-cloud-key-create-submit]").click();
    const plain = page.locator("dialog.cloud-plain");
    await plain.waitFor({ state: "visible", timeout: 10000 });
    assert.match(
      await plain.innerText(),
      /um-share-NEWSECRET/,
      "新建弹层没有显示完整明文"
    );
    assert.equal(state.posts, 1);
    assert.equal(state.body.name, "newkey");
    assert.equal(state.body.max_concurrency, 16);
    await plain.locator("[data-cloud-plain-copy]").click();
    await page.waitForTimeout(150);
    assert.equal(
      await page.evaluate(() => navigator.clipboard.readText()),
      "um-share-NEWSECRET",
      "复制出来的不是完整 Key"
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test("D18-5: a Key row reveals in place, collapses on a second click, and its inline cap saves on change", async () => {
  const state = {
    keys: [
      {
        id: "k1",
        name: "alice",
        masked: "um-share-****",
        enabled: true,
        max_concurrency: 2,
        row_version: 1,
        plaintext_viewable: true,
      },
    ],
    secrets: 0,
    patches: [],
    failPatch: false,
  };
  const { browser, page, errors } = await mountKeysPage((route, req, path) => {
    if (path === "/api/console/share-management/keys/k1/secret") {
      state.secrets += 1;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ secret: "um-share-FULLKEY" }),
      });
    }
    if (
      path === "/api/console/share-management/keys/k1" &&
      req.method() === "PATCH"
    ) {
      const body = JSON.parse(req.postData() || "{}");
      state.patches.push(body);
      if (state.failPatch)
        return route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({
            error: "row_version_conflict",
            reason: "行版本已变化",
          }),
        });
      state.keys = [
        {
          ...state.keys[0],
          max_concurrency: body.max_concurrency,
          row_version: 2,
        },
      ];
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ key: state.keys[0] }),
      });
    }
    if (path === "/api/console/share-management/keys")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ keys: state.keys }),
      });
    return cloudBaseReads(route, path);
  });
  try {
    const row = page.locator('[data-cloud-key-row="k1"]');
    await row.waitFor({ timeout: 10000 });
    assert.match(await row.innerText(), /um-share-\*\*\*\*/);
    assert.doesNotMatch(await row.innerText(), /um-share-FULLKEY/);
    await row.locator('[data-cloud-key-action="reveal:k1"]').click();
    await page.waitForFunction(
      () =>
        document
          .querySelector('[data-cloud-key-row="k1"]')
          ?.textContent.includes("um-share-FULLKEY"),
      null,
      { timeout: 10000 }
    );
    assert.equal(state.secrets, 1);
    assert.equal(
      await row.locator('[data-cloud-key-action="copy:k1"]').count(),
      1,
      "明文出现后没有复制"
    );
    await row.locator('[data-cloud-key-action="copy:k1"]').click();
    await page.waitForTimeout(150);
    assert.equal(
      await page.evaluate(() => navigator.clipboard.readText()),
      "um-share-FULLKEY",
      "复制出来的不是完整 Key"
    );
    await page.waitForTimeout(1200);
    assert.match(await row.innerText(), /um-share-FULLKEY/, "明文被自动遮回");
    await row.locator('[data-cloud-key-action="reveal:k1"]').click();
    await page.waitForTimeout(150);
    assert.match(await row.innerText(), /um-share-\*\*\*\*/);
    assert.doesNotMatch(await row.innerText(), /um-share-FULLKEY/);
    assert.equal(
      await row.locator('[data-cloud-key-action="copy:k1"]').count(),
      0,
      "收起后仍有复制按钮"
    );
    // Reveal again before the inline concurrency edit: the PATCH bumps
    // row_version and must not auto-remask this plaintext.
    await row.locator('[data-cloud-key-action="reveal:k1"]').click();
    await page.waitForFunction(
      () =>
        document
          .querySelector('[data-cloud-key-row="k1"]')
          ?.textContent.includes("um-share-FULLKEY"),
      null,
      { timeout: 10000 }
    );
    const cap = row.locator('[data-cloud-key-cap-input="k1"]');
    await cap.fill("24");
    await cap.evaluate((element) => element.blur());
    await page.waitForFunction(
      () => document.body.innerText.includes("并发上限已保存为 24"),
      null,
      { timeout: 10000 }
    );
    assert.equal(state.patches.length, 1);
    assert.deepEqual(Object.keys(state.patches[0]).sort(), [
      "expected_row_version",
      "max_concurrency",
    ]);
    assert.equal(state.patches[0].max_concurrency, 24);
    assert.equal(state.patches[0].expected_row_version, 1);
    // The concurrency PATCH bumps row_version; the revealed plaintext must stay
    // revealed until the operator clicks the row again.
    await page.waitForTimeout(1200);
    assert.match(
      await row.innerText(),
      /um-share-FULLKEY/,
      "并发保存后明文被自动遮回"
    );
    await cap.fill("49");
    await cap.evaluate((element) => element.blur());
    await page.waitForTimeout(300);
    assert.equal(state.patches.length, 1, "非法并发被提交");
    assert.match(await row.innerText(), /1–48/);
    // A failed PATCH did not take effect: the optimistic value must be dropped
    // and the row shows the Key's actual concurrency again.
    state.failPatch = true;
    await cap.fill("30");
    await cap.evaluate((element) => element.blur());
    await page.waitForFunction(
      () => document.body.innerText.includes("版本冲突"),
      null,
      { timeout: 10000 }
    );
    assert.equal(state.patches.length, 2);
    assert.equal(
      await cap.inputValue(),
      "24",
      "失败后的并发输入仍显示未生效的值"
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test("rank drill-down stays on the single page and keeps the revealed Key plaintext", async () => {
  const state = {
    keys: [
      {
        id: "k1",
        name: "alice",
        masked: "um-share-****",
        enabled: true,
        max_concurrency: 2,
        row_version: 1,
        plaintext_viewable: true,
      },
    ],
    secrets: 0,
  };
  const { browser, page, errors } = await mountKeysPage((route, req, path) => {
    if (path === "/api/console/share-management/keys/k1/secret") {
      state.secrets += 1;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ secret: "um-share-FULLKEY" }),
      });
    }
    if (path === "/api/console/share-usage")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(chartUsagePayload()),
      });
    if (path === "/api/console/share-management/keys")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ keys: state.keys }),
      });
    return cloudBaseReads(route, path);
  });
  try {
    const row = page.locator('[data-cloud-key-row="k1"]');
    await row.waitFor({ timeout: 10000 });
    await row.locator('[data-cloud-key-action="reveal:k1"]').click();
    await page.waitForFunction(
      () =>
        document
          .querySelector('[data-cloud-key-row="k1"]')
          ?.textContent.includes("um-share-FULLKEY"),
      null,
      { timeout: 10000 }
    );
    const hashBefore = await page.evaluate(() => location.hash);
    await page.locator('[data-cloud-drill="key:k1"]').click();
    await page.waitForTimeout(200);
    const hashAfter = await page.evaluate(() => location.hash);
    assert.equal(hashAfter, hashBefore, "页内下钻写入了旧 hash");
    assert.doesNotMatch(hashAfter, /#usage/);
    assert.match(
      await row.innerText(),
      /um-share-FULLKEY/,
      "页内下钻后明文被清空"
    );
    assert.equal(
      await page.locator("dialog[open]").count(),
      0,
      "页内下钻后弹层被关闭"
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test("ticket #10: clicking 测试 posts one real inference with no model, shows 测试中… then 通过 · 耗时, and reverts", async () => {
  const state = { posts: [] };
  const { browser, page, errors } = await mountKeysPage(
    async (route, req, path) => {
      if (
        path === "/api/console/share-management/keys/k1/test" &&
        req.method() === "POST"
      ) {
        state.posts.push(JSON.parse(req.postData() || "{}"));
        await new Promise((resolve) => setTimeout(resolve, 800));
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            key_id: "k1",
            row_version: 1,
            connected: true,
            reason: "ok",
            stage: "inference",
            http_status: 200,
            model: "DeepSeek-V4.1-Flash-1017",
            elapsed_ms: 1234,
            public_ip: "1.2.3.4",
            tested_from: "public",
            checked_at: Math.floor(Date.now() / 1000),
          }),
        });
      }
      if (path === "/api/console/share-management/keys")
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            keys: [
              {
                id: "k1",
                name: "alice",
                masked: "um-share-****",
                enabled: true,
                max_concurrency: 2,
                row_version: 1,
                plaintext_viewable: true,
              },
            ],
          }),
        });
      return cloudBaseReads(route, path);
    }
  );
  try {
    const row = page.locator('[data-cloud-key-row="k1"]');
    await row.waitFor({ timeout: 10000 });
    const button = row.locator('[data-cloud-key-action="test:k1"]');
    assert.equal((await button.innerText()).trim(), "测试");
    await button.click();
    await page.waitForFunction(
      () =>
        document
          .querySelector(
            '[data-cloud-key-row="k1"] [data-cloud-key-action="test:k1"]'
          )
          ?.textContent.includes("测试中…"),
      null,
      { timeout: 10000 }
    );
    assert.equal(state.posts.length, 1);
    assert.deepEqual(
      Object.keys(state.posts[0]),
      ["expected_row_version"],
      "测试请求指定了模型或多余字段"
    );
    assert.equal(state.posts[0].expected_row_version, 1);
    assert.equal(
      await page.locator("dialog[open]").count(),
      0,
      "测试结果弹窗了"
    );
    await page.waitForFunction(
      () =>
        document
          .querySelector(
            '[data-cloud-key-row="k1"] [data-cloud-key-action="test:k1"]'
          )
          ?.textContent.includes("通过 · 1.2 s"),
      null,
      { timeout: 10000 }
    );
    assert.equal(new URL(page.url()).hash, "", "测试跳页了");
    await page.waitForFunction(
      () =>
        document
          .querySelector(
            '[data-cloud-key-row="k1"] [data-cloud-key-action="test:k1"]'
          )
          ?.textContent.trim() === "测试",
      null,
      { timeout: 12000 }
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test("ticket #10: a finished test re-reads usage and the request detail so the request shows up", async () => {
  const state = { tested: false, usageReads: 0, requestReads: 0 };
  const { browser, page, errors } = await mountKeysPage(
    async (route, req, path) => {
      if (
        path === "/api/console/share-management/keys/k1/test" &&
        req.method() === "POST"
      ) {
        state.tested = true;
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            key_id: "k1",
            row_version: 1,
            connected: true,
            reason: "ok",
            stage: "inference",
            http_status: 200,
            model: "DeepSeek-V4.1-Flash-1017",
            elapsed_ms: 1234,
            public_ip: "1.2.3.4",
            tested_from: "public",
            checked_at: Math.floor(Date.now() / 1000),
          }),
        });
      }
      if (path === "/api/console/share-usage") {
        state.usageReads += 1;
        if (new URL(req.url()).searchParams.get("result") === "failed")
          return route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(failedUsagePayload(0)),
          });
        const payload = chartUsagePayload();
        if (state.tested) {
          const day = payload.keys[0].days[0];
          payload.requests = 2;
          payload.keys[0].requests = 2;
          payload.keys[0].days = [
            {
              ...day,
              requests: 2,
              coverage: { ...day.coverage, requests: 2, records: 2 },
            },
          ];
        }
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(payload),
        });
      }
      if (path === "/api/console/share-usage/requests") {
        state.requestReads += 1;
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            rows: state.tested ? [requestRow()] : [],
            nextCursor: null,
            truncated: false,
            filters: null,
            coverage: {},
            checkedAt: null,
          }),
        });
      }
      if (path === "/api/console/share-management/keys")
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            keys: [
              {
                id: "k1",
                name: "alice",
                masked: "um-share-****",
                enabled: true,
                max_concurrency: 2,
                row_version: 1,
                plaintext_viewable: true,
              },
            ],
          }),
        });
      return cloudBaseReads(route, path);
    },
    { timezoneId: "Asia/Shanghai" }
  );
  try {
    await page.waitForSelector("[data-cloud-usage-figures]", {
      timeout: 10000,
    });
    await page.waitForFunction(
      () =>
        document
          .querySelector('[data-cloud-usage-value="requests"]')
          ?.textContent.trim() === "1",
      null,
      { timeout: 10000 }
    );
    assert.equal(
      await page.locator("[data-cloud-request-row]").count(),
      0,
      "初始请求明细不为空"
    );
    const beforeUsage = state.usageReads;
    const beforeRequests = state.requestReads;
    await page
      .locator(
        '[data-cloud-key-row="k1"] [data-cloud-key-action="test:k1"]'
      )
      .click();
    await page.waitForFunction(
      () =>
        document
          .querySelector('[data-cloud-usage-value="requests"]')
          ?.textContent.trim() === "2",
      null,
      { timeout: 15000 }
    );
    await page.waitForSelector('[data-cloud-request-row="r1"]', {
      timeout: 15000,
    });
    assert.ok(
      state.usageReads > beforeUsage,
      "测试结束后没有重读用量"
    );
    assert.ok(
      state.requestReads > beforeRequests,
      "测试结束后没有重读请求明细"
    );
    assert.match(
      await page.locator("[data-cloud-requests]").innerText(),
      /alice/
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test("D18-10: the three chain buttons and the advanced fold's server actions are gone", () => {
  const html = overview.overviewHtml(
    overviewModel({
      actionButton: (action, target, label, cls) =>
        `<button class="${cls}" data-cloud-op="${action}|${target}">${label}</button>`,
    })
  );
  for (const action of [
    "share.chain_start|share",
    "share.chain_stop|share",
    "share.chain_estop|share",
    "share.backend_login|share",
    "share.backend_restart|share",
    "service.start|share-gateway",
    "service.stop|share-gateway",
    "service.restart|share-gateway",
  ])
    assert.ok(
      !html.includes(`data-cloud-op="${action}"`),
      `页面仍有 ${action}`
    );
  assert.doesNotMatch(html, /cloud-chain-actions/);
  assert.doesNotMatch(html, /排空超时会明确显示为强制停止/);
  assert.doesNotMatch(html, /立即取消在途/);
  assert.doesNotMatch(html, /data-cloud-maintenance-actions/);
  assert.doesNotMatch(html, /维护动作/);
  const foldAt = html.indexOf("data-cloud-advanced");
  assert.ok(foldAt > 0, "单页没有高级维护折叠");
  const advanced = html.slice(foldAt);
  assert.match(advanced, /data-cloud-op="share\.status\|share"/);
  assert.match(
    advanced,
    /data-cloud-op="share\.backend_reauthenticate\|share"/
  );
  assert.match(advanced, /分层检查（只读）/);
  assert.match(advanced, /重新认证 UM/);
});

test("D18-7/D18-8/D18-9: the deleted services page no longer renders the display card", () => {
  const html = overview.overviewHtml(
    overviewModel({
      display: ready({
        available: true,
        url: "/display/",
        reason: null,
        expires_at: 1793420000,
        mode: "novnc",
        operator: {
          owned_by_me: true,
          owner_present: true,
          generation: 2,
          stale: false,
        },
      }),
      displayState: ready({
        available: true,
        state: "logging_in",
        label: "正在登录",
        detail: null,
        observed_at: null,
        login_window_deadline_epoch: 1793420000,
        operator: {
          owned_by_me: false,
          owner_present: true,
          generation: 3,
          stale: false,
        },
        display: { available: true, reason: null },
      }),
    })
  );
  for (const text of [
    "私有登录窗口",
    "cloud-display-segment",
    "本页持有操作权",
    "其他页面持有操作权",
    "data-cloud-display-open",
    "data-cloud-display-reconnect",
    "data-cloud-display-cancel",
    "data-cloud-display-takeover",
  ])
    assert.doesNotMatch(html, new RegExp(text), text);
});

test("D18-13: the chart offers 7/14/30 days, keeps the total in the tooltip and marks missing days", () => {
  const html = overview.overviewHtml(overviewModel());
  for (const range of ["7", "14", "30"]) {
    assert.match(
      html,
      new RegExp(`data-cloud-range="${range}"`),
      `缺少时段 ${range}`
    );
  }
  assert.match(html, /当天合计/);
  const mixed = overviewModel({
    usage: ready({
      keys: [
        {
          keyId: "k1",
          keyName: "alice",
          days: [dayUsage(beijingDay(Date.now()))],
          models: [],
          undated: null,
          coverage: dayUsage(beijingDay(Date.now())).coverage,
          costCny: 0.005,
          tokenUsage: dayUsage(beijingDay(Date.now())).tokenUsage,
        },
        {
          keyId: "k2",
          keyName: "bob",
          days: [],
          models: [],
          undated: null,
          coverage: { ...dayUsage("x").coverage, requests: 2 },
          costCny: null,
          tokenUsage: {
            totalTokens: null,
            lowerBoundTokens: null,
            complete: false,
            unknownRequests: null,
          },
        },
      ],
      unattributed: emptyBucket,
      coverage: { ledgerPresent: true, ledgerReadable: true },
      requests: 3,
      checkedAt: null,
      filters: null,
      results: null,
    }),
  });
  const missing = overview.overviewHtml(mixed);
  assert.match(missing, /cloud-axis-missing/, "缺失日没有短标");
  assert.match(missing, /无数据/);
});

test("D18-14: today's figures are 请求 / Token / 失败 in one block", () => {
  const html = overview.overviewHtml(
    overviewModel({ usageFailed: ready(failedUsage(3)) })
  );
  const start = html.indexOf("data-cloud-usage-figures");
  assert.ok(start >= 0, "今日用量没有独立的一块");
  const block = html.slice(start, html.indexOf("</section>", start));
  for (const [id, value] of [
    ["requests", "1"],
    ["tokens", "10"],
    ["failed", "3"],
  ])
    assert.match(
      block,
      new RegExp(`data-cloud-usage-value="${id}"[^>]*>${value}<`),
      `今日${id} 不是 ${value}`
    );
  // The cost stays stated, but outside the three-number row; the global cap
  // now lives in the Key list header.
  assert.match(block, /今日模型参考费用/);
  assert.match(html, /并发 未上报 \/ 8/);
  assert.doesNotMatch(block, /配置并发上限/);
});

test("D18-14: a failed or stale failure read is 未知, never a fabricated 0", () => {
  const failed = {
    state: "failed",
    kind: "error",
    reason: "读取超时",
    status: null,
    at: Date.now(),
    last: null,
  };
  const blockOf = (usageFailed) => {
    const html = overview.overviewHtml(overviewModel({ usageFailed }));
    const start = html.indexOf("data-cloud-usage-figures");
    assert.ok(start >= 0, "今日用量没有独立的一块");
    return html.slice(start, html.indexOf("</section>", start));
  };
  const failedBlock = blockOf(failed);
  assert.match(failedBlock, /data-cloud-usage-value="failed"[^>]*>未知</);
  assert.doesNotMatch(failedBlock, /data-cloud-usage-value="failed"[^>]*>0</);
  const staleBlock = blockOf({
    ...failed,
    last: { value: failedUsage(3), at: Date.now() - 60_000 },
  });
  assert.match(staleBlock, /data-cloud-usage-value="failed"[^>]*>未知</);
  assert.match(staleBlock, /失败读数已过时/);
});

test("D18-13: an all-zero window says so and the deleted API-address quick action is gone", () => {
  const zero = overviewModel({
    usage: ready({
      keys: [],
      unattributed: emptyBucket,
      coverage: { ledgerPresent: true, ledgerReadable: true },
      requests: 0,
      checkedAt: null,
      filters: null,
      results: null,
    }),
  });
  const zeroHtml = overview.overviewHtml(zero);
  assert.match(zeroHtml, /每天的请求都是 0/);
  assert.match(zeroHtml, /不是读取失败/);

  const staleStatus = {
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at: Date.now(),
    last: {
      at: Date.now() - 60_000,
      value: { share_url: "https://dacintapi.de5.net/v1" },
    },
  };
  const staleHtml = overview.overviewHtml(
    overviewModel({ status: staleStatus })
  );
  assert.doesNotMatch(staleHtml, /上次读数，已过时/);
  assert.doesNotMatch(staleHtml, /复制 API 地址/);
  assert.doesNotMatch(staleHtml, /API 地址/);
});

/** D18-13: one non-empty day so the final page draws a real bar. */
function chartUsagePayload() {
  const day = beijingDay(Date.now());
  const coverage = {
    requests: 1,
    unknownRequests: 0,
    unpricedRequests: 0,
    incompleteRequests: 0,
    records: 1,
    pricedRequests: 1,
    unknownTimeRequests: 0,
    pricingUnavailableRequests: 0,
    costComplete: true,
    costCnyPartial: false,
    knownCostCnyPartial: false,
    estimatedCostCnyPartial: false,
    cacheReportedRequests: 0,
    cacheUnreportedRequests: 0,
    cacheUnknownRequests: 0,
    cacheUnknownPricedRequests: 0,
    excludedUnknownTimeRequests: 0,
    localCoverageAvailable: true,
  };
  const tokenUsage = {
    totalTokens: 10,
    inputTokens: 6,
    outputTokens: 4,
    lowerBoundTokens: 10,
    lowerBoundInputTokens: 6,
    lowerBoundOutputTokens: 4,
    estimatedTokens: 0,
    estimatedInputTokens: 0,
    estimatedOutputTokens: 0,
    completeRequests: 1,
    lowerBoundRequests: 0,
    estimatedRequests: 0,
    unknownRequests: 0,
    complete: true,
    cachedInputTokens: 0,
    cacheReportedRequests: 0,
    cacheUnreportedRequests: 0,
    cacheUnknownRequests: 0,
  };
  const summary = {
    knownCostUsd: 0.001,
    estimatedCostUsd: 0,
    costCny: 0.005,
    knownCostCny: 0.005,
    estimatedCostCny: 0,
    inputTokens: 6,
    outputTokens: 4,
    requests: 1,
    unknownRequests: 0,
    unpricedRequests: 0,
    incompleteRequests: 0,
    unknownTimeRequests: 0,
    costCnyPartial: false,
    knownCostCnyPartial: false,
    estimatedCostCnyPartial: false,
    coverage,
    tokenUsage,
    priceVersions: [],
  };
  return {
    keys: [
      {
        key_id: "k1",
        keyName: "alice",
        deleted: false,
        ...summary,
        models: [],
        days: [{ day, ...summary }],
        undated: null,
      },
    ],
    unattributed: { ...summary, models: [], days: [], undated: null },
    coverage: { ledgerPresent: true, ledgerReadable: true },
    checkedAt: Math.floor(Date.now() / 1000),
    requests: 1,
    filters: null,
    results: null,
  };
}

/** Ticket #8: today's failed requests as the normalized aggregate the page
 *  reads (today's Beijing day bucket carries the failed count). */
function failedUsage(count = 3) {
  const day = dayUsage(beijingDay(Date.now()));
  const coverage = { ...day.coverage, requests: count, records: count };
  return {
    keys: [
      {
        keyId: "k1",
        keyName: "alice",
        days: [{ ...day, requests: count, coverage }],
        models: [],
        undated: null,
        requests: count,
        coverage,
        costCny: day.costCny,
        tokenUsage: day.tokenUsage,
      },
    ],
    unattributed: emptyBucket,
    coverage: { ledgerPresent: true, ledgerReadable: true },
    requests: count,
    checkedAt: null,
    filters: { window: "24h", key: null, model: null, result: "failed" },
    results: {
      success: 0,
      failed: count,
      cancelled: 0,
      unknown: 0,
      total: count,
      unknownResult: 0,
    },
  };
}

/** Ticket #8: today's failed requests as the backend's `result=failed`
 *  aggregate (window=24h); only today's Beijing day bucket is used. */
function failedUsagePayload(count = 3) {
  const payload = chartUsagePayload();
  const day = beijingDay(Date.now());
  const todayRow = {
    ...payload.keys[0].days[0],
    day,
    requests: count,
    coverage: {
      ...payload.keys[0].days[0].coverage,
      requests: count,
      records: count,
    },
  };
  return {
    ...payload,
    keys: [
      {
        ...payload.keys[0],
        days: [todayRow],
        requests: count,
        coverage: {
          ...payload.keys[0].coverage,
          requests: count,
          records: count,
        },
      },
    ],
    requests: count,
    filters: { window: "24h", key: null, model: null, result: "failed" },
    results: {
      success: 0,
      failed: count,
      cancelled: 0,
      unknown: 0,
      total: count,
      unknownResult: 0,
    },
  };
}

test("D18-13: the final Chrome page keeps focus, tooltip, redraw and theme behaviour", async () => {
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ channel: "chrome" });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
  });
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== ORIGIN) return route.abort();
    const path = url.pathname.replace(/^\/gw\/usagesrv/, "");
    if (path === "/")
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: HTML,
      });
    if (path === "/api/console/share-usage") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(chartUsagePayload()),
      });
    }
    if (path === "/api/console/share-management/keys") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ keys: [] }),
      });
    }
    if (path === "/api/console/share-management/status") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          account: { running: true, valid: true },
          last_check_at: Math.floor(Date.now() / 1000),
        }),
      });
    }
    if (path === "/api/console/operations") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ operations: [] }),
      });
    }
    if (path === "/api/console/actions") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          actions: [{ action: "share.status", enabled: true }],
        }),
      });
    }
    if (path === "/api/summary") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ role: "cloud" }),
      });
    }
    if (path === "/api/console/health") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          checked_at: Math.floor(Date.now() / 1000),
          services: [{ key: "share-gateway", state: "Running", healthy: true }],
        }),
      });
    }
    if (path === "/api/console/cloud-ops") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: "{}",
      });
    }
    if (path === "/api/console/share-management/display-session") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ available: false, reason: "测试" }),
      });
    }
    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: "{}",
    });
  });
  const page = await context.newPage();
  try {
    await page.goto(`${ORIGIN}/`, { waitUntil: "domcontentloaded" });
    await page.addStyleTag({
      content: readFileSync(join(WEB, "src/shell/tokens.css"), "utf8"),
    });
    await page.addStyleTag({
      content: readFileSync(join(WEB, "src/shell/shell.css"), "utf8"),
    });
    await page.addStyleTag({
      content: readFileSync(join(WEB, "src/cloud/cloud.css"), "utf8"),
    });
    await page.addScriptTag({ content: browserBundle() });
    await page.evaluate(() => {
      window.CloudConsole.mountCloudConsole(document.querySelector("#stage"), {
        verdict: () => ({ state: "confirmed", reason: "" }),
        onChange: () => {},
      });
    });
    await page.waitForSelector("svg.cloud-chart .cloud-bar-slot", {
      timeout: 10000,
    });

    const shotDir = process.env.D18_13_SHOTS;
    const shot = async (name) => {
      if (!shotDir) return;
      mkdirSync(shotDir, { recursive: true });
      await page.screenshot({
        path: join(shotDir, `${name}.png`),
        fullPage: true,
      });
    };

    // Mouse click focuses the slot but the container outline stays hidden.
    const slot = page.locator(".cloud-bar-slot").first();
    await slot.click();
    const mouseFocus = await page.evaluate(() => {
      const slot = document.querySelector(".cloud-bar-slot");
      const style = slot ? getComputedStyle(slot) : null;
      return {
        active: document.activeElement === slot,
        outlineStyle: style ? style.outlineStyle : "",
        outlineWidth: style ? style.outlineWidth : "",
      };
    });
    assert.equal(mouseFocus.active, true, "点击后焦点不在柱位");
    assert.ok(
      mouseFocus.outlineStyle === "none" || mouseFocus.outlineWidth === "0px",
      `鼠标点击不应显示焦点框: ${JSON.stringify(mouseFocus)}`
    );
    await shot("d18-13-mouse-focus-1440-light");

    // Tab from the preceding metric button reaches the slot and shows the 2px ring.
    await page.locator('[data-cloud-metric="tokens"]').focus();
    await page.keyboard.press("Tab");
    const keyboardFocus = await page.evaluate(() => {
      const slot = document.activeElement;
      const style = slot ? getComputedStyle(slot) : null;
      return {
        active: Boolean(
          slot && slot.getAttribute("class")?.includes("cloud-bar-slot")
        ),
        outlineStyle: style ? style.outlineStyle : "",
        outlineWidth: style ? style.outlineWidth : "",
      };
    });
    assert.equal(keyboardFocus.active, true, "Tab 后焦点不在柱位");
    assert.equal(keyboardFocus.outlineStyle, "solid", "Tab 焦点环样式不是实线");
    assert.equal(keyboardFocus.outlineWidth, "2px", "Tab 焦点环不是 2px");
    await shot("d18-13-tab-focus-1440-light");

    // Keyboard focus also opens the same tooltip, including 当天合计.
    const tip = page.locator(".cloud-tip");
    assert.equal(await tip.isVisible(), true, "键盘焦点没有显示 tooltip");
    assert.match(await tip.innerText(), /当天合计/, "tooltip 缺少当天合计");

    // Pointer hover shows the tooltip; leaving hides it.
    await page.mouse.move(8, 8);
    await page.waitForTimeout(80);
    assert.equal(await tip.isVisible(), false, "指针离开后 tooltip 仍显示");
    await slot.hover();
    await page.waitForTimeout(80);
    assert.equal(await tip.isVisible(), true, "指针悬停没有显示 tooltip");
    await shot("d18-13-hover-tooltip-1440-light");

    // 7/14/30 switching redraws the real bar series.
    await page.locator('[data-cloud-range="7"]').click();
    await page.waitForFunction(
      () => document.querySelectorAll(".cloud-bar-slot").length === 7,
      null,
      { timeout: 10000 }
    );
    await page.locator('[data-cloud-range="30"]').click();
    await page.waitForFunction(
      () => document.querySelectorAll(".cloud-bar-slot").length === 30,
      null,
      { timeout: 10000 }
    );

    const chartWidth = () =>
      page
        .locator("svg.cloud-chart")
        .evaluate((svg) => Number(svg.getAttribute("viewBox").split(" ")[2]));
    const wide = await chartWidth();
    await page.setViewportSize({ width: 1024, height: 900 });
    await page.waitForFunction(
      (previous) => {
        const svg = document.querySelector("svg.cloud-chart");
        return (
          Boolean(svg) &&
          Number(svg.getAttribute("viewBox").split(" ")[2]) !== previous
        );
      },
      wide,
      { timeout: 10000 }
    );
    const narrow = await chartWidth();
    assert.notEqual(
      narrow,
      wide,
      `不同桌面宽度下图表没有按容器重绘: ${narrow} == ${wide}`
    );
    const fits = await page.locator("svg.cloud-chart").evaluate((svg) => {
      const box = svg.parentElement.getBoundingClientRect();
      const width = Number(svg.getAttribute("viewBox").split(" ")[2]);
      return width === Math.max(320, Math.min(960, Math.round(box.width - 32)));
    });
    assert.equal(fits, true, "图表 viewBox 宽度没有按容器宽度计算");
    await shot("d18-13-narrow-1024-light");
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.waitForFunction(
      (previous) => {
        const svg = document.querySelector("svg.cloud-chart");
        return (
          Boolean(svg) &&
          Number(svg.getAttribute("viewBox").split(" ")[2]) === previous
        );
      },
      wide,
      { timeout: 10000 }
    );

    // The real theme writer flips the chart mark, not a test-only data-theme.
    const lightFill = await page
      .locator(".cloud-bar")
      .first()
      .evaluate((el) => getComputedStyle(el).fill);
    // Ticket #4 / user story 51: the whole page follows the dark theme, not just
    // the chart. paintTheme writes data-dark-surface, and that is what redefines
    // the shell tokens the page's own surface and text read.
    const lightSurface = await page.evaluate(() => {
      const root = document.documentElement;
      const style = getComputedStyle(root);
      return {
        background: style.backgroundColor,
        color: style.color,
        dark: root.dataset.darkSurface,
      };
    });
    await page.evaluate(() => window.CloudConsole.paintTheme(1));
    const darkFill = await page
      .locator(".cloud-bar")
      .first()
      .evaluate((el) => getComputedStyle(el).fill);
    const darkSurface = await page.evaluate(() => {
      const root = document.documentElement;
      const style = getComputedStyle(root);
      return {
        background: style.backgroundColor,
        color: style.color,
        dark: root.dataset.darkSurface,
      };
    });
    assert.notEqual(darkFill, lightFill, "深色主题没有改变柱色");
    assert.notEqual(
      darkSurface.background,
      lightSurface.background,
      "深色主题没有改变页面底色"
    );
    assert.notEqual(
      darkSurface.color,
      lightSurface.color,
      "深色主题没有改变页面文字色"
    );
    assert.equal(darkSurface.dark, "true", "深色主题没有标记 data-dark-surface");
    await shot("d18-13-dark-1440");
  } finally {
    await browser.close();
  }
});

test("D18-14: today's figures are mono, right-aligned to one line, and the trend defaults to 14 days with click/keyboard totals", async () => {
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ channel: "chrome" });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
  });
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== ORIGIN) return route.abort();
    const path = url.pathname.replace(/^\/gw\/usagesrv/, "");
    if (path === "/")
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: HTML,
      });
    if (path === "/api/console/share-usage") {
      const failed = url.searchParams.get("result") === "failed";
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          failed ? failedUsagePayload(3) : chartUsagePayload()
        ),
      });
    }
    if (path === "/api/console/share-management/keys") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ keys: [] }),
      });
    }
    if (path === "/api/console/share-management/status") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          account: { running: true, valid: true },
          last_check_at: Math.floor(Date.now() / 1000),
        }),
      });
    }
    if (path === "/api/console/operations") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ operations: [] }),
      });
    }
    if (path === "/api/console/actions") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          actions: [{ action: "share.status", enabled: true }],
        }),
      });
    }
    if (path === "/api/summary") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ role: "cloud" }),
      });
    }
    if (path === "/api/console/health") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          checked_at: Math.floor(Date.now() / 1000),
          services: [{ key: "share-gateway", state: "Running", healthy: true }],
        }),
      });
    }
    if (path === "/api/console/cloud-ops") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: "{}",
      });
    }
    if (path === "/api/console/share-management/display-session") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ available: false, reason: "测试" }),
      });
    }
    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: "{}",
    });
  });
  const page = await context.newPage();
  try {
    await page.goto(`${ORIGIN}/`, { waitUntil: "domcontentloaded" });
    for (const css of [
      "src/shell/tokens.css",
      "src/shell/shell.css",
      "src/cloud/cloud.css",
    ])
      await page.addStyleTag({
        content: readFileSync(join(WEB, css), "utf8"),
      });
    await page.addScriptTag({ content: browserBundle() });
    await page.evaluate(() => {
      window.CloudConsole.mountCloudConsole(document.querySelector("#stage"), {
        verdict: () => ({ state: "confirmed", reason: "" }),
        onChange: () => {},
      });
    });
    await page.waitForSelector("svg.cloud-chart .cloud-bar-slot", {
      timeout: 10000,
    });
    // All three usage reads must have landed before the figures are read.
    await page.waitForFunction(
      () => {
        const value = (id) =>
          document
            .querySelector(`[data-cloud-usage-value="${id}"]`)
            ?.textContent?.trim();
        return (
          value("requests") === "1" &&
          value("tokens") === "10" &&
          value("failed") === "3"
        );
      },
      null,
      { timeout: 10000 }
    );
    await page.evaluate(() => document.fonts.ready);
    // The subtitle says 数据更新于 only after every read of this refresh has
    // settled, so no later re-render can steal focus mid-click/mid-Tab.
    await page.waitForFunction(
      () =>
        document
          .querySelector("[data-cloud-subtitle]")
          ?.textContent?.includes("数据更新于"),
      null,
      { timeout: 10000 }
    );

    // The three today figures: 请求 / Token / 失败, mono, right-aligned to
    // the same vertical line. Read them in one synchronous page task after the
    // layout stops moving, so a chart resize cannot detach them mid-read.
    const figures = await page.evaluate(async () => {
      const read = () =>
        [...document.querySelectorAll("[data-cloud-usage-figure]")].map(
          (el) => {
            const value = el.querySelector("[data-cloud-usage-value]");
            const style = getComputedStyle(value);
            const box = value.getBoundingClientRect();
            return {
              id: value.getAttribute("data-cloud-usage-value"),
              text: value.textContent.trim(),
              fontFamily: style.fontFamily,
              textAlign: style.textAlign,
              right: box.right,
            };
          }
        );
      let previous = JSON.stringify(read());
      for (let index = 0; index < 30; index += 1) {
        await new Promise((resolve) => requestAnimationFrame(resolve));
        const next = JSON.stringify(read());
        if (next === previous && read().length === 3) return JSON.parse(next);
        previous = next;
      }
      return JSON.parse(previous);
    });
    assert.deepEqual(
      figures.map((figure) => figure.id),
      ["requests", "tokens", "failed"]
    );
    assert.deepEqual(
      figures.map((figure) => figure.text),
      ["1", "10", "3"]
    );
    for (const figure of figures) {
      assert.match(figure.fontFamily, /mono/i, `${figure.id} 不是 mono 字体`);
      assert.equal(figure.textAlign, "right", `${figure.id} 没有右对齐`);
    }
    assert.ok(
      figures.every((figure) => Math.abs(figure.right - figures[0].right) < 1),
      `三个数字没有对齐到同一条竖线: ${figures
        .map((figure) => `${figure.id}=${figure.right}`)
        .join(" ")}`
    );

    // The trend defaults to 14 days.
    assert.equal(
      await page
        .locator('[data-cloud-range="14"]')
        .getAttribute("aria-pressed"),
      "true",
      "趋势默认不是 14 天"
    );
    assert.equal(
      await page.locator(".cloud-bar-slot").count(),
      14,
      "默认 14 天没有画 14 个数据点"
    );

    // Clicking a day shows that day's total.
    const slots = page.locator(".cloud-bar-slot");
    const tip = page.locator(".cloud-tip");
    const tipText = async () => {
      await page.waitForFunction(
        () => {
          const tip = document.querySelector(".cloud-tip");
          return Boolean(tip) && !tip.hidden;
        },
        null,
        { timeout: 10000 }
      );
      return (await tip.innerText()).replace(/\s+/g, " ");
    };
    await slots.nth(13).click();
    const clickText = await tipText();
    assert.match(clickText, /当天合计/, "点击某天没有显示当天合计");

    // Tab to the same day shows the same total.
    await page.locator('[data-cloud-metric="tokens"]').focus();
    for (let index = 0; index < 14; index += 1)
      await page.keyboard.press("Tab");
    const focused = await page.evaluate(() => {
      const element = document.activeElement;
      return element?.classList.contains("cloud-bar-slot")
        ? element.getAttribute("data-tip")
        : null;
    });
    assert.ok(focused, "Tab 没有落到第 14 个数据点");
    assert.equal(await tipText(), clickText, "键盘与点击的合计不一致");

    // A day with no bucket is marked as missing instead of painted as 0.
    const missing = await page.evaluate(() => {
      const slot = document.querySelector(".cloud-bar-slot");
      return {
        bars: slot ? slot.querySelectorAll(".cloud-bar").length : -1,
        text: slot?.textContent ?? "",
        label: slot?.getAttribute("aria-label") ?? "",
      };
    });
    assert.equal(missing.bars, 0, "缺数据的那天画了柱");
    assert.match(missing.text, /无数据/, "缺数据的那天没有短标");
    assert.match(missing.label, /请求未知/, "缺数据的那天被当成 0 请求");
  } finally {
    await browser.close();
  }
});

const ready = (value) => ({ state: "ready", at: Date.now(), value });
const dayUsage = (day, cost = 0.005) => ({
  identity: day,
  coverage: {
    requests: 1,
    records: 1,
    unknownRequests: 0,
    unpricedRequests: 0,
    incompleteRequests: 1,
    unknownTimeRequests: 0,
    cacheUnreportedRequests: 0,
    costCnyPartial: true,
    costComplete: false,
  },
  tokenUsage: {
    totalTokens: 10,
    inputTokens: 6,
    outputTokens: 4,
    lowerBoundTokens: 10,
    complete: true,
    unknownRequests: 0,
  },
  costCny: cost,
  knownCostCny: cost,
  estimatedCostCny: 0,
});
const emptyBucket = {
  days: [],
  models: [],
  undated: null,
  coverage: {
    requests: 0,
    records: 0,
    unknownRequests: 0,
    unpricedRequests: 0,
    incompleteRequests: 0,
    unknownTimeRequests: 0,
    cacheUnreportedRequests: 0,
    costCnyPartial: false,
    costComplete: true,
  },
  costCny: 0,
  tokenUsage: {
    totalTokens: 0,
    inputTokens: 0,
    outputTokens: 0,
    lowerBoundTokens: 0,
    complete: true,
    unknownRequests: 0,
  },
};
const beijingDay = (ms) =>
  new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(ms);
const overviewModel = (overrides = {}) => {
  const day = dayUsage(beijingDay(Date.now()));
  return {
    summary: ready({ role: "cloud" }),
    health: ready({
      checked_at: Math.floor(Date.now() / 1000),
      services: [{ key: "share-gateway", state: "Running", healthy: true }],
    }),
    actions: ready([{ action: "share.status", enabled: true }]),
    operations: ready([]),
    status: ready({
      account: { running: true, valid: true },
      last_check_at: Math.floor(Date.now() / 1000),
      share_max_concurrency: 8,
    }),
    keys: ready([
      {
        id: "k1",
        name: "alice",
        masked: "um-share-****",
        enabled: true,
        max_concurrency: 2,
        row_version: 1,
        plaintext_viewable: true,
      },
    ]),
    usage: ready({
      keys: [
        {
          keyId: "k1",
          keyName: "alice",
          days: [day],
          models: [],
          undated: null,
          coverage: day.coverage,
          costCny: day.costCny,
          tokenUsage: day.tokenUsage,
        },
      ],
      unattributed: emptyBucket,
      coverage: { ledgerPresent: true, ledgerReadable: true },
      requests: 1,
      checkedAt: null,
      filters: null,
    }),
    usageRequests: null,
    usageRequestsUi: {
      result: "",
      time: "today",
      cursors: [null],
      page: 0,
      loading: false,
    },
    usageFailed: ready(failedUsage(3)),
    security: ready({
      available: true,
      readAt: Math.floor(Date.now() / 1000),
      security: {
        configured: true,
        available: true,
        status: null,
        reason: null,
        remote: null,
        generationFloor: 7,
        records: 12,
        pendingRemote: 0,
        exportedAt: "2026-11-02T10:00:00+08:00",
      },
      reasons: [],
      unresolved: null,
    }),
    display: ready({
      available: false,
      reason: "测试",
      operator: {
        owned_by_me: false,
        owner_present: false,
        generation: 0,
        stale: false,
      },
    }),
    displayState: ready({
      available: true,
      state: "not_started",
      label: "未开始登录",
      detail: null,
      observed_at: null,
      login_window_deadline_epoch: null,
      operator: {
        owned_by_me: false,
        owner_present: false,
        generation: 0,
        stale: false,
      },
      display: { available: true, reason: null },
    }),
    verdict: { state: "confirmed", reason: "" },
    overviewUi: { metric: "requests", rank: "key", range: "14", chartWidth: 0 },
    usageUi: {
      dimension: "key",
      key: "",
      model: "",
      result: "",
      time: { kind: "14", day: "" },
      page: 1,
      open: null,
    },
    opsUi: { filter: "all", open: null },
    statusOpen: false,
    keyUi: {
      open: null,
      tests: new Map(),
      secrets: new Map(),
      notices: new Map(),
      busy: new Set(),
      caps: new Map(),
    },
    feedback: [],
    actionButton: () => "<button>操作</button>",
    writeReady: () => ({ ok: true, reason: "" }),
    keyBusy: () => false,
    keyNotice: () => undefined,
    keySecret: () => undefined,
    keyTest: () => undefined,
    keyCapValue: () => undefined,
    latestKeyTest: () => null,
    ...overrides,
  };
};

test("single page: the old ops and services content is gone and only the two read-only actions remain", () => {
  const html = overview.overviewHtml(
    overviewModel({
      actionButton: (action, target, label) =>
        `<button type="button" data-cloud-op="${action}|${target}">${label}</button>`,
    })
  );
  for (const text of [
    "安全状态",
    "本页提交",
    "操作记录",
    "价目表版本",
    "API 入口",
    "UM 后端",
    "上游认证",
    "登录动作",
    "最近核验",
    "一键启动",
    "普通停止",
    "强制停止",
  ])
    assert.doesNotMatch(html, new RegExp(text), text);
  assert.match(html, /高级维护/);
  const fold = html.slice(html.indexOf("data-cloud-advanced"));
  assert.match(fold, /分层检查（只读）/);
  assert.match(fold, /重新认证 UM/);
  const ops = [...fold.matchAll(/data-cloud-op="([^"]+)"/g)].map(
    (match) => match[1]
  );
  assert.deepEqual(ops, [
    "share.status|share",
    "share.backend_reauthenticate|share",
  ]);
  assert.doesNotMatch(fold, /data-cloud-maintenance-actions/);
  assert.doesNotMatch(fold, /维护动作/);
});

test("single page: only 总览 is in the nav, old hashes fall back and the drawer still opens", async () => {
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ channel: "chrome" });
  const context = await browser.newContext({
    viewport: { width: 390, height: 800 },
    serviceWorkers: "block",
    locale: "zh-CN",
  });
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== ORIGIN) return route.abort();
    const path = url.pathname.replace(/^\/gw\/usagesrv/, "");
    if (path === "/")
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: HTML,
      });
    if (path === "/api/console/share-usage") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(chartUsagePayload()),
      });
    }
    if (path === "/api/console/share-management/keys") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ keys: [] }),
      });
    }
    if (path === "/api/console/share-management/status") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          account: { running: true, valid: true },
          last_check_at: Math.floor(Date.now() / 1000),
        }),
      });
    }
    if (path === "/api/console/operations") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ operations: [] }),
      });
    }
    if (path === "/api/console/actions") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          actions: [{ action: "share.status", enabled: true }],
        }),
      });
    }
    if (path === "/api/summary") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ role: "cloud" }),
      });
    }
    if (path === "/api/console/health") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          checked_at: Math.floor(Date.now() / 1000),
          services: [{ key: "share-gateway", state: "Running", healthy: true }],
        }),
      });
    }
    if (path === "/api/console/cloud-ops") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: "{}",
      });
    }
    if (path === "/api/console/share-management/display-session") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ available: false, reason: "测试" }),
      });
    }
    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: "{}",
    });
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error.message)));
  try {
    await page.goto(`${ORIGIN}/`, { waitUntil: "domcontentloaded" });
    await page.addStyleTag({
      content: readFileSync(join(WEB, "src/shell/tokens.css"), "utf8"),
    });
    await page.addStyleTag({
      content: readFileSync(join(WEB, "src/shell/shell.css"), "utf8"),
    });
    await page.addStyleTag({
      content: readFileSync(join(WEB, "src/cloud/cloud.css"), "utf8"),
    });
    await page.addScriptTag({ content: browserBundle() });
    await page.evaluate(() => {
      window.CloudConsole.mountCloudConsole(document.querySelector("#stage"), {
        verdict: () => ({ state: "confirmed", reason: "" }),
        onChange: () => {},
      });
    });
    await page.waitForSelector("[data-cloud-area]", {
      state: "attached",
      timeout: 10000,
    });
    assert.equal(await page.locator("[data-cloud-area]").count(), 1);
    assert.equal(
      await page.locator("[data-cloud-area]").getAttribute("aria-label"),
      "总览"
    );
    for (const hash of ["keys", "usage", "services", "ops"]) {
      await page.evaluate((value) => {
        location.hash = value;
      }, hash);
      await page.waitForTimeout(80);
      assert.equal(
        await page
          .locator('[data-cloud-area="overview"]')
          .getAttribute("aria-current"),
        "page",
        `${hash} 没有落到总览`
      );
      const text = (await page.locator("body").innerText()).replace(
        /\s+/g,
        " "
      );
      assert.doesNotMatch(text, /该页已移除/);
      assert.match(text, /今日请求/, `${hash} 回落总览后没有总览内容`);
    }
    await page.evaluate(() => {
      location.hash = "overview";
    });
    await page.waitForTimeout(80);
    // Ticket #4 / user story 50: at 390 the page is one column and nothing
    // overflows the window. The trend and rank pair is the page's only
    // side-by-side row; it stacks, and the document never scrolls sideways.
    const narrowLayout = await page.evaluate(() => {
      const columns = (selector) => {
        const element = document.querySelector(selector);
        if (!element) return null;
        return getComputedStyle(element).gridTemplateColumns
          .split(" ")
          .filter(Boolean).length;
      };
      const root = document.documentElement;
      return {
        trend: columns(".cloud-trend-grid"),
        overflow: root.scrollWidth - root.clientWidth,
      };
    });
    assert.equal(narrowLayout.trend, 1, "窄窗下趋势与排行不是单列");
    assert.ok(
      narrowLayout.overflow <= 0,
      `窄窗下页面横向溢出 ${narrowLayout.overflow}px`
    );
    await page.locator("[data-cloud-drawer-toggle]").click();
    assert.equal(
      await page.locator("#cloud-nav").getAttribute("aria-hidden"),
      "false"
    );
    assert.equal(
      await page
        .locator("[data-cloud-drawer-toggle]")
        .getAttribute("aria-expanded"),
      "true"
    );
    assert.equal(
      await page
        .locator("[data-cloud-drawer-close]")
        .evaluate((element) => element.hidden),
      false
    );
    await page.mouse.click(380, 400);
    assert.equal(
      await page.locator("#cloud-nav").getAttribute("aria-hidden"),
      "true"
    );
    assert.equal(
      await page
        .locator("[data-cloud-drawer-toggle]")
        .getAttribute("aria-expanded"),
      "false"
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test("U: the single page keeps today's figures, cap, trend and the Key list", () => {
  const html = overview.overviewHtml(
    overviewModel({
      actionButton: (action, target, label) =>
        `<button type="button" data-cloud-op="${action}|${target}">${label}</button>`,
    })
  );
  assert.match(html, /今日请求/);
  assert.match(html, /今日模型参考费用/);
  assert.match(html, /并发 未上报 \/ 8/);
  assert.match(html, /cloud-trend-grid/);
  assert.match(html, /API Key/);
  assert.doesNotMatch(html, /cloud-hero/);
  assert.doesNotMatch(html, /需要处理的问题/);
  assert.doesNotMatch(html, /cloud-status-grid/);
  const fold = html.indexOf("data-cloud-advanced");
  assert.ok(fold > 0, "单页没有高级维护折叠");
  assert.match(html.slice(fold), /分层检查（只读）/);
  assert.match(html.slice(fold), /重新认证 UM/);
});

const requestRow = (overrides = {}) => ({
  requestId: "r1",
  time: "2026-11-11T10:00:00+08:00",
  day: "2026-11-11",
  keyId: "k1",
  keyName: "alice",
  deleted: false,
  model: "gpt-5",
  status: "200",
  result: "success",
  attribution: null,
  usageSource: "upstream",
  cacheReportStatus: "reported",
  inputTokens: 6,
  outputTokens: 4,
  durationMs: 1200,
  cacheTokens: 0,
  reasoningTokens: null,
  priced: true,
  priceVersion: "v1",
  costUsd: 0.001,
  costCny: 0.005,
  costCnyPartial: false,
  ...overrides,
});
const requestsPage = (rows, nextCursor = null) => ({
  rows,
  nextCursor,
  truncated: false,
  filters: null,
  coverage: {},
  checkedAt: null,
});
const requestsCardOf = (html) => {
  const start = html.indexOf("data-cloud-requests");
  assert.ok(start >= 0, "单页没有请求明细");
  return html.slice(start, html.indexOf("</section>", start));
};
const requestRowOf = (card, id) => {
  const start = card.indexOf(`data-cloud-request-row="${id}"`);
  assert.ok(start >= 0, `请求明细没有行 ${id}`);
  return card.slice(start, card.indexOf("</tr>", start));
};

test("requests: result/time filters, page-local counts and the deleted Key suffix", () => {
  const card = requestsCardOf(
    overview.overviewHtml(
      overviewModel({
        usageRequests: ready(
          requestsPage(
            [
              requestRow(),
              requestRow({
                requestId: "r2",
                keyId: "k2",
                keyName: "bob",
                deleted: true,
                model: "m2",
                status: "500",
                result: "failed",
                inputTokens: null,
                outputTokens: null,
                durationMs: null,
              }),
            ],
            "42"
          )
        ),
        usageRequestsUi: {
          result: "",
          time: "today",
          cursors: [null],
          page: 0,
          loading: false,
        },
      })
    )
  );
  for (const [id, word] of [
    ["all", "全部"],
    ["success", "成功"],
    ["failed", "失败"],
    ["unknown", "结果未确认"],
    ["cancelled", "已取消"],
  ])
    assert.match(
      card,
      new RegExp(`data-cloud-requests-result="${id}"[^>]*>${word}<`),
      `结果筛选缺少 ${word}`
    );
  for (const [id, word] of [
    ["today", "今天"],
    ["7", "近 7 天"],
    ["30", "近 30 天"],
  ])
    assert.match(
      card,
      new RegExp(`data-cloud-requests-window="${id}"[^>]*>${word}<`),
      `时间范围缺少 ${word}`
    );
  assert.doesNotMatch(card, /data-cloud-requests-window="14"/);
  assert.match(card, /<th>时间<\/th>/);
  assert.match(card, /<th>Key<\/th>/);
  assert.match(card, /<th>模型<\/th>/);
  assert.match(card, /<th>结果<\/th>/);
  assert.match(card, /<th>耗时<\/th>/);
  assert.match(card, /<th>Token<\/th>/);
  const first = requestRowOf(card, "r1");
  assert.match(first, /2026-11-11T10:00:00\+08:00/);
  assert.match(first, /alice/);
  assert.doesNotMatch(first, /alice（已删除）/);
  assert.match(first, /gpt-5/);
  assert.match(first, /成功/);
  assert.match(first, /1,200 ms/);
  assert.match(first, /Token 10/);
  const second = requestRowOf(card, "r2");
  assert.match(second, /bob（已删除）/, "已删除的 Key 没有带后缀");
  assert.match(second, /失败/);
  assert.match(second, /Token 未知/);
  assert.match(second, /未上报/, "旧记录缺 duration_ms 时没有如实标注未上报");
  assert.match(card, /本页 2 条/);
  assert.match(
    card,
    /本页：成功 1 · 失败 1 · 结果未确认 0 · 已取消 0/,
    "结果计数没有按已加载页统计"
  );
  assert.match(card, /只统计已加载页/);
  assert.doesNotMatch(card, /共 \d+/);
  assert.match(card, /data-cloud-requests-page="-1"[^>]*disabled/);
  assert.match(card, /data-cloud-requests-page="1"[^>]*>下一页</);
  assert.doesNotMatch(card, /data-cloud-requests-page="1"[^>]*disabled/);

  const secondPage = requestsCardOf(
    overview.overviewHtml(
      overviewModel({
        usageRequests: ready(
          requestsPage(
            [requestRow({ requestId: "r3", result: "cancelled" })],
            null
          )
        ),
        usageRequestsUi: {
          result: "cancelled",
          time: "7",
          cursors: [null, "42"],
          page: 1,
          loading: false,
        },
      })
    )
  );
  assert.match(secondPage, /第 2 页/);
  assert.match(secondPage, /本页 1 条/);
  assert.match(secondPage, /本页：成功 0 · 失败 0 · 结果未确认 0 · 已取消 1/);
  assert.match(secondPage, /data-cloud-requests-page="-1"[^>]*>上一页</);
  assert.doesNotMatch(secondPage, /data-cloud-requests-page="-1"[^>]*disabled/);
  assert.match(secondPage, /data-cloud-requests-page="1"[^>]*disabled/);
  assert.match(secondPage, /已取消/);
  assert.doesNotMatch(secondPage, /共 \d+/);
});

test("requests: result/time filters and cursor pages use the backend read face", async () => {
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ channel: "chrome" });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    serviceWorkers: "block",
    locale: "zh-CN",
  });
  const state = { calls: [] };
  const allRows = [
    requestRow(),
    requestRow({
      requestId: "r2",
      keyId: "k2",
      keyName: "bob",
      deleted: true,
      model: "m2",
      status: "500",
      result: "failed",
      inputTokens: null,
      outputTokens: null,
    }),
  ];
  const pageFor = (rows, nextCursor) => ({
    rows,
    nextCursor,
    truncated: false,
    filters: null,
    coverage: {},
    checkedAt: null,
  });
  await context.route("**/*", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== ORIGIN) return route.abort();
    const path = url.pathname.replace(/^\/gw\/usagesrv/, "");
    if (path === "/")
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: HTML,
      });
    if (path === "/api/console/share-usage/requests") {
      const query = Object.fromEntries(url.searchParams);
      state.calls.push(query);
      if (query.cursor === "42")
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(
            pageFor(
              [
                requestRow({
                  requestId: "r3",
                  result: "cancelled",
                  status: null,
                }),
              ],
              null
            )
          ),
        });
      const rows = query.result
        ? allRows.filter((row) => row.result === query.result)
        : allRows;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(pageFor(rows, "42")),
      });
    }
    return cloudBaseReads(route, path);
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error.message)));
  const cardText = async () =>
    (await page.locator("[data-cloud-requests]").innerText()).replace(
      /\s+/g,
      " "
    );
  try {
    await page.goto(`${ORIGIN}/`, { waitUntil: "domcontentloaded" });
    await page.addScriptTag({ content: browserBundle() });
    await page.evaluate(() => {
      window.CloudConsole.mountCloudConsole(document.querySelector("#stage"), {
        verdict: () => ({ state: "confirmed", reason: "" }),
        onChange: () => {},
      });
    });
    await page.waitForSelector('[data-cloud-request-row="r1"]', {
      timeout: 10000,
    });
    assert.equal(state.calls.length, 1, "挂载后没有自动读取第一页");
    assert.equal(state.calls[0].window, "today", "今天没有映射到北京时间自然日");
    assert.equal(state.calls[0].limit, "50", "第一页没有按 50 条分页");
    assert.equal(state.calls[0].result, undefined, "全部结果不应发送 result");
    assert.equal(state.calls[0].cursor, undefined, "第一页不应发送 cursor");
    assert.match(await cardText(), /bob（已删除）/);
    assert.match(await cardText(), /本页 2 条/);
    assert.match(
      await cardText(),
      /本页：成功 1 · 失败 1 · 结果未确认 0 · 已取消 0/
    );

    await page.locator('[data-cloud-requests-result="failed"]').click();
    await page.waitForFunction(
      () => document.querySelectorAll("[data-cloud-request-row]").length === 1
    );
    assert.equal(
      state.calls.at(-1).result,
      "failed",
      "失败筛选没有发送 result"
    );
    assert.equal(state.calls.at(-1).window, "today");
    assert.equal(state.calls.at(-1).cursor, undefined);
    assert.doesNotMatch(await cardText(), /alice/);
    assert.match(await cardText(), /bob（已删除）/);

    await page.locator('[data-cloud-requests-window="7"]').click();
    await page.waitForFunction(
      () =>
        document
          .querySelector('[data-cloud-requests-window="7"]')
          ?.getAttribute("aria-pressed") === "true"
    );
    await page.waitForTimeout(200);
    assert.equal(state.calls.at(-1).window, "7d", "近 7 天没有映射到 7d");
    assert.equal(state.calls.at(-1).result, "failed");

    await page.locator('[data-cloud-requests-window="30"]').click();
    await page.waitForFunction(
      () =>
        document
          .querySelector('[data-cloud-requests-window="30"]')
          ?.getAttribute("aria-pressed") === "true"
    );
    await page.waitForTimeout(200);
    assert.equal(state.calls.at(-1).window, "30d", "近 30 天没有映射到 30d");

    await page.locator('[data-cloud-requests-result="all"]').click();
    await page.waitForFunction(
      () => document.querySelectorAll("[data-cloud-request-row]").length === 2
    );
    assert.equal(state.calls.at(-1).result, undefined, "全部仍发送了 result");
    assert.equal(state.calls.at(-1).window, "30d");

    await page.locator('[data-cloud-requests-page="1"]').click();
    await page.waitForSelector('[data-cloud-request-row="r3"]', {
      timeout: 10000,
    });
    assert.equal(
      state.calls.at(-1).cursor,
      "42",
      "下一页没有带上一页的 nextCursor"
    );
    assert.equal(state.calls.at(-1).window, "30d");
    assert.match(await cardText(), /第 2 页/);
    assert.match(await cardText(), /本页 1 条/);
    assert.match(
      await cardText(),
      /本页：成功 0 · 失败 0 · 结果未确认 0 · 已取消 1/
    );
    assert.match(await cardText(), /已取消/);

    await page.locator('[data-cloud-requests-page="-1"]').click();
    await page.waitForSelector('[data-cloud-request-row="r1"]', {
      timeout: 10000,
    });
    assert.equal(state.calls.at(-1).cursor, undefined, "上一页没有回到第一页");
    assert.match(await cardText(), /第 1 页/);
    assert.match(await cardText(), /本页 2 条/);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test("status row: seven display states render their own words and unknown stays separate", () => {
  const status = ready({
    gateway_running: true,
    in_sync: true,
    account: { running: true, valid: true },
    last_check_at: Math.floor(Date.now() / 1000),
  });
  const rowOf = (state) => {
    const displayState = ready({
      available: true,
      state,
      label: `后端标签 ${state}`,
      detail: null,
      observed_at: null,
      login_window_deadline_epoch: null,
      display: { available: true, reason: null },
    });
    const html = overview.overviewHtml(overviewModel({ displayState, status }));
    const start = html.indexOf("data-cloud-status-toggle");
    assert.ok(start >= 0, "单页没有状态行");
    return html.slice(start, html.indexOf("</section>", start));
  };
  const cases = [
    ["not_started", "未开始"],
    ["logging_in", "登录中"],
    ["waiting_mfa", "等待 MFA"],
    ["valid", "已登录"],
    ["cancelled", "认证已失效"],
    ["failed", "认证已失效"],
    ["timed_out", "超时"],
    ["display_disconnected", "显示断线"],
  ];
  for (const [state, word] of cases) {
    const row = rowOf(state);
    assert.match(
      row,
      new RegExp(`data-cloud-status-toggle[^>]*>[^<]*${word}`),
      `${state} 没有渲染「${word}」`
    );
  }
  const unknown = rowOf("not_a_display_state");
  assert.match(unknown, /data-cloud-status-toggle[^>]*>[^<]*状态未知/);
});

test("status row: the login-window actions follow the display state", () => {
  const status = ready({
    gateway_running: true,
    in_sync: true,
    account: { running: true, valid: true },
    last_check_at: Math.floor(Date.now() / 1000),
  });
  const actions = (state) => {
    const displayState = ready({
      available: true,
      state,
      label: `后端标签 ${state}`,
      detail: null,
      observed_at: null,
      login_window_deadline_epoch: null,
      display: { available: true, reason: null },
    });
    const html = overview.overviewHtml(
      overviewModel({
        displayState,
        status,
        actionButton: (action, target, label) =>
          `<button type="button" data-cloud-op="${action}|${target}">${label}</button>`,
      })
    );
    const start = html.indexOf("data-cloud-status-toggle");
    const row = html.slice(start, html.indexOf("</section>", start));
    return {
      open: /data-cloud-login-window-open/.test(row),
      relogin: /data-cloud-op="share\.backend_login\|share"/.test(row),
      cancel: /data-cloud-login-cancel/.test(row),
      openWord: /打开登录窗口/.test(row),
      reloginWord: /重新登录/.test(row),
      cancelWord: /取消登录/.test(row),
    };
  };
  const onlyOpen = {
    open: true,
    relogin: false,
    cancel: false,
    openWord: true,
    reloginWord: false,
    cancelWord: false,
  };
  const openAndCancel = {
    open: true,
    relogin: false,
    cancel: true,
    openWord: true,
    reloginWord: false,
    cancelWord: true,
  };
  const reloginAndCancel = {
    open: false,
    relogin: true,
    cancel: true,
    openWord: false,
    reloginWord: true,
    cancelWord: true,
  };
  assert.deepEqual(actions("valid"), onlyOpen);
  assert.deepEqual(actions("not_started"), onlyOpen);
  for (const state of ["logging_in", "waiting_mfa"]) {
    assert.deepEqual(actions(state), openAndCancel);
  }
  for (const state of [
    "cancelled",
    "failed",
    "timed_out",
    "display_disconnected",
  ]) {
    assert.deepEqual(actions(state), reloginAndCancel);
  }
});

test("status row: gateway_running and in_sync decide service availability, unreadable is unknown", () => {
  const displayState = ready({
    available: true,
    state: "valid",
    label: "登录有效",
    detail: null,
    observed_at: null,
    login_window_deadline_epoch: null,
    display: { available: true, reason: null },
  });
  const rowOf = (status) => {
    const html = overview.overviewHtml(overviewModel({ displayState, status }));
    const start = html.indexOf("data-cloud-status-toggle");
    return html.slice(start, html.indexOf("</section>", start));
  };
  const available = rowOf(ready({ gateway_running: true, in_sync: true }));
  assert.match(available, /服务可用/);
  assert.match(available, /已登录/);

  const gatewayDown = rowOf(ready({ gateway_running: false, in_sync: true }));
  assert.match(gatewayDown, /服务异常/);
  assert.doesNotMatch(gatewayDown, /服务可用/);

  const outOfSync = rowOf(ready({ gateway_running: true, in_sync: false }));
  assert.match(outOfSync, /服务异常/);
  assert.doesNotMatch(outOfSync, /服务可用/);

  const unreported = rowOf(ready({}));
  assert.match(unreported, /服务状态未知/);
  assert.doesNotMatch(unreported, /服务可用/);

  const unreadable = rowOf({
    state: "failed",
    kind: "no-answer",
    reason: "读取超时",
    status: null,
    at: Date.now(),
    last: null,
  });
  assert.match(unreadable, /状态未知/);
  assert.match(unreadable, /服务状态未知/);
  assert.doesNotMatch(unreadable, /已登录/);
  assert.doesNotMatch(unreadable, /服务可用/);
});

test("status row: it shows when the data was updated and offers a refresh", () => {
  const html = overview.overviewHtml(overviewModel());
  const start = html.indexOf("data-cloud-status-toggle");
  assert.ok(start >= 0, "单页没有状态行");
  const row = html.slice(start, html.indexOf("</section>", start));
  assert.match(row, /数据更新于 \d{2}:\d{2}/);
  assert.match(row, /data-cloud-refresh[^>]*>[^<]*刷新/);
});

test("primary action: one button follows gateway_running and unknown is disabled", () => {
  const status = (running) =>
    ready({
      gateway_running: running,
      in_sync: true,
      account: { running: true, valid: true },
      last_check_at: Math.floor(Date.now() / 1000),
    });
  const actionOf = (statusRead) => {
    const html = overview.overviewHtml(overviewModel({ status: statusRead }));
    const start = html.indexOf("data-cloud-primary");
    assert.ok(start >= 0, "单页没有主动作按钮");
    return html.slice(start, html.indexOf("</section>", start));
  };
  const stopped = actionOf(status(false));
  assert.match(stopped, /data-cloud-primary="start"/);
  assert.match(stopped, /[^>]*>启动</);
  assert.doesNotMatch(stopped, /停止/);
  const running = actionOf(status(true));
  assert.match(running, /data-cloud-primary="stop"/);
  assert.match(running, /[^>]*>停止</);
  assert.doesNotMatch(running, /启动/);
  const unknown = actionOf(ready({ in_sync: true }));
  assert.match(unknown, /data-cloud-primary="start"/);
  assert.match(unknown, /disabled/);
  assert.match(unknown, /服务状态未知/);
  assert.equal(
    [...overview.overviewHtml(overviewModel()).matchAll(/data-cloud-primary=/g)]
      .length,
    1,
    "单页主动作按钮不止一个"
  );
});

test("primary action: an in-flight chain start shows 启动中…（等待认证） with its own step note", () => {
  const chainStart = (state, steps) => ({
    id: "op-start",
    action: "share.chain_start",
    target: "share",
    state,
    created_at: Math.floor(Date.now() / 1000),
    steps,
  });
  const status = ready({
    gateway_running: false,
    in_sync: true,
    account: { running: true, valid: true },
    last_check_at: Math.floor(Date.now() / 1000),
  });
  const actionOf = (model) => {
    const html = overview.overviewHtml(model);
    const start = html.indexOf("data-cloud-primary");
    assert.ok(start >= 0, "单页没有主动作按钮");
    return html.slice(start, html.indexOf("</section>", start));
  };
  const starting = actionOf(
    overviewModel({
      status,
      operations: ready([
        chainStart("running", [
          {
            at: 1,
            note: "整链启动：Key 前置 → 8110 → 认证等待 → 8100 → 公网边缘（只读）",
          },
          { at: 2, note: "专用后端 8110 启动已受理，等待运行确认" },
        ]),
      ]),
    })
  );
  assert.match(starting, /启动中…（等待认证）/);
  assert.match(starting, /专用后端 8110 启动已受理，等待运行确认/);
  assert.match(starting, /disabled/);
  assert.doesNotMatch(starting, /data-cloud-primary="stop"/);
  assert.doesNotMatch(starting, /认证有效|已登录/);
  // The page's own submit is in flight before the operations read catches up.
  const submitted = actionOf(
    overviewModel({
      status,
      primaryPending: { at: Date.now(), stage: "已提交，等待后端受理" },
    })
  );
  assert.match(submitted, /启动中…（等待认证）/);
  assert.match(submitted, /已提交，等待后端受理/);
  // A stale operations read is history, not current in-flight evidence.
  const stale = actionOf(
    overviewModel({
      status,
      operations: {
        state: "failed",
        kind: "no-answer",
        reason: "读取超时",
        status: null,
        at: Date.now(),
        last: {
          value: [
            chainStart("running", [
              { at: 2, note: "专用后端 8110 启动已受理，等待运行确认" },
            ]),
          ],
          at: Date.now() - 60_000,
        },
      },
    })
  );
  assert.match(stale, /data-cloud-primary="start"/);
  assert.doesNotMatch(stale, /启动中/);
  // A terminal start alone is not 停止 until the service read says running.
  const completed = actionOf(
    overviewModel({
      status,
      operations: ready([chainStart("succeeded", [{ at: 2, note: "完成" }])]),
    })
  );
  assert.match(completed, /data-cloud-primary="start"/);
  assert.doesNotMatch(completed, /启动中/);
  const running = actionOf(
    overviewModel({
      status: ready({ gateway_running: true, in_sync: true }),
      operations: ready([chainStart("succeeded", [{ at: 2, note: "完成" }])]),
    })
  );
  assert.match(running, /data-cloud-primary="stop"/);
});

test("primary action: clicking 启动 turns the same button into 启动中…（等待认证） and only completion shows 停止", async () => {
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ channel: "chrome" });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    serviceWorkers: "block",
    locale: "zh-CN",
  });
  const state = { running: false, operations: [], posts: [] };
  const stepNote = "专用后端 8110 启动已受理，等待运行确认";
  await context.route("**/*", (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin !== ORIGIN) return route.abort();
    if (req.method() !== "GET" && req.method() !== "HEAD") {
      const body = JSON.parse(req.postData() || "{}");
      state.posts.push(body);
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          operation: {
            id: "op-start",
            action: body.action,
            target: body.target,
            state: "running",
            created_at: Math.floor(Date.now() / 1000),
            steps: [{ at: Math.floor(Date.now() / 1000), note: stepNote }],
          },
        }),
      });
    }
    if (url.pathname === "/")
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: HTML,
      });
    const path = url.pathname.replace(/^\/gw\/usagesrv/, "");
    if (path === "/api/console/operations/op-start")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          operation: {
            id: "op-start",
            action: "share.chain_start",
            target: "share",
            state: "succeeded",
            created_at: Math.floor(Date.now() / 1000),
            finished_at: Math.floor(Date.now() / 1000),
            result: { state: "running" },
            steps: [{ at: Math.floor(Date.now() / 1000), note: stepNote }],
          },
        }),
      });
    if (path === "/api/console/operations")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ operations: state.operations }),
      });
    if (path === "/api/console/actions")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          actions: [
            { action: "share.chain_start", enabled: true },
            { action: "share.chain_stop", enabled: true },
            { action: "share.chain_estop", enabled: true },
          ],
        }),
      });
    if (path === "/api/summary")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ role: "cloud" }),
      });
    if (path === "/api/console/health")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          checked_at: Math.floor(Date.now() / 1000),
          services: [{ key: "share-gateway", state: "Running", healthy: true }],
        }),
      });
    if (path === "/api/console/share-management/status")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          gateway_running: state.running,
          in_sync: true,
          account: { running: true, valid: true },
          last_check_at: Math.floor(Date.now() / 1000),
        }),
      });
    if (path === "/api/console/share-management/display-state")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          available: true,
          state: "not_started",
          label: "未开始登录",
          detail: null,
          observed_at: null,
          login_window_deadline_epoch: null,
          display: { available: true, reason: null },
        }),
      });
    if (path === "/api/console/share-management/keys")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ keys: [] }),
      });
    if (path === "/api/console/share-usage")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(chartUsagePayload()),
      });
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: "{}",
    });
  });
  const page = await context.newPage();
  try {
    await page.goto(`${ORIGIN}/`, { waitUntil: "domcontentloaded" });
    await page.addScriptTag({ content: browserBundle() });
    await page.evaluate(() => {
      window.CloudConsole.mountCloudConsole(document.querySelector("#stage"), {
        verdict: () => ({ state: "confirmed", reason: "" }),
        onChange: () => {},
      });
    });
    const primary = page.locator("[data-cloud-primary]");
    await primary.waitFor({ timeout: 10000 });
    assert.equal((await primary.innerText()).trim(), "启动");
    await primary.click();
    await page.waitForFunction(
      () =>
        (
          document.querySelector("[data-cloud-primary]")?.textContent ?? ""
        ).includes("启动中…（等待认证）"),
      null,
      { timeout: 10000 }
    );
    assert.equal(
      await page.locator("[data-cloud-primary]").count(),
      1,
      "启动后主动作按钮不止一个"
    );
    const stage = page.locator("[data-cloud-primary-stage]");
    await stage.waitFor({ timeout: 10000 });
    assert.match(await stage.innerText(), new RegExp(stepNote));
    assert.equal(
      await page.locator('[data-cloud-primary="stop"]').count(),
      0,
      "启动中不应出现停止"
    );
    assert.deepEqual(
      state.posts.map((body) => `${body.action}|${body.target}`),
      ["share.chain_start|share"]
    );
    // Completion: the service read says running, so the same button is 停止.
    state.running = true;
    state.operations = [
      {
        id: "op-start",
        action: "share.chain_start",
        target: "share",
        state: "succeeded",
        created_at: Math.floor(Date.now() / 1000),
        finished_at: Math.floor(Date.now() / 1000),
        result: { state: "running" },
        steps: [{ at: Math.floor(Date.now() / 1000), note: stepNote }],
      },
    ];
    await page.locator("[data-cloud-refresh]").first().click();
    await page.waitForFunction(
      () =>
        (
          document.querySelector("[data-cloud-primary]")?.textContent ?? ""
        ).trim() === "停止",
      null,
      { timeout: 10000 }
    );
    assert.equal(
      await page.locator("[data-cloud-primary-stage]").count(),
      0,
      "完成后的停止按钮仍带启动阶段说明"
    );
  } finally {
    await browser.close();
  }
});

test("primary action: 停止 opens the stop confirm and offers 强制停止 only after a drain timeout", async () => {
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ channel: "chrome" });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    serviceWorkers: "block",
    locale: "zh-CN",
  });
  const state = { running: true, operations: [], posts: [] };
  await context.route("**/*", (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin !== ORIGIN) return route.abort();
    if (req.method() !== "GET" && req.method() !== "HEAD") {
      const body = JSON.parse(req.postData() || "{}");
      state.posts.push(body);
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          operation: {
            id: "op-stop",
            action: body.action,
            target: body.target,
            state: "running",
            created_at: Math.floor(Date.now() / 1000),
            steps: [],
          },
        }),
      });
    }
    if (url.pathname === "/")
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: HTML,
      });
    const path = url.pathname.replace(/^\/gw\/usagesrv/, "");
    if (path === "/api/console/operations/op-stop")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          operation: {
            id: "op-stop",
            action: "share.chain_stop",
            target: "share",
            state: "succeeded",
            created_at: Math.floor(Date.now() / 1000),
            finished_at: Math.floor(Date.now() / 1000),
            result: { state: "stopped" },
            steps: [],
          },
        }),
      });
    if (path === "/api/console/operations")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ operations: state.operations }),
      });
    if (path === "/api/console/actions")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          actions: [
            { action: "share.chain_start", enabled: true },
            { action: "share.chain_stop", enabled: true },
            { action: "share.chain_estop", enabled: true },
          ],
        }),
      });
    if (path === "/api/summary")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ role: "cloud" }),
      });
    if (path === "/api/console/health")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          checked_at: Math.floor(Date.now() / 1000),
          services: [{ key: "share-gateway", state: "Running", healthy: true }],
        }),
      });
    if (path === "/api/console/share-management/status")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          gateway_running: state.running,
          in_sync: true,
          account: { running: true, valid: true },
          last_check_at: Math.floor(Date.now() / 1000),
        }),
      });
    if (path === "/api/console/share-management/display-state")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          available: true,
          state: "not_started",
          label: "未开始登录",
          detail: null,
          observed_at: null,
          login_window_deadline_epoch: null,
          display: { available: true, reason: null },
        }),
      });
    if (path === "/api/console/share-management/keys")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ keys: [] }),
      });
    if (path === "/api/console/share-usage")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(chartUsagePayload()),
      });
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: "{}",
    });
  });
  const page = await context.newPage();
  try {
    await page.goto(`${ORIGIN}/`, { waitUntil: "domcontentloaded" });
    await page.addScriptTag({ content: browserBundle() });
    await page.evaluate(() => {
      window.CloudConsole.mountCloudConsole(document.querySelector("#stage"), {
        verdict: () => ({ state: "confirmed", reason: "" }),
        onChange: () => {},
      });
    });
    await page.waitForFunction(
      () =>
        (
          document.querySelector("[data-cloud-primary]")?.textContent ?? ""
        ).trim() === "停止",
      null,
      { timeout: 10000 }
    );
    const dialog = page.locator(".shell-confirm");
    await page.locator('[data-cloud-primary="stop"]').click();
    await dialog.waitFor({ state: "visible", timeout: 10000 });
    assert.equal(
      (await dialog.locator(".shell-confirm-body").innerText()).trim(),
      "停止会先拒绝新请求，等待在途请求结束。若有请求未结束，会提示可以强制停止。确定停止？"
    );
    assert.equal(
      await page.locator("[data-cloud-confirm-force]").isVisible(),
      false,
      "未排空超时就出现强制停止"
    );
    await dialog.locator('button[value="cancel"]').click();
    await dialog.waitFor({ state: "hidden", timeout: 10000 });
    assert.deepEqual(state.posts, [], "取消停止不应提交");
    // A drain timeout is the only evidence that offers 强制停止.
    state.operations = [
      {
        id: "op-stop",
        action: "share.chain_stop",
        target: "share",
        state: "succeeded",
        created_at: Math.floor(Date.now() / 1000),
        finished_at: Math.floor(Date.now() / 1000),
        result: { state: "drain_timeout" },
        steps: [],
      },
    ];
    await page.locator("[data-cloud-refresh]").first().click();
    await page.waitForFunction(
      () =>
        (
          document.querySelector("[data-cloud-primary]")?.textContent ?? ""
        ).trim() === "停止",
      null,
      { timeout: 10000 }
    );
    await page.locator('[data-cloud-primary="stop"]').click();
    await dialog.waitFor({ state: "visible", timeout: 10000 });
    const force = page.locator("[data-cloud-confirm-force]");
    assert.equal(await force.isVisible(), true, "排空超时后确认框没有强制停止");
    assert.equal((await force.innerText()).trim(), "强制停止");
    await force.click();
    await dialog.waitFor({ state: "hidden", timeout: 10000 });
    await page.waitForTimeout(400);
    assert.deepEqual(
      state.posts.map((body) => `${body.action}|${body.target}`),
      ["share.chain_estop|share"]
    );
  } finally {
    await browser.close();
  }
});

test("primary action: the page has one primary control and none of the three old buttons or the long note", () => {
  const html = overview.overviewHtml(
    overviewModel({ status: ready({ gateway_running: false, in_sync: true }) })
  );
  assert.equal(
    [...html.matchAll(/data-cloud-primary=/g)].length,
    1,
    "单页主动作按钮不止一个"
  );
  for (const text of [
    "一键启动",
    "普通停止",
    "强制停止",
    "排空超时会明确显示为强制停止",
    "立即取消在途",
  ])
    assert.doesNotMatch(html, new RegExp(text), text);
});

test("status row: clicking the word expands four evidence items and the two read-only actions, clicking again collapses", async () => {
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ channel: "chrome" });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    serviceWorkers: "block",
    locale: "zh-CN",
  });
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== ORIGIN) return route.abort();
    const path = url.pathname.replace(/^\/gw\/usagesrv/, "");
    if (path === "/")
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: HTML,
      });
    if (path === "/api/console/share-management/status")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          gateway_running: true,
          in_sync: true,
          account: { running: true, valid: true },
          upstream: { reachable: true },
          last_check_at: Math.floor(Date.now() / 1000),
        }),
      });
    if (path === "/api/console/share-management/display-state")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          available: true,
          state: "logging_in",
          label: "正在登录",
          detail: null,
          observed_at: null,
          login_window_deadline_epoch: null,
          display: { available: true, reason: null },
        }),
      });
    if (path === "/api/console/share-management/keys")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ keys: [] }),
      });
    if (path === "/api/console/share-usage")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(chartUsagePayload()),
      });
    if (path === "/api/console/operations")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          operations: [
            {
              id: "op-status",
              action: "share.status",
              target: "share",
              state: "succeeded",
              created_at: Math.floor(Date.now() / 1000) - 60,
              finished_at: Math.floor(Date.now() / 1000) - 59,
              result: {
                layers: { share_backend: "running" },
                backend_auth: {
                  state: "valid",
                  label: "认证有效",
                  observed_at: Math.floor(Date.now() / 1000) - 60,
                },
              },
              error: null,
              steps: [],
            },
            {
              id: "op-login",
              action: "share.backend_login",
              target: "share",
              state: "succeeded",
              created_at: Math.floor(Date.now() / 1000) - 120,
              finished_at: Math.floor(Date.now() / 1000) - 110,
              result: { state: "logging_in" },
              error: null,
              steps: [],
            },
          ],
        }),
      });
    if (path === "/api/console/actions")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          actions: [
            { action: "share.status", enabled: true },
            { action: "share.backend_login", enabled: true },
            { action: "share.backend_reauthenticate", enabled: true },
          ],
        }),
      });
    if (path === "/api/summary")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ role: "cloud" }),
      });
    if (path === "/api/console/health")
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          checked_at: Math.floor(Date.now() / 1000),
          services: [{ key: "share-gateway", state: "Running", healthy: true }],
        }),
      });
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: "{}",
    });
  });
  const page = await context.newPage();
  try {
    await page.goto(`${ORIGIN}/`, { waitUntil: "domcontentloaded" });
    await page.addStyleTag({
      content: readFileSync(join(WEB, "src/shell/tokens.css"), "utf8"),
    });
    await page.addStyleTag({
      content: readFileSync(join(WEB, "src/shell/shell.css"), "utf8"),
    });
    await page.addStyleTag({
      content: readFileSync(join(WEB, "src/cloud/cloud.css"), "utf8"),
    });
    await page.addScriptTag({ content: browserBundle() });
    await page.evaluate(() => {
      window.CloudConsole.mountCloudConsole(document.querySelector("#stage"), {
        verdict: () => ({ state: "confirmed", reason: "" }),
        onChange: () => {},
      });
    });
    await page.waitForSelector("[data-cloud-status-toggle]", {
      timeout: 10000,
    });
    assert.equal(
      await page.locator("[data-cloud-status-detail]").count(),
      0,
      "状态详情默认应收起"
    );
    await page.locator("[data-cloud-status-toggle]").click();
    const detail = page.locator("[data-cloud-status-detail]");
    await detail.waitFor({ timeout: 10000 });
    const text = (await detail.innerText()).replace(/\s+/g, " ");
    for (const evidence of [
      "认证读数",
      "上游认证",
      "最近核验",
      "登录动作",
      "分层检查（只读）",
      "重新认证 UM",
    ])
      assert.match(text, new RegExp(evidence), evidence);
    await page
      .locator('[data-cloud-status-detail] [data-cloud-op="share.status|share"]')
      .click();
    const confirm = page.locator(".shell-confirm");
    await confirm.waitFor({ state: "visible", timeout: 10000 });
    assert.equal(
      (await confirm.locator(".shell-confirm-body").innerText()).trim(),
      "只读取当前服务各层状态并记录一次检查，不改变、不重启云端 API 服务。",
      "只读分层检查被说成会改变服务"
    );
    await confirm.locator('button[value="cancel"]').click();
    await confirm.waitFor({ state: "hidden", timeout: 10000 });
    await page.locator("[data-cloud-status-toggle]").click();
    await page.waitForTimeout(80);
    assert.equal(
      await page.locator("[data-cloud-status-detail]").count(),
      0,
      "再点状态词应收起详情"
    );
  } finally {
    await browser.close();
  }
});

test("U: a partial cost below ¥0.01 never renders ≥<¥0.01 and stays separate from the cloud bill", () => {
  const html = overview.overviewHtml(overviewModel());
  assert.match(html, /≥¥0\.005/);
  assert.doesNotMatch(html, /≥<¥0\.01/);
  assert.match(html, /不是云账单/);
  assert.equal(overview.cnyBound(0.005, true), "≥¥0.005");
  assert.equal(overview.cnyBound(0.5, true), "≥¥0.50");
  assert.equal(overview.cnyBound(0, true), "¥0.00");
});

function browserBundle() {
  return buildSync({
    stdin: {
      contents: `export { mountCloudConsole } from "./src/cloud/cloud-console.ts"; export { paintTheme } from "./src/theme-ui.ts"; export { RoleGate, installRoleGate, BUILD_ROLE } from "./src/console/role-gate.ts";`,
      resolveDir: WEB,
      sourcefile: "cloud-console-browser-entry.ts",
      loader: "ts",
    },
    bundle: true,
    format: "iife",
    globalName: "CloudConsole",
    platform: "browser",
    write: false,
    loader: { ".css": "empty" },
    define: {
      "import.meta.env.MODE": '"test"',
      __CONSOLE_ROLE__: '"cloud"',
      __CONSOLE_CANDIDATE__: "false",
    },
  }).outputFiles[0].text;
}

const HTML = '<!doctype html><html><body><div id="stage"></div></body></html>';

test("A2: submit answers keep or close the intent and a refresh never re-POSTs", async () => {
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ channel: "chrome" });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    serviceWorkers: "block",
    locale: "zh-CN",
  });
  const state = {
    answer: null,
    posts: 0,
    identityKey: null,
    identityFound: false,
    identityDelayMs: 0,
  };
  await context.route("**/*", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin !== ORIGIN) return route.abort();
    if (req.method() !== "GET" && req.method() !== "HEAD") {
      state.posts += 1;
      return route.fulfill({
        status: state.answer.status,
        contentType: "application/json",
        body: JSON.stringify(state.answer.body ?? {}),
      });
    }
    if (url.pathname === "/")
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: HTML,
      });
    const path = url.pathname.replace(/^\/gw\/usagesrv/, "");
    if (
      path === "/api/console/operations" &&
      url.searchParams.get("limit") === "200"
    ) {
      if (state.identityFound && state.identityKey) {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            operations: [
              {
                id: "fx-intent-op",
                action: "share.status",
                target: "share",
                state: "succeeded",
                created_at: 1793413000,
                finished_at: 1793413001,
                result: {
                  layers: { share_backend: "running" },
                  backend_auth: { state: "valid", label: "认证有效" },
                },
                error: null,
                steps: [],
                idempotency_key: state.identityKey,
              },
            ],
          }),
        });
      }
      if (state.identityDelayMs > 0)
        await new Promise((resolve) =>
          setTimeout(resolve, state.identityDelayMs)
        );
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ operations: [] }),
      });
    }
    if (
      path === "/api/console/operations" &&
      url.searchParams.get("limit") === "8"
    ) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ operations: [] }),
      });
    }
    if (path === "/api/console/actions") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          actions: [{ action: "share.status", enabled: true }],
        }),
      });
    }
    if (path === "/api/summary") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ role: "cloud" }),
      });
    }
    if (path === "/api/console/health") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          checked_at: Math.floor(Date.now() / 1000),
          services: [{ key: "share-gateway", state: "Running", healthy: true }],
        }),
      });
    }
    if (path === "/api/console/share-management/status") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          account: { running: true, valid: true },
          last_check_at: Math.floor(Date.now() / 1000),
        }),
      });
    }
    if (path === "/api/console/share-management/keys") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ keys: [] }),
      });
    }
    if (path === "/api/console/share-usage") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          keys: [],
          unattributed: {},
          coverage: {},
          checkedAt: null,
          requests: null,
          filters: null,
        }),
      });
    }
    if (path === "/api/console/cloud-ops") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({}),
      });
    }
    if (path === "/api/console/share-management/display-session") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ available: false, reason: "测试" }),
      });
    }
    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: "{}",
    });
  });

  const bundle = browserBundle();
  const errors = [];
  const mount = async () => {
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(String(error.message)));
    await page.goto(`${ORIGIN}/`, { waitUntil: "domcontentloaded" });
    await page.addScriptTag({ content: bundle });
    await page.evaluate(() => {
      window.CloudConsole.mountCloudConsole(document.querySelector("#stage"), {
        verdict: () => ({ state: "confirmed", reason: "" }),
        onChange: () => {},
      });
    });
    await page.waitForFunction(
      () => {
        const button = document.querySelector(
          '[data-cloud-op="share.status|share"]'
        );
        return button instanceof HTMLButtonElement && !button.disabled;
      },
      null,
      { timeout: 10000 }
    );
    return page;
  };
  const text = async (page) =>
    (await page.locator("body").innerText()).replace(/\s+/g, " ");
  const intents = (page) =>
    page.evaluate(() =>
      sessionStorage.getItem("cloud-console.pending-intents.v1")
    );
  const submit = async (page) => {
    // U: the layered check lives in the overview's evidence fold, so open it
    // before clicking the read-only action.
    const evidence = page.locator("details[data-cloud-evidence]");
    if (
      (await evidence.count()) &&
      !(await evidence.first().getAttribute("open"))
    ) {
      await evidence.first().locator("summary").click();
    }
    await page.locator('[data-cloud-op="share.status|share"]').first().click();
    await page.waitForTimeout(200);
    await page.locator('.shell-confirm button[value="confirm"]').click();
  };

  try {
    // A definite 4xx refusal closes the intent; no re-query entry remains.
    const refused = await mount();
    state.answer = {
      status: 409,
      body: { error: "fixture_conflict", reason: "夹具明确拒绝" },
    };
    await submit(refused);
    await refused.waitForTimeout(800);
    assert.equal(state.posts, 1, "4xx 明确拒绝应只提交一次");
    assert.equal(await intents(refused), null, "4xx 明确拒绝后仍保留幂等身份");
    assert.match(await text(refused), /被拒绝/);
    assert.equal(await refused.locator("[data-cloud-op-recheck]").count(), 0);
    await refused.close();

    // A 5xx keeps the intent, shows the unconfirmed wording, and a refresh
    // settles the original identity from the bounded page without a new POST.
    const unconfirmed = await mount();
    state.posts = 0;
    state.answer = { status: 500, body: { error: "fixture_unavailable" } };
    state.identityDelayMs = 5000;
    await submit(unconfirmed);
    await unconfirmed.waitForTimeout(1200);
    const intentsRaw = await intents(unconfirmed);
    const stored = JSON.parse(intentsRaw);
    assert.equal(state.posts, 1, "未确认应答应只提交一次");
    assert.equal(stored.length, 1, "未确认应答没有保留幂等身份");
    assert.match(await text(unconfirmed), /结果未确认/);
    assert.match(await text(unconfirmed), /已保留幂等身份/);
    assert.ok(
      (await unconfirmed.locator("[data-cloud-op-recheck]").count()) > 0
    );
    state.identityKey = stored[0].key;
    state.identityFound = true;
    state.identityDelayMs = 0;
    await unconfirmed.reload({ waitUntil: "domcontentloaded" });
    await unconfirmed.addScriptTag({ content: bundle });
    await unconfirmed.evaluate(() => {
      window.CloudConsole.mountCloudConsole(document.querySelector("#stage"), {
        verdict: () => ({ state: "confirmed", reason: "" }),
        onChange: () => {},
      });
    });
    await unconfirmed
      .waitForFunction(() => /已完成/.test(document.body.innerText), null, {
        timeout: 10000,
      })
      .catch(() => {});
    const refreshed = await text(unconfirmed);
    assert.match(
      refreshed,
      /已完成/,
      `刷新后没有按身份找到原操作：${refreshed.slice(0, 240)}`
    );
    assert.match(refreshed, /fx-intent-op/);
    assert.equal(state.posts, 1, "刷新回查出现了新的 POST");
    assert.equal(
      await intents(unconfirmed),
      null,
      "原操作已完成但仍保留幂等身份"
    );
    await unconfirmed.close();

    // 408, 429 and a 200 without an operation id keep the intent too.
    for (const [label, status] of [
      ["408", 408],
      ["429", 429],
      ["200-empty", 200],
    ]) {
      const page = await mount();
      state.posts = 0;
      state.answer = { status, body: {} };
      state.identityDelayMs = 5000;
      await submit(page);
      await page.waitForTimeout(1200);
      const raw = await intents(page);
      assert.equal(state.posts, 1, `${label}: 未确认应答应只提交一次`);
      assert.equal(
        JSON.parse(raw).length,
        1,
        `${label}: 未确认应答没有保留幂等身份`
      );
      assert.match(await text(page), /结果未确认/);
      assert.match(await text(page), /已保留幂等身份/);
      assert.ok((await page.locator("[data-cloud-op-recheck]").count()) > 0);
      await page.close();
    }
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test("role gate: the cloud console reads /api/summary and enables 启动 once the backend reports cloud", async () => {
  // The shipped boot (real RoleGate + installRoleGate) against a backend that
  // reports role=cloud. The gate's own contract names GET /api/summary as the
  // cloud marker, so a console that never reads it stays 正在核对后端角色 and
  // keeps 启动 disabled forever.
  const seen = [];
  const { browser, page, errors } = await mountKeysPage((route, req, path) => {
    seen.push(path);
    const json = (body) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    if (path === "/api/summary")
      return json({
        role: "cloud",
        assembly: { role: "cloud", services: ["share"], excluded: [] },
        services: [],
        sources: [],
      });
    if (path === "/api/console/actions") return json({ actions: [] });
    if (path === "/api/console/share-management/status")
      return json({ gateway_running: false });
    return json({});
  }, { realGate: true });
  try {
    await page.waitForSelector("[data-cloud-primary]", { timeout: 10000 });
    await page.waitForTimeout(1500);
    assert.ok(
      seen.includes("/api/summary"),
      "云端页没有读 /api/summary：角色永远无法确认"
    );
    assert.match(
      await page.locator("body").innerText(),
      /后端角色已确认/
    );
    assert.equal(
      await page.locator(".cloud-primary-note").count(),
      0,
      "角色已确认，主按钮不应再挂着角色说明"
    );
    assert.equal(
      await page.locator('[data-cloud-primary="start"]').first().isDisabled(),
      false,
      "角色确认后 启动 仍被禁用"
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test("role gate: the cloud console's read plan keeps the role marker read", () => {
  // The gate's own contract names GET /api/summary as the cloud marker. A
  // refresh plan that drops the read starves the gate and closes every write
  // for the page's lifetime; this static check runs without a browser.
  const source = readFileSync(
    join(WEB, "src/cloud/cloud-console.ts"),
    "utf8"
  );
  assert.match(source, /readSummary/);
  assert.match(source, /step\(\s*"summary"\s*,\s*readSummary\s*\)/);
});
