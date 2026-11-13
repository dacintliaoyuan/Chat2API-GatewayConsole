import { escapeHtml } from "../../html";
import type {
  Capability,
  Failure,
  FailureKind,
  Load,
  Reading,
  SpeedBasis,
} from "./contract";

/** Shared wording and number shapes of the UM console. Every instant is shown
 *  in Beijing time whatever the browser's own zone is. */

const ZONE = "Asia/Shanghai";
const clockFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: ZONE,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});
const shortFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: ZONE,
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
const dayFmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

export const esc = escapeHtml;

/** Controller data is typed, not trusted: an instant Intl cannot format (NaN
 *  from a failed parse, out of Date range) reads 「时间未知」 instead of
 *  throwing, so one bad field never takes the whole page down. */
export const TIME_UNKNOWN = "时间未知";
const validInstant = (ms: number) =>
  typeof ms === "number" && Number.isFinite(ms) && Math.abs(ms) <= 8.64e15;
const DAY_KEY = /^\d{4}-(\d{2})-(\d{2})$/;

/** Beijing day key ("YYYY-MM-DD") of an instant; "" when it is not one. */
export function dayKey(ms: number): string {
  return validInstant(ms) ? dayFmt.format(ms) : "";
}
export function clock(ms: number): string {
  return validInstant(ms) ? clockFmt.format(ms) : TIME_UNKNOWN;
}
/** Today: 「10:42」; other days: 「10/31 10:42」. */
export function when(ms: number, now = Date.now()): string {
  if (!validInstant(ms)) return TIME_UNKNOWN;
  const key = dayKey(ms);
  return key === dayKey(now)
    ? shortFmt.format(ms)
    : `${monthDay(key)} ${shortFmt.format(ms)}`;
}
/** 「10/31」 from a day key. */
export function monthDay(key: string): string {
  const m = DAY_KEY.exec(String(key));
  return m ? `${Number(m[1])}/${Number(m[2])}` : "日期未知";
}
/** 「11月1日」 from a day key. */
export function dayTitle(key: string): string {
  const m = DAY_KEY.exec(String(key));
  return m ? `${Number(m[1])}月${Number(m[2])}日` : "日期未知";
}

export function full(n: number): string {
  return Math.round(n).toLocaleString("zh-CN");
}
/** Token counts: exact below 1 万, then 万 / 亿 with three significant figures. */
export function tokens(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 1e8) return `${trim(n / 1e8)}亿`;
  if (abs >= 1e4) return `${trim(n / 1e4)}万`;
  return full(n);
}
function trim(n: number): string {
  const digits = Math.abs(n) >= 100 ? 0 : Math.abs(n) >= 10 ? 1 : 2;
  return n.toFixed(digits).replace(/\.0+$|(\.\d*?)0+$/, "$1");
}
export function duration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} 毫秒`;
  const s = ms / 1000;
  if (s < 60)
    return `${s < 10 ? s.toFixed(1).replace(/\.0$/, "") : Math.round(s)} 秒`;
  const m = Math.floor(s / 60);
  return `${m} 分 ${Math.round(s - m * 60)} 秒`;
}
export function speedNum(n: number): string {
  return n < 10 ? n.toFixed(1).replace(/\.0$/, "") : String(Math.round(n));
}
export function speed(n: number): string {
  return `${speedNum(n)} token/秒`;
}
export function chars(n: number): string {
  return `${tokens(n)} 字符`;
}

export const SPEED_BASIS: Readonly<Record<SpeedBasis, string>> = {
  whole_request: "整次平均，含等待",
  generation: "生成阶段实测",
};
export function speedBasisWord(basis: SpeedBasis): string {
  return wordOf(SPEED_BASIS, basis, "口径未知");
}

/** A closed-set word for a value the contract names. A value outside the set
 *  (a controller passing a raw backend string) gets the fallback rather than
 *  `undefined` — the page shows it is unknown instead of failing to render. */
export function wordOf<K extends string, V>(
  table: Readonly<Record<K, V>>,
  key: K,
  fallback: V
): V {
  return Object.hasOwn(table, key) ? table[key] : fallback;
}

const QUALIFIER_WORD = {
  unreported: "未报告",
  unknown: "未知",
  missing: "缺失",
} as const;

/** A reading as text: ≥ for a lower bound, ≈ for an estimate, and the three
 *  absent kinds in words (`dash` writes them as 「—」 with the word kept in
 *  the title, for dense tables). Never 0 for an absent figure. */
export function readingHtml(
  r: Reading,
  fmt: (n: number) => string,
  dash = false
): string {
  const note = "note" in r && r.note ? r.note : "";
  switch (r.kind) {
    case "exact":
      return esc(fmt(r.value));
    case "atLeast":
      return `<span title="${esc(note || "下界：部分记录缺这一项，真实值不低于此")}">≥${esc(fmt(r.value))}</span>`;
    case "estimate":
      return `<span title="${esc(note || "估算值")}">≈${esc(fmt(r.value))}</span>`;
    default: {
      const word = wordOf(QUALIFIER_WORD, r.kind, "未知");
      const title = note ? `${word}：${note}` : word;
      return dash
        ? `<span class="um-absent" title="${esc(title)}" aria-label="${esc(title)}">—</span>`
        : `<span class="um-absent" title="${esc(title)}">${word}</span>`;
    }
  }
}
/** The numeric value a chart may draw: exact, lower bound or estimate. */
export function drawable(r: Reading): number | null {
  return r.kind === "exact" || r.kind === "atLeast" || r.kind === "estimate"
    ? r.value
    : null;
}
export function readingText(r: Reading, fmt: (n: number) => string): string {
  switch (r.kind) {
    case "exact":
      return fmt(r.value);
    case "atLeast":
      return `≥${fmt(r.value)}`;
    case "estimate":
      return `≈${fmt(r.value)}`;
    default:
      return wordOf(QUALIFIER_WORD, r.kind, "未知");
  }
}

export const FAILURE_WORD: Readonly<Record<FailureKind, string>> = {
  unauthenticated: "未认证",
  refused: "无权读取",
  unavailable: "后端未提供",
  error: "读取失败",
  "no-answer": "未得到应答",
};
export function failureWord(kind: FailureKind): string {
  return wordOf(FAILURE_WORD, kind, "读取失败");
}
export function failureText(f: Failure): string {
  return `${failureWord(f.kind)}：${f.reason}`;
}

/** The data a section can show (live or stale), or null. */
export function loaded<T>(
  load: Load<T>
): { data: T; at: number; stale: Failure | null; refreshing: boolean } | null {
  if (load.state === "ready")
    return {
      data: load.data,
      at: load.at,
      stale: null,
      refreshing: !!load.refreshing,
    };
  if (load.state === "stale")
    return {
      data: load.data,
      at: load.at,
      stale: load.failure,
      refreshing: !!load.refreshing,
    };
  return null;
}

/** The block a section shows instead of data: unwired, first read, failure. */
export function placeholderHtml(load: Load<unknown>, what: string): string {
  switch (load.state) {
    case "unwired":
      return `<p class="um-placeholder" data-um-state="unwired"><span class="pill">未接线</span>${esc(what)}的数据接口尚未接入；接入前这里不显示任何数据。</p>`;
    case "loading":
      return `<p class="um-placeholder" data-um-state="loading"><span class="um-spinner" aria-hidden="true"></span>正在读取${esc(what)}…</p>`;
    case "failed":
      return `<p class="um-placeholder" data-um-state="failed" data-tone="err"><span class="pill" data-tone="err">${esc(failureWord(load.failure.kind))}</span>${esc(what)}没有读到：${esc(load.failure.reason)}（${esc(when(load.failure.at))}）</p>`;
    default:
      return "";
  }
}
/** 「更新于 10:42」, or the stale warning that keeps the last good data. */
export function freshnessHtml(
  at: number,
  stale: Failure | null,
  refreshing = false
): string {
  if (stale)
    return `<span class="um-stale" data-tone="warn" title="${esc(failureText(stale))}"><span class="dot" aria-hidden="true"></span>沿用 ${esc(when(at))} 的数据 · ${esc(failureWord(stale.kind))}于 ${esc(when(stale.at))}</span>`;
  return `<span class="um-fresh">更新于 ${esc(when(at))}${refreshing ? " · 正在刷新" : ""}</span>`;
}

/** Why a capability-bound control is closed, or null when it is open. */
export function blockReason(cap: Capability, busy = false): string | null {
  return !cap.enabled
    ? cap.reason
    : busy
      ? "同一操作正在进行，等待它结束"
      : null;
}
/** A button bound to a capability. A closed one keeps its reason as a hover
 *  hint; pages that matter also print the reason next to it. */
export function capButton(
  cap: Capability,
  attrs: string,
  cls: string,
  label: string,
  busy = false
): string {
  const block = blockReason(cap, busy);
  return `<button type="button" class="${cls}" ${attrs}${block ? ` disabled title="${esc(block)}"` : ""}>${label}</button>`;
}
