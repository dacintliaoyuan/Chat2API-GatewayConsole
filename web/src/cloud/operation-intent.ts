import type { OperationRow } from "./cloud-reads";
import { when } from "./cloud-format";

/** A2/A1: one maintenance intent's stable, non-secret identity and the
 *  operation's real terminal outcome.
 *
 *  The idempotency key is created BEFORE the submit and kept for every
 *  re-query of the same intent (a lost reply, a timeout, a page refresh). It
 *  carries no password, Key or Token, so it is safe to keep in sessionStorage
 *  and to show in the ledger detail. A new, independent intent always gets a
 *  new key; a terminal result clears the entry.
 *
 *  A1: a login trigger is not a login success. `operationOutcome` maps the
 *  backend's own `state`/`label`/`note` and only reports 认证有效 when the
 *  auth read face said valid. Closing the Chromium window normally is neither
 *  a disconnect nor a success, so the wording never claims either. */

export type Outcome = {
  tone: "ok" | "warn" | "err" | "neutral";
  word: string;
  text: string;
};

let fallback = 0;
/** A random key for one intent. Idempotency only, never a credential. */
export function newIntentKey(): string {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi && typeof cryptoApi.randomUUID === "function")
    return cryptoApi.randomUUID();
  const bytes = new Uint8Array(16);
  if (cryptoApi && typeof cryptoApi.getRandomValues === "function")
    cryptoApi.getRandomValues(bytes);
  else {
    fallback += 1;
    return `idem-${Date.now().toString(36)}-${fallback.toString(36)}`;
  }
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(
    ""
  );
}

export type PendingIntent = {
  key: string;
  action: string;
  target: string;
  at: number;
  /** The operation id the backend answered; null while the reply is unknown. */
  opId: string | null;
};

export const PENDING_STORAGE_KEY = "cloud-console.pending-intents.v1";
export const MAX_PENDING_INTENTS = 8;

/** The submit body for one intent: the backend's own optional
 *  `idempotency_key` (1-128 chars), never a password, Key or Token. */
export function submitBody(intent: PendingIntent): {
  action: string;
  target: string;
  idempotency_key: string;
} {
  return {
    action: intent.action,
    target: intent.target,
    idempotency_key: intent.key,
  };
}

export const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === "object" && !Array.isArray(value);

function safeStorage(): Storage | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    return null; // a blocked storage is not a broken page
  }
}

/** Only bounded, well-formed entries survive. A malformed row is dropped, never
 *  guessed into a submit; the list is capped so one page cannot grow unbounded. */
export function parsePendingIntents(raw: string | null): PendingIntent[] {
  if (typeof raw !== "string" || !raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const out: PendingIntent[] = [];
  const seen = new Set<string>();
  for (const item of parsed) {
    if (!isRecord(item)) continue;
    const key = typeof item.key === "string" ? item.key : "";
    const action = typeof item.action === "string" ? item.action : "";
    const target = typeof item.target === "string" ? item.target : "";
    const at =
      typeof item.at === "number" && Number.isFinite(item.at) ? item.at : 0;
    const opId = typeof item.opId === "string" && item.opId ? item.opId : null;
    if (
      !key ||
      key.length > 128 ||
      !action ||
      action.length > 64 ||
      target.length > 64
    )
      continue;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ key, action, target, at, opId });
    if (out.length >= MAX_PENDING_INTENTS) break;
  }
  return out;
}

export function loadPendingIntents(
  storage: Storage | null = safeStorage()
): PendingIntent[] {
  if (!storage) return [];
  try {
    return parsePendingIntents(storage.getItem(PENDING_STORAGE_KEY));
  } catch {
    return [];
  }
}

/** Persistence is best-effort: an unavailable storage keeps this page's in-memory
 *  intent (the re-query still works here) and never breaks the submit. */
export function savePendingIntents(
  intents: readonly PendingIntent[],
  storage: Storage | null = safeStorage()
): void {
  if (!storage) return;
  try {
    if (!intents.length) {
      storage.removeItem(PENDING_STORAGE_KEY);
      return;
    }
    storage.setItem(
      PENDING_STORAGE_KEY,
      JSON.stringify(intents.slice(-MAX_PENDING_INTENTS))
    );
  } catch {
    /* storage disabled/full: in-memory intent still guards this page */
  }
}

/** The loaded operation that already carries this intent's identity, if any.
 *  Matching needs action and target too: the same key used for another intent is
 *  a conflict, never the same operation. */
export function findOperationByIdentity(
  operations: readonly OperationRow[],
  action: string,
  target: string,
  key: string
): OperationRow | null {
  for (const op of operations) {
    if (op.action !== action || (op.target ?? "") !== target) continue;
    if (op.idempotency_key !== key) continue;
    return op;
  }
  return null;
}

export function findOperationById(
  operations: readonly OperationRow[],
  id: string
): OperationRow | null {
  return operations.find((op) => op.id === id) ?? null;
}

/** A2: the backend's own contention codes, in operator words. The code and
 *  message stay verbatim in the detail; this only explains what they mean. */
export const CONTENTION_HINT: Readonly<Record<string, string>> = {
  resource_busy_timeout:
    "后端资源互斥等待超时：本次未执行；可能是另一页面或另一操作正占用同一资源",
  superseded: "被同一资源上的新操作取代：本次未执行，迟到结果已丢弃",
  idempotency_key_conflict: "同一幂等键已用于不同的动作/目标：本次未执行",
  operation_already_finished: "操作已结束：取消无效",
  invalid_idempotency_key: "幂等键不符合后端范围：本次未执行",
  invalid_action: "后端没有注册该动作",
  action_disabled: "后端已禁用该动作",
};

export function errorText(op: OperationRow): string {
  const code = op.error?.code ?? "";
  const message = op.error?.message ?? "";
  return [code, message].filter(Boolean).join("：");
}

const MAX_TEXT = 400;
const clip = (text: string) =>
  text.length > MAX_TEXT ? `${text.slice(0, MAX_TEXT)}…` : text;

/** The instance's own bounded recovery summary, shown next to the honest state;
 *  it is never a credential and never a fabricated success. */
function recoveryDetail(recovery: unknown): string {
  if (!isRecord(recovery)) return "";
  const detail = typeof recovery.detail === "string" ? recovery.detail : "";
  const action = typeof recovery.action === "string" ? recovery.action : "";
  return detail || (action ? `下一步：${action}` : "");
}

/** A1: one login/re-auth result. `valid` is the only auth success: it is the
 *  auth read face answering, not the trigger request or the window. */
export function loginOutcome(
  result: Record<string, unknown> | null | undefined
): Outcome {
  if (!result)
    return {
      tone: "warn",
      word: "结果未确认",
      text: "后端没有返回登录结果；本页不会重发",
    };
  const state = typeof result.state === "string" ? result.state : null;
  const label =
    typeof result.label === "string" && result.label ? result.label : "";
  const note = typeof result.note === "string" ? result.note : "";
  const triggered = result.triggered === true;
  const accepted = result.accepted === true;
  const error = typeof result.error === "string" ? result.error : "";
  const recovery = recoveryDetail(result.recovery);
  const retryAt =
    isRecord(result.recovery) &&
    typeof result.recovery.next_attempt_epoch === "number" &&
    Number.isFinite(result.recovery.next_attempt_epoch)
      ? result.recovery.next_attempt_epoch
      : null;
  if (error === "reauth_refused:global_attempt_cap")
    return {
      tone: "warn",
      word: "登录被限流",
      text: clip(
        [
          "15 分钟内已达到 3 次重登启动上限",
          retryAt !== null
            ? `本地可重试时间：${when(retryAt * 1000)}`
            : "网关未上报可重试时间",
        ].join("；")
      ),
    };
  const suffix =
    label && note
      ? `（${label}）${note}`
      : note || (label ? `（${label}）` : "");
  switch (state) {
    case "valid":
      return triggered || accepted
        ? {
            tone: "ok",
            word: "认证有效",
            text: clip(`认证读面已确认有效${suffix}`),
          }
        : {
            tone: "ok",
            word: "认证已有效（无需触发）",
            text: clip(`认证读面已确认有效，未开启新登录${suffix}`),
          };
    case "logging_in":
      return {
        tone: "warn",
        word: "已触发 · 等待本人登录",
        text: clip(
          [
            note || "登录窗口已打开，等待本人完成 UM 登录/MFA",
            "正常关闭窗口不等于断线或失败；认证仍以读面为准",
          ].join("；")
        ),
      };
    case "needs_login":
      return {
        tone: "warn",
        word: "已受理 · 未观察到登录会话",
        text: clip(
          [
            note || "尚未观察到登录会话（logging_in）",
            "请稍后按原身份回查，不自动重复触发",
          ].join("；")
        ),
      };
    case "failed": {
      // A1: superseded / BrowserDisabled / manual_takeover never judged the
      // credential, so they are not a red "认证失败" and they never trigger a
      // new login. The instance's own recovery step is shown verbatim.
      const nonCredential =
        error === "superseded" ||
        error === "BrowserDisabled" ||
        error.startsWith("manual_takeover:") ||
        error === "umpass_mfa_required" ||
        error === "umpass_prefill_missing" ||
        error === "timeout" ||
        error === "window_closed" ||
        error === "attempts_exhausted" ||
        [
          "edge_missing",
          "browser_missing",
          "display_missing",
          "browser_config_error",
          "token_save_failed",
        ].includes(error);
      if (nonCredential) {
        const word =
          error === "superseded"
            ? "流程已被取代"
            : error === "BrowserDisabled"
              ? "浏览器不可用"
              : error === "umpass_mfa_required"
                ? "等待本人 MFA"
                : error === "umpass_prefill_missing"
                  ? "需要人工接管"
                  : error === "timeout"
                    ? "登录等待超时"
                    : error === "window_closed"
                      ? "登录窗口已关闭"
                      : error === "attempts_exhausted"
                        ? "自动恢复已用尽"
                        : "人工接管";
        return {
          tone: "warn",
          word,
          text: clip(
            [note || "未判凭据失败", recovery].filter(Boolean).join("；")
          ),
        };
      }
      return {
        tone: "err",
        word: "认证失败",
        text: clip(
          [
            note || "后端报告登录失败",
            recovery,
            "未清除认证，也未自动开启新一轮登录",
          ]
            .filter(Boolean)
            .join("；")
        ),
      };
    }
    case "unconfirmed":
    case "unknown":
    case null:
      return {
        tone: "warn",
        word: "已受理 · 认证状态未确认",
        text: clip(
          [
            note || "后端没有确认认证状态",
            "未知不等于失败或过期，请按原身份回查",
          ].join("；")
        ),
      };
    default:
      return {
        tone: "warn",
        word: "认证状态未确认",
        text: clip(`后端状态 ${state}${suffix}`),
      };
  }
}

export const OP_STATE: Readonly<
  Record<string, { tone: Outcome["tone"]; word: string }>
> = {
  queued: { tone: "neutral", word: "排队中" },
  running: { tone: "warn", word: "执行中" },
  succeeded: { tone: "ok", word: "已完成" },
  failed: { tone: "err", word: "失败" },
  cancelled: { tone: "neutral", word: "已取消" },
  interrupted: { tone: "err", word: "已中断" },
};

export function opStateWord(state: string): {
  tone: Outcome["tone"];
  word: string;
} {
  return OP_STATE[state] ?? { tone: "neutral", word: "结果未确认" };
}

export type OpGroup =
  "running" | "done" | "failed" | "unconfirmed" | "cancelled";
export function opGroup(state: string): OpGroup {
  if (state === "queued" || state === "running") return "running";
  if (state === "succeeded") return "done";
  if (state === "failed" || state === "interrupted") return "failed";
  if (state === "cancelled") return "cancelled";
  return "unconfirmed";
}

/** The operation's real outcome. A login trigger is never "已完成" while the
 *  auth read face has not confirmed `valid`. */
export function operationOutcome(op: OperationRow): Outcome {
  const state = opStateWord(op.state);
  if (op.state === "failed") {
    if (op.action === "account.login" && isRecord(op.result)) {
      return loginOutcome(op.result);
    }
    const text = errorText(op);
    const hint = CONTENTION_HINT[op.error?.code ?? ""];
    return {
      tone: "err",
      word: state.word,
      text: clip([text, hint].filter(Boolean).join("；") || "后端未给出原因"),
    };
  }
  if (op.state === "cancelled" || op.state === "interrupted") {
    const text = errorText(op);
    const hint = CONTENTION_HINT[op.error?.code ?? ""];
    return {
      tone: state.tone,
      word: state.word,
      text: clip(
        [text, hint].filter(Boolean).join("；") || "操作未完成；本页不会重发"
      ),
    };
  }
  if (op.state !== "succeeded")
    return { tone: state.tone, word: state.word, text: "等待后端结果" };
  if (op.action === "account.login") {
    return loginOutcome(op.result);
  }
  if (op.action === "share.status" && isRecord(op.result)) {
    const auth = isRecord(op.result.backend_auth)
      ? op.result.backend_auth
      : null;
    const layers = isRecord(op.result.layers) ? op.result.layers : null;
    const parts = [
      typeof layers?.share_backend === "string"
        ? `UM 后端${LAYER_WORD[layers.share_backend] ?? layers.share_backend}`
        : "",
      typeof auth?.state === "string"
        ? `认证${
            authWord(
              auth.state,
              typeof auth.error === "string" ? auth.error : null
            ).word
          }`
        : "",
      typeof auth?.label === "string" && auth.label ? auth.label : "",
    ].filter(Boolean);
    if (parts.length)
      return { tone: "ok", word: "已完成", text: clip(parts.join(" · ")) };
  }
  const parts = [
    "state",
    "valid",
    "running",
    "ok",
    "status",
    "message",
    "reason",
  ]
    .map((key) => {
      const value = op.result?.[key];
      return typeof value === "string" ||
        typeof value === "boolean" ||
        typeof value === "number"
        ? `${key}=${String(value)}`
        : "";
    })
    .filter(Boolean);
  return {
    tone: "ok",
    word: "已完成",
    text: clip(parts.join(" · ") || "后端未返回简短结果"),
  };
}

export type AuthObservation = {
  at: number | null;
  state: string | null;
  label: string | null;
  source: "status" | "login" | "reauth";
  /** The instance's own terminal error/recovery; a non-credential failure
   *  (MFA, manual takeover, closed window) must not become red "认证失败". */
  error: string | null;
  recovery: Record<string, unknown> | null;
};

const epochMs = (seconds: number | null | undefined): number | null =>
  typeof seconds === "number" && Number.isFinite(seconds)
    ? seconds * 1000
    : null;

/** The newest auth evidence from the loaded operation rows. A `share.status`
 *  carries the backend's layered `backend_auth`; a login/re-auth carries its own
 *  terminal `state`. The newest one is the observation, never an older one. */
export function lastAuthObservation(
  operations: readonly OperationRow[]
): AuthObservation | null {
  let best: { at: number; observation: AuthObservation } | null = null;
  for (const op of operations) {
    if (op.state !== "succeeded") continue;
    let state: string | null = null;
    let label: string | null = null;
    let at: number | null = null;
    let error: string | null = null;
    let recovery: Record<string, unknown> | null = null;
    let source: AuthObservation["source"];
    if (op.action === "share.status") {
      const auth =
        isRecord(op.result) && isRecord(op.result.backend_auth)
          ? op.result.backend_auth
          : null;
      if (!auth) continue;
      state = typeof auth.state === "string" ? auth.state : null;
      label = typeof auth.label === "string" && auth.label ? auth.label : null;
      at =
        typeof auth.observed_at === "number"
          ? epochMs(auth.observed_at)
          : epochMs(op.finished_at ?? op.created_at);
      source = "status";
      error = typeof auth.error === "string" && auth.error ? auth.error : null;
      recovery = isRecord(auth.recovery) ? auth.recovery : null;
    } else if (op.action === "account.login") {
      if (!isRecord(op.result)) continue;
      state = typeof op.result.state === "string" ? op.result.state : null;
      label =
        typeof op.result.label === "string" && op.result.label
          ? op.result.label
          : null;
      at = epochMs(op.finished_at ?? op.created_at);
      source = "login";
    } else {
      continue;
    }
    if (state === null) continue;
    if (isRecord(op.result)) {
      // A share.status result carries the error/recovery inside backend_auth;
      // it has no top-level error. Only override when the top-level field is
      // actually present, so the backend_auth reason is never cleared.
      if (typeof op.result.error === "string" && op.result.error) {
        error = op.result.error;
      }
      if (isRecord(op.result.recovery)) recovery = op.result.recovery;
    }
    if (!best || (at ?? 0) > best.at) {
      best = {
        at: at ?? 0,
        observation: { at, state, label, source, error, recovery },
      };
    }
  }
  return best?.observation ?? null;
}

/** The newest login operation, for the services card's own line. */
export function lastLoginOperation(
  operations: readonly OperationRow[]
): OperationRow | null {
  return (
    operations
      .filter((op) => op.action === "account.login")
      .sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))[0] ?? null
  );
}

/** A real MFA/manual/closed-window terminal state is not a credential
 *  rejection: the instance reports WHY it stopped, so the card says that instead
 *  of a red "认证失败". */
export function nonCredentialAuthWord(
  error: string | null
): { tone: Outcome["tone"]; word: string } | null {
  if (!error) return null;
  if (error === "umpass_mfa_required")
    return { tone: "warn", word: "等待本人 MFA" };
  if (error === "umpass_prefill_missing")
    return { tone: "warn", word: "需要人工接管" };
  if (error.startsWith("manual_takeover:"))
    return { tone: "warn", word: "人工接管" };
  if (error === "timeout") return { tone: "warn", word: "登录等待超时" };
  if (error === "window_closed")
    return { tone: "warn", word: "登录窗口已关闭" };
  if (error === "attempts_exhausted")
    return { tone: "warn", word: "自动恢复已用尽" };
  if (error === "BrowserDisabled")
    return { tone: "warn", word: "浏览器不可用" };
  if (error === "superseded") return { tone: "warn", word: "流程已被取代" };
  if (
    [
      "edge_missing",
      "browser_missing",
      "display_missing",
      "browser_config_error",
      "token_save_failed",
    ].includes(error)
  ) {
    return { tone: "warn", word: "浏览器环境不可用" };
  }
  return null;
}

/** Distinct auth words: needs_login / logging_in / failed / unknown are never
 *  folded into one "无效". */
export function authWord(
  state: string | null,
  error: string | null = null
): { tone: Outcome["tone"]; word: string } {
  switch (state) {
    case "valid":
      return { tone: "ok", word: "有效" };
    case "logging_in":
      return { tone: "warn", word: "登录进行中" };
    case "needs_login":
      return { tone: "warn", word: "需要登录" };
    case "failed":
      return nonCredentialAuthWord(error) ?? { tone: "err", word: "认证失败" };
    case "invalid":
      return { tone: "warn", word: "无效" };
    default:
      return { tone: "neutral", word: "未确认" };
  }
}

export const AUTH_SOURCE_WORD: Readonly<
  Record<AuthObservation["source"], string>
> = {
  status: "状态读数",
  login: "登录动作结果",
  reauth: "重新认证结果",
};

/** The backend's own layer words; an unknown word passes through. */
const LAYER_WORD: Readonly<Record<string, string>> = {
  running: "运行中",
  stopped: "已停止",
  unavailable: "不可用",
  unconfirmed: "未确认",
};

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
export const reasonWord = (reason: string): string => REASON_WORD[reason] ?? reason;

export const TEST_REASON: Readonly<Record<string, string>> = {
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
