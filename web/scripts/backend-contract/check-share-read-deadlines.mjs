import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const require = createRequire(new URL("../../package.json", import.meta.url));
const { buildSync } = require("esbuild");
const code = buildSync({
  entryPoints: [
    fileURLToPath(new URL("../../src/workbench.ts", import.meta.url)),
  ],
  bundle: true,
  format: "cjs",
  platform: "browser",
  write: false,
  loader: { ".css": "empty" },
  define: {
    "import.meta.env.MODE": '"test"',
    __CONSOLE_ROLE__: '"local"',
    __CONSOLE_CANDIDATE__: "false",
  },
}).outputFiles[0].text;

function consumer(fetcher) {
  const timers = new Map();
  let timerId = 0;
  const module = { exports: {} };
  const context = vm.createContext({
    module,
    exports: module.exports,
    console,
    AbortController,
    URL,
    HTMLElement: class {},
    customElements: { define() {}, get() {} },
    document: {
      createElement(tag) {
        assert.equal(tag, "canvas");
        return { getContext: () => ({ measureText: () => ({ width: 0 }) }) };
      },
    },
    fetch: fetcher,
    setTimeout(callback) {
      const id = ++timerId;
      timers.set(id, callback);
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    matchMedia() {
      return { matches: false, addEventListener() {} };
    },
  });
  context.window = context;
  vm.runInContext(code, context);
  return {
    workbench: Object.create(module.exports.Workbench.prototype),
    timers,
  };
}

const flush = async () => {
  for (let i = 0; i < 24; i++) await Promise.resolve();
};

for (const stalledAt of ["headers", "body"]) {
  test(`actual share-status consumer releases a request stalled at ${stalledAt}`, async () => {
    const never = new Promise(() => {});
    let signal;
    const fetcher = (_url, init) => {
      signal = init.signal;
      return stalledAt === "headers"
        ? never
        : Promise.resolve({ ok: true, status: 200, json: () => never });
    };
    const { workbench, timers } = consumer(fetcher);
    let settled = false;
    const pending = workbench.fetchShareStatus().finally(() => {
      settled = true;
    });
    await flush();
    assert.ok(
      timers.size > 0,
      "a stalled response must have a bounded deadline"
    );
    for (const callback of [...timers.values()]) callback();
    await flush();
    assert.equal(settled, true, "the share lane must become refreshable again");
    await pending;
    assert.equal(workbench.shareStatus.state, "error");
    assert.match(workbench.shareStatus.reason, /超时/);
    assert.equal(
      signal.aborted,
      true,
      "the underlying browser request must be cancelled"
    );
    assert.equal(timers.size, 0);
  });
}

test("actual share-status consumer coalesces overlapping refreshes", async () => {
  let requests = 0,
    complete;
  const fetcher = () => {
    requests++;
    return new Promise((resolve) => {
      complete = resolve;
    });
  };
  const { workbench, timers } = consumer(fetcher);
  const first = workbench.fetchShareStatus();
  const second = workbench.fetchShareStatus();
  await flush();
  assert.equal(
    requests,
    1,
    "polling and lane refresh must share one pending read"
  );
  complete({
    ok: true,
    status: 200,
    json: async () => ({ gateway_running: true }),
  });
  await Promise.all([first, second]);
  assert.equal(workbench.shareStatus.state, "live");
  assert.equal(timers.size, 0);
});

test("a stalled share-lane refresh releases its in-flight gate", async () => {
  const { workbench, timers } = consumer(
    (_url, init) =>
      new Promise((_resolve, reject) => {
        // A real fetch rejects when its AbortController fires; the fake must too,
        // otherwise a timed-out public-supervision read would never settle.
        init?.signal?.addEventListener(
          "abort",
          () => reject(new Error("aborted")),
          { once: true }
        );
      })
  );
  workbench.renderPanel = () => {};
  workbench.refreshShareData();
  await flush();
  assert.equal(workbench.shareFetchInFlight, true);
  assert.equal(
    timers.size,
    4,
    "status, keys, usage and public supervision each need a bounded read"
  );
  for (const callback of [...timers.values()]) callback();
  await flush();
  assert.equal(workbench.shareFetchInFlight, false);
  assert.equal(workbench.shareStatus.state, "error");
  assert.equal(workbench.shareKeys.state, "error");
  assert.equal(workbench.shareUsage.state, "error");
  assert.equal(workbench.publicShare.state, "error");
  assert.equal(timers.size, 0);
});
