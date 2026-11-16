import type {
  ActionCapability,
  Catalog,
  ContextEvidence,
  ContextView,
  GatewayConnection,
  Intent,
  JsonObject,
  LogPage,
  Operation,
  OperationResult,
  PublicShareEventsPage,
  PublicShareExport,
  PublicShareStatus,
  ReadResult,
  ShareUsage,
  ShareUsageRequestsPage,
  Transport,
  TransportResponse,
  UmCatalogPayload,
  UmLogPagePayload,
  UmMaintenancePagePayload,
  UmOperationsPayload,
  UmRequestLogsPayload,
  UmRequestResult,
  UmRequestsResponse,
  UmServicesPayload,
  UmSummaryPayload,
  UmTodayResponse,
  UmReadWindow,
  UmTrendRange,
  UmTrendResponse,
} from "./types.ts";
import {
  ContractError,
  normalizeCapabilities,
  normalizeCatalog,
  normalizeContextEvidence,
  normalizeContexts,
  normalizeGatewayConnection,
  normalizeLogPage,
  normalizeOperation,
  normalizePublicShareEvents,
  normalizePublicShareExport,
  normalizePublicShareStatus,
  normalizeShareUsage,
  normalizeShareUsageRequests,
  normalizeUmCatalog,
  normalizeUmLogPage,
  normalizeUmMaintenancePage,
  normalizeUmOperations,
  normalizeUmRequestLogs,
  normalizeUmRequestsResponse,
  normalizeUmServices,
  normalizeUmSummary,
  normalizeUmTodayResponse,
  normalizeUmTrendResponse,
  object,
  positiveCount,
  text,
} from "./normalize.ts";
import { LatestRequests, abortableSleep } from "./transport.ts";

const actionIntents: Readonly<Record<string, Intent>> = Object.freeze({
  "model.catalog": "read",
  "model.context_overrides": "read",
  "model.context_probe_preview": "read",
  "model.context_probe_run": "probe",
  // UM maintenance (B6): the status query is a read; verify probes the
  // upstream credential; login and service actions change local state.
  "account.um_status": "read",
  "account.um_verify": "probe",
  "account.login": "client-write",
  "service.start": "client-write",
  "service.stop": "client-write",
  "service.restart": "client-write",
});
const UM_READ_WINDOWS: readonly UmReadWindow[] = ["today", "24h", "7d", "30d"];
const UM_REQUEST_RESULTS: readonly UmRequestResult[] = [
  "success",
  "failed",
  "truncated",
  "client_disconnected",
  "cancelled",
  "unknown",
];
const UM_LOG_LEVELS: readonly string[] = [
  "error",
  "warn",
  "info",
  "debug",
  "unknown",
];
const UM_REQUEST_ID_RE = /^[0-9a-f]{32}$/;

/** The query `readUmRequests` answers; the controller reports it back to the
 *  frozen console so a late answer to an older query never lands. */
export interface UmRequestsReadQuery {
  window: UmReadWindow;
  model: string | null;
  result: UmRequestResult | "all";
  search: string;
}
export interface UmLogsReadQuery {
  window: UmReadWindow;
  levels: string[];
  source: string | null;
}
/** One strict-fresh UM catalog refresh: the backend's own outcome plus the
 *  catalog payload. `fresh` is only true when the gateway answered with its
 *  raw-fresh marker; `status`/`error` carry the explicit failure category. */
export interface UmCatalogRefreshRead {
  catalog: UmCatalogPayload;
  success: boolean;
  fresh: boolean;
  stale: boolean;
  status: string | null;
  error: string | null;
  warnings: string[];
  collectedAt: string | null;
}
const terminal = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
/** One lane: the binding owns a single selected gateway at a time. */
const bindingLane = "gateway-connection";

export class ActionUnavailableError extends Error {
  readonly action: string;
  constructor(action: string, message: string) {
    super(message);
    this.action = action;
    this.name = "ActionUnavailableError";
  }
}
export class ActionBusyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ActionBusyError";
  }
}

/** A direct GET that did not produce a usable JSON answer. `status` is null
 *  when no HTTP response arrived; `kind` keeps network/timeout/contract apart so
 *  the caller can report a clear state instead of inventing data. */
export type BackendReadFailureKind =
  "network" | "timeout" | "http" | "contract";
export class BackendReadError extends Error {
  readonly kind: BackendReadFailureKind;
  readonly status: number | null;
  constructor(
    kind: BackendReadFailureKind,
    message: string,
    status: number | null = null
  ) {
    super(message);
    this.kind = kind;
    this.status = status;
    this.name = "BackendReadError";
  }
}

function clone<T>(value: T): T {
  return structuredClone(value);
}
function responseReason(status: number, body: unknown): string {
  const row = object(body);
  const reason =
    row && (text(row.reason) || text(row.detail) || text(row.error));
  return reason
    ? `${reason}${reason.includes(`HTTP ${status}`) ? "" : ` (HTTP ${status})`}`
    : `HTTP ${status}`;
}
export interface RunActionOptions {
  /** A caller must explicitly distinguish evidence reads, drafts, client writes and billed probes. */
  intent: Intent;
  signal?: AbortSignal;
  timeoutMs?: number;
  pollIntervalMs?: number;
  onProgress?: (operation: Operation) => void;
  resourceKey?: string;
}
/** The operation body's params. `force`/`visible` are the ADR 0005 additive
 *  `account.login` fields; every other action keeps using the same JSON object. */
export type RunActionParams = JsonObject & {
  force?: boolean;
  visible?: boolean;
};

/** HTTP/operation seam only. No UI imports, timers on construction, tokens or automatic probes. */
export class BackendClient {
  private capabilities: Map<string, ActionCapability> | null = null;
  private active = new Set<string>();
  private readonly now: () => number;
  private readonly sleep: (
    milliseconds: number,
    signal?: AbortSignal
  ) => Promise<void>;
  private readonly transport: Transport;

  constructor(
    transport: Transport,
    clock?: {
      now: () => number;
      sleep: (milliseconds: number, signal?: AbortSignal) => Promise<void>;
    }
  ) {
    this.transport = transport;
    this.now = clock?.now ?? Date.now;
    this.sleep = clock?.sleep ?? abortableSleep;
  }

  async refreshCapabilities(signal?: AbortSignal): Promise<ActionCapability[]> {
    const response = await this.transport.request({
      method: "GET",
      path: "/actions",
      access: "session",
      signal,
    });
    if (response.status < 200 || response.status >= 300) {
      this.capabilities = null;
      throw new ContractError(responseReason(response.status, response.body));
    }
    const capabilities = normalizeCapabilities(response.body);
    this.capabilities = new Map(
      capabilities.map((capability) => [capability.action, capability])
    );
    return capabilities;
  }

  capability(action: string): ActionCapability | null {
    return this.capabilities?.get(action) ?? null;
  }

  async runAction(
    action: string,
    target: string,
    params: RunActionParams,
    options: RunActionOptions
  ): Promise<OperationResult> {
    const intent = actionIntents[action];
    if (!intent || intent !== options.intent)
      throw new ContractError(
        "action intent must match the registered bridge intent"
      );
    const timeoutMs = options.timeoutMs ?? 60_000;
    const interval = options.pollIntervalMs ?? 1_200;
    if (
      !Number.isFinite(timeoutMs) ||
      timeoutMs <= 0 ||
      timeoutMs > 1_200_000 ||
      !Number.isFinite(interval) ||
      interval <= 0
    )
      throw new ContractError(
        "operation timeout/poll interval must be bounded positive values"
      );
    const resource = options.resourceKey ?? `${action}|${target}`;
    const exclusive = intent !== "read";
    if (exclusive && this.active.has(resource))
      throw new ActionBusyError(
        "this operation is already in progress; no duplicate POST was sent"
      );
    if (exclusive) this.active.add(resource);
    let operation: Operation | null = null;
    let posted = false;
    try {
      if (!this.capabilities) await this.refreshCapabilities(options.signal);
      const capability = this.capability(action);
      if (!capability)
        throw new ActionUnavailableError(
          action,
          "current backend does not register this action"
        );
      if (!capability.enabled)
        throw new ActionUnavailableError(
          action,
          capability.disabledReason ?? "action disabled"
        );
      if (options.signal?.aborted)
        return {
          state: "refused",
          operation: null,
          operationId: null,
          reason: "cancelled_before_submit",
        };
      const deadline = this.now() + timeoutMs;
      posted = true;
      const accepted = await this.transport.request({
        method: "POST",
        path: "/operations",
        access: "session",
        signal: options.signal,
        body: { action, target, params: clone(params) },
      });
      if (accepted.status < 200 || accepted.status >= 300)
        return {
          state: "refused",
          operation: null,
          operationId: null,
          reason: responseReason(accepted.status, accepted.body),
        };
      operation = normalizeOperation(accepted.body);
      if (operation.action !== action || operation.target !== target)
        throw new ContractError(
          "accepted operation belongs to a different action/target"
        );
      while (true) {
        if (options.signal?.aborted) throw new Error("local_wait_cancelled");
        options.onProgress?.(clone(operation));
        if (terminal.has(operation.state)) {
          return {
            state: operation.state as OperationResult["state"],
            operation,
            operationId: operation.id,
            reason:
              operation.state === "succeeded"
                ? null
                : text(operation.error?.code),
          };
        }
        if (this.now() >= deadline) throw new Error("operation_deadline");
        await this.sleep(
          Math.min(interval, Math.max(1, deadline - this.now())),
          options.signal
        );
        if (options.signal?.aborted || this.now() >= deadline)
          throw new Error("operation_deadline_or_cancelled");
        const response = await this.transport.request({
          method: "GET",
          path: `/operations/${encodeURIComponent(operation.id)}`,
          access: "session",
          signal: options.signal,
        });
        if (response.status < 200 || response.status >= 300)
          throw new ContractError(
            responseReason(response.status, response.body)
          );
        const next = normalizeOperation(response.body);
        if (
          next.id !== operation.id ||
          next.action !== action ||
          next.target !== target
        )
          throw new ContractError("polled operation identity changed");
        operation = next;
      }
    } catch (error) {
      if (
        error instanceof ActionUnavailableError ||
        error instanceof ActionBusyError ||
        !posted
      )
        throw error;
      return {
        state: "unconfirmed",
        operation,
        operationId: operation?.id ?? null,
        reason:
          error instanceof Error
            ? error.message
            : "transport_or_contract_error",
      };
    } finally {
      if (exclusive) this.active.delete(resource);
    }
  }

  /** Explicit cooperative cancel; aborting the local wait never claims the server cancelled. */
  async cancelOperation(
    operation: Operation,
    signal?: AbortSignal
  ): Promise<Operation> {
    if (!this.capability(operation.action)?.cancelable)
      throw new ActionUnavailableError(
        operation.action,
        "operation is not cancelable"
      );
    if (terminal.has(operation.state)) return operation;
    const response = await this.transport.request({
      method: "POST",
      path: `/operations/${encodeURIComponent(operation.id)}/cancel`,
      access: "session",
      signal,
      body: {},
    });
    if (response.status < 200 || response.status >= 300)
      throw new ContractError(responseReason(response.status, response.body));
    const cancelled = normalizeOperation(response.body);
    if (
      cancelled.id !== operation.id ||
      cancelled.action !== operation.action ||
      cancelled.target !== operation.target
    )
      throw new ContractError("cancel response identity changed");
    return cancelled;
  }

  private async readAction<T>(
    action: string,
    normalize: (result: unknown) => T,
    signal?: AbortSignal
  ): Promise<T> {
    const result = await this.runAction(
      action,
      "model",
      {},
      { intent: "read", signal }
    );
    if (result.state !== "succeeded" || !result.operation?.result)
      throw new ContractError(result.reason ?? `action ${result.state}`);
    return normalize(result.operation.result);
  }

  readCatalog(signal?: AbortSignal): Promise<Catalog> {
    return this.readAction("model.catalog", normalizeCatalog, signal);
  }
  readContexts(signal?: AbortSignal): Promise<ContextView[]> {
    return this.readAction(
      "model.context_overrides",
      normalizeContexts,
      signal
    );
  }
  readContextEvidence(signal?: AbortSignal): Promise<ContextEvidence[]> {
    return this.readAction(
      "model.context_probe_preview",
      normalizeContextEvidence,
      signal
    );
  }

  /** One bounded share-usage read. The path carries the backend's own filters
   *  (window/result); this method owns only the read's honest error state. */
  private async shareUsageRead(
    path: string,
    signal?: AbortSignal
  ): Promise<ReadResult<ShareUsage>> {
    try {
      const response = await this.transport.request({
        method: "GET",
        path,
        access: "public",
        signal,
      });
      if (response.status < 200 || response.status >= 300)
        return {
          state:
            response.status === 404
              ? "unavailable"
              : response.status === 401 || response.status === 403
                ? "refused"
                : "error",
          value: null,
          httpStatus: response.status,
          reason: responseReason(response.status, response.body),
        };
      return {
        state: "ready",
        value: normalizeShareUsage(response.body),
        httpStatus: response.status,
        reason: null,
      };
    } catch (error) {
      if (signal?.aborted) throw error;
      return {
        state: "error",
        value: null,
        httpStatus: null,
        reason: error instanceof Error ? error.message : "network_error",
      };
    }
  }

  async readShareUsage(signal?: AbortSignal): Promise<ReadResult<ShareUsage>> {
    return this.shareUsageRead("/share-usage", signal);
  }

  /** Ticket #8: today's failed requests. The same read face with the
   *  backend's own `result=failed` filter over the last 24h, so today's
   *  Beijing day bucket is complete and yesterday's partial bucket is ignored. */
  async readShareUsageFailed(
    signal?: AbortSignal
  ): Promise<ReadResult<ShareUsage>> {
    return this.shareUsageRead("/share-usage?window=24h&result=failed", signal);
  }

  async readShareUsageRequests(
    params: {
      window?: string;
      key?: string;
      model?: string;
      result?: string;
      cursor?: string;
      limit?: number;
    } = {},
    signal?: AbortSignal
  ): Promise<ReadResult<ShareUsageRequestsPage>> {
    const query = new URLSearchParams();
    for (const [name, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") query.set(name, String(value));
    }
    const path = `/share-usage/requests${[...query].length ? `?${query}` : ""}`;
    try {
      const response = await this.transport.request({
        method: "GET",
        path,
        access: "public",
        signal,
      });
      if (response.status < 200 || response.status >= 300)
        return {
          state:
            response.status === 404
              ? "unavailable"
              : response.status === 401 || response.status === 403
                ? "refused"
                : "error",
          value: null,
          httpStatus: response.status,
          reason: responseReason(response.status, response.body),
        };
      return {
        state: "ready",
        value: normalizeShareUsageRequests(response.body),
        httpStatus: response.status,
        reason: null,
      };
    } catch (error) {
      if (signal?.aborted) throw error;
      return {
        state: "error",
        value: null,
        httpStatus: null,
        reason: error instanceof Error ? error.message : "network_error",
      };
    }
  }

  /** S5 public supervision read face. The console's share lane reads this
   *  snapshot/event/budget face directly (one GET, no operation record); a
   *  missing route is `unavailable` and a failed read is `error`, never an
   *  invented healthy snapshot. */
  async readPublicShareStatus(
    signal?: AbortSignal
  ): Promise<ReadResult<PublicShareStatus>> {
    try {
      const response = await this.transport.request({
        method: "GET",
        path: "/share-management/public",
        access: "session",
        signal,
      });
      if (response.status < 200 || response.status >= 300)
        return {
          state:
            response.status === 404
              ? "unavailable"
              : response.status === 401 || response.status === 403
                ? "refused"
                : "error",
          value: null,
          httpStatus: response.status,
          reason: responseReason(response.status, response.body),
        };
      return {
        state: "ready",
        value: normalizePublicShareStatus(response.body),
        httpStatus: response.status,
        reason: null,
      };
    } catch (error) {
      if (signal?.aborted) throw error;
      return {
        state: "error",
        value: null,
        httpStatus: null,
        reason: error instanceof Error ? error.message : "network_error",
      };
    }
  }

  async readPublicShareEvents(
    before: number | null,
    limit: number,
    signal?: AbortSignal
  ): Promise<ReadResult<PublicShareEventsPage>> {
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 200)
      throw new ContractError(
        "public share events limit must be an integer in 1..200"
      );
    if (before !== null && (!Number.isFinite(before) || before <= 0))
      throw new ContractError(
        "public share events cursor must be a positive epoch"
      );
    const params = new URLSearchParams({ limit: String(limit) });
    if (before !== null) params.set("before", String(before));
    try {
      const response = await this.transport.request({
        method: "GET",
        path: `/share-management/public-events?${params}`,
        access: "session",
        signal,
      });
      if (response.status < 200 || response.status >= 300)
        return {
          state:
            response.status === 404
              ? "unavailable"
              : response.status === 401 || response.status === 403
                ? "refused"
                : "error",
          value: null,
          httpStatus: response.status,
          reason: responseReason(response.status, response.body),
        };
      return {
        state: "ready",
        value: normalizePublicShareEvents(response.body),
        httpStatus: response.status,
        reason: null,
      };
    } catch (error) {
      if (signal?.aborted) throw error;
      return {
        state: "error",
        value: null,
        httpStatus: null,
        reason: error instanceof Error ? error.message : "network_error",
      };
    }
  }

  async readPublicShareExport(
    signal?: AbortSignal
  ): Promise<ReadResult<PublicShareExport>> {
    try {
      const response = await this.transport.request({
        method: "GET",
        path: "/share-management/public-export?format=json",
        access: "session",
        signal,
      });
      if (response.status < 200 || response.status >= 300)
        return {
          state:
            response.status === 404
              ? "unavailable"
              : response.status === 401 || response.status === 403
                ? "refused"
                : "error",
          value: null,
          httpStatus: response.status,
          reason: responseReason(response.status, response.body),
        };
      return {
        state: "ready",
        value: normalizePublicShareExport(response.body),
        httpStatus: response.status,
        reason: null,
      };
    } catch (error) {
      if (signal?.aborted) throw error;
      return {
        state: "error",
        value: null,
        httpStatus: null,
        reason: error instanceof Error ? error.message : "network_error",
      };
    }
  }

  /** Narrow direct read of one selected gateway's connection info. No operation
   *  record, no polling, no automatic probe; the transport owns the timeout.
   *  The response identity is bound to the requested service, so a response for
   *  another gateway is refused instead of being displayed under this one. */
  async readGatewayConnection(
    service: string,
    signal?: AbortSignal
  ): Promise<ReadResult<GatewayConnection>> {
    if (typeof service !== "string" || !service.trim())
      throw new ContractError("gateway service identity is required");
    const params = new URLSearchParams({ service });
    try {
      const response = await this.transport.request({
        method: "GET",
        path: `/gateway-connection?${params}`,
        access: "session",
        signal,
      });
      if (response.status < 200 || response.status >= 300)
        return {
          state:
            response.status === 404
              ? "unavailable"
              : response.status === 401 || response.status === 403
                ? "refused"
                : "error",
          value: null,
          httpStatus: response.status,
          reason: responseReason(response.status, response.body),
        };
      return {
        state: "ready",
        value: normalizeGatewayConnection(response.body, service),
        httpStatus: response.status,
        reason: null,
      };
    } catch (error) {
      if (signal?.aborted) throw error;
      return {
        state: "error",
        value: null,
        httpStatus: null,
        reason: error instanceof Error ? error.message : "network_error",
      };
    }
  }

  /** One GET whose body must be usable JSON. The transport's own timeout is
   *  kept apart from a caller abort, and a missing/failed answer keeps its
   *  status so the caller can report unavailable vs refused vs error. */
  private async readJson(path: string, signal?: AbortSignal): Promise<unknown> {
    let response: TransportResponse;
    try {
      response = await this.transport.request({
        method: "GET",
        path,
        access: "session",
        signal,
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      const message = error instanceof Error ? error.message : "network_error";
      // The transport aborts its own controller with the reason
      // "request_timeout"; browsers surface that reason as a string, not an
      // Error, so the reason is checked as well. A caller abort rethrew above.
      const reason = (error as { reason?: unknown } | null)?.reason;
      const timeout =
        reason === "request_timeout" ||
        (error instanceof Error &&
          error.name === "AbortError" &&
          !/cancel/i.test(message));
      throw new BackendReadError(
        timeout ? "timeout" : "network",
        timeout ? "请求超时，没有得到应答" : `网络中断：${message}`
      );
    }
    if (response.parseError === "invalid_json")
      throw new BackendReadError(
        "contract",
        "响应不是合法 JSON",
        response.status
      );
    if (response.status < 200 || response.status >= 300)
      throw new BackendReadError(
        "http",
        responseReason(response.status, response.body),
        response.status
      );
    return response.body;
  }

  // ------------------------------------------------------------ UM read face (B6)
  //
  // Direct bounded GETs of the local UM read surface. They never write, never
  // retry and never turn a failed read into an empty list; `readJson` keeps the
  // failure kind (network / timeout / http / contract) for the controller.

  async readUmToday(signal?: AbortSignal): Promise<UmTodayResponse> {
    return normalizeUmTodayResponse(await this.readJson("/um/today", signal));
  }

  async readUmTrend(
    range: UmTrendRange,
    signal?: AbortSignal
  ): Promise<UmTrendResponse> {
    if (range !== "7d" && range !== "14d" && range !== "30d")
      throw new ContractError("UM trend range must be 7d/14d/30d");
    return normalizeUmTrendResponse(
      await this.readJson(`/um/trend?${new URLSearchParams({ range })}`, signal)
    );
  }

  async readUmRequests(
    query: UmRequestsReadQuery,
    cursor: string | null,
    limit: number,
    signal?: AbortSignal
  ): Promise<UmRequestsResponse> {
    if (!UM_READ_WINDOWS.includes(query.window as UmReadWindow))
      throw new ContractError("invalid UM request window");
    if (
      query.result !== "all" &&
      !UM_REQUEST_RESULTS.includes(query.result as UmRequestResult)
    )
      throw new ContractError("invalid UM request result");
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100)
      throw new ContractError("UM request limit must be 1..100");
    if (cursor !== null && (!cursor || cursor.length > 512))
      throw new ContractError("invalid UM request cursor");
    const params = new URLSearchParams({
      window: query.window,
      result: query.result,
      limit: String(limit),
    });
    if (query.model !== null) params.set("model", query.model);
    if (query.search) params.set("search", query.search.slice(0, 120));
    if (cursor !== null) params.set("cursor", cursor);
    return normalizeUmRequestsResponse(
      await this.readJson(`/um/requests?${params}`, signal)
    );
  }

  async readUmLogs(
    query: UmLogsReadQuery,
    cursor: string | null,
    limit: number,
    signal?: AbortSignal
  ): Promise<UmLogPagePayload> {
    if (!UM_READ_WINDOWS.includes(query.window as UmReadWindow))
      throw new ContractError("invalid UM log window");
    if (query.levels.some((level) => !UM_LOG_LEVELS.includes(level)))
      throw new ContractError("invalid UM log level");
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 200)
      throw new ContractError("UM log limit must be 1..200");
    if (cursor !== null && (!cursor || cursor.length > 512))
      throw new ContractError("invalid UM log cursor");
    const params = new URLSearchParams({
      window: query.window,
      limit: String(limit),
    });
    if (query.source !== null) params.set("source", query.source);
    if (query.levels.length) params.set("levels", query.levels.join(","));
    if (cursor !== null) params.set("cursor", cursor);
    return normalizeUmLogPage(
      await this.readJson(`/um/logs?${params}`, signal)
    );
  }

  async readUmRequestLogs(
    requestId: string,
    limit: number,
    signal?: AbortSignal
  ): Promise<UmRequestLogsPayload> {
    if (!UM_REQUEST_ID_RE.test(requestId))
      throw new ContractError("invalid UM request id");
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 200)
      throw new ContractError("UM request log limit must be 1..200");
    const params = new URLSearchParams({
      request_id: requestId,
      limit: String(limit),
    });
    return normalizeUmRequestLogs(
      await this.readJson(`/um/request-logs?${params}`, signal)
    );
  }

  async readUmMaintenance(
    limit: number,
    signal?: AbortSignal
  ): Promise<UmMaintenancePagePayload> {
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 200)
      throw new ContractError("UM maintenance limit must be 1..200");
    return normalizeUmMaintenancePage(
      await this.readJson(
        `/um/maintenance?${new URLSearchParams({ limit: String(limit) })}`,
        signal
      )
    );
  }

  /** B2 strict-fresh UM catalog refresh (`model.catalog` with
   *  `sources=["um"], fresh=true`). The response's own `success`/`fresh`
   *  fields decide the outcome: a plain 200 without the raw-fresh marker is
   *  reported as a failed refresh by the caller, never as a fresh success.
   *  `refresh` carries the backend's explicit status/error text. */
  async readUmCatalogRefresh(
    source = "UM /api/models（raw-fresh 严格取新）",
    signal?: AbortSignal
  ): Promise<UmCatalogRefreshRead> {
    const result = await this.runAction(
      "model.catalog",
      "model",
      {
        sources: JSON.stringify(["um"]),
        fresh: true,
      },
      {
        intent: "read",
        signal,
        timeoutMs: 90_000,
        resourceKey: "um-catalog-refresh",
      }
    );
    if (result.state !== "succeeded" || !result.operation?.result)
      throw new ContractError(
        result.reason ?? `catalog refresh ${result.state}`
      );
    const raw = result.operation.result;
    const refresh = object(raw.refresh) ?? {};
    return {
      catalog: normalizeUmCatalog(raw, source),
      success: raw.success === true,
      fresh: raw.fresh === true,
      stale: raw.stale === true,
      status: text(refresh.status),
      error: text(refresh.error) ?? text(refresh.detail),
      warnings: Array.isArray(raw.warnings)
        ? raw.warnings.filter(
            (item): item is string => typeof item === "string"
          )
        : [],
      collectedAt: text(raw.collected_at) ?? text(refresh.collected_at),
    };
  }

  /** The ordinary (non-strict) UM catalog read. The response's
   *  `checked_epoch` is reported in seconds; `normalizeUmCatalog` converts it
   *  to the console's epoch milliseconds. */
  async readUmCatalog(
    source = "UM /api/models（只读目录）",
    signal?: AbortSignal
  ): Promise<UmCatalogPayload> {
    const result = await this.runAction(
      "model.catalog",
      "model",
      {
        sources: JSON.stringify(["um"]),
      },
      { intent: "read", signal, timeoutMs: 60_000, resourceKey: "um-catalog" }
    );
    if (result.state !== "succeeded" || !result.operation?.result)
      throw new ContractError(result.reason ?? `catalog ${result.state}`);
    return normalizeUmCatalog(result.operation.result, source);
  }

  /** The runtime snapshot is the authority for the chat2api service state. */
  async readServices(signal?: AbortSignal): Promise<UmServicesPayload> {
    return normalizeUmServices(await this.readJson("/services", signal));
  }

  /** Original console operation records; the UM maintenance view and the
   *  model test history read the same bounded table. */
  async readOperations(
    limit: number,
    signal?: AbortSignal
  ): Promise<UmOperationsPayload> {
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 200)
      throw new ContractError("operation limit must be 1..200");
    return normalizeUmOperations(
      await this.readJson(
        `/operations?${new URLSearchParams({ limit: String(limit) })}`,
        signal
      )
    );
  }

  /** `/api/summary` rides the console host but not the console prefix; the
   *  caller passes a transport whose basePath is `/gw/usagesrv/api`. */
  async readSummary(signal?: AbortSignal): Promise<UmSummaryPayload> {
    return normalizeUmSummary(await this.readJson("/summary", signal));
  }

  async readLogs(
    query: {
      source: string;
      category: string;
      q?: string;
      level?: string;
      since?: string;
      until?: string;
      cursor?: string;
      rotation?: number;
      request_id?: string;
    },
    signal?: AbortSignal
  ): Promise<LogPage> {
    if (!query.source || !query.category)
      throw new ContractError("log source/category is required");
    const params = new URLSearchParams({
      source: query.source,
      category: query.category,
    });
    for (const key of ["q", "level", "since", "until", "cursor"] as const)
      if (query[key]) params.set(key, query[key]);
    if (query.request_id !== undefined) {
      if (!/^[A-Za-z0-9_.:-]{1,64}$/.test(query.request_id))
        throw new ContractError("invalid request identity");
      params.set("request_id", query.request_id);
    }
    if (query.rotation !== undefined) {
      if (!Number.isSafeInteger(query.rotation) || query.rotation < 0)
        throw new ContractError("invalid log rotation");
      if (query.rotation > 0) params.set("rotation", String(query.rotation));
    }
    const response = await this.transport.request({
      method: "GET",
      path: `/log-entries?${params}`,
      access: "session",
      signal,
    });
    if (response.status < 200 || response.status >= 300)
      throw new ContractError(responseReason(response.status, response.body));
    return normalizeLogPage(response.body);
  }
}

export interface GatewayConnectionView {
  service: string;
  result: ReadResult<GatewayConnection>;
  readAt: number | null;
}

/** Single-slot owner of the currently selected gateway's connection read.
 *
 *  One gateway is selected at a time: starting a read for another service, a
 *  cancel, or leaving the page drops the previous plaintext key synchronously —
 *  before any new request is issued — and a late response from an older selection
 *  can never install itself into the new slot (same generation guard as
 *  LatestRequests). The plaintext key is reachable only through `current`; a read
 *  failure for the new gateway leaves no previous key behind. */
export class GatewayConnectionBinding {
  private readonly client: BackendClient;
  private readonly requests = new LatestRequests();
  private readonly now: () => number;
  private generation = 0;
  private slot: GatewayConnectionView | null = null;

  constructor(client: BackendClient, now: () => number = Date.now) {
    this.client = client;
    this.now = now;
  }

  get current(): GatewayConnectionView | null {
    return this.slot;
  }

  async select(
    service: string,
    signal?: AbortSignal
  ): Promise<GatewayConnectionView | null> {
    if (typeof service !== "string" || !service.trim())
      throw new ContractError("gateway service identity is required");
    this.clear();
    if (signal?.aborted) return null;
    const generation = ++this.generation;
    const cancelled = () => this.requests.cancel(bindingLane);
    signal?.addEventListener("abort", cancelled, { once: true });
    try {
      const outcome = await this.requests.run(bindingLane, (local) =>
        this.client.readGatewayConnection(service, local)
      );
      if (
        outcome.state !== "current" ||
        generation !== this.generation ||
        signal?.aborted
      )
        return null;
      const view: GatewayConnectionView = {
        service,
        result: outcome.value,
        readAt: this.now(),
      };
      this.slot = view;
      return view;
    } finally {
      signal?.removeEventListener("abort", cancelled);
    }
  }

  /** Drop the previous key immediately; a late response is never installed. */
  clear(): void {
    this.generation += 1;
    this.slot = null;
    this.requests.cancel(bindingLane);
  }
}
