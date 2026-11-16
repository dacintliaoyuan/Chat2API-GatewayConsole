// UM console views (src/local/um-ui/) against controller data that is typed
// but not trusted: a NaN instant, closed-set values outside the contract,
// markup in free text, a catalog that repeats an id. The views must render
// (never throw and leave the page frozen on older figures), escape every
// string, say 「未知」 where they do not understand a value, and gate each
// control on the same reason they draw it with. The trend's series and
// colours are checked here too; the state-driven browser behaviour (repaint,
// paging retry) is in scripts/console-gate/um-state.mjs.
//
//   node --test scripts/check-um-views.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const require = createRequire(new URL("../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const code = buildSync({
  stdin: {
    contents: [
      `export * from "./src/local/um-ui/format.ts";`,
      `export { requestsHtml, RESULT } from "./src/local/um-ui/requests.ts";`,
      `export { modelsHtml, catalogIds, testBlockReason, stopBlockReason, refreshBlockReason } from "./src/local/um-ui/models.ts";`,
      `export { TrendView, SeriesColors, monotonePath, drawableRuns } from "./src/local/um-ui/charts.ts";`,
      `export { unwiredState } from "./src/local/um-ui/contract.ts";`,
      `export { overviewHtml } from "./src/local/um-ui/overview.ts";`,
      `export { loginFrom } from "./src/local/um-data/view-model.ts";`,
      `export { UmDataController } from "./src/local/um-data/um-data-controller.ts";`,
    ].join("\n"),
    resolveDir: fileURLToPath(new URL("..", import.meta.url)),
    sourcefile: "um-views-entry.ts",
    loader: "ts",
  },
  bundle: true,
  format: "cjs",
  platform: "browser",
  write: false,
  loader: { ".css": "empty" },
  define: {
    "import.meta.env.MODE": '"test"',
    __CONSOLE_ROLE__: '"local"',
    __CONSOLE_CANDIDATE__: "true",
  },
}).outputFiles[0].text;
const module = { exports: {} };
vm.runInContext(
  code,
  vm.createContext({
    module,
    exports: module.exports,
    console,
    queueMicrotask,
    setTimeout,
    clearTimeout,
  })
);
const um = module.exports;

const exact = (value) => ({ kind: "exact", value });
const ready = (data) => ({
  state: "ready",
  data,
  at: Date.UTC(2026, 10, 1, 2, 0),
});
const open = { enabled: true };
const HOSTILE = `<img src=x onerror="alert(1)">`;

test("time formatters read 时间未知 for values that are not instants", () => {
  for (const bad of [
    Number.NaN,
    Number.POSITIVE_INFINITY,
    1e20,
    "soon",
    undefined,
  ]) {
    assert.equal(um.when(bad), um.TIME_UNKNOWN);
    assert.equal(um.clock(bad), um.TIME_UNKNOWN);
    assert.equal(um.dayKey(bad), "");
  }
  assert.equal(um.monthDay(undefined), "日期未知");
  assert.equal(um.dayTitle("2026-11"), "日期未知");
  assert.equal(um.monthDay("2026-11-01"), "11/1");
  assert.match(
    um.when(Date.UTC(2026, 9, 31, 2, 5), Date.UTC(2026, 10, 1, 2, 0)),
    /^10\/31 10:05$/
  );
});

test("closed-set words fall back for values outside the contract, prototype keys included", () => {
  assert.equal(um.wordOf(um.SPEED_BASIS, "constructor", "x"), "x");
  assert.equal(um.speedBasisWord("odd"), "口径未知");
  assert.equal(um.failureWord("teapot"), "读取失败");
  assert.equal(um.readingText({ kind: "vanished" }, String), "未知");
});

test("request rows with out-of-contract values render, escaped, without throwing", () => {
  const state = um.unwiredState().requests;
  const row = {
    key: `k"<1>`,
    requestId: `req"<x>`,
    at: Number.NaN,
    model: HOSTILE,
    result: "weird",
    resultNote: "<b>note</b>",
    httpStatus: "200<script>",
    input: exact(1),
    output: exact(2),
    cacheRead: { kind: "unreported" },
    reasoning: { kind: "unknown" },
    durationMs: exact(1200),
    firstTokenMs: { kind: "missing" },
    speed: exact(12.5),
    speedBasis: "odd",
    retries: [{ at: Number.NaN, note: "<i>retry</i>" }],
    error: { code: "E<1>", message: "<i>m</i>" },
  };
  state.page = ready({ rows: [row], scope: "<u>scope</u>", earlier: `c"1` });
  state.models = [HOSTILE];
  state.logs.page = ready({
    lines: [
      {
        key: "l1",
        at: Number.NaN,
        level: "warning",
        source: HOSTILE,
        text: "<script>x</script>",
        requestId: null,
      },
    ],
    sources: [HOSTILE],
    scope: "s",
    earlier: null,
  });
  state.maintenance = ready({
    rows: [
      {
        key: "m1",
        action: "reboot",
        state: "exploded",
        at: Number.NaN,
        finishedAt: null,
        steps: [{ at: 1, note: "<b>s</b>" }],
        result: "<b>r</b>",
      },
    ],
    scope: "s",
    earlier: null,
  });
  const none = { records: null, logs: null, maintenance: null };
  const ui = {
    view: "records",
    search: "",
    expanded: row.key,
    openSteps: new Set(["m1"]),
    detailAsked: null,
    earlier: none,
    earlierFailed: none,
  };
  for (const view of ["records", "logs", "maintenance"]) {
    const html = um.requestsHtml(state, { ...ui, view });
    assert.doesNotMatch(
      html,
      /<img|<script|<b>|<i>|<u>/,
      `${view}: markup from data must be escaped`
    );
    assert.match(html, /时间未知/, `${view}: a NaN instant reads 时间未知`);
  }
  const records = um.requestsHtml(state, ui);
  assert.match(records, /口径未知/);
  assert.match(
    records,
    new RegExp(`>${um.RESULT.unknown.word}<`),
    "an unknown result reads 未知"
  );
  assert.match(
    records,
    /data-um-detail=/,
    "an open row without its logs offers to read them"
  );
  assert.match(
    um.requestsHtml(state, { ...ui, view: "logs" }),
    /未分级/,
    "an unknown log level reads 未分级"
  );
  assert.match(
    um.requestsHtml(state, { ...ui, detailAsked: row.key }),
    /正在请求关联日志/
  );
  assert.match(
    um.requestsHtml(state, { ...ui, earlier: { ...none, records: `c"1` } }),
    /正在读取更早的记录…/,
    "an asked cursor is not offered twice"
  );
  // The same literal cursor asked by another view does not close this one.
  const other = um.requestsHtml(state, {
    ...ui,
    earlier: { ...none, logs: `c"1` },
  });
  assert.match(other, /data-um-earlier="c&quot;1">加载更早的记录</);
  // After a failed read the kept page offers the same cursor again, with why.
  state.page = {
    state: "stale",
    data: state.page.data,
    at: state.page.at,
    failure: { kind: "error", reason: "<b>timeout</b>", at: 2 },
  };
  const failed = um.requestsHtml(state, {
    ...ui,
    earlierFailed: {
      ...none,
      records: { cursor: `c"1`, reason: "读取失败：<b>timeout</b>" },
    },
  });
  assert.match(
    failed,
    /data-um-earlier="c&quot;1">重试加载更早的记录</,
    "a failed read can be retried"
  );
  assert.match(failed, /没有成功（读取失败：&lt;b&gt;timeout&lt;\/b&gt;）/);
  assert.doesNotMatch(failed, /<b>timeout/);
});

test("最近测试 is a compact context summary: figures kept whole, the rest in 详情", () => {
  const models = um.unwiredState().models;
  const row = (id, lastContext) => ({
    id,
    name: id,
    context: {
      declared: null,
      compatDefault: null,
      probedChars: null,
      tokenLowerBound: null,
    },
    thinking: { levels: [], source: "catalog", defaultLevel: null },
    lastContext,
  });
  const at = Date.UTC(2026, 9, 31, 6, 21);
  models.catalog = ready({
    models: [
      row("a", {
        at,
        ok: true,
        summary:
          "通过 32万 字符；上游报告 ≥96,214 Token；64万 被拒（超出上下文）",
      }),
      row("b", {
        at,
        ok: false,
        summary: `HTTP 502：${HOSTILE}；上下文探测不自动重试`,
      }),
      row("c", null),
    ],
    fetchedAt: at,
    source: "UM",
  });
  const ui = {
    selected: new Set(),
    expanded: new Set(),
    runDetail: new Set(),
    historyOpen: false,
    settingsOpen: false,
    awaiting: false,
  };
  const html = um.modelsHtml(models, ui);
  const cells = html
    .split('<td class="um-col-last"')
    .slice(1)
    .map((c) => c.slice(0, c.indexOf("</td>")));
  assert.equal(cells.length, 3);
  assert.match(
    cells[0],
    /<b>上下文<\/b>通过 <span class="um-nb">32万 字符<\/span><\/p>/,
    "上下文 lists only its conclusion"
  );
  assert.doesNotMatch(
    cells[0].replace(/title="[^"]*"/g, ""),
    /96,214|被拒/,
    "the further evidence is not crammed into the cell"
  );
  assert.match(
    cells[0],
    /title="上下文：通过 32万 字符；上游报告 ≥96,214 Token；64万 被拒（超出上下文）"/,
    "the full wording stays reachable"
  );
  assert.match(
    cells[0],
    /data-um-model-more="a" aria-expanded="false" aria-controls="um-model-0">详情</,
    "详情 opens the row's detail"
  );
  assert.match(cells[1], /data-tone="err"/);
  assert.match(
    cells[1],
    /<span class="sr-only">未通过：<\/span>HTTP <span class="um-nb">502<\/span>/,
    "a failure is said in words, not by colour alone"
  );
  assert.doesNotMatch(cells[1], /<img/, "summaries are escaped");
  assert.match(cells[2], /未测/);
  assert.doesNotMatch(
    cells[2],
    /详情/,
    "nothing to open when nothing was tested"
  );
});

test("retired 测速 has no action, no button and no read face", () => {
  const models = um.unwiredState().models;
  assert.equal(
    "speedtest" in models.actions,
    false,
    "the retired action is not part of the model actions"
  );
  const row = {
    id: "a",
    name: "a",
    context: {
      declared: null,
      compatDefault: null,
      probedChars: null,
      tokenLowerBound: null,
    },
    thinking: { levels: [], source: "catalog", defaultLevel: null },
    lastContext: null,
  };
  models.catalog = ready({
    models: [row],
    fetchedAt: Date.UTC(2026, 9, 31, 6, 21),
    source: "UM",
  });
  models.actions = { refresh: open, context: open, stop: open };
  const ui = {
    selected: new Set(["a"]),
    expanded: new Set(),
    runDetail: new Set(),
    historyOpen: false,
    settingsOpen: false,
    awaiting: false,
  };
  const html = um.modelsHtml(models, ui);
  assert.doesNotMatch(html, /data-um-test="speedtest"/);
  assert.doesNotMatch(html, /测速/);
  assert.match(html, /data-um-test="context"/);
  assert.equal(um.testBlockReason(models, ui, "context"), null);
});

test("trend colours follow the models in the window; no speed series is dropped", () => {
  const day = (v = 10) => ({
    tokens: exact(v),
    requests: exact(1),
    speed: exact(v),
  });
  const model = (name, v) => ({
    model: name,
    daily: [day(v), day(v)],
    total: day(v),
    failed: exact(0),
  });
  const trend = (models) => ({
    range: "7d",
    days: ["2026-10-31", "2026-11-01"],
    all: { daily: [day(), day()], total: day() },
    models,
    speedBasis: "whole_request",
  });
  const colors = new um.SeriesColors();
  const view = new um.TrendView(colors);
  view.set(trend(["a", "b", "c", "d"].map((m) => model(m))), "speed");
  assert.equal(colors.slot("d"), 4);
  // A window with only a newcomer: the departed models' slots are free again.
  view.set(trend([model("newcomer", 42)]), "speed");
  assert.notEqual(
    colors.slot("newcomer"),
    null,
    "a newcomer gets a freed colour slot"
  );
  assert.match(view.tipHtml(0), /newcomer/);
  // Six models at once: four colours, and the other two still drawn (neutral) for 速度.
  const six = ["m1", "m2", "m3", "m4", "m5", "m6"].map((m, i) =>
    model(m, 10 + i)
  );
  view.set(trend(six), "speed");
  for (const { model: m } of six)
    assert.match(view.tipHtml(0), new RegExp(`>${m}<`), `速度 keeps ${m}`);
  assert.match(
    view.tipHtml(0),
    /data-um-series-name="m1"/,
    "a tooltip row names its series by attribute"
  );
  view.set(trend(six), "tokens");
  assert.match(
    view.tipHtml(0),
    /其他模型（2）/,
    "additive metrics fold the rest into 其他模型"
  );
  // The signature follows the figures, not the object: an in-place change shows.
  const t = trend(six);
  view.set(t, "tokens");
  const before = view.signature;
  t.models[0].daily[0].tokens.value += 1000;
  view.set(t, "tokens");
  assert.notEqual(
    view.signature,
    before,
    "an in-place change moves the signature"
  );
  view.set(t, "tokens", "line");
  assert.equal(view.lines, true);
  view.set(t, "speed", "bar");
  assert.equal(view.lines, true, "速度 is a line whatever the shape asked");
});

/** Parse a monotonePath result and sample each cubic at 50 steps. */
function sampleCurve(d) {
  const tokens = d.match(/[MC]|-?\d+(?:\.\d+)?/g) ?? [];
  let i = 0,
    x = 0,
    y = 0;
  const points = [];
  while (i < tokens.length) {
    if (tokens[i++] === "M") {
      x = Number(tokens[i++]);
      y = Number(tokens[i++]);
      points.push({ x, y, end: true });
      continue;
    }
    const [x1, y1, x2, y2, x3, y3] = tokens.slice(i, i + 6).map(Number);
    i += 6;
    for (let k = 1; k <= 50; k++) {
      const t = k / 50,
        u = 1 - t;
      points.push({
        x:
          u * u * u * x +
          3 * u * u * t * x1 +
          3 * u * t * t * x2 +
          t * t * t * x3,
        y:
          u * u * u * y +
          3 * u * u * t * y1 +
          3 * u * t * t * y2 +
          t * t * t * y3,
      });
    }
    x = x3;
    y = y3;
    points.push({ x, y, end: true });
  }
  return points;
}

test("monotone curves pass every sample and never invent a peak or a trough", () => {
  assert.equal(um.monotonePath([]), null);
  assert.equal(
    um.monotonePath([{ x: 0, y: 5 }]),
    null,
    "one point is not a curve"
  );
  const straight = um.monotonePath([
    { x: 0, y: 10 },
    { x: 10, y: 20 },
  ]);
  assert.match(straight, /^M0\.00,10\.00C/, "two points draw a segment");
  const ys = sampleCurve(straight).map((p) => p.y);
  assert.ok(
    Math.abs(Math.min(...ys) - 10) < 1e-6 &&
      Math.abs(Math.max(...ys) - 20) < 1e-6,
    "two points draw a straight segment"
  );
  // Screen y is a negative affine of the value: sampling it back shows the value range.
  const pixels = (values) => values.map((v, i) => ({ x: i * 10, y: -v }));
  for (const [values, lo, hi] of [
    [[0, 10, 10, 0], 0, 10],
    [[0, 0, 100, 0, 0], 0, 100],
    [[1, 3, 2, 7, 4], 1, 7],
    [[5, 5, 5], 5, 5],
    [[0, 0, 0], 0, 0],
  ]) {
    const d = um.monotonePath(pixels(values));
    const curve = sampleCurve(d);
    const got = curve.map((p) => -p.y);
    assert.ok(
      Math.min(...got) >= lo - 1e-6 && Math.max(...got) <= hi + 1e-6,
      `${JSON.stringify(values)} overshoots: ${Math.min(...got)}..${Math.max(...got)}`
    );
    const ends = curve.filter((p) => p.end);
    assert.equal(ends.length, values.length, "one endpoint per sample");
    ends.forEach((p, i) => {
      assert.ok(
        Math.abs(p.x - i * 10) < 0.01 && Math.abs(p.y + values[i]) < 0.01,
        `${JSON.stringify(values)} misses sample ${i}`
      );
    });
  }
});

test("only consecutive known values form a run; a lone value is not one", () => {
  const runs = um.drawableRuns([
    exact(1),
    exact(0),
    { kind: "missing" },
    exact(2),
    { kind: "atLeast", value: 3 },
    { kind: "unreported" },
    exact(4),
  ]);
  assert.deepEqual(
    Array.from(runs, (run) => Array.from(run, (p) => p.index)),
    [[0, 1], [3, 4], [6]],
    "a gap splits runs; a real 0 continues one"
  );
  assert.deepEqual(
    Array.from(runs[0], (p) => p.value),
    [1, 0],
    "a real 0 is a value"
  );
  assert.equal(
    um.drawableRuns([exact(0), exact(0)]).length,
    1,
    "real zeros draw a line"
  );
  assert.equal(
    um.drawableRuns([{ kind: "exact", value: Number.NaN }, exact(1)]).length,
    1,
    "a non-finite value is a gap"
  );
  assert.equal(
    um.drawableRuns([{ kind: "unknown" }, { kind: "missing" }]).length,
    0,
    "nothing known, nothing drawn"
  );
});

test("a line window of lone values says so instead of 0 or 没有用量", () => {
  const bucket = (v, o = {}) => ({
    tokens: exact(o.tokens ?? v),
    requests: exact(1),
    speed: o.speed ?? exact(40),
  });
  const model = (name, speeds) => ({
    model: name,
    daily: speeds.map((s) => ({ ...bucket(10), speed: s })),
    total: bucket(10),
    failed: exact(0),
  });
  const trend = (models) => ({
    range: "7d",
    days: ["2026-10-31", "2026-11-01"],
    all: { daily: [bucket(10), bucket(10)], total: bucket(10) },
    models,
    speedBasis: "whole_request",
  });
  const view = new um.TrendView(new um.SeriesColors());
  const missing = { kind: "missing" };
  view.set(trend([model("solo", [exact(30), missing, exact(50)])]), "speed");
  assert.equal(view.state, "insufficient");
  assert.match(view.label(), /连续有效数据不足/);
  assert.doesNotMatch(view.label(), /没有可画/);
  view.set(trend([model("zero", [exact(0), exact(0)])]), "speed");
  assert.equal(view.state, "zero", "all real zeros are not an empty window");
  assert.doesNotMatch(view.label(), /没有可画/);
  assert.match(
    view.label(),
    /都是 0/,
    "the accessible summary says the values are a real 0"
  );
  view.set(trend([model("none", [missing, missing])]), "speed");
  assert.equal(view.state, "empty");
  assert.match(view.label(), /没有可画/);
  // Bars: a lone value is still a bar, so the window is not 「insufficient」.
  view.set(trend([model("solo", [exact(30), missing])]), "tokens");
  assert.equal(view.state, "drawn");
});

test("ranking keeps whole counts, names the bar scale and flags folded series", () => {
  const bucket = (v, o = {}) => ({
    tokens: exact(o.tokens ?? v),
    requests: o.requests ?? exact(1),
    speed: o.speed ?? exact(40),
  });
  const model = (name, v, o = {}) => ({
    model: name,
    daily: [bucket(v, o), bucket(v, o)],
    total: bucket(v, o),
    failed: o.failed ?? exact(0),
  });
  const trend = (models) => ({
    range: "7d",
    days: ["2026-10-31", "2026-11-01"],
    all: { daily: [bucket(10), bucket(10)], total: bucket(10) },
    models,
    speedBasis: "whole_request",
  });
  const view = new um.TrendView(new um.SeriesColors());
  const long =
    "um-extended-context-model-with-a-very-long-identifier-2026-10-15";
  view.set(
    trend([
      model(long, 1_234_567),
      model("zero", 0),
      model("unknown-fail", 500, { failed: { kind: "unreported" } }),
    ]),
    "tokens"
  );
  const html = view.rankingHtml();
  assert.match(
    html,
    /1,234,567/,
    "Token counts are whole thousands, not 万/亿"
  );
  assert.doesNotMatch(html, /123\.5万/);
  assert.match(
    html,
    /data-um-rank="um-extended-context/,
    "each row carries its model id"
  );
  assert.match(
    html,
    new RegExp(`title="${long}"`),
    "the full id stays reachable"
  );
  assert.match(html, /width:100\.0%/, "the top row is the bar's 100%");
  assert.match(html, /width:0\.0%/, "a real 0 draws no fill");
  assert.match(
    html,
    /条长＝相对本时段最高，非合计占比/,
    "the bar scale is named, not a share"
  );
  assert.match(html, /失败未报告/, "an unreported failure is not 0");
  assert.match(
    html,
    /data-um-series="0"/,
    "a separately drawn model links to its series"
  );
  // A zero-value model draws no bar in bar mode, so its row must not link and
  // must not dim the other series.
  assert.doesNotMatch(
    html,
    /data-um-rank="zero"[^>]*data-um-series/,
    "a 0-value bar has nothing to link to"
  );
  // A lower bound or an estimate keeps its ≥ / ≈ sign in the failure cell.
  view.set(
    trend([
      model("lower-fail", 900, { failed: { kind: "atLeast", value: 3 } }),
      model("est-fail", 800, { failed: { kind: "estimate", value: 4 } }),
    ]),
    "tokens"
  );
  const qualified = view.rankingHtml();
  assert.match(
    qualified,
    /≥3 次失败/,
    "an atLeast failure keeps its lower-bound sign"
  );
  assert.match(qualified, /≈4 次失败/, "an estimate failure keeps its ≈ sign");
  // Six models: the fifth and sixth fold into 其他模型（2）, and the rows say so.
  view.set(
    trend(
      ["m1", "m2", "m3", "m4", "m5", "m6"].map((n, i) => model(n, 1000 - i))
    ),
    "tokens"
  );
  const folded = view.rankingHtml();
  assert.match(folded, /其他模型（2）/);
  assert.match(
    folded,
    /并入「其他模型（2）」合计/,
    "a folded row is not passed off as its own curve"
  );
  assert.match(folded, /另有 1 个模型未列出/);
  assert.doesNotMatch(
    folded,
    /data-um-series="4"/,
    "the folded row links to nothing"
  );
  // A missing speed keeps its own word instead of a bare 缺失.
  view.set(trend([model("slow", 10, { speed: { kind: "missing" } })]), "speed");
  assert.match(view.rankingHtml(), /无速度样本/);
});

test("the scope only claims a dashed curve when one is actually drawn", () => {
  const bucket = (o = {}) => ({
    tokens: o.tokens ?? exact(10),
    requests: o.requests ?? exact(1),
    speed: o.speed ?? exact(40),
  });
  const model = (name, days) => ({
    model: name,
    daily: days.map((o) => bucket(o)),
    total: bucket(),
    failed: exact(0),
  });
  const trend = (models) => ({
    range: "7d",
    days: ["2026-10-31", "2026-11-01"],
    all: { daily: [bucket(), bucket()], total: bucket() },
    models,
    speedBasis: "whole_request",
  });
  const view = new um.TrendView(new um.SeriesColors());
  const lower = model("lower", [
    { tokens: { kind: "atLeast", value: 3 } },
    { tokens: { kind: "atLeast", value: 5 } },
  ]);
  // Bar mode: a lower bound is a bar; there is no dashed line to explain.
  view.set(trend([lower]), "tokens");
  assert.equal(view.quality.lower, false, "a bar carries no dashed line");
  // Line mode with two consecutive lower bounds: the short dash is real.
  view.set(trend([lower]), "tokens", "line");
  assert.equal(view.quality.lower, true);
  // Line mode, but the lower bound is a lone value: no curve, no dash claim.
  view.set(
    trend([
      model("lone", [
        { tokens: { kind: "atLeast", value: 3 } },
        { tokens: { kind: "missing" } },
      ]),
    ]),
    "tokens",
    "line"
  );
  assert.equal(view.quality.lower, false, "a lone value is not drawn");
  // Same rule for an estimate, which stays solid and is named in the tooltip.
  const estimated = model("est", [
    { requests: { kind: "estimate", value: 1 } },
    { requests: { kind: "estimate", value: 2 } },
  ]);
  view.set(trend([estimated]), "requests");
  assert.equal(view.quality.estimated, false);
  view.set(trend([estimated]), "requests", "line");
  assert.equal(view.quality.estimated, true);
});

test("model views dedupe the catalog and gate each control on the reason they draw", () => {
  const models = um.unwiredState().models;
  const model = (id, name = id) => ({
    id,
    name,
    context: {
      declared: null,
      compatDefault: null,
      probedChars: null,
      tokenLowerBound: null,
    },
    thinking: { levels: [], source: "elsewhere", defaultLevel: null },
    lastContext: null,
  });
  models.catalog = ready({
    models: [model("gpt-5.2", HOSTILE), model("gpt-5_2"), model("gpt-5.2")],
    fetchedAt: Number.NaN,
    source: "UM",
  });
  models.actions = {
    refresh: open,
    context: open,
    stop: open,
  };
  models.notice = { tone: `"><script>`, text: HOSTILE, at: 1 };
  const ui = {
    selected: new Set(),
    expanded: new Set(["gpt-5.2", "gpt-5_2"]),
    runDetail: new Set(),
    historyOpen: false,
    settingsOpen: false,
    awaiting: false,
  };
  assert.deepEqual(Array.from(um.catalogIds(models)), ["gpt-5.2", "gpt-5_2"]); // the bundle runs in its own realm
  assert.equal(
    um.testBlockReason(models, ui, "context"),
    "先勾选至少一个模型"
  );
  ui.selected.add("gpt-5.2");
  assert.equal(um.testBlockReason(models, ui, "context"), null);
  assert.equal(
    um.testBlockReason(models, { ...ui, awaiting: true }, "context"),
    "正在提交"
  );
  models.run = {
    id: "r1",
    startedAt: 1,
    state: "running",
    stopRequested: false,
    budget: { used: "3<b>", limit: 20 },
    items: [
      {
        model: HOSTILE,
        state: "exploded",
        stage: null,
        summary: null,
        detail: null,
      },
    ],
    note: null,
  };
  assert.match(um.testBlockReason(models, ui, "context"), /已有测试在进行/);
  assert.equal(um.stopBlockReason(models, "r1"), null);
  assert.equal(
    um.stopBlockReason(models, "r0"),
    "这次测试已经结束",
    "a stop for another run is closed"
  );
  assert.equal(
    um.stopBlockReason(
      { ...models, run: { ...models.run, stopRequested: true } },
      "r1"
    ),
    "已请求停止"
  );
  assert.equal(
    um.refreshBlockReason({
      ...models,
      refresh: { state: "refreshing", since: 1 },
    }),
    "同一操作正在进行，等待它结束"
  );
  const html = um.modelsHtml(models, ui);
  assert.doesNotMatch(
    html,
    /<img|<script|3<b>/,
    "markup from data must be escaped"
  );
  assert.match(html, /3&lt;b&gt;\/20/);
  assert.equal(
    new Set([...html.matchAll(/id="(um-model-\d+)"/g)].map((m) => m[1])).size,
    [...html.matchAll(/id="um-model-\d+"/g)].length,
    "expanded rows get distinct ids"
  );
  assert.match(
    html,
    /data-tone="neutral" role="status"/,
    "a notice tone outside the set falls back to neutral"
  );
  assert.match(html, /状态未知/);
  assert.match(html, /来源未知/);
  assert.doesNotMatch(
    html,
    /不支持/,
    "an unknown thinking source is not read as 「不支持」"
  );
});

test("the login card keeps on-demand login and adds the forced visible entry", () => {
  const overview = um.unwiredState().overview;
  overview.login = ready({
    state: "valid",
    word: "有效",
    detail: "当前认证有效",
    checkedAt: 1,
  });
  overview.actions.login = open;
  const valid = um.overviewHtml(overview, { metric: "tokens", line: false }, {});
  assert.doesNotMatch(valid, /data-um-act="login"/);
  assert.match(valid, /data-um-force-login/);
  assert.match(valid, /强制重新登录（可见窗口）/);
  overview.login = ready({
    state: "needs_login",
    word: "需要登录",
    detail: "当前认证需要重新登录",
    checkedAt: 1,
  });
  const needs = um.overviewHtml(overview, { metric: "tokens", line: false }, {});
  assert.match(needs, /data-um-act="login"/);
  assert.match(needs, /检查并按需登录/);
  assert.match(needs, /data-um-force-login/);
});

test("a Duo push wait shows the takeover status and opens a visible window", () => {
  const result = {
    state: "succeeded",
    operation: {
      result: {
        state: "logging_in",
        label: "登录中（需人工：duo_prompt_waiting）",
        error: null,
        checked_epoch: 1,
        login: {
          phase: "awaiting_duo",
          origin: "https://api-1234.duosecurity.com/prompt/xyz",
          manual_reason: "duo_prompt_waiting",
        },
      },
    },
    operationId: "op-duo-login",
    reason: null,
  };
  const login = um.loginFrom(result);
  assert.equal(login.takeover, true);
  assert.equal(login.manualReason, "duo_prompt_waiting");
  assert.equal(login.phase, "awaiting_duo");
  assert.equal(login.word, "等待本人确认 Duo 推送");
  assert.match(login.detail, /本轮仍在等待/);
  const overview = um.unwiredState().overview;
  overview.login = ready(login);
  overview.actions.login = open;
  const html = um.overviewHtml(overview, { metric: "tokens", line: false }, {});
  assert.match(html, /等待本人确认 Duo 推送/);
  assert.match(html, /打开可见窗口/);
  assert.match(html, /data-um-force-login/);
  assert.doesNotMatch(html, /强制重新登录（可见窗口）/);
});

test("a forced login event reaches account.login with force and visible", async () => {
  const calls = [];
  let controller;
  const result = {
    state: "succeeded",
    operation: { result: {} },
    operationId: "op-forced-login",
    reason: null,
  };
  const client = {
    runAction: async (action, target, params, options) => {
      calls.push({ action, target, params, options });
      controller.disposed = true;
      return result;
    },
  };
  const gate = {
    verdict: () => ({ state: "confirmed", reason: "" }),
    onChange: () => () => {},
  };
  controller = new um.UmDataController({ gate, client, summaryClient: client });
  controller.capabilities = new Map([
    [
      "account.login",
      {
        action: "account.login",
        enabled: true,
        cancelable: true,
        disabledReason: null,
        description: null,
      },
    ],
  ]);
  controller.handleEvent({ type: "um.login", force: true, visible: true });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].action, "account.login");
  assert.equal(calls[0].target, "account");
  assert.equal(calls[0].params.force, true);
  assert.equal(calls[0].params.visible, true);
});
