// Log-presentation checks for the retained local UM console (运行日志).
//
// The workbench log overview was retired with the workbench escape hatch
// (#27); the surviving log surface is `src/local/um-ui/requests.ts`. These
// checks bind that shipped renderer: an out-of-contract level reads 未分级, an
// unparseable instant reads 时间未知, free text is escaped, and the level chips
// are the one classification entry (no dead `.wb-log-level` filter).
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
      `export { requestsHtml } from "./src/local/um-ui/requests.ts";`,
      `export { unwiredState } from "./src/local/um-ui/contract.ts";`,
    ].join("\n"),
    resolveDir: fileURLToPath(new URL("..", import.meta.url)),
    sourcefile: "log-presentation-entry.ts",
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
    __CONSOLE_CANDIDATE__: '"true"',
  },
}).outputFiles[0].text;
const module = { exports: {} };
vm.runInContext(
  code,
  vm.createContext({ module, exports: module.exports, console })
);
const um = module.exports;

const ready = (data) => ({
  state: "ready",
  data,
  at: Date.UTC(2026, 10, 1, 2, 0),
});
const HOSTILE = `<img src=x onerror="alert(1)">`;

function logsState(lines, over = {}) {
  const state = um.unwiredState().requests;
  state.logs.page = ready({
    lines,
    sources: ["um"],
    scope: "北京时间今天 00:00 起的 UM 日志",
    earlier: null,
    ...over,
  });
  return state;
}
const ui = {
  view: "logs",
  search: "",
  expanded: null,
  openSteps: new Set(),
  detailAsked: null,
  earlier: { records: null, logs: null, maintenance: null },
  earlierFailed: { records: null, logs: null, maintenance: null },
};

test("an unknown log level reads 未分级, never a guessed level", () => {
  const html = um.requestsHtml(
    logsState([
      {
        key: "l1",
        at: Date.UTC(2026, 9, 31, 2, 5),
        level: "trace",
        source: "um",
        text: "stream end status=ok",
        requestId: null,
      },
    ]),
    ui
  );
  assert.match(html, /未分级/);
  assert.doesNotMatch(html, />trace</);
});

test("an unparseable instant reads 时间未知, and one bad row never freezes the page", () => {
  const html = um.requestsHtml(
    logsState([
      {
        key: "l1",
        at: Number.NaN,
        level: "info",
        source: "um",
        text: "first",
        requestId: null,
      },
      {
        key: "l2",
        at: Date.UTC(2026, 9, 31, 2, 5),
        level: "warning",
        source: "um",
        text: "second",
        requestId: null,
      },
    ]),
    ui
  );
  assert.match(html, /时间未知/);
  assert.match(html, /未分级/);
  assert.match(html, /second/);
  assert.doesNotMatch(html, /NaN/);
});

test("log free text is escaped; the level chips are the only classification entry", () => {
  const html = um.requestsHtml(
    logsState([
      {
        key: HOSTILE,
        at: Date.UTC(2026, 9, 31, 2, 5),
        level: "error",
        source: HOSTILE,
        text: HOSTILE,
        requestId: null,
      },
    ]),
    ui
  );
  assert.doesNotMatch(html, /<img|<script/);
  assert.match(html, /&lt;img/);
  for (const level of ["error", "warn", "info", "debug"]) {
    assert.match(html, new RegExp(`data-um-level="${level}"`));
  }
  assert.doesNotMatch(html, /wb-log-level|wb-log-overview/);
});

test("the log scope and page size stay honest about what was read", () => {
  const html = um.requestsHtml(
    logsState(
      [
        {
          key: "l1",
          at: Date.UTC(2026, 9, 31, 2, 5),
          level: "info",
          source: "um",
          text: "line",
          requestId: null,
        },
      ],
      { earlier: `c"1` }
    ),
    ui
  );
  assert.match(html, /读取范围：北京时间今天 00:00 起的 UM 日志/);
  assert.match(html, /本页 1 行/);
  assert.match(html, /data-um-learlier="c&quot;1">加载更早的记录</);
});
