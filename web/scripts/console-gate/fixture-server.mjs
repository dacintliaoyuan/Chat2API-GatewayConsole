// Offline fixture server for the console gate. Serves a frozen build directory
// byte for byte and answers /gw/usagesrv/* reads from a fixtures file
// ({ "<path>?<query>": { status, body } }). It talks to no other process: nothing
// is proxied. Unlisted writes are refused; optional demo actions only mutate
// isolated process memory. Unknown paths are 404.
// Fixtures carrying an "_anchor" day are moved to today on load (fixtures/shift.mjs),
// and health / runtime / quota timestamps are freshened per request so the console
// shows "fresh evidence" rather than the fixture's own age.
//
// Preview mode (`ops`, CLI `--ops rev25`): POSTs of a few READ actions are answered
// with the backend's real-handler synthetic results from
// ai-gateway/tests/fixtures/frontend_contract_rev25.json (model catalog, context
// readback, probe preview, CLIProxy login read), so a person can look at those
// views offline. The settings chain and the combined sync window's preview/confirm
// pair are included the same way — the answers are still synthetic real-handler
// results and this server still writes nothing anywhere — so the 保存设置 →
// 同步到客户端 → 查看变更 → 确认同步 flow can be looked at offline too. Every
// other non-GET is still answered 403. The gate runs without it.
//
// CLI: node fixture-server.mjs [--port 18199] [--build <dir>] [--fixtures <file>] [--ops rev25]
import { createServer } from "node:http";
import { readFileSync, existsSync, statSync } from "node:fs";
import { extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { paths } from "./core.mjs";
import { prepareFixtures } from "./fixtures/shift.mjs";
import { modelDemoOps } from "./model-demo-ops.mjs";
import { logOverviewFixture } from "./log-overview-fixture.mjs";

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".css": "text/css",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".json": "application/json",
  ".webmanifest": "application/manifest+json",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".txt": "text/plain; charset=utf-8",
  ".mp3": "audio/mpeg",
  ".ogg": "audio/ogg",
  ".md": "text/markdown; charset=utf-8",
};

/** A fixture read: exact key, then the same pathname (dated query strings such as
 *  stats?from=…&to=… or logs?limit=… match on path), with fresh timestamps. */
export function makeReader(fixtures) {
  return (key) => {
    const path = key.split("?")[0];
    const hit =
      fixtures[key] ??
      (path === "/api/console/log-overview"
        ? { status: 200, body: logOverviewFixture(fixtures) }
        : fixtures[
            Object.keys(fixtures).find((k) => k.split("?")[0] === path)
          ]);
    if (!hit) return null;
    const body =
      hit.body && typeof hit.body === "object"
        ? structuredClone(hit.body)
        : hit.body;
    const now = Math.floor(Date.now() / 1000);
    if (path === "/api/console/health" && body) body.checked_at = now;
    if (path === "/api/console/services" && body?.meta)
      body.meta.generated_epoch = now;
    if (path === "/api/console/quota-snapshot" && body) body.checked_at = now;
    return { status: hit.status, body };
  };
}

/** Read-action results for the preview mode, from the backend's generated contract
 *  fixture (synthetic, produced by the real handlers). The settings chain is included
 *  so the 保存草稿 → 预览 → 应用 → 读回 flow can be looked at offline; the
 *  combined sync window's 预览 → 确认 pair is included the same way so the §11.3
 *  编辑 → 保存设置 → 同步到客户端 → 查看变更 → 确认同步 flow can be looked at
 *  offline too. Every other write action is still answered 403. A missing segment
 *  leaves that action undefined, which the caller answers 403 (never a wrong shape). */
export function rev25ReadOps() {
  const rev = JSON.parse(
    readFileSync(
      resolve(
        paths.repoRoot,
        "tests/fixtures/frontend_contract_rev25.json"
      ),
      "utf8"
    )
  );
  const cliproxy =
    rev.cliproxy_status?.scenarios?.find((s) => s.case === "normal")?.operation
      ?.result ?? null;
  return {
    "model.catalog": rev.catalog,
    "model.context_overrides": rev.context_overrides,
    "model.context_probe_preview": rev.context_probe_preview,
    "model.settings_save": rev.settings_save,
    "model.settings_preview": rev.settings_preview,
    "model.settings_apply": rev.settings_apply,
    "model.sync_preview": rev.sync_combined_preview,
    "model.sync_apply": rev.sync_combined_apply,
    ...(cliproxy ? { "cliproxy.status": cliproxy } : {}),
    ...modelDemoOps(rev),
    $capabilities: rev.actions,
    $logSources: rev.log_sources,
    $logOverview: rev.log_overview,
    $logEntries: (params) => {
      const source = params.get("source"),
        category = params.get("category");
      const target = rev.log_overview.targets.find(
        (r) => r.source === source && r.category === category
      );
      const rows = rev.log_overview.rows.filter(
        (r) =>
          r.source === source &&
          r.category === category &&
          (!params.get("q") || r.message.includes(params.get("q")))
      );
      return {
        ...structuredClone(rev.log_entries),
        source,
        category,
        rows,
        status: target?.status ?? "unconfigured",
        next_cursor: null,
        truncated: false,
      };
    },
  };
}

export function startFixtureServer({
  port = 0,
  build = paths.candidateDir,
  fixtures = paths.fixtures,
  ops = null,
} = {}) {
  const BUILD = resolve(build);
  const read = makeReader(
    prepareFixtures(JSON.parse(readFileSync(fixtures, "utf8")))
  );
  const log = { nonGet: [], unanswered: [], served: 0 };
  const opRows = new Map();
  let opSeq = 0;
  const server = createServer((req, res) => {
    const u = new URL(req.url, "http://127.0.0.1");
    const send = (status, type, body) => {
      res.writeHead(status, {
        "content-type": type,
        "cache-control": "no-store",
      });
      res.end(req.method === "HEAD" ? undefined : body);
    };
    if (
      ops &&
      req.method === "POST" &&
      u.pathname === "/gw/usagesrv/api/console/operations"
    ) {
      let raw = "";
      req.on("data", (chunk) => {
        raw += chunk;
        if (raw.length > 65536) req.destroy();
      });
      req.on("end", () => {
        let body = {};
        try {
          body = JSON.parse(raw || "{}");
        } catch {
          /* answered as unknown below */
        }
        log.nonGet.push(`POST ${u.pathname} ${body.action ?? "?"}`);
        let result = ops[body.action];
        if (result === undefined)
          return send(
            403,
            "application/json",
            JSON.stringify({
              error: "fixture_server_read_only",
              reason: `预览夹具未提供 ${body.action ?? "该动作"}（未提供的动作拒绝）`,
            })
          );
        let error = null;
        try {
          if (typeof result === "function") result = result(body.params ?? {});
        } catch (exc) {
          error = { code: "fixture_rejected", message: String(exc.message) };
          result = null;
        }
        const now = Math.floor(Date.now() / 1000);
        const op = {
          id: `fx-op-${++opSeq}`,
          action: body.action,
          target: body.target ?? "",
          state: error ? "failed" : "succeeded",
          created_at: now,
          started_at: now,
          finished_at: now,
          cancel_requested: false,
          result: structuredClone(result),
          error,
        };
        opRows.set(op.id, op);
        send(202, "application/json", JSON.stringify({ operation: op }));
      });
      return;
    }
    if (req.method !== "GET" && req.method !== "HEAD") {
      log.nonGet.push(`${req.method} ${u.pathname}`);
      req.resume();
      return send(
        403,
        "application/json",
        JSON.stringify({ error: "fixture_server_read_only" })
      );
    }
    const opPoll = u.pathname.match(
      /^\/gw\/usagesrv\/api\/console\/operations\/(fx-op-\d+)$/
    );
    if (opPoll && opRows.has(opPoll[1])) {
      log.served++;
      return send(
        200,
        "application/json",
        JSON.stringify({ operation: opRows.get(opPoll[1]) })
      );
    }
    if (u.pathname.startsWith("/gw/")) {
      const demoLogRead = {
        "/gw/usagesrv/api/console/log-sources": ops?.$logSources,
        "/gw/usagesrv/api/console/log-overview": ops?.$logOverview,
        "/gw/usagesrv/api/console/log-entries": ops?.$logEntries
          ? ops.$logEntries(u.searchParams)
          : null,
      }[u.pathname];
      if (demoLogRead)
        return send(200, "application/json", JSON.stringify(demoLogRead));
      if (
        ops?.$modelPending &&
        u.pathname === "/gw/usagesrv/api/console/model-pending"
      ) {
        return send(
          200,
          "application/json",
          JSON.stringify(ops.$modelPending())
        );
      }
      if (
        ops?.$capabilities &&
        u.pathname === "/gw/usagesrv/api/console/actions"
      ) {
        return send(200, "application/json", JSON.stringify(ops.$capabilities));
      }
      const hit = u.pathname.startsWith("/gw/usagesrv/")
        ? read(u.pathname.replace(/^\/gw\/usagesrv/, "") + u.search)
        : null;
      if (!hit) {
        log.unanswered.push(`GET ${u.pathname}`);
        return send(
          404,
          "application/json",
          JSON.stringify({ error: "fixture_missing" })
        );
      }
      log.served++;
      return send(hit.status, "application/json", JSON.stringify(hit.body));
    }
    const abs = resolve(
      join(
        BUILD,
        u.pathname === "/" ? "/index.html" : decodeURIComponent(u.pathname)
      )
    );
    if (!abs.startsWith(BUILD) || !existsSync(abs) || !statSync(abs).isFile()) {
      log.unanswered.push(`GET ${u.pathname}`);
      return send(404, "text/plain", "");
    }
    log.served++;
    let bytes = readFileSync(abs);
    if (ops && extname(abs) === ".html")
      bytes = Buffer.from(
        bytes
          .toString("utf8")
          .replace(
            "<body>",
            '<body><div role="status" style="position:fixed;bottom:12px;right:12px;z-index:10000;background:#20252c;color:white;border:1px solid #cbf33d;border-radius:12px;padding:8px 14px;font:13px system-ui;pointer-events:none">离线评估 · 模拟数据 · 不连接真实网关</div>'
          )
      );
    return send(
      200,
      TYPES[extname(abs).toLowerCase()] || "application/octet-stream",
      bytes
    );
  });
  return new Promise((ok) =>
    server.listen(port, "127.0.0.1", () => {
      const url = `http://127.0.0.1:${server.address().port}/`;
      ok({ url, log, close: () => new Promise((done) => server.close(done)) });
    })
  );
}

if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  const arg = (name, dflt) => {
    const i = process.argv.indexOf(name);
    return i >= 0 ? process.argv[i + 1] : dflt;
  };
  const s = await startFixtureServer({
    port: Number(arg("--port", 18199)),
    build: arg("--build", paths.candidateDir),
    fixtures: arg("--fixtures", paths.fixtures),
    ops: arg("--ops", "") === "rev25" ? rev25ReadOps() : null,
  });
  console.log(`fixture server on ${s.url}`);
}
