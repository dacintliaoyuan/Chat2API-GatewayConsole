/**
 * Shared operation-feedback state for the gateway console workbench (R9-R37).
 *
 * Layers (handoff A37-1 + the maintainer's four-layer design):
 *  - button level: the triggering button flips into an explicit action state;
 *  - operation-area level: the result summary lives next to the action row
 *    that started it (model lane) instead of at the end of long content;
 *  - global level: a fixed notice pool for cross-page completion / failure.
 *    One entry per operation from submit to finish (never one per poll); at
 *    most three visible; success collapses briefly, failures stay until
 *    dismissed and remain reviewable;
 *  - system banner: 8096-unreachable class messages go to a fixed top
 *    banner instead of a toast the user may miss.
 *
 * No second operation ledger: client-side presentation only, keyed by the
 * server operation id. Notice text is whitelisted Chinese summaries extracted
 * from real payloads; secrets and raw server objects never enter notices.
 */

export type FeedbackState =
  | "preparing" | "queued" | "running" | "succeeded"
  | "partial" | "failed" | "cancelled" | "unconfirmed";

export interface FeedbackEntry {
  key: string;              // server operation id (or client-local fallback)
  action: string;           // registry action name
  label: string;            // Chinese action name
  target: string;
  state: FeedbackState;
  text: string;             // current readable summary (Chinese)
  detail: string;           // bounded result summary (may be empty)
  startedAt: number;
  updatedAt: number;
  pinned: boolean;          // failures/pending stay until dismissed
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
    "account.um_status": "上游认证状态",
    "account.um_verify": "上游账号核验",
    "share.start": "开启分享",
    "share.stop": "关闭分享",
    "share.estop": "紧急停止分享",
    "share.retry_stop": "重试关闭分享",
    "share.status": "读取分享状态",
    "share.backend_login": "专用账号登录",
    "share.diagnostics": "分层诊断",
    "share.backend_reauthenticate": "仅重新认证 8110",
    "share.backend_restart": "仅重启 8110",
    "share.gateway_restart": "仅重启 8100",
  };
  return CN_ACTION[action] ?? action;
}

/** Bounded Chinese summary from a finished payload. Epoch noise stripped,
 *  no raw JSON dump: real field values get Chinese names, anything unknown
 *  ends as a plain "已完成" instead of a stringify dump. */
export function summarizeResult(result: Record<string, unknown>): string {
  const status = typeof result.status === "string" ? result.status : "";
  const applied = Array.isArray(result.applied) ? (result.applied as unknown[]).length : null;
  const pending = Array.isArray(result.pending) ? (result.pending as unknown[]).length : null;
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

/** True while the entry should occupy a visible notice slot. */
export function isVisibleState(entry: FeedbackEntry): boolean {
  return entry.state === "preparing" || entry.state === "queued"
    || entry.state === "running" || entry.state === "unconfirmed"
    || entry.pinned;
}
