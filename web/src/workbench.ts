import { rollText, patchRollingPanel } from "./workbench-rolling";
import { escapeHtml } from "./html";
import {
  logKind,
  logKindLabels,
  logTsEpoch,
  chartBaselineKind,
  timeAxisWidth,
  axisLabelLines,
  type LogKind,
} from "./workbench-log-presentation";
type ConsoleProperties = Record<string, { value: unknown }>;
import { fmtTokens } from "./console/ui";
import {
  encodeSyncScope,
  normalizeContextEvidence,
  normalizeContexts,
  normalizeGatewayConnection,
  normalizePublicDnsDetail,
  normalizeShareUsage,
} from "./console/backend/normalize.ts";
import type {
  KeyUsage,
  ShareUsageBucket,
  CostDetail,
  ContextEvidence,
  ContextView,
  ModelSettings,
  TokenUsage,
  GatewayConnection,
  ReadResult,
  PublicShareStatus,
} from "./console/backend/types.ts";
import {
  BackendClient,
  GatewayConnectionBinding,
  type GatewayConnectionView,
} from "./console/backend/client.ts";
import { createFetchTransport } from "./console/backend/transport.ts";
import {
  copyGatewayConnectionField,
  type GatewayConnectionCopyField,
} from "./console/backend/clipboard.ts";
import {
  GatewayOrderPreferences,
  reorderGatewaySubset,
} from "./console/backend/preferences.ts";
import {
  PaneWidthPreferences,
  clampPaneWidth,
  paneWidthSpec,
  paneWidthAfterMove,
} from "./console/backend/pane-width.ts";
import { readShareJson } from "./console/share-json-read.ts";
import {
  shareReadReason,
  type ShareKeyRow,
  type ShareStatusPayload,
} from "./console/share-model.ts";
import {
  localShareReadiness,
  publicShareReadings,
  publicShareSummary,
  latestShareOperationWasStop,
  type ShareKeyTest,
  publicSupervisionReading,
  publicSupervisionStale,
  publicTlsReading,
  publicRecoveryReading,
  publicBudgetText,
  publicBudgetRetryReason,
  publicNextActionText,
  publicDnsAnomaly,
  publicDnsText,
  publicDnsTtlText,
} from "./console/share-readiness.ts";
import type { ProbeResult } from "./live";
import "./workbench.css";
import {
  actionLabel,
  stateDot,
  stateText,
  summarizeResult,
  isVisibleState,
  shareRecoveryHint,
  type FeedbackEntry,
  type FeedbackState,
} from "./console/operation-feedback";
import {
  defaultWorkbenchVisibility,
  applyVisibilityProperties,
  type WorkbenchVisibility,
} from "./workbench-visibility";

/** Lane names of the gateway console workbench (top-bar navigation, PR11). */
const names = ["总览", "网关", "操作记录", "费用", "日志", "模型", "分享"];
/** Navigation groups (2026-09-29, kept in the PR11 top bar): 运行 = what keeps the
 *  gateway usable, 数据 = what it recorded, 对外 = what it exposes. 操作记录
 *  (lane 2) stays behind the log lane's section tabs, as before. */
const NAV_GROUPS: readonly { label: string; lanes: readonly number[] }[] = [
  // Rev17: 账号 and 模型 are one 网关 workspace (lane 1); lane 5 survives only as
  // an alias that opens the workspace's 全部模型 view (see `select`).
  { label: "运行", lanes: [0, 1] },
  { label: "数据", lanes: [3, 4] },
  { label: "对外", lanes: [6] },
];
// R9-R32 page-head eyebrows (B-end CRM reference): one honest scope line
// above each lane title; the title below renders larger and lighter.
const LANE_CAPTIONS = [
  "用量趋势 · 网关运行 · 待处理",
  "网关列表 · 账号与模型 · 客户端同步",
  "8001 操作账本 · 失败样本",
  "真实账本聚合 · 价目对齐 · 调用明细",
  "运行日志 · 按来源查看",
  "模型目录与同步 · 多维度变更",
  "分享网关 · 开放状态 · 密钥与容量",
];

/** ONE format per unit across the whole console (contract 3.8 / 3.9). The same
 *  unit used to appear with three precisions on one screen (积分 as 27 / 26.97 /
 *  26.967) and money printed without thousands separators next to request
 *  counts that had them. Every ¥ figure and every credit figure now goes through
 *  these two helpers, so the KPI, the ledger tables and the trend notes cannot
 *  drift apart again. */
const fmtCny = (v: number): string =>
  `¥${v.toLocaleString("zh-CN", { minimumFractionDigits: 3, maximumFractionDigits: 3 })}`;
const fmtCredits = (v: number): string =>
  v.toLocaleString("zh-CN", {
    minimumFractionDigits: 3,
    maximumFractionDigits: 3,
  });
/** 单次均价（维护者 2026-10-19，chat2api 模型费用统计）：新单位「¥/请求」，
 *  自己的精度只此一处。总额 3 位小数（fmtCny）下 ¥0.0024 会读成 ¥0.002、
 *  ¥0.0253 读成 ¥0.025，单次均价因此用 4 位小数。 */
const fmtCnyPerRequest = (v: number): string =>
  `¥${v.toLocaleString("zh-CN", { minimumFractionDigits: 4, maximumFractionDigits: 4 })}`;
/** 缓存率（维护者 2026-10-19，chat2api 模型费用统计）：缓存输入 ÷ 输入 token，
 *  一位小数百分比，与控制台其他百分比（deltaChip）同一精度。输入为 0 时不给
 *  百分比（「—」），绝不折成 0%。 */
const fmtCacheRate = (cache: number, input: number): string =>
  input > 0 ? `${((cache / input) * 100).toFixed(1)}%` : "—";
/** 节点金额四态（后端 `_round_service`，usagesrv.py:903-913 / :1018-1040 明写
 *  「costCnyPartial=true 表示 costCny 只含可折算的记录（下界，不是总额）；只有整
 *  节点金额全部无法折算时 costCny=null（未知，绝不折 0、绝不按现价）」）：
 *  · absent  = 该行没有 ¥ 分量（积分链行，按既有的「—」显示）；
 *  · unknown = 整节点金额全部无法折算，显示「未知」；
 *  · partial = costCny 只含可折算记录，是下界——读数必须带下界标识；
 *  · full    = 完整金额，真实零也是完整值（¥0.000）。
 *  把 partial 印成确定总额、把 null 折成 0 再显示 ¥0.000，都是这一条要修的地方。 */
type CostAmount = {
  kind: "absent" | "unknown" | "partial" | "full";
  value?: number;
};
const costAmount = (
  v: number | null | undefined,
  partial?: unknown
): CostAmount => {
  if (v === undefined) return { kind: "absent" };
  if (v === null)
    return partial === true ? { kind: "partial" } : { kind: "unknown" };
  return { kind: partial === true ? "partial" : "full", value: v };
};
/** 单值金额文本：partial 必须带下界标识（≥），null+partial（可折算记录为空）写
 *  「未知（下界仅含可折算部分）」，绝不出现不带 ≥ 的确定总额。 */
const fmtCostAmount = (a: CostAmount): string =>
  a.kind === "absent"
    ? "—"
    : a.kind === "unknown"
      ? "未知"
      : a.kind === "partial"
        ? a.value === undefined
          ? "未知（下界仅含可折算部分）"
          : `≥${fmtCny(a.value)}`
        : fmtCny(a.value!);
/** 第一阅读层读数元素的文本：零 = ¥0.000 一字不差（真实零与「未知」分得开），
 *  partial = ≥¥…（带下界标识），unknown = 未知（不含 ¥），无分量 = —。 */
const costAmountReading = (a: CostAmount | undefined): string =>
  !a || a.kind === "absent"
    ? "—"
    : a.kind === "unknown"
      ? "未知"
      : a.kind === "partial"
        ? a.value === undefined
          ? "未知"
          : `≥${fmtCny(a.value)}`
        : fmtCny(a.value!);
/** 读数元素右侧的小字标记（读数的兄弟节点，不进读数文本）：partial 写「已知」
 *  （accent 色，与「未知」的 muted 不撞色），null+partial 写「下界仅含可折算」，
 *  真实零写「确为 0」；其余状态无标记。读数与标记同排（`.wb-reading` 行），
 *  8px 间距由该行的 column-gap 给出，这里不再自带 margin-left。
 *  Plan-Revision 9 ③（维护者 2026-09-28）：标记文案收短——总览 KPI 费用格在
 *  partial 态下读数盒只有 265.5px，长金额夹具（≥¥1,234,567.836，28px）加
 *  「已知部分」四个字放不下，标记会被挤到下一行、读数文本在数字内部折行，
 *  读数卡高 143.44px > 136px（门禁 ③ 实测，1600x900 双主题 partial 态）。
 *  标记收成「已知」/「下界」两个字，完整口径进 title：字号、读数、语义都不动。 */
const costAmountMarker = (a: CostAmount | undefined): string => {
  if (!a) return "";
  const style =
    "display:inline-block;margin:0;font-size:12.5px;letter-spacing:.06em";
  if (a.kind === "partial") {
    const bound = a.value === undefined;
    const text = bound ? "下界" : "已知";
    const title = bound
      ? "该读数是下界：只含可折算记录，且可折算部分为空"
      : "已知部分：该读数是下界，只含可折算记录";
    return `<small class="wb-muted" data-wb-cost-marker data-wb-cost-bound title="${title}" style="${style};color:var(--theme-accent)">${text}</small>`;
  }
  if (a.kind === "full" && a.value === 0) {
    return `<small class="wb-muted" data-wb-cost-marker style="${style};color:var(--theme-ink)">确为 0</small>`;
  }
  return "";
};
/** 读数槽的状态标记（data-wb-cost-state）：检查脚本按它定位下界读数。 */
const costAmountState = (a: CostAmount | undefined): string =>
  !a || a.kind === "absent" ? "absent" : a.kind;
/** partial 表格「费用」列的解释性 title（维护者 2026-09-28，OD 决定稿「决定 3」）：
 *  「各网关费用」与「模型费用统计」两表的 partial 行已印 ≥，悬停再给
 *  一句「已知部分，含不可折算记录」；整节点全不可折算（partial 且无可折算值，
 *  读数已是「未知（下界仅含可折算部分）」）不给——那里没有已知部分，给
 *  「已知部分」会是假话；完整值（含真实零）与「—」都不给。 */
const costAmountBoundTitleAttr = (a: CostAmount): string =>
  a.kind === "partial" && a.value !== undefined
    ? ` title="已知部分，含不可折算记录"`
    : "";
/** 逐行取整后相加: the printed rows are rounded to 3 decimals, so the printed
 *  total must be built from those SAME rounded values. Summing the raw values and
 *  rounding once at the end is what made the KPI (¥18128.072) differ from the
 *  rows it summarises (¥18128.073) by the rounding residue. */
const sumRounded3 = (values: number[]): number =>
  values.reduce((sum, value) => sum + Math.round(value * 1000) / 1000, 0);
/** Whole-token unit: one precision per unit, same as fmtCny/fmtCredits above. */
const fmtRequests = (v: number): string => v.toLocaleString("zh-CN");

/** C4 日志原文身份：内容摘要（两个独立 32 位混合，约 64 位），不依赖列表位置。
 *  没有可信 request_id 的行走不了请求关联，所以身份只由「时间戳 + 全文」导出。 */
function logEventDigest(text: string): string {
  let a = 0x811c9dc5,
    b = 0x9e3779b9;
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    a = Math.imul(a ^ code, 0x01000193) >>> 0;
    b = (Math.imul(b ^ code, 0x85ebca6b) ^ (b >>> 13)) >>> 0;
  }
  return `${a.toString(16).padStart(8, "0")}${b.toString(16).padStart(8, "0")}`;
}
/** 一次渲染内的事件身份：内容摘要 + 相同内容中的出现序号。重复时间戳 / 重复
 *  内容仍能分开，且不随列表下标（后台刷新换窗口时会漂移）改变；展开状态与
 *  点击复制都用它，不在点击时按位置重算列表。 */
function logRowIdentities(source: string, rows: LogEntryRow[]): string[] {
  const seen = new Map<string, number>();
  return rows.map((row) => {
    const fingerprint = `${row.ts ?? "unknown"}\u0000${row.message}`;
    const ordinal = seen.get(fingerprint) ?? 0;
    seen.set(fingerprint, ordinal + 1);
    return `${source}:${logEventDigest(fingerprint)}:${ordinal}`;
  });
}

/** One short 口径 note per lane for the ⓘ glyph beside the title (contract
 *  3.2 `wb-info`, indexed by lane id). It carries ONLY the metric definition;
 *  the resident `wb-cost-note` strip (缺失日 N / 价目核对) stays its own
 *  element and is never replaced by this glyph. */
const HEAD_INFO = [
  // 维护者 2026-09-28（Plan-Revision 9 逐页收纳 · 总览）：首屏 KPI 只留「标签 + 读数 +
  // 一行真实状态」，口径长句因此收到这里（ⓘ 详情）：区间口径（近 14 天 = 本期 7 天 + 上期 7 天）、
  // 剩余积分格是实时读数（/api/console/quota-snapshot，只保留最近一次快照、环比不可得）、
  // 各链探针路径在服务明细的每一行「详情」里。
  "口径：/api/summary 全量账本聚合（qoder / chat2api / dsh）；m365 链下线不计入（ADR 0019）；探针证据陈旧只降新鲜度，不判宕机。读数格区间口径：近 14 天 = 本期 7 天 + 上期 7 天（环比按同一窗口两侧的真实读数）；剩余积分格是实时读数（/api/console/quota-snapshot，只保留最近一次快照，环比不可得）；五链探针路径与耗时在服务明细每行的「详情」里。",
  "口径：额度与用量为只读查询，各带自己的状态词与检查时间；未知不折成 0。",
  "口径：操作三态 pending / ok / failed 不合并；只读查询不计入操作账本。",
  "口径：估算费用 = 各链账本真实合计（积分链按积分结算、不计入 ¥ 费用；m365 链排除）；未定价请求不折算为 ¥。",
  "口径：日志为各网关自身账本的有界尾读，未读取的来源如实列出；不以空表冒充零记录。",
  "口径：预览只读不改配置，应用同步才写入受管客户端配置；范围只收窄不扩大。",
  "口径：分享密钥明文只在页面会话内可见；轮换 / 撤销并终止 / 删除各自二次确认。",
];

/** 契约 3.13（图标规范）：**全控制台只有这一张路径表**，键 = 语义名，
 *  值 = 一组 `<path>`。几何统一在 `wbIcon()` 里声明（viewBox 0 0 16 16、
 *  16px、`stroke="currentColor"`、`stroke-width="1.5"`、`fill="none"`、圆头圆角），
 *  因此同一层级的图标不可能各写一套线宽。**不引入图标依赖**：`package.json`
 *  的运行依赖只有 `@kitlangton/rolling-number`，不增加图标依赖。 */
const WB_ICON_PATHS: Record<string, string> = {
  overview: `<path d="M2.6 8.2 8 3.2l5.4 5"/><path d="M4.3 7.4v5.4h7.4V7.4"/>`,
  account: `<circle cx="8" cy="5.6" r="2.4"/><path d="M3.4 13.2c0-2.3 2.1-3.6 4.6-3.6s4.6 1.3 4.6 3.6"/>`,
  ops: `<path d="M4.6 3.2v9.6"/><path d="M4.6 8.4h5.1a1.9 1.9 0 0 1 0 3.8H6.1"/><path d="M11.4 3.2v3.2"/>`,
  cost: `<path d="M8 2.6v10.8"/><path d="M10.6 5.4H6.7a1.6 1.6 0 0 0 0 3.2h2.6a1.6 1.6 0 0 1 0 3.2H5.4"/>`,
  logs: `<path d="M4.2 3.2h7.6v9.6H4.2z"/><path d="M6.1 6h4"/><path d="M6.1 8.4h4"/><path d="M6.1 10.8h2.4"/>`,
  // 问题 1（Plan-Revision 7，OD 决定稿 r7-icons-1.html 的 A 方案，B 的必做配套）：
  // 两枚读不出的图标换成通用几何。settings 原先「圆圈 + 8 条放射细线」必然读成
  // 太阳/亮度，换成**8 齿闭合外圈 + 中心圆孔**。放射细线是红线：判据查
  // `[aria-label="外观设置"]` 的 path 含 `M9.9 8a1.9 1.9`（中心圆孔 r=1.9）且不含
  // `M8 2.4v1.7`（放射线）。
  // Plan-Revision 9 ④（维护者 2026-09-28）：models 换掉 PR7 的**闭合目录轮廓**（折角
  // 那一条线在 20px 下与 logs/ledger 的矩形读起来没有差别），改成**方块模型符号**
  // ——等距立方体：六边形外轮廓 + 顶面两条棱 + 前面一条竖棱，路径只有一个语义来源。
  models: `<path d="M8 2.2 13.8 5.6v4.8L8 13.8 2.2 10.4V5.6z"/><path d="M13.8 5.6 8 9l-5.8-3.4"/><path d="M8 9v4.8"/>`,
  share: `<circle cx="4.6" cy="8" r="1.8"/><circle cx="11.4" cy="4.6" r="1.8"/><circle cx="11.4" cy="11.4" r="1.8"/><path d="M6.2 7.2 9.8 5.4"/><path d="M6.2 8.8l3.6 1.8"/>`,
  settings: `<path d="M12.25 6.24 14.28 6.78 14.28 9.22 12.25 9.76 13.31 11.58 11.58 13.31 9.76 12.25 9.22 14.28 6.78 14.28 6.24 12.25 4.42 13.31 2.69 11.58 3.75 9.76 1.72 9.22 1.72 6.78 3.75 6.24 2.69 4.42 4.42 2.69 6.24 3.75 6.78 1.72 9.22 1.72 9.76 3.75 11.58 2.69 13.31 4.42Z"/><path d="M9.9 8a1.9 1.9 0 1 1-3.8 0 1.9 1.9 0 1 1 3.8 0"/>`,
  refresh: `<path d="M12.8 8a4.8 4.8 0 1 1-1.5-3.5"/><path d="M12.8 3.1v2.9h-2.9"/>`,
  check: `<path d="M3.4 8.5 6.6 11.6 12.6 4.6"/>`,
  // KPI entity chips (PR11): requests = a pulse line, Token = stacked layers.
  pulse: `<path d="M2 8.4h2.6l1.6-4 3 8 1.7-4.4H14"/>`,
  layers: `<path d="M8 2.6 13.6 5.5 8 8.4 2.4 5.5z"/><path d="M2.4 8.2 8 11.1l5.6-2.9"/><path d="M2.4 10.8 8 13.7l5.6-2.9"/>`,
  search: `<circle cx="7.1" cy="7.1" r="3.9"/><path d="M10.1 10.1 13.4 13.4"/>`,
  copy: `<rect x="5.6" y="5.6" width="7.8" height="7.8" rx="1.6"/><path d="M10.4 5.6V4a1.6 1.6 0 0 0-1.6-1.6H4A1.6 1.6 0 0 0 2.4 4v4.8A1.6 1.6 0 0 0 4 10.4h1.6"/>`,
  alert: `<path d="M8 2.4 14.2 13.2H1.8z"/><path d="M8 6.4v3"/><path d="M8 11.1v.2"/>`,
  stop: `<rect x="3.8" y="3.8" width="8.4" height="8.4" rx="1.6"/>`,
  play: `<path d="M5.2 3.2 12.4 8l-7.2 4.8z"/>`,
  restart: `<path d="M3.2 8a4.8 4.8 0 1 1 1.5 3.5"/><path d="M3.2 12.9V10h2.9"/>`,
  external: `<path d="M7.2 3.6H4.6A1.6 1.6 0 0 0 3 5.2v6.2A1.6 1.6 0 0 0 4.6 13h6.2A1.6 1.6 0 0 0 12.4 11.4V9"/><path d="M9.6 3h3.4v3.4"/><path d="M12.8 3.2 8.4 7.6"/>`,
  detail: `<path d="M6.2 3.8 10.4 8l-4.2 4.2"/>`,
  clock: `<circle cx="8" cy="8" r="5.4"/><path d="M8 4.8V8l2.4 1.6"/>`,
  coin: `<circle cx="8" cy="8" r="5.4"/><path d="M10 6.2H6.7a1.4 1.4 0 0 0 0 2.8h2.6a1.4 1.4 0 0 1 0 2.8H6"/>`,
  ledger: `<path d="M3.4 3.4h9.2v9.2H3.4z"/><path d="M6.2 3.4v9.2"/><path d="M8.6 6.2h2.4"/><path d="M8.6 8.6h2.4"/>`,
  filter: `<path d="M2.6 4h10.8"/><path d="M4.8 8h6.4"/><path d="M6.8 12h2.4"/>`,
  plus: `<path d="M8 3.4v9.2"/><path d="M3.4 8h9.2"/>`,
  save: `<path d="M3.4 3.4h7.4l2 2v7.2H3.4z"/><path d="M5.8 3.4v3.4h4.6V3.4"/><path d="M5.8 12.6V9.4h4.4v3.2"/>`,
  trash: `<path d="M3.4 4.6h9.2"/><path d="M5.4 4.6V3.2h5.2v1.4"/><path d="M4.6 4.6l.8 8.2h5.2l.8-8.2"/><path d="M6.8 6.8v4"/><path d="M9.2 6.8v4"/>`,
  eye: `<path d="M1.9 8S4.4 4.2 8 4.2 14.1 8 14.1 8 11.6 11.8 8 11.8 1.9 8 1.9 8z"/><circle cx="8" cy="8" r="1.7"/>`,
  // Brand mark: one hub routing to three upstreams — the gateway itself.
  gateway: `<circle cx="4.2" cy="8" r="1.8"/><circle cx="12" cy="3.8" r="1.4"/><circle cx="12" cy="8" r="1.4"/><circle cx="12" cy="12.2" r="1.4"/><path d="M6 8h4.6"/><path d="M5.8 7.1 10.6 4.4"/><path d="M5.8 8.9l4.8 2.7"/>`,
  // Sidebar collapse: a panel edge plus the direction it folds towards.
  rail: `<rect x="2.4" y="2.8" width="11.2" height="10.4" rx="1.6"/><path d="M6 2.8v10.4"/><path d="M10.4 6.2 8.6 8l1.8 1.8"/>`,
};

/** 契约 3.13（a）：一个语义名 → 一个 16×16 内联 SVG，几何只有这一处。
 *  装饰性图标 `aria-hidden="true"`（先例 `workbench.ts` 的既有内联 SVG），
 *  语义由旁边的短标签或宿主按钮的 `aria-label` 承担——图标不单独承担状态。 */
const wbIcon = (name: string): string =>
  `<svg class="wb-icon" style="vertical-align:-2px" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${WB_ICON_PATHS[name] ?? ""}</svg>`;

/** Plan-Revision 6（图标口径）：纯图标按钮把同一个对象名写两处——`aria-label`
 *  给辅助技术（屏幕阅读器仍读出名称），按钮内的 `.wb-icon-hint` 提示板给鼠标悬停与
 *  键盘焦点（CSS 只在 `:hover` / `:focus-visible` 显示它）；浏览器 `title` 只作补充。
 *  提示板是默认不可见的视觉重复件，故标记 `aria-hidden`：门禁的「默认首层字量」
 *  因此只数真正常显的文字，不把 CSS 隐藏的提示算进去。 */
const wbIconHint = (name: string): string =>
  `<span class="wb-icon-hint" role="tooltip" aria-hidden="true">${escapeHtml(name)}</span>`;

/** KPI entity chip (PR11): a small tinted tile that ties a reading to its entity
 *  colour (requests indigo, Token teal, credits violet, fee on the lemon hero).
 *  Decorative: the label beside it names the reading. */
const kpiChip = (
  icon: string,
  entity: "req" | "tok" | "cred" | "cost"
): string =>
  `<span class="wb-kchip wb-kchip-${entity}" aria-hidden="true">${wbIcon(icon)}</span>`;

/** Plan-Revision 6（折叠口径）：整行可点的 `<summary>` 左侧写对象名或条数，
 *  右侧固定位置放 chevron；`data-wb-fold-chevron` 是门禁的标记，旋转由 CSS 做。
 *  标记必须为空（CSS 的 `:empty::before` 才画 chevron），所以这里不留文本节点。 */
const wbFoldMark = (): string =>
  `<span class="wb-fold-mark" data-wb-fold-chevron="1" aria-hidden="true"></span>`;

/** Navigation label: every nav control carries its visible name in the expanded
 *  sidebar, so the navigation does not depend on reading an icon. Collapsed
 *  (html[data-console-rail="collapsed"], also forced at ≤ 1180px) the label hides
 *  and the same name shows on the `wbIconHint()` plate instead; `aria-label`
 *  names the control in both states. */
const wbNavLabel = (text: string): string =>
  `<span class="wb-nav-label shell-nav-text">${escapeHtml(text)}</span>`;

/** 导航六项 + 设置（契约 4.1 / 3.13b）：图标 + 短标签，标签仍是 `names` 的
 *  原词，业务词一个不改。 */
const LANE_ICONS: readonly string[] = [
  "overview",
  "gateway",
  "ops",
  "cost",
  "logs",
  "models",
  "share",
];

/** Gateway console workbench data — ONLY the chains the console actually
 *  serves (ADR 0019 in-service set) and the shared usage host. Unrelated
 *  tasks (DSH desktop / AALC / MAA) are NOT console business and stay out;
 *  DSH only appears later as a cost-ledger source, never as a runtime-managed
 *  service card. `probe` is the read-only liveness path through the dev-server
 *  proxy; `targets` names the runtime-managed start/stop target. An entry
 *  without `targets` is the web host itself (no start/stop per contract);
 *  `statusAction` is the read-only account-auth query this chain exposes. */
type ServiceRow = {
  id: number;
  name: string;
  port: string;
  note: string;
  probe: string;
  targets?: string[];
  statusAction?: {
    action: string;
    target: string;
    label: string;
    gated?: true;
  };
  manageActions?: readonly {
    action: string;
    target: string;
    label: string;
    title: string;
  }[];
};
const SERVICES: readonly ServiceRow[] = [
  {
    id: 0,
    name: "chat2api 主链",
    port: "8000",
    note: "UM 上游会话代理",
    probe: "/gw/chat2api/api/models",
    targets: ["chat2api"],
    statusAction: {
      action: "account.um_status",
      target: "UM",
      label: "查询认证状态",
    },
    manageActions: [
      {
        action: "account.login",
        target: "UM",
        label: "账号登录",
        title:
          "先核验当前认证；有效时无需登录，凭据缺失或被拒时打开专用 Edge 登录窗口；执行前需确认",
      },
      {
        action: "account.um_verify",
        target: "UM",
        label: "核验登录是否有效",
        title: "向上游确认当前登录凭据是否有效；不调用模型，不自动重新登录",
      },
    ],
  },
  {
    id: 1,
    name: "费用统计服务",
    port: "8001",
    note: "统计聚合 + 控制台适配层（同一宿主，不提供启停）",
    probe: "/gw/usagesrv/health",
  },
  {
    id: 2,
    name: "Qoder 国际网关",
    port: "8097",
    note: "Go 原生网关；分桶额度与模型目录可直接读取",
    probe: "/gw/qoder-global/",
    targets: ["QoderGlobal"],
    statusAction: {
      action: "qoderglobal.status",
      target: "QoderGlobal",
      label: "查询登录状态",
    },
    manageActions: [
      {
        action: "qoderglobal.login",
        target: "",
        label: "登录窗口",
        title: "打开 Qoder 国际登录窗口（由维护者完成登录）",
      },
      {
        action: "qoderglobal.quota",
        target: "QoderGlobal",
        label: "查额度",
        title: "读取 Qoder 国际链额度（只读）",
      },
    ],
  },
  // Rev16 A16-1: the CLIProxy login-registration read exists only on backends that
  // register `cliproxy.status`; `gated` keeps the button away until they do.
  {
    id: 3,
    name: "Gemini 网关 (CLIProxy)",
    port: "8320",
    note: "Antigravity OAuth · 可用模型以实时目录为准",
    probe: "/gw/cliproxy/healthz",
    targets: ["CLIProxy"],
    statusAction: {
      action: "cliproxy.status",
      target: "CLIProxy",
      label: "查询登录状态",
      gated: true,
    },
  },
  {
    id: 4,
    name: "Qoder 国内中转",
    port: "8095",
    note: "官方 CN SDK 网关",
    probe: "/gw/qoder-cn/",
    targets: ["Qoder"],
    statusAction: {
      action: "qoder.status",
      target: "Qoder",
      label: "查询登录状态",
    },
    manageActions: [
      {
        action: "qoder.quota_read",
        target: "Qoder",
        label: "查额度",
        title: "读取国内中转额度（只读）",
      },
    ],
  },
];

/** R11-6/B2: catalog service id → the backend health snapshot's service id
 *  (/api/console/health on 8001 — the SAME path works in dev through the
 *  /gw/usagesrv prefix and natively on the static origin). */
const HEALTH_ID_BY_SERVICE: Record<number, string> = {
  0: "chat2api",
  1: "usagesrv",
  2: "qoder-global",
  3: "cliproxy",
  4: "qoder-cn",
};

/** F12-3: model series drawn at once on a trend chart (four validated colour
 *  slots besides the total line). */
const TOKEN_SERIES_MAX = 4;
/** Fixed colour slot per gateway source for composition charts (identity, never
 *  rank); a source outside this list draws in the neutral "other" tone. */
const SOURCE_SLOTS: Readonly<Record<string, number>> = {
  chat2api: 4,
  qoder_global: 1,
  cliproxy: 2,
  qoder_cn: 3,
};

/** Rev16 A16-2: the console host (8001, id 1) keeps its health probe and the fee
 *  lane, but it is not a business chain, so it has no row in the service list,
 *  the jump index or the chain counts. Rows are looked up by their stable `id`,
 *  never by array position, so the remaining chains keep their ids (and the log
 *  / account / runtime mappings keyed on them). */
const CONSOLE_HOST_ID = 1;
/** The main chain (chat2api) that the overview verdict speaks for. */
const MAIN_CHAIN_ID = 0;
const CHAINS: readonly ServiceRow[] = SERVICES.filter(
  (s) => s.id !== CONSOLE_HOST_ID
);
const serviceById = (id: number): ServiceRow | undefined =>
  SERVICES.find((s) => s.id === id);
const CHAIN_COUNT_WORD =
  ["零", "一", "两", "三", "四", "五", "六", "七", "八", "九"][CHAINS.length] ??
  String(CHAINS.length);

/** Rev17 排序增补：the browser-local gateway-order preference namespace. The
 *  candidate preview build is the Demo (its #demo-* surfaces and the console
 *  gate run synthetic fixtures), so it keeps its own namespace; production (and
 *  the dev server, a different origin anyway) uses the production one. A preview
 *  run can therefore never write the order a real page reads back. */
const GW_ORDER_NAMESPACE = __CONSOLE_CANDIDATE__ ? "demo" : "production";
/** Presentable reason for every read status whose stored order was NOT used; the
 *  module's semantics stay untouched (the stored payload is kept, the effective
 *  order is the default). */
const GW_ORDER_RESET_REASONS: Readonly<Record<string, string>> = {
  reset_corrupt: "保存的顺序已损坏",
  reset_version: "保存的顺序格式版本不认识",
  reset_duplicate: "保存的顺序有重复条目",
  storage_read_failed: "浏览器拒绝读取保存的顺序",
};

/** 需人工登录的动作：`*.login` 会在维护者机器上弹出真实浏览器窗口、由维护者
 *  本人完成登录（account.login / qoderglobal.login）。它们是账号页唯一的
 *  「需人工」一级，先二次确认再提交；只读的认证/额度查询仍是一次点击直达。 */
const isLoginAction = (action: string): boolean => action.endsWith(".login");

/** What an armed two-step confirm is for: the action plus its target. Rev8
 *  模型设置的应用按钮没有 action/target 数据属性，用 `source|model_id` 当目标；
 *  模型同步的应用按钮带 `data-wb-gw-source`，用来源当目标。两处都只比较身份，
 *  不比较文案——重绘后按身份重新武装。 */
const confirmKey = (b: HTMLElement): string => {
  const d = b.dataset;
  const action =
    d.wbSvcAction ?? d.wbModelAction ?? d.wbAcctAction ?? d.wbMsAction ?? "";
  const target =
    d.wbTarget ?? d.wbAcctTarget ?? d.wbMsKey ?? d.wbGwSource ?? "";
  return `${action}|${target}`;
};

/** 账号页的只读查询动作（契约 4.3）：各链自己的认证/登录状态查询与额度查询。
 *  它们的真实返回是账号页「认证状态」与「上游额度桶」的唯一来源，所以与
 *  `model.*` 一样进 `actionResults` 记忆——否则 `accountAuthHtml` 没有任何
 *  写入者，永远只能显示「未读取」，「已认证 / 未认证 / 读于」不可达。 */
/** Read-only actions whose last answer (or failure) the page remembers. */
const SHARE_READ_ACTIONS: readonly string[] = ["share.diagnostics"];
/** Confirm identity also names the action area, so duplicate entry points do not share an arm. */
const shareConfirmKey = (b: HTMLElement): string =>
  b.dataset.wbSharePublic
    ? `public:${b.dataset.wbSharePublic}`
    : b.dataset.wbShareAction
      ? `act:${b.dataset.wbShareAction}:${b.dataset.wbShareScope ?? "main"}`
      : b.dataset.wbShareKey
        ? `key:${b.dataset.wbShareKey}:${b.dataset.wbShareKeyId ?? ""}`
        : "";
/** Rev15 A15 single-layer maintenance actions (one share orchestration lock). */
const SHARE_LAYER_ACTIONS: readonly string[] = [
  "share.backend_login",
  "share.backend_reauthenticate",
  "share.backend_restart",
  "share.gateway_restart",
];
const SHARE_BROKER_ACTIONS: readonly string[] = [
  ...SHARE_LAYER_ACTIONS,
  "share.start",
  "share.repair_limits",
];
/** Rev15 A15: Chinese summary of a finished single-layer share action, from the
 *  fields the backend returns - the backend's own label, whether a restart
 *  really happened, and a cancel that came too late to stop it (the operation
 *  reports what actually happened; the page does not call it rolled back).
 *  null for every other action (the generic summary applies). */
function shareLayerSummary(
  actionName: string,
  op: { result?: Record<string, unknown> | null; cancel_requested?: boolean }
): string | null {
  const r = op.result ?? {};
  const late =
    op.cancel_requested === true ? "取消已请求但未能拦下：按实际结果报告" : "";
  if (actionName === "share.diagnostics") return "分层状态已读取";
  if (actionName === "share.repair_limits")
    return "隔离规则已修复，分享保持关闭";
  if (actionName === "share.backend_login") {
    const prefix = r.started_here === true ? "8110 已启动；" : "";
    if (r.state === "valid")
      return (
        prefix +
        (r.triggered === false
          ? "认证有效，无需重新登录"
          : "登录完成，认证有效")
      );
    const state =
      r.state === "logging_in"
        ? "登录进行中，请在专用 Edge 完成登录"
        : r.state === "needs_login"
          ? "仍需登录，尚未确认登录会话"
          : "当前认证状态未确认";
    return (
      prefix +
      [state, typeof r.note === "string" ? r.note.slice(0, 160) : ""]
        .filter(Boolean)
        .join("；")
    );
  }
  if (actionName === "share.backend_reauthenticate") {
    const head =
      r.accepted === false
        ? "未受理"
        : typeof r.label === "string" && r.label
          ? r.label
          : typeof r.state === "string"
            ? `状态 ${r.state}`
            : "已提交";
    return [head, typeof r.note === "string" ? r.note.slice(0, 80) : "", late]
      .filter(Boolean)
      .join("；");
  }
  if (
    actionName === "share.backend_restart" ||
    actionName === "share.gateway_restart"
  ) {
    const part = (
      actionName === "share.gateway_restart" ? r.gateway : r.backend
    ) as Record<string, unknown> | undefined;
    const secs =
      typeof part?.elapsed_s === "number"
        ? `，用时 ${Math.round(part.elapsed_s)} 秒`
        : "";
    const head =
      r.restarted === true
        ? `已重启${secs}`
        : r.restarted === false
          ? "未重启"
          : "已完成（未返回是否重启）";
    return [head, late].filter(Boolean).join("；");
  }
  return null;
}
const ACCOUNT_READ_ACTIONS: readonly string[] = [
  "account.um_status",
  "account.um_verify",
  "cliproxy.status",
  "qoder.status",
  "qoder.quota_read",
  "qoderglobal.status",
  "qoderglobal.quota",
];
/** 模型页只读读面的失败记忆（与账号页共用同一张 `actionResultFailures`）：
 *  catalog / 设置读面 / 探测证据读取失败时页面仍挂着上一次读数，必须留下
 *  实测失败状态，否则「读取失败」永远不可达（维护者 2026-10-06 接线）。 */
const MODEL_READ_ACTIONS: readonly string[] = [
  "model.catalog",
  "model.context_overrides",
  "model.context_probe_preview",
];
/** 只读查询失败/成功要写进失败记忆的动作集合（账号页 + 分享 + 模型读面）。 */
const REMEMBER_READ_RESULT: readonly string[] = [
  ...ACCOUNT_READ_ACTIONS,
  ...SHARE_READ_ACTIONS,
  ...MODEL_READ_ACTIONS,
];

/** 账号卡折叠里「原始返回」的展示上限（维护者 2026-09-29）：只读查询的载荷是
 *  后端受控片段（有界），但前端仍给它一个上限，超长时如实写截断与原长，绝不
 *  静默丢字段；完整终态仍可在「操作记录」里读到。 */
const ACCOUNT_RAW_RETURN_MAX = 1200;

/** 后端 `GET /api/console/stats` 的区间上限，与 console_stats.py:100 的
 *  `MAX_RANGE_DAYS = 92` 同一口径：前端预检用同一个数字，不另立一套阈值；
 *  后端仍会独立校验并对超限返回 400。 */
const STATS_RANGE_MAX_DAYS = 92;

/** Age at which remembered probe evidence stops counting as current. The
 *  backend snapshot has a 30s TTL and this page polls it every 30s, so three
 *  missed cycles (90s) mean the shown verdict is no longer a reading of the
 *  present. Stale ≠ down: a stale dot says "cannot tell right now", which is
 *  why it renders amber and never red. */
const HEALTH_STALE_AFTER_MS = 90_000;

interface HealthServiceEntry {
  id: string;
  port: number;
  /** "unknown" = the probe itself could not decide (e.g. a connect
   *  timeout) — it must never be displayed as a fabricated dead/alive. */
  state: "listening" | "not_listening" | "unknown";
  http_status?: number | null;
  ms?: number;
  note?: string;
}

/** Catalog source id → speedtest action source id (console_speedtest.SOURCES
 *  uses its own enum: um / qodercn / qoderglobal / m365). */
const SPEEDTEST_SOURCE: Record<string, string> = {
  um: "um",
  m365: "m365",
  qoder_cn: "qodercn",
  qoder_global: "qoderglobal",
  cliproxy: "cliproxy",
};

/** Rev17 单网关模型来源关联（接线表，不是运行时推算）：网关服务的稳定身份
 *  （SERVICES[].targets[0]）↔ 模型目录来源。单一权威在后端：
 *  console_speedtest.py:113-120 `_SOURCE_SERVICE_KEYS`（um→chat2api、
 *  qodercn→Qoder、qoderglobal→QoderGlobal、cliproxy→CLIProxy）与
 *  lib/gateway-runtime.ps1 的服务描述符是同一个服务身份；M365 已归档
 *  （model_sync.PAUSED_SOURCES），不接线。页面只做键名转发：目录行、
 *  绑定行与同步计划全部来自后端返回，不按端口或显示名猜来源。 */
const GATEWAY_MODEL_SOURCE: Readonly<Record<string, string>> = {
  chat2api: "um",
  Qoder: "qoder_cn",
  QoderGlobal: "qoder_global",
  CLIProxy: "cliproxy",
};
/** §11.1（2026-10-10）模型来源 → 运行/停用门的目标服务身份：GATEWAY_MODEL_SOURCE
 *  的逆表，与 console_speedtest.py `_SOURCE_SERVICE_KEYS` 及 lib/gateway-runtime.ps1
 *  的服务描述符同一身份；M365 已暂停，不在表内（未知目标 ≠ 已停止）。 */
const MODEL_SOURCE_TARGET: Readonly<Record<string, string>> = {
  um: "chat2api",
  qoder_cn: "Qoder",
  qoder_global: "QoderGlobal",
  cliproxy: "CLIProxy",
};

/** 模型设置上下文来源的中文标注（后端字段 declared/manual/measured）。 */
const CONTEXT_SOURCE_LABEL: Readonly<Record<string, string>> = {
  declared: "目录声明",
  manual: "人工设置",
  measured: "已验证下界",
};
/** 客户端读回状态的中文标注（后端字段 ok/absent/unavailable/unknown）。 */
const CLIENT_READBACK_LABEL: Readonly<Record<string, string>> = {
  ok: "已读回",
  absent: "客户端无该模型",
  unavailable: "读回不可用",
  unknown: "未知",
};

/** A12-3: the usage endpoints' catalog source ids → the call-log feed's own
 *  source names, so the 24h drilldown can find per-request rows. The feed's
 *  reader table (usagesrv.py `_console_log_readers`) names the CN gateway's
 *  ledger "qoder" (QODER_USAGE_LOG = ai-gateway/qoder/data/usage.jsonl, written
 *  by the 8095 CN gateway) and the international chain's ledger "qoder-global"
 *  (qoder-global/logs/requests.log — the SAME ledger the usage endpoints read,
 *  console_stats.py:760), registered under BOTH spellings the backend could use:
 *  "qoder-global" (the directory name) and "qoder_global" (the usage endpoints'
 *  catalog id) — one file, two ids. F4b: 国际链的桶因此可下钻；下钻把这两个
 *  拼写当作同一逻辑来源（同一账本只取其中一份，见 feedSpelling），绝不把
 *  同一记录按两个 id 重复列出。An empty list = the page does not request that
 *  source's ledger at all (um 由 chat2api 账本记账、dsh 是冻结日快照、cliproxy
 *  采集链未启用): 下钻如实说明「不在调用日志账本内 / 未启用」，绝不用空表
 *  冒充零记录。 */
const LOG_FEED_SOURCE_ALIAS: Record<string, string[]> = {
  chat2api: ["chat2api"],
  qoder_cn: ["qoder"],
  qoder_global: ["qoder-global", "qoder_global"],
  um: [],
  dsh: [],
  cliproxy: [],
};

/** F4b: every source the alias table can name is requested from the feed, so a
 *  bucket's 逐次 drill can look for its rows. The server owns the source→path
 *  table and answers each covered id with its own rows; an id it does NOT read is
 *  named in the response's `sources_unavailable` (usagesrv.py /api/console/logs).
 *  The drill reads that statement — not the client's own request list — to tell
 *  「该来源不在调用日志账本内」 from 「尾部读取里没有该来源的记录」, and an
 *  absent statement stays 未知 instead of being read as a coverage claim.
 *  The per-source tail depth stays the one the log table was built around
 *  (40 rows/source) — adding a source never deepens one tail at the cost of
 *  another, and the endpoint's own 200-row cap still bounds the read. */
const LOG_FEED_SOURCES = [
  "chat2api",
  "qoder",
  "qoder-global",
  "qoder_global",
] as const;
const LOG_FEED_PER_SOURCE = 40;
const LOG_FEED_LIMIT = Math.min(
  200,
  LOG_FEED_PER_SOURCE * LOG_FEED_SOURCES.length
);

/** Contract 3.6 `wb-write-group` (registered 2026-09-26): the write-boundary
 *  mark and the ONE button it labels travel as a pair. `.wb-model-ops` is a flex
 *  row with `gap: var(--wb-sp-3)` = 8px, so a bare mark sat 8px from the
 *  button before it AND from the one after it — the geometry could not say which
 *  button it modified, and the two model-lane tabs even ordered the pair
 *  oppositely. Inside a group the pair is 2px apart while the row's own gap
 *  still separates the groups, and both tabs order it [mark][button]. The class
 *  is the CSS lane's hook; the inline style states the geometry at the point of
 *  use until workbench.css carries the rule. */
const WB_WRITE_GROUP_STYLE = "display:inline-flex;align-items:center;gap:2px";

/** The `missing[].reason` values of /api/console/usage-tokens, named in the
 *  table. An unknown reason string still renders its raw value. */
const MISSING_REASON_LABEL: Record<string, string> = {
  output_only: "输入未知（仅输出可见）",
  no_input: "输入未知",
  no_tokens: "无 Token 记录",
  invalid_time: "时间无法判定（时间戳无效）",
};

/** Log-lane 时段 filter (A12-3): undefined = 全部, else the window length in
 *  ms measured against each row's own timestamp. */
const LOG_FEED_WINDOWS: Record<string, number | undefined> = {
  "": undefined,
  "1h": 3600_000,
  "6h": 6 * 3600_000,
  "24h": 24 * 3600_000,
};

/** Quick-jump index (R9-R25): lanes, per-service panels and function
 *  keywords, one line each — as the workspace grows, an entry goes here so
 *  nothing is more than a keystroke away. Rev16 A16-5: an entry that names a
 *  part of a page lands on it (`anchor` is scrolled into view, `modelSection`
 *  opens that model tab) instead of the page top. */
const JUMP_INDEX: readonly {
  label: string;
  lane: number;
  service?: number;
  anchor?: string;
  modelSection?: "catalog" | "sync" | "probe";
}[] = [
  { label: "总览 · 运行裁决与用量", lane: 0 },
  { label: "总览 · 服务明细与启停", lane: 0, anchor: ".wb-ov-services" },
  { label: "网关 · 账号登录与额度", lane: 1 },
  { label: "模型 · 目录与同步", lane: 5, modelSection: "catalog" },
  { label: "模型 · 同步预览与应用", lane: 5, modelSection: "sync" },
  { label: "费用 · 用量与账单", lane: 3 },
  {
    label: "费用 · 调用明细",
    lane: 3,
    anchor: '[data-wb-anchor="usage-feed"]',
  },
  { label: "日志 · 操作记录", lane: 2 },
  { label: "分享 · 开放状态与主操作", lane: 6 },
  { label: "分享 · 访问密钥管理", lane: 6, anchor: ".wb-share-keys" },
  {
    label: "分享 · 容量与近期操作",
    lane: 6,
    anchor: '[data-wb-anchor="share-capacity"]',
  },
  ...CHAINS.map((s) => ({
    label: `${s.name} · 管理详情`,
    lane: 1,
    service: s.id,
  })),
  { label: "账号登录 / 重新授权", lane: 1, service: 0 },
  { label: "额度查询", lane: 1, service: 4 },
  { label: "服务重启（当前网关）", lane: 1 },
];

/** Console adapter action row (GET /api/console/actions). */
type ActionRow = {
  action: string;
  enabled: boolean;
  disabled_reason: string | null;
  description: string;
  cancelable: boolean;
};
/** Runtime service snapshot entry (GET /api/console/services). */
type RuntimeEntry = {
  key: string;
  display_name: string;
  port: number;
  state: string;
  healthy: boolean;
  detail: string;
  process_ids: number[];
};
/** One operation row (GET /api/console/operations). `cancel_requested` is the
 *  backend's OWN field (console_operations.py `_view`): a cancel was accepted
 *  for a non-terminal row — 「已请求取消」 is NOT 「已取消」, the ledger's
 *  `state` still answers the terminal word. */
type OpRow = {
  id: string;
  action: string;
  target: string;
  state: string;
  created_at: number;
  cancel_requested?: boolean;
  result?: Record<string, unknown> | null;
  error?: { code?: string; message?: string } | null;
  steps?: { at?: number; note?: string }[];
};
type OpStatus = { state: "pending" | "ok" | "failed"; text: string };
/** Remembered results of the model.* operations (model lane, R9-R31):
 *  catalog / sync_preview / sync_apply / sync_pending / task_status. */
type CatalogModelRow = {
  id: string;
  display_name: string | null;
  context: number | null;
  efforts: string[];
  default_effort: string | null;
  publish: boolean;
  efforts_source?: string | null;
  output_token_cap?: { value?: number | null; source?: string | null } | null;
};
/** Per-day and per-model ledger aggregates (GET /api/summary services.*.days/.models).
 *  `costCnyPartial` 沿用后端语义（usagesrv.py:903-913）：true = costCny 只含可折算
 *  记录，是下界，不是总额。 */
type DayPoint = {
  day: string;
  requests: number;
  costCny?: number;
  costCnyPartial?: boolean;
  credits?: number;
};
/** Per-model ledger totals. `tokens` sums input + output of the rows that carry
 *  them; `unknownRequests` counts requests whose usage the ledger could not read
 *  (unknown_usage) - those are NOT in `tokens`, so a non-zero count makes the
 *  model's Token reading partial, never a silent 0. */
type ModelPoint = {
  model: string;
  requests: number;
  tokens: number;
  unknownRequests: number;
  tokensMissing: boolean;
};
type SummarySeries = {
  days: DayPoint[];
  models: ModelPoint[];
  chainDays?: Record<string, DayPoint[]>;
};
/** One per-chain series of the fee-lane trend chart (R9-R40 task 3): billed
 *  chains draw accent area/lines, the credits chain draws a dashed ink line —
 *  credits never merge into the ¥ totals. */
type FeeTrendSeries = {
  label: string;
  values: number[];
  kind: "billed" | "credits";
  opacity: number;
  fillOpacity: number;
};
/** Per-chain fee detail (R9-R29 cost lane): one row per aggregated gateway
 *  with its full model breakdown, straight from /api/summary services.*.
 *  `costCnyPartial` = 该链 costCny 只含可折算记录（下界，不是总额）。 */
type ChainModel = {
  model: string;
  requests: number;
  input: number;
  output: number;
  cache?: number;
  priced?: number;
  unpriced?: number;
  costCny?: number | null;
  costCnyPartial?: boolean;
  credits?: number;
};
type ChainFee = {
  name: string;
  requests: number;
  input: number;
  output: number;
  costCny?: number | null;
  costCnyPartial?: boolean;
  credits?: number;
  models: ChainModel[];
};
/** One gateway call-log entry (R9-R30): tail rows of the chain's own usage
 *  ledger JSONL (chat2api / qoder / qoder-global), unified shape. F2: the
 *  numeric fields are `number | null` — the ledger's missing/unparsable value
 *  arrives as null (未知/未采集), never as a fabricated 0, so a request that
 *  lacks its output tokens must never render as `0 Token/s`. */
type LogEntry = {
  source: string;
  time: string;
  model: string;
  status: string;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_input_tokens: number | null;
  reasoning_tokens: number | null;
  duration_ms: number | null;
  credits?: number | null;
};
/** Seven-day bounded statistics (GET /api/console/stats?from&to). */
type StatsWeek = {
  totals: {
    requests: number;
    input_tokens: number;
    output_tokens: number;
    cache_tokens: number;
    reasoning_tokens: number;
    estimated_cost_cny: number;
    credits: number;
  };
  days: { date: string; day_total: { requests?: number } }[];
};
/** A12-3 (Plan-Revision 12): one per-day ledger row of
 *  GET /api/console/usage-tokens?days=14 (bounded, read-only, no-store;
 *  Beijing natural days). `input_tokens` / `output_tokens` sum ONLY the requests
 *  that carry BOTH components, so the row's complete total may enter a complete
 *  total; `complete` ⇔ `partial_requests == 0`. F1: a request missing one
 *  component is carried in `partial_*` (and in `missing`) and NEVER folded into
 *  the complete total — and it must not push the bucket's other, complete
 *  requests out of that total either (which is why the sums are per-request, not
 *  per-bucket). Identity is source+model — the same model name under two sources
 *  stays two rows. */
type UsageTokenRow = {
  date: string;
  source: string;
  model: string;
  input_tokens: number | null;
  output_tokens: number | null;
  complete: boolean;
  /** Requests that entered the two sums above. */
  complete_requests: number;
  /** Requests missing either component (listed separately, never in the total). */
  partial_requests: number;
  /** Sum over the partial requests' input / output components (null = that
   *  component is not visible in any of them). Never folded into the complete
   *  total; the visible output part backs 仅输出可见. */
  partial_input_tokens: number | null;
  partial_output_tokens: number | null;
};
/** `date: null` = the record's timestamp could not be parsed, so it belongs to
 *  NO Beijing day (reason `invalid_time`). Such a row is kept and listed as
 *  时间无法判定 — never silently dropped by a window filter and never guessed
 *  onto a day. F1: `input_tokens` / `output_tokens` are that bucket's PARTIAL
 *  requests' visible components (null when the component is not visible at all),
 *  so 仅输出可见 can show its own real numbers instead of a day lookup. */
type UsageTokenMissingRow = {
  date: string | null;
  source: string;
  model: string;
  reason: string;
  input_tokens?: number | null;
  output_tokens?: number | null;
};
/** One coverage error of one source (coverage.sources[...].errors[]): a type word
 *  plus an optional numeric count (bad lines / failed records / missing files). */
type UsageCoverageError = {
  type?: string;
  detail?: string;
  bad_lines?: number;
  count?: number;
  failed_records?: number;
  failed_rows?: number;
};
/** Per-source coverage disclosure of the two bounded usage reads
 *  (coverage.sources[<source>]): whether the ledger file was actually read
 *  (`ledger_present`), whether the source collects at all (`enabled` — a chain
 *  that is not collecting explains an empty window WITHOUT claiming zero usage)
 *  and the server's own `note`. Every field is optional and an absent one stays
 *  未知 on screen; the client never fills a coverage field in. */
type UsageSourceCoverage = {
  ledger?: string | null;
  ledger_present?: boolean;
  enabled?: boolean;
  events?: number;
  note?: string;
  errors?: UsageCoverageError[];
  truncated?: boolean;
  bytes_truncated?: boolean;
  dropped_events?: number;
  undated_records?: number;
  non_ok_records?: number;
  unknown_fields?: string[];
};
/** The coverage block of one bounded usage read. `truncated` / `clamped` /
 *  `dropped_rows` / `dropped_missing_rows` are the server's own bounded-read
 *  marks: shown when set (a dropped count of 0 is not noise). `dropped_rows`
 *  counts the dropped days[] rows, `dropped_missing_rows` the dropped missing[]
 *  rows (each list is capped on its own, console_stats.py). An absent `sources`
 *  renders as 覆盖信息未提供, never as an invented all-clear. */
type UsageCoverage = {
  days?: number;
  complete_rows?: number;
  partial_rows?: number;
  scanned?: number;
  hours?: number;
  rows?: number;
  truncated?: boolean;
  clamped?: boolean;
  dropped_rows?: number;
  dropped_missing_rows?: number;
  sources?: Record<string, UsageSourceCoverage>;
};
type UsageTokensPayload = {
  days: UsageTokenRow[];
  missing: UsageTokenMissingRow[];
  coverage?: UsageCoverage;
  timezone?: string;
  checked_epoch?: number;
};
/** A12-3: one hour+source+model bucket of
 *  GET /api/console/usage-rates?hours=24 (bounded, read-only). `rate` is
 *  output_tokens / (duration_ms/1000) over WHOLE requests (first-token wait
 *  included). The server emits a bucket only for records that HAVE output tokens
 *  and a positive duration (console_stats.usage_rates skips the rest into
 *  `skipped`), so every row it returns carries a computable rate; the
 *  `number | null` contract is kept as a defensive branch — a future upstream
 *  contract change must render 未知, never a fabricated 0. Rows whose records
 *  were skipped are counted in `skipped`, never in the denominator; the reason
 *  counts are independent (one record may miss its output tokens AND its
 *  duration), so their sum may exceed the skipped record count. `invalid_time`
 *  counts records whose timestamp could not be parsed (they cannot be placed in
 *  any hour). */
type UsageRateRow = {
  hour: string;
  source: string;
  model: string;
  output_tokens: number;
  duration_ms: number;
  requests: number;
  rate: number | null;
};
type UsageRatesPayload = {
  hours: UsageRateRow[];
  skipped: {
    missing_output_tokens?: number;
    missing_duration?: number;
    invalid_duration?: number;
    invalid_time?: number;
  };
  /** F5b (Plan-Revision 12): the LEDGER-recorded request count per source
   *  over the SAME hour window as `hours` — every ledger record of the window,
   *  including the ones whose rate cannot be computed (missing output tokens /
   *  unusable duration). A source whose ledger was NOT read is `null` (未知);
   *  a read ledger with an empty window is a real 0 (零记录 ≠ 无记录), which is
   *  what makes 「无记录 / 未知 / 有记录但不可算速率」 distinguishable.
   *  Absent key (older backend) = 未知, never a fabricated 0. */
  recorded_requests?: Record<string, number | null>;
  coverage?: UsageCoverage;
  timezone?: string;
  checked_epoch?: number;
};
/** Honest read state shared by the two bounded usage reads: a failed re-read
 *  keeps the last REAL payload and marks it stale with its capture time —
 *  it is never replaced by an empty or zeroed payload. */
type UsageRead<T> = {
  state: "loading" | "live" | "stale" | "error";
  payload?: T;
  checkedAt?: number;
  reason?: string;
};
/** Price table (GET /api/console/prices — the raw prices.json the pricing
 *  meter itself uses; _说明 stripped server-side). */
type PriceTable = {
  exchangeRate?: number;
  checkedAt?: string;
  checkedStale?: boolean;
  models?: Record<string, Record<string, unknown>>;
  providers?: Record<string, Record<string, unknown>>;
};
/** Server-side log source registry (GET /api/console/log-sources, R9-R38):
 *  the server owns the mapping source→path; the client only ever sends ids. */
type LogSourceRow = {
  source: string;
  display: string;
  categories: LogSourceCategory[];
};
type LogSourceCategory = {
  category: string;
  display: string;
  name: string | null;
  rotating?: boolean;
  rotation_index?: number;
  level_filter?: boolean;
};
/** One run-log row (GET /api/console/log-entries): server-redacted. An
 *  unparsable line keeps its raw text (parse_ok=false), a multi-line error
 *  block arrives already attached to its parent message. */
/** Rev8 B8-5 unified run event the backend derives from a log line (every field
 *  null when it cannot prove it; nothing here is guessed by the page). */
type LogRowEvent = {
  timestamp?: string | null;
  model?: string | null;
  request_id?: string | null;
  phase?: string | null;
  outcome?: string | null;
  error_code?: string | null;
  duration_ms?: number | null;
};
type LogEntryRow = {
  ts_norm?: number | null;
  ts?: string;
  level?: string;
  message: string;
  parse_ok: boolean;
  cut?: boolean;
  event?: LogRowEvent | null;
};
type LogEntriesResult = {
  status: "ok" | "missing" | "denied" | "error" | "unconfigured";
  rows: LogEntryRow[];
  scanned_bytes: number;
  window_start_offset: number;
  truncated: boolean;
  next_cursor: string | null;
  reset_hint: boolean;
  file_key: string | null;
  error?: string;
  /** Echoed only when the backend applied the request filter (scope = read window). */
  request_id?: string;
  request_scope?: string;
};
/** Log-detail view state (R9-R38): survives back-navigation and lane
 *  switches; expanded is a set of rendered-event identities with the error block
 *  unfolded (C4: never a row index — prepends and refreshes shift indices). */
type LogDetailState = {
  source: string;
  category: string;
  q: string;
  since: string;
  until: string;
  follow: boolean;
  newCount: number;
  rows: LogEntryRow[];
  nextCursor: string | null;
  status: string;
  errorCode: string;
  scannedBytes: number;
  truncated: boolean;
  resetHint: boolean;
  stale: boolean;
  loading: boolean;
  requested: boolean;
  expanded: Set<string>;
  wrap: boolean;
  fetchedAt?: string;
  /** Selected rotation generation (0 = active file, 1..N = fixed registry
   *  rotation like chat2api.log.N; server answers invalid_rotation beyond
   *  the cap and falls back to the active path for missing generations). */
  rotation: number;
  /** F12-6: rows of one request only - the backend filters on the event's
   *  request_id (exact), not on a text match. "" = no request filter. */
  requestId: string;
  /** Whether the last answer confirmed the request filter: "applied" when the
   *  backend echoed the same request_id, "unconfirmed" when it did not (the rows
   *  are then the unfiltered window), "" while not asked or not answered. */
  requestEcho?: "applied" | "unconfirmed" | "";
};

/** Share lane payloads (A36-2 narrow facade, read-only composition):
 *  ShareStatusPayload / ShareKeyRow live in console/share-model.ts.
 *  Missing/unknown fields render as 不可取得 — the UI never fabricates
 *  state, never substitutes fixture data (R9-R35 honest-gate pattern). */
type ShareStatusState =
  | { state: "loading" }
  | { state: "gated" }
  | { state: "refused" }
  | { state: "error"; reason: string }
  | { state: "live"; payload: ShareStatusPayload };
type ShareKeysState =
  | { state: "loading" }
  | { state: "gated" }
  | { state: "refused" }
  | { state: "error"; reason: string }
  | { state: "live"; rows: ShareKeyRow[] };
/** S5 public supervision read (`GET /api/console/share-management/public`).
 *  `live` is the backend's own available:false/unreadable shape too — the card
 *  renders that honestly instead of substituting a healthy snapshot. */
type PublicShareState =
  | { state: "loading" }
  | { state: "unavailable" | "refused" | "error"; reason: string }
  | { state: "live"; payload: PublicShareStatus };
/** 分享用量读口载荷（`GET /api/console/share-usage`，只读、无查询参数、无请求体）。
 *  与需管理令牌的 `GET /api/share/usage` 是同一个构造点、同一份数据、同一口径
 *  （usagesrv.py `_share_usage_response`），但走控制台信任面：本机同源页面免凭据
 *  直读，管理令牌绝不进网页。顶层 5 键全部处理；`keys[]` 只含归因成功的 Key，
 *  `unattributed` 与它同形状（`key_id` 为 null）——未归属用量永远单列，绝不摊到
 *  某个 Key。`costUsd` 为 null = 未知；0 且该模型在 `coverage.unpricedModels`
 *  里 = 无价（未知）：两者都不显示 ¥0。The per-key rows are read through the
 *  backend's `normalizeShareUsage` (src/console/backend), so this page keeps them
 *  opaque here instead of a second, drifting copy of their shape. */
/** F12-8: one per-key (or unattributed) table row, from the backend normaliser. */
type ShareUsageRowView = {
  identity: string;
  /** inputTokens / outputTokens are the backend's legacy per-side sums (complete +
   *  lower-bound + estimated mixed); `tokens` is its split, read first. */
  name: string;
  records: number | null;
  requests: number | null;
  inputTokens: number | null;
  outputTokens: number | null;
  tokens: TokenUsage | null;
  /** USD stays traceable beside the CNY reading; it is never printed as CNY. */
  known: number | null;
  estimated: number | null;
  /** CNY reading from the same price chain; null means unknown, and the three
   *  partial flags each describe only their own subtotal (null = the response did
   *  not say, so the reading is 「人民币读数未提供」). */
  cny: number | null;
  knownCny: number | null;
  estimatedCny: number | null;
  cnyPartial: boolean | null;
  knownCnyPartial: boolean | null;
  estimatedCnyPartial: boolean | null;
  unknown: number | null;
  unpriced: number | null;
  models: CostDetail[];
  days: CostDetail[];
  /** Records whose timestamp could not be parsed have no day identity; the
   *  backend keeps them in their own bucket so their Token and CNY readings are
   *  not lost behind the coverage counter. */
  undated: CostDetail | null;
};
type ShareUsageCoverage = {
  ledgerPresent?: boolean;
  ledgerReadable?: boolean;
  recordsTotal?: number;
  unparsedLines?: number;
  truncated?: boolean;
  ledger?: string;
  limit?: number;
  records?: number;
  attributed?: number;
  unattributed?: number;
  incompleteRequests?: number;
  unknownUsageRequests?: number;
  unknownTimeRequests?: number;
  unpricedRequests?: number;
  peakRequests?: number;
  unpricedModels?: string[];
  priceVersions?: string[];
  priceFn?: boolean;
  attributionReasons?: Record<string, number>;
};
type ShareUsagePayload = {
  requests?: number;
  keys?: unknown[];
  unattributed?: unknown;
  checkedAt?: string;
  coverage?: ShareUsageCoverage;
};
/** 读口状态（照 `ShareKeysState` 的五态形状）。`unavailable` 携带**实测 HTTP
 *  状态**：404 = 当前服务端版本未载入该路由（读口未开放，本轮 8099 实见），
 *  其余非 2xx = 读口存在但读取失败。两者都不折成「每 Key 0 请求」。
 *  401/403 单列 `refused`：按跨端交付契约，本口免凭据，401 只可能是请求打到了
 *  分享管理面或别的路由，不是这个读口的预期状态。 */
type ShareUsageState =
  | { state: "loading" }
  | { state: "gated" }
  | { state: "refused"; reason: string }
  | { state: "unavailable"; status: number; reason: string }
  | { state: "error"; reason: string }
  | { state: "live"; payload: ShareUsagePayload };
/** 任意日期区间聚合（`GET /api/console/stats?from&to`，只读、有界）。区间是北京
 *  时间自然日的闭区间 [from, to]（后端半开 [from 00:00, to+1d 00:00)）；`from > to`
 *  或跨度超过 92 天由后端返回 400（console_stats.py:100 `MAX_RANGE_DAYS`），
 *  前端在发请求前用同一上限预检，不把拒绝折成空表。响应与 `bounded_stats`
 *  同一份：`days[]` / `totals` / `coverage` / `meta`。 */
type StatsRangeDay = {
  date: string;
  day_total: {
    requests?: number | null;
    input_tokens?: number | null;
    output_tokens?: number | null;
    estimated_cost_cny?: number | null;
    credits?: number | null;
  };
};
type StatsRangePayload = {
  days?: StatsRangeDay[];
  totals?: Record<string, number | null>;
  coverage?: {
    sources?: Record<string, unknown>;
    missing_days?: string[];
    unpriced_models?: string[];
  };
  meta?: {
    timezone?: string;
    range_days?: number;
    price_checked_at?: string | null;
  };
};
/** 区间聚合的四态：loading / live / 后端拒绝（400，携带后端受控 detail）/
 *  其他失败（含实测 HTTP 状态，404 = 当前服务端版本未载入该端点）。 */
type StatsRangeState =
  | { state: "loading"; from: string; to: string }
  | {
      state: "rejected";
      from: string;
      to: string;
      status: number;
      reason: string;
    }
  | {
      state: "failed";
      from: string;
      to: string;
      status?: number;
      reason: string;
    }
  | {
      state: "live";
      from: string;
      to: string;
      status: number;
      payload: StatsRangePayload;
    };
/** One short account-card reading (2026-09-29 redesign): the value, one line of
 *  time or reason under it, and the full sentence for its `title`. Plain text;
 *  escaped where it is placed. */
type AccountFact = { text: string; sub: string; title: string };
/** 健康快照手动重探（`GET /api/console/health?refresh=1`，只读有界）的实测状态：
 *  后端接受 `refresh=1` 时重探一次；服务端版本未载入该参数时按**实测 HTTP 状态**
 *  如实显示，不沿用「后端未提供」的泛化说法。 */
type HealthRefreshState =
  | { state: "busy" }
  | { state: "ok"; at: number; cached: boolean }
  | { state: "unavailable"; status: number }
  | { state: "failed"; status?: number; reason: string };
/** One tooltip text tier as the browser actually renders it (see
 *  Workbench.tipFonts): the plate is sized from these numbers, never from a
 *  token read that can disagree with the tip's own CSS. */
type TipFont = { size: number; weight: number; family: string };

/** One per-client sync-change row (L-C): appliedDetails/pendingDetails carry
 *  {consumer, kind, model_id, description}; absent (old server) falls back
 *  to the flat pending strings. */
type SyncDetailRow = {
  consumer?: string;
  kind?: string;
  model_id?: string;
  description?: string;
};
/** One history line (model.sync_history, bounded 1-50). */
type SyncHistoryEntry = {
  timestamp?: string;
  status?: string;
  applied?: string[];
  pending?: string[];
  warnings?: string[];
};
/** Speedtest single-shot result (model.speedtest). */
type SpeedtestResult = {
  status?: string;
  ttfb_s?: number | null;
  total_s?: number | null;
  first_content_preview?: string | null;
  usage?: Record<string, unknown> | null;
  /** Client-side settle time (the server result does not echo a clock). */
  at?: number;
};
/** Context-probe queue record (model.context_probe_preview, backend canonical
 *  fields `records` / `output_token_cap`, Rev8 E8-1): honest estimates in
 *  characters (input) and tokens (output cap); `executable` false = no endpoint or
 *  unreadable credentials, so the record costs nothing this round. */
type ContextProbeQueueRow = {
  source?: string;
  model_id?: string;
  declared_context?: number | null;
  status?: string;
  estimated_requests?: number | null;
  executable?: boolean;
  input_chars_estimate?: number | null;
  output_token_cap?: number | null;
  estimate_note?: string | null;
};
type ContextProbePreview = {
  records?: ContextProbeQueueRow[];
  budget_note?: string;
  check_epoch?: number;
};
/** 提交时范围参数的一个维度的解析结果（维护者 2026-09-29 · 模型同步闭环）：
 *  `null` = 该维度未提交（= 全部，后端默认）；数组 = 已提交的具体值；
 *  `"unknown"` = 该维度提交了但形状读不出来——面板写「未知」，绝不折成
 *  「全部」或「空集」，也绝不把「归属不了」写成「本范围 0 条」。 */
type ScopeList = string[] | "unknown" | null;

/** §11.1（2026-10-10）单击模型后自动读取的固定区域状态（按 source|model_id）。
 *  两个读面都是**只读**：`settings` = 本地已保存设置（草稿 / 已应用 / 客户端
 *  读回，model.context_overrides）；`evidence` = 已有探测证据（model.context_probe_preview，
 *  纯读本地持久化记录，零请求）。`gated` = 运行/停用门未放行（来源未运行或运行
 *  状态未知），本次不自动提交，保留手动读取入口；`failed` 保留实测原因，可手动重试；
 *  迟到结果按读取 seq 丢弃（旧读面结果不落新选模型）。 */
type MsAutoRead = {
  settings: "idle" | "loading" | "done" | "failed" | "gated";
  evidence: "idle" | "loading" | "done" | "failed";
  reason?: string;
};

export class Workbench {
  enabled = false;
  private root: HTMLElement;
  private props: ConsoleProperties = {};
  private lane = 0;
  private modelSection: "catalog" | "sync" | "probe" = "catalog";
  private lastSecond = -1;
  private exitAnimation?: Animation;
  /** Rev16 A16-4: the running page-enter fade, cancelled by the next switch. */
  private laneEnterAnimation?: Animation;
  private visibility: WorkbenchVisibility = defaultWorkbenchVisibility();
  /** Live probe evidence per service id; absent = not probed yet (fixture dot). */
  private live = new Map<number, ProbeResult>();
  private probing = false;
  /** Management-session cost/summary state machine (R20-04/05): every
   *  non-success has its own honest state; "stale" keeps the last real
   *  values with their capture time, never refills from fixture data. */
  private summaryState:
    "loading" | "unauthenticated" | "forbidden" | "error" | "stale" | "live" =
    "loading";
  private summaryError = "";
  /** 首层横幅的实测原因（§3.9 六态可分、§3.12(c)1 短句须含状态词与动作对象）：
   *  只有 fetch 本身失败才是「8001 不可达」；5xx、非 2xx、解析失败与形状不符
   *  都说明 8001 **应答了**，横幅不得把「应答了错」写成「不可达」，也不得
   *  把它写成「未读取」——同一事实在横幅与车道正文里必须是同一个结论。 */
  private summaryCause = "";
  private summaryEpoch?: number;
  private summaryLive?: {
    requests: number;
    tokens: number;
    costCny: string;
    costCnyPartial?: boolean;
    costCnyUnknown?: boolean;
    credits: number;
  };
  /** Derived per-day/per-model series for the overview charts; same lifecycle
   *  as summaryLive (stale keeps it, login reset clears it). */
  private summarySeries?: SummarySeries;
  /** Per-chain fee breakdown (R9-R29 cost lane); same lifecycle as summaryLive. */
  private chainFees?: ChainFee[];
  /** Price table from /api/console/prices (the meter's own prices.json). */
  private priceTable?: PriceTable;
  /** Gateway call-log tail (R9-R30); gated without a management session.
   *  `unavailable` is the server's own sources_unavailable statement (the
   *  requested ids it does NOT read): the 逐次 drill reads it to tell 「该来源
   *  不在调用日志账本内」 from 「尾部读取里没有该来源的记录」. Absent (older
   *  backend) leaves the coverage UNKNOWN — the client never guesses it from
   *  its own request list. */
  /** `reason` 只在 `state !== "live"` 时写入，且必须是实测原因（`HTTP 500`
   *  / 形状不符 / 传输失败）——「读取被拒绝：无凭据会话或 8001 不可达」这种
   *  把两种可能并列写成结论的措辞没有实测证据，不得由前端推断。 */
  private logsFeed?: {
    state: "gated" | "live";
    rows: LogEntry[];
    unavailable?: string[];
    reason?: string;
  };
  /** Seven-day bounded statistics (/api/console/stats), driven per poll. */
  private statsWeek?: StatsWeek;
  /** 近 7 天统计的失败原因（含实测 HTTP 状态）：读取中 / 失败 / 尚未读取
   *  三态分开，缺字段不按 0 显示。 */
  private statsWeekError?: string;
  /** 任意区间聚合（费用页）的读取状态：显式提交才发一次只读 GET；入页的 7 天
   *  读取与 30s 轮询都不带 from/to，所以区间读取不会被轮询放大。 */
  private statsRange?: StatsRangeState;
  private statsRangeInFlight = false;
  /** 健康快照手动重探：null = 本次页面还没点过；`unavailable` 携带实测 HTTP
   *  状态（404 = 当前服务端版本未载入 `refresh=1`）。 */
  private healthRefresh?: HealthRefreshState;
  private healthRefreshInFlight = false;
  /** 已提交「请求取消」的操作 id（POST /api/console/operations/{id}/cancel 已受理）：
   *  受理 ≠ 已取消，终态仍由操作账本回答，所以这里只标「已请求取消」。 */
  private cancelRequested = new Set<string>();
  private cancelInFlight = new Set<string>();
  private cancelError = new Map<string, string>();
  /** A12-3: bounded read-only usage reads — /api/console/usage-tokens
   *  (14-day complete tokens) and /api/console/usage-rates (24h whole-request
   *  output rate). Both are plain read-only GETs: reading them never creates
   *  an operation ledger row. A failed re-read keeps the previous REAL
   *  payload (stale) instead of a fabricated 0 or empty list. */
  private usageTokens?: UsageRead<UsageTokensPayload>;
  private usageRates?: UsageRead<UsageRatesPayload>;
  private usageTokensInFlight = false;
  private usageRatesInFlight = false;
  /** 14-day token-trend series picker: the literal search and the model
   *  series the user explicitly unfolded (key = source|model). Multi-model
   *  series stay folded by default, and the count that stays folded is always
   *  listed — folding never drops rows silently. */
  private tokenSeriesQ = "";
  private tokenSeriesOpen = new Set<string>();
  /** The 14-day card's right-hand breakdown: by source (default) or by model.
   *  By source every slotted source is drawn unless the user hid it here. */
  private tokenGroup: "source" | "model" = "source";
  private tokenSourceHidden = new Set<string>();
  /** F12-3: the colour slot each drawn model series holds (14-day Token chart and
   *  24h rate chart); a series keeps its slot while it stays drawn. */
  private tokenSeriesSlot = new Map<string, number>();
  private rateSeriesSlot = new Map<string, number>();
  /** 24h rate panel drilldown: the hour+source+model bucket whose
   *  per-request rows are shown (undefined = no drilldown open). */
  private rateDrill?: { hour: string; source: string; model: string };
  /** Log-lane feed filters (A12-3): gateway (existing), model and the
   *  relative time window. Client-side reads over the bounded tail rows. */
  private logFeedModel = "";
  private logFeedWindow = "";
  /** 级别（状态码族）筛选：与来源/模型/时段一样只读同一批有界尾读行（客户端
   *  过滤，不另发请求）；它只影响表格显示，挡掉多少条会如实写出。 */
  private logFeedLevel = "";
  /** Remembered model.* operation results (R9-R31 model lane). */
  private actionResults: Record<string, Record<string, unknown>> = {};
  /** 账号页只读查询（认证状态 / 额度）的失败记忆：动作名 → 失败结论（带实测
   *  HTTP 状态或操作终态错误）。失败没有真实返回值，所以与 `actionResults`
   *  分开存：`accountAuthHtml` 有上次真实值时保留它并标陈旧，没有时如实写
   *  「读取失败」，绝不把「没读到」折成「未认证」。成功后清除本键。 */
  private actionResultFailures = new Map<string, string>();
  private modelOverviewRequested = false;
  /** 模型概览读口的真实状态（维护者 2026-09-28）：idle = 尚未发起，loading =
   *  请求在途，live = 读到，transport = 传输失败，http = HTTP 非 2xx（带实测
   *  状态），shape = 响应形状与契约不符。**不得**从 `!sessionLive()` 反推
   *  「8001 不可达」——那只是另一路读口（opsRecent / runtime）没落地。 */
  private modelOverviewState:
    "idle" | "loading" | "live" | "transport" | "http" | "shape" = "idle";
  private modelOverviewStatus = 0;
  /** L-C model lane extras: catalog search filter, checked-model scope
   *  (source|model_id keys), sync-scope mode, history, speedtest memory,
   *  context-probe queue, readback state. Write faces stay behind the
   *  existing two-step confirms; filters are client-side reads. */
  private modelCatalogQ = "";
  private modelChecked = new Set<string>();
  private modelScopeCustom = false;
  private modelScopeSources = new Set<string>();
  private modelScopeConsumers = new Set<string>();
  /** F12-5: the catalog row whose details fill the independent detail pane
   *  (`src|id`); details never expand inside the table, so the table's columns
   *  and row heights stay put. §11.1（2026-10-10）：点选同时触发自动只读读取
   *  （本地已保存设置 + 已有证据）。 */
  private modelPick: string | null = null;
  private modelHistory?: {
    rows: SyncHistoryEntry[];
    total: number;
    skipped: number;
    failed?: boolean;
  };
  private speedResults = new Map<string, SpeedtestResult>();
  private speedBusy: string | null = null;
  /** R11-5/B2: last quota snapshot from /api/console/quota-snapshot (null =
   *  never read). The credits KPI reads THIS (live remaining + checked_at);
   *  the ledger's accumulated credits value stays on the cost page under its
   *  own label (累计消耗积分) — the two must never share one field.
   *  `expires_at` 是后端按上游 /quota 载荷原样透出的到期时点（usagesrv.py
   *  `_qoder_quota_snapshot`：缺席即 null，绝不猜算）；账号卡首屏的
   *  「额度与失效时间」读它，缺席写「未知」。 */
  private quotaSnapshot?: {
    state: string;
    remaining: number | null;
    used: number | null;
    total: number | null;
    checked_at: number;
    error?: string;
    expires_at: string | null;
  };
  /** 实时额度快照的读取状态（与同页 `usageReadNote` 同口径）：读取中 / 读取
   *  失败（带实测 HTTP 状态或传输原因）/ 已读取 三态分开。失败若只 `return`
   *  不留原因，`quotaNoteHtml()` 只能落「尚未读取」分支，把「没读到」写成
   *  「还没问过」——两者都是未知，但恢复路径不同，不得混同。 */
  private quotaSnapshotRead?: {
    state: "loading" | "error" | "gated";
    reason?: string;
  };
  private speedPending = new Map<string, { source: string; model: string }>();
  /** F5: 国际链（qoder_global）额度的有界只读读取状态。读数与检查时间来自
   *  既有的只读适配动作 qoderglobal.quota（console_adapters.py:543 声明为纯
   *  GET /quota 透传、bump_generation_on_success=False，不写上游状态）；本机
   *  8001 目前没有国际链的免操作账本只读端点（国内链有
   *  /api/console/quota-snapshot），所以入页读取复用该只读动作，并用守卫保证
   *  每次入页只发一次、重绘与 30s 轮询都不再发。state: reading = 读取中；
   *  live = 本次读到；stale = 本次读取失败，保留上次真实值并标过期。quota
   *  缺席 = 尚未读到（未知 ≠ 0）。 */
  private qoderGlobalQuota?: {
    state: "reading" | "live" | "stale" | "gated";
    quota?: Record<string, unknown>;
    checkedAt?: number;
    error?: string;
  };
  private qoderGlobalQuotaRequested = false;
  private qoderGlobalQuotaInFlight = false;
  /** P1/R1（2026-10-10）运行/停用门（前端侧）的裁决与拒绝原因：门没放行时如实
   *  写出原因（未知 ≠ 已停止），绝不把没读过伪装成 0 或已停止。 */
  private qoderGlobalQuotaGate?: { verdict: string; detail: string };
  /** P1/R1：门因「运行状态尚未读取」而没发读取时记一次待补读；运行快照落地后
   *  由 probeTick 补一次（一次状态跃迁一次，不是定时重试循环；读取真失败不记，
   *  所以失败不会变成自动循环重发）。 */
  private qoderGlobalQuotaDeferred = false;
  /** P1/R1：人工停用意图。只由**真实启停动作自己的终态**写入（Stopped /
   *  AlreadyStopped = 停用；Started / AlreadyRunning / Restarted = 运行），被拒绝、
   *  端口冲突与归档都不动它——失败的停用不能当成「已经停了」。 */
  private svcStopIntent = new Map<string, "running" | "stopped">();
  /** P1/R1：国内额度快照读取被门拒绝的原因（probeTick 的 30s 读取同样受门约束：
   *  停用 / 未运行 / 未知都不发这次注定被拒的请求，未知不伪装成 0）。 */
  private quotaSnapshotGate?: { verdict: string; detail: string };
  private probeQueue?: {
    state: "loading" | "error" | "live";
    reason?: string;
    payload?: ContextProbePreview;
  };
  /** 契约 3.12b / 3.12c.4：模型页探针说明的展开态（说明是解释性内容，
   *  默认折叠；展开态在重绘与切 lane 后保持）。 */
  private probeIntroOpen = false;
  private previewStale = false;
  /** 提交时范围快照（维护者 2026-09-29 · 模型同步闭环）：预览变更与应用
   *  同步的 range 参数必须完全相同，应用后的回读必须按**同一范围**发起。
   *  `previewScopeParams` = 当前展示的预览结果所按的范围（结果落定时按
   *  op id 写回那次提交的快照，双击/并发预览不会串范围）；`applyScopeParams`
   *  = 最近一次应用**提交**的范围（提交时写入，终态后的自动回读用它）；
   *  `previewScopePending` 是 op id → 提交时快照的在途表。三者都只由
   *  `modelScopeParams()` 这一个构造器产生；null = 尚未提交（即全部默认
   *  范围，回读不带范围参数）。勾选在预览之后变化时，预览面板按当前勾选
   *  与 `previewScopeParams` 的差异如实标「范围已变」，不把另一组范围的
   *  预览冒充成本次应用的范围。 */
  private previewScopeParams: Record<string, unknown> | null = null;
  private applyScopeParams: Record<string, unknown> | null = null;
  private previewScopePending = new Map<string, Record<string, unknown>>();
  /** 应用后自动回读请求携带的范围（null = 入页 / 手动读口，不带范围）。
   *  后端 `GET /api/console/model-pending` 当前不接收范围参数
   *  （usagesrv.py:3209 给 handler 传 `{}`），所以响应是**全局概览**：
   *  `modelOverviewHtml` 按实测写出这一点，绝不把它说成「已按范围回读」。 */
  private modelReadbackScope: Record<string, unknown> | null = null;
  /** §11.2 模型设置（页面会话内的表单草稿）：字段值按 `source|model_id` 记，
   *  否则 30s 轮询 / 读面落定后的重绘会抹掉正在编辑的值（先例 `shareKeyEdit`）。
   *  保存结果不另存一份，从 `actionResults` 按动作读回（model.settings_save 的
   *  source/model_id 决定那份结果属于哪个模型）；发布结果从统一同步窗口的
   *  model.sync_apply 结果读回。 */
  private msAlias = new Map<string, string>();
  private msEditRevision = new Map<string, number>();
  private msEditsAwaitingReadback = new Map<string, number>();
  private msCtx = new Map<string, string>();
  private msCtxTokens = new Map<string, string>();
  private msEffort = new Map<string, string>();
  /** §11.1（2026-10-10）单击模型触发的自动读取状态（按 source|model_id）：
   *  固定区域写「读取中 / 读取失败 + 重试 / 门未放行 + 原因」；未知字段不可
   *  当零或默认值。 */
  private msAutoRead = new Map<string, MsAutoRead>();
  /** 设置读面在途时被推迟的自动读取：在途读面覆盖全部模型，落定后按**当时
   *  选中**补一次（不是定时重试循环；读取真失败不记，失败由固定区域的重试
   *  入口负责）。 */
  private msAutoReadDeferred: string | null = null;
  /** §11.1 迟到结果不落当前选中：设置读面（model.context_overrides）已经按
   *  模型身份提交过读取的 key，以及**最后一次提交属于哪个 key**。读面是按
   *  来源的目录读，所以「载荷覆盖当前模型」= 同一个来源；另一个来源的迟到
   *  载荷既不算「已读取」，也不清掉当前模型已保存的基线，更不挡住当前模型
   *  自己的一次补读。 */
  private msSettingsRead = new Set<string>();
  private msSettingsReadKey: string | null = null;
  /** §11.3 统一同步窗口：最近一次「确认范围并生成预览」的绑定（范围签名 +
   *  selection 签名 + preview_token）。范围、草稿、目录或客户端输入任一变化使
   *  旧预览失效——签名不同就不展示「确认同步」，也不复用旧 token。 */
  private syncBind?: {
    scopeSig: string;
    selectionSig: string;
    token: string;
    selection: Record<string, number>;
  };
  /** §11.3 同步窗口范围模式（复用既有 `modelScopeCustom`）：false = 全部
   *  （后端默认）；true = 只同步选中（空选拒绝）。单网关窗口固定本来源
   *  （scope_mode=selected + sources=[本来源]），不受该开关影响。 */
  /** §11.4 模型页「更多 / 能力与证据 / 测试模型」的展开态（原生开合会被
   *  重绘折回默认态，与既有折叠一样由字段还原）。 */
  private modelMoreOpen = false;
  private modelLaneMoreOpen = false;
  private msEvidenceOpen = false;
  private msTestOpen = false;
  /** §11.4「恢复默认」待确认的模型 key（null = 未发起）：明确列出恢复哪些
   *  字段与值，形成可预览草稿，不直接发布。 */
  private msRestoreKey: string | null = null;
  /** 目录读口「M365 暂停范围与依据」详情是否展开（原生开合态会被原地重绘折回
   *  默认态，所以和既有折叠一样由字段还原）。 */
  private modelIdDetailKey: string | null = null;
  /** 应用写操作终态后的**自动回读**是否失败：失败时概览仍挂着上一次
   *  操作前的真实读数，这条标记让 `modelOverviewHtml` 把它标成「操作已完成、
   *  最新状态读取失败」并给只读重试，而不是把旧读数当成本次结果。手动读口
   *  （进入车道 / 重试读取）失败时保留既有标记，只有真的读到才清掉。 */
  private pendingReadbackFailed = false;
  /** 契约 3.12b / 3.12c.4：费用页口径长解释的展开态。展开态必须在
   *  `.wb-content` 原地 morph 与切 lane 后保持（先例 `sharePreconditionsOpen`），
   *  否则每次 30s 轮询重绘都会把它折回默认态。 */
  private credDetailsOpen = false;
  /** 契约 3.12c.4：账号卡「链说明与原始返回」折叠的展开态（维护者 2026-09-29
   *  ·Plan-Revision 9 逐页落点「账号」：OAuth / 探针长句与只读查询的原始返回
   *  收进每张卡自己的折叠）。四张卡各有一个折叠宿主，开合态按服务 id 记在集合
   *  里；30s 轮询与切 lane 的重绘都按字段还原，先例 `probeIntroOpen`。 */
  private accountFoldOpen = new Set<number>();
  /** Rev17 gateway workspace (lane 1). `gwSel`: "models" = the pinned 全部模型
   *  entry, otherwise a chain's stable service id. */
  private gwSel: "models" | number = "models";
  private gwTab: "account" | "models" = "account";
  /** Rev18 客户端连接 seam. The backend's GatewayConnectionBinding owns the read
   *  (select / clear on switch / late-response guard) and the copy decision, and
   *  hands the page its current view and the copy outcome here. `gwConnService`
   *  is the service identity the slot currently belongs to ("" = none),
   *  `gwConnReading` marks a read in flight, `gwConnSeq` is the page-side
   *  generation that keeps a superseded read from installing. The plaintext key
   *  lives only in this page session — never storage, logs or operation records.
   *  The page only shows a view whose service is the selected gateway's. */
  private gwConnection: GatewayConnectionView | null = null;
  private gwCopyFeedback: {
    service: string;
    field: "url" | "key";
    ok: boolean;
    reason?: string;
  } | null = null;
  private gwConnBinding: GatewayConnectionBinding | null = null;
  private gwConnService = "";
  private gwConnReading = false;
  private gwConnSeq = 0;
  /** Candidate-only demo of the block (#demo-connection): synthetic bodies from
   *  the backend fixtures, labelled as such; never present in a production build. */
  private connDemoBuild:
    ((service: string, state: string) => GatewayConnectionView) | null = null;
  private connDemoState = "present";
  private connDemoFeedback: "" | "ok" | "fail" = "";
  private gwQuery = "";
  private gwAuth: "all" | "ok" | "attention" | "unknown" = "all";
  /** Sidebar order by stable service id. Every completed move is written as one
   *  complete sequence through the backend's order-preference module, whose
   *  namespace keeps this preview build apart from production; the in-page order
   *  stays authoritative and `gwOrderNote` states what actually happened. */
  private gwOrder: number[] = CHAINS.map((s) => s.id);
  private gwOrderPrefs: GatewayOrderPreferences | null = null;
  private gwOrderNote = "顺序拖动后会在本浏览器记住。";
  /** Narrow screens show the list OR the detail; this is the detail side. */
  private gwNarrowDetail = false;
  /** Pointer reorder in progress: rows are moved live in the DOM and every panel
   *  repaint waits for the drop (a repaint would morph the rows mid-drag). */
  private gwDrag?: {
    id: number;
    pointerId: number;
    startOrder: number[];
    moved: boolean;
  };
  private gwRepaintDeferred = false;
  private gwQTimer = 0;
  private gwEventsInstalled = false;
  /** One-line announcement of the last reorder for assistive tech. */
  private gwAnnounce = "";
  /** Open state of the lane-level explanation folds (`laneNoteFoldHtml`, 2026-09-29
   *  redesign), keyed by fold name. Same contract as the named fold fields above:
   *  the click records the target state and every repaint restores it. */
  private noteFoldOpen = new Set<string>();
  /** Preserve .wb-content scroll across the apply→readback repaint. */
  private keepScroll = false;
  /** P4/R4：内层滚动区域（列表 / 详情各自）按稳定名字保存的读取位置。
   *  面板原地补丁会重建某些兄弟节点，浏览器自己的滚动锚定不保证跨重绘保留，
   *  所以每次重绘前按 `data-wb-scroll` 取值、补丁后写回。 */
  private panelScroll = new Map<string, { top: number; for: string }>();
  /** §10.1 设计决定：两侧调宽的存储键 `gateway-console-pane-width-v1:${namespace}`，
   *  日志来源侧栏 200–480px（默认 300）、模型详情栏 260–560px（默认 320）；
   *  拖动 / ← → 每次 16px，Home / End 到最小 / 最大，Escape 恢复默认。 */
  private paneWidths: Record<string, number> = {};
  private paneWidthPrefs: PaneWidthPreferences | null = null;
  private paneWidthLoaded = false;
  private paneWidthNote = "拖动分隔条调宽，仅在本浏览器记住。";
  /** 拖动分隔条期间禁止重绘（重绘会把列宽写回旧值）。 */
  private splitDrag?: {
    id: string;
    pointerId: number;
    startX: number;
    startWidth: number;
    moved: boolean;
  };
  /** Management-session derived live data; undefined = gated (fixture view). */
  private runtime?: Record<string, RuntimeEntry>;
  private actions?: ActionRow[];
  private opsRecent?: {
    state: "gated" | "live";
    rows: OpRow[];
    reason?: string;
  };
  private opStatus?: OpStatus;
  /** R9-R37 notice pool: one entry per submitted operation from submit to
   *  finish, keyed client-locally then rebound to the server operation id. */
  private feedbackEntries = new Map<string, FeedbackEntry>();
  private feedbackSeq = 0;
  /** System banner dismissed for the current 8001-unreachable episode. */
  private bannerDismissed = false;
  /** R9-R38 run-log detail: sources registry plus the live detail view.
   *  state survives back-navigation and lane switches; seq aborts stale
   *  responses when the source flips so rows never cross gateways. */
  private logSources?: LogSourceRow[];
  /** P3/R3：一个来源的**全部已注册分类**合并成一份快照。`targets` = 该来源的
   *  目标数，`read` = 真正读到（status ok）的目标数：全部读到才是 ok，部分目标
   *  未读写 partial（并保留已读目标的行），全部未读沿用第一个失败状态。行仍按
   *  目标顺序拼接，`truncated` 取或。 */
  private logOverview = new Map<
    string,
    {
      status: string;
      rows: LogEntryRow[];
      truncated: boolean;
      targets?: number;
      read?: number;
    }
  >();
  private logOverviewLoading = false;
  private logOverviewError = "";
  private logOverviewSeq = 0;
  private logOverviewFilter: LogKind | "all" = "all";
  private logRawOpen = new Set<string>();
  /** C4：最近一次总览渲染的「身份 → 该次渲染绑定的行」。复制点击按渲染时身份取原文，
   *  不在点击时按动态列表重算；身份不在表里就如实说「已不在当前列表」，绝不复制另一条事件。 */
  private logOverviewRenderRows = new Map<string, LogEntryRow>();
  /** C4：单来源详情的同款渲染身份表（原文展开 / 复制共用同一身份）。 */
  private logDetailRenderRows = new Map<string, LogEntryRow>();
  private logSourceQuery = "";
  private logSourceOrder: string[] = [];
  private logOrderPrefs?: GatewayOrderPreferences;
  private logOrderLoaded = false;
  private logOrderNote = "拖拽来源或按 Alt+↑ / Alt+↓ 排序，仅在本浏览器记住。";
  private logDrag?: { pointerId: number; startOrder: string[]; moved: boolean };
  private logOverviewCheckedAt = "";
  private logSourcesFailed = false;
  /** 来源注册表失败的实测原因（HTTP 状态或传输原因）：失败句必须能说出
   *  读到了什么，而不是一句泛化的「读取失败」。 */
  private logSourcesError = "";
  private logDetail?: LogDetailState;
  private logDetailOpen = false;
  /** Rev16 A16-3: the page a 「运行日志」 button was pressed on, so the detail's
   *  back button returns there (null = opened from the log page itself). */
  private logReturnLane: number | null = null;
  /** Focus the detail title once it renders after a cross-page jump. */
  private logFocusPending = false;
  private logDetailSeq = 0;
  /** Unique gradient id per rendered trend chart (the total series' soft fill). */
  private chartFillSeq = 0;
  private logSourcesRequested = false;
  /** The log target asked for while the source registry was still loading; only
   *  the latest one is kept and opened when the read settles (A16-3). */
  private logPending: { source: string; category?: string } | null = null;
  private logFeedRequested = false;
  private statsRequested = false;
  private logQTimer?: number;
  private modelQTimer?: number;
  /** A12-3: debounce timer for the 14-day series search input. */
  private tokenQTimer?: number;
  /** L-D: the usage-ledger feed keeps a per-gateway selection (空=全部）；
   *  the detail sub-view always showed one source already. */
  private logFeedSrc = "";
  private confirmBtn?: HTMLElement;
  /** Two-step confirm state that survives lane repaints: renderPanel rebuilds
   *  the lane HTML wholesale (probe ticks repaint several times per minute)
   *  and replaces the armed node, so the armed selector + label are kept and
   *  re-applied after each rebuild. Cleared by every disarm path. */
  private armedConfirm: { sel: string; text: string; key: string } | null =
    null;
  /** 「已复制」flash state that survives morph repaints: the in-place panel
   *  patch resets a flashed label within the flash window (probe ticks
   *  repaint repeatedly), so the flash is re-applied after every rebuild
   *  until its deadline. */
  private flashCopiedState: {
    sel: string;
    base: string;
    until: number;
  } | null = null;
  private opPollAbort = false;
  /** Share lane (R36-2 narrow facade): status/keys refresh on every lane
   *  entry, on every share.* terminal state, on the lane refresh button and
   *  on the poll tick once the lane has been visited. shareFetchInFlight
   *  dedupes overlapping reads (the lane re-renders many times per minute)
   *  while still allowing repeat reads. Plaintext lives ONLY in memory for
   *  this page session — never localStorage, never operation records, never
   *  generic feedback; lane switches and repaints keep it, only the 清除
   *  control or a page reload drops it (R9-R46). */
  private shareStatus?: ShareStatusState;
  private shareStatusRead?: Promise<void>;
  private shareKeys?: ShareKeysState;
  /** 分享用量读口（`/gw/usagesrv/api/console/share-usage`）：与状态/密钥同入口
   *  读取，请求序号守卫保证被超越的在途响应不覆盖新状态（照 fetchShareKeys）。 */
  private shareUsage?: ShareUsageState;
  private shareUsageSeq = 0;
  /** S5 公网巡检读面（`GET .../share-management/public`）：与状态/密钥/用量
   *  同入口刷新，不新增轮询定时器。按钮禁用只做展示，真正的互斥由服务端
   *  `share:public`、`share:public-state` 与 `share:orchestration` 承担。 */
  private publicShare?: PublicShareState;
  private publicShareRead?: Promise<void>;
  private publicShareClientInstance?: BackendClient;
  /** 脱敏诊断导出的最近一次结果（成功或失败），卡片内常驻显示。 */
  private publicShareExport?: {
    state: "ok" | "failed" | "unconfirmed";
    text: string;
  };
  /** R11-F2 (Plan-Revision 11): plaintext per key_id, memory-only. Seeded
   *  by the create/rotate immediate responses and by the narrow per-key
   *  readback; NEVER written to localStorage, URLs, notices, operation
   *  records or logs. Legacy hash-only rows are never requested.
   *  P1 (2026-09-24): each entry carries the row_version it was read at.
   *  A row whose version moved (rotation in another tab, concurrent op)
   *  invalidates the cached plaintext instead of showing/copying the old
   *  value, and the readback sends expected_row_version so the backend
   *  answers version_conflict rather than a plaintext from a newer row. */
  private shareKeySecrets = new Map<
    string,
    { secret: string; rowVersion: number | null }
  >();
  private shareKeySecretBusy = new Set<string>();
  /** P1 (2026-09-24): monotone per-key read sequence — a response only
   *  applies while it is still the newest request for that key, so an
   *  in-flight read can never overwrite a newer value (a rotation's
   *  plaintext or a newer read). */
  private shareKeySecretSeq = new Map<string, number>();
  /** P2 (2026-09-24): monotone sequence for the keys LIST read — a read
   *  that is superseded while in flight must not apply its rows (its prune
   *  would otherwise drop a plaintext the newer state already holds). */
  private shareKeysSeq = 0;
  private shareKeyTests = new Map<string, ShareKeyTest>();
  private shareSecurityDetailsOpen = false;
  /** R11-F2: per-key readback failure reason shown IN PLACE
   *  (legacy / undecryptable / version_conflict) — the honest state must be
   *  visible where the key is, not only in a transient notice. */
  private shareKeySecretNotes = new Map<string, string>();
  private shareFetchInFlight = false;
  private shareFetchQueued = false;
  private shareLaneVisited = false;
  /** 最近一次单层重启动作自己的前后层读数（Rev19 A15 维护前后比较）：只取
   *  该动作终态结果里的 `before` / `after` 同层 map（console_share_actions
   *  `_diag_pair`），页面只比较这一次动作的前后、不重读一遍冒充。 */
  private shareRestartCompare?: {
    action: string;
    at: number;
    before: Record<string, unknown>;
    after: Record<string, unknown>;
  };
  /** Share lane tabs (R3): the compact overview stays outside the tabs, the
   *  chosen tab and every disclosure state survive polling and lane switches. */
  private shareTab: "keys" | "diagnostics" | "usage" = "keys";
  /** Focus the selected tab once, right after the tab click/keyboard repaint. */
  private shareTabFocus = false;
  /** After an inline key action repaints, hand focus back to the exact action
   *  button that was clicked (the low-frequency actions are now expanded in the
   *  row, not behind a floating layer). */
  private shareKeyFocusPending?: { id: string; action: string };
  private sharePlaintext?: {
    kind: "created" | "rotated";
    name: string;
    value: string;
    at: number;
  };
  /** P2 (2026-09-24): whether the backend actually stored a same-identity
   *  ciphertext for the plaintext in the modal — a DPAPI failure degrades
   *  the row to hash-only, so the success note must not claim 已写入本机
   *  for a key that can only ever show 旧密钥不可恢复. null = unknown. */
  private shareKeyPlaintextViewable: boolean | null = null;
  /** Key-create modal state (R9-R46): the modal DOM lives under .wb-main —
   *  outside the morphed .wb-content — so repaints never reset it. A create
   *  keeps the modal open and flips it to the success view; 完成 closes. */
  private shareKeyFormOpen = false;
  private shareKeyModalView: "form" | "success" = "form";
  private shareKeyFocusName = false;
  /** P2 (2026-09-24): a create request is in flight — the modal stays
   *  open until its response lands, so the submit button must be disabled
   *  meanwhile or one click could create two keys. */
  private shareKeySubmitting = false;
  private shareKeyError = "";
  private shareConfirm?: HTMLElement;
  /** The armed share-lane confirm by identity (lifecycle/maintenance action, or
   *  key operation + key id). The in-place patch keeps the armed node but resets
   *  its label from the fresh HTML, so after every repaint the arm is re-applied
   *  to the button that still confirms the same thing - or dropped. The second
   *  click is never taken from a node that no longer reads 「确认…」. */
  private shareArmed: { key: string; text: string } | null = null;
  /** Inline rename / per-key-cap edit (replaces window.prompt): the edited
   *  cell switches to an input + 保存/取消; the typed value mirrors into
   *  state so morph repaints cannot wipe it. */
  private shareKeyEdit?: { id: string; field: "name" | "cap"; value: string };
  /** Plaintext section extras: mask toggle and the post-clear note survive
   *  re-renders as class state. */
  private shareSecretHidden = false;
  private shareSecretCleared = false;
  /** Block ② five-layer preconditions detail: collapsed by default, auto
   *  opened when a layer shows failure/contradiction (A36-1). */
  private sharePreconditionsOpen = false;
  /** Keep share disclosures open across polling and lane changes. */
  private sharePanelsOpen = new Set<string>();
  /** 分享页「每 Key 明细」的开合态：与 `sharePreconditionsOpen` 同一问题——
   *  没有状态字段时，每次重绘（含 30s 轮询）都会把它折回默认态。 */
  private shareKeyDetailsOpen = new Set<string>();
  /** F12-8: keys drawn on the per-key daily fee chart (by key_id, at most
   *  TOKEN_SERIES_MAX); until the user picks, the two costliest are shown. */
  private shareKeyCompare = new Set<string>();
  private shareKeyCompareTouched = false;
  private shareKeySlot = new Map<string, number>();
  /** R9-R40 task 4, R9-R46 rework: inline terminal summaries keyed by lane
   *  block (laneKey), each with its own honest state (ok / failed /
   *  unconfirmed). Rendered as the .wb-svc-result chip owned by the block's
   *  row/ops area; fades out 10s after it was written and is cleared by the
   *  block's next action. */
  private laneSummaries: Record<
    string,
    { text: string; state: "ok" | "failed" | "unconfirmed"; at: number }
  > = {};
  /** R9-R40 task 2: unfolded failure groups (keyed by action name). */
  private failuresOpen = new Set<string>();
  /** A lane-level explanation fold (2026-09-29 redesign): definitions and long
   *  口径 sentences that used to sit open under every lane move behind one
   *  closed-by-default fold, so the first reading layer is readings and actions.
   *  Only explanation goes in here — never a failure, a stale mark or a count the
   *  page is asserting (those keep their own resident surface, contract 3.12c). */
  private laneNoteFoldHtml(
    key: string,
    title: string,
    bodyHtml: string
  ): string {
    const open = this.noteFoldOpen.has(key);
    return `<details class="wb-secondary-details wb-note-fold"${open ? " open" : ""} data-wb-note-fold="${escapeHtml(key)}"><summary title="${escapeHtml(title)} · 点击展开 / 收起">${escapeHtml(title)}${wbFoldMark()}</summary>${bodyHtml}</details>`;
  }
  /** R9-R40 task 4: which lane block an action belongs to — model.* collapses
   *  to the model lane; service/account actions map to the owning service row
   *  via target or registry action names. Unknown actions get a private key
   *  that is never rendered (no invented shared slot). */
  private laneKey(actionName: string, target: string): string {
    if (actionName.startsWith("model.")) return "model";
    for (const s of SERVICES) {
      if (target && s.targets?.includes(target)) return `svc${s.id}`;
      if (s.statusAction?.action === actionName) return `svc${s.id}`;
      if ((s.manageActions ?? []).some((a) => a.action === actionName))
        return `svc${s.id}`;
    }
    return `act:${actionName}`;
  }
  /** Inline terminal result chip (R9-R46): one state-colored pill bound to
   *  the block that ran the action — success/failure/unconfirmed each carry
   *  their own color; the text stays the summarizeResult product (or the
   *  failure/uncertainty reason). Long text truncates with a title. */
  private svcResultChip(key: string): string {
    const s = this.laneSummaries[key];
    if (!s) return "";
    return `<span class="wb-svc-result wb-svc-result-${s.state}" data-svc-result="${escapeHtml(key)}" title="${escapeHtml(s.text)}">${escapeHtml(s.text)}</span>`;
  }
  /** Model-lane variant of the chip: same component, one row under the ops
   *  bar instead of inside a service row. */
  private modelResultChipHtml(): string {
    const chip = this.svcResultChip("model");
    return chip ? `<div class="wb-svc-result-row">${chip}</div>` : "";
  }
  /** Write one terminal summary and schedule its 10s auto-fade. Idempotent:
   *  the timer only deletes its own generation (at-match), so a newer
   *  summary on the same block is never killed by an older timer; the
   *  next-action clear in submitAction still applies. The 400ms fade runs on
   *  the live node only on the lanes that render chips (service / model). */
  private setLaneSummary(
    key: string,
    text: string,
    state: "ok" | "failed" | "unconfirmed"
  ) {
    this.laneSummaries[key] = { text, state, at: Date.now() };
    const at = this.laneSummaries[key]!.at;
    // No repaint here: every caller ends in fbFinish(), whose repaint also puts
    // the chip on the overview list row (PR11 Q17) — repainting here as well
    // rebuilt the whole overview twice per terminal state.
    window.setTimeout(() => {
      const cur = this.laneSummaries[key];
      if (!cur || cur.at !== at) return;
      const node =
        this.lane === 0 || this.lane === 1
          ? this.root.querySelector<HTMLElement>(
              `[data-svc-result="${CSS.escape(key)}"]`
            )
          : null;
      if (node && !this.stage.classList.contains("reduce-motion")) {
        node.classList.add("wb-svc-result-fade");
        window.setTimeout(() => {
          const latest = this.laneSummaries[key];
          if (latest && latest.at === at) {
            delete this.laneSummaries[key];
            this.renderPanel();
          }
        }, 400);
        return;
      }
      delete this.laneSummaries[key];
      if (this.lane === 0 || this.lane === 1) this.renderPanel();
    }, 10_000);
  }
  /** Overview master-detail selection (PR11, maintainer 2026-09-30): the chain
   *  the overview's dark service panel shows. The overview always shows one
   *  (chat2api by default), independently of the account lane's toggled detail. */
  private ovService = 0;
  private summaryInFlight = false;
  constructor(
    private stage: HTMLElement,
    private onMode: () => void,
    private onLane: (lane: number) => void
  ) {
    // PR11 shell (maintainer 2026-09-30, direction「青柠」): no sidebar. One sticky
    // top region carries the bar (brand, the grouped navigation track, quick jump,
    // appearance and the label toggle), the 8001 banner and the page head (page
    // name, the console-wide run verdict and the evidence time), so the banner
    // never paints over the head and the verdict stays in view on every lane.
    // The label preference is owned by console-entry.ts and lands here as
    // html[data-console-rail] (collapsed = icons only). The compatibility clock
    // node stays hidden in the console.
    // 2026-10-31 redesign (ADR 0001): the console is the local role of a shared
    // shell — a glass sidebar (environment marker, grouped navigation, appearance
    // and sidebar toggle) beside a solid content column whose sticky head carries
    // the page name, the console-wide run verdict, the evidence time and the
    // quick-jump capsule. Every data-wb-* / class hook the delegation and the
    // console gate rely on keeps its name.
    stage.insertAdjacentHTML(
      "beforeend",
      `<section class="workbench app-shell" hidden aria-label="本地控制台">
      <aside class="shell-sidebar" aria-label="本地控制台导航">
        <div class="shell-side-panel shell-glass">
          <div class="shell-env" data-role-state="checking"><div class="shell-env-name"><span class="shell-env-mark" aria-hidden="true"></span><span>本地 · 自用网关</span></div><div class="shell-env-sub" data-shell-role-text>管理本机服务 · 正在核对后端角色</div></div>
          <div class="wb-time" hidden><time class="wb-clock"></time><div class="wb-date"></div></div>
          <nav class="wb-nav shell-nav" aria-label="工作台导航">${NAV_GROUPS.map((group, g) => `<div class="wb-nav-group" role="group" aria-labelledby="wb-nav-group-${g}"><span class="wb-nav-group-label shell-nav-label" id="wb-nav-group-${g}">${escapeHtml(group.label)}</span>${group.lanes.map((i) => `<button data-wb-lane="${i}" aria-pressed="false" aria-label="${escapeHtml(names[i])}">${wbIcon(LANE_ICONS[i] ?? "overview")}${wbNavLabel(names[i])}${wbIconHint(names[i])}</button>`).join("")}</div>`).join("")}</nav>
          <div class="shell-side-foot">
            <button type="button" class="shell-icon-btn" data-action="settings" aria-label="外观设置">${wbIcon("settings")}${wbIconHint("外观设置")}</button>
            <span class="shell-side-text">外观</span>
            <button type="button" class="shell-icon-btn" data-action="rail-toggle" aria-label="收起侧栏" aria-expanded="true">${wbIcon("rail")}${wbIconHint("收起侧栏")}</button>
          </div>
        </div>
      </aside>
      <div class="wb-main shell-main">
        <div class="wb-top">
          <header class="wb-head"><div class="wb-head-title"><h1 class="wb-title"></h1><button type="button" class="wb-info" hidden></button></div><div class="wb-head-meta"><span class="wb-head-status" data-state="unknown" role="status"><i class="wb-dot wb-dot-unknown" aria-hidden="true"></i><span class="wb-head-status-text"></span></span><span data-wb-share-head-public class="wb-badge wb-badge-idle" role="status" hidden></span><span class="wb-index"></span></div>
            <div class="wb-jump shell-glass">${wbIcon("search")}<input class="wb-jump-input" type="text" placeholder="跳转到服务 / 页面…" aria-label="工作台快速跳转" autocomplete="off" spellcheck="false"/><div class="wb-jump-list" hidden role="listbox"></div></div>
          </header>
          <div class="shell-role-notice" data-shell-role-notice role="status" hidden></div>
          <div class="wb-banner" hidden></div>
        </div>
        <div class="wb-content"></div>
      </div>
    </section>`
    );
    this.root = stage.querySelector(".workbench")!;
    this.loadGatewayOrder();
    void this.loadConnectionDemo();
    // The sticky top region wraps with the nav, so its height is published as
    // --wb-top-h: sticky panes inside wb-main stop below it instead of sliding
    // under it, and size themselves to the space that is left.
    const top = this.root.querySelector<HTMLElement>(".wb-top");
    if (top && typeof ResizeObserver !== "undefined") {
      new ResizeObserver(() =>
        this.root.style.setProperty(
          "--wb-top-h",
          `${Math.ceil(top.getBoundingClientRect().height)}px`
        )
      ).observe(top);
    }
    // F12-3: a chart point taken with the pointer shows the chart's selected state
    // (band + tooltip) instead of the keyboard focus ring; Chrome matches
    // :focus-visible for a clicked SVG tab stop, which drew a black frame. The
    // mode is kept on the root - outside the patched content, whose repaint drops
    // any attribute the fresh HTML does not carry - and any key press ends it, so
    // Tab still gets the ring.
    this.root.addEventListener("pointerdown", (event) => {
      if ((event.target as Element).closest?.(".wb-area-hotspot"))
        this.root.setAttribute("data-wb-chart-pointer", "");
    });
    this.root.addEventListener(
      "keydown",
      () => this.root.removeAttribute("data-wb-chart-pointer"),
      true
    );
    // A16-3: the user moving focus themselves ends the post-jump title focus.
    this.root.addEventListener("focusin", (event) => {
      if (
        this.logFocusPending &&
        !(event.target as Element).closest?.("[data-log-title]")
      )
        this.logFocusPending = false;
    });
    // Overview master-detail list (PR11): ↑ / ↓ move the selection between the
    // chains and keep keyboard focus on the newly selected row.
    this.root.addEventListener("keydown", (event) => {
      const item = (event.target as Element).closest<HTMLElement>(
        "[data-wb-md-item]"
      );
      if (!item || (event.key !== "ArrowDown" && event.key !== "ArrowUp"))
        return;
      event.preventDefault();
      const ids = CHAINS.map((s) => s.id);
      const at = ids.indexOf(Number(item.dataset.wbMdItem));
      const next =
        ids[
          (at + (event.key === "ArrowDown" ? 1 : ids.length - 1)) % ids.length
        ];
      // A confirm armed in the detail pane belongs to the chain it was armed on;
      // moving the selection disarms it (a keypress never reaches the click
      // handler's disarm).
      this.clearConfirm();
      this.ovService = next;
      this.renderPanel();
      this.root
        .querySelector<HTMLElement>(`[data-wb-md-item="${next}"]`)
        ?.focus();
    });
    // R11-F1: delegated at DOCUMENT level — the notice pool now lives on
    // document.body (viewport layer), so its buttons must be reachable by the
    // same delegation that serves the in-root buttons. The branches below only
    // act on elements carrying the specific data-wb-* attributes, so widening
    // the listener scope changes nothing for other clicks.
    document.addEventListener("click", (event) => {
      const button = (event.target as Element).closest<HTMLButtonElement>(
        "button"
      );
      if (!button) return;
      if (this.confirmBtn && this.confirmBtn !== button) this.clearConfirm();
      if (this.shareConfirm && this.shareConfirm !== button) this.disarmShare();
      if (this.handleGatewayClick(button)) return;
      const noticeKey = button.dataset.wbNoticeDismiss;
      if (noticeKey !== undefined) {
        const entry = this.feedbackEntries.get(noticeKey);
        if (entry) {
          entry.pinned = false;
          // Non-running entries leave the pool for good; a running one only
          // unpins and keeps receiving poll updates.
          if (entry.state !== "running") this.feedbackEntries.delete(noticeKey);
          this.renderPanel();
        }
        return;
      }
      if (button.dataset.wbBannerDismiss !== undefined) {
        this.bannerDismissed = true;
        this.renderNotices();
        return;
      }
      if (button.dataset.wbLogRawToggle !== undefined) {
        const key = button.dataset.wbLogRawToggle;
        if (this.logRawOpen.has(key)) this.logRawOpen.delete(key);
        else {
          if (this.logRawOpen.size >= 200)
            this.logRawOpen.delete(this.logRawOpen.values().next().value!);
          this.logRawOpen.add(key);
        }
        this.renderPanel();
        return;
      }
      if (button.dataset.wbLogSource !== undefined) {
        const source = button.dataset.wbLogSource;
        if (source) this.openLogDetail(source);
        else {
          this.logDetailSeq++;
          this.logDetailOpen = false;
          this.logPending = null;
          this.logReturnLane = null;
          this.renderPanel();
        }
        return;
      }
      if (button.dataset.wbLogDetailRefresh !== undefined && this.logDetail) {
        this.logDetail.follow = true;
        this.logDetail.newCount = 0;
        this.logDetail.loading = true;
        this.renderPanel();
        void this.fetchLogEntries(this.logDetail, false);
        return;
      }
      if (button.dataset.wbLogOverviewCopy !== undefined) {
        // C4：按渲染时身份取该次渲染绑定的原文。后台刷新可能已经改了
        // this.logOverview（逐批 set、末尾才重绘），按点击时重算的下标会取到
        // 另一条事件；身份不在渲染表里就如实反馈，不回退成下标。
        const row = this.logOverviewRenderRows.get(
          button.dataset.wbLogOverviewCopy
        );
        if (row) void this.copyLogRaw(row.message, button);
        else button.textContent = "该事件已不在当前列表，请刷新总览后重试";
        return;
      }
      const logOpenSrc = button.dataset.logOpen;
      if (logOpenSrc) {
        this.openLogDetail(logOpenSrc, undefined, this.lane);
        return;
      }
      const logReopen = button.dataset.logReopen;
      if (logReopen) {
        // A failed registry read is retried explicitly, then the same source reopens.
        this.logSourcesRequested = true;
        this.logSourcesFailed = false;
        this.logSourcesError = "";
        if (this.logDetail)
          this.logDetail = {
            ...this.logDetail,
            status: "loading",
            errorCode: "",
            loading: true,
          };
        this.logPending = { source: logReopen };
        this.renderPanel();
        void this.fetchLogSources();
        return;
      }
      if (button.dataset.wbLogSourcesRetry !== undefined) {
        // 来源注册表的显式重试：只读重读一次，放开「本次已请求」守卫。
        this.retryLogSources();
        return;
      }
      if (button.dataset.wbOpsRetry !== undefined) {
        // 操作账本的显式重试：只读重读一次（不产生操作行）。
        void this.fetchRecentOps().then(() => this.renderPanel());
        return;
      }
      if (button.dataset.wbLogFeedRetry !== undefined) {
        // 调用日志尾读的显式重试（维护者 2026-09-28）：同一口的有界只读重读一次，
        // 不产生操作行、不改数据语义。
        void this.fetchLogs(true).then(() => this.renderPanel());
        return;
      }
      if (button.dataset.wbLogOverviewRefresh !== undefined) {
        void this.fetchLogOverview();
        return;
      }
      if (button.dataset.wbLogOverviewFilter !== undefined) {
        const kind = button.dataset.wbLogOverviewFilter;
        if (kind === "all" || Object.hasOwn(logKindLabels, kind))
          this.logOverviewFilter = kind as LogKind | "all";
        this.renderPanel();
        return;
      }
      const logCopyId = button.dataset.wbLogCopy;
      if (logCopyId !== undefined) {
        // 单行日志详情的完整原文复制（维护者 2026-09-28）：取该行的完整 message
        // （多行全文，不是首层那个折叠前缀），反馈沿用既有的一次性「已复制」闪动。
        // C4：按渲染时身份取该次渲染绑定的行；身份不在表里就如实反馈，不回退成下标。
        const row = this.logDetailRenderRows.get(logCopyId);
        if (row) {
          void this.copyLogRaw(row.message, button);
        } else {
          button.textContent = "该事件已不在当前列表，请刷新来源后重试";
        }
        return;
      }
      const logReqId = button.dataset.logReq;
      if (logReqId !== undefined && this.logDetail) {
        // F12-6 查看该请求相关记录: the backend's exact request_id filter over
        // the read window; an empty value clears it.
        this.logDetail.requestId = logReqId;
        this.logDetail.requestEcho = "";
        this.logDetail.nextCursor = null;
        this.logDetail.expanded = new Set();
        this.renderPanel();
        void this.fetchLogEntries(this.logDetail, false);
        return;
      }
      if (button.dataset.logBack !== undefined) {
        this.logDetailOpen = false; // rows stay saved for re-entry
        const back = this.logReturnLane;
        this.logReturnLane = null;
        if (back !== null && back !== this.lane) this.select(back);
        else this.renderPanel();
        return;
      }
      const logCat = button.dataset.wbLogCat;
      if (logCat && this.logDetail) {
        this.logDetail.category = logCat;
        this.logDetail.nextCursor = null;
        this.renderPanel();
        void this.fetchLogEntries(this.logDetail, false);
        return;
      }
      const logExpandId = button.dataset.logExpand;
      if (logExpandId !== undefined && logExpandId !== "" && this.logDetail) {
        // C4：展开 / 收起按渲染时身份记账，不按下标——加载更早记录会前置整段
        // 行，下标会把已展开的原文落到另一条事件上。
        this.logDetail.expanded.add(logExpandId);
        this.renderPanel();
        return;
      }
      const logCollapseId = button.dataset.logCollapse;
      if (
        logExpandId === undefined &&
        logCollapseId !== undefined &&
        logCollapseId !== "" &&
        this.logDetail
      ) {
        this.logDetail.expanded.delete(logCollapseId);
        this.renderPanel();
        return;
      }
      if (button.dataset.logOlder !== undefined && this.logDetail) {
        void this.fetchLogEntries(this.logDetail, true);
        return;
      }
      if (button.dataset.logWrap !== undefined && this.logDetail) {
        this.logDetail.wrap = !this.logDetail.wrap;
        this.renderPanel();
        return;
      }
      if (button.dataset.logResume !== undefined && this.logDetail) {
        this.logDetail.follow = true;
        this.logDetail.newCount = 0;
        void this.fetchLogEntries(this.logDetail, false);
        return;
      }
      if (button.dataset.logClear !== undefined && this.logDetail) {
        const d = this.logDetail;
        d.q = "";
        d.since = "";
        d.until = "";
        d.requestId = "";
        this.logOverviewFilter = "all";
        void this.fetchLogEntries(d, false);
        return;
      }
      const svcAction = button.dataset.wbSvcAction;
      if (
        svcAction === "start" ||
        svcAction === "stop" ||
        svcAction === "restart"
      ) {
        if (this.isArmed(button)) {
          this.clearConfirm();
          void this.submitAction(
            `service.${svcAction}`,
            button.dataset.wbTarget ?? ""
          );
        } else {
          this.armConfirm(button, svcAction);
        }
        return;
      }
      const acctAction = button.dataset.wbAcctAction;
      if (acctAction) {
        // 账号页动作分级：主动作 = 只读的认证/额度查询（一次点击直达，无副作用）；
        // 需人工登录的动作（*.login：弹出真实浏览器窗口、由维护者本人完成登录）
        // 单独一级，先武装二次确认再执行——既有标题文案就是「执行前需确认」，
        // 所以这里补上它承诺的门，不自动打开登录。
        if (isLoginAction(acctAction)) {
          if (this.isArmed(button)) {
            this.clearConfirm();
            void this.submitAction(
              acctAction,
              button.dataset.wbAcctTarget ?? ""
            );
          } else {
            this.armConfirmLabel(
              button,
              acctAction === "account.login"
                ? "确认检查并按需登录？（认证有效时无需登录）"
                : "确认打开登录窗口？（真实弹出浏览器窗口，由维护者本人完成登录）"
            );
          }
          return;
        }
        void this.submitAction(acctAction, button.dataset.wbAcctTarget ?? "");
        return;
      }
      // S5 公网巡检按钮：一键复查只提交只读动作；受限重试走分享车道既有的
      // 两段确认（确认文案明确会按 30 分钟预算重发映射/重启 TS）；导出只下载
      // 后端白名单生成的诊断 JSON。
      const publicAction = button.dataset.wbSharePublic;
      if (publicAction) {
        if (publicAction === "export") {
          void this.downloadPublicShareExport();
          return;
        }
        if (publicAction === "check") {
          void this.submitAction("share.public_check", "share");
          return;
        }
        if (publicAction === "retry") {
          if (
            this.shareConfirm === button &&
            this.shareArmed?.key === shareConfirmKey(button)
          ) {
            this.shareConfirm = undefined;
            this.shareArmed = null;
            button.classList.remove("wb-confirm");
            button.textContent =
              button.dataset.wbBaseLabel ?? button.textContent ?? "";
            delete button.dataset.wbBaseLabel;
            void this.submitAction("share.public_retry", "share");
          } else {
            this.disarmShare();
            this.shareConfirm = button;
            button.dataset.wbBaseLabel ??= button.textContent ?? "";
            button.classList.add("wb-confirm");
            button.textContent =
              "确认受限重试？（将按 30 分钟预算重发映射/重启 TS）";
            this.shareArmed = {
              key: shareConfirmKey(button),
              text: button.textContent ?? "",
            };
          }
          return;
        }
        return;
      }
      // R3 share tabs: a click selects the tab and focuses it after the repaint.
      // The overview and the address stay outside the tab panels.
      const shareTab = button.dataset.wbShareTab;
      if (
        shareTab === "keys" ||
        shareTab === "diagnostics" ||
        shareTab === "usage"
      ) {
        this.setShareTab(shareTab, true);
        return;
      }
      // Share lane lifecycle (A36-1 block 1): all five actions arm a confirm
      // first — backend_login may start 8110 and open its browser, start/stop touch
      // the share chain, estop/retry_stop are the stop paths. The armed
      // button keeps its base label in dataset for the confirm round-trip.
      const shareAction = button.dataset.wbShareAction;
      if (shareAction) {
        if (
          this.shareBrokerUnavailable() &&
          SHARE_BROKER_ACTIONS.includes(shareAction)
        ) {
          this.disarmShare();
          this.renderPanel();
          return;
        }
        if (
          this.shareConfirm === button &&
          this.shareArmed?.key === shareConfirmKey(button)
        ) {
          this.shareConfirm = undefined;
          this.shareArmed = null;
          button.classList.remove("wb-confirm");
          button.textContent =
            button.dataset.wbBaseLabel ?? button.textContent ?? "";
          delete button.dataset.wbBaseLabel;
          void this.submitAction(shareAction, "share");
        } else {
          this.disarmShare();
          this.shareConfirm = button;
          button.dataset.wbBaseLabel ??= button.textContent ?? "";
          button.classList.add("wb-confirm");
          button.textContent =
            shareAction === "share.estop"
              ? "确认紧急停止？"
              : shareAction === "share.stop"
                ? "确认停止分享？"
                : shareAction === "share.retry_stop"
                  ? "确认继续关闭？"
                  : shareAction === "share.backend_login"
                    ? "确认启动并检查 8110？需要时打开专用登录，认证有效则跳过"
                    : shareAction === "share.backend_reauthenticate"
                      ? "确认仅重新认证 8110？"
                      : shareAction === "share.backend_restart"
                        ? "确认仅重启运行中的 8110？（不冷启动；将暂停接收并排空请求）"
                        : shareAction === "share.gateway_restart"
                          ? "确认仅恢复 / 重启 8100？"
                          : shareAction === "share.repair_limits"
                            ? "确认暂停公网并修复？（完成后保持关闭）"
                            : "确认开启分享？";
          this.shareArmed = {
            key: shareConfirmKey(button),
            text: button.textContent ?? "",
          };
        }
        return;
      }
      const shareKeyAction = button.dataset.wbShareKey;
      if (shareKeyAction === "test") {
        const row = this.shareKeyRow(button.dataset.wbShareKeyId ?? "");
        if (row) void this.testShareKey(row);
        return;
      }
      if (button.dataset.wbShareSecurityDetails !== undefined) {
        this.shareSecurityDetailsOpen = !this.shareSecurityDetailsOpen;
        this.renderPanel();
        return;
      }
      if (shareKeyAction === "create") {
        this.shareKeyFormOpen = true;
        this.shareKeyModalView = "form";
        this.shareKeyError = "";
        this.shareKeySubmitting = false;
        this.shareKeyPlaintextViewable = null;
        this.shareKeyFocusName = true;
        this.renderPanel();
        return;
      }
      if (
        shareKeyAction === "cancel-create" ||
        shareKeyAction === "modal-complete"
      ) {
        this.closeShareKeyModal();
        return;
      }
      if (shareKeyAction === "submit-create") {
        const form = button.closest("form");
        const name =
          form
            ?.querySelector<HTMLInputElement>("[data-wb-share-key-name]")
            ?.value.trim() ?? "";
        const capRaw =
          form?.querySelector<HTMLInputElement>("[data-wb-share-key-cap]")
            ?.value ?? "";
        const cap = Number(capRaw);
        if (!name) {
          this.shareKeyError = "名称不能为空";
          this.renderPanel();
          return;
        }
        if (capRaw !== "" && (!Number.isInteger(cap) || cap < 1 || cap > 64)) {
          this.shareKeyError = "每 Key 并发需为 1–64 整数";
          this.renderPanel();
          return;
        }
        // P2 (2026-09-24): the modal STAYS open while the create is in
        // flight — its own comment says create flips it to the success view,
        // but closing it here made that view unreachable and pushed every
        // error into the lane below. The submit button is disabled until the
        // response lands, so one create can never be submitted twice.
        this.shareKeySubmitting = true;
        this.renderPanel();
        void this.createShareKey(name, capRaw === "" ? undefined : cap);
        return;
      }
      if (
        shareKeyAction === "rotate" ||
        shareKeyAction === "revoke-terminate" ||
        shareKeyAction === "delete"
      ) {
        if (
          this.shareConfirm === button &&
          this.shareArmed?.key === shareConfirmKey(button)
        ) {
          this.shareConfirm = undefined;
          this.shareArmed = null;
          button.classList.remove("wb-confirm");
          button.textContent =
            button.dataset.wbBaseLabel ?? button.textContent ?? "";
          delete button.dataset.wbBaseLabel;
          this.shareKeyFocusPending = {
            id: button.dataset.wbShareKeyId ?? "",
            action: shareKeyAction,
          };
          void this.shareKeyOp(
            shareKeyAction,
            button.dataset.wbShareKeyId ?? ""
          );
        } else {
          this.disarmShare();
          this.shareConfirm = button;
          button.dataset.wbBaseLabel ??= button.textContent ?? "";
          button.classList.add("wb-confirm");
          button.title =
            shareKeyAction === "rotate"
              ? "旧明文立即失效"
              : shareKeyAction === "revoke-terminate"
                ? "撤销后该 Key 的在途请求会被终止"
                : "删除后不可恢复";
          button.textContent =
            shareKeyAction === "rotate"
              ? "确认轮换"
              : shareKeyAction === "revoke-terminate"
                ? "确认撤销"
                : "确认删除";
          this.shareArmed = {
            key: shareConfirmKey(button),
            text: button.textContent ?? "",
          };
        }
        return;
      }
      if (shareKeyAction === "toggle-enable") {
        const row = this.shareKeyRow(button.dataset.wbShareKeyId ?? "");
        if (!row) return;
        this.shareKeyFocusPending = {
          id: row.id ?? "",
          action: "toggle-enable",
        };
        void this.patchShareKey(
          row.id ?? "",
          { enabled: !row.enabled },
          Number(row.row_version)
        );
        return;
      }
      if (shareKeyAction === "reveal-secret") {
        // 受控的一次性展示（契约 4.8）：一次点击 = 一次只读读回，不自动读、不重复读。
        const row = this.shareKeyRow(button.dataset.wbShareKeyId ?? "");
        if (!row) return;
        this.revealShareKeySecret(row);
        return;
      }
      if (shareKeyAction === "copy-secret") {
        // R11-F2: copy THIS row's in-memory plaintext; nothing is persisted
        // and the secret never enters notices/URLs/logs.
        const cached = this.shareKeySecrets.get(
          button.dataset.wbShareKeyId ?? ""
        );
        if (!cached) return;
        const secret = cached.secret;
        // 契约 3.13b：这个按钮是纯图标，反馈仍须是文字，且基值取 `innerHTML`
        // （取 textContent 会把图标在闪动结束时永久抹掉）；`aria-label` 不随
        // 反馈抖动改写。
        const base = button.innerHTML;
        const done = () => {
          button.textContent = "已复制";
          window.setTimeout(() => {
            if (button.isConnected) button.innerHTML = base;
          }, 1200);
        };
        try {
          const result = navigator.clipboard?.writeText(secret);
          if (result) {
            void result.then(done, done);
          } else {
            done();
          }
        } catch {
          done();
        }
        return;
      }
      if (shareKeyAction === "rename") {
        const row = this.shareKeyRow(button.dataset.wbShareKeyId ?? "");
        if (!row) return;
        // Inline edit (R9-R46): the name cell swaps to an input — no
        // window.prompt. The editor takes focus.
        this.shareKeyEdit = {
          id: row.id ?? "",
          field: "name",
          value: row.name ?? "",
        };
        this.renderPanel();
        return;
      }
      if (shareKeyAction === "adjust-cap") {
        const row = this.shareKeyRow(button.dataset.wbShareKeyId ?? "");
        if (!row) return;
        this.shareKeyEdit = {
          id: row.id ?? "",
          field: "cap",
          value:
            row.max_concurrency === undefined
              ? ""
              : String(row.max_concurrency),
        };
        this.renderPanel();
        return;
      }
      if (shareKeyAction === "cancel-edit") {
        const edit = this.shareKeyEdit;
        this.shareKeyFocusPending = edit
          ? {
              id: edit.id,
              action: edit.field === "name" ? "rename" : "adjust-cap",
            }
          : undefined;
        this.shareKeyEdit = undefined;
        this.renderPanel();
        return;
      }
      if (shareKeyAction === "save-edit") {
        const edit = this.shareKeyEdit;
        const row = edit ? this.shareKeyRow(edit.id) : undefined;
        if (!edit || !row) {
          this.shareKeyEdit = undefined;
          this.renderPanel();
          return;
        }
        const raw = (
          button.closest("td")?.querySelector<HTMLInputElement>("input")
            ?.value ?? edit.value
        ).trim();
        if (edit.field === "name") {
          if (!raw || raw === (row.name ?? "")) {
            this.shareKeyEdit = undefined;
            this.renderPanel();
            return;
          }
          this.shareKeyFocusPending = { id: row.id ?? "", action: "rename" };
          this.shareKeyEdit = undefined;
          void this.patchShareKey(
            row.id ?? "",
            { name: raw },
            Number(row.row_version)
          );
          return;
        }
        const cap = Number(raw);
        if (!Number.isInteger(cap) || cap < 1 || cap > 64) {
          this.shareKeyError = "每 Key 并发需为 1–64 整数";
          this.renderPanel();
          return;
        }
        if (raw === String(row.max_concurrency ?? "")) {
          this.shareKeyFocusPending = {
            id: row.id ?? "",
            action: "adjust-cap",
          };
          this.shareKeyEdit = undefined;
          this.renderPanel();
          return;
        }
        this.shareKeyFocusPending = { id: row.id ?? "", action: "adjust-cap" };
        this.shareKeyEdit = undefined;
        void this.patchShareKey(
          row.id ?? "",
          { max_concurrency: cap },
          Number(row.row_version)
        );
        return;
      }
      if (button.dataset.wbShareSecretToggle !== undefined) {
        // Mask/unmask the resident plaintext (•••• toggle); state survives
        // repaints as class state.
        this.shareSecretHidden = !this.shareSecretHidden;
        this.renderPanel();
        return;
      }
      if (button.dataset.wbShareSecretClear !== undefined) {
        // Drop the in-memory plaintext; the keys block keeps a muted note
        // (rotate is the re-issue path). Never persisted anywhere.
        this.sharePlaintext = undefined;
        this.shareSecretCleared = true;
        this.renderPanel();
        return;
      }
      if (
        button.dataset.wbShareSecretCopy !== undefined &&
        this.sharePlaintext
      ) {
        void navigator.clipboard?.writeText(this.sharePlaintext.value);
        this.flashCopied(button);
        return;
      }
      const shareCopy = button.dataset.wbShareCopy;
      if (shareCopy !== undefined) {
        void navigator.clipboard?.writeText(shareCopy);
        this.flashCopied(button);
        return;
      }
      const modelIdCopy = button.dataset.wbModelIdCopy;
      if (modelIdCopy !== undefined) {
        // PR9 B6：目录单元格「完整模型 id」详情里的复制。复制值取渲染时绑定的
        // 原 id（不是从被截断的显示文本里拼回来的），反馈沿用既有的一次性
        // 「已复制」闪动。按钮在 <summary> 内，所以要 preventDefault：否则浏览器
        // 会按原生 <summary> 的激活行为顺手把折叠开合一次（复制不该改变开合态）。
        event.preventDefault();
        void navigator.clipboard?.writeText(modelIdCopy);
        this.flashCopied(button);
        return;
      }
      if (button.dataset.wbShareCopyRecovery !== undefined) {
        // The recovery entry is a fixed local .bat; the page can hand the
        // command to the operator but never runs an elevated process itself.
        event.preventDefault();
        void navigator.clipboard?.writeText("Recover-ShareControl.bat");
        this.flashCopied(button);
        return;
      }
      if (button.dataset.wbShareRefresh !== undefined) {
        // Manual refetch of both share reads (lane-title button / error
        // retry): same read-only path as lane entry, never an operation row.
        this.refreshShareData();
        return;
      }
      if (button.dataset.wbShareDiag !== undefined) {
        // A15: the layered read is a read-only operation (one ledger row per
        // run), so it runs when asked, never on lane entry or the poll.
        void this.submitAction("share.diagnostics", "share");
        return;
      }
      const shareCompare = button.dataset.wbShareCompare;
      if (shareCompare !== undefined) {
        this.shareKeyCompareTouched = true;
        if (this.shareKeyCompare.has(shareCompare))
          this.shareKeyCompare.delete(shareCompare);
        else if (this.shareKeyCompare.size < TOKEN_SERIES_MAX)
          this.shareKeyCompare.add(shareCompare);
        this.renderPanel();
        this.root
          .querySelector<HTMLElement>(
            `[data-wb-share-compare="${CSS.escape(shareCompare)}"]`
          )
          ?.focus({ preventScroll: true });
        return;
      }
      const modelPick = button.dataset.wbModelPick;
      if (modelPick !== undefined) {
        // F12-5: a row's details open in the detail pane beside the table; focus
        // stays on the row's own button so the keyboard keeps its place.
        // §11.1（2026-10-10）：单击模型同时自动读取本地已保存设置与已有证据
        // （两个只读读面各一次，见 ensureModelAutoRead）；读取中 / 失败 /
        // 门未放行都写在详情固定区域并可重试。
        this.modelPick = modelPick;
        this.ensureModelAutoRead(modelPick);
        this.renderPanel();
        this.root
          .querySelector<HTMLElement>(
            `[data-wb-model-pick="${CSS.escape(modelPick)}"]`
          )
          ?.focus({ preventScroll: true });
        return;
      }
      const msAutoRead = button.dataset.wbMsAutoread;
      if (msAutoRead === "settings" || msAutoRead === "evidence") {
        const key = button.dataset.wbMsKey ?? "";
        if (!key) return;
        this.forceModelAutoRead(key, msAutoRead);
        return;
      }
      if (button.dataset.wbMsMore !== undefined) {
        this.modelMoreOpen = !this.modelMoreOpen;
        this.renderPanel();
        return;
      }
      if (button.dataset.wbModelLaneMore !== undefined) {
        this.modelLaneMoreOpen = !this.modelLaneMoreOpen;
        this.renderPanel();
        return;
      }
      if (button.dataset.wbSyncConfirm !== undefined) {
        const source = button.dataset.wbSyncSource ?? "";
        this.confirmSyncRange(source);
        return;
      }
      if (button.dataset.wbSyncPublish !== undefined) {
        const source = button.dataset.wbSyncSource ?? "";
        // The reviewed preview is the confirmation boundary; one click publishes it.
        this.publishSyncWindow(source);
        return;
      }
      if (button.dataset.wbMsRestoreCancel !== undefined) {
        this.msRestoreKey = null;
        this.renderPanel();
        return;
      }
      if (button.dataset.wbMsAdoptClient !== undefined) {
        const key = button.dataset.wbMsKey ?? "";
        const conflict = this.msConflictFor(key);
        const tokens = conflict?.clientTokens;
        if (
          tokens === null ||
          tokens === undefined ||
          !Number.isSafeInteger(tokens) ||
          tokens < 1 ||
          tokens > 10_000_000
        )
          return;
        this.msEditRevision.set(key, (this.msEditRevision.get(key) ?? 0) + 1);
        this.msCtx.set(key, "manual");
        this.msCtxTokens.set(key, String(tokens));
        this.renderPanel();
        return;
      }
      if (button.dataset.wbMsRestore !== undefined) {
        const key = button.dataset.wbMsKey ?? "";
        if (!key) return;
        const at = key.indexOf("|");
        if (at <= 0) return;
        const source = key.slice(0, at),
          modelId = key.slice(at + 1);
        if (this.msRestoreKey !== key) {
          this.msRestoreKey = key;
          this.renderPanel();
          return;
        }
        this.msRestoreKey = null;
        if (this.actionInFlight("model.settings_save")) return;
        const expected = this.msExpectedDraftVersion(key);
        const params: Record<string, unknown> = {
          source,
          model_id: modelId,
          alias: null,
          contextSource: null,
          contextTokens: null,
          defaultEffort: null,
        };
        if (expected !== null) params.expected_draft_version = expected;
        void this.submitAction("model.settings_save", "model", params);
        return;
      }
      // §11.1/§11.4（2026-10-10）：「以目录值显示／以已验证值显示」切换已删除
      // （改为证据区并列呈现）；「校验档位」跳队列不再作为成功入口——测试模型
      // 独立入口如实写明本轮不发起真实请求。
      // R9-R40 task 2: the failure group's chip really unfolds its rows —
      // `failuresOpen` had a declaration and two readers but NO writer and no
      // click branch, so the button looked clickable and did nothing.
      if (button.dataset.wbProbeIntro !== undefined) {
        this.probeIntroOpen = !this.probeIntroOpen;
        this.renderPanel();
        return;
      }
      if (button.dataset.wbCredDetails !== undefined) {
        this.credDetailsOpen = !this.credDetailsOpen;
        this.renderPanel();
        return;
      }
      if (button.dataset.wbFailToggle !== undefined) {
        const action = button.dataset.wbFailToggle;
        if (this.failuresOpen.has(action)) this.failuresOpen.delete(action);
        else this.failuresOpen.add(action);
        this.renderPanel();
        return;
      }
      const modelSection = button.dataset.wbModelSection;
      if (
        modelSection === "catalog" ||
        modelSection === "sync" ||
        modelSection === "probe"
      ) {
        this.modelSection = modelSection;
        this.renderPanel();
        return;
      }
      const modelAction = button.dataset.wbModelAction;
      // §11.1/R7（2026-10-10）刷新模型：单网关「刷新本来源目录」带显式 sources
      // Only confirmed running sources are queried; stopped/unknown sources are skipped.
      if (modelAction === "model.catalog") {
        const source = button.dataset.wbCatalogSource ?? "";
        const candidates = source
          ? [source]
          : Object.keys(MODEL_SOURCE_TARGET).filter((s) => s !== "m365");
        const sources = candidates.filter(
          (s) =>
            this.upstreamReadGate(MODEL_SOURCE_TARGET[s] ?? "").verdict ===
            "running"
        );
        if (!sources.length) {
          this.setLaneSummary(
            "model",
            "没有已确认运行的来源，本次未访问上游。请先启动需要查询的网关。",
            "unconfirmed"
          );
          this.renderPanel();
          return;
        }
        void this.submitAction("model.catalog", "model", {
          sources: JSON.stringify(sources),
        });
        return;
      }
      // §11.2（2026-10-10）模型设置：只保留「保存设置」主按钮（model.settings_save）
      // ——保存只写待同步草稿、只发实际改动字段并携带读面版本；未改字段不以空值
      // 清除。预览 / 发布在统一同步窗口（§11.3）；「恢复默认」在「更多」里。
      const msAction = button.dataset.wbMsAction;
      if (msAction) {
        const key = button.dataset.wbMsKey ?? "";
        const at = key.indexOf("|");
        if (at <= 0) return;
        const source = key.slice(0, at),
          modelId = key.slice(at + 1);
        if (msAction === "read") {
          if (this.actionInFlight("model.context_overrides")) return;
          this.forceModelAutoRead(key, "settings");
          return;
        }
        if (msAction === "evidence-read") {
          if (this.actionInFlight("model.context_probe_preview")) return;
          this.forceModelAutoRead(key, "evidence");
          return;
        }
        if (msAction === "use-measured") {
          // 证据区显式「用作上下文设置」：只填入待保存设置（measured 来源），
          // 不自动应用、不保存——用户仍要点「保存设置」形成草稿。
          const floor = this.msEvidenceFloor(key);
          if (floor === null) return;
          this.msEditRevision.set(key, (this.msEditRevision.get(key) ?? 0) + 1);
          this.msCtx.set(key, "measured");
          this.msCtxTokens.set(key, String(floor));
          this.renderPanel();
          return;
        }
        if (msAction === "save") {
          if (this.actionInFlight("model.settings_save")) return;
          const read = this.msContextState(key);
          const built = this.msChangedFields(
            key,
            read.state === "ok" ? read.ctx : null
          );
          if (!built || !built.changed.length) return;
          const params: Record<string, unknown> = {
            source,
            model_id: modelId,
            ...built.fields,
          };
          const expected = this.msExpectedDraftVersion(key);
          if (expected !== null) params.expected_draft_version = expected;
          void this.submitAction("model.settings_save", "model", params);
          return;
        }
        return;
      }
      if (modelAction === "model.speedtest") {
        const source = button.dataset.speedSource ?? "";
        const model = button.dataset.speedModel ?? "";
        if (!source || !model || this.speedBusy) return;
        this.speedBusy = `${source}|${model}`;
        this.renderPanel();
        void this.submitAction("model.speedtest", "model", { source, model });
        return;
      }
      if (button.dataset.wbQuotaRefresh !== undefined) {
        // R11-5/B2 manual refresh: bounded read-only re-read, then repaint
        // the KPI in place (no operation row, no charged request).
        void this.fetchQuotaSnapshot(true).then(() => {
          this.applySummary();
          this.renderPanel();
        });
        return;
      }
      if (button.dataset.wbHealthRefresh !== undefined) {
        // 手动重探：一次有界只读 GET（?refresh=1），在途守卫吸收重复点击。
        void this.manualReprobe();
        return;
      }
      if (button.dataset.wbStatsRun !== undefined) {
        // 任意区间聚合：只有显式提交才发一次只读 GET（入页读取与 30s
        // 轮询都不带 from/to，所以区间读取不会被轮询放大）。
        const from =
          this.root.querySelector<HTMLInputElement>("[data-wb-stats-from]")
            ?.value ?? "";
        const to =
          this.root.querySelector<HTMLInputElement>("[data-wb-stats-to]")
            ?.value ?? "";
        void this.runStatsRange(from, to);
        return;
      }
      if (button.dataset.wbStatsClear !== undefined) {
        this.statsRange = undefined;
        this.renderPanel();
        return;
      }
      const opCancel = button.dataset.wbOpCancel;
      if (opCancel) {
        void this.cancelOperation(opCancel);
        return;
      }
      if (button.dataset.wbUsageRefresh !== undefined) {
        // A12-3 manual refresh: bounded read-only re-read of BOTH usage
        // endpoints (no operation row, no charged request).
        this.ensureUsageReads(true);
        return;
      }
      if (button.dataset.wbQoderGlobalQuotaRefresh !== undefined) {
        // F5 manual refresh: explicit user intent re-issues the bounded
        // read-only international quota read (the entry guard would skip it).
        this.ensureQoderGlobalQuota(true);
        return;
      }
      if (button.dataset.wbRateDrill !== undefined) {
        // A12-3 24h panel: open the per-request rows of one
        // hour+source+model bucket (client-side read over the ledger tail).
        const key = button.dataset.wbRateDrill;
        const [hour, source, model] = key.split("|");
        if (hour && source && model) this.rateDrill = { hour, source, model };
        this.renderPanel();
        return;
      }
      if (button.dataset.wbRateClose !== undefined) {
        this.rateDrill = undefined;
        this.renderPanel();
        return;
      }
      if (button.dataset.wbSeriesToggle !== undefined) {
        // A12-3 trend series fold/unfold: display-level only — the series
        // numbers stay in the list either way, and the folded count is shown.
        const key = button.dataset.wbSeriesToggle;
        if (key.startsWith("src:")) {
          const src = key.slice(4);
          if (this.tokenSourceHidden.has(src))
            this.tokenSourceHidden.delete(src);
          else this.tokenSourceHidden.add(src);
        } else if (this.tokenSeriesOpen.has(key))
          this.tokenSeriesOpen.delete(key);
        else if (this.tokenSeriesOpen.size < TOKEN_SERIES_MAX)
          this.tokenSeriesOpen.add(key);
        this.renderPanel();
        this.root
          .querySelector<HTMLElement>(
            `[data-wb-series-toggle="${CSS.escape(key)}"]`
          )
          ?.focus({ preventScroll: true });
        return;
      }
      const tokGroup = button.dataset.wbTokGroup;
      if (tokGroup === "source" || tokGroup === "model") {
        this.tokenGroup = tokGroup;
        this.renderPanel();
        this.root
          .querySelector<HTMLElement>(`[data-wb-tok-group="${tokGroup}"]`)
          ?.focus({ preventScroll: true });
        return;
      }
      if (modelAction === "model.readback") {
        // Client-local read-only refetch of the pending overview: not an
        // operation row, never a re-submitted write or charged probe.
        // 同范围重试：仍按应用提交时的范围参数发起（没有应用过就不带范围）。
        // 不在这里先清 `pendingReadbackFailed`：那会让「重试又失败」时
        // 「操作已完成、最新状态读取失败」那一行消失，只剩更弱的
        // 「同步概览读取失败」——标记改由这次真实读取的结果决定
        // （读到才清，见 fetchModelPending）。
        void this.fetchModelPending(false, this.applyScopeParams);
        return;
      }
      if (modelAction === "model.scope_toggle") {
        this.modelScopeCustom = !this.modelScopeCustom;
        this.renderPanel();
        return;
      }
      if (modelAction === "model.history") {
        void this.submitAction("model.sync_history", "model", { limit: 20 });
        return;
      }
      if (modelAction) {
        void this.submitAction(modelAction, "model");
        return;
      }
      if (button.dataset.wbSvcDetail !== undefined) {
        const id = Number(button.dataset.wbDetail);
        if (this.lane === 0) {
          // Overview master-detail: a click selects the chain the panel shows and
          // never toggles it off. Keyboard position survives the repaint when the
          // click came from the list itself.
          const fromList = button.dataset.wbMdItem !== undefined;
          this.ovService = id;
          this.renderPanel();
          if (fromList)
            this.root
              .querySelector<HTMLElement>(`[data-wb-md-item="${id}"]`)
              ?.focus();
          return;
        }
        // Off the overview (Rev17): a chain's detail lives in the gateway workspace.
        if (CHAINS.some((s) => s.id === id)) {
          this.gwSel = id;
          this.gwTab = "account";
          this.gwNarrowDetail = true;
          this.select(1);
        }
        return;
      }
      if (button.dataset.wbLane !== undefined) {
        this.select(+button.dataset.wbLane);
        onLane(this.lane);
      }
    });
    // P2 (2026-09-26): the key-create modal's Escape and Tab contract lives
    // at DOCUMENT level. The backdrop's own keydown only fires while focus is
    // INSIDE the modal, and the modal is the last node of `.wb-main` — Tab from
    // 取消 walked out to the page's nav (取消 → body → 总览 → 账号 …), after which
    // Escape could no longer close the dialog at all. Escape now closes wherever
    // focus is (and hands focus back to 新增密钥), and Tab cycles inside the
    // dialog, pulling focus back if it ever left.
    document.addEventListener("keydown", (event) => {
      if (!this.shareKeyFormOpen) return;
      const backdrop = this.root.querySelector<HTMLElement>(
        ".wb-share-modal-backdrop"
      );
      if (!backdrop || backdrop.hidden) return;
      if (event.key === "Escape") {
        event.preventDefault();
        this.closeShareKeyModal();
        return;
      }
      if (event.key !== "Tab") return;
      // A hidden view's controls are not tab stops: the form view and the
      // success view are two faces of the same dialog, and only one is shown.
      const stops = [
        ...backdrop.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        ),
      ].filter((element) => !element.closest("[hidden]"));
      if (!stops.length) return;
      const first = stops[0]!,
        last = stops[stops.length - 1]!;
      const active = document.activeElement;
      const inside = active instanceof HTMLElement && backdrop.contains(active);
      if (inside && (event.shiftKey ? active !== first : active !== last))
        return;
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    });
    // R3 share tabs: ArrowLeft / ArrowRight / Home / End move the selection
    // and keep focus on the newly selected tab. Ids are stable, so a repaint
    // never hands the selected tab another panel.
    this.root.addEventListener("keydown", (event) => {
      const tab = (event.target as Element).closest?.<HTMLElement>(
        "[data-wb-share-tab]"
      );
      if (
        !tab ||
        !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
      )
        return;
      const tabs = [
        ...this.root.querySelectorAll<HTMLElement>(
          '[role="tab"][data-wb-share-tab]'
        ),
      ].filter(
        (el) =>
          el.dataset.wbShareTab === "keys" ||
          el.dataset.wbShareTab === "diagnostics" ||
          el.dataset.wbShareTab === "usage"
      );
      const at = tabs.indexOf(tab);
      if (at < 0) return;
      event.preventDefault();
      const next =
        event.key === "Home"
          ? 0
          : event.key === "End"
            ? tabs.length - 1
            : (at + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) %
              tabs.length;
      const key = tabs[next]?.dataset.wbShareTab;
      if (key === "keys" || key === "diagnostics" || key === "usage")
        this.setShareTab(key, true);
    });
    this.apply({});
    // 契约 3.12c.4：折叠状态必须在重绘后保持。原生 <details> 的开合由浏览器
    // 完成，但 renderPanel 会重建这段 HTML（30s 轮询与切 lane 都重绘），所以
    // 开合态写进类字段：点击时先记录目标态，重绘后由字段还原。九个折叠宿主
    // 都在这里登记——分享页的准备条件与「每 Key 明细」原先只有状态字段却没有任何
    // 写入者，于是每次重绘都折回默认态；模型页「同步」分区的「探测队列」与
    // 「同步任务」原先既没有状态字段也没有宿主，同样每次都折回默认关闭；账号页
    // 四张卡各自的折叠（维护者 2026-09-29）按服务 id 记在 `accountFoldOpen`；
    // 模型页 M365「暂停范围与依据」与目录单元格「完整模型 id」两条折叠的写入分支
    // 原先不可达——缺的正是下面选择器清单里的宿主项，`modelIdDetailKey` 因此恒为空、
    // 每次重绘都折回默认态（维护者 2026-09-29 门禁定向复现），本轮一并接上。
    this.root.addEventListener("click", (event) => {
      const target = event.target as Element;
      const summary = target.closest?.(
        "[data-wb-probe-intro] > summary, [data-wb-cred-details] > summary, [data-wb-share-preconditions] > summary, [data-wb-share-key-details] > summary, [data-wb-share-panel] > summary, [data-wb-model-pause] > summary, [data-wb-account-fold] > summary, [data-wb-note-fold] > summary, [data-wb-ms-more] > summary, [data-wb-ms-evidence] > summary, [data-wb-ms-test] > summary, [data-wb-model-lane-more] > summary"
      );
      if (!summary) {
        // Plan-Revision 9 ②（维护者 2026-09-28）：折叠宿主是表格单元格时，整格都是命中盒
        // ——命中盒判据在 td 的四条边缘各真实点一次，单元格自己的空白（不是折叠体内部）
        // 也必须切换。这里再放宽一格：同一行（`tr`）里任何不是折叠体内部的位置都算
        // 这一行的折叠命中盒——门禁取几何与真实点击之间若有一次重绘（分享车道三个只读
        // 读口落定会重建表格），格子会位移，按格取的点会落到相邻格上（门禁实测：
        // 1536x864/dsf125/dark/live lane 6「上缘内 4px 命中 TD.wb-fee-num」）。
        // 点折叠体内部不重复切换，其余文字与动作一个不动。
        const row = target.closest?.("tr");
        const inside = target.closest?.("details[data-wb-share-key-details]");
        const host = row?.querySelector<HTMLDetailsElement>(
          "details[data-wb-share-key-details]"
        );
        if (host && !inside) {
          if (host.open)
            this.shareKeyDetailsOpen.delete(host.dataset.wbShareKeyDetails!);
          else this.shareKeyDetailsOpen.add(host.dataset.wbShareKeyDetails!);
          host.open = !host.open;
        }
        return;
      }
      const host = summary.parentElement as HTMLDetailsElement | null;
      if (!host) return;
      // 点击时 `open` 仍是切换前的值，所以目标态是它的反面。这里只记录状态，
      // 不在这里重绘：重绘会把原生刚切换的开合态再折回默认态。
      if (host.dataset.wbProbeIntro !== undefined)
        this.probeIntroOpen = !host.open;
      else if (host.dataset.wbCredDetails !== undefined)
        this.credDetailsOpen = !host.open;
      else if (host.dataset.wbSharePreconditions !== undefined)
        this.sharePreconditionsOpen = !host.open;
      else if (host.dataset.wbSharePanel !== undefined) {
        if (host.open) this.sharePanelsOpen.delete(host.dataset.wbSharePanel);
        else this.sharePanelsOpen.add(host.dataset.wbSharePanel);
      } else if (host.dataset.wbShareKeyDetails !== undefined) {
        if (host.open)
          this.shareKeyDetailsOpen.delete(host.dataset.wbShareKeyDetails);
        else this.shareKeyDetailsOpen.add(host.dataset.wbShareKeyDetails);
      } else if (host.dataset.wbModelPause !== undefined) {
        // PR9 B7：M365 暂停读口的「暂停范围与依据」详情（维护者 2026-09-28 逐页
        // 落点 · 模型）。同一读口一个宿主，开合态记在字段里；原生开合态会被
        // 30s 轮询重绘折回默认态，所以和既有折叠一样只记录、不在这里重绘。
        this.modelIdDetailKey = host.open ? null : "m365-pause";
      } else if (host.dataset.wbNoteFold !== undefined) {
        // Lane-level explanation folds (2026-09-29 redesign), keyed by name.
        const key = host.dataset.wbNoteFold;
        if (host.open) this.noteFoldOpen.delete(key);
        else this.noteFoldOpen.add(key);
      } else if (host.dataset.wbMsMore !== undefined) {
        this.modelMoreOpen = !host.open;
      } else if (host.dataset.wbMsEvidence !== undefined) {
        this.msEvidenceOpen = !host.open;
      } else if (host.dataset.wbMsTest !== undefined) {
        this.msTestOpen = !host.open;
      } else if (host.dataset.wbModelLaneMore !== undefined) {
        this.modelLaneMoreOpen = !host.open;
      } else if (host.dataset.wbAccountFold !== undefined) {
        // 账号卡折叠（维护者 2026-09-29）：四张卡各一个宿主，开合态按服务 id 记。
        // 点击时 `open` 仍是切换前的值，所以目标态是它的反面；这里只记状态，
        // 不重绘（重绘会把浏览器刚切换的开合态折回默认态）。
        const id = Number(host.dataset.wbAccountFold);
        if (host.open) this.accountFoldOpen.delete(id);
        else this.accountFoldOpen.add(id);
      }
    });
    window.setInterval(() => {
      void this.probeTick();
    }, 30000);
    // R9-R38 auto-follow: a dedicated 5s loop re-reads the open detail's
    // tail only while follow is on; paused reading is never pulled down.
    window.setInterval(() => {
      const d = this.logDetail;
      if (this.logDetailOpen && d && d.follow && this.sessionLive()) {
        void this.fetchLogEntries(d, false);
      }
    }, 5000);
    // R9-R25 quick jump: one top-bar input filters lanes, service panels and
    // function keywords; ↑↓ walk the matches, Enter/click jumps, Esc closes.
    // No modal, no extra page — content growth only ever edits JUMP_INDEX.
    const jumpInput =
      this.root.querySelector<HTMLInputElement>(".wb-jump-input");
    const jumpList = this.root.querySelector<HTMLElement>(".wb-jump-list");
    if (jumpInput && jumpList) {
      type JumpEntry = (typeof JUMP_INDEX)[number];
      let jumpHits: JumpEntry[] = [];
      let jumpActive = -1;
      const jumpClose = () => {
        jumpList.hidden = true;
        jumpActive = -1;
      };
      const jumpGo = (entry: JumpEntry) => {
        jumpInput.value = "";
        jumpClose();
        // A jump is a navigation, not a click on the armed button: disarm first.
        this.clearConfirm();
        if (entry.service !== undefined) {
          this.ovService = entry.service;
          this.gwSel = entry.service;
          this.gwTab = "account";
          this.gwNarrowDetail = true;
        } else if (entry.lane === 1) {
          // An account-level entry that names no gateway keeps the gateway already
          // selected; from 全部模型 it opens the main chain's account tab.
          if (typeof this.gwSel !== "number") {
            this.gwSel = MAIN_CHAIN_ID;
            this.ovService = MAIN_CHAIN_ID;
          }
          this.gwTab = "account";
          this.gwNarrowDetail = true;
        }
        if (entry.modelSection) this.modelSection = entry.modelSection;
        this.select(entry.lane);
        if (entry.anchor)
          this.root
            .querySelector<HTMLElement>(entry.anchor)
            ?.scrollIntoView({ block: "start" });
      };
      const jumpRender = () => {
        const q = jumpInput.value.trim().toLowerCase();
        const hits = (
          q
            ? JUMP_INDEX.filter((e) => e.label.toLowerCase().includes(q))
            : JUMP_INDEX.slice(0, names.length)
        ).slice(0, 8);
        jumpHits = hits;
        jumpActive = -1;
        jumpList.innerHTML = hits
          .map(
            (e) =>
              `<button type="button" role="option" data-jump="${JUMP_INDEX.indexOf(e)}">${escapeHtml(e.label)}</button>`
          )
          .join("");
        jumpList.hidden = hits.length === 0;
      };
      jumpInput.addEventListener("input", jumpRender);
      jumpInput.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          jumpInput.value = "";
          jumpRender();
          return;
        }
        if (jumpList.hidden) return;
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          const step = event.key === "ArrowDown" ? 1 : -1;
          const count = jumpHits.length;
          if (count) {
            jumpActive =
              jumpActive < 0
                ? step === 1
                  ? 0
                  : count - 1
                : (jumpActive + step + count) % count;
            jumpList.querySelectorAll("[data-jump]").forEach((el, i) => {
              el.classList.toggle("wb-jump-active", i === jumpActive);
              if (i === jumpActive) el.scrollIntoView({ block: "nearest" });
            });
          }
          event.preventDefault();
          return;
        }
        if (event.key === "Enter") {
          const entry = jumpHits[jumpActive >= 0 ? jumpActive : 0];
          if (entry) jumpGo(entry);
          event.preventDefault();
        }
      });
      jumpList.addEventListener("click", (event) => {
        const option = (event.target as Element).closest<HTMLElement>(
          "[data-jump]"
        );
        if (!option) return;
        const entry = JUMP_INDEX[Number(option.dataset.jump)];
        if (entry) jumpGo(entry);
      });
      // The delay lets a click on a list option land before the list hides.
      jumpInput.addEventListener("blur", () => {
        setTimeout(jumpClose, 150);
      });
      jumpInput.addEventListener("focus", jumpRender);
    }
  }
  private text(propKey: string) {
    const v = this.props[propKey]?.value;
    return typeof v === "string" ? v.trim().slice(0, 240) : "";
  }
  private taskId(i: number) {
    return `${i}:${this.text(`task${i + 1}`)}`;
  }
  private apply(props: ConsoleProperties) {
    Object.assign(this.props, props);
    this.visibility = applyVisibilityProperties(this.visibility, props);
    if (props.desktopmode)
      this.setEnabled(props.desktopmode.value === "workbench");
    this.renderNav();
    this.renderPanel();
  }
  setEnabled(value: boolean) {
    this.enabled = value;
    this.stage.dataset.workbench = String(value);
    this.onMode();
    this.syncVisibility();
    this.syncElements();
    if (value) {
      void this.probeTick();
      // A12-3: entering the page performs ONE bounded read-only query of the
      // usage endpoints (the lane panels then read what landed); the 30s poll
      // does not repeat it, only the panels' 刷新 button does.
      this.ensureUsageReads();
      // F5: the international account card reads its quota once on entry too
      // (bounded, read-only); repaints and the 30s poll never re-issue it.
      this.ensureQoderGlobalQuota();
    }
  }
  syncVisibility() {
    // The shell owns the default surface: it stays hidden during boot, the
    // entry phase (no mode attribute yet) and while a detail file is open,
    // and reappears automatically when the mode falls back to archive.
    // A missing mode attribute means the page has not entered boot yet.
    const mode = this.stage.dataset.mode;
    const hidden =
      !this.enabled || mode === "boot" || mode === "detail" || !mode;
    const entering = this.root.hidden && !hidden;
    const reduced = this.stage.classList.contains("reduce-motion");
    this.root.inert = hidden;
    this.root.setAttribute("aria-hidden", String(hidden));
    // A36-1 block ②: the star-map layer is the console face's background —
    // the dimmed 3D array hides while the workbench shell is the active
    // surface (boot ceremony / detail / archive browsing keep the scene as
    // their own primary surface); data-wb-face drives the CSS swap. Honest
    // states only: the array returns as content in those faces, never as
    // a fabricated background.
    this.stage.dataset.wbFace = hidden ? "off" : "on";
    if (hidden && !this.root.hidden && !reduced) {
      if (!this.exitAnimation) {
        const animation = this.root.animate(
          [{ opacity: getComputedStyle(this.root).opacity }, { opacity: 0 }],
          { duration: 220, fill: "forwards", easing: "ease-out" }
        );
        this.exitAnimation = animation;
        animation.onfinish = () => {
          this.root.hidden = true;
          animation.cancel();
          this.exitAnimation = undefined;
        };
      }
      return;
    }
    const interrupted = Boolean(this.exitAnimation);
    const opacity = getComputedStyle(this.root).opacity;
    this.exitAnimation?.cancel();
    this.exitAnimation = undefined;
    this.root.hidden = hidden;
    if (interrupted && !hidden && !reduced)
      this.root.animate([{ opacity }, { opacity: 1 }], {
        duration: 220,
        easing: "ease-out",
      });
  }
  select(lane: number) {
    if (lane < 0 || lane >= names.length) return;
    if (lane === 5) {
      // Rev17: the former 模型 lane is the workspace's pinned 全部模型 view.
      this.gwSel = "models";
      this.gwNarrowDetail = true;
      lane = 1;
    }
    if (lane === 6) {
      // R9-R46: share lane entry refreshes both reads — share data used to be
      // fetched once per page session and never again afterwards.
      this.shareLaneVisited = true;
      this.refreshShareData();
    }
    // A12-3: the lanes that show the usage panels read them once on first
    // entry (bounded, read-only); a lane that was already read keeps its rows
    // until the panel's 刷新 button asks for a re-read. F5: the account lane
    // also reads the international chain's quota once on entry — the guard
    // keeps a repaint from re-issuing it. Lane 3 is included because the fee
    // lane's first-screen Token reading carries its own 时间区间/环比, and the
    // 14-day complete-token read is the only real source of that window (the
    // same bounded read-only GET the other lanes issue — no operation row).
    if (lane === 0 || lane === 1 || lane === 3 || lane === 4)
      this.ensureUsageReads();
    if (lane === 1) {
      this.ensureQoderGlobalQuota();
      this.syncGatewayConnection();
    }
    const entering = lane !== this.lane;
    // A log detail opened from another page returns there only until the user
    // navigates elsewhere themselves.
    if (lane !== 4) this.logReturnLane = null;
    // A share confirm armed on the share page does not survive leaving it.
    if (entering) this.disarmShare();
    // The chosen tab and disclosure states survive the switch; a pending
    // inline-action focus does not.
    if (entering) this.shareKeyFocusPending = undefined;
    // Rev19 A19-2: the plaintext call key lives only on the gateway page —
    // leaving lane 1 drops the installed read result and cancels the in-flight
    // read through the binding's own clear() (a late response cannot refill it).
    if (this.lane === 1 && lane !== 1) this.clearGatewayConnection();
    this.lane = lane;
    this.renderNav();
    this.renderPanel();
    if (entering) this.playLaneEnter();
  }
  /** Rev16 A16-4: a short fade-in when the user changes page, never on the poll's
   *  repaints (those call renderPanel directly). A quick second switch cancels the
   *  running fade, so only the last page animates; reduced motion switches
   *  instantly. Opacity only: a transform would re-anchor the fixed-position
   *  layers inside the content for the duration. */
  private playLaneEnter() {
    this.laneEnterAnimation?.cancel();
    this.laneEnterAnimation = undefined;
    if (this.stage.classList.contains("reduce-motion")) return;
    const content = this.root.querySelector<HTMLElement>(".wb-content");
    if (!content || typeof content.animate !== "function") return;
    const animation = content.animate([{ opacity: 0.25 }, { opacity: 1 }], {
      duration: 200,
      easing: "cubic-bezier(0.2, 0, 0, 1)",
    });
    animation.onfinish = () => {
      if (this.laneEnterAnimation === animation)
        this.laneEnterAnimation = undefined;
    };
    this.laneEnterAnimation = animation;
  }
  /** One poll cycle: liveness probes + management-session data attempt, then
   *  in-place dot/quote updates (no full re-render, so the rolling panel is
   *  untouched while the user watches). */
  private async probeTick() {
    if (!this.enabled || this.probing) return;
    this.probing = true;
    try {
      // R11-6/B2 (2026-09-23): liveness evidence comes from the 8001
      // backend's server-side loopback probes (/api/console/health —
      // native on production, the same path via the /gw/usagesrv prefix in
      // dev). The browser no longer probes dev-only /gw/* paths that the
      // static origin answers with 404, so a Running process can never be
      // displayed as "探针 HTTP 404 · 未命中" again. The snapshot is a
      // bounded read with a short TTL on the backend. A failed snapshot
      // (null) leaves the previous evidence untouched — it must never be
      // rewritten as "every service down" (P1, 2026-09-24). A service
      // missing from a GOOD snapshot has no fresh evidence either: it lands
      // in the honest unknown state (grey dot), not in "down".
      const health = await this.fetchHealth();
      if (health) this.applyHealthSnapshot(health);
      // Dots land first (fast probes); each fast session read repaints the
      // moment IT lands — the production console reads can take seconds
      // themselves, and a lane switch in between would otherwise show a
      // gated panel until the next 30s poll. /api/summary can take tens of
      // seconds on the production ledger (full-ledger aggregation), so it
      // runs fire-and-forget: its honest "reading…" line reports progress
      // and applySummary patches the KPIs in place when it lands. The
      // in-flight guard keeps the 30s poll from stacking requests on top of
      // a slow summary.
      this.applyLive();
      // Unconditional: every lane's panel reads live state (lane 3's fee
      // tables and lane 2's ledger view must repaint when the slow summary
      // lands too, not just the overview/services lanes).
      const repaint = () => this.renderPanel();
      // R9-R46: share status rides the poll once the user has entered the
      // share lane; lanes never visited fire no share requests. Keys are NOT
      // polled — they only change through this console's own operations,
      // which refresh both reads on their terminal states.
      const shareRead = this.shareLaneVisited
        ? this.fetchShareStatus().then(repaint)
        : Promise.resolve();
      // 公网巡检读面随同一轮询刷新（不新增定时器）；它只读快照，不触发任何检查或恢复。
      const publicShareRead = this.shareLaneVisited
        ? this.fetchPublicShare().then(repaint)
        : Promise.resolve();
      await Promise.all([
        this.fetchRuntime().then(repaint),
        this.fetchActions().then(repaint),
        this.fetchRecentOps().then(repaint),
        this.fetchPrices().then(repaint),
        this.fetchLogs().then(repaint),
        this.fetchStats().then(repaint),
        this.fetchQuotaSnapshot().then(repaint),
        shareRead,
        publicShareRead,
      ]);
      this.applyLive();
      this.applySummary();
      repaint();
      void this.fetchSummary().then(() => {
        this.applySummary();
        repaint();
      });
    } finally {
      this.probing = false;
    }
  }
  /** R11-6/B2: the backend's server-side health snapshot (bounded read,
   *  short TTL). Same path in dev (/gw/usagesrv prefix) and on the static
   *  production origin (native /api/console route). A failed fetch answers
   *  null — callers keep the previous evidence instead of inventing a
   *  dead verdict (stale ≠ dead). */
  private async fetchHealth(
    force = false
  ): Promise<{ checked_at: number; services: HealthServiceEntry[] } | null> {
    try {
      // `refresh=1` is the backend's OWN manual re-probe path (usagesrv.py
      // `_console_health_payload(force)`: re-probes instead of serving the
      // cached snapshot, `cached` is then False). It is still one bounded,
      // read-only GET — no operation-ledger row, no write.
      const response = await fetch(
        `/gw/usagesrv/api/console/health${force ? "?refresh=1" : ""}`,
        { credentials: "include" }
      );
      if (!response.ok) {
        // 实测 HTTP 状态：404 等 = 当前服务端版本未载入该参数；它只对
        // 手动重探有意义，30s 轮询走非 force 路径不受影响。
        if (force)
          this.healthRefresh =
            response.status === 404
              ? { state: "unavailable", status: response.status }
              : {
                  state: "failed",
                  status: response.status,
                  reason: `HTTP ${response.status}`,
                };
        return null;
      }
      const data = (await response.json()) as {
        checked_at?: number;
        cached?: boolean;
        services?: HealthServiceEntry[];
      };
      if (!Array.isArray(data.services)) {
        if (force)
          this.healthRefresh = {
            state: "failed",
            status: response.status,
            reason: "响应缺少 services 数组",
          };
        return null;
      }
      if (force) {
        this.healthRefresh = {
          state: "ok",
          at: data.checked_at ?? 0,
          cached: data.cached === true,
        };
      }
      return { checked_at: data.checked_at ?? 0, services: data.services };
    } catch {
      if (force)
        this.healthRefresh = {
          state: "failed",
          reason: "网络失败或服务未运行",
        };
      return null;
    }
  }
  /** One health snapshot → the probe evidence every card renders. Extracted so
   *  the 30s poll and the manual re-probe write the SAME evidence shape from
   *  the SAME mapping — a second copy of this loop would let the two
   *  disagree about what a service's dot means. Evidence age comes from the
   *  snapshot's own checked_at (backend epoch seconds), so the stamp is when
   *  the SERVER probed, not when this page happened to read it; a snapshot
   *  without a usable stamp falls back to the local clock (still this read's
   *  reading). A service missing from a GOOD snapshot has no fresh evidence:
   *  it lands in the honest unknown state (grey dot), not in "down". */
  private applyHealthSnapshot(health: {
    checked_at: number;
    services: HealthServiceEntry[];
  }) {
    const observedAt =
      typeof health.checked_at === "number" && health.checked_at > 0
        ? health.checked_at * 1000
        : Date.now();
    for (const s of SERVICES) {
      const healthId = HEALTH_ID_BY_SERVICE[s.id];
      const entry = health.services.find((x) => x.id === healthId);
      if (entry) {
        this.live.set(s.id, { ...this.healthToProbe(entry), observedAt });
      } else {
        this.live.set(s.id, {
          alive: false,
          state: "unknown",
          ms: 0,
          observedAt,
          note: "健康快照未含本源（后端端点未就绪或未含该服务）",
        });
      }
    }
  }
  /** 手动重探（`GET /api/console/health?refresh=1`）：一次有界只读 GET，与
   *  30s 轮询共用同一份证据写入路径（applyHealthSnapshot）。服务端版本未载入
   *  该参数时按实测 HTTP 状态显示「当前服务端版本未载入」，失败时保留上一次
   *  真实证据，都不判宕机、不折成空表。 */
  private async manualReprobe() {
    if (this.healthRefreshInFlight) return;
    this.healthRefreshInFlight = true;
    this.healthRefresh = { state: "busy" };
    this.renderPanel();
    const health = await this.fetchHealth(true);
    this.healthRefreshInFlight = false;
    if (health) this.applyHealthSnapshot(health);
    // 证据先落定再重绘：一次点击只发一次请求，重复点击被在途守卫吸收。
    this.applyLive();
    this.renderPanel();
  }
  /** Map one backend health entry onto the probe-evidence shape the cards
   *  already render; the note says WHERE the evidence came from so a
   *  server-side verdict is never mistaken for a browser-side probe. */
  private healthToProbe(entry: HealthServiceEntry): ProbeResult {
    const ms = typeof entry.ms === "number" ? entry.ms : 0;
    if (entry.state === "not_listening") {
      return {
        alive: false,
        note: `端口 ${entry.port} 未监听（服务端探测）`,
        ms,
        status: undefined,
      };
    }
    if (entry.state === "unknown") {
      // Honest middle state (connect timeout ≠ refused): the card shows the
      // probe's own note, and the dot stays grey — never a fabricated dead
      // verdict (P1, 2026-09-24).
      return {
        alive: false,
        state: "unknown",
        note: entry.note ?? `端口 ${entry.port} 探测未定（服务端探测）`,
        ms,
        status: undefined,
      };
    }
    if (typeof entry.http_status === "number") {
      return {
        alive: true,
        note: `HTTP ${entry.http_status} · 服务端探测命中`,
        ms,
        status: entry.http_status,
      };
    }
    return {
      alive: true,
      note: entry.note ?? `TCP 可达（服务端探测）`,
      ms,
      status: undefined,
    };
  }
  /** R11-5/B2: bounded read-only quota snapshot (short TTL server-side;
   *  force = the manual refresh path). A failed fetch keeps the previous
   *  snapshot — never refolds into a fabricated 0.
   *
   *  P1/R1（2026-10-10）：这次读取同样先过运行/停用门（目标 Qoder 国内链），
   *  与后端 `usagesrv._quota_snapshot_payload` 同口径——人工停用 / 未运行 /
   *  未知都不发这次注定被拒的请求，手动刷新也不例外；门只读，不发探测、
   *  不写标记、不启动/停止进程。 */
  private async fetchQuotaSnapshot(force = false): Promise<void> {
    const gate = this.upstreamReadGate("Qoder");
    if (gate.verdict !== "running") {
      this.quotaSnapshotGate = gate;
      this.quotaSnapshotRead = { state: "gated", reason: gate.short };
      return;
    }
    this.quotaSnapshotGate = undefined;
    // 读取中只在「还没有可显示的快照」时写出：有上次真实值时继续显示它
    // （不折 0、不清空），与 qoderglobal.quota 的 reading 口径一致。
    if (!this.quotaSnapshot) this.quotaSnapshotRead = { state: "loading" };
    try {
      const response = await fetch(
        `/gw/usagesrv/api/console/quota-snapshot${force ? "?refresh=1" : ""}`,
        { credentials: "include" }
      );
      if (!response.ok) {
        // 实测 HTTP 状态照原样写出：失败不得折成「尚未读取」，也不冒充空快照。
        this.quotaSnapshotRead = {
          state: "error",
          reason: `HTTP ${response.status}`,
        };
        return;
      }
      const data = (await response.json()) as {
        state?: string;
        remaining?: number | null;
        used?: number | null;
        total?: number | null;
        checked_at?: number;
        error?: string;
        expires_at?: string | null;
      };
      this.quotaSnapshot = {
        state: String(data.state ?? "unknown"),
        remaining: typeof data.remaining === "number" ? data.remaining : null,
        used: typeof data.used === "number" ? data.used : null,
        total: typeof data.total === "number" ? data.total : null,
        checked_at:
          typeof data.checked_at === "number"
            ? data.checked_at
            : Math.floor(Date.now() / 1000),
        error: typeof data.error === "string" ? data.error : undefined,
        // 到期时点原样透出（后端从上游 /quota 载荷透出）：字符串才收，
        // 其余（含缺席）写 null = 未知，绝不猜一个时点。
        expires_at:
          typeof data.expires_at === "string" && data.expires_at.trim()
            ? data.expires_at
            : null,
      };
      this.quotaSnapshotRead = undefined;
    } catch {
      // 传输失败：保留上一次真实快照，并把失败原因写在自己的读取状态上。
      this.quotaSnapshotRead = {
        state: "error",
        reason: "网络失败或服务未运行",
      };
    }
  }
  /** P1/R1（2026-10-10）自动上游读取的**运行/停用门**（前端侧）。
   *
   *  后端 `usagesrv._upstream_read_gate` 用「人工停用意图（desired-state 标记）+
   *  有界 TCP 进程事实 + 被动健康读取」裁决；前端只有运行快照（/api/console/services
   *  的 state，后端 Get-GatewayServiceInspection 的 Running/Degraded/Conflict/
   *  Residual/Stopped）与本页自己的真实操作记录，所以门用同一批只读事实拼出同口径
   *  的裁决，绝不发探测、不写标记、不启动/停止进程：
   *   · 人工停用意图：本页会话里最近一次 service.start / service.stop 的终态，
   *     以及 8001 操作账本里该目标最近一次终态启停记录；
   *   · 进程事实：Running / Degraded = 在运行；Stopped = 未运行；Residual = 受管
   *     残留进程仍在监听（按运行放行）；Conflict = 端口被未知程序占用。
   *  运行状态**尚未读取**（快照未返回或 state 不在已知集合）判 unknown：未知既不
   *  伪装成运行中，也不伪装成已停止——调用方据此不发这次注定被拒的请求。
   *
   *  `short` 是首层读数格用的短句（完整裁决句进 `detail`，由 `title` 承载：
   *  长句会把 KPI 格撑高、把「服务明细」挤出首屏）。 */
  private upstreamReadGate(target: string): {
    verdict: "running" | "stopped" | "not_running" | "unknown";
    short: string;
    detail: string;
  } {
    if (!target) {
      return {
        verdict: "unknown",
        short: "运行状态未确认",
        detail: "该链没有运行目标：本次不向上游查询（未知 ≠ 已停止）",
      };
    }
    if (this.manualStopIntent(target) === "stopped") {
      return {
        verdict: "stopped",
        short: "人工停用意图生效",
        detail:
          "人工停用意图生效（本页最近一次停止动作成功）：本次不向上游查询；后台读取不写停用标记、不启动进程，停用意图不被撤销",
      };
    }
    const runtime = this.runtime?.[target];
    const state = runtime?.state;
    if (!state) {
      return {
        verdict: "unknown",
        short: "运行状态未确认",
        detail:
          "运行状态尚未读取（运行快照未返回）：本次不向上游查询，也不判已停止或运行中（未知 ≠ 已停止）",
      };
    }
    if (state === "Running" || state === "Degraded") {
      return {
        verdict: "running",
        short: "运行中",
        detail: `运行中（运行快照：${runtime.detail || state}）`,
      };
    }
    if (state === "Residual") {
      return {
        verdict: "running",
        short: "运行中",
        detail:
          "受管残留进程（运行快照：发现受管残留进程；端口由受管进程占用）：读取按运行放行",
      };
    }
    if (state === "Stopped") {
      return {
        verdict: "not_running",
        short: "进程未运行",
        detail:
          "进程未运行（运行快照：已停止；人工停用与异常退出在此读面不可区分）：本次不向上游查询，也不改动守护契约（不写停用意图、不启动进程）",
      };
    }
    if (state === "Conflict") {
      return {
        verdict: "unknown",
        short: "运行状态未确认",
        detail:
          "端口被未知程序占用（运行快照：端口被未知程序占用）：不确认本链在运行，本次不向上游查询",
      };
    }
    return {
      verdict: "unknown",
      short: "运行状态未确认",
      detail: `运行状态未知（运行快照：${state}）：本次不向上游查询（未知 ≠ 已停止）`,
    };
  }
  /** P1/R1：人工停用意图。先看本页会话里 service.start / service.stop 的终态
   *  （真实动作自己带回的 status），再看 8001 操作账本里该目标最近一次终态启停
   *  记录；两者都没有是 undefined（未知），绝不把「没有记录」当成停用，也绝不把
   *  失败的停用当成「已经停了」。 */
  private manualStopIntent(target: string): "running" | "stopped" | undefined {
    const local = this.svcStopIntent.get(target);
    if (local) return local;
    for (const op of this.opsRecent?.rows ?? []) {
      if (op.target !== target || op.state !== "succeeded") continue;
      const status =
        typeof op.result?.status === "string" ? op.result.status : "";
      if (
        op.action === "service.stop" &&
        (status === "Stopped" || status === "AlreadyStopped")
      )
        return "stopped";
      if (
        op.action === "service.start" &&
        (status === "Started" ||
          status === "AlreadyRunning" ||
          status === "Restarted" ||
          status === "StartPending")
      )
        return "running";
      if (op.action === "service.restart" && status === "Restarted")
        return "running";
    }
    return undefined;
  }
  /** P1/R1：把一次真实启停动作的终态记成人工意图。只认真正做成的动作：
   *  Stopped / AlreadyStopped = 停用；Started / AlreadyRunning / Restarted /
   *  StartPending = 运行；被拒绝 / 端口冲突 / 归档 / 失败都不动意图——失败的停用
   *  不能当成「已经停了」。 */
  private rememberServiceIntent(
    actionName: string,
    target: string,
    result: Record<string, unknown>
  ): void {
    if (!target) return;
    const status = typeof result.status === "string" ? result.status : "";
    if (
      actionName === "service.stop" &&
      (status === "Stopped" || status === "AlreadyStopped")
    ) {
      this.svcStopIntent.set(target, "stopped");
    } else if (
      actionName === "service.start" &&
      (status === "Started" ||
        status === "AlreadyRunning" ||
        status === "Restarted" ||
        status === "StartPending")
    ) {
      this.svcStopIntent.set(target, "running");
    } else if (actionName === "service.restart" && status === "Restarted") {
      this.svcStopIntent.set(target, "running");
    }
  }
  /** F5: 国际链额度的入页读取（有界、只读、每次入页一次）。
   *
   *  读取复用既有的只读适配动作 qoderglobal.quota（其自身说明即「只读」，
   *  纯 GET /quota 透传、bump_generation_on_success=False）。守卫的四层：
   *  · 运行/停用门（P1/R1）：人工停用、进程未运行或运行状态未知都不自动提交
   *    （与后端 _upstream_read_gate 同口径：未知 ≠ 已停止，也不伪装成运行中）；
   *  · 无管理会话：连读取都发不出去，卡片如实显示未读取；
   *  · 已在途：不重复发（手动刷新同样不并发）；
   *  · 本次入页已发过且非强制：不重发——30s 轮询与每次重绘都不再发，
   *    所以「入页读取一次」不会变成反复的管理操作。
   *  手动「刷新额度」是明确的用户意图，仍要先过运行/停用门（停用来源不因一次
   *  点击就向上游查询，与后端 _quota_snapshot_payload 的手动刷新同口径）。 */
  private ensureQoderGlobalQuota(force = false) {
    const gate = this.upstreamReadGate("QoderGlobal");
    if (gate.verdict !== "running") {
      // 运行状态尚未读取：记一次待补读（运行快照落地后由 probeTick 补一次）；
      // 停用 / 未运行不记——离开重进或轮询都不会因一次失败自动重发。
      this.qoderGlobalQuotaDeferred = gate.verdict === "unknown" && !force;
      this.qoderGlobalQuotaGate = gate;
      const previous = this.qoderGlobalQuota;
      // 门没放行：没有「读取中」（本次根本没发请求），如实写出短句原因；完整裁决句
      // 在 qoderGlobalQuotaGate（卡片状态行放进 title）；有上次真实值就保留它并说明
      // 本次未读取，没有就写未知——未知 ≠ 0，也不伪装成已停止。
      this.qoderGlobalQuota = previous?.quota
        ? {
            state: "gated",
            quota: previous.quota,
            checkedAt: previous.checkedAt,
            error: gate.short,
          }
        : { state: "gated", error: gate.short };
      return;
    }
    this.qoderGlobalQuotaGate = undefined;
    this.qoderGlobalQuotaDeferred = false;
    if (!this.sessionLive()) return;
    if (this.qoderGlobalQuotaInFlight) return;
    if (this.qoderGlobalQuotaRequested && !force) return;
    this.qoderGlobalQuotaRequested = true;
    this.qoderGlobalQuotaInFlight = true;
    const previous = this.qoderGlobalQuota;
    // 读取中：有上次真实值时继续显示它（不折 0、不清空），没有时如实显示读取中。
    this.qoderGlobalQuota = previous?.quota
      ? { ...previous, state: "reading", error: undefined }
      : { state: "reading" };
    void this.submitAction("qoderglobal.quota", "QoderGlobal");
  }
  /** F5: 读取终态落定（成功 / 失败 / 轮询超时 / 被拒绝 / 连接失败都走这里）。
   *  成功：记录额度桶、来源（适配动作名）与检查时间。失败：保留上次真实值并
   *  标过期；没有上次值时如实显示「未知」，绝不折成 0。失败后解除「已请求」
   *  守卫，使下一次入页可以重试一次（手动刷新始终可以重试）。 */
  private settleQoderGlobalQuota(
    actionName: string,
    result?: Record<string, unknown>,
    error?: string
  ): boolean {
    if (actionName !== "qoderglobal.quota") return false;
    this.qoderGlobalQuotaInFlight = false;
    // 真实读取落地后运行/停用门不再解释当前状态（它的短句已进 error / 完整句已进
    // qoderGlobalQuotaGate 的上一次写入，这里清掉避免旧裁决冒充本次结果）。
    this.qoderGlobalQuotaGate = undefined;
    const quota = result?.quota;
    const checkedAt =
      typeof result?.checked_epoch === "number" && result.checked_epoch > 0
        ? result.checked_epoch
        : undefined;
    if (!error && quota && typeof quota === "object" && !Array.isArray(quota)) {
      this.qoderGlobalQuota = {
        state: "live",
        quota: quota as Record<string, unknown>,
        checkedAt,
      };
      return true;
    }
    const previous = this.qoderGlobalQuota;
    this.qoderGlobalQuotaRequested = false;
    const reason = error ?? "响应未含额度桶（未采集）";
    this.qoderGlobalQuota = previous?.quota
      ? {
          state: "stale",
          quota: previous.quota,
          checkedAt: previous.checkedAt,
          error: reason,
        }
      : { state: "stale", error: reason };
    return true;
  }
  private quotaValueText(): string {
    const snap = this.quotaSnapshot;
    if (!snap || snap.remaining === null || snap.remaining === undefined)
      return "—";
    // Same 积分 format as every other credit figure on the page (contract 3.8).
    return fmtCredits(snap.remaining);
  }
  /** Honest provenance line: WHEN it was checked and whether it is the live
   *  read, the TTL cache, or a stale value kept after a failed refresh. A
   *  check from an earlier day shows its date too, so a kept value can never
   *  pass itself off as today's reading (A12-3 ①). */
  private quotaNoteHtml(): string {
    // 句子的**唯一来源**：总览 KPI 格把它包成裸 <small>（`.wb-kpis small` 已给
    // 12.5px 与弱化色，且不带 `.wb-muted` 的 14px 上边距），账号卡包成
    // `<small class="wb-muted">`。同一事实因此只有一个句子，两种承载各按自己的
    // 形制排版，而不会出现两套措辞。
    return `<small class="wb-muted">${this.quotaStateText()}</small>`;
  }
  /** 实时额度读口的真实状态句（不带包装）：读取中 / 读取失败（带实测原因，给
   *  重试路径）/ 尚未读取 / 检查于 <时点>（缓存 / 陈旧 / 本次刷新失败）。
   *  首层只留短句（Plan-Revision 9 ③：读数格最多两行状态）：原因与快照自己的
   *  error 进 `quotaStateDetail()`，由总览格的状态行放进 `title`。 */
  private quotaStateText(): string {
    return escapeHtml(this.quotaStatePlain());
  }
  /** `quotaStateText()` as plain text (the account card escapes it at its own
   *  point of use, the KPI cell through the wrapper above). */
  private quotaStatePlain(): string {
    const snap = this.quotaSnapshot;
    if (!snap) {
      // 四态分开（与 usageReadNote 同口径）：读取中 / 读取失败（带原因，给
      // 重试路径）/ 门未放行（P1/R1：运行/停用门拒绝了这次自动读取，如实写
      // 原因而不是折成「尚未读取」）/ 尚未读取。读取失败曾被折成「尚未读取」，
      // 于是「刷新额度」点了文案不变——失败不是「还没问过」。
      const read = this.quotaSnapshotRead;
      if (read?.state === "loading") return "实时额度读取中…";
      if (read?.state === "gated") {
        return `实时额度本次未读取（${read.reason ?? "运行/停用门未放行"}）`;
      }
      if (read?.state === "error") {
        return `实时额度读取失败（${read.reason ?? "原因未知"}）`;
      }
      return "尚未读取（打开总览时自动读取）";
    }
    const t = new Date(snap.checked_at * 1000);
    const hh = String(t.getHours()).padStart(2, "0");
    const mm = String(t.getMinutes()).padStart(2, "0");
    const now = new Date();
    const sameDay =
      t.getFullYear() === now.getFullYear() &&
      t.getMonth() === now.getMonth() &&
      t.getDate() === now.getDate();
    const stamp = sameDay
      ? `${hh}:${mm}`
      : `${t.getMonth() + 1}/${t.getDate()} ${hh}:${mm}`;
    // 后端被门拒绝时回的 `state` 是门的裁决词（stopped / not_running /
    // process_unknown，见 usagesrv._gated_quota_snapshot_payload）：这些不是
    // 读数状态，如实写成「本次未向上游读取」并带它自己的 detail，不折成「未知」。
    const suffix =
      snap.state === "live"
        ? ""
        : snap.state === "cached"
          ? "（缓存）"
          : snap.state === "stale"
            ? "（刷新失败·保留上次真实值）"
            : snap.state === "stopped" ||
                snap.state === "not_running" ||
                snap.state === "process_unknown"
              ? `（本次未向上游读取：${snap.error ?? snap.state}）`
              : "（未知）";
    return `检查于 ${stamp}${suffix}`;
  }
  /** 状态句的完整口径（本次刷新失败的原因 + 快照自己的 error）：只进总览格状态行的
   *  `title`——「本次刷新失败」与「未知 ≠ 0」仍可读到，只是不占首层行。 */
  private quotaStateDetail(): string {
    const snap = this.quotaSnapshot;
    const read = this.quotaSnapshotRead;
    const failed =
      read?.state === "error"
        ? `本次刷新失败：${read.reason ?? "原因未知"}；`
        : "";
    const gated =
      read?.state === "gated"
        ? `本次未向上游读取：${this.quotaSnapshotGate?.detail ?? read.reason ?? "运行/停用门未放行"}；`
        : "";
    const error = snap?.error ? `${snap.error}；` : "";
    return `${failed}${gated}${error}读取失败时不以 0 或空快照冒充，也不沿用上一次读数当本次结果。`;
  }
  /** Runtime service snapshot; only exists inside a management session. */
  private async fetchRuntime() {
    try {
      const response = await fetch("/gw/usagesrv/api/console/services", {
        credentials: "include",
      });
      if (!response.ok) {
        this.runtime = undefined;
        return;
      }
      const data = await response.json();
      const services = data?.services;
      if (!Array.isArray(services)) {
        this.runtime = undefined;
        return;
      }
      const map: Record<string, RuntimeEntry> = {};
      for (const entry of services) {
        if (
          entry &&
          typeof entry === "object" &&
          typeof (entry as RuntimeEntry).key === "string"
        ) {
          map[(entry as RuntimeEntry).key] = entry as RuntimeEntry;
        }
      }
      this.runtime = map;
      // P1/R1（2026-10-10）：运行快照落地后补一次被门推迟的自动读取。入页时快照
      // 还没返回，门按「未知」拒了那次提交（见 ensureQoderGlobalQuota）；这里在
      // 一次状态跃迁上补一次——不是定时重试循环，读取真失败不记待补读，所以失败
      // 不会自动循环重发，只有下一次入页或手动刷新才重试。
      if (this.qoderGlobalQuotaDeferred) {
        this.qoderGlobalQuotaDeferred = false;
        this.ensureQoderGlobalQuota();
      }
    } catch {
      this.runtime = undefined;
    }
  }
  /** Price table (R9-R29): the meter's own prices.json via the console API —
   *  the fee page shows exactly what the billing engine charges, not a copy. */
  private async fetchPrices() {
    try {
      const response = await fetch("/gw/usagesrv/api/console/prices", {
        credentials: "include",
      });
      if (!response.ok) {
        this.priceTable = undefined;
        return;
      }
      const data = await response.json();
      if (!data || typeof data !== "object" || data.ok !== true) {
        this.priceTable = undefined;
        return;
      }
      this.priceTable = data as PriceTable;
    } catch {
      this.priceTable = undefined;
    }
  }
  /** Gateway call logs (R9-R30): tail of each chain's own usage ledger.
   *  Absent logs = the chain has no ledger (honest coverage in the view).
   *  F4b: the read asks for every source the alias table can name
   *  (LOG_FEED_SOURCES); the server owns the source→path table, answers each
   *  covered id with its own rows and names every id it does NOT read in
   *  `sources_unavailable` (usagesrv.py _console_log_readers). The 逐次 drill
   *  of an international bucket can therefore look for its rows AND tell 「不在
   *  账本覆盖内」 from 「尾部没有记录」 without the client guessing a path. */
  private async fetchLogs(force = false) {
    // `force` = the lane-entry read. The lane must ASK before it can state
    // anything about the answer, so the entry read is not gated on
    // `sessionLive()` (which only becomes true after the 30s poll's first
    // tick); a non-ok answer is then written as the real failure it is.
    // The 30s poll's non-force call, when it does NOT ask, must not answer
    // either: it used to write `{state:"gated"}` here, so the lane showed
    // 「调用日志读取失败」 before any request existed — 2 of 4 runs printed that
    // sentence ahead of the lane-entry read's successful answer. A read that was
    // never issued leaves the previous answer (or 读取中…) standing; the lane
    // entry and the 刷新 button are the reads that answer for it.
    if (!force && !this.sessionLive()) return;
    try {
      const response = await fetch(
        `/gw/usagesrv/api/console/logs?limit=${LOG_FEED_LIMIT}&sources=${LOG_FEED_SOURCES.join(",")}`,
        { credentials: "include" }
      );
      if (!response.ok) {
        // 实测 HTTP 状态照原样带进 `logsFeed`：首层只写这一条实测原因，
        // 不把「被拒绝」推断成「无凭据会话或 8001 不可达」。
        this.logsFeed = {
          state: "gated",
          rows: [],
          reason: `HTTP ${response.status}`,
        };
        return;
      }
      const data = await response.json();
      const logs = data?.logs;
      if (!Array.isArray(logs)) {
        this.logsFeed = {
          state: "gated",
          rows: [],
          reason: "响应不含 logs 数组（形状不符）",
        };
        return;
      }
      const unavailable = data?.sources_unavailable;
      this.logsFeed = {
        state: "live",
        rows: logs as LogEntry[],
        // 服务端未声明时不造覆盖结论：缺字段 = 覆盖未知（不是「全部覆盖」）
        unavailable:
          Array.isArray(unavailable) &&
          unavailable.every((s) => typeof s === "string")
            ? (unavailable as string[])
            : undefined,
      };
    } catch {
      // 传输失败同样带实测原因：未知 ≠ 零记录，也不冒充「无凭据会话」。
      this.logsFeed = {
        state: "gated",
        rows: [],
        reason: "网络失败或服务未运行",
      };
    }
  }
  /** Run-log sources registry (R9-R38): fetched once per session; the server
   *  owns the source→path mapping, ids are all the client ever sends.
   *  失败态：`!response.ok` 原先只清空 `logSources`，`logSourcesFailed` 只在
   *  catch 赋值，而重读守卫是 `!logSourcesRequested && !logSourcesFailed`
   *  ——HTTP 失败因此既没有失败句、也不再重试，页面永远停在「读取中…」。
   *  现在两种失败都写失败态与实测状态，并留可点的重试入口。 */
  private async fetchLogSources() {
    try {
      const response = await fetch("/gw/usagesrv/api/console/log-sources", {
        credentials: "include",
      });
      if (!response.ok) {
        this.logSourcesFailed = true;
        this.logSourcesError = `HTTP ${response.status}`;
        this.logSources = undefined;
        return;
      }
      const data = await response.json();
      const sources = data?.sources;
      if (!Array.isArray(sources)) {
        this.logSourcesFailed = true;
        this.logSourcesError = "响应未含来源表（形状不符）";
        this.logSources = undefined;
        return;
      }
      const rows = sources.filter((s: unknown) => {
        const row = s as LogSourceRow;
        return (
          typeof row?.source === "string" && Array.isArray(row?.categories)
        );
      }) as LogSourceRow[];
      // Rev12 F12-6: the log page shows runtime logs only. A source's JSONL call
      // ledger (category "ledger") is usage, read on the fee lane's call detail,
      // so it is not offered as a log category here.
      this.logSources = rows.map((row) => ({
        ...row,
        categories: row.categories.filter((c) => c.category !== "ledger"),
      }));
      this.logSourcesFailed = false;
      this.logSourcesError = "";
    } catch {
      this.logSourcesFailed = true;
      this.logSourcesError = "网络失败或服务未运行";
      this.logSources = undefined;
    } finally {
      this.resumeLogPending();
    }
  }
  /** Open the target asked for while the registry loaded - only when the user is
   *  still on the log page; a settled read never pulls them back to it. */
  private resumeLogPending() {
    const pending = this.logPending;
    this.logPending = null;
    if (!pending) return;
    if (this.lane !== 4) {
      // Left meanwhile: a detail parked in its loading state must not greet the
      // user as 「读取中」 forever when they come back.
      if (this.logDetail?.loading && this.logDetail.source === pending.source)
        this.logDetail = undefined;
      return;
    }
    this.logDetail = undefined;
    this.openLogDetail(pending.source, pending.category);
  }
  /** 来源注册表的显式重试：只读重读一次（不产生操作行），并放开守卫。 */
  private retryLogSources() {
    this.logSourcesRequested = true;
    this.logSourcesFailed = false;
    this.logSourcesError = "";
    void this.fetchLogSources().then(() => this.renderPanel());
  }
  /** Bounded tail/older read of one run log. The seq number drops responses
   *  that belong to an older source flip; filters are server-side literals. */
  private async fetchLogOverview() {
    if (this.logOverviewLoading || !this.logSources) return;
    const seq = ++this.logOverviewSeq;
    this.logOverviewLoading = true;
    this.renderPanel();
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch("/gw/usagesrv/api/console/log-overview", {
        credentials: "include",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      if (
        !Array.isArray(result.rows) ||
        !Array.isArray(result.sources) ||
        !Array.isArray(result.targets) ||
        !result.coverage ||
        result.coverage.full_history !== false ||
        result.rows.some(
          (r: LogEntryRow & { source?: string }) =>
            !r ||
            typeof r.source !== "string" ||
            typeof r.message !== "string" ||
            typeof r.parse_ok !== "boolean"
        )
      )
        throw new Error("总览响应形状不符");
      const next = new Map<
        string,
        {
          status: string;
          rows: LogEntryRow[];
          truncated: boolean;
          targets: number;
          read: number;
        }
      >();
      for (const source of result.sources) {
        if (
          !source ||
          typeof source.source !== "string" ||
          typeof source.status !== "string" ||
          !Number.isSafeInteger(source.targets_total) ||
          !Number.isSafeInteger(source.targets_read) ||
          source.targets_read < 0 ||
          source.targets_read > source.targets_total
        )
          throw new Error("来源覆盖形状不符");
        next.set(source.source, {
          status: source.status,
          rows: result.rows.filter(
            (r: { source: string }) => r.source === source.source
          ),
          truncated: result.targets.some(
            (target: {
              source?: string;
              truncated?: boolean;
              rows_dropped?: number;
            }) =>
              target.source === source.source &&
              (target.truncated || (target.rows_dropped ?? 0) > 0)
          ),
          targets: source.targets_total,
          read: source.targets_read,
        });
      }
      if (seq !== this.logOverviewSeq) return;
      this.logOverview = next;
      this.logOverviewError = "";
      this.logOverviewCheckedAt = new Date().toLocaleTimeString("zh-CN");
    } catch (error) {
      if (seq === this.logOverviewSeq)
        this.logOverviewError =
          error instanceof Error ? error.message : "读取失败";
    } finally {
      window.clearTimeout(timeout);
      if (seq === this.logOverviewSeq) {
        this.logOverviewLoading = false;
        this.renderPanel();
      }
    }
  }
  private async fetchLogEntries(detail: LogDetailState, older: boolean) {
    const seq = ++this.logDetailSeq;
    const params = new URLSearchParams();
    params.set("source", detail.source);
    params.set("category", detail.category);
    if (older && detail.nextCursor) params.set("cursor", detail.nextCursor);
    if (detail.q) params.set("q", detail.q);
    if (detail.since) params.set("since", detail.since);
    if (detail.until) params.set("until", detail.until);
    if (detail.rotation > 0) params.set("rotation", String(detail.rotation));
    if (detail.requestId) params.set("request_id", detail.requestId);
    try {
      const response = await fetch(
        `/gw/usagesrv/api/console/log-entries?${params}`,
        { credentials: "include" }
      );
      if (!response.ok) {
        throw new Error(String(response.status));
      }
      const data = (await response.json()) as LogEntriesResult;
      if (seq !== this.logDetailSeq) return; // superseded by a source flip
      if (!data || typeof data !== "object" || !data.status) {
        throw new Error("shape");
      }
      if (older) {
        // Prepend keeps the reading position; newer tail refreshes replace
        // rows wholesale. A rotation (reset_hint) drops everything.
        detail.rows = [...(data.rows || []), ...detail.rows];
      } else if (detail.follow || !detail.rows.length) {
        detail.rows = data.rows ?? [];
        detail.newCount = 0;
      } else {
        // Paused follow-up read: never overwrite the reading position,
        // estimate arrivals by comparing the newest timestamps.
        const oldTop = detail.rows[0]?.ts ?? "";
        const rows = data.rows ?? [];
        const newTop = rows[0]?.ts ?? "";
        if (newTop && newTop !== oldTop) {
          detail.newCount += rows.filter((r) => r.ts && r.ts > oldTop).length;
        }
      }
      detail.nextCursor = data.next_cursor ?? null;
      detail.requestEcho = detail.requestId
        ? data.request_id === detail.requestId &&
          data.request_scope === "read_window"
          ? "applied"
          : "unconfirmed"
        : "";
      detail.status = data.status;
      detail.errorCode = data.error ?? "";
      detail.scannedBytes = data.scanned_bytes ?? 0;
      detail.truncated = Boolean(data.truncated);
      detail.resetHint = Boolean(data.reset_hint);
      if (data.reset_hint) {
        detail.rows = [];
        detail.nextCursor = null;
      }
      detail.stale = false;
      detail.loading = false;
      detail.fetchedAt = new Date().toLocaleTimeString("zh-CN");
    } catch (exc) {
      if (seq !== this.logDetailSeq) return; // superseded by a source flip
      detail.status = detail.rows.length ? detail.status : "error";
      detail.errorCode = exc instanceof Error ? exc.message : "network";
      // Refresh failure keeps the old rows and flags them as possibly stale.
      detail.stale = detail.rows.length > 0;
    } finally {
      detail.loading = false;
      this.renderPanel();
    }
  }
  /** Seven-day bounded stats (the existing /api/console/stats endpoint,
   *  Asia/Shanghai calendar): totals feed the log-lane summary strip. The
   *  tick path never CLEARS a previous real answer: a read that was not
   *  issued leaves the last answer standing (the same rule fetchLogs follows),
   *  and a failed read records its own reason so the strip can tell 读取失败
   *  from 尚未读取 instead of printing one conflated sentence. */
  private async fetchStats(force = false) {
    // Same lane-entry rule as fetchLogs: ask first, then report the answer.
    if (!force && !this.sessionLive()) return;
    const now = new Date();
    const key = (d: Date) =>
      `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const from = key(
      new Date(now.getFullYear(), now.getMonth(), now.getDate() - 6)
    );
    try {
      const response = await fetch(
        `/gw/usagesrv/api/console/stats?from=${from}&to=${key(now)}`,
        { credentials: "include" }
      );
      if (!response.ok) {
        // 实测 HTTP 状态：404 = 当前服务端版本未载入该端点，其余为读取失败。
        this.statsWeekError =
          response.status === 404
            ? `HTTP 404 · 当前服务端版本未载入 /api/console/stats`
            : `HTTP ${response.status}`;
        return;
      }
      const data = await response.json();
      if (!data || typeof data !== "object" || !data.totals) {
        this.statsWeekError = "响应缺少 totals";
        return;
      }
      this.statsWeek = data as StatsWeek;
      this.statsWeekError = undefined;
    } catch {
      this.statsWeekError = "网络失败或服务未运行";
    }
  }
  /** 任意区间聚合（`GET /api/console/stats?from&to`，只读、有界）。前端在
   *  发请求前用后端同一上限（92 天，console_stats.py:100 `MAX_RANGE_DAYS`）
   *  预检，避免用一次注定 400 的请求去问；后端仍会独立校验，400 携带它自己
   *  受控的 `detail`，如实显示，不折成空表。只有显式提交才发一次请求，
   *  30s 轮询不带 from/to。 */
  private async runStatsRange(fromRaw: string, toRaw: string) {
    const from = fromRaw.trim();
    const to = toRaw.trim();
    const fail = (
      state: "rejected" | "failed",
      status: number,
      reason: string
    ) => {
      this.statsRange =
        state === "rejected"
          ? { state: "rejected", from, to, status, reason }
          : { state: "failed", from, to, status, reason };
      this.renderPanel();
    };
    const day = (s: string) =>
      /^\d{4}-\d{2}-\d{2}$/.test(s) ? Date.parse(`${s}T00:00:00Z`) : NaN;
    if (!from || !to) return fail("failed", 0, "请先选择起始日与结束日");
    const lo = day(from),
      hi = day(to);
    if (!Number.isFinite(lo) || !Number.isFinite(hi))
      return fail("failed", 0, "日期格式需为 YYYY-MM-DD");
    if (lo > hi) return fail("failed", 0, "起始日晚于结束日");
    // 与后端 MAX_RANGE_DAYS 同一口径：超过 92 天由后端返回 400，前端先如实说明。
    const spanDays = Math.round((hi - lo) / 86400000) + 1;
    if (spanDays > STATS_RANGE_MAX_DAYS) {
      return fail(
        "failed",
        0,
        `区间 ${spanDays} 天超过 ${STATS_RANGE_MAX_DAYS} 天上限（后端会以 400 拒绝）`
      );
    }
    if (this.statsRangeInFlight) return;
    this.statsRangeInFlight = true;
    this.statsRange = { state: "loading", from, to };
    this.renderPanel();
    try {
      const response = await fetch(
        `/gw/usagesrv/api/console/stats?from=${from}&to=${to}`,
        { credentials: "include" }
      );
      const body = (await response.json().catch(() => null)) as {
        error?: string;
        detail?: string;
      } | null;
      if (!response.ok) {
        // 400 = 后端自己的区间校验拒绝；404 = 当前服务端版本未载入该端点。
        const reason = body?.detail || body?.error || `HTTP ${response.status}`;
        this.statsRange =
          response.status === 400
            ? { state: "rejected", from, to, status: response.status, reason }
            : { state: "failed", from, to, status: response.status, reason };
        return;
      }
      if (
        !body ||
        typeof body !== "object" ||
        !Array.isArray((body as StatsRangePayload).days)
      ) {
        this.statsRange = {
          state: "failed",
          from,
          to,
          status: response.status,
          reason: "响应缺少 days 数组",
        };
        return;
      }
      this.statsRange = {
        state: "live",
        from,
        to,
        status: response.status,
        payload: body as StatsRangePayload,
      };
    } catch {
      this.statsRange = {
        state: "failed",
        from,
        to,
        reason: "网络失败或服务未运行",
      };
    } finally {
      this.statsRangeInFlight = false;
      this.renderPanel();
    }
  }
  /** A12-3 (Plan-Revision 12): bounded read-only complete-token days
   *  (GET /api/console/usage-tokens?days=14). Read-only GET — no operation
   *  ledger row, no management action. A failed read keeps the previous REAL
   *  rows with their capture time (stale): missing components are never
   *  refolded into 0, and an empty window is never invented. */
  private async fetchUsageTokens(): Promise<void> {
    if (this.usageTokensInFlight) return;
    this.usageTokensInFlight = true;
    try {
      const response = await fetch(
        "/gw/usagesrv/api/console/usage-tokens?days=14",
        { credentials: "include" }
      );
      if (!response.ok) {
        this.usageTokens = this.usageKeep(
          this.usageTokens,
          `HTTP ${response.status}`
        );
        return;
      }
      const data = (await response.json()) as UsageTokensPayload | null;
      if (!data || !Array.isArray(data.days) || !Array.isArray(data.missing)) {
        this.usageTokens = this.usageKeep(
          this.usageTokens,
          "响应形状与预期不符"
        );
        return;
      }
      this.usageTokens = {
        state: "live",
        payload: data,
        checkedAt: this.usageStamp(data.checked_epoch),
      };
    } catch {
      this.usageTokens = this.usageKeep(
        this.usageTokens,
        "网络失败或服务未运行"
      );
    } finally {
      this.usageTokensInFlight = false;
    }
  }
  /** A12-3: bounded read-only whole-request output rate
   *  (GET /api/console/usage-rates?hours=24). Same honest read discipline
   *  as the token read above: a failed re-read keeps the last real buckets. */
  private async fetchUsageRates(): Promise<void> {
    if (this.usageRatesInFlight) return;
    this.usageRatesInFlight = true;
    try {
      const response = await fetch(
        "/gw/usagesrv/api/console/usage-rates?hours=24",
        { credentials: "include" }
      );
      if (!response.ok) {
        this.usageRates = this.usageKeep(
          this.usageRates,
          `HTTP ${response.status}`
        );
        return;
      }
      const data = (await response.json()) as UsageRatesPayload | null;
      if (!data || !Array.isArray(data.hours) || !data.skipped) {
        this.usageRates = this.usageKeep(this.usageRates, "响应形状与预期不符");
        return;
      }
      this.usageRates = {
        state: "live",
        payload: data,
        checkedAt: this.usageStamp(data.checked_epoch),
      };
    } catch {
      this.usageRates = this.usageKeep(this.usageRates, "网络失败或服务未运行");
    } finally {
      this.usageRatesInFlight = false;
    }
  }
  /** A12-3: one bounded usage read per page entry and per first visit of a
   *  lane that shows the data; afterwards only the explicit manual refresh
   *  re-reads. The 30s poll deliberately does NOT re-read them: both
   *  endpoints scan real ledgers server-side, and the page must not turn one
   *  bounded read into a recurring scan. Read-only GETs throughout. */
  private ensureUsageReads(force = false) {
    if (force || !this.usageTokens)
      void this.fetchUsageTokens().then(() => this.renderPanel());
    if (force || !this.usageRates)
      void this.fetchUsageRates().then(() => this.renderPanel());
  }
  /** Capture stamp of one usage payload: the server's own checked_epoch when it
   *  carries a usable one, else this read's clock (never a fabricated value). */
  private usageStamp(epoch?: number): number {
    return typeof epoch === "number" && epoch > 0
      ? epoch
      : Math.floor(Date.now() / 1000);
  }
  /** A failed re-read keeps the last REAL payload; without one there is
   *  nothing honest to show but the failure itself. */
  private usageKeep<T>(
    prev: UsageRead<T> | undefined,
    reason: string
  ): UsageRead<T> {
    return prev?.payload
      ? {
          state: "stale",
          payload: prev.payload,
          checkedAt: prev.checkedAt,
          reason,
        }
      : { state: "error", reason };
  }
  /** Honest read line shared by both usage reads: WHEN it was read, whether it
   *  is a kept stale payload, or why nothing could be read at all. */
  private usageReadNote(
    read: UsageRead<unknown> | undefined,
    what: string
  ): string {
    if (!read) return `${what}尚未读取。`;
    if (read.state === "loading") return `${what}读取中…`;
    if (read.state === "error")
      return `${what}读取失败（${read.reason ?? "原因未知"}）；不以 0 或空表冒充。`;
    const at = read.checkedAt
      ? new Date(read.checkedAt * 1000).toLocaleTimeString("zh-CN", {
          hour12: false,
        })
      : "未知";
    return read.state === "stale"
      ? `${what}本次刷新失败（${read.reason ?? "原因未知"}）；以下为上一次真实读取（读于 ${at}）。`
      : `${what}读于 ${at}（只读查询，不产生操作记录）。`;
  }
  /** A12-3 覆盖说明 (Plan-Revision 12): the server's own coverage block of one
   *  bounded usage read, rendered per source — ledger_present / enabled / note
   *  plus that source's coverage errors and its count of records with an invalid
   *  timestamp (时间无法判定). It is what makes 「采集链未启用 / 账本缺席」(no
   *  readable data at all) distinguishable from 「该窗口确实无用量」(the ledger
   *  WAS read and simply holds no rows for the window). Truncation marks
   *  (truncated / clamped / dropped_rows) are shown when set. An absent coverage
   *  block says 覆盖信息未提供 — a missing field is never filled in by the client. */
  private usageCoverageHtml(
    coverage: UsageCoverage | undefined,
    what: string
  ): string {
    const head = `数据覆盖（${escapeHtml(what)} · 服务端 coverage）`;
    const sources = coverage?.sources;
    if (
      !coverage ||
      !sources ||
      typeof sources !== "object" ||
      Array.isArray(sources) ||
      !Object.keys(sources).length
    ) {
      return `<p class="wb-muted">${head}：覆盖信息未提供（后端未返回 coverage.sources）；账本是否被读取未知，不按零用量推断。</p>`;
    }
    const parts = Object.keys(sources)
      .sort()
      .map((src) => {
        const cov = sources[src] ?? {};
        const present =
          cov.ledger_present === true
            ? "账本已读取"
            : cov.ledger_present === false
              ? "账本缺席"
              : "账本状态未知";
        const enabled =
          cov.enabled === true
            ? "来源已启用"
            : cov.enabled === false
              ? "来源未启用"
              : "启用状态未知";
        const events =
          typeof cov.events === "number"
            ? `事件 ${cov.events} 条`
            : "事件数未知";
        const errs =
          Array.isArray(cov.errors) && cov.errors.length
            ? `，覆盖错误 ${cov.errors
                .map((e) => {
                  const type = typeof e?.type === "string" ? e.type : "unknown";
                  const count = [
                    e?.bad_lines,
                    e?.count,
                    e?.failed_records,
                    e?.failed_rows,
                  ].find((v) => typeof v === "number");
                  return typeof count === "number" ? `${type}(${count})` : type;
                })
                .join("、")}`
            : "";
        const note =
          typeof cov.note === "string" && cov.note.trim()
            ? `：${cov.note.trim()}`
            : "";
        // 时间戳无效的记录（missing[].reason=invalid_time / skipped.invalid_time）在覆盖里
        // 也单列：它们不归任何一天，非零时显示，零不显示。
        const undated =
          typeof cov.undated_records === "number" && cov.undated_records > 0
            ? `，时间无法判定 ${cov.undated_records} 条（时间戳无效，不归任何一天）`
            : "";
        return `${escapeHtml(this.sourceLabel(src))} ${escapeHtml(present)} · ${escapeHtml(enabled)} · ${escapeHtml(events)}${escapeHtml(errs)}${escapeHtml(undated)}${escapeHtml(note)}`;
      });
    const marks: string[] = [];
    if (coverage.truncated === true) marks.push("truncated（结果被截断）");
    if (coverage.clamped === true)
      marks.push("clamped（请求窗口已收口到上限）");
    if (typeof coverage.dropped_rows === "number" && coverage.dropped_rows > 0)
      marks.push(`dropped_rows=${coverage.dropped_rows}`);
    // F3a：days[] 与 missing[] 各自按行数上限截断，两个丢弃数分别披露（各列表
    // 单独计数，缺分量行被丢弃不会伪装成「该窗口没有缺分量」）。
    if (
      typeof coverage.dropped_missing_rows === "number" &&
      coverage.dropped_missing_rows > 0
    ) {
      marks.push(
        `dropped_missing_rows=${coverage.dropped_missing_rows}（缺失行被截断丢弃 ${coverage.dropped_missing_rows} 条）`
      );
    }
    const markText = marks.length
      ? ` 截断标记：${escapeHtml(marks.join(" · "))}。`
      : "";
    return `<p class="wb-muted">${head}：${parts.join("；")}。${markText}「账本缺席 / 来源未启用」= 没有可读数据，与「该窗口确实无用量」不同；两者都不按 0 显示。</p>`;
  }
  /** Session-derived reads gate (ops ledger live or runtime present). */
  private sessionLive(): boolean {
    return this.opsRecent?.state === "live" || Boolean(this.runtime);
  }
  /** Action registry (service.start/stop enablement + honest reasons). */
  private async fetchActions() {
    try {
      const response = await fetch("/gw/usagesrv/api/console/actions", {
        credentials: "include",
      });
      if (!response.ok) {
        this.actions = undefined;
        return;
      }
      const data = await response.json();
      const actions = data?.actions;
      if (!Array.isArray(actions)) {
        this.actions = undefined;
        return;
      }
      this.actions = actions.filter((a: unknown) => {
        const row = a as ActionRow;
        return typeof row?.action === "string";
      }) as ActionRow[];
    } catch {
      this.actions = undefined;
    }
  }
  /** Recent real operations from the 8001 ledger (management session). `undefined`
   *  = 本次读取尚未返回（渲染为「读取中」，不是「不可读」）；`gated` = 读取已
   *  失败，`reason` 是实测原因（HTTP 状态 / 形状不符 / 传输失败），所以任何
   *  界面都不必把「8001 应答了错误」写成「不可达」。 */
  private async fetchRecentOps() {
    try {
      const response = await fetch(
        "/gw/usagesrv/api/console/operations?limit=8",
        { credentials: "include" }
      );
      if (!response.ok) {
        this.opsRecent = {
          state: "gated",
          rows: [],
          reason: `HTTP ${response.status}`,
        };
        return;
      }
      const data = await response.json();
      const operations = data?.operations;
      if (!Array.isArray(operations)) {
        this.opsRecent = {
          state: "gated",
          rows: [],
          reason: "响应形状与契约不符（缺 operations[]）",
        };
        return;
      }
      this.opsRecent = {
        state: "live",
        rows: operations.filter((op: unknown) => {
          const row = op as OpRow;
          return (
            typeof row?.id === "string" &&
            typeof row?.action === "string" &&
            typeof row?.state === "string"
          );
        }) as OpRow[],
      };
    } catch {
      this.opsRecent = {
        state: "gated",
        rows: [],
        reason: "网络失败或服务未运行",
      };
    }
  }
  /** 操作账本不可读时的实测原因（与首层横幅同一口径）：应答了 HTTP 错误就写
   *  应答错误，只有 fetch 本身失败才是传输层失败——客户端没测到「不可达」。 */
  private opsReadFailureText(): string {
    const reason = this.opsRecent?.reason;
    return `8001 操作账本读取失败（${reason || "原因未知"}）`;
  }
  /** Share facade reads (A36-2 narrow facade /api/console/share-management/*).
   *  Honest states only: non-ok / network / shape mismatch = error with its
   *  own reason; never substitutes fixture data (R9-R35 honest-gate rule).
   *  维护者 2026-09-28：非 2xx 的原因按分享失败原因的 ④ 口径取
   *  `shareReadReason()`——后端错误码与实测 HTTP 状态只写一次。 */
  private fetchShareStatus(): Promise<void> {
    if (this.shareStatusRead) return this.shareStatusRead;
    const pending = this.readShareStatus().finally(() => {
      if (this.shareStatusRead === pending) this.shareStatusRead = undefined;
    });
    this.shareStatusRead = pending;
    return pending;
  }
  private async readShareStatus() {
    try {
      const { response, data: payload } = await readShareJson(
        "/gw/usagesrv/api/console/share-management/status",
        { credentials: "include" }
      );
      if (!response.ok) {
        const body = payload as { error?: string; reason?: string } | null;
        this.shareStatus = {
          state: "error",
          reason: shareReadReason(body, response.status),
        };
        return;
      }
      const data = payload as ShareStatusPayload | null;
      if (!data || typeof data !== "object") {
        this.shareStatus = { state: "error", reason: "响应形状不符" };
        return;
      }
      this.shareStatus = { state: "live", payload: data };
    } catch (error) {
      this.shareStatus = {
        state: "error",
        reason: error instanceof Error ? error.message : String(error),
      };
    }
  }
  /** R11-F2 (Plan-Revision 11): the "API Key" column shows the FULL
   *  plaintext for rows whose backend flag says a same-identity ciphertext
   *  exists — fetched once per row per page lifetime from the narrow per-key
   *  endpoint, then patched into that row's cell in place. Legacy hash-only
   *  rows are never requested and keep the honest 不可恢复 note. The plaintext
   *  lives only in this in-memory map plus the rendered cell.
   *  P1 (2026-09-24): the cache is version-aware — a row whose version
   *  moved drops its cached plaintext and re-reads with the gate; a row that
   *  stopped being viewable drops it outright. */
  private syncShareKeySecrets(rows: ShareKeyRow[]) {
    for (const [id, test] of this.shareKeyTests) {
      const row = rows.find((r) => r.id === id);
      if (!row || row.row_version !== test.rowVersion || !row.enabled)
        this.shareKeyTests.delete(id);
    }
    // P2 (2026-09-24): a row that LEFT the list keeps no cached plaintext —
    // the list read is the authority for which keys still exist, so a deleted or
    // revoked key must not keep its plaintext in memory until a page reload.
    const live = new Set<string>();
    for (const row of rows) {
      const id = row.id ?? "";
      if (!id) continue;
      live.add(id);
      const rowVersion =
        typeof row.row_version === "number" ? row.row_version : null;
      if (row.plaintext_viewable !== true) {
        this.shareKeySecrets.delete(id);
        this.shareKeySecretNotes.delete(id);
        continue;
      }
      const cached = this.shareKeySecrets.get(id);
      // P2 (2026-09-24): only a row that moved FORWARD invalidates the
      // cached plaintext. A stale list (version behind the rotation this page
      // already saw) must not delete the newer value and re-read with an old
      // version — the backend would refuse that as version_conflict and the row
      // would briefly show 列表版本已变化 instead of the fresh plaintext.
      if (
        cached &&
        cached.rowVersion !== null &&
        rowVersion !== null &&
        cached.rowVersion < rowVersion
      ) {
        this.shareKeySecrets.delete(id);
        this.shareKeySecretNotes.delete(id);
      }
      // 契约 4.8 / 边界：完整 Key **默认掩码**，一次性展示走受控入口。这里原先
      // 在列表读取后自动为每个 `plaintext_viewable` 行发一次明文读回，于是页面
      // 一进分享页就把完整 Key 直接渲染出来——不需要任何用户动作。自动读回因此
      // 取消，改由该行自己的「显示明文」按钮触发（一次点击 = 一次读回）。
    }
    for (const id of [...this.shareKeySecrets.keys()]) {
      if (live.has(id)) continue;
      this.shareKeySecrets.delete(id);
      this.shareKeySecretNotes.delete(id);
    }
  }
  /** Explicit public HTTPS round trip, including a small real model reply. */
  private async testShareKey(row: ShareKeyRow) {
    const id = row.id ?? "";
    const rowVersion = Number(row.row_version);
    if (
      !id ||
      !row.enabled ||
      !Number.isInteger(rowVersion) ||
      rowVersion < 1 ||
      this.shareKeyTests.get(id)?.state === "busy"
    )
      return;
    const pending: ShareKeyTest = {
      rowVersion,
      state: "busy",
      reason: "",
      startedAt: Date.now() / 1000,
      publicUrl:
        this.shareStatus?.state === "live"
          ? (this.shareStatus.payload.share_url ?? undefined)
          : undefined,
    };
    this.shareKeyTests.set(id, pending);
    this.renderPanel();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 95000);
    try {
      const response = await fetch(
        `/gw/usagesrv/api/console/share-management/keys/${encodeURIComponent(id)}/test`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
          },
          body: JSON.stringify({ expected_row_version: rowVersion }),
          signal: controller.signal,
        }
      );
      const data = await response.json();
      if (this.shareKeyTests.get(id) !== pending) return;
      const current = this.shareKeyRow(id);
      if (!current || current.row_version !== rowVersion || !current.enabled) {
        this.shareKeyTests.delete(id);
        return;
      }
      const valid =
        response.ok &&
        data?.key_id === id &&
        data?.row_version === rowVersion &&
        data?.tested_from === "public" &&
        typeof data?.connected === "boolean" &&
        (!data.connected ||
          (data.stage === "inference" &&
            data.http_status === 200 &&
            typeof data.model === "string" &&
            data.model.length > 0 &&
            typeof data.public_ip === "string" &&
            data.public_ip.length > 0));
      this.shareKeyTests.set(id, {
        ...pending,
        state: valid && data.connected ? "ok" : "error",
        reason:
          valid && typeof data.reason === "string"
            ? data.reason
            : "probe_contract_mismatch",
        at:
          valid && typeof data.checked_at === "number"
            ? data.checked_at
            : undefined,
        stage: valid && typeof data.stage === "string" ? data.stage : undefined,
        model: valid && typeof data.model === "string" ? data.model : undefined,
        elapsedMs:
          valid && typeof data.elapsed_ms === "number"
            ? data.elapsed_ms
            : undefined,
        publicUrl:
          valid && typeof data.public_url === "string"
            ? data.public_url
            : pending.publicUrl,
        publicIp:
          valid && typeof data.public_ip === "string"
            ? data.public_ip
            : undefined,
        dns: valid ? normalizePublicDnsDetail(data.dns) : undefined,
      });
    } catch {
      if (this.shareKeyTests.get(id) === pending)
        this.shareKeyTests.set(id, {
          ...pending,
          state: "error",
          reason: controller.signal.aborted ? "timeout" : "connection_failed",
        });
    } finally {
      clearTimeout(timer);
      this.renderPanel();
    }
  }

  private revealShareKeySecret(row: ShareKeyRow) {
    const id = row.id ?? "";
    if (!id || row.plaintext_viewable !== true) return;
    if (this.shareKeySecrets.has(id) || this.shareKeySecretBusy.has(id)) return;
    const rowVersion =
      typeof row.row_version === "number" ? row.row_version : null;
    this.shareKeySecretBusy.add(id);
    this.renderPanel();
    void this.fetchShareKeySecret(id, rowVersion).finally(() => {
      this.shareKeySecretBusy.delete(id);
      this.renderPanel();
    });
  }
  /** P1 (2026-09-24): the ONE place a create/rotate response plants a
   *  plaintext: stores it with the row version it came back at, clears any
   *  inline note, and bumps the per-key read sequence so an in-flight
   *  plaintext read started earlier can never overwrite it. */
  private setShareKeySecret(
    keyId: string,
    secret: string,
    rowVersion: number | null
  ) {
    this.shareKeySecrets.set(keyId, { secret, rowVersion });
    this.shareKeySecretNotes.delete(keyId);
    this.shareKeySecretSeq.set(
      keyId,
      (this.shareKeySecretSeq.get(keyId) ?? 0) + 1
    );
  }
  private async fetchShareKeySecret(
    keyId: string,
    rowVersion: number | null
  ): Promise<void> {
    // P1 (2026-09-24): the request owns a sequence number; a response that
    // is no longer the newest for this key is dropped, so a stale read can
    // never overwrite a newer plaintext (rotation or newer read).
    const seq = (this.shareKeySecretSeq.get(keyId) ?? 0) + 1;
    this.shareKeySecretSeq.set(keyId, seq);
    const gate =
      rowVersion !== null ? `?expected_row_version=${rowVersion}` : "";
    try {
      const { response, data: payload } = await readShareJson(
        `/gw/usagesrv/api/console/share-management/keys/${encodeURIComponent(keyId)}/secret${gate}`,
        { credentials: "include" }
      );
      if ((this.shareKeySecretSeq.get(keyId) ?? 0) !== seq) return;
      if (!response.ok) {
        // P2 (2026-09-24): a refused readback must not leave the row on
        // 读取中… forever — write the honest inline failure state instead.
        this.shareKeySecretNotes.set(
          keyId,
          `fetch_failed:HTTP ${response.status}`
        );
        this.patchShareKeyCell(keyId);
        return;
      }
      const data = payload as {
        secret?: string;
        secret_unavailable?: boolean;
        reason?: string;
      } | null;
      if ((this.shareKeySecretSeq.get(keyId) ?? 0) !== seq) return;
      if (data && typeof data.secret === "string" && data.secret) {
        this.shareKeySecrets.set(keyId, { secret: data.secret, rowVersion });
        this.shareKeySecretNotes.delete(keyId);
      } else if (data?.secret_unavailable === true) {
        // R11-F2: the backend answers distinct inline states — show the
        // reason where the key is, so the row never sticks on 读取中….
        this.shareKeySecrets.delete(keyId);
        this.shareKeySecretNotes.set(
          keyId,
          String(data.reason ?? "unavailable")
        );
      } else {
        // P2 (2026-09-24): 2xx with a body that carries neither a secret
        // nor an inline unavailable state (or is not JSON at all) must not
        // leave the row on 读取中… — that is the one state the copy above
        // promises the user will never see.
        this.shareKeySecrets.delete(keyId);
        this.shareKeySecretNotes.set(keyId, "fetch_failed:unexpected_body");
      }
      this.patchShareKeyCell(keyId);
    } catch {
      // P2 (2026-09-24): network failure is an inline state too (retry
      // rides the next lane entry / refresh), never an endless 读取中….
      if ((this.shareKeySecretSeq.get(keyId) ?? 0) !== seq) return;
      this.shareKeySecretNotes.set(keyId, "fetch_failed:network");
      this.patchShareKeyCell(keyId);
    }
  }
  private patchShareKeyCell(keyId: string) {
    const cell = this.root.querySelector<HTMLElement>(
      `td[data-share-key-cell="${CSS.escape(keyId)}"]`
    );
    if (!cell) return;
    const row =
      this.shareKeys?.state === "live"
        ? this.shareKeys.rows.find((r) => (r.id ?? "") === keyId)
        : undefined;
    if (row) cell.innerHTML = this.shareKeyValueCellHtml(row);
  }
  private shareKeyValueCellHtml(row: ShareKeyRow): string {
    const id = row.id ?? "";
    const cached = this.shareKeySecrets.get(id);
    if (cached) {
      return (
        `<code class="wb-share-key-plain">${escapeHtml(cached.secret)}</code> ` +
        `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="copy-secret" data-wb-share-key-id="${escapeHtml(id)}" title="复制该 Key 的明文（仅本次会话内存）" aria-label="复制该 Key 的明文">${wbIcon("copy")}${wbIconHint("复制该 Key 的明文")}</button>`
      );
    }
    const note = this.shareKeySecretNotes.get(id);
    if (note === "legacy") {
      return `<code>${escapeHtml(row.masked ?? "—")}</code> <small class="wb-share-key-legacy-note">旧密钥不可恢复，手动轮换后可见</small>`;
    }
    if (note === "undecryptable") {
      return `<code>${escapeHtml(row.masked ?? "—")}</code> <small class="wb-share-key-legacy-note">密文不可解密（换身份/迁机后不可恢复），手动轮换后可见</small>`;
    }
    if (note === "version_conflict") {
      return `<code>${escapeHtml(row.masked ?? "—")}</code> <small class="wb-share-key-legacy-note">列表版本已变化，刷新后重看</small>`;
    }
    if (note?.startsWith("fetch_failed")) {
      // P2 (2026-09-24): a refused or failed readback is an inline state.
      // 404/410 are permanent (the key is gone or unknown) — promising a
      // 刷新 retry there would be a claim the endpoint cannot honour.
      const detail = note.slice("fetch_failed:".length) || "未知原因";
      const permanent = /HTTP (404|410)/.test(detail);
      const hint = permanent ? "该 Key 已不存在，请刷新列表" : "点「刷新」重试";
      return `<code>${escapeHtml(row.masked ?? "—")}</code> <small class="wb-share-key-legacy-note">明文读取失败（${escapeHtml(detail)}），${hint}</small>`;
    }
    if (note) {
      // Any other backend reason still gets an honest line — never 读取中….
      return `<code>${escapeHtml(row.masked ?? "—")}</code> <small class="wb-share-key-legacy-note">明文不可用（${escapeHtml(note)}）</small>`;
    }
    if (row.plaintext_viewable === true) {
      // 契约 4.8 / 边界：完整 Key **默认掩码**，一次性展示走受控入口。这里原先
      // 在列表读取后自动发明文读回，于是页面一进分享页就把完整 Key 渲染出来。
      // 现在默认只显示后端自己的 `masked`，读回由用户点「显示明文」触发。
      if (this.shareKeySecretBusy.has(id)) {
        return `<code>${escapeHtml(row.masked ?? "—")}</code> <small class="wb-muted">明文读取中…</small>`;
      }
      return (
        `<code>${escapeHtml(row.masked ?? "—")}</code> ` +
        `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="reveal-secret" data-wb-share-key-id="${escapeHtml(id)}" title="一次性读回该 Key 的明文（只读、按需一次、仅本页内存）" aria-label="显示该 Key 的明文">${wbIcon("eye")}显示明文</button>`
      );
    }
    return `<code>${escapeHtml(row.masked ?? "—")}</code> <small class="wb-share-key-legacy-note">旧密钥不可恢复，手动轮换后可见</small>`;
  }
  private async fetchShareKeys() {
    // P2 (2026-09-24): the read owns a sequence number — a list read that
    // is superseded while in flight must not apply its rows (and must not
    // let the prune drop a plaintext the newer state already holds).
    const seq = (this.shareKeysSeq ?? 0) + 1;
    this.shareKeysSeq = seq;
    try {
      const { response, data: payload } = await readShareJson(
        "/gw/usagesrv/api/console/share-management/keys",
        { credentials: "include" }
      );
      if ((this.shareKeysSeq ?? 0) !== seq) return;
      if (!response.ok) {
        const body = payload as { error?: string; reason?: string } | null;
        // P2 (2026-09-24): the guard applies after this await too — a
        // superseded read must not overwrite a newer live list with an
        // error state.
        if ((this.shareKeysSeq ?? 0) !== seq) return;
        // 维护者 2026-09-28：与状态读口同一条规则——后端错误码与实测 HTTP 状态只写一次。
        this.shareKeys = {
          state: "error",
          reason: shareReadReason(body, response.status),
        };
        return;
      }
      const data = payload as { keys?: ShareKeyRow[] } | null;
      if ((this.shareKeysSeq ?? 0) !== seq) return;
      const rows = Array.isArray(data?.keys) ? data.keys : null;
      if (!rows) {
        this.shareKeys = { state: "error", reason: "响应缺少 keys 数组" };
        return;
      }
      this.shareKeys = { state: "live", rows };
      this.syncShareKeySecrets(rows);
    } catch (error) {
      if ((this.shareKeysSeq ?? 0) !== seq) return;
      this.shareKeys = {
        state: "error",
        reason: error instanceof Error ? error.message : String(error),
      };
    }
  }
  /** 分享用量读口：`GET /api/console/share-usage`（只读、无参数、无请求体）。
   *
   *  路由：控制台信任面（本机同源免凭据直读），**不是**需管理令牌的
   *  `GET /api/share/usage`。跨端交付契约（output/ai-gw-round3-20260926/
   *  cross-line-delivery.md §1/§2）：本机页面直接 fetch，不带 credentials、
   *  不带任何令牌头；管理令牌绝不进网页。
   *
   *  状态按**实测 HTTP 状态**分辨，绝不折成「每 Key 0 请求」：
   *  · 404 = 当前服务端版本未载入该路由（读口未开放）；
   *  · 401/403 = 请求打到了分享管理面或别的路由（本口免凭据，这不是
   *    本读口的预期状态）；
   *  · 其余非 2xx（503 share_metering 未载入 / 503 账本路径解析不出 /
   *    502 账本读取失败 / 502 形状不符）= 读口存在但读取失败。
   *  请求序号守卫照 fetchShareKeys：被超越的在途响应不覆盖新状态。 */
  private async fetchShareUsage() {
    const seq = (this.shareUsageSeq ?? 0) + 1;
    this.shareUsageSeq = seq;
    try {
      const { response, data: payload } = await readShareJson(
        "/gw/usagesrv/api/console/share-usage"
      );
      if ((this.shareUsageSeq ?? 0) !== seq) return;
      if (!response.ok) {
        const body = payload as { error?: string } | null;
        if ((this.shareUsageSeq ?? 0) !== seq) return;
        const code = body?.error || "";
        // 单一原因串，且保留实测 HTTP 状态：后端 error 自身已带状态（例如
        // 「HTTP 500」）时原样使用，否则补一次「（HTTP 500）」；无可读错误体时
        // 只剩「HTTP 500」。四个分支都不再无条件追加第二次状态——原先 error
        // 分支会印出「HTTP 500（HTTP 500）」。
        const reason = code
          ? code.includes(`HTTP ${response.status}`)
            ? code
            : `${code}（HTTP ${response.status}）`
          : `HTTP ${response.status}`;
        this.shareUsage =
          response.status === 404
            ? {
                state: "unavailable",
                status: response.status,
                reason: `${reason}（当前服务端版本未载入该读口）`,
              }
            : response.status === 401 || response.status === 403
              ? {
                  state: "refused",
                  reason: `${reason}（本口免凭据；401/403 说明请求打到了分享管理面或别的路由）`,
                }
              : { state: "error", reason };
        return;
      }
      const data = payload as ShareUsagePayload | null;
      if ((this.shareUsageSeq ?? 0) !== seq) return;
      if (
        !data ||
        typeof data !== "object" ||
        !Array.isArray(data.keys) ||
        !data.unattributed
      ) {
        this.shareUsage = {
          state: "error",
          reason: "响应形状与契约不符（缺 keys[] / unattributed）",
        };
        return;
      }
      this.shareUsage = { state: "live", payload: data };
    } catch (error) {
      if ((this.shareUsageSeq ?? 0) !== seq) return;
      this.shareUsage = {
        state: "error",
        reason: error instanceof Error ? error.message : "网络失败或服务未运行",
      };
    }
  }
  /** S5 公网巡检读面（`GET /api/console/share-management/public`）。
   *  归一化在 `src/console/backend/normalize.ts`，读取走同一个 BackendClient；
   *  页面只展示后端答案，缺失字段写「未提供」，不伪造正常快照。 */
  private sharePublicClient(): BackendClient {
    return (
      this.publicShareClientInstance ??
      (this.publicShareClientInstance = new BackendClient(
        createFetchTransport({ fetch: (input, init) => fetch(input, init) })
      ))
    );
  }
  private fetchPublicShare(): Promise<void> {
    if (this.publicShareRead) return this.publicShareRead;
    const pending = this.readPublicShare().finally(() => {
      if (this.publicShareRead === pending) this.publicShareRead = undefined;
    });
    this.publicShareRead = pending;
    return pending;
  }
  private async readPublicShare() {
    try {
      const result = await this.sharePublicClient().readPublicShareStatus();
      if (result.state === "ready" && result.value) {
        this.publicShare = { state: "live", payload: result.value };
        return;
      }
      this.publicShare = {
        state:
          result.state === "unavailable"
            ? "unavailable"
            : result.state === "refused"
              ? "refused"
              : "error",
        reason:
          result.reason ??
          (result.state === "unavailable"
            ? "当前服务端版本未载入公网巡检读口"
            : "公网巡检读取失败"),
      };
    } catch (error) {
      this.publicShare = {
        state: "error",
        reason: error instanceof Error ? error.message : "公网巡检读取失败",
      };
    }
  }
  /** 脱敏诊断导出：一次只读 GET，成功后用后端 `export_diagnostics()` 的
   *  归一化内容生成 Blob 下载；失败如实显示，不伪造文件。 */
  private async downloadPublicShareExport() {
    this.publicShareExport = {
      state: "unconfirmed",
      text: "正在生成脱敏诊断导出…",
    };
    this.renderPanel();
    try {
      const result = await this.sharePublicClient().readPublicShareExport();
      if (result.state !== "ready" || !result.value) {
        this.publicShareExport = {
          state: "failed",
          text: `导出失败：${result.reason ?? result.state}（未生成文件）`,
        };
        this.renderPanel();
        return;
      }
      const payload = result.value;
      const generated = payload.generatedAt
        ? Date.parse(payload.generatedAt)
        : Number.NaN;
      const stamp = Number.isFinite(generated)
        ? new Date(generated)
            .toISOString()
            .replace(/[-:]/g, "")
            .replace(/\..*$/, "")
            .replace("T", "-")
        : new Date()
            .toISOString()
            .replace(/[-:]/g, "")
            .replace(/\..*$/, "")
            .replace("T", "-");
      const blob = new Blob([JSON.stringify(payload, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `share-public-diagnostics-${stamp}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
      this.publicShareExport = {
        state: "ok",
        text: `已导出脱敏诊断（事件 ${payload.events?.length ?? "未提供"} 条；只含白名单字段）`,
      };
    } catch (error) {
      this.publicShareExport = {
        state: "failed",
        text: `导出失败：${error instanceof Error ? error.message : "未知错误"}（未生成文件）`,
      };
    }
    this.renderPanel();
  }
  /** Refresh the share reads (lane entry, post-operation, manual refresh,
   *  poll tick). R9-R46: the in-flight boolean dedupes overlapping reads —
   *  the lane re-renders many times per minute and each repaint must not
   *  stack requests — while a queued follow-up guarantees that a terminal
   *  state arriving mid-read still produces one fresh read afterwards.
   *  分享用量读口（/api/console/share-usage）与公网巡检读口走同一入口，
   *  所以各块数据永远来自同一次刷新，不会各自漂移。 */
  private refreshShareData(): void {
    if (this.shareFetchInFlight) {
      this.shareFetchQueued = true;
      return;
    }
    this.shareFetchInFlight = true;
    void Promise.all([
      this.fetchShareStatus(),
      this.fetchShareKeys(),
      this.fetchShareUsage(),
      this.fetchPublicShare(),
    ])
      .catch(() => undefined)
      .finally(() => {
        this.shareFetchInFlight = false;
        this.renderPanel();
        if (this.shareFetchQueued) {
          this.shareFetchQueued = false;
          this.refreshShareData();
        }
      });
  }
  /** Key create → 201. The plaintext comes back in this immediate response
   *  AND stays retrievable through the narrow per-key endpoint for the same
   *  Windows run identity (R11-B1), so the keys block's API Key cell can
   *  show it again in any later page session; legacy hash-only keys stay
   *  unrecoverable until a manual rotation. Never localStorage, never
   *  operation records, never generic feedback (R9-R46). The modal stays
   *  open until its response lands (P2 2026-09-24). */
  private async createShareKey(name: string, cap?: number) {
    this.shareKeyError = "";
    try {
      const response = await fetch(
        "/gw/usagesrv/api/console/share-management/keys",
        {
          method: "POST",
          credentials: "include",
          headers: {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
          },
          body: JSON.stringify(cap ? { name, max_concurrency: cap } : { name }),
        }
      );
      const data = (await response.json().catch(() => null)) as {
        key?: ShareKeyRow;
        plaintext?: string;
        error?: string;
        reason?: string;
      } | null;
      if (!response.ok) {
        this.shareKeyError =
          data?.reason || data?.error || `HTTP ${response.status}`;
        // P2 (2026-09-24): this early return must reset the in-flight
        // marker too — leaving it set pinned the 创建 button on
        // 创建中… until a full page reload (a duplicate name answers 409,
        // so this path is reachable from the UI).
        this.shareKeySubmitting = false;
        this.renderPanel();
        return;
      }
      if (typeof data?.plaintext === "string" && data.plaintext) {
        this.sharePlaintext = {
          kind: "created",
          name: data.key?.name ?? name,
          value: data.plaintext,
          at: Date.now(),
        };
        this.shareSecretHidden = false;
        this.shareSecretCleared = false;
        // R11-F2: the row's full key becomes viewable immediately (and the
        // map survives repaints); the resident section below keeps its own
        // session-memory display. P1 (2026-09-24): the entry carries the
        // response row's version and bumps the read sequence, so a plaintext
        // read that started before this create can never land on top of it.
        if (data.key?.id) {
          this.shareKeyPlaintextViewable = data.key.plaintext_viewable === true;
          this.setShareKeySecret(
            data.key.id,
            data.plaintext,
            typeof data.key.row_version === "number"
              ? data.key.row_version
              : null
          );
        } else {
          this.shareKeyPlaintextViewable = null;
        }
        // The modal stays open and flips to its success view; closing is the
        // user's 完成 click (R9-R46).
        this.shareKeyModalView = "success";
      } else {
        this.shareKeyError = "响应缺少明文；请到该 Key 行用「轮换」重新生成";
      }
    } catch (error) {
      this.shareKeyError =
        error instanceof Error ? error.message : String(error);
    }
    // P2 (2026-09-24): the submit button is disabled only while the
    // request is in flight — the modal is no longer closed on submit, so
    // this is what keeps one create from being submitted twice.
    this.shareKeySubmitting = false;
    this.refreshShareData();
    this.renderPanel();
  }
  /** Key patch (enable toggle / rename / per-key cap). Non-destructive
   *  config writes ride expected_row_version; version conflicts surface as
   *  409 with the server's own reason — no silent overwrite (A36-2). */
  private async patchShareKey(
    id: string,
    patch: Record<string, unknown>,
    rowVersion: number
  ) {
    try {
      const response = await fetch(
        `/gw/usagesrv/api/console/share-management/keys/${encodeURIComponent(id)}`,
        {
          method: "PATCH",
          credentials: "include",
          headers: {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
          },
          body: JSON.stringify({ ...patch, expected_row_version: rowVersion }),
        }
      );
      const data = (await response.json().catch(() => null)) as {
        error?: string;
        reason?: string;
      } | null;
      if (!response.ok) {
        this.shareKeyError =
          data?.reason || data?.error || `HTTP ${response.status}`;
      } else {
        this.shareKeyError = "";
      }
    } catch (error) {
      this.shareKeyError =
        error instanceof Error ? error.message : String(error);
    }
    this.refreshShareData();
  }
  /** Rotate → new plaintext once from the server; revoke-and-terminate → key
   *  disabled plus in-flight termination; delete → remove. Rotate's plaintext
   *  follows the same session-memory contract as create (viewable in the
   *  keys-block plaintext section until cleared or reload, R9-R46). Every
   *  destructive/rotating op rides expected_row_version (optimistic gate,
   *  A36-2); the version comes from the live list row — without it the op
   *  cannot be gated honestly, so the action is refused with its own reason
   *  instead of sent blind. (delete sends the gate in the DELETE body; the
   *  facade requires a positive integer on all three.) */
  private async shareKeyOp(
    action: "rotate" | "revoke-terminate" | "delete",
    id: string
  ) {
    const base = `/gw/usagesrv/api/console/share-management/keys/${encodeURIComponent(id)}`;
    const row = this.shareKeyRow(id);
    const rowVersion =
      typeof row?.row_version === "number" ? row.row_version : undefined;
    if (!id || rowVersion === undefined) {
      this.shareKeyError = "缺少行版本（密钥列表未就绪或已变化），请刷新后重试";
      this.renderPanel();
      return;
    }
    const init: RequestInit = {
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      method: action === "delete" ? "DELETE" : "POST",
      body: JSON.stringify({ expected_row_version: rowVersion }),
    };
    const url =
      action === "rotate"
        ? `${base}/rotate`
        : action === "revoke-terminate"
          ? `${base}/revoke-and-terminate`
          : base;
    try {
      const response = await fetch(url, init);
      const data = (await response.json().catch(() => null)) as {
        plaintext?: string;
        key?: ShareKeyRow;
        error?: string;
        reason?: string;
      } | null;
      if (!response.ok) {
        this.shareKeyError =
          data?.reason || data?.error || `HTTP ${response.status}`;
      } else {
        if (
          action === "rotate" &&
          typeof data?.plaintext === "string" &&
          data.plaintext
        ) {
          this.sharePlaintext = {
            kind: "rotated",
            name: data.key?.name ?? "",
            value: data.plaintext,
            at: Date.now(),
          };
          // R11-F2: the rotated row shows the NEW key immediately; the old
          // plaintext is gone (the backend overwrote the ciphertext). P1
          // (2026-09-24): version + sequence bump — an in-flight read of
          // the OLD version can never overwrite this value.
          if (data.key?.id) {
            this.shareKeyPlaintextViewable =
              data.key.plaintext_viewable === true;
            this.setShareKeySecret(
              data.key.id,
              data.plaintext,
              typeof data.key.row_version === "number"
                ? data.key.row_version
                : null
            );
          } else {
            this.shareKeyPlaintextViewable = null;
          }
        }
        this.shareKeyError = "";
      }
    } catch (error) {
      this.shareKeyError =
        error instanceof Error ? error.message : String(error);
    }
    this.refreshShareData();
    this.renderPanel();
  }
  /** Cost/summary fetch with per-failure honest states (R20-05): 401 =
   *  no management session; 403 = permission refused; 5xx / network /
   *  shape mismatch = error with its own note. Never falls back to fixture. */
  private async fetchSummary() {
    // Independent in-flight guard: the poll interval (30s) and the summary
    // latency (tens of seconds on the production ledger) can overlap, and
    // two concurrent full-ledger aggregations are pure waste.
    if (this.summaryInFlight) return;
    this.summaryInFlight = true;
    try {
      await this.fetchSummaryInner();
    } finally {
      this.summaryInFlight = false;
    }
  }
  private async fetchSummaryInner() {
    let response: Response;
    try {
      response = await fetch("/gw/usagesrv/api/summary", {
        credentials: "include",
      });
    } catch {
      this.summaryCause = "8001 不可达（网络失败或服务未运行）";
      this.enterSummaryError("无法连接费用服务（网络失败或未运行）");
      return;
    }
    if (response.status === 401) {
      this.summaryCause = "8001 拒绝读取（HTTP 401，无凭据会话）";
      this.summaryState = "unauthenticated";
      this.summaryError = "";
      return;
    }
    if (response.status === 403) {
      this.summaryCause = "管理权限被拒（HTTP 403）";
      this.summaryState = "forbidden";
      return;
    }
    if (response.status >= 500) {
      this.summaryCause = `8001 应答错误（HTTP ${response.status}）`;
      this.enterSummaryError(`费用服务错误（HTTP ${response.status}）`);
      return;
    }
    if (!response.ok) {
      this.summaryCause = `8001 异常应答（HTTP ${response.status}）`;
      this.enterSummaryError(`费用服务异常响应（HTTP ${response.status}）`);
      return;
    }
    let data: unknown;
    try {
      data = await response.json();
    } catch {
      this.summaryCause = "8001 应答无法解析（响应体不是 JSON）";
      this.enterSummaryError("费用响应解析失败");
      return;
    }
    const services = (data as { services?: unknown })?.services;
    if (!services || typeof services !== "object") {
      this.summaryCause = "8001 应答缺少 services 对象（形状不符）";
      this.enterSummaryError("费用响应形状与预期不符");
      return;
    }
    let requests = 0,
      tokens = 0,
      cost = 0,
      credits = 0,
      sawAny = false;
    // 跨链费用下界传播：任一行 costCnyPartial（含 null+partial，对下界贡献 0）→ 合计
    // 只能是下界；任一行 null 且不带 partial → 合计连下界都给不出，是「未知」。
    let costPartial = false,
      costUnknown = false;
    const chainRows: ChainFee[] = [];
    for (const [name, svc] of Object.entries(
      services as Record<string, unknown>
    )) {
      // ADR 0019: the M365 ledger stays untouched on the server, but the
      // console cost lane excludes the offline chain from its aggregation.
      if (String(name).toLowerCase() === "m365") continue;
      const s = svc as {
        requests?: unknown;
        tokens?: { input?: unknown; output?: unknown };
        input?: unknown;
        output?: unknown;
        costCny?: unknown;
        costCnyPartial?: unknown;
        credits?: unknown;
        models?: unknown;
      } | null;
      if (!s || typeof s !== "object") {
        this.summaryCause = `8001 应答里 ${String(name)} 不是对象（字段形状不符）`;
        this.enterSummaryError("费用响应字段形状与预期不符");
        return;
      }
      sawAny = true;
      requests += Number(s.requests) || 0;
      tokens +=
        (Number(s.tokens?.input ?? s.input) || 0) +
        (Number(s.tokens?.output ?? s.output) || 0);
      if (s.costCny === undefined) {
        // 该链没有 ¥ 分量（积分链）：不进合计，也不标 partial。
      } else if (s.costCny === null) {
        // 整节点金额全部无法折算：带 partial 的行只含可折算记录（下界贡献 0），
        // 不带 partial 的行连下界都给不出 → 合计为「未知」。
        if (s.costCnyPartial === true) costPartial = true;
        else costUnknown = true;
      } else {
        cost += Number(s.costCny) || 0;
        if (s.costCnyPartial === true) costPartial = true;
      }
      credits += Number(s.credits) || 0;
      chainRows.push({
        name: String(name),
        requests: Number(s.requests) || 0,
        input: Number(s.tokens?.input ?? s.input) || 0,
        output: Number(s.tokens?.output ?? s.output) || 0,
        costCny:
          s.costCny === undefined
            ? undefined
            : s.costCny === null
              ? null
              : Number(s.costCny) || 0,
        costCnyPartial: s.costCnyPartial === true ? true : undefined,
        credits: s.credits === undefined ? undefined : Number(s.credits) || 0,
        models: (Array.isArray(s.models) ? s.models : []).flatMap((m) => {
          const row = m as {
            model?: unknown;
            requests?: unknown;
            input?: unknown;
            output?: unknown;
            cache?: unknown;
            priced?: unknown;
            unpriced?: unknown;
            costCny?: unknown;
            costCnyPartial?: unknown;
            credits?: unknown;
          };
          if (typeof row?.model !== "string" || !row.model) return [];
          return [
            {
              model: row.model,
              requests: Number(row.requests) || 0,
              input: Number(row.input) || 0,
              output: Number(row.output) || 0,
              // 缓存输入 / 已定价 / 未定价（维护者 2026-10-19，chat2api 模型费用统计）：
              // 账本字段缺失时按 0 取用（与上面 requests/input/output 同一既有口径）；
              // 只有 chat2api 链的模型行带这三个字段（_bucket 每模型恒写 cache/priced/unpriced）。
              cache: Number(row.cache) || 0,
              priced: Number(row.priced) || 0,
              unpriced: Number(row.unpriced) || 0,
              costCny:
                row.costCny === undefined
                  ? undefined
                  : row.costCny === null
                    ? null
                    : Number(row.costCny) || 0,
              costCnyPartial: row.costCnyPartial === true ? true : undefined,
              credits:
                row.credits === undefined
                  ? undefined
                  : Number(row.credits) || 0,
            },
          ];
        }),
      });
    }
    if (!sawAny) {
      this.summaryCause = "8001 应答的 services 为空（缺少服务聚合）";
      this.enterSummaryError("费用响应缺少服务聚合");
      return;
    }
    this.summaryLive = {
      requests,
      tokens,
      costCny: cost.toFixed(3),
      costCnyPartial: costPartial,
      costCnyUnknown: costUnknown,
      credits,
    };
    this.chainFees = chainRows;
    this.summarySeries = this.deriveSeries(services as Record<string, unknown>);
    this.summaryState = "live";
    this.summaryEpoch = Date.now();
    this.summaryCause = "";
  }
  /** Merge per-day/per-model aggregates across the aggregated chains (M365
   *  excluded per ADR 0019, matching the cost lane). Unknown shapes and
   *  malformed dates are skipped, not guessed. */
  /** R9-R31: model.* results feed the model lane (catalog tables, sync
   *  diffs) — remembered per action instead of only shown once. */
  private deriveSeries(services: Record<string, unknown>): SummarySeries {
    const byDay = new Map<string, DayPoint>();
    const byModel = new Map<string, ModelPoint>();
    // R9-R40 task 3: per-chain day aggregates feed the fee-lane trend chart
    // (same skip list as the merged totals, so both share one honest口径).
    const chainAgg = new Map<string, Map<string, DayPoint>>();
    for (const [name, svc] of Object.entries(services)) {
      if (String(name).toLowerCase() === "m365") continue;
      const s = svc as { days?: unknown; models?: unknown } | null;
      if (!s || typeof s !== "object") continue;
      const chainDays =
        chainAgg.get(String(name)) ?? new Map<string, DayPoint>();
      chainAgg.set(String(name), chainDays);
      for (const d of Array.isArray(s.days) ? s.days : []) {
        const p = d as {
          day?: unknown;
          requests?: unknown;
          costCny?: unknown;
          costCnyPartial?: unknown;
          credits?: unknown;
        };
        const day =
          typeof p?.day === "string" && /^\d{4}-\d{2}-\d{2}$/.test(p.day)
            ? p.day
            : "";
        if (!day) continue;
        const cur = byDay.get(day) ?? { day, requests: 0 };
        cur.requests += Number(p.requests) || 0;
        if (typeof p.costCny === "number" && Number.isFinite(p.costCny))
          cur.costCny = (cur.costCny ?? 0) + p.costCny;
        // 下界逐链传播：任一日行带 costCnyPartial（含 null+partial，对下界贡献 0）
        // 即整日合计为下界；缺失日不折 0，也不标 partial。
        if (p.costCnyPartial === true) cur.costCnyPartial = true;
        if (typeof p.credits === "number" && Number.isFinite(p.credits))
          cur.credits = (cur.credits ?? 0) + p.credits;
        byDay.set(day, cur);
        const ccur = chainDays.get(day) ?? { day, requests: 0 };
        ccur.requests += Number(p.requests) || 0;
        if (typeof p.costCny === "number" && Number.isFinite(p.costCny))
          ccur.costCny = (ccur.costCny ?? 0) + p.costCny;
        if (p.costCnyPartial === true) ccur.costCnyPartial = true;
        if (typeof p.credits === "number" && Number.isFinite(p.credits))
          ccur.credits = (ccur.credits ?? 0) + p.credits;
        chainDays.set(day, ccur);
      }
      for (const m of Array.isArray(s.models) ? s.models : []) {
        const p = m as {
          model?: unknown;
          requests?: unknown;
          input?: unknown;
          output?: unknown;
          unknown_usage?: unknown;
        };
        if (typeof p?.model !== "string" || !p.model) continue;
        const cur = byModel.get(p.model) ?? {
          model: p.model,
          requests: 0,
          tokens: 0,
          unknownRequests: 0,
          tokensMissing: false,
        };
        cur.requests += Number(p.requests) || 0;
        const io = [p.input, p.output];
        if (io.every((v) => typeof v === "number" && Number.isFinite(v)))
          cur.tokens += (p.input as number) + (p.output as number);
        else cur.tokensMissing = true;
        cur.unknownRequests += Number(p.unknown_usage) || 0;
        byModel.set(p.model, cur);
      }
    }
    const days = [...byDay.values()].sort((a, b) => a.day.localeCompare(b.day));
    const models = [...byModel.values()].sort(
      (a, b) => b.tokens - a.tokens || b.requests - a.requests
    );
    return { days, models };
  }
  /** A refresh failure after a successful read keeps the last REAL values
   *  with their capture time (stale); without prior real data it is an
   *  honest error. No fixture refill in any branch. */
  private enterSummaryError(message: string) {
    this.summaryError = message;
    if (this.summaryState === "live" && this.summaryLive)
      this.summaryState = "stale";
    else this.summaryState = "error";
  }
  private summaryNote() {
    const captured = this.summaryEpoch
      ? `；数据截至 ${new Date(this.summaryEpoch).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`
      : "";
    switch (this.summaryState) {
      case "loading":
        return "正在读取费用聚合…";
      case "unauthenticated":
        return "费用服务拒绝读取（接口 401）；显示空值，不以模拟数据回填。";
      case "forbidden":
        return "管理权限被拒（403）；当前会话无费用读取权。";
      case "error":
        return `读取失败：${this.summaryError}；显示为空值，不以模拟数据回填。`;
      case "stale":
        return `本次刷新失败：${this.summaryError}；以下为上一次真实读取${captured}。`;
      case "live":
        return "累计口径：全量账本聚合（qoder / chat2api / dsh；m365 已下线不计入）。";
    }
  }
  private kpi():
    | { requests: number; tokens: number; costCny: string; credits: number }
    | undefined {
    return this.summaryState === "live" || this.summaryState === "stale"
      ? this.summaryLive
      : undefined;
  }
  /** The ¥ first-screen reading as PRINTED (contract 3.8: 显示的合计 = 显示的
   *  分项之和). With the per-chain rows read, the reading is the sum of those
   *  SAME printed rows (each rounded to 3 decimals first) — the raw sum rounded
   *  once at the end is what made the KPI and its own table disagree by the
   *  rounding residue. Without the rows it falls back to the backend total, and
   *  with neither it is undefined (an honest em-dash, never a 0).
   *  partial 逐链传播（维护者 2026-09-28）：任一行 costCnyPartial=true 时读数只能是
   *  下界（≥¥…+「已知部分」），绝不冒充完整总额；null 行按后端语义折 0 进下界，
   *  但不带 partial 的 null 行让合计连下界都给不出（「未知」）。 */
  private displayedCost(): CostAmount | undefined {
    if (!this.chainFees) {
      if (!this.summaryLive) return undefined;
      const value = Number(this.summaryLive.costCny);
      if (this.summaryLive.costCnyUnknown) return { kind: "unknown" };
      return this.summaryLive.costCnyPartial
        ? { kind: "partial", value }
        : { kind: "full", value };
    }
    const billed = this.chainFees.filter((c) => c.costCny !== undefined);
    // 3.8 的「显示的合计 = 显示的分项之和」：任一行显示「未知」且不带下界
    // （costCny=null 且无 costCnyPartial）时，合计也必须是「未知」——把未知
    // 折成 0 再印一个确定的 ¥ 数字，正是本轮要修的那一类。
    if (billed.some((c) => c.costCny === null && c.costCnyPartial !== true))
      return { kind: "unknown" };
    const values = billed
      .filter((c) => typeof c.costCny === "number")
      .map((c) => c.costCny as number);
    const partial = billed.some((c) => c.costCnyPartial === true);
    if (!values.length) return partial ? { kind: "partial" } : undefined;
    const total = sumRounded3(values);
    return partial
      ? { kind: "partial", value: total }
      : { kind: "full", value: total };
  }
  /** 费用格的内容（第一阅读层）：读数元素只写值文本（零 = ¥0.000 一字不差，
   *  下界 = ≥¥…，未知 = 未知，无 ¥ 分量 = —），小字标记作为读数元素的兄弟
   *  紧邻其后，两者一起放进 `.wb-reading` 读数行（同排、基线对齐、8px 间距；
   *  窄容器按容器查询降档，见 workbench.css），标记因此不会落到读数的下一行。
   *  `data-wb-cost-state` 在格容器上，由调用方一并写出。 */
  private costCellHtml(tag: "b" | "div"): string {
    const amount = this.kpi() ? this.displayedCost() : undefined;
    const reading = costAmountReading(amount);
    const value =
      tag === "b"
        ? `<b data-kpi="cost">${reading}</b>`
        : `<div class="wb-large" data-kpi="cost">${reading}</div>`;
    return `<div class="wb-reading">${value}${costAmountMarker(amount)}</div>`;
  }
  /** 费用格的状态标记（data-wb-cost-state）：absent / full / partial / unknown。 */
  private costReadingState(): string {
    if (!this.kpi()) return "absent";
    return costAmountState(this.displayedCost());
  }
  /** Same rule for the 积分 reading, on the credit rows' own scale. */
  private displayedCredits(): string | undefined {
    if (!this.chainFees) {
      return this.summaryLive
        ? fmtCredits(this.summaryLive.credits)
        : undefined;
    }
    const credit = this.chainFees.filter((c) => c.credits !== undefined);
    return credit.length
      ? fmtCredits(sumRounded3(credit.map((c) => c.credits ?? 0)))
      : undefined;
  }

  // ---------------- overview charts (R9-R24, hand-drawn SVG, no deps)
  // Visual language per Tremor/shadcn restraint: hairline horizontal grid,
  // first/last x labels only, low-saturation brand accent, rounded bar tops,
  // tabular numbers. Every series comes from the real /api/summary ledger —
  // states without real data render their honest state line, never fixtures.

  /** A calendar window keeps FOUR states apart: a recorded zero, a day absent
   *  from the ledger, a day that IS recorded but whose component the backend
   *  marked unknown, and a day that IS recorded with costCnyPartial=true while
   *  its convertible part is EMPTY (costCny=null). None of the last three has a
   *  value to draw, so all break a trend line — but only the absent day is
   *  「那天没有记录」, and a null must never be written as 0 or as a numeric
   *  lower bound: `unknown[i]` and `boundOnly[i]` carry that distinction to the
   *  chart's hotspot label and to the resident note.
   *  维护者 2026-09-28（② 日级 null+partial 口径统一）：null+partial 是链级
   *  costAmount(null, true) 的日级同一载荷——`unknown`（分量未知）与 `boundOnly`
   *  （下界仅含可折算部分）互斥，常驻天数因此不把后者算进「分量未知」，读数
   *  与链行同写「未知（下界仅含可折算部分）」。
   *  `partial[i]` 是有值下界：该日有值、但后端标了 costCnyPartial（只含可折算
   *  记录）——值是下界，图上画空心点 / 虚线段，绝不冒充完整费用。 */
  private trendDays(
    field: "requests" | "costCny" | "credits",
    count = 14
  ): {
    labels: string[];
    values: (number | null)[];
    unknown: boolean[];
    partial: boolean[];
    boundOnly: boolean[];
  } {
    const recorded = new Map(
      (this.summarySeries?.days ?? []).map((d) => [d.day, d])
    );
    const labels: string[] = [];
    const values: (number | null)[] = [];
    const unknown: boolean[] = [];
    const partial: boolean[] = [];
    const boundOnly: boolean[] = [];
    const now = new Date();
    for (let i = count - 1; i >= 0; i--) {
      const dt = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
      const day = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
      const row = recorded.get(day);
      const value = row
        ? field === "requests"
          ? row.requests
          : field === "costCny"
            ? (row.costCny ?? null)
            : (row.credits ?? null)
        : null;
      labels.push(day);
      values.push(value);
      // 当天有账本记录、该分量没有可画的值、后端又标了 costCnyPartial = 下界
      // 仅含可折算部分（可折算为空）：与链级 costAmount(null, true) 同一句话，
      // 既没有值也没有数值下界，绝不折 0、绝不画下界数值。
      const boundOnlyDay =
        value === null &&
        row !== undefined &&
        field === "costCny" &&
        row.costCnyPartial === true;
      // 有记录但分量未知（不带 partial）= 分量未知，与上一态互斥。
      unknown.push(value === null && row !== undefined && !boundOnlyDay);
      boundOnly.push(boundOnlyDay);
      // 有值且该日只含可折算记录 = 有值下界（不是完整费用）。
      partial.push(
        value !== null && field === "costCny" && row?.costCnyPartial === true
      );
    }
    return { labels, values, unknown, partial, boundOnly };
  }
  /** 本期 vs 上期 over the real 14-day ledger window (contract 4.2/4.5):
   *  本期 = the last 7 days, 上期 = the 7 days before them. A day absent
   *  from the ledger is NOT a zero here (trendDays keeps it null) — it is left
   *  out of the sum and the counted days are printed, so a partial window is
   *  never passed off as a complete one; no comparable day at all yields
   *  undefined (honest 不可得) instead of a fabricated 0.
   *  任一侧含下界日（costCnyPartial）时该侧合计也是下界：`nowPartial` /
   *  `prevPartial` 把它带给环比，环比因此不再给无条件精确涨跌。 */
  private windowPair(
    field: "requests" | "costCny" | "credits"
  ):
    | {
        now: number;
        prev: number;
        nowDays: number;
        prevDays: number;
        nowPartial: boolean;
        prevPartial: boolean;
      }
    | undefined {
    const series = this.trendDays(field, 14);
    if (series.values.length < 14) return undefined;
    const slice = (values: (number | null)[], partial: boolean[]) => {
      const known = values
        .map((v, i) => (v === null ? -1 : i))
        .filter((i) => i >= 0);
      return {
        total: known.reduce((s, i) => s + values[i]!, 0),
        days: known.length,
        partial: known.some((i) => partial[i]),
      };
    };
    const now = slice(series.values.slice(7), series.partial.slice(7));
    const prev = slice(series.values.slice(0, 7), series.partial.slice(0, 7));
    if (!now.days || !prev.days) return undefined;
    return {
      now: now.total,
      prev: prev.total,
      nowDays: now.days,
      prevDays: prev.days,
      nowPartial: now.partial,
      prevPartial: prev.partial,
    };
  }
  /** Token 本期/上期 from the SAME bounded 14-day complete-token read the
   *  token trend chart draws: a day without a complete row is left out (never
   *  folded into 0) and the counted days are printed. With no read at all the
   *  caller says 不可得 — the window is never invented from another endpoint. */
  private tokenWindowPair():
    | { now: number; prev: number; nowDays: number; prevDays: number }
    | undefined {
    const read = this.usageTokens;
    if (!read?.payload) return undefined;
    const labels = this.beijingDayKeys(14);
    const inWindow = (date: string) =>
      date >= labels[0]! && date <= labels.at(-1)!;
    const byDay = new Map<string, number>();
    for (const row of read.payload.days) {
      if (!inWindow(row.date)) continue;
      const complete = this.completeTokens(row);
      if (complete === null) continue;
      byDay.set(row.date, (byDay.get(row.date) ?? 0) + complete);
    }
    const slice = (days: string[]) => {
      const known = days
        .map((d) => byDay.get(d))
        .filter((v): v is number => v !== undefined);
      return { total: known.reduce((s, v) => s + v, 0), days: known.length };
    };
    const now = slice(labels.slice(7));
    const prev = slice(labels.slice(0, 7));
    if (!now.days || !prev.days) return undefined;
    return {
      now: now.total,
      prev: prev.total,
      nowDays: now.days,
      prevDays: prev.days,
    };
  }
  /** `wb-delta` chip (contract 3.9): ▲/▼ N%, with the two real values always
   *  printed next to it in the state row. A zero 上期 has no percentage — the
   *  chip says so instead of printing ∞ or a made-up number, and a flat move
   *  prints ＝ 0.0% rather than an empty chip.
   *  维护者 2026-09-28（费用下界）：任一侧是下界时不再给无条件精确涨跌——
   *  只有下界仍能证明方向时才写「↑/↓ 至少」，方向不可证写「不可比」。
   *  Plan-Revision 9 ③（维护者 2026-09-28 逐页收纳 · 总览）：读数格的状态行
   *  最多两行，chip 因此只留方向与读数；「本期较上期」与不可比的**原因**进
   *  状态行自己的 `title`（`deltaReason`），首层不再把整句摊在读数下面。
   *  判据与语义一个字不改：无基数 / 不可比 / 至少 / ＝0.0% 四种口径都在。 */
  private deltaChip(pair: {
    now: number;
    prev: number;
    nowPartial?: boolean;
    prevPartial?: boolean;
  }): string {
    if (!pair.prev)
      return `<span class="wb-delta" data-wb-delta>环比无基数</span>`;
    const nowPartial = pair.nowPartial === true;
    const prevPartial = pair.prevPartial === true;
    if (nowPartial && prevPartial)
      return `<span class="wb-delta" data-wb-delta>环比不可比</span>`;
    if (nowPartial) {
      // 本期是下界、上期精确：只有下界已大于上期才能断言「至少上升」。
      return pair.now > pair.prev
        ? `<span class="wb-delta up" data-wb-delta>↑ 至少</span>`
        : `<span class="wb-delta" data-wb-delta>环比不可比</span>`;
    }
    if (prevPartial) {
      // 上期是下界、本期精确：只有本期已小于上期下界才能断言「至少下降」。
      return pair.now < pair.prev
        ? `<span class="wb-delta down" data-wb-delta>↓ 至少</span>`
        : `<span class="wb-delta" data-wb-delta>环比不可比</span>`;
    }
    const pct = ((pair.now - pair.prev) / pair.prev) * 100;
    if (!Number.isFinite(pct))
      return `<span class="wb-delta" data-wb-delta>环比不可得</span>`;
    if (Math.abs(pct) < 0.05)
      return `<span class="wb-delta" data-wb-delta>＝ 0.0%</span>`;
    return `<span class="wb-delta ${pct > 0 ? "up" : "down"}" data-wb-delta>${pct > 0 ? "▲" : "▼"} ${Math.abs(pct).toFixed(1)}%</span>`;
  }
  /** 时间区间 + 环比 reading for one first-screen KPI (contract 4.2/4.5: the
   *  first screen must answer 「用量发生了什么」, which a bare cumulative number
   *  cannot). The window is the real 14-day ledger window; an unreadable or
   *  too-thin window says so in words instead of printing a 0 or a fake
   *  percentage. `bound` 供费用行使用（维护者 2026-09-28 / OD 决定稿）：任一侧
   *  是下界时，整行两侧都写成 ≥¥…（整体标 partial），绝不出现不带 ≥ 的区间合计。 */
  private kpiWindowHtml(
    pair:
      | {
          now: number;
          prev: number;
          nowDays: number;
          prevDays: number;
          nowPartial?: boolean;
          prevPartial?: boolean;
        }
      | undefined,
    format: (v: number) => string,
    gapNote: string,
    bound?: (v: number, partial: boolean) => string
  ): string {
    // 维护者 2026-09-28（Plan-Revision 9 逐页收纳 · 总览）：读数格只留「标签 + 读数 +
    // 最多两行真实状态」。区间读数与环比是同一件事的两半（本期 / 上期 + 方向），
    // 原先它们是 `.wb-kpi` 网格里各自一行的两个直系子元素，于是同一格占两行——
    // 这里把两半收进同一个状态行容器（一行放不下才折行），格高因此由「标签 + 读数 +
    // 最多两行状态」决定（112–136px），而不是把口径长句堆在读数下面。读数本身一个字不改。
    // 口径（近 14 天 = 本期 7 天 + 上期 7 天 · 有记录日）与不可比的**原因**不再占首层行：
    // 完整句子进状态行自己的 `title`（同一事实仍可读到，只是不撑高卡片）。
    // 状态行用裸 <small>（不带 `.wb-muted`）：`.wb-kpis small` 已给 12.5px 与弱化色。
    if (!pair) {
      const why = `近 14 天 · ${gapNote}；环比不可得（不折成 0）。`;
      return `<div class="wb-kpi-state" title="${escapeHtml(why)}"><small>近 14 天 · ${escapeHtml(gapNote)}</small></div>`;
    }
    const anyPartial = pair.nowPartial === true || pair.prevPartial === true;
    const show = bound
      ? (v: number) => bound(v, anyPartial)
      : (v: number) => format(v);
    const dayNote =
      pair.nowDays < 7 || pair.prevDays < 7
        ? ` · 有记录日 本期 ${pair.nowDays}/7、上期 ${pair.prevDays}/7`
        : "";
    const title = `近 14 天（本期 7 天 / 上期 7 天${dayNote}）· 本期 ${show(pair.now)} · 上期 ${show(pair.prev)}${this.deltaReason(pair)}`;
    // 首层状态行只留「本期 X · 上期 Y」+ 环比 chip：长金额夹具下（≥¥1,234,567.836）
    // 再加「近 14 天 · 」前缀会把状态行撑到 3 行，读数卡高 143.44px > 136px
    // （门禁 ③ 实测，1600x900 双主题 partial 态）。窗口口径与原因都在 title 里，一个字不少。
    return `<div class="wb-kpi-state" title="${escapeHtml(title)}"><small>本期 ${escapeHtml(show(pair.now))} · 上期 ${escapeHtml(show(pair.prev))}</small>${this.deltaChip(pair)}</div>`;
  }
  /** 环比不可比的**原因句**（不带包装）：只在状态行的 `title` 里出现，
   *  首层状态行只留「环比不可比」这几个字——原因仍可读到，只是不撑高卡片。 */
  private deltaReason(pair: {
    now: number;
    prev: number;
    nowPartial?: boolean;
    prevPartial?: boolean;
  }): string {
    if (!pair.prev) return "；环比无基数（上期 0）。";
    const nowPartial = pair.nowPartial === true;
    const prevPartial = pair.prevPartial === true;
    if (nowPartial && prevPartial)
      return "；环比不可比（本期与上期均为下界）。";
    if (nowPartial)
      return pair.now > pair.prev
        ? "；环比至少上升（本期为下界，上期精确）。"
        : "；环比不可比（本期为下界，方向不可证）。";
    if (prevPartial)
      return pair.now < pair.prev
        ? "；环比至少下降（上期为下界，本期精确）。"
        : "；环比不可比（上期为下界，方向不可证）。";
    return "";
  }
  /** Mini bar spark for a KPI card (last 7 days); a flat all-zero week still
   *  draws (a real quiet week), only absent series stay empty. */

  // ---------------- A12-3 usage reads: 14-day complete tokens + 24h rates
  /** One row's COMPLETE token total (input+output), or null when the row has
   *  no such request at all (not complete / a component is absent). F1: the
   *  backend's input_tokens/output_tokens already sum ONLY the requests that
   *  carry both components, so this predicate must NOT also demand
   *  `row.complete` — a bucket with one partial request keeps its other,
   *  complete requests inside the complete total. The missing component is NEVER
   *  treated as 0. */
  private completeTokens(row: UsageTokenRow): number | null {
    if (
      typeof row.input_tokens !== "number" ||
      typeof row.output_tokens !== "number"
    )
      return null;
    if (
      !Number.isFinite(row.input_tokens) ||
      !Number.isFinite(row.output_tokens)
    )
      return null;
    return row.input_tokens + row.output_tokens;
  }
  /** F1: the row's partial-request counts. An absent field (older payload) is
   *  unknown — a missing contract field is never read as 0 records. */
  private partialRequests(row: UsageTokenRow): number | null {
    return typeof row.partial_requests === "number" &&
      Number.isFinite(row.partial_requests)
      ? row.partial_requests
      : null;
  }
  /** F1: the row's visible partial output component (null = not visible at
   *  all). Used by 仅输出可见 — it is never folded into a complete total. */
  private partialOutputTokens(row: UsageTokenRow): number | null {
    return typeof row.partial_output_tokens === "number" &&
      Number.isFinite(row.partial_output_tokens)
      ? row.partial_output_tokens
      : null;
  }
  /** Beijing (UTC+8) natural-day keys for a window ending today: both usage
   *  endpoints declare Asia/Shanghai, so the axis is built from that fixed
   *  offset instead of the viewer's local clock (a browser in another zone
   *  must still bucket by the same natural days the server did). */
  private beijingDayKeys(count: number): string[] {
    const bj = Date.now() + 8 * 3600 * 1000;
    const keys: string[] = [];
    for (let i = count - 1; i >= 0; i--)
      keys.push(new Date(bj - i * 86400 * 1000).toISOString().slice(0, 10));
    return keys;
  }
  /** Beijing (UTC+8) hour keys for a window ending in the current hour: the
   *  rate endpoint buckets by `YYYY-MM-DDTHH:00:00+08:00` (console_stats.py
   *  `_hour_label`), so the F4a chart axis is built from that fixed offset and
   *  a viewer in another zone still sees the server's own hours. The `slice(0,13)`
   *  prefix of a bucket's `hour` is the same key. */
  private beijingHourKeys(count: number): string[] {
    const bj = Date.now() + 8 * 3600 * 1000;
    const keys: string[] = [];
    for (let i = count - 1; i >= 0; i--)
      keys.push(new Date(bj - i * 3600 * 1000).toISOString().slice(0, 13));
    return keys;
  }
  /** Series label for one source+model identity: the model name alone while it
   *  is unambiguous, with the source appended when two sources share the name
   *  (identity stays source+model — D12-3 — even when the label shortens). */
  private tokenSeriesLabel(
    source: string,
    model: string,
    ambiguous: boolean
  ): string {
    return ambiguous ? `${model}（${this.sourceLabel(source)}）` : model;
  }
  /** Multi-series area shared by the 14-day token trend and the F4a hourly
   *  rate trend: the same hand-drawn recipe as areaSvg (hairline grid,
   *  first/last x labels, per-point focusable hit targets) extended to N
   *  series. The total series keeps the full accent stroke; model series step
   *  down in opacity and carry a dash, and each one is named in the tooltip so
   *  a line is never anonymous. `opts` only relabels the axis, the aria
   *  description and the value format — the drawing stays one implementation. */
  // ---------------- chart tip measurement + shared axis (contract 3.7)
  /** The tip's two text tiers AND its per-string metrics, read from a throwaway
   *  SVG probe inside the workbench: the SAME element kind, the SAME class and
   *  the SAME text the rendered tip holds, so the numbers come from the
   *  browser's own layout of this tip rather than from a second measurement
   *  engine that can disagree with it.
   *
   *  Two rounds of canvas 2D measurement were not enough. `ctx.font` can
   *  express family/size/weight but NOT `font-variant-numeric`, while
   *  `.wb-area-tip text` declares `tabular-nums` — so every digit was measured
   *  at its PROPORTIONAL advance while the tip rendered TABULAR figures, and the
   *  plate came out short of its own widest live row (¥1,100.322, a string made
   *  almost entirely of digits) even after a hand-tuned allowance: the residual
   *  did not shrink with the allowance because the disagreement was in the glyph
   *  advances, not in a constant. `getComputedTextLength()` returns the advance
   *  the SVG renderer itself uses for that exact string (font-variant, kerning
   *  and the element's own CSS included), and `getBBox()` returns the INK box,
   *  whose right edge is what a rect must clear. Both are read on this probe.
   *
   *  The probe is appended, measured and removed in one synchronous step, so it
   *  never joins the rendered chart; it is laid out (not `display:none`, which
   *  would measure 0) and hidden off-canvas instead. A 260-unit line cap keeps
   *  a pathological name from covering the chart, and an ellipsised line still
   *  carries its full text in the hotspot's aria-label. */
  private tipProbe?: {
    host: HTMLElement;
    label: SVGTextElement;
    value: SVGTextElement;
  };
  private tipFont?: { label: TipFont; value: TipFont };
  private tipElements():
    | { host: HTMLElement; label: SVGTextElement; value: SVGTextElement }
    | undefined {
    if (this.tipProbe) return this.tipProbe;
    const host = document.createElement("div");
    host.setAttribute("aria-hidden", "true");
    host.style.cssText =
      "position:absolute;left:-9999px;top:0;width:320px;height:80px;overflow:hidden;pointer-events:none";
    host.innerHTML = `<svg width="320" height="80" aria-hidden="true"><g class="wb-area-tip"><text>0</text><text class="wb-area-tip-value">0</text></g></svg>`;
    this.root.appendChild(host);
    const label = host.querySelector<SVGTextElement>(
      ".wb-area-tip text:not(.wb-area-tip-value)"
    );
    const value = host.querySelector<SVGTextElement>(
      ".wb-area-tip .wb-area-tip-value"
    );
    // No probe element (no tip rule in this build) leaves the caller on its
    // honest estimate — never on a hard-coded plate width that would clip.
    if (!label || !value) {
      host.remove();
      return undefined;
    }
    this.tipProbe = { host, label, value };
    return this.tipProbe;
  }
  private tipFonts(): { label: TipFont; value: TipFont } {
    if (this.tipFont) return this.tipFont;
    const probe = this.tipElements();
    const read = (element: SVGTextElement | undefined): TipFont => {
      const style = element ? getComputedStyle(element) : null;
      // No resolved style (no tip rule in this build) falls back to the
      // contract's own two tiers rather than to a number that clips.
      return {
        size: Number.parseFloat(style?.fontSize ?? "") || 12,
        weight: Number.parseInt(style?.fontWeight ?? "", 10) || 400,
        family: style?.fontFamily || "system-ui, sans-serif",
      };
    };
    const fonts = {
      label: read(probe?.label),
      value: read(probe?.value),
    };
    this.tipFont = fonts;
    return fonts;
  }
  /** One string's rendered advance width AND ink right edge, measured on the
   *  tip's own <text> element. `inkRight` is max(ink box right edge, advance):
   *  an italic/overhanging glyph can ink past its advance, and the plate is a
   *  rect that must clear the INK, not the advance. Without the probe (no DOM
   *  layout in this build) the estimate is deliberately GENEROUS — 0.72em per
   *  character, above any digit advance — so a missing measurement widens the
   *  plate instead of clipping the text. */
  private measureTip(
    text: string,
    tier: "label" | "value"
  ): { advance: number; inkRight: number } {
    const probe = this.tipElements();
    const element = tier === "value" ? probe?.value : probe?.label;
    const fallback = text.length * this.tipFonts()[tier].size * 0.72;
    if (!element) return { advance: fallback, inkRight: fallback };
    element.textContent = text;
    try {
      const box = element.getBBox();
      const advance = element.getComputedTextLength();
      return { advance, inkRight: Math.max(box.x + box.width, advance) };
    } catch {
      // Detached/unrendered probe (a browser that refuses getBBox here):
      // same generous estimate, never a silent zero-width line.
      return { advance: fallback, inkRight: fallback };
    }
  }
  /** Ellipsise to the measured measure — a cut with NO marker is what the old
   *  fixed 14-character slice did, and the full name still travels in the
   *  hotspot's aria-label. */
  private fitText(
    text: string,
    maxPx: number,
    tier: "label" | "value"
  ): string {
    if (this.measureTip(text, tier).advance <= maxPx) return text;
    let lo = 0,
      hi = text.length;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      if (this.measureTip(`${text.slice(0, mid)}…`, tier).advance <= maxPx)
        lo = mid;
      else hi = mid - 1;
    }
    return lo > 0 ? `${text.slice(0, lo)}…` : "…";
  }
  /** One tooltip plate from the lines it actually holds. The plate is a rect
   *  drawn in the SAME user units as its text, so it is sized from the text's
   *  measured INK right edge at the tip's own rendered type — the widest line's
   *  ink plus the same `padX` on the right, rounded up to whole units. That
   *  leaves every line at least `padX` of clearance to the plate's edge and
   *  removes the per-line "advance allowance" guess two rounds of measurement
   *  were tuned with; no line can reach the edge, so no truncation and no
   *  overflow. The 260-unit cap keeps a pathological name from covering the
   *  chart, and an ellipsised line still carries its full text in the hotspot's
   *  aria-label. */
  private areaTip(lines: { text: string; value: boolean; color?: string }[]): {
    plate: string;
    width: number;
  } {
    const lineHeight = 15,
      padX = 8,
      maxLine = 260;
    const dotPad = lines.some((line) => line.color) ? 10 : 0;
    const tier = (index: number): "label" | "value" =>
      lines[index]!.value ? "value" : "label";
    const rendered = lines.map((line, index) =>
      this.fitText(line.text, maxLine, tier(index))
    );
    const measured = rendered.map((text, index) =>
      this.measureTip(text, tier(index))
    );
    const widestInk = Math.max(...measured.map((m) => m.inkRight), 0);
    const plateWidth = Math.ceil(padX + dotPad + widestInk) + padX;
    const plateHeight = 12 + lineHeight * lines.length;
    const plate =
      `<rect width="${plateWidth}" height="${plateHeight}" rx="8"/>` +
      rendered
        .map((text, index) => {
          const y = 12 + index * lineHeight;
          const color = lines[index]!.color;
          const dot = color
            ? `<circle cx="${padX + 2}" cy="${y - 4}" r="2.5" style="fill:${color}"/>`
            : "";
          return `${dot}<text x="${padX + dotPad}" y="${y}"${lines[index]!.value ? ` class="wb-area-tip-value"` : ""}>${escapeHtml(text)}</text>`;
        })
        .join("");
    return { plate, width: plateWidth };
  }
  /** Four dashed gridlines; F12-3: the three above the baseline carry their
   *  value (the unit is in the chart's reading row), so the plot has a scale.
   *  The lines go under the data and the labels over it (callers paint
   *  `labels` after the area fills, before the hotspots), so a fill never
   *  sits on top of a label and lowers its contrast (gate G2). */
  private gridLines(
    max: number,
    padX: number,
    width: number,
    y: (v: number) => number,
    label?: (v: number) => string
  ): { lines: string; labels: string } {
    const steps = [0, 1, 2, 3].map((step) => ({
      step,
      v: max * (1 - step / 3),
    }));
    return {
      lines: steps
        .map(
          ({ v }) =>
            `<line x1="${padX}" y1="${y(v).toFixed(1)}" x2="${width - padX}" y2="${y(v).toFixed(1)}" class="wb-gridline"/>`
        )
        .join(""),
      labels: label
        ? steps
            .filter(({ step }) => step < 3)
            .map(
              ({ v }) =>
                `<text x="${padX + 2}" y="${(y(v) - 4).toFixed(1)}" class="wb-gridlabel">${escapeHtml(label(v))}</text>`
            )
            .join("")
        : "",
    };
  }
  /** Every sample keeps its tick; dated hours use two lines to remain readable.
   *  Each label is centred on its sample and pushed inward only as far as the
   *  plot edge needs (2026-10-31): a start / end anchor on the first and last
   *  labels left those two pairs ~2px apart while the rest sit ~20px apart. */
  private axisTicks(
    labels: string[],
    axis: (label: string) => string,
    x: (i: number) => number,
    h: number,
    width: number
  ): string {
    return labels
      .map((label, i) => {
        const lines = axisLabelLines(axis(label));
        // Half the label's width on timeAxisWidth's own estimate (7 units a character).
        const half = Math.max(...lines.map((line) => line.length)) * 3.5;
        const cx = Math.min(Math.max(x(i), half + 1), width - half - 1).toFixed(
          1
        );
        return `<text x="${cx}" y="${h - (lines.length > 1 ? 19 : 5)}" class="wb-gridlabel" text-anchor="middle">${lines.map((line, j) => `<tspan x="${cx}" dy="${j ? 14 : 0}">${escapeHtml(line)}</tspan>`).join("")}</text>`;
      })
      .join("");
  }
  /** F12-3: a trend chart's readings sit in its title area, above the plot -
   *  never painted onto the data: the interval total (when a sum is a reading),
   *  the last value with its date, the mean drawn as the dashed baseline (or the
   *  caller's weighted mean) and the unit. Lower-bound days keep the ≥ wording,
   *  so a partial total or mean never reads as complete. */
  private chartStatsHtml(o: {
    values: (number | null)[];
    labels: string[];
    axis: (label: string) => string;
    format: (v: number) => string;
    unit: string;
    totalLabel: string;
    partialValues: boolean[];
    scope: string;
    mean?: { label: string; value: number | null };
  }): string {
    const known = o.values.flatMap((v, i) => (v === null ? [] : [{ v, i }]));
    if (!known.length) return "";
    const anyPartial = known.some((p) => o.partialValues[p.i] === true);
    const last = known.at(-1)!;
    const lastPartial = o.partialValues[last.i] === true;
    const total = known.reduce((s, p) => s + p.v, 0);
    const mean = o.mean ?? {
      label: `均值 · ${known.length} 个有记录${o.scope}${anyPartial ? "（下界）" : ""}`,
      value: total / known.length,
    };
    const item = (label: string, value: string, swatch = "") =>
      `<span class="wb-chart-stat"><small>${swatch}${escapeHtml(label)}</small><b>${escapeHtml(value)}</b></span>`;
    const swatch = `<svg width="18" height="8" aria-hidden="true"><line x1="0" y1="4" x2="18" y2="4" class="wb-trend-ref"/></svg>`;
    return (
      `<div class="wb-chart-stats">` +
      (o.totalLabel
        ? item(
            anyPartial ? `${o.totalLabel}（下界）` : o.totalLabel,
            `${anyPartial ? "≥" : ""}${o.format(total)}`
          )
        : "") +
      item(
        `末值 · ${o.axis(o.labels[last.i] ?? "")}`,
        `${lastPartial ? "≥" : ""}${o.format(last.v)}`
      ) +
      (mean.value !== null
        ? item(
            mean.label,
            `${anyPartial && !o.mean ? "≥" : ""}${o.format(mean.value)}`,
            swatch
          )
        : "") +
      `<span class="wb-chart-unit">单位：${escapeHtml(o.unit)}</span></div>`
    );
  }
  /** Contract 3.7 `wb-trend-ref`: the interval mean (missing days left OUT —
   *  a day absent from the ledger is not a 0), drawn heavier and darker than
   *  the grid. The caller lists it in the chart's legend under the same class
   *  name, so the reference can never be mistaken for the data line.
   *  区间含下界日时均值同样只是下界（每个下界日的真实值 ≥ 画出的值）——
   *  `partial` 随图例一起写出，不把下界均值印成完整均值。 */
  private trendRef(
    values: (number | null)[],
    x: (i: number) => number,
    y: (v: number) => number,
    padX: number,
    width: number,
    partialValues: boolean[] = [],
    fixedMean?: number | null
  ): {
    line: string;
    mean: number | undefined;
    days: number;
    partial: boolean;
  } {
    const known = values.filter((v): v is number => v !== null);
    if (!known.length || fixedMean === null)
      return { line: "", mean: undefined, days: 0, partial: false };
    const mean = fixedMean ?? known.reduce((s, v) => s + v, 0) / known.length;
    const gy = y(mean).toFixed(1);
    const partial = values.some(
      (v, i) => v !== null && partialValues[i] === true
    );
    return {
      line: `<line x1="${padX}" y1="${gy}" x2="${width - padX}" y2="${gy}" class="wb-trend-ref"/>`,
      mean,
      days: known.length,
      partial,
    };
  }
  /** Keep each drawn key's colour slot; free the slots of keys no longer drawn and
   *  give newcomers the lowest free slot (1-4). Identity, never rank. */
  private seriesSlots(
    map: Map<string, number>,
    keys: string[]
  ): Map<string, number> {
    for (const key of [...map.keys()]) if (!keys.includes(key)) map.delete(key);
    const used = new Set(map.values());
    for (const key of keys) {
      if (map.has(key)) continue;
      let slot = 1;
      while (used.has(slot)) slot += 1;
      map.set(key, slot);
      used.add(slot);
    }
    return map;
  }
  /** Monotone cubic (Fritsch–Carlson): the curve passes through every measured
   *  point and never overshoots a local extreme, so smoothing cannot invent a peak
   *  or a dip the ledger did not record. A one-point segment is drawn as a dot by
   *  the caller, not as an invisible zero-length path. */
  private monotonePath(points: { x: number; y: number }[]): string {
    if (points.length < 2) return "";
    const dx: number[] = [],
      slope: number[] = [];
    for (let i = 0; i < points.length - 1; i++) {
      dx[i] = points[i + 1].x - points[i].x;
      slope[i] = dx[i] === 0 ? 0 : (points[i + 1].y - points[i].y) / dx[i];
    }
    const tangent = new Array<number>(points.length).fill(0);
    tangent[0] = slope[0];
    for (let i = 1; i < points.length - 1; i++) {
      if (slope[i - 1] * slope[i] <= 0) {
        tangent[i] = 0;
        continue;
      }
      const w1 = 2 * dx[i] + dx[i - 1];
      const w2 = dx[i] + 2 * dx[i - 1];
      tangent[i] = (w1 + w2) / (w1 / slope[i - 1] + w2 / slope[i]);
    }
    tangent[points.length - 1] = slope[points.length - 2];
    let d = `M${points[0].x.toFixed(1)},${points[0].y.toFixed(1)}`;
    for (let i = 0; i < points.length - 1; i++) {
      const x1 = points[i].x + dx[i] / 3;
      const y1 = points[i].y + (tangent[i] * dx[i]) / 3;
      const x2 = points[i + 1].x - dx[i] / 3;
      const y2 = points[i + 1].y - (tangent[i + 1] * dx[i]) / 3;
      d += ` C${x1.toFixed(1)},${y1.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)} ${points[i + 1].x.toFixed(1)},${points[i + 1].y.toFixed(1)}`;
    }
    return d;
  }
  private tokenSeriesSvg(
    labels: string[],
    series: {
      key: string;
      label: string;
      values: (number | null)[];
      kind: "total" | "model";
      slot?: number;
      partial?: boolean[];
      missing?: (string | null)[];
    }[],
    opts: {
      aria: string;
      axis?: (label: string) => string;
      format?: (value: number) => string;
      totalLabel?: string;
      missingLabel?: (index: number) => string;
      partialValues?: boolean[];
      unit: string;
      scope: string;
      mean?: { label: string; value: number | null };
      tick?: (value: number) => string;
      tipTime?: (label: string) => string;
      width?: number;
    }
  ): string {
    const n = labels.length;
    // Reserve reading space for every time sample; narrow cards scroll locally.
    // The plot is drawn at the caller's width (the scale it is meant to be read
    // at) and scrolls only below what its axis needs or 85% of that scale, so a
    // card a little narrower than the width shrinks the plot slightly (its type
    // stays legible) instead of scrolling it.
    const h = 260,
      padT = 24,
      padB = 48,
      padX = 14;
    const axis =
      opts.axis ?? ((label: string) => label.slice(5).replace("-", "/"));
    const width = timeAxisWidth(labels.map(axis), opts.width ?? 1000);
    const minWidth = Math.max(
      timeAxisWidth(labels.map(axis), 0),
      Math.round(width * 0.85)
    );
    const format = opts.format ?? ((value: number) => fmtTokens(value));
    const known = series.flatMap((s) =>
      s.values.filter((v): v is number => v !== null)
    );
    const max = Math.max(...known, 1);
    const x = (i: number) =>
      padX + (i / Math.max(n - 1, 1)) * (width - padX * 2);
    const y = (v: number) => padT + (1 - v / max) * (h - padT - padB);
    // F12-3: each series has its own validated colour (the total line plus the
    // model's fixed slot), named in the legend - no longer one hue told apart by
    // opacity alone.
    const stroke = (s: { kind: "total" | "model"; slot?: number }) =>
      s.kind === "total"
        ? "var(--wb-chart-total)"
        : `var(--wb-cat-${s.slot ?? 1})`;
    const segments = (s: { values: (number | null)[] }) => {
      const segs: number[][] = [];
      s.values.forEach((v, i) => {
        if (v === null) return;
        if (i === 0 || s.values[i - 1] === null) segs.push([]);
        segs.at(-1)!.push(i);
      });
      return segs;
    };
    const total = series.find((s) => s.kind === "total");
    const totalValues = total?.values ?? [];
    const fillId = `wb-total-fill-${++this.chartFillSeq}`;
    const totalSegments = total ? segments(total) : [];
    const area = total
      ? totalSegments
          .filter((indices) => indices.length > 1)
          .map((indices) => {
            const first = indices[0],
              last = indices.at(-1)!;
            return `<path d="${this.monotonePath(indices.map((i) => ({ x: x(i), y: y(totalValues[i]!) })))} L${x(last).toFixed(1)},${(h - padB).toFixed(1)} L${x(first).toFixed(1)},${(h - padB).toFixed(1)} Z" fill="url(#${fillId})" stroke="none"/>`;
          })
          .join("")
      : "";
    const defs = total
      ? `<defs><linearGradient id="${fillId}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" style="stop-color:var(--wb-chart-total);stop-opacity:.20"/><stop offset="100%" style="stop-color:var(--wb-chart-total);stop-opacity:0"/></linearGradient></defs>`
      : "";
    // Model lines stay thin and translucent; the total is drawn last, over them,
    // so the weighted reading is never hidden by a model that happens to match it.
    const modelLines = series
      .filter((s) => s.kind !== "total")
      .map((s) =>
        segments(s)
          .map((indices) => {
            if (indices.length === 1) {
              const i = indices[0];
              return `<circle cx="${x(i).toFixed(1)}" cy="${y(s.values[i]!).toFixed(1)}" r="2.6" class="wb-area-single" style="fill:${stroke(s)}"/>`;
            }
            return `<path d="${this.monotonePath(indices.map((i) => ({ x: x(i), y: y(s.values[i]!) })))}" class="wb-area-line" fill="none" style="stroke:${stroke(s)};stroke-width:1.8;stroke-opacity:.72"/>`;
          })
          .join("")
      )
      .join("");
    const totalLine = total
      ? totalSegments
          .map((indices) => {
            if (indices.length === 1) {
              const i = indices[0];
              return `<circle cx="${x(i).toFixed(1)}" cy="${y(totalValues[i]!).toFixed(1)}" r="2.8" class="wb-area-single" style="fill:var(--wb-chart-total)"/>`;
            }
            return `<path d="${this.monotonePath(indices.map((i) => ({ x: x(i), y: y(totalValues[i]!) })))}" class="wb-area-line" fill="none" style="stroke:var(--wb-chart-total);stroke-width:2.8"/>`;
          })
          .join("")
      : "";
    const totalDots = total
      ? totalValues
          .map((v, i) =>
            v === null
              ? ""
              : `<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="2.2" style="fill:var(--wb-chart-total);stroke:var(--theme-panel);stroke-width:1.4"/>`
          )
          .join("")
      : "";
    const markers = series
      .map((s, slot) =>
        s.values
          .map((v, i) => {
            const kind = chartBaselineKind(v);
            if (!kind) return "";
            const cx = x(i) + Math.min(slot, 4) * 2;
            return `<line class="wb-chart-baseline-${kind}" x1="${cx.toFixed(1)}" x2="${cx.toFixed(1)}" y1="${h - padB - 8}" y2="${h - padB}" style="stroke:${kind === "zero" ? stroke(s) : "var(--theme-muted)"}" aria-hidden="true"/>`;
          })
          .join("")
      )
      .join("");
    const hotWidth = (width - padX * 2) / Math.max(n - 1, 1);
    const tipTime = opts.tipTime ?? ((label: string) => label);
    const hotspots = labels
      .map((label, i) => {
        const cx = x(i);
        // The tooltip opens with the point's time, then one line per series.
        const time = tipTime(label);
        const lines = series.map((s) => {
          const v = s.values[i];
          // 总计线的空日不是「无记录」一种：缺完整分量（当日有账本记录）与
          // 无账本行是两种状态，各自写清；`missingLabel` 只作用于总计线，
          // 单条序列可以用 `missing` 为自己的空日写同样诚实的说明。
          const missing =
            s.kind === "total" && opts.missingLabel
              ? opts.missingLabel(i)
              : (s.missing?.[i] ?? "无记录");
          // A day whose value is only a lower bound says so on every line it is
          // read from (tooltip and accessible name), not only in the stats row.
          const lower =
            (s.kind === "total" ? opts.partialValues?.[i] : s.partial?.[i]) ===
            true;
          return {
            text: `${s.label}：${v === null || v === undefined ? missing : lower ? `≥${format(v)}（下界）` : format(v)}`,
            value: false,
            color: stroke(s),
          };
        });
        const tip = this.areaTip([{ text: time, value: false }, ...lines]);
        const tipX = Math.min(
          Math.max(cx - tip.width / 2, 2),
          Math.max(2, width - tip.width - 2)
        );
        return `<g class="wb-area-hotspot" tabindex="0" role="img" aria-label="${escapeHtml(time)}：${escapeHtml(lines.map((line) => line.text).join("，"))}">
        <rect x="${(cx - hotWidth / 2).toFixed(1)}" y="0" width="${hotWidth.toFixed(1)}" height="${h - padB}" class="wb-area-hit"/>
        <line x1="${cx.toFixed(1)}" x2="${cx.toFixed(1)}" y1="${padT}" y2="${h - padB}" class="wb-area-crosshair"/>
        <g class="wb-area-tip" transform="translate(${tipX.toFixed(1)},1)">${tip.plate}</g>
      </g>`;
      })
      .join("");
    // The total series carries the interval reference baseline and the readings
    // above the plot; the per-model series stay on the tooltip, the legend and the
    // keyboard.
    const ref = total
      ? this.trendRef(
          totalValues,
          x,
          y,
          padX,
          width,
          opts.partialValues ?? [],
          opts.mean ? opts.mean.value : undefined
        )
      : { line: "", mean: undefined, days: 0, partial: false };
    const stats = total
      ? this.chartStatsHtml({
          values: totalValues,
          labels,
          axis,
          format,
          unit: opts.unit,
          totalLabel: opts.totalLabel ?? "区间合计",
          partialValues: opts.partialValues ?? [],
          scope: opts.scope,
          mean: opts.mean,
        })
      : "";
    const lastValue = (s: { values: (number | null)[] }) => {
      for (let i = s.values.length - 1; i >= 0; i--)
        if (s.values[i] !== null) return s.values[i]!;
      return null;
    };
    const legend =
      series.length >= 2
        ? `<ul class="wb-chart-legend" aria-label="图例">${series
            .map((s) => {
              const last = lastValue(s);
              return `<li><i style="--wb-swatch:${stroke(s)}" aria-hidden="true"></i>${escapeHtml(s.label)}<small>${escapeHtml(last === null ? "无记录" : format(last))}</small></li>`;
            })
            .join("")}</ul>`
        : "";
    const grid = this.gridLines(
      max,
      padX,
      width,
      y,
      known.length ? (opts.tick ?? format) : undefined
    );
    return `${stats}${legend}<div class="wb-chart-scroll"><svg class="wb-area" style="min-width:${minWidth}px" viewBox="0 0 ${width} ${h}" role="group" aria-label="${escapeHtml(opts.aria)}">
      ${defs}
      ${grid.lines}
      ${area}
      ${modelLines}
      ${totalLine}${totalDots}
      ${markers}${ref.line}${grid.labels}
      ${hotspots}
      ${this.axisTicks(labels, axis, x, h, width)}
    </svg></div><p class="wb-chart-marker-note">底部实线短标：已统计为 0；虚线短标：无记录或统计未知（不折算为 0）。</p>`;
  }
  /** A12-3 main trend: per-day COMPLETE token totals over the bounded
   *  14-day window — one total series plus the source+model series the user
   *  unfolded. F1: the total takes only the requests that carry BOTH
   *  components (the backend's input_tokens/output_tokens already sum exactly
   *  those), so a partial request in one bucket no longer pushes its complete
   *  neighbours out of the total; the partial requests are counted and listed
   *  separately (partial_output_tokens → 仅输出可见). A day with no complete
   *  row breaks the line and is reported separately from a recorded zero
   *  (零记录 ≠ 无记录). Buckets whose timestamp is invalid (date=null) are
   *  kept and listed as 时间无法判定 (never dropped by the window filter).
   *  The server's per-source coverage block is rendered verbatim (数据覆盖),
   *  so an empty window can be told apart from a collection chain that is not
   *  running. Model series stay folded by default and the number of series that
   *  stay folded is always listed (folding never loses rows). */
  /** A14-4 / T2 composition: the 14-day COMPLETE Token split by gateway source,
   *  as a ring. The amounts are the reading (centre + legend); a share appears
   *  only in each segment's tooltip and names its denominator (complete records
   *  of the window). Requests missing a component never enter it, and when the
   *  window has any, the card says how many were left out. Colour follows the
   *  source (SOURCE_SLOTS), never its rank. */
  private emptyTrendHtml(unit: string, labels: string[]): string {
    return this.tokenSeriesSvg(
      labels,
      [
        {
          key: "unknown",
          label: "统计未提供",
          values: labels.map(() => null),
          kind: "total",
        },
      ],
      {
        aria: `${unit} 暂无统计，底部标记为未知`,
        unit,
        scope: unit === "Token/s" ? "小时" : "日",
        axis: (label) => label.slice(5).replace("T", " "),
        missingLabel: () => "统计未提供",
      }
    );
  }
  private emptyCompositionHtml(note: string): string {
    return `<div class="wb-ring"><svg class="wb-ring-svg" viewBox="0 0 150 150" role="img" aria-label="${escapeHtml(note)}"><circle cx="75" cy="75" r="58" fill="none" stroke="var(--theme-muted)" stroke-width="10" opacity="0.25"/><line x1="75" x2="75" y1="126" y2="140" stroke="var(--theme-muted)" stroke-width="2"/><text x="75" y="80" text-anchor="middle" class="wb-ring-sub">${note === "完整 Token 为 0" ? "0 Token" : "暂无统计"}</text></svg><p class="wb-muted">${escapeHtml(note)}；无可计算构成，不显示占比。</p></div>`;
  }
  private tokenCompositionHtml(): string {
    const read = this.usageTokens;
    if (!read?.payload)
      return this.emptyCompositionHtml(
        this.usageReadNote(read, "近 14 天完整 Token")
      );
    const labels = this.beijingDayKeys(14);
    const inWindow = (d: string | null) =>
      d !== null && d >= labels[0]! && d <= labels.at(-1)!;
    const bySource = new Map<string, number>();
    let partialRequests = 0,
      partialUnknown = false;
    for (const row of read.payload.days) {
      if (!inWindow(row.date)) continue;
      const complete = this.completeTokens(row);
      if (complete !== null)
        bySource.set(row.source, (bySource.get(row.source) ?? 0) + complete);
      const partial = this.partialRequests(row);
      if (partial === null) partialUnknown = true;
      else partialRequests += partial;
    }
    const outputOnly = read.payload.missing.filter(
      (m) => m.date === null || inWindow(m.date)
    ).length;
    const order = (src: string) => SOURCE_SLOTS[src] ?? 9;
    const parts = [...bySource.entries()]
      .filter(([, v]) => v > 0)
      .sort((a, b) => order(a[0]) - order(b[0]) || a[0].localeCompare(b[0]));
    const total = parts.reduce((sum, [, v]) => sum + v, 0);
    if (!total)
      return this.emptyCompositionHtml(
        bySource.size ? "完整 Token 为 0" : "暂无完整统计"
      );
    const color = (src: string) =>
      SOURCE_SLOTS[src]
        ? `var(--wb-cat-${SOURCE_SLOTS[src]})`
        : "var(--theme-muted)";
    const incomplete = partialUnknown || partialRequests > 0 || outputOnly > 0;
    const size = 150,
      c = size / 2,
      r = 58,
      gap = parts.length > 1 ? 2 / r : 0;
    const point = (a: number) =>
      `${(c + r * Math.sin(a)).toFixed(2)},${(c - r * Math.cos(a)).toFixed(2)}`;
    let angle = 0;
    const segs = parts
      .map(([src, v]) => {
        const sweep = (v / total) * Math.PI * 2;
        const a0 = angle + gap / 2,
          a1 = angle + sweep - gap / 2;
        angle += sweep;
        const name = this.sourceLabel(src);
        // A share never rounds into a claim the data does not make: 100.0% beside
        // another source, or 0.0% for a real amount.
        const pct = (v / total) * 100;
        const pctText =
          parts.length > 1 && pct >= 99.95
            ? ">99.9%"
            : pct < 0.05
              ? "<0.1%"
              : `${pct.toFixed(1)}%`;
        // Maintainer decision 2 (2026-10-06): no share at all while the denominator
        // is incomplete (requests missing a component, or their count unknown).
        const share = incomplete
          ? "不给占比：有缺分量记录，分母不完整"
          : `占近 14 天完整记录 ${pctText}`;
        const lines = [
          { text: name, value: false },
          { text: fmtTokens(v), value: true },
          { text: share, value: false },
        ];
        const tip = this.areaTip(lines);
        // A lone source is a full ring: two half arcs (one arc cannot close a circle).
        const d =
          parts.length === 1
            ? `M${point(0)} A${r},${r} 0 1 1 ${point(Math.PI)} A${r},${r} 0 1 1 ${point(Math.PI * 2 - 0.0001)}`
            : `M${point(a0)} A${r},${r} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${point(Math.max(a1, a0 + 0.0001))}`;
        return (
          `<g class="wb-area-hotspot wb-ring-seg" tabindex="0" role="img" aria-label="${escapeHtml(lines.map((l) => l.text).join("，"))}">` +
          `<path d="${d}" class="wb-ring-arc" style="stroke:${color(src)}"/>` +
          `<g class="wb-area-tip" transform="translate(${(c - tip.width / 2).toFixed(1)},${(c - 26).toFixed(1)})">${tip.plate}</g></g>`
        );
      })
      .join("");
    const legend = parts
      .map(
        ([src, v]) =>
          `<li><i style="--wb-swatch:${color(src)}" aria-hidden="true"></i><span>${escapeHtml(this.sourceLabel(src))}</span><b>${escapeHtml(fmtTokens(v))}</b></li>`
      )
      .join("");
    const left = partialUnknown
      ? "；部分请求数未知"
      : partialRequests || outputOnly
        ? `；不含缺分量请求 ${partialRequests.toLocaleString("zh-CN")} 条、仅输出可见行 ${outputOnly}`
        : "";
    return (
      `<div class="wb-ring">` +
      `<svg class="wb-ring-svg" viewBox="0 0 ${size} ${size}" role="group" aria-label="近 14 天完整 Token 按网关构成，按 Tab 逐段查看">` +
      `<text x="${c}" y="${c - 2}" text-anchor="middle" class="wb-ring-total">${escapeHtml(fmtTokens(total))}</text>` +
      `<text x="${c}" y="${c + 16}" text-anchor="middle" class="wb-ring-sub">完整 Token</text>${segs}</svg>` +
      `<ul class="wb-chart-legend wb-ring-legend" aria-label="图例">${legend}</ul></div>` +
      `<p class="wb-muted">口径：近 14 天输入+输出齐全的记录${left}。${incomplete ? "分母不完整，悬停提示不给占比。" : "占比只在悬停提示里，分母是这些完整记录。"}</p>`
    );
  }
  private tokenTrendHtml(): string {
    const read = this.usageTokens;
    const note = `<p class="wb-muted">${this.usageReadNote(read, "近 14 天完整 Token")}</p>`;
    if (!read || !read.payload)
      return note + this.emptyTrendHtml("Token", this.beijingDayKeys(14));
    const payload = read.payload;
    const labels = this.beijingDayKeys(14);
    const inWindow = (date: string) =>
      date >= labels[0]! && date <= labels.at(-1)!;
    const rows = payload.days.filter((row) => inWindow(row.date));
    const dayTotals = new Map<string, number>();
    const rowsByDay = new Map<string, number>();
    const partialByDay = new Map<string, number>();
    const bySeries = new Map<
      string,
      {
        source: string;
        model: string;
        values: Map<string, number>;
        input: number;
        output: number;
        days: number;
      }
    >();
    let partialRequests = 0;
    let partialKnown = true;
    let partialOutput: number | null = null;
    let partialOutputMissing = 0;
    for (const row of rows) {
      rowsByDay.set(row.date, (rowsByDay.get(row.date) ?? 0) + 1);
      // F1: the complete total takes ONLY the requests that carry both
      // components — the backend's input_tokens/output_tokens already sum
      // exactly those, so one partial request in the same bucket no longer
      // pushes its complete neighbours out of the total. The partial requests
      // are counted and listed separately (partial_output_tokens backs
      // 仅输出可见) and are never folded into the total nor into 0.
      const complete = this.completeTokens(row);
      if (complete !== null) {
        dayTotals.set(row.date, (dayTotals.get(row.date) ?? 0) + complete);
        const key = `${row.source}|${row.model}`;
        const series = bySeries.get(key) ?? {
          source: row.source,
          model: row.model,
          values: new Map<string, number>(),
          input: 0,
          output: 0,
          days: 0,
        };
        series.values.set(
          row.date,
          (series.values.get(row.date) ?? 0) + complete
        );
        series.input += row.input_tokens ?? 0;
        series.output += row.output_tokens ?? 0;
        series.days += 1;
        bySeries.set(key, series);
      }
      const partial = this.partialRequests(row);
      if (partial === null) partialKnown = false;
      else if (partial > 0) {
        partialByDay.set(row.date, (partialByDay.get(row.date) ?? 0) + partial);
        partialRequests += partial;
        const partialOut = this.partialOutputTokens(row);
        if (partialOut === null) partialOutputMissing += partial;
        else partialOutput = (partialOutput ?? 0) + partialOut;
      }
    }
    // 仅输出可见: the buckets whose partial requests miss a component. Their
    // visible output numbers are real (missing[] carries the partial bucket's own
    // input_tokens/output_tokens — a lookup in days[] would now read the
    // COMPLETE component and mislabel it as the partial one); the missing input
    // stays 未知 and the bucket never enters the complete total above. A row whose
    // timestamp is invalid arrives with date=null: it can be assigned to NO day,
    // so the window filter KEEPS it and it is listed separately as 时间无法判定
    // instead of being silently dropped (and never guessed onto a day).
    const missingRows = payload.missing.filter(
      (m) => m.date === null || inWindow(m.date)
    );
    const undatedMissing = missingRows.filter((r) => r.date === null);
    const outputOnly = [
      ...missingRows.filter((r) => r.date !== null),
      ...undatedMissing,
    ];
    const visibleNumber = (value: unknown): number | null =>
      typeof value === "number" && Number.isFinite(value) ? value : null;
    const outputOnlyTotal = outputOnly.reduce(
      (s, r) => s + (visibleNumber(r.output_tokens) ?? 0),
      0
    );
    const outputOnlyDays = new Set(
      outputOnly
        .map((r) => r.date)
        .filter((d): d is string => typeof d === "string")
    );
    const outputOnlyNoNumber = outputOnly.filter(
      (r) => visibleNumber(r.output_tokens) === null
    ).length;
    const undatedMissingText = undatedMissing.length
      ? `；另有 ${undatedMissing.length} 行时间无法判定（时间戳无效，不归任何一天：${undatedMissing
          .slice(0, 10)
          .map(
            (r) =>
              `${escapeHtml(this.sourceLabel(r.source))}/${escapeHtml(r.model)}`
          )
          .join(
            "、"
          )}${undatedMissing.length > 10 ? "…" : ""}），既不进完整总量也不计入任何一天`
      : "";
    const seriesList = [...bySeries.entries()]
      .map(([key, s]) => ({
        key,
        ...s,
        total: [...s.values.values()].reduce((a, b) => a + b, 0),
      }))
      .sort((a, b) => b.total - a.total);
    const ambiguous = new Set(
      seriesList.map((s) => s.model).filter((m, i, all) => all.indexOf(m) !== i)
    );
    // 按来源: the model rows summed per source, plus every source the coverage
    // block names (a source with no complete row is listed, never dropped).
    const bySource = new Map<
      string,
      { values: Map<string, number>; input: number; output: number }
    >();
    for (const s of seriesList) {
      const agg = bySource.get(s.source) ?? {
        values: new Map<string, number>(),
        input: 0,
        output: 0,
      };
      for (const [d, v] of s.values)
        agg.values.set(d, (agg.values.get(d) ?? 0) + v);
      agg.input += s.input;
      agg.output += s.output;
      bySource.set(s.source, agg);
    }
    const cov = payload.coverage;
    const covSources =
      cov?.sources &&
      typeof cov.sources === "object" &&
      !Array.isArray(cov.sources)
        ? cov.sources
        : {};
    const sourceList = [
      ...new Set([...bySource.keys(), ...Object.keys(covSources)]),
    ]
      .map((src) => {
        const agg = bySource.get(src);
        return {
          key: `src:${src}`,
          source: src,
          values: agg?.values ?? new Map<string, number>(),
          input: agg?.input ?? 0,
          output: agg?.output ?? 0,
          total: agg ? [...agg.values.values()].reduce((a, b) => a + b, 0) : 0,
          has: Boolean(agg),
        };
      })
      .sort(
        (a, b) =>
          (SOURCE_SLOTS[a.source] ?? 9) - (SOURCE_SLOTS[b.source] ?? 9) ||
          a.source.localeCompare(b.source)
      );
    const bySourceMode = this.tokenGroup === "source";
    const drawnModels = new Set(
      seriesList
        .filter((s) => this.tokenSeriesOpen.has(s.key))
        .slice(0, TOKEN_SERIES_MAX)
        .map((s) => s.key)
    );
    const modelSlots = this.seriesSlots(this.tokenSeriesSlot, [...drawnModels]);
    // Every source with a fixed colour slot is drawn unless the user hid it; a
    // source without a slot is listed only (no generated fifth hue).
    const drawnSources = new Set(
      sourceList
        .filter(
          (s) =>
            s.has &&
            SOURCE_SLOTS[s.source] &&
            !this.tokenSourceHidden.has(s.source)
        )
        .map((s) => s.key)
    );
    const totalValues = labels.map((d) =>
      dayTotals.has(d) ? dayTotals.get(d)! : null
    );
    const chartSeries = [
      {
        key: "__total__",
        label: "总计",
        values: totalValues,
        kind: "total" as const,
      },
      ...(bySourceMode
        ? sourceList
            .filter((s) => drawnSources.has(s.key))
            .map((s) => ({
              key: s.key,
              label: this.sourceLabel(s.source),
              kind: "model" as const,
              slot: SOURCE_SLOTS[s.source],
              values: labels.map((d) =>
                s.values.has(d) ? s.values.get(d)! : null
              ),
            }))
        : seriesList
            .filter((s) => drawnModels.has(s.key))
            .map((s) => ({
              key: s.key,
              label: this.tokenSeriesLabel(
                s.source,
                s.model,
                ambiguous.has(s.model)
              ),
              kind: "model" as const,
              slot: modelSlots.get(s.key),
              values: labels.map((d) =>
                s.values.has(d) ? s.values.get(d)! : null
              ),
            }))),
    ];
    const chart = this.tokenSeriesSvg(labels, chartSeries, {
      aria: `近 ${labels.length} 天每日完整 Token 趋势，逐日悬停或按 Tab 查看数值`,
      tipTime: (label: string) => `${label}（北京时间）`,
      unit: "Token（输入 + 输出，完整记录）",
      scope: "日",
      // Drawn for the overview's full-row card (~950–1050px at 1280–1366).
      width: 880,
      // 总计线的空日分两种（维护者 2026-09-28）：当天有账本行但缺完整分量
      // 与当天无任何账本行——图上都断线，但 tooltip 不得只写「无记录」。
      missingLabel: (i) => {
        const d = labels[i]!;
        return rowsByDay.has(d) || partialByDay.has(d) || outputOnlyDays.has(d)
          ? "缺完整分量（当日有账本记录）"
          : "无记录";
      },
    });
    const zeroDays = labels.filter(
      (d) => dayTotals.has(d) && dayTotals.get(d) === 0
    );
    const missingDays = labels.filter(
      (d) =>
        !dayTotals.has(d) &&
        (rowsByDay.has(d) || partialByDay.has(d) || outputOnlyDays.has(d))
    );
    const noRecordDays = labels.filter(
      (d) => !dayTotals.has(d) && !rowsByDay.has(d) && !outputOnlyDays.has(d)
    );
    const windowTotal = labels.reduce((s, d) => s + (dayTotals.get(d) ?? 0), 0);
    const windowInput = rows.reduce(
      (s, r) =>
        s + (this.completeTokens(r) === null ? 0 : (r.input_tokens ?? 0)),
      0
    );
    const windowOutput = rows.reduce(
      (s, r) =>
        s + (this.completeTokens(r) === null ? 0 : (r.output_tokens ?? 0)),
      0
    );
    const partialCountText = partialKnown
      ? partialRequests.toLocaleString("zh-CN")
      : "未知";
    // No partial request at all is a known zero, not 未知.
    const partialOutputText =
      partialOutput !== null
        ? fmtTokens(partialOutput)
        : partialKnown && partialRequests === 0
          ? "0"
          : "未知";
    // The day-state counts stay resident under the chart (they qualify its
    // points); only their explanation and the row lists fold away.
    const dayLine = `<p class="wb-muted wb-tok-days">零记录 ${zeroDays.length} 天 · 无记录 ${noRecordDays.length} 天（断线）· 缺分量 ${missingDays.length} 天 · 部分请求 ${partialCountText} 条、仅输出可见 ${partialOutputText}（另列，不进完整总量）</p>`;
    // A coverage gap (ledger absent, source off, read errors, truncation) is a
    // state the page asserts, so it keeps a resident line; the per-source
    // wording sits in the fold.
    const covGaps = Object.keys(covSources).filter((src) => {
      const c = covSources[src] ?? {};
      return (
        c.ledger_present !== true ||
        c.enabled !== true ||
        (Array.isArray(c.errors) && c.errors.length > 0)
      );
    });
    const cut =
      cov?.truncated === true ||
      cov?.clamped === true ||
      (typeof cov?.dropped_rows === "number" && cov.dropped_rows > 0) ||
      (typeof cov?.dropped_missing_rows === "number" &&
        cov.dropped_missing_rows > 0);
    const covLine = !Object.keys(covSources).length
      ? `<p class="wb-tok-cov"><span class="wb-badge wb-badge-warn">覆盖未知</span>后端未返回来源覆盖，账本是否被读取未知，不按零用量推断。</p>`
      : covGaps.length || cut
        ? `<p class="wb-tok-cov"><span class="wb-badge wb-badge-warn">覆盖有缺口</span>${covGaps.length ? `${escapeHtml(covGaps.map((src) => this.sourceLabel(src)).join("、"))} 的账本缺席、来源未启用或读取出错` : ""}${covGaps.length && cut ? "；" : ""}${cut ? "结果被截断或收口" : ""}；缺口部分没有可读数据，不按 0 显示（详见下方折叠）。</p>`
        : "";
    // Right column: the breakdown table. Its first column is the draw toggle, so
    // the table is also the chart's series picker.
    const toggle = (
      key: string,
      label: string,
      on: boolean,
      slot: number | undefined,
      disabledWhy: string
    ) => {
      const verb = on ? "从图上移除" : "画到图上";
      const swatch =
        on && slot
          ? `<i class="wb-series-swatch" style="--wb-swatch:var(--wb-cat-${slot})" aria-hidden="true"></i>`
          : "";
      return `<button type="button" class="wb-tok-draw" data-wb-series-toggle="${escapeHtml(key)}" aria-pressed="${on}" aria-label="${verb}：${escapeHtml(label)}" title="${escapeHtml(disabledWhy || `${verb}：${label}`)}"${disabledWhy ? " disabled" : ""}>${swatch}</button>`;
    };
    const cells = (
      total: number | null,
      input: number | null,
      output: number | null
    ) =>
      `<td class="wb-fee-num">${total === null ? "—" : fmtTokens(total)}</td><td class="wb-fee-num">${input === null ? "—" : fmtTokens(input)}</td><td class="wb-fee-num">${output === null ? "—" : fmtTokens(output)}</td>`;
    const q = this.tokenSeriesQ.trim().toLowerCase();
    const matched = q
      ? seriesList.filter(
          (s) =>
            s.model.toLowerCase().includes(q) ||
            this.sourceLabel(s.source).toLowerCase().includes(q) ||
            s.source.toLowerCase().includes(q)
        )
      : seriesList;
    const MODEL_ROWS = 40;
    const bodyRows = bySourceMode
      ? sourceList
          .map((s) => {
            const slot = SOURCE_SLOTS[s.source];
            const why = !s.has
              ? "该来源窗口内没有完整记录，无线可画"
              : !slot
                ? "该来源没有固定颜色，只在表中列出"
                : "";
            return (
              `<tr><td class="wb-center">${toggle(s.key, this.sourceLabel(s.source), drawnSources.has(s.key), slot, why)}</td>` +
              `<td>${escapeHtml(this.sourceLabel(s.source))}<small class="wb-muted">${s.has ? `完整 ${s.values.size} 天` : "窗口内无完整记录（不按 0）"}</small></td>` +
              `${s.has ? cells(s.total, s.input, s.output) : cells(null, null, null)}</tr>`
            );
          })
          .join("")
      : matched
          .slice(0, MODEL_ROWS)
          .map((s) => {
            const label = this.tokenSeriesLabel(
              s.source,
              s.model,
              ambiguous.has(s.model)
            );
            const on = drawnModels.has(s.key);
            const why =
              !on && drawnModels.size >= TOKEN_SERIES_MAX
                ? `图上最多同时画 ${TOKEN_SERIES_MAX} 个模型，先移除一个`
                : "";
            return (
              `<tr><td class="wb-center">${toggle(s.key, label, on, modelSlots.get(s.key), why)}</td>` +
              `<td>${escapeHtml(s.model)}<small class="wb-muted">${escapeHtml(this.sourceLabel(s.source))} · 完整 ${s.days} 天</small></td>${cells(s.total, s.input, s.output)}</tr>`
            );
          })
          .join("");
    const emptyRow = `<tr><td colspan="5">${bySourceMode ? "该窗口没有来源记录。" : q ? "没有匹配的模型；换关键词或清空搜索。" : "该窗口没有完整的来源+模型行。"}</td></tr>`;
    const groupTabs =
      `<div class="wb-section-tabs wb-tok-group" role="group" aria-label="分类方式">` +
      `<button type="button" data-wb-tok-group="source" aria-pressed="${bySourceMode}">按来源</button>` +
      `<button type="button" data-wb-tok-group="model" aria-pressed="${!bySourceMode}">按模型</button></div>`;
    const search = bySourceMode
      ? ""
      : `<input type="text" class="wb-trend-q" data-wb-series-q placeholder="按来源 / 模型检索" value="${escapeHtml(this.tokenSeriesQ)}" autocomplete="off" spellcheck="false" aria-label="检索模型"/>`;
    const tableNote = bySourceMode
      ? `<small class="wb-muted">首列切换是否画在图上；来源颜色固定。</small>`
      : `<small class="wb-muted">图上 ${drawnModels.size} / ${TOKEN_SERIES_MAX} 个模型 · 表中 ${Math.min(matched.length, MODEL_ROWS)} / ${seriesList.length} 行${matched.length > MODEL_ROWS ? `（其余 ${matched.length - MODEL_ROWS} 行请用搜索）` : ""}</small>`;
    const table =
      `<div class="wb-tok-side"><div class="wb-tok-tools">${groupTabs}${search}</div>` +
      `<div class="wb-table-scroll wb-tok-scroll"><table class="wb-table wb-tok-table"><thead><tr><th class="wb-center">图上</th><th>${bySourceMode ? "来源" : "模型"}</th><th class="wb-fee-num">完整 Token</th><th class="wb-fee-num">输入</th><th class="wb-fee-num">输出</th></tr></thead>` +
      `<tbody>${bodyRows || emptyRow}</tbody>` +
      `<tfoot><tr><td></td><th scope="row">14 天合计</th>${cells(windowTotal, windowInput, windowOutput)}</tr></tfoot></table></div>${tableNote}</div>`;
    const missingHtml = outputOnly.length
      ? `<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>日期</th><th>来源</th><th>模型</th><th class="wb-fee-num">输出（可见）</th><th class="wb-fee-num">输入（可见）</th><th>缺失分量</th><th>原因</th></tr></thead><tbody>${outputOnly
          .slice(0, 40)
          .map(
            (r) =>
              `<tr><td>${r.date === null ? "时间无法判定" : escapeHtml(r.date)}</td><td>${escapeHtml(this.sourceLabel(r.source))}</td><td>${escapeHtml(r.model)}</td><td class="wb-fee-num">${visibleNumber(r.output_tokens) === null ? "未知" : fmtTokens(visibleNumber(r.output_tokens)!)}</td><td class="wb-fee-num">${visibleNumber(r.input_tokens) === null ? "未知" : fmtTokens(visibleNumber(r.input_tokens)!)}</td><td>${escapeHtml(MISSING_REASON_LABEL[r.reason] ?? r.reason)}</td><td>${escapeHtml(r.reason)}</td></tr>`
          )
          .join(
            ""
          )}</tbody></table></div><p class="wb-muted">仅输出可见合计 ${fmtTokens(outputOnlyTotal)}（只统计部分请求里可见的输出分量，其余不补 0${outputOnlyNoNumber ? `；另有 ${outputOnlyNoNumber} 行连输出数字也没有` : ""}），共 ${outputOnlyDays.size} 天；不计入上方完整总量${undatedMissingText}。</p>`
      : `<p class="wb-muted">该窗口没有缺分量的历史行。</p>`;
    const dayNote = `零记录 = 账本记录为 0，图上按 0 画；无记录 = 当天没有任何账本行，图上断线；缺分量 = 输入或输出缺一，见下表。部分请求只进「仅输出可见」，不进完整总量${partialOutputMissing ? `（其中 ${partialOutputMissing} 条连输出分量也没有）` : ""}。`;
    const details = this.laneNoteFoldHtml(
      "tok-coverage",
      "数据覆盖与缺分量明细",
      `<p class="wb-muted">${dayNote}</p>${this.usageCoverageHtml(payload.coverage, "近 14 天 Token")}` +
        `<h4 class="wb-subhead">仅输出可见 / 时间无法判定（不混入完整总量）</h4>${missingHtml}`
    );
    return (
      note +
      covLine +
      `<div class="wb-tok-split"><div class="wb-tok-main">${chart}${dayLine}</div>${table}</div>` +
      details
    );
  }
  /** A12-3 24h panel: hour+source+model buckets of the whole-request
   *  average output rate (output tokens ÷ the request's own wall time, the
   *  first-token wait included), newest hour first. Records the backend
   *  skipped are counted on their own line and never enter a denominator; the
   *  reason counts are independent (one record may be counted twice) and a
   *  record with an invalid timestamp is counted as 时间无法判定. A bucket whose
   *  rate is null shows 未知 rather than 0 — a DEFENSIVE branch: the endpoint
   *  emits a bucket only for records with output tokens and a positive duration,
   *  so every row it returns carries a computable rate. The server's per-source
   *  coverage block is rendered as 数据覆盖 so an empty window is not read as
   *  zero usage while the collection chain is off. F4a adds the hourly trend
   *  chart (same fixed Beijing hours, same whole-request average) above the
   *  table. 逐次 opens the per-request rows of that bucket. */
  private ratePanelHtml(): string {
    const read = this.usageRates;
    const note = `<p class="wb-muted">${this.usageReadNote(read, "近 24 小时整次平均输出 Token/s")}</p>`;
    if (!read || !read.payload)
      return note + this.emptyTrendHtml("Token/s", this.beijingHourKeys(24));
    const payload = read.payload;
    const rows = [...payload.hours].sort(
      (a, b) =>
        b.hour.localeCompare(a.hour) ||
        a.source.localeCompare(b.source) ||
        a.model.localeCompare(b.model)
    );
    const skipped = payload.skipped;
    const skippedHtml = `<div class="wb-stats-row"><div><small>缺输出 Token</small><b>${this.countText(skipped.missing_output_tokens)}</b></div><div><small>缺耗时</small><b>${this.countText(skipped.missing_duration)}</b></div><div><small>无效耗时</small><b>${this.countText(skipped.invalid_duration)}</b></div><div><small>时间无法判定</small><b>${this.countText(skipped.invalid_time)}</b></div><div><small>参与分母的分桶</small><b>${rows.filter((r) => r.rate !== null).length} / ${rows.length}</b></div></div>`;
    const table = rows.length
      ? `<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>小时（北京时间）</th><th>来源</th><th>模型</th><th class="wb-fee-num">请求数</th><th class="wb-fee-num">输出 token</th><th class="wb-fee-num">有效耗时</th><th class="wb-fee-num">整次平均输出 Token/s</th><th class="wb-center">逐次</th></tr></thead><tbody>${rows
          .slice(0, 60)
          .map(
            (r) =>
              `<tr><td>${escapeHtml(this.hourLabel(r.hour))}</td><td>${escapeHtml(this.sourceLabel(r.source))}</td><td>${escapeHtml(r.model)}</td><td class="wb-fee-num">${this.numText(r.requests)}</td><td class="wb-fee-num">${this.numText(r.output_tokens)}</td><td class="wb-fee-num">${r.duration_ms > 0 ? `${(r.duration_ms / 1000).toFixed(1)}s` : "—"}</td><td class="wb-fee-num">${r.rate === null ? "未知" : r.rate.toFixed(2)}</td><td class="wb-center"><button type="button" class="wb-btn wb-btn-sm" data-wb-rate-drill="${escapeHtml(`${r.hour}|${r.source}|${r.model}`)}" title="查看该时段的逐次明细（账本尾部读取，有界）" aria-label="查看该时段的逐次明细">${wbIcon("detail")}${wbIconHint("查看该时段的逐次明细")}</button></td></tr>`
          )
          .join("")}</tbody></table></div>`
      : `<p class="wb-muted">近 24 小时没有可用分桶记录；无法判定为零速率（账本是否被读取见下方「数据覆盖」说明，不写成 0 用量）。</p>`;
    const detail = this.rateDrill ? this.rateDrillHtml(this.rateDrill) : "";
    return (
      note +
      this.rateTrendHtml(payload.hours) +
      table +
      skippedHtml +
      this.usageCoverageHtml(payload.coverage, "近 24 小时速率") +
      `<p class="wb-muted">被跳过的记录不参与分母；缺输出 Token / 缺耗时 / 无效耗时三项计数相互独立（同一条记录可同时计入多项，其和可能大于被跳过的记录条数），时间无法判定（时间戳无效）单独计数；rate 为「未知」的分桶表示该桶无法计算，不按 0 显示。</p>` +
      detail
    );
  }
  /** F4a: the 24h panel's hourly trend — x = Beijing hour (the same fixed
   *  UTC+8 axis the rate buckets use, console_stats.py `_hour_label`), y = that
   *  hour's whole-request average output rate (Σ output tokens ÷ Σ wall time over
   *  the buckets that CAN be computed — a weighted average, not a mean of rates).
   *  Up to three model series follow as dashed lines. The rate endpoint emits a
   *  bucket only for records that HAVE output tokens and a positive duration, so
   *  every returned bucket carries a computable rate and the null branch below is
   *  DEFENSIVE (an upstream contract change must render 未知, never a 0 — it is
   *  not the current cause of anything). Reuses tokenSeriesSvg: no chart library,
   *  same .wb-* tokens and dark/light theme. */
  private rateTrendHtml(rows: UsageRateRow[]): string {
    const labels = this.beijingHourKeys(24);
    const inWindow = (key: string) =>
      key >= labels[0]! && key <= labels.at(-1)!;
    const buckets = rows.filter((r) => inWindow(r.hour.slice(0, 13)));
    const totals = new Map<string, { output: number; duration: number }>();
    const bySeries = new Map<
      string,
      {
        source: string;
        model: string;
        hours: Map<string, { output: number; duration: number }>;
        output: number;
      }
    >();
    let computable = 0,
      unknown = 0;
    for (const row of buckets) {
      if (row.rate === null) {
        unknown += 1;
        continue;
      }
      computable += 1;
      const hour = row.hour.slice(0, 13);
      const output = Number.isFinite(row.output_tokens) ? row.output_tokens : 0;
      const duration = Number.isFinite(row.duration_ms) ? row.duration_ms : 0;
      const total = totals.get(hour) ?? { output: 0, duration: 0 };
      total.output += output;
      total.duration += duration;
      totals.set(hour, total);
      const key = `${row.source}|${row.model}`;
      const series = bySeries.get(key) ?? {
        source: row.source,
        model: row.model,
        hours: new Map(),
        output: 0,
      };
      const acc = series.hours.get(hour) ?? { output: 0, duration: 0 };
      acc.output += output;
      acc.duration += duration;
      series.hours.set(hour, acc);
      series.output += output;
      bySeries.set(key, series);
    }
    const rateOf = (acc: { output: number; duration: number } | undefined) =>
      acc && acc.duration > 0 ? acc.output / (acc.duration / 1000) : null;
    const totalValues = labels.map((h) => rateOf(totals.get(h)));
    const modelSeries = [...bySeries.entries()]
      .sort((a, b) => b[1].output - a[1].output)
      .slice(0, 3);
    const slots = this.seriesSlots(
      this.rateSeriesSlot,
      modelSeries.map(([key]) => key)
    );
    const ambiguous = new Set(
      modelSeries
        .map(([, s]) => s.model)
        .filter((m, i, all) => all.indexOf(m) !== i)
    );
    const chartSeries = [
      {
        key: "__total__",
        label: "全部模型",
        values: totalValues,
        kind: "total" as const,
      },
      ...modelSeries.map(([key, s]) => ({
        key,
        label: this.tokenSeriesLabel(s.source, s.model, ambiguous.has(s.model)),
        values: labels.map((h) => rateOf(s.hours.get(h))),
        kind: "model" as const,
        slot: slots.get(key),
      })),
    ];
    const chart = this.tokenSeriesSvg(labels, chartSeries, {
      aria: "近 24 小时整次平均输出 Token/s 按小时趋势，逐小时悬停或按 Tab 查看数值",
      axis: (label: string) => `${label.slice(5).replace("T", " ")}:00`,
      tipTime: (label: string) =>
        `${label.slice(5).replace("T", " ")}:00（北京时间）`,
      format: (value: number) => `${value.toFixed(2)} Token/s`,
      tick: (value: number) => value.toFixed(0),
      unit: "Token/s（整次平均输出速度）",
      scope: "小时",
      // T1: the dashed baseline is the window's WEIGHTED rate (Σ output ÷ Σ wall
      // time, the same reading as the overview card), never a mean of hours.
      mean: {
        label: "近 24 小时加权平均",
        value: (() => {
          let o = 0,
            d = 0;
          for (const t of totals.values()) {
            o += t.output;
            d += t.duration;
          }
          return d > 0 ? o / (d / 1000) : null;
        })(),
      },
      // A sum of hourly Token/s is not a reading: only the last value is
      // written (the window's weighted mean stays in the caption below).
      totalLabel: "",
      // 空小时 = 该小时没有可计算分桶（rate 未知），不是「无记录」。
      // 提示板首行 = 系列名 + 该句，而 `areaTip` 的行宽上限是 260 单位：整句
      // 「…（rate 未知，不按 0）」实测 264.19，会被省略号截断；hotspot 的
      // `aria-label` 带的是全文，门禁因此判「aria-label 与自身提示文本不一致」。
      // 故首行只留状态句「（rate 未知）」；「不按 0」仍在图注与本区块的常驻
      // 说明里，不在提示板重复。
      missingLabel: () => "该小时无可计算分桶（rate 未知）",
    });
    const caption = `图注：近 24 小时共 ${buckets.length} 个速率分桶，${computable} 个可计算速率并进图，${unknown} 个 rate 未知不进图、不按 0 画（防御分支：该端点的行必然带可计算速率；若上游契约变化导致无法计算则显示未知，不折 0）；纵轴为该小时可计算分桶的加权平均（Σ 输出 ÷ Σ 整次耗时）；模型系列 ${bySeries.size} 条，图上按输出量取前 ${modelSeries.length} 条，其余仍在下方明细表。`;
    return chart + `<p class="wb-muted">${caption}</p>`;
  }
  /** F4b: which feed id carries a logical source's rows, per the server's own
   *  coverage statement. The feed is requested with every spelling the alias
   *  table can name (the server owns the id→ledger table) and the server answers
   *  each COVERED id with its own rows while naming every id it does NOT read in
   *  the response's `sources_unavailable` (usagesrv.py /api/console/logs). Since
   *  one logical source can be registered under two spellings for the SAME ledger,
   *  the rows are taken from ONE covered spelling (the first that has rows) — a
   *  record is never listed twice — and `duplicates` names the spellings left out
   *  for that reason. `state` is "uncovered" only on the SERVER's statement and
   *  stays "unknown" when the response does not carry one: the client never reads
   *  its own request list as a coverage claim. A source the page does not request
   *  at all (no feed id in the alias table) has no drillable ledger. */
  private feedSpelling(source: string): {
    state: "unknown" | "uncovered" | "covered";
    spelling: string | null;
    covered: string[];
    duplicates: string[];
    rows: LogEntry[];
  } {
    const feed = this.logsFeed;
    const rows = feed && feed.state === "live" ? feed.rows : [];
    const aliases = (LOG_FEED_SOURCE_ALIAS[source] ?? []).filter((s) =>
      (LOG_FEED_SOURCES as readonly string[]).includes(s)
    );
    if (!aliases.length)
      return {
        state: "uncovered",
        spelling: null,
        covered: [],
        duplicates: [],
        rows: [],
      };
    const unavailable =
      feed && feed.state === "live" ? feed.unavailable : undefined;
    if (unavailable === undefined)
      return {
        state: "unknown",
        spelling: null,
        covered: [],
        duplicates: [],
        rows: [],
      };
    const covered = aliases.filter((s) => !unavailable.includes(s));
    if (!covered.length)
      return {
        state: "uncovered",
        spelling: null,
        covered,
        duplicates: [],
        rows: [],
      };
    const spelling =
      covered.find((s) => rows.some((e) => e.source === s)) ?? covered[0]!;
    return {
      state: "covered",
      spelling,
      covered,
      duplicates: covered.filter((s) => s !== spelling),
      rows: rows.filter((e) => e.source === spelling),
    };
  }
  /** A ledger timestamp → the Beijing (UTC+8) `YYYY-MM-DDTHH` key of the instant
   *  it denotes: the same axis the rate buckets' `hour` label uses
   *  (console_stats._hour_label), so a bucket's Beijing hour can be compared
   *  with a ledger row whatever zone that ledger wrote (the CN ledgers write
   *  "+08:00" strings, the international ledger writes UTC "...Z"). An
   *  unparsable timestamp returns null and never matches silently. */
  private beijingHourKeyOf(time: string): string | null {
    const t = Date.parse(time);
    if (!Number.isFinite(t)) return null;
    return new Date(t + 8 * 3600 * 1000).toISOString().slice(0, 13);
  }
  /** F4b: the feed rows with the alias group's duplicate copies collapsed.
   *  The feed is requested with every spelling the alias table can name, so a
   *  ledger the server registers under two ids answers the SAME records twice —
   *  once per id. The alias table says which spellings name one logical source,
   *  so all but the first covered spelling that has rows are left out (`collapsed`)
   *  and the kept spelling is named (`kept`); a record is never listed, filtered
   *  or counted twice. The rows of a source without aliases pass through
   *  unchanged, and an absent coverage statement (older backend) collapses
   *  nothing — the client never guesses which spellings are one ledger. */
  private feedVisibleRows(): {
    rows: LogEntry[];
    collapsed: string[];
    kept: string[];
  } {
    const feed = this.logsFeed;
    if (!feed || feed.state !== "live")
      return { rows: [], collapsed: [], kept: [] };
    const unavailable = feed.unavailable;
    if (unavailable === undefined)
      return { rows: feed.rows, collapsed: [], kept: [] };
    const collapsed: string[] = [],
      kept: string[] = [];
    for (const spellings of Object.values(LOG_FEED_SOURCE_ALIAS)) {
      const requested = spellings.filter((s) =>
        (LOG_FEED_SOURCES as readonly string[]).includes(s)
      );
      if (requested.length < 2) continue;
      const covered = requested.filter((s) => !unavailable.includes(s));
      if (covered.length < 2) continue;
      const keptSpelling = covered.find((s) =>
        feed.rows.some((e) => e.source === s)
      );
      if (!keptSpelling) continue;
      kept.push(keptSpelling);
      collapsed.push(...covered.filter((s) => s !== keptSpelling));
    }
    if (!collapsed.length) return { rows: feed.rows, collapsed, kept };
    const hidden = new Set(collapsed);
    return {
      rows: feed.rows.filter((e) => !hidden.has(e.source)),
      collapsed,
      kept,
    };
  }
  /** Per-request rows of one rate bucket, read from the SAME bounded
   *  ledger tail the log lane shows and filtered by hour+source+model. The
   *  rate bucket ids and the ledger ids are not identical strings, so the
   *  known mapping is spelled out (usagesrv.py `_console_log_readers`: the
   *  "qoder" feed IS the CN gateway's usage.jsonl, "qoder-global" /
   *  "qoder_global" the international chain's requests.log — F4b). The
   *  coverage verdict comes from the server's `sources_unavailable`, so 「该来源
   *  不在调用日志账本内」 is never claimed from the client's own request list,
   *  and a source the tail covers but this bounded read has no rows for is told
   *  apart from it instead of both showing an empty table as if it were zero
   *  rows. */
  private rateDrillHtml(drill: {
    hour: string;
    source: string;
    model: string;
  }): string {
    const head = `<div class="wb-chart-head"><h3>逐次明细 · ${escapeHtml(this.hourLabel(drill.hour))} · ${escapeHtml(this.sourceLabel(drill.source))} · ${escapeHtml(drill.model)}</h3><button type="button" class="wb-btn wb-btn-sm" data-wb-rate-close="1">返回汇总</button></div>`;
    const feed = this.logsFeed;
    if (!feed || feed.state !== "live") {
      return `<div class="wb-rate-drill">${head}<p class="wb-muted">逐次明细不可取得：调用日志账本未读取（${feed ? escapeHtml(feed.reason ?? "原因未知") : "尚未读取"}）；不以空表冒充零记录。</p></div>`;
    }
    const found = this.feedSpelling(drill.source);
    if (found.state === "unknown") {
      return `<div class="wb-rate-drill">${head}<p class="wb-muted">接口未返回来源覆盖声明（sources_unavailable）：该来源是否在调用日志账本的读取范围内未知；逐次明细不可得，不以空表冒充零记录。</p></div>`;
    }
    if (found.state === "uncovered") {
      const why = (LOG_FEED_SOURCE_ALIAS[drill.source] ?? []).length
        ? "服务端声明该来源的账本 id 不在调用日志账本的读取范围内（sources_unavailable）"
        : "本页请求的来源列表不含该来源的账本 id（um 由 chat2api 账本记账、dsh 是冻结日快照、cliproxy 采集链未启用）";
      return `<div class="wb-rate-drill">${head}<p class="wb-muted">该来源不在调用日志账本内：${why}；逐次明细不可得，不以空表冒充零记录。</p></div>`;
    }
    const sourceRows = found.rows;
    if (!sourceRows.length) {
      return `<div class="wb-rate-drill">${head}<p class="wb-muted">本次读取的日志尾部没有该来源（账本 id ${escapeHtml(found.spelling ?? "")}）的任何记录：尾部读取有界，账本可能最近确实没有记录，也可能该 id 对应的账本文件缺席——两者无法据此区分；逐次明细不可得，不以空表冒充零记录。</p></div>`;
    }
    const hourKey = drill.hour.slice(0, 13);
    const rows = sourceRows.filter(
      (e) =>
        e.model === drill.model && this.beijingHourKeyOf(e.time) === hourKey
    );
    const dedupeNote = found.duplicates.length
      ? `；该来源的账本以 ${escapeHtml(found.covered.join(" / "))} 多个 id 注册（同一账本），逐次明细只取 ${escapeHtml(found.spelling ?? "")} 一份，不按 id 重复计数`
      : "";
    if (!rows.length) {
      return `<div class="wb-rate-drill">${head}<p class="wb-muted">该来源在本次读取的日志尾部有 ${sourceRows.length} 条记录，但未覆盖该时段（尾部读取有界）${dedupeNote}；逐次明细不可得，不代表该时段没有请求。</p></div>`;
    }
    return `<div class="wb-rate-drill">${head}<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>时间（账本原值）</th><th>模型</th><th class="wb-fee-num">输出 token</th><th class="wb-fee-num">耗时</th><th class="wb-fee-num">本请求 Token/s</th></tr></thead><tbody>${rows
      .slice(0, 60)
      .map(
        (e) =>
          `<tr><td>${escapeHtml((e.time || "").slice(5, 19).replace("T", " "))}</td><td>${escapeHtml(e.model || "—")}</td><td class="wb-fee-num">${this.numText(e.output_tokens)}</td><td class="wb-fee-num">${this.durationText(e.duration_ms)}</td><td class="wb-fee-num">${this.logRateText(e.output_tokens, e.duration_ms)}</td></tr>`
      )
      .join(
        ""
      )}</tbody></table></div><p class="wb-muted">逐次明细来自网关账本尾部读取（最多 ${LOG_FEED_LIMIT} 条，每来源 ${LOG_FEED_PER_SOURCE} 条的有界窗口），未覆盖的请求不在表内；时间列为账本写入的原值（国内链为 +08:00，国际链为 UTC），时段按北京时间整点比较（国际链的 UTC 时间戳已换算后匹配）${dedupeNote}；「未采集」= 账本未写该分量（后端以 null 披露），不按 0 显示。</p></div>`;
  }
  /** F2: the log lane's per-request rate cell. A missing component arrives
   *  as null from /api/console/logs (未采集), so the cell says 未采集 instead
   *  of computing a 0 Token/s out of a hole; a non-positive duration is —
   *  (no measurable wall time). A real measured 0 output with a valid duration
   *  still renders 0.00 — that 0 is a reading, not a hole. */
  private logRateText(outputTokens: unknown, durationMs: unknown): string {
    if (
      typeof outputTokens !== "number" ||
      !Number.isFinite(outputTokens) ||
      typeof durationMs !== "number" ||
      !Number.isFinite(durationMs)
    )
      return "未采集";
    if (durationMs <= 0) return "—";
    return this.rateText(this.requestRate(outputTokens, durationMs));
  }
  /** Whole-request average output rate of ONE ledger row: output tokens
   *  over that request's own wall time (first-token wait included). A
   *  missing output number or a missing / non-positive duration is 未知 —
   *  never 0, which would read as a real measured stall. */
  private requestRate(
    outputTokens: unknown,
    durationMs: unknown
  ): number | null {
    if (typeof outputTokens !== "number" || !Number.isFinite(outputTokens))
      return null;
    if (
      typeof durationMs !== "number" ||
      !Number.isFinite(durationMs) ||
      durationMs <= 0
    )
      return null;
    return outputTokens / (durationMs / 1000);
  }
  private rateText(rate: number | null): string {
    return rate === null ? "—" : rate.toFixed(2);
  }
  private numText(value: unknown): string {
    return typeof value === "number" && Number.isFinite(value)
      ? value.toLocaleString("zh-CN")
      : "—";
  }
  private durationText(value: unknown): string {
    return typeof value === "number" && Number.isFinite(value) && value > 0
      ? `${(value / 1000).toFixed(1)}s`
      : "—";
  }
  /** A contract count that the backend may omit: an absent count is 未知,
   *  never a fabricated 0. */
  private countText(value: number | undefined): string {
    return typeof value === "number" && Number.isFinite(value)
      ? value.toLocaleString("zh-CN")
      : "未知";
  }
  /** "YYYY-MM-DDTHH:00:00+08:00" → "MM-DD HH:00" for tables and heads. */
  private hourLabel(hour: string): string {
    return hour.length >= 16 ? hour.slice(5, 16).replace("T", " ") : hour;
  }
  /** R9-R34: model-lane overview via the local passthrough endpoint (same
   *  shape as the model.sync_pending action; no operation row, no session).
   *  Failures land in an honest error line, never in fixture data.
   *  维护者 2026-09-28：失败原因取自**真实请求状态**——传输失败 / HTTP 非
   *  2xx（带实测状态）/ 形状错误分开记；不再用 `!sessionLive()` 推断
   *  「8001 不可达」，也不再写「受管状态损坏」这种没测过的结论。
   *  `afterApply` = 应用写操作终态后的**自动回读**（PR9 模型同步闭环）：
   *  这一路失败时概览还挂着操作前的旧读数，必须留下 `pendingReadbackFailed`
   *  ——否则 `modelOverviewHtml` 里「操作已完成，最新状态读取失败（不重发写
   *  操作）」那一行没有任何写入者，永远不可达，旧读数也就冒充成本次结果。
   *  手动读口（进入车道 / 「重试读取」）只按本次真实结果清标记：读到才清，
   *  重试再失败时那一行仍然成立（不降级成「下方动作仍可手动触发」）。
   *  维护者 2026-09-29（同范围回读）：`scope` = 本次回读请求携带的范围
   *  （应用终态后用 `applyScopeParams`；入页/手动读口为 null）。请求带上与
   *  应用相同的范围参数，但后端读口当前不解析它（usagesrv.py:3209 给 handler
   *  传 `{}`），所以响应是全局概览——`modelOverviewHtml` 按实测写出这一点，
   *  不把它说成「已按范围回读」。 */
  private async fetchModelPending(
    afterApply = false,
    scope: Record<string, unknown> | null = null
  ) {
    this.modelOverviewState = "loading";
    // 记录本次回读请求的范围：读口失败行与同范围说明都要能说出请求的是哪一组
    // 范围（入页/手动读口不带范围 → null）。
    this.modelReadbackScope = scope;
    let readOk = false;
    try {
      const response = await fetch(
        `/gw/usagesrv/api/console/model-pending${this.scopeQuery(scope)}`,
        { credentials: "include" }
      );
      if (!response.ok) {
        this.modelOverviewState = "http";
        this.modelOverviewStatus = response.status;
      } else {
        const data = (await response.json().catch(() => null)) as Record<
          string,
          unknown
        > | null;
        if (!data || typeof data !== "object") {
          this.modelOverviewState = "shape";
        } else {
          this.modelOverviewState = "live";
          this.actionResults["model.sync_pending"] = data;
          readOk = true;
        }
      }
    } catch {
      this.modelOverviewState = "transport";
    }
    if (readOk) this.pendingReadbackFailed = false;
    else if (afterApply) this.pendingReadbackFailed = true;
    this.renderPanel();
  }
  /** Week-over-previous-day delta chip from the stats endpoint days (log
   *  lane). Neutral amber for down — fewer requests is a signal, not a
   *  failure; no chip without a comparable base day. */
  private weekDeltaHtml(): string {
    // 缺字段不按 0：最后两天里任一天没有请求数字段就明确写「不可比较」，
    // 绝不把「没读到」当成「那天是 0 请求」再算一个假百分比。
    const days = this.statsWeek?.days ?? [];
    const requests = days.map((d) => d.day_total?.requests);
    if (requests.length < 2) return "";
    const last = requests[requests.length - 1];
    const prev = requests[requests.length - 2];
    if (
      typeof last !== "number" ||
      !Number.isFinite(last) ||
      typeof prev !== "number" ||
      !Number.isFinite(prev)
    ) {
      return `<span class="wb-delta">较前日不可比较（该日请求数未提供）</span>`;
    }
    if (!prev) return `<span class="wb-delta">较前日无基数（前日 0）</span>`;
    const pct = ((last - prev) / prev) * 100;
    return Math.abs(pct) < 0.05
      ? ""
      : `<span class="wb-delta ${pct > 0 ? "up" : "down"}">${pct > 0 ? "▲" : "▼"} ${Math.abs(pct).toFixed(1)}% 较前日</span>`;
  }
  /** Ledger status → liveness dot (R9-R32): "ok"/2xx green, 4xx/5xx/error
   *  red, anything else = 未定 (hollow ring). 契约 3.5 的形状词汇里没有「实心
   *  琥珀圆」这一档：琥珀只作**陈旧**（半填充）。原先「其他状态」借用实心
   *  琥珀圆，与进行中的操作同形同色，两个不同含义因此共用一个形状。 */
  private statusDotHtml(status: string | null | undefined): string {
    const s = (status || "").toLowerCase();
    if (!s) return "";
    if (s === "ok" || s.startsWith("2"))
      return `<i class="wb-dot wb-dot-run" aria-hidden="true"></i>`;
    if (
      s.startsWith("4") ||
      s.startsWith("5") ||
      s.includes("error") ||
      s.includes("fail")
    )
      return `<i class="wb-dot wb-dot-down" aria-hidden="true"></i>`;
    return `<i class="wb-dot wb-dot-unknown" aria-hidden="true"></i>`;
  }
  private sparkSvg(values: number[]): string {
    const vals = values.slice(-7);
    const max = Math.max(...vals, 1);
    const bw = 6,
      gap = 3,
      h = 28;
    const rects = vals
      .map((v, i) => {
        const bh = Math.max(2, Math.round((v / max) * (h - 2)));
        const on = v > 0;
        return `<rect x="${i * (bw + gap) + 2}" y="${h - bh}" width="${bw}" height="${bh}" rx="2" fill="${on ? "var(--theme-accent)" : "var(--theme-line)"}" opacity="${on ? 0.9 : 0.55}"/>`;
      })
      .join("");
    return `<svg class="wb-spark" width="${vals.length * (bw + gap) + 2}" height="${h}" aria-hidden="true">${rects}</svg>`;
  }
  /** A separate focusable hit target for each calendar day provides the exact
   *  value on pointer hover and keyboard focus; a day with no value never connects,
   *  and `unknownDays[i]` keeps 「有记录但分量未知」 from being labelled
   *  「无记录」 — all the null states break the line, only the last one is a
   *  missing day.
   *  `partialDays[i]`（维护者 2026-09-28）：该日有值但只含可折算记录——值是
   *  下界，画空心点 / 虚线段，tooltip 写「已知部分 ≥…（当日含不可折算记录）」，
   *  绝不冒充完整费用。
   *  `boundOnlyDays[i]`（维护者 2026-09-28 ②）：该日有账本记录、金额只含可
   *  折算记录且可折算为空——与链级 costAmount(null, true) 同一句话，tooltip 写
   *  「未知（下界仅含可折算部分…）」，不画 0、也不画数值下界（值为 null，
   *  因此不落点、断线）。 */
  private areaSvg(
    values: (number | null)[],
    labels: string[],
    unit: "cny" | "credits",
    width = 640,
    height = 190,
    unknownDays: boolean[] = [],
    partialDays: boolean[] = [],
    boundOnlyDays: boolean[] = []
  ): string {
    const n = values.length;
    const axis = (label: string) => label.slice(5).replace("-", "/");
    width = timeAxisWidth(labels.map(axis), width);
    const h = height,
      padT = 20,
      padB = 25,
      padX = 12;
    const max = Math.max(...values.filter((v): v is number => v !== null), 1);
    const x = (i: number) =>
      padX + (i / Math.max(n - 1, 1)) * (width - padX * 2);
    const y = (v: number) => padT + (1 - v / max) * (h - padT - padB);
    // One format per unit (contract 3.8): the same ¥ / 积分 helpers the KPI
    // and the ledger tables use, so one screen cannot show a money value
    // without thousands separators next to a count that has them.
    const format = (v: number) =>
      unit === "cny"
        ? v > 0 && v < 0.0005
          ? "<¥0.001"
          : fmtCny(v)
        : `${fmtCredits(v)} 积分`;
    const segments: number[][] = [];
    values.forEach((v, i) => {
      if (v === null) return;
      if (i === 0 || values[i - 1] === null) segments.push([]);
      segments.at(-1)!.push(i);
    });
    const paths = segments
      .map((indices) => {
        const line = indices
          .map(
            (i, j) =>
              `${j ? "L" : "M"}${x(i).toFixed(1)},${y(values[i]!).toFixed(1)}`
          )
          .join(" ");
        const fill =
          indices.length > 1
            ? `<path d="${line} L${x(indices.at(-1)!).toFixed(1)},${h - padB} L${x(indices[0]).toFixed(1)},${h - padB} Z" class="wb-area-fill"/>`
            : "";
        // 含下界日的段画虚线（.wb-area-line-partial），与完整段（实线）分开。
        const partial = indices.some((i) => partialDays[i] === true);
        return `${fill}<path d="${line}" class="wb-area-line${partial ? " wb-area-line-partial" : ""}"${partial ? ` style="stroke-dasharray:3 3"` : ""}/>`;
      })
      .join("");
    const baselineMarkers = values
      .map((v, i) => {
        const kind = chartBaselineKind(v);
        return kind
          ? `<line class="wb-chart-baseline-${kind}" x1="${x(i).toFixed(1)}" x2="${x(i).toFixed(1)}" y1="${h - padB - 8}" y2="${h - padB}" style="stroke:var(--theme-muted)" aria-hidden="true"/>`
          : "";
      })
      .join("");
    const points = segments
      .filter((indices) => indices.length === 1)
      .map((indices) => {
        const i = indices[0];
        // 下界日画空心点（内色 = 当前主题面板色 + accent 描边），完整日点保持
        // .wb-area-single。维护者 2026-09-28（PR8 遗留 ② / Plan-Revision 9 费用页）：
        // 内色必须取自当前主题的面板色令牌，不得写死浅色值——深色主题下写死 #ffffff
        // 会在深色面板上留一个浅色实心点，与「空心」形制自相矛盾；用令牌后两个主题
        // 各自取到自己的面板色，写死同一个值即判红。
        return partialDays[i] === true
          ? `<circle cx="${x(i).toFixed(1)}" cy="${y(values[i]!).toFixed(1)}" r="2.7" class="wb-area-partial" style="fill:var(--theme-panel);stroke:var(--theme-accent)" stroke-width="1.5"/>`
          : `<circle cx="${x(i).toFixed(1)}" cy="${y(values[i]!).toFixed(1)}" r="2.7" class="wb-area-single"/>`;
      })
      .join("");
    const hotWidth = (width - padX * 2) / Math.max(n - 1, 1);
    const hotspots = values
      .map((v, i) => {
        const cx = x(i);
        // null 的两种状态各写各的（维护者 2026-09-28 ②）：只含可折算记录且可折算
        // 为空的日子与链行同写「未知（下界仅含可折算部分）」，绝不折 0 或写数值下界。
        const value =
          v === null
            ? boundOnlyDays[i] === true
              ? "未知（下界仅含可折算部分，当日有账本记录）"
              : unknownDays[i]
                ? "分量未知（当日有账本记录）"
                : "无记录"
            : partialDays[i] === true
              ? `已知部分 ≥${format(v)}（当日含不可折算记录）`
              : format(v);
        const date = labels[i] ?? "";
        const tip = this.areaTip([
          { text: date, value: false },
          { text: value, value: true },
        ]);
        const tipX = Math.min(
          Math.max(cx - tip.width / 2, 2),
          Math.max(2, width - tip.width - 2)
        );
        const dot =
          v === null
            ? ""
            : `<circle cx="${cx.toFixed(1)}" cy="${y(v).toFixed(1)}" r="4" class="wb-area-focus-dot"/>`;
        return `<g class="wb-area-hotspot" tabindex="0" role="img" aria-label="${escapeHtml(date)}：${escapeHtml(value)}">
        <rect x="${(cx - hotWidth / 2).toFixed(1)}" y="0" width="${hotWidth.toFixed(1)}" height="${h - padB}" class="wb-area-hit"/>
        <line x1="${cx.toFixed(1)}" x2="${cx.toFixed(1)}" y1="${padT}" y2="${h - padB}" class="wb-area-crosshair"/>${dot}
        <g class="wb-area-tip" transform="translate(${tipX.toFixed(1)},1)">${tip.plate}</g>
      </g>`;
      })
      .join("");
    const ref = this.trendRef(values, x, y, padX, width, partialDays);
    // Gridline values carry just enough decimals for their range (the exact
    // reading is in the tooltip); an empty chart gets no scale at all.
    const digits = max >= 100 ? 0 : max >= 10 ? 1 : 2;
    const tick = (v: number) =>
      `${unit === "cny" ? "¥" : ""}${v.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
    const hasData = values.some((v) => v !== null);
    const stats = this.chartStatsHtml({
      values,
      labels,
      axis,
      format,
      unit: unit === "cny" ? "人民币（估算费用）" : "Qoder 积分",
      totalLabel: "区间合计",
      partialValues: partialDays,
      scope: "日",
    });
    const grid = this.gridLines(
      max,
      padX,
      width,
      y,
      hasData ? tick : undefined
    );
    return `${stats}<div class="wb-chart-scroll"><svg class="wb-area" style="min-width:${width}px" viewBox="0 0 ${width} ${h}" role="group" aria-label="近 ${n} 天趋势，逐日悬停或按 Tab 查看数值">
      ${grid.lines}
      ${paths}${baselineMarkers}${points}${ref.line}${grid.labels}
      ${hotspots}
      ${this.axisTicks(labels, axis, x, h, width)}
    </svg></div>`;
  }
  /** R9-R40 task 3: fee-lane SVG summary — the billed chains' daily cost
   *  as a compact area (same hand-drawn zero-dependency SVG recipe as the
   *  overview), the Qoder credit chain summarized separately since credit
   *  and ¥ never mix into one scale. No chart junk, honest empty states.
   *  下界日（costCnyPartial）画空心点 / 虚线段，段说明里单独计数——
   *  区间合计与末值也只写下界，绝不冒充完整费用。 */
  private feeTrendHtml(): string {
    const cost = this.trendDays("costCny", 14);
    const credits = this.trendDays("credits", 14);
    // boundOnly（有记录、金额只含可折算记录且可折算为空）也有账本记录，只是没有可画
    // 的值：只按 unknown 判会把「整条序列都是这种日子」的车道整段藏掉（维护者 ②）。
    const hasCost = cost.values.some(
      (v, i) => v !== null || cost.unknown[i] || cost.boundOnly[i]
    );
    const hasCredits = credits.values.some(
      (v, i) => v !== null || credits.unknown[i]
    );
    const partialCostDays = cost.partial.filter(Boolean).length;
    const boundOnlyCostDays = cost.boundOnly.filter(Boolean).length;
    // 维护者 2026-09-28（Plan-Revision 9 逐页收纳 · 费用页）：趋势图区高按提案值落到
    // 260–300px（原 150/190 的图在整页里读不出日级趋势）。并排两张各 260、单张 280，
    // 仍在 260–300 内；Y 轴仍无数值刻度，只有 4 条虚线网格，tooltip 与下界形制不变。
    const chart = (
      series: typeof cost,
      unit: "cny" | "credits",
      title: string
    ) =>
      `<div class="wb-fee-trend" data-wb-series="${unit}"><h4>${title}</h4>${this.areaSvg(series.values, series.labels, unit, hasCost && hasCredits ? 640 : 1000, hasCost && hasCredits ? 260 : 280, series.unknown, series.partial, series.boundOnly)}</div>`;
    const charts = [
      chart(cost, "cny", "计费链 · ¥"),
      chart(credits, "credits", "Qoder · 积分"),
    ].join("");
    const missing = [
      !hasCost ? "计费链" : "",
      !hasCredits ? "Qoder 积分链" : "",
    ].filter(Boolean);
    // 2026-09-29: the legend explanation folds under the charts; the day counts
    // stay in the lane's resident strip, and a chain with no ledger rows keeps
    // its own resident line (无法判定为零用量).
    const legend = `<p class="wb-muted">每日原值；无记录、「有记录但分量未知」与「有记录、金额只含可折算记录且可折算为空」的日期都断线并各自标注，账本记录的 0 正常显示；只含可折算记录的有值下界日画空心点 / 虚线段。费用与积分各用自己的刻度，不折算。${boundOnlyCostDays ? `本窗口计费链有 ${boundOnlyCostDays} 天下界日（下界为空，断线）。` : ""}${partialCostDays ? `本窗口计费链有 ${partialCostDays} 天有值下界日（空心点 / 虚线段，区间合计与末值按下界写出），不冒充完整费用。` : ""}</p>`;
    // PR11 (Q20): the fee lane's main chart sits on the charcoal surface; the fee
    // series is lemon and the credits series violet (colour follows the entity).
    return (
      `<section class="wb-block wb-char"><h3>近 14 天趋势</h3>` +
      (charts
        ? `<div class="wb-fee-trends${hasCost && hasCredits ? "" : " wb-fee-trends-single"}">${charts}</div>`
        : "") +
      (missing.length
        ? `<p class="wb-muted wb-fee-trend-missing">${missing.join("、")}近 14 天无账本记录；无法判定为零用量。</p>`
        : "") +
      this.laneNoteFoldHtml("fee-trend", "图例与断线口径", legend) +
      `</section>`
    );
  }
  /** 「本期 vs 上期」 (contract 3.9 `wb-delta-bar` / `wb-sim-chip`, 4.5 main
   *  area). One row per first-screen reading over the SAME real 14-day ledger
   *  window the trend charts read: 本期 = the last 7 days, 上期 = the 7 days
   *  before them. The bar's two segments are PROPORTIONS of the two printed
   *  values — a relative shape, not a third scale — so EACH bar carries its own
   *  `wb-sim-chip` (the contract forbids marking only one of them). A component
   *  the ledger rows do not carry stays 不可得 instead of a 0, and the delta chip
   *  prints the two real values so no percentage is ever read alone.
   *  费用行下界（维护者 2026-09-28 / OD 决定稿）：任一侧 partial 时整行两侧都写成
   *  ≥¥…（整体标 partial），绝不出现不带 ≥ 的区间合计；环比由 deltaChip 判定，
   *  不给无条件精确涨跌。 */
  private periodCompareHtml(): string {
    const rows: {
      label: string;
      field: "requests" | "costCny" | "credits";
      format: (v: number) => string;
      bound: (v: number, partial: boolean) => string;
    }[] = [
      {
        label: "请求数",
        field: "requests",
        format: fmtRequests,
        bound: fmtRequests,
      },
      {
        label: "估算费用",
        field: "costCny",
        format: fmtCny,
        bound: (v, partial) => (partial ? `≥${fmtCny(v)}` : fmtCny(v)),
      },
      {
        label: "积分",
        field: "credits",
        format: fmtCredits,
        bound: fmtCredits,
      },
    ];
    const body = rows
      .map((row) => {
        const pair = this.windowPair(row.field);
        if (!pair) {
          return `<div class="wb-cred-row"><span class="wb-cred-k">${row.label}</span><span class="wb-cred-v">本期 / 上期 不可得（近 14 天账本日不足或该分量未记录，不折成 0）</span></div>`;
        }
        const max = Math.max(pair.now, pair.prev, 1);
        // No minimum width: a real 0 renders as a zero-width segment on the
        // track (the track itself stays visible), never as a fabricated sliver.
        const nowWidth = Math.max(0, (pair.now / max) * 100).toFixed(1);
        const prevWidth = Math.max(0, (pair.prev / max) * 100).toFixed(1);
        const anyPartial = pair.nowPartial || pair.prevPartial;
        return (
          `<div class="wb-cred-row"><span class="wb-cred-k">${row.label}</span><span class="wb-cred-v">` +
          `本期 ${escapeHtml(row.bound(pair.now, anyPartial))} · 上期 ${escapeHtml(row.bound(pair.prev, anyPartial))} ${this.deltaChip(pair)}` +
          `<span class="wb-delta-bar" aria-hidden="true"><i style="width:${prevWidth}%"></i><i style="width:${nowWidth}%"></i></span>` +
          `<small class="wb-muted" title="差值条：上期 / 本期按打印值的比例示意（不按刻度读数）">上期 / 本期比例 <span class="wb-sim-chip">示意</span></small>` +
          `</span></div>`
        );
      })
      .join("");
    return (
      `<section class="wb-block wb-period"><h3>本期 vs 上期</h3>` +
      `<div class="wb-period-rows">${body}</div>` +
      this.laneNoteFoldHtml(
        "period",
        "窗口口径",
        `<p class="wb-muted">窗口：近 14 天账本（本期 = 最近 7 天，上期 = 再往前 7 天）；逐日原值见趋势图，缺失日不按 0 计。任一侧含下界日（只含可折算记录）时，本期 / 上期两侧都写成 ≥¥…（整行标 partial），环比不再给无条件精确涨跌。</p>`
      ) +
      `</section>`
    );
  }
  /** Model usage ranking (Bar List, Rev12 F12-2): per-model Token (input + output)
   *  from the aggregated ledgers, largest first. A model with requests whose usage
   *  is unknown keeps its known Token and says 部分 (those requests are not
   *  counted); a model whose rows carry no Token at all reads 未知, not 0. */
  private barListHtml(top = 6): string {
    const rows = (this.summarySeries?.models ?? []).slice(0, top);
    if (!rows.length) return `<p class="wb-muted">账本暂无模型级记录。</p>`;
    const max = Math.max(...rows.map((m) => m.tokens), 1);
    // 维护者 2026-09-28（问题 2 · 排行行名截断）：行名被省略号截断时，完整名称在
    // `title` 里（同一个名字，仍走 `escapeHtml`）；柱长与读数位置不变。
    const rowHtml = (m: ModelPoint) => {
      const partial = m.unknownRequests > 0 || m.tokensMissing;
      const none = m.tokens === 0 && partial;
      const note =
        m.unknownRequests > 0
          ? `${fmtRequests(m.unknownRequests)} 次请求用量未知，未计入`
          : "部分记录没有 Token 字段，未计入";
      const value = none ? "未知" : fmtTokens(m.tokens);
      const mark =
        partial && !none
          ? ` <small class="wb-barlist-partial" title="${escapeHtml(note)}">部分</small>`
          : "";
      return (
        `<div class="wb-barlist-row"><span class="wb-barlist-name" title="${escapeHtml(m.model)}">${escapeHtml(m.model)}</span>` +
        `<span class="wb-barlist-bar"><i style="width:${none ? 0 : Math.max(2, (m.tokens / max) * 100).toFixed(1)}%"></i></span>` +
        `<span class="wb-barlist-val"${partial ? ` title="${escapeHtml(note)}"` : ""}>${value}${mark}</span></div>`
      );
    };
    return `<div class="wb-barlist">${rows.map(rowHtml).join("")}</div>`;
  }
  /** Whether the remembered evidence has aged past HEALTH_STALE_AFTER_MS.
   *  Failed snapshot reads keep the last verdict on the card, so without this
   *  check an old reading would keep passing itself off as the present. */
  private probeStale(result: ProbeResult): boolean {
    return (
      result.observedAt !== undefined &&
      Date.now() - result.observedAt > HEALTH_STALE_AFTER_MS
    );
  }
  /** Liveness dot semantics (R20-06): the dot shows PROBE REACHABILITY
   *  only — no probe yet = unknown (grey), never a static default green.
   *  A 401 probe answer proves the HTTP entry responds (process up) but
   *  NOT that the account is valid; that split lives in the title evidence
   *  and the account-status action, not in the dot color. P1 (2026-09-24):
   *  the same grey covers evidence that could not decide (server-side
   *  connect timeout, or a snapshot that did not cover this service) — a
   *  "cannot decide" verdict is never painted red.
   *  P1 (2026-09-24) stale evidence: amber, never red. Red is a verdict
   *  ("confirmed unreachable"); aged evidence is the absence of a verdict
   *  ("cannot tell what the state is now") — painting it red would assert
   *  an outage nobody observed.
   *  Priority, deliberately: staleness is checked FIRST, so an unknown
   *  verdict that stops refreshing also ages into amber — grey means "no
   *  verdict is available now", and once the evidence is old that is exactly
   *  what it says. A fresh unknown stays grey. */
  private dotState(id: number): "unknown" | "run" | "down" | "stale" {
    const result = this.live.get(id);
    if (!result) return "unknown";
    // Contract 3.5 shape encoding: 陈旧 renders the HALF-FILLED amber ring
    // (`wb-dot-stale`), a different shape from the solid amber used for
    // in-flight operations — so 陈旧 can never be read as a live verdict,
    // and it is never red (陈旧 ≠ 宕机).
    if (this.probeStale(result)) return "stale";
    const state = result.state ?? (result.alive ? "alive" : "dead");
    if (state === "unknown") return "unknown";
    return state === "alive" ? "run" : "down";
  }
  /** Probe evidence line: the verdict, WHEN it was observed (local 24h clock,
   *  second precision — the stamp comes from the snapshot's checked_at, so it is
   *  the server's probe time) and, past HEALTH_STALE_AFTER_MS, the fact that
   *  the line shows the last successful reading rather than the present. */
  private probeEvidence(id: number) {
    const result = this.live.get(id);
    if (!result) return "未探测";
    const suffix =
      result.status === 401 || result.status === 403
        ? "（鉴权应答：入口有响应；账号可用性未核）"
        : result.status && result.status >= 500
          ? "（服务错误或失联）"
          : "";
    const at = result.observedAt;
    const clock = new Date(at ?? 0);
    const hh = String(clock.getHours()).padStart(2, "0");
    const mm = String(clock.getMinutes()).padStart(2, "0");
    const ss = String(clock.getSeconds()).padStart(2, "0");
    const seen = at === undefined ? "" : ` · 检查于 ${hh}:${mm}:${ss}`;
    const stale = this.probeStale(result)
      ? " · 证据已过期（连续读取失败，显示的是上次成功结果）"
      : "";
    return `${result.note} ${result.ms}ms${suffix}${seen}${stale}`;
  }
  /** 手动重探的控件与它的实测状态：`GET /api/console/health?refresh=1` 是后端
   *  自己的手动重探路径（usagesrv.py:3051-3060 `_console_health_payload(force)`：
   *  force 时不读缓存快照而是重探一次，`cached` 随后为 False），所以这里不再
   *  写成「后端未提供」的禁用控件。若运行版本确实未载入该参数，按**实测 HTTP
   *  状态**显示「当前服务端版本未载入」，不沿用源码已证伪的泛化说法；失败保留
   *  上一次真实证据，不判宕机、不折成空表。
   *  2026-09-29：控件移到总览运行裁决的右侧；短行写本次重探的当前状态，完整句子
   *  （含这次读取写与不写什么）留在 `title`，裁决条保持一行，内容一条不少。 */
  private healthRefreshHtml(): string {
    const state = this.healthRefresh;
    const at = (epoch?: number) =>
      epoch
        ? new Date(epoch * 1000).toLocaleTimeString("zh-CN", { hour12: false })
        : "时点未知";
    const [short, full] = !state
      ? [
          "服务端循环探测 · 30s",
          "健康快照平时由服务端循环探测并短 TTL 缓存；手动重探是一次有界只读 GET，不产生操作记录、不写任何状态。",
        ]
      : state.state === "busy"
        ? [
            "重探中…",
            "手动重探中…（一次有界只读 GET，不产生操作记录、不写任何状态）",
          ]
        : state.state === "ok"
          ? [
              `已重探 ${at(state.at)}${state.cached ? "（服务端缓存）" : ""}`,
              `已重探：服务端于 ${at(state.at)} 重新探测（本次${state.cached ? "仍命中服务端缓存" : "为真实重探"}）；证据与检查时点已更新。`,
            ]
          : state.state === "unavailable"
            ? [
                `重探未载入（HTTP ${state.status}）`,
                `当前服务端版本未载入：GET /api/console/health?refresh=1 实测 HTTP ${state.status}（该参数被忽略或路由未载入）；健康快照仍由服务端循环探测提供。`,
              ]
            : [
                `重探失败${state.status ? `（HTTP ${state.status}）` : ""}`,
                `手动重探失败：${state.reason}${state.status ? `（HTTP ${state.status}）` : ""}；保留上一次真实证据，不判宕机、不折成空表。`,
              ];
    return `<div class="wb-verdict-ops" title="${escapeHtml(full)}"><small class="wb-verdict-probe" data-state="${state?.state ?? "idle"}">${escapeHtml(short)}</small><button type="button" class="wb-btn" data-wb-health-refresh="1" aria-label="手动重探健康快照"${state?.state === "busy" ? " disabled" : ""}>${wbIcon("refresh")}<span>重探</span></button></div>`;
  }
  /** 账号卡动作分级（账号页）：① 主动作 = 该链的只读认证/登录状态查询（一次点击
   *  直达，无副作用）；② 次动作 = 其余只读查询与登录窗口；③ 需人工登录的动作
   *  （`*.login`）单独一级并写明「由维护者本人完成登录」，点击后仍先二次确认。
   *  没有只读查询动作的链如实写明「此来源未提供账号管理接口」，不画死按钮。
   *  维护者 2026-09-28（无障碍口径）：末尾的纯图标按钮已有 `aria-label`
   *  （辅助技术读到的名称）与 `.wb-icon-hint` 提示板（`:hover` / `:focus-visible`
   *  各显示一次），所以**不加**原生 `title`——原生 title 与提示板会先后出现，
   *  是双重悬停提示；缺原生 title 本身不等于无障碍失败。 */
  /** A chain's read-only login query, if this backend offers it: a `gated` one
   *  counts only once the action registry lists it enabled (the console never
   *  sends an action the running backend does not register). */
  private statusActionOf(s: ServiceRow): ServiceRow["statusAction"] {
    const status = s.statusAction;
    if (!status?.gated) return status;
    const row = this.actions?.find((a) => a.action === status.action);
    return row?.enabled ? status : undefined;
  }
  /** Why a chain has no login query right now, one short phrase plus a sentence. */
  private statusGap(s: ServiceRow): { word: string; why: string } {
    const status = s.statusAction;
    if (!status) return { word: "不支持查询", why: "该链未提供只读认证接口" };
    if (!this.actions)
      return {
        word: "登录查询待确认",
        why: "动作注册表尚未读到，暂不提交查询",
      };
    const row = this.actions.find((a) => a.action === status.action);
    if (!row)
      return {
        word: "登录查询尚未接入",
        why: `当前后端版本未注册 ${status.action}`,
      };
    return {
      word: "登录查询已停用",
      why: row.disabled_reason || `${status.action} 已停用`,
    };
  }
  /** Rev16 A16-1: the CLIProxy login-registration read (`cliproxy.status`) as one
   *  short reading. Only provider-matched (Gemini / Antigravity) entries carry the
   *  Gemini login fact; `observed_ok` is the last runtime refresh, not "valid now";
   *  a partial failure says 部分失败 with its counts, never 全部掉线; a refused
   *  management read is not a login state. */
  private cliproxyLoginReading(result: Record<string, unknown>): {
    text: string;
    sub: string;
    cls: string;
    plain: string;
  } {
    const summary = (result.summary ?? {}) as Record<string, unknown>;
    const n = (k: string) =>
      typeof summary[k] === "number" ? (summary[k] as number) : 0;
    const matched = n("provider_matched_total"),
      ok = n("observed_auth_ok"),
      failed = n("observed_auth_failed"),
      unknown = n("observed_auth_unknown");
    const accounts = Array.isArray(result.accounts)
      ? (result.accounts as Record<string, unknown>[])
      : [];
    const stale = accounts.some(
      (a) =>
        a.provider_match === true &&
        ((a.observed_upstream_auth ?? {}) as Record<string, unknown>).stale ===
          true
    );
    const staleNote = stale ? " · 部分观测已过阈值（陈旧≠失效）" : "";
    const counts = `成功 ${ok} · 失败 ${failed} · 未知 ${unknown}`;
    if (summary.partial_failure === true) {
      return {
        text: "部分失败",
        sub: `${counts}（${matched} 个匹配账号）`,
        cls: "warn",
        plain: `部分失败：${matched} 个 Gemini/Antigravity 账号中 ${counts}；不是全部掉线${staleNote}`,
      };
    }
    const verdict = summary.verdict,
      reason = typeof summary.reason === "string" ? summary.reason : "";
    if (verdict === "observed_ok") {
      // A stale observation is not a current pass: no green, and the sidebar
      // counts it as 未知 rather than 已认证 (stale is not a failure either).
      return stale
        ? {
            text: "最近刷新正常（已陈旧）",
            sub: `${matched} 个匹配账号${staleNote}`,
            cls: "idle",
            plain: `最近一次运行时刷新正常，但观测已过阈值（${matched} 个匹配账号；陈旧≠失效，也不代表此刻有效）`,
          }
        : {
            text: "最近刷新正常",
            sub: `${matched} 个匹配账号`,
            cls: "ok",
            plain: `最近一次运行时刷新正常（${matched} 个匹配账号；只代表最近一次刷新，不代表此刻有效）`,
          };
    }
    if (verdict === "observed_failed") {
      return {
        text: "认证失效",
        sub: `${failed} 个匹配账号失败`,
        cls: "danger",
        plain: `认证失效：${matched} 个匹配账号中 ${failed} 个最近一次刷新失败${staleNote}`,
      };
    }
    const words: Record<string, [string, string]> = {
      no_matching_provider_accounts: [
        "无 Gemini 账号",
        `只有其他来源账号 ${n("other_provider_total")} 个、来源不明 ${n("provider_unknown_total")} 个，不计作 Gemini 登录证据`,
      ],
      no_accounts: ["未登记账号", "管理面账号列表为空；不据此推断任何认证状态"],
      management_read_denied: [
        "管理面读取被拒",
        "只表示管理面读取被拒，与登录状态无关",
      ],
      management_shape_unsupported: [
        "返回形状不符",
        "管理面响应形状不符，账号事实未知",
      ],
      runtime_list_unavailable: [
        "运行时状态未知",
        "只证明账号登记在场，运行时认证状态未知",
      ],
      accounts_truncated: [
        "账号过多已截断",
        `只报告前 ${n("accounts_reported")} 个（共 ${n("accounts_total")} 个），不下总体结论`,
      ],
      no_refresh_observed: [
        "未观测到刷新",
        `${counts}；尚未观测到可下结论的刷新`,
      ],
    };
    const [text, why] = words[reason] ?? [
      "登录状态未知",
      reason ? `原因码 ${reason}` : "返回未给出结论",
    ];
    return {
      text,
      sub: why,
      cls: "idle",
      plain: `${text}：${why}${staleNote}`,
    };
  }
  private accountActionsHtml(s: ServiceRow): string {
    // 主按钮 = 下一主动作（`accountNextLogin`）：通常是该链的只读状态查询；真实返回
    // 说未认证时换成登录动作（仍先二次确认）。其余都是次级按钮。
    const nextLogin = this.accountNextLogin(s)?.action;
    const tier = (primary: boolean) =>
      primary ? "wb-btn wb-action-primary" : "wb-btn";
    const statusAction = this.statusActionOf(s);
    const gap = this.statusGap(s);
    const status = statusAction
      ? `<button class="${tier(!nextLogin)}" data-wb-acct-action="${statusAction.action}" data-wb-acct-target="${statusAction.target}" title="经 8001 适配层提交只读账号查询（无副作用，不产生计费请求）">${escapeHtml(statusAction.label)}</button>`
      : `<span class="wb-muted">${escapeHtml(s.statusAction ? `${gap.word}：${gap.why}` : "此来源未提供账号管理接口")}</span>`;
    const manage = (s.manageActions ?? [])
      .map(
        (a) =>
          `<button class="${tier(a.action === nextLogin)}" data-wb-acct-action="${a.action}" data-wb-acct-target="${a.target}" title="${escapeHtml(a.title)}">${escapeHtml(a.label)}</button>`
      )
      .join("");
    return status + manage;
  }
  /** The main chain's verdict word and its dot state, shared by the overview
   *  verdict and the console-wide head status so the two can never disagree.
   *  No evidence yet stays 未探测 (hollow ring), never a default green. */
  private mainVerdict(): {
    word: string;
    dot: "run" | "down" | "stale" | "unknown";
  } {
    const has = this.live.has(MAIN_CHAIN_ID);
    const state = this.dotState(MAIN_CHAIN_ID);
    if (!has) return { word: "主链未探测", dot: "unknown" };
    if (state === "run") return { word: "主链可用", dot: "run" };
    if (state === "down") return { word: "主链未监听", dot: "down" };
    if (state === "stale") return { word: "主链证据已过期", dot: "stale" };
    return { word: "主链探测未定", dot: "unknown" };
  }
  /** Overview verdict (contract 4.2, the lane's first visual focus): ONE sentence
   *  answering 「现在能不能用」, derived from the SAME probe evidence the service
   *  cards render (`this.live` + `probeStale`). No evidence yet is stated as
   *  such — never defaulted to 可用; stale evidence only lowers freshness and is
   *  never reported as a dead chain; `unknown` stays 未定, never a fabricated
   *  green. */
  private verdictHtml(): string {
    const rows = CHAINS.map((s) => ({
      has: this.live.has(s.id),
      state: this.dotState(s.id),
    }));
    const mainWord = this.mainVerdict().word;
    const down = rows.filter((r) => r.state === "down").length;
    const unknown = rows.filter((r) => r.has && r.state === "unknown").length;
    const stale = rows.filter((r) => r.state === "stale").length;
    const probeless = rows.filter((r) => !r.has).length;
    const need = down + unknown + stale;
    const needDetail = [
      down ? `未监听 ${down}` : "",
      unknown ? `探测未定 ${unknown}` : "",
      stale ? `证据过期 ${stale}` : "",
    ]
      .filter(Boolean)
      .join(" · ");
    const probelessNote = probeless
      ? `；另有 ${probeless} 条尚无探针证据（不判宕机）`
      : "";
    const note =
      probeless === rows.length
        ? `${CHAIN_COUNT_WORD}链尚无探针证据（8001 健康快照未返回）：不判「正常」，也不判「宕机」。`
        : need
          ? `${CHAIN_COUNT_WORD}链中 <b>${need}</b> 条需要处理（${needDetail}）；其余 ${rows.length - need - probeless} 条监听中${probelessNote}。`
          : `${CHAIN_COUNT_WORD}链全部监听中${probelessNote}。`;
    // PR11: the verdict chip carries a shape per state beside the word (check /
    // alert / clock / search), so the state never rests on the chip's colour.
    const dot = this.mainVerdict().dot;
    const icon =
      dot === "run"
        ? "check"
        : dot === "down"
          ? "alert"
          : dot === "stale"
            ? "clock"
            : "search";
    // 2026-10-31: the verdict is the overview's one hero card — its tone follows
    // the main-chain state, the icon tile repeats the state as a shape.
    return `<div class="wb-verdict" data-wb-key="verdict" data-state="${dot}"><span class="wb-verdict-icon" aria-hidden="true">${wbIcon(icon)}</span><div class="wb-verdict-text"><div class="wb-verdict-value">${escapeHtml(mainWord)}</div><p class="wb-verdict-note">${note}</p></div>${this.healthRefreshHtml()}</div>`;
  }
  /** The attention word of one chain on the overview's 服务 list, or null for a
   *  clean 监听中 (contract 4.2 / 3.9, formerly the separate 「需要处理」 queue).
   *  A chain with no evidence says so instead of disappearing, and a stale
   *  chain says 证据过期 rather than 未监听.
   *  P1/R1（2026-10-10）：用词结合**人工停止意图**（不再只按实时探测归组）：
   *  该链运行/停用门判 stopped（本页真实停止动作，或 8001 操作账本的终态启停
   *  记录）写「人工停用」——这是明确意图，不是故障，也不把 TCP 1.5s 超时一律
   *  当 stopped：探测未定的行仍写「探测未定」（未知 ≠ 已停止）。 */
  private attentionWord(
    s: ServiceRow
  ): { word: string; tone: "danger" | "warn" | "idle"; title: string } | null {
    const has = this.live.has(s.id);
    const state = this.dotState(s.id);
    if (has && state === "run") return null;
    const gate = this.upstreamReadGate(s.targets?.[0] ?? "");
    if (!has)
      return {
        word: "尚无探针证据",
        tone: "idle",
        title: "不判宕机、也不判正常，不折成 0",
      };
    if (gate.verdict === "stopped")
      return { word: "人工停用", tone: "idle", title: gate.detail };
    if (state === "down") return { word: "未监听", tone: "danger", title: "" };
    if (state === "stale")
      return {
        word: "证据过期",
        tone: "warn",
        title: "陈旧证据只降新鲜度，不判宕机",
      };
    return { word: "探测未定", tone: "idle", title: "" };
  }
  /** In-place probe evidence refresh on the probe-marked chips/rows (no full
   *  re-render): dot class + appended evidence on the stored base title. */
  private applyLive() {
    this.root.querySelectorAll<HTMLElement>("[data-wb-probe]").forEach((el) => {
      const id = Number(el.dataset.wbProbe);
      const result = this.live.get(id);
      if (!result) return;
      const dot = el.querySelector(".wb-dot");
      if (dot) dot.className = `wb-dot wb-dot-${this.dotState(id)}`;
      if (!el.dataset.wbBaseTitle)
        el.dataset.wbBaseTitle = el.getAttribute("title") ?? "";
      el.setAttribute(
        "title",
        `${el.dataset.wbBaseTitle} · ${this.probeEvidence(id)}`
      );
    });
  }
  /** Rev14 T1 (maintainer 2026-10-06: 整次平均输出速度): over the last 24 hours,
   *  Σ output tokens ÷ Σ whole-request duration (first-token wait included),
   *  weighted over every bucket the rate endpoint returns - never a mean of the
   *  hourly rates. The endpoint only emits buckets that have output tokens and a
   *  positive duration; records without them are counted in `skipped`, not in the
   *  quotient. No computable bucket is 无记录 or 不可计算 (the ledger did record
   *  requests), and a failed read is 未知 - never 0. */
  private tpsReading(): { text: string; state: string } {
    const read = this.usageRates;
    if (!read?.payload) {
      return read && read.state !== "loading"
        ? { text: "未知", state: `读取失败（${read.reason ?? "原因未知"}）` }
        : { text: "—", state: "读取中…" };
    }
    let out = 0,
      ms = 0;
    for (const r of read.payload.hours) {
      if (
        Number.isFinite(r.output_tokens) &&
        r.output_tokens >= 0 &&
        Number.isFinite(r.duration_ms) &&
        r.duration_ms > 0
      ) {
        out += r.output_tokens;
        ms += r.duration_ms;
      }
    }
    const stale = read.state === "live" ? "" : " · 本次刷新失败，为上次值";
    if (ms <= 0) {
      const recorded = Object.values(read.payload.recorded_requests ?? {});
      const some = recorded.some((v) => typeof v === "number" && v > 0);
      const allZero = recorded.length > 0 && recorded.every((v) => v === 0);
      return some
        ? { text: "不可计算", state: `有请求但缺输出 Token 或耗时${stale}` }
        : allZero
          ? { text: "无记录", state: `近 24 小时没有请求${stale}` }
          : {
              text: "未知",
              state: `近 24 小时没有可计算的分桶，账本覆盖未知${stale}`,
            };
    }
    const tps = out / (ms / 1000);
    return {
      text: tps.toLocaleString("zh-CN", {
        minimumFractionDigits: 1,
        maximumFractionDigits: 1,
      }),
      state: `近 24 小时 · ${fmtTokens(out)} 输出 Token${stale}`,
    };
  }
  private tpsKpiHtml(): string {
    const r = this.tpsReading();
    const formula =
      "整次平均输出速度 = 近 24 小时有效输出 Token 总和 ÷ 有效请求耗时总和（含首字等待）。按小时与模型分桶加权，不平均各小时速率；缺输出 Token 或耗时的请求不进分子与分母。";
    return (
      `<div class="wb-kpi wb-kpi-rate" title="${escapeHtml(formula)}"><small>${kpiChip("pulse", "tok")}整次平均输出速度 · Token/s</small><b data-kpi="tps">${escapeHtml(r.text)}</b>` +
      `<div class="wb-kpi-state"><small>${escapeHtml(r.state)}</small></div></div>`
    );
  }
  /** KPI numbers/captions update in place after a summary read; undefined
   *  KPI renders an honest em-dash, never a fixture. 费用格沿用与首屏同一段
   *  读数文本与同一个小字标记（partial 时 ≥¥… +「已知部分」），两条路径不可能漂移。 */
  private applySummary() {
    const values = this.kpi();
    this.root.querySelectorAll<HTMLElement>("[data-kpi]").forEach((el) => {
      const key = el.dataset.kpi;
      if (key === "cost") {
        const amount = values === undefined ? undefined : this.displayedCost();
        el.textContent = costAmountReading(amount);
        // 状态属性在格容器上：读数现在位于 `.wb-reading` 行内，不再是格的直接子元素，
        // 所以按属性往上找格，而不是取 parentElement（那会命中读数行）。
        const cell = el.closest<HTMLElement>("[data-wb-cost-state]");
        if (cell) {
          cell.dataset.wbCostState = costAmountState(amount);
          cell
            .querySelectorAll("[data-wb-cost-marker]")
            .forEach((marker) => marker.remove());
          el.insertAdjacentHTML("afterend", costAmountMarker(amount));
        }
        return;
      }
      // Readings that do not come from the summary keep their own sources.
      if (key === "quota") {
        el.textContent = this.quotaValueText();
        return;
      }
      if (key === "tps") {
        el.textContent = this.tpsReading().text;
        return;
      }
      if (values === undefined) {
        el.textContent = "—";
        return;
      }
      if (key === "tokens") el.textContent = fmtTokens(values.tokens);
      // The reading equals the sum of the rows printed beside it (contract 3.8).
      else if (key === "credits")
        el.textContent = this.displayedCredits() ?? "—";
    });
    // Two resident notes: the overview keeps the 累计口径 sentence alone, while
    // the fee lane's note additionally carries 缺失日 N and 价目核对 (contract
    // 4.5 — those two facts stay on the resident strip, never demoted into a
    // wb-info tooltip). Both are re-derived here because the series and the
    // price table they read land in the same pass as the summary.
    this.root
      .querySelectorAll<HTMLElement>("[data-wb-summary-note]")
      .forEach((el) => {
        el.textContent = this.summaryNote();
      });
    this.root
      .querySelectorAll<HTMLElement>("[data-wb-cost-note]")
      .forEach((el) => {
        el.textContent = this.summaryNote() + this.costResidentNote();
      });
  }
  private syncElements() {
    this.stage.dataset.workbenchBrand = String(
      !this.enabled || this.visibility.brand
    );
    this.stage.dataset.workbenchFooter = String(
      !this.enabled || this.visibility.footer
    );
    this.stage.dataset.workbenchSettings = String(
      !this.enabled || this.visibility.settings
    );
  }
  /** Head stamp (contract 4.2): WHEN the shown evidence was read, taken from
   *  the server health snapshot's own `checked_at` — never from the page's paint
   *  time, so a repaint cannot pass itself off as a fresh reading. With no
   *  snapshot at all the stamp says so instead of printing the local clock. */
  private headStamp(): string {
    let newest: number | undefined;
    for (const result of this.live.values()) {
      if (
        result.observedAt !== undefined &&
        (newest === undefined || result.observedAt > newest)
      ) {
        newest = result.observedAt;
      }
    }
    if (newest === undefined) return "数据检查时点未知";
    const t = new Date(newest);
    const pad = (n: number) => String(n).padStart(2, "0");
    return `数据检查于 ${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`;
  }
  /** One-line head: title + the lane's own caption as its INLINE SUFFIX, the
   *  ⓘ 口径 glyph and the read stamp. Kept in ONE place so renderNav and
   *  renderPanel can never disagree about what the head says. 契约 2.11a.2：原先
   *  标题下另有一条常驻范围行（`wb-head-note`），与各车道 caption 说的是同一
   *  件事（「这页看什么」）——同一解释不得在两个常驻位置各写一遍，范围行因此
   *  删除，保留 caption 作为标题的行内后缀（§4.2）与 `wb-info` 的 口径 短句。
   *  Contract 4.2: the caption is a suffix — as a prefix it pushed the lane
   *  name 177px right of `.wb-content`'s left edge (measured 1600x900:
   *  「总览」 started at x=483, the content column at x=306), so the title
   *  no longer starts where its own content starts. */
  private renderHead() {
    const info = this.root.querySelector<HTMLButtonElement>(".wb-info");
    if (this.lane < 0) {
      this.root.querySelector(".wb-title")!.textContent = "";
      this.root.querySelector(".wb-index")!.textContent = "";
      if (info) {
        info.hidden = true;
        info.textContent = "";
      }
      return;
    }
    // 维护者 2026-09-28（Plan-Revision 9 逐页收纳 · 总览）：页头只留**页名 + 数据时间**——
    // 原先标题后还挂一条常驻 caption（「用量趋势 · 网关运行 · 待处理」），是同一件事的第二处
    // 常驻解释（§2.11a.2）。caption 移进标题旁的 ⓘ 详情（与 HEAD_INFO 口径同一处），
    // 页头因此只剩页名；`wb-index` 的检查时点不动。当前页名称仍只在这里写一次。
    this.root.querySelector(".wb-title")!.textContent = names[this.lane] ?? "";
    // Contract 3.2 `wb-info`: the ⓘ glyph beside the title carries the lane's
    // short 口径 sentence ONLY. It never replaces the resident `wb-cost-note`
    // strip (缺失日 N / 价目核对 stay there), and a lane without a note hides
    // the glyph instead of showing an empty circle.
    if (info) {
      // 页名之外的「这页看什么」与口径同一处：caption 与 HEAD_INFO 合成一条 ⓘ 说明，
      // 不另占页头一行。
      const note = [LANE_CAPTIONS[this.lane] ?? "", HEAD_INFO[this.lane] ?? ""]
        .filter(Boolean)
        .join(" · ");
      info.hidden = !note;
      // `.wb-info` carries its own `display: inline-flex`, which beats the UA
      // `[hidden]` rule — the inline display is what actually hides it.
      info.style.display = note ? "" : "none";
      // 问题 4（维护者 2026-09-28）：这个 ⓘ 原先只有浏览器 `title`，页内没有
      // 提示板——而 `title` 不算可访问提示（纯图标控件必须有页内提示板）。按既有
      // 口径补上 `.wb-icon-hint`（同一个类、同一条 `:hover` / `:focus-visible`
      // 显示规则），名称仍是同一个口径句子；口径句子比一般控件名长得多，故提示板
      // 按既有形制换行显示（见 workbench.css §6 的 `.wb-info` 规则）。
      info.innerHTML = note ? `i${wbIconHint(note)}` : "";
      info.title = note;
      info.setAttribute("aria-label", note ? `口径说明：${note}` : "口径说明");
    }
    this.root.querySelector(".wb-index")!.textContent = this.headStamp();
    // Console-wide run verdict (2026-09-29 redesign): the same main-chain word
    // the overview hero shows, on every lane, so 「现在能不能用」 is answered
    // wherever the maintainer is. Shape + word carry the state, never hue alone.
    const status = this.root.querySelector<HTMLElement>(".wb-head-status");
    // The overview's own verdict card says the same thing first; the head pill
    // carries it on every other lane (2026-10-31, no second copy on one page).
    if (status) status.hidden = this.lane === 0;
    if (status) {
      // Written only when it changes: the pill is a polite live region, and a
      // repaint with the same verdict must not be announced again.
      const verdict = this.mainVerdict();
      if (status.dataset.state !== verdict.dot) {
        status.dataset.state = verdict.dot;
        status
          .querySelector(".wb-dot")
          ?.setAttribute("class", `wb-dot wb-dot-${verdict.dot}`);
      }
      const text = status.querySelector(".wb-head-status-text");
      if (text && text.textContent !== verdict.word)
        text.textContent = verdict.word;
    }
    const publicStatus = this.root.querySelector<HTMLElement>(
      "[data-wb-share-head-public]"
    );
    if (publicStatus) {
      publicStatus.hidden = this.lane !== 6;
      if (this.lane === 6) {
        const payload =
          this.shareStatus?.state === "live" ? this.shareStatus.payload : {};
        const summary = publicShareSummary(
          payload,
          this.latestSharePublicTest()
        );
        const text = `分享：${summary.text}`;
        publicStatus.className = `wb-badge wb-badge-${summary.state}`;
        if (publicStatus.textContent !== text) publicStatus.textContent = text;
      }
    }
  }
  private renderAll() {
    this.renderNav();
    this.renderPanel();
    this.syncElements();
  }
  private renderNav() {
    this.root
      .querySelectorAll<HTMLButtonElement>("[data-wb-lane]")
      .forEach((b) =>
        b.setAttribute("aria-pressed", String(+b.dataset.wbLane! === this.lane))
      );
    this.renderHead();
  }
  tick(t = Date.now()) {
    if (Math.floor(t / 1000) === this.lastSecond) return;
    this.lastSecond = Math.floor(t / 1e3);
    const d = new Date(t);
    rollText(
      this.root.querySelector(".wb-clock")!,
      d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }),
      !this.stage.classList.contains("reduce-motion")
    );
    this.root.querySelector(".wb-date")!.textContent = d.toLocaleDateString(
      "zh-CN",
      { year: "numeric", month: "long", day: "numeric", weekday: "long" }
    );
  }
  /** P4/R4：内层滚动区域按**稳定名字**存取读取位置——取值在补丁之前（此时还是旧
   *  节点），写回在补丁之后（可能已经是新节点），所以即使节点被重建也保得住位置；
   *  浏览器自己的滚动锚定只是补充，不是唯一依赖。 */
  private capturePanelScroll(): Map<string, { top: number; for: string }> {
    const captured = new Map<string, { top: number; for: string }>();
    this.root
      .querySelectorAll<HTMLElement>(".wb-content [data-wb-scroll]")
      .forEach((el) => {
        captured.set(el.dataset.wbScroll!, {
          top: el.scrollTop,
          for: el.dataset.wbScrollFor ?? "",
        });
      });
    return captured;
  }
  private restorePanelScroll(
    captured: Map<string, { top: number; for: string }>
  ): void {
    this.panelScroll = captured;
    this.root
      .querySelectorAll<HTMLElement>(".wb-content [data-wb-scroll]")
      .forEach((el) => {
        const saved = captured.get(el.dataset.wbScroll!);
        if (!saved) return;
        // 区域换了选中对象（模型 / 来源）时从顶部开始，不继承上一份文档的位置；
        // 同一对象的重绘（读取 / 保存 / 刷新返回）原样保留。
        const top =
          saved.for === (el.dataset.wbScrollFor ?? "") ? saved.top : 0;
        if (el.scrollTop !== top) el.scrollTop = top;
      });
  }
  /** §10.1 设计决定（2026-10-10）两侧调宽：存储键
   *  `gateway-console-pane-width-v1:${namespace}`（namespace = demo / production，
   *  与网关顺序偏好同一命名空间），日志来源侧栏 200–480px（默认 300）、模型详情栏
   *  260–560px（默认 320）；最小宽度与设计默认值都在 `pane-width.ts` 里声明一次。
   *  读不出来用默认值，并在分隔条 title 里如实写出原因。 */
  private paneWidthPrefsLoad(): PaneWidthPreferences | null {
    if (this.paneWidthLoaded) return this.paneWidthPrefs;
    this.paneWidthLoaded = true;
    try {
      this.paneWidthPrefs = new PaneWidthPreferences({
        storage: localStorage,
        namespace: GW_ORDER_NAMESPACE,
      });
      const read = this.paneWidthPrefs.read();
      this.paneWidths = read.widths;
      if (read.status === "stored")
        this.paneWidthNote = "列宽已在本浏览器记住。";
      else if (read.status !== "default")
        this.paneWidthNote =
          "此前保存的列宽未能采用，当前使用设计默认宽度；再次拖动会尝试保存。";
    } catch {
      this.paneWidthNote = "存储不可用，列宽只在本页有效。";
    }
    return this.paneWidthPrefs;
  }
  /** 该分隔条的当前宽度（像素）：没读过先读一次，读不出来用设计默认值。 */
  private paneWidth(id: string): number {
    this.paneWidthPrefsLoad();
    const spec = paneWidthSpec(id);
    return clampPaneWidth(spec, this.paneWidths[id] ?? spec.fallback);
  }
  /** 分隔条容器的内联自定义属性：容器上的这一个值是列宽的唯一事实来源。 */
  private paneWidthStyle(id: string): string {
    const spec = paneWidthSpec(id);
    return ` style="${spec.cssVar}:${this.paneWidth(id)}px"`;
  }
  /** 分隔条上的一次完整写回：拒绝时页面宽度仍为准，并写清是哪一种拒绝。 */
  private savePaneWidth(id: string, width: number): void {
    const spec = paneWidthSpec(id);
    this.paneWidths[id] = clampPaneWidth(spec, width);
    const prefs = this.paneWidthPrefsLoad();
    if (!prefs) {
      this.paneWidthNote =
        "列宽只在本页有效：本浏览器未记住（无法访问本浏览器存储）。";
      return;
    }
    try {
      const saved = prefs.save({ ...this.paneWidths });
      this.paneWidthNote = saved.persisted
        ? "列宽已在本浏览器记住。"
        : `本浏览器未记住列宽（${saved.reason === "storage_write_failed" ? "写入被拒绝" : "内容超出上限"}）；本次列宽只在本页有效。`;
    } catch {
      this.paneWidthNote = "存储写入失败，列宽只在本页有效。";
    }
  }
  /** 分隔条本体：`role="separator"` + 当前宽度读给辅助技术；键盘等价与拖动
   *  共用同一份宽度状态（拖动和调宽是同一件事的两种输入）。 */
  private splitHandleHtml(id: string, label: string): string {
    const spec = paneWidthSpec(id);
    const width = this.paneWidth(id);
    return `<div class="wb-split-handle" role="separator" aria-orientation="vertical" tabindex="0" data-wb-split="${escapeHtml(id)}" aria-label="${escapeHtml(`${label}：拖动调宽，或按 ← / → 每次 16px、Home / End 到最小 / 最大、Escape 恢复默认`)}" aria-valuemin="${spec.min}" aria-valuemax="${spec.max}" aria-valuenow="${width}" aria-valuetext="${width}px" title="${escapeHtml(`${label} · 当前 ${width}px（${spec.min}–${spec.max}px）· ${this.paneWidthNote}`)}"><i aria-hidden="true"></i></div>`;
  }
  /** 拖动分隔条：指针移动时直接写容器的自定义属性（不发重绘），释放时才保存
   *  一次；Escape / pointercancel 回到按下前的宽度，不写存储。 */
  private splitDragStart(event: PointerEvent, id: string, handle: HTMLElement) {
    if (event.button !== 0 || this.splitDrag || this.gwDrag || this.logDrag)
      return;
    const container = handle.parentElement;
    if (!container) return;
    const spec = paneWidthSpec(id);
    event.preventDefault();
    const start = this.paneWidth(id);
    let applied = start;
    this.splitDrag = {
      id,
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth: start,
      moved: false,
    };
    const apply = (width: number) => {
      applied = width;
      container.style.setProperty(spec.cssVar, `${width}px`);
    };
    container.classList.add("wb-split-dragging");
    const move = (e: PointerEvent) => {
      if (!this.splitDrag || e.pointerId !== this.splitDrag.pointerId) return;
      apply(paneWidthAfterMove(spec, start, e.clientX - this.splitDrag.startX));
      if (applied !== start) this.splitDrag.moved = true;
    };
    const finish = (commit: boolean) => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", cancel);
      window.removeEventListener("keydown", key, true);
      container.classList.remove("wb-split-dragging");
      const drag = this.splitDrag;
      this.splitDrag = undefined;
      this.gwRepaintDeferred = false;
      if (drag && commit && drag.moved) this.savePaneWidth(id, applied);
      else container.style.setProperty(spec.cssVar, `${start}px`);
      this.renderPanel();
    };
    const up = (e: PointerEvent) => {
      if (this.splitDrag && e.pointerId === this.splitDrag.pointerId)
        finish(true);
    };
    const cancel = (e: PointerEvent) => {
      if (this.splitDrag && e.pointerId === this.splitDrag.pointerId)
        finish(false);
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        finish(false);
      }
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", cancel);
    window.addEventListener("keydown", key, true);
  }
  /** 分隔条的键盘等价（同一份宽度状态）：← / → 每次 16px、Home / End 到最小 /
   *  最大、Escape 恢复设计默认；每次按键都是一次明确的改动，立即保存一次。 */
  private splitHandleKey(event: KeyboardEvent, id: string): void {
    const spec = paneWidthSpec(id);
    const current = this.paneWidth(id);
    const next =
      event.key === "ArrowLeft"
        ? paneWidthAfterMove(spec, current, -16)
        : event.key === "ArrowRight"
          ? paneWidthAfterMove(spec, current, 16)
          : event.key === "Home"
            ? spec.min
            : event.key === "End"
              ? spec.max
              : event.key === "Escape"
                ? spec.fallback
                : null;
    if (next === null) return;
    event.preventDefault();
    const width = clampPaneWidth(spec, next);
    if (width === current) return;
    this.savePaneWidth(id, width);
    this.renderPanel();
    // 重绘后原节点可能已被替换：焦点回到同一身份的分隔条上。
    this.root
      .querySelector<HTMLElement>(`[data-wb-split="${CSS.escape(id)}"]`)
      ?.focus({ preventScroll: true });
  }
  private renderPanel() {
    if (this.gwDrag || this.logDrag || this.splitDrag) {
      this.gwRepaintDeferred = true;
      return;
    }
    if (this.lane < 0) {
      this.root.querySelector(".wb-content")!.replaceChildren();
      this.renderHead();
      return;
    }
    this.renderHead();
    let html = "";
    if (this.lane === 0 || this.lane === 1) {
      const isOverview = this.lane === 0;
      const kpiValues = this.kpi();
      // 账本可读 ≠ 运行快照可读：`Boolean(this.runtime)` 曾把「8001 运行快照
      // 读到了」当成「操作账本读到了」，于是账本读失败时这里仍断言
      // 「台账为空或全部成功」——未知被写成了零。
      const ledgerLive = this.opsRecent?.state === "live";
      const failures = ledgerLive
        ? this.opsRecent!.rows.filter((op) => op.state === "failed")
        : [];
      // 读取在途（`undefined`）与读取失败（`gated`）分开：在途时写「读取中」，
      // 只有真的读失败才写实测原因——应答了 HTTP 错误就写应答错误。
      // 失败操作按 action 聚合成组头（与操作记录页的失败分组同一模式：组头写明
      // action 与失败数，点击才展开单行详情）。维护者 2026-09-28：待处理行高
      // 48–56px、长失败原因进服务行/ⓘ 详情——三条 share 失败各约 98px 内联列出会把
      // 「服务明细」挤出首屏（1536×864/dsf1.25 错误态实测：标题底 975 > 864、
      // 区块起点 1048 > 864）；聚合成组头后同一界面首屏四层重新成立，且失败一条
      // 不少：组头有计数与动作、展开后单行详情带时间、目标、错误码与全文。
      // 2026-10-31: on the overview the ledger reads as one strip under 服务 —
      // a one-line state beside the title, the action groups (when any) below it.
      const failGroups = new Map<string, OpRow[]>();
      for (const op of failures) {
        const list = failGroups.get(op.action) ?? [];
        list.push(op);
        failGroups.set(op.action, list);
      }
      const opsState =
        this.opsRecent === undefined
          ? `<p class="wb-muted">操作账本读取中…（未返回前不写「未读到」）。</p>`
          : !ledgerLive
            ? `<p class="wb-muted">${escapeHtml(this.opsReadFailureText())}（未知，不是「没有失败的操作」）。</p>`
            : failures.length
              ? `<p class="wb-muted"><b class="wb-ov-fail-count">${failures.length} 条失败</b> · 按动作分 ${failGroups.size} 组，点组头展开单行详情</p>`
              : `<p class="wb-muted">账本已读取，没有失败的操作记录。</p>`;
      const failGroupsHtml = failGroups.size
        ? `<div class="wb-fail-groups">${Array.from(failGroups.entries())
            .map(([action, ops]) => {
              const on = this.failuresOpen.has(action);
              // 同组错误码一致时才在组头写它（混合错误码不挑一个冒充全部）。
              const codes = new Set(
                ops.map((op) => op.error?.code ?? "failed")
              );
              const code = codes.size === 1 ? [...codes][0] : null;
              const detail = on
                ? `<div class="wb-fail-detail">${ops.map((op) => `<div class="wb-op"><span class="wb-op-time">${this.opTime(op.created_at)}</span><span>${escapeHtml(op.action)} → ${escapeHtml(op.target || "—")}</span><b>${escapeHtml(op.error?.code ?? "failed")}${op.error?.message ? ` · ${escapeHtml(op.error.message)}` : ""}</b><i class="wb-dot wb-dot-down" aria-hidden="true"></i></div>`).join("")}</div>`
                : "";
              return `<div class="wb-fail-group"><button type="button" class="wb-fail-head${on ? " wb-fail-on" : ""}" data-wb-fail-toggle="${escapeHtml(action)}" aria-expanded="${on}" title="展开 / 收起 ${escapeHtml(action)} 的失败单行详情（长失败原因在这里展开，不占首屏高度）"><i class="wb-dot wb-dot-down" aria-hidden="true"></i><span>${escapeHtml(action)}</span><b>失败 ×${ops.length}${code ? ` · ${escapeHtml(code)}` : ""}</b></button>${detail}</div>`;
            })
            .join("")}</div>`
        : "";
      // DOM order is the reading order: verdict → usage → services → operations
      // → the second-tier trends, each section the content's full width.
      // PR11 (maintainer 2026-09-30, Q17): 服务 is a master-detail panel — the
      // list names every chain with its probe shape, its runtime word, its
      // attention word, its port and any in-flight or terminal action state; the
      // detail beside it is the chain's full management detail (probe evidence,
      // runtime snapshot, account status and actions, start / restart / stop
      // behind the existing confirm, logs). Stop lives in the detail only, so a
      // destructive action always takes a deliberate selection first.
      // 契约 3.12b：探针说明是解释性内容，默认折叠。折叠的是说明文本，
      // 各链摘要本身与每一行的状态词仍在首层；展开后说明全文一条不少。
      // 2026-10-31 (maintainer: 「需要处理和服务明细功能有点重叠」): the chain
      // queue and the master-detail listed the same chains twice. They are one
      // panel now — every list row carries its own attention word, the head
      // counts them — and the ledger's failed operations, the queue's only
      // facts of its own, moved to the 最近操作 strip below it. The panel spans
      // the content width so the detail's readings and actions fit their rows
      // (beside a narrow card it wrapped to twice the list's height).
      const attention = CHAINS.filter(
        (s) => this.attentionWord(s) !== null
      ).length;
      const overviewServices = isOverview
        ? `<section class="wb-block wb-ov-services wb-char" aria-labelledby="wb-ov-services-title"><div class="wb-section-head"><h3 id="wb-ov-services-title">服务</h3><small>${attention ? `<b class="wb-ov-attn-count">${attention} 条需要处理</b> · ` : `${CHAIN_COUNT_WORD}链均在监听中 · `}选择一条查看状态、端口与可用动作</small></div>` +
          `<div class="wb-md"><div class="wb-md-side"><div class="wb-md-list" role="group" aria-label="服务链（↑ / ↓ 切换）">${CHAINS.map((s) => this.serviceItemHtml(s)).join("")}</div>` +
          `<details class="wb-secondary-details"${this.probeIntroOpen ? " open" : ""} data-wb-probe-intro="1"><summary title="探针说明（来源 / 检查时点 / 陈旧口径 · ${CHAIN_COUNT_WORD}链）· 点击展开 / 收起">探针说明${wbFoldMark()}</summary><p class="wb-muted">状态词与探针时点来自 8001 健康快照（/api/console/health · 30s TTL），启停经 8001 适配层真实执行并需二次确认；陈旧证据只降新鲜度，不判宕机。端口、探针方法/耗时、运行快照与认证说明在所选链的详情里；手动重探在页首运行裁决右侧（usagesrv.py 的 refresh=1 真实重探）。</p></details></div>` +
          this.serviceDetailHtml(this.ovService, "overview") +
          `</div></section>`
        : "";
      // A12-3 (Plan-Revision 12): the main trend is the 14-day DAILY COMPLETE
      // token series, the per-model request ranking reads the three-chain ledger
      // aggregate, and the 24h whole-request output-rate panel follows. Each card
      // carries its own honest state. Contract 4.2: the trend section is the
      // second tier and never takes part in the verdict above it.
      // Contract 5.2 #25 + 3.6: a 总览级统一时间轴 endpoint really does NOT exist
      // (14 天走 usage-tokens、24 小时走 usage-rates、7 天走 stats), so the control
      // stays `disabled` with its evidence in the title and 「后端未提供」 visible.
      // 2026-10-31: the two time-axis charts take full rows. Beside the narrow
      // 构成 column the 24h chart (24 hourly samples) and, beside its table, the
      // 14-day chart were narrower than their own axes and scrolled at
      // 1280–1366px. 构成 (14 days, by gateway) and 按模型排行 (the three-chain
      // ledger, all time) read different windows, so they are two cards on one
      // row rather than one card with a subhead.
      const chartGrid = isOverview
        ? `<section class="wb-ov-trends" data-wb-key="ov-trends"><div class="wb-section-head"><h3>用量趋势与速率</h3><small>第二层 · 不参与上方的可用性裁决</small><button type="button" class="wb-btn wb-btn-sm" disabled title="后端未提供：/api/console 只有 usage-tokens（14 天）/ usage-rates（24 小时）/ stats（近 7 天）三个窗口各不相同的接口；本页不把三条曲线画成一条。">统一时间轴 · 后端未提供</button></div>` +
          `<div class="wb-chart-grid">` +
          `<section class="wb-chart-card wb-chart-wide wb-tok-card" data-wb-series="tok"><div class="wb-chart-head"><h3>每日完整 Token · 近 14 天</h3><small>输入+输出齐全的行合计 · 北京时间自然日 · 缺分量的历史另列</small></div>${this.tokenTrendHtml()}</section>` +
          `<div class="wb-chart-pair"><section class="wb-chart-card"><div class="wb-chart-head"><h3>Token 构成</h3><small>按网关 · 近 14 天完整记录</small></div>${this.tokenCompositionHtml()}</section>` +
          `<section class="wb-chart-card"><div class="wb-chart-head"><h3>按模型排行</h3><small>Token（输入 + 输出）· 三链账本累计</small></div>${kpiValues ? this.barListHtml(6) : `<p class="wb-muted">${escapeHtml(this.summaryNote())}</p>`}</section></div>` +
          `<section class="wb-chart-card wb-chart-wide" data-wb-series="tok"><div class="wb-chart-head"><h3>整次平均输出 Token/s · 近 24 小时</h3><small>输出 ÷ 整次耗时（含首字等待）· 按小时与模型分组 · 跳过记录不参与分母</small></div>${this.ratePanelHtml()}</section>` +
          `</div></section>`
        : "";
      // 总览首屏顺序（维护者 Plan-Revision 3 定案，2026-10-31 合并③④）：① 运行裁决
      // → ② 用量与费用 → ③ 服务（主从面板，逐行带需处理词）→ 最近操作 → 第二层趋势。
      // 读数条是无框分区（`wb-bare`）：三张读数卡自己就是卡，外面不再套第二圈框。
      const kpiBlock =
        `<section class="wb-block wb-bare wb-ov-usage" data-wb-key="ov-usage"><h3>用量与费用</h3>` +
        // 契约 3.3：KPI 条内**不渲染** `wb-spark`（装饰性七根柱没有坐标也没有刻度）。
        // 真实趋势仍在下方趋势区与逐日 tooltip 里。
        `<div class="wb-kpis">` +
        `<div class="wb-kpi"><small>${kpiChip("layers", "tok")}Token (in+out)</small><b data-kpi="tokens">${kpiValues ? fmtTokens(kpiValues.tokens) : "—"}</b>${kpiValues ? this.kpiWindowHtml(this.tokenWindowPair(), fmtTokens, "14 天完整 Token 序列未读取或无记录") : ""}</div>` +
        this.tpsKpiHtml() +
        // The run verdict above is the page's one hero, so the fee is a reading
        // of the band like the other three (2026-10-31).
        `<div class="wb-kpi wb-kpi-cost" data-wb-cost-state="${this.costReadingState()}"><small>${kpiChip("cost", "cost")}估算费用</small>${kpiValues ? this.costCellHtml("b") : `<b data-kpi="cost">—</b>`}${kpiValues ? this.kpiWindowHtml(this.windowPair("costCny"), fmtCny, "账本日不足 7 天或该分量未记录", (v, partial) => (partial ? `≥${fmtCny(v)}` : fmtCny(v))) : ""}</div>` +
        // 维护者 2026-09-28（Plan-Revision 9 逐页收纳 · 总览）：「标签 + 读数 + 一行真实状态」；
        // 区间口径进标题的 ⓘ 详情（HEAD_INFO[0]），状态行与刷新按钮同排。
        `<div class="wb-kpi"><small>${kpiChip("coin", "cred")}剩余积分（Qoder·实时）</small><b data-kpi="quota">${this.quotaValueText()}</b><div class="wb-kpi-state" title="${escapeHtml(this.quotaStateDetail())}"><small>${this.quotaStateText()}</small><button type="button" class="wb-btn wb-btn-sm" data-wb-quota-refresh="1" title="重新读取国内网关额度（只读，短缓存）" aria-label="刷新额度">${wbIcon("refresh")}${wbIconHint("刷新额度")}</button></div></div>` +
        `</div>` +
        `<p class="wb-muted wb-cost-note" data-wb-summary-note>${escapeHtml(this.summaryNote())}</p></section>`;
      const opsBlock =
        `<section class="wb-block wb-ov-ops" aria-labelledby="wb-ov-ops-title"><div class="wb-ov-ops-bar"><div class="wb-section-head"><h3 id="wb-ov-ops-title">最近操作</h3><small>8001 操作账本 · 最近 8 条中的失败</small></div>` +
        opsState +
        `<button type="button" class="wb-btn wb-btn-sm" data-wb-lane="2">${wbIcon("ops")}查看操作记录</button></div>` +
        failGroupsHtml +
        `</section>`;
      html = isOverview
        ? this.verdictHtml() +
          kpiBlock +
          overviewServices +
          opsBlock +
          chartGrid
        : this.gatewayWorkspaceHtml() +
          this.laneNoteFoldHtml(
            "account",
            "账号与额度说明",
            `<p class="wb-muted">额度与用量为只读查询：进入页面各读一次（不产生操作记录、不写上游状态）；读取失败保留上次真实值并标过期，未知一律显示「未知」，不折成 0。需要人工登录的动作会弹出真实浏览器窗口、由维护者本人完成登录，先二次确认再提交，绝不自动打开。</p>`
          ) +
          this.opStatusHtml();
    }
    if (this.lane === 2) {
      // Contract 4.4 order: failure groups first (the lane's focus), the full
      // 8001 ledger window below them (main area), the single-op three-state
      // line last (secondary). Both views render the SAME opsRecent rows, so the
      // grouping can never disagree with the list it is drawn from.
      html =
        `<div class="wb-section-tabs" data-wb-key="section-tabs"><button data-wb-lane="4">网关日志</button><button aria-pressed="true" data-wb-lane="2">操作记录</button></div>` +
        this.failuresLaneHtml() +
        this.opsListHtml();
    }
    if (this.lane === 3) {
      html = this.feeTablesHtml();
    }
    if (this.lane === 4) {
      html =
        `<div class="wb-section-tabs" data-wb-key="section-tabs"><button aria-pressed="true" data-wb-lane="4">网关日志</button><button data-wb-lane="2">操作记录</button></div>` +
        this.logWorkspaceHtml();
    }
    if (this.lane === 6) {
      html = this.shareLaneHtml();
    }
    // R9-R40 task 5 (maintainer-requested): a thin top progress bar while a
    // read is in flight — summary aggregation or an open log detail read.
    // reduce-motion callers get a static translucent strip instead.
    // P4/R4（2026-10-10）：进度条住在一个**固定加载槽位**里，槽位永远是
    // `.wb-content` 的第一个子节点（身份 `wb-loadslot`）。旧实现忙时把加载条
    // 插到内容首部、闲时又拿掉，位置补丁就按位置把后面每个兄弟节点错位比较
    // （div 对 section），于是整段列表被重建：日志列表 scrollTop 200 → 0、
    // 模型详情 100 → 0、日志详情标题丢焦点。槽位不随忙闲增删，后续兄弟的
    // 位置与身份都不变；槽位自身 `position: absolute` 不参与布局，几何不变。
    const busy =
      this.summaryState === "loading" || Boolean(this.logDetail?.loading);
    const reduceMotion = this.stage.classList.contains("reduce-motion");
    const slotHtml =
      `<div class="wb-loadslot" data-wb-key="wb-loadslot" data-wb-busy="${busy ? "1" : "0"}" aria-hidden="true">` +
      (busy
        ? `<div class="wb-loadbar${reduceMotion ? " wb-loadbar-static" : ""}"></div>`
        : "") +
      `</div>`;
    const content = this.root.querySelector<HTMLElement>(".wb-content")!;
    // The share lane's own page scroll must survive its in-row repaints
    // (rename/cap editor, a test result, a confirm label): otherwise the
    // buttons the user is aiming at appear to move under the pointer.
    const main = this.root.querySelector<HTMLElement>(".wb-main");
    const shareScroll = this.lane === 6 && main ? main.scrollTop : null;
    // L-C: the apply→readback repaint keeps the reading position (keepScroll
    // is set only by the auto-readback path, consumed exactly once).
    const savedScroll = this.keepScroll ? content.scrollTop : null;
    // P4/R4：内层列表与详情各自的滚动位置按**稳定区域名**存取（不按元素引用），
    // 重绘（读取 / 保存 / 刷新返回）不重置；区域换了选中对象（`data-wb-scroll-for`
    // 变化）时从顶部开始，而不是继承上一份文档的位置。
    const innerScroll = this.capturePanelScroll();
    patchRollingPanel(content, slotHtml + html, !reduceMotion);
    this.decorateTableLabels(content);
    this.restorePanelScroll(innerScroll);
    if (shareScroll !== null && main) {
      main.scrollTop = shareScroll;
      // The editor's focus can still nudge the lane scroll in the same frame;
      // restore once more after layout so the action buttons never appear to move.
      window.requestAnimationFrame(() => {
        if (main.isConnected) main.scrollTop = shareScroll;
      });
    }
    if (savedScroll !== null) {
      content.scrollTop = savedScroll;
      this.keepScroll = false;
    }
    this.installGatewayEvents();
    this.installFieldEvents();
    this.applyShareFocus();
    this.rearmConfirm();
    this.rearmShareConfirm();
    this.reflashCopied();
    this.renderNotices();
    this.syncShareModal();
    this.restoreLogFocus();
  }
  /** 手机卡片布局（2026-10-22）：5 列以上的宽表在 ≤760px 改成「标签 / 值」
   *  两行式卡片，不再横向滚动。表头文案写进每个数据格的 `data-label`；
   *  `colspan` 空态行是整行说明，不补标签；`tfoot` 的「合计」首格也不补。
   *  面板是原地 morph，节点会被复用，所以每次渲染都重跑（幂等）。 */
  private decorateTableLabels(root: HTMLElement) {
    for (const table of root.querySelectorAll<HTMLTableElement>(
      "table.wb-table"
    )) {
      if (
        table.classList.contains("wb-model-table") ||
        table.classList.contains("wb-tok-table")
      )
        continue;
      const host = table.closest(".wb-block, .wb-chart-card.wb-chart-wide");
      const heads = [
        ...table.querySelectorAll<HTMLTableCellElement>("thead th"),
      ].map((th) => (th.textContent || "").trim());
      if (!host || heads.length < 5) {
        delete table.dataset.wbCards;
        continue;
      }
      table.dataset.wbCards = "1";
      const set = (cell: HTMLTableCellElement, index: number) => {
        if (cell.colSpan > 1) return;
        const text = heads[index];
        if (text && cell.dataset.label !== text) cell.dataset.label = text;
      };
      for (const row of table.querySelectorAll<HTMLTableRowElement>("tbody tr"))
        [...row.children].forEach((cell, i) => {
          if (cell instanceof HTMLTableCellElement) set(cell, i);
        });
      const foot = table.querySelector<HTMLTableRowElement>("tfoot tr");
      if (foot)
        [...foot.children].forEach((cell, i) => {
          if (i > 0 && cell instanceof HTMLTableCellElement) set(cell, i);
        });
    }
  }
  /** 级别筛选的判定：2xx / 4xx / 5xx 按状态码族，其余（含空状态）归「其他」。
   *  空选择 = 全部通过，所以「全部」是真正的不过滤，而不是一个假选项。 */
  private inFeedLevel(status: string | null | undefined): boolean {
    if (!this.logFeedLevel) return true;
    const s = (status || "").toLowerCase();
    if (this.logFeedLevel === "other")
      return !(s.startsWith("2") || s.startsWith("4") || s.startsWith("5"));
    return s.startsWith(this.logFeedLevel);
  }
  private fieldEventsInstalled = false;
  /** Every lane's text fields, checkboxes and selects, delegated ONCE on the root.
   *  The in-place panel patch keeps nodes across renders (and can hand a node
   *  another role by position), so the former per-render listeners stacked: one
   *  change then repainted - or re-read the log - once per stacked listener, and
   *  each repaint added another. Each event is routed by what its target is at
   *  the moment it fires; the handlers read the current state, never a closure.
   *  Searches debounce (model / 14-day series 300ms with focus kept across the
   *  repaint, log 400ms re-querying the server-side literal); the share-key edit
   *  mirrors its typed value into shareKeyEdit so probe-tick repaints cannot
   *  wipe it. */
  private installFieldEvents() {
    if (this.fieldEventsInstalled) return;
    this.fieldEventsInstalled = true;
    this.root.addEventListener("input", (event) => {
      const t = event.target;
      if (!(t instanceof HTMLInputElement)) return;
      if (t.matches("[data-model-q]")) {
        window.clearTimeout(this.modelQTimer);
        this.modelQTimer = window.setTimeout(() => {
          const value = t.value.trim().slice(0, 120);
          if (value === this.modelCatalogQ) return;
          this.modelCatalogQ = value;
          this.renderPanel();
          const fresh =
            this.root.querySelector<HTMLInputElement>("[data-model-q]");
          if (fresh && document.activeElement !== fresh) fresh.focus();
        }, 300);
      } else if (t.matches("[data-log-q]")) {
        window.clearTimeout(this.logQTimer);
        this.logQTimer = window.setTimeout(() => {
          const cur = this.logDetail;
          if (!cur) return;
          const value = t.value.trim().slice(0, 120);
          if (value === cur.q) return;
          cur.q = value;
          void this.fetchLogEntries(cur, false);
        }, 400);
      } else if (t.matches("[data-wb-series-q]")) {
        window.clearTimeout(this.tokenQTimer);
        this.tokenQTimer = window.setTimeout(() => {
          const value = t.value.trim().slice(0, 60);
          if (value === this.tokenSeriesQ) return;
          this.tokenSeriesQ = value;
          this.renderPanel();
          const fresh =
            this.root.querySelector<HTMLInputElement>("[data-wb-series-q]");
          if (fresh && document.activeElement !== fresh) fresh.focus();
        }, 300);
      } else if (t.matches("[data-wb-share-edit]")) {
        if (this.shareKeyEdit) this.shareKeyEdit.value = t.value;
      } else if (t.matches("[data-wb-ms-field]")) {
        // Rev8 模型设置草稿：页面会话内记下正在编辑的值，30s 轮询 / 读面
        // 落定后的重绘按字段还原，不把用户输入抹掉；不发任何请求。
        this.msFieldInput(t);
      }
    });
    this.root.addEventListener("change", (event) => {
      const t = event.target;
      if (t instanceof HTMLInputElement) {
        if (t.matches("[data-wb-sync-mode]")) {
          // §11.3 同步窗口范围模式：全部 / 只同步选中（空选拒绝在提交前写清）。
          const mode = t.dataset.wbSyncMode;
          if (mode === "all") this.modelScopeCustom = false;
          else if (mode === "selected") this.modelScopeCustom = true;
          else return;
          this.renderPanel();
          return;
        }
        const set = t.matches("[data-model-src-check]")
          ? this.modelScopeSources
          : t.matches("[data-model-consumer-check]")
            ? this.modelScopeConsumers
            : t.matches("[data-model-row-check]")
              ? this.modelChecked
              : null;
        if (set) {
          if (t.checked) set.add(t.value);
          else set.delete(t.value);
          this.renderPanel();
          return;
        }
        const range = t.matches("[data-log-since]")
          ? "since"
          : t.matches("[data-log-until]")
            ? "until"
            : null;
        const d = this.logDetail;
        if (range && d) {
          d[range] = t.value;
          void this.fetchLogEntries(d, false);
        }
        if (t.matches("[data-wb-ms-field]")) this.msFieldInput(t);
        return;
      }
      if (!(t instanceof HTMLSelectElement)) return;
      if (t.matches("[data-wb-ms-field]")) {
        this.msFieldInput(t);
        return;
      }
      if (t.matches("[data-log-feed-src]")) {
        this.logFeedSrc = t.value;
        this.renderPanel();
        return;
      }
      if (t.matches("[data-log-feed-model]")) {
        this.logFeedModel = t.value;
        this.renderPanel();
        return;
      }
      if (t.matches("[data-log-feed-window]")) {
        this.logFeedWindow = t.value;
        this.renderPanel();
        return;
      }
      if (t.matches("[data-log-feed-level]")) {
        this.logFeedLevel = t.value;
        this.renderPanel();
        return;
      }
      const d = this.logDetail;
      if (!d) return;
      if (t.matches("[data-log-gen]")) {
        const value = Number(t.value);
        if (!Number.isInteger(value) || value < 0) return;
        d.rotation = value;
        d.nextCursor = null;
        void this.fetchLogEntries(d, false);
      }
    });
    // Scrolling up inside the log rows pauses follow (one repaint, not one per render).
    this.root.addEventListener(
      "wheel",
      (event) => {
        if (!(event.target as Element).closest?.(".wb-log-rows")) return;
        const d = this.logDetail;
        if (!d || event.deltaY >= 0 || !d.follow) return;
        d.follow = false;
        this.renderPanel();
      },
      { passive: true }
    );
  }
  /** Inline share-key edit: focus (and select a cap) once, right after it opens. */
  /** R3 tabs: one private selection, stable ids/aria wiring, and focus kept on
   *  the selected tab after the repaint. The overview stays outside the tabs. */
  private setShareTab(tab: "keys" | "diagnostics" | "usage", focus = false) {
    this.shareTab = tab;
    this.shareTabFocus = focus;
    this.disarmShare();
    this.shareKeyFocusPending = undefined;
    this.renderPanel();
  }
  /** Repaint-side focus restoration for the share lane: the selected tab after
   *  a tab click, and the exact inline action button after that action. */
  private applyShareFocus() {
    if (this.lane !== 6) {
      this.shareTabFocus = false;
      this.shareKeyFocusPending = undefined;
      return;
    }
    if (this.shareTabFocus) {
      this.shareTabFocus = false;
      this.root
        .querySelector<HTMLElement>(
          `[role="tab"][data-wb-share-tab="${this.shareTab}"]`
        )
        ?.focus({ preventScroll: true });
    }
    if (this.shareKeyFocusPending) {
      const pending = this.shareKeyFocusPending;
      this.shareKeyFocusPending = undefined;
      this.root
        .querySelector<HTMLElement>(
          `[data-wb-share-key="${pending.action}"][data-wb-share-key-id="${CSS.escape(pending.id)}"]`
        )
        ?.focus({ preventScroll: true });
    }
  }
  private chainLabel(name: string): string {
    const map: Record<string, string> = {
      chat2api: "chat2api 主链",
      qoder: "Qoder 国内中转",
      dsh: "DSH 账本",
      usage: "控制台宿主",
    };
    return map[name] ?? name;
  }
  // ---------------- fee lane (R9-R29): gateway fees, per-model detail, prices
  /** Fee lane tables read the SAME ledger the usage meter aggregates
   *  (/api/summary aggregate + the meter's own prices.json). Tables are
   *  Planner-grade compact rows. R9-R32: per-gateway cost share bars. */
  /** 任意区间聚合（`GET /api/console/stats?from&to`，只读、有界）：真实控件 +
   *  真实结果。后端支持该区间（usagesrv.py:3119-3147 读 `from`/`to` 并交给
   *  `console_stats.bounded_stats`，console_stats.py:100,149-165 上限 92 天），
   *  所以这里不再是「后端未提供」的禁用控件。
   *
   *  诚实规则：结果里的每个读数缺字段一律「未知」，绝不 `?? 0`；`coverage.
   *  missing_days` 单独列出（缺日不是 0 费用）；`estimated_cost_cny` 与 `credits`
   *  分开显示，不合并、不折算。400 显示后端自己的受控 `detail`（前端不把它
   *  折成空表），404 按**实测 HTTP 状态**显示「当前服务端版本未载入」。
   *  逐日表按行数上限显示，超过时写明「仅显示前 N 天」。 */
  private statsRangeHtml(): string {
    const state = this.statsRange;
    const now = new Date();
    const key = (d: Date) =>
      `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const from = state
      ? state.from
      : key(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 29));
    const to = state ? state.to : key(now);
    const fieldStyle = `font:inherit;font-size:var(--wb-fs-3);padding:3px var(--wb-sp-2);color:var(--theme-ink);background:var(--theme-field);border:1px solid var(--theme-line);border-radius:var(--wb-r-s)`;
    const form =
      `<div class="wb-feed-src">` +
      `<label><small>起始日</small><input type="date" data-wb-stats-from value="${escapeHtml(from)}" style="${fieldStyle}" aria-label="区间聚合起始日"/></label>` +
      `<label><small>结束日</small><input type="date" data-wb-stats-to value="${escapeHtml(to)}" style="${fieldStyle}" aria-label="区间聚合结束日"/></label>` +
      `<button type="button" class="wb-btn" data-wb-stats-run="1" title="向 GET /api/console/stats?from&to 发一次有界只读查询（不产生操作记录、不写任何状态）">查询区间聚合</button>` +
      (state
        ? `<button type="button" class="wb-btn wb-btn-sm" data-wb-stats-clear="1">清除结果</button>`
        : "") +
      `<small class="wb-muted" title="只读、有界；上限 ${STATS_RANGE_MAX_DAYS} 天（与后端 console_stats.py 同一口径）。只有显式提交才发一次请求。">只读 · 上限 ${STATS_RANGE_MAX_DAYS} 天 · 提交才查询</small>` +
      `</div>`;
    const body = !state
      ? `<p class="wb-muted">尚未查询。区间聚合与近 14 天趋势是不同窗口的读数，不互相代替，也不把缺日补成 0。</p>`
      : state.state === "loading"
        ? `<p class="wb-muted">区间 ${escapeHtml(state.from)} → ${escapeHtml(state.to)} 读取中…（一次有界只读 GET）</p>`
        : state.state === "rejected"
          ? `<p class="wb-muted">区间 ${escapeHtml(state.from)} → ${escapeHtml(state.to)} 被后端拒绝（HTTP ${state.status}）：${escapeHtml(state.reason)}；不折成空表，也不按 0 显示。</p>`
          : state.state === "failed"
            ? `<p class="wb-muted">区间 ${escapeHtml(state.from)} → ${escapeHtml(state.to)} 读取失败（${escapeHtml(state.reason)}${state.status ? ` · HTTP ${state.status}${state.status === 404 ? " · 当前服务端版本未载入该端点" : ""}` : ""}）；保留上一次真实结果，不以 0 或空表冒充。</p>`
            : this.statsRangeBodyHtml(state);
    return `<section class="wb-block"><h3>任意区间聚合</h3>${form}${body}</section>`;
  }
  /** 区间聚合结果（`bounded_stats` 的 `days[]` / `totals` / `coverage` / `meta`）。
   *  逐日表的每格只显示该日真实合计；缺字段写「未知」。`coverage.missing_days`
   *  与 `unpriced_models` 原样列出，`meta.range_days` 用后端自己的口径。 */
  private statsRangeBodyHtml(state: {
    from: string;
    to: string;
    payload: StatsRangePayload;
  }): string {
    const payload = state.payload;
    const totals = payload.totals ?? {};
    const num = (
      value: number | null | undefined,
      format: (v: number) => string
    ): string =>
      typeof value === "number" && Number.isFinite(value)
        ? format(value)
        : "未知";
    const readings =
      `<div class="wb-stats-row">` +
      `<div><small>请求数</small><b>${num(totals.requests, fmtRequests)}</b></div>` +
      `<div><small>输入 token</small><b>${num(totals.input_tokens, fmtTokens)}</b></div>` +
      `<div><small>输出 token</small><b>${num(totals.output_tokens, fmtTokens)}</b></div>` +
      `<div><small>估算费用（¥）</small><b>${num(totals.estimated_cost_cny, fmtCny)}</b></div>` +
      `<div><small>积分（Qoder）</small><b>${num(totals.credits, fmtCredits)}</b></div>` +
      `</div>`;
    const missing = payload.coverage?.missing_days ?? [];
    const unpriced = payload.coverage?.unpriced_models ?? [];
    const sourceCount = payload.coverage?.sources
      ? Object.keys(payload.coverage.sources).length
      : undefined;
    const coverage =
      `<p class="wb-muted">区间 ${escapeHtml(state.from)} → ${escapeHtml(state.to)}` +
      `${typeof payload.meta?.range_days === "number" ? ` · 后端口径 ${payload.meta.range_days} 天` : ""}` +
      `${typeof payload.meta?.timezone === "string" ? ` · 时区 ${escapeHtml(payload.meta.timezone)}` : ""}` +
      `${typeof payload.meta?.price_checked_at === "string" ? ` · 价目核对 ${escapeHtml(payload.meta.price_checked_at)}` : ""}` +
      `；覆盖来源 ${sourceCount === undefined ? "未提供" : `${sourceCount} 个`}` +
      `${missing.length ? ` · 区间内无记录日 ${missing.length} 天（${escapeHtml(missing.slice(0, 10).join("、"))}${missing.length > 10 ? "…" : ""}；缺日不是 0 费用）` : " · 区间内每日都有记录"}` +
      `${unpriced.length ? ` · 未定价模型：${escapeHtml(unpriced.join("、"))}（不折成 ¥0）` : ""}` +
      `；积分链按积分结算，不计入 ¥ 费用，也不与 Token 合成合计。</p>`;
    const days = payload.days ?? [];
    const shown = days.slice(-31);
    const rows = shown
      .map(
        (day) =>
          `<tr><td>${escapeHtml(day.date)}</td>` +
          `<td class="wb-fee-num">${num(day.day_total?.requests, fmtRequests)}</td>` +
          `<td class="wb-fee-num">${num(day.day_total?.input_tokens, fmtTokens)}</td>` +
          `<td class="wb-fee-num">${num(day.day_total?.output_tokens, fmtTokens)}</td>` +
          `<td class="wb-fee-num">${num(day.day_total?.estimated_cost_cny, fmtCny)}</td>` +
          `<td class="wb-fee-num">${num(day.day_total?.credits, fmtCredits)}</td></tr>`
      )
      .join("");
    const table =
      `<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>日</th><th class="wb-fee-num">请求数</th><th class="wb-fee-num">输入 token</th><th class="wb-fee-num">输出 token</th><th class="wb-fee-num">估算费用（¥）</th><th class="wb-fee-num">积分（Qoder）</th></tr></thead><tbody>${rows || '<tr><td colspan="6">该区间账本没有可展示的日记录（缺日不补 0）。</td></tr>'}</tbody></table></div>` +
      (days.length > shown.length
        ? `<p class="wb-muted">区间共 ${days.length} 个有记录的日，此处只显示最后 ${shown.length} 天；上面合计覆盖整个区间。</p>`
        : "");
    return readings + coverage + table;
  }
  /** 费用页主信息区（contract 4.5）：各网关费用 → 模型明细 → 价目表。 */
  private feeTablesHtml() {
    const kpiValues = this.kpi();
    const value = (key: "tokens" | "credits") => {
      if (!kpiValues) return "—";
      if (key === "tokens") return fmtTokens(kpiValues.tokens);
      return this.displayedCredits() ?? "不可取得";
    };
    // Rev12 F12-2 (maintainer 2026-10-02): the main readings are Token, fee and
    // credits - the 累计请求 card is gone (request counts stay in the per-gateway
    // table as a diagnostic column); A12-3 ⑤ already removed the share bars.
    // Contract 4.5 first screen: each of the three readings also carries its
    // 时间区间 and 环比 (`wb-delta`), because four bare cumulative numbers
    // cannot answer 「用量发生了什么」. The window is the real 14-day ledger
    // window; an unavailable window says so instead of a 0 or a fake percentage.
    // 费用格（维护者 2026-09-28）：读数沿用 displayedCost 的下界语义，
    // partial 时写 ≥¥… +「已知部分」，`data-wb-cost-state` 标出四态。
    const readings =
      `<div class="wb-cost">` +
      `<div><small>${kpiChip("layers", "tok")}Token (in+out)</small><div class="wb-large" data-kpi="tokens">${value("tokens")}</div>${this.kpiWindowHtml(this.tokenWindowPair(), fmtTokens, "14 天完整 Token 序列未读取或无记录")}</div>` +
      `<div class="wb-kpi-hero" data-wb-cost-state="${this.costReadingState()}"><small>${kpiChip("cost", "cost")}估算费用</small>${this.costCellHtml("div")}${this.kpiWindowHtml(this.windowPair("costCny"), fmtCny, "账本日不足 7 天或该分量未记录", (v, partial) => (partial ? `≥${fmtCny(v)}` : fmtCny(v)))}</div>` +
      `<div><small>${kpiChip("coin", "cred")}累计消耗积分（账本）</small><div class="wb-large" data-kpi="credits">${value("credits")}</div>${this.kpiWindowHtml(this.windowPair("credits"), fmtCredits, "账本日不足 7 天或该分量未记录")}</div>` +
      `</div>`;
    // Contract 5.2 #25 + 3.6: 导出／下载账单 has NO backend (no
    // export/download/csv endpoint anywhere under /api/console/), so it stays
    // `disabled` WITH its reason visible (「后端未提供」) and in its title.
    // 任意区间聚合 is a REAL control (GET /api/console/stats?from&to, 92-day cap).
    const exportButton = `<button type="button" class="wb-btn wb-btn-sm" disabled title="后端未提供：/api/console/ 无 export/download/csv 端点，账本只在页面上只读展示。">导出账单 · 后端未提供</button>`;
    // PR11 layout: the readings band and its resident 口径 strip span the column;
    // below them the trend (on charcoal) sits on the left and the lane's context
    // (口径与可信度, then 本期 vs 上期) on the right, so the first screen reads
    // 「多少 → 变化如何 → 可信吗」. The strip keeps 缺失日 N / 价目核对 resident
    // (contract 4.5); their definitions live in the 口径与可信度 fold.
    const kpiReady = Boolean(kpiValues && this.chainFees);
    // Until the aggregate lands (or when it failed) the trend slot holds the
    // pending card, so the left column is never an empty band beside 口径与可信度.
    // The resident strip already carries the honest state; the card only adds
    // what is coming, without repeating the same sentence.
    const pendingCard = `<section class="wb-chart-card wb-chart-pending"><p class="wb-muted">聚合返回后此处展示近 14 天趋势、各网关费用、模型明细与价目表（生产账本聚合约需 30 秒）。</p></section>`;
    const focus =
      readings +
      `<div class="wb-cost-foot"><p class="wb-muted wb-cost-note" data-wb-cost-note>${escapeHtml(this.summaryNote() + this.costResidentNote())}</p>${exportButton}</div>` +
      `<div class="wb-cost-top">` +
      `<div class="wb-cost-main">${kpiReady ? this.feeTrendHtml() : pendingCard}</div>` +
      `<div class="wb-cost-side">${this.credHtml()}${kpiReady ? this.periodCompareHtml() : ""}</div>` +
      `</div>`;
    if (!kpiValues || !this.chainFees) {
      return focus + this.statsRangeHtml() + this.usageFeedHtml();
    }
    // A12-3 ⑤: the per-gateway percentage column (and its .wb-cellbar
    // track) was removed; the absolute request / token / fee / credit
    // numbers stay, and a credit chain keeps saying 积分链.
    const gateRows = this.chainFees
      .map((c) => {
        const amount = costAmount(c.costCny, c.costCnyPartial);
        return `<tr><td>${escapeHtml(this.chainLabel(c.name))}</td><td class="wb-fee-num">${fmtRequests(c.requests)}</td><td class="wb-fee-num">${fmtTokens(c.input)}</td><td class="wb-fee-num">${fmtTokens(c.output)}</td><td class="wb-fee-num"${costAmountBoundTitleAttr(amount)}>${fmtCostAmount(amount)}</td><td class="wb-fee-num">${c.credits !== undefined ? fmtCredits(c.credits) : "—"}</td></tr>`;
      })
      .join("");
    const credit: ChainModel[] = [];
    for (const c of this.chainFees) {
      for (const m of c.models) if (m.costCny === undefined) credit.push(m);
    }
    credit.sort((a, b) => (b.credits ?? 0) - (a.credits ?? 0));
    // 模型费用统计（维护者 2026-10-19）：只列 chat2api 主链的模型——维护者当前只用
    // chat2api，其他链的模型不在这张表里（Qoder 积分链有自己的表，DSH 冻结快照只在
    // 「各网关费用」与「双账台」如实保留）。升级的列：缓存输入 + 缓存率（缓存是输入的
    // 子集，DeepSeek 缓存命中价远低于未命中价）、未定价请求（账本没有价格匹配的请求，
    // 不计入费用与均价，绝不折 0）、均价 = 估算费用 ÷ 已定价请求数（新单位 ¥/请求）。
    // 费用列沿用下界口径：partial 行加 ≥（只含可折算记录），绝不写成确定总额；同一行
    // 的 title 由 costAmountBoundTitleAttr() 给（OD 决定稿「决定 3」）。
    const umModels = (
      this.chainFees.find((c) => c.name === "chat2api")?.models ?? []
    )
      .slice()
      .sort(
        (a, b) =>
          (b.costCny ?? -1) - (a.costCny ?? -1) || b.requests - a.requests
      );
    const umAvg = (amount: CostAmount, priced: number): string => {
      if (amount.kind === "absent" || amount.kind === "unknown") return "—";
      if (amount.value === undefined || priced <= 0) return "不可得";
      const avg = amount.value / priced;
      return amount.kind === "partial"
        ? `≥${fmtCnyPerRequest(avg)}`
        : fmtCnyPerRequest(avg);
    };
    const umCacheCell = (cache: number, input: number): string =>
      `${fmtTokens(cache)}<small class="wb-fee-sub" title="缓存率 = 缓存输入 ÷ 输入 token">${fmtCacheRate(cache, input)}</small>`;
    const umRow = (m: ChainModel) => {
      const amount = costAmount(m.costCny, m.costCnyPartial);
      const priced = m.priced ?? 0;
      return `<tr><td>${escapeHtml(m.model)}</td><td class="wb-fee-num">${fmtRequests(m.requests)}</td><td class="wb-fee-num">${fmtTokens(m.input)}</td><td class="wb-fee-num" title="上游账本记录的缓存命中输入 token（输入 token 的子集）">${umCacheCell(m.cache ?? 0, m.input)}</td><td class="wb-fee-num">${fmtTokens(m.output)}</td><td class="wb-fee-num" title="账本没有价格匹配的请求数（如价目版本生效之前的请求）；不计入费用与均价，绝不折 0">${fmtRequests(m.unpriced ?? 0)}</td><td class="wb-fee-num"${costAmountBoundTitleAttr(amount)}>${fmtCostAmount(amount)}</td><td class="wb-fee-num" title="估算费用 ÷ 已定价请求数；未定价请求不参与均价">${umAvg(amount, priced)}</td></tr>`;
    };
    // 合计 = 各行**显示值**之和（契约 3.8）：费用按逐行取整后的值相加，与账本原值
    // 合计可能有 ≤0.001 的舍入差；任一行只含可折算记录时合计也是下界（≥¥…）。
    const umSum = (pick: (m: ChainModel) => number) =>
      umModels.reduce((s, m) => s + pick(m), 0);
    const umCache = umSum((m) => m.cache ?? 0);
    const umInput = umSum((m) => m.input);
    const umPriced = umSum((m) => m.priced ?? 0);
    const umCost = sumRounded3(
      umModels.map((m) => (typeof m.costCny === "number" ? m.costCny : 0))
    );
    const umPartial = umModels.some(
      (m) => m.costCny === null || m.costCnyPartial === true
    );
    const umTotal: CostAmount = umModels.some(
      (m) => typeof m.costCny === "number"
    )
      ? { kind: umPartial ? "partial" : "full", value: umCost }
      : umPartial
        ? { kind: "partial" }
        : { kind: "unknown" };
    const umFoot = umModels.length
      ? `<tfoot><tr><td>合计</td><td class="wb-fee-num">${fmtRequests(umSum((m) => m.requests))}</td><td class="wb-fee-num">${fmtTokens(umInput)}</td><td class="wb-fee-num">${umCacheCell(umCache, umInput)}</td><td class="wb-fee-num">${fmtTokens(umSum((m) => m.output))}</td><td class="wb-fee-num">${fmtRequests(umSum((m) => m.unpriced ?? 0))}</td><td class="wb-fee-num"${costAmountBoundTitleAttr(umTotal)}>${fmtCostAmount(umTotal)}</td><td class="wb-fee-num">${umAvg(umTotal, umPriced)}</td></tr></tfoot>`
      : "";
    const umNote = `<p class="wb-muted">本表只列 chat2api 主链（UM）的模型：维护者当前只用 chat2api，其他链的模型不在本表——Qoder 积分链有自己的表，DSH 冻结快照只在「各网关费用」与「双账台」如实保留。缓存输入 = 上游账本记录的缓存命中输入 token（输入 token 的子集），缓存率 = 缓存输入 ÷ 输入 token；未定价请求 = 账本里没有价格匹配的请求（如价目版本生效之前的请求），不计入费用与均价、绝不折 0；均价 = 估算费用 ÷ 已定价请求数（单位 ¥/请求，4 位小数），费用列沿用下界口径（只含可折算记录时写 ≥¥… +「已知」）。合计 = 各行显示值之和（逐行四舍五入到 3 位后相加；与账本原值合计可能有 ≤0.001 的舍入差）。</p>`;
    const creditRow = (m: ChainModel) =>
      `<tr><td>${escapeHtml(m.model)}</td><td class="wb-fee-num">${fmtRequests(m.requests)}</td><td class="wb-fee-num">${fmtTokens(m.input)}</td><td class="wb-fee-num">${fmtTokens(m.output)}</td><td class="wb-fee-num">${m.credits !== undefined ? fmtCredits(m.credits) : "—"}</td></tr>`;
    return (
      focus +
      this.statsRangeHtml() +
      this.ledgerPairHtml() +
      `<section class="wb-block"><h3>各网关费用</h3><div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>网关</th><th class="wb-fee-num">请求数</th><th class="wb-fee-num">输入 token</th><th class="wb-fee-num">输出 token</th><th class="wb-fee-num">费用</th><th class="wb-fee-num">积分</th></tr></thead><tbody>${gateRows}</tbody></table></div></section>` +
      `<section class="wb-block"><h3>模型费用统计 · chat2api 主链</h3><div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>模型</th><th class="wb-fee-num">请求数</th><th class="wb-fee-num">输入 token</th><th class="wb-fee-num">缓存输入</th><th class="wb-fee-num">输出 token</th><th class="wb-fee-num">未定价请求</th><th class="wb-fee-num">费用（¥）</th><th class="wb-fee-num">均价（¥/次）</th></tr></thead><tbody>${umModels.length ? umModels.map(umRow).join("") : '<tr><td colspan="8">本次账本读取里没有 chat2api 主链的模型行（未读取到不代表零记录）。</td></tr>'}</tbody>${umFoot}</table></div>${this.laneNoteFoldHtml("fee-um-table", "表格口径与来源说明", umNote)}</section>` +
      (credit.length
        ? `<section class="wb-block"><h3>模型调用明细 · 积分链（Qoder）</h3><div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>模型</th><th class="wb-fee-num">请求数</th><th class="wb-fee-num">输入 token</th><th class="wb-fee-num">输出 token</th><th class="wb-fee-num">积分</th></tr></thead><tbody>${credit.map(creditRow).join("")}</tbody></table></div></section>`
        : "") +
      `<section class="wb-block"><h3>价目表</h3>${this.priceTableHtml()}</section>` +
      this.usageFeedHtml()
    );
  }
  /** Resident 口径 strip for the fee lane (contract 4.5): 「缺失日 N」 and
   *  「价目核对」 stay on `wb-cost-note`, NOT folded into a `wb-info` tooltip.
   *  A day absent from the ledger is counted as 缺失日 and drawn as a broken
   *  line — never as a zero; a day that IS recorded but whose fee component is
   *  unknown is counted in its OWN sentence (分量未知 N) and is never written as
   *  「账本无记录」. The price-table check time comes from the same
   *  prices.json the meter consumes, and an unread table says so instead of
   *  pretending it was verified.
   *
   *  「缺失日」只在 `summaryState === "live"` 时成立：读取失败（或尚未成功
   *  读取）时序列为空，按空序列反推会得到「近 14 天账本无记录」这种确定
   *  结论——空序列是「没读到」，不是「账本没有记录」。该状态下趋势图不渲染
   *  （stale 渲染的是上一次真实读取），所以「图上断线」也不能照写。 */
  private costResidentNote(): string {
    const price = this.priceTable;
    const priceLine = price
      ? `价目核对 ${String(price.checkedAt ?? "不可取得")}${price.checkedStale ? "（超过 30 天未核对，建议查官网更新）" : ""}`
      : "价目核对不可取得（价目表未读到，不冒充已核对）";
    if (this.summaryState !== "live") {
      // 未知就是未知：不谈缺失日、不写「账本无记录」；失败原因带实测状态。
      const why =
        this.summaryState === "loading"
          ? "费用聚合读取中"
          : this.summaryState === "unauthenticated"
            ? "费用服务拒绝读取（HTTP 401）"
            : this.summaryState === "forbidden"
              ? "管理权限被拒（HTTP 403）"
              : `本次读取失败：${this.summaryError || "原因未知"}`;
      const chart =
        this.kpi() && this.chainFees ? "图为上一次真实读取" : "趋势图不渲染";
      return ` · 缺失日与断线均不可得（${why}；${chart}） · ${priceLine}。`;
    }
    // 四种日级状态分开计数、互斥（维护者 2026-09-28 ②）；各自的定义在「口径与可信度」
    // 折叠里（credHtml 的「日级口径」行）——常驻条只写本次读数的事实。
    const cost = this.trendDays("costCny", 14);
    const missingDays = cost.values.filter(
      (v, i) => v === null && !cost.unknown[i] && !cost.boundOnly[i]
    ).length;
    const unknownCostDays = cost.unknown.filter(Boolean).length;
    const boundOnlyCostDays = cost.boundOnly.filter(Boolean).length;
    const partialCostDays = cost.partial.filter(Boolean).length;
    return (
      ` · 近 14 天缺失日 ${missingDays} 天 · 分量未知 ${unknownCostDays} 天` +
      (boundOnlyCostDays ? ` · 下界日 ${boundOnlyCostDays} 天` : "") +
      (partialCostDays ? ` · 有值下界日 ${partialCostDays} 天` : "") +
      ` · ${priceLine}。`
    );
  }
  /** 口径与可信度 panel (contract 4.5 / 3.9): the fee lane's first screen
   *  carries all SIX `summaryState` words with the CURRENT one marked, so
   *  「读取中」, 「本次真实读取」, 「刷新失败保留上次值」, 「401」, 「403」 and
   *  「读取失败」 are told apart at a glance and `stale` is never merged with
   *  `error`. The two 口径 hard constraints are stated HERE, not in a tooltip:
   *  积分链按积分结算、不计入 ¥ 费用, and the m365 chain is excluded. */
  private credHtml(): string {
    const states: {
      key: Workbench["summaryState"];
      text: string;
      cls: string;
    }[] = [
      { key: "loading", text: "loading · 读取中", cls: "wb-badge-idle" },
      { key: "live", text: "live · 本次真实读取", cls: "wb-badge-ok" },
      {
        key: "stale",
        text: "stale · 刷新失败，保留上次真实值",
        cls: "wb-badge-warn",
      },
      {
        key: "unauthenticated",
        text: "unauthenticated · 401 无凭据",
        cls: "wb-badge-danger",
      },
      {
        key: "forbidden",
        text: "forbidden · 403 无权限",
        cls: "wb-badge-danger",
      },
      { key: "error", text: "error · 读取失败", cls: "wb-badge-danger" },
    ];
    // 维护者 2026-09-28（Plan-Revision 9 逐页收纳 · 费用页）：六态词表是**词典**
    // （长「loading/live/stale/401/403/error」条目），原先六条 chip 常驻首层。词表移进
    // 「口径与可信度」折叠，首层只显示**本次实际状态**（下面 head 的「当前态 <state>」）。
    // 当前态在词表里用 `aria-current="true"` 标出——不再往 chip 文本里加「（当前）」，
    // 11px 短标里 24 字已是上限，加后缀会把「本次实际状态」变成超长句。
    const chips = states
      .map((s) => {
        const on = s.key === this.summaryState;
        return `<span class="wb-badge ${s.cls}"${on ? ' aria-current="true"' : ""}>${escapeHtml(s.text)}</span>`;
      })
      .join("");
    const captured = this.summaryEpoch
      ? `数据截至 ${new Date(this.summaryEpoch).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`
      : "尚无成功读取";
    const credits = this.chainCredits("qoder");
    const billed = (this.chainFees ?? []).filter(
      (c) => c.costCny !== undefined
    ).length;
    const credit = (this.chainFees ?? []).filter(
      (c) => c.costCny === undefined
    ).length;
    return (
      `<div class="wb-cred">` +
      `<div class="wb-cred-head"><h3>口径与可信度</h3><small>当前态 ${escapeHtml(this.summaryState)} · ${escapeHtml(captured)}</small></div>` +
      // 维护者 2026-09-28（Plan-Revision 9 逐页收纳 · 费用页）：折的是**解释与词典**——
      // 五条口径整句与六态词表（长「loading/live/stale/401/403/error」条目）都进
      // 「口径与可信度」折叠区；`wb-cred` 作为区块仍常驻，`wb-cred-head` 的
      // 「当前态 <state> · 数据截至 <时点>」就是首层的**本次实际状态**。
      // 账本构成是「本次读到什么」的常驻事实，不进默认折叠：未读取到聚合时
      // 写「未知」而不是 0 条——折叠区里看不见的未知等于被当成 0 读。
      `<div class="wb-cred-row" data-wb-cred-composition="1"><span class="wb-cred-k">当前账本构成</span><span class="wb-cred-v">${
        this.chainFees === undefined
          ? "计费链未知 · 积分链未知（本次账本聚合未读取到，不按 0 条显示）"
          : `计费链 ${billed} 条 · 积分链 ${credit} 条`
      }</span></div>` +
      `<details class="wb-secondary-details"${this.credDetailsOpen ? " open" : ""} data-wb-cred-details="1">` +
      `<summary title="口径与可信度（4 条口径行 · 六态词表 · 检查时点）· 点击展开 / 收起">口径与可信度${wbFoldMark()}</summary>` +
      `<div class="wb-cred-states">${chips}</div>` +
      `<div class="wb-cred-row"><span class="wb-cred-k">积分链口径</span><span class="wb-cred-v">Qoder 积分链按积分结算，<b>不计入 ¥ 费用</b>；积分与费用各用自己的刻度，不折算、不合并${credits === undefined ? "（本链积分账本累计未知，不按 0 显示）" : `（本链积分账本累计 ${fmtCredits(credits)}）`}。</span></div>` +
      `<div class="wb-cred-row"><span class="wb-cred-k">排除的链</span><span class="wb-cred-v">m365 链已下线（ADR 0019），<b>不计入</b>本页费用与积分。</span></div>` +
      `<div class="wb-cred-row"><span class="wb-cred-k">账本范围</span><span class="wb-cred-v">本页只读个人网关账本聚合（qoder / chat2api / dsh）。分享网关的用量是另一本账，不并入本页；两本账不相加。</span></div>` +
      `<div class="wb-cred-row"><span class="wb-cred-k">账本构成口径</span><span class="wb-cred-v">按链名下的账本行统计；未读取到聚合时写「未知」，不按 0 条显示——「0 条」只代表读到的账本确实没有对应行，不代表账本为空。</span></div>` +
      `<div class="wb-cred-row"><span class="wb-cred-k">日级口径</span><span class="wb-cred-v">缺失日 = 近 14 天内账本没有该日记录（图上断线，不按 0 显示）；分量未知 = 当天有账本记录、但费用分量未知（同样断线，不按 0）；下界日 = 当天有记录、金额只含可折算记录且可折算为空（下界为空：图上断线，与链行同写「未知（下界仅含可折算部分）」）；有值下界日 = 只含可折算记录且有值（读数写 ≥¥… +「已知部分」，图上空心点 / 虚线段，区间合计与末值只写下界）。费用聚合不是 live 时这些计数都不可得，未知不按 0 天或「账本无记录」显示；stale 时趋势图是上一次真实读取的序列。</span></div>` +
      `<p class="wb-cred-note">六态为互斥的当前态：stale（刷新失败，保留上次真实值）与 error（读取失败、无上次值）是两回事，不合并；401 / 403 是权限结论，不是空账本。读取失败一律显示空值，不以模拟数据回填。</p>` +
      `</details>` +
      `</div>`
    );
  }
  /** 双账台 (contract 4.5 / 3.9): the ¥ ledger and the credit ledger stand side
   *  by side, each with its OWN scale — the two are never summed and never
   *  converted into one another, so a credit chain can never acquire a ¥ figure
   *  it does not have. Both columns read the same real `chainFees` rows the
   *  tables below render, and a chain absent from the read keeps its honest
   *  「不可取得」 instead of a 0. 费用行沿用同一份下界读数（displayedCost），
   *  任一行只含可折算记录时合计写成 ≥¥… +「已知部分」，不冒充完整总额。 */
  private ledgerPairHtml(): string {
    const fees = this.chainFees ?? [];
    const billed = fees.filter((c) => c.costCny !== undefined);
    const credit = fees.filter((c) => c.costCny === undefined);
    const sum = (rows: ChainFee[], pick: (c: ChainFee) => number) =>
      rows.reduce((s, c) => s + pick(c), 0);
    const row = (label: string, value: string, note: string) =>
      `<div class="wb-cred-row"><span class="wb-cred-k">${escapeHtml(label)}</span><span class="wb-cred-v">${escapeHtml(value)}<br/><small class="wb-muted">${escapeHtml(note)}</small></span></div>`;
    const billedScale = billed.length
      ? `刻度：人民币 ¥（${billed.length} 条计费链账本合计）`
      : "刻度：人民币 ¥（本次聚合无计费链行；未读取到不代表为零）";
    const creditScale = credit.length
      ? `刻度：Qoder 积分（${credit.length} 条积分链账本合计，独立于 ¥）`
      : "刻度：Qoder 积分（本次聚合无积分链行；未读取到不代表为零）";
    // Contract 3.8: the printed total must equal the sum of the printed rows.
    // The rows print 3-decimal values, so the totals are built from those SAME
    // rounded row values (sumRounded3) — summing the raw values and rounding
    // once at the end is what made the printed total differ from its own rows.
    const rounding =
      "显示合计 = 各行显示值之和（逐行四舍五入到 3 位后相加；与账本原值合计可能有 ≤0.001 的舍入差）";
    const cost = this.displayedCost();
    const costText =
      cost === undefined
        ? "不可取得"
        : `${fmtCostAmount(cost)}${cost.kind === "partial" && cost.value !== undefined ? "（已知部分）" : ""}`;
    return (
      `<div class="wb-ledger-pair">` +
      `<section class="wb-ledger"><div class="wb-ledger-head"><strong>¥ 账 · 计费链</strong><span class="wb-ledger-scale">${escapeHtml(billedScale)}</span></div>` +
      row(
        "请求数",
        billed.length
          ? fmtRequests(sum(billed, (c) => c.requests))
          : "不可取得",
        "计费链账本行合计；不并入积分账"
      ) +
      row(
        "估算费用",
        costText,
        `未定价请求不折算为 ¥；链金额未知时不折 0；含下界时只写下界；${rounding}`
      ) +
      `</section>` +
      `<section class="wb-ledger"><div class="wb-ledger-head"><strong>积分账 · Qoder</strong><span class="wb-ledger-scale">${escapeHtml(creditScale)}</span></div>` +
      row(
        "请求数",
        credit.length
          ? fmtRequests(sum(credit, (c) => c.requests))
          : "不可取得",
        "积分链账本行合计；不并入 ¥ 账"
      ) +
      row(
        "积分",
        credit.length
          ? fmtCredits(sumRounded3(credit.map((c) => c.credits ?? 0)))
          : "不可取得",
        "按积分结算；不折算为人民币"
      ) +
      `</section></div>`
    );
  }
  private priceTableHtml(): string {
    const p = this.priceTable;
    if (!p)
      return `<p class="wb-muted">价目表读取中或 8001 不可达（读取计价器自用价目文件）。</p>`;
    const n = (v: unknown): string =>
      typeof v === "number" && Number.isFinite(v) ? String(v) : "—";
    const head = `<p class="wb-muted">汇率 ¥/USD ${p.exchangeRate ?? "—"} · 价目核对 ${p.checkedAt ?? "—"}${p.checkedStale ? ' · <b class="wb-stale-warn">超过 30 天未核对，建议查官网更新</b>' : ""} · providers 单位 USD/百万 token；models 为 DeepSeek 峰谷档（offPeak / peak 子档）。本表与计价器消费的是同一份文件。</p>`;
    const deepRows = Object.entries(p.models ?? {})
      .map(([k, v]) => {
        const off = (v.offPeak ?? v) as Record<string, unknown>;
        const peak = (v.peak ?? {}) as Record<string, unknown>;
        return `<tr><td>${escapeHtml(k)}</td><td class="wb-fee-num">${n(off.cacheHit)}</td><td class="wb-fee-num">${n(off.cacheMiss)}</td><td class="wb-fee-num">${n(off.output)}</td><td class="wb-fee-num">${n(peak.cacheMiss)}</td><td class="wb-fee-num">${n(peak.output)}</td></tr>`;
      })
      .join("");
    const providerRows = Object.entries(p.providers ?? {})
      .flatMap(([prov, body]) => {
        const models = ((body as Record<string, unknown>).models ??
          {}) as Record<string, Record<string, unknown>>;
        return Object.entries(models).map(
          ([mk, mv]) =>
            `<tr><td>${escapeHtml(prov)} · ${escapeHtml(mk)}</td><td class="wb-fee-num">${n(mv.input)}</td><td class="wb-fee-num">${n(mv.cachedInput)}</td><td class="wb-fee-num">${n(mv.output)}</td><td>${escapeHtml(String(mv.billingMode ?? ""))}</td></tr>`
        );
      })
      .join("");
    return `${head}<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>DeepSeek 峰谷档</th><th class="wb-fee-num">闲时 cacheHit</th><th class="wb-fee-num">offPeak cacheMiss</th><th class="wb-fee-num">offPeak output</th><th class="wb-fee-num">peak cacheMiss</th><th class="wb-fee-num">peak output</th></tr></thead><tbody>${deepRows}</tbody></table><table class="wb-table"><thead><tr><th>Provider · 模型</th><th class="wb-fee-num">输入</th><th class="wb-fee-num">缓存输入</th><th class="wb-fee-num">输出</th><th>计价</th></tr></thead><tbody>${providerRows}</tbody></table></div>`;
  }
  // ---------------- log lane (R9-R30): gateway call logs + 7-day stats
  /** Log lane (R9-R30): seven-day real statistics from the same ledger the
   *  summary aggregates, then the gateways' own call logs (tail rows, newest
   *  first), then the 8001 operation ledger. Coverage is named honestly —
   *  DSH is a historical snapshot, M365 is offline per ADR 0019. R9-R32 adds
   *  the day-over-day delta chip and per-row status dots. */
  /** Log lane read-state line (contract 4.6, `wb-log-state`): the lane's first
   *  screen states what the read actually IS — how many rows the bounded tail
   *  returned, whether the source registry itself could be read, and which
   *  sources the SERVER declined to read. A failed registry read is named in
   *  words (never folded into an empty source list), and an unread source is
   *  never rendered as 「没有记录」. */
  private logLaneStateHtml(): string {
    // The lane states what the read IS, so an UNANSWERED read says 读取中…
    // instead of 「8001 不可达」/「无凭据会话」: the old first branch wrote
    // 8001 不可达 purely because the 30s poll had not reached its first
    // tick yet, i.e. before any request existed to answer for it. Only an
    // answered, failed read writes its own failure now.
    const feed = this.logsFeed;
    if (!feed) {
      return `<p class="wb-log-state" data-state="loading">调用日志读取中…（有界尾读：每来源 ${LOG_FEED_PER_SOURCE} 条，上限 ${LOG_FEED_LIMIT} 条）</p>`;
    }
    if (feed.state !== "live") {
      // 维护者 2026-09-28（Plan-Revision 9 逐页收纳 · 日志页）：读取失败要有**页内重试**，
      // 不再只写一句失败话；重试是同一口的有界只读重读（不产生操作行），
      // 与来源注册表的重试同一个形状（纯图标 + aria-label + 页内提示板）。
      return `<p class="wb-log-state" data-state="gated">调用日志读取失败（${escapeHtml(feed.reason ?? "原因未知")}）；不以空表冒充零记录。<button type="button" class="wb-btn wb-btn-sm" data-wb-log-feed-retry="1" title="重新读取各网关调用日志（有界尾读 · 只读，不产生操作行）" aria-label="重试读取调用日志">${wbIcon("refresh")}${wbIconHint("重试读取调用日志")}</button></p>`;
    }
    const unavailable = feed.unavailable ?? [];
    const parts = [
      `本次有界尾读读到 ${feed.rows.length} 条（每来源 ${LOG_FEED_PER_SOURCE} 条，上限 ${LOG_FEED_LIMIT} 条）`,
      this.logSourcesFailed
        ? `来源注册表读取失败（${this.logSourcesError || "原因未知"}），重试进入日志页或点「重试读取来源注册表」。`
        : this.logSources
          ? `来源注册表已读取 ${this.logSources.length} 个来源`
          : "来源注册表读取中…",
      unavailable.length
        ? `服务端声明未读取的来源：${unavailable.map((s) => this.chainLabel(s)).join("、")}（不在调用日志账本内，不以空表冒充零记录）`
        : "服务端未声明未读取的来源",
    ];
    return `<p class="wb-log-state" data-state="live">${escapeHtml(parts.join(" · "))}</p>`;
  }
  /** 日志页（Rev12 F12-6，维护者 2026-10-02）：只放运行事件。入页立即读一次来源
   *  注册表；调用明细（逐条调用与近 7 天统计）属于用量，已迁到费用页
   *  （usageFeedHtml），操作账本留在「操作记录」页签。各来源运行事件的统一中文
   *  摘要与「查看该请求相关记录」等后端统一事件读口交付后再接。 */
  private async copyLogRaw(message: string, button: HTMLButtonElement) {
    try {
      if (!navigator.clipboard?.writeText)
        throw new Error("clipboard_unavailable");
      await navigator.clipboard.writeText(message);
      this.flashCopied(button);
    } catch {
      button.textContent = "复制失败，请展开原文后手动复制";
    }
  }
  private logOverviewStatus(status: string): string {
    return (
      (
        {
          unavailable: "全部目标未读",
          no_targets: "未配置运行日志",
          unconfigured: "未配置日志",
          missing: "日志文件缺失",
          denied: "无读取权限",
          error: "读取失败",
        } as Record<string, string>
      )[status] ?? status
    );
  }
  private logOverviewEvents() {
    return (this.logSources ?? [])
      .flatMap((source) => {
        const snapshot = this.logOverview.get(source.source);
        // 部分目标未读（partial）仍列出已读目标的行；全部未读（失败 / 未配置 / 缺席）
        // 没有行可列，不把读取失败折成 0 条。
        if (snapshot?.status !== "ok" && snapshot?.status !== "partial")
          return [];
        const identities = logRowIdentities(source.source, snapshot.rows);
        return snapshot.rows.map((row, i) => ({
          row,
          source,
          kind: logKind(row),
          identity: identities[i]!,
        }));
        // P3/R3：按**归一化时间**排序（`logTsEpoch`，与后端 `normalize_ts` 同口径），
        // 原始 `ts` 文本原样保留在行里（表格仍显示它）。时间无法识别的行排在所有有
        // 时间行之后、彼此保持原顺序（未知时间绝不参与比较）——原先按原始字符串比较
        // 会把同日凌晨的 ISO 行排到晚上日志之前，正是要修的反例。
      })
      .sort((a, b) => {
        const ta =
          a.row.ts_norm !== undefined ? a.row.ts_norm : logTsEpoch(a.row.ts);
        const tb =
          b.row.ts_norm !== undefined ? b.row.ts_norm : logTsEpoch(b.row.ts);
        if (ta === null && tb === null) return 0;
        if (ta === null) return 1;
        if (tb === null) return -1;
        return tb - ta;
      });
  }
  /** 分类筛选（分类 chips）是总览表与详情表共用的唯一分类入口；这里只做显示
   *  筛选，不静默丢弃：返回当前筛选下要列出的行。 */
  private logKindFilteredRows(rows: LogEntryRow[]): LogEntryRow[] {
    return this.logOverviewFilter === "all"
      ? rows
      : rows.filter((row) => logKind(row) === this.logOverviewFilter);
  }
  /** 当前分类筛选的中文读数，供空表 / 计数句点名实际筛选轴（不是别的筛选）。 */
  private logKindFilterLabel(): string {
    return this.logOverviewFilter === "all"
      ? "全部分类"
      : logKindLabels[this.logOverviewFilter];
  }
  private logFiltersHtml(rows: LogEntryRow[], read: boolean): string {
    const kinds = Object.keys(logKindLabels) as LogKind[];
    return `<div class="wb-log-overview-filters"><button class="wb-btn wb-btn-sm" data-wb-log-overview-filter="all" aria-pressed="${this.logOverviewFilter === "all"}">全部 ${read ? rows.length : "—"}</button>${kinds.map((kind) => `<button class="wb-btn wb-btn-sm wb-log-kind-${kind}" data-wb-log-overview-filter="${kind}" aria-pressed="${this.logOverviewFilter === kind}">${logKindLabels[kind]} ${read ? rows.filter((row) => logKind(row) === kind).length : "—"}</button>`).join("")}</div>`;
  }
  private logWorkspaceHtml(): string {
    const right =
      this.logDetailOpen && this.logDetail
        ? this.logDetailHtml()
        : this.logLaneHtml();
    const sources = this.logSources ?? [];
    const ids = sources.map((source) => source.source);
    if (sources.length && !this.logOrderLoaded) {
      this.logOrderLoaded = true;
      try {
        this.logOrderPrefs = new GatewayOrderPreferences({
          storage: localStorage,
          namespace: `${GW_ORDER_NAMESPACE}:runtime-logs`,
        });
        const read = this.logOrderPrefs.read(ids);
        this.logSourceOrder = read.order;
        if (read.status === "coordinated")
          this.logOrderNote = "来源顺序已在本浏览器记住。";
        else if (read.status !== "default")
          this.logOrderNote =
            "此前保存顺序未能采用，当前使用默认顺序；再次排序会尝试保存。";
      } catch {
        this.logOrderNote = "存储不可用，顺序只在本页有效。";
      }
    }
    this.logSourceOrder = this.logSourceOrder.filter((id) => ids.includes(id));
    for (const id of ids)
      if (!this.logSourceOrder.includes(id)) this.logSourceOrder.push(id);
    const selected = this.logDetailOpen
      ? (this.logDetail?.source ?? this.logPending?.source)
      : undefined;
    const visible = this.logSourceOrder.flatMap((id) =>
      sources.filter(
        (source) =>
          source.source === id &&
          `${source.display} ${id}`
            .toLowerCase()
            .includes(this.logSourceQuery.toLowerCase())
      )
    );
    const rows = visible
      .map((source) => {
        const snap = this.logOverview.get(source.source);
        // P3/R3：部分目标未读时如实写「部分目标未读（read/targets）」并保留已读行数，
        // 不折成「读取中」或 0 条。
        const status = !source.categories.length
          ? "未配置运行日志"
          : !snap
            ? "未读取"
            : snap.status === "ok"
              ? `${snap.rows.length} 条 · 本次窗口`
              : snap.status === "partial"
                ? `${snap.rows.length} 条 · 部分目标未读（${snap.read ?? "?"}/${snap.targets ?? "?"}）`
                : `读取状态：${this.logOverviewStatus(snap.status)}`;
        return `<li class="wb-gw-row${selected === source.source ? " wb-gw-row-on" : ""}" data-wb-key="log-source:${escapeHtml(source.source)}" data-wb-log-source-row="${escapeHtml(source.source)}"><button class="wb-md-item wb-gw-item" data-wb-log-source="${escapeHtml(source.source)}" aria-pressed="${selected === source.source}"><span class="wb-md-name">${escapeHtml(source.display)}</span><span class="wb-md-state">${escapeHtml(status)}</span></button><button class="wb-gw-handle" data-wb-log-handle="${escapeHtml(source.source)}" aria-label="拖动 ${escapeHtml(source.display)} 调整顺序" title="拖动排序；条目上可按 Alt+↑ / Alt+↓"><i></i><i></i><i></i></button></li>`;
      })
      .join("");
    return `<div class="wb-gw wb-log-workspace" data-wb-key="log-workspace"${this.paneWidthStyle("gw-side")}><aside class="wb-gw-side wb-char" data-wb-key="log-sources" aria-label="运行日志来源"><button class="wb-md-item wb-gw-all" data-wb-log-source="" aria-pressed="${!selected}">${wbIcon("logs")}<span class="wb-md-name">全部运行日志</span></button><div class="wb-gw-filter"><input type="search" class="wb-gw-q" data-wb-log-source-q aria-label="搜索日志来源" placeholder="搜索网关或后台服务" value="${escapeHtml(this.logSourceQuery)}"/></div><ol class="wb-gw-list wb-log-source-list" data-wb-scroll="gw-side-list" aria-label="可排序日志来源">${rows || `<li>没有符合条件的来源</li>`}</ol><p class="${/(失败|未能|未记住|不可用|被拒|异常|只在本页)/.test(this.logOrderNote) ? "wb-gw-hint" : "wb-sr-only"}" aria-live="polite">${escapeHtml(this.logOrderNote)}</p></aside>${this.splitHandleHtml("gw-side", "日志来源栏宽")}<div class="wb-gw-main" data-wb-key="log-main">${right}</div></div>`;
  }
  /** 来源顺序的一次完整写回：拒绝时页面顺序仍为准，并写清是哪一种拒绝
   *  （超上限 / 写入被拒），与网关顺序的同一句法（persistGatewayOrder）。 */
  private saveLogOrder(visible: string[]) {
    this.logSourceOrder = reorderGatewaySubset(this.logSourceOrder, visible);
    const prefs = this.logOrderPrefs;
    if (!prefs) {
      this.logOrderNote =
        "顺序只在本页有效：本浏览器未记住来源顺序（无法访问本浏览器存储）。";
      return;
    }
    try {
      const saved = prefs.save(
        this.logSourceOrder,
        (this.logSources ?? []).map((source) => source.source)
      );
      this.logOrderNote = saved.persisted
        ? "来源顺序已在本浏览器记住。"
        : `本浏览器未记住来源顺序（${saved.reason === "sequence_too_long" ? "顺序超过上限" : "写入被拒绝"}）；本次顺序只在本页有效。`;
    } catch {
      this.logOrderNote = "存储写入失败，顺序只在本页有效。";
    }
  }
  private logLaneHtml() {
    if (!this.logSourcesRequested && !this.logSourcesFailed) {
      this.logSourcesRequested = true;
      void this.fetchLogSources().then(() => {
        this.renderPanel();
        void this.fetchLogOverview();
      });
    }
    // The registry buttons and the 读取中… placeholder come from the registry
    // read's OWN state, so an unanswered read no longer hides the entry row.
    // 失败态：失败句必须给出实测原因，并留一个可点的只读重试入口——原先
    // `!response.ok` 只清空列表且永不重试，页面永远停在「读取中…」。
    const logOpenHtml = `<div class="wb-log-open-row">${(this.logSources ?? [])
      .map(
        (r) =>
          `<button type="button" class="wb-btn wb-btn-sm" data-log-open="${escapeHtml(r.source)}" title="查看 ${escapeHtml(r.display)} 的运行日志详情（服务端脱敏、有限尾读）">${wbIcon("logs")}${escapeHtml(r.display)}</button>`
      )
      .join(
        ""
      )}${this.logSourcesFailed ? `<span class="wb-muted">来源注册表读取失败（${escapeHtml(this.logSourcesError || "原因未知")}），重试进入日志页。</span><button type="button" class="wb-btn wb-btn-sm" data-wb-log-sources-retry="1" title="重新读取来源注册表（只读，不产生操作记录）" aria-label="重试读取来源注册表">${wbIcon("refresh")}${wbIconHint("重试读取来源注册表")}</button>` : this.logSources ? "" : `<span class="wb-muted">来源注册表读取中…</span>`}</div>`;
    const events = this.logOverviewEvents();
    const filtered = events.filter(
      (event) =>
        this.logOverviewFilter === "all" ||
        event.kind === this.logOverviewFilter
    );
    const hasRead = (this.logSources ?? []).some((source) => {
      const status = this.logOverview.get(source.source)?.status;
      return status === "ok" || status === "partial";
    });
    const filters = this.logFiltersHtml(
      events.map((event) => event.row),
      hasRead
    );
    // C4：本次渲染的行按身份入表，复制点击只从这里取原文；下标只用于
    // 表格位置，不再进入按钮（后台刷新换窗口时下标会漂移）。
    const rendered = filtered.slice(0, 200);
    this.logOverviewRenderRows = new Map(
      rendered.map((event) => [event.identity, event.row])
    );
    const rows = rendered
      .map(
        ({ row, source, kind, identity }) =>
          `<tr class="wb-log-kind-${kind}" data-wb-key="log-overview-row:${escapeHtml(identity)}"><td><time>${escapeHtml(row.ts ?? "时间未知")}</time></td><td><button class="wb-btn wb-btn-sm" data-log-open="${escapeHtml(source.source)}">${escapeHtml(source.display)}</button></td><td><span class="wb-log-kind">${logKindLabels[kind]}</span></td><td>${this.logRowSummaryHtml(row) || escapeHtml(row.message.split("\n")[0] ?? "")}<div class="wb-log-raw"><button class="wb-btn wb-btn-sm" data-wb-log-raw-toggle="${escapeHtml(identity)}" aria-expanded="${this.logRawOpen.has(identity)}">${this.logRawOpen.has(identity) ? "收起原文" : "原文"}</button>${this.logRawOpen.has(identity) ? `<pre class="wb-log-msg open">${escapeHtml(row.message)}</pre>` : ""}<button class="wb-btn wb-btn-sm" data-wb-log-overview-copy="${escapeHtml(identity)}" aria-label="复制完整原文">复制</button></div></td></tr>`
      )
      .join("");
    const coverage = (this.logSources ?? [])
      .map((source) => {
        const snapshot = this.logOverview.get(source.source);
        // 覆盖行点名每个来源的**目标数**与截断：部分目标未读时保留已读行数并写
        // 「部分目标未读（read/targets）」，不把读取失败计成 0 条。
        const body = !snapshot
          ? "未读取"
          : snapshot.status === "ok"
            ? `${snapshot.rows.length} 条${snapshot.truncated ? "（尾读窗口）" : ""}`
            : snapshot.status === "partial"
              ? `${snapshot.rows.length} 条 · 部分目标未读（${snapshot.read ?? "?"}/${snapshot.targets ?? "?"}）${snapshot.truncated ? "（尾读窗口）" : ""}`
              : escapeHtml(this.logOverviewStatus(snapshot.status));
        return `${escapeHtml(source.display)}：${!source.categories.length ? "未配置运行日志" : body}`;
      })
      .join(" · ");
    return (
      `<section class="wb-block wb-log-sources"><div class="wb-section-head"><h3>运行日志总览</h3><button class="wb-btn wb-btn-sm" data-wb-log-overview-refresh ${this.logOverviewLoading ? "disabled" : ""}>${this.logOverviewLoading ? "读取中…" : "刷新总览"}</button></div>${filters}` +
      (this.logOverviewError
        ? `<p class="wb-stale-warn">总览本次读取失败（${escapeHtml(this.logOverviewError)}）；${this.logOverview.size ? "以下保留上次读取的事件" : "尚无已读事件"}，可点击「刷新总览」重试。</p>`
        : "") +
      `<p class="wb-muted wb-log-scope">当前已读窗口 · 最多 200 条${this.logOverviewCheckedAt ? ` · ${escapeHtml(this.logOverviewCheckedAt)}` : ""}</p>` +
      `<div class="wb-table-scroll wb-log-overview-table" data-wb-key="log-overview-table" data-wb-scroll="log-overview"><table class="wb-table"><thead><tr><th>时间</th><th>网关</th><th>分类</th><th>事件</th></tr></thead><tbody>${rows || `<tr><td colspan="4">${this.logOverviewLoading ? "正在读取各来源…" : events.length ? `没有符合分类筛选「${escapeHtml(this.logKindFilterLabel())}」的事件；点上方「全部」可看回全部` : "暂无已读事件；来源状态见下方"}</td></tr>`}</tbody></table></div>` +
      this.laneNoteFoldHtml(
        "log-read-coverage",
        "读取范围与说明",
        `<p>${coverage}</p><p>这里只统计已读窗口，不是历史累计。未读或读取失败不计为零；通过仅依据明确成功结果。调用用量见「费用」，控制台动作见「操作记录」。</p>`
      ) +
      `${this.logSourcesFailed || !this.logSources ? logOpenHtml : ""}</section>`
    );
  }
  /** 调用明细（Rev12 F12-6：从日志页迁到费用页末尾）：各网关调用日志的有界尾读
   *  与近 7 天统计。进入费用页立即各读一次，不等 30s 轮询的第一拍。 */
  private usageFeedHtml() {
    // Lane entry issues both bounded read-only reads straight away —
    // 有界尾读 and 近 7 天统计 — instead of waiting for the 30s
    // poll's first tick (the lane used to state 无凭据会话 before any request
    // existed, and its first read landed only after the poll). Each read reports
    // its OWN answer; the state line below says 读取中… until one arrives.
    if (!this.logFeedRequested) {
      this.logFeedRequested = true;
      void this.fetchLogs(true).then(() => this.renderPanel());
    }
    if (!this.statsRequested) {
      this.statsRequested = true;
      void this.fetchStats(true).then(() => this.renderPanel());
    }
    const note = `<p class="wb-muted">调用日志与统计来自各网关自身账本（chat2api · qoder 国内 · qoder-global 国际三链 JSONL/日志；接口实际读取哪些来源由服务端 readers 决定，未读取的来源在「逐次」下钻里如实说明；DSH 为历史快照不接入日志流、M365 链下线，费用口径见费用页）；操作记录来自 8001 操作账本。</p>`;
    // 近 7 天统计：每个读数缺字段一律「未知」，绝不 `?? 0` 冒真零；逐日
    // 序列里有一天缺字段就不画 spark（缺字段画成 0 会把「没读到」读成
    // 「那天确实是 0 请求」），并把缺字段的天数写出来。读取中 / 读取失败 /
    // 尚未读取三态分开，失败带实测 HTTP 状态。
    const numOrUnknown = (
      value: number | null | undefined,
      format: (v: number) => string
    ): string =>
      typeof value === "number" && Number.isFinite(value)
        ? format(value)
        : "未知";
    const weekRequests = (this.statsWeek?.days ?? []).map(
      (d) => d.day_total?.requests
    );
    const weekGapDays = weekRequests.filter(
      (v) => typeof v !== "number" || !Number.isFinite(v)
    ).length;
    // 刷新失败且上一次真实读数仍在时，同一屏里必须写出「本次刷新失败」并标陈旧
    // （契约六态 stale）：只写失败原因、数字却一字不变，读起来像本次读取成功。
    const statsHtml = this.statsWeek
      ? (this.statsWeekError
          ? `<p class="wb-muted">近 7 天统计刷新失败（${escapeHtml(this.statsWeekError)}）；以下为上一次真实读数，已标陈旧（本次刷新失败），不以 0 或空表冒充。</p>`
          : "") +
        `<div class="wb-stats-row"><div><small>请求</small><b>${numOrUnknown(this.statsWeek.totals.requests, fmtRequests)}</b>${weekGapDays ? "" : this.sparkSvg(weekRequests as number[])}${this.weekDeltaHtml()}</div><div><small>输入 token</small><b>${numOrUnknown(this.statsWeek.totals.input_tokens, fmtTokens)}</b></div><div><small>输出 token</small><b>${numOrUnknown(this.statsWeek.totals.output_tokens, fmtTokens)}</b></div><div><small>估算费用</small><b>${numOrUnknown(this.statsWeek.totals.estimated_cost_cny, fmtCny)}</b></div><div><small>积分（Qoder）</small><b>${numOrUnknown(this.statsWeek.totals.credits, fmtCredits)}</b></div></div>` +
        (weekGapDays
          ? `<p class="wb-muted">近 7 天逐日请求里有 ${weekGapDays} 天未提供该字段（按「未知」处理，不补 0，因此不画逐日柱）。</p>`
          : "")
      : this.statsWeekError
        ? `<p class="wb-muted">近 7 天统计读取失败（${escapeHtml(this.statsWeekError)}）；本次没有可保留的真实读数，不以 0 或空表冒充。</p>`
        : `<p class="wb-muted">近 7 天统计尚未读取（/api/console/stats 近 7 天口径）；进入日志页立即读取一次，读取失败如实写失败，不以 0 或空表冒充。</p>`;
    const feed = this.logsFeed;
    // F4b: 服务端可能把同一账本按两个 id 各出一份行（国际链注册了
    // "qoder-global" 与 "qoder_global"），表格与筛选只列其中一份，
    // 同一记录不重复出现；被折叠的拼写在说明里点名。
    const visible = this.feedVisibleRows();
    const feedSrcs =
      feed && feed.state === "live"
        ? [...new Set(visible.rows.map((e) => e.source))].sort()
        : [];
    if (this.logFeedSrc && !feedSrcs.includes(this.logFeedSrc)) {
      this.logFeedSrc = ""; // vanished source falls back to 全部
    }
    const feedModels =
      feed && feed.state === "live"
        ? [...new Set(visible.rows.map((e) => e.model).filter(Boolean))].sort()
        : [];
    if (this.logFeedModel && !feedModels.includes(this.logFeedModel)) {
      this.logFeedModel = ""; // vanished model falls back to 全部
    }
    // A12-3 时段 filter: the window is measured against each row's OWN
    // timestamp (an absolute instant), so it stays correct across timezones;
    // rows whose time cannot be parsed never enter a window silently — they
    // are counted and named in the caption instead.
    const windowMs = LOG_FEED_WINDOWS[this.logFeedWindow];
    const nowMs = Date.now();
    const inWindow = (e: LogEntry) => {
      if (windowMs === undefined) return true;
      const t = Date.parse(e.time);
      return Number.isFinite(t) && nowMs - t <= windowMs;
    };
    const unparsable =
      windowMs === undefined
        ? 0
        : visible.rows.filter((e) => !Number.isFinite(Date.parse(e.time)))
            .length;
    const shownRows =
      feed && feed.state === "live"
        ? visible.rows.filter(
            (e) =>
              (!this.logFeedSrc || e.source === this.logFeedSrc) &&
              (!this.logFeedModel || e.model === this.logFeedModel) &&
              inWindow(e) &&
              this.inFeedLevel(e.status)
          )
        : [];
    // 级别（状态）筛选：与来源/模型/时段同样只读同一批有界尾读行，不另发请求；
    // 不匹配的行不静默消失——被它挡掉多少条在下方如实写出。
    const levelFiltered =
      feed && feed.state === "live"
        ? visible.rows.filter(
            (e) =>
              (!this.logFeedSrc || e.source === this.logFeedSrc) &&
              (!this.logFeedModel || e.model === this.logFeedModel) &&
              inWindow(e) &&
              !this.inFeedLevel(e.status)
          ).length
        : 0;
    // Plan-Revision 9 ⑤（维护者 2026-09-28）：每个工作区都必须有节标题（`.wb-block > h3`）。
    // 调用日志在读取中/读取失败时此前整块不渲染（feedHtml 为空串），该车道因此没有节标题
    // （门禁 ⑤ 实测：1600x900/light/live lane 4「sectionTitle 没有量到元素」）。表体仍然只在
    // 有界尾读结果到位后渲染，但节标题与读取状态行（上一行的 `.wb-log-state`）在三种状态下都在。
    const feedHtml =
      !feed || feed.state !== "live"
        ? `<section class="wb-block wb-char" data-wb-anchor="usage-feed"><h3>各网关调用日志</h3>${this.logLaneStateHtml()}${this.laneNoteFoldHtml("log-table", "表格口径与来源说明", note)}</section>`
        : (() => {
            const rows = shownRows
              .slice(0, 60)
              .map(
                (e) =>
                  `<tr><td>${escapeHtml((e.time || "").slice(5, 19).replace("T", " "))}</td><td>${escapeHtml(e.source)}</td><td>${escapeHtml(e.model || "—")}</td><td>${this.statusDotHtml(e.status)}${escapeHtml(e.status || "—")}</td><td class="wb-fee-num">${this.numText(e.input_tokens)}</td><td class="wb-fee-num">${this.numText(e.output_tokens)}</td><td class="wb-fee-num">${e.credits !== undefined && e.credits !== null ? fmtCredits(e.credits) : "—"}</td><td class="wb-fee-num">${this.durationText(e.duration_ms)}</td><td class="wb-fee-num">${this.logRateText(e.output_tokens, e.duration_ms)}</td></tr>`
              )
              .join("");
            const srcSel =
              feedSrcs.length > 1
                ? `<label class="wb-feed-src"><small>网关</small><select data-log-feed-src aria-label="调用日志网关筛选"><option value="">全部</option>${feedSrcs.map((s) => `<option value="${escapeHtml(s)}"${this.logFeedSrc === s ? " selected" : ""}>${escapeHtml(this.chainLabel(s))}</option>`).join("")}</select></label>`
                : "";
            const modelSel =
              feedModels.length > 1
                ? `<label class="wb-feed-src"><small>模型</small><select data-log-feed-model aria-label="调用日志模型筛选"><option value="">全部</option>${feedModels.map((m) => `<option value="${escapeHtml(m)}"${this.logFeedModel === m ? " selected" : ""}>${escapeHtml(m)}</option>`).join("")}</select></label>`
                : "";
            const winSel = `<label class="wb-feed-src"><small>时段</small><select data-log-feed-window aria-label="调用日志时段筛选"><option value=""${this.logFeedWindow === "" ? " selected" : ""}>全部</option><option value="1h"${this.logFeedWindow === "1h" ? " selected" : ""}>近 1 小时</option><option value="6h"${this.logFeedWindow === "6h" ? " selected" : ""}>近 6 小时</option><option value="24h"${this.logFeedWindow === "24h" ? " selected" : ""}>近 24 小时</option></select></label>`;
            // 级别筛选按每条记录自身状态码族分组，只作用在这批有界尾读行上；
            // 「其他」= 既不是 2xx/4xx/5xx 的状态词，也如实可选。
            const levelSel = `<label class="wb-feed-src"><small>级别</small><select data-log-feed-level aria-label="调用日志级别筛选"><option value=""${this.logFeedLevel === "" ? " selected" : ""}>全部</option><option value="2xx"${this.logFeedLevel === "2xx" ? " selected" : ""}>2xx 成功</option><option value="4xx"${this.logFeedLevel === "4xx" ? " selected" : ""}>4xx 客户端</option><option value="5xx"${this.logFeedLevel === "5xx" ? " selected" : ""}>5xx 服务端</option><option value="other"${this.logFeedLevel === "other" ? " selected" : ""}>其他状态</option></select></label>`;
            const rateNote = `<p class="wb-muted">整次平均输出 Token/s 列按每条记录自身的输出 Token ÷ 整次耗时（含首字等待）计算；输出 Token 或耗时未采集（后端以 null 披露）显示「未采集」，耗时非正显示「—」，都不按 0 显示。</p>`;
            const winNote =
              windowMs === undefined
                ? ""
                : `<p class="wb-muted">时段筛选按每行自身时间戳与本机当前时间比较。${unparsable ? `${unparsable} 条时间不可解析的记录未参与筛选（不静默丢弃，仅在非时段视图可见）。` : ""}</p>`;
            const levelNote = levelFiltered
              ? `<p class="wb-muted">级别筛选挡掉了 ${levelFiltered} 条记录（只影响表格显示，不静默丢弃；切回「全部」即可看回）。</p>`
              : "";
            // F4b：同一账本被服务端以多个 id 注册时，表格只列其中一份并在此点名，
            // 不让同一记录按两个 id 各出现一次。
            const dedupeNote = visible.collapsed.length
              ? `<p class="wb-muted">${escapeHtml(visible.collapsed.join(" / "))} 与 ${escapeHtml(visible.kept.join(" / "))} 是同一账本的多个注册 id（服务端 readers 表），表中只列 ${escapeHtml(visible.kept.join(" / "))} 一份，不按 id 重复计数。</p>`
              : "";
            // Plan-Revision 9 ⑥（维护者 2026-09-28 逐页落点 · 日志）：四个筛选（网关/模型/
            // 时段/级别）是同一排控件——控件间 gap 8px、控件高 36–40px（workbench.css 的
            // .wb-log-filters 与 .wb-feed-src select）。此前它们各自散在区块里，靠行内空白分开。
            return `<section class="wb-block wb-char" data-wb-anchor="usage-feed"><h3>各网关调用日志（最近 ${Math.min(shownRows.length, 60)} 条${this.logFeedSrc ? ` · 仅 ${escapeHtml(this.chainLabel(this.logFeedSrc))}` : " · 全源最新在前"}${this.logFeedModel ? ` · 仅模型 ${escapeHtml(this.logFeedModel)}` : ""}${windowMs === undefined ? "" : ` · ${escapeHtml(this.logFeedWindow)}`}${this.logFeedLevel ? ` · 仅 ${escapeHtml(this.logFeedLevel)}` : ""}）</h3>${this.logLaneStateHtml()}<div class="wb-log-filters">${srcSel}${modelSel}${winSel}${levelSel}</div>${winNote}${levelNote}<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>时间（账本原值）</th><th>网关</th><th>模型</th><th>状态</th><th class="wb-fee-num">输入</th><th class="wb-fee-num">输出</th><th class="wb-fee-num">积分</th><th class="wb-fee-num">耗时</th><th class="wb-fee-num">整次平均输出 Token/s</th></tr></thead><tbody>${rows || '<tr><td colspan="9">账本暂无匹配的调用记录。</td></tr>'}</tbody></table></div>${this.laneNoteFoldHtml("log-table", "表格口径与来源说明", rateNote + dedupeNote + note)}</section>`;
          })();
    return (
      feedHtml +
      `<section class="wb-block"><h3>近 7 天统计</h3>${statsHtml}</section>`
    );
  }
  /** Open the run-log detail sub-view for one gateway source. Re-entering
   *  the same source keeps the previous rows/filters/follow state; flipping
   *  source or category re-reads. */
  // ---------------- run-log detail (R9-R38): per-gateway run logs
  /** Open the run-log detail sub-view for one gateway source. Re-entering
   *  the same source keeps the previous rows/filters/follow state; flipping
   *  source or category re-reads. */
  /** `from` is the page the button was pressed on (a click entry). Rev16 A16-3:
   *  one click lands on the log page with this source selected - the lane switch
   *  goes through `select()` like the navigation, so the nav state, title, body
   *  and focus agree; the back button then returns to `from`. */
  private openLogDetail(source: string, category?: string, from?: number) {
    if (from !== undefined) {
      this.logReturnLane = from === 4 ? null : from;
      this.logFocusPending = true;
    }
    if (source === this.logDetail?.source && source && !category) {
      // Back-navigation re-entry: restore the saved rows/filters unchanged.
      this.logDetailOpen = true;
      this.showLogLane();
      return;
    }
    if (!this.logSources && !this.logSourcesFailed) {
      // The registry has not answered yet (never asked, or a read in flight): the
      // log page shows its registry-loading state and only the LATEST target is
      // kept for when the read settles - an earlier click can no longer bring its
      // own source back, and no source is called 「没有可读的运行日志类别」 just
      // because the registry is still loading.
      this.logPending = { source, category };
      this.logDetail = undefined;
      this.showLogLane();
      if (!this.logSourcesRequested) {
        this.logSourcesRequested = true;
        void this.fetchLogSources();
      }
      return;
    }
    const row = (this.logSources ?? []).find((r) => r.source === source);
    const cat = category || row?.categories[0]?.category || "";
    const hasCats = Boolean(row && row.categories.length);
    // No category: a failed registry read and a source without runtime logs are
    // different facts (the first can be retried), never one 「读取失败」.
    const registryFailed = !row && this.logSourcesFailed;
    this.logDetail = {
      source,
      category: cat,
      q: "",
      since: "",
      until: "",
      follow: true,
      newCount: 0,
      rows: [],
      nextCursor: null,
      status: hasCats
        ? "loading"
        : registryFailed
          ? "registry"
          : "unconfigured",
      errorCode: hasCats
        ? ""
        : registryFailed
          ? this.logSourcesError
          : "not_configured",
      scannedBytes: 0,
      truncated: false,
      resetHint: false,
      stale: false,
      loading: hasCats,
      requested: true,
      expanded: new Set(),
      wrap: true,
      rotation: 0,
      requestId: "",
    };
    this.logDetailOpen = true;
    this.showLogLane();
    const current = this.logDetail;
    if (hasCats && current) void this.fetchLogEntries(current, false);
  }
  /** Render the log page, switching to it first when the jump came from another
   *  page; a pending focus lands on the detail title once it exists. */
  private showLogLane() {
    if (this.lane !== 4) this.select(4);
    else this.renderPanel();
  }
  /** Rev16 A16-3: after a cross-page jump the detail title holds focus until the
   *  first read settles. The panel patcher morphs by position, so the loading bar
   *  appearing / leaving recreates the title node; it is re-focused on each
   *  repaint meanwhile, unless the user has moved focus on their own (the root's
   *  focusin listener clears the pending flag). */
  private restoreLogFocus() {
    if (!this.logFocusPending || this.lane !== 4 || !this.logDetailOpen) return;
    const title = this.root.querySelector<HTMLElement>("[data-log-title]");
    if (!title) return;
    if (document.activeElement !== title) title.focus({ preventScroll: true });
    if (!this.logDetail?.loading) this.logFocusPending = false;
  }
  /** Run-log detail subview (R9-R38). Filter controls are plain literals
   *  queried server-side; the row list keeps reading position on prepend. */
  private logDetailHtml(): string {
    const detail = this.logDetail;
    if (!detail) return `<p class="wb-muted lane-note">日志详情初始化中…</p>`;
    const src = (this.logSources ?? []).find((r) => r.source === detail.source);
    const cats = src?.categories ?? [];
    const tabs = cats
      .map(
        (c) =>
          `<button type="button" class="wb-tab${c.category === detail.category ? " wb-tab-on" : ""}" data-wb-log-cat="${escapeHtml(c.category)}" data-wb-log-src="${escapeHtml(detail.source)}">${escapeHtml(c.display)}</button>`
      )
      .join("");
    const stateHtml = this.logDetailStatusHtml(detail);
    // R9-R40 task 6: rotation selector for rotating categories only — the
    // whitelist index maps 1..N to fixed registry generations; the server
    // answers invalid_rotation beyond the cap and falls back for missing
    // generations, so the dropdown only offers what the registry declares.
    const activeCat = cats.find((c) => c.category === detail.category);
    const genOptions = activeCat?.rotating
      ? Array.from(
          { length: Math.min(activeCat.rotation_index ?? 0, 5) },
          (_, i) => i + 1
        )
          .map(
            (n) =>
              `<option value="${n}"${detail.rotation === n ? " selected" : ""}>.${n}（更早）</option>`
          )
          .join("")
      : "";
    const genHtml =
      activeCat?.rotating && (activeCat.rotation_index ?? 0) > 0
        ? `<div class="wb-log-gen-row"><label class="wb-log-date"><small>轮转代次</small><select data-log-gen aria-label="轮转代次"><option value="0"${detail.rotation === 0 ? " selected" : ""}>活动文件</option>${genOptions}</select></label>${detail.rotation > 0 ? `<span class="wb-muted">正在查看轮转代次 .${detail.rotation}</span>` : ""}</div>`
        : "";
    const filterHtml = `<div class="wb-log-filter">
        <input type="text" class="wb-log-q" data-log-q placeholder="关键词（字面量）" value="${escapeHtml(detail.q)}" autocomplete="off" spellcheck="false" aria-label="日志关键词"/>
        <label class="wb-log-date"><small>自</small><input type="date" data-log-since value="${escapeHtml(detail.since)}"/></label>
        <label class="wb-log-date"><small>至</small><input type="date" data-log-until value="${escapeHtml(detail.until)}"/></label>
        <button type="button" class="wb-log-clear" data-log-clear>清除筛选</button>
      </div>`;
    return `<section class="wb-log-detail" data-wb-key="log-detail">
      <div class="wb-log-head"><button type="button" class="wb-btn" data-log-back>← 返回${this.logReturnLane !== null ? escapeHtml(names[this.logReturnLane] ?? "") : "日志列表"}</button>
        <b data-log-title tabindex="-1">${escapeHtml(src?.display ?? detail.source)}</b>
        <span class="wb-muted">· ${escapeHtml(cats.find((c) => c.category === detail.category)?.display || detail.category || (detail.status === "registry" ? "类别未知" : "无运行日志类别"))}</span>
        <button type="button" class="wb-btn" data-wb-log-detail-refresh ${detail.loading ? "disabled" : ""}>刷新来源</button>
        <button type="button" class="wb-btn wb-ghost" data-log-wrap>${detail.wrap ? "不换行查看" : "恢复换行"}</button>
      </div>
      <div class="wb-log-tabs">${tabs || (detail.status === "registry" ? "" : `<span class="wb-muted">该来源没有运行日志类别</span>`)}</div>
      ${genHtml}
      ${stateHtml}
      ${this.logFiltersHtml(detail.rows, detail.status === "ok")}
      ${cats.length ? filterHtml : ""}
      ${this.logDetailRowsHtml(detail)}
    </section>`;
  }
  /** Reading-state line (R9-R38): every non-success gets its own honest
   *  note; truncated windows name the covered byte range. */
  private logDetailStatusHtml(detail: LogDetailState): string {
    const bits: string[] = [];
    const map: Record<string, string> = {
      missing: "日志文件不存在（该链可能未产生此日志）",
      denied: "权限不足",
      unconfigured: "该来源没有可读的运行日志类别",
      registry: "日志来源注册表读取失败，无法确定该来源的日志类别",
    };
    if (detail.status === "loading") bits.push(`读取中…`);
    else if (detail.status === "ok") {
      // C4：这里的计数必须与表格实际列出的行一致——表格只列当前分类筛选
      // 下的行，所以读数也写筛选后 / 全部，并写清被筛掉多少。
      const shown = this.logKindFilteredRows(detail.rows).length;
      if (!detail.rows.length) bits.push("没有记录");
      else if (shown === detail.rows.length)
        bits.push(
          `最近读取 ${escapeHtml(detail.fetchedAt ?? "")} · 本次窗口 ${detail.rows.length} 条`
        );
      else
        bits.push(
          `最近读取 ${escapeHtml(detail.fetchedAt ?? "")} · 分类筛选「${escapeHtml(this.logKindFilterLabel())}」显示 ${shown} / ${detail.rows.length} 条（筛除 ${detail.rows.length - shown} 条）`
        );
    } else bits.push(map[detail.status] ?? "读取失败");
    if (detail.status === "ok" && !detail.follow && !detail.loading) {
      const label = detail.newCount
        ? `暂停跟随 · 有 ${detail.newCount} 条新日志 · 点击回到最新`
        : "跟随已暂停 · 点击回到最新";
      bits.push(
        `<button type="button" class="wb-btn wb-log-resume" data-log-resume>${escapeHtml(label)}</button>`
      );
    }
    if (detail.errorCode && detail.status !== "ok")
      bits.push(`（code：${escapeHtml(detail.errorCode)}）`);
    if (detail.requestId) {
      // The chip states what the backend confirmed, not what was asked for.
      const what =
        detail.requestEcho === "applied"
          ? `仅显示请求 <code>${escapeHtml(detail.requestId)}</code> 在本读取窗口内的相关记录`
          : detail.requestEcho === "unconfirmed"
            ? `已请求只看 <code>${escapeHtml(detail.requestId)}</code>，但后端未确认按该请求筛选：下面是未筛选的读取窗口`
            : `正在按请求 <code>${escapeHtml(detail.requestId)}</code> 筛选…`;
      bits.push(
        `<span class="wb-log-reqf">${what} <button type="button" class="wb-btn wb-btn-sm" data-log-req="">清除</button></span>`
      );
    }
    if (detail.status === "registry")
      bits.push(
        `<button type="button" class="wb-btn wb-btn-sm" data-log-reopen="${escapeHtml(detail.source)}">重试读取来源注册表</button>`
      );
    if (detail.truncated)
      bits.push(
        `仅覆盖最近 ${detail.scannedBytes.toLocaleString("zh-CN")} 字节窗口`
      );
    if (detail.resetHint)
      bits.push(`<b class="wb-stale-warn">日志已轮转，游标已重置</b>`);
    if (detail.stale)
      bits.push(`<b class="wb-stale-warn">刷新失败，内容可能过时</b>`);
    if (detail.loading) bits.push(`读取中…`);
    return `<p class="wb-log-state">${bits.join(" ") || "读取中…"}</p>`;
  }
  /** Row list (R9-R38): old→new file order; a multi-line error block starts
   *  folded (first line + expand button); highlight is escaped <mark>. */
  /** F12-6 Chinese summary of one run event: what kind of line it is, the
   *  outcome, model, phase, duration and error code - each only when the backend
   *  derived it (null fields are left out, never guessed). "" when the row has
   *  no event to summarise (the raw line is then the reading). */
  private logRowSummaryHtml(row: LogEntryRow): string {
    const e = row.event;
    if (!row.parse_ok || !e) return "";
    const kinds: [RegExp, string][] = [
      [/^stream end\b/, "流式输出结束"],
      [/^request summary\b/, "请求小结"],
      [/^output budget\b/, "输出上限决定"],
      [/^upstream slow\b/, "上游响应慢"],
      [/^request failed\b/, "请求失败"],
    ];
    const kind = kinds.find(([re]) => re.test(row.message))?.[1];
    const outcome =
      e.outcome === "ok"
        ? ["成功", "ok"]
        : e.outcome === "error"
          ? ["失败", "danger"]
          : e.outcome
            ? [e.outcome, "idle"]
            : null;
    const bits: string[] = [];
    if (e.model) bits.push(`模型 ${escapeHtml(e.model)}`);
    if (e.phase) bits.push(`阶段 ${escapeHtml(e.phase)}`);
    if (typeof e.duration_ms === "number" && e.duration_ms > 0)
      bits.push(
        `耗时 ${(e.duration_ms / 1000).toFixed(e.duration_ms >= 10000 ? 0 : 1)} 秒`
      );
    if (e.error_code) bits.push(`错误 ${escapeHtml(e.error_code)}`);
    if (!kind && !outcome && !bits.length) return "";
    return `<p class="wb-log-sum">${kind ? `<b>${escapeHtml(kind)}</b>` : ""}${outcome ? `<span class="wb-badge wb-badge-${outcome[1]}">${escapeHtml(outcome[0]!)}</span>` : ""}${bits.map((b) => `<span>${b}</span>`).join("")}</p>`;
  }
  private logDetailRowsHtml(detail: LogDetailState): string {
    const q = detail.q;
    // C4：身份在整段 detail.rows 上计算（与分类筛选无关），展开 / 复制都按它；
    // 行下标只用于表格位置，不进入按钮值。
    const identities = logRowIdentities(detail.source, detail.rows);
    const rendered = detail.rows
      .map((row, idx) => ({ row, identity: identities[idx]! }))
      .filter(
        ({ row }) =>
          this.logOverviewFilter === "all" ||
          logKind(row) === this.logOverviewFilter
      );
    this.logDetailRenderRows = new Map(
      rendered.map(({ row, identity }) => [identity, row])
    );
    const rows = rendered
      .map(({ row, identity }) => {
        const open = detail.expanded.has(identity);
        const parts = row.message.split("\n");
        // F12-6: the first reading of a row is the Chinese summary of the event the
        // backend parsed; the raw line is folded and copyable. A row the backend
        // could not parse shows its raw first line instead of a summary.
        const summary = this.logRowSummaryHtml(row);
        const shown = open ? row.message : summary ? "" : (parts[0] ?? "");
        // 查看该请求相关记录: only from the backend's own request_id (never a
        // text match on the line), and not on the request already filtered to.
        const rid =
          typeof row.event?.request_id === "string" ? row.event.request_id : "";
        const reqChip =
          rid && rid !== detail.requestId
            ? `<button type="button" class="wb-btn wb-btn-sm wb-log-req" data-log-req="${escapeHtml(rid)}" title="只看该请求在本读取窗口内的相关记录（按服务端 request_id 精确筛选）">查看该请求相关记录</button>`
            : "";
        const toggle =
          summary || parts.length > 1
            ? `<button type="button" class="wb-log-more" ${open ? `data-log-collapse="${escapeHtml(identity)}"` : `data-log-expand="${escapeHtml(identity)}"`}>${open ? `${wbIcon("detail")}收起原文` : `${wbIcon(row.level === "ERROR" ? "alert" : "detail")}${parts.length > 1 ? `展开原文（${parts.length} 行，全文不截断）` : "展开原文"}`}</button>`
            : "";
        // 维护者 2026-09-28（Plan-Revision 9 逐页收纳 · 日志 / 操作页）：长错误 / 详细 JSON
        // 在**单行详情**里展开（上面的 toggle 与 <pre> 都是同一行的），完整值要能**一键复制**：
        // 复制按钮取的是这一行的完整原文（不是首层那个折叠前缀），反馈沿用既有的
        // 「已复制」一次性闪动（flashCopied），所以长错误既能读全，也能拿走。
        const copy = `<button type="button" class="wb-btn wb-btn-sm wb-log-copy" data-wb-log-copy="${escapeHtml(identity)}" title="复制该行完整原文（不截断）" aria-label="复制该行完整原文">${wbIcon("copy")}复制</button>`;
        return `<tr class="wb-log-kind-${logKind(row)}" data-wb-key="log-row:${escapeHtml(identity)}">
        <td><time>${escapeHtml(row.ts ?? "时间未知")}</time></td>
        <td><span class="wb-log-kind">${logKindLabels[logKind(row)]} · ${escapeHtml(row.level || "级别未知")}</span></td>
        <td>${summary}${shown ? `<pre class="wb-log-msg${open ? " open" : ""}${detail.wrap ? "" : " nowrap"}">${q ? this.logHighlightHtml(shown, q) : escapeHtml(shown)}</pre>` : ""}${reqChip}${toggle}</td>
        <td>${copy}</td>
      </tr>`;
      })
      .join("");
    const olderBtn = detail.nextCursor
      ? `<button type="button" class="wb-btn" data-log-older>加载更早记录</button>`
      : detail.resetHint
        ? ""
        : `<span class="wb-muted">没有更早的窗口记录</span>`;
    // C4：空表句点名实际筛选轴（分类 chips）与生效值；关键词 / 日期范围在
    // 服务端就已生效，这里如实区分，不把空窗口写成「没有事件」。
    const emptyText =
      detail.status === "ok"
        ? this.logOverviewFilter === "all"
          ? "本次窗口没有事件；关键词 / 日期范围在服务端筛选，可清除后重试"
          : `本次窗口没有符合分类筛选「${escapeHtml(this.logKindFilterLabel())}」的事件；点上方「全部」可看回全部`
        : "日志尚未读到；状态与覆盖范围见上方";
    return `<div class="wb-log-rows wb-table-scroll wb-log-event-table" data-wb-key="log-rows" data-wb-scroll="log-rows" data-wb-scroll-for="${escapeHtml(detail.source)}"><table class="wb-table"><thead><tr><th>时间</th><th>分类</th><th>事件与原文</th><th>操作</th></tr></thead><tbody>${rows || `<tr><td colspan="4">${emptyText}</td></tr>`}</tbody></table></div>${olderBtn ? `<div class="wb-log-older">${olderBtn}</div>` : ""}`;
  }
  /** Escaped-then-marked keyword highlight; never feeds raw log text to HTML. */
  private logHighlightHtml(text: string, q: string): string {
    if (!q) return escapeHtml(text);
    const lower = text.toLowerCase();
    const low = q.toLowerCase();
    let out = "";
    let at = 0;
    while (true) {
      const found = lower.indexOf(low, at);
      if (found < 0) break;
      out +=
        escapeHtml(text.slice(at, found)) +
        "<mark>" +
        escapeHtml(text.slice(found, found + q.length)) +
        "</mark>";
      at = found + q.length;
    }
    return out + escapeHtml(text.slice(at));
  }
  // ---------------- model lane (R9-R31): catalog overview + sync
  /** Model lane (R9-R31), 总 + 分: overview counts and pending multi-dimension
   *  changes (add/update/retire/display-name — context is only ONE of the
   *  dimensions) from model.sync_pending; update actions run through the
   *  registry (preview no-write, apply = real managed write with confirm);
   *  per-source model table from model.catalog (four /v1/models sources).
   *  Read-only pulls are manual/once-per-session — every operation submit is
   *  a real ledger row, so the lane never polls these by itself.
   *  维护者 2026-09-28：本车道**不再**用 `!sessionLive()` 早退并断言「8001
   *  不可达」——控制台宿主读口（opsRecent / runtime）没落地只是那一路上没读到，
   *  概览的失败态由本读口自己的真实请求状态（传输 / HTTP / 形状 / 读取中）
   *  在 `modelOverviewHtml` 里写清。 */
  private modelLaneHtml() {
    if (!this.modelOverviewRequested) {
      this.modelOverviewRequested = true;
      // R9-R34: the overview is the high-frequency read of the lane, so it
      // uses the local passthrough endpoint (same shape as model.sync_pending,
      // no operation row, no session needed); write-class actions below stay
      // real POST operations.
      void this.fetchModelPending();
    }
    const pending = this.actionResults["model.sync_pending"];
    const probeQueue = this.actionResults["model.context_probe_preview"];
    // Rev17: syncing belongs to the 全部模型 view itself - no separate 客户端同步
    // tab. "sync" is the catalog with its sync tool area open above the table.
    const tabs = `<div class="wb-section-tabs" aria-label="模型管理分区">${(
      [
        ["catalog", "模型目录"],
        ["probe", "测速与探测"],
      ] as const
    )
      .map(
        ([id, label]) =>
          `<button data-wb-model-section="${id}" aria-pressed="${id === "catalog" ? this.modelSection !== "probe" : this.modelSection === id}">${label}</button>`
      )
      .join("")}</div>`;
    if (this.modelSection !== "probe") {
      const syncOpen = this.modelSection === "sync";
      // §11.1（2026-10-10）主入口：刷新模型 / 同步到客户端 / 更多；搜索、来源筛选
      // 与模型选择保留；「探测队列…」收进「测试模型」（§11.4）与「测速与探测」分区。
      // 维护者 2026-09-28（无障碍口径）：纯图标按钮已有 `aria-label` 与
      // `.wb-icon-hint` 提示板，所以**不加**原生 `title`——两者会先后出现，
      // 是双重悬停提示（同 accountActionsHtml 的账号卡详情按钮）。
      const toolbar = `<div class="wb-section-toolbar wb-model-actions"><div class="wb-model-ops"><button class="wb-btn wb-btn-sm" data-wb-model-action="model.catalog" aria-label="刷新模型">${wbIcon("refresh")}刷新模型</button><button class="wb-btn wb-btn-sm${syncOpen ? "" : " wb-action-primary"}" data-wb-model-section="${syncOpen ? "catalog" : "sync"}" aria-expanded="${syncOpen}">${syncOpen ? "收起同步" : "同步到客户端…"}</button><button class="wb-btn wb-btn-sm" data-wb-model-lane-more="1" aria-expanded="${this.modelLaneMoreOpen}">${this.modelLaneMoreOpen ? "收起更多" : "更多…"}</button></div></div>`;
      // §11.3：一个窗口：编辑 → 保存设置（待同步） → 同步到客户端 → 查看变更
      // → 确认同步。范围确认后自动生成只读预览（不再另找预览按钮）。
      const syncArea = syncOpen
        ? `<section class="wb-block wb-gw-sync" aria-label="同步到客户端">` +
          this.modelSyncWindowHtml() +
          this.modelOverviewHtml(pending) +
          `</section>`
        : "";
      return (
        tabs +
        toolbar +
        (this.modelLaneMoreOpen ? this.modelLaneMoreHtml() : "") +
        syncArea +
        this.modelResultChipHtml() +
        this.modelCatalogHtml(pending)
      );
    }
    if (this.modelSection === "probe") {
      return (
        tabs +
        `<section class="wb-probe-intro"><h3>实测能力，单独确认消耗</h3><details class="wb-secondary-details"${this.probeIntroOpen ? " open" : ""} data-wb-probe-intro="1"><summary title="探针说明（只读预览 / 写入边界 · 预算与二次确认）· 点击展开 / 收起">探针说明${wbFoldMark()}</summary><p class="wb-muted">同步从不发起模型请求，探测与同步彻底分开。这里可以查看待探测模型与预算；单独确认执行的测速、思考档位校验与上下文探测，等后端提供独立的探测动作后在此开放（原「应用并探测」组合入口已删除）。</p></details><div class="wb-model-ops"><button class="wb-action-primary" data-wb-model-action="model.context_probe_preview">查看探测队列</button><button data-wb-model-section="catalog">返回目录单项测速</button></div></section>` +
        this.modelResultChipHtml() +
        this.modelProbeQueueHtml(pending, probeQueue)
      );
    }
    return tabs;
  }

  /** §11.4（2026-10-10）车道级「更多」：同步历史 / 任务、定时同步（只读）
   *  等低频功能；「恢复默认」在目录里选中模型后由该模型的「更多」承担
   *  （明确恢复哪些字段与值，形成可预览草稿、保留历史证据、不直接发布）。 */
  private modelLaneMoreHtml(): string {
    const history = this.actionResults["model.sync_history"];
    const task = this.actionResults["model.task_status"];
    return (
      `<details class="wb-secondary-details"${this.modelLaneMoreOpen ? " open" : ""} data-wb-model-lane-more="1"><summary title="更多（低频功能）· 点击展开 / 收起">更多${wbFoldMark()}</summary>` +
      `<div class="wb-model-ops"><button type="button" class="wb-btn wb-btn-sm" data-wb-model-action="model.history" aria-label="刷新同步历史">${wbIcon("refresh")}同步历史</button><button type="button" class="wb-btn wb-btn-sm" data-wb-model-action="model.task_status">定时同步 / 任务状态</button></div>` +
      (history === undefined
        ? `<p class="wb-muted">同步历史尚未读取（点「同步历史」只读读取；未读取不等于没有历史）。</p>`
        : this.modelHistoryHtml()) +
      (task === undefined
        ? `<p class="wb-muted">定时同步 / 任务状态尚未读取（点按钮只读读取）。</p>`
        : this.modelTaskHtml()) +
      `<p class="wb-muted">定时同步只消费已应用设置，不发布待同步草稿；同步从不发起模型请求。「恢复默认」在目录里选中模型后由该模型的「更多」承担：明确恢复显示名称 / 上下文长度 / 默认思考档位为跟随来源，形成可预览草稿、保留探测历史证据、不直接发布。</p>` +
      `</details>`
    );
  }
  /** §11.3（2026-10-10）统一同步窗口：编辑 → 保存设置（待同步） → 同步到
   *  客户端 → 查看变更 → 确认同步。
   *  · 一个窗口选目标与范围（单网关默认本来源；全部模型明确「全部 / 只同步
   *    选中」，后者空选拒绝）；打开窗口本身不写配置；
   *  · 范围确认后自动生成只读预览（不再另找预览按钮）；
   *  · 预览列出新增 / 更新 / 移除、已保存草稿的名称 / 上下文 / 档位差异、
   *    客户端冲突、延期 / 失败原因与未保存编辑提示；
   *  · 「确认同步」一次发布预览所绑定的范围 / 目标 / 草稿版本（preview_token
   *    + selection）；范围、草稿、目录或客户端输入任一变化使旧预览失效；
   *  · 取消 / 失败 / 延期保留待同步草稿与旧 applied（后端契约）。
   *  后端组合契约：model.sync_preview / model.sync_apply（scope_mode 必填；
   *  selection = {source|model_id: 草稿版本}，应用必须带回 preview_token）。 */
  private modelSyncWindowHtml(source?: string): string {
    const ready = this.syncWindowScopeReady(source);
    const scope = ready.scope;
    const scopeSig = this.scopeSignature(scope);
    const readState = this.msReadState();
    const pending = this.msPendingSelection(scope);
    const preview = this.actionResults["model.sync_preview"];
    const previewBusy = this.actionInFlight("model.sync_preview");
    const applyBusy = this.actionInFlight("model.sync_apply");
    const bind = this.syncBind;
    const scopeChanged = !!bind && bind.scopeSig !== scopeSig;
    const selectionChanged =
      !!bind &&
      pending !== null &&
      this.selectionSignature(pending.selection) !== bind.selectionSig;
    const previewStale = this.previewStale && !!preview;
    const stale = scopeChanged || selectionChanged || previewStale;
    const targetLine = source
      ? `来源：${escapeHtml(this.sourceLabel(source))} · 目标：已配置的客户端`
      : `跨来源同步到已配置的客户端`;
    const chooser = source
      ? ""
      : `<div class="wb-model-settings-ctx" role="radiogroup" aria-label="同步范围"><span>范围</span>` +
        `<label><input type="radio" name="wb-sync-mode" data-wb-sync-mode="all"${this.modelScopeCustom ? "" : " checked"}/>全部模型</label>` +
        `<label><input type="radio" name="wb-sync-mode" data-wb-sync-mode="selected"${this.modelScopeCustom ? " checked" : ""}/>只同步选中</label></div>` +
        (this.modelScopeCustom ? this.modelScopePanelHtml() : "");
    const draftNote =
      pending === null
        ? `<p class="wb-muted">本地设置${readState === "unread" ? "尚未读取" : "读取异常"}。点击「查看变更」先读取设置，避免遗漏待同步草稿。</p>`
        : (Object.keys(pending.selection).length
            ? `<p class="wb-muted">本次包含 ${Object.keys(pending.selection).length} 个待同步设置。</p>`
            : `<p class="wb-muted">没有待同步设置，将检查目录中的模型变化。</p>`) +
          (pending !== null && pending.unknown.length
            ? `<p class="wb-stale-warn">有 ${pending.unknown.length} 个草稿的版本读不出来（不静默消费）：${escapeHtml(pending.unknown.slice(0, 8).join("、"))}${pending.unknown.length > 8 ? " 等" : ""}；请刷新设置读面后重试。</p>`
            : "");
    const outOfScope = this.msOutOfScopeDraftCount(scope);
    const outOfScopeNote =
      outOfScope === null
        ? ""
        : outOfScope
          ? `<p class="wb-muted">范围外待同步草稿 ${outOfScope} 个保留（不在本次 selection 内，不被消费）。</p>`
          : "";
    // 目录里已不存在该模型的草稿：不纳入本次发布（纳入会把整轮预览顶失败），
    // 也绝不静默丢掉——窗口如实列出，应用时由后端清理记录并在结果里回报。
    const staleDraftNote =
      pending !== null && pending.stale.length
        ? `<p class="wb-stale-warn">有 ${pending.stale.length} 个草稿的模型已不在目录（草稿无法发布，本次不纳入；应用时会清理记录）：${escapeHtml(pending.stale.slice(0, 8).join("、"))}${pending.stale.length > 8 ? " 等" : ""}。</p>`
        : "";
    const pickedKey = this.modelPick;
    const pickedCtx = pickedKey
      ? this.msContextState(pickedKey)
      : { state: "unread" as const, ctx: null };
    const dirty =
      pickedKey && pickedCtx.state === "ok"
        ? this.msChangedFields(pickedKey, pickedCtx.ctx)
        : null;
    const dirtyNote =
      dirty && dirty.changed.length
        ? `<p class="wb-stale-warn">未保存编辑：当前选中模型 ${escapeHtml(pickedKey!)} 有未保存改动（${escapeHtml(dirty.changed.join("、"))}），不会进入本次发布；请先「保存设置」形成待同步草稿。</p>`
        : "";
    const staleNote = stale
      ? `<p class="wb-stale-warn">旧预览已失效（${scopeChanged ? "范围已变" : selectionChanged ? "草稿已变" : "应用后目录已变"}）：以下读数不再是本次预览的结果，也不复用旧 preview_token；请重新「确认范围并生成预览」。</p>`
      : "";
    const scopeLine = `<p class="wb-muted">本次范围：${escapeHtml(this.scopeSummary(scope))}${ready.reason ? ` · <b class="wb-stale-warn">${escapeHtml(ready.reason)}</b>` : ""}</p>`;
    const confirmRange =
      `<div class="wb-model-ops"><button type="button" class="wb-btn wb-btn-sm" data-wb-sync-confirm="1" data-wb-sync-source="${escapeHtml(source ?? "")}"${previewBusy || ready.reason ? " disabled" : ""} title="${escapeHtml(ready.reason || "只读生成预览（model.sync_preview）；不写客户端配置、不发模型请求")}">${wbIcon("refresh")}查看变更</button></div>` +
      (previewBusy
        ? `<p class="wb-muted">预览生成中…（只读；未写入任何配置）</p>`
        : "");
    const pv =
      preview && typeof preview === "object" && !Array.isArray(preview)
        ? (preview as Record<string, unknown>)
        : null;
    const inner =
      pv?.preview &&
      typeof pv.preview === "object" &&
      !Array.isArray(pv.preview)
        ? (pv.preview as Record<string, unknown>)
        : null;
    const plan =
      inner && Array.isArray(inner.plan)
        ? (inner.plan as unknown[]).map(String)
        : null;
    const warnings =
      inner && Array.isArray(inner.warnings)
        ? (inner.warnings as unknown[]).map(String)
        : [];
    const changes =
      inner && Array.isArray(inner.changes)
        ? (inner.changes as unknown[]).filter(
            (c): c is Record<string, unknown> =>
              !!c && typeof c === "object" && !Array.isArray(c)
          )
        : [];
    const conflicts =
      inner && Array.isArray(inner.contextConflicts)
        ? (inner.contextConflicts as unknown[]).filter(
            (c): c is Record<string, unknown> =>
              !!c && typeof c === "object" && !Array.isArray(c)
          )
        : [];
    const retirement =
      inner && Array.isArray(inner.pendingRetirement)
        ? (inner.pendingRetirement as unknown[]).filter(
            (c): c is Record<string, unknown> =>
              !!c && typeof c === "object" && !Array.isArray(c)
          )
        : [];
    const notices =
      inner && Array.isArray(inner.overrideNotices)
        ? (inner.overrideNotices as unknown[]).map(String)
        : [];
    const token =
      pv && typeof pv.preview_token === "string" ? pv.preview_token : "";
    const boundSelection =
      pv &&
      pv.selection &&
      typeof pv.selection === "object" &&
      !Array.isArray(pv.selection)
        ? (pv.selection as Record<string, unknown>)
        : null;
    // 预览回带的 selection 逐键如实写出（版本读不出来写「未知」，绝不折成 0）。
    const canPublish =
      !!bind &&
      !stale &&
      !applyBusy &&
      !!token &&
      (pending === null || !pending.unknown.length);
    const publishTitle = !bind
      ? "先「确认范围并生成预览」（发布必须带回预览绑定的范围 / 草稿版本 / preview_token）"
      : stale
        ? "预览已失效：请重新确认范围并生成预览"
        : applyBusy
          ? "同步进行中…"
          : pending !== null && pending.unknown.length
            ? "有草稿版本读不出来：不静默消费，请刷新设置读面"
            : "确认同步：一次发布预览所绑定的范围 / 目标 / 草稿版本（写客户端配置，不发探测）";
    const previewMatches = !!bind && !stale;
    const previewHtml =
      pv && previewMatches
        ? `<div class="wb-svc-detail"><h4>同步预览 · 尚未应用${stale ? `<span class="wb-count-chip wb-stale-chip">已失效</span>` : ""}</h4>` +
          `<p class="wb-muted">${escapeHtml(this.scopeSummary(scope))} · ${boundSelection ? Object.keys(boundSelection).length : 0} 个待同步设置</p>` +
          (plan
            ? plan.length
              ? `<div class="wb-sync-diff"><strong>本次范围变更（新增 / 更新 / 移除 · ${plan.length} 项）</strong><ul>${plan.map((d) => `<li>${escapeHtml(d)}</li>`).join("")}</ul></div>`
              : `<p class="wb-muted">按实时目录与受管配置重算：无配置变化。</p>`
            : `<p class="wb-muted">变更计划未知：响应未返回 plan 明细（不把它当成「无配置变化」）。</p>`) +
          (changes.length
            ? `<div class="wb-sync-diff"><strong>已保存草稿差异（名称 / 上下文 / 档位 · ${changes.length} 项）</strong><ul>${changes.map((c) => `<li>${escapeHtml(this.msChangeLine(c))}</li>`).join("")}</ul></div>`
            : "") +
          (conflicts.length
            ? `<div class="wb-sync-diff"><strong>客户端冲突（${conflicts.length} 项 · 未裁定前不写回）</strong><ul>${conflicts.map((c) => `<li>${escapeHtml(this.msConflictLine(c))}</li>`).join("")}</ul></div>`
            : "") +
          (retirement.length
            ? `<div class="wb-sync-diff"><strong>待退役 / 待应用（${retirement.length} 项）</strong><ul>${retirement.map((d) => `<li>${escapeHtml(String(d.description ?? d.kind ?? "").slice(0, 200))}</li>`).join("")}</ul></div>`
            : "") +
          (notices.length
            ? `<div class="wb-sync-diff"><strong>覆盖生效通知</strong><ul>${notices.map((n) => `<li>${escapeHtml(n)}</li>`).join("")}</ul></div>`
            : "") +
          (warnings.length
            ? `<div class="wb-sync-diff"><strong>延期 / 失败原因（来源警告）</strong><ul>${warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul></div>`
            : "") +
          staleNote +
          dirtyNote +
          `</div>`
        : pv
          ? `<p class="wb-muted">范围或设置已变化，请重新点击「查看变更」。</p>`
          : `<p class="wb-muted">选择范围后点击「查看变更」。预览不会写入客户端。</p>`;
    const applyHtml = this.modelSyncApplyHtml();
    return (
      `<div class="wb-sync-window">` +
      `<div class="wb-sync-toolbar"><div><h3>同步到客户端</h3><p class="wb-muted">${targetLine}</p></div></div>` +
      chooser +
      scopeLine +
      draftNote +
      outOfScopeNote +
      staleDraftNote +
      confirmRange +
      previewHtml +
      `<div class="wb-sync-publish"><button type="button" class="wb-btn wb-action-primary" data-wb-sync-publish="1" data-wb-sync-source="${escapeHtml(source ?? "")}"${canPublish ? "" : " disabled"} title="${escapeHtml(publishTitle)}">确认同步</button></div>` +
      `<p class="wb-muted">确认后写入客户端配置，不发起模型请求。范围或设置变化后需重新预览；失败或延期时保留待同步设置。</p>` +
      applyHtml +
      `</div>`
    );
  }
  /** §11.3.3 应用结果：逐目标如实报告（成功 / 延期 / 失败），多目标不制造
   *  跨目标原子成功承诺；读回与状态面分开；延期 / 失败保留待同步草稿与旧
   *  applied。 */
  private modelSyncApplyHtml(): string {
    const result = this.actionResults["model.sync_apply"];
    if (!result) return "";
    const str = (value: unknown): string =>
      typeof value === "string" ? value : "";
    const list = (value: unknown): unknown[] =>
      Array.isArray(value) ? value : [];
    const rows = (value: unknown): Record<string, unknown>[] =>
      list(value).filter(
        (r): r is Record<string, unknown> =>
          !!r && typeof r === "object" && !Array.isArray(r)
      );
    const status = str(result.status);
    const summary = str(result.summary);
    const applied = list(result.applied).map(String);
    const pending = list(result.pending).map(String);
    const appliedDetails = rows(result.appliedDetails);
    const pendingDetails = rows(result.pendingDetails);
    const conflicts = rows(result.contextConflicts);
    const settingsApplied = list(result.settingsApplied).map(String);
    const settingsDeferred = list(result.settingsDeferred).map(String);
    const readback =
      result.settingsReadback &&
      typeof result.settingsReadback === "object" &&
      !Array.isArray(result.settingsReadback)
        ? (result.settingsReadback as Record<string, unknown>)
        : null;
    const readbackRows = readback ? rows(readback.keys) : [];
    const boundary =
      result.applicationBoundary &&
      typeof result.applicationBoundary === "object" &&
      !Array.isArray(result.applicationBoundary)
        ? (result.applicationBoundary as Record<string, unknown>)
        : null;
    const line = (row: Record<string, unknown>): string =>
      escapeHtml(
        String(
          row.description ?? `${row.kind ?? ""} ${row.model_id ?? ""}`
        ).slice(0, 200)
      );
    return (
      `<div class="wb-svc-detail"><h4>同步结果（逐目标如实报告）</h4>` +
      `<p class="wb-muted">${escapeHtml(summary || "同步已完成")}${status ? `（status=${escapeHtml(status)}）` : ""}</p>` +
      (appliedDetails.length
        ? `<div class="wb-sync-diff"><strong>已应用（${appliedDetails.length} 项）</strong><ul>${appliedDetails.map((d) => `<li>${line(d)}</li>`).join("")}</ul></div>`
        : applied.length
          ? `<div class="wb-sync-diff"><strong>已应用（${applied.length} 项）</strong><ul>${applied.map((d) => `<li>${escapeHtml(d)}</li>`).join("")}</ul></div>`
          : "") +
      (pendingDetails.length
        ? `<div class="wb-sync-diff"><strong>待应用 / 延期（${pendingDetails.length} 项 · 保留待同步草稿与旧已应用值）</strong><ul>${pendingDetails.map((d) => `<li>${line(d)}</li>`).join("")}</ul></div>`
        : pending.length
          ? `<div class="wb-sync-diff"><strong>待应用（${pending.length} 项）</strong><ul>${pending.map((d) => `<li>${escapeHtml(d)}</li>`).join("")}</ul></div>`
          : "") +
      (settingsApplied.length || settingsDeferred.length
        ? `<div class="wb-sync-diff"><strong>设置草稿发布</strong><ul>${settingsApplied.map((k) => `<li>${escapeHtml(k)}：已应用（草稿已转已应用、草稿清空）</li>`).join("")}${settingsDeferred.map((k) => `<li>${escapeHtml(k)}：延期（新草稿保留、旧已应用值不变）</li>`).join("")}</ul></div>`
        : "") +
      (readback
        ? `<div class="wb-sync-diff"><strong>写后读回（状态面与客户端实际值分开）</strong><ul><li>客户端读回${readback.clientReadbackAvailable === true ? "可用" : "不可用（读面未返回客户端实际值）"}${readback.filesVerified === true ? " · 文件已写并读回一致" : readback.filesVerified === false ? " · 文件读回不一致（见警告）" : ""}</li>${readbackRows.map((r) => `<li>${escapeHtml(`${r.source ?? ""}|${r.model_id ?? ""}`)}：已应用 ${typeof r.effective_context === "number" ? r.effective_context.toLocaleString("zh-CN") : "未知"} · 客户端 ${typeof r.client_context === "number" ? r.client_context.toLocaleString("zh-CN") : "未知（读回不可用）"}</li>`).join("")}</ul></div>`
        : "") +
      (conflicts.length
        ? `<div class="wb-sync-diff"><strong>客户端冲突（${conflicts.length} 项 · 未裁定前不写回）</strong><ul>${conflicts.map((c) => `<li>${escapeHtml(this.msConflictLine(c))}</li>`).join("")}</ul></div>`
        : "") +
      (boundary
        ? `<p class="wb-muted">发布状态：${boundary.published === true ? "已写入客户端配置" : "未写入客户端配置"}。客户端加载与会话采用：${boundary.clientLoaded === "verified" ? "已核验加载，会话采用仍需确认" : "尚未核验"}。</p>`
        : "") +
      `</div>`
    );
  }
  /** 本次范围外的待同步草稿数（原样保留、不被消费）：读面未读取 / 形状不可
   *  识别时返回 null（未知，不折成 0）。 */
  private msOutOfScopeDraftCount(
    scope: Record<string, unknown>
  ): number | null {
    const raw = this.actionResults["model.context_overrides"];
    if (raw === undefined) return null;
    let rows: ContextView[];
    try {
      rows = normalizeContexts(raw);
    } catch {
      return null;
    }
    const lists = this.scopeLists(scope);
    let count = 0;
    for (const row of rows) {
      if (!row.draft) continue;
      if (this.msKeyInScope(`${row.source}|${row.modelId}`, lists)) continue;
      count += 1;
    }
    return count;
  }
  /** Restriction payload for preview/apply when custom scope mode is on.
   *  Empty selections mean 全部 — matching the backend's own default so the
   *  written face never silently widens beyond what the user checked.
   *
   *  TRANSPORT ENCODING (2026-09-26): the backend's shared param vocabulary
   *  has no list type. `console_adapters._MODEL_SYNC_SCOPE_SCHEMA` declares
   *  `sources`/`consumers` as `str`, and
   *  `console_operations._validate_params` rejects any other type BEFORE the
   *  handler runs (`invalid_params / sources must be a string`). The list-shaped
   *  values therefore travel as JSON array strings — the exact encoding the
   *  handler parses (`console_model_actions._console_scope` →
   *  `model_sync._normalize_scope`). A raw array here was rejected at the
   *  door, so 应用同步 with a checked 来源/客户端 never reached the handler.
   *  `models` stays a plain object {source: [model_id, ...]} as the schema
   *  declares. Checkbox presentation is unchanged; the request now explicitly
   *  distinguishes all from selected, including an empty selected scope. */
  private modelScopeParams(): Record<string, unknown> {
    if (!this.modelScopeCustom) return encodeSyncScope({ mode: "all" });
    const models: Record<string, string[]> = {};
    if (this.modelChecked.size) {
      for (const key of this.modelChecked) {
        const at = key.indexOf("|");
        if (at <= 0) continue;
        const source = key.slice(0, at);
        const modelId = key.slice(at + 1);
        const list = models[source] ?? [];
        list.push(modelId);
        models[source] = list;
      }
    }
    // Empty selected scopes stay explicitly selected. The backend refuses
    // them; rendering an empty selection must never broaden it to all models.
    return {
      scope_mode: "selected",
      ...(this.modelScopeSources.size
        ? { sources: JSON.stringify([...this.modelScopeSources].sort()) }
        : {}),
      ...(this.modelScopeConsumers.size
        ? { consumers: JSON.stringify([...this.modelScopeConsumers].sort()) }
        : {}),
      ...(Object.keys(models).length ? { models } : {}),
    };
  }
  /** 提交时范围快照：深拷贝（`modelScopeParams()` 每次返回新对象，这里再拷
   *  一层，快照与之后任何勾选/编码改动都不共享引用）。params 只含字符串
   *  与「来源 → 模型 id 列表」两层，按 JSON 往返安全。 */
  private snapshotScope(
    params: Record<string, unknown>
  ): Record<string, unknown> {
    return JSON.parse(JSON.stringify(params)) as Record<string, unknown>;
  }
  /** 解析一个范围参数对象（构造器产物或提交时快照）的三个维度。
   *  `null` = 该键未提交（= 全部）；`"unknown"` = 键存在但形状读不出来
   *  （写「未知」，不折成「全部」）。 */
  private scopeLists(params: Record<string, unknown> | null): {
    sources: ScopeList;
    consumers: ScopeList;
    models: ScopeList;
  } {
    const readList = (value: unknown): ScopeList => {
      if (value === undefined) return null;
      if (typeof value !== "string") return "unknown";
      try {
        const parsed: unknown = JSON.parse(value);
        return Array.isArray(parsed) &&
          parsed.every((v) => typeof v === "string")
          ? (parsed as string[])
          : "unknown";
      } catch {
        return "unknown";
      }
    };
    let models: ScopeList = null;
    const rawModels = params ? params.models : undefined;
    if (rawModels !== undefined) {
      if (
        !rawModels ||
        typeof rawModels !== "object" ||
        Array.isArray(rawModels)
      ) {
        models = "unknown";
      } else {
        const keys: string[] = [];
        for (const [source, ids] of Object.entries(
          rawModels as Record<string, unknown>
        )) {
          if (!Array.isArray(ids) || ids.some((id) => typeof id !== "string")) {
            models = "unknown";
            break;
          }
          keys.push(...(ids as string[]).map((id) => `${source}|${id}`));
        }
        if (models !== "unknown") models = keys;
      }
    }
    return {
      sources: readList(params ? params.sources : undefined),
      consumers: readList(params ? params.consumers : undefined),
      models,
    };
  }
  /** 范围参数 → 一句人话（面板与确认行共用）。`null` 维度写「全部」（后端
   *  默认），读不出来的维度写「未知」——两者绝不混同。 */
  private scopeSummary(params: Record<string, unknown> | null): string {
    if (!params) return "未知（提交时快照缺失）";
    const lists = this.scopeLists(params);
    const text = (value: ScopeList, label: (v: string) => string): string =>
      value === "unknown"
        ? "未知"
        : value === null
          ? "全部"
          : value.map(label).join("、") || "全部";
    return (
      `来源 ${text(lists.sources, (s) => this.sourceLabel(s))}` +
      ` · 客户端 ${text(lists.consumers, (v) => v)}` +
      ` · 勾选模型 ${lists.models === "unknown" ? "未知" : lists.models === null ? "全部" : `${lists.models.length} 个`}`
    );
  }
  /** 范围参数的规范签名，只用于**本地比较**（预览提交时的范围 vs 当前勾选）：
   *  数组先排序，所以勾选顺序变化不算范围变化；不改变提交给后端的编码。 */
  private scopeSignature(params: Record<string, unknown> | null): string {
    if (!params) return "";
    const lists = this.scopeLists(params);
    const part = (value: ScopeList): string =>
      value === "unknown"
        ? "?"
        : value === null
          ? "*"
          : [...value].sort().join(",");
    return `${part(lists.sources)}|${part(lists.consumers)}|${part(lists.models)}`;
  }
  /** 回读 URL 的范围查询串：与应用提交的范围参数**同值**（字符串原样、
   *  对象按 JSON）。后端读口当前不解析它（usagesrv.py:3209 只给 handler
   *  传 `{}`），所以这串是**准确的接口依赖记录**：同范围请求已发出，响应
   *  是否为范围结果由 `modelReadbackScopeHtml` 按实测写出，这里不假设。 */
  private scopeQuery(scope: Record<string, unknown> | null): string {
    if (!scope) return "";
    const query = new URLSearchParams();
    for (const key of ["sources", "consumers", "models"]) {
      const value = scope[key];
      if (typeof value === "string") query.set(key, value);
      else if (value && typeof value === "object")
        query.set(key, JSON.stringify(value));
    }
    const qs = query.toString();
    return qs ? `?${qs}` : "";
  }
  private modelScopePanelHtml(): string {
    const srcChip = (src: string) =>
      `<label class="wb-scope-check"><input type="checkbox" data-model-src-check value="${src}"${this.modelScopeSources.has(src) ? " checked" : ""}/>${escapeHtml(this.sourceLabel(src))}</label>`;
    return (
      `<div class="wb-svc-detail"><h4>同步范围（不勾选 = 全部；勾选模型在下方目录表选择）</h4>` +
      `<div class="wb-scope-row"><small>来源</small>${["um", "qoder_cn", "qoder_global", "cliproxy"].map(srcChip).join("")}</div>` +
      `<div class="wb-scope-row"><small>客户端</small><label class="wb-scope-check"><input type="checkbox" data-model-consumer-check value="DSH"${this.modelScopeConsumers.has("DSH") ? " checked" : ""}/>DSH</label><label class="wb-scope-check"><input type="checkbox" data-model-consumer-check value="ZCode"${this.modelScopeConsumers.has("ZCode") ? " checked" : ""}/>ZCode</label></div>` +
      (this.modelChecked.size
        ? `<div class="wb-scope-row"><small>已勾选模型</small><span>${[
            ...this.modelChecked,
          ]
            .slice(0, 12)
            .map((k) => `<code>${escapeHtml(k)}</code>`)
            .join(
              " "
            )}${this.modelChecked.size > 12 ? ` 等 ${this.modelChecked.size} 个` : ""}</span></div>`
        : "") +
      `</div>`
    );
  }
  /** Overview (L-C): per-client pending grouping with appliedDetails fallback,
   *  probe-queue counters, readback-failure honesty. 未读到时的措辞只来自本读口
   *  的真实请求状态（维护者 2026-09-28）：传输失败 / HTTP 非 2xx（实测状态）/
   *  形状错误 / 读取中，绝不写「8001 不可达或受管状态损坏」。
   *  维护者 2026-09-29（模型同步闭环）：应用终态后的自动回读按应用提交时的
   *  范围参数发起；后端读口当前不接收范围参数（usagesrv.py:3209 给 handler
   *  传 `{}`），所以响应是**全局概览**——按实测写出这一点，并按响应里已有的
   *  明确字段（`pending_details[].consumer`，缺席时用描述前缀）把本范围行与
   *  全局待处理分开；归属不了的维度写「未知」并记下接口依赖，不折成 0 条。
   *  计数缺字段一律「未知」，绝不 `?? 0`。 */
  private modelOverviewHtml(pending?: Record<string, unknown>): string {
    if (!pending) {
      const state = this.modelOverviewState;
      const line =
        state === "transport"
          ? "同步概览读取失败（网络失败或服务未运行）；下方动作仍可手动触发。"
          : state === "http"
            ? `同步概览读取失败（HTTP ${this.modelOverviewStatus}）；下方动作仍可手动触发。`
            : state === "shape"
              ? "同步概览读取失败（响应形状与预期不符）；下方动作仍可手动触发。"
              : "同步概览读取中…";
      // 没有旧快照时「操作已完成、最新状态读取失败」这条同样必须可达：
      // 回读失败标记 + 本读口真实失败状态 = 上面那句之外再说清「应用已完成、
      // 只是最新状态没读到」，并给只读重试（不重发写操作）。
      const readbackFailed =
        this.pendingReadbackFailed &&
        (state === "transport" || state === "http" || state === "shape");
      const scopeText = this.modelReadbackScope
        ? `；本次回读请求范围：${escapeHtml(this.scopeSummary(this.modelReadbackScope))}`
        : "";
      return (
        `<p class="wb-muted">${escapeHtml(line)}</p>` +
        (readbackFailed
          ? `<p class="wb-stale-warn">操作已完成，最新状态读取失败（不重发写操作）${scopeText}。<button type="button" class="wb-btn wb-btn-sm" data-wb-model-action="model.readback">重试读取</button></p>`
          : "")
      );
    }
    // 回读进行中：旧读数仍在页面上，必须标出「读取中」，否则它会被当成
    // 本次回读的结果（本读口的真实状态字段，不靠猜）。
    const reading = this.modelOverviewState === "loading";
    const countsRaw = pending.catalog_counts;
    const countsKnown =
      !!countsRaw && typeof countsRaw === "object" && !Array.isArray(countsRaw);
    const counts = (countsKnown ? countsRaw : {}) as Record<string, unknown>;
    const countChips = Object.entries(counts)
      .filter(([k]) => k !== "m365")
      .map(
        ([k, v]) =>
          `<span class="wb-count-chip">${escapeHtml(this.sourceLabel(k))} <b>${this.countText(typeof v === "number" ? v : undefined)}</b></span>`
      )
      .join("");
    const pendingKnown = Array.isArray(pending.pending);
    const pendingList = pendingKnown ? (pending.pending as string[]) : [];
    const details = Array.isArray(pending.pending_details)
      ? (pending.pending_details as SyncDetailRow[])
      : [];
    // `last_result` 与 `last_result_error` 分开读：结果文件读不出来时写「未知」，
    // 绝不把「没读到最近应用结果」写成「最近应用结果没有警告」。
    const lastResult = (pending.last_result ?? {}) as Record<string, unknown>;
    const lastResultKnown =
      !!pending.last_result && typeof pending.last_result === "object";
    const lastResultError =
      typeof pending.last_result_error === "string"
        ? pending.last_result_error
        : "";
    const warnings = Array.isArray(lastResult.warnings)
      ? lastResult.warnings
      : [];
    // Group the pending changes by client (L-C). pendingDetails carries a
    // real consumer field; without it the description prefix (the backend
    // wording "DSH …"/"ZCode …") is the honest grouping key.
    const groups = new Map<string, SyncDetailRow[]>();
    if (details.length) {
      for (const row of details) {
        const consumer = row.consumer || "未标注";
        const list = groups.get(consumer) ?? [];
        list.push(row);
        groups.set(consumer, list);
      }
    } else if (pendingList.length) {
      for (const desc of pendingList) {
        const hit = ["DSH", "ZCode"].find((c) => String(desc).startsWith(c));
        const consumer = hit ?? "未归属";
        const list = groups.get(consumer) ?? [];
        list.push({ description: String(desc) });
        groups.set(consumer, list);
      }
    }
    const kindLabel: Record<string, string> = {
      add: "新增",
      update: "更新",
      remove: "退役",
      provider_name: "显示名",
      pin: "固定",
    };
    const groupHtml = groups.size
      ? [...groups.entries()]
          .map(([consumer, rows]) => {
            const kindCounts = new Map<string, number>();
            for (const r of rows) {
              const k = r.kind ?? "其他";
              kindCounts.set(k, (kindCounts.get(k) ?? 0) + 1);
            }
            const chips = [...kindCounts.entries()]
              .map(
                ([k, n]) =>
                  `<span class="wb-count-chip">${escapeHtml(kindLabel[k] ?? k)} ${n}</span>`
              )
              .join("");
            const lines = rows
              .slice(0, 40)
              .map(
                (r) =>
                  `<li>${escapeHtml(String(r.description ?? `${r.kind ?? ""} ${r.model_id ?? ""}`).slice(0, 200))}</li>`
              )
              .join("");
            const more =
              rows.length > 40
                ? `<li class="wb-muted">…其余 ${rows.length - 40} 项同组</li>`
                : "";
            return `<div class="wb-sync-client"><b>${escapeHtml(consumer)}</b> ${chips}<ul>${lines}${more}</ul></div>`;
          })
          .join("")
      : "";
    const probesKnown =
      !!pending.probes &&
      typeof pending.probes === "object" &&
      !Array.isArray(pending.probes);
    const probeRecords = (probesKnown ? pending.probes : {}) as Record<
      string,
      Record<string, unknown>
    >;
    const probePending = Object.entries(probeRecords).filter(
      ([, r]) => r.status === "pending"
    ).length;
    const probeVerified = Object.values(probeRecords).filter(
      (r) => r.status === "verified"
    ).length;
    // 回读失败 = 回读标记仍在 + 概览读口这一路的真实请求状态是失败（传输 /
    // HTTP / 形状）；「读取中」不算失败。不再借用 modelOverviewFailed 这个已删除的布尔。
    const readbackFailed =
      this.pendingReadbackFailed &&
      (this.modelOverviewState === "transport" ||
        this.modelOverviewState === "http" ||
        this.modelOverviewState === "shape");
    // The backend's lastStatus is an English enum (pending / changed / warning /
    // normal); every other state word on this page is Chinese, so it is mapped
    // here. An UNKNOWN word passes through unchanged (never guessed into a
    // Chinese word the backend did not send).
    const syncStatus: Record<string, string> = {
      pending: "待处理",
      changed: "有变更",
      warning: "有警告",
      normal: "正常",
    };
    const lastStatus =
      pending.last_status === undefined || pending.last_status === null
        ? "—"
        : String(pending.last_status);
    return (
      `<div class="wb-stats-row"><div><small>各源目录数</small><b>${countChips || "—"}</b></div><div><small>最近同步状态</small><b>${escapeHtml(syncStatus[lastStatus] ?? lastStatus)}</b></div><div><small>待处理项</small><b>${pendingKnown ? pendingList.length : "未知"}</b></div><div><small>待探测（能力）</small><b>${probesKnown ? probePending : "未知"}</b></div><div><small>已实测（能力）</small><b>${probesKnown ? probeVerified : "未知"}</b></div></div>` +
      (reading
        ? `<p class="wb-muted">同步概览读取中…（以下为上一次读数）</p>`
        : "") +
      (readbackFailed
        ? `<p class="wb-stale-warn">操作已完成，最新状态读取失败（不重发写操作）${this.modelReadbackScope ? `；本次回读请求范围：${escapeHtml(this.scopeSummary(this.modelReadbackScope))}` : ""}。<span class="wb-count-chip wb-stale-chip">陈旧：以下读数与应用前的状态</span><button type="button" class="wb-btn wb-btn-sm" data-wb-model-action="model.readback">重试读取</button></p>`
        : "") +
      (readbackFailed
        ? ""
        : this.modelReadbackScopeHtml(
            pending,
            pendingKnown,
            pendingList,
            details
          )) +
      (pendingKnown
        ? pendingList.length
          ? `<div class="wb-sync-diff"><strong>待处理变更（按客户端分组：新增/更新/退役/显示名——上下文只是其中之一）</strong>${groupHtml || `<ul>${pendingList.map((d) => `<li>${escapeHtml(String(d))}</li>`).join("")}</ul>`}</div>`
          : `<p class="wb-muted">当前无待处理的模型变更。</p>`
        : `<p class="wb-muted">待处理变更未知：读口未返回 pending 字段（不把它当成「当前无待处理的模型变更」）。</p>`) +
      (lastResultKnown
        ? warnings.length
          ? `<div class="wb-sync-diff"><strong>最近应用结果 · 警告</strong><ul>${warnings.map((w) => `<li>${escapeHtml(String(w))}</li>`).join("")}</ul></div>`
          : ""
        : lastResultError
          ? `<p class="wb-muted">最近应用结果未知：结果文件读取失败（${escapeHtml(lastResultError)}）——不把它当成「没有警告」。</p>`
          : `<p class="wb-muted">最近应用结果未知：读口未返回 last_result 字段（不把它当成「没有警告」）。</p>`)
    );
  }
  /** 应用后回读的同范围说明 + 本范围归属（维护者 2026-09-29）：
   *  读口当前不接收范围参数（usagesrv.py:3209 给 handler 传 `{}`），所以响应
   *  是全局概览，这里如实写「全局概览」并按响应里**已有的明确字段**把本范围
   *  行标出来：`pending_details[].consumer` 是真实客户端字段，缺席时用描述
   *  前缀（后端的 "DSH …"/"ZCode …" 措辞）。来源级 / 模型级归属读口不返回
   *  → 写「未知」并记下接口依赖，绝不把「归属不了」折成「本范围 0 条」。 */
  private modelReadbackScopeHtml(
    pending: Record<string, unknown>,
    pendingKnown: boolean,
    pendingList: string[],
    details: SyncDetailRow[]
  ): string {
    const scope = this.modelReadbackScope;
    if (!scope) return "";
    const lists = this.scopeLists(scope);
    const allRows: SyncDetailRow[] = details.length
      ? details
      : pendingList.map((desc) => ({ description: String(desc) }));
    const consumerOf = (row: SyncDetailRow): string =>
      row.consumer ??
      ["DSH", "ZCode"].find((c) =>
        String(row.description ?? "").startsWith(c)
      ) ??
      "";
    let attribution: string;
    if (!pendingKnown) {
      attribution =
        "本范围对应行未知：读口未返回 pending / pending_details（不折成 0 条）。";
    } else if (lists.consumers === "unknown") {
      attribution = "本范围对应行未知：提交时的客户端范围参数形状读不出来。";
    } else {
      const inScope =
        lists.consumers === null
          ? allRows
          : allRows.filter((row) => lists.consumers!.includes(consumerOf(row)));
      const out = allRows.length - inScope.length;
      attribution = `本范围可归属的待处理行 ${inScope.length} 条${out ? `，其余 ${out} 条在范围外` : ""}`;
    }
    const listLen = (value: ScopeList): number | null =>
      value === "unknown" ? null : value === null ? 0 : value.length;
    // 来源级 / 模型级归属：明细行没有 source 字段（`pending_details` 只有
    // consumer/kind/model_id/description；`pending` 只有描述串），所以有来源或
    // 模型范围时这两级写「未知」，绝不把「归属不了」折成「本范围 0 条」。
    const hasDetailRows = details.length > 0;
    const noSource = hasDetailRows
      ? "不返回 source，无法与 source|model_id 范围匹配"
      : "只有描述串，没有来源字段";
    const noModel = hasDetailRows
      ? "不返回 source，无法与 source|model_id 范围匹配"
      : "只有描述串，没有 model_id";
    const sourceNote =
      listLen(lists.sources) === 0
        ? ""
        : `；来源级归属未知（明细行${noSource}）`;
    const modelNote =
      listLen(lists.models) === 0 ? "" : `；模型级归属未知（明细行${noModel}）`;
    return `<p class="wb-muted">应用后回读：请求已带同范围参数（${escapeHtml(this.scopeSummary(scope))}）；后端读口当前不接收范围参数，下面是全局概览。${escapeHtml(attribution)}${sourceNote}${modelNote}。</p>`;
  }
  /** Probe queue (L-E.4): effort-probe pending from sync state + the
   *  context-probe queue with estimates. Estimates come from the server;
   *  an unpriced model stays 未定价, never a fabricated fee. */
  private modelProbeQueueHtml(
    pending?: Record<string, unknown>,
    queueResult?: Record<string, unknown>
  ): string {
    const probes = (pending?.probes ?? {}) as Record<
      string,
      Record<string, unknown>
    >;
    const probeRows = Object.entries(probes)
      .filter(([, r]) => r.status === "pending")
      .map(([key, r]) => {
        const [source, modelId] = key.split("|");
        // 缺 attempts 字段写「未知」，绝不 `?? 0` 印成「已尝试 0/3」。
        const attempts =
          typeof r.attempts === "number" && Number.isFinite(r.attempts)
            ? `${r.attempts}/3`
            : "未知";
        return `<li><code>${escapeHtml(modelId ?? key)}</code>（${escapeHtml(this.sourceLabel(source))}）· 新模型能力待探测 · 已尝试 ${attempts}</li>`;
      })
      .join("");
    // `records` 字段缺席 ≠ 空队列：只有读口真的返回了数组才写「队列为空」。
    const queueKnown = Array.isArray(queueResult?.records);
    const contextQueue = queueKnown
      ? (queueResult!.records as ContextProbeQueueRow[])
      : [];
    const contextRows = contextQueue
      .map((row) => {
        const reqs =
          typeof row.estimated_requests === "number"
            ? row.estimated_requests
            : null;
        const chars =
          typeof row.input_chars_estimate === "number"
            ? row.input_chars_estimate
            : null;
        const ctx =
          typeof row.declared_context === "number"
            ? row.declared_context
            : null;
        return `<tr><td>${escapeHtml(this.sourceLabel(String(row.source ?? "")))}</td><td><code>${escapeHtml(String(row.model_id ?? ""))}</code></td><td class="wb-fee-num">${ctx !== null ? ctx.toLocaleString("zh-CN") : "—"}</td><td>${escapeHtml(String(row.status ?? "—"))}${row.executable === false ? " · 本轮不可执行" : ""}</td><td class="wb-fee-num">${reqs !== null ? String(reqs) : "—"}</td><td class="wb-fee-num">${chars !== null ? chars.toLocaleString("zh-CN") : "—"}</td><td>${escapeHtml(String(row.estimate_note || "按各来源账本计"))}</td></tr>`;
      })
      .join("");
    const budgetNote =
      typeof queueResult?.budget_note === "string"
        ? queueResult.budget_note
        : "";
    return (
      `<section class="wb-block"><h3>待探测队列（能力 / 上下文）</h3>` +
      (probeRows
        ? `<ul class="wb-probe-queue">${probeRows}</ul><p class="wb-muted">能力探测是单独确认的计费动作，从不随同步发出；真实计费执行入口需单独授权与预算（「校验档位」跳队列不是验证成功）。</p>`
        : `<p class="wb-muted">能力探测队列当前为空（新模型或声明变化出现时进入队列）。</p>`) +
      (queueResult
        ? (queueKnown
            ? contextRows
              ? `<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>来源</th><th>模型</th><th class="wb-fee-num">声明上下文</th><th>队列状态</th><th class="wb-fee-num">估算请求数</th><th class="wb-fee-num">输入字符估计</th><th>估算说明</th></tr></thead><tbody>${contextRows}</tbody></table></div>`
              : `<p class="wb-muted">上下文探测队列为空（有声明上下文的模型全部已实测或无声明）。</p>`
            : `<p class="wb-muted">上下文探测队列未知：响应未返回 records 字段（不把它当成空队列）。</p>`) +
          (budgetNote
            ? `<p class="wb-muted">${escapeHtml(budgetNote)}</p>`
            : "") +
          `<p class="wb-muted">真实长输入实测会产生计费请求：清单、请求数与预算须集中批准后才执行；分享测试预算不挪用为上下文探测预算。</p>`
        : `<p class="wb-muted">上下文探测队列未读取（点「探测队列」查询；只读动作）。</p>`) +
      `</section>`
    );
  }
  private modelHistoryHtml(): string {
    const history = this.actionResults["model.sync_history"];
    if (!history) return "";
    const rows = Array.isArray(history.history)
      ? (history.history as SyncHistoryEntry[])
      : [];
    // 缺字段一律「未知」，绝不 `?? 0`：损坏跳过行数读不到时不写「0 行」，
    // 总数读不到时不用「已显示条数」冒充总数。
    const skipped =
      typeof history.skipped === "number" && Number.isFinite(history.skipped)
        ? history.skipped
        : null;
    const total =
      typeof history.total === "number" && Number.isFinite(history.total)
        ? history.total
        : null;
    const line = (e: SyncHistoryEntry) => {
      const count = (value: unknown): string =>
        Array.isArray(value) ? String(value.length) : "未知";
      return `<tr><td>${escapeHtml(String(e.timestamp ?? "—")).slice(0, 19)}</td><td>${escapeHtml(String(e.status ?? "—"))}</td><td class="wb-fee-num">${count(e.applied)}</td><td class="wb-fee-num">${count(e.pending)}</td><td class="wb-fee-num">${count(e.warnings)}</td></tr>`;
    };
    return (
      `<section class="wb-block"><h3>同步历史（最近 ${rows.length} 条 · 共 ${total === null ? "未知" : total} 条${skipped ? ` · ${skipped} 行损坏跳过` : ""}）</h3>` +
      (rows.length
        ? `<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>时间</th><th>状态</th><th class="wb-fee-num">已应用</th><th class="wb-fee-num">待应用</th><th class="wb-fee-num">警告</th></tr></thead><tbody>${rows.map(line).join("")}</tbody></table></div>`
        : `<p class="wb-muted">暂无历史记录。</p>`) +
      `</section>`
    );
  }
  private modelTaskHtml(): string {
    const task = this.actionResults["model.task_status"];
    if (!task) return "";
    return `<section class="wb-block"><h3>同步计划任务</h3><div class="wb-kv">${renderTaskRows(task)}</div></section>`;
    function renderTaskRows(t: Record<string, unknown>): string {
      return Object.entries(t)
        .filter(([, v]) => v !== null && v !== undefined)
        .slice(0, 8)
        .map(
          ([k, v]) =>
            `<span>${escapeHtml(k)}</span><b>${escapeHtml(typeof v === "object" ? JSON.stringify(v).slice(0, 120) : String(v))}</b>`
        )
        .join("");
    }
  }
  /** Catalog table (L-C): search filter, scope checkboxes, per-model speedtest
   *  with honest results, per-binding pin toggles (only when the backend
   *  exposes the binding contract), per-source access status.
   *  Rev17 `onlySource` = 单网关模型页按后端服务身份表过滤本来源（目录读取本身
   *  仍是后端四源只读读取，页面只过滤展示，不另读一遍、也不推算）。 */
  private modelCatalogHtml(
    pending?: Record<string, unknown>,
    onlySource?: string
  ): string {
    const catalog = this.actionResults["model.catalog"];
    // 读取失败记忆（与单网关模型页共用同一张 `actionResultFailures`）：目录读面
    // 本次实测失败时页面必须写失败句，不能把「读取失败」渲染成「尚未读取」。
    const catalogFailure = this.actionResultFailures.get("model.catalog");
    const sourceName = onlySource ? this.sourceLabel(onlySource) : "";
    // PR9 B7（维护者 2026-09-28 逐页落点 · 模型）：M365 暂停读口首层只留短句，
    // 技术缘由（后端口径、暂停范围、历史绑定保留）收进同一个读口的键盘可达详情。
    // `paused_sources` 是 model.sync_pending 的只读字段（console_model_actions.py:209
    // 直接返回 model_sync.PAUSED_SOURCES）；读到就按它写，没读到就只写前端自己的
    // 排除口径——不把「没读到」写成「没有暂停源」，也不臆造故障原因。
    const pausedRaw = pending?.paused_sources;
    const pausedSources = Array.isArray(pausedRaw)
      ? pausedRaw.filter((s): s is string => typeof s === "string")
      : [];
    if (!catalog) {
      const pausedNote = pausedSources.length
        ? `本次概览读口如实返回 paused_sources = ${pausedSources.map((s) => escapeHtml(s)).join("、")}（model.sync_pending 的只读字段），与上面的排除口径一致。`
        : `本次概览读口没有返回 paused_sources（未读取或字段缺失），所以这里只写前端自己的排除口径：目录表按 m365 过滤、同步范围不含 m365。`;
      // 读取失败按既有错误状态如实显示（维护者 2026-10-06 接线）：本次读取失败
      // （`model.catalog` 的实测失败原因）就写失败句 + 重试入口，绝不落成
      // 「尚未读取」或空表；未读取（还没问过）与读取失败是两件事。
      const empty = catalogFailure
        ? onlySource
          ? `<div class="wb-empty-card"><strong class="wb-stale-warn">目录读取失败</strong><small>${escapeHtml(catalogFailure)}。点「刷新本来源目录」重试（只读，不改客户端配置）；失败不等于没有模型，也不等于本来源为空。</small></div>`
          : `<div class="wb-empty-card"><strong class="wb-stale-warn">目录读取失败</strong><small>${escapeHtml(catalogFailure)}。点工具条上的「刷新目录」重试（只读，不改客户端配置）；失败不等于没有模型，也不等于空目录。</small></div>`
        : onlySource
          ? `<div class="wb-empty-card"><strong>目录尚未读取</strong><small>点「刷新本来源目录」读取四源实时目录（只读，不改客户端配置）；本页只列出 ${escapeHtml(sourceName)} 的行，读取结果与失败原因显示在工具条下方，未读取不等于没有模型。</small></div>`
          : `<div class="wb-empty-card"><strong>目录尚未读取</strong><small>点击「刷新模型」读取目录。</small></div>`;
      return (
        `<section class="wb-block wb-char"><h3>${onlySource ? `模型目录（来源 ${escapeHtml(sourceName)} 实时）` : "模型目录（已读取来源）"}${catalogFailure ? " · 读取失败" : ""}</h3>` +
        empty +
        `<details class="wb-model-id-details"${this.modelIdDetailKey === "m365-pause" ? " open" : ""} data-wb-model-pause="1"><summary title="M365 暂停范围与依据（后端只读常量 · 历史绑定保留 · 本页不重新启用）· 点击展开 / 收起">暂停范围与依据${wbFoldMark()}</summary><div>` +
        `<p class="wb-muted">后端只读依据（model_sync.py 的 PAUSED_SOURCES）：<code>PAUSED_SOURCES = frozenset({"m365"})</code>——历史绑定与状态仅作兼容读取；不抓取目录、不进同步计划、不发能力探测、不写客户端配置，也不自动恢复旧服务。<code>KNOWN_SOURCES</code> 仍含 m365，<code>ACTIVE_SOURCES</code> / <code>ACTIVE_BINDINGS</code> 已把它排除。${pausedNote}</p>` +
        `<p class="wb-muted">本页不臆造故障原因，也没有重新启用该源的入口；重新启用需维护者裁定。</p>` +
        `</div></details></section>`
      );
    }
    const catalogsAll = (catalog.catalogs ?? {}) as Record<
      string,
      CatalogModelRow[]
    >;
    // 单网关模型页只展示本来源的行；目录行本身仍是后端返回（后端不接收来源
    // 参数、固定返回四源），页面只做过滤，不改写、不推算。
    const catalogs: Record<string, CatalogModelRow[]> = onlySource
      ? {
          [onlySource]: Array.isArray(catalogsAll[onlySource])
            ? catalogsAll[onlySource]
            : [],
        }
      : catalogsAll;
    const warnList = Array.isArray(catalog.warnings)
      ? (catalog.warnings as string[])
      : [];
    const checkedAt =
      typeof catalog.checked_epoch === "number"
        ? new Date(catalog.checked_epoch * 1000).toLocaleTimeString("zh-CN", {
            hour12: false,
          })
        : "";
    // P1 (2026-09-24): persisted capability-probe records (state["probes"],
    // exposed read-only by model.sync_pending) — the ONLY probe evidence the
    // catalog can show; a verified record's efforts are the probe-verified
    // tiers, kept apart from the source value.
    const probes = (pending?.probes ?? {}) as Record<
      string,
      Record<string, unknown>
    >;
    const q = this.modelCatalogQ.trim().toLowerCase();
    const rows = Object.entries(catalogs)
      .filter(([src]) => src !== "m365")
      .flatMap(([src, list]) => {
        const arr = Array.isArray(list) ? list : [];
        const warnHit = warnList.some((w) =>
          String(w).includes(this.sourceLabel(src))
        );
        const status = arr.length
          ? `<span class="wb-count-chip">已接入 ${arr.length}</span>`
          : warnHit
            ? `<b class="wb-stale-warn">读取失败</b>`
            : `<span class="wb-muted">空目录</span>`;
        return arr
          .map((m, i) => {
            if (
              q &&
              !(
                m.id.toLowerCase().includes(q) ||
                String(m.display_name ?? "")
                  .toLowerCase()
                  .includes(q)
              )
            )
              return "";
            const checkedKey = `${src}|${m.id}`;
            const checkCell = this.modelScopeCustom
              ? `<td><input type="checkbox" data-model-row-check value="${escapeHtml(checkedKey)}" aria-label="勾选模型 ${escapeHtml(m.id)}"${this.modelChecked.has(checkedKey) ? " checked" : ""}/></td>`
              : "";
            const effortKey = `${src}|${m.id}`;
            const picked = this.modelPick === effortKey;
            return `<tr data-wb-key="model-row:${escapeHtml(effortKey)}"${picked ? ` class="wb-model-row-on"` : ""}><td>${escapeHtml(this.sourceLabel(src))}${i === 0 && warnHit ? " " + status : ""}</td>${this.modelIdCellHtml(src, m.id)}<td>${escapeHtml(m.display_name || "—")}</td><td class="wb-fee-num">${m.context !== null && m.context !== undefined ? m.context.toLocaleString("zh-CN") : "—"}</td>${this.effortCellHtml(src, m, probes[effortKey])}<td>${m.publish ? "发布" : "不发布"}</td>${checkCell}</tr>`;
          })
          .join("");
      })
      .join("");
    const renderWarns = (list: string[]): string =>
      list.length
        ? `<p class="wb-muted"><b class="wb-stale-warn">${escapeHtml(list.join("；"))}</b></p>`
        : "";
    const headCells =
      `<th>来源</th><th>模型 id</th><th>显示名</th><th class="wb-fee-num">声明上下文</th><th>推理档</th><th>发布</th>` +
      (this.modelScopeCustom ? `<th>勾选</th>` : "");
    // F12-5: fixed column widths, shared by header and rows, so a refresh or a
    // long value never re-flows the table; details live in the pane beside it.
    const cols = `<colgroup><col class="wb-col-src"/><col class="wb-col-id"/><col class="wb-col-name"/><col class="wb-col-ctx"/><col class="wb-col-effort"/><col class="wb-col-pub"/>${this.modelScopeCustom ? `<col class="wb-col-check"/>` : ""}</colgroup>`;
    const pickedRowAll = this.modelPickRow(catalogsAll);
    // 单网关模型页里逐模型详情仍在「全部模型」视图：本页选中的行不属于本来源时
    // 不拿它冒充本页详情。
    let pickedRow: { src: string; m: CatalogModelRow } | "none" | "gone";
    if (onlySource) {
      pickedRow =
        pickedRowAll !== "none" &&
        pickedRowAll !== "gone" &&
        pickedRowAll.src === onlySource
          ? pickedRowAll
          : "none";
    } else pickedRow = pickedRowAll;
    // 单网关模型页里逐模型详情（测速 / 设置 / 证据）仍在「全部模型」视图的详情栏；
    // 这里不拿另一个来源的选中行冒充本来源详情。
    const pane = onlySource
      ? pickedRow === "none"
        ? `<aside class="wb-model-detail" data-wb-key="model-detail" data-wb-scroll="model-detail" data-wb-scroll-for="${escapeHtml(this.modelPick ?? "")}" aria-label="模型详情"><p class="wb-muted">逐模型详情（测速、设置、证据）在「全部模型」视图里点选模型后显示；本页只列 ${escapeHtml(sourceName)} 的目录行与同步入口。</p></aside>`
        : this.modelDetailPaneHtml(pickedRow, probes)
      : this.modelDetailPaneHtml(pickedRow, probes);
    // 单来源视图里其他来源的警告不吞掉：如实计数并说明它们属于别处。
    const warnShown = onlySource
      ? warnList.filter((w) => String(w).includes(sourceName))
      : warnList;
    const warnHidden = onlySource ? warnList.length - warnShown.length : 0;
    const readFailed = onlySource ? warnShown.length > 0 : !rows;
    const title = onlySource ? `${escapeHtml(sourceName)} 模型` : "模型目录";
    return (
      `<section class="wb-block wb-char wb-model-catalog"><div class="wb-catalog-head"><h3>${title}${readFailed ? " · 读取失败" : ""}</h3>${checkedAt ? `<span class="wb-muted" title="目录读取时间">${escapeHtml(checkedAt)}</span>` : ""}</div>` +
      `<div class="wb-model-toolbar"><input type="text" class="wb-model-q" data-model-q placeholder="搜索模型名称或 ID" value="${escapeHtml(this.modelCatalogQ)}" aria-label="目录搜索"/></div>` +
      renderWarns(warnShown) +
      (warnHidden
        ? `<p class="wb-muted">另有 ${warnHidden} 条其他来源的读取警告（本页只列 ${escapeHtml(sourceName)}）；完整警告见「全部模型」视图。</p>`
        : "") +
      // 单来源视图里空表必须说清是「读取失败」还是「真的 0 个模型」，不让空表冒充两种含义。
      (onlySource && !rows
        ? warnShown.length
          ? `<p class="wb-muted"><b class="wb-stale-warn">本来源目录读取失败（见上方警告）；空表不代表没有模型。</b></p>`
          : `<p class="wb-muted">本来源目录为空（读取成功、返回 0 个模型）；空目录不代表账号额度为零。</p>`
        : "") +
      `<div class="wb-model-split" data-wb-key="model-split"${this.paneWidthStyle("model-detail")}><div class="wb-table-scroll" data-wb-key="model-table" data-wb-scroll="model-table"><table class="wb-table wb-model-table">${cols}<thead><tr>${headCells}</tr></thead><tbody data-model-tbody>${rows}</tbody></table></div>` +
      this.splitHandleHtml("model-detail", "模型详情栏宽") +
      pane +
      `</div>` +
      this.laneNoteFoldHtml(
        "model-usage-help",
        "使用说明",
        `<ul><li>刷新只读取已确认运行的来源；停用或状态未知的网关不自动查询。</li><li>保存设置后需预览并同步到客户端。</li><li>声明上下文来自目录；实测值仅是已验证下界，人工设置不会提高上游容量。</li><li>测试模型会发起计费请求，与同步分开操作。</li></ul>`
      ) +
      `</section>`
    );
  }
  /** F12-5: the model id cell is the row's selector - the id (clamped to two
   *  lines, full value in the title and in the detail pane) on a button that
   *  opens the row's details beside the table. */
  private modelIdCellHtml(src: string, id: string): string {
    const key = `${src}|${id}`;
    const on = this.modelPick === key;
    return `<td><button type="button" class="wb-model-pick" data-wb-model-pick="${escapeHtml(key)}" aria-pressed="${on}" title="${escapeHtml(id)} · 查看详情"><span class="wb-model-id">${escapeHtml(id)}</span></button></td>`;
  }
  /** The picked row of the current catalog, or why there is none. */
  private modelPickRow(
    catalogs: Record<string, CatalogModelRow[]>
  ): { src: string; m: CatalogModelRow } | "none" | "gone" {
    if (!this.modelPick) return "none";
    const at = this.modelPick.indexOf("|");
    const src = this.modelPick.slice(0, at),
      id = this.modelPick.slice(at + 1);
    const m = (Array.isArray(catalogs[src]) ? catalogs[src] : []).find(
      (row) => row.id === id
    );
    return m ? { src, m } : "gone";
  }
  /** F12-5 independent detail pane: full id, declared context, the read-only
   *  output cap with its source, the effort tiers with provenance and the
   *  advanced comparison, and the hand-started speed test. A refresh keeps the
   *  pick; a model no longer in the catalog says so instead of vanishing. */
  private modelDetailPaneHtml(
    row: { src: string; m: CatalogModelRow } | "none" | "gone",
    probes: Record<string, Record<string, unknown>>
  ): string {
    if (row === "none")
      return `<aside class="wb-model-detail" data-wb-key="model-detail" data-wb-scroll="model-detail" data-wb-scroll-for="${escapeHtml(this.modelPick ?? "")}" aria-label="模型详情"><p class="wb-muted">选择模型，查看设置与证据。</p></aside>`;
    if (row === "gone")
      return `<aside class="wb-model-detail" data-wb-key="model-detail" data-wb-scroll="model-detail" data-wb-scroll-for="${escapeHtml(this.modelPick ?? "")}" aria-label="模型详情"><p class="wb-muted">之前选中的模型已不在本次目录里（可能已退役或该来源读取失败）。请重新选择。</p></aside>`;
    const { src, m } = row;
    const key = `${src}|${m.id}`;
    return (
      `<aside class="wb-model-detail" data-wb-key="model-detail" data-wb-scroll="model-detail" data-wb-scroll-for="${escapeHtml(key)}" aria-label="模型详情：${escapeHtml(m.id)}">` +
      `<div class="wb-model-detail-head"><h4>${escapeHtml(m.id)}</h4>${m.publish ? "" : `<span class="wb-badge wb-badge-idle">不发布</span>`}</div>` +
      `<p class="wb-muted">${escapeHtml(this.sourceLabel(src))} <button type="button" class="wb-btn wb-btn-sm" data-wb-model-id-copy="${escapeHtml(m.id)}">复制 id</button></p>` +
      this.modelSettingsHtml(src, m, probes[key]) +
      this.msTestHtml(src, m) +
      `</aside>`
    );
  }
  /** §11.4（2026-10-10）「测试模型」独立入口：短请求测速 / 上下文验证 /
   *  思考档位验证。本轮只用 fake 上游演示、不发起真实请求；真实计费请求需单独
   *  授权与预算。「校验档位」跳队列不是验证成功——在独立执行入口接线前，这里
   *  只显示只读队列与预算，不报告验证结果。 */
  private msTestHtml(src: string, m: CatalogModelRow): string {
    const speed =
      src === "m365"
        ? `<span class="wb-muted">无密钥供给</span>`
        : this.speedCellHtml(src, m.id);
    const speedKey = `${SPEEDTEST_SOURCE[src] ?? src}|${m.id}`;
    const probeBusy = this.actionInFlight("model.context_probe_preview");
    return (
      `<details class="wb-secondary-details"${this.msTestOpen ? " open" : ""} data-wb-ms-test="1"><summary title="测试模型（独立入口；本轮不发起真实请求）· 点击展开 / 收起">测试模型${wbFoldMark()}</summary>` +
      `<div class="wb-model-speed"><h5>短请求测速</h5><div data-speed-cell="${escapeHtml(speedKey)}">${speed}</div><small class="wb-muted">真实计费的短请求，只测此模型；不证明最大上下文或长任务稳定性。本轮演示只用 fake 上游，不发起真实请求。</small></div>` +
      `<div class="wb-model-ops"><button type="button" class="wb-btn wb-btn-sm" data-wb-model-action="model.context_probe_preview"${probeBusy ? " disabled" : ""}>查看探测队列（只读）</button></div>` +
      `<p class="wb-muted">上下文验证与思考档位验证需要独立的真实计费执行入口（后端 model.context_probe_run 已有请求数与 token 上限、取消与预算）；在独立入口接线前，这里只显示只读队列与预算，不报告验证结果——「校验档位」跳去队列不是验证成功。真实请求需单独授权，不沿用分享测试预算。</p>` +
      `</details>`
    );
  }
  /** §11.1/§11.4（2026-10-10）推理档位证据（只读并列）：目录值（来源）/
   *  能力探测已验证值 / 目录默认档位。已删除「以目录值显示 / 以已验证值显示」
   *  的页面显示切换（改为并列呈现），也删除跳队列的「校验档位」入口——
   *  测试模型是独立入口（见 msTestHtml）。 */
  private effortCellHtml(
    src: string,
    m: CatalogModelRow,
    probe?: Record<string, unknown>
  ): string {
    const verified = this.verifiedEfforts(probe);
    const catalog = (m.efforts ?? []).join("/");
    const label = this.effortSourceLabel(m.efforts_source);
    // 上游显式声明空档位时也不能说「两表都未提供」——那是它自己的声明。
    const emptyNote =
      m.efforts_source === "upstream"
        ? "上游目录明示为空档"
        : "上游目录与本地已知表均未提供";
    return (
      `<td><div class="wb-effort-cell"><span title="${escapeHtml(catalog ? label : emptyNote)}">${escapeHtml(catalog || "未提供")}</span>` +
      (verified?.length
        ? `<span class="wb-effort-verified">已验证 ${escapeHtml(verified.join("/"))}</span>`
        : "") +
      `</div></td>`
    );
  }
  /** §11.1/§11.2/§11.4（2026-10-10）模型设置区：固定区域的自动只读读取
   *  （设置读面 + 探测证据，状态与重试入口都写在这里）+ 三项日常配置表单
   *  + 「能力与证据」折叠（声明上下文 / 已验证 Token 下界 / 字符证据 /
   *  来源·时间·历史 / 只读输出上限，并列呈现；不再有「以目录值显示 / 以已验证
   *  值显示」的显示切换）+ 「更多」（同步历史 / 任务、恢复默认）。
   *  保存只写待同步草稿（保存≠已应用、不发模型请求）；发布在统一同步窗口
   *  （§11.3）一次确认。 */
  private modelSettingsHtml(
    src: string,
    m: CatalogModelRow,
    probe?: Record<string, unknown>
  ): string {
    const key = `${src}|${m.id}`;
    const raw = this.actionResults["model.context_probe_preview"];
    const failed = this.actionResultFailures.get("model.context_probe_preview");
    let ev: ContextEvidence | undefined;
    let evState: "unread" | "bad" | "none" | "ok" = "unread";
    if (raw !== undefined) {
      try {
        ev = normalizeContextEvidence(raw).find(
          (r) => r.source === src && r.modelId === m.id
        );
        evState = ev ? "ok" : "none";
      } catch {
        evState = "bad";
      }
    }
    const n = (v: number) => v.toLocaleString("zh-CN");
    const when = (iso: string | null) => {
      const t = iso ? Date.parse(iso) : NaN;
      return Number.isFinite(t)
        ? new Date(t).toLocaleString("zh-CN", {
            hour12: false,
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
          })
        : "";
    };
    const fact = (dt: string, dd: string, note = "") =>
      `<div><dt>${escapeHtml(dt)}</dt><dd>${dd}${note ? `<small>${escapeHtml(note)}</small>` : ""}</dd></div>`;
    const evNote =
      evState === "unread"
        ? failed
          ? `探测证据读取失败（${failed}）`
          : "探测证据未读取"
        : evState === "bad"
          ? "探测证据形状不可识别（未知，不当作没有证据）"
          : evState === "none"
            ? "该模型不在探测证据记录中"
            : "";
    const floor = ev?.verifiedTokenFloor ?? null;
    // 证据区按后端读数原样显示（0 是合法读数）；表单的「实测下界」只接受
    // 可用上下文（后端 contextTokens 要求 ≥1），0 不是容量，按无值渲染。
    const usableFloor = floor !== null && floor >= 1 ? floor : null;
    const chars =
      ev &&
      (ev.observedCharacterFloor !== null ||
        ev.observedCharacterCeiling !== null)
        ? ev.observedCharacterCeiling !== null &&
          ev.observedCharacterFloor !== null
          ? `${n(ev.observedCharacterFloor)}–${n(ev.observedCharacterCeiling)} 字符`
          : ev.observedCharacterFloor !== null
            ? `≥ ${n(ev.observedCharacterFloor)} 字符`
            : `≤ ${n(ev.observedCharacterCeiling!)} 字符`
        : "";
    const cap = m.output_token_cap;
    const capText =
      cap && typeof cap.value === "number"
        ? `${n(cap.value)}（${cap.source === "compat_default" ? "兼容默认值，不是上游批准容量" : cap.source ? `来源：${cap.source}` : "来源未提供"}）`
        : "上游未提供（未核实）";
    const verified = this.verifiedEfforts(probe) ?? [];
    const efforts = [...new Set([...(m.efforts ?? []), ...verified])];
    const effortSrc = (e: string) =>
      [
        (m.efforts ?? []).includes(e)
          ? `目录 · ${this.effortSourceLabel(m.efforts_source)}`
          : "",
        verified.includes(e) ? "探测已验证" : "",
      ]
        .filter(Boolean)
        .join("，");
    const settings = this.msContextState(key);
    const settingsBusy = this.actionInFlight("model.context_overrides");
    const settingsFailed = this.msSettingsFailure(key);
    const settingsRead = this.msAutoReadState(key, "settings");
    const evidenceRead = this.msAutoReadState(key, "evidence");
    const readBtn = (kind: "settings" | "evidence", label: string) =>
      `<button type="button" class="wb-btn wb-btn-sm" data-wb-ms-autoread="${kind}" data-wb-ms-key="${escapeHtml(key)}" title="只读读取（${kind === "settings" ? "model.context_overrides：只读本地设置与客户端配置" : "model.context_probe_preview：纯读本地持久化记录"}）；不发模型请求、不启动网关">${wbIcon("refresh")}${escapeHtml(label)}</button>`;
    const readWord = (kind: "settings" | "evidence", state: string): string => {
      const label = kind === "settings" ? "设置" : "探测证据";
      if (state === "done")
        return kind === "settings" ? "本地设置已读取" : "本地证据已读取";
      if (state === "loading") return `${label}读取中…`;
      if (state === "failed")
        return `${label}读取失败（${kind === "settings" ? (settingsFailed ?? "原因未知") : (failed ?? (evState === "bad" ? "形状不可识别（未知，不当作没有记录）" : "原因未知"))}）`;
      if (state === "gated")
        return `${label}本次未自动读取（${this.msAutoReadReason(key)}；不联系上游、不启动网关）`;
      return `${label}尚未读取`;
    };
    // 固定区域（§11.1）：单击模型自动读取的两个只读读面状态与重试入口都写
    // 在这里；读取中 / 读取失败 / 门未放行各写各的，未知不当零或默认值。
    const readStatus = (kind: "settings" | "evidence", state: string) =>
      state === "done"
        ? ""
        : `<span class="wb-ms-autoread-item">${escapeHtml(readWord(kind, state))}</span>` +
          (state === "loading"
            ? ""
            : readBtn(kind, state === "failed" ? "重试读取" : "读取"));
    const autoContent =
      readStatus("settings", settingsRead) +
      readStatus("evidence", evidenceRead);
    const autoRegion = autoContent
      ? `<div class="wb-ms-autoread" aria-live="polite">${autoContent}</div>`
      : "";
    const badge =
      settings.state === "ok"
        ? settings.ctx?.draft
          ? `<span class="wb-badge wb-badge-warn">待同步</span>`
          : settings.ctx?.applied
            ? `<span class="wb-badge wb-badge-ok">已应用</span>`
            : `<span class="wb-badge wb-badge-idle">无草稿</span>`
        : settings.state === "none"
          ? `<span class="wb-badge wb-badge-idle">无设置记录</span>`
          : settingsFailed
            ? `<span class="wb-badge wb-badge-warn">设置读取失败</span>`
            : settings.state === "bad"
              ? `<span class="wb-badge wb-badge-warn">设置读面形状不可识别</span>`
              : settingsBusy
                ? `<span class="wb-badge wb-badge-idle">设置读取中…</span>`
                : `<span class="wb-badge wb-badge-idle">设置未读取</span>`;
    // §11.4「能力与证据」折叠：声明上下文 / 已验证 Token 下界 / 字符证据 /
    // 来源·时间·历史 / 只读输出上限，并列呈现。字符不换算成已验证 Token；
    // 兼容输出默认值明确不是上游批准容量；人工设置不是提升上游容量的手段。
    const evidence =
      `<details class="wb-secondary-details"${this.msEvidenceOpen ? " open" : ""} data-wb-ms-evidence="1"><summary title="能力与证据（只读）· 点击展开 / 收起">能力与证据${wbFoldMark()}</summary>` +
      `<dl class="wb-model-facts">` +
      fact(
        "声明上下文",
        m.context !== null && m.context !== undefined
          ? `${n(m.context)} Token`
          : "上游未声明（未知）",
        "目录元数据"
      ) +
      fact(
        "已验证 Token 下界",
        ev ? (floor !== null ? `≥ ${n(floor)} Token` : "尚无") : "—",
        ev
          ? [
              floor !== null ? "实测通过的长度，是下界不是上限" : "",
              ev.lastSuccess ? `最近成功 ${when(ev.lastSuccess)}` : "",
              ev.lastRun ? `最近运行 ${when(ev.lastRun)}` : "",
              ev.staleReason ? `已过期：${ev.staleReason}` : "",
            ]
              .filter(Boolean)
              .join(" · ")
          : evNote
      ) +
      fact(
        "字符证据",
        ev ? chars || "尚无" : "—",
        "字符证据不换算成已验证 Token"
      ) +
      fact(
        "来源 · 时间 · 历史",
        ev
          ? `${escapeHtml(ev.status ?? "状态未知")} · 证据修订 ${escapeHtml(String(ev.evidenceRevision ?? "未知"))} · 历史 ${escapeHtml(String(ev.historyCount ?? "未知"))} 条`
          : escapeHtml(evNote),
        "只读"
      ) +
      fact("单次输出上限", escapeHtml(capText)) +
      `</dl>` +
      this.effortAdvancedHtml(src, m, probe) +
      (usableFloor !== null
        ? `<div class="wb-model-ops"><button type="button" class="wb-btn wb-btn-sm" data-wb-ms-action="use-measured" data-wb-ms-key="${escapeHtml(key)}" title="把实测下界填入待保存设置（保留 measured 来源）；不自动应用、不保存">用作上下文设置（${n(usableFloor)}）</button></div>`
        : "") +
      (evState === "unread" && !failed
        ? `<div class="wb-model-ops">${readBtn("evidence", "读取探测证据")}</div>`
        : "") +
      `</details>`;
    return (
      `<section class="wb-model-settings" aria-label="模型设置"><div class="wb-model-settings-head"><h5>模型设置</h5>${badge}</div>` +
      autoRegion +
      (this.msConflictFor(key)
        ? `<div class="wb-ms-conflict"><strong>客户端上下文有改动</strong><p>客户端 ${escapeHtml(this.msConflictFor(key)!.client)} Token · 控制台 ${escapeHtml(this.msConflictFor(key)!.target)} Token。选择下方配置并保存，再重新预览。</p><button type="button" class="wb-btn wb-btn-sm" data-wb-ms-adopt-client="1" data-wb-ms-key="${escapeHtml(key)}">采用客户端值</button></div>`
        : "") +
      this.modelSettingsFormHtml(
        key,
        m,
        efforts,
        effortSrc,
        usableFloor,
        settings
      ) +
      this.laneNoteFoldHtml(
        "model-settings-state",
        "同步状态",
        this.modelSettingsStateHtml(key)
      ) +
      evidence +
      this.modelMoreHtml(key) +
      `</section>`
    );
  }
  /** §11.4 每个模型的「更多」：恢复默认（明确恢复哪些字段与值，形成可预览
   *  草稿，保留历史证据，不直接发布）。同步历史 / 任务与定时同步在车道级
   *  「更多」里（见 modelLaneMoreHtml）。 */
  private modelMoreHtml(key: string): string {
    const restoreOpen = this.msRestoreKey === key;
    const restoreBusy = this.actionInFlight("model.settings_save");
    const canRestore = this.msContextState(key).state === "ok" && !restoreBusy;
    return (
      `<details class="wb-secondary-details"${this.modelMoreOpen ? " open" : ""} data-wb-ms-more="1"><summary title="更多（低频功能）· 点击展开 / 收起">更多${wbFoldMark()}</summary>` +
      (restoreOpen
        ? `<div class="wb-svc-detail"><h4>恢复默认 · 待确认</h4>` +
          `<p class="wb-muted">将名称、上下文和思考档位恢复为来源默认值。保存为待同步草稿，保留探测历史；同步前仍需预览确认。</p>` +
          `<div class="wb-model-ops"><button type="button" class="wb-btn wb-btn-sm" data-wb-ms-restore="1" data-wb-ms-key="${escapeHtml(key)}"${canRestore ? "" : " disabled"} title="${escapeHtml(!canRestore ? "先读取设置（保存的并发检查需要读面的草稿版本）" : "只写待同步草稿（保存≠已应用）")}">确认恢复默认</button><button type="button" class="wb-btn wb-btn-sm" data-wb-ms-restore-cancel="1">取消</button></div>` +
          `</div>`
        : `<div class="wb-model-ops"><button type="button" class="wb-btn wb-btn-sm" data-wb-ms-restore="1" data-wb-ms-key="${escapeHtml(key)}"${canRestore ? "" : " disabled"} title="${escapeHtml(!canRestore ? "先读取设置（保存的并发检查需要读面的草稿版本）" : "明确将恢复哪些字段与值（显示名称 / 上下文 / 默认档位），形成可预览草稿、不直接发布")}">恢复默认…</button></div>`) +
      `</details>`
    );
  }
  /** 设置读面（model.context_overrides，只读，normalizeContexts）→ 草稿 / 已应用 /
   *  客户端读回三行。无值一律「未知」；读面未读取 / 读取失败 / 形状不可识别 /
   *  该模型不在读面各自如实说明，绝不把「没读到」折成「没有设置」。读面是按来源
   *  的目录读：连续选 A/B 时 A 的迟到载荷属于另一个来源 → 这里仍是「未读取」
   *  （读取中同样如实写），不借用 A 的失败，也不借用 A 的行。 */
  private modelSettingsStateHtml(key: string): string {
    const read = this.msSettingsReadState(key);
    const busy = read.state === "loading";
    const failure =
      read.state === "ok" || read.state === "none"
        ? this.msSettingsFailure(key)
        : undefined;
    const readBtn = (label: string) =>
      `<button type="button" class="wb-btn wb-btn-sm" data-wb-ms-action="read" data-wb-ms-key="${escapeHtml(key)}">${wbIcon("refresh")}${escapeHtml(label)}</button>`;
    if (read.state === "bad") {
      return (
        `<div class="wb-svc-detail"><p class="wb-muted">设置读面形状不可识别（未知，不当作没有设置）。</p>` +
        (busy ? "" : readBtn("重试读取")) +
        `</div>`
      );
    }
    if (read.state !== "ok" && read.state !== "none") {
      const line = busy
        ? "设置读面读取中…（草稿 / 已应用 / 客户端读回在读取返回前一律未知）"
        : failure
          ? `设置读面读取失败（${failure}）；未读取前草稿 / 已应用 / 客户端读回一律未知`
          : "设置读面未读取；未读取前草稿 / 已应用 / 客户端读回一律未知（上面的固定区域给出读取入口）";
      return (
        `<div class="wb-svc-detail"><p class="wb-muted">${escapeHtml(line)}。只读 model.context_overrides，读一条操作记录、不改配置。</p>` +
        `</div>`
      );
    }
    const ctx = read.ctx;
    if (!ctx) {
      return (
        `<div class="wb-svc-detail"><p class="wb-muted">设置读面未返回该模型的记录（未知，不当作没有设置；该模型可能不在当前目录）。</p>` +
        (busy ? "" : readBtn("重新读取")) +
        `</div>`
      );
    }
    const settingsText = (
      s: ModelSettings | null,
      fallback: string
    ): string => {
      if (!s) return fallback;
      const parts: string[] = [];
      if (s.alias) parts.push(`别名 ${s.alias}`);
      if (s.contextSource)
        parts.push(
          `上下文 ${CONTEXT_SOURCE_LABEL[s.contextSource] ?? s.contextSource}${s.contextTokens !== null ? ` ${s.contextTokens.toLocaleString("zh-CN")}` : ""}`
        );
      if (s.defaultEffort) parts.push(`默认档 ${s.defaultEffort}`);
      return parts.length ? parts.join(" · ") : "空设置（全部为默认）";
    };
    const matchesText = (m: {
      context: boolean | null;
      alias: boolean | null;
      defaultEffort: boolean | null;
    }): string => {
      const parts: string[] = [];
      const word = (v: boolean | null, label: string) =>
        parts.push(
          v === null
            ? `${label}无法比较`
            : v
              ? `${label}一致`
              : `${label}不一致`
        );
      word(m.context, "上下文");
      word(m.alias, "别名");
      word(m.defaultEffort, "默认档");
      return parts.join(" · ");
    };
    // 客户端读回按后端返回逐条写出：每个客户端一段「消费者 · 状态 · 上下文…」，
    // 段间用「；」分隔（纯文本，不嵌块级元素）；未返回时写「未知」。
    const readbackText =
      (ctx.clientReadbacks.length
        ? ctx.clientReadbacks
            .map((r) =>
              [
                r.consumer,
                CLIENT_READBACK_LABEL[r.state] ?? r.state,
                r.contextTokens !== null
                  ? `上下文 ${r.contextTokens.toLocaleString("zh-CN")}`
                  : "",
                r.alias ? `别名 ${r.alias}` : "",
                r.defaultEffort ? `默认档 ${r.defaultEffort}` : "",
                r.outputCap.value !== null
                  ? `输出上限 ${r.outputCap.value.toLocaleString("zh-CN")}`
                  : "",
                matchesText(r.matchesApplied),
              ]
                .filter(Boolean)
                .join(" · ")
            )
            .join("；")
        : ctx.clientReadbackState !== "unknown"
          ? [
              `客户端读回 ${CLIENT_READBACK_LABEL[ctx.clientReadbackState] ?? ctx.clientReadbackState}`,
              ctx.clientContextTokens !== null
                ? `上下文 ${ctx.clientContextTokens.toLocaleString("zh-CN")}`
                : "",
              ctx.clientAlias ? `别名 ${ctx.clientAlias}` : "",
              ctx.clientDefaultEffort
                ? `默认档 ${ctx.clientDefaultEffort}`
                : "",
              ctx.clientOutputCap.value !== null
                ? `输出上限 ${ctx.clientOutputCap.value.toLocaleString("zh-CN")}`
                : "",
              ctx.clientMatchesApplied !== null
                ? ctx.clientMatchesApplied
                  ? "与已应用一致"
                  : "与已应用不一致"
                : "",
            ]
              .filter(Boolean)
              .join(" · ")
          : "客户端读回未知（读面未返回该模型的读回字段）") +
      (ctx.clientReadbackConsumer
        ? `（读回客户端 ${ctx.clientReadbackConsumer}）`
        : "");
    // 应用时间的本地格式：解析不出来就留空（不写一个猜的时间）。
    const readbackWhen = (iso: string): string => {
      const t = Date.parse(iso);
      return Number.isFinite(t)
        ? new Date(t).toLocaleString("zh-CN", {
            hour12: false,
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
          })
        : "";
    };
    // §R6/D4（2026-10-10）客户端上下文冲突：显示客户端值与控制台目标值两边及
    // 来源（发布基准 / 无基准记录），未裁定前不写回（resolution=pending）。
    return (
      `<div class="wb-svc-detail"><div class="wb-kv">` +
      `<span>草稿</span><b>${escapeHtml(settingsText(ctx.draft, "无草稿（保存草稿后生成）"))}` +
      `<small>草稿版本 ${ctx.draftVersion !== null ? ctx.draftVersion : "未知"}${ctx.held ? " · 待应用（镜像按 held 抑制该模型的更新）" : ""}</small></b>` +
      `<span>已应用</span><b>${escapeHtml(settingsText(ctx.applied, "无已应用设置"))}` +
      `<small>已应用版本 ${ctx.appliedVersion !== null ? ctx.appliedVersion : "未知"}${ctx.appliedAt ? ` · 应用时间 ${readbackWhen(ctx.appliedAt)}` : ""}</small></b>` +
      `<span>客户端读回</span><b>${escapeHtml(readbackText)}</b>` +
      `<span>有效同步值</span><b>${ctx.effectiveContextTokens !== null ? `${ctx.effectiveContextTokens.toLocaleString("zh-CN")} Token` : "未知"}（来源 ${escapeHtml(CONTEXT_SOURCE_LABEL[ctx.effectiveSource ?? ""] ?? ctx.effectiveSource ?? "未知")}）</b>` +
      `</div>` +
      (busy
        ? `<p class="wb-muted">设置读面读取中…（以下为上一次读数）</p>`
        : "") +
      (failure
        ? `<p class="wb-muted">设置读面读取失败（${escapeHtml(failure)}）${busy ? "" : "；以下为上一次读数。"}</p>`
        : "") +
      (busy ? "" : readBtn("刷新设置读面")) +
      `</div>`
    );
  }
  /** §11.2（2026-10-10）三项日常配置表单（只保留「保存设置」主按钮）：显示名称 /
   *  上下文长度 / 默认思考档位。字段值优先取页面会话内正在编辑的值，其次当前
   *  有效基线（草稿 → 已应用 → 跟随来源），如实回填——applied manual 与已有
   *  草稿都不还原；未知声明显示「未知」。保存只写待同步草稿、不写客户端；只发
   *  实际改动字段并携带读面版本（expected_draft_version）；未改字段不以空值清除。
   *  预览与发布在统一同步窗口（§11.3）一次确认，本表单不再有第二套预览 / 应用
   *  按钮（「恢复默认」在「更多」里）。 */
  private modelSettingsFormHtml(
    key: string,
    m: CatalogModelRow,
    efforts: string[],
    effortSrc: (e: string) => string,
    floor: number | null,
    settings: { state: string; ctx: ContextView | null }
  ): string {
    const value = this.msFormValue(key, settings.ctx);
    const baseline = value.baseline;
    const n = (v: number) => v.toLocaleString("zh-CN");
    const saveBusy = this.actionInFlight("model.settings_save");
    const built = this.msChangedFields(key, settings.ctx);
    // measured 显示与采用：显式点选（msCtx）时用当前证据下界；沿用已保存的
    // measured 时用已保存值（证据刷新不自动应用），证据区有更新的下界时另行
    // 标注「用作上下文设置」后才采用。
    const savedMeasured =
      baseline.mode === "measured" ? Number(baseline.tokens) : NaN;
    const savedMeasuredOk =
      Number.isSafeInteger(savedMeasured) &&
      savedMeasured >= 1 &&
      savedMeasured <= 10_000_000
        ? savedMeasured
        : null;
    const measured =
      value.picked === "measured"
        ? (floor ?? savedMeasuredOk)
        : savedMeasuredOk;
    const measuredNote =
      value.picked === "measured"
        ? "本次选择"
        : floor !== null && floor !== savedMeasuredOk
          ? `证据区有更新的下界 ${n(floor)}；点「用作上下文设置」后采用`
          : "";
    // 已保存的档位不在当前目录档位表内时也如实回填（标注「已保存值」），否则
    // 打开页面会把它显示成「跟随来源默认」，保存其它字段时把已保存档位清掉。
    const effortOptions =
      value.effort && !efforts.includes(value.effort)
        ? [...efforts, value.effort]
        : efforts;
    // 保存的并发检查要读面的草稿版本（expected_draft_version）：读面未读取 /
    // 形状不可识别时按钮按禁用渲染，绝不省略版本检查去覆盖并发修改；读面读到
    //（含无草稿记录的 0）即可保存。没有实际改动 = 按钮禁用（零写入）。
    const readOk = settings.state === "ok";
    const canSave =
      built !== null && built.changed.length > 0 && !saveBusy && readOk;
    const saveTitle =
      settings.state === "unread"
        ? "先读取设置（保存的并发检查需要读面的草稿版本）"
        : settings.state === "bad"
          ? "设置读面形状不可识别：先重试读取（未知，不当作没有设置）"
          : settings.state === "none"
            ? "设置读面未返回该模型的记录（该模型可能不在当前目录）；保存会被后端按目录身份拒绝"
            : built === null
              ? "字段组合不合法（人工上下文需为 1..10000000 的正整数），不提交伪造值"
              : !built.changed.length
                ? "没有实际改动（未改字段不以空值清除）"
                : `只写待同步草稿（保存≠已应用，不发模型请求）；本次改动：${built.changed.join("、")}`;
    return (
      `<fieldset class="wb-model-settings-form"><legend class="wb-sr-only">三项日常配置（保存只写待同步草稿；发布在统一同步窗口预览并确认一次）</legend>` +
      `<label><span>显示名称</span><input type="text" data-wb-ms-field="alias" data-wb-ms-key="${escapeHtml(key)}" placeholder="留空使用原名称" title="仅修改显示名称，真实模型 ID 不变" aria-label="显示名称" value="${escapeHtml(value.alias)}"/></label>` +
      `<div class="wb-model-settings-ctx" role="radiogroup" aria-label="上下文长度"><span>上下文长度</span>` +
      `<label><input type="radio" name="wb-ms-ctx-${escapeHtml(key)}" data-wb-ms-field="ctx" data-wb-ms-key="${escapeHtml(key)}" value="declared"${value.mode === "declared" ? " checked" : ""}/>跟随上游声明${m.context !== null && m.context !== undefined ? ` ${n(m.context)}` : "（未知）"}</label>` +
      `<label><input type="radio" name="wb-ms-ctx-${escapeHtml(key)}" aria-label="自定义上下文" data-wb-ms-field="ctx" data-wb-ms-key="${escapeHtml(key)}" value="manual"${value.mode === "manual" ? " checked" : ""}/>自定义 <input type="number" min="1" step="1" max="10000000" data-wb-ms-field="tokens" data-wb-ms-key="${escapeHtml(key)}" aria-label="自定义上下文 Token" value="${escapeHtml(value.tokens)}"${value.mode === "manual" ? "" : " disabled"}/> Token</label>` +
      (value.mode === "measured"
        ? `<label><input type="radio" name="wb-ms-ctx-${escapeHtml(key)}" data-wb-ms-field="ctx" data-wb-ms-key="${escapeHtml(key)}" value="measured" checked/>已选实测下界 ${measured !== null ? n(measured) : "未知"}${measuredNote ? `（${escapeHtml(measuredNote)}）` : ""}</label>`
        : "") +
      `</div>` +
      `<label><span>默认思考档位</span><select data-wb-ms-field="effort" data-wb-ms-key="${escapeHtml(key)}" aria-label="默认思考档位"><option value="">跟随来源默认</option>${effortOptions.map((e) => `<option value="${escapeHtml(e)}" title="${escapeHtml(efforts.includes(e) ? effortSrc(e) : "已保存值；当前目录未列出")}"${value.effort === e ? " selected" : ""}>${escapeHtml(e)}${efforts.includes(e) ? "" : "（已保存）"}</option>`).join("")}</select></label>` +
      `<div class="wb-model-ops">` +
      `<button type="button" class="wb-action-primary" data-wb-ms-action="save" data-wb-ms-key="${escapeHtml(key)}"${canSave ? "" : " disabled"} title="${escapeHtml(saveTitle)}">保存设置</button>` +
      `<small class="wb-muted" data-wb-ms-save-state="${escapeHtml(key)}" aria-live="polite">${saveBusy ? "正在保存…" : built?.changed.length ? "有未保存修改" : settings.ctx?.draft ? "已保存，待同步" : "尚无修改"}</small>` +
      `</div></fieldset>`
    );
  }
  /** 该模型探测证据里的已验证 Token 下界（model.context_probe_preview 的
   *  token_evidence，下界证据不是容量）；未读取 / 形状不可识别 / 不在记录中
   *  一律 null。`token_evidence=0` 是合法读数（上游报 0 输入 Token），但 0 不是
   *  可用上下文，且后端 contextTokens 要求 1..10000000，所以同样按无值处理——
   *  「采用已验证下界」选项按无值渲染，页面不推算。 */
  private msEvidenceFloor(key: string): number | null {
    const raw = this.actionResults["model.context_probe_preview"];
    if (raw === undefined) return null;
    try {
      const ev = normalizeContextEvidence(raw).find(
        (r) => `${r.source}|${r.modelId}` === key
      );
      const floor = ev?.verifiedTokenFloor ?? null;
      return floor !== null && floor >= 1 ? floor : null;
    } catch {
      return null;
    }
  }
  /** 保存草稿的预期版本：读面读到就用读面的 draftVersion（无草稿记录 = 0），
   *  读面未读取 / 形状不可识别时不带 expected_draft_version（版本检查交给
   *  后端），绝不静默覆盖并发修改。 */
  private msExpectedDraftVersion(key: string): number | null {
    const read = this.msContextState(key);
    if (read.state === "ok") return read.ctx?.draftVersion ?? 0;
    if (read.state === "none") return 0;
    return null;
  }
  /** 后端设置变更行 → 一句人话：before / draft 两侧的别名、上下文、默认档
   *  逐字段写清；读不出来的字段不写。 */
  private msChangeLine(row: Record<string, unknown>): string {
    const side = (value: unknown): string => {
      if (!value || typeof value !== "object" || Array.isArray(value))
        return "无（未设置）";
      const s = value as Record<string, unknown>;
      const parts: string[] = [];
      if (typeof s.alias === "string" && s.alias) parts.push(`别名 ${s.alias}`);
      if (typeof s.contextSource === "string" && s.contextSource) {
        const tokens =
          typeof s.contextTokens === "number"
            ? ` ${s.contextTokens.toLocaleString("zh-CN")}`
            : "";
        parts.push(
          `上下文 ${CONTEXT_SOURCE_LABEL[s.contextSource] ?? s.contextSource}${tokens}`
        );
      }
      if (typeof s.defaultEffort === "string" && s.defaultEffort)
        parts.push(`默认档 ${s.defaultEffort}`);
      return parts.length ? parts.join(" · ") : "空设置（全部为默认）";
    };
    const model = typeof row.model_id === "string" ? row.model_id : "";
    return `${model ? `${model}：` : ""}${side(row.before)} → ${side(row.draft)}`;
  }
  /** 设置读面（model.context_overrides）的读取状态：未读取 / 读取中 / 形状不可识别 /
   *  读到（读面覆盖目录模型与存量设置的并集）/ 读面未返回该模型。另一个来源的迟到
   *  载荷（连续选 A/B 时 A 的迟到结果）一律算「未读取」，不借用它的行。 */
  private msContextState(key: string): {
    state: "unread" | "bad" | "none" | "ok";
    ctx: ContextView | null;
  } {
    const read = this.msSettingsReadState(key);
    if (read.state === "unread" || read.state === "loading")
      return { state: "unread", ctx: null };
    if (read.state === "bad") return { state: "bad", ctx: null };
    return { state: read.ctx ? "ok" : "none", ctx: read.ctx };
  }
  /** §11.2/R5（2026-10-10）当前有效基线：草稿优先，其次已应用；两者都缺席
   *  时是「跟随来源」（显示名空 / 上下文 declared / 档位空）。草稿存在时它的
   *  字段就是待同步值（含显式 null = 恢复来源默认），绝不回退已应用值冒充。 */
  private msBaseline(ctx: ContextView | null): {
    alias: string;
    mode: "declared" | "manual" | "measured";
    tokens: string;
    effort: string;
    hasDraft: boolean;
  } {
    const source = ctx?.draft ?? ctx?.applied ?? null;
    const mode =
      source?.contextSource === "manual" || source?.contextSource === "measured"
        ? source.contextSource
        : "declared";
    return {
      alias: source?.alias ?? "",
      mode,
      tokens:
        source?.contextTokens !== null && source?.contextTokens !== undefined
          ? String(source.contextTokens)
          : "",
      effort: source?.defaultEffort ?? "",
      hasDraft: !!ctx?.draft,
    };
  }
  /** §11.2 表单当前值：页面会话内正在编辑的值优先，其次基线（草稿 → 已应用
   *  → 跟随来源）。 */
  private msFormValue(
    key: string,
    ctx: ContextView | null
  ): {
    alias: string;
    mode: string;
    tokens: string;
    effort: string;
    picked?: string;
    baseline: {
      alias: string;
      mode: "declared" | "manual" | "measured";
      tokens: string;
      effort: string;
      hasDraft: boolean;
    };
  } {
    const baseline = this.msBaseline(ctx);
    return {
      alias: this.msAlias.get(key) ?? baseline.alias,
      mode: this.msCtx.get(key) ?? baseline.mode,
      tokens: this.msCtxTokens.get(key) ?? baseline.tokens,
      effort: this.msEffort.get(key) ?? baseline.effort,
      picked: this.msCtx.get(key),
      baseline,
    };
  }
  /** §11.2/R5 只发送用户实际改动字段（相对当前有效基线）：未改字段不出现在
   *  payload 里，绝不以空值清除；`changed` 为空 = 没有实际改动（保存按钮按
   *  禁用渲染，零写入）。manual 的 Token 必须是 1..10000000 的正整数（与后端
   *  同口径）；measured 采用证据区的已验证下界（≥1），保留 measured 来源，
   *  不伪装 manual/declared。返回 null = 字段读不出来 / 组合不合法（按钮禁用，
   *  绝不提交伪造值）。 */
  private msChangedFields(
    key: string,
    ctx: ContextView | null
  ): { fields: Record<string, unknown>; changed: string[] } | null {
    const value = this.msFormValue(key, ctx);
    const fields: Record<string, unknown> = {};
    const changed: string[] = [];
    const alias = value.alias.trim();
    if (alias !== value.baseline.alias) {
      fields.alias = alias ? alias.slice(0, 128) : null;
      changed.push("显示名称");
    }
    if (value.mode === "declared") {
      if (value.baseline.mode !== "declared" || value.baseline.tokens !== "") {
        fields.contextSource = "declared";
        fields.contextTokens = null;
        changed.push("上下文长度");
      }
    } else if (value.mode === "manual") {
      const tokens = Number(value.tokens.trim());
      if (!Number.isSafeInteger(tokens) || tokens < 1 || tokens > 10_000_000)
        return null;
      if (
        value.baseline.mode !== "manual" ||
        value.baseline.tokens !== String(tokens)
      ) {
        fields.contextSource = "manual";
        fields.contextTokens = tokens;
        changed.push("上下文长度");
      }
    } else if (value.mode === "measured") {
      // 显式点选 measured（msCtx）时采用当前证据下界；沿用已保存的 measured 时用
      // 已保存值（证据刷新不自动应用）。两者都缺席 = null（按钮禁用，不提交）。
      const saved =
        value.baseline.mode === "measured"
          ? Number(value.baseline.tokens)
          : NaN;
      const savedOk =
        Number.isSafeInteger(saved) && saved >= 1 && saved <= 10_000_000
          ? saved
          : null;
      const floor =
        value.picked === "measured" ? this.msEvidenceFloor(key) : null;
      const tokens = floor ?? savedOk;
      if (tokens === null) return null;
      if (
        value.baseline.mode !== "measured" ||
        value.baseline.tokens !== String(tokens)
      ) {
        fields.contextSource = "measured";
        fields.contextTokens = tokens;
        changed.push("上下文长度");
      }
    } else return null;
    const effort = value.effort.trim();
    if (effort !== value.baseline.effort) {
      fields.defaultEffort = effort ? effort.slice(0, 32) : null;
      changed.push("默认思考档位");
    }
    return { fields, changed };
  }
  /** §11.1 自动读取状态（按模型 + 读面）：读面结果已落地 = done；读面失败 =
   *  failed（可手动重试）；自动读取在途 = loading；门未放行 = gated（带原因，
   *  保留手动读取入口）。证据读面（model.context_probe_preview）是纯本地读，覆盖
   *  全部模型，所以 done / failed 对所有模型成立；设置读面（model.context_overrides）
   *  是**按来源**的目录读，所以它只对该来源成立——连续选 A/B 时 A 的迟到载荷
   *  绝不把 B 写成「已读取」。gated 只在被门拒绝的那个模型上成立。 */
  private msAutoReadState(
    key: string,
    kind: "settings" | "evidence"
  ): "idle" | "loading" | "done" | "failed" | "gated" {
    const action =
      kind === "settings"
        ? "model.context_overrides"
        : "model.context_probe_preview";
    const covers = kind !== "settings" || this.msSettingsCovers(key);
    if (this.actionResults[action] !== undefined && covers) {
      // 读到但形状不可识别不是「已读取」（未知，不当作没有记录）。
      if (kind === "settings" && this.msReadState() === "bad") return "failed";
      if (kind === "evidence") {
        try {
          normalizeContextEvidence(this.actionResults[action]);
        } catch {
          return "failed";
        }
      }
      return "done";
    }
    const failure = this.actionResultFailures.get(action);
    if (failure !== undefined && covers) return "failed";
    // 这个模型自己的设置在途 = 读取中（不是「尚未读取」）；另一个来源的迟到
    // 载荷不参与这里的判定。
    if (
      kind === "settings" &&
      this.actionInFlight(action) &&
      this.msSettingsRead.has(key)
    )
      return "loading";
    const record = this.msAutoRead.get(key);
    if (record && record[kind] === "loading") return "loading";
    if (record && record[kind] === "gated") return "gated";
    return "idle";
  }
  /** §11.1 迟到结果不落当前选中：设置读面是**按来源**的目录读，所以一次载荷
   *  只对该来源成立。连续选 A/B 时 A 的迟到载荷属于另一个来源 → 返回 false，
   *  当前模型仍是「未读取」，绝不把 A 的行、A 的失败或 A 的「已读取」当成 B
   *  的设置，也不让 A 的载荷把 B 已保存的基线清成「跟随来源」。 */
  private msSettingsCovers(key: string): boolean {
    const issued = this.msSettingsReadKey;
    if (issued === null || issued === key) return true;
    return (
      issued.slice(0, issued.indexOf("|")) === key.slice(0, key.indexOf("|"))
    );
  }
  /** 设置读面在某个模型上的状态：读到 / 在途 / 另一个来源的迟到载荷，三者分开
   *  （未知 ≠ 没有设置）。 */
  private msSettingsReadState(key: string): {
    state: "unread" | "loading" | "ok" | "none" | "bad";
    ctx: ContextView | null;
  } {
    const inFlight =
      this.msSettingsRead.has(key) &&
      this.actionInFlight("model.context_overrides");
    if (!this.msSettingsCovers(key))
      return { state: inFlight ? "loading" : "unread", ctx: null };
    const raw = this.actionResults["model.context_overrides"];
    if (raw === undefined)
      return { state: inFlight ? "loading" : "unread", ctx: null };
    try {
      const ctx =
        normalizeContexts(raw).find(
          (r) => `${r.source}|${r.modelId}` === key
        ) ?? null;
      return { state: ctx ? "ok" : "none", ctx };
    } catch {
      return { state: "bad", ctx: null };
    }
  }
  /** 设置读面在这个模型上的失败原因：只报**同一来源**的失败（另一个来源的
   *  迟到失败不是这个模型的失败）。 */
  private msSettingsFailure(key: string): string | undefined {
    if (!this.msSettingsCovers(key)) return undefined;
    return this.actionResultFailures.get("model.context_overrides");
  }
  /** 自动读取的门/失败原因（固定区域的状态行用）：门未放行时是门的短句，
   *  读面失败时是实测原因，形状不可识别单独写；都没有时写「运行状态未确认」
   *  （未知 ≠ 已停止）。 */
  private msAutoReadReason(key: string): string {
    const record = this.msAutoRead.get(key);
    if (record?.reason) return record.reason;
    const failure = this.msSettingsFailure(key);
    if (failure !== undefined) return failure;
    if (this.msSettingsCovers(key) && this.msReadState() === "bad")
      return "形状不可识别（未知，不当作没有设置）";
    return "运行状态未确认";
  }
  /** §11.1（2026-10-10）单击模型触发的自动读取：两个只读读面各自动提交
   *  一次（已读到 / 已失败都不自动重发；手动「读取 / 重试」仍可再发）。
   *  · 证据读面（model.context_probe_preview）纯读本地持久化记录，零请求、
   *    零写入——总是可以自动读取；
   *  · 设置读面（model.context_overrides）经来源目录只读采集，所以只在
   *    该模型来源的运行/停用门放行（运行中 / 受管残留）时自动提交；门未
   *    放行时如实写「门未放行 + 原因」并保留手动读取入口——不把未知当
   *    已停止、不启动停用网关、不自动重试。
   *  读面在途时记一次待补读（落定后按**当时选中**补一次；不是定时
   *  重试循环，读取真失败由固定区域的重试入口负责）。
   *  「已提交过」按**模型身份**记账（`msSettingsRead`），不是「全局槽位里
   *  有没有值」：连续选 A/B 时 A 的迟到载荷落进全局槽位后，B 仍会补读自己
   *  的一次（否则 B 永远停在「尚未读取」，且 B 已保存的设置被写成默认）。 */
  /** 一次设置读面的提交（自动读取 / 手动重发 / 发布后回读 / 保存后回读四条路径共用）：
   *  记下「这个模型读过」并更新**最后一次提交的 key**，迟到结果保护按它判断；
   *  目录级回读没有单一选中模型时传 null，此时保留上一次的 key（未知，不猜测）。 */
  private msSettingsReadSubmit(key: string | null): void {
    if (key) this.msSettingsRead.add(key);
    this.msSettingsReadKey = null;
    const models: Record<string, string[]> = {};
    if (key && key.indexOf("|") > 0) {
      const at = key.indexOf("|");
      models[key.slice(0, at)] = [key.slice(at + 1)];
    }
    void this.submitAction("model.context_overrides", "model", {
      local_only: true,
      models,
    });
  }
  private ensureModelAutoRead(key: string) {
    const at = key.indexOf("|");
    if (at <= 0) return;
    const record = this.msAutoRead.get(key) ?? {
      settings: "idle" as const,
      evidence: "idle" as const,
    };
    const next: MsAutoRead = { ...record };
    if (
      this.actionResults["model.context_probe_preview"] === undefined &&
      !this.actionResultFailures.has("model.context_probe_preview") &&
      next.evidence === "idle"
    ) {
      if (this.actionInFlight("model.context_probe_preview"))
        this.msAutoReadDeferred = key;
      else {
        next.evidence = "loading";
        void this.submitAction("model.context_probe_preview", "model", {});
      }
    }
    if (
      !this.msSettingsRead.has(key) &&
      this.msSettingsFailure(key) === undefined &&
      next.settings === "idle"
    ) {
      // Settings/evidence reads are local even when the gateway is stopped.
      if (this.actionInFlight("model.context_overrides")) {
        this.msAutoReadDeferred = key;
      } else {
        next.settings = "loading";
        this.msSettingsReadSubmit(key);
      }
    }
    this.msAutoRead.set(key, next);
  }
  /** 固定区域的手动读取 / 重试：只读重发对应读面一次（不是自动循环）。
   *  手动是明确用户意图（与既有「读取设置」按钮同一语义）；来源未运行时
   *  仍写出门的裁决，由调用方的 title 承载。 */
  private forceModelAutoRead(key: string, kind: "settings" | "evidence") {
    const record = this.msAutoRead.get(key) ?? {
      settings: "idle" as const,
      evidence: "idle" as const,
    };
    const next: MsAutoRead = { ...record };
    if (kind === "settings") {
      if (this.actionInFlight("model.context_overrides")) return;
      next.settings = "loading";
      next.reason = undefined;
      this.msAutoRead.set(key, next);
      // 手动重发也是一次按模型身份的读取：走同一份 key 记账，迟到结果保护同样生效。
      this.msSettingsReadSubmit(key);
      return;
    }
    if (this.actionInFlight("model.context_probe_preview")) return;
    next.evidence = "loading";
    this.msAutoRead.set(key, next);
    void this.submitAction("model.context_probe_preview", "model", {});
  }
  /** 读面落定后的补读：按**当时选中**补一次（不是按被推迟的旧 key，也
   *  不是定时重试循环）。 */
  private settleAutoReadDeferred() {
    const deferred = this.msAutoReadDeferred;
    this.msAutoReadDeferred = null;
    if (!deferred || !this.modelPick) return;
    this.ensureModelAutoRead(this.modelPick);
  }
  /** §11.3 待同步草稿的 selection（{source|model_id: 草稿版本}）：读面读到
   *  的每个模型，凡有草稿且落在本次范围内 → 纳入；读面未读取 / 形状不可
   *  识别 → 返回 null（版本未知，不静默消费草稿，窗口要求先读取读面）；
   *  `unknown` = 有草稿但读面没给出草稿版本（读面读到却缺版本），同样
   *  不能静默消费。范围外草稿原样保留（不进 selection）。 */
  private msPendingSelection(
    scope: Record<string, unknown>
  ): {
    selection: Record<string, number>;
    unknown: string[];
    stale: string[];
  } | null {
    const raw = this.actionResults["model.context_overrides"];
    if (raw === undefined) return null;
    let rows: ContextView[];
    try {
      rows = normalizeContexts(raw);
    } catch {
      return null;
    }
    const lists = this.scopeLists(scope);
    const staleKeys = this.msCatalogAbsentDraftKeys(scope);
    const selection: Record<string, number> = {};
    const unknown: string[] = [];
    const stale: string[] = [];
    for (const row of rows) {
      if (!row.draft) continue;
      const key = `${row.source}|${row.modelId}`;
      if (row.draftVersion === null) {
        unknown.push(key);
        continue;
      }
      if (!this.msKeyInScope(key, lists)) continue;
      // 目录里已不存在该模型：草稿永远发布不出去（发布路径要求模型在目录内），
      // 纳入 selection 会把整轮预览顶失败。这里排除并由窗口如实列出——绝不静默
      // 丢掉用户写的草稿，也绝不让一条草稿把整个同步窗口堵死。
      if (staleKeys.has(key)) {
        stale.push(key);
        continue;
      }
      selection[key] = row.draftVersion;
    }
    return { selection, unknown, stale };
  }

  /** 目录里已不存在该模型的草稿（`source|model_id`）：只有「本轮读到了该来源的
   *  目录行且里面没有这个 id」才算确证；目录未读取 / 该来源本轮没有目录行 → 一律
   *  按「不知道」不排除（绝不用一次目录读取失败去动用户的草稿）。 */
  private msCatalogAbsentDraftKeys(
    scope: Record<string, unknown>
  ): Set<string> {
    const absent = new Set<string>();
    const rawCatalog = this.actionResults["model.catalog"];
    if (!rawCatalog || typeof rawCatalog !== "object") return absent;
    const catalogs = ((rawCatalog as Record<string, unknown>).catalogs ??
      {}) as Record<string, unknown>;
    const rawRows = this.actionResults["model.context_overrides"];
    if (rawRows === undefined) return absent;
    let rows: ContextView[];
    try {
      rows = normalizeContexts(rawRows);
    } catch {
      return absent;
    }
    const lists = this.scopeLists(scope);
    for (const row of rows) {
      if (!row.draft) continue;
      const key = `${row.source}|${row.modelId}`;
      if (!this.msKeyInScope(key, lists)) continue;
      const list = catalogs[row.source];
      if (!Array.isArray(list)) continue;
      const ids = (list as CatalogModelRow[]).map((m) => String(m.id ?? ""));
      if (ids.includes(row.modelId)) continue;
      absent.add(key);
    }
    return absent;
  }
  /** 模型 key 是否落在已提交范围内（与后端 `_combined_selection` 同一规则：
   *  sources 有值时来源必须在内；models 给出该来源时模型 id 必须在表内；
   *  范围外草稿原样保留）。读不出来的维度按「未知」保守拒绝。 */
  private msKeyInScope(
    key: string,
    lists: { sources: ScopeList; consumers: ScopeList; models: ScopeList }
  ): boolean {
    const at = key.indexOf("|");
    if (at <= 0) return false;
    const source = key.slice(0, at),
      modelId = key.slice(at + 1);
    if (lists.sources === "unknown") return false;
    if (lists.sources !== null && !lists.sources.includes(source)) return false;
    if (lists.models === "unknown") return false;
    if (lists.models !== null) {
      const forSource = lists.models.filter((entry) =>
        entry.startsWith(`${source}|`)
      );
      if (forSource.length && !forSource.includes(key)) return false;
    }
    return true;
  }
  /** selection 的规范签名（只用于本地比较：键排序 + 版本；不改提交编码）。 */
  private selectionSignature(selection: Record<string, number>): string {
    return Object.keys(selection)
      .sort()
      .map((key) => `${key}=${selection[key]}`)
      .join(",");
  }
  /** §11.3/R6（2026-10-10）客户端上下文冲突（model.sync_preview /
   *  model.sync_apply 的 contextConflicts 通道，与 warnings 分开）：显示客户端值
   *  与控制台目标值两边及来源（发布基准 / 无基准记录）；未裁定前不写回
   *  （resolution=pending）。读不到字段写「未知」，绝不折成 0。 */
  private msConflictFor(
    key: string
  ): {
    client: string;
    clientTokens: number | null;
    target: string;
    baseline: string;
    baselineRecorded: boolean;
    action: string;
    resolution: string;
    consumer: string;
  } | null {
    const preview = this.actionResults["model.sync_preview"];
    const inner =
      preview && typeof preview === "object" && !Array.isArray(preview)
        ? (preview as Record<string, unknown>).preview
        : undefined;
    const list =
      inner && typeof inner === "object" && !Array.isArray(inner)
        ? (inner as Record<string, unknown>).contextConflicts
        : undefined;
    if (!Array.isArray(list)) return null;
    const num = (value: unknown): string =>
      typeof value === "number" ? value.toLocaleString("zh-CN") : "未知";
    for (const row of list) {
      if (!row || typeof row !== "object" || Array.isArray(row)) continue;
      const r = row as Record<string, unknown>;
      if (`${r.source ?? ""}|${r.model_id ?? ""}` !== key) continue;
      return {
        client: num(r.client),
        clientTokens: typeof r.client === "number" ? r.client : null,
        target: num(r.target),
        baseline: num(r.baseline),
        baselineRecorded: r.baseline_recorded === true,
        action: typeof r.action === "string" ? r.action : "",
        resolution: typeof r.resolution === "string" ? r.resolution : "pending",
        consumer: typeof r.consumer === "string" ? r.consumer : "",
      };
    }
    return null;
  }
  /** §11.3 同步窗口的范围参数：单网关 = 本来源（scope_mode=selected +
   *  sources=[本来源]）；全部模型窗口按窗口内选择（全部 / 只同步选中，
   *  复用既有的 `modelScopeParams()` 构造器与编码）。 */
  private syncWindowScope(source?: string): Record<string, unknown> {
    if (source) return this.gwScopeParams(source);
    return this.modelScopeParams();
  }
  /** 范围是否可提交：空选拒绝在提交前就写清楚（不发注定被拒的请求）。 */
  private syncWindowScopeReady(source?: string): {
    scope: Record<string, unknown>;
    reason: string;
  } {
    const scope = this.syncWindowScope(source);
    if (source || !this.modelScopeCustom) return { scope, reason: "" };
    const lists = this.scopeLists(scope);
    const empty =
      lists.sources === null &&
      lists.consumers === null &&
      lists.models === null;
    return empty
      ? {
          scope,
          reason:
            "只同步选中：空选拒绝（至少勾选一个来源 / 客户端 / 模型，或改选「全部」）",
        }
      : { scope, reason: "" };
  }
  /** 设置读面的整体状态（不绑定某个模型）：未读取 / 形状不可识别时，待同步
   *  草稿版本未知——本次预览不纳入草稿（普通同步语义，绝不静默消费草稿）。 */
  private msReadState(): "unread" | "bad" | "ok" {
    const raw = this.actionResults["model.context_overrides"];
    if (raw === undefined) return "unread";
    try {
      normalizeContexts(raw);
      return "ok";
    } catch {
      return "bad";
    }
  }
  /** §11.3 客户端上下文冲突的单行说明（预览 / 应用共用，与 warnings 分开）：
   *  两边值与来源（发布基准 / 无基准记录）都写清；未裁定前不写回。 */
  private msConflictLine(row: Record<string, unknown>): string {
    const key = `${row.source ?? ""}|${row.model_id ?? ""}`;
    const num = (value: unknown): string =>
      typeof value === "number" ? value.toLocaleString("zh-CN") : "未知";
    const baseline =
      row.baseline_recorded === true
        ? `发布基准 ${num(row.baseline)}`
        : "无发布基准记录";
    const resolution =
      row.resolution === "pending"
        ? "待用户裁定：本次不覆盖"
        : "用户已显式决定控制台值";
    if (row.action === "unreadable") {
      return `${key}：客户端上下文读取不可用（本轮不猜测、不覆盖；${resolution}）`;
    }
    return `${key}：客户端 ${num(row.client)} / 控制台目标 ${num(row.target)} / ${baseline}（${resolution}）`;
  }
  /** §11.3「确认范围并生成预览」：只读提交 model.sync_preview（范围内
   *  草稿 selection 一并纳入，范围确认后自动生成只读预览——不再另找预览
   *  按钮）。空选拒绝在提交前就写清楚；设置读面未读取时本次预览不纳入
   *  草稿（普通同步语义，绝不静默消费草稿）。 */
  private confirmSyncRange(source: string) {
    if (this.actionInFlight("model.sync_preview")) return;
    const ready = this.syncWindowScopeReady(source);
    if (ready.reason) {
      this.setLaneSummary("model", `同步范围未确认：${ready.reason}`, "failed");
      this.renderPanel();
      return;
    }
    const pending = this.msPendingSelection(ready.scope);
    if (pending === null) {
      this.setLaneSummary(
        "model",
        "正在读取本地设置，读取后请再次查看变更",
        "unconfirmed"
      );
      if (!this.actionInFlight("model.context_overrides"))
        this.msSettingsReadSubmit(this.modelPick);
      return;
    }
    if (pending.unknown.length) {
      this.setLaneSummary(
        "model",
        `同步预览未提交：${pending.unknown.length} 个草稿版本读不出来（不静默消费）`,
        "failed"
      );
      this.renderPanel();
      return;
    }
    const params = Object.keys(pending.selection).length
      ? { ...ready.scope, selection: pending.selection }
      : ready.scope;
    void this.submitAction("model.sync_preview", "model", params);
  }
  /** §11.3「确认同步」：一次发布预览所绑定的范围 / 目标 / 草稿版本
   *  （preview_token + selection）。范围、草稿、目录或客户端输入任一变化使
   *  旧预览失效（签名不同即不展示确认，也不复用旧 token）。 */
  private publishSyncWindow(source: string) {
    if (this.actionInFlight("model.sync_apply")) return;
    const bind = this.syncBind;
    const ready = this.syncWindowScopeReady(source);
    if (
      !bind ||
      ready.reason ||
      bind.scopeSig !== this.scopeSignature(ready.scope)
    ) {
      this.setLaneSummary(
        "model",
        "同步预览已失效：范围或草稿已变，请重新确认范围并生成预览",
        "failed"
      );
      this.renderPanel();
      return;
    }
    const pending = this.msPendingSelection(ready.scope);
    if (
      pending === null ||
      pending.unknown.length ||
      this.selectionSignature(pending.selection) !== bind.selectionSig
    ) {
      this.setLaneSummary(
        "model",
        "同步预览已失效：草稿已变，请重新生成预览",
        "failed"
      );
      this.renderPanel();
      return;
    }
    const params: Record<string, unknown> = {
      ...ready.scope,
      preview_token: bind.token,
    };
    if (Object.keys(pending.selection).length)
      params.selection = pending.selection;
    this.applyScopeParams = ready.scope;
    void this.submitAction("model.sync_apply", "model", params);
  }
  /** 某个动作是否有在途提交（准备 / 排队 / 执行中）：重复点击不产生第二次提交，
   *  按钮在途时禁用；与共享车道用同一套反馈条目，不另建在途表。 */
  private actionInFlight(actionName: string): boolean {
    for (const entry of this.feedbackEntries.values()) {
      if (
        entry.action === actionName &&
        (entry.state === "preparing" ||
          entry.state === "queued" ||
          entry.state === "running")
      )
        return true;
    }
    return false;
  }
  /** 设置表单字段的输入镜像（页面会话内）：文本 / 数字 / 单选 / 下拉都按
   *  `source|model_id` 记值；上下文单选改变时重绘（切换人工输入框的可用态），
   *  其余字段不重绘（避免输入中丢焦点）。 */
  private msFieldInput(t: HTMLInputElement | HTMLSelectElement) {
    const key = t.dataset.wbMsKey ?? "";
    if (!key) return;
    this.msEditRevision.set(key, (this.msEditRevision.get(key) ?? 0) + 1);
    const field = t.dataset.wbMsField;
    if (field === "alias") this.msAlias.set(key, t.value);
    else if (field === "tokens") this.msCtxTokens.set(key, t.value);
    else if (field === "effort") this.msEffort.set(key, t.value);
    else if (field === "ctx") {
      this.msCtx.set(key, t.value);
      this.renderPanel();
    }
    this.patchModelSaveControls(key);
  }
  /** Update only the save controls while typing, preserving input focus and caret. */
  private patchModelSaveControls(key: string): void {
    const read = this.msContextState(key);
    const built = this.msChangedFields(
      key,
      read.state === "ok" ? read.ctx : null
    );
    const busy = this.actionInFlight("model.settings_save");
    const button = this.root.querySelector<HTMLButtonElement>(
      `[data-wb-ms-action="save"][data-wb-ms-key="${CSS.escape(key)}"]`
    );
    if (button) {
      button.disabled = busy || read.state !== "ok" || !built?.changed.length;
      button.title =
        built === null
          ? "请输入有效的上下文长度"
          : built.changed.length
            ? "保存为待同步草稿"
            : "尚无修改";
    }
    const status = this.root.querySelector<HTMLElement>(
      `[data-wb-ms-save-state="${CSS.escape(key)}"]`
    );
    if (status)
      status.textContent = busy
        ? "正在保存…"
        : built === null
          ? "请检查上下文长度"
          : built.changed.length
            ? "有未保存修改"
            : read.ctx?.draft
              ? "已保存，待同步"
              : "尚无修改";
  }
  /** §11.1/§11.4（2026-10-10）推理档位证据（只读并列）：目录值（来源）/
   *  能力探测已验证值 / 目录默认档位。已删除「以目录值显示 / 以已验证值显示」
   *  的页面显示切换（改为并列呈现），也删除跳队列的「校验档位」入口——
   *  测试模型是独立入口（见 msTestHtml），跳队列不是验证成功。 */
  private effortAdvancedHtml(
    src: string,
    m: CatalogModelRow,
    probe?: Record<string, unknown>
  ): string {
    const verified = this.verifiedEfforts(probe);
    const status = typeof probe?.status === "string" ? probe.status : "";
    const catalog = (m.efforts ?? []).join("/");
    const sourceLabel = this.effortSourceLabel(m.efforts_source);
    const row = (name: string, value: string, note = "") =>
      `<span>${escapeHtml(name)}</span><b>${escapeHtml(value || "—")}${note ? ` <small class="wb-muted">（${escapeHtml(note)}）</small>` : ""}</b>`;
    return (
      `<div class="wb-svc-detail"><h4>思考档位与来源（只读并列） · ${escapeHtml(m.id)}（${escapeHtml(this.sourceLabel(src))}）</h4>` +
      `<div class="wb-kv">` +
      row("目录值（来源）", catalog, sourceLabel) +
      row(
        "能力探测已验证值",
        verified ? verified.join("/") : "尚无",
        verified ? "计费探测记录" : "尚无已验证记录"
      ) +
      row(
        "目录默认档位",
        String(m.default_effort ?? ""),
        m.default_effort ? "目录元数据" : ""
      ) +
      `</div>` +
      `<p class="wb-muted">两列并列呈现、只读：不改上游目录、不写同步配置、不写探测记录；默认档位只接受上游明示 / 本地已知表 / 能力探测已验证的档位。${status === "pending" ? "当前探测记录：待探测。" : ""}</p>` +
      `</div>`
    );
  }
  /** The probe-verified effort set for one model, or null. Only a
   *  "verified" record carries evidence; any other status is not a tier
   *  verdict and must never be displayed as one. */
  private verifiedEfforts(probe?: Record<string, unknown>): string[] | null {
    if (!probe || probe.status !== "verified") return null;
    const efforts = Array.isArray(probe.efforts)
      ? (probe.efforts as unknown[])
      : [];
    const names = efforts.filter((e) => typeof e === "string" && e) as string[];
    return names.length ? names : null;
  }
  private effortSourceLabel(source?: string | null): string {
    if (source === "upstream") return "上游目录明示";
    if (source === "known") return "本地已知表";
    if (source === "probe") return "能力探测已验证";
    return "来源未标注";
  }
  private speedResultText(speed: SpeedtestResult): string {
    const status = String(speed.status ?? "—");
    const ttfb =
      typeof speed.ttfb_s === "number" ? `${speed.ttfb_s.toFixed(2)}s` : "—";
    const total =
      typeof speed.total_s === "number" ? `${speed.total_s.toFixed(1)}s` : "—";
    return `${status} · 首字 ${ttfb} / 总 ${total}`;
  }
  /** One catalog speed cell (R11-9): the same markup the row template and
   *  the targeted patch both use, so an in-place update can never drift
   *  from a full render. */
  private speedCellHtml(src: string, modelId: string): string {
    const actionSource = SPEEDTEST_SOURCE[src] ?? src;
    const busy = this.speedBusy === `${actionSource}|${modelId}`;
    const result = this.speedResults.get(`${actionSource}|${modelId}`);
    return (
      `<button type="button" class="wb-btn wb-btn-sm" data-wb-model-action="model.speedtest" data-speed-source="${escapeHtml(actionSource)}" data-speed-model="${escapeHtml(modelId)}"${busy ? " disabled" : ""}>测速</button>` +
      (result
        ? `<small class="wb-speed-cell">${escapeHtml(this.speedResultText(result))}</small>`
        : "")
    );
  }
  /** R11-9: patch ONE catalog speed cell in place after its speedtest
   *  reaches a terminal state — no full-lane rebuild, so search filters,
   *  scroll position and the other rows stay untouched. The lane's
   *  op-status chip (if present) is patched too so the terminal summary
   *  does not go stale until the next natural repaint. */
  private patchSpeedCell(source: string, model: string) {
    const actionSource = SPEEDTEST_SOURCE[source] ?? source;
    const cell = this.root.querySelector<HTMLElement>(
      `[data-speed-cell="${CSS.escape(`${actionSource}|${model}`)}"]`
    );
    if (cell) cell.innerHTML = this.speedCellHtml(source, model);
    const chip = this.root.querySelector(".wb-op-status");
    if (chip) chip.outerHTML = this.opStatusHtml();
  }
  /** A12-3 ①: 账号卡折叠里的额度与用量明细（维护者 2026-09-29 起不再常显）。
   *  国内链 = 本机唯一的实时额度快照（绝对数字 + 它自己的检查时点；刷新失败
   *  保留上一次真实值并标过期，未知绝不折成 0）+ 上游四桶 + 本机账本用量。
   *  国际链 = 上游四桶额度 + 账本记录到的调用与输出 Token；输入 Token 缺席
   *  或积分不可归因一律写未知，绝不合成一个合计费用。两链的读取都是有界、
   *  只读的，只落 DOM，不产生操作账本行。 */
  private accountQuotaHtml(serviceId: number): string {
    if (serviceId === 0)
      return `<p class="wb-quota-note">UM 无可信账户余额接口；本机请求与估算费用请看「费用」。</p>`;
    if (serviceId === 3)
      return `<p class="wb-quota-note">Antigravity 可能有 5 小时及更长限额；本地 CLIProxy 当前未提供可核实的账户剩余量和重置时点。此处不按通用 Gemini API 额度猜算。</p>`;
    if (serviceId === 4)
      return this.qoderCnQuotaHtml() + this.qoderCnAccountHtml();
    if (serviceId === 2) return this.qoderGlobalAccountHtml();
    return "";
  }
  /** A12-3 ① 国内：the ONE live quota reading this console can make
   *  (/api/console/quota-snapshot). It keeps its own state word and check
   *  time; a failed re-read keeps the last REAL value marked stale, and an
   *  unread quota says 未知 — never 0. 维护者 2026-09-29 起它随分桶与用量
   *  一起收进该卡自己的折叠：首屏的额度读数只留一行真实数字与到期时点
   *  （`accountCnQuotaReading`），明细、刷新按钮与读取状态在这里展开。 */
  private qoderCnQuotaHtml(): string {
    const snap = this.quotaSnapshot;
    if (snap) {
      return `<div class="wb-quota"><small>实时额度快照（/api/console/quota-snapshot · 状态 ${escapeHtml(snap.state)}）</small><div class="wb-quota-bucket"><div><strong>剩余</strong><span>${this.quotaValueText()} · 已用 ${this.absolute(snap.used)} / 总量 ${this.absolute(snap.total)}</span></div></div>${this.quotaNoteHtml()}<button type="button" class="wb-btn wb-btn-sm" data-wb-quota-refresh="1" title="重新读取国内网关额度（只读，短缓存）" aria-label="刷新额度">${wbIcon("refresh")}${wbIconHint("刷新额度")}</button></div>`;
    }
    // 无快照 = 本会话还没有成功读过，因此没有可保留的上一次真实值；读取状态
    // 由 `quotaNoteHtml()` 统一写出（读取中 / 读取失败带实测 HTTP 状态 /
    // 尚未读取），与总览页 KPI 对同一事实给出同一结论。这里只补数字本身的
    // 诚实结论：未知就是未知，不折成 0。
    return `<div class="wb-quota"><small>实时额度快照</small><p class="wb-quota-note">剩余、已用与总量均未知，不以 0 或空快照冒充。点击刷新读取上游现值（只读）。</p>${this.quotaNoteHtml()}<button type="button" class="wb-btn wb-btn-sm" data-wb-quota-refresh="1" title="重新读取国内网关额度（只读，短缓存）" aria-label="刷新额度">${wbIcon("refresh")}${wbIconHint("刷新额度")}</button></div>`;
  }
  /** 账号车道首屏（维护者 2026-09-29 · Plan-Revision 9 逐页落点「账号」）：四源
   *  服务卡各自的第一阅读层先给三条真实读数——登录状态、额度与失效时间、下一主动
   *  动作；OAuth / 协议与探针的长句、额度与用量明细、只读查询的原始返回收进该卡
   *  自己的折叠（`.wb-secondary-details`：整行可点、右端 16px chevron、行高 ≥44px，
   *  形制由 workbench.css §12 承担）。读数只来自各链自己的只读查询动作真实返回与
   *  它自己的 `checked_epoch`；没有真实值一律写「未知」，绝不折成 0 或空。 */
  // ---------------- Rev17 gateway workspace: one list of gateways, one detail
  /** A gateway's sidebar auth bucket: the account tab's own login reading (the
   *  only-read query's real answer), never inferred from liveness. */
  private gatewayAuthBucket(s: ServiceRow): "ok" | "attention" | "unknown" {
    const cls = this.accountLoginFact(s).badge.cls;
    return cls === "ok"
      ? "ok"
      : cls === "danger" || cls === "warn"
        ? "attention"
        : "unknown";
  }
  /** Chains in the sidebar order; ids no longer served drop out, new ones append. */
  private gatewayOrdered(): ServiceRow[] {
    const byId = new Map(CHAINS.map((s) => [s.id, s] as const));
    const seen = new Set<number>();
    const out: ServiceRow[] = [];
    for (const id of this.gwOrder) {
      const s = byId.get(id);
      if (s && !seen.has(id)) {
        seen.add(id);
        out.push(s);
      }
    }
    for (const s of CHAINS) if (!seen.has(s.id)) out.push(s);
    return out;
  }
  private gatewayVisible(): ServiceRow[] {
    const q = this.gwQuery.trim().toLowerCase();
    return this.gatewayOrdered().filter(
      (s) =>
        (!q || s.name.toLowerCase().includes(q) || s.port.includes(q)) &&
        (this.gwAuth === "all" || this.gatewayAuthBucket(s) === this.gwAuth)
    );
  }
  /** Move `id` to `index` among the VISIBLE rows. Hidden rows keep their slots in
   *  the full order: the visible ids are re-dealt into the positions they already
   *  occupy, so a filtered move never collapses the full order onto the subset. */
  private gatewayMove(id: number, index: number): boolean {
    const full = this.gatewayOrdered().map((s) => s.id);
    const visible = this.gatewayVisible().map((s) => s.id);
    const from = visible.indexOf(id);
    const to = Math.max(0, Math.min(visible.length - 1, index));
    if (from < 0 || from === to) return false;
    const next = visible.slice();
    next.splice(from, 1);
    next.splice(to, 0, id);
    // The delivered merge keeps hidden gateways in their exact slots; ids are the
    // stable service identities, never list positions.
    this.gwOrder = reorderGatewaySubset(full.map(String), next.map(String)).map(
      Number
    );
    const name = serviceById(id)?.name ?? "";
    this.gwAnnounce = `已将 ${name} 移到第 ${to + 1} 位`;
    this.persistGatewayOrder();
    return true;
  }
  /** Startup read of the browser-local order preference. The module re-coordinates
   *  the stored sequence with the available set and reports its own status; the page
   *  maps the effective order back and states the fact instead of guessing. */
  private loadGatewayOrder(): void {
    const ids = CHAINS.map((s) => String(s.id));
    let prefs: GatewayOrderPreferences | null = null;
    try {
      prefs = new GatewayOrderPreferences({
        storage: localStorage,
        namespace: GW_ORDER_NAMESPACE,
      });
    } catch {
      prefs = null;
    }
    this.gwOrderPrefs = prefs;
    if (!prefs) {
      this.gwOrderNote =
        "顺序只在本页有效：本浏览器未记住顺序（无法访问本浏览器存储）。";
      return;
    }
    const read = prefs.read(ids);
    const known = new Set(ids);
    const order: number[] = [];
    for (const id of read.order)
      if (known.has(id)) {
        const n = Number(id);
        if (!order.includes(n)) order.push(n);
      }
    for (const s of CHAINS) if (!order.includes(s.id)) order.push(s.id);
    this.gwOrder = order;
    this.gwOrderNote =
      read.status === "default"
        ? "顺序拖动后会在本浏览器记住（当前为默认顺序）。"
        : read.status === "coordinated"
          ? "顺序已在本浏览器记住。"
          : `浏览器里保存的顺序未采用（${GW_ORDER_RESET_REASONS[read.status] ?? read.status}），当前按默认顺序；再次拖动会覆盖保存。`;
  }
  /** One full-sequence write after a completed move. A refusal keeps the in-page
   *  order authoritative and is stated as NOT remembered, with its reason. */
  private persistGatewayOrder(): void {
    const available = CHAINS.map((s) => String(s.id));
    const order = this.gatewayOrdered().map((s) => String(s.id));
    const prefs = this.gwOrderPrefs;
    if (!prefs) {
      this.gwOrderNote =
        "顺序只在本页有效：本浏览器未记住顺序（无法访问本浏览器存储）。";
      return;
    }
    try {
      const save = prefs.save(order, available);
      this.gwOrderNote = save.persisted
        ? "顺序已在本浏览器记住。"
        : `本浏览器未记住顺序（${save.reason === "sequence_too_long" ? "顺序超过上限" : "写入被拒绝"}）；本次顺序只在本页有效。`;
    } catch {
      this.gwOrderNote =
        "本浏览器未记住顺序（顺序状态异常）；本次顺序只在本页有效。";
    }
  }
  /** One read per selected gateway: the binding clears the previous slot
   *  synchronously (no old plaintext key survives the switch), issues the read and
   *  drops a late response; the page mirrors the last view it installed. The
   *  candidate Demo (#demo-connection) is a synthetic preview: it keeps its own
   *  fixtures and never issues a real read. */
  /** Rev19 A19-2: drop the installed read result (and its plaintext call key)
   *  the moment the gateway page is left. The binding's own clear() cancels the
   *  in-flight lane, so a late response can never refill the dropped value. */
  private clearGatewayConnection(): void {
    if (!this.gwConnService && !this.gwConnReading && !this.gwConnection)
      return;
    this.gwConnBinding?.clear();
    this.gwConnService = "";
    this.gwConnReading = false;
    this.gwConnSeq += 1;
    this.gwConnection = null;
    this.gwCopyFeedback = null;
    if (this.lane === 1) this.renderPanel();
  }
  private syncGatewayConnection(): void {
    const id = this.gwSel;
    const service =
      typeof id === "number" ? (serviceById(id)?.targets?.[0] ?? "") : "";
    if (!service || this.connDemoBuild) {
      this.clearGatewayConnection();
      return;
    }
    if (this.gwConnService === service) return;
    const binding =
      this.gwConnBinding ??
      (this.gwConnBinding = new GatewayConnectionBinding(
        new BackendClient(
          createFetchTransport({ fetch: (input, init) => fetch(input, init) })
        )
      ));
    binding.clear();
    this.gwCopyFeedback = null;
    this.gwConnService = service;
    this.gwConnReading = true;
    this.gwConnection = null;
    const seq = ++this.gwConnSeq;
    void binding
      .select(service)
      .then((view) => {
        if (seq !== this.gwConnSeq || this.gwSel !== id) return;
        this.gwConnReading = false;
        if (view) this.gwConnection = view;
        if (this.lane === 1) this.renderPanel();
      })
      .catch(() => {
        if (seq !== this.gwConnSeq || this.gwSel !== id) return;
        this.gwConnReading = false;
        if (this.lane === 1) this.renderPanel();
      });
  }
  /** Copy one field of the current read result. The module owns the decision:
   *  success only after the clipboard write resolved, every refusal and rejected
   *  write comes back as a failure whose reason never carries the value. */
  private async copySelectedConnectionField(
    field: "url" | "key"
  ): Promise<void> {
    const id = this.gwSel;
    const s = typeof id === "number" ? serviceById(id) : undefined;
    const service = s?.targets?.[0] ?? "";
    if (!service) return;
    const view =
      this.gwConnection && this.gwConnection.service === service
        ? this.gwConnection
        : null;
    const copyField: GatewayConnectionCopyField =
      field === "url" ? "baseUrl" : "keyPlaintext";
    const outcome = await copyGatewayConnectionField(
      view?.result.value ?? null,
      copyField,
      navigator.clipboard
    );
    if (this.gwSel !== id) return;
    this.gwCopyFeedback = {
      service,
      field,
      ok: outcome.ok,
      reason: outcome.reason ?? undefined,
    };
    this.renderPanel();
  }
  private gatewayWorkspaceHtml(): string {
    const detail =
      this.gwSel === "models"
        ? this.gatewayModelsHtml()
        : this.gatewayDetailHtml(this.gwSel);
    return (
      `<div class="wb-gw" data-wb-key="gw-workspace" data-wb-gw-view="${this.gwNarrowDetail ? "detail" : "list"}"${this.paneWidthStyle("gw-side")}>${this.gatewaySideHtml()}` +
      this.splitHandleHtml("gw-side", "网关列表栏宽") +
      `<div class="wb-gw-main" data-wb-key="gw-main">${detail}</div></div>`
    );
  }
  private gatewaySideHtml(): string {
    const ordered = this.gatewayOrdered();
    const visible = this.gatewayVisible();
    const count = (b: "ok" | "attention" | "unknown") =>
      ordered.filter((s) => this.gatewayAuthBucket(s) === b).length;
    const chip = (key: typeof this.gwAuth, label: string, n: number) =>
      `<button type="button" class="wb-gw-chip" data-wb-gw-auth="${key}" aria-pressed="${this.gwAuth === key}">${escapeHtml(label)} <span>${n}</span></button>`;
    const allOn = this.gwSel === "models";
    const sel =
      typeof this.gwSel === "number" ? serviceById(this.gwSel) : undefined;
    const hiddenSel =
      sel && !visible.some((s) => s.id === sel.id)
        ? `<p class="wb-gw-hint">当前详情：${escapeHtml(sel.name)}（已被筛选隐藏，详情不变）</p>`
        : "";
    const rows = visible.length
      ? visible
          .map((s, i) => this.gatewayRowHtml(s, i, visible.length))
          .join("")
      : `<li class="wb-gw-empty">没有符合条件的网关${this.gwQuery ? `（搜索「${escapeHtml(this.gwQuery)}」）` : ""}</li>`;
    return (
      `<aside class="wb-gw-side wb-char" aria-label="网关列表">` +
      `<button type="button" class="wb-md-item wb-gw-all" data-wb-gw-sel="models" aria-pressed="${allOn}">${wbIcon("models")}<span class="wb-md-name">全部模型</span></button>` +
      `<div class="wb-gw-filter"><input type="search" class="wb-gw-q" data-wb-gw-q placeholder="搜索网关名称或端口" aria-label="搜索网关" value="${escapeHtml(this.gwQuery)}"/>` +
      `<div class="wb-gw-chips" role="group" aria-label="按认证状态筛选">${chip("all", "全部", ordered.length)}${chip("ok", "已认证", count("ok"))}${chip("attention", "需处理", count("attention"))}${chip("unknown", "未知", count("unknown"))}</div></div>` +
      hiddenSel +
      `<ol class="wb-gw-list" data-wb-scroll="gw-list" aria-label="网关（拖动右侧把手，或在条目上按 Alt+↑ / Alt+↓ 调整顺序）">${rows}</ol>` +
      `<p class="${/(失败|未能|未记住|不可用|被拒|异常|只在本页)/.test(this.gwOrderNote) ? "wb-gw-hint" : "wb-sr-only"}">${escapeHtml(this.gwOrderNote)}</p>` +
      `<p class="wb-sr-only" aria-live="polite" data-wb-gw-announce>${escapeHtml(this.gwAnnounce)}</p>` +
      `</aside>`
    );
  }
  private gatewayRowHtml(s: ServiceRow, index: number, count: number): string {
    const on = this.gwSel === s.id;
    const login = this.accountLoginFact(s);
    const pending = this.svcPendingText(s);
    const base = `${s.name} · 端口 ${s.port}`;
    return (
      `<li class="wb-gw-row${on ? " wb-gw-row-on" : ""}" data-wb-key="gw-row:${s.id}" data-wb-gw-row="${s.id}">` +
      `<button type="button" class="wb-md-item wb-gw-item" data-wb-gw-sel="${s.id}" data-wb-probe="${s.id}" data-wb-base-title="${escapeHtml(base)}" aria-pressed="${on}" title="${escapeHtml(`${base} · ${this.probeEvidence(s.id)}`)}">` +
      `<i class="wb-dot wb-dot-${this.dotState(s.id)}" aria-hidden="true"></i>` +
      `<span class="wb-md-name">${escapeHtml(s.name)}</span>` +
      `<span class="wb-md-state"><span class="wb-badge wb-badge-${login.badge.cls}">${escapeHtml(login.badge.text)}</span><span class="wb-md-port">:${s.port}</span>${pending ? `<span class="wb-md-pending">${escapeHtml(pending)}</span>` : ""}</span>` +
      `</button>` +
      `<span class="wb-gw-move">` +
      `<button type="button" class="wb-gw-step" data-wb-gw-move="${s.id}" data-wb-gw-dir="-1" aria-label="上移 ${escapeHtml(s.name)}"${index === 0 ? " disabled" : ""}><i aria-hidden="true"></i></button>` +
      `<span class="wb-gw-handle" data-wb-gw-handle="${s.id}" title="拖动调整顺序" aria-hidden="true"><i></i><i></i><i></i></span>` +
      `<button type="button" class="wb-gw-step wb-gw-step-down" data-wb-gw-move="${s.id}" data-wb-gw-dir="1" aria-label="下移 ${escapeHtml(s.name)}"${index === count - 1 ? " disabled" : ""}><i aria-hidden="true"></i></button>` +
      `</span></li>`
    );
  }
  /** One gateway: its head, then 账号 (summary readings, the management detail,
   *  then the same chain's 详情与原始返回) or 模型. */
  private gatewayDetailHtml(id: number): string {
    const s = serviceById(id);
    if (!s || s.id === CONSOLE_HOST_ID) {
      return `<section class="wb-gw-detail"><p class="wb-muted">该网关已不在列表中，请在左侧重新选择。</p></section>`;
    }
    const login = this.accountLoginFact(s);
    const fact = (label: string, f: AccountFact, key: string) =>
      `<div class="wb-account-fact" data-wb-account-fact="${key}"><dt>${escapeHtml(label)}</dt><dd title="${escapeHtml(f.title)}"><b>${escapeHtml(f.text)}</b>${f.sub ? `<small>${escapeHtml(f.sub)}</small>` : ""}</dd></div>`;
    const expiry = this.accountExpiryFact(s);
    const runtime = this.runtimeBadge(s);
    const tab = (key: typeof this.gwTab, label: string) =>
      `<button type="button" data-wb-gw-tab="${key}" aria-pressed="${this.gwTab === key}">${escapeHtml(label)}</button>`;
    // 账号: the four short readings first, then the management detail (runtime,
    // probe, account state and every action, on charcoal like the overview's
    // pane), then the same chain's 详情与原始返回 below it.
    const body =
      this.gwTab === "models"
        ? this.gatewayModelsTabHtml(s)
        : `<div class="wb-block wb-gw-facts"><dl class="wb-account-facts">` +
          fact("登录状态", login, "login") +
          fact("可用额度", this.accountQuotaFact(s), "quota") +
          fact(expiry.label, expiry, "expiry") +
          fact("下一主动作", this.accountNextFact(s), "next") +
          `</dl></div>` +
          this.gatewayConnectionHtml(s) +
          `<div class="wb-char wb-gw-manage">${this.serviceDetailHtml(s.id, "workspace")}</div>` +
          `<div class="wb-block wb-gw-more">${this.accountCardFoldHtml(s)}</div>`;
    return (
      `<section class="wb-gw-detail" data-wb-account="${s.id}" aria-label="${escapeHtml(s.name)}">` +
      `<div class="wb-gw-head"><button type="button" class="wb-btn wb-btn-sm wb-gw-back" data-wb-gw-back>← 网关列表</button>` +
      `<h3 tabindex="-1">${escapeHtml(s.name)}</h3><span class="wb-badge wb-badge-${runtime.cls}">${escapeHtml(runtime.text)}</span>` +
      `<span class="wb-badge wb-badge-${login.badge.cls}" title="${escapeHtml(login.title)}">${escapeHtml(login.badge.text)}</span><span class="wb-port">端口 ${s.port}</span></div>` +
      `<div class="wb-section-tabs" role="group" aria-label="${escapeHtml(s.name)} 的内容">${tab("account", "账号")}${tab("models", "模型")}</div>` +
      body +
      `</section>`
    );
  }
  /** A single gateway's 模型 tab (Rev17/Rev18). 网关 ↔ 模型来源的关联来自后端
   *  自己的服务身份表（GATEWAY_MODEL_SOURCE ↔ console_speedtest.py:113-120
   *  `_SOURCE_SERVICE_KEYS`）；目录行、绑定行与同步计划全部来自后端返回
   *  （model.catalog / model.sync_pending / model.sync_preview），页面只过滤
   *  展示、按显式范围参数提交，不按端口或名称猜来源、不自己算计划。 */
  private gatewayModelsTabHtml(s: ServiceRow): string {
    const service = s.targets?.[0] ?? "";
    const source = GATEWAY_MODEL_SOURCE[service] ?? "";
    if (!source) {
      return (
        `<div class="wb-empty-card wb-gw-pending"><strong>本网关的模型来源关联未接线</strong>` +
        `<small>后端服务身份「${escapeHtml(service || "未提供")}」不在已登记的服务 ↔ 模型来源表内（console_speedtest._SOURCE_SERVICE_KEYS 未收录），页面不按端口或名称猜测，所以这里不列出模型，也不提供只同步本网关的入口。可以在「全部模型」里按来源筛选查看与同步。</small>` +
        `<div class="wb-model-ops"><button type="button" class="wb-btn" data-wb-gw-sel="models">打开全部模型</button></div></div>`
      );
    }
    const pending = this.actionResults["model.sync_pending"];
    const catalog = this.actionResults["model.catalog"];
    const catalogFailure = this.actionResultFailures.get("model.catalog");
    const catalogBusy = this.actionInFlight("model.catalog");
    const sourceName = this.sourceLabel(source);
    // 绑定行：后端 model.sync_pending 的只读 bindings 字段（source/consumer/
    // provider），未读取 / 字段缺失 / 无本来源行各自如实说明，不折成「没有绑定」。
    let bindings: string;
    if (pending === undefined) {
      bindings = `<p class="wb-muted">绑定读面未读取：进入本页只读读取 model.sync_pending 后显示客户端绑定；未读取不等于没有绑定。</p>`;
    } else if (!Array.isArray(pending.bindings)) {
      bindings = `<p class="wb-muted">绑定读面未返回 bindings 字段（未知，不当作没有绑定）。</p>`;
    } else {
      const rows = (pending.bindings as unknown[])
        .filter(
          (r): r is Record<string, unknown> =>
            !!r && typeof r === "object" && !Array.isArray(r)
        )
        .filter((r) => r.source === source);
      bindings = rows.length
        ? `<div class="wb-sync-diff"><strong>客户端绑定（后端只读字段 bindings）</strong><ul>${rows.map((r) => `<li>${escapeHtml(String(r.consumer ?? "未知"))} · 供应商 ${escapeHtml(String(r.provider ?? "未知"))}</li>`).join("")}</ul></div>`
        : `<p class="wb-muted">读面 bindings 里没有本来源的行（${escapeHtml(sourceName)} 可能不在当前启用绑定内）；不当作没有绑定。</p>`;
    }
    // 目录读取是四源只读读取（后端固定返回全部源），本页只过滤展示本来源；
    // 读取失败 / 读取中按实测状态写（未读取由上面的空卡片说明，未读取不等于没有模型）。
    const catalogState =
      catalog === undefined && (catalogBusy || catalogFailure)
        ? catalogBusy
          ? `<p class="wb-muted">本来源目录读取中…（只读 model.catalog）</p>`
          : `<p class="wb-muted">本来源目录读取失败（${escapeHtml(catalogFailure!)}）；${this.actionResults["model.catalog"] === undefined ? "本次没有可用的上一次读数，下方是失败说明与重试入口。" : "下方表格仍为上一次读数。"}</p>`
        : "";
    return (
      `<div class="wb-gw-models-src">` +
      `<div class="wb-svc-detail"><h4>模型来源关联 · ${escapeHtml(sourceName)}</h4>` +
      `<p class="wb-muted">网关服务身份 <code>${escapeHtml(service)}</code> ↔ 模型来源 <code>${escapeHtml(source)}</code>（与测速、连接读面同一服务身份表；目录行与同步计划全部来自后端返回，页面不推算）。</p>` +
      bindings +
      `</div>` +
      `<div class="wb-section-toolbar"><p class="wb-muted">本来源目录只读（刷新只读本来源，不再四源全读）；读取失败只进警告，空目录不代表账号额度为零。</p><div class="wb-model-ops"><button class="wb-btn wb-btn-sm" data-wb-model-action="model.catalog" data-wb-catalog-source="${escapeHtml(source)}" aria-label="刷新本来源目录"${catalogBusy ? " disabled" : ""}>${wbIcon("refresh")}刷新本来源目录</button></div></div>` +
      catalogState +
      this.modelCatalogHtml(pending, source) +
      `<div class="wb-sync-toolbar"><div><h4>本来源同步（${escapeHtml(sourceName)}）</h4><p class="wb-muted">范围参数显式：scope_mode=${escapeHtml(String(this.gwScopeParams(source).scope_mode ?? ""))} · sources=${escapeHtml(String(this.gwScopeParams(source).sources ?? ""))}（与统一同步窗口共用同一构造器与编码）</p></div></div>` +
      this.modelSyncWindowHtml(source) +
      `</div>`
    );
  }
  /** 单网关的显式范围参数（Rev17 契约 3）：scope_mode=selected + sources=[本来源]。
   *  与「全部模型」的 `modelScopeParams()` 是两个入口、同一后端契约；这里不带
   *  consumers/models——只收窄到本来源，绝不默认全量。 */
  private gwScopeParams(source: string): Record<string, unknown> {
    return { scope_mode: "selected", sources: JSON.stringify([source]) };
  }
  /** The pinned 全部模型 view: the cross-source catalog with its sync tool area. */
  private gatewayModelsHtml(): string {
    return (
      `<section class="wb-gw-detail wb-gw-models" aria-label="全部模型">` +
      `<div class="wb-gw-head"><button type="button" class="wb-btn wb-btn-sm wb-gw-back" data-wb-gw-back>← 网关列表</button><h3 tabindex="-1">全部模型</h3></div>` +
      this.modelLaneHtml() +
      `</section>`
    );
  }
  /** Rev18 客户端连接: the API Base URL and the plaintext call key a client uses
   *  against THIS local gateway (not a login page, an upstream site or a share
   *  key). Every key state keeps its own wording - a read that failed or is
   *  unknown is never shown as 没有 - and both values wrap in full (nothing a
   *  user copies is truncated). A copy button is enabled only while the current
   *  read really carries that value; the candidate Demo keeps its synthetic
   *  preview (copy stays the disabled placeholder). */
  private gatewayConnectionHtml(s: ServiceRow): string {
    const service = s.targets?.[0] ?? "";
    const demo = this.connectionDemo(service);
    const view =
      demo?.view ??
      (this.gwConnection && this.gwConnection.service === service
        ? this.gwConnection
        : null);
    const result: ReadResult<GatewayConnection> | null = view?.result ?? null;
    const ready =
      result?.state === "ready" && result.value ? result.value : null;
    const keyPlain =
      ready && ready.keyState === "present" ? ready.keyPlaintext : null;
    /** A read for THIS gateway is in flight (the binding already cleared the
     *  previous slot, so the block never shows the old gateway's values). */
    const reading =
      !demo && this.gwConnReading && this.gwConnService === service;
    const copy = (field: "url" | "key", label: string) => {
      if (demo)
        return `<button type="button" class="wb-btn wb-btn-sm wb-conn-copy" data-wb-conn-copy="${field}" disabled title="演示数据不写剪贴板；复制反馈用上方「复制反馈」预览">${wbIcon("copy")}复制 · 演示停用</button>`;
      const carried =
        field === "url"
          ? Boolean(ready?.baseUrl?.trim())
          : Boolean(keyPlain?.trim());
      return carried
        ? `<button type="button" class="wb-btn wb-btn-sm wb-conn-copy" data-wb-conn-copy="${field}" title="复制${label}">${wbIcon("copy")}复制</button>`
        : `<button type="button" class="wb-btn wb-btn-sm wb-conn-copy" data-wb-conn-copy="${field}" disabled title="当前读数没有可复制的${label}">${wbIcon("copy")}复制</button>`;
    };
    const value = (text: string, field: "url" | "key", label: string) =>
      `<div class="wb-conn-val"><code>${escapeHtml(text)}</code>${copy(field, label)}</div>`;
    const word = (text: string, cls: "idle" | "warn" | "bad" | "plain") =>
      cls === "plain"
        ? `<div class="wb-conn-val"><b>${escapeHtml(text)}</b></div>`
        : `<div class="wb-conn-val"><span class="wb-badge wb-badge-${cls}">${escapeHtml(text)}</span></div>`;
    const row = (label: string, body: string, sub: string) =>
      `<div class="wb-conn-row"><dt>${escapeHtml(label)}</dt><dd>${body}${sub ? `<small>${sub}</small>` : ""}</dd></div>`;
    let rows: string;
    let notes = "";
    if (!result) {
      rows =
        row(
          "API Base URL",
          word(reading ? "读取中…" : "尚未读取", "idle"),
          reading
            ? "正在读取该网关的连接信息（只读）；页面不按端口猜地址。"
            : "尚未读取；页面不按端口猜地址。"
        ) +
        row(
          "API Key",
          word(reading ? "读取中…" : "尚未读取", "idle"),
          reading
            ? "正在读取该网关的连接信息（只读）。"
            : "尚未读取，不代表没有 Key。"
        );
    } else if (result.state !== "ready" || !result.value) {
      const why =
        result.state === "refused"
          ? `读取被拒绝${result.httpStatus ? `（HTTP ${result.httpStatus}）` : ""}`
          : result.state === "unavailable"
            ? `后端未提供连接信息读面${result.httpStatus ? `（HTTP ${result.httpStatus}）` : ""}`
            : "读取失败";
      const reason = `${why}${result.reason ? `：${escapeHtml(result.reason)}` : ""}。`;
      rows =
        row(
          "API Base URL",
          word(result.state === "unavailable" ? "未提供" : "读取失败", "warn"),
          reason
        ) +
        row(
          "API Key",
          word(result.state === "unavailable" ? "未提供" : "读取失败", "warn"),
          `${reason}不能据此判断有没有 Key。`
        );
    } else {
      const c = result.value;
      const faces = c.alternateProtocols
        .map(
          (f) =>
            `另有 ${escapeHtml(f.protocol)} ${escapeHtml(f.path)}${f.note ? `（${escapeHtml(f.note)}）` : ""}`
        )
        .join("；");
      const urlSub = `${escapeHtml(c.protocol)} · 路径 ${escapeHtml(c.protocolPath)}${faces ? `；${faces}` : ""}`;
      const url = c.baseUrl
        ? value(c.baseUrl, "url", "地址")
        : word("未知", "warn");
      const source = c.keySource
        ? `Key 来源 <code>${escapeHtml(c.keySource)}</code>`
        : "";
      const key =
        c.keyState === "present" && c.keyPlaintext
          ? [
              value(c.keyPlaintext, "key", " Key"),
              [source, "明文显示，请勿截图外传"].filter(Boolean).join(" · "),
            ]
          : c.keyState === "absent"
            ? [
                word("没有", "plain"),
                c.auth === "loopback_no_key"
                  ? "本机回环实例未开启入站鉴权，调用时不需要 Key。"
                  : escapeHtml(c.reason),
              ]
            : c.keyState === "needs_key_unconfigured"
              ? [
                  word("未配置（需要 Key）", "warn"),
                  `该网关调用需要 Key，但${c.keySource ? ` <code>${escapeHtml(c.keySource)}</code> ` : "其 Key 来源"}缺席或为空：先补配置，不能当作免鉴权。`,
                ]
              : c.keyState === "unreadable"
                ? [
                    word("读取失败", "warn"),
                    `${escapeHtml(c.reason)}${source ? ` · ${source}` : ""}`,
                  ]
                : [
                    word("未知", "idle"),
                    `${escapeHtml(c.reason)}；不按「没有」处理。`,
                  ];
      rows =
        row(
          "API Base URL",
          url,
          c.baseUrl ? urlSub : `${escapeHtml(c.reason)}；不从显示名或端口猜算。`
        ) + row("API Key", key[0]!, key[1]!);
      const checked =
        c.checkedEpoch > 0
          ? new Date(c.checkedEpoch * 1000).toLocaleString("zh-CN", {
              hour12: false,
            })
          : "未知";
      notes =
        (c.runtimeHealthy === false
          ? `<p class="wb-conn-note"><span class="wb-badge wb-badge-warn">网关未运行</span>当前状态 ${escapeHtml(c.runtimeState ?? "未知")}：地址与 Key 仍可查看，但现在调用会失败。</p>`
          : "") +
        `<p class="wb-muted wb-conn-meta">检查于 ${escapeHtml(checked)}${view?.readAt ? ` · 读取于 ${escapeHtml(new Date(view.readAt).toLocaleTimeString("zh-CN", { hour12: false }))}` : ""}</p>`;
    }
    // Copy outcome: success only after the clipboard write resolved; a failure
    // says why and never reads as success. The live region is always present.
    const fb = demo
      ? demo.feedback
      : this.gwCopyFeedback && this.gwCopyFeedback.service === service
        ? this.gwCopyFeedback
        : null;
    const fbText = fb
      ? `${fb.ok ? `已复制${fb.field === "url" ? " API Base URL" : " API Key"}` : `复制失败：${fb.reason ?? "原因未知"}（未写入剪贴板）`}${demo?.tag ?? ""}`
      : "";
    const feedback = `<p class="wb-conn-feedback${fb ? (fb.ok ? " wb-conn-ok" : " wb-conn-fail") : ""}" role="status" aria-live="polite">${escapeHtml(fbText)}</p>`;
    const badge = demo
      ? demo.badge
      : view
        ? ""
        : `<span class="wb-badge wb-badge-idle">${reading ? "读取中" : "尚未读取"}</span>`;
    return (
      `<section class="wb-block wb-gw-conn" aria-label="客户端连接">` +
      `<div class="wb-conn-head"><h4>客户端连接</h4>${badge}<small>客户端调用本网关用的地址与 Key（不是登录页、控制台或上游网站）</small></div>` +
      (demo?.bar ?? "") +
      `<dl class="wb-conn-list">${rows}</dl>${notes}${feedback}</section>`
    );
  }
  /** The candidate demo's view, labels and state picker for one gateway, or null.
   *  Everything demo-specific sits inside the build-time candidate branch, so a
   *  production build carries none of it. */
  private connectionDemo(
    service: string
  ): {
    view: GatewayConnectionView;
    badge: string;
    bar: string;
    tag: string;
    feedback: { ok: boolean; field: "url" | "key"; reason?: string } | null;
  } | null {
    if (__CONSOLE_CANDIDATE__) {
      if (!this.connDemoBuild || !service) return null;
      const states = [
        ["present", "有 Key"],
        ["absent", "没有"],
        ["needs_key_unconfigured", "需要但未配置"],
        ["unreadable", "Key 读取失败"],
        ["unknown", "未知"],
        ["long", "超长字符串"],
        ["stopped", "网关未运行"],
        ["read_error", "读面读取失败"],
        ["refused", "读取被拒绝"],
        ["unavailable", "未提供读面"],
      ] as const;
      return {
        view: this.connDemoBuild(service, this.connDemoState),
        badge: `<span class="wb-badge wb-badge-warn">演示数据（合成，不是真实连接）</span>`,
        bar:
          `<div class="wb-conn-demo" role="group" aria-label="演示状态（合成数据）"><small>演示状态</small>${states.map(([k, label]) => `<button type="button" class="wb-btn wb-btn-sm" data-wb-conn-demo="${k}" aria-pressed="${this.connDemoState === k}">${label}</button>`).join("")}` +
          `<small>复制反馈</small><button type="button" class="wb-btn wb-btn-sm" data-wb-conn-demo-feedback="ok" aria-pressed="${this.connDemoFeedback === "ok"}">成功</button><button type="button" class="wb-btn wb-btn-sm" data-wb-conn-demo-feedback="fail" aria-pressed="${this.connDemoFeedback === "fail"}">失败</button></div>`,
        tag: "（演示）",
        feedback: this.connDemoFeedback
          ? {
              ok: this.connDemoFeedback === "ok",
              field: "key",
              reason: "合成：浏览器拒绝写入剪贴板",
            }
          : null,
      };
    }
    return null;
  }
  /** Candidate-only (#demo-connection): builds the block's views from the
   *  backend's synthetic connection fixtures. The candidate check is a build-time
   *  constant, so a production build drops this branch and the fixture chunk. */
  private async loadConnectionDemo(): Promise<void> {
    if (__CONSOLE_CANDIDATE__) {
      if (location.hash !== "#demo-connection") return;
      const { gatewayConnectionFixtureBody, FIXTURE_CONNECTION_KEY } =
        await import("./console/backend/fixture.ts");
      const failed = (
        state: "error" | "refused" | "unavailable",
        httpStatus: number | null,
        reason: string
      ): ReadResult<GatewayConnection> => ({
        state,
        value: null,
        httpStatus,
        reason,
      });
      this.connDemoBuild = (service, state) => {
        const ready = (
          body: Record<string, unknown>
        ): ReadResult<GatewayConnection> => ({
          state: "ready",
          value: normalizeGatewayConnection(body, service),
          httpStatus: 200,
          reason: null,
        });
        const result =
          state === "read_error"
            ? failed("error", null, "合成：网络中断")
            : state === "refused"
              ? failed("refused", 403, "合成：当前会话无权读取")
              : state === "unavailable"
                ? failed("unavailable", 404, "合成：该版本后端没有连接读面")
                : state === "long"
                  ? ready(
                      gatewayConnectionFixtureBody("present", {
                        service,
                        base_url: `http://127.0.0.1:8095/v1/${"synthetic-very-long-path-segment/".repeat(4)}end`,
                        key_plaintext: `${FIXTURE_CONNECTION_KEY}-${"synthetic-padding-not-a-real-key-".repeat(5)}end`,
                      })
                    )
                  : state === "stopped"
                    ? ready(
                        gatewayConnectionFixtureBody("present", {
                          service,
                          runtime_state: "Stopped",
                          runtime_healthy: false,
                        })
                      )
                    : ready(
                        gatewayConnectionFixtureBody(state as "present", {
                          service,
                        })
                      );
        return { service, result, readAt: Date.now() };
      };
      // The Demo keeps its synthetic preview: drop any real slot a race may
      // have installed before this build landed.
      this.syncGatewayConnection();
      if (this.lane === 1) this.renderPanel();
    }
  }
  /** Workspace clicks: selection, tabs, filters, keyboard-equivalent moves and the
   *  narrow back button. Returns true when the click was the workspace's. */
  private handleGatewayClick(button: HTMLButtonElement): boolean {
    const d = button.dataset;
    if (d.wbGwSel !== undefined) {
      const next = d.wbGwSel === "models" ? "models" : Number(d.wbGwSel);
      if (next !== "models" && !CHAINS.some((s) => s.id === next)) return true;
      if (next !== this.gwSel) {
        this.gwSel = next;
        this.gwCopyFeedback = null;
        this.connDemoFeedback = "";
        if (next !== "models") this.gwTab = "account";
      }
      this.gwNarrowDetail = true;
      this.syncGatewayConnection();
      if (this.lane !== 1) this.select(1);
      else this.renderPanel();
      // Side by side, focus stays on the list (like the overview's chain list);
      // when the narrow layout swaps the list for the detail, the detail title
      // takes it so keyboard and reader land on what just opened.
      const side = this.root.querySelector<HTMLElement>(".wb-gw-side");
      if (side && getComputedStyle(side).display === "none")
        this.root
          .querySelector<HTMLElement>(".wb-gw-detail h3")
          ?.focus({ preventScroll: true });
      else
        this.root
          .querySelector<HTMLElement>(`.wb-gw-side [data-wb-gw-sel="${next}"]`)
          ?.focus({ preventScroll: true });
      return true;
    }
    if (d.wbGwTab === "account" || d.wbGwTab === "models") {
      this.gwTab = d.wbGwTab;
      // 单网关模型页的绑定读面（model.sync_pending 的只读 bindings 字段）：
      // 本页还没读过就按本页的读口读一次（只读直读、不记操作行，与
      // 「全部模型」共用同一份读面；读过就不再重复拉取）。
      if (
        d.wbGwTab === "models" &&
        this.actionResults["model.sync_pending"] === undefined
      ) {
        void this.fetchModelPending();
      }
      this.renderPanel();
      return true;
    }
    if (d.wbConnDemo !== undefined && this.connDemoBuild) {
      this.connDemoState = d.wbConnDemo;
      this.connDemoFeedback = "";
      this.renderPanel();
      this.root
        .querySelector<HTMLElement>(
          `[data-wb-conn-demo="${CSS.escape(d.wbConnDemo)}"]`
        )
        ?.focus({ preventScroll: true });
      return true;
    }
    if (
      (d.wbConnDemoFeedback === "ok" || d.wbConnDemoFeedback === "fail") &&
      this.connDemoBuild
    ) {
      this.connDemoFeedback = d.wbConnDemoFeedback;
      this.renderPanel();
      this.root
        .querySelector<HTMLElement>(
          `[data-wb-conn-demo-feedback="${d.wbConnDemoFeedback}"]`
        )
        ?.focus({ preventScroll: true });
      return true;
    }
    if (d.wbConnCopy === "url" || d.wbConnCopy === "key") {
      void this.copySelectedConnectionField(d.wbConnCopy);
      return true;
    }
    if (
      d.wbGwAuth === "all" ||
      d.wbGwAuth === "ok" ||
      d.wbGwAuth === "attention" ||
      d.wbGwAuth === "unknown"
    ) {
      this.gwAuth = d.wbGwAuth;
      this.renderPanel();
      return true;
    }
    if (d.wbGwMove !== undefined) {
      const id = Number(d.wbGwMove);
      const at = this.gatewayVisible().findIndex((s) => s.id === id);
      if (at >= 0 && this.gatewayMove(id, at + Number(d.wbGwDir))) {
        this.renderPanel();
        this.root
          .querySelector<HTMLElement>(`[data-wb-gw-sel="${id}"]`)
          ?.focus();
      }
      return true;
    }
    if (d.wbGwBack !== undefined) {
      // Rev19 A19-2: closing the detail also leaves the gateway view — the
      // plaintext call key is dropped with it (same rule as leaving the lane).
      this.clearGatewayConnection();
      this.gwNarrowDetail = false;
      this.renderPanel();
      const sel = this.gwSel;
      this.root
        .querySelector<HTMLElement>(`[data-wb-gw-sel="${sel}"]`)
        ?.focus();
      return true;
    }
    return false;
  }
  /** Workspace input, installed ONCE on the root by delegation: the panel patcher
   *  keeps nodes in place across repaints, so per-render listeners on the list
   *  or the search box would stack. Covers the debounced name search, ↑/↓ and
   *  Alt+↑/↓ in the list, and pointer drag on the grips. */
  private installGatewayEvents() {
    if (this.gwEventsInstalled) return;
    this.gwEventsInstalled = true;
    this.root.addEventListener("input", (event) => {
      const input = (event.target as Element).closest?.<HTMLInputElement>(
        "[data-wb-log-source-q]"
      );
      if (!input) return;
      this.logSourceQuery = input.value.slice(0, 60);
      const caret = input.selectionStart;
      this.renderPanel();
      const fresh = this.root.querySelector<HTMLInputElement>(
        "[data-wb-log-source-q]"
      );
      fresh?.focus();
      if (caret !== null) fresh?.setSelectionRange(caret, caret);
    });
    this.root.addEventListener("pointerdown", (event) => {
      const grip = (event.target as Element).closest?.<HTMLElement>(
        "[data-wb-log-handle]"
      );
      if (grip) this.logDragStart(event, grip.dataset.wbLogHandle!);
    });
    // §10.1 两侧调宽：分隔条与拖动排序是不同的功能，所以分开委托；键盘等价
    // 在同一处接线（一次安装，重绘不叠加监听）。
    this.root.addEventListener("pointerdown", (event) => {
      const handle = (event.target as Element).closest?.<HTMLElement>(
        "[data-wb-split]"
      );
      if (handle) this.splitDragStart(event, handle.dataset.wbSplit!, handle);
    });
    this.root.addEventListener("keydown", (event) => {
      const handle = (event.target as Element).closest?.<HTMLElement>(
        "[data-wb-split]"
      );
      if (handle) this.splitHandleKey(event, handle.dataset.wbSplit!);
    });
    this.root.addEventListener("keydown", (event) => {
      const item = (event.target as Element).closest?.<HTMLElement>(
        ".wb-log-source-list [data-wb-log-source]"
      );
      if (
        !item ||
        !event.altKey ||
        !["ArrowUp", "ArrowDown"].includes(event.key)
      )
        return;
      event.preventDefault();
      const visible = [
        ...this.root.querySelectorAll<HTMLElement>("[data-wb-log-source-row]"),
      ].map((row) => row.dataset.wbLogSourceRow!);
      const at = visible.indexOf(item.dataset.wbLogSource!);
      const next = at + (event.key === "ArrowUp" ? -1 : 1);
      if (at < 0 || next < 0 || next >= visible.length) return;
      [visible[at], visible[next]] = [visible[next]!, visible[at]!];
      this.saveLogOrder(visible);
      this.renderPanel();
      this.root
        .querySelector<HTMLElement>(
          `[data-wb-log-source="${CSS.escape(item.dataset.wbLogSource!)}"]`
        )
        ?.focus();
    });
    this.root.addEventListener("input", (event) => {
      const q = (event.target as Element).closest?.<HTMLInputElement>(
        "[data-wb-gw-q]"
      );
      if (!q) return;
      window.clearTimeout(this.gwQTimer);
      this.gwQTimer = window.setTimeout(() => {
        const value = q.value.slice(0, 60);
        if (value === this.gwQuery) return;
        this.gwQuery = value;
        const caret = q.selectionStart;
        this.renderPanel();
        const fresh =
          this.root.querySelector<HTMLInputElement>("[data-wb-gw-q]");
        if (fresh) {
          fresh.focus();
          if (caret !== null) fresh.setSelectionRange(caret, caret);
        }
      }, 200);
    });
    this.root.addEventListener("pointerdown", (event) => {
      const handle = (event.target as Element).closest?.<HTMLElement>(
        "[data-wb-gw-handle]"
      );
      if (handle)
        this.gatewayDragStart(event, Number(handle.dataset.wbGwHandle));
    });
    this.root.addEventListener("keydown", (event) => {
      const item = (event.target as Element).closest?.<HTMLElement>(
        ".wb-gw-list [data-wb-gw-sel]"
      );
      if (!item) return;
      const id = Number(item.dataset.wbGwSel);
      const visible = this.gatewayVisible();
      const at = visible.findIndex((s) => s.id === id);
      if (at < 0) return;
      if (
        event.altKey &&
        (event.key === "ArrowUp" || event.key === "ArrowDown")
      ) {
        event.preventDefault();
        if (this.gatewayMove(id, at + (event.key === "ArrowUp" ? -1 : 1))) {
          this.renderPanel();
          this.root
            .querySelector<HTMLElement>(`[data-wb-gw-sel="${id}"]`)
            ?.focus();
        }
        return;
      }
      if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        event.preventDefault();
        const next = visible[at + (event.key === "ArrowUp" ? -1 : 1)];
        if (next)
          this.root
            .querySelector<HTMLElement>(`[data-wb-gw-sel="${next.id}"]`)
            ?.focus();
      }
    });
  }
  /** Pointer reorder: the row follows the pointer by swapping places with the row
   *  it crosses (DOM order only, nothing saved yet); Escape restores the order
   *  from before the drag; the order is committed on release. The list scrolls
   *  when the pointer nears its edges. Move / release are heard on the window:
   *  moving the row in the DOM drops the grip's pointer capture. */
  private logDragStart(event: PointerEvent, id: string) {
    if (event.button !== 0 || this.logDrag || this.gwDrag) return;
    const list = this.root.querySelector<HTMLElement>(".wb-log-source-list");
    const row = list?.querySelector<HTMLElement>(
      `[data-wb-log-source-row="${CSS.escape(id)}"]`
    );
    if (!list || !row) return;
    event.preventDefault();
    this.logDrag = {
      pointerId: event.pointerId,
      startOrder: this.logSourceOrder.slice(),
      moved: false,
    };
    row.classList.add("wb-gw-dragging");
    const rowsNow = () => [
      ...list.querySelectorAll<HTMLElement>("[data-wb-log-source-row]"),
    ];
    const move = (e: PointerEvent) => {
      if (e.pointerId !== this.logDrag?.pointerId) return;
      const before = rowsNow()
        .filter((r) => r !== row)
        .find((r) => {
          const b = r.getBoundingClientRect();
          return e.clientY < b.top + b.height / 2;
        });
      if (before && before !== row.nextElementSibling) {
        list.insertBefore(row, before);
        this.logDrag.moved = true;
      } else if (!before && row !== list.lastElementChild) {
        list.appendChild(row);
        this.logDrag.moved = true;
      }
      const box = list.getBoundingClientRect();
      if (e.clientY < box.top + 24) list.scrollTop -= 8;
      else if (e.clientY > box.bottom - 24) list.scrollTop += 8;
    };
    const finish = (commit: boolean) => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", cancel);
      window.removeEventListener("keydown", key, true);
      const drag = this.logDrag;
      this.logDrag = undefined;
      row.classList.remove("wb-gw-dragging");
      if (drag && commit && drag.moved)
        this.saveLogOrder(rowsNow().map((r) => r.dataset.wbLogSourceRow!));
      else if (drag) {
        this.logSourceOrder = drag.startOrder;
        if (!commit) this.logOrderNote = "已取消此次排序，未写入浏览器存储。";
      }
      this.gwRepaintDeferred = false;
      this.renderPanel();
    };
    const up = (e: PointerEvent) => {
      if (e.pointerId === this.logDrag?.pointerId) finish(true);
    };
    const cancel = (e: PointerEvent) => {
      if (e.pointerId === this.logDrag?.pointerId) finish(false);
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        finish(false);
      }
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", cancel);
    window.addEventListener("keydown", key, true);
  }
  private gatewayDragStart(event: PointerEvent, id: number) {
    if (event.button !== 0 || this.gwDrag) return;
    const list = this.root.querySelector<HTMLElement>(".wb-gw-list");
    const row = list?.querySelector<HTMLElement>(`[data-wb-gw-row="${id}"]`);
    if (!list || !row) return;
    event.preventDefault();
    const startOrder = this.gwOrder.slice();
    this.gwDrag = { id, pointerId: event.pointerId, startOrder, moved: false };
    row.classList.add("wb-gw-dragging");
    list.classList.add("wb-gw-sorting");
    const rowsNow = () => [
      ...list.querySelectorAll<HTMLElement>("[data-wb-gw-row]"),
    ];
    const onMove = (e: PointerEvent) => {
      if (!this.gwDrag || e.pointerId !== this.gwDrag.pointerId) return;
      const others = rowsNow().filter((r) => r !== row);
      const before = others.find((r) => {
        const b = r.getBoundingClientRect();
        return e.clientY < b.top + b.height / 2;
      });
      if (before && before !== row.nextElementSibling) {
        list.insertBefore(row, before);
        this.gwDrag.moved = true;
      } else if (!before && row !== list.lastElementChild) {
        list.appendChild(row);
        this.gwDrag.moved = true;
      }
      const box = list.getBoundingClientRect();
      if (e.clientY < box.top + 24) list.scrollTop -= 8;
      else if (e.clientY > box.bottom - 24) list.scrollTop += 8;
    };
    const finish = (commit: boolean) => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onCancel);
      window.removeEventListener("keydown", onKey, true);
      row.classList.remove("wb-gw-dragging");
      list.classList.remove("wb-gw-sorting");
      const drag = this.gwDrag;
      this.gwDrag = undefined;
      if (!drag) return;
      if (commit && drag.moved) {
        const domOrder = rowsNow().map((r) => Number(r.dataset.wbGwRow));
        const target = domOrder.indexOf(drag.id);
        this.gwOrder = drag.startOrder;
        this.gatewayMove(drag.id, target);
      } else {
        this.gwOrder = drag.startOrder;
        if (!commit) this.gwAnnounce = "已取消调整顺序";
      }
      this.gwRepaintDeferred = false;
      this.renderPanel();
    };
    const onUp = (e: PointerEvent) => {
      if (this.gwDrag && e.pointerId === this.gwDrag.pointerId) finish(true);
    };
    const onCancel = (e: PointerEvent) => {
      if (this.gwDrag && e.pointerId === this.gwDrag.pointerId) finish(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        finish(false);
      }
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onCancel);
    window.addEventListener("keydown", onKey, true);
  }
  /** 登录状态短读数：与 `accountAuthValueHtml` 读同一份真实返回与失败记忆，
   *  只是分成「短值 + 一行时点/原因」；完整句子进 `title`。未读取 / 读取失败 /
   *  未认证 / 已认证 / 不支持查询各自一个词，互不折叠。 */
  private accountLoginFact(
    s: ServiceRow
  ): AccountFact & { badge: { text: string; cls: string } } {
    const title = `${this.accountAuthPlain(s)}；登录凭据失效时间：上游未提供（额度到期不是登录失效）。`;
    const statusAction = this.statusActionOf(s);
    const action = statusAction?.action;
    if (!action) {
      const gap = this.statusGap(s);
      return {
        text: gap.word,
        sub: gap.why,
        title,
        badge: {
          text: gap.word === "不支持查询" ? "登录状态未知" : gap.word,
          cls: "gated",
        },
      };
    }
    const result = this.actionResults[action];
    const failure = this.actionResultFailures.get(action);
    if (!result) {
      return failure
        ? {
            text: "读取失败",
            sub: failure,
            title,
            badge: { text: "登录读取失败", cls: "warn" },
          }
        : {
            text: "未读取",
            sub: `点「${statusAction!.label}」只读读取`,
            title,
            badge: { text: "登录未读取", cls: "idle" },
          };
    }
    const at = this.actionCheckedAt(result);
    const stale = failure ? " · 本次刷新失败，为上次值" : "";
    if (action === "cliproxy.status") {
      const r = this.cliproxyLoginReading(result);
      return {
        text: r.text,
        sub: `${r.sub} · 读于 ${at}${stale}`,
        title,
        badge: {
          text: r.text,
          cls: failure && r.cls === "ok" ? "warn" : r.cls,
        },
      };
    }
    const authenticated = result.authenticated;
    if (typeof authenticated === "boolean") {
      return authenticated
        ? {
            text: "已认证",
            sub: `读于 ${at}${stale}`,
            title,
            badge: { text: "已认证", cls: failure ? "warn" : "ok" },
          }
        : {
            text: "未认证",
            sub: `该链自报 · 读于 ${at}${stale}`,
            title,
            badge: { text: "未认证", cls: "danger" },
          };
    }
    const word =
      typeof result.state === "string" && result.state
        ? `状态 ${result.state}`
        : "已读取（无状态词）";
    return {
      text: word,
      sub: `读于 ${at}${stale}`,
      title,
      badge: { text: word, cls: "idle" },
    };
  }
  /** 可用额度短读数：国内链读本机实时快照、国际链读上游四桶合计（只把真实数字
   *  相加），UM 与 CLIProxy 没有可信余额接口写「未知」。完整句子进 `title`。 */
  private accountQuotaFact(s: ServiceRow): AccountFact {
    const title = this.accountQuotaPlain(s);
    if (s.id === 4) {
      const snap = this.quotaSnapshot;
      if (!snap) return { text: "未知", sub: this.quotaStatePlain(), title };
      const value = this.quotaValueText();
      return {
        text: value === "—" ? "未知" : `${value} 积分`,
        sub: this.quotaStatePlain(),
        title,
      };
    }
    if (s.id === 2) {
      const read = this.qoderGlobalQuota;
      const quota = read?.quota;
      const hasQuota =
        Boolean(quota) && typeof quota === "object" && !Array.isArray(quota);
      const checked = read?.checkedAt
        ? new Date(read.checkedAt * 1000).toLocaleTimeString("zh-CN", {
            hour12: false,
          })
        : "时点未知";
      const state = !read
        ? "尚未读取"
        : read.state === "reading"
          ? "读取中…"
          : read.state === "gated"
            ? `本次未读取（${read.error ?? "运行/停用门未放行"}）`
            : read.state === "stale"
              ? "本次读取失败"
              : `读于 ${checked}`;
      if (!hasQuota) return { text: "未知", sub: state, title };
      const sum = this.quotaSum(quota as Record<string, unknown>);
      return {
        text:
          sum.value === null
            ? "未知"
            : `${sum.value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ${sum.unit ?? "积分"}`,
        sub:
          sum.value === null && sum.mixed ? `分桶单位不一致 · ${state}` : state,
        title,
      };
    }
    return {
      text: "未知",
      sub: s.id === 0 ? "UM 无可信余额接口" : "未提供可核实剩余量",
      title,
    };
  }
  /** 到期短读数：只写**额度**到期（上游载荷自己的 `expires_at`），绝不把它当成
   *  登录凭据失效时间；没有额度接口的链写凭据 / 重置时点「未提供」。 */
  private accountExpiryFact(s: ServiceRow): AccountFact & { label: string } {
    const cut = (v: unknown) =>
      typeof v === "string" && v.trim() ? v.trim().slice(0, 50) : "";
    if (s.id === 4) {
      const expires = cut(this.quotaSnapshot?.expires_at);
      return {
        label: "额度到期",
        text: expires || (this.quotaSnapshot ? "上游未提供" : "未知"),
        sub: "",
        title:
          "国内链额度快照自己的到期时点（后端按上游载荷原样透出）；这是额度到期，不是登录失效。",
      };
    }
    if (s.id === 2) {
      const quota = this.qoderGlobalQuota?.quota;
      const has =
        Boolean(quota) && typeof quota === "object" && !Array.isArray(quota);
      const expires = has
        ? cut((quota as Record<string, unknown>).expires_at)
        : "";
      return {
        label: "额度到期",
        text: expires || (has ? "上游未提供" : "未知"),
        sub: "5 小时窗口重置：上游未提供",
        title: "上游四桶额度载荷自己的到期时点；这是额度到期，不是登录失效。",
      };
    }
    if (s.id === 0)
      return {
        label: "凭据到期",
        text: "上游未提供",
        sub: "",
        title: "该链无凭据到期查询接口，不猜算。",
      };
    return {
      label: "额度重置",
      text: "未提供",
      sub: "",
      title:
        "本地 CLIProxy 未提供重置 / 到期时点，不按通用 Gemini API 额度猜算。",
    };
  }
  /** 下一主动作短读数：与动作行里的主按钮同一个判断（`accountNextLogin`）。 */
  private accountNextFact(s: ServiceRow): AccountFact {
    const status = this.statusActionOf(s);
    if (!status) {
      if (s.statusAction) {
        const gap = this.statusGap(s);
        return {
          text: gap.word,
          sub: gap.why,
          title: `${gap.word}：${gap.why}；接入前不提供查询按钮（不画死按钮）。`,
        };
      }
      return {
        text: "无账号接口",
        sub: "可用：只读刷新与「详情」",
        title:
          "该链未提供账号管理接口；可用的只有只读刷新与「详情」（不画死按钮）。",
      };
    }
    const login = this.accountNextLogin(s);
    if (login)
      return {
        text: login.label,
        sub: "本人完成登录 · 先二次确认",
        title: `本次真实返回为未认证：改用「${login.label}」，由维护者本人完成登录（先二次确认，绝不自动打开窗口）。`,
      };
    return {
      text: status.label,
      sub: "",
      title: `「${status.label}」：经 8001 只读提交，一次点击直达、无副作用。`,
    };
  }
  /** 该链的真实返回说未认证、且该链有登录动作时，下一主动作就是那个登录动作。 */
  private accountNextLogin(
    s: ServiceRow
  ): { action: string; label: string } | undefined {
    const status = this.statusActionOf(s);
    if (!status || this.actionResults[status.action]?.authenticated !== false)
      return undefined;
    return (s.manageActions ?? []).find((a) => isLoginAction(a.action));
  }
  /** 第一阅读层 · 登录状态：值只来自该链自己的只读查询动作真实返回（`authenticated`
   *  / `state`，绝不由前端推断），检查时点用返回自己的 `checked_epoch`；没有读取过
   *  写「未读取」，读取失败写失败原因并保留上一次真实值，绝不折成「未认证」。 */
  private accountLoginReadingHtml(s: ServiceRow): string {
    return `<div class="wb-kv"><span>登录状态（完整）</span><b>${this.accountAuthValueHtml(s)}</b></div>`;
  }
  /** 第一阅读层 · 额度与失效时间：只给真实读数与它自己的检查时点，没有真实值一律
   *  写「未知」，绝不折成 0；分桶明细与长口径进该卡自己的折叠。国内链读本机实时
   *  快照（/api/console/quota-snapshot），国际链读上游四桶（只读适配动作
   *  qoderglobal.quota），两者都不写上游状态。 */
  private accountQuotaReadingHtml(s: ServiceRow): string {
    return `<div class="wb-kv"><span>额度与失效时间（完整）</span><b>${escapeHtml(this.accountQuotaPlain(s))}</b></div>`;
  }
  /** The full 额度与失效时间 sentence as plain text (escaped where it is placed:
   *  the fold row above, the short quota cell's `title`). */
  private accountQuotaPlain(s: ServiceRow): string {
    return s.id === 4
      ? this.accountCnQuotaReading()
      : s.id === 2
        ? this.accountGlobalQuotaReading()
        : s.id === 0
          ? "未知（UM 无可信账户余额接口，不按 0 显示）· 失效时间未知（该链无凭据到期查询接口）"
          : "未知（本地 CLIProxy 未提供可核实的剩余量）· 失效时间未知（未提供重置 / 到期时点）";
  }
  /** 国内链额度读数：本机唯一的实时额度读口（/api/console/quota-snapshot，只读）。
   *  快照缺席 = 未知，读取状态由 `quotaStateText()` 统一写出（读取中 / 读取失败带
   *  实测 HTTP 状态 / 尚未读取），与总览页 KPI 对同一事实给出同一结论；到期时点取
   *  快照自己的 `expires_at`（后端按上游载荷原样透出），缺席写未知，绝不猜算。 */
  private accountCnQuotaReading(): string {
    const snap = this.quotaSnapshot;
    // 快照缺席 = 未知：读取状态句仍取 `quotaStateText()`（读取中 / 读取失败带实测
    // HTTP 状态 / 尚未读取），与总览页 KPI 对同一事实给出同一结论；这里不再把它
    // 套进一层括号，读作「未知 · 失效时间未知 · 读取中」。
    if (!snap)
      return `未知 · 失效时间未知（快照未提供到期时点） · ${this.quotaStatePlain()}`;
    const stamp = new Date(snap.checked_at * 1000).toLocaleTimeString("zh-CN", {
      hour12: false,
    });
    const expires =
      typeof snap.expires_at === "string" && snap.expires_at.trim()
        ? snap.expires_at.slice(0, 50)
        : "未知（上游未提供）";
    const state =
      snap.state === "live"
        ? `读于 ${stamp}`
        : snap.state === "cached"
          ? `读于 ${stamp}（服务端缓存）`
          : snap.state === "stale"
            ? `读于 ${stamp}（本次刷新失败，保留上次真实值）`
            : `读于 ${stamp}（状态 ${snap.state}）`;
    return `剩余 ${this.quotaValueText()} · 已用 ${this.absolute(snap.used)} / 总量 ${this.absolute(snap.total)} · 额度到期 ${expires} · ${state}`;
  }
  /** 国际链额度读数：上游四桶额度的合计剩余（只把真实数字相加；单位不一致或没有
   *  可读桶时写未知，绝不跨单位合并、也不折成 0），到期时点取上游载荷自己的
   *  `expires_at`，缺席写未知。读取状态沿用 `qoderGlobalQuota` 自己的三态
   *  （reading / live / stale），检查时点用它的 `checkedAt`。 */
  private accountGlobalQuotaReading(): string {
    const read = this.qoderGlobalQuota;
    const quota = read?.quota;
    const hasQuota =
      Boolean(quota) && typeof quota === "object" && !Array.isArray(quota);
    const checked = read?.checkedAt
      ? new Date(read.checkedAt * 1000).toLocaleTimeString("zh-CN", {
          hour12: false,
        })
      : "时点未知";
    const state = !read
      ? "尚未读取（进入账号页时读取一次；只读查询，不写上游状态）"
      : read.state === "reading"
        ? hasQuota
          ? `本次读取中…（以下为上一次真实读取，读于 ${checked}）`
          : "本次读取中…"
        : read.state === "gated"
          ? hasQuota
            ? `本次未读取（${read.error ?? "运行/停用门未放行"}）；以下为上一次真实读取（读于 ${checked}）`
            : `本次未读取（${read.error ?? "运行/停用门未放行"}）；没有可保留的上一次真实值，未知 ≠ 0`
          : read.state === "stale"
            ? hasQuota
              ? `本次读取失败（${read.error ?? "原因未知"}），以下为上一次真实读取（读于 ${checked}，可能已过期）`
              : `本次读取失败（${read.error ?? "原因未知"}），没有可保留的上一次真实值`
            : `读于 ${checked}`;
    if (!hasQuota)
      return `未知（上游未返回可读额度；未知 ≠ 0）· 失效时间未知（上游未提供到期时点） · ${state}`;
    const q = quota as Record<string, unknown>;
    const sum = this.quotaSum(q);
    const remaining =
      sum.value === null
        ? `未知（分桶没有可读的剩余量${sum.mixed ? "，或分桶单位不一致、不合并" : ""}；未知 ≠ 0）`
        : `${sum.value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ${sum.unit ?? "积分"}`;
    const expires =
      typeof q.expires_at === "string" && q.expires_at.trim()
        ? q.expires_at.slice(0, 50)
        : "未知（上游未提供）";
    return `剩余 ${remaining} · 额度到期 ${expires} · ${state}`;
  }
  /** 上游四桶额度的合计剩余：只把真实数字相加；读到不一致的单位或没有可读桶时
   *  返回 null（未知 ≠ 0，也不跨单位合并）。`mixed` 供调用方如实写明单位不一致，
   *  绝不挑一个单位冒充合计。 */
  private quotaSum(quota: Record<string, unknown>): {
    value: number | null;
    unit: string | null;
    mixed: boolean;
  } {
    let sum = 0,
      seen = 0,
      unit: string | null = null,
      unitSeen = false,
      mixed = false;
    for (const key of ["plan", "add_on", "organization", "session"]) {
      const bucket = quota[key];
      if (!bucket || typeof bucket !== "object" || Array.isArray(bucket))
        continue;
      const b = bucket as Record<string, unknown>;
      const remaining = b.remaining;
      if (
        typeof remaining !== "number" ||
        !Number.isFinite(remaining) ||
        remaining < 0
      )
        continue;
      sum += remaining;
      seen += 1;
      const u =
        typeof b.unit === "string" && b.unit.trim() ? b.unit.trim() : "";
      if (!unitSeen) {
        unitSeen = true;
        unit = u || null;
      } else if ((u || null) !== unit) mixed = true;
    }
    return { value: seen && !mixed ? sum : null, unit, mixed };
  }
  /** 第一阅读层 · 下一主动作：只写这张卡真实提供的下一步与它的边界（该链自己的
   *  只读状态查询，经 8001 只读提交、一次点击直达、无副作用）；真实返回说未认证
   *  时，下一步是先由维护者本人完成登录（先二次确认，绝不自动打开窗口）。没有
   *  账号管理接口的链如实写明可用的只有只读刷新与「详情」，不画死按钮。 */
  private accountNextActionHtml(s: ServiceRow): string {
    const status = this.statusActionOf(s);
    if (!status) {
      const gap = this.statusGap(s);
      const text = s.statusAction
        ? `${gap.word}：${gap.why}；接入前不提供查询按钮`
        : "该链未提供账号管理接口；可用的只有只读刷新与「详情」（不画死按钮）";
      return `<div class="wb-kv"><span>下一主动作（完整）</span><b>${escapeHtml(text)}</b></div>`;
    }
    const result = this.actionResults[status.action];
    const login = (s.manageActions ?? []).find((a) => isLoginAction(a.action));
    const follow =
      result?.authenticated === false && login
        ? `；本次真实返回为未认证：改用「${escapeHtml(login.label)}」，由维护者本人完成登录（先二次确认）`
        : "";
    return `<div class="wb-kv"><span>下一主动作（完整）</span><b>「${escapeHtml(status.label)}」：经 8001 只读提交，一次点击直达、无副作用${follow}</b></div>`;
  }
  /** 该卡自己的折叠（维护者 2026-09-29）：OAuth / 协议与探针的长句、额度与
   *  用量明细、只读查询的原始返回都收在这里。折叠体只放说明、明细与证据，不藏
   *  任何阻塞失败面（`.wb-banner` / `.wb-op-status[data-state="failed"]` /
   *  `.wb-svc-result-failed` / `.wb-log-state` / `.wb-fail-groups` /
   *  `.wb-stale-chip` 六类各有自己的形状与颜色，见 workbench.css §12）。
   *  开合态按服务 id 记在 `accountFoldOpen`，30s 轮询与切 lane 重绘后还原。 */
  private accountCardFoldHtml(s: ServiceRow): string {
    const open = this.accountFoldOpen.has(s.id);
    return (
      `<details class="wb-secondary-details"${open ? " open" : ""} data-wb-account-fold="${s.id}">` +
      `<summary title="完整读数、链说明（OAuth / 协议）、探针证据、额度与用量明细、只读查询的原始返回 · 点击展开 / 收起">详情与原始返回${wbFoldMark()}</summary>` +
      this.accountLoginReadingHtml(s) +
      this.accountQuotaReadingHtml(s) +
      this.accountNextActionHtml(s) +
      `<p class="wb-muted">链说明：${escapeHtml(s.note)}</p>` +
      `<p class="wb-muted">账户层能回答什么：${escapeHtml(this.accountCapabilityNote(s))}</p>` +
      `<p class="wb-muted">探针证据：${escapeHtml(this.probeEvidence(s.id))}</p>` +
      this.accountQuotaHtml(s.id) +
      this.accountRawReturnHtml(s) +
      `</details>`
    );
  }
  /** 一条链账户层能回答什么、不能回答什么（长句）：原为账号页首屏的常显行，
   *  维护者 2026-09-29 把读数与口径分层后收进该卡自己的折叠。 */
  private accountCapabilityNote(s: ServiceRow): string {
    if (s.id === 0)
      return "该链无可信账户余额接口；本机请求与估算费用见「费用」。";
    if (s.id === 2)
      return "上游四桶额度按「查额度」只读读取（每次会记一条操作行、不写上游状态）；未读取前未知，不按 0 显示。";
    if (s.id === 3)
      return "本地 CLIProxy 未提供可核实的账户剩余量与重置时点，不按通用 Gemini API 额度猜算。";
    return "本机实时额度快照（/api/console/quota-snapshot）进入账号页读一次；上游四桶额度按「查额度」只读读取；两者都不写上游状态、不产生计费请求。";
  }
  /** 该卡只读查询动作的**原始返回**（本页记住的真实载荷）：只作证据，不做
   *  解释；没有读取过写「未读取」，读取失败写失败结论，序列化失败写「无法
   *  序列化」。登录动作不是只读查询，其返回不在此展开（终态见「操作记录」）。
   *  载荷有界，超长时如实写截断与原长，不静默丢字段。 */
  private accountRawReturnHtml(s: ServiceRow): string {
    const actions = [
      this.statusActionOf(s)?.action,
      ...(s.manageActions ?? []).map((a) => a.action),
    ]
      .filter((a): a is string => Boolean(a))
      .filter((a) => ACCOUNT_READ_ACTIONS.includes(a));
    const logins = (s.manageActions ?? [])
      .filter((a) => isLoginAction(a.action))
      .map((a) => a.label);
    const loginNote = logins.length
      ? `<p class="wb-muted">登录动作（${escapeHtml(logins.join(" / "))}）不是只读查询，其返回不在此展开；终态见「操作记录」。</p>`
      : "";
    if (!actions.length)
      return (
        loginNote +
        `<p class="wb-muted">该卡没有只读查询动作，没有原始返回可列。</p>`
      );
    return (
      loginNote +
      actions
        .map((action) => {
          const result = this.actionResults[action];
          const failure = this.actionResultFailures.get(action);
          if (!result) {
            return `<div class="wb-kv"><span>原始返回 · ${escapeHtml(action)}</span><b>${failure ? `未取得（${escapeHtml(failure)}）` : "未读取"}</b></div>`;
          }
          let json = "";
          try {
            json = JSON.stringify(result);
          } catch {
            json = "";
          }
          if (!json)
            return `<div class="wb-kv"><span>原始返回 · ${escapeHtml(action)}</span><b>返回值无法序列化（不猜内容）</b></div>`;
          const capped = json.length > ACCOUNT_RAW_RETURN_MAX;
          return `<div class="wb-kv"><span>原始返回 · ${escapeHtml(action)}</span><b>${escapeHtml(capped ? json.slice(0, ACCOUNT_RAW_RETURN_MAX) : json)}${capped ? `…（原始返回共 ${json.length} 字符，此处截断到 ${ACCOUNT_RAW_RETURN_MAX}；完整终态见「操作记录」的该动作行，账本读取有界）` : ""}</b></div>`;
        })
        .join("")
    );
  }
  /** 账号页：一条链的认证状态 + 检查时点（管理详情面板的一行读数；链名已在
   *  面板头或工作区头里，标签不再重复）。
   *  值只来自该链只读查询动作的真实返回，绝不由前端推断；没有返回过 = 未读取，
   *  失败 = 失败 + 保留上一次真实值。只读查询是一次点击直达的动作，不需要
   *  二次确认。 */
  private accountAuthHtml(s: ServiceRow): string {
    return `<div class="wb-kv"><span>认证状态</span><b>${this.accountAuthValueHtml(s)}</b></div>`;
  }
  /** 一条链登录状态的**值**（不带标签，账号卡第一阅读层与管理详情共用）：
   *  状态词只来自该链只读查询动作的真实返回（`authenticated` / `state`，绝不
   *  由前端推断），检查时点用返回自己的 `checked_epoch`；没有返回过 = 未读取，
   *  失败 = 失败 + 保留上一次真实值。 */
  private accountAuthValueHtml(s: ServiceRow): string {
    return escapeHtml(this.accountAuthPlain(s));
  }
  /** `accountAuthValueHtml()` as plain text: the short login cell puts it in a
   *  `title`, so it is escaped once, where it is placed. */
  private accountAuthPlain(s: ServiceRow): string {
    const statusAction = this.statusActionOf(s);
    const action = statusAction?.action;
    if (!action) {
      if (!s.statusAction)
        return "未知（该链未提供只读认证查询接口，不猜一个状态）";
      const gap = this.statusGap(s);
      return `未知（${gap.word}：${gap.why}；进程运行或端口可达不等于已登录）`;
    }
    const result = this.actionResults[action];
    // 失败记忆（带实测 HTTP 状态或操作终态错误）：没有它时，一次失败的读取会
    // 落进「未读取」分支，把「没读到」写成「还没问过」。
    const failure = this.actionResultFailures.get(action);
    if (!result) {
      return failure
        ? `读取失败（${failure}）：本次没有真实返回值，登录状态未知，不折成「未认证」；点「${statusAction!.label}」重试只读读取。`
        : `未读取（点「${statusAction!.label}」只读读取；未读取前不判登录有效或失效）`;
    }
    const at = this.actionCheckedAt(result);
    if (action === "cliproxy.status") {
      const stale = failure
        ? ` · 本次读取失败（${failure}），以上为上一次真实读取`
        : "";
      return `${this.cliproxyLoginReading(result).plain} · 读于 ${at}${stale}`;
    }
    const authenticated = result.authenticated;
    const state =
      typeof authenticated === "boolean"
        ? authenticated
          ? "已认证"
          : "未认证（该链自报）"
        : typeof result.state === "string" && result.state
          ? `状态 ${result.state}${typeof result.label === "string" && result.label ? `（${result.label}）` : ""}`
          : "已读取（响应未含状态词，不猜）";
    const note =
      typeof result.note === "string" && result.note ? ` · ${result.note}` : "";
    const verified =
      result.verified === false ? " · 未主动核验（内存态只读）" : "";
    // 本次读取失败但有上一次真实值：保留真实值并标陈旧，绝不因失败把它折成
    // 「未认证」，也不假装刷新成功。
    const stale = failure
      ? ` · 本次读取失败（${failure}），以上为上一次真实读取`
      : "";
    return `${state} · 读于 ${at}${verified}${note}${stale}`;
  }
  /** A12-3 ① 国内：SECONDARY detail of the CN chain — the upstream four
   *  buckets read by the 查额度 action, plus this machine's own ledger
   *  credits/tokens. The live snapshot itself lives on the lane's first screen
   *  (qoderCnQuotaHtml), so the number is never rendered twice. */
  private qoderCnAccountHtml(): string {
    // The 查额度 action still reads the upstream four buckets (absolute
    // numbers, no ratio); they render as a SECONDARY block only after that
    // action has landed, so the live snapshot above stays the primary reading.
    const read = this.actionResults["qoder.quota_read"];
    const bucketQuota = read?.quota;
    const buckets =
      bucketQuota &&
      typeof bucketQuota === "object" &&
      !Array.isArray(bucketQuota)
        ? `<div class="wb-quota"><small>上游四桶额度（查额度读于 ${escapeHtml(this.actionCheckedAt(read))} · 上游自报）</small>${this.quotaBucketsHtml(bucketQuota as Record<string, unknown>) || `<p class="wb-quota-note">上游未返回可展示的额度桶；未知 ≠ 零。</p>`}</div>`
        : "";
    const credits = this.chainCredits("qoder");
    const creditsHtml = `<p class="wb-muted">本机积分（账本累计 · /api/summary）：${credits === undefined ? "未知（聚合未返回该链积分，不按 0 显示）" : fmtCredits(credits)}</p>`;
    return (
      buckets +
      this.usageBlockHtml(
        "qoder_cn",
        "本机用量（账本 · 北京时间自然日）",
        "complete",
        creditsHtml
      )
    );
  }
  /** A12-3 ① 国际：the upstream four-bucket quota (absolute numbers)
   *  plus the RECORDED ledger numbers. F5: the quota is read once on lane
   *  entry through the read-only adapter action (the CN card's own
   *  /api/console/quota-snapshot has no international counterpart), keeping
   *  the same semantics — number + source + check time + manual refresh, a
   *  failed read keeps the last REAL value and marks it stale, and an unknown
   *  value is never folded into 0. The recorded-call numbers come from the
   *  LEDGER records: 今日/近 7 天 from /api/console/usage-tokens (complete +
   *  partial requests, so records whose rate cannot be computed are included)
   *  and 近 24 小时 from the rate endpoint's own per-source ledger count
   *  `recorded_requests` (F5b — the rate buckets alone would hide records
   *  that cannot compute a rate); a failed read is 未知 and a read ledger with
   *  an empty window is 无记录. */
  private qoderGlobalAccountHtml(): string {
    const read = this.qoderGlobalQuota;
    const quota = read?.quota;
    const hasQuota =
      Boolean(quota) && typeof quota === "object" && !Array.isArray(quota);
    const checked = read?.checkedAt
      ? new Date(read.checkedAt * 1000).toLocaleTimeString("zh-CN", {
          hour12: false,
        })
      : "未知";
    const expires = hasQuota
      ? (quota as Record<string, unknown>).expires_at
      : undefined;
    const expiry =
      typeof expires === "string" && expires.trim()
        ? escapeHtml(expires.slice(0, 50))
        : "上游未提供";
    const stateText = !read
      ? "尚未读取（进入账号页时读取一次；只读查询，不写上游状态）"
      : read.state === "reading"
        ? hasQuota
          ? `本次读取中…（下方为上一次真实读取，读于 ${checked}）`
          : "本次读取中…"
        : read.state === "gated"
          ? hasQuota
            ? `本次未读取（${read.error ?? "运行/停用门未放行"}）；以下为上一次真实读取（读于 ${checked}）`
            : `本次未读取（${read.error ?? "运行/停用门未放行"}）；没有可保留的上一次真实值，未知 ≠ 0`
          : read.state === "stale"
            ? hasQuota
              ? `本次读取失败（${read.error ?? "原因未知"}）；以下为上一次真实读取（读于 ${checked}，可能已过期）`
              : `本次读取失败（${read.error ?? "原因未知"}）；没有可保留的上一次真实值，未知 ≠ 0`
            : `本次读到（读于 ${checked}）`;
    // 门拒绝时首层只留短句，完整裁决句进状态行的 title（读数格不被长句撑高）。
    const gateTitle =
      read?.state === "gated" && this.qoderGlobalQuotaGate
        ? ` title="${escapeHtml(this.qoderGlobalQuotaGate.detail)}"`
        : "";
    const quotaHtml = `<div class="wb-quota"><small>上游四桶额度 · 来源 qoderglobal.quota（只读 GET /quota 透传 · 上游自报）</small>${hasQuota ? this.quotaBucketsHtml(quota as Record<string, unknown>) || `<p class="wb-quota-note">上游未返回可展示的额度桶；未知 ≠ 零。</p>` : `<p class="wb-quota-note">额度桶未采集；剩余、已用与总量均未知，不按 0 显示。</p>`}<small${gateTitle}>${escapeHtml(stateText)} · 额度到期：${hasQuota ? expiry : "未知"} · 5 小时窗口重置：上游未提供</small><button type="button" class="wb-btn wb-btn-sm" data-wb-qoder-global-quota-refresh="1" title="重新读取国际链额度（只读，不写上游状态；每次点击一次）" aria-label="刷新国际链额度">${wbIcon("refresh")}${wbIconHint("刷新国际链额度")}</button></div>`;
    const labels = this.beijingDayKeys(14);
    const creditsHtml = `<p class="wb-muted">可归因积分：未知（账本未记录该来源的可归因积分，不按 0 显示，也不与 Token 合成合计费用）。已记录调用（账本口径 · 含无法计算速率的记录）：今日 ${this.sourceLedgerCount("qoder_global", [labels.at(-1)!])} · 近 7 天 ${this.sourceLedgerCount("qoder_global", labels.slice(-7))}（/api/console/usage-tokens · 北京时间自然日）；近 24 小时已记录调用 ${this.sourceRecorded24h("qoder_global")}（/api/console/usage-rates 的 recorded_requests · 该窗口内该来源被账本记录的请求数，含无法计算速率的记录；「无记录」= 账本已读取且该窗口无记录，「未知」= 账本未读取或响应未提供）。</p>`;
    return (
      quotaHtml +
      this.usageBlockHtml(
        "qoder_global",
        "已记录用量（账本 · 北京时间自然日）",
        "output-only",
        creditsHtml
      )
    );
  }
  /** The check time of one remembered action result (epoch seconds) — an
   *  absent stamp is 未知, never this page's clock. */
  private actionCheckedAt(result: Record<string, unknown> | undefined): string {
    const epoch = result?.checked_epoch;
    return typeof epoch === "number" && epoch > 0
      ? new Date(epoch * 1000).toLocaleTimeString("zh-CN", { hour12: false })
      : "未知";
  }
  /** A12-3 ① 用量块: today / last-7-day ABSOLUTE numbers for one
   *  source. `complete` sums ONLY the requests that carry BOTH components
   *  (完整口径 — the backend's input_tokens/output_tokens already sum exactly
   *  those, so one partial request in a bucket never pushes its complete
   *  neighbours out of the total, F1); `output-only` reports the ledger's
   *  visible output numbers (complete + the partial buckets'
   *  partial_output_tokens) and names an unknown input as 未知. A window with
   *  no row at all is 无记录 — a recorded zero stays a real 0, and neither is
   *  ever invented. Rows whose timestamp is invalid (date=null) belong to no
   *  window: they are counted on their own 时间无法判定 line instead of being
   *  dropped or guessed onto a day. */
  private usageBlockHtml(
    source: string,
    title: string,
    mode: "complete" | "output-only",
    extraHtml: string
  ): string {
    const read = this.usageTokens;
    const head = `<small>${escapeHtml(title)}</small>`;
    const refresh = `<button type="button" class="wb-btn wb-btn-sm" data-wb-usage-refresh="1" title="重新读取用量端点（只读，有界）" aria-label="刷新用量">${wbIcon("refresh")}${wbIconHint("刷新用量")}</button>`;
    if (!read || !read.payload) {
      return `<div class="wb-quota">${head}<p class="wb-quota-note">${this.usageReadNote(read, "用量")}</p>${refresh}</div>`;
    }
    const labels = this.beijingDayKeys(14);
    const window = (days: string[]) => {
      let complete = 0,
        input = 0,
        output = 0,
        completeRows = 0,
        missingRows = 0;
      let partialRows = 0,
        partialRequests = 0,
        partialKnown = true,
        outputAll: number | null = null;
      let partialInput: number | null = null,
        partialOutput: number | null = null,
        partialOutputMissing = 0;
      for (const r of read.payload!.days) {
        if (r.source !== source || !days.includes(r.date)) continue;
        // F1: 完整总量只累加「两分量都齐全」的请求（后端 input/output 即这两项
        // 之和），同桶里一条缺分量的请求不再把其它完整请求挤出总量；该桶的
        // 部分请求另列（partial_*），可展示的输出用于「输出（账本可见）」。
        const total = this.completeTokens(r);
        if (total !== null) {
          complete += total;
          input += r.input_tokens ?? 0;
          output += r.output_tokens ?? 0;
          completeRows += 1;
        }
        const partial = this.partialRequests(r);
        if (partial === null) partialKnown = false;
        else if (partial > 0) {
          partialRows += 1;
          partialRequests += partial;
          const pIn =
            typeof r.partial_input_tokens === "number" &&
            Number.isFinite(r.partial_input_tokens)
              ? r.partial_input_tokens
              : null;
          const pOut = this.partialOutputTokens(r);
          if (pIn !== null) partialInput = (partialInput ?? 0) + pIn;
          if (pOut === null) partialOutputMissing += 1;
          else partialOutput = (partialOutput ?? 0) + pOut;
        }
        // 输出（账本可见）= 完整请求的输出 + 部分请求里可见的输出；两者都是
        // 账本记录值，缺失的分量不补 0（缺输出 Token 的请求不进入这一列）。
        if (
          typeof r.output_tokens === "number" &&
          Number.isFinite(r.output_tokens)
        )
          outputAll = (outputAll ?? 0) + r.output_tokens;
        const visiblePartialOut = this.partialOutputTokens(r);
        if (visiblePartialOut !== null)
          outputAll = (outputAll ?? 0) + visiblePartialOut;
      }
      for (const m of read.payload!.missing) {
        // date=null 的行（时间戳无效）不属于任何窗口，单独计数（见下方 undatedNote），
        // 既不静默丢弃，也不硬塞进今日 / 近 7 天。
        if (m.source === source && m.date !== null && days.includes(m.date))
          missingRows += 1;
      }
      return {
        complete,
        input,
        output,
        completeRows,
        missingRows,
        partialRows,
        partialRequests,
        partialKnown,
        outputAll,
        partialInput,
        partialOutput,
        partialOutputMissing,
      };
    };
    const bucket = (label: string, v: ReturnType<typeof window>) => {
      const partialCountText = v.partialKnown
        ? `${v.partialRequests.toLocaleString("zh-CN")} 条`
        : "条数未知";
      const partialOutText =
        v.partialOutput === null ? "未知" : fmtTokens(v.partialOutput);
      // 输出可见只在「完整口径」块里另列：output-only 块的主数字已经含了它。
      const partialDetail =
        mode === "complete"
          ? `，另列：输出可见 ${partialOutText}${v.partialOutputMissing ? `，另有 ${v.partialOutputMissing} 行连输出也没有` : ""}`
          : "";
      const partialClause =
        v.partialRows || !v.partialKnown
          ? `；另有缺分量请求 ${partialCountText}（不混入完整总量${partialDetail}）`
          : "";
      const inputVisible = v.input + (v.partialInput ?? 0);
      const inputText =
        v.partialInput === null && (v.partialRows || !v.partialKnown)
          ? "未知"
          : fmtTokens(inputVisible);
      const inputSuffix =
        v.partialInput !== null && v.partialRows ? "（含部分可见）" : "";
      const value =
        mode === "complete"
          ? v.completeRows
            ? `完整 Token ${fmtTokens(v.complete)}（输入 ${fmtTokens(v.input)} / 输出 ${fmtTokens(v.output)}）${partialClause}`
            : `完整 Token 未知（该窗口没有可展示的完整请求：缺分量请求 ${partialCountText}，另列不计入）`
          : v.completeRows || v.partialRows || !v.partialKnown
            ? `输出（账本可见）${v.outputAll === null ? "未知" : fmtTokens(v.outputAll)} · 输入 ${inputText}${inputSuffix}${partialClause}`
            : `无记录（该窗口没有该来源的账本行，无法判定为零）`;
      return `<div class="wb-quota-bucket"><div><strong>${escapeHtml(label)}</strong><span>${value}</span></div></div>`;
    };
    const today = window([labels.at(-1)!]);
    const week = window(labels.slice(-7));
    const partialNote =
      !today.partialKnown || !week.partialKnown
        ? `<small>缺失分量：后端未返回 partial_requests 契约字段，条数未知（不按 0 计）；可展示的分量见「仅输出可见」另列。</small>`
        : today.partialRequests + week.partialRequests
          ? `<small>缺失分量：今日 ${today.partialRequests} 条 · 近 7 天 ${week.partialRequests} 条部分请求（输入或输出缺一 → 该分量显示「未知」，不补 0，不计入完整总量；可展示的输出另列）</small>`
          : `<small>缺失分量：今日与近 7 天均无（窗口内每个请求的输入与输出都齐全）</small>`;
    const rowsNote =
      today.missingRows + week.missingRows
        ? `<small>另列：今日 ${today.missingRows} 行 · 近 7 天 ${week.missingRows} 行记录含缺分量请求，未混入上面的完整总量（分量数字见「仅输出可见」）。</small>`
        : "";
    const undatedRows = read.payload.missing.filter(
      (m) => m.source === source && m.date === null
    ).length;
    const undatedNote = undatedRows
      ? `<small>时间无法判定（另列）：${undatedRows} 行记录时间戳无效，不归任何一天，既不进今日 / 近 7 天合计，也不按 0 计。</small>`
      : "";
    return `<div class="wb-quota">${head}${bucket("今日", today)}${bucket("近 7 天", week)}${partialNote}${rowsNote}${undatedNote}${extraHtml}${refresh}<small>${this.usageReadNote(read, "用量")}</small></div>`;
  }
  /** Upstream four-bucket quota, ABSOLUTE numbers only: the used-ratio
   *  progress bar was removed (A12-3 ⑤) and an unknown total stays 未知
   *  instead of being folded into a fake ratio. */
  private quotaBucketsHtml(quota: Record<string, unknown>): string {
    const buckets = ["plan", "add_on", "organization", "session"] as const;
    const labels: Record<string, string> = {
      plan: "订阅",
      add_on: "加购",
      organization: "组织",
      session: "会话",
    };
    const num = (value: unknown): number | null =>
      typeof value === "number" && Number.isFinite(value) && value >= 0
        ? value
        : null;
    const value = (v: number | null) =>
      v === null
        ? "未知"
        : v.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
    return buckets
      .map((key) => {
        const bucket = quota[key];
        if (!bucket || typeof bucket !== "object" || Array.isArray(bucket))
          return "";
        const b = bucket as Record<string, unknown>;
        const unit =
          typeof b.unit === "string" ? escapeHtml(b.unit.slice(0, 20)) : "积分";
        return `<div class="wb-quota-bucket"><div><strong>${labels[key]}</strong><span>剩余 ${value(num(b.remaining))} ${unit} · 已用 ${value(num(b.used))} / 总量 ${value(num(b.total))}</span></div></div>`;
      })
      .filter(Boolean)
      .join("");
  }
  /** Ledger credits for one aggregated chain (/api/summary
   *  services.*.credits): undefined means the aggregate has not landed or
   *  carries no credits for that chain — 未知, never 0. */
  private chainCredits(name: string): number | undefined {
    return this.chainFees?.find((c) => c.name === name)?.credits;
  }
  /** F5b: the rate-COMPUTABLE request count of one source over the bounded
   *  24h window — the sum over the rate buckets the 24h panel shows. The
   *  endpoint emits a bucket only for records whose rate CAN be computed, so
   *  this count is strictly the rate-computable part of the window; the
   *  LEDGER-record count (which includes records that cannot compute a rate)
   *  comes from sourceRecorded24h below. An absent read → 未知; an empty
   *  window → 0 (a real 0 of the read window, never a fabricated one). */
  private sourceRateComputable24h(source: string): number | null {
    const read = this.usageRates;
    if (!read || !read.payload) return null;
    const rows = read.payload.hours.filter((r) => r.source === source);
    return rows.reduce(
      (s, r) => s + (Number.isFinite(r.requests) ? r.requests : 0),
      0
    );
  }
  /** F5b: the LEDGER-recorded call count of one source over the 24h window,
   *  from /api/console/usage-rates `recorded_requests` — the endpoint's own
   *  per-source count over the SAME hour window as its buckets, including the
   *  records whose rate cannot be computed. It is therefore the 「近 24 小时已
   *  记录调用（按账本）」 number, not the rate-bucket sum. 0 is a REAL zero
   *  (the ledger WAS read and holds no record for the window → 无记录), null is
   *  未知 (that source's ledger was not read — a read failure is never folded
   *  into 0), and an absent read / absent key is 未知 too. When the ledger
   *  count is positive while nothing is rate-computable, that is stated instead
   *  of being shown as 0 or as a missing row. */
  private sourceRecorded24h(source: string): string {
    const read = this.usageRates;
    if (!read || !read.payload) return "未知（速率端点未读取）";
    const recorded = read.payload.recorded_requests?.[source];
    if (recorded === null) return "未知（该来源账本未读取）";
    if (typeof recorded !== "number" || !Number.isFinite(recorded))
      return "未知（响应未提供该来源的账本计数）";
    if (recorded === 0) return "无记录（0 条）";
    const computable = this.sourceRateComputable24h(source);
    if (computable === null) return `${recorded.toLocaleString("zh-CN")} 条`;
    return computable === 0
      ? `${recorded.toLocaleString("zh-CN")} 条（有记录但均无法计算速率）`
      : `${recorded.toLocaleString("zh-CN")} 条（其中可计算速率 ${computable.toLocaleString("zh-CN")} 条）`;
  }
  /** F5: ledger-record count of one source over a Beijing natural-day window,
   *  from /api/console/usage-tokens (the read the usage block already shows).
   *  complete_requests + partial_requests = every ledger request in the
   *  bucket, so a record that cannot compute a rate is INCLUDED — unlike the
   *  rate buckets, which only carry rate-computable records. An absent read is
   *  未知, a missing contract field is 未知 (never a fabricated 0), a real 0
   *  stays a real 0, and a window with no row is 无记录 — unless the server's
   *  own coverage says the ledger was NOT read (账本缺席) or the source is not
   *  collecting (来源未启用), which are never folded into 无记录 either. */
  private sourceLedgerCount(source: string, days: string[]): string {
    const read = this.usageTokens;
    if (!read || !read.payload) return "未知";
    const rows = read.payload.days.filter(
      (r) => r.source === source && days.includes(r.date)
    );
    if (!rows.length) {
      const coverage = read.payload.coverage?.sources?.[source];
      if (coverage?.ledger_present === false) return "账本未读取（未知）";
      if (coverage?.enabled === false) return "来源未启用（未知）";
      return "无记录";
    }
    let total = 0;
    for (const r of rows) {
      if (
        !Number.isFinite(r.complete_requests) ||
        !Number.isFinite(r.partial_requests)
      )
        return "未知";
      total += r.complete_requests + r.partial_requests;
    }
    return `${total.toLocaleString("zh-CN")} 条`;
  }
  /** An absolute ledger/quota number: an absent value is 未知, never 0. */
  private absolute(value: number | null | undefined): string {
    return typeof value === "number" && Number.isFinite(value)
      ? value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
      : "未知";
  }
  private sourceLabel(src: string): string {
    const map: Record<string, string> = {
      um: "UM 上游",
      m365: "M365",
      qoder_cn: "Qoder（国内）",
      qoder_global: "Qoder（国际）",
      cliproxy: "Gemini（CLIProxy）",
    };
    return map[src] ?? src;
  }
  /** Real failures only (R20-03): the issue views are driven by the 8001
   *  operation ledger inside a management session; without a session or with
   *  an empty ledger the surface says so honestly. Knowledge documents are
   *  never presented as live alerts. */
  private failuresNote(all: boolean) {
    // 读取在途不写「不可读」：`undefined` = 本次读取尚未返回，只有读取真的
    // 失败（`gated`，带实测原因）才写失败句。应答了 HTTP 错误就写应答错误，
    // 不写成「不可达」——客户端没有测到不可达。
    if (this.opsRecent === undefined)
      return "8001 操作账本读取中…（未返回前不写「不可读」）。";
    if (this.opsRecent?.state !== "live")
      return `${this.opsReadFailureText()}；无真实操作记录（恢复后自动接入）。`;
    return all
      ? "最近真实操作（8001 操作记录，本机直读）。"
      : "当前失败操作（来自 8001 操作账本；为空即如实显示空态）。";
  }
  /** Issues lane and log lane share one runs-list surface: a bordered card
   *  of operation rows (state in Chinese) or one honest empty card — the
   *  old two stacked muted lines said the same thing twice. */
  /** Key row lookup for the rename / adjust-cap / toggle branches. */
  private shareKeyRow(id: string): ShareKeyRow | undefined {
    if (this.shareKeys?.state !== "live") return undefined;
    return this.shareKeys.rows.find((row) => row.id === id);
  }
  /** R3 share page: a compact resident overview (status, readiness, address,
   *  actions and only real abnormalities) above three tabs. Keys is the default;
   *  diagnostics and usage are one click away. Reads still go out immediately on
   *  lane entry; a failed read keeps its own reason, and plaintext keeps its
   *  existing session-only lifetime. */
  private shareLaneHtml(): string {
    // Lane entry issues BOTH reads immediately. The old guard tested
    // `sessionLive()` FIRST and returned 「8001 不可达：分享网关状态暂不可读」 before a
    // single request had been sent — a verdict the page had not asked for. The
    // reads now always go out; while they are unanswered the two blocks below
    // say 读取中…, and only a FAILED read writes its own failure sentence
    // (the request's own reason names the transport failure).
    if (!this.shareLaneVisited) {
      // Direct renderPanel entries (select() already refreshed on lane entry;
      // the in-flight guard dedupes the overlap).
      this.shareLaneVisited = true;
      this.refreshShareData();
    }
    const status = this.shareStatus;
    const keys = this.shareKeys;
    const keysHtml = !keys
      ? `<p class="wb-muted" data-state="loading">密钥列表读取中…</p>`
      : keys.state === "gated"
        ? `<p class="wb-muted">密钥列表不可取得（无凭据会话）。</p>`
        : keys.state === "refused"
          ? `<p class="wb-muted">密钥列表被拒绝（403 权限不符）。</p>`
          : keys.state === "loading"
            ? `<p class="wb-muted" data-state="loading">密钥列表读取中…</p>`
            : keys.state === "error"
              ? `<p class="wb-muted">密钥列表读取失败（${escapeHtml(keys.reason)}）。<button type="button" class="wb-btn wb-btn-sm" data-wb-share-refresh aria-label="重试读取密钥列表">${wbIcon("refresh")}${wbIconHint("重试读取密钥列表")}</button></p>`
              : this.shareKeysTableHtml(keys.rows);
    const keyCount = keys?.state === "live" ? keys.rows.length : null;
    const tab = (
      key: "keys" | "diagnostics" | "usage",
      label: string,
      count?: number | null
    ) => {
      const selected = this.shareTab === key;
      return `<button type="button" role="tab" class="wb-share-tab" id="wb-share-tab-${key}" aria-controls="wb-share-panel-${key}" aria-selected="${selected}" tabindex="${selected ? "0" : "-1"}" data-wb-share-tab="${key}" data-wb-key="share-tab-${key}">${label}${typeof count === "number" ? `<span>${count}</span>` : ""}</button>`;
    };
    return (
      `<div class="wb-share-grid">` +
      this.shareHeroHtml(status) +
      `<div class="wb-share-tabs" role="tablist" aria-label="分享管理内容">` +
      tab("keys", "密钥", keyCount) +
      tab("diagnostics", "诊断与恢复") +
      tab("usage", "用量") +
      `</div>` +
      `<section role="tabpanel" class="wb-share-panel wb-share-keys-panel wb-share-access" id="wb-share-panel-keys" aria-labelledby="wb-share-tab-keys" data-wb-share-tabpanel="keys" data-wb-key="share-panel-keys"${this.shareTab === "keys" ? "" : " hidden"}>` +
      `<div class="wb-section-head"><div><h3>访问密钥</h3><small>${keyCount === null ? "密钥数读取中" : `${keyCount} 个密钥`}</small></div>${this.shareKeyCreateButtonHtml()}</div>` +
      `<div class="wb-share-keys">${keysHtml}${!this.shareKeyFormOpen && this.shareKeyError ? `<p class="wb-share-error">${escapeHtml(this.shareKeyError)}</p>` : ""}${this.sharePlaintextPanelHtml()}</div>` +
      `</section>` +
      this.shareDiagnosticsHtml(status) +
      this.shareUsagePanelHtml() +
      `</div>`
    );
  }
  /** Rev15 A15 layered maintenance: the share chain's two processes are read and
   *  kept apart - 分享入口 · 8100 and UM 转发与登录 · 8110. Readings come from the
   *  read-only `share.diagnostics` action (run when asked: each run is one
   *  operation row); every layer keeps its own state, verdict, check time,
   *  staleness and reason, and a reachable port is never shown as a valid login.
   *  The single-layer buttons are the backend's own actions, offered only when
   *  this backend registers them enabled, behind the usual two-step confirm;
   *  whole-chain open / stop / emergency stop stay in the hero. While a stop
   *  intent is in force no single-layer recovery is offered. */
  /** R3 resident overview: one compact row of honest readings, the lifecycle
   *  actions, the address line and only real abnormality banners. A normal page
   *  adds no fault sentence; a stopped/cancelled share is not drawn as a fault
   *  that needs automatic reopening. */
  private shareHeroHtml(status?: ShareStatusState): string {
    const payload = status?.state === "live" ? status.payload : undefined;
    const state = payload
      ? this.shareStateOf(payload)
      : {
          key: "unconfirmed",
          text:
            status && status.state !== "loading"
              ? "状态未确认（读取失败）"
              : "状态未确认（读取中）",
        };
    const local = payload
      ? localShareReadiness(payload)
      : { state: "idle" as const, text: "未确认" };
    const publicRead = this.publicShare;
    const publicLive = publicRead?.state === "live" ? publicRead.payload : null;
    const cancelled = publicLive?.cancel?.cancelled === true;
    const tls = publicTlsReading(publicLive?.snapshot ?? null, cancelled);
    const manual = publicShareSummary(
      payload ?? {},
      this.latestSharePublicTest()
    );
    return (
      `<section class="wb-block wb-share-block wb-share-hero">` +
      `<div class="wb-share-topline">` +
      `<span class="wb-share-pill wb-share-${escapeHtml(state.key)}" data-wb-share-state>${escapeHtml(state.text)}</span>` +
      `<span class="wb-share-top-reading" data-wb-share-top="local">本机就绪 <b class="wb-badge wb-badge-${local.state}">${escapeHtml(local.text)}</b></span>` +
      `<span class="wb-share-top-reading" data-wb-share-public-tls="1">公网 TLS <b class="wb-badge wb-badge-${tls.state}" role="status">${escapeHtml(tls.text)}</b></span>` +
      `<span class="wb-share-top-reading" data-wb-share-public-summary="1">手动模型测试 <b class="wb-badge wb-badge-${manual.state}" role="status">${escapeHtml(manual.text)}</b></span>` +
      `<div class="wb-share-top-actions">${this.shareActionButtonsHtml(payload ?? {})}<button type="button" class="wb-btn wb-btn-sm" data-wb-share-refresh title="重新读取分享状态、密钥、用量与公网巡检（只读）" aria-label="刷新分享数据">${wbIcon("refresh")}刷新</button></div>` +
      `</div>` +
      `<section class="wb-share-access-url"><h4>公网地址</h4>${this.shareUrlHtml(status)}</section>` +
      this.shareStatusFailureBanner(status) +
      (payload ? this.shareBrokerRecoveryHtml(payload) : "") +
      this.sharePublicBanner() +
      `</section>`
    );
  }
  /** A measured read failure is written once, in the resident overview. The
   *  address line points here instead of repeating the same reason. */
  private shareStatusFailureBanner(status?: ShareStatusState): string {
    if (!status || status.state === "loading") return "";
    const text =
      status.state === "gated"
        ? "无凭据会话（8001 不可达或未授权）"
        : status.state === "refused"
          ? "403 权限不符"
          : status.state === "error"
            ? `分享状态读取失败（${status.reason}）；8001 不可达时没有权威状态，启停动作仍可手动提交`
            : "";
    if (!text) return "";
    return `<div class="wb-share-alert" data-wb-share-alert="status-read" role="status"><div><strong>状态读取失败</strong><span>${escapeHtml(text)}</span></div></div>`;
  }
  /** Public supervision abnormalities stay visible in the overview: all_failed,
   *  degraded and stale carry their own reason and failing entry; a manual cancel
   *  is a paused note (never a fault that asks to reopen automatically). A
   *  stopped share or a not-yet-checked snapshot adds no fault banner. */
  private sharePublicBanner(): string {
    const read = this.publicShare;
    if (!read || read.state === "loading") return "";
    if (read.state !== "live") {
      const text =
        read.state === "refused"
          ? "公网巡检读口被拒绝（403 权限不符）"
          : read.state === "unavailable"
            ? `公网巡检读口不可取得（${read.reason}）`
            : `公网巡检读取失败（${read.reason}）`;
      return `<div class="wb-share-alert" data-wb-share-alert="public-read" role="status"><div><strong>公网巡检状态未知</strong><span>${escapeHtml(text)}；未知不是正常，也不自动恢复。</span></div><button type="button" class="wb-btn wb-btn-sm" data-wb-share-refresh>重新读取</button><button type="button" class="wb-btn wb-btn-sm" data-wb-share-tab="diagnostics">查看诊断</button></div>`;
    }
    const live = read.payload;
    const snapshot = live.snapshot ?? null;
    const cancel = live.cancel ?? null;
    if (live.available === false) {
      return `<div class="wb-share-alert" data-wb-share-alert="public-unavailable" role="status"><div><strong>公网巡检状态未知</strong><span>当前服务端未部署公网巡检模块（available:false）；状态一律未提供，不自动恢复。</span></div><button type="button" class="wb-btn wb-btn-sm" data-wb-share-refresh>重新读取</button><button type="button" class="wb-btn wb-btn-sm" data-wb-share-tab="diagnostics">查看诊断</button></div>`;
    }
    if (live.unreadable === true) {
      const checkReason = this.sharePublicCheckReason();
      return `<div class="wb-share-alert" data-wb-share-alert="public-unreadable" role="status"><div><strong>公网巡检状态未知</strong><span>公网巡检状态文件不可读（state_unreadable）；不冒充正常，可一键复查重新生成快照。</span></div><button type="button" class="wb-btn wb-btn-sm" data-wb-share-public="check" title="${escapeHtml(checkReason || "只读 DNS/TLS 复查，不发送推理请求")}"${checkReason ? " disabled" : ""}>一键复查</button><button type="button" class="wb-btn wb-btn-sm" data-wb-share-tab="diagnostics">查看诊断</button></div>`;
    }
    if (cancel?.cancelled === true) {
      return `<div class="wb-share-paused" data-wb-share-alert="public-paused" role="status"><span>自动巡检已暂停（人工取消：${escapeHtml(cancel.reason ?? "原因未提供")}）· 保留现有取消信号，不自动重开。</span><button type="button" class="wb-btn wb-btn-sm" data-wb-share-tab="diagnostics">查看原因</button></div>`;
    }
    if (!snapshot || snapshot.intent?.state === "stopped") return "";
    const now = Date.now() / 1000;
    const stale = publicSupervisionStale(snapshot, now);
    const overall = snapshot.overall;
    const dns = snapshot.dns ?? null;
    const dnsAnomaly = publicDnsAnomaly(dns);
    const dnsSuffix = dnsAnomaly ? ` · ${publicDnsText(dns)}` : "";
    const failed = (snapshot.entries ?? []).filter(
      (entry) =>
        entry.state === "failed" ||
        entry.dns === "failed" ||
        entry.tls === "failed" ||
        entry.certificate === "failed"
    );
    const observed =
      typeof snapshot.observedAt === "number" &&
      Number.isFinite(snapshot.observedAt)
        ? new Date(snapshot.observedAt * 1000).toLocaleString("zh-CN", {
            hour12: false,
          })
        : "未提供";
    const reason = this.sharePublicCheckReason();
    // A pure DNS anomaly is a page-only notice: it never borrows the generic
    // degraded/recovered fault banner or claims an automatic recovery.
    if (dnsAnomaly && overall !== "all_failed" && !failed.length) {
      const ttl = publicDnsTtlText(dns, now);
      return `<div class="wb-share-alert" data-wb-share-alert="public-dns" role="status"><div><strong>${escapeHtml(publicDnsText(dns))}</strong><span>${escapeHtml(snapshot.reason ?? "原因未提供")} · ${escapeHtml(ttl)} · 最近观测 ${escapeHtml(observed)} · DNS 仅页面提示，不自动恢复、不发系统通知</span></div><button type="button" class="wb-btn wb-btn-sm" data-wb-share-public="check" title="${escapeHtml(reason || "只读 DNS/TLS 复查，不发送推理请求")}"${reason ? " disabled" : ""}>一键复查</button><button type="button" class="wb-btn wb-btn-sm" data-wb-share-tab="diagnostics">查看诊断</button></div>`;
    }
    if (
      dns?.addressesTruncated &&
      overall !== "all_failed" &&
      (overall === "incomplete" || !failed.length)
    ) {
      return `<div class="wb-share-alert" data-wb-share-alert="public-dns" role="status"><div><strong>检查未完成（地址覆盖缺口）</strong><span>本轮只检查了前 4 个有效地址，剩余入口未验证；这是未检查完，不是全部失败，不自动恢复、不发系统通知。</span></div><button type="button" class="wb-btn wb-btn-sm" data-wb-share-public="check" title="${escapeHtml(reason || "只读 DNS/TLS 复查，不发送推理请求")}"${reason ? " disabled" : ""}>一键复查</button><button type="button" class="wb-btn wb-btn-sm" data-wb-share-tab="diagnostics">查看诊断</button></div>`;
    }
    if (overall !== "all_failed" && overall !== "degraded" && !stale) return "";
    const title =
      overall === "all_failed"
        ? "公网入口失败"
        : overall === "degraded"
          ? "部分入口异常"
          : "公网巡检结果陈旧";
    const detail =
      (failed.length
        ? failed
            .map(
              (entry) =>
                `${entry.host ?? "host 未提供"}：${entry.reason ?? "原因未提供"}`
            )
            .join("；")
        : "入口明细见诊断页") + dnsSuffix;
    return `<div class="wb-share-alert${overall === "all_failed" ? " wb-share-alert-danger" : ""}" data-wb-share-alert="public-fault" role="status"><div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(snapshot.reason ?? "原因未提供")} · ${escapeHtml(detail)} · 最近观测 ${escapeHtml(observed)}${stale ? " · 超过 2 分钟视为陈旧" : ""}</span></div><button type="button" class="wb-btn wb-btn-sm" data-wb-share-public="check" title="${escapeHtml(reason || "只读 DNS/TLS 复查，不发送推理请求")}"${reason ? " disabled" : ""}>一键复查</button><button type="button" class="wb-btn wb-btn-sm" data-wb-share-tab="diagnostics">查看诊断</button></div>`;
  }
  private shareDiagnosticsHtml(status?: ShareStatusState): string {
    const selected = this.shareTab === "diagnostics";
    return (
      `<section role="tabpanel" class="wb-share-panel wb-share-diagnostics" id="wb-share-panel-diagnostics" aria-labelledby="wb-share-tab-diagnostics" data-wb-share-tabpanel="diagnostics" data-wb-key="share-panel-diagnostics"${selected ? "" : " hidden"}>` +
      `<div class="wb-section-head"><div><h3>诊断与恢复</h3><small>本机、认证与公网分别判断</small></div><button type="button" class="wb-btn wb-btn-sm" data-wb-share-refresh title="重新读取分享状态、密钥、用量与公网巡检（只读）">${wbIcon("refresh")}刷新</button></div>` +
      this.shareLayersHtml() +
      this.sharePublicHtml() +
      `<section class="wb-share-capacity" data-wb-anchor="share-capacity"><div class="wb-section-head"><h3>容量与近期操作</h3><small>上限与最近 8 条分享操作</small></div>${this.shareCapacityHtml(status)}</section>` +
      this.shareConnectionHtml(status) +
      this.sharePreconditionsHtml(status) +
      `</section>`
    );
  }
  private shareUsagePanelHtml(): string {
    const selected = this.shareTab === "usage";
    const usage = this.shareUsageParts();
    return (
      `<section role="tabpanel" class="wb-share-panel wb-share-usage-panel" id="wb-share-panel-usage" aria-labelledby="wb-share-tab-usage" data-wb-share-tabpanel="usage" data-wb-key="share-panel-usage"${selected ? "" : " hidden"}>` +
      `<div class="wb-section-head"><div><h3>分享用量</h3><small>专用后端账本 · 当前返回范围</small></div><span class="wb-muted">人民币 CNY</span></div>` +
      this.shareAttributedKeysHtml() +
      usage.summary +
      `<details class="wb-share-fold" data-wb-share-panel="usage-trend"${this.sharePanelsOpen.has("usage-trend") ? " open" : ""}><summary title="近 14 天费用趋势 · 点击展开 / 收起">近 14 天费用趋势（人民币）${wbFoldMark()}</summary>${usage.chart}</details>` +
      usage.notes +
      `</section>`
    );
  }
  private shareLayersHtml(): string {
    const brokerUnavailable = this.shareBrokerUnavailable();
    const cap = (action: string) =>
      this.actions?.find((r) => r.action === action);
    const gapText = (action: string) => {
      const row = cap(action);
      return !this.actions
        ? "动作注册表未读取（读取中或读取失败）"
        : !row
          ? "当前后端版本不支持（待后端接入）"
          : `已停用：${row.disabled_reason ?? "原因未提供"}`;
    };
    const diag = this.actionResults["share.diagnostics"];
    const failure = this.actionResultFailures.get("share.diagnostics");
    const layers = (
      diag && typeof diag.layers === "object" && diag.layers ? diag.layers : {}
    ) as Record<string, Record<string, unknown>>;
    // A check time from another day carries its date: 「检查于 09:12」 must not
    // read as today's reading.
    const time = (v: unknown) => {
      if (typeof v !== "number" || v <= 0) return "时间未知";
      const at = new Date(v * 1000);
      return at.toDateString() === new Date().toDateString()
        ? at.toLocaleTimeString("zh-CN", { hour12: false })
        : at.toLocaleString("zh-CN", {
            hour12: false,
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
          });
    };
    // The backend's own vocabulary (console_share_actions `_diag_*`): `lost` reads
    // by layer, `unconfirmed` is never shown as a pass, and an unrecognised word
    // stays neutral rather than green.
    const word = (key: string, state: string): [string, string] => {
      const good: Record<string, string> = {
        running: "运行中",
        ok: "正常",
        on: "开启",
        valid: "认证有效",
      };
      const warn: Record<string, string> = {
        logging_in: "认证中",
        maintenance: "维护中",
        draining: "排空中",
      };
      const bad: Record<string, string> = {
        stopped: "已停止",
        off: "关闭",
        invalid: "认证失效",
        failed: "认证失败",
        needs_login: "需要登录",
      };
      const lost: Record<string, string> = {
        connectivity: "连接中断",
        upstream: "不可达",
        security_gate: "条件不满足",
      };
      if (good[state]) return [good[state]!, "ok"];
      if (warn[state]) return [warn[state]!, "warn"];
      if (state === "lost") return [lost[key] ?? "中断", "danger"];
      if (bad[state]) return [bad[state]!, "danger"];
      if (state === "unavailable") return ["不可取得", "idle"];
      if (state === "unconfirmed") return ["未确认", "idle"];
      if (key === "watchdog" && state === "absent")
        return ["任务缺失", "danger"];
      if (key === "watchdog" && state === "queued") return ["等待运行", "warn"];
      return [state === "unknown" || !state ? "未知" : state, "idle"];
    };
    const NAMES: Record<string, string> = {
      share_gateway: "入口进程",
      connectivity: "Tailscale 连接",
      ts_service: "Tailscale 服务",
      funnel: "Funnel 映射",
      security_gate: "安全条件",
      watchdog: "看门狗任务",
      share_backend: "8110 进程",
      backend_auth: "8110 认证",
      upstream: "UM 上游连接",
    };
    const repairButton = () => {
      const enabled = cap("share.repair_limits")?.enabled === true;
      const disabled =
        !enabled || brokerUnavailable || Boolean(busy) || diagBusy;
      const hint = !enabled
        ? gapText("share.repair_limits")
        : brokerUnavailable
          ? "先恢复受限后台，再读取分层状态"
          : "暂停公网并停止分享入口，重建隔离规则；完成后保持关闭，可再点“开启分享”";
      return `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-action="share.repair_limits" title="${escapeHtml(hint)}"${disabled ? " disabled" : ""}>${busy?.action === "share.repair_limits" ? "修复中…" : "修复隔离规则"}</button>`;
    };
    const layerRow = (key: string, compact = false) => {
      const l = layers[key];
      if (!l && key !== "security_gate")
        return `<li><span>${escapeHtml(NAMES[key] ?? key)}</span><span class="wb-badge wb-badge-idle">${diag ? "未返回" : "未读取"}</span></li>`;
      if (!l)
        return `<li><span>安全条件</span><span class="wb-badge wb-badge-idle">未读取</span><small>${compact ? "读取分层状态后显示 WFP 与防火墙证据。" : "点“重新检查”获取 WFP 与防火墙证据。"}</small>${compact ? "" : `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-diag${diagBusy ? " disabled" : ""}>重新检查</button>${repairButton()}`}</li>`;
      const [word0, cls0] = word(key, String(l.state ?? ""));
      // A stale reading keeps its last state but never the fresh green: the
      // badge itself says it is old, so it cannot pass for a current verdict.
      // Staleness is the backend's verdict (l.stale); the page only adds that a
      // failed refresh leaves every reading still shown as the previous one.
      const stale =
        l.state !== "unavailable" && (l.stale === true || Boolean(failure));
      const w = stale ? `${word0}（已陈旧）` : word0;
      const cls = stale && cls0 === "ok" ? "warn" : cls0;
      const verdict =
        l.verdict === "confirmed"
          ? "已确认"
          : l.verdict === "unconfirmed"
            ? "未确认"
            : l.verdict === "unavailable"
              ? "证据不可取得"
              : String(l.verdict ?? "");
      const note = [
        verdict,
        `检查于 ${time(l.checked_at)}`,
        typeof l.reason === "string" && l.reason ? `原因 ${l.reason}` : "",
      ]
        .filter(Boolean)
        .join(" · ");
      let security = "";
      if (key === "security_gate" && !compact) {
        const evidence = l.evidence as Record<string, unknown> | undefined;
        const rawCode =
          typeof evidence?.detail === "string"
            ? evidence.detail
            : "limits_unavailable";
        const code =
          rawCode === "ok" && l.state !== "ok"
            ? "limits_not_satisfied"
            : rawCode;
        const reasons: Record<string, string> = {
          ok: "WFP 隔离和出站防火墙检查均通过。",
          "wfp-filter-missing": "WFP 隔离规则缺失，需要管理员重新部署。",
          "wfp-filter-mismatch": "WFP 隔离范围不匹配，需要管理员重新部署。",
          "firewall-missing": "出站防火墙规则缺失，需要管理员恢复。",
          "firewall-disabled": "出站防火墙规则已停用。",
          "firewall-scope-mismatch":
            "出站防火墙规则的协议、端口、地址或配置范围不符合要求。",
          "wfp-query-failed": "WFP 检查未能完成，不能据此确认安全。",
          "firewall-query-failed": "防火墙检查未能完成，不能据此确认安全。",
          limits_process_failed: "后台检查程序执行失败。",
          limits_launch_failed: "后台检查程序无法启动。",
          limits_timeout: "后台安全检查超时，请重新检查；当前安全条件未确认。",
          limits_executable_missing: "后台缺少安全检查程序。",
          limits_permission_denied: "后台无权启动安全检查程序。",
          limits_unwired: "后台未配置安全检查入口。",
          limits_output_invalid: "后台没有返回有效的检查结果。",
          limits_not_satisfied:
            "隔离规则或防火墙范围不符合要求，需要管理员核查。",
        };
        const explanation =
          l.state === "unconfirmed" || l.state === "unavailable"
            ? (reasons[code] ??
              "管理员后台未能取得完整证据；未确认不代表检查通过。")
            : (reasons[code] ?? "后台返回了安全检查结论。");
        security =
          `<span class="wb-share-safety-actions"><button type="button" class="wb-btn wb-btn-sm" data-wb-share-diag${diagBusy ? " disabled" : ""}>${diagBusy ? "检查中…" : "重新检查"}</button>${repairButton()}<button type="button" class="wb-btn wb-btn-sm" data-wb-share-security-details aria-expanded="${this.shareSecurityDetailsOpen}">${this.shareSecurityDetailsOpen ? "收起原因" : "查看原因"}</button></span>` +
          (this.shareSecurityDetailsOpen
            ? `<small class="wb-share-safety-reason">${escapeHtml(explanation)} 这是管理员后台检查，无需在浏览器里手动勾选。检查于 ${escapeHtml(time(l.checked_at))}。</small>`
            : "");
      }
      return `<li><span>${escapeHtml(NAMES[key] ?? key)}</span><span class="wb-badge wb-badge-${cls}">${escapeHtml(w)}</span><small>${escapeHtml(note)}</small>${security}${key === "security_gate" && !compact ? this.svcResultChip("act:share.repair_limits") : ""}</li>`;
    };
    const faults = (
      (layers.share_gateway?.evidence as Record<string, unknown> | undefined)
        ?.faults as Record<string, unknown> | undefined
    )?.verdict as Record<string, unknown> | undefined;
    const last = faults?.last_fault as Record<string, unknown> | undefined;
    const PHASES: Record<string, string> = {
      auth: "入口凭据",
      config: "配置",
      body: "请求体",
      limit: "限额",
      upstream: "上游",
      stream: "断流",
    };
    const counts =
      faults?.counts_by_phase && typeof faults.counts_by_phase === "object"
        ? Object.entries(faults.counts_by_phase as Record<string, unknown>)
            .filter(([, n]) => typeof n === "number" && n > 0)
            .map(([k, n]) => `${PHASES[k] ?? k} ${n}`)
        : [];
    const countsHtml = counts.length
      ? `<p class="wb-muted">入口留证的故障计数（按阶段）：${escapeHtml(counts.join(" · "))}</p>`
      : "";
    const faultHtml = last
      ? `<p class="wb-share-fault"><b>最近故障</b> ${escapeHtml([time(last.at), last.phase ? `阶段 ${last.phase}` : "", typeof last.status === "number" ? `入口回 HTTP ${last.status}` : "", last.code ? `${last.code}` : "", typeof last.upstream_status === "number" ? `上游 HTTP ${last.upstream_status}` : "", typeof last.duration_ms === "number" ? `耗时 ${Math.round(last.duration_ms)} ms` : "", last.request_id ? `请求 ${last.request_id}` : ""].filter(Boolean).join(" · "))}</p>`
      : faults
        ? `<p class="wb-muted">入口留证中没有最近一次故障。</p>`
        : diag
          ? `<p class="wb-muted">入口故障证据未读取；可点“重新检查”获取。</p>`
          : "";
    const stopInForce = diag?.desired_stop_in_force === true;
    const maintenance = diag?.maintenance as
      Record<string, unknown> | undefined;
    const lease = maintenance?.lease as
      Record<string, unknown> | null | undefined;
    const instance = maintenance?.instance as
      Record<string, unknown> | null | undefined;
    const lastOutcome = maintenance?.last_outcome as
      Record<string, unknown> | null | undefined;
    const lastError = diag?.last_error as
      Record<string, unknown> | null | undefined;
    // 陈旧只降新鲜度（Rev19）：本次刷新失败时，维护监督读数与各层读数一样
    // 保留上一次的真实值并标「已陈旧」——绝不判宕机、也不折成「没有维护」。
    const mark = (w: string, stale: boolean) => (stale ? `${w}（已陈旧）` : w);
    const staleOf = (entry: Record<string, unknown> | null | undefined) =>
      !!entry &&
      entry.state !== "unavailable" &&
      (entry.stale === true || Boolean(failure));
    const outcomeHtml = lastOutcome
      ? `上次维护结果：${lastOutcome.ok === true ? "成功" : lastOutcome.ok === false ? "未成功" : "结果未提供"}${lastOutcome.code ? `（${escapeHtml(String(lastOutcome.code))}）` : ""}${lastOutcome.detail ? ` ${escapeHtml(String(lastOutcome.detail).slice(0, 160))}` : ""}`
      : "";
    const instanceHtml = instance
      ? `受信实例 ${instance.instance_id ? escapeHtml(String(instance.instance_id)) : "未提供"}（状态 ${escapeHtml(String(instance.state ?? "未提供"))} · 在途 ${typeof instance.active_count === "number" ? instance.active_count : "未提供"} · 最后代次 ${typeof instance.last_seen_generation === "number" ? instance.last_seen_generation : "未提供"}）`
      : "";
    const statusLine = [
      stopInForce ? "人工停止意图生效中：不提供单层恢复入口" : "",
      lease
        ? mark(
            `维护租约进行中（${String(lease.component ?? "组件未提供")}${lease.lease_id ? ` · 租约 ${String(lease.lease_id)}` : ""}${lease.round !== undefined && lease.round !== null ? ` · 轮次 ${String(lease.round)}` : ""}）`,
            Boolean(failure)
          )
        : maintenance
          ? "当前没有维护租约（维护视图已读取）"
          : "",
      instance ? mark(instanceHtml, staleOf(instance)) : "",
      lastOutcome ? mark(outcomeHtml, staleOf(lastOutcome)) : "",
      // 权威记录（desired_stop_in_force / last_error）由页面自己的一句话写出：
      // 缺失即未提供，不折成「没有停止意图」。
      lastError && (lastError.code || lastError.message)
        ? `上次维护未成功：${String(lastError.code ?? "")}${lastError.message ? ` ${String(lastError.message).slice(0, 120)}` : ""}`
        : "",
    ]
      .filter(Boolean)
      .map((t) => `<p class="wb-muted">${escapeHtml(t)}</p>`)
      .join("");
    // 监督状态（Rev19 接线）：分享链的独立监督只来自监督日志——console 既有读面
    // （share.diagnostics）带回 `supervision` 段时，按后端自己的形状显示决策
    // （action / code / attempts_this_hour / confirmations / instance / 耗时）与状态
    // （idle_reason / consecutive_failures / restart_attempts_utc / version）；读面
    // 未返回该段时如实写「未返回监督段（未提供 ≠ 监督未运行）」，绝不显示成
    // 已启用、也不写恢复循环。陈旧只降新鲜度。
    const supervision = (
      diag?.supervision && typeof diag.supervision === "object"
        ? diag.supervision
        : null
    ) as Record<string, unknown> | null;
    const supDecision = (
      supervision?.decision && typeof supervision.decision === "object"
        ? supervision.decision
        : null
    ) as Record<string, unknown> | null;
    const supState = (
      supervision?.state && typeof supervision.state === "object"
        ? supervision.state
        : null
    ) as Record<string, unknown> | null;
    const supText = (v: unknown) => (typeof v === "string" && v ? v : "未提供");
    const supCount = (v: unknown) =>
      typeof v === "number" && Number.isFinite(v) ? String(v) : "未提供";
    const supAttempts = Array.isArray(supState?.restart_attempts_utc)
      ? supState.restart_attempts_utc.length
      : null;
    const supStale = Boolean(failure) || supervision?.stale === true;
    const supervisionHtml = supervision
      ? `<p class="wb-muted">监督状态${supStale ? "（已陈旧）" : ""}：决策 ${escapeHtml(supText(supDecision?.action))} / ${escapeHtml(supText(supDecision?.code))} · 本小时尝试 ${escapeHtml(supCount(supDecision?.attempts_this_hour))} 次 · 确认 ${escapeHtml(supCount(supDecision?.confirmations))} · 实例 ${escapeHtml(supText(supDecision?.instance && typeof supDecision.instance === "object" ? (supDecision.instance as Record<string, unknown>).identity : null))}${typeof supDecision?.duration_ms === "number" ? ` · 耗时 ${Math.round(supDecision.duration_ms)} ms` : ""} · 空闲原因 ${escapeHtml(supText(supState?.idle_reason))} · 连续失败 ${escapeHtml(supCount(supState?.consecutive_failures))} · 最近重启尝试 ${supAttempts === null ? "未提供" : `${supAttempts} 次`}${typeof supDecision?.at_utc === "string" && supDecision.at_utc ? ` · 决策于 ${escapeHtml(supDecision.at_utc.slice(0, 19).replace("T", " "))}` : ""}${typeof supDecision?.detail === "string" && supDecision.detail ? `。${escapeHtml(supDecision.detail.slice(0, 200))}` : ""}</p>`
      : diag
        ? `<p class="wb-muted">监督状态：本次分层读面未返回监督段（未提供 ≠ 监督未运行）；监督独立于 8001 与本网页运行，页面不另写恢复循环。整链开启 / 停止 / 紧急停止在上方「开放状态与主操作」。</p>`
        : `<p class="wb-muted">监督状态：尚未读取分层状态，监督段一律未知；监督独立于 8001 与本网页运行，页面不另写恢复循环。整链开启 / 停止 / 紧急停止在上方「开放状态与主操作」。</p>`;
    // 维护前后比较（Rev19 接线）：单层重启动作自己的终态结果带回该动作的
    // `before` / `after` 同层读数（console_share_actions `_diag_pair`）——页面只
    // 比较这一次动作的前后，不重读一遍冒充，缺失的一侧写「未提供」，陈旧只降
    // 新鲜度（同一 `word()` 词表与同一条陈旧口径）。
    const compare = this.shareRestartCompare;
    const compareHtml = (action: string) => {
      if (!compare || compare.action !== action) return "";
      const keys = [
        ...new Set([
          ...Object.keys(compare.before),
          ...Object.keys(compare.after),
        ]),
      ];
      const side = (
        key: string,
        entry: Record<string, unknown> | undefined
      ) => {
        if (!entry) return `<span class="wb-badge wb-badge-idle">未提供</span>`;
        const [w0, cls0] = word(key, String(entry.state ?? ""));
        const stale = staleOf(entry);
        const cls = stale && cls0 === "ok" ? "warn" : cls0;
        return `<span class="wb-badge wb-badge-${cls}">${escapeHtml(stale ? `${w0}（已陈旧）` : w0)}</span>`;
      };
      const rows = keys
        .map((key) => {
          const b = compare.before[key] as Record<string, unknown> | undefined;
          const a = compare.after[key] as Record<string, unknown> | undefined;
          return `<li><span>${escapeHtml(NAMES[key] ?? key)}</span><small>维护前 ${side(key, b)} → 维护后 ${side(key, a)}</small><small>${escapeHtml(`${b && typeof b.checked_at === "number" ? `前 ${time(b.checked_at)}` : "前 时间未提供"} · ${a && typeof a.checked_at === "number" ? `后 ${time(a.checked_at)}` : "后 时间未提供"}`)}</small></li>`;
        })
        .join("");
      return `<div><p class="wb-muted"><b>维护前后比较（本次「${escapeHtml(actionLabel(compare.action))}」自己的前后读数）</b></p><ul class="wb-share-layer-list wb-share-layer-compare">${rows}</ul><p class="wb-muted">比较只来自该动作返回的 before / after（不重读一遍冒充）；缺失的一侧写「未提供」，陈旧只降新鲜度、不判宕机。</p></div>`;
    };
    // The terminal result of a single-layer action sits under its own button
    // (operation-area level of the feedback design), not only in the notice
    // pool where a success fades.
    // Every share lifecycle/maintenance action runs under one orchestration
    // lock on the backend; while one is in flight from this page the single-
    // layer buttons do not offer a second submit (the read-only diagnostics
    // only locks its own button).
    const inFlight = (e: FeedbackEntry) =>
      e.state === "preparing" || e.state === "queued" || e.state === "running";
    const busy = [...this.feedbackEntries.values()].find(
      (e) =>
        inFlight(e) &&
        e.action.startsWith("share.") &&
        e.action !== "share.diagnostics"
    );
    const diagBusy = [...this.feedbackEntries.values()].some(
      (e) => inFlight(e) && e.action === "share.diagnostics"
    );
    const actionBtn = (action: string, label: string, impact: string) => {
      const row = cap(action);
      const chip = this.svcResultChip(`act:${action}`);
      if (row?.enabled && busy) {
        return busy.action === action
          ? `<div class="wb-share-layer-act"><button type="button" class="wb-btn" disabled aria-busy="true">${escapeHtml(label)} · 执行中</button><small>${escapeHtml(busy.text)}</small></div>`
          : `<div class="wb-share-layer-act"><button type="button" class="wb-btn" disabled>${escapeHtml(label)}</button><small>${escapeHtml(`「${busy.label}」进行中，结束后再操作`)}</small>${chip}</div>`;
      }
      if (!row?.enabled)
        return `<div class="wb-share-layer-act"><button type="button" class="wb-btn" disabled>${escapeHtml(label)}</button><small>${escapeHtml(gapText(action))}</small>${chip}</div>`;
      if (stopInForce)
        return `<div class="wb-share-layer-act"><button type="button" class="wb-btn" disabled>${escapeHtml(label)}</button><small>人工停止生效中</small>${chip}</div>`;
      if (brokerUnavailable)
        return `<div class="wb-share-layer-act"><button type="button" class="wb-btn" disabled>${escapeHtml(label)}</button><small>先按上方指引恢复受限后台，再读取分层状态</small>${chip}</div>`;
      return `<div class="wb-share-layer-act"><button type="button" class="wb-btn" data-wb-share-action="${action}" data-wb-share-scope="layer">${escapeHtml(label)}</button><small>${escapeHtml(impact)}</small>${chip}</div>`;
    };
    const diagCap = cap("share.diagnostics");
    const readBtn = diagCap?.enabled
      ? diagBusy
        ? `<button type="button" class="wb-btn wb-btn-sm" disabled aria-busy="true">${wbIcon("refresh")}读取中…</button>`
        : `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-diag>${wbIcon("refresh")}读取分层状态</button>`
      : `<small class="wb-muted">分层诊断：${escapeHtml(gapText("share.diagnostics"))}</small>`;
    const readState = failure
      ? `<p class="wb-muted">分层诊断${diag ? "本次刷新失败，以下为上一次读数" : "读取失败"}（${escapeHtml(failure)}）。</p>`
      : diag
        ? `<p class="wb-muted">${typeof diag.repair_checked_at === "number" ? `隔离修复核验于 ${escapeHtml(time(diag.repair_checked_at))}；其他层保留各自检查时间。` : `读于 ${escapeHtml(time(diag.checked_at))}（只读诊断，每次读取记一条操作记录）。`}${typeof diag.note === "string" && diag.note ? ` 后端口径：${escapeHtml(diag.note.slice(0, 200))}` : ""}</p>`
        : `<p class="wb-muted">尚未读取分层状态：点「读取分层状态」只读读取一次；未读取前各层一律未知。</p>`;
    const card = (
      title: string,
      keys: string[],
      acts: string,
      extra = "",
      showLayers = false
    ) =>
      `<div class="wb-share-layer"><h4>${escapeHtml(title)}</h4>${showLayers ? `<ul class="wb-share-layer-list">${keys.map((key) => layerRow(key)).join("")}</ul>` : ""}${extra}<div class="wb-share-layer-acts">${acts}</div></div>`;
    const watchdog = layers.watchdog;
    const wd = watchdog?.evidence as Record<string, unknown> | undefined;
    const wdText = (v: unknown) => (typeof v === "string" && v ? v : "未取得");
    const match = (v: unknown) =>
      v === true ? "符合部署配置" : v === false ? "不匹配" : "未确认";
    const wdReasons: Record<string, string> = {
      watchdog_task_missing: "计划任务缺失，需要管理员恢复部署。",
      watchdog_not_running: "任务当前未运行，需要核对部署与启动状态。",
      watchdog_task_queued: "计划任务已排队，尚未确认开始运行。",
      watchdog_execution_mismatch:
        "执行程序或脚本路径与部署配置不匹配，需要管理员核对。",
      watchdog_query_failed: "后台未能完成计划任务查询。",
      watchdog_capability_missing:
        "当前后台版本不支持看门狗诊断，需要更新后台后重新检查。",
      watchdog_broker_unavailable: "受限管理后台不可取得，无法查询计划任务。",
    };
    const wdRow = (name: string, value: string) =>
      `<div><dt>${name}</dt><dd>${escapeHtml(value)}</dd></div>`;
    const watchdogHtml =
      `<div class="wb-share-watchdog"><h4>受限运行环境与看门狗</h4>` +
      (wd
        ? `<dl class="wb-share-watchdog-evidence">` +
          wdRow("计划任务", wdText(wd.task_name)) +
          wdRow("当前任务状态", wdText(wd.task_state)) +
          wdRow(
            "执行程序",
            `${wdText(wd.execute)} · ${match(wd.execute_matches)}`
          ) +
          wdRow(
            "脚本路径",
            `${wdText(wd.script_path)} · ${match(wd.script_matches)}`
          ) +
          wdRow(
            "上次任务返回值",
            `${wdText(wd.last_task_result_hex)}${typeof wd.last_task_result === "number" ? `（${wd.last_task_result}）` : ""}`
          ) +
          wdRow("上次运行时间", time(wd.last_run_at)) +
          wdRow(
            "日志最近更新",
            `${time(wd.heartbeat_at)}${wd.heartbeat_source === "watchdog_log_mtime" ? "（保护日志时间）" : ""}`
          ) +
          `</dl><p class="wb-muted">上次任务返回值是历史记录；任务正在运行时，旧的非零返回值不能单独判为离线。日志更新时间也不能单独证明进程活跃。</p>`
        : `<p class="wb-muted">${diag ? "本次后台未提供看门狗证据；旧版本或查询受限均不能据此推断任务不存在。" : "点击“读取分层状态”查询，当前状态未确认。"}</p>`) +
      (typeof watchdog?.reason === "string" && wdReasons[watchdog.reason]
        ? `<p class="wb-muted">${escapeHtml(wdReasons[watchdog.reason]!)}</p>`
        : "") +
      `</div>`;
    const summaryKeys = [
      "share_gateway",
      "share_backend",
      "backend_auth",
      "connectivity",
      "upstream",
      "ts_service",
      "funnel",
      "security_gate",
      "watchdog",
    ];
    return (
      `<section class="wb-share-layers" data-wb-share-section="layers">` +
      `<div class="wb-section-head"><div><h3>分层状态与维护</h3><small>进程、认证、安全与单层恢复分别判断</small></div><div class="wb-share-section-actions">${readBtn}</div></div>` +
      readState +
      `<ul class="wb-share-layer-list wb-share-layer-summary" aria-label="分层状态">${summaryKeys.map((key) => layerRow(key, true)).join("")}</ul>` +
      `<details class="wb-share-fold" data-wb-share-panel="layers-maintenance"${this.sharePanelsOpen.has("layers-maintenance") ? " open" : ""}><summary title="展开 / 收起分层维护与管理员恢复">分层维护与管理员恢复${wbFoldMark()}</summary>` +
      statusLine +
      supervisionHtml +
      `<div class="wb-share-layer-grid">` +
      card(
        "分享入口 · 8100",
        [
          "share_gateway",
          "connectivity",
          "ts_service",
          "funnel",
          "security_gate",
        ],
        actionBtn(
          "share.gateway_restart",
          "仅恢复 / 重启 8100",
          "入口短暂中断；不动 8110，不关公网"
        ),
        compareHtml("share.gateway_restart"),
        true
      ) +
      card(
        "UM 转发与登录 · 8110",
        ["share_backend", "backend_auth", "upstream"],
        actionBtn(
          "share.backend_login",
          "启动 / 按需登录 8110",
          "未运行时启动；认证有效则跳过登录，需要时由你完成专用 Edge 登录"
        ) +
          actionBtn(
            "share.backend_reauthenticate",
            "仅重新认证 8110",
            "只做认证，不冷启动进程"
          ) +
          actionBtn(
            "share.backend_restart",
            "仅重启运行中的 8110",
            "先暂停入口接收并排空在途请求；未运行时请点“启动 / 按需登录 8110”"
          ),
        compareHtml("share.backend_restart"),
        true
      ) +
      `</div>${watchdogHtml}${faultHtml}${countsHtml}` +
      `</details></section>`
    );
  }
  /** S5 公网链路巡检与恢复（契约 §5.6）：每入口状态、当前原因、恢复进度、
   *  剩余次数、下次检查/动作时间、一键复查、受限重试、故障时间线与脱敏导出。
   *  所有值来自 `GET .../share-management/public`；缺失写「未提供」/「未知」，
   *  绝不把缺失折成正常。按钮禁用只做展示，服务端的 share:public 资源、
   *  滚动预算与 share:orchestration 动作锁才是互斥权威。 */
  private sharePublicHtml(): string {
    const read = this.publicShare;
    const live = read?.state === "live" ? read.payload : null;
    const snapshot = live?.snapshot ?? null;
    const cancel = live?.cancel ?? null;
    const overall = publicSupervisionReading(
      snapshot,
      Date.now() / 1000,
      cancel?.cancelled === true
    );
    const recovery = publicRecoveryReading(
      snapshot,
      cancel?.cancelled === true
    );
    const budget = live?.budget ?? snapshot?.budget ?? null;
    const budgetText = publicBudgetText(budget);
    const cap = (action: string) =>
      this.actions?.find((row) => row.action === action);
    const retryCap = cap("share.public_retry");
    const gapText = (row: ActionRow | undefined) =>
      !this.actions
        ? "动作注册表未读取（读取中或读取失败）"
        : !row
          ? "当前后端版本不支持（待后端接入）"
          : `已停用：${row.disabled_reason ?? "原因未提供"}`;
    const httpReadable = read?.state === "live";
    const moduleAvailable = httpReadable && live?.available === true;
    const inFlight = (entry: FeedbackEntry) =>
      entry.state === "preparing" ||
      entry.state === "queued" ||
      entry.state === "running";
    const feedbackBusy = [...this.feedbackEntries.values()].some(
      (entry) => inFlight(entry) && entry.action.startsWith("share.")
    );
    const opBusy =
      this.opsRecent?.state === "live" &&
      this.opsRecent.rows.some(
        (op) =>
          op.action.startsWith("share.") &&
          (op.state === "pending" ||
            op.state === "queued" ||
            op.state === "running")
      );
    const busy = feedbackBusy || opBusy;
    const readFail = !read
      ? "公网巡检状态读取中…"
      : read.state === "unavailable"
        ? `公网巡检读口未载入（${read.reason}）；不显示正常，也不自动恢复`
        : read.state === "refused"
          ? "公网巡检读口被拒绝（403 权限不符）"
          : read.state === "error"
            ? `公网巡检读取失败（${read.reason}）；未知不是正常`
            : live?.available === false
              ? "当前服务端未部署公网巡检模块（available:false）；一键复查与受限重试不可用，状态一律未提供"
              : live?.unreadable === true
                ? "公网巡检状态文件不可读（state_unreadable）；不冒充正常，可点一键复查重新生成快照"
                : !snapshot
                  ? "尚无公网巡检快照（未提供）；一键复查会写入首份快照"
                  : "";
    const round = snapshot?.round ?? null;
    const local = snapshot?.local ?? null;
    const cancelText =
      cancel?.cancelled === true
        ? `取消信号生效中（${cancel.reason ?? "原因未提供"}）：自动恢复暂停，分享开启成功后才会清除`
        : cancel?.cancelled === false
          ? "无人工取消信号"
          : "取消状态未提供";
    const intent = snapshot?.intent ?? null;
    const intentText = intent
      ? `运行意图 ${intent.state === "running" ? "running" : intent.state === "stopped" ? "stopped" : "未知"}${intent.at ? ` · ${intent.at}` : ""}`
      : "运行意图未提供";
    // A cancelled round or a stopped intent owns the schedule: printing a due
    // time here would read as "it will check again", which is exactly what the
    // pause/stop means it will not do.
    const supervisionPaused = cancel?.cancelled === true;
    const supervisionStopped = snapshot?.intent?.state === "stopped";
    const nextCheck = supervisionPaused
      ? "巡检已暂停（人工取消）"
      : supervisionStopped
        ? "分享已停止"
        : this.sharePublicNextTimeText(round?.nextCheckAt ?? null);
    const nextAction = supervisionPaused
      ? "不自动检查或恢复"
      : supervisionStopped
        ? "不自动动作"
        : publicNextActionText(snapshot);
    const observedText =
      typeof snapshot?.observedAt === "number" &&
      Number.isFinite(snapshot.observedAt)
        ? new Date(snapshot.observedAt * 1000).toLocaleString("zh-CN", {
            hour12: false,
          })
        : "未提供";
    const checkWord = (value: string) =>
      value === "ok"
        ? ["正常", "ok"]
        : value === "failed"
          ? ["失败", "danger"]
          : ["未检查", "idle"];
    const entryStateWord = (value: string) =>
      value === "ok"
        ? ["成功", "ok"]
        : value === "failed"
          ? ["失败", "danger"]
          : ["未检查", "idle"];
    const entries = snapshot?.entries ?? [];
    const dns = snapshot?.dns ?? null;
    const dnsSourceWord = (state: string) =>
      state === "ok"
        ? ["正常", "ok"]
        : state === "nxdomain"
          ? ["NXDOMAIN", "warn"]
          : state === "empty"
            ? ["无 A 记录", "idle"]
            : state === "timeout"
              ? ["超时", "warn"]
              : state === "error"
                ? ["失败", "danger"]
                : ["未检查", "idle"];
    const dnsRows = dns
      ? (dns.sources ?? []).length
        ? (dns.sources ?? [])
            .map((source) => {
              const [stateText, stateClass] = dnsSourceWord(source.state);
              const sourceName =
                source.source === "cloudflare"
                  ? "Cloudflare DoH"
                  : source.source === "google"
                    ? "Google DoH"
                    : (source.source ?? "来源未提供");
              const ttl =
                source.negativeTtlSeconds !== null
                  ? `负缓存约 ${source.negativeTtlSeconds} s`
                  : source.ttlSeconds !== null
                    ? `TTL ${source.ttlSeconds} s`
                    : "TTL 未提供";
              const checked =
                source.checkedAt === null
                  ? "未提供"
                  : new Date(source.checkedAt * 1000).toLocaleString("zh-CN", {
                      hour12: false,
                    });
              return (
                `<li data-wb-share-public-dns-source="${escapeHtml(source.source ?? "unknown")}"><span>${escapeHtml(sourceName)}</span>` +
                `<span class="wb-badge wb-badge-${stateClass}">${escapeHtml(stateText)}</span>` +
                `<small>状态码 ${source.status === null ? "未提供" : source.status} · ${escapeHtml(source.classification ?? "分类未提供")}` +
                ` · 地址 ${escapeHtml(source.addresses.length ? source.addresses.join(", ") : "无有效地址")}` +
                ` · ${escapeHtml(ttl)} · 检查 ${escapeHtml(checked)}` +
                ` · 耗时 ${source.elapsedMs === null ? "未提供" : `${source.elapsedMs} ms`}</small></li>`
              );
            })
            .join("")
        : `<li><span>解析源</span><span class="wb-badge wb-badge-idle">未提供</span><small>本快照没有逐源读数；未提供 ≠ 正常。</small></li>`
      : "";
    const dnsAddresses =
      dns && dns.addresses.length ? dns.addresses.join(", ") : "无有效地址";
    const entryRows = entries.length
      ? entries
          .map((entry) => {
            const [stateText, stateClass] = entryStateWord(entry.state);
            const [dnsText, dnsClass] = checkWord(entry.dns);
            const [tlsText, tlsClass] = checkWord(entry.tls);
            const [certText, certClass] = checkWord(entry.certificate);
            return (
              `<li data-wb-share-public-entry><span>${escapeHtml(entry.host ?? "host 未提供")}</span>` +
              `<span class="wb-badge wb-badge-${stateClass}">${stateText}</span>` +
              `<small>IP ${escapeHtml(entry.ip ?? "未提供")} · DNS <span class="wb-badge wb-badge-${dnsClass}">${dnsText}</span>` +
              ` · TLS <span class="wb-badge wb-badge-${tlsClass}">${tlsText}</span>` +
              ` · 证书 <span class="wb-badge wb-badge-${certClass}">${certText}</span>` +
              ` · 原因 ${escapeHtml(entry.reason ?? "未提供")}` +
              ` · 耗时 ${entry.elapsedMs === null ? "未提供" : `${entry.elapsedMs} ms`}</small></li>`
            );
          })
          .join("")
      : `<li><span>入口结果</span><span class="wb-badge wb-badge-idle">未提供</span><small>本轮没有入口结果（未提供 ≠ 正常）；未检查的入口不会被写成失败。</small></li>`;
    const events = live?.events ?? null;
    const eventTime = (value: string | null) => {
      if (!value) return "时间未提供";
      const epoch = Date.parse(value);
      return Number.isFinite(epoch)
        ? new Date(epoch).toLocaleString("zh-CN", { hour12: false })
        : "时间未提供";
    };
    const eventRows =
      events === null
        ? `<p class="wb-muted">事件未提供（不是「无事件」）；读口未返回事件列表。</p>`
        : events.length === 0
          ? `<p class="wb-muted">暂无事件（后端返回空时间线）。</p>`
          : `<ul class="wb-share-layer-list">${events
              .slice(0, 10)
              .map(
                (event) =>
                  `<li><span>${escapeHtml(eventTime(event.at))}</span>` +
                  `<span class="wb-badge wb-badge-idle">${escapeHtml(event.status ?? "状态未提供")}</span>` +
                  `<small>原因码 ${escapeHtml(event.code ?? "未提供")} · 动作 ${escapeHtml(event.action ?? "未提供")}</small></li>`
              )
              .join("")}</ul>`;
    const eventsNote =
      live?.eventsUnreadable === true
        ? `<p class="wb-muted">事件不可读（不是「无事件」）；时间线只按后端事实显示。</p>`
        : live?.eventsTruncated === true
          ? `<p class="wb-muted">事件较多，仅显示最近 ${events?.length ?? 10} 条。</p>`
          : "";
    const checkReason = this.sharePublicCheckReason();
    const retryBudgetReason = publicBudgetRetryReason(budget);
    const retryReason = !this.actions
      ? gapText(undefined)
      : !retryCap
        ? gapText(undefined)
        : !retryCap.enabled
          ? gapText(retryCap)
          : busy
            ? "任一分享动作在途，结束后再操作"
            : !httpReadable
              ? "公网巡检读面不可用"
              : !moduleAvailable
                ? "公网巡检模块未部署"
                : !snapshot
                  ? "没有全部入口失败的快照（未提供）"
                  : snapshot.overall !== "all_failed"
                    ? "仅当全部入口失败时可受限重试"
                    : retryBudgetReason
                      ? retryBudgetReason
                      : cancel?.cancelled === true
                        ? "取消信号生效中，不自动恢复"
                        : intent?.state !== "running"
                          ? "运行意图不是 running（未提供按未运行处理）"
                          : (() => {
                              const maintenance =
                                typeof local?.maintenance === "string"
                                  ? local.maintenance
                                  : "unavailable";
                              return maintenance === "active" ||
                                maintenance === "unavailable"
                                ? `维护租约为 ${maintenance}，不自动恢复`
                                : "";
                            })() ||
                            (this.shareBrokerUnavailable()
                              ? "受限后台未响应，先恢复后台"
                              : "");
    const exportReason = !httpReadable
      ? "公网巡检读面不可用"
      : !moduleAvailable
        ? "公网巡检模块未部署"
        : "";
    const button = (
      kind: "check" | "retry" | "export",
      label: string,
      reason: string
    ) =>
      `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-public="${kind}"` +
      ` title="${escapeHtml(reason || (kind === "check" ? "只读 DNS/TLS 复查，不发送推理请求" : kind === "retry" ? "复用 30 分钟预算与编排互斥" : "下载脱敏诊断 JSON"))}"` +
      `${reason ? " disabled" : ""}>${escapeHtml(label)}</button>`;
    const reasons = [
      ...new Set([checkReason, retryReason, exportReason].filter(Boolean)),
    ];
    return (
      `<section class="wb-share-public" data-wb-share-public-card>` +
      `<div class="wb-section-head"><div><h3>公网巡检与恢复</h3><small>DNS / TLS · 不自动调用模型</small></div><span class="wb-badge wb-badge-${overall.state}" data-wb-share-public-reading="overall" data-wb-share-public-overall role="status">${escapeHtml(overall.text)}</span></div>` +
      (readFail
        ? `<p class="wb-muted">${escapeHtml(readFail)}${read?.state === "error" || read?.state === "unavailable" ? ` <button type="button" class="wb-btn wb-btn-sm" data-wb-share-refresh>重新读取</button>` : ""}</p>`
        : "") +
      `<div class="wb-share-public-actions">${button("check", "一键复查", checkReason)}${button("retry", "受限重试", retryReason)}${button("export", "导出诊断", exportReason)}</div>` +
      `<p class="wb-muted">只做 DNS/TLS 检查，不发送推理请求。模型回复以密钥的手动测试为准。</p>` +
      `<dl class="wb-share-public-grid" aria-label="公网巡检与恢复状态">` +
      `<div data-wb-share-public-reading="recovery"><dt>恢复状态</dt><dd><span class="wb-badge wb-badge-${recovery.state}">${escapeHtml(recovery.text)}</span><small>${escapeHtml(snapshot?.recovery?.reason ?? "恢复原因未提供")}</small></dd></div>` +
      `<div data-wb-share-public-reading="budget"><dt>恢复预算</dt><dd>${escapeHtml(budgetText)}</dd><small>滚动 30 分钟，成功与人工重试不清零</small></div>` +
      `<div data-wb-share-public-reading="next-check"><dt>下次检查</dt><dd>${escapeHtml(nextCheck)}</dd><small>${escapeHtml(intentText)}</small></dd></div>` +
      `<div data-wb-share-public-reading="observed"><dt>最近观测</dt><dd>${escapeHtml(observedText)}</dd><small>超过 2 分钟视为陈旧</small></div>` +
      `<div data-wb-share-public-reading="next-action"><dt>下次动作</dt><dd>${escapeHtml(nextAction)}</dd><small>${escapeHtml(cancelText)}</small></dd></div>` +
      `</dl>` +
      `<p class="wb-muted">当前原因：${escapeHtml(snapshot?.reason ?? "未提供")}。本机进程、认证和安全状态见「分层状态与维护」。</p>` +
      `<h4>公网入口</h4><ul class="wb-share-layer-list">${entryRows}</ul>` +
      `<h4>DNS 解析（双源）</h4>` +
      (dns
        ? `<p class="wb-muted" data-wb-share-public-dns="overall">${escapeHtml(publicDnsText(dns))} · ${escapeHtml(publicDnsTtlText(dns, Date.now() / 1000))} · 有效地址 ${escapeHtml(dnsAddresses)}${dns.addressesTruncated ? "（仅显示前 4 个，覆盖不完整）" : ""} · 下次检查 ${escapeHtml(nextCheck)}</p>` +
          `<ul class="wb-share-layer-list" data-wb-share-public-dns-sources>${dnsRows}</ul>` +
          `<p class="wb-muted">解析源读数只用于诊断，不是公网入口 IP；不同有效地址集合不判故障；TTL 只表示本次响应有效期，不保证到点恢复，也不延长默认 60 秒巡检。</p>`
        : `<p class="wb-muted" data-wb-share-public-dns="missing">DNS 解析详情未提供（不是「正常」）；旧快照缺少该字段时保持未提供。</p>`) +
      `<details class="wb-share-fold" data-wb-share-panel="public-events"${this.sharePanelsOpen.has("public-events") ? " open" : ""}><summary title="展开 / 收起故障时间线">故障时间线${wbFoldMark()}</summary>${eventsNote}${eventRows}</details>` +
      (reasons.length
        ? `<p class="wb-muted">暂不可用：${escapeHtml(reasons.join("；"))}。</p>`
        : "") +
      (this.publicShareExport
        ? `<p class="wb-muted" data-wb-share-public-export-result>${escapeHtml(this.publicShareExport.text)}</p>`
        : "") +
      `</section>`
    );
  }
  private sharePublicNextTimeText(value: string | null): string {
    if (!value) return "未提供";
    const epoch = Date.parse(value);
    if (!Number.isFinite(epoch)) return "未提供";
    if (epoch / 1000 <= Date.now() / 1000) return "已到时间，等待下一轮";
    return new Date(epoch).toLocaleString("zh-CN", { hour12: false });
  }
  /** 分享用量读口（`GET /api/console/share-usage`，只读、无参数、无请求体）。
   *
   *  路由与控制台其余只读口同源免凭据；**不是**需管理令牌的
   *  `GET /api/share/usage`（管理令牌绝不进网页）。状态一律按**实测 HTTP
   *  状态**分辨，绝不折成「每 Key 0 请求」：
   *  · 404 = 当前服务端版本未载入该路由（读口未开放），保留可重试；
   *  · 401/403 = 请求打到了分享管理面或别的路由（本口免凭据，这不是本读口
   *    的预期状态）；
   *  · 503/502 = 读口存在但读取失败（share_metering 未载入 / 账本路径解析
   *    不出 / 账本读取失败），原样写出错误码。
   *  读数永远来自分享后端账本，与「累计参考费用」是两个账本，不合并、不折算。 */
  /** The share-usage read as the summary-first usage tab: `summary` (state,
   *  counters and the per-key table), `chart` (the on-demand 14-day CNY trend)
   *  and `notes` (the ledger scope fold). Until the read is answered both the
   *  summary and the chart say so; only the summary carries the retry. */
  private shareUsageParts(): { summary: string; chart: string; notes: string } {
    const read = this.shareUsage;
    const retry = `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-refresh title="重新读取分享状态、密钥列表与用量（只读）" aria-label="重试读取分享用量">${wbIcon("refresh")}${wbIconHint("重试读取分享用量")}</button>`;
    const body =
      !read || read.state === "loading"
        ? `<p class="wb-muted" data-state="loading">分享用量读取中…（GET /gw/usagesrv/api/console/share-usage · 只读、免凭据）</p>`
        : read.state === "gated"
          ? `<p class="wb-muted">分享用量读口不可取得（无凭据会话）；不以文档冒充用量。${retry}</p>`
          : read.state === "refused"
            ? `<p class="wb-muted">分享用量读口被拒绝（403 / 401）：本口免凭据直读，被拒绝说明请求打到了分享管理面或别的路由，不是本读口的预期状态。不以 0 用量冒充。${retry}</p>`
            : read.state === "unavailable"
              ? `<p class="wb-muted">当前服务端版本未载入：GET /gw/usagesrv/api/console/share-usage 实测 HTTP ${read.status}（${escapeHtml(read.reason)}）。读口未开放，因此这里不显示任何每 Key 数字——不以 0 冒充「没有用量」，也不把历史未归属用量摊到某个 Key。${retry}</p>`
              : read.state === "error"
                ? `<p class="wb-muted">分享用量读取失败（${escapeHtml(read.reason)}）；不以 0 或空表冒充零用量，也不沿用上一次读数当成本次结果。${retry}</p>`
                : null;
    const parts =
      body === null && read?.state === "live"
        ? this.shareUsageBodyParts(read.payload)
        : null;
    const chart = parts
      ? parts.chart
      : `<div class="wb-share-key-chart" data-wb-series="cny"><p class="wb-muted">${!read || read.state === "loading" ? "日费用图随分享用量读取显示（读取中）。" : "分享用量未读到，图中标记未知；原因见上方用量读面。"}</p>` +
        this.emptyTrendHtml("CNY", this.beijingDayKeys(14)) +
        `</div>`;
    return {
      summary: parts ? parts.summary : (body ?? ""),
      chart,
      notes: this.laneNoteFoldHtml(
        "share-usage",
        "账本口径",
        `<p class="wb-muted">读数来自分享专用后端账本（与「累计参考费用」是两个账本，不合并、不折算）。人民币按记录当时生效的价表版本与汇率折算，原始 USD 在明细里保留可追溯；已知与估算分开，未归属独立列出，缺时间或汇率的金额保留未知，不按当前汇率补算历史。</p>`
      ),
    };
  }
  /** 分享用量的读数主体：汇总表优先（每 Key 一行，未归属桶是同一张表里
   *  的一行，key_id 为 null），绝不摊到某个 Key。费用口径统一用人民币：
   *  `costCnyPartial === null` = 人民币读数未提供；true 且金额非空 = 下界
   *  「≥ ¥…」；true 且金额为空 = 未知；false = 确定金额。原始 USD 在
   *  主表次要位置和明细里保留可追溯。页面只显示掩码后的 Key：能 join 到
   *  管理库行时用行自己的 `masked`，join 不到（已删除 / 已轮换的 Key 仍在
   *  历史里）保留账本 id 的尾部并写明原因，绝不隐藏、也绝不打印完整 Key。
   *  明细（模型 / 日 / 无日期）放在可展开行里。 */
  private shareUsageBodyParts(payload: ShareUsagePayload): {
    summary: string;
    chart: string;
  } {
    const coverage = payload.coverage;
    const unpriced = new Set(coverage?.unpricedModels ?? []);
    const head =
      `<p class="wb-muted">本次读到 ${typeof payload.requests === "number" ? fmtRequests(payload.requests) : "未知"} 条记录` +
      `（账本 ${escapeHtml(coverage?.ledger ?? "文件名未提供")} · 上限 ${typeof coverage?.limit === "number" ? fmtRequests(coverage.limit) : "未提供"}` +
      ` · 检查时间 ${escapeHtml(this.shareUsageTime(payload.checkedAt))}）` +
      `${coverage?.ledgerPresent === false ? " · 账本文件缺席（没有可读数据，不等于零用量）" : ""}` +
      `${coverage?.ledgerReadable === false ? " · 账本不可读" : ""}` +
      `${coverage?.truncated === true ? " · 结果被截断（truncated）" : ""}` +
      `${typeof coverage?.unparsedLines === "number" && coverage.unparsedLines > 0 ? ` · 不可解析行 ${coverage.unparsedLines}` : ""}</p>`;
    const counterText = (value: number | undefined, what: string) =>
      typeof value === "number"
        ? `${what} ${fmtRequests(value)}`
        : `${what} 未提供`;
    const counters =
      `<p class="wb-muted">` +
      `${counterText(coverage?.attributed, "已归因")} · ${counterText(coverage?.unattributed, "未归属")} · ` +
      `${counterText(coverage?.unknownUsageRequests, "用量未知")} · ${counterText(coverage?.unpricedRequests, "无价")} · ` +
      `${counterText(coverage?.unknownTimeRequests, "时间无法判定")}` +
      `；未归属 = 账本里没有可归因 key_id 的记录，单列在下方，绝不摊到某个 Key。` +
      `${unpriced.size ? `未定价模型：${escapeHtml([...unpriced].join("、"))}（费用未知，不折成 ¥0）` : ""}</p>`;
    // F12-8: the backend's own normaliser (src/console/backend) reads the per-key
    // rows - name, deleted flag, records, Token and the known / estimated split per
    // day and model - AND the unattributed bucket, which is the same shape without
    // a key identity. A payload it rejects is a shape error, never an empty table.
    let keys: KeyUsage[];
    let unattributedBucket: ShareUsageBucket;
    try {
      const normalised = normalizeShareUsage(payload);
      keys = normalised.keys;
      unattributedBucket = normalised.unattributed;
    } catch (error) {
      return {
        summary:
          head +
          `<p class="wb-muted">分享用量响应形状与预期不符（${escapeHtml(error instanceof Error ? error.message : "未知原因")}）；不显示每 Key 数字，也不按 0 显示。</p>`,
        chart:
          `<div class="wb-share-key-chart" data-wb-series="cny"><p class="wb-muted">分享用量响应形状与预期不符，图中标记未知（原因见上方用量读面）。</p>` +
          this.emptyTrendHtml("CNY", this.beijingDayKeys(14)) +
          `</div>`,
      };
    }
    const requestMax = Math.max(
      0,
      ...keys.map((key) => key.coverage.requests ?? 0),
      unattributedBucket.coverage.requests ?? 0
    );
    const rows = keys
      .map((key) =>
        this.shareUsageRowHtml(this.shareKeyView(key), unpriced, requestMax)
      )
      .join("");
    // 未归属桶与每 Key 行同形、同一套归一化（含 Token 细分），但仍单列在
    // 同一张表的最后一行，绝不摊到任何 Key。
    const unattributedRow = this.shareUsageRowHtml(
      this.shareUnattributedView(unattributedBucket),
      unpriced,
      requestMax
    );
    const chart =
      this.shareKeyChartHtml(keys) ||
      `<p class="wb-muted">账本里还没有可归因到 Key 的记录，没有可比较的日费用（未归属记录见上方汇总表）。</p>`;
    return {
      summary:
        head +
        counters +
        `<div class="wb-table-scroll wb-share-usage-scroll"><table class="wb-table wb-share-usage-table"><thead><tr><th>Key（名称 · 短 ID）</th><th>请求</th><th>Token（输入 / 输出）</th><th>人民币（已知 / 估算）</th></tr></thead><tbody>${rows}${unattributedRow}</tbody></table></div>`,
      chart,
    };
  }
  /** F12-8 per-key view of one key: its managed name (the usage ledger's name,
   *  else the management row's), a short id tail, the deleted flag, and the
   *  masked key only when the management row still has it. A rotated key keeps
   *  its key_id, so its history is already merged here; a deleted one keeps its
   *  name; a key with no name anywhere says 名称未知. Never the plaintext. */
  private shareKeyName(key: KeyUsage): string {
    const row = this.shareKeyRow(key.keyId);
    const name = key.keyName ?? row?.name ?? null;
    const tail = `…${key.keyId.slice(-6)}`;
    return (
      `<strong>${escapeHtml(name ?? "名称未知")}</strong> <code class="wb-key-id">${escapeHtml(tail)}</code>` +
      (key.deleted
        ? ` <span class="wb-badge wb-badge-idle">已删除</span>`
        : "") +
      (row?.masked
        ? `<small class="wb-muted wb-key-masked">${escapeHtml(row.masked)}</small>`
        : "")
    );
  }
  private shareKeyView(key: KeyUsage): ShareUsageRowView {
    return {
      identity: key.keyId,
      name: this.shareKeyName(key),
      records: key.coverage.records,
      requests: key.coverage.requests,
      inputTokens: key.inputTokens,
      outputTokens: key.outputTokens,
      tokens: key.tokenUsage,
      known: key.knownCostUsd,
      estimated: key.estimatedCostUsd,
      cny: key.costCny,
      knownCny: key.knownCostCny,
      estimatedCny: key.estimatedCostCny,
      cnyPartial: key.coverage.costCnyPartial,
      knownCnyPartial: key.coverage.knownCostCnyPartial,
      estimatedCnyPartial: key.coverage.estimatedCostCnyPartial,
      unknown: key.coverage.unknownRequests,
      unpriced: key.coverage.unpricedRequests,
      models: key.models,
      days: key.days,
      undated: key.undated
        ? { identity: "时间无法判定", ...key.undated }
        : null,
    };
  }
  /** The unattributed bucket has the same shape without a key id; it stays its
   *  own row and is never spread over the keys. Its Token split (完整 / 下界 /
   *  估算 / 未知) comes from the SAME backend normaliser as the key rows — the
   *  backend delivered it, so the page no longer parses it itself; a payload
   *  without a split still reads as 未拆分口径, never as a complete total. */
  private shareUnattributedView(bucket: ShareUsageBucket): ShareUsageRowView {
    return {
      identity: "unattributed",
      name: `<strong>未归属</strong><small class="wb-muted">（账本未记录 key_id 的历史记录，不摊到任何 Key）</small>`,
      records: bucket.coverage.records,
      requests: bucket.coverage.requests,
      inputTokens: bucket.inputTokens,
      outputTokens: bucket.outputTokens,
      tokens: bucket.tokenUsage,
      known: bucket.knownCostUsd,
      estimated: bucket.estimatedCostUsd,
      cny: bucket.costCny,
      knownCny: bucket.knownCostCny,
      estimatedCny: bucket.estimatedCostCny,
      cnyPartial: bucket.coverage.costCnyPartial,
      knownCnyPartial: bucket.coverage.knownCostCnyPartial,
      estimatedCnyPartial: bucket.coverage.estimatedCostCnyPartial,
      unknown: bucket.coverage.unknownRequests,
      unpriced: bucket.coverage.unpricedRequests,
      models: bucket.models,
      days: bucket.days,
      undated: bucket.undated
        ? { identity: "时间无法判定", ...bucket.undated }
        : null,
    };
  }
  /** F12-8 comparison chart: each compared key's daily fee (known + estimated,
   *  CNY) over the last 14 days, plus all attributed keys together. A day whose
   *  CNY reading is a lower bound or whose usage is unknown/unpriced is a lower
   *  bound (≥); a day the response did not mark for CNY reads 人民币读数未提供.
   *  Colours follow the key (stable slots), the legend names them, and the
   *  chooser below picks up to TOKEN_SERIES_MAX keys. */
  private shareKeyChartHtml(keys: KeyUsage[]): string {
    if (!keys.length) {
      return `<div class="wb-share-key-chart" data-wb-series="cny">${this.emptyTrendHtml("CNY", this.beijingDayKeys(14))}</div>`;
    }
    const labels = this.beijingDayKeys(14);
    const dayCost = (d: CostDetail | undefined): number | null => {
      if (!d || d.coverage.costCnyPartial === null) return null;
      return d.costCny;
    };
    const lower = (d: CostDetail | undefined) =>
      !!d &&
      (d.coverage.costCnyPartial === true ||
        d.coverage.costCnyPartial === null ||
        (d.coverage.unknownRequests ?? 0) > 0 ||
        (d.coverage.unpricedRequests ?? 0) > 0);
    const totalOf = (k: KeyUsage) => k.costCny ?? 0;
    if (!this.shareKeyCompareTouched) {
      this.shareKeyCompare = new Set(
        [...keys]
          .sort((a, b) => totalOf(b) - totalOf(a))
          .slice(0, 2)
          .map((k) => k.keyId)
      );
    }
    const shown = keys
      .filter((k) => this.shareKeyCompare.has(k.keyId))
      .slice(0, TOKEN_SERIES_MAX);
    const slots = this.seriesSlots(
      this.shareKeySlot,
      shown.map((k) => k.keyId)
    );
    const byDay = (k: KeyUsage) =>
      new Map(k.days.map((d) => [d.identity, d] as const));
    const maps = new Map(keys.map((k) => [k.keyId, byDay(k)] as const));
    const totalValues = labels.map((day) => {
      let sum: number | null = null;
      for (const k of keys) {
        const v = dayCost(maps.get(k.keyId)!.get(day));
        if (v !== null) sum = (sum ?? 0) + v;
      }
      return sum;
    });
    const totalPartial = labels.map((day) =>
      keys.some((k) => lower(maps.get(k.keyId)!.get(day)))
    );
    const dayRows = (day: string) =>
      keys
        .map((k) => maps.get(k.keyId)!.get(day))
        .filter((d): d is CostDetail => !!d);
    const missing = labels.map((day) => {
      const rows = dayRows(day);
      if (!rows.length) return "无记录";
      return rows.some((d) => d.coverage.costCnyPartial === null)
        ? "人民币读数未提供"
        : "未知";
    });
    const nameOf = (k: KeyUsage) =>
      `${k.keyName ?? this.shareKeyRow(k.keyId)?.name ?? "名称未知"}（…${k.keyId.slice(-6)}）${k.deleted ? "·已删除" : ""}`;
    const series = [
      {
        key: "__total__",
        label: "全部已归因 Key",
        values: totalValues,
        kind: "total" as const,
      },
      ...shown.map((k) => ({
        key: k.keyId,
        label: nameOf(k),
        values: labels.map((day) => dayCost(maps.get(k.keyId)!.get(day))),
        kind: "model" as const,
        slot: slots.get(k.keyId),
        // A key's day with a lower-bound CNY reading or unknown/unpriced usage is a lower bound.
        partial: labels.map((day) => lower(maps.get(k.keyId)!.get(day))),
        missing: labels.map((day) => {
          const d = maps.get(k.keyId)!.get(day);
          if (!d) return "无记录";
          return d.coverage.costCnyPartial === null
            ? "人民币读数未提供"
            : "未知";
        }),
      })),
    ];
    const chart = this.tokenSeriesSvg(labels, series, {
      aria: "近 14 天每 Key 日费用（人民币，已知 + 估算），逐日悬停或按 Tab 查看数值",
      width: 620,
      format: (v: number) => `¥${v.toFixed(4)}`,
      tick: (v: number) => `¥${v.toFixed(2)}`,
      unit: "人民币（已知 + 估算，分享专用账本）",
      scope: "日",
      partialValues: totalPartial,
      tipTime: (label: string) => `${label}（北京时间）`,
      missingLabel: (index: number) => missing[index] ?? "无记录",
    });
    const full = shown.length >= TOKEN_SERIES_MAX;
    const chooser = `<div class="wb-key-chooser" role="group" aria-label="选择要比较的 Key（最多 ${TOKEN_SERIES_MAX} 个）">${keys
      .map((k) => {
        const on = this.shareKeyCompare.has(k.keyId);
        const swatch =
          on && slots.has(k.keyId)
            ? `<i class="wb-series-swatch" style="--wb-swatch:var(--wb-cat-${slots.get(k.keyId)})" aria-hidden="true"></i>`
            : "";
        return `<button type="button" class="wb-gw-chip" data-wb-share-compare="${escapeHtml(k.keyId)}" aria-pressed="${on}"${!on && full ? " disabled" : ""}>${swatch}${escapeHtml(nameOf(k))}</button>`;
      })
      .join("")}</div>`;
    return `<div class="wb-share-key-chart" data-wb-series="cny">${chooser}${chart}</div>`;
  }
  /** One summary-table row of per-key (or unattributed) usage, with its day /
   *  model drill. Every money reading is CNY: `cnyPartial === null` reads
   *  「人民币读数未提供」; true with an amount reads 「≥ ¥…」; true without
   *  an amount reads 「未知」; false reads 「¥…」. The original USD stays in a
   *  secondary line so it remains traceable without being printed as CNY. A
   *  request count may carry a light comparison bar. */
  private shareUsageRowHtml(
    view: ShareUsageRowView,
    unpriced: Set<string>,
    requestMax = 0
  ): string {
    const usd = (value: number | null | undefined, model?: string): string => {
      if (value === null || value === undefined || !Number.isFinite(value))
        return "未知";
      if (value === 0 && model !== undefined && unpriced.has(model))
        return "无价（未知）";
      return `$${value.toFixed(4)}`;
    };
    const cny = (
      value: number | null | undefined,
      partial: boolean | null
    ): string => {
      if (partial === null) return "人民币读数未提供";
      if (value === null || value === undefined || !Number.isFinite(value))
        return "未知";
      const amount = `¥${value.toLocaleString("zh-CN", { minimumFractionDigits: 4, maximumFractionDigits: 4 })}`;
      return partial ? `≥ ${amount}` : amount;
    };
    const requestCell = (value: number | null) => {
      if (value === null) return `<span class="wb-share-request">未知</span>`;
      if (value <= 0)
        return `<span class="wb-share-request">${fmtRequests(value)}</span>`;
      const width =
        requestMax > 0
          ? Math.max(2, Math.round((value / requestMax) * 100))
          : 0;
      return `<span class="wb-share-request"><span class="wb-share-mini-track" aria-hidden="true"><span style="width:${width}%"></span></span>${fmtRequests(value)}</span>`;
    };
    const tok = (v: number | null) => (v === null ? "未知" : fmtTokens(v));
    // The complete part is the reading; lower-bound and estimated parts are named
    // beside it. Without the split (older backend) the legacy sums are shown as
    // what they are - unsplit - never as complete totals.
    const tokens = (
      t: TokenUsage | null,
      legacyIn: number | null,
      legacyOut: number | null,
      inline: boolean
    ) => {
      const legacy = () =>
        `${tok(legacyIn)} / ${tok(legacyOut)}${inline ? "（未拆分口径）" : `<small class="wb-muted">未拆分口径（含下界与估算）</small>`}`;
      // A split whose complete side is empty is not a real 0/0: when the same
      // node also reports unknown-usage requests, the honest reading is 未知.
      if (
        t &&
        t.inputTokens === null &&
        t.outputTokens === null &&
        t.totalTokens === null
      ) {
        return (t.unknownRequests ?? 0) > 0 ? "未知" : legacy();
      }
      if (!t) return legacy();
      const extra = [
        (t.lowerBoundTokens ?? 0) > 0
          ? `下界 ${tok(t.lowerBoundInputTokens)} / ${tok(t.lowerBoundOutputTokens)}`
          : "",
        (t.estimatedTokens ?? 0) > 0
          ? `估算 ${tok(t.estimatedInputTokens)} / ${tok(t.estimatedOutputTokens)}`
          : "",
      ]
        .filter(Boolean)
        .join(" · ");
      const head = `${tok(t.inputTokens)} / ${tok(t.outputTokens)}`;
      return extra
        ? inline
          ? `${head}（另有 ${extra}）`
          : `${head}<small class="wb-muted">另有 ${extra}</small>`
        : head;
    };
    const detailGroup = (
      title: string,
      items: CostDetail[],
      kind: "model" | "day" | "undated"
    ) =>
      `<span class="wb-key-breakdown"><strong>${title}</strong><span class="wb-key-breakdown-head"><span>${kind === "model" ? "模型" : kind === "day" ? "日期（北京时间）" : "记录时间"}</span><span>Token（输入 / 输出）</span><span>已知人民币</span><span>估算人民币</span><span>原始 USD</span></span>${items.length ? items.map((d) => `<span class="wb-key-breakdown-row"><span>${escapeHtml(d.identity)}<small>${d.coverage.records === null ? "记录数未知" : `${fmtRequests(d.coverage.records)} 条记录`}${(d.coverage.unknownRequests ?? 0) > 0 ? ` · 用量未知 ${d.coverage.unknownRequests}` : ""}${(d.coverage.unpricedRequests ?? 0) > 0 ? ` · 无价 ${d.coverage.unpricedRequests}` : ""}</small></span><span>${tokens(d.tokenUsage, d.inputTokens, d.outputTokens, false)}</span><span>${cny(d.knownCostCny, d.coverage.knownCostCnyPartial)}</span><span>${cny(d.estimatedCostCny, d.coverage.estimatedCostCnyPartial)}</span><span class="wb-muted">${usd(d.knownCostUsd, kind === "model" ? d.identity : undefined)} / ${usd(d.estimatedCostUsd)}</span></span>`).join("") : `<span class="wb-muted">无明细记录</span>`}</span>`;
    const modelRows = detailGroup("按模型", view.models, "model");
    const dayRows = detailGroup("按日期", view.days, "day");
    const undatedRows = view.undated
      ? detailGroup("无日期", [view.undated], "undated")
      : "";
    const undatedCount = view.undated ? " / 无日期桶" : "";
    const open = this.shareKeyDetailsOpen.has(view.identity);
    const summary =
      `<tr class="wb-key-summary"><td class="wb-share-usage-key" data-label="Key">${view.name}<small class="wb-muted">${view.records === null ? "记录数未知" : `${fmtRequests(view.records)} 条账本记录`}</small></td>` +
      `<td class="wb-share-usage-requests" data-label="请求">${requestCell(view.requests)}</td>` +
      `<td class="wb-share-tok" data-label="Token"><strong>${tokens(view.tokens, view.inputTokens, view.outputTokens, false)}</strong></td>` +
      `<td class="wb-share-usage-cost" data-label="人民币"><span class="wb-key-cost"><small>已知人民币</small><strong>${cny(view.knownCny, view.knownCnyPartial)}</strong></span><span class="wb-key-cost"><small>估算人民币</small><span>${cny(view.estimatedCny, view.estimatedCnyPartial)}</span></span><small class="wb-muted wb-share-cny-total">合计人民币 ${cny(view.cny, view.cnyPartial)}</small><small class="wb-muted wb-share-usd-trace">原始 USD 已知 ${usd(view.known)} / 估算 ${usd(view.estimated)}</small><span class="wb-key-quality">用量未知 <b>${view.unknown === null ? "未知" : fmtRequests(view.unknown)}</b></span><span class="wb-key-quality">无价记录 <b>${view.unpriced === null ? "未知" : fmtRequests(view.unpriced)}</b></span></td></tr>`;
    // The drill is a full-width row below the summary, not a fifth column:
    // its five readings need the table width, or the header and the original-USD
    // pair wrap into three cramped lines.
    return (
      summary +
      `<tr class="wb-key-details-row"><td colspan="4"><details class="wb-secondary-details"${open ? " open" : ""} data-wb-share-key-details="${escapeHtml(view.identity)}"><summary title="模型与日期明细 · 点击展开 / 收起">模型与日期明细 · 模型 ${view.models.length} / 日 ${view.days.length}${undatedCount}${wbFoldMark()}<span class="wb-share-key-detail-body">${modelRows}${dayRows}${undatedRows}</span></summary></details></td></tr>`
    );
  }
  /** 分享页只显示掩码后的 Key。能 join 到管理库行时用它自己的 `masked`；
   *  join 不到（已删除 / 已轮换的 Key 仍在账本历史里）保留账本 id 的尾部并写明
   *  原因——既不隐藏该行，也绝不打印完整 Key。 */
  private shareUsageKeyLabel(keyId: string | null): string {
    if (!keyId) return "未归属";
    const row = this.shareKeyRow(keyId);
    if (row?.masked) return row.masked;
    return `…${keyId.slice(-4)}（管理列表已无该 Key：已删除或轮换，仅显示 id 尾部）`;
  }
  /** `checkedAt` 是 UTC ISO-8601 字符串；渲染时转本地时间并标出「检查时间」。 */
  private shareUsageTime(checkedAt?: string): string {
    if (typeof checkedAt !== "string" || !checkedAt) return "未提供";
    const at = new Date(checkedAt);
    return Number.isFinite(at.getTime())
      ? at.toLocaleString("zh-CN", { hour12: false })
      : "不可解析";
  }
  /** Six honest states (A36-1): 未开放/正在开启/已开放/正在关闭/关闭有残留/
   *  状态未确认. Derived only from evidence layers; contradictory or missing
   *  evidence never collapses to a fabricated clean state. */
  private shareStateOf(payload?: ShareStatusPayload): {
    key: string;
    text: string;
  } {
    if (!payload) return { key: "unconfirmed", text: "状态未确认（读取中）" };
    const funnelOn = payload.funnel_on === true;
    const funnelOff = payload.funnel_on === false;
    // gateway_running observes the actual share gateway INSTANCE through the
    // broker's read-only seam; broker.running is the always-on management
    // daemon and is deliberately NOT open/close-residue evidence (R9-R45.4:
    // a stale succeeded stop in the 8-row ops window used to shadow the
    // live open state because the residue branch ran first and read the
    // broker as residue).
    const gatewayUp = payload.gateway_running === true;
    const opInFlight =
      this.opsRecent?.state === "live"
        ? this.opsRecent.rows.find(
            (op) =>
              op.action.startsWith("share.") &&
              (op.state === "pending" ||
                op.state === "queued" ||
                op.state === "running")
          )
        : undefined;
    if (opInFlight?.action === "share.estop")
      return { key: "closing", text: "紧急停止中" };
    if (
      opInFlight?.action === "share.stop" ||
      opInFlight?.action === "share.retry_stop"
    ) {
      return { key: "closing", text: "正在关闭" };
    }
    if (opInFlight?.action === "share.start")
      return { key: "opening", text: "正在开启" };
    // A mapping is independent of local readiness and public DNS/inference.
    // A ready live chain supersedes an older succeeded stop in the ops window.
    if (funnelOn && localShareReadiness(payload).state === "ok")
      return { key: "open", text: "本机就绪 · 映射已开启" };
    // 关闭有残留: a stop/estop finished but a live layer still shows (the
    // gateway instance, or funnel). opsRecent holds only the last 8 rows.
    const stopDone =
      this.opsRecent?.state === "live" &&
      latestShareOperationWasStop(this.opsRecent.rows);
    if (stopDone && (gatewayUp || funnelOn))
      return { key: "closing-residue", text: "关闭有残留" };
    // A closed Funnel is an intentionally closed public entry, not missing
    // evidence: a running gateway with Funnel off is 未开放, never 状态未确认.
    if (funnelOff) {
      return { key: "closed", text: "未开放" };
    }
    if (funnelOn)
      return {
        key: "unconfirmed",
        text: `映射已开启 · 本机${localShareReadiness(payload).text}`,
      };
    return { key: "unconfirmed", text: "状态未确认（证据不全或互相矛盾）" };
  }
  /** The facade maps an unavailable broker response to running=false. This is
   *  reachability evidence, not proof that its protected SYSTEM task stopped. */
  private shareBrokerUnavailable(payload?: ShareStatusPayload): boolean {
    const current =
      payload ??
      (this.shareStatus?.state === "live"
        ? this.shareStatus.payload
        : undefined);
    return current?.broker?.running === false;
  }
  private shareBrokerRecoveryHtml(payload: ShareStatusPayload): string {
    if (!this.shareBrokerUnavailable(payload)) return "";
    return (
      `<div class="wb-share-alert wb-share-alert-danger" data-wb-share-broker-recovery role="status"><div><strong>受限后台未响应，需要先恢复后台</strong><span>暂时无法确认 8110 与受保护任务状态；连接失败不代表任务缺失，也不代表账号失效。</span>` +
      `<ol class="wb-muted"><li>在控制台目录运行 <code>Recover-ShareControl.bat</code>，按 Windows 管理员提示恢复后台。</li>` +
      `<li>返回后点“重新读取状态”；后台响应后，再到「诊断与恢复」页签读取分层状态。</li>` +
      `<li>需要时在「诊断与恢复」页签点“启动 / 按需登录 8110”（认证有效则跳过登录），再点“开启分享”。</li></ol></div>` +
      `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-copy-recovery title="复制恢复命令">复制恢复命令</button>` +
      `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-refresh>重新读取状态</button></div>`
    );
  }
  /** Shared enablement facts for the public check / retry / export buttons and
   *  the overview's one-click check. The server still owns the mutex; these
   *  reasons only disable the local control. */
  private shareActionBusy(): boolean {
    const inFlight = (entry: FeedbackEntry) =>
      entry.state === "preparing" ||
      entry.state === "queued" ||
      entry.state === "running";
    const feedbackBusy = [...this.feedbackEntries.values()].some(
      (entry) => inFlight(entry) && entry.action.startsWith("share.")
    );
    const opBusy =
      this.opsRecent?.state === "live" &&
      this.opsRecent.rows.some(
        (op) =>
          op.action.startsWith("share.") &&
          (op.state === "pending" ||
            op.state === "queued" ||
            op.state === "running")
      );
    return feedbackBusy || opBusy;
  }
  private shareActionGapText(row: ActionRow | undefined): string {
    return !this.actions
      ? "动作注册表未读取（读取中或读取失败）"
      : !row
        ? "当前后端版本不支持（待后端接入）"
        : `已停用：${row.disabled_reason ?? "原因未提供"}`;
  }
  private sharePublicCheckReason(): string {
    const read = this.publicShare;
    const live = read?.state === "live" ? read.payload : null;
    const checkCap = this.actions?.find(
      (row) => row.action === "share.public_check"
    );
    if (!this.actions) return this.shareActionGapText(undefined);
    if (!checkCap) return this.shareActionGapText(undefined);
    if (!checkCap.enabled) return this.shareActionGapText(checkCap);
    if (this.shareActionBusy()) return "任一分享动作在途，结束后再操作";
    if (read?.state !== "live") return "公网巡检读面不可用";
    if (live?.available !== true) return "公网巡检模块未部署";
    return "";
  }
  /** Connection details (broker reachability, publication sync, the readiness
   *  evidence list) moved into the diagnostics tab. The measured status failure
   *  is written once in the overview; this block points back to it. */
  private shareConnectionHtml(status?: ShareStatusState): string {
    const open = this.sharePanelsOpen.has("connection");
    if (!status) {
      return `<details class="wb-share-connection" data-wb-share-panel="connection"${open ? " open" : ""}><summary title="展开 / 收起连接详情">连接详情${wbFoldMark()}</summary><p class="wb-muted" data-state="loading">连接详情读取中…</p></details>`;
    }
    if (status.state === "loading") {
      return `<details class="wb-share-connection" data-wb-share-panel="connection"${open ? " open" : ""}><summary title="展开 / 收起连接详情">连接详情${wbFoldMark()}</summary><p class="wb-muted" data-state="loading">连接详情读取中…</p></details>`;
    }
    if (status.state !== "live") {
      return `<details class="wb-share-connection" data-wb-share-panel="connection"${open ? " open" : ""}><summary title="展开 / 收起连接详情">连接详情${wbFoldMark()}</summary><p class="wb-muted">连接详情读取失败：${escapeHtml(this.shareReadFailureText(status))}；原因见顶部状态。</p></details>`;
    }
    const payload = status.payload;
    const brokerLine = payload.broker
      ? payload.broker.running === true
        ? `受限后台已响应（PID ${payload.broker.pid ?? "—"} · ${payload.broker.version ?? "版本不可取得"}）`
        : payload.broker.running === false
          ? `受限后台未响应${payload.broker.error_code ? `（${payload.broker.error_code.slice(0, 80)}）` : ""}`
          : "受限后台状态未确认"
      : "受限后台状态未确认（证据不可取得）";
    const syncLine =
      payload.in_sync === true
        ? "发布同步一致"
        : payload.in_sync === false
          ? `发布同步不一致（desired ${payload.desired_generation ?? "?"} / applied ${payload.applied_generation ?? "?"}）`
          : "发布同步不可取得";
    const checkedAt =
      typeof payload.last_check_at === "number"
        ? new Date(payload.last_check_at * 1000).toLocaleString("zh-CN", {
            hour12: false,
          })
        : "不可取得";
    const failure =
      typeof payload.last_failure === "string" && payload.last_failure
        ? ` · 失败原因：${escapeHtml(payload.last_failure)}`
        : "";
    return (
      `<details class="wb-share-connection" data-wb-share-panel="connection"${open ? " open" : ""}><summary title="展开 / 收起连接详情">连接详情${wbFoldMark()}</summary>` +
      `<p class="wb-share-evidence">${escapeHtml(brokerLine)} · ${escapeHtml(syncLine)} · 最近检查 ${escapeHtml(checkedAt)}${failure}</p>${this.shareReadinessHtml(payload)}</details>`
    );
  }
  private latestSharePublicTest(): ShareKeyTest | undefined {
    const rows = this.shareKeys?.state === "live" ? this.shareKeys.rows : [];
    return [...this.shareKeyTests]
      .filter(([id, t]) =>
        rows.some(
          (r) =>
            r.id === id && r.enabled === true && r.row_version === t.rowVersion
        )
      )
      .map(([, t]) => t)
      .sort((a, b) => b.startedAt - a.startedAt)[0];
  }
  private shareReadinessHtml(payload: ShareStatusPayload): string {
    const latest = this.latestSharePublicTest();
    const local = localShareReadiness(payload);
    const { dns, model } = publicShareReadings(payload, latest);
    const mapping =
      payload.funnel_on === true
        ? { state: "ok", text: "已开启" }
        : payload.funnel_on === false
          ? { state: "idle", text: "已关闭" }
          : { state: "idle", text: "未确认" };
    const row = (
      key: string,
      label: string,
      value: { state: string; text: string },
      detail: string
    ) =>
      `<div data-wb-share-reading="${key}"><dt>${label}</dt><dd><span class="wb-badge wb-badge-${value.state}">${escapeHtml(value.text)}</span><small>${escapeHtml(detail)}</small></dd></div>`;
    const at = latest?.at
      ? new Date(latest.at * 1000).toLocaleString("zh-CN", { hour12: false })
      : "";
    const sameAddress = Boolean(
      payload.share_url &&
      latest?.publicUrl &&
      payload.share_url.replace(/\/$/, "") ===
        latest.publicUrl.replace(/\/$/, "")
    );
    return (
      `<dl class="wb-share-readiness" aria-label="分享可用性证据">` +
      row("local", "本机链路", local, "入口、8110 进程、专用认证与发布同步") +
      row("mapping", "Funnel 映射", mapping, "本机读取的映射状态") +
      row(
        "dns",
        "公网 DNS",
        dns,
        sameAddress && latest?.publicIp
          ? `${latest.publicIp}${at ? ` · ${at}` : ""}`
          : "由下方密钥的“测试”按钮检查"
      ) +
      row(
        "model",
        "真实模型调用",
        model,
        sameAddress
          ? [
              latest?.model,
              latest?.elapsedMs !== undefined ? `${latest.elapsedMs} ms` : "",
              at,
            ]
              .filter(Boolean)
              .join(" · ") || "点击密钥“测试”发送一次短请求，会产生少量用量"
          : "点击密钥“测试”发送一次短请求，会产生少量用量"
      ) +
      `</dl><p class="wb-muted">公网结果仅保留本页最近一次密钥测试，5 分钟后标为陈旧；另一设备的网络体验仍需单独确认。</p>`
    );
  }
  /** 已归属 Key 数（维护者 2026-09-28，Plan-Revision 9 逐页收纳 · 分享页首屏）：
   *  「开放状态、已归属 Key 数、主操作与失败/重试」四件事写在首屏。归属数只来自
   *  分享后端账本自己的读数（`/api/console/share-usage` 的 `keys[]` 计数 +
   *  `coverage.attributed` 请求计数），没有第二条来源；读口未读取 / 不可用 / 读取
   *  失败时如实写「不可用」并指回「每 Key 用量与费用」块，绝不折成「0 个 Key」。
   *  这一行不带任何实测 HTTP 状态：状态词只由分享状态读口那一条失败句写出一次。 */
  private shareAttributedKeysHtml(): string {
    const read = this.shareUsage;
    if (!read || read.state === "loading")
      return `<small>已归属 Key：读取中…</small>`;
    if (read.state !== "live") {
      return `<small>已归属 Key：不可用（分享用量读口未读取到，见「用量」页签；不以 0 个冒充）</small>`;
    }
    const keys = read.payload.keys ?? [];
    const attributed = read.payload.coverage?.attributed;
    const unattributed = read.payload.unattributed;
    return `<small>已归属 Key：${fmtRequests(keys.length)} 个（账本 keys[] 计数）· 已归因请求 ${typeof attributed === "number" ? fmtRequests(attributed) : "未提供"}${unattributed ? " · 另有未归属桶单列，不摊到任何 Key" : ""}</small>`;
  }
  /** Lifecycle buttons per state (A36-1 block ①). All arm a confirm first.
   *  The per-layer 启动 / 按需登录 8110 entry lives in the diagnostics tab,
   *  not in this compact overview; a read never triggers it automatically. */
  private shareActionButtonsHtml(payload: ShareStatusPayload): string {
    const state = this.shareStateOf(payload);
    const brokerGate = this.shareBrokerUnavailable(payload)
      ? ` disabled title="先按上方指引恢复受限后台，再读取分层状态"`
      : "";
    const startBtn =
      state.key === "closed" ||
      state.key === "closing-residue" ||
      state.key === "unconfirmed"
        ? `<button type="button" class="wb-btn" data-wb-share-action="share.start"${brokerGate}>开启分享</button>`
        : "";
    const stopBtn =
      state.key === "open" || state.key === "unconfirmed"
        ? `<button type="button" class="wb-btn" data-wb-share-action="share.stop">停止分享</button>`
        : "";
    const retryStopBtn =
      state.key === "closing-residue"
        ? `<button type="button" class="wb-btn" data-wb-share-action="share.retry_stop">继续关闭</button>`
        : "";
    const estopBtn = `<button type="button" class="wb-btn wb-danger" data-wb-share-action="share.estop"${state.key === "closed" ? ` disabled title="分享已关闭"` : ""}>紧急停止</button>`;
    // A down 8100 is not a dead end: the same fixed single-layer action the
    // diagnostics card offers stays reachable from the overview, and it only
    // restarts the gateway (8110 and the public mapping are left alone).
    const recoverGatewayBtn =
      payload.gateway_running === false
        ? `<button type="button" class="wb-btn" data-wb-share-action="share.gateway_restart"${brokerGate} title="只恢复 / 重启 8100；不动 8110 与公网映射">恢复分享网关</button>`
        : "";
    return [recoverGatewayBtn, startBtn, stopBtn, retryStopBtn, estopBtn]
      .filter(Boolean)
      .join("");
  }
  /** Keys table (A36-1 block ③ + R11-B1). The API Key column renders the
   *  FULL plaintext for rows whose same-identity ciphertext exists (the
   *  narrow per-key endpoint), the masked identifier for every other row;
   *  the resident plaintext section below the table stays this session's own
   *  reveal surface (session-scoped, R9-R46).
   *  (R9-R43) The facade carries no per-key sync source — publication sync
   *  is a property of the whole config generation, shown on the status
   *  block; the old per-row「配置生效」column was dropped rather than
   *  fabricated. */
  private shareKeysTableHtml(rows: ShareKeyRow[]): string {
    if (!rows.length) return `<p class="wb-muted">当前无访问密钥。</p>`;
    return (
      `<table class="wb-share-key-table"><thead><tr><th>名称</th><th>密钥</th><th>启用</th><th>并发</th><th>操作</th></tr></thead><tbody>` +
      rows
        .map((row) => {
          const id = row.id ?? "";
          const enabled = row.enabled === true;
          const test0 = this.shareKeyTests.get(id);
          const test =
            test0?.rowVersion === row.row_version ? test0 : undefined;
          const probeReasons: Record<string, string> = {
            key_rejected: "密钥被拒绝",
            upstream_unavailable: "上游暂不可用",
            invalid_response: "响应无效",
            no_models: "未返回可用模型",
            timeout: "连接超时",
            connection_failed: "连接失败",
            http_error: "入口返回错误",
            key_changed: "密钥已变化，请刷新",
            key_disabled: "密钥已停用",
            secret_unavailable: "本机无法读取密钥明文",
            gateway_unconfirmed: "分享进程身份未确认",
            tls_handshake_failed: "公网 TLS 握手失败",
            tls_certificate_invalid: "公网证书校验失败",
            dns_failed: "公网 DNS 解析失败",
            public_dns_failed: "公网 DNS 查询失败",
            public_url_unavailable: "公网地址未确认",
            redirect_refused: "公网入口发生重定向",
            rate_limited: "请求被限流，请稍后重试",
            test_in_progress: "已有测试进行中，请稍后重试",
            probe_failed: "公网测试未完成",
            probe_contract_mismatch:
              "接口尚未支持公网模型测试，请更新控制台服务",
          };
          const oldSample =
            test?.at !== undefined &&
            (Date.now() / 1000 - test.at > 300 || Date.now() / 1000 < test.at);
          const testLabel =
            (test?.state === "busy"
              ? "公网模型测试中…"
              : test?.state === "ok"
                ? "已连通，模型已回复（测试时）"
                : test
                  ? `测试失败：${probeReasons[test.reason] ?? "检查未完成"}`
                  : "") +
            (test?.state !== "busy" && oldSample ? "（已陈旧）" : "");
          const testDetail = test
            ? [
                "公网往返",
                test.model,
                test.elapsedMs !== undefined
                  ? `${test.elapsedMs} ms`
                  : undefined,
                test.at
                  ? new Date(test.at * 1000).toLocaleString("zh-CN", {
                      hour12: false,
                    })
                  : undefined,
              ]
                .filter(Boolean)
                .join(" · ")
            : "";
          // Inline edit (R9-R46): the edited cell swaps to an input + 保存/取消
          // in place of the window.prompt flow; only one cell edits at a time.
          const editing =
            this.shareKeyEdit?.id === id ? this.shareKeyEdit : undefined;
          const nameCell =
            editing?.field === "name"
              ? `<td class="wb-share-key-name"><div class="wb-share-edit"><input type="text" data-wb-share-edit value="${escapeHtml(editing.value)}" maxlength="64" aria-label="新名称"/><div class="wb-share-edit-actions"><button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="save-edit" data-wb-share-key-id="${escapeHtml(id)}">保存</button><button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="cancel-edit">取消</button></div></div></td>`
              : `<td class="wb-share-key-name">${escapeHtml(row.name ?? "—")}</td>`;
          const capCell =
            editing?.field === "cap"
              ? `<td class="wb-share-key-cap"><div class="wb-share-edit"><input type="number" data-wb-share-edit min="1" max="64" inputmode="numeric" value="${escapeHtml(editing.value)}" aria-label="每 Key 并发上限（1–64）"/><div class="wb-share-edit-actions"><button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="save-edit" data-wb-share-key-id="${escapeHtml(id)}">保存</button><button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="cancel-edit">取消</button></div></div></td>`
              : `<td class="wb-share-key-cap">${row.max_concurrency ?? "—"}</td>`;
          return (
            `<tr>${nameCell}` +
            `<td class="wb-share-key-secret" data-share-key-cell="${escapeHtml(id)}">${this.shareKeyValueCellHtml(row)}</td>` +
            // 契约 3.5：`wb-dot-ok` / `wb-dot-off` 两个类在 CSS 里不存在，
            // 启用与停用因此渲染成**同一个**中性实心圆，只能靠文字区分。改用
            // 既有形状：启用 = 实心圆，停用 = 空心环，颜色与形状同时区分。
            `<td class="wb-share-key-state"><i class="wb-dot wb-dot-${enabled ? "run" : "unknown"}"></i>${enabled ? "启用" : "停用"}</td>` +
            capCell +
            `<td class="wb-share-key-opcell"><div class="wb-share-key-actions">` +
            `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="test" data-wb-share-key-id="${escapeHtml(id)}" title="经过公网 HTTPS 分享地址读取模型并发送一次短对话，最多 32 个输出 Token；产生少量模型用量，成功表示本次公网往返与模型回复通过"${!enabled || test?.state === "busy" ? " disabled" : ""}>测试</button>` +
            `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="toggle-enable" data-wb-share-key-id="${escapeHtml(id)}">${enabled ? "停用" : "启用"}</button>` +
            `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="rename" data-wb-share-key-id="${escapeHtml(id)}">改名</button>` +
            `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="adjust-cap" data-wb-share-key-id="${escapeHtml(id)}">调并发</button>` +
            `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="rotate" data-wb-share-key-id="${escapeHtml(id)}">轮换</button>` +
            `<button type="button" class="wb-btn wb-btn-sm wb-danger" data-wb-share-key="revoke-terminate" data-wb-share-key-id="${escapeHtml(id)}">撤销并终止</button>` +
            `<button type="button" class="wb-btn wb-btn-sm wb-danger" data-wb-share-key="delete" data-wb-share-key-id="${escapeHtml(id)}">删除</button>` +
            (test
              ? `<small class="wb-share-key-test" data-wb-share-key-id="${escapeHtml(id)}" role="status" title="${escapeHtml([testLabel, testDetail].filter(Boolean).join(" · "))}">${escapeHtml(testLabel)}${testDetail ? ` · ${escapeHtml(testDetail)}` : ""}</small>`
              : `<small class="wb-share-key-test" data-wb-share-key-id="${escapeHtml(id)}" role="status"></small>`) +
            `</div></td></tr>`
          );
        })
        .join("") +
      `</tbody></table>`
    );
  }
  /** Create entry button (block ③). The form itself lives in the key-create
   *  modal under .wb-main — outside the morphed .wb-content — synced per
   *  render by syncShareModal; Enter submits, Esc cancels (R9-R46). */
  private shareKeyCreateButtonHtml(): string {
    return `<button type="button" class="wb-btn" data-wb-share-key="create">新增密钥</button>`;
  }
  /** Key-create modal (R9-R46): lazily created once under .wb-main, so the
   *  in-place morph patch of .wb-content never resets it — only hidden or
   *  shown here, and its input values survive every repaint. Two views:
   *  form → success (create keeps the modal open; 完成 closes and hands
   *  focus back to the 新增密钥 button). */
  private syncShareModal() {
    const main = this.root.querySelector<HTMLElement>(".wb-main");
    if (!main) return;
    const existing = main.querySelector<HTMLElement>(
      ".wb-share-modal-backdrop"
    );
    if (!this.shareKeyFormOpen) {
      if (existing) existing.hidden = true;
      return;
    }
    let backdrop = existing;
    if (!backdrop) {
      // position:fixed (not absolute inset:0): .wb-main is an overflow-y:auto
      // scroll container, so a padding-box-anchored backdrop would scroll
      // away with the lane content instead of covering the viewport.
      main.insertAdjacentHTML(
        "beforeend",
        `<div class="wb-share-modal-backdrop" hidden role="presentation">` +
          `<div class="wb-share-modal" role="dialog" aria-modal="true" aria-label="新增访问密钥">` +
          `<div data-share-modal-view="form">` +
          `<h3 class="wb-share-modal-title">新增访问密钥</h3>` +
          `<form onsubmit="return false">` +
          `<label class="wb-share-modal-field">名称<input type="text" data-wb-share-key-name maxlength="64"></label>` +
          `<label class="wb-share-modal-field">每 Key 并发（1–64，留空用服务默认）<input type="number" min="1" max="64" inputmode="numeric" data-wb-share-key-cap></label>` +
          `<p class="wb-share-error" data-share-modal-error hidden></p>` +
          `<div class="wb-share-key-form-actions"><button type="button" class="wb-btn" data-wb-share-key="submit-create">创建</button><button type="button" class="wb-btn" data-wb-share-key="cancel-create">取消</button></div>` +
          `</form></div>` +
          `<div data-share-modal-view="success" hidden>` +
          `<h3 class="wb-share-modal-title">密钥已创建</h3>` +
          `<p class="wb-share-modal-note" data-share-modal-note></p>` +
          `<code class="wb-share-secret-value" data-share-modal-secret></code>` +
          `<div class="wb-share-key-form-actions"><button type="button" class="wb-btn" data-wb-share-secret-copy title="复制明文（仅本次会话内存，不写任何持久化）" aria-label="复制明文">${wbIcon("copy")}${wbIconHint("复制明文")}</button><button type="button" class="wb-btn" data-wb-share-key="modal-complete">完成</button></div>` +
          `</div>` +
          `</div></div>`
      );
      const fresh = main.querySelector<HTMLElement>(".wb-share-modal-backdrop");
      if (!fresh) return;
      backdrop = fresh;
      fresh.addEventListener("click", (event) => {
        if (event.target === fresh) this.closeShareKeyModal();
      });
      fresh.addEventListener("keydown", (event) => {
        // Escape is NOT handled here: the document-level listener owns it, so a
        // close works even when focus has left the dialog (the old branch here
        // only ran while focus was inside — exactly the reported bug).
        // P2 (2026-09-24): the agreed keyboard contract — Enter in the
        // name field moves focus to the concurrency field, and only the
        // concurrency field's Enter submits. A keydown that is part of an
        // IME composition (isComposing / keyCode 229) belongs to the input
        // method and must never submit.
        if (event.key !== "Enter" || event.isComposing || event.keyCode === 229)
          return;
        const target = event.target as HTMLInputElement | null;
        if (target?.matches?.("[data-wb-share-key-name]")) {
          event.preventDefault();
          fresh
            .querySelector<HTMLInputElement>("[data-wb-share-key-cap]")
            ?.focus();
          return;
        }
        if (target?.matches?.("[data-wb-share-key-cap]")) {
          event.preventDefault();
          fresh
            .querySelector<HTMLButtonElement>(
              "[data-wb-share-key='submit-create']"
            )
            ?.click();
        }
      });
    }
    backdrop.hidden = false;
    const formView = backdrop.querySelector<HTMLElement>(
      '[data-share-modal-view="form"]'
    );
    const successView = backdrop.querySelector<HTMLElement>(
      '[data-share-modal-view="success"]'
    );
    if (formView) formView.hidden = this.shareKeyModalView !== "form";
    if (successView) successView.hidden = this.shareKeyModalView !== "success";
    const secret = backdrop.querySelector<HTMLElement>(
      "[data-share-modal-secret]"
    );
    if (secret && this.sharePlaintext)
      secret.textContent = this.sharePlaintext.value;
    // P2 (2026-09-24): the success note must match what the backend ACTUALLY
    // stored — a DPAPI failure degrades the row to hash-only, and claiming
    // 明文已写入本机 then would be false for exactly the key in the dialog.
    const note = backdrop.querySelector<HTMLElement>("[data-share-modal-note]");
    if (note) {
      note.textContent =
        this.shareKeyPlaintextViewable === false
          ? "明文仅在本次响应可见：本机未写入（DPAPI 保护失败），请立即复制；该行会显示「旧密钥不可恢复」，可稍后手动轮换重试。"
          : "明文已写入本机（同一运行身份可回看）：「访问密钥」列的 API Key 单元随时可查看复制；换运行身份或换机器不可回看，轮换会覆盖旧明文。";
    }
    const errorLine = backdrop.querySelector<HTMLElement>(
      "[data-share-modal-error]"
    );
    if (errorLine) {
      errorLine.textContent = this.shareKeyError;
      errorLine.hidden =
        !this.shareKeyError || this.shareKeyModalView !== "form";
    }
    // P2 (2026-09-24): the create request disables its own submit button
    // until the response lands (the modal now stays open through the request).
    const submit = backdrop.querySelector<HTMLButtonElement>(
      "[data-wb-share-key='submit-create']"
    );
    if (submit) {
      submit.disabled = this.shareKeySubmitting;
      submit.textContent = this.shareKeySubmitting ? "创建中…" : "创建";
    }
    if (this.shareKeyFocusName && this.shareKeyModalView === "form") {
      this.shareKeyFocusName = false;
      // A fresh open always starts empty (the persistent modal node would
      // otherwise carry the previous session's typed values).
      const nameInput = backdrop.querySelector<HTMLInputElement>(
        "[data-wb-share-key-name]"
      );
      const capInput = backdrop.querySelector<HTMLInputElement>(
        "[data-wb-share-key-cap]"
      );
      if (nameInput) nameInput.value = "";
      if (capInput) capInput.value = "";
      nameInput?.focus();
    }
  }
  /** Close the key-create modal and hand focus back to the 新增密钥 button. */
  private closeShareKeyModal() {
    this.shareKeyFormOpen = false;
    this.shareKeyModalView = "form";
    this.shareKeyError = "";
    this.shareKeyFocusName = false;
    this.renderPanel();
    this.root
      .querySelector<HTMLButtonElement>('[data-wb-share-key="create"]')
      ?.focus();
  }
  /** Copy-button feedback: 「已复制」 for 1.5s, then back. A morph repaint
   *  resets the label within the flash window (same failure family as the
   *  armed confirm), so the flash is also tracked in flashCopiedState and
   *  re-applied by reflashCopied after every rebuild. 契约 3.13b：纯图标
   *  复制按钮没有文本节点，基值因此取 `innerHTML`（取 textContent 会把
   *  图标在闪动结束时永久抹掉）；`aria-label` 不随反馈抖动改写。 */
  private flashCopied(button: HTMLElement) {
    const base = button.dataset.wbBaseLabel ?? button.innerHTML;
    const sel =
      button.dataset.wbShareSecretCopy !== undefined
        ? "[data-wb-share-secret-copy]"
        : button.dataset.wbShareCopy !== undefined
          ? "[data-wb-share-copy]"
          : button.dataset.wbModelIdCopy !== undefined
            ? "[data-wb-model-id-copy]"
            : null;
    if (sel) this.flashCopiedState = { sel, base, until: Date.now() + 1500 };
    button.dataset.wbBaseLabel = base;
    button.textContent = "已复制";
    window.setTimeout(() => {
      if (button.isConnected && button.dataset.wbBaseLabel === base) {
        button.innerHTML = base;
        delete button.dataset.wbBaseLabel;
      }
    }, 1500);
  }
  /** Re-apply the 「已复制」 flash after a repaint replaced/reset the flashed
   *  node; schedules the remaining restore on the current node. */
  private reflashCopied() {
    const st = this.flashCopiedState;
    if (!st) return;
    const remaining = st.until - Date.now();
    if (remaining <= 0) {
      this.flashCopiedState = null;
      return;
    }
    const button = this.root.querySelector<HTMLElement>(st.sel);
    if (!button) {
      this.flashCopiedState = null;
      return;
    }
    button.dataset.wbBaseLabel = st.base;
    button.textContent = "已复制";
    window.setTimeout(() => {
      if (button.isConnected && button.dataset.wbBaseLabel === st.base) {
        button.innerHTML = st.base;
        delete button.dataset.wbBaseLabel;
      }
      if (this.flashCopiedState === st) this.flashCopiedState = null;
    }, remaining);
  }
  /** Share URL (block ②). URL copyable only when open; 未开放 marked
   *  honestly — never a fabricated address (A36-2). An UNANSWERED read is
   *  neither: with no payload yet the line says 读取中… instead of asserting
   *  「不可取得」 or 「当前未开放」. A failed status read points at the
   *  resident overview instead of printing the same reason twice. */
  private shareUrlHtml(status?: ShareStatusState): string {
    if (!status) {
      return `<p class="wb-muted" data-state="loading">分享地址读取中…（请求未返回前不写「不可取得」，也不写「当前未开放」）。</p>`;
    }
    if (status.state === "loading") {
      return `<p class="wb-muted" data-state="loading">分享地址读取中…</p>`;
    }
    if (status.state !== "live") {
      return `<p class="wb-muted">分享地址不可取得（状态读取失败，原因见顶部状态）；不以「读取中」冒充未完成，也不以文档冒充状态。</p>`;
    }
    const payload = status.payload;
    const url = payload.share_url;
    return (
      `<div class="wb-share-url-row">` +
      (typeof url === "string" && url
        ? `<code class="wb-share-url">${escapeHtml(url)}</code><button type="button" class="wb-btn wb-btn-sm" data-wb-share-copy="${escapeHtml(url)}" title="复制分享地址" aria-label="复制分享地址">${wbIcon("copy")}${wbIconHint("复制分享地址")}</button>`
        : `<code class="wb-share-url wb-share-url-unavailable">不可取得（未开放时不伪造地址）</code>`) +
      (payload.funnel_on === true
        ? ""
        : ` <small class="wb-share-closed-mark">${payload.funnel_on === false ? "Funnel 映射已关闭" : "Funnel 映射未确认"}</small>`) +
      `</div>`
    );
  }
  /** Preparation conditions moved into the diagnostics tab: the same five
   *  layers, folded by default, and still pointing at the one status failure
   *  sentence in the overview rather than repeating it. */
  private sharePreconditionsHtml(status?: ShareStatusState): string {
    const open = this.sharePreconditionsOpen ? " open" : "";
    if (!status || status.state === "loading") {
      return `<details class="wb-share-preconditions wb-secondary-details"${open} data-wb-share-preconditions="1"><summary title="准备条件明细（默认折叠 · 可执行项与未知分层）· 点击展开 / 收起">准备条件${wbFoldMark()}</summary><p class="wb-muted" data-state="loading">准备条件读取中…</p></details>`;
    }
    if (status.state !== "live") {
      return `<details class="wb-share-preconditions wb-secondary-details"${open} data-wb-share-preconditions="1"><summary title="准备条件明细（默认折叠 · 可执行项与未知分层）· 点击展开 / 收起">准备条件${wbFoldMark()}</summary><p class="wb-muted">准备条件读取失败：原因见顶部状态。</p></details>`;
    }
    const payload = status.payload;
    const accountLine = payload.account
      ? payload.account.valid === true
        ? "专用账号认证有效"
        : payload.account.valid === false
          ? `专用账号认证无效${payload.account.failure ? `（${escapeHtml(payload.account.failure)}）` : ""}`
          : "专用账号认证状态不可取得"
      : "专用账号认证不可取得";
    const blocked = payload.blocked_publications;
    const blockedCount = Array.isArray(blocked) ? blocked.length : null;
    const pubFail = payload.last_publication_failure;
    const pubFailLine =
      pubFail && typeof pubFail === "object"
        ? `${escapeHtml(String(pubFail.error ?? "发布失败")).slice(0, 120)}（${escapeHtml(String(pubFail.at ?? "")).slice(0, 19)}）`
        : "";
    const keyLine =
      payload.active_keys !== undefined
        ? payload.active_keys > 0
          ? `已启用 ${payload.active_keys} / 共 ${payload.total_keys ?? "?"}`
          : `<b class="wb-stale-warn">缺有效 Key（active ${payload.active_keys ?? 0} / total ${payload.total_keys ?? "?"}）——可在「访问密钥」页签创建；不自动创建正式 Key、不复活已删除 Key</b>`
        : "不可取得";
    const syncDetail =
      payload.in_sync === false
        ? `不一致（desired ${payload.desired_generation ?? "?"} / applied ${payload.applied_generation ?? "?"}${payload.pending_revocation ? " · 有未确认撤销发布" : ""}${blockedCount !== null && blockedCount > 0 ? ` · ${blockedCount} 条发布受阻` : ""}${pubFailLine ? ` · 最近失败：${pubFailLine}` : ""}）；安全恢复路径：share.retry_stop 按剩余步骤继续关闭/确认（不要求先重开）；发布不自动重发`
        : payload.in_sync === true
          ? "同步"
          : "不可取得";
    return (
      `<details class="wb-share-preconditions wb-secondary-details"${open} data-wb-share-preconditions="1"><summary title="准备条件明细（默认折叠 · 可执行项与未知分层）· 点击展开 / 收起">准备条件${wbFoldMark()}</summary><ul>` +
      `<li>有效密钥：${keyLine}${payload.active_keys === 0 ? "（测试 Key 仅在受控验证窗口内创建，明文在页面会话内可重复查看，完成/失败/超时后撤销）" : ""}</li>` +
      `<li>发布同步：${syncDetail}</li>` +
      `<li>broker 运行版本：${payload.broker ? (payload.broker.running === true ? `运行中（${escapeHtml(payload.broker.version ?? "版本不可取得")}）` : payload.broker.running === false ? "未运行" : "不可取得") : "不可取得"}</li>` +
      `<li>实例/上游通道：${payload.upstream?.reachable === true ? "可达" : payload.upstream?.reachable === false ? "不可达" : "不可取得"}</li>` +
      `<li>${accountLine}</li>` +
      `<li>公网入口：${payload.funnel_on === true ? "开放" : payload.funnel_on === false ? "关闭" : "不可取得"}</li>` +
      `</ul><p class="wb-muted">分层维护可读取专用后端认证、Tailscale、Funnel 和看门狗证据；公网 DNS 与真实模型回复由密钥“测试”按钮检查。</p></details>`
    );
  }
  /** The SAME status answer's failure state, spelled for the blocks that read
   *  it: the resident overview writes the measured reason exactly once, and the
   *  address line, connection details, capacity block and preparation conditions
   *  point at it instead of repeating it — 「还在读」 and 「已经失败」 stay
   *  distinguishable, but the same failure reason is not printed twice on one
   *  page (维护者 2026-09-28 的 ④：状态在页面上只允许出现一次). */
  private shareReadFailureText(status: ShareStatusState): string {
    if (status.state === "gated")
      return "无凭据会话（8001 不可达或未授权；原因见顶部状态）";
    if (status.state === "refused") return "403 权限不符（原因见顶部状态）";
    if (status.state === "error") return "原因见顶部状态";
    return "读取未完成";
  }
  /** Capacity + recent share ops (block ④). Global limit and per-key caps
   *  shown separately; recent ops filtered to share.* only — when crowded
   *  out of the 8-row window the page says so, never backfills history
   *  (A36-1: 总览不回填最近操作列表). */
  private shareCapacityHtml(status?: ShareStatusState): string {
    const payload = status?.state === "live" ? status.payload : undefined;
    const cap =
      typeof payload?.share_max_concurrency === "number"
        ? payload.share_max_concurrency
        : undefined;
    const active =
      typeof payload?.active_keys === "number"
        ? payload.active_keys
        : undefined;
    const total =
      typeof payload?.total_keys === "number" ? payload.total_keys : undefined;
    const recent =
      this.opsRecent?.state === "live"
        ? this.opsRecent.rows.filter((op) => op.action.startsWith("share."))
        : [];
    // An unanswered share-status read is not 「不可取得」: the limits line says
    // 读取中… until the request answers. A FAILED read says so here as well —
    // it used to keep 读取中… forever after a 503 (the state the hero above
    // names), so 「还在读」 and 「已经失败」 were indistinguishable. The
    // recent-ops list is an INDEPENDENT read and keeps its own honest state
    // either way.
    let limits: string;
    if (payload) {
      limits =
        `<div class="wb-share-limits"><div><small>全局分享并发上限</small><b>${cap ?? "不可取得"}</b></div>` +
        `<div><small>启用密钥 / 密钥总数</small><b>${active ?? "—"} / ${total ?? "—"}</b></div></div>`;
    } else if (status && status.state !== "loading") {
      limits = `<p class="wb-muted">全局分享并发上限与密钥计数读取失败：${this.shareReadFailureText(status)}；不以「读取中」冒充未完成，也不以数字冒充读数。</p>`;
    } else {
      limits = `<p class="wb-muted" data-state="loading">全局分享并发上限与密钥计数读取中…（未返回前不写「不可取得」）。</p>`;
    }
    return (
      limits +
      `<h4>近期分享操作（仅本页相关记录）</h4>` +
      (this.opsRecent === undefined
        ? `<p class="wb-muted" data-state="loading">操作账本读取中…（未返回前不写「不可读」，也不写「没有记录」）。</p>`
        : this.opsRecent.state !== "live"
          ? `<p class="wb-muted">${escapeHtml(this.opsReadFailureText())}；无权威来源，不以文档冒充记录。</p>`
          : !recent.length
            ? `<p class="wb-muted">最近 8 条操作中没有分享相关记录；更早的操作请到「日志」lane 核对（本页不回填历史操作列表）。</p>`
            : `<ul class="wb-share-recent">${recent.map((op) => `<li><b>${escapeHtml(op.action)}</b><small> · ${this.opTime(op.created_at)} · ${escapeHtml(op.state)}${op.error?.message ? ` · ${escapeHtml(op.error.message)}` : ""}</small></li>`).join("")}</ul>`)
    );
  }
  /** Plaintext section (A36-2 origin, relaxed R9-R46): this session's
   *  create/rotate plaintext is held in memory for this page — visible by
   *  default (single-user machine) with copy / mask / clear controls, and it
   *  survives lane switches and repaints. Only the 清除 button or a page
   *  reload drops it; nothing persists (no localStorage, no operation
   *  records, no generic feedback). After a clear the section leaves a muted
   *  note. Cross-page-session viewing of any key's plaintext rides the narrow
   *  per-key endpoint (same Windows run identity, R11-B1); legacy hash-only
   *  keys need a manual rotation first. */
  private sharePlaintextPanelHtml(): string {
    const p = this.sharePlaintext;
    if (!p) {
      return this.shareSecretCleared
        ? `<p class="wb-muted">本次会话内明文已清除；如需再次取得请对该 Key 使用「轮换」。</p>`
        : "";
    }
    const shown = this.shareSecretHidden
      ? "••••••••••••••••"
      : escapeHtml(p.value);
    return (
      `<div class="wb-share-secret" role="status">` +
      `<strong>${p.kind === "created" ? "密钥已创建" : "密钥已轮换"} · ${escapeHtml(p.name)} · 明文会话内可随时查看</strong>` +
      `<code class="wb-share-secret-value${this.shareSecretHidden ? " wb-share-secret-masked" : ""}">${shown}</code>` +
      `<div class="wb-share-secret-actions">` +
      `<button type="button" class="wb-btn" data-wb-share-secret-copy title="复制本次会话内的明文" aria-label="复制本次会话内的明文">${wbIcon("copy")}${wbIconHint("复制本次会话内的明文")}</button>` +
      `<button type="button" class="wb-btn" data-wb-share-secret-toggle>${this.shareSecretHidden ? "显示" : "隐藏"}</button>` +
      `<button type="button" class="wb-btn wb-danger" data-wb-share-secret-clear>清除</button>` +
      `</div>` +
      `<small>明文仅保存在本页面内存中，本次会话内可随时查看与复制；不写入 localStorage、不进操作记录。「访问密钥」列的明文回看走按 Key 端点（同一运行身份，跨页面会话可回看）；旧密钥需手动轮换后才可见。</small>` +
      `</div>`
    );
  }
  /** Operation state → the honest Chinese word the ledger rows render. Shared
   *  by the failure groups and the full list so the two views of the SAME 8001
   *  ledger can never disagree about what one row says. `cancelled` /
   *  `interrupted` are the backend's own terminal states
   *  (console_operations.py TERMINAL_STATES) and are named as such instead of
   *  falling through in English. */
  private opStateText(state: string): string {
    return (
      (
        {
          succeeded: "成功",
          failed: "失败",
          queued: "排队中",
          running: "执行中",
          cancelled: "已取消",
          interrupted: "被中断（服务重启）",
        } as Record<string, string>
      )[state] ?? state
    );
  }
  /** Operation state → dot shape class. 成功 = 实心圆, 失败 = 实心方,
   *  queued/running = 琥珀（进行中，既非成功也非失败）; 已取消 = 中空圆
   *  （有人主动停的，不是告警，也不冒充成功），被中断 = 失败形（没有正常
   *  结束）。只用既有类名，不新造 CSS 类。 */
  private opDotCls(state: string): string {
    if (state === "succeeded") return "run";
    if (state === "failed" || state === "interrupted") return "down";
    if (state === "cancelled") return "unknown";
    return "watch";
  }
  /** 一行操作的取消状态后缀：后端 `cancel_requested` 为真且行仍未终态时写
   *  「已请求取消」——受理 ≠ 已取消，终态仍由账本的 `state` 回答。 */
  private opCancelMark(op: OpRow): string {
    const nonTerminal =
      op.state === "queued" || op.state === "running" || op.state === "pending";
    return op.cancel_requested === true && nonTerminal
      ? ` · 已请求取消（受理 ≠ 已取消，终态以账本为准）`
      : "";
  }
  /** Operation-record lane main area (contract 4.4): the FULL 8001 operation
   *  ledger window, newest first — the same rows the failure groups above are a
   *  filtered view of. It is never a second ledger: an unreachable 8001 says so
   *  instead of rendering an empty list that would read as 「没有操作」. */
  private opsListHtml(): string {
    const live = this.opsRecent?.state === "live";
    const rows = live ? this.opsRecent!.rows : [];
    // 协作取消（usagesrv.py:3270-3282 `POST /operations/{id}/cancel` +
    // console_operations.py:479+）：只在**后端标明可取消**（动作注册表
    // `cancelable === true`）且**仍在运行/排队**时开放。受理只写
    // `cancel_requested`：排队中的操作由后端直接落 `cancelled`（从未执行），
    // 执行中的要等当前步骤结束才落终态——所以「已请求取消」与「已取消」
    // 是两件事，按钮状态与账本状态都按后端回答显示，不由前端推断。
    const cancel = (op: OpRow) => {
      if (op.state !== "queued" && op.state !== "running") return "";
      const spec = this.actions?.find((a) => a.action === op.action);
      if (!spec)
        return ` <button type="button" class="wb-btn wb-btn-sm" disabled title="动作注册表未读取：无法确认该动作可取消（不猜一个状态）">取消</button>`;
      if (!spec.cancelable)
        return ` <button type="button" class="wb-btn wb-btn-sm" disabled title="该动作后端标明不可取消（动作注册表 cancelable=false）">取消</button>`;
      if (this.cancelInFlight.has(op.id))
        return ` <button type="button" class="wb-btn wb-btn-sm" disabled title="取消请求在途">取消中…</button>`;
      // 受理以后端回答为准（`op.cancel_requested`），不再只看客户端集合：否则
      // 同一行会一边写「已请求取消」（后端已受理）一边给出可点按钮，点一次
      // 就重复提交一次。客户端集合只补上「已发出、账本尚未回读」的在途窗口。
      if (op.cancel_requested === true || this.cancelRequested.has(op.id)) {
        return ` <button type="button" class="wb-btn wb-btn-sm" disabled title="已请求取消：受理 ≠ 已取消，终态以操作账本为准">已请求取消</button>`;
      }
      const error = this.cancelError.get(op.id);
      return ` <button type="button" class="wb-btn wb-btn-sm" data-wb-op-cancel="${escapeHtml(op.id)}" title="向 POST /api/console/operations/{id}/cancel 发一次协作取消请求（只写 cancel_requested；排队中的操作会立刻落 cancelled，执行中的要等当前步骤结束）">取消${error ? `（上次失败：${escapeHtml(error)}）` : ""}</button>`;
    };
    // 三态分开：`undefined` = 本次读取尚未返回（写「读取中」），`gated` =
    // 读取已失败（写实测原因），`live` = 已读到（才可写条数或空态）。读取在途
    // 时不得先把结论写成「本次未读到」——那会把「还在读」读成「读不到」。
    const loading = this.opsRecent === undefined;
    // 未读到时不写「最近 0 条」：0 条是「读到了、且一条没有」，与「本次未读到」
    // 是两个结论，标题因此分开写（未知与零不混）。
    return (
      `<section class="wb-block wb-char"><h3>操作账本（${live ? `最近 ${rows.length} 条 · 8001` : loading ? "读取中 · 未知 ≠ 0" : "本次未读到 · 未知 ≠ 0"}）</h3>` +
      `<p class="wb-muted wb-lane-note">${escapeHtml(this.failuresNote(true))}</p>` +
      this.laneNoteFoldHtml(
        "ops-cancel",
        "失败分组与取消口径",
        `<p class="wb-muted">上方失败分组是同一账本按 action 聚合的过滤视图，不是另一份错误账本。取消只对后端标明可取消且仍在运行/排队的操作开放；受理（requested）只写 cancel_requested，「已请求取消」不等于「已取消」——终态由操作账本回答。</p>`
      ) +
      (loading
        ? `<div class="wb-empty-card" data-state="loading"><strong>操作账本读取中</strong><small>8001 操作账本尚未返回；未返回前不写「不可读」，也不写「没有操作」。</small></div>`
        : live
          ? rows.length
            ? `<div class="wb-list">${rows.map((op) => `<div class="wb-op"><span class="wb-op-time">${this.opTime(op.created_at)}</span><span>${escapeHtml(op.action)}</span><b>${escapeHtml(op.target || "—")} · ${escapeHtml(this.opStateText(op.state))}${this.opCancelMark(op)}</b><i class="wb-dot wb-dot-${this.opDotCls(op.state)}" aria-hidden="true"></i>${cancel(op)}</div>`).join("")}</div>`
            : `<div class="wb-empty-card"><strong>账本已读取，暂无操作记录</strong><small>只读查询不计入操作账本；真实启停与账号动作会在此留痕。</small></div>`
          : `<div class="wb-empty-card"><strong>操作账本不可读</strong><small>${escapeHtml(this.opsReadFailureText())}（未知，不是「没有操作」）。</small><button type="button" class="wb-btn wb-btn-sm" data-wb-ops-retry="1" title="重新读取 8001 操作账本（只读，不产生操作行）" aria-label="重试读取操作账本">${wbIcon("refresh")}${wbIconHint("重试读取操作账本")}</button></div>`) +
      this.opStatusHtml() +
      `</section>`
    );
  }
  private failuresLaneHtml(all = false) {
    // 账本不可读时不得断言空：原先 `sessionLive` 把「运行快照可读」也算作
    // 账本可读，于是 8001 操作账本读失败时，同一页一边写「操作账本不可读」
    // 一边写「暂无操作记录」/「没有失败的操作」——把未知写成了零。
    const live = this.opsRecent?.state === "live";
    // 读取在途（`undefined`）与读取失败（`gated`）分开：在途时不得先把结论
    // 写成「不可读 / 本次未读到」，只有读取真的失败了才写失败句与实测原因。
    const loading = this.opsRecent === undefined;
    const rows = live ? this.opsRecent!.rows : [];
    const scoped = all ? rows : rows.filter((op) => op.state === "failed");
    // 两个区块共用同一句「不可读」会变成同一解释写两遍（§2.11a.2），
    // 因此按区块各自表述：失败分组说「没有失败的操作」，账本列表说「没有操作」。
    const unreadable = `<div class="wb-empty-card"><strong>${all ? "操作账本不可读" : "失败分组不可读"}</strong><small>${escapeHtml(this.opsReadFailureText())}（未知，不是「${all ? "没有操作" : "没有失败的操作"}」）。</small><button type="button" class="wb-btn wb-btn-sm" data-wb-ops-retry="1" title="重新读取 8001 操作账本（只读，不产生操作行）" aria-label="重试读取操作账本（失败分组）">${wbIcon("refresh")}${wbIconHint("重试读取操作账本（失败分组）")}</button></div>`;
    const loadingCard = `<div class="wb-empty-card" data-state="loading"><strong>${all ? "操作账本读取中" : "失败分组读取中"}</strong><small>8001 操作账本尚未返回；未返回前不写「不可读」，也不写「${all ? "没有操作" : "没有失败的操作"}」。</small></div>`;
    const stateText = (state: string) => this.opStateText(state);
    const dotCls = (state: string) => this.opDotCls(state);
    // R9-R40 task 2: failures aggregate by action — a group chip shows the
    // action and its failure count, clicking unfolds the rows. Honesty
    // stays: whatever failed is listed, nothing is folded away silently.
    const opLine = (op: OpRow) =>
      `<div class="wb-op"><span class="wb-op-time">${this.opTime(op.created_at)}</span><span>${escapeHtml(op.action)} → ${escapeHtml(op.target || "—")}</span><b>${escapeHtml(stateText(op.state))}${op.error?.code ? ` · ${escapeHtml(op.error.code)}` : ""}${op.error?.message ? ` · ${escapeHtml(op.error.message)}` : ""}</b><i class="wb-dot wb-dot-${dotCls(op.state)}" aria-hidden="true"></i></div>`;
    const body = loading
      ? loadingCard
      : !live
        ? unreadable
        : all
          ? scoped.length
            ? `<div class="wb-list">${scoped.map((op) => `<div class="wb-op"><span class="wb-op-time">${this.opTime(op.created_at)}</span><span>${escapeHtml(op.action)}</span><b>${escapeHtml(op.target || "—")} · ${escapeHtml(stateText(op.state))}${this.opCancelMark(op)}</b><i class="wb-dot wb-dot-${dotCls(op.state)}" aria-hidden="true"></i></div>`).join("")}</div>`
            : `<div class="wb-empty-card"><strong>账本已读取，暂无操作记录</strong><small>只读查询不计入操作账本；真实启停与账号动作会在此留痕。</small></div>`
          : scoped.length
            ? (() => {
                const groups = new Map<string, OpRow[]>();
                for (const op of scoped) {
                  const list = groups.get(op.action) ?? [];
                  list.push(op);
                  groups.set(op.action, list);
                }
                return `<div class="wb-fail-groups">${Array.from(
                  groups.entries()
                )
                  .map(([action, ops]) => {
                    const on = this.failuresOpen.has(action);
                    const detail = on
                      ? `<div class="wb-fail-detail">${ops.map(opLine).join("")}</div>`
                      : "";
                    return `<div class="wb-fail-group"><button type="button" class="wb-fail-head${on ? " wb-fail-on" : ""}" data-wb-fail-toggle="${escapeHtml(action)}" aria-expanded="${on}"><i class="wb-dot wb-dot-down" aria-hidden="true"></i><span>${escapeHtml(action)}</span><b>失败 ×${ops.length}</b></button>${detail}</div>`;
                  })
                  .join("")}</div>`;
              })()
            : `<div class="wb-empty-card"><strong>账本已读取，没有失败的操作</strong><small>失败操作来自 8001 操作账本；为空即如实显示空态，不以文档冒充告警。</small></div>`;
    return `<p class="wb-muted lane-note">${this.failuresNote(all)}</p>${body}`;
  }

  /** One service row (R20-07): the only entry point is the real management
   *  panel — no archive-document detour. Registry-driven start/stop buttons;
   *  the usage host has no start/stop by contract. */
  /** Runtime-state badge for one service row (进程层, visible at first
   *  glance instead of hidden in a detail panel). The 8001 host row has no
   *  managed target — it IS the console host, so a gated badge would lie;
   *  it gets a neutral host badge instead. No session → "needs session";
   *  a real state maps to honest text and color; unknown state text passes
   *  through as 未知 rather than a guessed green. */
  private runtimeBadge(s: ServiceRow): { text: string; cls: string } {
    if (!s.targets?.length) return { text: "本控制台宿主", cls: "idle" };
    const state = this.runtime?.[s.targets[0]]?.state;
    if (!state) {
      // A poll in flight ≠ no session: the runtime snapshot itself takes
      // seconds on the production host, so mid-poll shows honest progress
      // instead of pretending the session is missing.
      return this.probing
        ? { text: "运行状态读取中…", cls: "gated" }
        : { text: "运行状态暂缺", cls: "gated" };
    }
    switch (state) {
      case "Running":
        return { text: "运行中", cls: "ok" };
      case "Degraded":
        return { text: "降级", cls: "warn" };
      case "Conflict":
        return { text: "端口冲突", cls: "danger" };
      case "Residual":
        return { text: "受管残留", cls: "warn" };
      case "Stopped":
        return { text: "已停止", cls: "idle" };
      default:
        return { text: state, cls: "idle" };
    }
  }
  /** Registry remains authoritative even when the compact row shows one action.
   *  契约 3.13b：主操作与危险动作**必须在按钮内有文字**（图标只做前缀），
   *  启停因此不改成纯图标；`wb-write-mark` 的写入边界标注同样保留文字。 */
  private servicePowerButton(
    s: ServiceRow,
    kind: "start" | "stop" | "restart"
  ): string {
    const findAction = (name: string) =>
      this.actions?.find((a) => a.action === name);
    const label = kind === "start" ? "启动" : kind === "stop" ? "停止" : "重启";
    const icon =
      kind === "start" ? "play" : kind === "stop" ? "stop" : "restart";
    const row = findAction(
      kind === "start"
        ? "service.start"
        : kind === "stop"
          ? "service.stop"
          : "service.restart"
    );
    if (row && !row.enabled)
      return `<button disabled title="${escapeHtml(row.disabled_reason ?? "动作已停用")}">${wbIcon(icon)}${label}</button>`;
    return `<button data-wb-svc-action="${kind}" data-wb-target="${s.targets?.[0] ?? ""}" title="经 8001 适配层真实${label}（授权目标集；需二次确认）${kind === "restart" ? "；重启会中断该服务在途工作" : ""}">${wbIcon(icon)}${label}</button>`;
  }
  /** One chain in the overview master-detail list (PR11). The dot is the probe
   *  shape, the word is the runtime snapshot word, so neither state leans on hue
   *  alone; an action in flight and the chain's terminal result chip show on the
   *  chain's own row, also when another chain is selected. The title's evidence
   *  suffix is refreshed in place by applyLive (data-wb-base-title). */
  private serviceItemHtml(s: ServiceRow): string {
    const on = this.ovService === s.id;
    const badge = this.runtimeBadge(s);
    const pending = this.svcPendingText(s);
    const chip = this.svcResultChip(`svc${s.id}`);
    const base = `${s.name} · 端口 ${s.port}`;
    const attention = this.attentionWord(s);
    return (
      `<button type="button" class="wb-md-item${attention ? " wb-md-item-attn" : ""}" data-wb-svc-detail="1" data-wb-detail="${s.id}" data-wb-md-item="${s.id}" data-wb-probe="${s.id}" data-wb-base-title="${escapeHtml(base)}" aria-pressed="${on}" aria-controls="wb-md-detail" title="${escapeHtml(`${base} · ${this.probeEvidence(s.id)}`)}">` +
      `<i class="wb-dot wb-dot-${this.dotState(s.id)}" aria-hidden="true"></i>` +
      `<span class="wb-md-name">${escapeHtml(s.name)}</span>` +
      `<span class="wb-md-state"><span class="wb-badge wb-badge-${badge.cls}">${escapeHtml(badge.text)}</span>${attention ? `<span class="wb-badge wb-badge-${attention.tone} wb-md-attn"${attention.title ? ` title="${escapeHtml(attention.title)}"` : ""}>需处理 · ${escapeHtml(attention.word)}</span>` : ""}${pending ? `<span class="wb-md-pending">${escapeHtml(pending)}</span>` : ""}</span>` +
      `<span class="wb-md-port">:${s.port}</span>` +
      (chip ? `<span class="wb-md-result">${chip}</span>` : "") +
      `</button>`
    );
  }
  /** An action on this chain that is still in flight, as one short phrase
   *  (「停止服务 · 进行中」); empty when none. */
  private svcPendingText(s: ServiceRow): string {
    for (const entry of this.feedbackEntries.values()) {
      if (
        entry.state !== "preparing" &&
        entry.state !== "queued" &&
        entry.state !== "running"
      )
        continue;
      if (this.laneKey(entry.action, entry.target) !== `svc${s.id}`) continue;
      // The chain's own button word (「查询登录状态」) reads better than the
      // generic label, which is the bare action id for account reads.
      const own = [s.statusAction, ...(s.manageActions ?? [])].find(
        (a) => a?.action === entry.action
      );
      return `${own?.label ?? entry.label} · 进行中`;
    }
    return "";
  }
  /** Per-service management detail (R9-R25): runtime snapshot, probe
   *  evidence, account-status + manage actions, and real start/stop. The
   *  four state layers (process/health/account/session) are shown layered,
   *  not merged. */
  private serviceDetailHtml(
    id: number,
    pane: "overview" | "workspace"
  ): string {
    const s = serviceById(id);
    if (!s || s.id === CONSOLE_HOST_ID) return "";
    const runtime = this.runtime?.[s.targets?.[0] ?? ""];
    const runtimeRead =
      this.opsRecent?.state === "live" || Boolean(this.runtime);
    // 认证状态与检查时点也在这张详情里：未读取 / 失败 / 上一次真实值分开写，
    // 未知不折成「未认证」。主动作（只读认证查询）在最前，次动作与需人工
    // 登录的动作随后（登录动作先二次确认）。
    const authHtml = this.accountAuthHtml(s);
    const accountHtml = `<div class="wb-svc-ops">${this.accountActionsHtml(s)}</div>`;
    const powerHtml = (s.targets ?? []).length
      ? `<div class="wb-svc-ops wb-svc-power">${this.servicePowerButton(s, "start")}${this.servicePowerButton(s, "restart")}${this.servicePowerButton(s, "stop")}</div>`
      : `<p class="wb-muted">本服务为控制台宿主自身，不提供启停入口（既有契约）。</p>`;
    const logSource = this.serviceLogSource(id);
    const logHtml = logSource
      ? `<div class="wb-svc-ops"><button data-log-open="${escapeHtml(logSource)}" title="直接查看该网关运行日志详情（服务端脱敏、有限尾读，不重启任何服务）">${wbIcon("logs")}运行日志详情</button></div>`
      : "";
    const layers = `<p class="wb-sr-only">进程、连通性和认证分别显示；进程运行不代表认证有效。</p>`;
    // Overview pane (PR11): the panel's head, one list of readings (probe
    // evidence, runtime, PID, note, account status) and one action bar. A list
    // keeps each reading on one line at the pane's width, where the former four
    // tiles wrapped the probe evidence into four lines (2026-10-31).
    // Runtime readings that are not read yet stay 「读取中或暂缺」, never a default.
    const badge = this.runtimeBadge(s);
    const pids =
      runtimeRead && runtime
        ? runtime.process_ids.length
          ? runtime.process_ids.join(", ")
          : "—"
        : s.targets?.length
          ? "读取中或暂缺"
          : "—";
    const runWord =
      runtimeRead && runtime
        ? `${runtime.state} · ${runtime.detail}`
        : s.targets?.length
          ? "读取中或暂缺"
          : "控制台宿主，无运行快照";
    // Inside the gateway workspace the detail sits under the workspace's own head
    // (name, port, state), so it starts at its readings.
    const head =
      pane === "workspace"
        ? ""
        : `<div class="wb-md-head"><h4>${escapeHtml(s.name)}</h4><span class="wb-badge wb-badge-${badge.cls}">${escapeHtml(badge.text)}</span><span class="wb-port">端口 ${s.port}</span></div>`;
    return (
      `<div class="wb-md-detail"${pane === "workspace" ? "" : ` id="wb-md-detail"`} role="region" aria-label="${escapeHtml(s.name)} · 管理详情">` +
      head +
      `<div class="wb-md-facts">` +
      `<div class="wb-kv"><span>探针证据</span><b>${escapeHtml(this.probeEvidence(s.id))}</b></div>` +
      `<div class="wb-kv"><span>运行状态</span><b>${escapeHtml(runWord)}</b></div>` +
      `<div class="wb-kv"><span>进程 PID</span><b>${escapeHtml(pids)}</b></div>` +
      `<div class="wb-kv"><span>说明</span><b>${escapeHtml(s.note)}</b></div>` +
      authHtml +
      `</div>` +
      `<div class="wb-md-actions">${accountHtml}${logHtml}${powerHtml}</div>` +
      layers +
      `</div>`
    );
  }
  /** Gateway id → log-source id for the service-lane entry (R9-R38). */
  private serviceLogSource(id: number): string {
    const map: Record<number, string> = {
      0: "chat2api",
      1: "console",
      2: "qoder_global",
      3: "gemini",
      4: "qoder_cn",
    };
    return map[id] ?? "";
  }
  private opTime(created: number | string): string {
    const v = Number(created);
    if (!Number.isFinite(v) || v <= 0) return "—";
    return new Date(v * 1000).toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  private opStatusHtml(): string {
    return this.opStatus
      ? `<p class="wb-op-status" data-state="${this.opStatus.state}">${escapeHtml(this.opStatus.text)}</p>`
      : "";
  }
  /** Legacy single-op lane summary repaint, kept for compatibility: now a
   *  full notices+panel pass (patching .wb-op-status in place would clobber
   *  the dot+label shape the operation-area summary renders). */
  private renderOpStatus() {
    this.renderNotices();
    this.renderPanel();
  }
  /** Operation-area summary (R9-R37 A37-2): the most recent feedback entry
   *  among the given actions, rendered next to the action row that started
   *  it. Returns "" when no matching entry exists. */
  private opAreaSummaryHtml(actions: string[]): string {
    let latest: FeedbackEntry | undefined;
    for (const entry of this.feedbackEntries.values()) {
      if (!actions.includes(entry.action)) continue;
      if (!latest || entry.updatedAt > latest.updatedAt) latest = entry;
    }
    if (!latest) return "";
    const state =
      latest.state === "succeeded"
        ? "ok"
        : latest.state === "failed"
          ? "failed"
          : "pending";
    return `<p class="wb-op-status" data-state="${state}"><i class="wb-dot wb-dot-${stateDot(latest.state)}" aria-hidden="true"></i>${escapeHtml(latest.label)} · ${escapeHtml(stateText(latest.state))}${latest.detail ? ` · ${escapeHtml(latest.detail)}` : ""}</p>`;
  }
  /** Patch one feedback entry in place and repaint the notice pool only —
   *  never the panel (renderNotices must not call renderPanel). */
  private fbUpdate(
    key: string,
    state: FeedbackState,
    text: string,
    pinned = false
  ) {
    const entry = this.feedbackEntries.get(key);
    if (!entry) return;
    entry.state = state;
    entry.text = text;
    entry.pinned = pinned;
    entry.updatedAt = Date.now();
    this.fbMirror(entry);
    this.renderNotices();
  }
  /** Re-bind a client-local feedback entry to the server operation id. */
  private fbRekey(oldKey: string, newId: string) {
    const entry = this.feedbackEntries.get(oldKey);
    if (!entry || oldKey === newId) return;
    this.feedbackEntries.delete(oldKey);
    entry.key = newId;
    this.feedbackEntries.set(newId, entry);
  }
  /** Terminal transition: patch the entry (state/text/detail), repaint the
   *  notice pool AND the panel (model.* lane data was stored just before). */
  private fbFinish(
    key: string,
    state: FeedbackState,
    text: string,
    pinned = false,
    detail = "",
    repaint = true
  ) {
    const entry = this.feedbackEntries.get(key);
    if (!entry) return;
    entry.state = state;
    entry.text = text;
    entry.pinned = pinned;
    entry.detail = detail;
    entry.updatedAt = Date.now();
    this.fbMirror(entry);
    this.renderNotices();
    // R11-9: terminal states of a per-row speedtest skip the full-lane
    // repaint — the caller patches the one affected cell (and the lane
    // chip) in place, so search filters, scroll position and the other
    // rows never flicker. Every other terminal state still repaints.
    if (repaint) this.renderPanel();
  }
  /** Keep the legacy lane-summary slot (opStatus) mirrored from one entry so
   *  the model/share lane summaries keep working alongside the notice pool.
   *  R9-R46: service.* no longer mirrors — its terminal summary now lives on
   *  the owning service row's .wb-svc-result chip; mirroring both was the
   *  "row chip + page-bottom green box" double track. share.* and model.*
   *  mirroring stays unchanged for now. */
  private fbMirror(entry: FeedbackEntry) {
    if (entry.action.startsWith("service.")) return;
    const head = `${entry.label} → ${entry.target || "—"}`;
    const line = entry.text.startsWith(entry.label)
      ? entry.text
      : `${head} ${entry.text}`;
    this.opStatus = {
      state:
        entry.state === "succeeded"
          ? "ok"
          : entry.state === "failed"
            ? "failed"
            : "pending",
      text: entry.detail ? `${line}。${entry.detail}` : line,
    };
  }
  /** Global notice pool + system banner (R9-R37 A37): at most three cards,
   *  newest first, overflow as a compact chip; 8001-unreachable class
   *  messages go to the top banner. Never calls renderPanel (no recursion:
   *  renderPanel itself ends with renderNotices). */
  private renderNotices() {
    // The banner lives in the sticky top region the shell markup declares
    // (above the head, never painted over it).
    const banner = this.root.querySelector<HTMLElement>(".wb-banner");
    // R11-F1 (Plan-Revision 11): the notice pool lives on the VIEWPORT layer
    // (document.body + position:fixed) so scrolling wb-main, switching lanes or
    // any transform-scaling ancestor never moves an in-flight/failed notice out
    // of view. The workbench click listener is document-level, so the pool's
    // close/action buttons keep working from here.
    let pool = document.querySelector<HTMLElement>(
      ".wb-notices[data-wb-notice-pool]"
    );
    if (!pool) {
      pool = document.createElement("div");
      pool.className = "wb-notices";
      pool.setAttribute("data-wb-notice-pool", "1");
      pool.setAttribute("aria-live", "polite");
      document.body.append(pool);
    }
    if (!banner) return;
    // Banner: the measured cause of the current episode, dismissed until the
    // console reaches any reachable state again. The sentence names what was
    // actually measured (`summaryCause`, set at every failure branch above):
    // a 5xx / unparsable / mis-shaped body means 8001 DID answer, so the
    // banner must not claim it was unreachable — the lane body writes the same
    // fact with its HTTP status, and the two must agree.
    const bannerDue =
      this.summaryState === "error" || this.summaryState === "unauthenticated";
    if (!bannerDue) this.bannerDismissed = false;
    const showBanner = bannerDue && !this.bannerDismissed;
    banner.hidden = !showBanner;
    banner.innerHTML = showBanner
      ? `<span>⚠ ${escapeHtml(this.summaryCause || "费用聚合读取失败")}——服务/操作数据暂不可用，恢复后自动接入</span><button type="button" data-wb-banner-dismiss="1" title="隐藏此提示">知道了</button>`
      : "";
    // 维护者 2026-09-28（Plan-Revision 9 逐页收纳 · 日志 / 操作页）：瞬时通知池最多 2 条
    // （原先 3 条，第 3 条会盖到页头时点与筛选行上）；多出来的只写一行计数，
    // 不静默丢弃——「还有 N 项」本身就是常驻事实。
    const visible = [...this.feedbackEntries.values()]
      .filter(isVisibleState)
      .sort((a, b) => b.updatedAt - a.updatedAt);
    const cards = visible
      .slice(0, 2)
      .map((entry) => {
        const closable =
          entry.pinned ||
          entry.state === "failed" ||
          entry.state === "unconfirmed";
        return (
          `<div class="wb-notice"><i class="wb-dot wb-dot-${stateDot(entry.state)}" aria-hidden="true"></i>` +
          `<div class="wb-notice-body"><div class="wb-notice-head"><b>${escapeHtml(entry.label)}</b>` +
          `<span class="wb-notice-target">${escapeHtml(entry.target || "—")} · ${escapeHtml(stateText(entry.state))}</span></div>` +
          `<div class="wb-notice-text">${escapeHtml(entry.text)}</div>` +
          (entry.detail
            ? `<div class="wb-notice-detail">${escapeHtml(entry.detail)}</div>`
            : "") +
          `</div><div class="wb-notice-side"><small class="wb-notice-time">${escapeHtml(this.feedbackElapsed(entry.startedAt))}</small>` +
          (closable
            ? `<button type="button" class="wb-notice-close" data-wb-notice-dismiss="${escapeHtml(entry.key)}" aria-label="收起这条通知" title="收起">×</button>`
            : "") +
          `</div></div>`
        );
      })
      .join("");
    pool.innerHTML =
      visible.length > 2
        ? `${cards}<div class="wb-notices-compact">另有 ${visible.length - 2} 项进行中 / 待处理（瞬时通知最多 2 条）</div>`
        : cards;
    // 2026-09-29 redesign: the pool sits at the viewport's bottom-right corner
    // (workbench.css §13), clear of the sticky head (page name, run verdict,
    // read stamp) and of the filter rows at the top of each lane, so the head no
    // longer reserves the pool's width. A pinned failure notice there can cover
    // the lower-right of a long page until it is closed with its own × button.
  }
  /** Elapsed time since an entry started, in minutes/seconds. */
  private feedbackElapsed(startedAt: number): string {
    const secs = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
    if (secs >= 3600)
      return `${Math.floor(secs / 3600)} 时 ${Math.floor((secs % 3600) / 60)} 分`;
    if (secs >= 60) return `${Math.floor(secs / 60)} 分 ${secs % 60} 秒`;
    return `${secs} 秒`;
  }
  /** Two-step arm: first click arms the button (visual confirm), a second
   *  click on the SAME button executes. Any other click disarms. */
  private armConfirm(
    button: HTMLElement,
    action: "start" | "stop" | "restart"
  ) {
    this.armConfirmLabel(
      button,
      action === "start"
        ? "确认启动？"
        : action === "stop"
          ? "确认停止？"
          : "确认重启？"
    );
  }
  private armConfirmLabel(button: HTMLElement, armedText: string) {
    this.clearConfirm();
    this.confirmBtn = button;
    const action =
      button.dataset.wbModelAction ??
      button.dataset.wbSvcAction ??
      button.dataset.wbAcctAction ??
      button.dataset.wbMsAction;
    if (action) {
      this.armedConfirm = {
        sel: `[data-wb-model-action="${action}"], [data-wb-svc-action="${action}"], [data-wb-acct-action="${action}"], [data-wb-ms-action="${action}"]`,
        text: armedText,
        key: confirmKey(button),
      };
    }
    button.classList.add("wb-confirm");
    button.dataset.wbBaseLabel = button.textContent ?? "";
    button.textContent = armedText;
  }
  /** The second activation executes only on the button that was armed AND still
   *  confirms the same action + target (see rearmConfirm). */
  private isArmed(button: HTMLElement): boolean {
    return (
      this.confirmBtn === button &&
      this.armedConfirm?.key === confirmKey(button)
    );
  }
  private clearConfirm() {
    const armed = this.confirmBtn;
    this.armedConfirm = null;
    if (armed) {
      armed.classList.remove("wb-confirm");
      armed.textContent = armed.dataset.wbBaseLabel ?? armed.textContent;
      this.confirmBtn = undefined;
    }
  }
  /** Share-lane confirm, cleared by identity: the armed node gets its own label
   *  back (when the patch has not already restored it) and nothing stays armed. */
  private disarmShare() {
    const armed = this.shareConfirm;
    this.shareConfirm = undefined;
    this.shareArmed = null;
    if (armed?.isConnected && armed.classList.contains("wb-confirm")) {
      armed.classList.remove("wb-confirm");
      armed.textContent = armed.dataset.wbBaseLabel ?? armed.textContent;
      delete armed.dataset.wbBaseLabel;
    }
  }
  /** After a repaint: re-arm the button that confirms the same action / key op,
   *  or drop the arm when no enabled one is left (e.g. it is now 执行中). */
  private rearmShareConfirm() {
    const armed = this.shareArmed;
    if (!armed) return;
    const button = [
      ...this.root.querySelectorAll<HTMLButtonElement>(
        "button[data-wb-share-action], button[data-wb-share-key], button[data-wb-share-public]"
      ),
    ].find((b) => shareConfirmKey(b) === armed.key && !b.disabled);
    if (!button) {
      this.shareConfirm = undefined;
      this.shareArmed = null;
      return;
    }
    this.shareConfirm = button;
    if (!button.classList.contains("wb-confirm")) {
      button.dataset.wbBaseLabel = button.textContent ?? "";
      button.classList.add("wb-confirm");
      button.textContent = armed.text;
    }
  }
  /** Re-apply the armed two-step confirm after a repaint. The in-place panel
   *  patch keeps the armed node connected but resets its label/class (and the
   *  base-label dataset) from the fresh HTML — identity survives, visuals do
   *  not; a wholesale rebuild replaces the node entirely. Re-arm whichever
   *  node currently holds the action: identity is what the second click
   *  checks, the label is what the maintainer reads. */
  private rearmConfirm() {
    const armed = this.armedConfirm;
    if (!armed) return;
    // The in-place patch can morph the armed node into ANOTHER target's button
    // (the overview detail pane is one skeleton for every chain), so a connected
    // node only keeps the arm while it still confirms the same action + target;
    // otherwise the arm moves to that exact button or is dropped — never to a
    // chain nobody confirmed.
    const keep =
      this.confirmBtn?.isConnected && confirmKey(this.confirmBtn) === armed.key
        ? this.confirmBtn
        : undefined;
    const button =
      keep ??
      [...this.root.querySelectorAll<HTMLElement>(armed.sel)].find(
        (b) => confirmKey(b) === armed.key
      );
    if (!button) {
      this.armedConfirm = null;
      this.confirmBtn = undefined;
      return;
    }
    this.confirmBtn = button;
    button.dataset.wbBaseLabel = button.textContent ?? "";
    button.classList.add("wb-confirm");
    button.textContent = armed.text;
  }
  /** Submit one real adapter action through the 8001 layer. The cookie
   *  session carries the write; X-Requested-With is the CSRF marker the
   *  route layer requires on session-authenticated writes. R9-R37: every
   *  submit owns one feedback entry (client-local key, rebound to the
   *  server operation id once accepted) rendered in the notice pool. */
  /** Clear the speedtest busy marker for an operation that reached any
   *  terminal state; the pending param mapping is consumed exactly once. */
  private speedSettle(opId: string) {
    const ctx = this.speedPending.get(opId);
    if (!ctx) return;
    this.speedPending.delete(opId);
    if (this.speedBusy === `${ctx.source}|${ctx.model}`) this.speedBusy = null;
  }
  /** R11-9 + P2 (2026-09-24): the ONE exit a polled speedtest may take.
   *  The mapping is read BEFORE speedSettle consumes it (settling first
   *  left it empty and forced the full repaint this path exists to avoid),
   *  and EVERY exit calls it — terminal states and the two non-terminal
   *  ones alike: a transient status failure used to leave speedBusy set,
   *  which disabled that row's button AND every other row's (speedBusy is a
   *  single global marker) until a page reload. */
  private settleSpeedPoll(actionName: string, opId: string): boolean {
    if (actionName !== "model.speedtest") return false;
    const ctx = this.speedPending.get(opId);
    if (!ctx) return false;
    this.speedSettle(opId);
    this.patchSpeedCell(ctx.source, ctx.model);
    return true;
  }
  private async submitAction(
    actionName: string,
    target: string,
    params?: Record<string, unknown>
  ) {
    const settingsEdit =
      actionName === "model.settings_save" && params
        ? {
            key: `${params.source}|${params.model_id}`,
            revision:
              this.msEditRevision.get(`${params.source}|${params.model_id}`) ??
              0,
          }
        : undefined;
    const label = actionLabel(actionName);
    const key = `local-${Date.now()}-${++this.feedbackSeq}`;
    const startedAt = Date.now();
    const entry: FeedbackEntry = {
      key,
      action: actionName,
      label,
      target,
      state: "preparing",
      text: `${label} → ${target || "—"} 准备提交…`,
      detail: "",
      startedAt,
      updatedAt: startedAt,
      pinned: false,
    };
    this.feedbackEntries.set(key, entry);
    if (settingsEdit) this.patchModelSaveControls(settingsEdit.key);
    if (actionName === "model.sync_apply") {
      const confirm = this.root.querySelector<HTMLButtonElement>(
        "[data-wb-sync-publish]"
      );
      if (confirm) confirm.disabled = true;
    }
    // R9-R46: service.* no longer feeds the legacy page-bottom op-status line
    // (its terminal summary lives on the owning service row's chip).
    if (!actionName.startsWith("service.")) {
      this.opStatus = { state: "pending", text: entry.text };
    }
    // R9-R40 task 4: the block's next action clears its persisted summary.
    delete this.laneSummaries[this.laneKey(actionName, target)];
    this.renderNotices();
    // PR11 Q17: the overview list marks a chain with an action in flight even
    // when another chain is selected, so the list repaints once at submit.
    if (this.lane === 0 && this.laneKey(actionName, target).startsWith("svc"))
      this.renderPanel();
    // A15-3: the single-layer buttons lock while any share operation is in
    // flight (no second submit from this page).
    if (this.lane === 6 && actionName.startsWith("share.")) this.renderPanel();
    try {
      const response = await fetch("/gw/usagesrv/api/console/operations", {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify({
          action: actionName,
          target,
          ...(params ? { params } : {}),
        }),
      });
      const data = (await response.json().catch(() => null)) as {
        operation?: OpRow;
        error?: string;
        reason?: string;
        detail?: string;
      } | null;
      if (!response.ok) {
        // usagesrv only reads `reason` (an OperationUnavailable attribute),
        // while a param rejection is an OperationError whose text lives on
        // `message` — the response therefore carries the code in `error` and the
        // backend's OWN safety-filtered text in `detail` (paths, long tokens and
        // absolute paths are dropped server-side). `reason` still wins when the
        // backend sends it; with no controlled field the wording stays the bare
        // code exactly as before.
        const code = data?.error ?? "";
        const reason =
          data?.reason ||
          this.rejectDetailText(code, data?.detail) ||
          code ||
          `HTTP ${response.status}`;
        // 账号页只读查询被拒：把实测原因（无受控字段时即实测 HTTP 状态）
        // 记成失败结论，`accountAuthHtml` 才能写出「读取失败」而不是
        // 「未读取」或「未认证」。
        if (REMEMBER_READ_RESULT.includes(actionName)) {
          this.actionResultFailures.set(actionName, `被拒绝：${reason}`);
        }
        // P2 (2026-09-24): a speedtest whose SUBMIT was rejected never
        // starts a poll, so no terminal exit would ever release its busy
        // marker — the row's button (and, via the global marker, every
        // other row's) stayed disabled until a page reload.
        this.releaseSpeedBusy(actionName);
        this.settleQoderGlobalQuota(actionName, undefined, `被拒绝：${reason}`);
        this.setLaneSummary(
          this.laneKey(actionName, target),
          `${label} → ${target || "—"} 被拒绝：${reason}`,
          "failed"
        );
        this.fbFinish(key, "failed", `被拒绝：${reason}`, true);
        return;
      }
      const op = data?.operation;
      if (!op?.id) {
        this.releaseSpeedBusy(actionName);
        this.settleQoderGlobalQuota(
          actionName,
          undefined,
          "响应缺少操作标识，结果未知"
        );
        this.fbFinish(key, "failed", "响应缺少操作标识，结果未知", true);
        return;
      }
      this.fbRekey(key, op.id);
      // Speedtest results don't echo their source/model — remember the
      // submitted params so the terminal result can land on the right row.
      if (actionName === "model.speedtest" && params) {
        const source = typeof params.source === "string" ? params.source : "";
        const model = typeof params.model === "string" ? params.model : "";
        if (source && model) this.speedPending.set(op.id, { source, model });
      }
      // 预览提交时范围快照（维护者 2026-09-29）：预览结果落定时才知道它按
      // 哪一组范围算出来——按 op id 记这次提交的范围，双击/并发预览不会把
      // 后一次提交的范围写进前一次的结果。
      if (actionName === "model.sync_preview" && params) {
        this.previewScopePending.set(op.id, this.snapshotScope(params));
      }
      this.fbUpdate(op.id, "queued", "已排队");
      await this.pollOperation(op.id, actionName, target, settingsEdit);
    } catch {
      // P2 (2026-09-24): the transport-level catch is a submit failure
      // too — the operation may never have been accepted, so the speedtest
      // busy marker must be released here as well.
      this.releaseSpeedBusy(actionName);
      this.settleQoderGlobalQuota(
        actionName,
        undefined,
        "连接失败——可能已被受理，不自动重发"
      );
      this.setLaneSummary(
        this.laneKey(actionName, target),
        `${label} → ${target || "—"} 结果待确认：连接失败——可能已被受理，不自动重发`,
        "unconfirmed"
      );
      this.fbFinish(
        key,
        "unconfirmed",
        "结果待确认",
        true,
        "连接失败——可能已被受理，不自动重发；稍后核对操作账本"
      );
    }
  }
  /** Turn the submit rejection's controlled `detail` field into an actionable
   *  Chinese sentence. The backend only attaches this field to a rejection it
   *  could describe safely, and only after dropping anything that looks like a
   *  path, a drive letter or a 24+ char token — so the text is a
   *  backend-vetted, non-sensitive fragment, never a raw exception. The code
   *  list is NOT fixed to the four param codes: the candidate preview's own
   *  read-only refusal (`candidate_preview_read_only`, vite.config.ts:66) sends
   *  an actionable Chinese detail, and the old `code !== "invalid_params"`
   *  gate dropped it so the user saw the bare enum. The frontend still never
   *  passes text through blind: it re-runs the same shape gate below and falls
   *  back to the caller's bare code when the text looks path-, drive- or
   *  token-shaped. Known parameter names are translated (a rejected scope shape
   *  used to read as the bare `invalid_params`), and an absent field returns
   *  "" so the caller keeps today's wording. */
  private rejectDetailText(code: string, detail?: string): string {
    if (typeof detail !== "string" || !detail) return "";
    const text = detail.replace(/\s+/g, " ").trim().slice(0, 200);
    if (!text) return "";
    // The frontend's own shape gate, narrowed to the shapes that carry a
    // secret: a path separator, a drive letter or a 24+ char UNBROKEN
    // alphanumeric run (a key, a hash). A dotted/underscored identifier is
    // NOT rejected: the read-only refusal's own detail names
    // `console_adapters.build_registry()`, and the old `[A-Za-z0-9_.-]{24,}`
    // gate dropped that backend-vetted sentence — the exact regression this
    // change exists to fix. Anything the gate rejects falls back to the bare
    // code, never to the raw text.
    if (/[\\/]|[A-Za-z]:|[A-Za-z0-9]{24,}/.test(text)) return "";
    if (code !== "invalid_params") return text;
    const names: Record<string, string> = {
      sources: "来源",
      consumers: "客户端",
      models: "模型",
      probe: "探测开关",
      key: "模型键",
      source: "来源",
      model: "模型",
      max_tokens: "最大 token",
      limit: "条数",
      action: "动作",
      target: "目标",
    };
    const name = (raw: string) => names[raw] ?? raw;
    const must = /^([A-Za-z_]+) must be a string$/.exec(text);
    if (must) {
      const key = must[1]!;
      return key === "sources" || key === "consumers"
        ? `${name(key)}参数格式不符（需为 JSON 数组字符串）`
        : `${name(key)}参数类型不符（需为字符串）`;
    }
    const shape: [RegExp, string][] = [
      [/^([A-Za-z_]+) must be an object$/, "格式不符（需为对象）"],
      [/^([A-Za-z_]+) must be a bool$/, "类型不符（需为布尔值）"],
      [/^([A-Za-z_]+) must be an int$/, "类型不符（需为整数）"],
      [/^([A-Za-z_]+) too long$/, "过长"],
      [/^([A-Za-z_]+) out of range$/, "超出范围"],
    ];
    for (const [pattern, tail] of shape) {
      const hit = pattern.exec(text);
      if (hit) return `${name(hit[1]!)}参数${tail}`;
    }
    if (text === "params must be an object") return "参数必须是对象";
    if (text === "params must be finite JSON") return "参数必须是有限 JSON";
    if (text === "params too large") return "参数体积超出上限";
    const unknown = /^unknown param\(s\): (.+)$/.exec(text);
    if (unknown) return `无法识别的参数：${unknown[1]}`;
    return `参数被拒绝（${text}）`;
  }
  /** P2 (2026-09-24): release a speedtest's busy marker when its SUBMIT
   *  failed — no poll was ever started, so no terminal exit would. The
   *  marker is a single global string, so leaving it set disabled that
   *  row's button AND every other row's until a page reload. */
  private releaseSpeedBusy(actionName: string) {
    if (actionName !== "model.speedtest") return;
    this.speedBusy = null;
  }
  /** Poll one accepted operation to a terminal state (60s deadline; a bounded
   *  manual public retry gets 630s = server CYCLE_DEADLINE_S 570 + 60s margin,
   *  so a recovery still running is not misread as unconfirmed). Live
   *  states only patch the notice pool; terminal states repaint the panel
   *  too (fbFinish). Transport-level uncertainty ends as unconfirmed:
   *  unknown ≠ failed, the 8001 ledger holds the real outcome. */
  private async pollOperation(
    opId: string,
    actionName: string,
    target: string,
    settingsEdit?: { key: string; revision: number }
  ) {
    this.opPollAbort = false;
    const deadline =
      Date.now() + (actionName === "share.public_retry" ? 630_000 : 60_000);
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 1200));
      if (this.opPollAbort) return;
      try {
        const response = await fetch(
          `/gw/usagesrv/api/console/operations/${opId}`,
          { credentials: "include" }
        );
        if (!response.ok) {
          this.settleSpeedPoll(actionName, opId);
          this.settleQoderGlobalQuota(
            actionName,
            undefined,
            `状态查询失败（HTTP ${response.status}）`
          );
          // 账号页只读查询：状态查询本身失败也要留下实测 HTTP 状态，
          // 否则失败会被写成「未读取」。
          if (REMEMBER_READ_RESULT.includes(actionName)) {
            this.actionResultFailures.set(
              actionName,
              `状态查询失败（HTTP ${response.status}）`
            );
          }
          this.setLaneSummary(
            this.laneKey(actionName, target),
            `${actionLabel(actionName)} → ${target || "—"} 状态查询失败（HTTP ${response.status}），结果未知，请稍后核对操作账本`,
            "unconfirmed"
          );
          this.fbFinish(
            opId,
            "unconfirmed",
            `${actionLabel(actionName)} → ${target || "—"} 状态查询失败（HTTP ${response.status}），结果未知，请稍后核对操作账本`,
            true
          );
          return;
        }
        const data = (await response.json().catch(() => null)) as {
          operation?: OpRow;
        } | null;
        const op = data?.operation;
        if (!op) {
          this.settleSpeedPoll(actionName, opId);
          this.settleQoderGlobalQuota(
            actionName,
            undefined,
            "操作记录查询失败，结果未知"
          );
          this.setLaneSummary(
            this.laneKey(actionName, target),
            `${actionLabel(actionName)} → ${target || "—"} 操作记录查询失败，结果未知，请稍后核对操作账本`,
            "unconfirmed"
          );
          this.fbFinish(
            opId,
            "unconfirmed",
            `${actionLabel(actionName)} → ${target || "—"} 操作记录查询失败，结果未知，请稍后核对操作账本`,
            true
          );
          return;
        }
        if (op.state === "queued" || op.state === "running") {
          // A15-3: a single-layer action shows its operation id and the latest
          // stage the backend wrote (drain / restart / resume), and the share
          // lane repaints when it changes so the button area follows along.
          const stage = SHARE_LAYER_ACTIONS.includes(actionName)
            ? ([...(op.steps ?? [])]
                .reverse()
                .find((s) => typeof s.note === "string" && s.note)?.note ?? "")
            : "";
          const text =
            (op.state === "queued" ? "排队中…" : "执行中…") +
            (SHARE_LAYER_ACTIONS.includes(actionName)
              ? `（操作 ${op.id.slice(0, 12)}${stage ? `；${stage.slice(0, 120)}` : ""}）`
              : "");
          const changed = this.feedbackEntries.get(opId)?.text !== text;
          this.fbUpdate(
            opId,
            op.state === "queued" ? "queued" : "running",
            text
          );
          if (
            changed &&
            SHARE_LAYER_ACTIONS.includes(actionName) &&
            this.lane === 6
          )
            this.renderPanel();
          continue;
        }
        if (op.state === "succeeded") {
          // 账号页的只读查询（认证状态 / 额度桶）与 model.* 一样记忆：
          // `accountAuthHtml` 读的是这张表，没有写入者时「已认证 / 未认证 /
          // 读于」永远不可达（同会话 qoderglobal.quota 能落地，通道本可用）。
          if (REMEMBER_READ_RESULT.includes(actionName)) {
            this.actionResults[actionName] = op.result ?? {};
            // 本次读取成功：清掉上一次的失败记忆，否则「读取失败」会一直挂着。
            this.actionResultFailures.delete(actionName);
          }
          if (actionName === "share.repair_limits") {
            const result = op.result;
            const security = result?.security_gate as
              Record<string, unknown> | undefined;
            const evidence = security?.evidence as
              Record<string, unknown> | undefined;
            if (
              result?.repaired === true &&
              result.public_closed === true &&
              result.gateway_stopped === true &&
              security?.state === "ok" &&
              security.verdict === "confirmed" &&
              evidence?.verdict === true
            ) {
              const previous = this.actionResults["share.diagnostics"] ?? {};
              const oldLayers =
                previous.layers && typeof previous.layers === "object"
                  ? previous.layers
                  : {};
              const checkedAt =
                typeof result.checked_at === "number"
                  ? result.checked_at
                  : security.checked_at;
              const closed = (state: string) => ({
                state,
                verdict: "confirmed",
                source: "share.repair_limits",
                checked_at: checkedAt,
                stale: false,
                evidence: { timestamp_basis: "action_result" },
              });
              this.actionResults["share.diagnostics"] = {
                ...previous,
                repair_checked_at: checkedAt,
                desired_stop_in_force: true,
                layers: {
                  ...oldLayers,
                  security_gate: security,
                  funnel: closed("off"),
                  share_gateway: closed("stopped"),
                },
                note: "隔离修复只更新安全条件、公网和入口进程；其他层保留各自检查时间。",
              };
              this.actionResultFailures.delete("share.diagnostics");
            } else {
              this.actionResultFailures.set(
                "share.diagnostics",
                "隔离修复结果缺少完整核验证据，请重新检查"
              );
            }
          }
          // R9-R31: model.* results feed the model lane (catalog tables, sync
          // diffs) — remembered per action instead of only shown once.
          if (actionName.startsWith("model.")) {
            this.actionResults[actionName] = op.result ?? {};
            if (actionName === "model.context_overrides") {
              for (const [key, revision] of this.msEditsAwaitingReadback) {
                if (this.msContextState(key).state !== "ok") continue;
                if ((this.msEditRevision.get(key) ?? 0) === revision) {
                  this.msAlias.delete(key);
                  this.msCtx.delete(key);
                  this.msCtxTokens.delete(key);
                  this.msEffort.delete(key);
                }
                this.msEditsAwaitingReadback.delete(key);
              }
            }
            // A freshly computed preview reflects the post-apply state — the
            // staleness marker left by the last apply no longer applies.
            // 提交时范围快照：这次预览结果按**提交那一刻**的范围算出来
            // （`previewScopePending` 按 op id 取回），面板据此写「本次预览
            // 范围」并在勾选变化时标过期；取不到就写「未知」而不是猜当前勾选。
            if (actionName === "model.sync_preview") {
              this.previewStale = false;
              this.previewScopeParams =
                this.previewScopePending.get(op.id) ?? null;
              this.previewScopePending.delete(op.id);
              // §11.3（2026-10-10）统一同步窗口：记录这次预览绑定的范围签名 +
              // selection + preview_token；范围 / 草稿 / 目录或客户端输入变化后签名
              // 不同，窗口不再展示「确认同步」，也不复用旧 token。
              const token =
                typeof op.result?.preview_token === "string"
                  ? op.result.preview_token
                  : "";
              const selectionRaw = op.result?.selection;
              const selection: Record<string, number> = {};
              if (
                selectionRaw &&
                typeof selectionRaw === "object" &&
                !Array.isArray(selectionRaw)
              ) {
                for (const [key, version] of Object.entries(
                  selectionRaw as Record<string, unknown>
                )) {
                  if (
                    typeof version === "number" &&
                    Number.isSafeInteger(version) &&
                    version > 0
                  )
                    selection[key] = version;
                }
              }
              const scope = this.previewScopeParams;
              this.syncBind =
                token && scope
                  ? {
                      scopeSig: this.scopeSignature(scope),
                      selectionSig: this.selectionSignature(selection),
                      token,
                      selection,
                    }
                  : undefined;
            }
            // L-C: per-model speedtest memory (see speedPending mapping).
            if (actionName === "model.speedtest") {
              const ctxRow = this.speedPending.get(op.id);
              if (ctxRow) {
                this.speedResults.set(`${ctxRow.source}|${ctxRow.model}`, {
                  ...(op.result ?? {}),
                  at: Date.now(),
                });
              }
            }
            // L-C auto readback: an apply terminal state invalidates the old
            // preview and re-reads the pending overview read-only — never a
            // re-submitted write, never a charged probe.
            if (actionName === "model.sync_apply") {
              this.previewStale = true;
              this.pendingReadbackFailed = false;
              this.keepScroll = true;
              // §11.3：发布已消费（或被拒绝）这次预览绑定的 preview_token；窗口
              // 需重新「确认范围并生成预览」后才能再次发布（不复用旧 token）。
              this.syncBind = undefined;
              // 发布终态后草稿已转已应用（或被延期保留），设置读面必须重读：
              // 「草稿 · 已应用 · 客户端读回」与窗口里的待同步草稿都按读面回答。
              this.msSettingsReadSubmit(this.modelPick);
              // 自动回读：这一路失败必须留下回读失败标记（fetchModelPending
              // 的 afterApply），否则「操作已完成、最新状态读取失败」永远不可达。
              // 范围：应用按**提交时快照**回读（同范围）；pin 无范围参数，
              // 按全局概览读（null）。
              const readbackScope =
                actionName === "model.sync_apply"
                  ? this.applyScopeParams
                  : null;
              void this.fetchModelPending(true, readbackScope);
            }
            // §11.2/§11.3 接线：保存设置（待同步草稿）的终态后按只读读面回读
            // （model.context_overrides），页面才有「草稿 · 已应用 · 客户端读回」
            // 的真实值；发布由统一同步窗口的 model.sync_apply 一次确认承担
            // （草稿与目录差异在同一受控发布轮里消费）。
            if (actionName === "model.settings_save") {
              if (settingsEdit)
                this.msEditsAwaitingReadback.set(
                  settingsEdit.key,
                  settingsEdit.revision
                );
              this.msSettingsReadSubmit(this.modelPick);
            }
          }
          // §11.1：两个只读读面（设置 / 探测证据）落定后，补一次被在途读取
          // 推迟的自动读取——按**当时选中**的模型补一次，不是定时重试循环。
          if (
            actionName === "model.context_overrides" ||
            actionName === "model.context_probe_preview"
          ) {
            this.settleAutoReadDeferred();
          }
          // R9-R40 task 4, R9-R46 chip rework: the initiating lane block keeps
          // one inline terminal chip until its next action clears it or the
          // 10s auto-fade deletes it (no dual track with the R9-R31
          // actionResults memory).
          const summary =
            shareLayerSummary(actionName, op) ??
            summarizeResult(op.result ?? {});
          const authUnconfirmed =
            actionName === "share.backend_login" &&
            op.result?.state !== "valid";
          this.setLaneSummary(
            this.laneKey(actionName, target),
            summary,
            authUnconfirmed ? "unconfirmed" : "ok"
          );
          if (actionName.startsWith("share.")) {
            // R9-R46: share terminal states re-read both share reads, so the
            // lane never keeps its entry snapshot after a lifecycle change.
            void this.refreshShareData();
          }
          // Rev19 A15 维护前后比较：单层重启动作自己的终态结果带回 before /
          // after 同层读数（后端 `_diag_pair()`），页面记下这一次动作的前后，
          // 供「分层维护」卡里比较；结果没带就不写（不重读一遍冒充）。
          if (
            actionName === "share.backend_restart" ||
            actionName === "share.gateway_restart"
          ) {
            const before = op.result?.before,
              after = op.result?.after;
            this.shareRestartCompare =
              before &&
              typeof before === "object" &&
              after &&
              typeof after === "object"
                ? {
                    action: actionName,
                    at: Date.now(),
                    before: before as Record<string, unknown>,
                    after: after as Record<string, unknown>,
                  }
                : undefined;
          }
          // R11-9: a per-row speedtest terminal state patches its ONE cell
          // in place instead of rebuilding the whole lane (settleSpeedPoll
          // reads the mapping before speedSettle consumes it — P2 2026-09-24).
          const speedCtx = this.settleSpeedPoll(actionName, opId);
          // F5: 国际链额度的只读读取在终态落定（数字 + 来源 + 检查时间）。
          this.settleQoderGlobalQuota(actionName, op.result ?? {});
          // P1/R1（2026-10-10）：真实启停动作的终态记成人工意图——停用成功后
          // 本次自动读取（入页 / 重进 / 进模型）不再向上游提交；启动成功则恢复。
          this.rememberServiceIntent(actionName, target, op.result ?? {});
          this.fbFinish(
            opId,
            authUnconfirmed ? "unconfirmed" : "succeeded",
            `${actionLabel(actionName)} → ${target || "—"} ${authUnconfirmed ? "已受理，认证待确认" : "完成"}`,
            authUnconfirmed,
            summary,
            !speedCtx
          );
          return;
        }
        const failedSpeedCtx = this.settleSpeedPoll(actionName, opId);
        // 预览终态失败：它那次提交的范围快照不再等结果，丢掉（同一次预览的
        // 结果只落一次；不把它留给下一次预览的结果）。
        this.previewScopePending.delete(opId);
        const code = op.error?.code ?? op.state;
        const message = op.error?.message ?? "";
        const recoveryHint = shareRecoveryHint(actionName, code);
        const recovery = recoveryHint ? `；${recoveryHint}` : "";
        // 账号页只读查询的终态失败留下失败结论（含操作终态错误码/消息）：
        // 不留原因时 `accountAuthHtml` 只能落「未读取」分支，把「没读到」
        // 写成「还没问过」，也把「未知」折成「未认证」。
        if (REMEMBER_READ_RESULT.includes(actionName)) {
          this.actionResultFailures.set(
            actionName,
            `失败（${code}${message ? `：${message}` : ""}）`
          );
        }
        this.settleQoderGlobalQuota(
          actionName,
          undefined,
          `失败（${code}${message ? `：${message}` : ""}）`
        );
        this.setLaneSummary(
          this.laneKey(actionName, target),
          `${actionLabel(actionName)} → ${target || "—"} 失败（${code}${message ? `：${message}` : ""}）${recovery}`,
          "failed"
        );
        if (actionName.startsWith("share.")) void this.refreshShareData();
        this.fbFinish(
          opId,
          "failed",
          `${actionLabel(actionName)} → ${target || "—"} 失败`,
          true,
          `失败（${code}${message ? `：${message}` : ""}）${recovery}`,
          !failedSpeedCtx
        );
        void this.probeTick();
        return;
      } catch {
        continue;
      }
    }
    this.setLaneSummary(
      this.laneKey(actionName, target),
      `${actionLabel(actionName)} → ${target || "—"} 轮询超时，结果未知（未知 ≠ 失败），请稍后核对操作账本`,
      "unconfirmed"
    );
    // F5: 超时同样落定国际链额度的读取状态（保留上次真实值并标过期）。
    this.settleQoderGlobalQuota(actionName, undefined, "轮询超时，结果未知");
    // R11-9 (completion-path integrity): a speedtest that times out still
    // settles its busy marker and patches its ONE cell — the poll deadline
    // must not leave the row disabled until an unrelated repaint.
    const timedOutSpeedCtx = this.settleSpeedPoll(actionName, opId);
    // 轮询超时的预览不会再落结果：丢掉它的提交时快照（未知 ≠ 失败，面板
    // 不拿旧快照冒充这次预览的范围）。
    this.previewScopePending.delete(opId);
    this.fbFinish(
      opId,
      "unconfirmed",
      `${actionLabel(actionName)} → ${target || "—"} 轮询超时，结果未知（未知 ≠ 失败），请稍后核对操作账本`,
      true,
      "",
      !timedOutSpeedCtx
    );
  }
  /** 协作取消：`POST /api/console/operations/{id}/cancel`（usagesrv.py:3270-3282）。
   *
   *  只对**后端标明可取消**（动作注册表 `cancelable === true`）且**仍在
   *  运行/排队**的操作开放；按钮因此由「注册表 + 当前状态」两件事共同决定，
   *  不凭前端猜测。请求形状：空体 POST（`Content-Type` 与既有同源写同款），
   *  返回 `{requested: true, operation}`。
   *
   *  「请求取消」与「已取消」是两件事：受理只写 `cancel_requested`，排队中的
   *  操作由后端直接落 `cancelled`（从未执行），执行中的操作要等当前步骤结束
   *  才落终态——所以这里按返回的 `operation.state` 如实显示，绝不把「已请求」
   *  写成「已取消」，也不自动重发。终态仍以操作账本为准。 */
  private async cancelOperation(opId: string) {
    if (!opId || this.cancelInFlight.has(opId)) return;
    this.cancelInFlight.add(opId);
    this.cancelError.delete(opId);
    this.renderPanel();
    try {
      const response = await fetch(
        `/gw/usagesrv/api/console/operations/${encodeURIComponent(opId)}/cancel`,
        {
          method: "POST",
          credentials: "include",
          headers: {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
          },
        }
      );
      const data = (await response.json().catch(() => null)) as {
        requested?: boolean;
        operation?: OpRow;
        error?: string;
        detail?: string;
      } | null;
      if (!response.ok) {
        const reason = data?.detail || data?.error || `HTTP ${response.status}`;
        this.cancelError.set(opId, reason);
        return;
      }
      if (data?.requested !== true) {
        this.cancelError.set(opId, "响应未确认受理（不重发）");
        return;
      }
      this.cancelRequested.add(opId);
      // 受理后立刻回读一次操作账本：排队中的操作可能已经落 cancelled，
      // 执行中的仍是 running + cancel_requested —— 终态由账本回答，不靠本地推断。
      void this.fetchRecentOps().then(() => this.renderPanel());
    } catch {
      this.cancelError.set(opId, "网络失败或服务未运行（不自动重发）");
    } finally {
      this.cancelInFlight.delete(opId);
      this.renderPanel();
    }
  }
}
