import { escapeHtml } from "../html";
import { fmtCny, fmtNum, fmtTokens } from "../console/ui";
import type { ShareKeyRow, ShareStatusPayload } from "../console/share-model";
import type {
  CostDetail,
  ShareUsage,
  ShareUsageRequestRow,
} from "../console/backend/types";
import {
  FAILURE_WORD,
  lastValue,
  type CloudHealth,
  type HealthRow,
  type OperationRow,
  type ReadState,
} from "./cloud-reads";
import {
  AUTH_SOURCE_WORD,
  authWord,
  lastAuthObservation,
  operationOutcome,
  opStateWord,
  type AuthObservation,
} from "./operation-intent";
import type { CloudModel, KeyTest } from "./cloud-console";

/** Cloud overview (总览): the service and authentication state, the latest
 *  verification, what needs handling, today's usage beside the configured
 *  concurrency cap, and a 14-day trend with its ranking. Every figure comes
 *  from a real read; a read that failed says so with its reason, a value that
 *  cannot be known says 未知, and nothing is rounded into a reassuring 0. */

export type Tone = "ok" | "warn" | "err" | "neutral";
export type OverviewMetric = "requests" | "tokens";
export type OverviewRank = "key" | "model";
/** D18-13: the chart window is 7/14/30 days and the Key ranking uses the
 *  same window; `chartWidth` is the measured container width (0 = default). */
export type OverviewRange = "7" | "14" | "30";
export type OverviewUi = {
  metric: OverviewMetric;
  rank: OverviewRank;
  range: OverviewRange;
  chartWidth: number;
};

const ICON = {
  alert: `<path d="M12 3.5 21 19.5H3z"/><path d="M12 10v4.5M12 17.2h.01"/>`,
  ok: `<circle cx="12" cy="12" r="8.5"/><path d="m8.5 12.2 2.4 2.4 4.8-5"/>`,
  key: `<circle cx="8" cy="15" r="4.5"/><path d="M11.2 11.8 20 3M16.5 6.5l2.5 2.5M14 9l2 2"/>`,
  copy: `<rect x="8.5" y="8.5" width="11" height="11" rx="2.5"/><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/>`,
  stop: `<rect x="6.5" y="6.5" width="11" height="11" rx="2.5"/>`,
  play: `<path d="M8 5.5v13l10.5-6.5z"/>`,
  login: `<path d="M14 4.5h3.5a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H14"/><path d="M4.5 12h10M11 8.5l3.5 3.5-3.5 3.5"/>`,
};
export const svgIcon = (name: keyof typeof ICON, cls = "shell-icon") =>
  `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICON[name]}</svg>`;

const SHANGHAI_DAY = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});
/** Calendar day in Asia/Shanghai — the backend buckets share usage by Beijing day. */
export const beijingDay = (ms: number) => SHANGHAI_DAY.format(ms);
/** The `n` Beijing days ending today, oldest first. */
export function lastDays(n: number, now = Date.now()): string[] {
  return Array.from({ length: n }, (_, i) =>
    beijingDay(now - (n - 1 - i) * 86_400_000)
  );
}
export const clock = (ms: number) =>
  new Date(ms).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
export const clockFromEpoch = (seconds: number | undefined | null) =>
  typeof seconds === "number" && Number.isFinite(seconds)
    ? clock(seconds * 1000)
    : null;
/** A reading's time: the clock alone today (Beijing), with the date otherwise. */
export const when = (ms: number) =>
  beijingDay(ms) === beijingDay(Date.now())
    ? clock(ms)
    : `${beijingDay(ms).slice(5).replace("-", "/")} ${clock(ms)}`;

export const tonePill = (tone: Tone, word: string) =>
  `<span class="pill" data-tone="${tone}"><span class="dot" aria-hidden="true"></span>${escapeHtml(word)}</span>`;
export const toneStatus = (tone: Tone, word: string) =>
  `<span class="status" data-tone="${tone}"><span class="dot" aria-hidden="true"></span>${escapeHtml(word)}</span>`;

/** The fields every usage aggregate carries; a key row, the unattributed bucket
 *  and a day/model detail all satisfy this structurally. */
export type SummaryRow = Pick<
  CostDetail,
  "coverage" | "tokenUsage" | "costCny" | "knownCostCny" | "estimatedCostCny"
>;
export type UsageAggregate = {
  requests: number | null;
  tokens: number | null;
  tokensComplete: boolean;
  costCny: number | null;
  costPartial: boolean;
  unknownRequests: number | null;
  unpricedRequests: number | null;
  incompleteRequests: number | null;
  unknownTimeRequests: number | null;
  tokenUnknownRequests: number | null;
  cacheUnreportedRequests: number | null;
  records: number | null;
};
function sumOrNull(
  values: readonly (number | null | undefined)[]
): number | null {
  let total = 0;
  for (const value of values) {
    if (typeof value !== "number" || !Number.isFinite(value)) return null;
    total += value;
  }
  return total;
}
/** One honest aggregate over real rows. A missing request/coverage field stays
 *  null, and a Token total falls back to the reported lower bound (marked
 *  incomplete) instead of inventing a complete 0. */
export function aggregateRows(rows: readonly SummaryRow[]): UsageAggregate {
  const tokenValues = rows.map((row) =>
    row.tokenUsage.totalTokens !== null
      ? row.tokenUsage.totalTokens
      : row.tokenUsage.lowerBoundTokens
  );
  const tokens = sumOrNull(tokenValues);
  return {
    requests: sumOrNull(rows.map((row) => row.coverage.requests)),
    tokens,
    tokensComplete:
      tokens !== null &&
      rows.every(
        (row) =>
          row.tokenUsage.totalTokens !== null &&
          row.tokenUsage.complete === true
      ),
    costCny: sumOrNull(rows.map((row) => row.costCny)),
    costPartial: rows.some(
      (row) =>
        row.coverage.costCnyPartial === true ||
        row.coverage.costComplete === false
    ),
    unknownRequests: sumOrNull(rows.map((row) => row.coverage.unknownRequests)),
    unpricedRequests: sumOrNull(
      rows.map((row) => row.coverage.unpricedRequests)
    ),
    incompleteRequests: sumOrNull(
      rows.map((row) => row.coverage.incompleteRequests)
    ),
    unknownTimeRequests: sumOrNull(
      rows.map((row) => row.coverage.unknownTimeRequests)
    ),
    tokenUnknownRequests: sumOrNull(
      rows.map((row) => row.tokenUsage.unknownRequests)
    ),
    cacheUnreportedRequests: sumOrNull(
      rows.map((row) => row.coverage.cacheUnreportedRequests)
    ),
    records: sumOrNull(rows.map((row) => row.coverage.records)),
  };
}
const zeroAggregate = (): UsageAggregate => ({
  requests: 0,
  tokens: 0,
  tokensComplete: true,
  costCny: 0,
  costPartial: false,
  unknownRequests: 0,
  unpricedRequests: 0,
  incompleteRequests: 0,
  unknownTimeRequests: 0,
  tokenUnknownRequests: 0,
  cacheUnreportedRequests: 0,
  records: 0,
});
export function windowSum(
  days: readonly CostDetail[],
  window: ReadonlySet<string>
): UsageAggregate {
  return aggregateRows(days.filter((row) => window.has(row.identity)));
}

export type DailyTotals = {
  days: Map<string, UsageAggregate>;
  /** Any key (or the unattributed bucket) actually carries day rows. */
  hasDaily: boolean;
  /** Keys that have requests but no day detail at all. Their requests are not in
   *  any day bucket, so a daily total must not add a fabricated 0 for them. */
  missingKeys: number;
  /** The unattributed bucket has requests but no day detail; it is not a Key. */
  missingUnattributed: boolean;
};
/** Requests and Tokens per Beijing day, across every key and the unattributed
 *  bucket. The day rows are the backend's own buckets; a key without them is
 *  counted as missing instead of being spread into a fake zero. */
export function dailyTotals(usage: ShareUsage): DailyTotals {
  const buckets = new Map<string, SummaryRow[]>();
  let hasDaily = false;
  let missingKeys = 0;
  const add = (rows: readonly CostDetail[]) => {
    if (rows.length) hasDaily = true;
    for (const row of rows) {
      const bucket = buckets.get(row.identity) ?? [];
      bucket.push(row);
      buckets.set(row.identity, bucket);
    }
  };
  for (const key of usage.keys) {
    if (
      key.days.length === 0 &&
      (key.coverage.requests === null || key.coverage.requests > 0)
    )
      missingKeys += 1;
    add(key.days);
  }
  const missingUnattributed =
    usage.unattributed.days.length === 0 &&
    (usage.unattributed.coverage.requests === null ||
      usage.unattributed.coverage.requests > 0);
  add(usage.unattributed.days);
  return {
    days: new Map(
      [...buckets].map(([day, rows]) => [day, aggregateRows(rows)])
    ),
    hasDaily,
    missingKeys,
    missingUnattributed,
  };
}
/** Records the backend could not date; they are in the totals but on no day.
 *  `reported:false` means the backend has no undated bucket at all; a reported
 *  bucket whose request count is null stays unknown (`count:null`), never 0. */
export type UndatedRequests = { reported: boolean; count: number | null };
export function undatedRequests(usage: ShareUsage): UndatedRequests {
  const buckets = [
    usage.unattributed.undated,
    ...usage.keys.map((key) => key.undated),
  ].filter(
    (bucket): bucket is NonNullable<typeof bucket> =>
      bucket !== null && bucket !== undefined
  );
  if (!buckets.length) return { reported: false, count: 0 };
  return {
    reported: true,
    count: sumOrNull(buckets.map((bucket) => bucket.coverage.requests)),
  };
}
/** The backend's coverage block says whether the share ledger was present and
 *  readable. An absent/unreadable ledger answers requests 0 with no rows, but that
 *  is NOT a measured empty ledger: the KPI, the ranking, the trend and the Key
 *  rows say so instead of showing 0. A payload that omits the flags cannot prove
 *  the ledger was read, so it is unknown too, never a measured zero. */
export type LedgerRead = { unread: boolean; reason: string };
export function ledgerRead(usage: ShareUsage): LedgerRead {
  const present = usage.coverage.ledgerPresent;
  const readable = usage.coverage.ledgerReadable;
  if (present === false) return { unread: true, reason: "账本文件缺席" };
  if (readable === false) return { unread: true, reason: "账本读取失败" };
  if (present !== true || readable !== true)
    return { unread: true, reason: "账本状态未上报" };
  return { unread: false, reason: "" };
}
export type TodayUsage = {
  /** False when the backend supplied no day detail at all while it did report
   *  requests; the page then says 后端未提供按日明细 instead of 0. */
  available: boolean;
  requests: number | null;
  tokens: number | null;
  tokensComplete: boolean;
  costCny: number | null;
  costPartial: boolean;
  missingKeys: number;
  missingUnattributed: boolean;
};
export function todayUsage(usage: ShareUsage, now = Date.now()): TodayUsage {
  const totals = dailyTotals(usage);
  const empty: TodayUsage = {
    available: false,
    requests: null,
    tokens: null,
    tokensComplete: false,
    costCny: null,
    costPartial: false,
    missingKeys: totals.missingKeys,
    missingUnattributed: totals.missingUnattributed,
  };
  if (!totals.hasDaily && (usage.requests === null || usage.requests > 0))
    return empty;
  const day = totals.days.get(beijingDay(now));
  if (!day) {
    // With every key carrying day buckets, no bucket for today is a real zero.
    // A key without day detail could still have had requests today, so the total
    // is not claimed as 0 while any such key exists.
    if (totals.missingKeys > 0 || totals.missingUnattributed) return empty;
    return {
      ...empty,
      available: true,
      requests: 0,
      tokens: 0,
      tokensComplete: true,
      costCny: 0,
    };
  }
  return {
    available: true,
    requests: day.requests,
    tokens: day.tokens,
    tokensComplete: day.tokensComplete,
    costCny: day.costCny,
    costPartial: day.costPartial,
    missingKeys: totals.missingKeys,
    missingUnattributed: totals.missingUnattributed,
  };
}

/** API entry (share-gateway, 8100): the backend's pid / process-identity snapshot. */
export function apiEntry(health: ReadState<CloudHealth>): {
  tone: Tone;
  word: string;
  row: HealthRow | null;
  at: number | null;
  stale: boolean;
} {
  const read = lastValue(health);
  if (!read)
    return {
      tone: "neutral",
      word: health.state === "loading" ? "读取中" : "未知",
      row: null,
      at: null,
      stale: false,
    };
  const row =
    read.value.services?.find((s) => s.key === "share-gateway") ?? null;
  const at =
    typeof read.value.checked_at === "number"
      ? read.value.checked_at * 1000
      : read.at;
  if (!row)
    return {
      tone: "neutral",
      word: "未上报",
      row: null,
      at,
      stale: read.stale,
    };
  const word =
    row.state === "Running"
      ? row.healthy === false
        ? "运行中 · 未通过检查"
        : "运行中"
      : row.state === "Stopped"
        ? "已停止"
        : row.state === "Conflict"
          ? "进程身份冲突"
          : "未知";
  const tone: Tone =
    row.state === "Running"
      ? row.healthy === false
        ? "warn"
        : "ok"
      : row.state === "Stopped"
        ? "err"
        : row.state === "Conflict"
          ? "warn"
          : "neutral";
  return { tone, word, row, at, stale: read.stale };
}

export type StatusCheck = {
  at: number | null;
  backend: string | null;
  authState: string | null;
  authLabel: string | null;
};
const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === "object" && !Array.isArray(value);
/** The newest succeeded share.status result in the operations window - the
 *  backend's own layered check. The status read folds the auth states into
 *  account.valid and leaves `account` out while 8110 is down, so the layer word
 *  and the auth label exist only here (backend cross-check, 2026-10-31). */
export function lastStatusCheck(
  ops: ReadState<OperationRow[]>
): StatusCheck | null {
  const op = (lastValue(ops)?.value ?? [])
    .filter(
      (row) =>
        row.action === "share.status" &&
        row.state === "succeeded" &&
        isRecord(row.result)
    )
    .sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))[0];
  if (!op) return null;
  const result = op.result as Record<string, unknown>;
  const layers = isRecord(result.layers) ? result.layers : {};
  const auth = isRecord(result.backend_auth) ? result.backend_auth : {};
  const seconds =
    typeof auth.observed_at === "number"
      ? auth.observed_at
      : (op.finished_at ?? op.created_at ?? null);
  return {
    at: typeof seconds === "number" ? seconds * 1000 : null,
    backend:
      typeof layers.share_backend === "string" ? layers.share_backend : null,
    authState: typeof auth.state === "string" ? auth.state : null,
    authLabel: typeof auth.label === "string" && auth.label ? auth.label : null,
  };
}
const LAYER_WORD: Readonly<Record<string, string>> = {
  running: "运行中",
  stopped: "已停止",
};
/** The last layered check as one line: 「最近一次分层检查 21:05：UM 后端已停止」. */
export function checkLine(check: StatusCheck | null): string {
  if (!check) return "";
  const parts = [
    check.backend
      ? `UM 后端${LAYER_WORD[check.backend] ?? ` ${check.backend}`}`
      : "",
    check.authLabel ?? (check.authState ? `认证 ${check.authState}` : ""),
  ].filter(Boolean);
  return parts.length
    ? `最近一次分层检查 ${check.at ? when(check.at) : "时间未知"}：${parts.join(" · ")}`
    : "";
}

type Word = { tone: Tone; word: string };
/** The only current login progress: a login/re-auth operation the backend still
 *  reports as queued or running in a fresh operations read. `at` is its submit
 *  time (created_at, ms), not the time the progress was observed. */
export type CurrentLogin = {
  state: "queued" | "running";
  at: number | null;
  action: string;
};
const LOGIN_ACTIONS: ReadonlySet<string> = new Set([
  "share.backend_login",
  "share.backend_reauthenticate",
]);
const LOGIN_ACTION_WORD: Readonly<Record<string, string>> = {
  "share.backend_login": "启动 UM 登录流程",
  "share.backend_reauthenticate": "重新认证 UM",
};
/** The newest login/re-auth operation still in flight. A terminal row is history,
 *  and a stale operations read cannot prove the backend is still running it. */
function currentLoginOperation(
  ops: ReadState<OperationRow[]>
): CurrentLogin | null {
  const read = lastValue(ops);
  if (!read || read.stale) return null;
  const op = read.value
    .filter(
      (row) =>
        LOGIN_ACTIONS.has(row.action) &&
        (row.state === "queued" || row.state === "running")
    )
    .sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))[0];
  if (!op) return null;
  return {
    state: op.state === "queued" ? "queued" : "running",
    at:
      typeof op.created_at === "number" && Number.isFinite(op.created_at)
        ? op.created_at * 1000
        : null,
    action: op.action,
  };
}
export type UmState = {
  backend: Word;
  auth: Word;
  at: number | null;
  check: StatusCheck | null;
  observation: AuthObservation | null;
  /** Only the STATUS read is stale: backend/auth/at are then last values, not
   *  current readings. A stale operations read never makes a fresh status
   *  reading stale. */
  stale: boolean;
  /** Only the OPERATIONS read is stale: check/observation/currentLogin are dated
   *  history. It never downgrades a fresh status read's current auth word. */
  opsStale: boolean;
  /** True only when THIS status read carries the account layer, so only then is
   *  `auth` a current reading. A historical observation never sets it. */
  authReported: boolean;
  /** The newest historical observation's own time. It is never `last_check_at`,
   *  which stamps the status read, not the auth observation. */
  authAt: number | null;
  /** The only current login progress: a fresh operations read's queued/running
   *  login/re-auth operation. A terminal observation never fills it. */
  currentLogin: CurrentLogin | null;
};
/** UM backend (8110) and its authentication. The status read carries
 *  account {running, valid}; while 8110 is down the backend leaves `account`
 *  out rather than sending running:false, so an absent account is 未上报 and
 *  only the last layered check can say 已停止. `last_check_at` stamps THIS
 *  status read; it is not the time the auth was observed.
 *
 *  A1/R2: `valid` from THIS status read is the only current auth success.
 *  The newest auth observation (an older share.status, login or re-auth result)
 *  is dated historical evidence: it never becomes the current word. A current
 *  `valid=false` is always 无效; a terminal `logging_in`/`needs_login`/`failed`
 *  observation is history and never refines it. R9: current login progress is
 *  only a queued/running login/re-auth operation in a fresh operations read. */
export function umState(
  status: ReadState<ShareStatusPayload>,
  ops: ReadState<OperationRow[]>
): UmState {
  const read = lastValue(status);
  const opsRead = lastValue(ops);
  const check = lastStatusCheck(ops);
  const observation = lastAuthObservation(opsRead?.value ?? []);
  const currentLogin = currentLoginOperation(ops);
  const unknown: Word = {
    tone: "neutral",
    word: status.state === "loading" ? "读取中" : "未知",
  };
  const evidence = {
    check,
    observation,
    authAt: observation?.at ?? null,
    currentLogin,
  };
  if (!read)
    return {
      backend: unknown,
      auth: unknown,
      at: null,
      ...evidence,
      stale: false,
      opsStale: opsRead?.stale === true,
      authReported: false,
    };
  const account = read.value.account;
  const at =
    typeof read.value.last_check_at === "number"
      ? read.value.last_check_at * 1000
      : null;
  const backend: Word =
    account?.running === true
      ? { tone: "ok", word: "运行中" }
      : account?.running === false
        ? { tone: "err", word: "未运行" }
        : { tone: "neutral", word: "未上报" };
  let auth: Word;
  let authReported = false;
  if (account?.running === false) {
    auth = { tone: "neutral", word: "无法检查" };
    authReported = true;
  } else if (account?.valid === true) {
    auth = { tone: "ok", word: "有效" };
    authReported = true;
  } else if (account?.valid === false) {
    authReported = true;
    // R9: the current account layer says invalid. No terminal observation may
    // refine it into logging_in/needs_login/failed; those stay in the history
    // line. A queued/running login op is progress, never the auth word.
    auth = { tone: "warn", word: "无效" };
  } else {
    // No account layer in this read: the current auth is unreported even when a
    // historical observation once said valid.
    auth = { tone: "neutral", word: "未上报" };
  }
  // The status read's staleness and the operations read's staleness are
  // separate: a fresh account.valid=false stays the current 无效 even when the
  // operations read failed and kept an older observation.
  return {
    backend,
    auth,
    at,
    ...evidence,
    stale: read.stale,
    opsStale: opsRead?.stale === true,
    authReported,
  };
}
/** 8110 is down as last seen: the status says so, or it is silent and the last
 *  layered check found the backend stopped. */
export const umDown = (um: UmState) =>
  um.backend.word === "未运行" ||
  (um.backend.word === "未上报" && um.check?.backend === "stopped");

/** The newest historical auth observation as one dated line. It is history, not
 *  the current word: the status read's account layer is the only current
 *  reading, and the observation carries its own time rather than the status
 *  read's `last_check_at`. */
export function authHistoryLine(um: UmState): string {
  const observation = um.observation;
  if (!observation) return "";
  const word = authWord(observation.state, observation.error).word;
  return `历史认证证据：${AUTH_SOURCE_WORD[observation.source]}${observation.at ? ` ${when(observation.at)}` : " 时间未上报"}报告${word}${um.opsStale ? "（读数已过时）" : ""}`;
}

/** Ticket #5: the seven display/auth states the status row renders. A state
 *  word comes only from a fresh display-state read; a missing or stale read is
 *  状态未知, never the last 已登录. */
const DISPLAY_STATE_WORD: Readonly<Record<string, string>> = {
  not_started: "未开始",
  logging_in: "登录中",
  waiting_mfa: "等待 MFA",
  valid: "已登录",
  cancelled: "认证已失效",
  failed: "认证已失效",
  timed_out: "超时",
  display_disconnected: "显示断线",
};

export type ServiceState = {
  tone: Tone;
  word: string;
  /** true = gateway_running and in_sync are both true; false = the read says
   *  the service is not usable; null = one of the two fields is unreadable. */
  available: boolean | null;
};

/** Ticket #5: the service read face is `gateway_running` AND `in_sync`. A field
 *  the read does not carry is unknown, never a reassuring "服务可用". */
export function serviceState(
  status: ReadState<ShareStatusPayload>
): ServiceState {
  const read = lastValue(status);
  if (!read || read.stale)
    return { tone: "neutral", word: "服务状态未知", available: null };
  const { gateway_running, in_sync } = read.value;
  if (gateway_running === true && in_sync === true)
    return { tone: "ok", word: "服务可用", available: true };
  if (gateway_running === false)
    return {
      tone: "err",
      word: "服务异常（API 入口未运行）",
      available: false,
    };
  if (in_sync === false)
    return { tone: "err", word: "服务异常（配置未同步）", available: false };
  return { tone: "neutral", word: "服务状态未知", available: null };
}

export type StatusRow = {
  tone: Tone;
  word: string;
  /** The status read's own time, or the display-state read's when the service
   *  read has no value at all. */
  at: number | null;
  open: boolean;
  actions: "open" | "open-cancel" | "relogin-cancel";
};

/** Ticket #5: the one status line. The word is the display/auth state; the
 *  service read must be current AND both `gateway_running`/`in_sync` must be
 *  known before the word may say anything but 状态未知. The right-side login
 *  actions follow the word. */
export function statusRow(model: CloudModel): StatusRow {
  const displayRead = lastValue(model.displayState);
  const statusRead = lastValue(model.status);
  const service = serviceState(model.status);
  const current =
    displayRead && !displayRead.stale && service.available !== null;
  const word = current
    ? (DISPLAY_STATE_WORD[displayRead.value.state ?? "unknown"] ?? "状态未知")
    : "状态未知";
  // Ticket #5 只给三组状态定了动作。剩下的两种——已登录/未开始与
  // 状态未知——都给「打开登录窗口」：打开窗口是只读动作，不改变认证，
  // 所以未知状态下给它是安全的，也不会把未知说成已登录。
  const actions =
    word === "登录中" || word === "等待 MFA"
      ? "open-cancel"
      : word === "认证已失效" || word === "超时" || word === "显示断线"
        ? "relogin-cancel"
        : "open";
  const tone: Tone =
    word === "已登录" && service.available === true
      ? "ok"
      : service.available === false
        ? "err"
        : word === "认证已失效" || word === "超时" || word === "显示断线"
          ? "err"
          : word === "登录中" || word === "等待 MFA"
            ? "warn"
            : "neutral";
  return {
    tone,
    word,
    at: statusRead?.at ?? displayRead?.at ?? null,
    open: model.statusOpen === true,
    actions,
  };
}

function latestLoginOperation(
  ops: ReadState<OperationRow[]>
): OperationRow | null {
  const read = lastValue(ops);
  if (!read) return null;
  return (
    read.value
      .filter((row) => LOGIN_ACTIONS.has(row.action))
      .sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))[0] ?? null
  );
}

/** Ticket #5: the expanded status row. Four dated evidence lines and the two
 *  read-only actions; the page never promotes history to the current word. */
export function statusDetailHtml(model: CloudModel): string {
  const statusRead = lastValue(model.status);
  const displayRead = lastValue(model.displayState);
  const opsRead = lastValue(model.operations);
  const status = statusRead?.value ?? null;
  const display = displayRead?.value ?? null;
  const account = status?.account ?? null;
  const authWord = !status
    ? "状态读数未返回"
    : account?.running === false
      ? "UM 后端未运行，无法检查认证"
      : account?.valid === true
        ? "认证有效"
        : account?.valid === false
          ? "认证无效"
          : "认证未上报";
  const authStale = statusRead?.stale ? " · 读数已过时，不当作当前状态" : "";
  const displayWord = !display
    ? "认证读数未返回"
    : `${display.label || (DISPLAY_STATE_WORD[display.state ?? "unknown"] ?? "状态未知")}${display.detail ? ` · ${display.detail}` : ""}${displayRead?.stale ? " · 读数已过时" : ""}`;
  const deadline =
    typeof display?.login_window_deadline_epoch === "number"
      ? when(display.login_window_deadline_epoch * 1000)
      : null;
  const displayAt = displayRead?.at ?? null;
  const upstream =
    status?.upstream?.reachable === true
      ? "可达"
      : status?.upstream?.reachable === false
        ? "不可达"
        : "未上报";
  const check = lastStatusCheck(model.operations);
  const checkText = check
    ? `${checkLine(check)}${opsRead?.stale ? "（读数已过时）" : ""}`
    : opsRead
      ? opsRead.stale
        ? "操作读数已过时，无法确认最近一次分层检查"
        : "未读取到最近一次分层检查"
      : "操作读数未返回";
  const login = latestLoginOperation(model.operations);
  const loginText = login
    ? `${LOGIN_ACTION_WORD[login.action] ?? login.action} · ${operationOutcome(login).word}${login.created_at ? ` · 提交于 ${when(login.created_at * 1000)}` : " · 提交时间未上报"}${opsRead?.stale ? " · 读数已过时" : ""}`
    : opsRead
      ? opsRead.stale
        ? "操作读数已过时，无法确认登录动作"
        : "未读取到登录动作"
      : "操作读数未返回";
  const upstreamStale = statusRead?.stale
    ? " · 读数已过时，不当作当前状态"
    : "";
  return `<div class="cloud-status-detail" data-cloud-status-detail>
    <dl class="cloud-status-evidence">
      <div><dt>认证读数</dt><dd>${escapeHtml(displayWord)}${displayAt ? ` · 读数于 ${escapeHtml(when(displayAt))}` : " · 读数时间未上报"}${deadline ? ` · 登录窗口截止 ${escapeHtml(deadline)}（实例自己的窗口，本页不延长）` : ""} · ${escapeHtml(authWord)}${escapeHtml(authStale)}</dd></div>
      <div><dt>上游认证</dt><dd>${escapeHtml(upstream)}${escapeHtml(upstreamStale)} · 可达不等于每次请求都成功${statusRead ? ` · 状态读取于 ${escapeHtml(clock(statusRead.at))}` : ""}</dd></div>
      <div><dt>最近核验</dt><dd>${escapeHtml(checkText)}</dd></div>
      <div><dt>登录动作</dt><dd>${escapeHtml(loginText)}</dd></div>
    </dl>
    <div class="cloud-status-detail-actions">
      ${model.actionButton("share.status", "share", "分层检查（只读）", "btn btn-outline btn-sm")}
      ${model.actionButton("share.backend_reauthenticate", "share", "重新认证 UM", "btn btn-tonal btn-sm")}
    </div>
    <p class="card-foot">打开登录窗口不等于 UM 认证成功，登录/MFA 由本人完成；上游可达不等于每次请求都成功。读数失败后保留的上次值会标为过时，不当作当前状态。</p>
  </div>`;
}

/** Ticket #5: the status line at the top of the single page. */
export function statusRowHtml(model: CloudModel): string {
  const row = statusRow(model);
  const service = serviceState(model.status);
  const at = row.at ? clock(row.at) : "时间未知";
  const open = `<button type="button" class="btn btn-primary btn-sm" data-cloud-login-window-open>打开登录窗口</button>`;
  const cancel = `<button type="button" class="btn btn-outline btn-sm" data-cloud-login-cancel>取消登录</button>`;
  const relogin = model.actionButton(
    "share.backend_login",
    "share",
    "重新登录",
    "btn btn-primary btn-sm"
  );
  const actions =
    row.actions === "open"
      ? open
      : row.actions === "open-cancel"
        ? `${open}${cancel}`
        : `${relogin}${cancel}`;
  return `<section class="cloud-status-row" data-tone="${row.tone}" aria-label="服务状态">
    <span class="cloud-status-dot" data-tone="${row.tone}" aria-hidden="true"></span>
    <button type="button" class="cloud-status-toggle cloud-status-word" data-cloud-status-toggle aria-expanded="${row.open}" title="展开 / 收起状态详情">${escapeHtml(row.word)}</button>
    <span class="cloud-status-service" data-tone="${service.available === false ? "err" : "neutral"}">${escapeHtml(service.word)}</span>
    <span class="cloud-status-time">数据更新于 ${escapeHtml(at)}</span>
    <span class="cloud-status-grow"></span>
    <div class="cloud-status-login">${actions}</div>
    <button type="button" class="btn btn-outline btn-sm" data-cloud-refresh>刷新</button>
    ${row.open ? statusDetailHtml(model) : ""}
  </section>`;
}

/** Ticket #6: the page's one primary action. Its word follows the service
 *  read's `gateway_running`; a start in flight keeps the button in place with
 *  the operation's own stage note, and only a completed start lets the word
 *  become 停止. An unreadable status is unknown, never a guessed 启动. */
export const PRIMARY_START = "share.chain_start";
export const PRIMARY_STOP = "share.chain_stop";
export const PRIMARY_FORCE = "share.chain_estop";

const TERMINAL_OP = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
]);

export type PrimaryAction = {
  kind: "start" | "stop" | "starting";
  word: string;
  /** The chain start's own latest step note while it is in flight. */
  stage: string | null;
  action: typeof PRIMARY_START | typeof PRIMARY_STOP | null;
  disabled: boolean;
  reason: string;
  /** Ticket #6: only a drain-timed-out stop offers 强制停止. */
  force: boolean;
};

function latestChainStart(
  operations: ReadState<OperationRow[]>
): OperationRow | null {
  const read = lastValue(operations);
  // A stale read is history, never current in-flight evidence.
  if (!read || read.stale) return null;
  return (
    read.value
      .filter((row) => row.action === PRIMARY_START)
      .sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))[0] ?? null
  );
}

/** The chain start's own latest step note; the stage text is never invented. */
export function stageNote(
  operation: OperationRow | null | undefined
): string | null {
  const steps = operation?.steps ?? [];
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const note = steps[index]?.note;
    if (typeof note === "string" && note.trim()) return note;
  }
  return null;
}

/** The latest chain stop ended in a drain timeout and no forced stop followed:
 *  only then may the stop dialog offer 强制停止. */
function stopDrainTimedOut(operations: ReadState<OperationRow[]>): boolean {
  const read = lastValue(operations);
  if (!read || read.stale) return false;
  const latest = read.value
    .filter(
      (row) => row.action === PRIMARY_STOP || row.action === PRIMARY_FORCE
    )
    .sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))[0];
  if (!latest || latest.action !== PRIMARY_STOP) return false;
  if (latest.state !== "succeeded" || !latest.result) return false;
  return latest.result.state === "drain_timeout";
}

export function primaryAction(model: CloudModel): PrimaryAction {
  const statusRead = lastValue(model.status);
  const current = statusRead && !statusRead.stale ? statusRead.value : null;
  const pending = model.primaryPending ?? null;
  const inFlight = latestChainStart(model.operations);
  const starting =
    inFlight && !TERMINAL_OP.has(inFlight.state) ? inFlight : null;
  const running =
    current && typeof current.gateway_running === "boolean"
      ? current.gateway_running
      : null;
  // A drain timeout already stopped the gateway but left in-flight requests
  // unconfirmed. The primary action must stay 停止 so its confirm can offer
  // 强制停止 even though `gateway_running` has already flipped to false.
  const force = stopDrainTimedOut(model.operations);
  // A current status read that confirms the service is running is completion
  // evidence, so an older start row still marked running cannot keep 启动中.
  if (running === true) {
    const ready = model.writeReady();
    return {
      kind: "stop",
      word: "停止",
      stage: null,
      action: PRIMARY_STOP,
      disabled: !ready.ok,
      reason: ready.ok ? "" : ready.reason,
      force,
    };
  }
  if (starting || pending)
    return {
      kind: "starting",
      word: "启动中…（等待认证）",
      stage: stageNote(starting) ?? pending?.stage ?? "已提交，等待后端受理",
      action: null,
      disabled: true,
      reason: "启动已受理，等待后端完成；动作受理不是认证成功",
      force: false,
    };
  if (force) {
    const ready = model.writeReady();
    return {
      kind: "stop",
      word: "停止",
      stage: null,
      action: PRIMARY_STOP,
      disabled: !ready.ok,
      reason: ready.ok ? "" : ready.reason,
      force: true,
    };
  }
  if (running === null)
    return {
      kind: "start",
      word: "启动",
      stage: null,
      action: null,
      disabled: true,
      reason: "服务状态未知，无法判断启动还是停止；请刷新后重试",
      force: false,
    };
  const ready = model.writeReady();
  return {
    kind: "start",
    word: "启动",
    stage: null,
    action: PRIMARY_START,
    disabled: !ready.ok,
    reason: ready.ok ? "" : ready.reason,
    force: false,
  };
}

export function primaryActionHtml(model: CloudModel): string {
  const action = primaryAction(model);
  const button = `<button type="button" class="btn ${action.kind === "stop" ? "btn-danger" : "btn-primary"} cloud-primary-button" data-cloud-primary="${action.kind === "starting" ? "starting" : action.kind}"${action.disabled ? ` disabled title="${escapeHtml(action.reason)}"` : ""}>${escapeHtml(action.word)}</button>`;
  const stage = action.stage
    ? `<p class="cloud-primary-stage" data-cloud-primary-stage>${escapeHtml(action.stage)}</p>`
    : "";
  const reason =
    action.disabled && !action.stage && action.reason
      ? `<p class="cloud-primary-note">${escapeHtml(action.reason)}</p>`
      : "";
  return `<section class="cloud-primary-action" aria-label="服务控制">${button}${stage}${reason}</section>`;
}

export type AuthHeadline = { tone: Tone; title: string; detail: string };
/** The current auth headline from the status read, or null when the current
 *  reading needs no issue. A status read without the account layer is 未上报,
 *  never a green 有效 from history; the history stays dated evidence in the
 *  detail. Only a current `valid` is a success. R9: the title says UM 登录进行中
 *  only while a login/re-auth operation is queued/running in a fresh operations
 *  read; a terminal observation is history and never the current progress. */
export function authHeadline(um: UmState): AuthHeadline | null {
  if (um.stale) return null;
  if (
    um.auth.word === "有效" ||
    um.auth.word === "读取中" ||
    um.auth.word === "未知"
  )
    return null;
  if (um.backend.word === "未运行") return null;
  const title = um.currentLogin
    ? "UM 登录进行中"
    : !um.authReported
      ? "UM 认证未上报"
      : `UM 认证${um.auth.word}`;
  const read = um.at ? `状态读取于 ${when(um.at)}` : "状态读取时间未上报";
  const current = um.authReported
    ? `当前认证：${um.auth.word}`
    : "当前认证未上报：这次状态读数没有账号层（后端未上报）";
  const login = um.currentLogin
    ? `当前登录进度：${opStateWord(um.currentLogin.state).word} · ${LOGIN_ACTION_WORD[um.currentLogin.action] ?? um.currentLogin.action}${um.currentLogin.at ? ` · 提交于 ${when(um.currentLogin.at)}` : " · 提交时间未上报"}`
    : "";
  const history = authHistoryLine(um);
  const checked = um.check?.authLabel
    ? `最近分层检查：${um.check.authLabel}`
    : "";
  const notSuccess = um.currentLogin
    ? "当前登录进度不代表认证成功；只有认证读面确认有效才算成功；历史有效不代表当前认证。"
    : "只有认证读面确认有效才算成功；历史有效不代表当前认证。";
  return {
    tone: um.auth.tone === "err" ? "err" : "warn",
    title,
    detail: `${[read, current, login, history, checked].filter(Boolean).join(" · ")}。${notSuccess}`,
  };
}

/** A partial CNY total is a lower bound. "<¥0.01" already means "below
 *  ¥0.01", so it never gets a second "≥" prefix (which would read
 *  ≥<¥0.01). A lower bound below ¥0.01 keeps enough precision to stay
 *  honest (≥¥0.005), and a partial 0 is still 0. */
export function cnyBound(value: number, partial: boolean): string {
  if (!partial || value === 0) return fmtCny(value);
  const text = fmtCny(value);
  if (text.startsWith("<")) return `≥¥${Number(value.toPrecision(2))}`;
  return `≥${text}`;
}

function failedLine(read: ReadState<unknown>, what = ""): string {
  if (read.state !== "failed") return "";
  const time = clock(read.at);
  const kept = read.last
    ? `；下方是 ${clock(read.last.at)} 的上次读数（已过时，当前未知）`
    : "";
  return `<p class="cloud-read-note" data-tone="${read.kind === "no-answer" ? "neutral" : "warn"}">${escapeHtml(`${what}${FAILURE_WORD[read.kind]}（${time}）：${read.reason}${kept}`)}</p>`;
}

function createKeyButton(
  model: CloudModel,
  label: string,
  cls = "btn btn-primary btn-sm"
): string {
  const writable = model.writeReady();
  return `<button type="button" class="${cls}" data-cloud-key-create-open${writable.ok ? "" : ` disabled title="${escapeHtml(writable.reason)}"`}>${svgIcon("key")}${escapeHtml(label)}</button>`;
}

function usageKpis(model: CloudModel): string {
  const usageRead = lastValue(model.usage);
  // A stale read is not a current value: today's figures stay unknown until a
  // fresh read answers, and the note names the stale reading instead of
  // rendering the old value as today's usage.
  const usage = usageRead && !usageRead.stale ? usageRead : null;
  const today = usage ? todayUsage(usage.value) : null;
  const ledger = usage ? ledgerRead(usage.value) : null;
  const totals = usage ? dailyTotals(usage.value) : null;
  // A readable ledger that reports 0 requests with no day rows is a measured
  // empty week, not an unknown one; a null request count stays unknown.
  const week =
    usage && !ledger?.unread
      ? totals?.hasDaily ||
        (usage.value.requests === 0 &&
          !totals?.missingKeys &&
          !totals?.missingUnattributed)
        ? lastDays(7).reduce(
            (sum, day) => sum + (totals?.days.get(day)?.requests ?? 0),
            0
          )
        : null
      : null;
  // An unread ledger answers requests 0 without day rows; that is unknown, not
  // a measured zero, so every usage figure falls back to 未知 with the reason.
  // A stale read is the same: 上次读数 is not today's value.
  const measuredToday =
    usage && !usage.stale && !ledger?.unread && today?.available
      ? today
      : null;
  const usageNote = !usageRead
    ? model.usage.state === "loading"
      ? "用量读取中"
      : model.usage.state === "failed"
        ? `用量${FAILURE_WORD[model.usage.kind]}：${model.usage.reason}；今日用量未知，不用 0 代替`
        : "等待用量读数"
    : usageRead.stale
      ? `用量读数已过时（上次 ${clock(usageRead.at)}）：今日用量未知，不用 0 代替`
      : ledger?.unread
        ? `${ledger.reason}；今日用量未知，不用 0 代替`
        : !today?.available
          ? "后端没有按日明细，今日用量未知，不用 0 代替"
          : "";
  // Ticket #8: today's failures come from the backend's own `result=failed`
  // aggregate read (window=24h). A missing, failed or stale read is 未知,
  // never a fabricated 0.
  const failedRead = lastValue(model.usageFailed);
  let failed: number | null = null;
  let failedNote = "";
  if (!failedRead) {
    failedNote =
      model.usageFailed?.state === "failed"
        ? `失败读数${FAILURE_WORD[model.usageFailed.kind]}：${model.usageFailed.reason}`
        : model.usageFailed?.state === "loading"
          ? "失败读数读取中"
          : "等待失败读数";
  } else if (failedRead.stale) {
    failedNote = `失败读数已过时（上次 ${clock(failedRead.at)}）`;
  } else {
    const failedLedger = ledgerRead(failedRead.value);
    const failedToday = todayUsage(failedRead.value);
    if (failedLedger.unread)
      failedNote = `${failedLedger.reason}；今日失败未知，不用 0 代替`;
    else if (!failedToday.available)
      failedNote = "后端没有按日明细，今日失败未知，不用 0 代替";
    else failed = failedToday.requests;
  }
  const requestValue = measuredToday
    ? measuredToday.requests === null
      ? "未知"
      : fmtNum(measuredToday.requests)
    : "未知";
  const tokenValue = measuredToday
    ? measuredToday.tokens === null
      ? "未知"
      : `${measuredToday.tokensComplete ? "" : "≥"}${fmtTokens(measuredToday.tokens)}`
    : "未知";
  const failedValue = failed === null ? "未知" : fmtNum(failed);
  const costValue = measuredToday
    ? measuredToday.costCny === null
      ? "未知"
      : cnyBound(measuredToday.costCny, measuredToday.costPartial)
    : "未知";
  const weekNote = week === null ? "近 7 天未知" : `近 7 天 ${fmtNum(week)}`;
  const tokenNote =
    measuredToday && !measuredToday.tokensComplete
      ? "今日 Token 含未完整记录，≥ 表示下界"
      : "";
  return `${failedLine(model.usage)}
  <section class="cloud-usage-figures" aria-label="今日用量" data-cloud-usage-figures>
    <div class="cloud-usage-figure" data-cloud-usage-figure="requests">
      <span class="cloud-usage-figure-label">今日请求</span>
      <span class="cloud-usage-figure-value mono" data-cloud-usage-value="requests">${requestValue}</span>
    </div>
    <div class="cloud-usage-figure" data-cloud-usage-figure="tokens">
      <span class="cloud-usage-figure-label">今日 Token</span>
      <span class="cloud-usage-figure-value mono" data-cloud-usage-value="tokens">${tokenValue}</span>
    </div>
    <div class="cloud-usage-figure" data-cloud-usage-figure="failed">
      <span class="cloud-usage-figure-label">今日失败</span>
      <span class="cloud-usage-figure-value mono" data-cloud-usage-value="failed">${failedValue}</span>
    </div>
    <p class="cloud-usage-figure-note">${weekNote} · 北京时间日期${tokenNote ? ` · ${tokenNote}` : ""}${totals?.missingKeys ? ` · ${totals.missingKeys} 个 Key 没有按日明细，合计不含它们` : ""}${totals?.missingUnattributed ? " · 未归属桶没有按日明细，合计不含它" : ""}</p>
    <p class="cloud-usage-figure-note">今日模型参考费用 <span class="mono" data-cloud-usage-cost>${costValue}</span> · 按模型参考价估算，不是云账单${measuredToday?.costPartial ? "；部分记录未完成，实际可能更高" : ""}</p>
    ${usageNote ? `<p class="cloud-usage-figure-note" data-tone="warn">${escapeHtml(usageNote)}</p>` : ""}
    ${usageNote ? `<p class="cloud-usage-figure-note" data-tone="warn">${escapeHtml(usageNote)}</p>` : ""}
    ${failedNote ? `<p class="cloud-usage-figure-note" data-tone="warn">${escapeHtml(failedNote)}</p>` : ""}
  </section>`;
}

type RankRow = {
  id: string;
  label: string;
  sub: string;
  value: number | null;
  missing: boolean;
  drill: string;
};

function rankRows(model: CloudModel): {
  rows: RankRow[];
  note: string;
  scope: string;
} {
  const usage = lastValue(model.usage)?.value;
  if (!usage) return { rows: [], note: "", scope: "" };
  const ui = model.overviewUi;
  if (ui.rank === "model") {
    const buckets = new Map<string, SummaryRow[]>();
    let missingKeys = 0;
    for (const key of usage.keys) {
      if (
        key.models.length === 0 &&
        (key.coverage.requests === null || key.coverage.requests > 0)
      )
        missingKeys += 1;
      for (const row of key.models) {
        const bucket = buckets.get(row.identity) ?? [];
        bucket.push(row);
        buckets.set(row.identity, bucket);
      }
    }
    for (const row of usage.unattributed.models) {
      const bucket = buckets.get(row.identity) ?? [];
      bucket.push(row);
      buckets.set(row.identity, bucket);
    }
    const rows = [...buckets]
      .map(([identity, details]) => {
        const aggregate = aggregateRows(details);
        return {
          id: identity,
          label: identity,
          sub: "全部时段",
          value: aggregate[ui.metric],
          missing: false,
          drill: `model:${identity}`,
        };
      })
      .sort((a, b) => (b.value ?? -1) - (a.value ?? -1))
      .slice(0, 6);
    const missingUnattributed =
      usage.unattributed.models.length === 0 &&
      (usage.unattributed.coverage.requests === null ||
        usage.unattributed.coverage.requests > 0);
    return {
      rows,
      note: `${missingKeys ? `${missingKeys} 个 Key 没有按模型明细，排行不含它们。` : ""}${missingUnattributed ? "未归属桶没有按模型明细，排行不含它。" : ""}模型明细只有全部时段，后端未提供模型×日期；不能与左侧 14 天图同窗。`,
      scope: "全部时段",
    };
  }
  const days = Number(ui.range);
  const window = new Set(lastDays(days));
  const totals = dailyTotals(usage);
  const keyRows = lastValue(model.keys)?.value ?? [];
  const rows: RankRow[] = [];
  for (const key of usage.keys) {
    const missing =
      key.days.length === 0 &&
      (key.coverage.requests === null || key.coverage.requests > 0);
    const aggregate = missing ? null : windowSum(key.days, window);
    rows.push({
      id: key.keyId,
      label: key.keyName ?? key.keyId,
      sub: keyRows.find((row) => row.id === key.keyId)?.masked || key.keyId,
      value: aggregate ? aggregate[ui.metric] : null,
      missing,
      drill: `key:${key.keyId}`,
    });
  }
  const unattributed = usage.unattributed;
  const missing =
    unattributed.days.length === 0 &&
    (unattributed.coverage.requests === null ||
      unattributed.coverage.requests > 0);
  const aggregate = missing ? null : windowSum(unattributed.days, window);
  rows.push({
    id: "unattributed",
    label: "未归属（无法对应到 Key）",
    sub: "单列，不摊到任何 Key",
    value: aggregate ? aggregate[ui.metric] : null,
    missing,
    drill: "key:unattributed",
  });
  rows.sort((a, b) => (b.value ?? -1) - (a.value ?? -1));
  return {
    rows: rows.slice(0, 6),
    note: `${totals.missingKeys ? `${totals.missingKeys} 个 Key 没有按日明细，排行不含它们。` : ""}Key 排行与左侧图表同为近 ${days} 天、同一指标。`,
    scope: `近 ${days} 天`,
  };
}

function rankCard(model: CloudModel): string {
  const usage = lastValue(model.usage);
  const ui = model.overviewUi;
  const rank = rankRows(model);
  const metricWord = ui.metric === "requests" ? "请求" : "Token";
  const value = (row: RankRow) => {
    if (row.missing) return "按日明细未提供";
    const metric = row.value;
    if (metric === null) return "未知";
    return ui.metric === "requests" ? fmtNum(metric) : fmtTokens(metric);
  };
  const head = `<div class="card-head"><h3>${ui.rank === "key" ? "Key 排行" : "模型排行"}</h3><span class="card-meta">${rank.scope} · ${metricWord}</span><span class="card-grow"></span>
    <div class="cloud-seg" role="group" aria-label="排行维度">
      <button type="button" data-cloud-rank="key" aria-pressed="${ui.rank === "key"}">按 Key</button>
      <button type="button" data-cloud-rank="model" aria-pressed="${ui.rank === "model"}">按模型</button>
    </div></div>`;
  if (!usage)
    return `<section class="card cloud-rank" aria-label="排行">${head}${failedLine(model.usage)}</section>`;
  // An absent/unreadable ledger answers requests 0 with no rows; that is not a
  // measured empty ledger, so the card says so instead of "还没有用量记录".
  const ledger = ledgerRead(usage.value);
  if (ledger.unread) {
    return `<section class="card cloud-rank" aria-label="排行">${head}${failedLine(model.usage)}
      <p class="card-foot">账本未读取/不可读（${escapeHtml(ledger.reason)}）：累计请求与排行保持未知，不当作空账本，也不补 0。</p>
    </section>`;
  }
  // Only a measured 0 on every signal is "no usage yet"; a null request
  // count is unknown and is never read as an empty ledger.
  const emptyUsage =
    usage.value.keys.length === 0 &&
    usage.value.unattributed.days.length === 0 &&
    usage.value.unattributed.coverage.requests === 0 &&
    usage.value.requests === 0;
  const unknownUsage =
    usage.value.keys.length === 0 &&
    usage.value.unattributed.days.length === 0 &&
    (usage.value.unattributed.coverage.requests === null ||
      usage.value.requests === null);
  return `<section class="card cloud-rank" aria-label="排行">${head}
    ${emptyUsage ? `<p class="card-foot">还没有用量记录。</p>` : ""}
    ${unknownUsage ? `<p class="card-foot">后端没有返回累计请求数，无法判断是否没有用量；不补 0。</p>` : ""}
    ${
      !emptyUsage && rank.rows.length
        ? `<ol class="cloud-rank-list">${rank.rows
            .map(
              (row, index) => `<li>
      <button type="button" class="cloud-rank-row" data-cloud-drill="${escapeHtml(row.drill)}">
        <span class="cloud-rank-index" aria-hidden="true">${index + 1}</span>
        <span class="cloud-rank-text"><strong>${escapeHtml(row.label)}</strong><small>${escapeHtml(row.sub)}</small></span>
        <span class="cloud-rank-value num">${escapeHtml(value(row))}</span>
      </button></li>`
            )
            .join("")}</ol>`
        : emptyUsage || unknownUsage
          ? ""
          : `<p class="card-foot">后端未提供${ui.rank === "key" ? "按日" : "按模型"}排行明细。</p>`
    }
    <p class="card-foot">${escapeHtml(rank.note)}</p>
  </section>`;
}

/** Ticket #10: the test result stays in the row's own button for a few
 *  seconds: 测试 → 测试中… → 通过 · 耗时 / 失败 · 原因 → 测试. The
 *  elapsed time is the backend's own measurement; a missing one is 未知. */
function testElapsedText(ms: number | undefined): string {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0)
    return "耗时未知";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}
function keyTestLabel(test: KeyTest | undefined): string {
  if (!test) return "测试";
  if (test.state === "busy") return "测试中…";
  if (test.state === "ok") return `通过 · ${testElapsedText(test.elapsedMs)}`;
  return `失败 · ${test.reason || "后端未给出原因"}`;
}
function keyTestTone(test: KeyTest | undefined): string {
  if (!test) return "neutral";
  if (test.state === "busy") return "warn";
  return test.state === "ok" ? "ok" : "err";
}

/** D18-7: one Key row. The name sits above the mask, the mask is the main
 *  visual, the concurrency number is edited in place and 撤销 is the row's only
 *  destructive action. Clicking the row swaps the mask for the full plaintext and
 *  adds 复制; clicking it again collapses. */
function keyRowHtml(model: CloudModel, key: ShareKeyRow): string {
  const id = String(key.id);
  const secret = model.keySecret(id);
  const revealed = secret?.revealed === true;
  const notice = model.keyNotice(id);
  const writable = model.writeReady();
  const capValue =
    model.keyCapValue(id) ??
    (typeof key.max_concurrency === "number"
      ? String(key.max_concurrency)
      : "");
  const disabled = !writable.ok
    ? ` disabled title="${escapeHtml(writable.reason)}"`
    : model.keyBusy(id)
      ? ` disabled title="同一操作正在进行"`
      : "";
  const test = model.keyTest(id);
  const testReason =
    key.enabled !== true
      ? "Key 已停用，无法测试"
      : typeof key.row_version !== "number" || key.row_version < 1
        ? "缺少行版本，请刷新列表"
        : test?.state === "busy"
          ? "同一 Key 已有测试在进行"
          : "";
  const testDisabled = disabled
    ? disabled
    : testReason
      ? ` disabled title="${escapeHtml(testReason)}"`
      : "";
  return `<li class="cloud-key-row" data-cloud-key-row="${escapeHtml(id)}">
    <button type="button" class="cloud-key-main" data-cloud-key-action="reveal:${escapeHtml(id)}" aria-expanded="${revealed}">
      <span class="cloud-key-name">${escapeHtml(key.name || "（未命名）")}</span>
      ${
        revealed
          ? `<code class="cloud-key-plain mono">${escapeHtml(secret?.secret ?? "")}</code>`
          : `<span class="cloud-key-mask mono">${escapeHtml(key.masked || "—")}</span>`
      }
    </button>
    <div class="cloud-key-row-actions">
      ${
        revealed
          ? `<button type="button" class="cloud-key-copy" data-cloud-key-action="copy:${escapeHtml(id)}">复制</button>`
          : ""
      }
      <input class="cloud-key-cap mono" type="number" min="1" max="48" step="1" inputmode="numeric" value="${escapeHtml(capValue)}" data-cloud-key-cap-input="${escapeHtml(id)}" aria-label="Key ${escapeHtml(key.name || id)} 的并发上限"${disabled}>
      <button type="button" class="cloud-key-test-btn" data-cloud-key-action="test:${escapeHtml(id)}" data-tone="${keyTestTone(test)}"${testDisabled}>${escapeHtml(keyTestLabel(test))}</button>
      <button type="button" class="cloud-key-revoke" data-cloud-key-action="revoke:${escapeHtml(id)}"${disabled}>撤销</button>
    </div>
    ${notice ? `<p class="cloud-key-notice" data-tone="${notice.tone}">${escapeHtml(notice.text)}</p>` : ""}
  </li>`;
}

function keysCard(model: CloudModel): string {
  const keys = lastValue(model.keys);
  const statusRead = lastValue(model.status);
  // The Key list header owns the one read-only global concurrency line. A stale
  // status read cannot state a current cap or in-flight count, so both stay
  // unknown and the line says why.
  const status = statusRead && !statusRead.stale ? statusRead.value : null;
  const cap =
    typeof status?.share_max_concurrency === "number"
      ? status.share_max_concurrency
      : null;
  const inFlight =
    typeof status?.capacity?.in_flight === "number"
      ? status.capacity.in_flight
      : null;
  const concurrencyTitle = statusRead?.stale
    ? `状态读数已过时（上次 ${clock(statusRead.at)}），当前并发未知`
    : "当前在途 / 全局配置上限（只读）";
  const concurrency = `并发 ${inFlight === null ? "未上报" : fmtNum(inFlight)} / ${cap === null ? "未上报" : fmtNum(cap)}`;
  const keysStale = keys?.stale === true;
  const keysStaleNote = keysStale
    ? `<p class="cloud-read-note" data-tone="warn">Key 读数已过时（上次 ${clock(keys.at)}）：列表与已启用 Key 数当前未知</p>`
    : "";
  const head = `<div class="card-head"><h3>API Key</h3><span class="card-meta">${keys ? `共 ${keys.value.length} 个${keysStale ? "（读数已过时）" : ""}` : ""}</span><span class="cloud-key-concurrency mono" title="${escapeHtml(concurrencyTitle)}">${escapeHtml(concurrency)}</span><span class="card-grow"></span>
    ${keys && keys.value.length === 0 ? "" : createKeyButton(model, "新建 Key")}</div>`;
  if (!keys) {
    return `<section class="card cloud-keys" aria-label="API Key">${head}${failedLine(model.keys)}${model.keys.state === "loading" ? `<p class="card-foot">读取中…</p>` : ""}</section>`;
  }
  const empty = keys.value.length
    ? ""
    : `<div class="cloud-key-empty"><p>还没有 Key。新建一个长期 Key，只填名字与并发。</p>${createKeyButton(model, "新建 Key")}</div>`;
  return `<section class="card cloud-keys" aria-label="API Key">${head}${failedLine(model.keys)}${keysStaleNote}
    ${keys.value.length ? `<ul class="cloud-key-list">${keys.value.map((key) => keyRowHtml(model, key)).join("")}</ul>` : ""}
    ${empty}
    <p class="card-foot">Key 长期有效。点开一行看完整明文并复制，再点收起；明文只留在当前页面内存，不进入地址栏、浏览器存储或日志。并发为单 Key 配置上限（1–48），改完立即生效。</p>
  </section>`;
}

/** D18-13: daily usage over the selected 7/14/30 Beijing days. One series,
 *  bars anchored at the baseline, a hover / focus tooltip per day and a table for
 *  assistive tech. A day whose Token total is unknown has no bar and says 未知;
 *  it is never painted as a zero. An all-zero window says so instead of drawing
 *  a silent flat line, and a partial day is marked as a lower bound. */
function trendCard(model: CloudModel): string {
  const usage = lastValue(model.usage);
  const ui = model.overviewUi;
  const days = Number(ui.range);
  const metricWord = ui.metric === "requests" ? "请求" : "Token";
  const head = `<div class="card-head"><h3>近 ${days} 天${metricWord}</h3><span class="card-meta">每日 · 全部 Key 与未归属</span><span class="card-grow"></span>
    <div class="cloud-seg" role="group" aria-label="图表时段">
      ${(["7", "14", "30"] as const).map((range) => `<button type="button" data-cloud-range="${range}" aria-pressed="${ui.range === range}">近 ${range} 天</button>`).join("")}
    </div>
    <div class="cloud-seg" role="group" aria-label="趋势指标">
      <button type="button" data-cloud-metric="requests" aria-pressed="${ui.metric === "requests"}">请求</button>
      <button type="button" data-cloud-metric="tokens" aria-pressed="${ui.metric === "tokens"}">Token</button>
    </div></div>`;
  if (!usage)
    return `<section class="card cloud-trend" aria-label="近 ${days} 天用量">${head}${failedLine(model.usage)}${model.usage.state === "loading" ? `<p class="card-foot">读取中…</p>` : ""}</section>`;
  // An absent/unreadable ledger answers requests 0 with no day rows; painting
  // those days as a zero curve would claim a measured empty ledger.
  const ledger = ledgerRead(usage.value);
  if (ledger.unread) {
    return `<section class="card cloud-trend" aria-label="近 ${days} 天用量">${head}${failedLine(model.usage)}
      <p class="cloud-chart-empty">账本未读取/不可读（${escapeHtml(ledger.reason)}）：无法绘制每日趋势，不画全 0 曲线，也不当作空账本。</p>
      <p class="card-foot">请求、Token 与每日桶都保持未知，不补 0。</p>
    </section>`;
  }
  const dayList = lastDays(days);
  const totals = dailyTotals(usage.value);
  if (!totals.hasDaily && (usage.value.requests ?? 0) > 0) {
    return `<section class="card cloud-trend" aria-label="近 ${days} 天用量">${head}
      <p class="cloud-chart-empty">后端未提供按日明细，无法绘制每日趋势；不用 0 补一条假曲线。</p>
      <p class="card-foot">已读取到累计 ${fmtNum(usage.value.requests ?? 0)} 次请求，但没有日期维度。</p>
    </section>`;
  }
  const values = dayList.map(
    (day) =>
      totals.days.get(day) ??
      (totals.missingKeys === 0 && !totals.missingUnattributed
        ? zeroAggregate()
        : null)
  );
  const metricValues = values.map((value) => (value ? value[ui.metric] : null));
  const known = metricValues.filter((value): value is number => value !== null);
  if (!known.length) {
    return `<section class="card cloud-trend" aria-label="近 ${days} 天用量">${head}
      <p class="cloud-chart-empty">后端未提供${metricWord}明细，无法绘制每日趋势。</p>
      <p class="card-foot">${totals.missingKeys ? `${totals.missingKeys} 个 Key 没有按日明细；` : ""}不用 0 补一条假曲线。</p>
    </section>`;
  }
  const max = Math.max(...known);
  // An all-zero window is a measured zero, not a broken chart: say so instead
  // of leaving a silent empty baseline.
  const allZero = max <= 0;
  // 「每天的…都是 0」只在每一天都有读数时成立：有 Key 缺按日明细时，
  // 缺失日会标「无数据」，脚注不能宣称每天都是 0。
  const everyDayZero = allZero && known.length === metricValues.length;
  const top = allZero ? 4 : niceCeil(max);
  const W = ui.chartWidth > 0 ? ui.chartWidth : 320,
    H = 150,
    left = 34,
    right = 6,
    plotTop = 10,
    base = 124;
  const step = (W - left - right) / dayList.length;
  const barW = Math.max(2, step - 2);
  const y = (value: number) => base - (value / top) * (base - plotTop);
  const grid = [0, top / 2, top]
    .map(
      (value) =>
        `<line x1="${left}" x2="${W - right}" y1="${y(value).toFixed(1)}" y2="${y(value).toFixed(1)}" class="cloud-grid-line"/>` +
        `<text x="${left - 6}" y="${(y(value) + 4).toFixed(1)}" text-anchor="end" class="cloud-axis">${ui.metric === "requests" ? fmtNum(value) : fmtTokens(value)}</text>`
    )
    .join("");
  const bars = values
    .map((value, index) => {
      const x = left + index * step + (step - barW) / 2;
      const metric = value ? value[ui.metric] : null;
      const h = metric === null ? 0 : base - y(metric);
      const r = Math.min(4, barW / 2, h);
      const total =
        metric === null
          ? "未知"
          : `${fmtNum(metric)} ${ui.metric === "requests" ? "次请求" : "Token"}`;
      const label =
        `${dayList[index]}${index === dayList.length - 1 ? "（今天）" : ""}：` +
        `${value && value.requests !== null ? `${fmtNum(value.requests)} 次请求` : "请求未知"}，` +
        `Token ${value && value.tokens !== null ? `${value.tokensComplete ? "" : "≥"}${fmtTokens(value.tokens)}` : "未知"}` +
        `；当天合计 ${total}` +
        (metric === null ? `；${metricWord}未提供` : "");
      const mark =
        h > 0
          ? `<path class="cloud-bar" d="M${x.toFixed(1)} ${base}V${(base - h + r).toFixed(1)}q0 -${r.toFixed(1)} ${r.toFixed(1)} -${r.toFixed(1)}h${(barW - 2 * r).toFixed(1)}q${r.toFixed(1)} 0 ${r.toFixed(1)} ${r.toFixed(1)}V${base}z"/>`
          : "";
      // D18-13: a day with no bucket gets a short missing mark at the baseline
      // instead of silently looking like a measured 0.
      const missingMark =
        value === null
          ? `<text x="${(left + index * step + step / 2).toFixed(1)}" y="${base + 12}" text-anchor="middle" class="cloud-axis cloud-axis-missing">无数据</text>`
          : "";
      return `<g class="cloud-bar-slot" tabindex="0" role="img" aria-label="${escapeHtml(label)}" data-tip="${escapeHtml(label)}"><rect class="cloud-bar-hit" x="${(left + index * step).toFixed(1)}" y="${plotTop}" width="${step.toFixed(1)}" height="${base - plotTop}"/>${mark}${missingMark}</g>`;
    })
    .join("");
  const tickEvery = dayList.length > 14 ? 5 : dayList.length > 7 ? 2 : 1;
  const ticks = dayList
    .map((day, index) =>
      index % tickEvery === 0 || index === dayList.length - 1
        ? `<text x="${(left + index * step + step / 2).toFixed(1)}" y="${base + 18}" text-anchor="middle" class="cloud-axis">${index === dayList.length - 1 ? "今天" : day.slice(5).replace("-", "/")}</text>`
        : ""
    )
    .join("");
  const undated = undatedRequests(usage.value);
  const undatedText = !undated.reported
    ? ""
    : undated.count === null
      ? "另有请求后端无法确定日期；数量未上报。"
      : undated.count > 0
        ? `另有 ${fmtNum(undated.count)} 次请求后端无法确定日期，计入累计但不在图中。`
        : "";
  const partialDays = values.filter(
    (value) => value !== null && value.costPartial === true
  ).length;
  const lowerBound =
    partialDays > 0
      ? `有 ${partialDays} 天的记录不完整，${metricWord}是下界（≥）。`
      : "";
  const allZeroNote = everyDayZero
    ? `所选时段内每天的${metricWord}都是 0：这是已读取到的 0，不是读取失败。`
    : "";
  const table = `<table class="sr-only"><caption>近 ${days} 天每日${metricWord}</caption><thead><tr><th>日期</th><th>请求</th><th>Token</th></tr></thead><tbody>${dayList
    .map((day, index) => {
      const value = values[index];
      return `<tr><td>${day}</td><td>${value ? (value.requests === null ? "未知" : value.requests) : "未知"}</td><td>${value ? (value.tokens === null ? "未知" : value.tokens) : "未知"}</td></tr>`;
    })
    .join("")}</tbody></table>`;
  return `<section class="card cloud-trend" aria-label="近 ${days} 天${metricWord}">${head}${failedLine(model.usage)}
    <svg class="cloud-chart" viewBox="0 0 ${W} ${H}" aria-hidden="false" role="group" aria-label="近 ${days} 天每日${metricWord}柱状图">${grid}${bars}${ticks}</svg>${table}
    <p class="card-foot">按北京时间日期汇总；${totals.missingKeys || totals.missingUnattributed ? `${totals.missingKeys ? `有 ${totals.missingKeys} 个 Key ` : ""}${totals.missingUnattributed ? "未归属桶 " : ""}没有按日明细，图中不补 0。` : "有日期桶但没有记录的日子为 0。"}${undatedText}${lowerBound}${allZeroNote}${usage.stale ? ` 读数时间 ${clock(usage.at)}（已过时）。` : ""}</p>
  </section>`;
}
const REQUEST_RESULT_WORD: Readonly<Record<string, string>> = {
  success: "成功",
  failed: "失败",
  cancelled: "已取消",
  unknown: "结果未确认",
};

/** Ticket #9: the ledger records `duration_ms` per request; an old row without it
 *  stays unknown instead of inventing a number. Token is input + output when both
 *  are recorded, otherwise unknown. */
function requestTokenText(row: ShareUsageRequestRow): string {
  if (
    typeof row.inputTokens !== "number" ||
    typeof row.outputTokens !== "number"
  )
    return "未知";
  return fmtNum(row.inputTokens + row.outputTokens);
}

/** Ticket #9: the request-detail list with its result/time filters and
 *  cursor-backed pages. Every count on this card is computed from the loaded page
 *  only; it never claims to be the whole ledger. A revoked or deleted Key keeps
 *  its historical name with the「（已删除）」suffix. */
function requestsCard(model: CloudModel): string {
  const read = model.usageRequests;
  const ui = model.usageRequestsUi;
  const loading = read === null || read.state === "loading";
  const stale = read?.state === "failed" ? lastValue(read) : null;
  const loaded =
    read?.state === "ready"
      ? read.value
      : read?.state === "failed"
        ? (stale?.value ?? null)
        : null;
  const rows = loaded?.rows ?? [];
  const meta = loading
    ? "读取中"
    : read.state === "ready"
      ? `本页 ${fmtNum(rows.length)} 条`
      : stale
        ? `上次读数 ${fmtNum(rows.length)} 条（已过时）`
        : "读取失败";
  const resultButtons = (
    [
      ["all", "全部"],
      ["success", "成功"],
      ["failed", "失败"],
      ["unknown", "结果未确认"],
      ["cancelled", "已取消"],
    ] as const
  )
    .map(([id, word]) => {
      const active = id === "all" ? ui.result === "" : ui.result === id;
      return `<button type="button" data-cloud-requests-result="${id}" aria-pressed="${active}">${word}</button>`;
    })
    .join("");
  const windowButtons = (
    [
      ["today", "今天"],
      ["7", "近 7 天"],
      ["30", "近 30 天"],
    ] as const
  )
    .map(
      ([id, word]) =>
        `<button type="button" data-cloud-requests-window="${id}" aria-pressed="${ui.time === id}">${word}</button>`
    )
    .join("");
  const counts = { success: 0, failed: 0, unknown: 0, cancelled: 0 };
  for (const row of rows) counts[row.result] += 1;
  const failure =
    read?.state === "failed"
      ? `<p class="cloud-read-note" data-tone="warn">请求明细${FAILURE_WORD[read.kind]}：${escapeHtml(read.reason)}${stale ? `；下方是 ${clock(stale.at)} 的上次读数（已过时）` : ""}</p>`
      : "";
  const table = loading
    ? `<p class="cloud-empty" data-cloud-requests-loading>请求明细读取中…</p>`
    : read?.state === "failed" && !stale
      ? `<p class="cloud-empty">请求明细读取失败，当前无法判断是否有请求；不用空结果代替。</p>`
      : rows.length
        ? `<div class="cloud-table-scroll"><table class="data-table cloud-request-table">
        <caption>请求明细（本页 ${fmtNum(rows.length)} 条，按账本从新到旧）</caption>
        <thead><tr><th>时间</th><th>Key</th><th>模型</th><th>结果</th><th>耗时</th><th>Token</th><th>失败原因</th></tr></thead>
        <tbody>${rows
          .map((row) => {
            const name = row.keyName ?? row.keyId ?? "未归属";
            const key = `${escapeHtml(name)}${row.deleted === true ? "（已删除）" : ""}`;
            const result = REQUEST_RESULT_WORD[row.result] ?? "结果未确认";
            const reason =
              row.status ?? (row.result === "failed" ? "后端未给出原因" : "—");
            const duration =
              typeof row.durationMs === "number"
                ? `${fmtNum(row.durationMs)} ms`
                : "未上报";
            return `<tr data-cloud-request-row="${escapeHtml(row.requestId ?? "unknown")}" data-cloud-request-result="${row.result}">
          <td class="mono">${escapeHtml(row.time ?? "日期未知")}</td>
          <td>${key}</td>
          <td>${escapeHtml(row.model ?? "未知")}</td>
          <td>${result}${row.status ? `<span class="cloud-key-mask">${escapeHtml(row.status)}</span>` : ""}</td>
          <td class="mono">${duration}</td>
          <td>Token ${requestTokenText(row)}</td>
          <td>${escapeHtml(reason)}</td>
        </tr>`;
          })
          .join("")}</tbody></table></div>`
      : `<p class="cloud-empty">当前筛选没有请求；计数只覆盖已加载页。</p>`;
  const pageIndex = ui.page;
  const nextCursor = loaded?.nextCursor ?? null;
  const hasPage = loaded !== null;
  const pager = `<div class="cloud-pager">
      <button type="button" class="btn btn-outline btn-sm" data-cloud-requests-page="-1"${pageIndex <= 0 ? " disabled" : ""}>上一页</button>
      <span>第 ${fmtNum(pageIndex + 1)} 页 · 本页 ${fmtNum(rows.length)} 条</span>
      <button type="button" class="btn btn-outline btn-sm" data-cloud-requests-page="1"${nextCursor ? "" : " disabled"}>下一页</button>
    </div>`;
  const retry =
    read?.state === "failed"
      ? `<div class="cloud-pager"><button type="button" class="btn btn-outline btn-sm" data-cloud-requests-retry>重试</button></div>`
      : "";
  return `<section class="card cloud-requests" data-cloud-requests aria-label="请求明细">
    <div class="card-head"><h3>请求明细</h3><span class="card-meta">${meta}</span></div>
    <div class="cloud-filters" role="group" aria-label="请求筛选">
      <div class="cloud-seg" role="group" aria-label="结果筛选">${resultButtons}</div>
      <div class="cloud-seg" role="group" aria-label="时间范围">${windowButtons}</div>
    </div>
    ${failure}
    ${table}
    ${hasPage ? `<p class="cloud-scope-note">本页：成功 ${fmtNum(counts.success)} · 失败 ${fmtNum(counts.failed)} · 结果未确认 ${fmtNum(counts.unknown)} · 已取消 ${fmtNum(counts.cancelled)}。计数只统计已加载页（${fmtNum(rows.length)} 条），不是全部历史。</p>` : ""}
    ${hasPage ? pager : ""}
    ${retry}
    ${loaded?.truncated === true ? `<p class="cloud-scope-note" data-tone="warn">读取窗口已截断，更旧记录未读入；翻页只覆盖已读窗口。</p>` : ""}
    <p class="card-foot">请求明细来自分享账本，按账本从新到旧分页；结果未确认表示账本没有记录结果，不并入成功。耗时来自账本的 duration_ms；旧记录没有该字段时显示「未上报」，不补 0。</p>
  </section>`;
}

function niceCeil(value: number): number {
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const n = value / magnitude;
  const nice = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  return Math.max(2, nice * magnitude);
}

/** Ticket #4: the services page and its server actions are gone. The only
 *  remaining server actions are the two read-only ones, kept in one fold until
 *  the state row owns them. */
function advancedFold(model: CloudModel): string {
  return `<details class="cloud-advanced" data-cloud-advanced data-cloud-evidence>
    <summary>高级维护（分层检查与重新认证）</summary>
    <div class="cloud-actions-row">
      ${model.actionButton("share.status", "share", "分层检查（只读）", "btn btn-outline btn-sm")}
      ${model.actionButton("share.backend_reauthenticate", "share", "重新认证 UM…", "btn btn-tonal btn-sm")}
    </div>
  </details>`;
}

export function overviewHtml(model: CloudModel): string {
  // Ticket #4: one page. The old hero, issue list and layered evidence fold
  // were the services page's content and are no longer rendered. The order is
  // the spec's own (spec.md:92): 状态行、主动作按钮、Key 列表、今日用量数字、
  // 趋势、请求明细、高级折叠。
  return `${statusRowHtml(model)}
    ${primaryActionHtml(model)}
    ${keysCard(model)}
    ${usageKpis(model)}
    <div class="cloud-trend-grid">${trendCard(model)}${rankCard(model)}</div>
    ${requestsCard(model)}
    ${advancedFold(model)}`;
}
