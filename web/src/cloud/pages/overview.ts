import { escapeHtml } from "../../html";
import {
  lastValue,
  type OperationRow,
  type ReadState,
} from "../cloud-reads";
import type { CloudModel } from "../model";
import { apiUrlHtml, statusRowHtml } from "./status";
import { keysCard } from "./keys";
import { rankCard, trendCard, usageKpis } from "./usage";
import { requestsCard } from "./requests";

/** Ticket #6: the page's one primary action. Its word follows the service
 *  read's `gateway_running`; a start in flight keeps the button in place with
 *  the operation's own stage note, and only a completed start lets the word
 *  become 停止. An unreadable status is unknown, never a guessed 启动. */
export const PRIMARY_START = "share.chain_start";
export const PRIMARY_STOP = "share.chain_stop";
export const PRIMARY_FORCE = "share.chain_estop";

const TERMINAL_OP = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
]);

export type PrimaryAction = {
  kind: "start" | "stop" | "starting";
  word: string;
  /** The chain start's own latest step note while it is in flight. */
  stage: string | null;
  action: typeof PRIMARY_START | typeof PRIMARY_STOP | null;
  disabled: boolean;
  reason: string;
  /** Ticket #6: only a drain-timed-out stop offers 强制停止. */
  force: boolean;
};

function latestChainStart(
  operations: ReadState<OperationRow[]>
): OperationRow | null {
  const read = lastValue(operations);
  // A stale read is history, never current in-flight evidence.
  if (!read || read.stale) return null;
  return (
    read.value
      .filter((row) => row.action === PRIMARY_START)
      .sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))[0] ?? null
  );
}

/** The chain start's own latest step note; the stage text is never invented. */
export function stageNote(
  operation: OperationRow | null | undefined
): string | null {
  const steps = operation?.steps ?? [];
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const note = steps[index]?.note;
    if (typeof note === "string" && note.trim()) return note;
  }
  return null;
}

/** The latest chain stop ended in a drain timeout and no forced stop followed:
 *  only then may the stop dialog offer 强制停止. */
function stopDrainTimedOut(operations: ReadState<OperationRow[]>): boolean {
  const read = lastValue(operations);
  if (!read || read.stale) return false;
  const latest = read.value
    .filter(
      (row) => row.action === PRIMARY_STOP || row.action === PRIMARY_FORCE
    )
    .sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))[0];
  if (!latest || latest.action !== PRIMARY_STOP) return false;
  if (latest.state !== "succeeded" || !latest.result) return false;
  return latest.result.state === "drain_timeout";
}

export function primaryAction(model: CloudModel): PrimaryAction {
  const statusRead = lastValue(model.status);
  const current = statusRead && !statusRead.stale ? statusRead.value : null;
  const pending = model.primaryPending ?? null;
  const inFlight = latestChainStart(model.operations);
  const starting =
    inFlight && !TERMINAL_OP.has(inFlight.state) ? inFlight : null;
  const running =
    current && typeof current.gateway_running === "boolean"
      ? current.gateway_running
      : null;
  // A drain timeout already stopped the gateway but left in-flight requests
  // unconfirmed. The primary action must stay 停止 so its confirm can offer
  // 强制停止 even though `gateway_running` has already flipped to false.
  const force = stopDrainTimedOut(model.operations);
  // A current status read that confirms the service is running is completion
  // evidence, so an older start row still marked running cannot keep 启动中.
  if (running === true) {
    const ready = model.writeReady();
    return {
      kind: "stop",
      word: "停止",
      stage: null,
      action: PRIMARY_STOP,
      disabled: !ready.ok,
      reason: ready.ok ? "" : ready.reason,
      force,
    };
  }
  if (starting || pending)
    return {
      kind: "starting",
      word: "启动中…（等待认证）",
      stage: stageNote(starting) ?? pending?.stage ?? "已提交，等待后端受理",
      action: null,
      disabled: true,
      reason: "启动已受理，等待后端完成；动作受理不是认证成功",
      force: false,
    };
  if (force) {
    const ready = model.writeReady();
    return {
      kind: "stop",
      word: "停止",
      stage: null,
      action: PRIMARY_STOP,
      disabled: !ready.ok,
      reason: ready.ok ? "" : ready.reason,
      force: true,
    };
  }
  if (running === null)
    return {
      kind: "start",
      word: "启动",
      stage: null,
      action: null,
      disabled: true,
      reason: "服务状态未知，无法判断启动还是停止；请刷新后重试",
      force: false,
    };
  const ready = model.writeReady();
  return {
    kind: "start",
    word: "启动",
    stage: null,
    action: PRIMARY_START,
    disabled: !ready.ok,
    reason: ready.ok ? "" : ready.reason,
    force: false,
  };
}

export function primaryActionHtml(model: CloudModel): string {
  const action = primaryAction(model);
  const button = `<button type="button" class="btn ${action.kind === "stop" ? "btn-danger" : "btn-primary"} cloud-primary-button" data-cloud-primary="${action.kind === "starting" ? "starting" : action.kind}"${action.disabled ? ` disabled title="${escapeHtml(action.reason)}"` : ""}>${escapeHtml(action.word)}</button>`;
  const stage = action.stage
    ? `<p class="cloud-primary-stage" data-cloud-primary-stage>${escapeHtml(action.stage)}</p>`
    : "";
  const reason =
    action.disabled && !action.stage && action.reason
      ? `<p class="cloud-primary-note">${escapeHtml(action.reason)}</p>`
      : "";
  return `<section class="cloud-primary-action" aria-label="服务控制">${button}${stage}${reason}</section>`;
}

/** Ticket #4: the services page and its server actions are gone. The only
 *  remaining server actions are the two read-only ones, kept in one fold until
 *  the state row owns them. */
function advancedFold(model: CloudModel): string {
  return `<details class="cloud-advanced" data-cloud-advanced data-cloud-evidence>
    <summary>高级维护（分层检查与重新认证）</summary>
    <div class="cloud-actions-row">
      ${model.actionButton("share.status", "share", "分层检查（只读）", "btn btn-outline btn-sm")}
      ${model.actionButton("share.backend_reauthenticate", "share", "重新认证 UM…", "btn btn-tonal btn-sm")}
    </div>
  </details>`;
}

export function overviewHtml(model: CloudModel): string {
  // Ticket #4: one page. The old hero, issue list and layered evidence fold
  // were the services page's content and are no longer rendered. The order is
  // the spec's own (spec.md:92): 状态行、主动作按钮、Key 列表、今日用量数字、
  // 趋势、请求明细、高级折叠。
  return `${statusRowHtml(model)}
    ${apiUrlHtml(model)}
    ${primaryActionHtml(model)}
    ${keysCard(model)}
    ${usageKpis(model)}
    <div class="cloud-trend-grid">${trendCard(model)}${rankCard(model)}</div>
    ${requestsCard(model)}
    ${advancedFold(model)}`;
}
