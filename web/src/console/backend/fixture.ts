import type { ActionCapability, ConnectionKeyState, JsonObject, OperationState, Transport, TransportRequest, TransportResponse } from "./types.ts";
import { ContractError, object, text } from "./normalize.ts";

export interface FixtureActionResult {
  states?: OperationState[];
  result?: JsonObject | null;
  error?: JsonObject | null;
  /** Model a definitive submit rejection rather than a fictitious terminal success. */
  rejection?: TransportResponse;
  /** Model transport uncertainty; this never triggers a resubmission. */
  submitError?: string;
  pollError?: string;
}

export interface FixtureScenario {
  name: string;
  /** Use the sanitized, generated registry fixture; unsupported actions stay absent. */
  actions: ActionCapability[];
  reads: Record<string, TransportResponse>;
  operations: Record<string, FixtureActionResult[]>;
}

interface FixtureOperation {
  id: string;
  action: string;
  target: string;
  reply: FixtureActionResult;
  states: OperationState[];
  at: number;
  cancelRequested: boolean;
}

function clone<T>(value: T): T { return structuredClone(value); }

/** Entirely in memory; this transport has no fetch, process, filesystem, or live proxy. */
export class FixtureTransport implements Transport {
  readonly calls: TransportRequest[] = [];
  readonly scenarioName: string;
  private readonly scenario: FixtureScenario;
  private counters = new Map<string, number>();
  private running = new Map<string, FixtureOperation>();
  private sequence = 0;

  constructor(scenario: FixtureScenario) {
    this.scenario = clone(scenario);
    this.scenarioName = scenario.name;
  }

  async request(request: TransportRequest): Promise<TransportResponse> {
    if (request.signal?.aborted) throw new Error("fixture_cancelled");
    this.calls.push({ ...request, body: request.body ? clone(request.body) : undefined });
    const path = request.path.split("?", 1)[0];
    if (request.method === "GET" && path === "/actions") {
      return { status: 200, body: { actions: this.scenario.actions.map(row => ({
        action: row.action, enabled: row.enabled, cancelable: row.cancelable,
        disabled_reason: row.disabledReason, description: row.description,
      })) } };
    }
    if (request.method === "POST" && path === "/operations") {
      const action = text(request.body?.action);
      const target = text(request.body?.target);
      if (!action || target === null || !object(request.body?.params))
        return { status: 400, body: { error: "invalid_params" } };
      const capability = this.scenario.actions.find(row => row.action === action);
      if (!capability) return { status: 400, body: { error: "invalid_action" } };
      if (!capability.enabled) return { status: 409, body: { error: "action_disabled", reason: capability.disabledReason } };
      const index = this.counters.get(action) ?? 0;
      this.counters.set(action, index + 1);
      const reply = this.scenario.operations[action]?.[index];
      if (!reply) return { status: 503, body: { error: "fixture_scenario_exhausted" } };
      if (reply.submitError) throw new Error(reply.submitError);
      if (reply.rejection) return clone(reply.rejection);
      const states: OperationState[] = reply.states?.length ? [...reply.states] : ["queued", "running", "succeeded"];
      const operation = {
        id: `fixture-${++this.sequence}`, action, target, reply, states,
        at: 0, cancelRequested: false,
      };
      this.running.set(operation.id, operation);
      return { status: 202, body: { operation: this.view(operation) } };
    }
    const match = /^\/operations\/([^/]+)(\/cancel)?$/.exec(path);
    if (match) {
      const operation = this.running.get(decodeURIComponent(match[1]));
      if (!operation) return { status: 404, body: { error: "not_found" } };
      if (request.method === "POST" && match[2]) {
        if (!this.scenario.actions.find(row => row.action === operation.action)?.cancelable)
          return { status: 409, body: { error: "not_cancelable" } };
        operation.cancelRequested = true;
        // A cooperative cancel request is acknowledged; terminal cancellation is observed by polling.
        operation.states = [operation.states[operation.at], "cancelled"];
        operation.at = 0;
        return { status: 200, body: { operation: this.view(operation) } };
      }
      if (request.method !== "GET" || match[2]) throw new ContractError("fixture operation method/path is invalid");
      if (operation.reply.pollError) throw new Error(operation.reply.pollError);
      operation.at = Math.min(operation.at + 1, operation.states.length - 1);
      return { status: 200, body: { operation: this.view(operation) } };
    }
    const read = this.scenario.reads[request.path] ?? this.scenario.reads[path];
    if (request.method === "GET" && read) return clone(read);
    return { status: 404, body: { error: "fixture_route_not_defined" } };
  }

  private view(operation: FixtureOperation): JsonObject {
    const state = operation.states[operation.at];
    const finished = ["succeeded", "failed", "cancelled", "interrupted"].includes(state);
    return {
      id: operation.id, action: operation.action, target: operation.target, state,
      resource: `fixture:${operation.action}`, generation: 1,
      created_at: "2026-10-02T12:00:00+08:00", started_at: state === "queued" ? null : "2026-10-02T12:00:01+08:00",
      finished_at: finished ? "2026-10-02T12:00:02+08:00" : null,
      cancel_requested: operation.cancelRequested, steps: [],
      result: state === "succeeded" ? clone(operation.reply.result ?? {}) : null,
      error: state === "failed" || state === "interrupted" ? clone(operation.reply.error ?? { code: "fixture_failure" }) : null,
      idempotency_key: null,
    };
  }
}

// Synthetic chat2api payloads; no real credentials or network access.
export const FIXTURE_CONNECTION_KEY = "fixture-connection-key-not-real";
export const FIXTURE_CONNECTION_STATES: readonly ConnectionKeyState[] = ["present", "absent", "needs_key_unconfigured", "unreadable", "unknown"];
export function gatewayConnectionFixtureBody(state: ConnectionKeyState, overrides: JsonObject = {}): JsonObject {
  if (!FIXTURE_CONNECTION_STATES.includes(state)) throw new ContractError("unknown connection fixture state");
  return { ok: true, service: "chat2api", display_name: "chat2api", port: 8000,
    health_path: "/health", health_path_source: "descriptor", base_url: "http://127.0.0.1:8000/v1",
    protocol: "openai-compatible", protocol_path: "/v1", runtime_state: "Running", runtime_healthy: true,
    auth: state === "absent" ? "loopback_no_key" : "bearer", key_state: state,
    key_plaintext: state === "present" ? FIXTURE_CONNECTION_KEY : null,
    key_source: "env:CHAT2API_LOCAL_API_KEY", key_source_state: state === "needs_key_unconfigured" ? "absent" : state,
    key_in_use: "startup_env", reason: "合成夹具：凭据状态仅用于离线展示，不代表真实实例。",
    evidence: ["合成夹具"], checked_epoch: 1790920800, ...clone(overrides) };
}
export function gatewayConnectionFixtureResponse(service: string, state: ConnectionKeyState): TransportResponse {
  return { status: 200, body: gatewayConnectionFixtureBody(state, { service }) };
}
export function gatewayConnectionFixtureReads(): Record<string, TransportResponse> {
  return { ["/gateway-connection?" + new URLSearchParams({ service: "chat2api" })]: gatewayConnectionFixtureResponse("chat2api", "absent") };
}
export function gatewayConnectionFixtureScenario(name = "offline-connection-contract"): FixtureScenario {
  return { name, actions: [], reads: gatewayConnectionFixtureReads(), operations: {} };
}
