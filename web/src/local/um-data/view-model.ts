/** Pure payload → view-model mapping for the local UM data controller.
 *
 *  Every function here is side-effect free: it only turns a backend payload (or a
 *  failed read) into the frozen `um-ui` view types. The controller modules
 *  apply the result to their own state; this module never fetches, publishes or
 *  touches the role gate. */

import {
  ActionBusyError,
  ActionUnavailableError,
  BackendReadError,
} from "../../console/backend/client";
import {
  ContractError,
  UmReadStatusError,
} from "../../console/backend/normalize";
import type {
  GatewayConnection,
  JsonObject,
  OperationResult,
  ReadResult,
  UmMaintenancePagePayload,
  UmOperationPayload,
  UmServicesPayload,
  UmTodayPayload,
  UmTrendPayload,
  UmUpstreamFaultsPayload,
} from "../../console/backend/types";
import type {
  ActionProgress,
  Failure,
  FailureKind,
  Load,
  MaintenanceAction,
  ModelTestState,
  Reading,
  UmAccess,
  UmLogin,
  UmMaintenancePage,
  UmModelRow,
  UmService,
  UmTestDetail,
  UmToday,
  UmTrend,
  UmUpstreamFaults,
  UmVerify,
} from "../um-ui/contract";
import { when } from "../um-ui/format";

/** chat2api on 8000: the only service the local UM console manages. */
export const CHAT2API = "chat2api";
export const UM_SOURCE = "um";

const SERVICE_STATE: Readonly<Record<string, UmService["state"]>> = {
  Running: "running",
  Starting: "starting",
  Stopping: "stopping",
  Stopped: "stopped",
  Residual: "stopped",
  Degraded: "degraded",
  Conflict: "degraded",
};
const SERVICE_WORD: Readonly<Record<string, string>> = {
  Running: "运行中",
  Starting: "启动中",
  Stopping: "停止中",
  Stopped: "未运行",
  Residual: "发现受管残留进程",
  Degraded: "降级",
  Conflict: "端口被占用",
};
const LOGIN_WORD: Readonly<Record<UmLogin["state"], string>> = {
  valid: "已登录",
  needs_login: "需要登录",
  invalid: "登录失败",
  logging_in: "登录中",
  unknown: "状态未知",
};
const DUO_PROMPT_WAITING_REASON = "duo_prompt_waiting";
const DUO_AUTO_WORD = "正在完成 Duo 验证";
const DUO_AUTO_DETAIL = "Duo 自动验证进行中；本轮仍在等待，无需本人操作";
const DUO_TAKEOVER_WORD = "等待本人确认 Duo 推送";
const DUO_TAKEOVER_DETAIL =
  "Duo 需要本人确认；本轮仍在等待，可打开可见窗口完成确认";
const RATE_LIMIT_ERROR = "reauth_refused:global_attempt_cap";
const RATE_LIMIT_DETAIL = "15 分钟内已达到 3 次重登启动上限";
const MANUAL_LOGIN_DETAILS: Readonly<Record<string, string>> = {
  umpass_autofill_protected: "Edge 已预填，浏览器会在你与页面交互前隐藏预填值，后台登录无法确认。请在 Edge 窗口中点击登录表单继续。",
  umpass_prefill_missing: "后台登录在 UMPASS 表单中未检测到可用的预填，请打开 Edge 继续登录。",
  umpass_mfa_required: "UMPASS 要求本人完成多重验证，请打开 Edge 继续登录。",
  unrecognized_page: "后台登录无法识别当前页面，请打开 Edge 继续登录。",
};

export type TestEntry = {
  model: string;
  state: ModelTestState;
  summary: string;
  detail: UmTestDetail | null;
};

export function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error ?? "未知错误");
}

/** One failed read as the frozen view's failure: no answer / unauthenticated /
 *  refused / unavailable / error are kept apart. */
export function failureFrom(error: unknown, at: number): Failure {
  if (error instanceof BackendReadError) {
    if (error.kind === "timeout")
      return { kind: "no-answer", reason: error.message, at };
    if (error.kind === "network")
      return { kind: "no-answer", reason: error.message, at };
    if (error.kind === "contract")
      return { kind: "error", reason: error.message, at };
    const status = error.status;
    if (status === 401)
      return { kind: "unauthenticated", reason: error.message, at };
    if (status === 403) return { kind: "refused", reason: error.message, at };
    if (
      status === 404 ||
      status === 410 ||
      status === 502 ||
      status === 503 ||
      status === 504
    )
      return { kind: "unavailable", reason: error.message, at };
    return { kind: "error", reason: error.message, at };
  }
  if (error instanceof UmReadStatusError) {
    const status = error.status;
    if (
      status === "missing" ||
      status === "unavailable" ||
      status === "unconfigured"
    )
      return { kind: "unavailable", reason: error.message, at };
    if (status === "denied")
      return { kind: "refused", reason: error.message, at };
    if (status === "auth_required")
      return { kind: "unauthenticated", reason: error.message, at };
    return { kind: "error", reason: error.message, at };
  }
  if (error instanceof ActionUnavailableError)
    return { kind: "unavailable", reason: error.message, at };
  if (error instanceof ActionBusyError)
    return { kind: "error", reason: error.message, at };
  if (error instanceof ContractError)
    return { kind: "error", reason: error.message, at };
  return { kind: "error", reason: messageOf(error), at };
}

/** The failure of one operation result (the operation itself answered, but its
 *  state is a failure/refusal): the backend's own code and message are kept. */
export function operationFailure(result: OperationResult, at: number): Failure {
  const error = result.operation?.error;
  const code = error ? text(error.code) : null;
  const message = error ? text(error.message) : null;
  const reason =
    [code, message].filter(Boolean).join("：") ||
    result.reason ||
    "操作没有成功";
  if (result.state === "unconfirmed") return { kind: "no-answer", reason, at };
  return { kind: "error", reason, at };
}

/** One operation record's own failure (the row's error), for reads whose
 *  operation record is the only evidence. */
function failureFromOperationPayload(
  op: UmOperationPayload,
  at: number
): Failure {
  const error = op.error;
  const code = error ? text(error.code) : null;
  const message = error ? text(error.message) : null;
  return {
    kind: "error",
    reason: [code, message].filter(Boolean).join("：") || "操作没有成功",
    at,
  };
}

export function operationNote(result: OperationResult): string {
  const error = result.operation?.error;
  const code = error ? text(error.code) : null;
  const message = error ? text(error.message) : null;
  return (
    [code, message].filter(Boolean).join("：") ||
    result.reason ||
    (result.state === "succeeded" ? "已完成" : "结果未确认")
  );
}

function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}
function object(value: unknown): JsonObject | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : null;
}
function rowsOf(value: unknown): JsonObject[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is JsonObject =>
          item !== null && typeof item === "object" && !Array.isArray(item)
      )
    : [];
}
export function rowText(row: JsonObject | null, key: string): string | null {
  return row ? text(row[key]) : null;
}
function rowNumber(row: JsonObject | null, key: string): number | null {
  const value = row ? row[key] : null;
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}
function rowCount(row: JsonObject | null, key: string): number | null {
  const value = rowNumber(row, key);
  return value !== null && Number.isSafeInteger(value) ? value : null;
}

/** A reading's number, only for the kinds that carry one. */
export function readingNumber(reading: Reading): number | null {
  return reading.kind === "exact" ||
    reading.kind === "atLeast" ||
    reading.kind === "estimate"
    ? reading.value
    : null;
}
function missing(note: string): Reading {
  return { kind: "missing", note };
}

export const TERMINAL = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
]);

export function verifyFrom(
  operations: UmOperationPayload[],
  at: number
): Load<UmVerify> {
  const op = operations.find(
    (candidate) =>
      candidate.action === "account.um_verify" && TERMINAL.has(candidate.state)
  );
  if (!op) return { state: "ready", data: null, at };
  const result = op.result;
  const verify = rowText(result, "verify");
  const note = rowText(result, "note");
  const reason = rowText(result, "reason");
  const stamp = op.finishedAt ?? op.createdAt ?? at;
  if (verify === "valid") {
    return {
      state: "ready",
      at,
      data: {
        result: "passed",
        word: "通过",
        detail: note ?? "上游确认当前登录有效（未调用模型）",
        at: stamp,
      },
    };
  }
  if (verify === "rejected" || verify === "needs_login") {
    return {
      state: "ready",
      at,
      data: {
        result: "failed",
        word: "未通过",
        detail: reason ?? note ?? `上游核验结果：${verify}`,
        at: stamp,
      },
    };
  }
  if (op.state === "failed") {
    return {
      state: "ready",
      at,
      data: {
        result: "unknown",
        word: "结果未知",
        detail: failureFromOperationPayload(op, at).reason,
        at: stamp,
      },
    };
  }
  if (op.state === "cancelled") {
    return {
      state: "ready",
      at,
      data: {
        result: "unknown",
        word: "已取消",
        detail: "这次核验被取消，结果未知",
        at: stamp,
      },
    };
  }
  return {
    state: "ready",
    at,
    data: {
      result: "unknown",
      word: verify === null ? "结果未知" : `核验结果：${verify}`,
      detail: note ?? reason ?? "核验没有给出可用结论",
      at: stamp,
    },
  };
}

// ---------------------------------------------------------------- mapping

export function serviceFrom(payload: UmServicesPayload): UmService {
  const entry = payload.services.find((service) => service.key === CHAT2API);
  if (!entry)
    throw new UmReadStatusError(
      "unavailable",
      "后端没有报告 chat2api 服务（UM 主链未装配）"
    );
  return {
    state: SERVICE_STATE[entry.state] ?? "unknown",
    word: SERVICE_WORD[entry.state] ?? "状态未知",
    detail: entry.detail || (entry.healthy === false ? "健康检查未通过" : ""),
    port: entry.port,
    pid: entry.processIds[0] ?? null,
    checkedAt: payload.checkedAt,
  };
}

export function loginFrom(result: OperationResult): UmLogin {
  if (result.state !== "succeeded" || !result.operation?.result) {
    return {
      state: "unknown",
      word: "无法检查",
      detail: `查询 UM 认证状态没有成功：${operationNote(result)}`,
      checkedAt: null,
    };
  }
  const raw = result.operation.result;
  const state = rowText(raw, "state");
  const error = rowText(raw, "error");
  const note = rowText(raw, "note");
  const label = rowText(raw, "label");
  const checked = rowNumber(raw, "checked_epoch");
  const stamp = checked === null ? null : Math.round(checked * 1000);
  const progress = object(raw.login);
  const phase = rowText(progress, "phase");
  const origin = rowText(progress, "origin");
  const manualReason = rowText(progress, "manual_reason");
  const duoWaiting = manualReason === DUO_PROMPT_WAITING_REASON;
  const duoAuto = phase === "awaiting_duo" && !duoWaiting;
  const takeoverDetail = manualReason && Object.hasOwn(MANUAL_LOGIN_DETAILS, manualReason)
    ? MANUAL_LOGIN_DETAILS[manualReason]
    : null;
  const takeover = duoWaiting || takeoverDetail !== null;
  const recovery = object(raw.recovery);
  const retryEpoch = rowNumber(recovery, "next_attempt_epoch");
  const rateLimited = error === RATE_LIMIT_ERROR;
  const verifying = phase === "verifying_login";
  const rejection = rowNumber(raw, "rejected_before_expiry_status");
  const temporarilyUnknown = state === "unknown" && label === "暂时无法核验";
  const nextWatch = rowNumber(raw, "next_watch_epoch");
  const verificationReason = rowText(raw, "last_failure_reason") ?? rowText(raw, "verification_reason");
  const verificationDetails = [
    raw.browser_catalog_valid === true ? "网页登录正常，网关待核验" : null,
    rejection === null || state === "valid"
      ? null
      : `旧凭据尚未到期，已被 UM 拒绝（HTTP ${rejection}）`,
    (verifying || temporarilyUnknown) && verificationReason
      ? `核验原因：${verificationReason}`
      : null,
    temporarilyUnknown && nextWatch !== null ? `下次后台复查：${when(nextWatch * 1000)}` : null,
  ].filter(Boolean).join(" · ");
  const mapped: UmLogin["state"] =
    state === "valid"
      ? "valid"
      : state === "needs_login"
        ? "needs_login"
        : state === "logging_in"
          ? "logging_in"
          : state === "failed"
            ? "invalid"
            : "unknown";
  return {
    state: mapped,
    word: rateLimited
      ? "登录被限流"
      : duoWaiting
        ? DUO_TAKEOVER_WORD
        : duoAuto
          ? DUO_AUTO_WORD
          : mapped === "valid"
            ? LOGIN_WORD.valid
            : verifying
              ? "正在核验登录结果"
            : takeover
              ? "需要本人继续登录"
              : (label ?? LOGIN_WORD[mapped]),
    detail: rateLimited
      ? `${RATE_LIMIT_DETAIL}；可重试时间：${
          retryEpoch === null ? "网关未上报" : when(retryEpoch * 1000)
        }`
      : duoWaiting
        ? DUO_TAKEOVER_DETAIL
        : duoAuto
          ? DUO_AUTO_DETAIL
          : (takeoverDetail ?? error ??
            (verificationDetails || note) ??
            (state === null ? "认证状态无法确认" : `chat2api 内存态：${state}`)),
    checkedAt: stamp,
    phase,
    origin,
    manualReason,
    takeover,
  };
}

export function todayFrom(today: UmTodayPayload): UmToday {
  return {
    day: today.day,
    tokens: today.tokens,
    cacheRead: today.cacheRead,
    reasoning: today.reasoning,
    requests: today.requests,
    speed: today.speed,
  };
}

export function trendFrom(trend: UmTrendPayload): UmTrend {
  return {
    range: trend.range,
    days: trend.days,
    all: { daily: trend.all.daily, total: trend.all.total },
    models: trend.models.map((model) => ({
      model: model.model,
      daily: model.daily,
      total: model.total,
      failed: model.failed,
    })),
    speedBasis: trend.speedBasis,
  };
}

export function maintenanceFrom(
  payload: UmMaintenancePagePayload
): UmMaintenancePage {
  return {
    rows: payload.rows.map((row) => ({
      key: row.key,
      action: row.action as MaintenanceAction,
      state: row.state,
      at: row.at,
      finishedAt: row.finishedAt,
      steps: row.steps,
      result: row.result,
    })),
    scope: payload.scope,
    earlier: payload.earlier,
  };
}

export function accessFrom(result: ReadResult<GatewayConnection>): UmAccess {
  if (result.state !== "ready" || !result.value) {
    const status = result.state === "refused" ? "denied" : "unavailable";
    throw new UmReadStatusError(status, result.reason ?? "连接信息读取失败");
  }
  const connection = result.value;
  const hints: string[] = [
    "在客户端（例如 Magpie）里把上面的地址填成 OpenAI 兼容接口的 Base URL；模型 ID 用下表的 ID。",
  ];
  if (connection.auth === "loopback_no_key") {
    hints.push(
      "本机 8000 当前不要求调用密钥（回环无鉴权）；这是运行实例自己的事实。"
    );
  } else if (connection.auth === "bearer") {
    hints.push("本机 8000 要求调用密钥（Bearer）；本页不显示也不收集密钥值。");
  } else {
    hints.push("是否需要调用密钥尚未确认（未知 ≠ 无鉴权）。");
  }
  if (connection.keyState === "present")
    hints.push("调用密钥来自本机环境变量（值不显示）。");
  else if (connection.keyState === "needs_key_unconfigured")
    hints.push("8000 需要调用密钥，但本机来源缺失或为空。");
  else if (connection.keyState === "unreadable")
    hints.push("调用密钥来源存在，但读不出来。");
  else if (connection.keyState === "absent") hints.push("没有读到调用密钥。");
  else hints.push("调用密钥状态未知。");
  const protocol =
    connection.protocol === "openai-compatible"
      ? `OpenAI 兼容（${connection.protocolPath}）`
      : `${connection.protocol}（${connection.protocolPath}）`;
  return { baseUrl: connection.baseUrl, apiType: protocol, hints };
}

export function thinkingSource(
  source: string | null
): UmModelRow["thinking"]["source"] {
  if (source === "upstream") return "catalog";
  if (source === "known" || source === "probe") return "local";
  return "unknown";
}

function contextState(status: string, requests: number): ModelTestState {
  if (status === "verified") return "done";
  if (status === "failed") return "failed";
  return requests > 0 ? "partial" : "not_run";
}

export function contextEntries(op: { result: JsonObject | null }): TestEntry[] {
  const rows = rowsOf(op.result?.results);
  return rows.flatMap((row) => {
    const key = rowText(row, "key");
    const model =
      rowText(row, "model_id") ??
      (key !== null && key.includes("|")
        ? key.slice(key.indexOf("|") + 1)
        : key);
    if (model === null) return [];
    const status = rowText(row, "status") ?? "unknown";
    const requests = rowCount(row, "requests") ?? 0;
    const note = rowText(row, "note");
    const detail: UmTestDetail = {
      durationMs: missing(
        "操作结果不提供逐模型耗时；模型表的上下文信息来自探测队列的持久证据"
      ),
      firstTokenMs: missing("上下文探测不测首字"),
      outputTokens: missing("操作结果不提供逐模型输出 Token"),
      speed: missing("上下文探测不计速度"),
      speedBasis: "whole_request",
      contextChars: missing(
        "操作结果不提供逐模型字符步进；已刷新模型表的上下文信息"
      ),
      contextTokenLowerBound: missing(
        "操作结果不提供逐模型 Token 下界；已刷新模型表的上下文信息"
      ),
      error: status === "failed" ? (note ?? "上下文探测失败") : null,
      lines: [
        `上下文探测：${status}`,
        `请求 ${requests} 次`,
        ...(note ? [note] : []),
      ],
    };
    return [
      {
        model,
        state: contextState(status, requests),
        summary: `上下文探测：${status}${note ? ` · ${note}` : ""}（请求 ${requests} 次）`,
        detail,
      },
    ];
  });
}

export function resultEntries(result: OperationResult): TestEntry[] {
  if (result.state !== "succeeded" || !result.operation?.result) return [];
  return contextEntries({ result: result.operation.result });
}

export function resultBudget(
  result: OperationResult
): { used: number; limit: number } | null {
  const raw = result.operation?.result;
  const budget = raw ? object(raw.budget) : null;
  if (!budget) return null;
  const used = rowCount(budget, "requests_used");
  const limit = rowCount(budget, "max_requests");
  return used === null || limit === null ? null : { used, limit };
}

export function runNote(result: OperationResult): string {
  if (result.state === "succeeded") {
    const raw = result.operation?.result ?? null;
    return (
      rowText(raw, "summary") ??
      "整批共用一份请求预算；字符步进按字符计，Token 是上游报告的已验证下界，不是最大上下文。"
    );
  }
  if (result.state === "cancelled")
    return "已请求停止；已完成的模型结果保留，之后的模型不再开始。";
  if (result.state === "unconfirmed")
    return `结果未确认：${operationNote(result)}。以操作记录为准，本页不重发。`;
  return `测试没有完成：${operationNote(result)}`;
}

export function catalogFailureKind(status: string | null): FailureKind {
  if (status === "auth_required") return "unauthenticated";
  if (status === "unavailable") return "unavailable";
  return "error";
}

export function progressResult(
  result: OperationResult
): Extract<ActionProgress, { state: "done" }>["result"] {
  if (result.state === "succeeded") return "succeeded";
  if (result.state === "cancelled") return "cancelled";
  if (result.state === "interrupted" || result.state === "unconfirmed")
    return "unconfirmed";
  return "failed";
}

export function upstreamFaultsFrom(
  payload: UmUpstreamFaultsPayload
): UmUpstreamFaults {
  return {
    active: payload.active,
    history: payload.history,
    lastCheck: payload.lastCheck,
    nextCheckAt: payload.nextCheckAt,
  };
}
