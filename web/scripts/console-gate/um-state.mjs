// Console gate - the UM console's own state handling in a real browser
// (frontend handoff r4, return review R4–R6). The source view modules are
// bundled with esbuild and mounted on a blank page with synthetic state that
// the check changes directly, the way a data controller may: in place, failed,
// cancelled. No CSS, no candidate build, no network (every request of the
// context is answered locally with the blank page). A pass proves the
// console's bookkeeping, never a controller or a real UM read.
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const WEB = fileURLToPath(new URL("../..", import.meta.url));
const ORIGIN = "http://um-state.invalid/";

function bundle() {
  const require = createRequire(new URL("../../package.json", import.meta.url));
  const { buildSync } = require("esbuild");
  return buildSync({
    stdin: {
      contents: [
        `export { mountUmConsole } from "./src/local/um-ui/um-console.ts";`,
        `export { startPreview } from "./src/local/um-ui/preview.ts";`,
        `export { tokens } from "./src/local/um-ui/format.ts";`,
      ].join("\n"),
      resolveDir: WEB,
      sourcefile: "um-state-entry.ts",
      loader: "ts",
    },
    bundle: true,
    write: false,
    format: "iife",
    globalName: "UmState",
    platform: "browser",
    loader: { ".css": "empty" },
    define: {
      "import.meta.env.MODE": '"test"',
      __CONSOLE_ROLE__: '"local"',
      __CONSOLE_CANDIDATE__: "true",
    },
  }).outputFiles[0].text;
}

/** @param c createContext() result; @param check the um group's (id, ok, msg). */
export async function runUmState(c, check) {
  const ctx = await c.browser.newContext({
    viewport: { width: 1366, height: 768 },
    serviceWorkers: "block",
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
  });
  const outbound = [];
  await ctx.route("**/*", (route) => {
    if (!route.request().url().startsWith(ORIGIN))
      outbound.push(route.request().url());
    return route.fulfill({
      status: 200,
      contentType: "text/html",
      body: `<!doctype html><html><body><div id="stage"></div></body></html>`,
    });
  });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e.message).slice(0, 160)));
  await page.goto(ORIGIN);
  await page.addScriptTag({ content: bundle() });
  // The preview's 「正常」 data as a plain copy: its own driver is not attached,
  // the check is the controller here.
  await page.evaluate(() => {
    UmState.startPreview("normal", (s) => {
      window.umState ??= structuredClone(s);
    });
    window.umEvents = [];
    window.umFrame = () =>
      new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    window.umHandle = UmState.mountUmConsole(
      document.querySelector("#stage"),
      window.umState,
      {
        onEvent: (e) => window.umEvents.push(e),
        gate: {
          verdict: () => ({ state: "confirmed", reason: "" }),
          onChange: () => {},
        },
        previewHref: null,
      }
    );
  });
  await page.waitForSelector(".um-chart-svg");

  // ---------------------------------------------------------------- R4: in-place figures repaint the chart
  const r4 = await page.evaluate(async () => {
    await umFrame();
    const svg = () => document.querySelector(".um-chart").innerHTML;
    const before = svg();
    const t = umState.overview.trend.data;
    const a = t.models[0].daily[0].tokens,
      b = t.models[0].daily[1].tokens;
    // Half of day 2 moves to day 1: totals, ranking and markup stay the same.
    const shift = Math.round(b.value / 2);
    a.value += shift;
    b.value -= shift;
    t.all.daily[0].tokens.value += shift;
    t.all.daily[1].tokens.value -= shift;
    umHandle.update(umState);
    await umFrame();
    return {
      changed: svg() !== before,
      model: t.models[0].model,
      expect: UmState.tokens(a.value),
    };
  });
  check("R4", r4.changed, "趋势数据原地变化后（总量与排行不变）图形应重绘");
  await page.focus("[data-um-chart]");
  await page.keyboard.press("Home");
  const tip = await page.textContent(".um-tip");
  check(
    "R4",
    tip.includes(r4.model) && tip.includes(r4.expect),
    `重绘后当天提示应读到新数值 ${r4.expect}：${tip.slice(0, 120)}`
  );
  const kept = await page.evaluate(async () => {
    const t = umState.overview.trend.data;
    t.models[0].daily[0].tokens.value += 1;
    umHandle.update(umState);
    await umFrame();
    return {
      focused: document.activeElement?.hasAttribute("data-um-chart") ?? false,
      tip: !document.querySelector(".um-tip").hidden,
    };
  });
  check(
    "R4",
    kept.focused && kept.tip,
    `原地更新时图表应保留焦点与当前提示：${JSON.stringify(kept)}`
  );
  await page.keyboard.press("Escape");

  // ---------------------------------------------------------------- R5: no speed series disappears; lone values are not dots
  const r5 = await page.evaluate(async () => {
    const s = umState,
      base = s.overview.trend.data;
    const named = (name, speed) => {
      const m = structuredClone(base.models[0]);
      m.model = name;
      m.daily.forEach((d) => {
        d.speed = { kind: "exact", value: speed };
      });
      m.total.speed = { kind: "exact", value: speed };
      return m;
    };
    const show = (models) => {
      s.overview.trend = {
        state: "ready",
        data: { ...structuredClone(base), models },
        at: Date.now(),
      };
      umHandle.update(s);
    };
    const seen = () => {
      const curves = [...document.querySelectorAll(".um-chart-svg .um-line")];
      return {
        curves: curves.length,
        subpaths: curves.map(
          (p) => (p.getAttribute("d").match(/M/g) || []).length
        ),
        dots: [...document.querySelectorAll(".um-chart-svg circle")].filter(
          (c) => !c.closest(".um-cursor-dots")
        ).length,
        cursor:
          document.querySelector(".um-cursor")?.getAttribute("visibility") ??
          null,
        dashed: document.querySelectorAll(".um-chart-svg .um-line-other")
          .length,
        state:
          document.querySelector("[data-um-chart]")?.dataset.umChartState ??
          null,
        empty: /没有可画|连续有效数据不足/.test(
          document.querySelector(".um-chart").textContent
        ),
        tip: document.querySelector(".um-tip")?.textContent ?? "",
      };
    };
    // The no-static-dot probe must catch a circle whatever its class, or every
    // `dots === 0` below would be vacuous.
    const probeSvg = document.querySelector(".um-chart-svg");
    const probeDot = document.createElementNS(
      "http://www.w3.org/2000/svg",
      "circle"
    );
    probeDot.setAttribute("class", "um-stray-dot");
    probeSvg.appendChild(probeDot);
    const straySeen = [
      ...document.querySelectorAll(".um-chart-svg circle"),
    ].filter((c) => !c.closest(".um-cursor-dots")).length;
    probeDot.remove();
    // Four models take every colour, then a window holds only a newcomer.
    show(["a", "b", "c", "d"].map((n, i) => named(n, 30 + i)));
    document.querySelector('[data-um-metric="speed"]').click();
    await umFrame();
    show([named("new-model-after-window-change", 42)]);
    await umFrame();
    const solo = {
      ...seen(),
      ranked: /new-model-after-window-change/.test(
        document.querySelector(".um-trend-rank").textContent
      ),
    };
    // Six at once: six curves, the two without a colour dashed.
    show(["m1", "m2", "m3", "m4", "m5", "m6"].map((n, i) => named(n, 20 + i)));
    await umFrame();
    const six = seen();
    // A gap splits one model's curve in two; no point is drawn for the gap.
    const gapped = structuredClone(base.models[0]);
    gapped.model = "gapped";
    gapped.daily.forEach((d, i) => {
      d.speed =
        i === 2
          ? { kind: "missing", note: "缺口" }
          : { kind: "exact", value: 40 + i };
    });
    gapped.total.speed = { kind: "exact", value: 42 };
    show([gapped]);
    await umFrame();
    const gap = seen();
    // One lone value, a gap, another lone value: no curve, no dot, values kept.
    const lone = structuredClone(base.models[0]);
    lone.model = "lone";
    lone.daily.forEach((d, i) => {
      d.speed =
        i === 0
          ? { kind: "exact", value: 37 }
          : i === 2
            ? { kind: "exact", value: 51 }
            : { kind: "missing" };
    });
    lone.total.speed = { kind: "exact", value: 44 };
    show([lone]);
    await umFrame();
    const chartEl = document.querySelector("[data-um-chart]");
    chartEl.focus();
    chartEl.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Home",
        bubbles: true,
        cancelable: true,
      })
    );
    await umFrame();
    const loneSeen = {
      ...seen(),
      cursor:
        document.querySelector(".um-cursor")?.getAttribute("visibility") ??
        null,
    };
    // Real zeros still form a flat curve, not an empty window.
    const zero = structuredClone(base.models[0]);
    zero.model = "zero";
    zero.daily.forEach((d) => {
      d.speed = { kind: "exact", value: 0 };
    });
    zero.total.speed = { kind: "exact", value: 0 };
    show([zero]);
    await umFrame();
    const zeroSeen = seen();
    // Additive metrics fold models past the four colour slots: those rows say so
    // and link to nothing, so an aggregate is never passed off as one model.
    show(["m1", "m2", "m3", "m4", "m5", "m6"].map((n, i) => named(n, 20 + i)));
    document.querySelector('[data-um-metric="tokens"]').click();
    await umFrame();
    const folded = [...document.querySelectorAll(".um-rank-row")].map((r) => ({
      series: r.dataset.umSeries ?? null,
      note: r.querySelector(".um-rank-note")?.textContent ?? "",
    }));
    // A model whose bars are all real 0 draws no bar: its row must not link.
    const zeroBar = structuredClone(base.models[0]);
    zeroBar.model = "zero-bar";
    zeroBar.daily.forEach((d) => {
      d.tokens = { kind: "exact", value: 0 };
    });
    zeroBar.total.tokens = { kind: "exact", value: 0 };
    show([structuredClone(base.models[0]), zeroBar]);
    await umFrame();
    const zeroRow = [...document.querySelectorAll(".um-rank-row")].find(
      (r) => r.dataset.umRank === "zero-bar"
    );
    const zeroLink = zeroRow?.dataset.umSeries ?? null;
    s.overview.trend = { state: "ready", data: base, at: Date.now() };
    umHandle.update(s);
    return {
      days: base.days.length,
      solo,
      six,
      gap,
      lone: loneSeen,
      zero: zeroSeen,
      folded,
      zeroLink,
      straySeen,
    };
  });
  check(
    "R5",
    r5.solo.curves === 1 &&
      r5.solo.subpaths[0] === 1 &&
      r5.solo.dots === 0 &&
      !r5.solo.empty &&
      r5.solo.ranked,
    `换时段后只剩新模型时，速度图应画出它的一条曲线（${r5.days} 天）、无圆点、排行一致：${JSON.stringify(r5.solo)}`
  );
  check(
    "R5",
    r5.six.curves === 6 &&
      r5.six.dashed === 2 &&
      r5.six.dots === 0 &&
      r5.six.subpaths.every((n) => n === 1),
    `一个时段 6 个模型时速度图应画全 6 条曲线（其中 2 条中性虚线）、无逐点圆点：${JSON.stringify(r5.six)}`
  );
  check(
    "R5",
    r5.gap.curves === 2 &&
      r5.gap.subpaths.every((n) => n === 1) &&
      r5.gap.dots === 0 &&
      !r5.gap.empty,
    `缺口应把同一模型的曲线断成两段，不补 0、不硬连：${JSON.stringify(r5.gap)}`
  );
  check(
    "R5",
    r5.lone.curves === 0 &&
      r5.lone.dots === 0 &&
      r5.lone.state === "insufficient" &&
      r5.lone.empty &&
      /lone/.test(r5.lone.tip) &&
      /37 token\/秒/.test(r5.lone.tip),
    `孤立单值不应画点或线，但明细要保留数值并说明连续数据不足：${JSON.stringify(r5.lone)}`
  );
  check(
    "R5",
    r5.lone.cursor === "hidden",
    `孤立单值定位时也不应留下定位圆点或辅助线：${r5.lone.cursor}`
  );
  check(
    "R5",
    r5.zero.curves === 1 &&
      r5.zero.state === "zero" &&
      r5.zero.dots === 0 &&
      !r5.zero.empty,
    `真实 0 应画成一条平线，不能当成「没有数据」：${JSON.stringify(r5.zero)}`
  );
  const foldedRow = r5.folded.find((r) => r.series === null);
  check(
    "R5",
    !!foldedRow &&
      /并入「其他模型（2）」/.test(foldedRow.note) &&
      r5.folded.filter((r) => r.series !== null).length ===
        r5.folded.length - 1,
    `并入「其他模型」的行不联动任何单模型曲线，并如实写明：${JSON.stringify(r5.folded)}`
  );
  check(
    "R5",
    r5.zeroLink === null,
    `全 0 的模型在柱状图里没有可见柱，不应联动其他序列：${r5.zeroLink}`
  );
  check(
    "R5",
    r5.straySeen === 1,
    `静态散点判据应能数到任意 class 的 circle（否则 dots === 0 恒真）：${r5.straySeen}`
  );

  // ---------------------------------------------------------------- R6: 「加载更早」 retries per view
  for (const [view, attr, event] of [
    ["records", "data-um-earlier", "requests.earlier"],
    ["logs", "data-um-learlier", "logs.earlier"],
    ["maintenance", "data-um-mearlier", "maintenance.earlier"],
  ]) {
    const r6 = await page.evaluate(
      async ({ view, attr, event }) => {
        const s = umState;
        const pages = {
          records: () => ({ rows: [], scope: "合成的请求页", earlier: "c1" }),
          logs: () => ({
            lines: [],
            sources: [],
            scope: "合成的日志范围",
            earlier: "c1",
          }),
          maintenance: () => ({
            rows: [],
            scope: "合成的维护范围",
            earlier: "c1",
          }),
        };
        const set = (v, load) => {
          if (v === "records") s.requests.page = load;
          else if (v === "logs") s.requests.logs.page = load;
          else s.requests.maintenance = load;
        };
        const get = () =>
          view === "records"
            ? s.requests.page
            : view === "logs"
              ? s.requests.logs.page
              : s.requests.maintenance;
        const put = (load) => {
          set(view, load);
          umHandle.update(s);
        };
        // Every view offers the same literal cursor: they must not block each other.
        for (const v of Object.keys(pages))
          set(v, { state: "ready", data: pages[v](), at: Date.now() });
        location.hash = `#requests/${view}`;
        await umFrame();
        const data = get().data;
        umHandle.update(s);
        await umFrame();
        const button = () => document.querySelector(`[${attr}]`);
        const sent = () => umEvents.filter((e) => e.type === event).length;
        const start = sent();
        button().click();
        const asked = {
          disabled: button().disabled,
          label: button().textContent,
        };
        button().click(); // closed: never a second request for the same read
        const once = sent() - start;
        // It fails: the old page stays, with a newer failure.
        put({
          state: "stale",
          data,
          at: Date.now(),
          failure: { kind: "error", reason: "合成的读取超时", at: Date.now() },
        });
        await umFrame();
        const failed = {
          disabled: button().disabled,
          label: button().textContent,
          note: /没有成功/.test(document.querySelector(".um-more").textContent),
        };
        button().click();
        const retried = sent() - start;
        // That read is seen in flight, then ends without an answer (cancelled).
        put({
          state: "stale",
          data,
          at: Date.now(),
          failure: get().failure,
          refreshing: true,
        });
        put({ state: "stale", data, at: Date.now(), failure: get().failure });
        await umFrame();
        const cancelled = { disabled: button().disabled };
        // Asked here, the next view's identical cursor stays open.
        button().click();
        const next =
          view === "records"
            ? "logs"
            : view === "logs"
              ? "maintenance"
              : "records";
        const nextAttr = {
          records: "data-um-earlier",
          logs: "data-um-learlier",
          maintenance: "data-um-mearlier",
        }[next];
        location.hash = `#requests/${next}`;
        await umFrame();
        const neighbour =
          document.querySelector(`[${nextAttr}]`)?.disabled ?? null;
        // Answered by a page without the cursor, which releases the ask.
        put({
          state: "ready",
          data: { ...data, earlier: null },
          at: Date.now(),
        });
        return { asked, once, failed, retried, cancelled, neighbour };
      },
      { view, attr, event }
    );
    check(
      "R6",
      r6.asked.disabled && /正在读取更早/.test(r6.asked.label) && r6.once === 1,
      `${view}：点击后应只发一次并显示读取中：${JSON.stringify(r6.asked)}，发出 ${r6.once} 次`
    );
    check(
      "R6",
      !r6.failed.disabled &&
        /重试/.test(r6.failed.label) &&
        r6.failed.note &&
        r6.retried === 2,
      `${view}：读取失败后应保留旧页并可用同一 cursor 重试：${JSON.stringify(r6.failed)}，共发出 ${r6.retried} 次`
    );
    check(
      "R6",
      !r6.cancelled.disabled,
      `${view}：读取进行后无结果结束（取消）应释放按钮`
    );
    check(
      "R6",
      r6.neighbour === false,
      `${view}：另一视图字面相同的 cursor 不应被本视图的读取阻塞`
    );
  }

  check(
    "R4",
    errors.length === 0 && outbound.length === 0,
    `状态检查页不应报错或外发请求：${[...errors, ...outbound].slice(0, 3).join(" | ")}`
  );
  await ctx.close();
}
