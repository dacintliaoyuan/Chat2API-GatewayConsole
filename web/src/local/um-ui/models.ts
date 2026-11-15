import type {
  ModelTestState,
  UmModelRow,
  UmModels,
  UmTestRecord,
  UmTestRun,
} from "./contract";
import {
  blockReason,
  chars,
  drawable,
  duration,
  esc,
  freshnessHtml,
  full,
  loaded,
  placeholderHtml,
  readingHtml,
  speed,
  speedBasisWord,
  tokens,
  when,
  wordOf,
} from "./format";

/** 模型与设置: the access card, every UM model in one table with one shared
 *  selection bar (上下文探测 runs on click, no preview, no second confirm),
 *  the current test, then folded history and settings. */

export type ModelsUi = {
  selected: ReadonlySet<string>;
  expanded: ReadonlySet<string>;
  runDetail: ReadonlySet<string>;
  historyOpen: boolean;
  settingsOpen: boolean;
  /** A test was just asked for and the controller has not answered yet. */
  awaiting: boolean;
};

export const TEST_STATE: Readonly<
  Record<ModelTestState, { word: string; tone: string }>
> = {
  waiting: { word: "等待中", tone: "neutral" },
  running: { word: "执行中", tone: "neutral" },
  done: { word: "已完成", tone: "ok" },
  failed: { word: "失败", tone: "err" },
  partial: { word: "部分完成", tone: "warn" },
  not_run: { word: "未执行", tone: "neutral" },
};
const testState = (state: ModelTestState) =>
  wordOf(TEST_STATE, state, { word: "状态未知", tone: "neutral" });
const TERMINAL = new Set<ModelTestState>([
  "done",
  "failed",
  "partial",
  "not_run",
]);
const THINK_SOURCE: Readonly<Record<UmModelRow["thinking"]["source"], string>> =
  { catalog: "UM 目录", local: "本地已知", unknown: "来源未知" };
const thinkSource = (source: UmModelRow["thinking"]["source"]) =>
  wordOf(THINK_SOURCE, source, "来源未知");
const NOTICE_TONES = new Set(["ok", "warn", "err", "neutral"]);

/** The catalog's model ids, once each, in catalog order. */
export function catalogIds(m: UmModels): string[] {
  return [...new Set(loaded(m.catalog)?.data.models.map((x) => x.id) ?? [])];
}

/** Why a context probe cannot start now, or null. The selection bar draws
 *  its button from this and the console checks it again on click, so a
 *  stale or child-dispatched click can never send what the page shows closed. */
export function testBlockReason(m: UmModels, ui: ModelsUi): string | null {
  const count = catalogIds(m).filter((id) => ui.selected.has(id)).length;
  const running = m.run !== null && m.run.state !== "finished";
  return (
    blockReason(m.actions.context) ??
    (running
      ? "已有测试在进行，等它结束或停止后再发起"
      : ui.awaiting
        ? "正在提交"
        : count === 0
          ? "先勾选至少一个模型"
          : null)
  );
}
/** Why the run `runId` cannot be asked to stop now, or null. */
export function stopBlockReason(m: UmModels, runId: string): string | null {
  const run = m.run;
  if (!run || run.id !== runId || run.state === "finished")
    return "这次测试已经结束";
  return run.stopRequested ? "已请求停止" : blockReason(m.actions.stop);
}
/** Why the catalog cannot be refreshed now, or null. */
export function refreshBlockReason(m: UmModels): string | null {
  return blockReason(m.actions.refresh, m.refresh.state === "refreshing");
}

function accessHtml(m: UmModels): string {
  const shown = loaded(m.access);
  const head = `<div class="card-head"><h2 id="um-access-title">接入信息</h2>${shown ? `<span class="card-meta">${freshnessHtml(shown.at, shown.stale, shown.refreshing)}</span>` : ""}</div>`;
  if (!shown)
    return `<section class="card um-access" aria-labelledby="um-access-title">${head}${placeholderHtml(m.access, "接入信息")}</section>`;
  const a = shown.data;
  const url = a.baseUrl
    ? `<code class="mono um-url">${esc(a.baseUrl)}</code><button type="button" class="btn btn-sm btn-tonal" data-um-copy>复制</button>`
    : `<span class="um-absent">后端没有给出接口地址</span>`;
  return `<section class="card um-access" aria-labelledby="um-access-title">${head}
    <div class="um-access-grid">
      <div class="um-field"><span class="um-field-label">UM 接口地址</span><div class="um-field-value">${url}</div></div>
      <div class="um-field"><span class="um-field-label">接口类型</span><div class="um-field-value">${a.apiType ? esc(a.apiType) : `<span class="um-absent">未提供</span>`}</div></div>
    </div>
    ${a.hints.length ? `<ul class="um-hints">${a.hints.map((h) => `<li>${esc(h)}</li>`).join("")}</ul>` : ""}
  </section>`;
}

function contextCell(row: UmModelRow): string {
  const c = row.context;
  const lines: string[] = [];
  if (c.declared)
    lines.push(
      `<span title="来源：${esc(c.declared.source)}">声明 ${esc(tokens(c.declared.tokens))} Token</span>`
    );
  else if (c.compatDefault !== null)
    lines.push(
      `<span title="兼容默认值，不是 UM 批准的上限">兼容默认 ${esc(tokens(c.compatDefault))}<span class="um-hint um-nb">（非 UM 声明）</span></span>`
    );
  else lines.push(`<span class="um-absent">未声明</span>`);
  if (c.tokenLowerBound)
    lines.push(
      `<span title="上游报告的 Token 数：已验证的下界，不是最大上下文">实测 ≥${esc(tokens(c.tokenLowerBound.tokens))} Token</span>`
    );
  else if (c.probedChars)
    lines.push(
      `<span title="字符步进通过的最大长度，按字符计">字符步进 ${esc(chars(c.probedChars.chars))}</span>`
    );
  return lines.join("<br>");
}
function thinkingCell(row: UmModelRow): string {
  const t = row.thinking;
  // 「不支持」 only on a known source's word; anything else is unknown.
  if (!t.levels.length)
    return t.source === "catalog" || t.source === "local"
      ? "不支持"
      : `<span class="um-absent">未知</span>`;
  return `${t.levels.map(esc).join(" · ")}<br><span class="um-hint">${thinkSource(t.source)}</span>`;
}
/** The statements of a controller summary, split only where the controller
 *  itself separates them (「；」 or 「 · 」), never inside a figure. */
function clauses(summary: string): string[] {
  return String(summary)
    .split(/\s*[；;]\s*|\s+·\s+/)
    .map((s) => s.trim())
    .filter(Boolean);
}
/** A figure with its unit (「46 token/秒」「≥96,214 Token」「32万 字符」「9.8 秒」)
 *  stays on one line; the words around it wrap as usual. */
const FIGURE =
  /[≥≈]?\d[\d,.]*(?:\s?[万亿])?(?:\s?(?:token\/秒|Token|tokens?|字符|毫秒|秒|分钟|小时|ms))?/g;
function figuresHtml(text: string): string {
  let html = "",
    last = 0;
  for (const m of text.matchAll(FIGURE)) {
    const at = m.index ?? 0;
    html += `${esc(text.slice(last, at))}<span class="um-nb">${esc(m[0])}</span>`;
    last = at + m[0].length;
  }
  return html + esc(text.slice(last));
}
/** 最近测试, compact: the conclusion on the first line and the time under
 *  it. The controller's full wording, long errors and the evidence stay one
 *  click away in the row's 详情, never cut off unseen. */
function lastTestCell(
  row: UmModelRow,
  detailId: string,
  open: boolean
): string {
  if (!row.lastContext) return `<span class="um-absent">未测</span>`;
  const test = row.lastContext;
  const [head = ""] = clauses(test.summary);
  return `<div class="um-last-cell"><div class="um-last" data-tone="${test.ok ? "ok" : "err"}" title="${esc(`上下文：${test.summary}`)}"><span class="dot" aria-hidden="true"></span><div class="um-last-body">
      <p class="um-last-head"><b>上下文</b>${test.ok ? "" : `<span class="sr-only">未通过：</span>`}${figuresHtml(head)}</p>
      <p class="um-last-meta"><span class="um-last-time um-nb">${esc(when(test.at))}</span></p></div></div><button type="button" class="um-link um-last-more" data-um-model-more="${esc(row.id)}" aria-expanded="${open}" aria-controls="${detailId}">${open ? "收起详情" : "详情"}</button></div>`;
}
function modelDetailHtml(row: UmModelRow, source: string): string {
  const c = row.context;
  const facts: [string, string][] = [
    ["模型 ID", `<code>${esc(row.id)}</code>`],
    ["目录来源", esc(source)],
    [
      "目录声明",
      c.declared
        ? `${full(c.declared.tokens)} Token（${esc(c.declared.source)}）`
        : "UM 目录没有声明上下文",
    ],
    [
      "兼容默认",
      c.compatDefault !== null
        ? `${full(c.compatDefault)} Token：客户端兼容用的默认值，不是 UM 批准的上限`
        : "无",
    ],
    [
      "字符步进",
      c.probedChars
        ? `通过 ${full(c.probedChars.chars)} 字符（${esc(when(c.probedChars.at))}）：按字符计，不换算成 Token`
        : "还没有字符步进结果",
    ],
    [
      "Token 下界",
      c.tokenLowerBound
        ? `≥${full(c.tokenLowerBound.tokens)} Token（${esc(when(c.tokenLowerBound.at))}）：上游报告的已验证下界，不是最大上下文`
        : "还没有上游报告的 Token 证据",
    ],
    [
      "思考档位",
      row.thinking.levels.length
        ? `${row.thinking.levels.map(esc).join(" · ")}${row.thinking.defaultLevel ? `，默认 ${esc(row.thinking.defaultLevel)}` : ""}（${thinkSource(row.thinking.source)}）`
        : thinkSource(row.thinking.source),
    ],
    [
      "最近上下文探测",
      row.lastContext
        ? `${esc(row.lastContext.summary)}（${esc(when(row.lastContext.at))}）`
        : "未测",
    ],
  ];
  return `<dl class="um-facts um-facts-2">${facts.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>
    <p class="um-note">重测：勾选这个模型，再用下方选择栏发起；旧结果按既有规则归档，不会被静默跳过。</p>`;
}

function catalogHtml(m: UmModels, ui: ModelsUi): string {
  const shown = loaded(m.catalog);
  const refreshing = m.refresh.state === "refreshing";
  const refreshBlock = refreshBlockReason(m);
  const refreshBtn = `<button type="button" class="btn btn-sm btn-outline" data-um-refresh-catalog${refreshBlock ? ` disabled title="${esc(refreshBlock)}"` : ""}>${refreshing ? `<span class="um-spinner" aria-hidden="true"></span>正在刷新…` : `<svg class="shell-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.6-5.9"/><path d="M20 4.5V9h-4.5"/></svg>刷新 UM 模型列表`}</button>`;
  const meta = shown
    ? `目录 ${esc(when(shown.data.fetchedAt))} · ${esc(shown.data.source)} · 共 ${shown.data.models.length} 个`
    : "";
  const head = `<div class="card-head"><h2 id="um-catalog-title">UM 模型</h2><span class="card-meta">${meta}</span><span class="card-grow"></span>${refreshBtn}</div>`;
  const status =
    m.refresh.state === "failed"
      ? `<p class="um-banner" data-tone="warn" role="status"><span class="dot" aria-hidden="true"></span>刷新失败（${esc(when(m.refresh.at))}）：${esc(m.refresh.reason)}。下表仍是 ${shown ? esc(when(shown.data.fetchedAt)) : "上次"} 取到的目录，已过期。</p>`
      : m.refresh.state === "succeeded"
        ? `<p class="um-banner" data-tone="ok" role="status"><span class="dot" aria-hidden="true"></span>已从 UM 取到最新目录（${esc(when(m.refresh.at))}）；刷新只读目录，不探测、不改客户端配置。</p>`
        : shown?.stale
          ? `<p class="um-banner" data-tone="warn" role="status">${freshnessHtml(shown.at, shown.stale)}</p>`
          : "";
  const notice = m.notice
    ? `<p class="um-banner" data-tone="${NOTICE_TONES.has(m.notice.tone) ? m.notice.tone : "neutral"}" role="status"><span class="dot" aria-hidden="true"></span>${esc(m.notice.text)}（${esc(when(m.notice.at))}）</p>`
    : "";
  // A closed refresh says why (the unwired banner already covers the unwired case).
  const blocked =
    !m.actions.refresh.enabled && m.catalog.state !== "unwired"
      ? `<p class="um-block-line">刷新 UM 模型列表暂不可用：${esc(m.actions.refresh.reason)}</p>`
      : "";
  if (!shown)
    return `<section class="card um-catalog" aria-labelledby="um-catalog-title">${head}${blocked}${status}${placeholderHtml(m.catalog, "UM 模型目录")}</section>`;
  const models = shown.data.models;
  const body = models
    .map((row, i) => {
      const open = ui.expanded.has(row.id);
      // By position: free-text ids such as gpt-5.2 and gpt-5_2 sanitise alike.
      const id = `um-model-${i}`;
      const picked = ui.selected.has(row.id);
      // Explicit roles: a narrow card restacks the rows (um.css), and a table
      // whose parts change display keeps its semantics only through them.
      const main = `<tr class="um-model-row${picked ? " is-picked" : ""}" role="row">
      <td class="um-col-pick" role="cell"><input type="checkbox" data-um-pick="${esc(row.id)}"${picked ? " checked" : ""} aria-label="选择 ${esc(row.name)}"></td>
      <td class="um-col-name" role="cell"><button type="button" class="um-model-name" data-um-model="${esc(row.id)}" aria-expanded="${open}" aria-controls="${id}"><svg class="um-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg><span>${esc(row.name)}</span></button>${row.id !== row.name ? `<div class="um-model-id mono" title="${esc(row.id)}">${esc(row.id)}</div>` : ""}</td>
      <td class="um-col-ctx" role="cell" data-label="上下文信息">${contextCell(row)}</td><td class="um-col-think" role="cell" data-label="思考档位">${thinkingCell(row)}</td><td class="um-col-last" role="cell" data-label="最近测试">${lastTestCell(row, id, open)}</td></tr>`;
      return open
        ? `${main}<tr class="um-req-detail" id="${id}" role="row"><td colspan="5" role="cell">${modelDetailHtml(row, shown.data.source)}</td></tr>`
        : main;
    })
    .join("");
  const table = models.length
    ? `<div class="um-table-scroll"><table class="data-table um-model-table" role="table"><thead><tr role="row"><th scope="col" class="um-col-pick" role="columnheader">选择</th><th scope="col" class="um-col-name" role="columnheader">模型名称 / ID</th><th scope="col" class="um-col-ctx" role="columnheader">上下文信息</th><th scope="col" class="um-col-think" role="columnheader">思考档位</th><th scope="col" class="um-col-last" role="columnheader">最近测试</th></tr></thead><tbody>${body}</tbody></table></div>`
    : `<p class="um-empty">UM 目录是空的：后端合法地返回了 0 个模型。</p>`;
  return `<section class="card um-catalog" aria-labelledby="um-catalog-title">${head}${blocked}${status}${table}${notice}${selectionBar(m, ui)}</section>`;
}

function selectionBar(m: UmModels, ui: ModelsUi): string {
  const ids = catalogIds(m);
  const count = ids.filter((id) => ui.selected.has(id)).length;
  const block = testBlockReason(m, ui);
  const button = `<button type="button" class="btn btn-sm btn-tonal" data-um-test="context"${block ? ` disabled title="${esc(block)}"` : ""}>上下文探测</button>`;
  const note = block;
  return `<div class="um-selbar" role="toolbar" aria-label="批量测试">
    <button type="button" class="btn btn-sm btn-outline" data-um-pick-all${!ids.length || count === ids.length ? " disabled" : ""}>全选</button>
    <span class="um-selbar-count" aria-live="polite">已选 <b>${count}</b> 个</span>
    ${button}
    <button type="button" class="btn btn-sm btn-outline" data-um-pick-none${count ? "" : " disabled"}>清空选择</button>
    <span class="um-selbar-note">${note ? esc(note) : "点击后直接执行，不再二次确认；多个模型按顺序依次测试。上下文探测整批共用一份请求预算。"}</span>
  </div>`;
}

function runDetailHtml(item: UmTestRun["items"][number]): string {
  const d = item.detail;
  if (!d) return `<p class="um-note">还没有结果详情。</p>`;
  const facts: [string, string][] = [
    ["耗时", readingHtml(d.durationMs, duration)],
    [
      "首字耗时",
      d.firstTokenMs.kind === "missing"
        ? `<span class="um-absent">未实测</span>`
        : readingHtml(d.firstTokenMs, duration),
    ],
    ["输出 Token", readingHtml(d.outputTokens, full)],
    [
      "速度",
      `${readingHtml(d.speed, speed)}${drawable(d.speed) !== null ? `（${esc(speedBasisWord(d.speedBasis))}）` : ""}`,
    ],
    ["字符步进", readingHtml(d.contextChars, chars)],
    [
      "Token 下界",
      `${readingHtml(d.contextTokenLowerBound, (n) => `≥${full(n)} Token`)}`,
    ],
  ];
  if (d.error) facts.push(["错误", esc(d.error)]);
  return `<dl class="um-facts um-facts-2">${facts.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>${d.lines.length ? `<ol class="um-steps">${d.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ol>` : ""}`;
}

function runHtml(m: UmModels, ui: ModelsUi): string {
  const run = m.run;
  if (!run) return "";
  const done = run.items.filter((i) => TERMINAL.has(i.state)).length;
  const finished = run.state === "finished";
  const stopLabel = "停止后续探测";
  const stopBlock = stopBlockReason(m, run.id);
  const stop = finished
    ? ""
    : `<button type="button" class="btn btn-sm btn-danger" data-um-stop="${esc(run.id)}"${stopBlock ? ` disabled title="${esc(stopBlock)}"` : ""}>${run.stopRequested ? "已请求停止" : stopLabel}</button>`;
  const meta = [
    `开始于 ${esc(when(run.startedAt))}`,
    `已完成 ${done}/${run.items.length}`,
    run.budget
      ? `请求预算 已用 ${esc(String(run.budget.used))}/${esc(String(run.budget.limit))}`
      : null,
  ]
    .filter(Boolean)
    .join(" · ");
  const heading =
    run.state === "submitting"
      ? "正在提交上下文探测"
      : finished
        ? "最近一次上下文探测 · 已结束"
        : "当前测试 · 上下文探测";
  const items = run.items
    .map((item, i) => {
      const st = testState(item.state);
      const open = ui.runDetail.has(item.model);
      const id = `um-run-${i}`;
      const text =
        item.state === "running"
          ? (item.stage ?? "执行中")
          : (item.summary ?? (item.state === "waiting" ? "排在后面" : ""));
      return `<li class="um-run-item" data-state="${esc(String(item.state))}">
      <span class="um-run-model" title="${esc(item.model)}">${esc(item.model)}</span>
      <span class="status" data-tone="${st.tone}">${item.state === "running" ? `<span class="um-spinner" aria-hidden="true"></span>` : `<span class="dot" aria-hidden="true"></span>`}${st.word}</span>
      <span class="um-run-text">${esc(text)}</span>
      ${item.detail ? `<button type="button" class="um-link" data-um-rundetail="${esc(item.model)}" aria-expanded="${open}" aria-controls="${id}">详情</button>` : "<span></span>"}
      ${open ? `<div class="um-run-detail" id="${id}">${runDetailHtml(item)}</div>` : ""}
    </li>`;
    })
    .join("");
  const stopNote =
    run.stopRequested && !finished
      ? `<p class="um-note" role="status">已请求停止：正在进行的请求可能仍会完成，之后的模型不再开始；已完成的结果保留。</p>`
      : "";
  return `<section class="card um-run" aria-labelledby="um-run-title">
    <div class="card-head"><h2 id="um-run-title">${heading}</h2><span class="card-meta">${meta}</span><span class="card-grow"></span>${stop}</div>
    ${stopNote}<ol class="um-run-list" aria-live="polite">${items}</ol>${run.note ? `<p class="um-note">${esc(run.note)}</p>` : ""}</section>`;
}

function historyHtml(m: UmModels, ui: ModelsUi, models: string[]): string {
  const h = m.history;
  const shown = loaded(h.records);
  const count = shown
    ? ` <span class="um-count">${shown.data.length}</span>`
    : "";
  let body = "";
  if (ui.historyOpen) {
    const wired = h.records.state !== "unwired";
    const filters = `<div class="um-filters">
      <label class="um-select"><span class="sr-only">模型</span><select data-um-hmodel${wired ? "" : " disabled"}><option value="">全部模型</option>${models.map((x) => `<option value="${esc(x)}"${x === h.filter.model ? " selected" : ""}>${esc(x)}</option>`).join("")}</select></label>
      ${shown ? `<span class="card-meta">${freshnessHtml(shown.at, shown.stale, shown.refreshing)}</span>` : ""}
    </div>`;
    const rows = shown
      ? shown.data.length
        ? `<div class="um-table-scroll"><table class="data-table" role="table"><thead><tr role="row"><th scope="col" role="columnheader">时间</th><th scope="col" role="columnheader">模型</th><th scope="col" role="columnheader">结果</th><th scope="col" role="columnheader">摘要</th></tr></thead><tbody>${shown.data.map(historyRow).join("")}</tbody></table></div>`
        : `<p class="um-empty">没有符合筛选的测试记录。</p>`
      : placeholderHtml(h.records, "历史测试记录");
    body = `<div class="um-fold-body">${filters}${rows}</div>`;
  }
  return `<details class="card um-fold" data-um-fold="history"${ui.historyOpen ? " open" : ""}><summary><svg class="um-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>历史测试记录${count}</summary>${body}</details>`;
}
function historyRow(r: UmTestRecord): string {
  const st = testState(r.result);
  return `<tr${r.archived ? ` class="is-archived"` : ""} role="row"><td class="tnum um-hist-time" role="cell">${esc(when(r.at))}</td><td class="um-req-model" role="cell" data-label="模型" title="${esc(r.model)}">${esc(r.model)}</td><td role="cell" data-label="结果"><span class="status" data-tone="${st.tone}"><span class="dot" aria-hidden="true"></span>${st.word}</span>${r.archived ? ` <span class="pill">已归档</span>` : ""}</td><td class="um-hist-summary" role="cell" data-label="摘要">${esc(r.summary)}</td></tr>`;
}

function settingsHtml(m: UmModels, open: boolean): string {
  let body = "";
  if (open) {
    const shown = loaded(m.runtime);
    const runtime = shown
      ? `<dl class="um-facts um-facts-2">${shown.data.map((f) => `<div><dt>${esc(f.label)}</dt><dd>${esc(f.value)}</dd></div>`).join("")}</dl><p class="um-note">超时、重试等运行配置在这里只读显示，不在网页修改。${freshnessHtml(shown.at, shown.stale, shown.refreshing)}</p>`
      : placeholderHtml(m.runtime, "运行信息");
    body = `<div class="um-fold-body um-settings">
      <div class="um-settings-block"><h3>外观</h3><p class="um-note">主题、侧栏、动效和透明度保存在这台浏览器里。</p><button type="button" class="btn btn-sm btn-tonal" data-action="settings">打开外观设置</button></div>
      <div class="um-settings-block"><h3>运行信息（只读）</h3>${runtime}</div>
    </div>`;
  }
  return `<details class="card um-fold" data-um-fold="settings"${open ? " open" : ""}><summary><svg class="um-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>外观与运行信息</summary>${body}</details>`;
}

export function modelsHtml(m: UmModels, ui: ModelsUi): string {
  const ids = catalogIds(m);
  return `<div class="um-page um-models">${accessHtml(m)}${catalogHtml(m, ui)}${runHtml(m, ui)}${historyHtml(m, ui, ids)}${settingsHtml(m, ui.settingsOpen)}</div>`;
}
