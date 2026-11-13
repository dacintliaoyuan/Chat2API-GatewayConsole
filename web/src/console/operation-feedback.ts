/**
 * Shared operation-feedback state for the gateway consoles (R9-R37).
 *
 * Layers (handoff A37-1 + the maintainer's four-layer design):
 *  - button level: the triggering button flips into an explicit action state;
 *  - operation-area level: the result summary lives next to the action row
 *    that started it (model lane) instead of at the end of long content;
 *  - global level: a fixed notice pool for cross-page completion / failure.
 *    One entry per operation from submit to finish (never one per poll); at
 *    most three visible; success collapses briefly, failures stay until
 *    dismissed and remain reviewable;
 *  - system banner: 8001-unreachable class messages go to a fixed top
 *    banner instead of a toast the user may miss.
 *
 * No second operation ledger: client-side presentation only, keyed by the
 * server operation id. Notice text is whitelisted Chinese summaries extracted
 * from real payloads; secrets and raw server objects never enter notices.
 */

export type FeedbackState =
  | "preparing"
  | "queued"
  | "running"
  | "succeeded"
  | "partial"
  | "failed"
  | "cancelled"
  | "unconfirmed";

export interface FeedbackEntry {
  key: string; // server operation id (or client-local fallback)
  action: string; // registry action name
  label: string; // Chinese action name
  target: string;
  state: FeedbackState;
  text: string; // current readable summary (Chinese)
  detail: string; // bounded result summary (may be empty)
  startedAt: number;
  updatedAt: number;
  pinned: boolean; // failures/pending stay until dismissed
}

export type FeedbackListener = (entries: FeedbackEntry[]) => void;

const CN_ACTION: Record<string, string> = {
  "model.sync_preview": "同步预览",
  "model.sync_apply": "应用同步",
  "model.catalog": "刷新目录",
  "model.task_status": "同步任务状态",
  "model.sync_pending": "同步概览",
  "service.start": "启动服务",
  "service.stop": "停止服务",
  "service.restart": "重启服务",
};

const STATE_LABEL: Record<FeedbackState, string> = {
  preparing: "准备提交",
  queued: "已排队",
  running: "执行中",
  succeeded: "已完成",
  partial: "部分完成",
  failed: "失败",
  cancelled: "已取消",
  unconfirmed: "结果待确认",
};

const STATE_DOT: Record<FeedbackState, "run" | "watch" | "down"> = {
  preparing: "watch",
  queued: "watch",
  running: "watch",
  succeeded: "run",
  partial: "watch",
  failed: "down",
  cancelled: "watch",
  unconfirmed: "watch",
};

export function stateText(state: FeedbackState): string {
  return STATE_LABEL[state] ?? state;
}

export function stateDot(state: FeedbackState): string {
  return STATE_DOT[state] ?? "watch";
}

/** Chinese name for a registry action; falls back to the raw action id. */
export function actionLabel(action: string): string {
  const CN_ACTION: Record<string, string> = {
    "model.sync_preview": "同步预览",
    "model.sync_apply": "应用同步",
    "model.catalog": "刷新目录",
    "model.task_status": "同步任务状态",
    "service.start": "启动服务",
    "service.stop": "停止服务",
    "service.restart": "重启服务",
    "model.sync_history": "同步历史",
    "model.sync_pending": "同步概览",
    "model.speedtest": "模型测速",
    "model.context_probe_preview": "探测队列预览",
    "model.context_probe_run": "执行上下文探测",
    "model.context_overrides": "读取上下文设置",
    "model.context_override_set": "设置上下文覆盖",
    "model.context_override_clear": "恢复默认上下文",
    "model.settings_save": "保存设置草稿",
    "model.settings_preview": "预览模型设置",
    "model.settings_apply": "应用模型设置",
    "model.task_install": "安装同步任务",
    "model.task_disable": "禁用同步任务",
    "account.login": "账号登录",
    "account.um_status": "UM 认证状态",
    "account.um_verify": "核验登录是否有效",
    "cliproxy.status": "Gemini 登录查询",
    "qoder.status": "Qoder 登录状态",
    "qoder.models": "Qoder 模型目录",
    "qoder.quota_read": "Qoder 积分额度",
    "qoder.poc": "Qoder 连通性验证",
    "qoder.credential_bridge": "Qoder 凭据桥接",
    "qoder.source_diagnostics": "Qoder 凭据来源诊断",
    "qoderglobal.login": "国际版登录",
    "qoderglobal.status": "国际版登录状态",
    "qoderglobal.models": "国际版模型目录",
    "qoderglobal.quota": "国际版额度",
    "m365.supply_status": "M365 凭据供给状态",
    "m365.candidate_status": "M365 候选状态",
    "m365.candidate_verify": "M365 候选验证",
    "m365.mapping_publish": "M365 映射发布",
    "m365.settings_save": "M365 设置保存",
    "m365.speedtest_key_setup": "创建测速密钥",
    "share.start": "开启分享",
    "share.stop": "关闭分享",
    "share.estop": "紧急停止分享",
    "share.retry_stop": "重试关闭分享",
    "share.status": "读取分享状态",
    "share.backend_login": "启动 / 按需登录 8110",
    "share.diagnostics": "分层诊断",
    "share.repair_limits": "修复隔离规则",
    "share.backend_reauthenticate": "仅重新认证 8110",
    "share.backend_restart": "仅重启运行中的 8110",
    "share.gateway_restart": "仅重启 8100",
    "share.public_check": "公网链路复查",
    "share.public_retry": "公网链路受限重试",
  };
  return CN_ACTION[action] ?? action;
}

/** Bounded Chinese summary from a finished payload. Epoch noise stripped,
 *  no raw JSON dump: real field values get Chinese names, anything unknown
 *  ends as a plain "已完成" instead of a stringify dump. */
export function summarizeResult(result: Record<string, unknown>): string {
  if (
    result.final_state === "valid" &&
    typeof result.login_skipped === "boolean"
  ) {
    return result.login_skipped
      ? "认证有效，无需重新登录"
      : "登录完成，认证有效";
  }
  const status = typeof result.status === "string" ? result.status : "";
  const applied = Array.isArray(result.applied)
    ? (result.applied as unknown[]).length
    : null;
  const pending = Array.isArray(result.pending)
    ? (result.pending as unknown[]).length
    : null;
  if (status === "pending" && (applied !== null || pending !== null)) {
    return `已应用 ${applied} 项，${pending} 项待客户端退出后应用`;
  }
  const fields: string[] = [];
  if (typeof result.state === "string") fields.push(`状态 ${result.state}`);
  if (result.installed === true) fields.push("计划任务已安装");
  if (typeof result.quota === "string" || typeof result.quota === "number") {
    fields.push(`额度 ${String(result.quota).slice(0, 30)}`);
  }
  if (status) fields.push(`运行状态 ${status}`);
  if (applied !== null && applied > 0) fields.push(`已应用 ${applied} 项`);
  return fields.length ? fields.join("；") : "已完成";
}

/** Recovery follows the failed layer; a broker failure must not send the user
 *  through another 8110 login before the restricted control channel is usable. */
export function shareRecoveryHint(action: string, code: string): string {
  if (!action.startsWith("share.")) return "";
  if (
    [
      "share_broker_unwired",
      "share_broker_unavailable",
      "broker_unavailable",
      "deadline_exceeded",
    ].includes(code)
  ) {
    return "先恢复受限后台：在控制台目录运行 Recover-ShareControl.bat，按 Windows 管理员提示恢复后重新读取状态与分层状态；再按需登录 8110、开启分享";
  }
  return action === "share.backend_restart" &&
    code === "share_backend_not_running"
    ? "请点“启动 / 按需登录 8110”，检查认证并在需要时启动登录"
    : "";
}

/** True while the entry should occupy a visible notice slot. */
export function isVisibleState(entry: FeedbackEntry): boolean {
  return (
    entry.state === "preparing" ||
    entry.state === "queued" ||
    entry.state === "running" ||
    entry.state === "unconfirmed" ||
    entry.pinned
  );
}
