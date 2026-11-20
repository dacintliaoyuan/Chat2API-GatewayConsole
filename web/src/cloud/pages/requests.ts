import { escapeHtml } from "../../html";
import { fmtNum } from "../../console/ui";
import type { ShareUsageRequestRow } from "../../console/backend/types";
import {
  FAILURE_WORD,
  lastValue,
  readShareUsageRequests,
} from "../cloud-reads";
import { clock, when } from "../cloud-format";
import type { CloudModel, RequestsTimeRange } from "../model";

const REQUESTS_WINDOW: Readonly<Record<RequestsTimeRange, string>> = {
  // "today" is the backend's Beijing calendar day, not a rolling 24h window.
  today: "today",
  "7": "7d",
  "30": "30d",
};

const REQUEST_RESULT_WORD: Readonly<Record<string, string>> = {
  success: "成功",
  failed: "失败",
  cancelled: "已取消",
  unknown: "结果未确认",
};

/** Ticket #9: the ledger records `duration_ms` per request; an old row without it
 *  stays unknown instead of inventing a number. Token is input + output when both
 *  are recorded, otherwise unknown. */
function requestTokenText(row: ShareUsageRequestRow): string {
  if (
    typeof row.inputTokens !== "number" ||
    typeof row.outputTokens !== "number"
  )
    return "未知";
  return fmtNum(row.inputTokens + row.outputTokens);
}

/** Ticket #9: the request-detail list with its result/time filters and
 *  cursor-backed pages. Every count on this card is computed from the loaded page
 *  only; it never claims to be the whole ledger. A revoked or deleted Key keeps
 *  its historical name with the「（已删除）」suffix. */
export function requestsCard(model: CloudModel): string {
  const read = model.usageRequests;
  const ui = model.usageRequestsUi;
  const loading = read === null || read.state === "loading";
  const stale = read?.state === "failed" ? lastValue(read) : null;
  const loaded =
    read?.state === "ready"
      ? read.value
      : read?.state === "failed"
        ? (stale?.value ?? null)
        : null;
  const rows = loaded?.rows ?? [];
  const meta = loading
    ? "读取中"
    : read.state === "ready"
      ? `本页 ${fmtNum(rows.length)} 条`
      : stale
        ? `上次读数 ${fmtNum(rows.length)} 条（已过时）`
        : "读取失败";
  const resultButtons = (
    [
      ["all", "全部"],
      ["success", "成功"],
      ["failed", "失败"],
      ["unknown", "结果未确认"],
      ["cancelled", "已取消"],
    ] as const
  )
    .map(([id, word]) => {
      const active = id === "all" ? ui.result === "" : ui.result === id;
      return `<button type="button" data-cloud-requests-result="${id}" aria-pressed="${active}">${word}</button>`;
    })
    .join("");
  const windowButtons = (
    [
      ["today", "今天"],
      ["7", "近 7 天"],
      ["30", "近 30 天"],
    ] as const
  )
    .map(
      ([id, word]) =>
        `<button type="button" data-cloud-requests-window="${id}" aria-pressed="${ui.time === id}">${word}</button>`
    )
    .join("");
  const counts = { success: 0, failed: 0, unknown: 0, cancelled: 0 };
  for (const row of rows) counts[row.result] += 1;
  const failure =
    read?.state === "failed"
      ? `<p class="cloud-read-note" data-tone="warn">请求明细${FAILURE_WORD[read.kind]}：${escapeHtml(read.reason)}${stale ? `；下方是 ${clock(stale.at)} 的上次读数（已过时）` : ""}</p>`
      : "";
  const table = loading
    ? `<p class="cloud-empty" data-cloud-requests-loading>请求明细读取中…</p>`
    : read?.state === "failed" && !stale
      ? `<p class="cloud-empty">请求明细读取失败，当前无法判断是否有请求；不用空结果代替。</p>`
      : rows.length
        ? `<div class="cloud-table-scroll"><table class="data-table cloud-request-table">
        <caption>请求明细（本页 ${fmtNum(rows.length)} 条，按账本从新到旧）</caption>
        <thead><tr><th>时间</th><th>Key</th><th>模型</th><th>结果</th><th>耗时</th><th>Token</th><th>失败原因</th></tr></thead>
        <tbody>${rows
          .map((row) => {
            const name = row.keyName ?? row.keyId ?? "未归属";
            const key = `${escapeHtml(name)}${row.deleted === true ? "（已删除）" : ""}`;
            const result = REQUEST_RESULT_WORD[row.result] ?? "结果未确认";
            const reason =
              row.status ?? (row.result === "failed" ? "后端未给出原因" : "—");
            const duration =
              typeof row.durationMs === "number"
                ? `${fmtNum(row.durationMs)} ms`
                : "未上报";
            const timeMs = row.time ? Date.parse(row.time) : NaN;
            const timeText = Number.isFinite(timeMs)
              ? when(timeMs)
              : (row.time ?? "日期未知");
            return `<tr data-cloud-request-row="${escapeHtml(row.requestId ?? "unknown")}" data-cloud-request-result="${row.result}">
          <td class="mono">${escapeHtml(timeText)}</td>
          <td>${key}</td>
          <td>${escapeHtml(row.model ?? "未知")}</td>
          <td>${result}${row.status ? `<span class="cloud-key-mask">${escapeHtml(row.status)}</span>` : ""}</td>
          <td class="mono">${duration}</td>
          <td>Token ${requestTokenText(row)}</td>
          <td>${escapeHtml(reason)}</td>
        </tr>`;
          })
          .join("")}</tbody></table></div>`
      : `<p class="cloud-empty">当前筛选没有请求；计数只覆盖已加载页。</p>`;
  const pageIndex = ui.page;
  const nextCursor = loaded?.nextCursor ?? null;
  const hasPage = loaded !== null;
  const pager = `<div class="cloud-pager">
      <button type="button" class="btn btn-outline btn-sm" data-cloud-requests-page="-1"${pageIndex <= 0 ? " disabled" : ""}>上一页</button>
      <span>第 ${fmtNum(pageIndex + 1)} 页 · 本页 ${fmtNum(rows.length)} 条</span>
      <button type="button" class="btn btn-outline btn-sm" data-cloud-requests-page="1"${nextCursor ? "" : " disabled"}>下一页</button>
    </div>`;
  const retry =
    read?.state === "failed"
      ? `<div class="cloud-pager"><button type="button" class="btn btn-outline btn-sm" data-cloud-requests-retry>重试</button></div>`
      : "";
  return `<section class="card cloud-requests" data-cloud-requests aria-label="请求明细">
    <div class="card-head"><h3>请求明细</h3><span class="card-meta">${meta}</span></div>
    <div class="cloud-filters" role="group" aria-label="请求筛选">
      <div class="cloud-seg" role="group" aria-label="结果筛选">${resultButtons}</div>
      <div class="cloud-seg" role="group" aria-label="时间范围">${windowButtons}</div>
    </div>
    ${failure}
    ${table}
    ${hasPage ? `<p class="cloud-scope-note">本页：成功 ${fmtNum(counts.success)} · 失败 ${fmtNum(counts.failed)} · 结果未确认 ${fmtNum(counts.unknown)} · 已取消 ${fmtNum(counts.cancelled)}。计数只统计已加载页（${fmtNum(rows.length)} 条），不是全部历史。</p>` : ""}
    ${hasPage ? pager : ""}
    ${retry}
    ${loaded?.truncated === true ? `<p class="cloud-scope-note" data-tone="warn">读取窗口已截断，更旧记录未读入；翻页只覆盖已读窗口。</p>` : ""}
    <p class="card-foot">请求明细来自分享账本，按账本从新到旧分页；结果未确认表示账本没有记录结果，不并入成功。耗时来自账本的 duration_ms；旧记录没有该字段时显示「未上报」，不补 0。</p>
  </section>`;
}

export type RequestsPageContext = {
  model: CloudModel;
  render: () => void;
};

export function createRequestsPage(ctx: RequestsPageContext) {
  const { model, render } = ctx;

  let requestsSeq = 0;
  async function loadRequestsPage(page: number, reset = false): Promise<void> {
    const ui = model.usageRequestsUi;
    if (reset) {
      ui.cursors = [null];
      ui.page = 0;
      page = 0;
    }
    const index = Math.max(0, page);
    if (index > 0 && !ui.cursors[index]) return;
    const seq = ++requestsSeq;
    ui.loading = true;
    model.usageRequests = { state: "loading" };
    render();
    const params: {
      window: string;
      result?: string;
      cursor?: string;
      limit: number;
    } = {
      window: REQUESTS_WINDOW[ui.time],
      limit: 50,
    };
    if (ui.result) params.result = ui.result;
    const cursor = ui.cursors[index];
    if (cursor) params.cursor = cursor;
    const read = await readShareUsageRequests(
      model.usageRequests ?? { state: "loading" },
      params
    );
    if (seq !== requestsSeq) return;
    model.usageRequests = read;
    ui.loading = false;
    if (read.state === "ready") {
      ui.page = index;
      ui.cursors = ui.cursors.slice(0, index + 1);
      if (read.value.nextCursor) ui.cursors[index + 1] = read.value.nextCursor;
    }
    render();
  }

  /** Drop any in-flight page answer so it cannot repaint a reset filter. */
  function invalidate(): void {
    requestsSeq += 1;
  }

  return { loadRequestsPage, invalidate };
}
