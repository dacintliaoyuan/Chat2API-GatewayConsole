// Console gate - single entry point. Run after every console UI change (see README.md).
//
//   node scripts/console-gate/run.mjs [--role local|cloud] [--quick] [--only gate,honesty,logs,states,hover,um]
//        [--url <origin>] [--build <dir>] [--fixtures <file>] [--out <dir>]
//        [--settle <ms>] [--lane-wait <ms>] [--binding candidate|skip]
//
// --role picks that role's candidate (release/candidate/<role>) and synthetic
// fixtures, and the candidate binding also checks the manifest's role. The
// local candidate opens the UM console by default: the workbench groups go
// through `?console=workbench`, and `--role local` adds the `um` group (UM
// console views on the offline preview, F1–F7 of the r3 handoff). Without
// it: starts the offline fixture server on release/candidate with the
// sanitized fixtures, runs every check, writes <out>/result.json and screenshots,
// stops the server, exits 1 if anything failed. --url points the browser checks at
// an already-running origin instead (a live 8001 needs --settle 16000 --lane-wait
// 6000; its reads are real but every non-GET is still answered 403 locally). The
// fixture-state check always runs offline on --build.
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { createContext, paths, ROLE_BUILDS, ROLE_FIXTURES } from "./core.mjs";
import { prepareFixtures } from "./fixtures/shift.mjs";
import { startFixtureServer } from "./fixture-server.mjs";
import { runGate } from "./gate.mjs";
import { runStates } from "./states.mjs";
import { runHoverScan } from "./hoverscan.mjs";

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
  opt(
    "--only",
    role === "cloud"
      ? "cloud"
      : `gate,honesty,logs,states,hover${role === "local" ? ",um" : ""}`
  ).split(",")
);
mkdirSync(out, { recursive: true });

const fixtures = prepareFixtures(
  JSON.parse(readFileSync(fixturesFile, "utf8"))
);
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
  laneWait: Number(opt("--lane-wait", 2200)),
  entry: role === "cloud" ? "" : "?console=workbench",
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
  if (only.has("gate"))
    await guarded("gate", async () => {
      result.gate = await runGate(c, { fixtures });
    });
  if (only.has("logs")) {
    const mod = resolve(paths.here, "logs.mjs");
    if (existsSync(mod))
      await guarded("logs", async () => {
        result.logs = await (
          await import(new URL("./logs.mjs", import.meta.url))
        ).runLogs(c, { fixtures });
      });
    else c.fail("logs", "logs.mjs 不存在——日志页判据没有运行，不按通过计");
  }
  if (only.has("honesty")) {
    const mod = resolve(paths.here, "honesty.mjs");
    if (existsSync(mod))
      await guarded("honesty", async () => {
        result.honesty = await (
          await import(new URL("./honesty.mjs", import.meta.url))
        ).runHonesty(c, { fixtures, binding: opt("--binding", "candidate") });
      });
    else
      c.fail("honesty", "honesty.mjs 不存在——诚实性判据没有运行，不按通过计");
  }
  if (only.has("hover"))
    await guarded("hover", async () => {
      result.hover = await runHoverScan(c);
    });
  if (only.has("um"))
    await guarded("um", async () => {
      result.um = await (
        await import(new URL("./um.mjs", import.meta.url))
      ).runUm(c);
    });
  if (only.has("states"))
    await guarded("states", async () => {
      const states = await runStates({ out, build, fixtures });
      result.states = {
        ok: states.ok,
        unanswered: states.unanswered,
        cases: states.report.length,
      };
      if (!states.ok)
        c.fail(
          "states",
          `夹具状态未全部通过：${JSON.stringify(states.report.filter((r) => r.errors.length || r.overflow.doc || r.overflow.main || r.unstyled.length).map((r) => `${r.scenario}/${r.theme}`))}${states.unanswered.length ? ` 漏答 ${states.unanswered.length}` : ""}`
        );
      c.rows.push({ label: "states", report: states.report });
    });
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
