const isObject = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === "object" && !Array.isArray(value);

const BASE = "/gw/usagesrv";

/** One share-key write outcome. `refused` is 401/403 (including the role
 *  gate's local refusal), `conflict` is the optimistic row-version 409,
 *  `unavailable` is 404/410, and `no-answer` means no HTTP answer arrived. */
export type KeyWriteOutcome =
  | { ok: true; status: number; data: Record<string, unknown> }
  | {
      ok: false;
      status: number | null;
      kind: "refused" | "conflict" | "unavailable" | "error" | "no-answer";
      reason: string;
    };

function safePath(path: string): boolean {
  const pathname = path.split("?", 1)[0];
  return (
    path.startsWith("/") &&
    !path.startsWith("//") &&
    !path.includes("\\") &&
    !pathname.split("/").some((segment) => segment === "." || segment === "..")
  );
}

/** A narrow share-key write through the A36-2 facade. The caller passes the
 *  row version; a write without one is refused here instead of sent blind. The
 *  plaintext in a create/rotate answer is returned to the caller's memory only
 *  and is never logged or put in the URL. */
export async function shareKeyWrite(
  method: "POST" | "PATCH" | "DELETE",
  path: string,
  body: Record<string, unknown>,
  timeoutMs = 20_000
): Promise<KeyWriteOutcome> {
  if (!safePath(path))
    return {
      ok: false,
      status: null,
      kind: "error",
      reason: "写入路径不符合控制台范围",
    };
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 120_000)
    return {
      ok: false,
      status: null,
      kind: "error",
      reason: "写入期限不符合范围",
    };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${BASE}${path}`, {
      method,
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const record = isObject(data) ? data : null;
      const reason =
        (record &&
          ((typeof record.reason === "string" && record.reason) ||
            (typeof record.detail === "string" && record.detail) ||
            (typeof record.error === "string" && record.error))) ||
        `HTTP ${response.status}`;
      const kind =
        response.status === 409
          ? "conflict"
          : response.status === 401 || response.status === 403
            ? "refused"
            : response.status === 404 || response.status === 410
              ? "unavailable"
              : "error";
      return { ok: false, status: response.status, kind, reason };
    }
    return {
      ok: true,
      status: response.status,
      data: isObject(data) ? data : {},
    };
  } catch (error) {
    return {
      ok: false,
      status: null,
      kind: "no-answer",
      reason: controller.signal.aborted
        ? "请求超时，结果未确认"
        : `未得到应答（${error instanceof Error ? error.message : String(error)}）`,
    };
  } finally {
    clearTimeout(timer);
  }
}

/** D18-8: end THIS login round only. The backend cleans the browser and
 *  reports `cancelled` / `no_login_in_progress`; it never stops the dedicated
 *  instance and never reopens a round by itself. */
export async function cancelBackendLogin(): Promise<KeyWriteOutcome> {
  return shareKeyWrite(
    "POST",
    "/api/console/share-management/backend-login-cancel",
    {},
    30_000
  );
}

/** D18-9: acquire the single display operator slot, or explicitly take it
 *  over. A 409 `display_operator_exists` carries the current owner so the page
 *  can ask before taking over instead of claiming it. */
export async function setDisplayOperator(
  action: "acquire" | "takeover"
): Promise<KeyWriteOutcome> {
  return shareKeyWrite(
    "POST",
    "/api/console/share-management/display-operator",
    { action },
    30_000
  );
}

/** D18-10: mint a one-time, origin-bound display grant. The page opens the
 *  returned `/display/grant#grant=...` URL; that page exchanges the grant for
 *  the HttpOnly display-session cookie. Opening `/display/` directly has no
 *  display session and is denied. */
export async function requestDisplayGrant(): Promise<KeyWriteOutcome> {
  return shareKeyWrite(
    "POST",
    "/api/console/share-management/display-grant",
    {},
    30_000
  );
}
