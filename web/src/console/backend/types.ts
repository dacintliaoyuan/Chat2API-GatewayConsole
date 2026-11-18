/** Backend contracts only. This module has no DOM, renderer, or live-data side effects. */
export type JsonObject = Record<string, unknown>;
export type Intent = "read" | "draft" | "client-write" | "probe";
export type OperationState =
  "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted";

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
  state:
    | "succeeded"
    | "failed"
    | "cancelled"
    | "interrupted"
    | "refused"
    | "unconfirmed";
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
  matchesApplied: {
    context: boolean | null;
    alias: boolean | null;
    defaultEffort: boolean | null;
  };
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
  | {
      mode: "selected";
      sources?: readonly string[];
      consumers?: readonly string[];
      models?: Readonly<Record<string, readonly string[]>>;
    };

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

export interface UsageCoverage {
  requests: number | null;
  unknownRequests: number | null;
  incompleteRequests: number | null;
  records: number | null;
  unknownTimeRequests: number | null;
  /** Q11 cache reporting evidence, separate from token coverage. A reported
   *  zero is evidence; unreported/unknown are counted, never read as zero. */
  cacheReportedRequests: number | null;
  cacheUnreportedRequests: number | null;
  cacheUnknownRequests: number | null;
  /** Records whose time could not be parsed; a bounded window excludes them
   *  instead of guessing a time. */
  excludedUnknownTimeRequests: number | null;
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
  /** Cache evidence is separate from input/output coverage (Q11). */
  cachedInputTokens: number | null;
  cacheReportedRequests: number | null;
  cacheUnreportedRequests: number | null;
  cacheUnknownRequests: number | null;
}

export interface UsageDetail {
  identity: string;
  inputTokens: number | null;
  outputTokens: number | null;
  coverage: UsageCoverage;
  tokenUsage: TokenUsage;
}

export interface KeyUsage {
  keyId: string;
  keyName: string | null;
  deleted: boolean | null;
  inputTokens: number | null;
  outputTokens: number | null;
  coverage: UsageCoverage;
  tokenUsage: TokenUsage;
  models: UsageDetail[];
  days: UsageDetail[];
  undated: Omit<UsageDetail, "identity"> | null;
}

/** 未归属桶（Rev19）：账本里没有可归因 `key_id` 的记录，形状与每 Key 行
 *  相同，但没有 Key 身份。`keyId` 恒为 null——它永远单列，绝不摊到任何
 *  Key；`tokenUsage` 与 `models`/`days` 用同一套归一化规则（后端已交付），
 *  缺席的字段如实为 null / 空，不折成完整口径。 */
export interface ShareUsageBucket {
  keyId: null;
  inputTokens: number | null;
  outputTokens: number | null;
  coverage: UsageCoverage;
  tokenUsage: TokenUsage;
  models: UsageDetail[];
  days: UsageDetail[];
  /** Records whose date is unknowable; same shape as a key's undated row. */
  undated: Omit<UsageDetail, "identity"> | null;
}

/** D18-2: the real outcome split of the filtered records. `unknown` counts
 *  records whose outcome was never recorded and is NEVER merged into `success`.
 *  null means the backend did not provide that counter (legacy shape), which the
 *  page renders as 未提供 instead of a fabricated 0. */
export interface ShareUsageResults {
  success: number | null;
  failed: number | null;
  cancelled: number | null;
  unknown: number | null;
  total: number | null;
  unknownResult: number | null;
}

export interface ShareUsage {
  keys: KeyUsage[];
  unattributed: ShareUsageBucket;
  coverage: JsonObject;
  checkedAt: string | null;
  requests: number | null;
  /** Echo of the filter set this aggregate was built with (r14 additive).
   *  null means the response did not echo it (legacy shape). */
  filters: ShareUsageFilters | null;
  /** The four real outcomes plus their total and coverage (C8). */
  results: ShareUsageResults | null;
}

/** Filters shared by the share-usage aggregate and the per-request page. */
export type ShareUsageWindow = "1h" | "24h" | "7d" | "30d" | "all";
export interface ShareUsageFilters {
  window: ShareUsageWindow | null;
  key: string | null;
  model: string | null;
  result: string | null;
}

export type ShareRequestResult = "success" | "failed" | "cancelled" | "unknown";
export type ShareUsageSource = "upstream" | "partial" | "estimated" | "none";
export type ShareCacheReportStatus = "reported" | "unreported" | "unknown";

/** One ledger row on the per-request page (`GET .../share-usage/requests`).
 *  Unknown stays null: a missing time keeps `time`/`day` null and unknown
 *  usage keeps token counts null. */
export interface ShareUsageRequestRow {
  requestId: string | null;
  time: string | null;
  day: string | null;
  keyId: string | null;
  keyName: string | null;
  deleted: boolean | null;
  model: string | null;
  /** Raw ledger status; null when the row did not record one. */
  status: string | null;
  result: ShareRequestResult;
  attribution: string | null;
  usageSource: ShareUsageSource;
  cacheReportStatus: ShareCacheReportStatus;
  inputTokens: number | null;
  outputTokens: number | null;
  /** The ledger's measured request duration; null for an old row without it. */
  durationMs: number | null;
  cacheTokens: number | null;
  reasoningTokens: number | null;
}

export interface ShareUsageRequestsPage {
  rows: ShareUsageRequestRow[];
  /** Opaque line-number cursor; null means no older matched row in the read window. */
  nextCursor: string | null;
  truncated: boolean | null;
  filters: ShareUsageFilters | null;
  coverage: JsonObject;
  checkedAt: string | null;
}

/** r14 overview capacity block (`GET .../share-management/status.capacity`).
 *  `configured` is the user-configured admission cap (128 in the current
 *  deployment), never a measured upstream capacity. `inFlight` comes from the
 *  restricted broker's read-only instance view; `queued` and `upstreamCapacity`
 *  have no read seam and stay null with an explicit reason. */
export interface ShareCapacity {
  configured: number | null;
  inFlight: number | null;
  inFlightReason: string | null;
  queued: number | null;
  queuedReason: string | null;
  upstreamCapacity: number | null;
  upstreamCapacityMeasured: boolean | null;
  upstreamCapacityReason: string | null;
  checkedAt: number | null;
  note: string | null;
}

/** One read-only cloud-ops section. A missing configuration is
 *  `configured: false`; an unreadable state is `available: false` with a
 *  reason — never a fabricated "no budget / no backup / no revocations". */
export interface CloudOpsSection {
  configured: boolean;
  available: boolean;
  status: JsonObject | null;
  reason: string | null;
  remote: JsonObject | null;
  generationFloor: number | null;
  records: number | null;
  pendingRemote: number | null;
  exportedAt: string | null;
}

/** `GET /api/console/cloud-ops` (S2 read face, D18-11 narrowed).
 *
 *  The cloud console keeps only the security read face: the budget guard and the
 *  automatic backup are no longer part of the default assembly, so their sections
 *  are gone from this contract instead of being rendered as "not configured". */
export interface CloudSecurityStatus {
  available: boolean;
  /** The time this read face answered; never a last-check time. */
  readAt: number | null;
  security: CloudOpsSection;
  reasons: string[];
  /** Explicitly-open questions this read face does not close. */
  unresolved: JsonObject | null;
}

export interface ReadResult<T> {
  state: "ready" | "unavailable" | "refused" | "error";
  value: T | null;
  httpStatus: number | null;
  reason: string | null;
}

/** S5 public share supervision read face (`GET /api/console/share-management/public`).
 *
 *  Every optional field is nullable: a missing value is "not provided" and is never
 *  coerced into a healthy default. `overall`, `entry.state`, `recovery.state` and
 *  `intent.state` are closed sets with an explicit `unknown` member, while the
 *  backend's own reason codes pass through as opaque strings (they are never mapped
 *  to `ok`). */
export type PublicShareOverall =
  "ok" | "degraded" | "all_failed" | "incomplete" | "not_checked" | "unknown";
export type PublicShareEntryState = "ok" | "failed" | "unchecked";
export type PublicShareCheck = "ok" | "failed" | "not_checked";
export type PublicDnsSourceState =
  "ok" | "empty" | "nxdomain" | "error" | "timeout" | "not_checked";
export type PublicDnsState =
  | "ok"
  | "empty"
  | "nxdomain"
  | "error"
  | "timeout"
  | "disagreement"
  | "unconfirmed"
  | "not_checked";

/** One DoH source's bounded reading. A missing TTL stays null (未知), never a
 *  fabricated 300 seconds; the page shows the source's own rcode and time. */
export interface PublicDnsSource {
  source: string | null;
  state: PublicDnsSourceState;
  status: number | null;
  classification: string | null;
  addresses: string[];
  addressesTruncated: boolean | null;
  ttlSeconds: number | null;
  negativeTtlSeconds: number | null;
  checkedAt: number | null;
  elapsedMs: number | null;
}

/** Additive two-source DNS evidence. ``disagreement`` means one source had a
 *  valid answer and the other explicitly denied it; a different valid address
 *  set is NOT a fault. */
export interface PublicDnsDetail {
  state: PublicDnsState;
  reason: string | null;
  addresses: string[];
  addressesTruncated: boolean | null;
  ttlSeconds: number | null;
  negativeTtlSeconds: number | null;
  /** Aggregate observation time; a missing value keeps TTL remaining unknown. */
  checkedAt: number | null;
  sources: PublicDnsSource[];
}
export type PublicShareRecoveryState =
  | "idle"
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "manual_required"
  | "budget_exhausted"
  | "stopped"
  | "unknown";

export interface PublicShareEntry {
  host: string | null;
  ip: string | null;
  state: PublicShareEntryState;
  dns: PublicShareCheck;
  tls: PublicShareCheck;
  certificate: PublicShareCheck;
  reason: string | null;
  elapsedMs: number | null;
  checkedAt: number | null;
}

export interface PublicShareRecovery {
  state: PublicShareRecoveryState;
  action: string | null;
  stage: string | null;
  reason: string | null;
  attemptedAt: string | null;
  finishedAt: string | null;
  manual: boolean | null;
}

export interface PublicShareBudget {
  windowSeconds: number | null;
  mappingUsed: number | null;
  mappingLimit: number | null;
  tsRestartUsed: number | null;
  tsRestartLimit: number | null;
  mappingNextEligibleAt: string | null;
  tsRestartNextEligibleAt: string | null;
  /** True when the budget file is damaged/version-mismatched; the page must
   *  show 「预算不可读，不自动恢复」 and must not treat it as 0/0. */
  unreadable: boolean | null;
  reason: string | null;
}

export interface PublicShareEvent {
  at: string | null;
  status: string | null;
  code: string | null;
  action: string | null;
}

export interface PublicShareIntent {
  state: "running" | "stopped" | "unknown";
  at: string | null;
}

export interface PublicShareRound {
  nextCheckAt: string | null;
  nextActionAt: string | null;
}

export interface PublicShareSnapshot {
  observedAt: number | null;
  overall: PublicShareOverall;
  reason: string | null;
  publicUrl: string | null;
  host: string | null;
  entries: PublicShareEntry[];
  dns: PublicDnsDetail | null;
  recovery: PublicShareRecovery | null;
  budget: PublicShareBudget | null;
  intent: PublicShareIntent | null;
  round: PublicShareRound | null;
  local: JsonObject | null;
}

export interface PublicShareStatus {
  available: boolean;
  version: number | null;
  checkedAt: number | null;
  snapshot: PublicShareSnapshot | null;
  events: PublicShareEvent[] | null;
  eventsTruncated: boolean | null;
  /** True when the events file exists but is unreadable; the page must show
   *  「事件不可读」 and must not render it as 「无事件」. */
  eventsUnreadable: boolean | null;
  budget: PublicShareBudget | null;
  cancel: { cancelled: boolean | null; reason: string | null } | null;
  unreadable: boolean;
  reason: string | null;
  stateFile: string | null;
}

export interface PublicShareEventsPage {
  available: boolean;
  events: PublicShareEvent[] | null;
  nextBefore: number | null;
  truncated: boolean | null;
  reason: string | null;
}

/** Redacted diagnostics export (`GET .../public-export?format=json`). The page
 *  downloads this object as a Blob; it never re-reads or augments it from other
 *  endpoints. */
export interface PublicShareExport {
  exportVersion: number | null;
  generatedAt: string | null;
  redaction: JsonObject | null;
  snapshot: PublicShareSnapshot | null;
  events: PublicShareEvent[] | null;
  budget: PublicShareBudget | null;
  cancel: { cancelled: boolean | null; reason: string | null } | null;
  limits: JsonObject | null;
}

/** Rev15 (2026-10-06): read-only connection info for one selected gateway.
 *
 *  Mirrors the Python read face (`console_gateway_connection.py`) exactly. The
 *  read state of the call key is a closed set; `unknown` is an honest state of
 *  its own and is never coerced into a "no key" state. A plaintext key exists
 *  only while the read state is `present`; every other state keeps it null. */
export type ConnectionKeyState =
  | "present" // authority file/env readable and non-empty
  | "absent" // chat2api loopback instance: no key and auth off
  | "needs_key_unconfigured" // gateway needs a key but the source is missing/empty
  | "unreadable" // source exists but cannot be read
  | "unknown"; // running instance unconfirmed; never downgraded

export type ConnectionKeySourceState =
  "present" | "absent" | "unreadable" | "unknown";

/** Rev15 R32-KEY-ACTIVE: how the shown key value is consumed by the running
 *  instance. `per_request` = the gateway rereads the file on every request;
 *  `startup_env` = the value was read into the process environment at launch
 *  and later file edits may not take effect; `config_consumed` = the gateway
 *  takes the key from its own config document.
 *  `unknown` is honest and never coerced into one of the known mechanisms. */
export type ConnectionKeyInUse =
  "per_request" | "startup_env" | "config_consumed" | "unknown";

export type ConnectionAuth =
  | "bearer" // clients must present the key
  | "loopback_no_key" // loopback instance with inbound auth off
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

// ---------------------------------------------------------------- UM read face (B5/B6)
//
// Wire shapes of `/api/console/um/*` plus the supporting read faces
// (`/api/console/services`, `/api/console/operations`, `/api/summary`). They
// mirror the Python handlers field for field; the B6 data controller maps them
// to the frozen view contract in `local/um-ui/contract.ts`. A reading keeps the
// backend's honest kinds (exact / atLeast / estimate / unreported / unknown /
// missing); a real zero is only ever `exact` with value 0.

export type UmReadingKind =
  "exact" | "atLeast" | "estimate" | "unreported" | "unknown" | "missing";

export type UmReading =
  | { kind: "exact"; value: number; note?: string }
  | { kind: "atLeast"; value: number; note?: string }
  | { kind: "estimate"; value: number; note?: string }
  | { kind: "unreported"; note?: string }
  | { kind: "unknown"; note?: string }
  | { kind: "missing"; note?: string };

export interface UmTodayPayload {
  day: string;
  tokens: { total: UmReading; input: UmReading; output: UmReading };
  cacheRead: {
    tokens: UmReading;
    report: "reported" | "partial" | "unreported" | "unknown";
    detail: string;
  };
  reasoning: UmReading;
  requests: { recorded: UmReading; failed: UmReading };
  speed: {
    value: UmReading;
    basis: "whole_request" | "generation";
    samples: number | null;
  };
}

export interface UmTodayResponse {
  today: UmTodayPayload;
  coverage: JsonObject;
  timezone: string | null;
  /** Epoch milliseconds; the backend reports seconds. */
  checkedAt: number | null;
}

export type UmTrendRange = "7d" | "14d" | "30d";
export type UmReadWindow = "today" | "24h" | "7d" | "30d";

export interface UmBucketPayload {
  tokens: UmReading;
  requests: UmReading;
  speed: UmReading;
}

export interface UmTrendModelPayload {
  model: string;
  daily: UmBucketPayload[];
  total: UmBucketPayload;
  failed: UmReading;
}

export interface UmTrendPayload {
  range: UmTrendRange;
  days: string[];
  all: { daily: UmBucketPayload[]; total: UmBucketPayload };
  models: UmTrendModelPayload[];
  speedBasis: "whole_request" | "generation";
}

export interface UmTrendResponse {
  trend: UmTrendPayload;
  coverage: JsonObject;
  timezone: string | null;
  checkedAt: number | null;
}

export type UmRequestResult =
  | "success"
  | "failed"
  | "truncated"
  | "client_disconnected"
  | "cancelled"
  | "unknown";

export interface UmRequestRowPayload {
  key: string;
  requestId: string | null;
  at: number;
  model: string;
  result: UmRequestResult;
  resultNote: string;
  httpStatus: number | null;
  input: UmReading;
  output: UmReading;
  cacheRead: UmReading;
  reasoning: UmReading;
  durationMs: UmReading;
  firstTokenMs: UmReading;
  speed: UmReading;
  speedBasis: "whole_request" | "generation";
  retries: { at: number; note: string }[] | null;
  error: { code: string; message: string } | null;
}

export interface UmRequestPagePayload {
  rows: UmRequestRowPayload[];
  scope: string;
  earlier: string | null;
}

export interface UmRequestsResponse {
  page: UmRequestPagePayload;
  models: string[];
  coverage: JsonObject;
  timezone: string | null;
  checkedAt: number | null;
}

export type UmLogLevel = "error" | "warn" | "info" | "debug" | "unknown";

export interface UmLogLinePayload {
  key: string;
  at: number | null;
  level: UmLogLevel;
  source: string;
  text: string;
  requestId: string | null;
}

export interface UmLogPagePayload {
  lines: UmLogLinePayload[];
  sources: string[];
  scope: string;
  earlier: string | null;
  coverage: JsonObject;
}

export interface UmRequestLogsPayload {
  lines: UmLogLinePayload[];
  scope: string;
  partial: boolean;
  coverage: JsonObject;
}

export interface UmMaintenanceRowPayload {
  key: string;
  /** Raw backend action id; the frozen view falls back to this name when it
   *  is not one of its five write actions (e.g. account.um_status). */
  action: string;
  state:
    "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted";
  at: number;
  finishedAt: number | null;
  steps: { at: number; note: string }[];
  result: string;
}

export interface UmMaintenancePagePayload {
  rows: UmMaintenanceRowPayload[];
  scope: string;
  earlier: string | null;
  coverage: JsonObject;
}

export interface UmCatalogModelPayload {
  id: string;
  displayName: string | null;
  declaredContext: number | null;
  efforts: string[];
  effortsSource: string | null;
  defaultEffort: string | null;
}

export interface UmCatalogPayload {
  models: UmCatalogModelPayload[];
  /** Epoch milliseconds; the backend reports seconds. */
  checkedAt: number | null;
  source: string;
  warnings: string[];
}

export interface UmServiceEntry {
  key: string;
  displayName: string | null;
  port: number | null;
  state: string;
  healthy: boolean | null;
  detail: string;
  processIds: number[];
}

export interface UmUpstreamFaultPayload {
  id: string;
  classification: string;
  upstreamStatus: number | null;
  summary: string | null;
  failures: number;
  startedAt: number;
  lastAt: number;
  source: string;
}

export interface UmUpstreamRecoveryPayload extends UmUpstreamFaultPayload {
  recoveredAt: number;
  recoverySource: string;
  recoveryStatus: number | null;
  recoveryDetail: string | null;
}

export interface UmUpstreamFaultsPayload {
  active: UmUpstreamFaultPayload | null;
  history: UmUpstreamRecoveryPayload[];
  lastCheck: {
    at: number;
    source: string;
    result: string;
    upstreamStatus: number | null;
  } | null;
  nextCheckAt: number | null;
  source: string;
}

export interface UmServicesPayload {
  services: UmServiceEntry[];
  /** Epoch milliseconds; the backend reports seconds. */
  checkedAt: number | null;
  cached: boolean;
  cacheAgeSeconds: number | null;
}

export interface UmOperationPayload {
  id: string;
  action: string;
  target: string;
  state:
    "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted";
  result: JsonObject | null;
  error: JsonObject | null;
  /** Epoch milliseconds; the backend reports seconds. */
  createdAt: number | null;
  finishedAt: number | null;
  steps: { at: number; note: string }[];
}

export interface UmOperationsPayload {
  operations: UmOperationPayload[];
}

export interface UmSummaryPayload {
  /** `cloud` when the backend is the cloud role; the local role omits it. */
  role: string | null;
}
