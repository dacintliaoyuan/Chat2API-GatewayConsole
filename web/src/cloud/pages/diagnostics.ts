import { escapeHtml } from "../../html";
import { fmtNum } from "../../console/ui";
import type {
  UmLoginRoundPayload,
  UmLoginRoundsPagePayload,
} from "../../console/backend/types";
import { FAILURE_WORD, lastValue, type ReadState } from "../cloud-reads";
import { fullWhen } from "../cloud-format";
import type { CloudModel } from "../model";

/** #116: 请求与诊断 → 运行日志. The cloud console shows the durable UM
 *  login-round history the 8003 gateway writes. The read is read-only and the
 *  page never probes, logs in or appends a record. */

const ROUND_STATE: Readonly<
  Record<"running" | "completed" | "incomplete", { word: string; tone: string }>
> = {
  running: { word: "进行中", tone: "neutral" },
  completed: { word: "已完成", tone: "ok" },
  incomplete: { word: "未完成", tone: "warn" },
};
const TRIGGER: Readonly<Record<string, string>> = {
  business_request: "业务请求",
  auth_watch: "后台巡检",
  explicit: "控制台显式",
};
const OUTCOME: Readonly<Record<string, string>> = {
  restored: "已恢复",
  verification_unknown: "核验未知",
  rejected: "上游拒绝",
  manual_takeover: "需人工接管",
  unavailable: "上游不可用",
  terminal: "终止",
  cancelled: "已取消",
  transient: "暂时失败",
  busy: "占用中",
  token_uncommitted: "令牌未提交",
};
const EVIDENCE: Readonly<Record<string, string>> = {
  valid: "有效",
  unknown: "未知",
  rejected: "拒绝",
};

function loginRoundRow(row: UmLoginRoundPayload): string {
  const state = ROUND_STATE[row.state];
  const outcome =
    row.outcome === null ? null : (OUTCOME[row.outcome] ?? row.outcome);
  const evidence =
    row.evidenceVerdict === null
      ? null
      : (EVIDENCE[row.evidenceVerdict] ?? row.evidenceVerdict);
  const upstream =
    row.upstreamStatus === null
      ? "无 HTTP 状态"
      : `HTTP ${row.upstreamStatus}`;
  return `<tr data-cloud-login-rounds-row="${escapeHtml(row.key)}" data-cloud-login-round-state="${row.state}">
    <td class="mono" title="运行 ${escapeHtml(row.runId)}"><span class="tnum">${escapeHtml(fullWhen(row.startedAt))}</span></td>
    <td class="num">${fmtNum(row.round)}</td>
    <td><span class="status" data-tone="${state.tone}"><span class="dot" aria-hidden="true"></span>${state.word}</span></td>
    <td>${row.visible ? "可见窗口" : "后台"}</td>
    <td>${escapeHtml(TRIGGER[row.triggerSource ?? ""] ?? row.triggerSource ?? "未记录")}</td>
    <td class="num">${fmtNum(row.submitCount)}</td>
    <td>${outcome === null ? '<span class="cloud-key-mask">尚未结束</span>' : escapeHtml(outcome)}</td>
    <td>${row.reason === null ? "—" : escapeHtml(row.reason)}</td>
    <td>${escapeHtml(upstream)}${evidence === null ? "" : ` · ${escapeHtml(evidence)}`}</td>
  </tr>`;
}

/** The one source this page has: UM 登录轮次. A failed read is shown as a
 *  failure, never folded into an empty list; a missing file is the backend's
 *  legal empty history. */
export function loginRoundsHtml(model: CloudModel): string {
  const read: ReadState<UmLoginRoundsPagePayload> | null = model.loginRounds;
  const ui = model.loginRoundsUi;
  const loading = read === null || read.state === "loading";
  const stale = read?.state === "failed" ? lastValue(read) : null;
  const loaded =
    read?.state === "ready"
      ? read.value
      : read?.state === "failed"
        ? (stale?.value ?? null)
        : null;
  const rows = loaded?.rounds ?? [];
  const meta = loading
    ? "读取中"
    : read?.state === "ready"
      ? `本页 ${fmtNum(rows.length)} 轮`
      : stale
        ? `上次读数 ${fmtNum(rows.length)} 轮（已过时）`
        : "读取失败";
  const filters = `<form class="cloud-filters" data-cloud-login-rounds-form role="search" aria-label="筛选 UM 登录轮次">
    <label class="cloud-field-inline"><span>轮次</span><input name="round" type="search" inputmode="numeric" data-cloud-login-rounds-round placeholder="按轮次查找" value="${escapeHtml(ui.round)}"${ui.loading ? " disabled" : ""}></label>
    <label class="cloud-field-inline"><span>原因或结果</span><input name="q" type="search" maxlength="120" data-cloud-login-rounds-q placeholder="按原因或结果查找" value="${escapeHtml(ui.q)}"${ui.loading ? " disabled" : ""}></label>
    <button type="submit" class="btn btn-outline btn-sm"${ui.loading ? " disabled" : ""}>筛选</button>
    <button type="button" class="btn btn-outline btn-sm" data-cloud-login-rounds-retry${ui.loading ? " disabled" : ""}>重新读取</button>
  </form>`;
  const failure =
    read?.state === "failed"
      ? `<p class="cloud-read-note" data-tone="warn">UM 登录轮次${FAILURE_WORD[read.kind]}：${escapeHtml(read.reason)}${stale ? `；下方是 ${escapeHtml(fullWhen(stale.at))} 的上次读数（已过时）` : ""}</p>`
      : "";
  let body: string;
  if (loading && !stale)
    body = `<p class="cloud-empty" data-cloud-login-rounds-loading>UM 登录轮次读取中…</p>`;
  else if (read?.state === "failed" && !stale)
    body = `<p class="cloud-empty">UM 登录轮次读取失败，当前无法判断是否有登录轮次；不用空结果代替。</p>`;
  else if (!rows.length)
    body = `<p class="cloud-empty">保留范围内没有符合条件的登录轮次。</p>`;
  else
    body = `<div class="cloud-table-scroll"><table class="data-table cloud-login-rounds-table">
      <caption>UM 登录轮次（本页 ${fmtNum(rows.length)} 轮，按开始时间从新到旧）</caption>
      <thead><tr><th>开始（北京时间）</th><th class="num">轮次</th><th>状态</th><th>模式</th><th>触发来源</th><th class="num">提交</th><th>结果</th><th>原因</th><th>上游 / 证据</th></tr></thead>
      <tbody>${rows.map(loginRoundRow).join("")}</tbody>
    </table></div>`;
  const scope =
    loaded?.scope && loaded.scope.trim()
      ? `<p class="cloud-scope-note">${escapeHtml(loaded.scope)}</p>`
      : "";
  const truncated =
    loaded?.truncated === true
      ? `<p class="cloud-scope-note" data-tone="warn">已达单页上限；请按轮次或原因缩小范围。</p>`
      : "";
  return `<section class="card cloud-login-rounds" data-cloud-diagnostics aria-label="UM 登录轮次">
    <div class="card-head"><h3>运行日志 · UM 登录轮次</h3><span class="card-meta">${meta}</span></div>
    <p class="cloud-scope-note">来源：UM 登录轮次（云端 8003 写入；只读，不触发探测或登录）。</p>
    ${filters}
    ${failure}
    ${body}
    ${scope}
    ${truncated}
  </section>`;
}

/** The 请求与诊断 area: the run-log page with its one login-round source. */
export function diagnosticsHtml(model: CloudModel): string {
  return loginRoundsHtml(model);
}
