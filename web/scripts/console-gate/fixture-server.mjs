// Offline fixture server for the console gate. Serves a frozen build directory
// byte for byte and answers /gw/usagesrv/* reads from a fixtures file
// ({ "<path>?<query>": { status, body } }). It talks to no other process: nothing
// is proxied. Unlisted writes are refused. Unknown paths are 404.
// Fixtures carrying an "_anchor" day are moved to today on load (fixtures/shift.mjs),
// and health / runtime timestamps are freshened per request so the console
// shows "fresh evidence" rather than the fixture's own age.
//
// CLI: node fixture-server.mjs [--port 18199] [--build <dir>] [--fixtures <file>]
import { createServer } from "node:http";
import { readFileSync, existsSync, statSync } from "node:fs";
import { extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { ROLE_BUILDS, paths } from "./core.mjs";
import { prepareFixtures } from "./fixtures/shift.mjs";

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
      fixtures[Object.keys(fixtures).find((k) => k.split("?")[0] === path)];
    if (!hit) return null;
    const body =
      hit.body && typeof hit.body === "object"
        ? structuredClone(hit.body)
        : hit.body;
    const now = Math.floor(Date.now() / 1000);
    if (path === "/api/console/health" && body) body.checked_at = now;
    if (path === "/api/console/services" && body?.meta)
      body.meta.generated_epoch = now;
    return { status: hit.status, body };
  };
}

export function startFixtureServer({
  port = 0,
  build = ROLE_BUILDS.local,
  fixtures = paths.fixtures,
} = {}) {
  const BUILD = resolve(build);
  const read = makeReader(
    prepareFixtures(JSON.parse(readFileSync(fixtures, "utf8")))
  );
  const log = { nonGet: [], unanswered: [], served: 0 };
  const server = createServer((req, res) => {
    const u = new URL(req.url, "http://127.0.0.1");
    const send = (status, type, body) => {
      res.writeHead(status, {
        "content-type": type,
        "cache-control": "no-store",
      });
      res.end(req.method === "HEAD" ? undefined : body);
    };
    if (req.method !== "GET" && req.method !== "HEAD") {
      log.nonGet.push(`${req.method} ${u.pathname}`);
      req.resume();
      return send(
        403,
        "application/json",
        JSON.stringify({ error: "fixture_server_read_only" })
      );
    }
    if (u.pathname.startsWith("/gw/")) {
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
    return send(
      200,
      TYPES[extname(abs).toLowerCase()] || "application/octet-stream",
      readFileSync(abs)
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
    build: arg("--build", ROLE_BUILDS.local),
    fixtures: arg("--fixtures", paths.fixtures),
  });
  console.log(`fixture server on ${s.url}`);
}
