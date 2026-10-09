/** Public distribution boundary. Every live operation stays in this product's scope. */
export const PUBLIC_ACTIONS = new Set([
  "account.login", "account.um_status", "account.um_verify",
  "service.start", "service.stop", "service.restart",
  "model.catalog", "model.context_overrides", "model.context_probe_preview", "model.context_probe_run",
  "model.settings_save", "model.settings_preview", "model.settings_apply", "model.speedtest",
  "model.sync_preview", "model.sync_apply", "model.sync_pending", "model.sync_history", "model.task_status",
  "share.start", "share.stop", "share.estop", "share.retry_stop", "share.backend_login",
  "share.backend_reauthenticate", "share.backend_restart", "share.gateway_restart", "share.diagnostics",
]);

export const PUBLIC_LOG_SOURCES = new Set(["chat2api", "console", "console_operations", "share", "share-backend"]);

function list(value: unknown, expected: string): void {
  let parsed: unknown;
  try { parsed = typeof value === "string" ? JSON.parse(value) : value; }
  catch { throw new Error("公开版本的同步范围编码无效"); }
  if (!Array.isArray(parsed) || parsed.length !== 1 || parsed[0] !== expected)
    throw new Error("公开版本的同步范围必须限定为 chat2api → ZCode");
}

export function assertPublicOperation(action: string, target: string, params: Record<string, unknown>): void {
  if (!PUBLIC_ACTIONS.has(action)) throw new Error("公开版本未提供该动作");
  if (action.startsWith("service.") && target !== "chat2api") throw new Error("公开版本只管理 chat2api");
  if (action.startsWith("account.") && target !== "UM" && !(action === "account.login" && target === ""))
    throw new Error("公开版本只管理当前 chat2api 上游账号");
  if (action.startsWith("model.") && target !== "model") throw new Error("模型动作目标无效");
  if ("source" in params && params.source !== "um") throw new Error("公开版本只支持当前 chat2api 上游的模型");
  if (params.selection && (typeof params.selection !== "object" || Array.isArray(params.selection)
      || Object.keys(params.selection).some(key => !key.startsWith("um|")))) throw new Error("模型草稿范围无效");
  if (action === "model.sync_preview" || action === "model.sync_apply") {
    if (params.scope_mode !== "selected") throw new Error("同步必须使用明确范围");
    list(params.sources, "um");
    list(params.consumers, "ZCode");
    if (params.models !== undefined && (typeof params.models !== "object" || params.models === null || Array.isArray(params.models)
        || Object.keys(params.models).some(source => source !== "um"))) throw new Error("模型选择范围无效");
  }
}

/** Same-origin reads and explicitly scoped writes; construction never calls a backend. */
export const consoleFetch: typeof fetch = async (input, init) => {
  const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const url = new URL(raw, window.location.origin);
  if (url.origin !== window.location.origin || !(url.pathname === "/api/summary" || url.pathname.startsWith("/api/console/")))
    throw new Error("控制台请求必须使用同源 API");
  const service = url.searchParams.get("service");
  if (service !== null && service !== "chat2api") throw new Error("公开版本只读取 chat2api 连接信息");
  const source = url.searchParams.get("source");
  if (source !== null && !PUBLIC_LOG_SOURCES.has(source)) throw new Error("日志来源不在公开版本范围内");
  const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
  if (method === "POST" && url.pathname === "/api/console/operations") {
    if (typeof init?.body !== "string") throw new Error("操作请求正文无效");
    const body = JSON.parse(init.body) as { action: string; target: string; params?: Record<string, unknown> };
    assertPublicOperation(body.action, body.target, body.params ?? {});
  }
  return window.fetch(input, init);
};
