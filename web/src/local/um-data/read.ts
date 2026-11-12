/** Reads, queries, lazy page loads and read-result application for the local
 *  UM data controller. Every read keeps the frozen semantics: a failed refresh
 *  keeps the last good data as stale, a cancelled or superseded read never
 *  installs its answer, and a missing ledger is a failure rather than an empty
 *  list. */

import type { UmRequestsReadQuery } from "../../console/backend/client";
import type {
  ContextEvidence,
  GatewayConnection,
  OperationResult,
  ReadResult,
  UmServicesPayload,
} from "../../console/backend/types";
import type {
  HistoryFilter,
  Load,
  LogQuery,
  RequestQuery,
  RequestsView,
  TestKind,
  TrendRange,
  UmAccess,
  UmCatalog,
  UmLogin,
  UmLogLine,
  UmMaintenancePage,
  UmModelRow,
  UmService,
  UmTestRecord,
  UmTrend,
  UmVerify,
} from "../um-ui/contract";
import {
  CATALOG_PLAIN_SOURCE,
  UmDataState,
  beginLoad,
  failedOrStale,
} from "./state";
import {
  CHAT2API,
  TERMINAL,
  UM_SOURCE,
  accessFrom,
  contextEntries,
  costFrom,
  failureFrom,
  loginFrom,
  maintenanceFrom,
  readingNumber,
  serviceFrom,
  speedtestEntries,
  thinkingSource,
  todayFrom,
  trendFrom,
  verifyFrom,
} from "./view-model";

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

function mergeRows<T extends { key: string }>(older: T[], newer: T[]): T[] {
  const seen = new Set(older.map((row) => row.key));
  return [...older, ...newer.filter((row) => !seen.has(row.key))];
}

function mergeLines(older: UmLogLine[], newer: UmLogLine[]): UmLogLine[] {
  const seen = new Set(older.map((line) => line.key));
  return [...older, ...newer.filter((line) => !seen.has(line.key))];
}

export class UmDataReads extends UmDataState {
  /** Start the role probe. The console emits its first `page` event on mount; the
   *  role probe itself starts here so writes are confirmed as early as possible. */
  start(): void {
    if (this.started || this.disposed) return;
    this.started = true;
    void this.bootstrap();
  }

  /** One section read: loading on the first read, refreshing on a newer one;
   *  a failure keeps the last good data as stale; a cancelled or superseded read
   *  never installs its answer and never drops the newer read's spinner. */
  protected async readSection<T, D>(
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

  protected async bootstrap(): Promise<void> {
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

  protected async readSummary(): Promise<void> {
    try {
      this.summary = await this.summaryClient.readSummary();
    } catch {
      this.summary = null;
    }
  }

  // ---------------------------------------------------------------- pages

  protected onPage(
    page: "overview" | "requests" | "models",
    view: RequestsView | null
  ): void {
    if (page === "overview") this.ensureOverview(false);
    else if (page === "requests") this.ensureRequests(view ?? "records", false);
    else this.ensureModels(false);
  }

  protected onRefresh(
    page: "overview" | "requests" | "models",
    view: RequestsView | null
  ): void {
    if (page === "overview") this.ensureOverview(true);
    else if (page === "requests") this.ensureRequests(view ?? "records", true);
    else this.ensureModels(true);
  }

  protected ensureOverview(force: boolean): void {
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

  protected readService(): void {
    void this.readSection<UmServicesPayload, UmService>(
      "um-services",
      () => this.state.overview.service,
      (load) => this.patchOverview({ service: load }),
      (signal) => this.client.readServices(signal),
      (payload) => serviceFrom(payload)
    );
  }

  protected readLogin(): void {
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

  protected readToday(): void {
    void this.readTodayTask();
  }

  protected async readTodayTask(): Promise<void> {
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

  protected readTrend(): void {
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

  protected onTrendRange(range: TrendRange): void {
    if (range === this.trendRange) return;
    this.trendRange = range;
    void this.roleReady.then(() => {
      if (this.disposed || !this.roleAvailable) return;
      this.readTrend();
    });
  }

  protected refreshFailedLatest(): void {
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

  // ---------------------------------------------------------------- operations

  protected readOperations(force: boolean): Promise<void> {
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

  protected async doReadOperations(
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

  protected applyOperations(): void {
    const at = this.operationsAt ?? this.now();
    this.patchOverview({ verify: verifyFrom(this.operations, at) });
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

  protected applyHistoryFilter(filter: HistoryFilter): void {
    this.patchModels({
      history: { filter, records: this.historyLoad(filter) },
    });
  }

  protected historyLoad(filter: HistoryFilter): Load<UmTestRecord[]> {
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

  protected testRecords(filter: HistoryFilter): UmTestRecord[] {
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

  protected ensureRequests(view: RequestsView, force: boolean): void {
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

  protected onRequestsQuery(query: RequestQuery): void {
    if (sameQuery(query, this.state.requests.query)) return;
    this.patchRequests({ query, page: { state: "loading" }, detail: null });
    this.requestsLoadedFor = null;
    void this.roleThen(() => this.readRequestsPage());
  }

  protected readRequestsPage(): void {
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

  protected readEarlierRequests(cursor: string): void {
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

  protected mergeRequestModels(models: string[]): void {
    if (!models.length) return;
    const merged = [...new Set([...this.state.requests.models, ...models])];
    this.patchRequests({ models: merged });
  }

  protected readRequestDetail(key: string): void {
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

  protected onLogsQuery(query: LogQuery): void {
    if (sameLogQuery(query, this.state.requests.logs.query)) return;
    this.patchRequests({ logs: { query, page: { state: "loading" } } });
    this.logsLoadedFor = null;
    void this.roleThen(() => this.readLogsPage());
  }

  protected readLogsPage(): void {
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

  protected mergeLogSources(sources: string[]): void {
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

  protected readEarlierLogs(cursor: string): void {
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

  protected readMaintenance(force = false): void {
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

  protected ensureModels(force: boolean): void {
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

  protected readAccess(): void {
    void this.readSection<ReadResult<GatewayConnection>, UmAccess>(
      "um-access",
      () => this.state.models.access,
      (load) => this.patchModels({ access: load }),
      (signal) => this.client.readGatewayConnection(CHAT2API, signal),
      accessFrom
    );
  }

  protected async readCatalog(force: boolean): Promise<void> {
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

  protected readEvidence(): void {
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

  protected applyCatalogRows(): void {
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

  protected lastTest(
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
}
