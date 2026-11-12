// Contrast measurement for the console gate. Excluded from measurement: text inside
// a CLOSED <details> (outside its summary; range.getClientRects() still returns
// boxes for it), text clipped out of an ancestor's overflow box, and text not fully
// unoccluded (five-point check) - each was sampled against whatever lay underneath.
// In-page contrast measurement against the REAL composited background:
// 1) collect every visible, unoccluded text run (text nodes, SVG text, select
//    values, placeholders) with its computed colour, alpha and opacity chain;
// 2) hide glyph paint only (-webkit-text-fill-color / SVG fill), screenshot;
// 3) sample the background pixels under each run from that screenshot and
//    compute WCAG contrast of the effective text colour against them.

export const COLLECT = () => {
  const vw = innerWidth,
    vh = innerHeight;
  const out = [];
  const parse = (c) => {
    const m = c && c.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1]
      .split(/[ ,/]+/)
      .filter(Boolean)
      .map(Number);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const opacityChain = (el) => {
    let o = 1;
    for (let e = el; e && e.nodeType === 1; e = e.parentElement)
      o *= Number(getComputedStyle(e).opacity);
    return o;
  };
  const path = (el) => {
    const seg = [];
    for (
      let e = el, i = 0;
      e && e.nodeType === 1 && i < 4;
      e = e.parentElement, i++
    ) {
      const cls = (e.getAttribute("class") || "")
        .trim()
        .split(/\s+/)
        .filter(Boolean)
        .slice(0, 3)
        .map((c) => "." + c)
        .join("");
      seg.unshift(e.tagName.toLowerCase() + cls);
    }
    return seg.join(" > ");
  };
  const tags = (el) => {
    const t = [];
    if (el.closest(".wb-char, .wb-nav, .wb-brand-mark")) t.push("char");
    if (
      el.closest(
        ".wb-nav button[aria-pressed='true'], .wb-kpis > div:nth-child(3), .wb-cost > div:nth-child(3), .wb-tab-on, .wb-tab[aria-pressed='true'], .wb-section-tabs button[aria-pressed='true'], .wb-jump-active, .appearance-options label:has(:checked)"
      )
    )
      t.push("lemon");
    if (el.closest(":disabled, [aria-disabled='true']")) t.push("disabled");
    if (el.closest("th")) t.push("th");
    if (el.closest("svg")) t.push("chart");
    if (
      el.closest(
        ".wb-badge, .wb-delta, .wb-svc-result, .wb-share-pill, .wb-write-mark, .wb-sim-chip, .wb-count-chip, .wb-head-status, .wb-issue-tag, .wb-stale-chip, .wb-verdict-value"
      )
    )
      t.push("pill");
    if (
      el.closest(
        ".wb-muted, small, .wb-port, .wb-index, .wb-cost-note, .lane-note, .wb-lane-note, .wb-md-state, .wb-md-port, .wb-axislabel, .wb-gridlabel"
      )
    )
      t.push("secondary");
    if (el.closest("h2, h3, h4, .wb-title")) t.push("heading");
    if (el.closest(".wb-notices")) t.push("notice");
    if (el.closest("button, a, summary, select, input, label"))
      t.push("control");
    return t;
  };
  // A run counts only when it is fully unoccluded: the centre AND four points
  // inset from the corners must hit the element (or its own subtree / svg). A
  // run half under a fixed notice used to pass on its centre alone and was then
  // sampled against the notice's white card.
  const occludedBy = (el, r) => {
    const ix = Math.min(2, r.width / 4),
      iy = Math.min(2, r.height / 4);
    const pts = [
      [r.left + r.width / 2, r.top + r.height / 2],
      [r.left + ix, r.top + iy],
      [r.right - ix, r.top + iy],
      [r.left + ix, r.bottom - iy],
      [r.right - ix, r.bottom - iy],
    ];
    for (const [px, py] of pts) {
      const hit = document.elementFromPoint(
        Math.min(vw - 1, Math.max(0, px)),
        Math.min(vh - 1, Math.max(0, py))
      );
      if (!hit) return "none";
      if (hit === el || el.contains(hit) || hit.contains(el)) continue;
      // label/for or SVG text inside an svg the pointer hit
      if (el.closest("svg") && hit.closest("svg") === el.closest("svg"))
        continue;
      return path(hit);
    }
    return null;
  };
  // Text scrolled or clipped out of an ancestor's box (a table inside its own
  // scroll container) is not painted there, although its rects are in the viewport.
  const clippedOut = (el, r) => {
    const cx = r.left + r.width / 2,
      cy = r.top + r.height / 2;
    for (
      let e = el.parentElement;
      e && e !== document.body;
      e = e.parentElement
    ) {
      const c = getComputedStyle(e);
      if (c.overflowX === "visible" && c.overflowY === "visible") continue;
      const b = e.getBoundingClientRect();
      if (cx < b.left || cx > b.right || cy < b.top || cy > b.bottom)
        return true;
    }
    return false;
  };
  const push = (el, rects, text, color, extra = {}) => {
    const cs = getComputedStyle(el);
    const col = parse(color);
    if (!col || col.a === 0) return;
    const rs = rects.filter(
      (r) =>
        r.width >= 2 &&
        r.height >= 4 &&
        r.left >= 0 &&
        r.top >= 0 &&
        r.right <= vw &&
        r.bottom <= vh &&
        !clippedOut(el, r)
    );
    if (!rs.length) return;
    const occ = occludedBy(el, rs[0]);
    if (occ) return;
    out.push({
      text: text.replace(/\s+/g, " ").trim().slice(0, 40),
      color: col,
      opacity: opacityChain(el),
      fontSize: parseFloat(cs.fontSize),
      fontWeight: Number(cs.fontWeight) || 400,
      rects: rs.map((r) => ({ x: r.left, y: r.top, w: r.width, h: r.height })),
      path: path(el),
      tags: tags(el),
      ...extra,
    });
  };
  const root = document.body;
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    const t = n.nodeValue;
    if (!t || !t.trim()) continue;
    const el = n.parentElement;
    if (
      !el ||
      el.closest(
        "script, style, option, title, .wb-icon-hint, [aria-hidden='true'] .wb-icon-hint"
      )
    )
      continue;
    if (el.closest("svg")) continue; // handled below
    if (
      el.checkVisibility &&
      !el.checkVisibility({
        contentVisibilityAuto: true,
        visibilityProperty: true,
        opacityProperty: true,
      })
    )
      continue;
    const cs = getComputedStyle(el);
    if (cs.visibility !== "visible") continue;
    const range = document.createRange();
    range.selectNodeContents(n);
    const rects = [...range.getClientRects()];
    push(
      el,
      rects,
      t,
      cs.webkitTextFillColor &&
        cs.webkitTextFillColor !== cs.color &&
        parse(cs.webkitTextFillColor)?.a
        ? cs.webkitTextFillColor
        : cs.color
    );
  }
  document.querySelectorAll("svg text").forEach((el) => {
    if (
      el.checkVisibility &&
      !el.checkVisibility({
        contentVisibilityAuto: true,
        visibilityProperty: true,
        opacityProperty: true,
      })
    )
      return;
    const cs = getComputedStyle(el);
    if (cs.visibility !== "visible" || cs.display === "none") return;
    if (Number(cs.opacity) === 0) return;
    // skip tooltip plates that are hidden by group opacity
    if (opacityChain(el) === 0) return;
    push(el, [el.getBoundingClientRect()], el.textContent || "", cs.fill, {
      svg: true,
    });
  });
  document.querySelectorAll("select").forEach((el) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const rr = new DOMRect(
      r.left + 8,
      r.top + r.height * 0.25,
      Math.max(4, (r.width - 36) * 0.8),
      r.height * 0.5
    );
    push(el, [rr], el.selectedOptions[0]?.textContent || "", cs.color, {
      select: true,
    });
  });
  document
    .querySelectorAll(
      "input[type=text], input[type=search], input:not([type]), textarea"
    )
    .forEach((el) => {
      if (el.value) return;
      const ph = el.getAttribute("placeholder");
      if (!ph) return;
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el, "::placeholder");
      if (parseFloat(getComputedStyle(el).width) < 60) return;
      const rr = new DOMRect(
        r.left + parseFloat(getComputedStyle(el).paddingLeft),
        r.top + r.height * 0.3,
        Math.min(r.width * 0.6, ph.length * 12),
        r.height * 0.4
      );
      push(el, [rr], ph, cs.color, { placeholder: true });
    });
  return out;
};

export const MASK_CSS = `*, *::before, *::after { -webkit-text-fill-color: transparent !important; text-shadow: none !important; caret-color: transparent !important; text-decoration-color: transparent !important; }
svg text, svg tspan { fill: transparent !important; stroke: none !important; }
::placeholder { -webkit-text-fill-color: transparent !important; color: transparent !important; }
select { color: transparent !important; }`;

// Analyse a screenshot (data URL) in the page: per item, median background and
// the per-pixel contrast distribution of the effective text colour.
export const ANALYSE = async ({ dataUrl, items, dpr }) => {
  const img = await createImageBitmap(await (await fetch(dataUrl)).blob());
  const cv = new OffscreenCanvas(img.width, img.height);
  const cx = cv.getContext("2d");
  cx.drawImage(img, 0, 0);
  const data = cx.getImageData(0, 0, img.width, img.height).data;
  const lin = (c) => {
    c /= 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  const L = (r, g, b) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  const CR = (a, b) => {
    const x = Math.max(a, b),
      y = Math.min(a, b);
    return (x + 0.05) / (y + 0.05);
  };
  return items.map((it) => {
    const a = it.color.a * it.opacity;
    const crs = [];
    const bgs = [];
    for (const r of it.rects) {
      const x0 = Math.round(r.x * dpr),
        y0 = Math.round(r.y * dpr),
        x1 = Math.round((r.x + r.w) * dpr),
        y1 = Math.round((r.y + r.h) * dpr);
      const step = Math.max(1, Math.floor(Math.min(x1 - x0, y1 - y0) / 6));
      for (let y = y0; y < y1; y += step)
        for (let x = x0; x < x1; x += step) {
          if (x < 0 || y < 0 || x >= img.width || y >= img.height) continue;
          const i = (y * img.width + x) * 4;
          const br = data[i],
            bg = data[i + 1],
            bb = data[i + 2];
          const tr = it.color.r * a + br * (1 - a),
            tg = it.color.g * a + bg * (1 - a),
            tb = it.color.b * a + bb * (1 - a);
          crs.push(CR(L(tr, tg, tb), L(br, bg, bb)));
          bgs.push([br, bg, bb]);
        }
    }
    if (!crs.length) return { ...it, cr: null };
    const sorted = [...crs].sort((p, q) => p - q);
    const pick = (q) =>
      sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];
    const med = bgs.map((c) => c).sort((p, q) => L(...p) - L(...q))[
      Math.floor(bgs.length / 2)
    ];
    const eff = [
      it.color.r * a + med[0] * (1 - a),
      it.color.g * a + med[1] * (1 - a),
      it.color.b * a + med[2] * (1 - a),
    ];
    const large =
      it.fontSize >= 24 || (it.fontSize >= 18.66 && it.fontWeight >= 700);
    const hex = (c) =>
      "#" + c.map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");
    const lemonBg = med[0] > 185 && med[1] > 215 && med[2] < 130;
    return {
      text: it.text,
      path: it.path,
      tags:
        lemonBg && !it.tags.includes("lemon")
          ? [...it.tags, "lemon-bg"]
          : it.tags,
      fontSize: it.fontSize,
      fontWeight: it.fontWeight,
      large,
      textColor:
        hex([it.color.r, it.color.g, it.color.b]) +
        (a < 1 ? `@${a.toFixed(2)}` : ""),
      effText: hex(eff),
      bgMedian: hex(med),
      crMedian: +CR(L(...eff), L(...med)).toFixed(2),
      crP10: +pick(0.1).toFixed(2),
      crMin: +sorted[0].toFixed(2),
      threshold: large ? 3 : 4.5,
      rect: it.rects[0],
      svg: !!it.svg,
      select: !!it.select,
      placeholder: !!it.placeholder,
    };
  });
};
