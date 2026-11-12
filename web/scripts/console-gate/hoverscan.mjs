// Console gate - hover contrast scan: every visible, enabled button on every lane
// (both themes) is hovered one by one; a button whose hover fill is opaque and whose
// label then drops below 4.5:1 is reported. (Found the dark-theme hover that painted
// a filled primary button's label in its own fill colour, 1:1.)

/** @param c createContext() result. Records failures on c and returns the counts. */
export async function runHoverScan(c) {
  let checked = 0;
  const bad = [];
  for (const theme of c.THEMES) {
    const { ctx, page } = await c.newPage({
      theme,
      prefs: { motion: "reduced" },
    });
    const scan = async (lane) => {
      for (const h of await page.$$(".workbench button")) {
        const info = await h
          .evaluate((e) => {
            const r = e.getBoundingClientRect();
            const cs = getComputedStyle(e);
            return {
              vis:
                r.width > 0 &&
                r.height > 0 &&
                cs.visibility === "visible" &&
                !e.disabled &&
                r.top >= 0 &&
                r.bottom <= innerHeight,
              text: (e.textContent || "").trim().slice(0, 14),
            };
          })
          .catch(() => ({ vis: false }));
        if (!info.vis || !info.text) continue;
        try {
          await h.hover({ timeout: 1500 });
        } catch {
          continue;
        }
        await page.waitForTimeout(60);
        const m = await h
          .evaluate((e) => {
            const rgb = (s) => {
              const x = s.match(/rgba?\(([^)]+)\)/);
              if (!x) return null;
              const p = x[1]
                .split(/[ ,/]+/)
                .filter(Boolean)
                .map(Number);
              return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
            };
            const lin = (v) => {
              v /= 255;
              return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
            };
            const L = (q) =>
              0.2126 * lin(q.r) + 0.7152 * lin(q.g) + 0.0722 * lin(q.b);
            const cs = getComputedStyle(e);
            const bg = rgb(cs.backgroundColor),
              fg = rgb(cs.color);
            if (!bg || bg.a < 0.9 || !fg) return null;
            const hi = Math.max(L(bg), L(fg)),
              lo = Math.min(L(bg), L(fg));
            return {
              cr: +((hi + 0.05) / (lo + 0.05)).toFixed(2),
              bg: cs.backgroundColor,
              fg: cs.color,
              cls: String(e.className),
            };
          })
          .catch(() => null);
        checked++;
        if (m && m.cr < 4.5) bad.push({ theme, lane, text: info.text, ...m });
      }
      await page.mouse.move(2, 895);
    };
    for (const L of c.LANES) {
      if (L.lane !== 0) await c.gotoLane(page, L.lane);
      // R3: the share lane's controls are split across three tabs. Enter each
      // one with a real click so its buttons are actually scanned.
      const tabs = L.lane === 6 ? ["keys", "diagnostics", "usage"] : [null];
      for (const tab of tabs) {
        if (tab) {
          await page
            .locator(`[role="tab"][data-wb-share-tab="${tab}"]`)
            .click();
          await page.waitForTimeout(400);
        }
        await scan(`${L.key}${tab ? `/${tab}` : ""}`);
      }
      // Every key action is inline; scan the expanded action buttons directly.
      if (L.lane === 6) {
        await page.locator('[role="tab"][data-wb-share-tab="keys"]').click();
        await page.waitForTimeout(300);
        await scan("share/keys-actions");
      }
    }
    await ctx.close();
  }
  const distinct = [
    ...new Map(
      bad.map((b) => [`${b.theme}|${b.cls}|${b.bg}|${b.fg}`, b])
    ).values(),
  ];
  for (const b of distinct)
    c.fail(
      `hover/${b.theme}/${b.lane}`,
      `悬停后文字对比度 ${b.cr}:1 <4.5（「${b.text}」 ${b.fg} on ${b.bg}，${b.cls}）`
    );
  c.rows.push({ label: "hoverscan", checked, failing: bad.length });
  return { checked, failing: bad.length, distinct };
}
