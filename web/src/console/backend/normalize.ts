import type {
  ActionCapability,
  Catalog,
  CloudOpsSection,
  CloudSecurityStatus,
  ConnectionAuth,
  ConnectionKeyInUse,
  ConnectionKeySourceState,
  ConnectionKeyState,
  ConnectionProtocolFace,
  ContextEvidence,
  ContextView,
  UsageCoverage,
  UsageDetail,
  GatewayConnection,
  JsonObject,
  KeyUsage,
  LogPage,
  ModelDetail,
  ModelSettings,
  Operation,
  OperationState,
  OutputCap,
  PublicDnsDetail,
  PublicDnsSource,
  PublicDnsSourceState,
  PublicDnsState,
  PublicShareBudget,
  PublicShareCheck,
  PublicShareEntry,
  PublicShareEntryState,
  PublicShareEvent,
  PublicShareEventsPage,
  PublicShareExport,
  PublicShareIntent,
  PublicShareOverall,
  PublicShareRecovery,
  PublicShareRecoveryState,
  PublicShareRound,
  PublicShareSnapshot,
  PublicShareStatus,
  SettingsApply,
  ShareCacheReportStatus,
  ShareCapacity,
  ShareRequestResult,
  ShareUsage,
  ShareUsageFilters,
  ShareUsageRequestRow,
  ShareUsageRequestsPage,
  ShareUsageResults,
  ShareUsageSource,
  ShareUsageWindow,
  SyncScope,
  TokenUsage,
  UmBucketPayload,
  UmCatalogModelPayload,
  UmCatalogPayload,
  UmLogLevel,
  UmLogLinePayload,
  UmLogPagePayload,
  UmMaintenancePagePayload,
  UmMaintenanceRowPayload,
  UmOperationPayload,
  UmOperationsPayload,
  UmReading,
  UmReadingKind,
  UmRequestLogsPayload,
  UmRequestPagePayload,
  UmRequestResult,
  UmRequestRowPayload,
  UmRequestsResponse,
  UmServiceEntry,
  UmServicesPayload,
  UmSummaryPayload,
  UmTodayPayload,
  UmTodayResponse,
  UmTrendModelPayload,
  UmTrendPayload,
  UmTrendRange,
  UmTrendResponse,
  UmUpstreamFaultPayload,
  UmUpstreamFaultsPayload,
  UmUpstreamRecoveryPayload,
} from "./types.ts";

export class ContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ContractError";
  }
}

export function object(value: unknown): JsonObject | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : null;
}
export function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}
export function boolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}
/** Strict numeric evidence: zero survives; booleans, strings and infinities do not. */
export function nonnegative(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
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
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}
function requiredObject(value: unknown, name: string): JsonObject {
  const result = object(value);
  if (!result) throw new ContractError(`${name} must be an object`);
  return result;
}
function requiredRows(value: unknown, name: string): JsonObject[] {
  if (!Array.isArray(value) || value.some((row) => !object(row)))
    throw new ContractError(`${name} must be an array of objects`);
  return value as JsonObject[];
}
function identity(row: JsonObject): {
  key: string;
  source: string;
  modelId: string;
} {
  const source = text(row.source);
  const modelId = text(row.model_id);
  if (!source || !modelId) throw new ContractError("model identity is missing");
  return { key: `${source}|${modelId}`, source, modelId };
}
function outputCap(value: unknown): OutputCap {
  const row = object(value);
  return {
    value: row ? positiveCount(row.value) : null,
    source: row ? text(row.source) : null,
  };
}
export function normalizeCapabilities(value: unknown): ActionCapability[] {
  const root = requiredObject(value, "actions response");
  return requiredRows(root.actions, "actions").map((row) => {
    if (!text(row.action) || boolean(row.enabled) === null)
      throw new ContractError("action capability lacks action/enabled");
    return {
      action: row.action as string,
      enabled: row.enabled as boolean,
      cancelable: row.cancelable === true,
      disabledReason: text(row.disabled_reason),
      description: text(row.description),
    };
  });
}
const operationStates = new Set<OperationState>([
  "queued",
  "running",
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
]);
export function normalizeOperation(value: unknown): Operation {
  const root = requiredObject(value, "operation response");
  const row = requiredObject(root.operation, "operation");
  if (
    !text(row.id) ||
    !text(row.action) ||
    !operationStates.has(row.state as OperationState)
  )
    throw new ContractError("operation identity/state is invalid");
  return {
    id: row.id as string,
    action: row.action as string,
    target: text(row.target) ?? "",
    state: row.state as OperationState,
    result: object(row.result),
    error: object(row.error),
    cancelRequested: row.cancel_requested === true,
    createdAt: text(row.created_at),
    finishedAt: text(row.finished_at),
    steps: (Array.isArray(row.steps) ? row.steps : []).flatMap((step) => {
      const line = object(step);
      const at = epochMs(line?.at);
      return line && at !== null ? [{ at, note: text(line.note) ?? "" }] : [];
    }),
  };
}
export function normalizeCatalog(value: unknown): Catalog {
  const root = requiredObject(value, "catalog result");
  const catalogs = requiredObject(root.catalogs, "catalogs");
  const models: Catalog["models"] = [];
  for (const [source, values] of Object.entries(catalogs)) {
    for (const row of requiredRows(values, `catalogs.${source}`)) {
      if (!text(row.id)) throw new ContractError("catalog model id is missing");
      models.push({
        key: `${source}|${row.id}`,
        source,
        modelId: row.id as string,
        displayName: text(row.display_name),
        declaredContext: count(row.context),
        declaredState: text(row.context_state),
        outputCap: outputCap(row.output_token_cap),
        supportedEfforts: stringList(row.efforts),
        effortsSource: text(row.efforts_source),
        defaultEffort: text(row.default_effort),
        published: boolean(row.publish),
      });
    }
  }
  return {
    models,
    warnings: stringList(root.warnings),
    checkedEpoch: count(root.checked_epoch),
  };
}
function settings(value: unknown): ModelSettings | null {
  const row = object(value);
  if (!row) return null;
  return {
    alias: text(row.alias),
    contextSource: ["declared", "manual", "measured"].includes(
      row.contextSource as string
    )
      ? (row.contextSource as ModelSettings["contextSource"])
      : null,
    contextTokens: positiveCount(row.contextTokens),
    defaultEffort: text(row.defaultEffort),
  };
}
export function normalizeContexts(value: unknown): ContextView[] {
  const root = requiredObject(value, "context result");
  return requiredRows(root.overrides, "overrides").map((row) => ({
    ...identity(row),
    declaredTokens: count(row.declared_context),
    declaredAt: text(row.declared_at),
    appliedContextTokens: count(row.override_context),
    effectiveContextTokens: count(row.effective_context),
    effectiveSource: text(row.effective_source),
    draft: settings(row.settings_draft),
    draftVersion: count(row.settings_draft_version),
    applied: settings(row.settings_applied),
    appliedVersion: count(row.settings_applied_version),
    appliedAt: text(row.settings_applied_at),
    held: boolean(row.settings_held),
    clientContextTokens: count(row.client_context),
    clientReadbackConsumer: text(row.client_readback_consumer),
    clientReadbackState: ["ok", "absent", "unavailable"].includes(
      row.client_readback_state as string
    )
      ? (row.client_readback_state as ContextView["clientReadbackState"])
      : "unknown",
    clientMatchesApplied: boolean(row.client_matches_applied),
    clientOutputCap: outputCap(row.client_output_token_cap),
    clientAlias: text(row.client_alias),
    clientDefaultEffort: text(row.client_default_effort),
    clientReadbacks: (Array.isArray(row.client_readbacks)
      ? row.client_readbacks
      : []
    ).flatMap((value) => {
      const item = object(value);
      if (!item || !text(item.consumer)) return [];
      const matches = object(item.matchesApplied) ?? {};
      return [
        {
          consumer: item.consumer as string,
          state: ["ok", "absent", "unavailable"].includes(item.state as string)
            ? (item.state as ContextView["clientReadbackState"])
            : ("unknown" as const),
          contextTokens: positiveCount(item.contextTokens),
          alias: text(item.alias),
          defaultEffort: text(item.defaultEffort),
          outputCap: outputCap(item.outputTokenCap),
          matchesApplied: {
            context: boolean(matches.context),
            alias: boolean(matches.alias),
            defaultEffort: boolean(matches.defaultEffort),
          },
        },
      ];
    }),
  }));
}
export function normalizeContextEvidence(value: unknown): ContextEvidence[] {
  const root = requiredObject(value, "context probe preview result");
  return requiredRows(root.records, "records").map((row) => ({
    ...identity(row),
    declaredTokens: count(row.declared_context),
    verifiedTokenFloor: count(row.token_evidence),
    observedCharacterFloor: count(row.observed_floor),
    observedCharacterCeiling: count(row.observed_ceiling),
    status: text(row.status),
    evidenceRevision: count(row.evidence_revision),
    schemeRevision: count(row.scheme_revision),
    lastSuccess: text(row.last_success),
    lastRun: text(row.last_run),
    staleReason: text(row.stale_reason),
    historyCount: count(row.history_count),
    history: (Array.isArray(row.history) ? row.history : []).flatMap(
      (value) => {
        const item = object(value);
        if (!item) return [];
        return [
          {
            evidenceRevision: count(item.evidence_revision),
            verifiedTokenFloor: count(item.token_evidence),
            observedCharacterFloor: count(item.observed_floor),
            observedCharacterCeiling: count(item.observed_ceiling),
            status: text(item.status),
            lastRun: text(item.last_run),
            lastSuccess: text(item.last_success),
            staleReason: text(item.stale_reason),
            archivedAt: text(item.archived_at),
          },
        ];
      }
    ),
  }));
}
/** Join by source + real model ID; never by display name, row order or an alias. */
export function joinModelDetails(
  catalog: Catalog,
  contexts: ContextView[],
  evidence: ContextEvidence[]
): ModelDetail[] {
  const rows = new Map<string, ModelDetail>();
  for (const row of catalog.models)
    rows.set(row.key, {
      key: row.key,
      catalog: row,
      context: null,
      evidence: null,
    });
  for (const row of contexts) {
    const detail = rows.get(row.key) ?? {
      key: row.key,
      catalog: null,
      context: null,
      evidence: null,
    };
    detail.context = row;
    rows.set(row.key, detail);
  }
  for (const row of evidence) {
    const detail = rows.get(row.key) ?? {
      key: row.key,
      catalog: null,
      context: null,
      evidence: null,
    };
    detail.evidence = row;
    rows.set(row.key, detail);
  }
  return [...rows.values()].sort((a, b) => a.key.localeCompare(b.key));
}
function exactStrings(values: readonly string[] | undefined): string[] {
  if (values === undefined) return [];
  if (
    !Array.isArray(values) ||
    values.some((value) => typeof value !== "string" || !value.trim())
  )
    throw new ContractError("selected scope contains an invalid identity");
  return [...new Set(values)].sort();
}
/** The registry requires JSON array strings for sources/consumers, and an object for models. */
export function encodeSyncScope(scope: SyncScope): JsonObject {
  if (scope.mode === "all") {
    if (["sources", "consumers", "models"].some((key) => key in scope))
      throw new ContractError("all scope cannot contain selected dimensions");
    return { scope_mode: "all" };
  }
  if (scope.mode !== "selected")
    throw new ContractError("scope mode must be explicit");
  const sources = exactStrings(scope.sources);
  const consumers = exactStrings(scope.consumers);
  const models: Record<string, string[]> = {};
  if (scope.models !== undefined && !object(scope.models))
    throw new ContractError("models must be an object");
  for (const [source, ids] of Object.entries(scope.models ?? {})) {
    if (!source.trim())
      throw new ContractError("model source must be nonempty");
    const exact = exactStrings(ids);
    if (exact.length) models[source] = exact;
  }
  if (!sources.length && !consumers.length && !Object.keys(models).length)
    throw new ContractError("selected scope cannot be empty");
  return {
    scope_mode: "selected",
    ...(sources.length ? { sources: JSON.stringify(sources) } : {}),
    ...(consumers.length ? { consumers: JSON.stringify(consumers) } : {}),
    ...(Object.keys(models).length ? { models } : {}),
  };
}
export function normalizeLogPage(value: unknown): LogPage {
  const root = requiredObject(value, "log page");
  if (!text(root.status)) throw new ContractError("log status is missing");
  return {
    status: root.status as string,
    events: requiredRows(root.rows, "log rows").map((row) => {
      const event = object(row.event) ?? {};
      return {
        timestamp: text(event.timestamp) ?? text(row.ts),
        level: text(row.level),
        model: text(event.model),
        requestId: text(event.request_id),
        phase: text(event.phase),
        outcome: text(event.outcome),
        errorCode: text(event.error_code),
        durationMs: count(event.duration_ms),
        tokenRate: nonnegative(event.token_rate),
        tokenRateKind: text(event.token_rate_kind),
        rawMessage: text(row.message),
        parsed: boolean(row.parse_ok),
        cut: boolean(row.cut),
      };
    }),
    nextCursor: text(root.next_cursor),
    resetHint: root.reset_hint === true,
    truncated: boolean(root.truncated),
    scannedBytes: count(root.scanned_bytes),
    error: text(root.error),
    requestId: text(root.request_id),
    requestScope: root.request_scope === "read_window" ? "read_window" : null,
  };
}
function usageCoverage(row: JsonObject): UsageCoverage {
  const coverage = object(row.coverage) ?? {};
  return {
    requests: count(row.requests),
    unknownRequests: count(row.unknownRequests),
    incompleteRequests: count(row.incompleteRequests),
    records: count(coverage.records),
    unknownTimeRequests: count(coverage.unknownTimeRequests),
    cacheReportedRequests: count(coverage.cacheReportedRequests),
    cacheUnreportedRequests: count(coverage.cacheUnreportedRequests),
    cacheUnknownRequests: count(coverage.cacheUnknownRequests),
    excludedUnknownTimeRequests: count(coverage.excludedUnknownTimeRequests),
  };
}
function tokenUsage(value: unknown): TokenUsage {
  const row = object(value) ?? {};
  return {
    totalTokens: count(row.totalTokens),
    inputTokens: count(row.inputTokens),
    outputTokens: count(row.outputTokens),
    lowerBoundTokens: count(row.lowerBoundTokens),
    lowerBoundInputTokens: count(row.lowerBoundInputTokens),
    lowerBoundOutputTokens: count(row.lowerBoundOutputTokens),
    estimatedTokens: count(row.estimatedTokens),
    estimatedInputTokens: count(row.estimatedInputTokens),
    estimatedOutputTokens: count(row.estimatedOutputTokens),
    completeRequests: count(row.completeRequests),
    lowerBoundRequests: count(row.lowerBoundRequests),
    estimatedRequests: count(row.estimatedRequests),
    unknownRequests: count(row.unknownRequests),
    complete: boolean(row.complete),
    // Q11 cache evidence is separate from input/output coverage; these counts
    // are preserved instead of being folded into a measured zero.
    cachedInputTokens: count(row.cachedInputTokens),
    cacheReportedRequests: count(row.cacheReportedRequests),
    cacheUnreportedRequests: count(row.cacheUnreportedRequests),
    cacheUnknownRequests: count(row.cacheUnknownRequests),
  };
}
function summary(row: JsonObject): Omit<UsageDetail, "identity"> {
  return {
    // Legacy per-side sums are retained for compatibility; complete totals are only tokenUsage.totalTokens.
    inputTokens: count(row.inputTokens),
    outputTokens: count(row.outputTokens),
    coverage: usageCoverage(row),
    tokenUsage: tokenUsage(row.tokenUsage),
  };
}
function usageDetails(value: unknown, key: "model" | "day"): UsageDetail[] {
  return requiredRows(value, key === "model" ? "key models" : "key days").map(
    (row) => {
      if (!text(row[key]))
        throw new ContractError(`cost detail ${key} is missing`);
      return { identity: row[key] as string, ...summary(row) };
    }
  );
}
/** 未归属桶的模型/日明细是**加性**的：后端没带这两列就是没有行（不是伪造的
 *  空行），带了就按与每 Key 行同一套身份 + 摘要规则归一化。身份不是字符串
 *  的行无法归到任何名字下——按页面既有规则跳过，绝不用猜出来的身份合并。 */
function bucketDetails(value: unknown, key: "model" | "day"): UsageDetail[] {
  if (value === undefined) return [];
  const rows = requiredRows(
    value,
    key === "model" ? "unattributed models" : "unattributed days"
  );
  return rows.flatMap((row) => {
    const identity = text(row[key]);
    return identity === null ? [] : [{ identity, ...summary(row) }];
  });
}
function shareUsageWindow(value: unknown): ShareUsageWindow | null {
  const window = text(value);
  return window === "1h" ||
    window === "24h" ||
    window === "7d" ||
    window === "30d" ||
    window === "all"
    ? window
    : null;
}
function shareUsageFilters(value: unknown): ShareUsageFilters | null {
  const row = object(value);
  if (row === null) return null;
  return {
    window: shareUsageWindow(row.window),
    key: text(row.key),
    model: text(row.model),
    result: text(row.result),
  };
}
export function normalizeShareUsage(value: unknown): ShareUsage {
  const root = requiredObject(value, "share usage");
  const keys: KeyUsage[] = requiredRows(root.keys, "keys").map((row) => {
    if (!text(row.key_id)) throw new ContractError("key_id is missing");
    return {
      keyId: row.key_id as string,
      keyName: text(row.keyName),
      deleted: boolean(row.deleted),
      ...summary(row),
      models: usageDetails(row.models, "model"),
      days: usageDetails(row.days, "day"),
      undated: object(row.undated) ? summary(row.undated as JsonObject) : null,
    };
  });
  // 未归属桶走与每 Key 行**同一个** `summary()`：Token 细分（完整 / 下界 /
  // 估算 / 未知）因此与 Key 行同形，页面不再自己解析；桶本身仍
  // 单列（`keyId` 恒为 null），绝不摊到任何 Key。
  const unattributedRow = requiredObject(root.unattributed, "unattributed");
  return {
    keys,
    unattributed: {
      keyId: null,
      ...summary(unattributedRow),
      models: bucketDetails(unattributedRow.models, "model"),
      days: bucketDetails(unattributedRow.days, "day"),
      // The bucket's own undated row is additive too: absent means no such row.
      undated: object(unattributedRow.undated)
        ? summary(unattributedRow.undated as JsonObject)
        : null,
    },
    coverage: requiredObject(root.coverage, "coverage"),
    checkedAt: text(root.checkedAt),
    requests: count(root.requests),
    filters: shareUsageFilters(root.filters),
    results: shareUsageResults(root.results),
  };
}

/** D18-2: the backend's four-outcome split. A missing block is null (the page
 *  says 未提供); a block that is present but omits one counter keeps that counter
 *  null instead of fabricating a 0. */
function shareUsageResults(value: unknown): ShareUsageResults | null {
  const row = object(value);
  if (!row) return null;
  const coverage = object(row.coverage);
  return {
    success: count(row.success),
    failed: count(row.failed),
    cancelled: count(row.cancelled),
    unknown: count(row.unknown),
    total: count(row.total),
    unknownResult: coverage ? count(coverage.unknownResult) : null,
  };
}

const SHARE_REQUEST_RESULTS: ReadonlySet<ShareRequestResult> = new Set([
  "success",
  "failed",
  "cancelled",
  "unknown",
]);
const SHARE_USAGE_SOURCES: ReadonlySet<ShareUsageSource> = new Set([
  "upstream",
  "partial",
  "estimated",
  "none",
]);
const SHARE_CACHE_STATUSES: ReadonlySet<ShareCacheReportStatus> = new Set([
  "reported",
  "unreported",
  "unknown",
]);

function shareRequestRow(row: JsonObject, index: number): ShareUsageRequestRow {
  const result = text(row.result);
  if (
    result === null ||
    !SHARE_REQUEST_RESULTS.has(result as ShareRequestResult)
  )
    throw new ContractError(`request row ${index} result is not a known state`);
  const usageSource = text(row.usageSource);
  if (
    usageSource === null ||
    !SHARE_USAGE_SOURCES.has(usageSource as ShareUsageSource)
  )
    throw new ContractError(
      `request row ${index} usageSource is not a known state`
    );
  const cache = text(row.cacheReportStatus);
  if (
    cache === null ||
    !SHARE_CACHE_STATUSES.has(cache as ShareCacheReportStatus)
  )
    throw new ContractError(
      `request row ${index} cacheReportStatus is not a known state`
    );
  return {
    requestId: text(row.requestId),
    time: text(row.time),
    day: text(row.day),
    keyId: text(row.keyId),
    keyName: text(row.keyName),
    deleted: boolean(row.deleted),
    model: text(row.model),
    status: text(row.status),
    result: result as ShareRequestResult,
    attribution: text(row.attribution),
    usageSource: usageSource as ShareUsageSource,
    cacheReportStatus: cache as ShareCacheReportStatus,
    inputTokens: count(row.inputTokens),
    outputTokens: count(row.outputTokens),
    durationMs: count(row.durationMs),
    cacheTokens: count(row.cacheTokens),
    reasoningTokens: count(row.reasoningTokens),
  };
}

export function normalizeShareUsageRequests(
  value: unknown
): ShareUsageRequestsPage {
  const root = requiredObject(value, "share usage requests");
  return {
    rows: requiredRows(root.rows, "rows").map(shareRequestRow),
    nextCursor: text(root.nextCursor),
    truncated: boolean(root.truncated),
    filters: shareUsageFilters(root.filters),
    coverage: requiredObject(root.coverage, "coverage"),
    checkedAt: text(root.checkedAt),
  };
}

export function normalizeShareCapacity(value: unknown): ShareCapacity {
  const row = object(value) ?? {};
  return {
    configured: count(row.configured),
    inFlight: count(row.in_flight ?? row.inFlight),
    inFlightReason: text(row.in_flight_reason ?? row.inFlightReason),
    queued: count(row.queued),
    queuedReason: text(row.queued_reason ?? row.queuedReason),
    upstreamCapacity: count(row.upstream_capacity ?? row.upstreamCapacity),
    upstreamCapacityMeasured: boolean(
      row.upstream_capacity_measured ?? row.upstreamCapacityMeasured
    ),
    upstreamCapacityReason: text(
      row.upstream_capacity_reason ?? row.upstreamCapacityReason
    ),
    checkedAt: count(row.checked_at ?? row.checkedAt),
    note: text(row.note),
  };
}

function cloudOpsSection(value: unknown): CloudOpsSection {
  const row = object(value) ?? {};
  // The security section carries its evidence at the section top level, while
  // budget/backup carry a nested status object.  Keep both shapes visible.
  const nested = object(row.status);
  const topLevel =
    nested === null &&
    (row.generation_floor !== undefined ||
      row.records !== undefined ||
      row.pending_remote !== undefined ||
      row.exported_at !== undefined)
      ? {
          generation_floor: row.generation_floor,
          records: row.records,
          pending_remote: row.pending_remote,
          exported_at: row.exported_at,
        }
      : null;
  return {
    configured: row.configured === true,
    available: row.available === true,
    status: nested ?? topLevel,
    reason: text(row.reason),
    remote: object(row.remote),
    generationFloor: count(row.generation_floor),
    records: count(row.records),
    pendingRemote: count(row.pending_remote),
    exportedAt: text(row.exported_at),
  };
}

export function normalizeCloudSecurityStatus(
  value: unknown
): CloudSecurityStatus {
  const root = requiredObject(value, "cloud ops status");
  return {
    available: root.available === true,
    readAt: count(root.read_at),
    security: cloudOpsSection(root.security),
    reasons: stringList(root.reasons),
    unresolved: object(root.unresolved),
  };
}

export function normalizeSettingsApply(value: unknown): SettingsApply {
  const row = requiredObject(value, "settings apply result");
  const applied = stringList(row.applied);
  const deferred = stringList(row.deferred);
  if (
    !Array.isArray(row.applied) ||
    !Array.isArray(row.deferred) ||
    applied.length !== row.applied.length ||
    deferred.length !== row.deferred.length
  )
    throw new ContractError(
      "settings apply must distinguish applied/deferred arrays"
    );
  if (applied.some((key) => deferred.includes(key)))
    throw new ContractError("one model cannot be both applied and deferred");
  return {
    applied,
    deferred,
    skipped: Array.isArray(row.skipped) ? row.skipped : [],
    status: text(row.status),
    warnings: stringList(row.warnings),
    pendingDetails:
      row.pendingDetails === undefined
        ? []
        : requiredRows(row.pendingDetails, "pendingDetails"),
    note: text(row.note),
  };
}

// Rev15 (2026-10-06) gateway connection read face. Shape errors throw; a state
// outside the closed set is a contract error, never silently turned into a "no key"
// state. Error messages never interpolate payload values, so a plaintext key cannot
// escape through an error text.
const connectionKeyStates = new Set<ConnectionKeyState>([
  "present",
  "absent",
  "needs_key_unconfigured",
  "unreadable",
  "unknown",
]);
const connectionKeySourceStates = new Set<ConnectionKeySourceState>([
  "present",
  "absent",
  "unreadable",
  "unknown",
]);
const connectionKeyInUses = new Set<ConnectionKeyInUse>([
  "per_request",
  "startup_env",
  "config_consumed",
  "unknown",
]);
const connectionAuths = new Set<ConnectionAuth>([
  "bearer",
  "loopback_no_key",
  "unknown",
]);

function requiredString(value: unknown, name: string): string {
  if (typeof value !== "string")
    throw new ContractError(`${name} must be a string`);
  return value;
}
function requiredText(value: unknown, name: string): string {
  if (typeof value !== "string" || !value.trim())
    throw new ContractError(`${name} is missing`);
  return value;
}
/** Missing keys are contract errors; an explicit null stays null. */
function nullableText(value: unknown, name: string): string | null {
  if (value === undefined) throw new ContractError(`${name} is missing`);
  if (value === null) return null;
  if (typeof value !== "string")
    throw new ContractError(`${name} must be a string or null`);
  return value;
}
function nullableCount(value: unknown, name: string): number | null {
  if (value === undefined) throw new ContractError(`${name} is missing`);
  if (value === null) return null;
  const result = count(value);
  if (result === null)
    throw new ContractError(`${name} must be a nonnegative integer or null`);
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
  if (typeof value !== "boolean")
    throw new ContractError(`${name} must be a boolean or null`);
  return value;
}
function nullablePort(value: unknown, name: string): number | null {
  const port = nullableCount(value, name);
  if (port !== null && (port < 1 || port > 65535))
    throw new ContractError(`${name} is out of range`);
  return port;
}
function requiredStringList(value: unknown, name: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string"))
    throw new ContractError(`${name} must be an array of strings`);
  return [...value] as string[];
}
function closedState<T extends string>(
  value: unknown,
  allowed: ReadonlySet<T>,
  name: string
): T {
  if (typeof value !== "string" || !allowed.has(value as T))
    throw new ContractError(`${name} is not in the contract set`);
  return value as T;
}

/** Gateway connection read face; `expectedService` binds the response identity to
 *  the requested gateway so a late or foreign response cannot be installed. */
export function normalizeGatewayConnection(
  value: unknown,
  expectedService?: string
): GatewayConnection {
  const root = requiredObject(value, "gateway connection");
  const service = requiredText(root.service, "gateway connection service");
  if (expectedService !== undefined && service !== expectedService)
    throw new ContractError(
      "gateway connection response belongs to a different service"
    );
  const keyState = closedState(
    root.key_state,
    connectionKeyStates,
    "gateway connection key state"
  );
  const auth = closedState(
    root.auth,
    connectionAuths,
    "gateway connection auth"
  );
  const keyPlaintext = nullableText(
    root.key_plaintext,
    "gateway connection plaintext key"
  );
  if (keyState === "present") {
    if (!keyPlaintext || !keyPlaintext.trim())
      throw new ContractError(
        "gateway connection key state present requires a plaintext key"
      );
  } else if (keyPlaintext !== null) {
    throw new ContractError(
      "gateway connection plaintext key is only allowed while the key state is present"
    );
  }
  // Cross-field invariants mirrored from the read face. The key mechanism is a
  // separate fact from the key state: a state that carries (or needs) a value must
  // name how the running instance consumes it, while `unknown` never claims a
  // mechanism it could not confirm. `absent` is only produced by the chat2api
  // instance record, which marks the mechanism `startup_env` before the only
  // `key_state = "absent"` assignment (console_gateway_connection.py:596 then
  // :637; file sources never produce `absent`), so any other mechanism there
  // would be an invented pair.
  const keyInUse = closedState(
    root.key_in_use,
    connectionKeyInUses,
    "gateway connection key in use"
  );
  if (keyState === "absent") {
    if (keyInUse !== "startup_env")
      throw new ContractError(
        "gateway connection key state and key mechanism do not agree"
      );
  } else if (keyState !== "unknown" && keyInUse === "unknown") {
    throw new ContractError(
      "gateway connection key state requires a known key mechanism"
    );
  }
  // Auth is a separate axis from the key state: `bearer` says clients must
  // present a key, not that a key was read. The read face leaves auth `unknown`
  // while the instance is unconfirmed and keeps `bearer` for a readable key file
  // whose config consistency check failed (:483 then :489-494), so only the
  // `loopback_no_key` pair — produced solely with `absent` — stays refused.
  if (keyState === "absent") {
    if (auth !== "loopback_no_key")
      throw new ContractError(
        "gateway connection key state and auth do not agree"
      );
  } else if (keyState === "unknown") {
    if (auth !== "bearer" && auth !== "unknown")
      throw new ContractError(
        "gateway connection key state and auth do not agree"
      );
  } else if (auth !== "bearer") {
    throw new ContractError(
      "gateway connection key state and auth do not agree"
    );
  }
  const alternateProtocols: ConnectionProtocolFace[] =
    root.alternate_protocols === undefined || root.alternate_protocols === null
      ? []
      : requiredRows(root.alternate_protocols, "alternate protocols").map(
          (row) => ({
            protocol: requiredText(row.protocol, "alternate protocol"),
            path: requiredText(row.path, "alternate protocol path"),
            note: nullableText(row.note, "alternate protocol note"),
          })
        );
  return {
    service,
    displayName: nullableText(
      root.display_name,
      "gateway connection display name"
    ),
    port: nullablePort(root.port, "gateway connection port"),
    baseUrl: nullableText(root.base_url, "gateway connection base URL"),
    protocol: requiredText(root.protocol, "gateway connection protocol"),
    protocolPath: requiredText(
      root.protocol_path,
      "gateway connection protocol path"
    ),
    alternateProtocols,
    healthPath: requiredText(
      root.health_path,
      "gateway connection health path"
    ),
    healthPathSource: requiredText(
      root.health_path_source,
      "gateway connection health path source"
    ),
    runtimeState: nullableText(
      root.runtime_state,
      "gateway connection runtime state"
    ),
    runtimeHealthy: nullableBoolean(
      root.runtime_healthy,
      "gateway connection runtime healthy"
    ),
    auth,
    keyState,
    keyPlaintext,
    keySource: nullableText(root.key_source, "gateway connection key source"),
    keySourceState: closedState(
      root.key_source_state,
      connectionKeySourceStates,
      "gateway connection key source state"
    ),
    keyInUse,
    reason: requiredString(root.reason, "gateway connection reason"),
    evidence: requiredStringList(root.evidence, "gateway connection evidence"),
    checkedEpoch: requiredCount(
      root.checked_epoch,
      "gateway connection checked time"
    ),
  };
}

// S5 public share supervision read face. Missing optional fields stay null (the
// page writes 未提供); only `available` is mandatory because every response of
// this endpoint carries it. Unknown closed-set values become explicit unknowns,
// never a healthy default, and reason codes pass through unchanged.
const publicOveralls = new Set<PublicShareOverall>([
  "ok",
  "degraded",
  "all_failed",
  "incomplete",
  "not_checked",
  "unknown",
]);
const publicEntryStates = new Set<PublicShareEntryState>([
  "ok",
  "failed",
  "unchecked",
]);
const publicChecks = new Set<PublicShareCheck>(["ok", "failed", "not_checked"]);
const publicRecoveryStates = new Set<PublicShareRecoveryState>([
  "idle",
  "pending",
  "running",
  "succeeded",
  "failed",
  "manual_required",
  "budget_exhausted",
  "stopped",
  "unknown",
]);

function publicOverall(value: unknown): PublicShareOverall {
  return typeof value === "string" &&
    publicOveralls.has(value as PublicShareOverall)
    ? (value as PublicShareOverall)
    : "unknown";
}
function publicEntryState(value: unknown): PublicShareEntryState {
  return typeof value === "string" &&
    publicEntryStates.has(value as PublicShareEntryState)
    ? (value as PublicShareEntryState)
    : "unchecked";
}
function publicCheck(value: unknown): PublicShareCheck {
  return typeof value === "string" &&
    publicChecks.has(value as PublicShareCheck)
    ? (value as PublicShareCheck)
    : "not_checked";
}
function publicRecoveryState(value: unknown): PublicShareRecoveryState {
  return typeof value === "string" &&
    publicRecoveryStates.has(value as PublicShareRecoveryState)
    ? (value as PublicShareRecoveryState)
    : "unknown";
}
const publicDnsSourceStates = new Set<PublicDnsSourceState>([
  "ok",
  "empty",
  "nxdomain",
  "error",
  "timeout",
  "not_checked",
]);
const publicDnsStates = new Set<PublicDnsState>([
  "ok",
  "empty",
  "nxdomain",
  "error",
  "timeout",
  "disagreement",
  "unconfirmed",
  "not_checked",
]);
function publicDnsState(value: unknown): PublicDnsState {
  return typeof value === "string" &&
    publicDnsStates.has(value as PublicDnsState)
    ? (value as PublicDnsState)
    : "not_checked";
}
function publicDnsSourceState(value: unknown): PublicDnsSourceState {
  return typeof value === "string" &&
    publicDnsSourceStates.has(value as PublicDnsSourceState)
    ? (value as PublicDnsSourceState)
    : "not_checked";
}
function publicDnsAddresses(value: unknown): string[] {
  return Array.isArray(value)
    ? value
        .filter((address): address is string => typeof address === "string")
        .slice(0, 4)
    : [];
}
function publicDnsSource(value: unknown): PublicDnsSource | null {
  const row = object(value);
  if (!row) return null;
  const source = text(row.source);
  if (source !== "cloudflare" && source !== "google" && source !== "nextdns")
    return null;
  return {
    source,
    state: publicDnsSourceState(row.state),
    status: count(row.status),
    classification: text(row.classification),
    addresses: publicDnsAddresses(row.addresses),
    addressesTruncated: boolean(row.addresses_truncated),
    ttlSeconds: count(row.ttl_s),
    negativeTtlSeconds: count(row.negative_ttl_s),
    checkedAt: nonnegative(row.checked_at_epoch),
    elapsedMs: count(row.elapsed_ms),
  };
}
/** Missing DNS fields stay null (未提供); an invalid closed-set state is
 *  explicit not_checked, never a healthy default. Raw DoH bodies are never kept. */
function publicDns(value: unknown): PublicDnsDetail | null {
  const row = object(value);
  if (!row) return null;
  return {
    state: publicDnsState(row.state),
    reason: text(row.reason),
    addresses: publicDnsAddresses(row.addresses),
    addressesTruncated: boolean(row.addresses_truncated),
    ttlSeconds: count(row.ttl_s),
    negativeTtlSeconds: count(row.negative_ttl_s),
    checkedAt: nonnegative(row.checked_at_epoch),
    sources: Array.isArray(row.sources)
      ? row.sources
          .flatMap((item) => {
            const source = publicDnsSource(item);
            return source ? [source] : [];
          })
          .slice(0, 2)
      : [],
  };
}

export function normalizePublicDnsDetail(
  value: unknown
): PublicDnsDetail | null {
  return publicDns(value);
}

function publicEntry(value: unknown): PublicShareEntry | null {
  const row = object(value);
  if (!row) return null;
  return {
    host: text(row.host),
    ip: text(row.ip),
    state: publicEntryState(row.state),
    dns: publicCheck(row.dns),
    tls: publicCheck(row.tls),
    certificate: publicCheck(row.certificate),
    reason: text(row.reason),
    elapsedMs: count(row.elapsed_ms),
    checkedAt: nonnegative(row.checked_at_epoch),
  };
}
function publicEvent(value: unknown): PublicShareEvent | null {
  const row = object(value);
  if (!row) return null;
  return {
    at: text(row.at_utc) ?? text(row.at),
    status: text(row.status),
    code: text(row.code),
    action: text(row.action),
  };
}
function publicBudget(value: unknown): PublicShareBudget | null {
  const row = object(value);
  if (!row) return null;
  return {
    windowSeconds: nonnegative(row.window_s),
    mappingUsed: count(row.mapping_used),
    mappingLimit: count(row.mapping_limit),
    tsRestartUsed: count(row.ts_restart_used),
    tsRestartLimit: count(row.ts_restart_limit),
    mappingNextEligibleAt: text(row.mapping_next_eligible_at_utc),
    tsRestartNextEligibleAt: text(row.ts_restart_next_eligible_at_utc),
    unreadable: boolean(row.unreadable),
    reason: text(row.reason),
  };
}
function publicRecovery(value: unknown): PublicShareRecovery | null {
  const row = object(value);
  if (!row) return null;
  return {
    state: publicRecoveryState(row.state),
    action: text(row.action),
    stage: text(row.stage),
    reason: text(row.reason),
    attemptedAt: text(row.attempted_at_utc),
    finishedAt: text(row.finished_at_utc),
    manual: boolean(row.manual),
  };
}
function publicIntent(value: unknown): PublicShareIntent | null {
  const row = object(value);
  if (!row) return null;
  const state = row.state;
  return {
    state: state === "running" || state === "stopped" ? state : "unknown",
    at: text(row.at_utc),
  };
}
function publicRound(value: unknown): PublicShareRound | null {
  const row = object(value);
  if (!row) return null;
  return {
    nextCheckAt: text(row.next_check_at_utc),
    nextActionAt: text(row.next_action_at_utc),
  };
}
function publicCancel(
  value: unknown
): { cancelled: boolean | null; reason: string | null } | null {
  const row = object(value);
  if (!row) return null;
  return { cancelled: boolean(row.cancelled), reason: text(row.reason) };
}
function publicSnapshot(value: unknown): PublicShareSnapshot | null {
  const row = object(value);
  if (!row) return null;
  const entries = Array.isArray(row.entries)
    ? row.entries.flatMap((item) => {
        const entry = publicEntry(item);
        return entry ? [entry] : [];
      })
    : [];
  return {
    observedAt: nonnegative(row.observed_at_epoch),
    overall: publicOverall(row.overall),
    reason: text(row.reason),
    publicUrl: text(row.public_url),
    host: text(row.host),
    entries,
    dns: publicDns(row.dns),
    recovery: publicRecovery(row.recovery),
    budget: publicBudget(row.budget),
    intent: publicIntent(row.intent),
    round: publicRound(row.round),
    local: object(row.local),
  };
}

export function normalizePublicShareStatus(value: unknown): PublicShareStatus {
  const root = requiredObject(value, "public share status");
  const available = boolean(root.available);
  if (available === null)
    throw new ContractError("public share status must name availability");
  const events =
    root.events === undefined || root.events === null
      ? null
      : Array.isArray(root.events)
        ? root.events.flatMap((item) => {
            const event = publicEvent(item);
            return event ? [event] : [];
          })
        : (() => {
            throw new ContractError(
              "public share events must be an array or null"
            );
          })();
  return {
    available,
    version: count(root.version),
    checkedAt: nonnegative(root.checked_at),
    snapshot: publicSnapshot(root.snapshot),
    events,
    eventsTruncated: boolean(root.events_truncated),
    eventsUnreadable: boolean(root.events_unreadable),
    budget: publicBudget(root.budget),
    cancel: publicCancel(root.cancel),
    // A missing damage flag is never read as "the file is readable".
    unreadable: boolean(root.unreadable) ?? true,
    reason: text(root.reason),
    stateFile: text(root.state_file),
  };
}

export function normalizePublicShareEvents(
  value: unknown
): PublicShareEventsPage {
  const root = requiredObject(value, "public share events");
  const available = boolean(root.available);
  if (available === null)
    throw new ContractError("public share events must name availability");
  const events =
    root.events === undefined || root.events === null
      ? null
      : Array.isArray(root.events)
        ? root.events.flatMap((item) => {
            const event = publicEvent(item);
            return event ? [event] : [];
          })
        : (() => {
            throw new ContractError(
              "public share event rows must be an array or null"
            );
          })();
  return {
    available,
    events,
    nextBefore: nonnegative(root.next_before),
    truncated: boolean(root.truncated),
    reason: text(root.reason),
  };
}

export function normalizePublicShareExport(value: unknown): PublicShareExport {
  const root = requiredObject(value, "public share export");
  const events =
    root.events === undefined || root.events === null
      ? null
      : Array.isArray(root.events)
        ? root.events.flatMap((item) => {
            const event = publicEvent(item);
            return event ? [event] : [];
          })
        : (() => {
            throw new ContractError(
              "public share export events must be an array or null"
            );
          })();
  return {
    exportVersion: count(root.export_version),
    generatedAt: text(root.generated_at),
    redaction: object(root.redaction),
    snapshot: publicSnapshot(root.snapshot),
    events,
    budget: publicBudget(root.budget),
    cancel: publicCancel(root.cancel),
    limits: object(root.limits),
  };
}

// ---------------------------------------------------------------- UM read face (B5/B6)
//
// The backend's read surface already reports its own honesty kinds; these
// normalizers preserve them and never turn a missing figure into 0. A response
// that cannot be shaped is a contract error (the caller keeps the last good data
// as stale); a readable target with no rows is a legal empty list.

export class UmReadStatusError extends Error {
  readonly status: string;
  constructor(status: string, message: string) {
    super(message);
    this.status = status;
    this.name = "UmReadStatusError";
  }
}

const UM_READING_KINDS = new Set<UmReadingKind>([
  "exact",
  "atLeast",
  "estimate",
  "unreported",
  "unknown",
  "missing",
]);

/** One backend reading; a missing/unparsable figure stays explicit, never 0. */
export function normalizeUmReading(value: unknown, name = "读数"): UmReading {
  const row = object(value);
  if (!row) return { kind: "unknown", note: `${name} 缺失或形状无法解析` };
  const kind = text(row.kind);
  if (!kind || !UM_READING_KINDS.has(kind as UmReadingKind))
    return { kind: "unknown", note: `${name} 的报告状态无法解析` };
  const note = text(row.note) || undefined;
  if (kind === "exact" || kind === "atLeast" || kind === "estimate") {
    const number = nonnegative(row.value);
    if (number === null)
      return { kind: "unknown", note: `${name} 缺少可用数值` };
    if (kind === "exact")
      return note
        ? { kind: "exact", value: number, note }
        : { kind: "exact", value: number };
    if (kind === "atLeast")
      return note
        ? { kind: "atLeast", value: number, note }
        : { kind: "atLeast", value: number };
    return note
      ? { kind: "estimate", value: number, note }
      : { kind: "estimate", value: number };
  }
  if (kind === "unreported")
    return note ? { kind: "unreported", note } : { kind: "unreported" };
  if (kind === "unknown")
    return note ? { kind: "unknown", note } : { kind: "unknown" };
  return note ? { kind: "missing", note } : { kind: "missing" };
}

function umTextList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}
/** The backend reports seconds; every console instant is milliseconds. */
function epochMs(value: unknown): number | null {
  const seconds = nonnegative(value);
  return seconds === null ? null : Math.round(seconds * 1000);
}
function umBucket(value: unknown, name: string): UmBucketPayload {
  const row = object(value);
  if (!row) throw new ContractError(`${name} must be an object`);
  return {
    tokens: normalizeUmReading(row.tokens, `${name}.tokens`),
    requests: normalizeUmReading(row.requests, `${name}.requests`),
    speed: normalizeUmReading(row.speed, `${name}.speed`),
  };
}
export function normalizeUmTodayResponse(value: unknown): UmTodayResponse {
  const root = requiredObject(value, "UM today response");
  const today = requiredObject(root.today, "UM today");
  const tokens = object(today.tokens) ?? {};
  const cacheRead = object(today.cacheRead) ?? {};
  const requests = object(today.requests) ?? {};
  const speed = object(today.speed) ?? {};
  const report = cacheRead.report;
  return {
    today: {
      day: requiredText(today.day, "UM today day"),
      tokens: {
        total: normalizeUmReading(tokens.total, "Token 合计"),
        input: normalizeUmReading(tokens.input, "输入 Token"),
        output: normalizeUmReading(tokens.output, "输出 Token"),
      },
      cacheRead: {
        tokens: normalizeUmReading(cacheRead.tokens, "缓存读取"),
        report:
          report === "reported" ||
          report === "partial" ||
          report === "unreported"
            ? report
            : "unknown",
        detail: text(cacheRead.detail) ?? "",
      },
      reasoning: normalizeUmReading(today.reasoning, "思考 Token"),
      requests: {
        recorded: normalizeUmReading(requests.recorded, "已记录请求"),
        failed: normalizeUmReading(requests.failed, "失败请求"),
      },
      speed: {
        value: normalizeUmReading(speed.value, "速度"),
        basis: speed.basis === "generation" ? "generation" : "whole_request",
        samples: count(speed.samples),
      },
    },
    coverage: object(root.coverage) ?? {},
    timezone: text(root.timezone),
    checkedAt: epochMs(root.checked_epoch),
  };
}
export function normalizeUmTrendResponse(value: unknown): UmTrendResponse {
  const root = requiredObject(value, "UM trend response");
  const trend = requiredObject(root.trend, "UM trend");
  const range = trend.range;
  if (range !== "7d" && range !== "14d" && range !== "30d")
    throw new ContractError("UM trend range is not one of 7d/14d/30d");
  if (
    !Array.isArray(trend.days) ||
    trend.days.some((day) => typeof day !== "string")
  )
    throw new ContractError("UM trend days must be a string array");
  const days = [...trend.days] as string[];
  const all = requiredObject(trend.all, "UM trend all");
  if (!Array.isArray(all.daily) || all.daily.length !== days.length)
    throw new ContractError("UM trend all.daily must align with days");
  const models: UmTrendModelPayload[] = [];
  for (const item of Array.isArray(trend.models) ? trend.models : []) {
    const row = requiredObject(item, "UM trend model");
    if (!text(row.model))
      throw new ContractError("UM trend model id is missing");
    if (!Array.isArray(row.daily) || row.daily.length !== days.length)
      throw new ContractError("UM trend model daily must align with days");
    models.push({
      model: row.model as string,
      daily: row.daily.map((bucket, index) =>
        umBucket(bucket, `UM trend ${row.model as string} day ${index}`)
      ),
      total: umBucket(row.total, `UM trend ${row.model as string} total`),
      failed: normalizeUmReading(
        row.failed,
        `UM trend ${row.model as string} failed`
      ),
    });
  }
  // A missing/unavailable ledger yields a full window of `missing` readings;
  // rendering that as a real trend would claim the window was measured. The
  // backend names the state in coverage, so the section stays failed instead.
  const coverage = object(root.coverage) ?? {};
  const status = text(coverage.status) ?? "ok";
  if (status === "missing" || status === "unavailable" || status === "error")
    throw new UmReadStatusError(
      status,
      text(coverage.note) ?? text(coverage.error) ?? "UM 趋势账本读取失败"
    );
  return {
    trend: {
      range: range as UmTrendRange,
      days,
      all: {
        daily: all.daily.map((bucket, index) =>
          umBucket(bucket, `UM trend all day ${index}`)
        ),
        total: umBucket(all.total, "UM trend all total"),
      },
      models,
      speedBasis:
        trend.speedBasis === "generation" ? "generation" : "whole_request",
    },
    coverage: object(root.coverage) ?? {},
    timezone: text(root.timezone),
    checkedAt: epochMs(root.checked_epoch),
  };
}
function umRetries(value: unknown): { at: number; note: string }[] | null {
  if (value === null) return null;
  if (!Array.isArray(value)) return null;
  return value.flatMap((item) => {
    const row = object(item);
    const at = count(row?.at);
    if (!row || at === null) return [];
    return [{ at, note: text(row.note) ?? "" }];
  });
}
function umRequestRow(value: unknown): UmRequestRowPayload {
  const row = requiredObject(value, "UM request row");
  const requestId = text(row.requestId);
  const result = row.result;
  const error = object(row.error);
  return {
    key: requiredText(row.key, "UM request key"),
    requestId: requestId && UM_REQUEST_ID_RE.test(requestId) ? requestId : null,
    at: requiredCount(row.at, "UM request time"),
    model: requiredText(row.model, "UM request model"),
    result:
      result === "success" ||
      result === "failed" ||
      result === "truncated" ||
      result === "client_disconnected" ||
      result === "cancelled"
        ? result
        : "unknown",
    resultNote: text(row.resultNote) ?? "",
    httpStatus: count(row.httpStatus),
    input: normalizeUmReading(row.input, "输入 Token"),
    output: normalizeUmReading(row.output, "输出 Token"),
    cacheRead: normalizeUmReading(row.cacheRead, "缓存读取"),
    reasoning: normalizeUmReading(row.reasoning, "思考 Token"),
    durationMs: normalizeUmReading(row.durationMs, "耗时"),
    firstTokenMs: normalizeUmReading(row.firstTokenMs, "首字耗时"),
    speed: normalizeUmReading(row.speed, "速度"),
    speedBasis:
      row.speedBasis === "generation" ? "generation" : "whole_request",
    retries: umRetries(row.retries),
    error: error
      ? {
          code: text(error.code) ?? "unknown",
          message: text(error.message) ?? "",
        }
      : null,
  };
}
const UM_REQUEST_ID_RE = /^[0-9a-f]{32}$/;
export function normalizeUmRequestsResponse(
  value: unknown
): UmRequestsResponse {
  const root = requiredObject(value, "UM requests response");
  const page = requiredObject(root.page, "UM requests page");
  if (!Array.isArray(page.rows))
    throw new ContractError("UM request rows must be an array");
  // A missing/unavailable ledger is not a legal empty list: an empty page would
  // read as 「这段时间没有请求」. The backend names that state in coverage;
  // only a complete or explicitly partial ledger may be shown as rows.
  const coverage = object(page.coverage) ?? object(root.coverage) ?? {};
  const status = text(coverage.status) ?? "ok";
  if (status === "missing" || status === "unavailable" || status === "error")
    throw new UmReadStatusError(
      status,
      text(coverage.note) ?? text(coverage.error) ?? "UM 请求账本读取失败"
    );
  const scope = text(page.scope) ?? "";
  return {
    page: {
      rows: page.rows.map(umRequestRow),
      scope:
        status === "partial"
          ? `${scope}（账本不完整：${text(coverage.note) ?? "可解析部分照常保留"}）`
          : scope,
      earlier: text(page.earlier),
    },
    models: umTextList(root.models),
    coverage: object(root.coverage) ?? {},
    timezone: text(root.timezone),
    checkedAt: epochMs(root.checked_epoch),
  };
}
export function normalizeUmLogLine(value: unknown): UmLogLinePayload {
  const row = requiredObject(value, "UM log line");
  const level = row.level;
  const requestId = text(row.requestId);
  return {
    key: requiredText(row.key, "UM log key"),
    at: count(row.at),
    level:
      level === "error" ||
      level === "warn" ||
      level === "info" ||
      level === "debug"
        ? level
        : "unknown",
    source: text(row.source) ?? "unknown",
    text: text(row.text) ?? "",
    requestId: requestId && UM_REQUEST_ID_RE.test(requestId) ? requestId : null,
  };
}
function umLogLines(value: unknown): UmLogLinePayload[] {
  if (!Array.isArray(value))
    throw new ContractError("UM log lines must be an array");
  return value.map(normalizeUmLogLine);
}
function umLogStatus(row: JsonObject): string {
  const coverage = object(row.coverage) ?? {};
  return text(coverage.status) ?? text(row.status) ?? "error";
}
export function normalizeUmLogPage(value: unknown): UmLogPagePayload {
  const root = requiredObject(value, "UM log page");
  const status = umLogStatus(root);
  if (status !== "ok")
    throw new UmReadStatusError(
      status,
      text(root.error) ??
        text((object(root.coverage) ?? {}).note) ??
        "日志读取失败"
    );
  return {
    lines: umLogLines(root.lines),
    sources: umTextList(root.sources),
    scope: text(root.scope) ?? "",
    earlier: text(root.earlier),
    coverage: object(root.coverage) ?? {},
  };
}
export function normalizeUmRequestLogs(value: unknown): UmRequestLogsPayload {
  const root = requiredObject(value, "UM request logs");
  const coverage = object(root.coverage) ?? {};
  const status = umLogStatus(root);
  if (status !== "ok" && status !== "partial")
    throw new UmReadStatusError(
      status,
      text(coverage.note) ?? "关联日志读取失败"
    );
  return {
    lines: umLogLines(root.lines),
    scope: text(root.scope) ?? "",
    partial: status === "partial",
    coverage,
  };
}
export function normalizeUmMaintenancePage(
  value: unknown
): UmMaintenancePagePayload {
  const root = requiredObject(value, "UM maintenance page");
  const coverage = object(root.coverage) ?? {};
  const status = text(coverage.status) ?? "error";
  if (status !== "ok")
    throw new UmReadStatusError(
      status,
      text(coverage.error) ?? text(coverage.note) ?? "维护记录读取失败"
    );
  if (!Array.isArray(root.rows))
    throw new ContractError("UM maintenance rows must be an array");
  const rows: UmMaintenanceRowPayload[] = [];
  let undated = 0;
  for (const item of root.rows) {
    const row = requiredObject(item, "UM maintenance row");
    const at = count(row.at);
    if (at === null) {
      undated += 1;
      continue;
    }
    const state = row.state;
    rows.push({
      key: requiredText(row.key, "UM maintenance key"),
      action: requiredText(row.action, "UM maintenance action"),
      state:
        state === "queued" ||
        state === "running" ||
        state === "succeeded" ||
        state === "failed" ||
        state === "cancelled" ||
        state === "interrupted"
          ? state
          : "interrupted",
      at,
      finishedAt: count(row.finishedAt),
      steps: (Array.isArray(row.steps) ? row.steps : []).flatMap((step) => {
        const line = object(step);
        const stepAt = count(line?.at);
        return line && stepAt !== null
          ? [{ at: stepAt, note: text(line.note) ?? "" }]
          : [];
      }),
      result: text(row.result) ?? "",
    });
  }
  const baseScope = text(root.scope) ?? "";
  return {
    rows,
    scope: undated
      ? `${baseScope}（${undated} 条记录缺少开始时间，未列出）`
      : baseScope,
    earlier: text(root.earlier),
    coverage,
  };
}
export function normalizeUmCatalog(
  value: unknown,
  source = "UM /v1/models（只读目录）"
): UmCatalogPayload {
  const root = requiredObject(value, "UM catalog");
  const catalogs = object(root.catalogs) ?? {};
  if (catalogs.um !== undefined && !Array.isArray(catalogs.um))
    throw new ContractError("UM catalog models must be an array");
  const models: UmCatalogModelPayload[] = [];
  for (const item of Array.isArray(catalogs.um) ? catalogs.um : []) {
    const row = requiredObject(item, "UM catalog model");
    models.push({
      id: requiredText(row.id, "UM catalog model id"),
      displayName: text(row.display_name),
      declaredContext: count(row.context),
      efforts: umTextList(row.efforts),
      effortsSource: text(row.efforts_source),
      defaultEffort: text(row.default_effort),
    });
  }
  return {
    models,
    checkedAt: epochMs(root.checked_epoch),
    source,
    warnings: umTextList(root.warnings),
  };
}
export function normalizeUmServices(value: unknown): UmServicesPayload {
  const root = requiredObject(value, "UM services response");
  if (boolean(root.ok) === false)
    throw new ContractError("UM services response reports failure");
  if (!Array.isArray(root.services))
    throw new ContractError("UM services must be an array");
  const meta = object(root.meta) ?? {};
  const services: UmServiceEntry[] = root.services.map((item) => {
    const row = requiredObject(item, "UM service");
    return {
      key: requiredText(row.key, "UM service key"),
      displayName: text(row.display_name),
      port: count(row.port),
      state: requiredText(row.state, "UM service state"),
      healthy: boolean(row.healthy),
      detail: text(row.detail) ?? "",
      processIds: (Array.isArray(row.process_ids)
        ? row.process_ids
        : []
      ).flatMap((id) => (count(id) === null ? [] : [count(id) as number])),
    };
  });
  return {
    services,
    checkedAt: epochMs(meta.generated_epoch),
    cached: meta.cached === true,
    cacheAgeSeconds: count(meta.cache_age_seconds),
  };
}
export function normalizeUmOperations(value: unknown): UmOperationsPayload {
  const root = requiredObject(value, "UM operations response");
  if (!Array.isArray(root.operations))
    throw new ContractError("UM operations must be an array");
  const operations: UmOperationPayload[] = root.operations.map((item) => {
    const row = requiredObject(item, "UM operation");
    const state = row.state;
    if (
      state !== "queued" &&
      state !== "running" &&
      state !== "succeeded" &&
      state !== "failed" &&
      state !== "cancelled" &&
      state !== "interrupted"
    )
      throw new ContractError("UM operation state is not in the contract set");
    return {
      id: requiredText(row.id, "UM operation id"),
      action: requiredText(row.action, "UM operation action"),
      target: text(row.target) ?? "",
      state,
      result: object(row.result),
      error: object(row.error),
      createdAt: epochMs(row.created_at),
      finishedAt: epochMs(row.finished_at),
      steps: (Array.isArray(row.steps) ? row.steps : []).flatMap((step) => {
        const line = object(step);
        const at = epochMs(line?.at);
        return line && at !== null ? [{ at, note: text(line.note) ?? "" }] : [];
      }),
    };
  });
  return { operations };
}
export function normalizeUmSummary(value: unknown): UmSummaryPayload {
  const root = requiredObject(value, "UM summary response");
  const assembly = object(root.assembly) ?? {};
  return {
    role: text(root.role) ?? text(assembly.role),
  };
}

function upstreamFault(value: unknown): UmUpstreamFaultPayload {
  const row = requiredObject(value, "UM upstream fault");
  const startedAt = epochMs(row.started_epoch);
  const lastAt = epochMs(row.last_epoch);
  if (startedAt === null || lastAt === null)
    throw new ContractError("UM upstream fault must carry its start and last instants");
  return {
    id: requiredText(row.id, "UM upstream fault id"),
    classification: requiredText(row.classification, "UM upstream fault classification"),
    upstreamStatus: count(row.upstream_status),
    summary: text(row.summary),
    failures: count(row.failures) ?? 0,
    startedAt,
    lastAt,
    source: requiredText(row.source, "UM upstream fault source"),
  };
}

function upstreamRecovery(value: unknown): UmUpstreamRecoveryPayload {
  const row = requiredObject(value, "UM upstream recovery");
  const recoveredAt = epochMs(row.recovered_epoch);
  if (recoveredAt === null)
    throw new ContractError("UM upstream recovery must carry its instant");
  return {
    ...upstreamFault(row),
    recoveredAt,
    recoverySource: requiredText(row.recovery_source, "UM upstream recovery source"),
    recoveryStatus: count(row.recovery_status),
    recoveryDetail: text(row.recovery_detail),
  };
}

export function normalizeUmUpstreamFaults(value: unknown): UmUpstreamFaultsPayload {
  const root = requiredObject(value, "UM upstream faults response");
  if (root.status !== "ok")
    throw new ContractError("UM upstream faults response reports failure");
  const history = Array.isArray(root.history)
    ? root.history.map(upstreamRecovery)
    : (() => {
        throw new ContractError("UM upstream fault history must be an array");
      })();
  let lastCheck: UmUpstreamFaultsPayload["lastCheck"] = null;
  if (root.last_check !== null && root.last_check !== undefined) {
    const row = requiredObject(root.last_check, "UM upstream last check");
    const at = epochMs(row.epoch);
    if (at === null)
      throw new ContractError("UM upstream last check must carry its instant");
    lastCheck = {
      at,
      source: requiredText(row.source, "UM upstream last check source"),
      result: requiredText(row.result, "UM upstream last check result"),
      upstreamStatus: count(row.upstream_status),
    };
  }
  return {
    active:
      root.active === null || root.active === undefined
        ? null
        : upstreamFault(root.active),
    history,
    lastCheck,
    nextCheckAt: epochMs(root.next_check_epoch),
    source: requiredText(root.source, "UM upstream fault source"),
  };
}
