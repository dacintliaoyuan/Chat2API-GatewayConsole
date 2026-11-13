/** UM console view contract (handoff r3, frozen 2026-11-01 for the B6 wiring).
 *
 *  The three pages (总览 / 请求与诊断 / 模型与设置) are views only. A data
 *  controller (B6; suggested web/src/local/um-data-controller.ts) owns every
 *  read, task, cancel and backend action: it hands the console a UmViewState
 *  through `UmConsoleHandle.update`, and receives each UmEvent the console
 *  emits. The console never fetches and never decides a backend outcome.
 *
 *  Time: instants are epoch milliseconds, shown in Beijing time; day buckets
 *  are Beijing natural days written "YYYY-MM-DD". Units: tokens and requests
 *  are counts, money is CNY yuan and always a reference figure (never a
 *  charge), durations are milliseconds, speed is output tokens per second.
 *
 *  Evolution is additive: new optional fields or new union members only; an
 *  existing field never changes its meaning. */

/** One figure and how much of it is known. A real zero is
 *  `{ kind: "exact", value: 0 }`; no other kind is ever shown as 0. */
export type Reading =
  | { kind: "exact"; value: number }
  /** Part of the population is missing: the true figure is at least `value`. */
  | { kind: "atLeast"; value: number; note?: string }
  /** A computed figure, e.g. reference prices applied to recorded tokens. */
  | { kind: "estimate"; value: number; note?: string }
  /** The source does not report this figure (e.g. a reply without cache usage). */
  | { kind: "unreported"; note?: string }
  /** It should be known but is not: a broken or unparsable record. */
  | { kind: "unknown"; note?: string }
  /** There is no record of it at all (e.g. a first-token time nobody measured). */
  | { kind: "missing"; note?: string };

export type FailureKind =
  "unauthenticated" | "refused" | "unavailable" | "error" | "no-answer";
export type Failure = { kind: FailureKind; reason: string; at: number };

/** One read section. A failed refresh keeps the last good data (`stale`). */
export type Load<T> =
  /** No data controller feeds this section yet: the honest state of the real
   *  console before B6, never replaced by sample data. */
  | { state: "unwired" }
  /** First read, nothing to show yet. */
  | { state: "loading" }
  /** `refreshing`: a newer read is in flight; `data` is still the shown one. */
  | { state: "ready"; data: T; at: number; refreshing?: boolean }
  | {
      state: "stale";
      data: T;
      at: number;
      failure: Failure;
      refreshing?: boolean;
    }
  | { state: "failed"; failure: Failure };

/** Whether an action can be triggered now; `reason` is shown beside it. The
 *  controller folds the role gate in: writes stay disabled until the backend
 *  role is confirmed. */
export type Capability = { enabled: true } | { enabled: false; reason: string };

/** The latest submit of one action, as the controller reports it. */
export type ActionProgress =
  | { state: "pending"; since: number; note?: string }
  | {
      state: "done";
      result: "succeeded" | "failed" | "cancelled" | "unconfirmed";
      at: number;
      note: string;
    };

// ---------------------------------------------------------------- 总览

/** chat2api on 8000, the process that serves UM. */
export type UmService = {
  state:
    "running" | "starting" | "stopping" | "stopped" | "degraded" | "unknown";
  /** The state in the backend's own words, e.g. 「运行中」. */
  word: string;
  detail: string;
  port: number | null;
  pid: number | null;
  checkedAt: number | null;
};
/** UM sign-in as the read-only status query reports it (account.um_status). */
export type UmLogin = {
  state: "valid" | "needs_login" | "invalid" | "logging_in" | "unknown";
  word: string;
  detail: string;
  checkedAt: number | null;
};
/** The most recent upstream verification (account.um_verify); null = never run. */
export type UmVerify = {
  result: "passed" | "failed" | "unknown";
  word: string;
  detail: string;
  at: number;
} | null;

/** `whole_request`: output ÷ the request's whole duration, waiting included.
 *  `generation`: a measured generation phase. Neither is a first-token time. */
export type SpeedBasis = "whole_request" | "generation";

export type UmToday = {
  /** Beijing day key. */
  day: string;
  tokens: { total: Reading; input: Reading; output: Reading };
  /** Cache reads are part of input and are never added to it again. */
  cacheRead: {
    tokens: Reading;
    report: "reported" | "partial" | "unreported" | "unknown";
    detail: string;
  };
  /** Thinking tokens are part of output and are never added to it again. */
  reasoning: Reading;
  /** Logical requests: a physical retry is not counted twice. */
  requests: { recorded: Reading; failed: Reading };
  speed: { value: Reading; basis: SpeedBasis; samples: number | null };
};

/** The day's reference cost: UM's own ledger priced with the effective UM
 *  prices followed from Magpie. Not synced is not zero. */
export type UmCost = {
  cny: Reading;
  price: {
    state: "synced" | "unsynced" | "failed";
    source: string | null;
    snapshotAt: number | null;
    version: string | null;
    failure: string | null;
  };
  fx: { rate: number | null; version: string | null };
  unpricedRequests: number | null;
  estimatedRequests: number | null;
};

export type TrendRange = "7d" | "14d" | "30d";
/** All figures of one model (or of all models) over one bucket. */
export type UmBucket = {
  tokens: Reading;
  costCny: Reading;
  requests: Reading;
  speed: Reading;
};
export type UmModelSeries = {
  model: string;
  /** Aligned with `UmTrend.days`. */
  daily: UmBucket[];
  /** The window's totals: the ranking reads exactly these. */
  total: UmBucket;
  failed: Reading;
};
export type UmTrend = {
  range: TrendRange;
  /** Beijing day keys of the whole window, oldest first, today last. */
  days: string[];
  /** All models together; speed is a request-weighted average, not a sum. */
  all: { daily: UmBucket[]; total: UmBucket };
  /** Sorted by window tokens, largest first. */
  models: UmModelSeries[];
  speedBasis: SpeedBasis;
};

export type UmIssue = {
  kind:
    | "request_failed"
    | "truncated"
    | "client_disconnected"
    | "login"
    | "service"
    | "catalog"
    | "price"
    | "test";
  title: string;
  detail: string;
  count: number;
  lastAt: number;
  /** Where the evidence is; null when no view of this console holds it. */
  evidence: { view: RequestsView; query?: Partial<RequestQuery> } | null;
};

export type MaintenanceAction =
  "verify" | "login" | "start" | "stop" | "restart";

export type UmOverview = {
  service: Load<UmService>;
  login: Load<UmLogin>;
  verify: Load<UmVerify>;
  today: Load<UmToday>;
  cost: Load<UmCost>;
  trend: Load<UmTrend>;
  /** Recent problems; the window is named in `window`, e.g. 「近 24 小时」. */
  issues: Load<{ window: string; items: UmIssue[] }>;
  actions: Record<MaintenanceAction, Capability>;
  progress: Partial<Record<MaintenanceAction, ActionProgress>>;
};

// ---------------------------------------------------------------- 请求与诊断

export type RequestsView = "records" | "logs" | "maintenance";
/** HTTP 200 does not make a truncated reply a success, and a client that went
 *  away is not a cancel anybody asked for. */
export type RequestResult =
  | "success"
  | "failed"
  | "truncated"
  | "client_disconnected"
  | "cancelled"
  | "unknown";
export type TimeWindow = "today" | "24h" | "7d" | "30d";
export type RequestQuery = {
  window: TimeWindow;
  model: string | null;
  result: RequestResult | "all";
  search: string;
};
export type UmRequestRow = {
  /** Stable identity of the ledger row. */
  key: string;
  /** Null: the row cannot be tied to log lines reliably, and the view says so. */
  requestId: string | null;
  at: number;
  model: string;
  result: RequestResult;
  /** The result in the backend's own words, e.g. 「HTTP 200，回复在完成前截断」. */
  resultNote: string;
  httpStatus: number | null;
  input: Reading;
  output: Reading;
  cacheRead: Reading;
  reasoning: Reading;
  costCny: Reading;
  durationMs: Reading;
  /** Only a real measurement; otherwise missing. */
  firstTokenMs: Reading;
  speed: Reading;
  speedBasis: SpeedBasis;
  /** Physical retries recorded for this logical request; null = not recorded. */
  retries: { at: number; note: string }[] | null;
  error: { code: string; message: string } | null;
};
export type UmRequestPage = {
  rows: UmRequestRow[];
  /** What the read covered, e.g. 「北京时间今天 00:00 起的 UM 账本」. */
  scope: string;
  /** Cursor for older rows; null when there are none. */
  earlier: string | null;
};
export type LogLevel = "error" | "warn" | "info" | "debug" | "unknown";
export type LogQuery = {
  window: TimeWindow;
  levels: LogLevel[];
  source: string | null;
};
export type UmLogLine = {
  key: string;
  at: number | null;
  level: LogLevel;
  source: string;
  text: string;
  requestId: string | null;
};
export type UmLogPage = {
  lines: UmLogLine[];
  sources: string[];
  scope: string;
  earlier: string | null;
};
export type UmMaintenanceRow = {
  key: string;
  action: MaintenanceAction;
  state:
    "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted";
  at: number;
  finishedAt: number | null;
  steps: { at: number; note: string }[];
  result: string;
};
export type UmMaintenancePage = {
  rows: UmMaintenanceRow[];
  scope: string;
  earlier: string | null;
};

export type UmRequests = {
  /** The query `page` answers. A late answer to an older query must not land. */
  query: RequestQuery;
  page: Load<UmRequestPage>;
  /** Model names offered by the model filter. */
  models: string[];
  /** Log lines tied to one request, read on demand (`requests.detail`). */
  detail: { key: string; logs: Load<UmLogLine[]> } | null;
  logs: { query: LogQuery; page: Load<UmLogPage> };
  maintenance: Load<UmMaintenancePage>;
};

// ---------------------------------------------------------------- 模型与设置

export type UmAccess = {
  baseUrl: string | null;
  apiType: string | null;
  hints: string[];
};
export type UmModelRow = {
  id: string;
  name: string;
  context: {
    /** What the UM catalog declares, with where it came from. */
    declared: { tokens: number; source: string } | null;
    /** A compatibility default: never a UM-approved limit. */
    compatDefault: number | null;
    /** Character steps that passed: a probe measure, counted in characters. */
    probedChars: { chars: number; at: number } | null;
    /** Tokens an upstream reported: a verified lower bound, not a maximum. */
    tokenLowerBound: { tokens: number; at: number } | null;
  };
  thinking: {
    levels: string[];
    source: "catalog" | "local" | "unknown";
    defaultLevel: string | null;
  };
  lastContext: { at: number; ok: boolean; summary: string } | null;
};
export type UmCatalog = {
  models: UmModelRow[];
  fetchedAt: number;
  source: string;
};
export type UmCatalogRefresh =
  | { state: "idle" }
  | { state: "refreshing"; since: number }
  | { state: "succeeded"; at: number }
  /** The list keeps the last good catalog, shown as stale from `catalog.at`. */
  | { state: "failed"; at: number; reason: string };

export type ModelTestState =
  "waiting" | "running" | "done" | "failed" | "partial" | "not_run";
export type UmTestDetail = {
  durationMs: Reading;
  firstTokenMs: Reading;
  outputTokens: Reading;
  speed: Reading;
  speedBasis: SpeedBasis;
  /** Context probes: the largest character step that passed. */
  contextChars: Reading;
  /** Context probes: tokens an upstream reported, a lower bound. */
  contextTokenLowerBound: Reading;
  error: string | null;
  lines: string[];
};
export type UmTestRun = {
  id: string;
  startedAt: number;
  state: "submitting" | "running" | "finished";
  /** Stop asked for: the request in flight may still finish. */
  stopRequested: boolean;
  /** Context probes share one request budget across the whole batch. */
  budget: { used: number; limit: number } | null;
  items: {
    model: string;
    state: ModelTestState;
    stage: string | null;
    summary: string | null;
    detail: UmTestDetail | null;
  }[];
  note: string | null;
};
export type HistoryFilter = { model: string | null };
export type UmTestRecord = {
  key: string;
  model: string;
  at: number;
  result: ModelTestState;
  summary: string;
  archived: boolean;
};
export type UmModels = {
  access: Load<UmAccess>;
  catalog: Load<UmCatalog>;
  refresh: UmCatalogRefresh;
  run: UmTestRun | null;
  history: { filter: HistoryFilter; records: Load<UmTestRecord[]> };
  /** Read-only runtime facts (timeouts, retries, ports); not edited here. */
  runtime: Load<{ label: string; value: string }[]>;
  actions: {
    refresh: Capability;
    context: Capability;
    stop: Capability;
  };
  /** The controller's answer to the latest model action that produced no run
   *  (a refused selection, nothing left to do); null when there is none. */
  notice: {
    tone: "ok" | "warn" | "err" | "neutral";
    text: string;
    at: number;
  } | null;
};

// ---------------------------------------------------------------- root

export type UmViewState = {
  /** `preview`: offline synthetic data in a candidate build, always labelled. */
  mode: "live" | "preview";
  overview: UmOverview;
  requests: UmRequests;
  models: UmModels;
};

export type UmPage = "overview" | "requests" | "models";

/** Everything the console asks of the controller. Login, start, stop and
 *  restart are emitted only after the console's own confirm; model tests and
 *  the catalog refresh are emitted on click, with no preview step. The
 *  console holds a just-clicked test button closed (「正在提交」) until the
 *  controller answers `models.test` with an `update` carrying a run with a
 *  new id, a `models.notice` with a new `at`, or a changed test capability;
 *  an unrelated update does not reopen it. Every closed control is checked
 *  again against the live state when used, so a stale click sends nothing.
 *  Filters build on the last query asked for until the controller reports
 *  that query back. */
export type UmEvent =
  /** The reader opened a page or a diagnostics view; reads may start lazily. */
  | { type: "page"; page: UmPage; view: RequestsView | null }
  | { type: "refresh"; page: UmPage; view: RequestsView | null }
  | { type: "trend.range"; range: TrendRange }
  | { type: "um.verify" }
  /** 检查并按需登录: verify first, open the sign-in window only if needed. */
  | { type: "um.login" }
  | { type: "um.service"; action: "start" | "stop" | "restart" }
  | { type: "requests.query"; query: RequestQuery }
  | { type: "requests.earlier"; cursor: string }
  /** Load the log lines tied to a row; only sent for rows with a requestId. */
  | { type: "requests.detail"; key: string }
  | { type: "logs.query"; query: LogQuery }
  | { type: "logs.earlier"; cursor: string }
  | { type: "maintenance.earlier"; cursor: string }
  | { type: "models.refreshCatalog" }
  /** Explicit UM model ids, deduplicated, never empty. */
  | { type: "models.test"; models: string[] }
  | { type: "models.stop"; runId: string }
  | { type: "models.history"; filter: HistoryFilter };

export interface UmConsoleHandle {
  update(state: UmViewState): void;
}

const UNWIRED = "本地 UM 数据控制器尚未接线（上云主任务 B6）";

/** The real console's state until a controller feeds it: every section
 *  unwired and every action closed, with the reason shown beside it. */
export function unwiredState(): UmViewState {
  const closed: Capability = { enabled: false, reason: UNWIRED };
  return {
    mode: "live",
    overview: {
      service: { state: "unwired" },
      login: { state: "unwired" },
      verify: { state: "unwired" },
      today: { state: "unwired" },
      cost: { state: "unwired" },
      trend: { state: "unwired" },
      issues: { state: "unwired" },
      actions: {
        verify: closed,
        login: closed,
        start: closed,
        stop: closed,
        restart: closed,
      },
      progress: {},
    },
    requests: {
      query: { window: "today", model: null, result: "all", search: "" },
      page: { state: "unwired" },
      models: [],
      detail: null,
      logs: {
        query: { window: "today", levels: [], source: null },
        page: { state: "unwired" },
      },
      maintenance: { state: "unwired" },
    },
    models: {
      access: { state: "unwired" },
      catalog: { state: "unwired" },
      refresh: { state: "idle" },
      run: null,
      history: {
        filter: { model: null },
        records: { state: "unwired" },
      },
      runtime: { state: "unwired" },
      actions: {
        refresh: closed,
        context: closed,
        stop: closed,
      },
      notice: null,
    },
  };
}
