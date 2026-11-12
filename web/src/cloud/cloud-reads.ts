import { readShareJson } from "../console/share-json-read";
import {
  shareReadReason,
  type ShareKeyRow,
  type ShareStatusPayload,
} from "../console/share-model";
import { BackendClient } from "../console/backend/client";
import { createFetchTransport } from "../console/backend/transport";
import { normalizeCloudSecurityStatus } from "../console/backend/normalize";
import type {
  CloudSecurityStatus,
  ReadResult,
  ShareUsage,
  ShareUsageRequestsPage,
} from "../console/backend/types";

/** Read layer of the cloud console. Every read keeps its own honest state:
 *  401, 403, "not provided / not assembled", other HTTP errors and a request
 *  that never got an answer are told apart, and a failed refresh keeps the last
 *  good value with its time instead of blanking it (stale is not dead). A
 *  request without an answer says only that — it never claims the cloud host,
 *  the UM backend or the private network is down.
 *
 *  The same module owns the narrow share-key write calls (A36-2 facade). Those
 *  answer a discriminated outcome instead of throwing, so a caller always knows
 *  whether a write was refused, conflicted, unanswered or done. No plaintext is
 *  ever put in a URL, a log, storage or a data attribute here. */

const BASE = "/gw/usagesrv";

export type FailureKind =
  "unauthenticated" | "refused" | "unavailable" | "error" | "no-answer";
export type ReadState<T> =
  | { state: "loading" }
  | { state: "ready"; value: T; at: number }
  | {
      state: "failed";
      kind: FailureKind;
      reason: string;
      status: number | null;
      at: number;
      last: { value: T; at: number } | null;
    };

/** The latest good value of a read, live or stale. */
export function lastValue<T>(
  read: ReadState<T>
): { value: T; at: number; stale: boolean } | null {
  if (read.state === "ready")
    return { value: read.value, at: read.at, stale: false };
  if (read.state === "failed" && read.last)
    return { ...read.last, stale: true };
  return null;
}

export const FAILURE_WORD: Readonly<Record<FailureKind, string>> = {
  unauthenticated: "未认证",
  refused: "无权读取",
  unavailable: "后端未提供",
  error: "读取失败",
  "no-answer": "未得到应答",
};

export type CloudSummary = {
  generatedAt?: string;
  role?: string;
  assembly?: {
    role?: string;
    services?: string[];
    excluded?: string[];
    note?: string;
  } | null;
  sources?: {
    priceMeta?: { checkedAt?: string; checkedStale?: boolean } | null;
  } | null;
  priceVersions?: { versioned?: boolean; current?: PriceVersion | null } | null;
};
export type PriceVersion = {
  versionId?: string;
  effectiveAt?: string;
  exchangeRate?: number;
  checkedAt?: string;
  checkedStale?: boolean;
};
export type HealthRow = {
  key?: string;
  id?: string;
  display_name?: string;
  port?: number;
  state?: string;
  healthy?: boolean;
  detail?: string;
  process_ids?: number[];
  note?: string;
};
export type CloudHealth = {
  checked_at?: number;
  cached?: boolean;
  services?: HealthRow[];
};
export type ActionRow = {
  action: string;
  enabled?: boolean;
  disabled_reason?: string | null;
  cancelable?: boolean;
  description?: string;
};
export type OperationStep = { at?: number; note?: string };
export type OperationRow = {
  id: string;
  action: string;
  target?: string;
  state: string;
  created_at?: number;
  started_at?: number | null;
  finished_at?: number | null;
  error?: { code?: string; message?: string } | null;
  result?: Record<string, unknown> | null;
  /** Backend evidence: the locked resource, generation, cancel flag, steps and
   *  the non-secret idempotency identity (A2). */
  resource?: string;
  generation?: number;
  cancel_requested?: boolean;
  steps?: OperationStep[];
  idempotency_key?: string | null;
  /** 200 on a deduplicated submit: the backend returned the existing
   *  operation instead of executing a second one. */
  deduplicated?: boolean;
};

/** S2 cloud-ops read face (GET /api/console/cloud-ops, D18-11 narrowed).
 *
 *  The cloud console keeps only the security section: the budget guard and the
 *  automatic backup are no longer part of the default assembly, so their sections
 *  are not read or rendered. The security evidence (`generation_floor`,
 *  `records`, `pending_remote`, `exported_at`) sits at the section top level;
 *  `normalizeCloudSecurityStatus` lifts it into the section's flat fields. */
export type SecurityStatus = {
  generation_floor?: number;
  records?: number;
  pending_remote?: number;
  exported_at?: string;
};

/** The private display channel the cloud backend is expected to offer. This is a
 *  narrow contract, not an existing API: the backend may answer 404 until its
 *  noVNC/WebSocket配套 is wired. A URL must stay on this origin and must not
 *  carry a credential in its query string; the page never adds one. */
export type DisplayOperator = {
  owned_by_me?: boolean;
  owner_present?: boolean;
  generation?: number;
  stale?: boolean;
  owner_label?: string;
};
export type DisplaySession = {
  available?: boolean;
  url?: string;
  reason?: string;
  expires_at?: number;
  mode?: string;
  /** D18-9: who currently operates the single display page. */
  operator?: DisplayOperator;
};

/** D18-7/D18-8/D18-9: one read-only display-state snapshot
 *  (GET .../display-state). It never triggers a login and never extends the
 *  login window; `login_window_deadline_epoch` is the instance's OWN deadline,
 *  passed through verbatim. */
export type DisplayStateName =
  | "valid"
  | "cancelled"
  | "timed_out"
  | "failed"
  | "waiting_mfa"
  | "logging_in"
  | "display_disconnected"
  | "not_started"
  | "unknown";
export type DisplayState = {
  available?: boolean;
  state?: DisplayStateName;
  label?: string;
  detail?: string | null;
  observed_at?: number | null;
  login_window_deadline_epoch?: number | null;
  operator?: DisplayOperator;
  display?: { available?: boolean; reason?: string | null };
};

function failureKind(status: number, body: unknown): FailureKind {
  if (status === 401) return "unauthenticated";
  if (status === 403) return "refused";
  const code =
    body && typeof body === "object"
      ? (body as { error?: unknown }).error
      : undefined;
  if (status === 404 || code === "capability_not_assembled")
    return "unavailable";
  return "error";
}

async function readJson<T>(
  path: string,
  accept: (body: unknown) => T | null,
  previous: ReadState<T>,
  timeoutMs = 20_000
): Promise<ReadState<T>> {
  const last = lastValue(previous);
  const keep = last ? { value: last.value, at: last.at } : null;
  const at = Date.now();
  try {
    const { response, data } = await readShareJson(
      `${BASE}${path}`,
      { credentials: "include" },
      timeoutMs
    );
    if (!response.ok) {
      const reason = shareReadReason(
        data as { error?: string; reason?: string } | null,
        response.status
      );
      return {
        state: "failed",
        kind: failureKind(response.status, data),
        reason,
        status: response.status,
        at,
        last: keep,
      };
    }
    const value = accept(data);
    if (value === null)
      return {
        state: "failed",
        kind: "error",
        reason: "响应形状不符",
        status: response.status,
        at,
        last: keep,
      };
    return { state: "ready", value, at };
  } catch (error) {
    return {
      state: "failed",
      kind: "no-answer",
      reason: error instanceof Error ? error.message : String(error),
      status: null,
      at,
      last: keep,
    };
  }
}

/** A same-origin absolute path for the private display window. A protocol
 *  relative URL, another origin, a backslash or a credential-looking query
 *  parameter is refused rather than opened. */
export function safeDisplayUrl(raw: unknown): string | null {
  if (
    typeof raw !== "string" ||
    !raw.startsWith("/") ||
    raw.startsWith("//") ||
    raw.includes("\\") ||
    /[\u0000-\u001f\u007f]/.test(raw)
  )
    return null;
  try {
    const url = new URL(raw, location.origin);
    if (url.origin !== location.origin) return null;
    for (const name of url.searchParams.keys()) {
      if (/token|password|secret|ticket|session|auth/i.test(name)) return null;
    }
    if (url.hash && /token|password|secret|ticket|session|auth/i.test(url.hash))
      return null;
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return null;
  }
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === "object" && !Array.isArray(value);

export const readSummary = (previous: ReadState<CloudSummary>) =>
  readJson<CloudSummary>(
    "/api/summary",
    (body) => (isObject(body) ? (body as CloudSummary) : null),
    previous,
    30_000
  );
export const readHealth = (previous: ReadState<CloudHealth>) =>
  readJson<CloudHealth>(
    "/api/console/health",
    (body) =>
      isObject(body) && Array.isArray(body.services)
        ? (body as CloudHealth)
        : null,
    previous
  );
export const readActions = (previous: ReadState<ActionRow[]>) =>
  readJson<ActionRow[]>(
    "/api/console/actions",
    (body) => {
      const rows = isObject(body) ? body.actions : null;
      return Array.isArray(rows)
        ? rows.filter(
            (row): row is ActionRow =>
              isObject(row) && typeof row.action === "string"
          )
        : null;
    },
    previous
  );
export const readOperations = (previous: ReadState<OperationRow[]>) =>
  readJson<OperationRow[]>(
    "/api/console/operations?limit=8",
    (body) => {
      const rows = isObject(body) ? body.operations : null;
      return Array.isArray(rows)
        ? rows.filter(
            (row): row is OperationRow =>
              isObject(row) &&
              typeof row.id === "string" &&
              typeof row.action === "string"
          )
        : null;
    },
    previous
  );

/** A2 read-back of one operation by its id. This is not limited to the recent
 *  8 rows the ledger shows, and it never writes anything. */
export type OperationRead =
  | { kind: "found"; operation: OperationRow }
  | { kind: "absent" }
  | { kind: "failed"; reason: string; status: number | null };

function operationRow(value: unknown): OperationRow | null {
  return isObject(value) &&
    typeof value.id === "string" &&
    typeof value.action === "string" &&
    typeof value.state === "string"
    ? (value as OperationRow)
    : null;
}

export async function readOperationById(
  id: string,
  timeoutMs = 20_000
): Promise<OperationRead> {
  if (!id || id.length > 128)
    return { kind: "failed", reason: "操作 ID 不符合范围", status: null };
  try {
    const { response, data } = await readShareJson(
      `${BASE}/api/console/operations/${encodeURIComponent(id)}`,
      { credentials: "include" },
      timeoutMs
    );
    if (response.status === 404) return { kind: "absent" };
    if (!response.ok) {
      const record = isObject(data) ? data : null;
      const reason =
        (record &&
          ((typeof record.reason === "string" && record.reason) ||
            (typeof record.detail === "string" && record.detail) ||
            (typeof record.error === "string" && record.error))) ||
        `HTTP ${response.status}`;
      return { kind: "failed", reason, status: response.status };
    }
    const row = isObject(data) ? operationRow(data.operation) : null;
    return row
      ? { kind: "found", operation: row }
      : { kind: "failed", reason: "响应形状不符", status: response.status };
  } catch (error) {
    return {
      kind: "failed",
      status: null,
      reason: /超时|abort|timeout/i.test(
        error instanceof Error ? error.message : String(error)
      )
        ? "回查超时，结果仍未确认"
        : `回查未得到应答（${error instanceof Error ? error.message : String(error)}）`,
    };
  }
}

/** A2 read-back by the non-secret idempotency identity. The list is the
 *  backend's own bounded page (up to 200); a match is the same intent, never a
 *  second execution. `absent` means the loaded page does not contain it, not that
 *  the backend never accepted it. */
export async function readOperationByIdentity(
  action: string,
  target: string,
  key: string,
  timeoutMs = 20_000
): Promise<OperationRead> {
  if (!key || key.length > 128 || !action || action.length > 64) {
    return { kind: "failed", reason: "幂等身份不符合范围", status: null };
  }
  try {
    const { response, data } = await readShareJson(
      `${BASE}/api/console/operations?limit=200`,
      { credentials: "include" },
      timeoutMs
    );
    if (!response.ok) {
      const record = isObject(data) ? data : null;
      const reason =
        (record &&
          ((typeof record.reason === "string" && record.reason) ||
            (typeof record.detail === "string" && record.detail) ||
            (typeof record.error === "string" && record.error))) ||
        `HTTP ${response.status}`;
      return { kind: "failed", reason, status: response.status };
    }
    const rows =
      isObject(data) && Array.isArray(data.operations) ? data.operations : null;
    if (!rows)
      return {
        kind: "failed",
        reason: "响应形状不符",
        status: response.status,
      };
    const match = rows.find(
      (row) =>
        isObject(row) &&
        row.idempotency_key === key &&
        row.action === action &&
        (row.target ?? "") === target
    );
    const operation = match ? operationRow(match) : null;
    return operation ? { kind: "found", operation } : { kind: "absent" };
  } catch (error) {
    return {
      kind: "failed",
      status: null,
      reason: /超时|abort|timeout/i.test(
        error instanceof Error ? error.message : String(error)
      )
        ? "回查超时，结果仍未确认"
        : `回查未得到应答（${error instanceof Error ? error.message : String(error)}）`,
    };
  }
}
export const readShareStatus = (previous: ReadState<ShareStatusPayload>) =>
  readJson<ShareStatusPayload>(
    "/api/console/share-management/status",
    (body) => (isObject(body) ? (body as ShareStatusPayload) : null),
    previous,
    45_000
  );
export const readShareKeys = (previous: ReadState<ShareKeyRow[]>) =>
  readJson<ShareKeyRow[]>(
    "/api/console/share-management/keys",
    (body) => {
      const rows = isObject(body) ? body.keys : null;
      return Array.isArray(rows)
        ? rows.filter(
            (row): row is ShareKeyRow =>
              isObject(row) && typeof row.id === "string"
          )
        : null;
    },
    previous,
    45_000
  );
/** Keep both evidence shapes: the nested `status` object and the security
 *  section's flat fields.  The normalizer already lifted the flat fields out of
 *  the section, so the page no longer depends on a nested security.status. */
export const readSecurityStatus = (previous: ReadState<CloudSecurityStatus>) =>
  readJson<CloudSecurityStatus>(
    "/api/console/cloud-ops",
    (body) => {
      if (!isObject(body)) return null;
      try {
        return normalizeCloudSecurityStatus(body);
      } catch {
        return null;
      }
    },
    previous,
    30_000
  );
export const readDisplaySession = (previous: ReadState<DisplaySession>) =>
  readJson<DisplaySession>(
    "/api/console/share-management/display-session",
    (body) => (isObject(body) ? (body as DisplaySession) : null),
    previous,
    20_000
  );
/** D18-7/D18-8: the read-only display-state snapshot. A failed read keeps
 *  the last value with its time; it never triggers a login. */
export const readDisplayState = (previous: ReadState<DisplayState>) =>
  readJson<DisplayState>(
    "/api/console/share-management/display-state",
    (body) => (isObject(body) ? (body as DisplayState) : null),
    previous,
    20_000
  );

const usageClient = new BackendClient(
  createFetchTransport({
    fetch: (input, init) => fetch(input, init),
    requestTimeoutMs: 45_000,
  })
);
/** Share usage through the shared adapter (normalizeShareUsage: unknown stays null, never 0). */
async function shareUsageRead(
  previous: ReadState<ShareUsage>,
  request: () => Promise<ReadResult<ShareUsage>>
): Promise<ReadState<ShareUsage>> {
  const last = lastValue(previous);
  const keep = last ? { value: last.value, at: last.at } : null;
  const at = Date.now();
  try {
    const result = await request();
    if (result.state === "ready" && result.value)
      return { state: "ready", value: result.value, at };
    // A null status is either a request without an answer or a body the shared
    // normalizer rejected (a contract error); only the former is "no answer".
    const kind: FailureKind =
      result.httpStatus === null
        ? /fetch|network|timeout|abort/i.test(result.reason ?? "")
          ? "no-answer"
          : "error"
        : result.httpStatus === 401
          ? "unauthenticated"
          : result.state === "refused"
            ? "refused"
            : result.state === "unavailable"
              ? "unavailable"
              : "error";
    return {
      state: "failed",
      kind,
      reason: result.reason ?? "读取失败",
      status: result.httpStatus,
      at,
      last: keep,
    };
  } catch (error) {
    return {
      state: "failed",
      kind: "error",
      reason: error instanceof Error ? error.message : String(error),
      status: null,
      at,
      last: keep,
    };
  }
}

export const readShareUsage = (previous: ReadState<ShareUsage>) =>
  shareUsageRead(previous, () => usageClient.readShareUsage());

/** Ticket #8: today's failed requests. The same read face with the backend's
 *  own `result=failed` filter over the last 24h; today's Beijing day bucket in
 *  the answer is therefore complete. A failed or stale read stays unknown. */
export const readShareUsageFailed = (previous: ReadState<ShareUsage>) =>
  shareUsageRead(previous, () => usageClient.readShareUsageFailed());

/** Per-request page through the same adapter; unknown stays null. */
export async function readShareUsageRequests(
  previous: ReadState<ShareUsageRequestsPage>,
  params: {
    window?: string;
    key?: string;
    model?: string;
    result?: string;
    cursor?: string;
    limit?: number;
  }
): Promise<ReadState<ShareUsageRequestsPage>> {
  const last = lastValue(previous);
  const keep = last ? { value: last.value, at: last.at } : null;
  const at = Date.now();
  try {
    const result = await usageClient.readShareUsageRequests(params);
    if (result.state === "ready" && result.value)
      return { state: "ready", value: result.value, at };
    const kind: FailureKind =
      result.httpStatus === null
        ? /fetch|network|timeout|abort/i.test(result.reason ?? "")
          ? "no-answer"
          : "error"
        : result.httpStatus === 401
          ? "unauthenticated"
          : result.state === "refused"
            ? "refused"
            : result.state === "unavailable"
              ? "unavailable"
              : "error";
    return {
      state: "failed",
      kind,
      reason: result.reason ?? "读取失败",
      status: result.httpStatus,
      at,
      last: keep,
    };
  } catch (error) {
    return {
      state: "failed",
      kind: "error",
      reason: error instanceof Error ? error.message : String(error),
      status: null,
      at,
      last: keep,
    };
  }
}

/** One narrow plaintext readback for a key the list says is viewable. The
 *  backend answers distinct inline states (legacy / undecryptable / version
 *  conflict) instead of an error; all of them are reported as unavailable
 *  with the backend's own reason, never as an empty secret. */
export type KeySecretOutcome =
  | { ok: true; secret: string }
  | {
      ok: false;
      status: number | null;
      kind: "refused" | "unavailable" | "error" | "no-answer";
      reason: string;
    };

export async function readShareKeySecret(
  keyId: string,
  rowVersion: number,
  timeoutMs = 20_000
): Promise<KeySecretOutcome> {
  if (!keyId || keyId.length > 128)
    return {
      ok: false,
      status: null,
      kind: "error",
      reason: "Key 标识不符合范围",
    };
  if (!Number.isInteger(rowVersion) || rowVersion < 1)
    return {
      ok: false,
      status: null,
      kind: "error",
      reason: "缺少行版本，请刷新列表",
    };
  try {
    const { response, data } = await readShareJson(
      `${BASE}/api/console/share-management/keys/${encodeURIComponent(keyId)}/secret?expected_row_version=${rowVersion}`,
      { credentials: "include" },
      timeoutMs
    );
    const record = isObject(data) ? data : null;
    if (!response.ok) {
      const reason =
        (record &&
          ((typeof record.reason === "string" && record.reason) ||
            (typeof record.detail === "string" && record.detail) ||
            (typeof record.error === "string" && record.error))) ||
        `HTTP ${response.status}`;
      const kind =
        response.status === 401 || response.status === 403
          ? "refused"
          : response.status === 404 || response.status === 410
            ? "unavailable"
            : "error";
      return { ok: false, status: response.status, kind, reason };
    }
    if (record && typeof record.secret === "string" && record.secret)
      return { ok: true, secret: record.secret };
    if (record?.secret_unavailable === true)
      return {
        ok: false,
        status: response.status,
        kind: "unavailable",
        reason: String(record.reason ?? "不可读取"),
      };
    return {
      ok: false,
      status: response.status,
      kind: "error",
      reason: "响应形状不符",
    };
  } catch (error) {
    return {
      ok: false,
      status: null,
      kind: "no-answer",
      reason: controllerReason(error),
    };
  }
}
function controllerReason(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  if (/超时|abort|timeout/i.test(message)) return "请求超时，结果未确认";
  return `未得到应答（${message}）`;
}
