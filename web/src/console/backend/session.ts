import type { BackendClient } from "./client.ts";
import type {
  LogPage,
  ModelDetail,
  OperationResult,
  SettingsApply,
  SettingsPreview,
  ShareUsage,
} from "./types.ts";
import { joinModelDetails, normalizeSettingsApply } from "./normalize.ts";
import { LatestRequests } from "./transport.ts";

export interface ResourceState<T> {
  state: "idle" | "loading" | "ready" | "error";
  /** A failed refresh retains the last observed value and marks it stale. */
  value: T | null;
  stale: boolean;
  reason: string | null;
  updatedAt: number | null;
}

export interface SettingsApplyReadback {
  operation: OperationResult;
  application: SettingsApply | null;
  readback: "not_requested" | "ready" | "failed";
  models: ModelDetail[] | null;
  reason: string | null;
}

function initial<T>(): ResourceState<T> {
  return {
    state: "idle",
    value: null,
    stale: false,
    reason: null,
    updatedAt: null,
  };
}

/** Functional state owner for thin renderers. It changes no style, markup or layout. */
export class BackendSession {
  readonly requests = new LatestRequests();
  readonly client: BackendClient;
  private readonly now: () => number;
  private disposed = false;
  models: ResourceState<ModelDetail[]> = initial();
  shareUsage: ResourceState<ShareUsage> = initial();
  logs: ResourceState<LogPage> = initial();

  constructor(client: BackendClient, now: () => number = Date.now) {
    this.client = client;
    this.now = now;
  }

  private async refresh<T>(
    lane: string,
    state: ResourceState<T>,
    task: (signal: AbortSignal) => Promise<T>
  ): Promise<boolean> {
    if (this.disposed) return false;
    state.state = "loading";
    state.reason = null;
    try {
      const result = await this.requests.run(lane, task);
      if (result.state === "superseded") return false;
      if (result.state === "cancelled") {
        state.state = state.value === null ? "idle" : "ready";
        return false;
      }
      state.state = "ready";
      state.value = result.value;
      state.stale = false;
      state.reason = null;
      state.updatedAt = this.now();
      return true;
    } catch (error) {
      state.state = "error";
      state.stale = state.value !== null;
      state.reason = error instanceof Error ? error.message : "unknown_error";
      return false;
    }
  }

  async refreshModels(signal?: AbortSignal): Promise<boolean> {
    if (signal?.aborted || this.disposed) return false;
    const cancelled = () => this.requests.cancel("models");
    signal?.addEventListener("abort", cancelled, { once: true });
    try {
      return await this.refresh("models", this.models, async (localSignal) => {
        const [catalog, contexts, evidence] = await Promise.all([
          this.client.readCatalog(localSignal),
          this.client.readContexts(localSignal),
          this.client.readContextEvidence(localSignal),
        ]);
        return joinModelDetails(catalog, contexts, evidence);
      });
    } finally {
      signal?.removeEventListener("abort", cancelled);
    }
  }

  refreshShareUsage(): Promise<boolean> {
    return this.refresh("share-usage", this.shareUsage, async (signal) => {
      const result = await this.client.readShareUsage(signal);
      if (result.state !== "ready" || !result.value)
        throw new Error(`${result.state}: ${result.reason ?? "read_failed"}`);
      return result.value;
    });
  }

  refreshLogs(
    query: Parameters<BackendClient["readLogs"]>[0]
  ): Promise<boolean> {
    // Switching source/filter owns the same lane, invalidating every older response.
    const snapshot = structuredClone(query);
    return this.refresh("logs", this.logs, (signal) =>
      this.client.readLogs(snapshot, signal)
    );
  }

  /** Application and actual readback are separate facts, including successful deferral. */
  async applySettingsAndReadback(
    preview: SettingsPreview,
    selection: Readonly<Record<string, number>>,
    signal?: AbortSignal
  ): Promise<SettingsApplyReadback> {
    if (this.disposed)
      return {
        operation: {
          state: "refused",
          operation: null,
          operationId: null,
          reason: "session_disposed",
        },
        application: null,
        readback: "not_requested",
        models: null,
        reason: "session_disposed",
      };
    const operation = await this.client.applySettings(
      preview,
      selection,
      signal
    );
    if (operation.state !== "succeeded" || !operation.operation?.result) {
      return {
        operation,
        application: null,
        readback: "not_requested",
        models: null,
        reason: operation.reason,
      };
    }
    const application = normalizeSettingsApply(operation.operation.result);
    if (!application.applied.length) {
      return {
        operation,
        application,
        readback: "not_requested",
        models: null,
        reason: application.note,
      };
    }
    if (signal?.aborted || this.disposed) {
      return {
        operation,
        application,
        readback: "failed",
        models: null,
        reason: this.disposed
          ? "session_disposed_after_apply"
          : "local_wait_cancelled_after_apply",
      };
    }
    const readback = await this.refreshModels(signal);
    return {
      operation,
      application,
      readback: readback ? "ready" : "failed",
      models: readback ? this.models.value : null,
      reason: readback ? null : this.models.reason,
    };
  }

  dispose(): void {
    this.disposed = true;
    this.requests.cancelAll();
  }
}
