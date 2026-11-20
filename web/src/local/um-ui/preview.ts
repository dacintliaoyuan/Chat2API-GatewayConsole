import type {
  Capability,
  HistoryFilter,
  Load,
  LogQuery,
  MaintenanceAction,
  ModelTestState,
  Reading,
  RequestQuery,
  RequestResult,
  TrendRange,
  UmBucket,
  UmEvent,
  UmIssue,
  UmLoginRound,
  UmLogLine,
  UmMaintenanceRow,
  UmModelRow,
  UmRequestRow,
  UmTestDetail,
  UmTestRecord,
  UmTestRun,
  UmTrend,
  UmViewState,
} from "./contract";
import { dayKey } from "./format";

/** Offline preview for candidate builds (handoff r3): synthetic UM data and a
 *  driver that answers the console's events inside the browser. It never
 *  sends a request. local-console loads it only in a candidate build opened
 *  with `?um-preview=<scenario>`, and the page labels it as synthetic.
 *  Scenarios cover the honesty cases: exact zero, unreported,
 *  stale, estimated, failed / refused reads and partial test results. */

export const SCENARIOS = [
  { id: "normal", label: "正常" },
  { id: "attention", label: "需处理" },
  { id: "down", label: "服务停止" },
  { id: "zero", label: "零用量" },
] as const;
export type ScenarioId = (typeof SCENARIOS)[number]["id"];
export function isScenario(id: string | null): id is ScenarioId {
  return SCENARIOS.some((s) => s.id === id);
}

const DAY = 86_400_000;
const MIN = 60_000;
const exact = (value: number): Reading => ({ kind: "exact", value });
const est = (value: number): Reading => ({ kind: "estimate", value });
const missing = (note: string): Reading => ({ kind: "missing", note });
const open: Capability = { enabled: true };
const closed = (reason: string): Capability => ({ enabled: false, reason });

/** mulberry32: the same scenario always draws the same numbers. */
function random(seed: number): () => number {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

type Spec = {
  id: string;
  weight: number;
  speed: number;
  declared: number | null;
  compat: number | null;
  think: string[];
  thinkSource: "catalog" | "local" | "unknown";
  thinkDefault: string | null;
  steps: (true | string)[];
};
/** Synthetic models. `steps`: the outcome of each context step (40K, 80K,
 *  160K, 320K, 640K characters): true passes, a string stops with that reason. */
const SPECS: Spec[] = [
  {
    id: "claude-sonnet-4-6",
    weight: 1,
    speed: 46,
    declared: 200_000,
    compat: null,
    think: ["low", "medium", "high"],
    thinkSource: "catalog",
    thinkDefault: "medium",
    steps: [true, true, true, true, "64万 字符被拒：超出上下文（HTTP 400）"],
  },
  {
    id: "gpt-5.2-codex",
    weight: 0.55,
    speed: 71,
    declared: 400_000,
    compat: null,
    think: ["minimal", "low", "medium", "high"],
    thinkSource: "catalog",
    thinkDefault: "medium",
    steps: [true, true, true, "32万 字符超时（超时不构成上限）"],
  },
  {
    id: "gemini-3-pro-preview",
    weight: 0.22,
    speed: 88,
    declared: 1_048_576,
    compat: null,
    think: ["low", "high"],
    thinkSource: "local",
    thinkDefault: "high",
    steps: [true, true, true, true, true],
  },
  {
    id: "deepseek-v3.2",
    weight: 0.09,
    speed: 31,
    declared: null,
    compat: 128_000,
    think: [],
    thinkSource: "unknown",
    thinkDefault: null,
    steps: [true, true, "16万 字符时上游限流（HTTP 429，不构成上限）"],
  },
  {
    id: "um-extended-context-model-with-a-very-long-identifier-2026-10-15",
    weight: 0.04,
    speed: 52,
    declared: 256_000,
    compat: null,
    think: ["standard", "extended"],
    thinkSource: "catalog",
    thinkDefault: "standard",
    steps: [true, true, true, true, true],
  },
];
const CHAR_STEPS = [40_000, 80_000, 160_000, 320_000, 640_000];

type Raw = {
  input: number;
  output: number;
  cache: number;
  reasoning: number;
  requests: number;
  failed: number;
  speed: number;
};

export function startPreview(
  scenario: ScenarioId,
  publish: (state: UmViewState) => void
): (event: UmEvent) => void {
  const now = Date.now();
  const rand = random(scenario.length * 7919 + 17);
  const zero = scenario === "zero";
  const days = Array.from({ length: 30 }, (_, i) =>
    dayKey(now - (29 - i) * DAY)
  );
  const today = days[29];
  const dayFraction = Math.max(
    0.12,
    (now - Date.parse(`${today}T00:00:00+08:00`)) / DAY
  );

  // ------------------------------------------------------------ the ledger
  const raw: Raw[][] = SPECS.map((spec, m) =>
    days.map((key, i) => {
      const weekday = new Date(`${key}T12:00:00+08:00`).getUTCDay();
      const quiet = weekday === 0 || weekday === 6 ? 0.45 : 1;
      const active = !zero && rand() > 0.08 + m * 0.14;
      const tokens = active
        ? Math.round(
            2_600_000 *
              spec.weight *
              quiet *
              (0.45 + rand() * 1.1) *
              (i === 29 ? dayFraction : 1)
          )
        : 0;
      const input = Math.round(tokens * 0.86),
        output = tokens - input;
      const cache = Math.round(input * 0.62),
        reasoning = Math.round(output * 0.34);
      const requests = tokens
        ? Math.max(1, Math.round(tokens / (14_000 + rand() * 9_000)))
        : 0;
      const failed =
        requests && rand() < 0.3 ? Math.round(requests * rand() * 0.06) : 0;
      return {
        input,
        output,
        cache,
        reasoning,
        requests,
        failed,
        speed: spec.speed * (0.8 + rand() * 0.4),
      };
    })
  );
  // Readings per scenario: 「需处理」 has one unparsable day; 「服务停止」 has
  // no usable readings.
  const bucket = (m: number, i: number): UmBucket => {
    const r = raw[m][i];
    if (scenario === "attention" && m === 1 && i === 25) {
      return {
        tokens: { kind: "unknown", note: "这天的账本行无法解析" },
        requests: { kind: "atLeast", value: r.requests },
        speed: { kind: "unknown" },
      };
    }
    return {
      tokens: exact(r.input + r.output),
      requests: exact(r.requests),
      speed: r.requests ? exact(r.speed) : missing("当天没有请求"),
    };
  };
  const sum = (list: Reading[]): Reading => {
    let total = 0,
      lower = false,
      approx = false,
      any = false;
    for (const r of list) {
      if (r.kind === "exact" || r.kind === "estimate" || r.kind === "atLeast") {
        total += r.value;
        any = true;
        approx ||= r.kind === "estimate";
        lower ||= r.kind === "atLeast";
      } else lower = true;
    }
    if (!any)
      return list.find((r) => r.kind === "unknown") ?? list[0] ?? exact(0);
    return lower
      ? { kind: "atLeast", value: total }
      : approx
        ? est(total)
        : exact(total);
  };
  const weightedSpeed = (
    pairs: { speed: Reading; requests: Reading }[]
  ): Reading => {
    let w = 0,
      s = 0;
    for (const p of pairs)
      if (
        p.speed.kind === "exact" &&
        p.requests.kind === "exact" &&
        p.requests.value > 0
      ) {
        w += p.requests.value;
        s += p.speed.value * p.requests.value;
      }
    return w ? exact(s / w) : missing("没有可计时的请求");
  };
  const merge = (list: UmBucket[]): UmBucket => ({
    tokens: sum(list.map((b) => b.tokens)),
    requests: sum(list.map((b) => b.requests)),
    speed: weightedSpeed(list),
  });
  const trendFor = (range: TrendRange): UmTrend => {
    const n = range === "7d" ? 7 : range === "14d" ? 14 : 30;
    const window = days.slice(30 - n);
    const models = SPECS.map((spec, m) => {
      const daily = window.map((_, k) => bucket(m, 30 - n + k));
      const failed = raw[m].slice(30 - n).reduce((a, r) => a + r.failed, 0);
      return {
        model: spec.id,
        daily,
        total: merge(daily),
        failed: exact(failed),
        weight: raw[m]
          .slice(30 - n)
          .reduce((a, r) => a + r.input + r.output, 0),
      };
    })
      .filter((x) => x.weight > 0)
      .sort((a, b) => b.weight - a.weight)
      .map(({ weight: _w, ...rest }) => rest);
    const allDaily = window.map((_, k) =>
      merge(SPECS.map((_, m) => bucket(m, 30 - n + k)))
    );
    return {
      range,
      days: window,
      all: { daily: allDaily, total: merge(allDaily) },
      models,
      speedBasis: "whole_request",
    };
  };

  // ------------------------------------------------------------ requests, logs, maintenance
  const RESULT_NOTE: Record<RequestResult, string> = {
    success: "HTTP 200，正常完成",
    failed: "HTTP 529，上游过载；有限重试后仍失败",
    truncated: "HTTP 200，但回复在完成前截断，按失败计",
    client_disconnected: "客户端在回复完成前断开连接；不推断为主动取消",
    cancelled: "收到明确的取消请求后停止",
    unknown: "账本没有记录完成状态",
  };
  const rows: UmRequestRow[] = [];
  const logs: UmLogLine[] = [];
  if (!zero) {
    let at = now - 3 * MIN;
    for (let k = 0; k < 96; k++) {
      const m =
        rand() < 0.55
          ? 0
          : rand() < 0.6
            ? 1
            : rand() < 0.6
              ? 2
              : rand() < 0.7
                ? 3
                : 4;
      const spec = SPECS[m];
      const roll = rand();
      const result: RequestResult =
        scenario === "attention"
          ? roll < 0.08
            ? "truncated"
            : roll < 0.13
              ? "failed"
              : roll < 0.16
                ? "client_disconnected"
                : roll < 0.18
                  ? "cancelled"
                  : roll < 0.2
                    ? "unknown"
                    : "success"
          : roll < 0.03
            ? "failed"
            : roll < 0.05
              ? "client_disconnected"
              : "success";
      const input = Math.round(300 + rand() * 12_000),
        output = result === "failed" ? 0 : Math.round(80 + rand() * 2_600);
      const durationMs = Math.round(
        (output / spec.speed) * 1000 + 800 + rand() * 4_000
      );
      const requestId = (scenario === "attention" ? k % 5 === 3 : k % 11 === 7)
        ? null
        : `req_${Math.floor(rand() * 0xffffffffff)
            .toString(16)
            .padStart(10, "0")}`;
      const cacheKnown = !(scenario === "attention" && k % 4 === 1);
      rows.push({
        key: `row-${k}`,
        requestId,
        at,
        model: spec.id,
        result,
        resultNote: RESULT_NOTE[result],
        httpStatus:
          result === "failed"
            ? 529
            : result === "client_disconnected" || result === "unknown"
              ? null
              : 200,
        input: exact(input),
        output: exact(output),
        cacheRead: cacheKnown
          ? exact(Math.round(input * rand() * 0.8))
          : { kind: "unreported", note: "这条回复没有缓存字段" },
        reasoning:
          scenario === "attention" && m === 1
            ? { kind: "unreported", note: "上游没有报告思考 Token" }
            : exact(Math.round(output * 0.3)),
        durationMs: exact(durationMs),
        firstTokenMs:
          k % 3 === 0
            ? exact(Math.round(700 + rand() * 2_400))
            : missing("没有实测首字"),
        speed:
          result === "success"
            ? exact(output / (durationMs / 1000))
            : missing("没有完整回复，不计速度"),
        speedBasis: "whole_request",
        retries:
          result === "failed"
            ? [
                { at: at - 9_000, note: "HTTP 529，1.5 秒后重试" },
                { at: at - 4_000, note: "HTTP 529，3 秒后重试" },
              ]
            : scenario === "attention" && k % 7 === 2
              ? null
              : [],
        error:
          result === "failed"
            ? {
                code: "upstream_529",
                message: "上游过载，2 次有限重试后仍失败",
              }
            : result === "truncated"
              ? {
                  code: "stream_truncated",
                  message: "上游流在 finish_reason 之前中断",
                }
              : result === "client_disconnected"
                ? { code: "client_closed", message: "客户端提前关闭连接" }
                : null,
      });
      if (requestId) {
        logs.push({
          key: `log-${k}-a`,
          at: at - durationMs,
          level: "info",
          source: "chat2api",
          text: `→ ${spec.id} /v1/chat/completions（stream）input=${input}`,
          requestId,
        });
        logs.push(
          result === "truncated"
            ? {
                key: `log-${k}-b`,
                at,
                level: "error",
                source: "chat2api",
                text: "上游流中断：没有收到 [DONE]，按截断失败记账（HTTP 200 不算成功）",
                requestId,
              }
            : result === "failed"
              ? {
                  key: `log-${k}-b`,
                  at,
                  level: "error",
                  source: "chat2api",
                  text: "上游返回 529（overloaded），有限重试 2/2 后放弃",
                  requestId,
                }
              : {
                  key: `log-${k}-b`,
                  at,
                  level: "info",
                  source: "chat2api",
                  text: `← 完成 finish_reason=stop output=${output}`,
                  requestId,
                }
        );
      }
      if (k % 9 === 4)
        logs.push({
          key: `log-${k}-w`,
          at: at - 20_000,
          level: "warn",
          source: "um-auth",
          text: "会话令牌将在 30 分钟内过期，下次请求时刷新",
          requestId: null,
        });
      if (k % 13 === 6)
        logs.push({
          key: `log-${k}-d`,
          at: at - 40_000,
          level: "debug",
          source: "usage-writer",
          text: "账本写入 1 行（逻辑请求，重试不重复计）",
          requestId: null,
        });
      at -= Math.round((rand() * 140 + 12) * MIN);
    }
    logs.push({
      key: "log-tb",
      at: null,
      level: "error",
      source: "chat2api",
      text: 'Traceback (most recent call last):\n  File "chat2api.py", line 812, in stream_reply\nConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接',
      requestId: null,
    });
    logs.sort((a, b) => (b.at ?? 0) - (a.at ?? 0));
  }
  const loginRounds: UmLoginRound[] = zero
    ? []
    : [
        {
          key: "preview-run:8",
          runId: "preview-run",
          round: 8,
          state: "completed",
          triggerSource: "business_request",
          visible: false,
          startedAt: now - 6 * MIN,
          endedAt: now - 6 * MIN + 42_000,
          submitCount: 1,
          outcome: "restored",
          reason: null,
          upstreamStatus: 200,
          evidenceVerdict: "valid",
        },
        {
          key: "preview-run:7",
          runId: "preview-run",
          round: 7,
          state: "completed",
          triggerSource: "auth_watch",
          visible: false,
          startedAt: now - 34 * MIN,
          endedAt: now - 34 * MIN + 12_000,
          submitCount: 0,
          outcome: "verification_unknown",
          reason: "ConnectionError",
          upstreamStatus: null,
          evidenceVerdict: "unknown",
        },
      ];
  const maintenance: UmMaintenanceRow[] = zero
    ? []
    : [
        ...(scenario === "attention"
          ? [
              {
                key: "mt-v2",
                action: "verify" as const,
                state: "failed" as const,
                at: now - 25 * MIN,
                finishedAt: now - 25 * MIN + 2_300,
                steps: [
                  { at: now - 25 * MIN, note: "读取本机凭据" },
                  {
                    at: now - 25 * MIN + 900,
                    note: "向上游确认（不调用模型）",
                  },
                  {
                    at: now - 25 * MIN + 2_300,
                    note: "上游返回 401：登录已失效",
                  },
                ],
                result: "登录已失效，需要重新登录",
              },
            ]
          : []),
        ...(scenario === "down"
          ? [
              {
                key: "mt-stop",
                action: "stop" as const,
                state: "succeeded" as const,
                at: now - 42 * MIN,
                finishedAt: now - 42 * MIN + 3_100,
                steps: [
                  { at: now - 42 * MIN, note: "记录停用意图" },
                  { at: now - 42 * MIN + 1_200, note: "停止进程 PID 31544" },
                  { at: now - 42 * MIN + 3_100, note: "端口 8000 已释放" },
                ],
                result: "已停止；需要手动启动",
              },
            ]
          : []),
        {
          key: "mt-v1",
          action: "verify",
          state: "succeeded",
          at: now - 5 * 60 * MIN,
          finishedAt: now - 5 * 60 * MIN + 1_800,
          steps: [
            { at: now - 5 * 60 * MIN, note: "读取本机凭据" },
            { at: now - 5 * 60 * MIN + 1_800, note: "上游确认有效" },
          ],
          result: "登录有效",
        },
        {
          key: "mt-l1",
          action: "login",
          state: "succeeded",
          at: now - 26 * 60 * MIN,
          finishedAt: now - 26 * 60 * MIN + 95_000,
          steps: [
            { at: now - 26 * 60 * MIN, note: "核验：凭据被拒（401）" },
            { at: now - 26 * 60 * MIN + 2_000, note: "打开专用 Edge 登录窗口" },
            { at: now - 26 * 60 * MIN + 92_000, note: "检测到登录完成" },
            { at: now - 26 * 60 * MIN + 95_000, note: "核验：有效" },
          ],
          result: "已重新登录",
        },
        {
          key: "mt-r1",
          action: "restart",
          state: "interrupted",
          at: now - 50 * 60 * MIN,
          finishedAt: null,
          steps: [
            { at: now - 50 * 60 * MIN, note: "停止进程 PID 28810" },
            { at: now - 50 * 60 * MIN + 4_000, note: "页面在结果返回前关闭" },
          ],
          result: "结果未确认：以服务状态为准，本页不重发",
        },
        {
          key: "mt-s1",
          action: "start",
          state: "succeeded",
          at: now - 74 * 60 * MIN,
          finishedAt: now - 74 * 60 * MIN + 6_200,
          steps: [
            { at: now - 74 * 60 * MIN, note: "启动 chat2api" },
            { at: now - 74 * 60 * MIN + 6_200, note: "端口 8000 可连接" },
          ],
          result: "已启动",
        },
      ];

  // ------------------------------------------------------------ models
  const catalogRows: UmModelRow[] = SPECS.map((spec, m) => ({
    id: spec.id,
    name: spec.id,
    context: {
      declared:
        spec.declared !== null
          ? {
              tokens: spec.declared,
              source: m === 2 ? "本地已知模型表" : "UM /api/models",
            }
          : null,
      compatDefault: spec.compat,
      probedChars:
        !zero && m === 0
          ? { chars: 320_000, at: now - 3 * DAY }
          : !zero && m === 2
            ? { chars: 640_000, at: now - 3 * DAY }
            : null,
      tokenLowerBound:
        !zero && m === 0 ? { tokens: 96_214, at: now - 3 * DAY } : null,
    },
    thinking: {
      levels: spec.think,
      source: spec.thinkSource,
      defaultLevel: spec.thinkDefault,
    },
    lastContext: zero
      ? null
      : m === 0
        ? {
            at: now - 3 * DAY,
            ok: true,
            summary: "通过 32万 字符 · 上游报告 ≥96,214 Token",
          }
        : scenario === "attention" && m === 4
          ? {
              at: now - 2 * DAY,
              ok: true,
              summary: "通过 64万 字符 · 上游没有报告 Token",
            }
          : scenario === "attention" && m === 3
            ? {
                at: now - 2 * DAY,
                ok: false,
                summary: "通过 16万 字符 · 上游限流（HTTP 429，不构成上限）",
              }
            : null,
  }));
  const history: UmTestRecord[] = zero
    ? []
    : [
        {
          key: "h3",
          model: SPECS[0].id,
          at: now - 3 * DAY,
          result: "done",
          summary: "通过 32万 字符 · 上游报告 ≥96,214 Token",
          archived: false,
        },
        {
          key: "h4",
          model: SPECS[2].id,
          at: now - 3 * DAY,
          result: "done",
          summary: "通过 64万 字符 · 上游没有报告 Token",
          archived: false,
        },
      ];

  // ------------------------------------------------------------ the state
  const at = now - 40_000;
  const ready = <T>(data: T, when = at): Load<T> => ({
    state: "ready",
    data,
    at: when,
  });
  const t = raw.map((r) => r[29]);
  const todayReq = t.reduce((a, r) => a + r.requests, 0);
  const down = scenario === "down";
  const serviceUp = !down;
  const maintenanceCaps = (
    up: boolean
  ): Record<MaintenanceAction, Capability> => ({
    verify: up ? open : closed("UM 服务未运行，先启动"),
    login: up ? open : closed("UM 服务未运行，先启动"),
    start: up ? closed("UM 服务已在运行") : open,
    stop: up ? open : closed("UM 服务未运行"),
    restart: up ? open : closed("UM 服务未运行，请用「启动」"),
  });
  const modelCaps = (up: boolean, busy: boolean) => ({
    refresh: up ? open : closed("UM 服务未运行，读不到目录"),
    context: !up
      ? closed("UM 服务未运行")
      : busy
        ? closed("已有测试在进行，等它结束或停止后再发起")
        : open,
    stop: open,
  });
  const issues: UmIssue[] =
    scenario === "normal"
      ? [
          {
            kind: "request_failed",
            title: "请求失败",
            detail: "上游返回 529（过载），有限重试后仍失败",
            count: 2,
            lastAt: now - 95 * MIN,
            evidence: {
              view: "records",
              query: { result: "failed", window: "24h" },
            },
          },
        ]
      : scenario === "attention"
        ? [
            {
              kind: "login",
              title: "需要重新登录",
              detail: "最近一次核验：上游返回 401，登录已失效",
              count: 1,
              lastAt: now - 25 * MIN,
              evidence: { view: "maintenance" },
            },
            {
              kind: "truncated",
              title: "回复截断",
              detail: "HTTP 200 但回复没有完成，按失败计",
              count: 6,
              lastAt: now - 18 * MIN,
              evidence: {
                view: "records",
                query: { result: "truncated", window: "24h" },
              },
            },
            {
              kind: "request_failed",
              title: "请求失败",
              detail: "上游 529 过载，有限重试后仍失败",
              count: 4,
              lastAt: now - 64 * MIN,
              evidence: {
                view: "records",
                query: { result: "failed", window: "24h" },
              },
            },
            {
              kind: "catalog",
              title: "目录刷新失败",
              detail: "UM 返回了登录页，模型列表沿用旧目录",
              count: 1,
              lastAt: now - 12 * MIN,
              evidence: null,
            },
          ]
        : down
          ? [
              {
                kind: "service",
                title: "UM 服务未运行",
                detail: "8000 没有在监听；经 8000 的请求全部失败",
                count: 1,
                lastAt: now - 42 * MIN,
                evidence: { view: "maintenance" },
              },
              {
                kind: "request_failed",
                title: "请求失败",
                detail: "停止期间客户端连接被拒绝",
                count: 12,
                lastAt: now - 6 * MIN,
                evidence: {
                  view: "records",
                  query: { result: "failed", window: "today" },
                },
              },
            ]
          : [];

  const initialRun: UmTestRun | null =
    scenario === "attention"
      ? {
          id: "run-a1",
          startedAt: now - 70_000,
          state: "running",
          stopRequested: false,
          budget: { used: 12, limit: 20 },
          items: [
            {
              model: SPECS[0].id,
              state: "done",
              stage: null,
              summary:
                "通过 32万 字符；上游报告 ≥96,214 Token；64万 被拒（超出上下文）",
              detail: contextDetail(0, 4, 96_214, SPECS[0].steps[4] as string),
            },
            {
              model: SPECS[1].id,
              state: "partial",
              stage: null,
              summary: "通过 16万 字符；32万 字符超时（超时不构成上限）",
              detail: contextDetail(1, 3, null, SPECS[1].steps[3] as string),
            },
            {
              model: SPECS[2].id,
              state: "running",
              stage: "字符步进 16万…（本批第 12 次请求）",
              summary: null,
              detail: null,
            },
            {
              model: SPECS[3].id,
              state: "waiting",
              stage: null,
              summary: null,
              detail: null,
            },
          ],
          note: null,
        }
      : null;

  let state: UmViewState = {
    mode: "preview",
    overview: {
      service: ready({
        state: serviceUp ? "running" : "stopped",
        word: serviceUp ? "运行中" : "未运行",
        detail: serviceUp
          ? "chat2api 主链"
          : "停用意图已记录（42 分钟前手动停止）",
        port: 8000,
        pid: serviceUp ? 41872 : null,
        checkedAt: at,
      }),
      login: ready(
        scenario === "attention"
          ? {
              state: "needs_login",
              word: "需要重新登录",
              detail: "最近一次核验被上游拒绝（401）",
              checkedAt: now - 25 * MIN,
            }
          : down
            ? {
                state: "unknown",
                word: "无法检查",
                detail: "UM 服务未运行，查不到登录状态",
                checkedAt: null,
              }
            : {
                state: "valid",
                word: "有效",
                detail: "凭据存在且最近核验通过",
                checkedAt: at,
              }
      ),
      verify: ready(
        scenario === "attention"
          ? {
              result: "failed",
              word: "未通过",
              detail: "上游返回 401：登录已失效",
              at: now - 25 * MIN,
            }
          : zero
            ? null
            : {
                result: "passed",
                word: "通过",
                detail: "上游确认当前登录有效（未调用模型）",
                at: now - 5 * 60 * MIN,
              }
      ),
      upstream: ready(
        scenario === "attention"
          ? {
              active: {
                id: "fault-preview",
                classification: "upstream_5xx",
                upstreamStatus: 503,
                summary: "no healthy upstream",
                failures: 3,
                startedAt: now - 25 * MIN,
                lastAt: now - 2 * MIN,
                source: "background_probe",
              },
              history: [
                {
                  id: "fault-history",
                  classification: "timeout",
                  upstreamStatus: null,
                  summary: null,
                  failures: 2,
                  startedAt: now - 3 * 60 * MIN,
                  lastAt: now - 70 * MIN,
                  source: "business_request",
                  recoveredAt: now - 65 * MIN,
                  recoverySource: "active_probe",
                  recoveryStatus: 200,
                  recoveryDetail: "credential_probe",
                },
              ],
              lastCheck: {
                at: now - 2 * MIN,
                source: "background_probe",
                result: "upstream_5xx",
                upstreamStatus: 503,
              },
              nextCheckAt: now + 3 * MIN,
            }
          : {
              active: null,
              history: [],
              lastCheck: down
                ? null
                : {
                    at: now - 5 * MIN,
                    source: "background_probe",
                    result: "valid",
                    upstreamStatus: 200,
                  },
              nextCheckAt: down ? null : now + 5 * MIN,
            }
      ),
      today: down
        ? {
            state: "stale",
            data: todayOf(),
            at: now - 15 * MIN,
            failure: {
              kind: "no-answer",
              reason: "统计汇总没有在 8 秒内应答",
              at: now - 30_000,
            },
          }
        : ready(todayOf()),
      trend: ready(trendFor("7d")),
      issues: ready({ window: "近 24 小时", items: issues }),
      actions: maintenanceCaps(serviceUp),
      progress: {},
    },
    requests: {
      query: { window: "today", model: null, result: "all", search: "" },
      page: { state: "loading" },
      models: SPECS.map((s) => s.id),
      detail: null,
      logs: {
        query: { window: "today", levels: [], source: null },
        page: { state: "loading" },
      },
      loginRounds: {
        query: { round: "", q: "" },
        page: ready(
          {
            rounds: loginRounds,
            scope: "本机 8000 UM 登录轮次历史（最近 7 天 / 4 MiB）· 只读",
            truncated: false,
          },
          now
        ),
      },
      maintenance: down
        ? {
            state: "failed",
            failure: {
              kind: "refused",
              reason: "HTTP 403：本页无权读取维护记录",
              at: now - 20_000,
            },
          }
        : { state: "loading" },
    },
    models: {
      access: ready({
        baseUrl: "http://127.0.0.1:8000/v1",
        apiType: "OpenAI 兼容（/v1/chat/completions、/v1/models）",
        hints: [
          "在客户端（例如 Magpie）里把上面的地址填成 OpenAI 兼容接口的 Base URL，模型 ID 用下表的 ID。",
          "这里不显示也不收集任何密钥；是否需要鉴权以 8000 的实际配置为准。",
        ],
      }),
      catalog: down
        ? {
            state: "failed",
            failure: {
              kind: "no-answer",
              reason: "8000 没有应答，读不到 UM 目录",
              at: now - 20_000,
            },
          }
        : ready({
            models: catalogRows,
            fetchedAt: now - 2 * 60 * MIN,
            source: "UM /api/models（严格取新）",
          }),
      refresh: { state: "idle" },
      run: initialRun,
      history: {
        filter: { model: null },
        records: { state: "loading" },
      },
      runtime: ready([
        { label: "服务端口", value: "8000（chat2api 主链）" },
        { label: "上游请求超时", value: "600 秒" },
        { label: "自动重试", value: "最多 2 次，只在连接失败或 5xx 时" },
        {
          label: "上下文探测预算",
          value: "整批 20 次请求，每次输出最多 64 Token",
        },
        { label: "时区", value: "北京时间（UTC+8）自然日" },
      ]),
      actions: modelCaps(serviceUp, initialRun !== null),
      notice: null,
    },
  };

  function todayOf() {
    const r = (pick: (x: Raw) => number) => t.reduce((a, x) => a + pick(x), 0);
    const input = r((x) => x.input),
      output = r((x) => x.output),
      reqs = r((x) => x.requests);
    const speedAll = reqs
      ? t.reduce((a, x) => a + x.speed * x.requests, 0) / reqs
      : 0;
    return {
      day: today,
      tokens: {
        total: exact(input + output),
        input: exact(input),
        output: exact(output),
      },
      cacheRead:
        scenario === "attention"
          ? {
              tokens: {
                kind: "atLeast" as const,
                value: r((x) => x.cache),
                note: "部分回复没有缓存字段",
              },
              report: "partial" as const,
              detail: "今天有 9 条回复没有报告缓存字段",
            }
          : {
              tokens: exact(r((x) => x.cache)),
              report: "reported" as const,
              detail: "今天每条回复都带有缓存字段",
            },
      reasoning:
        scenario === "attention"
          ? {
              kind: "unreported" as const,
              note: "gpt-5.2-codex 的回复没有报告思考 Token",
            }
          : exact(r((x) => x.reasoning)),
      requests: { recorded: exact(reqs), failed: exact(r((x) => x.failed)) },
      speed: {
        value: reqs ? exact(speedAll) : missing("今天还没有可计时的请求"),
        basis: "whole_request" as const,
        samples: reqs,
      },
    };
  }

  // ------------------------------------------------------------ the driver
  const emit = () => {
    state = { ...state };
    publish(state);
  };
  const later = (ms: number, task: () => void) => window.setTimeout(task, ms);
  const PAGE = 20;
  const windowStart = (w: RequestQuery["window"]) =>
    w === "today"
      ? Date.parse(`${today}T00:00:00+08:00`)
      : now - (w === "24h" ? 1 : w === "7d" ? 7 : 30) * DAY;
  const scopeOf = (w: RequestQuery["window"]) =>
    ({
      today: "北京时间今天 00:00 起",
      "24h": "最近 24 小时",
      "7d": "最近 7 天",
      "30d": "最近 30 天",
    })[w] + "的 UM 账本（合成数据）";
  const requestPage = (
    q: RequestQuery,
    offset: number,
    keep: UmRequestRow[] = []
  ) => {
    const needle = q.search.toLowerCase();
    const hit = rows.filter(
      (r) =>
        r.at >= windowStart(q.window) &&
        (q.model === null || r.model === q.model) &&
        (q.result === "all" || r.result === q.result) &&
        (!needle ||
          [
            r.requestId ?? "",
            r.model,
            r.error?.message ?? "",
            r.error?.code ?? "",
          ].some((s) => s.toLowerCase().includes(needle)))
    );
    const slice = hit.slice(offset, offset + PAGE);
    return {
      rows: [...keep, ...slice],
      scope: scopeOf(q.window),
      earlier: offset + PAGE < hit.length ? String(offset + PAGE) : null,
    };
  };
  const logPage = (q: LogQuery, offset: number, keep: UmLogLine[] = []) => {
    const hit = logs.filter(
      (l) =>
        (l.at === null || l.at >= windowStart(q.window)) &&
        (!q.levels.length || q.levels.includes(l.level)) &&
        (q.source === null || l.source === q.source)
    );
    return {
      lines: [...keep, ...hit.slice(offset, offset + 30)],
      sources: [...new Set(logs.map((l) => l.source))],
      scope: `${scopeOf(q.window).replace("的 UM 账本（合成数据）", "")}的本机日志（合成数据）`,
      earlier: offset + 30 < hit.length ? String(offset + 30) : null,
    };
  };
  const maintenancePage = (offset: number, keep: UmMaintenanceRow[] = []) => ({
    rows: [...keep, ...maintenance.slice(offset, offset + 4)],
    scope: "最近 7 天的维护记录（合成数据）",
    earlier: offset + 4 < maintenance.length ? String(offset + 4) : null,
  });
  const historyOf = (f: HistoryFilter) =>
    history
      .filter((h) => f.model === null || h.model === f.model)
      .sort((a, b) => b.at - a.at);

  // Lazy first reads, like a controller would do on `page`.
  later(350, () => {
    state.requests.page = ready(
      requestPage(state.requests.query, 0),
      Date.now()
    );
    state.requests.logs.page = down
      ? {
          state: "failed",
          failure: {
            kind: "error",
            reason: "读取本机日志超时（8 秒）",
            at: Date.now(),
          },
        }
      : ready(logPage(state.requests.logs.query, 0), Date.now());
    if (!down)
      state.requests.maintenance = ready(maintenancePage(0), Date.now());
    state.models.history.records = ready(
      historyOf(state.models.history.filter),
      Date.now()
    );
    emit();
  });

  function finishAction(
    action: MaintenanceAction,
    result: "succeeded" | "failed",
    note: string,
    steps: string[]
  ): void {
    const start =
      state.overview.progress[action]?.state === "pending"
        ? (state.overview.progress[action] as { since: number }).since
        : Date.now();
    state.overview.progress = {
      ...state.overview.progress,
      [action]: { state: "done", result, at: Date.now(), note },
    };
    maintenance.unshift({
      key: `mt-${Date.now()}`,
      action,
      state: result,
      at: start,
      finishedAt: Date.now(),
      steps: steps.map((s, i) => ({ at: start + i * 600, note: s })),
      result: note,
    });
    if (state.requests.maintenance.state === "ready")
      state.requests.maintenance = ready(maintenancePage(0), Date.now());
    emit();
  }
  function begin(action: MaintenanceAction, note: string): void {
    state.overview.progress = {
      ...state.overview.progress,
      [action]: { state: "pending", since: Date.now(), note },
    };
    emit();
  }
  function setService(up: boolean): void {
    const o = state.overview;
    o.service = ready(
      {
        state: up ? "running" : "stopped",
        word: up ? "运行中" : "未运行",
        detail: up ? "chat2api 主链" : "停用意图已记录（刚刚手动停止）",
        port: 8000,
        pid: up ? 40000 + Math.floor(Math.random() * 9000) : null,
        checkedAt: Date.now(),
      },
      Date.now()
    );
    o.actions = maintenanceCaps(up);
    state.models.actions = modelCaps(
      up,
      state.models.run !== null && state.models.run.state !== "finished"
    );
  }

  function startRun(models: string[]): void {
    if (state.models.run && state.models.run.state !== "finished") {
      state.models.notice = {
        tone: "warn",
        text: "已有测试在进行，这次点击没有发起新测试",
        at: Date.now(),
      };
      emit();
      return;
    }
    const run: UmTestRun = {
      id: `run-${Date.now()}`,
      startedAt: Date.now(),
      state: "submitting",
      stopRequested: false,
      budget: { used: 0, limit: 20 },
      items: models.map((model) => ({
        model,
        state: "waiting" as ModelTestState,
        stage: null,
        summary: null,
        detail: null,
      })),
      note: null,
    };
    state.models.run = run;
    state.models.notice = null;
    state.models.actions = modelCaps(true, true);
    emit();
    later(450, () => {
      run.state = "running";
      emit();
      next(run, 0);
    });
  }
  function next(run: UmTestRun, i: number): void {
    if (i >= run.items.length) {
      finishRun(run);
      return;
    }
    const item = run.items[i];
    if (run.stopRequested) {
      for (const rest of run.items.slice(i))
        Object.assign(rest, {
          state: "not_run",
          summary: "已请求停止，没有开始",
        });
      finishRun(run);
      return;
    }
    if (run.budget && run.budget.used >= run.budget.limit) {
      for (const rest of run.items.slice(i))
        Object.assign(rest, {
          state: "not_run",
          summary: "整批请求预算已用完，没有开始",
        });
      finishRun(run);
      return;
    }
    const m = SPECS.findIndex((s) => s.id === item.model);
    item.state = "running";
    // Context: one character step per request, sharing the batch budget.
    const steps = SPECS[m].steps;
    let k = 0;
    const tick = () => {
      if (run.budget!.used >= run.budget!.limit) {
        Object.assign(item, {
          state: k > 0 ? "partial" : "not_run",
          stage: null,
          summary:
            k > 0
              ? `预算用完：通过 ${CHAR_STEPS[k - 1] / 10_000}万 字符后停止`
              : "整批请求预算已用完，没有开始",
          detail:
            k > 0 ? contextDetail(m, k, null, "整批请求预算已用完") : null,
        });
        emit();
        later(250, () => next(run, i + 1));
        return;
      }
      run.budget!.used += 1;
      item.stage = `字符步进 ${CHAR_STEPS[k] / 10_000}万…（本批第 ${run.budget!.used} 次请求）`;
      emit();
      later(520, () => {
        const outcome = steps[k];
        if (outcome === true && k + 1 < steps.length && !run.stopRequested) {
          k += 1;
          tick();
          return;
        }
        const passed = outcome === true ? k + 1 : k;
        const reason =
          outcome === true
            ? run.stopRequested && k + 1 < steps.length
              ? "已请求停止，后续步进不再发起"
              : null
            : outcome;
        // Only claude's 32万-character step brings an upstream token count back.
        const bound = m === 0 && passed >= 4 ? 96_214 : null;
        // A timeout, a rate limit or a stop says nothing about capacity: partial.
        const result: ModelTestState =
          passed === 0
            ? "failed"
            : reason && /超时|限流|停止/.test(reason)
              ? "partial"
              : "done";
        const summary =
          passed === 0
            ? (reason ?? "第一步就没有通过")
            : `通过 ${CHAR_STEPS[passed - 1] / 10_000}万 字符${bound ? `；上游报告 ≥${bound.toLocaleString("zh-CN")} Token` : "；上游没有报告 Token"}${reason ? `；${reason}` : ""}`;
        Object.assign(item, {
          state: result,
          stage: null,
          summary,
          detail: contextDetail(m, passed, bound, reason),
        });
        emit();
        later(250, () => next(run, i + 1));
      });
    };
    tick();
  }
  function finishRun(run: UmTestRun): void {
    run.state = "finished";
    run.note =
      "整批共用一份请求预算；字符步进按字符计，Token 是上游报告的已验证下界，不是最大上下文。";
    const at = Date.now();
    for (const item of run.items) {
      if (item.state === "not_run" || !item.summary) continue;
      const row = catalogRows.find((r) => r.id === item.model);
      if (row) {
        const entry = {
          at,
          ok: item.state === "done" || item.state === "partial",
          summary: item.summary,
        };
        row.lastContext = entry;
        if (item.detail?.contextChars.kind === "exact")
          row.context.probedChars = {
            chars: item.detail.contextChars.value,
            at,
          };
      }
      for (const old of history)
        if (old.model === item.model) old.archived = true;
      history.unshift({
        key: `h-${at}-${item.model}`,
        model: item.model,
        at,
        result: item.state,
        summary: item.summary,
        archived: false,
      });
    }
    if (state.models.catalog.state === "ready")
      state.models.catalog = ready(
        {
          ...state.models.catalog.data,
          models: catalogRows.map((r) => ({ ...r })),
        },
        state.models.catalog.at
      );
    state.models.history.records = ready(
      historyOf(state.models.history.filter),
      at
    );
    state.models.actions = modelCaps(
      state.overview.service.state === "ready" &&
        state.overview.service.data.state === "running",
      false
    );
    emit();
  }

  if (initialRun) later(4_000, () => next(initialRun, 2));
  publish(state);

  return (event: UmEvent) => {
    switch (event.type) {
      case "page":
        return;
      case "refresh": {
        const loads =
          event.page === "overview"
            ? ([
                "service",
                "login",
                "verify",
                "today",
                "trend",
                "issues",
              ] as const)
            : [];
        for (const key of loads) {
          const l = state.overview[key];
          if (l.state === "ready" || l.state === "stale")
            (l as { refreshing?: boolean }).refreshing = true;
        }
        emit();
        later(700, () => {
          for (const key of loads) {
            const l = state.overview[key];
            if (l.state === "ready")
              Object.assign(l, { refreshing: false, at: Date.now() });
            if (l.state === "stale")
              Object.assign(l, {
                refreshing: false,
                failure: { ...l.failure, at: Date.now() },
              });
          }
          if (event.page === "requests")
            state.requests.page = ready(
              requestPage(state.requests.query, 0),
              Date.now()
            );
          emit();
        });
        return;
      }
      case "trend.range":
        state.overview.trend = ready(trendFor(event.range), Date.now());
        emit();
        return;
      case "um.verify":
        begin("verify", "向上游确认，不调用模型");
        later(1_500, () => {
          if (
            scenario === "attention" &&
            state.overview.login.state === "ready" &&
            state.overview.login.data.state !== "valid"
          ) {
            state.overview.verify = ready(
              {
                result: "failed",
                word: "未通过",
                detail: "上游返回 401：登录已失效",
                at: Date.now(),
              },
              Date.now()
            );
            finishAction("verify", "failed", "上游返回 401：登录已失效", [
              "读取本机凭据",
              "向上游确认（不调用模型）",
              "上游返回 401",
            ]);
          } else {
            state.overview.verify = ready(
              {
                result: "passed",
                word: "通过",
                detail: "上游确认当前登录有效（未调用模型）",
                at: Date.now(),
              },
              Date.now()
            );
            finishAction("verify", "succeeded", "登录有效", [
              "读取本机凭据",
              "向上游确认（不调用模型）",
              "有效",
            ]);
          }
        });
        return;
      case "um.login": {
        const forced = event.force === true;
        begin(
          "login",
          forced
            ? "显式强制重新登录（可见窗口）"
            : "先核验，需要时才打开登录窗口"
        );
        later(2_400, () => {
          const was =
            state.overview.login.state === "ready"
              ? state.overview.login.data.state
              : "unknown";
          state.overview.login = ready(
            {
              state: "valid",
              word: "有效",
              detail: forced
                ? "已显式强制重新登录（预览只模拟，没有打开任何窗口）"
                : was === "valid"
                  ? "认证有效，没有打开登录窗口"
                  : "已重新登录（预览只模拟，没有打开任何窗口）",
              checkedAt: Date.now(),
            },
            Date.now()
          );
          state.overview.verify = ready(
            {
              result: "passed",
              word: "通过",
              detail: "上游确认当前登录有效（未调用模型）",
              at: Date.now(),
            },
            Date.now()
          );
          state.overview.issues = ready(
            {
              window: "近 24 小时",
              items: issues.filter((i) => i.kind !== "login"),
            },
            Date.now()
          );
          finishAction(
            "login",
            "succeeded",
            forced
              ? "已强制重新登录（可见窗口）"
              : was === "valid"
                ? "认证有效，无需登录"
                : "已重新登录",
            forced
              ? [
                  "跳过按需核验",
                  "打开可见登录窗口（预览中只模拟）",
                  "登录：有效",
                ]
              : was === "valid"
                ? ["核验：有效", "无需登录"]
                : [
                    "核验：凭据被拒（401）",
                    "打开专用登录窗口（预览中只模拟）",
                    "核验：有效",
                  ]
          );
        });
        return;
      }
      case "um.service":
        begin(
          event.action,
          event.action === "stop"
            ? "记录停用意图后停止进程"
            : event.action === "start"
              ? "启动 chat2api"
              : "停止后再启动"
        );
        later(1_800, () => {
          setService(event.action !== "stop");
          finishAction(
            event.action,
            "succeeded",
            event.action === "stop"
              ? "已停止；需要手动启动"
              : event.action === "start"
                ? "已启动，8000 可连接"
                : "已重启，8000 可连接",
            event.action === "stop"
              ? ["记录停用意图", "停止进程", "端口 8000 已释放"]
              : ["启动 chat2api", "端口 8000 可连接"]
          );
        });
        return;
      case "requests.query":
        state.requests.query = event.query;
        state.requests.page = { state: "loading" };
        state.requests.detail = null;
        emit();
        later(250, () => {
          if (state.requests.query === event.query) {
            state.requests.page = ready(
              requestPage(event.query, 0),
              Date.now()
            );
            emit();
          }
        });
        return;
      case "requests.earlier": {
        const page = state.requests.page;
        if (page.state !== "ready") return;
        state.requests.page = { ...page, refreshing: true };
        emit();
        later(400, () => {
          state.requests.page = ready(
            requestPage(
              state.requests.query,
              Number(event.cursor),
              page.data.rows
            ),
            Date.now()
          );
          emit();
        });
        return;
      }
      case "requests.detail": {
        const row = rows.find((r) => r.key === event.key);
        state.requests.detail = { key: event.key, logs: { state: "loading" } };
        emit();
        later(400, () => {
          if (state.requests.detail?.key !== event.key) return;
          state.requests.detail = {
            key: event.key,
            logs: ready(
              logs.filter(
                (l) => row?.requestId && l.requestId === row.requestId
              ),
              Date.now()
            ),
          };
          emit();
        });
        return;
      }
      case "logs.query":
        state.requests.logs = {
          query: event.query,
          page: down
            ? {
                state: "failed",
                failure: {
                  kind: "error",
                  reason: "读取本机日志超时（8 秒）",
                  at: Date.now(),
                },
              }
            : ready(logPage(event.query, 0), Date.now()),
        };
        emit();
        return;
      case "logs.earlier": {
        const page = state.requests.logs.page;
        if (page.state !== "ready") return;
        state.requests.logs = {
          query: state.requests.logs.query,
          page: ready(
            logPage(
              state.requests.logs.query,
              Number(event.cursor),
              page.data.lines
            ),
            Date.now()
          ),
        };
        emit();
        return;
      }
      case "loginRounds.query": {
        const round = event.query.round.trim();
        const q = event.query.q.trim().toLowerCase();
        const rows = loginRounds.filter(
          (row) =>
            (!round || String(row.round) === round) &&
            (!q ||
              (row.reason ?? "").toLowerCase().includes(q) ||
              (row.outcome ?? "").toLowerCase().includes(q) ||
              (row.triggerSource ?? "").toLowerCase().includes(q))
        );
        state.requests.loginRounds = {
          query: event.query,
          page: ready(
            {
              rounds: rows,
              scope: `本机 8000 UM 登录轮次历史（预览筛选）· 本页 ${rows.length} 条`,
              truncated: false,
            },
            Date.now()
          ),
        };
        emit();
        return;
      }
      case "maintenance.earlier": {
        const page = state.requests.maintenance;
        if (page.state !== "ready") return;
        state.requests.maintenance = ready(
          maintenancePage(Number(event.cursor), page.data.rows),
          Date.now()
        );
        emit();
        return;
      }
      case "models.refreshCatalog":
        state.models.refresh = { state: "refreshing", since: Date.now() };
        emit();
        later(1_300, () => {
          if (scenario === "attention") {
            state.models.refresh = {
              state: "failed",
              at: Date.now(),
              reason: "UM 返回了登录页（认证失效），没有拿到目录",
            };
            if (state.models.catalog.state === "ready")
              state.models.catalog = {
                state: "stale",
                data: state.models.catalog.data,
                at: state.models.catalog.at,
                failure: {
                  kind: "unauthenticated",
                  reason: "UM 返回了登录页",
                  at: Date.now(),
                },
              };
          } else {
            state.models.refresh = { state: "succeeded", at: Date.now() };
            if (
              state.models.catalog.state === "ready" ||
              state.models.catalog.state === "stale"
            )
              state.models.catalog = ready(
                { ...state.models.catalog.data, fetchedAt: Date.now() },
                Date.now()
              );
          }
          emit();
        });
        return;
      case "models.test":
        startRun([...new Set(event.models)]);
        return;
      case "models.stop":
        if (
          state.models.run?.id === event.runId &&
          state.models.run.state !== "finished"
        ) {
          state.models.run.stopRequested = true;
          emit();
        }
        return;
      case "models.history":
        state.models.history = {
          filter: event.filter,
          records: ready(historyOf(event.filter), Date.now()),
        };
        emit();
        return;
    }
  };
}

function contextDetail(
  m: number,
  passed: number,
  bound: number | null,
  reason: string | null
): UmTestDetail {
  return {
    durationMs: { kind: "exact", value: passed * 4_300 },
    firstTokenMs: { kind: "missing", note: "上下文探测不测首字" },
    outputTokens: { kind: "exact", value: passed * 12 },
    speed: { kind: "missing", note: "上下文探测不计速度" },
    speedBasis: "whole_request",
    contextChars: passed
      ? { kind: "exact", value: CHAR_STEPS[passed - 1] }
      : { kind: "missing" },
    contextTokenLowerBound:
      bound !== null
        ? { kind: "exact", value: bound }
        : { kind: "unreported", note: `${SPECS[m].id} 的上游没有报告 Token` },
    // A stop or a spent budget ends the probe without anything going wrong.
    error: reason && !/停止|预算/.test(reason) ? reason : null,
    lines: [
      ...CHAR_STEPS.slice(0, passed).map((c) => `${c / 10_000}万 字符：通过`),
      ...(reason ? [reason] : []),
    ],
  };
}
