// Log-presentation checks (C4). The classification / axis helpers are imported
// from src/workbench-log-presentation.ts; the event-identity helpers are extracted
// from src/workbench.ts and transpiled with the project's own TypeScript, so these
// checks bind the SHIPPED code rather than a copy of it. The remaining checks
// assert the C4 defects cannot silently come back: an index-based overview copy
// button, an index-based detail copy / raw-fold, the dead level filter, the dead
// div-row styles, the empty-table sentence and the detail status count.
//
// This tree (standalone release) has no browser-driven console gate, so the
// page-level part is asserted textually against the shipped handlers in
// src/workbench.ts — the click paths themselves are bound, but a real browser
// interaction is NOT covered here and stays unverified in this tree.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import { logKind, chartBaselineKind, timeAxisWidth, axisLabelLines } from "../src/workbench-log-presentation.ts";

const here = dirname(fileURLToPath(import.meta.url));
const workbench = readFileSync(resolve(here, "../src/workbench.ts"), "utf8");
// 注释不是规则：先剥掉，避免「已删」说明里的旧选择器被当成又回来了。
const css = readFileSync(resolve(here, "../src/workbench.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

/** Extract one `function name(...) { ... }` declaration by brace matching. */
function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `workbench.ts 里找不到 ${name}()：身份检查绑定的函数被改名 / 删除了`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") { depth--; if (!depth) return source.slice(start, i + 1); }
  }
  throw new Error(`${name}() 没有闭合`);
}
/** The shipped identity helpers, transpiled and evaluated as-is. */
function loadIdentityHelpers() {
  const fragment = `${extractFunction(workbench, "logEventDigest")}\n${extractFunction(workbench, "logRowIdentities")}`;
  const js = ts.transpileModule(fragment, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText;
  return new Function(`${js}\nreturn { logEventDigest, logRowIdentities };`)();
}
const row = (ts_, message) => ({ ts: ts_, message, parse_ok: true, event: null });

test("severities override success; plain information does not claim a pass", () => {
  assert.equal(logKind({ level: "warning", parse_ok: true, event: { outcome: "ok" } }), "warning");
  assert.equal(logKind({ level: "ERROR", parse_ok: true, event: { outcome: "success" } }), "error");
  assert.equal(logKind({ level: "INFO", parse_ok: true }), "info");
  assert.equal(logKind({ level: "INFO", parse_ok: true, event: { outcome: "ok" } }), "success");
  assert.equal(logKind({ level: "INFO", parse_ok: true, event: { outcome: "failed" } }), "error");
  assert.equal(logKind({ parse_ok: false }), "unknown");
});

test("each hourly label keeps its date across midnight and gets reading space", () => {
  const labels = Array.from({ length:24 }, (_, i) => `10-${i < 12 ? "06" : "07"} ${String((i + 12) % 24).padStart(2, "0")}:00`);
  assert.deepEqual(axisLabelLines(labels[11]), ["10-06", "23:00"]);
  assert.deepEqual(axisLabelLines(labels[12]), ["10-07", "00:00"]);
  assert.deepEqual(axisLabelLines("10-07 02"), ["10-07", "02:00"]);
  assert.equal(labels.flatMap(axisLabelLines).length, 48);
  assert.ok(timeAxisWidth(labels, 640) >= 23 * 56 + 48);
  assert.equal(timeAxisWidth([], 640), 640);
  assert.deepEqual(axisLabelLines("10/07"), ["10/07"]);
});
test("missing statistics stay distinct from an observed zero", () => {
  assert.equal(chartBaselineKind(0), "zero");
  assert.equal(chartBaselineKind(null), "missing");
  assert.equal(chartBaselineKind(undefined), "missing");
  assert.equal(chartBaselineKind(1), null);
});

test("event identity separates duplicate timestamps and duplicate content, and never drifts with list position", () => {
  const { logEventDigest, logRowIdentities } = loadIdentityHelpers();
  const rows = [
    row("2026-10-07 07:41:31,087", "stream end status=ok"),
    row("2026-10-07 07:41:31,087", "stream end status=ok"),
    row("2026-10-07 07:41:31,087", "request summary final=HTTP502"),
    row("2026-10-07 07:41:32,000", "stream end status=ok"),
    row(null, "[continued] unparsable"),
  ];
  const ids = logRowIdentities("chat2api", rows);
  assert.equal(new Set(ids).size, ids.length, "重复内容 / 重复时间戳必须各自有身份");
  assert.notEqual(ids[0], ids[1], "完全相同（时间戳 + 内容）的两条必须靠出现序号分开");
  assert.notEqual(ids[1], ids[2], "相同时间戳、不同内容必须分开");
  assert.notEqual(ids[0], ids[3], "相同内容、不同时间戳必须分开");
  assert.deepEqual(logRowIdentities("chat2api", rows), ids, "身份必须对同一快照稳定");
  assert.notEqual(logRowIdentities("share-backend", rows)[0], ids[0], "不同来源的同内容事件必须分开");
  assert.match(ids[0], /^chat2api:[0-9a-f]{16}:\d+$/, "身份格式 = 来源:内容摘要:出现序号");
  // 窗口里插入一条新事件（下标全变）不改变其余事件的身份：身份不来自列表下标。
  const shifted = logRowIdentities("chat2api", [row("2026-10-07 07:41:33,000", "new"), ...rows]);
  assert.deepEqual(shifted.slice(1), ids, "插入新事件后其余事件的身份必须不变");
  assert.equal(logEventDigest("same"), logEventDigest("same"));
  assert.notEqual(logEventDigest("same"), logEventDigest("other"));
});

test("the overview copy button carries a render-time identity, not a list index", () => {
  assert.match(workbench, /data-wb-log-overview-copy="\$\{escapeHtml\(identity\)\}"/, "总览复制按钮必须携带渲染时身份");
  assert.ok(!/data-wb-log-overview-copy="\$\{idx\}"/.test(workbench), "总览复制按钮又带上了列表下标");
  assert.match(workbench, /logOverviewRenderRows\.get\(button\.dataset\.wbLogOverviewCopy\)/, "点击复制必须按渲染时身份表取行");
  assert.ok(!/this\.logOverviewEvents\(\)\.filter\(e => this\.logOverviewFilter === "all" \|\| e\.kind === this\.logOverviewFilter\)\[Number\(/.test(workbench), "点击复制又按下标重算动态列表");
  assert.ok(!/identity: `\$\{source\.source\}:\$\{row\.ts \?\? "unknown"\}:\$\{i\}`/.test(workbench), "事件身份又嵌入了行下标");
  assert.match(workbench, /this\.logOverviewRenderRows = new Map\(rendered\.map\(event => \[event\.identity, event\.row\]\)\)/, "总览渲染必须把该次渲染的身份入表");
  // 身份表里的行不在时如实报告，不静默复制另一条事件。
  assert.match(workbench, /该事件已不在当前列表，请刷新总览后重试/, "总览复制未命中必须如实反馈");
});

test("the detail copy and raw fold carry the same render-time identity, not a row index", () => {
  assert.match(workbench, /data-wb-log-copy="\$\{escapeHtml\(identity\)\}"/, "详情复制按钮必须携带渲染时身份");
  assert.ok(!/data-wb-log-copy="\$\{idx\}"/.test(workbench), "详情复制按钮又带上了行下标");
  assert.match(workbench, /logDetailRenderRows\.get\(logCopyId\)/, "详情复制必须按渲染时身份表取行");
  assert.ok(!/this\.logDetail\?\.rows\[Number\(logCopyIdx\)\]/.test(workbench), "详情复制又按点击时下标取行");
  assert.match(workbench, /this\.logDetailRenderRows = new Map\(rendered\.map\(\(\{ row, identity \}\) => \[identity, row\]\)\)/, "详情渲染必须把该次渲染的身份入表");
  assert.match(workbench, /该事件已不在当前列表，请刷新来源后重试/, "详情复制未命中必须如实反馈");
  // 展开 / 收起按渲染时身份记账：加载更早记录会前置整段行，下标会漂移。
  assert.match(workbench, /data-log-collapse="\$\{escapeHtml\(identity\)\}"/, "收起按钮必须携带渲染时身份");
  assert.match(workbench, /data-log-expand="\$\{escapeHtml\(identity\)\}"/, "展开按钮必须携带渲染时身份");
  assert.match(workbench, /this\.logDetail\.expanded\.add\(logExpandId\)/, "展开必须按身份记账");
  assert.match(workbench, /this\.logDetail\.expanded\.delete\(logCollapseId\)/, "收起必须按身份记账");
  assert.ok(!/this\.logDetail\.expanded\.add\(Number\(/.test(workbench), "展开又按行下标记账");
  assert.ok(!/expanded: Set<number>/.test(workbench), "展开状态又退回行下标集合");
  // 「展开原始内容」的折叠开关与复制共用同一身份（总览表）。
  assert.match(workbench, /data-wb-log-raw-toggle="\$\{escapeHtml\(identity\)\}"/, "展开原始内容必须携带同一渲染时身份");
  assert.ok(!/data-wb-log-raw-toggle="\$\{idx\}"/.test(workbench), "展开原始内容又带上了行下标");
});

test("the dead level filter is gone; the kind chips are the one classification entry", () => {
  assert.ok(!workbench.includes("wb-log-level"), "死代码 .wb-log-level 监听又回来了");
  assert.ok(!workbench.includes('params.set("level"'), "死代码 level 查询参数又回来了");
  assert.ok(!workbench.includes("d.level"), "死代码 detail.level 又回来了");
  assert.ok(!css.includes("wb-log-level"), "死代码 .wb-log-level 样式又回来了");
  assert.match(workbench, /data-wb-log-overview-filter="\$\{kind\}"/, "分类 chips 必须保留为唯一分类入口");
  assert.match(workbench, /private logKindFilteredRows\(/, "分类筛选必须有唯一入口");
});

test("the dead div-row styles are gone and the new table rows carry the kind colour", () => {
  assert.ok(!/\.wb-log-row\b/.test(css), "死样式 .wb-log-row 又回来了");
  assert.ok(!/\.wb-lv\b/.test(css), "死样式 .wb-lv 又回来了");
  assert.ok(!/\.wb-log-err\b/.test(css), "死样式 .wb-log-err 又回来了");
  assert.ok(!/\.wb-log-raw summary/.test(css), "永不渲染的 .wb-log-raw summary 样式又回来了");
  assert.match(css, /\.wb-log-overview-table tbody tr\[class\*="wb-log-kind-"\] > td:first-child/, "总览新表格行必须画行级分类色条");
  assert.match(css, /\.wb-log-event-table tbody tr\[class\*="wb-log-kind-"\] > td:first-child/, "详情新表格行必须画行级分类色条");
  assert.match(css, /border-left: 3px solid var\(--wb-log-color\)/, "行级分类色条必须取分类色 token");
  assert.match(css, /\.wb-log-kind-error \.wb-log-msg \{ color: var\(--wb-status-err\); \}/, "ERROR 行原文必须保持红字");
  assert.match(css, /:root\[data-dark-surface="true"\] \.wb-log-kind-info \{ --wb-log-color:#8dc7ff; \}/, "暗色下信息蓝必须另有覆盖值");
  assert.match(workbench, /tr class="wb-log-kind-\$\{kind\}"/, "总览表行必须带分类类名");
  assert.match(workbench, /tr class="wb-log-kind-\$\{logKind\(row\)\}"/, "详情表行必须带分类类名");
});

test("the empty-table sentence and the detail count name the kind filter", () => {
  assert.match(workbench, /没有符合分类筛选「\$\{escapeHtml\(this\.logKindFilterLabel\(\)\)\}」的事件/, "空表句必须点名实际分类筛选轴");
  assert.ok(!workbench.includes("当前筛选无事件"), "空表句又退回不点名筛选轴的旧文案");
  assert.match(workbench, /logKindFilteredRows\(detail\.rows\)/, "详情状态句必须按筛选后行计数");
  assert.ok(!/bits\.push\(detail\.rows\.length \?/.test(workbench), "详情状态句又按未筛选行计数");
});

test("the source-order note distinguishes the two rejection reasons", () => {
  assert.match(workbench, /saved\.reason === "sequence_too_long" \? "顺序超过上限" : "写入被拒绝"/, "顺序写回必须区分超上限与写入被拒绝");
  assert.ok(!workbench.includes("顺序只在本页有效：本浏览器未记住此次调整。"), "两种拒绝原因又折成同一句");
});
