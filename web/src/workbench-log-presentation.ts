export type LogKind = "error" | "warning" | "success" | "info" | "unknown";
export const logKindLabels: Record<LogKind, string> = {
  error: "错误 / 异常", warning: "警告", success: "通过", info: "信息", unknown: "未分类",
};

/** Severity wins over an outcome. INFO alone is never proof of success. */
export function logKind(row: { level?: string; parse_ok: boolean; event?: { outcome?: string | null } | null }): LogKind {
  const level = row.level?.toUpperCase();
  const outcome = row.event?.outcome?.toLowerCase();
  if (["ERROR", "CRITICAL", "FATAL", "BUG"].includes(level ?? "") || ["error", "failed", "failure"].includes(outcome ?? "")) return "error";
  if (["WARN", "WARNING"].includes(level ?? "")) return "warning";
  if (["ok", "success", "passed"].includes(outcome ?? "")) return "success";
  if (row.parse_ok && ["INFO", "DEBUG", "TRACE"].includes(level ?? "")) return "info";
  return "unknown";
}

/** Missing samples retain a distinct, non-numeric marker at the baseline. */
export function chartBaselineKind(value: number | null | undefined): "zero" | "missing" | null {
  return value === 0 ? "zero" : value == null ? "missing" : null;
}

/** Dates and hours remain separate lines, including both sides of midnight. */
export function axisLabelLines(label: string): string[] {
  const match = /^(.+?)[ T](\d{2}(?::\d{2})?)$/.exec(label);
  return match ? [match[1]!, match[2]!.includes(":") ? match[2]! : `${match[2]}:00`] : [label];
}
/** Wide plots scroll locally rather than dropping time samples. */
export function timeAxisWidth(labels: string[], base: number): number {
  const longest = Math.max(0, ...labels.flatMap(axisLabelLines).map(line => line.length));
  return Math.max(base, Math.max(0, labels.length - 1) * Math.max(56, longest * 7 + 18) + 48);
}
