// Console gate - runtime-log lane checks (C4): page-level event handlers on the
// log lane, not just pure-function checks. Every read is answered by an override
// on the SAME offline fixture server the other groups use; nothing is proxied and
// every non-GET is still answered 403 locally by core.mjs.
//
//   logs/copy-precision + raw-fold: the overview's 「复制完整原文」 must copy the
//       text the row SHOWS, even while a background refresh has already replaced
//       part of `logOverview` but has not repainted yet (the exact C4 defect: a
//       click-time re-derivation by list index would copy the refreshed text).
//       The opened raw fold must survive that repaint, and the control at the end
//       proves the refresh really changed the page data.
//   logs/filter: the kind chips are the ONE classification entry: a chip filters
//       the table to its own kind, 「全部」 restores it, and its count matches
//       the rows listed.
//   logs/empty-table-wording: a chip that matches nothing names that kind axis in
//       the empty-table sentence (C4 wording), not a vague 「当前筛选无事件」.
//   logs/duplicates: two events with identical timestamp AND identical content
//       stay separately expandable (an identity reduced to content alone would
//       collapse them).
//   logs/clipboard-refusal: a refused clipboard write is reported on the button
//       (复制失败…), never silently treated as copied.
//   logs/kind-colour: the row-level kind colour bar, the kind label colour and
//       the ERROR raw-text red are present in BOTH themes and on a narrow
//       viewport, wired to the resolved status tokens.
//   logs/detail-count: the detail status sentence counts the rows the table
//       actually lists and names the filtered-out remainder (C4).
import { logOverviewFixture } from "./log-overview-fixture.mjs";
const LOG_OVERVIEW_PATH = "/gw/usagesrv/api/console/log-overview";
const LOG_ENTRIES_PATH = "/gw/usagesrv/api/console/log-entries";
/** 夹具键不带 /gw/usagesrv 前缀（fixture-server 读取前会剥掉它）。 */
const LOG_ENTRIES_FIXTURE = "/api/console/log-entries";
const REFRESHED_PREFIX = "REFRESHED ";
/** Long enough for the first refresh batch (800ms) to land, short enough that the
 *  last one (6000ms) has not: the window where logOverview is partly replaced
 *  and the page has not repainted yet. */
const MID_REFRESH_WAIT = 2500;
/** After this the whole delayed refresh has landed and repainted. */
const REFRESH_DONE_WAIT = 5200;

const logRows = (page) => page.locator(".wb-log-overview-table tbody tr");
const detailRows = (page) => page.locator(".wb-log-event-table tbody tr");
/** 与 core.mjs 的 MASK_INIT 同一口径：页面把 24+ 字符的形似密钥串打码后才读 /
 *  截图，而复制取到的是未打码的原文，所以两侧都先按同一规则打码再比较。 */
const maskKeys = (text) =>
  String(text ?? "").replace(/[A-Za-z0-9][A-Za-z0-9_\-+\/=.:]{23,}/g, (match) =>
    "•".repeat(Math.min(match.length, 24))
  );
/** 真实数据行的标记（不是「正在读取各来源…」占位行）。 */
const OVERVIEW_READY = "[data-wb-log-overview-copy]";
const DETAIL_READY = "[data-wb-log-copy]";
const waitOverview = (page) =>
  page.waitForSelector(`.wb-log-overview-table ${OVERVIEW_READY}`, {
    timeout: 30000,
  });
const waitDetail = (page) =>
  page.waitForSelector(`.wb-log-event-table ${DETAIL_READY}`, {
    timeout: 30000,
  });

/** Record every clipboard write instead of touching the real clipboard. */
async function installCopySpy(page) {
  await page.evaluate(() => {
    window.__gateCopied = [];
    const record = {
      writeText: async (text) => {
        window.__gateCopied.push(String(text));
      },
    };
    try {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: record,
      });
    } catch {
      navigator.clipboard.writeText = record.writeText;
    }
  });
}
/** Refuse every clipboard write (the permission-denied path). */
async function installCopyRefusal(page) {
  await page.evaluate(() => {
    const refuse = {
      writeText: async () => {
        throw new Error("gate_clipboard_refused");
      },
    };
    try {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: refuse,
      });
    } catch {
      navigator.clipboard.writeText = refuse.writeText;
    }
  });
}
const copied = (page) =>
  page.evaluate(() => (window.__gateCopied ?? []).at(-1));

export async function runLogs(c, { fixtures }) {
  const fixture =
    fixtures[`${LOG_ENTRIES_FIXTURE}?source=chat2api&category=service`];
  if (!fixture?.body?.rows?.length) {
    c.fail("logs", "夹具缺少 log-entries 行，日志页判据不成立");
    return { ok: false, checks: 0 };
  }
  const sources = (
    fixtures["/api/console/log-sources"]?.body?.sources ?? []
  ).filter((s) => (s.categories ?? []).length);
  if (sources.length < 2) {
    c.fail("logs", "夹具可读来源少于两个，刷新间复制判据不成立");
    return { ok: false, checks: 0 };
  }
  const rows = structuredClone(fixture.body.rows);
  // 复制最高时间戳里的第一行并紧跟其后插入：表格按 ts 倒序后，重复内容落在最前两行。
  const topTs = rows
    .map((row) => row.ts)
    .filter(Boolean)
    .sort()
    .at(-1);
  const dupIndex = rows.findIndex((row) => row.ts === topTs);
  const duplicate = { ...structuredClone(rows[dupIndex]) };
  const withDuplicate = [
    ...rows.slice(0, dupIndex + 1),
    duplicate,
    ...rows.slice(dupIndex + 1),
  ];
  const initialBody = { ...structuredClone(fixture.body), rows: withDuplicate };
  const refreshedBody = {
    ...structuredClone(fixture.body),
    rows: withDuplicate.map((row) => ({
      ...row,
      message: `${REFRESHED_PREFIX}${row.message}`,
    })),
  };
  const infoOnlyBody = {
    ...structuredClone(fixture.body),
    rows: rows.filter(
      (row) =>
        (row.level ?? "").toUpperCase() === "INFO" &&
        row.event?.outcome !== "ok"
    ),
  };
  const aggregate = (body) =>
    logOverviewFixture(fixtures, { "chat2api/service": body });
  let refreshPhase = false;
  const override = async (route, req) => {
    const url = req.url();
    const parsed = new URL(url);
    const overview = parsed.pathname === LOG_OVERVIEW_PATH;
    const detail =
      parsed.pathname === LOG_ENTRIES_PATH &&
      parsed.searchParams.get("source") === "chat2api" &&
      parsed.searchParams.get("category") === "service";
    if (!overview && !detail) return false;
    // One aggregate response now lands atomically; exercise copying the old
    // rendered event while this real consumer is still awaiting its refresh.
    if (refreshPhase) await new Promise((done) => setTimeout(done, 6000));
    const body = refreshPhase ? refreshedBody : initialBody;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(overview ? aggregate(body) : body),
    });
    return true;
  };
  const checks = [];
  const pageError = (label, errors) => {
    if (errors.length) c.fail(label, `页面报错：${errors[0]}`);
  };
  const broken = (label, error) =>
    c.fail(
      label,
      `门禁自身异常，该判据不成立：${String(error?.message ?? error)
        .split("\n")[0]
        .slice(0, 160)}`
    );

  // ------------------------------------------------- 1 copy precision across a refresh + raw fold survives it
  {
    const { page, errors, ctx } = await c.newPage({ override, settle: 1200 });
    await c.gotoLane(page, 4);
    try {
      await waitOverview(page);
      const first = logRows(page).first();
      const toggle = first.locator("[data-wb-log-raw-toggle]");
      if (!(await toggle.count()))
        throw new Error("总览第一行没有原文展开按钮");
      if ((await toggle.getAttribute("aria-expanded")) !== "false")
        throw new Error("原文初始不是折叠态");
      await toggle.click();
      await page.waitForTimeout(200);
      const shown = await first.locator("pre.wb-log-msg").first().textContent();
      if (!shown) throw new Error("展开后没有原文块");
      if ((await toggle.getAttribute("aria-expanded")) !== "true")
        throw new Error("展开后 aria-expanded 不是 true");
      await installCopySpy(page);
      await first.locator("[data-wb-log-overview-copy]").click();
      const baseline = await copied(page);
      if (maskKeys(baseline) !== shown)
        c.fail(
          "logs/原文复制",
          `展开后复制的内容与屏上原文不一致（屏上 ${JSON.stringify((shown ?? "").slice(0, 40))} / 复制 ${JSON.stringify((baseline ?? "").slice(0, 40))}）`
        );
      await toggle.click();
      await page.waitForTimeout(200);
      if ((await toggle.getAttribute("aria-expanded")) !== "false")
        throw new Error("收起后 aria-expanded 不是 false");
      await toggle.click();
      await page.waitForTimeout(200);
      // Aggregate refresh is pending; copy must still use the rendered event.
      refreshPhase = true;
      await page.locator("[data-wb-log-overview-refresh]").click();
      await page.waitForTimeout(MID_REFRESH_WAIT);
      const shownDuring = await first
        .locator("pre.wb-log-msg")
        .first()
        .textContent();
      if (!shownDuring)
        throw new Error("刷新重绘后原文块消失（展开状态没有跨重绘保持）");
      if (shownDuring !== shown)
        c.fail(
          "logs/刷新间复制",
          "刷新重绘把原文块换成了另一条事件（展开身份不稳定）"
        );
      await installCopySpy(page);
      await first.locator("[data-wb-log-overview-copy]").click();
      const during = await copied(page);
      if (maskKeys(during) !== shownDuring)
        c.fail(
          "logs/刷新间复制",
          `后台刷新期间复制了另一条事件的原文（屏上 ${JSON.stringify((shownDuring ?? "").slice(0, 40))} / 复制 ${JSON.stringify((during ?? "").slice(0, 40))}）`
        );
      // 对照：刷新确实换了数据，否则上面的检查是空的。
      await page.waitForTimeout(REFRESH_DONE_WAIT);
      await installCopySpy(page);
      await first.locator("[data-wb-log-overview-copy]").click();
      const after = await copied(page);
      if (!String(after ?? "").startsWith(REFRESHED_PREFIX))
        c.fail(
          "logs/刷新间复制",
          `刷新没有把夹具改动落到页面：${JSON.stringify(String(after ?? "").slice(0, 60))}`
        );
      pageError("logs/刷新间复制", errors);
      checks.push("copy-precision", "raw-fold");
    } catch (e) {
      broken("logs/原文复制", e);
    } finally {
      await ctx.close();
    }
  }

  refreshPhase = false;

  // ------------------------------------------------- 2 kind chips are the one classification entry
  {
    const { page, errors, ctx } = await c.newPage({ override, settle: 1200 });
    await c.gotoLane(page, 4);
    try {
      await waitOverview(page);
      const total = await logRows(page).count();
      if (!total) throw new Error("总览表没有行");
      await page.click('[data-wb-log-overview-filter="error"]');
      await page.waitForTimeout(300);
      const shown = await page.$$eval(
        ".wb-log-overview-table tbody tr",
        (trs) => trs.map((tr) => tr.className)
      );
      if (!shown.length) throw new Error("「错误 / 异常」筛选后没有行");
      const stray = shown.filter((name) => !name.includes("wb-log-kind-error"));
      if (stray.length)
        c.fail(
          "logs/分类筛选",
          `分类筛选后出现了非该类行：${stray.slice(0, 2).join(" / ")}`
        );
      const chip = await page
        .locator('[data-wb-log-overview-filter="error"]')
        .innerText();
      const chipCount = Number(/ (\d+)\s*$/.exec(chip)?.[1]);
      if (chipCount !== shown.length)
        c.fail(
          "logs/分类筛选",
          `分类 chip 计数 ${JSON.stringify(chip)} 与实际列出 ${shown.length} 行不一致`
        );
      await page.click('[data-wb-log-overview-filter="all"]');
      await page.waitForTimeout(300);
      const restored = await logRows(page).count();
      if (restored !== total)
        c.fail("logs/分类筛选", `切回「全部」后行数 ${restored} ≠ 原 ${total}`);
      pageError("logs/分类筛选", errors);
      checks.push("filter-switch");
    } catch (e) {
      broken("logs/分类筛选", e);
    } finally {
      await ctx.close();
    }
  }

  // ------------------------------------------------- 3 empty-table sentence names the kind axis
  {
    const infoOverride = async (route, req) => {
      if (new URL(req.url()).pathname !== LOG_OVERVIEW_PATH) return false;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(aggregate(infoOnlyBody)),
      });
      return true;
    };
    const { page, errors, ctx } = await c.newPage({
      override: infoOverride,
      settle: 1200,
    });
    await c.gotoLane(page, 4);
    try {
      await waitOverview(page);
      await page.click('[data-wb-log-overview-filter="error"]');
      await page.waitForTimeout(300);
      const message = await page
        .locator(".wb-log-overview-table tbody")
        .innerText();
      if (!message.includes("错误"))
        c.fail(
          "logs/分类空表句",
          `空表句没有点名实际分类筛选值：${JSON.stringify(message.slice(0, 80))}`
        );
      if (!message.includes("分类"))
        c.fail(
          "logs/分类空表句",
          `空表句没有点名分类筛选轴：${JSON.stringify(message.slice(0, 80))}`
        );
      pageError("logs/分类空表句", errors);
      checks.push("empty-table-wording");
    } catch (e) {
      broken("logs/分类空表句", e);
    } finally {
      await ctx.close();
    }
  }

  // ------------------------------------------------- 4 duplicates stay separately expandable
  {
    const { page, errors, ctx } = await c.newPage({ override, settle: 1200 });
    await c.gotoLane(page, 4);
    try {
      await waitOverview(page);
      const first = logRows(page).first();
      const second = logRows(page).nth(1);
      const firstToggle = first.locator("[data-wb-log-raw-toggle]");
      const secondToggle = second.locator("[data-wb-log-raw-toggle]");
      if (!(await firstToggle.count()) || !(await secondToggle.count()))
        throw new Error("重复行没有原文展开按钮");
      const expandedCount = () =>
        page
          .locator(
            '.wb-log-overview-table [data-wb-log-raw-toggle][aria-expanded="true"]'
          )
          .count();
      await firstToggle.click();
      await page.waitForTimeout(200);
      const oneOpen = await expandedCount();
      if (oneOpen !== 1)
        c.fail(
          "logs/重复事件",
          `展开第一条后展开态有 ${oneOpen} 条（相同时间戳 + 相同内容被并成同一身份）`
        );
      await secondToggle.click();
      await page.waitForTimeout(200);
      const twoOpen = await expandedCount();
      if (twoOpen !== 2)
        c.fail(
          "logs/重复事件",
          `两条重复事件不能各自展开（展开态 ${twoOpen}）`
        );
      const firstText = await first
        .locator("pre.wb-log-msg")
        .first()
        .textContent();
      const secondText = await second
        .locator("pre.wb-log-msg")
        .first()
        .textContent();
      if (!firstText || firstText !== secondText)
        throw new Error("夹具没有产生内容相同的重复事件");
      await installCopySpy(page);
      await second.locator("[data-wb-log-overview-copy]").click();
      const got = await copied(page);
      if (maskKeys(got) !== secondText)
        c.fail(
          "logs/重复事件",
          `重复事件行复制取到的不是该行原文（该行 ${JSON.stringify((secondText ?? "").slice(0, 40))} / 复制 ${JSON.stringify((got ?? "").slice(0, 40))}）`
        );
      await firstToggle.click();
      await page.waitForTimeout(200);
      const afterCollapse = await expandedCount();
      if (afterCollapse !== 1)
        c.fail(
          "logs/重复事件",
          `收起一条后展开态有 ${afterCollapse} 条（收起没有按同一身份落回）`
        );
      pageError("logs/重复事件", errors);
      checks.push("duplicate-events");
    } catch (e) {
      broken("logs/重复事件", e);
    } finally {
      await ctx.close();
    }
  }

  // ------------------------------------------------- 5 clipboard refusal is reported, not faked
  {
    const { page, errors, ctx } = await c.newPage({ override, settle: 1200 });
    await c.gotoLane(page, 4);
    try {
      await waitOverview(page);
      await installCopyRefusal(page);
      const button = logRows(page)
        .first()
        .locator("[data-wb-log-overview-copy]");
      await button.click();
      await page.waitForTimeout(300);
      const label = await button.textContent();
      if (!label.includes("复制失败"))
        c.fail(
          "logs/剪贴板拒绝",
          `剪贴板拒绝后按钮没有如实反馈：${JSON.stringify((label ?? "").slice(0, 40))}`
        );
      pageError("logs/剪贴板拒绝", errors);
      checks.push("clipboard-refusal");
    } catch (e) {
      broken("logs/剪贴板拒绝", e);
    } finally {
      await ctx.close();
    }
  }

  // ------------------------------------------------- 6 kind colours in both themes and on a narrow viewport
  for (const [tag, options] of [
    ["light", { width: 1600, height: 900, theme: "light" }],
    ["dark", { width: 1600, height: 900, theme: "dark" }],
    ["narrow", { width: 880, height: 800, theme: "light" }],
  ]) {
    const { page, errors, ctx } = await c.newPage({
      ...options,
      override,
      settle: 1200,
    });
    await c.gotoLane(page, 4);
    try {
      await waitOverview(page);
      const probe = await page.$$eval(
        ".wb-log-overview-table tbody tr",
        (trs) => {
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
            cells: trs.map((tr) => {
              const cell = tr.querySelector("td");
              const style = getComputedStyle(cell);
              const label = tr.querySelector(".wb-log-kind");
              return {
                kind:
                  [...tr.classList].find((name) =>
                    name.startsWith("wb-log-kind-")
                  ) ?? "",
                bar: `${style.borderLeftWidth} ${style.borderLeftStyle}`,
                barColor: style.borderLeftColor,
                labelColor: label ? getComputedStyle(label).color : "",
              };
            }),
            errorToken: token,
          };
        }
      );
      const kinds = [...new Set(probe.cells.map((cell) => cell.kind))];
      if (!kinds.length) throw new Error("总览表没有行");
      const missing = probe.cells.filter(
        (cell) =>
          !cell.bar.startsWith("3px") || cell.barColor === "rgba(0, 0, 0, 0)"
      );
      if (missing.length)
        c.fail(
          "logs/分类色条",
          `[${tag}] ${missing.length} / ${probe.cells.length} 行没有行级分类色条`
        );
      const colours = new Set(probe.cells.map((cell) => cell.barColor));
      if (colours.size !== kinds.length)
        c.fail(
          "logs/分类色条",
          `[${tag}] ${kinds.length} 个分类只画出 ${colours.size} 种色条色：${[...colours].join(" / ")}`
        );
      const labelColours = new Set(probe.cells.map((cell) => cell.labelColor));
      if (labelColours.size !== kinds.length)
        c.fail(
          "logs/分类色条",
          `[${tag}] 分类标签颜色没有按分类区分：${[...labelColours].join(" / ")}`
        );
      // ERROR 行的原文红：展开 ERROR 行（它带摘要，折叠时不渲染 pre）。
      const errorToggle = page
        .locator(
          ".wb-log-overview-table tbody tr.wb-log-kind-error [data-wb-log-raw-toggle]"
        )
        .first();
      if (!(await errorToggle.count()))
        throw new Error("总览表没有 ERROR 行，无法验证错误红");
      await errorToggle.click();
      await page.waitForTimeout(200);
      const errorMsgColor = await page
        .locator(
          ".wb-log-overview-table tbody tr.wb-log-kind-error pre.wb-log-msg"
        )
        .first()
        .evaluate((el) => getComputedStyle(el).color);
      if (errorMsgColor !== probe.errorToken)
        c.fail(
          "logs/错误原文红",
          `[${tag}] ERROR 原文色 ${errorMsgColor} 未接 --wb-status-err（${probe.errorToken}）`
        );
      pageError("logs/分类色条", errors);
    } catch (e) {
      broken(`logs/分类色条[${tag}]`, e);
    } finally {
      await ctx.close();
    }
  }
  checks.push("kind-colour-light", "kind-colour-dark", "kind-colour-narrow");

  // ------------------------------------------------- 7 detail status sentence counts what the table lists
  {
    const { page, errors, ctx } = await c.newPage({ override, settle: 1200 });
    await c.gotoLane(page, 4);
    try {
      await waitOverview(page);
      await page.locator('[data-wb-log-source="chat2api"]').click();
      await waitDetail(page);
      const all = await detailRows(page).count();
      await page.click('.wb-log-detail [data-wb-log-overview-filter="error"]');
      await page.waitForTimeout(400);
      const listed = await detailRows(page).count();
      if (!listed || listed >= all)
        throw new Error(`夹具没有筛掉任何行（列出 ${listed} / ${all}）`);
      const state = await page
        .locator(".wb-log-detail .wb-log-state")
        .innerText();
      if (!state.includes(String(listed)) || !state.includes(String(all)))
        c.fail(
          "logs/详情计数",
          `详情状态句没有写筛选后 / 全部计数：${JSON.stringify(state.slice(0, 90))}`
        );
      pageError("logs/详情计数", errors);
      checks.push("detail-filtered-count");
    } catch (e) {
      broken("logs/详情计数", e);
    } finally {
      await ctx.close();
    }
  }

  return { ok: true, checks };
}
