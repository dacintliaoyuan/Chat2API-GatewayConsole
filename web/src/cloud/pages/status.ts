import { escapeHtml } from "../../html";
import type { ShareStatusPayload } from "../../console/share-model";
import {
  FAILURE_WORD,
  lastValue,
  readDisplaySession,
  readDisplayState,
  safeDisplayUrl,
  type CloudHealth,
  type HealthRow,
  type OperationRow,
  type ReadState,
} from "../cloud-reads";
import { cancelBackendLogin } from "../cloud-writes";
import {
  AUTH_SOURCE_WORD,
  authWord,
  lastAuthObservation,
  operationOutcome,
  opStateWord,
  type AuthObservation,
} from "../operation-intent";
import { clock, when } from "../cloud-format";
import type { Tone } from "../model";
import type { CloudModel, Feedback, KeyUi } from "../model";

/** API entry (share-gateway, 8100): the backend's pid / process-identity snapshot. */
export function apiEntry(health: ReadState<CloudHealth>): {
  tone: Tone;
  word: string;
  row: HealthRow | null;
  at: number | null;
  stale: boolean;
} {
  const read = lastValue(health);
  if (!read)
    return {
      tone: "neutral",
      word: health.state === "loading" ? "读取中" : "未知",
      row: null,
      at: null,
      stale: false,
    };
  const row =
    read.value.services?.find((s) => s.key === "share-gateway") ?? null;
  const at =
    typeof read.value.checked_at === "number"
      ? read.value.checked_at * 1000
      : read.at;
  if (!row)
    return {
      tone: "neutral",
      word: "未上报",
      row: null,
      at,
      stale: read.stale,
    };
  const word =
    row.state === "Running"
      ? row.healthy === false
        ? "运行中 · 未通过检查"
        : "运行中"
      : row.state === "Stopped"
        ? "已停止"
        : row.state === "Conflict"
          ? "进程身份冲突"
          : "未知";
  const tone: Tone =
    row.state === "Running"
      ? row.healthy === false
        ? "warn"
        : "ok"
      : row.state === "Stopped"
        ? "err"
        : row.state === "Conflict"
          ? "warn"
          : "neutral";
  return { tone, word, row, at, stale: read.stale };
}

export type StatusCheck = {
  at: number | null;
  backend: string | null;
  authState: string | null;
  authLabel: string | null;
};
const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === "object" && !Array.isArray(value);
/** The newest succeeded share.status result in the operations window - the
 *  backend's own layered check. The status read folds the auth states into
 *  account.valid and leaves `account` out while the UM backend is down, so the
 *  layer word and the auth label exist only here (backend cross-check,
 *  2026-10-31). */
export function lastStatusCheck(
  ops: ReadState<OperationRow[]>
): StatusCheck | null {
  const op = (lastValue(ops)?.value ?? [])
    .filter(
      (row) =>
        row.action === "share.status" &&
        row.state === "succeeded" &&
        isRecord(row.result)
    )
    .sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))[0];
  if (!op) return null;
  const result = op.result as Record<string, unknown>;
  const layers = isRecord(result.layers) ? result.layers : {};
  const auth = isRecord(result.backend_auth) ? result.backend_auth : {};
  const seconds =
    typeof auth.observed_at === "number"
      ? auth.observed_at
      : (op.finished_at ?? op.created_at ?? null);
  return {
    at: typeof seconds === "number" ? seconds * 1000 : null,
    backend:
      typeof layers.share_backend === "string" ? layers.share_backend : null,
    authState: typeof auth.state === "string" ? auth.state : null,
    authLabel: typeof auth.label === "string" && auth.label ? auth.label : null,
  };
}
const LAYER_WORD: Readonly<Record<string, string>> = {
  running: "运行中",
  stopped: "已停止",
};
/** The last layered check as one line: 「最近一次分层检查 21:05：UM 后端已停止」. */
export function checkLine(check: StatusCheck | null): string {
  if (!check) return "";
  const parts = [
    check.backend
      ? `UM 后端${LAYER_WORD[check.backend] ?? ` ${check.backend}`}`
      : "",
    check.authLabel ?? (check.authState ? `认证 ${check.authState}` : ""),
  ].filter(Boolean);
  return parts.length
    ? `最近一次分层检查 ${check.at ? when(check.at) : "时间未知"}：${parts.join(" · ")}`
    : "";
}

type Word = { tone: Tone; word: string };
/** The only current login progress: a login/re-auth operation the backend still
 *  reports as queued or running in a fresh operations read. `at` is its submit
 *  time (created_at, ms), not the time the progress was observed. */
export type CurrentLogin = {
  state: "queued" | "running";
  at: number | null;
  action: string;
};
const LOGIN_ACTIONS: ReadonlySet<string> = new Set(["account.login"]);
const LOGIN_ACTION_WORD: Readonly<Record<string, string>> = {
  "account.login": "登录 UM",
};
/** The newest login/re-auth operation still in flight. A terminal row is history,
 *  and a stale operations read cannot prove the backend is still running it. */
function currentLoginOperation(
  ops: ReadState<OperationRow[]>
): CurrentLogin | null {
  const read = lastValue(ops);
  if (!read || read.stale) return null;
  const op = read.value
    .filter(
      (row) =>
        LOGIN_ACTIONS.has(row.action) &&
        (row.state === "queued" || row.state === "running")
    )
    .sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))[0];
  if (!op) return null;
  return {
    state: op.state === "queued" ? "queued" : "running",
    at:
      typeof op.created_at === "number" && Number.isFinite(op.created_at)
        ? op.created_at * 1000
        : null,
    action: op.action,
  };
}
export type UmState = {
  backend: Word;
  auth: Word;
  at: number | null;
  check: StatusCheck | null;
  observation: AuthObservation | null;
  /** Only the STATUS read is stale: backend/auth/at are then last values, not
   *  current readings. A stale operations read never makes a fresh status
   *  reading stale. */
  stale: boolean;
  /** Only the OPERATIONS read is stale: check/observation/currentLogin are dated
   *  history. It never downgrades a fresh status read's current auth word. */
  opsStale: boolean;
  /** True only when THIS status read carries the account layer, so only then is
   *  `auth` a current reading. A historical observation never sets it. */
  authReported: boolean;
  /** The newest historical observation's own time. It is never `last_check_at`,
   *  which stamps the status read, not the auth observation. */
  authAt: number | null;
  /** The only current login progress: a fresh operations read's queued/running
   *  login/re-auth operation. A terminal observation never fills it. */
  currentLogin: CurrentLogin | null;
};
/** UM backend and its authentication. The status read carries
 *  account {running, valid}; while the backend is down it leaves `account`
 *  out rather than sending running:false, so an absent account is 未上报 and
 *  only the last layered check can say 已停止. `last_check_at` stamps THIS
 *  status read; it is not the time the auth was observed.
 *
 *  A1/R2: `valid` from THIS status read is the only current auth success.
 *  The newest auth observation (an older share.status, login or re-auth result)
 *  is dated historical evidence: it never becomes the current word. A current
 *  `valid=false` is always 无效; a terminal `logging_in`/`needs_login`/`failed`
 *  observation is history and never refines it. R9: current login progress is
 *  only a queued/running login/re-auth operation in a fresh operations read. */
export function umState(
  status: ReadState<ShareStatusPayload>,
  ops: ReadState<OperationRow[]>
): UmState {
  const read = lastValue(status);
  const opsRead = lastValue(ops);
  const check = lastStatusCheck(ops);
  const observation = lastAuthObservation(opsRead?.value ?? []);
  const currentLogin = currentLoginOperation(ops);
  const unknown: Word = {
    tone: "neutral",
    word: status.state === "loading" ? "读取中" : "未知",
  };
  const evidence = {
    check,
    observation,
    authAt: observation?.at ?? null,
    currentLogin,
  };
  if (!read)
    return {
      backend: unknown,
      auth: unknown,
      at: null,
      ...evidence,
      stale: false,
      opsStale: opsRead?.stale === true,
      authReported: false,
    };
  const account = read.value.account;
  const at =
    typeof read.value.last_check_at === "number"
      ? read.value.last_check_at * 1000
      : null;
  const backend: Word =
    account?.running === true
      ? { tone: "ok", word: "运行中" }
      : account?.running === false
        ? { tone: "err", word: "未运行" }
        : { tone: "neutral", word: "未上报" };
  let auth: Word;
  let authReported = false;
  if (account?.running === false) {
    auth = { tone: "neutral", word: "无法检查" };
    authReported = true;
  } else if (account?.valid === true) {
    auth = { tone: "ok", word: "有效" };
    authReported = true;
  } else if (account?.valid === false) {
    authReported = true;
    // R9: the current account layer says invalid. No terminal observation may
    // refine it into logging_in/needs_login/failed; those stay in the history
    // line. A queued/running login op is progress, never the auth word.
    auth = { tone: "warn", word: "无效" };
  } else {
    // No account layer in this read: the current auth is unreported even when a
    // historical observation once said valid.
    auth = { tone: "neutral", word: "未上报" };
  }
  // The status read's staleness and the operations read's staleness are
  // separate: a fresh account.valid=false stays the current 无效 even when the
  // operations read failed and kept an older observation.
  return {
    backend,
    auth,
    at,
    ...evidence,
    stale: read.stale,
    opsStale: opsRead?.stale === true,
    authReported,
  };
}
/** The UM backend is down as last seen: the status says so, or it is silent and
 *  the last layered check found the backend stopped. */
export const umDown = (um: UmState) =>
  um.backend.word === "未运行" ||
  (um.backend.word === "未上报" && um.check?.backend === "stopped");

/** The newest historical auth observation as one dated line. It is history, not
 *  the current word: the status read's account layer is the only current
 *  reading, and the observation carries its own time rather than the status
 *  read's `last_check_at`. */
export function authHistoryLine(um: UmState): string {
  const observation = um.observation;
  if (!observation) return "";
  const word = authWord(observation.state, observation.error).word;
  return `历史认证证据：${AUTH_SOURCE_WORD[observation.source]}${observation.at ? ` ${when(observation.at)}` : " 时间未上报"}报告${word}${um.opsStale ? "（读数已过时）" : ""}`;
}

/** Ticket #5: the seven display/auth states the status row renders. A state
 *  word comes only from a fresh display-state read; a missing or stale read is
 *  状态未知, never the last 已登录. */
const DISPLAY_STATE_WORD: Readonly<Record<string, string>> = {
  not_started: "未开始",
  logging_in: "登录中",
  waiting_mfa: "等待 MFA",
  waiting_duo: "等待 Duo 推送",
  valid: "已登录",
  cancelled: "认证已失效",
  failed: "认证已失效",
  timed_out: "超时",
  display_disconnected: "显示断线",
};

export type ServiceState = {
  tone: Tone;
  word: string;
  /** true = gateway_running and in_sync are both true; false = the read says
   *  the service is not usable; null = one of the two fields is unreadable. */
  available: boolean | null;
};

/** Ticket #5: the service read face is `gateway_running` AND `in_sync`. A field
 *  the read does not carry is unknown, never a reassuring "服务可用". */
export function serviceState(
  status: ReadState<ShareStatusPayload>
): ServiceState {
  const read = lastValue(status);
  if (!read || read.stale)
    return { tone: "neutral", word: "服务状态未知", available: null };
  const { gateway_running, in_sync } = read.value;
  if (gateway_running === true && in_sync === true)
    return { tone: "ok", word: "服务可用", available: true };
  if (gateway_running === false)
    return {
      tone: "err",
      word: "服务异常（API 入口未运行）",
      available: false,
    };
  if (in_sync === false)
    return { tone: "err", word: "服务异常（配置未同步）", available: false };
  return { tone: "neutral", word: "服务状态未知", available: null };
}

export type StatusRow = {
  tone: Tone;
  word: string;
  /** The status read's own time, or the display-state read's when the service
   *  read has no value at all. */
  at: number | null;
  open: boolean;
  actions: "open" | "open-cancel" | "relogin-cancel";
};

/** Ticket #5: the one status line. The word is the display/auth state; the
 *  service read must be current AND both `gateway_running`/`in_sync` must be
 *  known before the word may say anything but 状态未知. The right-side login
 *  actions follow the word. */
export function statusRow(model: CloudModel): StatusRow {
  const displayRead = lastValue(model.displayState);
  const statusRead = lastValue(model.status);
  const service = serviceState(model.status);
  const current =
    displayRead && !displayRead.stale && service.available !== null;
  const word = current
    ? (DISPLAY_STATE_WORD[displayRead.value.state ?? "unknown"] ?? "状态未知")
    : "状态未知";
  // Ticket #5 只给三组状态定了动作。剩下的两种——已登录/未开始与
  // 状态未知——都给「打开登录窗口」：打开窗口是只读动作，不改变认证，
  // 所以未知状态下给它是安全的，也不会把未知说成已登录。
  const actions =
    word === "登录中" || word === "等待 MFA"
      ? "open-cancel"
      : word === "认证已失效" || word === "超时" || word === "显示断线"
        ? "relogin-cancel"
        : "open";
  const tone: Tone =
    word === "已登录" && service.available === true
      ? "ok"
      : service.available === false
        ? "err"
        : word === "认证已失效" || word === "超时" || word === "显示断线"
          ? "err"
          : word === "登录中" || word === "等待 MFA"
            ? "warn"
            : "neutral";
  return {
    tone,
    word,
    at: statusRead?.at ?? displayRead?.at ?? null,
    open: model.statusOpen === true,
    actions,
  };
}

function latestLoginOperation(
  ops: ReadState<OperationRow[]>
): OperationRow | null {
  const read = lastValue(ops);
  if (!read) return null;
  return (
    read.value
      .filter((row) => LOGIN_ACTIONS.has(row.action))
      .sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))[0] ?? null
  );
}

/** Ticket #5: the expanded status row. Four dated evidence lines and the two
 *  read-only actions; the page never promotes history to the current word. */
export function statusDetailHtml(model: CloudModel): string {
  const statusRead = lastValue(model.status);
  const displayRead = lastValue(model.displayState);
  const opsRead = lastValue(model.operations);
  const status = statusRead?.value ?? null;
  const display = displayRead?.value ?? null;
  const account = status?.account ?? null;
  const authWord = !status
    ? "状态读数未返回"
    : account?.running === false
      ? "UM 后端未运行，无法检查认证"
      : account?.valid === true
        ? "认证有效"
        : account?.valid === false
          ? "认证无效"
          : "认证未上报";
  const authStale = statusRead?.stale ? " · 读数已过时，不当作当前状态" : "";
  const displayWord = !display
    ? "认证读数未返回"
    : `${display.label || (DISPLAY_STATE_WORD[display.state ?? "unknown"] ?? "状态未知")}${display.detail ? ` · ${display.detail}` : ""}${displayRead?.stale ? " · 读数已过时" : ""}`;
  const deadline =
    typeof display?.login_window_deadline_epoch === "number"
      ? when(display.login_window_deadline_epoch * 1000)
      : null;
  const displayAt = displayRead?.at ?? null;
  const upstream =
    status?.upstream?.reachable === true
      ? "可达"
      : status?.upstream?.reachable === false
        ? "不可达"
        : "未上报";
  const check = lastStatusCheck(model.operations);
  const checkText = check
    ? `${checkLine(check)}${opsRead?.stale ? "（读数已过时）" : ""}`
    : opsRead
      ? opsRead.stale
        ? "操作读数已过时，无法确认最近一次分层检查"
        : "未读取到最近一次分层检查"
      : "操作读数未返回";
  const login = latestLoginOperation(model.operations);
  const loginText = login
    ? `${LOGIN_ACTION_WORD[login.action] ?? login.action} · ${operationOutcome(login).word}${login.created_at ? ` · 提交于 ${when(login.created_at * 1000)}` : " · 提交时间未上报"}${opsRead?.stale ? " · 读数已过时" : ""}`
    : opsRead
      ? opsRead.stale
        ? "操作读数已过时，无法确认登录动作"
        : "未读取到登录动作"
      : "操作读数未返回";
  const upstreamStale = statusRead?.stale
    ? " · 读数已过时，不当作当前状态"
    : "";
  return `<div class="cloud-status-detail" data-cloud-status-detail>
    <dl class="cloud-status-evidence">
      <div><dt>认证读数</dt><dd>${escapeHtml(displayWord)}${displayAt ? ` · 读数于 ${escapeHtml(when(displayAt))}` : " · 读数时间未上报"}${deadline ? ` · 登录窗口截止 ${escapeHtml(deadline)}（实例自己的窗口，本页不延长）` : ""} · ${escapeHtml(authWord)}${escapeHtml(authStale)}</dd></div>
      <div><dt>上游认证</dt><dd>${escapeHtml(upstream)}${escapeHtml(upstreamStale)} · 可达不等于每次请求都成功${statusRead ? ` · 状态读取于 ${escapeHtml(clock(statusRead.at))}` : ""}</dd></div>
      <div><dt>最近核验</dt><dd>${escapeHtml(checkText)}</dd></div>
      <div><dt>登录动作</dt><dd>${escapeHtml(loginText)}</dd></div>
    </dl>
    <div class="cloud-status-detail-actions">
      ${model.actionButton("share.status", "share", "分层检查（只读）", "btn btn-outline btn-sm")}
      ${model.actionButton("share.gateway_restart", "share", "重启分享入口 8100", "btn btn-tonal btn-sm")}
    </div>
    <p class="card-foot">打开登录窗口不等于 UM 认证成功，登录/MFA 由本人完成；上游可达不等于每次请求都成功。读数失败后保留的上次值会标为过时，不当作当前状态。</p>
  </div>`;
}

export type PublicApiUrl = { url: string | null; reason: string };

const API_URL_REASON_WORD: Readonly<Record<string, string>> = {
  public_origin_unconfigured: "未配置公网地址",
  public_origin_invalid: "公网地址配置无效",
};

/** The public API base URL to give another client. The backend's `share_url`
 *  is an origin (no /v1); the OpenAI-compatible base URL appends `/v1`.
 *  An unconfigured, stale or malformed origin is reported honestly, never
 *  guessed from history. */
export function publicApiUrl(
  status: ReadState<ShareStatusPayload>
): PublicApiUrl {
  const read = lastValue(status);
  if (!read) return { url: null, reason: "服务状态未读取" };
  if (read.stale) return { url: null, reason: "状态读数已过时" };
  const value = read.value;
  const origin =
    typeof value.share_url === "string" ? value.share_url.trim() : "";
  if (!origin) {
    const raw =
      typeof value.share_url_reason === "string" && value.share_url_reason
        ? value.share_url_reason
        : value.share_url_source === "configured"
          ? "backend_missing_share_url"
          : "public_origin_unconfigured";
    return { url: null, reason: API_URL_REASON_WORD[raw] ?? raw };
  }
  let parsed: URL;
  try {
    parsed = new URL(origin);
  } catch {
    return { url: null, reason: "公网地址格式无法确认" };
  }
  if (parsed.protocol !== "https:") {
    return { url: null, reason: "公网地址不是 https" };
  }
  return { url: `${origin.replace(/\/+$/, "")}/v1`, reason: "" };
}

/** Read-only API address line: the URL to hand to another client, or the
 *  honest reason it is unavailable. It never exposes a Key. */
export function apiUrlHtml(model: CloudModel): string {
  const api = publicApiUrl(model.status);
  if (!api.url) {
    return `<section class="cloud-api-url" data-tone="warn" aria-label="公网 API">
    <span class="cloud-api-url-label">公网 API</span>
    <span class="cloud-api-url-reason">${escapeHtml(api.reason)}</span>
  </section>`;
  }
  return `<section class="cloud-api-url" aria-label="API 地址">
    <span class="cloud-api-url-label">API 地址</span>
    <code>${escapeHtml(api.url)}</code>
    <button type="button" class="btn btn-outline btn-sm" data-cloud-copy-api-url="${escapeHtml(api.url)}">复制</button>
    <span class="cloud-api-url-note">给其他客户端的 OpenAI 兼容 Base URL；Key 在下方 Key 列表</span>
  </section>`;
}

/** Ticket #5: the status line at the top of the single page. */
export function statusRowHtml(model: CloudModel): string {
  const row = statusRow(model);
  const service = serviceState(model.status);
  const at = row.at ? clock(row.at) : "时间未知";
  const open = `<button type="button" class="btn btn-primary btn-sm" data-cloud-login-window-open>打开登录窗口</button>`;
  const cancel = `<button type="button" class="btn btn-outline btn-sm" data-cloud-login-cancel>取消登录</button>`;
  const relogin = model.actionButton(
    "account.login",
    "share",
    "重新登录",
    "btn btn-primary btn-sm"
  );
  const actions =
    row.actions === "open"
      ? open
      : row.actions === "open-cancel"
        ? `${open}${cancel}`
        : `${relogin}${cancel}`;
  return `<section class="cloud-status-row" data-tone="${row.tone}" aria-label="服务状态">
    <span class="cloud-status-dot" data-tone="${row.tone}" aria-hidden="true"></span>
    <button type="button" class="cloud-status-toggle cloud-status-word" data-cloud-status-toggle aria-expanded="${row.open}" title="展开 / 收起状态详情">${escapeHtml(row.word)}</button>
    <span class="cloud-status-service" data-tone="${service.available === false ? "err" : "neutral"}">${escapeHtml(service.word)}</span>
    <span class="cloud-status-time">数据更新于 ${escapeHtml(at)}</span>
    <span class="cloud-status-grow"></span>
    <div class="cloud-status-login">${actions}</div>
    <button type="button" class="btn btn-outline btn-sm" data-cloud-refresh>刷新</button>
    ${row.open ? statusDetailHtml(model) : ""}
  </section>`;
}

export type AuthHeadline = { tone: Tone; title: string; detail: string };
/** The current auth headline from the status read, or null when the current
 *  reading needs no issue. A status read without the account layer is 未上报,
 *  never a green 有效 from history; the history stays dated evidence in the
 *  detail. Only a current `valid` is a success. R9: the title says UM 登录进行中
 *  only while a login/re-auth operation is queued/running in a fresh operations
 *  read; a terminal observation is history and never the current progress. */
export function authHeadline(um: UmState): AuthHeadline | null {
  if (um.stale) return null;
  if (
    um.auth.word === "有效" ||
    um.auth.word === "读取中" ||
    um.auth.word === "未知"
  )
    return null;
  if (um.backend.word === "未运行") return null;
  const title = um.currentLogin
    ? "UM 登录进行中"
    : !um.authReported
      ? "UM 认证未上报"
      : `UM 认证${um.auth.word}`;
  const read = um.at ? `状态读取于 ${when(um.at)}` : "状态读取时间未上报";
  const current = um.authReported
    ? `当前认证：${um.auth.word}`
    : "当前认证未上报：这次状态读数没有账号层（后端未上报）";
  const login = um.currentLogin
    ? `当前登录进度：${opStateWord(um.currentLogin.state).word} · ${LOGIN_ACTION_WORD[um.currentLogin.action] ?? um.currentLogin.action}${um.currentLogin.at ? ` · 提交于 ${when(um.currentLogin.at)}` : " · 提交时间未上报"}`
    : "";
  const history = authHistoryLine(um);
  const checked = um.check?.authLabel
    ? `最近分层检查：${um.check.authLabel}`
    : "";
  const notSuccess = um.currentLogin
    ? "当前登录进度不代表认证成功；只有认证读面确认有效才算成功；历史有效不代表当前认证。"
    : "只有认证读面确认有效才算成功；历史有效不代表当前认证。";
  return {
    tone: um.auth.tone === "err" ? "err" : "warn",
    title,
    detail: `${[read, current, login, history, checked].filter(Boolean).join(" · ")}。${notSuccess}`,
  };
}

export type StatusActionsContext = {
  model: CloudModel;
  keyUi: KeyUi;
  render: () => void;
  addFeedback: (entry: Omit<Feedback, "key" | "at" | "done">) => Feedback;
  settle: (row: Feedback, tone: Feedback["tone"], text: string) => void;
};

/** D18-7/D18-8: the display/auth actions. A read never triggers a login and
 *  never extends the window; a cancel only ends THIS login round. */
export function createStatusActions(ctx: StatusActionsContext) {
  const { model, keyUi, render, addFeedback, settle } = ctx;

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
    const opened = window.open(url, "_blank");
    if (!opened) {
      // A blocked popup must not leave the login stuck behind an invisible
      // window: open the same one-time grant in this tab instead.
      displayFeedback(
        "neutral",
        "弹出窗口被拦截，已在当前页打开私有登录窗口；打开窗口不等于 UM 认证成功"
      );
      render();
      location.assign(url);
      return;
    }
    try {
      opened.opener = null;
    } catch {
      // The grant page is same-origin; a cross-origin opener is already severed.
    }
    displayFeedback(
      "neutral",
      "已打开私有登录窗口；打开窗口不等于 UM 认证成功，关闭后请重新检查"
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

  return { openDisplayWindow, cancelLogin };
}
