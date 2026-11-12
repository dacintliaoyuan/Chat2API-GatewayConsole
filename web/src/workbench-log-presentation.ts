export type LogKind = "error" | "warning" | "success" | "info" | "unknown";
export const logKindLabels: Record<LogKind, string> = {
  error: "错误 / 异常",
  warning: "警告",
  success: "通过",
  info: "信息",
  unknown: "未分类",
};

/** Severity wins over an outcome. INFO alone is never proof of success. */
export function logKind(row: {
  level?: string;
  parse_ok: boolean;
  event?: { outcome?: string | null } | null;
}): LogKind {
  const level = row.level?.toUpperCase();
  const outcome = row.event?.outcome?.toLowerCase();
  if (
    ["ERROR", "CRITICAL", "FATAL", "BUG"].includes(level ?? "") ||
    ["error", "failed", "failure"].includes(outcome ?? "")
  )
    return "error";
  if (["WARN", "WARNING"].includes(level ?? "")) return "warning";
  if (["ok", "success", "passed"].includes(outcome ?? "")) return "success";
  if (row.parse_ok && ["INFO", "DEBUG", "TRACE"].includes(level ?? ""))
    return "info";
  return "unknown";
}

/** Missing samples retain a distinct, non-numeric marker at the baseline. */
export function chartBaselineKind(
  value: number | null | undefined
): "zero" | "missing" | null {
  return value === 0 ? "zero" : value == null ? "missing" : null;
}

/** Dates and hours remain separate lines, including both sides of midnight. */
export function axisLabelLines(label: string): string[] {
  const match = /^(.+?)[ T](\d{2}(?::\d{2})?)$/.exec(label);
  return match
    ? [match[1]!, match[2]!.includes(":") ? match[2]! : `${match[2]}:00`]
    : [label];
}
/** Wide plots scroll locally rather than dropping time samples. */
export function timeAxisWidth(labels: string[], base: number): number {
  const longest = Math.max(
    0,
    ...labels.flatMap(axisLabelLines).map((line) => line.length)
  );
  return Math.max(
    base,
    Math.max(0, labels.length - 1) * Math.max(56, longest * 7 + 18) + 48
  );
}

/** 归一化时间戳的形状（与后端 console_log_reader.normalize_ts 同口径）：生产者实际
 *  写出的 `YYYY-MM-DD HH:MM:SS[,fff]`、`YYYY-MM-DDTHH:MM:SS[.fff][Z|±HH:MM]`、
 *  `YYYY/MM/DD HH:MM:SS[.fff]` 与 `YYYY/MM/DD 周X HH:MM:SS[.fff]`（启动器
 *  `%DATE% %TIME%`）。 */
const LOG_TS_RE =
  /^(\d{4})[-/](\d{2})[-/](\d{2})(?:\s*周[一二三四五六日])?[ T](\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d+))?\s*(Z|[+-]\d{2}:?\d{2})?$/;

/** 归一化时间：把一条日志的原始时间文本折算成可比较的 epoch 秒，无法识别（含缺席）
 *  返回 null——未知时间绝不猜算。与后端 `console_log_reader.normalize_ts` 同口径，
 *  因为各来源的原始文本混用空格逗号格式与 ISO `T`/时区格式：按原始字符串排序会把
 *  同日凌晨的 ISO 行排到晚上日志之前（P3/R3 反例）。
 *
 *  不带时区的时间戳按**本地时间**解释（生产者就是这么写的）；带时区的时间戳按该
 *  时区换算，并回读同一时区校验日期分量——月 13 / 日 32 这类不存在的日期在后端
 *  是 None，这里也不得借 JS 日期回卷算成另一天。 */
export function logTsEpoch(ts: string | null | undefined): number | null {
  if (typeof ts !== "string") return null;
  const text = ts.trim();
  if (/^\d{10}$/.test(text)) return Number(text);
  const match = text.match(LOG_TS_RE);
  if (!match) return null;
  const year = Number(match[1]),
    month = Number(match[2]),
    day = Number(match[3]);
  const hour = Number(match[4]),
    minute = Number(match[5]),
    second = Number(match[6]);
  // 小数部分取前 6 位（后端 `int((fraction + "000000")[:6])`）：".945" 与 ".9456789"
  // 都按 6 位微秒取值，不四舍五入。
  const fraction = (match[7] ?? "").padEnd(6, "0").slice(0, 6);
  const ms = fraction ? Number(fraction) / 1000 : 0;
  if (
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > 31 ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  )
    return null;
  const zone = match[8];
  const offsetMinutes = !zone
    ? null
    : zone === "Z"
      ? 0
      : (zone[0] === "-" ? -1 : 1) *
        (Number(zone.slice(1, 3)) * 60 + Number(zone.slice(-2)));
  if (offsetMinutes === null) {
    const local = new Date(year, month - 1, day, hour, minute, second, ms);
    const exact =
      local.getFullYear() === year &&
      local.getMonth() === month - 1 &&
      local.getDate() === day &&
      local.getHours() === hour &&
      local.getMinutes() === minute &&
      local.getSeconds() === second;
    return exact ? local.getTime() / 1000 : null;
  }
  const epoch =
    Date.UTC(year, month - 1, day, hour, minute, second, ms) -
    offsetMinutes * 60000;
  const back = new Date(epoch + offsetMinutes * 60000);
  const exact =
    back.getUTCFullYear() === year &&
    back.getUTCMonth() === month - 1 &&
    back.getUTCDate() === day &&
    back.getUTCHours() === hour &&
    back.getUTCMinutes() === minute &&
    back.getUTCSeconds() === second;
  return exact ? epoch / 1000 : null;
}
