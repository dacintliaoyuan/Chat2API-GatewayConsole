/* Console gate - PR11 checks (G1-G19). Merged from output/redesign-pr11/gate/pr11-gate.mjs
 * (2026-10-01) into the repo with the shared harness (core.mjs); the r6 honesty /
 * robustness criteria live in honesty.mjs. run.mjs is the entry point.
 *
 *   G1  no horizontal overflow (document, .wb-main, anything past the viewport
 *       that is not inside its own scroll container);
 *   G2  text contrast measured against the REAL composited background (every
 *       visible text run; 4.5:1, 3:1 for large text; disabled controls exempt);
 *   G3  no control falls back to the browser's own unstyled button/field look;
 *   G4  hit sizes: nav buttons, master-detail rows and fold rows ≥ 44px tall,
 *       every other control ≥ 24×24 (WCAG 2.5.8);
 *   G5  every icon-only control has an accessible name;
 *   G6  titles, readings and names really render in MiSans and the port numbers
 *       in JetBrains Mono (CDP platform fonts); the retired Smiley Sans and
 *       Manrope render nowhere (frontend handoff Plan-Revision 2 #8);
 *   G7  accent budget (Q16/Q20; since the 2026-10-31 redesign the accent fill is
 *       --wb-lemon-fill = the M3 primary container, resolved at run time):
 *       overview first screen = the current nav item only; every lane at most
 *       one large accent area;
 *   G8  overview first screen: header row, title, verdict strip and KPI band
 *       inside the first viewport at every viewport, including real 125% scaling;
 *   G9  the retired backdrop levels (Plan-Revision 2 #7) have no effect: every
 *       stored level draws the same static ground with no gradient, and the
 *       stored value is kept;
 *   G10 non-GET never leaves the browser (every one answered locally with 403);
 *   G11 master-detail keyboard: ↓ moves selection + focus, detail follows;
 *   G12 narrow widths: ≤ 1180px is icon-only navigation with named buttons;
 *   G13 reduced motion reaches the settings dialog (html[data-console-motion]);
 *   G14 a lane switch renders in < 250ms (the html:has() regression guard);
 *   G15 the settings dialog has 主题 / 侧栏 / 界面动效 / 透明度 and no font,
 *       backdrop or old 导航 group (Plan-Revision 2 #7);
 *   G16 the hollow lower-bound point is drawn and its inner colour is the tile's;
 *   G17 an armed 停止 confirm never moves to another chain (↓ in the list);
 *   G18 while the aggregate is pending the fee lane's left column holds the
 *       pending card and the loading bar is not covered by the sticky top;
 *   G19 the summary-error banner spans the content column (the glass sidebar
 *       floats beside it since the redesign) without horizontal scroll.
 *
 *
 * Every browser context comes from core.mjs (non-GET/HEAD answered 403 locally,
 * foreign hosts aborted, service workers blocked, key-like strings masked).
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createHash } from "node:crypto";
import { COLLECT, MASK_CSS, ANALYSE } from "./contrast-lib.mjs";
import { paths } from "./core.mjs";

// G1 / G3 / G4 / G5 / G7 in one DOM pass over the current screen.
const DOM_PASS = () => {
  const vw = innerWidth,
    vh = innerHeight;
  const rgb = (s) => {
    const m = s && s.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1]
      .split(/[ ,/]+/)
      .filter(Boolean)
      .map(Number);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const near = (c, t) =>
    c &&
    t &&
    c.a > 0.9 &&
    Math.abs(c.r - t[0]) <= 6 &&
    Math.abs(c.g - t[1]) <= 6 &&
    Math.abs(c.b - t[2]) <= 6;
  // The accent fill of the current theme, read from the page's own token
  // (--wb-lemon-fill) instead of a hard-coded colour.
  const probe = document.createElement("i");
  probe.style.cssText =
    "position:absolute;width:0;height:0;background-color:var(--wb-lemon-fill)";
  (document.querySelector(".workbench") ?? document.body).append(probe);
  const accent = rgb(getComputedStyle(probe).backgroundColor);
  probe.remove();
  const lemon =
    accent && accent.a > 0.9 ? [accent.r, accent.g, accent.b] : null;
  const visible = (el) => {
    const cs = getComputedStyle(el);
    if (
      cs.display === "none" ||
      cs.visibility !== "visible" ||
      Number(cs.opacity) === 0
    )
      return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const name = (el) =>
    (el.getAttribute("aria-label") || el.textContent || "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 30);
  const path = (el) => {
    const seg = [];
    for (
      let e = el, i = 0;
      e && e.nodeType === 1 && i < 3;
      e = e.parentElement, i++
    )
      seg.unshift(
        e.tagName.toLowerCase() +
          ((e.getAttribute("class") || "").trim()
            ? "." +
              e.getAttribute("class").trim().split(/\s+/).slice(0, 2).join(".")
            : "")
      );
    return seg.join(">");
  };
  // SVG elements clip by default (overflow: hidden), so only HTML boxes count as scroll containers.
  const scroller = (el) => {
    for (
      let e = el.parentElement;
      e && e !== document.body;
      e = e.parentElement
    ) {
      if (!(e instanceof HTMLElement)) continue;
      const o = getComputedStyle(e).overflowX;
      if (o === "auto" || o === "scroll" || o === "hidden" || o === "clip")
        return e;
    }
    return null;
  };
  const main = document.querySelector(".wb-main");
  const overflow = {
    doc: document.documentElement.scrollWidth > vw + 1,
    main: main ? main.scrollWidth > main.clientWidth + 1 : false,
    past: [],
  };
  for (const el of document.querySelectorAll(".workbench *")) {
    const r = el.getBoundingClientRect();
    if (
      !r.width ||
      r.right <= vw + 1 ||
      getComputedStyle(el).position === "fixed"
    )
      continue;
    const sc = scroller(el);
    if (sc && sc !== main && sc.getBoundingClientRect().right <= vw + 1)
      continue;
    if (!visible(el)) continue;
    overflow.past.push(`${path(el)} right=${Math.round(r.right)}`);
    if (overflow.past.length >= 6) break;
  }
  const unstyled = [],
    small = [],
    unnamed = [];
  const controls = [
    ...document.querySelectorAll(
      ".workbench :is(button, a[href], summary, select, input:not([type=hidden]), textarea, [role=button]), .console-appearance :is(button, summary, input, label)"
    ),
  ].filter(visible);
  for (const el of controls) {
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > vh) continue; // first screen only
    const bg = rgb(cs.backgroundColor);
    const defaultLook =
      /outset|inset/.test(cs.borderTopStyle) ||
      (el.tagName === "BUTTON" &&
        bg &&
        bg.a === 1 &&
        ((bg.r === 240 && bg.g === 240 && bg.b === 240) ||
          (bg.r === 239 && bg.g === 239 && bg.b === 239) ||
          (bg.r === 107 && bg.g === 107 && bg.b === 107)) &&
        cs.borderTopLeftRadius === "0px");
    if (defaultLook)
      unstyled.push(
        `${path(el)}「${name(el)}」 bg=${cs.backgroundColor} border=${cs.borderTopStyle}`
      );
    const tall44 = el.matches(".wb-nav button, .wb-md-item, summary");
    const hitH = r.height,
      hitW = r.width;
    if (
      el.matches("input[type=radio], input[type=checkbox]") &&
      el.closest("label")
    ) {
      /* the label is the target */
    } else if (tall44 && hitH < 43.5)
      small.push(
        `${path(el)}「${name(el)}」 ${Math.round(hitW)}×${Math.round(hitH)} (<44 tall)`
      );
    else if (
      !tall44 &&
      (hitH < 24 || hitW < 24) &&
      !el.closest("p, li, td, .wb-muted, small")
    )
      small.push(
        `${path(el)}「${name(el)}」 ${Math.round(hitW)}×${Math.round(hitH)} (<24)`
      );
    if (el.matches("button, [role=button], a[href]")) {
      const text = [...el.childNodes]
        .filter(
          (n) =>
            n.nodeType === 3 ||
            (n.nodeType === 1 &&
              !n.matches("svg, .wb-icon-hint, [aria-hidden=true]"))
        )
        .map((n) => n.textContent)
        .join("")
        .trim();
      if (
        !text &&
        !el.getAttribute("aria-label") &&
        !el.getAttribute("aria-labelledby")
      )
        unnamed.push(`${path(el)} title=${el.getAttribute("title") || ""}`);
    }
  }
  // Accent areas on the first screen (an accent fill = background within 6/255 of the token).
  const lemons = [];
  for (const el of document.querySelectorAll(
    ".workbench *, .console-appearance *"
  )) {
    const cs = getComputedStyle(el);
    const bgc = rgb(cs.backgroundColor);
    if (!near(bgc, lemon)) continue;
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > vh) continue;
    lemons.push({
      at: path(el),
      text: name(el),
      area: Math.round(r.width * r.height),
      w: Math.round(r.width),
      h: Math.round(r.height),
    });
  }
  return {
    overflow,
    unstyled: unstyled.slice(0, 8),
    small: small.slice(0, 10),
    unnamed: unnamed.slice(0, 8),
    lemons,
    accent: lemon,
  };
};

async function contrastPass(page, dsf) {
  const items = await page.evaluate(COLLECT);
  const style = await page.addStyleTag({ content: MASK_CSS });
  await page.waitForTimeout(80);
  const buf = await page.screenshot();
  await style.evaluate((el) => el.remove());
  const dataUrl = `data:image/png;base64,${buf.toString("base64")}`;
  const res = await page.evaluate(ANALYSE, { dataUrl, items, dpr: dsf });
  return res.filter((r) => r.cr !== null);
}

/** Run G1-G19 against c.BASE. `fixtures` supplies the summary payload G16 rewrites. */
export async function runGate(c, { fixtures }) {
  const {
    BASE,
    HOST,
    OUT,
    VIEWPORTS,
    THEMES,
    LANES,
    rows,
    nonGet,
    foreign,
    fail,
    newPage,
    gotoLane,
  } = c;
  // The candidate the gate measured (served entry assets + manifest source hash).
  const manifest = JSON.parse(
    readFileSync(
      resolve(c.build ?? paths.candidateDir, "candidate-build.json"),
      "utf8"
    )
  );
  const servedHtml = await (await fetch(BASE)).text();
  const servedAssets = [
    ...servedHtml.matchAll(/(?:src|href)="\/(assets\/[^"]+)"/g),
  ].map((m) => m[1]);
  const candidate = {
    base: BASE,
    sourceHash: manifest.sourceHash,
    builtAt: manifest.builtAt,
    servedAssets,
    servedMatchesManifest: servedAssets.every((a) =>
      manifest.entryAssets.includes(a)
    ),
  };
  if (!candidate.servedMatchesManifest)
    fail(
      "candidate",
      `${HOST} serves ${servedAssets.join(",")} but the manifest lists ${manifest.entryAssets.join(",")}`
    );

  // ---------------------------------------------------------------- main matrix
  for (const vp of VIEWPORTS) {
    for (const theme of THEMES) {
      const { ctx, page } = await newPage({
        width: vp.width,
        height: vp.height,
        dsf: vp.dsf,
        theme,
      });
      for (const L of LANES) {
        if (L.lane !== 0) await gotoLane(page, L.lane);
        // R3: the share lane renders three real tabs. Each tab is opened with a
        // click so G1-G7 measure the visible panel rather than one default
        // panel standing in for the whole lane.
        const tabs = L.lane === 6 ? ["keys", "diagnostics", "usage"] : [null];
        for (const tab of tabs) {
          if (tab) {
            await page
              .locator(`[role="tab"][data-wb-share-tab="${tab}"]`)
              .click();
            await page.waitForTimeout(500);
          }
          const label = `${vp.tag}/${theme}/${L.key}${tab ? `/${tab}` : ""}`;
          const dom = await page.evaluate(DOM_PASS);
          if (dom.overflow.doc || dom.overflow.main)
            fail(
              label,
              `G1 页面横向滚动（document=${dom.overflow.doc} main=${dom.overflow.main}）`
            );
          if (dom.overflow.past.length)
            fail(
              label,
              `G1 元素越出视口：${dom.overflow.past.slice(0, 3).join(" / ")}`
            );
          if (dom.unstyled.length)
            fail(
              label,
              `G3 控件退回浏览器默认外观：${dom.unstyled.slice(0, 3).join(" / ")}`
            );
          if (dom.small.length)
            fail(label, `G4 命中区不足：${dom.small.slice(0, 4).join(" / ")}`);
          if (dom.unnamed.length)
            fail(
              label,
              `G5 纯图标控件无可访问名：${dom.unnamed.slice(0, 3).join(" / ")}`
            );
          if (!dom.accent)
            fail(label, "G7 读不到强调色令牌 --wb-lemon-fill——该行判据不成立");
          const big = dom.lemons.filter(
            (l) => l.area > 4000 && !/wb-nav|appearance/.test(l.at)
          );
          if (L.lane === 0) {
            // Since the 2026-10-31 redesign the KPI band has no accent hero in
            // either theme: the current nav pill is the overview's only accent fill.
            const nav = dom.lemons.filter(
              (l) => l.area > 600 && /wb-nav/.test(l.at)
            ).length;
            const others = dom.lemons.filter(
              (l) => l.area > 600 && !/wb-nav/.test(l.at)
            );
            if (nav !== 1 || others.length)
              fail(
                label,
                `G7 总览首屏强调色填充应只有当前导航：${dom.lemons
                  .filter((l) => l.area > 600)
                  .map((l) => `${l.at}(${l.w}×${l.h})`)
                  .join(" / ")}`
              );
          } else if (big.length > 1) {
            fail(
              label,
              `G7 一页不止一块大面积强调色：${big.map((l) => `${l.at}(${l.w}×${l.h})`).join(" / ")}`
            );
          }
          const contrast = await contrastPass(page, vp.dsf);
          const low = contrast.filter(
            (c) =>
              !c.tags.includes("disabled") &&
              !c.placeholder &&
              c.crMedian < c.threshold
          );
          const lowPh = contrast.filter(
            (c) => c.placeholder && c.crMedian < 4.5
          );
          if (low.length)
            fail(
              label,
              `G2 对比度不足 ${low.length} 处：${low
                .slice(0, 4)
                .map(
                  (c) =>
                    `「${c.text}」${c.crMedian}<${c.threshold} ${c.effText}/${c.bgMedian} @${c.path.split(" > ").slice(-2).join(">")}`
                )
                .join(" / ")}`
            );
          if (lowPh.length)
            fail(
              label,
              `G2 占位文字对比度不足：${lowPh
                .slice(0, 2)
                .map((c) => `「${c.text}」${c.crMedian}`)
                .join(" / ")}`
            );
          if (L.lane === 0) {
            const layers = await page.evaluate(() => {
              const b = (s) => {
                const e = document.querySelector(s);
                if (!e) return null;
                const r = e.getBoundingClientRect();
                return { top: Math.round(r.top), bottom: Math.round(r.bottom) };
              };
              // The header row (.wb-head: title, status, quick jump) took the old top bar's place.
              return {
                vh: innerHeight,
                head: b(".wb-head"),
                title: b(".wb-title"),
                verdict: b(".wb-verdict"),
                kpis: b(".wb-kpis"),
                services: b(".wb-ov-services"),
              };
            });
            for (const k of ["head", "title", "verdict", "kpis"]) {
              if (!layers[k]) fail(label, `G8 首屏缺少 ${k}`);
              else if (layers[k].bottom > layers.vh)
                fail(
                  label,
                  `G8 首屏层 ${k} 底缘 ${layers[k].bottom} > 视口 ${layers.vh}`
                );
            }
            if (layers.services && layers.services.top > layers.vh)
              fail(label, `G8 服务明细顶缘 ${layers.services.top} 不在首屏`);
            rows.push({ label, layers });
          }
          const shot = `${OUT}/shots/${vp.tag}-${theme}-${L.key}${tab ? `-${tab}` : ""}.png`;
          await page.screenshot({ path: shot });
          rows.push({
            label,
            tab: tab ?? null,
            lemons: dom.lemons
              .filter((l) => l.area > 600)
              .map((l) => `${l.at} ${l.w}×${l.h}`),
            contrastMin: contrast.length
              ? Math.min(
                  ...contrast
                    .filter((c) => !c.tags.includes("disabled"))
                    .map((c) => c.crMedian)
                )
              : null,
            texts: contrast.length,
            shot,
          });
        }
      }
      // G6 fonts + G14 lane-switch time, once per theme at the first viewport.
      if (vp === VIEWPORTS[0]) {
        await gotoLane(page, 0);
        const cdp = await ctx.newCDPSession(page);
        await cdp.send("DOM.enable");
        await cdp.send("CSS.enable");
        const doc = await cdp.send("DOM.getDocument", { depth: -1 });
        const fontsOf = async (sel) => {
          const { nodeId } = await cdp.send("DOM.querySelector", {
            nodeId: doc.root.nodeId,
            selector: sel,
          });
          if (!nodeId) return [];
          return (
            await cdp.send("CSS.getPlatformFontsForNode", { nodeId })
          ).fonts.map((f) => f.familyName);
        };
        const want = [
          [".wb-title", /MiSans/],
          [".wb-block > h3", /MiSans/],
          ['.wb-kpis b[data-kpi="tokens"]', /MiSans/],
          [".wb-md-port", /JetBrains Mono/],
          [".wb-md-name", /MiSans/],
        ];
        const fonts = {};
        for (const [sel, re] of want) {
          fonts[sel] = await fontsOf(sel);
          if (!fonts[sel].some((f) => re.test(f)))
            fail(
              `${theme}/fonts`,
              `G6 ${sel} 实际字体 ${JSON.stringify(fonts[sel])}，应含 ${re}`
            );
          if (fonts[sel].some((f) => /Smiley Sans|Manrope/.test(f)))
            fail(
              `${theme}/fonts`,
              `G6 ${sel} 仍用已移除的字体：${JSON.stringify(fonts[sel])}`
            );
        }
        const switches = [];
        // Lane 1 is the 网关 workspace (its default view is the 全部模型 catalog,
        // the heaviest render since Rev17 folded the model lane into it).
        for (const lane of [3, 0, 1, 0]) {
          switches.push(
            await page.evaluate(async (lane) => {
              const t0 = performance.now();
              document
                .querySelector(`.wb-nav button[data-wb-lane="${lane}"]`)
                .click();
              return Math.round(performance.now() - t0);
            }, lane)
          );
          await page.waitForTimeout(1200);
        }
        if (Math.max(...switches) >= 250)
          fail(
            `${theme}/perf`,
            `G14 车道切换同步渲染 ${switches.join("/")}ms（≥250ms）`
          );
        rows.push({
          label: `${theme}/fonts+perf`,
          fonts,
          switchesMs: switches,
        });
        // G11 master-detail keyboard.
        const kb = await page.evaluate(async () => {
          const first = document.querySelector('[data-wb-md-item="0"]');
          first.click();
          await new Promise((r) => setTimeout(r, 200));
          document.querySelector('[data-wb-md-item="0"]').focus();
          document
            .querySelector('[data-wb-md-item="0"]')
            .dispatchEvent(
              new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })
            );
          await new Promise((r) => setTimeout(r, 300));
          const active = document.activeElement;
          const pressed = [...document.querySelectorAll("[data-wb-md-item]")]
            .filter((b) => b.getAttribute("aria-pressed") === "true")
            .map((b) => b.dataset.wbMdItem);
          const outline =
            getComputedStyle(active).outlineStyle +
            " " +
            getComputedStyle(active).outlineWidth;
          return {
            focused: active?.dataset?.wbMdItem ?? null,
            pressed,
            head: document
              .querySelector(".wb-md-detail h4")
              ?.textContent?.trim(),
            outline,
          };
        });
        if (
          kb.focused === "0" ||
          kb.focused === null ||
          kb.pressed.length !== 1 ||
          kb.pressed[0] !== kb.focused
        )
          fail(
            `${theme}/md-keyboard`,
            `G11 ↓ 未移动选择与焦点：${JSON.stringify(kb)}`
          );
        rows.push({ label: `${theme}/md-keyboard`, kb });
      }
      await ctx.close();
    }
  }

  // ---------------------------------------------------------------- G9 retired backdrops
  // Plan-Revision 2 #7 (2026-10-31): the vivid / soft / solid levels are retired.
  // A stored level is kept (so an older page can still read it) but draws
  // nothing: every level gives the same static ground, no gradient.
  for (const theme of THEMES) {
    const grounds = {};
    for (const backdrop of ["vivid", "soft", "solid"]) {
      const { ctx, page } = await newPage({ theme, prefs: { backdrop } });
      grounds[backdrop] = await page.evaluate(() => {
        let stored = null;
        try {
          stored =
            JSON.parse(
              localStorage.getItem("gateway-console-appearance-v1") ?? "null"
            )?.backdrop ?? null;
        } catch {}
        const cs = getComputedStyle(document.querySelector(".workbench"));
        return {
          attr: document.documentElement.dataset.consoleBackdrop ?? null,
          image: cs.backgroundImage,
          color: cs.backgroundColor,
          stored,
        };
      });
      await page.screenshot({
        path: `${OUT}/shots/backdrop-${backdrop}-${theme}.png`,
      });
      await ctx.close();
    }
    const h = (s) => createHash("sha1").update(s).digest("hex").slice(0, 8);
    for (const [level, g] of Object.entries(grounds)) {
      if (g.image !== "none")
        fail(
          `${theme}/backdrop`,
          `G9 已退役的「${level}」背景档仍画出背景图：${g.image.slice(0, 60)}`
        );
      if (g.attr !== null)
        fail(
          `${theme}/backdrop`,
          `G9 已退役的背景档仍写到 html[data-console-backdrop]=${g.attr}`
        );
      if (g.stored !== level)
        fail(
          `${theme}/backdrop`,
          `G9 已存的背景档「${level}」没有保留（读回 ${g.stored}）`
        );
    }
    if (
      new Set(Object.values(grounds).map((g) => `${g.image}|${g.color}`))
        .size !== 1
    )
      fail(
        `${theme}/backdrop`,
        `G9 三个已退役背景档的底色不一致：${JSON.stringify(grounds)}`
      );
    rows.push({
      label: `${theme}/backdrop`,
      grounds: Object.fromEntries(
        Object.entries(grounds).map(([k, v]) => [
          k,
          `${v.attr} ${h(v.image)} ${v.color} stored=${v.stored}`,
        ])
      ),
    });
  }

  // ---------------------------------------------------------------- G12 narrow widths + collapsed rail
  for (const c of [
    { tag: "w1280", width: 1280, height: 800 },
    { tag: "w1180", width: 1180, height: 800 },
    { tag: "w760", width: 760, height: 900 },
    { tag: "w375", width: 375, height: 812 },
    {
      tag: "rail-collapsed-1600",
      width: 1600,
      height: 900,
      prefs: { rail: "collapsed" },
    },
  ]) {
    for (const theme of THEMES) {
      const label = `${c.tag}/${theme}`;
      const { ctx, page } = await newPage({
        width: c.width,
        height: c.height,
        theme,
        prefs: c.prefs ?? {},
      });
      for (const L of [LANES[0], LANES[3], LANES[4]]) {
        if (L.lane !== 0) await gotoLane(page, L.lane);
        const dom = await page.evaluate(DOM_PASS);
        if (dom.overflow.doc || dom.overflow.main)
          fail(
            `${label}/${L.key}`,
            `G1 页面横向滚动（document=${dom.overflow.doc} main=${dom.overflow.main}）`
          );
        if (dom.overflow.past.length)
          fail(
            `${label}/${L.key}`,
            `G1 元素越出视口：${dom.overflow.past.slice(0, 3).join(" / ")}`
          );
        if (dom.unstyled.length)
          fail(
            `${label}/${L.key}`,
            `G3 控件退回浏览器默认外观：${dom.unstyled.slice(0, 3).join(" / ")}`
          );
        if (dom.unnamed.length)
          fail(
            `${label}/${L.key}`,
            `G5 纯图标控件无可访问名：${dom.unnamed.slice(0, 3).join(" / ")}`
          );
        await page.screenshot({
          path: `${OUT}/shots/${c.tag}-${theme}-${L.key}.png`,
        });
      }
      const rail = await page.evaluate(() => ({
        attr: document.documentElement.dataset.consoleRail,
        labelsShown: [
          ...document.querySelectorAll(".wb-nav .wb-nav-label"),
        ].filter((l) => getComputedStyle(l).display !== "none").length,
        navNamed: [...document.querySelectorAll(".wb-nav button")].every((b) =>
          b.getAttribute("aria-label")
        ),
        navMinH: Math.min(
          ...[...document.querySelectorAll(".wb-nav button")].map(
            (b) => b.getBoundingClientRect().height
          )
        ),
      }));
      const expectCollapsed = c.width <= 1180 || c.prefs?.rail === "collapsed";
      if (expectCollapsed && (rail.attr !== "collapsed" || rail.labelsShown))
        fail(label, `G12 应为纯图标导航：${JSON.stringify(rail)}`);
      if (!expectCollapsed && rail.attr !== "expanded")
        fail(label, `G12 应为文字导航：${JSON.stringify(rail)}`);
      if (!rail.navNamed) fail(label, "G12 导航按钮缺可访问名");
      if (rail.navMinH < 43.5)
        fail(label, `G4 导航命中高 ${rail.navMinH} < 44`);
      rows.push({ label, rail });
      await ctx.close();
    }
  }

  // ---------------------------------------------------------------- G13 / G15 settings dialog
  for (const theme of THEMES) {
    for (const reducedMotion of ["no-preference", "reduce"]) {
      const label = `dialog/${theme}/${reducedMotion}`;
      const { ctx, page } = await newPage({ theme, reducedMotion });
      await page.click('[data-action="settings"]');
      await page.waitForTimeout(500);
      const d = await page.evaluate(() => {
        const dlg = document.querySelector(".console-appearance");
        const groups = dlg
          ? [
              ...dlg.querySelectorAll(
                "legend, h3, .appearance-group-title, fieldset > span:first-child"
              ),
            ].map((e) => e.textContent.trim())
          : [];
        const t = dlg ? getComputedStyle(dlg).transitionDuration : null;
        return {
          open: !!dlg && getComputedStyle(dlg).display !== "none",
          groups,
          text: dlg?.textContent?.replace(/\s+/g, " ").slice(0, 400) ?? "",
          motionAttr: document.documentElement.dataset.consoleMotion,
          transition: t,
        };
      });
      if (!d.open) fail(label, "G15 外观设置没有打开");
      // Plan-Revision 2 #7: 背景 retired, 导航 became 侧栏, 透明度 added.
      for (const g of ["主题", "侧栏", "界面动效", "透明度"])
        if (!d.groups.includes(g))
          fail(label, `G15 缺少「${g}」组：${JSON.stringify(d.groups)}`);
      for (const g of ["背景", "导航"])
        if (d.groups.includes(g)) fail(label, `G15 仍有已退役的「${g}」组`);
      if (/字体/.test(d.text)) fail(label, "G15 仍有字体选项（PR11 已移除）");
      if (reducedMotion === "reduce" && d.motionAttr !== "reduced")
        fail(
          label,
          `G13 系统减少动效时 html[data-console-motion]=${d.motionAttr}`
        );
      if (reducedMotion === "no-preference" && d.motionAttr !== "full")
        fail(
          label,
          `G13 html[data-console-motion]=${d.motionAttr}（应为 full）`
        );
      const dom = await page.evaluate(DOM_PASS);
      if (dom.unstyled.length)
        fail(label, `G3 ${dom.unstyled.slice(0, 2).join(" / ")}`);
      const contrast = await contrastPass(page, 1);
      const low = contrast.filter(
        (c) =>
          !c.tags.includes("disabled") &&
          c.crMedian < c.threshold &&
          /appearance|dialog/.test(c.path)
      );
      if (low.length)
        fail(
          label,
          `G2 对话框对比度不足：${low
            .slice(0, 3)
            .map((c) => `「${c.text}」${c.crMedian}`)
            .join(" / ")}`
        );
      await page.screenshot({
        path: `${OUT}/shots/dialog-${theme}-${reducedMotion}.png`,
      });
      rows.push({
        label,
        dialog: { groups: d.groups, motionAttr: d.motionAttr },
      });
      await ctx.close();
    }
  }

  // ---------------------------------------------------------------- G16 hollow lower-bound point (PR11 reading)
  // The r6 fixture (summary-real.json of 09-27) no longer contains today-3, so the
  // same payload is built from the latest read-only capture: every chat2api day is
  // null + partial except today-3, which gets a value + partial, so the trend draws
  // a single-point segment - the hollow lower-bound point. PR11 puts the trend on a
  // charcoal tile, so "hollow" means: the point's inner colour is the surface it
  // sits on (the scoped --theme-panel = the tile), never a hard-coded light value.
  {
    const payload = structuredClone(fixtures["/api/summary"].body);
    const now = new Date();
    const d3 = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 3);
    const day = `${d3.getFullYear()}-${String(d3.getMonth() + 1).padStart(2, "0")}-${String(d3.getDate()).padStart(2, "0")}`;
    const chain = payload.services?.chat2api;
    const row = chain?.days?.find((d) => d.day === day);
    if (!row)
      fail("hollow-point", `G16 夹具建不起来：采集的 summary 里没有 ${day}`);
    else {
      if (typeof chain.costCny === "number") chain.costCnyPartial = true;
      for (const m of chain.models ?? []) {
        m.costCny = null;
        m.costCnyPartial = true;
      }
      for (const d of chain.days) {
        d.costCny = null;
        d.costCnyPartial = true;
      }
      row.costCny = 12.345;
      row.costCnyPartial = true;
      for (const theme of THEMES) {
        const label = `hollow-point/${theme}`;
        const { ctx, page } = await newPage({
          theme,
          override: async (route, req) => {
            if (!new URL(req.url()).pathname.endsWith("/api/summary"))
              return false;
            await route.fulfill({
              status: 200,
              contentType: "application/json",
              body: JSON.stringify(payload),
            });
            return true;
          },
        });
        await gotoLane(page, 3);
        const r = await page.evaluate((day) => {
          const pts = [...document.querySelectorAll("circle.wb-area-partial")];
          const tile = pts[0]?.closest(".wb-fee-trend");
          const tip = [...document.querySelectorAll("g.wb-area-hotspot")].find(
            (g) => (g.getAttribute("aria-label") || "").startsWith(day)
          );
          return {
            points: pts.map((c) => getComputedStyle(c).fill),
            tileBg: tile ? getComputedStyle(tile).backgroundColor : null,
            scopedPanel: pts[0]
              ? getComputedStyle(pts[0])
                  .getPropertyValue("--theme-panel")
                  .trim()
              : null,
            tip: tip
              ? [...tip.querySelectorAll(".wb-area-tip text")].map((t) =>
                  t.textContent.trim()
                )
              : null,
          };
        }, day);
        if (!r.points.length) fail(label, `G16 ${day} 没有画出空心下界点`);
        else {
          if (r.tileBg && r.points.some((f) => f !== r.tileBg))
            fail(
              label,
              `G16 空心点内色 ${r.points.join("/")} ≠ 所在图块底色 ${r.tileBg}`
            );
          if (!r.tip || !r.tip.some((t) => t.includes("≥")))
            fail(
              label,
              `G16 该日提示没有下界标识「≥」：${JSON.stringify(r.tip)}`
            );
        }
        rows.push({ label, day, hollow: r });
        await page.screenshot({
          path: `${OUT}/shots/hollow-point-${theme}.png`,
        });
        await ctx.close();
      }
    }
  }

  // ---------------------------------------------------------------- G17 armed confirm stays with its chain
  // Maintainer decision: 停止 keeps its two-step confirm. Arm 停止 on one chain in
  // the overview detail pane, move the selection with ↓, and the pane's 停止 must
  // no longer be armed; one more click there must only arm it (no request).
  {
    const label = "confirm-follows-chain";
    const { ctx, page } = await newPage({});
    const before = nonGet.length;
    await page.click('[data-wb-md-item="2"]');
    await page.waitForTimeout(300);
    await page.click('.wb-md-detail [data-wb-svc-action="stop"]');
    await page.waitForTimeout(200);
    const armed = await page.evaluate(() => {
      const b = document.querySelector(
        '.wb-md-detail [data-wb-svc-action="stop"]'
      );
      return { text: b?.textContent?.trim(), target: b?.dataset.wbTarget };
    });
    await page.focus('[data-wb-md-item="2"]');
    await page.keyboard.press("ArrowDown");
    await page.waitForTimeout(400);
    const after = await page.evaluate(() => {
      const b = document.querySelector(
        '.wb-md-detail [data-wb-svc-action="stop"]'
      );
      return {
        text: b?.textContent?.trim(),
        cls: b?.className,
        target: b?.dataset.wbTarget,
        head: document.querySelector(".wb-md-detail h4")?.textContent?.trim(),
      };
    });
    await page.click('.wb-md-detail [data-wb-svc-action="stop"]');
    await page.waitForTimeout(400);
    const second = await page.evaluate(() =>
      document
        .querySelector('.wb-md-detail [data-wb-svc-action="stop"]')
        ?.textContent?.trim()
    );
    const sent = nonGet.slice(before).filter((x) => /operations/.test(x));
    if (!/确认停止/.test(armed.text || ""))
      fail(
        label,
        `G17 第一次点击停止没有进入二次确认：${JSON.stringify(armed)}`
      );
    if (/确认停止/.test(after.text || "") || /wb-confirm/.test(after.cls || ""))
      fail(label, `G17 ↓ 换链后停止按钮仍处于确认态：${JSON.stringify(after)}`);
    if (sent.length)
      fail(label, `G17 换链后一次点击就发出了请求：${sent.join(" / ")}`);
    if (!/确认停止/.test(second || ""))
      fail(label, `G17 换链后点击停止应只进入确认态，实得「${second}」`);
    rows.push({ label, armed, after, second, sent });
    await ctx.close();
  }

  // ---------------------------------------------------------------- G18 fee lane while the aggregate is pending, loading bar visible
  for (const theme of THEMES) {
    const label = `fee-pending/${theme}`;
    const { ctx, page } = await newPage({
      theme,
      settle: 2500,
      override: async (route, req) => {
        return new URL(req.url()).pathname.endsWith("/api/summary"); // never answered: stays pending
      },
    });
    await gotoLane(page, 3);
    const r = await page.evaluate(() => {
      const main = document.querySelector(".wb-cost-main");
      const bar = document.querySelector(".wb-loadbar");
      let barVisible = null;
      if (bar) {
        const b = bar.getBoundingClientRect();
        const hit = document.elementFromPoint(
          b.left + b.width / 2,
          b.top + b.height / 2
        );
        barVisible =
          !!hit && (hit === bar || bar.contains(hit) || hit.contains(bar));
      }
      return {
        mainChildren: main ? main.children.length : null,
        pending: !!main?.querySelector(".wb-chart-pending"),
        bar: !!bar,
        barVisible,
      };
    });
    if (!r.mainChildren || !r.pending)
      fail(label, `G18 聚合未返回时左栏为空：${JSON.stringify(r)}`);
    if (!r.bar) fail(label, "G18 读取中没有加载条");
    else if (r.barVisible === false)
      fail(label, `G18 读取中的加载条被吸顶区盖住：${JSON.stringify(r)}`);
    rows.push({ label, feePending: r });
    await page.screenshot({ path: `${OUT}/shots/fee-pending-${theme}.png` });
    await ctx.close();
  }

  // ---------------------------------------------------------------- G19 summary error banner at narrow widths
  for (const c of [
    { w: 1600, h: 900 },
    { w: 1280, h: 800 },
    { w: 1024, h: 768 },
    { w: 375, h: 812 },
  ]) {
    const label = `banner/${c.w}`;
    const { ctx, page } = await newPage({
      width: c.w,
      height: c.h,
      override: async (route, req) => {
        if (!new URL(req.url()).pathname.endsWith("/api/summary")) return false;
        await route.fulfill({
          status: 500,
          contentType: "application/json",
          body: JSON.stringify({ error: "summary unavailable" }),
        });
        return true;
      },
    });
    const r = await page.evaluate(() => {
      const b = document.querySelector(".wb-banner");
      const main = document.querySelector(".wb-main");
      const rect = b && !b.hidden ? b.getBoundingClientRect() : null;
      // The content column's inner box (the floating sidebar and the scrollbar excluded).
      const col = main
        ? {
            left: Math.round(
              main.getBoundingClientRect().left + main.clientLeft
            ),
            right: Math.round(
              main.getBoundingClientRect().left +
                main.clientLeft +
                main.clientWidth
            ),
          }
        : null;
      return {
        shown: !!rect,
        left: rect ? Math.round(rect.left) : null,
        right: rect ? Math.round(rect.right) : null,
        col,
        vw: innerWidth,
        docX: document.documentElement.scrollWidth > innerWidth + 1,
        mainX: main ? main.scrollWidth > main.clientWidth + 1 : null,
        text: b?.textContent?.trim().slice(0, 60),
      };
    });
    if (!r.shown) fail(label, "G19 聚合 500 时没有显示横幅");
    else {
      if (r.docX || r.mainX)
        fail(label, `G19 横幅造成横向滚动：${JSON.stringify(r)}`);
      if (
        !r.col ||
        Math.abs(r.left - r.col.left) > 1 ||
        Math.abs(r.right - r.col.right) > 1
      )
        fail(
          label,
          `G19 横幅没有贴齐内容列两侧（横幅 ${r.left}..${r.right} / 内容列 ${r.col?.left}..${r.col?.right}）`
        );
      if (/不可达/.test(r.text || ""))
        fail(label, `G19 8001 应答了 500，横幅却写「不可达」：${r.text}`);
    }
    rows.push({ label, banner: r });
    await page.screenshot({ path: `${OUT}/shots/banner-${c.w}.png` });
    await ctx.close();
  }

  // ---------------------------------------------------------------- G10 non-GET self-check
  {
    const { ctx, page } = await newPage({});
    const status = await page.evaluate(
      async () =>
        (
          await fetch("/gw/usagesrv/api/console/operations", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: '{"probe":"pr11-gate"}',
          })
        ).status
    );
    if (status !== 403)
      fail("self-check", `G10 合成 POST 应得本地 403，实得 ${status}`);
    await ctx.close();
  }

  rows.push({
    label: "requests",
    nonGetAnsweredLocally: nonGet.length,
    nonGetSample: [...new Set(nonGet.map((s) => s.replace(/\?.*$/, "")))].slice(
      0,
      6
    ),
    foreignAborted: foreign.length,
  });
  if (foreign.length)
    rows.push({ label: "foreign", sample: [...new Set(foreign)].slice(0, 5) });

  return { candidate };
}
