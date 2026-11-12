// Console gate - fixture states: renders the console states that only a POST (or a
// failing read) can produce, without touching any service. Moved from
// output/redesign-pr11/fixtures/harness.mjs (2026-10-01) into the repo.
//
// Isolation: the page is opened on http://127.0.0.1:65099 (nothing listens there)
// and EVERY request is answered by this module - static files from the build
// directory, /gw/usagesrv reads from the fixtures (plus per-scenario overrides), and
// the operation POSTs from scenario fixtures. route.continue() is never called;
// any request it cannot answer is logged as `unanswered` and fails the run.
import { readFileSync, existsSync, statSync, mkdirSync } from "node:fs";
import { extname, join, resolve } from "node:path";
import { loadPlaywright, navigateTo, paths } from "./core.mjs";
import { makeReader } from "./fixture-server.mjs";
// Rev18 客户端连接读面：选中网关时会发起一次只读 /api/console/gateway-connection，
// 情景必须回答它。夹具来自 src/console/backend/fixture.ts（与离线契约用例同一份
// 合成值：发明出来的 service / 端口 / 路径 / 哨兵 Key，不是真实凭据）。
import { gatewayConnectionFixtureReads } from "../../src/console/backend/fixture.ts";

const ORIGIN = "http://127.0.0.1:65099";
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".css": "text/css",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".json": "application/json",
  ".webmanifest": "application/manifest+json",
  ".png": "image/png",
  ".txt": "text/plain; charset=utf-8",
  ".mp3": "audio/mpeg",
  ".ogg": "audio/ogg",
  ".wav": "audio/wav",
  ".ico": "image/x-icon",
  ".md": "text/markdown; charset=utf-8",
};

/** R3 share page: enter one of the three real tabs with a click and wait for
 *  its panel. The overview and the address line stay outside the tab panels. */
async function openShareTab(page, id) {
  const tab = page.locator(`[role="tab"][data-wb-share-tab="${id}"]`);
  await tab.click();
  await page
    .locator(`[data-wb-share-tabpanel="${id}"]`)
    .waitFor({ state: "visible" });
  if ((await tab.getAttribute("aria-selected")) !== "true")
    throw Error(`Share tab ${id} did not become selected`);
}

/** Expand one of the share page's secondary folds by its data-wb-share-panel id. */
async function openShareFold(page, id) {
  const fold = page.locator(`details[data-wb-share-panel="${id}"]`);
  if ((await fold.count()) && (await fold.getAttribute("open")) === null) {
    await fold.locator(":scope > summary").click();
  }
  if ((await fold.count()) && (await fold.getAttribute("open")) === null)
    throw Error(`Share fold ${id} did not expand`);
}

/** Run the fixture-state scenarios (all, or `names`) in both themes; screenshots go
 *  to `<out>/states/`. */
export async function runStates({
  out,
  build = paths.candidateDir,
  fixtures,
  names = null,
  themes = ["light", "dark"],
  viewport = { width: 1600, height: 900 },
}) {
  const { chromium } = await loadPlaywright();
  const BUILD = resolve(build);
  const SHOTS = resolve(out, "states");
  mkdirSync(SHOTS, { recursive: true });
  const readFixture = makeReader(fixtures);
  const NOW = Math.floor(Date.now() / 1000);
  const health = (overrides, checkedAt = NOW) => ({
    status: 200,
    body: {
      checked_at: checkedAt,
      cached: false,
      services: fixtures["/api/console/health"].body.services.map((s) => ({
        ...s,
        ...(overrides[s.id] ?? {}),
      })),
    },
  });
  const runtime = (overrides) => ({
    status: 200,
    body: {
      ...fixtures["/api/console/services"].body,
      services: fixtures["/api/console/services"].body.services.map((s) => ({
        ...s,
        ...(overrides[s.key] ?? {}),
      })),
    },
  });
  const QUOTA_RESULT = {
    quota: {
      add_on: {
        percentage: 24,
        remaining: 380,
        total: 500,
        unit: "credits",
        used: 120,
      },
      exceeded: false,
      expires_at: 253402214400000,
      organization: null,
      plan: { percentage: 0, remaining: 0, total: 0, unit: "credits", used: 0 },
      session: null,
    },
    checked_epoch: NOW,
  };
  const OPS_LIST = {
    status: 200,
    body: {
      operations: [
        {
          id: "fx-op-1",
          action: "service.restart",
          target: "chat2api",
          state: "failed",
          created_at: NOW - 120,
          cancel_requested: false,
          result: null,
          error: {
            code: "restart_timeout",
            message: "重启后 30 秒内端口未恢复监听（夹具）",
          },
        },
        {
          id: "fx-op-2",
          action: "account.um_verify",
          target: "UM",
          state: "failed",
          created_at: NOW - 300,
          cancel_requested: false,
          result: null,
          error: {
            code: "upstream_unauthorized",
            message: "上游返回 401，凭据失效（夹具）",
          },
        },
        {
          id: "fx-op-3",
          action: "model.sync_apply",
          target: "model",
          state: "failed",
          created_at: NOW - 600,
          cancel_requested: false,
          result: null,
          error: { code: "lock_busy", message: "另一次同步持有写锁（夹具）" },
        },
        {
          id: "fx-op-4",
          action: "service.start",
          target: "CLIProxy",
          state: "cancelled",
          created_at: NOW - 900,
          cancel_requested: true,
          result: null,
          error: null,
        },
        {
          id: "fx-op-5",
          action: "share.stop",
          target: "share",
          state: "running",
          created_at: NOW - 30,
          cancel_requested: false,
          result: null,
          error: null,
        },
        {
          id: "fx-op-6",
          action: "model.speedtest",
          target: "model",
          state: "queued",
          created_at: NOW - 10,
          cancel_requested: false,
          result: null,
          error: null,
        },
        {
          id: "fx-op-7",
          action: "qoderglobal.quota",
          target: "QoderGlobal",
          state: "succeeded",
          created_at: NOW - 1200,
          cancel_requested: false,
          result: QUOTA_RESULT,
          error: null,
        },
        {
          id: "fx-op-8",
          action: "account.um_status",
          target: "UM",
          state: "succeeded",
          created_at: NOW - 1500,
          cancel_requested: false,
          result: { authenticated: true, checked_epoch: NOW - 1500 },
          error: null,
        },
        // 2026-10-22 维护者截图里的真实形状：同一动作的长原因（不是夹具里的短句）。
        // 它在 1536×864 下曾只剩 108px / 12 行，在 1180 下被压到 9px / 2042px。
        {
          id: "fx-op-9",
          action: "account.login",
          target: "UM",
          state: "failed",
          created_at: NOW - 60,
          cancel_requested: false,
          result: null,
          error: {
            code: "status_unreachable",
            message:
              "无法确认当前认证，暂不打开登录：查询失败：chat2api 服务不可达，核验状态未知（未知 ≠ 失效）；chat2api 原本运行，未执行启停，服务保持运行",
          },
        },
      ],
    },
  };
  /** Fail-group reading geometry (2026-10-22): every expanded detail row must
   *  keep the reason as prose on its own full-width line, and neither the row nor
   *  the header may paint past its own box. Before the fix 1536×864 measured
   *  108/327 px (12 lines) for the reason and 1180 measured 9.4/228.9 px with a
   *  2042 px row plus a header whose text sat 5 px outside the button. */
  const FAIL_LAYOUT = () => {
    document.querySelectorAll(".wb-fail-head").forEach((head) => {
      if (head.getAttribute("aria-expanded") === "false") head.click();
    });
    const contentWidth = (el) => {
      const cs = getComputedStyle(el);
      return (
        el.getBoundingClientRect().width -
        parseFloat(cs.paddingLeft) -
        parseFloat(cs.paddingRight)
      );
    };
    return {
      heads: [...document.querySelectorAll(".wb-fail-head")].map((head) => ({
        cw: head.clientWidth,
        sw: head.scrollWidth,
      })),
      rows: [...document.querySelectorAll(".wb-fail-detail .wb-op")].map(
        (row) => {
          const b = row.querySelector("b");
          const br = b ? b.getBoundingClientRect() : null;
          return {
            contentW: contentWidth(row),
            msgW: br ? br.width : 0,
            cw: row.clientWidth,
            sw: row.scrollWidth,
            lines: br
              ? Math.max(
                  1,
                  Math.round(
                    br.height /
                      (parseFloat(getComputedStyle(b).lineHeight) || 20)
                  )
                )
              : 0,
          };
        }
      ),
    };
  };
  const validateFailLayout = (probe) => {
    const errors = [];
    if (!probe?.rows?.length) return ["失败分组没有展开出详情行"];
    for (const row of probe.rows) {
      if (row.sw > row.cw + 1)
        errors.push(`失败详情行横向溢出 ${row.sw} > ${row.cw}`);
      if (row.msgW < row.contentW * 0.7)
        errors.push(
          `失败原因列只占 ${Math.round(row.msgW)} / ${Math.round(row.contentW)} px（${row.lines} 行）`
        );
    }
    for (const head of probe.heads) {
      if (head.sw > head.cw + 1)
        errors.push(`失败分组头横向溢出 ${head.sw} > ${head.cw}`);
    }
    return errors;
  };
  const SHARE_STATUS = {
    status: 200,
    body: {
      schema_version: 1,
      share_max_concurrency: 64,
      desired_generation: 5,
      applied_generation: 5,
      in_sync: true,
      pending_revocation: false,
      active_keys: 3,
      total_keys: 4,
      broker: { running: true, version: "fixture", pid: 4242 },
      last_publication_failure: null,
      blocked_publications: [],
      blocked: false,
      share_url: "https://share.fixture.invalid/",
      funnel_on: true,
      gateway_running: true,
      account: {
        running: true,
        valid: true,
        checked_at: NOW - 60,
        failure: null,
      },
      upstream: { reachable: true },
      last_check_at: NOW - 60,
      last_failure: null,
    },
  };
  const SHARE_KEYS = {
    status: 200,
    body: {
      keys: [
        {
          id: "fx-key-a",
          name: "夹具 A",
          masked: "um-share-****FX01",
          plaintext_viewable: true,
          enabled: true,
          max_concurrency: 8,
          row_version: 3,
          created_at: "2026-09-22T09:58:57Z",
        },
        {
          id: "fx-key-b",
          name: "夹具 B",
          masked: "um-share-****FX02",
          plaintext_viewable: false,
          enabled: true,
          max_concurrency: 16,
          row_version: 1,
          created_at: "2026-09-24T06:48:58Z",
        },
        {
          id: "fx-key-c",
          name: "夹具 C（停用）",
          masked: "um-share-****FX03",
          plaintext_viewable: true,
          enabled: false,
          max_concurrency: 4,
          row_version: 7,
          created_at: "2026-09-26T01:02:03Z",
        },
        // R3: the 1600×900 first screen must exercise four keys, not only the
        // three that the previous compact layout happened to fit.
        {
          id: "fx-key-d",
          name: "夹具 D",
          masked: "um-share-****FX04",
          plaintext_viewable: false,
          enabled: true,
          max_concurrency: 32,
          row_version: 2,
          created_at: "2026-09-27T03:04:05Z",
        },
      ],
    },
  };
  // S5 公网巡检读夹具（契约 §5.1）：形状照 share_public_supervision.read_status()，
  // 全部是合成值；默认 healthy，专用场景再覆盖成全部失败/需人工/预算耗尽。
  const PUBLIC_ENTRY = (state, reason, ip) => ({
    host: "share.fixture.invalid",
    ip,
    state,
    dns: "ok",
    tls: state === "ok" ? "ok" : "failed",
    certificate: state === "ok" ? "ok" : "not_checked",
    reason,
    elapsed_ms: 42,
    checked_at_epoch: NOW - 20,
  });
  const PUBLIC_DNS = (overrides = {}) => ({
    version: 1,
    checked_at_utc: new Date((NOW - 20) * 1000).toISOString(),
    checked_at_epoch: NOW - 20,
    state: "ok",
    reason: "ok",
    addresses: ["203.0.113.10"],
    addresses_truncated: false,
    ttl_s: 60,
    negative_ttl_s: null,
    sources: [
      {
        source: "cloudflare",
        state: "ok",
        status: 0,
        classification: "ok",
        addresses: ["203.0.113.10"],
        addresses_truncated: false,
        ttl_s: 60,
        negative_ttl_s: null,
        checked_at_epoch: NOW - 20,
        elapsed_ms: 20,
      },
      {
        source: "google",
        state: "ok",
        status: 0,
        classification: "ok",
        addresses: ["203.0.113.10"],
        addresses_truncated: false,
        ttl_s: 60,
        negative_ttl_s: null,
        checked_at_epoch: NOW - 20,
        elapsed_ms: 18,
      },
    ],
    ...overrides,
  });
  const PUBLIC_SHARE = (overrides = {}) => {
    const budget = {
      unreadable: false,
      reason: null,
      window_s: 1800,
      mapping_used: 0,
      mapping_limit: 2,
      ts_restart_used: 0,
      ts_restart_limit: 1,
      mapping_attempts_utc: [],
      ts_restarts_utc: [],
      mapping_next_eligible_at_utc: null,
      ts_restart_next_eligible_at_utc: null,
      ...(overrides.budget ?? {}),
    };
    const snapshot = {
      version: 1,
      boot_id: "fixture-boot",
      observed_at_utc: new Date((NOW - 20) * 1000).toISOString(),
      observed_at_epoch: NOW - 20,
      intent: {
        state: "running",
        at_utc: new Date((NOW - 3600) * 1000).toISOString(),
        boot_id: "fixture-boot",
        source: "share.start",
      },
      public_url: SHARE_STATUS.body.share_url,
      host: "share.fixture.invalid",
      round: {
        id: "fixture-round",
        phase: "normal",
        started_at_utc: new Date((NOW - 20) * 1000).toISOString(),
        finished_at_utc: new Date((NOW - 20) * 1000).toISOString(),
        confirmation: 2,
        next_check_at_utc: new Date((NOW + 40) * 1000).toISOString(),
        next_action_at_utc: null,
      },
      overall: "ok",
      reason: "ok",
      entries: [PUBLIC_ENTRY("ok", "ok", "203.0.113.10")],
      dns: PUBLIC_DNS(),
      local: {
        ts_service: "running",
        connectivity: "ok",
        backend: "running",
        auth: "valid",
        gateway: "running",
        security_gate: "ok",
        maintenance: "none",
      },
      recovery: {
        state: "idle",
        action: "none",
        stage: "none",
        reason: null,
        attempted_at_utc: null,
        finished_at_utc: null,
        manual: false,
        actions: [],
      },
      budget,
      cancel: { cancelled: false, reason: null },
      notification: { last_key: null, last_at_utc: null, last_result: null },
      state_file: "share-public-state.json",
      event_file: "share-public-events.jsonl",
      budget_file: "share-public-budget.json",
      cancel_file: "share-public-cancel.json",
      ...(overrides.snapshot ?? {}),
    };
    return {
      status: 200,
      body: {
        available: true,
        version: 1,
        checked_at: NOW,
        snapshot,
        events: [
          {
            at_utc: new Date((NOW - 20) * 1000).toISOString(),
            status: "ok",
            code: "ok",
            action: "none",
          },
          {
            at_utc: new Date((NOW - 90) * 1000).toISOString(),
            status: "degraded",
            code: "partial_failure",
            action: "none",
          },
        ],
        events_truncated: false,
        events_unreadable: false,
        budget,
        cancel: { cancelled: false, reason: null },
        unreadable: false,
        reason: null,
        state_file: "share-public-state.json",
        ...(overrides.status ?? {}),
      },
    };
  };
  // S5 public actions are absent from the older console fixture; add only the
  // two new entries so the public card can exercise its own enablement.
  const PUBLIC_SHARE_ACTIONS = () => ({
    status: 200,
    body: {
      actions: [
        ...fixtures["/api/console/actions"].body.actions,
        {
          action: "share.public_check",
          enabled: true,
          cancelable: true,
          param_schema: {},
          description: "fixture public check",
        },
        {
          action: "share.public_retry",
          enabled: true,
          cancelable: true,
          param_schema: {},
          description: "fixture public retry",
        },
      ],
    },
  });
  /** R3 RMB readings: the captured share-usage fixture predates CNY, so every
   *  row (keys, models, days, unattributed) gets the CNY fields the backend now
   *  returns. `full` = exact amounts, `partial` = lower bounds (≥), `unknown` =
   *  marked partial with no amount (未知). The fixture itself is never edited. */
  const usageWithCny = (mode) => {
    const body = structuredClone(fixtures["/api/console/share-usage"].body);
    const apply = (row, index) => {
      const known = 12.3456 + index;
      const estimated = 0.789 + index;
      row.costCnyPartial = mode !== "full";
      row.knownCostCnyPartial = mode !== "full";
      row.estimatedCostCnyPartial = mode !== "full";
      row.knownCostCny = mode === "unknown" ? null : known;
      row.estimatedCostCny = mode === "unknown" ? null : estimated;
      row.costCny = mode === "unknown" ? null : known + estimated;
      if (mode === "full") {
        row.unknownRequests = 0;
        row.unpricedRequests = 0;
        if (row.coverage) {
          row.coverage.unknownUsageRequests = 0;
          row.coverage.unpricedRequests = 0;
          row.coverage.costComplete = true;
        }
      }
      for (const model of row.models ?? []) apply(model, index + 1);
      for (const day of row.days ?? []) apply(day, index + 2);
      if (row.undated) apply(row.undated, index + 3);
    };
    body.keys.forEach((row, index) => apply(row, index));
    apply(body.unattributed, 7);
    // The captured fixture has no undated bucket; add one to the unattributed
    // row so the drill's 无日期 group is exercised with the same CNY modes.
    if (body.unattributed) {
      const undated = structuredClone(body.unattributed);
      delete undated.models;
      delete undated.days;
      delete undated.undated;
      body.unattributed.undated = { ...undated, day: null, requests: 1 };
      apply(body.unattributed.undated, 9);
    }
    return { status: 200, body };
  };
  /** Read the RMB readings the usage tab actually rendered: the per-key summary
   *  table (last row = unattributed), the expanded 14-day trend tooltips and the
   *  panel's own text. The detail cell is read separately: its rows stay in the
   *  DOM while the fold is closed, so folding it into lastRow/lastCells would let
   *  a drill amount satisfy an assertion about the summary row. */
  const usageReading = () => {
    const table = document.querySelector(".wb-share-usage-table");
    const summaries = [
      ...document.querySelectorAll(
        ".wb-share-usage-table tbody tr.wb-key-summary"
      ),
    ];
    const row = summaries.at(-1);
    const detailRows = [
      ...document.querySelectorAll(
        ".wb-share-usage-table tbody tr.wb-key-details-row"
      ),
    ];
    const detailRow = detailRows.at(-1);
    const summaryCells = row ? [...row.querySelectorAll("td")] : [];
    const detailHost = detailRow?.querySelector(
      "details[data-wb-share-key-details]"
    );
    const detailBody = detailRow?.querySelector(".wb-share-key-detail-body");
    const hotspots = [
      ...document.querySelectorAll(".wb-share-key-chart g.wb-area-hotspot"),
    ];
    return {
      rows: summaries.length,
      lastRow: summaryCells.length
        ? summaryCells
            .map((td) => td.textContent.replace(/\s+/g, " ").trim())
            .join(" ")
        : null,
      lastCells: summaryCells.map((td) =>
        td.textContent.replace(/\s+/g, " ").trim()
      ),
      lastDetail: detailRow
        ? detailRow.textContent.replace(/\s+/g, " ").trim()
        : null,
      detailOpen: detailHost ? detailHost.open : null,
      detailBodyDisplay: detailBody
        ? getComputedStyle(detailBody).display
        : null,
      tableText: table ? table.textContent.replace(/\s+/g, " ").trim() : null,
      trend: hotspots.map((g) => g.getAttribute("aria-label") ?? ""),
      panelText:
        document
          .querySelector('[data-wb-share-tabpanel="usage"]')
          ?.textContent.replace(/\s+/g, " ")
          .trim() ?? "",
    };
  };
  const CATALOG_RESULT = {
    catalogs: {
      um: [
        {
          id: "DeepSeek-V4.1-Flash",
          display_name: "DeepSeek V4.1 Flash",
          context: 131072,
          efforts: [],
          default_effort: null,
          publish: true,
        },
        {
          id: "GLM-5.3",
          display_name: "GLM 5.3",
          context: 200000,
          efforts: ["low", "high"],
          default_effort: "high",
          publish: true,
        },
      ],
      qoder_cn: [
        {
          id: "qwen3-coder-plus",
          display_name: "Qwen3 Coder Plus",
          context: 262144,
          efforts: [],
          default_effort: null,
          publish: true,
        },
      ],
      qoder_global: [
        {
          id: "qfmodel",
          display_name: "Qoder Fast",
          context: null,
          efforts: [],
          default_effort: null,
          publish: false,
        },
      ],
      cliproxy: [],
    },
    warnings: ["Gemini（CLIProxy） 目录读取失败：HTTP 502（夹具）"],
    checked_epoch: NOW,
  };
  // Rev19 分享监督/维护读面：形状照 console_share_actions 的 diagnostics /
  // backend_restart 返回（layers / maintenance / supervision / before / after），
  // 值全部是合成值（"夹具"），不读任何真实服务、文件或凭据。connectivity 层故意
  // 带 stale=true：陈旧只降新鲜度，页面必须标「已陈旧」而不是判宕机。
  const SHARE_DIAG_LAYER = (state, verdict, stale, extra = {}) => ({
    state,
    verdict,
    source: "夹具：只读缝（合成值）",
    checked_at: NOW - 5,
    age_s: 5,
    ttl_s: 60,
    stale,
    reason: null,
    evidence: { timestamp_basis: "local_read" },
    ...extra,
  });
  const SHARE_DIAGNOSTICS = {
    checked_at: NOW - 5,
    layers: {
      share_gateway: SHARE_DIAG_LAYER("running", "confirmed", false, {
        instance_probe: { pid: 4242, started_at: NOW - 300, reason: "ok" },
      }),
      connectivity: SHARE_DIAG_LAYER("lost", "confirmed", true, {
        reason: "connectivity_stale_evidence",
      }),
      ts_service: SHARE_DIAG_LAYER("running", "confirmed", false),
      funnel: SHARE_DIAG_LAYER("on", "confirmed", false),
      security_gate: SHARE_DIAG_LAYER("ok", "confirmed", false, {
        evidence: { check: "limits", verdict: true, detail: "ok" },
      }),
      share_backend: SHARE_DIAG_LAYER("running", "confirmed", false, {
        pid_state: {
          state: "running",
          pid: 4243,
          port: 8110,
          identity: "verified",
          identity_reason: "ok",
        },
      }),
      backend_auth: SHARE_DIAG_LAYER("valid", "confirmed", false, {
        auth_label: "认证有效",
      }),
      upstream: SHARE_DIAG_LAYER("ok", "confirmed", false),
      watchdog: SHARE_DIAG_LAYER("running", "confirmed", false, {
        evidence: {
          task_name: "umshare-watchdog",
          exists: true,
          task_state: "Running",
          execute: "C:\\Program Files\\PowerShell\\7\\pwsh.exe",
          execute_matches: true,
          script_path: "C:\\ProgramData\\UMShareRun\\watchdog-umshare.ps1",
          script_matches: true,
          last_task_result: 2147946720,
          last_task_result_hex: "0x800710E0",
          last_run_at: NOW - 300,
          queried_at: NOW - 5,
          heartbeat_at: NOW - 8,
          heartbeat_source: "watchdog_log_mtime",
        },
      }),
    },
    maintenance: {
      lease: {
        lease_id: "fx-lease-1",
        component: "share_gateway",
        round: 3,
        expires_at: NOW + 55,
        by: "夹具",
      },
      instance: {
        instance_id: "fx-instance-1",
        state: "running",
        active_count: 0,
        last_seen_generation: 12,
      },
      last_outcome: {
        ok: false,
        code: "share_backend_drain_timeout",
        detail: "夹具：上一次排空超时（合成值）",
        at: NOW - 120,
      },
    },
    desired_stop_in_force: false,
    last_error: null,
    note: "夹具：只读全域诊断（合成值）",
    supervision: {
      generatedFrom: "share_backend_supervision.run_once（夹具合成值）",
      decision: {
        at_utc: "2026-10-06T12:00:56+00:00",
        action: "none",
        code: "budget_exhausted",
        detail: "本小时启动预算已耗尽（3/3）：只报告并给人工入口（夹具）",
        confirmations: 2,
        attempts_this_hour: 3,
        instance: { pid: null, identity: "unknown" },
        duration_ms: 500,
      },
      state: {
        version: 1,
        boot_id: "FIXTURE-HOST|2026-10-06T09:00:00Z",
        consecutive_failures: 2,
        restart_attempts_utc: [
          "2026-10-06T11:45:55Z",
          "2026-10-06T11:50:55Z",
          "2026-10-06T11:55:55Z",
        ],
        idle_reason: "budget_exhausted",
      },
    },
  };
  const SHARE_RESTART_RESULT = {
    restarted: true,
    component: "share_backend",
    backend: { component: "share_backend", pid: 4243, elapsed_s: 12.4 },
    drain: { state: "drained", active_left: 0, resumed: true },
    before: {
      share_backend: SHARE_DIAG_LAYER("running", "confirmed", false),
      backend_auth: SHARE_DIAG_LAYER("valid", "confirmed", false, {
        auth_label: "认证有效",
      }),
    },
    after: {
      share_backend: SHARE_DIAG_LAYER("running", "confirmed", false),
      backend_auth: SHARE_DIAG_LAYER("valid", "confirmed", true, {
        auth_label: "认证有效",
      }),
    },
    last_error: null,
    note: "夹具：8110 单层重启完成（合成值）",
  };

  // Scenario = reads to override + operation fixtures + the steps that drive the page.
  // ops[action] = { reject: {status, body} } | { final: "succeeded"|"failed"|"running", result?, error? }
  const compactShare = {
    lane: 6,
    compactShare: true,
    shareTab: "keys",
    reads: {
      "/api/console/share-management/status": SHARE_STATUS,
      "/api/console/share-management/keys": SHARE_KEYS,
      "/api/console/share-management/public": PUBLIC_SHARE(),
    },
    drive: async (page) => {
      // Default tab: keys selected, the other two rendered but hidden.
      if ((await page.locator('[role="tab"][data-wb-share-tab]').count()) !== 3)
        throw Error("The share page must expose exactly three tabs");
      for (const id of ["keys", "diagnostics", "usage"]) {
        if (
          (await page
            .locator(`[role="tab"][data-wb-share-tab="${id}"]`)
            .count()) !== 1
        )
          throw Error(`Missing share tab ${id}`);
      }
      if (
        (await page
          .locator('[role="tab"][data-wb-share-tab="keys"]')
          .getAttribute("aria-selected")) !== "true"
      )
        throw Error("Keys must be the default share tab");
      for (const id of ["diagnostics", "usage"]) {
        if (
          (await page
            .locator(`[role="tab"][data-wb-share-tab="${id}"]`)
            .getAttribute("aria-selected")) !== "false"
        )
          throw Error(`${id} must start unselected`);
        if (await page.locator(`[data-wb-share-tabpanel="${id}"]`).isVisible())
          throw Error(`${id} panel must start hidden`);
      }
      if (!(await page.locator('[data-wb-share-tabpanel="keys"]').isVisible()))
        throw Error("The keys panel must be visible by default");
      // The overview and the address stay outside and above the tab panels.
      const order = await page.evaluate(() => {
        const top = (selector) =>
          document.querySelector(selector)?.getBoundingClientRect().top ?? null;
        const hero = document.querySelector(".wb-share-hero");
        return {
          hero: top(".wb-share-hero"),
          tabs: top(".wb-share-tabs"),
          keys: top('[data-wb-share-tabpanel="keys"]'),
          addressInHero: !!hero?.querySelector(".wb-share-access-url"),
          addressVisible: (() => {
            const el = hero?.querySelector(".wb-share-url");
            if (!el) return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          })(),
        };
      });
      if (!(order.hero < order.tabs && order.tabs < order.keys))
        throw Error("The overview and the tabs must precede the panels");
      if (!order.addressInHero || !order.addressVisible)
        throw Error(
          "The public address must stay visible in the overview outside the tabs"
        );
      // 1600×900 first screen: all four fixture keys are rendered.
      const keyRows = await page
        .locator(".wb-share-key-table tbody tr")
        .count();
      if (keyRows !== 4)
        throw Error(
          `The first screen must list all four fixture keys (got ${keyRows})`
        );
      const keyOverflow = await page.evaluate(() => {
        const keys = document.querySelector(".wb-share-keys");
        return keys ? keys.scrollWidth - keys.clientWidth : null;
      });
      if (keyOverflow === null || keyOverflow > 1)
        throw Error(
          `The keys table overflows its container by ${keyOverflow}px`
        );
      // Real tab clicks: each panel becomes visible and the others hide.
      for (const id of ["diagnostics", "usage", "keys"]) {
        await openShareTab(page, id);
        for (const other of ["keys", "diagnostics", "usage"]) {
          if (
            other !== id &&
            (await page
              .locator(`[data-wb-share-tabpanel="${other}"]`)
              .isVisible())
          )
            throw Error(`Panel ${other} must hide while ${id} is selected`);
        }
      }
      // Keyboard: ArrowRight/ArrowLeft move selection and focus.
      await page.locator('[role="tab"][data-wb-share-tab="keys"]').focus();
      await page.keyboard.press("ArrowRight");
      await page
        .locator(
          '[role="tab"][data-wb-share-tab="diagnostics"][aria-selected="true"]'
        )
        .waitFor();
      if (
        !(await page
          .locator('[role="tab"][data-wb-share-tab="diagnostics"]')
          .evaluate((el) => document.activeElement === el))
      )
        throw Error("ArrowRight must focus the diagnostics tab");
      await page.keyboard.press("ArrowLeft");
      await page
        .locator('[role="tab"][data-wb-share-tab="keys"][aria-selected="true"]')
        .waitFor();
      // Every low-frequency key action is expanded inline; there is no 更多
      // layer. The inline editor takes focus and cancelling it returns focus to
      // the exact action button that opened it.
      const rename = page.locator(
        '[data-wb-share-key="rename"][data-wb-share-key-id="fx-key-a"]'
      );
      if (!(await rename.isVisible()))
        throw Error("Secondary key actions must be expanded inline");
      if (await page.locator('[data-wb-share-key="more"]').count())
        throw Error("更多 must be removed; every key action is inline");
      await rename.click();
      await page.locator('[data-wb-share-edit][aria-label="新名称"]').waitFor();
      await page.locator('[data-wb-share-key="cancel-edit"]').click();
      if (!(await rename.evaluate((el) => document.activeElement === el)))
        throw Error(
          "A completed key action must return focus to its inline button"
        );
      // The selected tab and an opened fold survive a data refresh.
      await openShareTab(page, "usage");
      await openShareFold(page, "usage-trend");
      await page.locator(".wb-share-hero [data-wb-share-refresh]").click();
      await page.waitForTimeout(900);
      if (
        (await page
          .locator('[role="tab"][data-wb-share-tab="usage"]')
          .getAttribute("aria-selected")) !== "true"
      )
        throw Error("The selected tab must survive a data refresh");
      if (
        (await page
          .locator('details[data-wb-share-panel="usage-trend"]')
          .getAttribute("open")) === null
      )
        throw Error("An opened fold must survive a data refresh");
      await page
        .locator('details[data-wb-share-panel="usage-trend"] > summary')
        .click();
      await openShareTab(page, "keys");
      await page.evaluate(() =>
        document
          .querySelector(".wb-main")
          ?.scrollTo({ top: 0, behavior: "instant" })
      );
    },
    shots: ["first"],
    probe: () => {
      const panel = (id) =>
        document.querySelector(`[data-wb-share-tabpanel="${id}"]`);
      const keys = document.querySelector(".wb-share-keys");
      return {
        tab:
          document.querySelector('[role="tab"][aria-selected="true"]')?.dataset
            .wbShareTab ?? null,
        tabs: document.querySelectorAll('[role="tab"][data-wb-share-tab]')
          .length,
        keysVisible: panel("keys")?.hidden === false,
        diagnosticsHidden: panel("diagnostics")?.hidden === true,
        usageHidden: panel("usage")?.hidden === true,
        keyRows: document.querySelectorAll(".wb-share-key-table tbody tr")
          .length,
        keyOverflow: keys ? keys.scrollWidth - keys.clientWidth : null,
      };
    },
    validateProbe: (probe) => [
      ...(probe?.tab !== "keys"
        ? [
            `The compact share page must end on the keys tab (got ${probe?.tab})`,
          ]
        : []),
      ...(probe?.tabs !== 3
        ? [`The share page must expose three tabs (got ${probe?.tabs})`]
        : []),
      ...(!probe?.keysVisible ? ["The keys panel must be visible"] : []),
      ...(!probe?.diagnosticsHidden || !probe?.usageHidden
        ? ["Only the keys panel may be visible"]
        : []),
      ...(probe?.keyRows !== 4
        ? [`The fixture must render four keys (got ${probe?.keyRows})`]
        : []),
      ...(probe?.keyOverflow === null || probe?.keyOverflow > 1
        ? [`The keys table overflows by ${probe?.keyOverflow}px`]
        : []),
    ],
  };
  const SCENARIOS = {
    "share-compact": compactShare,
    "share-compact-narrow": {
      ...compactShare,
      viewport: { width: 960, height: 900 },
    },
    "share-compact-736": {
      ...compactShare,
      viewport: { width: 736, height: 900 },
    },
    "share-compact-375": {
      ...compactShare,
      viewport: { width: 375, height: 812 },
    },
    "share-compact-375-actions": {
      ...compactShare,
      viewport: { width: 375, height: 900 },
      drive: async (page) => {
        // The 375 layout turns the table into compact entries; every action is
        // inline, stays inside the viewport, and there is no 更多 layer.
        if (await page.locator('[data-wb-share-key="more"]').count())
          throw Error("更多 must be removed");
        for (const action of [
          "test",
          "toggle-enable",
          "rename",
          "adjust-cap",
          "rotate",
          "revoke-terminate",
          "delete",
        ]) {
          const button = page
            .locator(`[data-wb-share-key="${action}"]`)
            .first();
          if (!(await button.isVisible()))
            throw Error(`${action} must be visible inline at 375`);
          const box = await button.boundingBox();
          if (!box || box.x < 0 || box.x + box.width > 376)
            throw Error(
              `${action} must stay in the viewport: ${JSON.stringify(box)}`
            );
        }
        const overflow = await page.evaluate(() => {
          const el = document.querySelector(".wb-share-keys");
          return el ? el.scrollWidth - el.clientWidth : null;
        });
        if (overflow === null || overflow > 1)
          throw Error(`The keys table overflows by ${overflow}px`);
      },
      shots: ["first"],
    },
    "share-closed-gateway-running": {
      lane: 6,
      shareTab: "keys",
      reads: {
        "/api/console/share-management/status": {
          status: 200,
          body: { ...SHARE_STATUS.body, funnel_on: false, share_url: "" },
        },
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      async drive(page) {
        // A running gateway with the Funnel intentionally closed is 未开放;
        // it is not missing evidence and must not read 状态未确认.
        await page
          .locator(".wb-share-pill")
          .filter({ hasText: "未开放" })
          .waitFor();
        const text = await page.locator("[data-wb-share-state]").innerText();
        if (text.includes("状态未确认"))
          throw Error(
            "A running gateway with a closed Funnel must read 未开放, not 状态未确认"
          );
        if (
          (await page
            .locator('[data-wb-share-action="share.start"]')
            .count()) !== 1
        )
          throw Error("A closed share must offer 开启分享");
      },
      shots: ["section:.wb-share-hero"],
    },
    "share-gateway-down": {
      lane: 6,
      shareTab: "keys",
      reads: {
        "/api/console/share-management/status": {
          status: 200,
          body: {
            ...SHARE_STATUS.body,
            funnel_on: false,
            share_url: "",
            gateway_running: false,
          },
        },
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      async drive(page) {
        // A down 8100 keeps the fixed single-layer recovery reachable from the
        // overview, next to the whole-chain 开启分享 action.
        const recover = page.locator(
          '.wb-share-hero [data-wb-share-action="share.gateway_restart"]'
        );
        await recover.waitFor();
        if (!(await recover.innerText()).includes("恢复分享网关"))
          throw Error("A down gateway must offer 恢复分享网关");
        if (
          (await page
            .locator('[data-wb-share-action="share.start"]')
            .count()) !== 1
        )
          throw Error("A down gateway must still offer 开启分享");
      },
      shots: ["section:.wb-share-hero"],
    },
    "share-public-read-failed": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/share-management/public": {
          status: 500,
          body: { error: "public_read_failed" },
        },
      },
      async drive(page) {
        const alert = page.locator('[data-wb-share-alert="public-read"]');
        await alert.waitFor();
        if ((await alert.locator("[data-wb-share-refresh]").count()) !== 1)
          throw Error("A public read failure must offer 重新读取");
      },
      shots: ["section:.wb-share-hero"],
    },
    "share-public-unreadable": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/share-management/public": PUBLIC_SHARE({
          status: { unreadable: true },
        }),
      },
      async drive(page) {
        const alert = page.locator('[data-wb-share-alert="public-unreadable"]');
        await alert.waitFor();
        if (
          (await alert.locator('[data-wb-share-public="check"]').count()) !== 1
        )
          throw Error("An unreadable public state must offer 一键复查");
      },
      shots: ["section:.wb-share-hero"],
    },
    "share-public-cancelled-ok": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/public": PUBLIC_SHARE({
          status: { cancel: { cancelled: true, reason: "manual_close" } },
        }),
      },
      drive: async (page) => {
        const badge = page.locator("[data-wb-share-public-overall]");
        if ((await badge.innerText()) !== "巡检已暂停")
          throw Error(
            "A live cancellation must override an old healthy snapshot"
          );
        if (
          !(
            await page
              .locator('[data-wb-share-public-reading="recovery"]')
              .innerText()
          ).includes("已暂停")
        )
          throw Error("Cancelled recovery cannot look recovered");
      },
      shots: ["section:.wb-share-public"],
    },
    "share-public-dns-disagreement": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/public": PUBLIC_SHARE({
          snapshot: {
            overall: "degraded",
            reason: "dns_disagreement",
            dns: PUBLIC_DNS({
              state: "disagreement",
              reason: "dns_disagreement",
              ttl_s: 60,
              negative_ttl_s: 300,
              sources: [
                {
                  source: "cloudflare",
                  state: "nxdomain",
                  status: 3,
                  classification: "nxdomain",
                  addresses: [],
                  addresses_truncated: false,
                  ttl_s: null,
                  negative_ttl_s: 300,
                  checked_at_epoch: NOW - 20,
                  elapsed_ms: 20,
                },
                {
                  source: "google",
                  state: "ok",
                  status: 0,
                  classification: "ok",
                  addresses: ["203.0.113.10"],
                  addresses_truncated: false,
                  ttl_s: 60,
                  negative_ttl_s: null,
                  checked_at_epoch: NOW - 20,
                  elapsed_ms: 18,
                },
              ],
            }),
          },
        }),
      },
      async drive(page) {
        const alert = page.locator('[data-wb-share-alert="public-dns"]');
        await alert.waitFor();
        if (!(await alert.innerText()).includes("DNS 解析分歧"))
          throw Error("A DNS disagreement must be named in the overview");
        if (!(await alert.innerText()).includes("不发系统通知"))
          throw Error("DNS-only evidence must not claim a system notification");
        if (await page.locator('[data-wb-share-alert="public-fault"]').count())
          throw Error("Pure DNS must not borrow the generic fault banner");
        const badge = page.locator("[data-wb-share-public-overall]");
        if (!(await badge.innerText()).includes("DNS 解析分歧"))
          throw Error("The overall badge must name the DNS disagreement");
        const dns = page.locator('[data-wb-share-public-dns="overall"]');
        if (!(await dns.innerText()).includes("负缓存剩余约"))
          throw Error("The DNS section must show the negative TTL");
        if (!(await dns.innerText()).includes("下次检查"))
          throw Error("The DNS section must show the actual next check time");
        if (
          (await page.locator("[data-wb-share-public-dns-source]").count()) !==
          2
        )
          throw Error("Both DNS sources must be listed");
      },
      shots: ["section:.wb-share-hero", "section:.wb-share-public"],
    },
    "share-public-dns-nxdomain": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/public": PUBLIC_SHARE({
          snapshot: {
            overall: "not_checked",
            reason: "dns_nxdomain",
            entries: [],
            dns: PUBLIC_DNS({
              state: "nxdomain",
              reason: "dns_nxdomain",
              addresses: [],
              ttl_s: null,
              negative_ttl_s: 300,
              sources: [
                {
                  source: "cloudflare",
                  state: "nxdomain",
                  status: 3,
                  classification: "nxdomain",
                  addresses: [],
                  addresses_truncated: false,
                  ttl_s: null,
                  negative_ttl_s: 300,
                  checked_at_epoch: NOW - 20,
                  elapsed_ms: 20,
                },
                {
                  source: "google",
                  state: "nxdomain",
                  status: 3,
                  classification: "nxdomain",
                  addresses: [],
                  addresses_truncated: false,
                  ttl_s: null,
                  negative_ttl_s: 300,
                  checked_at_epoch: NOW - 20,
                  elapsed_ms: 18,
                },
              ],
            }),
          },
        }),
      },
      async drive(page) {
        const alert = page.locator('[data-wb-share-alert="public-dns"]');
        await alert.waitFor();
        if (!(await alert.innerText()).includes("DNS 域名不存在（NXDOMAIN）"))
          throw Error("NXDOMAIN must be named as itself");
        if (await page.locator('[data-wb-share-alert="public-fault"]').count())
          throw Error("A negative DNS answer is not a confirmed TLS failure");
        if (await page.locator("[data-wb-share-public-entry]").count())
          throw Error("No address means no TLS entry");
      },
      shots: ["section:.wb-share-hero", "section:.wb-share-public"],
    },
    "share-public-dns-truncated": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/public": PUBLIC_SHARE({
          snapshot: {
            overall: "incomplete",
            reason: "addresses_truncated",
            entries: [
              PUBLIC_ENTRY("failed", "tls_handshake_failed", "203.0.113.10"),
              PUBLIC_ENTRY("failed", "tls_handshake_failed", "203.0.113.11"),
              PUBLIC_ENTRY("failed", "tls_handshake_failed", "203.0.113.12"),
              PUBLIC_ENTRY("failed", "tls_handshake_failed", "203.0.113.13"),
            ],
            dns: PUBLIC_DNS({
              addresses: [
                "203.0.113.10",
                "203.0.113.11",
                "203.0.113.12",
                "203.0.113.13",
                "203.0.113.14",
              ],
              addresses_truncated: true,
              sources: [
                {
                  source: "cloudflare",
                  state: "ok",
                  status: 0,
                  classification: "ok",
                  addresses: ["203.0.113.10", "203.0.113.11", "203.0.113.12"],
                  addresses_truncated: true,
                  ttl_s: 60,
                  negative_ttl_s: null,
                  checked_at_epoch: NOW,
                  elapsed_ms: 20,
                },
                {
                  source: "google",
                  state: "ok",
                  status: 0,
                  classification: "ok",
                  addresses: ["203.0.113.13", "203.0.113.14"],
                  addresses_truncated: true,
                  ttl_s: 60,
                  negative_ttl_s: null,
                  checked_at_epoch: NOW,
                  elapsed_ms: 18,
                },
              ],
            }),
          },
        }),
      },
      async drive(page) {
        const alert = page.locator('[data-wb-share-alert="public-dns"]');
        await alert.waitFor();
        if (!(await alert.innerText()).includes("检查未完成（地址覆盖缺口）"))
          throw Error("A truncated address set must be named as incomplete");
        if (await page.locator('[data-wb-share-alert="public-fault"]').count())
          throw Error("A truncated set is not a confirmed all-entry failure");
        const badge = page.locator("[data-wb-share-public-overall]");
        if (!(await badge.innerText()).includes("地址覆盖不完整"))
          throw Error("The overall badge must show the coverage gap");
        const dns = page.locator('[data-wb-share-public-dns="overall"]');
        if (!(await dns.innerText()).includes("覆盖不完整"))
          throw Error("The DNS section must show the coverage gap");
      },
      shots: ["section:.wb-share-hero", "section:.wb-share-public"],
    },
    "share-public-dns-ttl-expired": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/public": PUBLIC_SHARE({
          snapshot: {
            overall: "not_checked",
            reason: "dns_nxdomain",
            entries: [],
            dns: PUBLIC_DNS({
              state: "nxdomain",
              reason: "dns_nxdomain",
              addresses: [],
              ttl_s: null,
              negative_ttl_s: 300,
              checked_at_epoch: NOW - 400,
              sources: [
                {
                  source: "cloudflare",
                  state: "nxdomain",
                  status: 3,
                  classification: "nxdomain",
                  addresses: [],
                  addresses_truncated: false,
                  ttl_s: null,
                  negative_ttl_s: 300,
                  checked_at_epoch: NOW - 400,
                  elapsed_ms: 20,
                },
                {
                  source: "google",
                  state: "nxdomain",
                  status: 3,
                  classification: "nxdomain",
                  addresses: [],
                  addresses_truncated: false,
                  ttl_s: null,
                  negative_ttl_s: 300,
                  checked_at_epoch: NOW - 400,
                  elapsed_ms: 18,
                },
              ],
            }),
          },
        }),
      },
      async drive(page) {
        const alert = page.locator('[data-wb-share-alert="public-dns"]');
        await alert.waitFor();
        const dns = page.locator('[data-wb-share-public-dns="overall"]');
        if (!(await dns.innerText()).includes("负缓存已到期，等待新检查"))
          throw Error("An expired negative TTL must wait for a new check");
        if (!(await dns.innerText()).includes("已到期"))
          throw Error("The DNS section must not claim recovery");
      },
      shots: ["section:.wb-share-public"],
    },
    "share-public-dns-unconfirmed": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/public": PUBLIC_SHARE({
          snapshot: {
            overall: "degraded",
            reason: "dns_unconfirmed",
            dns: PUBLIC_DNS({
              state: "unconfirmed",
              reason: "dns_unconfirmed",
              sources: [
                {
                  source: "cloudflare",
                  state: "timeout",
                  status: null,
                  classification: "timeout",
                  addresses: [],
                  addresses_truncated: false,
                  ttl_s: null,
                  negative_ttl_s: null,
                  checked_at_epoch: NOW - 20,
                  elapsed_ms: 8000,
                },
                {
                  source: "google",
                  state: "ok",
                  status: 0,
                  classification: "ok",
                  addresses: ["203.0.113.10"],
                  addresses_truncated: false,
                  ttl_s: 60,
                  negative_ttl_s: null,
                  checked_at_epoch: NOW - 20,
                  elapsed_ms: 18,
                },
              ],
            }),
          },
        }),
      },
      async drive(page) {
        const alert = page.locator('[data-wb-share-alert="public-dns"]');
        await alert.waitFor();
        if (!(await alert.innerText()).includes("DNS 源未确认"))
          throw Error("A timeout source must stay unconfirmed");
        const source = page.locator(
          '[data-wb-share-public-dns-source="cloudflare"]'
        );
        if (!(await source.innerText()).includes("超时"))
          throw Error("The timed-out source must name its own state");
        if (await page.locator('[data-wb-share-alert="public-fault"]').count())
          throw Error("An unconfirmed source is not a TLS failure");
      },
      shots: ["section:.wb-share-hero", "section:.wb-share-public"],
    },
    "share-public-dns-missing": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/public": PUBLIC_SHARE({
          snapshot: { dns: undefined },
        }),
      },
      async drive(page) {
        const missing = page.locator('[data-wb-share-public-dns="missing"]');
        await missing.waitFor();
        if (!(await missing.innerText()).includes("未提供"))
          throw Error("An old snapshot must show DNS as not provided");
        if (await page.locator('[data-wb-share-alert="public-dns"]').count())
          throw Error("A missing DNS field must not invent an anomaly");
      },
      shots: ["section:.wb-share-public"],
    },
    "share-public-dns-mixed-tls": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/public": PUBLIC_SHARE({
          snapshot: {
            overall: "degraded",
            reason: "partial_failure",
            entries: [
              PUBLIC_ENTRY("ok", "ok", "203.0.113.10"),
              PUBLIC_ENTRY("failed", "tls_handshake_failed", "203.0.113.11"),
            ],
            dns: PUBLIC_DNS({
              state: "disagreement",
              reason: "dns_disagreement",
            }),
          },
        }),
      },
      async drive(page) {
        const alert = page.locator('[data-wb-share-alert="public-fault"]');
        await alert.waitFor();
        if (!(await alert.innerText()).includes("DNS 解析分歧"))
          throw Error(
            "A mixed failure must keep the DNS detail next to the real TLS failure"
          );
        if (!(await alert.innerText()).includes("tls_handshake_failed"))
          throw Error("The real TLS failure must stay in the banner");
      },
      shots: ["section:.wb-share-hero"],
    },
    "overview-down": {
      reads: {
        "/api/console/health": health({
          chat2api: {
            state: "not_listening",
            http_status: null,
            note: undefined,
          },
          cliproxy: { state: "unknown", note: "连接超时，探测未定（夹具）" },
        }),
        "/api/console/services": runtime({
          chat2api: {
            state: "Stopped",
            healthy: false,
            detail: "已停止",
            process_ids: [],
          },
        }),
      },
      async drive(page) {
        await page.click('[data-wb-md-item="0"]');
        await page.waitForTimeout(500);
      },
      shots: ["first", "section:.wb-ov-services"],
      probe: () =>
        [...document.querySelectorAll(".wb-md-detail button")].map((b) => {
          const c = getComputedStyle(b);
          const r = b.getBoundingClientRect();
          return `${(b.getAttribute("aria-label") || b.textContent).trim().slice(0, 12)} ${Math.round(r.width)}x${Math.round(r.height)} bg=${c.backgroundColor} fg=${c.color} bd=${c.borderTopWidth} ${c.borderTopStyle} ${c.borderTopColor} r=${c.borderTopLeftRadius}`;
        }),
    },
    "overview-stale": {
      reads: { "/api/console/health": health({}, NOW - 600) },
      async drive(page) {
        await page.click('[data-wb-md-item="3"]');
        await page.waitForTimeout(500);
      },
      shots: ["first", "section:.wb-ov-services"],
    },
    "overview-actions": {
      reads: { "/api/console/health": health({}) },
      ops: {
        "account.um_verify": {
          reject: {
            status: 409,
            body: {
              error: "operation_busy",
              reason: "另一条账号操作仍在执行（夹具）",
            },
          },
        },
        "qoder.status": {
          final: "succeeded",
          result: { authenticated: true, checked_epoch: NOW },
        },
        "qoderglobal.status": { final: "running" },
      },
      async drive(page, mark) {
        const click = async (sel) => {
          const t = Date.now();
          await page.locator(sel).first().click({ timeout: 8000 });
          mark(`${sel} ${Date.now() - t}ms`);
        };
        await click('[data-wb-md-item="2"]');
        await click('.wb-md-detail [data-wb-acct-action="qoderglobal.status"]');
        await page.waitForTimeout(1500);
        await click('[data-wb-md-item="0"]');
        await click('.wb-md-detail [data-wb-acct-action="account.um_verify"]');
        await page.waitForTimeout(600);
        await click('[data-wb-md-item="4"]');
        await click('.wb-md-detail [data-wb-acct-action="qoder.status"]');
        await page.waitForTimeout(4500);
      },
      shots: ["section:.wb-ov-services"],
      probe: () =>
        [...document.querySelectorAll(".wb-md-item")].map((b) => ({
          id: b.dataset.wbMdItem,
          pressed: b.getAttribute("aria-pressed"),
          pending: b.querySelector(".wb-md-pending")?.textContent?.trim() || "",
          result: b.querySelector(".wb-md-result")?.textContent?.trim() || "",
        })),
    },
    // 2026-10-22 排版回归：维护者截图里的失败分组在总览的窄左列，而不是操作
    // 记录页的宽表；排在 states 前段，避免既有的 share-key-test-dns 超时把它藏掉。
    // 2026-10-31：失败分组随「需要处理」并入服务面板后移到「最近操作」横条。
    "overview-failures": {
      lane: 0,
      reads: { "/api/console/operations": OPS_LIST },
      probe: FAIL_LAYOUT,
      validateProbe: validateFailLayout,
      shots: ["section:.wb-ov-ops"],
    },
    "overview-failures-narrow": {
      lane: 0,
      reads: { "/api/console/operations": OPS_LIST },
      probe: FAIL_LAYOUT,
      validateProbe: validateFailLayout,
      viewport: { width: 1180, height: 800 },
      shots: ["section:.wb-ov-ops"],
    },
    "share-broker-unreachable": {
      lane: 6,
      shareTab: "diagnostics",
      shareFolds: ["layers-maintenance", "connection"],
      reads: {
        "/api/console/share-management/status": {
          status: 200,
          body: {
            ...SHARE_STATUS.body,
            broker: { running: false, error_code: "deadline_exceeded" },
            gateway_running: null,
            funnel_on: null,
            account: { running: null, valid: null },
            upstream: { reachable: null },
          },
        },
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      ops: {
        "share.diagnostics": {
          final: "succeeded",
          result: {
            checked_at: NOW,
            layers: {
              watchdog: SHARE_DIAG_LAYER("unavailable", "unavailable", false, {
                reason: "watchdog_broker_unavailable",
              }),
              share_backend: SHARE_DIAG_LAYER(
                "unconfirmed",
                "unconfirmed",
                false
              ),
              backend_auth: SHARE_DIAG_LAYER(
                "unconfirmed",
                "unconfirmed",
                false
              ),
            },
          },
        },
      },
      async drive(page) {
        await page.locator("[data-wb-share-diag]").first().click();
        await page.waitForTimeout(2000);
      },
      probe: () => ({
        guide:
          document.querySelector("[data-wb-share-broker-recovery]")
            ?.textContent ?? "",
        copyRecovery: Boolean(
          document.querySelector("[data-wb-share-copy-recovery]")
        ),
        evidence:
          document.querySelector(".wb-share-evidence")?.textContent ?? "",
        watchdog:
          document.querySelector(".wb-share-watchdog")?.textContent ?? "",
        enabledDependent: [
          ...document.querySelectorAll("[data-wb-share-action]"),
        ].filter(
          (el) =>
            !el.disabled &&
            [
              "share.start",
              "share.backend_login",
              "share.backend_restart",
              "share.backend_reauthenticate",
              "share.gateway_restart",
              "share.repair_limits",
            ].includes(el.dataset.wbShareAction)
        ).length,
        stop: Boolean(
          document.querySelector('[data-wb-share-action="share.estop"]')
        ),
        auth: document.querySelector(".wb-share-layer-grid")?.textContent ?? "",
      }),
      validateProbe: (probe, posts) => [
        ...(!probe.guide.includes("受限后台未响应")
          ? ["A failed broker probe must offer recovery guidance"]
          : []),
        ...(!probe.guide.includes("读取分层状态") ||
        !probe.guide.includes("认证有效")
          ? ["Recovery guidance must explain readback and skipped login"]
          : []),
        ...(!probe.copyRecovery
          ? ["A failed broker probe must offer 复制恢复命令"]
          : []),
        ...(probe.evidence.includes("broker 未运行")
          ? ["Broker unreachability is not proof that its process stopped"]
          : []),
        ...(probe.watchdog.includes("任务缺失") ||
        probe.auth.includes("认证失效")
          ? ["Unavailable protected evidence must remain unknown"]
          : []),
        ...(probe.enabledDependent !== 0
          ? ["Unavailable broker must not encourage dependent retries"]
          : []),
        ...(!probe.stop ? ["Emergency stop must remain available"] : []),
        ...(posts.length !== 1 || posts[0] !== "share.diagnostics"
          ? ["Only explicitly requested read-only diagnostics may be posted"]
          : []),
      ],
      shots: ["section:.wb-share-hero", "section:.wb-share-layers"],
    },
    "share-broker-restored": {
      lane: 6,
      shareTab: "diagnostics",
      shareFolds: ["layers-maintenance", "connection"],
      reads: {
        "/api/console/share-management/status": {
          status: 200,
          body: {
            ...SHARE_STATUS.body,
            broker: { running: false, error_code: "deadline_exceeded" },
            gateway_running: null,
            funnel_on: null,
            account: { running: null, valid: null },
          },
        },
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      ops: {
        "share.backend_login": {
          final: "succeeded",
          expectedTarget: "share",
          result: {
            state: "valid",
            triggered: false,
            started_here: false,
            note: "认证有效",
          },
        },
      },
      async drive(page) {
        const hadGuide = await page
          .locator("[data-wb-share-broker-recovery]")
          .count();
        await page.route("**/api/console/share-management/status", (route) =>
          route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              ...SHARE_STATUS.body,
              gateway_running: false,
              funnel_on: false,
              account: { running: true, valid: true },
            }),
          })
        );
        await page
          .locator("[data-wb-share-broker-recovery] [data-wb-share-refresh]")
          .click();
        await page
          .locator(".wb-share-evidence")
          .filter({ hasText: "受限后台已响应" })
          .waitFor();
        await page.evaluate((value) => {
          window.__brokerHadRecoveryGuide = value;
        }, hadGuide === 1);
        const button = page.locator(
          '.wb-share-layers [data-wb-share-action="share.backend_login"]'
        );
        await button.click();
        await page.waitForTimeout(200);
        await button.click();
        await page.waitForTimeout(2000);
      },
      probe: () => ({
        hadGuide: window.__brokerHadRecoveryGuide,
        guide: Boolean(
          document.querySelector("[data-wb-share-broker-recovery]")
        ),
        canStart: Boolean(
          document.querySelector(
            '[data-wb-share-action="share.start"]:not(:disabled)'
          )
        ),
        summary: document.querySelector(".wb-share-layers")?.textContent ?? "",
      }),
      validateProbe: (probe, posts) => [
        ...(!probe.hadGuide || probe.guide
          ? ["A fresh successful broker read must clear the recovery gate"]
          : []),
        ...(!probe.canStart
          ? [
              "A restored broker must make the authorized start entry available again",
            ]
          : []),
        ...(!probe.summary.includes("认证有效，无需重新登录")
          ? ["A valid account must still skip login after broker recovery"]
          : []),
        ...(posts.length !== 1 || posts[0] !== "share.backend_login"
          ? ["Readback may not launch recovery or start sharing automatically"]
          : []),
      ],
      shots: ["section:.wb-share-hero", "section:.wb-share-layers"],
    },
    "share-broker-start-failure": {
      lane: 6,
      shareTab: "keys",
      reads: {
        "/api/console/share-management/status": {
          status: 200,
          body: {
            ...SHARE_STATUS.body,
            gateway_running: false,
            funnel_on: false,
          },
        },
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      ops: {
        "share.start": {
          final: "failed",
          expectedTarget: "share",
          error: {
            code: "share_broker_unavailable",
            message: "受限后台管道不可达（夹具）",
          },
        },
      },
      async drive(page) {
        const button = page.locator('[data-wb-share-action="share.start"]');
        await button.click();
        await button.click();
        await page.waitForTimeout(2000);
      },
      probe: () =>
        [...document.querySelectorAll(".wb-notice-detail")]
          .map((el) => el.textContent ?? "")
          .join(" "),
      validateProbe: (probe, posts) => [
        ...(!probe.includes("先恢复受限后台") ||
        !probe.includes("Recover-ShareControl.bat")
          ? ["Broker operation failure must point to the fixed recovery entry"]
          : []),
        ...(posts.length !== 1 || posts[0] !== "share.start"
          ? ["A failed start must not automatically launch login or retry"]
          : []),
      ],
      shots: ["section:.wb-share-hero", "section:.wb-notices"],
    },
    "share-broker-unknown": {
      lane: 6,
      shareTab: "diagnostics",
      shareFolds: ["connection", "layers-maintenance"],
      reads: {
        "/api/console/share-management/status": {
          status: 200,
          body: {
            ...SHARE_STATUS.body,
            broker: null,
            gateway_running: null,
            funnel_on: null,
            account: { running: null, valid: null },
          },
        },
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      probe: () => ({
        guide: Boolean(
          document.querySelector("[data-wb-share-broker-recovery]")
        ),
        evidence:
          document.querySelector(".wb-share-evidence")?.textContent ?? "",
        login: Boolean(
          document.querySelector(
            '.wb-share-layers [data-wb-share-action="share.backend_login"]'
          )
        ),
      }),
      validateProbe: (probe, posts) => [
        ...(probe.guide
          ? ["Missing broker evidence cannot be labeled unreachable"]
          : []),
        ...(!probe.evidence.includes("未确认")
          ? ["Missing broker evidence must remain unconfirmed"]
          : []),
        ...(!probe.login
          ? [
              "Unknown evidence alone must not remove the authorized login route",
            ]
          : []),
        ...(posts.length
          ? ["A read must never start recovery automatically"]
          : []),
      ],
      shots: ["section:.wb-share-hero"],
    },
    ...Object.fromEntries(
      [
        [
          "um-login-valid",
          {
            final: "succeeded",
            result: { final_state: "valid", login_skipped: true },
          },
          "认证有效，无需重新登录",
        ],
        [
          "um-login-required",
          {
            final: "succeeded",
            result: { final_state: "valid", login_skipped: false },
          },
          "登录完成，认证有效",
        ],
        [
          "um-login-unknown",
          {
            final: "failed",
            error: {
              code: "login_verification_unknown",
              message: "无法确认当前认证，暂不打开登录；请稍后重试",
            },
          },
          "无法确认当前认证",
        ],
      ].map(([name, outcome, expected]) => [
        name,
        {
          lane: 1,
          reads: Object.fromEntries(
            Object.entries(gatewayConnectionFixtureReads()).map(
              ([path, read]) => [`/api/console${path}`, read]
            )
          ),
          ops: { "account.login": { ...outcome, expectedTarget: "UM" } },
          async drive(page) {
            const button = page
              .locator('[data-wb-acct-action="account.login"]')
              .first();
            await button.click();
            await button.click();
            await page.waitForTimeout(1800);
          },
          probe: () =>
            Object.fromEntries(
              [
                "认证有效，无需重新登录",
                "登录完成，认证有效",
                "无法确认当前认证",
              ].map((text) => [text, document.body.innerText.includes(text)])
            ),
          validateProbe: (outcomes, posts) => [
            ...(outcomes[expected] !== true
              ? [`Missing UM login outcome: ${expected}`]
              : []),
            ...(posts.filter((action) => action === "account.login").length !==
            1
              ? ["UM login must submit exactly once after confirmation"]
              : []),
          ],
          shots: ["first"],
        },
      ])
    ),
    "account-states": {
      lane: 1,
      // 读面路径按页面 transport 的前缀（/gw/usagesrv 之后的 /api/console 前缀）。
      reads: Object.fromEntries(
        Object.entries(gatewayConnectionFixtureReads()).map(([path, read]) => [
          `/api/console${path}`,
          read,
        ])
      ),
      ops: {
        "qoderglobal.quota": { final: "succeeded", result: QUOTA_RESULT },
        "account.um_status": {
          final: "succeeded",
          result: { authenticated: true, checked_epoch: NOW },
        },
        "qoderglobal.status": {
          final: "succeeded",
          result: {
            authenticated: false,
            checked_epoch: NOW,
            note: "会话已过期（夹具）",
          },
        },
        "qoder.status": {
          reject: { status: 502, body: { error: "upstream_unreachable" } },
        },
      },
      // Rev17: one gateway detail at a time - select each gateway, then query it.
      async drive(page) {
        for (const [id, a] of [
          ["0", "account.um_status"],
          ["2", "qoderglobal.status"],
          ["4", "qoder.status"],
        ]) {
          await page.click(`[data-wb-gw-sel="${id}"]`);
          await page.waitForTimeout(300);
          const b = page.locator(`[data-wb-acct-action="${a}"]`).first();
          if (await b.count()) {
            await b.click();
            await page.waitForTimeout(400);
          }
        }
        await page.waitForTimeout(3000);
      },
      shots: ["first", "full"],
      probe: () =>
        [...document.querySelectorAll(".wb-content .wb-kv")]
          .filter((k) => /认证状态|登录状态/.test(k.textContent))
          .map((k) => k.textContent.replace(/\s+/g, " ").trim().slice(0, 120)),
    },
    "models-loaded": {
      lane: 5,
      ops: { "model.catalog": { final: "succeeded", result: CATALOG_RESULT } },
      async drive(page) {
        await page.click('[data-wb-model-action="model.catalog"]');
        await page.waitForTimeout(3000);
      },
      shots: ["first", "full"],
    },
    "models-failed": {
      lane: 5,
      reads: {
        "/api/console/model-pending": {
          status: 500,
          body: { error: "model_pending_failed", detail: "夹具：读口返回 500" },
        },
      },
      ops: {
        "model.catalog": {
          reject: {
            status: 503,
            body: {
              error: "catalog_unavailable",
              reason: "四源目录读取失败（夹具）",
            },
          },
        },
      },
      async drive(page) {
        await page.click('[data-wb-model-action="model.catalog"]');
        await page.waitForTimeout(1500);
      },
      shots: ["first", "full"],
      // Rev19 模型读取失败：目录读取失败必须写失败原因 + 重试入口，绝不折成
      // 空表或「尚未读取」；未读取（还没问过）与读取失败是两件事。
      probe: () => {
        const section = [...document.querySelectorAll("section")].find((s) =>
          /模型目录/.test(s.querySelector("h3")?.textContent ?? "")
        );
        return section
          ? section
              .querySelector("h3")
              .textContent.replace(/\s+/g, " ")
              .trim()
              .slice(0, 60) +
              " | " +
              section.textContent.replace(/\s+/g, " ").trim().slice(0, 220)
          : null;
      },
    },
    "share-reveal": {
      lane: 6,
      shareTab: "keys",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/share-management/keys/fx-key-a/secret": {
          status: 200,
          body: { secret: "um-share-FIXTURE-not-a-real-key-0001" },
        },
      },
      async drive(page) {
        const b = page.locator(
          '[data-wb-share-key="reveal-secret"][data-wb-share-key-id="fx-key-a"]'
        );
        if (await b.count()) {
          await b.click();
          await page.waitForTimeout(800);
        }
        await openShareTab(page, "usage");
        await page.locator(".wb-share-usage-table tbody tr").first().waitFor();
      },
      shots: ["first", "full"],
      // Rev19 未归属 Token 细分：读面（/api/console/share-usage）的未归属桶已带
      // tokenUsage，页面必须按同一行结构显示细分，不再写「未拆分口径」。
      // R3: the summary table's last row is the unattributed bucket, and the
      // captured (legacy) response carries no CNY fields, so every RMB cell must
      // say 人民币读数未提供 rather than folding it to ¥0.
      probe: () => {
        const rows = [
          ...document.querySelectorAll(
            ".wb-share-usage-table tbody tr.wb-key-summary"
          ),
        ];
        const row = rows.at(-1);
        // Read the summary row only: the drill is a separate full-width row
        // below it and must not satisfy a money assertion on the row itself.
        const cells = row ? [...row.querySelectorAll("td")] : [];
        return row
          ? {
              rows: rows.length,
              cells: cells.map((td) =>
                td.textContent.replace(/\s+/g, " ").trim().slice(0, 120)
              ),
            }
          : null;
      },
      validateProbe: (probe) => [
        ...(!probe ? ["The usage tab did not render a summary table"] : []),
        ...(probe?.rows !== 6
          ? [
              `The usage table must keep every key and the unattributed row (got ${probe?.rows})`,
            ]
          : []),
        ...(!probe?.cells?.[0]?.includes("未归属")
          ? ["Unattributed usage must stay its own row"]
          : []),
        ...(!probe?.cells?.some((cell) => cell.includes("人民币读数未提供"))
          ? ["A legacy response without CNY fields must say 人民币读数未提供"]
          : []),
      ],
    },
    // Rev19 分享监督/维护读面：分层维护卡的监督状态、陈旧读数与维护前后比较都
    // 来自页面既有的只读读面（share.diagnostics 的结果 + 两处单层重启动作自己的
    // 终态结果）。夹具形状照 console_share_actions 的 diagnostics / backend_restart
    // 返回，值全部是合成值；此处不触任何真实服务。
    "share-maintenance": {
      lane: 6,
      shareTab: "diagnostics",
      shareFolds: ["layers-maintenance"],
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      ops: {
        "share.diagnostics": { final: "succeeded", result: SHARE_DIAGNOSTICS },
        "share.backend_restart": {
          final: "succeeded",
          result: SHARE_RESTART_RESULT,
        },
      },
      async drive(page) {
        const diag = page.locator("[data-wb-share-diag]").first();
        if (await diag.count()) {
          await diag.click();
          await page.waitForTimeout(700);
        }
        const restart = page
          .locator('[data-wb-share-action="share.backend_restart"]')
          .first();
        // 二次确认（第一次点按只武装按钮）后等过轮询周期（1.2s）再读终态，
        // 这样终态结果与「维护前后比较」都真的落过屏，而不是停在「进行中」。
        if (await restart.count()) {
          await restart.click();
          await page.waitForTimeout(300);
          await restart.click();
          await page.waitForTimeout(3500);
        }
        const watchdog = page.locator(".wb-share-watchdog");
        // R3 moved the layer summary out of the watchdog card; the running
        // badge now lives on the summary row and the card carries the evidence.
        await page
          .locator(".wb-share-layer-summary li")
          .filter({ hasText: "看门狗任务" })
          .filter({ hasText: "运行中" })
          .waitFor();
        await watchdog.filter({ hasText: "Running" }).waitFor();
        if (!(await watchdog.innerText()).includes("0x800710E0"))
          throw Error("Historical task result must remain visible");
        if (
          (await page.locator(".wb-share-layers").innerText()).includes(
            "入口到 8110 的连接"
          )
        )
          throw Error("Tailscale reading is mislabeled");
      },
      shots: ["section:.wb-share-layers"],
      probe: () => {
        const section = document.querySelector(".wb-share-layers");
        const compare = section?.querySelector(".wb-share-layer-compare");
        const time = compare?.querySelector("li > small:last-child");
        const rect = time?.getBoundingClientRect();
        const grid = section?.querySelector(".wb-share-layer-grid");
        return {
          text: section
            ? section.textContent.replace(/\s+/g, " ").trim().slice(0, 1400)
            : null,
          compareTimeWidth: rect ? Math.round(rect.width) : null,
          compareTimeHeight: rect ? Math.round(rect.height) : null,
          gridMaxWidth: grid ? getComputedStyle(grid).maxWidth : null,
        };
      },
      validateProbe: (probe) => [
        ...(probe?.text == null
          ? ["The maintenance layers section did not render"]
          : []),
        ...(probe?.gridMaxWidth !== "none"
          ? [
              `The layer grid must use the panel width, not ${probe?.gridMaxWidth}`,
            ]
          : []),
        ...(probe?.compareTimeWidth == null || probe.compareTimeWidth < 120
          ? [`The compare time cell collapsed to ${probe?.compareTimeWidth}px`]
          : []),
        ...(probe?.compareTimeHeight == null || probe.compareTimeHeight > 60
          ? [`The compare time cell wrapped to ${probe?.compareTimeHeight}px`]
          : []),
      ],
    },
    ...Object.fromEntries(
      [
        [
          "share-backend-stopped",
          { running: false, valid: null },
          {
            triggered: false,
            state: "valid",
            label: "认证有效",
            started_here: true,
            note: "专用后端上游认证已有效（无需触发登录）",
          },
          "8110 已启动；认证有效，无需重新登录",
        ],
        [
          "share-backend-auth-unknown",
          { running: true },
          {
            triggered: true,
            state: "unknown",
            label: "状态未确认",
            started_here: false,
            note: "登录触发已受理；当前认证状态未确认",
          },
          "当前认证状态未确认",
        ],
        [
          "share-backend-logging-in",
          { running: true, valid: false },
          {
            triggered: true,
            state: "logging_in",
            label: "登录中",
            started_here: true,
            note: "请在专用实例浏览器完成 UM 登录",
          },
          "登录进行中，请在专用 Edge 完成登录",
        ],
        [
          "share-backend-valid",
          { running: true, valid: true },
          {
            triggered: false,
            state: "valid",
            label: "认证有效",
            started_here: false,
            note: "专用后端上游认证已有效（无需触发登录）",
          },
          "认证有效，无需重新登录",
        ],
      ].map(([name, account, result, expected]) => [
        name,
        {
          lane: 6,
          shareTab: "diagnostics",
          shareFolds: ["layers-maintenance"],
          clock: true,
          reads: {
            "/api/console/share-management/status": {
              ...SHARE_STATUS,
              body: { ...SHARE_STATUS.body, account },
            },
            "/api/console/share-management/keys": SHARE_KEYS,
          },
          ops: {
            "share.backend_login": {
              final: "succeeded",
              result,
              expectedTarget: "share",
            },
          },
          async drive(page) {
            const button = page.locator(
              '.wb-share-layers [data-wb-share-action="share.backend_login"]'
            );
            if (!(await button.count())) return;
            let submitted = 0;
            page.on("request", (req) => {
              if (
                req.method() === "POST" &&
                req.url().endsWith("/api/console/operations")
              )
                submitted++;
            });
            await button.click();
            await page.waitForTimeout(200);
            if (submitted !== 0)
              throw Error(
                "The first start/login click must only ask for confirmation"
              );
            const confirmation = await button.innerText();
            if (
              !confirmation.includes("启动") ||
              !confirmation.includes("有效则跳过")
            )
              throw Error(
                "Start/login confirmation must explain cold start and skipped login"
              );
            await page.clock.fastForward(30000);
            await page.waitForTimeout(350);
            await page.evaluate(
              (same) => {
                window.__shareLoginConfirmKept = same;
              },
              (await button.innerText()) === confirmation
            );
            await button.click();
            await page.waitForTimeout(2000);
          },
          probe: () => ({
            layerLogin: Boolean(
              document.querySelector(
                '.wb-share-layers [data-wb-share-action="share.backend_login"]'
              )
            ),
            entryLogin: Boolean(
              document.querySelector(
                '.wb-share-layers [data-wb-share-action="share.backend_login"]'
              )
            ),
            summary:
              document.querySelector(".wb-share-layers")?.textContent ?? "",
            diagnosticsUnknown: document
              .querySelector(".wb-share-layers")
              ?.textContent.includes("未读取前各层一律未知"),
            resultState:
              document.querySelector(
                '[data-svc-result="act:share.backend_login"]'
              )?.className ?? "",
            confirmKept: window.__shareLoginConfirmKept === true,
          }),
          validateProbe: (probe, posts) => [
            ...(!probe.layerLogin
              ? ["8110 maintenance must offer the authorized start/login entry"]
              : []),
            ...(account.valid !== true && !probe.entryLogin
              ? [
                  "Stopped or unknown account must keep a visible start/login entry",
                ]
              : []),
            ...(!probe.summary.includes(expected)
              ? [`Missing dedicated backend login outcome: ${expected}`]
              : []),
            ...(result.state !== "valid" &&
            !probe.resultState.includes("unconfirmed")
              ? [
                  "Accepted or unknown authentication must not be presented as authenticated success",
                ]
              : []),
            ...(!probe.diagnosticsUnknown
              ? ["A login result must not invent unread diagnostic layers"]
              : []),
            ...(!probe.confirmKept
              ? ["Periodic read must keep confirmation on the chosen entry"]
              : []),
            ...(posts.length !== 1 || posts[0] !== "share.backend_login"
              ? ["Only one explicitly confirmed backend login may be submitted"]
              : []),
          ],
          shots: ["section:.wb-share-layers"],
        },
      ])
    ),
    "share-backend-restart-stopped": {
      lane: 6,
      shareTab: "diagnostics",
      shareFolds: ["layers-maintenance"],
      reads: {
        "/api/console/share-management/status": {
          ...SHARE_STATUS,
          body: {
            ...SHARE_STATUS.body,
            account: { running: false, valid: null },
          },
        },
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      ops: {
        "share.backend_restart": {
          final: "failed",
          expectedTarget: "share",
          error: {
            code: "share_backend_not_running",
            message:
              "8110 未确认运行（state=pid_file_absent，identity=unknown）：绝不冷启动；先经 share.backend_login 或授权入口",
          },
        },
      },
      async drive(page) {
        const button = page
          .locator('[data-wb-share-action="share.backend_restart"]')
          .first();
        const label = await button.innerText();
        await button.click();
        const confirmation = await button.innerText();
        await page.evaluate(
          (values) => {
            window.__shareRestartProbe = values;
          },
          { label, confirmation }
        );
        await button.click();
        await page.waitForTimeout(2000);
      },
      probe: () => ({
        ...window.__shareRestartProbe,
        layer: document.querySelector(".wb-share-layers")?.textContent ?? "",
        startEntry: Boolean(
          document.querySelector(
            '.wb-share-layers [data-wb-share-action="share.backend_login"]'
          )
        ),
      }),
      validateProbe: (probe, posts) => [
        ...(probe.label.includes("恢复")
          ? ["A strict restart must not promise recovery of a stopped process"]
          : []),
        ...(!probe.confirmation.includes("不冷启动")
          ? [
              "Restart confirmation must explain the running-instance requirement",
            ]
          : []),
        ...(!probe.startEntry ||
        !probe.layer.includes("请点“启动 / 按需登录 8110”")
          ? [
              "A stopped-backend failure must point to the available authorized start/login entry",
            ]
          : []),
        ...(posts.length !== 1 || posts[0] !== "share.backend_restart"
          ? ["A failed restart must not automatically submit start/login"]
          : []),
      ],
      shots: ["section:.wb-share-layers"],
    },
    "share-key-test": {
      lane: 6,
      shareTab: "keys",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      keyProbe: true,
      ops: {
        "share.diagnostics": {
          final: "succeeded",
          result: {
            ...SHARE_DIAGNOSTICS,
            layers: {
              ...SHARE_DIAGNOSTICS.layers,
              security_gate: SHARE_DIAG_LAYER(
                "unconfirmed",
                "unconfirmed",
                false,
                {
                  evidence: {
                    check: "limits",
                    verdict: null,
                    detail: "wfp-query-failed",
                  },
                }
              ),
            },
          },
        },
      },
      async drive(page) {
        const button = page.locator(
          '[data-wb-share-key="test"][data-wb-share-key-id="fx-key-a"]'
        );
        await button.click();
        await page
          .locator('.wb-share-key-test[data-wb-share-key-id="fx-key-a"]')
          .filter({ hasText: "已连通" })
          .waitFor();
        if (
          !(
            await page
              .locator('.wb-share-key-test[data-wb-share-key-id="fx-key-a"]')
              .textContent()
          ).includes("公网往返")
        )
          throw Error("Probe must identify the public path");
        if (
          !(await page
            .locator(
              '[data-wb-share-key="test"][data-wb-share-key-id="fx-key-c"]'
            )
            .isDisabled())
        )
          throw Error("Disabled key must not probe");
        // The readiness evidence and security explanation live in the
        // diagnostics tab; the key test itself stays on the keys tab.
        await openShareTab(page, "diagnostics");
        await openShareFold(page, "connection");
        await openShareFold(page, "layers-maintenance");
        await page
          .locator('[data-wb-share-reading="dns"]')
          .filter({ hasText: "已解析（测试时）" })
          .waitFor();
        await page
          .locator('[data-wb-share-reading="model"]')
          .filter({ hasText: "已回复（测试时）" })
          .waitFor();
        await page.locator("[data-wb-share-diag]").first().click();
        await page.locator("[data-wb-share-security-details]").waitFor();
        await page.locator("[data-wb-share-security-details]").click();
        await page
          .locator(".wb-share-safety-reason")
          .filter({ hasText: "不能据此确认安全" })
          .waitFor();
        await openShareTab(page, "keys");
      },
      shots: [
        "section:.wb-share-hero",
        "section:.wb-share-key-table",
        { tab: "diagnostics", selector: "section:.wb-share-layers" },
      ],
      probe: () => ({
        result: document.querySelector(
          '.wb-share-key-test[data-wb-share-key-id="fx-key-a"]'
        )?.textContent,
        safety: document.querySelector(".wb-share-safety-reason")?.textContent,
      }),
    },
    "share-key-test-tls": {
      lane: 6,
      shareTab: "keys",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      keyProbe: {
        connected: false,
        reason: "tls_handshake_failed",
        stage: "models",
        http_status: null,
        model: null,
      },
      async drive(page) {
        await page
          .locator(
            '[data-wb-share-key="test"][data-wb-share-key-id="fx-key-a"]'
          )
          .click();
        const result = page.locator(
          '.wb-share-key-test[data-wb-share-key-id="fx-key-a"]'
        );
        await result.filter({ hasText: "公网 TLS 握手失败" }).waitFor();
        if ((await result.textContent()).includes("已连通"))
          throw Error("TLS failure cannot appear connected");
      },
      shots: ["section:.wb-share-key-table"],
      probe: () =>
        document.querySelector(
          '.wb-share-key-test[data-wb-share-key-id="fx-key-a"]'
        )?.textContent,
    },
    "share-readiness-degraded": {
      lane: 6,
      shareTab: "diagnostics",
      shareFolds: ["connection"],
      reads: {
        "/api/console/share-management/status": {
          status: 200,
          body: {
            ...SHARE_STATUS.body,
            account: { running: false, valid: false },
          },
        },
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      async drive(page) {
        // The compact overview still carries the local reading; the connection
        // fold carries the mapping / DNS / model evidence.
        await page
          .locator('[data-wb-share-top="local"]')
          .filter({ hasText: "未就绪" })
          .waitFor();
        if (
          (await page.locator("[data-wb-share-state]").innerText()).includes(
            "本机就绪 · 映射已开启"
          )
        )
          throw Error("A mapping must not hide a stopped backend");
        await page
          .locator('[data-wb-share-reading="local"]')
          .filter({ hasText: "未就绪" })
          .waitFor();
        if (await page.locator(".wb-share-open").count())
          throw Error("A mapping must not hide a stopped backend");
        await page
          .locator('[data-wb-share-reading="dns"]')
          .filter({ hasText: "未测试" })
          .waitFor();
      },
      shots: ["section:.wb-share-hero"],
    },
    "share-readiness-unknown": {
      lane: 6,
      shareTab: "keys",
      reads: {
        "/api/console/share-management/status": {
          status: 200,
          body: { gateway_running: false },
        },
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      async drive(page) {
        await page
          .locator(".wb-share-pill")
          .filter({ hasText: "状态未确认" })
          .waitFor();
        if (
          (await page.locator("[data-wb-share-state]").innerText()).includes(
            "未开放"
          )
        )
          throw Error("Missing Funnel evidence must stay unknown");
        await page
          .locator(".wb-share-closed-mark")
          .filter({ hasText: "映射未确认" })
          .waitFor();
      },
      shots: ["section:.wb-share-hero", "section:.wb-share-access-url"],
    },
    "share-new-start-after-old-stop": {
      lane: 6,
      shareTab: "keys",
      reads: {
        "/api/console/share-management/status": {
          status: 200,
          body: {
            ...SHARE_STATUS.body,
            account: { running: false, valid: false },
          },
        },
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/operations": {
          status: 200,
          body: {
            operations: [
              {
                id: "fx-new-start",
                action: "share.start",
                target: "share",
                state: "succeeded",
                created_at: NOW - 10,
              },
              {
                id: "fx-old-stop",
                action: "share.stop",
                target: "share",
                state: "succeeded",
                created_at: NOW - 30,
              },
            ],
          },
        },
      },
      async drive(page) {
        await page
          .locator(".wb-share-pill")
          .filter({ hasText: "映射已开启 · 本机未就绪" })
          .waitFor();
        if (
          (await page.locator("[data-wb-share-state]").innerText()).includes(
            "关闭有残留"
          )
        )
          throw Error("A superseded stop must not describe a later outage");
      },
      shots: ["first"],
    },
    "share-key-test-dns": {
      lane: 6,
      shareTab: "keys",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      keyProbe: {
        connected: false,
        reason: "public_dns_failed",
        stage: "dns",
        public_ip: null,
        http_status: null,
        model: null,
        public_url: SHARE_STATUS.body.share_url,
      },
      async drive(page) {
        await page
          .locator(
            '[data-wb-share-key="test"][data-wb-share-key-id="fx-key-a"]'
          )
          .click();
        await openShareTab(page, "diagnostics");
        await openShareFold(page, "connection");
        await page
          .locator('[data-wb-share-reading="dns"]')
          .filter({ hasText: "解析未通过" })
          .waitFor();
        await page
          .locator('[data-wb-share-reading="model"]')
          .filter({ hasText: "未进入模型推理" })
          .waitFor();
        await page
          .locator('[data-wb-share-reading="local"]')
          .filter({ hasText: "已就绪" })
          .waitFor();
      },
      shots: ["section:.wb-share-hero"],
    },
    "share-key-test-stale": {
      lane: 6,
      shareTab: "keys",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      keyProbe: {
        checked_at: NOW - 600,
        public_url: SHARE_STATUS.body.share_url,
      },
      async drive(page) {
        await page
          .locator(
            '[data-wb-share-key="test"][data-wb-share-key-id="fx-key-a"]'
          )
          .click();
        await openShareTab(page, "diagnostics");
        await openShareFold(page, "connection");
        await page
          .locator('[data-wb-share-reading="model"]')
          .filter({ hasText: "已陈旧" })
          .waitFor();
        if (
          await page
            .locator('[data-wb-share-reading="model"] .wb-badge-ok')
            .count()
        )
          throw Error("Old model replies must not appear fresh");
      },
      shots: ["section:.wb-share-hero"],
    },
    "share-key-test-version-change": {
      lane: 6,
      shareTab: "keys",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      keyProbe: { public_url: SHARE_STATUS.body.share_url },
      async drive(page) {
        await page
          .locator(
            '[data-wb-share-key="test"][data-wb-share-key-id="fx-key-a"]'
          )
          .click();
        await openShareTab(page, "diagnostics");
        await openShareFold(page, "connection");
        await page
          .locator('[data-wb-share-reading="model"]')
          .filter({ hasText: "已回复" })
          .waitFor();
        await page.route("**/api/console/share-management/keys", (route) =>
          route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              ...SHARE_KEYS.body,
              keys: SHARE_KEYS.body.keys.map((r) =>
                r.id === "fx-key-a" ? { ...r, row_version: 4 } : r
              ),
            }),
          })
        );
        await page.locator("[data-wb-share-refresh]").first().click();
        await page
          .locator('[data-wb-share-reading="model"]')
          .filter({ hasText: "未测试" })
          .waitFor();
        if (
          (
            (await page
              .locator('.wb-share-key-test[data-wb-share-key-id="fx-key-a"]')
              .textContent()) || ""
          ).trim()
        )
          throw Error("A rotated key must drop the old sample");
      },
      shots: ["section:.wb-share-hero"],
    },
    "share-watchdog-unavailable": {
      lane: 6,
      shareTab: "diagnostics",
      shareFolds: ["layers-maintenance"],
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      ops: {
        "share.diagnostics": {
          final: "succeeded",
          result: {
            ...SHARE_DIAGNOSTICS,
            layers: {
              ...SHARE_DIAGNOSTICS.layers,
              watchdog: SHARE_DIAG_LAYER("unavailable", "unavailable", false, {
                reason: "watchdog_capability_missing",
              }),
            },
          },
        },
      },
      async drive(page) {
        await page.locator("[data-wb-share-diag]").first().click();
        await page
          .locator(".wb-share-watchdog")
          .filter({ hasText: "需要更新后台" })
          .waitFor();
        if (
          (await page.locator(".wb-share-watchdog").innerText()).includes(
            "任务缺失"
          )
        )
          throw Error("Old broker support is unknown, not missing task");
      },
      shots: ["section:.wb-share-watchdog"],
    },
    "share-watchdog-queued": {
      lane: 6,
      shareTab: "diagnostics",
      shareFolds: ["layers-maintenance"],
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      ops: {
        "share.diagnostics": {
          final: "succeeded",
          result: {
            ...SHARE_DIAGNOSTICS,
            layers: {
              ...SHARE_DIAGNOSTICS.layers,
              watchdog: SHARE_DIAG_LAYER("queued", "confirmed", false, {
                reason: "watchdog_task_queued",
                evidence: {
                  ...SHARE_DIAGNOSTICS.layers.watchdog.evidence,
                  task_state: "Queued",
                },
              }),
            },
          },
        },
      },
      async drive(page) {
        await page.locator("[data-wb-share-diag]").first().click();
        // R3: the running badge sits on the layer summary row; the watchdog
        // card carries the backend's own task state.
        await page
          .locator(".wb-share-layer-summary li")
          .filter({ hasText: "看门狗任务" })
          .filter({ hasText: "等待运行" })
          .waitFor();
        await page
          .locator(".wb-share-watchdog")
          .filter({ hasText: "Queued" })
          .waitFor();
      },
      shots: ["section:.wb-share-watchdog"],
    },
    "share-key-test-old-local": {
      lane: 6,
      shareTab: "keys",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      keyProbe: { tested_from: "local" },
      async drive(page) {
        await page
          .locator(
            '[data-wb-share-key="test"][data-wb-share-key-id="fx-key-a"]'
          )
          .click();
        await page
          .locator('.wb-share-key-test[data-wb-share-key-id="fx-key-a"]')
          .filter({ hasText: "接口尚未支持公网模型测试" })
          .waitFor();
      },
      shots: ["section:.wb-share-key-table"],
      probe: () =>
        document.querySelector(
          '.wb-share-key-test[data-wb-share-key-id="fx-key-a"]'
        )?.textContent,
    },
    "share-key-test-catalog-only": {
      lane: 6,
      shareTab: "keys",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      keyProbe: { stage: "models" },
      async drive(page) {
        await page
          .locator(
            '[data-wb-share-key="test"][data-wb-share-key-id="fx-key-a"]'
          )
          .click();
        await page
          .locator('.wb-share-key-test[data-wb-share-key-id="fx-key-a"]')
          .filter({ hasText: "接口尚未支持公网模型测试" })
          .waitFor();
      },
      shots: ["section:.wb-share-key-table"],
      probe: () =>
        document.querySelector(
          '.wb-share-key-test[data-wb-share-key-id="fx-key-a"]'
        )?.textContent,
    },
    "share-public-recovery": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
        // 控制台夹具（api.json）早于 S5 动作注册；本场景只补两个新动作，
        // 其余动作仍来自同一份夹具，不覆盖既有能力表。
        "/api/console/actions": PUBLIC_SHARE_ACTIONS(),
        "/api/console/share-management/public": PUBLIC_SHARE({
          budget: {
            unreadable: false,
            mapping_used: 2,
            ts_restart_used: 1,
            mapping_attempts_utc: [new Date((NOW - 300) * 1000).toISOString()],
            ts_restarts_utc: [new Date((NOW - 240) * 1000).toISOString()],
            mapping_next_eligible_at_utc: new Date(
              (NOW + 1500) * 1000
            ).toISOString(),
            ts_restart_next_eligible_at_utc: new Date(
              (NOW + 1560) * 1000
            ).toISOString(),
          },
          snapshot: {
            overall: "all_failed",
            reason: "all_entries_failed",
            entries: [
              PUBLIC_ENTRY("failed", "tls_handshake_failed", "203.0.113.10"),
              PUBLIC_ENTRY("failed", "timeout", "203.0.113.11"),
            ],
            local: {
              ts_service: "running",
              connectivity: "ok",
              backend: "running",
              auth: "valid",
              gateway: "running",
              security_gate: "ok",
              maintenance: "active",
            },
            recovery: {
              state: "budget_exhausted",
              action: "mapping_resend_after_ts",
              stage: "verify",
              reason: "budget_exhausted",
              attempted_at_utc: new Date((NOW - 300) * 1000).toISOString(),
              finished_at_utc: new Date((NOW - 10) * 1000).toISOString(),
              manual: true,
              actions: [
                {
                  action: "mapping_resend",
                  at_utc: new Date((NOW - 300) * 1000).toISOString(),
                  result: "ok",
                },
                {
                  action: "ts_restart",
                  at_utc: new Date((NOW - 240) * 1000).toISOString(),
                  result: "ok",
                },
                {
                  action: "mapping_resend_after_ts",
                  at_utc: new Date((NOW - 180) * 1000).toISOString(),
                  result: "unconfirmed",
                },
              ],
            },
            budget: {
              unreadable: false,
              window_s: 1800,
              mapping_used: 2,
              mapping_limit: 2,
              ts_restart_used: 1,
              ts_restart_limit: 1,
              mapping_attempts_utc: [
                new Date((NOW - 300) * 1000).toISOString(),
              ],
              ts_restarts_utc: [new Date((NOW - 240) * 1000).toISOString()],
              mapping_next_eligible_at_utc: new Date(
                (NOW + 1500) * 1000
              ).toISOString(),
              ts_restart_next_eligible_at_utc: new Date(
                (NOW + 1560) * 1000
              ).toISOString(),
            },
          },
        }),
      },
      async drive(page) {
        await page
          .locator("[data-wb-share-public-overall]")
          .filter({ hasText: "全部入口失败" })
          .waitFor();
        await page
          .locator('[data-wb-share-public-reading="recovery"]')
          .filter({ hasText: "预算耗尽" })
          .waitFor();
        await page
          .locator('[data-wb-share-public-reading="budget"]')
          .filter({ hasText: "下次可重试" })
          .waitFor();
        // 动作注册表加载完成后，一键复查应可用；受限重试只应因预算耗尽而禁用。
        // R3: the public fault banner in the overview and the public card in
        // the diagnostics tab both offer 一键复查; this scenario reads the card.
        await page.waitForFunction(
          () => {
            const button = document.querySelector(
              '[data-wb-share-public-card] [data-wb-share-public="check"]'
            );
            return button && !button.disabled;
          },
          null,
          { timeout: 10000 }
        );
        if (
          !(await page.locator('[data-wb-share-public="retry"]').isDisabled())
        )
          throw Error("预算耗尽时受限重试必须禁用");
        const text = await page
          .locator("[data-wb-share-public-card]")
          .innerText();
        if (!text.includes("预算耗尽"))
          throw Error("受限重试的禁用原因必须写明预算耗尽");
        const tlsSemantics =
          text.includes("TLS 通过不代表模型回复已验证") ||
          text.includes("模型回复以密钥的手动测试为准");
        if (!text.includes("故障时间线") || !tlsSemantics)
          throw Error("公网卡片缺少时间线或 TLS 口径");
        if (text.includes("无事件"))
          throw Error("事件列表存在时不能写「无事件」");
      },
      shots: ["section:.wb-share-public"],
      probe: () =>
        document.querySelector("[data-wb-share-public-card]")?.textContent,
    },
    "share-public-stale": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/actions": PUBLIC_SHARE_ACTIONS(),
        "/api/console/share-management/public": PUBLIC_SHARE({
          snapshot: {
            observed_at_utc: new Date((NOW - 3600) * 1000).toISOString(),
            observed_at_epoch: NOW - 3600,
            round: {
              id: "fixture-round-stale",
              phase: "normal",
              started_at_utc: new Date((NOW - 3600) * 1000).toISOString(),
              finished_at_utc: new Date((NOW - 3600) * 1000).toISOString(),
              confirmation: 0,
              next_check_at_utc: new Date((NOW - 3540) * 1000).toISOString(),
              next_action_at_utc: null,
            },
          },
        }),
      },
      async drive(page) {
        await page
          .locator("[data-wb-share-public-overall]")
          .filter({ hasText: "结果陈旧" })
          .waitFor();
        const overall = await page
          .locator("[data-wb-share-public-overall]")
          .innerText();
        if (overall.includes("正常")) throw Error("陈旧快照不能显示为正常");
        const observed = await page
          .locator('[data-wb-share-public-reading="observed"]')
          .innerText();
        if (observed.includes("未提供"))
          throw Error("卡片必须渲染最近观测时间");
      },
      shots: ["section:.wb-share-public"],
      probe: () =>
        document.querySelector("[data-wb-share-public-card]")?.textContent,
    },
    "share-public-cancel": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/actions": PUBLIC_SHARE_ACTIONS(),
        "/api/console/share-management/public": PUBLIC_SHARE({
          status: { cancel: { cancelled: true, reason: "manual_close" } },
          snapshot: {
            overall: "all_failed",
            reason: "all_entries_failed",
            entries: [
              PUBLIC_ENTRY("failed", "tls_handshake_failed", "203.0.113.10"),
            ],
          },
        }),
      },
      async drive(page) {
        await page.waitForFunction(
          () => {
            const button = document.querySelector(
              '[data-wb-share-public="retry"]'
            );
            return button && button.disabled;
          },
          null,
          { timeout: 10000 }
        );
        const text = await page
          .locator("[data-wb-share-public-card]")
          .innerText();
        if (!text.includes("取消信号生效中"))
          throw Error("取消生效时必须写明受限重试禁用原因");
        // A paused round must not still advertise a due next check/action: that
        // reads as "it will check again", contradicting the pause.
        const nextCheck = await page
          .locator('[data-wb-share-public-reading="next-check"]')
          .innerText();
        const nextAction = await page
          .locator('[data-wb-share-public-reading="next-action"]')
          .innerText();
        if (!nextCheck.includes("暂停") || nextCheck.includes("等待下一轮"))
          throw Error(`暂停后下次检查不得写成等待下一轮：${nextCheck}`);
        if (!nextAction.includes("不自动") || nextAction.includes("等待下一轮"))
          throw Error(`暂停后下次动作不得写成等待下一轮：${nextAction}`);
      },
      shots: ["section:.wb-share-public"],
    },
    "share-public-not-failed": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/actions": PUBLIC_SHARE_ACTIONS(),
        "/api/console/share-management/public": PUBLIC_SHARE(),
      },
      async drive(page) {
        await page.waitForFunction(
          () => {
            const button = document.querySelector(
              '[data-wb-share-public="retry"]'
            );
            return button && button.disabled;
          },
          null,
          { timeout: 10000 }
        );
        const text = await page
          .locator("[data-wb-share-public-card]")
          .innerText();
        if (!text.includes("仅当全部入口失败时可受限重试"))
          throw Error("非 all_failed 必须禁用受限重试并写明原因");
      },
      shots: ["section:.wb-share-public"],
    },
    "share-public-maintenance-unavailable": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/actions": PUBLIC_SHARE_ACTIONS(),
        "/api/console/share-management/public": PUBLIC_SHARE({
          snapshot: {
            overall: "all_failed",
            reason: "all_entries_failed",
            entries: [PUBLIC_ENTRY("failed", "timeout", "203.0.113.10")],
            local: { maintenance: "unavailable" },
          },
        }),
      },
      async drive(page) {
        await page.waitForFunction(
          () => {
            const button = document.querySelector(
              '[data-wb-share-public="retry"]'
            );
            return button && button.disabled;
          },
          null,
          { timeout: 10000 }
        );
        const text = await page
          .locator("[data-wb-share-public-card]")
          .innerText();
        if (!text.includes("维护租约为 unavailable"))
          throw Error("维护不可读必须禁用受限重试并写明原因");
      },
      shots: ["section:.wb-share-public"],
    },
    "share-public-broker-unavailable": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/status": {
          status: 200,
          body: {
            ...SHARE_STATUS.body,
            broker: { ...SHARE_STATUS.body.broker, running: false },
          },
        },
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/actions": PUBLIC_SHARE_ACTIONS(),
        "/api/console/share-management/public": PUBLIC_SHARE({
          snapshot: {
            overall: "all_failed",
            reason: "all_entries_failed",
            entries: [PUBLIC_ENTRY("failed", "timeout", "203.0.113.10")],
          },
        }),
      },
      async drive(page) {
        await page.waitForFunction(
          () => {
            const button = document.querySelector(
              '[data-wb-share-public="retry"]'
            );
            return button && button.disabled;
          },
          null,
          { timeout: 10000 }
        );
        const text = await page
          .locator("[data-wb-share-public-card]")
          .innerText();
        if (!text.includes("受限后台未响应，先恢复后台"))
          throw Error("后台不可用时受限重试必须禁用并写明原因");
      },
      shots: ["section:.wb-share-public"],
    },
    "share-public-maintenance-none": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/actions": PUBLIC_SHARE_ACTIONS(),
        "/api/console/share-management/public": PUBLIC_SHARE({
          snapshot: {
            overall: "all_failed",
            reason: "all_entries_failed",
            entries: [PUBLIC_ENTRY("failed", "timeout", "203.0.113.10")],
            local: { maintenance: "none" },
          },
        }),
      },
      async drive(page) {
        await page.waitForFunction(
          () => {
            const button = document.querySelector(
              '[data-wb-share-public="retry"]'
            );
            return button && !button.disabled;
          },
          null,
          { timeout: 10000 }
        );
        const text = await page
          .locator("[data-wb-share-public-card]")
          .innerText();
        if (text.includes("维护租约为"))
          throw Error("维护 none 是无租约正常态，不得禁用受限重试");
      },
      shots: ["section:.wb-share-public"],
    },
    "share-public-in-flight": {
      lane: 6,
      shareTab: "diagnostics",
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/actions": PUBLIC_SHARE_ACTIONS(),
        "/api/console/share-management/public": PUBLIC_SHARE(),
      },
      ops: { "share.public_check": { final: "running" } },
      async drive(page) {
        await page
          .locator('[data-wb-share-public-card] [data-wb-share-public="check"]')
          .click();
        await page.waitForFunction(
          () => {
            const check = document.querySelector(
              '[data-wb-share-public-card] [data-wb-share-public="check"]'
            );
            const retry = document.querySelector(
              '[data-wb-share-public-card] [data-wb-share-public="retry"]'
            );
            return check && retry && check.disabled && retry.disabled;
          },
          null,
          { timeout: 10000 }
        );
        const text = await page
          .locator("[data-wb-share-public-card]")
          .innerText();
        if (!text.includes("任一分享动作在途"))
          throw Error("动作在途时必须禁用并写明原因");
      },
      shots: ["section:.wb-share-public"],
    },
    "share-repair-success": {
      lane: 6,
      shareTab: "diagnostics",
      shareFolds: ["layers-maintenance"],
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/actions": {
          status: 200,
          body: {
            actions: [
              ...fixtures["/api/console/actions"].body.actions,
              {
                action: "share.repair_limits",
                enabled: true,
                cancelable: false,
                param_schema: {},
                description: "fixture repair",
              },
            ],
          },
        },
      },
      ops: {
        "share.repair_limits": {
          final: "succeeded",
          result: {
            repaired: true,
            public_closed: true,
            gateway_stopped: true,
            checked_at: NOW,
            security_gate: SHARE_DIAG_LAYER("ok", "confirmed", false, {
              evidence: { verdict: true, detail: "ok" },
            }),
          },
        },
      },
      async drive(page) {
        const button = page.locator(
          '[data-wb-share-action="share.repair_limits"]'
        );
        await button.click();
        if (!(await button.textContent()).includes("保持关闭"))
          throw Error("Repair must explain its stop effect before submit");
        await button.click();
        await page
          .locator(".wb-share-layer-list li")
          .filter({ hasText: "安全条件" })
          .filter({ hasText: "正常" })
          .first()
          .waitFor();
        await page
          .locator(".wb-share-layer-list li")
          .filter({ hasText: "Funnel 映射" })
          .filter({ hasText: "关闭" })
          .first()
          .waitFor();
        const text = await page.locator(".wb-share-layers").innerText();
        if (
          text.includes("维护视图已读取") ||
          text.includes("没有最近一次故障")
        )
          throw Error("Partial repair evidence cannot invent unrelated checks");
      },
      shots: ["section:.wb-share-layers"],
      probe: () => document.querySelector(".wb-share-layers")?.textContent,
    },
    "share-repair-failed": {
      lane: 6,
      shareTab: "diagnostics",
      shareFolds: ["layers-maintenance"],
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/actions": {
          status: 200,
          body: {
            actions: [
              ...fixtures["/api/console/actions"].body.actions,
              {
                action: "share.repair_limits",
                enabled: true,
                cancelable: false,
                param_schema: {},
                description: "fixture repair",
              },
            ],
          },
        },
      },
      ops: {
        "share.repair_limits": {
          final: "failed",
          error: {
            code: "share_repair_failed",
            message: "规则重建失败，公网保持关闭（夹具）",
          },
        },
      },
      async drive(page) {
        const button = page.locator(
          '[data-wb-share-action="share.repair_limits"]'
        );
        await button.click();
        await button.click();
        await page
          .getByText("规则重建失败，公网保持关闭（夹具）", { exact: false })
          .first()
          .waitFor();
        if (
          (
            await page
              .locator(".wb-share-layer-list li")
              .filter({ hasText: "安全条件" })
              .first()
              .textContent()
          ).includes("正常")
        )
          throw Error("A failed repair cannot invent a pass");
      },
      shots: ["section:.wb-share-layers"],
    },
    "share-repair-unavailable": {
      lane: 6,
      shareTab: "diagnostics",
      shareFolds: ["layers-maintenance"],
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      async drive(page) {
        if (
          !(await page
            .locator('[data-wb-share-action="share.repair_limits"]')
            .isDisabled())
        )
          throw Error("Unregistered repair must be disabled");
      },
      shots: ["section:.wb-share-layers"],
    },
    // R3 RMB readings: the captured share-usage fixture predates the CNY fields
    // (the legacy scenario below keeps that response and its 人民币读数未提供
    // wording). These scenarios add the fields the backend now returns: exact
    // amounts, lower bounds (costCnyPartial=true with amounts) and marked-but-
    // unknown (costCnyPartial=true with no amount).
    "share-usage-rmb": {
      lane: 6,
      shareTab: "usage",
      shareFolds: ["usage-trend"],
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/share-usage": usageWithCny("full"),
      },
      async drive(page) {
        await page.locator(".wb-share-usage-table tbody tr").first().waitFor();
        const read = await page.evaluate(usageReading);
        if (read.rows !== 6)
          throw Error(
            `The usage table must keep every key and the unattributed row (got ${read.rows})`
          );
        if (!read.lastRow?.includes("未归属"))
          throw Error("The unattributed bucket must stay its own row");
        if (read.lastCells.length !== 4)
          throw Error(
            `The summary row must be read without the drill cell (got ${read.lastCells.length} cells)`
          );
        if (
          !read.lastRow.includes("¥") ||
          read.lastRow.includes("人民币读数未提供")
        )
          throw Error(
            `Exact CNY amounts must render in the unattributed summary row: ${read.lastRow}`
          );
        if (
          !read.lastCells.some(
            (cell) => cell.includes("已知人民币") && cell.includes("¥")
          )
        )
          throw Error("The known CNY column must render an exact amount");
        if (!read.lastDetail?.includes("无日期"))
          throw Error(
            `The drill must keep the undated bucket: ${read.lastDetail}`
          );
        if (!read.lastDetail?.includes("¥"))
          throw Error(
            `The undated bucket must render its exact CNY reading: ${read.lastDetail}`
          );
        if (read.detailOpen !== false || read.detailBodyDisplay !== "none")
          throw Error(
            `A closed usage detail must not paint its rows (open=${read.detailOpen}, display=${read.detailBodyDisplay})`
          );
        if (!read.trend.some((label) => label.includes("¥")))
          throw Error(
            `The 14-day trend must render CNY amounts: ${JSON.stringify(read.trend.slice(0, 3))}`
          );
        if (read.trend.some((label) => label.includes("人民币读数未提供")))
          throw Error("An exact CNY reading must not say 人民币读数未提供");
        const fold = page
          .locator(".wb-share-usage-table tbody tr")
          .last()
          .locator("details[data-wb-share-key-details] > summary");
        await fold.click();
        await page.waitForFunction(
          () => {
            const host = document.querySelector(
              ".wb-share-usage-table tbody tr:last-child details[data-wb-share-key-details]"
            );
            const body = host?.querySelector(".wb-share-key-detail-body");
            return (
              host?.open === true &&
              body &&
              getComputedStyle(body).display !== "none"
            );
          },
          null,
          { timeout: 10000 }
        );
        await fold.click();
        await page.waitForFunction(
          () => {
            const host = document.querySelector(
              ".wb-share-usage-table tbody tr:last-child details[data-wb-share-key-details]"
            );
            const body = host?.querySelector(".wb-share-key-detail-body");
            return (
              host?.open === false &&
              body &&
              getComputedStyle(body).display === "none"
            );
          },
          null,
          { timeout: 10000 }
        );
      },
      shots: ["section:.wb-share-usage-panel"],
      probe: usageReading,
      validateProbe: (probe) => [
        ...(probe?.rows !== 6
          ? [
              `The usage table must keep every key and the unattributed row (got ${probe?.rows})`,
            ]
          : []),
        ...(!probe?.lastRow?.includes("未归属")
          ? ["The unattributed bucket must stay its own row"]
          : []),
        ...(!probe?.lastRow?.includes("¥")
          ? ["The unattributed row must render the CNY reading"]
          : []),
      ],
    },
    "share-usage-rmb-partial": {
      lane: 6,
      shareTab: "usage",
      shareFolds: ["usage-trend"],
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/share-usage": usageWithCny("partial"),
      },
      async drive(page) {
        await page.locator(".wb-share-usage-table tbody tr").first().waitFor();
        const read = await page.evaluate(usageReading);
        if (read.rows !== 6)
          throw Error(
            `The usage table must keep every key and the unattributed row (got ${read.rows})`
          );
        if (!read.lastRow?.includes("未归属"))
          throw Error("The unattributed bucket must stay its own row");
        if (!read.lastRow.includes("≥ ¥"))
          throw Error(
            `A partial CNY reading must be a lower bound: ${read.lastRow}`
          );
        if (
          !read.lastDetail?.includes("无日期") ||
          !read.lastDetail.includes("≥ ¥")
        )
          throw Error(
            `The undated bucket must keep its lower-bound CNY reading: ${read.lastDetail}`
          );
        if (!read.trend.some((label) => label.includes("≥¥")))
          throw Error(
            `The trend must mark lower bounds: ${JSON.stringify(read.trend.slice(0, 3))}`
          );
        if (read.trend.some((label) => label.includes("人民币读数未提供")))
          throw Error("A partial reading is not a missing CNY reading");
      },
      shots: ["section:.wb-share-usage-panel"],
      probe: usageReading,
      validateProbe: (probe) => [
        ...(probe?.rows !== 6
          ? [
              `The usage table must keep every key and the unattributed row (got ${probe?.rows})`,
            ]
          : []),
        ...(!probe?.lastRow?.includes("≥ ¥")
          ? ["The unattributed row must mark its lower-bound CNY reading"]
          : []),
        ...(!probe?.trend?.some((label) => label.includes("≥¥"))
          ? ["The trend must mark lower-bound days"]
          : []),
      ],
    },
    "share-usage-rmb-unknown": {
      lane: 6,
      shareTab: "usage",
      shareFolds: ["usage-trend"],
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
        "/api/console/share-usage": usageWithCny("unknown"),
      },
      async drive(page) {
        await page.locator(".wb-share-usage-table tbody tr").first().waitFor();
        const read = await page.evaluate(usageReading);
        if (read.rows !== 6)
          throw Error(
            `The usage table must keep every key and the unattributed row (got ${read.rows})`
          );
        if (!read.lastRow?.includes("未归属"))
          throw Error("The unattributed bucket must stay its own row");
        if (!read.lastRow.includes("未知"))
          throw Error(
            `A marked-but-missing CNY reading must say 未知: ${read.lastRow}`
          );
        if (read.lastRow.includes("¥"))
          throw Error(
            `A missing CNY reading must not be folded to ¥0: ${read.lastRow}`
          );
        if (read.lastRow.includes("人民币读数未提供"))
          throw Error(
            "A marked-but-missing CNY reading is not the legacy 人民币读数未提供 case"
          );
        if (
          !read.lastDetail?.includes("无日期") ||
          read.lastDetail.includes("¥")
        )
          throw Error(
            `The undated bucket must stay unknown without ¥: ${read.lastDetail}`
          );
        if (!read.trend.some((label) => label.includes("未知")))
          throw Error(
            `The trend must mark unknown days: ${JSON.stringify(read.trend.slice(0, 3))}`
          );
        if (read.trend.some((label) => label.includes("¥")))
          throw Error("A missing CNY trend day must not render an amount");
      },
      shots: ["section:.wb-share-usage-panel"],
      probe: usageReading,
      validateProbe: (probe) => [
        ...(probe?.rows !== 6
          ? [
              `The usage table must keep every key and the unattributed row (got ${probe?.rows})`,
            ]
          : []),
        ...(!probe?.lastRow?.includes("未知")
          ? ["The unattributed row must say 未知"]
          : []),
        ...(probe?.lastRow?.includes("¥")
          ? ["A missing CNY reading must not render ¥"]
          : []),
      ],
    },
    "share-usage-legacy": {
      lane: 6,
      shareTab: "usage",
      shareFolds: ["usage-trend"],
      reads: {
        "/api/console/share-management/status": SHARE_STATUS,
        "/api/console/share-management/keys": SHARE_KEYS,
      },
      async drive(page) {
        await page.locator(".wb-share-usage-table tbody tr").first().waitFor();
        const read = await page.evaluate(usageReading);
        if (read.rows !== 6)
          throw Error(
            `The usage table must keep every key and the unattributed row (got ${read.rows})`
          );
        if (!read.lastRow?.includes("未归属"))
          throw Error("The unattributed bucket must stay its own row");
        if (!read.lastRow.includes("人民币读数未提供"))
          throw Error(
            `A legacy response without CNY fields must say 人民币读数未提供: ${read.lastRow}`
          );
        if (!read.trend.some((label) => label.includes("人民币读数未提供")))
          throw Error(
            "The trend must mark days the legacy response did not price in CNY"
          );
      },
      shots: ["section:.wb-share-usage-panel"],
      probe: usageReading,
      validateProbe: (probe) => [
        ...(probe?.rows !== 6
          ? [
              `The usage table must keep every key and the unattributed row (got ${probe?.rows})`,
            ]
          : []),
        ...(!probe?.lastRow?.includes("人民币读数未提供")
          ? ["A legacy response without CNY fields must say 人民币读数未提供"]
          : []),
        ...(!probe?.trend?.some((label) => label.includes("人民币读数未提供"))
          ? ["The trend must keep the legacy CNY wording"]
          : []),
      ],
    },
    "ops-failures": {
      lane: 2,
      reads: { "/api/console/operations": OPS_LIST },
      shots: ["first", "full"],
    },
  };

  names = names ?? Object.keys(SCENARIOS);
  const browser = await chromium.launch({ channel: "chrome" });
  const report = [];
  const unanswered = [];

  try {
    for (const name of names) {
      const sc = SCENARIOS[name];
      for (const theme of themes) {
        const ctx = await browser.newContext({
          viewport: sc.viewport ?? viewport,
          colorScheme: theme,
          serviceWorkers: "block",
          locale: "zh-CN",
          timezoneId: "Asia/Shanghai",
        });
        await ctx.addInitScript((t) => {
          try {
            localStorage.setItem(
              "gateway-console-appearance-v1",
              JSON.stringify({
                theme: t,
                backdrop: "vivid",
                rail: "expanded",
                motion: "system",
              })
            );
          } catch {}
        }, theme);
        const posts = [];
        const ops = new Map();
        let seq = 0;
        await ctx.route("**/*", async (route) => {
          const req = route.request();
          const u = new URL(req.url());
          const method = req.method();
          if (u.origin !== ORIGIN) {
            unanswered.push(`${name}/${theme} foreign ${method} ${req.url()}`);
            return route.abort();
          }
          if (u.pathname.startsWith("/gw/")) {
            const key = u.pathname.replace(/^\/gw\/usagesrv/, "") + u.search;
            const path = key.split("?")[0];
            if (
              method === "POST" &&
              /\/share-management\/keys\/[^/]+\/test$/.test(path) &&
              !sc.keyProbe
            ) {
              unanswered.push(`${name}/${theme} unexpected public test`);
              return route.fulfill({
                status: 403,
                contentType: "application/json",
                body: JSON.stringify({ error: "unexpected_public_test" }),
              });
            }
            if (
              method === "POST" &&
              sc.keyProbe &&
              path === "/api/console/share-management/keys/fx-key-a/test"
            ) {
              const body = JSON.parse(req.postData() || "{}");
              if (
                body.expected_row_version !== 3 ||
                Object.keys(body).length !== 1
              )
                throw Error("Unexpected key probe payload");
              posts.push("key-test");
              return route.fulfill({
                status: 200,
                contentType: "application/json",
                body: JSON.stringify({
                  key_id: "fx-key-a",
                  row_version: 3,
                  connected: true,
                  reason: "ok",
                  tested_from: "public",
                  checked_at: Math.floor(Date.now() / 1000),
                  stage: "inference",
                  http_status: 200,
                  model: "fixture-model",
                  public_ip: "1.1.1.1",
                  public_url: SHARE_STATUS.body.share_url,
                  elapsed_ms: 42,
                  ...(typeof sc.keyProbe === "object" ? sc.keyProbe : {}),
                }),
              });
            }
            if (method === "POST" && path === "/api/console/operations") {
              let body = {};
              try {
                body = JSON.parse(req.postData() || "{}");
              } catch {}
              posts.push(body.action);
              const fx = sc.ops?.[body.action];
              if (!fx)
                return route.fulfill({
                  status: 403,
                  contentType: "application/json",
                  body: JSON.stringify({
                    error: "fixture_no_op",
                    reason: `夹具未定义 ${body.action}`,
                  }),
                });
              if (
                fx.expectedTarget !== undefined &&
                body.target !== fx.expectedTarget
              ) {
                unanswered.push(
                  `${name}/${theme} ${body.action} target=${JSON.stringify(body.target)} expected=${fx.expectedTarget}`
                );
                return route.fulfill({
                  status: 400,
                  contentType: "application/json",
                  body: JSON.stringify({
                    error: "invalid_target",
                    detail: "target must be 1-64 chars",
                  }),
                });
              }
              if (fx.reject)
                return route.fulfill({
                  status: fx.reject.status,
                  contentType: "application/json",
                  body: JSON.stringify(fx.reject.body),
                });
              const id = `fx-${++seq}`;
              ops.set(id, { ...fx, action: body.action, target: body.target });
              return route.fulfill({
                status: 202,
                contentType: "application/json",
                body: JSON.stringify({
                  operation: {
                    id,
                    action: body.action,
                    target: body.target,
                    state: "queued",
                    created_at: NOW,
                  },
                }),
              });
            }
            if (method !== "GET" && method !== "HEAD")
              return route.fulfill({
                status: 403,
                contentType: "application/json",
                body: JSON.stringify({ error: "fixture_read_only" }),
              });
            const opMatch = path.match(
              /^\/api\/console\/operations\/(fx-\d+)$/
            );
            if (opMatch && ops.has(opMatch[1])) {
              const o = ops.get(opMatch[1]);
              o.polls = (o.polls ?? 0) + 1;
              const state = o.final === "running" ? "running" : o.final;
              return route.fulfill({
                status: 200,
                contentType: "application/json",
                body: JSON.stringify({
                  operation: {
                    id: opMatch[1],
                    action: o.action,
                    target: o.target,
                    state,
                    created_at: NOW,
                    result: state === "succeeded" ? o.result : null,
                    error: o.error ?? null,
                  },
                }),
              });
            }
            const read =
              sc.reads?.[key] ??
              sc.reads?.[path] ??
              (path === "/api/console/share-management/public"
                ? PUBLIC_SHARE()
                : readFixture(key));
            if (!read) {
              unanswered.push(`${name}/${theme} ${method} ${key}`);
              return route.fulfill({
                status: 404,
                contentType: "application/json",
                body: JSON.stringify({ error: "fixture_missing" }),
              });
            }
            return route.fulfill({
              status: read.status,
              contentType: "application/json",
              body: JSON.stringify(read.body),
            });
          }
          let file =
            u.pathname === "/" ? "/index.html" : decodeURIComponent(u.pathname);
          const abs = resolve(join(BUILD, file));
          if (
            !abs.startsWith(resolve(BUILD)) ||
            !existsSync(abs) ||
            !statSync(abs).isFile()
          ) {
            unanswered.push(`${name}/${theme} static ${method} ${u.pathname}`);
            return route.fulfill({ status: 404, body: "" });
          }
          return route.fulfill({
            status: 200,
            contentType:
              TYPES[extname(abs).toLowerCase()] || "application/octet-stream",
            body: readFileSync(abs),
          });
        });
        try {
          const page = await ctx.newPage();
          if (sc.clock) await page.clock.install();
          const errors = [];
          page.on("pageerror", (e) =>
            errors.push(String(e.message).slice(0, 160))
          );
          // The local candidate opens the UM console by default; these states belong
          // to the workbench (an older candidate ignores the query).
          await page.goto(`${ORIGIN}/?console=workbench`, {
            waitUntil: "load",
          });
          await page.waitForSelector(".workbench .wb-content", {
            timeout: 20000,
          });
          await page.waitForTimeout(3500);
          if (sc.lane !== undefined) {
            await navigateTo(page, sc.lane);
            await page.waitForTimeout(2500);
          }
          // R3: every share scenario enters its target tab with a real click before
          // interacting, and expands only the folds that hold the controls it reads.
          // The compact scenarios separately verify the default keys tab.
          if (sc.lane === 6) {
            await openShareTab(page, sc.shareTab ?? "keys");
            for (const id of sc.shareFolds ?? []) await openShareFold(page, id);
          }
          const marks = [];
          const t0 = Date.now();
          if (sc.drive)
            await sc.drive(page, (m) =>
              marks.push(`${((Date.now() - t0) / 1000).toFixed(1)}s ${m}`)
            );
          if (sc.keyProbe && posts.filter((p) => p === "key-test").length !== 1)
            errors.push(
              "Public test must run exactly once, after the explicit click"
            );
          const probe = sc.probe ? await page.evaluate(sc.probe) : null;
          if (sc.validateProbe) errors.push(...sc.validateProbe(probe, posts));
          const overflow = await page.evaluate(() => ({
            doc: document.documentElement.scrollWidth > innerWidth + 1,
            main: (() => {
              const m = document.querySelector(".wb-main");
              return m ? m.scrollWidth > m.clientWidth + 1 : null;
            })(),
          }));
          // Any rendered control that fell back to the browser's own button/field look.
          const unstyled = await page.evaluate(() =>
            [
              ...document.querySelectorAll(
                ".workbench :is(button, select, input:not([type=hidden]), textarea)"
              ),
            ]
              .filter((el) => {
                const cs = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                if (!r.width || !r.height || cs.visibility !== "visible")
                  return false;
                return (
                  /outset|inset/.test(cs.borderTopStyle) ||
                  (el.tagName === "BUTTON" &&
                    /^rgb\((240, 240, 240|239, 239, 239|107, 107, 107)\)$/.test(
                      cs.backgroundColor
                    ) &&
                    cs.borderTopLeftRadius === "0px")
                );
              })
              .map(
                (el) =>
                  `${el.className || el.tagName}「${(el.getAttribute("aria-label") || el.textContent).trim().slice(0, 16)}」`
              )
              .slice(0, 6)
          );
          for (const shot of sc.shots ?? ["first"]) {
            const s =
              typeof shot === "string" ? shot : (shot.selector ?? "first");
            if (typeof shot === "object" && shot.tab)
              await openShareTab(page, shot.tab);
            const file = `${SHOTS}/${name}-${theme}-${s.replace(/[^a-z0-9]+/gi, "_")}.png`;
            if (s === "first") await page.screenshot({ path: file });
            else if (s === "full") {
              const unroll = await page.addStyleTag({
                content:
                  "#viewport,#stage{height:auto!important;min-height:0!important;overflow:visible!important}.workbench{position:relative!important;inset:auto!important}.wb-main{overflow:visible!important}.wb-top{position:static!important}",
              });
              await page.screenshot({ path: file, fullPage: true });
              await unroll.evaluate((el) => el.remove());
            } else if (s.startsWith("section:")) {
              const loc = page.locator(s.slice(8)).first();
              await loc.scrollIntoViewIfNeeded();
              await loc.screenshot({ path: file });
            }
          }
          report.push({
            scenario: name,
            theme,
            unstyled,
            posts,
            polls: [...ops].map(
              ([id, o]) => `${id} ${o.action} polls=${o.polls ?? 0}`
            ),
            probe,
            overflow,
            errors,
            marks,
          });
        } finally {
          await ctx.close();
        }
      }
    }
  } finally {
    await browser.close();
  }
  const ok =
    unanswered.length === 0 &&
    report.every(
      (r) =>
        !r.errors.length &&
        !r.overflow.doc &&
        !r.overflow.main &&
        !r.unstyled.length
    );
  return { ok, unanswered, report };
}
