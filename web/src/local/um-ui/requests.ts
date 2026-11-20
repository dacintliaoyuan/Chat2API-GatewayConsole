import type {
  Failure,
  Load,
  LogLevel,
  LogQuery,
  RequestResult,
  RequestsView,
  TimeWindow,
  UmLogLine,
  UmMaintenancePage,
  UmMaintenanceRow,
  UmRequestRow,
  UmRequests,
} from "./contract";
import { ACTION_LABEL } from "./overview";
import {
  clock,
  dayKey,
  duration,
  esc,
  freshnessHtml,
  full,
  fullWhen,
  loaded,
  placeholderHtml,
  readingHtml,
  speed,
  speedBasisWord,
  tokens,
  when,
  wordOf,
} from "./format";

/** 请求与诊断: request records first (newest first, filters by time, model and
 *  real result, plus search), then run logs and the maintenance record. No
 *  trend chart here; maintenance is kept apart from request counts. */

export type RequestsUi = {
  view: RequestsView;
  search: string;
  expanded: string | null;
  openSteps: ReadonlySet<string>;
  /** The row whose linked logs were asked for and have not arrived yet. */
  detailAsked: string | null;
  /** Per view: the 「加载更早」 cursor sent and not answered yet. */
  earlier: Readonly<Record<RequestsView, string | null>>;
  /** Per view: the cursor whose read came back failed, and why. */
  earlierFailed: Readonly<
    Record<RequestsView, { cursor: string; reason: string } | null>
  >;
};

export const VIEWS: readonly { id: RequestsView; label: string }[] = [
  { id: "records", label: "请求记录" },
  { id: "logs", label: "运行日志" },
  { id: "maintenance", label: "维护操作" },
];
const WINDOWS: readonly { id: TimeWindow; label: string }[] = [
  { id: "today", label: "今天" },
  { id: "24h", label: "24 小时" },
  { id: "7d", label: "7 天" },
  { id: "30d", label: "30 天" },
];
export const RESULT: Readonly<
  Record<RequestResult, { word: string; tone: string }>
> = {
  success: { word: "成功", tone: "ok" },
  failed: { word: "失败", tone: "err" },
  truncated: { word: "截断", tone: "err" },
  client_disconnected: { word: "客户端断开", tone: "warn" },
  cancelled: { word: "已取消", tone: "neutral" },
  unknown: { word: "未知", tone: "neutral" },
};
const LEVEL: Readonly<Record<LogLevel, { word: string; tone: string }>> = {
  error: { word: "错误", tone: "err" },
  warn: { word: "警告", tone: "warn" },
  info: { word: "信息", tone: "neutral" },
  debug: { word: "调试", tone: "neutral" },
  unknown: { word: "未分级", tone: "neutral" },
};
const MAINT_STATE: Readonly<
  Record<UmMaintenanceRow["state"], { word: string; tone: string }>
> = {
  queued: { word: "排队中", tone: "neutral" },
  running: { word: "执行中", tone: "neutral" },
  succeeded: { word: "成功", tone: "ok" },
  failed: { word: "失败", tone: "err" },
  cancelled: { word: "已取消", tone: "neutral" },
  interrupted: { word: "已中断", tone: "warn" },
};
const STATE_UNKNOWN = { word: "状态未知", tone: "neutral" } as const;

function windowSeg(
  current: TimeWindow,
  attr: string,
  disabled: boolean
): string {
  return `<div class="um-seg" role="group" aria-label="时间范围">${WINDOWS.map((w) => `<button type="button" ${attr}="${w.id}" aria-pressed="${w.id === current}"${disabled ? " disabled" : ""}>${esc(w.label)}</button>`).join("")}</div>`;
}
/** 「加载更早」 of one view. Closed while its own read is out or the page is
 *  refreshing; after a failed read the kept rows stay and it offers a retry. */
function moreButton(
  view: RequestsView,
  cursor: string | null,
  attr: string,
  page: { refreshing: boolean; stale: Failure | null },
  ui: RequestsUi
): string {
  if (cursor === null) return "";
  const asked = ui.earlier[view] === cursor;
  const failed =
    !asked && page.stale !== null && ui.earlierFailed[view]?.cursor === cursor
      ? ui.earlierFailed[view]
      : null;
  const busy = asked || page.refreshing;
  const label = asked
    ? "正在读取更早的记录…"
    : page.refreshing
      ? "正在刷新…"
      : failed
        ? "重试加载更早的记录"
        : "加载更早的记录";
  const note = failed
    ? `<p class="um-note" role="status">读取更早的记录没有成功（${esc(failed.reason)}）；已显示的记录保留。</p>`
    : "";
  return `<div class="um-more">${note}<button type="button" class="btn btn-sm btn-outline" ${attr}="${esc(cursor)}"${busy ? " disabled" : ""}>${label}</button></div>`;
}

function recordsHtml(r: UmRequests, ui: RequestsUi): string {
  const q = r.query;
  const wired = r.page.state !== "unwired";
  const filters = `<div class="um-filters" role="search" aria-label="筛选请求">
    ${windowSeg(q.window, "data-um-rwin", !wired)}
    <label class="um-select"><span class="sr-only">模型</span><select data-um-rmodel${wired ? "" : " disabled"}><option value="">全部模型</option>${r.models.map((m) => `<option value="${esc(m)}"${m === q.model ? " selected" : ""}>${esc(m)}</option>`).join("")}</select></label>
    <label class="um-select"><span class="sr-only">结果</span><select data-um-rresult${wired ? "" : " disabled"}><option value="all">全部结果</option>${(Object.keys(RESULT) as RequestResult[]).map((k) => `<option value="${k}"${k === q.result ? " selected" : ""}>${RESULT[k].word}</option>`).join("")}</select></label>
    <label class="um-search"><span class="sr-only">搜索</span><svg class="shell-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/></svg><input type="search" data-um-rsearch placeholder="搜索请求 ID、模型或错误" value="${esc(ui.search)}"${wired ? "" : " disabled"}></label>
  </div>`;
  const page = loaded(r.page);
  if (!page)
    return `<section class="card um-req" aria-label="请求记录">${filters}${placeholderHtml(r.page, "请求记录")}</section>`;
  const { rows, scope, earlier } = page.data;
  const filtered = q.model !== null || q.result !== "all" || q.search !== "";
  const meta = `<p class="um-scope">${esc(scope)} · ${freshnessHtml(page.at, page.stale, page.refreshing)} · 本页 ${full(rows.length)} 条，时间倒序</p>`;
  if (!rows.length) {
    return `<section class="card um-req" aria-label="请求记录">${filters}${meta}<p class="um-empty">${filtered ? "没有符合筛选的请求。" : "这段时间没有 UM 请求记录。"}</p>${moreButton("records", earlier, "data-um-earlier", page, ui)}</section>`;
  }
  const body = rows
    .map((row, i) => rowHtml(row, i, ui.expanded === row.key, r, ui))
    .join("");
  // Explicit roles: a narrow card restacks the rows (um.css), and a table whose
  // parts change display keeps its semantics only through them.
  return `<section class="card um-req" aria-label="请求记录">${filters}${meta}
    <div class="um-table-scroll"><table class="data-table um-req-table" role="table">
      <thead><tr role="row"><th scope="col" role="columnheader">时间</th><th scope="col" role="columnheader">模型</th><th scope="col" role="columnheader">结果</th><th scope="col" class="num" role="columnheader">输入 / 输出 Token</th><th scope="col" class="num" role="columnheader">耗时</th><th scope="col" class="num" role="columnheader">速度</th></tr></thead>
      <tbody>${body}</tbody></table></div>
    ${moreButton("records", earlier, "data-um-earlier", page, ui)}</section>`;
}

function rowHtml(
  row: UmRequestRow,
  index: number,
  open: boolean,
  r: UmRequests,
  ui: RequestsUi
): string {
  const result = wordOf(RESULT, row.result, RESULT.unknown);
  // By position: keys are free text, and two keys can sanitise to one id.
  const id = `um-req-${index}`;
  const main = `<tr class="um-req-row${open ? " is-open" : ""}" role="row">
    <td class="um-req-time" role="cell"><button type="button" class="um-row-toggle" data-um-row="${esc(row.key)}" aria-expanded="${open}" aria-controls="${id}"><svg class="um-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg><span class="tnum">${esc(when(row.at))}</span></button></td>
    <td class="um-req-model" role="cell" data-label="模型" title="${esc(row.model)}">${esc(row.model)}</td>
    <td role="cell" data-label="结果"><span class="status" data-tone="${result.tone}"><span class="dot" aria-hidden="true"></span>${result.word}</span></td>
    <td class="num" role="cell" data-label="输入 / 输出 Token">${readingHtml(row.input, full, true)} / ${readingHtml(row.output, full, true)}</td>
    <td class="num" role="cell" data-label="耗时">${readingHtml(row.durationMs, duration, true)}</td>
    <td class="num" role="cell" data-label="速度">${readingHtml(row.speed, speed, true)}</td></tr>`;
  if (!open) return main;
  return `${main}<tr class="um-req-detail" id="${id}" role="row"><td colspan="6" role="cell">${detailHtml(row, r, ui)}</td></tr>`;
}

function detailHtml(row: UmRequestRow, r: UmRequests, ui: RequestsUi): string {
  const retries =
    row.retries === null
      ? "重试情况没有记录"
      : row.retries.length === 0
        ? "没有记录到重试"
        : `<ol class="um-retries">${row.retries.map((x) => `<li><span class="tnum">${esc(clock(x.at))}</span> ${esc(x.note)}</li>`).join("")}</ol>`;
  const facts: [string, string][] = [
    ["结果说明", esc(row.resultNote)],
    [
      "HTTP 状态",
      row.httpStatus !== null
        ? esc(String(row.httpStatus))
        : `<span class="um-absent">没有 HTTP 状态</span>（连接在响应前结束或没有记录）`,
    ],
    [
      "错误",
      row.error
        ? `<code>${esc(row.error.code)}</code> ${esc(row.error.message)}`
        : "无",
    ],
    [
      "请求 ID",
      row.requestId
        ? `<code>${esc(row.requestId)}</code>`
        : `<span class="um-absent">缺少 request_id</span>：日志关联不足`,
    ],
    ["时间", `${esc(when(row.at))}（北京时间）`],
    ["缓存读取", `${readingHtml(row.cacheRead, tokens)}（已含在输入中）`],
    ["思考 Token", `${readingHtml(row.reasoning, tokens)}（已计入输出）`],
    [
      "首字耗时",
      row.firstTokenMs.kind === "missing"
        ? `<span class="um-absent">未实测</span>`
        : readingHtml(row.firstTokenMs, duration),
    ],
    ["速度口径", esc(speedBasisWord(row.speedBasis))],
    ["重试", retries],
  ];
  return `<div class="um-req-detail-grid"><dl class="um-facts">${facts.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>
    <div class="um-req-logs"><h3>可关联的日志</h3>${linkedLogsHtml(row, r, ui)}</div></div>`;
}

function linkedLogsHtml(
  row: UmRequestRow,
  r: UmRequests,
  ui: RequestsUi
): string {
  if (!row.requestId)
    return `<p class="um-note">这条记录没有 request_id，无法可靠地对上日志；为免误导，不按时间就近猜测。</p>`;
  const detail = r.detail && r.detail.key === row.key ? r.detail.logs : null;
  // Read again: the controller may have dropped this row's logs for another
  // row's, or the read failed; the reader can always ask once more.
  const again = (label: string) =>
    `<button type="button" class="btn btn-sm btn-outline" data-um-detail="${esc(row.key)}">${label}</button>`;
  if (!detail)
    return ui.detailAsked === row.key
      ? `<p class="um-note">正在请求关联日志…</p>`
      : `<p class="um-note">关联日志还没有读取。${again("读取关联日志")}</p>`;
  const shown = loaded(detail);
  if (!shown)
    return `${placeholderHtml(detail, "关联日志")}${detail.state === "failed" ? again("重新读取") : ""}`;
  if (!shown.data.length)
    return `<p class="um-note">日志读取范围内没有带这个 request_id 的行。</p>`;
  return `<ol class="um-log-list um-log-compact">${shown.data.map(logLineHtml).join("")}</ol>${shown.stale ? `<p class="um-note">${freshnessHtml(shown.at, shown.stale)} ${again("重新读取")}</p>` : ""}`;
}

function logLineHtml(line: UmLogLine): string {
  const level = wordOf(LEVEL, line.level, LEVEL.unknown);
  const day = line.at === null ? "" : dayKey(line.at);
  const timestamp =
    line.at === null
      ? `<span class="um-absent" title="这一行没有可解析的时间">时间缺失</span>`
      : esc(day ? `${day} ${clock(line.at)}` : clock(line.at));
  return `<li class="um-log" data-level="${esc(String(line.level))}"><span class="um-log-time tnum">${timestamp}</span><span class="pill" data-tone="${level.tone}">${level.word}</span><span class="um-log-src">${esc(line.source)}</span><code class="um-log-text">${esc(line.text)}</code></li>`;
}

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

function loginRoundsHtml(r: UmRequests, ui: RequestsUi): string {
  const q = r.loginRounds.query;
  const shown = loaded(r.loginRounds.page);
  const wired = r.loginRounds.page.state !== "unwired";
  const filters = `<div class="um-filters" role="search" aria-label="筛选 UM 登录轮次">
    <label class="um-search"><span class="sr-only">轮次</span><input type="search" inputmode="numeric" data-um-lrround placeholder="按轮次查找" value="${esc(q.round)}"${wired ? "" : " disabled"}></label>
    <label class="um-search"><span class="sr-only">原因</span><input type="search" data-um-lrq placeholder="按原因或结果查找" maxlength="120" value="${esc(q.q)}"${wired ? "" : " disabled"}></label>
  </div>`;
  const head = `<p class="um-scope um-scope-top">来源：UM 登录轮次（本机 8000 写入；只读，不触发探测或登录）。</p>`;
  if (!shown)
    return `<section class="card um-req" aria-label="UM 登录轮次">${head}${filters}${placeholderHtml(r.loginRounds.page, "UM 登录轮次")}</section>`;
  const { rounds, scope, truncated } = shown.data;
  const meta = `<p class="um-scope">${esc(scope)} · ${freshnessHtml(shown.at, shown.stale, shown.refreshing)} · 本页 ${full(rounds.length)} 轮${truncated ? "（已达单页上限，请按轮次或原因缩小范围）" : ""}</p>`;
  if (!rounds.length) {
    return `<section class="card um-req" aria-label="UM 登录轮次">${head}${filters}${meta}<p class="um-empty">保留范围内没有符合条件的登录轮次。</p></section>`;
  }
  const body = rounds
    .map((row) => {
      const state = ROUND_STATE[row.state];
      const outcome =
        row.outcome === null ? null : (OUTCOME[row.outcome] ?? row.outcome);
      const evidence =
        row.evidenceVerdict === null
          ? null
          : (EVIDENCE[row.evidenceVerdict] ?? row.evidenceVerdict);
      return `<tr role="row">
        <td class="um-req-time" role="cell" title="运行 ${esc(row.runId)}"><span class="tnum">${esc(fullWhen(row.startedAt))}</span></td>
        <td class="num" role="cell" data-label="轮次">${full(row.round)}</td>
        <td role="cell" data-label="状态"><span class="status" data-tone="${state.tone}"><span class="dot" aria-hidden="true"></span>${state.word}</span></td>
        <td role="cell" data-label="模式">${row.visible ? "可见窗口" : "后台"}</td>
        <td role="cell" data-label="触发来源">${esc(TRIGGER[row.triggerSource ?? ""] ?? row.triggerSource ?? "未记录")}</td>
        <td class="num" role="cell" data-label="提交">${full(row.submitCount)}</td>
        <td role="cell" data-label="结果">${outcome === null ? '<span class="um-absent">尚未结束</span>' : esc(outcome)}</td>
        <td role="cell" data-label="原因">${esc([
          row.reason,
          row.rejectedBeforeExpiryStatus == null
            ? null
            : `旧凭据尚未到期，已被 UM 拒绝（HTTP ${row.rejectedBeforeExpiryStatus}）`,
        ].filter(Boolean).join(" · ") || "—")}</td>
        <td role="cell" data-label="上游 / 证据">${row.upstreamStatus === null ? "无 HTTP 状态" : `HTTP ${row.upstreamStatus}`}${evidence === null ? "" : ` · ${esc(evidence)}`}</td>
      </tr>`;
    })
    .join("");
  return `<section class="card um-req" aria-label="UM 登录轮次">${head}${filters}${meta}
    <div class="um-table-scroll"><table class="data-table um-req-table" role="table">
      <thead><tr role="row"><th scope="col" role="columnheader">开始（北京时间）</th><th scope="col" class="num" role="columnheader">轮次</th><th scope="col" role="columnheader">状态</th><th scope="col" role="columnheader">模式</th><th scope="col" role="columnheader">触发来源</th><th scope="col" class="num" role="columnheader">提交</th><th scope="col" role="columnheader">结果</th><th scope="col" role="columnheader">原因</th><th scope="col" role="columnheader">上游 / 证据</th></tr></thead>
      <tbody>${body}</tbody></table></div></section>`;
}

function logsHtml(r: UmRequests, ui: RequestsUi): string {
  const q: LogQuery = r.logs.query;
  const wired = r.logs.page.state !== "unwired";
  const shown = loaded(r.logs.page);
  const sources = shown?.data.sources ?? [];
  const levels: LogLevel[] = ["error", "warn", "info", "debug"];
  const filters = `<div class="um-filters" role="search" aria-label="筛选日志">
    ${windowSeg(q.window, "data-um-lwin", !wired)}
    <div class="um-seg" role="group" aria-label="级别（不选即全部）">${levels.map((l) => `<button type="button" data-um-level="${l}" aria-pressed="${q.levels.includes(l)}"${wired ? "" : " disabled"}>${LEVEL[l].word}</button>`).join("")}</div>
    <label class="um-select"><span class="sr-only">来源</span><select data-um-lsource${wired ? "" : " disabled"}><option value="">全部来源</option>${sources.map((s) => `<option value="${esc(s)}"${s === q.source ? " selected" : ""}>${esc(s)}</option>`).join("")}</select></label>
  </div>`;
  if (!shown)
    return `<section class="card um-logs" aria-label="运行日志">${filters}${placeholderHtml(r.logs.page, "运行日志")}</section>`;
  const { lines, scope, earlier } = shown.data;
  const meta = `<p class="um-scope">读取范围：${esc(scope)} · ${freshnessHtml(shown.at, shown.stale, shown.refreshing)} · 本页 ${full(lines.length)} 行</p>`;
  const list = lines.length
    ? `<ol class="um-log-list">${lines.map(logLineHtml).join("")}</ol>`
    : `<p class="um-empty">读取范围内没有符合筛选的日志行。</p>`;
  return `<section class="card um-logs" aria-label="运行日志">${filters}${meta}${list}${moreButton("logs", earlier, "data-um-learlier", shown, ui)}</section>`;
}

function maintenanceHtml(
  load: Load<UmMaintenancePage>,
  ui: RequestsUi
): string {
  const shown = loaded(load);
  const head = `<p class="um-scope um-scope-top">登录、核验、启动、停止、重启的过程与结果；与模型请求分开记录，不计入请求数。</p>`;
  if (!shown)
    return `<section class="card um-maint" aria-label="维护操作">${head}${placeholderHtml(load, "维护操作")}</section>`;
  const { rows, scope, earlier } = shown.data;
  const meta = `<p class="um-scope">${esc(scope)} · ${freshnessHtml(shown.at, shown.stale, shown.refreshing)}</p>`;
  if (!rows.length)
    return `<section class="card um-maint" aria-label="维护操作">${head}${meta}<p class="um-empty">读取范围内没有维护操作记录。</p>${moreButton("maintenance", earlier, "data-um-mearlier", shown, ui)}</section>`;
  const body = rows
    .map((row, i) => {
      const st = wordOf(MAINT_STATE, row.state, STATE_UNKNOWN);
      const open = ui.openSteps.has(row.key);
      const id = `um-steps-${i}`;
      const took =
        row.finishedAt !== null ? duration(row.finishedAt - row.at) : "—";
      const main = `<tr role="row"><td class="um-maint-time" role="cell"><button type="button" class="um-row-toggle" data-um-steps="${esc(row.key)}" aria-expanded="${open}" aria-controls="${id}"${row.steps.length ? "" : " disabled"}><svg class="um-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg><span class="tnum">${esc(when(row.at))}</span></button></td>
      <td role="cell" data-label="操作">${esc(wordOf(ACTION_LABEL, row.action, String(row.action)))}</td><td role="cell" data-label="状态"><span class="status" data-tone="${st.tone}"><span class="dot" aria-hidden="true"></span>${st.word}</span></td>
      <td class="um-maint-result" role="cell" data-label="结果">${esc(row.result)}</td><td class="num" role="cell" data-label="用时">${esc(took)}</td></tr>`;
      return open
        ? `${main}<tr class="um-req-detail" id="${id}" role="row"><td colspan="5" role="cell"><ol class="um-steps">${row.steps.map((s) => `<li><span class="tnum">${esc(clock(s.at))}</span> ${esc(s.note)}</li>`).join("")}</ol></td></tr>`
        : main;
    })
    .join("");
  return `<section class="card um-maint" aria-label="维护操作">${head}${meta}<div class="um-table-scroll"><table class="data-table um-maint-table" role="table"><thead><tr role="row"><th scope="col" role="columnheader">开始</th><th scope="col" role="columnheader">操作</th><th scope="col" role="columnheader">状态</th><th scope="col" role="columnheader">结果</th><th scope="col" class="num" role="columnheader">用时</th></tr></thead><tbody>${body}</tbody></table></div>${moreButton("maintenance", earlier, "data-um-mearlier", shown, ui)}</section>`;
}

export function requestsHtml(r: UmRequests, ui: RequestsUi): string {
  const tabs = `<div class="um-subtabs" role="tablist" aria-label="请求与诊断">${VIEWS.map((v) => `<button type="button" role="tab" id="um-tab-${v.id}" aria-controls="um-panel" aria-selected="${v.id === ui.view}" tabindex="${v.id === ui.view ? 0 : -1}" data-um-view="${v.id}">${v.label}</button>`).join("")}</div>`;
  const panel =
    ui.view === "records"
      ? recordsHtml(r, ui)
      : ui.view === "logs"
        ? `${loginRoundsHtml(r, ui)}${logsHtml(r, ui)}`
        : maintenanceHtml(r.maintenance, ui);
  return `<div class="um-page um-requests">${tabs}<div id="um-panel" role="tabpanel" aria-labelledby="um-tab-${ui.view}">${panel}</div></div>`;
}
