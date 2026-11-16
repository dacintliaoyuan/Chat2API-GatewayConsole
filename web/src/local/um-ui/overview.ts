import type {
  ActionProgress,
  Load,
  MaintenanceAction,
  UmIssue,
  UmLogin,
  UmOverview,
  UmService,
  UmToday,
  UmVerify,
} from "./contract";
import { METRICS, type Metric, type TrendView } from "./charts";
import {
  blockReason,
  capButton,
  dayTitle,
  drawable,
  esc,
  freshnessHtml,
  full,
  loaded,
  monthDay,
  placeholderHtml,
  readingHtml,
  speed,
  speedBasisWord,
  speedNum,
  tokens,
  when,
  wordOf,
} from "./format";

/** 总览: UM 服务 / UM 登录 / 最近 UM 核验 side by side, today's five
 *  readings, the trend with a ranking of the same window, and the recent
 *  problems that lead into 请求与诊断. */

/** `line`: Token / 请求 drawn as lines instead of stacked bars. */
export type OverviewUi = { metric: Metric; line: boolean };

type Tone = "ok" | "warn" | "err" | "neutral";
const SERVICE_TONE: Readonly<Record<UmService["state"], Tone>> = {
  running: "ok",
  starting: "neutral",
  stopping: "neutral",
  stopped: "err",
  degraded: "warn",
  unknown: "neutral",
};
const LOGIN_TONE: Readonly<Record<UmLogin["state"], Tone>> = {
  valid: "ok",
  needs_login: "warn",
  invalid: "err",
  logging_in: "neutral",
  unknown: "neutral",
};
const VERIFY_TONE: Readonly<Record<NonNullable<UmVerify>["result"], Tone>> = {
  passed: "ok",
  failed: "err",
  unknown: "neutral",
};

export const ACTION_LABEL: Readonly<Record<MaintenanceAction, string>> = {
  verify: "核验登录",
  login: "检查并按需登录",
  start: "启动 UM 服务",
  stop: "停止 UM 服务",
  restart: "重启 UM 服务",
};
type ActionResult = Extract<ActionProgress, { state: "done" }>["result"];
const RESULT_WORD: Readonly<Record<ActionResult, string>> = {
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  unconfirmed: "结果未确认",
};
const RESULT_TONE: Readonly<Record<ActionResult, Tone>> = {
  succeeded: "ok",
  failed: "err",
  cancelled: "neutral",
  unconfirmed: "warn",
};

/** The action button of a maintenance entry (`data-um-act` names it). A
 *  closed one prints its reason under it: a disabled button cannot be
 *  focused, so a hover hint alone would hide the reason from keyboards. */
export function actionButton(
  overview: UmOverview,
  action: MaintenanceAction,
  cls = "btn btn-sm btn-tonal",
  label = ACTION_LABEL[action]
): string {
  const busy = overview.progress[action]?.state === "pending";
  const reason = blockReason(overview.actions[action], busy);
  return `${capButton(overview.actions[action], `data-um-act="${action}"`, cls, esc(label), busy)}${reason && !busy ? `<span class="um-block-reason">${esc(reason)}</span>` : ""}`;
}

/** The secondary explicit forced re-login entry (ADR 0005). It shares the
 *  login capability and progress with the on-demand entry; the click asks for a
 *  visible window and skips the on-demand verification. */
export function forceLoginButton(o: UmOverview): string {
  const busy = o.progress.login?.state === "pending";
  const reason = blockReason(o.actions.login, busy);
  return `${capButton(o.actions.login, "data-um-force-login", "btn btn-sm btn-outline", "强制重新登录（可见窗口）", busy)}${reason && !busy ? `<span class="um-block-reason">${esc(reason)}</span>` : ""}`;
}

function progressHtml(
  action: MaintenanceAction,
  progress: ActionProgress | undefined
): string {
  if (!progress) return "";
  if (progress.state === "pending") {
    return `<p class="um-progress" data-tone="neutral" role="status"><span class="um-spinner" aria-hidden="true"></span>${esc(ACTION_LABEL[action])}：进行中${progress.note ? ` · ${esc(progress.note)}` : ""}（${esc(when(progress.since))} 起）</p>`;
  }
  return `<p class="um-progress" data-tone="${wordOf(RESULT_TONE, progress.result, "neutral")}" role="status"><span class="dot" aria-hidden="true"></span>${esc(ACTION_LABEL[action])}：${esc(wordOf(RESULT_WORD, progress.result, "结果未知"))} · ${esc(progress.note)}（${esc(when(progress.at))}）</p>`;
}

function statusCard(
  title: string,
  load: Load<unknown>,
  body:
    | (() => {
        tone: Tone;
        word: string;
        detail: string;
        time: string;
        handle: string;
      })
    | null,
  progress: string
): string {
  const head = `<header class="um-status-head"><h2>${esc(title)}</h2></header>`;
  const view = body?.();
  if (!view)
    return `<article class="um-status card" data-tone="neutral">${head}${placeholderHtml(load, title)}${progress}</article>`;
  const shown = loaded(load)!;
  const fresh = shown.stale
    ? freshnessHtml(shown.at, shown.stale)
    : `<span class="um-fresh">${esc(view.time)}</span>`;
  return `<article class="um-status card" data-tone="${view.tone}">${head}
    <p class="um-status-word"><span class="dot" aria-hidden="true"></span>${esc(view.word)}</p>
    <p class="um-status-detail">${esc(view.detail)}</p>
    <div class="um-status-foot">${fresh}${view.handle ? `<span class="um-status-actions">${view.handle}</span>` : ""}</div>
    ${progress}</article>`;
}

function statusRow(o: UmOverview): string {
  const service = loaded(o.service),
    login = loaded(o.login),
    verify = loaded(o.verify);
  const checked = (at: number | null) =>
    at === null ? "核对时间未知" : `核对于 ${when(at)}`;
  const serviceCard = statusCard(
    "UM 服务",
    o.service,
    service &&
      (() => {
        const s = service.data;
        const facts = [
          s.port !== null ? `端口 ${s.port}` : null,
          s.pid !== null ? `PID ${s.pid}` : null,
          s.detail || null,
        ]
          .filter(Boolean)
          .join(" · ");
        return {
          tone: wordOf(SERVICE_TONE, s.state, "neutral"),
          word: s.word,
          detail: facts,
          time: checked(s.checkedAt),
          handle:
            s.state === "stopped"
              ? actionButton(o, "start")
              : s.state === "degraded"
                ? `<span class="um-hint">可在「维护」中重启</span>`
                : "",
        };
      }),
    [
      o.progress.start && progressHtml("start", o.progress.start),
      o.progress.stop && progressHtml("stop", o.progress.stop),
      o.progress.restart && progressHtml("restart", o.progress.restart),
    ]
      .filter(Boolean)
      .join("")
  );
  const loginCard = statusCard(
    "UM 登录",
    o.login,
    login &&
      (() => {
        const l = login.data;
        return {
          tone: wordOf(LOGIN_TONE, l.state, "neutral"),
          word: l.word,
          detail: l.detail,
          time: checked(l.checkedAt),
          handle: [
            ["needs_login", "invalid", "unknown"].includes(l.state)
              ? actionButton(o, "login")
              : "",
            forceLoginButton(o),
          ]
            .filter(Boolean)
            .join(""),
        };
      }),
    progressHtml("login", o.progress.login)
  );
  const verifyCard = statusCard(
    "最近 UM 核验",
    o.verify,
    verify &&
      (() => {
        const v = verify.data;
        if (v === null)
          return {
            tone: "neutral" as Tone,
            word: "从未核验",
            detail: "还没有向上游确认过当前登录凭据。",
            time: "",
            handle: actionButton(
              o,
              "verify",
              "btn btn-sm btn-tonal",
              "立即核验"
            ),
          };
        return {
          tone: wordOf(VERIFY_TONE, v.result, "neutral"),
          word: v.word,
          detail: v.detail,
          time: `核验于 ${when(v.at)}`,
          handle:
            v.result !== "passed"
              ? actionButton(o, "verify", "btn btn-sm btn-tonal", "重新核验")
              : "",
        };
      }),
    progressHtml("verify", o.progress.verify)
  );
  return `<section class="um-status-row" aria-label="UM 状态">${serviceCard}${loginCard}${verifyCard}</section>`;
}

const CACHE_REPORT: Readonly<Record<UmToday["cacheRead"]["report"], string>> = {
  reported: "每条回复都报告了缓存",
  partial: "部分回复未报告缓存",
  unreported: "上游没有报告缓存",
  unknown: "报告情况未知",
};

function kpi(label: string, value: string, sub: string, note = ""): string {
  return `<div class="um-kpi"><div class="um-kpi-label">${esc(label)}</div><div class="um-kpi-value">${value}</div><div class="um-kpi-sub"${note ? ` title="${esc(note)}"` : ""}>${sub}</div></div>`;
}

function kpisHtml(t: UmToday): string {
  const failed = t.requests.failed;
  const failedHtml =
    failed.kind === "exact" && failed.value === 0
      ? "无失败"
      : failed.kind === "exact" ||
          failed.kind === "atLeast" ||
          failed.kind === "estimate"
        ? `<span class="um-err-text">${readingHtml(failed, (n) => `${full(n)} 次失败`)}</span>`
        : `失败数 ${readingHtml(failed, full)}`;
  return `<div class="um-kpis">
    ${kpi("Token 合计", readingHtml(t.tokens.total, tokens), `输入 ${readingHtml(t.tokens.input, tokens)} · 输出 ${readingHtml(t.tokens.output, tokens)}`)}
    ${kpi("缓存读取", readingHtml(t.cacheRead.tokens, tokens), `已含在输入中 · ${esc(wordOf(CACHE_REPORT, t.cacheRead.report, "报告情况未知"))}`, t.cacheRead.detail)}
    ${kpi("思考 Token", readingHtml(t.reasoning, tokens), "已计入输出，不重复相加")}
    ${kpi("已记录请求", readingHtml(t.requests.recorded, full), `${failedHtml} · 重试不重复计`)}
    ${kpi("速度", drawable(t.speed.value) === null ? readingHtml(t.speed.value, speed) : `${readingHtml(t.speed.value, speedNum)}<small>token/秒</small>`, `${esc(speedBasisWord(t.speed.basis))}${t.speed.samples !== null ? ` · ${full(t.speed.samples)} 次计时` : ""}`)}
  </div>`;
}

function usageHtml(o: UmOverview, ui: OverviewUi): string {
  const today = loaded(o.today);
  const meta = today
    ? `${esc(dayTitle(today.data.day))} · 北京时间 · ${freshnessHtml(today.at, today.stale, today.refreshing)}`
    : "北京时间自然日";
  return `<section class="card um-usage" aria-labelledby="um-usage-title">
    <div class="um-usage-head"><div class="um-usage-title"><h2 id="um-usage-title">今日用量</h2><p class="card-meta">${meta}</p></div></div>
    ${today ? kpisHtml(today.data) : placeholderHtml(o.today, "今日用量")}
  </section>`;
}

function segmented(
  name: string,
  label: string,
  options: readonly { id: string; label: string }[],
  current: string,
  attr: string,
  disabled = false
): string {
  return `<div class="um-seg" role="group" aria-label="${esc(label)}">${options.map((o) => `<button type="button" ${attr}="${o.id}" aria-pressed="${o.id === current}"${disabled ? " disabled" : ""}>${esc(o.label)}</button>`).join("")}</div>`;
}
const RANGES = [
  { id: "7d", label: "7 天" },
  { id: "14d", label: "14 天" },
  { id: "30d", label: "30 天" },
] as const;

function trendHtml(o: UmOverview, ui: OverviewUi, chart: TrendView): string {
  const trend = loaded(o.trend);
  const range = trend?.data.range ?? "7d";
  // 速度 is a line whatever the toggle says: the button shows it pressed and closed.
  const speedOnly = ui.metric === "speed";
  const head = `<div class="card-head um-trend-head"><h2>用量趋势</h2>
    ${segmented("metric", "指标", METRICS, ui.metric, "data-um-metric", !trend)}
    <span class="card-grow"></span>
    ${segmented("range", "时段", RANGES, range, "data-um-range", !trend)}
    <button type="button" class="um-chip-toggle" data-um-toggle="line" aria-pressed="${speedOnly || ui.line}"${!trend || speedOnly ? " disabled" : ""}${speedOnly ? ` title="速度不能相加，只画折线"` : ""}><svg class="shell-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3.5 17.5l5-6 4 3.5 7.5-9"/></svg>折线图</button></div>`;
  if (!trend)
    return `<section class="card um-trend">${head}${placeholderHtml(o.trend, "用量趋势")}</section>`;
  const t = trend.data;
  chart.set(t, ui.metric, ui.line ? "line" : "bar");
  const span = t.days.length
    ? `${esc(monthDay(t.days[0]))} – ${esc(monthDay(t.days[t.days.length - 1]))}`
    : "这个时段没有日期";
  // Quality is carried by the line style and the tooltip: name it once, only when
  // the window actually holds such a figure, so the chart never drops it silently.
  const quality = [
    chart.quality.lower ? "≥ 下界为短虚线" : "",
    chart.quality.estimated ? "≈ 估算见悬浮明细" : "",
  ]
    .filter(Boolean)
    .join(" · ");
  const scope = `<p class="um-trend-scope">${span} · 北京时间自然日，今天未结束 · 排行与图表同一时段${quality ? ` · ${quality}` : ""} · ${freshnessHtml(trend.at, trend.stale, trend.refreshing)}</p>`;
  const plot = `<div class="um-chart" data-um-chart tabindex="0" role="group" aria-roledescription="图表" aria-label="${esc(chart.label())}"></div><p class="sr-only" aria-live="polite" data-um-chart-live></p>`;
  return `<section class="card um-trend">${head}${scope}<div class="um-trend-body"><div class="um-trend-plot">${plot}</div><div class="um-trend-rank" role="group" aria-label="模型排行（同一时段）">${chart.rankingHtml()}</div></div></section>`;
}

const ISSUE_TONE: Readonly<Record<UmIssue["kind"], Tone>> = {
  request_failed: "err",
  truncated: "err",
  client_disconnected: "warn",
  login: "warn",
  service: "err",
  catalog: "warn",
  test: "warn",
};

function issuesHtml(o: UmOverview): string {
  const issues = loaded(o.issues);
  const head = (meta: string) =>
    `<div class="card-head"><h2>近期问题</h2><span class="card-meta">${meta}</span><span class="card-grow"></span><button type="button" class="btn btn-sm btn-outline" data-um-go="requests/records">查看请求记录</button></div>`;
  if (!issues)
    return `<section class="card um-issues">${head("")}${placeholderHtml(o.issues, "近期问题")}</section>`;
  const { window, items } = issues.data;
  const meta = `${esc(window)} · ${freshnessHtml(issues.at, issues.stale, issues.refreshing)}`;
  if (!items.length)
    return `<section class="card um-issues">${head(meta)}<p class="um-empty"><span class="dot" data-tone="ok" aria-hidden="true"></span>${esc(window)}没有记录到 UM 问题。</p></section>`;
  const rows = items
    .map(
      (
        issue,
        i
      ) => `<li class="um-issue" data-tone="${wordOf(ISSUE_TONE, issue.kind, "warn")}">
      <span class="status" data-tone="${wordOf(ISSUE_TONE, issue.kind, "warn")}"><span class="dot" aria-hidden="true"></span>${esc(issue.title)}</span>
      <span class="um-issue-count">${full(issue.count)} 次</span>
      <span class="um-issue-detail">${esc(issue.detail)}</span>
      <span class="um-issue-time">最近 ${esc(when(issue.lastAt))}</span>
      ${issue.evidence ? `<button type="button" class="btn btn-sm btn-tonal" data-um-evidence="${i}">查看证据</button>` : `<span class="um-hint">证据不在本页</span>`}
    </li>`
    )
    .join("");
  return `<section class="card um-issues">${head(meta)}<ul class="um-issue-list">${rows}</ul></section>`;
}

export function overviewHtml(
  o: UmOverview,
  ui: OverviewUi,
  chart: TrendView
): string {
  return `<div class="um-page um-overview">${statusRow(o)}${usageHtml(o, ui)}${trendHtml(o, ui, chart)}${issuesHtml(o)}</div>`;
}

/** The maintenance menu items with their impact, in the toolbar capsule. */
export function maintenanceMenuHtml(o: UmOverview): string {
  const items: [MaintenanceAction, string][] = [
    ["verify", "向上游确认当前登录是否有效；不调用模型，不自动登录"],
    ["login", "先核验；只有凭据缺失或被拒时才打开专用登录窗口"],
    ["start", "启动 8000 上的 chat2api 主链"],
    ["restart", "重启期间经 8000 的请求会失败"],
    ["stop", "停止后经 8000 的请求全部失败，需手动启动"],
  ];
  return items
    .map(([action, impact]) => {
      const block = blockReason(
        o.actions[action],
        o.progress[action]?.state === "pending"
      );
      return `<button type="button" role="menuitem" class="um-menu-item${action === "stop" ? " um-menu-danger" : ""}" data-um-act="${action}"${block ? ' aria-disabled="true"' : ""}>
      <span class="um-menu-name">${esc(ACTION_LABEL[action])}${action === "verify" ? "" : "…"}</span><span class="um-menu-note">${esc(block ?? impact)}</span></button>`;
    })
    .join("");
}

/** Confirm copy for the writes the console confirms itself (r3: the existing
 *  confirms stay; only model tests drop theirs). */
export const CONFIRM: Readonly<
  Record<
    Exclude<MaintenanceAction, "verify">,
    { title: string; body: string; confirm: string; danger?: boolean }
  >
> = {
  login: {
    title: "检查并按需登录 UM？",
    confirm: "检查并按需登录",
    body: "先核验当前 UM 登录；认证有效时不做任何事。凭据缺失或被拒时，会在本机打开专用 Edge 登录窗口，需要你本人完成登录。",
  },
  start: {
    title: "启动 UM 服务（8000）？",
    confirm: "启动",
    body: "启动 8000 上的 chat2api 主链。启动后客户端经 8000 的请求重新被受理；这次启动会取代之前记录的停用意图。",
  },
  restart: {
    title: "重启 UM 服务（8000）？",
    confirm: "重启 UM 服务",
    danger: true,
    body: "重启期间经 8000 的请求会失败，进行中的请求会被中断；进程起来后恢复受理。",
  },
  stop: {
    title: "停止 UM 服务（8000）？",
    confirm: "停止 UM 服务",
    danger: true,
    body: "停止后，经 8000 的所有 UM 请求都会失败，进行中的请求会被中断。后端先记录停用意图再停止进程，之后需要手动启动。",
  },
};
