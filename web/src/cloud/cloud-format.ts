import { escapeHtml } from "../html";
import type { CostDetail, ShareUsage } from "../console/backend/types";
import {
  FAILURE_WORD,
  lastValue,
  type ReadState,
} from "./cloud-reads";
import type { Tone } from "./model";

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
export const zeroAggregate = (): UsageAggregate => ({
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

export function failedLine(read: ReadState<unknown>, what = ""): string {
  if (read.state !== "failed") return "";
  const time = clock(read.at);
  const kept = read.last
    ? `；下方是 ${clock(read.last.at)} 的上次读数（已过时，当前未知）`
    : "";
  return `<p class="cloud-read-note" data-tone="${read.kind === "no-answer" ? "neutral" : "warn"}">${escapeHtml(`${what}${FAILURE_WORD[read.kind]}（${time}）：${read.reason}${kept}`)}</p>`;
}
