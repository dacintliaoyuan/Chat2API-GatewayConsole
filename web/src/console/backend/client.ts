import type {
  ActionCapability, Catalog, ContextEvidence, ContextView, GatewayConnection, Intent, JsonObject, LogPage,
  ModelSettings, Operation, OperationResult, ReadResult, SettingsPreview, ShareUsage,
  SyncScope, Transport,
} from "./types.ts";
import {
  ContractError, encodeSyncScope, normalizeCapabilities, normalizeCatalog,
  normalizeContextEvidence, normalizeContexts, normalizeGatewayConnection, normalizeLogPage, normalizeOperation,
  normalizeShareUsage, object, positiveCount, text,
} from "./normalize.ts";
import { LatestRequests, abortableSleep } from "./transport.ts";
import { assertPublicOperation, PUBLIC_ACTIONS, PUBLIC_LOG_SOURCES } from "./public-scope.ts";

const actionIntents: Readonly<Record<string, Intent>> = Object.freeze({
  "model.catalog": "read", "model.context_overrides": "read",
  "model.context_probe_preview": "read", "model.sync_preview": "read",
  "model.sync_pending": "read", "model.sync_history": "read", "model.settings_preview": "read",
  "model.settings_save": "draft", "model.settings_apply": "client-write",
  "model.sync_apply": "client-write", "model.context_probe_run": "probe",
  "model.speedtest": "probe",
});
const terminal = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
/** One lane: the binding owns a single selected gateway at a time. */
const bindingLane = "gateway-connection";

export class ActionUnavailableError extends Error {
  readonly action: string;
  constructor(action: string, message: string) { super(message); this.action = action; this.name = "ActionUnavailableError"; }
}
export class ActionBusyError extends Error {
  constructor(message: string) { super(message); this.name = "ActionBusyError"; }
}

function clone<T>(value: T): T { return structuredClone(value); }
function responseReason(status: number, body: unknown): string {
  const row = object(body);
  const reason = row && (text(row.reason) || text(row.detail) || text(row.error));
  return reason ? `${reason}${reason.includes(`HTTP ${status}`) ? "" : ` (HTTP ${status})`}` : `HTTP ${status}`;
}
function selectionSnapshot(selection: Readonly<Record<string, number>>): Record<string, number> {
  if (!object(selection) || !Object.keys(selection).length) throw new ContractError("settings selection cannot be empty");
  const copied: Record<string, number> = {};
  for (const [key, version] of Object.entries(selection)) {
    if (!/^[^|]+\|.+$/.test(key) || positiveCount(version) === null)
      throw new ContractError("settings selection requires real model identities and positive draft versions");
    copied[key] = version;
  }
  return copied;
}
function selectionsMatch(left: Readonly<Record<string, number>>, right: Readonly<Record<string, number>>): boolean {
  const keys = Object.keys(left).sort();
  const other = Object.keys(right).sort();
  return keys.length === other.length && keys.every((key, index) => key === other[index] && left[key] === right[key]);
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

/** HTTP/operation seam only. No UI imports, timers on construction, tokens or automatic probes. */
export class BackendClient {
  private capabilities: Map<string, ActionCapability> | null = null;
  private active = new Set<string>();
  private readonly now: () => number;
  private readonly sleep: (milliseconds: number, signal?: AbortSignal) => Promise<void>;
  private readonly transport: Transport;

  constructor(transport: Transport, clock?: {
    now: () => number;
    sleep: (milliseconds: number, signal?: AbortSignal) => Promise<void>;
  }) {
    this.transport = transport;
    this.now = clock?.now ?? Date.now;
    this.sleep = clock?.sleep ?? abortableSleep;
  }

  async refreshCapabilities(signal?: AbortSignal): Promise<ActionCapability[]> {
    const response = await this.transport.request({ method: "GET", path: "/actions", access: "session", signal });
    if (response.status < 200 || response.status >= 300) {
      this.capabilities = null;
      throw new ContractError(responseReason(response.status, response.body));
    }
    const capabilities = normalizeCapabilities(response.body).filter(row => PUBLIC_ACTIONS.has(row.action));
    this.capabilities = new Map(capabilities.map(capability => [capability.action, capability]));
    return capabilities;
  }

  capability(action: string): ActionCapability | null { return this.capabilities?.get(action) ?? null; }

  async runAction(action: string, target: string, params: JsonObject, options: RunActionOptions): Promise<OperationResult> {
    assertPublicOperation(action, target, params);
    const intent = actionIntents[action];
    if (!intent || intent !== options.intent) throw new ContractError("action intent must match the registered bridge intent");
    const timeoutMs = options.timeoutMs ?? 60_000;
    const interval = options.pollIntervalMs ?? 1_200;
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 1_200_000 || !Number.isFinite(interval) || interval <= 0)
      throw new ContractError("operation timeout/poll interval must be bounded positive values");
    const resource = options.resourceKey ?? `${action}|${target}`;
    const exclusive = intent !== "read";
    if (exclusive && this.active.has(resource)) throw new ActionBusyError("this operation is already in progress; no duplicate POST was sent");
    if (exclusive) this.active.add(resource);
    let operation: Operation | null = null;
    let posted = false;
    try {
      if (!this.capabilities) await this.refreshCapabilities(options.signal);
      const capability = this.capability(action);
      if (!capability) throw new ActionUnavailableError(action, "current backend does not register this action");
      if (!capability.enabled) throw new ActionUnavailableError(action, capability.disabledReason ?? "action disabled");
      if (options.signal?.aborted)
        return { state: "refused", operation: null, operationId: null, reason: "cancelled_before_submit" };
      const deadline = this.now() + timeoutMs;
      posted = true;
      const accepted = await this.transport.request({
        method: "POST", path: "/operations", access: "session", signal: options.signal,
        body: { action, target, params: clone(params) },
      });
      if (accepted.status < 200 || accepted.status >= 300)
        return { state: "refused", operation: null, operationId: null, reason: responseReason(accepted.status, accepted.body) };
      operation = normalizeOperation(accepted.body);
      if (operation.action !== action || operation.target !== target)
        throw new ContractError("accepted operation belongs to a different action/target");
      while (true) {
        if (options.signal?.aborted) throw new Error("local_wait_cancelled");
        options.onProgress?.(clone(operation));
        if (terminal.has(operation.state)) {
          return {
            state: operation.state as OperationResult["state"], operation,
            operationId: operation.id, reason: operation.state === "succeeded" ? null : text(operation.error?.code),
          };
        }
        if (this.now() >= deadline) throw new Error("operation_deadline");
        await this.sleep(Math.min(interval, Math.max(1, deadline - this.now())), options.signal);
        if (options.signal?.aborted || this.now() >= deadline) throw new Error("operation_deadline_or_cancelled");
        const response = await this.transport.request({
          method: "GET", path: `/operations/${encodeURIComponent(operation.id)}`, access: "session", signal: options.signal,
        });
        if (response.status < 200 || response.status >= 300) throw new ContractError(responseReason(response.status, response.body));
        const next = normalizeOperation(response.body);
        if (next.id !== operation.id || next.action !== action || next.target !== target)
          throw new ContractError("polled operation identity changed");
        operation = next;
      }
    } catch (error) {
      if (error instanceof ActionUnavailableError || error instanceof ActionBusyError || !posted) throw error;
      return {
        state: "unconfirmed", operation, operationId: operation?.id ?? null,
        reason: error instanceof Error ? error.message : "transport_or_contract_error",
      };
    } finally { if (exclusive) this.active.delete(resource); }
  }

  /** Explicit cooperative cancel; aborting the local wait never claims the server cancelled. */
  async cancelOperation(operation: Operation, signal?: AbortSignal): Promise<Operation> {
    if (!this.capability(operation.action)?.cancelable) throw new ActionUnavailableError(operation.action, "operation is not cancelable");
    if (terminal.has(operation.state)) return operation;
    const response = await this.transport.request({
      method: "POST", path: `/operations/${encodeURIComponent(operation.id)}/cancel`, access: "session", signal, body: {},
    });
    if (response.status < 200 || response.status >= 300) throw new ContractError(responseReason(response.status, response.body));
    const cancelled = normalizeOperation(response.body);
    if (cancelled.id !== operation.id || cancelled.action !== operation.action || cancelled.target !== operation.target)
      throw new ContractError("cancel response identity changed");
    return cancelled;
  }

  private async readAction<T>(action: string, normalize: (result: unknown) => T, signal?: AbortSignal): Promise<T> {
    const result = await this.runAction(action, "model", {}, { intent: "read", signal });
    if (result.state !== "succeeded" || !result.operation?.result)
      throw new ContractError(result.reason ?? `action ${result.state}`);
    return normalize(result.operation.result);
  }

  readCatalog(signal?: AbortSignal): Promise<Catalog> { return this.readAction("model.catalog", normalizeCatalog, signal); }
  readContexts(signal?: AbortSignal): Promise<ContextView[]> { return this.readAction("model.context_overrides", normalizeContexts, signal); }
  readContextEvidence(signal?: AbortSignal): Promise<ContextEvidence[]> { return this.readAction("model.context_probe_preview", normalizeContextEvidence, signal); }

  async saveSettings(source: string, modelId: string, fields: Partial<ModelSettings>, expectedVersion: number | null, signal?: AbortSignal): Promise<OperationResult> {
    if (!source || !modelId || (expectedVersion !== null && (!Number.isSafeInteger(expectedVersion) || expectedVersion < 0)))
      throw new ContractError("settings identity/version is invalid");
    if (!object(fields) || !Object.keys(fields).length || Object.keys(fields).some(key => !["alias", "contextSource", "contextTokens", "defaultEffort"].includes(key)))
      throw new ContractError("settings patch must contain only supported fields");
    return this.runAction("model.settings_save", "model", {
      source, model_id: modelId, ...clone(fields), ...(expectedVersion !== null ? { expected_draft_version: expectedVersion } : {}),
    }, { intent: "draft", signal, resourceKey: `settings|${source}|${modelId}` });
  }

  async previewSettings(selection: Readonly<Record<string, number>>, signal?: AbortSignal): Promise<SettingsPreview> {
    const snapshot = selectionSnapshot(selection);
    const result = await this.runAction("model.settings_preview", "model", { selection: snapshot }, { intent: "read", signal });
    if (result.state !== "succeeded" || !result.operation?.result) throw new ContractError(result.reason ?? `preview ${result.state}`);
    const raw = result.operation.result;
    const returned = selectionSnapshot(raw.selection as Readonly<Record<string, number>>);
    if (!selectionsMatch(snapshot, returned)) throw new ContractError("preview selection does not match the submitted draft versions");
    const token = text(raw.preview_token);
    if (!token || !/^[a-f0-9]{64}$/i.test(token)) throw new ContractError("preview binding token is missing/invalid");
    if (!Array.isArray(raw.changes) || raw.changes.some(row => !object(row))) throw new ContractError("preview changes must be an object array");
    return Object.freeze({
      selection: Object.freeze(returned), previewToken: token,
      changes: clone(raw.changes) as JsonObject[], plan: clone(raw.plan),
      warnings: Array.isArray(raw.warnings) ? raw.warnings.filter((row): row is string => typeof row === "string") : [],
      status: text(raw.status),
    });
  }

  applySettings(preview: SettingsPreview, currentSelection: Readonly<Record<string, number>>, signal?: AbortSignal): Promise<OperationResult> {
    const selection = selectionSnapshot(currentSelection);
    if (!selectionsMatch(selectionSnapshot(preview.selection), selection)) throw new ContractError("draft selection changed after preview; preview again");
    if (!/^[a-f0-9]{64}$/i.test(preview.previewToken)) throw new ContractError("a valid settings preview is required");
    return this.runAction("model.settings_apply", "model", { selection, preview_token: preview.previewToken }, {
      intent: "client-write", signal, resourceKey: "settings-apply",
    });
  }

  previewSync(scope: SyncScope, signal?: AbortSignal): Promise<OperationResult> {
    return this.runAction("model.sync_preview", "model", encodeSyncScope(scope), { intent: "read", signal });
  }

  applySync(scope: SyncScope, signal?: AbortSignal): Promise<OperationResult> {
    return this.runAction("model.sync_apply", "model", encodeSyncScope(scope), { intent: "client-write", signal });
  }

  async readShareUsage(signal?: AbortSignal): Promise<ReadResult<ShareUsage>> {
    try {
      const response = await this.transport.request({ method: "GET", path: "/share-usage", access: "public", signal });
      if (response.status < 200 || response.status >= 300) return {
        state: response.status === 404 ? "unavailable" : response.status === 401 || response.status === 403 ? "refused" : "error",
        value: null, httpStatus: response.status, reason: responseReason(response.status, response.body),
      };
      return { state: "ready", value: normalizeShareUsage(response.body), httpStatus: response.status, reason: null };
    } catch (error) {
      if (signal?.aborted) throw error;
      return { state: "error", value: null, httpStatus: null, reason: error instanceof Error ? error.message : "network_error" };
    }
  }

  /** Narrow direct read of one selected gateway's connection info. No operation
   *  record, no polling, no automatic probe; the transport owns the timeout.
   *  The response identity is bound to the requested service, so a response for
   *  another gateway is refused instead of being displayed under this one. */
  async readGatewayConnection(service: string, signal?: AbortSignal): Promise<ReadResult<GatewayConnection>> {
    if (service !== "chat2api") throw new ContractError("public console only reads chat2api");
    const params = new URLSearchParams({ service });
    try {
      const response = await this.transport.request({
        method: "GET", path: `/gateway-connection?${params}`, access: "session", signal,
      });
      if (response.status < 200 || response.status >= 300) return {
        state: response.status === 404 ? "unavailable" : response.status === 401 || response.status === 403 ? "refused" : "error",
        value: null, httpStatus: response.status, reason: responseReason(response.status, response.body),
      };
      return { state: "ready", value: normalizeGatewayConnection(response.body, service), httpStatus: response.status, reason: null };
    } catch (error) {
      if (signal?.aborted) throw error;
      return { state: "error", value: null, httpStatus: null, reason: error instanceof Error ? error.message : "network_error" };
    }
  }

  async readLogs(query: { source: string; category: string; q?: string; level?: string; since?: string; until?: string; cursor?: string; rotation?: number; request_id?: string }, signal?: AbortSignal): Promise<LogPage> {
    if (!PUBLIC_LOG_SOURCES.has(query.source) || !query.category) throw new ContractError("public log source/category is invalid");
    const params = new URLSearchParams({ source: query.source, category: query.category });
    for (const key of ["q", "level", "since", "until", "cursor"] as const) if (query[key]) params.set(key, query[key]);
    if (query.request_id !== undefined) {
      if (!/^[A-Za-z0-9_.:-]{1,64}$/.test(query.request_id)) throw new ContractError("invalid request identity");
      params.set("request_id", query.request_id);
    }
    if (query.rotation !== undefined) {
      if (!Number.isSafeInteger(query.rotation) || query.rotation < 0) throw new ContractError("invalid log rotation");
      if (query.rotation > 0) params.set("rotation", String(query.rotation));
    }
    const response = await this.transport.request({ method: "GET", path: `/log-entries?${params}`, access: "session", signal });
    if (response.status < 200 || response.status >= 300) throw new ContractError(responseReason(response.status, response.body));
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

  get current(): GatewayConnectionView | null { return this.slot; }

  async select(service: string, signal?: AbortSignal): Promise<GatewayConnectionView | null> {
    if (typeof service !== "string" || !service.trim())
      throw new ContractError("gateway service identity is required");
    this.clear();
    if (signal?.aborted) return null;
    const generation = ++this.generation;
    const cancelled = () => this.requests.cancel(bindingLane);
    signal?.addEventListener("abort", cancelled, { once: true });
    try {
      const outcome = await this.requests.run(bindingLane, local => this.client.readGatewayConnection(service, local));
      if (outcome.state !== "current" || generation !== this.generation || signal?.aborted) return null;
      const view: GatewayConnectionView = { service, result: outcome.value, readAt: this.now() };
      this.slot = view;
      return view;
    } finally { signal?.removeEventListener("abort", cancelled); }
  }

  /** Drop the previous key immediately; a late response is never installed. */
  clear(): void {
    this.generation += 1;
    this.slot = null;
    this.requests.cancel(bindingLane);
  }
}
