// Console gate - single entry point. Run after every console UI change (see README.md).
//
//   node scripts/console-gate/run.mjs [--role local|cloud] [--quick] [--only um|cloud]
//        [--url <origin>] [--build <dir>] [--fixtures <file>] [--out <dir>]
//        [--settle <ms>]
//
// --role picks that role's candidate (release/candidate/<role>) and synthetic
// fixtures, and the candidate binding also checks the manifest's role. The local
// candidate opens the UM console and `--role local` runs the `um` group (UM console
// views on the offline preview, F1–F7 of the r3 handoff). Without it: starts the
// offline fixture server on release/candidate with the sanitized fixtures, runs the
// selected checks, writes <out>/result.json and screenshots, stops the server, exits
// 1 if anything failed. --url points the browser checks at an already-running
// origin instead (a live 8001 needs --settle 16000; its reads are real but every
// non-GET is still answered 403 locally).
import { writeFileSync, mkdirSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { createContext, paths, ROLE_BUILDS, ROLE_FIXTURES } from "./core.mjs";
import { startFixtureServer } from "./fixture-server.mjs";

const argv = process.argv.slice(2);
const flag = (name) => argv.includes(name);
const opt = (name, dflt) => {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : dflt;
};
const stamp = new Date()
  .toISOString()
  .replace(/[-:]/g, "")
  .replace(/\..*$/, "")
  .replace("T", "-");
const out = resolve(
  opt("--out", resolve(paths.repoRoot, "output/console-gate", stamp))
);
const role = opt("--role", null);
if (role !== null && !Object.hasOwn(ROLE_BUILDS, role)) {
  console.error(`--role 只接受 ${Object.keys(ROLE_BUILDS).join(" / ")}`);
  process.exit(2);
}
const build = resolve(
  opt("--build", role ? ROLE_BUILDS[role] : paths.candidateDir)
);
const fixturesFile = resolve(
  opt("--fixtures", role ? ROLE_FIXTURES[role] : paths.fixtures)
);
const only = new Set(
  opt("--only", role === "cloud" ? "cloud" : "um").split(",")
);
mkdirSync(out, { recursive: true });

let server = null;
let base = opt("--url", null);
if (!base) {
  server = await startFixtureServer({ build, fixtures: fixturesFile });
  base = server.url;
}
const c = await createContext({
  base,
  out,
  quick: flag("--quick"),
  settle: Number(opt("--settle", 5500)),
});
c.build = build;
c.role = role;
const result = {
  role,
  base,
  build,
  fixtures: fixturesFile,
  out,
  started: new Date().toISOString(),
};
// A check that throws is recorded as a failure of that check (never a pass) and
// the remaining checks still run, so one broken selector cannot hide the rest.
const guarded = async (name, task) => {
  try {
    await task();
  } catch (e) {
    c.fail(
      name,
      `门禁自身异常，该组判据不成立：${String(e?.message ?? e)
        .split("\n")[0]
        .slice(0, 200)}`
    );
  }
};
try {
  if (only.has("cloud")) {
    const mod = resolve(paths.here, "cloud.mjs");
    if (existsSync(mod))
      await guarded("cloud", async () => {
        result.cloud = await (
          await import(new URL("./cloud.mjs", import.meta.url))
        ).runCloud(c);
      });
    else c.fail("cloud", "cloud.mjs 不存在——云端判据没有运行，不按通过计");
  }
  if (only.has("um")) {
    const mod = resolve(paths.here, "um.mjs");
    if (existsSync(mod))
      await guarded("um", async () => {
        result.um = await (
          await import(new URL("./um.mjs", import.meta.url))
        ).runUm(c);
      });
    else c.fail("um", "um.mjs 不存在——UM 判据没有运行，不按通过计");
  }
} finally {
  await c.close();
  if (server) {
    result.fixtureServer = server.log;
    await server.close();
  }
}
c.rows.push({
  label: "requests",
  nonGetAnsweredLocally: c.nonGet.length,
  nonGetSample: [...new Set(c.nonGet.map((s) => s.replace(/\?.*$/, "")))].slice(
    0,
    6
  ),
  foreignAborted: c.foreign.length,
});
result.ok = c.failures.length === 0;
result.failureCount = c.failures.length;
result.failures = c.failures;
result.rows = c.rows;
result.finished = new Date().toISOString();
writeFileSync(resolve(out, "result.json"), JSON.stringify(result, null, 1));
console.log(
  JSON.stringify(
    {
      ok: result.ok,
      out,
      base,
      failureCount: result.failureCount,
      failures: c.failures.slice(0, 40),
      nonGetAnsweredLocally: c.nonGet.length,
      foreignAborted: c.foreign.length,
    },
    null,
    1
  )
);
process.exit(result.ok ? 0 : 1);
