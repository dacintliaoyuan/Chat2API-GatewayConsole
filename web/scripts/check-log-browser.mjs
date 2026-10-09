// Console log-lane BROWSER acceptance for the standalone release tree (C4/C6).
//
// This tree has no browser-driven console gate, so the log-lane checks used to be
// textual only. This script serves the BUILT page from a temporary static server
// with an inline minimal API fixture and drives the REAL page with Playwright:
//
//   logs/copy-precision + raw-fold  the overview's 「复制完整原文」 must copy the text
//       the row SHOWS even while a background refresh has replaced part of
//       `logOverview` and the page has not repainted yet (the exact C4 defect: a
//       click-time re-derivation by list index would copy the refreshed text).
//       The opened raw fold must survive that repaint; a control proves the
//       refresh really changed the page data.
//   logs/filter                    the kind chips are the ONE classification entry:
//       a chip filters the table to its own kind, 「全部」 restores it, and its
//       count matches the rows listed.
//   logs/empty-table-wording       a chip that matches nothing names that kind
//       axis in the empty-table sentence, not a vague 「当前筛选无事件」.
//   logs/duplicates               two events with identical timestamp AND
//       identical content stay separately expandable and separately copyable.
//   logs/clipboard-refusal         a refused clipboard write is reported on the
//       button (复制失败…), never silently treated as copied.
//   logs/kind-colour               the row-level kind colour bar, the kind label
//       colour and the ERROR raw-text red are present in BOTH themes and on a
//       narrow viewport, wired to the resolved status tokens.
//   logs/detail-count              the detail status sentence counts the rows the
//       table actually lists and names the filtered-out remainder.
//
// Everything is local: the server binds 127.0.0.1 on an ephemeral port, the
// fixtures are inline synthetic values, and no real service, credential or
// production port is touched. Playwright is NOT a dependency of this tree: pass
// its module path via PLAYWRIGHT_MODULE (or install it as a devDependency).
//
//   node scripts/check-log-browser.mjs [--build <dir>] [--out <dir>] [--verbose]
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { extname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = resolve(fileURLToPath(new URL(".", import.meta.url)));
const root = resolve(here, "..");
const argv = process.argv.slice(2);
const opt = (name, dflt) => { const i = argv.indexOf(name); return i >= 0 ? argv[i + 1] : dflt; };
const flag = name => argv.includes(name);
const buildDir = resolve(opt("--build", join(root, "dist")));
// 结果写在源码树之外（默认系统临时目录）：本树是上传树，不新增可再生成产物。
const outDir = resolve(opt("--out", join(tmpdir(), "console-log-browser-check")));
const verbose = flag("--verbose");

const MIME = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
  ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
  ".wasm": "application/wasm", ".map": "application/json; charset=utf-8",
};

// ---------------------------------------------------------------- fixtures
// 合成夹具：每个来源各有自己的行（跨来源不重复），来源 0 的第一行在表内重复一份
// （同一时间戳 + 同一内容），供重复事件判据使用。
const TEMPLATES = [
  { level: "INFO", event: { outcome: "ok" }, message: "request summary request=alpha final=HTTP200 physical_attempts=1/1 duration_ms=812" },
  { level: "WARNING", event: { outcome: "warn" }, message: "upstream slow response request=beta duration_ms=5400" },
  { level: "ERROR", event: { outcome: "failed" }, message: "upstream read error request=gamma stage=stream\nstack: fixture-trace-line-one\nstack: fixture-trace-line-two" },
  { level: "INFO", event: { outcome: "none" }, message: "scheduler tick interval=60s next=on" },
];
// 来源名必须落在发布版自己的公开范围内（public-scope.PUBLIC_LOG_SOURCES）。
const SOURCES = [
  { source: "chat2api", display: "chat2api 主链", hour: "09", categories: [{ category: "service", display: "服务" }] },
  { source: "console", display: "控制台", hour: "08", categories: [{ category: "service", display: "服务" }] },
  { source: "console_operations", display: "控制台操作", hour: "07", categories: [{ category: "service", display: "服务" }] },
  { source: "share", display: "分享入口", hour: "06", categories: [{ category: "service", display: "服务" }] },
  { source: "share-backend", display: "分享后端", hour: "05", categories: [{ category: "service", display: "服务" }] },
];
const REFRESHED_PREFIX = "REFRESHED ";
function rowsFor(source, { refreshed = false, infoOnly = false } = {}) {
  const prefix = refreshed ? REFRESHED_PREFIX : "";
  const rows = TEMPLATES.map((template, index) => ({
    ts: `2026-10-08 ${source.hour}:${String(12 - index).padStart(2, "0")}:00`,
    level: template.level, parse_ok: true, event: { ...template.event },
    message: prefix + template.message,
  }));
  if (infoOnly) return rows.filter(row => row.level === "INFO" && row.event.outcome !== "ok");
  if (source.source === SOURCES[0].source) rows.splice(1, 0, { ...rows[0], event: { ...rows[0].event } });
  return rows;
}
/** Long enough for the first refresh batch (800ms) to land, short enough
 *  that the last one (6000ms) has not: the window where logOverview is partly
 *  replaced and the page has not repainted yet. */
const MID_REFRESH_WAIT = 2500;
const REFRESH_DONE_WAIT = 5200;

function startFixtureServer() {
  let phase = "normal";
  const firstBatch = new Set(SOURCES.slice(0, 3).map(source => source.source));
  const server = createServer(async (req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    const json = (payload, status = 200) => {
      res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
      res.end(JSON.stringify(payload));
    };
    if (url.pathname === "/__phase/refresh") { phase = "refresh"; return json({ phase }); }
    if (url.pathname === "/__phase/entries-info-only") { phase = "info"; return json({ phase }); }
    if (url.pathname === "/__phase/normal") { phase = "normal"; return json({ phase }); }
    if (url.pathname === "/api/console/log-sources") return json({ sources: SOURCES.map(({ hour, ...rest }) => rest) });
    if (url.pathname === "/api/console/log-entries") {
      const source = url.searchParams.get("source") ?? "";
      const meta = SOURCES.find(entry => entry.source === source) ?? SOURCES[0];
      if (phase === "info") return json({ status: "ok", rows: rowsFor(meta, { infoOnly: true }), truncated: false });
      if (phase !== "refresh") return json({ status: "ok", rows: rowsFor(meta), truncated: false });
      // 批 1（前三个来源）先落地、其余留到最后：制造「已换数据、未重绘」的窗口。
      await new Promise(done => setTimeout(done, firstBatch.has(source) ? 800 : 6000));
      return json({ status: "ok", rows: rowsFor(meta, { refreshed: true }), truncated: false });
    }
    if (url.pathname === "/api/summary") return json({ ok: true });
    if (url.pathname.startsWith("/api/console/")) return json({ status: "ok", services: [], checked_at: 0 });
    // Static page (SPA: unknown paths fall back to index.html).
    const candidate = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/+/, "");
    const file = join(buildDir, candidate);
    if (!file.startsWith(buildDir) || !existsSync(file)) {
      const fallback = join(buildDir, "index.html");
      if (!existsSync(fallback)) { res.writeHead(404); return res.end("missing build"); }
      const html = await readFile(fallback);
      res.writeHead(200, { "content-type": MIME[".html"] });
      return res.end(html);
    }
    const body = await readFile(file);
    res.writeHead(200, { "content-type": MIME[extname(file)] ?? "application/octet-stream" });
    res.end(body);
  });
  return new Promise(done => server.listen(0, "127.0.0.1", () => done({ server, url: `http://127.0.0.1:${server.address().port}` })));
}

async function loadPlaywright() {
  const candidates = [process.env.PLAYWRIGHT_MODULE,
    resolve(here, "node_modules/playwright/index.mjs"), "playwright"].filter(Boolean);
  for (const candidate of candidates) {
    try { return await import(candidate.startsWith(".") || candidate.includes(":\\") || candidate.includes(":/") ? pathToFileURL(candidate).href : candidate); }
    catch { /* try the next candidate */ }
  }
  throw new Error("Playwright 不可用：设置 PLAYWRIGHT_MODULE 指向 playwright/index.mjs，或在本目录安装 playwright");
}

const failures = [];
const checks = [];
const fail = (label, detail) => { failures.push(`${label}：${detail}`); if (verbose) console.error(`FAIL ${label}: ${detail}`); };

const logRows = page => page.locator(".wb-log-overview-table tbody tr");
const detailRows = page => page.locator(".wb-log-event-table tbody tr");
const maskKeys = text => String(text ?? "").replace(/[A-Za-z0-9][A-Za-z0-9_\-+\/=.:]{23,}/g, match => "•".repeat(Math.min(match.length, 24)));
const waitOverview = page => page.waitForSelector(".wb-log-overview-table [data-wb-log-overview-copy]", { timeout: 30000 });
const waitDetail = page => page.waitForSelector(".wb-log-event-table [data-wb-log-copy]", { timeout: 30000 });

async function installCopySpy(page) {
  await page.evaluate(() => {
    window.__gateCopied = [];
    const record = { writeText: async text => { window.__gateCopied.push(String(text)); } };
    try { Object.defineProperty(navigator, "clipboard", { configurable: true, value: record }); }
    catch { navigator.clipboard.writeText = record.writeText; }
  });
}
async function installCopyRefusal(page) {
  await page.evaluate(() => {
    const refuse = { writeText: async () => { throw new Error("gate_clipboard_refused"); } };
    try { Object.defineProperty(navigator, "clipboard", { configurable: true, value: refuse }); }
    catch { navigator.clipboard.writeText = refuse.writeText; }
  });
}
const copied = page => page.evaluate(() => (window.__gateCopied ?? []).at(-1));

async function main() {
  if (!existsSync(join(buildDir, "index.html"))) throw new Error(`构建产物缺失：${join(buildDir, "index.html")}（先 npm run build）`);
  const { chromium } = await loadPlaywright();
  const { server, url } = await startFixtureServer();
  const browser = await chromium.launch({ channel: "chrome" });
  const errors = [];
  const newPage = async ({ width = 1600, height = 900, theme = "light" } = {}) => {
    const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: theme });
    const page = await ctx.newPage();
    page.on("pageerror", error => errors.push(String(error?.message ?? error)));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.goto(url, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(900);
    await page.evaluate(() => { localStorage.setItem("wb-theme", document.documentElement.dataset.darkSurface === "true" ? "dark" : "light"); });
    return { page, ctx };
  };
  const gotoLogs = async (page, phase = "normal") => {
    // 夹具阶段是服务端全局状态：每个检查先显式复位，避免上一个检查的阶段泄漏。
    await page.evaluate(name => fetch(`/__phase/${name}`).then(response => response.json()), phase);
    await page.click('.wb-nav button[data-wb-lane="4"]');
    await page.waitForTimeout(900);
    await page.evaluate(() => { const main = document.querySelector(".wb-main"); if (main) main.scrollTop = 0; });
    await page.waitForTimeout(150);
  };
  const pageError = (label) => { if (errors.length) fail(label, `页面报错：${errors[0]}`); };
  const broken = (label, error) => fail(label, `检查自身异常，该判据不成立：${String(error?.message ?? error).split("\n")[0].slice(0, 160)}`);

  try {
    // ------------------------------------------- 1 copy precision across a refresh + raw fold survives it
    {
      const { page, ctx } = await newPage();
      await gotoLogs(page);
      try {
        await waitOverview(page);
        // 身份而不是位置：刷新会换掉列表，按下标取行会落到另一条事件上。
        const identity = await logRows(page).first().locator("[data-wb-log-raw-toggle]").getAttribute("data-wb-log-raw-toggle");
        const rowFor = id => page.locator(`.wb-log-overview-table tbody tr:has([data-wb-log-raw-toggle="${id}"])`);
        let row = rowFor(identity);
        const toggle = row.locator("[data-wb-log-raw-toggle]");
        if ((await toggle.getAttribute("aria-expanded")) !== "false") throw new Error("原文初始不是折叠态");
        await toggle.click();
        await page.waitForTimeout(200);
        const shown = await row.locator("pre.wb-log-msg").first().textContent();
        if (!shown) throw new Error("展开后没有原文块");
        if ((await toggle.getAttribute("aria-expanded")) !== "true") throw new Error("展开后 aria-expanded 不是 true");
        await installCopySpy(page);
        await row.locator("[data-wb-log-overview-copy]").click();
        const baseline = await copied(page);
        if (maskKeys(baseline) !== shown) fail("logs/原文复制", `展开后复制的内容与屏上原文不一致（屏上 ${JSON.stringify((shown ?? "").slice(0, 40))} / 复制 ${JSON.stringify((baseline ?? "").slice(0, 40))}）`);
        await toggle.click();
        await page.waitForTimeout(200);
        if ((await toggle.getAttribute("aria-expanded")) !== "false") throw new Error("收起后 aria-expanded 不是 false");
        await toggle.click();
        await page.waitForTimeout(200);
        // 后台刷新：批 1 已换掉 logOverview，页面尚未重绘。点击复制必须仍取屏上那一条。
        await page.evaluate(() => fetch("/__phase/refresh").then(response => response.json()));
        await page.locator("[data-wb-log-overview-refresh]").click();
        await page.waitForTimeout(MID_REFRESH_WAIT);
        row = rowFor(identity);
        if (!(await row.count())) fail("logs/刷新间复制", "刷新间原始行按身份消失（展开身份不稳定）");
        const shownDuring = await row.locator("pre.wb-log-msg").first().textContent();
        if (!shownDuring) throw new Error("刷新重绘后原文块消失（展开状态没有跨重绘保持）");
        if (shownDuring !== shown) fail("logs/刷新间复制", "刷新重绘把原文块换成了另一条事件（展开身份不稳定）");
        await installCopySpy(page);
        await row.locator("[data-wb-log-overview-copy]").click();
        const during = await copied(page);
        if (maskKeys(during) !== shownDuring) fail("logs/刷新间复制", `后台刷新期间复制了另一条事件的原文（屏上 ${JSON.stringify((shownDuring ?? "").slice(0, 40))} / 复制 ${JSON.stringify((during ?? "").slice(0, 40))}）`);
        // 对照：刷新确实换了数据——旧身份消失、新行复制到的是刷新后的原文。
        await page.waitForTimeout(REFRESH_DONE_WAIT);
        if (await rowFor(identity).count()) fail("logs/刷新间复制", "刷新完成后旧身份仍在（无法证明复制取的是刷新前原文）");
        const newTop = logRows(page).first();
        const newToggle = newTop.locator("[data-wb-log-raw-toggle]");
        const newIdentity = await newToggle.getAttribute("data-wb-log-raw-toggle");
        if (newIdentity === identity) fail("logs/刷新间复制", "刷新后行身份没变（夹具改动没落到页面）");
        await newToggle.click();
        await page.waitForTimeout(200);
        await installCopySpy(page);
        await newTop.locator("[data-wb-log-overview-copy]").click();
        const after = await copied(page);
        if (!String(after ?? "").startsWith(REFRESHED_PREFIX)) fail("logs/刷新间复制", `刷新没有把夹具改动落到页面：${JSON.stringify(String(after ?? "").slice(0, 60))}`);
        pageError("logs/刷新间复制");
        checks.push("copy-precision", "raw-fold");
      } catch (error) { broken("logs/原文复制", error); } finally { await ctx.close(); }
    }

    // ------------------------------------------- 2 kind chips are the one classification entry
    {
      const { page, ctx } = await newPage();
      await gotoLogs(page);
      try {
        await waitOverview(page);
        const total = await logRows(page).count();
        if (!total) throw new Error("总览表没有行");
        await page.click('[data-wb-log-overview-filter="error"]');
        await page.waitForTimeout(300);
        const shown = await page.$$eval(".wb-log-overview-table tbody tr", trs => trs.map(tr => tr.className));
        if (!shown.length) throw new Error("「错误 / 异常」筛选后没有行");
        const stray = shown.filter(name => !name.includes("wb-log-kind-error"));
        if (stray.length) fail("logs/分类筛选", `分类筛选后出现了非该类行：${stray.slice(0, 2).join(" / ")}`);
        const chip = await page.locator('[data-wb-log-overview-filter="error"]').innerText();
        const chipCount = Number(/ (\d+)\s*$/.exec(chip)?.[1]);
        if (chipCount !== shown.length) fail("logs/分类筛选", `分类 chip 计数 ${JSON.stringify(chip)} 与实际列出 ${shown.length} 行不一致`);
        await page.click('[data-wb-log-overview-filter="all"]');
        await page.waitForTimeout(300);
        const restored = await logRows(page).count();
        if (restored !== total) fail("logs/分类筛选", `切回「全部」后行数 ${restored} ≠ 原 ${total}`);
        pageError("logs/分类筛选");
        checks.push("filter-switch");
      } catch (error) { broken("logs/分类筛选", error); } finally { await ctx.close(); }
    }

    // ------------------------------------------- 3 empty-table sentence names the kind axis
    {
      const { page, ctx } = await newPage();
      await gotoLogs(page);
      try {
        await waitOverview(page);
        // 切到「只有信息行」的夹具：错误分类下必须为空，才能验证空表句。
        await page.evaluate(() => fetch("/__phase/entries-info-only").then(response => response.json()));
        await page.locator("[data-wb-log-overview-refresh]").click();
        await page.waitForTimeout(MID_REFRESH_WAIT);
        await page.click('[data-wb-log-overview-filter="error"]');
        await page.waitForTimeout(300);
        const message = await page.locator(".wb-log-overview-table tbody").innerText();
        if (!message.includes("错误")) fail("logs/分类空表句", `空表句没有点名实际分类筛选值：${JSON.stringify(message.slice(0, 80))}`);
        if (!message.includes("分类")) fail("logs/分类空表句", `空表句没有点名分类筛选轴：${JSON.stringify(message.slice(0, 80))}`);
        pageError("logs/分类空表句");
        checks.push("empty-table-wording");
      } catch (error) { broken("logs/分类空表句", error); } finally { await ctx.close(); }
    }

    // ------------------------------------------- 4 duplicates stay separately expandable
    {
      const { page, ctx } = await newPage();
      await gotoLogs(page);
      try {
        await waitOverview(page);
        const first = logRows(page).first();
        const second = logRows(page).nth(1);
        const firstToggle = first.locator("[data-wb-log-raw-toggle]");
        const secondToggle = second.locator("[data-wb-log-raw-toggle]");
        if (!(await firstToggle.count()) || !(await secondToggle.count())) throw new Error("重复行没有原文展开按钮");
        const expandedCount = () => page.locator('.wb-log-overview-table [data-wb-log-raw-toggle][aria-expanded="true"]').count();
        await firstToggle.click();
        await page.waitForTimeout(200);
        const oneOpen = await expandedCount();
        if (oneOpen !== 1) fail("logs/重复事件", `展开第一条后展开态有 ${oneOpen} 条（相同时间戳 + 相同内容被并成同一身份）`);
        await secondToggle.click();
        await page.waitForTimeout(200);
        const twoOpen = await expandedCount();
        if (twoOpen !== 2) fail("logs/重复事件", `两条重复事件不能各自展开（展开态 ${twoOpen}）`);
        const firstText = await first.locator("pre.wb-log-msg").first().textContent();
        const secondText = await second.locator("pre.wb-log-msg").first().textContent();
        if (!firstText || firstText !== secondText) throw new Error("夹具没有产生内容相同的重复事件");
        await installCopySpy(page);
        await second.locator("[data-wb-log-overview-copy]").click();
        const got = await copied(page);
        if (maskKeys(got) !== secondText) fail("logs/重复事件", `重复事件行复制取到的不是该行原文（该行 ${JSON.stringify((secondText ?? "").slice(0, 40))} / 复制 ${JSON.stringify((got ?? "").slice(0, 40))}）`);
        await firstToggle.click();
        await page.waitForTimeout(200);
        const afterCollapse = await expandedCount();
        if (afterCollapse !== 1) fail("logs/重复事件", `收起一条后展开态有 ${afterCollapse} 条（收起没有按同一身份落回）`);
        pageError("logs/重复事件");
        checks.push("duplicate-events");
      } catch (error) { broken("logs/重复事件", error); } finally { await ctx.close(); }
    }

    // ------------------------------------------- 5 clipboard refusal is reported, not faked
    {
      const { page, ctx } = await newPage();
      await gotoLogs(page);
      try {
        await waitOverview(page);
        await installCopyRefusal(page);
        const button = logRows(page).first().locator("[data-wb-log-overview-copy]");
        await button.click();
        await page.waitForTimeout(300);
        const label = await button.textContent();
        if (!String(label ?? "").includes("复制失败")) fail("logs/剪贴板拒绝", `剪贴板拒绝后按钮没有如实反馈：${JSON.stringify(String(label ?? "").slice(0, 40))}`);
        pageError("logs/剪贴板拒绝");
        checks.push("clipboard-refusal");
      } catch (error) { broken("logs/剪贴板拒绝", error); } finally { await ctx.close(); }
    }

    // ------------------------------------------- 6 kind colours in both themes and on a narrow viewport
    for (const [tag, options] of [["light", { width: 1600, height: 900, theme: "light" }], ["dark", { width: 1600, height: 900, theme: "dark" }], ["narrow", { width: 880, height: 800, theme: "light" }]]) {
      const { page, ctx } = await newPage(options);
      await gotoLogs(page);
      try {
        await waitOverview(page);
        const probe = await page.$$eval(".wb-log-overview-table tbody tr", trs => {
          const token = (() => {
            const host = document.querySelector(".workbench") ?? document.body;
            const el = document.createElement("span");
            el.style.color = "var(--wb-status-err)";
            host.appendChild(el);
            const resolved = getComputedStyle(el).color;
            el.remove();
            return resolved;
          })();
          return {
            cells: trs.map(tr => {
              const cell = tr.querySelector("td");
              const style = getComputedStyle(cell);
              const label = tr.querySelector(".wb-log-kind");
              return {
                kind: [...tr.classList].find(name => name.startsWith("wb-log-kind-")) ?? "",
                bar: `${style.borderLeftWidth} ${style.borderLeftStyle}`,
                barColor: style.borderLeftColor,
                labelColor: label ? getComputedStyle(label).color : "",
              };
            }),
            errorToken: token,
          };
        });
        const kinds = [...new Set(probe.cells.map(cell => cell.kind))];
        if (!kinds.length) throw new Error("总览表没有行");
        const missing = probe.cells.filter(cell => !cell.bar.startsWith("3px") || cell.barColor === "rgba(0, 0, 0, 0)");
        if (missing.length) fail("logs/分类色条", `[${tag}] ${missing.length} / ${probe.cells.length} 行没有行级分类色条`);
        const colours = new Set(probe.cells.map(cell => cell.barColor));
        if (colours.size !== kinds.length) fail("logs/分类色条", `[${tag}] ${kinds.length} 个分类只画出 ${colours.size} 种色条色：${[...colours].join(" / ")}`);
        const labelColours = new Set(probe.cells.map(cell => cell.labelColor));
        if (labelColours.size !== kinds.length) fail("logs/分类色条", `[${tag}] 分类标签颜色没有按分类区分：${[...labelColours].join(" / ")}`);
        const errorToggle = page.locator(".wb-log-overview-table tbody tr.wb-log-kind-error [data-wb-log-raw-toggle]").first();
        if (!(await errorToggle.count())) throw new Error("总览表没有 ERROR 行，无法验证错误红");
        await errorToggle.click();
        await page.waitForTimeout(200);
        const errorMsgColor = await page.locator(".wb-log-overview-table tbody tr.wb-log-kind-error pre.wb-log-msg").first().evaluate(el => getComputedStyle(el).color);
        if (errorMsgColor !== probe.errorToken) fail("logs/错误原文红", `[${tag}] ERROR 原文色 ${errorMsgColor} 未接 --wb-status-err（${probe.errorToken}）`);
        pageError("logs/分类色条");
      } catch (error) { broken(`logs/分类色条[${tag}]`, error); } finally { await ctx.close(); }
    }
    checks.push("kind-colour-light", "kind-colour-dark", "kind-colour-narrow");

    // ------------------------------------------- 7 detail status sentence counts what the table lists
    {
      const { page, ctx } = await newPage();
      await gotoLogs(page);
      try {
        await waitOverview(page);
        await page.locator("[data-log-open]").first().click();
        await waitDetail(page);
        const all = await detailRows(page).count();
        await page.click('.wb-log-detail [data-wb-log-overview-filter="error"]');
        await page.waitForTimeout(400);
        const listed = await detailRows(page).count();
        if (!listed || listed >= all) throw new Error(`夹具没有筛掉任何行（列出 ${listed} / ${all}）`);
        const state = await page.locator(".wb-log-detail .wb-log-state").innerText();
        if (!state.includes(String(listed)) || !state.includes(String(all))) fail("logs/详情计数", `详情状态句没有写筛选后 / 全部计数：${JSON.stringify(state.slice(0, 90))}`);
        pageError("logs/详情计数");
        checks.push("detail-filtered-count");
      } catch (error) { broken("logs/详情计数", error); } finally { await ctx.close(); }
    }
  } finally {
    await browser.close();
    await new Promise(done => server.close(done));
  }

  const result = { ok: failures.length === 0, checks, failureCount: failures.length, failures, build: buildDir, out: outDir };
  console.log(JSON.stringify(result, null, 2));
  process.exitCode = failures.length === 0 ? 0 : 1;
}

main().catch(error => {
  console.error(JSON.stringify({ ok: false, error: String(error?.message ?? error) }, null, 2));
  process.exitCode = 2;
});
