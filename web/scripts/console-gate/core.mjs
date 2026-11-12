// Shared harness for the console gate: Playwright loading, the read-only request
// guard, the key mask, page opening and lane navigation. gate.mjs, honesty.mjs,
// states.mjs and hoverscan.mjs all build on createContext(), so every browser
// context of every check carries the same guard.
//
// Guard (registered on the context BEFORE the first navigation): every non-GET/HEAD
// is answered locally with 403 and never forwarded; every request to a host other
// than the target origin is aborted; service workers are blocked; 24+ character
// key-like strings are masked in the page before anything can be screenshotted.
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
export const paths = {
  here,
  console: resolve(here, "../.."),
  repoRoot: resolve(here, "../../.."),
  candidateDir: resolve(here, "../../release/candidate"),
  fixtures: resolve(here, "fixtures/api.json"),
};
/** Role candidates (ADR 0025, vite --mode candidate-local / candidate-cloud) and
 *  the synthetic fixtures each role is checked against. */
export const ROLE_BUILDS = {
  local: resolve(here, "../../release/candidate/local"),
  cloud: resolve(here, "../../release/candidate/cloud"),
};
export const ROLE_FIXTURES = {
  local: resolve(here, "fixtures/api.json"),
  cloud: resolve(here, "fixtures/cloud-api.json"),
};

/** Playwright is a console dev dependency: PLAYWRIGHT_MODULE wins, then the
 *  repo's own install under web/node_modules. */
export async function loadPlaywright() {
  const mod =
    process.env.PLAYWRIGHT_MODULE ||
    resolve(paths.console, "node_modules/playwright/index.mjs");
  return import(pathToFileURL(mod).href);
}

export const VIEWPORTS_FULL = [
  { tag: "1600x900", width: 1600, height: 900, dsf: 1 },
  { tag: "1920x1080", width: 1920, height: 1080, dsf: 1 },
  { tag: "2560x1440", width: 2560, height: 1440, dsf: 1 },
  { tag: "1536x864-dsf125", width: 1536, height: 864, dsf: 1.25 },
];
export const THEMES = ["light", "dark"];
/** Every surface. Since Rev17 the 网关 workspace (nav lane 1) has two: lane 1 here
 *  means a gateway's detail (chat2api, 账号 tab) and lane 5 the pinned 全部模型
 *  view (the former model lane's alias). 操作记录 (lane 2) sits behind the log
 *  lane's section tabs. */
export const LANES = [
  { lane: 0, key: "overview" },
  { lane: 1, key: "gateway" },
  { lane: 5, key: "models" },
  { lane: 3, key: "cost" },
  { lane: 4, key: "logs" },
  { lane: 2, key: "ops" },
  { lane: 6, key: "share" },
];

/** Reach one LANES surface with real clicks (see LANES for lanes 1 / 2 / 5). */
export async function navigateTo(page, lane) {
  if (lane === 2) {
    await page.click('.wb-nav button[data-wb-lane="4"]');
    await page.waitForTimeout(700);
    await page.click('.wb-section-tabs button[data-wb-lane="2"]');
  } else if (lane === 1 || lane === 5) {
    await page.click('.wb-nav button[data-wb-lane="1"]');
    await page.waitForTimeout(400);
    await page.click(`[data-wb-gw-sel="${lane === 1 ? "0" : "models"}"]`);
    if (lane === 1) {
      const tab = page.locator('[data-wb-gw-tab="account"]');
      if (await tab.count()) await tab.click();
    }
  } else {
    await page.click(`.wb-nav button[data-wb-lane="${lane}"]`);
  }
}

export const MASK_INIT = `(() => {
  const RE = /[A-Za-z0-9][A-Za-z0-9_\\-+\\/=.:]{23,}/g;
  const maskText = n => { const v = n.nodeValue; if (v && RE.test(v)) { RE.lastIndex = 0; n.nodeValue = v.replace(RE, m => "\\u2022".repeat(Math.min(m.length, 24))); } RE.lastIndex = 0; };
  const sweep = root => { if (!root) return; if (root.nodeType === 3) { maskText(root); return; } const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT); let n; while ((n = w.nextNode())) maskText(n); };
  const start = () => { sweep(document.documentElement); new MutationObserver(rs => { for (const r of rs) { if (r.type === "characterData") maskText(r.target); else r.addedNodes.forEach(sweep); } }).observe(document.documentElement, { subtree: true, childList: true, characterData: true }); };
  if (document.documentElement) start(); else document.addEventListener("DOMContentLoaded", start);
})();`;

/**
 * One gate run's shared state and helpers.
 * @param {{ base: string, out: string, quick?: boolean, settle?: number, laneWait?: number, entry?: string }} o
 *   settle / laneWait: ms to wait after opening a page / switching a lane. The
 *   fixture server answers instantly; a live 8001 needs ~16000 / ~6000.
 *   entry: query appended to the console address on every newPage.
 */
export async function createContext({
  base,
  out,
  quick = false,
  settle = 5500,
  laneWait = 2200,
  entry = "",
}) {
  const { chromium } = await loadPlaywright();
  const BASE = base.endsWith("/") ? base : `${base}/`;
  const HOST = new URL(BASE).host;
  mkdirSync(resolve(out, "shots"), { recursive: true });
  const failures = [],
    rows = [],
    nonGet = [],
    foreign = [];
  const browser = await chromium.launch({ channel: "chrome" });
  const ctx = {
    BASE,
    HOST,
    OUT: out,
    QUICK: quick,
    SETTLE: settle,
    LANE_WAIT: laneWait,
    ENTRY: entry,
    VIEWPORTS: quick ? [VIEWPORTS_FULL[0]] : VIEWPORTS_FULL,
    THEMES,
    LANES,
    failures,
    rows,
    nonGet,
    foreign,
    browser,
    fail: (label, msg) => failures.push(`${label}: ${msg}`),
    shot: (name) => resolve(out, "shots", `${name}.png`),
    /** Open the console. `override(route, req)` may answer a GET locally (a scenario
     *  fixture) and return true; it never sees a non-GET. A non-GET is answered 403
     *  here unless `nonGetAnswer(req)` returns a local { status, body } for it (a
     *  scenario's rejected action) - either way it is answered locally and never
     *  forwarded. Uncaught page errors are collected in the returned `errors`. */
    async newPage({
      width = 1600,
      height = 900,
      dsf = 1,
      theme = "light",
      prefs = {},
      reducedMotion = "no-preference",
      override = null,
      nonGetAnswer = null,
      settle: wait = settle,
      waitForContent = true,
      ready = ".workbench .wb-content",
    } = {}) {
      const bctx = await browser.newContext({
        viewport: { width, height },
        deviceScaleFactor: dsf,
        colorScheme: theme,
        reducedMotion,
        serviceWorkers: "block",
        locale: "zh-CN",
        timezoneId: "Asia/Shanghai",
      });
      await bctx.route("**/*", async (route) => {
        const req = route.request();
        const method = req.method();
        if (method !== "GET" && method !== "HEAD") {
          nonGet.push(`${method} ${req.url()}`);
          const local = nonGetAnswer ? nonGetAnswer(req) : null;
          if (local)
            return route.fulfill({
              status: local.status,
              contentType: "application/json",
              body: JSON.stringify(local.body ?? {}),
            });
          return route.fulfill({
            status: 403,
            contentType: "application/json",
            body: JSON.stringify({ error: "gate_read_only" }),
          });
        }
        let host = "";
        try {
          host = new URL(req.url()).host;
        } catch {}
        if (
          host !== HOST &&
          !req.url().startsWith("data:") &&
          !req.url().startsWith("blob:")
        ) {
          foreign.push(req.url());
          return route.abort();
        }
        if (override && (await override(route, req))) return;
        return route.continue();
      });
      const appearance = {
        theme,
        backdrop: "vivid",
        rail: "expanded",
        motion: "system",
        ...prefs,
      };
      await bctx.addInitScript(
        `try { localStorage.setItem("gateway-console-appearance-v1", ${JSON.stringify(JSON.stringify(appearance))}); } catch {}`
      );
      await bctx.addInitScript(MASK_INIT);
      const page = await bctx.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(String(e.message).slice(0, 160)));
      // `entry`: a query the console reads on load. The local candidate shows the
      // UM console by default; its workbench checks open `?console=workbench`.
      await page.goto(`${BASE}${entry}`, { waitUntil: "domcontentloaded" });
      if (waitForContent) await page.waitForSelector(ready, { timeout: 20000 });
      await page.waitForTimeout(wait);
      await page.evaluate(() => document.fonts.ready);
      return { ctx: bctx, page, errors };
    },
    async gotoLane(page, lane) {
      await navigateTo(page, lane);
      await page.waitForTimeout(laneWait);
      await page.evaluate(() => {
        const m = document.querySelector(".wb-main");
        if (m) m.scrollTop = 0;
      });
      await page.waitForTimeout(150);
    },
    async close() {
      await browser.close();
    },
  };
  return ctx;
}
