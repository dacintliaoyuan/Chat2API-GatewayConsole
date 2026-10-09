import type {
  ActionCapability, Catalog, ConnectionAuth, ConnectionKeyInUse, ConnectionKeySourceState,
  ConnectionKeyState, ConnectionProtocolFace, ContextEvidence, ContextView, CostCoverage,
  CostDetail, GatewayConnection, JsonObject, KeyUsage, LogPage, ModelDetail, ModelSettings,
  Operation, OperationState, OutputCap, SettingsApply, ShareUsage, SyncScope, TokenUsage,
} from "./types.ts";

export class ContractError extends Error {
  constructor(message: string) { super(message); this.name = "ContractError"; }
}

export function object(value: unknown): JsonObject | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject : null;
}
export function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}
export function boolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}
/** Strict numeric evidence: zero survives; booleans, strings and infinities do not. */
export function nonnegative(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}
export function count(value: unknown): number | null {
  const number = nonnegative(value);
  return number !== null && Number.isSafeInteger(number) ? number : null;
}
export function positiveCount(value: unknown): number | null {
  const number = count(value);
  return number !== null && number > 0 ? number : null;
}
function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}
function requiredObject(value: unknown, name: string): JsonObject {
  const result = object(value);
  if (!result) throw new ContractError(`${name} must be an object`);
  return result;
}
function requiredRows(value: unknown, name: string): JsonObject[] {
  if (!Array.isArray(value) || value.some(row => !object(row)))
    throw new ContractError(`${name} must be an array of objects`);
  return value as JsonObject[];
}
function identity(row: JsonObject): { key: string; source: string; modelId: string } {
  const source = text(row.source);
  const modelId = text(row.model_id);
  if (!source || !modelId) throw new ContractError("model identity is missing");
  return { key: `${source}|${modelId}`, source, modelId };
}
function outputCap(value: unknown): OutputCap {
  const row = object(value);
  return { value: row ? positiveCount(row.value) : null, source: row ? text(row.source) : null };
}
export function normalizeCapabilities(value: unknown): ActionCapability[] {
  const root = requiredObject(value, "actions response");
  return requiredRows(root.actions, "actions").map(row => {
    if (!text(row.action) || boolean(row.enabled) === null)
      throw new ContractError("action capability lacks action/enabled");
    return {
      action: row.action as string, enabled: row.enabled as boolean,
      cancelable: row.cancelable === true, disabledReason: text(row.disabled_reason),
      description: text(row.description),
    };
  });
}
const operationStates = new Set<OperationState>(["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]);
export function normalizeOperation(value: unknown): Operation {
  const root = requiredObject(value, "operation response");
  const row = requiredObject(root.operation, "operation");
  if (!text(row.id) || !text(row.action) || !operationStates.has(row.state as OperationState))
    throw new ContractError("operation identity/state is invalid");
  return {
    id: row.id as string, action: row.action as string, target: text(row.target) ?? "",
    state: row.state as OperationState, result: object(row.result), error: object(row.error),
    cancelRequested: row.cancel_requested === true,
    createdAt: text(row.created_at), finishedAt: text(row.finished_at),
  };
}
export function normalizeCatalog(value: unknown): Catalog {
  const root = requiredObject(value, "catalog result");
  const catalogs = requiredObject(root.catalogs, "catalogs");
  const models: Catalog["models"] = [];
  for (const [source, values] of Object.entries(catalogs)) {
    if (source !== "um") continue;
    for (const row of requiredRows(values, `catalogs.${source}`)) {
      if (!text(row.id)) throw new ContractError("catalog model id is missing");
      models.push({
        key: `${source}|${row.id}`, source, modelId: row.id as string,
        displayName: text(row.display_name), declaredContext: count(row.context),
        declaredState: text(row.context_state), outputCap: outputCap(row.output_token_cap),
        supportedEfforts: stringList(row.efforts), effortsSource: text(row.efforts_source),
        defaultEffort: text(row.default_effort), published: boolean(row.publish),
      });
    }
  }
  return { models, warnings: stringList(root.warnings), checkedEpoch: count(root.checked_epoch) };
}
function settings(value: unknown): ModelSettings | null {
  const row = object(value);
  if (!row) return null;
  return {
    alias: text(row.alias),
    contextSource: ["declared", "manual", "measured"].includes(row.contextSource as string)
      ? row.contextSource as ModelSettings["contextSource"] : null,
    contextTokens: positiveCount(row.contextTokens), defaultEffort: text(row.defaultEffort),
  };
}
export function normalizeContexts(value: unknown): ContextView[] {
  const root = requiredObject(value, "context result");
  return requiredRows(root.overrides, "overrides").filter(row => row.source === "um").map(row => ({
    ...identity(row), declaredTokens: count(row.declared_context), declaredAt: text(row.declared_at),
    appliedContextTokens: count(row.override_context), effectiveContextTokens: count(row.effective_context),
    effectiveSource: text(row.effective_source), draft: settings(row.settings_draft),
    draftVersion: count(row.settings_draft_version), applied: settings(row.settings_applied),
    appliedVersion: count(row.settings_applied_version), appliedAt: text(row.settings_applied_at),
    held: boolean(row.settings_held), clientContextTokens: count(row.client_context),
    clientReadbackConsumer: text(row.client_readback_consumer),
    clientReadbackState: ["ok", "absent", "unavailable"].includes(row.client_readback_state as string)
      ? row.client_readback_state as ContextView["clientReadbackState"] : "unknown",
    clientMatchesApplied: boolean(row.client_matches_applied),
    clientOutputCap: outputCap(row.client_output_token_cap),
    clientAlias: text(row.client_alias), clientDefaultEffort: text(row.client_default_effort),
    clientReadbacks: (Array.isArray(row.client_readbacks) ? row.client_readbacks : [])
      .flatMap(value => {
        const item = object(value);
        if (!item || item.consumer !== "ZCode") return [];
        const matches = object(item.matchesApplied) ?? {};
        return [{ consumer: item.consumer as string,
          state: ["ok", "absent", "unavailable"].includes(item.state as string)
            ? item.state as ContextView["clientReadbackState"] : "unknown" as const,
          contextTokens: positiveCount(item.contextTokens), alias: text(item.alias),
          defaultEffort: text(item.defaultEffort), outputCap: outputCap(item.outputTokenCap),
          matchesApplied: { context: boolean(matches.context), alias: boolean(matches.alias),
            defaultEffort: boolean(matches.defaultEffort) } }];
      }),
  }));
}
export function normalizeContextEvidence(value: unknown): ContextEvidence[] {
  const root = requiredObject(value, "context probe preview result");
  return requiredRows(root.records, "records").filter(row => row.source === "um").map(row => ({
    ...identity(row), declaredTokens: count(row.declared_context),
    verifiedTokenFloor: count(row.token_evidence),
    observedCharacterFloor: count(row.observed_floor), observedCharacterCeiling: count(row.observed_ceiling),
    status: text(row.status), evidenceRevision: count(row.evidence_revision),
    schemeRevision: count(row.scheme_revision), lastSuccess: text(row.last_success),
    lastRun: text(row.last_run), staleReason: text(row.stale_reason), historyCount: count(row.history_count),
    history: (Array.isArray(row.history) ? row.history : []).flatMap(value => {
      const item = object(value);
      if (!item) return [];
      return [{ evidenceRevision: count(item.evidence_revision),
        verifiedTokenFloor: count(item.token_evidence), observedCharacterFloor: count(item.observed_floor),
        observedCharacterCeiling: count(item.observed_ceiling), status: text(item.status),
        lastRun: text(item.last_run), lastSuccess: text(item.last_success),
        staleReason: text(item.stale_reason), archivedAt: text(item.archived_at) }];
    }),
  }));
}
/** Join by source + real model ID; never by display name, row order or an alias. */
export function joinModelDetails(catalog: Catalog, contexts: ContextView[], evidence: ContextEvidence[]): ModelDetail[] {
  const rows = new Map<string, ModelDetail>();
  for (const row of catalog.models) rows.set(row.key, { key: row.key, catalog: row, context: null, evidence: null });
  for (const row of contexts) {
    const detail = rows.get(row.key) ?? { key: row.key, catalog: null, context: null, evidence: null };
    detail.context = row; rows.set(row.key, detail);
  }
  for (const row of evidence) {
    const detail = rows.get(row.key) ?? { key: row.key, catalog: null, context: null, evidence: null };
    detail.evidence = row; rows.set(row.key, detail);
  }
  return [...rows.values()].sort((a, b) => a.key.localeCompare(b.key));
}
function exactStrings(values: readonly string[] | undefined): string[] {
  if (values === undefined) return [];
  if (!Array.isArray(values) || values.some(value => typeof value !== "string" || !value.trim()))
    throw new ContractError("selected scope contains an invalid identity");
  return [...new Set(values)].sort();
}
/** The registry requires JSON array strings for sources/consumers, and an object for models. */
export function encodeSyncScope(scope: SyncScope): JsonObject {
  if (scope.mode === "all") {
    if (["sources", "consumers", "models"].some(key => key in scope))
      throw new ContractError("all scope cannot contain selected dimensions");
    return { scope_mode: "selected", sources: JSON.stringify(["um"]), consumers: JSON.stringify(["ZCode"]) };
  }
  if (scope.mode !== "selected") throw new ContractError("scope mode must be explicit");
  const sources = exactStrings(scope.sources);
  const consumers = exactStrings(scope.consumers);
  if (sources.some(source => source !== "um") || consumers.some(consumer => consumer !== "ZCode"))
    throw new ContractError("public console only supports chat2api to ZCode");
  const models: Record<string, string[]> = {};
  if (scope.models !== undefined && !object(scope.models)) throw new ContractError("models must be an object");
  for (const [source, ids] of Object.entries(scope.models ?? {})) {
    if (source !== "um") throw new ContractError("public model source must be um");
    const exact = exactStrings(ids);
    if (exact.length) models[source] = exact;
  }
  if (!sources.length && !consumers.length && !Object.keys(models).length)
    throw new ContractError("selected scope cannot be empty");
  return {
    scope_mode: "selected", sources: JSON.stringify(["um"]), consumers: JSON.stringify(["ZCode"]),
    ...(Object.keys(models).length ? { models } : {}),
  };
}
export function normalizeLogPage(value: unknown): LogPage {
  const root = requiredObject(value, "log page");
  if (!text(root.status)) throw new ContractError("log status is missing");
  return {
    status: root.status as string,
    events: requiredRows(root.rows, "log rows").map(row => {
      const event = object(row.event) ?? {};
      return {
        timestamp: text(event.timestamp) ?? text(row.ts), level: text(row.level),
        model: text(event.model), requestId: text(event.request_id), phase: text(event.phase),
        outcome: text(event.outcome), errorCode: text(event.error_code),
        durationMs: count(event.duration_ms), tokenRate: nonnegative(event.token_rate),
        tokenRateKind: text(event.token_rate_kind), rawMessage: text(row.message),
        parsed: boolean(row.parse_ok), cut: boolean(row.cut),
      };
    }),
    nextCursor: text(root.next_cursor), resetHint: root.reset_hint === true,
    truncated: boolean(root.truncated), scannedBytes: count(root.scanned_bytes), error: text(root.error),
    requestId: text(root.request_id), requestScope: root.request_scope === "read_window" ? "read_window" : null,
  };
}
function costCoverage(row: JsonObject): CostCoverage {
  const coverage = object(row.coverage) ?? {};
  return {
    requests: count(row.requests), unknownRequests: count(row.unknownRequests),
    unpricedRequests: count(row.unpricedRequests), incompleteRequests: count(row.incompleteRequests),
    records: count(coverage.records), pricedRequests: count(coverage.pricedRequests),
    unknownTimeRequests: count(coverage.unknownTimeRequests), pricingUnavailableRequests: count(coverage.pricingUnavailableRequests),
    costComplete: boolean(coverage.costComplete),
    localCoverageAvailable: count(row.unknownRequests) !== null && count(row.unpricedRequests) !== null &&
      count(coverage.records) !== null && boolean(coverage.costComplete) !== null,
  };
}
function tokenUsage(value: unknown): TokenUsage {
  const row = object(value) ?? {};
  return {
    totalTokens: count(row.totalTokens), inputTokens: count(row.inputTokens), outputTokens: count(row.outputTokens),
    lowerBoundTokens: count(row.lowerBoundTokens), lowerBoundInputTokens: count(row.lowerBoundInputTokens),
    lowerBoundOutputTokens: count(row.lowerBoundOutputTokens), estimatedTokens: count(row.estimatedTokens),
    estimatedInputTokens: count(row.estimatedInputTokens), estimatedOutputTokens: count(row.estimatedOutputTokens),
    completeRequests: count(row.completeRequests), lowerBoundRequests: count(row.lowerBoundRequests),
    estimatedRequests: count(row.estimatedRequests), unknownRequests: count(row.unknownRequests), complete: boolean(row.complete),
  };
}
function summary(row: JsonObject): Omit<CostDetail, "identity"> {
  return {
    knownCostUsd: nonnegative(row.knownCostUsd), estimatedCostUsd: nonnegative(row.estimatedCostUsd),
    // Legacy per-side sums are retained for compatibility; complete totals are only tokenUsage.totalTokens.
    inputTokens: count(row.inputTokens), outputTokens: count(row.outputTokens), coverage: costCoverage(row),
    tokenUsage: tokenUsage(row.tokenUsage), priceVersions: stringList(row.priceVersions),
  };
}
function costDetails(value: unknown, key: "model" | "day"): CostDetail[] {
  return requiredRows(value, key === "model" ? "key models" : "key days").map(row => {
    if (!text(row[key])) throw new ContractError(`cost detail ${key} is missing`);
    return { identity: row[key] as string, ...summary(row) };
  });
}
/** 未归属桶的模型/日明细是**加性**的：后端没带这两列就是没有行（不是伪造的
 *  空行），带了就按与每 Key 行同一套身份 + 摘要规则归一化。身份不是字符串
 *  的行无法归到任何名字下——按页面既有规则跳过，绝不用猜出来的身份合并。 */
function bucketDetails(value: unknown, key: "model" | "day"): CostDetail[] {
  if (value === undefined) return [];
  const rows = requiredRows(value, key === "model" ? "unattributed models" : "unattributed days");
  return rows.flatMap(row => {
    const identity = text(row[key]);
    return identity === null ? [] : [{ identity, ...summary(row) }];
  });
}
export function normalizeShareUsage(value: unknown): ShareUsage {
  const root = requiredObject(value, "share usage");
  const keys: KeyUsage[] = requiredRows(root.keys, "keys").map(row => {
    if (!text(row.key_id)) throw new ContractError("key_id is missing");
    return {
      keyId: row.key_id as string, keyName: text(row.keyName), deleted: boolean(row.deleted),
      ...summary(row),
      models: costDetails(row.models, "model"), days: costDetails(row.days, "day"),
      undated: object(row.undated) ? summary(row.undated as JsonObject) : null,
    };
  });
  // 未归属桶走与每 Key 行**同一个** `summary()`：Token 细分（完整 / 下界 /
  // 估算 / 未知）与费用分列因此与 Key 行同形，页面不再自己解析；桶本身仍
  // 单列（`keyId` 恒为 null），绝不摊到任何 Key。
  const unattributedRow = requiredObject(root.unattributed, "unattributed");
  return {
    keys,
    unattributed: {
      keyId: null, ...summary(unattributedRow),
      models: bucketDetails(unattributedRow.models, "model"),
      days: bucketDetails(unattributedRow.days, "day"),
    },
    coverage: requiredObject(root.coverage, "coverage"),
    checkedAt: text(root.checkedAt), requests: count(root.requests),
  };
}

export function normalizeSettingsApply(value: unknown): SettingsApply {
  const row = requiredObject(value, "settings apply result");
  const applied = stringList(row.applied);
  const deferred = stringList(row.deferred);
  if (!Array.isArray(row.applied) || !Array.isArray(row.deferred) ||
      applied.length !== row.applied.length || deferred.length !== row.deferred.length)
    throw new ContractError("settings apply must distinguish applied/deferred arrays");
  if (applied.some(key => deferred.includes(key)))
    throw new ContractError("one model cannot be both applied and deferred");
  return {
    applied, deferred, skipped: Array.isArray(row.skipped) ? row.skipped : [],
    status: text(row.status), warnings: stringList(row.warnings),
    pendingDetails: row.pendingDetails === undefined ? [] : requiredRows(row.pendingDetails, "pendingDetails"),
    note: text(row.note),
  };
}

// Rev15 (2026-10-06) gateway connection read face. Shape errors throw; a state
// outside the closed set is a contract error, never silently turned into a "no key"
// state. Error messages never interpolate payload values, so a plaintext key cannot
// escape through an error text.
const connectionKeyStates = new Set<ConnectionKeyState>(["present", "absent", "needs_key_unconfigured", "unreadable", "unknown"]);
const connectionKeySourceStates = new Set<ConnectionKeySourceState>(["present", "absent", "unreadable", "unknown"]);
const connectionKeyInUses = new Set<ConnectionKeyInUse>(["per_request", "startup_env", "config_consumed", "unknown"]);
const connectionAuths = new Set<ConnectionAuth>(["bearer", "loopback_no_key", "unknown"]);

function requiredString(value: unknown, name: string): string {
  if (typeof value !== "string") throw new ContractError(`${name} must be a string`);
  return value;
}
function requiredText(value: unknown, name: string): string {
  if (typeof value !== "string" || !value.trim()) throw new ContractError(`${name} is missing`);
  return value;
}
/** Missing keys are contract errors; an explicit null stays null. */
function nullableText(value: unknown, name: string): string | null {
  if (value === undefined) throw new ContractError(`${name} is missing`);
  if (value === null) return null;
  if (typeof value !== "string") throw new ContractError(`${name} must be a string or null`);
  return value;
}
function nullableCount(value: unknown, name: string): number | null {
  if (value === undefined) throw new ContractError(`${name} is missing`);
  if (value === null) return null;
  const result = count(value);
  if (result === null) throw new ContractError(`${name} must be a nonnegative integer or null`);
  return result;
}
function requiredCount(value: unknown, name: string): number {
  const result = count(value);
  if (result === null) throw new ContractError(`${name} is missing or invalid`);
  return result;
}
function nullableBoolean(value: unknown, name: string): boolean | null {
  if (value === undefined) throw new ContractError(`${name} is missing`);
  if (value === null) return null;
  if (typeof value !== "boolean") throw new ContractError(`${name} must be a boolean or null`);
  return value;
}
function nullablePort(value: unknown, name: string): number | null {
  const port = nullableCount(value, name);
  if (port !== null && (port < 1 || port > 65535)) throw new ContractError(`${name} is out of range`);
  return port;
}
function requiredStringList(value: unknown, name: string): string[] {
  if (!Array.isArray(value) || value.some(item => typeof item !== "string"))
    throw new ContractError(`${name} must be an array of strings`);
  return [...value] as string[];
}
function closedState<T extends string>(value: unknown, allowed: ReadonlySet<T>, name: string): T {
  if (typeof value !== "string" || !allowed.has(value as T))
    throw new ContractError(`${name} is not in the contract set`);
  return value as T;
}

/** Gateway connection read face; `expectedService` binds the response identity to
 *  the requested gateway so a late or foreign response cannot be installed. */
export function normalizeGatewayConnection(value: unknown, expectedService?: string): GatewayConnection {
  const root = requiredObject(value, "gateway connection");
  const service = requiredText(root.service, "gateway connection service");
  if (expectedService !== undefined && service !== expectedService)
    throw new ContractError("gateway connection response belongs to a different service");
  const keyState = closedState(root.key_state, connectionKeyStates, "gateway connection key state");
  const auth = closedState(root.auth, connectionAuths, "gateway connection auth");
  const keyPlaintext = nullableText(root.key_plaintext, "gateway connection plaintext key");
  if (keyState === "present") {
    if (!keyPlaintext || !keyPlaintext.trim())
      throw new ContractError("gateway connection key state present requires a plaintext key");
  } else if (keyPlaintext !== null) {
    throw new ContractError("gateway connection plaintext key is only allowed while the key state is present");
  }
  // Cross-field invariants mirrored from the read face. The key mechanism is a
  // separate fact from the key state: a state that carries (or needs) a value must
  // name how the running instance consumes it, while `unknown` never claims a
  // mechanism it could not confirm. `absent` is only produced by the chat2api
  // instance record, which marks the mechanism `startup_env` before the only
  // `key_state = "absent"` assignment (console_gateway_connection.py:596 then
  // :637; file sources never produce `absent`), so any other mechanism there
  // would be an invented pair.
  const keyInUse = closedState(root.key_in_use, connectionKeyInUses, "gateway connection key in use");
  if (keyState === "absent") {
    if (keyInUse !== "startup_env")
      throw new ContractError("gateway connection key state and key mechanism do not agree");
  } else if (keyState !== "unknown" && keyInUse === "unknown") {
    throw new ContractError("gateway connection key state requires a known key mechanism");
  }
  // Auth is a separate axis from the key state: `bearer` says clients must
  // present a key, not that a key was read. The read face leaves auth `unknown`
  // while the instance is unconfirmed and keeps `bearer` for a readable key file
  // whose config consistency check failed (:483 then :489-494), so only the
  // `loopback_no_key` pair — produced solely with `absent` — stays refused.
  if (keyState === "absent") {
    if (auth !== "loopback_no_key")
      throw new ContractError("gateway connection key state and auth do not agree");
  } else if (keyState === "unknown") {
    if (auth !== "bearer" && auth !== "unknown")
      throw new ContractError("gateway connection key state and auth do not agree");
  } else if (auth !== "bearer") {
    throw new ContractError("gateway connection key state and auth do not agree");
  }
  const alternateProtocols: ConnectionProtocolFace[] =
    root.alternate_protocols === undefined || root.alternate_protocols === null ? []
      : requiredRows(root.alternate_protocols, "alternate protocols").map(row => ({
        protocol: requiredText(row.protocol, "alternate protocol"),
        path: requiredText(row.path, "alternate protocol path"),
        note: nullableText(row.note, "alternate protocol note"),
      }));
  return {
    service,
    displayName: nullableText(root.display_name, "gateway connection display name"),
    port: nullablePort(root.port, "gateway connection port"),
    baseUrl: nullableText(root.base_url, "gateway connection base URL"),
    protocol: requiredText(root.protocol, "gateway connection protocol"),
    protocolPath: requiredText(root.protocol_path, "gateway connection protocol path"),
    alternateProtocols,
    healthPath: requiredText(root.health_path, "gateway connection health path"),
    healthPathSource: requiredText(root.health_path_source, "gateway connection health path source"),
    runtimeState: nullableText(root.runtime_state, "gateway connection runtime state"),
    runtimeHealthy: nullableBoolean(root.runtime_healthy, "gateway connection runtime healthy"),
    auth,
    keyState, keyPlaintext,
    keySource: nullableText(root.key_source, "gateway connection key source"),
    keySourceState: closedState(root.key_source_state, connectionKeySourceStates, "gateway connection key source state"),
    keyInUse,
    reason: requiredString(root.reason, "gateway connection reason"),
    evidence: requiredStringList(root.evidence, "gateway connection evidence"),
    checkedEpoch: requiredCount(root.checked_epoch, "gateway connection checked time"),
  };
}
