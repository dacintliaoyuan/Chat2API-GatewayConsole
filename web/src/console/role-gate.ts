/** Console role gate (ADR 0025: one front end, two consoles, role chosen at
 *  build time).
 *
 *  The build fixes the role; the page opens writes only once the backend's own
 *  report agrees with it:
 *  - cloud: a `role: "cloud"` marker — GET /api/summary, or the body of a read
 *    the cloud refuses (503 capability_not_assembled).
 *  - local: the local backend sends no role marker (a backend gap, recorded in
 *    the handoff), so it is confirmed by exclusion: no cloud marker AND the
 *    capability table lists actions only the local registry assembles.
 *  - any cloud marker the local build sees, or local-only actions the cloud
 *    build sees, is a mismatch: the page stays read-only for its lifetime and
 *    says why.
 *  The role is never inferred from the page address: a cloud console reached
 *  through an SSH forward is on localhost too.
 *
 *  Until confirmed, every non-GET/HEAD request to /gw/usagesrv/ is answered in
 *  the browser with a 403 that names the reason and is never sent. This is a
 *  front-end safety rail, not a replacement for the server's own checks. */

export type ConsoleRole = "local" | "cloud";
export type RoleState = "checking" | "confirmed" | "mismatch" | "unconfirmed";
export interface RoleVerdict {
  readonly state: RoleState;
  /** Plain-language reason, shown next to the environment marker. */
  readonly reason: string;
}

export const BUILD_ROLE: ConsoleRole = __CONSOLE_ROLE__;
export const ROLE_LABEL: Readonly<Record<ConsoleRole, string>> = {
  local: "本地控制台",
  cloud: "云端控制台",
};

/** Action families only the local registry assembles (console_adapters.build_registry:
 *  Qoder / CLIProxy / model sync); the cloud registry never imports them. */
const LOCAL_ONLY_ACTION_PREFIXES = [
  "model.",
  "qoder.",
  "qoderglobal.",
  "cliproxy.",
];
const SCOPE = "/gw/usagesrv/";
const SUMMARY_PATH = "/api/summary";
const ACTIONS_PATH = "/api/console/actions";
export const ROLE_GATE_ERROR = "console_role_gate";

function roleMarker(body: unknown): ConsoleRole | null {
  if (!body || typeof body !== "object") return null;
  const record = body as {
    role?: unknown;
    assembly?: { role?: unknown } | null;
  };
  const role = record.role ?? record.assembly?.role;
  return role === "cloud" || role === "local" ? role : null;
}

export class RoleGate {
  private cloudMarker = false;
  private localMarker = false;
  /** null = the capability table has not been read successfully yet. */
  private localOnlyAction: string | null | undefined = undefined;
  private summaryWithoutRole = false;
  private lastFailure: string | null = null;
  private sticky: RoleVerdict | null = null;
  private listeners = new Set<(verdict: RoleVerdict) => void>();
  private current: RoleVerdict;

  constructor(readonly role: ConsoleRole) {
    this.current = this.evaluate();
  }

  verdict(): RoleVerdict {
    return this.current;
  }
  writable(): boolean {
    return this.current.state === "confirmed";
  }

  onChange(listener: (verdict: RoleVerdict) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** One answered request under /gw/usagesrv (path without that prefix). */
  observe(path: string, status: number, body: unknown): void {
    const marker = roleMarker(body);
    if (marker === "cloud") this.cloudMarker = true;
    if (marker === "local") this.localMarker = true;
    if (path === ACTIONS_PATH) {
      const actions =
        status === 200 && body && typeof body === "object"
          ? (body as { actions?: unknown }).actions
          : undefined;
      if (Array.isArray(actions)) {
        const names = actions
          .map((row) =>
            row && typeof row === "object"
              ? (row as { action?: unknown }).action
              : undefined
          )
          .filter((name): name is string => typeof name === "string");
        this.localOnlyAction =
          names.find((name) =>
            LOCAL_ONLY_ACTION_PREFIXES.some((prefix) => name.startsWith(prefix))
          ) ?? null;
      } else {
        this.lastFailure = `能力表读取失败（HTTP ${status}）`;
      }
    }
    if (path === SUMMARY_PATH) {
      if (status === 200 && body && typeof body === "object")
        this.summaryWithoutRole = marker === null;
      else if (status !== 200)
        this.lastFailure = `读取 ${SUMMARY_PATH} 失败（HTTP ${status}）`;
    }
    this.publish();
  }

  /** A request that never got an answer (network, timeout, abort). */
  observeFailure(path: string, reason: string): void {
    if (path !== SUMMARY_PATH && path !== ACTIONS_PATH) return;
    this.lastFailure = `读取 ${path} 未得到应答（${reason}）`;
    this.publish();
  }

  private publish(): void {
    const next = this.evaluate();
    if (
      next.state === this.current.state &&
      next.reason === this.current.reason
    )
      return;
    this.current = next;
    for (const listener of this.listeners) listener(next);
  }

  private evaluate(): RoleVerdict {
    if (this.sticky) return this.sticky;
    const verdict =
      this.role === "cloud" ? this.evaluateCloud() : this.evaluateLocal();
    // A mismatch is evidence about which backend answered; a later quiet read
    // does not undo it.
    if (verdict.state === "mismatch") this.sticky = verdict;
    return verdict;
  }

  private evaluateCloud(): RoleVerdict {
    if (this.localMarker || this.localOnlyAction) {
      return {
        state: "mismatch",
        reason: this.localOnlyAction
          ? `后端能力表含本地专属动作（${this.localOnlyAction}）：这是本地后端，不是云端 API 服务，本页只读。`
          : "后端回报 local 角色：这是本地后端，不是云端 API 服务，本页只读。",
      };
    }
    if (this.cloudMarker)
      return {
        state: "confirmed",
        reason: "后端回报 cloud 角色，与本页一致。",
      };
    if (this.summaryWithoutRole)
      return {
        state: "unconfirmed",
        reason:
          "后端没有回报 cloud 角色，无法确认是云端 API 服务；写操作保持关闭。",
      };
    if (this.lastFailure)
      return {
        state: "unconfirmed",
        reason: `${this.lastFailure}；确认后端角色前写操作保持关闭。`,
      };
    return {
      state: "checking",
      reason: "正在核对后端角色；确认前写操作保持关闭。",
    };
  }

  private evaluateLocal(): RoleVerdict {
    if (this.cloudMarker) {
      return {
        state: "mismatch",
        reason:
          "后端回报 cloud 角色：这是云端 API 服务的后端，本页是本地控制台，只读。",
      };
    }
    if (this.localMarker)
      return {
        state: "confirmed",
        reason: "后端回报 local 角色，与本页一致。",
      };
    if (this.localOnlyAction)
      return {
        state: "confirmed",
        reason: "后端能力表含本地专属动作且没有 cloud 标记，确认是本地后端。",
      };
    if (this.localOnlyAction === null)
      return {
        state: "unconfirmed",
        reason:
          "后端能力表没有本地专属动作，无法确认是本地后端；写操作保持关闭。",
      };
    if (this.lastFailure)
      return {
        state: "unconfirmed",
        reason: `${this.lastFailure}；确认后端角色前写操作保持关闭。`,
      };
    return {
      state: "checking",
      reason: "正在核对后端角色；确认前写操作保持关闭。",
    };
  }
}

function requestUrl(input: RequestInfo | URL): URL | null {
  try {
    const href =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    return new URL(href, location.href);
  } catch {
    return null;
  }
}

/** Route every same-origin /gw/usagesrv/ request through the gate: writes are
 *  refused locally until the role is confirmed, and the answers that carry
 *  role evidence are read on a clone (the caller's response is untouched). */
export function installRoleGate(gate: RoleGate): void {
  const send = window.fetch.bind(window);
  window.fetch = async (
    input: RequestInfo | URL,
    init?: RequestInit
  ): Promise<Response> => {
    const url = requestUrl(input);
    if (
      !url ||
      url.origin !== location.origin ||
      !url.pathname.startsWith(SCOPE)
    )
      return send(input, init);
    const path = url.pathname.slice(SCOPE.length - 1);
    const method = String(
      init?.method ?? (input instanceof Request ? input.method : "GET")
    ).toUpperCase();
    if (method !== "GET" && method !== "HEAD" && !gate.writable()) {
      const verdict = gate.verdict();
      return new Response(
        JSON.stringify({
          error: ROLE_GATE_ERROR,
          reason: `${ROLE_LABEL[gate.role]}未确认后端角色，请求未发送：${verdict.reason}`,
        }),
        {
          status: 403,
          headers: { "Content-Type": "application/json; charset=utf-8" },
        }
      );
    }
    let response: Response;
    try {
      response = await send(input, init);
    } catch (error) {
      gate.observeFailure(
        path,
        error instanceof DOMException && error.name === "AbortError"
          ? "已取消或超时"
          : "网络中断"
      );
      throw error;
    }
    if (
      path === SUMMARY_PATH ||
      path === ACTIONS_PATH ||
      response.status >= 400
    ) {
      response
        .clone()
        .json()
        .then((body) => gate.observe(path, response.status, body))
        .catch(() => gate.observe(path, response.status, null));
    }
    return response;
  };
}
