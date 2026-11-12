import "./cloud.css";
import { installCloudBrand } from "./brand";
import { escapeHtml } from "../html";
import { fmtNum } from "../console/ui";
import type { RoleGate, RoleVerdict } from "../console/role-gate";
import type { ShareKeyRow, ShareStatusPayload } from "../console/share-model";
import type {
  ShareUsage,
  ShareUsageRequestsPage,
  CloudSecurityStatus,
} from "../console/backend/types";
import {
  cancelBackendLogin,
  FAILURE_WORD,
  lastValue,
  readActions,
  readDisplaySession,
  readDisplayState,
  readOperationById,
  readOperationByIdentity,
  readOperations,
  readShareKeySecret,
  readShareKeys,
  readShareStatus,
  readShareUsage,
  readShareUsageFailed,
  readShareUsageRequests,
  readSummary,
  safeDisplayUrl,
  shareKeyWrite,
  type ActionRow,
  type CloudHealth,
  type CloudSummary,
  type DisplaySession,
  type DisplayState,
  type KeySecretOutcome,
  type KeyWriteOutcome,
  type OperationRead,
  type OperationRow,
  type ReadState,
} from "./cloud-reads";
import {
  clock,
  overviewHtml,
  PRIMARY_FORCE,
  PRIMARY_START,
  PRIMARY_STOP,
  primaryAction,
  stageNote,
  type OverviewUi,
} from "./overview";
import { opLabel } from "./areas";
import {
  findOperationById,
  findOperationByIdentity,
  loadPendingIntents,
  newIntentKey,
  operationOutcome,
  savePendingIntents,
  submitBody,
  type Outcome,
  type PendingIntent,
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
/** Ticket #10: an inline test result stays visible for a few seconds, then the
 *  row's button returns to 测试. The result itself is never persisted. */
const KEY_TEST_RESULT_MS = 5_000;
const TERMINAL = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
/** Local wait per submitted operation; past it the result is reported as
 *  unconfirmed with its id — the page never re-sends a write. */
const WAIT_MS: Readonly<Record<string, number>> = {
  "share.backend_login": 15 * 60_000,
  "share.backend_reauthenticate": 5 * 60_000,
};

type ConfirmCopy = {
  title: string;
  body: string;
  confirm: string;
  detail?: string;
  danger?: boolean;
};
/** Confirmation copy for every write this console offers: target and effect. */
const CONFIRM: Readonly<Record<string, ConfirmCopy>> = {
  "share.backend_login|share": {
    title: "启动 UM 登录流程？",
    confirm: "开始登录",
    body: "登录窗口在云机上打开，需要私有显示通道；未接入时会超时并占用一次登录启动配额。登录/MFA 必须由你本人完成。",
  },
  "share.backend_reauthenticate|share": {
    title: "重新认证 UM？",
    danger: true,
    confirm: "重新认证",
    body: "只重新认证分享专用后端 8110；不冷启动、不重启进程、不绕过人工认证。需要显示会话时请先打开私有登录窗口。",
  },
  "share.status|share": {
    title: "分层检查（只读）？",
    confirm: "开始检查",
    body: "只读取当前服务各层状态并记录一次检查，不改变、不重启云端 API 服务。",
  },
  "share.chain_stop|share": {
    title: "停止 API 服务？",
    danger: true,
    confirm: "停止",
    body: "停止会先拒绝新请求，等待在途请求结束。若有请求未结束，会提示可以强制停止。确定停止？",
  },
};

export type KeyNotice = {
  tone: "ok" | "warn" | "err" | "neutral";
  text: string;
  at: number;
};
export type KeyTest = {
  state: "busy" | "ok" | "error";
  rowVersion?: number;
  reason?: string;
  stage?: string;
  httpStatus?: number;
  model?: string;
  elapsedMs?: number;
  publicIp?: string;
  from?: "public" | "private";
  at?: number;
};
export type KeyUi = {
  busy: Set<string>;
  notices: Map<string, KeyNotice>;
  secrets: Map<
    string,
    { secret: string; rowVersion: number; at: number; revealed: boolean }
  >;
  tests: Map<string, KeyTest>;
  /** The one key whose detail panel is open. */
  open: string | null;
  caps: Map<string, string>;
  createBusy: boolean;
  createIdem: string;
  createError: string | null;
  /** `viewable` is the backend's `plaintext_viewable` for the row just created
   *  or rotated; null means the answer did not report it. A created Key opens in
   *  full once so it can be copied; a rotated Key starts masked until asked. */
  plain: {
    kind: "created" | "rotated";
    name: string;
    value: string;
    at: number;
    hidden: boolean;
    viewable: boolean | null;
  } | null;
};
export type UsageTime = {
  kind: "all" | "today" | "7" | "14" | "30" | "day";
  day: string;
};
export type UsageDimension = "key" | "model" | "day";
export type UsageUi = {
  dimension: UsageDimension;
  key: string;
  model: string;
  time: UsageTime;
  page: number;
  open: string | null;
  /** D18-2: the result filter opened from the failure summary; "" = all. */
  result: string;
};

/** Ticket #9: the request-detail result filter. `unknown` is the ledger's
 *  "result was never recorded" outcome; it is never folded into success. */
export type RequestsResultFilter =
  "" | "success" | "failed" | "unknown" | "cancelled";
export type RequestsTimeRange = "today" | "7" | "30";
export type RequestsUi = {
  result: RequestsResultFilter;
  time: RequestsTimeRange;
  /** The cursor used to load each visited page; index 0 is the first page. */
  cursors: (string | null)[];
  /** Zero-based index into `cursors`. */
  page: number;
  loading: boolean;
};

export interface CloudModel {
  summary: ReadState<CloudSummary>;
  health: ReadState<CloudHealth>;
  actions: ReadState<ActionRow[]>;
  operations: ReadState<OperationRow[]>;
  status: ReadState<ShareStatusPayload>;
  keys: ReadState<ShareKeyRow[]>;
  usage: ReadState<ShareUsage>;
  /** Ticket #8: today's failed requests, from the backend's `result=failed`
   *  aggregate read. A failed or stale read stays unknown, never 0. */
  usageFailed: ReadState<ShareUsage>;
  /** Per-request page, loaded on demand from /share-usage/requests. */
  usageRequests: ReadState<ShareUsageRequestsPage> | null;
  /** Ticket #9: result/time filters and cursor-backed pages. */
  usageRequestsUi: RequestsUi;
  /** D18-11: only the security read face remains. */
  security: ReadState<CloudSecurityStatus>;
  display: ReadState<DisplaySession>;
  /** D18-7/D18-8/D18-9: the read-only display auth/operator snapshot. */
  displayState: ReadState<DisplayState>;
  verdict: RoleVerdict;
  overviewUi: OverviewUi;
  usageUi: UsageUi;
  /** Ticket #5: whether the status row's evidence is expanded. */
  statusOpen: boolean;
  /** Ticket #6: the primary start this page submitted and has not settled yet;
   *  the operations read alone lags behind the click. */
  primaryPending?: { at: number; stage: string } | null;
  keyUi: KeyUi;
  feedback: readonly Feedback[];
  /** A write button: enabled only when the role is confirmed, the action is
   *  registered and enabled, and no submit of it is in flight. */
  actionButton(
    action: string,
    target: string,
    html: string,
    cls: string
  ): string;
  /** Share-key writes need the confirmed backend role too. */
  writeReady(): { ok: boolean; reason: string };
  keyBusy(id: string): boolean;
  keyNotice(id: string): KeyNotice | undefined;
  keySecret(
    id: string
  ):
    | { secret: string; rowVersion: number; at: number; revealed: boolean }
    | undefined;
  keyTest(id: string): KeyTest | undefined;
  keyCapValue(id: string): string | undefined;
  /** The page's own latest test of one scope; never persisted evidence. */
  latestKeyTest(
    from: "public" | "private"
  ): { name: string; test: KeyTest } | null;
}

export type Feedback = {
  key: string;
  action: string;
  target: string;
  tone: "ok" | "warn" | "err" | "neutral";
  text: string;
  opId: string | null;
  at: number;
  done: boolean;
  /** A2: the stable, non-secret identity this page can re-query. Null once the
   *  backend returned a terminal result (or the intent was abandoned). */
  intentKey: string | null;
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === "object" && !Array.isArray(value);

/** A2: only an explicit 4xx refusal proves the operation never ran. A 408, a
 *  429, a 5xx, a 2xx without an operation id or an unparseable answer may
 *  still have been accepted, so the intent keeps its idempotency identity and is
 *  re-queried instead of re-submitted. */
const REFUSAL_STATUSES: ReadonlySet<number> = new Set([
  400, 401, 403, 404, 409, 410, 422,
]);

export type SubmitAnswer =
  | { kind: "accepted"; operation: OperationRow }
  | { kind: "refused"; reason: string }
  | { kind: "unconfirmed"; reason: string };

export function submitAnswer(
  status: number,
  data: unknown,
  action: string,
  target: string
): SubmitAnswer {
  const record = isRecord(data) ? data : null;
  const operation =
    record &&
    isRecord(record.operation) &&
    typeof record.operation.id === "string" &&
    record.operation.id
      ? (record.operation as unknown as OperationRow)
      : null;
  if (operation) {
    // A backend operation id is accepted only when it names THIS intent; a
    // foreign operation's id is an unconfirmed identity, never our result.
    if (
      (operation.action && operation.action !== action) ||
      (operation.target ?? "") !== target
    ) {
      return { kind: "unconfirmed", reason: "operation_identity_mismatch" };
    }
    return { kind: "accepted", operation };
  }
  const reason =
    (record &&
      ((typeof record.reason === "string" && record.reason) ||
        (typeof record.detail === "string" && record.detail) ||
        (typeof record.error === "string" && record.error))) ||
    `HTTP ${status}`;
  if (REFUSAL_STATUSES.has(status)) return { kind: "refused", reason };
  return { kind: "unconfirmed", reason };
}

/** D18-4: the create dialog carries only a name and a concurrency cap. An
 *  empty cap is refused like any other non-integer: the dialog's own default 16
 *  is what the user sees prefilled, so an empty field is a real input, not the
 *  documented default. A value outside 1–48 or a non-integer is refused instead
 *  of being silently replaced. */
export type CreateSubmission =
  | {
      kind: "invalid";
      field: "name" | "max_concurrency";
      reason: string;
    }
  | { kind: "ready"; body: { name: string; max_concurrency: number } };

export function submitCreate(
  nameRaw: string,
  capRaw: string
): CreateSubmission {
  const name = nameRaw.trim();
  if (!name || name.length > 64)
    return {
      kind: "invalid",
      field: "name",
      reason: "名称需要 1–64 个字符",
    };
  const raw = capRaw.trim();
  const cap = Number(raw);
  if (!/^\d+$/.test(raw) || !Number.isInteger(cap) || cap < 1 || cap > 48)
    return {
      kind: "invalid",
      field: "max_concurrency",
      reason: "并发上限必须是 1–48 的整数（默认 16）",
    };
  return { kind: "ready", body: { name, max_concurrency: cap } };
}

/** D18-5: the inline concurrency edit saves on change. A value outside 1–48
 *  or a non-integer is refused; the row's current value is not a write. */
export type ConcurrencySubmission =
  | { kind: "invalid"; reason: string }
  | { kind: "ready"; value: number; unchanged: boolean };

export function submitConcurrency(
  raw: string,
  current: number | undefined
): ConcurrencySubmission {
  const text = raw.trim();
  const value = Number(text);
  if (
    !/^\d+$/.test(text) ||
    !Number.isInteger(value) ||
    value < 1 ||
    value > 48
  )
    return { kind: "invalid", reason: "并发上限必须是 1–48 的整数" };
  return { kind: "ready", value, unchanged: value === current };
}

/** D18-6: revoke is the tombstone route; the only body is the optimistic
 *  row version. The backend writes `deleted_at` and clears the ciphertext. */
export type RevokeSubmission =
  | { kind: "invalid"; reason: string }
  | {
      kind: "ready";
      path: string;
      body: { expected_row_version: number };
    };

export function revokeKey(
  id: string,
  rowVersion: number | undefined
): RevokeSubmission {
  if (!id) return { kind: "invalid", reason: "缺少 Key 标识" };
  if (
    typeof rowVersion !== "number" ||
    !Number.isInteger(rowVersion) ||
    rowVersion < 1
  )
    return { kind: "invalid", reason: "缺少行版本，请刷新列表后重试" };
  return {
    kind: "ready",
    path: `/api/console/share-management/keys/${encodeURIComponent(id)}/revoke-and-terminate`,
    body: { expected_row_version: rowVersion },
  };
}

/** D18-4: the gateway's own concurrency refusals, in operator words. The
 *  backend code stays verbatim next to this word; it is never "unknown". */
const REASON_WORD: Readonly<Record<string, string>> = {
  share_concurrency: "并发已满（分享网关全局并发）",
  per_key_concurrency: "并发已满（该 Key 的并发上限）",
};
const reasonWord = (reason: string): string => REASON_WORD[reason] ?? reason;

const TEST_REASON: Readonly<Record<string, string>> = {
  unconfirmed: "后端未确认连接结果",
  test_in_progress: "同一 Key 已有测试在进行",
  key_disabled: "Key 已停用",
  public_url_unavailable: "后端没有可用的公网 API 地址",
  gateway_unconfirmed: "API 入口运行身份未确认",
  key_changed: "测试期间 Key 已变化，请刷新后重试",
  secret_unavailable: "Key 明文不可读取，无法测试",
};

/** R4: the create dialog's note is capability-neutral. The backend may or may
 *  not keep a ciphertext it can re-read, so it never promises a fixed one-time
 *  view. The created plaintext is shown in this dialog at once (not masked), and
 *  the single page has no detail view — a later look is 点开该行. */
export const CREATE_PLAIN_NOTE =
  "只创建长期 Key；不设有效期或 Token 配额。明文在本次弹层直接显示；后端保存可回查密文时可再点开该行查看，否则关闭后只能轮换。";
/** R4: the plain dialog's static warning. Whether a later readback exists is
 *  the backend's capability, never a fixed claim. */
export const PLAIN_DIALOG_NOTE =
  "明文只保存在本页内存；能否再次查看取决于后端能力，请先复制保存。";

/** R4: one capability-consistent note after a create/rotate. `viewable` is the
 *  answer's key row `plaintext_viewable`; null means it was not reported. The
 *  note never says a second view is always impossible: the backend allows it for
 *  a viewable row, otherwise the Key has to be rotated. */
export function plainNotice(
  kind: "created" | "rotated",
  viewable: boolean | null
): string {
  const head =
    kind === "created"
      ? "这是新建 Key 的明文；"
      : "旧 Key 已失效；这是新 Key 的明文；";
  if (viewable === true)
    return `${head}后端保存了可回查的密文，关闭后可再点开该行查看。`;
  if (viewable === false)
    return `${head}后端未保存可回查的密文，关闭后无法再次查看；需要时只能轮换。`;
  return `${head}后端未上报明文回查能力，关闭后不保证可再次查看；需要时只能轮换。`;
}

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
          reason: "候选预览是只读的：写操作请在云端 18096 生产页面执行",
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
  <dialog class="shell-confirm" aria-labelledby="cloud-confirm-title"><form method="dialog"><h2 id="cloud-confirm-title"></h2><p class="shell-confirm-body"></p><p class="shell-confirm-target mono"></p><div class="shell-confirm-actions"><button value="cancel" class="btn btn-outline">取消</button><button value="force" class="btn btn-danger-fill" data-cloud-confirm-force hidden>强制停止</button><button value="confirm" class="btn btn-primary"></button></div></form></dialog>
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

  function areaFromHash(): Area {
    const id = location.hash.replace(/^#/, "");
    return AREAS.some((a) => a.id === id) ? (id as Area) : "overview";
  }

  function writeBlock(action: string, target: string): string | null {
    if (__CONSOLE_CANDIDATE__)
      return "候选预览是只读的：写操作请在云端 18096 生产页面执行";
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
          if (key === "keys") reconcileSecrets();
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
        void loadRequestsPage(model.usageRequestsUi.page);
      void reconcileIntents();
    } finally {
      refreshing = false;
    }
  }

  async function refreshKeys(): Promise<void> {
    model.keys = await readShareKeys(model.keys);
    reconcileSecrets();
    render();
  }
  async function refreshUsage(): Promise<void> {
    model.usage = await readShareUsage(model.usage);
    model.usageFailed = await readShareUsageFailed(model.usageFailed);
    render();
  }

  /** D18-7/D18-8: re-read the display session and the display-state snapshot.
   *  A read never triggers a login and never extends the window. */
  async function refreshDisplay(): Promise<void> {
    model.display = await readDisplaySession(model.display);
    model.displayState = await readDisplayState(model.displayState);
    render();
  }

  function displayFeedback(
    tone: "ok" | "warn" | "err" | "neutral",
    text: string
  ): void {
    settle(
      addFeedback({
        action: "share.display",
        target: "share",
        tone,
        text,
        opId: null,
        intentKey: null,
      }),
      tone,
      text
    );
  }

  /** Ticket #5: open the cloud host's private display channel. The page only
   *  opens a same-origin URL without a credential-looking parameter; opening
   *  the window is never claimed as a successful UM login. */
  async function openDisplayWindow(): Promise<void> {
    const read = await readDisplaySession(model.display);
    model.display = read;
    if (read.state !== "ready") {
      const reason =
        read.state === "failed"
          ? `${FAILURE_WORD[read.kind]}（${clock(read.at)}）：${read.reason}`
          : "私有登录窗口接口尚未返回";
      displayFeedback("warn", `未打开：${reason}`);
      render();
      return;
    }
    const value = read.value;
    if (value.available !== true) {
      displayFeedback(
        "warn",
        `未打开：${value.reason || "后端没有可用的私有登录窗口"}`
      );
      render();
      return;
    }
    const url = safeDisplayUrl(value.url);
    if (!url) {
      displayFeedback(
        "warn",
        "后端返回的私有登录窗口地址不是同源路径或含凭据参数，未打开"
      );
      render();
      return;
    }
    const opened = window.open(url, "_blank", "noopener,noreferrer");
    displayFeedback(
      "neutral",
      opened === null
        ? "已请求打开私有登录窗口；若没有出现，请允许本站弹出窗口后重试。打开窗口不等于 UM 认证成功"
        : "已打开私有登录窗口；打开窗口不等于 UM 认证成功，关闭后请重新检查"
    );
    render();
  }

  /** D18-8: cancel THIS login round only. The backend cleans the browser and
   *  reports `cancelled` / `no_login_in_progress`; it never stops the dedicated
   *  instance and never reopens a round by itself. */
  async function cancelLogin(): Promise<void> {
    const ready = model.writeReady();
    if (!ready.ok) {
      displayFeedback("warn", `未取消：${ready.reason}`);
      return;
    }
    if (keyUi.busy.has("display:cancel")) return;
    keyUi.busy.add("display:cancel");
    render();
    const outcome = await cancelBackendLogin();
    keyUi.busy.delete("display:cancel");
    if (!outcome.ok) {
      displayFeedback(
        outcome.kind === "no-answer" ? "warn" : "err",
        `取消登录${outcome.kind === "no-answer" ? "结果未确认" : "失败"}：${outcome.reason}`
      );
      void refreshDisplay();
      return;
    }
    const state =
      typeof outcome.data.state === "string" ? outcome.data.state : "";
    const detail =
      typeof outcome.data.detail === "string" && outcome.data.detail
        ? `；${outcome.data.detail}`
        : "";
    displayFeedback(
      outcome.data.cancelled === true ? "neutral" : "warn",
      outcome.data.cancelled === true
        ? `本轮登录已取消${state ? `（${state}）` : ""}；不停止服务，也不会自动重开新一轮${detail}`
        : `当前没有正在进行的登录，未取消任何一轮${detail}`
    );
    void refreshDisplay();
  }
  const REQUESTS_WINDOW: Readonly<Record<RequestsTimeRange, string>> = {
    // "today" is the backend's Beijing calendar day, not a rolling 24h window.
    today: "today",
    "7": "7d",
    "30": "30d",
  };
  /** Ticket #9: one cursor-backed page of request details. A filter change
   *  resets the cursor stack; a stale in-flight answer is dropped by sequence so
   *  it can never repaint rows for a filter the reader already left. */
  let requestsSeq = 0;
  async function loadRequestsPage(page: number, reset = false): Promise<void> {
    const ui = model.usageRequestsUi;
    if (reset) {
      ui.cursors = [null];
      ui.page = 0;
      page = 0;
    }
    const index = Math.max(0, page);
    if (index > 0 && !ui.cursors[index]) return;
    const seq = ++requestsSeq;
    ui.loading = true;
    model.usageRequests = { state: "loading" };
    render();
    const params: {
      window: string;
      result?: string;
      cursor?: string;
      limit: number;
    } = {
      window: REQUESTS_WINDOW[ui.time],
      limit: 50,
    };
    if (ui.result) params.result = ui.result;
    const cursor = ui.cursors[index];
    if (cursor) params.cursor = cursor;
    const read = await readShareUsageRequests(
      model.usageRequests ?? { state: "loading" },
      params
    );
    if (seq !== requestsSeq) return;
    model.usageRequests = read;
    ui.loading = false;
    if (read.state === "ready") {
      ui.page = index;
      ui.cursors = ui.cursors.slice(0, index + 1);
      if (read.value.nextCursor) ui.cursors[index + 1] = read.value.nextCursor;
    }
    render();
  }

  function reconcileSecrets(keys = lastValue(model.keys)?.value): void {
    if (!keys) return;
    const byId = new Map(keys.map((row) => [row.id, row]));
    for (const [id, entry] of keyUi.secrets) {
      const row = byId.get(id);
      if (!row) {
        // The Key left the list (revoked or deleted): its plaintext must not
        // stay on screen.
        keyUi.secrets.delete(id);
        keyUi.tests.delete(id);
        continue;
      }
      // Ticket #5 / spec 180: 明文不会自动遮回 —— 改行内并发会让
      // row_version 前移，但密文本身没变，明文要留在屏幕上直到再点该行。
      // 存的版本跟着行前移，后续读取用当前版本比较。
      if (
        entry.rowVersion !== null &&
        typeof row.row_version === "number" &&
        row.row_version !== entry.rowVersion
      )
        entry.rowVersion = row.row_version;
    }
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

  function setNotice(id: string, tone: KeyNotice["tone"], text: string): void {
    keyUi.notices.set(id, { tone, text, at: Date.now() });
    render();
  }
  function clearNotice(id: string): void {
    keyUi.notices.delete(id);
    render();
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
      const isLogin =
        action === "share.backend_login" ||
        action === "share.backend_reauthenticate";
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

  function confirmWith(
    copy: ConfirmCopy,
    run: () => void,
    force?: { label: string; run: () => void }
  ): void {
    confirmDialog.querySelector("h2")!.textContent = copy.title;
    confirmDialog.querySelector(".shell-confirm-body")!.textContent = copy.body;
    confirmDialog.querySelector(".shell-confirm-target")!.textContent =
      copy.detail ?? "";
    const ok = confirmDialog.querySelector<HTMLButtonElement>(
      'button[value="confirm"]'
    )!;
    ok.textContent = copy.confirm;
    ok.className = copy.danger ? "btn btn-danger-fill" : "btn btn-primary";
    const forceButton = confirmDialog.querySelector<HTMLButtonElement>(
      "[data-cloud-confirm-force]"
    )!;
    forceButton.hidden = !force;
    forceButton.textContent = force?.label ?? "";
    confirmDialog.returnValue = "";
    confirmDialog.onclose = () => {
      if (confirmDialog.returnValue === "confirm") run();
      else if (confirmDialog.returnValue === "force") force?.run();
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
    const force =
      action === PRIMARY_STOP && primaryAction(model).force
        ? {
            label: "强制停止",
            run: () => void submit(PRIMARY_FORCE, target),
          }
        : undefined;
    confirmWith(
      { ...copy, detail: `动作 ${action} · 目标 ${target}` },
      () => void submit(action, target),
      force
    );
  }

  async function copyText(text: string): Promise<boolean> {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      return false;
    }
  }

  function setCreateError(text: string): void {
    keyUi.createError = text || null;
    createErrorBox.textContent = text;
  }

  function openCreate(): void {
    const writable = model.writeReady();
    if (!writable.ok) {
      const text = `未打开新建：${writable.reason}`;
      settle(
        addFeedback({
          action: "share.key_create",
          target: "share",
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
    if (!keyUi.createIdem) keyUi.createIdem = newIntentKey();
    createForm.reset();
    const capInput = createForm.querySelector<HTMLInputElement>(
      'input[name="max_concurrency"]'
    );
    if (capInput) capInput.value = "16";
    setCreateError("");
    createDialog.showModal();
    createForm.querySelector<HTMLInputElement>('input[name="name"]')?.focus();
  }

  async function createKey(): Promise<void> {
    if (keyUi.createBusy) return;
    const ready = model.writeReady();
    if (!ready.ok) {
      setCreateError(ready.reason);
      return;
    }
    const nameInput =
      createForm.querySelector<HTMLInputElement>('input[name="name"]');
    const capInput = createForm.querySelector<HTMLInputElement>(
      'input[name="max_concurrency"]'
    );
    if (!nameInput || !capInput) return;
    const submission = submitCreate(nameInput.value, capInput.value);
    if (submission.kind === "invalid") {
      setCreateError(submission.reason);
      (submission.field === "name" ? nameInput : capInput).focus();
      return;
    }
    const name = submission.body.name;
    keyUi.createBusy = true;
    createSubmit.disabled = true;
    createSubmit.textContent = "创建中…";
    setCreateError("");
    const body: Record<string, unknown> = {
      name,
      max_concurrency: submission.body.max_concurrency,
      idempotency_key: keyUi.createIdem,
    };
    const outcome = await shareKeyWrite(
      "POST",
      "/api/console/share-management/keys",
      body
    );
    keyUi.createBusy = false;
    createSubmit.disabled = false;
    createSubmit.textContent = "创建";
    if (!outcome.ok) {
      setCreateError(
        outcome.kind === "no-answer"
          ? `结果未确认：${outcome.reason}；同一幂等键已保留，可安全重试`
          : `创建失败：${outcome.reason}`
      );
      if (outcome.kind === "no-answer") void refreshKeys();
      else keyUi.createIdem = newIntentKey();
      return;
    }
    const key = isRecord(outcome.data.key) ? outcome.data.key : null;
    const plaintext =
      typeof outcome.data.plaintext === "string" ? outcome.data.plaintext : "";
    const createdName =
      typeof key?.name === "string" && key.name ? key.name : name;
    const viewable =
      typeof key?.plaintext_viewable === "boolean"
        ? key.plaintext_viewable
        : null;
    keyUi.createIdem = newIntentKey();
    createDialog.close("created");
    if (plaintext) openPlain("created", createdName, plaintext, viewable);
    else {
      const text =
        "已创建，但响应缺少明文；请再点开该行尝试查看明文（后端允许时），否则轮换生成";
      settle(
        addFeedback({
          action: "share.key_create",
          target: "share",
          tone: "warn",
          text,
          opId: null,
          intentKey: null,
        }),
        "warn",
        text
      );
    }
    void refreshKeys();
    void refreshUsage();
  }

  function openPlain(
    kind: "created" | "rotated",
    name: string,
    value: string,
    viewable: boolean | null
  ): void {
    // D18-4: a freshly created Key is shown in full once so the operator can
    // copy it before closing. A rotated Key keeps the old R4 masked-until-asked
    // behaviour; the value stays in this page's memory only.
    keyUi.plain = {
      kind,
      name,
      value,
      at: Date.now(),
      hidden: kind !== "created",
      viewable,
    };
    renderPlain();
    plainDialog.showModal();
    (keyUi.plain.hidden ? plainToggle : plainCopy).focus();
  }

  function renderPlain(): void {
    const plain = keyUi.plain;
    if (!plain) return;
    plainTitle.textContent = `${plain.kind === "created" ? "已创建" : "已轮换"} API Key：${plain.name || "（未命名）"}`;
    plainValue.textContent = plain.hidden ? "••••••••••••••••" : plain.value;
    plainToggle.hidden = plain.kind === "created";
    plainToggle.textContent = plain.hidden ? "显示明文" : "隐藏明文";
    plainToggle.setAttribute("aria-pressed", String(!plain.hidden));
    plainNote.textContent = plainNotice(plain.kind, plain.viewable);
  }

  async function revealSecret(id: string): Promise<void> {
    const row = keyRow(id);
    if (!row) {
      setNotice(id, "warn", "Key 列表已变化，找不到该 Key");
      return;
    }
    if (row.plaintext_viewable === false) {
      setNotice(id, "warn", "后端未保存可回查的密文；只能轮换生成新 Key");
      return;
    }
    if (row.plaintext_viewable !== true) {
      setNotice(
        id,
        "warn",
        "明文状态未上报：不假设可查看，也不自动轮换；请刷新列表核对"
      );
      return;
    }
    const rowVersion = row.row_version;
    if (
      typeof rowVersion !== "number" ||
      !Number.isInteger(rowVersion) ||
      rowVersion < 1
    ) {
      setNotice(id, "warn", "缺少行版本，请刷新列表后重试");
      return;
    }
    const busyKey = `secret:${id}`;
    if (keyUi.busy.has(busyKey) || keyUi.secrets.has(id)) return;
    keyUi.busy.add(busyKey);
    render();
    const outcome = await readShareKeySecret(id, rowVersion);
    keyUi.busy.delete(busyKey);
    if (!outcome.ok) {
      setNotice(
        id,
        outcome.kind === "no-answer" ? "warn" : "err",
        secretFailure(outcome)
      );
      return;
    }
    keyUi.secrets.set(id, {
      secret: outcome.secret,
      rowVersion,
      at: Date.now(),
      revealed: true,
    });
    keyUi.notices.delete(id);
    render();
  }

  /** D18-7: clicking the row toggles the plaintext in place. The secret stays
   *  revealed until the operator clicks the row again; there is no auto-remask. */
  function toggleReveal(id: string): void {
    if (keyUi.secrets.has(id)) {
      keyUi.secrets.delete(id);
      render();
      return;
    }
    void revealSecret(id);
  }
  function secretFailure(
    outcome: Extract<KeySecretOutcome, { ok: false }>
  ): string {
    const reason = outcome.reason;
    if (reason === "legacy") return "旧密钥不可恢复，手动轮换后可见";
    if (reason === "undecryptable")
      return "密文不可解密（换身份或迁机后不可恢复），手动轮换后可见";
    if (reason === "version_conflict") return "列表版本已变化，刷新后重看";
    if (reason === "key_deleted") return "Key 已删除";
    if (reason === "key_not_found") return "Key 不存在，请刷新列表";
    return outcome.kind === "no-answer"
      ? `结果未确认：${reason}`
      : `明文读取失败：${reason}`;
  }

  function hideSecret(id: string): void {
    keyUi.secrets.delete(id);
    render();
  }
  function toggleSecret(id: string): void {
    const entry = keyUi.secrets.get(id);
    if (!entry) return;
    entry.revealed = !entry.revealed;
    render();
  }
  async function copySecret(id: string): Promise<void> {
    const entry = keyUi.secrets.get(id);
    if (!entry) return;
    const ok = await copyText(entry.secret);
    setNotice(
      id,
      ok ? "ok" : "warn",
      ok ? "已复制该 Key 明文" : "复制失败：浏览器拒绝写入剪贴板"
    );
  }

  async function saveCap(id: string): Promise<void> {
    const row = keyRow(id);
    if (!row) {
      setNotice(id, "warn", "Key 列表已变化，找不到该 Key");
      return;
    }
    const input = content.querySelector<HTMLInputElement>(
      `[data-cloud-key-cap-input="${CSS.escape(id)}"]`
    );
    const submission = submitConcurrency(
      input?.value ?? "",
      row.max_concurrency
    );
    if (submission.kind === "invalid") {
      setNotice(id, "warn", submission.reason);
      input?.focus();
      return;
    }
    if (submission.unchanged) {
      setNotice(id, "neutral", "并发上限没有变化");
      return;
    }
    await patchKey(
      id,
      { max_concurrency: submission.value },
      `并发上限已保存为 ${submission.value}`
    );
  }

  async function patchKey(
    id: string,
    patch: Record<string, unknown>,
    successText: string
  ): Promise<void> {
    const row = keyRow(id);
    if (!row) {
      setNotice(id, "warn", "Key 列表已变化，找不到该 Key");
      return;
    }
    const ready = model.writeReady();
    if (!ready.ok) {
      setNotice(id, "warn", ready.reason);
      return;
    }
    const rowVersion = row.row_version;
    if (
      typeof rowVersion !== "number" ||
      !Number.isInteger(rowVersion) ||
      rowVersion < 1
    ) {
      setNotice(id, "warn", "缺少行版本，请刷新列表后重试");
      return;
    }
    const busyKey = `patch:${id}`;
    if (keyUi.busy.has(busyKey)) return;
    keyUi.busy.add(busyKey);
    render();
    const outcome = await shareKeyWrite(
      "PATCH",
      `/api/console/share-management/keys/${encodeURIComponent(id)}`,
      {
        ...patch,
        expected_row_version: rowVersion,
      }
    );
    keyUi.busy.delete(busyKey);
    if (!outcome.ok) {
      keyFailure(id, outcome);
      return;
    }
    // A concurrency-only PATCH bumps row_version. The revealed plaintext is
    // still valid for this Key, so adopt the new version before the refresh
    // reconciles; otherwise reconcileSecrets would clear it and re-mask the row.
    const updatedKey = isRecord(outcome.data.key) ? outcome.data.key : null;
    const nextVersion =
      typeof updatedKey?.row_version === "number"
        ? updatedKey.row_version
        : null;
    const revealed = keyUi.secrets.get(id);
    if (revealed && nextVersion !== null) revealed.rowVersion = nextVersion;
    setNotice(id, "ok", successText);
    keyUi.caps.delete(id);
    void refreshKeys();
    void refreshUsage();
  }

  function keyFailure(
    id: string,
    outcome: Extract<KeyWriteOutcome, { ok: false }>
  ): void {
    // A failed write did not take effect: drop the optimistic input value so
    // the row shows the Key's actual concurrency again.
    keyUi.caps.delete(id);
    const text =
      outcome.kind === "conflict"
        ? `版本冲突：${outcome.reason}；列表已刷新，请按新版本重试`
        : outcome.kind === "no-answer"
          ? `结果未确认：${outcome.reason}；本页不会自动重发，请刷新列表核对后再操作`
          : `未完成：${reasonWord(outcome.reason)}`;
    keyUi.notices.set(id, {
      tone:
        outcome.kind === "no-answer" || outcome.kind === "conflict"
          ? "warn"
          : "err",
      text,
      at: Date.now(),
    });
    if (outcome.kind === "conflict" || outcome.kind === "no-answer")
      void refreshKeys();
    render();
  }

  async function rotateKey(id: string): Promise<void> {
    const row = keyRow(id);
    if (!row) {
      setNotice(id, "warn", "Key 列表已变化，找不到该 Key");
      return;
    }
    const rowVersion = row.row_version;
    if (
      typeof rowVersion !== "number" ||
      !Number.isInteger(rowVersion) ||
      rowVersion < 1
    ) {
      setNotice(id, "warn", "缺少行版本，请刷新列表后重试");
      return;
    }
    confirmWith(
      {
        title: "轮换这个 API Key？",
        danger: true,
        confirm: "轮换 Key",
        body: "轮换后旧 Key 立即失效；新明文在本次响应中显示并默认遮蔽。后端保存可回查密文时可在详情再次查看，否则关闭后只能再次轮换。需要先确保使用方可以更换 Key。",
        detail: `Key ${keyLabel(row, id)} · 行版本 ${rowVersion}`,
      },
      () =>
        void (async () => {
          const ready = model.writeReady();
          if (!ready.ok) {
            setNotice(id, "warn", ready.reason);
            return;
          }
          const busyKey = `rotate:${id}`;
          if (keyUi.busy.has(busyKey)) return;
          keyUi.busy.add(busyKey);
          render();
          const outcome = await shareKeyWrite(
            "POST",
            `/api/console/share-management/keys/${encodeURIComponent(id)}/rotate`,
            { expected_row_version: rowVersion }
          );
          keyUi.busy.delete(busyKey);
          if (!outcome.ok) {
            keyFailure(id, outcome);
            return;
          }
          keyUi.secrets.delete(id);
          keyUi.notices.delete(id);
          const plaintext =
            typeof outcome.data.plaintext === "string"
              ? outcome.data.plaintext
              : "";
          const key = isRecord(outcome.data.key) ? outcome.data.key : null;
          const name =
            typeof key?.name === "string" && key.name
              ? key.name
              : keyLabel(row, id);
          const viewable =
            typeof key?.plaintext_viewable === "boolean"
              ? key.plaintext_viewable
              : null;
          void refreshKeys();
          if (plaintext) openPlain("rotated", name, plaintext, viewable);
          else
            setNotice(
              id,
              "warn",
              "已轮换，但响应缺少明文；请在该 Key 详情尝试查看明文（后端允许时），否则再次轮换"
            );
        })()
    );
  }

  async function revokeKeyAction(id: string): Promise<void> {
    const row = keyRow(id);
    if (!row) {
      setNotice(id, "warn", "Key 列表已变化，找不到该 Key");
      return;
    }
    const request = revokeKey(id, row.row_version);
    if (request.kind === "invalid") {
      setNotice(id, "warn", request.reason);
      return;
    }
    confirmWith(
      {
        title: "撤销这个 API Key？",
        danger: true,
        confirm: "撤销",
        body: "撤销后不可恢复，正在使用该 Key 的客户端会立即失败；该 Key 会从列表消失，历史用量仍保留。确定撤销？",
        detail: `Key ${keyLabel(row, id)}`,
      },
      () =>
        void (async () => {
          const ready = model.writeReady();
          if (!ready.ok) {
            setNotice(id, "warn", ready.reason);
            return;
          }
          const busyKey = `revoke:${id}`;
          if (keyUi.busy.has(busyKey)) return;
          keyUi.busy.add(busyKey);
          render();
          const outcome = await shareKeyWrite(
            "POST",
            request.path,
            request.body
          );
          keyUi.busy.delete(busyKey);
          if (!outcome.ok) {
            keyFailure(id, outcome);
            return;
          }
          // D18-3: the revoke answer separates what the management side
          // released (8110 PENDING/GRANTED), what it aborted (8110 COMMITTED)
          // and what 8100 closed upstream. A missing count stays 未知, never 0.
          const count = (name: string) => {
            const value = outcome.data[name];
            return typeof value === "number" && Number.isFinite(value)
              ? fmtNum(value)
              : "未上报";
          };
          const note =
            typeof outcome.data.note === "string" && outcome.data.note
              ? `；${outcome.data.note}`
              : "";
          keyUi.secrets.delete(id);
          // 撤销成功后这一行会从列表消失，所以结果写页级反馈，
          // 不写在即将被移除的行上。
          addFeedback({
            action: "share.key_revoke",
            target: id,
            tone: outcome.data.terminated === true ? "ok" : "warn",
            text: `已撤销：终止 ${count("terminated")} · 释放 ${count("released")} · 中止 ${count("aborted")} · 关闭上游 ${count("upstream_closed")}${note}`,
            opId: null,
            intentKey: null,
          });
          void refreshKeys();
        })()
    );
  }

  async function deleteKey(id: string): Promise<void> {
    const row = keyRow(id);
    if (!row) {
      setNotice(id, "warn", "Key 列表已变化，找不到该 Key");
      return;
    }
    const rowVersion = row.row_version;
    if (
      typeof rowVersion !== "number" ||
      !Number.isInteger(rowVersion) ||
      rowVersion < 1
    ) {
      setNotice(id, "warn", "缺少行版本，请刷新列表后重试");
      return;
    }
    confirmWith(
      {
        title: "删除这个 API Key？",
        danger: true,
        confirm: "删除 Key",
        body: "删除后该 Key 从列表移除，不能恢复；历史用量仍保留在分享账本中。需要终止在途请求时请先用撤销。",
        detail: `Key ${keyLabel(row, id)} · 行版本 ${rowVersion}`,
      },
      () =>
        void (async () => {
          const ready = model.writeReady();
          if (!ready.ok) {
            setNotice(id, "warn", ready.reason);
            return;
          }
          const busyKey = `delete:${id}`;
          if (keyUi.busy.has(busyKey)) return;
          keyUi.busy.add(busyKey);
          render();
          const outcome = await shareKeyWrite(
            "DELETE",
            `/api/console/share-management/keys/${encodeURIComponent(id)}`,
            { expected_row_version: rowVersion }
          );
          keyUi.busy.delete(busyKey);
          if (!outcome.ok) {
            keyFailure(id, outcome);
            return;
          }
          keyUi.secrets.delete(id);
          keyUi.notices.delete(id);
          void refreshKeys();
          const text = `已删除 Key ${keyLabel(row, id)}；历史用量仍在账本中`;
          settle(
            addFeedback({
              action: "share.key_delete",
              target: "share",
              tone: "ok",
              text,
              opId: null,
              intentKey: null,
            }),
            "ok",
            text
          );
          render();
        })()
    );
  }

  /** Ticket #10: a finished test keeps its word and elapsed time in the row's
   *  own button for a few seconds. A newer test replaces the entry, so the old
   *  timer cannot clear the new result. */
  function setKeyTest(id: string, test: KeyTest): void {
    keyUi.tests.set(id, test);
    render();
    if (test.state === "busy") return;
    window.setTimeout(() => {
      if (keyUi.tests.get(id) !== test) return;
      keyUi.tests.delete(id);
      render();
    }, KEY_TEST_RESULT_MS);
  }

  async function testKey(id: string, privateTest = false): Promise<void> {
    const row = keyRow(id);
    if (!row) {
      setNotice(id, "warn", "Key 列表已变化，找不到该 Key");
      return;
    }
    const ready = model.writeReady();
    if (!ready.ok) {
      setNotice(id, "warn", ready.reason);
      return;
    }
    const rowVersion = row.row_version;
    if (
      typeof rowVersion !== "number" ||
      !Number.isInteger(rowVersion) ||
      rowVersion < 1
    ) {
      setNotice(id, "warn", "缺少行版本，请刷新列表后重试");
      return;
    }
    if (row.enabled !== true) {
      setNotice(id, "warn", "Key 已停用，启用后才能测试");
      return;
    }
    if (keyUi.tests.get(id)?.state === "busy") return;
    const testedFrom = privateTest ? "private" : "public";
    setKeyTest(id, { state: "busy", rowVersion, from: testedFrom });
    const path = `/api/console/share-management/keys/${encodeURIComponent(id)}/test${privateTest ? "-private" : ""}`;
    const outcome = await shareKeyWrite(
      "POST",
      path,
      { expected_row_version: rowVersion },
      100_000
    );
    // Ticket #10: the test is one real inference through the share gateway, so
    // it is metered like any other request. Re-read the ledger and the request
    // detail instead of inventing a row from the probe answer.
    void refreshUsage();
    if (model.usageRequests !== null && !model.usageRequestsUi.loading)
      void loadRequestsPage(model.usageRequestsUi.page);
    if (!outcome.ok) {
      setKeyTest(id, {
        state: "error",
        rowVersion,
        from: testedFrom,
        at: Date.now(),
        reason:
          outcome.kind === "no-answer"
            ? `结果未确认：${outcome.reason}`
            : outcome.reason,
      });
      return;
    }
    const data = outcome.data;
    const connected = data.connected === true;
    const expectedStage = privateTest ? "models" : "inference";
    const contractOk =
      data.key_id === id &&
      data.row_version === rowVersion &&
      typeof data.connected === "boolean" &&
      data.tested_from === testedFrom &&
      (connected
        ? data.stage === expectedStage &&
          data.http_status === 200 &&
          typeof data.model === "string" &&
          data.model.length > 0 &&
          (privateTest ||
            (typeof data.public_ip === "string" && data.public_ip.length > 0))
        : typeof data.reason === "string");
    if (!contractOk) {
      setKeyTest(id, {
        state: "error",
        rowVersion,
        from: testedFrom,
        reason: "探测结果形状不符",
        at: Date.now(),
      });
      return;
    }
    const reason = typeof data.reason === "string" ? data.reason : "";
    setKeyTest(id, {
      state: connected ? "ok" : "error",
      rowVersion,
      reason: connected
        ? ""
        : (TEST_REASON[reason] ?? reason) || "后端未给出原因",
      stage: typeof data.stage === "string" ? data.stage : undefined,
      httpStatus:
        typeof data.http_status === "number" ? data.http_status : undefined,
      model: typeof data.model === "string" ? data.model : undefined,
      elapsedMs:
        typeof data.elapsed_ms === "number" ? data.elapsed_ms : undefined,
      publicIp: typeof data.public_ip === "string" ? data.public_ip : undefined,
      from: testedFrom,
      at:
        typeof data.checked_at === "number"
          ? data.checked_at * 1000
          : Date.now(),
    });
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
    const primary = target.closest<HTMLButtonElement>("[data-cloud-primary]");
    if (primary && !primary.disabled) {
      const action = primaryAction(model).action;
      if (action === PRIMARY_START) void submit(PRIMARY_START, "share");
      else if (action === PRIMARY_STOP) confirmThen(PRIMARY_STOP, "share");
      return;
    }
    if (target.closest("[data-cloud-status-toggle]")) {
      model.statusOpen = !model.statusOpen;
      render();
      return;
    }
    if (target.closest("[data-cloud-login-window-open]")) {
      void openDisplayWindow();
      return;
    }
    if (target.closest("[data-cloud-login-cancel]")) {
      void cancelLogin();
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
      openCreate();
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
      void loadRequestsPage(0, true);
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
      void loadRequestsPage(0, true);
      return;
    }
    if (target.closest("[data-cloud-usage-requests-more]")) {
      void loadRequestsPage(model.usageRequestsUi.page + 1);
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
      void loadRequestsPage(0, true);
      return;
    }
    const requestsWindow = target.closest<HTMLElement>(
      "[data-cloud-requests-window]"
    );
    if (requestsWindow) {
      const value = requestsWindow.dataset.cloudRequestsWindow;
      model.usageRequestsUi.time =
        value === "7" ? "7" : value === "30" ? "30" : "today";
      void loadRequestsPage(0, true);
      return;
    }
    const requestsPageButton = target.closest<HTMLButtonElement>(
      "[data-cloud-requests-page]"
    );
    if (requestsPageButton && !requestsPageButton.disabled) {
      void loadRequestsPage(
        model.usageRequestsUi.page +
          Number(requestsPageButton.dataset.cloudRequestsPage || "0")
      );
      return;
    }
    if (target.closest("[data-cloud-requests-retry]")) {
      void loadRequestsPage(model.usageRequestsUi.page);
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
      requestsSeq += 1;
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
      void keyActionFor(keyAction.dataset.cloudKeyAction ?? "");
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

  async function keyActionFor(raw: string): Promise<void> {
    const [action, ...rest] = raw.split(":");
    const id = rest.join(":");
    if (!id) return;
    if (action === "cap") await saveCap(id);
    else if (action === "reveal") toggleReveal(id);
    else if (action === "show") toggleSecret(id);
    else if (action === "hide") hideSecret(id);
    else if (action === "copy") await copySecret(id);
    else if (action === "test") await testKey(id);
    else if (action === "test-private") await testKey(id, true);
    else if (action === "rotate") await rotateKey(id);
    else if (action === "revoke") await revokeKeyAction(id);
    else if (action === "delete") await deleteKey(id);
  }

  // The create dialog lives outside the repainted content: its fields are not
  // rebuilt by the 30-second refresh, and its idempotency key survives a retry.
  createDialog
    .querySelector("[data-cloud-key-create-close]")!
    .addEventListener("click", () => createDialog.close("cancel"));
  createForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void createKey();
  });
  createDialog.addEventListener("close", () => {
    keyUi.createError = null;
    createErrorBox.textContent = "";
  });
  plainDialog
    .querySelector("[data-cloud-plain-close]")!
    .addEventListener("click", () => plainDialog.close("done"));
  plainDialog.addEventListener("close", () => {
    keyUi.plain = null;
    keyUi.secrets.clear();
    plainValue.textContent = "";
    render();
  });
  plainDialog
    .querySelector("[data-cloud-plain-toggle]")!
    .addEventListener("click", () => {
      if (!keyUi.plain) return;
      keyUi.plain.hidden = !keyUi.plain.hidden;
      renderPlain();
    });
  plainDialog
    .querySelector("[data-cloud-plain-copy]")!
    .addEventListener("click", () => {
      const plain = keyUi.plain;
      if (!plain) return;
      void copyText(plain.value).then((ok) => {
        plainNote.textContent = ok
          ? "已复制明文"
          : "复制失败：浏览器拒绝写入剪贴板";
      });
    });

  // Selects and inline inputs commit on change, not on click: a click on a
  // native select can arrive before its value has moved.
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
    }
    // D18-5: an inline concurrency edit saves on change, so no 保存 button.
    const cap = target.closest<HTMLInputElement>("[data-cloud-key-cap-input]");
    if (cap) {
      void saveCap(cap.dataset.cloudKeyCapInput ?? "");
      return;
    }
  });
  root.addEventListener("input", (event) => {
    const target = event.target as Element;
    const cap = target.closest<HTMLInputElement>("[data-cloud-key-cap-input]");
    if (cap) keyUi.caps.set(cap.dataset.cloudKeyCapInput ?? "", cap.value);
  });

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
    if (model.usageRequests === null) void loadRequestsPage(0, true);
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
