/** Backend contracts only. This module has no DOM, renderer, or live-data side effects. */
export type JsonObject = Record<string, unknown>;
export type Intent = "read" | "draft" | "client-write" | "probe";
export type OperationState = "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted";

export interface ActionCapability {
  action: string;
  enabled: boolean;
  cancelable: boolean;
  disabledReason: string | null;
  description: string | null;
}

export interface Operation {
  id: string;
  action: string;
  target: string;
  state: OperationState;
  result: JsonObject | null;
  error: JsonObject | null;
  cancelRequested: boolean;
  createdAt: string | null;
  finishedAt: string | null;
}

export interface TransportRequest {
  method: "GET" | "POST";
  /** A console-relative path, e.g. /actions; never an absolute URL. */
  path: string;
  body?: JsonObject;
  access: "session" | "public";
  signal?: AbortSignal;
}

export interface TransportResponse {
  status: number;
  body: unknown;
  parseError?: "invalid_json";
}

export interface Transport {
  request(request: TransportRequest): Promise<TransportResponse>;
}

export interface OperationResult {
  state: "succeeded" | "failed" | "cancelled" | "interrupted" | "refused" | "unconfirmed";
  operation: Operation | null;
  /** Retained for read-only reconciliation; an uncertain POST is never retried. */
  operationId: string | null;
  reason: string | null;
}

export interface ModelSettings {
  alias: string | null;
  contextSource: "declared" | "manual" | "measured" | null;
  contextTokens: number | null;
  defaultEffort: string | null;
}

export interface SettingsPreview {
  selection: Readonly<Record<string, number>>;
  previewToken: string;
  changes: JsonObject[];
  /** Preserve the actual backend plan rather than constructing UI estimates. */
  plan: unknown;
  warnings: string[];
  status: string | null;
}

export interface SettingsApply {
  applied: string[];
  deferred: string[];
  skipped: unknown[];
  status: string | null;
  warnings: string[];
  pendingDetails: JsonObject[];
  note: string | null;
}

export interface OutputCap {
  value: number | null;
  source: string | null;
}

export interface CatalogModel {
  key: string;
  source: string;
  modelId: string;
  displayName: string | null;
  declaredContext: number | null;
  declaredState: string | null;
  outputCap: OutputCap;
  supportedEfforts: string[];
  effortsSource: string | null;
  defaultEffort: string | null;
  published: boolean | null;
}

export interface Catalog {
  models: CatalogModel[];
  warnings: string[];
  checkedEpoch: number | null;
}

export interface ContextView {
  key: string;
  source: string;
  modelId: string;
  declaredTokens: number | null;
  declaredAt: string | null;
  appliedContextTokens: number | null;
  effectiveContextTokens: number | null;
  effectiveSource: string | null;
  draft: ModelSettings | null;
  draftVersion: number | null;
  applied: ModelSettings | null;
  appliedVersion: number | null;
  appliedAt: string | null;
  held: boolean | null;
  clientContextTokens: number | null;
  /** Which consumer the single top-level readback came from (additive; null when
   *  the read face did not name one). */
  clientReadbackConsumer: string | null;
  clientReadbackState: "ok" | "absent" | "unavailable" | "unknown";
  clientMatchesApplied: boolean | null;
  /** Newer backend readback fields remain optional until actually present. */
  clientOutputCap: OutputCap;
  clientAlias: string | null;
  clientDefaultEffort: string | null;
  clientReadbacks: ClientReadback[];
}

export interface ClientReadback {
  consumer: string;
  state: "ok" | "absent" | "unavailable" | "unknown";
  contextTokens: number | null;
  alias: string | null;
  defaultEffort: string | null;
  outputCap: OutputCap;
  matchesApplied: { context: boolean | null; alias: boolean | null; defaultEffort: boolean | null };
}

export interface ContextHistory {
  evidenceRevision: number | null;
  verifiedTokenFloor: number | null;
  observedCharacterFloor: number | null;
  observedCharacterCeiling: number | null;
  status: string | null;
  lastRun: string | null;
  lastSuccess: string | null;
  staleReason: string | null;
  archivedAt: string | null;
}

export interface ContextEvidence {
  key: string;
  source: string;
  modelId: string;
  declaredTokens: number | null;
  verifiedTokenFloor: number | null;
  observedCharacterFloor: number | null;
  observedCharacterCeiling: number | null;
  status: string | null;
  evidenceRevision: number | null;
  schemeRevision: number | null;
  lastSuccess: string | null;
  lastRun: string | null;
  staleReason: string | null;
  historyCount: number | null;
  history: ContextHistory[];
}

export interface ModelDetail {
  key: string;
  catalog: CatalogModel | null;
  context: ContextView | null;
  evidence: ContextEvidence | null;
}

export type SyncScope =
  | { mode: "all" }
  | { mode: "selected"; sources?: readonly string[]; consumers?: readonly string[]; models?: Readonly<Record<string, readonly string[]>> };

export interface LogEvent {
  timestamp: string | null;
  level: string | null;
  model: string | null;
  requestId: string | null;
  phase: string | null;
  outcome: string | null;
  errorCode: string | null;
  durationMs: number | null;
  tokenRate: number | null;
  tokenRateKind: string | null;
  rawMessage: string | null;
  parsed: boolean | null;
  cut: boolean | null;
}

export interface LogPage {
  status: string;
  events: LogEvent[];
  nextCursor: string | null;
  resetHint: boolean;
  truncated: boolean | null;
  scannedBytes: number | null;
  error: string | null;
  requestId: string | null;
  requestScope: "read_window" | null;
}

export interface CostCoverage {
  requests: number | null;
  unknownRequests: number | null;
  unpricedRequests: number | null;
  incompleteRequests: number | null;
  records: number | null;
  pricedRequests: number | null;
  unknownTimeRequests: number | null;
  pricingUnavailableRequests: number | null;
  costComplete: boolean | null;
  /** False for legacy details that do not identify local unknown coverage. */
  localCoverageAvailable: boolean;
}

export interface TokenUsage {
  totalTokens: number | null;
  inputTokens: number | null;
  outputTokens: number | null;
  lowerBoundTokens: number | null;
  lowerBoundInputTokens: number | null;
  lowerBoundOutputTokens: number | null;
  estimatedTokens: number | null;
  estimatedInputTokens: number | null;
  estimatedOutputTokens: number | null;
  completeRequests: number | null;
  lowerBoundRequests: number | null;
  estimatedRequests: number | null;
  unknownRequests: number | null;
  complete: boolean | null;
}

export interface CostDetail {
  identity: string;
  knownCostUsd: number | null;
  estimatedCostUsd: number | null;
  inputTokens: number | null;
  outputTokens: number | null;
  coverage: CostCoverage;
  tokenUsage: TokenUsage;
  priceVersions: string[];
}

export interface KeyUsage {
  keyId: string;
  keyName: string | null;
  deleted: boolean | null;
  knownCostUsd: number | null;
  estimatedCostUsd: number | null;
  inputTokens: number | null;
  outputTokens: number | null;
  coverage: CostCoverage;
  tokenUsage: TokenUsage;
  priceVersions: string[];
  models: CostDetail[];
  days: CostDetail[];
  undated: Omit<CostDetail, "identity"> | null;
}

/** 未归属桶（Rev19）：账本里没有可归因 `key_id` 的记录，形状与每 Key 行
 *  相同，但没有 Key 身份。`keyId` 恒为 null——它永远单列，绝不摊到任何
 *  Key；`tokenUsage` 与 `models`/`days` 用同一套归一化规则（后端已交付），
 *  缺席的字段如实为 null / 空，不折成完整口径。 */
export interface ShareUsageBucket {
  keyId: null;
  knownCostUsd: number | null;
  estimatedCostUsd: number | null;
  inputTokens: number | null;
  outputTokens: number | null;
  coverage: CostCoverage;
  tokenUsage: TokenUsage;
  priceVersions: string[];
  models: CostDetail[];
  days: CostDetail[];
}

export interface ShareUsage {
  keys: KeyUsage[];
  unattributed: ShareUsageBucket;
  coverage: JsonObject;
  checkedAt: string | null;
  requests: number | null;
}

export interface ReadResult<T> {
  state: "ready" | "unavailable" | "refused" | "error";
  value: T | null;
  httpStatus: number | null;
  reason: string | null;
}

/** Rev15 (2026-10-06): read-only connection info for one selected gateway.
 *
 *  Mirrors the Python read face (`console_gateway_connection.py`) exactly. The
 *  read state of the call key is a closed set; `unknown` is an honest state of
 *  its own and is never coerced into a "no key" state. A plaintext key exists
 *  only while the read state is `present`; every other state keeps it null. */
export type ConnectionKeyState =
  | "present"              // authority file/env readable and non-empty
  | "absent"               // chat2api loopback instance: no key and auth off
  | "needs_key_unconfigured" // gateway needs a key but the source is missing/empty
  | "unreadable"           // source exists but cannot be read
  | "unknown";            // running instance unconfirmed; never downgraded

export type ConnectionKeySourceState = "present" | "absent" | "unreadable" | "unknown";

/** How the running instance consumes its key. Unknown stays unknown; a saved
 *  configuration value never proves that a running instance uses it. */
export type ConnectionKeyInUse =
  | "per_request"
  | "startup_env"
  | "config_consumed"
  | "unknown";

export type ConnectionAuth =
  | "bearer"               // clients must present the key
  | "loopback_no_key"       // loopback instance with inbound auth off
  | "unknown";

/** An alternate protocol face of the same gateway; not used by existing consumers. */
export interface ConnectionProtocolFace {
  protocol: string;
  path: string;
  note: string | null;
}

export interface GatewayConnection {
  /** Stable service ID from the runtime snapshot; never a display name. */
  service: string;
  displayName: string | null;
  port: number | null;
  /** Loopback address + protocol path, built only from the snapshot port. */
  baseUrl: string | null;
  protocol: string;
  protocolPath: string;
  alternateProtocols: ConnectionProtocolFace[];
  /** Descriptor constant: the runtime snapshot does not carry a health path. */
  healthPath: string;
  healthPathSource: string;
  runtimeState: string | null;
  runtimeHealthy: boolean | null;
  auth: ConnectionAuth;
  keyState: ConnectionKeyState;
  /** Plaintext call key, present only while keyState === "present"; in-memory only. */
  keyPlaintext: string | null;
  /** Credential source (path/env name) for the user to verify; never the value. */
  keySource: string | null;
  keySourceState: ConnectionKeySourceState;
  /** How the shown key value is consumed; mirrors the Python read face and
   *  keeps "file value" apart from "value the instance confirmed in use".
   *  A known `keyState` always carries a known mechanism, while `unknown`
   *  keeps both `unknown` and a mechanism the file source confirmed. */
  keyInUse: ConnectionKeyInUse;
  reason: string;
  evidence: string[];
  checkedEpoch: number;
}
