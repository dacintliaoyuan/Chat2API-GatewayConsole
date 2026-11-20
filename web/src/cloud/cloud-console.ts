import "./cloud.css";
import { installCloudBrand } from "./brand";
import { escapeHtml } from "../html";
import type { RoleGate, RoleVerdict } from "../console/role-gate";
import type { ShareKeyRow } from "../console/share-model";
import {
  FAILURE_WORD,
  lastValue,
  readActions,
  readDisplayState,
  readOperationById,
  readOperationByIdentity,
  readOperations,
  readShareKeys,
  readShareStatus,
  readShareUsage,
  readShareUsageFailed,
  readSummary,
  type OperationRead,
  type OperationRow,
} from "./cloud-reads";
import {
  clock,
  overviewHtml,
  PRIMARY_START,
  PRIMARY_STOP,
  primaryAction,
  stageNote,
} from "./overview";
import { opLabel } from "./areas";
import {
  CREATE_PLAIN_NOTE,
  PLAIN_DIALOG_NOTE,
  findOperationById,
  findOperationByIdentity,
  loadPendingIntents,
  newIntentKey,
  operationOutcome,
  savePendingIntents,
  submitAnswer,
  submitBody,
  type Outcome,
  type PendingIntent,
} from "./operation-intent";
import type {
  CloudModel,
  ConfirmCopy,
  Feedback,
  KeyTest,
  KeyUi,
  UsageTime,
} from "./model";
import { createKeysPage } from "./pages/keys";
import { createRequestsPage } from "./pages/requests";
import { createStatusActions } from "./pages/status";

export type {
  CloudModel,
  Feedback,
  KeyNotice,
  KeyTest,
  KeyUi,
  RequestsResultFilter,
  RequestsTimeRange,
  RequestsUi,
  UsageDimension,
  UsageTime,
  UsageUi,
} from "./model";
export {
  CREATE_PLAIN_NOTE,
  PLAIN_DIALOG_NOTE,
  plainNotice,
  revokeKey,
  submitAnswer,
  submitConcurrency,
  submitCreate,
} from "./operation-intent";
export type {
  ConcurrencySubmission,
  CreateSubmission,
  RevokeSubmission,
  SubmitAnswer,
} from "./operation-intent";

/** Ticket #4: the console is one page. The old `#keys`, `#usage`, `#services`
 *  and `#ops` hashes resolve to this one area instead of a removed page. */
export type Area = "overview";
const AREAS: readonly { id: Area; label: string; icon: string }[] = [
  {
    id: "overview",
    label: "总览",
    icon: `<rect x="3.5" y="3.5" width="7" height="7" rx="2"/><rect x="13.5" y="3.5" width="7" height="7" rx="2"/><rect x="3.5" y="13.5" width="7" height="7" rx="2"/><rect x="13.5" y="13.5" width="7" height="7" rx="2"/>`,
  },
];
const REFRESH_MS = 30_000;
const TERMINAL = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
/** Local wait per submitted operation; past it the result is reported as
 *  unconfirmed with its id — the page never re-sends a write. */
const WAIT_MS: Readonly<Record<string, number>> = {
  "service.start": 3 * 60_000,
  "service.stop": 3 * 60_000,
  "service.restart": 3 * 60_000,
  "share.gateway_restart": 3 * 60_000,
};

/** Confirmation copy for every write this console offers: target and effect. */
const CONFIRM: Readonly<Record<string, ConfirmCopy>> = {
  "service.start|share-gateway": {
    title: "启动 API 服务？",
    confirm: "启动",
    body: "启动 UM 分享入口；未接入时会如实报告失败，不冒充已启动。",
  },
  "service.stop|share-gateway": {
    title: "停止 API 服务？",
    danger: true,
    confirm: "停止",
    body: "停止会先拒绝新请求，等待在途请求结束；停止意图由云端持久化。确定停止？",
  },
  "share.status|share": {
    title: "分层检查（只读）？",
    confirm: "开始检查",
    body: "只读取当前服务各层状态并记录一次检查，不改变、不重启云端 API 服务。",
  },
  "share.diagnostics|share": {
    title: "分层诊断（只读）？",
    confirm: "开始检查",
    body: "只读取当前服务各层状态并记录一次检查，不改变、不重启云端 API 服务。",
  },
  "share.gateway_restart|share": {
    title: "重启分享入口 8100？",
    danger: true,
    confirm: "重启",
    body: "经受限 broker 执行一次单层重启；暂停接收、有界排空、重启后恢复接收。确定重启？",
  },
};
export function mountCloudConsole(stage: HTMLElement, gate: RoleGate): void {
  installCloudBrand();
  const loading = { state: "loading" } as const;
  const feedback: Feedback[] = [];
  /** A2: unresolved intents (key, action, target, op id). Persisted in
   *  sessionStorage so a page refresh can re-query the original identity. */
  const intents: PendingIntent[] = loadPendingIntents();
  for (const intent of intents) {
    // The page itself is new after a refresh: show the pending intent and let
    // the first refresh reconcile it by identity. Never a second submit.
    feedback.push({
      key: `intent-${intent.key}`,
      action: intent.action,
      target: intent.target,
      tone: "warn",
      text: "刷新后待回查：按原幂等身份核对，本页不重发",
      opId: intent.opId,
      at: intent.at,
      done: false,
      intentKey: intent.key,
    });
  }
  const keyUi: KeyUi = {
    busy: new Set(),
    notices: new Map(),
    secrets: new Map(),
    tests: new Map(),
    open: null,
    caps: new Map(),
    createBusy: false,
    createIdem: newIntentKey(),
    createError: null,
    plain: null,
  };
  const model: CloudModel = {
    summary: loading,
    health: loading,
    actions: loading,
    operations: loading,
    status: loading,
    keys: loading,
    usage: loading,
    usageFailed: loading,
    usageRequests: null,
    usageRequestsUi: {
      result: "",
      time: "today",
      cursors: [null],
      page: 0,
      loading: false,
    },
    security: loading,
    display: loading,
    displayState: loading,
    verdict: gate.verdict(),
    overviewUi: { metric: "requests", rank: "key", range: "14", chartWidth: 0 },
    usageUi: {
      dimension: "key",
      key: "",
      model: "",
      time: { kind: "14", day: "" },
      page: 1,
      open: null,
      result: "",
    },
    statusOpen: false,
    primaryPending: null,
    keyUi,
    feedback,
    actionButton: (action, target, html, cls) => {
      const block = writeBlock(action, target);
      return `<button type="button" class="${cls}" data-cloud-op="${escapeHtml(`${action}|${target}`)}"${block ? ` disabled title="${escapeHtml(block)}"` : ""}>${html}</button>`;
    },
    writeReady: () => {
      if (__CONSOLE_CANDIDATE__)
        return {
          ok: false,
          reason: "候选预览是只读的：写操作请在云端生产页面执行",
        };
      const verdict = gate.verdict();
      return verdict.state === "confirmed"
        ? { ok: true, reason: "" }
        : { ok: false, reason: verdict.reason };
    },
    keyBusy: (id) => [...keyUi.busy].some((key) => key.endsWith(`:${id}`)),
    keyNotice: (id) => keyUi.notices.get(id),
    keySecret: (id) => keyUi.secrets.get(id),
    keyTest: (id) => keyUi.tests.get(id),
    keyCapValue: (id) => keyUi.caps.get(id),
    latestKeyTest: (from) => {
      let best: { name: string; test: KeyTest } | null = null;
      for (const [id, test] of keyUi.tests) {
        if ((test.from ?? "public") !== from) continue;
        if (!best || (test.at ?? 0) > (best.test.at ?? 0))
          best = { name: keyLabel(keyRow(id), id), test };
      }
      return best;
    },
  };
  let area: Area = areaFromHash();
  let refreshing = false;
  let lastRefresh: number | null = null;
  let drawerOpen = false;
  let reconciling = false;
  stage.insertAdjacentHTML(
    "beforeend",
    `<section class="app-shell cloud-console" aria-label="云端控制台">
    <aside class="shell-sidebar" id="cloud-nav" aria-label="云端控制台导航">
      <div class="shell-side-panel shell-glass">
        <div class="shell-env" data-role-state="checking"><div class="shell-env-name"><span class="shell-env-mark" aria-hidden="true"></span><span>云端 · UM API 服务</span></div><div class="shell-env-sub" data-shell-role-text>只管理云端 API 服务</div></div>
        <nav class="shell-nav" aria-label="云端控制台导航">${AREAS.map((a) => `<button type="button" data-cloud-area="${a.id}" aria-label="${escapeHtml(a.label)}"><svg viewBox="0 0 24 24" aria-hidden="true">${a.icon}</svg><span class="shell-nav-text">${escapeHtml(a.label)}</span><span class="shell-hint" aria-hidden="true">${escapeHtml(a.label)}</span></button>`).join("")}</nav>
        <div class="shell-side-foot">
          <button type="button" class="shell-icon-btn" data-action="settings" aria-label="外观设置"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg><span class="shell-hint" aria-hidden="true">外观设置</span></button>
          <span class="shell-side-text">外观</span>
          <button type="button" class="shell-icon-btn" data-action="rail-toggle" aria-label="收起侧栏" aria-expanded="true"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="4.5" width="17" height="15" rx="3"/><path d="M9.5 4.5v15M15.5 10l-2 2 2 2"/></svg><span class="shell-hint" aria-hidden="true">收起侧栏</span></button>
        </div>
      </div>
    </aside>
    <main class="shell-main">
      <div class="shell-page">
        <header class="shell-toolbar">
          <button type="button" class="cloud-drawer-toggle" data-cloud-drawer-toggle aria-label="打开导航" aria-expanded="false" aria-controls="cloud-nav"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button>
          <div class="shell-title"><h1 data-cloud-title></h1><p data-cloud-subtitle></p></div>
          <div class="shell-capsules"><div class="shell-capsule shell-glass"><button type="button" data-cloud-refresh><svg class="shell-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.6-5.9"/><path d="M20 4.5V9h-4.5"/></svg>刷新</button></div></div>
        </header>
        <div class="shell-role-notice" data-shell-role-notice role="status" hidden></div>
        <div class="cloud-feedback" aria-live="polite"></div>
        <div class="cloud-content"></div>
      </div>
    </main>
    <div class="cloud-scrim" data-cloud-drawer-close aria-hidden="true" hidden></div>
  </section>
  <div class="cloud-tip" role="tooltip" hidden></div>
  <dialog class="shell-confirm" aria-labelledby="cloud-confirm-title"><form method="dialog"><h2 id="cloud-confirm-title"></h2><p class="shell-confirm-body"></p><p class="shell-confirm-target mono"></p><div class="shell-confirm-actions"><button value="cancel" class="btn btn-outline">取消</button><button value="confirm" class="btn btn-primary"></button></div></form></dialog>
  <dialog class="cloud-key-create" aria-labelledby="cloud-key-create-title">
    <form data-cloud-key-create-form novalidate>
      <h2 id="cloud-key-create-title">新建长期 API Key</h2>
      <p class="cloud-dialog-note">${escapeHtml(CREATE_PLAIN_NOTE)}</p>
      <label class="cloud-field"><span>名称</span><input name="name" type="text" maxlength="64" autocomplete="off" required placeholder="例如：朋友的 Cursor"></label>
      <label class="cloud-field"><span>并发上限（1–48，默认 16）</span><input name="max_concurrency" type="number" min="1" max="48" step="1" value="16" inputmode="numeric"></label>
      <p class="cloud-form-error" data-cloud-key-create-error role="alert"></p>
      <div class="cloud-dialog-actions"><button type="button" class="btn btn-outline" data-cloud-key-create-close>取消</button><button type="submit" class="btn btn-primary" data-cloud-key-create-submit>创建</button></div>
    </form>
  </dialog>
  <dialog class="cloud-key-rename" aria-labelledby="cloud-key-rename-title">
    <form data-cloud-key-rename-form novalidate>
      <h2 id="cloud-key-rename-title">Key 改名</h2>
      <label class="cloud-field"><span>名称</span><input name="name" type="text" maxlength="64" autocomplete="off" required></label>
      <p class="cloud-form-error" data-cloud-key-rename-error role="alert"></p>
      <div class="cloud-dialog-actions"><button type="button" class="btn btn-outline" data-cloud-key-rename-close>取消</button><button type="submit" class="btn btn-primary">保存</button></div>
    </form>
  </dialog>
  <dialog class="cloud-plain" aria-labelledby="cloud-plain-title">
    <div class="cloud-plain-body">
      <h2 id="cloud-plain-title"></h2>
      <p class="cloud-plain-warn">${escapeHtml(PLAIN_DIALOG_NOTE)}</p>
      <div class="cloud-plain-value"><code data-cloud-plain-value></code></div>
      <p class="cloud-plain-note" data-cloud-plain-note></p>
      <div class="cloud-dialog-actions">
        <button type="button" class="btn btn-tonal" data-cloud-plain-toggle>显示明文</button>
        <button type="button" class="btn btn-outline" data-cloud-plain-copy>复制明文</button>
        <button type="button" class="btn btn-primary" data-cloud-plain-close>完成</button>
      </div>
    </div>
  </dialog>`
  );

  const root = stage.querySelector<HTMLElement>(".cloud-console")!;
  const content = root.querySelector<HTMLElement>(".cloud-content")!;
  const tip = stage.querySelector<HTMLElement>(".cloud-tip")!;
  const nav = root.querySelector<HTMLElement>("#cloud-nav")!;
  const scrim = root.querySelector<HTMLElement>("[data-cloud-drawer-close]")!;
  const drawerToggle = root.querySelector<HTMLButtonElement>(
    "[data-cloud-drawer-toggle]"
  )!;
  const confirmDialog =
    stage.querySelector<HTMLDialogElement>(".shell-confirm")!;
  const createDialog =
    stage.querySelector<HTMLDialogElement>(".cloud-key-create")!;
  const createForm = createDialog.querySelector<HTMLFormElement>("form")!;
  const createSubmit = createDialog.querySelector<HTMLButtonElement>(
    "[data-cloud-key-create-submit]"
  )!;
  const createErrorBox = createDialog.querySelector<HTMLElement>(
    "[data-cloud-key-create-error]"
  )!;
  const renameDialog =
    stage.querySelector<HTMLDialogElement>(".cloud-key-rename")!;
  const renameForm = renameDialog.querySelector<HTMLFormElement>("form")!;
  const renameErrorBox = renameDialog.querySelector<HTMLElement>(
    "[data-cloud-key-rename-error]"
  )!;
  const plainDialog = stage.querySelector<HTMLDialogElement>(".cloud-plain")!;
  const plainTitle =
    plainDialog.querySelector<HTMLElement>("#cloud-plain-title")!;
  const plainValue = plainDialog.querySelector<HTMLElement>(
    "[data-cloud-plain-value]"
  )!;
  const plainNote = plainDialog.querySelector<HTMLElement>(
    "[data-cloud-plain-note]"
  )!;
  const plainToggle = plainDialog.querySelector<HTMLButtonElement>(
    "[data-cloud-plain-toggle]"
  )!;
  const plainCopy = plainDialog.querySelector<HTMLButtonElement>(
    "[data-cloud-plain-copy]"
  )!;
  const mobile = window.matchMedia("(max-width: 720px)");
  const requests = createRequestsPage({ model, render });
  const statusActions = createStatusActions({
    model,
    keyUi,
    render,
    addFeedback,
    settle,
  });
  const keys = createKeysPage({
    model,
    keyUi,
    content,
    createDialog,
    createForm,
    createSubmit,
    createErrorBox,
    renameDialog,
    renameForm,
    renameErrorBox,
    plainDialog,
    plainTitle,
    plainValue,
    plainNote,
    plainToggle,
    plainCopy,
    render,
    refreshKeys,
    refreshUsage,
    loadRequestsPage: requests.loadRequestsPage,
    addFeedback,
    settle,
    keyRow,
    keyLabel,
    confirmWith,
  });
  keys.init();

  function areaFromHash(): Area {
    const id = location.hash.replace(/^#/, "");
    return AREAS.some((a) => a.id === id) ? (id as Area) : "overview";
  }

  function writeBlock(action: string, target: string): string | null {
    if (__CONSOLE_CANDIDATE__)
      return "候选预览是只读的：写操作请在云端生产页面执行";
    if (gate.verdict().state !== "confirmed") return gate.verdict().reason;
    const actions = lastValue(model.actions)?.value;
    if (!actions)
      return model.actions.state === "failed"
        ? `能力表${FAILURE_WORD[model.actions.kind]}：${model.actions.reason}`
        : "能力表尚未读取";
    const row = actions.find((a) => a.action === action);
    if (!row) return "当前后端没有注册该动作";
    if (row.enabled !== true) return row.disabled_reason || "后端已禁用该动作";
    // A2: an unresolved intent of the same action/target blocks a second one
    // until it is re-queried or explicitly abandoned; another action/target is
    // an independent operation and is never merged.
    const pending = intents.find(
      (intent) => intent.action === action && intent.target === target
    );
    if (pending)
      return "上一次结果未确认；请先按原身份回查，或结束等待后再发起新的独立操作";
    return null;
  }

  function keyRow(id: string): ShareKeyRow | null {
    return (
      (lastValue(model.keys)?.value ?? []).find((row) => row.id === id) ?? null
    );
  }
  function keyLabel(row: ShareKeyRow | null, id: string): string {
    return row?.name || row?.masked || id;
  }
  function setDrawer(open: boolean, returnFocus = true): void {
    drawerOpen = open;
    root.dataset.drawer = open ? "open" : "closed";
    drawerToggle.setAttribute("aria-expanded", String(open));
    drawerToggle.setAttribute("aria-label", open ? "关闭导航" : "打开导航");
    scrim.hidden = !open;
    if (mobile.matches) nav.setAttribute("aria-hidden", String(!open));
    else nav.removeAttribute("aria-hidden");
    if (open) {
      requestAnimationFrame(() =>
        nav.querySelector<HTMLButtonElement>(".shell-nav button")?.focus()
      );
    } else if (returnFocus) {
      drawerToggle.focus();
    }
  }

  function paintRole(verdict: RoleVerdict): void {
    model.verdict = verdict;
    const env = root.querySelector<HTMLElement>(".shell-env");
    if (env) env.dataset.roleState = verdict.state;
    const sub = root.querySelector<HTMLElement>("[data-shell-role-text]");
    if (sub)
      sub.textContent =
        verdict.state === "confirmed"
          ? "只管理云端 API 服务 · 后端角色已确认"
          : verdict.state === "checking"
            ? "只管理云端 API 服务 · 正在核对后端角色"
            : "后端角色未确认 · 只读";
    const notice = root.querySelector<HTMLElement>("[data-shell-role-notice]")!;
    notice.hidden =
      verdict.state === "confirmed" || verdict.state === "checking";
    notice.dataset.state = verdict.state;
    notice.innerHTML = notice.hidden
      ? ""
      : `<div><strong>${verdict.state === "mismatch" ? "后端与本页角色不符，整页只读" : "后端角色未确认，写操作已关闭"}</strong><p>${escapeHtml(verdict.reason)}</p></div>`;
    render();
  }
  function renderFeedback(): void {
    const box = root.querySelector<HTMLElement>(".cloud-feedback")!;
    box.innerHTML = feedback
      .slice(-4)
      .reverse()
      .map((entry) => {
        const identity = entry.opId
          ? `<span class="mono">操作 ${escapeHtml(entry.opId)}</span>`
          : entry.intentKey
            ? `<span class="mono">幂等 ${escapeHtml(entry.intentKey)}</span>`
            : "";
        const actions = entry.intentKey
          ? `<button type="button" class="cloud-feedback-action" data-cloud-op-recheck="${escapeHtml(entry.intentKey)}">按原身份回查</button>
           <button type="button" class="cloud-feedback-action" data-cloud-op-abandon="${escapeHtml(entry.intentKey)}">结束等待</button>`
          : entry.done
            ? `<button type="button" class="shell-icon-btn" data-cloud-dismiss="${escapeHtml(entry.key)}" aria-label="关闭这条反馈"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg></button>`
            : "";
        return `<div class="cloud-feedback-row" data-tone="${entry.tone}">
        <span class="status" data-tone="${entry.tone}"><span class="dot" aria-hidden="true"></span>${escapeHtml(opLabel(entry.action, entry.target))}</span>
        <span class="cloud-feedback-text">${escapeHtml(entry.text)}${identity}</span>
        <span class="cloud-feedback-time">${clock(entry.at)}</span>
        ${actions}
      </div>`;
      })
      .join("");
  }
  function render(): void {
    const meta = AREAS.find((a) => a.id === area)!;
    root
      .querySelectorAll<HTMLButtonElement>("[data-cloud-area]")
      .forEach((button) => {
        if (button.dataset.cloudArea === area)
          button.setAttribute("aria-current", "page");
        else button.removeAttribute("aria-current");
      });
    root.querySelector("[data-cloud-title]")!.textContent = meta.label;
    const failed = [
      model.status,
      model.keys,
      model.usage,
      // Ticket #9：失败读数也是本页的读取面，失败时同样要计入页头。
      model.usageFailed,
      model.actions,
    ].filter((r) => r.state === "failed").length;
    root.querySelector("[data-cloud-subtitle]")!.textContent =
      lastRefresh === null
        ? "云端 API 服务 · 正在读取"
        : `云端 API 服务 · 数据更新于 ${clock(lastRefresh)} · 每 30 秒刷新${failed ? ` · ${failed} 项读取未成功` : ""}`;
    // Keep the reader's place: the focused control is found again by its data
    // attribute after the content is rebuilt.
    const focused =
      document.activeElement instanceof HTMLElement &&
      content.contains(document.activeElement)
        ? focusKey(document.activeElement)
        : null;
    const html = overviewHtml(model);
    if (content.dataset.area !== area || content.innerHTML !== html) {
      content.innerHTML = html;
      content.dataset.area = area;
      if (focused)
        content
          .querySelector<HTMLElement>(focused)
          ?.focus({ preventScroll: true });
    }
  }
  function focusKey(element: HTMLElement): string | null {
    for (const name of [
      "data-cloud-op",
      "data-cloud-go",
      "data-cloud-copy",
      "data-cloud-refresh",
      "data-cloud-primary",
      "data-cloud-status-toggle",
      "data-cloud-login-window-open",
      "data-cloud-login-cancel",
      "data-cloud-key-action",
      "data-cloud-key-cap-input",
      "data-cloud-key-create-open",
      "data-cloud-usage-page",
      "data-cloud-usage-dim",
      "data-cloud-usage-key",
      "data-cloud-usage-model",
      "data-cloud-usage-time",
      "data-cloud-metric",
      "data-cloud-rank",
      "data-cloud-drill",
      "data-cloud-ops-filter",
      "data-cloud-op-detail",
      "data-cloud-op-recheck",
      "data-cloud-op-abandon",
      "aria-label",
    ]) {
      const value = element.getAttribute(name);
      if (value !== null) return `[${name}="${CSS.escape(value)}"]`;
    }
    return null;
  }
  async function refresh(): Promise<void> {
    if (refreshing) return;
    refreshing = true;
    try {
      // Each read repaints as it lands; a slow one does not hold the others.
      const step = <K extends keyof CloudModel>(
        key: K,
        read: (previous: CloudModel[K]) => Promise<CloudModel[K]>
      ) =>
        read(model[key]).then((value) => {
          model[key] = value;
          if (key === "keys") keys.reconcileSecrets();
          render();
        });
      const reads: Promise<void>[] = [
        // The role gate's own contract names GET /api/summary as the cloud
        // marker; without this read the page stays 正在核对后端角色 and every
        // write stays closed (the capability table alone cannot confirm cloud).
        step("summary", readSummary),
        step("actions", readActions),
        step("operations", readOperations),
        step("status", readShareStatus),
        step("displayState", readDisplayState),
        step("keys", readShareKeys),
        step("usage", readShareUsage),
        step("usageFailed", readShareUsageFailed),
      ];
      await Promise.all(reads);
      lastRefresh = Date.now();
      render();
      // Ticket #9: a refresh also re-reads the request-detail page the reader
      // already loaded, keeping the same filter and page.
      if (model.usageRequests !== null && !model.usageRequestsUi.loading)
        void requests.loadRequestsPage(model.usageRequestsUi.page);
      void reconcileIntents();
    } finally {
      refreshing = false;
    }
  }

  async function refreshKeys(): Promise<void> {
    model.keys = await readShareKeys(model.keys);
    keys.reconcileSecrets();
    render();
  }
  async function refreshUsage(): Promise<void> {
    model.usage = await readShareUsage(model.usage);
    model.usageFailed = await readShareUsageFailed(model.usageFailed);
    render();
  }

  function addFeedback(entry: Omit<Feedback, "key" | "at" | "done">): Feedback {
    const row: Feedback = {
      ...entry,
      key: `fb-${Date.now()}-${feedback.length}`,
      at: Date.now(),
      done: false,
    };
    feedback.push(row);
    if (feedback.length > 12) feedback.shift();
    renderFeedback();
    return row;
  }
  function settle(row: Feedback, tone: Feedback["tone"], text: string): void {
    Object.assign(row, { tone, text, at: Date.now(), done: true });
    renderFeedback();
  }
  /** A2: a result that is not confirmed yet stays open (no dismiss) and keeps
   *  its intent identity, so the user can re-query it instead of re-sending. */
  function hold(row: Feedback, text: string): void {
    Object.assign(row, { tone: "warn", text, at: Date.now(), done: false });
    renderFeedback();
  }
  function saveIntents(): void {
    savePendingIntents(intents);
  }
  function removeIntent(key: string): void {
    const at = intents.findIndex((intent) => intent.key === key);
    if (at >= 0) {
      const [intent] = intents.splice(at, 1);
      if (intent.action === PRIMARY_START && model.primaryPending) {
        model.primaryPending = null;
        render();
      }
    }
    saveIntents();
  }
  function feedbackForIntent(key: string): Feedback | undefined {
    return feedback.find((entry) => entry.intentKey === key);
  }
  /** A2: a terminal backend result closes the intent and its feedback with the
   *  operation's real outcome. */
  function settleIntent(
    intent: PendingIntent,
    outcome: Outcome,
    operation: OperationRow | null
  ): void {
    const row = feedbackForIntent(intent.key);
    if (row) {
      if (operation) row.opId = operation.id;
      row.intentKey = null;
      settle(
        row,
        outcome.tone,
        outcome.text ? `${outcome.word}：${outcome.text}` : outcome.word
      );
    }
    removeIntent(intent.key);
  }

  /** A2: re-query the original identity, never re-submit. The operation id is
   *  used when known; otherwise the non-secret idempotency key is looked up in
   *  the backend's bounded page, so a row that dropped out of the recent 8 is
   *  still reachable. */
  async function recheckIntent(
    intent: PendingIntent,
    manual: boolean
  ): Promise<void> {
    const row = feedbackForIntent(intent.key);
    if (manual && row) {
      row.text = "正在按原身份回查…";
      renderFeedback();
    }
    const read: OperationRead = intent.opId
      ? await readOperationById(intent.opId)
      : await readOperationByIdentity(intent.action, intent.target, intent.key);
    if (read.kind === "found") {
      const operation = read.operation;
      intent.opId = operation.id;
      saveIntents();
      if (TERMINAL.has(operation.state)) {
        settleIntent(intent, operationOutcome(operation), operation);
        render();
        return;
      }
      if (row) {
        row.opId = operation.id;
        row.text =
          operation.state === "queued"
            ? "后端已受理，仍在排队；本页不重发"
            : "后端已受理，仍在执行；本页不重发";
        renderFeedback();
      }
      return;
    }
    if (read.kind === "absent") {
      if (row) {
        row.text = intent.opId
          ? "后端没有该操作 ID（可能已被清理）；仍为结果未确认，已保留幂等身份，本页不重发"
          : "后端最近 200 条没有该幂等身份（可能未被受理或已超出范围）；仍为结果未确认，已保留幂等身份，本页不重发";
        renderFeedback();
      }
      return;
    }
    if (row) {
      row.text = `回查未完成：${read.reason}；仍为结果未确认，本页不重发`;
      renderFeedback();
    }
  }

  async function recheckIntentByKey(key: string): Promise<void> {
    const intent = intents.find((candidate) => candidate.key === key);
    if (!intent) return;
    await recheckIntent(intent, true);
  }

  /** A2: "结束等待" abandons the tracking of one intent, not the backend
   *  operation. The identity is cleared so a new, independent operation can be
   *  started; the backend may still be executing the old one. */
  function abandonIntent(key: string): void {
    const intent = intents.find((candidate) => candidate.key === key);
    if (!intent) return;
    const row = feedbackForIntent(key);
    if (row) {
      row.intentKey = null;
      settle(
        row,
        "neutral",
        `已结束等待；后端可能仍在执行（${intent.opId ?? "未取得操作 ID"}），本页不会重发`
      );
    }
    removeIntent(key);
    render();
  }

  /** A2: on refresh, look for the original identity in the loaded page first
   *  and then re-query it. A terminal result settles the "结果未确认" row; a
   *  running one stays open. Never a second submit. */
  async function reconcileIntents(): Promise<void> {
    if (reconciling || !intents.length) return;
    reconciling = true;
    try {
      const loaded = lastValue(model.operations)?.value ?? [];
      for (const intent of [...intents]) {
        if (!intents.some((candidate) => candidate.key === intent.key))
          continue;
        const known =
          (intent.opId ? findOperationById(loaded, intent.opId) : null) ??
          findOperationByIdentity(
            loaded,
            intent.action,
            intent.target,
            intent.key
          );
        if (known && TERMINAL.has(known.state)) {
          settleIntent(intent, operationOutcome(known), known);
          continue;
        }
        if (known) {
          const row = feedbackForIntent(intent.key);
          if (row) {
            row.opId = known.id;
            row.text = "后端已受理，仍在执行；本页不重发";
            renderFeedback();
          }
          continue;
        }
        await recheckIntent(intent, false);
      }
    } finally {
      reconciling = false;
    }
  }
  async function submit(action: string, target: string): Promise<void> {
    const block = writeBlock(action, target);
    if (block) {
      const text = `未提交：${block}`;
      settle(
        addFeedback({
          action,
          target,
          tone: "warn",
          text,
          opId: null,
          intentKey: null,
        }),
        "warn",
        text
      );
      return;
    }
    // A2: the stable identity exists BEFORE the submit and survives a lost
    // reply, a timeout or a refresh. A new, independent submit gets a new key.
    const intent: PendingIntent = {
      key: newIntentKey(),
      action,
      target,
      at: Date.now(),
      opId: null,
    };
    intents.push(intent);
    saveIntents();
    if (action === PRIMARY_START)
      // 后端还没受理：此刻没有操作行，也就没有它自己的 steps，所以这是
      // 本页对「已提交」的唯一如实说法；受理后立刻改用 step 文案。
      model.primaryPending = {
        at: Date.now(),
        stage: "已提交，等待后端受理",
      };
    render();
    const row = addFeedback({
      action,
      target,
      tone: "neutral",
      text: "正在提交…",
      opId: null,
      intentKey: intent.key,
    });
    try {
      const response = await fetch("/gw/usagesrv/api/console/operations", {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify(submitBody(intent)),
      });
      const data = (await response.json().catch(() => null)) as {
        operation?: OperationRow;
        error?: string;
        reason?: string;
        detail?: string;
      } | null;
      const answer = submitAnswer(response.status, data, action, target);
      if (answer.kind === "refused") {
        // A definite refusal never ran the operation; a re-query cannot find it,
        // so the intent is closed instead of blocking the next one.
        removeIntent(intent.key);
        row.intentKey = null;
        settle(row, "err", `被拒绝：${answer.reason}`);
        return;
      }
      if (answer.kind === "unconfirmed") {
        // The backend may already have accepted the operation. Keep the identity
        // and re-query it; a new key here could run the action twice.
        hold(
          row,
          `结果未确认：服务端没有确认操作 ID（${answer.reason}）；已保留幂等身份，刷新或回查不会重发`
        );
        void reconcileIntents();
        return;
      }
      let operation = answer.operation;
      intent.opId = operation.id;
      saveIntents();
      row.opId = operation.id;
      if (action === PRIMARY_START) {
        model.primaryPending = {
          at: Date.now(),
          stage: stageNote(operation) ?? "已受理，等待后端执行",
        };
        render();
      }
      if (operation.deduplicated === true) {
        row.text = "后端按原身份返回已有操作，未重新执行";
        renderFeedback();
      }
      const deadline = Date.now() + (WAIT_MS[action] ?? 3 * 60_000);
      const isLogin = action === "account.login";
      while (!TERMINAL.has(operation.state)) {
        row.text =
          operation.state === "queued"
            ? "排队中（后端已受理）"
            : isLogin
              ? "已提交，等待登录结果（打开窗口不等于认证成功）"
              : "执行中（后端已受理）";
        renderFeedback();
        if (action === PRIMARY_START) {
          // 已受理：阶段文案取整链启动动作自己的 steps；operation 里还没有
          // 对应 step 时，只能说到「已受理」为止。
          model.primaryPending = {
            at: Date.now(),
            stage: stageNote(operation) ?? "已受理，等待后端执行",
          };
          render();
        }
        if (Date.now() >= deadline) {
          hold(
            row,
            "结果未确认：本页已停止等待，操作可能仍在进行；请按原身份回查，本页不会重发"
          );
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 1_500));
        try {
          const poll = await fetch(
            `/gw/usagesrv/api/console/operations/${encodeURIComponent(operation.id)}`,
            { credentials: "include" }
          );
          const next = (await poll.json().catch(() => null)) as {
            operation?: OperationRow;
          } | null;
          if (poll.ok && next?.operation?.id === operation.id)
            operation = next.operation;
        } catch {
          /* A lost poll is retried until the deadline; the operation itself is unaffected. */
        }
      }
      settleIntent(intent, operationOutcome(operation), operation);
    } catch (error) {
      hold(
        row,
        `结果未确认：提交请求没有得到应答（${error instanceof Error ? error.message : String(error)}）；已保留幂等身份，刷新或回查不会重发`
      );
    } finally {
      void refresh();
    }
  }
  function confirmWith(copy: ConfirmCopy, run: () => void): void {
    confirmDialog.querySelector("h2")!.textContent = copy.title;
    confirmDialog.querySelector(".shell-confirm-body")!.textContent = copy.body;
    confirmDialog.querySelector(".shell-confirm-target")!.textContent =
      copy.detail ?? "";
    const ok = confirmDialog.querySelector<HTMLButtonElement>(
      'button[value="confirm"]'
    )!;
    ok.textContent = copy.confirm;
    ok.className = copy.danger ? "btn btn-danger-fill" : "btn btn-primary";
    confirmDialog.returnValue = "";
    confirmDialog.onclose = () => {
      if (confirmDialog.returnValue === "confirm") run();
    };
    confirmDialog.showModal();
    confirmDialog
      .querySelector<HTMLButtonElement>('button[value="cancel"]')!
      .focus();
  }
  function confirmThen(action: string, target: string): void {
    const copy = CONFIRM[`${action}|${target}`] ?? {
      title: opLabel(action, target),
      body: "这个操作会改变云端 API 服务；请确认目标与影响。",
      confirm: "继续",
      danger: true,
    };
    confirmWith(
      { ...copy, detail: `动作 ${action} · 目标 ${target}` },
      () => void submit(action, target)
    );
  }
  root.addEventListener("click", (event) => {
    const target = event.target as Element;
    const navButton = target.closest<HTMLButtonElement>("[data-cloud-area]");
    if (navButton) {
      if (mobile.matches) setDrawer(false, false);
      location.hash = navButton.dataset.cloudArea!;
      return;
    }
    if (target.closest("[data-cloud-drawer-toggle]")) {
      setDrawer(!drawerOpen);
      return;
    }
    if (target.closest("[data-cloud-drawer-close]")) {
      setDrawer(false);
      return;
    }
    const go = target.closest<HTMLElement>("[data-cloud-go]");
    if (go) {
      if (mobile.matches) setDrawer(false, false);
      location.hash = go.dataset.cloudGo!;
      return;
    }
    if (target.closest("[data-cloud-refresh]")) {
      void refresh();
      return;
    }
    const copyApiUrl = target.closest<HTMLElement>(
      "[data-cloud-copy-api-url]"
    );
    if (copyApiUrl) {
      const value = copyApiUrl.dataset.cloudCopyApiUrl ?? "";
      if (value) {
        void navigator.clipboard?.writeText(value);
        addFeedback({
          action: "share.copy_api_url",
          target: "share",
          tone: "ok",
          text: "已复制 API 地址",
          opId: null,
          intentKey: null,
        });
        render();
      }
      return;
    }
    const primary = target.closest<HTMLButtonElement>("[data-cloud-primary]");
    if (primary && !primary.disabled) {
      const action = primaryAction(model).action;
      if (action === PRIMARY_START) void submit(PRIMARY_START, "share-gateway");
      else if (action === PRIMARY_STOP) confirmThen(PRIMARY_STOP, "share-gateway");
      return;
    }
    if (target.closest("[data-cloud-status-toggle]")) {
      model.statusOpen = !model.statusOpen;
      render();
      return;
    }
    if (target.closest("[data-cloud-login-window-open]")) {
      void statusActions.openDisplayWindow();
      return;
    }
    if (target.closest("[data-cloud-login-cancel]")) {
      void statusActions.cancelLogin();
      return;
    }
    const op = target.closest<HTMLButtonElement>("[data-cloud-op]");
    if (op && !op.disabled) {
      const [action, opTarget] = op.dataset.cloudOp!.split("|");
      confirmThen(action, opTarget);
      return;
    }
    const dismiss = target.closest<HTMLElement>("[data-cloud-dismiss]");
    if (dismiss) {
      const at = feedback.findIndex(
        (f) => f.key === dismiss.dataset.cloudDismiss
      );
      if (at >= 0) feedback.splice(at, 1);
      renderFeedback();
      return;
    }
    if (target.closest("[data-cloud-key-create-open]")) {
      keys.openCreate();
      return;
    }
    const recheck = target.closest<HTMLElement>("[data-cloud-op-recheck]");
    if (recheck) {
      void recheckIntentByKey(recheck.dataset.cloudOpRecheck ?? "");
      return;
    }
    const abandon = target.closest<HTMLElement>("[data-cloud-op-abandon]");
    if (abandon) {
      abandonIntent(abandon.dataset.cloudOpAbandon ?? "");
      return;
    }
    const result = target.closest<HTMLElement>("[data-cloud-result]");
    if (result) {
      // D18-2: one result filter opens the per-request page with `result=`.
      model.usageUi.result = result.dataset.cloudResult ?? "";
      model.usageUi.page = 1;
      model.usageUi.open = null;
      void requests.loadRequestsPage(0, true);
      render();
      return;
    }
    const range = target.closest<HTMLElement>("[data-cloud-range]");
    if (range) {
      const value = range.dataset.cloudRange;
      model.overviewUi.range = value === "7" || value === "30" ? value : "14";
      render();
      return;
    }
    if (target.closest("[data-cloud-usage-requests]")) {
      void requests.loadRequestsPage(0, true);
      return;
    }
    if (target.closest("[data-cloud-usage-requests-more]")) {
      void requests.loadRequestsPage(model.usageRequestsUi.page + 1);
      return;
    }
    const requestsResult = target.closest<HTMLElement>(
      "[data-cloud-requests-result]"
    );
    if (requestsResult) {
      const value = requestsResult.dataset.cloudRequestsResult;
      model.usageRequestsUi.result =
        value === "success" ||
        value === "failed" ||
        value === "cancelled" ||
        value === "unknown"
          ? value
          : "";
      void requests.loadRequestsPage(0, true);
      return;
    }
    const requestsWindow = target.closest<HTMLElement>(
      "[data-cloud-requests-window]"
    );
    if (requestsWindow) {
      const value = requestsWindow.dataset.cloudRequestsWindow;
      model.usageRequestsUi.time =
        value === "7" ? "7" : value === "30" ? "30" : "today";
      void requests.loadRequestsPage(0, true);
      return;
    }
    const requestsPageButton = target.closest<HTMLButtonElement>(
      "[data-cloud-requests-page]"
    );
    if (requestsPageButton && !requestsPageButton.disabled) {
      void requests.loadRequestsPage(
        model.usageRequestsUi.page +
          Number(requestsPageButton.dataset.cloudRequestsPage || "0")
      );
      return;
    }
    if (target.closest("[data-cloud-requests-retry]")) {
      void requests.loadRequestsPage(model.usageRequestsUi.page);
      return;
    }
    const metric = target.closest<HTMLElement>("[data-cloud-metric]");
    if (metric) {
      model.overviewUi.metric =
        metric.dataset.cloudMetric === "tokens" ? "tokens" : "requests";
      render();
      return;
    }
    const rank = target.closest<HTMLElement>("[data-cloud-rank]");
    if (rank) {
      model.overviewUi.rank =
        rank.dataset.cloudRank === "model" ? "model" : "key";
      render();
      return;
    }
    const drill = target.closest<HTMLElement>("[data-cloud-drill]");
    if (drill) {
      drillInto(drill.dataset.cloudDrill ?? "");
      return;
    }
    if (target.closest("[data-cloud-usage-reset]")) {
      model.usageUi.key = "";
      model.usageUi.model = "";
      model.usageUi.result = "";
      model.usageUi.time = { kind: "14", day: "" };
      model.usageUi.page = 1;
      model.usageUi.open = null;
      requests.invalidate();
      model.usageRequests = null;
      model.usageRequestsUi = {
        result: "",
        time: "today",
        cursors: [null],
        page: 0,
        loading: false,
      };
      render();
      return;
    }
    const usageDim = target.closest<HTMLElement>("[data-cloud-usage-dim]");
    if (usageDim) {
      model.usageUi.dimension =
        usageDim.dataset.cloudUsageDim === "model"
          ? "model"
          : usageDim.dataset.cloudUsageDim === "day"
            ? "day"
            : "key";
      model.usageUi.page = 1;
      model.usageUi.open = null;
      if (model.usageUi.dimension === "model") {
        model.usageUi.model = "";
        model.usageUi.time = { kind: "all", day: "" };
      }
      if (model.usageUi.dimension === "day") model.usageUi.model = "";
      if (model.usageUi.dimension === "key") model.usageUi.model = "";
      render();
      return;
    }
    const pager = target.closest<HTMLButtonElement>("[data-cloud-usage-page]");
    if (pager) {
      model.usageUi.page = Math.max(
        1,
        model.usageUi.page + Number(pager.dataset.cloudUsagePage || "0")
      );
      render();
      return;
    }
    const open = target.closest<HTMLElement>("[data-cloud-usage-open]");
    if (open) {
      model.usageUi.open = open.dataset.cloudUsageOpen || null;
      render();
      return;
    }
    if (target.closest("[data-cloud-usage-close]")) {
      model.usageUi.open = null;
      render();
      return;
    }
    const keyAction = target.closest<HTMLElement>("[data-cloud-key-action]");
    if (keyAction) {
      void keys.keyActionFor(keyAction.dataset.cloudKeyAction ?? "");
      return;
    }
  });
  function parseUsageTime(value: string): UsageTime {
    if (value === "today") return { kind: "today", day: "" };
    if (value === "7" || value === "14" || value === "30")
      return { kind: value, day: "" };
    if (value.startsWith("day:")) return { kind: "day", day: value.slice(4) };
    return { kind: "all", day: "" };
  }
  function drillInto(raw: string): void {
    const [kind, ...rest] = raw.split(":");
    const id = rest.join(":");
    if (!id) return;
    model.usageUi.open = null;
    model.usageUi.page = 1;
    if (kind === "key") {
      model.usageUi.dimension = "key";
      model.usageUi.key = id;
      model.usageUi.model = "";
      model.usageUi.time = { kind: "14", day: "" };
    } else if (kind === "model") {
      model.usageUi.dimension = "model";
      model.usageUi.key = "";
      model.usageUi.model = id.startsWith("unattributed:")
        ? id.slice("unattributed:".length)
        : id;
      model.usageUi.time = { kind: "all", day: "" };
    } else if (kind === "day") {
      model.usageUi.dimension = "day";
      model.usageUi.key = "";
      model.usageUi.model = "";
      model.usageUi.time = { kind: "day", day: id };
    } else return;
    if (mobile.matches) setDrawer(false, false);
    // The single page owns every drill-down: it must not write an old area
    // hash, because the hashchange handler clears this page's revealed Key
    // plaintext, the plain dialog and the open Key detail.
    render();
  }
  root.addEventListener("change", (event) => {
    const target = event.target as Element;
    const usageTime = target.closest<HTMLSelectElement>(
      "[data-cloud-usage-time]"
    );
    if (usageTime) {
      model.usageUi.time = parseUsageTime(usageTime.value);
      if (model.usageUi.dimension === "model") model.usageUi.dimension = "key";
      model.usageUi.page = 1;
      model.usageUi.open = null;
      render();
      return;
    }
    const usageKey = target.closest<HTMLSelectElement>(
      "[data-cloud-usage-key]"
    );
    if (usageKey) {
      model.usageUi.key = usageKey.value;
      model.usageUi.page = 1;
      model.usageUi.open = null;
      render();
      return;
    }
    const usageModel = target.closest<HTMLSelectElement>(
      "[data-cloud-usage-model]"
    );
    if (usageModel) {
      model.usageUi.model = usageModel.value;
      model.usageUi.page = 1;
      model.usageUi.open = null;
      if (usageModel.value) {
        model.usageUi.dimension = "model";
        model.usageUi.time = { kind: "all", day: "" };
      }
      render();
      return;
    }  });

  // Action menus are gone: every key action lives in its detail row, which is
  // rendered from the model (no transient <details> state to preserve).

  // Chart tooltip: one plate for every mark, by pointer or keyboard focus.
  const showTip = (slot: Element | null, x: number, y: number) => {
    const text = slot?.getAttribute("data-tip");
    if (!text) {
      tip.hidden = true;
      return;
    }
    tip.textContent = text;
    tip.hidden = false;
    const width = tip.offsetWidth;
    tip.style.left = `${Math.min(window.innerWidth - width - 8, Math.max(8, x - width / 2))}px`;
    tip.style.top = `${Math.max(8, y - tip.offsetHeight - 10)}px`;
  };
  content.addEventListener("pointermove", (event) =>
    showTip(
      (event.target as Element).closest("[data-tip]"),
      event.clientX,
      event.clientY
    )
  );
  content.addEventListener("pointerleave", () => {
    tip.hidden = true;
  });
  content.addEventListener("focusin", (event) => {
    const slot = (event.target as Element).closest("[data-tip]");
    if (!slot) {
      tip.hidden = true;
      return;
    }
    const box = slot.getBoundingClientRect();
    showTip(slot, box.left + box.width / 2, box.top);
  });
  content.addEventListener("focusout", () => {
    tip.hidden = true;
  });
  // Drawer: Escape closes, Tab stays inside while open, and a desktop resize
  // restores the static rail.
  root.addEventListener("keydown", (event) => {
    if (!drawerOpen) return;
    if (event.key === "Escape") {
      event.preventDefault();
      setDrawer(false);
      return;
    }
    if (event.key !== "Tab") return;
    const focusables = [
      ...nav.querySelectorAll<HTMLElement>(
        "button:not([disabled]), [href], input:not([disabled]), select:not([disabled])"
      ),
    ].filter((element) => element.offsetParent !== null);
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  const onViewportChange = () => {
    if (!mobile.matches) setDrawer(false, false);
    else if (!drawerOpen) nav.setAttribute("aria-hidden", "true");
  };
  // D18-13: the trend chart is redrawn at the container's own width instead
  // of stretching one fixed 320-unit viewBox. The measured width is capped so a
  // very wide screen does not turn into a 4000-unit drawing.
  const fitChart = () => {
    if (area !== "overview") return;
    const svg = content.querySelector<SVGSVGElement>("svg.cloud-chart");
    const box = svg?.parentElement?.getBoundingClientRect() ?? null;
    const width = box && box.width > 0 ? Math.round(box.width - 32) : 0;
    const next = Math.max(320, Math.min(960, width));
    if (!next || model.overviewUi.chartWidth === next) return;
    model.overviewUi.chartWidth = next;
    render();
  };
  const chartObserver =
    typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(() => fitChart());
  chartObserver?.observe(content);
  window.addEventListener("resize", fitChart);
  mobile.addEventListener("change", onViewportChange);
  window.addEventListener("hashchange", () => {
    area = areaFromHash();
    tip.hidden = true;
    if (drawerOpen) setDrawer(false, false);
    keyUi.secrets.clear();
    keyUi.plain = null;
    keyUi.open = null;
    plainValue.textContent = "";
    if (plainDialog.open) plainDialog.close("navigated");
    render();
    root.querySelector(".shell-main")!.scrollTop = 0;
  });
  window.addEventListener("pagehide", () => {
    // A page kept in the back/forward cache must not restore a visible secret:
    // clear the memory and the dialog, then repaint the cards without it.
    keyUi.secrets.clear();
    keyUi.plain = null;
    plainValue.textContent = "";
    if (plainDialog.open) plainDialog.close("pagehide");
    render();
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) render();
  });
  gate.onChange(paintRole);
  paintRole(gate.verdict());
  setDrawer(false, false);
  void refresh().then(() => {
    // Ticket #9: the request detail is part of the page, so its first page is
    // read once on mount; later 刷新 keeps the current filter and page.
    if (model.usageRequests === null) void requests.loadRequestsPage(0, true);
  });
  // Polling has a lifecycle: it pauses while the page is hidden and catches up
  // once it is visible again.
  window.setInterval(() => {
    if (!document.hidden) void refresh();
  }, REFRESH_MS);
  document.addEventListener("visibilitychange", () => {
    if (
      !document.hidden &&
      (lastRefresh === null || Date.now() - lastRefresh > REFRESH_MS)
    )
      void refresh();
  });
}
