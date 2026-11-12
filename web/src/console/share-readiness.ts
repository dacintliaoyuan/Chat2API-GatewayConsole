/** Presentation of independent share-chain observations. No I/O or probes. */
import type {
  PublicShareBudget,
  PublicShareSnapshot,
  PublicDnsDetail,
  PublicDnsState,
} from "./backend/types.ts";

export type ShareReadinessInput = {
  gateway_running?: boolean;
  funnel_on?: boolean;
  in_sync?: boolean;
  share_url?: string | null;
  account?: { running?: boolean; valid?: boolean } | null;
};

export type ShareKeyTest = {
  rowVersion: number;
  state: "busy" | "ok" | "error";
  reason: string;
  startedAt: number;
  at?: number;
  stage?: string;
  model?: string;
  elapsedMs?: number;
  publicUrl?: string;
  publicIp?: string;
  dns?: PublicDnsDetail | null;
};

export type ShareReading = {
  state: "ok" | "warn" | "danger" | "idle";
  text: string;
};
const reading = (state: ShareReading["state"], text: string): ShareReading => ({
  state,
  text,
});

/** DNS-only anomalies are page evidence. They never become a confirmed TLS or
 *  model failure, and a different valid address set is not an anomaly. */
export const PUBLIC_DNS_ANOMALIES: ReadonlySet<PublicDnsState> = new Set([
  "empty",
  "nxdomain",
  "error",
  "timeout",
  "disagreement",
  "unconfirmed",
]);

export function publicDnsAnomaly(
  dns: PublicDnsDetail | null | undefined
): boolean {
  return Boolean(dns && PUBLIC_DNS_ANOMALIES.has(dns.state));
}

const PUBLIC_DNS_TEXT: Record<PublicDnsState, string> = {
  ok: "DNS 解析正常",
  empty: "DNS 无 A 记录",
  nxdomain: "DNS 域名不存在（NXDOMAIN）",
  error: "DNS 查询失败",
  timeout: "DNS 查询超时",
  disagreement: "DNS 解析分歧",
  unconfirmed: "DNS 源未确认",
  not_checked: "DNS 未检查",
};

export function publicDnsText(dns: PublicDnsDetail | null | undefined): string {
  if (!dns) return "DNS 未提供";
  return PUBLIC_DNS_TEXT[dns.state] ?? "DNS 状态未知";
}

export function publicDnsSuffix(
  dns: PublicDnsDetail | null | undefined
): string {
  if (!dns) return "";
  if (publicDnsAnomaly(dns)) return ` · ${publicDnsText(dns)}`;
  if (dns.state === "not_checked") return " · DNS 未检查";
  return "";
}

export function publicDnsTtlText(
  dns: PublicDnsDetail | null | undefined,
  now = Date.now() / 1000
): string {
  if (!dns || dns.state === "not_checked") return "TTL 未提供";
  const remaining = (ttl: number): number | null => {
    if (dns.checkedAt === null) return null;
    return Math.max(0, ttl - Math.max(0, now - dns.checkedAt));
  };
  const ttlRemaining =
    dns.ttlSeconds === null ? null : remaining(dns.ttlSeconds);
  const negativeRemaining =
    dns.negativeTtlSeconds === null ? null : remaining(dns.negativeTtlSeconds);
  const ttlText =
    dns.ttlSeconds === null
      ? null
      : ttlRemaining === null
        ? "TTL 剩余未知（缺采样时间）"
        : ttlRemaining <= 0
          ? "TTL 已到期，等待新检查"
          : `TTL 剩余约 ${Math.ceil(ttlRemaining)} s`;
  const negativeText =
    dns.negativeTtlSeconds === null
      ? null
      : negativeRemaining === null
        ? "负缓存剩余未知（缺采样时间）"
        : negativeRemaining <= 0
          ? "负缓存已到期，等待新检查"
          : `负缓存剩余约 ${Math.ceil(negativeRemaining)} s`;
  if (dns.state === "nxdomain" || dns.state === "empty") {
    return negativeText ?? "TTL 未提供";
  }
  if (ttlText && negativeText) return `${ttlText} · ${negativeText}`;
  return ttlText ?? negativeText ?? "TTL 未提供";
}

export function latestShareOperationWasStop(
  operations: ReadonlyArray<{
    action: string;
    state: string;
    created_at: number;
  }>
): boolean {
  const latest = operations
    .filter((op) =>
      ["share.start", "share.stop", "share.estop", "share.retry_stop"].includes(
        op.action
      )
    )
    .sort((a, b) => b.created_at - a.created_at)[0];
  return latest?.state === "succeeded" && latest.action !== "share.start";
}

export function localShareReadiness(
  payload: ShareReadinessInput
): ShareReading {
  const checks = [
    payload.gateway_running,
    payload.account?.running,
    payload.account?.valid,
    payload.in_sync,
  ];
  if (checks.some((v) => v === false)) return reading("warn", "未就绪");
  if (checks.every((v) => v === true)) return reading("ok", "已就绪");
  return reading("idle", "未确认");
}

const publicTestMatchesAddress = (
  payload: ShareReadinessInput,
  test: ShareKeyTest
): boolean => {
  const address = (s?: string | null) => s?.replace(/\/$/, "");
  return (
    Boolean(payload.share_url) &&
    address(test.publicUrl) === address(payload.share_url)
  );
};
const publicTestIsStale = (test: ShareKeyTest, now: number): boolean =>
  typeof test.at !== "number" ||
  !Number.isFinite(test.at) ||
  now < test.at ||
  now - test.at > 300;

/** Summary stays independent of local readiness and only reflects a dated test. */
export function publicShareSummary(
  payload: ShareReadinessInput,
  test?: ShareKeyTest,
  now = Date.now() / 1000
): ShareReading {
  if (!test || !publicTestMatchesAddress(payload, test))
    return reading("idle", "公网未验证");
  if (test.state === "busy") return reading("idle", "公网测试中");
  if (publicTestIsStale(test, now)) return reading("warn", "公网结果已陈旧");
  const dns = publicDnsSuffix(test.dns);
  const dnsWarn =
    publicDnsAnomaly(test.dns) || test.dns?.state === "not_checked";
  if (test.state === "ok") {
    return reading(dnsWarn ? "warn" : "ok", "公网已回复（测试时）" + dns);
  }
  return reading("warn", "公网测试失败" + dns);
}

/** A public test is a dated sample, never current availability. Expired samples
 *  stay visible with their age; another address cannot borrow their success. */
export function publicShareReadings(
  payload: ShareReadinessInput,
  test?: ShareKeyTest,
  now = Date.now() / 1000
): { dns: ShareReading; model: ShareReading } {
  const unknown = {
    dns: reading("idle", "未测试"),
    model: reading("idle", "未测试"),
  };
  if (!test) return unknown;
  if (!publicTestMatchesAddress(payload, test)) {
    return {
      dns: reading("idle", "当前地址未测试"),
      model: reading("idle", "当前地址未测试"),
    };
  }
  if (test.state === "busy")
    return { dns: reading("idle", "测试中"), model: reading("idle", "测试中") };
  const stale = publicTestIsStale(test, now);
  const suffix = stale ? "（已陈旧）" : "（测试时）";
  const dnsAnomaly = publicDnsAnomaly(test.dns);
  const dns = dnsAnomaly
    ? reading(stale ? "warn" : "warn", `${publicDnsText(test.dns)}${suffix}`)
    : test.dns?.state === "not_checked"
      ? reading("warn", `DNS 未检查${suffix}`)
      : test.publicIp
        ? reading(stale ? "warn" : "ok", `已解析${suffix}`)
        : test.stage === "dns" ||
            test.reason === "public_dns_failed" ||
            test.reason === "dns_failed"
          ? reading("warn", `解析未通过${suffix}`)
          : reading("idle", "未确认");
  const model =
    test.state === "ok"
      ? reading(
          stale || dnsAnomaly ? "warn" : "ok",
          `已回复${suffix}${dnsAnomaly ? publicDnsSuffix(test.dns) : ""}`
        )
      : test.reason === "tls_handshake_failed"
        ? reading("warn", `公网 TLS 握手失败，未进入模型推理${suffix}`)
        : test.stage === "inference"
          ? reading("warn", `调用未通过${suffix}`)
          : test.stage === "models"
            ? reading("warn", `模型目录检查失败，未进入模型推理${suffix}`)
            : reading("warn", `未进入模型推理${suffix}`);
  return { dns, model };
}

const publicLocalTime = (value: string, now: number): string => {
  const epoch = Date.parse(value);
  if (!Number.isFinite(epoch)) return "未提供";
  if (epoch / 1000 <= now) return "已到时间，等待下一轮";
  return new Date(epoch).toLocaleString("zh-CN", { hour12: false });
};

/** Two normal 60 s rounds: older evidence is stale, never "normal". */
export const PUBLIC_SUPERVISION_STALE_S = 120;

/** A snapshot without a dated observation cannot prove the current state.
 *  ``nextCheckAt`` past its grace also marks a missed scheduled round. */
export function publicSupervisionStale(
  snapshot: PublicShareSnapshot | null,
  now = Date.now() / 1000
): boolean {
  if (!snapshot) return false;
  const observed = snapshot.observedAt;
  if (typeof observed !== "number" || !Number.isFinite(observed)) return true;
  if (now - observed > PUBLIC_SUPERVISION_STALE_S) return true;
  const next = snapshot.round?.nextCheckAt;
  if (typeof next === "string") {
    const epoch = Date.parse(next) / 1000;
    if (Number.isFinite(epoch) && now > epoch + PUBLIC_SUPERVISION_STALE_S)
      return true;
  }
  return false;
}

/** S5 public supervision card readings. Presentation only: no request, probe or
 *  clock; a missing snapshot is 「未提供」 and never a healthy default. A snapshot
 *  older than two normal rounds is stale: it is downgraded, never 「正常」. */
export function publicSupervisionReading(
  snapshot: PublicShareSnapshot | null,
  now = Date.now() / 1000,
  cancelled = false
): ShareReading {
  if (!snapshot) return reading("idle", "未提供");
  if (cancelled) return reading("idle", "巡检已暂停");
  if (snapshot.intent?.state === "stopped")
    return reading("idle", "分享已停止");
  const stale = publicSupervisionStale(snapshot, now);
  const staleSuffix = stale ? " · 结果陈旧" : "";
  const dnsSuffix = publicDnsSuffix(snapshot.dns);
  const dnsAnomaly = publicDnsAnomaly(snapshot.dns);
  switch (snapshot.overall) {
    case "ok":
      if (stale) return reading("warn", "结果陈旧（未按周期巡检）");
      if (snapshot.dns?.state === "not_checked")
        return reading("warn", "正常 · DNS 未检查");
      return reading("ok", "正常" + dnsSuffix);
    case "degraded":
      if (dnsAnomaly) {
        return reading(
          "warn",
          `部分可用（${publicDnsText(snapshot.dns)}）` + staleSuffix
        );
      }
      if (snapshot.dns?.state === "not_checked") {
        return reading("warn", "部分可用（DNS 未检查）" + staleSuffix);
      }
      return reading("warn", "部分可用（降级）" + staleSuffix);
    case "all_failed":
      if (snapshot.recovery?.state === "manual_required")
        return reading("danger", "全部入口失败 · 需人工" + staleSuffix);
      if (snapshot.recovery?.state === "budget_exhausted")
        return reading("danger", "全部入口失败 · 预算耗尽" + staleSuffix);
      return reading("danger", "全部入口失败" + staleSuffix + dnsSuffix);
    case "incomplete":
      if (snapshot.dns?.addressesTruncated) {
        return reading("warn", "未检查完（地址覆盖不完整）" + staleSuffix);
      }
      return reading("idle", "未检查完" + staleSuffix);
    case "not_checked":
      if (dnsAnomaly) {
        return reading("warn", publicDnsText(snapshot.dns) + staleSuffix);
      }
      if (snapshot.dns?.state === "not_checked") {
        return reading("warn", "DNS 未检查" + staleSuffix);
      }
      return reading("idle", "未检查" + staleSuffix);
    default:
      return reading("idle", "未知" + staleSuffix);
  }
}

/** Public TLS reading derived ONLY from the supervision snapshot's per-entry
 *  DNS/TLS/certificate results and its overall verdict. A manual model test is a
 *  dated model sample and is never promoted to public TLS evidence here; a live
 *  mapping or a reachable port is not TLS evidence either. Pure: no request,
 *  probe or clock of its own (``now`` is supplied by the caller). */
export function publicTlsReading(
  snapshot: PublicShareSnapshot | null,
  cancelled = false,
  now = Date.now() / 1000
): ShareReading {
  if (!snapshot) return reading("idle", "公网未检查");
  if (cancelled) return reading("idle", "巡检已暂停");
  if (snapshot.intent?.state === "stopped")
    return reading("idle", "公网已关闭");
  const entries = snapshot.entries ?? [];
  const failed = entries.filter(
    (entry) =>
      entry.dns === "failed" ||
      entry.tls === "failed" ||
      entry.certificate === "failed"
  );
  const dnsAnomaly = publicDnsAnomaly(snapshot.dns);
  const dnsSuffix = publicDnsSuffix(snapshot.dns);
  if (snapshot.overall === "all_failed")
    return reading("danger", "公网入口失败" + dnsSuffix);
  if (snapshot.dns?.addressesTruncated) {
    return reading("warn", "部分入口未检查（地址超过 4 个）");
  }
  if (failed.length) return reading("warn", "部分入口异常" + dnsSuffix);
  if (dnsAnomaly) {
    const allTlsOk =
      entries.length > 0 &&
      entries.every(
        (entry) => entry.tls === "ok" && entry.certificate === "ok"
      );
    return allTlsOk
      ? reading("warn", `公网 TLS 通过 · ${publicDnsText(snapshot.dns)}`)
      : reading("warn", publicDnsText(snapshot.dns));
  }
  if (snapshot.overall === "degraded") return reading("warn", "部分入口异常");
  if (publicSupervisionStale(snapshot, now)) return reading("warn", "结果陈旧");
  const allChecked =
    entries.length > 0 &&
    entries.every(
      (entry) =>
        entry.dns === "ok" && entry.tls === "ok" && entry.certificate === "ok"
    );
  if (snapshot.dns?.state === "not_checked") {
    return allChecked
      ? reading("warn", "公网 TLS 通过 · DNS 未检查")
      : reading("warn", "DNS 未检查");
  }
  if (snapshot.overall === "ok" && allChecked)
    return reading("ok", "公网 TLS 通过");
  return reading("idle", "公网未检查");
}

export function publicRecoveryReading(
  snapshot: PublicShareSnapshot | null,
  cancelled = false
): ShareReading {
  if (!snapshot) return reading("idle", "未提供");
  if (cancelled) return reading("idle", "已暂停");
  if (snapshot.intent?.state === "stopped") return reading("idle", "已停止");
  const recovery = snapshot.recovery;
  if (!recovery) return reading("idle", "未提供");
  switch (recovery.state) {
    case "idle":
      return reading("idle", "未在恢复");
    case "pending":
      return reading("warn", "恢复中（准备）");
    case "running":
      return reading(
        "warn",
        `恢复中（${recovery.stage && recovery.stage !== "none" ? recovery.stage : "进行中"}）`
      );
    case "succeeded":
      return reading("ok", "已恢复");
    case "failed":
      return reading("danger", "恢复未确认");
    case "manual_required":
      return reading("danger", "需人工");
    case "budget_exhausted":
      return reading("danger", "预算耗尽");
    case "stopped":
      return reading("idle", "已停止");
    default:
      return reading("idle", "未知");
  }
}

type PublicBudgetState =
  | { state: "missing" }
  | { state: "unreadable" }
  | { state: "unconfirmed" }
  | { state: "incomplete" }
  | {
      state: "readable";
      mappingUsed: number;
      mappingLimit: number;
      tsRestartUsed: number;
      tsRestartLimit: number;
    };

/** Classify the budget before any count is rendered or retry is enabled.
 *  Only an explicit `unreadable:false` plus all four counts is readable; a
 *  missing flag or a missing count is unknown, never a readable 0/0 budget. */
function publicBudgetState(
  budget: PublicShareBudget | null
): PublicBudgetState {
  if (!budget) return { state: "missing" };
  if (budget.unreadable === true) return { state: "unreadable" };
  if (budget.unreadable !== false) return { state: "unconfirmed" };
  const mappingUsed = budget.mappingUsed;
  const mappingLimit = budget.mappingLimit;
  const tsRestartUsed = budget.tsRestartUsed;
  const tsRestartLimit = budget.tsRestartLimit;
  if (
    typeof mappingUsed !== "number" ||
    !Number.isFinite(mappingUsed) ||
    typeof mappingLimit !== "number" ||
    !Number.isFinite(mappingLimit) ||
    typeof tsRestartUsed !== "number" ||
    !Number.isFinite(tsRestartUsed) ||
    typeof tsRestartLimit !== "number" ||
    !Number.isFinite(tsRestartLimit)
  ) {
    return { state: "incomplete" };
  }
  return {
    state: "readable",
    mappingUsed,
    mappingLimit,
    tsRestartUsed,
    tsRestartLimit,
  };
}

export function publicBudgetText(budget: PublicShareBudget | null): string {
  if (!budget) return "预算未提供";
  const state = publicBudgetState(budget);
  if (state.state === "unreadable") return "预算不可读，不自动恢复";
  if (state.state === "unconfirmed")
    return "预算未提供（不可读标记未提供），不自动恢复";
  if (state.state === "incomplete")
    return "预算未提供（次数不完整），不自动恢复";
  if (state.state === "missing") return "预算未提供";
  const windowSeconds = budget.windowSeconds;
  const windowText =
    typeof windowSeconds !== "number" || !Number.isFinite(windowSeconds)
      ? "窗口未提供"
      : `${Math.round(windowSeconds / 60)} 分钟`;
  let text =
    `映射 ${state.mappingUsed}/${state.mappingLimit}` +
    ` · TS 重启 ${state.tsRestartUsed}/${state.tsRestartLimit}` +
    ` · 窗口 ${windowText}`;
  if (
    state.mappingUsed >= state.mappingLimit &&
    state.tsRestartUsed >= state.tsRestartLimit
  ) {
    const candidates = [
      budget.mappingNextEligibleAt,
      budget.tsRestartNextEligibleAt,
    ]
      .filter(
        (value): value is string =>
          typeof value === "string" && Number.isFinite(Date.parse(value))
      )
      .sort((a, b) => Date.parse(a) - Date.parse(b));
    text += candidates.length
      ? ` · 下次可重试 ${new Date(candidates[0]).toLocaleString("zh-CN", { hour12: false })}`
      : " · 下次可重试时间未提供";
  }
  return text;
}

/** Budget-side reason that disables 受限重试; null means the budget alone does
 *  not block the retry. Unknown readability or missing counts always block and
 *  are never folded into 0/0. */
export function publicBudgetRetryReason(
  budget: PublicShareBudget | null
): string | null {
  const state = publicBudgetState(budget);
  if (state.state === "missing") return "预算未提供，无法确认剩余次数";
  if (state.state === "unreadable") return "预算不可读，不自动恢复";
  if (state.state === "unconfirmed")
    return "预算未提供（不可读标记未提供），不自动恢复";
  if (state.state === "incomplete")
    return "预算未提供（次数不完整），无法确认剩余次数";
  return state.mappingUsed >= state.mappingLimit &&
    state.tsRestartUsed >= state.tsRestartLimit
    ? "预算耗尽，等待窗口滑出"
    : null;
}

/** The next check/action time, using the snapshot's own round times. A past
 *  time reads 「已到时间，等待下一轮」; a missing or unparseable one reads
 *  「未提供」 rather than a fabricated schedule. */
export function publicNextActionText(
  snapshot: PublicShareSnapshot | null,
  now = Date.now() / 1000
): string {
  if (!snapshot?.round) return "未提供";
  const action = snapshot.round.nextActionAt;
  const check = snapshot.round.nextCheckAt;
  const value = action ?? check;
  if (!value) return "未提供";
  const label = action ? "动作" : "检查";
  const epoch = Date.parse(value) / 1000;
  if (!Number.isFinite(epoch)) return "未提供";
  if (epoch <= now) return `已到时间，等待下一轮（${label}）`;
  return `${label} ${publicLocalTime(value, now)}`;
}
