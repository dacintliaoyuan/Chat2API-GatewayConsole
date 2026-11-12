/** B6 data controller for the frozen UM console (handoff r3).
 *
 *  The three pages are views only: this controller owns every read, task,
 *  cancel and backend action and hands the console a `UmViewState` through
 *  `UmConsoleHandle.update`. It never renders and the views never fetch.
 *
 *  Reads go through the existing console backend seam:
 *  - `/api/console/um/today|trend|requests|logs|request-logs|maintenance`
 *  - `/api/console/services` (runtime snapshot, the authority for chat2api)
 *  - `/api/console/gateway-connection?service=chat2api` (access facts)
 *  - `/api/summary` (`umReference`: Magpie reference price facts)
 *  - the operation registry for `account.um_status`, `account.um_verify`,
 *    `account.login`, `service.start|stop|restart`, `model.catalog`,
 *    `model.context_probe_preview`, `model.context_probe_run` and
 *    `model.speedtest` (the explicit UM batch).
 *
 *  Honesty rules kept from the frozen contract:
 *  - a real zero is `{ kind: "exact", value: 0 }`; every absent figure keeps
 *    the backend's own kind (unreported / unknown / missing);
 *  - a failed refresh keeps the last good data and marks it stale;
 *  - a cancelled or superseded read never installs its answer;
 *  - a missing/unavailable ledger is a failure, never an empty list;
 *  - the role gate folds into every write: nothing is posted until the backend
 *    role is confirmed, and a cloud backend (no local UM read face) is reported
 *    as unavailable instead of being filled with invented data.
 *
 *  No chat body is collected: the request/log reads use the backend's bounded,
 *  already-redacted read faces and never request message content. */

import type { RoleGate } from "../console/role-gate";
import {
  ActionBusyError,
  ActionUnavailableError,
  BackendClient,
  BackendReadError,
  type UmLogsReadQuery,
  type UmRequestsReadQuery,
} from "../console/backend/client";
import { ContractError, UmReadStatusError } from "../console/backend/normalize";
import {
  LatestRequests,
  createFetchTransport,
} from "../console/backend/transport";
import type {
  ActionCapability,
  ContextEvidence,
  GatewayConnection,
  JsonObject,
  Operation,
  OperationResult,
  ReadResult,
  UmCatalogPayload,
  UmCostPayload,
  UmMaintenancePagePayload,
  UmOperationPayload,
  UmServicesPayload,
  UmSummaryPayload,
  UmSummaryReferencePayload,
  UmTodayPayload,
  UmTrendPayload,
} from "../console/backend/types";
import { unwiredState } from "./um-ui/contract";
import type {
  ActionProgress,
  Capability,
  Failure,
  FailureKind,
  HistoryFilter,
  Load,
  LogQuery,
  MaintenanceAction,
  ModelTestState,
  Reading,
  RequestQuery,
  RequestsView,
  TestKind,
  TrendRange,
  UmAccess,
  UmCatalog,
  UmConsoleHandle,
  UmCost,
  UmEvent,
  UmIssue,
  UmLogin,
  UmLogLine,
  UmLogPage,
  UmMaintenancePage,
  UmModelRow,
  UmModels,
  UmOverview,
  UmRequests,
  UmService,
  UmTestDetail,
  UmTestRecord,
  UmTestRun,
  UmToday,
  UmTrend,
  UmVerify,
  UmViewState,
} from "./um-ui/contract";
import { when } from "./um-ui/format";

/** chat2api on 8000: the only service the local UM console manages. */
const CHAT2API = "chat2api";
const UM_SOURCE = "um";
/** The registry's five maintenance actions, their write intents and timeouts.
 *  `account.um_verify` is a single upstream probe, `account.login` may open a
 *  local sign-in window (bounded by the backend's own 30 s + 190 s budget), and
 *  the service actions wait on the runtime wrapper. */
const ACTION: Readonly<Record<MaintenanceAction, string>> = {
  verify: "account.um_verify",
  login: "account.login",
  start: "service.start",
  stop: "service.stop",
  restart: "service.restart",
};
const ACTION_INTENT: Readonly<
  Record<MaintenanceAction, "probe" | "client-write">
> = {
  verify: "probe",
  login: "client-write",
  start: "client-write",
  stop: "client-write",
  restart: "client-write",
};
const ACTION_TIMEOUT: Readonly<Record<MaintenanceAction, number>> = {
  verify: 60_000,
  login: 300_000,
  start: 120_000,
  stop: 120_000,
  restart: 180_000,
};
const ACTION_PENDING_NOTE: Readonly<Record<MaintenanceAction, string>> = {
  verify: "向上游确认，不调用模型",
  login: "先核验，需要时才打开登录窗口",
  start: "启动 8000 上的 chat2api",
  stop: "记录停用意图后停止进程",
  restart: "停止后再启动",
};
const TERMINAL = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
/** One UM batch is bounded by the backend: at most 20 models, one short
 *  request per model, no retry, one shared 20-request budget for context. */
const MAX_BATCH_MODELS = 20;

const SERVICE_STATE: Readonly<Record<string, UmService["state"]>> = {
  Running: "running",
  Starting: "starting",
  Stopping: "stopping",
  Stopped: "stopped",
  Residual: "stopped",
  Degraded: "degraded",
  Conflict: "degraded",
};
const SERVICE_WORD: Readonly<Record<string, string>> = {
  Running: "运行中",
  Starting: "启动中",
  Stopping: "停止中",
  Stopped: "未运行",
  Residual: "发现受管残留进程",
  Degraded: "降级",
  Conflict: "端口被占用",
};
const LOGIN_WORD: Readonly<Record<UmLogin["state"], string>> = {
  valid: "有效",
  needs_login: "需要登录",
  invalid: "登录失败",
  logging_in: "登录中",
  unknown: "状态未知",
};

type TestEntry = {
  model: string;
  state: ModelTestState;
  summary: string;
  detail: UmTestDetail | null;
};

type Gate = { ok: true } | { ok: false; reason: string };

export interface UmDataControllerOptions {
  gate: RoleGate;
  /** The console-relative client (default base path `/gw/usagesrv/api/console`). */
  client?: BackendClient;
  /** `/api/summary` rides the console host but not the console prefix. */
  summaryClient?: BackendClient;
  now?: () => number;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error ?? "未知错误");
}

/** One failed read as the frozen view's failure: no answer / unauthenticated /
 *  refused / unavailable / error are kept apart. */
function failureFrom(error: unknown, at: number): Failure {
  if (error instanceof BackendReadError) {
    if (error.kind === "timeout")
      return { kind: "no-answer", reason: error.message, at };
    if (error.kind === "network")
      return { kind: "no-answer", reason: error.message, at };
    if (error.kind === "contract")
      return { kind: "error", reason: error.message, at };
    const status = error.status;
    if (status === 401)
      return { kind: "unauthenticated", reason: error.message, at };
    if (status === 403) return { kind: "refused", reason: error.message, at };
    if (
      status === 404 ||
      status === 410 ||
      status === 502 ||
      status === 503 ||
      status === 504
    )
      return { kind: "unavailable", reason: error.message, at };
    return { kind: "error", reason: error.message, at };
  }
  if (error instanceof UmReadStatusError) {
    const status = error.status;
    if (
      status === "missing" ||
      status === "unavailable" ||
      status === "unconfigured"
    )
      return { kind: "unavailable", reason: error.message, at };
    if (status === "denied")
      return { kind: "refused", reason: error.message, at };
    if (status === "auth_required")
      return { kind: "unauthenticated", reason: error.message, at };
    return { kind: "error", reason: error.message, at };
  }
  if (error instanceof ActionUnavailableError)
    return { kind: "unavailable", reason: error.message, at };
  if (error instanceof ActionBusyError)
    return { kind: "error", reason: error.message, at };
  if (error instanceof ContractError)
    return { kind: "error", reason: error.message, at };
  return { kind: "error", reason: messageOf(error), at };
}

/** The failure of one operation result (the operation itself answered, but its
 *  state is a failure/refusal): the backend's own code and message are kept. */
function operationFailure(result: OperationResult, at: number): Failure {
  const error = result.operation?.error;
  const code = error ? text(error.code) : null;
  const message = error ? text(error.message) : null;
  const reason =
    [code, message].filter(Boolean).join("：") ||
    result.reason ||
    "操作没有成功";
  if (result.state === "unconfirmed") return { kind: "no-answer", reason, at };
  return { kind: "error", reason, at };
}

/** One operation record's own failure (the row's error), for reads whose
 *  operation record is the only evidence. */
function failureFromOperationPayload(
  op: UmOperationPayload,
  at: number
): Failure {
  const error = op.error;
  const code = error ? text(error.code) : null;
  const message = error ? text(error.message) : null;
  return {
    kind: "error",
    reason: [code, message].filter(Boolean).join("：") || "操作没有成功",
    at,
  };
}

function operationNote(result: OperationResult): string {
  const error = result.operation?.error;
  const code = error ? text(error.code) : null;
  const message = error ? text(error.message) : null;
  return (
    [code, message].filter(Boolean).join("：") ||
    result.reason ||
    (result.state === "succeeded" ? "已完成" : "结果未确认")
  );
}

/** `loading` on a first read, `refreshing` on a newer read with data shown. */
function beginLoad<T>(load: Load<T>): Load<T> {
  if (load.state === "ready")
    return { state: "ready", data: load.data, at: load.at, refreshing: true };
  if (load.state === "stale")
    return {
      state: "stale",
      data: load.data,
      at: load.at,
      failure: load.failure,
      refreshing: true,
    };
  return { state: "loading" };
}
function failedOrStale<T>(previous: Load<T>, failure: Failure): Load<T> {
  if (previous.state === "ready")
    return { state: "stale", data: previous.data, at: previous.at, failure };
  if (previous.state === "stale")
    return { state: "stale", data: previous.data, at: previous.at, failure };
  return { state: "failed", failure };
}

function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}
function object(value: unknown): JsonObject | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : null;
}
function rowsOf(value: unknown): JsonObject[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is JsonObject =>
          item !== null && typeof item === "object" && !Array.isArray(item)
      )
    : [];
}
function rowText(row: JsonObject | null, key: string): string | null {
  return row ? text(row[key]) : null;
}
function rowNumber(row: JsonObject | null, key: string): number | null {
  const value = row ? row[key] : null;
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}
function rowCount(row: JsonObject | null, key: string): number | null {
  const value = rowNumber(row, key);
  return value !== null && Number.isSafeInteger(value) ? value : null;
}

/** A reading's number, only for the kinds that carry one. */
function readingNumber(reading: Reading): number | null {
  return reading.kind === "exact" ||
    reading.kind === "atLeast" ||
    reading.kind === "estimate"
    ? reading.value
    : null;
}
function missing(note: string): Reading {
  return { kind: "missing", note };
}

/** The request-query identity used to decide whether a page answers the query
 *  the console asked for. */
function queryKey(query: RequestQuery): string {
  return `${query.window}|${query.model ?? ""}|${query.result}|${query.search}`;
}
function logQueryKey(query: LogQuery): string {
  return `${query.window}|${query.source ?? ""}|${[...query.levels].sort().join(",")}`;
}
function sameQuery(a: RequestQuery, b: RequestQuery): boolean {
  return (
    a.window === b.window &&
    a.model === b.model &&
    a.result === b.result &&
    a.search === b.search
  );
}
function sameLogQuery(a: LogQuery, b: LogQuery): boolean {
  return (
    a.window === b.window &&
    a.source === b.source &&
    a.levels.length === b.levels.length &&
    a.levels.every((level) => b.levels.includes(level))
  );
}

/** The catalog rows' declared-source label: the same label the catalog read
 *  reports, so the view never claims a stricter origin than the read used. */
const CATALOG_PLAIN_SOURCE = "UM /api/models（只读目录）";
const CATALOG_FRESH_SOURCE = "UM /api/models（raw-fresh 严格取新）";

/** The role gate's confirmation is asynchronous: reads that ride the operation
 *  registry must wait until the backend role is no longer `checking`, otherwise
 *  the front-end gate refuses their POST with its own 403. */
const GATE_WAIT_MS = 2_500;

export class UmDataController {
  private readonly gate: RoleGate;
  private readonly client: BackendClient;
  private readonly summaryClient: BackendClient;
  private readonly requests = new LatestRequests();
  private readonly now: () => number;
  private handle: UmConsoleHandle | null = null;
  private state: UmViewState;
  private disposed = false;
  private started = false;

  /** Resolves once the role probe (`/api/summary` + the capability table) ran. */
  private readonly roleReady: Promise<void>;
  private resolveRole!: () => void;
  private roleAvailable = true;
  private roleReason = "";
  private summary: UmSummaryPayload | null = null;

  private capabilities: Map<string, ActionCapability> | null = null;
  private capabilityFailure: Failure | null = null;
  private unsubscribeGate: (() => void) | null = null;

  /** One token per read lane: a superseded read never installs its answer or
   *  drops the newer read's spinner. */
  private readonly laneTokens = new Map<string, number>();
  private pendingPublish = false;

  // Raw reads kept for derived sections (the frozen view has no place for them).
  private operations: UmOperationPayload[] = [];
  private operationsAt: number | null = null;
  private operationsFailure: Failure | null = null;
  private operationsPromise: Promise<void> | null = null;
  private catalog: UmCatalogPayload | null = null;
  private catalogSource = CATALOG_PLAIN_SOURCE;
  private catalogFetchedAt: number | null = null;
  private catalogStale: Failure | null = null;
  private catalogGeneration = 0;
  private evidence: ContextEvidence[] = [];
  private evidenceAt: number | null = null;
  private timezone: string | null = null;
  private failedLatestAt: number | null = null;
  private failedLatestUnread = false;
  private trendRange: TrendRange = "7d";
  private requestsLoadedFor: string | null = null;
  private logsLoadedFor: string | null = null;
  private maintenanceLoaded = false;
  private overviewLoaded = false;
  private modelsLoaded = false;

  private run: UmTestRun | null = null;
  private runOperation: Operation | null = null;
  private cancelSentFor: string | null = null;
  private lastProgressReadAt = 0;
  private reconnectNoticeFor: string | null = null;

  constructor(options: UmDataControllerOptions) {
    this.gate = options.gate;
    this.now = options.now ?? Date.now;
    this.client =
      options.client ??
      new BackendClient(
        createFetchTransport({ fetch: (input, init) => fetch(input, init) })
      );
    this.summaryClient =
      options.summaryClient ??
      new BackendClient(
        createFetchTransport({
          fetch: (input, init) => fetch(input, init),
          basePath: "/gw/usagesrv/api",
        })
      );
    this.state = unwiredState();
    this.roleReady = new Promise<void>((resolve) => {
      this.resolveRole = resolve;
    });
    this.unsubscribeGate = this.gate.onChange(() => {
      this.recomputeCapabilities();
      if (
        this.gate.verdict().state === "confirmed" &&
        this.capabilities === null
      )
        void this.loadCapabilities();
    });
  }

  /** The state the console renders right now. */
  viewState(): UmViewState {
    return this.state;
  }

  /** Hand the controller the console handle; the controller never fetches before
   *  this point, so a page that fails to mount cannot leave reads running. */
  attach(handle: UmConsoleHandle): void {
    this.handle = handle;
    handle.update(this.state);
  }

  /** Mark the overview sections as loading before the first paint; no read
   *  starts here, so the page never flashes 「数据未接线」 while attaching. */
  prepare(): void {
    if (this.started || this.disposed) return;
    this.patchOverview({
      service: beginLoad(this.state.overview.service),
      login: beginLoad(this.state.overview.login),
      verify: beginLoad(this.state.overview.verify),
      today: beginLoad(this.state.overview.today),
      cost: beginLoad(this.state.overview.cost),
      trend: beginLoad(this.state.overview.trend),
      issues: { state: "loading" },
    });
  }

  /** Start the role probe. The console emits its first `page` event on mount; the
   *  role probe itself starts here so writes are confirmed as early as possible. */
  start(): void {
    if (this.started || this.disposed) return;
    this.started = true;
    void this.bootstrap();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.requests.cancelAll();
    this.unsubscribeGate?.();
    this.unsubscribeGate = null;
    this.handle = null;
  }

  handleEvent(event: UmEvent): void {
    if (this.disposed) return;
    switch (event.type) {
      case "page":
        this.onPage(event.page, event.view);
        return;
      case "refresh":
        this.onRefresh(event.page, event.view);
        return;
      case "trend.range":
        this.onTrendRange(event.range);
        return;
      case "um.verify":
        void this.runMaintenance("verify");
        return;
      case "um.login":
        void this.runMaintenance("login");
        return;
      case "um.service":
        void this.runMaintenance(event.action);
        return;
      case "requests.query":
        this.onRequestsQuery(event.query);
        return;
      case "requests.earlier":
        this.readEarlierRequests(event.cursor);
        return;
      case "requests.detail":
        this.readRequestDetail(event.key);
        return;
      case "logs.query":
        this.onLogsQuery(event.query);
        return;
      case "logs.earlier":
        this.readEarlierLogs(event.cursor);
        return;
      case "maintenance.earlier":
        this.readMaintenance(true);
        return;
      case "models.refreshCatalog":
        void this.refreshCatalog();
        return;
      case "models.test":
        this.startTest(event.kind, event.models);
        return;
      case "models.stop":
        void this.cancelRun(event.runId);
        return;
      case "models.history":
        this.applyHistoryFilter(event.filter);
        return;
    }
  }

  // ---------------------------------------------------------------- publish

  private publish(): void {
    if (this.disposed || this.pendingPublish) return;
    this.pendingPublish = true;
    queueMicrotask(() => {
      this.pendingPublish = false;
      if (!this.disposed) this.handle?.update(this.state);
    });
  }

  private patchOverview(patch: Partial<UmOverview>): void {
    this.state = {
      ...this.state,
      overview: { ...this.state.overview, ...patch },
    };
    this.publish();
  }
  private patchRequests(patch: Partial<UmRequests>): void {
    this.state = {
      ...this.state,
      requests: { ...this.state.requests, ...patch },
    };
    this.publish();
  }
  private patchModels(patch: Partial<UmModels>): void {
    this.state = { ...this.state, models: { ...this.state.models, ...patch } };
    this.publish();
  }
  private notice(
    tone: "ok" | "warn" | "err" | "neutral",
    message: string
  ): void {
    this.patchModels({ notice: { tone, text: message, at: this.now() } });
  }

  private nextLane(lane: string): number {
    const token = (this.laneTokens.get(lane) ?? 0) + 1;
    this.laneTokens.set(lane, token);
    return token;
  }
  private isCurrentLane(lane: string, token: number): boolean {
    return this.laneTokens.get(lane) === token;
  }

  /** One section read: loading on the first read, refreshing on a newer one;
   *  a failure keeps the last good data as stale; a cancelled or superseded read
   *  never installs its answer and never drops the newer read's spinner. */
  private async readSection<T, D>(
    lane: string,
    current: () => Load<D>,
    apply: (load: Load<D>) => void,
    task: (signal: AbortSignal) => Promise<T>,
    map: (value: T) => D,
    after?: (value: T) => void
  ): Promise<T | null> {
    const previous = current();
    const token = this.nextLane(lane);
    apply(beginLoad(previous));
    try {
      const outcome = await this.requests.run(lane, task);
      if (outcome.state === "cancelled") {
        if (this.isCurrentLane(lane, token)) apply(previous);
        return null;
      }
      if (outcome.state === "superseded" || !this.isCurrentLane(lane, token))
        return null;
      apply({ state: "ready", data: map(outcome.value), at: this.now() });
      after?.(outcome.value);
      return outcome.value;
    } catch (error) {
      if (this.disposed || !this.isCurrentLane(lane, token)) return null;
      apply(failedOrStale(previous, failureFrom(error, this.now())));
      return null;
    }
  }

  // ---------------------------------------------------------------- role

  private async bootstrap(): Promise<void> {
    if (this.gate.role !== "local") {
      this.markUnavailable(
        "本页是本地 UM 控制台：没有装配云端控制台的数据读面"
      );
      return;
    }
    this.patchOverview({
      service: beginLoad(this.state.overview.service),
      login: beginLoad(this.state.overview.login),
      verify: beginLoad(this.state.overview.verify),
      today: beginLoad(this.state.overview.today),
      cost: beginLoad(this.state.overview.cost),
      trend: beginLoad(this.state.overview.trend),
      issues: { state: "loading" },
    });
    await Promise.all([this.readSummary(), this.loadCapabilities()]);
    if (this.disposed) return;
    const role = this.summary?.role;
    if (role !== null && role !== undefined && role !== "local") {
      this.markUnavailable(
        `后端回报 ${role} 角色：本页没有装配这个角色的 UM 读面`
      );
      return;
    }
    this.resolveRole();
  }

  private markUnavailable(reason: string): void {
    this.roleAvailable = false;
    this.roleReason = reason;
    this.resolveRole();
    const failure: Failure = { kind: "unavailable", reason, at: this.now() };
    this.patchOverview({
      service: { state: "failed", failure },
      login: { state: "failed", failure },
      verify: { state: "failed", failure },
      today: { state: "failed", failure },
      cost: { state: "failed", failure },
      trend: { state: "failed", failure },
      issues: { state: "failed", failure },
      actions: this.closedActions(reason),
      progress: {},
    });
    this.patchRequests({
      page: { state: "failed", failure },
      logs: { ...this.state.requests.logs, page: { state: "failed", failure } },
      maintenance: { state: "failed", failure },
      detail: null,
    });
    this.patchModels({
      access: { state: "failed", failure },
      catalog: { state: "failed", failure },
      refresh: { state: "failed", at: this.now(), reason },
      history: {
        ...this.state.models.history,
        records: { state: "failed", failure },
      },
      runtime: { state: "failed", failure },
      actions: this.closedModelActions(reason),
    });
  }

  private async readSummary(): Promise<void> {
    try {
      this.summary = await this.summaryClient.readSummary();
    } catch {
      this.summary = null;
    }
  }

  private async loadCapabilities(): Promise<void> {
    try {
      const capabilities = await this.client.refreshCapabilities();
      this.capabilities = new Map(
        capabilities.map((capability) => [capability.action, capability])
      );
      this.capabilityFailure = null;
    } catch (error) {
      this.capabilities = null;
      this.capabilityFailure = failureFrom(error, this.now());
    }
    if (!this.disposed) this.recomputeCapabilities();
  }

  private async whenGateSettled(): Promise<void> {
    if (this.gate.verdict().state !== "checking") return;
    await new Promise<void>((resolve) => {
      let finished = false;
      const done = () => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        off();
        resolve();
      };
      const timer = setTimeout(done, GATE_WAIT_MS);
      const off = this.gate.onChange(() => {
        if (this.gate.verdict().state !== "checking") done();
      });
    });
  }

  /** Wait for the role probe, then run `task` only when this page may read. */
  private async roleThen(task: () => void): Promise<void> {
    await this.roleReady;
    if (this.disposed || !this.roleAvailable) return;
    await this.whenGateSettled();
    if (this.disposed || !this.roleAvailable) return;
    task();
  }

  // ---------------------------------------------------------------- pages

  private onPage(
    page: "overview" | "requests" | "models",
    view: RequestsView | null
  ): void {
    if (page === "overview") this.ensureOverview(false);
    else if (page === "requests") this.ensureRequests(view ?? "records", false);
    else this.ensureModels(false);
  }

  private onRefresh(
    page: "overview" | "requests" | "models",
    view: RequestsView | null
  ): void {
    if (page === "overview") this.ensureOverview(true);
    else if (page === "requests") this.ensureRequests(view ?? "records", true);
    else this.ensureModels(true);
  }

  private ensureOverview(force: boolean): void {
    const loads: Load<unknown>[] = [
      this.state.overview.service,
      this.state.overview.login,
      this.state.overview.verify,
      this.state.overview.today,
      this.state.overview.cost,
      this.state.overview.trend,
    ];
    const unread = loads.some(
      (load) => load.state === "unwired" || load.state === "failed"
    );
    if (!force && this.overviewLoaded && !unread) return;
    this.overviewLoaded = true;
    void this.roleThen(() => {
      this.readService();
      this.readToday();
      this.readTrend();
      this.readLogin();
      void this.readOperations(force);
    });
  }

  private readService(): void {
    void this.readSection<UmServicesPayload, UmService>(
      "um-services",
      () => this.state.overview.service,
      (load) => this.patchOverview({ service: load }),
      (signal) => this.client.readServices(signal),
      (payload) => serviceFrom(payload)
    );
  }

  private readLogin(): void {
    void this.readSection<OperationResult, UmLogin>(
      "um-login",
      () => this.state.overview.login,
      (load) => this.patchOverview({ login: load }),
      (signal) =>
        this.client.runAction(
          "account.um_status",
          "account",
          {},
          {
            intent: "read",
            signal,
            timeoutMs: 30_000,
            resourceKey: "um-account-status",
          }
        ),
      (result) => loginFrom(result)
    );
  }

  private readToday(): void {
    void this.readTodayTask();
  }

  private async readTodayTask(): Promise<void> {
    const previousToday = this.state.overview.today;
    const previousCost = this.state.overview.cost;
    const token = this.nextLane("um-today");
    this.patchOverview({
      today: beginLoad(previousToday),
      cost: beginLoad(previousCost),
    });
    try {
      const outcome = await this.requests.run("um-today", (signal) =>
        this.client.readUmToday(signal)
      );
      if (outcome.state === "cancelled") {
        if (this.isCurrentLane("um-today", token))
          this.patchOverview({ today: previousToday, cost: previousCost });
        return;
      }
      if (
        outcome.state === "superseded" ||
        !this.isCurrentLane("um-today", token)
      )
        return;
      const response = outcome.value;
      const at = response.checkedAt ?? this.now();
      this.timezone = response.timezone ?? this.timezone;
      this.patchOverview({
        today: { state: "ready", data: todayFrom(response.today), at },
        cost: {
          state: "ready",
          data: costFrom(response.cost, this.summary?.umReference ?? null),
          at,
        },
      });
      this.rebuildRuntime();
      this.refreshFailedLatest();
    } catch (error) {
      if (this.disposed || !this.isCurrentLane("um-today", token)) return;
      const failure = failureFrom(error, this.now());
      this.patchOverview({
        today: failedOrStale(previousToday, failure),
        cost: failedOrStale(previousCost, failure),
      });
      this.recomputeIssues();
    }
  }

  private readTrend(): void {
    const range = this.trendRange;
    void this.readSection<UmTrend, UmTrend>(
      "um-trend",
      () => this.state.overview.trend,
      (load) => this.patchOverview({ trend: load }),
      (signal) =>
        this.client.readUmTrend(range, signal).then((response) => {
          this.timezone = response.timezone ?? this.timezone;
          return trendFrom(response.trend);
        }),
      (trend) => trend
    );
  }

  private onTrendRange(range: TrendRange): void {
    if (range === this.trendRange) return;
    this.trendRange = range;
    void this.roleReady.then(() => {
      if (this.disposed || !this.roleAvailable) return;
      this.readTrend();
    });
  }

  private refreshFailedLatest(): void {
    const today = this.state.overview.today;
    const failed =
      today.state === "ready" || today.state === "stale"
        ? readingNumber(today.data.requests.failed)
        : null;
    this.failedLatestAt = null;
    this.failedLatestUnread = false;
    if (failed === null || failed === 0) {
      this.recomputeIssues();
      return;
    }
    const query: UmRequestsReadQuery = {
      window: "24h",
      model: null,
      result: "failed",
      search: "",
    };
    void this.requests
      .run("um-failed-latest", (signal) =>
        this.client.readUmRequests(query, null, 1, signal)
      )
      .then((outcome) => {
        if (outcome.state !== "current") return;
        this.failedLatestAt = outcome.value.page.rows[0]?.at ?? null;
        this.failedLatestUnread = this.failedLatestAt === null;
        this.recomputeIssues();
      })
      .catch(() => {
        this.failedLatestUnread = true;
        this.recomputeIssues();
      });
  }

  private recomputeIssues(): void {
    const items: UmIssue[] = [];
    const service = this.state.overview.service;
    if (service.state === "failed") {
      items.push({
        kind: "service",
        title: "UM 服务状态没有读到",
        detail: service.failure.reason,
        count: 1,
        lastAt: service.failure.at,
        evidence: { view: "maintenance" },
      });
    } else if (service.state === "ready" || service.state === "stale") {
      const data = service.data;
      if (data.state !== "running") {
        items.push({
          kind: "service",
          title:
            data.state === "stopped"
              ? "UM 服务未运行"
              : data.state === "degraded"
                ? "UM 服务降级"
                : "UM 服务状态未知",
          detail: data.detail || data.word,
          count: 1,
          lastAt: data.checkedAt ?? service.at,
          evidence: { view: "maintenance" },
        });
      }
    }
    const login = this.state.overview.login;
    if (login.state === "ready" || login.state === "stale") {
      const data = login.data;
      if (data.state === "needs_login" || data.state === "invalid") {
        items.push({
          kind: "login",
          title: data.state === "invalid" ? "UM 登录已失效" : "需要重新登录",
          detail: data.detail,
          count: 1,
          lastAt: data.checkedAt ?? login.at,
          evidence: { view: "maintenance" },
        });
      }
    }
    const today = this.state.overview.today;
    if (today.state === "ready" || today.state === "stale") {
      const failed = readingNumber(today.data.requests.failed);
      if (failed !== null && failed > 0) {
        const latest =
          this.failedLatestAt !== null
            ? `最近一次失败在 ${when(this.failedLatestAt)}`
            : this.failedLatestUnread
              ? "最近一次失败时间没有读到"
              : "最近一次失败时间未记录";
        items.push({
          kind: "request_failed",
          title: "请求失败",
          detail: `今天账本记录了 ${failed} 次失败（含截断/上游失败）；重试不重复计请求。${latest}`,
          count: failed,
          lastAt: this.failedLatestAt ?? today.at,
          evidence: {
            view: "records",
            query: { window: "24h", result: "failed", search: "" },
          },
        });
      }
    }
    const cost = this.state.overview.cost;
    if (cost.state === "ready" || cost.state === "stale") {
      const data = cost.data;
      if (data.price.state === "failed" || data.price.state === "unsynced") {
        items.push({
          kind: "price",
          title:
            data.price.state === "failed" ? "价格同步失败" : "价格尚未同步",
          detail:
            data.price.failure ??
            "参考金额只按已定价部分计算；未同步不是零费用",
          count: 1,
          lastAt: cost.at,
          evidence: null,
        });
      } else if (data.unpricedRequests !== null && data.unpricedRequests > 0) {
        items.push({
          kind: "price",
          title: "有请求未定价",
          detail: `${data.unpricedRequests} 个请求未定价，未计入参考金额`,
          count: data.unpricedRequests,
          lastAt: cost.at,
          evidence: null,
        });
      }
    }
    const refresh = this.state.models.refresh;
    if (refresh.state === "failed") {
      items.push({
        kind: "catalog",
        title: "目录刷新失败",
        detail: refresh.reason,
        count: 1,
        lastAt: refresh.at,
        evidence: null,
      });
    }
    const run = this.run;
    if (run && run.state === "finished") {
      const failed = run.items.filter((item) => item.state === "failed").length;
      if (failed > 0) {
        items.push({
          kind: "test",
          title: "模型测试未通过",
          detail:
            run.note ??
            `最近一次${run.kind === "speedtest" ? "测速" : "上下文探测"}有 ${failed} 个模型失败`,
          count: failed,
          lastAt: run.startedAt,
          evidence: null,
        });
      }
    }
    items.sort((a, b) => b.lastAt - a.lastAt);
    this.patchOverview({
      issues: {
        state: "ready",
        at: this.now(),
        data: { window: "今天（北京时间）与当前服务状态", items },
      },
    });
  }

  // ---------------------------------------------------------------- operations

  private readOperations(force: boolean): Promise<void> {
    if (!force && this.operationsAt !== null) return Promise.resolve();
    if (this.operationsPromise) return this.operationsPromise;
    const token = this.nextLane("um-operations");
    const previousVerify = this.state.overview.verify;
    const previousHistory = this.state.models.history.records;
    this.patchOverview({ verify: beginLoad(previousVerify) });
    this.patchModels({
      history: {
        ...this.state.models.history,
        records: beginLoad(previousHistory),
      },
    });
    const task = this.doReadOperations(token, previousVerify, previousHistory);
    this.operationsPromise = task;
    return task;
  }

  private async doReadOperations(
    token: number,
    previousVerify: Load<UmVerify>,
    previousHistory: Load<UmTestRecord[]>
  ): Promise<void> {
    try {
      const outcome = await this.requests.run("um-operations", (signal) =>
        this.client.readOperations(100)
      );
      if (outcome.state === "cancelled") {
        if (this.isCurrentLane("um-operations", token)) {
          this.patchOverview({ verify: previousVerify });
          this.patchModels({
            history: { ...this.state.models.history, records: previousHistory },
          });
        }
        return;
      }
      if (
        outcome.state === "superseded" ||
        !this.isCurrentLane("um-operations", token)
      )
        return;
      this.operations = outcome.value.operations;
      this.operationsAt = this.now();
      this.operationsFailure = null;
      this.applyOperations();
    } catch (error) {
      if (this.disposed || !this.isCurrentLane("um-operations", token)) return;
      const failure = failureFrom(error, this.now());
      this.operationsFailure = failure;
      this.patchOverview({ verify: failedOrStale(previousVerify, failure) });
      this.patchModels({
        history: {
          ...this.state.models.history,
          records: failedOrStale(previousHistory, failure),
        },
      });
      this.recomputeIssues();
    } finally {
      this.operationsPromise = null;
    }
  }

  private applyOperations(): void {
    const at = this.operationsAt ?? this.now();
    this.patchOverview({ verify: this.verifyFrom(this.operations, at) });
    this.applyCatalogRows();
    const records = this.testRecords(this.state.models.history.filter);
    this.patchModels({
      history: {
        ...this.state.models.history,
        records: { state: "ready", data: records, at },
      },
    });
    // A page reload must not pretend an in-flight test vanished: the operation
    // record is the authority, and this page never rebuilds its per-model
    // progress. Say so instead of showing an empty "no test" page.
    const active =
      this.run === null
        ? this.operations.find(
            (op) =>
              (op.action === "model.speedtest" ||
                op.action === "model.context_probe_run") &&
              (op.state === "running" || op.state === "queued")
          )
        : undefined;
    if (active && active.id !== this.reconnectNoticeFor) {
      this.reconnectNoticeFor = active.id;
      this.notice(
        "warn",
        `操作记录里还有一次${active.action === "model.speedtest" ? "批量测速" : "上下文探测"}（${active.id}）没有结束；` +
          "本页不重建它的进度，结束后可在历史里看到结果，也不会自动重放。"
      );
    }
    this.recomputeIssues();
    this.recomputeCapabilities();
  }

  private verifyFrom(
    operations: UmOperationPayload[],
    at: number
  ): Load<UmVerify> {
    const op = operations.find(
      (candidate) =>
        candidate.action === "account.um_verify" &&
        TERMINAL.has(candidate.state)
    );
    if (!op) return { state: "ready", data: null, at };
    const result = op.result;
    const verify = rowText(result, "verify");
    const note = rowText(result, "note");
    const reason = rowText(result, "reason");
    const stamp = op.finishedAt ?? op.createdAt ?? at;
    if (verify === "valid") {
      return {
        state: "ready",
        at,
        data: {
          result: "passed",
          word: "通过",
          detail: note ?? "上游确认当前登录有效（未调用模型）",
          at: stamp,
        },
      };
    }
    if (verify === "rejected" || verify === "needs_login") {
      return {
        state: "ready",
        at,
        data: {
          result: "failed",
          word: "未通过",
          detail: reason ?? note ?? `上游核验结果：${verify}`,
          at: stamp,
        },
      };
    }
    if (op.state === "failed") {
      return {
        state: "ready",
        at,
        data: {
          result: "unknown",
          word: "结果未知",
          detail: failureFromOperationPayload(op, at).reason,
          at: stamp,
        },
      };
    }
    if (op.state === "cancelled") {
      return {
        state: "ready",
        at,
        data: {
          result: "unknown",
          word: "已取消",
          detail: "这次核验被取消，结果未知",
          at: stamp,
        },
      };
    }
    return {
      state: "ready",
      at,
      data: {
        result: "unknown",
        word: verify === null ? "结果未知" : `核验结果：${verify}`,
        detail: note ?? reason ?? "核验没有给出可用结论",
        at: stamp,
      },
    };
  }

  private applyHistoryFilter(filter: HistoryFilter): void {
    this.patchModels({
      history: { filter, records: this.historyLoad(filter) },
    });
  }

  private historyLoad(filter: HistoryFilter): Load<UmTestRecord[]> {
    if (this.operationsAt === null) {
      return this.operationsFailure
        ? { state: "failed", failure: this.operationsFailure }
        : { state: "loading" };
    }
    return {
      state: "ready",
      data: this.testRecords(filter),
      at: this.operationsAt,
    };
  }

  private testRecords(filter: HistoryFilter): UmTestRecord[] {
    const all: UmTestRecord[] = [];
    for (const op of this.operations) {
      if (!TERMINAL.has(op.state)) continue;
      const kind: TestKind | null =
        op.action === "model.speedtest"
          ? "speedtest"
          : op.action === "model.context_probe_run"
            ? "context"
            : null;
      if (kind === null) continue;
      const entries =
        kind === "speedtest" ? speedtestEntries(op) : contextEntries(op);
      for (const entry of entries) {
        all.push({
          key: `${op.id}|${entry.model}`,
          model: entry.model,
          kind,
          at: op.finishedAt ?? op.createdAt ?? this.operationsAt ?? this.now(),
          result: entry.state,
          summary: entry.summary,
          archived: false,
        });
      }
    }
    all.sort((a, b) => b.at - a.at || a.key.localeCompare(b.key));
    const newest = new Map<string, string>();
    for (const record of all) {
      const key = `${record.kind}|${record.model}`;
      if (!newest.has(key)) newest.set(key, record.key);
    }
    for (const record of all)
      if (newest.get(`${record.kind}|${record.model}`) !== record.key)
        record.archived = true;
    return all.filter(
      (record) =>
        (filter.model === null || record.model === filter.model) &&
        (filter.kind === null || record.kind === filter.kind)
    );
  }

  // ---------------------------------------------------------------- requests

  private ensureRequests(view: RequestsView, force: boolean): void {
    const loaded =
      view === "records"
        ? this.requestsLoadedFor === queryKey(this.state.requests.query)
        : view === "logs"
          ? this.logsLoadedFor === logQueryKey(this.state.requests.logs.query)
          : this.maintenanceLoaded;
    if (!force && loaded) return;
    void this.roleThen(() => {
      if (view === "records") this.readRequestsPage();
      else if (view === "logs") this.readLogsPage();
      else this.readMaintenance();
    });
  }

  private onRequestsQuery(query: RequestQuery): void {
    if (sameQuery(query, this.state.requests.query)) return;
    this.patchRequests({ query, page: { state: "loading" }, detail: null });
    this.requestsLoadedFor = null;
    void this.roleThen(() => this.readRequestsPage());
  }

  private readRequestsPage(): void {
    const query = this.state.requests.query;
    const key = queryKey(query);
    void this.readSection(
      "um-requests",
      () => this.state.requests.page,
      (load) => this.patchRequests({ page: load }),
      (signal) => this.client.readUmRequests(query, null, 50, signal),
      (response) => response.page,
      (response) => {
        if (queryKey(this.state.requests.query) !== key) return;
        this.requestsLoadedFor = key;
        this.mergeRequestModels(response.models);
      }
    );
  }

  private readEarlierRequests(cursor: string): void {
    const query = this.state.requests.query;
    const key = queryKey(query);
    const page = this.state.requests.page;
    if (page.state !== "ready" && page.state !== "stale") return;
    const rows = page.data.rows;
    const token = this.nextLane("um-requests-earlier");
    this.patchRequests({ page: { ...page, refreshing: true } });
    void this.requests
      .run("um-requests-earlier", (signal) =>
        this.client.readUmRequests(query, cursor, 50, signal)
      )
      .then((outcome) => {
        if (
          outcome.state !== "current" ||
          !this.isCurrentLane("um-requests-earlier", token)
        )
          return;
        if (queryKey(this.state.requests.query) !== key) return;
        const next = outcome.value.page;
        this.patchRequests({
          page: {
            state: "ready",
            at: this.now(),
            data: {
              rows: mergeRows(rows, next.rows),
              scope: next.scope,
              earlier: next.earlier,
            },
          },
        });
        this.mergeRequestModels(outcome.value.models);
      })
      .catch((error) => {
        if (!this.isCurrentLane("um-requests-earlier", token)) return;
        if (queryKey(this.state.requests.query) !== key) return;
        this.patchRequests({
          page: failedOrStale(page, failureFrom(error, this.now())),
        });
      });
  }

  private mergeRequestModels(models: string[]): void {
    if (!models.length) return;
    const merged = [...new Set([...this.state.requests.models, ...models])];
    this.patchRequests({ models: merged });
  }

  private readRequestDetail(key: string): void {
    const page = this.state.requests.page;
    const row =
      page.state === "ready" || page.state === "stale"
        ? page.data.rows.find((candidate) => candidate.key === key)
        : null;
    if (!row || !row.requestId) {
      this.patchRequests({
        detail: {
          key,
          logs: {
            state: "failed",
            failure: {
              kind: "error",
              reason:
                "这条记录没有可用的 request_id，无法可靠地对上日志；不按时间就近猜测",
              at: this.now(),
            },
          },
        },
      });
      return;
    }
    const requestId = row.requestId;
    const token = this.nextLane("um-request-detail");
    this.patchRequests({ detail: { key, logs: { state: "loading" } } });
    void this.requests
      .run("um-request-detail", (signal) =>
        this.client.readUmRequestLogs(requestId, 100, signal)
      )
      .then((outcome) => {
        if (outcome.state !== "current") return;
        if (!this.isCurrentLane("um-request-detail", token)) return;
        if (this.state.requests.detail?.key !== key) return;
        const payload = outcome.value;
        const at = this.now();
        this.patchRequests({
          detail: {
            key,
            logs: payload.partial
              ? {
                  state: "stale",
                  data: payload.lines,
                  at,
                  failure: {
                    kind: "error",
                    reason: "部分日志分类读取失败，只显示已读到的行",
                    at,
                  },
                }
              : { state: "ready", data: payload.lines, at },
          },
        });
      })
      .catch((error) => {
        if (!this.isCurrentLane("um-request-detail", token)) return;
        if (this.state.requests.detail?.key !== key) return;
        this.patchRequests({
          detail: {
            key,
            logs: { state: "failed", failure: failureFrom(error, this.now()) },
          },
        });
      });
  }

  private onLogsQuery(query: LogQuery): void {
    if (sameLogQuery(query, this.state.requests.logs.query)) return;
    this.patchRequests({ logs: { query, page: { state: "loading" } } });
    this.logsLoadedFor = null;
    void this.roleThen(() => this.readLogsPage());
  }

  private readLogsPage(): void {
    const query = this.state.requests.logs.query;
    const key = logQueryKey(query);
    void this.readSection(
      "um-logs",
      () => this.state.requests.logs.page,
      (load) =>
        this.patchRequests({
          logs: { ...this.state.requests.logs, page: load },
        }),
      (signal) => this.client.readUmLogs(query, null, 80, signal),
      (payload) => payload,
      (payload) => {
        if (logQueryKey(this.state.requests.logs.query) !== key) return;
        this.logsLoadedFor = key;
        this.mergeLogSources(payload.sources);
      }
    );
  }

  private mergeLogSources(sources: string[]): void {
    if (!sources.length) return;
    const page = this.state.requests.logs.page;
    if (page.state !== "ready" && page.state !== "stale") return;
    const merged = [...new Set([...page.data.sources, ...sources])];
    this.patchRequests({
      logs: {
        ...this.state.requests.logs,
        page: { ...page, data: { ...page.data, sources: merged } },
      },
    });
  }

  private readEarlierLogs(cursor: string): void {
    const query = this.state.requests.logs.query;
    const key = logQueryKey(query);
    const page = this.state.requests.logs.page;
    if (page.state !== "ready" && page.state !== "stale") return;
    const lines = page.data.lines;
    const token = this.nextLane("um-logs-earlier");
    this.patchRequests({
      logs: {
        ...this.state.requests.logs,
        page: { ...page, refreshing: true },
      },
    });
    void this.requests
      .run("um-logs-earlier", (signal) =>
        this.client.readUmLogs(query, cursor, 80, signal)
      )
      .then((outcome) => {
        if (outcome.state !== "current") return;
        if (!this.isCurrentLane("um-logs-earlier", token)) return;
        if (logQueryKey(this.state.requests.logs.query) !== key) return;
        const payload = outcome.value;
        this.patchRequests({
          logs: {
            ...this.state.requests.logs,
            page: {
              state: "ready",
              at: this.now(),
              data: {
                lines: mergeLines(lines, payload.lines),
                sources: payload.sources,
                scope: payload.scope,
                earlier: payload.earlier,
              },
            },
          },
        });
      })
      .catch((error) => {
        if (!this.isCurrentLane("um-logs-earlier", token)) return;
        if (logQueryKey(this.state.requests.logs.query) !== key) return;
        this.patchRequests({
          logs: {
            ...this.state.requests.logs,
            page: failedOrStale(page, failureFrom(error, this.now())),
          },
        });
      });
  }

  private readMaintenance(force = false): void {
    if (!force && this.maintenanceLoaded) return;
    void this.readSection(
      "um-maintenance",
      () => this.state.requests.maintenance,
      (load) => this.patchRequests({ maintenance: load }),
      (signal) => this.client.readUmMaintenance(50, signal),
      (payload) => maintenanceFrom(payload),
      () => {
        this.maintenanceLoaded = true;
      }
    );
  }

  // ---------------------------------------------------------------- models

  private ensureModels(force: boolean): void {
    const loads: Load<unknown>[] = [
      this.state.models.access,
      this.state.models.catalog,
      this.state.models.history.records,
      this.state.models.runtime,
    ];
    const unread = loads.some(
      (load) => load.state === "unwired" || load.state === "failed"
    );
    if (!force && this.modelsLoaded && !unread) return;
    this.modelsLoaded = true;
    void this.roleThen(() => {
      this.patchModels({
        access: beginLoad(this.state.models.access),
        catalog: beginLoad(this.state.models.catalog),
        runtime: beginLoad(this.state.models.runtime),
      });
      this.readAccess();
      void this.readCatalog(force);
      this.readEvidence();
      void this.readOperations(force);
    });
  }

  private readAccess(): void {
    void this.readSection<ReadResult<GatewayConnection>, UmAccess>(
      "um-access",
      () => this.state.models.access,
      (load) => this.patchModels({ access: load }),
      (signal) => this.client.readGatewayConnection(CHAT2API, signal),
      accessFrom
    );
  }

  private async readCatalog(force: boolean): Promise<void> {
    if (!force && this.catalogFetchedAt !== null) return;
    // A strict refresh owns the catalog while it is in flight: a plain read
    // would invalidate its generation and leave the refresh spinner stuck.
    if (this.state.models.refresh.state === "refreshing") return;
    const generation = ++this.catalogGeneration;
    const token = this.nextLane("um-catalog");
    this.patchModels({ catalog: beginLoad(this.state.models.catalog) });
    try {
      const outcome = await this.requests.run("um-catalog", (signal) =>
        this.client.readUmCatalog(CATALOG_PLAIN_SOURCE, signal)
      );
      if (outcome.state === "cancelled") {
        if (this.isCurrentLane("um-catalog", token))
          this.patchModels({ catalog: this.state.models.catalog });
        return;
      }
      if (
        outcome.state === "superseded" ||
        !this.isCurrentLane("um-catalog", token)
      )
        return;
      if (generation !== this.catalogGeneration) return;
      this.catalog = outcome.value;
      this.catalogSource = CATALOG_PLAIN_SOURCE;
      this.catalogFetchedAt = outcome.value.checkedAt ?? this.now();
      this.catalogStale = null;
      this.applyCatalogRows();
    } catch (error) {
      if (this.disposed || !this.isCurrentLane("um-catalog", token)) return;
      if (generation !== this.catalogGeneration) return;
      const failure = failureFrom(error, this.now());
      if (this.catalog !== null) {
        this.catalogStale = failure;
        this.applyCatalogRows();
      } else {
        this.patchModels({ catalog: { state: "failed", failure } });
      }
      this.recomputeCapabilities();
    }
  }

  private readEvidence(): void {
    void this.requests
      .run("um-evidence", (signal) => this.client.readContextEvidence(signal))
      .then((outcome) => {
        if (outcome.state !== "current" || this.disposed) return;
        this.evidence = outcome.value;
        this.evidenceAt = this.now();
        this.applyCatalogRows();
      })
      .catch(() => {
        /* evidence is additive: rows keep null (unknown) */
      });
  }

  private applyCatalogRows(): void {
    const payload = this.catalog;
    if (payload === null) return;
    const at = this.catalogFetchedAt ?? this.now();
    const evidence = new Map<string, ContextEvidence>();
    for (const row of this.evidence)
      evidence.set(`${row.source}|${row.modelId}`, row);
    const rows: UmModelRow[] = payload.models.map((model) => {
      const found = evidence.get(`${UM_SOURCE}|${model.id}`);
      const contextAt = this.evidenceAt ?? at;
      return {
        id: model.id,
        name: model.displayName ?? model.id,
        context: {
          declared:
            model.declaredContext !== null
              ? { tokens: model.declaredContext, source: this.catalogSource }
              : null,
          compatDefault: null,
          probedChars:
            found && found.observedCharacterFloor !== null
              ? { chars: found.observedCharacterFloor, at: contextAt }
              : null,
          tokenLowerBound:
            found && found.verifiedTokenFloor !== null
              ? { tokens: found.verifiedTokenFloor, at: contextAt }
              : null,
        },
        thinking: {
          levels: model.efforts,
          source: thinkingSource(model.effortsSource),
          defaultLevel: model.defaultEffort,
        },
        lastSpeed: this.lastTest(model.id, "speedtest"),
        lastContext: this.lastTest(model.id, "context"),
      };
    });
    const data: UmCatalog = {
      models: rows,
      fetchedAt: at,
      source: this.catalogSource,
    };
    this.patchModels({
      catalog: this.catalogStale
        ? { state: "stale", data, at, failure: this.catalogStale }
        : { state: "ready", data, at },
    });
    this.rebuildRuntime();
    this.recomputeCapabilities();
  }

  private lastTest(
    model: string,
    kind: TestKind
  ): { at: number; ok: boolean; summary: string } | null {
    for (const op of this.operations) {
      if (!TERMINAL.has(op.state)) continue;
      if (
        op.action !==
        (kind === "speedtest" ? "model.speedtest" : "model.context_probe_run")
      )
        continue;
      const entry = (
        kind === "speedtest" ? speedtestEntries(op) : contextEntries(op)
      ).find((candidate) => candidate.model === model);
      if (entry) {
        return {
          at: op.finishedAt ?? op.createdAt ?? this.operationsAt ?? this.now(),
          ok: entry.state === "done" || entry.state === "partial",
          summary: entry.summary,
        };
      }
    }
    return null;
  }

  private rebuildRuntime(): void {
    const facts: { label: string; value: string }[] = [];
    const service = this.state.overview.service;
    if (service.state === "ready" || service.state === "stale") {
      if (service.data.port !== null)
        facts.push({
          label: "服务端口",
          value: `${service.data.port}（chat2api 主链）`,
        });
    }
    const access = this.state.models.access;
    if (access.state === "ready" || access.state === "stale") {
      if (access.data.baseUrl)
        facts.push({ label: "UM 接口地址", value: access.data.baseUrl });
    }
    const reference = this.summary?.umReference;
    if (reference) {
      const price = reference.price;
      facts.push({
        label: "参考价格来源",
        value:
          price.state === "synced"
            ? `${price.source ?? "Magpie"}${price.version ? ` · 版本 ${price.version}` : ""}`
            : price.state === "unsynced"
              ? "价格尚未同步（不是零费用）"
              : "价格同步失败，沿用上次快照",
      });
      facts.push({
        label: "参考价快照",
        value:
          price.snapshotAt !== null
            ? `${when(price.snapshotAt)}${price.failure ? ` · ${price.failure}` : ""}`
            : "还没有成功的价格快照",
      });
      facts.push({
        label: "汇率",
        value:
          reference.fx.rate !== null
            ? `1 美元 = ${reference.fx.rate} 元${reference.fx.version ? ` · 版本 ${reference.fx.version}` : ""}`
            : "汇率未就绪，金额不完整",
      });
    }
    if (this.timezone !== null)
      facts.push({
        label: "账本时区",
        value: `${this.timezone}（后端报告的账本时区）`,
      });
    facts.push({
      label: "测速",
      value: "每个所选模型 1 次短请求，不自动重试（后端契约）",
    });
    facts.push({
      label: "上下文探测预算",
      value: "整批最多 20 次请求，每次输出最多 64 Token（后端契约）",
    });
    this.patchModels({
      runtime: { state: "ready", data: facts, at: this.now() },
    });
  }

  private async refreshCatalog(): Promise<void> {
    if (this.state.models.refresh.state === "refreshing") return;
    const gate = this.actionEnabled("model.catalog");
    if (!gate.ok) {
      this.notice("warn", `刷新 UM 模型列表暂不可用：${gate.reason}`);
      return;
    }
    this.patchModels({ refresh: { state: "refreshing", since: this.now() } });
    this.recomputeCapabilities();
    const generation = ++this.catalogGeneration;
    try {
      const result =
        await this.client.readUmCatalogRefresh(CATALOG_FRESH_SOURCE);
      if (this.disposed || generation !== this.catalogGeneration) return;
      if (!result.success || !result.fresh) {
        const reason =
          result.error ??
          `后端没有确认严格取新（status=${result.status ?? "unknown"}）`;
        this.patchModels({
          refresh: { state: "failed", at: this.now(), reason },
        });
        const failure: Failure = {
          kind: catalogFailureKind(result.status),
          reason,
          at: this.now(),
        };
        if (this.catalog !== null) {
          this.catalogStale = failure;
          this.applyCatalogRows();
        } else {
          this.patchModels({ catalog: { state: "failed", failure } });
        }
        this.recomputeIssues();
        this.recomputeCapabilities();
        return;
      }
      this.catalog = result.catalog;
      this.catalogSource = CATALOG_FRESH_SOURCE;
      this.catalogFetchedAt = result.catalog.checkedAt ?? this.now();
      this.catalogStale = null;
      this.applyCatalogRows();
      this.patchModels({ refresh: { state: "succeeded", at: this.now() } });
      this.recomputeIssues();
      this.recomputeCapabilities();
    } catch (error) {
      if (this.disposed) return;
      const failure = failureFrom(error, this.now());
      this.patchModels({
        refresh: { state: "failed", at: failure.at, reason: failure.reason },
      });
      if (this.catalog !== null) {
        this.catalogStale = failure;
        this.applyCatalogRows();
      } else {
        this.patchModels({ catalog: { state: "failed", failure } });
      }
      this.recomputeIssues();
      this.recomputeCapabilities();
    }
  }

  // ---------------------------------------------------------------- tests

  private startTest(kind: TestKind, requested: string[]): void {
    if (this.runActive()) {
      this.notice("warn", "已有测试在进行，这次点击没有发起新测试");
      return;
    }
    const ids = [
      ...new Set(requested.map((model) => model.trim()).filter(Boolean)),
    ];
    if (!ids.length) {
      this.notice("warn", "没有选择模型：未选模型不测试");
      return;
    }
    if (ids.length > MAX_BATCH_MODELS) {
      this.notice(
        "err",
        `批量测试最多 ${MAX_BATCH_MODELS} 个模型（已选 ${ids.length} 个），这次没有发起测试`
      );
      return;
    }
    const catalog = new Set(this.catalogIds());
    const models = ids.filter((model) => catalog.has(model));
    if (!models.length) {
      this.notice("warn", "所选模型已不在当前目录里，这次没有发起测试");
      return;
    }
    const gate = this.actionEnabled(
      kind === "speedtest" ? "model.speedtest" : "model.context_probe_run"
    );
    if (!gate.ok) {
      this.notice("err", `无法发起测试：${gate.reason}`);
      return;
    }
    this.run = {
      id: `local-${this.now()}`,
      kind,
      startedAt: this.now(),
      state: "submitting",
      stopRequested: false,
      budget: kind === "context" ? { used: 0, limit: MAX_BATCH_MODELS } : null,
      items: models.map((model) => ({
        model,
        state: "waiting",
        stage: null,
        summary: null,
        detail: null,
      })),
      note: null,
    };
    this.runOperation = null;
    this.cancelSentFor = null;
    this.reconnectNoticeFor = null;
    this.lastProgressReadAt = 0;
    this.patchModels({ run: this.run, notice: null });
    if (models.length !== ids.length) {
      this.notice(
        "warn",
        `${ids.length - models.length} 个所选模型已不在当前目录里，已跳过`
      );
    }
    this.recomputeCapabilities();
    void this.runTest(models);
  }

  private async runTest(models: string[]): Promise<void> {
    const run = this.run;
    if (!run) return;
    const params: JsonObject =
      run.kind === "speedtest"
        ? { source: UM_SOURCE, models: JSON.stringify(models) }
        : { models: { [UM_SOURCE]: models } };
    try {
      const result = await this.client.runAction(
        run.kind === "speedtest"
          ? "model.speedtest"
          : "model.context_probe_run",
        "model",
        params,
        {
          intent: "probe",
          timeoutMs: run.kind === "speedtest" ? 660_000 : 1_200_000,
          pollIntervalMs: 1_500,
          onProgress: (progress) => {
            if (this.run?.id !== run.id) return;
            this.runOperation = progress;
            if (run.state === "submitting") {
              run.state = "running";
              this.publishRun();
              this.recomputeCapabilities();
            }
            if (run.stopRequested && this.cancelSentFor !== run.id) {
              this.cancelSentFor = run.id;
              void this.cancelRun(run.id);
            }
            void this.readProgress(run, progress);
          },
        }
      );
      if (this.run?.id !== run.id) return;
      this.finishRun(run, result);
    } catch (error) {
      if (this.run?.id !== run.id) return;
      if (error instanceof ActionBusyError) {
        this.run = null;
        this.runOperation = null;
        this.notice("warn", error.message);
        this.recomputeCapabilities();
        return;
      }
      this.finishWithoutResult(run, messageOf(error));
    }
  }

  private async cancelRun(runId: string): Promise<void> {
    const run = this.run;
    if (!run || run.id !== runId || run.state === "finished") return;
    if (!run.stopRequested) {
      run.stopRequested = true;
      this.publishRun();
      this.recomputeCapabilities();
    }
    const operation = this.runOperation;
    if (!operation) return;
    this.cancelSentFor = run.id;
    try {
      await this.client.cancelOperation(operation);
      if (this.run?.id === runId) {
        run.note =
          "已请求停止：正在进行的请求可能仍会完成，之后的模型不再开始；已完成的结果保留";
        this.publishRun();
      }
    } catch (error) {
      if (this.run?.id === runId)
        this.notice("warn", `停止请求没有成功：${messageOf(error)}`);
    }
  }

  private async readProgress(
    run: UmTestRun,
    progress: Operation
  ): Promise<void> {
    if (this.run?.id !== run.id) return;
    const now = this.now();
    if (now - this.lastProgressReadAt < 2_000) return;
    this.lastProgressReadAt = now;
    try {
      const payload = await this.client.readOperations(20);
      if (this.run?.id !== run.id || run.state === "finished") return;
      const op = payload.operations.find(
        (candidate) => candidate.id === progress.id
      );
      if (op) this.applySteps(run, op);
    } catch {
      /* progress is optional; the run keeps its last stage */
    }
  }

  private applySteps(run: UmTestRun, op: UmOperationPayload): void {
    if (run.kind === "speedtest") {
      for (const step of op.steps) {
        const match = step.note.match(
          /^批量测速 \d+\/\d+：(.+) → (ok|failed|timeout)$/
        );
        if (!match) continue;
        const item = run.items.find(
          (candidate) => candidate.model === match[1]
        );
        if (!item) continue;
        item.state = match[2] === "ok" ? "done" : "failed";
        item.stage = null;
        item.summary = `测速结果：${match[2]}`;
        const next = run.items[run.items.indexOf(item) + 1];
        if (next && next.state === "waiting") {
          next.state = "running";
          next.stage = "已发出 1 次短请求，等待回复…";
        }
      }
    } else {
      const seen = new Map<string, number>();
      for (const step of op.steps) {
        const match = step.note.match(/^上下文探测 (.+?)：(\w+)，请求 (\d+)$/);
        if (!match) continue;
        const key = match[1];
        const model = key.includes("|") ? key.slice(key.indexOf("|") + 1) : key;
        const item = run.items.find((candidate) => candidate.model === model);
        if (!item) continue;
        item.state = match[2] === "verified" ? "done" : "partial";
        item.stage = null;
        item.summary = `上下文探测：${match[2]}（请求 ${match[3]} 次）`;
        seen.set(model, Number(match[3]));
      }
      if (run.budget) {
        let used = 0;
        for (const count of seen.values()) used += count;
        if (used > 0) run.budget.used = used;
      }
    }
    this.publishRun();
  }

  private finishRun(run: UmTestRun, result: OperationResult): void {
    const entries = resultEntries(run.kind, result);
    const byModel = new Map(entries.map((entry) => [entry.model, entry]));
    for (const item of run.items) {
      const entry = byModel.get(item.model);
      if (entry) {
        item.state = entry.state;
        item.stage = null;
        item.summary = entry.summary;
        item.detail = entry.detail;
      } else if (item.state === "waiting" || item.state === "running") {
        item.state = "not_run";
        item.stage = null;
        item.summary =
          result.state === "cancelled"
            ? "已请求停止，没有开始"
            : "这次没有返回结果";
      }
    }
    run.state = "finished";
    run.stopRequested = result.state === "cancelled" || run.stopRequested;
    run.note = runNote(run.kind, result);
    const budget = resultBudget(result);
    if (run.budget && budget) run.budget = budget;
    this.runOperation = null;
    this.publishRun();
    this.recomputeCapabilities();
    void this.readOperations(true);
    if (run.kind === "context") this.readEvidence();
  }

  private finishWithoutResult(run: UmTestRun, reason: string): void {
    for (const item of run.items) {
      if (item.state === "waiting" || item.state === "running") {
        item.state = "not_run";
        item.stage = null;
        item.summary = "没有拿到测试结果";
      }
    }
    run.state = "finished";
    run.note = reason;
    this.runOperation = null;
    this.publishRun();
    this.recomputeCapabilities();
  }

  private publishRun(): void {
    const run = this.run;
    this.patchModels({
      run: run
        ? {
            ...run,
            budget: run.budget ? { ...run.budget } : null,
            items: run.items.map((item) => ({ ...item })),
          }
        : null,
    });
  }

  // ---------------------------------------------------------------- maintenance

  private async runMaintenance(action: MaintenanceAction): Promise<void> {
    if (this.state.overview.progress[action]?.state === "pending") return;
    const gate = this.actionEnabled(ACTION[action]);
    if (!gate.ok) {
      this.finishProgress(action, "failed", gate.reason);
      return;
    }
    this.patchOverview({
      progress: {
        ...this.state.overview.progress,
        [action]: {
          state: "pending",
          since: this.now(),
          note: ACTION_PENDING_NOTE[action],
        },
      },
    });
    this.recomputeCapabilities();
    try {
      const result = await this.client.runAction(
        ACTION[action],
        action === "verify" || action === "login" ? "account" : CHAT2API,
        {},
        {
          intent: ACTION_INTENT[action],
          timeoutMs: ACTION_TIMEOUT[action],
          pollIntervalMs: 1_200,
        }
      );
      if (this.disposed) return;
      if (action === "verify") this.applyVerifyResult(result);
      this.finishProgress(
        action,
        progressResult(result),
        operationNote(result)
      );
      this.afterMaintenance(action);
    } catch (error) {
      if (this.disposed) return;
      this.finishProgress(action, "failed", messageOf(error));
    }
  }

  private applyVerifyResult(result: OperationResult): void {
    const at = this.now();
    if (result.state === "succeeded" && result.operation?.result) {
      const raw = result.operation.result;
      const verify = rowText(raw, "verify");
      const note = rowText(raw, "note");
      const reason = rowText(raw, "reason");
      const parsed = result.operation.finishedAt
        ? Date.parse(result.operation.finishedAt)
        : NaN;
      const stamp = Number.isFinite(parsed) ? parsed : at;
      if (verify === "valid") {
        this.patchOverview({
          verify: {
            state: "ready",
            at,
            data: {
              result: "passed",
              word: "通过",
              detail: note ?? "上游确认当前登录有效（未调用模型）",
              at: stamp,
            },
          },
        });
      } else if (verify === "rejected" || verify === "needs_login") {
        this.patchOverview({
          verify: {
            state: "ready",
            at,
            data: {
              result: "failed",
              word: "未通过",
              detail: reason ?? note ?? `上游核验结果：${verify}`,
              at: stamp,
            },
          },
        });
      } else {
        this.patchOverview({
          verify: {
            state: "ready",
            at,
            data: {
              result: "unknown",
              word: "结果未知",
              detail: note ?? reason ?? "核验没有给出可用结论",
              at: stamp,
            },
          },
        });
      }
      return;
    }
    const failure = operationFailure(result, at);
    this.patchOverview({
      verify: {
        state: "ready",
        at,
        data: {
          result: "unknown",
          word: "结果未知",
          detail: failure.reason,
          at,
        },
      },
    });
  }

  private finishProgress(
    action: MaintenanceAction,
    result: Extract<ActionProgress, { state: "done" }>["result"],
    note: string
  ): void {
    this.patchOverview({
      progress: {
        ...this.state.overview.progress,
        [action]: { state: "done", result, at: this.now(), note },
      },
    });
    this.recomputeCapabilities();
  }

  private afterMaintenance(action: MaintenanceAction): void {
    if (action === "verify") {
      void this.readOperations(true);
      return;
    }
    this.readService();
    this.readLogin();
    void this.readOperations(true);
  }

  // ---------------------------------------------------------------- capabilities

  private catalogIds(): string[] {
    const catalog = this.state.models.catalog;
    if (catalog.state !== "ready" && catalog.state !== "stale") return [];
    return [...new Set(catalog.data.models.map((model) => model.id))];
  }

  private runActive(): boolean {
    return this.run !== null && this.run.state !== "finished";
  }

  private cap(action: string): ActionCapability | null {
    return this.capabilities?.get(action) ?? null;
  }

  private actionEnabled(action: string): Gate {
    if (!this.roleAvailable) return { ok: false, reason: this.roleReason };
    const verdict = this.gate.verdict();
    if (verdict.state !== "confirmed")
      return { ok: false, reason: `后端角色未确认：${verdict.reason}` };
    const capability = this.cap(action);
    if (!capability) {
      return {
        ok: false,
        reason: this.capabilityFailure
          ? `能力表没有读到：${this.capabilityFailure.reason}`
          : "能力表尚未读到",
      };
    }
    if (!capability.enabled)
      return {
        ok: false,
        reason: capability.disabledReason ?? "后端没有启用这个动作",
      };
    return { ok: true };
  }

  private closedActions(reason: string): UmOverview["actions"] {
    const closed: Capability = { enabled: false, reason };
    return {
      verify: closed,
      login: closed,
      start: closed,
      stop: closed,
      restart: closed,
    };
  }
  private closedModelActions(reason: string): UmModels["actions"] {
    const closed: Capability = { enabled: false, reason };
    return {
      refresh: closed,
      speedtest: closed,
      context: closed,
      stop: closed,
    };
  }

  private recomputeCapabilities(): void {
    if (!this.roleAvailable) {
      this.patchOverview({ actions: this.closedActions(this.roleReason) });
      this.patchModels({ actions: this.closedModelActions(this.roleReason) });
      return;
    }
    const service = this.serviceState();
    const known = service !== null && service !== "unknown";
    const running = service === "running" || service === "degraded";
    const stopped = service === "stopped";
    const busy = this.runActive();
    const catalog = this.catalogIds();
    const open = (action: string, extra: string | null): Capability => {
      const gate = this.actionEnabled(action);
      if (!gate.ok) return { enabled: false, reason: gate.reason };
      if (extra) return { enabled: false, reason: extra };
      return { enabled: true };
    };
    const notKnown = "UM 服务状态未知：先刷新服务状态";
    this.patchOverview({
      actions: {
        verify: open(
          "account.um_verify",
          !known ? notKnown : running ? null : "UM 服务未运行，先启动"
        ),
        login: open(
          "account.login",
          !known ? notKnown : running ? null : "UM 服务未运行，先启动"
        ),
        start: open(
          "service.start",
          !known ? notKnown : running ? "UM 服务已在运行" : null
        ),
        stop: open(
          "service.stop",
          !known ? notKnown : stopped ? "UM 服务未运行" : null
        ),
        restart: open(
          "service.restart",
          !known ? notKnown : stopped ? "UM 服务未运行，请用「启动」" : null
        ),
      },
    });
    this.patchModels({
      actions: {
        refresh: open(
          "model.catalog",
          !known ? notKnown : running ? null : "UM 服务未运行，读不到目录"
        ),
        speedtest: open(
          "model.speedtest",
          !known
            ? notKnown
            : !running
              ? "UM 服务未运行"
              : busy
                ? "已有测试在进行，等它结束或停止后再发起"
                : catalog.length
                  ? null
                  : "目录里没有可测试的模型"
        ),
        context: open(
          "model.context_probe_run",
          !known
            ? notKnown
            : !running
              ? "UM 服务未运行"
              : busy
                ? "已有测试在进行，等它结束或停止后再发起"
                : catalog.length
                  ? null
                  : "目录里没有可测试的模型"
        ),
        stop: !this.roleAvailable
          ? { enabled: false, reason: this.roleReason }
          : this.gate.verdict().state !== "confirmed"
            ? {
                enabled: false,
                reason: `后端角色未确认：${this.gate.verdict().reason}`,
              }
            : busy
              ? { enabled: true }
              : { enabled: false, reason: "当前没有进行中的测试" },
      },
    });
  }

  private serviceState(): UmService["state"] | null {
    const load = this.state.overview.service;
    if (load.state === "ready" || load.state === "stale")
      return load.data.state;
    return null;
  }
}

// ---------------------------------------------------------------- mapping

function serviceFrom(payload: UmServicesPayload): UmService {
  const entry = payload.services.find((service) => service.key === CHAT2API);
  if (!entry)
    throw new UmReadStatusError(
      "unavailable",
      "后端没有报告 chat2api 服务（UM 主链未装配）"
    );
  return {
    state: SERVICE_STATE[entry.state] ?? "unknown",
    word: SERVICE_WORD[entry.state] ?? "状态未知",
    detail: entry.detail || (entry.healthy === false ? "健康检查未通过" : ""),
    port: entry.port,
    pid: entry.processIds[0] ?? null,
    checkedAt: payload.checkedAt,
  };
}

function loginFrom(result: OperationResult): UmLogin {
  if (result.state !== "succeeded" || !result.operation?.result) {
    return {
      state: "unknown",
      word: "无法检查",
      detail: `查询 UM 认证状态没有成功：${operationNote(result)}`,
      checkedAt: null,
    };
  }
  const raw = result.operation.result;
  const state = rowText(raw, "state");
  const error = rowText(raw, "error");
  const note = rowText(raw, "note");
  const label = rowText(raw, "label");
  const checked = rowNumber(raw, "checked_epoch");
  const stamp = checked === null ? null : Math.round(checked * 1000);
  const mapped: UmLogin["state"] =
    state === "valid"
      ? "valid"
      : state === "needs_login"
        ? "needs_login"
        : state === "logging_in"
          ? "logging_in"
          : state === "failed"
            ? "invalid"
            : "unknown";
  return {
    state: mapped,
    word: label ?? LOGIN_WORD[mapped],
    detail:
      error ??
      note ??
      (state === null ? "认证状态无法确认" : `chat2api 内存态：${state}`),
    checkedAt: stamp,
  };
}

function todayFrom(today: UmTodayPayload): UmToday {
  return {
    day: today.day,
    tokens: today.tokens,
    cacheRead: today.cacheRead,
    reasoning: today.reasoning,
    requests: today.requests,
    speed: today.speed,
  };
}

function costFrom(
  cost: UmCostPayload,
  reference: UmSummaryReferencePayload | null
): UmCost {
  // The day's CNY figure comes from today's own ledger; the price/fx facts are
  // the same Magpie chain, and `/api/summary`'s whole-ledger reference is used
  // when it holds a synced snapshot the day read did not report.
  const useReference =
    reference !== null &&
    cost.price.state !== "synced" &&
    reference.price.state === "synced";
  return {
    cny: cost.cny,
    price: useReference ? reference.price : cost.price,
    fx: useReference ? reference.fx : cost.fx,
    unpricedRequests: cost.unpricedRequests,
    estimatedRequests: cost.estimatedRequests,
  };
}

function trendFrom(trend: UmTrendPayload): UmTrend {
  return {
    range: trend.range,
    days: trend.days,
    all: { daily: trend.all.daily, total: trend.all.total },
    models: trend.models.map((model) => ({
      model: model.model,
      daily: model.daily,
      total: model.total,
      failed: model.failed,
    })),
    speedBasis: trend.speedBasis,
  };
}

function maintenanceFrom(payload: UmMaintenancePagePayload): UmMaintenancePage {
  return {
    rows: payload.rows.map((row) => ({
      key: row.key,
      action: row.action as MaintenanceAction,
      state: row.state,
      at: row.at,
      finishedAt: row.finishedAt,
      steps: row.steps,
      result: row.result,
    })),
    scope: payload.scope,
    earlier: payload.earlier,
  };
}

function accessFrom(result: ReadResult<GatewayConnection>): UmAccess {
  if (result.state !== "ready" || !result.value) {
    const status = result.state === "refused" ? "denied" : "unavailable";
    throw new UmReadStatusError(status, result.reason ?? "连接信息读取失败");
  }
  const connection = result.value;
  const hints: string[] = [
    "在客户端（例如 Magpie）里把上面的地址填成 OpenAI 兼容接口的 Base URL；模型 ID 用下表的 ID。",
  ];
  if (connection.auth === "loopback_no_key") {
    hints.push(
      "本机 8000 当前不要求调用密钥（回环无鉴权）；这是运行实例自己的事实。"
    );
  } else if (connection.auth === "bearer") {
    hints.push("本机 8000 要求调用密钥（Bearer）；本页不显示也不收集密钥值。");
  } else {
    hints.push("是否需要调用密钥尚未确认（未知 ≠ 无鉴权）。");
  }
  if (connection.keyState === "present")
    hints.push("调用密钥来自本机环境变量（值不显示）。");
  else if (connection.keyState === "needs_key_unconfigured")
    hints.push("8000 需要调用密钥，但本机来源缺失或为空。");
  else if (connection.keyState === "unreadable")
    hints.push("调用密钥来源存在，但读不出来。");
  else if (connection.keyState === "absent") hints.push("没有读到调用密钥。");
  else hints.push("调用密钥状态未知。");
  const protocol =
    connection.protocol === "openai-compatible"
      ? `OpenAI 兼容（${connection.protocolPath}）`
      : `${connection.protocol}（${connection.protocolPath}）`;
  return { baseUrl: connection.baseUrl, apiType: protocol, hints };
}

function thinkingSource(
  source: string | null
): UmModelRow["thinking"]["source"] {
  if (source === "upstream") return "catalog";
  if (source === "known" || source === "probe") return "local";
  return "unknown";
}

function speedState(status: string): ModelTestState {
  if (status === "ok") return "done";
  if (status === "failed" || status === "timeout") return "failed";
  return "partial";
}

function speedSummary(row: JsonObject): string {
  const status = rowText(row, "status") ?? "unknown";
  const output = rowCount(row, "output_tokens");
  const whole = rowNumber(row, "whole_avg_tokens_per_s");
  const total = rowNumber(row, "total_s");
  const ttfb = rowNumber(row, "ttfb_s");
  const error = object(row.error);
  const bits: string[] = [];
  if (status !== "ok") {
    const code = error ? text(error.code) : null;
    const message = error ? text(error.message) : null;
    bits.push(
      [code, message].filter(Boolean).join("：") || `测速结果：${status}`
    );
    return bits.join(" · ");
  }
  if (whole !== null)
    bits.push(`${formatSpeed(whole)} token/秒（整次平均，含等待）`);
  else bits.push("完成，但没有可计算的速度");
  if (output !== null) bits.push(`输出 ${output} Token`);
  if (total !== null && total > 0) bits.push(`耗时 ${total.toFixed(1)} 秒`);
  if (ttfb !== null) bits.push(`首字 ${ttfb.toFixed(2)} 秒`);
  return bits.join(" · ");
}

function formatSpeed(value: number): string {
  return value < 10
    ? value.toFixed(1).replace(/\.0$/, "")
    : String(Math.round(value));
}

function speedDetail(row: JsonObject): UmTestDetail {
  const status = rowText(row, "status") ?? "unknown";
  const output = rowCount(row, "output_tokens");
  const whole = rowNumber(row, "whole_avg_tokens_per_s");
  const total = rowNumber(row, "total_s");
  const ttfb = rowNumber(row, "ttfb_s");
  const error = object(row.error);
  const lines: string[] = [`测速结果：${status}`];
  if (total !== null) lines.push(`整次耗时 ${total.toFixed(2)} 秒`);
  if (ttfb !== null) lines.push(`首字实测 ${ttfb.toFixed(2)} 秒`);
  if (output !== null) lines.push(`输出 ${output} Token（上游报告的整段输出）`);
  if (whole !== null)
    lines.push(`整次平均 ${formatSpeed(whole)} token/秒（含首字等待）`);
  return {
    durationMs:
      total === null
        ? missing("没有记录耗时")
        : { kind: "exact", value: Math.round(total * 1000) },
    firstTokenMs:
      ttfb === null
        ? missing("这次没有实测首字")
        : { kind: "exact", value: Math.round(ttfb * 1000) },
    outputTokens:
      output === null
        ? missing("上游没有报告输出 Token")
        : { kind: "exact", value: output },
    speed:
      whole === null
        ? missing(status === "ok" ? "没有可计算的速度" : "请求未成功，不计速度")
        : { kind: "exact", value: whole },
    speedBasis: "whole_request",
    contextChars: missing("测速不探测上下文"),
    contextTokenLowerBound: missing("测速不探测上下文"),
    error:
      status === "ok"
        ? null
        : [error ? text(error.code) : null, error ? text(error.message) : null]
            .filter(Boolean)
            .join("：") || `测速结果：${status}`,
    lines,
  };
}

function contextState(status: string, requests: number): ModelTestState {
  if (status === "verified") return "done";
  if (status === "failed") return "failed";
  return requests > 0 ? "partial" : "not_run";
}

function contextEntries(op: { result: JsonObject | null }): TestEntry[] {
  const rows = rowsOf(op.result?.results);
  return rows.flatMap((row) => {
    const key = rowText(row, "key");
    const model =
      rowText(row, "model_id") ??
      (key !== null && key.includes("|")
        ? key.slice(key.indexOf("|") + 1)
        : key);
    if (model === null) return [];
    const status = rowText(row, "status") ?? "unknown";
    const requests = rowCount(row, "requests") ?? 0;
    const note = rowText(row, "note");
    const detail: UmTestDetail = {
      durationMs: missing(
        "操作结果不提供逐模型耗时；模型表的上下文信息来自探测队列的持久证据"
      ),
      firstTokenMs: missing("上下文探测不测首字"),
      outputTokens: missing("操作结果不提供逐模型输出 Token"),
      speed: missing("上下文探测不计速度"),
      speedBasis: "whole_request",
      contextChars: missing(
        "操作结果不提供逐模型字符步进；已刷新模型表的上下文信息"
      ),
      contextTokenLowerBound: missing(
        "操作结果不提供逐模型 Token 下界；已刷新模型表的上下文信息"
      ),
      error: status === "failed" ? (note ?? "上下文探测失败") : null,
      lines: [
        `上下文探测：${status}`,
        `请求 ${requests} 次`,
        ...(note ? [note] : []),
      ],
    };
    return [
      {
        model,
        state: contextState(status, requests),
        summary: `上下文探测：${status}${note ? ` · ${note}` : ""}（请求 ${requests} 次）`,
        detail,
      },
    ];
  });
}

function speedtestEntries(op: { result: JsonObject | null }): TestEntry[] {
  const raw = op.result;
  if (!raw) return [];
  const rows = rowsOf(raw.results);
  const list = rows.length ? rows : [raw];
  return list.flatMap((row) => {
    const model = rowText(row, "model");
    if (model === null) return [];
    const status = rowText(row, "status") ?? "unknown";
    return [
      {
        model,
        state: speedState(status),
        summary: speedSummary(row),
        detail: speedDetail(row),
      },
    ];
  });
}

function resultEntries(kind: TestKind, result: OperationResult): TestEntry[] {
  if (result.state !== "succeeded" || !result.operation?.result) return [];
  const raw = { result: result.operation.result };
  return kind === "speedtest" ? speedtestEntries(raw) : contextEntries(raw);
}

function resultBudget(
  result: OperationResult
): { used: number; limit: number } | null {
  const raw = result.operation?.result;
  const budget = raw ? object(raw.budget) : null;
  if (!budget) return null;
  const used = rowCount(budget, "requests_used");
  const limit = rowCount(budget, "max_requests");
  return used === null || limit === null ? null : { used, limit };
}

function runNote(kind: TestKind, result: OperationResult): string {
  if (result.state === "succeeded") {
    const raw = result.operation?.result ?? null;
    return (
      rowText(raw, "summary") ??
      (kind === "speedtest"
        ? "每个所选模型最多 1 次短请求，不自动重试；速度为整次平均（含等待）。"
        : "整批共用一份请求预算；字符步进按字符计，Token 是上游报告的已验证下界，不是最大上下文。")
    );
  }
  if (result.state === "cancelled")
    return "已请求停止；已完成的模型结果保留，之后的模型不再开始。";
  if (result.state === "unconfirmed")
    return `结果未确认：${operationNote(result)}。以操作记录为准，本页不重发。`;
  return `测试没有完成：${operationNote(result)}`;
}

function mergeRows<T extends { key: string }>(older: T[], newer: T[]): T[] {
  const seen = new Set(older.map((row) => row.key));
  return [...older, ...newer.filter((row) => !seen.has(row.key))];
}

function mergeLines(older: UmLogLine[], newer: UmLogLine[]): UmLogLine[] {
  const seen = new Set(older.map((line) => line.key));
  return [...older, ...newer.filter((line) => !seen.has(line.key))];
}

function catalogFailureKind(status: string | null): FailureKind {
  if (status === "auth_required") return "unauthenticated";
  if (status === "unavailable") return "unavailable";
  return "error";
}

function progressResult(
  result: OperationResult
): Extract<ActionProgress, { state: "done" }>["result"] {
  if (result.state === "succeeded") return "succeeded";
  if (result.state === "cancelled") return "cancelled";
  if (result.state === "interrupted" || result.state === "unconfirmed")
    return "unconfirmed";
  return "failed";
}
