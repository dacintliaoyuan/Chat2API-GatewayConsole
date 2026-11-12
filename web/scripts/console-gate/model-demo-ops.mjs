// Browser review only: an in-memory simulator using generated handler shapes.
// No network, files, credentials, tasks or real client writes are possible here.
import { createHash } from "node:crypto";

export function modelDemoOps(rev) {
  const contexts = structuredClone(rev.context_overrides);
  const rows = contexts.overrides;
  let binding = null;
  const fail = (message) => {
    throw new Error(message);
  };
  const parse = (value) =>
    typeof value === "string" ? JSON.parse(value) : value;
  const inScope = (row, p) => {
    if (p.scope_mode === "all") return true;
    const sources = parse(p.sources);
    if (sources?.length && !sources.includes(row.source)) return false;
    return (
      !p.models?.[row.source]?.length ||
      p.models[row.source].includes(row.model_id)
    );
  };
  const signature = (p) =>
    JSON.stringify({
      scope_mode: p.scope_mode,
      sources: parse(p.sources),
      consumers: parse(p.consumers),
      models: p.models,
      selection: p.selection ?? null,
      rows,
    });
  const digest = (p) => createHash("sha256").update(signature(p)).digest("hex");
  return {
    $modelPending: () => ({
      pending: [],
      last_result: null,
      last_result_error: null,
      probes: {},
      catalog_counts: Object.fromEntries(
        Object.entries(rev.catalog.catalogs).map(([source, models]) => [
          source,
          models.length,
        ])
      ),
      last_status: "demo",
      last_run: null,
      bindings: [],
      paused_sources: ["m365"],
    }),
    "model.catalog": (p) => {
      const result = structuredClone(rev.catalog);
      const sources = parse(p.sources);
      if (sources)
        result.catalogs = Object.fromEntries(
          Object.entries(result.catalogs).filter(([s]) => sources.includes(s))
        );
      result.sources = Object.keys(result.catalogs);
      return result;
    },
    "model.context_overrides": () => structuredClone(contexts),
    "model.settings_save": (p) => {
      const row = rows.find(
        (r) => r.source === p.source && r.model_id === p.model_id
      );
      if (!row) fail("模拟目录中没有该模型");
      if (p.expected_draft_version !== row.settings_draft_version)
        fail("草稿版本已变化，请重新读取");
      const draft = {
        alias: null,
        contextSource: null,
        contextTokens: null,
        defaultEffort: null,
        ...(row.settings_draft ?? row.settings_applied ?? {}),
      };
      for (const field of [
        "alias",
        "contextSource",
        "contextTokens",
        "defaultEffort",
      ]) {
        if (Object.hasOwn(p, field)) draft[field] = p[field];
      }
      if (
        draft.contextTokens !== null &&
        (!Number.isSafeInteger(draft.contextTokens) || draft.contextTokens < 1)
      )
        fail("上下文必须为正整数");
      row.settings_draft = draft;
      row.settings_draft_version++;
      row.settings_held = true;
      binding = null;
      return {
        ...structuredClone(rev.settings_save),
        source: p.source,
        model_id: p.model_id,
        draft: structuredClone(draft),
        draft_version: row.settings_draft_version,
      };
    },
    "model.sync_preview": (p) => {
      if (!["all", "selected"].includes(p.scope_mode)) fail("请选择同步范围");
      if (
        p.scope_mode === "selected" &&
        !parse(p.sources)?.length &&
        !parse(p.consumers)?.length &&
        !Object.keys(p.models ?? {}).length
      )
        fail("请至少选择一个范围");
      const changes = [];
      for (const [key, version] of Object.entries(p.selection ?? {})) {
        const row = rows.find((r) => `${r.source}|${r.model_id}` === key);
        if (
          !row ||
          !inScope(row, p) ||
          !row.settings_draft ||
          row.settings_draft_version !== version
        )
          fail("草稿版本或范围已变化");
        changes.push({
          key,
          source: row.source,
          model_id: row.model_id,
          before: structuredClone(row.settings_applied),
          draft: structuredClone(row.settings_draft),
        });
      }
      const result = structuredClone(rev.sync_combined_preview);
      result.selection = p.selection ?? null;
      result.preview_token = digest(p);
      result.preview = {
        ...result.preview,
        selection: p.selection ?? {},
        preview_token: result.preview_token,
        changes,
        plan: changes.map((r) => `更新 ${r.model_id} 的已保存设置`),
        contextConflicts: [],
        overrideNotices: [],
        warnings: [],
        settingsReadback: null,
      };
      binding = { params: structuredClone(p), token: result.preview_token };
      return result;
    },
    "model.sync_apply": (p) => {
      const { preview_token, ...params } = p;
      if (
        !binding ||
        preview_token !== binding.token ||
        digest(params) !== binding.token
      )
        fail("预览已失效，请重新查看变更");
      const keys = Object.keys(params.selection ?? {});
      for (const key of keys) {
        const row = rows.find((r) => `${r.source}|${r.model_id}` === key);
        row.settings_applied = structuredClone(row.settings_draft);
        row.settings_applied_version = row.settings_draft_version;
        row.settings_draft = null;
        row.settings_held = false;
        row.effective_context =
          row.settings_applied.contextTokens ?? row.declared_context;
        row.override_context = row.settings_applied.contextTokens;
        row.effective_source = row.settings_applied.contextSource ?? "declared";
        // A simulated successful file write does not prove real client adoption.
        row.client_readback_state = "unavailable";
        row.client_context = null;
        row.client_matches_applied = null;
        row.client_readbacks = [];
      }
      binding = null;
      return {
        ...structuredClone(rev.sync_combined_apply),
        selection: params.selection ?? null,
        status: keys.length ? "changed" : "unchanged",
        summary: `离线模拟完成：${keys.length} 个设置已更新`,
        appliedDetails: [],
        pendingDetails: [],
        pendingRetirement: [],
        overrideNotices: [],
        applied: keys.map((key) => `模拟更新 ${key}`),
        pending: [],
        warnings: [],
        contextConflicts: [],
        settingsApplied: keys,
        settingsDeferred: [],
        settingsReadback: {
          keys: structuredClone(
            rows.filter((r) => keys.includes(`${r.source}|${r.model_id}`))
          ),
          clientReadbackAvailable: false,
        },
        applicationBoundary: {
          published: false,
          filesWritten: [],
          stateWritten: false,
          clientLoaded: "unverified",
          sessionAdopted: "unverified",
        },
        note: "离线模拟已完成；未写入真实客户端，未验证真实会话采用。",
      };
    },
  };
}
