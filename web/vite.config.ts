import { defineConfig, type Plugin, type UserConfig } from "vite";
import { readFileSync, statSync } from "node:fs";
import { mkdir, readdir, writeFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import type { ServerResponse } from "node:http";

/** Gateway probe table. The dev server and the candidate preview read the SAME
 *  real ports, so the rows are shared here instead of duplicated. */
const GW_TARGETS: readonly { prefix: string; target: string }[] = [
  { prefix: "/gw/usagesrv", target: "http://127.0.0.1:8001" },
  { prefix: "/gw/chat2api", target: "http://127.0.0.1:8000" },
  { prefix: "/gw/qoder-cn", target: "http://127.0.0.1:8095" },
  { prefix: "/gw/qoder-global", target: "http://127.0.0.1:8097" },
  { prefix: "/gw/cliproxy", target: "http://127.0.0.1:8320" },
];

/** The candidate line's build output. Pinned here instead of left to a
 *  command-line flag: on 2026-09-29 a candidate rebuild run without
 *  `--outDir` overwrote `dist` — the directory 8001 serves as the production
 *  front-end. `vite build --mode candidate` always writes here, and
 *  `--mode candidate` refuses any other outDir. */
const CANDIDATE_OUT_DIR = "release/candidate";

/** One code base, two consoles (ADR 0025): the role is fixed at build time and
 *  checked against the backend's own report once the page loads. */
type ConsoleRole = "local" | "cloud";
const CONSOLE_ROLES: readonly ConsoleRole[] = ["local", "cloud"];
/** Names the role of a non-candidate build; required for a production build. */
const ROLE_ENV = "CONSOLE_ROLE";
/** The local console no longer carries API Key / API service management:
 *  both its candidate and production entry mount the UM console (B6), while the
 *  cloud console owns that management. A local production build still needs
 *  this explicit declaration so the only management entry is never dropped by
 *  mistake; `?console=workbench` remains an escape hatch, not the default. */
const LOCAL_SHARE_RETIRED_ENV = "CONSOLE_LOCAL_SHARE_RETIRED";

/** Candidate lines, each pinned to one directory and one role. `candidate` keeps
 *  the legacy default directory (the 8099 preview and the console-log gate read
 *  it) as the local role; the role lines live in their own subdirectories so a
 *  role build never overwrites the default candidate. The legacy default line
 *  writes the shared root without emptying it, so its rebuild keeps the `local/`
 *  and `cloud/` role candidates; each role line empties only its own
 *  subdirectory. */
const CANDIDATE_LINES: Readonly<Record<string, { outDir: string; role: ConsoleRole }>> = {
  candidate: { outDir: CANDIDATE_OUT_DIR, role: "local" },
  "candidate-local": { outDir: `${CANDIDATE_OUT_DIR}/local`, role: "local" },
  "candidate-cloud": { outDir: `${CANDIDATE_OUT_DIR}/cloud`, role: "cloud" },
};

/** The candidate line a mode pins, or null (own keys only). */
function candidateLineOf(mode: string) {
  return Object.hasOwn(CANDIDATE_LINES, mode) ? CANDIDATE_LINES[mode] : null;
}

function productionBuildDeclared(): boolean {
  return process.env[PRODUCTION_BUILD_ENV] === "1";
}

/** The build's console role. A candidate mode pins it; a production build must
 *  name it in CONSOLE_ROLE, and the local role additionally needs the share
 *  takeover declaration. Other builds and the dev server default to local.
 *  Unknown or conflicting values fail before anything is emitted. */
function resolveConsoleRole(mode: string, command: "build" | "serve"): ConsoleRole {
  const raw = process.env[ROLE_ENV];
  const declared = raw === undefined ? null : raw.toLowerCase();
  if (declared !== null && !CONSOLE_ROLES.includes(declared as ConsoleRole)) {
    throw new Error(`${ROLE_ENV} 只接受 local 或 cloud（实际=${JSON.stringify(raw)}）。`);
  }
  const line = candidateLineOf(mode);
  if (line) {
    if (declared !== null && declared !== line.role) {
      throw new Error(`--mode ${mode} 固定为 ${line.role} 角色，与 ${ROLE_ENV}=${declared} 冲突；`
        + `换用对应角色的候选模式（${Object.keys(CANDIDATE_LINES).join(" / ")}）。`);
    }
    return line.role;
  }
  if (command === "build" && productionBuildDeclared()) {
    if (declared === null) {
      throw new Error(`生产构建必须显式声明 ${ROLE_ENV}=local 或 ${ROLE_ENV}=cloud：`
        + `本地与云端控制台是两个不同的页面，不按默认值猜。`);
    }
    if (declared === "local" && process.env[LOCAL_SHARE_RETIRED_ENV] !== "1") {
      throw new Error(`本地角色的生产页面将改为不含 API Key 与 API 服务管理的 UM 控制台；云端控制台接管前发布它可能失去唯一的管理入口。`
        + `确认云端已接管后显式声明 ${LOCAL_SHARE_RETIRED_ENV}=1（分享已由云端接管）；`
        + `接管前需要紧急重建本地生产页时，用引入此守卫之前的提交构建。`);
    }
  }
  return (declared as ConsoleRole | null) ?? "local";
}

/** The candidate line whose directory `outDir` is, if any. */
function candidateLineFor(root: string, outDir: string) {
  return Object.entries(CANDIDATE_LINES)
    .find(([, line]) => resolve(root, line.outDir) === outDir) ?? null;
}

/** Production output, one directory per console role (ADR 0025). The local
 *  console is hosted by 8001 from `dist`; the cloud console is built to
 *  `dist-cloud` and carried into the release package as `web/dist`. Neither
 *  directory may receive the other role's tree. Writing either one is the
 *  production switch, so it must be asked for explicitly: a bare
 *  `npm run build` (the 2026-09-29 accident) now fails before vite emits
 *  anything. Its npm `prebuild` step still runs first and refreshes the
 *  rolling-number compatibility patch, never either output. */
const PRODUCTION_OUT_DIRS: Readonly<Record<ConsoleRole, string>> = {
  local: "dist",
  cloud: "dist-cloud",
};
/** Set by the production build itself: `CONSOLE_PRODUCTION_BUILD=1`. */
const PRODUCTION_BUILD_ENV = "CONSOLE_PRODUCTION_BUILD";

/** Build inputs whose bytes define the build identity. The identity file lists
 *  each with its sha256 plus one aggregate `sourceHash`, so re-hashing this
 *  source and comparing tells whether a carried artifact was built from it. */
const CANDIDATE_SOURCE_FILES = [
  "index.html", "package.json", "tsconfig.json", "vite.config.ts",
];
const CANDIDATE_SOURCE_DIRS = ["src", "public"];
/** The historical file name; `um-cloud-deploy` reads it as the build identity. */
const CANDIDATE_MANIFEST = "candidate-build.json";

/** Refuse a build whose output would land on a directory this line does not own.
 *
 *  - `dist` and `dist-cloud` are the production hosting directories of the
 *    local and cloud consoles: writing either one is the production switch, and
 *    only the matching role may do it after declaring the production build.
 *  - a production build is pinned to its role's production directory; any other
 *    resolved outDir is refused.
 *  - a candidate mode is pinned to its own directory (CANDIDATE_LINES): any
 *    other resolved outDir is refused.
 *  - a candidate directory only takes its own role, whatever the mode, and
 *    nothing else is written under `release/candidate/`. */
function buildOutputGuard(mode: string, role: ConsoleRole): Plugin {
  return {
    name: "console-build-output-guard",
    apply: "build",
    configResolved(config) {
      const outDir = resolve(config.root, config.build.outDir);
      const productionDir = resolve(config.root, PRODUCTION_OUT_DIRS[role]);
      const productionDirs = CONSOLE_ROLES.map(candidate => ({
        role: candidate,
        dir: resolve(config.root, PRODUCTION_OUT_DIRS[candidate]),
      }));
      const candidateRoot = resolve(config.root, CANDIDATE_OUT_DIR);
      const modes = Object.keys(CANDIDATE_LINES).join(" / ");
      const owner = productionDirs.find(entry => entry.dir === outDir);
      if (owner && owner.role !== role) {
        throw new Error(
          `拒绝把 ${role} 角色写进 ${PRODUCTION_OUT_DIRS[owner.role]}/：`
          + `该目录是 ${owner.role} 角色的生产输出；本机角色写 ${PRODUCTION_OUT_DIRS.local}/，`
          + `云端角色写 ${PRODUCTION_OUT_DIRS.cloud}/。`,
        );
      }
      const production = productionBuildDeclared();
      if (owner && !production) {
        throw new Error(
          `拒绝写 ${PRODUCTION_OUT_DIRS[owner.role]}/：该目录是生产页面的托管目录，写入它等于生产切换；`
          + `确需生产构建请显式声明 ${PRODUCTION_BUILD_ENV}=1 与 ${ROLE_ENV}=${owner.role}。`
          + `候选构建请用 --mode ${modes}，各自的输出目录固定。`,
        );
      }
      const pinned = candidateLineOf(mode);
      if (production && !pinned && outDir !== productionDir) {
        throw new Error(
          `拒绝生产构建写 ${config.build.outDir}/：${role} 角色的生产输出固定为 `
          + `${PRODUCTION_OUT_DIRS[role]}/，不接受其它 outDir。`,
        );
      }
      if (pinned && outDir !== resolve(config.root, pinned.outDir)) {
        throw new Error(
          `拒绝候选构建写 ${config.build.outDir}/：--mode ${mode} 的输出固定为 ${pinned.outDir}/，`
          + `不接受其它 outDir。`,
        );
      }
      const line = candidateLineFor(config.root, outDir);
      if (line && line[1].role !== role) {
        throw new Error(
          `拒绝把 ${role} 角色写进 ${line[1].outDir}/：该目录是 ${line[1].role} 角色的候选（--mode ${line[0]}）。`,
        );
      }
      if (!line && outDir.startsWith(candidateRoot + sep)) {
        throw new Error(
          `拒绝写 ${config.build.outDir}/：${CANDIDATE_OUT_DIR}/ 下只有固定的候选目录（--mode ${modes}）。`,
        );
      }
    },
  };
}

function sha256(bytes: Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function gitHead(root: string): string | null {
  try {
    return execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim() || null;
  } catch {
    return null;  // 没有 git 或不是仓库：如实写 null，不猜一个值
  }
}

/** Every regular file under `dir`, as paths relative to the project root.
 *  `readdir(..., {recursive: true})` also lists directories, so each entry is
 *  checked before it is taken as a build input. */
async function listBuildInputs(root: string, dir: string): Promise<string[]> {
  const found = await readdir(resolve(root, dir), { recursive: true });
  return found
    .map((name: string) => `${dir}/${name.replaceAll("\\", "/")}`)
    .filter((path: string) => statSync(resolve(root, path)).isFile());
}

/** Bind a built console tree to the source it was built from, inside the
 *  artifact: `candidate-build.json` carries every build input's sha256, one
 *  aggregate `sourceHash` and the console role it was built as.
 *
 *  `um-cloud-deploy` reads this file to refuse a tree built for another role,
 *  so every tree that can be carried writes it: a candidate line names itself,
 *  a production build names "production" and its role. The local production
 *  `dist` (8001) writes it too, so `usagesrv.py` can refuse to host a tree
 *  built for the other role. */
function consoleBuildIdentity(mode: string, role: ConsoleRole): Plugin {
  let root = "";
  let outDir = "";
  return {
    name: "console-build-identity",
    apply: "build",
    configResolved(config) {
      root = config.root;
      outDir = resolve(config.root, config.build.outDir);
    },
    async writeBundle(_options, bundle) {
      const line = candidateLineFor(root, outDir);
      const production = outDir === resolve(root, PRODUCTION_OUT_DIRS[role]);
      const paths = [...CANDIDATE_SOURCE_FILES];
      for (const dir of CANDIDATE_SOURCE_DIRS) paths.push(...await listBuildInputs(root, dir));
      const sources = paths.sort().map(path => {
        const bytes = readFileSync(resolve(root, path));
        return { path, sha256: sha256(bytes), bytes: bytes.length };
      });
      const sourceHash = sha256(Buffer.from(
        sources.map(file => `${file.path}\0${file.sha256}\n`).join(""), "utf8"));
      await mkdir(outDir, { recursive: true });
      await writeFile(resolve(outDir, CANDIDATE_MANIFEST), `${JSON.stringify({
        line: line ? line[0] : production ? "production" : "build",
        ...(line ? { outDir: line[1].outDir }
          : production ? { outDir: PRODUCTION_OUT_DIRS[role] } : {}),
        mode,
        role,
        builtAt: new Date().toISOString(),
        gitHead: gitHead(root),
        sourceHash,
        entryAssets: Object.keys(bundle).filter(name => /\.(js|css)$/.test(name)).sort(),
        sources,
      }, null, 2)}\n`, "utf8");
    },
  };
}

/** Answer a refused request here — never forwarded to the gateway. */
function refuseCandidateRequest(res: ServerResponse, method: string, path: string,
                               code: string, detail: string) {
  res.statusCode = 403;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify({ error: code, detail, method, path }));
}

/** Read-only proxy table for `vite preview` (the 8099 candidate).
 *
 *  GET/HEAD are the only methods that may reach a gateway port: the candidate
 *  reads real data through these rows. `vite preview` inherits `server.proxy`
 *  when `preview.proxy` is absent, which is why the rows are spelled out here
 *  rather than left to inheritance. The dev server keeps its own disclosed write
 *  capability (ADR 0019) — this table is preview-only, and the guard below
 *  refuses every non-GET/HEAD before it. */
function readOnlyProxy() {
  return Object.fromEntries(GW_TARGETS.map(({ prefix, target }) => [prefix, {
    target,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(new RegExp(`^${prefix}`), ""),
  }]));
}

/** First guard, ahead of the proxy: every non-GET/HEAD request is refused
 *  here, locally, with a readable reason. Nothing is forwarded to a gateway
 *  port, and no request body is read.
 *
 *  Until 2026-09-29 this let a POST through when its body carried one of the
 *  read-only actions `console_adapters.build_registry()` declares, rewriting
 *  its Origin so usagesrv's route gate would accept it. That exception is
 *  revoked: those POSTs now get the same local 403, because the backend's
 *  read-only declaration is not something this page can verify per request, and
 *  each forwarded one landed in the production operation ledger. GET/HEAD still
 *  reach 8001 for real data; the 8001 production page is the place for every
 *  operation. Only `configurePreviewServer` is implemented, so the dev server
 *  keeps its existing disclosed capability. */
function candidateReadOnlyGuard(): Plugin {
  return {
    name: "candidate-read-only-preview",
    configurePreviewServer(server) {
      server.middlewares.use((req, res, next) => {
        const method = String(req.method ?? "GET").toUpperCase();
        if (method === "GET" || method === "HEAD") { next(); return; }
        refuseCandidateRequest(res, method, String(req.url ?? ""),
          "candidate_preview_read_only",
          "候选预览是只读的：一切非 GET/HEAD 都在本机 403，一个字节都不会转发给 8001 生产网关"
          + "（包括后端声明为只读的动作 POST）。要执行任何操作请用 8001 生产页面。");
      });
    },
  };
}
const consoleConfig = (mode: string, role: ConsoleRole): UserConfig => ({
  base: "/",
  // Build-time constants: the entry assembles only this role's console, and
  // candidate-only demo code drops out of every other build.
  define: {
    __CONSOLE_ROLE__: JSON.stringify(role),
    __CONSOLE_CANDIDATE__: JSON.stringify(candidateLineOf(mode) !== null),
  },
  build: {
    // A candidate mode pins its output; a production build pins its role's
    // output. A bare build keeps vite's default (`dist`) and the output guard
    // refuses it before anything is emitted. The legacy default candidate writes
    // the shared root as the local console: emptying that root would delete the
    // cloud role subdirectory, so it leaves the root contents in place. The role
    // lines own their subdirectories and keep vite's default emptying there.
    ...(candidateLineOf(mode)
      ? {
          outDir: candidateLineOf(mode)!.outDir,
          ...(mode === "candidate" ? { emptyOutDir: false } : {}),
        }
      : productionBuildDeclared() ? { outDir: PRODUCTION_OUT_DIRS[role] } : {}),
    rollupOptions: {
      input: {
        index: fileURLToPath(new URL("./index.html", import.meta.url)),
      },
    },
  },
  // Read-only liveness/summary access to the production gateway ports through
  // the dev server (same origin). GET probes only; the prototype never talks
  // to these ports from a static build (no server proxy in production output).
  // B-A (2026-09-20): /gw/qoder-cn joins the proxy table (8095 CN gateway,
  // ADR 0019 upline); /gw/m365 is gone with the offline chain (no consumer).
  server: {
    // A36-2: dev server binds loopback only; the Origin rewrite below
    // verifies the ORIGINAL Host/Origin before trusting it.
    host: "127.0.0.1",
    proxy: {
      "/gw/usagesrv": {
        target: "http://127.0.0.1:8001",
        changeOrigin: true,
        rewrite: p => p.replace(/^\/gw\/usagesrv/, ""),
        // Dev-only, disclosed (ADR 0019): the usagesrv route layer gates
        // POSTs on Origin == its own origin (http://127.0.0.1:8001), while
        // the dev page origin is 5173. This prefix rewrites the Origin
        // header so bootstrap-code login and the service start/stop POSTs
        // reach the route layer during development. Static builds contain
        // no proxy and keep the honest origin gate.
        configure: proxy => {
          // A36-2: the rewrite rides loopback trust. The ORIGINAL Host and
          // Origin are verified BEFORE the rewrite — a foreign Host never
          // gets its Origin rewritten into this service's trusted origin.
          proxy.on("proxyReq", (proxyReq, req) => {
            const host = String(req.headers.host ?? "").replace(/:\d+$/, "");
            if (!["127.0.0.1", "localhost", "[::1]", "::1"].includes(host)) {
              proxyReq.destroy();
              return;
            }
            const origin = req.headers.origin;
            if (origin !== undefined
              && !/^https?:\/\/(127\.0\.0\.1|localhost|\[::1\]|::1)(:\d+)?$/.test(origin)) {
              proxyReq.destroy();
              return;
            }
            proxyReq.setHeader("Origin", "http://127.0.0.1:8001");
          });
        },
      },
      "/gw/chat2api": { target: "http://127.0.0.1:8000", changeOrigin: true, rewrite: p => p.replace(/^\/gw\/chat2api/, "") },
      "/gw/qoder-cn": { target: "http://127.0.0.1:8095", changeOrigin: true, rewrite: p => p.replace(/^\/gw\/qoder-cn/, "") },
      "/gw/qoder-global": { target: "http://127.0.0.1:8097", changeOrigin: true, rewrite: p => p.replace(/^\/gw\/qoder-global/, "") },
      "/gw/cliproxy": { target: "http://127.0.0.1:8320", changeOrigin: true, rewrite: p => p.replace(/^\/gw\/cliproxy/, "") },
    },
  },
  // The 8099 candidate preview: real data through GET/HEAD, and nothing else —
  // candidateReadOnlyGuard refuses every non-GET/HEAD locally, so the page the
  // maintainer clicks around in cannot reach a production gateway port.
  // `host` mirrors the dev server's loopback bind. `cors: false` because vite's
  // CORS middleware runs ahead of plugin middlewares and would answer an OPTIONS
  // preflight itself (204, measured 2026-09-30); the page is same-origin, so with
  // it off every OPTIONS reaches the guard and gets the same local 403.
  preview: {
    host: "127.0.0.1",
    cors: false,
    proxy: readOnlyProxy(),
  },
  plugins: [candidateReadOnlyGuard(), buildOutputGuard(mode, role), consoleBuildIdentity(mode, role)],
});

export default defineConfig(({ mode, command }) => consoleConfig(mode, resolveConsoleRole(mode, command)));
