import type { Transport, TransportRequest, TransportResponse } from "./types.ts";
import { ContractError } from "./normalize.ts";

/** Explicit live adapter. Constructing it does not perform a request. */
export function createFetchTransport(options: {
  fetch: typeof fetch;
  basePath?: string;
  requestTimeoutMs?: number;
}): Transport {
  const basePath = options.basePath ?? "/api/console";
  if (!basePath.startsWith("/") || basePath.startsWith("//") || /[?#\\]/.test(basePath) || basePath.split("/").some(segment => segment === "." || segment === ".."))
    throw new ContractError("basePath must be a same-origin absolute path");
  const timeoutMs = options.requestTimeoutMs ?? 15_000;
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 120_000)
    throw new ContractError("request timeout must be bounded");
  return {
    async request(request: TransportRequest): Promise<TransportResponse> {
      if (!request.path.startsWith("/") || request.path.startsWith("//") || request.path.includes("\\") || request.path.includes("#"))
        throw new ContractError("request path must be console-relative");
      const pathSegments = decodeURIComponent(request.path.split("?", 1)[0]).split("/");
      if (pathSegments.some(segment => segment === "." || segment === ".."))
        throw new ContractError("request path cannot escape the console base");
      if (request.method === "GET" && request.body !== undefined)
        throw new ContractError("GET cannot have a body");
      const controller = new AbortController();
      const forwardAbort = () => controller.abort(request.signal?.reason);
      if (request.signal?.aborted) forwardAbort();
      else request.signal?.addEventListener("abort", forwardAbort, { once: true });
      const timer = setTimeout(() => controller.abort("request_timeout"), timeoutMs);
      try {
        const response = await options.fetch(`${basePath}${request.path}`, {
          method: request.method,
          credentials: request.access === "session" ? "include" : "omit",
          signal: controller.signal,
          ...(request.method === "POST" ? {
            headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
            body: JSON.stringify(request.body ?? {}),
          } : {}),
        });
        try { return { status: response.status, body: await response.json() }; }
        catch { return { status: response.status, body: null, parseError: "invalid_json" }; }
      } finally {
        clearTimeout(timer);
        request.signal?.removeEventListener("abort", forwardAbort);
      }
    },
  };
}

export type LatestResult<T> = { state: "current"; value: T } | { state: "superseded" } | { state: "cancelled" };

/** Requests own a lane generation; abort is an optimization, never the correctness guard. */
export class LatestRequests {
  private lanes = new Map<string, { generation: number; controller: AbortController }>();
  private sequence = 0;

  async run<T>(lane: string, task: (signal: AbortSignal) => Promise<T>): Promise<LatestResult<T>> {
    this.lanes.get(lane)?.controller.abort("superseded");
    const entry = { generation: ++this.sequence, controller: new AbortController() };
    this.lanes.set(lane, entry);
    try {
      const value = await task(entry.controller.signal);
      if (this.lanes.get(lane) !== entry) return { state: "superseded" };
      if (entry.controller.signal.aborted) return { state: "cancelled" };
      return { state: "current", value };
    } catch (error) {
      if (this.lanes.get(lane) !== entry) return { state: "superseded" };
      if (entry.controller.signal.aborted) return { state: "cancelled" };
      throw error;
    } finally {
      if (this.lanes.get(lane) === entry) this.lanes.delete(lane);
    }
  }

  cancel(lane: string): void { this.lanes.get(lane)?.controller.abort("cancelled"); }
  cancelAll(): void { for (const lane of this.lanes.keys()) this.cancel(lane); }
}

export function abortableSleep(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new Error("cancelled")); return; }
    const aborted = () => { clearTimeout(timer); signal?.removeEventListener("abort", aborted); reject(new Error("cancelled")); };
    const timer = setTimeout(() => { signal?.removeEventListener("abort", aborted); resolve(); }, milliseconds);
    signal?.addEventListener("abort", aborted, { once: true });
  });
}
