/** State, role gate, publishing and capability folding for the local UM data
 *  controller. This is the base of the controller composition: reads and
 *  actions extend it, and this module owns the shared state, the lane tokens,
 *  the role probe, the stale/refresh cache and the action gates. */

import type { RoleGate } from "../../console/role-gate";
import { BackendClient } from "../../console/backend/client";
import {
  LatestRequests,
  createFetchTransport,
} from "../../console/backend/transport";
import type {
  ActionCapability,
  ContextEvidence,
  Operation,
  UmCatalogPayload,
  UmOperationPayload,
  UmSummaryPayload,
} from "../../console/backend/types";
import { unwiredState } from "../um-ui/contract";
import type {
  Capability,
  Failure,
  Load,
  TrendRange,
  UmConsoleHandle,
  UmIssue,
  UmModels,
  UmOverview,
  UmRequests,
  UmService,
  UmTestRun,
  UmViewState,
} from "../um-ui/contract";
import { when } from "../um-ui/format";
import { failureFrom, readingNumber } from "./view-model";

/** The catalog rows' declared-source label: the same label the catalog read
 *  reports, so the view never claims a stricter origin than the read used. */
export const CATALOG_PLAIN_SOURCE = "UM /api/models（只读目录）";
export const CATALOG_FRESH_SOURCE = "UM /api/models（raw-fresh 严格取新）";

/** The role gate's confirmation is asynchronous: reads that ride the operation
 *  registry must wait until the backend role is no longer `checking`, otherwise
 *  the front-end gate refuses their POST with its own 403. */
const GATE_WAIT_MS = 2_500;

type Gate = { ok: true } | { ok: false; reason: string };

export interface UmDataControllerOptions {
  gate: RoleGate;
  /** The console-relative client (default base path `/gw/usagesrv/api/console`). */
  client?: BackendClient;
  /** `/api/summary` rides the console host but not the console prefix. */
  summaryClient?: BackendClient;
  now?: () => number;
}

/** `loading` on a first read, `refreshing` on a newer read with data shown. */
export function beginLoad<T>(load: Load<T>): Load<T> {
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
export function failedOrStale<T>(previous: Load<T>, failure: Failure): Load<T> {
  if (previous.state === "ready")
    return { state: "stale", data: previous.data, at: previous.at, failure };
  if (previous.state === "stale")
    return { state: "stale", data: previous.data, at: previous.at, failure };
  return { state: "failed", failure };
}

export class UmDataState {
  protected readonly gate: RoleGate;
  protected readonly client: BackendClient;
  protected readonly summaryClient: BackendClient;
  protected readonly requests = new LatestRequests();
  protected readonly now: () => number;
  protected handle: UmConsoleHandle | null = null;
  protected state: UmViewState;
  protected disposed = false;
  protected started = false;

  /** Resolves once the role probe (`/api/summary` + the capability table) ran. */
  protected readonly roleReady: Promise<void>;
  protected resolveRole!: () => void;
  protected roleAvailable = true;
  protected roleReason = "";
  protected summary: UmSummaryPayload | null = null;

  protected capabilities: Map<string, ActionCapability> | null = null;
  protected capabilityFailure: Failure | null = null;
  protected unsubscribeGate: (() => void) | null = null;

  /** One token per read lane: a superseded read never installs its answer or
   *  drops the newer read's spinner. */
  protected readonly laneTokens = new Map<string, number>();
  protected pendingPublish = false;

  // Raw reads kept for derived sections (the frozen view has no place for them).
  protected operations: UmOperationPayload[] = [];
  protected operationsAt: number | null = null;
  protected operationsFailure: Failure | null = null;
  protected operationsPromise: Promise<void> | null = null;
  protected catalog: UmCatalogPayload | null = null;
  protected catalogSource = CATALOG_PLAIN_SOURCE;
  protected catalogFetchedAt: number | null = null;
  protected catalogStale: Failure | null = null;
  protected catalogGeneration = 0;
  protected evidence: ContextEvidence[] = [];
  protected evidenceAt: number | null = null;
  protected timezone: string | null = null;
  protected failedLatestAt: number | null = null;
  protected failedLatestUnread = false;
  protected trendRange: TrendRange = "7d";
  protected requestsLoadedFor: string | null = null;
  protected logsLoadedFor: string | null = null;
  protected maintenanceLoaded = false;
  protected overviewLoaded = false;
  protected modelsLoaded = false;

  protected run: UmTestRun | null = null;
  protected runOperation: Operation | null = null;
  protected cancelSentFor: string | null = null;
  protected lastProgressReadAt = 0;
  protected reconnectNoticeFor: string | null = null;

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

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.requests.cancelAll();
    this.unsubscribeGate?.();
    this.unsubscribeGate = null;
    this.handle = null;
  }

  // ---------------------------------------------------------------- publish

  protected publish(): void {
    if (this.disposed || this.pendingPublish) return;
    this.pendingPublish = true;
    queueMicrotask(() => {
      this.pendingPublish = false;
      if (!this.disposed) this.handle?.update(this.state);
    });
  }

  protected patchOverview(patch: Partial<UmOverview>): void {
    this.state = {
      ...this.state,
      overview: { ...this.state.overview, ...patch },
    };
    this.publish();
  }
  protected patchRequests(patch: Partial<UmRequests>): void {
    this.state = {
      ...this.state,
      requests: { ...this.state.requests, ...patch },
    };
    this.publish();
  }
  protected patchModels(patch: Partial<UmModels>): void {
    this.state = { ...this.state, models: { ...this.state.models, ...patch } };
    this.publish();
  }
  protected notice(
    tone: "ok" | "warn" | "err" | "neutral",
    message: string
  ): void {
    this.patchModels({ notice: { tone, text: message, at: this.now() } });
  }

  protected nextLane(lane: string): number {
    const token = (this.laneTokens.get(lane) ?? 0) + 1;
    this.laneTokens.set(lane, token);
    return token;
  }
  protected isCurrentLane(lane: string, token: number): boolean {
    return this.laneTokens.get(lane) === token;
  }

  protected markUnavailable(reason: string): void {
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

  protected async loadCapabilities(): Promise<void> {
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

  protected async whenGateSettled(): Promise<void> {
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
  protected async roleThen(task: () => void): Promise<void> {
    await this.roleReady;
    if (this.disposed || !this.roleAvailable) return;
    await this.whenGateSettled();
    if (this.disposed || !this.roleAvailable) return;
    task();
  }

  protected recomputeIssues(): void {
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

  protected rebuildRuntime(): void {
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

  // ---------------------------------------------------------------- capabilities

  protected catalogIds(): string[] {
    const catalog = this.state.models.catalog;
    if (catalog.state !== "ready" && catalog.state !== "stale") return [];
    return [...new Set(catalog.data.models.map((model) => model.id))];
  }

  protected runActive(): boolean {
    return this.run !== null && this.run.state !== "finished";
  }

  protected cap(action: string): ActionCapability | null {
    return this.capabilities?.get(action) ?? null;
  }

  protected actionEnabled(action: string): Gate {
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

  protected closedActions(reason: string): UmOverview["actions"] {
    const closed: Capability = { enabled: false, reason };
    return {
      verify: closed,
      login: closed,
      start: closed,
      stop: closed,
      restart: closed,
    };
  }
  protected closedModelActions(reason: string): UmModels["actions"] {
    const closed: Capability = { enabled: false, reason };
    return {
      refresh: closed,
      speedtest: closed,
      context: closed,
      stop: closed,
    };
  }

  protected recomputeCapabilities(): void {
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

  protected serviceState(): UmService["state"] | null {
    const load = this.state.overview.service;
    if (load.state === "ready" || load.state === "stale")
      return load.data.state;
    return null;
  }
}
