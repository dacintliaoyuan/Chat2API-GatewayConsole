import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";

const source = await readFile(
  new URL("../public/sw.js", import.meta.url),
  "utf8"
);

function harness(failDelete = false) {
  const listeners = new Map();
  const calls = [];
  vm.runInNewContext(source, {
    URL,
    self: {
      addEventListener: (type, listener) => listeners.set(type, listener),
      skipWaiting: async () => calls.push("skipWaiting"),
      registration: {
        scope: "http://localhost/console/",
        unregister: async () => calls.push("unregister"),
      },
      clients: { claim: async () => calls.push("claim") },
    },
    caches: {
      keys: async () => [
        "rhine-lab:/console/:old",
        "rhine-lab:/:other",
        "user-data",
      ],
      delete: async (name) => {
        calls.push(name);
        if (failDelete) throw new Error("cache unavailable");
        return true;
      },
    },
  });
  return {
    calls,
    listeners,
    run: (type) =>
      new Promise((resolve, reject) => {
        listeners.get(type)({
          waitUntil: (promise) => promise.then(resolve, reject),
        });
      }),
  };
}

test("retirement removes only this registration cache and unregisters", async () => {
  const worker = harness();
  await worker.run("install");
  await worker.run("activate");
  assert.deepEqual(worker.calls, [
    "skipWaiting",
    "rhine-lab:/console/:old",
    "claim",
    "unregister",
  ]);
  assert.equal(worker.listeners.has("fetch"), false);
});

test("cache cleanup failure is reported and does not unregister prematurely", async () => {
  const worker = harness(true);
  await assert.rejects(worker.run("activate"), /cache unavailable/);
  assert.deepEqual(worker.calls, ["rhine-lab:/console/:old"]);
});
