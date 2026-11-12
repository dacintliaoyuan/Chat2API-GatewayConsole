// Log-presentation checks (C4). The classification / axis helpers are imported
// from src/workbench-log-presentation.ts; the event-identity helpers are extracted
// from src/workbench.ts and transpiled with the project's own TypeScript, so these
// checks bind the SHIPPED code rather than a copy of it. The remaining checks
// assert the C4 defects cannot silently come back: an index-based overview copy
// button, the dead level filter, the dead div-row styles, the empty-table sentence
// and the detail status count. P3/R3 (2026-10-10) adds the same guard for the
// first-category truncation and the raw-stamp ordering of the log overview, plus
// the normalized-time helper itself (the ISO-morning counterexample). Page-level
// event handlers are checked by scripts/console-gate/logs.mjs.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  logKind,
  logTsEpoch,
  chartBaselineKind,
  timeAxisWidth,
  axisLabelLines,
} from "../src/workbench-log-presentation.ts";

// TypeScript is resolved through createRequire, not a static ESM import: the
// staged candidate tree carries only the index and therefore has no
// ``node_modules``, so a static ``import "typescript"`` fails with
// ERR_MODULE_NOT_FOUND while ``require`` honours the NODE_PATH the verifier
// points at the source workspace.  check-cloud-console.mjs loads esbuild the
// same way.
const require = createRequire(new URL("../package.json", import.meta.url));
const ts = require("typescript");

const here = dirname(fileURLToPath(import.meta.url));
const workbench = readFileSync(resolve(here, "../src/workbench.ts"), "utf8");
// 注释不是规则：先剥掉，避免「已删」说明里的旧选择器被当成又回来了。
const css = readFileSync(resolve(here, "../src/workbench.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  ""
);

/** Extract one `function name(...) { ... }` declaration by brace matching. */
function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(
    start >= 0,
    `workbench.ts 里找不到 ${name}()：身份检查绑定的函数被改名 / 删除了`
  );
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") {
      depth--;
      if (!depth) return source.slice(start, i + 1);
    }
  }
  throw new Error(`${name}() 没有闭合`);
}
/** The shipped identity helpers, transpiled and evaluated as-is. */
function loadIdentityHelpers() {
  const fragment = `${extractFunction(workbench, "logEventDigest")}\n${extractFunction(workbench, "logRowIdentities")}`;
  const js = ts.transpileModule(fragment, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ESNext,
    },
  }).outputText;
  return new Function(`${js}\nreturn { logEventDigest, logRowIdentities };`)();
}
const row = (ts_, message) => ({
  ts: ts_,
  message,
  parse_ok: true,
  event: null,
});

test("severities override success; plain information does not claim a pass", () => {
  assert.equal(
    logKind({ level: "warning", parse_ok: true, event: { outcome: "ok" } }),
    "warning"
  );
  assert.equal(
    logKind({ level: "ERROR", parse_ok: true, event: { outcome: "success" } }),
    "error"
  );
  assert.equal(logKind({ level: "INFO", parse_ok: true }), "info");
  assert.equal(
    logKind({ level: "INFO", parse_ok: true, event: { outcome: "ok" } }),
    "success"
  );
  assert.equal(
    logKind({ level: "INFO", parse_ok: true, event: { outcome: "failed" } }),
    "error"
  );
  assert.equal(logKind({ parse_ok: false }), "unknown");
});

test("each hourly label keeps its date across midnight and gets reading space", () => {
  const labels = Array.from(
    { length: 24 },
    (_, i) =>
      `10-${i < 12 ? "06" : "07"} ${String((i + 12) % 24).padStart(2, "0")}:00`
  );
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
  assert.equal(
    new Set(ids).size,
    ids.length,
    "重复内容 / 重复时间戳必须各自有身份"
  );
  assert.notEqual(
    ids[0],
    ids[1],
    "完全相同（时间戳 + 内容）的两条必须靠出现序号分开"
  );
  assert.notEqual(ids[1], ids[2], "相同时间戳、不同内容必须分开");
  assert.notEqual(ids[0], ids[3], "相同内容、不同时间戳必须分开");
  assert.deepEqual(
    logRowIdentities("chat2api", rows),
    ids,
    "身份必须对同一快照稳定"
  );
  assert.notEqual(
    logRowIdentities("qoder_cn", rows)[0],
    ids[0],
    "不同来源的同内容事件必须分开"
  );
  assert.match(
    ids[0],
    /^chat2api:[0-9a-f]{16}:\d+$/,
    "身份格式 = 来源:内容摘要:出现序号"
  );
  // 窗口里插入一条新事件（下标全变）不改变其余事件的身份：身份不来自列表下标。
  const shifted = logRowIdentities("chat2api", [
    row("2026-10-07 07:41:33,000", "new"),
    ...rows,
  ]);
  assert.deepEqual(shifted.slice(1), ids, "插入新事件后其余事件的身份必须不变");
  assert.equal(logEventDigest("same"), logEventDigest("same"));
  assert.notEqual(logEventDigest("same"), logEventDigest("other"));
});

test("the overview copy button carries a render-time identity, not a list index", () => {
  assert.match(
    workbench,
    /data-wb-log-overview-copy="\$\{escapeHtml\(identity\)\}"/,
    "总览复制按钮必须携带渲染时身份"
  );
  assert.ok(
    !/data-wb-log-overview-copy="\$\{idx\}"/.test(workbench),
    "总览复制按钮又带上了列表下标"
  );
  // 格式不是契约：prettier 会把这一行折成多行（`get(` 与实参分行、实参前缩进），
  // 所以匹配允许任意空白；只要「按渲染时身份表取行」这一行为消失，断言仍会红。
  assert.match(
    workbench,
    /logOverviewRenderRows\.get\(\s*button\.dataset\.wbLogOverviewCopy\s*\)/,
    "点击复制必须按渲染时身份表取行"
  );
  assert.ok(
    !/this\.logOverviewEvents\(\)\.filter\(e => this\.logOverviewFilter === "all" \|\| e\.kind === this\.logOverviewFilter\)\[Number\(/.test(
      workbench
    ),
    "点击复制又按下标重算动态列表"
  );
  assert.ok(
    !/identity: `\$\{source\.source\}:\$\{row\.ts \?\? "unknown"\}:\$\{i\}`/.test(
      workbench
    ),
    "事件身份又嵌入了行下标"
  );
  assert.match(
    workbench,
    /data-wb-log-copy="\$\{escapeHtml\(identity\)\}"/,
    "详情复制按钮必须携带渲染时身份"
  );
  assert.match(
    workbench,
    /logDetailRenderRows\.get\(logCopyId\)/,
    "详情复制必须按渲染时身份表取行"
  );
});

test("the dead level filter is gone; the kind chips are the one classification entry", () => {
  assert.ok(
    !workbench.includes("wb-log-level"),
    "死代码 .wb-log-level 监听又回来了"
  );
  assert.ok(
    !workbench.includes('params.set("level"'),
    "死代码 level 查询参数又回来了"
  );
  assert.ok(!workbench.includes("d.level"), "死代码 detail.level 又回来了");
  assert.ok(!css.includes("wb-log-level"), "死代码 .wb-log-level 样式又回来了");
  assert.match(
    workbench,
    /data-wb-log-overview-filter="\$\{kind\}"/,
    "分类 chips 必须保留为唯一分类入口"
  );
  assert.match(
    workbench,
    /private logKindFilteredRows\(/,
    "分类筛选必须有唯一入口"
  );
});

test("the dead div-row styles are gone and the new table rows carry the kind colour", () => {
  assert.ok(!/\.wb-log-row\b/.test(css), "死样式 .wb-log-row 又回来了");
  assert.ok(!/\.wb-lv\b/.test(css), "死样式 .wb-lv 又回来了");
  assert.ok(!/\.wb-log-err\b/.test(css), "死样式 .wb-log-err 又回来了");
  assert.ok(
    !/\.wb-log-raw summary/.test(css),
    "永不渲染的 .wb-log-raw summary 样式又回来了"
  );
  assert.match(
    css,
    /\.wb-log-overview-table tbody tr\[class\*="wb-log-kind-"\] > td:first-child/,
    "总览新表格行必须画行级分类色条"
  );
  assert.match(
    css,
    /\.wb-log-event-table tbody tr\[class\*="wb-log-kind-"\] > td:first-child/,
    "详情新表格行必须画行级分类色条"
  );
  assert.match(
    css,
    /border-left: 3px solid var\(--wb-log-color\)/,
    "行级分类色条必须取分类色 token"
  );
  // 声明块可能被 prettier 展开成多行（每个声明一行），也可能保持单行；两者都是
  // 同一条规则。匹配因此按「选择器 + 声明块 + 声明」跨空白，规则被删或换成别的
  // 颜色 token 时仍然会红。
  assert.match(
    css,
    /\.wb-log-kind-error \.wb-log-msg \{\s*color: var\(--wb-status-err\);\s*\}/,
    "ERROR 行原文必须保持红字"
  );
  assert.match(
    css,
    /:root\[data-dark-surface="true"\] \.wb-log-kind-info \{\s*--wb-log-color:\s*#8dc7ff;\s*\}/,
    "暗色下信息蓝必须另有覆盖值"
  );
  assert.match(
    workbench,
    /tr class="wb-log-kind-\$\{kind\}"/,
    "总览表行必须带分类类名"
  );
  assert.match(
    workbench,
    /tr class="wb-log-kind-\$\{logKind\(row\)\}"/,
    "详情表行必须带分类类名"
  );
});

test("the empty-table sentence and the detail count name the kind filter", () => {
  assert.match(
    workbench,
    /没有符合分类筛选「\$\{escapeHtml\(this\.logKindFilterLabel\(\)\)\}」的事件/,
    "空表句必须点名实际分类筛选轴"
  );
  assert.ok(
    !workbench.includes("当前筛选无事件"),
    "空表句又退回不点名筛选轴的旧文案"
  );
  assert.match(
    workbench,
    /logKindFilteredRows\(detail\.rows\)/,
    "详情状态句必须按筛选后行计数"
  );
  assert.ok(
    !/bits\.push\(detail\.rows\.length \?/.test(workbench),
    "详情状态句又按未筛选行计数"
  );
});

test("the overview reads every registered category, not just each source's first", () => {
  // P3/R3：每个来源的**全部已注册分类**都是目标；只读首分类的截断不得回来。
  assert.ok(
    !workbench.includes("source.categories.slice(0, 1)"),
    "每个来源只读首分类的截断又回来了"
  );
  assert.ok(
    workbench.includes('fetch("/gw/usagesrv/api/console/log-overview"'),
    "总览必须使用服务端全部目标聚合口"
  );
  // 同一来源的多个目标合并成一份快照：一个目标失败不丢其它目标的行，也不把读取失败计成 0。
  // prettier 把对象字面量折成每个属性一行（`targets: x,` 与 `read: y,`），
  // 所以按属性名跨空白匹配；两个字段任一消失、或改成不来自 `source.` 的值，仍会红。
  assert.match(
    workbench,
    /targets:\s*source\.targets_total,\s*read:\s*source\.targets_read,/,
    "保留服务端目标覆盖状态"
  );
  assert.ok(
    workbench.includes(
      'snapshot?.status !== "ok" && snapshot?.status !== "partial"'
    ),
    "部分目标未读仍显示已读行"
  );
});

test("the overview orders by normalized time, never by the raw stamp", () => {
  // P3/R3：原始字符串比较把同日凌晨的 ISO 行排到晚上日志之前——这个反例不得回来。
  assert.ok(
    !/\(b\.row\.ts \?\? ""\)\.localeCompare\(a\.row\.ts \?\? ""\)/.test(
      workbench
    ),
    "总览又按原始时间字符串排序"
  );
  assert.match(
    workbench,
    /logTsEpoch\(a\.row\.ts\)/,
    "总览必须按归一化时间排序"
  );
  assert.match(
    workbench,
    /logTsEpoch\(b\.row\.ts\)/,
    "总览必须按归一化时间排序"
  );
  assert.ok(
    "2026-10-07T02:30:00+08:00" > "2026-10-07 23:10:00",
    "反例前提：原始字符串比较确实把同日凌晨 ISO 排前"
  );
  assert.ok(
    logTsEpoch("2026-10-07 23:10:00") > logTsEpoch("2026-10-07T02:30:00+08:00"),
    "同日凌晨 ISO 必须排在晚上日志之后"
  );
  assert.equal(
    logTsEpoch("2026-10-07 23:10:00.945"),
    logTsEpoch("2026-10-07 23:10:00.9456789"),
    "小数部分按 6 位截断（与后端同口径）"
  );
  assert.equal(
    logTsEpoch("2026-10-07T02:30:00Z"),
    Date.UTC(2026, 9, 7, 2, 30, 0) / 1000,
    "带 Z 的时间戳按 UTC 换算"
  );
  assert.equal(logTsEpoch("2026-13-45 10:00:00"), null, "不存在的日期不猜算");
  assert.equal(logTsEpoch(null), null, "缺席的时间戳是未知，不猜算");
  assert.equal(
    logTsEpoch("not-a-time"),
    null,
    "无法识别的时间戳是未知，不猜算"
  );
});
