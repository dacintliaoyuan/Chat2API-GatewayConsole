// Read-only shape capture for regenerating the console-gate fixtures.
//
//   node scripts/console-gate/fixtures/capture.mjs --out <dir under SelfUseAI/output/> [--origin http://127.0.0.1:8001] [--build <dir>]
//
// Opens the candidate build on the offline fixture server and lets the page issue
// its own reads; every GET /gw/* the page makes is fetched from --origin by Node
// and recorded as { "<path>?<query>": { status, body } } (paths relative to
// /gw/usagesrv). Nothing else is forwarded: non-GET is answered 403 locally by
// core.mjs before this hook runs, foreign hosts are aborted, and per-key share
// endpoints (single key, secret, plaintext) are refused here. The output holds
// REAL data: keep it under the gitignored output/ tree and feed it to
// sanitize.mjs; never commit it.
import { writeFileSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { createContext, LANES, paths } from "../core.mjs";
import { startFixtureServer } from "../fixture-server.mjs";

const argv = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : dflt;
};
const out = opt("--out", null);
if (!out) {
  console.error(
    "usage: node capture.mjs --out <dir under SelfUseAI/output/> [--origin http://127.0.0.1:8001]"
  );
  process.exit(2);
}
const origin = opt("--origin", "http://127.0.0.1:8001").replace(/\/$/, "");
const OUT = resolve(out);
if (!OUT.startsWith(resolve(paths.repoRoot, "output"))) {
  console.error(
    "refusing: the capture holds real data and must stay under SelfUseAI/output/"
  );
  process.exit(2);
}
mkdirSync(OUT, { recursive: true });

const empty = resolve(OUT, "empty-fixtures.json");
writeFileSync(empty, "{}");
const server = await startFixtureServer({
  build: opt("--build", paths.candidateDir),
  fixtures: empty,
});
const DENY = /\/share-management\/keys\/|secret|plaintext|reveal/i;
const capture = {},
  denied = [],
  failed = [];
const override = async (route, req) => {
  const u = new URL(req.url());
  if (!u.pathname.startsWith("/gw/")) return false;
  if (DENY.test(u.pathname)) {
    denied.push(u.pathname);
    await route.fulfill({
      status: 403,
      contentType: "application/json",
      body: '{"error":"capture_denied"}',
    });
    return true;
  }
  try {
    const r = await fetch(origin + u.pathname + u.search, {
      method: "GET",
      signal: AbortSignal.timeout(30000),
    });
    const text = await r.text();
    let body;
    try {
      body = JSON.parse(text);
    } catch {
      body = { __text: text.slice(0, 2000) };
    }
    if (u.pathname.startsWith("/gw/usagesrv/"))
      capture[u.pathname.replace(/^\/gw\/usagesrv/, "") + u.search] = {
        status: r.status,
        body,
      };
    await route.fulfill({
      status: r.status,
      contentType: r.headers.get("content-type") || "application/json",
      body: text,
    });
  } catch (e) {
    failed.push(`${u.pathname}: ${String(e.message).slice(0, 80)}`);
    await route.fulfill({
      status: 504,
      contentType: "application/json",
      body: '{"error":"capture_upstream_failed"}',
    });
  }
  return true;
};
const c = await createContext({
  base: server.url,
  out: OUT,
  settle: 9000,
  laneWait: 4500,
});
try {
  const { ctx, page } = await c.newPage({ override, settle: 9000 });
  for (const L of LANES) {
    await c.gotoLane(page, L.lane);
    if (L.key === "models") {
      for (const s of ["catalog", "sync", "probe"]) {
        const b = await page.$(`[data-wb-model-section="${s}"]`);
        if (b) {
          await b.click();
          await page.waitForTimeout(3500);
        }
      }
    }
    if (L.key === "logs") {
      const open = await page.$("[data-log-open]");
      if (open) {
        await open.click();
        await page.waitForTimeout(4500);
      }
    }
    if (L.key === "accounts") {
      for (const item of await page.$$("[data-wb-md-item]")) {
        await item.click();
        await page.waitForTimeout(1200);
      }
    }
  }
  await ctx.close();
} finally {
  await c.close();
  await server.close();
}
writeFileSync(
  resolve(OUT, "api-capture.json"),
  JSON.stringify(capture, null, 1)
);
console.log(
  JSON.stringify(
    {
      out: resolve(OUT, "api-capture.json"),
      endpoints: Object.keys(capture).length,
      denied,
      failed,
      nonGetAnsweredLocally: c.nonGet.length,
    },
    null,
    1
  )
);
process.exit(failed.length ? 1 : 0);
