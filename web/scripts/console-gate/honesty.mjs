// Console gate - honesty / robustness criteria, carried over from the r6 gate
// (output/r6-gate-20260927.mjs, maintainer reviews 2026-09-27..29). Only the
// criteria that still hold for the current console are kept; every threshold and
// wording rule below is the r6 value. Dropped on purpose: the PR9/PR10 visual
// numbers (token colours, radii, paddings, type ladder, sidebar geometry, content
// widths, canvas pixels), the fixed ±45° chevron angles (PR11 restyled the mark;
// "the mark flips" is kept), the hollow-point probe (gate.mjs G16), the first-
// screen layer positions (G8/G19) and the non-GET self-check (G10).
//
//   H1  candidate binding: the served build is release/candidate built from the
//       current source (same source-hash rule as vite.config.ts, entry asset bytes);
//   H2  the shared entry installs the role gate and appearance, the local console
//       mounts the current workbench, the ground stays static (backdrop retired);
//   H3  summary read failed (HTTP 500): the overview shows the banner, the lane
//       body names the measured status, and nothing claims 「不可达」; a good read
//       shows no banner;
//   H4  lower-bound costs (costCnyPartial): the cost lane's first reading layer
//       carries a lower-bound marker, a long partial amount keeps its marker within
//       [0,8]px right of the value, a partial table cell's 「≥」 hugs its number,
//       and null+partial days read as lower bounds in the chain row, the ledger row,
//       the trend tooltips and the resident day count (never 「分量未知」);
//   H5  a share failure (backend code or measured HTTP status, read or action) is
//       printed exactly once on the share failure surface, never mixed with the
//       other kind;
//   H6  folds: every rendered fold has the chevron mark, a pointer cursor, says
//       展开 / 收起 (inline or title), is at least 120px wide (an inline key-action
//       menu follows the 24px control target in G4), hides no ellipsis-
//       truncated text when open, toggles on every Enter and every real click with
//       the mark visibly flipping, a fold hosted in a table cell toggles from all
//       four cell edges, and an opened fold survives a lane switch;
//   H7  chart tooltips: no plate is narrower than its text, and each hotspot's
//       accessible name contains its own tooltip text;
//   H8  focus: each of the first sixteen Tab stops inside each lane's content (and
//       once, on the overview, in the shared header) shows a ring with at least
//       3:1 contrast against what surrounds it;
//   Every criterion that walks rendered items fails when it finds none where they
//   must exist (no chart tooltip on the overview / cost lane, no fold in a whole
//   viewport+theme pass, no Tab stop in a lane's content): an empty walk is a
//   broken criterion, never a pass.
//   H9  no uncaught page error on any lane.
import { readFileSync, readdirSync } from "node:fs";
import { createHash } from "node:crypto";
import { resolve } from "node:path";
import { paths } from "./core.mjs";

const LOWER_BOUND_MARKERS = ["≥", "已知部分", "下界", "只含可折算"];
const BOUND_TEXT = /≥|下界|只含可折算/;
const LONG_AMOUNT_TARGET = 1234567.89;
const LONG_AMOUNT_RE = /^≥¥\d{1,3}(,\d{3}){2,}\.\d{3}$/;
const MIN_FOLD_HOST_WIDTH = 120;
const FOLD_HINT = ["展开", "收起"];
const FOCUS_MIN_CONTRAST = 3;
const FOCUS_STOPS = 16;
/** Lanes whose first layer always carries charts, so tooltips must be found. */
const TIP_LANES = new Set(["overview", "cost"]);

const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const ymd = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

// ---------------------------------------------------------------- H1 candidate binding
const CANDIDATE_SOURCE_FILES = [
  "index.html",
  "package.json",
  "tsconfig.json",
  "vite.config.ts",
];
const CANDIDATE_SOURCE_DIRS = ["src", "public"];

/** H1 on its own (also used by run-free checks): source hash vs the candidate
 *  manifest, the served entry assets vs the candidate's bytes on disk, and —
 *  for a role candidate — the role the manifest says it was built as. */
export async function candidateBinding(
  base,
  buildDir = paths.candidateDir,
  role = null
) {
  const manifestPath = resolve(buildDir, "candidate-build.json");
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  } catch (e) {
    return {
      ok: false,
      why: `读不到候选清单 ${manifestPath}（${String(e.message).slice(0, 100)}）`,
    };
  }
  if (role !== null && manifest.role !== role)
    return {
      ok: false,
      why: `候选清单的角色是 ${manifest.role ?? "（未写）"}，不是 ${role}`,
    };
  const files = [...CANDIDATE_SOURCE_FILES];
  const walk = (abs, rel) => {
    for (const entry of readdirSync(abs, { withFileTypes: true })) {
      if (entry.isDirectory())
        walk(resolve(abs, entry.name), `${rel}/${entry.name}`);
      else files.push(`${rel}/${entry.name}`);
    }
  };
  for (const dir of CANDIDATE_SOURCE_DIRS)
    walk(resolve(paths.console, dir), dir);
  files.sort();
  const source = new Map(
    files.map((p) => [p, sha256(readFileSync(resolve(paths.console, p)))])
  );
  const listed = new Map(
    (manifest.sources ?? []).map((s) => [s.path, s.sha256])
  );
  const added = files.filter((p) => !listed.has(p));
  const removed = [...listed.keys()].filter((p) => !source.has(p));
  const mismatched = files.filter(
    (p) => listed.has(p) && listed.get(p) !== source.get(p)
  );
  const sourceHash = sha256(
    Buffer.from(
      files.map((p) => `${p}\u0000${source.get(p)}\n`).join(""),
      "utf8"
    )
  );
  const entryAssets = manifest.entryAssets ?? [];
  const disk = new Map(
    entryAssets.map((a) => {
      try {
        return [a, sha256(readFileSync(resolve(buildDir, a)))];
      } catch {
        return [a, null];
      }
    })
  );
  const served = { assets: [], bytes: [] };
  try {
    const html = await (await fetch(base)).text();
    served.assets = [
      ...html.matchAll(/(?:src|href)="\/?(assets\/[^"]+)"/g),
    ].map((m) => m[1]);
    for (const a of served.assets) {
      const got = sha256(
        Buffer.from(await (await fetch(new URL(a, base))).arrayBuffer())
      );
      served.bytes.push({ asset: a, matchesDisk: disk.get(a) === got });
    }
  } catch (e) {
    served.error = String(e.message).slice(0, 120);
  }
  const unlisted = served.assets.filter((a) => !entryAssets.includes(a));
  const byteMismatch = served.bytes
    .filter((b) => !b.matchesDisk)
    .map((b) => b.asset);
  const ok =
    manifest.sourceHash === sourceHash &&
    !added.length &&
    !removed.length &&
    !mismatched.length &&
    !served.error &&
    served.assets.length > 0 &&
    !unlisted.length &&
    !byteMismatch.length;
  return {
    ok,
    builtAt: manifest.builtAt,
    gitHead: manifest.gitHead,
    sourceHash: { manifest: manifest.sourceHash, computed: sourceHash },
    added: added.slice(0, 6),
    removed: removed.slice(0, 6),
    mismatched: mismatched.slice(0, 6),
    served,
    unlisted,
    byteMismatch,
  };
}

// ---------------------------------------------------------------- scenario payloads
/** r6 「费用下界」 payload, derived from the fixtures' own summary: the chat2api
 *  chain keeps a value but becomes partial, its models and days become
 *  null+partial, and every billed amount is scaled so the first-layer reading is a
 *  long amount (≈≥¥1,234,567.890). The four states are unchanged, only which level
 *  sits in which state. The billed set mirrors workbench.ts displayedCost (M365
 *  excluded, chains without a ¥ component excluded). */
function partialSummary(summary) {
  const raw = structuredClone(summary);
  const chain = raw.services?.chat2api;
  if (!chain) return null;
  if (typeof chain.costCny === "number") chain.costCnyPartial = true;
  for (const m of chain.models ?? []) {
    m.costCny = null;
    m.costCnyPartial = true;
  }
  for (const d of chain.days ?? []) {
    d.costCny = null;
    d.costCnyPartial = true;
  }
  const round3 = (v) => Math.round(v * 1000) / 1000;
  const billed = Object.entries(raw.services ?? {})
    .filter(
      ([n, s]) =>
        n.toLowerCase() !== "m365" &&
        s &&
        typeof s === "object" &&
        s.costCny !== undefined
    )
    .map(([, s]) => s);
  const printed = billed.reduce(
    (sum, s) => sum + (typeof s.costCny === "number" ? round3(s.costCny) : 0),
    0
  );
  const k = printed > 0 ? LONG_AMOUNT_TARGET / printed : 1;
  const grow = (node) => {
    if (node && typeof node.costCny === "number")
      node.costCny = Math.round(node.costCny * k * 10000) / 10000;
  };
  for (const s of billed) {
    grow(s);
    (s.models ?? []).forEach(grow);
    (s.days ?? []).forEach(grow);
  }
  return raw;
}

/** Days of the 14-day window (local days incl. today, as workbench.ts trendDays)
 *  that are null+partial in the chat2api chain: H4's expected count and dates. */
function nullPartialDays(payload) {
  const now = new Date();
  const window = new Set(
    Array.from({ length: 14 }, (_, i) =>
      ymd(new Date(now.getFullYear(), now.getMonth(), now.getDate() - i))
    )
  );
  return (payload.services?.chat2api?.days ?? [])
    .filter(
      (d) =>
        window.has(d.day) && d.costCny === null && d.costCnyPartial === true
    )
    .map((d) => d.day);
}

/** Share status that renders 「未开放」 with a 「开启分享」 button (workbench.ts
 *  shareStateOf: funnel off + share gateway instance down). */
function closedShareStatus(fixtures) {
  const live = fixtures["/api/console/share-management/status"]?.body ?? {};
  return {
    ...live,
    funnel_on: false,
    gateway_running: false,
    share_url: null,
    active_keys: 0,
    in_sync: true,
    last_publication_failure: null,
  };
}

const SHARE_FAIL_CASES = [
  {
    kind: "read",
    tag: "读取失败/后端错误码",
    http: 400,
    body: { error: "share_read_failed" },
    token: "share_read_failed",
    other: /HTTP \d{3}/,
  },
  {
    kind: "read",
    tag: "读取失败/实测 HTTP 状态",
    http: 500,
    body: {},
    token: "HTTP 500",
    other: /share_read_failed/,
  },
  // The code is kept under 24 characters: core.mjs masks 24+ character key-like
  // runs before anything is read or screenshotted, which would hide a longer code.
  {
    kind: "action",
    tag: "动作失败/后端错误码",
    http: 400,
    body: { error: "share_not_ready" },
    token: "share_not_ready",
    other: /HTTP \d{3}/,
  },
  {
    kind: "action",
    tag: "动作失败/实测 HTTP 状态",
    http: 500,
    body: {},
    token: "HTTP 500",
    other: /share_not_ready/,
  },
  // Proof row: an unrelated read (the operations ledger) answers the same measured
  // status, so a whole-page count would see it twice; the carrier count must not.
  {
    kind: "read",
    tag: "读取失败/无关读口同状态",
    http: 500,
    body: {},
    token: "HTTP 500",
    other: /share_read_failed/,
    extraOps: true,
    proveWholePage: true,
  },
];

// ---------------------------------------------------------------- in-page readers
/** Rendered folds only: the settings dialog's folds live on document.body and are
 *  0x0 while it is closed; they are not this surface's folds. */
const RENDERED_FOLD_PICK = `[...document.querySelectorAll(".workbench details")].find(d => {
  const s = d.querySelector(":scope > summary");
  if (!s || !s.querySelector("[data-wb-fold-chevron]")) return false;
  const c = getComputedStyle(s);
  return c.display !== "none" && c.visibility !== "hidden" && (s.offsetWidth > 0 || s.offsetHeight > 0);
})`;

/** R3 share page: enter one of the three real tabs by clicking it and wait for
 *  its panel. The overview and the address line stay outside the panels. */
async function openShareTab(page, id) {
  const tab = page.locator(`[role="tab"][data-wb-share-tab="${id}"]`);
  await tab.click();
  await page
    .locator(`[data-wb-share-tabpanel="${id}"]`)
    .waitFor({ state: "visible" });
}

/** Expand a share page's secondary fold by its data-wb-share-panel id. */
async function openShareFold(page, id) {
  const fold = page.locator(`details[data-wb-share-panel="${id}"]`);
  if ((await fold.count()) && (await fold.getAttribute("open")) === null) {
    await fold.locator(":scope > summary").click();
  }
}

function readSurface(cfg) {
  const num = (v) => Number(Number(v).toFixed(1));
  const visible = (el) => {
    const c = getComputedStyle(el);
    return (
      c.display !== "none" &&
      c.visibility !== "hidden" &&
      (el.offsetWidth > 0 || el.offsetHeight > 0)
    );
  };
  const tipOverflow = [];
  for (const tip of document.querySelectorAll(".wb-area-tip")) {
    const rect = tip.querySelector("rect");
    const texts = [...tip.querySelectorAll("text")];
    if (!rect || !texts.length) continue;
    const w = Number(rect.getAttribute("width"));
    const x0 = Number(rect.getAttribute("x") || 0);
    const ink = Math.max(
      ...texts.map((t) => {
        const b = t.getBBox();
        return b.x + b.width - x0;
      })
    );
    if (w - ink < 0) tipOverflow.push(num(ink - w));
  }
  const ariaMismatch = [
    ...document.querySelectorAll("g.wb-area-hotspot"),
  ].filter((g) => {
    const first = g.querySelector(".wb-area-tip text");
    return (
      first &&
      !String(g.getAttribute("aria-label") ?? "").includes(
        String(first.textContent ?? "").trim()
      )
    );
  }).length;
  const folds = [...document.querySelectorAll(".workbench details")]
    .map((d) => {
      const s = d.querySelector(":scope > summary");
      if (!s || !visible(s)) return null;
      const title = s.getAttribute("title") || "";
      const inline = (s.textContent || "").trim();
      return {
        text: inline.slice(0, 30),
        chevron: !!s.querySelector("[data-wb-fold-chevron]"),
        cursor: getComputedStyle(s).cursor,
        width: num(s.getBoundingClientRect().width),
        hint: cfg.foldHint.some((w) => `${title} ${inline}`.includes(w)),
      };
    })
    .filter(Boolean);
  // R3: every low-frequency key action is expanded inline. They are ordinary
  // buttons (no 更多 menu): keep the control-target and pointer contract.
  const inlineActions = [
    ...document.querySelectorAll(
      ".workbench .wb-share-key-actions [data-wb-share-key]"
    ),
  ]
    .filter(visible)
    .map((el) => {
      const inline = (el.textContent || "").trim();
      return {
        text: inline.slice(0, 30),
        cursor: getComputedStyle(el).cursor,
        width: num(el.getBoundingClientRect().width),
      };
    });
  const truncated = [
    ...document.querySelectorAll(
      ".workbench details[open] :is(p, li, span, div, td)"
    ),
  ]
    .filter((el) => {
      const c = getComputedStyle(el);
      return (
        c.textOverflow === "ellipsis" && el.scrollWidth > el.clientWidth + 1
      );
    })
    .map((el) => (el.textContent || "").trim().slice(0, 24));
  const banner = document.querySelector(".wb-banner");
  const note = document.querySelector("[data-wb-summary-note]");
  const costCells = [...document.querySelectorAll("[data-wb-cost-state]")].map(
    (cell) => {
      const value = cell.querySelector('[data-kpi="cost"]');
      const marker = cell.querySelector("[data-wb-cost-marker]");
      const vr = value?.getBoundingClientRect(),
        mr = marker?.getBoundingClientRect();
      return {
        state: cell.getAttribute("data-wb-cost-state"),
        valueText: value ? (value.textContent || "").trim() : null,
        markerText: marker ? (marker.textContent || "").trim() : null,
        gap: vr && mr ? num(mr.left - vr.right) : null,
      };
    }
  );
  const partialRows = [];
  for (const tr of document.querySelectorAll(".wb-content table tbody tr")) {
    const cell = [...tr.querySelectorAll("td")].find((td) =>
      (td.textContent || "").trim().startsWith("≥")
    );
    if (!cell) continue;
    const node =
      [...cell.childNodes].find(
        (n) => n.nodeType === 3 && (n.textContent || "").includes("≥")
      ) ??
      [...cell.querySelectorAll("*")]
        .flatMap((e) => [...e.childNodes])
        .find((n) => n.nodeType === 3 && (n.textContent || "").includes("≥"));
    let gap = null;
    if (node) {
      const t = node.textContent,
        at = t.indexOf("≥");
      if (t.length > at + 1) {
        const r1 = document.createRange();
        r1.setStart(node, at);
        r1.setEnd(node, at + 1);
        const r2 = document.createRange();
        r2.setStart(node, at + 1);
        r2.setEnd(node, t.length);
        gap = num(
          r2.getBoundingClientRect().left - r1.getBoundingClientRect().right
        );
      }
    }
    partialRows.push({
      cell: (cell.textContent || "").trim().slice(0, 40),
      gap,
    });
  }
  const costChart = [...document.querySelectorAll(".wb-fee-trend")].find((c) =>
    /计费链|¥/.test(c.querySelector("h4")?.textContent || "")
  );
  const costTrendTips = costChart
    ? [...costChart.querySelectorAll("g.wb-area-hotspot")].map((g) => ({
        date: (g.getAttribute("aria-label") || "").split(/[：:\s]/)[0],
        texts: [...g.querySelectorAll(".wb-area-tip text")].map((t) =>
          (t.textContent || "").trim()
        ),
      }))
    : null;
  const feeTableRows = [
    ...document.querySelectorAll(".wb-content section, .wb-content .wb-block"),
  ]
    .filter((s) =>
      /^各网关费用/.test((s.querySelector("h3")?.textContent || "").trim())
    )
    .flatMap((s) =>
      [...s.querySelectorAll("tbody tr")].map((tr) =>
        (tr.textContent || "").replace(/\s+/g, " ").trim().slice(0, 90)
      )
    );
  const ledgerRows = [
    ...document.querySelectorAll(".wb-ledger .wb-cred-row"),
  ].map((r) => (r.textContent || "").replace(/\s+/g, " ").trim().slice(0, 90));
  return {
    tipCount: document.querySelectorAll(".wb-area-tip").length,
    tipOverflow,
    ariaMismatch,
    folds,
    inlineActions,
    truncated,
    banner:
      banner && !banner.hidden && visible(banner)
        ? (banner.textContent || "").trim().slice(0, 140)
        : null,
    summaryNote: note ? (note.textContent || "").trim().slice(0, 200) : null,
    firstLayerSample: (
      document.querySelector(".wb-content")?.textContent || ""
    ).slice(0, 400),
    costCells,
    partialRows,
    costTrendTips,
    feeTableRows,
    ledgerRows,
    costNote:
      (document.querySelector("[data-wb-cost-note]")?.textContent || "")
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 300) || null,
  };
}

// ---------------------------------------------------------------- fold probes
/** H6: the first rendered fold toggles on Enter (twice) and on a real click at its
 *  centre (twice), each input toggling against the state before it; the mark's
 *  rotation differs between closed and open. Toggles are recorded on a document-
 *  level capture listener (the panel patcher may rebuild the node). */
async function foldFlipProbe(page) {
  const found = await page.evaluate((pick) => {
    const d = eval(pick);
    if (!d) return null;
    const s = d.querySelector(":scope > summary");
    if (d.open) s.click();
    s.scrollIntoView({ block: "center" });
    return (s.textContent || "").trim().slice(0, 30);
  }, RENDERED_FOLD_PICK);
  if (found === null) return { kind: "none" };
  await page.waitForTimeout(450);
  await page.evaluate(() => {
    const angleOf = (el) => {
      const mark = el.querySelector(":scope > summary [data-wb-fold-chevron]");
      const m = mark
        ? getComputedStyle(mark).transform.match(/matrix\(([^)]+)\)/)
        : null;
      const [a, b] = m ? m[1].split(",").map(Number) : [1, 0];
      return Math.round(((Math.atan2(b, a) * 180) / Math.PI) * 100) / 100;
    };
    window.__hFold = [];
    window.__hAngle = angleOf;
    document.addEventListener(
      "toggle",
      (ev) => {
        const el = ev.target;
        if (!el || el.tagName !== "DETAILS") return;
        const e = { open: el.open, settled: null };
        window.__hFold.push(e);
        setTimeout(() => {
          e.settled = angleOf(el);
        }, 650);
      },
      true
    );
  });
  const state = () =>
    page.evaluate((pick) => {
      const d = eval(pick);
      return d ? { open: d.open, angle: window.__hAngle(d) } : null;
    }, RENDERED_FOLD_PICK);
  const count = () => page.evaluate(() => window.__hFold.length);
  const waitNew = (n) =>
    page
      .waitForFunction(
        (c) =>
          window.__hFold.length > c && window.__hFold.at(-1).settled !== null,
        n,
        { timeout: 2500 }
      )
      .catch(() => {});
  const closed = await state();
  const inputs = [];
  await page.evaluate(
    (pick) => eval(pick)?.querySelector(":scope > summary").focus(),
    RENDERED_FOLD_PICK
  );
  for (const name of ["Enter", "第二次 Enter"]) {
    const pre = await state(),
      n = await count();
    await page.keyboard.press("Enter");
    await waitNew(n);
    const toggles = await page.evaluate((c) => window.__hFold.slice(c), n);
    inputs.push({
      name: `键盘${name}`,
      pre: pre?.open ?? null,
      toggle: toggles[0] ?? null,
    });
  }
  for (const name of ["点击", "第二次点击"]) {
    const pre = await state(),
      n = await count();
    const at = await page.evaluate((pick) => {
      const b = eval(pick)
        ?.querySelector(":scope > summary")
        .getBoundingClientRect();
      return b ? { x: b.left + b.width / 2, y: b.top + b.height / 2 } : null;
    }, RENDERED_FOLD_PICK);
    if (!at) break;
    await page.mouse.click(at.x, at.y);
    await waitNew(n);
    const toggles = await page.evaluate((c) => window.__hFold.slice(c), n);
    const hit = await page.evaluate((p) => {
      const el = document.elementFromPoint(p.x, p.y);
      return el
        ? `${el.tagName}.${String(el.className || "").split(" ")[0]}`
        : "视口外";
    }, at);
    inputs.push({
      name: `鼠标${name}`,
      pre: pre?.open ?? null,
      toggle: toggles[0] ?? null,
      hit,
    });
  }
  const openAngle =
    inputs[0]?.toggle?.open === true ? inputs[0].toggle.settled : null;
  if ((await state())?.open)
    await page.evaluate(
      (pick) => eval(pick)?.querySelector(":scope > summary").click(),
      RENDERED_FOLD_PICK
    );
  return {
    kind: "checked",
    text: found,
    closedAngle: closed?.angle ?? null,
    openAngle,
    inputs,
  };
}

/** H6: a fold whose host is a table cell toggles when the cell is clicked 2px
 *  inside its bottom edge and 4px inside its left / right / top edges (real
 *  pointer clicks; summary.click() would bypass the hit box). */
async function foldHostEdgeHits(page) {
  // Only the share lane has the usage summary table. Every other lane has no
  // such cell host; the old selector silently found none there.
  if (!(await page.locator('[role="tab"][data-wb-share-tab="usage"]').count()))
    return { kind: "none", problems: [] };
  // Usage cells keep their whole-cell disclosure contract. The key-actions
  // layer is a fixed menu rendered outside the table, never a cell-hosted fold,
  // so `:not([data-wb-share-key-menu])` keeps the same exclusion the old
  // `.wb-share-key-more` details class provided.
  const selected = await page.evaluate(() => {
    const tab = document.querySelector(
      '[role="tab"][data-wb-share-tab="usage"]'
    );
    return tab?.getAttribute("aria-selected") === "true";
  });
  await openShareTab(page, "usage");
  const sel = (inline = "start") =>
    page.evaluate((inline) => {
      const d = [
        ...document.querySelectorAll(
          ".workbench td details:not([data-wb-share-key-menu])"
        ),
      ].find((x) => x.querySelector(":scope > summary") && x.offsetWidth > 0);
      if (!d) return null;
      d.querySelector(":scope > summary").scrollIntoView({
        block: "center",
        inline,
      });
      const t = d.closest("td").getBoundingClientRect();
      return {
        open: d.open,
        text: (d.querySelector(":scope > summary").textContent || "")
          .trim()
          .slice(0, 24),
        td: { l: t.left, r: t.right, t: t.top, b: t.bottom },
      };
    }, inline);
  let g = await sel();
  if (!g) {
    if (!selected) await openShareTab(page, "keys");
    return { kind: "none" };
  }
  await page.waitForTimeout(500);
  g = await sel();
  const read = () =>
    page.evaluate(
      () =>
        [
          ...document.querySelectorAll(
            ".workbench td details:not([data-wb-share-key-menu])"
          ),
        ].find((x) => x.offsetWidth > 0)?.open ?? null
    );
  const edge = (geo, name) =>
    name === "底缘内 2px"
      ? { x: (geo.td.l + geo.td.r) / 2, y: geo.td.b - 2 }
      : name === "上缘内 4px"
        ? { x: (geo.td.l + geo.td.r) / 2, y: geo.td.t + 4 }
        : name === "左缘内 4px"
          ? { x: geo.td.l + 4, y: (geo.td.t + geo.td.b) / 2 }
          : { x: geo.td.r - 4, y: (geo.td.t + geo.td.b) / 2 };
  const size = page.viewportSize();
  const problems = [];
  for (const name of ["底缘内 2px", "左缘内 4px", "右缘内 4px", "上缘内 4px"]) {
    const geo = await sel(name === "右缘内 4px" ? "end" : "start");
    if (!geo) {
      problems.push(`${name}: 宿主折叠消失`);
      break;
    }
    const p = edge(geo, name);
    if (p.x < 1 || p.y < 1 || p.x > size.width - 1 || p.y > size.height - 1) {
      problems.push(`${name}: 点击点不在视口内，该点未测`);
      continue;
    }
    const before = await read();
    await page.mouse.click(p.x, p.y);
    await page
      .waitForFunction(
        (w) =>
          [
            ...document.querySelectorAll(
              ".workbench td details:not([data-wb-share-key-menu])"
            ),
          ].find((x) => x.offsetWidth > 0)?.open === w,
        !before,
        { timeout: 2000 }
      )
      .catch(() => {});
    const afterOpen = await read();
    const geo2 = await sel(name === "右缘内 4px" ? "end" : "start");
    const p2 = geo2 ? edge(geo2, name) : p;
    await page.mouse.click(p2.x, p2.y);
    await page
      .waitForFunction(
        (w) =>
          [
            ...document.querySelectorAll(
              ".workbench td details:not([data-wb-share-key-menu])"
            ),
          ].find((x) => x.offsetWidth > 0)?.open === w,
        before,
        { timeout: 2000 }
      )
      .catch(() => {});
    const afterClose = await read();
    if (afterOpen === before || afterClose !== before)
      problems.push(`${name}: ${before} → ${afterOpen} → ${afterClose}`);
  }
  if (await read()) {
    const geo = await sel();
    if (geo) {
      const p = edge(geo, "底缘内 2px");
      await page.mouse.click(p.x, p.y);
    }
  }
  if (!selected) await openShareTab(page, "keys");
  return { kind: "checked", text: g.text, problems };
}

/** H6: an opened fold is still open after switching to another lane and back. */
async function foldSurvivesLaneSwitch(c, page, lane) {
  const opened = await page.evaluate((pick) => {
    const d = eval(pick);
    if (!d) return null;
    const s = d.querySelector(":scope > summary");
    if (d.open) s.click();
    s.click();
    return {
      title: (s.textContent || "").trim().slice(0, 40),
      afterClick: d.open,
      host:
        d
          .getAttributeNames()
          .filter((n) => n.startsWith("data-wb"))
          .join(",") || "(无宿主属性)",
    };
  }, RENDERED_FOLD_PICK);
  if (!opened) return null;
  if (!opened.afterClick)
    return {
      ...opened,
      problem: `折叠入口「${opened.title}」点击后 open 仍为 false（宿主 ${opened.host}）`,
    };
  await c.gotoLane(page, lane === 0 ? 1 : 0);
  await c.gotoLane(page, lane);
  const after = await page.evaluate((t) => {
    const d = [...document.querySelectorAll(".workbench details")].find(
      (x) =>
        (x.querySelector(":scope > summary")?.textContent || "")
          .trim()
          .slice(0, 40) === t
    );
    if (!d) return null;
    const open = d.open;
    if (open) d.querySelector(":scope > summary").click();
    return open;
  }, opened.title);
  if (after === null)
    return {
      ...opened,
      problem: `折叠项「${opened.title}」切车道后找不到（该行判据不成立；宿主 ${opened.host}）`,
    };
  if (!after)
    return {
      ...opened,
      problem: `折叠开合态未跨车道保持：「${opened.title}」切回后 open=false（宿主 ${opened.host}）`,
    };
  return opened;
}

/** H8: ring colour vs the composited backdrop around it, for the first
 *  FOCUS_STOPS Tab stops after `from` - the lane's own content (".wb-content"),
 *  or the page start ("body") for the shared header. Starting every lane at the
 *  body would walk the same header stops on every lane and never reach content. */
async function focusRing(page, from = ".wb-content") {
  await page.evaluate((sel) => {
    document.activeElement?.blur?.();
    const start =
      sel === "body"
        ? document.body
        : (document.querySelector(sel) ?? document.body);
    // The start point keeps its temporary tabindex until the first Tab has left
    // it (removing it while focused would drop focus back to the page start).
    if (!start.hasAttribute("tabindex"))
      start.setAttribute(
        "data-gate-tabstart",
        start.getAttribute("tabindex") ?? ""
      );
    if (!start.hasAttribute("tabindex")) start.setAttribute("tabindex", "-1");
    start.focus({ preventScroll: true });
  }, from);
  const out = [];
  for (let i = 0; i < FOCUS_STOPS; i++) {
    await page.keyboard.press("Tab");
    if (i === 0)
      await page.evaluate(() =>
        document.querySelectorAll("[data-gate-tabstart]").forEach((n) => {
          n.removeAttribute("tabindex");
          n.removeAttribute("data-gate-tabstart");
        })
      );
    await page.waitForTimeout(60);
    const info = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el || el === document.body) return null;
      const parse = (v) => {
        const t = String(v || "").trim();
        if (!t || t === "transparent") return null;
        const n = t.match(/-?[\d.]+(?:e-?\d+)?/g);
        if (!n || n.length < 3) return null;
        const scale = t.startsWith("color(srgb") ? 255 : 1;
        const a = n.length >= 4 ? Number(n[3]) : 1;
        return {
          rgb: n.slice(0, 3).map((x) => Math.round(Number(x) * scale)),
          a,
        };
      };
      const c = getComputedStyle(el);
      const lum = (rgb) => {
        const f = (x) => {
          const s = x / 255;
          return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
        };
        return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2]);
      };
      const ratio = (a, b) =>
        Number(
          (
            (Math.max(lum(a), lum(b)) + 0.05) /
            (Math.min(lum(a), lum(b)) + 0.05)
          ).toFixed(2)
        );
      const paperOf = () =>
        parse(
          getComputedStyle(
            document.querySelector(".workbench") || document.body
          ).backgroundColor
        )?.rgb ??
        parse(
          getComputedStyle(document.documentElement).getPropertyValue(
            "--theme-paper"
          )
        )?.rgb ?? [255, 255, 255];
      // Backdrop = layers composited from the ring's own side outward: an outside
      // ring (offset > 0) sits on the parent, an inset ring on the element itself.
      const backdrop = (start) => {
        const layers = [];
        for (
          let node = start;
          node && node !== document.documentElement;
          node = node.parentElement
        ) {
          const p = parse(getComputedStyle(node).backgroundColor);
          if (p && p.a > 0) layers.push(p);
          if (p && p.a >= 1) break;
        }
        let out = paperOf();
        for (let i = layers.length - 1; i >= 0; i--)
          out = layers[i].rgb.map((v, j) =>
            Math.round(v * layers[i].a + out[j] * (1 - layers[i].a))
          );
        return out;
      };
      const ringOf = (node) => {
        const s = getComputedStyle(node);
        if (s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0)
          return {
            kind: "outline",
            color: parse(s.outlineColor),
            outside: parseFloat(s.outlineOffset) >= 0,
          };
        if (s.boxShadow && s.boxShadow !== "none")
          return {
            kind: "box-shadow",
            color: parse(s.boxShadow.match(/(rgba?|color)\([^)]*\)/)?.[0]),
            outside: !/inset/.test(s.boxShadow),
          };
        return null;
      };
      // A control may draw its ring on a wrapper via :focus-within (the jump box):
      // then the wrapper's ring is the one the user sees.
      let host = el,
        ring = ringOf(el);
      for (
        let p = el.parentElement, i = 0;
        !ring && p && i < 3;
        p = p.parentElement, i++
      ) {
        if (p.matches(":focus-within")) {
          const r = ringOf(p);
          if (r) {
            host = p;
            ring = r;
          }
        }
      }
      const bg = backdrop(ring?.outside === false ? host : host.parentElement);
      return {
        at: `${el.tagName}.${String(el.className || "").split(" ")[0]}${host !== el ? ` (环在 ${host.tagName}.${String(host.className || "").split(" ")[0]})` : ""}`,
        ringStyle: ring?.kind ?? "none",
        color: ring?.color ? `rgb(${ring.color.rgb.join(",")})` : null,
        backdrop: `rgb(${bg.join(",")})`,
        contrast: ring?.color ? ratio(ring.color.rgb, bg) : null,
      };
    });
    if (info) out.push(info);
  }
  return out;
}

// ---------------------------------------------------------------- the run
/** Run H1-H9 against c.BASE. `binding` = "candidate" checks H1, "skip" records it skipped. */
export async function runHonesty(c, { fixtures, binding = "candidate" }) {
  const { fail, rows } = c;
  const report = { binding: null, surfaces: [], share: [] };

  // H1
  if (binding === "skip")
    rows.push({
      label: "honesty/H1",
      skipped: "候选绑定按 --binding skip 跳过（不是通过）",
    });
  else {
    report.binding = await candidateBinding(
      c.BASE,
      c.build ?? paths.candidateDir,
      c.role ?? null
    );
    rows.push({ label: "honesty/H1", binding: report.binding });
    if (!report.binding.ok)
      fail(
        "H1",
        `候选未绑定最终源码：${report.binding.why ?? JSON.stringify({ hash: report.binding.sourceHash, added: report.binding.added, removed: report.binding.removed, mismatched: report.binding.mismatched, unlisted: report.binding.unlisted, byteMismatch: report.binding.byteMismatch, served: report.binding.served.error })}——按最终源码重建候选（npx tsc --noEmit && npx vite build --mode ${c.role ? `candidate-${c.role}` : "candidate"}）再验`
      );
  }

  // H2 — since the 2026-10-31 redesign (ADR 0001) the entry is shared: it
  // installs the role gate and the appearance, then loads the role's console;
  // the local console still mounts the Workbench (production and
  // `?console=workbench`). The backdrop levels are retired (Plan-Revision 2 #7):
  // the ground is static, the preferences that remain are wired.
  try {
    const read = (path) => readFileSync(resolve(paths.console, path), "utf8");
    const html = read("index.html");
    const entry = read("src/console-entry.ts");
    const local = read("src/local/local-console.ts");
    const appearance = read("src/shell/appearance.ts");
    if (!html.includes("/src/console-entry.ts"))
      fail("H2", "页面入口不是当前控制台");
    if (!/installRoleGate\(/.test(entry) || !/initAppearance\(/.test(entry))
      fail("H2", "控制台入口没有先装角色门禁与外观偏好");
    if (!/import\(["']\.\/local\/local-console["']\)/.test(entry))
      fail("H2", "控制台入口没有按角色装入本地控制台");
    if (!/new Workbench\(/.test(local))
      fail("H2", "本地控制台未装配 Workbench");
    if (
      [entry, local].some((src) =>
        /ResearchBackground|from ["'][^"']*(?:scene|wallpaper|main)["']/.test(
          src
        )
      )
    )
      fail("H2", "控制台入口重新引入已退役的背景或壁纸分支");
    if (!appearance.includes("delete root.dataset.consoleBackdrop"))
      fail("H2", "已退役的背景档没有停用");
    if (
      !appearance.includes("root.dataset.consoleTransparency =") ||
      !appearance.includes("root.dataset.consoleMotion =")
    )
      fail("H2", "透明度或动效偏好没有接线");
  } catch (e) {
    fail(
      "H2",
      `控制台入口不可读（${String(e.message).slice(0, 80)}）——该行判据不成立`
    );
  }

  const summary = fixtures["/api/summary"]?.body;
  const partial = summary ? partialSummary(summary) : null;
  if (!partial)
    fail(
      "H4",
      "夹具 summary 里没有 chat2api 链，建不起下界情景——该行判据不成立"
    );
  const expectedDays = partial ? nullPartialDays(partial) : [];

  for (const vp of c.VIEWPORTS) {
    for (const theme of c.THEMES) {
      const tag = `${vp.tag}/${theme}`;
      // ---- live: every lane, generic criteria
      {
        const { ctx, page, errors } = await c.newPage({
          width: vp.width,
          height: vp.height,
          dsf: vp.dsf,
          theme,
        });
        const s0 = await page.evaluate(readSurface, { foldHint: FOLD_HINT });
        if (s0.banner)
          fail(`${tag}/live`, `H3 读取成功时不应有首层横幅：${s0.banner}`);
        for (const L of c.LANES) {
          await c.gotoLane(page, L.lane);
          if (L.key === "overview") {
            // Exercise the series picker (and its repaint) before reading tooltips;
            // a disabled toggle (source without data or colour) is not clickable.
            const enabled = page.locator(
              "[data-wb-series-toggle]:not([disabled])"
            );
            const toggles = await enabled.count();
            for (let t = 0; t < Math.min(toggles, 3); t++) {
              await enabled.nth(t).click();
              await page.waitForTimeout(500);
            }
          }
          // R3: the share lane now renders three tabs. Each tab is entered with
          // a real click so its own folds, folds' truncation and focus stops are
          // measured rather than the hidden default panel standing in for all.
          const shareTabs =
            L.lane === 6 ? ["keys", "diagnostics", "usage"] : [null];
          for (const shareTab of shareTabs) {
            if (shareTab) await openShareTab(page, shareTab);
            const label = `${tag}/live/${L.key}${shareTab ? `/${shareTab}` : ""}`;
            const s = await page.evaluate(readSurface, { foldHint: FOLD_HINT });
            if (TIP_LANES.has(L.key) && s.tipCount === 0)
              fail(
                label,
                "H7 该页应有图表提示却一个也没找到——判据空转，不算通过"
              );
            if (s.tipOverflow.length)
              fail(
                label,
                `H7 ${s.tipOverflow.length} 个提示底板窄于文字（最差 ${Math.max(...s.tipOverflow)}px）`
              );
            if (s.ariaMismatch)
              fail(
                label,
                `H7 ${s.ariaMismatch} 个 hotspot 的可访问名不含自身提示文本`
              );
            for (const f of s.folds) {
              if (!f.chevron)
                fail(label, `H6 折叠入口「${f.text}」没有 chevron 标记`);
              if (f.cursor !== "pointer")
                fail(label, `H6 折叠入口「${f.text}」指针为 ${f.cursor}`);
              if (!f.hint)
                fail(
                  label,
                  `H6 折叠入口「${f.text}」没有写明「展开 / 收起」（行内或提示）`
                );
              if (f.width < MIN_FOLD_HOST_WIDTH)
                fail(
                  label,
                  `H6 折叠入口「${f.text}」被挤成 ${f.width}px 窄列（< ${MIN_FOLD_HOST_WIDTH}px）`
                );
            }
            for (const a of s.inlineActions) {
              if (a.cursor !== "pointer")
                fail(label, `H6 行内操作「${a.text}」指针为 ${a.cursor}`);
              if (a.width < 24)
                fail(
                  label,
                  `H6 行内操作「${a.text}」被挤成 ${a.width}px（< 24px 控件目标）`
                );
            }
            const flip = await foldFlipProbe(page);
            if (flip.kind === "checked") {
              for (const i of flip.inputs) {
                if (!i.toggle)
                  fail(
                    label,
                    `H6 ${i.name}没有切换折叠「${flip.text}」${i.hit ? `（命中 ${i.hit}）` : ""}`
                  );
                else if (i.pre !== null && i.toggle.open === i.pre)
                  fail(
                    label,
                    `H6 ${i.name}没有切换折叠「${flip.text}」（输入前 open=${i.pre}）`
                  );
              }
              if (
                flip.openAngle === null ||
                flip.closedAngle === null ||
                Math.abs(flip.openAngle - flip.closedAngle) < 45
              )
                fail(
                  label,
                  `H6 折叠「${flip.text}」的标记开合没有翻向（关闭 ${flip.closedAngle}° / 打开 ${flip.openAngle}°）`
                );
            }
            const opened = await page.evaluate(() => {
              const ds = [
                ...document.querySelectorAll(".workbench details"),
              ].filter((d) => !d.open && d.offsetWidth > 0);
              ds.forEach((d) => {
                d.open = true;
              });
              return ds.length;
            });
            const trunc = opened
              ? await page.evaluate(readSurface, { foldHint: FOLD_HINT })
              : s;
            if (opened)
              await page.evaluate(() =>
                document
                  .querySelectorAll(".workbench details[open]")
                  .forEach((d) => {
                    d.open = false;
                  })
              );
            if (trunc.truncated.length)
              fail(
                label,
                `H6 展开的折叠体里 ${trunc.truncated.length} 处文字被省略号截断（${trunc.truncated.slice(0, 3).join(" / ")}）`
              );
            const td =
              L.lane === 6
                ? shareTab === "usage"
                  ? await foldHostEdgeHits(page)
                  : { kind: "none", problems: [] }
                : await foldHostEdgeHits(page);
            for (const p of td.problems ?? [])
              fail(
                label,
                `H6 表格折叠「${td.text}」宿主单元格命中盒未覆盖整格：${p}`
              );
            if (
              vp === c.VIEWPORTS[0] &&
              (L.lane !== 6 || shareTab === "diagnostics")
            ) {
              const sw = await foldSurvivesLaneSwitch(c, page, L.lane);
              if (sw?.problem) fail(label, `H6 ${sw.problem}`);
            }
            const focus = [
              ...(L.key === "overview" ? await focusRing(page, "body") : []),
              ...(await focusRing(page)),
            ];
            if (!focus.length)
              fail(
                label,
                "H8 内容区里一个 Tab 焦点件也没找到——判据空转，不算通过"
              );
            const dim = focus.filter(
              (f) => f.contrast === null || f.contrast < FOCUS_MIN_CONTRAST
            );
            if (dim.length)
              fail(
                label,
                `H8 ${dim.length}/${focus.length} 个焦点件的环对周围底色对比 < ${FOCUS_MIN_CONTRAST}:1（${dim
                  .slice(0, 3)
                  .map(
                    (f) =>
                      `${f.at} ${f.ringStyle} ${f.color ?? "无环"} 对 ${f.backdrop} = ${f.contrast}`
                  )
                  .join("；")}）`
              );
            report.surfaces.push({
              label,
              tips: s.tipCount,
              folds: s.folds.length,
              inlineActions: s.inlineActions.length,
              flip: flip.kind,
              tdFold: td.kind,
              focusStops: focus.length,
            });
          }
        }
        const passFolds = report.surfaces
          .filter((r) => r.label.startsWith(`${tag}/live/`))
          .reduce((n, r) => n + r.folds, 0);
        if (!passFolds)
          fail(
            `${tag}/live`,
            "H6 整轮各页一个折叠也没找到——判据空转，不算通过"
          );
        if (errors.length)
          fail(
            `${tag}/live`,
            `H9 页面报错：${[...new Set(errors)].slice(0, 3).join(" | ")}`
          );
        await ctx.close();
      }
      // ---- summary read failed (500)
      {
        const { ctx, page, errors } = await c.newPage({
          width: vp.width,
          height: vp.height,
          dsf: vp.dsf,
          theme,
          override: async (route, req) => {
            if (!req.url().includes("/api/summary")) return false;
            await route.fulfill({
              status: 500,
              contentType: "application/json",
              body: JSON.stringify({ error: "summary unavailable" }),
            });
            return true;
          },
        });
        await page
          .waitForFunction(
            () =>
              /读取失败/.test(
                document.querySelector("[data-wb-summary-note]")?.textContent ||
                  ""
              ),
            null,
            { timeout: 15000 }
          )
          .catch(() => {});
        const s = await page.evaluate(readSurface, { foldHint: FOLD_HINT });
        const label = `${tag}/error/overview`;
        if (!s.banner)
          fail(label, "H3 注入 500 后首层横幅未出现（读取失败必须在首层可见）");
        if (!s.summaryNote || !s.summaryNote.includes("HTTP 500"))
          fail(
            label,
            `H3 车道正文未带实测 HTTP 状态：${s.summaryNote ?? "无正文"}`
          );
        if (s.banner && s.banner.includes("不可达"))
          fail(label, `H3 8001 应答了 500，横幅却写「不可达」：${s.banner}`);
        if (errors.length)
          fail(
            label,
            `H9 页面报错：${[...new Set(errors)].slice(0, 3).join(" | ")}`
          );
        await ctx.close();
      }
      // ---- lower-bound costs
      if (partial) {
        const { ctx, page, errors } = await c.newPage({
          width: vp.width,
          height: vp.height,
          dsf: vp.dsf,
          theme,
          override: async (route, req) => {
            if (!req.url().includes("/api/summary")) return false;
            await route.fulfill({
              status: 200,
              contentType: "application/json",
              body: JSON.stringify(partial),
            });
            return true;
          },
        });
        for (const lane of [0, 3]) {
          await c.gotoLane(page, lane);
          const s = await page.evaluate(readSurface, { foldHint: FOLD_HINT });
          const label = `${tag}/partial/${lane === 0 ? "overview" : "cost"}`;
          for (const cell of s.costCells.filter((x) => x.state === "partial")) {
            if (cell.valueText === null) {
              fail(
                label,
                'H4 partial 状态格没有读数元素（[data-kpi="cost"] 缺失）——该行判据不成立'
              );
              continue;
            }
            if (!LONG_AMOUNT_RE.test(cell.valueText)) {
              fail(
                label,
                `H4 长金额情景未生效：读数「${cell.valueText}」不匹配 ${LONG_AMOUNT_RE}`
              );
              continue;
            }
            if (cell.markerText === null)
              fail(
                label,
                `H4 partial 读数「${cell.valueText}」右侧没有标记元素`
              );
            else if (cell.gap === null || cell.gap < 0 || cell.gap > 8)
              fail(
                label,
                `H4 下界标记不在读数右侧 [0,8]px：「${cell.markerText}」间距 ${cell.gap}px`
              );
          }
          if (!s.costCells.some((x) => x.state === "partial"))
            fail(
              label,
              `H4 下界情景下没有 partial 状态格（${JSON.stringify(s.costCells.map((x) => x.state))}）——该行判据不成立`
            );
          for (const row of s.partialRows)
            if (row.gap === null || row.gap < 0 || row.gap > 8)
              fail(
                label,
                `H4 partial 表格格的「≥」未贴住读数：${row.cell}（间距 ${row.gap}px）`
              );
          if (lane !== 3) continue;
          if (!LOWER_BOUND_MARKERS.some((m) => s.firstLayerSample.includes(m)))
            fail(
              label,
              `H4 费用页首层没有下界标识（≥ / 已知部分 / 下界 / 只含可折算）：${s.firstLayerSample.slice(0, 120)}`
            );
          if (!expectedDays.length) {
            fail(
              label,
              "H4 日级 null+partial 夹具未落在近 14 天窗口内——该行判据不成立"
            );
            continue;
          }
          const chainRow =
            s.feeTableRows.find((t) => t.includes("chat2api")) ?? null;
          const ledgerRow =
            s.ledgerRows.find((t) => t.includes("估算费用")) ?? null;
          if (chainRow === null)
            fail(
              label,
              `H4 费用页找不到 chat2api 的链行——该行判据不成立：${s.feeTableRows.slice(0, 3).join(" ｜ ")}`
            );
          else if (!BOUND_TEXT.test(chainRow))
            fail(label, `H4 null+partial 链行没有下界口径：${chainRow}`);
          if (ledgerRow !== null && !BOUND_TEXT.test(ledgerRow))
            fail(label, `H4 双账台「估算费用」行没有下界口径：${ledgerRow}`);
          if (s.costTrendTips === null)
            fail(
              label,
              "H4 找不到费用趋势图（.wb-fee-trend ¥）——该行判据不成立"
            );
          else {
            const bad = expectedDays
              .map((day) => {
                const t = s.costTrendTips.find((x) => x.date === day);
                const v = t
                  ? (t.texts[1] ?? t.texts.join(" "))
                  : "（该日没有提示）";
                return { day, v, ok: BOUND_TEXT.test(v) };
              })
              .filter((x) => !x.ok);
            if (bad.length)
              fail(
                label,
                `H4 null+partial 日的趋势提示不是下界口径：${bad
                  .slice(0, 3)
                  .map((x) => `${x.day}=「${x.v}」`)
                  .join("；")}${bad.length > 3 ? ` …共 ${bad.length} 天` : ""}`
              );
          }
          const note = s.costNote ?? "";
          const boundDays = /下界日\s*([\d,]+)\s*天/.exec(note),
            unknownDays = /分量未知\s*([\d,]+)\s*天/.exec(note);
          if (
            !boundDays ||
            Number(boundDays[1].replace(/,/g, "")) !== expectedDays.length
          )
            fail(
              label,
              `H4 常驻说明应写「下界日 ${expectedDays.length} 天」，实写「${boundDays?.[0] ?? "（无）"}」：${note.slice(0, 120)}`
            );
          else if (
            unknownDays &&
            Number(unknownDays[1].replace(/,/g, "")) !== 0
          )
            fail(
              label,
              `H4 null+partial 被常驻说明记成「${unknownDays[0]}」，与链行不是同一解释`
            );
        }
        if (errors.length)
          fail(
            `${tag}/partial`,
            `H9 页面报错：${[...new Set(errors)].slice(0, 3).join(" | ")}`
          );
        await ctx.close();
      }
      // ---- share failure surface
      for (const sc of SHARE_FAIL_CASES) {
        const label = `${tag}/share ${sc.tag}`;
        const posted = [];
        const { ctx, page } = await c.newPage({
          width: vp.width,
          height: vp.height,
          dsf: vp.dsf,
          theme,
          nonGetAnswer: (req) => {
            if (
              sc.kind !== "action" ||
              !req.url().includes("/api/console/operations")
            )
              return null;
            posted.push(req.postData() || "");
            return { status: sc.http, body: sc.body };
          },
          override: async (route, req) => {
            const url = req.url();
            if (url.includes("/api/console/share-management/status")) {
              await route.fulfill(
                sc.kind === "read"
                  ? {
                      status: sc.http,
                      contentType: "application/json",
                      body: JSON.stringify(sc.body),
                    }
                  : {
                      status: 200,
                      contentType: "application/json",
                      body: JSON.stringify(closedShareStatus(fixtures)),
                    }
              );
              return true;
            }
            if (sc.extraOps && url.includes("/api/console/operations")) {
              await route.fulfill({
                status: sc.http,
                contentType: "application/json",
                body: JSON.stringify({ error: "operations_read_failed" }),
              });
              return true;
            }
            return false;
          },
        });
        await c.gotoLane(page, 6);
        if (sc.kind === "action") {
          const armed = await page.evaluate(() => {
            const b = document.querySelector(
              '[data-wb-share-action="share.start"]'
            );
            if (!b) return null;
            b.click();
            b.click();
            return true;
          });
          if (!armed) {
            fail(
              label,
              "H5 分享状态注入后没有「开启分享」入口——该行判据不成立"
            );
            await ctx.close();
            continue;
          }
        }
        await page
          .waitForFunction(
            (t) => document.body.innerText.includes(t),
            sc.token,
            { timeout: 10000 }
          )
          .catch(() => {});
        // R3: the measured status failure stays in the overview, but the
        // independent operations read failure and the capacity block live in the
        // diagnostics tab. Enter it so the whole-page carrier count includes
        // both the share-status failure and the unrelated read.
        await openShareTab(page, "diagnostics");
        await page.waitForTimeout(500);
        const r = await page.evaluate((token) => {
          const own = (el) =>
            [...el.childNodes]
              .filter((n) => n.nodeType === 3)
              .map((n) => n.textContent || "")
              .join("");
          const carriers = [];
          for (const el of document.querySelectorAll("*")) {
            const t = own(el);
            if (!t.includes(token)) continue;
            const flat = t.replace(/\s+/g, " ").trim();
            carriers.push({
              at: `${el.tagName}.${String(el.className || "").split(" ")[0]}`,
              count: t.split(token).length - 1,
              text: flat.slice(0, 90),
              share: !!el.closest(".wb-notice") || flat.includes("分享"),
            });
          }
          return {
            carriers,
            whole: document.body.innerText.split(token).length - 1,
          };
        }, sc.token);
        await ctx.close();
        const surface = r.carriers.filter((x) => x.share);
        const total = surface.reduce((n, x) => n + x.count, 0);
        report.share.push({
          label,
          total,
          whole: r.whole,
          carriers: surface.map((x) => x.text),
        });
        if (total !== 1)
          fail(
            label,
            `H5 分享失败面上的「${sc.token}」出现 ${total} 次（应只出现一次）：${surface.map((x) => `${x.at}×${x.count}「${x.text}」`).join("；") || "无"}`
          );
        const other = (
          surface
            .map((x) => x.text)
            .join(" ｜ ")
            .match(sc.other) ?? []
        ).length;
        if (other)
          fail(
            label,
            `H5 后端给了${sc.tag}，分享失败面上却出现另一种状态：${surface.map((x) => x.text).join(" ｜ ")}`
          );
        if (sc.proveWholePage && r.whole <= 1)
          fail(
            label,
            `H5 无关读口没有印出同一状态（整页计数 ${r.whole}），证明不了按承载者计数——该行判据不成立`
          );
        if (sc.kind === "action" && !posted.some((b) => b.includes("share.")))
          fail(
            label,
            `H5 本地没有截到分享动作请求体——该行判据不成立：${JSON.stringify(posted)}`
          );
      }
    }
  }
  rows.push({
    label: "honesty/surfaces",
    surfaces: report.surfaces,
    share: report.share,
    expectedNullPartialDays: expectedDays.length,
  });
  return report;
}
