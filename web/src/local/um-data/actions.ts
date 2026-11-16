/** Actions, operations, catalog refresh and event dispatch for the local UM
 *  data controller. Every write is folded through the same role gate and
 *  capability table as before; the controller composition only wires the public
 *  class to these modules. */

import { ActionBusyError } from "../../console/backend/client";
import type {
  JsonObject,
  Operation,
  OperationResult,
  UmOperationPayload,
} from "../../console/backend/types";
import type {
  ActionProgress,
  Failure,
  MaintenanceAction,
  UmEvent,
  UmLoginParams,
  UmTestRun,
} from "../um-ui/contract";
import { CATALOG_FRESH_SOURCE } from "./state";
import { UmDataReads } from "./read";
import {
  CHAT2API,
  UM_SOURCE,
  catalogFailureKind,
  failureFrom,
  messageOf,
  operationFailure,
  operationNote,
  progressResult,
  resultBudget,
  resultEntries,
  rowText,
  runNote,
} from "./view-model";

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
/** One UM batch is bounded by the backend: at most 20 models, one short
 *  request per model, no retry, one shared 20-request budget for context. */
const MAX_BATCH_MODELS = 20;

/** The login event's additive params (ADR 0005). The view emits the fields
 *  directly; a controller caller may pass the same object as `params`. */
function loginParams(
  event: Extract<UmEvent, { type: "um.login" }>
): UmLoginParams {
  if (event.params) return event.params;
  const params: UmLoginParams = {};
  if (event.force !== undefined) params.force = event.force;
  if (event.visible !== undefined) params.visible = event.visible;
  return params;
}

export class UmDataActions extends UmDataReads {
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
        void this.runMaintenance("login", loginParams(event));
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
        this.startTest(event.models);
        return;
      case "models.stop":
        void this.cancelRun(event.runId);
        return;
      case "models.history":
        this.applyHistoryFilter(event.filter);
        return;
    }
  }

  protected async refreshCatalog(): Promise<void> {
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

  protected startTest(requested: string[]): void {
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
    const gate = this.actionEnabled("model.context_probe_run");
    if (!gate.ok) {
      this.notice("err", `无法发起测试：${gate.reason}`);
      return;
    }
    this.run = {
      id: `local-${this.now()}`,
      startedAt: this.now(),
      state: "submitting",
      stopRequested: false,
      budget: { used: 0, limit: MAX_BATCH_MODELS },
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

  protected async runTest(models: string[]): Promise<void> {
    const run = this.run;
    if (!run) return;
    const params: JsonObject = { models: { [UM_SOURCE]: models } };
    try {
      const result = await this.client.runAction(
        "model.context_probe_run",
        "model",
        params,
        {
          intent: "probe",
          timeoutMs: 1_200_000,
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

  protected async cancelRun(runId: string): Promise<void> {
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

  protected async readProgress(
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

  protected applySteps(run: UmTestRun, op: UmOperationPayload): void {
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
    this.publishRun();
  }

  protected finishRun(run: UmTestRun, result: OperationResult): void {
    const entries = resultEntries(result);
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
    run.note = runNote(result);
    const budget = resultBudget(result);
    if (run.budget && budget) run.budget = budget;
    this.runOperation = null;
    this.publishRun();
    this.recomputeCapabilities();
    void this.readOperations(true);
    this.readEvidence();
  }

  protected finishWithoutResult(run: UmTestRun, reason: string): void {
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

  protected publishRun(): void {
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

  protected async runMaintenance(
    action: MaintenanceAction,
    params: UmLoginParams = {}
  ): Promise<void> {
    if (this.state.overview.progress[action]?.state === "pending") return;
    const gate = this.actionEnabled(ACTION[action]);
    if (!gate.ok) {
      this.finishProgress(action, "failed", gate.reason);
      return;
    }
    const note =
      action === "login" && params.force === true
        ? params.visible === true
          ? "显式强制重新登录（可见窗口）"
          : "显式强制重新登录"
        : ACTION_PENDING_NOTE[action];
    this.patchOverview({
      progress: {
        ...this.state.overview.progress,
        [action]: {
          state: "pending",
          since: this.now(),
          note,
        },
      },
    });
    this.recomputeCapabilities();
    try {
      const result = await this.client.runAction(
        ACTION[action],
        action === "verify" || action === "login" ? "account" : CHAT2API,
        params,
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

  protected applyVerifyResult(result: OperationResult): void {
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

  protected finishProgress(
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

  protected afterMaintenance(action: MaintenanceAction): void {
    if (action === "verify") {
      void this.readOperations(true);
      return;
    }
    this.readService();
    this.readLogin();
    void this.readOperations(true);
  }
}
