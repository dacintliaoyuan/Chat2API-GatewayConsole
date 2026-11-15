import "./um.css";
import type { RoleGate, RoleVerdict } from "../../console/role-gate";
import { reduceMotion } from "../../shell/appearance";
import type {
  Load,
  LogLevel,
  LogQuery,
  MaintenanceAction,
  RequestQuery,
  RequestsView,
  TrendRange,
  UmConsoleHandle,
  UmEvent,
  UmPage,
  UmViewState,
} from "./contract";
import { SeriesColors, TrendView, type Metric } from "./charts";
import { esc, failureText, loaded, when } from "./format";
import {
  CONFIRM,
  maintenanceMenuHtml,
  overviewHtml,
  type OverviewUi,
} from "./overview";
import { requestsHtml, VIEWS, type RequestsUi } from "./requests";
import {
  catalogIds,
  modelsHtml,
  refreshBlockReason,
  stopBlockReason,
  testBlockReason,
  type ModelsUi,
} from "./models";

/** The local UM console (handoff r3): 总览 / 请求与诊断 / 模型与设置 in the
 *  shared shell. It renders the UmViewState it is handed and reports every
 *  intent as a UmEvent; it never fetches. View-only state (the page, the
 *  chosen chart metric, open rows, the model selection) lives here. */

export type PreviewScenario = { id: string; label: string };
export type UmConsoleOptions = {
  onEvent: (event: UmEvent) => void;
  /** The role gate's verdict paints the sidebar marker and notice (live mode). */
  gate: RoleGate;
  /** Candidate builds: the offline preview's address, offered while unwired. */
  previewHref: string | null;
  /** Preview mode: the synthetic scenarios and the current one. */
  scenarios?: {
    list: readonly PreviewScenario[];
    current: string;
    href: (id: string | null) => string;
  };
};

const PAGES: readonly { id: UmPage; label: string; icon: string }[] = [
  {
    id: "overview",
    label: "总览",
    icon: `<rect x="3.5" y="3.5" width="7" height="7" rx="2"/><rect x="13.5" y="3.5" width="7" height="7" rx="2"/><rect x="3.5" y="13.5" width="7" height="7" rx="2"/><rect x="13.5" y="13.5" width="7" height="7" rx="2"/>`,
  },
  {
    id: "requests",
    label: "请求与诊断",
    icon: `<path d="M3 12h4l3-7 4 14 3-7h4"/>`,
  },
  {
    id: "models",
    label: "模型与设置",
    icon: `<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>`,
  },
];
const GEAR = `<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>`;

type Route = { page: UmPage; view: RequestsView };
function routeFromHash(): Route {
  const [page, view] = location.hash.replace(/^#/, "").split("/");
  const known = PAGES.some((p) => p.id === page)
    ? (page as UmPage)
    : "overview";
  return {
    page: known,
    view: VIEWS.some((v) => v.id === view) ? (view as RequestsView) : "records",
  };
}

/** Every main read is unwired: the real console before a controller exists. */
function unwired(state: UmViewState): boolean {
  const loads: Load<unknown>[] = [
    state.overview.service,
    state.overview.today,
    state.overview.trend,
    state.requests.page,
    state.models.catalog,
  ];
  return state.mode === "live" && loads.every((l) => l.state === "unwired");
}

const sameQuery = (a: RequestQuery, b: RequestQuery) =>
  a.window === b.window &&
  a.model === b.model &&
  a.result === b.result &&
  a.search === b.search;
const sameLogQuery = (a: LogQuery, b: LogQuery) =>
  a.window === b.window &&
  a.source === b.source &&
  a.levels.length === b.levels.length &&
  a.levels.every((l) => b.levels.includes(l));

const EARLIER_EVENT = {
  records: "requests.earlier",
  logs: "logs.earlier",
  maintenance: "maintenance.earlier",
} as const;
/** The read section a diagnostics view pages through with 「加载更早」. */
function earlierLoad(
  state: UmViewState,
  view: RequestsView
): Load<{ earlier: string | null }> {
  return view === "records"
    ? state.requests.page
    : view === "logs"
      ? state.requests.logs.page
      : state.requests.maintenance;
}
/** A section's failure by value: a controller may keep the old object and change it in place. */
function failureKey(load: Load<unknown>): string | null {
  return load.state === "stale"
    ? `${load.failure.kind}|${load.failure.at}|${load.failure.reason}`
    : null;
}

export function mountUmConsole(
  stage: HTMLElement,
  initial: UmViewState,
  options: UmConsoleOptions
): UmConsoleHandle {
  let state = initial;
  let route = routeFromHash();
  const colors = new SeriesColors();
  const chart = new TrendView(colors);
  const overviewUi: OverviewUi = { metric: "tokens", line: false };
  const requestsUi = {
    view: route.view,
    search: initial.requests.query.search,
    expanded: null as string | null,
    openSteps: new Set<string>(),
    detailAsked: null as string | null,
    earlier: { records: null, logs: null, maintenance: null } as Record<
      RequestsView,
      string | null
    >,
    earlierFailed: { records: null, logs: null, maintenance: null } as Record<
      RequestsView,
      { cursor: string; reason: string } | null
    >,
  };
  // Per view, the 「加载更早」 read that is out: the page's failure when it was
  // asked, and whether the controller was seen reading it.
  const earlierAsks: Record<
    RequestsView,
    { failure: string | null; busy: boolean } | null
  > = { records: null, logs: null, maintenance: null };
  const modelsUi = {
    selected: new Set<string>(),
    expanded: new Set<string>(),
    runDetail: new Set<string>(),
    historyOpen: false,
    settingsOpen: false,
    awaiting: false,
  };
  let lastHtml = "";
  let lastMenuHtml = "";
  /** TrendView.signature of what the SVG shows now. */
  let paintedSignature: string | null = null;
  let menuOpen = false;
  /** The maintenance action whose confirm dialog is open. */
  let confirming: MaintenanceAction | null = null;
  // Filters build on what was last asked, not on the controller's last answer:
  // with an async controller, two quick changes must not lose the first.
  let pendingQuery: RequestQuery | null = null;
  let pendingLogQuery: LogQuery | null = null;
  // What the last state said, by value: a controller may hand over the same
  // nested objects mutated in place (the preview does), so the previous state
  // object cannot be compared with the next one.
  const seenOf = (s: UmViewState) => ({
    runId: s.models.run?.id ?? null,
    noticeAt: s.models.notice?.at ?? null,
    context: s.models.actions.context.enabled,
  });
  let seen = seenOf(initial);
  const emit = (event: UmEvent) => options.onEvent(event);

  stage.insertAdjacentHTML(
    "beforeend",
    `<section class="app-shell um-console" aria-label="本机 UM 控制台">
    <aside class="shell-sidebar" aria-label="UM 控制台导航">
      <div class="shell-side-panel shell-glass">
        <div class="shell-env" data-role-state="checking"><div class="shell-env-name"><span class="shell-env-mark" aria-hidden="true"></span><span>本机 · UM 控制台</span></div><div class="shell-env-sub" data-shell-role-text></div></div>
        <nav class="shell-nav" aria-label="UM 控制台导航">${PAGES.map((p) => `<button type="button" data-um-page="${p.id}" aria-label="${esc(p.label)}"><svg viewBox="0 0 24 24" aria-hidden="true">${p.icon}</svg><span class="shell-nav-text">${esc(p.label)}</span><span class="shell-hint" aria-hidden="true">${esc(p.label)}</span></button>`).join("")}</nav>
        <div class="shell-side-foot">
          <button type="button" class="shell-icon-btn" data-action="settings" aria-label="外观设置"><svg viewBox="0 0 24 24" aria-hidden="true">${GEAR}</svg><span class="shell-hint" aria-hidden="true">外观设置</span></button>
          <span class="shell-side-text">外观</span>
          <button type="button" class="shell-icon-btn" data-action="rail-toggle" aria-label="收起侧栏" aria-expanded="true"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="3"/><path d="M9.5 4.5v15M15.5 10l-2 2 2 2"/></svg><span class="shell-hint" aria-hidden="true">收起侧栏</span></button>
        </div>
      </div>
    </aside>
    <main class="shell-main">
      <div class="shell-page">
        <header class="shell-toolbar">
          <div class="shell-title"><h1 data-um-title></h1><p data-um-subtitle></p></div>
          <div class="shell-capsules"><div class="shell-capsule shell-glass um-capsule">
            <div class="um-menu-wrap"><button type="button" data-um-menu aria-haspopup="menu" aria-expanded="false" aria-controls="um-maint-menu"><svg class="shell-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3.5 17.5v3h3l5.8-5.8a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z"/></svg>维护<svg class="um-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10l5 5 5-5"/></svg></button>
              <div class="um-menu" id="um-maint-menu" role="menu" aria-label="UM 维护" hidden></div></div>
            <span class="shell-capsule-sep" aria-hidden="true"></span>
            <button type="button" data-um-refresh><svg class="shell-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.6-5.9"/><path d="M20 4.5V9h-4.5"/></svg>刷新</button>
          </div></div>
        </header>
        <div class="shell-role-notice" data-shell-role-notice role="status" hidden></div>
        <div class="um-mode" data-um-mode hidden></div>
        <div class="um-content"></div>
      </div>
    </main>
  </section>
  <div class="um-tip" role="tooltip" hidden></div>
  <dialog class="shell-confirm" aria-labelledby="um-confirm-title"><form method="dialog"><h2 id="um-confirm-title"></h2><p class="shell-confirm-body"></p><p class="shell-confirm-target"></p><div class="shell-confirm-actions"><button value="cancel" class="btn btn-outline">取消</button><button value="confirm" class="btn btn-primary"></button></div></form></dialog>`
  );

  const root = stage.querySelector<HTMLElement>(".um-console")!;
  const content = root.querySelector<HTMLElement>(".um-content")!;
  const main = root.querySelector<HTMLElement>(".shell-main")!;
  const menu = root.querySelector<HTMLElement>(".um-menu")!;
  const menuButton = root.querySelector<HTMLButtonElement>("[data-um-menu]")!;
  const tip = stage.querySelector<HTMLElement>(".um-tip")!;
  const confirmDialog =
    stage.querySelector<HTMLDialogElement>(".shell-confirm")!;
  const resize = new ResizeObserver(() => paintChart());

  // ------------------------------------------------------------ painting

  function paintRole(): void {
    const verdict: RoleVerdict = options.gate.verdict();
    const env = root.querySelector<HTMLElement>(".shell-env")!;
    const sub = root.querySelector<HTMLElement>("[data-shell-role-text]")!;
    const notice = root.querySelector<HTMLElement>("[data-shell-role-notice]")!;
    const live = state.mode === "live" && !unwired(state);
    env.dataset.roleState =
      state.mode === "preview" ? "preview" : live ? verdict.state : "unwired";
    sub.textContent =
      state.mode === "preview"
        ? "离线预览 · 不连接后端"
        : !live
          ? "数据未接线 · 操作关闭"
          : verdict.state === "confirmed"
            ? "管理本机 UM · 后端角色已确认"
            : verdict.state === "checking"
              ? "管理本机 UM · 正在核对后端角色"
              : "后端角色未确认 · 只读";
    notice.hidden =
      !live || verdict.state === "confirmed" || verdict.state === "checking";
    notice.dataset.state = verdict.state;
    notice.innerHTML = notice.hidden
      ? ""
      : `<div><strong>${verdict.state === "mismatch" ? "后端与本页角色不符，整页只读" : "后端角色未确认，写操作已关闭"}</strong><p>${esc(verdict.reason)}</p></div>`;
  }

  function paintMode(): void {
    const box = root.querySelector<HTMLElement>("[data-um-mode]")!;
    if (state.mode === "preview" && options.scenarios) {
      const s = options.scenarios;
      box.hidden = false;
      box.dataset.mode = "preview";
      box.innerHTML = `<div class="um-mode-text"><strong>离线预览 · 合成数据</strong><p>数字、模型和状态都是为检查界面编造的，不是本机 UM 的真实情况；本页的操作只在浏览器里模拟，不发出任何请求。</p></div>
        <nav class="um-mode-nav" aria-label="预览情景"><span class="um-mode-label">情景</span>${s.list.map((x) => `<a class="chip" href="${esc(s.href(x.id))}" aria-current="${x.id === s.current ? "page" : "false"}"${x.id === s.current ? ` aria-pressed="true"` : ""}>${esc(x.label)}</a>`).join("")}<a class="um-link" href="${esc(s.href(null))}">退出预览</a></nav>`;
    } else if (unwired(state)) {
      box.hidden = false;
      box.dataset.mode = "unwired";
      box.innerHTML = `<div class="um-mode-text"><strong>数据尚未接线</strong><p>本页只是界面：UM 的读数、任务和操作由数据控制器（上云主任务 B6）接入。接入前各区显示「未接线」，操作保持关闭，不显示任何猜测的数字。</p></div>${options.previewHref ? `<nav class="um-mode-nav"><a class="btn btn-sm btn-tonal" href="${esc(options.previewHref)}">查看离线预览（合成数据）</a></nav>` : ""}`;
    } else {
      box.hidden = true;
      box.innerHTML = "";
    }
  }

  function subtitle(): string {
    if (state.mode === "preview")
      return "本机 UM · 离线预览（合成数据）· 北京时间";
    if (unwired(state)) return "本机 UM · 数据未接线";
    const loads: Load<unknown>[] =
      route.page === "overview"
        ? [
            state.overview.service,
            state.overview.login,
            state.overview.today,
            state.overview.trend,
          ]
        : route.page === "requests"
          ? [
              state.requests.page,
              state.requests.logs.page,
              state.requests.maintenance,
            ]
          : [state.models.access, state.models.catalog];
    const newest = Math.max(0, ...loads.map((l) => loaded(l)?.at ?? 0));
    const failed = loads.filter(
      (l) => l.state === "failed" || l.state === "stale"
    ).length;
    return `本机 UM · 北京时间${newest ? ` · 最近更新 ${when(newest)}` : ""}${failed ? ` · ${failed} 项读取未成功` : ""}`;
  }

  function pageHtml(): string {
    if (route.page === "overview")
      return overviewHtml(state.overview, overviewUi, chart);
    if (route.page === "requests")
      return requestsHtml(state.requests, requestsUi as RequestsUi);
    return modelsHtml(state.models, modelsUi as ModelsUi);
  }

  /** Shown instead of the page when the state cannot be drawn: never the
   *  previous page's figures under a newer state. */
  function renderFailureHtml(error: unknown): string {
    const reason = String((error as Error)?.message ?? error).slice(0, 200);
    return `<div class="um-page"><section class="card um-render-error" role="alert"><h2>这一页无法显示当前数据</h2>
      <p>数据控制器送来的状态里有界面画不出的值（${esc(reason)}）。为免误导，这里不再显示之前的数字；刷新后仍出现时，请在控制器一侧检查这份状态。</p></section></div>`;
  }

  function render(): void {
    const meta = PAGES.find((p) => p.id === route.page)!;
    root.querySelectorAll<HTMLButtonElement>("[data-um-page]").forEach((b) => {
      if (b.dataset.umPage === route.page)
        b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    root.querySelector("[data-um-title]")!.textContent = meta.label;
    let html: string;
    try {
      root.querySelector("[data-um-subtitle]")!.textContent = subtitle();
      paintRole();
      paintMode();
      if (menuOpen) paintMenu();
      html = pageHtml();
    } catch (error) {
      console.error("UM console: cannot render the current state", error);
      chart.trend = null;
      html = renderFailureHtml(error);
    }
    if (html === lastHtml) {
      // Same markup, but the figures may still have moved (the SVG is not part
      // of it), possibly inside the very same trend object.
      if (chart.trend && chart.signature !== paintedSignature) paintChart();
      return;
    }
    // Keep the reader's place across a rebuild: the focused control is found
    // again by its data attribute, an input keeps its caret.
    const active =
      document.activeElement instanceof HTMLElement &&
      content.contains(document.activeElement)
        ? document.activeElement
        : null;
    const key = active ? focusKey(active) : null;
    const caret =
      active instanceof HTMLInputElement && active.type === "search"
        ? ([active.selectionStart, active.selectionEnd] as const)
        : null;
    content.innerHTML = html;
    lastHtml = html;
    if (key) {
      const again = content.querySelector<HTMLElement>(key);
      again?.focus({ preventScroll: true });
      if (caret && again instanceof HTMLInputElement)
        again.setSelectionRange(caret[0], caret[1]);
    }
    // A rebuilt ranking keeps the link only if the focused row came back; a row
    // that vanished must not leave the chart dimmed.
    if (
      chart.focusSeries !== null &&
      !content.querySelector(".um-rank-row.is-linked")
    )
      chart.focusSeries = null;
    resize.disconnect();
    const host = content.querySelector<HTMLElement>("[data-um-chart]");
    if (host) {
      resize.observe(host);
      paintChart();
    } else hideTip();
  }
  function focusKey(element: HTMLElement): string | null {
    for (const attr of Array.from(element.attributes)) {
      if (attr.name.startsWith("data-um-"))
        return `[${attr.name}="${CSS.escape(attr.value)}"]`;
    }
    const label = element.getAttribute("aria-label");
    return label ? `[aria-label="${CSS.escape(label)}"]` : null;
  }

  function paintChart(): void {
    const host = content.querySelector<HTMLElement>("[data-um-chart]");
    if (!host || !chart.trend) return;
    try {
      chart.paint(host);
    } catch (error) {
      console.error("UM console: cannot draw the trend", error);
      host.innerHTML = `<p class="um-placeholder" data-tone="err">趋势数据画不出来（${esc(String((error as Error)?.message ?? error).slice(0, 120))}）；右侧排行仍按同一时段列出。</p>`;
    }
    paintedSignature = chart.signature;
    if (chart.active !== null) showTip(host);
    else hideTip();
  }

  /** The open menu is rebuilt only when its content changed, and keeps the
   *  focused item: an update must not throw the keyboard out of the menu. */
  function paintMenu(): void {
    const html = maintenanceMenuHtml(state.overview);
    if (html === lastMenuHtml) return;
    const focused =
      (document.activeElement as HTMLElement | null)?.closest?.<HTMLElement>(
        ".um-menu [data-um-act]"
      )?.dataset.umAct ?? null;
    menu.innerHTML = html;
    lastMenuHtml = html;
    if (focused)
      menu
        .querySelector<HTMLElement>(`[data-um-act="${CSS.escape(focused)}"]`)
        ?.focus();
  }

  // ------------------------------------------------------------ tooltip

  function showTip(host: HTMLElement): void {
    const at = chart.anchor(host);
    if (!at || chart.active === null) {
      hideTip();
      return;
    }
    try {
      tip.innerHTML = chart.tipHtml(chart.active);
    } catch {
      hideTip();
      return;
    }
    tip.hidden = false;
    const w = tip.offsetWidth,
      h = tip.offsetHeight;
    // Keep the plate inside the plot's own box and beside the active column:
    // it never covers the y-axis labels, the day labels or the attached marker,
    // and never the ranking beside the chart.
    const box = host.getBoundingClientRect();
    const g = chart.geometry;
    const plotLeft = box.left + (g?.left ?? 0);
    const plotRight = box.right - 4;
    const plotTop = box.top + (g?.top ?? 0);
    const plotBottom = plotTop + (g?.plotH ?? box.height);
    const rightRoom = plotRight - (at.x + 14);
    const leftRoom = at.x - 14 - plotLeft;
    let left =
      rightRoom >= w ? at.x + 14 : leftRoom >= w ? at.x - 14 - w : at.x + 14;
    left = Math.max(plotLeft + 4, Math.min(plotRight - w, left));
    let top = at.y + 14;
    if (top + h > plotBottom - 4) top = at.y - h - 14;
    top = Math.max(plotTop + 4, Math.min(window.innerHeight - h - 8, top));
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, top)}px`;
  }
  function hideTip(): void {
    tip.hidden = true;
  }
  function setActive(
    host: HTMLElement,
    index: number | null,
    announce: boolean
  ): void {
    chart.active = index;
    chart.highlight(host);
    if (index === null) {
      hideTip();
      return;
    }
    showTip(host);
    if (announce) {
      const live = content.querySelector<HTMLElement>("[data-um-chart-live]");
      if (live) live.textContent = tip.textContent ?? "";
    }
  }
  /** Link a ranking row to the series it is drawn as on its own. A folded row
   *  (no data-um-series) and a lone value (not a drawn curve) link to nothing:
   *  the chart is not dimmed and the row is not presented as its own line. */
  function linkRank(row: HTMLElement | null): void {
    const host = content.querySelector<HTMLElement>("[data-um-chart]");
    const raw = row?.dataset.umSeries;
    const series = raw === undefined ? NaN : Number(raw);
    chart.focusSeries = Number.isInteger(series) && series >= 0 ? series : null;
    content
      .querySelectorAll(".um-rank-row.is-linked")
      .forEach((el) => el.classList.remove("is-linked"));
    if (row && chart.focusSeries !== null) row.classList.add("is-linked");
    if (host) chart.highlight(host);
  }

  // ------------------------------------------------------------ actions

  function navigate(page: UmPage, view?: RequestsView): void {
    const hash =
      page === "requests" ? `#requests/${view ?? requestsUi.view}` : `#${page}`;
    if (location.hash !== hash) location.hash = hash;
    else onRoute();
  }
  function onRoute(): void {
    const next = routeFromHash();
    const changed =
      next.page !== route.page ||
      (next.page === "requests" && next.view !== route.view);
    route = next;
    requestsUi.view = next.view;
    closeMenu(false);
    hideTip();
    chart.active = null;
    chart.focusSeries = null;
    render();
    if (changed) main.scrollTop = 0;
    emit({
      type: "page",
      page: route.page,
      view: route.page === "requests" ? route.view : null,
    });
  }

  /** Checked against the live state at every step: when the menu item is
   *  picked, and again when the confirm closes (the action may have closed or
   *  started elsewhere while the dialog was open). */
  function allowed(action: MaintenanceAction): boolean {
    return (
      state.overview.actions[action]?.enabled === true &&
      state.overview.progress[action]?.state !== "pending"
    );
  }

  function maintenance(action: MaintenanceAction): void {
    if (!allowed(action)) return;
    if (action === "verify") {
      emit({ type: "um.verify" });
      return;
    }
    const copy = CONFIRM[action];
    confirmDialog.querySelector("h2")!.textContent = copy.title;
    confirmDialog.querySelector(".shell-confirm-body")!.textContent = copy.body;
    confirmDialog.querySelector(".shell-confirm-target")!.textContent =
      state.mode === "preview"
        ? "离线预览：确认后只在本页模拟，不发出请求。"
        : "";
    const ok = confirmDialog.querySelector<HTMLButtonElement>(
      'button[value="confirm"]'
    )!;
    ok.textContent = copy.confirm;
    ok.className = copy.danger ? "btn btn-danger-fill" : "btn btn-primary";
    confirmDialog.returnValue = "";
    confirming = action;
    confirmDialog.onclose = () => {
      confirming = null;
      if (confirmDialog.returnValue !== "confirm" || !allowed(action)) return;
      emit(
        action === "login"
          ? { type: "um.login" }
          : { type: "um.service", action }
      );
    };
    confirmDialog.showModal();
    confirmDialog
      .querySelector<HTMLButtonElement>('button[value="cancel"]')!
      .focus();
  }

  function openMenu(): void {
    menuOpen = true;
    lastMenuHtml = maintenanceMenuHtml(state.overview);
    menu.innerHTML = lastMenuHtml;
    menu.hidden = false;
    menuButton.setAttribute("aria-expanded", "true");
    menu.querySelector<HTMLElement>("[role=menuitem]")?.focus();
  }
  function closeMenu(returnFocus: boolean): void {
    if (!menuOpen) return;
    menuOpen = false;
    menu.hidden = true;
    menuButton.setAttribute("aria-expanded", "false");
    if (returnFocus) menuButton.focus();
  }

  function query(patch: Partial<RequestQuery>): void {
    const base = pendingQuery ?? state.requests.query;
    const next = { ...base, ...patch };
    if (sameQuery(next, base)) return;
    pendingQuery = next;
    requestsUi.expanded = null;
    emit({ type: "requests.query", query: next });
  }
  function logQuery(patch: Partial<LogQuery>): void {
    const base = pendingLogQuery ?? state.requests.logs.query;
    const next = { ...base, ...patch };
    if (sameLogQuery(next, base)) return;
    pendingLogQuery = next;
    emit({ type: "logs.query", query: next });
  }
  /** One 「加载更早」 read per view at a time; once the controller answers it
   *  (see update), the same cursor may be asked again. */
  function earlier(view: RequestsView, cursor: string): void {
    if (requestsUi.earlier[view] !== null) return;
    requestsUi.earlier[view] = cursor;
    requestsUi.earlierFailed[view] = null;
    earlierAsks[view] = {
      failure: failureKey(earlierLoad(state, view)),
      busy: false,
    };
    render();
    emit({ type: EARLIER_EVENT[view], cursor });
  }
  function askDetail(key: string): void {
    requestsUi.detailAsked = key;
    emit({ type: "requests.detail", key });
  }

  async function copyUrl(button: HTMLButtonElement): Promise<void> {
    const url = loaded(state.models.access)?.data.baseUrl;
    if (!url) return;
    const original = button.textContent;
    try {
      await navigator.clipboard.writeText(url);
      button.textContent = "已复制";
    } catch {
      button.textContent = "浏览器拒绝写入剪贴板";
    }
    setTimeout(() => {
      if (button.isConnected) button.textContent = original;
    }, 2_400);
  }

  root.addEventListener("click", (event) => {
    const target = event.target as Element;
    // A closed control never acts, whatever reaches it: a click on a child of
    // a disabled button, or on a node left over from an older render.
    if (
      target.closest(
        "button:disabled, [aria-disabled='true']:not([role=menuitem])"
      )
    )
      return;
    const pick = (selector: string) => target.closest<HTMLElement>(selector);
    let el: HTMLElement | null;
    if ((el = pick("[data-um-page]"))) {
      navigate(el.dataset.umPage as UmPage);
      return;
    }
    if ((el = pick("[data-um-menu]"))) {
      if (menuOpen) closeMenu(true);
      else openMenu();
      return;
    }
    if ((el = pick(".um-menu [data-um-act]"))) {
      if (el.getAttribute("aria-disabled") === "true") return;
      closeMenu(true);
      maintenance(el.dataset.umAct as MaintenanceAction);
      return;
    }
    if (menuOpen && !pick(".um-menu-wrap")) closeMenu(false);
    if ((el = pick("[data-um-refresh]"))) {
      emit({
        type: "refresh",
        page: route.page,
        view: route.page === "requests" ? route.view : null,
      });
      return;
    }
    if ((el = pick("[data-um-act]"))) {
      maintenance(el.dataset.umAct as MaintenanceAction);
      return;
    }
    if ((el = pick("[data-um-go]"))) {
      const [page, view] = el.dataset.umGo!.split("/");
      navigate(page as UmPage, view as RequestsView | undefined);
      return;
    }
    if ((el = pick("[data-um-evidence]"))) {
      const issues = loaded(state.overview.issues)?.data.items;
      const evidence = issues?.[Number(el.dataset.umEvidence)]?.evidence;
      if (!evidence) return;
      if (evidence.view === "records" && evidence.query) query(evidence.query);
      navigate("requests", evidence.view);
      return;
    }
    // 总览
    if ((el = pick("[data-um-metric]"))) {
      // Keep the column the reader was on: clearing it here would let focusin
      // snap the tooltip back to the last day when the chart holds the focus.
      const keep = chart.active;
      overviewUi.metric = el.dataset.umMetric as Metric;
      chart.focusSeries = null;
      chart.active = null;
      render();
      if (keep !== null) {
        const host = content.querySelector<HTMLElement>("[data-um-chart]");
        if (host && document.activeElement === host && chart.trend)
          setActive(host, Math.min(keep, chart.trend.days.length - 1), true);
      }
      return;
    }
    if ((el = pick("[data-um-range]"))) {
      if (el.getAttribute("aria-pressed") !== "true")
        emit({ type: "trend.range", range: el.dataset.umRange as TrendRange });
      return;
    }
    if ((el = pick('[data-um-toggle="line"]'))) {
      if (overviewUi.metric !== "speed") {
        overviewUi.line = !overviewUi.line;
        render();
      }
      return;
    }
    // 请求与诊断
    if ((el = pick("[data-um-view]"))) {
      navigate("requests", el.dataset.umView as RequestsView);
      return;
    }
    if ((el = pick("[data-um-rwin]"))) {
      query({ window: el.dataset.umRwin as RequestQuery["window"] });
      return;
    }
    if ((el = pick("[data-um-row]"))) {
      const key = el.dataset.umRow!;
      requestsUi.expanded = requestsUi.expanded === key ? null : key;
      const row = loaded(state.requests.page)?.data.rows.find(
        (r) => r.key === key
      );
      if (
        requestsUi.expanded &&
        row?.requestId &&
        state.requests.detail?.key !== key
      )
        askDetail(key);
      render();
      return;
    }
    if ((el = pick("[data-um-detail]"))) {
      const key = el.dataset.umDetail!;
      if (
        loaded(state.requests.page)?.data.rows.some(
          (r) => r.key === key && r.requestId
        )
      ) {
        askDetail(key);
        render();
      }
      return;
    }
    if ((el = pick("[data-um-steps]"))) {
      const key = el.dataset.umSteps!;
      if (!requestsUi.openSteps.delete(key)) requestsUi.openSteps.add(key);
      render();
      return;
    }
    if ((el = pick("[data-um-earlier]"))) {
      earlier("records", el.dataset.umEarlier!);
      return;
    }
    if ((el = pick("[data-um-learlier]"))) {
      earlier("logs", el.dataset.umLearlier!);
      return;
    }
    if ((el = pick("[data-um-mearlier]"))) {
      earlier("maintenance", el.dataset.umMearlier!);
      return;
    }
    if ((el = pick("[data-um-lwin]"))) {
      logQuery({ window: el.dataset.umLwin as RequestQuery["window"] });
      return;
    }
    if ((el = pick("[data-um-level]"))) {
      const level = el.dataset.umLevel as LogLevel;
      const current = (pendingLogQuery ?? state.requests.logs.query).levels;
      logQuery({
        levels: current.includes(level)
          ? current.filter((l) => l !== level)
          : [...current, level],
      });
      return;
    }
    // 模型与设置
    if ((el = pick("[data-um-pick-all]"))) {
      for (const id of catalogIds(state.models)) modelsUi.selected.add(id);
      render();
      return;
    }
    if ((el = pick("[data-um-pick-none]"))) {
      modelsUi.selected.clear();
      render();
      return;
    }
    if ((el = pick("[data-um-test]"))) {
      if (testBlockReason(state.models, modelsUi) !== null) return;
      const ids = catalogIds(state.models).filter((id) =>
        modelsUi.selected.has(id)
      );
      modelsUi.awaiting = true;
      render();
      emit({ type: "models.test", models: ids });
      return;
    }
    if ((el = pick("[data-um-stop]"))) {
      const runId = el.dataset.umStop!;
      if (stopBlockReason(state.models, runId) === null)
        emit({ type: "models.stop", runId });
      return;
    }
    if ((el = pick("[data-um-refresh-catalog]"))) {
      if (refreshBlockReason(state.models) === null)
        emit({ type: "models.refreshCatalog" });
      return;
    }
    // The model name and 最近测试's 详情 open the same row detail.
    if ((el = pick("[data-um-model], [data-um-model-more]"))) {
      const id = el.dataset.umModel ?? el.dataset.umModelMore!;
      if (!modelsUi.expanded.delete(id)) modelsUi.expanded.add(id);
      render();
      return;
    }
    if ((el = pick("[data-um-rundetail]"))) {
      const id = el.dataset.umRundetail!;
      if (!modelsUi.runDetail.delete(id)) modelsUi.runDetail.add(id);
      render();
      return;
    }
    if ((el = pick("[data-um-copy]"))) {
      void copyUrl(el as HTMLButtonElement);
      return;
    }
  });

  root.addEventListener("change", (event) => {
    const target = event.target as HTMLInputElement | HTMLSelectElement;
    if (target.matches("[data-um-rmodel]")) {
      query({ model: target.value || null });
      return;
    }
    if (target.matches("[data-um-rresult]")) {
      query({ result: target.value as RequestQuery["result"] });
      return;
    }
    if (target.matches("[data-um-rsearch]")) {
      query({ search: target.value.trim() });
      return;
    }
    if (target.matches("[data-um-lsource]")) {
      logQuery({ source: target.value || null });
      return;
    }
    if (target.matches("[data-um-hmodel]")) {
      emit({
        type: "models.history",
        filter: { ...state.models.history.filter, model: target.value || null },
      });
      return;
    }
    if (target.matches("[data-um-pick]")) {
      const id = (target as HTMLInputElement).dataset.umPick!;
      if ((target as HTMLInputElement).checked) modelsUi.selected.add(id);
      else modelsUi.selected.delete(id);
      render();
    }
  });
  root.addEventListener("input", (event) => {
    const target = event.target as HTMLInputElement;
    if (target.matches("[data-um-rsearch]")) requestsUi.search = target.value;
  });
  root.addEventListener(
    "search",
    (event) => {
      const target = event.target as HTMLInputElement;
      if (target.matches("[data-um-rsearch]"))
        query({ search: target.value.trim() });
    },
    true
  );
  root.addEventListener(
    "toggle",
    (event) => {
      const details = event.target as HTMLDetailsElement;
      const fold = details.dataset?.umFold;
      if (!fold) return;
      if (fold === "history") {
        if (modelsUi.historyOpen === details.open) return;
        modelsUi.historyOpen = details.open;
        const records = state.models.history.records;
        if (details.open && records.state !== "unwired" && !loaded(records))
          emit({ type: "models.history", filter: state.models.history.filter });
      } else if (fold === "settings") {
        if (modelsUi.settingsOpen === details.open) return;
        modelsUi.settingsOpen = details.open;
      }
      render();
    },
    true
  );

  root.addEventListener("keydown", (event) => {
    const target = event.target as HTMLElement;
    if (menuOpen && target.closest(".um-menu")) {
      const items = Array.from(
        menu.querySelectorAll<HTMLElement>("[role=menuitem]")
      );
      const at = items.indexOf(target);
      if (event.key === "Escape") {
        event.preventDefault();
        closeMenu(true);
      } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        items[
          (at + (event.key === "ArrowDown" ? 1 : -1) + items.length) %
            items.length
        ]?.focus();
      } else if (event.key === "Tab") closeMenu(false);
      return;
    }
    if (
      target.matches("[role=tab]") &&
      (event.key === "ArrowLeft" || event.key === "ArrowRight")
    ) {
      event.preventDefault();
      const at = VIEWS.findIndex((v) => v.id === requestsUi.view);
      const next =
        VIEWS[
          (at + (event.key === "ArrowRight" ? 1 : -1) + VIEWS.length) %
            VIEWS.length
        ];
      navigate("requests", next.id);
      content
        .querySelector<HTMLElement>(`[data-um-view="${next.id}"]`)
        ?.focus();
      return;
    }
    if (target.matches("[data-um-rsearch]") && event.key === "Enter") {
      query({ search: (target as HTMLInputElement).value.trim() });
      return;
    }
    if (target.matches("[data-um-rank]")) {
      const rows = Array.from(
        content.querySelectorAll<HTMLElement>("[data-um-rank]")
      );
      const at = rows.indexOf(target);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        rows[
          (at + (event.key === "ArrowDown" ? 1 : -1) + rows.length) %
            rows.length
        ]?.focus();
        return;
      }
      if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        rows[event.key === "Home" ? 0 : rows.length - 1]?.focus();
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        linkRank(null);
        target.blur();
        return;
      }
    }
    if (target.matches("[data-um-chart]") && chart.trend?.days.length) {
      const n = chart.trend.days.length;
      const current = chart.active ?? n - 1;
      const next =
        event.key === "ArrowLeft"
          ? Math.max(0, current - 1)
          : event.key === "ArrowRight"
            ? Math.min(n - 1, current + 1)
            : event.key === "Home"
              ? 0
              : event.key === "End"
                ? n - 1
                : null;
      if (event.key === "Escape") {
        setActive(target, null, false);
        return;
      }
      if (next === null) return;
      event.preventDefault();
      setActive(target, next, true);
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && menuOpen) closeMenu(true);
  });
  document.addEventListener("click", (event) => {
    if (menuOpen && !root.contains(event.target as Node)) closeMenu(false);
  });

  content.addEventListener("pointermove", (event) => {
    const host = (event.target as Element).closest<HTMLElement>(
      "[data-um-chart]"
    );
    if (!host) {
      // Leaving the plot for the ranking (still inside .um-content) must also
      // release the column band, the guide and the attached markers.
      const chartHost = content.querySelector<HTMLElement>("[data-um-chart]");
      if (
        chartHost &&
        chart.active !== null &&
        document.activeElement !== chartHost
      )
        setActive(chartHost, null, false);
      return;
    }
    const index = chart.indexAt(host, event.clientX);
    if (index !== chart.active) setActive(host, index, false);
  });
  // A ranking row points at its own curve only while pointed at or focused:
  // the highlight is local, it never asks the controller for anything.
  content.addEventListener("pointerover", (event) => {
    const row = (event.target as Element).closest<HTMLElement>(
      "[data-um-rank]"
    );
    if (row && !row.classList.contains("is-linked")) linkRank(row);
  });
  content.addEventListener("pointerout", (event) => {
    const row = (event.target as Element).closest<HTMLElement>(
      "[data-um-rank]"
    );
    if (!row) return;
    const to = event.relatedTarget as Element | null;
    if (to && row.contains(to)) return;
    if (
      document.activeElement instanceof Element &&
      document.activeElement.closest("[data-um-rank]")
    )
      return;
    linkRank(null);
  });
  content.addEventListener("pointerleave", () => {
    const host = content.querySelector<HTMLElement>("[data-um-chart]");
    if (host && document.activeElement !== host) setActive(host, null, false);
  });
  content.addEventListener("focusin", (event) => {
    const target = event.target as Element;
    const host = target.closest<HTMLElement>("[data-um-chart]");
    if (host && chart.trend) {
      setActive(host, chart.active ?? chart.trend.days.length - 1, true);
      return;
    }
    const row = target.closest<HTMLElement>("[data-um-rank]");
    if (row) linkRank(row);
  });
  content.addEventListener("focusout", (event) => {
    const target = event.target as Element;
    const host = target.closest<HTMLElement>("[data-um-chart]");
    if (host) {
      setActive(host, null, false);
      return;
    }
    const row = target.closest<HTMLElement>("[data-um-rank]");
    if (!row) return;
    const to = event.relatedTarget as Element | null;
    if (to && to.closest("[data-um-rank]")) return;
    linkRank(null);
  });
  // The plate is fixed to the viewport. On a scroll it follows a keyboard-held
  // column; a pointer-held one is released until the pointer moves again.
  main.addEventListener(
    "scroll",
    () => {
      const host = content.querySelector<HTMLElement>("[data-um-chart]");
      if (host && chart.active !== null) {
        if (document.activeElement === host) showTip(host);
        else setActive(host, null, false);
      } else if (!tip.hidden) hideTip();
    },
    { passive: true }
  );

  window.addEventListener("hashchange", onRoute);
  options.gate.onChange(() => paintRole());

  render();
  // After mountUmConsole returns, so a controller that answers inside onEvent
  // already holds the handle it updates.
  queueMicrotask(() =>
    emit({
      type: "page",
      page: route.page,
      view: route.page === "requests" ? route.view : null,
    })
  );

  return {
    update(next: UmViewState): void {
      const now = seenOf(next);
      const newRun = now.runId !== null && now.runId !== seen.runId;
      state = next;
      // A test click stays 「正在提交」 until the controller answers it — a run
      // with a new id, a new notice, or a test capability that opened or
      // closed — not on any unrelated update (a duplicate click would follow).
      if (
        modelsUi.awaiting &&
        (newRun ||
          (now.noticeAt !== null && now.noticeAt !== seen.noticeAt) ||
          now.context !== seen.context)
      )
        modelsUi.awaiting = false;
      seen = now;
      // A query is pending until the controller reports exactly it back.
      if (pendingQuery && sameQuery(next.requests.query, pendingQuery))
        pendingQuery = null;
      if (
        pendingLogQuery &&
        sameLogQuery(next.requests.logs.query, pendingLogQuery)
      )
        pendingLogQuery = null;
      // The asked-for linked logs arrived, in whatever state they are.
      if (
        requestsUi.detailAsked !== null &&
        next.requests.detail?.key === requestsUi.detailAsked
      )
        requestsUi.detailAsked = null;
      // A 「加载更早」 read is answered when its view no longer offers the
      // cursor (more rows, or another query), when the kept page reports a
      // newer failure, or when a read seen in flight stops (cancelled): the
      // button opens again, and after a failure the same cursor can be retried.
      for (const { id: view } of VIEWS) {
        const cursor = requestsUi.earlier[view],
          ask = earlierAsks[view];
        if (cursor === null || ask === null) continue;
        const load = earlierLoad(next, view),
          shown = loaded(load);
        const moved = shown === null || shown.data.earlier !== cursor;
        const failed =
          !moved && shown.stale !== null && failureKey(load) !== ask.failure;
        if (!moved && !failed && !(ask.busy && !shown.refreshing)) {
          if (shown.refreshing) ask.busy = true;
          continue;
        }
        requestsUi.earlier[view] = null;
        earlierAsks[view] = null;
        requestsUi.earlierFailed[view] =
          failed && shown?.stale
            ? { cursor, reason: failureText(shown.stale) }
            : null;
      }
      // A model that left the catalog cannot stay selected.
      const ids = new Set(catalogIds(next.models));
      if (loaded(next.models.catalog))
        for (const id of [...modelsUi.selected])
          if (!ids.has(id)) modelsUi.selected.delete(id);
      if (document.activeElement?.matches?.("[data-um-rsearch]") !== true)
        requestsUi.search = next.requests.query.search;
      // A confirm whose action closed, or started elsewhere, while the dialog
      // was open is withdrawn rather than left to send a refused write.
      if (confirming && confirmDialog.open && !allowed(confirming))
        confirmDialog.close("cancel");
      render();
      // A test the reader just started unfolds below the table: bring it in view.
      if (newRun && route.page === "models")
        content
          .querySelector(".um-run")
          ?.scrollIntoView({
            block: "nearest",
            behavior: reduceMotion() ? "auto" : "smooth",
          });
    },
  };
}
