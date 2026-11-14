// Console gate - the local UM console (frontend handoff r3 F1–F7, r4).
//
// Runs on the local candidate (`run.mjs --role local`). The real mode is
// checked as it ships before the B6 wiring: honestly unwired, no reads, every
// action closed. Everything else uses the candidate-only offline preview
// (`?um-preview=<scenario>`), whose synthetic driver answers the console's
// events in the page: a pass here proves the views, the interaction and the
// honesty wording — never a real UM, Magpie or production read. r4 adds the
// trend's 折线图 toggle, the compact 最近测试, the shell's seamless toolbar
// cover and (um-state.mjs) the console's own state handling, R4–R6.
//
// Text checks avoid strings the gate's key mask rewrites (24+ characters):
// the UM address and the long synthetic model ID are matched by attribute.
import { runUmState } from "./um-state.mjs";

const PAGES = [
  "overview",
  "requests/records",
  "requests/logs",
  "requests/maintenance",
  "models",
];
const VIEWPORTS = [
  { tag: "1280x720", width: 1280, height: 720 },
  { tag: "1366x768", width: 1366, height: 768 },
  { tag: "1920x1080", width: 1920, height: 1080 },
  { tag: "760x900", width: 760, height: 900 },
];

/** @param c createContext() result. Records failures on c and returns a report. */
export async function runUm(c) {
  const report = { checks: 0, shots: [] };
  const fail = (id, msg) => c.fail(`um/${id}`, msg);
  const check = (id, ok, msg) => {
    report.checks++;
    if (!ok) fail(id, msg);
  };

  async function open({
    scenario = null,
    hash = "overview",
    width = 1366,
    height = 768,
    theme = "light",
    prefs = {},
    reducedMotion = "no-preference",
  } = {}) {
    const { ctx, page, errors } = await c.newPage({
      width,
      height,
      theme,
      prefs,
      reducedMotion,
      waitForContent: false,
      settle: 50,
    });
    const gw = [];
    page.on("request", (r) => {
      try {
        if (new URL(r.url()).pathname.startsWith("/gw/"))
          gw.push(`${r.method()} ${new URL(r.url()).pathname}`);
      } catch {}
    });
    await page.goto(
      `${c.BASE}${scenario ? `?um-preview=${scenario}` : ""}#${hash}`,
      { waitUntil: "load" }
    );
    await page.waitForSelector(".um-content > .um-page", { timeout: 20000 });
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(700);
    return { ctx, page, errors, gw };
  }
  const text = (page, sel = ".um-content") =>
    page.evaluate((s) => document.querySelector(s)?.innerText ?? "", sel);
  const go = async (page, hash) => {
    await page.evaluate((h) => {
      location.hash = h;
    }, hash);
    await page.waitForTimeout(450);
  };
  const shot = async (page, name) => {
    const file = c.shot(`um-${name}`);
    await page.screenshot({ path: file });
    report.shots.push(file);
  };
  const until = async (page, fn, arg, ms = 15000) =>
    page.waitForFunction(fn, arg, { timeout: ms }).then(
      () => true,
      () => false
    );

  // ---------------------------------------------------------------- F1 / F7: structure, real mode unwired, isolation
  {
    const { ctx, page, errors, gw } = await open({ hash: "overview" });
    const nav = await page.$$eval(".shell-nav [data-um-page]", (bs) =>
      bs.map((b) => b.getAttribute("aria-label"))
    );
    check(
      "F1",
      JSON.stringify(nav) ===
        JSON.stringify(["总览", "请求与诊断", "模型与设置"]),
      `一级入口应为 总览／请求与诊断／模型与设置，实际 ${JSON.stringify(nav)}`
    );
    check(
      "F1",
      (await page.getAttribute(".shell-env", "data-role-state")) === "unwired",
      "未接线的真实模式，侧栏环境标记应为 unwired"
    );
    check(
      "F1",
      await page.isVisible('[data-um-mode][data-mode="unwired"]'),
      "真实模式应有「数据尚未接线」横幅"
    );
    for (const hash of PAGES) {
      await go(page, hash);
      const placeholders = await page.$$eval(
        '.um-placeholder[data-um-state="unwired"]',
        (ps) => ps.length
      );
      const numbers = await page.$$eval(
        ".um-kpi, .um-req-table, .um-model-table, .um-log-list",
        (e) => e.length
      );
      check(
        "F1",
        placeholders > 0 && numbers === 0,
        `${hash}：未接线时应只显示「未接线」占位（占位 ${placeholders}，数据块 ${numbers}）`
      );
      const open = await page.$$eval(
        "[data-um-act], [data-um-test], [data-um-refresh-catalog]",
        (bs) => bs.filter((b) => !b.disabled).length
      );
      check("F1", open === 0, `${hash}：未接线时 ${open} 个操作按钮仍可点`);
    }
    await go(page, "requests/records");
    const views = await page.$$eval("[data-um-view]", (bs) =>
      bs.map((b) => b.textContent.trim())
    );
    check(
      "F1",
      JSON.stringify(views) ===
        JSON.stringify(["请求记录", "运行日志", "维护操作"]),
      `诊断二级视图应为 请求记录／运行日志／维护操作，实际 ${JSON.stringify(views)}`
    );
    check(
      "F7",
      gw.length === 0,
      `未接线的 UM 控制台不应发出任何 /gw/ 请求：${gw.slice(0, 4).join("; ")}`
    );
    // The unwired banner's 「查看离线预览」 link is the one element the old
    // global `a` rule would have painted with the console accent; it must stay
    // a plain button, with no native outset border either.
    const unwiredLinks = await page.$$eval(
      '[data-um-mode][data-mode="unwired"] a, [data-um-mode][data-mode="unwired"] button',
      (els) =>
        els
          .filter((el) => {
            const s = getComputedStyle(el);
            return [
              s.borderTopStyle,
              s.borderRightStyle,
              s.borderBottomStyle,
              s.borderLeftStyle,
            ].includes("outset");
          })
          .map((el) => `${el.tagName}.${String(el.className).slice(0, 40)}`)
    );
    check(
      "F8",
      unwiredLinks.length === 0,
      `未接线横幅不应有原生 outset 边框的链接：${JSON.stringify(unwiredLinks)}`
    );
    check(
      "F1",
      errors.length === 0,
      `页面报错：${errors.slice(0, 2).join(" | ")}`
    );
    await ctx.close();
    // The workbench escape hatch is retired: `?console=workbench` must open the
    // UM console, and the old `.workbench` shell must never mount.
    const legacy = await c.newPage({ waitForContent: false, settle: 50 });
    await legacy.page.goto(`${c.BASE}?console=workbench`, {
      waitUntil: "load",
    });
    const legacyUm = await legacy.page
      .waitForSelector(".um-content > .um-page", { timeout: 20000 })
      .then(
        () => true,
        () => false
      );
    const legacyWorkbench = await legacy.page.locator(".workbench").count();
    check(
      "F1",
      legacyUm && legacyWorkbench === 0,
      "?console=workbench 应打开 UM 控制台，旧工作台不得再挂载"
    );
    await legacy.ctx.close();
  }

  // ---------------------------------------------------------------- F2: overview readings and honesty
  {
    const { ctx, page, errors, gw } = await open({ scenario: "normal" });
    const labels = await page.$$eval(".um-kpi-label", (ls) =>
      ls.map((l) => l.textContent.trim())
    );
    check(
      "F2",
      JSON.stringify(labels) ===
        JSON.stringify([
          "Token 合计",
          "缓存读取",
          "思考 Token",
          "已记录请求",
          "速度",
        ]),
      `今日五项应为 Token 合计／缓存读取／思考 Token／已记录请求／速度，实际 ${JSON.stringify(labels)}`
    );
    const all = await text(page, "body");
    check("F2", !/命中率/.test(all), "页面不应出现缓存命中率");
    check(
      "F2",
      /排行与图表同一时段/.test(await text(page, ".um-trend")),
      "趋势应写明排行与图表同一时段"
    );
    check(
      "F2",
      (await page.$$(".um-rank-row")).length > 0 &&
        (await page.$$(".um-chart-svg .um-bar")).length > 0,
      "趋势应画出堆叠柱并给出排行"
    );
    await page.click('[data-um-metric="speed"]');
    await page.waitForTimeout(300);
    const speedMarks = await page.evaluate(() => ({
      lines: document.querySelectorAll(".um-chart-svg .um-line").length,
      bars: document.querySelectorAll(".um-chart-svg .um-bar").length,
      dots: [...document.querySelectorAll(".um-chart-svg circle")].filter(
        (c) => !c.closest(".um-cursor-dots")
      ).length,
      state:
        document.querySelector("[data-um-chart]")?.dataset.umChartState ?? null,
    }));
    check(
      "F2",
      speedMarks.lines > 0 &&
        speedMarks.bars === 0 &&
        speedMarks.dots === 0 &&
        speedMarks.state === "drawn",
      `速度应画曲线、不堆叠、无逐点圆点：${JSON.stringify(speedMarks)}`
    );
    // The no-static-dot probe must catch a circle whatever its class: a future
    // dot under a new class name must not leave the check vacuously green.
    const dotProbe = await page.evaluate(() => {
      const svg = document.querySelector(".um-chart-svg");
      const c = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "circle"
      );
      c.setAttribute("class", "um-stray-dot");
      c.setAttribute("cx", "0");
      c.setAttribute("cy", "0");
      c.setAttribute("r", "1");
      svg.appendChild(c);
      const n = [...document.querySelectorAll(".um-chart-svg circle")].filter(
        (x) => !x.closest(".um-cursor-dots")
      ).length;
      c.remove();
      return n;
    });
    check(
      "F2",
      dotProbe === 1,
      `静态散点判据应能数到任意 class 的 circle（否则判据恒真）：${dotProbe}`
    );
    check(
      "F2",
      (await page.isDisabled('[data-um-toggle="line"]')) &&
        (await page.getAttribute('[data-um-toggle="line"]', "aria-pressed")) ===
          "true",
      "速度只画折线：折线图按钮应显示选中并关闭"
    );
    // r4: 折线图 switches Token / 请求 between stacked bars and lines over
    // the same window, models and figures; it no longer opens a date table.
    await page.click('[data-um-metric="tokens"]');
    await page.waitForTimeout(300);
    const shape = () =>
      page.evaluate(() => ({
        lines: document.querySelectorAll(".um-chart-svg .um-line").length,
        bars: document.querySelectorAll(".um-chart-svg .um-bar").length,
        dots: [...document.querySelectorAll(".um-chart-svg circle")].filter(
          (c) => !c.closest(".um-cursor-dots")
        ).length,
        pressed: document
          .querySelector('[data-um-toggle="line"]')
          ?.getAttribute("aria-pressed"),
        label: document
          .querySelector('[data-um-toggle="line"]')
          ?.textContent.trim(),
        table: document.querySelectorAll(".um-trend table").length,
        rank: document.querySelector(".um-trend-rank")?.textContent ?? "",
        scope: document.querySelector(".um-trend-scope")?.textContent ?? "",
      }));
    const bars = await shape();
    await page.click('[data-um-toggle="line"]');
    await page.waitForTimeout(300);
    const asLines = await shape();
    check(
      "F2",
      bars.label === "折线图" &&
        bars.pressed === "false" &&
        bars.bars > 0 &&
        bars.table === 0,
      `趋势右上角应是「折线图」切换，默认柱状、无日期表格：${JSON.stringify({ ...bars, rank: undefined, scope: undefined })}`
    );
    check(
      "F2",
      asLines.pressed === "true" &&
        asLines.lines > 0 &&
        asLines.bars === 0 &&
        asLines.dots === 0 &&
        asLines.table === 0,
      `点「折线图」后 Token 应改画成曲线、无柱、无逐点圆点：${JSON.stringify({ ...asLines, rank: undefined, scope: undefined })}`
    );
    check(
      "F2",
      asLines.rank === bars.rank &&
        asLines.scope.replace(/ · (≥ 下界为短虚线|≈ 估算见悬浮明细)/g, "") ===
          bars.scope.replace(/ · (≥ 下界为短虚线|≈ 估算见悬浮明细)/g, ""),
      "切换柱/线不应改变时段、排行与数值（线型质量说明可不同）"
    );
    await page.click('[data-um-metric="tokens"]');
    await page.click('[data-um-toggle="line"]');
    await page.waitForTimeout(300);
    const back = await shape();
    check(
      "F2",
      back.pressed === "false" && back.bars > 0 && back.lines === 0,
      `再点一次应回到柱状图：${JSON.stringify({ ...back, rank: undefined, scope: undefined })}`
    );
    const scope7 = await text(page, ".um-trend-scope");
    await page.click('[data-um-range="30d"]');
    await page.waitForTimeout(400);
    check(
      "F2",
      (await page.getAttribute('[data-um-range="30d"]', "aria-pressed")) ===
        "true" && (await text(page, ".um-trend-scope")) !== scope7,
      "切到 30 天后图表与排行的时段应一起变化"
    );
    // Keyboard: the chart takes focus and steps day by day with the tooltip.
    await page.focus("[data-um-chart]");
    await page.keyboard.press("ArrowLeft");
    await page.waitForTimeout(150);
    check(
      "F6",
      (await page.isVisible(".um-tip")) &&
        /合计/.test(await text(page, ".um-tip")),
      "图表获得焦点后方向键应显示当天明细"
    );
    await page.keyboard.press("Escape");
    // r5b: no per-sample dots, a locator only where a drawn curve passes, and a
    // ranking row that links to its own series without asking the controller.
    await page.click('[data-um-metric="speed"]');
    await page.waitForTimeout(300);
    await page.locator("[data-um-chart]").scrollIntoViewIfNeeded();
    const chartBox = await page.locator("[data-um-chart]").boundingBox();
    let hover = null;
    for (const f of [0.5, 0.35, 0.65, 0.2, 0.8]) {
      await page.mouse.move(
        chartBox.x + 64 + (chartBox.width - 80) * f,
        chartBox.y + chartBox.height * 0.45
      );
      await page.waitForTimeout(150);
      const at = await page.evaluate(() => ({
        cursor:
          document.querySelector(".um-cursor")?.getAttribute("visibility") ??
          null,
        cursorDots: document.querySelectorAll(".um-cursor-dot").length,
        hl:
          document.querySelector(".um-col-hl")?.getAttribute("visibility") ??
          null,
        tip: document.querySelector(".um-tip")?.textContent ?? "",
      }));
      if (at.cursor === "visible" && at.cursorDots > 0) {
        hover = at;
        break;
      }
    }
    check(
      "F2",
      !!hover && hover.hl === "visible" && /token\/秒/.test(hover.tip),
      `悬停在实际曲线所在列时应出现附着曲线的定位标记与辅助线：${JSON.stringify(hover)}`
    );
    await page.locator(".um-rank-row").first().hover();
    await page.waitForTimeout(200);
    const linked = await page.evaluate(() => ({
      linked: document.querySelectorAll(".um-rank-row.is-linked").length,
      dimmed: document.querySelectorAll(".um-chart-svg .is-dim").length,
      series:
        document.querySelector(".um-rank-row.is-linked")?.dataset.umSeries ??
        null,
      scale: document.querySelector(".um-rank-scale")?.textContent ?? "",
      hidden:
        document.querySelector(".um-cursor")?.getAttribute("visibility") ??
        null,
      hl:
        document.querySelector(".um-col-hl")?.getAttribute("visibility") ??
        null,
    }));
    check(
      "F2",
      linked.linked === 1 && linked.series !== null && linked.dimmed > 0,
      `聚焦排行行时应突出对应曲线并减弱其他序列：${JSON.stringify(linked)}`
    );
    check(
      "F2",
      linked.hidden === "hidden" && linked.hl === "hidden",
      `指针离开图表到排行后，定位标记与日列高亮应收起：${JSON.stringify(linked)}`
    );
    check(
      "F2",
      /条长＝相对本时段最高，非合计占比/.test(linked.scale),
      `排行应写明条长语义：${JSON.stringify(linked.scale)}`
    );
    await page.focus("[data-um-rank]");
    const focused = await page.evaluate(
      () => document.activeElement?.getAttribute("data-um-rank") ?? null
    );
    await page.keyboard.press("ArrowDown");
    await page.waitForTimeout(150);
    const moved = await page.evaluate(() => ({
      rank: document.activeElement?.getAttribute("data-um-rank") ?? null,
      linked: document.querySelectorAll(".um-rank-row.is-linked").length,
    }));
    check(
      "F2",
      focused !== null &&
        moved.rank !== null &&
        moved.rank !== focused &&
        moved.linked === 1,
      `排行行应可用键盘逐行聚焦并联动曲线：${JSON.stringify({ focused, ...moved })}`
    );
    await page.keyboard.press("Escape");
    const cleared = await page.evaluate(() => ({
      dimmed: document.querySelectorAll(".um-chart-svg .is-dim").length,
      active: document.activeElement?.getAttribute("data-um-rank") ?? null,
    }));
    check(
      "F2",
      cleared.dimmed === 0 && cleared.active === null,
      `Esc 应收起排行高亮：${JSON.stringify(cleared)}`
    );
    check(
      "F2",
      gw.length === 0,
      `排行高亮不应触发后端请求：${gw.slice(0, 3).join("; ")}`
    );
    check(
      "F2",
      errors.length === 0 && gw.length === 0,
      `离线预览不应报错或发出 /gw/ 请求：${[...errors, ...gw].slice(0, 3).join(" | ")}`
    );
    await shot(page, "f2-overview-normal");
    await ctx.close();

    for (const [scenario, expect] of [
      ["zero", { kpi: /^0$/, why: "零用量应显示明确的 0" }],
      ["attention", { kpi: /未报告/, why: "需处理情景应显示未报告" }],
      ["down", { kpi: /./, why: "服务停止情景仍显示原始用量读面" }],
    ]) {
      const s = await open({ scenario });
      const kpis = await s.page.$$eval(".um-kpi-value", (vs) =>
        vs.map((v) => v.innerText.trim())
      );
      check(
        "F2",
        kpis.some((v) => expect.kpi.test(v)),
        `${expect.why}：五项 ${JSON.stringify(kpis)}`
      );
      if (scenario === "down") {
        check(
          "F2",
          (await s.page.$$(".um-usage .um-stale")).length > 0,
          "读取失败时今日用量应沿用旧数据并标明时间"
        );
        check(
          "F2",
          /启动 UM 服务/.test(await text(s.page, ".um-status-row")),
          "服务未运行时卡片旁应有启动入口"
        );
        check(
          "F2",
          (await s.page.$$(".um-block-reason")).length > 0,
          "关闭的处理入口应写出原因"
        );
      }
      if (scenario === "attention")
        check(
          "F2",
          /检查并按需登录/.test(await text(s.page, ".um-status-row")),
          "登录失效时卡片旁应有「检查并按需登录」"
        );
      if (scenario === "zero") {
        // The 零用量 window has no model rows at all: the honest state is
        // 「没有记录」, not an invented line and not a hidden 0 in the KPIs.
        const zero = await s.page.evaluate(() => ({
          state:
            document.querySelector("[data-um-chart]")?.dataset.umChartState ??
            null,
          note: document.querySelector(".um-chart")?.textContent ?? "",
          rank: document.querySelector(".um-trend-rank")?.textContent ?? "",
          kpi: [...document.querySelectorAll(".um-kpi-value")].map((v) =>
            v.innerText.trim()
          ),
        }));
        check(
          "F2",
          zero.state === "empty" &&
            /没有可画的Token数据/.test(zero.note) &&
            /没有记录到 UM 请求/.test(zero.rank) &&
            zero.kpi.some((v) => /^0$/.test(v)),
          `零用量应显示明确的 0，图与排行如实说没有记录而不是编造曲线：${JSON.stringify(zero)}`
        );
        await s.page.click('[data-um-metric="speed"]');
        await s.page.waitForTimeout(300);
        const noSpeed = await s.page.evaluate(() => ({
          state:
            document.querySelector("[data-um-chart]")?.dataset.umChartState ??
            null,
          text: document.querySelector(".um-chart")?.textContent ?? "",
        }));
        check(
          "F2",
          noSpeed.state === "empty" && /没有可画的速度数据/.test(noSpeed.text),
          `零用量下速度没有任何样本，应明确没有可画的速度数据：${JSON.stringify(noSpeed)}`
        );
      }
      if (scenario === "attention") {
        await s.page.click('[data-um-metric="speed"]');
        await s.page.waitForTimeout(300);
        const partial = await s.page.evaluate(() => ({
          lines: document.querySelectorAll(".um-chart-svg .um-line").length,
          rows: document.querySelectorAll(".um-rank-row").length,
          dots: [...document.querySelectorAll(".um-chart-svg circle")].filter(
            (c) => !c.closest(".um-cursor-dots")
          ).length,
          notes: [...document.querySelectorAll(".um-rank-note")].map((n) =>
            n.textContent.trim()
          ),
        }));
        check(
          "F2",
          partial.lines > 0 &&
            partial.lines <= partial.rows &&
            partial.dots === 0 &&
            partial.notes.some((n) => /未连线/.test(n)),
          `部分模型没有连续有效数据时不应连线，排行要说明「未连线」：${JSON.stringify(partial)}`
        );
        await s.page.click('[data-um-metric="requests"]');
        await s.page.waitForTimeout(300);
        const barScope = await text(s.page, ".um-trend-scope");
        check(
          "F2",
          !/下界为短虚线/.test(barScope),
          `柱状图没有虚线，scope 不应声称「下界为短虚线」：${barScope}`
        );
        await s.page.click('[data-um-toggle="line"]');
        await s.page.waitForTimeout(300);
        const lineScope = await text(s.page, ".um-trend-scope");
        const lower = await s.page.$$eval(
          '.um-chart-svg .um-line[data-quality="lower"]',
          (ls) => ls.length
        );
        check(
          "F2",
          lower > 0 && /下界为短虚线/.test(lineScope),
          `下界请求画成短虚线时，scope 应说明该线型：${lower} / ${lineScope}`
        );
      }
      await shot(s.page, `f2-overview-${scenario}`);
      await s.ctx.close();
    }
  }

  // ---------------------------------------------------------------- F3: diagnostics
  {
    const { ctx, page, errors } = await open({
      scenario: "attention",
      hash: "requests/records",
    });
    check(
      "F3",
      (await page.$$(".um-content .um-chart")).length === 0,
      "请求与诊断页不应有趋势图"
    );
    const heads = await page.$$eval(".um-req-table thead th", (ths) =>
      ths.map((t) => t.textContent.trim())
    );
    check(
      "F3",
      JSON.stringify(heads) ===
        JSON.stringify([
          "时间",
          "模型",
          "结果",
          "输入 / 输出 Token",
          "耗时",
          "速度",
        ]),
      `请求表列应为 时间／模型／结果／输入/输出 Token／耗时／速度，实际 ${JSON.stringify(heads)}`
    );
    await page.click('[data-um-rwin="7d"]');
    await page.waitForTimeout(500);
    const words = await page.$$eval(".um-req-table tbody .status", (s) => [
      ...new Set(s.map((x) => x.textContent.trim())),
    ]);
    check(
      "F3",
      words.includes("截断") && words.includes("成功"),
      `7 天内应能看到截断与成功：${JSON.stringify(words)}`
    );
    const before = (await page.$$(".um-req-table tbody tr.um-req-row")).length;
    if (await page.$("[data-um-earlier]")) {
      await page.click("[data-um-earlier]");
      await page.waitForTimeout(700);
      check(
        "F3",
        (await page.$$(".um-req-table tbody tr.um-req-row")).length > before,
        "加载更早应追加记录"
      );
    } else check("F3", false, "7 天窗口应提供「加载更早的记录」");
    // row-3 has no request_id in this scenario: the view says the link is missing.
    await page.click('[data-um-row="row-3"]');
    await page.waitForTimeout(500);
    const detail = await text(page, ".um-req-detail");
    check(
      "F3",
      /缺少 request_id/.test(detail) && /不按时间就近猜测/.test(detail),
      "缺 request_id 的请求应说明关联不足、不猜日志"
    );
    await page.click('[data-um-row="row-3"]');
    const truncated = await page.$$eval(
      ".um-req-row",
      (rows) =>
        rows
          .find(
            (r) => r.querySelector(".status")?.textContent.trim() === "截断"
          )
          ?.querySelector("[data-um-row]")
          ?.getAttribute("data-um-row") ?? null
    );
    if (truncated) {
      await page.click(`[data-um-row="${truncated}"]`);
      await page.waitForTimeout(700);
      const t = await text(page, ".um-req-detail");
      check(
        "F3",
        /HTTP 200/.test(t) && /截断/.test(t),
        "截断请求的详情应同时写出 HTTP 200 与截断，按失败计"
      );
    }
    await page.selectOption("[data-um-rresult]", "client_disconnected");
    await page.waitForTimeout(500);
    const cd = await page.$$eval(".um-req-table tbody .status", (s) =>
      s.map((x) => x.textContent.trim())
    );
    check(
      "F3",
      cd.every((w) => w === "客户端断开"),
      `按结果筛选「客户端断开」后不应混入其它结果：${JSON.stringify(cd)}`
    );
    await go(page, "requests/logs");
    check(
      "F3",
      /读取范围/.test(await text(page, ".um-logs")),
      "日志应写明读取范围"
    );
    await page.click('[data-um-level="error"]');
    await page.waitForTimeout(300);
    const levels = await page.$$eval(".um-log .pill", (ps) => [
      ...new Set(ps.map((p) => p.textContent.trim())),
    ]);
    check(
      "F3",
      levels.length > 0 && levels.every((l) => l === "错误"),
      `级别筛选「错误」后只应剩错误：${JSON.stringify(levels)}`
    );
    await go(page, "requests/maintenance");
    const maint = await text(page, ".um-maint");
    check(
      "F3",
      /不计入请求数/.test(maint) && !!(await page.$("[data-um-mearlier]")),
      "维护记录应与请求计数分开并可加载更早"
    );
    check(
      "F3",
      errors.length === 0,
      `页面报错：${errors.slice(0, 2).join(" | ")}`
    );
    await shot(page, "f3-maintenance-attention");
    await ctx.close();

    const down = await open({ scenario: "down", hash: "requests/records" });
    check(
      "F3",
      (await down.page.$$(".um-req-table tbody tr")).length > 0,
      "服务停止时账本仍可读，请求记录应照常列出"
    );
    await go(down.page, "requests/logs");
    check(
      "F3",
      /读取失败/.test(await text(down.page, ".um-logs")) &&
        (await down.page.$$(".um-log")).length === 0,
      "日志读取失败应如实显示失败，不是空列表"
    );
    await go(down.page, "requests/maintenance");
    check(
      "F3",
      /无权读取/.test(await text(down.page, ".um-maint")),
      "维护记录被拒应显示「无权读取」"
    );
    await down.ctx.close();
    const zero = await open({ scenario: "zero", hash: "requests/records" });
    check(
      "F3",
      /没有 UM 请求记录/.test(await text(zero.page, ".um-req")),
      "合法空列表应说明这段时间没有请求"
    );
    await zero.ctx.close();
  }

  // ---------------------------------------------------------------- F4 / F5: catalog and tests
  {
    const { ctx, page, errors, gw } = await open({
      scenario: "normal",
      hash: "models",
    });
    check(
      "F4",
      !!(await page.$(".um-access [data-um-copy]")) &&
        !!(await page.$(".um-access .um-url")),
      "接入卡应常驻并带复制"
    );
    const inputs = await page.$$eval(".um-content input", (is) =>
      is.map((i) => i.type)
    );
    check(
      "F4",
      inputs.every((t) => t === "checkbox"),
      `模型页不应有搜索框或 Key 输入：${JSON.stringify(inputs)}`
    );
    const heads = await page.$$eval(".um-model-table thead th", (ths) =>
      ths.map((t) => t.textContent.trim())
    );
    check(
      "F4",
      JSON.stringify(heads) ===
        JSON.stringify([
          "选择",
          "模型名称 / ID",
          "上下文信息",
          "思考档位",
          "最近测试",
        ]),
      `模型表列应为 选择／模型名称/ID／上下文信息／思考档位／最近测试（无价格），实际 ${JSON.stringify(heads)}`
    );
    check(
      "F4",
      /非 UM 声明/.test(await text(page, ".um-model-table")),
      "兼容默认值应标明不是 UM 声明"
    );
    await page.click('[data-um-model="claude-sonnet-4-6"]');
    await page.waitForTimeout(300);
    const facts = await text(page, ".um-model-table .um-req-detail");
    check(
      "F4",
      /按字符计/.test(facts) && /已验证下界，不是最大上下文/.test(facts),
      "行内详情应把字符步进与 Token 下界分开写清"
    );

    // F5: one shared selection bar; a click runs at once, no dialog, no double submit.
    check(
      "F5",
      await page.isDisabled('[data-um-test="context"]'),
      "未选模型时上下文探测应关闭"
    );
    await page.check('[data-um-pick="claude-sonnet-4-6"]');
    await page.check('[data-um-pick="deepseek-v3.2"]');
    check(
      "F5",
      /已选 2 个/.test(await text(page, ".um-selbar")),
      "选择栏应显示已选 2 个"
    );
    await page.click('[data-um-test="context"]');
    const after = await page.evaluate(() => ({
      dialog: !!document.querySelector("dialog[open]"),
      disabled:
        document.querySelector('[data-um-test="context"]')?.disabled ?? null,
      run: !!document.querySelector(".um-run"),
    }));
    check(
      "F5",
      !after.dialog && after.disabled === true && after.run,
      `点击上下文探测应直接执行、无确认框且按钮立即关闭：${JSON.stringify(after)}`
    );
    const done = await until(
      page,
      () =>
        /最近一次上下文探测 · 已结束/.test(
          document.querySelector(".um-run")?.innerText ?? ""
        ),
      null,
      15000
    );
    check("F5", done, "上下文探测应在合成驱动下结束");
    const states = await page.$$eval(".um-run-item", (items) =>
      items.map((i) => i.getAttribute("data-state"))
    );
    check(
      "F5",
      JSON.stringify(states) === JSON.stringify(["done", "partial"]),
      `逐模型结果应为已完成＋部分完成（上游限流不构成上限）：${JSON.stringify(states)}`
    );
    await page.click('[data-um-rundetail="claude-sonnet-4-6"]');
    await page.waitForTimeout(200);
    check(
      "F5",
      /字符步进/.test(await text(page, ".um-run-detail")),
      "上下文详情应写明字符步进口径"
    );

    // Context probe on every model, then stop the rest.
    await page.click("[data-um-pick-all]");
    await page.click('[data-um-test="context"]');
    await page.waitForTimeout(1400);
    check(
      "F5",
      /请求预算 已用/.test(await text(page, ".um-run")),
      "上下文探测应显示整批请求预算"
    );
    await page.click("[data-um-stop]");
    await page.waitForTimeout(150);
    check(
      "F5",
      /已请求停止/.test(await text(page, ".um-run")),
      "停止后应显示「已请求停止」"
    );
    const stopped = await until(
      page,
      () =>
        /已结束/.test(document.querySelector(".um-run h2")?.textContent ?? ""),
      null,
      15000
    );
    const notRun = await page.$$eval(
      '.um-run-item[data-state="not_run"]',
      (i) => i.length
    );
    check(
      "F5",
      stopped && notRun > 0,
      `停止后后续模型应标为未执行（结束 ${stopped}，未执行 ${notRun}）`
    );
    await page.click('[data-um-fold="history"] > summary');
    await page.waitForTimeout(400);
    check(
      "F5",
      (await page.$$('[data-um-fold="history"] tbody tr')).length > 0 &&
        /已归档/.test(await text(page, '[data-um-fold="history"]')),
      "历史记录应可展开，重测把旧结果标为已归档"
    );
    check(
      "F5",
      errors.length === 0 && gw.length === 0,
      `离线测试驱动不应报错或发出 /gw/ 请求：${[...errors, ...gw].slice(0, 3).join(" | ")}`
    );
    await shot(page, "f5-models-after-tests");
    await ctx.close();

    const att = await open({ scenario: "attention", hash: "models" });
    await att.page.click("[data-um-refresh-catalog]");
    const failed = await until(
      att.page,
      () =>
        /刷新失败/.test(document.querySelector(".um-catalog")?.innerText ?? ""),
      null,
      6000
    );
    check(
      "F4",
      failed && (await att.page.$$(".um-model-table tbody tr")).length > 0,
      "目录刷新失败应保留旧列表并标明失败时间"
    );
    await att.ctx.close();

    // r4: 最近测试 is a compact summary at a readable width — never a column of
    // single characters — with figures, units and times kept whole, and the
    // full wording one click away. 「需处理」 has a long upstream error, a model
    // with a 64-character ID and models never tested.
    for (const [w, h, theme, rail] of [
      [1280, 720, "light", "expanded"],
      [1366, 768, "dark", "collapsed"],
      [760, 900, "dark", "collapsed"],
    ]) {
      const s = await open({
        scenario: "attention",
        hash: "models",
        width: w,
        height: h,
        theme,
        prefs: { rail },
      });
      const tag = `${theme} ${w}x${h} ${rail}`;
      const cells = await s.page.evaluate(() => {
        const lineH = (el) => parseFloat(getComputedStyle(el).lineHeight) || 19;
        const scroll = document.querySelector(".um-catalog .um-table-scroll");
        return {
          stacked:
            getComputedStyle(document.querySelector(".um-model-table"))
              .display === "block",
          scrolls: scroll ? scroll.scrollWidth - scroll.clientWidth : null,
          widths: [...document.querySelectorAll(".um-last-cell")].map((c) =>
            Math.round(c.getBoundingClientRect().width)
          ),
          heads: [...document.querySelectorAll(".um-last-head")].map(
            (p) =>
              Math.round((p.getBoundingClientRect().height / lineH(p)) * 10) /
              10
          ),
          broken: [...document.querySelectorAll(".um-last .um-nb")]
            .filter((n) => n.getClientRects().length > 1)
            .map((n) => n.textContent),
          untested: [...document.querySelectorAll(".um-col-last")].filter(
            (td) => td.tagName === "TD" && /未测/.test(td.textContent)
          ).length,
          more: document.querySelectorAll("[data-um-model-more]").length,
        };
      });
      check(
        "F4",
        cells.widths.length > 0 && cells.widths.every((x) => x >= 200),
        `${tag}：最近测试列应有可读宽度（≥200px），实际 ${JSON.stringify(cells.widths)}`
      );
      check(
        "F4",
        cells.heads.every((n) => n <= 2.2),
        `${tag}：结论最多两行，不应逐字竖排：${JSON.stringify(cells.heads)}`
      );
      check(
        "F4",
        cells.broken.length === 0,
        `${tag}：数字、单位与时间不应被拆行：${JSON.stringify(cells.broken)}`
      );
      check(
        "F4",
        cells.untested > 0 && cells.more === cells.widths.length,
        `${tag}：未测的模型写「未测」，有结果的每格都有详情入口（详情 ${cells.more}，结果格 ${cells.widths.length}）`
      );
      check(
        "F6",
        w > 800 ? !cells.stacked && cells.scrolls === 0 : cells.stacked,
        `${tag}：${w > 800 ? "宽窗口应保持表格且不横向滚动" : "窄窗口应把每个模型改为上下排列"}（stacked=${cells.stacked}，scroll=${cells.scrolls}）`
      );
      // The long error is clamped in the cell and whole in the row's 详情.
      const row = await s.page.$('[data-um-model-more="deepseek-v3.2"]');
      if (row) {
        await row.click();
        await s.page.waitForTimeout(250);
        const detail = await s.page.evaluate(() => ({
          expanded: document
            .querySelector('[data-um-model-more="deepseek-v3.2"]')
            ?.getAttribute("aria-expanded"),
          focus: document.activeElement?.getAttribute("data-um-model-more"),
          full: /通过 16万 字符[\s\S]*上游限流（HTTP 429，不构成上限）/.test(
            document.querySelector(".um-model-table .um-req-detail")
              ?.textContent ?? ""
          ),
        }));
        check(
          "F4",
          detail.expanded === "true" &&
            detail.full &&
            detail.focus === "deepseek-v3.2",
          `${tag}：详情应展开该行并给出完整的长错误，焦点留在详情按钮：${JSON.stringify(detail)}`
        );
      } else check("F4", false, `${tag}：上游长错误的模型应有详情入口`);
      await shot(s.page, `r4-models-${theme}-${w}x${h}-${rail}`);
      check(
        "F4",
        s.errors.length === 0,
        `${tag} 页面报错：${s.errors.slice(0, 2).join(" | ")}`
      );
      await s.ctx.close();
    }
  }

  // ---------------------------------------------------------------- r4: the toolbar cover draws no edge of its own
  // The sticky bar hides what scrolls under it by repainting the shell's own
  // backdrop. Walking down columns just right of the page's left edge (inside
  // its padding, no text) through the bar and its fade, no one-pixel step may
  // exceed the gradient's own: the square solid cover of r3 jumped 18–36 at
  // its top edge. 1366 collapsed is the user's report (the rail panel ends
  // where the page starts); 1920 leaves a gap that also shows a left edge.
  for (const theme of c.THEMES) {
    for (const [width, height, rail] of [
      [1920, 1080, "expanded"],
      [1366, 768, "collapsed"],
    ]) {
      const { ctx, page } = await open({
        scenario: "normal",
        width,
        height,
        theme,
        prefs: { rail },
      });
      const at = await page.evaluate(() => {
        const p = document.querySelector(".shell-page").getBoundingClientRect(),
          t = document.querySelector(".shell-toolbar").getBoundingClientRect();
        return { left: Math.round(p.left), bottom: Math.round(t.bottom) };
      });
      const clip = { x: at.left - 12, y: 0, width: 40, height: at.bottom + 40 };
      const png = (await page.screenshot({ clip })).toString("base64");
      const seam = await page.evaluate(
        async ({ png }) => {
          const img = new Image();
          img.src = `data:image/png;base64,${png}`;
          await img.decode();
          const canvas = document.createElement("canvas");
          canvas.width = img.width;
          canvas.height = img.height;
          const g = canvas.getContext("2d");
          g.drawImage(img, 0, 0);
          const px = (x, y) => g.getImageData(x, y, 1, 1).data;
          const step = (a, b) =>
            Math.max(
              Math.abs(a[0] - b[0]),
              Math.abs(a[1] - b[1]),
              Math.abs(a[2] - b[2])
            );
          let down = { step: 0, x: 0, y: 0 };
          for (const x of [16, 24, 32])
            for (let y = 1; y < img.height; y++) {
              const s = step(px(x, y - 1), px(x, y));
              if (s > down.step) down = { step: s, x, y };
            }
          return { down, left: step(px(4, 30), px(16, 30)) };
        },
        { png }
      );
      const gap = await page.evaluate(
        () =>
          document.querySelector(".shell-page").getBoundingClientRect().left -
          document.querySelector(".shell-side-panel").getBoundingClientRect()
            .right
      );
      const leftOk = gap < 16 || seam.left <= 3;
      check(
        "F6",
        seam.down.step <= 3 && leftOk,
        `${theme} ${width}x${height} ${rail}：工具栏遮挡层不应形成色差拼缝（向下逐像素最大通道差 ${seam.down.step}，位于页面左缘右侧 ${seam.down.x - 12}px、y=${seam.down.y}${gap < 16 ? "" : `；左缘内外差 ${seam.left}`}）`
      );
      if (theme === "dark" && width === 1920)
        await shot(page, "r4-shell-top-left-dark-1920");
      if (theme === "light" && width === 1366)
        await shot(page, "r4-shell-top-left-light-1366-collapsed");
      await ctx.close();
    }
  }

  // ---------------------------------------------------------------- r5: the toolbar cover is opaque through the bar
  // The r4 mask began fading inside the bar: its last 8px let the table header
  // and rows show through (the user's 2026-11-01 screenshot). r5 ends the cover at
  // the bar's bottom edge: no mask, no extension, content below stays fully opaque.
  // Sample the card's empty left padding (page padding 28px + 2px, inside the
  // card's 18px padding) at scrollTop 0 and with the first card scrolled under the
  // bar: the bar's own bottom 8px must not change.
  for (const theme of c.THEMES) {
    const { ctx, page, errors } = await open({
      scenario: "normal",
      hash: "models",
      width: 1366,
      height: 768,
      theme,
    });
    const at = await page.evaluate(() => {
      const p = document.querySelector(".shell-page").getBoundingClientRect(),
        t = document.querySelector(".shell-toolbar").getBoundingClientRect(),
        c = document.querySelector(".um-content").getBoundingClientRect();
      return {
        x: Math.round(p.left + 37),
        contentTop: c.top,
        toolbarBottom: t.bottom,
        toolbarTop: t.top,
      };
    });
    const cover = await page.evaluate(() => {
      const t = document.querySelector(".shell-toolbar");
      const s = getComputedStyle(t, "::before");
      return { mask: s.maskImage || s.webkitMaskImage, bottom: s.bottom };
    });
    check(
      "F8",
      (!cover.mask || cover.mask === "none") && cover.bottom === "0px",
      `${theme} 工具栏遮挡层不应带渐隐或伸出条底（mask=${cover.mask}，bottom=${cover.bottom}）`
    );
    const clip = {
      x: at.x,
      y: 0,
      width: 6,
      height: Math.ceil(at.toolbarBottom + 34),
    };
    const top = (await page.screenshot({ clip })).toString("base64");
    const probe = Math.min(
      await page.evaluate(() => {
        const m = document.querySelector(".shell-main");
        return m.scrollHeight - m.clientHeight;
      }),
      Math.max(0, Math.round(at.contentTop - (at.toolbarBottom - 20)))
    );
    await page.evaluate((y) => {
      document.querySelector(".shell-main").scrollTop = y;
    }, probe);
    await page.waitForTimeout(300);
    const down = (await page.screenshot({ clip })).toString("base64");
    const bleed = await page.evaluate(
      async ({ top, down, barBottom, probe }) => {
        const load = async (data) => {
          const img = new Image();
          img.src = `data:image/png;base64,${data}`;
          await img.decode();
          const c = document.createElement("canvas");
          c.width = img.width;
          c.height = img.height;
          const g = c.getContext("2d");
          g.drawImage(img, 0, 0);
          return g;
        };
        const a = await load(top),
          b = await load(down);
        const px = (g, y) => g.getImageData(3, y, 1, 1).data;
        const step = (p, q) =>
          Math.max(
            Math.abs(p[0] - q[0]),
            Math.abs(p[1] - q[1]),
            Math.abs(p[2] - q[2])
          );
        let max = { step: 0, y: 0 };
        for (
          let y = Math.round(barBottom - 8);
          y <= Math.round(barBottom - 2);
          y++
        ) {
          const s = step(px(a, y), px(b, y));
          if (s > max.step) max = { step: s, y };
        }
        return { ...max, probe };
      },
      {
        top,
        down,
        barBottom: at.toolbarBottom - Math.min(probe, at.toolbarTop),
        probe,
      }
    );
    check(
      "F8",
      bleed.step <= 3,
      `${theme} 1366x768：工具栏遮挡层最后 8px 不应透出下方内容（最大通道差 ${bleed.step}，y=${bleed.y}，滚动 ${bleed.probe}px）`
    );
    check(
      "F8",
      errors.length === 0,
      `${theme} 模型页页面报错：${errors.slice(0, 2).join(" | ")}`
    );
    await ctx.close();
  }

  // The model selection bar is the only sticky bottom element. Mid-catalog it
  // must meet the window bottom with square corners (the card clips the rounded
  // corners at its own end); a bottom radius here reads as a cut-off card.
  {
    const { ctx, page } = await open({
      scenario: "normal",
      hash: "models",
      width: 1366,
      height: 768,
    });
    await page.evaluate(() => {
      document.querySelector(".shell-main").scrollTop = 100;
    });
    await page.waitForTimeout(250);
    const bar = await page.evaluate(() => {
      const m = document.querySelector(".shell-main"),
        s = document.querySelector(".um-selbar");
      if (!s) return null;
      const b = s.getBoundingClientRect(),
        cs = getComputedStyle(s);
      return {
        position: cs.position,
        radius: [cs.borderBottomLeftRadius, cs.borderBottomRightRadius],
        bottom: +b.bottom.toFixed(1),
        viewport: innerHeight,
        scroll: m.scrollTop,
      };
    });
    check(
      "F8",
      bar &&
        bar.position === "sticky" &&
        Math.abs(bar.bottom - bar.viewport) <= 1 &&
        bar.radius.every((r) => r === "0px"),
      `模型选择栏吸底时应贴住窗口底且两个底角都是直角：${JSON.stringify(bar)}`
    );
    await ctx.close();
  }

  // The r5 review's other points: buttons must not fall back to a native
  // outset border; the preview banner's links keep the banner ink in both
  // themes; and the y-axis label (12px in CSS, measured at 12px) stays inside the
  // SVG's left edge.
  for (const theme of c.THEMES) {
    const { ctx, page } = await open({
      scenario: "normal",
      hash: "overview",
      width: 1366,
      height: 768,
      theme,
    });
    const v = await page.evaluate(() => {
      const mode = document.querySelector(".um-mode");
      const chips = mode ? [...mode.querySelectorAll(".chip")] : [];
      const chip =
        chips.find((el) => el.getAttribute("aria-current") !== "page") ??
        chips[0];
      const link = mode?.querySelector(".um-link");
      const axis = [...document.querySelectorAll(".um-chart-svg .um-axis")].map(
        (t) => t.getBBox()
      );
      const outset = [...document.querySelectorAll("button, input, select, a")]
        .filter((el) => {
          const s = getComputedStyle(el);
          return [
            s.borderTopStyle,
            s.borderRightStyle,
            s.borderBottomStyle,
            s.borderLeftStyle,
          ].includes("outset");
        })
        .map((el) => `${el.tagName}.${String(el.className).slice(0, 40)}`);
      return {
        banner: mode ? getComputedStyle(mode).color : null,
        chip: chip ? getComputedStyle(chip).color : null,
        link: link ? getComputedStyle(link).color : null,
        axisLeft: axis.length ? Math.min(...axis.map((b) => b.x)) : null,
        outset,
      };
    });
    check(
      "F8",
      !v.banner || (v.chip === v.banner && v.link === v.banner),
      `${theme} 预览横幅的情景 chips 与退出预览应使用横幅墨色：${JSON.stringify(v)}`
    );
    check(
      "F8",
      v.axisLeft === null || v.axisLeft >= -0.5,
      `${theme} 图表 y 轴标签不应越过 SVG 左缘：${JSON.stringify(v.axisLeft)}`
    );
    check(
      "F8",
      v.outset.length === 0,
      `${theme} 不应有原生 outset 边框的按钮/链接：${JSON.stringify(v.outset)}`
    );
    // The maintenance menu is drawn in the toolbar, outside the page content, so
    // it gets its own border read while it is open.
    await page.click("[data-um-menu]");
    const menuBorders = await page.$$eval(".um-menu [data-um-act]", (els) =>
      els.map((el) => {
        const s = getComputedStyle(el);
        return [
          s.borderTopStyle,
          s.borderRightStyle,
          s.borderBottomStyle,
          s.borderLeftStyle,
        ];
      })
    );
    check(
      "F8",
      menuBorders.length > 0 && menuBorders.every((b) => !b.includes("outset")),
      `${theme} 维护菜单项不应有原生 outset 边框：${JSON.stringify(menuBorders)}`
    );
    await page.keyboard.press("Escape");
    await ctx.close();
  }

  // ---------------------------------------------------------------- F6: themes, viewports, preferences, confirms
  for (const theme of c.THEMES) {
    for (const vp of c.QUICK ? [VIEWPORTS[1]] : VIEWPORTS) {
      const { ctx, page, errors } = await open({
        scenario: "attention",
        width: vp.width,
        height: vp.height,
        theme,
      });
      for (const hash of PAGES) {
        await go(page, hash);
        const o = await page.evaluate(() => {
          const m = document.querySelector(".shell-main");
          return {
            doc: document.documentElement.scrollWidth - innerWidth,
            main: m.scrollWidth - m.clientWidth,
          };
        });
        check(
          "F6",
          o.doc <= 0 && o.main <= 0,
          `${theme} ${vp.tag} ${hash} 横向溢出 ${JSON.stringify(o)}`
        );
      }
      check(
        "F6",
        errors.length === 0,
        `${theme} ${vp.tag} 页面报错：${errors.slice(0, 2).join(" | ")}`
      );
      if (vp.tag === "1366x768") {
        await go(page, "overview");
        await shot(page, `f6-overview-${theme}`);
      }
      await ctx.close();
    }
  }
  {
    const { ctx, page } = await open({
      scenario: "normal",
      prefs: { motion: "reduced", transparency: "reduced" },
    });
    const prefs = await page.evaluate(() => ({
      motion: document.documentElement.dataset.consoleMotion,
      transparency: document.documentElement.dataset.consoleTransparency,
      panel: getComputedStyle(document.querySelector(".shell-side-panel"))
        .backgroundColor,
    }));
    check(
      "F6",
      prefs.motion === "reduced" &&
        prefs.transparency === "reduced" &&
        !/rgba\([^)]*,\s*0?\.\d+\)/.test(prefs.panel),
      `减少动效／降低透明度应生效并让导航层不透明：${JSON.stringify(prefs)}`
    );
    // Maintenance menu by keyboard; a stop goes through the existing confirm.
    await page.focus("[data-um-menu]");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(150);
    check(
      "F6",
      (await page.isVisible(".um-menu")) &&
        (await page.evaluate(
          () => document.activeElement?.getAttribute("role") === "menuitem"
        )),
      "维护菜单应可用键盘打开并聚焦第一项"
    );
    await page.keyboard.press("Escape");
    check(
      "F6",
      await page.evaluate(() =>
        document.activeElement?.hasAttribute("data-um-menu")
      ),
      "Esc 关闭菜单后焦点应回到维护按钮"
    );
    await page.click("[data-um-menu]");
    await page.click('.um-menu [data-um-act="stop"]');
    await page.waitForTimeout(200);
    check(
      "F6",
      /停止 UM 服务/.test(await text(page, "dialog.shell-confirm[open]")),
      "停止应先弹出既有的确认与影响说明"
    );
    await page.click('dialog.shell-confirm button[value="cancel"]');
    await page.waitForTimeout(200);
    check(
      "F6",
      (await page.$$(".um-progress")).length === 0,
      "取消确认后不应发起停止"
    );
    await ctx.close();
  }
  {
    // A confirm whose action closes while it is open is withdrawn and never
    // sends: stop the service, open 重启 while the stop is still running; the
    // preview's stop lands after 1.8 s and closes 重启 (服务未运行).
    const { ctx, page } = await open({ scenario: "normal" });
    await page.click("[data-um-menu]");
    await page.click('.um-menu [data-um-act="stop"]');
    await page.click('dialog.shell-confirm button[value="confirm"]');
    await page.waitForTimeout(150);
    await page.click("[data-um-menu]");
    await page.click('.um-menu [data-um-act="restart"]');
    await page.waitForTimeout(150);
    const opened = /重启 UM 服务/.test(
      await text(page, "dialog.shell-confirm[open]")
    );
    await page.waitForTimeout(2_300);
    const after = await page.evaluate(() => ({
      open: !!document.querySelector("dialog.shell-confirm[open]"),
      restart: [...document.querySelectorAll(".um-progress")].some((p) =>
        /重启 UM 服务/.test(p.textContent ?? "")
      ),
    }));
    check(
      "F6",
      opened && !after.open && !after.restart,
      `重启确认打开时服务被停止，确认框应自动撤回且不发出重启：${JSON.stringify({ opened, ...after })}`
    );
    await ctx.close();
  }

  // ---------------------------------------------------------------- R4–R6: the console's own state handling
  await runUmState(c, check);

  c.rows.push({
    label: "um",
    checks: report.checks,
    shots: report.shots.length,
  });
  return report;
}
