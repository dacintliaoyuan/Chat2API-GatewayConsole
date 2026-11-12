import test from "node:test";
import assert from "node:assert/strict";
import * as readiness from "../../src/console/share-readiness.ts";
import {
  localShareReadiness,
  publicShareReadings,
  latestShareOperationWasStop,
  publicSupervisionReading,
  publicSupervisionStale,
  publicRecoveryReading,
  publicBudgetText,
  publicBudgetRetryReason,
  publicNextActionText,
} from "../../src/console/share-readiness.ts";

const ready = {
  gateway_running: true,
  account: { running: true, valid: true },
  in_sync: true,
  funnel_on: true,
  share_url: "https://share.fixture.invalid/",
};
const sample = {
  rowVersion: 3,
  state: "ok",
  reason: "ok",
  startedAt: 99,
  at: 100,
  stage: "inference",
  publicUrl: ready.share_url,
  publicIp: "1.1.1.1",
  model: "fixture",
};

test("a newer lifecycle operation supersedes an old stop before a later outage", () => {
  const stop = { action: "share.stop", state: "succeeded", created_at: 10 };
  const start = { action: "share.start", state: "succeeded", created_at: 20 };
  assert.equal(latestShareOperationWasStop([stop, start]), false);
  assert.equal(
    latestShareOperationWasStop([{ ...start, state: "failed" }, stop]),
    false
  );
  assert.equal(
    latestShareOperationWasStop([
      start,
      { ...stop, action: "share.retry_stop", created_at: 30 },
    ]),
    true
  );
  assert.equal(
    latestShareOperationWasStop([{ ...stop, state: "failed" }]),
    false
  );
});

test("Funnel mapping alone cannot prove local readiness", () => {
  assert.equal(
    localShareReadiness({ funnel_on: true, in_sync: true }).state,
    "idle"
  );
  assert.equal(
    localShareReadiness({ ...ready, account: { running: false, valid: true } })
      .state,
    "warn"
  );
  assert.equal(
    localShareReadiness({ ...ready, gateway_running: false }).state,
    "warn"
  );
  assert.equal(
    localShareReadiness({ ...ready, account: { valid: true } }).state,
    "idle"
  );
  assert.equal(localShareReadiness(ready).state, "ok");
});

test("local readiness and a DNS failure do not invent a model response", () => {
  assert.deepEqual(publicShareReadings(ready), {
    dns: { state: "idle", text: "未测试" },
    model: { state: "idle", text: "未测试" },
  });
  const result = publicShareReadings(
    ready,
    {
      ...sample,
      state: "error",
      stage: "dns",
      reason: "public_dns_failed",
      publicIp: undefined,
    },
    101
  );
  assert.equal(result.dns.text, "解析未通过（测试时）");
  assert.deepEqual(result.model, {
    state: "warn",
    text: "未进入模型推理（测试时）",
  });
});

test("a public DNS success survives a model rejection as a separate fact", () => {
  const result = publicShareReadings(
    ready,
    { ...sample, state: "error", reason: "key_rejected" },
    101
  );
  assert.equal(result.dns.state, "ok");
  assert.equal(result.model.text, "调用未通过（测试时）");
});

test("a successful sample carries freshness and address boundaries", () => {
  assert.equal(
    publicShareReadings(ready, sample, 101).model.text,
    "已回复（测试时）"
  );
  assert.equal(publicShareReadings(ready, sample, 401).dns.state, "warn");
  assert.equal(publicShareReadings(ready, sample, 99).model.state, "warn");
  assert.equal(
    publicShareReadings(
      { ...ready, share_url: "https://new.fixture.invalid/" },
      sample,
      101
    ).model.text,
    "当前地址未测试"
  );
});

test("TLS and catalog failures are warnings even when DNS resolved", () => {
  const failed = {
    ...sample,
    state: "error",
    stage: "models",
    reason: "tls_handshake_failed",
  };
  assert.deepEqual(publicShareReadings(ready, failed, 101).model, {
    state: "warn",
    text: "公网 TLS 握手失败，未进入模型推理（测试时）",
  });
  assert.equal(publicShareReadings(ready, failed, 101).dns.state, "ok");
  const catalog = publicShareReadings(
    ready,
    { ...failed, reason: "models_failed" },
    101
  );
  assert.deepEqual(catalog.model, {
    state: "warn",
    text: "模型目录检查失败，未进入模型推理（测试时）",
  });
  assert.match(publicShareReadings(ready, failed, 401).model.text, /已陈旧/);
  assert.equal(
    publicShareReadings(ready, { ...failed, state: "busy" }, 401).model.state,
    "idle"
  );
  assert.equal(
    publicShareReadings(
      { ...ready, share_url: "https://new.fixture.invalid/" },
      failed,
      101
    ).model.text,
    "当前地址未测试"
  );
});

test("public summary reports sampled evidence independently of local readiness", () => {
  assert.equal(typeof readiness.publicShareSummary, "function");
  const summary = readiness.publicShareSummary;
  assert.deepEqual(summary(ready, undefined, 101), {
    state: "idle",
    text: "公网未验证",
  });
  assert.deepEqual(
    summary(
      { ...ready, share_url: "https://new.fixture.invalid/" },
      sample,
      101
    ),
    { state: "idle", text: "公网未验证" }
  );
  assert.deepEqual(summary(ready, { ...sample, state: "busy" }, 401), {
    state: "idle",
    text: "公网测试中",
  });
  assert.deepEqual(
    summary(
      ready,
      {
        ...sample,
        state: "error",
        stage: "models",
        reason: "tls_handshake_failed",
      },
      101
    ),
    { state: "warn", text: "公网测试失败" }
  );
  assert.deepEqual(summary(ready, sample, 101), {
    state: "ok",
    text: "公网已回复（测试时）",
  });
  assert.equal(
    summary({ ...ready, funnel_on: false }, sample, 101).state,
    "ok"
  );
  for (const state of ["ok", "error"]) {
    assert.deepEqual(summary(ready, { ...sample, state }, 401), {
      state: "warn",
      text: "公网结果已陈旧",
    });
    assert.deepEqual(summary(ready, { ...sample, state, at: undefined }, 101), {
      state: "warn",
      text: "公网结果已陈旧",
    });
    assert.deepEqual(summary(ready, { ...sample, state }, 99), {
      state: "warn",
      text: "公网结果已陈旧",
    });
    assert.deepEqual(
      summary(ready, { ...sample, state, at: Number.NaN }, 101),
      { state: "warn", text: "公网结果已陈旧" }
    );
  }
  assert.equal(summary(ready, sample, 400).state, "ok");
  assert.equal(
    summary(ready, { ...sample, publicUrl: ready.share_url.slice(0, -1) }, 101)
      .state,
    "ok"
  );
});

const publicSnapshot = (
  overall,
  recovery = null,
  round = null,
  observedAt = 100
) => ({
  observedAt,
  overall,
  reason: overall === "ok" ? "ok" : "fixture",
  publicUrl: null,
  host: null,
  entries: [],
  recovery,
  budget: null,
  intent: null,
  round,
  local: null,
});

test("public supervision readings keep missing and unknown apart from ok", () => {
  const now = 101;
  assert.deepEqual(publicSupervisionReading(null, now), {
    state: "idle",
    text: "未提供",
  });
  assert.deepEqual(publicSupervisionReading(publicSnapshot("ok"), now), {
    state: "ok",
    text: "正常",
  });
  assert.deepEqual(publicSupervisionReading(publicSnapshot("degraded"), now), {
    state: "warn",
    text: "部分可用（降级）",
  });
  assert.deepEqual(
    publicSupervisionReading(publicSnapshot("all_failed"), now),
    { state: "danger", text: "全部入口失败" }
  );
  assert.deepEqual(
    publicSupervisionReading(
      publicSnapshot("all_failed", { state: "manual_required" }),
      now
    ),
    { state: "danger", text: "全部入口失败 · 需人工" }
  );
  assert.deepEqual(
    publicSupervisionReading(
      publicSnapshot("all_failed", { state: "budget_exhausted" }),
      now
    ),
    { state: "danger", text: "全部入口失败 · 预算耗尽" }
  );
  assert.equal(
    publicSupervisionReading(publicSnapshot("incomplete"), now).state,
    "idle"
  );
  assert.equal(
    publicSupervisionReading(publicSnapshot("not_checked"), now).state,
    "idle"
  );
  assert.equal(
    publicSupervisionReading(publicSnapshot("unknown"), now).state,
    "idle"
  );
});

test("a stale public supervision snapshot is never rendered as normal", () => {
  const now = 1000;
  assert.equal(
    publicSupervisionStale(publicSnapshot("ok", null, null, now - 20), now),
    false
  );
  assert.equal(
    publicSupervisionStale(publicSnapshot("ok", null, null, now - 121), now),
    true
  );
  assert.equal(
    publicSupervisionStale(publicSnapshot("ok", null, null, undefined), now),
    true
  );
  assert.deepEqual(
    publicSupervisionReading(publicSnapshot("ok", null, null, now - 121), now),
    { state: "warn", text: "结果陈旧（未按周期巡检）" }
  );
  assert.deepEqual(
    publicSupervisionReading(publicSnapshot("ok", null, null, undefined), now),
    { state: "warn", text: "结果陈旧（未按周期巡检）" }
  );
  const missedRound = publicSnapshot(
    "ok",
    null,
    {
      nextCheckAt: new Date((now - 121) * 1000).toISOString(),
      nextActionAt: null,
    },
    now - 20
  );
  assert.deepEqual(publicSupervisionReading(missedRound, now), {
    state: "warn",
    text: "结果陈旧（未按周期巡检）",
  });
  assert.match(
    publicSupervisionReading(
      publicSnapshot("all_failed", null, null, now - 121),
      now
    ).text,
    /结果陈旧/
  );
});

test("manual stop and cancellation override an otherwise healthy snapshot", () => {
  const stopped = {
    ...publicSnapshot("ok", { state: "succeeded" }, null, 1000),
    intent: { state: "stopped" },
  };
  const cancelled = publicSnapshot("ok", { state: "succeeded" }, null, 1000);
  assert.deepEqual(publicSupervisionReading(stopped, 1001), {
    state: "idle",
    text: "分享已停止",
  });
  assert.deepEqual(publicSupervisionReading(cancelled, 1001, true), {
    state: "idle",
    text: "巡检已暂停",
  });
  assert.deepEqual(publicRecoveryReading(stopped), {
    state: "idle",
    text: "已停止",
  });
  assert.deepEqual(publicRecoveryReading(cancelled, true), {
    state: "idle",
    text: "已暂停",
  });
});

test("public recovery readings distinguish progress, manual and stopped", () => {
  assert.deepEqual(publicRecoveryReading(null), {
    state: "idle",
    text: "未提供",
  });
  assert.deepEqual(publicRecoveryReading(publicSnapshot("ok")), {
    state: "idle",
    text: "未提供",
  });
  assert.deepEqual(
    publicRecoveryReading(publicSnapshot("ok", { state: "idle" })),
    { state: "idle", text: "未在恢复" }
  );
  assert.deepEqual(
    publicRecoveryReading(publicSnapshot("ok", { state: "pending" })),
    { state: "warn", text: "恢复中（准备）" }
  );
  assert.deepEqual(
    publicRecoveryReading(
      publicSnapshot("ok", { state: "running", stage: "ts_start" })
    ),
    { state: "warn", text: "恢复中（ts_start）" }
  );
  assert.deepEqual(
    publicRecoveryReading(publicSnapshot("ok", { state: "succeeded" })),
    { state: "ok", text: "已恢复" }
  );
  assert.deepEqual(
    publicRecoveryReading(publicSnapshot("all_failed", { state: "failed" })),
    { state: "danger", text: "恢复未确认" }
  );
  assert.deepEqual(
    publicRecoveryReading(
      publicSnapshot("all_failed", { state: "manual_required" })
    ),
    { state: "danger", text: "需人工" }
  );
  assert.deepEqual(
    publicRecoveryReading(
      publicSnapshot("all_failed", { state: "budget_exhausted" })
    ),
    { state: "danger", text: "预算耗尽" }
  );
  assert.deepEqual(
    publicRecoveryReading(publicSnapshot("all_failed", { state: "stopped" })),
    { state: "idle", text: "已停止" }
  );
  assert.deepEqual(
    publicRecoveryReading(publicSnapshot("all_failed", { state: "unknown" })),
    { state: "idle", text: "未知" }
  );
});

test("public budget text never folds unreadable into 0/0 and names the retry time", () => {
  assert.equal(publicBudgetText(null), "预算未提供");
  assert.equal(
    publicBudgetText({ unreadable: true, reason: "budget_unreadable" }),
    "预算不可读，不自动恢复"
  );
  assert.equal(
    publicBudgetText({
      unreadable: false,
      windowSeconds: 1800,
      mappingUsed: 1,
      mappingLimit: 2,
      tsRestartUsed: 0,
      tsRestartLimit: 1,
      mappingNextEligibleAt: null,
      tsRestartNextEligibleAt: null,
    }),
    "映射 1/2 · TS 重启 0/1 · 窗口 30 分钟"
  );
  const exhausted = publicBudgetText({
    unreadable: false,
    windowSeconds: 1800,
    mappingUsed: 2,
    mappingLimit: 2,
    tsRestartUsed: 1,
    tsRestartLimit: 1,
    mappingNextEligibleAt: "2026-10-24T12:30:00+00:00",
    tsRestartNextEligibleAt: "2026-10-24T12:40:00+00:00",
  });
  assert.match(exhausted, /下次可重试/);
  assert.equal(exhausted.includes("未提供"), false);
  assert.match(
    publicBudgetText({
      unreadable: false,
      windowSeconds: null,
      mappingUsed: 2,
      mappingLimit: 2,
      tsRestartUsed: 1,
      tsRestartLimit: 1,
      mappingNextEligibleAt: null,
      tsRestartNextEligibleAt: null,
    }),
    /下次可重试时间未提供/
  );
});

test("a missing or partial budget is never a readable 0/0 budget", () => {
  const base = {
    windowSeconds: 1800,
    mappingUsed: 0,
    mappingLimit: 2,
    tsRestartUsed: 0,
    tsRestartLimit: 1,
    mappingNextEligibleAt: null,
    tsRestartNextEligibleAt: null,
  };
  assert.equal(
    publicBudgetText({ ...base, unreadable: null, reason: null }),
    "预算未提供（不可读标记未提供），不自动恢复"
  );
  assert.equal(
    publicBudgetRetryReason({ ...base, unreadable: null, reason: null }),
    "预算未提供（不可读标记未提供），不自动恢复"
  );
  assert.equal(
    publicBudgetText({ ...base, unreadable: false, mappingLimit: null }),
    "预算未提供（次数不完整），不自动恢复"
  );
  assert.equal(
    publicBudgetRetryReason({ ...base, unreadable: false, mappingLimit: null }),
    "预算未提供（次数不完整），无法确认剩余次数"
  );
  assert.equal(publicBudgetRetryReason({ ...base, unreadable: false }), null);
  assert.equal(
    publicBudgetRetryReason({
      ...base,
      unreadable: false,
      mappingUsed: 2,
      tsRestartUsed: 1,
    }),
    "预算耗尽，等待窗口滑出"
  );
});

test("public next action text keeps past, future and missing apart", () => {
  const now = 1761307200;
  const iso = (seconds) => new Date(seconds * 1000).toISOString();
  assert.equal(publicNextActionText(null, now), "未提供");
  assert.equal(publicNextActionText(publicSnapshot("ok"), now), "未提供");
  assert.equal(
    publicNextActionText(
      publicSnapshot("ok", null, { nextCheckAt: null, nextActionAt: null }),
      now
    ),
    "未提供"
  );
  assert.equal(
    publicNextActionText(
      publicSnapshot("ok", null, {
        nextCheckAt: iso(now - 1),
        nextActionAt: null,
      }),
      now
    ),
    "已到时间，等待下一轮（检查）"
  );
  const future = publicNextActionText(
    publicSnapshot("ok", null, {
      nextCheckAt: null,
      nextActionAt: iso(now + 60),
    }),
    now
  );
  assert.match(future, /^动作 /);
  assert.equal(future.includes("已到时间"), false);
  assert.equal(
    publicNextActionText(
      publicSnapshot("ok", null, {
        nextCheckAt: "not-a-time",
        nextActionAt: null,
      }),
      now
    ),
    "未提供"
  );
});
