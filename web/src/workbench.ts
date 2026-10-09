import { consoleFetch, assertPublicOperation, PUBLIC_ACTIONS, PUBLIC_LOG_SOURCES } from "./console/backend/public-scope";
import { rollText, patchRollingPanel } from "./workbench-rolling";
import { escapeHtml } from "./html";
import { logKind, logKindLabels, chartBaselineKind, timeAxisWidth, axisLabelLines, type LogKind } from "./workbench-log-presentation";
type ConsoleProperties = Record<string, { value: unknown }>;
import { fmtTokens } from "./console/ui";
import { encodeSyncScope, normalizeContextEvidence, normalizeContexts, normalizeGatewayConnection, normalizeShareUsage } from "./console/backend/normalize.ts";
import type { KeyUsage, ShareUsageBucket, CostDetail, ContextEvidence, ContextView, ModelSettings, TokenUsage, GatewayConnection, ReadResult } from "./console/backend/types.ts";
import { BackendClient, GatewayConnectionBinding, type GatewayConnectionView } from "./console/backend/client.ts";
import { createFetchTransport } from "./console/backend/transport.ts";
import { copyGatewayConnectionField, type GatewayConnectionCopyField } from "./console/backend/clipboard.ts";
import { GatewayOrderPreferences, reorderGatewaySubset } from "./console/backend/preferences.ts";
import type { ProbeResult } from "./live";
import "./workbench.css";
import {
  actionLabel, stateDot, stateText, summarizeResult, isVisibleState,
  type FeedbackEntry, type FeedbackState,
} from "./console/operation-feedback";
import { defaultWorkbenchVisibility, applyVisibilityProperties, type WorkbenchVisibility } from "./workbench-visibility";

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
  "8096 操作账本 · 失败样本",
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
  v.toLocaleString("zh-CN", { minimumFractionDigits: 3, maximumFractionDigits: 3 });
/** 节点金额四态（后端 `_round_service`，usagesrv.py:903-913 / :1018-1040 明写
 *  「costCnyPartial=true 表示 costCny 只含可折算的记录（下界，不是总额）；只有整
 *  节点金额全部无法折算时 costCny=null（未知，绝不折 0、绝不按现价）」）：
 *  · absent  = 该行没有 ¥ 分量（积分链行，按既有的「—」显示）；
 *  · unknown = 整节点金额全部无法折算，显示「未知」；
 *  · partial = costCny 只含可折算记录，是下界——读数必须带下界标识；
 *  · full    = 完整金额，真实零也是完整值（¥0.000）。
 *  把 partial 印成确定总额、把 null 折成 0 再显示 ¥0.000，都是这一条要修的地方。 */
type CostAmount = { kind: "absent" | "unknown" | "partial" | "full"; value?: number };
const costAmount = (v: number | null | undefined, partial?: unknown): CostAmount => {
  if (v === undefined) return { kind: "absent" };
  if (v === null) return partial === true ? { kind: "partial" } : { kind: "unknown" };
  return { kind: partial === true ? "partial" : "full", value: v };
};
/** 单值金额文本：partial 必须带下界标识（≥），null+partial（可折算记录为空）写
 *  「未知（下界仅含可折算部分）」，绝不出现不带 ≥ 的确定总额。 */
const fmtCostAmount = (a: CostAmount): string =>
  a.kind === "absent" ? "—"
    : a.kind === "unknown" ? "未知"
      : a.kind === "partial" ? (a.value === undefined ? "未知（下界仅含可折算部分）" : `≥${fmtCny(a.value)}`)
        : fmtCny(a.value!);
/** 第一阅读层读数元素的文本：零 = ¥0.000 一字不差（真实零与「未知」分得开），
 *  partial = ≥¥…（带下界标识），unknown = 未知（不含 ¥），无分量 = —。 */
const costAmountReading = (a: CostAmount | undefined): string =>
  !a || a.kind === "absent" ? "—"
    : a.kind === "unknown" ? "未知"
      : a.kind === "partial" ? (a.value === undefined ? "未知" : `≥${fmtCny(a.value)}`)
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
  const style = "display:inline-block;margin:0;font-size:12.5px;letter-spacing:.06em";
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
 *  「chat2api 费用」与「模型调用明细 · 计费链」两列的 partial 行已印 ≥，悬停再给
 *  一句「已知部分，含不可折算记录」；整节点全不可折算（partial 且无可折算值，
 *  读数已是「未知（下界仅含可折算部分）」）不给——那里没有已知部分，给
 *  「已知部分」会是假话；完整值（含真实零）与「—」都不给。 */
const costAmountBoundTitleAttr = (a: CostAmount): string =>
  a.kind === "partial" && a.value !== undefined ? ` title="已知部分，含不可折算记录"` : "";
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
  let a = 0x811c9dc5, b = 0x9e3779b9;
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
  return rows.map(row => {
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
  "口径：/api/summary 的 chat2api 账本聚合；探针证据陈旧只降低新鲜度，不判宕机。近 14 天 = 本期 7 天 + 上期 7 天，环比按两侧真实读数计算。",
  "口径：额度与用量为只读查询，各带自己的状态词与检查时间；未知不折成 0。",
  "口径：操作三态 pending / ok / failed 不合并；只读查询不计入操作账本。",
  "口径：估算费用来自 chat2api 账本；未定价请求不折算为 ¥。",
  "口径：日志为各网关自身账本的有界尾读，未读取的来源如实列出；不以空表冒充零记录。",
  "口径：预览只读不改配置，应用同步才写入受管客户端配置；范围只收窄不扩大。",
  "口径：分享密钥明文只在页面会话内可见；轮换 / 撤销并终止 / 删除各自二次确认。",
];

/** 契约 3.13（图标规范）：**全控制台只有这一张路径表**，键 = 语义名，
 *  值 = 一组 `<path>`。几何统一在 `wbIcon()` 里声明（viewBox 0 0 16 16、
 *  16px、`stroke="currentColor"`、`stroke-width="1.5"`、`fill="none"`、圆头圆角），
 *  因此同一层级的图标不可能各写一套线宽。**不引入图标依赖**：`package.json`
 *  的 dependencies 只有 `@kitlangton/rolling-number` 与 `three`，一个都不加。 */
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
const kpiChip = (icon: string, entity: "req" | "tok" | "cred" | "cost"): string =>
  `<span class="wb-kchip wb-kchip-${entity}" aria-hidden="true">${wbIcon(icon)}</span>`;

/** Plan-Revision 6（折叠口径）：整行可点的 `<summary>` 左侧写对象名或条数，
 *  右侧固定位置放 chevron；`data-wb-fold-chevron` 是门禁的标记，旋转由 CSS 做。
 *  标记必须为空（CSS 的 `:empty::before` 才画 chevron），所以这里不留文本节点。 */
const wbFoldMark = (): string =>
  `<span class="wb-fold-mark" data-wb-fold-chevron="1" aria-hidden="true"></span>`;

/** Navigation label: every nav control carries its visible name in the 文字
 *  mode, so the navigation does not depend on reading an icon. In the 仅图标 mode
 *  (html[data-console-rail="collapsed"], also forced at ≤ 1180px) the label hides
 *  and the same name shows on the `wbIconHint()` plate instead; `aria-label`
 *  names the control in both states. */
const wbNavLabel = (text: string): string =>
  `<span class="wb-nav-label">${escapeHtml(text)}</span>`;

/** 导航六项 + 设置（契约 4.1 / 3.13b）：图标 + 短标签，标签仍是 `names` 的
 *  原词，业务词一个不改。 */
const LANE_ICONS: readonly string[] = ["overview", "gateway", "ops", "cost", "logs", "models", "share"];

type ServiceRow = { id: number; name: string; port: string; note: string; targets?: string[]; statusAction?: { action: string; target: string; label: string; gated?: true }; manageActions?: readonly { action: string; target: string; label: string; title: string }[] };
const SERVICES: readonly ServiceRow[] = [
  { id: 0, name: "chat2api 主链", port: "8000", note: "Open WebUI 会话代理", targets: ["chat2api"], statusAction: { action: "account.um_status", target: "UM", label: "查询认证状态" }, manageActions: [
    { action: "account.login", target: "", label: "账号登录", title: "打开登录窗口，由维护者本人完成登录；执行前需确认" },
    { action: "account.um_verify", target: "UM", label: "主动核验", title: "主动核验上游凭据有效性" },
  ] },
  { id: 1, name: "费用统计服务", port: "8096", note: "统计聚合与控制台适配层，不提供启停" },
];
const HEALTH_ID_BY_SERVICE: Record<number, string> = { 0: "chat2api", 1: "usagesrv" };

/** F12-3: model series drawn at once on a trend chart (four validated colour
 *  slots besides the total line). */
const TOKEN_SERIES_MAX = 4;
const SOURCE_SLOTS: Readonly<Record<string, number>> = { chat2api: 4 };

/** Rev16 A16-2: the console host (8096, id 1) keeps its health probe and the fee
 *  lane, but it is not a business chain, so it has no row in the service list,
 *  the jump index or the chain counts. Rows are looked up by their stable `id`,
 *  never by array position, so the remaining chains keep their ids (and the log
 *  / account / runtime mappings keyed on them). */
const CONSOLE_HOST_ID = 1;
/** The main chain (chat2api) that the overview verdict speaks for. */
const MAIN_CHAIN_ID = 0;
const CHAINS: readonly ServiceRow[] = SERVICES.filter(s => s.id !== CONSOLE_HOST_ID);
const serviceById = (id: number): ServiceRow | undefined => SERVICES.find(s => s.id === id);
const CHAIN_COUNT_WORD = ["零", "一", "两", "三", "四", "五", "六", "七", "八", "九"][CHAINS.length] ?? String(CHAINS.length);

/** Rev17 排序增补：the browser-local gateway-order preference namespace. The
 *  candidate preview build is the Demo (its #demo-* surfaces and the console
 *  gate run synthetic fixtures), so it keeps its own namespace; production (and
 *  the dev server, a different origin anyway) uses the production one. A preview
 *  run can therefore never write the order a real page reads back. */
const GW_ORDER_NAMESPACE = import.meta.env.MODE === "candidate" ? "demo" : "production";
/** Presentable reason for every read status whose stored order was NOT used; the
 *  module's semantics stay untouched (the stored payload is kept, the effective
 *  order is the default). */
const GW_ORDER_RESET_REASONS: Readonly<Record<string, string>> = {
  reset_corrupt: "保存的顺序已损坏",
  reset_version: "保存的顺序格式版本不认识",
  reset_duplicate: "保存的顺序有重复条目",
  storage_read_failed: "浏览器拒绝读取保存的顺序",
};

const isLoginAction = (action: string): boolean => action.endsWith(".login");

/** What an armed two-step confirm is for: the action plus its target. Rev8
 *  模型设置的应用按钮没有 action/target 数据属性，用 `source|model_id` 当目标；
 *  模型同步的应用按钮带 `data-wb-gw-source`，用来源当目标。两处都只比较身份，
 *  不比较文案——重绘后按身份重新武装。 */
const confirmKey = (b: HTMLElement): string => {
  const d = b.dataset;
  const action = d.wbSvcAction ?? d.wbModelAction ?? d.wbAcctAction ?? d.wbMsAction ?? "";
  const target = d.wbTarget ?? d.wbAcctTarget ?? d.wbMsKey ?? d.wbGwSource ?? "";
  return `${action}|${target}`;
};

/** 账号页的只读查询动作（契约 4.3）：各链自己的认证/登录状态查询与额度查询。
 *  它们的真实返回是账号页「认证状态」与「上游额度桶」的唯一来源，所以与
 *  `model.*` 一样进 `actionResults` 记忆——否则 `accountAuthHtml` 没有任何
 *  写入者，永远只能显示「未读取」，「已认证 / 未认证 / 读于」不可达。 */
/** Read-only actions whose last answer (or failure) the page remembers. */
const SHARE_READ_ACTIONS: readonly string[] = ["share.diagnostics"];
/** Identity of a share-lane confirm: the action, or the key operation + key id. */
const shareConfirmKey = (b: HTMLElement): string =>
  b.dataset.wbShareAction ? `act:${b.dataset.wbShareAction}`
    : b.dataset.wbShareKey ? `key:${b.dataset.wbShareKey}:${b.dataset.wbShareKeyId ?? ""}` : "";
/** Rev15 A15 single-layer maintenance actions (one share orchestration lock). */
const SHARE_LAYER_ACTIONS: readonly string[] = ["share.backend_reauthenticate", "share.backend_restart", "share.gateway_restart"];
/** Rev15 A15: Chinese summary of a finished single-layer share action, from the
 *  fields the backend returns - the backend's own label, whether a restart
 *  really happened, and a cancel that came too late to stop it (the operation
 *  reports what actually happened; the page does not call it rolled back).
 *  null for every other action (the generic summary applies). */
function shareLayerSummary(actionName: string, op: { result?: Record<string, unknown> | null; cancel_requested?: boolean }): string | null {
  const r = op.result ?? {};
  const late = op.cancel_requested === true ? "取消已请求但未能拦下：按实际结果报告" : "";
  if (actionName === "share.diagnostics") return "分层状态已读取";
  if (actionName === "share.backend_reauthenticate") {
    const head = r.accepted === false ? "未受理" : typeof r.label === "string" && r.label ? r.label : typeof r.state === "string" ? `状态 ${r.state}` : "已提交";
    return [head, typeof r.note === "string" ? r.note.slice(0, 80) : "", late].filter(Boolean).join("；");
  }
  if (actionName === "share.backend_restart" || actionName === "share.gateway_restart") {
    const part = (actionName === "share.gateway_restart" ? r.gateway : r.backend) as Record<string, unknown> | undefined;
    const secs = typeof part?.elapsed_s === "number" ? `，用时 ${Math.round(part.elapsed_s)} 秒` : "";
    const head = r.restarted === true ? `已重启${secs}` : r.restarted === false ? "未重启" : "已完成（未返回是否重启）";
    return [head, late].filter(Boolean).join("；");
  }
  return null;
}
const ACCOUNT_READ_ACTIONS: readonly string[] = ["account.um_status", "account.um_verify"];
/** 模型页只读读面的失败记忆（与账号页共用同一张 `actionResultFailures`）：
 *  catalog / 设置读面 / 探测证据读取失败时页面仍挂着上一次读数，必须留下
 *  实测失败状态，否则「读取失败」永远不可达（维护者 2026-10-06 接线）。 */
const MODEL_READ_ACTIONS: readonly string[] = [
  "model.catalog", "model.context_overrides", "model.context_probe_preview",
];
/** 只读查询失败/成功要写进失败记忆的动作集合（账号页 + 分享 + 模型读面）。 */
const REMEMBER_READ_RESULT: readonly string[] = [
  ...ACCOUNT_READ_ACTIONS, ...SHARE_READ_ACTIONS, ...MODEL_READ_ACTIONS,
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
const SPEEDTEST_SOURCE: Record<string, string> = { um: "um" };
const GATEWAY_MODEL_SOURCE: Readonly<Record<string, string>> = { chat2api: "um" };
/** 已接线的模型来源集合：同步按钮只接受表内的来源值，防止数据属性被改成
 *  任意字符串后把范围参数发散成页面自己造的值。 */
const GATEWAY_MODEL_SOURCES: ReadonlySet<string> = new Set(Object.values(GATEWAY_MODEL_SOURCE));

/** 模型设置上下文来源的中文标注（后端字段 declared/manual/measured）。 */
const CONTEXT_SOURCE_LABEL: Readonly<Record<string, string>> = {
  declared: "目录声明", manual: "人工设置", measured: "已验证下界",
};
/** 客户端读回状态的中文标注（后端字段 ok/absent/unavailable/unknown）。 */
const CLIENT_READBACK_LABEL: Readonly<Record<string, string>> = {
  ok: "已读回", absent: "客户端无该模型", unavailable: "读回不可用", unknown: "未知",
};
const LOG_FEED_SOURCE_ALIAS: Record<string, string[]> = { chat2api: ["chat2api"], um: [] };
const LOG_FEED_SOURCES = ["chat2api"] as const;
const LOG_FEED_PER_SOURCE = 40;
const LOG_FEED_LIMIT = Math.min(200, LOG_FEED_PER_SOURCE * LOG_FEED_SOURCES.length);

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
  "": undefined, "1h": 3600_000, "6h": 6 * 3600_000, "24h": 24 * 3600_000,
};

/** Quick-jump index (R9-R25): lanes, per-service panels and function
 *  keywords, one line each — as the workspace grows, an entry goes here so
 *  nothing is more than a keystroke away. Rev16 A16-5: an entry that names a
 *  part of a page lands on it (`anchor` is scrolled into view, `modelSection`
 *  opens that model tab) instead of the page top. */
const JUMP_INDEX: readonly { label: string; lane: number; service?: number; anchor?: string; modelSection?: "catalog" | "sync" | "probe" }[] = [
  { label: "总览 · 运行裁决与用量", lane: 0 },
  { label: "总览 · 服务明细与启停", lane: 0, anchor: ".wb-ov-services" },
  { label: "网关 · 账号登录与额度", lane: 1 },
  { label: "模型 · 目录与同步", lane: 5, modelSection: "catalog" },
  { label: "模型 · 同步预览与应用", lane: 5, modelSection: "sync" },
  { label: "费用 · 用量与账单", lane: 3 },
  { label: "费用 · 调用明细", lane: 3, anchor: '[data-wb-anchor="usage-feed"]' },
  { label: "日志 · 操作记录", lane: 2 },
  { label: "分享 · 开放状态与主操作", lane: 6 },
  { label: "分享 · 访问密钥管理", lane: 6, anchor: ".wb-share-keys" },
  { label: "分享 · 容量与近期操作", lane: 6, anchor: '[data-wb-anchor="share-capacity"]' },
  ...CHAINS.map(s => ({ label: `${s.name} · 管理详情`, lane: 1, service: s.id })),
  { label: "账号登录 / 重新授权", lane: 1, service: 0 },
  { label: "额度查询", lane: 1, service: 4 },
  { label: "服务重启（当前网关）", lane: 1 },
];

/** Console adapter action row (GET /api/console/actions). */
type ActionRow = { action: string; enabled: boolean; disabled_reason: string | null; description: string; cancelable: boolean };
/** Runtime service snapshot entry (GET /api/console/services). */
type RuntimeEntry = { key: string; display_name: string; port: number; state: string; healthy: boolean; detail: string; process_ids: number[] };
/** One operation row (GET /api/console/operations). `cancel_requested` is the
 *  backend's OWN field (console_operations.py `_view`): a cancel was accepted
 *  for a non-terminal row — 「已请求取消」 is NOT 「已取消」, the ledger's
 *  `state` still answers the terminal word. */
type OpRow = { id: string; action: string; target: string; state: string; created_at: number; cancel_requested?: boolean; result?: Record<string, unknown> | null; error?: { code?: string; message?: string } | null; steps?: { at?: number; note?: string }[] };
type OpStatus = { state: "pending" | "ok" | "failed"; text: string };
/** Remembered results of the model.* operations (model lane, R9-R31):
 *  catalog / sync_preview / sync_apply / sync_pending / task_status. */
type CatalogModelRow = { id: string; display_name: string | null; context: number | null; efforts: string[]; default_effort: string | null; publish: boolean; efforts_source?: string | null; output_token_cap?: { value?: number | null; source?: string | null } | null };
/** Per-day and per-model ledger aggregates (GET /api/summary services.*.days/.models).
 *  `costCnyPartial` 沿用后端语义（usagesrv.py:903-913）：true = costCny 只含可折算
 *  记录，是下界，不是总额。 */
type DayPoint = { day: string; requests: number; costCny?: number; costCnyPartial?: boolean; credits?: number };
/** Per-model ledger totals. `tokens` sums input + output of the rows that carry
 *  them; `unknownRequests` counts requests whose usage the ledger could not read
 *  (unknown_usage) - those are NOT in `tokens`, so a non-zero count makes the
 *  model's Token reading partial, never a silent 0. */
type ModelPoint = { model: string; requests: number; tokens: number; unknownRequests: number; tokensMissing: boolean };
type SummarySeries = { days: DayPoint[]; models: ModelPoint[]; chainDays?: Record<string, DayPoint[]> };
/** One per-chain series of the fee-lane trend chart (R9-R40 task 3): billed
 *  chains draw accent area/lines, the credits chain draws a dashed ink line —
 *  credits never merge into the ¥ totals. */
type FeeTrendSeries = { label: string; values: number[]; kind: "billed" | "credits"; opacity: number; fillOpacity: number };
/** Per-chain fee detail (R9-R29 cost lane): one row per aggregated gateway
 *  with its full model breakdown, straight from /api/summary services.*.
 *  `costCnyPartial` = 该链 costCny 只含可折算记录（下界，不是总额）。 */
type ChainModel = { model: string; chain: string; requests: number; input: number; output: number; costCny?: number | null; costCnyPartial?: boolean; credits?: number };
type ChainFee = { name: string; requests: number; input: number; output: number; costCny?: number | null; costCnyPartial?: boolean; credits?: number; models: ChainModel[] };

type LogEntry = { source: string; time: string; model: string; status: string; input_tokens: number | null; output_tokens: number | null; cached_input_tokens: number | null; reasoning_tokens: number | null; duration_ms: number | null; credits?: number | null };
/** Seven-day bounded statistics (GET /api/console/stats?from&to). */
type StatsWeek = { totals: { requests: number; input_tokens: number; output_tokens: number; cache_tokens: number; reasoning_tokens: number; estimated_cost_cny: number; credits: number }; days: { date: string; day_total: { requests?: number } }[] };
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
  date: string; source: string; model: string;
  input_tokens: number | null; output_tokens: number | null; complete: boolean;
  /** Requests that entered the two sums above. */
  complete_requests: number;
  /** Requests missing either component (listed separately, never in the total). */
  partial_requests: number;
  /** Sum over the partial requests' input / output components (null = that
   *  component is not visible in any of them). Never folded into the complete
   *  total; the visible output part backs 仅输出可见. */
  partial_input_tokens: number | null; partial_output_tokens: number | null;
};
/** `date: null` = the record's timestamp could not be parsed, so it belongs to
 *  NO Beijing day (reason `invalid_time`). Such a row is kept and listed as
 *  时间无法判定 — never silently dropped by a window filter and never guessed
 *  onto a day. F1: `input_tokens` / `output_tokens` are that bucket's PARTIAL
 *  requests' visible components (null when the component is not visible at all),
 *  so 仅输出可见 can show its own real numbers instead of a day lookup. */
type UsageTokenMissingRow = {
  date: string | null; source: string; model: string; reason: string;
  input_tokens?: number | null; output_tokens?: number | null;
};
/** One coverage error of one source (coverage.sources[...].errors[]): a type word
 *  plus an optional numeric count (bad lines / failed records / missing files). */
type UsageCoverageError = { type?: string; detail?: string; bad_lines?: number; count?: number; failed_records?: number; failed_rows?: number };
/** Per-source coverage disclosure of the two bounded usage reads
 *  (coverage.sources[<source>]): whether the ledger file was actually read
 *  (`ledger_present`), whether the source collects at all (`enabled` — a chain
 *  that is not collecting explains an empty window WITHOUT claiming zero usage)
 *  and the server's own `note`. Every field is optional and an absent one stays
 *  未知 on screen; the client never fills a coverage field in. */
type UsageSourceCoverage = {
  ledger?: string | null; ledger_present?: boolean; enabled?: boolean;
  events?: number; note?: string; errors?: UsageCoverageError[];
  truncated?: boolean; bytes_truncated?: boolean; dropped_events?: number;
  undated_records?: number; non_ok_records?: number; unknown_fields?: string[];
};
/** The coverage block of one bounded usage read. `truncated` / `clamped` /
 *  `dropped_rows` / `dropped_missing_rows` are the server's own bounded-read
 *  marks: shown when set (a dropped count of 0 is not noise). `dropped_rows`
 *  counts the dropped days[] rows, `dropped_missing_rows` the dropped missing[]
 *  rows (each list is capped on its own, console_stats.py). An absent `sources`
 *  renders as 覆盖信息未提供, never as an invented all-clear. */
type UsageCoverage = {
  days?: number; complete_rows?: number; partial_rows?: number; scanned?: number;
  hours?: number; rows?: number;
  truncated?: boolean; clamped?: boolean; dropped_rows?: number;
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
  hour: string; source: string; model: string;
  output_tokens: number; duration_ms: number; requests: number; rate: number | null;
};
type UsageRatesPayload = {
  hours: UsageRateRow[];
  skipped: { missing_output_tokens?: number; missing_duration?: number; invalid_duration?: number; invalid_time?: number };
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
  exchangeRate?: number; checkedAt?: string; checkedStale?: boolean;
  models?: Record<string, Record<string, unknown>>;
  providers?: Record<string, Record<string, unknown>>;
};
/** Server-side log source registry (GET /api/console/log-sources, R9-R38):
 *  the server owns the mapping source→path; the client only ever sends ids. */
type LogSourceRow = { source: string; display: string; categories: LogSourceCategory[] };
type LogSourceCategory = { category: string; display: string; name: string | null; rotating?: boolean; rotation_index?: number; level_filter?: boolean };
/** One run-log row (GET /api/console/log-entries): server-redacted. An
 *  unparsable line keeps its raw text (parse_ok=false), a multi-line error
 *  block arrives already attached to its parent message. */
/** Rev8 B8-5 unified run event the backend derives from a log line (every field
 *  null when it cannot prove it; nothing here is guessed by the page). */
type LogRowEvent = { timestamp?: string | null; model?: string | null; request_id?: string | null; phase?: string | null; outcome?: string | null; error_code?: string | null; duration_ms?: number | null };
type LogEntryRow = { ts?: string; level?: string; message: string; parse_ok: boolean; cut?: boolean; event?: LogRowEvent | null };
type LogEntriesResult = {
  status: "ok" | "missing" | "denied" | "error" | "unconfigured";
  rows: LogEntryRow[]; scanned_bytes: number; window_start_offset: number;
  truncated: boolean; next_cursor: string | null; reset_hint: boolean; file_key: string | null;
  error?: string;
  /** Echoed only when the backend applied the request filter (scope = read window). */
  request_id?: string; request_scope?: string;
};
/** Log-detail view state (R9-R38): survives back-navigation and lane
 *  switches; expanded is a set of rendered-event identities with the error block
 *  unfolded (C4: never a row index — prepends and refreshes shift indices). */
type LogDetailState = {
  source: string; category: string;
  q: string; since: string; until: string;
  follow: boolean; newCount: number;
  rows: LogEntryRow[]; nextCursor: string | null;
  status: string; errorCode: string; scannedBytes: number;
  truncated: boolean; resetHint: boolean; stale: boolean;
  loading: boolean; requested: boolean;
  expanded: Set<string>; wrap: boolean; fetchedAt?: string;
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

/** Share lane payloads (A36-2 narrow facade, read-only composition).
 *  Missing/unknown fields render as 不可取得 — the UI never fabricates
 *  state, never substitutes fixture data (R9-R35 honest-gate pattern). */
type ShareStatusPayload = {
  schema_version?: number;
  share_max_concurrency?: number;
  desired_generation?: number;
  applied_generation?: number;
  in_sync?: boolean;
  pending_revocation?: boolean;
  active_keys?: number;
  total_keys?: number;
  broker?: { running?: boolean; version?: string; pid?: number } | null;
  /** ShareManager.status() returns the failure as an object {kind,error,at};
   *  the old string type never matched the real payload. */
  last_publication_failure?: { kind?: string; error?: string; at?: string } | null;
  /** Real payload is a list of publication-blocker rows, not a count. */
  blocked_publications?: Array<{
    operation_id?: string; status?: string; target_generation?: number;
    stop_confirmed?: boolean; blocked?: boolean; created_at?: string;
  }>;
  blocked?: boolean;
  share_url?: string | null;
  funnel_on?: boolean;
  /** Gateway INSTANCE evidence (broker verify_running seam) — distinct from
   *  broker.running, which is the always-on management daemon and is never
   *  open/residue evidence. */
  gateway_running?: boolean;
  account?: { valid?: boolean; checked_at?: number; failure?: string | null } | null;
  upstream?: { reachable?: boolean } | null;
  last_check_at?: number;
  last_failure?: string | null;
};
type ShareKeyRow = {
  id?: string;
  name?: string;
  masked?: string;
  /** R11-F2: backend flag — this row stores a same-identity ciphertext and
   *  its plaintext is retrievable through the narrow per-key endpoint. */
  plaintext_viewable?: boolean;
  enabled?: boolean;
  max_concurrency?: number;
  row_version?: number;
  created_at?: number;
};
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
  name: string; records: number | null; inputTokens: number | null; outputTokens: number | null; tokens: TokenUsage | null;
  known: number | null; estimated: number | null; unknown: number | null; unpriced: number | null;
  models: CostDetail[]; days: CostDetail[];
};
type ShareUsageCoverage = {
  ledgerPresent?: boolean; ledgerReadable?: boolean; recordsTotal?: number;
  unparsedLines?: number; truncated?: boolean; ledger?: string; limit?: number;
  records?: number; attributed?: number; unattributed?: number;
  incompleteRequests?: number; unknownUsageRequests?: number;
  unknownTimeRequests?: number; unpricedRequests?: number;
  peakRequests?: number; unpricedModels?: string[]; priceVersions?: string[];
  priceFn?: boolean; attributionReasons?: Record<string, number>;
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
    requests?: number | null; input_tokens?: number | null;
    output_tokens?: number | null; estimated_cost_cny?: number | null;
    credits?: number | null;
  };
};
type StatsRangePayload = {
  days?: StatsRangeDay[];
  totals?: Record<string, number | null>;
  coverage?: { sources?: Record<string, unknown>; missing_days?: string[]; unpriced_models?: string[] };
  meta?: { timezone?: string; range_days?: number; price_checked_at?: string | null };
};
/** 区间聚合的四态：loading / live / 后端拒绝（400，携带后端受控 detail）/
 *  其他失败（含实测 HTTP 状态，404 = 当前服务端版本未载入该端点）。 */
type StatsRangeState =
  | { state: "loading"; from: string; to: string }
  | { state: "rejected"; from: string; to: string; status: number; reason: string }
  | { state: "failed"; from: string; to: string; status?: number; reason: string }
  | { state: "live"; from: string; to: string; status: number; payload: StatsRangePayload };
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
type SyncDetailRow = { consumer?: string; kind?: string; model_id?: string; description?: string };
/** One history line (model.sync_history, bounded 1-50). */
type SyncHistoryEntry = {
  timestamp?: string; status?: string; applied?: string[];
  pending?: string[]; warnings?: string[];
};
/** Speedtest single-shot result (model.speedtest). */
type SpeedtestResult = {
  status?: string; ttfb_s?: number | null; total_s?: number | null;
  first_content_preview?: string | null; usage?: Record<string, unknown> | null;
  /** Client-side settle time (the server result does not echo a clock). */
  at?: number;
};
/** Context-probe queue record (model.context_probe_preview, backend canonical
 *  fields `records` / `output_token_cap`, Rev8 E8-1): honest estimates in
 *  characters (input) and tokens (output cap); `executable` false = no endpoint or
 *  unreadable credentials, so the record costs nothing this round. */
type ContextProbeQueueRow = {
  source?: string; model_id?: string; declared_context?: number | null;
  status?: string; estimated_requests?: number | null; executable?: boolean;
  input_chars_estimate?: number | null; output_token_cap?: number | null;
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

/** 分享读口失败原因（维护者 2026-09-28 的 ④）：后端自己的原因串优先，
 *  它没有可读错误体时才回落到**实测** HTTP 状态——同一事实在页面上只写
 *  一次。原先无条件补一次「（HTTP 400）」会把后端已给出错误码的应答印成
 *  两种状态（「share_read_failed（HTTP 400）」），本轮复核发现 9 的口径是
 *  `body.reason || body.error || `HTTP {status}``，与分享动作被拒
 *  （`submitAction` 的「被拒绝：${reason}」）同一条：后端给了错误码时
 *  页面不得再出现实测状态。 */
const shareReadReason = (body: { error?: string; reason?: string } | null, status: number): string =>
  body?.reason || body?.error || `HTTP ${status}`;

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
  private summaryState: "loading" | "unauthenticated" | "forbidden" | "error" | "stale" | "live" = "loading";
  private summaryError = "";
  /** 首层横幅的实测原因（§3.9 六态可分、§3.12(c)1 短句须含状态词与动作对象）：
   *  只有 fetch 本身失败才是「8096 不可达」；5xx、非 2xx、解析失败与形状不符
   *  都说明 8096 **应答了**，横幅不得把「应答了错」写成「不可达」，也不得
   *  把它写成「未读取」——同一事实在横幅与车道正文里必须是同一个结论。 */
  private summaryCause = "";
  private summaryEpoch?: number;
  private summaryLive?: { requests: number; tokens: number; costCny: string; costCnyPartial?: boolean; costCnyUnknown?: boolean; credits: number };
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
   *  / 形状不符 / 传输失败）——「读取被拒绝：无凭据会话或 8096 不可达」这种
   *  把两种可能并列写成结论的措辞没有实测证据，不得由前端推断。 */
  private logsFeed?: { state: "gated" | "live"; rows: LogEntry[]; unavailable?: string[]; reason?: string };
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
   *  「8096 不可达」——那只是另一路读口（opsRecent / runtime）没落地。 */
  private modelOverviewState: "idle" | "loading" | "live" | "transport" | "http" | "shape" = "idle";
  private modelOverviewStatus = 0;
  /** L-C model lane extras: catalog search filter, checked-model scope
   *  (source|model_id keys), sync-scope mode, history, speedtest memory,
   *  context-probe queue, readback state. Write faces stay behind the
   *  existing two-step confirms; filters are client-side reads. */
  private modelCatalogQ = "";
  private modelChecked = new Set<string>();
  private modelScopeCustom = false;
  /** P1 (2026-09-24): the ONE catalog row whose 高级覆盖 area is
   *  expanded (key = source|model_id). Display-level only: it picks which
   *  value the row SHOWS (source value vs probe-verified value) and never
   *  writes the source directory, the sync config or a probe record. */
  /** F12-5: the catalog row whose details fill the independent detail pane
   *  (`src|id`); details never expand inside the table, so the table's columns
   *  and row heights stay put. */
  private modelPick: string | null = null;
  /** P1 (2026-09-24): rows where the user restored the SOURCE value as
   *  the effective display, overriding a probe-verified value for THIS page
   *  session only (cleared on reload; nothing is written anywhere). */
  private effortRestored = new Set<string>();
  private modelHistory?: { rows: SyncHistoryEntry[]; total: number; skipped: number; failed?: boolean };
  private speedResults = new Map<string, SpeedtestResult>();
  private speedBusy: string | null = null;
  private speedPending = new Map<string, { source: string; model: string }>();
  private probeQueue?: { state: "loading" | "error" | "live"; reason?: string; payload?: ContextProbePreview };
  /** 契约 3.12b / 3.12c.4：模型页探针说明的展开态（说明是解释性内容，
   *  默认折叠；展开态在重绘与切 lane 后保持）。 */
  private probeIntroOpen = false;
  /** 契约 3.12c.4（同一问题）：模型同步区里「同步任务」折叠原先没有登记字段，
   *  浏览器原生点开后，30s 轮询重绘或切 lane 回来都会折回默认关闭。开合态写进
   *  类字段，由点击记录器写入、由重绘按字段还原。 */
  private syncTasksOpen = false;
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
  /** Rev8 模型设置（页面会话内的表单草稿）：字段值按 `source|model_id` 记，
   *  否则 30s 轮询 / 读面落定后的重绘会抹掉正在编辑的值（先例 `shareKeyEdit`）。
   *  保存 / 预览 / 应用的结果不另存一份，全部从 `actionResults` 按动作读回：
   *  `model.settings_save` 的 source/model_id、`model.settings_preview` 的
   *  selection、`model.settings_apply` 的 selection 都决定那份结果属于哪个模型。 */
  private msAlias = new Map<string, string>();
  private msCtx = new Map<string, string>();
  private msCtxTokens = new Map<string, string>();
  private msEffort = new Map<string, string>();
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
  private gwCopyFeedback: { service: string; field: "url" | "key"; ok: boolean; reason?: string } | null = null;
  private gwConnBinding: GatewayConnectionBinding | null = null;
  private gwConnService = "";
  private gwConnReading = false;
  private gwConnSeq = 0;
  /** Candidate-only demo of the block (#demo-connection): synthetic bodies from
   *  the backend fixtures, labelled as such; never present in a production build. */
  private connDemoBuild: ((service: string, state: string) => GatewayConnectionView) | null = null;
  private connDemoState = "present";
  private connDemoFeedback: "" | "ok" | "fail" = "";
  private gwQuery = "";
  private gwAuth: "all" | "ok" | "attention" | "unknown" = "all";
  /** Sidebar order by stable service id. Every completed move is written as one
   *  complete sequence through the backend's order-preference module, whose
   *  namespace keeps this preview build apart from production; the in-page order
   *  stays authoritative and `gwOrderNote` states what actually happened. */
  private gwOrder: number[] = CHAINS.map(s => s.id);
  private gwOrderPrefs: GatewayOrderPreferences | null = null;
  private gwOrderNote = "顺序拖动后会在本浏览器记住。";
  /** Narrow screens show the list OR the detail; this is the detail side. */
  private gwNarrowDetail = false;
  /** Pointer reorder in progress: rows are moved live in the DOM and every panel
   *  repaint waits for the drop (a repaint would morph the rows mid-drag). */
  private gwDrag?: { id: number; pointerId: number; startOrder: number[]; moved: boolean };
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
  /** Management-session derived live data; undefined = gated (fixture view). */
  private runtime?: Record<string, RuntimeEntry>;
  private actions?: ActionRow[];
  private opsRecent?: { state: "gated" | "live"; rows: OpRow[]; reason?: string };
  private opStatus?: OpStatus;
  /** R9-R37 notice pool: one entry per submitted operation from submit to
   *  finish, keyed client-locally then rebound to the server operation id. */
  private feedbackEntries = new Map<string, FeedbackEntry>();
  private feedbackSeq = 0;
  /** System banner dismissed for the current 8096-unreachable episode. */
  private bannerDismissed = false;
  /** R9-R38 run-log detail: sources registry plus the live detail view.
   *  state survives back-navigation and lane switches; seq aborts stale
   *  responses when the source flips so rows never cross gateways. */
  private logSources?: LogSourceRow[];
  private logOverview = new Map<string, { status: string; rows: LogEntryRow[]; truncated: boolean }>();
  private logOverviewLoading = false;
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
  private armedConfirm: { sel: string; text: string; key: string } | null = null;
  /** 「已复制」flash state that survives morph repaints: the in-place panel
   *  patch resets a flashed label within the flash window (probe ticks
   *  repaint repeatedly), so the flash is re-applied after every rebuild
   *  until its deadline. */
  private flashCopiedState: { sel: string; base: string; until: number } | null = null;
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
  private shareKeys?: ShareKeysState;
  /** 分享用量读口（`/api/console/share-usage`）：与状态/密钥同入口
   *  读取，请求序号守卫保证被超越的在途响应不覆盖新状态（照 fetchShareKeys）。 */
  private shareUsage?: ShareUsageState;
  private shareUsageSeq = 0;
  /** R11-F2 (Plan-Revision 11): plaintext per key_id, memory-only. Seeded
   *  by the create/rotate immediate responses and by the narrow per-key
   *  readback; NEVER written to localStorage, URLs, notices, operation
   *  records or logs. Legacy hash-only rows are never requested.
   *  P1 (2026-09-24): each entry carries the row_version it was read at.
   *  A row whose version moved (rotation in another tab, concurrent op)
   *  invalidates the cached plaintext instead of showing/copying the old
   *  value, and the readback sends expected_row_version so the backend
   *  answers version_conflict rather than a plaintext from a newer row. */
  private shareKeySecrets = new Map<string, { secret: string; rowVersion: number | null }>();
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
  private shareRestartCompare?: { action: string; at: number; before: Record<string, unknown>; after: Record<string, unknown> };
  private shareStatusFailed = false;
  private sharePlaintext?: { kind: "created" | "rotated"; name: string; value: string; at: number };
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
  private shareKeyEdit?: { id: string; field: "name" | "cap"; value: string; focusPending: boolean };
  /** Plaintext section extras: mask toggle and the post-clear note survive
   *  re-renders as class state. */
  private shareSecretHidden = false;
  private shareSecretCleared = false;
  /** Block ② five-layer preconditions detail: collapsed by default, auto
   *  opened when a layer shows failure/contradiction (A36-1). */
  private sharePreconditionsOpen = false;
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
  private laneSummaries: Record<string, { text: string; state: "ok" | "failed" | "unconfirmed"; at: number }> = {};
  /** R9-R40 task 2: unfolded failure groups (keyed by action name). */
  private failuresOpen = new Set<string>();
  /** A lane-level explanation fold (2026-09-29 redesign): definitions and long
   *  口径 sentences that used to sit open under every lane move behind one
   *  closed-by-default fold, so the first reading layer is readings and actions.
   *  Only explanation goes in here — never a failure, a stale mark or a count the
   *  page is asserting (those keep their own resident surface, contract 3.12c). */
  private laneNoteFoldHtml(key: string, title: string, bodyHtml: string): string {
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
      if ((s.manageActions ?? []).some(a => a.action === actionName)) return `svc${s.id}`;
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
  private setLaneSummary(key: string, text: string, state: "ok" | "failed" | "unconfirmed") {
    this.laneSummaries[key] = { text, state, at: Date.now() };
    const at = this.laneSummaries[key]!.at;
    // No repaint here: every caller ends in fbFinish(), whose repaint also puts
    // the chip on the overview list row (PR11 Q17) — repainting here as well
    // rebuilt the whole overview twice per terminal state.
    window.setTimeout(() => {
      const cur = this.laneSummaries[key];
      if (!cur || cur.at !== at) return;
      const node = this.lane === 0 || this.lane === 1
        ? this.root.querySelector<HTMLElement>(`[data-svc-result="${CSS.escape(key)}"]`)
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
  constructor(private stage: HTMLElement, private onMode: () => void, private onLane: (lane: number) => void) {
    // PR11 shell (maintainer 2026-09-30, direction「青柠」): no sidebar. One sticky
    // top region carries the bar (brand, the grouped navigation track, quick jump,
    // appearance and the label toggle), the 8096 banner and the page head (page
    // name, the console-wide run verdict and the evidence time), so the banner
    // never paints over the head and the verdict stays in view on every lane.
    // The label preference is owned by console-entry.ts and lands here as
    // html[data-console-rail] (collapsed = icons only). The compatibility clock
    // node stays hidden in the console.
    stage.insertAdjacentHTML("beforeend", `<section class="workbench" hidden aria-label="控制台工作台">
      <div class="wb-main">
        <div class="wb-top">
          <header class="wb-bar">
            <div class="wb-brand"><span class="wb-brand-mark" aria-hidden="true">${wbIcon("gateway")}</span><span class="wb-brand-text"><strong>AI 网关</strong><small>本机统一控制台</small></span></div>
            <div class="wb-time" hidden><time class="wb-clock"></time><div class="wb-date"></div></div>
            <nav class="wb-nav" aria-label="工作台导航">${NAV_GROUPS.map((group, g) => `<div class="wb-nav-group" role="group" aria-labelledby="wb-nav-group-${g}"><span class="wb-nav-group-label" id="wb-nav-group-${g}">${escapeHtml(group.label)}</span>${group.lanes.map(i => `<button data-wb-lane="${i}" aria-pressed="false" aria-label="${escapeHtml(names[i])}">${wbIcon(LANE_ICONS[i] ?? "overview")}${wbNavLabel(names[i])}${wbIconHint(names[i])}</button>`).join("")}</div>`).join("")}</nav>
            <div class="wb-bar-tools">
              <div class="wb-jump">${wbIcon("search")}<input class="wb-jump-input" type="text" placeholder="跳转到服务 / 页面…" aria-label="工作台快速跳转" autocomplete="off" spellcheck="false"/><div class="wb-jump-list" hidden role="listbox"></div></div>
              <button type="button" class="wb-bar-btn" data-action="settings" aria-label="外观设置">${wbIcon("settings")}${wbNavLabel("外观设置")}${wbIconHint("外观设置")}</button>
              <button type="button" class="wb-bar-btn wb-bar-icon" data-action="rail-toggle" aria-label="导航仅显示图标" aria-expanded="true">${wbIcon("rail")}${wbIconHint("导航仅显示图标")}</button>
            </div>
          </header>
          <div class="wb-banner" hidden></div>
          <header class="wb-head"><div class="wb-head-title"><h2 class="wb-title"></h2><button type="button" class="wb-info" hidden></button></div><div class="wb-head-meta"><span class="wb-head-status" data-state="unknown" role="status"><i class="wb-dot wb-dot-unknown" aria-hidden="true"></i><span class="wb-head-status-text"></span></span><span class="wb-index"></span></div></header>
        </div>
        <div class="wb-content"></div>
      </div>
    </section>`);
    this.root = stage.querySelector(".workbench")!;
    this.loadGatewayOrder();
    void this.loadConnectionDemo();
    // The sticky top region wraps with the nav, so its height is published as
    // --wb-top-h: sticky panes inside wb-main stop below it instead of sliding
    // under it, and size themselves to the space that is left.
    const top = this.root.querySelector<HTMLElement>(".wb-top");
    if (top && typeof ResizeObserver !== "undefined") {
      new ResizeObserver(() => this.root.style.setProperty("--wb-top-h", `${Math.ceil(top.getBoundingClientRect().height)}px`)).observe(top);
    }
    // F12-3: a chart point taken with the pointer shows the chart's selected state
    // (band + tooltip) instead of the keyboard focus ring; Chrome matches
    // :focus-visible for a clicked SVG tab stop, which drew a black frame. The
    // mode is kept on the root - outside the patched content, whose repaint drops
    // any attribute the fresh HTML does not carry - and any key press ends it, so
    // Tab still gets the ring.
    this.root.addEventListener("pointerdown", event => {
      if ((event.target as Element).closest?.(".wb-area-hotspot")) this.root.setAttribute("data-wb-chart-pointer", "");
    });
    this.root.addEventListener("keydown", () => this.root.removeAttribute("data-wb-chart-pointer"), true);
    // A16-3: the user moving focus themselves ends the post-jump title focus.
    this.root.addEventListener("focusin", event => {
      if (this.logFocusPending && !(event.target as Element).closest?.("[data-log-title]")) this.logFocusPending = false;
    });
    // Overview master-detail list (PR11): ↑ / ↓ move the selection between the
    // chains and keep keyboard focus on the newly selected row.
    this.root.addEventListener("keydown", event => {
      const item = (event.target as Element).closest<HTMLElement>("[data-wb-md-item]");
      if (!item || (event.key !== "ArrowDown" && event.key !== "ArrowUp")) return;
      event.preventDefault();
      const ids = CHAINS.map(s => s.id);
      const at = ids.indexOf(Number(item.dataset.wbMdItem));
      const next = ids[(at + (event.key === "ArrowDown" ? 1 : ids.length - 1)) % ids.length];
      // A confirm armed in the detail pane belongs to the chain it was armed on;
      // moving the selection disarms it (a keypress never reaches the click
      // handler's disarm).
      this.clearConfirm();
      this.ovService = next;
      this.renderPanel();
      this.root.querySelector<HTMLElement>(`[data-wb-md-item="${next}"]`)?.focus();
    });
    // R11-F1: delegated at DOCUMENT level — the notice pool now lives on
    // document.body (viewport layer), so its buttons must be reachable by the
    // same delegation that serves the in-root buttons. The branches below only
    // act on elements carrying the specific data-wb-* attributes, so widening
    // the listener scope changes nothing for other clicks.
    document.addEventListener("click", event => {
      const button = (event.target as Element).closest<HTMLButtonElement>("button");
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
        else { if (this.logRawOpen.size >= 200) this.logRawOpen.delete(this.logRawOpen.values().next().value!); this.logRawOpen.add(key); }
        this.renderPanel(); return;
      }
      if (button.dataset.wbLogSource !== undefined) {
        const source = button.dataset.wbLogSource;
        if (source) this.openLogDetail(source);
        else { this.logDetailSeq++; this.logDetailOpen = false; this.logPending = null; this.logReturnLane = null; this.renderPanel(); }
        return;
      }
      if (button.dataset.wbLogDetailRefresh !== undefined && this.logDetail) {
        this.logDetail.follow = true; this.logDetail.newCount = 0; this.logDetail.loading = true; this.renderPanel();
        void this.fetchLogEntries(this.logDetail, false); return;
      }
      if (button.dataset.wbLogOverviewCopy !== undefined) {
        // C4：按渲染时身份取该次渲染绑定的原文。后台刷新可能已经改了
        // this.logOverview（逐批 set、末尾才重绘），按点击时重算的下标会取到
        // 另一条事件；身份不在渲染表里就如实反馈，不回退成下标。
        const row = this.logOverviewRenderRows.get(button.dataset.wbLogOverviewCopy);
        if (row) void this.copyLogRaw(row.message, button);
        else button.textContent = "该事件已不在当前列表，请刷新总览后重试";
        return;
      }
      const logOpenSrc = button.dataset.logOpen;
      if (logOpenSrc) { this.openLogDetail(logOpenSrc, undefined, this.lane); return; }
      const logReopen = button.dataset.logReopen;
      if (logReopen) {
        // A failed registry read is retried explicitly, then the same source reopens.
        this.logSourcesRequested = true;
        this.logSourcesFailed = false;
        this.logSourcesError = "";
        if (this.logDetail) this.logDetail = { ...this.logDetail, status: "loading", errorCode: "", loading: true };
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
        if (kind === "all" || Object.hasOwn(logKindLabels, kind)) this.logOverviewFilter = kind as LogKind | "all";
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
      if (logExpandId === undefined && logCollapseId !== undefined && logCollapseId !== "" && this.logDetail) {
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
        d.q = ""; d.since = ""; d.until = ""; d.requestId = ""; this.logOverviewFilter = "all";
        void this.fetchLogEntries(d, false);
        return;
      }
      const svcAction = button.dataset.wbSvcAction;
      if (svcAction === "start" || svcAction === "stop" || svcAction === "restart") {
        if (this.isArmed(button)) {
          this.clearConfirm();
          void this.submitAction(`service.${svcAction}`, button.dataset.wbTarget ?? "");
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
            void this.submitAction(acctAction, button.dataset.wbAcctTarget ?? "");
          } else {
            this.armConfirmLabel(button, "确认打开登录窗口？（真实弹出浏览器窗口，由维护者本人完成登录）");
          }
          return;
        }
        void this.submitAction(acctAction, button.dataset.wbAcctTarget ?? "");
        return;
      }
      // Share lane lifecycle (A36-1 block 1): all five actions arm a confirm
      // first — backend_login pops a real browser profile, start/stop touch
      // the share chain, estop/retry_stop are the stop paths. The armed
      // button keeps its base label in dataset for the confirm round-trip.
      const shareAction = button.dataset.wbShareAction;
      if (shareAction) {
        if (this.shareConfirm === button && this.shareArmed?.key === shareConfirmKey(button)) {
          this.shareConfirm = undefined;
          this.shareArmed = null;
          button.classList.remove("wb-confirm");
          button.textContent = button.dataset.wbBaseLabel ?? button.textContent ?? "";
          delete button.dataset.wbBaseLabel;
          void this.submitAction(shareAction, "share");
        } else {
          this.disarmShare();
          this.shareConfirm = button;
          button.dataset.wbBaseLabel ??= button.textContent ?? "";
          button.classList.add("wb-confirm");
          button.textContent =
            shareAction === "share.estop" ? "确认紧急停止？"
            : shareAction === "share.stop" ? "确认停止分享？"
            : shareAction === "share.retry_stop" ? "确认继续关闭？"
            : shareAction === "share.backend_login" ? "确认打开专用账号登录？"
            : shareAction === "share.backend_reauthenticate" ? "确认仅重新认证 8110？"
            : shareAction === "share.backend_restart" ? "确认仅恢复 / 重启 8110？"
            : shareAction === "share.gateway_restart" ? "确认仅恢复 / 重启 8100？"
            : "确认开启分享？";
          this.shareArmed = { key: shareConfirmKey(button), text: button.textContent ?? "" };
        }
        return;
      }
      const shareKeyAction = button.dataset.wbShareKey;
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
      if (shareKeyAction === "cancel-create" || shareKeyAction === "modal-complete") {
        this.closeShareKeyModal();
        return;
      }
      if (shareKeyAction === "submit-create") {
        const form = button.closest("form");
        const name = form?.querySelector<HTMLInputElement>("[data-wb-share-key-name]")?.value.trim() ?? "";
        const capRaw = form?.querySelector<HTMLInputElement>("[data-wb-share-key-cap]")?.value ?? "";
        const cap = Number(capRaw);
        if (!name) { this.shareKeyError = "名称不能为空"; this.renderPanel(); return; }
        if (capRaw !== "" && (!Number.isInteger(cap) || cap < 1 || cap > 64)) { this.shareKeyError = "每 Key 并发需为 1–64 整数"; this.renderPanel(); return; }
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
      if (shareKeyAction === "rotate" || shareKeyAction === "revoke-terminate" || shareKeyAction === "delete") {
        if (this.shareConfirm === button && this.shareArmed?.key === shareConfirmKey(button)) {
          this.shareConfirm = undefined;
          this.shareArmed = null;
          button.classList.remove("wb-confirm");
          button.textContent = button.dataset.wbBaseLabel ?? button.textContent ?? "";
          delete button.dataset.wbBaseLabel;
          void this.shareKeyOp(shareKeyAction, button.dataset.wbShareKeyId ?? "");
        } else {
          this.disarmShare();
          this.shareConfirm = button;
          button.dataset.wbBaseLabel ??= button.textContent ?? "";
          button.classList.add("wb-confirm");
          button.textContent =
            shareKeyAction === "rotate" ? "确认轮换？（旧明文立即失效）"
            : shareKeyAction === "revoke-terminate" ? "确认撤销并终止该 Key 在途请求？"
            : "确认删除该 Key？";
          this.shareArmed = { key: shareConfirmKey(button), text: button.textContent ?? "" };
        }
        return;
      }
      if (shareKeyAction === "toggle-enable") {
        const row = this.shareKeyRow(button.dataset.wbShareKeyId ?? "");
        if (!row) return;
        void this.patchShareKey(row.id ?? "", { enabled: !row.enabled }, Number(row.row_version));
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
        const cached = this.shareKeySecrets.get(button.dataset.wbShareKeyId ?? "");
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
          if (result) { void result.then(done, done); } else { done(); }
        } catch { done(); }
        return;
      }
      if (shareKeyAction === "rename") {
        const row = this.shareKeyRow(button.dataset.wbShareKeyId ?? "");
        if (!row) return;
        // Inline edit (R9-R46): the name cell swaps to an input — no
        // window.prompt.
        this.shareKeyEdit = { id: row.id ?? "", field: "name", value: row.name ?? "", focusPending: true };
        this.renderPanel();
        return;
      }
      if (shareKeyAction === "adjust-cap") {
        const row = this.shareKeyRow(button.dataset.wbShareKeyId ?? "");
        if (!row) return;
        this.shareKeyEdit = { id: row.id ?? "", field: "cap", value: row.max_concurrency === undefined ? "" : String(row.max_concurrency), focusPending: true };
        this.renderPanel();
        return;
      }
      if (shareKeyAction === "cancel-edit") {
        this.shareKeyEdit = undefined;
        this.renderPanel();
        return;
      }
      if (shareKeyAction === "save-edit") {
        const edit = this.shareKeyEdit;
        const row = edit ? this.shareKeyRow(edit.id) : undefined;
        if (!edit || !row) { this.shareKeyEdit = undefined; this.renderPanel(); return; }
        const raw = (button.closest("td")?.querySelector<HTMLInputElement>("input")?.value ?? edit.value).trim();
        if (edit.field === "name") {
          if (!raw || raw === (row.name ?? "")) { this.shareKeyEdit = undefined; this.renderPanel(); return; }
          this.shareKeyEdit = undefined;
          void this.patchShareKey(row.id ?? "", { name: raw }, Number(row.row_version));
          return;
        }
        const cap = Number(raw);
        if (!Number.isInteger(cap) || cap < 1 || cap > 64) {
          this.shareKeyError = "每 Key 并发需为 1–64 整数";
          this.renderPanel();
          return;
        }
        if (raw === String(row.max_concurrency ?? "")) { this.shareKeyEdit = undefined; this.renderPanel(); return; }
        this.shareKeyEdit = undefined;
        void this.patchShareKey(row.id ?? "", { max_concurrency: cap }, Number(row.row_version));
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
      if (button.dataset.wbShareSecretCopy !== undefined && this.sharePlaintext) {
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
      if (button.dataset.wbShareRefresh !== undefined) {
        // Manual refetch of both share reads (lane-title button / error
        // retry): same read-only path as lane entry, never an operation row.
        this.refreshShareData();
        return;
      }
      if (button.dataset.wbProbeEntry !== undefined) {
        // R11-8 (Plan-Revision 11): the read-only context-probe queue gets an
        // entry AT the catalog point of use — one click switches the section
        // AND fires the read-only preview read (no billing; real billed probes
        // keep their existing confirm + budget gate in the probe section).
        this.modelSection = "probe";
        this.renderPanel();
        if (this.actionResults["model.context_probe_preview"] === undefined) {
          void this.submitAction("model.context_probe_preview", "model", {});
        }
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
        if (this.shareKeyCompare.has(shareCompare)) this.shareKeyCompare.delete(shareCompare);
        else if (this.shareKeyCompare.size < TOKEN_SERIES_MAX) this.shareKeyCompare.add(shareCompare);
        this.renderPanel();
        this.root.querySelector<HTMLElement>(`[data-wb-share-compare="${CSS.escape(shareCompare)}"]`)?.focus({ preventScroll: true });
        return;
      }
      const modelPick = button.dataset.wbModelPick;
      if (modelPick !== undefined) {
        // F12-5: a row's details open in the detail pane beside the table; focus
        // stays on the row's own button so the keyboard keeps its place.
        this.modelPick = modelPick;
        this.renderPanel();
        this.root.querySelector<HTMLElement>(`[data-wb-model-pick="${CSS.escape(modelPick)}"]`)?.focus({ preventScroll: true });
        return;
      }
      const effortAction = button.dataset.wbEffortAction;
      if (effortAction === "use-source" || effortAction === "use-verified") {
        const effortKey = button.dataset.wbEffortKey ?? "";
        if (!effortKey) return;
        if (effortAction === "use-source") this.effortRestored.add(effortKey);
        else this.effortRestored.delete(effortKey);
        this.renderPanel();
        return;
      }
      if (effortAction === "validate") {
        // 校验 = a billed capability probe: this entry only switches to the probe
        // section (queue and budget, read-only). The separately confirmed probe
        // action arrives with the backend (Rev12); no paid path is created here.
        this.modelSection = "probe";
        this.renderPanel();
        return;
      }
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
      if (modelSection === "catalog" || modelSection === "sync" || modelSection === "probe") {
        this.modelSection = modelSection;
        this.renderPanel();
        return;
      }
      const modelAction = button.dataset.wbModelAction;
      // Rev17 单网关模型页：按来源的同步入口。范围参数显式（scope_mode=selected
      // + sources=[本来源]），来源值只接受后端服务身份表内的键，不按数据属性
      // 发散；预览与应用共用 `gwScopeParams()` 一个构造器。
      const gwSync = button.dataset.wbGwSync;
      if (gwSync === "preview" || gwSync === "apply") {
        const source = button.dataset.wbGwSource ?? "";
        if (!GATEWAY_MODEL_SOURCES.has(source)) return;
        if (gwSync === "preview") {
          void this.submitAction("model.sync_preview", "model", this.gwScopeParams(source));
        } else if (this.isArmed(button)) {
          this.clearConfirm();
          delete button.dataset.wbBaseLabel;
          const scope = this.gwScopeParams(source);
          this.applyScopeParams = scope;
          void this.submitAction("model.sync_apply", "model", scope);
        } else {
          this.armConfirmLabel(button, "确认应用本来源同步？（会写入客户端配置，不发起探测）");
        }
        return;
      }
      // Rev8 模型设置：保存 / 恢复默认 / 预览 / 应用（model.settings_save /
      // settings_preview / settings_apply）。保存只写草稿；预览绑定草稿版本并回
      // preview_token；应用带回该 selection 与 token，先二次确认；ZCode 运行中
      // 整体延期由后端返回如实显示。
      const msAction = button.dataset.wbMsAction;
      if (msAction) {
        const key = button.dataset.wbMsKey ?? "";
        const at = key.indexOf("|");
        if (at <= 0) return;
        const source = key.slice(0, at), modelId = key.slice(at + 1);
        if (msAction === "read") {
          if (this.actionInFlight("model.context_overrides")) return;
          void this.submitAction("model.context_overrides", "model");
          return;
        }
        if (msAction === "save" || msAction === "restore") {
          if (this.actionInFlight("model.settings_save")) return;
          const fields = msAction === "restore"
            ? { alias: null, contextSource: null, contextTokens: null, defaultEffort: null }
            : this.msFormFields(key);
          if (!fields) return;
          const params: Record<string, unknown> = { source, model_id: modelId, ...fields };
          const expected = this.msExpectedDraftVersion(key);
          if (expected !== null) params.expected_draft_version = expected;
          void this.submitAction("model.settings_save", "model", params);
          return;
        }
        if (msAction === "preview") {
          if (this.actionInFlight("model.settings_preview")) return;
          const version = this.msDraftVersion(key);
          if (version === null) return;
          void this.submitAction("model.settings_preview", "model", { selection: { [key]: version } });
          return;
        }
        if (msAction === "apply") {
          if (this.actionInFlight("model.settings_apply")) return;
          const preview = this.msPreviewFor(key);
          if (!preview) return;
          if (this.isArmed(button)) {
            this.clearConfirm();
            delete button.dataset.wbBaseLabel;
            void this.submitAction("model.settings_apply", "model",
              { selection: preview.selection, preview_token: preview.token });
          } else {
            this.armConfirmLabel(button, "确认应用到客户端？（写客户端配置；ZCode 运行中：安装版已准入时热写，未准入时整体延期）");
          }
          return;
        }
        return;
      }
      // 模型详情内的证据读取：只读 model.context_probe_preview（与探测队列同一个
      // 只读预览读口），按钮只触发读取，绝不发计费探测。
      if (button.dataset.wbEvidenceRead !== undefined) {
        if (this.actionInFlight("model.context_probe_preview")) return;
        void this.submitAction("model.context_probe_preview", "model", {});
        return;
      }
      if (modelAction === "model.sync_apply") {
        // 应用同步从不探测（Rev12：同步与计费验证彻底分开，「应用并探测」组合
        // 入口已删除）；probe 显式传 false。范围参数只传递自定义面板勾选的内容，
        // 且与应用前预览用的是**同一个构造器** `modelScopeParams()`。
        if (this.isArmed(button)) {
          this.clearConfirm();
          delete button.dataset.wbBaseLabel;
          // 提交时范围快照：终态后的自动回读按**这一次提交**的范围发起
          // （同范围回读）；快照在这里取，确认期间勾选再变也不会改写它。
          const scope = this.modelScopeParams();
          this.applyScopeParams = scope;
          void this.submitAction("model.sync_apply", "model", scope);
        } else {
          this.armConfirmLabel(button, "确认应用同步？（会写入客户端配置，不发起探测）");
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
      if (button.dataset.wbHealthRefresh !== undefined) {
        // 手动重探：一次有界只读 GET（?refresh=1），在途守卫吸收重复点击。
        void this.manualReprobe();
        return;
      }
      if (button.dataset.wbStatsRun !== undefined) {
        // 任意区间聚合：只有显式提交才发一次只读 GET（入页读取与 30s
        // 轮询都不带 from/to，所以区间读取不会被轮询放大）。
        const from = this.root.querySelector<HTMLInputElement>("[data-wb-stats-from]")?.value ?? "";
        const to = this.root.querySelector<HTMLInputElement>("[data-wb-stats-to]")?.value ?? "";
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
          if (this.tokenSourceHidden.has(src)) this.tokenSourceHidden.delete(src);
          else this.tokenSourceHidden.add(src);
        } else if (this.tokenSeriesOpen.has(key)) this.tokenSeriesOpen.delete(key);
        else if (this.tokenSeriesOpen.size < TOKEN_SERIES_MAX) this.tokenSeriesOpen.add(key);
        this.renderPanel();
        this.root.querySelector<HTMLElement>(`[data-wb-series-toggle="${CSS.escape(key)}"]`)?.focus({ preventScroll: true });
        return;
      }
      const tokGroup = button.dataset.wbTokGroup;
      if (tokGroup === "source" || tokGroup === "model") {
        this.tokenGroup = tokGroup;
        this.renderPanel();
        this.root.querySelector<HTMLElement>(`[data-wb-tok-group="${tokGroup}"]`)?.focus({ preventScroll: true });
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
      if (modelAction === "model.sync_preview") {
        // 预览变更 and 应用同步 MUST share ONE scope constructor and ONE
        // transport encoding: the preview used to take the generic branch with
        // NO scope params, so it recomputed the DEFAULT full plan while the
        // apply wrote only the checked subset — the preview's face and the
        // written face disagreed. Both now submit `modelScopeParams()`, and the
        // preview result lands with its submit-time snapshot
        // (`previewScopePending` → `previewScopeParams`) so the panel can
        // say which range it was computed with and mark itself stale when the
        // checkboxes move afterwards.
        void this.submitAction("model.sync_preview", "model", this.modelScopeParams());
        return;
      }
      if (modelAction) { void this.submitAction(modelAction, "model"); return; }
      if (button.dataset.wbSvcDetail !== undefined) {
        const id = Number(button.dataset.wbDetail);
        if (this.lane === 0) {
          // Overview master-detail: a click selects the chain the panel shows and
          // never toggles it off. Keyboard position survives the repaint when the
          // click came from the list itself.
          const fromList = button.dataset.wbMdItem !== undefined;
          this.ovService = id;
          this.renderPanel();
          if (fromList) this.root.querySelector<HTMLElement>(`[data-wb-md-item="${id}"]`)?.focus();
          return;
        }
        // Off the overview (Rev17): a chain's detail lives in the gateway workspace.
        if (CHAINS.some(s => s.id === id)) { this.gwSel = id; this.gwTab = "account"; this.gwNarrowDetail = true; this.select(1); }
        return;
      }
      if (button.dataset.wbLane !== undefined) { this.select(+button.dataset.wbLane); onLane(this.lane); }
    });
    // P2 (2026-09-26): the key-create modal's Escape and Tab contract lives
    // at DOCUMENT level. The backdrop's own keydown only fires while focus is
    // INSIDE the modal, and the modal is the last node of `.wb-main` — Tab from
    // 取消 walked out to the page's nav (取消 → body → 总览 → 账号 …), after which
    // Escape could no longer close the dialog at all. Escape now closes wherever
    // focus is (and hands focus back to 新增密钥), and Tab cycles inside the
    // dialog, pulling focus back if it ever left.
    document.addEventListener("keydown", event => {
      if (!this.shareKeyFormOpen) return;
      const backdrop = this.root.querySelector<HTMLElement>(".wb-share-modal-backdrop");
      if (!backdrop || backdrop.hidden) return;
      if (event.key === "Escape") {
        event.preventDefault();
        this.closeShareKeyModal();
        return;
      }
      if (event.key !== "Tab") return;
      // A hidden view's controls are not tab stops: the form view and the
      // success view are two faces of the same dialog, and only one is shown.
      const stops = [...backdrop.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
        .filter(element => !element.closest("[hidden]"));
      if (!stops.length) return;
      const first = stops[0]!, last = stops[stops.length - 1]!;
      const active = document.activeElement;
      const inside = active instanceof HTMLElement && backdrop.contains(active);
      if (inside && (event.shiftKey ? active !== first : active !== last)) return;
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    });
    this.apply({});
    // 契约 3.12c.4：折叠状态必须在重绘后保持。原生 <details> 的开合由浏览器
    // 完成，但 renderPanel 会重建这段 HTML（30s 轮询与切 lane 都重绘），所以
    // 开合态写进类字段：点击时先记录目标态，重绘后由字段还原。九个折叠宿主
    // 都在这里登记——分享页的准备条件与「每 Key 明细」原先只有状态字段却没有任何
    // 写入者，于是每次重绘都折回默认态；模型页「同步」分区的「探测队列」与
    // 「同步任务」原先既没有状态字段也没有宿主，同样每次都折回默认关闭；账号页
    // 四张卡各自的折叠（维护者 2026-09-29）按服务 id 记在 `accountFoldOpen`；
    // 原先不可达——缺的正是下面选择器清单里的宿主项，`modelIdDetailKey` 因此恒为空、
    // 每次重绘都折回默认态（维护者 2026-09-29 门禁定向复现），本轮一并接上。
    this.root.addEventListener("click", event => {
      const target = event.target as Element;
      const summary = target.closest?.(
        "[data-wb-probe-intro] > summary, [data-wb-cred-details] > summary, [data-wb-share-preconditions] > summary, [data-wb-share-key-details] > summary, [data-wb-sync-tasks] > summary, [data-wb-model-pause] > summary, [data-wb-account-fold] > summary, [data-wb-note-fold] > summary");
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
        const host = row?.querySelector<HTMLDetailsElement>("details[data-wb-share-key-details]");
        if (host && !inside) {
          if (host.open) this.shareKeyDetailsOpen.delete(host.dataset.wbShareKeyDetails!);
          else this.shareKeyDetailsOpen.add(host.dataset.wbShareKeyDetails!);
          host.open = !host.open;
        }
        return;
      }
      const host = summary.parentElement as HTMLDetailsElement | null;
      if (!host) return;
      // 点击时 `open` 仍是切换前的值，所以目标态是它的反面。这里只记录状态，
      // 不在这里重绘：重绘会把原生刚切换的开合态再折回默认态。
      if (host.dataset.wbProbeIntro !== undefined) this.probeIntroOpen = !host.open;
      else if (host.dataset.wbCredDetails !== undefined) this.credDetailsOpen = !host.open;
      else if (host.dataset.wbSharePreconditions !== undefined) this.sharePreconditionsOpen = !host.open;
      else if (host.dataset.wbShareKeyDetails !== undefined) {
        if (host.open) this.shareKeyDetailsOpen.delete(host.dataset.wbShareKeyDetails);
        else this.shareKeyDetailsOpen.add(host.dataset.wbShareKeyDetails);
      }
      else if (host.dataset.wbSyncTasks !== undefined) this.syncTasksOpen = !host.open;
      else
if (host.dataset.wbNoteFold !== undefined) {
        // Lane-level explanation folds (2026-09-29 redesign), keyed by name.
        const key = host.dataset.wbNoteFold;
        if (host.open) this.noteFoldOpen.delete(key);
        else this.noteFoldOpen.add(key);
      }
      else if (host.dataset.wbAccountFold !== undefined) {
        // 账号卡折叠（维护者 2026-09-29）：四张卡各一个宿主，开合态按服务 id 记。
        // 点击时 `open` 仍是切换前的值，所以目标态是它的反面；这里只记状态，
        // 不重绘（重绘会把浏览器刚切换的开合态折回默认态）。
        const id = Number(host.dataset.wbAccountFold);
        if (host.open) this.accountFoldOpen.delete(id);
        else this.accountFoldOpen.add(id);
      }
    });
    window.setInterval(() => { void this.probeTick(); }, 30000);
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
    const jumpInput = this.root.querySelector<HTMLInputElement>(".wb-jump-input");
    const jumpList = this.root.querySelector<HTMLElement>(".wb-jump-list");
    if (jumpInput && jumpList) {
      type JumpEntry = (typeof JUMP_INDEX)[number];
      let jumpHits: JumpEntry[] = [];
      let jumpActive = -1;
      const jumpClose = () => { jumpList.hidden = true; jumpActive = -1; };
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
          if (typeof this.gwSel !== "number") { this.gwSel = MAIN_CHAIN_ID; this.ovService = MAIN_CHAIN_ID; }
          this.gwTab = "account";
          this.gwNarrowDetail = true;
        }
        if (entry.modelSection) this.modelSection = entry.modelSection;
        this.select(entry.lane);
        if (entry.anchor) this.root.querySelector<HTMLElement>(entry.anchor)?.scrollIntoView({ block: "start" });
      };
      const jumpRender = () => {
        const q = jumpInput.value.trim().toLowerCase();
        const hits = (q ? JUMP_INDEX.filter(e => e.label.toLowerCase().includes(q)) : JUMP_INDEX.slice(0, names.length)).slice(0, 8);
        jumpHits = hits;
        jumpActive = -1;
        jumpList.innerHTML = hits.map(e => `<button type="button" role="option" data-jump="${JUMP_INDEX.indexOf(e)}">${escapeHtml(e.label)}</button>`).join("");
        jumpList.hidden = hits.length === 0;
      };
      jumpInput.addEventListener("input", jumpRender);
      jumpInput.addEventListener("keydown", event => {
        if (event.key === "Escape") { jumpInput.value = ""; jumpRender(); return; }
        if (jumpList.hidden) return;
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          const step = event.key === "ArrowDown" ? 1 : -1;
          const count = jumpHits.length;
          if (count) {
            jumpActive = jumpActive < 0 ? (step === 1 ? 0 : count - 1) : (jumpActive + step + count) % count;
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
      jumpList.addEventListener("click", event => {
        const option = (event.target as Element).closest<HTMLElement>("[data-jump]");
        if (!option) return;
        const entry = JUMP_INDEX[Number(option.dataset.jump)];
        if (entry) jumpGo(entry);
      });
      // The delay lets a click on a list option land before the list hides.
      jumpInput.addEventListener("blur", () => { setTimeout(jumpClose, 150); });
      jumpInput.addEventListener("focus", jumpRender);
    }
  }
  private text(propKey: string) { const v = this.props[propKey]?.value; return typeof v === "string" ? v.trim().slice(0, 240) : ""; }
  private taskId(i: number) { return `${i}:${this.text(`task${i + 1}`)}`; }
  private apply(props: ConsoleProperties) {
    Object.assign(this.props, props);
    this.visibility = applyVisibilityProperties(this.visibility, props);
    if (props.desktopmode) this.setEnabled(props.desktopmode.value === "workbench");
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
    }
  }
  syncVisibility() {
    // The shell owns the default surface: it stays hidden during boot, the
    // entry phase (no mode attribute yet) and while a detail file is open,
    // and reappears automatically when the mode falls back to archive.
    // A missing mode attribute means the page has not entered boot yet.
    const mode = this.stage.dataset.mode;
    const hidden = !this.enabled || mode === "boot" || mode === "detail" || !mode;
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
        const animation = this.root.animate([{ opacity: getComputedStyle(this.root).opacity }, { opacity: 0 }], { duration: 220, fill: "forwards", easing: "ease-out" });
        this.exitAnimation = animation;
        animation.onfinish = () => { this.root.hidden = true; animation.cancel(); this.exitAnimation = undefined; };
      }
      return;
    }
    const interrupted = Boolean(this.exitAnimation);
    const opacity = getComputedStyle(this.root).opacity;
    this.exitAnimation?.cancel();
    this.exitAnimation = undefined;
    this.root.hidden = hidden;
    if (interrupted && !hidden && !reduced) this.root.animate([{ opacity }, { opacity: 1 }], { duration: 220, easing: "ease-out" });
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
    if (lane === 0 || lane === 1 || lane === 3 || lane === 4) this.ensureUsageReads();
    if (lane === 1) { this.syncGatewayConnection(); }
    const entering = lane !== this.lane;
    // A log detail opened from another page returns there only until the user
    // navigates elsewhere themselves.
    if (lane !== 4) this.logReturnLane = null;
    // A share confirm armed on the share page does not survive leaving it.
    if (entering) this.disarmShare();
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
    const animation = content.animate([{ opacity: 0.25 }, { opacity: 1 }], { duration: 200, easing: "cubic-bezier(0.2, 0, 0, 1)" });
    animation.onfinish = () => { if (this.laneEnterAnimation === animation) this.laneEnterAnimation = undefined; };
    this.laneEnterAnimation = animation;
  }
  /** One poll cycle: liveness probes + management-session data attempt, then
   *  in-place dot/quote updates (no full re-render, so the rolling panel is
   *  untouched while the user watches). */
  private async probeTick() {
    if (!this.enabled || this.probing) return;
    this.probing = true;
    try {
      // R11-6/B2 (2026-09-23): liveness evidence comes from the 8096
      // backend's server-side loopback probes (/api/console/health —
      // same-origin in both development and production). The browser uses
      // only the backend health reading, so a Running process can never be
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
      const shareRead = this.shareLaneVisited ? this.fetchShareStatus().then(repaint) : Promise.resolve();
      await Promise.all([this.fetchRuntime().then(repaint), this.fetchActions().then(repaint), this.fetchRecentOps().then(repaint), this.fetchPrices().then(repaint), this.fetchLogs().then(repaint), this.fetchStats().then(repaint), shareRead]);
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
   *  short TTL). Same-origin /api path in dev and on the static
   *  production origin (native /api/console route). A failed fetch answers
   *  null — callers keep the previous evidence instead of inventing a
   *  dead verdict (stale ≠ dead). */
  private async fetchHealth(force = false): Promise<{ checked_at: number; services: HealthServiceEntry[] } | null> {
    try {
      // `refresh=1` is the backend's OWN manual re-probe path (usagesrv.py
      // `_console_health_payload(force)`: re-probes instead of serving the
      // cached snapshot, `cached` is then False). It is still one bounded,
      // read-only GET — no operation-ledger row, no write.
      const response = await consoleFetch(`/api/console/health${force ? "?refresh=1" : ""}`,
        { credentials: "include" });
      if (!response.ok) {
        // 实测 HTTP 状态：404 等 = 当前服务端版本未载入该参数；它只对
        // 手动重探有意义，30s 轮询走非 force 路径不受影响。
        if (force) this.healthRefresh = response.status === 404
          ? { state: "unavailable", status: response.status }
          : { state: "failed", status: response.status, reason: `HTTP ${response.status}` };
        return null;
      }
      const data = await response.json() as { checked_at?: number; cached?: boolean; services?: HealthServiceEntry[] };
      if (!Array.isArray(data.services)) {
        if (force) this.healthRefresh = { state: "failed", status: response.status, reason: "响应缺少 services 数组" };
        return null;
      }
      if (force) {
        this.healthRefresh = { state: "ok", at: data.checked_at ?? 0, cached: data.cached === true };
      }
      return { checked_at: data.checked_at ?? 0, services: data.services };
    } catch {
      if (force) this.healthRefresh = { state: "failed", reason: "网络失败或服务未运行" };
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
  private applyHealthSnapshot(health: { checked_at: number; services: HealthServiceEntry[] }) {
    const observedAt = typeof health.checked_at === "number" && health.checked_at > 0
      ? health.checked_at * 1000
      : Date.now();
    for (const s of SERVICES) {
      const healthId = HEALTH_ID_BY_SERVICE[s.id];
      const entry = health.services.find(x => x.id === healthId);
      if (entry) {
        this.live.set(s.id, { ...this.healthToProbe(entry), observedAt });
      } else {
        this.live.set(s.id, {
          alive: false, state: "unknown", ms: 0, observedAt,
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
      return { alive: false, note: `端口 ${entry.port} 未监听（服务端探测）`, ms, status: undefined };
    }
    if (entry.state === "unknown") {
      // Honest middle state (connect timeout ≠ refused): the card shows the
      // probe's own note, and the dot stays grey — never a fabricated dead
      // verdict (P1, 2026-09-24).
      return { alive: false, state: "unknown", note: entry.note ?? `端口 ${entry.port} 探测未定（服务端探测）`, ms, status: undefined };
    }
    if (typeof entry.http_status === "number") {
      return { alive: true, note: `HTTP ${entry.http_status} · 服务端探测命中`, ms, status: entry.http_status };
    }
    return { alive: true, note: entry.note ?? `TCP 可达（服务端探测）`, ms, status: undefined };
  }
  /** Runtime service snapshot; only exists inside a management session. */
  private async fetchRuntime() {
    try {
      const response = await consoleFetch("/api/console/services", { credentials: "include" });
      if (!response.ok) { this.runtime = undefined; return; }
      const data = await response.json();
      const services = data?.services;
      if (!Array.isArray(services)) { this.runtime = undefined; return; }
      const map: Record<string, RuntimeEntry> = {};
      for (const entry of services) {
        if (entry && typeof entry === "object" && typeof (entry as RuntimeEntry).key === "string") {
          map[(entry as RuntimeEntry).key] = entry as RuntimeEntry;
        }
      }
      this.runtime = map;
    } catch {
      this.runtime = undefined;
    }
  }
  /** Price table (R9-R29): the meter's own prices.json via the console API —
   *  the fee page shows exactly what the billing engine charges, not a copy. */
  private async fetchPrices() {
    try {
      const response = await consoleFetch("/api/console/prices", { credentials: "include" });
      if (!response.ok) { this.priceTable = undefined; return; }
      const data = await response.json();
      if (!data || typeof data !== "object" || data.ok !== true) { this.priceTable = undefined; return; }
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
      const response = await consoleFetch(
        `/api/console/logs?limit=${LOG_FEED_LIMIT}&sources=${LOG_FEED_SOURCES.join(",")}`,
        { credentials: "include" });
      if (!response.ok) {
        // 实测 HTTP 状态照原样带进 `logsFeed`：首层只写这一条实测原因，
        // 不把「被拒绝」推断成「无凭据会话或 8096 不可达」。
        this.logsFeed = { state: "gated", rows: [], reason: `HTTP ${response.status}` };
        return;
      }
      const data = await response.json();
      const logs = data?.logs;
      if (!Array.isArray(logs)) {
        this.logsFeed = { state: "gated", rows: [], reason: "响应不含 logs 数组（形状不符）" };
        return;
      }
      const unavailable = data?.sources_unavailable;
      this.logsFeed = {
        state: "live", rows: logs as LogEntry[],
        // 服务端未声明时不造覆盖结论：缺字段 = 覆盖未知（不是「全部覆盖」）
        unavailable: Array.isArray(unavailable) && unavailable.every(s => typeof s === "string")
          ? unavailable as string[] : undefined,
      };
    } catch {
      // 传输失败同样带实测原因：未知 ≠ 零记录，也不冒充「无凭据会话」。
      this.logsFeed = { state: "gated", rows: [], reason: "网络失败或服务未运行" };
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
      const response = await consoleFetch("/api/console/log-sources", { credentials: "include" });
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
        return typeof row?.source === "string" && PUBLIC_LOG_SOURCES.has(row.source) && Array.isArray(row?.categories);
      }) as LogSourceRow[];
      // Rev12 F12-6: the log page shows runtime logs only. A source's JSONL call
      // ledger (category "ledger") is usage, read on the fee lane's call detail,
      // so it is not offered as a log category here.
      this.logSources = rows.map(row => ({ ...row, categories: row.categories.filter(c => c.category !== "ledger") }));
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
      if (this.logDetail?.loading && this.logDetail.source === pending.source) this.logDetail = undefined;
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
    const reads = this.logSources.flatMap(source => source.categories.slice(0, 1).map(category => ({ source, category })));
    // Each endpoint already enforces a bounded, redacted tail. No pagination or polling.
    try {
      for (let i = 0; i < reads.length; i += 3) {
        await Promise.all(reads.slice(i, i + 3).map(async ({ source, category }) => {
          let snapshot: { status: string; rows: LogEntryRow[]; truncated: boolean };
          const controller = new AbortController();
          const timeout = window.setTimeout(() => controller.abort(), 10000);
          try {
            const params = new URLSearchParams({ source: source.source, category: category.category });
            const response = await consoleFetch(`/api/console/log-entries?${params}`, { credentials: "include", signal: controller.signal });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const result = await response.json() as LogEntriesResult;
            if (!Array.isArray(result.rows) || typeof result.status !== "string" || result.rows.some(row => !row || typeof row.message !== "string" || typeof row.parse_ok !== "boolean")) throw new Error("响应形状不符");
            snapshot = { status: result.status, rows: result.rows, truncated: result.truncated };
          } catch (error) {
            snapshot = { status: error instanceof Error ? error.message : "读取失败", rows: [], truncated: false };
          } finally { window.clearTimeout(timeout); }
          if (seq === this.logOverviewSeq) this.logOverview.set(source.source, snapshot);
        }));
      }
    } finally {
      if (seq === this.logOverviewSeq) {
        this.logOverviewLoading = false;
        this.logOverviewCheckedAt = new Date().toLocaleTimeString("zh-CN");
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
      const response = await consoleFetch(`/api/console/log-entries?${params}`, { credentials: "include" });
      if (!response.ok) { throw new Error(String(response.status)); }
      const data = await response.json() as LogEntriesResult;
      if (seq !== this.logDetailSeq) return; // superseded by a source flip
      if (!data || typeof data !== "object" || !data.status) { throw new Error("shape"); }
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
          detail.newCount += rows.filter(r => r.ts && r.ts > oldTop).length;
        }
      }
      detail.nextCursor = data.next_cursor ?? null;
      detail.requestEcho = detail.requestId
        ? (data.request_id === detail.requestId && data.request_scope === "read_window" ? "applied" : "unconfirmed")
        : "";
      detail.status = data.status;
      detail.errorCode = data.error ?? "";
      detail.scannedBytes = data.scanned_bytes ?? 0;
      detail.truncated = Boolean(data.truncated);
      detail.resetHint = Boolean(data.reset_hint);
      if (data.reset_hint) { detail.rows = []; detail.nextCursor = null; }
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
    const key = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const from = key(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 6));
    try {
      const response = await consoleFetch(`/api/console/stats?from=${from}&to=${key(now)}`, { credentials: "include" });
      if (!response.ok) {
        // 实测 HTTP 状态：404 = 当前服务端版本未载入该端点，其余为读取失败。
        this.statsWeekError = response.status === 404
          ? `HTTP 404 · 当前服务端版本未载入 /api/console/stats`
          : `HTTP ${response.status}`;
        return;
      }
      const data = await response.json();
      if (!data || typeof data !== "object" || !data.totals) { this.statsWeekError = "响应缺少 totals"; return; }
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
    const fail = (state: "rejected" | "failed", status: number, reason: string) => {
      this.statsRange = state === "rejected"
        ? { state: "rejected", from, to, status, reason }
        : { state: "failed", from, to, status, reason };
      this.renderPanel();
    };
    const day = (s: string) => /^\d{4}-\d{2}-\d{2}$/.test(s) ? Date.parse(`${s}T00:00:00Z`) : NaN;
    if (!from || !to) return fail("failed", 0, "请先选择起始日与结束日");
    const lo = day(from), hi = day(to);
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return fail("failed", 0, "日期格式需为 YYYY-MM-DD");
    if (lo > hi) return fail("failed", 0, "起始日晚于结束日");
    // 与后端 MAX_RANGE_DAYS 同一口径：超过 92 天由后端返回 400，前端先如实说明。
    const spanDays = Math.round((hi - lo) / 86400000) + 1;
    if (spanDays > STATS_RANGE_MAX_DAYS) {
      return fail("failed", 0, `区间 ${spanDays} 天超过 ${STATS_RANGE_MAX_DAYS} 天上限（后端会以 400 拒绝）`);
    }
    if (this.statsRangeInFlight) return;
    this.statsRangeInFlight = true;
    this.statsRange = { state: "loading", from, to };
    this.renderPanel();
    try {
      const response = await consoleFetch(`/api/console/stats?from=${from}&to=${to}`, { credentials: "include" });
      const body = await response.json().catch(() => null) as { error?: string; detail?: string } | null;
      if (!response.ok) {
        // 400 = 后端自己的区间校验拒绝；404 = 当前服务端版本未载入该端点。
        const reason = body?.detail || body?.error || `HTTP ${response.status}`;
        this.statsRange = response.status === 400
          ? { state: "rejected", from, to, status: response.status, reason }
          : { state: "failed", from, to, status: response.status, reason };
        return;
      }
      if (!body || typeof body !== "object" || !Array.isArray((body as StatsRangePayload).days)) {
        this.statsRange = { state: "failed", from, to, status: response.status, reason: "响应缺少 days 数组" };
        return;
      }
      this.statsRange = { state: "live", from, to, status: response.status, payload: body as StatsRangePayload };
    } catch {
      this.statsRange = { state: "failed", from, to, reason: "网络失败或服务未运行" };
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
      const response = await consoleFetch("/api/console/usage-tokens?days=14",
        { credentials: "include" });
      if (!response.ok) {
        this.usageTokens = this.usageKeep(this.usageTokens, `HTTP ${response.status}`);
        return;
      }
      const data = await response.json() as UsageTokensPayload | null;
      if (!data || !Array.isArray(data.days) || !Array.isArray(data.missing)) {
        this.usageTokens = this.usageKeep(this.usageTokens, "响应形状与预期不符");
        return;
      }
      this.usageTokens = { state: "live", payload: data, checkedAt: this.usageStamp(data.checked_epoch) };
    } catch {
      this.usageTokens = this.usageKeep(this.usageTokens, "网络失败或服务未运行");
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
      const response = await consoleFetch("/api/console/usage-rates?hours=24",
        { credentials: "include" });
      if (!response.ok) {
        this.usageRates = this.usageKeep(this.usageRates, `HTTP ${response.status}`);
        return;
      }
      const data = await response.json() as UsageRatesPayload | null;
      if (!data || !Array.isArray(data.hours) || !data.skipped) {
        this.usageRates = this.usageKeep(this.usageRates, "响应形状与预期不符");
        return;
      }
      this.usageRates = { state: "live", payload: data, checkedAt: this.usageStamp(data.checked_epoch) };
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
    if (force || !this.usageTokens) void this.fetchUsageTokens().then(() => this.renderPanel());
    if (force || !this.usageRates) void this.fetchUsageRates().then(() => this.renderPanel());
  }
  /** Capture stamp of one usage payload: the server's own checked_epoch when it
   *  carries a usable one, else this read's clock (never a fabricated value). */
  private usageStamp(epoch?: number): number {
    return typeof epoch === "number" && epoch > 0 ? epoch : Math.floor(Date.now() / 1000);
  }
  /** A failed re-read keeps the last REAL payload; without one there is
   *  nothing honest to show but the failure itself. */
  private usageKeep<T>(prev: UsageRead<T> | undefined, reason: string): UsageRead<T> {
    return prev?.payload
      ? { state: "stale", payload: prev.payload, checkedAt: prev.checkedAt, reason }
      : { state: "error", reason };
  }
  /** Honest read line shared by both usage reads: WHEN it was read, whether it
   *  is a kept stale payload, or why nothing could be read at all. */
  private usageReadNote(read: UsageRead<unknown> | undefined, what: string): string {
    if (!read) return `${what}尚未读取。`;
    if (read.state === "loading") return `${what}读取中…`;
    if (read.state === "error") return `${what}读取失败（${read.reason ?? "原因未知"}）；不以 0 或空表冒充。`;
    const at = read.checkedAt ? new Date(read.checkedAt * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "未知";
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
  private usageCoverageHtml(coverage: UsageCoverage | undefined, what: string): string {
    const head = `数据覆盖（${escapeHtml(what)} · 服务端 coverage）`;
    const sources = coverage?.sources;
    if (!coverage || !sources || typeof sources !== "object" || Array.isArray(sources) || !Object.keys(sources).length) {
      return `<p class="wb-muted">${head}：覆盖信息未提供（后端未返回 coverage.sources）；账本是否被读取未知，不按零用量推断。</p>`;
    }
    const parts = Object.keys(sources).sort().map(src => {
      const cov = sources[src] ?? {};
      const present = cov.ledger_present === true ? "账本已读取" : cov.ledger_present === false ? "账本缺席" : "账本状态未知";
      const enabled = cov.enabled === true ? "来源已启用" : cov.enabled === false ? "来源未启用" : "启用状态未知";
      const events = typeof cov.events === "number" ? `事件 ${cov.events} 条` : "事件数未知";
      const errs = Array.isArray(cov.errors) && cov.errors.length
        ? `，覆盖错误 ${cov.errors.map(e => {
            const type = typeof e?.type === "string" ? e.type : "unknown";
            const count = [e?.bad_lines, e?.count, e?.failed_records, e?.failed_rows].find(v => typeof v === "number");
            return typeof count === "number" ? `${type}(${count})` : type;
          }).join("、")}`
        : "";
      const note = typeof cov.note === "string" && cov.note.trim() ? `：${cov.note.trim()}` : "";
      // 时间戳无效的记录（missing[].reason=invalid_time / skipped.invalid_time）在覆盖里
      // 也单列：它们不归任何一天，非零时显示，零不显示。
      const undated = typeof cov.undated_records === "number" && cov.undated_records > 0
        ? `，时间无法判定 ${cov.undated_records} 条（时间戳无效，不归任何一天）`
        : "";
      return `${escapeHtml(this.sourceLabel(src))} ${escapeHtml(present)} · ${escapeHtml(enabled)} · ${escapeHtml(events)}${escapeHtml(errs)}${escapeHtml(undated)}${escapeHtml(note)}`;
    });
    const marks: string[] = [];
    if (coverage.truncated === true) marks.push("truncated（结果被截断）");
    if (coverage.clamped === true) marks.push("clamped（请求窗口已收口到上限）");
    if (typeof coverage.dropped_rows === "number" && coverage.dropped_rows > 0) marks.push(`dropped_rows=${coverage.dropped_rows}`);
    // F3a：days[] 与 missing[] 各自按行数上限截断，两个丢弃数分别披露（各列表
    // 单独计数，缺分量行被丢弃不会伪装成「该窗口没有缺分量」）。
    if (typeof coverage.dropped_missing_rows === "number" && coverage.dropped_missing_rows > 0) {
      marks.push(`dropped_missing_rows=${coverage.dropped_missing_rows}（缺失行被截断丢弃 ${coverage.dropped_missing_rows} 条）`);
    }
    const markText = marks.length ? ` 截断标记：${escapeHtml(marks.join(" · "))}。` : "";
    return `<p class="wb-muted">${head}：${parts.join("；")}。${markText}「账本缺席 / 来源未启用」= 没有可读数据，与「该窗口确实无用量」不同；两者都不按 0 显示。</p>`;
  }
  /** Session-derived reads gate (ops ledger live or runtime present). */
  private sessionLive(): boolean {
    return this.opsRecent?.state === "live" || Boolean(this.runtime);
  }
  /** Action registry (service.start/stop enablement + honest reasons). */
  private async fetchActions() {
    try {
      const response = await consoleFetch("/api/console/actions", { credentials: "include" });
      if (!response.ok) { this.actions = undefined; return; }
      const data = await response.json();
      const actions = data?.actions;
      if (!Array.isArray(actions)) { this.actions = undefined; return; }
      this.actions = actions.filter((a: unknown) => {
        const row = a as ActionRow;
        return typeof row?.action === "string" && PUBLIC_ACTIONS.has(row.action);
      }) as ActionRow[];
    } catch {
      this.actions = undefined;
    }
  }
  /** Recent real operations from the 8096 ledger (management session). `undefined`
   *  = 本次读取尚未返回（渲染为「读取中」，不是「不可读」）；`gated` = 读取已
   *  失败，`reason` 是实测原因（HTTP 状态 / 形状不符 / 传输失败），所以任何
   *  界面都不必把「8096 应答了错误」写成「不可达」。 */
  private async fetchRecentOps() {
    try {
      const response = await consoleFetch("/api/console/operations?limit=8", { credentials: "include" });
      if (!response.ok) { this.opsRecent = { state: "gated", rows: [], reason: `HTTP ${response.status}` }; return; }
      const data = await response.json();
      const operations = data?.operations;
      if (!Array.isArray(operations)) { this.opsRecent = { state: "gated", rows: [], reason: "响应形状与契约不符（缺 operations[]）" }; return; }
      this.opsRecent = {
        state: "live",
        rows: operations.filter((op: unknown) => {
          const row = op as OpRow;
          return typeof row?.id === "string" && typeof row?.action === "string" && typeof row?.state === "string";
        }) as OpRow[],
      };
    } catch {
      this.opsRecent = { state: "gated", rows: [], reason: "网络失败或服务未运行" };
    }
  }
  /** 操作账本不可读时的实测原因（与首层横幅同一口径）：应答了 HTTP 错误就写
   *  应答错误，只有 fetch 本身失败才是传输层失败——客户端没测到「不可达」。 */
  private opsReadFailureText(): string {
    const reason = this.opsRecent?.reason;
    return `8096 操作账本读取失败（${reason || "原因未知"}）`;
  }
  /** Share facade reads (A36-2 narrow facade /api/console/share-management/*).
   *  Honest states only: non-ok / network / shape mismatch = error with its
   *  own reason; never substitutes fixture data (R9-R35 honest-gate rule).
   *  维护者 2026-09-28：非 2xx 的原因按分享失败原因的 ④ 口径取
   *  `shareReadReason()`——后端错误码与实测 HTTP 状态只写一次。 */
  private async fetchShareStatus() {
    try {
      const response = await consoleFetch("/api/console/share-management/status", { credentials: "include" });
      if (!response.ok) {
        const body = await response.json().catch(() => null) as { error?: string; reason?: string } | null;
        this.shareStatus = { state: "error", reason: shareReadReason(body, response.status) };
        return;
      }
      const data = await response.json().catch(() => null) as ShareStatusPayload | null;
      if (!data || typeof data !== "object") {
        this.shareStatus = { state: "error", reason: "响应形状不符" };
        return;
      }
      this.shareStatus = { state: "live", payload: data };
    } catch (error) {
      this.shareStatus = { state: "error", reason: error instanceof Error ? error.message : String(error) };
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
    // P2 (2026-09-24): a row that LEFT the list keeps no cached plaintext —
    // the list read is the authority for which keys still exist, so a deleted or
    // revoked key must not keep its plaintext in memory until a page reload.
    const live = new Set<string>();
    for (const row of rows) {
      const id = row.id ?? "";
      if (!id) continue;
      live.add(id);
      const rowVersion = typeof row.row_version === "number" ? row.row_version : null;
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
      if (cached && cached.rowVersion !== null && rowVersion !== null
          && cached.rowVersion < rowVersion) {
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
  /** 受控的一次性展示：只在用户点该行的「显示明文」时发一次读回。 */
  private revealShareKeySecret(row: ShareKeyRow) {
    const id = row.id ?? "";
    if (!id || row.plaintext_viewable !== true) return;
    if (this.shareKeySecrets.has(id) || this.shareKeySecretBusy.has(id)) return;
    const rowVersion = typeof row.row_version === "number" ? row.row_version : null;
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
  private setShareKeySecret(keyId: string, secret: string, rowVersion: number | null) {
    this.shareKeySecrets.set(keyId, { secret, rowVersion });
    this.shareKeySecretNotes.delete(keyId);
    this.shareKeySecretSeq.set(keyId, (this.shareKeySecretSeq.get(keyId) ?? 0) + 1);
  }
  private async fetchShareKeySecret(keyId: string, rowVersion: number | null): Promise<void> {
    // P1 (2026-09-24): the request owns a sequence number; a response that
    // is no longer the newest for this key is dropped, so a stale read can
    // never overwrite a newer plaintext (rotation or newer read).
    const seq = (this.shareKeySecretSeq.get(keyId) ?? 0) + 1;
    this.shareKeySecretSeq.set(keyId, seq);
    const gate = rowVersion !== null ? `?expected_row_version=${rowVersion}` : "";
    try {
      const response = await consoleFetch(
        `/api/console/share-management/keys/${encodeURIComponent(keyId)}/secret${gate}`,
        { credentials: "include" });
      if ((this.shareKeySecretSeq.get(keyId) ?? 0) !== seq) return;
      if (!response.ok) {
        // P2 (2026-09-24): a refused readback must not leave the row on
        // 读取中… forever — write the honest inline failure state instead.
        this.shareKeySecretNotes.set(keyId, `fetch_failed:HTTP ${response.status}`);
        this.patchShareKeyCell(keyId);
        return;
      }
      const data = await response.json().catch(() => null) as
        { secret?: string; secret_unavailable?: boolean; reason?: string } | null;
      if ((this.shareKeySecretSeq.get(keyId) ?? 0) !== seq) return;
      if (data && typeof data.secret === "string" && data.secret) {
        this.shareKeySecrets.set(keyId, { secret: data.secret, rowVersion });
        this.shareKeySecretNotes.delete(keyId);
      } else if (data?.secret_unavailable === true) {
        // R11-F2: the backend answers distinct inline states — show the
        // reason where the key is, so the row never sticks on 读取中….
        this.shareKeySecrets.delete(keyId);
        this.shareKeySecretNotes.set(keyId, String(data.reason ?? "unavailable"));
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
      `td[data-share-key-cell="${CSS.escape(keyId)}"]`);
    if (!cell) return;
    const row = this.shareKeys?.state === "live"
      ? this.shareKeys.rows.find(r => (r.id ?? "") === keyId) : undefined;
    if (row) cell.innerHTML = this.shareKeyValueCellHtml(row);
  }
  private shareKeyValueCellHtml(row: ShareKeyRow): string {
    const id = row.id ?? "";
    const cached = this.shareKeySecrets.get(id);
    if (cached) {
      return `<code class="wb-share-key-plain">${escapeHtml(cached.secret)}</code> `
        + `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="copy-secret" data-wb-share-key-id="${escapeHtml(id)}" title="复制该 Key 的明文（仅本次会话内存）" aria-label="复制该 Key 的明文">${wbIcon("copy")}${wbIconHint("复制该 Key 的明文")}</button>`;
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
      return `<code>${escapeHtml(row.masked ?? "—")}</code> `
        + `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="reveal-secret" data-wb-share-key-id="${escapeHtml(id)}" title="一次性读回该 Key 的明文（只读、按需一次、仅本页内存）" aria-label="显示该 Key 的明文">${wbIcon("eye")}显示明文</button>`;
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
      const response = await consoleFetch("/api/console/share-management/keys", { credentials: "include" });
      if ((this.shareKeysSeq ?? 0) !== seq) return;
      if (!response.ok) {
        const body = await response.json().catch(() => null) as { error?: string; reason?: string } | null;
        // P2 (2026-09-24): the guard applies after this await too — a
        // superseded read must not overwrite a newer live list with an
        // error state.
        if ((this.shareKeysSeq ?? 0) !== seq) return;
        // 维护者 2026-09-28：与状态读口同一条规则——后端错误码与实测 HTTP 状态只写一次。
        this.shareKeys = { state: "error", reason: shareReadReason(body, response.status) };
        return;
      }
      const data = await response.json().catch(() => null) as { keys?: ShareKeyRow[] } | null;
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
      this.shareKeys = { state: "error", reason: error instanceof Error ? error.message : String(error) };
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
      const response = await consoleFetch("/api/console/share-usage");
      if ((this.shareUsageSeq ?? 0) !== seq) return;
      if (!response.ok) {
        const body = await response.json().catch(() => null) as { error?: string } | null;
        if ((this.shareUsageSeq ?? 0) !== seq) return;
        const code = body?.error || "";
        // 单一原因串，且保留实测 HTTP 状态：后端 error 自身已带状态（例如
        // 「HTTP 500」）时原样使用，否则补一次「（HTTP 500）」；无可读错误体时
        // 只剩「HTTP 500」。四个分支都不再无条件追加第二次状态——原先 error
        // 分支会印出「HTTP 500（HTTP 500）」。
        const reason = code
          ? (code.includes(`HTTP ${response.status}`) ? code : `${code}（HTTP ${response.status}）`)
          : `HTTP ${response.status}`;
        this.shareUsage = response.status === 404
          ? { state: "unavailable", status: response.status, reason: `${reason}（当前服务端版本未载入该读口）` }
          : response.status === 401 || response.status === 403
            ? { state: "refused", reason: `${reason}（本口免凭据；401/403 说明请求打到了分享管理面或别的路由）` }
            : { state: "error", reason };
        return;
      }
      const data = await response.json().catch(() => null) as ShareUsagePayload | null;
      if ((this.shareUsageSeq ?? 0) !== seq) return;
      if (!data || typeof data !== "object" || !Array.isArray(data.keys) || !data.unattributed) {
        this.shareUsage = { state: "error", reason: "响应形状与契约不符（缺 keys[] / unattributed）" };
        return;
      }
      this.shareUsage = { state: "live", payload: data };
    } catch {
      if ((this.shareUsageSeq ?? 0) !== seq) return;
      this.shareUsage = { state: "error", reason: "网络失败或服务未运行" };
    }
  }
  /** Refresh the three share reads (lane entry, post-operation, manual refresh,
   *  poll tick). R9-R46: the in-flight boolean dedupes overlapping reads —
   *  the lane re-renders many times per minute and each repaint must not
   *  stack requests — while a queued follow-up guarantees that a terminal
   *  state arriving mid-read still produces one fresh read afterwards.
   *  分享用量读口（/api/console/share-usage）走同一入口，所以三块数据
   *  永远来自同一次读取，不会各自漂移。 */
  private refreshShareData(): void {
    if (this.shareFetchInFlight) { this.shareFetchQueued = true; return; }
    this.shareFetchInFlight = true;
    void Promise.all([this.fetchShareStatus(), this.fetchShareKeys(), this.fetchShareUsage()])
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
      const response = await consoleFetch("/api/console/share-management/keys", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
        body: JSON.stringify(cap ? { name, max_concurrency: cap } : { name }),
      });
      const data = await response.json().catch(() => null) as
        { key?: ShareKeyRow; plaintext?: string; error?: string; reason?: string } | null;
      if (!response.ok) {
        this.shareKeyError = data?.reason || data?.error || `HTTP ${response.status}`;
        // P2 (2026-09-24): this early return must reset the in-flight
        // marker too — leaving it set pinned the 创建 button on
        // 创建中… until a full page reload (a duplicate name answers 409,
        // so this path is reachable from the UI).
        this.shareKeySubmitting = false;
        this.renderPanel();
        return;
      }
      if (typeof data?.plaintext === "string" && data.plaintext) {
        this.sharePlaintext = { kind: "created", name: data.key?.name ?? name, value: data.plaintext, at: Date.now() };
        this.shareSecretHidden = false;
        this.shareSecretCleared = false;
        // R11-F2: the row's full key becomes viewable immediately (and the
        // map survives repaints); the resident section below keeps its own
        // session-memory display. P1 (2026-09-24): the entry carries the
        // response row's version and bumps the read sequence, so a plaintext
        // read that started before this create can never land on top of it.
        if (data.key?.id) {
          this.shareKeyPlaintextViewable = data.key.plaintext_viewable === true;
          this.setShareKeySecret(data.key.id, data.plaintext,
            typeof data.key.row_version === "number" ? data.key.row_version : null);
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
      this.shareKeyError = error instanceof Error ? error.message : String(error);
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
  private async patchShareKey(id: string, patch: Record<string, unknown>, rowVersion: number) {
    try {
      const response = await consoleFetch(`/api/console/share-management/keys/${encodeURIComponent(id)}`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
        body: JSON.stringify({ ...patch, expected_row_version: rowVersion }),
      });
      const data = await response.json().catch(() => null) as { error?: string; reason?: string } | null;
      if (!response.ok) {
        this.shareKeyError = data?.reason || data?.error || `HTTP ${response.status}`;
      } else {
        this.shareKeyError = "";
      }
    } catch (error) {
      this.shareKeyError = error instanceof Error ? error.message : String(error);
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
  private async shareKeyOp(action: "rotate" | "revoke-terminate" | "delete", id: string) {
    const base = `/api/console/share-management/keys/${encodeURIComponent(id)}`;
    const row = this.shareKeyRow(id);
    const rowVersion = typeof row?.row_version === "number" ? row.row_version : undefined;
    if (!id || rowVersion === undefined) {
      this.shareKeyError = "缺少行版本（密钥列表未就绪或已变化），请刷新后重试";
      this.renderPanel();
      return;
    }
    const init: RequestInit = {
      credentials: "include",
      headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
      method: action === "delete" ? "DELETE" : "POST",
      body: JSON.stringify({ expected_row_version: rowVersion }),
    };
    const url = action === "rotate" ? `${base}/rotate`
      : action === "revoke-terminate" ? `${base}/revoke-and-terminate`
      : base;
    try {
      const response = await consoleFetch(url, init);
      const data = await response.json().catch(() => null) as
        { plaintext?: string; key?: ShareKeyRow; error?: string; reason?: string } | null;
      if (!response.ok) {
        this.shareKeyError = data?.reason || data?.error || `HTTP ${response.status}`;
      } else {
        if (action === "rotate" && typeof data?.plaintext === "string" && data.plaintext) {
          this.sharePlaintext = { kind: "rotated", name: data.key?.name ?? "", value: data.plaintext, at: Date.now() };
          // R11-F2: the rotated row shows the NEW key immediately; the old
          // plaintext is gone (the backend overwrote the ciphertext). P1
          // (2026-09-24): version + sequence bump — an in-flight read of
          // the OLD version can never overwrite this value.
          if (data.key?.id) {
            this.shareKeyPlaintextViewable = data.key.plaintext_viewable === true;
            this.setShareKeySecret(data.key.id, data.plaintext,
              typeof data.key.row_version === "number" ? data.key.row_version : null);
          } else {
            this.shareKeyPlaintextViewable = null;
          }
        }
        this.shareKeyError = "";
      }
    } catch (error) {
      this.shareKeyError = error instanceof Error ? error.message : String(error);
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
      response = await consoleFetch("/api/summary", { credentials: "include" });
    } catch {
      this.summaryCause = "8096 不可达（网络失败或服务未运行）";
      this.enterSummaryError("无法连接费用服务（网络失败或未运行）");
      return;
    }
    if (response.status === 401) {
      this.summaryCause = "8096 拒绝读取（HTTP 401，无凭据会话）";
      this.summaryState = "unauthenticated"; this.summaryError = ""; return;
    }
    if (response.status === 403) {
      this.summaryCause = "管理权限被拒（HTTP 403）";
      this.summaryState = "forbidden"; return;
    }
    if (response.status >= 500) {
      this.summaryCause = `8096 应答错误（HTTP ${response.status}）`;
      this.enterSummaryError(`费用服务错误（HTTP ${response.status}）`); return;
    }
    if (!response.ok) {
      this.summaryCause = `8096 异常应答（HTTP ${response.status}）`;
      this.enterSummaryError(`费用服务异常响应（HTTP ${response.status}）`); return;
    }
    let data: unknown;
    try {
      data = await response.json();
    } catch {
      this.summaryCause = "8096 应答无法解析（响应体不是 JSON）";
      this.enterSummaryError("费用响应解析失败");
      return;
    }
    const services = (data as { services?: unknown })?.services;
    if (!services || typeof services !== "object") {
      this.summaryCause = "8096 应答缺少 services 对象（形状不符）";
      this.enterSummaryError("费用响应形状与预期不符"); return;
    }
    let requests = 0, tokens = 0, cost = 0, credits = 0, sawAny = false;
    // 跨链费用下界传播：任一行 costCnyPartial（含 null+partial，对下界贡献 0）→ 合计
    // 只能是下界；任一行 null 且不带 partial → 合计连下界都给不出，是「未知」。
    let costPartial = false, costUnknown = false;
    const chainRows: ChainFee[] = [];
    for (const [name, svc] of Object.entries(services as Record<string, unknown>)) {
      // console cost lane excludes the offline chain from its aggregation.
      if (String(name) !== "chat2api") continue;
      const s = svc as { requests?: unknown; tokens?: { input?: unknown; output?: unknown }; input?: unknown; output?: unknown; costCny?: unknown; costCnyPartial?: unknown; credits?: unknown; models?: unknown } | null;
      if (!s || typeof s !== "object") {
        this.summaryCause = `8096 应答里 ${String(name)} 不是对象（字段形状不符）`;
        this.enterSummaryError("费用响应字段形状与预期不符"); return;
      }
      sawAny = true;
      requests += Number(s.requests) || 0;
      tokens += (Number(s.tokens?.input ?? s.input) || 0) + (Number(s.tokens?.output ?? s.output) || 0);
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
        costCny: s.costCny === undefined ? undefined : s.costCny === null ? null : Number(s.costCny) || 0,
        costCnyPartial: s.costCnyPartial === true ? true : undefined,
        credits: s.credits === undefined ? undefined : Number(s.credits) || 0,
        models: (Array.isArray(s.models) ? s.models : []).flatMap(m => {
          const row = m as { model?: unknown; requests?: unknown; input?: unknown; output?: unknown; costCny?: unknown; costCnyPartial?: unknown; credits?: unknown };
          if (typeof row?.model !== "string" || !row.model) return [];
          return [{
            model: row.model,
            chain: String(name),
            requests: Number(row.requests) || 0,
            input: Number(row.input) || 0,
            output: Number(row.output) || 0,
            costCny: row.costCny === undefined ? undefined : row.costCny === null ? null : Number(row.costCny) || 0,
            costCnyPartial: row.costCnyPartial === true ? true : undefined,
            credits: row.credits === undefined ? undefined : Number(row.credits) || 0,
          }];
        }),
      });
    }
    if (!sawAny) {
      this.summaryCause = "8096 应答的 services 为空（缺少服务聚合）";
      this.enterSummaryError("费用响应缺少服务聚合"); return;
    }
    this.summaryLive = { requests, tokens, costCny: cost.toFixed(3), costCnyPartial: costPartial, costCnyUnknown: costUnknown, credits };
    this.chainFees = chainRows;
    this.summarySeries = this.deriveSeries(services as Record<string, unknown>);
    this.summaryState = "live";
    this.summaryEpoch = Date.now();
    this.summaryCause = "";
  }



  /** R9-R31: model.* results feed the model lane (catalog tables, sync
   *  diffs) — remembered per action instead of only shown once. */
  private deriveSeries(services: Record<string, unknown>): SummarySeries {
    const byDay = new Map<string, DayPoint>();
    const byModel = new Map<string, ModelPoint>();
    // R9-R40 task 3: per-chain day aggregates feed the fee-lane trend chart
    // (same skip list as the merged totals, so both share one honest口径).
    const chainAgg = new Map<string, Map<string, DayPoint>>();
    for (const [name, svc] of Object.entries(services)) {
      if (String(name) !== "chat2api") continue;
      const s = svc as { days?: unknown; models?: unknown } | null;
      if (!s || typeof s !== "object") continue;
      const chainDays = chainAgg.get(String(name)) ?? new Map<string, DayPoint>();
      chainAgg.set(String(name), chainDays);
      for (const d of Array.isArray(s.days) ? s.days : []) {
        const p = d as { day?: unknown; requests?: unknown; costCny?: unknown; costCnyPartial?: unknown; credits?: unknown };
        const day = typeof p?.day === "string" && /^\d{4}-\d{2}-\d{2}$/.test(p.day) ? p.day : "";
        if (!day) continue;
        const cur = byDay.get(day) ?? { day, requests: 0 };
        cur.requests += Number(p.requests) || 0;
        if (typeof p.costCny === "number" && Number.isFinite(p.costCny)) cur.costCny = (cur.costCny ?? 0) + p.costCny;
        // 下界逐链传播：任一日行带 costCnyPartial（含 null+partial，对下界贡献 0）
        // 即整日合计为下界；缺失日不折 0，也不标 partial。
        if (p.costCnyPartial === true) cur.costCnyPartial = true;
        if (typeof p.credits === "number" && Number.isFinite(p.credits)) cur.credits = (cur.credits ?? 0) + p.credits;
        byDay.set(day, cur);
        const ccur = chainDays.get(day) ?? { day, requests: 0 };
        ccur.requests += Number(p.requests) || 0;
        if (typeof p.costCny === "number" && Number.isFinite(p.costCny)) ccur.costCny = (ccur.costCny ?? 0) + p.costCny;
        if (p.costCnyPartial === true) ccur.costCnyPartial = true;
        if (typeof p.credits === "number" && Number.isFinite(p.credits)) ccur.credits = (ccur.credits ?? 0) + p.credits;
        chainDays.set(day, ccur);
      }
      for (const m of Array.isArray(s.models) ? s.models : []) {
        const p = m as { model?: unknown; requests?: unknown; input?: unknown; output?: unknown; unknown_usage?: unknown };
        if (typeof p?.model !== "string" || !p.model) continue;
        const cur = byModel.get(p.model) ?? { model: p.model, requests: 0, tokens: 0, unknownRequests: 0, tokensMissing: false };
        cur.requests += Number(p.requests) || 0;
        const io = [p.input, p.output];
        if (io.every(v => typeof v === "number" && Number.isFinite(v))) cur.tokens += (p.input as number) + (p.output as number);
        else cur.tokensMissing = true;
        cur.unknownRequests += Number(p.unknown_usage) || 0;
        byModel.set(p.model, cur);
      }
    }
    const days = [...byDay.values()].sort((a, b) => a.day.localeCompare(b.day));
    const models = [...byModel.values()].sort((a, b) => b.tokens - a.tokens || b.requests - a.requests);
    return { days, models };
  }
  /** A refresh failure after a successful read keeps the last REAL values
   *  with their capture time (stale); without prior real data it is an
   *  honest error. No fixture refill in any branch. */
  private enterSummaryError(message: string) {
    this.summaryError = message;
    if (this.summaryState === "live" && this.summaryLive) this.summaryState = "stale";
    else this.summaryState = "error";
  }
  private summaryNote() {
    const captured = this.summaryEpoch
      ? `；数据截至 ${new Date(this.summaryEpoch).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`
      : "";
    switch (this.summaryState) {
      case "loading": return "正在读取费用聚合…";
      case "unauthenticated": return "费用服务拒绝读取（接口 401）；显示空值，不以模拟数据回填。";
      case "forbidden": return "管理权限被拒（403）；当前会话无费用读取权。";
      case "error": return `读取失败：${this.summaryError}；显示为空值，不以模拟数据回填。`;
      case "stale": return `本次刷新失败：${this.summaryError}；以下为上一次真实读取${captured}。`;
      case "live": return "累计口径：chat2api 全量账本聚合。";
    }
  }
  private kpi(): { requests: number; tokens: number; costCny: string; credits: number } | undefined {
    return this.summaryState === "live" || this.summaryState === "stale" ? this.summaryLive : undefined;
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
      return this.summaryLive.costCnyPartial ? { kind: "partial", value } : { kind: "full", value };
    }
    const billed = this.chainFees.filter(c => c.costCny !== undefined);
    // 3.8 的「显示的合计 = 显示的分项之和」：任一行显示「未知」且不带下界
    // （costCny=null 且无 costCnyPartial）时，合计也必须是「未知」——把未知
    // 折成 0 再印一个确定的 ¥ 数字，正是本轮要修的那一类。
    if (billed.some(c => c.costCny === null && c.costCnyPartial !== true)) return { kind: "unknown" };
    const values = billed.filter(c => typeof c.costCny === "number").map(c => c.costCny as number);
    const partial = billed.some(c => c.costCnyPartial === true);
    if (!values.length) return partial ? { kind: "partial" } : undefined;
    const total = sumRounded3(values);
    return partial ? { kind: "partial", value: total } : { kind: "full", value: total };
  }
  /** 费用格的内容（第一阅读层）：读数元素只写值文本（零 = ¥0.000 一字不差，
   *  下界 = ≥¥…，未知 = 未知，无 ¥ 分量 = —），小字标记作为读数元素的兄弟
   *  紧邻其后，两者一起放进 `.wb-reading` 读数行（同排、基线对齐、8px 间距；
   *  窄容器按容器查询降档，见 workbench.css），标记因此不会落到读数的下一行。
   *  `data-wb-cost-state` 在格容器上，由调用方一并写出。 */
  private costCellHtml(tag: "b" | "div"): string {
    const amount = this.kpi() ? this.displayedCost() : undefined;
    const reading = costAmountReading(amount);
    const value = tag === "b"
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
      return this.summaryLive ? fmtCredits(this.summaryLive.credits) : undefined;
    }
    const credit = this.chainFees.filter(c => c.credits !== undefined);
    return credit.length ? fmtCredits(sumRounded3(credit.map(c => c.credits ?? 0))) : undefined;
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
  private trendDays(field: "requests" | "costCny" | "credits", count = 14): { labels: string[]; values: (number | null)[]; unknown: boolean[]; partial: boolean[]; boundOnly: boolean[] } {
    const recorded = new Map((this.summarySeries?.days ?? []).map(d => [d.day, d]));
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
      const value = row ? (field === "requests" ? row.requests : field === "costCny" ? row.costCny ?? null : row.credits ?? null) : null;
      labels.push(day);
      values.push(value);
      // 当天有账本记录、该分量没有可画的值、后端又标了 costCnyPartial = 下界
      // 仅含可折算部分（可折算为空）：与链级 costAmount(null, true) 同一句话，
      // 既没有值也没有数值下界，绝不折 0、绝不画下界数值。
      const boundOnlyDay = value === null && row !== undefined && field === "costCny" && row.costCnyPartial === true;
      // 有记录但分量未知（不带 partial）= 分量未知，与上一态互斥。
      unknown.push(value === null && row !== undefined && !boundOnlyDay);
      boundOnly.push(boundOnlyDay);
      // 有值且该日只含可折算记录 = 有值下界（不是完整费用）。
      partial.push(value !== null && field === "costCny" && row?.costCnyPartial === true);
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
  private windowPair(field: "requests" | "costCny" | "credits"): { now: number; prev: number; nowDays: number; prevDays: number; nowPartial: boolean; prevPartial: boolean } | undefined {
    const series = this.trendDays(field, 14);
    if (series.values.length < 14) return undefined;
    const slice = (values: (number | null)[], partial: boolean[]) => {
      const known = values.map((v, i) => (v === null ? -1 : i)).filter(i => i >= 0);
      return {
        total: known.reduce((s, i) => s + values[i]!, 0),
        days: known.length,
        partial: known.some(i => partial[i]),
      };
    };
    const now = slice(series.values.slice(7), series.partial.slice(7));
    const prev = slice(series.values.slice(0, 7), series.partial.slice(0, 7));
    if (!now.days || !prev.days) return undefined;
    return { now: now.total, prev: prev.total, nowDays: now.days, prevDays: prev.days, nowPartial: now.partial, prevPartial: prev.partial };
  }
  /** Token 本期/上期 from the SAME bounded 14-day complete-token read the
   *  token trend chart draws: a day without a complete row is left out (never
   *  folded into 0) and the counted days are printed. With no read at all the
   *  caller says 不可得 — the window is never invented from another endpoint. */
  private tokenWindowPair(): { now: number; prev: number; nowDays: number; prevDays: number } | undefined {
    const read = this.usageTokens;
    if (!read?.payload) return undefined;
    const labels = this.beijingDayKeys(14);
    const inWindow = (date: string) => date >= labels[0]! && date <= labels.at(-1)!;
    const byDay = new Map<string, number>();
    for (const row of read.payload.days) {
      if (!inWindow(row.date)) continue;
      const complete = this.completeTokens(row);
      if (complete === null) continue;
      byDay.set(row.date, (byDay.get(row.date) ?? 0) + complete);
    }
    const slice = (days: string[]) => {
      const known = days.map(d => byDay.get(d)).filter((v): v is number => v !== undefined);
      return { total: known.reduce((s, v) => s + v, 0), days: known.length };
    };
    const now = slice(labels.slice(7));
    const prev = slice(labels.slice(0, 7));
    if (!now.days || !prev.days) return undefined;
    return { now: now.total, prev: prev.total, nowDays: now.days, prevDays: prev.days };
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
  private deltaChip(pair: { now: number; prev: number; nowPartial?: boolean; prevPartial?: boolean }): string {
    if (!pair.prev) return `<span class="wb-delta" data-wb-delta>环比无基数</span>`;
    const nowPartial = pair.nowPartial === true;
    const prevPartial = pair.prevPartial === true;
    if (nowPartial && prevPartial) return `<span class="wb-delta" data-wb-delta>环比不可比</span>`;
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
    if (!Number.isFinite(pct)) return `<span class="wb-delta" data-wb-delta>环比不可得</span>`;
    if (Math.abs(pct) < 0.05) return `<span class="wb-delta" data-wb-delta>＝ 0.0%</span>`;
    return `<span class="wb-delta ${pct > 0 ? "up" : "down"}" data-wb-delta>${pct > 0 ? "▲" : "▼"} ${Math.abs(pct).toFixed(1)}%</span>`;
  }
  /** 时间区间 + 环比 reading for one first-screen KPI (contract 4.2/4.5: the
   *  first screen must answer 「用量发生了什么」, which a bare cumulative number
   *  cannot). The window is the real 14-day ledger window; an unreadable or
   *  too-thin window says so in words instead of printing a 0 or a fake
   *  percentage. `bound` 供费用行使用（维护者 2026-09-28 / OD 决定稿）：任一侧
   *  是下界时，整行两侧都写成 ≥¥…（整体标 partial），绝不出现不带 ≥ 的区间合计。 */
  private kpiWindowHtml(pair: { now: number; prev: number; nowDays: number; prevDays: number; nowPartial?: boolean; prevPartial?: boolean } | undefined, format: (v: number) => string, gapNote: string, bound?: (v: number, partial: boolean) => string): string {
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
    const show = bound ? (v: number) => bound(v, anyPartial) : (v: number) => format(v);
    const dayNote = pair.nowDays < 7 || pair.prevDays < 7
      ? ` · 有记录日 本期 ${pair.nowDays}/7、上期 ${pair.prevDays}/7` : "";
    const title = `近 14 天（本期 7 天 / 上期 7 天${dayNote}）· 本期 ${show(pair.now)} · 上期 ${show(pair.prev)}${this.deltaReason(pair)}`;
    // 首层状态行只留「本期 X · 上期 Y」+ 环比 chip：长金额夹具下（≥¥1,234,567.836）
    // 再加「近 14 天 · 」前缀会把状态行撑到 3 行，读数卡高 143.44px > 136px
    // （门禁 ③ 实测，1600x900 双主题 partial 态）。窗口口径与原因都在 title 里，一个字不少。
    return `<div class="wb-kpi-state" title="${escapeHtml(title)}"><small>本期 ${escapeHtml(show(pair.now))} · 上期 ${escapeHtml(show(pair.prev))}</small>${this.deltaChip(pair)}</div>`;
  }
  /** 环比不可比的**原因句**（不带包装）：只在状态行的 `title` 里出现，
   *  首层状态行只留「环比不可比」这几个字——原因仍可读到，只是不撑高卡片。 */
  private deltaReason(pair: { now: number; prev: number; nowPartial?: boolean; prevPartial?: boolean }): string {
    if (!pair.prev) return "；环比无基数（上期 0）。";
    const nowPartial = pair.nowPartial === true;
    const prevPartial = pair.prevPartial === true;
    if (nowPartial && prevPartial) return "；环比不可比（本期与上期均为下界）。";
    if (nowPartial) return pair.now > pair.prev
      ? "；环比至少上升（本期为下界，上期精确）。"
      : "；环比不可比（本期为下界，方向不可证）。";
    if (prevPartial) return pair.now < pair.prev
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
    if (typeof row.input_tokens !== "number" || typeof row.output_tokens !== "number") return null;
    if (!Number.isFinite(row.input_tokens) || !Number.isFinite(row.output_tokens)) return null;
    return row.input_tokens + row.output_tokens;
  }
  /** F1: the row's partial-request counts. An absent field (older payload) is
   *  unknown — a missing contract field is never read as 0 records. */
  private partialRequests(row: UsageTokenRow): number | null {
    return typeof row.partial_requests === "number" && Number.isFinite(row.partial_requests)
      ? row.partial_requests : null;
  }
  /** F1: the row's visible partial output component (null = not visible at
   *  all). Used by 仅输出可见 — it is never folded into a complete total. */
  private partialOutputTokens(row: UsageTokenRow): number | null {
    return typeof row.partial_output_tokens === "number" && Number.isFinite(row.partial_output_tokens)
      ? row.partial_output_tokens : null;
  }
  /** Beijing (UTC+8) natural-day keys for a window ending today: both usage
   *  endpoints declare Asia/Shanghai, so the axis is built from that fixed
   *  offset instead of the viewer's local clock (a browser in another zone
   *  must still bucket by the same natural days the server did). */
  private beijingDayKeys(count: number): string[] {
    const bj = Date.now() + 8 * 3600 * 1000;
    const keys: string[] = [];
    for (let i = count - 1; i >= 0; i--) keys.push(new Date(bj - i * 86400 * 1000).toISOString().slice(0, 10));
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
    for (let i = count - 1; i >= 0; i--) keys.push(new Date(bj - i * 3600 * 1000).toISOString().slice(0, 13));
    return keys;
  }
  /** Series label for one source+model identity: the model name alone while it
   *  is unambiguous, with the source appended when two sources share the name
   *  (identity stays source+model — D12-3 — even when the label shortens). */
  private tokenSeriesLabel(source: string, model: string, ambiguous: boolean): string {
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
  private tipProbe?: { host: HTMLElement; label: SVGTextElement; value: SVGTextElement };
  private tipFont?: { label: TipFont; value: TipFont };
  private tipElements(): { host: HTMLElement; label: SVGTextElement; value: SVGTextElement } | undefined {
    if (this.tipProbe) return this.tipProbe;
    const host = document.createElement("div");
    host.setAttribute("aria-hidden", "true");
    host.style.cssText = "position:absolute;left:-9999px;top:0;width:320px;height:80px;overflow:hidden;pointer-events:none";
    host.innerHTML = `<svg width="320" height="80" aria-hidden="true"><g class="wb-area-tip"><text>0</text><text class="wb-area-tip-value">0</text></g></svg>`;
    this.root.appendChild(host);
    const label = host.querySelector<SVGTextElement>(".wb-area-tip text:not(.wb-area-tip-value)");
    const value = host.querySelector<SVGTextElement>(".wb-area-tip .wb-area-tip-value");
    // No probe element (no tip rule in this build) leaves the caller on its
    // honest estimate — never on a hard-coded plate width that would clip.
    if (!label || !value) { host.remove(); return undefined; }
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
  private measureTip(text: string, tier: "label" | "value"): { advance: number; inkRight: number } {
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
  private fitText(text: string, maxPx: number, tier: "label" | "value"): string {
    if (this.measureTip(text, tier).advance <= maxPx) return text;
    let lo = 0, hi = text.length;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      if (this.measureTip(`${text.slice(0, mid)}…`, tier).advance <= maxPx) lo = mid;
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
  private areaTip(lines: { text: string; value: boolean }[]): { plate: string; width: number } {
    const lineHeight = 15, padX = 8, maxLine = 260;
    const tier = (index: number): "label" | "value" => (lines[index]!.value ? "value" : "label");
    const rendered = lines.map((line, index) => this.fitText(line.text, maxLine, tier(index)));
    const measured = rendered.map((text, index) => this.measureTip(text, tier(index)));
    const widestInk = Math.max(...measured.map(m => m.inkRight), 0);
    const plateWidth = Math.ceil(padX + widestInk) + padX;
    const plateHeight = 12 + lineHeight * lines.length;
    const plate = `<rect width="${plateWidth}" height="${plateHeight}" rx="6"/>`
      + rendered.map((text, index) => `<text x="${padX}" y="${12 + index * lineHeight}"${lines[index]!.value ? ` class="wb-area-tip-value"` : ""}>${escapeHtml(text)}</text>`).join("");
    return { plate, width: plateWidth };
  }
  /** Four dashed gridlines; F12-3: the three above the baseline carry their
   *  value (the unit is in the chart's reading row), so the plot has a scale.
   *  The lines go under the data and the labels over it (callers paint
   *  `labels` after the area fills, before the hotspots), so a fill never
   *  sits on top of a label and lowers its contrast (gate G2). */
  private gridLines(max: number, padX: number, width: number, y: (v: number) => number, label?: (v: number) => string): { lines: string; labels: string } {
    const steps = [0, 1, 2, 3].map(step => ({ step, v: max * (1 - step / 3) }));
    return {
      lines: steps.map(({ v }) => `<line x1="${padX}" y1="${y(v).toFixed(1)}" x2="${width - padX}" y2="${y(v).toFixed(1)}" class="wb-gridline"/>`).join(""),
      labels: label ? steps.filter(({ step }) => step < 3).map(({ v }) => `<text x="${padX + 2}" y="${(y(v) - 4).toFixed(1)}" class="wb-gridlabel">${escapeHtml(label(v))}</text>`).join("") : "",
    };
  }
  /** Every sample keeps its tick; dated hours use two lines to remain readable. */
  private axisTicks(labels: string[], axis: (label: string) => string, x: (i: number) => number, h: number): string {
    return labels.map((label, i) => {
      const lines = axisLabelLines(axis(label));
      const anchor = i === 0 ? "start" : i === labels.length - 1 ? "end" : "middle";
      const cx = x(i).toFixed(1);
      return `<text x="${cx}" y="${h - (lines.length > 1 ? 19 : 5)}" class="wb-gridlabel" text-anchor="${anchor}">${lines.map((line, j) => `<tspan x="${cx}" dy="${j ? 14 : 0}">${escapeHtml(line)}</tspan>`).join("")}</text>`;
    }).join("");
  }
  /** F12-3: a trend chart's readings sit in its title area, above the plot -
   *  never painted onto the data: the interval total (when a sum is a reading),
   *  the last value with its date, the mean drawn as the dashed baseline (or the
   *  caller's weighted mean) and the unit. Lower-bound days keep the ≥ wording,
   *  so a partial total or mean never reads as complete. */
  private chartStatsHtml(o: {
    values: (number | null)[]; labels: string[]; axis: (label: string) => string; format: (v: number) => string;
    unit: string; totalLabel: string; partialValues: boolean[]; scope: string; mean?: { label: string; value: number | null };
  }): string {
    const known = o.values.flatMap((v, i) => (v === null ? [] : [{ v, i }]));
    if (!known.length) return "";
    const anyPartial = known.some(p => o.partialValues[p.i] === true);
    const last = known.at(-1)!;
    const lastPartial = o.partialValues[last.i] === true;
    const total = known.reduce((s, p) => s + p.v, 0);
    const mean = o.mean ?? { label: `均值 · ${known.length} 个有记录${o.scope}${anyPartial ? "（下界）" : ""}`, value: total / known.length };
    const item = (label: string, value: string, swatch = "") =>
      `<span class="wb-chart-stat"><small>${swatch}${escapeHtml(label)}</small><b>${escapeHtml(value)}</b></span>`;
    const swatch = `<svg width="18" height="8" aria-hidden="true"><line x1="0" y1="4" x2="18" y2="4" class="wb-trend-ref"/></svg>`;
    return `<div class="wb-chart-stats">`
      + (o.totalLabel ? item(anyPartial ? `${o.totalLabel}（下界）` : o.totalLabel, `${anyPartial ? "≥" : ""}${o.format(total)}`) : "")
      + item(`末值 · ${o.axis(o.labels[last.i] ?? "")}`, `${lastPartial ? "≥" : ""}${o.format(last.v)}`)
      + (mean.value !== null ? item(mean.label, `${anyPartial && !o.mean ? "≥" : ""}${o.format(mean.value)}`, swatch) : "")
      + `<span class="wb-chart-unit">单位：${escapeHtml(o.unit)}</span></div>`;
  }
  /** Contract 3.7 `wb-trend-ref`: the interval mean (missing days left OUT —
   *  a day absent from the ledger is not a 0), drawn heavier and darker than
   *  the grid. The caller lists it in the chart's legend under the same class
   *  name, so the reference can never be mistaken for the data line.
   *  区间含下界日时均值同样只是下界（每个下界日的真实值 ≥ 画出的值）——
   *  `partial` 随图例一起写出，不把下界均值印成完整均值。 */
  private trendRef(values: (number | null)[], x: (i: number) => number, y: (v: number) => number, padX: number, width: number, partialValues: boolean[] = [], fixedMean?: number | null): { line: string; mean: number | undefined; days: number; partial: boolean } {
    const known = values.filter((v): v is number => v !== null);
    if (!known.length || fixedMean === null) return { line: "", mean: undefined, days: 0, partial: false };
    const mean = fixedMean ?? known.reduce((s, v) => s + v, 0) / known.length;
    const gy = y(mean).toFixed(1);
    const partial = values.some((v, i) => v !== null && partialValues[i] === true);
    return { line: `<line x1="${padX}" y1="${gy}" x2="${width - padX}" y2="${gy}" class="wb-trend-ref"/>`, mean, days: known.length, partial };
  }
  /** Keep each drawn key's colour slot; free the slots of keys no longer drawn and
   *  give newcomers the lowest free slot (1-4). Identity, never rank. */
  private seriesSlots(map: Map<string, number>, keys: string[]): Map<string, number> {
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
  private tokenSeriesSvg(
    labels: string[],
    series: { key: string; label: string; values: (number | null)[]; kind: "total" | "model"; slot?: number; partial?: boolean[] }[],
    opts: { aria: string; axis?: (label: string) => string; format?: (value: number) => string; totalLabel?: string; missingLabel?: (index: number) => string; partialValues?: boolean[]; unit: string; scope: string; mean?: { label: string; value: number | null }; tick?: (value: number) => string; tipTime?: (label: string) => string; width?: number },
  ): string {
    const n = labels.length;
    // Reserve reading space for every time sample; narrow cards scroll locally.
    const h = 220, padT = 20, padB = 42, padX = 12;
    const axis = opts.axis ?? ((label: string) => label.slice(5).replace("-", "/"));
    const width = timeAxisWidth(labels.map(axis), opts.width ?? 1000);
    const format = opts.format ?? ((value: number) => fmtTokens(value));
    const known = series.flatMap(s => s.values.filter((v): v is number => v !== null));
    const max = Math.max(...known, 1);
    const x = (i: number) => padX + (i / Math.max(n - 1, 1)) * (width - padX * 2);
    const y = (v: number) => padT + (1 - v / max) * (h - padT - padB);
    // F12-3: each series has its own validated colour (the total line plus the
    // model's fixed slot), named in the legend - no longer one hue told apart by
    // opacity alone.
    const stroke = (s: { kind: "total" | "model"; slot?: number }) => s.kind === "total" ? "var(--wb-chart-total)" : `var(--wb-cat-${s.slot ?? 1})`;
    const paths = series.map(s => {
      const segs: number[][] = [];
      s.values.forEach((v, i) => {
        if (v === null) return;
        if (i === 0 || s.values[i - 1] === null) segs.push([]);
        segs.at(-1)!.push(i);
      });
      const style = `stroke:${stroke(s)};stroke-width:${s.kind === "total" ? 2.4 : 1.6};stroke-opacity:1`;
      return segs.map(indices => {
        const line = indices.map((i, j) => `${j ? "L" : "M"}${x(i).toFixed(1)},${y(s.values[i]!).toFixed(1)}`).join(" ");
        return `<path d="${line}" class="wb-area-line" fill="none" style="${style}"/>`;
      }).join("");
    }).join("");
    const markers = series.map((s, slot) => s.values.map((v, i) => {
      const kind = chartBaselineKind(v);
      if (!kind) return "";
      const cx = x(i) + Math.min(slot, 4) * 2;
      return `<line class="wb-chart-baseline-${kind}" x1="${cx.toFixed(1)}" x2="${cx.toFixed(1)}" y1="${h - padB - 8}" y2="${h - padB}" style="stroke:${kind === "zero" ? stroke(s) : "var(--theme-muted)"}" aria-hidden="true"/>`;
    }).join("")).join("");
    const hotWidth = (width - padX * 2) / Math.max(n - 1, 1);
    const tipTime = opts.tipTime ?? ((label: string) => label);
    const hotspots = labels.map((label, i) => {
      const cx = x(i);
      // The tooltip opens with the point's time, then one line per series.
      const time = tipTime(label);
      const lines = series.map(s => {
        const v = s.values[i];
        // 总计线的空日不是「无记录」一种：缺完整分量（当日有账本记录）与
        // 无账本行是两种状态，各自写清；`missingLabel` 只作用于总计线。
        const missing = s.kind === "total" && opts.missingLabel ? opts.missingLabel(i) : "无记录";
        // A day whose value is only a lower bound says so on every line it is
        // read from (tooltip and accessible name), not only in the stats row.
        const lower = (s.kind === "total" ? opts.partialValues?.[i] : s.partial?.[i]) === true;
        return { text: `${s.label}：${v === null || v === undefined ? missing : lower ? `≥${format(v)}（下界）` : format(v)}`, value: false };
      });
      const tip = this.areaTip([{ text: time, value: false }, ...lines]);
      const tipX = Math.min(Math.max(cx - tip.width / 2, 2), Math.max(2, width - tip.width - 2));
      return `<g class="wb-area-hotspot" tabindex="0" role="img" aria-label="${escapeHtml(time)}：${escapeHtml(lines.map(line => line.text).join("，"))}">
        <rect x="${(cx - hotWidth / 2).toFixed(1)}" y="0" width="${hotWidth.toFixed(1)}" height="${h - padB}" class="wb-area-hit"/>
        <line x1="${cx.toFixed(1)}" x2="${cx.toFixed(1)}" y1="${padT}" y2="${h - padB}" class="wb-area-crosshair"/>
        <g class="wb-area-tip" transform="translate(${tipX.toFixed(1)},1)">${tip.plate}</g>
      </g>`;
    }).join("");
    // The total series is the one drawn heaviest, so it carries the interval
    // reference baseline and the readings above the plot; the per-model series
    // stay on the tooltip, the legend and the keyboard.
    const total = series.find(s => s.kind === "total");
    const ref = total ? this.trendRef(total.values, x, y, padX, width, opts.partialValues ?? [], opts.mean ? opts.mean.value : undefined) : { line: "", mean: undefined, days: 0, partial: false };
    const stats = total ? this.chartStatsHtml({ values: total.values, labels, axis, format, unit: opts.unit, totalLabel: opts.totalLabel ?? "区间合计", partialValues: opts.partialValues ?? [], scope: opts.scope, mean: opts.mean }) : "";
    const legend = series.length >= 2
      ? `<ul class="wb-chart-legend" aria-label="图例">${series.map(s => `<li><i style="--wb-swatch:${stroke(s)}" aria-hidden="true"></i>${escapeHtml(s.label)}</li>`).join("")}</ul>`
      : "";
    const grid = this.gridLines(max, padX, width, y, known.length ? opts.tick ?? format : undefined);
    return `${stats}${legend}<div class="wb-chart-scroll"><svg class="wb-area" style="min-width:${width}px" viewBox="0 0 ${width} ${h}" role="group" aria-label="${escapeHtml(opts.aria)}">
      ${grid.lines}
      ${paths}${markers}${ref.line}${grid.labels}
      ${hotspots}
      ${this.axisTicks(labels, axis, x, h)}
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
    return this.tokenSeriesSvg(labels, [{ key:"unknown", label:"统计未提供", values:labels.map(() => null), kind:"total" }], { aria:`${unit} 暂无统计，底部标记为未知`, unit, scope:unit === "Token/s" ? "小时" : "日", axis:label => label.slice(5).replace("T", " "), missingLabel:() => "统计未提供" });
  }
  private emptyCompositionHtml(note: string): string {
    return `<div class="wb-ring"><svg class="wb-ring-svg" viewBox="0 0 150 150" role="img" aria-label="${escapeHtml(note)}"><circle cx="75" cy="75" r="58" fill="none" stroke="var(--theme-muted)" stroke-width="10" opacity="0.25"/><line x1="75" x2="75" y1="126" y2="140" stroke="var(--theme-muted)" stroke-width="2"/><text x="75" y="80" text-anchor="middle" class="wb-ring-sub">${note === "完整 Token 为 0" ? "0 Token" : "暂无统计"}</text></svg><p class="wb-muted">${escapeHtml(note)}；无可计算构成，不显示占比。</p></div>`;
  }
  private tokenCompositionHtml(): string {
    const read = this.usageTokens;
    if (!read?.payload) return this.emptyCompositionHtml(this.usageReadNote(read, "近 14 天完整 Token"));
    const labels = this.beijingDayKeys(14);
    const inWindow = (d: string | null) => d !== null && d >= labels[0]! && d <= labels.at(-1)!;
    const bySource = new Map<string, number>();
    let partialRequests = 0, partialUnknown = false;
    for (const row of read.payload.days) {
      if (!inWindow(row.date)) continue;
      const complete = this.completeTokens(row);
      if (complete !== null) bySource.set(row.source, (bySource.get(row.source) ?? 0) + complete);
      const partial = this.partialRequests(row);
      if (partial === null) partialUnknown = true;
      else partialRequests += partial;
    }
    const outputOnly = read.payload.missing.filter(m => m.date === null || inWindow(m.date)).length;
    const order = (src: string) => SOURCE_SLOTS[src] ?? 9;
    const parts = [...bySource.entries()].filter(([, v]) => v > 0).sort((a, b) => order(a[0]) - order(b[0]) || a[0].localeCompare(b[0]));
    const total = parts.reduce((sum, [, v]) => sum + v, 0);
    if (!total) return this.emptyCompositionHtml(bySource.size ? "完整 Token 为 0" : "暂无完整统计");
    const color = (src: string) => SOURCE_SLOTS[src] ? `var(--wb-cat-${SOURCE_SLOTS[src]})` : "var(--theme-muted)";
    const incomplete = partialUnknown || partialRequests > 0 || outputOnly > 0;
    const size = 150, c = size / 2, r = 58, gap = parts.length > 1 ? 2 / r : 0;
    const point = (a: number) => `${(c + r * Math.sin(a)).toFixed(2)},${(c - r * Math.cos(a)).toFixed(2)}`;
    let angle = 0;
    const segs = parts.map(([src, v]) => {
      const sweep = (v / total) * Math.PI * 2;
      const a0 = angle + gap / 2, a1 = angle + sweep - gap / 2;
      angle += sweep;
      const name = this.sourceLabel(src);
      // A share never rounds into a claim the data does not make: 100.0% beside
      // another source, or 0.0% for a real amount.
      const pct = v / total * 100;
      const pctText = parts.length > 1 && pct >= 99.95 ? ">99.9%" : pct < 0.05 ? "<0.1%" : `${pct.toFixed(1)}%`;
      // Maintainer decision 2 (2026-10-06): no share at all while the denominator
      // is incomplete (requests missing a component, or their count unknown).
      const share = incomplete ? "不给占比：有缺分量记录，分母不完整" : `占近 14 天完整记录 ${pctText}`;
      const lines = [{ text: name, value: false }, { text: fmtTokens(v), value: true }, { text: share, value: false }];
      const tip = this.areaTip(lines);
      // A lone source is a full ring: two half arcs (one arc cannot close a circle).
      const d = parts.length === 1
        ? `M${point(0)} A${r},${r} 0 1 1 ${point(Math.PI)} A${r},${r} 0 1 1 ${point(Math.PI * 2 - 0.0001)}`
        : `M${point(a0)} A${r},${r} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${point(Math.max(a1, a0 + 0.0001))}`;
      return `<g class="wb-area-hotspot wb-ring-seg" tabindex="0" role="img" aria-label="${escapeHtml(lines.map(l => l.text).join("，"))}">`
        + `<path d="${d}" class="wb-ring-arc" style="stroke:${color(src)}"/>`
        + `<g class="wb-area-tip" transform="translate(${(c - tip.width / 2).toFixed(1)},${(c - 26).toFixed(1)})">${tip.plate}</g></g>`;
    }).join("");
    const legend = parts.map(([src, v]) => `<li><i style="--wb-swatch:${color(src)}" aria-hidden="true"></i><span>${escapeHtml(this.sourceLabel(src))}</span><b>${escapeHtml(fmtTokens(v))}</b></li>`).join("");
    const left = partialUnknown
      ? "；部分请求数未知"
      : partialRequests || outputOnly ? `；不含缺分量请求 ${partialRequests.toLocaleString("zh-CN")} 条、仅输出可见行 ${outputOnly}` : "";
    return `<div class="wb-ring">`
      + `<svg class="wb-ring-svg" viewBox="0 0 ${size} ${size}" role="group" aria-label="近 14 天完整 Token 按网关构成，按 Tab 逐段查看">`
      + `<text x="${c}" y="${c - 2}" text-anchor="middle" class="wb-ring-total">${escapeHtml(fmtTokens(total))}</text>`
      + `<text x="${c}" y="${c + 16}" text-anchor="middle" class="wb-ring-sub">完整 Token</text>${segs}</svg>`
      + `<ul class="wb-chart-legend wb-ring-legend" aria-label="图例">${legend}</ul></div>`
      + `<p class="wb-muted">口径：近 14 天输入+输出齐全的记录${left}。${incomplete ? "分母不完整，悬停提示不给占比。" : "占比只在悬停提示里，分母是这些完整记录。"}</p>`;
  }
  private tokenTrendHtml(): string {
    const read = this.usageTokens;
    const note = `<p class="wb-muted">${this.usageReadNote(read, "近 14 天完整 Token")}</p>`;
    if (!read || !read.payload) return note + this.emptyTrendHtml("Token", this.beijingDayKeys(14));
    const payload = read.payload;
    const labels = this.beijingDayKeys(14);
    const inWindow = (date: string) => date >= labels[0]! && date <= labels.at(-1)!;
    const rows = payload.days.filter(row => inWindow(row.date));
    const dayTotals = new Map<string, number>();
    const rowsByDay = new Map<string, number>();
    const partialByDay = new Map<string, number>();
    const bySeries = new Map<string, { source: string; model: string; values: Map<string, number>; input: number; output: number; days: number }>();
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
        const series = bySeries.get(key) ?? { source: row.source, model: row.model, values: new Map<string, number>(), input: 0, output: 0, days: 0 };
        series.values.set(row.date, (series.values.get(row.date) ?? 0) + complete);
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
    const missingRows = payload.missing.filter(m => m.date === null || inWindow(m.date));
    const undatedMissing = missingRows.filter(r => r.date === null);
    const outputOnly = [...missingRows.filter(r => r.date !== null), ...undatedMissing];
    const visibleNumber = (value: unknown): number | null =>
      typeof value === "number" && Number.isFinite(value) ? value : null;
    const outputOnlyTotal = outputOnly.reduce((s, r) => s + (visibleNumber(r.output_tokens) ?? 0), 0);
    const outputOnlyDays = new Set(outputOnly.map(r => r.date).filter((d): d is string => typeof d === "string"));
    const outputOnlyNoNumber = outputOnly.filter(r => visibleNumber(r.output_tokens) === null).length;
    const undatedMissingText = undatedMissing.length
      ? `；另有 ${undatedMissing.length} 行时间无法判定（时间戳无效，不归任何一天：${undatedMissing.slice(0, 10).map(r => `${escapeHtml(this.sourceLabel(r.source))}/${escapeHtml(r.model)}`).join("、")}${undatedMissing.length > 10 ? "…" : ""}），既不进完整总量也不计入任何一天`
      : "";
    const seriesList = [...bySeries.entries()]
      .map(([key, s]) => ({ key, ...s, total: [...s.values.values()].reduce((a, b) => a + b, 0) }))
      .sort((a, b) => b.total - a.total);
    const ambiguous = new Set(seriesList.map(s => s.model).filter((m, i, all) => all.indexOf(m) !== i));
    // 按来源: the model rows summed per source, plus every source the coverage
    // block names (a source with no complete row is listed, never dropped).
    const bySource = new Map<string, { values: Map<string, number>; input: number; output: number }>();
    for (const s of seriesList) {
      const agg = bySource.get(s.source) ?? { values: new Map<string, number>(), input: 0, output: 0 };
      for (const [d, v] of s.values) agg.values.set(d, (agg.values.get(d) ?? 0) + v);
      agg.input += s.input;
      agg.output += s.output;
      bySource.set(s.source, agg);
    }
    const cov = payload.coverage;
    const covSources = cov?.sources && typeof cov.sources === "object" && !Array.isArray(cov.sources) ? cov.sources : {};
    const sourceList = [...new Set([...bySource.keys(), ...Object.keys(covSources)])]
      .map(src => {
        const agg = bySource.get(src);
        return { key: `src:${src}`, source: src, values: agg?.values ?? new Map<string, number>(), input: agg?.input ?? 0, output: agg?.output ?? 0, total: agg ? [...agg.values.values()].reduce((a, b) => a + b, 0) : 0, has: Boolean(agg) };
      })
      .sort((a, b) => (SOURCE_SLOTS[a.source] ?? 9) - (SOURCE_SLOTS[b.source] ?? 9) || a.source.localeCompare(b.source));
    const bySourceMode = this.tokenGroup === "source";
    const drawnModels = new Set(seriesList.filter(s => this.tokenSeriesOpen.has(s.key)).slice(0, TOKEN_SERIES_MAX).map(s => s.key));
    const modelSlots = this.seriesSlots(this.tokenSeriesSlot, [...drawnModels]);
    // Every source with a fixed colour slot is drawn unless the user hid it; a
    // source without a slot is listed only (no generated fifth hue).
    const drawnSources = new Set(sourceList.filter(s => s.has && SOURCE_SLOTS[s.source] && !this.tokenSourceHidden.has(s.source)).map(s => s.key));
    const totalValues = labels.map(d => (dayTotals.has(d) ? dayTotals.get(d)! : null));
    const chartSeries = [
      { key: "__total__", label: "总计", values: totalValues, kind: "total" as const },
      ...(bySourceMode
        ? sourceList.filter(s => drawnSources.has(s.key)).map(s => ({
          key: s.key, label: this.sourceLabel(s.source), kind: "model" as const, slot: SOURCE_SLOTS[s.source],
          values: labels.map(d => (s.values.has(d) ? s.values.get(d)! : null)),
        }))
        : seriesList.filter(s => drawnModels.has(s.key)).map(s => ({
          key: s.key, label: this.tokenSeriesLabel(s.source, s.model, ambiguous.has(s.model)), kind: "model" as const, slot: modelSlots.get(s.key),
          values: labels.map(d => (s.values.has(d) ? s.values.get(d)! : null)),
        }))),
    ];
    const chart = this.tokenSeriesSvg(labels, chartSeries, {
        aria: `近 ${labels.length} 天每日完整 Token 趋势，逐日悬停或按 Tab 查看数值`,
        tipTime: (label: string) => `${label}（北京时间）`,
        unit: "Token（输入 + 输出，完整记录）",
        scope: "日",
        width: 760,
        // 总计线的空日分两种（维护者 2026-09-28）：当天有账本行但缺完整分量
        // 与当天无任何账本行——图上都断线，但 tooltip 不得只写「无记录」。
        missingLabel: i => {
          const d = labels[i]!;
          return rowsByDay.has(d) || partialByDay.has(d) || outputOnlyDays.has(d)
            ? "缺完整分量（当日有账本记录）" : "无记录";
        },
      });
    const zeroDays = labels.filter(d => dayTotals.has(d) && dayTotals.get(d) === 0);
    const missingDays = labels.filter(d => !dayTotals.has(d) && (rowsByDay.has(d) || partialByDay.has(d) || outputOnlyDays.has(d)));
    const noRecordDays = labels.filter(d => !dayTotals.has(d) && !rowsByDay.has(d) && !outputOnlyDays.has(d));
    const windowTotal = labels.reduce((s, d) => s + (dayTotals.get(d) ?? 0), 0);
    const windowInput = rows.reduce((s, r) => s + (this.completeTokens(r) === null ? 0 : r.input_tokens ?? 0), 0);
    const windowOutput = rows.reduce((s, r) => s + (this.completeTokens(r) === null ? 0 : r.output_tokens ?? 0), 0);
    const partialCountText = partialKnown ? partialRequests.toLocaleString("zh-CN") : "未知";
    // No partial request at all is a known zero, not 未知.
    const partialOutputText = partialOutput !== null ? fmtTokens(partialOutput)
      : partialKnown && partialRequests === 0 ? "0" : "未知";
    // The day-state counts stay resident under the chart (they qualify its
    // points); only their explanation and the row lists fold away.
    const dayLine = `<p class="wb-muted wb-tok-days">零记录 ${zeroDays.length} 天 · 无记录 ${noRecordDays.length} 天（断线）· 缺分量 ${missingDays.length} 天 · 部分请求 ${partialCountText} 条、仅输出可见 ${partialOutputText}（另列，不进完整总量）</p>`;
    // A coverage gap (ledger absent, source off, read errors, truncation) is a
    // state the page asserts, so it keeps a resident line; the per-source
    // wording sits in the fold.
    const covGaps = Object.keys(covSources).filter(src => {
      const c = covSources[src] ?? {};
      return c.ledger_present !== true || c.enabled !== true || (Array.isArray(c.errors) && c.errors.length > 0);
    });
    const cut = cov?.truncated === true || cov?.clamped === true
      || (typeof cov?.dropped_rows === "number" && cov.dropped_rows > 0)
      || (typeof cov?.dropped_missing_rows === "number" && cov.dropped_missing_rows > 0);
    const covLine = !Object.keys(covSources).length
      ? `<p class="wb-tok-cov"><span class="wb-badge wb-badge-warn">覆盖未知</span>后端未返回来源覆盖，账本是否被读取未知，不按零用量推断。</p>`
      : covGaps.length || cut
        ? `<p class="wb-tok-cov"><span class="wb-badge wb-badge-warn">覆盖有缺口</span>${covGaps.length ? `${escapeHtml(covGaps.map(src => this.sourceLabel(src)).join("、"))} 的账本缺席、来源未启用或读取出错` : ""}${covGaps.length && cut ? "；" : ""}${cut ? "结果被截断或收口" : ""}；缺口部分没有可读数据，不按 0 显示（详见下方折叠）。</p>`
        : "";
    // Right column: the breakdown table. Its first column is the draw toggle, so
    // the table is also the chart's series picker.
    const toggle = (key: string, label: string, on: boolean, slot: number | undefined, disabledWhy: string) => {
      const verb = on ? "从图上移除" : "画到图上";
      const swatch = on && slot ? `<i class="wb-series-swatch" style="--wb-swatch:var(--wb-cat-${slot})" aria-hidden="true"></i>` : "";
      return `<button type="button" class="wb-tok-draw" data-wb-series-toggle="${escapeHtml(key)}" aria-pressed="${on}" aria-label="${verb}：${escapeHtml(label)}" title="${escapeHtml(disabledWhy || `${verb}：${label}`)}"${disabledWhy ? " disabled" : ""}>${swatch}</button>`;
    };
    const cells = (total: number | null, input: number | null, output: number | null) =>
      `<td class="wb-fee-num">${total === null ? "—" : fmtTokens(total)}</td><td class="wb-fee-num">${input === null ? "—" : fmtTokens(input)}</td><td class="wb-fee-num">${output === null ? "—" : fmtTokens(output)}</td>`;
    const q = this.tokenSeriesQ.trim().toLowerCase();
    const matched = q
      ? seriesList.filter(s => s.model.toLowerCase().includes(q) || this.sourceLabel(s.source).toLowerCase().includes(q) || s.source.toLowerCase().includes(q))
      : seriesList;
    const MODEL_ROWS = 40;
    const bodyRows = bySourceMode
      ? sourceList.map(s => {
        const slot = SOURCE_SLOTS[s.source];
        const why = !s.has ? "该来源窗口内没有完整记录，无线可画" : !slot ? "该来源没有固定颜色，只在表中列出" : "";
        return `<tr><td class="wb-center">${toggle(s.key, this.sourceLabel(s.source), drawnSources.has(s.key), slot, why)}</td>`
          + `<td>${escapeHtml(this.sourceLabel(s.source))}<small class="wb-muted">${s.has ? `完整 ${s.values.size} 天` : "窗口内无完整记录（不按 0）"}</small></td>`
          + `${s.has ? cells(s.total, s.input, s.output) : cells(null, null, null)}</tr>`;
      }).join("")
      : matched.slice(0, MODEL_ROWS).map(s => {
        const label = this.tokenSeriesLabel(s.source, s.model, ambiguous.has(s.model));
        const on = drawnModels.has(s.key);
        const why = !on && drawnModels.size >= TOKEN_SERIES_MAX ? `图上最多同时画 ${TOKEN_SERIES_MAX} 个模型，先移除一个` : "";
        return `<tr><td class="wb-center">${toggle(s.key, label, on, modelSlots.get(s.key), why)}</td>`
          + `<td>${escapeHtml(s.model)}<small class="wb-muted">${escapeHtml(this.sourceLabel(s.source))} · 完整 ${s.days} 天</small></td>${cells(s.total, s.input, s.output)}</tr>`;
      }).join("");
    const emptyRow = `<tr><td colspan="5">${bySourceMode ? "该窗口没有来源记录。" : q ? "没有匹配的模型；换关键词或清空搜索。" : "该窗口没有完整的来源+模型行。"}</td></tr>`;
    const groupTabs = `<div class="wb-section-tabs wb-tok-group" role="group" aria-label="分类方式">`
      + `<button type="button" data-wb-tok-group="source" aria-pressed="${bySourceMode}">按来源</button>`
      + `<button type="button" data-wb-tok-group="model" aria-pressed="${!bySourceMode}">按模型</button></div>`;
    const search = bySourceMode ? ""
      : `<input type="text" class="wb-trend-q" data-wb-series-q placeholder="按来源 / 模型检索" value="${escapeHtml(this.tokenSeriesQ)}" autocomplete="off" spellcheck="false" aria-label="检索模型"/>`;
    const tableNote = bySourceMode
      ? `<small class="wb-muted">首列切换是否画在图上；来源颜色固定。</small>`
      : `<small class="wb-muted">图上 ${drawnModels.size} / ${TOKEN_SERIES_MAX} 个模型 · 表中 ${Math.min(matched.length, MODEL_ROWS)} / ${seriesList.length} 行${matched.length > MODEL_ROWS ? `（其余 ${matched.length - MODEL_ROWS} 行请用搜索）` : ""}</small>`;
    const table = `<div class="wb-tok-side"><div class="wb-tok-tools">${groupTabs}${search}</div>`
      + `<div class="wb-table-scroll wb-tok-scroll"><table class="wb-table wb-tok-table"><thead><tr><th class="wb-center">图上</th><th>${bySourceMode ? "来源" : "模型"}</th><th class="wb-fee-num">完整 Token</th><th class="wb-fee-num">输入</th><th class="wb-fee-num">输出</th></tr></thead>`
      + `<tbody>${bodyRows || emptyRow}</tbody>`
      + `<tfoot><tr><td></td><th scope="row">14 天合计</th>${cells(windowTotal, windowInput, windowOutput)}</tr></tfoot></table></div>${tableNote}</div>`;
    const missingHtml = outputOnly.length
      ? `<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>日期</th><th>来源</th><th>模型</th><th class="wb-fee-num">输出（可见）</th><th class="wb-fee-num">输入（可见）</th><th>缺失分量</th><th>原因</th></tr></thead><tbody>${outputOnly.slice(0, 40).map(r => `<tr><td>${r.date === null ? "时间无法判定" : escapeHtml(r.date)}</td><td>${escapeHtml(this.sourceLabel(r.source))}</td><td>${escapeHtml(r.model)}</td><td class="wb-fee-num">${visibleNumber(r.output_tokens) === null ? "未知" : fmtTokens(visibleNumber(r.output_tokens)!)}</td><td class="wb-fee-num">${visibleNumber(r.input_tokens) === null ? "未知" : fmtTokens(visibleNumber(r.input_tokens)!)}</td><td>${escapeHtml(MISSING_REASON_LABEL[r.reason] ?? r.reason)}</td><td>${escapeHtml(r.reason)}</td></tr>`).join("")}</tbody></table></div><p class="wb-muted">仅输出可见合计 ${fmtTokens(outputOnlyTotal)}（只统计部分请求里可见的输出分量，其余不补 0${outputOnlyNoNumber ? `；另有 ${outputOnlyNoNumber} 行连输出数字也没有` : ""}），共 ${outputOnlyDays.size} 天；不计入上方完整总量${undatedMissingText}。</p>`
      : `<p class="wb-muted">该窗口没有缺分量的历史行。</p>`;
    const dayNote = `零记录 = 账本记录为 0，图上按 0 画；无记录 = 当天没有任何账本行，图上断线；缺分量 = 输入或输出缺一，见下表。部分请求只进「仅输出可见」，不进完整总量${partialOutputMissing ? `（其中 ${partialOutputMissing} 条连输出分量也没有）` : ""}。`;
    const details = this.laneNoteFoldHtml("tok-coverage", "数据覆盖与缺分量明细",
      `<p class="wb-muted">${dayNote}</p>${this.usageCoverageHtml(payload.coverage, "近 14 天 Token")}`
      + `<h4 class="wb-subhead">仅输出可见 / 时间无法判定（不混入完整总量）</h4>${missingHtml}`);
    return note + covLine + `<div class="wb-tok-split"><div class="wb-tok-main">${chart}${dayLine}</div>${table}</div>` + details;
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
    if (!read || !read.payload) return note + this.emptyTrendHtml("Token/s", this.beijingHourKeys(24));
    const payload = read.payload;
    const rows = [...payload.hours].sort((a, b) =>
      b.hour.localeCompare(a.hour) || a.source.localeCompare(b.source) || a.model.localeCompare(b.model));
    const skipped = payload.skipped;
    const skippedHtml = `<div class="wb-stats-row"><div><small>缺输出 Token</small><b>${this.countText(skipped.missing_output_tokens)}</b></div><div><small>缺耗时</small><b>${this.countText(skipped.missing_duration)}</b></div><div><small>无效耗时</small><b>${this.countText(skipped.invalid_duration)}</b></div><div><small>时间无法判定</small><b>${this.countText(skipped.invalid_time)}</b></div><div><small>参与分母的分桶</small><b>${rows.filter(r => r.rate !== null).length} / ${rows.length}</b></div></div>`;
    const table = rows.length
      ? `<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>小时（北京时间）</th><th>来源</th><th>模型</th><th class="wb-fee-num">请求数</th><th class="wb-fee-num">输出 token</th><th class="wb-fee-num">有效耗时</th><th class="wb-fee-num">整次平均输出 Token/s</th><th class="wb-center">逐次</th></tr></thead><tbody>${rows.slice(0, 60).map(r => `<tr><td>${escapeHtml(this.hourLabel(r.hour))}</td><td>${escapeHtml(this.sourceLabel(r.source))}</td><td>${escapeHtml(r.model)}</td><td class="wb-fee-num">${this.numText(r.requests)}</td><td class="wb-fee-num">${this.numText(r.output_tokens)}</td><td class="wb-fee-num">${r.duration_ms > 0 ? `${(r.duration_ms / 1000).toFixed(1)}s` : "—"}</td><td class="wb-fee-num">${r.rate === null ? "未知" : r.rate.toFixed(2)}</td><td class="wb-center"><button type="button" class="wb-btn wb-btn-sm" data-wb-rate-drill="${escapeHtml(`${r.hour}|${r.source}|${r.model}`)}" title="查看该时段的逐次明细（账本尾部读取，有界）" aria-label="查看该时段的逐次明细">${wbIcon("detail")}${wbIconHint("查看该时段的逐次明细")}</button></td></tr>`).join("")}</tbody></table></div>`
      : `<p class="wb-muted">近 24 小时没有可用分桶记录；无法判定为零速率（账本是否被读取见下方「数据覆盖」说明，不写成 0 用量）。</p>`;
    const detail = this.rateDrill ? this.rateDrillHtml(this.rateDrill) : "";
    return note + this.rateTrendHtml(payload.hours) + table + skippedHtml + this.usageCoverageHtml(payload.coverage, "近 24 小时速率") + `<p class="wb-muted">被跳过的记录不参与分母；缺输出 Token / 缺耗时 / 无效耗时三项计数相互独立（同一条记录可同时计入多项，其和可能大于被跳过的记录条数），时间无法判定（时间戳无效）单独计数；rate 为「未知」的分桶表示该桶无法计算，不按 0 显示。</p>` + detail;
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
    const inWindow = (key: string) => key >= labels[0]! && key <= labels.at(-1)!;
    const buckets = rows.filter(r => inWindow(r.hour.slice(0, 13)));
    const totals = new Map<string, { output: number; duration: number }>();
    const bySeries = new Map<string, { source: string; model: string; hours: Map<string, { output: number; duration: number }>; output: number }>();
    let computable = 0, unknown = 0;
    for (const row of buckets) {
      if (row.rate === null) { unknown += 1; continue; }
      computable += 1;
      const hour = row.hour.slice(0, 13);
      const output = Number.isFinite(row.output_tokens) ? row.output_tokens : 0;
      const duration = Number.isFinite(row.duration_ms) ? row.duration_ms : 0;
      const total = totals.get(hour) ?? { output: 0, duration: 0 };
      total.output += output;
      total.duration += duration;
      totals.set(hour, total);
      const key = `${row.source}|${row.model}`;
      const series = bySeries.get(key) ?? { source: row.source, model: row.model, hours: new Map(), output: 0 };
      const acc = series.hours.get(hour) ?? { output: 0, duration: 0 };
      acc.output += output;
      acc.duration += duration;
      series.hours.set(hour, acc);
      series.output += output;
      bySeries.set(key, series);
    }
    const rateOf = (acc: { output: number; duration: number } | undefined) =>
      acc && acc.duration > 0 ? acc.output / (acc.duration / 1000) : null;
    const totalValues = labels.map(h => rateOf(totals.get(h)));
    const modelSeries = [...bySeries.entries()].sort((a, b) => b[1].output - a[1].output).slice(0, 3);
    const slots = this.seriesSlots(this.rateSeriesSlot, modelSeries.map(([key]) => key));
    const ambiguous = new Set(modelSeries.map(([, s]) => s.model).filter((m, i, all) => all.indexOf(m) !== i));
    const chartSeries = [
      { key: "__total__", label: "全部模型", values: totalValues, kind: "total" as const },
      ...modelSeries.map(([key, s]) => ({
        key,
        label: this.tokenSeriesLabel(s.source, s.model, ambiguous.has(s.model)),
        values: labels.map(h => rateOf(s.hours.get(h))),
        kind: "model" as const,
        slot: slots.get(key),
      })),
    ];
    const chart = this.tokenSeriesSvg(labels, chartSeries, {
        aria: "近 24 小时整次平均输出 Token/s 按小时趋势，逐小时悬停或按 Tab 查看数值",
        axis: (label: string) => `${label.slice(5).replace("T", " ")}:00`,
        tipTime: (label: string) => `${label.slice(5).replace("T", " ")}:00（北京时间）`,
        format: (value: number) => `${value.toFixed(2)} Token/s`,
        tick: (value: number) => value.toFixed(0),
        unit: "Token/s（整次平均输出速度）",
        scope: "小时",
        // T1: the dashed baseline is the window's WEIGHTED rate (Σ output ÷ Σ wall
        // time, the same reading as the overview card), never a mean of hours.
        mean: { label: "近 24 小时加权平均", value: (() => { let o = 0, d = 0; for (const t of totals.values()) { o += t.output; d += t.duration; } return d > 0 ? o / (d / 1000) : null; })() },
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
    spelling: string | null; covered: string[]; duplicates: string[]; rows: LogEntry[];
  } {
    const feed = this.logsFeed;
    const rows = feed && feed.state === "live" ? feed.rows : [];
    const aliases = (LOG_FEED_SOURCE_ALIAS[source] ?? []).filter(s => (LOG_FEED_SOURCES as readonly string[]).includes(s));
    if (!aliases.length) return { state: "uncovered", spelling: null, covered: [], duplicates: [], rows: [] };
    const unavailable = feed && feed.state === "live" ? feed.unavailable : undefined;
    if (unavailable === undefined) return { state: "unknown", spelling: null, covered: [], duplicates: [], rows: [] };
    const covered = aliases.filter(s => !unavailable.includes(s));
    if (!covered.length) return { state: "uncovered", spelling: null, covered, duplicates: [], rows: [] };
    const spelling = covered.find(s => rows.some(e => e.source === s)) ?? covered[0]!;
    return {
      state: "covered", spelling, covered,
      duplicates: covered.filter(s => s !== spelling),
      rows: rows.filter(e => e.source === spelling),
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
  private feedVisibleRows(): { rows: LogEntry[]; collapsed: string[]; kept: string[] } {
    const feed = this.logsFeed;
    if (!feed || feed.state !== "live") return { rows: [], collapsed: [], kept: [] };
    const unavailable = feed.unavailable;
    if (unavailable === undefined) return { rows: feed.rows, collapsed: [], kept: [] };
    const collapsed: string[] = [], kept: string[] = [];
    for (const spellings of Object.values(LOG_FEED_SOURCE_ALIAS)) {
      const requested = spellings.filter(s => (LOG_FEED_SOURCES as readonly string[]).includes(s));
      if (requested.length < 2) continue;
      const covered = requested.filter(s => !unavailable.includes(s));
      if (covered.length < 2) continue;
      const keptSpelling = covered.find(s => feed.rows.some(e => e.source === s));
      if (!keptSpelling) continue;
      kept.push(keptSpelling);
      collapsed.push(...covered.filter(s => s !== keptSpelling));
    }
    if (!collapsed.length) return { rows: feed.rows, collapsed, kept };
    const hidden = new Set(collapsed);
    return { rows: feed.rows.filter(e => !hidden.has(e.source)), collapsed, kept };
  }


  private rateDrillHtml(drill: { hour: string; source: string; model: string }): string {
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
        : "本页只读取 chat2api 调用账本，不按上游名称重复计入用量。";
      return `<div class="wb-rate-drill">${head}<p class="wb-muted">该来源不在调用日志账本内：${why}；逐次明细不可得，不以空表冒充零记录。</p></div>`;
    }
    const sourceRows = found.rows;
    if (!sourceRows.length) {
      return `<div class="wb-rate-drill">${head}<p class="wb-muted">本次读取的日志尾部没有该来源（账本 id ${escapeHtml(found.spelling ?? "")}）的任何记录：尾部读取有界，账本可能最近确实没有记录，也可能该 id 对应的账本文件缺席——两者无法据此区分；逐次明细不可得，不以空表冒充零记录。</p></div>`;
    }
    const hourKey = drill.hour.slice(0, 13);
    const rows = sourceRows.filter(e => e.model === drill.model && this.beijingHourKeyOf(e.time) === hourKey);
    const dedupeNote = found.duplicates.length
      ? `；该来源的账本以 ${escapeHtml(found.covered.join(" / "))} 多个 id 注册（同一账本），逐次明细只取 ${escapeHtml(found.spelling ?? "")} 一份，不按 id 重复计数`
      : "";
    if (!rows.length) {
      return `<div class="wb-rate-drill">${head}<p class="wb-muted">该来源在本次读取的日志尾部有 ${sourceRows.length} 条记录，但未覆盖该时段（尾部读取有界）${dedupeNote}；逐次明细不可得，不代表该时段没有请求。</p></div>`;
    }
    return `<div class="wb-rate-drill">${head}<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>时间（账本原值）</th><th>模型</th><th class="wb-fee-num">输出 token</th><th class="wb-fee-num">耗时</th><th class="wb-fee-num">本请求 Token/s</th></tr></thead><tbody>${rows.slice(0, 60).map(e => `<tr><td>${escapeHtml((e.time || "").slice(5, 19).replace("T", " "))}</td><td>${escapeHtml(e.model || "—")}</td><td class="wb-fee-num">${this.numText(e.output_tokens)}</td><td class="wb-fee-num">${this.durationText(e.duration_ms)}</td><td class="wb-fee-num">${this.logRateText(e.output_tokens, e.duration_ms)}</td></tr>`).join("")}</tbody></table></div><p class="wb-muted">逐次明细来自网关账本尾部读取（最多 ${LOG_FEED_LIMIT} 条，每来源 ${LOG_FEED_PER_SOURCE} 条的有界窗口），未覆盖的请求不在表内；时间列为账本写入的原值（国内链为 +08:00，国际链为 UTC），时段按北京时间整点比较（国际链的 UTC 时间戳已换算后匹配）${dedupeNote}；「未采集」= 账本未写该分量（后端以 null 披露），不按 0 显示。</p></div>`;
  }
  /** F2: the log lane's per-request rate cell. A missing component arrives
   *  as null from /api/console/logs (未采集), so the cell says 未采集 instead
   *  of computing a 0 Token/s out of a hole; a non-positive duration is —
   *  (no measurable wall time). A real measured 0 output with a valid duration
   *  still renders 0.00 — that 0 is a reading, not a hole. */
  private logRateText(outputTokens: unknown, durationMs: unknown): string {
    if (typeof outputTokens !== "number" || !Number.isFinite(outputTokens)
      || typeof durationMs !== "number" || !Number.isFinite(durationMs)) return "未采集";
    if (durationMs <= 0) return "—";
    return this.rateText(this.requestRate(outputTokens, durationMs));
  }
  /** Whole-request average output rate of ONE ledger row: output tokens
   *  over that request's own wall time (first-token wait included). A
   *  missing output number or a missing / non-positive duration is 未知 —
   *  never 0, which would read as a real measured stall. */
  private requestRate(outputTokens: unknown, durationMs: unknown): number | null {
    if (typeof outputTokens !== "number" || !Number.isFinite(outputTokens)) return null;
    if (typeof durationMs !== "number" || !Number.isFinite(durationMs) || durationMs <= 0) return null;
    return outputTokens / (durationMs / 1000);
  }
  private rateText(rate: number | null): string {
    return rate === null ? "—" : rate.toFixed(2);
  }
  private numText(value: unknown): string {
    return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN") : "—";
  }
  private durationText(value: unknown): string {
    return typeof value === "number" && Number.isFinite(value) && value > 0 ? `${(value / 1000).toFixed(1)}s` : "—";
  }
  /** A contract count that the backend may omit: an absent count is 未知,
   *  never a fabricated 0. */
  private countText(value: number | undefined): string {
    return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN") : "未知";
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
   *  「8096 不可达」，也不再写「受管状态损坏」这种没测过的结论。
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
  private async fetchModelPending(afterApply = false, scope: Record<string, unknown> | null = null) {
    this.modelOverviewState = "loading";
    // 记录本次回读请求的范围：读口失败行与同范围说明都要能说出请求的是哪一组
    // 范围（入页/手动读口不带范围 → null）。
    this.modelReadbackScope = scope;
    let readOk = false;
    try {
      const response = await consoleFetch(`/api/console/model-pending${this.scopeQuery(scope)}`, { credentials: "include" });
      if (!response.ok) {
        this.modelOverviewState = "http";
        this.modelOverviewStatus = response.status;
      } else {
        const data = await response.json().catch(() => null) as Record<string, unknown> | null;
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
    const requests = days.map(d => d.day_total?.requests);
    if (requests.length < 2) return "";
    const last = requests[requests.length - 1];
    const prev = requests[requests.length - 2];
    if (typeof last !== "number" || !Number.isFinite(last)
        || typeof prev !== "number" || !Number.isFinite(prev)) {
      return `<span class="wb-delta">较前日不可比较（该日请求数未提供）</span>`;
    }
    if (!prev) return `<span class="wb-delta">较前日无基数（前日 0）</span>`;
    const pct = ((last - prev) / prev) * 100;
    return Math.abs(pct) < 0.05 ? "" : `<span class="wb-delta ${pct > 0 ? "up" : "down"}">${pct > 0 ? "▲" : "▼"} ${Math.abs(pct).toFixed(1)}% 较前日</span>`;
  }
  /** Ledger status → liveness dot (R9-R32): "ok"/2xx green, 4xx/5xx/error
   *  red, anything else = 未定 (hollow ring). 契约 3.5 的形状词汇里没有「实心
   *  琥珀圆」这一档：琥珀只作**陈旧**（半填充）。原先「其他状态」借用实心
   *  琥珀圆，与进行中的操作同形同色，两个不同含义因此共用一个形状。 */
  private statusDotHtml(status: string | null | undefined): string {
    const s = (status || "").toLowerCase();
    if (!s) return "";
    if (s === "ok" || s.startsWith("2")) return `<i class="wb-dot wb-dot-run" aria-hidden="true"></i>`;
    if (s.startsWith("4") || s.startsWith("5") || s.includes("error") || s.includes("fail")) return `<i class="wb-dot wb-dot-down" aria-hidden="true"></i>`;
    return `<i class="wb-dot wb-dot-unknown" aria-hidden="true"></i>`;
  }
  private sparkSvg(values: number[]): string {
    const vals = values.slice(-7);
    const max = Math.max(...vals, 1);
    const bw = 6, gap = 3, h = 28;
    const rects = vals.map((v, i) => {
      const bh = Math.max(2, Math.round((v / max) * (h - 2)));
      const on = v > 0;
      return `<rect x="${i * (bw + gap) + 2}" y="${h - bh}" width="${bw}" height="${bh}" rx="2" fill="${on ? "var(--theme-accent)" : "var(--theme-line)"}" opacity="${on ? 0.9 : 0.55}"/>`;
    }).join("");
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
  private areaSvg(values: (number | null)[], labels: string[], unit: "cny" | "credits", width = 640, height = 190, unknownDays: boolean[] = [], partialDays: boolean[] = [], boundOnlyDays: boolean[] = []): string {
    const n = values.length;
    const axis = (label: string) => label.slice(5).replace("-", "/");
    width = timeAxisWidth(labels.map(axis), width);
    const h = height, padT = 20, padB = 25, padX = 12;
    const max = Math.max(...values.filter((v): v is number => v !== null), 1);
    const x = (i: number) => padX + (i / Math.max(n - 1, 1)) * (width - padX * 2);
    const y = (v: number) => padT + (1 - v / max) * (h - padT - padB);
    // One format per unit (contract 3.8): the same ¥ / 积分 helpers the KPI
    // and the ledger tables use, so one screen cannot show a money value
    // without thousands separators next to a count that has them.
    const format = (v: number) => unit === "cny" ? (v > 0 && v < 0.0005 ? "<¥0.001" : fmtCny(v)) : `${fmtCredits(v)} 积分`;
    const segments: number[][] = [];
    values.forEach((v, i) => { if (v === null) return; if (i === 0 || values[i - 1] === null) segments.push([]); segments.at(-1)!.push(i); });
    const paths = segments.map(indices => {
      const line = indices.map((i, j) => `${j ? "L" : "M"}${x(i).toFixed(1)},${y(values[i]!).toFixed(1)}`).join(" ");
      const fill = indices.length > 1 ? `<path d="${line} L${x(indices.at(-1)!).toFixed(1)},${h - padB} L${x(indices[0]).toFixed(1)},${h - padB} Z" class="wb-area-fill"/>` : "";
      // 含下界日的段画虚线（.wb-area-line-partial），与完整段（实线）分开。
      const partial = indices.some(i => partialDays[i] === true);
      return `${fill}<path d="${line}" class="wb-area-line${partial ? " wb-area-line-partial" : ""}"${partial ? ` style="stroke-dasharray:3 3"` : ""}/>`;
    }).join("");
    const baselineMarkers = values.map((v, i) => {
      const kind = chartBaselineKind(v);
      return kind ? `<line class="wb-chart-baseline-${kind}" x1="${x(i).toFixed(1)}" x2="${x(i).toFixed(1)}" y1="${h - padB - 8}" y2="${h - padB}" style="stroke:var(--theme-muted)" aria-hidden="true"/>` : "";
    }).join("");
    const points = segments.filter(indices => indices.length === 1).map(indices => {
      const i = indices[0];
      // 下界日画空心点（内色 = 当前主题面板色 + accent 描边），完整日点保持
      // .wb-area-single。维护者 2026-09-28（PR8 遗留 ② / Plan-Revision 9 费用页）：
      // 内色必须取自当前主题的面板色令牌，不得写死浅色值——深色主题下写死 #ffffff
      // 会在深色面板上留一个浅色实心点，与「空心」形制自相矛盾；用令牌后两个主题
      // 各自取到自己的面板色，写死同一个值即判红。
      return partialDays[i] === true
        ? `<circle cx="${x(i).toFixed(1)}" cy="${y(values[i]!).toFixed(1)}" r="2.7" class="wb-area-partial" style="fill:var(--theme-panel);stroke:var(--theme-accent)" stroke-width="1.5"/>`
        : `<circle cx="${x(i).toFixed(1)}" cy="${y(values[i]!).toFixed(1)}" r="2.7" class="wb-area-single"/>`;
    }).join("");
    const hotWidth = (width - padX * 2) / Math.max(n - 1, 1);
    const hotspots = values.map((v, i) => {
      const cx = x(i);
      // null 的两种状态各写各的（维护者 2026-09-28 ②）：只含可折算记录且可折算
      // 为空的日子与链行同写「未知（下界仅含可折算部分）」，绝不折 0 或写数值下界。
      const value = v === null
        ? (boundOnlyDays[i] === true
          ? "未知（下界仅含可折算部分，当日有账本记录）"
          : unknownDays[i] ? "分量未知（当日有账本记录）" : "无记录")
        : (partialDays[i] === true ? `已知部分 ≥${format(v)}（当日含不可折算记录）` : format(v));
      const date = labels[i] ?? "";
      const tip = this.areaTip([{ text: date, value: false }, { text: value, value: true }]);
      const tipX = Math.min(Math.max(cx - tip.width / 2, 2), Math.max(2, width - tip.width - 2));
      const dot = v === null ? "" : `<circle cx="${cx.toFixed(1)}" cy="${y(v).toFixed(1)}" r="4" class="wb-area-focus-dot"/>`;
      return `<g class="wb-area-hotspot" tabindex="0" role="img" aria-label="${escapeHtml(date)}：${escapeHtml(value)}">
        <rect x="${(cx - hotWidth / 2).toFixed(1)}" y="0" width="${hotWidth.toFixed(1)}" height="${h - padB}" class="wb-area-hit"/>
        <line x1="${cx.toFixed(1)}" x2="${cx.toFixed(1)}" y1="${padT}" y2="${h - padB}" class="wb-area-crosshair"/>${dot}
        <g class="wb-area-tip" transform="translate(${tipX.toFixed(1)},1)">${tip.plate}</g>
      </g>`;
    }).join("");
    const ref = this.trendRef(values, x, y, padX, width, partialDays);
    // Gridline values carry just enough decimals for their range (the exact
    // reading is in the tooltip); an empty chart gets no scale at all.
    const digits = max >= 100 ? 0 : max >= 10 ? 1 : 2;
    const tick = (v: number) => `${unit === "cny" ? "¥" : ""}${v.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
    const hasData = values.some(v => v !== null);
    const stats = this.chartStatsHtml({ values, labels, axis, format, unit: unit === "cny" ? "人民币（估算费用）" : "积分", totalLabel: "区间合计", partialValues: partialDays, scope: "日" });
    const grid = this.gridLines(max, padX, width, y, hasData ? tick : undefined);
    return `${stats}<div class="wb-chart-scroll"><svg class="wb-area" style="min-width:${width}px" viewBox="0 0 ${width} ${h}" role="group" aria-label="近 ${n} 天趋势，逐日悬停或按 Tab 查看数值">
      ${grid.lines}
      ${paths}${baselineMarkers}${points}${ref.line}${grid.labels}
      ${hotspots}
      ${this.axisTicks(labels, axis, x, h)}
    </svg></div>`;
  }
  private feeTrendHtml(): string {
    const cost = this.trendDays("costCny", 14);
    const hasCost = cost.values.some((v, i) => v !== null || cost.unknown[i] || cost.boundOnly[i]);
    const chart = this.areaSvg(cost.values, cost.labels, "cny", 1000, 280, cost.unknown, cost.partial, cost.boundOnly);
    return '<section class="wb-block wb-char"><h3>近 14 天趋势</h3><div class="wb-fee-trends wb-fee-trends-single"><div class="wb-fee-trend" data-wb-series="cny"><h4>chat2api · ¥</h4>' + chart + '</div></div>'
      + (hasCost ? '' : '<p class="wb-muted">近 14 天无账本记录；无法判定为零用量。</p>')
      + this.laneNoteFoldHtml("fee-trend", "图例与断线口径", '<p class="wb-muted">每日原值；无记录或分量未知的日期断线，账本记录的 0 正常显示；只含可折算记录的下界画空心点与虚线，不冒充完整费用。</p>') + '</section>';
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
    const rows: { label: string; field: "requests" | "costCny" | "credits"; format: (v: number) => string; bound: (v: number, partial: boolean) => string }[] = [
      { label: "请求数", field: "requests", format: fmtRequests, bound: fmtRequests },
      { label: "估算费用", field: "costCny", format: fmtCny, bound: (v, partial) => (partial ? `≥${fmtCny(v)}` : fmtCny(v)) },
    ];
    const body = rows.map(row => {
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
      return `<div class="wb-cred-row"><span class="wb-cred-k">${row.label}</span><span class="wb-cred-v">`
        + `本期 ${escapeHtml(row.bound(pair.now, anyPartial))} · 上期 ${escapeHtml(row.bound(pair.prev, anyPartial))} ${this.deltaChip(pair)}`
        + `<span class="wb-delta-bar" aria-hidden="true"><i style="width:${prevWidth}%"></i><i style="width:${nowWidth}%"></i></span>`
        + `<small class="wb-muted" title="差值条：上期 / 本期按打印值的比例示意（不按刻度读数）">上期 / 本期比例 <span class="wb-sim-chip">示意</span></small>`
        + `</span></div>`;
    }).join("");
    return `<section class="wb-block wb-period"><h3>本期 vs 上期</h3>`
      + `<div class="wb-period-rows">${body}</div>`
      + this.laneNoteFoldHtml("period", "窗口口径", `<p class="wb-muted">窗口：近 14 天账本（本期 = 最近 7 天，上期 = 再往前 7 天）；逐日原值见趋势图，缺失日不按 0 计。任一侧含下界日（只含可折算记录）时，本期 / 上期两侧都写成 ≥¥…（整行标 partial），环比不再给无条件精确涨跌。</p>`)
      + `</section>`;
  }
  /** Model usage ranking (Bar List, Rev12 F12-2): per-model Token (input + output)
   *  from the aggregated ledgers, largest first. A model with requests whose usage
   *  is unknown keeps its known Token and says 部分 (those requests are not
   *  counted); a model whose rows carry no Token at all reads 未知, not 0. */
  private barListHtml(top = 6): string {
    const rows = (this.summarySeries?.models ?? []).slice(0, top);
    if (!rows.length) return `<p class="wb-muted">账本暂无模型级记录。</p>`;
    const max = Math.max(...rows.map(m => m.tokens), 1);
    // 维护者 2026-09-28（问题 2 · 排行行名截断）：行名被省略号截断时，完整名称在
    // `title` 里（同一个名字，仍走 `escapeHtml`）；柱长与读数位置不变。
    const rowHtml = (m: ModelPoint) => {
      const partial = m.unknownRequests > 0 || m.tokensMissing;
      const none = m.tokens === 0 && partial;
      const note = m.unknownRequests > 0 ? `${fmtRequests(m.unknownRequests)} 次请求用量未知，未计入` : "部分记录没有 Token 字段，未计入";
      const value = none ? "未知" : fmtTokens(m.tokens);
      const mark = partial && !none ? ` <small class="wb-barlist-partial" title="${escapeHtml(note)}">部分</small>` : "";
      return `<div class="wb-barlist-row"><span class="wb-barlist-name" title="${escapeHtml(m.model)}">${escapeHtml(m.model)}</span>`
        + `<span class="wb-barlist-bar"><i style="width:${none ? 0 : Math.max(2, (m.tokens / max) * 100).toFixed(1)}%"></i></span>`
        + `<span class="wb-barlist-val"${partial ? ` title="${escapeHtml(note)}"` : ""}>${value}${mark}</span></div>`;
    };
    return `<div class="wb-barlist">${rows.map(rowHtml).join("")}</div>`;
  }
  /** Whether the remembered evidence has aged past HEALTH_STALE_AFTER_MS.
   *  Failed snapshot reads keep the last verdict on the card, so without this
   *  check an old reading would keep passing itself off as the present. */
  private probeStale(result: ProbeResult): boolean {
    return result.observedAt !== undefined && Date.now() - result.observedAt > HEALTH_STALE_AFTER_MS;
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
    const suffix = result.status === 401 || result.status === 403
      ? "（鉴权应答：入口有响应；账号可用性未核）"
      : result.status && result.status >= 500 ? "（服务错误或失联）" : "";
    const at = result.observedAt;
    const clock = new Date(at ?? 0);
    const hh = String(clock.getHours()).padStart(2, "0");
    const mm = String(clock.getMinutes()).padStart(2, "0");
    const ss = String(clock.getSeconds()).padStart(2, "0");
    const seen = at === undefined ? "" : ` · 检查于 ${hh}:${mm}:${ss}`;
    const stale = this.probeStale(result)
      ? " · 证据已过期（连续读取失败，显示的是上次成功结果）" : "";
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
    const at = (epoch?: number) => epoch ? new Date(epoch * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "时点未知";
    const [short, full] = !state
      ? ["服务端循环探测 · 30s", "健康快照平时由服务端循环探测并短 TTL 缓存；手动重探是一次有界只读 GET，不产生操作记录、不写任何状态。"]
      : state.state === "busy"
        ? ["重探中…", "手动重探中…（一次有界只读 GET，不产生操作记录、不写任何状态）"]
        : state.state === "ok"
          ? [`已重探 ${at(state.at)}${state.cached ? "（服务端缓存）" : ""}`, `已重探：服务端于 ${at(state.at)} 重新探测（本次${state.cached ? "仍命中服务端缓存" : "为真实重探"}）；证据与检查时点已更新。`]
          : state.state === "unavailable"
            ? [`重探未载入（HTTP ${state.status}）`, `当前服务端版本未载入：GET /api/console/health?refresh=1 实测 HTTP ${state.status}（该参数被忽略或路由未载入）；健康快照仍由服务端循环探测提供。`]
            : [`重探失败${state.status ? `（HTTP ${state.status}）` : ""}`, `手动重探失败：${state.reason}${state.status ? `（HTTP ${state.status}）` : ""}；保留上一次真实证据，不判宕机、不折成空表。`];
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
    const row = this.actions?.find(a => a.action === status.action);
    return row?.enabled ? status : undefined;
  }
  /** Why a chain has no login query right now, one short phrase plus a sentence. */
  private statusGap(s: ServiceRow): { word: string; why: string } {
    const status = s.statusAction;
    if (!status) return { word: "不支持查询", why: "该链未提供只读认证接口" };
    if (!this.actions) return { word: "登录查询待确认", why: "动作注册表尚未读到，暂不提交查询" };
    const row = this.actions.find(a => a.action === status.action);
    if (!row) return { word: "登录查询尚未接入", why: `当前后端版本未注册 ${status.action}` };
    return { word: "登录查询已停用", why: row.disabled_reason || `${status.action} 已停用` };
  }
  private accountActionsHtml(s: ServiceRow): string {
    // 主按钮 = 下一主动作（`accountNextLogin`）：通常是该链的只读状态查询；真实返回
    // 说未认证时换成登录动作（仍先二次确认）。其余都是次级按钮。
    const nextLogin = this.accountNextLogin(s)?.action;
    const tier = (primary: boolean) => primary ? "wb-btn wb-action-primary" : "wb-btn";
    const statusAction = this.statusActionOf(s);
    const gap = this.statusGap(s);
    const status = statusAction
      ? `<button class="${tier(!nextLogin)}" data-wb-acct-action="${statusAction.action}" data-wb-acct-target="${statusAction.target}" title="经 8096 适配层提交只读账号查询（无副作用，不产生计费请求）">${escapeHtml(statusAction.label)}</button>`
      : `<span class="wb-muted">${escapeHtml(s.statusAction ? `${gap.word}：${gap.why}` : "此来源未提供账号管理接口")}</span>`;
    const manage = (s.manageActions ?? []).map(a => `<button class="${tier(a.action === nextLogin)}" data-wb-acct-action="${a.action}" data-wb-acct-target="${a.target}" title="${escapeHtml(a.title)}">${escapeHtml(a.label)}</button>`).join("");
    return status + manage;
  }
  /** The main chain's verdict word and its dot state, shared by the overview
   *  verdict and the console-wide head status so the two can never disagree.
   *  No evidence yet stays 未探测 (hollow ring), never a default green. */
  private mainVerdict(): { word: string; dot: "run" | "down" | "stale" | "unknown" } {
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
    const rows = CHAINS.map(s => ({ has: this.live.has(s.id), state: this.dotState(s.id) }));
    const mainWord = this.mainVerdict().word;
    const down = rows.filter(r => r.state === "down").length;
    const unknown = rows.filter(r => r.has && r.state === "unknown").length;
    const stale = rows.filter(r => r.state === "stale").length;
    const probeless = rows.filter(r => !r.has).length;
    const need = down + unknown + stale;
    const needDetail = [
      down ? `未监听 ${down}` : "",
      unknown ? `探测未定 ${unknown}` : "",
      stale ? `证据过期 ${stale}` : "",
    ].filter(Boolean).join(" · ");
    const probelessNote = probeless ? `；另有 ${probeless} 条尚无探针证据（不判宕机）` : "";
    const note = probeless === rows.length
      ? `${CHAIN_COUNT_WORD}链尚无探针证据（8096 健康快照未返回）：不判「正常」，也不判「宕机」。`
      : need
        ? `${CHAIN_COUNT_WORD}链中 <b>${need}</b> 条需要处理（${needDetail}）；其余 ${rows.length - need - probeless} 条监听中${probelessNote}。`
        : `${CHAIN_COUNT_WORD}链全部监听中${probelessNote}。`;
    // PR11: the verdict chip carries a shape per state beside the word (check /
    // alert / clock / search), so the state never rests on the chip's colour.
    const dot = this.mainVerdict().dot;
    const icon = dot === "run" ? "check" : dot === "down" ? "alert" : dot === "stale" ? "clock" : "search";
    return `<div class="wb-verdict" data-state="${dot}"><div class="wb-verdict-value">${wbIcon(icon)}${escapeHtml(mainWord)}</div><p class="wb-verdict-note">${note}</p>${this.healthRefreshHtml()}</div>`;
  }
  /** Overview 「需要处理」 queue (contract 4.2 / 3.9): one row per chain whose
   *  current verdict is NOT a clean 监听中, each carrying the REAL entry points
   *  that act on it (管理详情 / 该链日志). A chain with no evidence gets its own
   *  row instead of disappearing from the queue, and a stale chain says 证据过期
   *  rather than 未监听 — the queue reads the same `this.live` evidence as the
   *  board above it, so the two can never disagree. */
  private queueHtml(hasIssues = false): string {
    const rows = CHAINS
      .map(s => ({ s, has: this.live.has(s.id), state: this.dotState(s.id) }))
      .filter(r => !(r.has && r.state === "run"));
    // Contract 3.9: the queue is RESIDENT on the overview's right, so the
    // `wb-queue` container renders in the healthy state too — with one honest
    // empty row — instead of the container disappearing behind a bare <p>.
    // 同一区块里已经列着失败操作（`hasIssues`）时不得再写「当前没有需要处理的项」：
    // 那会把 3 条真实失败读成 0。此时区块由失败组头（计数 + 动作 + 展开行）
    // 承载，链自己的空态行省略；队列本身仍常驻（组头就是常驻行）。
    if (!rows.length) {
      if (hasIssues) return "";
      return `<div class="wb-queue"><div class="wb-queue-item wb-queue-item-run"><i class="wb-dot wb-dot-run" aria-hidden="true"></i>`
        + `<div class="wb-queue-head"><strong>当前没有需要处理的项</strong></div>`
        + `<div class="wb-queue-ops"><span class="wb-muted">${CHAIN_COUNT_WORD}链探针证据均新鲜且监听中（陈旧 ≠ 宕机；尚无探针证据的链仍会在此列出，不折成 0）。</span></div></div></div>`;
    }
    const word = (r: { has: boolean; state: string }) => !r.has ? "尚无探针证据"
      : r.state === "down" ? "未监听"
        : r.state === "stale" ? "证据过期"
          : r.state === "run" ? "监听中" : "探测未定";
    const badge = (r: { has: boolean; state: string }) => !r.has ? "idle"
      : r.state === "down" ? "danger"
        : r.state === "stale" ? "warn" : "idle";
    // 首屏密度（契约 4.2）：8096 健康快照不可达时各链**都没有**探针证据，
    // 逐链一行会让「需要处理」块高到把「服务明细」挤出首屏。没有任何证据
    // 的链合成**一行**列出（链名一个不少、按钮一条不少），未知仍是未知，
    // 只是不再用五行同一句话占首屏高度——待处理本身没有被折叠隐藏。
    const probeless = rows.filter(r => !r.has);
    // 链名只写一次：按钮本身就是逐链入口（每个按钮带链名与 title），
    // 再列一遍同一批名字既是重复，也会把这一行撑到卡片右缘之外。
    const probelessRow = `<div class="wb-queue-item"><i class="wb-dot wb-dot-unknown" aria-hidden="true"></i>`
      + `<div class="wb-queue-head"><strong>${probeless.length} 条链尚无探针证据</strong><span class="wb-badge wb-badge-idle">尚无探针证据</span><span class="wb-muted">不判宕机、也不判正常，不折成 0</span></div>`
      + `<div class="wb-queue-ops">`
      + probeless.map(r => `<button data-wb-svc-detail="1" data-wb-detail="${r.s.id}" title="查看 ${escapeHtml(r.s.name)}（端口 ${r.s.port}）的运行快照、探针证据与可用操作">${wbIcon("detail")}${escapeHtml(r.s.name)}</button>`).join("")
      + `</div></div>`;
    return `<div class="wb-queue">${rows.map(r => {
      if (!r.has) return "";
      const logSource = this.serviceLogSource(r.s.id);
      return `<div class="wb-queue-item${r.state === "run" ? " wb-queue-item-run" : ""}"><i class="wb-dot wb-dot-${r.state}" aria-hidden="true"></i>`
        + `<div class="wb-queue-head"><strong>${escapeHtml(r.s.name)}</strong><span class="wb-port">${r.s.port}</span><span class="wb-badge wb-badge-${badge(r)}">${word(r)}</span></div>`
        + `<div class="wb-queue-ops"><button data-wb-svc-detail="1" data-wb-detail="${r.s.id}" title="查看运行快照、探针证据、账号管理与可用操作" aria-label="查看该链管理详情">${wbIcon("detail")}${wbIconHint("查看该链管理详情")}</button>`
        + (logSource ? `<button data-log-open="${escapeHtml(logSource)}" title="直达该网关运行日志详情（与日志页同一详情）" aria-label="查看该链日志详情">${wbIcon("logs")}${wbIconHint("查看该链日志详情")}</button>` : "")
        + `</div></div>`;
    }).join("")}${probeless.length ? probelessRow : ""}</div>`;
  }
  /** In-place probe evidence refresh on the probe-marked chips/rows (no full
   *  re-render): dot class + appended evidence on the stored base title. */
  private applyLive() {
    this.root.querySelectorAll<HTMLElement>("[data-wb-probe]").forEach(el => {
      const id = Number(el.dataset.wbProbe);
      const result = this.live.get(id);
      if (!result) return;
      const dot = el.querySelector(".wb-dot");
      if (dot) dot.className = `wb-dot wb-dot-${this.dotState(id)}`;
      if (!el.dataset.wbBaseTitle) el.dataset.wbBaseTitle = el.getAttribute("title") ?? "";
      el.setAttribute("title", `${el.dataset.wbBaseTitle} · ${this.probeEvidence(id)}`);
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
    let out = 0, ms = 0;
    for (const r of read.payload.hours) {
      if (Number.isFinite(r.output_tokens) && r.output_tokens >= 0 && Number.isFinite(r.duration_ms) && r.duration_ms > 0) { out += r.output_tokens; ms += r.duration_ms; }
    }
    const stale = read.state === "live" ? "" : " · 本次刷新失败，为上次值";
    if (ms <= 0) {
      const recorded = Object.values(read.payload.recorded_requests ?? {});
      const some = recorded.some(v => typeof v === "number" && v > 0);
      const allZero = recorded.length > 0 && recorded.every(v => v === 0);
      return some ? { text: "不可计算", state: `有请求但缺输出 Token 或耗时${stale}` }
        : allZero ? { text: "无记录", state: `近 24 小时没有请求${stale}` }
          : { text: "未知", state: `近 24 小时没有可计算的分桶，账本覆盖未知${stale}` };
    }
    const tps = out / (ms / 1000);
    return { text: tps.toLocaleString("zh-CN", { minimumFractionDigits: 1, maximumFractionDigits: 1 }), state: `近 24 小时 · ${fmtTokens(out)} 输出 Token${stale}` };
  }
  private tpsKpiHtml(): string {
    const r = this.tpsReading();
    const formula = "整次平均输出速度 = 近 24 小时有效输出 Token 总和 ÷ 有效请求耗时总和（含首字等待）。按小时与模型分桶加权，不平均各小时速率；缺输出 Token 或耗时的请求不进分子与分母。";
    return `<div class="wb-kpi wb-kpi-rate" title="${escapeHtml(formula)}"><small>${kpiChip("pulse", "tok")}整次平均输出速度 · Token/s</small><b data-kpi="tps">${escapeHtml(r.text)}</b>`
      + `<div class="wb-kpi-state"><small>${escapeHtml(r.state)}</small></div></div>`;
  }
  /** KPI numbers/captions update in place after a summary read; undefined
   *  KPI renders an honest em-dash, never a fixture. 费用格沿用与首屏同一段
   *  读数文本与同一个小字标记（partial 时 ≥¥… +「已知部分」），两条路径不可能漂移。 */
  private applySummary() {
    const values = this.kpi();
    this.root.querySelectorAll<HTMLElement>("[data-kpi]").forEach(el => {
      const key = el.dataset.kpi;
      if (key === "cost") {
        const amount = values === undefined ? undefined : this.displayedCost();
        el.textContent = costAmountReading(amount);
        // 状态属性在格容器上：读数现在位于 `.wb-reading` 行内，不再是格的直接子元素，
        // 所以按属性往上找格，而不是取 parentElement（那会命中读数行）。
        const cell = el.closest<HTMLElement>("[data-wb-cost-state]");
        if (cell) {
          cell.dataset.wbCostState = costAmountState(amount);
          cell.querySelectorAll("[data-wb-cost-marker]").forEach(marker => marker.remove());
          el.insertAdjacentHTML("afterend", costAmountMarker(amount));
        }
        return;
      }
      // Readings that do not come from the summary keep their own sources.
      if (key === "tps") { el.textContent = this.tpsReading().text; return; }
      if (values === undefined) { el.textContent = "—"; return; }
      if (key === "tokens") el.textContent = fmtTokens(values.tokens);
      // The reading equals the sum of the rows printed beside it (contract 3.8).
      else if (key === "credits") el.textContent = this.displayedCredits() ?? "—";
    });
    // Two resident notes: the overview keeps the 累计口径 sentence alone, while
    // the fee lane's note additionally carries 缺失日 N and 价目核对 (contract
    // 4.5 — those two facts stay on the resident strip, never demoted into a
    // wb-info tooltip). Both are re-derived here because the series and the
    // price table they read land in the same pass as the summary.
    this.root.querySelectorAll<HTMLElement>("[data-wb-summary-note]").forEach(el => { el.textContent = this.summaryNote(); });
    this.root.querySelectorAll<HTMLElement>("[data-wb-cost-note]").forEach(el => {
      el.textContent = this.summaryNote() + this.costResidentNote();
    });
  }
  private syncElements() {
    this.stage.dataset.workbenchBrand = String(!this.enabled || this.visibility.brand);
    this.stage.dataset.workbenchFooter = String(!this.enabled || this.visibility.footer);
    this.stage.dataset.workbenchSettings = String(!this.enabled || this.visibility.settings);
  }
  /** Head stamp (contract 4.2): WHEN the shown evidence was read, taken from
   *  the server health snapshot's own `checked_at` — never from the page's paint
   *  time, so a repaint cannot pass itself off as a fresh reading. With no
   *  snapshot at all the stamp says so instead of printing the local clock. */
  private headStamp(): string {
    let newest: number | undefined;
    for (const result of this.live.values()) {
      if (result.observedAt !== undefined && (newest === undefined || result.observedAt > newest)) {
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
      if (info) { info.hidden = true; info.textContent = ""; }
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
      const note = [LANE_CAPTIONS[this.lane] ?? "", HEAD_INFO[this.lane] ?? ""].filter(Boolean).join(" · ");
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
    if (status) {
      // Written only when it changes: the pill is a polite live region, and a
      // repaint with the same verdict must not be announced again.
      const verdict = this.mainVerdict();
      if (status.dataset.state !== verdict.dot) {
        status.dataset.state = verdict.dot;
        status.querySelector(".wb-dot")?.setAttribute("class", `wb-dot wb-dot-${verdict.dot}`);
      }
      const text = status.querySelector(".wb-head-status-text");
      if (text && text.textContent !== verdict.word) text.textContent = verdict.word;
    }
  }
  private renderAll() {
    this.renderNav();
    this.renderPanel();
    this.syncElements();
  }
  private renderNav() {
    this.root.querySelectorAll<HTMLButtonElement>("[data-wb-lane]").forEach(b => b.setAttribute("aria-pressed", String(+b.dataset.wbLane! === this.lane)));
    this.renderHead();
  }
  tick(t = Date.now()) {
    if (Math.floor(t / 1000) === this.lastSecond) return;
    this.lastSecond = Math.floor(t / 1e3);
    const d = new Date(t);
    rollText(this.root.querySelector(".wb-clock")!, d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }), !this.stage.classList.contains("reduce-motion"));
    this.root.querySelector(".wb-date")!.textContent = d.toLocaleDateString("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "long" });
  }
  private renderPanel() {
    if (this.gwDrag || this.logDrag) { this.gwRepaintDeferred = true; return; }
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
      // 账本可读 ≠ 运行快照可读：`Boolean(this.runtime)` 曾把「8096 运行快照
      // 读到了」当成「操作账本读到了」，于是账本读失败时这里仍断言
      // 「台账为空或全部成功」——未知被写成了零。
      const ledgerLive = this.opsRecent?.state === "live";
      const failures = ledgerLive ? this.opsRecent!.rows.filter(op => op.state === "failed") : [];
      // 读取在途（`undefined`）与读取失败（`gated`）分开：在途时写「读取中」，
      // 只有真的读失败才写实测原因——应答了 HTTP 错误就写应答错误。
      // 失败操作按 action 聚合成组头（与操作记录页的失败分组同一模式：组头写明
      // action 与失败数，点击才展开单行详情）。维护者 2026-09-28：待处理行高
      // 48–56px、长失败原因进服务行/ⓘ 详情——三条 share 失败各约 98px 内联列出会把
      // 「服务明细」挤出首屏（1536×864/dsf1.25 错误态实测：标题底 975 > 864、
      // 区块起点 1048 > 864）；聚合成组头后同一界面首屏四层重新成立，且失败一条
      // 不少：组头有计数与动作、展开后单行详情带时间、目标、错误码与全文。
      const issuesHtml = this.opsRecent === undefined
        ? `<p class="wb-muted">操作账本读取中…（未返回前不写「未读到」）。</p>`
        : !ledgerLive
          ? `<p class="wb-muted">${escapeHtml(this.opsReadFailureText())}（未知，不是「没有失败的操作」）。</p>`
        : failures.length
          ? (() => {
            const groups = new Map<string, OpRow[]>();
            for (const op of failures) {
              const list = groups.get(op.action) ?? [];
              list.push(op);
              groups.set(op.action, list);
            }
            return `<div class="wb-fail-groups">${Array.from(groups.entries()).map(([action, ops]) => {
              const on = this.failuresOpen.has(action);
              // 同组错误码一致时才在组头写它（混合错误码不挑一个冒充全部）。
              const codes = new Set(ops.map(op => op.error?.code ?? "failed"));
              const code = codes.size === 1 ? [...codes][0] : null;
              const detail = on
                ? `<div class="wb-fail-detail">${ops.map(op => `<div class="wb-op"><span class="wb-op-time">${this.opTime(op.created_at)}</span><span>${escapeHtml(op.action)} → ${escapeHtml(op.target || "—")}</span><b>${escapeHtml(op.error?.code ?? "failed")}${op.error?.message ? ` · ${escapeHtml(op.error.message)}` : ""}</b><i class="wb-dot wb-dot-down" aria-hidden="true"></i></div>`).join("")}</div>`
                : "";
              return `<div class="wb-fail-group"><button type="button" class="wb-fail-head${on ? " wb-fail-on" : ""}" data-wb-fail-toggle="${escapeHtml(action)}" aria-expanded="${on}" title="展开 / 收起 ${escapeHtml(action)} 的失败单行详情（长失败原因在这里展开，不占首屏高度）"><i class="wb-dot wb-dot-down" aria-hidden="true"></i><span>${escapeHtml(action)}</span><b>失败 ×${ops.length}${code ? ` · ${escapeHtml(code)}` : ""}</b></button>${detail}</div>`;
            }).join("")}</div>`;
          })()
          : `<p class="wb-muted">账本已读取，没有失败的操作记录。</p>`;
      // 需要处理 and 服务明细 share one row (PR11: the resident queue in the
      // narrow left column, the master-detail panel wide on the right), so the
      // fourth first-screen layer starts where the third does instead of below
      // it. DOM order is the frozen reading order (verdict → usage → queue →
      // services) and now equals the paint order.
      // PR11 (maintainer 2026-09-30, Q17): 服务明细 is a master-detail panel on
      // the dark surface — the list names every chain with its probe shape, its
      // runtime word, its port and any in-flight or terminal action state; the
      // detail beside it is the chain's full management detail (probe evidence,
      // runtime snapshot, account status and actions, start / restart / stop
      // behind the existing confirm, logs). Stop lives in the detail only, so a
      // destructive action always takes a deliberate selection first.
      // 契约 3.12b：探针说明是解释性内容，默认折叠。折叠的是说明文本，
      // 各链摘要本身与每一行的状态词仍在首层；展开后说明全文一条不少。
      const overviewServices = isOverview
        ? `<section class="wb-block wb-ov-services wb-char" aria-labelledby="wb-ov-services-title"><div class="wb-section-head"><h3 id="wb-ov-services-title">服务明细</h3><small>选择一条链查看状态、端口与可用动作</small></div>`
          + `<div class="wb-md"><div class="wb-md-side"><div class="wb-md-list" role="group" aria-label="服务链（↑ / ↓ 切换）">${CHAINS.map(s => this.serviceItemHtml(s)).join("")}</div>`
          + `<details class="wb-secondary-details"${this.probeIntroOpen ? " open" : ""} data-wb-probe-intro="1"><summary title="探针说明（来源 / 检查时点 / 陈旧口径 · ${CHAIN_COUNT_WORD}链）· 点击展开 / 收起">探针说明${wbFoldMark()}</summary><p class="wb-muted">状态词与探针时点来自 8096 健康快照（/api/console/health · 30s TTL），启停经 8096 适配层真实执行并需二次确认；陈旧证据只降新鲜度，不判宕机。端口、探针方法/耗时、运行快照与认证说明在所选链的详情里；手动重探在页首运行裁决右侧（usagesrv.py 的 refresh=1 真实重探）。</p></details></div>`
          + this.serviceDetailHtml(this.ovService, "overview")
          + `</div></section>`
        : "";
      // A12-3 (Plan-Revision 12): the main trend is the 14-day DAILY COMPLETE
      // token series, the per-model request ranking reads the three-chain ledger
      // aggregate, and the 24h whole-request output-rate panel follows. Each card
      // carries its own honest state. Contract 4.2: the trend section is the
      // second tier and never takes part in the verdict above it.
      // Contract 5.2 #25 + 3.6: a 总览级统一时间轴 endpoint really does NOT exist
      // (14 天走 usage-tokens、24 小时走 usage-rates、7 天走 stats), so the control
      // stays `disabled` with its evidence in the title and 「后端未提供」 visible.
      const chartGrid = isOverview
        ? `<section class="wb-ov-trends"><div class="wb-section-head"><h3>用量趋势与速率</h3><small>第二层 · 不参与上方的可用性裁决</small><button type="button" class="wb-btn wb-btn-sm" disabled title="后端未提供：/api/console 只有 usage-tokens（14 天）/ usage-rates（24 小时）/ stats（近 7 天）三个窗口各不相同的接口；本页不把三条曲线画成一条。">统一时间轴 · 后端未提供</button></div>`
          + `<div class="wb-chart-grid">`
          + `<section class="wb-chart-card wb-chart-wide" data-wb-series="tok"><div class="wb-chart-head"><h3>每日完整 Token · 近 14 天</h3><small>输入+输出齐全的行合计 · 北京时间自然日 · 缺分量的历史另列</small></div>${this.tokenTrendHtml()}</section>`
          + `<section class="wb-chart-card"><div class="wb-chart-head"><h3>Token 构成</h3><small>按网关 · 近 14 天完整记录</small></div>${this.tokenCompositionHtml()}`
          + `<div class="wb-chart-head wb-chart-subhead"><h4>按模型排行</h4><small>Token（输入 + 输出）· 三链账本累计</small></div>${kpiValues ? this.barListHtml(6) : `<p class="wb-muted">${escapeHtml(this.summaryNote())}</p>`}</section>`
          + `<section class="wb-chart-card wb-chart-wide" data-wb-series="tok"><div class="wb-chart-head"><h3>整次平均输出 Token/s · 近 24 小时</h3><small>输出 ÷ 整次耗时（含首字等待）· 按小时与模型分组 · 跳过记录不参与分母</small></div>${this.ratePanelHtml()}</section>`
          + `</div></section>`
        : "";
      // 总览首屏顺序（维护者 Plan-Revision 3 定案）：① 运行裁决 → ② 用量与费用
      // → ③ 待处理（常驻，左栏）→ ④ 服务明细（右栏主从面板，与③同一行）→ 第二层趋势。
      // 读数条是无框分区（`wb-bare`）：三张读数卡自己就是卡，外面不再套第二圈框。
      const kpiBlock = `<section class="wb-block wb-bare wb-ov-usage"><h3>用量与费用</h3>`
        // 契约 3.3：KPI 条内**不渲染** `wb-spark`（装饰性七根柱没有坐标也没有刻度）。
        // 真实趋势仍在下方趋势区与逐日 tooltip 里。
        + `<div class="wb-kpis">`
        + `<div class="wb-kpi"><small>${kpiChip("layers", "tok")}Token (in+out)</small><b data-kpi="tokens">${kpiValues ? fmtTokens(kpiValues.tokens) : "—"}</b>${kpiValues ? this.kpiWindowHtml(this.tokenWindowPair(), fmtTokens, "14 天完整 Token 序列未读取或无记录") : ""}</div>`
        + this.tpsKpiHtml()
        + `<div class="wb-kpi wb-kpi-hero" data-wb-cost-state="${this.costReadingState()}"><small>${kpiChip("cost", "cost")}估算费用</small>${kpiValues ? this.costCellHtml("b") : `<b data-kpi="cost">—</b>`}${kpiValues ? this.kpiWindowHtml(this.windowPair("costCny"), fmtCny, "账本日不足 7 天或该分量未记录", (v, partial) => (partial ? `≥${fmtCny(v)}` : fmtCny(v))) : ""}</div>`
        // 维护者 2026-09-28（Plan-Revision 9 逐页收纳 · 总览）：「标签 + 读数 + 一行真实状态」；
        // 区间口径进标题的 ⓘ 详情（HEAD_INFO[0]），状态行与刷新按钮同排。
        + `</div>`
        + `<p class="wb-muted wb-cost-note" data-wb-summary-note>${escapeHtml(this.summaryNote())}</p></section>`;
      const queueBlock = `<section class="wb-block wb-ov-queue"><h3>需要处理</h3>`
        // 队列自己的空态行已写明边界（尚无探针证据的链仍会在此列出，不折成 0），
        // 标题下不再另写一句同义范围行（§2.11a.2）。
        + this.queueHtml(failures.length > 0) + issuesHtml
        + `<div class="wb-block-foot"><button type="button" class="wb-btn wb-btn-sm" data-wb-lane="2">${wbIcon("ops")}查看操作记录</button></div></section>`;
      html = (isOverview
        ? this.verdictHtml()
        + kpiBlock
        + `<div class="wb-ov-grid">${queueBlock}${overviewServices}</div>`
        + chartGrid
        : this.gatewayWorkspaceHtml()
        + this.laneNoteFoldHtml("account", "账号与额度说明", `<p class="wb-muted">额度与用量为只读查询：进入页面各读一次（不产生操作记录、不写上游状态）；读取失败保留上次真实值并标过期，未知一律显示「未知」，不折成 0。需要人工登录的动作会弹出真实浏览器窗口、由维护者本人完成登录，先二次确认再提交，绝不自动打开。</p>`)
        + this.opStatusHtml());
    }
    if (this.lane === 2) {
      // Contract 4.4 order: failure groups first (the lane's focus), the full
      // 8096 ledger window below them (main area), the single-op three-state
      // line last (secondary). Both views render the SAME opsRecent rows, so the
      // grouping can never disagree with the list it is drawn from.
      html = `<div class="wb-section-tabs"><button data-wb-lane="4">网关日志</button><button aria-pressed="true" data-wb-lane="2">操作记录</button></div>`
        + this.failuresLaneHtml()
        + this.opsListHtml();
    }
    if (this.lane === 3) {
      html = this.feeTablesHtml();
    }
    if (this.lane === 4) {
      html = `<div class="wb-section-tabs"><button aria-pressed="true" data-wb-lane="4">网关日志</button><button data-wb-lane="2">操作记录</button></div>` + this.logWorkspaceHtml();
    }
    if (this.lane === 6) {
      html = this.shareLaneHtml();
    }
    // R9-R40 task 5 (maintainer-requested): a thin top progress bar while a
    // read is in flight — summary aggregation or an open log detail read.
    // reduce-motion callers get a static translucent strip instead.
    const busy = this.summaryState === "loading" || Boolean(this.logDetail?.loading);
    const barHtml = busy
      ? `<div class="wb-loadbar${this.stage.classList.contains("reduce-motion") ? " wb-loadbar-static" : ""}" aria-hidden="true"></div>`
      : "";
    const content = this.root.querySelector<HTMLElement>(".wb-content")!;
    // L-C: the apply→readback repaint keeps the reading position (keepScroll
    // is set only by the auto-readback path, consumed exactly once).
    const savedScroll = this.keepScroll ? content.scrollTop : null;
    patchRollingPanel(content, barHtml + html, !this.stage.classList.contains("reduce-motion"));
    if (savedScroll !== null) { content.scrollTop = savedScroll; this.keepScroll = false; }
    this.installGatewayEvents();
    this.installFieldEvents();
    this.applyShareKeyEditFocus();
    this.rearmConfirm();
    this.rearmShareConfirm();
    this.reflashCopied();
    this.renderNotices();
    this.syncShareModal();
    this.restoreLogFocus();
  }
  /** 级别筛选的判定：2xx / 4xx / 5xx 按状态码族，其余（含空状态）归「其他」。
   *  空选择 = 全部通过，所以「全部」是真正的不过滤，而不是一个假选项。 */
  private inFeedLevel(status: string | null | undefined): boolean {
    if (!this.logFeedLevel) return true;
    const s = (status || "").toLowerCase();
    if (this.logFeedLevel === "other") return !(s.startsWith("2") || s.startsWith("4") || s.startsWith("5"));
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
    this.root.addEventListener("input", event => {
      const t = event.target;
      if (!(t instanceof HTMLInputElement)) return;
      if (t.matches("[data-model-q]")) {
        window.clearTimeout(this.modelQTimer);
        this.modelQTimer = window.setTimeout(() => {
          const value = t.value.trim().slice(0, 120);
          if (value === this.modelCatalogQ) return;
          this.modelCatalogQ = value;
          this.renderPanel();
          const fresh = this.root.querySelector<HTMLInputElement>("[data-model-q]");
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
          const fresh = this.root.querySelector<HTMLInputElement>("[data-wb-series-q]");
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
    this.root.addEventListener("change", event => {
      const t = event.target;
      if (t instanceof HTMLInputElement) {
        const set = t.matches("[data-model-row-check]") ? this.modelChecked : null;
        if (set) {
          if (t.checked) set.add(t.value);
          else set.delete(t.value);
          this.renderPanel();
          return;
        }
        const range = t.matches("[data-log-since]") ? "since" : t.matches("[data-log-until]") ? "until" : null;
        const d = this.logDetail;
        if (range && d) {
          d[range] = t.value;
          void this.fetchLogEntries(d, false);
        }
        if (t.matches("[data-wb-ms-field]")) this.msFieldInput(t);
        return;
      }
      if (!(t instanceof HTMLSelectElement)) return;
      if (t.matches("[data-wb-ms-field]")) { this.msFieldInput(t); return; }
      if (t.matches("[data-log-feed-src]")) { this.logFeedSrc = t.value; this.renderPanel(); return; }
      if (t.matches("[data-log-feed-model]")) { this.logFeedModel = t.value; this.renderPanel(); return; }
      if (t.matches("[data-log-feed-window]")) { this.logFeedWindow = t.value; this.renderPanel(); return; }
      if (t.matches("[data-log-feed-level]")) { this.logFeedLevel = t.value; this.renderPanel(); return; }
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
    this.root.addEventListener("wheel", event => {
      if (!(event.target as Element).closest?.(".wb-log-rows")) return;
      const d = this.logDetail;
      if (!d || event.deltaY >= 0 || !d.follow) return;
      d.follow = false;
      this.renderPanel();
    }, { passive: true });
  }
  /** Inline share-key edit: focus (and select a cap) once, right after it opens. */
  private applyShareKeyEditFocus() {
    const edit = this.shareKeyEdit;
    if (!edit || this.lane !== 6 || !edit.focusPending) return;
    const input = this.root.querySelector<HTMLInputElement>("[data-wb-share-edit]");
    if (!input) return;
    edit.focusPending = false;
    input.focus();
    if (edit.field === "cap") input.select();
  }
  private chainLabel(name: string): string {
    const map: Record<string, string> = { chat2api: "chat2api 主链", usage: "控制台宿主" };
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
    const key = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const from = state ? state.from : key(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 29));
    const to = state ? state.to : key(now);
    const fieldStyle = `font:inherit;font-size:var(--wb-fs-3);padding:3px var(--wb-sp-2);color:var(--theme-ink);background:var(--theme-field);border:1px solid var(--theme-line);border-radius:var(--wb-r-s)`;
    const form = `<div class="wb-feed-src">`
      + `<label><small>起始日</small><input type="date" data-wb-stats-from value="${escapeHtml(from)}" style="${fieldStyle}" aria-label="区间聚合起始日"/></label>`
      + `<label><small>结束日</small><input type="date" data-wb-stats-to value="${escapeHtml(to)}" style="${fieldStyle}" aria-label="区间聚合结束日"/></label>`
      + `<button type="button" class="wb-btn" data-wb-stats-run="1" title="向 GET /api/console/stats?from&to 发一次有界只读查询（不产生操作记录、不写任何状态）">查询区间聚合</button>`
      + (state ? `<button type="button" class="wb-btn wb-btn-sm" data-wb-stats-clear="1">清除结果</button>` : "")
      + `<small class="wb-muted" title="只读、有界；上限 ${STATS_RANGE_MAX_DAYS} 天（与后端 console_stats.py 同一口径）。只有显式提交才发一次请求。">只读 · 上限 ${STATS_RANGE_MAX_DAYS} 天 · 提交才查询</small>`
      + `</div>`;
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
  private statsRangeBodyHtml(state: { from: string; to: string; payload: StatsRangePayload }): string {
    const payload = state.payload;
    const totals = payload.totals ?? {};
    const num = (value: number | null | undefined, format: (v: number) => string): string =>
      typeof value === "number" && Number.isFinite(value) ? format(value) : "未知";
    const readings = `<div class="wb-stats-row">`
      + `<div><small>请求数</small><b>${num(totals.requests, fmtRequests)}</b></div>`
      + `<div><small>输入 token</small><b>${num(totals.input_tokens, fmtTokens)}</b></div>`
      + `<div><small>输出 token</small><b>${num(totals.output_tokens, fmtTokens)}</b></div>`
      + `<div><small>估算费用（¥）</small><b>${num(totals.estimated_cost_cny, fmtCny)}</b></div>`
      + `</div>`;
    const missing = payload.coverage?.missing_days ?? [];
    const unpriced = payload.coverage?.unpriced_models ?? [];
    const sourceCount = payload.coverage?.sources ? Object.keys(payload.coverage.sources).length : undefined;
    const coverage = `<p class="wb-muted">区间 ${escapeHtml(state.from)} → ${escapeHtml(state.to)}`
      + `${typeof payload.meta?.range_days === "number" ? ` · 后端口径 ${payload.meta.range_days} 天` : ""}`
      + `${typeof payload.meta?.timezone === "string" ? ` · 时区 ${escapeHtml(payload.meta.timezone)}` : ""}`
      + `${typeof payload.meta?.price_checked_at === "string" ? ` · 价目核对 ${escapeHtml(payload.meta.price_checked_at)}` : ""}`
      + `；覆盖来源 ${sourceCount === undefined ? "未提供" : `${sourceCount} 个`}`
      + `${missing.length ? ` · 区间内无记录日 ${missing.length} 天（${escapeHtml(missing.slice(0, 10).join("、"))}${missing.length > 10 ? "…" : ""}；缺日不是 0 费用）` : " · 区间内每日都有记录"}`
      + `${unpriced.length ? ` · 未定价模型：${escapeHtml(unpriced.join("、"))}（不折成 ¥0）` : ""}`
      + `；费用为账本可核实的估算值。</p>`;
    const days = payload.days ?? [];
    const shown = days.slice(-31);
    const rows = shown.map(day => `<tr><td>${escapeHtml(day.date)}</td>`
      + `<td class="wb-fee-num">${num(day.day_total?.requests, fmtRequests)}</td>`
      + `<td class="wb-fee-num">${num(day.day_total?.input_tokens, fmtTokens)}</td>`
      + `<td class="wb-fee-num">${num(day.day_total?.output_tokens, fmtTokens)}</td>`
      + `<td class="wb-fee-num">${num(day.day_total?.estimated_cost_cny, fmtCny)}</td>` + `</tr>`).join("");
    const table = `<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>日</th><th class="wb-fee-num">请求数</th><th class="wb-fee-num">输入 token</th><th class="wb-fee-num">输出 token</th><th class="wb-fee-num">估算费用（¥）</th></tr></thead><tbody>${rows || '<tr><td colspan="5">该区间账本没有可展示的日记录（缺日不补 0）。</td></tr>'}</tbody></table></div>`
      + (days.length > shown.length ? `<p class="wb-muted">区间共 ${days.length} 个有记录的日，此处只显示最后 ${shown.length} 天；上面合计覆盖整个区间。</p>` : "");
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
    const readings = `<div class="wb-cost">`
      + `<div><small>${kpiChip("layers", "tok")}Token (in+out)</small><div class="wb-large" data-kpi="tokens">${value("tokens")}</div>${this.kpiWindowHtml(this.tokenWindowPair(), fmtTokens, "14 天完整 Token 序列未读取或无记录")}</div>`
      + `<div class="wb-kpi-hero" data-wb-cost-state="${this.costReadingState()}"><small>${kpiChip("cost", "cost")}估算费用</small>${this.costCellHtml("div")}${this.kpiWindowHtml(this.windowPair("costCny"), fmtCny, "账本日不足 7 天或该分量未记录", (v, partial) => (partial ? `≥${fmtCny(v)}` : fmtCny(v)))}</div>`
      + `</div>`;
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
    const focus = readings
      + `<div class="wb-cost-foot"><p class="wb-muted wb-cost-note" data-wb-cost-note>${escapeHtml(this.summaryNote() + this.costResidentNote())}</p>${exportButton}</div>`
      + `<div class="wb-cost-top">`
      + `<div class="wb-cost-main">${kpiReady ? this.feeTrendHtml() : pendingCard}</div>`
      + `<div class="wb-cost-side">${this.credHtml()}${kpiReady ? this.periodCompareHtml() : ""}</div>`
      + `</div>`;
    if (!kpiValues || !this.chainFees) {
      return focus + this.statsRangeHtml() + this.usageFeedHtml();
    }
    // A12-3 ⑤: the per-gateway percentage column (and its .wb-cellbar
    // track) was removed; the absolute request / token / fee / credit
    // numbers stay, and a credit chain keeps saying 积分链.
    const gateRows = this.chainFees.map(c => {
      const amount = costAmount(c.costCny, c.costCnyPartial);
      return `<tr><td>${escapeHtml(this.chainLabel(c.name))}</td><td class="wb-fee-num">${fmtRequests(c.requests)}</td><td class="wb-fee-num">${fmtTokens(c.input)}</td><td class="wb-fee-num">${fmtTokens(c.output)}</td><td class="wb-fee-num"${costAmountBoundTitleAttr(amount)}>${fmtCostAmount(amount)}</td></tr>`;
    }).join("");
    const billed: ChainModel[] = [];

    for (const c of this.chainFees) {
      for (const m of c.models) billed.push(m);
    }
    billed.sort((a, b) => (b.costCny ?? 0) - (a.costCny ?? 0));
    // 模型明细的费用列：partial 行加 ≥（只含可折算记录），绝不写成确定总额；
    // 同一行的 title 由 costAmountBoundTitleAttr() 给（OD 决定稿「决定 3」）。
    const billedRow = (m: ChainModel) => {
      const amount = costAmount(m.costCny, m.costCnyPartial);
      return `<tr><td>${escapeHtml(m.model)}</td><td>${escapeHtml(this.chainLabel(m.chain))}</td><td class="wb-fee-num">${fmtRequests(m.requests)}</td><td class="wb-fee-num">${fmtTokens(m.input)}</td><td class="wb-fee-num">${fmtTokens(m.output)}</td><td class="wb-fee-num"${costAmountBoundTitleAttr(amount)}>${fmtCostAmount(amount)}</td></tr>`;
    };
    return focus
      + this.statsRangeHtml()
      + this.ledgerPairHtml()
      + `<section class="wb-block"><h3>各网关费用</h3><div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>网关</th><th class="wb-fee-num">请求数</th><th class="wb-fee-num">输入 token</th><th class="wb-fee-num">输出 token</th><th class="wb-fee-num">费用</th></tr></thead><tbody>${gateRows}</tbody></table></div></section>`
      + `<section class="wb-block"><h3>模型调用明细 · 计费链（¥）</h3><div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>模型</th><th>网关</th><th class="wb-fee-num">请求数</th><th class="wb-fee-num">输入 token</th><th class="wb-fee-num">输出 token</th><th class="wb-fee-num">费用（¥）</th></tr></thead><tbody>${billed.length ? billed.map(billedRow).join("") : '<tr><td colspan="6">账本暂无计费链模型记录。</td></tr>'}</tbody></table></div></section>`
      + `<section class="wb-block"><h3>价目表</h3>${this.priceTableHtml()}</section>`
      + this.usageFeedHtml();
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
      const why = this.summaryState === "loading" ? "费用聚合读取中"
        : this.summaryState === "unauthenticated" ? "费用服务拒绝读取（HTTP 401）"
          : this.summaryState === "forbidden" ? "管理权限被拒（HTTP 403）"
            : `本次读取失败：${this.summaryError || "原因未知"}`;
      const chart = this.kpi() && this.chainFees ? "图为上一次真实读取" : "趋势图不渲染";
      return ` · 缺失日与断线均不可得（${why}；${chart}） · ${priceLine}。`;
    }
    // 四种日级状态分开计数、互斥（维护者 2026-09-28 ②）；各自的定义在「口径与可信度」
    // 折叠里（credHtml 的「日级口径」行）——常驻条只写本次读数的事实。
    const cost = this.trendDays("costCny", 14);
    const missingDays = cost.values.filter((v, i) => v === null && !cost.unknown[i] && !cost.boundOnly[i]).length;
    const unknownCostDays = cost.unknown.filter(Boolean).length;
    const boundOnlyCostDays = cost.boundOnly.filter(Boolean).length;
    const partialCostDays = cost.partial.filter(Boolean).length;
    return ` · 近 14 天缺失日 ${missingDays} 天 · 分量未知 ${unknownCostDays} 天`
      + (boundOnlyCostDays ? ` · 下界日 ${boundOnlyCostDays} 天` : "")
      + (partialCostDays ? ` · 有值下界日 ${partialCostDays} 天` : "")
      + ` · ${priceLine}。`;
  }


  private credHtml(): string {
    const states: { key: Workbench["summaryState"]; text: string; cls: string }[] = [
      { key: "loading", text: "loading · 读取中", cls: "wb-badge-idle" },
      { key: "live", text: "live · 本次真实读取", cls: "wb-badge-ok" },
      { key: "stale", text: "stale · 刷新失败，保留上次真实值", cls: "wb-badge-warn" },
      { key: "unauthenticated", text: "unauthenticated · 401 无凭据", cls: "wb-badge-danger" },
      { key: "forbidden", text: "forbidden · 403 无权限", cls: "wb-badge-danger" },
      { key: "error", text: "error · 读取失败", cls: "wb-badge-danger" },
    ];
    // 维护者 2026-09-28（Plan-Revision 9 逐页收纳 · 费用页）：六态词表是**词典**
    // （长「loading/live/stale/401/403/error」条目），原先六条 chip 常驻首层。词表移进
    // 「口径与可信度」折叠，首层只显示**本次实际状态**（下面 head 的「当前态 <state>」）。
    // 当前态在词表里用 `aria-current="true"` 标出——不再往 chip 文本里加「（当前）」，
    // 11px 短标里 24 字已是上限，加后缀会把「本次实际状态」变成超长句。
    const chips = states.map(s => {
      const on = s.key === this.summaryState;
      return `<span class="wb-badge ${s.cls}"${on ? ' aria-current="true"' : ""}>${escapeHtml(s.text)}</span>`;
    }).join("");
    const captured = this.summaryEpoch
      ? `数据截至 ${new Date(this.summaryEpoch).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`
      : "尚无成功读取";
    const billed = (this.chainFees ?? []).filter(c => c.costCny !== undefined).length;

    return `<div class="wb-cred">`
      + `<div class="wb-cred-head"><h3>口径与可信度</h3><small>当前态 ${escapeHtml(this.summaryState)} · ${escapeHtml(captured)}</small></div>`
      // 维护者 2026-09-28（Plan-Revision 9 逐页收纳 · 费用页）：折的是**解释与词典**——
      // 五条口径整句与六态词表（长「loading/live/stale/401/403/error」条目）都进
      // 「口径与可信度」折叠区；`wb-cred` 作为区块仍常驻，`wb-cred-head` 的
      // 「当前态 <state> · 数据截至 <时点>」就是首层的**本次实际状态**。
      // 账本构成是「本次读到什么」的常驻事实，不进默认折叠：未读取到聚合时
      // 写「未知」而不是 0 条——折叠区里看不见的未知等于被当成 0 读。
      + `<div class="wb-cred-row" data-wb-cred-composition="1"><span class="wb-cred-k">当前账本构成</span><span class="wb-cred-v">${this.chainFees === undefined
        ? "chat2api 账本未知（本次聚合未读取到，不按 0 条显示）"
        : `chat2api 账本 ${billed} 条`}</span></div>`
      + `<details class="wb-secondary-details"${this.credDetailsOpen ? " open" : ""} data-wb-cred-details="1">`
      + `<summary title="口径与可信度（4 条口径行 · 六态词表 · 检查时点）· 点击展开 / 收起">口径与可信度${wbFoldMark()}</summary>`
      + `<div class="wb-cred-states">${chips}</div>`
      + `<div class="wb-cred-row"><span class="wb-cred-k">账本范围</span><span class="wb-cred-v">本页只读 chat2api 账本。分享网关的用量是另一本账，不并入本页；两本账不相加。</span></div>`
      + `<div class="wb-cred-row"><span class="wb-cred-k">账本构成口径</span><span class="wb-cred-v">按链名下的账本行统计；未读取到聚合时写「未知」，不按 0 条显示——「0 条」只代表读到的账本确实没有对应行，不代表账本为空。</span></div>`
      + `<div class="wb-cred-row"><span class="wb-cred-k">日级口径</span><span class="wb-cred-v">缺失日 = 近 14 天内账本没有该日记录（图上断线，不按 0 显示）；分量未知 = 当天有账本记录、但费用分量未知（同样断线，不按 0）；下界日 = 当天有记录、金额只含可折算记录且可折算为空（下界为空：图上断线，与链行同写「未知（下界仅含可折算部分）」）；有值下界日 = 只含可折算记录且有值（读数写 ≥¥… +「已知部分」，图上空心点 / 虚线段，区间合计与末值只写下界）。费用聚合不是 live 时这些计数都不可得，未知不按 0 天或「账本无记录」显示；stale 时趋势图是上一次真实读取的序列。</span></div>`
      + `<p class="wb-cred-note">六态为互斥的当前态：stale（刷新失败，保留上次真实值）与 error（读取失败、无上次值）是两回事，不合并；401 / 403 是权限结论，不是空账本。读取失败一律显示空值，不以模拟数据回填。</p>`
      + `</details>`
      + `</div>`;
  }
  private ledgerPairHtml(): string {
    const cost = this.displayedCost();
    const amount = cost === undefined ? "不可取得" : fmtCostAmount(cost);
    const requestCount = this.chainFees === undefined ? "不可取得" : fmtRequests(this.chainFees.reduce((sum, row) => sum + row.requests, 0));
    return '<div class="wb-ledger-pair"><section class="wb-ledger"><div class="wb-ledger-head"><strong>¥ 账 · chat2api</strong><span class="wb-ledger-scale">估算费用；未定价请求不折算为 ¥</span></div><div class="wb-ledger-row"><span>请求数</span><b>' + requestCount + '</b></div><div class="wb-ledger-row"><span>估算费用</span><b>' + amount + '</b></div></section></div>';
  }
  private priceTableHtml(): string {
    const p = this.priceTable;
    if (!p) return `<p class="wb-muted">价目表读取中或 8096 不可达（读取计价器自用价目文件）。</p>`;
    const n = (v: unknown): string => (typeof v === "number" && Number.isFinite(v) ? String(v) : "—");
    const head = `<p class="wb-muted">汇率 ¥/USD ${p.exchangeRate ?? "—"} · 价目核对 ${p.checkedAt ?? "—"}${p.checkedStale ? " · <b class=\"wb-stale-warn\">超过 30 天未核对，建议查官网更新</b>" : ""} · providers 单位 USD/百万 token；models 为 DeepSeek 峰谷档（offPeak / peak 子档）。本表与计价器消费的是同一份文件。</p>`;
    const deepRows = Object.entries(p.models ?? {}).map(([k, v]) => {
      const off = (v.offPeak ?? v) as Record<string, unknown>;
      const peak = (v.peak ?? {}) as Record<string, unknown>;
      return `<tr><td>${escapeHtml(k)}</td><td class="wb-fee-num">${n(off.cacheHit)}</td><td class="wb-fee-num">${n(off.cacheMiss)}</td><td class="wb-fee-num">${n(off.output)}</td><td class="wb-fee-num">${n(peak.cacheMiss)}</td><td class="wb-fee-num">${n(peak.output)}</td></tr>`;
    }).join("");
    const providerRows = Object.entries(p.providers ?? {}).flatMap(([prov, body]) => {
      const models = ((body as Record<string, unknown>).models ?? {}) as Record<string, Record<string, unknown>>;
      return Object.entries(models).map(([mk, mv]) =>
        `<tr><td>${escapeHtml(prov)} · ${escapeHtml(mk)}</td><td class="wb-fee-num">${n(mv.input)}</td><td class="wb-fee-num">${n(mv.cachedInput)}</td><td class="wb-fee-num">${n(mv.output)}</td><td>${escapeHtml(String(mv.billingMode ?? ""))}</td></tr>`);
    }).join("");
    return `${head}<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>DeepSeek 峰谷档</th><th class="wb-fee-num">闲时 cacheHit</th><th class="wb-fee-num">offPeak cacheMiss</th><th class="wb-fee-num">offPeak output</th><th class="wb-fee-num">peak cacheMiss</th><th class="wb-fee-num">peak output</th></tr></thead><tbody>${deepRows}</tbody></table><table class="wb-table"><thead><tr><th>Provider · 模型</th><th class="wb-fee-num">输入</th><th class="wb-fee-num">缓存输入</th><th class="wb-fee-num">输出</th><th>计价</th></tr></thead><tbody>${providerRows}</tbody></table></div>`;
  }
  // ---------------- log lane (R9-R30): gateway call logs + 7-day stats


  /** Log lane read-state line (contract 4.6, `wb-log-state`): the lane's first
   *  screen states what the read actually IS — how many rows the bounded tail
   *  returned, whether the source registry itself could be read, and which
   *  sources the SERVER declined to read. A failed registry read is named in
   *  words (never folded into an empty source list), and an unread source is
   *  never rendered as 「没有记录」. */
  private logLaneStateHtml(): string {
    // The lane states what the read IS, so an UNANSWERED read says 读取中…
    // instead of 「8096 不可达」/「无凭据会话」: the old first branch wrote
    // 8096 不可达 purely because the 30s poll had not reached its first
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
      return `<p class="wb-log-state" data-state="gated">调用日志读取失败（${escapeHtml(feed.reason ?? "原因未知")}）；不以空表冒充零记录。<button type="button" class="wb-btn wb-btn-sm" data-wb-log-feed-retry="1" title="重新读取chat2api 调用日志（有界尾读 · 只读，不产生操作行）" aria-label="重试读取调用日志">${wbIcon("refresh")}${wbIconHint("重试读取调用日志")}</button></p>`;
    }
    const unavailable = feed.unavailable ?? [];
    const parts = [
      `本次有界尾读读到 ${feed.rows.length} 条（每来源 ${LOG_FEED_PER_SOURCE} 条，上限 ${LOG_FEED_LIMIT} 条）`,
      this.logSourcesFailed ? `来源注册表读取失败（${this.logSourcesError || "原因未知"}），重试进入日志页或点「重试读取来源注册表」。`
        : this.logSources ? `来源注册表已读取 ${this.logSources.length} 个来源`
          : "来源注册表读取中…",
      unavailable.length
        ? `服务端声明未读取的来源：${unavailable.map(s => this.chainLabel(s)).join("、")}（不在调用日志账本内，不以空表冒充零记录）`
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
      if (!navigator.clipboard?.writeText) throw new Error("clipboard_unavailable");
      await navigator.clipboard.writeText(message);
      this.flashCopied(button);
    } catch { button.textContent = "复制失败，请展开原文后手动复制"; }
  }
  private logOverviewEvents() {
    return (this.logSources ?? []).flatMap(source => {
      const snapshot = this.logOverview.get(source.source);
      if (snapshot?.status !== "ok") return [];
      const identities = logRowIdentities(source.source, snapshot.rows);
      return snapshot.rows.map((row, i) => ({ row, source, kind: logKind(row), identity: identities[i]! }));
    }).sort((a, b) => (b.row.ts ?? "").localeCompare(a.row.ts ?? ""));
  }
  /** 分类筛选（分类 chips）是总览表与详情表共用的唯一分类入口；这里只做显示
   *  筛选，不静默丢弃：返回当前筛选下要列出的行。 */
  private logKindFilteredRows(rows: LogEntryRow[]): LogEntryRow[] {
    return this.logOverviewFilter === "all" ? rows : rows.filter(row => logKind(row) === this.logOverviewFilter);
  }
  /** 当前分类筛选的中文读数，供空表 / 计数句点名实际筛选轴（不是别的筛选）。 */
  private logKindFilterLabel(): string {
    return this.logOverviewFilter === "all" ? "全部分类" : logKindLabels[this.logOverviewFilter];
  }
  private logFiltersHtml(rows: LogEntryRow[], read: boolean): string {
    const kinds = Object.keys(logKindLabels) as LogKind[];
    return `<div class="wb-log-overview-filters"><button class="wb-btn wb-btn-sm" data-wb-log-overview-filter="all" aria-pressed="${this.logOverviewFilter === "all"}">全部 ${read ? rows.length : "—"}</button>${kinds.map(kind => `<button class="wb-btn wb-btn-sm wb-log-kind-${kind}" data-wb-log-overview-filter="${kind}" aria-pressed="${this.logOverviewFilter === kind}">${logKindLabels[kind]} ${read ? rows.filter(row => logKind(row) === kind).length : "—"}</button>`).join("")}</div>`;
  }
  private logWorkspaceHtml(): string {
    const right = this.logDetailOpen && this.logDetail ? this.logDetailHtml() : this.logLaneHtml();
    const sources = this.logSources ?? [];
    const ids = sources.map(source => source.source);
    if (sources.length && !this.logOrderLoaded) {
      this.logOrderLoaded = true;
      try {
        this.logOrderPrefs = new GatewayOrderPreferences({ storage: localStorage, namespace: `${GW_ORDER_NAMESPACE}:runtime-logs` });
        const read = this.logOrderPrefs.read(ids); this.logSourceOrder = read.order;
        if (read.status === "coordinated") this.logOrderNote = "来源顺序已在本浏览器记住。";
        else if (read.status !== "default") this.logOrderNote = "此前保存顺序未能采用，当前使用默认顺序；再次排序会尝试保存。";
      }
      catch { this.logOrderNote = "存储不可用，顺序只在本页有效。"; }
    }
    this.logSourceOrder = this.logSourceOrder.filter(id => ids.includes(id));
    for (const id of ids) if (!this.logSourceOrder.includes(id)) this.logSourceOrder.push(id);
    const selected = this.logDetailOpen ? this.logDetail?.source ?? this.logPending?.source : undefined;
    const visible = this.logSourceOrder.flatMap(id => sources.filter(source => source.source === id && `${source.display} ${id}`.toLowerCase().includes(this.logSourceQuery.toLowerCase())));
    const rows = visible.map(source => {
      const snap = this.logOverview.get(source.source);
      const status = !source.categories.length ? "未配置运行日志" : !snap ? "未读取" : snap.status === "ok" ? `${snap.rows.length} 条 · 本次窗口` : `读取状态：${snap.status}`;
      return `<li class="wb-gw-row${selected === source.source ? " wb-gw-row-on" : ""}" data-wb-log-source-row="${escapeHtml(source.source)}"><button class="wb-md-item wb-gw-item" data-wb-log-source="${escapeHtml(source.source)}" aria-pressed="${selected === source.source}"><span class="wb-md-name">${escapeHtml(source.display)}</span><span class="wb-md-state">${escapeHtml(status)}</span></button><button class="wb-gw-handle" data-wb-log-handle="${escapeHtml(source.source)}" aria-label="拖动 ${escapeHtml(source.display)} 调整顺序" title="拖动排序；条目上可按 Alt+↑ / Alt+↓"><i></i><i></i><i></i></button></li>`;
    }).join("");
    return `<div class="wb-gw wb-log-workspace"><aside class="wb-gw-side wb-char" aria-label="运行日志来源"><button class="wb-md-item wb-gw-all" data-wb-log-source="" aria-pressed="${!selected}">${wbIcon("logs")}<span class="wb-md-name">全部运行日志</span><span class="wb-md-state">各来源本次读取窗口</span></button><div class="wb-gw-filter"><input type="search" class="wb-gw-q" data-wb-log-source-q aria-label="搜索日志来源" placeholder="搜索网关或后台服务" value="${escapeHtml(this.logSourceQuery)}"/></div><ol class="wb-gw-list wb-log-source-list" aria-label="可排序日志来源">${rows || `<li>没有符合条件的来源</li>`}</ol><p class="wb-gw-hint" aria-live="polite">${escapeHtml(this.logOrderNote)}</p></aside><div class="wb-gw-main">${right}</div></div>`;
  }
  /** 来源顺序的一次完整写回：拒绝时页面顺序仍为准，并写清是哪一种拒绝
   *  （超上限 / 写入被拒），与网关顺序的同一句法（persistGatewayOrder）。 */
  private saveLogOrder(visible: string[]) {
    this.logSourceOrder = reorderGatewaySubset(this.logSourceOrder, visible);
    const prefs = this.logOrderPrefs;
    if (!prefs) { this.logOrderNote = "顺序只在本页有效：本浏览器未记住来源顺序（无法访问本浏览器存储）。"; return; }
    try {
      const saved = prefs.save(this.logSourceOrder, (this.logSources ?? []).map(source => source.source));
      this.logOrderNote = saved.persisted ? "来源顺序已在本浏览器记住。"
        : `本浏览器未记住来源顺序（${saved.reason === "sequence_too_long" ? "顺序超过上限" : "写入被拒绝"}）；本次顺序只在本页有效。`;
    }
    catch { this.logOrderNote = "存储写入失败，顺序只在本页有效。"; }
  }
  private logLaneHtml() {
    if (!this.logSourcesRequested && !this.logSourcesFailed) {
      this.logSourcesRequested = true;
      void this.fetchLogSources().then(() => { this.renderPanel(); void this.fetchLogOverview(); });
    }
    // The registry buttons and the 读取中… placeholder come from the registry
    // read's OWN state, so an unanswered read no longer hides the entry row.
    // 失败态：失败句必须给出实测原因，并留一个可点的只读重试入口——原先
    // `!response.ok` 只清空列表且永不重试，页面永远停在「读取中…」。
    const logOpenHtml = `<div class="wb-log-open-row">${(this.logSources ?? []).map(r =>
      `<button type="button" class="wb-btn wb-btn-sm" data-log-open="${escapeHtml(r.source)}" title="查看 ${escapeHtml(r.display)} 的运行日志详情（服务端脱敏、有限尾读）">${wbIcon("logs")}${escapeHtml(r.display)}</button>`).join("")}${this.logSourcesFailed ? `<span class="wb-muted">来源注册表读取失败（${escapeHtml(this.logSourcesError || "原因未知")}），重试进入日志页。</span><button type="button" class="wb-btn wb-btn-sm" data-wb-log-sources-retry="1" title="重新读取来源注册表（只读，不产生操作记录）" aria-label="重试读取来源注册表">${wbIcon("refresh")}${wbIconHint("重试读取来源注册表")}</button>` : (this.logSources ? "" : `<span class="wb-muted">来源注册表读取中…</span>`)}</div>`;
    const events = this.logOverviewEvents();
    const filtered = events.filter(event => this.logOverviewFilter === "all" || event.kind === this.logOverviewFilter);
    const hasRead = (this.logSources ?? []).some(source => this.logOverview.get(source.source)?.status === "ok");
    const filters = this.logFiltersHtml(events.map(event => event.row), hasRead);
    // C4：本次渲染的行按身份入表，复制点击只从这里取原文；下标只用于
    // 表格位置，不再进入按钮（后台刷新换窗口时下标会漂移）。
    const rendered = filtered.slice(0, 200);
    this.logOverviewRenderRows = new Map(rendered.map(event => [event.identity, event.row]));
    const rows = rendered.map(({ row, source, kind, identity }) => `<tr class="wb-log-kind-${kind}"><td><time>${escapeHtml(row.ts ?? "时间未知")}</time></td><td><button class="wb-btn wb-btn-sm" data-log-open="${escapeHtml(source.source)}">${escapeHtml(source.display)}</button></td><td><span class="wb-log-kind">${logKindLabels[kind]}</span></td><td>${this.logRowSummaryHtml(row) || escapeHtml(row.message.split("\n")[0] ?? "")}<div class="wb-log-raw"><button class="wb-btn wb-btn-sm" data-wb-log-raw-toggle="${escapeHtml(identity)}" aria-expanded="${this.logRawOpen.has(identity)}">${this.logRawOpen.has(identity) ? "收起原始内容" : "展开原始内容"}</button>${this.logRawOpen.has(identity) ? `<pre class="wb-log-msg open">${escapeHtml(row.message)}</pre>` : ""}<button class="wb-btn wb-btn-sm" data-wb-log-overview-copy="${escapeHtml(identity)}">复制完整原文</button></div></td></tr>`).join("");
    const coverage = (this.logSources ?? []).map(source => {
      const snapshot = this.logOverview.get(source.source);
      return `${escapeHtml(source.display)}：${!source.categories.length ? "未配置运行日志" : snapshot ? snapshot.status === "ok" ? `${snapshot.rows.length} 条${snapshot.truncated ? "（尾读窗口）" : ""}` : escapeHtml(snapshot.status) : "未读取"}`;
    }).join(" · ");
    return `<section class="wb-block wb-log-sources"><div class="wb-section-head"><h3>运行日志总览</h3><button class="wb-btn wb-btn-sm" data-wb-log-overview-refresh ${this.logOverviewLoading ? "disabled" : ""}>${this.logOverviewLoading ? "读取中…" : "刷新总览"}</button></div>${filters}`
      + `<p class="wb-muted">各来源本次读取窗口内的事件，非累计次数；表格显示当前筛选最新 200 条。${this.logOverviewCheckedAt ? `检查于 ${escapeHtml(this.logOverviewCheckedAt)}。` : ""}通过仅依据明确成功结果；错误 / 异常可用于检查 bug。</p>`
      + `<div class="wb-table-scroll wb-log-overview-table"><table class="wb-table"><thead><tr><th>时间</th><th>网关</th><th>分类</th><th>事件</th></tr></thead><tbody>${rows || `<tr><td colspan="4">${this.logOverviewLoading ? "正在读取各来源…" : events.length ? `没有符合分类筛选「${escapeHtml(this.logKindFilterLabel())}」的事件；点上方「全部」可看回全部` : "暂无已读事件；来源状态见下方"}</td></tr>`}</tbody></table></div>`
      + `<p class="wb-muted">${coverage}</p>${this.logSourcesFailed || !this.logSources ? logOpenHtml : ""}<p class="wb-muted">原始日志按事件展开和复制；调用用量在「费用」页，控制台动作在「操作记录」页。</p></section>`;
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
    const note = `<p class="wb-muted">调用日志与统计来自 chat2api 账本，操作记录来自控制台操作账本；未读取与零用量分别显示。</p>`;
    // 近 7 天统计：每个读数缺字段一律「未知」，绝不 `?? 0` 冒真零；逐日
    // 序列里有一天缺字段就不画 spark（缺字段画成 0 会把「没读到」读成
    // 「那天确实是 0 请求」），并把缺字段的天数写出来。读取中 / 读取失败 /
    // 尚未读取三态分开，失败带实测 HTTP 状态。
    const numOrUnknown = (value: number | null | undefined, format: (v: number) => string): string =>
      typeof value === "number" && Number.isFinite(value) ? format(value) : "未知";
    const weekRequests = (this.statsWeek?.days ?? []).map(d => d.day_total?.requests);
    const weekGapDays = weekRequests.filter(v => typeof v !== "number" || !Number.isFinite(v)).length;
    // 刷新失败且上一次真实读数仍在时，同一屏里必须写出「本次刷新失败」并标陈旧
    // （契约六态 stale）：只写失败原因、数字却一字不变，读起来像本次读取成功。
    const statsHtml = this.statsWeek
      ? (this.statsWeekError
        ? `<p class="wb-muted">近 7 天统计刷新失败（${escapeHtml(this.statsWeekError)}）；以下为上一次真实读数，已标陈旧（本次刷新失败），不以 0 或空表冒充。</p>`
        : "")
        + `<div class="wb-stats-row"><div><small>请求</small><b>${numOrUnknown(this.statsWeek.totals.requests, fmtRequests)}</b>${weekGapDays ? "" : this.sparkSvg(weekRequests as number[])}${this.weekDeltaHtml()}</div><div><small>输入 token</small><b>${numOrUnknown(this.statsWeek.totals.input_tokens, fmtTokens)}</b></div><div><small>输出 token</small><b>${numOrUnknown(this.statsWeek.totals.output_tokens, fmtTokens)}</b></div><div><small>估算费用</small><b>${numOrUnknown(this.statsWeek.totals.estimated_cost_cny, fmtCny)}</b></div></div>`
        + (weekGapDays ? `<p class="wb-muted">近 7 天逐日请求里有 ${weekGapDays} 天未提供该字段（按「未知」处理，不补 0，因此不画逐日柱）。</p>` : "")
      : this.statsWeekError
        ? `<p class="wb-muted">近 7 天统计读取失败（${escapeHtml(this.statsWeekError)}）；本次没有可保留的真实读数，不以 0 或空表冒充。</p>`
        : `<p class="wb-muted">近 7 天统计尚未读取（/api/console/stats 近 7 天口径）；进入日志页立即读取一次，读取失败如实写失败，不以 0 或空表冒充。</p>`;
    const feed = this.logsFeed;
    // F4b: 服务端可能把同一账本按两个 id 各出一份行（国际链注册了
    // 同一记录不重复出现；被折叠的拼写在说明里点名。
    const visible = this.feedVisibleRows();
    const feedSrcs = feed && feed.state === "live"
      ? [...new Set(visible.rows.map(e => e.source))].sort() : [];
    if (this.logFeedSrc && !feedSrcs.includes(this.logFeedSrc)) {
      this.logFeedSrc = ""; // vanished source falls back to 全部
    }
    const feedModels = feed && feed.state === "live"
      ? [...new Set(visible.rows.map(e => e.model).filter(Boolean))].sort() : [];
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
    const unparsable = windowMs === undefined
      ? 0 : visible.rows.filter(e => !Number.isFinite(Date.parse(e.time))).length;
    const shownRows = feed && feed.state === "live"
      ? visible.rows.filter(e => (!this.logFeedSrc || e.source === this.logFeedSrc)
        && (!this.logFeedModel || e.model === this.logFeedModel)
        && inWindow(e)
        && this.inFeedLevel(e.status)) : [];
    // 级别（状态）筛选：与来源/模型/时段同样只读同一批有界尾读行，不另发请求；
    // 不匹配的行不静默消失——被它挡掉多少条在下方如实写出。
    const levelFiltered = feed && feed.state === "live"
      ? visible.rows.filter(e => (!this.logFeedSrc || e.source === this.logFeedSrc)
        && (!this.logFeedModel || e.model === this.logFeedModel)
        && inWindow(e) && !this.inFeedLevel(e.status)).length
      : 0;
    // Plan-Revision 9 ⑤（维护者 2026-09-28）：每个工作区都必须有节标题（`.wb-block > h3`）。
    // 调用日志在读取中/读取失败时此前整块不渲染（feedHtml 为空串），该车道因此没有节标题
    // （门禁 ⑤ 实测：1600x900/light/live lane 4「sectionTitle 没有量到元素」）。表体仍然只在
    // 有界尾读结果到位后渲染，但节标题与读取状态行（上一行的 `.wb-log-state`）在三种状态下都在。
    const feedHtml = !feed || feed.state !== "live"
      ? `<section class="wb-block wb-char" data-wb-anchor="usage-feed"><h3>各网关调用日志</h3>${this.logLaneStateHtml()}${this.laneNoteFoldHtml("log-table", "表格口径与来源说明", note)}</section>`
      : (() => {
        const rows = shownRows.slice(0, 60).map(e =>
          `<tr><td>${escapeHtml((e.time || "").slice(5, 19).replace("T", " "))}</td><td>${escapeHtml(e.source)}</td><td>${escapeHtml(e.model || "—")}</td><td>${this.statusDotHtml(e.status)}${escapeHtml(e.status || "—")}</td><td class="wb-fee-num">${this.numText(e.input_tokens)}</td><td class="wb-fee-num">${this.numText(e.output_tokens)}</td><td class="wb-fee-num">${this.durationText(e.duration_ms)}</td><td class="wb-fee-num">${this.logRateText(e.output_tokens, e.duration_ms)}</td></tr>`).join("");
        const srcSel = feedSrcs.length > 1
          ? `<label class="wb-feed-src"><small>网关</small><select data-log-feed-src aria-label="调用日志网关筛选"><option value="">全部</option>${feedSrcs.map(s => `<option value="${escapeHtml(s)}"${this.logFeedSrc === s ? " selected" : ""}>${escapeHtml(this.chainLabel(s))}</option>`).join("")}</select></label>`
          : "";
        const modelSel = feedModels.length > 1
          ? `<label class="wb-feed-src"><small>模型</small><select data-log-feed-model aria-label="调用日志模型筛选"><option value="">全部</option>${feedModels.map(m => `<option value="${escapeHtml(m)}"${this.logFeedModel === m ? " selected" : ""}>${escapeHtml(m)}</option>`).join("")}</select></label>`
          : "";
        const winSel = `<label class="wb-feed-src"><small>时段</small><select data-log-feed-window aria-label="调用日志时段筛选"><option value=""${this.logFeedWindow === "" ? " selected" : ""}>全部</option><option value="1h"${this.logFeedWindow === "1h" ? " selected" : ""}>近 1 小时</option><option value="6h"${this.logFeedWindow === "6h" ? " selected" : ""}>近 6 小时</option><option value="24h"${this.logFeedWindow === "24h" ? " selected" : ""}>近 24 小时</option></select></label>`;
        // 级别筛选与日志详情页的级别过滤同一口径（状态码族），只是作用在这批
        // 有界尾读行上；「其他」= 既不是 2xx/4xx/5xx 的状态词，也如实可选。
        const levelSel = `<label class="wb-feed-src"><small>级别</small><select data-log-feed-level aria-label="调用日志级别筛选"><option value=""${this.logFeedLevel === "" ? " selected" : ""}>全部</option><option value="2xx"${this.logFeedLevel === "2xx" ? " selected" : ""}>2xx 成功</option><option value="4xx"${this.logFeedLevel === "4xx" ? " selected" : ""}>4xx 客户端</option><option value="5xx"${this.logFeedLevel === "5xx" ? " selected" : ""}>5xx 服务端</option><option value="other"${this.logFeedLevel === "other" ? " selected" : ""}>其他状态</option></select></label>`;
        const rateNote = `<p class="wb-muted">整次平均输出 Token/s 列按每条记录自身的输出 Token ÷ 整次耗时（含首字等待）计算；输出 Token 或耗时未采集（后端以 null 披露）显示「未采集」，耗时非正显示「—」，都不按 0 显示。</p>`;
        const winNote = windowMs === undefined
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
        return `<section class="wb-block wb-char" data-wb-anchor="usage-feed"><h3>各网关调用日志（最近 ${Math.min(shownRows.length, 60)} 条${this.logFeedSrc ? ` · 仅 ${escapeHtml(this.chainLabel(this.logFeedSrc))}` : " · 全源最新在前"}${this.logFeedModel ? ` · 仅模型 ${escapeHtml(this.logFeedModel)}` : ""}${windowMs === undefined ? "" : ` · ${escapeHtml(this.logFeedWindow)}`}${this.logFeedLevel ? ` · 仅 ${escapeHtml(this.logFeedLevel)}` : ""}）</h3>${this.logLaneStateHtml()}<div class="wb-log-filters">${srcSel}${modelSel}${winSel}${levelSel}</div>${winNote}${levelNote}<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>时间（账本原值）</th><th>网关</th><th>模型</th><th>状态</th><th class="wb-fee-num">输入</th><th class="wb-fee-num">输出</th><th class="wb-fee-num">耗时</th><th class="wb-fee-num">整次平均输出 Token/s</th></tr></thead><tbody>${rows || '<tr><td colspan="9">账本暂无匹配的调用记录。</td></tr>'}</tbody></table></div>${this.laneNoteFoldHtml("log-table", "表格口径与来源说明", rateNote + dedupeNote + note)}</section>`;
      })();
    return feedHtml
      + `<section class="wb-block"><h3>近 7 天统计</h3>${statsHtml}</section>`;
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
    const row = (this.logSources ?? []).find(r => r.source === source);
    const cat = category || row?.categories[0]?.category || "";
    const hasCats = Boolean(row && row.categories.length);
    // No category: a failed registry read and a source without runtime logs are
    // different facts (the first can be retried), never one 「读取失败」.
    const registryFailed = !row && this.logSourcesFailed;
    this.logDetail = {
      source, category: cat, q: "", since: "", until: "",
      follow: true, newCount: 0, rows: [], nextCursor: null,
      status: hasCats ? "loading" : registryFailed ? "registry" : "unconfigured",
      errorCode: hasCats ? "" : registryFailed ? this.logSourcesError : "not_configured", scannedBytes: 0,
      truncated: false, resetHint: false, stale: false,
      loading: hasCats, requested: true, expanded: new Set(), wrap: true,
      rotation: 0, requestId: "",
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
    const src = (this.logSources ?? []).find(r => r.source === detail.source);
    const cats = src?.categories ?? [];
    const tabs = cats.map(c =>
      `<button type="button" class="wb-tab${c.category === detail.category ? " wb-tab-on" : ""}" data-wb-log-cat="${escapeHtml(c.category)}" data-wb-log-src="${escapeHtml(detail.source)}">${escapeHtml(c.display)}</button>`).join("");
    const stateHtml = this.logDetailStatusHtml(detail);
    // R9-R40 task 6: rotation selector for rotating categories only — the
    // whitelist index maps 1..N to fixed registry generations; the server
    // answers invalid_rotation beyond the cap and falls back for missing
    // generations, so the dropdown only offers what the registry declares.
    const activeCat = cats.find(c => c.category === detail.category);
    const genOptions = activeCat?.rotating
      ? Array.from({ length: Math.min(activeCat.rotation_index ?? 0, 5) }, (_, i) => i + 1)
          .map(n => `<option value="${n}"${detail.rotation === n ? " selected" : ""}>.${n}（更早）</option>`).join("")
      : "";
    const genHtml = activeCat?.rotating && (activeCat.rotation_index ?? 0) > 0
      ? `<div class="wb-log-gen-row"><label class="wb-log-date"><small>轮转代次</small><select data-log-gen aria-label="轮转代次"><option value="0"${detail.rotation === 0 ? " selected" : ""}>活动文件</option>${genOptions}</select></label>${detail.rotation > 0 ? `<span class="wb-muted">正在查看轮转代次 .${detail.rotation}</span>` : ""}</div>`
      : "";
    const filterHtml = `<div class="wb-log-filter">
        <input type="text" class="wb-log-q" data-log-q placeholder="关键词（字面量）" value="${escapeHtml(detail.q)}" autocomplete="off" spellcheck="false" aria-label="日志关键词"/>
        <label class="wb-log-date"><small>自</small><input type="date" data-log-since value="${escapeHtml(detail.since)}"/></label>
        <label class="wb-log-date"><small>至</small><input type="date" data-log-until value="${escapeHtml(detail.until)}"/></label>
        <button type="button" class="wb-log-clear" data-log-clear>清除筛选</button>
      </div>`;
    return `<section class="wb-log-detail">
      <div class="wb-log-head"><button type="button" class="wb-btn" data-log-back>← 返回${this.logReturnLane !== null ? escapeHtml(names[this.logReturnLane] ?? "") : "日志列表"}</button>
        <b data-log-title tabindex="-1">${escapeHtml(src?.display ?? detail.source)}</b>
        <span class="wb-muted">· ${escapeHtml(cats.find(c => c.category === detail.category)?.display || detail.category || (detail.status === "registry" ? "类别未知" : "无运行日志类别"))}</span>
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
      else if (shown === detail.rows.length) bits.push(`最近读取 ${escapeHtml(detail.fetchedAt ?? "")} · 本次窗口 ${detail.rows.length} 条`);
      else bits.push(`最近读取 ${escapeHtml(detail.fetchedAt ?? "")} · 分类筛选「${escapeHtml(this.logKindFilterLabel())}」显示 ${shown} / ${detail.rows.length} 条（筛除 ${detail.rows.length - shown} 条）`);
    } else bits.push(map[detail.status] ?? "读取失败");
    if (detail.status === "ok" && !detail.follow && !detail.loading) {
      const label = detail.newCount ? `暂停跟随 · 有 ${detail.newCount} 条新日志 · 点击回到最新` : "跟随已暂停 · 点击回到最新";
      bits.push(`<button type="button" class="wb-btn wb-log-resume" data-log-resume>${escapeHtml(label)}</button>`);
    }
    if (detail.errorCode && detail.status !== "ok") bits.push(`（code：${escapeHtml(detail.errorCode)}）`);
    if (detail.requestId) {
      // The chip states what the backend confirmed, not what was asked for.
      const what = detail.requestEcho === "applied"
        ? `仅显示请求 <code>${escapeHtml(detail.requestId)}</code> 在本读取窗口内的相关记录`
        : detail.requestEcho === "unconfirmed"
          ? `已请求只看 <code>${escapeHtml(detail.requestId)}</code>，但后端未确认按该请求筛选：下面是未筛选的读取窗口`
          : `正在按请求 <code>${escapeHtml(detail.requestId)}</code> 筛选…`;
      bits.push(`<span class="wb-log-reqf">${what} <button type="button" class="wb-btn wb-btn-sm" data-log-req="">清除</button></span>`);
    }
    if (detail.status === "registry") bits.push(`<button type="button" class="wb-btn wb-btn-sm" data-log-reopen="${escapeHtml(detail.source)}">重试读取来源注册表</button>`);
    if (detail.truncated) bits.push(`仅覆盖最近 ${detail.scannedBytes.toLocaleString("zh-CN")} 字节窗口`);
    if (detail.resetHint) bits.push(`<b class="wb-stale-warn">日志已轮转，游标已重置</b>`);
    if (detail.stale) bits.push(`<b class="wb-stale-warn">刷新失败，内容可能过时</b>`);
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
      [/^stream end\b/, "流式输出结束"], [/^request summary\b/, "请求小结"], [/^output budget\b/, "输出上限决定"],
      [/^upstream slow\b/, "上游响应慢"], [/^request failed\b/, "请求失败"],
    ];
    const kind = kinds.find(([re]) => re.test(row.message))?.[1];
    const outcome = e.outcome === "ok" ? ["成功", "ok"] : e.outcome === "error" ? ["失败", "danger"] : e.outcome ? [e.outcome, "idle"] : null;
    const bits: string[] = [];
    if (e.model) bits.push(`模型 ${escapeHtml(e.model)}`);
    if (e.phase) bits.push(`阶段 ${escapeHtml(e.phase)}`);
    if (typeof e.duration_ms === "number" && e.duration_ms > 0) bits.push(`耗时 ${(e.duration_ms / 1000).toFixed(e.duration_ms >= 10000 ? 0 : 1)} 秒`);
    if (e.error_code) bits.push(`错误 ${escapeHtml(e.error_code)}`);
    if (!kind && !outcome && !bits.length) return "";
    return `<p class="wb-log-sum">${kind ? `<b>${escapeHtml(kind)}</b>` : ""}${outcome ? `<span class="wb-badge wb-badge-${outcome[1]}">${escapeHtml(outcome[0]!)}</span>` : ""}${bits.map(b => `<span>${b}</span>`).join("")}</p>`;
  }
  private logDetailRowsHtml(detail: LogDetailState): string {
    const q = detail.q;
    // C4：身份在整段 detail.rows 上计算（与分类筛选无关），展开 / 复制都按它；
    // 行下标只用于表格位置，不进入按钮值。
    const identities = logRowIdentities(detail.source, detail.rows);
    const rendered = detail.rows
      .map((row, idx) => ({ row, identity: identities[idx]! }))
      .filter(({ row }) => this.logOverviewFilter === "all" || logKind(row) === this.logOverviewFilter);
    this.logDetailRenderRows = new Map(rendered.map(({ row, identity }) => [identity, row]));
    const rows = rendered.map(({ row, identity }) => {
      const open = detail.expanded.has(identity);
      const parts = row.message.split("\n");
      // F12-6: the first reading of a row is the Chinese summary of the event the
      // backend parsed; the raw line is folded and copyable. A row the backend
      // could not parse shows its raw first line instead of a summary.
      const summary = this.logRowSummaryHtml(row);
      const shown = open ? row.message : summary ? "" : parts[0] ?? "";
      // 查看该请求相关记录: only from the backend's own request_id (never a
      // text match on the line), and not on the request already filtered to.
      const rid = typeof row.event?.request_id === "string" ? row.event.request_id : "";
      const reqChip = rid && rid !== detail.requestId
        ? `<button type="button" class="wb-btn wb-btn-sm wb-log-req" data-log-req="${escapeHtml(rid)}" title="只看该请求在本读取窗口内的相关记录（按服务端 request_id 精确筛选）">查看该请求相关记录</button>`
        : "";
      const toggle = summary || parts.length > 1
        ? `<button type="button" class="wb-log-more" ${open ? `data-log-collapse="${escapeHtml(identity)}"` : `data-log-expand="${escapeHtml(identity)}"`}>${open ? `${wbIcon("detail")}收起原文` : `${wbIcon(row.level === "ERROR" ? "alert" : "detail")}${parts.length > 1 ? `展开原文（${parts.length} 行，全文不截断）` : "展开原文"}`}</button>`
        : "";
      // 维护者 2026-09-28（Plan-Revision 9 逐页收纳 · 日志 / 操作页）：长错误 / 详细 JSON
      // 在**单行详情**里展开（上面的 toggle 与 <pre> 都是同一行的），完整值要能**一键复制**：
      // 复制按钮取的是这一行的完整原文（不是首层那个折叠前缀），反馈沿用既有的
      // 「已复制」一次性闪动（flashCopied），所以长错误既能读全，也能拿走。
      const copy = `<button type="button" class="wb-btn wb-btn-sm wb-log-copy" data-wb-log-copy="${escapeHtml(identity)}" title="复制该行完整原文（不截断）" aria-label="复制该行完整原文">${wbIcon("copy")}复制</button>`;
      return `<tr class="wb-log-kind-${logKind(row)}">
        <td><time>${escapeHtml(row.ts ?? "时间未知")}</time></td>
        <td><span class="wb-log-kind">${logKindLabels[logKind(row)]} · ${escapeHtml(row.level || "级别未知")}</span></td>
        <td>${summary}${shown ? `<pre class="wb-log-msg${open ? " open" : ""}${detail.wrap ? "" : " nowrap"}">${q ? this.logHighlightHtml(shown, q) : escapeHtml(shown)}</pre>` : ""}${reqChip}${toggle}</td>
        <td>${copy}</td>
      </tr>`;
    }).join("");
    const olderBtn = detail.nextCursor
      ? `<button type="button" class="wb-btn" data-log-older>加载更早记录</button>`
      : (detail.resetHint ? "" : `<span class="wb-muted">没有更早的窗口记录</span>`);
    // C4：空表句点名实际筛选轴（分类 chips）与生效值；关键词 / 日期范围在
    // 服务端就已生效，这里如实区分，不把空窗口写成「没有事件」。
    const emptyText = detail.status === "ok"
      ? (this.logOverviewFilter === "all"
        ? "本次窗口没有事件；关键词 / 日期范围在服务端筛选，可清除后重试"
        : `本次窗口没有符合分类筛选「${escapeHtml(this.logKindFilterLabel())}」的事件；点上方「全部」可看回全部`)
      : "日志尚未读到；状态与覆盖范围见上方";
    return `<div class="wb-log-rows wb-table-scroll wb-log-event-table"><table class="wb-table"><thead><tr><th>时间</th><th>分类</th><th>事件与原文</th><th>操作</th></tr></thead><tbody>${rows || `<tr><td colspan="4">${emptyText}</td></tr>`}</tbody></table></div>${olderBtn ? `<div class="wb-log-older">${olderBtn}</div>` : ""}`;
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
      out += escapeHtml(text.slice(at, found))
        + "<mark>" + escapeHtml(text.slice(found, found + q.length)) + "</mark>";
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
   *  维护者 2026-09-28：本车道**不再**用 `!sessionLive()` 早退并断言「8096
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
    const tabs = `<div class="wb-section-tabs" aria-label="模型管理分区">${([["catalog", "模型目录"], ["probe", "测速与探测"]] as const).map(([id, label]) => `<button data-wb-model-section="${id}" aria-pressed="${id === "catalog" ? this.modelSection !== "probe" : this.modelSection === id}">${label}</button>`).join("")}</div>`;
    if (this.modelSection !== "probe") {
      const syncOpen = this.modelSection === "sync";
      // 维护者 2026-09-28（无障碍口径）：这个纯图标按钮已有 `aria-label`
      // 与 `.wb-icon-hint` 提示板，所以**不加**原生 `title`——两者会先后
      // 出现，是双重悬停提示（同 accountActionsHtml 的账号卡详情按钮）。
      const toolbar = `<div class="wb-section-toolbar"><p class="wb-muted">单项测速会发起真实请求；同步只写客户端配置，不发模型请求。</p><div class="wb-model-ops"><button class="wb-btn wb-btn-sm" data-wb-model-action="model.catalog" aria-label="刷新模型目录">${wbIcon("refresh")}刷新目录</button><button class="wb-btn wb-btn-sm${syncOpen ? "" : " wb-action-primary"}" data-wb-model-section="${syncOpen ? "catalog" : "sync"}" aria-expanded="${syncOpen}">${syncOpen ? "收起同步" : "同步到客户端…"}</button><button class="wb-btn wb-btn-sm" data-wb-probe-entry="1" title="查看只读的上下文探测队列（真实计费探测仍走原有确认与预算门）">探测队列…</button></div></div>`;
      // Contract 4.7: the sync area is read → preview → apply → readback, then the
      // sync history and task drill; the scope checkboxes live in the table below.
      const syncArea = syncOpen
        ? `<section class="wb-block wb-gw-sync" aria-label="同步到客户端">` + this.modelSyncActionsHtml() + this.modelOverviewHtml(pending) + this.modelPreviewHtml()
          + `<details class="wb-secondary-details"${this.syncTasksOpen ? " open" : ""} data-wb-sync-tasks="1"><summary title="同步历史与计划任务 · 点击展开 / 收起">同步任务${wbFoldMark()}</summary><div class="wb-model-ops"><button data-wb-model-action="model.history" aria-label="刷新同步历史">${wbIcon("refresh")}${wbIconHint("刷新同步历史")}</button><button data-wb-model-action="model.task_status">查询任务状态</button></div>${this.modelHistoryHtml()}${this.modelTaskHtml()}</details>`
          + `</section>`
        : "";
      return tabs + toolbar + syncArea + this.modelResultChipHtml() + this.modelCatalogHtml(pending);
    }
    if (this.modelSection === "probe") {
      return tabs + `<section class="wb-probe-intro"><h3>实测能力，单独确认消耗</h3><details class="wb-secondary-details"${this.probeIntroOpen ? " open" : ""} data-wb-probe-intro="1"><summary title="探针说明（只读预览 / 写入边界 · 预算与二次确认）· 点击展开 / 收起">探针说明${wbFoldMark()}</summary><p class="wb-muted">同步从不发起模型请求，探测与同步彻底分开。这里可以查看待探测模型与预算；单独确认执行的测速、思考档位校验与上下文探测，等后端提供独立的探测动作后在此开放（原「应用并探测」组合入口已删除）。</p></details><div class="wb-model-ops"><button class="wb-action-primary" data-wb-model-action="model.context_probe_preview">查看探测队列</button><button data-wb-model-section="catalog">返回目录单项测速</button></div></section>` + this.modelResultChipHtml() + this.modelProbeQueueHtml(pending, probeQueue);
    }
    return tabs;
  }

  /** Sync action row + custom-scope panel (L-C granularity). The scope only
   *  restricts the backend write face when actually submitted; unchecked
   *  boxes mean 全部 (the backend contract's own default). */
  private modelSyncActionsHtml(): string {
    const scope = this.modelScopeCustom ? this.modelScopePanelHtml() : "";
    // The write boundary is labelled step by step (contract 4.7, adopted from
    // direction B): 同步范围 / 预览变更 only read, 应用同步 writes. The four-step
    // read→preview→apply→readback chain keeps its own words, and each step says
    // whether it has written anything — preview never has.
    return `<div class="wb-sync-toolbar"><div><h3>同步到客户端</h3><p class="wb-muted">默认不探测 · 写入前请核对预览与范围</p></div><div class="wb-model-ops"><button data-wb-model-action="model.scope_toggle">${this.modelScopeCustom ? "收起范围" : "同步范围"}</button><button data-wb-model-action="model.sync_preview">预览变更</button><span class="wb-write-group" style="${WB_WRITE_GROUP_STYLE}"><span class="wb-write-mark wb-write-mark-write" title="写·改配置：真实写入客户端配置，需二次确认">写·改配置</span><button class="wb-action-primary" data-wb-model-action="model.sync_apply">应用同步…</button></span></div></div>`
      + scope
      + this.modelResultChipHtml()
      + (this.modelScopeCustom
        ? `<p class="wb-muted">本次同步将按当前范围提交（预览变更与应用同步用的是同一份参数）：${escapeHtml(this.scopeSummary(this.modelScopeParams()))}；未选范围不写、不退役。</p>`
        : "")
      // The preview and the apply read the SAME scope payload; saying so here
      // keeps 「预览与实际写入面不同」 from being re-introduced by a future
      // edit that wires only one of the two branches. The readback is part of
      // the same statement: it is issued with the apply's submit-time scope,
      // and the overview below says what the read port really answered.
      + `<p class="wb-muted">预览与应用共用 chat2api → ZCode 范围；不勾选模型时同步全部上游模型。应用终态后的自动回读携带同一范围参数；读口若返回全局概览，页面如实标注。</p>`;
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
    if (!this.modelScopeCustom) return this.gwScopeParams("um");
    const models: Record<string, string[]> = {};
    if (this.modelChecked.size) {
      for (const key of this.modelChecked) {
        const at = key.indexOf("|");
        if (at <= 0 || key.slice(0, at) !== "um") continue;
        const source = key.slice(0, at);
        const modelId = key.slice(at + 1);
        const list = models[source] ?? [];
        list.push(modelId);
        models[source] = list;
      }
    }
    // Empty selected scopes stay explicitly selected. The backend refuses
    // them; rendering an empty selection must never broaden it to all models.
    return { scope_mode: "selected",
      sources: JSON.stringify(["um"]),
      consumers: JSON.stringify(["ZCode"]),
      ...(Object.keys(models).length ? { models } : {}) };
  }
  /** 提交时范围快照：深拷贝（`modelScopeParams()` 每次返回新对象，这里再拷
   *  一层，快照与之后任何勾选/编码改动都不共享引用）。params 只含字符串
   *  与「来源 → 模型 id 列表」两层，按 JSON 往返安全。 */
  private snapshotScope(params: Record<string, unknown>): Record<string, unknown> {
    return JSON.parse(JSON.stringify(params)) as Record<string, unknown>;
  }
  /** 解析一个范围参数对象（构造器产物或提交时快照）的三个维度。
   *  `null` = 该键未提交（= 全部）；`"unknown"` = 键存在但形状读不出来
   *  （写「未知」，不折成「全部」）。 */
  private scopeLists(params: Record<string, unknown> | null): {
    sources: ScopeList; consumers: ScopeList; models: ScopeList;
  } {
    const readList = (value: unknown): ScopeList => {
      if (value === undefined) return null;
      if (typeof value !== "string") return "unknown";
      try {
        const parsed: unknown = JSON.parse(value);
        return Array.isArray(parsed) && parsed.every(v => typeof v === "string")
          ? parsed as string[] : "unknown";
      } catch { return "unknown"; }
    };
    let models: ScopeList = null;
    const rawModels = params ? params.models : undefined;
    if (rawModels !== undefined) {
      if (!rawModels || typeof rawModels !== "object" || Array.isArray(rawModels)) {
        models = "unknown";
      } else {
        const keys: string[] = [];
        for (const [source, ids] of Object.entries(rawModels as Record<string, unknown>)) {
          if (!Array.isArray(ids) || ids.some(id => typeof id !== "string")) {
            models = "unknown";
            break;
          }
          keys.push(...(ids as string[]).map(id => `${source}|${id}`));
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
      value === "unknown" ? "未知"
        : value === null ? "全部"
          : (value.map(label).join("、") || "全部");
    return `来源 ${text(lists.sources, s => this.sourceLabel(s))}`
      + ` · 客户端 ${text(lists.consumers, v => v)}`
      + ` · 勾选模型 ${lists.models === "unknown" ? "未知" : lists.models === null ? "全部" : `${lists.models.length} 个`}`;
  }
  /** 范围参数的规范签名，只用于**本地比较**（预览提交时的范围 vs 当前勾选）：
   *  数组先排序，所以勾选顺序变化不算范围变化；不改变提交给后端的编码。 */
  private scopeSignature(params: Record<string, unknown> | null): string {
    if (!params) return "";
    const lists = this.scopeLists(params);
    const part = (value: ScopeList): string =>
      value === "unknown" ? "?" : value === null ? "*" : [...value].sort().join(",");
    return `${part(lists.sources)}|${part(lists.consumers)}|${part(lists.models)}`;
  }
  /** 预览提交后勾选是否已变：这份预览是按另一组范围参数算出来的，不能再
   *  冒充「与应用同一范围」的预览。勾选改回预览时的范围后标记自动消失。
   *  `params` = 当前动作会提交的范围（全部模型页 = 勾选面板构造器；单网关
   *  模型页 = 本来源的显式范围），只比较签名，不改变提交编码。 */
  private previewScopeChangedAgainst(params: Record<string, unknown>): boolean {
    return this.previewScopeParams !== null
      && this.scopeSignature(params)
        !== this.scopeSignature(this.previewScopeParams);
  }
  private previewScopeChanged(): boolean {
    return this.previewScopeChangedAgainst(this.modelScopeParams());
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
      else if (value && typeof value === "object") query.set(key, JSON.stringify(value));
    }
    const qs = query.toString();
    return qs ? `?${qs}` : "";
  }
private modelScopePanelHtml(): string { return '<div class="wb-svc-detail"><h4>同步范围</h4><p class="wb-muted">chat2api → ZCode；可在下方目录表勾选模型，不勾选时同步全部上游模型。</p></div>'; }
  /** Overview (L-C): per-client pending grouping with appliedDetails fallback,
   *  probe-queue counters, readback-failure honesty. 未读到时的措辞只来自本读口
   *  的真实请求状态（维护者 2026-09-28）：传输失败 / HTTP 非 2xx（实测状态）/
   *  形状错误 / 读取中，绝不写「8096 不可达或受管状态损坏」。
   *  维护者 2026-09-29（模型同步闭环）：应用终态后的自动回读按应用提交时的
   *  范围参数发起；后端读口当前不接收范围参数（usagesrv.py:3209 给 handler
   *  传 `{}`），所以响应是**全局概览**——按实测写出这一点，并按响应里已有的
   *  明确字段（`pending_details[].consumer`，缺席时用描述前缀）把本范围行与
   *  全局待处理分开；归属不了的维度写「未知」并记下接口依赖，不折成 0 条。
   *  计数缺字段一律「未知」，绝不 `?? 0`。 */
  private modelOverviewHtml(pending?: Record<string, unknown>): string {
    if (!pending) {
      const state = this.modelOverviewState;
      const line = state === "transport" ? "同步概览读取失败（网络失败或服务未运行）；下方动作仍可手动触发。"
        : state === "http" ? `同步概览读取失败（HTTP ${this.modelOverviewStatus}）；下方动作仍可手动触发。`
          : state === "shape" ? "同步概览读取失败（响应形状与预期不符）；下方动作仍可手动触发。"
            : "同步概览读取中…";
      // 没有旧快照时「操作已完成、最新状态读取失败」这条同样必须可达：
      // 回读失败标记 + 本读口真实失败状态 = 上面那句之外再说清「应用已完成、
      // 只是最新状态没读到」，并给只读重试（不重发写操作）。
      const readbackFailed = this.pendingReadbackFailed
        && (state === "transport" || state === "http" || state === "shape");
      const scopeText = this.modelReadbackScope
        ? `；本次回读请求范围：${escapeHtml(this.scopeSummary(this.modelReadbackScope))}` : "";
      return `<p class="wb-muted">${escapeHtml(line)}</p>`
        + (readbackFailed
          ? `<p class="wb-stale-warn">操作已完成，最新状态读取失败（不重发写操作）${scopeText}。<button type="button" class="wb-btn wb-btn-sm" data-wb-model-action="model.readback">重试读取</button></p>`
          : "");
    }
    // 回读进行中：旧读数仍在页面上，必须标出「读取中」，否则它会被当成
    // 本次回读的结果（本读口的真实状态字段，不靠猜）。
    const reading = this.modelOverviewState === "loading";
    const countsRaw = pending.catalog_counts;
    const countsKnown = !!countsRaw && typeof countsRaw === "object" && !Array.isArray(countsRaw);
    const counts = (countsKnown ? countsRaw : {}) as Record<string, unknown>;
    const countChips = Object.entries(counts).filter(([k]) => k === "um").map(([k, v]) =>
      `<span class="wb-count-chip">${escapeHtml(this.sourceLabel(k))} <b>${this.countText(typeof v === "number" ? v : undefined)}</b></span>`).join("");
    const pendingKnown = Array.isArray(pending.pending);
    const pendingList = pendingKnown ? pending.pending as string[] : [];
    const details = Array.isArray(pending.pending_details)
      ? pending.pending_details as SyncDetailRow[] : [];
    // `last_result` 与 `last_result_error` 分开读：结果文件读不出来时写「未知」，
    // 绝不把「没读到最近应用结果」写成「最近应用结果没有警告」。
    const lastResult = (pending.last_result ?? {}) as Record<string, unknown>;
    const lastResultKnown = !!pending.last_result && typeof pending.last_result === "object";
    const lastResultError = typeof pending.last_result_error === "string" ? pending.last_result_error : "";
    const warnings = Array.isArray(lastResult.warnings) ? lastResult.warnings : [];
    // Group the pending changes by client (L-C). pendingDetails carries a
    // real consumer field; without it the description prefix (the backend
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
        const hit = ["ZCode"].find(c => String(desc).startsWith(c));
        const consumer = hit ?? "未归属";
        const list = groups.get(consumer) ?? [];
        list.push({ description: String(desc) });
        groups.set(consumer, list);
      }
    }
    const kindLabel: Record<string, string> = {
      add: "新增", update: "更新", remove: "退役",
      provider_name: "显示名", pin: "固定",
    };
    const groupHtml = groups.size
      ? [...groups.entries()].map(([consumer, rows]) => {
        const kindCounts = new Map<string, number>();
        for (const r of rows) {
          const k = r.kind ?? "其他";
          kindCounts.set(k, (kindCounts.get(k) ?? 0) + 1);
        }
        const chips = [...kindCounts.entries()]
          .map(([k, n]) => `<span class="wb-count-chip">${escapeHtml(kindLabel[k] ?? k)} ${n}</span>`).join("");
        const lines = rows.slice(0, 40).map(r =>
          `<li>${escapeHtml(String(r.description ?? `${r.kind ?? ""} ${r.model_id ?? ""}`).slice(0, 200))}</li>`).join("");
        const more = rows.length > 40 ? `<li class="wb-muted">…其余 ${rows.length - 40} 项同组</li>` : "";
        return `<div class="wb-sync-client"><b>${escapeHtml(consumer)}</b> ${chips}<ul>${lines}${more}</ul></div>`;
      }).join("")
      : "";
    const probesKnown = !!pending.probes && typeof pending.probes === "object" && !Array.isArray(pending.probes);
    const probeRecords = (probesKnown ? pending.probes : {}) as Record<string, Record<string, unknown>>;
    const probePending = Object.entries(probeRecords)
      .filter(([, r]) => r.status === "pending").length;
    const probeVerified = Object.values(probeRecords)
      .filter(r => r.status === "verified").length;
    // 回读失败 = 回读标记仍在 + 概览读口这一路的真实请求状态是失败（传输 /
    // HTTP / 形状）；「读取中」不算失败。不再借用 modelOverviewFailed 这个已删除的布尔。
    const readbackFailed = this.pendingReadbackFailed
      && (this.modelOverviewState === "transport" || this.modelOverviewState === "http" || this.modelOverviewState === "shape");
    // The backend's lastStatus is an English enum (pending / changed / warning /
    // normal); every other state word on this page is Chinese, so it is mapped
    // here. An UNKNOWN word passes through unchanged (never guessed into a
    // Chinese word the backend did not send).
    const syncStatus: Record<string, string> = {
      pending: "待处理", changed: "有变更", warning: "有警告", normal: "正常",
    };
    const lastStatus = pending.last_status === undefined || pending.last_status === null
      ? "—" : String(pending.last_status);
    return `<div class="wb-stats-row"><div><small>各源目录数</small><b>${countChips || "—"}</b></div><div><small>最近同步状态</small><b>${escapeHtml(syncStatus[lastStatus] ?? lastStatus)}</b></div><div><small>待处理项</small><b>${pendingKnown ? pendingList.length : "未知"}</b></div><div><small>待探测（能力）</small><b>${probesKnown ? probePending : "未知"}</b></div><div><small>已实测（能力）</small><b>${probesKnown ? probeVerified : "未知"}</b></div></div>`
      + (reading ? `<p class="wb-muted">同步概览读取中…（以下为上一次读数）</p>` : "")
      + (readbackFailed
        ? `<p class="wb-stale-warn">操作已完成，最新状态读取失败（不重发写操作）${this.modelReadbackScope ? `；本次回读请求范围：${escapeHtml(this.scopeSummary(this.modelReadbackScope))}` : ""}。<span class="wb-count-chip wb-stale-chip">陈旧：以下读数与应用前的状态</span><button type="button" class="wb-btn wb-btn-sm" data-wb-model-action="model.readback">重试读取</button></p>`
        : "")
      + (readbackFailed ? "" : this.modelReadbackScopeHtml(pending, pendingKnown, pendingList, details))
      + (pendingKnown
        ? (pendingList.length
          ? `<div class="wb-sync-diff"><strong>待处理变更（按客户端分组：新增/更新/退役/显示名——上下文只是其中之一）</strong>${groupHtml || `<ul>${pendingList.map(d => `<li>${escapeHtml(String(d))}</li>`).join("")}</ul>`}</div>`
          : `<p class="wb-muted">当前无待处理的模型变更。</p>`)
        : `<p class="wb-muted">待处理变更未知：读口未返回 pending 字段（不把它当成「当前无待处理的模型变更」）。</p>`)
      + (lastResultKnown
        ? (warnings.length ? `<div class="wb-sync-diff"><strong>最近应用结果 · 警告</strong><ul>${warnings.map(w => `<li>${escapeHtml(String(w))}</li>`).join("")}</ul></div>` : "")
        : (lastResultError
          ? `<p class="wb-muted">最近应用结果未知：结果文件读取失败（${escapeHtml(lastResultError)}）——不把它当成「没有警告」。</p>`
          : `<p class="wb-muted">最近应用结果未知：读口未返回 last_result 字段（不把它当成「没有警告」）。</p>`));
  }


  private modelReadbackScopeHtml(pending: Record<string, unknown>, pendingKnown: boolean,
      pendingList: string[], details: SyncDetailRow[]): string {
    const scope = this.modelReadbackScope;
    if (!scope) return "";
    const lists = this.scopeLists(scope);
    const allRows: SyncDetailRow[] = details.length
      ? details : pendingList.map(desc => ({ description: String(desc) }));
    const consumerOf = (row: SyncDetailRow): string =>
      row.consumer ?? ["ZCode"].find(c => String(row.description ?? "").startsWith(c)) ?? "";
    let attribution: string;
    if (!pendingKnown) {
      attribution = "本范围对应行未知：读口未返回 pending / pending_details（不折成 0 条）。";
    } else if (lists.consumers === "unknown") {
      attribution = "本范围对应行未知：提交时的客户端范围参数形状读不出来。";
    } else {
      const inScope = lists.consumers === null
        ? allRows : allRows.filter(row => lists.consumers!.includes(consumerOf(row)));
      const out = allRows.length - inScope.length;
      attribution = `本范围可归属的待处理行 ${inScope.length} 条${out ? `，其余 ${out} 条在范围外` : ""}`;
    }
    const listLen = (value: ScopeList): number | null =>
      value === "unknown" ? null : value === null ? 0 : value.length;
    // 来源级 / 模型级归属：明细行没有 source 字段（`pending_details` 只有
    // consumer/kind/model_id/description；`pending` 只有描述串），所以有来源或
    // 模型范围时这两级写「未知」，绝不把「归属不了」折成「本范围 0 条」。
    const hasDetailRows = details.length > 0;
    const noSource = hasDetailRows ? "不返回 source，无法与 source|model_id 范围匹配" : "只有描述串，没有来源字段";
    const noModel = hasDetailRows ? "不返回 source，无法与 source|model_id 范围匹配" : "只有描述串，没有 model_id";
    const sourceNote = listLen(lists.sources) === 0 ? "" : `；来源级归属未知（明细行${noSource}）`;
    const modelNote = listLen(lists.models) === 0 ? "" : `；模型级归属未知（明细行${noModel}）`;
    return `<p class="wb-muted">应用后回读：请求已带同范围参数（${escapeHtml(this.scopeSummary(scope))}）；后端读口当前不接收范围参数，下面是全局概览。${escapeHtml(attribution)}${sourceNote}${modelNote}。</p>`;
  }
  private modelPreviewHtml(): string {
    const preview = this.actionResults["model.sync_preview"];
    if (!preview) return "";
    const plan = (preview.preview ?? {}) as Record<string, unknown>;
    // 重算计划缺字段 = 未知，绝不落「无配置变化」：后端 preview 路径明确写入
    // `plan`（model_sync.py:1568-1572），读不到时只能说没读到。
    const planKnown = Array.isArray(plan.plan);
    const planChanges = planKnown ? plan.plan as string[] : [];
    const warnings = Array.isArray(plan.warnings) ? plan.warnings : [];
    // The preview states in words that it wrote nothing (contract 5.2 #2) and
    // carries the read boundary mark; the stale marker keeps its own wording.
    // 维护者 2026-09-29：这份预览按哪一组范围算出来由 `previewScopeParams`
    // 记录（结果落定时写回提交时快照）；勾选在预览之后变过时按当前勾选与
    // 快照的差异单独标「范围已变」——不把另一组范围的预览冒充成本次应用的范围。
    const scopeChanged = this.previewScopeChanged();
    return `<div class="wb-svc-detail"><h4>同步预览（${escapeHtml(String(plan.status ?? "—"))} · 未写入任何配置）${this.previewStale ? `<span class="wb-count-chip wb-stale-chip">已过期（应用后目录已变）</span>` : ""}${scopeChanged ? `<span class="wb-count-chip wb-stale-chip">已过期（范围勾选已变，应用会按当前范围提交；请重新预览）</span>` : ""}</h4>`
      + `<p class="wb-muted">本次预览范围：${escapeHtml(this.scopeSummary(this.previewScopeParams))}（与应用同步共用同一构造器与编码；勾选变化后请重新预览）。</p>`
      + (planKnown
        ? (planChanges.length
          ? `<div class="wb-sync-diff"><strong>重算同步计划（${planChanges.length} 项 · 真实写入需「应用同步」二次确认）</strong><ul>${planChanges.map(d => `<li>${escapeHtml(String(d))}</li>`).join("")}</ul></div>`
          : `<p class="wb-muted">按实时目录与受管配置重算：无配置变化。</p>`)
        : `<p class="wb-muted">重算同步计划未知：响应未返回 plan 明细（不把它当成「无配置变化」）。</p>`)
      + (warnings.length
        ? `<div class="wb-sync-diff"><strong>来源警告</strong><ul>${warnings.map(w => `<li>${escapeHtml(String(w))}</li>`).join("")}</ul></div>`
        : "")
      + `</div>`;
  }
  /** Probe queue (L-E.4): effort-probe pending from sync state + the
   *  context-probe queue with estimates. Estimates come from the server;
   *  an unpriced model stays 未定价, never a fabricated fee. */
  private modelProbeQueueHtml(
      pending?: Record<string, unknown>, queueResult?: Record<string, unknown>): string {
    const probes = (pending?.probes ?? {}) as Record<string, Record<string, unknown>>;
    const probeRows = Object.entries(probes)
      .filter(([, r]) => r.status === "pending")
      .map(([key, r]) => {
        const [source, modelId] = key.split("|");
        // 缺 attempts 字段写「未知」，绝不 `?? 0` 印成「已尝试 0/3」。
        const attempts = typeof r.attempts === "number" && Number.isFinite(r.attempts)
          ? `${r.attempts}/3` : "未知";
        return `<li><code>${escapeHtml(modelId ?? key)}</code>（${escapeHtml(this.sourceLabel(source))}）· 新模型能力待探测 · 已尝试 ${attempts}</li>`;
      }).join("");
    // `records` 字段缺席 ≠ 空队列：只有读口真的返回了数组才写「队列为空」。
    const queueKnown = Array.isArray(queueResult?.records);
    const contextQueue = queueKnown ? queueResult!.records as ContextProbeQueueRow[] : [];
    const contextRows = contextQueue.map(row => {
      const reqs = typeof row.estimated_requests === "number" ? row.estimated_requests : null;
      const chars = typeof row.input_chars_estimate === "number" ? row.input_chars_estimate : null;
      const ctx = typeof row.declared_context === "number" ? row.declared_context : null;
      return `<tr><td>${escapeHtml(this.sourceLabel(String(row.source ?? "")))}</td><td><code>${escapeHtml(String(row.model_id ?? ""))}</code></td><td class="wb-fee-num">${ctx !== null ? ctx.toLocaleString("zh-CN") : "—"}</td><td>${escapeHtml(String(row.status ?? "—"))}${row.executable === false ? " · 本轮不可执行" : ""}</td><td class="wb-fee-num">${reqs !== null ? String(reqs) : "—"}</td><td class="wb-fee-num">${chars !== null ? chars.toLocaleString("zh-CN") : "—"}</td><td>${escapeHtml(String(row.estimate_note || "按各来源账本计"))}</td></tr>`;
    }).join("");
    const budgetNote = typeof queueResult?.budget_note === "string" ? queueResult.budget_note : "";
    return `<section class="wb-block"><h3>待探测队列（能力 / 上下文）</h3>`
      + (probeRows
        ? `<ul class="wb-probe-queue">${probeRows}</ul><p class="wb-muted">能力探测是单独确认的计费动作，从不随同步发出；独立的执行入口等后端提供后开放。</p>`
        : `<p class="wb-muted">能力探测队列当前为空（新模型或声明变化出现时进入队列）。</p>`)
      + (queueResult
        ? (queueKnown
          ? (contextRows
            ? `<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>来源</th><th>模型</th><th class="wb-fee-num">声明上下文</th><th>队列状态</th><th class="wb-fee-num">估算请求数</th><th class="wb-fee-num">输入字符估计</th><th>估算说明</th></tr></thead><tbody>${contextRows}</tbody></table></div>`
            : `<p class="wb-muted">上下文探测队列为空（有声明上下文的模型全部已实测或无声明）。</p>`)
          : `<p class="wb-muted">上下文探测队列未知：响应未返回 records 字段（不把它当成空队列）。</p>`)
        + (budgetNote ? `<p class="wb-muted">${escapeHtml(budgetNote)}</p>` : "")
        + `<p class="wb-muted">真实长输入实测会产生计费请求：清单、请求数与预算须集中批准后才执行；分享测试预算不挪用为上下文探测预算。</p>`
        : `<p class="wb-muted">上下文探测队列未读取（点「探测队列」查询；只读动作）。</p>`)
      + `</section>`;
  }
  private modelHistoryHtml(): string {
    const history = this.actionResults["model.sync_history"];
    if (!history) return "";
    const rows = Array.isArray(history.history) ? history.history as SyncHistoryEntry[] : [];
    // 缺字段一律「未知」，绝不 `?? 0`：损坏跳过行数读不到时不写「0 行」，
    // 总数读不到时不用「已显示条数」冒充总数。
    const skipped = typeof history.skipped === "number" && Number.isFinite(history.skipped)
      ? history.skipped : null;
    const total = typeof history.total === "number" && Number.isFinite(history.total)
      ? history.total : null;
    const line = (e: SyncHistoryEntry) => {
      const count = (value: unknown): string =>
        Array.isArray(value) ? String(value.length) : "未知";
      return `<tr><td>${escapeHtml(String(e.timestamp ?? "—")).slice(0, 19)}</td><td>${escapeHtml(String(e.status ?? "—"))}</td><td class="wb-fee-num">${count(e.applied)}</td><td class="wb-fee-num">${count(e.pending)}</td><td class="wb-fee-num">${count(e.warnings)}</td></tr>`;
    };
    return `<section class="wb-block"><h3>同步历史（最近 ${rows.length} 条 · 共 ${total === null ? "未知" : total} 条${skipped ? ` · ${skipped} 行损坏跳过` : ""}）</h3>`
      + (rows.length
        ? `<div class="wb-table-scroll"><table class="wb-table"><thead><tr><th>时间</th><th>状态</th><th class="wb-fee-num">已应用</th><th class="wb-fee-num">待应用</th><th class="wb-fee-num">警告</th></tr></thead><tbody>${rows.map(line).join("")}</tbody></table></div>`
        : `<p class="wb-muted">暂无历史记录。</p>`)
      + `</section>`;
  }
  private modelTaskHtml(): string {
    const task = this.actionResults["model.task_status"];
    if (!task) return "";
    return `<section class="wb-block"><h3>同步计划任务</h3><div class="wb-kv">${renderTaskRows(task)}</div></section>`;
    function renderTaskRows(t: Record<string, unknown>): string {
      return Object.entries(t).filter(([, v]) => v !== null && v !== undefined)
        .slice(0, 8).map(([k, v]) => `<span>${escapeHtml(k)}</span><b>${escapeHtml(typeof v === "object" ? JSON.stringify(v).slice(0, 120) : String(v))}</b>`).join("");
    }
  }
  /** Catalog table (L-C): search filter, scope checkboxes, per-model speedtest
   *  with honest results, per-binding pin toggles (only when the backend
   *  exposes the binding contract), per-source access status.
   *  Rev17 `onlySource` = 单网关模型页按后端服务身份表过滤本来源（目录读取本身
   *  仍是后端UM只读读取，页面只过滤展示，不另读一遍、也不推算）。 */
  private modelCatalogHtml(pending?: Record<string, unknown>, onlySource?: string): string {
    const catalog = this.actionResults["model.catalog"];
    // 读取失败记忆（与单网关模型页共用同一张 `actionResultFailures`）：目录读面
    // 本次实测失败时页面必须写失败句，不能把「读取失败」渲染成「尚未读取」。
    const catalogFailure = this.actionResultFailures.get("model.catalog");
    const sourceName = onlySource ? this.sourceLabel(onlySource) : "";
    // 技术缘由（后端口径、暂停范围、历史绑定保留）收进同一个读口的键盘可达详情。
    // `paused_sources` 是 model.sync_pending 的只读字段（console_model_actions.py:209
    // 直接返回 model_sync.PAUSED_SOURCES）；读到就按它写，没读到就只写前端自己的
    // 排除口径——不把「没读到」写成「没有暂停源」，也不臆造故障原因。
    if (!catalog) {
      // 读取失败按既有错误状态如实显示（维护者 2026-10-06 接线）：本次读取失败
      // （`model.catalog` 的实测失败原因）就写失败句 + 重试入口，绝不落成
      // 「尚未读取」或空表；未读取（还没问过）与读取失败是两件事。
      const empty = catalogFailure
        ? (onlySource
          ? `<div class="wb-empty-card"><strong class="wb-stale-warn">目录读取失败</strong><small>${escapeHtml(catalogFailure)}。点「刷新本来源目录」重试（只读，不改客户端配置）；失败不等于没有模型，也不等于本来源为空。</small></div>`
          : `<div class="wb-empty-card"><strong class="wb-stale-warn">目录读取失败</strong><small>${escapeHtml(catalogFailure)}。点工具条上的「刷新目录」重试（只读，不改客户端配置）；失败不等于没有模型，也不等于空目录。</small></div>`)
        : (onlySource
          ? `<div class="wb-empty-card"><strong>目录尚未读取</strong><small>点「刷新本来源目录」读取上游模型目录（只读，不改客户端配置）；本页只列出 ${escapeHtml(sourceName)} 的行，读取结果与失败原因显示在工具条下方，未读取不等于没有模型。</small></div>`
          : `<div class="wb-empty-card"><strong>目录尚未读取</strong><small>点工具条上的「刷新目录」读取上游模型目录（只读，不改客户端配置）；读取结果与失败原因显示在工具条下方，未读取不等于没有模型。</small></div>`);
      return `<section class="wb-block wb-char"><h3>${onlySource ? `模型目录（来源 ${escapeHtml(sourceName)} 实时）` : "模型目录（上游实时）"}${catalogFailure ? " · 读取失败" : ""}</h3>`
        + empty
        + `</section>`;
    }
    const catalogsAll = (catalog.catalogs ?? {}) as Record<string, CatalogModelRow[]>;
    // 单网关模型页只展示本来源的行；目录行本身仍是后端返回（后端不接收来源
    // 参数、固定返回UM），页面只做过滤，不改写、不推算。
    const catalogs: Record<string, CatalogModelRow[]> = onlySource
      ? { [onlySource]: Array.isArray(catalogsAll[onlySource]) ? catalogsAll[onlySource] : [] }
      : catalogsAll;
    const warnList = Array.isArray(catalog.warnings) ? catalog.warnings as string[] : [];
    const checkedAt = typeof catalog.checked_epoch === "number"
      ? new Date(catalog.checked_epoch * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "";
    // P1 (2026-09-24): persisted capability-probe records (state["probes"],
    // exposed read-only by model.sync_pending) — the ONLY probe evidence the
    // catalog can show; a verified record's efforts are the probe-verified
    // tiers, kept apart from the source value.
    const probes = (pending?.probes ?? {}) as Record<string, Record<string, unknown>>;
    const q = this.modelCatalogQ.trim().toLowerCase();
    const rows = Object.entries(catalogs).filter(([src]) => src === "um").flatMap(([src, list]) => {
      const arr = Array.isArray(list) ? list : [];
      const warnHit = warnList.some(w => String(w).includes(this.sourceLabel(src)));
      const status = arr.length
        ? `<span class="wb-count-chip">已接入 ${arr.length}</span>`
        : (warnHit ? `<b class="wb-stale-warn">读取失败</b>` : `<span class="wb-muted">空目录</span>`);
      return arr.map((m, i) => {
        if (q && !(m.id.toLowerCase().includes(q)
            || String(m.display_name ?? "").toLowerCase().includes(q))) return "";
        const checkedKey = `${src}|${m.id}`;
        const checkCell = this.modelScopeCustom
          ? `<td><input type="checkbox" data-model-row-check value="${escapeHtml(checkedKey)}" aria-label="勾选模型 ${escapeHtml(m.id)}"${this.modelChecked.has(checkedKey) ? " checked" : ""}/></td>`
          : "";
        const effortKey = `${src}|${m.id}`;
        const picked = this.modelPick === effortKey;
        return `<tr${picked ? ` class="wb-model-row-on"` : ""}><td>${escapeHtml(this.sourceLabel(src))}${i === 0 ? " " + status : ""}</td>${this.modelIdCellHtml(src, m.id)}<td>${escapeHtml(m.display_name || "—")}</td><td class="wb-fee-num">${m.context !== null && m.context !== undefined ? m.context.toLocaleString("zh-CN") : "—"}</td>${this.effortCellHtml(src, m, probes[effortKey])}<td>${m.publish ? "发布" : "不发布"}</td>${checkCell}</tr>`;
      }).join("");
    }).join("");
    const renderWarns = (list: string[]): string =>
      list.length ? `<p class="wb-muted"><b class="wb-stale-warn">${escapeHtml(list.join("；"))}</b></p>` : "";
    const headCells = `<th>来源</th><th>模型 id</th><th>显示名</th><th class="wb-fee-num">声明上下文</th><th>推理档</th><th>发布</th>`
      + (this.modelScopeCustom ? `<th>勾选</th>` : "");
    // F12-5: fixed column widths, shared by header and rows, so a refresh or a
    // long value never re-flows the table; details live in the pane beside it.
    const cols = `<colgroup><col class="wb-col-src"/><col class="wb-col-id"/><col class="wb-col-name"/><col class="wb-col-ctx"/><col class="wb-col-effort"/><col class="wb-col-pub"/>${this.modelScopeCustom ? `<col class="wb-col-check"/>` : ""}</colgroup>`;
    const pickedRowAll = this.modelPickRow(catalogsAll);
    // 单网关模型页里逐模型详情仍在「全部模型」视图：本页选中的行不属于本来源时
    // 不拿它冒充本页详情。
    let pickedRow: { src: string; m: CatalogModelRow } | "none" | "gone";
    if (onlySource) {
      pickedRow = pickedRowAll !== "none" && pickedRowAll !== "gone" && pickedRowAll.src === onlySource
        ? pickedRowAll : "none";
    } else pickedRow = pickedRowAll;
    // 单网关模型页里逐模型详情（测速 / 设置 / 证据）仍在「全部模型」视图的详情栏；
    // 这里不拿另一个来源的选中行冒充本来源详情。
    const pane = onlySource
      ? (pickedRow === "none"
        ? `<aside class="wb-model-detail" aria-label="模型详情"><p class="wb-muted">逐模型详情（测速、设置、证据）在「全部模型」视图里点选模型后显示；本页只列 ${escapeHtml(sourceName)} 的目录行与同步入口。</p></aside>`
        : this.modelDetailPaneHtml(pickedRow, probes))
      : this.modelDetailPaneHtml(pickedRow, probes);
    // 单来源视图里其他来源的警告不吞掉：如实计数并说明它们属于别处。
    const warnShown = onlySource
      ? warnList.filter(w => String(w).includes(sourceName)) : warnList;
    const warnHidden = onlySource ? warnList.length - warnShown.length : 0;
    const readFailed = onlySource ? warnShown.length > 0 : !rows;
    const title = onlySource ? `模型目录（来源 ${escapeHtml(sourceName)} 实时` : "模型目录（上游实时";
    return `<section class="wb-block wb-char"><h3>${title}${readFailed ? " · 读取失败" : ""}${checkedAt ? ` · 读取于 ${escapeHtml(checkedAt)}` : ""}）</h3>`
      + `<div class="wb-model-toolbar"><input type="text" class="wb-model-q" data-model-q placeholder="搜索模型 id / 显示名（字面量）" value="${escapeHtml(this.modelCatalogQ)}" aria-label="目录搜索"/><small class="wb-muted">声明上下文=目录元数据；实测通过长度是观测下界，独立显示于测速/探测队列，不标注精确容量。</small></div>`
      + renderWarns(warnShown)
      + (warnHidden ? `<p class="wb-muted">另有 ${warnHidden} 条其他来源的读取警告（本页只列 ${escapeHtml(sourceName)}）；完整警告见「全部模型」视图。</p>` : "")
      // 单来源视图里空表必须说清是「读取失败」还是「真的 0 个模型」，不让空表冒充两种含义。
      + (onlySource && !rows
        ? (warnShown.length
          ? `<p class="wb-muted"><b class="wb-stale-warn">本来源目录读取失败（见上方警告）；空表不代表没有模型。</b></p>`
          : `<p class="wb-muted">本来源目录为空（读取成功、返回 0 个模型）；空目录不代表账号额度为零。</p>`)
        : "")
      + `<div class="wb-model-split"><div class="wb-table-scroll"><table class="wb-table wb-model-table">${cols}<thead><tr>${headCells}</tr></thead><tbody data-model-tbody>${rows}</tbody></table></div>`
      + pane + `</div>`
      + `<p class="wb-muted">目录刷新只读取当前上游模型目录；单源失败只进警告，空目录不代表账号额度为零。思考档按行标注来源：上游目录明示 / 本地已知表；能力探测验证过的档位单独标注「能力探测已验证」，可在右侧详情中与目录值对照或恢复；两者都未提供时显示「未提供」，绝不伪造档位。测速为手工触发的真实计费请求，绝不随目录刷新自动发出。</p></section>`;
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
  private modelPickRow(catalogs: Record<string, CatalogModelRow[]>): { src: string; m: CatalogModelRow } | "none" | "gone" {
    if (!this.modelPick) return "none";
    const at = this.modelPick.indexOf("|");
    const src = this.modelPick.slice(0, at), id = this.modelPick.slice(at + 1);
    const m = (Array.isArray(catalogs[src]) ? catalogs[src] : []).find(row => row.id === id);
    return m ? { src, m } : "gone";
  }
  /** F12-5 independent detail pane: full id, declared context, the read-only
   *  output cap with its source, the effort tiers with provenance and the
   *  advanced comparison, and the hand-started speed test. A refresh keeps the
   *  pick; a model no longer in the catalog says so instead of vanishing. */
  private modelDetailPaneHtml(row: { src: string; m: CatalogModelRow } | "none" | "gone", probes: Record<string, Record<string, unknown>>): string {
    if (row === "none") return `<aside class="wb-model-detail" aria-label="模型详情"><p class="wb-muted">在左侧目录点一个模型 id，这里显示它的完整信息、思考档位来源与独立测速。</p></aside>`;
    if (row === "gone") return `<aside class="wb-model-detail" aria-label="模型详情"><p class="wb-muted">之前选中的模型已不在本次目录里（可能已退役或该来源读取失败）。请重新选择。</p></aside>`;
    const { src, m } = row;
    const key = `${src}|${m.id}`;
    const cap = m.output_token_cap;
    const capText = cap && typeof cap.value === "number"
      ? `${cap.value.toLocaleString("zh-CN")}（${cap.source === "compat_default" ? "兼容默认值，不是上游批准容量" : cap.source ? `来源：${cap.source}` : "来源未提供"}）`
      : "上游未提供（未核实）";
    const speed = this.speedCellHtml(src, m.id);
    const speedKey = `${SPEEDTEST_SOURCE[src] ?? src}|${m.id}`;
    return `<aside class="wb-model-detail" aria-label="模型详情：${escapeHtml(m.id)}">`
      + `<div class="wb-model-detail-head"><h4>${escapeHtml(m.id)}</h4><span class="wb-badge wb-badge-${m.publish ? "ok" : "idle"}">${m.publish ? "发布到客户端" : "不发布"}</span></div>`
      + `<p class="wb-muted">${escapeHtml(this.sourceLabel(src))} · 实际模型 id 始终保留 <button type="button" class="wb-btn wb-btn-sm" data-wb-model-id-copy="${escapeHtml(m.id)}">复制 id</button></p>`
      + `<dl class="wb-model-facts">`
      + `<div><dt>显示名</dt><dd>${escapeHtml(m.display_name || "—")}</dd></div>`
      + `<div><dt>声明上下文</dt><dd>${m.context !== null && m.context !== undefined ? `${m.context.toLocaleString("zh-CN")} Token` : "上游未声明"}<small>目录元数据；实测通过长度是下界，见「测速与探测」</small></dd></div>`
      + `<div><dt>单次输出上限（只读）</dt><dd>${escapeHtml(capText)}</dd></div>`
      + `</dl>`
      + this.modelSettingsHtml(src, m, probes[key])
      + this.effortAdvancedHtml(src, m, probes[key])
      + `<div class="wb-model-speed"><h5>独立测速</h5><div data-speed-cell="${escapeHtml(speedKey)}">${speed}</div><small class="wb-muted">真实计费的短请求，只测此模型；不证明最大上下文或长任务稳定性。</small></div>`
      + `</aside>`;
  }
  /** P1 (2026-09-24): one catalog row's 推理档 cell. The value shown is
   *  the probe-verified set when one exists (unless the user restored the
   *  source value for this page session), otherwise the source value; the
   *  label says WHERE it came from — upstream-declared, the local known
   *  table, or a billed capability probe. A row with neither shows
   *  未提供 and never a fabricated tier. */
  private effortCellHtml(src: string, m: CatalogModelRow,
      probe?: Record<string, unknown>): string {
    const key = `${src}|${m.id}`;
    const verified = this.verifiedEfforts(probe);
    const catalog = (m.efforts ?? []).join("/");
    const useVerified = !!verified && !this.effortRestored.has(key);
    const value = useVerified ? verified.join("/") : catalog;
    const label = useVerified ? "能力探测已验证" : this.effortSourceLabel(m.efforts_source);
    // 上游显式声明空档位时也不能说「两表都未提供」——那是它自己的声明。
    const emptyNote = m.efforts_source === "upstream"
      ? "上游目录明示为空档" : "上游目录与本地已知表均未提供";
    return `<td><div class="wb-effort-cell"><span>${escapeHtml(value || "未提供")}</span>`
      + (value
        ? `<small class="wb-muted">（${escapeHtml(label)}）</small>`
        : `<small class="wb-muted">（${escapeHtml(emptyNote)}）</small>`)
      + `</div></td>`;
  }
  /** P1 (2026-09-24): the 高级覆盖 area for ONE catalog row — expanded
   *  separately per row. It keeps the SOURCE value visible (with its
   *  provenance), shows the probe-verified value when a billed capability
   *  probe recorded one, lets the row display either one for THIS page
   *  session only, and points 校验 at the existing billed probe entry —
   *  no new paid path, no write to the source directory or the sync
   *  config, nothing persisted. */
  /** A14-3 #3 model settings (Rev8 契约 5/6，2026-10-06 接线). 证据（目录声明、
   *  已验证 Token 下界、字符证据）与配置（草稿 / 已应用 / 客户端读回）分成两列；
   *  草稿表单的保存 / 恢复默认 / 预览 / 应用走既有 model.settings_* 动作：保存只写
   *  草稿（保存≠已应用，不发模型请求）、应用前必须先预览并二次确认、ZCode 运行中
   *  整体延期按后端返回如实显示。配置读面来自 model.context_overrides（只读），
   *  无值一律「未知」，页面不推算已应用或客户端状态。证据来自页面已读的只读探测
   *  预览（model.context_probe_preview），由「读取探测证据」按钮触发，绝不发计费探测。 */
  private modelSettingsHtml(src: string, m: CatalogModelRow, probe?: Record<string, unknown>): string {
    const key = `${src}|${m.id}`;
    const raw = this.actionResults["model.context_probe_preview"];
    const failed = this.actionResultFailures.get("model.context_probe_preview");
    let ev: ContextEvidence | undefined;
    let evState: "unread" | "bad" | "none" | "ok" = "unread";
    if (raw !== undefined) {
      try {
        ev = normalizeContextEvidence(raw).find(r => r.source === src && r.modelId === m.id);
        evState = ev ? "ok" : "none";
      } catch { evState = "bad"; }
    }
    const n = (v: number) => v.toLocaleString("zh-CN");
    const when = (iso: string | null) => {
      const t = iso ? Date.parse(iso) : NaN;
      return Number.isFinite(t) ? new Date(t).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
    };
    const fact = (dt: string, dd: string, note = "") => `<div><dt>${escapeHtml(dt)}</dt><dd>${dd}${note ? `<small>${escapeHtml(note)}</small>` : ""}</dd></div>`;
    const evNote = evState === "unread" ? (failed ? `探测证据读取失败（${failed}）` : "探测证据未读取")
      : evState === "bad" ? "探测证据形状不可识别（未知，不当作没有证据）"
      : evState === "none" ? "该模型不在探测证据记录中" : "";
    const floor = ev?.verifiedTokenFloor ?? null;
    // 证据区按后端读数原样显示（0 是合法读数）；表单的「采用已验证下界」只接受
    // 可用上下文（后端 contextTokens 要求 ≥1），0 不是容量，按无值渲染。
    const usableFloor = floor !== null && floor >= 1 ? floor : null;
    const chars = ev && (ev.observedCharacterFloor !== null || ev.observedCharacterCeiling !== null)
      ? ev.observedCharacterCeiling !== null && ev.observedCharacterFloor !== null
        ? `${n(ev.observedCharacterFloor)}–${n(ev.observedCharacterCeiling)} 字符`
        : ev.observedCharacterFloor !== null ? `≥ ${n(ev.observedCharacterFloor)} 字符` : `≤ ${n(ev.observedCharacterCeiling!)} 字符`
      : "";
    const evidence = `<dl class="wb-model-facts">`
      + fact("声明上下文", m.context !== null && m.context !== undefined ? `${n(m.context)} Token` : "上游未声明", "目录元数据")
      + fact("已验证 Token 下界", ev ? (floor !== null ? `≥ ${n(floor)} Token` : "尚无") : "—",
        ev ? [floor !== null ? "实测通过的长度，是下界不是上限" : "", ev.lastSuccess ? `最近成功 ${when(ev.lastSuccess)}` : "", ev.staleReason ? `已过期：${ev.staleReason}` : ""].filter(Boolean).join(" · ") : evNote)
      + fact("字符证据", ev ? chars || "尚无" : "—", ev ? `证据修订 ${ev.evidenceRevision ?? "未知"} · 历史 ${ev.historyCount ?? "未知"} 条` : "")
      + `</dl>`;
    const verified = this.verifiedEfforts(probe) ?? [];
    const efforts = [...new Set([...(m.efforts ?? []), ...verified])];
    const effortSrc = (e: string) => [(m.efforts ?? []).includes(e) ? `目录 · ${this.effortSourceLabel(m.efforts_source)}` : "", verified.includes(e) ? "探测已验证" : ""].filter(Boolean).join("，");
    // 读取探测证据 = 页面已读的只读预览读取（model.context_probe_preview），
    // 按钮只触发读取、绝不发计费探测；未读取时显示，读取中禁用，读到证据后隐藏。
    const evidenceBusy = this.actionInFlight("model.context_probe_preview");
    const evidenceButton = evState === "unread"
      ? (evidenceBusy
        ? `<button type="button" class="wb-btn wb-btn-sm" disabled aria-busy="true">${wbIcon("refresh")}读取中…</button>`
        : `<button type="button" class="wb-btn wb-btn-sm" data-wb-evidence-read="1" title="只读读取探测证据（model.context_probe_preview）；绝不发计费探测">${wbIcon("refresh")}读取探测证据</button>`)
      : "";
    const settings = this.msContextState(key);
    const settingsBusy = this.actionInFlight("model.context_overrides");
    const settingsFailed = this.actionResultFailures.get("model.context_overrides");
    const badge = settings.state === "ok"
      ? (settings.ctx?.draft
        ? `<span class="wb-badge wb-badge-warn">有草稿待应用</span>`
        : settings.ctx?.applied
          ? `<span class="wb-badge wb-badge-ok">已应用</span>`
          : `<span class="wb-badge wb-badge-idle">无草稿</span>`)
      : settings.state === "none"
        ? `<span class="wb-badge wb-badge-idle">无设置记录</span>`
        : settingsFailed
          ? `<span class="wb-badge wb-badge-warn">设置读取失败</span>`
          : settings.state === "bad"
            ? `<span class="wb-badge wb-badge-warn">设置读面形状不可识别</span>`
            : settingsBusy
              ? `<span class="wb-badge wb-badge-idle">设置读取中…</span>`
              : `<span class="wb-badge wb-badge-idle">设置未读取</span>`;
    return `<section class="wb-model-settings" aria-label="模型设置"><div class="wb-model-settings-head"><h5>模型设置</h5>${badge}${evidenceButton}</div>`
      + `<h6>证据</h6>${evidence}`
      + `<h6>实际配置</h6>`
      + this.modelSettingsStateHtml(key)
      + this.modelSettingsFormHtml(key, m, efforts, effortSrc, usableFloor, settings)
      + `<p class="wb-muted">保存草稿只写 model-sync 状态（保存≠已应用，不发模型请求）；应用前必须先预览并确认，ZCode 运行中：安装版已准入时热写（锁协议单文件发布），未准入时整体延期（按后端返回如实显示）；默认档位只列有依据的选项${efforts.length ? "" : "（本模型暂无）"}；输出上限只读。</p>`
      + `</section>`;
  }
  /** 设置读面（model.context_overrides，只读，normalizeContexts）→ 草稿 / 已应用 /
   *  客户端读回三行。无值一律「未知」；读面未读取 / 读取失败 / 形状不可识别 /
   *  该模型不在读面各自如实说明，绝不把「没读到」折成「没有设置」。 */
  private modelSettingsStateHtml(key: string): string {
    const raw = this.actionResults["model.context_overrides"];
    const failure = this.actionResultFailures.get("model.context_overrides");
    const busy = this.actionInFlight("model.context_overrides");
    const readBtn = (label: string) =>
      `<button type="button" class="wb-btn wb-btn-sm" data-wb-ms-action="read" data-wb-ms-key="${escapeHtml(key)}">${wbIcon("refresh")}${escapeHtml(label)}</button>`;
    if (raw === undefined) {
      const line = busy ? "设置读面读取中…（草稿 / 已应用 / 客户端读回在读取返回前一律未知）" : failure ? `设置读面读取失败（${failure}）；未读取前草稿 / 已应用 / 客户端读回一律未知` : "设置读面未读取；未读取前草稿 / 已应用 / 客户端读回一律未知";
      return `<div class="wb-svc-detail"><p class="wb-muted">${escapeHtml(line)}。只读 model.context_overrides，读一条操作记录、不改配置。${busy ? "" : " " + readBtn("读取设置")}</p>`
        + `</div>`;
    }
    let ctx: ContextView | null;
    try {
      ctx = normalizeContexts(raw).find(r => `${r.source}|${r.modelId}` === key) ?? null;
    } catch {
      return `<div class="wb-svc-detail"><p class="wb-muted">设置读面形状不可识别（未知，不当作没有设置）。</p>`
        + (busy ? "" : readBtn("重试读取")) + `</div>`;
    }
    if (!ctx) {
      return `<div class="wb-svc-detail"><p class="wb-muted">设置读面未返回该模型的记录（未知，不当作没有设置；该模型可能不在当前目录）。</p>`
        + (busy ? "" : readBtn("重新读取")) + `</div>`;
    }
    const settingsText = (s: ModelSettings | null, fallback: string): string => {
      if (!s) return fallback;
      const parts: string[] = [];
      if (s.alias) parts.push(`别名 ${s.alias}`);
      if (s.contextSource) parts.push(`上下文 ${CONTEXT_SOURCE_LABEL[s.contextSource] ?? s.contextSource}${s.contextTokens !== null ? ` ${s.contextTokens.toLocaleString("zh-CN")}` : ""}`);
      if (s.defaultEffort) parts.push(`默认档 ${s.defaultEffort}`);
      return parts.length ? parts.join(" · ") : "空设置（全部为默认）";
    };
    const matchesText = (m: { context: boolean | null; alias: boolean | null; defaultEffort: boolean | null }): string => {
      const parts: string[] = [];
      const word = (v: boolean | null, label: string) =>
        parts.push(v === null ? `${label}无法比较` : v ? `${label}一致` : `${label}不一致`);
      word(m.context, "上下文"); word(m.alias, "别名"); word(m.defaultEffort, "默认档");
      return parts.join(" · ");
    };
    // 客户端读回按后端返回逐条写出：每个客户端一段「消费者 · 状态 · 上下文…」，
    // 段间用「；」分隔（纯文本，不嵌块级元素）；未返回时写「未知」。
    const readbackText = (ctx.clientReadbacks.length
      ? ctx.clientReadbacks.map(r => [r.consumer, CLIENT_READBACK_LABEL[r.state] ?? r.state,
        r.contextTokens !== null ? `上下文 ${r.contextTokens.toLocaleString("zh-CN")}` : "",
        r.alias ? `别名 ${r.alias}` : "",
        r.defaultEffort ? `默认档 ${r.defaultEffort}` : "",
        r.outputCap.value !== null ? `输出上限 ${r.outputCap.value.toLocaleString("zh-CN")}` : "",
        matchesText(r.matchesApplied)].filter(Boolean).join(" · ")).join("；")
      : ctx.clientReadbackState !== "unknown"
        ? [`客户端读回 ${CLIENT_READBACK_LABEL[ctx.clientReadbackState] ?? ctx.clientReadbackState}`,
          ctx.clientContextTokens !== null ? `上下文 ${ctx.clientContextTokens.toLocaleString("zh-CN")}` : "",
          ctx.clientAlias ? `别名 ${ctx.clientAlias}` : "",
          ctx.clientDefaultEffort ? `默认档 ${ctx.clientDefaultEffort}` : "",
          ctx.clientOutputCap.value !== null ? `输出上限 ${ctx.clientOutputCap.value.toLocaleString("zh-CN")}` : "",
          ctx.clientMatchesApplied !== null ? (ctx.clientMatchesApplied ? "与已应用一致" : "与已应用不一致") : ""]
          .filter(Boolean).join(" · ")
        : "客户端读回未知（读面未返回该模型的读回字段）")
      + (ctx.clientReadbackConsumer ? `（读回客户端 ${ctx.clientReadbackConsumer}）` : "");
    // 应用时间的本地格式：解析不出来就留空（不写一个猜的时间）。
    const readbackWhen = (iso: string): string => {
      const t = Date.parse(iso);
      return Number.isFinite(t)
        ? new Date(t).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
    };
    return `<div class="wb-svc-detail"><div class="wb-kv">`
      + `<span>草稿</span><b>${escapeHtml(settingsText(ctx.draft, "无草稿（保存草稿后生成）"))}`
      + `<small>草稿版本 ${ctx.draftVersion !== null ? ctx.draftVersion : "未知"}${ctx.held ? " · 待应用（镜像按 held 抑制该模型的更新）" : ""}</small></b>`
      + `<span>已应用</span><b>${escapeHtml(settingsText(ctx.applied, "无已应用设置"))}`
      + `<small>已应用版本 ${ctx.appliedVersion !== null ? ctx.appliedVersion : "未知"}${ctx.appliedAt ? ` · 应用时间 ${readbackWhen(ctx.appliedAt)}` : ""}</small></b>`
      + `<span>客户端读回</span><b>${escapeHtml(readbackText)}</b>`
      + `<span>有效同步值</span><b>${ctx.effectiveContextTokens !== null ? `${ctx.effectiveContextTokens.toLocaleString("zh-CN")} Token` : "未知"}（来源 ${escapeHtml(CONTEXT_SOURCE_LABEL[ctx.effectiveSource ?? ""] ?? ctx.effectiveSource ?? "未知")}）</b>`
      + `</div>`
      + (busy ? `<p class="wb-muted">设置读面读取中…（以下为上一次读数）</p>` : "")
      + (failure ? `<p class="wb-muted">设置读面读取失败（${escapeHtml(failure)}）${busy ? "" : "；以下为上一次读数。"}</p>` : "")
      + (busy ? "" : readBtn("刷新设置读面"))
      + `</div>`;
  }
  /** 设置草稿表单（保存 / 恢复默认 / 预览 / 应用）：字段值优先取页面会话内
   *  正在编辑的值，其次读面草稿，最后空；应用按钮只在预览绑定当前草稿版本时
   *  可用（预览 token 与 selection 由后端绑定），应用走二次确认。 */
  private modelSettingsFormHtml(key: string, m: CatalogModelRow, efforts: string[],
      effortSrc: (e: string) => string, floor: number | null,
      settings: { state: string; ctx: ContextView | null }): string {
    const at = key.indexOf("|");
    const source = key.slice(0, at), modelId = key.slice(at + 1);
    const draft = settings.ctx?.draft ?? null;
    const alias = this.msAlias.get(key) ?? draft?.alias ?? "";
    const mode = this.msCtx.get(key) ?? draft?.contextSource ?? "declared";
    const tokens = this.msCtxTokens.get(key)
      ?? (draft?.contextTokens !== null && draft?.contextTokens !== undefined ? String(draft.contextTokens) : "");
    const effort = this.msEffort.get(key) ?? draft?.defaultEffort ?? "";
    const n = (v: number) => v.toLocaleString("zh-CN");
    const draftVersion = this.msDraftVersion(key);
    const preview = this.msPreviewFor(key);
    const apply = this.msApplyFor(key);
    const saveBusy = this.actionInFlight("model.settings_save");
    const previewBusy = this.actionInFlight("model.settings_preview");
    const applyBusy = this.actionInFlight("model.settings_apply");
    const saveFields = (): Record<string, unknown> | null => this.msFormFields(key);
    // 保存的并发检查要读面的草稿版本（expected_draft_version）：读面未读取 /
    // 形状不可识别 / 读面没有该模型时按钮按禁用渲染，绝不省略版本检查去
    // 覆盖并发修改；读面读到（含无草稿记录的 0）即可保存。
    const readOk = settings.state === "ok";
    const canSave = saveFields() !== null && !saveBusy && readOk;
    const saveTitle = !readOk
      ? "先读取设置（保存的并发检查需要读面的草稿版本）"
      : "只写草稿（保存≠已应用，不发模型请求）";
    const previewTitle = draftVersion === null
      ? "先保存草稿（预览绑定草稿版本）"
      : `预览绑定草稿版本 ${draftVersion}；不发模型请求`;
    const applyTitle = !preview
      ? "先预览设置变更（应用必须带回预览绑定的 selection 与 preview_token）"
      : `应用该预览（草稿版本 ${preview.selection[key] ?? "未知"}）；写客户端配置，ZCode 运行中：安装版已准入时热写，未准入时整体延期`;
    const previewHtml = preview
      ? `<div class="wb-svc-detail"><h4>设置预览（${escapeHtml(preview.status || "—")} · 未写客户端配置）</h4>`
        + `<p class="wb-muted">应用会带回这份 selection 与 preview_token（后端绑定草稿版本）；预览后再改草稿会过期，需重新预览。${escapeHtml(preview.note)}</p>`
        + (preview.changes.length
          ? `<div class="wb-sync-diff"><strong>重算设置变更（${preview.changes.length} 项 · 真实写入需「应用到客户端」二次确认）</strong><ul>${preview.changes.map(c => `<li>${escapeHtml(this.msChangeLine(c))}</li>`).join("")}</ul></div>`
          : "")
        + (preview.plan
          ? `<div class="wb-sync-diff"><strong>发布计划（后端重算）</strong><ul>${preview.plan.map(p => `<li>${escapeHtml(p)}</li>`).join("")}</ul></div>`
          : `<p class="wb-muted">发布计划未知：响应未返回 plan 明细（不把它当成「无配置变化」）。</p>`)
        + (preview.notices.length
          ? `<div class="wb-sync-diff"><strong>覆盖生效通知</strong><ul>${preview.notices.map(p => `<li>${escapeHtml(p)}</li>`).join("")}</ul></div>`
          : "")
        + (preview.warnings.length
          ? `<div class="wb-sync-diff"><strong>来源警告</strong><ul>${preview.warnings.map(p => `<li>${escapeHtml(p)}</li>`).join("")}</ul></div>`
          : "")
        + `</div>`
      : "";
    const applyHtml = apply
      ? apply.deferred
        ? `<div class="wb-svc-detail"><h4>应用到客户端 · 整体延期</h4><p class="wb-stale-warn">客户端写集合整体延期：新草稿保留，原已应用值和所有客户端未变更（按后端返回）。</p>`
          + (apply.note ? `<p class="wb-muted">${escapeHtml(apply.note)}</p>` : "")
          + (apply.pendingDetails.length
            ? `<div class="wb-sync-diff"><strong>延期明细</strong><ul>${apply.pendingDetails.map(d => `<li>${escapeHtml(String(d.description ?? d.kind ?? "").slice(0, 200))}</li>`).join("")}</ul></div>`
            : "")
          + `</div>`
        : apply.applied
          ? `<div class="wb-svc-detail"><h4>应用到客户端 · 已应用</h4><p class="wb-muted">草稿已转已应用、草稿清空${apply.status ? `（status=${escapeHtml(apply.status)}）` : ""}。${apply.note ? escapeHtml(apply.note) : ""}</p></div>`
          : `<div class="wb-svc-detail"><h4>应用到客户端 · 无变更</h4><p class="wb-muted">${escapeHtml(apply.note || "无可应用草稿（幂等成功，零写入零请求）")}</p></div>`
      : "";
    return `<fieldset class="wb-model-settings-form"><legend class="wb-sr-only">设置草稿（保存只写草稿；应用前必须先预览并确认）</legend>`
      + `<label><span>别名</span><input type="text" data-wb-ms-field="alias" data-wb-ms-key="${escapeHtml(key)}" placeholder="显示名，不改真实模型 id" aria-label="别名" value="${escapeHtml(alias)}"/></label>`
      + `<div class="wb-model-settings-ctx" role="radiogroup" aria-label="上下文"><span>上下文</span>`
      + `<label><input type="radio" name="wb-ms-ctx-${escapeHtml(key)}" data-wb-ms-field="ctx" data-wb-ms-key="${escapeHtml(key)}" value="declared"${mode === "declared" ? " checked" : ""}/>使用声明值${m.context !== null && m.context !== undefined ? ` ${n(m.context)}` : "（上游未声明）"}</label>`
      + `<label><input type="radio" name="wb-ms-ctx-${escapeHtml(key)}" data-wb-ms-field="ctx" data-wb-ms-key="${escapeHtml(key)}" value="measured"${mode === "measured" ? " checked" : ""}${floor === null ? " disabled" : ""}/>采用已验证下界${floor !== null ? ` ${n(floor)}` : "（尚无，先读取探测证据）"}</label>`
      + `<label><input type="radio" name="wb-ms-ctx-${escapeHtml(key)}" data-wb-ms-field="ctx" data-wb-ms-key="${escapeHtml(key)}" value="manual"${mode === "manual" ? " checked" : ""}/>人工设置 <input type="number" min="1" step="1" max="10000000" data-wb-ms-field="tokens" data-wb-ms-key="${escapeHtml(key)}" aria-label="人工上下文 Token" value="${escapeHtml(tokens)}"${mode === "manual" ? "" : " disabled"}/></label></div>`
      + `<label><span>默认思考档位</span><select data-wb-ms-field="effort" data-wb-ms-key="${escapeHtml(key)}" aria-label="默认思考档位"><option value="">不设置（客户端默认）</option>${efforts.map(e => `<option value="${escapeHtml(e)}"${effort === e ? " selected" : ""}>${escapeHtml(e)}（${escapeHtml(effortSrc(e))}）</option>`).join("")}</select></label>`
      + `<div class="wb-model-ops">`
      + `<button type="button" class="wb-btn wb-btn-sm" data-wb-ms-action="save" data-wb-ms-key="${escapeHtml(key)}"${canSave ? "" : " disabled"} title="${escapeHtml(saveTitle)}">保存草稿</button>`
      + `<button type="button" class="wb-btn wb-btn-sm" data-wb-ms-action="restore" data-wb-ms-key="${escapeHtml(key)}"${canSave ? "" : " disabled"} title="${escapeHtml(!readOk ? saveTitle : "把别名 / 上下文 / 默认档的草稿恢复为默认（保存后仍需预览与确认）")}">恢复默认</button>`
      + `<button type="button" class="wb-btn wb-btn-sm" data-wb-ms-action="preview" data-wb-ms-key="${escapeHtml(key)}"${draftVersion === null || previewBusy ? " disabled" : ""} title="${escapeHtml(previewTitle)}">预览设置变更</button>`
      + `<span class="wb-write-group" style="${WB_WRITE_GROUP_STYLE}"><span class="wb-write-mark wb-write-mark-write" title="写·改配置：真实写入客户端配置，需二次确认">写·改配置</span><button type="button" class="wb-action-primary" data-wb-ms-action="apply" data-wb-ms-key="${escapeHtml(key)}"${preview && !applyBusy ? "" : " disabled"} title="${escapeHtml(applyTitle)}">应用到客户端</button></span>`
      + `</div></fieldset>`
      + previewHtml
      + applyHtml;
  }
  /** 保存草稿的表单字段（显式全量提交）：别名空 = null（恢复目录显示名）、
   *  contextSource=declared 时 contextTokens 必须为 null、manual/measured 必须给出
   *  1..10000000 的正整数、默认档空 = null（恢复上游默认）。字段读不出来 /
   *  组合不合法时返回 null（按钮按禁用渲染，绝不提交伪造值）。 */
  private msFormFields(key: string): Record<string, unknown> | null {
    const at = key.indexOf("|");
    if (at <= 0) return null;
    const draft = this.msContextState(key).ctx?.draft ?? null;
    const alias = (this.msAlias.get(key) ?? draft?.alias ?? "").trim();
    const mode = this.msCtx.get(key) ?? draft?.contextSource ?? "declared";
    const tokensRaw = (this.msCtxTokens.get(key)
      ?? (draft?.contextTokens !== null && draft?.contextTokens !== undefined ? String(draft.contextTokens) : "")).trim();
    const effort = (this.msEffort.get(key) ?? draft?.defaultEffort ?? "").trim();
    const fields: Record<string, unknown> = {
      alias: alias ? alias.slice(0, 128) : null,
      defaultEffort: effort ? effort.slice(0, 32) : null,
    };
    if (mode === "declared") {
      fields.contextSource = "declared";
      fields.contextTokens = null;
    } else if (mode === "measured") {
      const floor = this.msEvidenceFloor(key);
      if (floor === null) return null;
      fields.contextSource = "measured";
      fields.contextTokens = floor;
    } else if (mode === "manual") {
      const tokens = Number(tokensRaw);
      if (!Number.isSafeInteger(tokens) || tokens < 1 || tokens > 10_000_000) return null;
      fields.contextSource = "manual";
      fields.contextTokens = tokens;
    } else {
      return null;
    }
    return fields;
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
      const ev = normalizeContextEvidence(raw).find(r => `${r.source}|${r.modelId}` === key);
      const floor = ev?.verifiedTokenFloor ?? null;
      return floor !== null && floor >= 1 ? floor : null;
    } catch { return null; }
  }
  /** 设置读面里该模型的草稿版本：读面优先，其次最近一次保存结果的
   *  draft_version（保存后读面尚未刷新时用）；都没有 = null（先保存草稿）。 */
  private msDraftVersion(key: string): number | null {
    const read = this.msContextState(key);
    if (read.state === "ok" && read.ctx?.draftVersion !== null && read.ctx?.draftVersion !== undefined) {
      return read.ctx.draftVersion;
    }
    const saved = this.actionResults["model.settings_save"];
    if (saved && `${saved.source ?? ""}|${saved.model_id ?? ""}` === key
        && typeof saved.draft_version === "number" && Number.isSafeInteger(saved.draft_version)) {
      return saved.draft_version;
    }
    return null;
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
  /** 最近一次设置预览中属于该模型的那一份：selection 只含本模型、版本与当前
   *  草稿一致、preview_token 形状合法时才算数（token 与 selection 由后端绑定）；
   *  否则返回 null（不冒充本次应用的预览）。 */
  private msPreviewFor(key: string): { selection: Record<string, number>; token: string; changes: Record<string, unknown>[]; plan: string[] | null; status: string; warnings: string[]; notices: string[]; note: string } | null {
    const preview = this.actionResults["model.settings_preview"];
    if (!preview) return null;
    const selection = preview.selection as Record<string, unknown> | undefined;
    if (!selection || typeof selection !== "object" || Array.isArray(selection)) return null;
    const keys = Object.keys(selection);
    if (keys.length !== 1 || keys[0] !== key) return null;
    const version = selection[key];
    if (typeof version !== "number" || !Number.isSafeInteger(version) || version < 1) return null;
    if (version !== this.msDraftVersion(key)) return null;
    const token = typeof preview.preview_token === "string" ? preview.preview_token : "";
    if (!/^[0-9a-f]{64}$/i.test(token)) return null;
    const changes = Array.isArray(preview.changes)
      ? preview.changes.filter((c): c is Record<string, unknown> => !!c && typeof c === "object" && !Array.isArray(c)) : [];
    const planRaw = preview.plan;
    const plan = Array.isArray(planRaw)
      ? planRaw.map(String) : null;
    return {
      selection: { [key]: version }, token, changes, plan,
      status: typeof preview.status === "string" ? preview.status : "",
      warnings: Array.isArray(preview.warnings) ? preview.warnings.map(String) : [],
      notices: Array.isArray(preview.overrideNotices) ? preview.overrideNotices.map(String) : [],
      note: typeof preview.note === "string" ? preview.note.slice(0, 200) : "",
    };
  }
  /** 最近一次应用到客户端的结果里属于该模型的那一份：apply 结果的 selection
   *  是后端回的提交选择；缺 selection（无可应用草稿的幂等结果）时不冒充。 */
  private msApplyFor(key: string): { applied: boolean; deferred: boolean; status: string; note: string; pendingDetails: Record<string, unknown>[] } | null {
    const apply = this.actionResults["model.settings_apply"];
    if (!apply) return null;
    const selection = apply.selection as Record<string, unknown> | undefined;
    if (!selection || typeof selection !== "object" || Array.isArray(selection) || !(key in selection)) return null;
    const applied = Array.isArray(apply.applied) ? apply.applied.map(String) : [];
    const deferred = Array.isArray(apply.deferred) ? apply.deferred.map(String) : [];
    return {
      applied: applied.includes(key), deferred: deferred.includes(key),
      status: typeof apply.status === "string" ? apply.status : "",
      note: typeof apply.note === "string" ? apply.note.slice(0, 200) : "",
      pendingDetails: Array.isArray(apply.pendingDetails)
        ? apply.pendingDetails.filter((d): d is Record<string, unknown> => !!d && typeof d === "object" && !Array.isArray(d)) : [],
    };
  }
  /** 后端设置变更行 → 一句人话：before / draft 两侧的别名、上下文、默认档
   *  逐字段写清；读不出来的字段不写。 */
  private msChangeLine(row: Record<string, unknown>): string {
    const side = (value: unknown): string => {
      if (!value || typeof value !== "object" || Array.isArray(value)) return "无（未设置）";
      const s = value as Record<string, unknown>;
      const parts: string[] = [];
      if (typeof s.alias === "string" && s.alias) parts.push(`别名 ${s.alias}`);
      if (typeof s.contextSource === "string" && s.contextSource) {
        const tokens = typeof s.contextTokens === "number" ? ` ${s.contextTokens.toLocaleString("zh-CN")}` : "";
        parts.push(`上下文 ${CONTEXT_SOURCE_LABEL[s.contextSource] ?? s.contextSource}${tokens}`);
      }
      if (typeof s.defaultEffort === "string" && s.defaultEffort) parts.push(`默认档 ${s.defaultEffort}`);
      return parts.length ? parts.join(" · ") : "空设置（全部为默认）";
    };
    const model = typeof row.model_id === "string" ? row.model_id : "";
    return `${model ? `${model}：` : ""}${side(row.before)} → ${side(row.draft)}`;
  }
  /** 设置读面（model.context_overrides）的读取状态：未读取 / 读取中 / 形状不可识别 /
   *  读到（读面覆盖目录模型与存量设置的并集）/ 读面未返回该模型。 */
  private msContextState(key: string): { state: "unread" | "bad" | "none" | "ok"; ctx: ContextView | null } {
    const raw = this.actionResults["model.context_overrides"];
    if (raw === undefined) return { state: "unread", ctx: null };
    try {
      const ctx = normalizeContexts(raw).find(r => `${r.source}|${r.modelId}` === key) ?? null;
      return { state: ctx ? "ok" : "none", ctx };
    } catch { return { state: "bad", ctx: null }; }
  }
  /** 某个动作是否有在途提交（准备 / 排队 / 执行中）：重复点击不产生第二次提交，
   *  按钮在途时禁用；与共享车道用同一套反馈条目，不另建在途表。 */
  private actionInFlight(actionName: string): boolean {
    for (const entry of this.feedbackEntries.values()) {
      if (entry.action === actionName
          && (entry.state === "preparing" || entry.state === "queued" || entry.state === "running")) return true;
    }
    return false;
  }
  /** 设置表单字段的输入镜像（页面会话内）：文本 / 数字 / 单选 / 下拉都按
   *  `source|model_id` 记值；上下文单选改变时重绘（切换人工输入框的可用态），
   *  其余字段不重绘（避免输入中丢焦点）。 */
  private msFieldInput(t: HTMLInputElement | HTMLSelectElement) {
    const key = t.dataset.wbMsKey ?? "";
    if (!key) return;
    const field = t.dataset.wbMsField;
    if (field === "alias") this.msAlias.set(key, t.value);
    else if (field === "tokens") this.msCtxTokens.set(key, t.value);
    else if (field === "effort") this.msEffort.set(key, t.value);
    else if (field === "ctx") {
      this.msCtx.set(key, t.value);
      this.renderPanel();
    }
  }
  private effortAdvancedHtml(src: string, m: CatalogModelRow,
      probe?: Record<string, unknown>): string {
    const key = `${src}|${m.id}`;
    const verified = this.verifiedEfforts(probe);
    const status = typeof probe?.status === "string" ? probe.status : "";
    const catalog = (m.efforts ?? []).join("/");
    const restored = this.effortRestored.has(key);
    const useVerified = !!verified && !restored;
    const effective = useVerified && verified ? verified.join("/") : catalog;
    const sourceLabel = this.effortSourceLabel(m.efforts_source);
    const row = (name: string, value: string, note = "") =>
      `<span>${escapeHtml(name)}</span><b>${escapeHtml(value || "—")}${note ? ` <small class="wb-muted">（${escapeHtml(note)}）</small>` : ""}</b>`;
    // P2 (2026-09-24): only the sources the capability probe actually

    // entry — promising it on a source that is never probed would be a claim
    // the backend cannot honour.
    const probeable = src === "um";
    return `<div class="wb-svc-detail"><h4>推理档高级覆盖 · ${escapeHtml(m.id)}（${escapeHtml(this.sourceLabel(src))}）</h4>`
      + `<div class="wb-kv">`
      + row("目录值（来源）", catalog, sourceLabel)
      + row("能力探测已验证值", verified ? verified.join("/") : "—",
        verified ? "计费探测记录" : "尚无已验证记录")
      + row("当前生效显示", effective,
        useVerified ? "能力探测已验证" : sourceLabel)
      + row("默认档位", String(m.default_effort ?? ""),
        m.default_effort ? "目录元数据" : "")
      + `</div>`
      + `<div class="wb-model-ops">`
      + `<button type="button" class="wb-btn wb-btn-sm" data-wb-effort-action="use-source" data-wb-effort-key="${escapeHtml(key)}"${restored || !verified ? " disabled" : ""} title="以目录值显示（仅本页；不改上游目录、不写同步配置）">以目录值显示</button>`
      + `<button type="button" class="wb-btn wb-btn-sm" data-wb-effort-action="use-verified" data-wb-effort-key="${escapeHtml(key)}"${useVerified || !verified ? " disabled" : ""} title="以能力探测已验证值显示（仅本页；不改上游目录、不写同步配置）">以已验证值显示</button>`
      + (probeable
        ? `<button type="button" class="wb-btn wb-btn-sm" data-wb-effort-action="validate" title="到「测速与探测」分区发起能力探测（真实计费请求，沿用原有二次确认与预算门）">校验档位…</button>`
        : "")
      + `</div>`
      + `<p class="wb-muted">覆盖仅作用于本页显示：不改上游目录、不写同步配置、不写探测记录。${probeable ? "校验是单独确认的计费能力探测，独立执行入口等后端提供后开放；探测验证成功后本行默认显示已验证值，可随时用「以目录值显示」恢复。" : `该来源（${escapeHtml(this.sourceLabel(src))}）不参与能力探测（仅支持已接入且具备探测入口的来源），因此本行没有校验入口。`}${status === "pending" ? "当前探测记录：待探测。" : ""}</p>`
      + `</div>`;
  }
  /** The probe-verified effort set for one model, or null. Only a
   *  "verified" record carries evidence; any other status is not a tier
   *  verdict and must never be displayed as one. */
  private verifiedEfforts(probe?: Record<string, unknown>): string[] | null {
    if (!probe || probe.status !== "verified") return null;
    const efforts = Array.isArray(probe.efforts) ? probe.efforts as unknown[] : [];
    const names = efforts.filter(e => typeof e === "string" && e) as string[];
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
    const ttfb = typeof speed.ttfb_s === "number" ? `${speed.ttfb_s.toFixed(2)}s` : "—";
    const total = typeof speed.total_s === "number" ? `${speed.total_s.toFixed(1)}s` : "—";
    return `${status} · 首字 ${ttfb} / 总 ${total}`;
  }
  /** One catalog speed cell (R11-9): the same markup the row template and
   *  the targeted patch both use, so an in-place update can never drift
   *  from a full render. */
  private speedCellHtml(src: string, modelId: string): string {
    const actionSource = SPEEDTEST_SOURCE[src] ?? src;
    const busy = this.speedBusy === `${actionSource}|${modelId}`;
    const result = this.speedResults.get(`${actionSource}|${modelId}`);
    return `<button type="button" class="wb-btn wb-btn-sm" data-wb-model-action="model.speedtest" data-speed-source="${escapeHtml(actionSource)}" data-speed-model="${escapeHtml(modelId)}"${busy ? " disabled" : ""}>测速</button>`
      + (result ? `<small class="wb-speed-cell">${escapeHtml(this.speedResultText(result))}</small>` : "");
  }
  /** R11-9: patch ONE catalog speed cell in place after its speedtest
   *  reaches a terminal state — no full-lane rebuild, so search filters,
   *  scroll position and the other rows stay untouched. The lane's
   *  op-status chip (if present) is patched too so the terminal summary
   *  does not go stale until the next natural repaint. */
  private patchSpeedCell(source: string, model: string) {
    const actionSource = SPEEDTEST_SOURCE[source] ?? source;
    const cell = this.root.querySelector<HTMLElement>(
      `[data-speed-cell="${CSS.escape(`${actionSource}|${model}`)}"]`);
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
    if (serviceId === 0) return `<p class="wb-quota-note">当前接入未提供可信账户余额接口；本机用量与参考费用请看「费用」。</p>`;
    return "";
  }
  /** 账号车道首屏（维护者 2026-09-29 · Plan-Revision 9 逐页落点「账号」）：UM
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
    return cls === "ok" ? "ok" : cls === "danger" || cls === "warn" ? "attention" : "unknown";
  }
  /** Chains in the sidebar order; ids no longer served drop out, new ones append. */
  private gatewayOrdered(): ServiceRow[] {
    const byId = new Map(CHAINS.map(s => [s.id, s] as const));
    const seen = new Set<number>();
    const out: ServiceRow[] = [];
    for (const id of this.gwOrder) {
      const s = byId.get(id);
      if (s && !seen.has(id)) { seen.add(id); out.push(s); }
    }
    for (const s of CHAINS) if (!seen.has(s.id)) out.push(s);
    return out;
  }
  private gatewayVisible(): ServiceRow[] {
    const q = this.gwQuery.trim().toLowerCase();
    return this.gatewayOrdered().filter(s => (!q || s.name.toLowerCase().includes(q) || s.port.includes(q))
      && (this.gwAuth === "all" || this.gatewayAuthBucket(s) === this.gwAuth));
  }
  /** Move `id` to `index` among the VISIBLE rows. Hidden rows keep their slots in
   *  the full order: the visible ids are re-dealt into the positions they already
   *  occupy, so a filtered move never collapses the full order onto the subset. */
  private gatewayMove(id: number, index: number): boolean {
    const full = this.gatewayOrdered().map(s => s.id);
    const visible = this.gatewayVisible().map(s => s.id);
    const from = visible.indexOf(id);
    const to = Math.max(0, Math.min(visible.length - 1, index));
    if (from < 0 || from === to) return false;
    const next = visible.slice();
    next.splice(from, 1);
    next.splice(to, 0, id);
    // The delivered merge keeps hidden gateways in their exact slots; ids are the
    // stable service identities, never list positions.
    this.gwOrder = reorderGatewaySubset(full.map(String), next.map(String)).map(Number);
    const name = serviceById(id)?.name ?? "";
    this.gwAnnounce = `已将 ${name} 移到第 ${to + 1} 位`;
    this.persistGatewayOrder();
    return true;
  }
  /** Startup read of the browser-local order preference. The module re-coordinates
   *  the stored sequence with the available set and reports its own status; the page
   *  maps the effective order back and states the fact instead of guessing. */
  private loadGatewayOrder(): void {
    const ids = CHAINS.map(s => String(s.id));
    let prefs: GatewayOrderPreferences | null = null;
    try { prefs = new GatewayOrderPreferences({ storage: localStorage, namespace: GW_ORDER_NAMESPACE }); }
    catch { prefs = null; }
    this.gwOrderPrefs = prefs;
    if (!prefs) { this.gwOrderNote = "顺序只在本页有效：本浏览器未记住顺序（无法访问本浏览器存储）。"; return; }
    const read = prefs.read(ids);
    const known = new Set(ids);
    const order: number[] = [];
    for (const id of read.order) if (known.has(id)) { const n = Number(id); if (!order.includes(n)) order.push(n); }
    for (const s of CHAINS) if (!order.includes(s.id)) order.push(s.id);
    this.gwOrder = order;
    this.gwOrderNote = read.status === "default" ? "顺序拖动后会在本浏览器记住（当前为默认顺序）。"
      : read.status === "coordinated" ? "顺序已在本浏览器记住。"
        : `浏览器里保存的顺序未采用（${GW_ORDER_RESET_REASONS[read.status] ?? read.status}），当前按默认顺序；再次拖动会覆盖保存。`;
  }
  /** One full-sequence write after a completed move. A refusal keeps the in-page
   *  order authoritative and is stated as NOT remembered, with its reason. */
  private persistGatewayOrder(): void {
    const available = CHAINS.map(s => String(s.id));
    const order = this.gatewayOrdered().map(s => String(s.id));
    const prefs = this.gwOrderPrefs;
    if (!prefs) { this.gwOrderNote = "顺序只在本页有效：本浏览器未记住顺序（无法访问本浏览器存储）。"; return; }
    try {
      const save = prefs.save(order, available);
      this.gwOrderNote = save.persisted ? "顺序已在本浏览器记住。"
        : `本浏览器未记住顺序（${save.reason === "sequence_too_long" ? "顺序超过上限" : "写入被拒绝"}）；本次顺序只在本页有效。`;
    } catch {
      this.gwOrderNote = "本浏览器未记住顺序（顺序状态异常）；本次顺序只在本页有效。";
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
    if (!this.gwConnService && !this.gwConnReading && !this.gwConnection) return;
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
    const service = typeof id === "number" ? serviceById(id)?.targets?.[0] ?? "" : "";
    if (!service || this.connDemoBuild) {
      this.clearGatewayConnection();
      return;
    }
    if (this.gwConnService === service) return;
    const binding = this.gwConnBinding ?? (this.gwConnBinding = new GatewayConnectionBinding(
      new BackendClient(createFetchTransport({ fetch: (input, init) => consoleFetch(input, init) })),
    ));
    binding.clear();
    this.gwCopyFeedback = null;
    this.gwConnService = service;
    this.gwConnReading = true;
    this.gwConnection = null;
    const seq = ++this.gwConnSeq;
    void binding.select(service).then(view => {
      if (seq !== this.gwConnSeq || this.gwSel !== id) return;
      this.gwConnReading = false;
      if (view) this.gwConnection = view;
      if (this.lane === 1) this.renderPanel();
    }).catch(() => {
      if (seq !== this.gwConnSeq || this.gwSel !== id) return;
      this.gwConnReading = false;
      if (this.lane === 1) this.renderPanel();
    });
  }
  /** Copy one field of the current read result. The module owns the decision:
   *  success only after the clipboard write resolved, every refusal and rejected
   *  write comes back as a failure whose reason never carries the value. */
  private async copySelectedConnectionField(field: "url" | "key"): Promise<void> {
    const id = this.gwSel;
    const s = typeof id === "number" ? serviceById(id) : undefined;
    const service = s?.targets?.[0] ?? "";
    if (!service) return;
    const view = this.gwConnection && this.gwConnection.service === service ? this.gwConnection : null;
    const copyField: GatewayConnectionCopyField = field === "url" ? "baseUrl" : "keyPlaintext";
    const outcome = await copyGatewayConnectionField(view?.result.value ?? null, copyField, navigator.clipboard);
    if (this.gwSel !== id) return;
    this.gwCopyFeedback = { service, field, ok: outcome.ok, reason: outcome.reason ?? undefined };
    this.renderPanel();
  }
  private gatewayWorkspaceHtml(): string {
    const detail = this.gwSel === "models" ? this.gatewayModelsHtml() : this.gatewayDetailHtml(this.gwSel);
    return `<div class="wb-gw" data-wb-gw-view="${this.gwNarrowDetail ? "detail" : "list"}">${this.gatewaySideHtml()}`
      + `<div class="wb-gw-main">${detail}</div></div>`;
  }
  private gatewaySideHtml(): string {
    const ordered = this.gatewayOrdered();
    const visible = this.gatewayVisible();
    const count = (b: "ok" | "attention" | "unknown") => ordered.filter(s => this.gatewayAuthBucket(s) === b).length;
    const chip = (key: typeof this.gwAuth, label: string, n: number) =>
      `<button type="button" class="wb-gw-chip" data-wb-gw-auth="${key}" aria-pressed="${this.gwAuth === key}">${escapeHtml(label)} <span>${n}</span></button>`;
    const allOn = this.gwSel === "models";
    const sel = typeof this.gwSel === "number" ? serviceById(this.gwSel) : undefined;
    const hiddenSel = sel && !visible.some(s => s.id === sel.id)
      ? `<p class="wb-gw-hint">当前详情：${escapeHtml(sel.name)}（已被筛选隐藏，详情不变）</p>` : "";
    const rows = visible.length
      ? visible.map((s, i) => this.gatewayRowHtml(s, i, visible.length)).join("")
      : `<li class="wb-gw-empty">没有符合条件的网关${this.gwQuery ? `（搜索「${escapeHtml(this.gwQuery)}」）` : ""}</li>`;
    return `<aside class="wb-gw-side wb-char" aria-label="网关列表">`
      + `<button type="button" class="wb-md-item wb-gw-all" data-wb-gw-sel="models" aria-pressed="${allOn}">${wbIcon("models")}<span class="wb-md-name">全部模型</span><span class="wb-md-state">上游模型目录 · 设置与 ZCode 同步</span></button>`
      + `<div class="wb-gw-filter"><input type="search" class="wb-gw-q" data-wb-gw-q placeholder="搜索网关名称或端口" aria-label="搜索网关" value="${escapeHtml(this.gwQuery)}"/>`
      + `<div class="wb-gw-chips" role="group" aria-label="按认证状态筛选">${chip("all", "全部", ordered.length)}${chip("ok", "已认证", count("ok"))}${chip("attention", "需处理", count("attention"))}${chip("unknown", "未知", count("unknown"))}</div></div>`
      + hiddenSel
      + `<ol class="wb-gw-list" aria-label="网关（拖动右侧把手，或在条目上按 Alt+↑ / Alt+↓ 调整顺序）">${rows}</ol>`
      + `<p class="wb-gw-hint">${escapeHtml(this.gwOrderNote)}</p>`
      + `<p class="wb-sr-only" aria-live="polite" data-wb-gw-announce>${escapeHtml(this.gwAnnounce)}</p>`
      + `</aside>`;
  }
  private gatewayRowHtml(s: ServiceRow, index: number, count: number): string {
    const on = this.gwSel === s.id;
    const login = this.accountLoginFact(s);
    const pending = this.svcPendingText(s);
    const base = `${s.name} · 端口 ${s.port}`;
    return `<li class="wb-gw-row${on ? " wb-gw-row-on" : ""}" data-wb-gw-row="${s.id}">`
      + `<button type="button" class="wb-md-item wb-gw-item" data-wb-gw-sel="${s.id}" data-wb-probe="${s.id}" data-wb-base-title="${escapeHtml(base)}" aria-pressed="${on}" title="${escapeHtml(`${base} · ${this.probeEvidence(s.id)}`)}">`
      + `<i class="wb-dot wb-dot-${this.dotState(s.id)}" aria-hidden="true"></i>`
      + `<span class="wb-md-name">${escapeHtml(s.name)}</span>`
      + `<span class="wb-md-state"><span class="wb-badge wb-badge-${login.badge.cls}">${escapeHtml(login.badge.text)}</span><span class="wb-md-port">:${s.port}</span>${pending ? `<span class="wb-md-pending">${escapeHtml(pending)}</span>` : ""}</span>`
      + `</button>`
      + `<span class="wb-gw-move">`
      + `<button type="button" class="wb-gw-step" data-wb-gw-move="${s.id}" data-wb-gw-dir="-1" aria-label="上移 ${escapeHtml(s.name)}"${index === 0 ? " disabled" : ""}><i aria-hidden="true"></i></button>`
      + `<span class="wb-gw-handle" data-wb-gw-handle="${s.id}" title="拖动调整顺序" aria-hidden="true"><i></i><i></i><i></i></span>`
      + `<button type="button" class="wb-gw-step wb-gw-step-down" data-wb-gw-move="${s.id}" data-wb-gw-dir="1" aria-label="下移 ${escapeHtml(s.name)}"${index === count - 1 ? " disabled" : ""}><i aria-hidden="true"></i></button>`
      + `</span></li>`;
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
    const body = this.gwTab === "models"
      ? this.gatewayModelsTabHtml(s)
      : `<div class="wb-block wb-gw-facts"><dl class="wb-account-facts">`
        + fact("登录状态", login, "login")
        + fact("可用额度", this.accountQuotaFact(s), "quota")
        + fact(expiry.label, expiry, "expiry")
        + fact("下一主动作", this.accountNextFact(s), "next")
        + `</dl></div>`
        + this.gatewayConnectionHtml(s)
        + `<div class="wb-char wb-gw-manage">${this.serviceDetailHtml(s.id, "workspace")}</div>`
        + `<div class="wb-block wb-gw-more">${this.accountCardFoldHtml(s)}</div>`;
    return `<section class="wb-gw-detail" data-wb-account="${s.id}" aria-label="${escapeHtml(s.name)}">`
      + `<div class="wb-gw-head"><button type="button" class="wb-btn wb-btn-sm wb-gw-back" data-wb-gw-back>← 网关列表</button>`
      + `<h3 tabindex="-1">${escapeHtml(s.name)}</h3><span class="wb-badge wb-badge-${runtime.cls}">${escapeHtml(runtime.text)}</span>`
      + `<span class="wb-badge wb-badge-${login.badge.cls}" title="${escapeHtml(login.title)}">${escapeHtml(login.badge.text)}</span><span class="wb-port">端口 ${s.port}</span></div>`
      + `<div class="wb-section-tabs" role="group" aria-label="${escapeHtml(s.name)} 的内容">${tab("account", "账号")}${tab("models", "模型")}</div>`
      + body
      + `</section>`;
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
      return `<div class="wb-empty-card wb-gw-pending"><strong>本网关的模型来源关联未接线</strong>`
        + `<small>后端服务身份「${escapeHtml(service || "未提供")}」不在已登记的服务 ↔ 模型来源表内（console_speedtest._SOURCE_SERVICE_KEYS 未收录），页面不按端口或名称猜测，所以这里不列出模型，也不提供只同步本网关的入口。可以在「全部模型」里按来源筛选查看与同步。</small>`
        + `<div class="wb-model-ops"><button type="button" class="wb-btn" data-wb-gw-sel="models">打开全部模型</button></div></div>`;
    }
    const pending = this.actionResults["model.sync_pending"];
    const catalog = this.actionResults["model.catalog"];
    const catalogFailure = this.actionResultFailures.get("model.catalog");
    const catalogBusy = this.actionInFlight("model.catalog");
    const preview = this.gwScopedPreview(source);
    const previewBusy = this.actionInFlight("model.sync_preview");
    const applyBusy = this.actionInFlight("model.sync_apply");
    const scope = this.gwScopeParams(source);
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
        .filter((r): r is Record<string, unknown> => !!r && typeof r === "object" && !Array.isArray(r))
        .filter(r => r.source === source);
      bindings = rows.length
        ? `<div class="wb-sync-diff"><strong>客户端绑定（后端只读字段 bindings）</strong><ul>${rows.map(r => `<li>${escapeHtml(String(r.consumer ?? "未知"))} · 供应商 ${escapeHtml(String(r.provider ?? "未知"))}</li>`).join("")}</ul></div>`
        : `<p class="wb-muted">读面 bindings 里没有本来源的行（${escapeHtml(sourceName)} 可能不在当前启用绑定内）；不当作没有绑定。</p>`;
    }
    // 目录读取是UM只读读取（后端固定返回全部源），本页只过滤展示本来源；
    // 读取失败 / 读取中按实测状态写（未读取由上面的空卡片说明，未读取不等于没有模型）。
    const catalogState = catalog === undefined && (catalogBusy || catalogFailure)
      ? (catalogBusy ? `<p class="wb-muted">本来源目录读取中…（只读 model.catalog）</p>`
        : `<p class="wb-muted">本来源目录读取失败（${escapeHtml(catalogFailure!)}）；${this.actionResults["model.catalog"] === undefined ? "本次没有可用的上一次读数，下方是失败说明与重试入口。" : "下方表格仍为上一次读数。"}</p>`)
      : "";
    const previewHtml = preview
      ? `<div class="wb-svc-detail"><h4>本来源同步预览（${escapeHtml(preview.status)} · 未写入任何配置）${preview.stale ? `<span class="wb-count-chip wb-stale-chip">已过期（应用后目录已变）</span>` : ""}</h4>`
        + `<p class="wb-muted">本次预览范围：${escapeHtml(this.scopeSummary(this.previewScopeParams))}（范围参数与「应用本来源同步」共用同一构造器与编码）。</p>`
        + (preview.planKnown
          ? (preview.changes.length
            ? `<div class="wb-sync-diff"><strong>重算同步计划（${preview.changes.length} 项 · 真实写入需「应用本来源同步」二次确认）</strong><ul>${preview.changes.map(d => `<li>${escapeHtml(String(d))}</li>`).join("")}</ul></div>`
            : `<p class="wb-muted">按实时目录与受管配置重算：无配置变化。</p>`)
          : `<p class="wb-muted">重算同步计划未知：响应未返回 plan 明细（不把它当成「无配置变化」）。</p>`)
        + (preview.warnings.length
          ? `<div class="wb-sync-diff"><strong>来源警告</strong><ul>${preview.warnings.map(w => `<li>${escapeHtml(String(w))}</li>`).join("")}</ul></div>`
          : "")
        + `</div>`
      : (this.previewScopeParams
        ? `<p class="wb-muted">最近一次同步预览的范围不是本来源（${escapeHtml(this.scopeSummary(this.previewScopeParams))}）——这里不冒充本来源结果；点「预览本来源变更」重算。</p>`
        : `<p class="wb-muted">尚未预览本来源变更：点「预览本来源变更」按显式范围重算（不发模型请求）。</p>`);
    return `<div class="wb-gw-models-src">`
      + `<div class="wb-svc-detail"><h4>模型来源关联 · ${escapeHtml(sourceName)}</h4>`
      + `<p class="wb-muted">网关服务身份 <code>${escapeHtml(service)}</code> ↔ 模型来源 <code>${escapeHtml(source)}</code>（与测速、连接读面同一服务身份表；目录行与同步计划全部来自后端返回，页面不推算）。</p>`
      + bindings
      + `</div>`
      + `<div class="wb-section-toolbar"><p class="wb-muted">本来源目录只读；读取失败只进警告，空目录不代表账号额度为零。</p><div class="wb-model-ops"><button class="wb-btn wb-btn-sm" data-wb-model-action="model.catalog" aria-label="刷新本来源目录"${catalogBusy ? " disabled" : ""}>${wbIcon("refresh")}刷新本来源目录</button></div></div>`
      + catalogState
      + this.modelCatalogHtml(pending, source)
      + `<div class="wb-sync-toolbar"><div><h4>本来源同步（${escapeHtml(sourceName)}）</h4><p class="wb-muted">范围参数显式：scope_mode=${escapeHtml(String(scope.scope_mode ?? ""))} · sources=${escapeHtml(String(scope.sources ?? ""))}（与应用共用同一构造器与编码）</p></div><div class="wb-model-ops"><button data-wb-gw-sync="preview" data-wb-gw-source="${escapeHtml(source)}"${previewBusy ? " disabled" : ""}>预览本来源变更</button><span class="wb-write-group" style="${WB_WRITE_GROUP_STYLE}"><span class="wb-write-mark wb-write-mark-write" title="写·改配置：真实写入客户端配置，需二次确认">写·改配置</span><button class="wb-action-primary" data-wb-gw-sync="apply" data-wb-gw-source="${escapeHtml(source)}"${applyBusy ? " disabled" : ""}>应用本来源同步…</button></span></div></div>`
      + `<p class="wb-muted">预览与应用共用同一份范围参数；应用终态后的自动回读按同一范围发起（后端读口当前不接收范围参数，回读是全局概览，见「全部模型」视图）。</p>`
      + previewHtml
      + `</div>`;
  }
private gwScopeParams(source: string): Record<string, unknown> { if (source !== "um") throw new Error("公开版本只支持当前 chat2api 上游的模型"); return { scope_mode: "selected", sources: JSON.stringify(["um"]), consumers: JSON.stringify(["ZCode"]) }; }
  /** 最近一次同步预览中属于本来源的那一份：提交时快照的来源集合等于 [source]
   *  时才算数（与应用共用同一范围构造器）；否则返回 null，不冒充本来源结果。 */
  private gwScopedPreview(source: string): { status: string; planKnown: boolean; changes: string[]; warnings: string[]; stale: boolean } | null {
    const preview = this.actionResults["model.sync_preview"];
    if (!preview) return null;
    if (this.scopeSignature(this.previewScopeParams) !== this.scopeSignature(this.gwScopeParams(source))) return null;
    const plan = (preview.preview ?? {}) as Record<string, unknown>;
    const planKnown = Array.isArray(plan.plan);
    return {
      status: String(plan.status ?? "—"),
      planKnown,
      changes: planKnown ? (plan.plan as unknown[]).map(String) : [],
      warnings: Array.isArray(plan.warnings) ? (plan.warnings as unknown[]).map(String) : [],
      stale: this.previewStale,
    };
  }
  /** The pinned 全部模型 view: the cross-source catalog with its sync tool area. */
  private gatewayModelsHtml(): string {
    return `<section class="wb-gw-detail wb-gw-models" aria-label="全部模型">`
      + `<div class="wb-gw-head"><button type="button" class="wb-btn wb-btn-sm wb-gw-back" data-wb-gw-back>← 网关列表</button><h3 tabindex="-1">全部模型</h3><span class="wb-muted">上游模型目录 · ZCode 同步</span></div>`
      + this.modelLaneHtml()
      + `</section>`;
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
    const view = demo?.view ?? (this.gwConnection && this.gwConnection.service === service ? this.gwConnection : null);
    const result: ReadResult<GatewayConnection> | null = view?.result ?? null;
    const ready = result?.state === "ready" && result.value ? result.value : null;
    const keyPlain = ready && ready.keyState === "present" ? ready.keyPlaintext : null;
    /** A read for THIS gateway is in flight (the binding already cleared the
     *  previous slot, so the block never shows the old gateway's values). */
    const reading = !demo && this.gwConnReading && this.gwConnService === service;
    const copy = (field: "url" | "key", label: string) => {
      if (demo) return `<button type="button" class="wb-btn wb-btn-sm wb-conn-copy" data-wb-conn-copy="${field}" disabled title="演示数据不写剪贴板；复制反馈用上方「复制反馈」预览">${wbIcon("copy")}复制 · 演示停用</button>`;
      const carried = field === "url" ? Boolean(ready?.baseUrl?.trim()) : Boolean(keyPlain?.trim());
      return carried
        ? `<button type="button" class="wb-btn wb-btn-sm wb-conn-copy" data-wb-conn-copy="${field}" title="复制${label}">${wbIcon("copy")}复制</button>`
        : `<button type="button" class="wb-btn wb-btn-sm wb-conn-copy" data-wb-conn-copy="${field}" disabled title="当前读数没有可复制的${label}">${wbIcon("copy")}复制</button>`;
    };
    const value = (text: string, field: "url" | "key", label: string) =>
      `<div class="wb-conn-val"><code>${escapeHtml(text)}</code>${copy(field, label)}</div>`;
    const word = (text: string, cls: "idle" | "warn" | "bad" | "plain") =>
      cls === "plain" ? `<div class="wb-conn-val"><b>${escapeHtml(text)}</b></div>` : `<div class="wb-conn-val"><span class="wb-badge wb-badge-${cls}">${escapeHtml(text)}</span></div>`;
    const row = (label: string, body: string, sub: string) =>
      `<div class="wb-conn-row"><dt>${escapeHtml(label)}</dt><dd>${body}${sub ? `<small>${sub}</small>` : ""}</dd></div>`;
    let rows: string;
    let notes = "";
    if (!result) {
      rows = row("API Base URL", word(reading ? "读取中…" : "尚未读取", "idle"), reading ? "正在读取该网关的连接信息（只读）；页面不按端口猜地址。" : "尚未读取；页面不按端口猜地址。")
        + row("API Key", word(reading ? "读取中…" : "尚未读取", "idle"), reading ? "正在读取该网关的连接信息（只读）。" : "尚未读取，不代表没有 Key。");
    } else if (result.state !== "ready" || !result.value) {
      const why = result.state === "refused" ? `读取被拒绝${result.httpStatus ? `（HTTP ${result.httpStatus}）` : ""}`
        : result.state === "unavailable" ? `后端未提供连接信息读面${result.httpStatus ? `（HTTP ${result.httpStatus}）` : ""}`
          : "读取失败";
      const reason = `${why}${result.reason ? `：${escapeHtml(result.reason)}` : ""}。`;
      rows = row("API Base URL", word(result.state === "unavailable" ? "未提供" : "读取失败", "warn"), reason)
        + row("API Key", word(result.state === "unavailable" ? "未提供" : "读取失败", "warn"), `${reason}不能据此判断有没有 Key。`);
    } else {
      const c = result.value;
      const faces = c.alternateProtocols.map(f => `另有 ${escapeHtml(f.protocol)} ${escapeHtml(f.path)}${f.note ? `（${escapeHtml(f.note)}）` : ""}`).join("；");
      const urlSub = `${escapeHtml(c.protocol)} · 路径 ${escapeHtml(c.protocolPath)}${faces ? `；${faces}` : ""}`;
      const url = c.baseUrl ? value(c.baseUrl, "url", "地址") : word("未知", "warn");
      const source = c.keySource ? `Key 来源 <code>${escapeHtml(c.keySource)}</code>` : "";
      const key = c.keyState === "present" && c.keyPlaintext
        ? [value(c.keyPlaintext, "key", " Key"), [source, "明文显示，请勿截图外传"].filter(Boolean).join(" · ")]
        : c.keyState === "absent"
          ? [word("没有", "plain"), c.auth === "loopback_no_key" ? "本机回环实例未开启入站鉴权，调用时不需要 Key。" : escapeHtml(c.reason)]
          : c.keyState === "needs_key_unconfigured"
            ? [word("未配置（需要 Key）", "warn"), `该网关调用需要 Key，但${c.keySource ? ` <code>${escapeHtml(c.keySource)}</code> ` : "其 Key 来源"}缺席或为空：先补配置，不能当作免鉴权。`]
            : c.keyState === "unreadable"
              ? [word("读取失败", "warn"), `${escapeHtml(c.reason)}${source ? ` · ${source}` : ""}`]
              : [word("未知", "idle"), `${escapeHtml(c.reason)}；不按「没有」处理。`];
      rows = row("API Base URL", url, c.baseUrl ? urlSub : `${escapeHtml(c.reason)}；不从显示名或端口猜算。`)
        + row("API Key", key[0]!, key[1]!);
      const checked = c.checkedEpoch > 0 ? new Date(c.checkedEpoch * 1000).toLocaleString("zh-CN", { hour12: false }) : "未知";
      notes = (c.runtimeHealthy === false ? `<p class="wb-conn-note"><span class="wb-badge wb-badge-warn">网关未运行</span>当前状态 ${escapeHtml(c.runtimeState ?? "未知")}：地址与 Key 仍可查看，但现在调用会失败。</p>` : "")
        + `<p class="wb-muted wb-conn-meta">检查于 ${escapeHtml(checked)}${view?.readAt ? ` · 读取于 ${escapeHtml(new Date(view.readAt).toLocaleTimeString("zh-CN", { hour12: false }))}` : ""}</p>`;
    }
    // Copy outcome: success only after the clipboard write resolved; a failure
    // says why and never reads as success. The live region is always present.
    const fb = demo ? demo.feedback : (this.gwCopyFeedback && this.gwCopyFeedback.service === service ? this.gwCopyFeedback : null);
    const fbText = fb ? `${fb.ok ? `已复制${fb.field === "url" ? " API Base URL" : " API Key"}` : `复制失败：${fb.reason ?? "原因未知"}（未写入剪贴板）`}${demo?.tag ?? ""}` : "";
    const feedback = `<p class="wb-conn-feedback${fb ? (fb.ok ? " wb-conn-ok" : " wb-conn-fail") : ""}" role="status" aria-live="polite">${escapeHtml(fbText)}</p>`;
    const badge = demo ? demo.badge : view ? "" : `<span class="wb-badge wb-badge-idle">${reading ? "读取中" : "尚未读取"}</span>`;
    return `<section class="wb-block wb-gw-conn" aria-label="客户端连接">`
      + `<div class="wb-conn-head"><h4>客户端连接</h4>${badge}<small>客户端调用本网关用的地址与 Key（不是登录页、控制台或上游网站）</small></div>`
      + (demo?.bar ?? "")
      + `<dl class="wb-conn-list">${rows}</dl>${notes}${feedback}</section>`;
  }
  /** The candidate demo's view, labels and state picker for one gateway, or null.
   *  Everything demo-specific sits inside the build-time MODE branch, so a
   *  production build carries none of it. */
  private connectionDemo(service: string): { view: GatewayConnectionView; badge: string; bar: string; tag: string; feedback: { ok: boolean; field: "url" | "key"; reason?: string } | null } | null {
    if (import.meta.env.MODE === "candidate") {
      if (!this.connDemoBuild || !service) return null;
      const states = [["present", "有 Key"], ["absent", "没有"], ["needs_key_unconfigured", "需要但未配置"], ["unreadable", "Key 读取失败"], ["unknown", "未知"], ["long", "超长字符串"], ["stopped", "网关未运行"], ["read_error", "读面读取失败"], ["refused", "读取被拒绝"], ["unavailable", "未提供读面"]] as const;
      return {
        view: this.connDemoBuild(service, this.connDemoState),
        badge: `<span class="wb-badge wb-badge-warn">演示数据（合成，不是真实连接）</span>`,
        bar: `<div class="wb-conn-demo" role="group" aria-label="演示状态（合成数据）"><small>演示状态</small>${states.map(([k, label]) => `<button type="button" class="wb-btn wb-btn-sm" data-wb-conn-demo="${k}" aria-pressed="${this.connDemoState === k}">${label}</button>`).join("")}`
          + `<small>复制反馈</small><button type="button" class="wb-btn wb-btn-sm" data-wb-conn-demo-feedback="ok" aria-pressed="${this.connDemoFeedback === "ok"}">成功</button><button type="button" class="wb-btn wb-btn-sm" data-wb-conn-demo-feedback="fail" aria-pressed="${this.connDemoFeedback === "fail"}">失败</button></div>`,
        tag: "（演示）",
        feedback: this.connDemoFeedback ? { ok: this.connDemoFeedback === "ok", field: "key", reason: "合成：浏览器拒绝写入剪贴板" } : null,
      };
    }
    return null;
  }
  /** Candidate-only (#demo-connection): builds the block's views from the
   *  backend's synthetic connection fixtures. The MODE check is a build-time
   *  constant, so a production build drops this branch and the fixture chunk. */
  private async loadConnectionDemo(): Promise<void> {
    if (import.meta.env.MODE === "candidate") {
      if (location.hash !== "#demo-connection") return;
      const { gatewayConnectionFixtureBody, FIXTURE_CONNECTION_KEY } = await import("./console/backend/fixture.ts");
      const failed = (state: "error" | "refused" | "unavailable", httpStatus: number | null, reason: string): ReadResult<GatewayConnection> =>
        ({ state, value: null, httpStatus, reason });
      this.connDemoBuild = (service, state) => {
        const ready = (body: Record<string, unknown>): ReadResult<GatewayConnection> =>
          ({ state: "ready", value: normalizeGatewayConnection(body, service), httpStatus: 200, reason: null });
        const result = state === "read_error" ? failed("error", null, "合成：网络中断")
          : state === "refused" ? failed("refused", 403, "合成：当前会话无权读取")
            : state === "unavailable" ? failed("unavailable", 404, "合成：该版本后端没有连接读面")
              : state === "long" ? ready(gatewayConnectionFixtureBody("present", {
                service,
                base_url: `http://127.0.0.1:8000/v1/${"synthetic-very-long-path-segment/".repeat(4)}end`,
                key_plaintext: `${FIXTURE_CONNECTION_KEY}-${"synthetic-padding-not-a-real-key-".repeat(5)}end`,
              }))
                : state === "stopped" ? ready(gatewayConnectionFixtureBody("present", { service, runtime_state: "Stopped", runtime_healthy: false }))
                  : ready(gatewayConnectionFixtureBody(state as "present", { service }));
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
      if (next !== "models" && !CHAINS.some(s => s.id === next)) return true;
      if (next !== this.gwSel) { this.gwSel = next; this.gwCopyFeedback = null; this.connDemoFeedback = ""; if (next !== "models") this.gwTab = "account"; }
      this.gwNarrowDetail = true;
      this.syncGatewayConnection();
      if (this.lane !== 1) this.select(1); else this.renderPanel();
      // Side by side, focus stays on the list (like the overview's chain list);
      // when the narrow layout swaps the list for the detail, the detail title
      // takes it so keyboard and reader land on what just opened.
      const side = this.root.querySelector<HTMLElement>(".wb-gw-side");
      if (side && getComputedStyle(side).display === "none") this.root.querySelector<HTMLElement>(".wb-gw-detail h3")?.focus({ preventScroll: true });
      else this.root.querySelector<HTMLElement>(`.wb-gw-side [data-wb-gw-sel="${next}"]`)?.focus({ preventScroll: true });
      return true;
    }
    if (d.wbGwTab === "account" || d.wbGwTab === "models") {
      this.gwTab = d.wbGwTab;
      // 单网关模型页的绑定读面（model.sync_pending 的只读 bindings 字段）：
      // 本页还没读过就按本页的读口读一次（只读直读、不记操作行，与
      // 「全部模型」共用同一份读面；读过就不再重复拉取）。
      if (d.wbGwTab === "models" && this.actionResults["model.sync_pending"] === undefined) {
        void this.fetchModelPending();
      }
      this.renderPanel();
      return true;
    }
    if (d.wbConnDemo !== undefined && this.connDemoBuild) {
      this.connDemoState = d.wbConnDemo;
      this.connDemoFeedback = "";
      this.renderPanel();
      this.root.querySelector<HTMLElement>(`[data-wb-conn-demo="${CSS.escape(d.wbConnDemo)}"]`)?.focus({ preventScroll: true });
      return true;
    }
    if ((d.wbConnDemoFeedback === "ok" || d.wbConnDemoFeedback === "fail") && this.connDemoBuild) {
      this.connDemoFeedback = d.wbConnDemoFeedback;
      this.renderPanel();
      this.root.querySelector<HTMLElement>(`[data-wb-conn-demo-feedback="${d.wbConnDemoFeedback}"]`)?.focus({ preventScroll: true });
      return true;
    }
    if (d.wbConnCopy === "url" || d.wbConnCopy === "key") {
      void this.copySelectedConnectionField(d.wbConnCopy);
      return true;
    }
    if (d.wbGwAuth === "all" || d.wbGwAuth === "ok" || d.wbGwAuth === "attention" || d.wbGwAuth === "unknown") {
      this.gwAuth = d.wbGwAuth;
      this.renderPanel();
      return true;
    }
    if (d.wbGwMove !== undefined) {
      const id = Number(d.wbGwMove);
      const at = this.gatewayVisible().findIndex(s => s.id === id);
      if (at >= 0 && this.gatewayMove(id, at + Number(d.wbGwDir))) {
        this.renderPanel();
        this.root.querySelector<HTMLElement>(`[data-wb-gw-sel="${id}"]`)?.focus();
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
      this.root.querySelector<HTMLElement>(`[data-wb-gw-sel="${sel}"]`)?.focus();
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
    this.root.addEventListener("input", event => {
      const input = (event.target as Element).closest?.<HTMLInputElement>("[data-wb-log-source-q]");
      if (!input) return;
      this.logSourceQuery = input.value.slice(0, 60);
      const caret = input.selectionStart;
      this.renderPanel();
      const fresh = this.root.querySelector<HTMLInputElement>("[data-wb-log-source-q]");
      fresh?.focus(); if (caret !== null) fresh?.setSelectionRange(caret, caret);
    });
    this.root.addEventListener("pointerdown", event => {
      const grip = (event.target as Element).closest?.<HTMLElement>("[data-wb-log-handle]");
      if (grip) this.logDragStart(event, grip.dataset.wbLogHandle!);
    });
    this.root.addEventListener("keydown", event => {
      const item = (event.target as Element).closest?.<HTMLElement>(".wb-log-source-list [data-wb-log-source]");
      if (!item || !event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const visible = [...this.root.querySelectorAll<HTMLElement>("[data-wb-log-source-row]")].map(row => row.dataset.wbLogSourceRow!);
      const at = visible.indexOf(item.dataset.wbLogSource!);
      const next = at + (event.key === "ArrowUp" ? -1 : 1);
      if (at < 0 || next < 0 || next >= visible.length) return;
      [visible[at], visible[next]] = [visible[next]!, visible[at]!];
      this.saveLogOrder(visible); this.renderPanel();
      this.root.querySelector<HTMLElement>(`[data-wb-log-source="${CSS.escape(item.dataset.wbLogSource!)}"]`)?.focus();
    });
    this.root.addEventListener("input", event => {
      const q = (event.target as Element).closest?.<HTMLInputElement>("[data-wb-gw-q]");
      if (!q) return;
      window.clearTimeout(this.gwQTimer);
      this.gwQTimer = window.setTimeout(() => {
        const value = q.value.slice(0, 60);
        if (value === this.gwQuery) return;
        this.gwQuery = value;
        const caret = q.selectionStart;
        this.renderPanel();
        const fresh = this.root.querySelector<HTMLInputElement>("[data-wb-gw-q]");
        if (fresh) { fresh.focus(); if (caret !== null) fresh.setSelectionRange(caret, caret); }
      }, 200);
    });
    this.root.addEventListener("pointerdown", event => {
      const handle = (event.target as Element).closest?.<HTMLElement>("[data-wb-gw-handle]");
      if (handle) this.gatewayDragStart(event, Number(handle.dataset.wbGwHandle));
    });
    this.root.addEventListener("keydown", event => {
      const item = (event.target as Element).closest?.<HTMLElement>(".wb-gw-list [data-wb-gw-sel]");
      if (!item) return;
      const id = Number(item.dataset.wbGwSel);
      const visible = this.gatewayVisible();
      const at = visible.findIndex(s => s.id === id);
      if (at < 0) return;
      if (event.altKey && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
        event.preventDefault();
        if (this.gatewayMove(id, at + (event.key === "ArrowUp" ? -1 : 1))) {
          this.renderPanel();
          this.root.querySelector<HTMLElement>(`[data-wb-gw-sel="${id}"]`)?.focus();
        }
        return;
      }
      if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        event.preventDefault();
        const next = visible[at + (event.key === "ArrowUp" ? -1 : 1)];
        if (next) this.root.querySelector<HTMLElement>(`[data-wb-gw-sel="${next.id}"]`)?.focus();
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
    const row = list?.querySelector<HTMLElement>(`[data-wb-log-source-row="${CSS.escape(id)}"]`);
    if (!list || !row) return;
    event.preventDefault();
    this.logDrag = { pointerId:event.pointerId, startOrder:this.logSourceOrder.slice(), moved:false };
    row.classList.add("wb-gw-dragging");
    const rowsNow = () => [...list.querySelectorAll<HTMLElement>("[data-wb-log-source-row]")];
    const move = (e: PointerEvent) => {
      if (e.pointerId !== this.logDrag?.pointerId) return;
      const before = rowsNow().filter(r => r !== row).find(r => { const b = r.getBoundingClientRect(); return e.clientY < b.top + b.height / 2; });
      if (before && before !== row.nextElementSibling) { list.insertBefore(row, before); this.logDrag.moved = true; }
      else if (!before && row !== list.lastElementChild) { list.appendChild(row); this.logDrag.moved = true; }
      const box = list.getBoundingClientRect();
      if (e.clientY < box.top + 24) list.scrollTop -= 8; else if (e.clientY > box.bottom - 24) list.scrollTop += 8;
    };
    const finish = (commit: boolean) => {
      window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); window.removeEventListener("pointercancel", cancel); window.removeEventListener("keydown", key, true);
      const drag = this.logDrag; this.logDrag = undefined; row.classList.remove("wb-gw-dragging");
      if (drag && commit && drag.moved) this.saveLogOrder(rowsNow().map(r => r.dataset.wbLogSourceRow!));
      else if (drag) { this.logSourceOrder = drag.startOrder; if (!commit) this.logOrderNote = "已取消此次排序，未写入浏览器存储。"; }
      this.gwRepaintDeferred = false; this.renderPanel();
    };
    const up = (e: PointerEvent) => { if (e.pointerId === this.logDrag?.pointerId) finish(true); };
    const cancel = (e: PointerEvent) => { if (e.pointerId === this.logDrag?.pointerId) finish(false); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); } };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", up); window.addEventListener("pointercancel", cancel); window.addEventListener("keydown", key, true);
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
    const rowsNow = () => [...list.querySelectorAll<HTMLElement>("[data-wb-gw-row]")];
    const onMove = (e: PointerEvent) => {
      if (!this.gwDrag || e.pointerId !== this.gwDrag.pointerId) return;
      const others = rowsNow().filter(r => r !== row);
      const before = others.find(r => { const b = r.getBoundingClientRect(); return e.clientY < b.top + b.height / 2; });
      if (before && before !== row.nextElementSibling) { list.insertBefore(row, before); this.gwDrag.moved = true; }
      else if (!before && row !== list.lastElementChild) { list.appendChild(row); this.gwDrag.moved = true; }
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
        const domOrder = rowsNow().map(r => Number(r.dataset.wbGwRow));
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
    const onUp = (e: PointerEvent) => { if (this.gwDrag && e.pointerId === this.gwDrag.pointerId) finish(true); };
    const onCancel = (e: PointerEvent) => { if (this.gwDrag && e.pointerId === this.gwDrag.pointerId) finish(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); } };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onCancel);
    window.addEventListener("keydown", onKey, true);
  }
  /** 登录状态短读数：与 `accountAuthValueHtml` 读同一份真实返回与失败记忆，
   *  只是分成「短值 + 一行时点/原因」；完整句子进 `title`。未读取 / 读取失败 /
   *  未认证 / 已认证 / 不支持查询各自一个词，互不折叠。 */
  private accountLoginFact(s: ServiceRow): AccountFact & { badge: { text: string; cls: string } } {
    const title = `${this.accountAuthPlain(s)}；登录凭据失效时间：上游未提供（额度到期不是登录失效）。`;
    const statusAction = this.statusActionOf(s);
    const action = statusAction?.action;
    if (!action) { const gap = this.statusGap(s); return { text: gap.word, sub: gap.why, title, badge: { text: gap.word === "不支持查询" ? "登录状态未知" : gap.word, cls: "gated" } }; }
    const result = this.actionResults[action];
    const failure = this.actionResultFailures.get(action);
    if (!result) {
      return failure
        ? { text: "读取失败", sub: failure, title, badge: { text: "登录读取失败", cls: "warn" } }
        : { text: "未读取", sub: `点「${statusAction!.label}」只读读取`, title, badge: { text: "登录未读取", cls: "idle" } };
    }
    const at = this.actionCheckedAt(result);
    const stale = failure ? " · 本次刷新失败，为上次值" : "";
    const authenticated = result.authenticated;
    if (typeof authenticated === "boolean") {
      return authenticated
        ? { text: "已认证", sub: `读于 ${at}${stale}`, title, badge: { text: "已认证", cls: failure ? "warn" : "ok" } }
        : { text: "未认证", sub: `该链自报 · 读于 ${at}${stale}`, title, badge: { text: "未认证", cls: "danger" } };
    }
    const word = typeof result.state === "string" && result.state ? `状态 ${result.state}` : "已读取（无状态词）";
    return { text: word, sub: `读于 ${at}${stale}`, title, badge: { text: word, cls: "idle" } };
  }
private accountQuotaFact(s: ServiceRow): AccountFact { return { text: "未知", sub: "当前接入未提供可信余额接口", title: this.accountQuotaPlain(s) }; }
private accountExpiryFact(_s: ServiceRow): AccountFact & { label: string } { return { label: "凭据到期", text: "上游未提供", sub: "", title: "当前接入未提供凭据到期查询接口，不猜算。" }; }
  /** 下一主动作短读数：与动作行里的主按钮同一个判断（`accountNextLogin`）。 */
  private accountNextFact(s: ServiceRow): AccountFact {
    const status = this.statusActionOf(s);
    if (!status) {
      if (s.statusAction) { const gap = this.statusGap(s); return { text: gap.word, sub: gap.why, title: `${gap.word}：${gap.why}；接入前不提供查询按钮（不画死按钮）。` }; }
      return { text: "无账号接口", sub: "可用：只读刷新与「详情」", title: "该链未提供账号管理接口；可用的只有只读刷新与「详情」（不画死按钮）。" };
    }
    const login = this.accountNextLogin(s);
    if (login) return { text: login.label, sub: "本人完成登录 · 先二次确认", title: `本次真实返回为未认证：改用「${login.label}」，由维护者本人完成登录（先二次确认，绝不自动打开窗口）。` };
    return { text: status.label, sub: "只读 · 无副作用", title: `「${status.label}」：经 8096 只读提交，一次点击直达、无副作用。` };
  }
  /** 该链的真实返回说未认证、且该链有登录动作时，下一主动作就是那个登录动作。 */
  private accountNextLogin(s: ServiceRow): { action: string; label: string } | undefined {
    const status = this.statusActionOf(s);
    if (!status || this.actionResults[status.action]?.authenticated !== false) return undefined;
    return (s.manageActions ?? []).find(a => isLoginAction(a.action));
  }
  /** 第一阅读层 · 登录状态：值只来自该链自己的只读查询动作真实返回（`authenticated`
   *  / `state`，绝不由前端推断），检查时点用返回自己的 `checked_epoch`；没有读取过
   *  写「未读取」，读取失败写失败原因并保留上一次真实值，绝不折成「未认证」。 */
  private accountLoginReadingHtml(s: ServiceRow): string {
    return `<div class="wb-kv"><span>登录状态（完整）</span><b>${this.accountAuthValueHtml(s)}</b></div>`;
  }


  private accountQuotaReadingHtml(s: ServiceRow): string {
    return `<div class="wb-kv"><span>额度与失效时间（完整）</span><b>${escapeHtml(this.accountQuotaPlain(s))}</b></div>`;
  }
private accountQuotaPlain(_s: ServiceRow): string { return "未知（当前接入未提供可信账户余额接口，不按 0 显示）· 失效时间未知（未提供凭据到期查询接口）"; }
  /** 第一阅读层 · 下一主动作：只写这张卡真实提供的下一步与它的边界（该链自己的
   *  只读状态查询，经 8096 只读提交、一次点击直达、无副作用）；真实返回说未认证
   *  时，下一步是先由维护者本人完成登录（先二次确认，绝不自动打开窗口）。没有
   *  账号管理接口的链如实写明可用的只有只读刷新与「详情」，不画死按钮。 */
  private accountNextActionHtml(s: ServiceRow): string {
    const status = this.statusActionOf(s);
    if (!status) {
      const gap = this.statusGap(s);
      const text = s.statusAction ? `${gap.word}：${gap.why}；接入前不提供查询按钮` : "该链未提供账号管理接口；可用的只有只读刷新与「详情」（不画死按钮）";
      return `<div class="wb-kv"><span>下一主动作（完整）</span><b>${escapeHtml(text)}</b></div>`;
    }
    const result = this.actionResults[status.action];
    const login = (s.manageActions ?? []).find(a => isLoginAction(a.action));
    const follow = result?.authenticated === false && login
      ? `；本次真实返回为未认证：改用「${escapeHtml(login.label)}」，由维护者本人完成登录（先二次确认）`
      : "";
    return `<div class="wb-kv"><span>下一主动作（完整）</span><b>「${escapeHtml(status.label)}」：经 8096 只读提交，一次点击直达、无副作用${follow}</b></div>`;
  }
  /** 该卡自己的折叠（维护者 2026-09-29）：OAuth / 协议与探针的长句、额度与
   *  用量明细、只读查询的原始返回都收在这里。折叠体只放说明、明细与证据，不藏
   *  任何阻塞失败面（`.wb-banner` / `.wb-op-status[data-state="failed"]` /
   *  `.wb-svc-result-failed` / `.wb-log-state` / `.wb-fail-groups` /
   *  `.wb-stale-chip` 六类各有自己的形状与颜色，见 workbench.css §12）。
   *  开合态按服务 id 记在 `accountFoldOpen`，30s 轮询与切 lane 重绘后还原。 */
  private accountCardFoldHtml(s: ServiceRow): string {
    const open = this.accountFoldOpen.has(s.id);
    return `<details class="wb-secondary-details"${open ? " open" : ""} data-wb-account-fold="${s.id}">`
      + `<summary title="完整读数、链说明（OAuth / 协议）、探针证据、额度与用量明细、只读查询的原始返回 · 点击展开 / 收起">详情与原始返回${wbFoldMark()}</summary>`
      + this.accountLoginReadingHtml(s)
      + this.accountQuotaReadingHtml(s)
      + this.accountNextActionHtml(s)
      + `<p class="wb-muted">链说明：${escapeHtml(s.note)}</p>`
      + `<p class="wb-muted">账户层能回答什么：${escapeHtml(this.accountCapabilityNote(s))}</p>`
      + `<p class="wb-muted">探针证据：${escapeHtml(this.probeEvidence(s.id))}</p>`
      + this.accountQuotaHtml(s.id)
      + this.accountRawReturnHtml(s)
      + `</details>`;
  }
private accountCapabilityNote(_s: ServiceRow): string { return "当前接入未提供可信账户余额接口；本机用量与参考费用见「费用」。"; }
  /** 该卡只读查询动作的**原始返回**（本页记住的真实载荷）：只作证据，不做
   *  解释；没有读取过写「未读取」，读取失败写失败结论，序列化失败写「无法
   *  序列化」。登录动作不是只读查询，其返回不在此展开（终态见「操作记录」）。
   *  载荷有界，超长时如实写截断与原长，不静默丢字段。 */
  private accountRawReturnHtml(s: ServiceRow): string {
    const actions = [this.statusActionOf(s)?.action, ...(s.manageActions ?? []).map(a => a.action)]
      .filter((a): a is string => Boolean(a))
      .filter(a => ACCOUNT_READ_ACTIONS.includes(a));
    const logins = (s.manageActions ?? []).filter(a => isLoginAction(a.action)).map(a => a.label);
    const loginNote = logins.length
      ? `<p class="wb-muted">登录动作（${escapeHtml(logins.join(" / "))}）不是只读查询，其返回不在此展开；终态见「操作记录」。</p>`
      : "";
    if (!actions.length) return loginNote + `<p class="wb-muted">该卡没有只读查询动作，没有原始返回可列。</p>`;
    return loginNote + actions.map(action => {
      const result = this.actionResults[action];
      const failure = this.actionResultFailures.get(action);
      if (!result) {
        return `<div class="wb-kv"><span>原始返回 · ${escapeHtml(action)}</span><b>${failure ? `未取得（${escapeHtml(failure)}）` : "未读取"}</b></div>`;
      }
      let json = "";
      try { json = JSON.stringify(result); } catch { json = ""; }
      if (!json) return `<div class="wb-kv"><span>原始返回 · ${escapeHtml(action)}</span><b>返回值无法序列化（不猜内容）</b></div>`;
      const capped = json.length > ACCOUNT_RAW_RETURN_MAX;
      return `<div class="wb-kv"><span>原始返回 · ${escapeHtml(action)}</span><b>${escapeHtml(capped ? json.slice(0, ACCOUNT_RAW_RETURN_MAX) : json)}${capped ? `…（原始返回共 ${json.length} 字符，此处截断到 ${ACCOUNT_RAW_RETURN_MAX}；完整终态见「操作记录」的该动作行，账本读取有界）` : ""}</b></div>`;
    }).join("");
  }
  /** 账号页：一条链的认证状态 + 检查时点（管理详情面板用；标签带链名）。
   *  值只来自该链只读查询动作的真实返回，绝不由前端推断；没有返回过 = 未读取，
   *  失败 = 失败 + 保留上一次真实值。只读查询是一次点击直达的动作，不需要
   *  二次确认。 */
  private accountAuthHtml(s: ServiceRow): string {
    return `<div class="wb-kv"><span>${escapeHtml(s.name)} · 认证状态</span><b>${this.accountAuthValueHtml(s)}</b></div>`;
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
      if (!s.statusAction) return "未知（该链未提供只读认证查询接口，不猜一个状态）";
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
    const authenticated = result.authenticated;
    const state = typeof authenticated === "boolean"
      ? (authenticated ? "已认证" : "未认证（该链自报）")
      : typeof result.state === "string" && result.state
        ? `状态 ${result.state}${typeof result.label === "string" && result.label ? `（${result.label}）` : ""}`
        : "已读取（响应未含状态词，不猜）";
    const note = typeof result.note === "string" && result.note ? ` · ${result.note}` : "";
    const verified = result.verified === false ? " · 未主动核验（内存态只读）" : "";
    // 本次读取失败但有上一次真实值：保留真实值并标陈旧，绝不因失败把它折成
    // 「未认证」，也不假装刷新成功。
    const stale = failure ? ` · 本次读取失败（${failure}），以上为上一次真实读取` : "";
    return `${state} · 读于 ${at}${verified}${note}${stale}`;
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
  private usageBlockHtml(source: string, title: string, mode: "complete" | "output-only", extraHtml: string): string {
    const read = this.usageTokens;
    const head = `<small>${escapeHtml(title)}</small>`;
    const refresh = `<button type="button" class="wb-btn wb-btn-sm" data-wb-usage-refresh="1" title="重新读取用量端点（只读，有界）" aria-label="刷新用量">${wbIcon("refresh")}${wbIconHint("刷新用量")}</button>`;
    if (!read || !read.payload) {
      return `<div class="wb-quota">${head}<p class="wb-quota-note">${this.usageReadNote(read, "用量")}</p>${refresh}</div>`;
    }
    const labels = this.beijingDayKeys(14);
    const window = (days: string[]) => {
      let complete = 0, input = 0, output = 0, completeRows = 0, missingRows = 0;
      let partialRows = 0, partialRequests = 0, partialKnown = true, outputAll: number | null = null;
      let partialInput: number | null = null, partialOutput: number | null = null, partialOutputMissing = 0;
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
          const pIn = typeof r.partial_input_tokens === "number" && Number.isFinite(r.partial_input_tokens) ? r.partial_input_tokens : null;
          const pOut = this.partialOutputTokens(r);
          if (pIn !== null) partialInput = (partialInput ?? 0) + pIn;
          if (pOut === null) partialOutputMissing += 1;
          else partialOutput = (partialOutput ?? 0) + pOut;
        }
        // 输出（账本可见）= 完整请求的输出 + 部分请求里可见的输出；两者都是
        // 账本记录值，缺失的分量不补 0（缺输出 Token 的请求不进入这一列）。
        if (typeof r.output_tokens === "number" && Number.isFinite(r.output_tokens)) outputAll = (outputAll ?? 0) + r.output_tokens;
        const visiblePartialOut = this.partialOutputTokens(r);
        if (visiblePartialOut !== null) outputAll = (outputAll ?? 0) + visiblePartialOut;
      }
      for (const m of read.payload!.missing) {
        // date=null 的行（时间戳无效）不属于任何窗口，单独计数（见下方 undatedNote），
        // 既不静默丢弃，也不硬塞进今日 / 近 7 天。
        if (m.source === source && m.date !== null && days.includes(m.date)) missingRows += 1;
      }
      return { complete, input, output, completeRows, missingRows, partialRows, partialRequests, partialKnown, outputAll, partialInput, partialOutput, partialOutputMissing };
    };
    const bucket = (label: string, v: ReturnType<typeof window>) => {
      const partialCountText = v.partialKnown ? `${v.partialRequests.toLocaleString("zh-CN")} 条` : "条数未知";
      const partialOutText = v.partialOutput === null ? "未知" : fmtTokens(v.partialOutput);
      // 输出可见只在「完整口径」块里另列：output-only 块的主数字已经含了它。
      const partialDetail = mode === "complete"
        ? `，另列：输出可见 ${partialOutText}${v.partialOutputMissing ? `，另有 ${v.partialOutputMissing} 行连输出也没有` : ""}`
        : "";
      const partialClause = v.partialRows || !v.partialKnown
        ? `；另有缺分量请求 ${partialCountText}（不混入完整总量${partialDetail}）`
        : "";
      const inputVisible = v.input + (v.partialInput ?? 0);
      const inputText = v.partialInput === null && (v.partialRows || !v.partialKnown)
        ? "未知" : fmtTokens(inputVisible);
      const inputSuffix = v.partialInput !== null && v.partialRows ? "（含部分可见）" : "";
      const value = mode === "complete"
        ? (v.completeRows
          ? `完整 Token ${fmtTokens(v.complete)}（输入 ${fmtTokens(v.input)} / 输出 ${fmtTokens(v.output)}）${partialClause}`
          : `完整 Token 未知（该窗口没有可展示的完整请求：缺分量请求 ${partialCountText}，另列不计入）`)
        : (v.completeRows || v.partialRows || !v.partialKnown
          ? `输出（账本可见）${v.outputAll === null ? "未知" : fmtTokens(v.outputAll)} · 输入 ${inputText}${inputSuffix}${partialClause}`
          : `无记录（该窗口没有该来源的账本行，无法判定为零）`);
      return `<div class="wb-quota-bucket"><div><strong>${escapeHtml(label)}</strong><span>${value}</span></div></div>`;
    };
    const today = window([labels.at(-1)!]);
    const week = window(labels.slice(-7));
    const partialNote = !today.partialKnown || !week.partialKnown
      ? `<small>缺失分量：后端未返回 partial_requests 契约字段，条数未知（不按 0 计）；可展示的分量见「仅输出可见」另列。</small>`
      : today.partialRequests + week.partialRequests
        ? `<small>缺失分量：今日 ${today.partialRequests} 条 · 近 7 天 ${week.partialRequests} 条部分请求（输入或输出缺一 → 该分量显示「未知」，不补 0，不计入完整总量；可展示的输出另列）</small>`
        : `<small>缺失分量：今日与近 7 天均无（窗口内每个请求的输入与输出都齐全）</small>`;
    const rowsNote = today.missingRows + week.missingRows
      ? `<small>另列：今日 ${today.missingRows} 行 · 近 7 天 ${week.missingRows} 行记录含缺分量请求，未混入上面的完整总量（分量数字见「仅输出可见」）。</small>`
      : "";
    const undatedRows = read.payload.missing.filter(m => m.source === source && m.date === null).length;
    const undatedNote = undatedRows
      ? `<small>时间无法判定（另列）：${undatedRows} 行记录时间戳无效，不归任何一天，既不进今日 / 近 7 天合计，也不按 0 计。</small>`
      : "";
    return `<div class="wb-quota">${head}${bucket("今日", today)}${bucket("近 7 天", week)}${partialNote}${rowsNote}${undatedNote}${extraHtml}${refresh}<small>${this.usageReadNote(read, "用量")}</small></div>`;
  }
  /** Ledger credits for one aggregated chain (/api/summary
   *  services.*.credits): undefined means the aggregate has not landed or
   *  carries no credits for that chain — 未知, never 0. */
  private chainCredits(name: string): number | undefined {
    return this.chainFees?.find(c => c.name === name)?.credits;
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
    const rows = read.payload.hours.filter(r => r.source === source);
    return rows.reduce((s, r) => s + (Number.isFinite(r.requests) ? r.requests : 0), 0);
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
    if (typeof recorded !== "number" || !Number.isFinite(recorded)) return "未知（响应未提供该来源的账本计数）";
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
    const rows = read.payload.days.filter(r => r.source === source && days.includes(r.date));
    if (!rows.length) {
      const coverage = read.payload.coverage?.sources?.[source];
      if (coverage?.ledger_present === false) return "账本未读取（未知）";
      if (coverage?.enabled === false) return "来源未启用（未知）";
      return "无记录";
    }
    let total = 0;
    for (const r of rows) {
      if (!Number.isFinite(r.complete_requests) || !Number.isFinite(r.partial_requests)) return "未知";
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
private sourceLabel(src: string): string { return src === "um" ? "Open WebUI 上游" : src === "chat2api" ? "chat2api" : src; }
  /** Real failures only (R20-03): the issue views are driven by the 8096
   *  operation ledger inside a management session; without a session or with
   *  an empty ledger the surface says so honestly. Knowledge documents are
   *  never presented as live alerts. */
  private failuresNote(all: boolean) {
    // 读取在途不写「不可读」：`undefined` = 本次读取尚未返回，只有读取真的
    // 失败（`gated`，带实测原因）才写失败句。应答了 HTTP 错误就写应答错误，
    // 不写成「不可达」——客户端没有测到不可达。
    if (this.opsRecent === undefined) return "8096 操作账本读取中…（未返回前不写「不可读」）。";
    if (this.opsRecent?.state !== "live") return `${this.opsReadFailureText()}；无真实操作记录（恢复后自动接入）。`;
    return all ? "最近真实操作（8096 操作记录，本机直读）。" : "当前失败操作（来自 8096 操作账本；为空即如实显示空态）。";
  }
  /** Issues lane and log lane share one runs-list surface: a bordered card
   *  of operation rows (state in Chinese) or one honest empty card — the
   *  old two stacked muted lines said the same thing twice. */
  /** Key row lookup for the rename / adjust-cap / toggle branches. */
  private shareKeyRow(id: string): ShareKeyRow | undefined {
    if (this.shareKeys?.state !== "live") return undefined;
    return this.shareKeys.rows.find(row => row.id === id);
  }
  /** Share lane: ①开放状态与主操作 ②分层维护 ③分享地址与访问密钥 (one card:
   *  address + 准备条件 on top, keys below; maintainer 2026-10-07) ④每 Key 日费用
   *  with 容量与近期操作 under it, beside ⑤每 Key 用量明细. Honest states only — gated/refused/error
   *  keep their own reason; the UI never fabricates state, never substitutes
   *  fixture data (R9-R35 honest-gate pattern). R9-R46: block ① is a
   *  full-width hero with a refresh button; data refreshes on lane entry,
   *  poll ticks and share.* terminal states (no more entry-only snapshot).
   *  The created/rotated plaintext lives in a resident keys-block section
   *  until cleared or reload. */
  private shareLaneHtml(): string {
    // Lane entry issues BOTH reads immediately. The old guard tested
    // `sessionLive()` FIRST and returned 「8096 不可达：分享网关状态暂不可读」 before a
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
    const statusHtml = !status
      ? `<p class="wb-muted" data-state="loading">分享状态读取中…</p>`
      : status.state === "gated" ? `<p class="wb-muted">分享状态不可取得（无凭据会话）；不以文档冒充状态。</p>`
      : status.state === "refused" ? `<p class="wb-muted">分享状态被拒绝（403 权限不符）。</p>`
      : status.state === "loading" ? `<p class="wb-muted" data-state="loading">分享状态读取中…</p>`
      : status.state === "error" ? `<p class="wb-muted">分享状态读取失败（${escapeHtml(status.reason)}）；8096 不可达时没有权威状态，不以文档冒充状态。启停动作仍可手动提交。<button type="button" class="wb-btn wb-btn-sm" data-wb-share-refresh aria-label="重试读取分享状态">${wbIcon("refresh")}${wbIconHint("重试读取分享状态")}</button></p>`
      : this.shareStatusHtml(status.payload);
    const keys = this.shareKeys;
    const keysHtml = !keys
      ? `<p class="wb-muted" data-state="loading">密钥列表读取中…</p>`
      : keys.state === "gated" ? `<p class="wb-muted">密钥列表不可取得（无凭据会话）。</p>`
      : keys.state === "refused" ? `<p class="wb-muted">密钥列表被拒绝（403 权限不符）。</p>`
      : keys.state === "loading" ? `<p class="wb-muted" data-state="loading">密钥列表读取中…</p>`
      : keys.state === "error" ? `<p class="wb-muted">密钥列表读取失败（${escapeHtml(keys.reason)}）。<button type="button" class="wb-btn wb-btn-sm" data-wb-share-refresh aria-label="重试读取密钥列表">${wbIcon("refresh")}${wbIconHint("重试读取密钥列表")}</button></p>`
      : this.shareKeysTableHtml(keys.rows);
    // Order: hero → layers → address + keys (all three span the grid), then two
    // columns - the daily-fee chart with capacity under it, and the per-key
    // detail table - so neither column carries the whole ledger alone.
    // Plan-Revision 9（维护者 2026-09-28 ③/⑤）：三张卡都带工作台的卡类（.wb-block），
    // 节标题是卡自己的直系 h3——与其余车道同形，节标题因此按 .wb-block > h3 读到
    // （PR11 得意黑区块标题档 16px/400）。卡头只留右侧的刷新动作，标题不再包在卡头里（同一基线由
    // 卡的内距与动作自己的行高承担）。文字与动作一个都不减。
    const usage = this.shareUsageParts();
    return `<div class="wb-share-grid">`
      + `<section class="wb-block wb-share-block wb-share-hero"><div class="wb-section-head"><h3>开放状态与主操作</h3><button type="button" class="wb-btn wb-btn-sm" data-wb-share-refresh title="重新读取分享状态与密钥列表（只读）" aria-label="刷新分享状态与密钥列表">${wbIcon("refresh")}刷新</button></div>${statusHtml}</section>`
      + this.shareLayersHtml()
      // Patch/rename/cap failures surface inside the modal while it is open;
      // when it is closed the block-level mirror keeps them visible.
      // One access card: the address a client opens on top, the keys it presents
      // below (maintainer 2026-10-07). The address and 准备条件 read the SAME
      // status answer as the hero, so a failed read reaches them too and they
      // never sit on 读取中… while the hero already says 读取失败.
      + `<section class="wb-block wb-share-block wb-share-access wb-char"><h3>分享地址与访问密钥</h3>`
      + `<div class="wb-share-access-url"><h4>分享地址</h4>${this.shareUrlHtml(status)}</div>`
      + `<div class="wb-share-keys"><h4>访问密钥</h4>${keysHtml}${this.shareKeyCreateButtonHtml()}${!this.shareKeyFormOpen && this.shareKeyError ? `<p class="wb-share-error">${escapeHtml(this.shareKeyError)}</p>` : ""}${this.sharePlaintextPanelHtml()}</div></section>`
      // Per-key usage in two cards side by side: the daily-fee comparison (with
      // capacity under it) and the per-key detail table, so neither column
      // carries the whole ledger alone.
      + `<div class="wb-share-col"><section class="wb-block wb-share-block wb-share-usage-chart"><h3>每 Key 日费用 · 近 14 天</h3>${usage.chart}</section>`
      + `<section class="wb-block wb-share-block" data-wb-anchor="share-capacity"><h3>容量与近期操作</h3>${this.shareCapacityHtml(status)}</section></div>`
      + `<div class="wb-share-col"><section class="wb-block wb-share-block wb-share-usage"><h3>每 Key 用量明细（分享后端账本）</h3>${usage.detail}</section></div>`
      + `</div>`;
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
  private shareLayersHtml(): string {
    const cap = (action: string) => this.actions?.find(r => r.action === action);
    const gapText = (action: string) => {
      const row = cap(action);
      return !this.actions ? "动作注册表未读取（读取中或读取失败）" : !row ? "当前后端版本不支持（待后端接入）" : `已停用：${row.disabled_reason ?? "原因未提供"}`;
    };
    const diag = this.actionResults["share.diagnostics"];
    const failure = this.actionResultFailures.get("share.diagnostics");
    const layers = (diag && typeof diag.layers === "object" && diag.layers ? diag.layers : {}) as Record<string, Record<string, unknown>>;
    // A check time from another day carries its date: 「检查于 09:12」 must not
    // read as today's reading.
    const time = (v: unknown) => {
      if (typeof v !== "number" || v <= 0) return "时间未知";
      const at = new Date(v * 1000);
      return at.toDateString() === new Date().toDateString()
        ? at.toLocaleTimeString("zh-CN", { hour12: false })
        : at.toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    };
    // The backend's own vocabulary (console_share_actions `_diag_*`): `lost` reads
    // by layer, `unconfirmed` is never shown as a pass, and an unrecognised word
    // stays neutral rather than green.
    const word = (key: string, state: string): [string, string] => {
      const good: Record<string, string> = { running: "运行中", ok: "正常", on: "开启", valid: "认证有效" };
      const warn: Record<string, string> = { logging_in: "认证中", maintenance: "维护中", draining: "排空中" };
      const bad: Record<string, string> = { stopped: "已停止", off: "关闭", invalid: "认证失效", failed: "认证失败", needs_login: "需要登录" };
      const lost: Record<string, string> = { connectivity: "连接中断", upstream: "不可达", security_gate: "条件不满足" };
      if (good[state]) return [good[state]!, "ok"];
      if (warn[state]) return [warn[state]!, "warn"];
      if (state === "lost") return [lost[key] ?? "中断", "danger"];
      if (bad[state]) return [bad[state]!, "danger"];
      if (state === "unavailable") return ["不可取得", "idle"];
      if (state === "unconfirmed") return ["未确认", "idle"];
      return [state === "unknown" || !state ? "未知" : state, "idle"];
    };
    const NAMES: Record<string, string> = {
      share_gateway: "入口进程", connectivity: "入口到 8110 的连接", ts_service: "Tailscale 服务", funnel: "Funnel 公网", security_gate: "安全条件",
      share_backend: "8110 进程", backend_auth: "8110 认证", upstream: "Open WebUI 上游连接",
    };
    const layerRow = (key: string) => {
      const l = layers[key];
      if (!l) return `<li><span>${escapeHtml(NAMES[key] ?? key)}</span><span class="wb-badge wb-badge-idle">${diag ? "未返回" : "未读取"}</span></li>`;
      const [word0, cls0] = word(key, String(l.state ?? ""));
      // A stale reading keeps its last state but never the fresh green: the
      // badge itself says it is old, so it cannot pass for a current verdict.
      // Staleness is the backend's verdict (l.stale); the page only adds that a
      // failed refresh leaves every reading still shown as the previous one.
      const stale = l.state !== "unavailable" && (l.stale === true || Boolean(failure));
      const w = stale ? `${word0}（已陈旧）` : word0;
      const cls = stale && cls0 === "ok" ? "warn" : cls0;
      const verdict = l.verdict === "confirmed" ? "已确认" : l.verdict === "unconfirmed" ? "未确认" : l.verdict === "unavailable" ? "证据不可取得" : String(l.verdict ?? "");
      const note = [verdict, `检查于 ${time(l.checked_at)}`, typeof l.reason === "string" && l.reason ? `原因 ${l.reason}` : ""].filter(Boolean).join(" · ");
      return `<li><span>${escapeHtml(NAMES[key] ?? key)}</span><span class="wb-badge wb-badge-${cls}">${escapeHtml(w)}</span><small>${escapeHtml(note)}</small></li>`;
    };
    const faults = ((layers.share_gateway?.evidence as Record<string, unknown> | undefined)?.faults as Record<string, unknown> | undefined)?.verdict as Record<string, unknown> | undefined;
    const last = faults?.last_fault as Record<string, unknown> | undefined;
    const PHASES: Record<string, string> = { auth: "入口凭据", config: "配置", body: "请求体", limit: "限额", upstream: "上游", stream: "断流" };
    const counts = faults?.counts_by_phase && typeof faults.counts_by_phase === "object"
      ? Object.entries(faults.counts_by_phase as Record<string, unknown>).filter(([, n]) => typeof n === "number" && n > 0).map(([k, n]) => `${PHASES[k] ?? k} ${n}`) : [];
    const countsHtml = counts.length ? `<p class="wb-muted">入口留证的故障计数（按阶段）：${escapeHtml(counts.join(" · "))}</p>` : "";
    const faultHtml = last
      ? `<p class="wb-share-fault"><b>最近故障</b> ${escapeHtml([time(last.at), last.phase ? `阶段 ${last.phase}` : "", typeof last.status === "number" ? `入口回 HTTP ${last.status}` : "", last.code ? `${last.code}` : "", typeof last.upstream_status === "number" ? `上游 HTTP ${last.upstream_status}` : "", typeof last.duration_ms === "number" ? `耗时 ${Math.round(last.duration_ms)} ms` : "", last.request_id ? `请求 ${last.request_id}` : ""].filter(Boolean).join(" · "))}</p>`
      : diag ? `<p class="wb-muted">入口留证中没有最近一次故障。</p>` : "";
    const stopInForce = diag?.desired_stop_in_force === true;
    const maintenance = diag?.maintenance as Record<string, unknown> | undefined;
    const lease = maintenance?.lease as Record<string, unknown> | null | undefined;
    const instance = maintenance?.instance as Record<string, unknown> | null | undefined;
    const lastOutcome = maintenance?.last_outcome as Record<string, unknown> | null | undefined;
    const lastError = diag?.last_error as Record<string, unknown> | null | undefined;
    // 陈旧只降新鲜度（Rev19）：本次刷新失败时，维护监督读数与各层读数一样
    // 保留上一次的真实值并标「已陈旧」——绝不判宕机、也不折成「没有维护」。
    const mark = (w: string, stale: boolean) => (stale ? `${w}（已陈旧）` : w);
    const staleOf = (entry: Record<string, unknown> | null | undefined) =>
      !!entry && entry.state !== "unavailable" && (entry.stale === true || Boolean(failure));
    const outcomeHtml = lastOutcome
      ? `上次维护结果：${lastOutcome.ok === true ? "成功" : lastOutcome.ok === false ? "未成功" : "结果未提供"}${lastOutcome.code ? `（${escapeHtml(String(lastOutcome.code))}）` : ""}${lastOutcome.detail ? ` ${escapeHtml(String(lastOutcome.detail).slice(0, 160))}` : ""}`
      : "";
    const instanceHtml = instance
      ? `受信实例 ${instance.instance_id ? escapeHtml(String(instance.instance_id)) : "未提供"}（状态 ${escapeHtml(String(instance.state ?? "未提供"))} · 在途 ${typeof instance.active_count === "number" ? instance.active_count : "未提供"} · 最后代次 ${typeof instance.last_seen_generation === "number" ? instance.last_seen_generation : "未提供"}）`
      : "";
    const statusLine = [
      stopInForce ? "人工停止意图生效中：不提供单层恢复入口" : "",
      lease
        ? mark(`维护租约进行中（${String(lease.component ?? "组件未提供")}${lease.lease_id ? ` · 租约 ${String(lease.lease_id)}` : ""}${lease.round !== undefined && lease.round !== null ? ` · 轮次 ${String(lease.round)}` : ""}）`, Boolean(failure))
        : diag ? "当前没有维护租约（维护视图已读取）" : "",
      instance ? mark(instanceHtml, staleOf(instance)) : "",
      lastOutcome ? mark(outcomeHtml, staleOf(lastOutcome)) : "",
      // 权威记录（desired_stop_in_force / last_error）由页面自己的一句话写出：
      // 缺失即未提供，不折成「没有停止意图」。
      lastError && (lastError.code || lastError.message) ? `上次维护未成功：${String(lastError.code ?? "")}${lastError.message ? ` ${String(lastError.message).slice(0, 120)}` : ""}` : "",
    ].filter(Boolean).map(t => `<p class="wb-muted">${escapeHtml(t)}</p>`).join("");
    // 监督状态（Rev19 接线）：分享链的独立监督只来自监督日志——console 既有读面
    // （share.diagnostics）带回 `supervision` 段时，按后端自己的形状显示决策
    // （action / code / attempts_this_hour / confirmations / instance / 耗时）与状态
    // （idle_reason / consecutive_failures / restart_attempts_utc / version）；读面
    // 未返回该段时如实写「未返回监督段（未提供 ≠ 监督未运行）」，绝不显示成
    // 已启用、也不写恢复循环。陈旧只降新鲜度。
    const supervision = (diag?.supervision && typeof diag.supervision === "object" ? diag.supervision : null) as Record<string, unknown> | null;
    const supDecision = (supervision?.decision && typeof supervision.decision === "object" ? supervision.decision : null) as Record<string, unknown> | null;
    const supState = (supervision?.state && typeof supervision.state === "object" ? supervision.state : null) as Record<string, unknown> | null;
    const supText = (v: unknown) => (typeof v === "string" && v ? v : "未提供");
    const supCount = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? String(v) : "未提供");
    const supAttempts = Array.isArray(supState?.restart_attempts_utc) ? supState.restart_attempts_utc.length : null;
    const supStale = Boolean(failure) || supervision?.stale === true;
    const supervisionHtml = supervision
      ? `<p class="wb-muted">监督状态${supStale ? "（已陈旧）" : ""}：决策 ${escapeHtml(supText(supDecision?.action))} / ${escapeHtml(supText(supDecision?.code))} · 本小时尝试 ${escapeHtml(supCount(supDecision?.attempts_this_hour))} 次 · 确认 ${escapeHtml(supCount(supDecision?.confirmations))} · 实例 ${escapeHtml(supText(supDecision?.instance && typeof supDecision.instance === "object" ? (supDecision.instance as Record<string, unknown>).identity : null))}${typeof supDecision?.duration_ms === "number" ? ` · 耗时 ${Math.round(supDecision.duration_ms)} ms` : ""} · 空闲原因 ${escapeHtml(supText(supState?.idle_reason))} · 连续失败 ${escapeHtml(supCount(supState?.consecutive_failures))} · 最近重启尝试 ${supAttempts === null ? "未提供" : `${supAttempts} 次`}${typeof supDecision?.at_utc === "string" && supDecision.at_utc ? ` · 决策于 ${escapeHtml(supDecision.at_utc.slice(0, 19).replace("T", " "))}` : ""}${typeof supDecision?.detail === "string" && supDecision.detail ? `。${escapeHtml(supDecision.detail.slice(0, 200))}` : ""}</p>`
      : diag
        ? `<p class="wb-muted">监督状态：本次分层读面未返回监督段（未提供 ≠ 监督未运行）；监督独立于 8096 与本网页运行，页面不另写恢复循环。整链开启 / 停止 / 紧急停止在上方「开放状态与主操作」。</p>`
        : `<p class="wb-muted">监督状态：尚未读取分层状态，监督段一律未知；监督独立于 8096 与本网页运行，页面不另写恢复循环。整链开启 / 停止 / 紧急停止在上方「开放状态与主操作」。</p>`;
    // 维护前后比较（Rev19 接线）：单层重启动作自己的终态结果带回该动作的
    // `before` / `after` 同层读数（console_share_actions `_diag_pair`）——页面只
    // 比较这一次动作的前后，不重读一遍冒充，缺失的一侧写「未提供」，陈旧只降
    // 新鲜度（同一 `word()` 词表与同一条陈旧口径）。
    const compare = this.shareRestartCompare;
    const compareHtml = (action: string) => {
      if (!compare || compare.action !== action) return "";
      const keys = [...new Set([...Object.keys(compare.before), ...Object.keys(compare.after)])];
      const side = (key: string, entry: Record<string, unknown> | undefined) => {
        if (!entry) return `<span class="wb-badge wb-badge-idle">未提供</span>`;
        const [w0, cls0] = word(key, String(entry.state ?? ""));
        const stale = staleOf(entry);
        const cls = stale && cls0 === "ok" ? "warn" : cls0;
        return `<span class="wb-badge wb-badge-${cls}">${escapeHtml(stale ? `${w0}（已陈旧）` : w0)}</span>`;
      };
      const rows = keys.map(key => {
        const b = compare.before[key] as Record<string, unknown> | undefined;
        const a = compare.after[key] as Record<string, unknown> | undefined;
        return `<li><span>${escapeHtml(NAMES[key] ?? key)}</span><small>维护前 ${side(key, b)} → 维护后 ${side(key, a)}</small><small>${escapeHtml(`${b && typeof b.checked_at === "number" ? `前 ${time(b.checked_at)}` : "前 时间未提供"} · ${a && typeof a.checked_at === "number" ? `后 ${time(a.checked_at)}` : "后 时间未提供"}`)}</small></li>`;
      }).join("");
      return `<div><p class="wb-muted"><b>维护前后比较（本次「${escapeHtml(actionLabel(compare.action))}」自己的前后读数）</b></p><ul class="wb-share-layer-list">${rows}</ul><p class="wb-muted">比较只来自该动作返回的 before / after（不重读一遍冒充）；缺失的一侧写「未提供」，陈旧只降新鲜度、不判宕机。</p></div>`;
    };
    // The terminal result of a single-layer action sits under its own button
    // (operation-area level of the feedback design), not only in the notice
    // pool where a success fades.
    // Every share lifecycle/maintenance action runs under one orchestration
    // lock on the backend; while one is in flight from this page the single-
    // layer buttons do not offer a second submit (the read-only diagnostics
    // only locks its own button).
    const inFlight = (e: FeedbackEntry) => e.state === "preparing" || e.state === "queued" || e.state === "running";
    const busy = [...this.feedbackEntries.values()].find(e => inFlight(e) && e.action.startsWith("share.") && e.action !== "share.diagnostics");
    const diagBusy = [...this.feedbackEntries.values()].some(e => inFlight(e) && e.action === "share.diagnostics");
    const actionBtn = (action: string, label: string, impact: string) => {
      const row = cap(action);
      const chip = this.svcResultChip(`act:${action}`);
      if (row?.enabled && busy) {
        return busy.action === action
          ? `<div class="wb-share-layer-act"><button type="button" class="wb-btn" disabled aria-busy="true">${escapeHtml(label)} · 执行中</button><small>${escapeHtml(busy.text)}</small></div>`
          : `<div class="wb-share-layer-act"><button type="button" class="wb-btn" disabled>${escapeHtml(label)}</button><small>${escapeHtml(`「${busy.label}」进行中，结束后再操作`)}</small>${chip}</div>`;
      }
      if (!row?.enabled) return `<div class="wb-share-layer-act"><button type="button" class="wb-btn" disabled>${escapeHtml(label)}</button><small>${escapeHtml(gapText(action))}</small>${chip}</div>`;
      if (stopInForce) return `<div class="wb-share-layer-act"><button type="button" class="wb-btn" disabled>${escapeHtml(label)}</button><small>人工停止生效中</small>${chip}</div>`;
      return `<div class="wb-share-layer-act"><button type="button" class="wb-btn" data-wb-share-action="${action}">${escapeHtml(label)}</button><small>${escapeHtml(impact)}</small>${chip}</div>`;
    };
    const diagCap = cap("share.diagnostics");
    const readBtn = diagCap?.enabled
      ? diagBusy
        ? `<button type="button" class="wb-btn wb-btn-sm" disabled aria-busy="true">${wbIcon("refresh")}读取中…</button>`
        : `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-diag>${wbIcon("refresh")}读取分层状态</button>`
      : `<small class="wb-muted">分层诊断：${escapeHtml(gapText("share.diagnostics"))}</small>`;
    const readState = failure
      ? `<p class="wb-muted">分层诊断${diag ? "本次刷新失败，以下为上一次读数" : "读取失败"}（${escapeHtml(failure)}）。</p>`
      : diag ? `<p class="wb-muted">读于 ${escapeHtml(time(diag.checked_at))}（只读诊断，每次读取记一条操作记录）。${typeof diag.note === "string" && diag.note ? ` 后端口径：${escapeHtml(diag.note.slice(0, 200))}` : ""}</p>`
        : `<p class="wb-muted">尚未读取分层状态：点「读取分层状态」只读读取一次；未读取前各层一律未知。</p>`;
    const card = (title: string, keys: string[], acts: string, extra = "") =>
      `<div class="wb-share-layer"><h4>${escapeHtml(title)}</h4><ul class="wb-share-layer-list">${keys.map(layerRow).join("")}</ul>${extra}<div class="wb-share-layer-acts">${acts}</div></div>`;
    return `<section class="wb-block wb-share-block wb-share-layers"><div class="wb-section-head"><h3>分层维护</h3>${readBtn}</div>${readState}${statusLine}${supervisionHtml}`
      + `<div class="wb-share-layer-grid">`
      + card("分享入口 · 8100", ["share_gateway", "connectivity", "ts_service", "funnel", "security_gate"],
        actionBtn("share.gateway_restart", "仅恢复 / 重启 8100", "入口短暂中断；不动 8110，不关公网"), faultHtml + countsHtml + compareHtml("share.gateway_restart"))
      + card("分享专用后端 · 8110", ["share_backend", "backend_auth", "upstream"],
        actionBtn("share.backend_reauthenticate", "仅重新认证 8110", "只做认证，不冷启动进程")
        + actionBtn("share.backend_restart", "仅恢复 / 重启 8110", "先暂停入口接收并排空在途请求，再重启 8110"),
        compareHtml("share.backend_restart"))
      + `</div></section>`;
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
  /** The share-usage read as the two cards' bodies: `chart` (daily-fee
   *  comparison) and `detail` (provenance, counters, per-key table, 账本口径).
   *  Until the read is answered both say so; the state sentence (with its
   *  retry) lives in the detail card, the chart card points at it. */
  private shareUsageParts(): { chart: string; detail: string } {
    const read = this.shareUsage;
    const retry = `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-refresh title="重新读取分享状态、密钥列表与用量（只读）" aria-label="重试读取分享用量">${wbIcon("refresh")}${wbIconHint("重试读取分享用量")}</button>`;
    const body = !read || read.state === "loading"
      ? `<p class="wb-muted" data-state="loading">分享用量读取中…（GET /api/console/share-usage · 只读、免凭据）</p>`
      : read.state === "gated"
        ? `<p class="wb-muted">分享用量读口不可取得（无凭据会话）；不以文档冒充用量。${retry}</p>`
        : read.state === "refused"
          ? `<p class="wb-muted">分享用量读口被拒绝（403 / 401）：本口免凭据直读，被拒绝说明请求打到了分享管理面或别的路由，不是本读口的预期状态。不以 0 用量冒充。${retry}</p>`
          : read.state === "unavailable"
            ? `<p class="wb-muted">当前服务端版本未载入：GET /api/console/share-usage 实测 HTTP ${read.status}（${escapeHtml(read.reason)}）。读口未开放，因此这里不显示任何每 Key 数字——不以 0 冒充「没有用量」，也不把历史未归属用量摊到某个 Key。${retry}</p>`
            : read.state === "error"
              ? `<p class="wb-muted">分享用量读取失败（${escapeHtml(read.reason)}）；不以 0 或空表冒充零用量，也不沿用上一次读数当成本次结果。${retry}</p>`
              : null;
    const parts = body === null && read?.state === "live" ? this.shareUsageBodyParts(read.payload) : null;
    const chart = parts ? parts.chart
      : `<p class="wb-muted">${!read || read.state === "loading" ? "日费用图随分享用量读取显示（读取中）。" : "分享用量未读到，图中标记未知；原因见「每 Key 用量明细」。"}</p>` + this.emptyTrendHtml("USD", this.beijingDayKeys(14));
    return { chart, detail: (parts ? parts.detail : body ?? "") + this.laneNoteFoldHtml("share-usage", "账本口径", `<p class="wb-muted">读数来自分享专用后端账本（与「累计参考费用」是两个账本，不合并、不折算）；每 Key 用量/费用与未归属桶同层列出，费用按 USD 原值显示（不折算成 ¥）。</p>`) };
  }
  /** 分享用量的读数主体：每 Key 一行，未归属桶是同一张表里的一行（key_id 为
   *  null），绝不摊到某个 Key。费用口径：`costUsd` 为 null = 未知；0 且该模型
   *  在 `coverage.unpricedModels` 里 = 无价（未知）——两者都不显示 ¥0 / $0。
   *  页面只显示掩码后的 Key：能 join 到管理库行时用行自己的 `masked`，join 不到
   *  （已删除 / 已轮换的 Key 仍在历史里）保留账本 id 的尾部并写明原因，绝不隐藏、
   *  也绝不打印完整 Key。明细（模型 / 日）放在可展开行里。 */
  private shareUsageBodyParts(payload: ShareUsagePayload): { chart: string; detail: string } {
    const coverage = payload.coverage;
    const unpriced = new Set(coverage?.unpricedModels ?? []);
    const head = `<p class="wb-muted">本次读到 ${typeof payload.requests === "number" ? fmtRequests(payload.requests) : "未知"} 条记录`
      + `（账本 ${escapeHtml(coverage?.ledger ?? "文件名未提供")} · 上限 ${typeof coverage?.limit === "number" ? fmtRequests(coverage.limit) : "未提供"}`
      + ` · 检查时间 ${escapeHtml(this.shareUsageTime(payload.checkedAt))}）`
      + `${coverage?.ledgerPresent === false ? " · 账本文件缺席（没有可读数据，不等于零用量）" : ""}`
      + `${coverage?.ledgerReadable === false ? " · 账本不可读" : ""}`
      + `${coverage?.truncated === true ? " · 结果被截断（truncated）" : ""}`
      + `${typeof coverage?.unparsedLines === "number" && coverage.unparsedLines > 0 ? ` · 不可解析行 ${coverage.unparsedLines}` : ""}</p>`;
    const counterText = (value: number | undefined, what: string) =>
      typeof value === "number" ? `${what} ${fmtRequests(value)}` : `${what} 未提供`;
    const counters = `<p class="wb-muted">`
      + `${counterText(coverage?.attributed, "已归因")} · ${counterText(coverage?.unattributed, "未归属")} · `
      + `${counterText(coverage?.unknownUsageRequests, "用量未知")} · ${counterText(coverage?.unpricedRequests, "无价")} · `
      + `${counterText(coverage?.unknownTimeRequests, "时间无法判定")}`
      + `；未归属 = 账本里没有可归因 key_id 的记录，单列在下方，绝不摊到某个 Key。`
      + `${unpriced.size ? `未定价模型：${escapeHtml([...unpriced].join("、"))}（费用未知，不折成 $0）` : ""}</p>`;
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
        chart: `<p class="wb-muted">分享用量响应形状与预期不符，图中标记未知（原因见「每 Key 用量明细」）。</p>` + this.emptyTrendHtml("USD", this.beijingDayKeys(14)),
        detail: head + `<p class="wb-muted">分享用量响应形状与预期不符（${escapeHtml(error instanceof Error ? error.message : "未知原因")}）；不显示每 Key 数字，也不按 0 显示。</p>`,
      };
    }
    const rows = keys.map(key => this.shareUsageRowHtml(this.shareKeyView(key), unpriced)).join("");
    // 未归属桶与每 Key 行同形、同一套归一化（含 Token 细分），但仍单列在
    // 同一张表的最后一行，绝不摊到任何 Key。
    const unattributedRow = this.shareUsageRowHtml(this.shareUnattributedView(unattributedBucket), unpriced);
    const chart = this.shareKeyChartHtml(keys) || `<p class="wb-muted">账本里还没有可归因到 Key 的记录，没有可比较的日费用（未归属记录见「每 Key 用量明细」）。</p>`;
    return { chart, detail: head + counters
      + `<div class="wb-table-scroll wb-key-usage-scroll"><table class="wb-table wb-key-usage"><thead><tr><th>Key（名称 · 短 ID）</th><th>完整 Token（输入 / 输出）</th><th>费用（USD）</th><th>数据质量</th></tr></thead><tbody>${rows}${unattributedRow}</tbody></table></div>` };
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
    return `<strong>${escapeHtml(name ?? "名称未知")}</strong> <code class="wb-key-id">${escapeHtml(tail)}</code>`
      + (key.deleted ? ` <span class="wb-badge wb-badge-idle">已删除</span>` : "")
      + (row?.masked ? `<small class="wb-muted wb-key-masked">${escapeHtml(row.masked)}</small>` : "");
  }
  private shareKeyView(key: KeyUsage): ShareUsageRowView {
    return {
      identity: key.keyId,
      name: this.shareKeyName(key), records: key.coverage.records, inputTokens: key.inputTokens, outputTokens: key.outputTokens, tokens: key.tokenUsage,
      known: key.knownCostUsd, estimated: key.estimatedCostUsd, unknown: key.coverage.unknownRequests, unpriced: key.coverage.unpricedRequests,
      models: key.models, days: key.days,
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
      records: bucket.coverage.records, inputTokens: bucket.inputTokens, outputTokens: bucket.outputTokens,
      tokens: bucket.tokenUsage,
      known: bucket.knownCostUsd, estimated: bucket.estimatedCostUsd,
      unknown: bucket.coverage.unknownRequests, unpriced: bucket.coverage.unpricedRequests,
      models: bucket.models, days: bucket.days,
    };
  }
  /** F12-8 comparison chart: each compared key's daily fee (known + estimated,
   *  USD) over the last 14 days, plus all attributed keys together. A day with
   *  usage-unknown or unpriced requests is a lower bound (≥). Colours follow the
   *  key (stable slots), the legend names them, and the chooser below picks up
   *  to TOKEN_SERIES_MAX keys. */
  private shareKeyChartHtml(keys: KeyUsage[]): string {
    if (!keys.length) return this.emptyTrendHtml("USD", this.beijingDayKeys(14));
    const labels = this.beijingDayKeys(14);
    const dayCost = (d: CostDetail | undefined): number | null => {
      if (!d) return null;
      const k = d.knownCostUsd, e = d.estimatedCostUsd;
      return k === null && e === null ? null : (k ?? 0) + (e ?? 0);
    };
    const lower = (d: CostDetail | undefined) => !!d && ((d.coverage.unknownRequests ?? 0) > 0 || (d.coverage.unpricedRequests ?? 0) > 0);
    const totalOf = (k: KeyUsage) => (k.knownCostUsd ?? 0) + (k.estimatedCostUsd ?? 0);
    if (!this.shareKeyCompareTouched) {
      this.shareKeyCompare = new Set([...keys].sort((a, b) => totalOf(b) - totalOf(a)).slice(0, 2).map(k => k.keyId));
    }
    const shown = keys.filter(k => this.shareKeyCompare.has(k.keyId)).slice(0, TOKEN_SERIES_MAX);
    const slots = this.seriesSlots(this.shareKeySlot, shown.map(k => k.keyId));
    const byDay = (k: KeyUsage) => new Map(k.days.map(d => [d.identity, d] as const));
    const maps = new Map(keys.map(k => [k.keyId, byDay(k)] as const));
    const totalValues = labels.map(day => {
      let sum: number | null = null;
      for (const k of keys) { const v = dayCost(maps.get(k.keyId)!.get(day)); if (v !== null) sum = (sum ?? 0) + v; }
      return sum;
    });
    const totalPartial = labels.map(day => keys.some(k => lower(maps.get(k.keyId)!.get(day))));
    const nameOf = (k: KeyUsage) => `${k.keyName ?? this.shareKeyRow(k.keyId)?.name ?? "名称未知"}（…${k.keyId.slice(-6)}）${k.deleted ? "·已删除" : ""}`;
    const series = [
      { key: "__total__", label: "全部已归因 Key", values: totalValues, kind: "total" as const },
      ...shown.map(k => ({ key: k.keyId, label: nameOf(k), values: labels.map(day => dayCost(maps.get(k.keyId)!.get(day))), kind: "model" as const, slot: slots.get(k.keyId),
        // A key's day with usage-unknown or unpriced requests is a lower bound.
        partial: labels.map(day => lower(maps.get(k.keyId)!.get(day))) })),
    ];
    const chart = this.tokenSeriesSvg(labels, series, {
        aria: "近 14 天每 Key 日费用（美元，已知 + 估算），逐日悬停或按 Tab 查看数值",
        width: 620,
        format: (v: number) => `$${v.toFixed(4)}`,
        tick: (v: number) => `$${v.toFixed(2)}`,
        unit: "美元（已知 + 估算，分享专用账本）",
        scope: "日",
        partialValues: totalPartial,
        tipTime: (label: string) => `${label}（北京时间）`,
        missingLabel: () => "无记录",
      });
    const full = shown.length >= TOKEN_SERIES_MAX;
    const chooser = `<div class="wb-key-chooser" role="group" aria-label="选择要比较的 Key（最多 ${TOKEN_SERIES_MAX} 个）">${keys.map(k => {
      const on = this.shareKeyCompare.has(k.keyId);
      const swatch = on && slots.has(k.keyId) ? `<i class="wb-series-swatch" style="--wb-swatch:var(--wb-cat-${slots.get(k.keyId)})" aria-hidden="true"></i>` : "";
      return `<button type="button" class="wb-gw-chip" data-wb-share-compare="${escapeHtml(k.keyId)}" aria-pressed="${on}"${!on && full ? " disabled" : ""}>${swatch}${escapeHtml(nameOf(k))}</button>`;
    }).join("")}</div>`;
    return `<div class="wb-share-key-chart" data-wb-series="cny">${chooser}${chart}</div>`;
  }
  /** One table row of per-key (or unattributed) usage, with its day / model
   *  drill. Cost: null = 未知; 0 on an unpriced model = 无价 - never $0. */
  private shareUsageRowHtml(view: ShareUsageRowView, unpriced: Set<string>): string {
    const usd = (value: number | null | undefined, model?: string): string => {
      if (value === null || value === undefined || !Number.isFinite(value)) return "未知";
      if (value === 0 && model !== undefined && unpriced.has(model)) return "无价（未知）";
      return `$${value.toFixed(4)}`;
    };
    const tok = (v: number | null) => (v === null ? "未知" : fmtTokens(v));
    // The complete part is the reading; lower-bound and estimated parts are named
    // beside it. Without the split (older backend) the legacy sums are shown as
    // what they are - unsplit - never as complete totals.
    const tokens = (t: TokenUsage | null, legacyIn: number | null, legacyOut: number | null, inline: boolean) => {
      if (!t || (t.inputTokens === null && t.outputTokens === null && t.totalTokens === null)) return `${tok(legacyIn)} / ${tok(legacyOut)}${inline ? "（未拆分口径）" : `<small class="wb-muted">未拆分口径（含下界与估算）</small>`}`;
      const extra = [
        (t.lowerBoundTokens ?? 0) > 0 ? `下界 ${tok(t.lowerBoundInputTokens)} / ${tok(t.lowerBoundOutputTokens)}` : "",
        (t.estimatedTokens ?? 0) > 0 ? `估算 ${tok(t.estimatedInputTokens)} / ${tok(t.estimatedOutputTokens)}` : "",
      ].filter(Boolean).join(" · ");
      const head = `${tok(t.inputTokens)} / ${tok(t.outputTokens)}`;
      return extra ? (inline ? `${head}（另有 ${extra}）` : `${head}<small class="wb-muted">另有 ${extra}</small>`) : head;
    };
    const detailGroup = (title: string, items: CostDetail[], isModel: boolean) => `<span class="wb-key-breakdown"><strong>${title}</strong><span class="wb-key-breakdown-head"><span>${isModel ? "模型" : "日期（北京时间）"}</span><span>完整 Token · 输入 / 输出</span><span>已知费用</span><span>估算费用</span></span>${items.length ? items.map(d => `<span class="wb-key-breakdown-row"><span>${escapeHtml(d.identity)}<small>${d.coverage.records === null ? "记录数未知" : `${fmtRequests(d.coverage.records)} 条记录`}${(d.coverage.unknownRequests ?? 0) > 0 ? ` · 用量未知 ${d.coverage.unknownRequests}` : ""}${(d.coverage.unpricedRequests ?? 0) > 0 ? ` · 无价 ${d.coverage.unpricedRequests}` : ""}</small></span><span>${tokens(d.tokenUsage, d.inputTokens, d.outputTokens, false)}</span><span>${usd(d.knownCostUsd, isModel ? d.identity : undefined)}</span><span>${usd(d.estimatedCostUsd)}</span></span>`).join("") : `<span class="wb-muted">无明细记录</span>`}</span>`;
    const modelRows = detailGroup("按模型", view.models, true);
    const dayRows = detailGroup("按日期", view.days, false);
    const detail = `<details class="wb-secondary-details"${this.shareKeyDetailsOpen.has(view.identity) ? " open" : ""} data-wb-share-key-details="${escapeHtml(view.identity)}"><summary title="查看模型与日期明细 · 点击展开 / 收起">查看模型与日期明细 · 模型 ${view.models.length} / 日 ${view.days.length}${wbFoldMark()}<span class="wb-share-key-detail-body">${modelRows}${dayRows}</span></summary></details>`;
    return `<tr class="wb-key-summary"><td>${view.name}<small class="wb-muted">${view.records === null ? "记录数未知" : `${fmtRequests(view.records)} 条账本记录`}</small></td>`
      + `<td class="wb-share-tok"><strong>${tokens(view.tokens, view.inputTokens, view.outputTokens, false)}</strong></td>`
      + `<td><span class="wb-key-cost"><small>已知参考费用</small><strong>${usd(view.known)}</strong></span><span class="wb-key-cost"><small>另有估算费用</small><span>${usd(view.estimated)}</span></span></td>`
      + `<td><span class="wb-key-quality">用量未知 <b>${view.unknown === null ? "未知" : fmtRequests(view.unknown)}</b></span><span class="wb-key-quality">无价记录 <b>${view.unpriced === null ? "未知" : fmtRequests(view.unpriced)}</b></span></td></tr>`
      + `<tr class="wb-key-details-row"><td colspan="4">${detail}</td></tr>`;
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
    return Number.isFinite(at.getTime()) ? at.toLocaleString("zh-CN", { hour12: false }) : "不可解析";
  }
  /** Six honest states (A36-1): 未开放/正在开启/已开放/正在关闭/关闭有残留/
   *  状态未确认. Derived only from evidence layers; contradictory or missing
   *  evidence never collapses to a fabricated clean state. */
  private shareStateOf(payload?: ShareStatusPayload): { key: string; text: string } {
    if (!payload) return { key: "unconfirmed", text: "状态未确认（读取中）" };
    const funnelOn = payload.funnel_on === true;
    // gateway_running observes the actual share gateway INSTANCE through the
    // broker's read-only seam; broker.running is the always-on management
    // daemon and is deliberately NOT open/close-residue evidence (R9-R45.4:
    // a stale succeeded stop in the 8-row ops window used to shadow the
    // live open state because the residue branch ran first and read the
    // broker as residue).
    const gatewayUp = payload.gateway_running === true;
    const gatewayDown = payload.gateway_running === false;
    const opInFlight = this.opsRecent?.state === "live"
      ? this.opsRecent.rows.find(op => op.action.startsWith("share.")
        && (op.state === "pending" || op.state === "running"))
      : undefined;
    if (opInFlight?.action === "share.estop") return { key: "closing", text: "紧急停止中" };
    if (opInFlight?.action === "share.stop" || opInFlight?.action === "share.retry_stop") {
      return { key: "closing", text: "正在关闭" };
    }
    if (opInFlight?.action === "share.start") return { key: "opening", text: "正在开启" };
    // 已开放 first: funnel on + config in sync is the authoritative public
    // evidence; any OLDER succeeded stop is superseded by the later start
    // and must not shadow it.
    if (funnelOn && payload.in_sync !== false) return { key: "open", text: "已开放" };
    // 关闭有残留: a stop/estop finished but a live layer still shows (the
    // gateway instance, or funnel). opsRecent holds only the last 8 rows.
    const stopDone = this.opsRecent?.state === "live"
      ? this.opsRecent.rows.find(op => (op.action === "share.stop" || op.action === "share.estop")
        && op.state === "succeeded")
      : undefined;
    if (stopDone && (gatewayUp || funnelOn)) return { key: "closing-residue", text: "关闭有残留" };
    if (gatewayDown && funnelOn === false) {
      return { key: "closed", text: "未开放" };
    }
    return { key: "unconfirmed", text: "状态未确认（证据不全或互相矛盾）" };
  }
  private shareStatusHtml(payload: ShareStatusPayload): string {
    const state = this.shareStateOf(payload);
    const checkedAt = typeof payload.last_check_at === "number"
      ? new Date(payload.last_check_at * 1000).toLocaleString("zh-CN", { hour12: false })
      : "不可取得";
    const failure = typeof payload.last_failure === "string" && payload.last_failure
      ? ` · 失败原因：${escapeHtml(payload.last_failure)}`
      : "";
    const brokerLine = payload.broker
      ? (payload.broker.running
        ? `broker 运行中（PID ${payload.broker.pid ?? "—"} · ${payload.broker.version ?? "版本不可取得"}）`
        : "broker 未运行")
      : "broker 状态不可取得";
    const syncLine = payload.in_sync === true ? "发布同步一致"
      : payload.in_sync === false
        ? `发布同步不一致（desired ${payload.desired_generation ?? "?"} / applied ${payload.applied_generation ?? "?"}）`
        : "发布同步不可取得";
    return `<div class="wb-share-status"><span class="wb-share-pill wb-share-${escapeHtml(state.key)}">${escapeHtml(state.text)}</span>`
      + `<small>最近检查 ${escapeHtml(checkedAt)}${failure}</small>`
      + this.shareAttributedKeysHtml() + `</div>`
      + `<p class="wb-share-evidence">${escapeHtml(brokerLine)} · ${escapeHtml(syncLine)}</p>`
      + `<div class="wb-share-actions">${this.shareActionButtonsHtml(payload)}</div>`;
  }
  /** 已归属 Key 数（维护者 2026-09-28，Plan-Revision 9 逐页收纳 · 分享页首屏）：
   *  「开放状态、已归属 Key 数、主操作与失败/重试」四件事写在首屏。归属数只来自
   *  分享后端账本自己的读数（`/api/console/share-usage` 的 `keys[]` 计数 +
   *  `coverage.attributed` 请求计数），没有第二条来源；读口未读取 / 不可用 / 读取
   *  失败时如实写「不可用」并指回「每 Key 用量与费用」块，绝不折成「0 个 Key」。
   *  这一行不带任何实测 HTTP 状态：状态词只由分享状态读口那一条失败句写出一次。 */
  private shareAttributedKeysHtml(): string {
    const read = this.shareUsage;
    if (!read || read.state === "loading") return `<small>已归属 Key：读取中…</small>`;
    if (read.state !== "live") {
      return `<small>已归属 Key：不可用（分享用量读口未读取到，见「每 Key 用量与费用」块；不以 0 个冒充）</small>`;
    }
    const keys = read.payload.keys ?? [];
    const attributed = read.payload.coverage?.attributed;
    const unattributed = read.payload.unattributed;
    return `<small>已归属 Key：${fmtRequests(keys.length)} 个（账本 keys[] 计数）· 已归因请求 ${typeof attributed === "number" ? fmtRequests(attributed) : "未提供"}${unattributed ? " · 另有未归属桶单列，不摊到任何 Key" : ""}</small>`;
  }
  /** Lifecycle buttons per state (A36-1 block ①). All arm a confirm first;
   *  backend_login is direct like the account chain (pops the real browser
   *  window the maintainer completes). */
  private shareActionButtonsHtml(payload: ShareStatusPayload): string {
    const state = this.shareStateOf(payload);
    const accountInvalid = payload.account?.valid === false;
    const startBtn = state.key === "closed" || state.key === "closing-residue" || state.key === "unconfirmed"
      ? `<button type="button" class="wb-btn" data-wb-share-action="share.start">开启分享</button>`
      : "";
    const stopBtn = state.key === "open" || state.key === "unconfirmed"
      ? `<button type="button" class="wb-btn" data-wb-share-action="share.stop">停止分享</button>`
      : "";
    const retryStopBtn = state.key === "closing-residue"
      ? `<button type="button" class="wb-btn" data-wb-share-action="share.retry_stop">继续关闭</button>`
      : "";
    const estopBtn = `<button type="button" class="wb-btn wb-danger" data-wb-share-action="share.estop">紧急停止</button>`;
    const loginBtn = accountInvalid
      ? `<button type="button" class="wb-btn" data-wb-share-action="share.backend_login">登录分享专用账号</button>`
      : "";
    return [startBtn, stopBtn, retryStopBtn, estopBtn, loginBtn].filter(Boolean).join("");
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
    return `<table class="wb-share-key-table"><thead><tr><th>名称</th><th>API Key</th><th>启用</th><th>每 Key 并发</th><th>操作</th></tr></thead><tbody>`
      + rows.map(row => {
        const id = row.id ?? "";
        const enabled = row.enabled === true;
        // Inline edit (R9-R46): the edited cell swaps to an input + 保存/取消
        // in place of the window.prompt flow; only one cell edits at a time.
        const editing = this.shareKeyEdit?.id === id ? this.shareKeyEdit : undefined;
        const nameCell = editing?.field === "name"
          ? `<td><div class="wb-share-edit"><input type="text" data-wb-share-edit value="${escapeHtml(editing.value)}" maxlength="64" aria-label="新名称"/><div class="wb-share-edit-actions"><button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="save-edit" data-wb-share-key-id="${escapeHtml(id)}">保存</button><button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="cancel-edit">取消</button></div></div></td>`
          : `<td>${escapeHtml(row.name ?? "—")}</td>`;
        const capCell = editing?.field === "cap"
          ? `<td><div class="wb-share-edit"><input type="number" data-wb-share-edit min="1" max="64" inputmode="numeric" value="${escapeHtml(editing.value)}" aria-label="每 Key 并发上限（1–64）"/><div class="wb-share-edit-actions"><button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="save-edit" data-wb-share-key-id="${escapeHtml(id)}">保存</button><button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="cancel-edit">取消</button></div></div></td>`
          : `<td>${row.max_concurrency ?? "—"}</td>`;
        return `<tr>${nameCell}`
          + `<td data-share-key-cell="${escapeHtml(id)}">${this.shareKeyValueCellHtml(row)}</td>`
          // 契约 3.5：`wb-dot-ok` / `wb-dot-off` 两个类在 CSS 里不存在，
          // 启用与停用因此渲染成**同一个**中性实心圆，只能靠文字区分。改用
          // 既有形状：启用 = 实心圆，停用 = 空心环，颜色与形状同时区分。
          + `<td><i class="wb-dot wb-dot-${enabled ? "run" : "unknown"}"></i>${enabled ? "启用" : "停用"}</td>`
          + capCell
          + `<td class="wb-share-key-actions">`
          + `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="toggle-enable" data-wb-share-key-id="${escapeHtml(id)}">${enabled ? "停用" : "启用"}</button>`
          + `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="rename" data-wb-share-key-id="${escapeHtml(id)}">改名</button>`
          + `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="adjust-cap" data-wb-share-key-id="${escapeHtml(id)}">调并发</button>`
          + `<button type="button" class="wb-btn wb-btn-sm" data-wb-share-key="rotate" data-wb-share-key-id="${escapeHtml(id)}">轮换</button>`
          + `<button type="button" class="wb-btn wb-btn-sm wb-danger" data-wb-share-key="revoke-terminate" data-wb-share-key-id="${escapeHtml(id)}">撤销并终止</button>`
          + `<button type="button" class="wb-btn wb-btn-sm wb-danger" data-wb-share-key="delete" data-wb-share-key-id="${escapeHtml(id)}">删除</button>`
          + `</td></tr>`;
      }).join("") + `</tbody></table>`;
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
    const existing = main.querySelector<HTMLElement>(".wb-share-modal-backdrop");
    if (!this.shareKeyFormOpen) {
      if (existing) existing.hidden = true;
      return;
    }
    let backdrop = existing;
    if (!backdrop) {
      // position:fixed (not absolute inset:0): .wb-main is an overflow-y:auto
      // scroll container, so a padding-box-anchored backdrop would scroll
      // away with the lane content instead of covering the viewport.
      main.insertAdjacentHTML("beforeend", `<div class="wb-share-modal-backdrop" hidden role="presentation">`
        + `<div class="wb-share-modal" role="dialog" aria-modal="true" aria-label="新增访问密钥">`
        + `<div data-share-modal-view="form">`
        + `<h3 class="wb-share-modal-title">新增访问密钥</h3>`
        + `<form onsubmit="return false">`
        + `<label class="wb-share-modal-field">名称<input type="text" data-wb-share-key-name maxlength="64"></label>`
        + `<label class="wb-share-modal-field">每 Key 并发（1–64，留空用服务默认）<input type="number" min="1" max="64" inputmode="numeric" data-wb-share-key-cap></label>`
        + `<p class="wb-share-error" data-share-modal-error hidden></p>`
        + `<div class="wb-share-key-form-actions"><button type="button" class="wb-btn" data-wb-share-key="submit-create">创建</button><button type="button" class="wb-btn" data-wb-share-key="cancel-create">取消</button></div>`
        + `</form></div>`
        + `<div data-share-modal-view="success" hidden>`
        + `<h3 class="wb-share-modal-title">密钥已创建</h3>`
        + `<p class="wb-share-modal-note" data-share-modal-note></p>`
        + `<code class="wb-share-secret-value" data-share-modal-secret></code>`
        + `<div class="wb-share-key-form-actions"><button type="button" class="wb-btn" data-wb-share-secret-copy title="复制明文（仅本次会话内存，不写任何持久化）" aria-label="复制明文">${wbIcon("copy")}${wbIconHint("复制明文")}</button><button type="button" class="wb-btn" data-wb-share-key="modal-complete">完成</button></div>`
        + `</div>`
        + `</div></div>`);
      const fresh = main.querySelector<HTMLElement>(".wb-share-modal-backdrop");
      if (!fresh) return;
      backdrop = fresh;
      fresh.addEventListener("click", event => {
        if (event.target === fresh) this.closeShareKeyModal();
      });
      fresh.addEventListener("keydown", event => {
        // Escape is NOT handled here: the document-level listener owns it, so a
        // close works even when focus has left the dialog (the old branch here
        // only ran while focus was inside — exactly the reported bug).
        // P2 (2026-09-24): the agreed keyboard contract — Enter in the
        // name field moves focus to the concurrency field, and only the
        // concurrency field's Enter submits. A keydown that is part of an
        // IME composition (isComposing / keyCode 229) belongs to the input
        // method and must never submit.
        if (event.key !== "Enter" || event.isComposing || event.keyCode === 229) return;
        const target = event.target as HTMLInputElement | null;
        if (target?.matches?.("[data-wb-share-key-name]")) {
          event.preventDefault();
          fresh.querySelector<HTMLInputElement>("[data-wb-share-key-cap]")?.focus();
          return;
        }
        if (target?.matches?.("[data-wb-share-key-cap]")) {
          event.preventDefault();
          fresh.querySelector<HTMLButtonElement>("[data-wb-share-key='submit-create']")?.click();
        }
      });
    }
    backdrop.hidden = false;
    const formView = backdrop.querySelector<HTMLElement>('[data-share-modal-view="form"]');
    const successView = backdrop.querySelector<HTMLElement>('[data-share-modal-view="success"]');
    if (formView) formView.hidden = this.shareKeyModalView !== "form";
    if (successView) successView.hidden = this.shareKeyModalView !== "success";
    const secret = backdrop.querySelector<HTMLElement>("[data-share-modal-secret]");
    if (secret && this.sharePlaintext) secret.textContent = this.sharePlaintext.value;
    // P2 (2026-09-24): the success note must match what the backend ACTUALLY
    // stored — a DPAPI failure degrades the row to hash-only, and claiming
    // 明文已写入本机 then would be false for exactly the key in the dialog.
    const note = backdrop.querySelector<HTMLElement>("[data-share-modal-note]");
    if (note) {
      note.textContent = this.shareKeyPlaintextViewable === false
        ? "明文仅在本次响应可见：本机未写入（DPAPI 保护失败），请立即复制；该行会显示「旧密钥不可恢复」，可稍后手动轮换重试。"
        : "明文已写入本机（同一运行身份可回看）：「访问密钥」列的 API Key 单元随时可查看复制；换运行身份或换机器不可回看，轮换会覆盖旧明文。";
    }
    const errorLine = backdrop.querySelector<HTMLElement>("[data-share-modal-error]");
    if (errorLine) {
      errorLine.textContent = this.shareKeyError;
      errorLine.hidden = !this.shareKeyError || this.shareKeyModalView !== "form";
    }
    // P2 (2026-09-24): the create request disables its own submit button
    // until the response lands (the modal now stays open through the request).
    const submit = backdrop.querySelector<HTMLButtonElement>("[data-wb-share-key='submit-create']");
    if (submit) {
      submit.disabled = this.shareKeySubmitting;
      submit.textContent = this.shareKeySubmitting ? "创建中…" : "创建";
    }
    if (this.shareKeyFocusName && this.shareKeyModalView === "form") {
      this.shareKeyFocusName = false;
      // A fresh open always starts empty (the persistent modal node would
      // otherwise carry the previous session's typed values).
      const nameInput = backdrop.querySelector<HTMLInputElement>("[data-wb-share-key-name]");
      const capInput = backdrop.querySelector<HTMLInputElement>("[data-wb-share-key-cap]");
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
    this.root.querySelector<HTMLButtonElement>('[data-wb-share-key="create"]')?.focus();
  }
  /** Copy-button feedback: 「已复制」 for 1.5s, then back. A morph repaint
   *  resets the label within the flash window (same failure family as the
   *  armed confirm), so the flash is also tracked in flashCopiedState and
   *  re-applied by reflashCopied after every rebuild. 契约 3.13b：纯图标
   *  复制按钮没有文本节点，基值因此取 `innerHTML`（取 textContent 会把
   *  图标在闪动结束时永久抹掉）；`aria-label` 不随反馈抖动改写。 */
  private flashCopied(button: HTMLElement) {
    const base = button.dataset.wbBaseLabel ?? button.innerHTML;
    const sel = button.dataset.wbShareSecretCopy !== undefined
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
  /** Share URL + five-layer preconditions (block ②). URL copyable only when
   *  open; 未开放 marked honestly — never a fabricated address (A36-2).
   *  An UNANSWERED read is neither: with no payload yet the block says
   *  读取中… instead of asserting 「不可取得」 or 「当前未开放」, and only an
   *  answered read with funnel_on !== true prints 当前未开放.
   *  读取中的占位件带 `data-state="loading"`（与日志 lane 的
   *  `.wb-log-state` 与操作账本的 `data-state="loading"` 同一条约定）：
   *  自动检查据此知道本区块的只读数据尚未到位，不会把「还在读」的
   *  那一帧当成布局落定。 */
  private shareUrlHtml(status?: ShareStatusState): string {
    if (!status) {
      return `<p class="wb-muted" data-state="loading">分享地址与准备条件读取中…（请求未返回前不写「不可取得」，也不写「当前未开放」）。</p>`;
    }
    // An ANSWERED failure is not 「读取中」. This block used to key only on the
    // payload, so a 503 left it on 读取中… forever while the hero above said
    // 读取失败 — a user could not tell 「还在读」 from 「已经失败」. The reason is
    // the hero's own state read a second time, never a new guess.
    if (status.state === "loading") {
      return `<p class="wb-muted" data-state="loading">分享地址与准备条件读取中…</p>`;
    }
    if (status.state !== "live") {
      return `<p class="wb-muted">分享地址与准备条件读取失败：${this.shareReadFailureText(status)}；不以「读取中」冒充未完成，也不以文档冒充状态。</p>`;
    }
    const payload = status.payload;
    const url = payload.share_url;
    const open = payload.funnel_on === true;
    const accountLine = payload.account
      ? payload.account.valid === true ? "专用账号认证有效"
        : payload.account.valid === false ? `专用账号认证无效${payload.account.failure ? `（${escapeHtml(payload.account.failure)}）` : ""}`
        : "专用账号认证状态不可取得"
      : "专用账号认证不可取得";
    const blocked = payload.blocked_publications;
    const blockedCount = Array.isArray(blocked) ? blocked.length : null;
    const pubFail = payload.last_publication_failure;
    const pubFailLine = pubFail && typeof pubFail === "object"
      ? `${escapeHtml(String(pubFail.error ?? "发布失败")).slice(0, 120)}（${escapeHtml(String(pubFail.at ?? "")).slice(0, 19)}）`
      : "";
    const keyLine = payload.active_keys !== undefined
      ? payload.active_keys > 0
        ? `已启用 ${payload.active_keys} / 共 ${payload.total_keys ?? "?"}`
        : `<b class="wb-stale-warn">缺有效 Key（active ${payload.active_keys ?? 0} / total ${payload.total_keys ?? "?"}）——可在下方「访问密钥」创建；不自动创建正式 Key、不复活已删除 Key</b>`
      : "不可取得";
    const syncDetail = payload.in_sync === false
      ? `不一致（desired ${payload.desired_generation ?? "?"} / applied ${payload.applied_generation ?? "?"}${payload.pending_revocation ? " · 有未确认撤销发布" : ""}${blockedCount !== null && blockedCount > 0 ? ` · ${blockedCount} 条发布受阻` : ""}${pubFailLine ? ` · 最近失败：${pubFailLine}` : ""}）；安全恢复路径：share.retry_stop 按剩余步骤继续关闭/确认（不要求先重开）；发布不自动重发`
      : payload.in_sync === true ? "同步" : "不可取得";
    return `<div class="wb-share-url-row">`
      + (typeof url === "string" && url
        ? `<code class="wb-share-url">${escapeHtml(url)}</code><button type="button" class="wb-btn wb-btn-sm" data-wb-share-copy="${escapeHtml(url)}" title="复制分享地址" aria-label="复制分享地址">${wbIcon("copy")}${wbIconHint("复制分享地址")}</button>`
        : `<code class="wb-share-url wb-share-url-unavailable">不可取得（未开放时不伪造地址）</code>`)
      + (open ? "" : ` <small class="wb-share-closed-mark">当前未开放</small>`)
      + `</div>`
      + `<details class="wb-share-preconditions wb-secondary-details"${this.sharePreconditionsOpen ? " open" : ""} data-wb-share-preconditions="1"><summary title="准备条件明细（默认折叠 · 可执行项与未知分层）· 点击展开 / 收起">准备条件${wbFoldMark()}</summary><ul>`
      + `<li>有效密钥：${keyLine}${payload.active_keys === 0 ? "（测试 Key 仅在受控验证窗口内创建，明文在页面会话内可重复查看，完成/失败/超时后撤销）" : ""}</li>`
      + `<li>发布同步：${syncDetail}</li>`
      + `<li>broker 运行版本：${payload.broker ? (payload.broker.running ? `运行中（${payload.broker.version ?? "版本不可取得"}）` : "未运行") : "不可取得"}</li>`
      + `<li>实例/上游通道：${payload.upstream?.reachable === true ? "可达" : payload.upstream ? "不可达" : "不可取得"}</li>`
      + `<li>${accountLine}</li>`
      + `<li>公网入口：${payload.funnel_on === true ? "开放" : payload.funnel_on === false ? "关闭" : "不可取得"}</li>`
      + `</ul><p class="wb-muted">链层核验（专用后端认证 / TS / 连接 / Funnel）由开启链逐层执行——share.start 是权威核验者，本页「不可取得」不冒充链层状态。外部可达性由维护者另一设备协助验证。</p></details>`;
  }
  /** The SAME status answer's failure state, spelled for the three blocks that
   *  read it: the hero (开放状态与主操作) writes the measured reason exactly
   *  once, and blocks ② (分享地址与准备条件) and ④ (容量与近期操作) point
   *  at it instead of repeating it — 「还在读」 and 「已经失败」 stay
   *  distinguishable (they used to sit on 读取中… forever after a 503 while the
   *  page promised 「失败时由上方状态块写出失败原因」), but the same failure
   *  reason is not printed twice on one page (维护者 2026-09-28 的 ④：状态在
   *  页面上只允许出现一次). */
  private shareReadFailureText(status: ShareStatusState): string {
    if (status.state === "gated") return "无凭据会话（8096 不可达或未授权）";
    if (status.state === "refused") return "403 权限不符";
    if (status.state === "error") return "原因见上方「开放状态与主操作」";
    return "读取未完成";
  }
  /** Capacity + recent share ops (block ④). Global limit and per-key caps
   *  shown separately; recent ops filtered to share.* only — when crowded
   *  out of the 8-row window the page says so, never backfills history
   *  (A36-1: 总览不回填最近操作列表). */
  private shareCapacityHtml(status?: ShareStatusState): string {
    const payload = status?.state === "live" ? status.payload : undefined;
    const cap = typeof payload?.share_max_concurrency === "number" ? payload.share_max_concurrency : undefined;
    const active = typeof payload?.active_keys === "number" ? payload.active_keys : undefined;
    const total = typeof payload?.total_keys === "number" ? payload.total_keys : undefined;
    const recent = this.opsRecent?.state === "live"
      ? this.opsRecent.rows.filter(op => op.action.startsWith("share."))
      : [];
    // An unanswered share-status read is not 「不可取得」: the limits line says
    // 读取中… until the request answers. A FAILED read says so here as well —
    // it used to keep 读取中… forever after a 503 (the state the hero above
    // names), so 「还在读」 and 「已经失败」 were indistinguishable. The
    // recent-ops list is an INDEPENDENT read and keeps its own honest state
    // either way.
    let limits: string;
    if (payload) {
      limits = `<div class="wb-share-limits"><div><small>全局分享并发上限</small><b>${cap ?? "不可取得"}</b></div>`
        + `<div><small>启用密钥 / 密钥总数</small><b>${active ?? "—"} / ${total ?? "—"}</b></div></div>`;
    } else if (status && status.state !== "loading") {
      limits = `<p class="wb-muted">全局分享并发上限与密钥计数读取失败：${this.shareReadFailureText(status)}；不以「读取中」冒充未完成，也不以数字冒充读数。</p>`;
    } else {
      limits = `<p class="wb-muted" data-state="loading">全局分享并发上限与密钥计数读取中…（未返回前不写「不可取得」）。</p>`;
    }
    return limits
      + `<h4>近期分享操作（仅本页相关记录）</h4>`
      + (this.opsRecent === undefined
        ? `<p class="wb-muted" data-state="loading">操作账本读取中…（未返回前不写「不可读」，也不写「没有记录」）。</p>`
        : this.opsRecent.state !== "live"
        ? `<p class="wb-muted">${escapeHtml(this.opsReadFailureText())}；无权威来源，不以文档冒充记录。</p>`
        : !recent.length
          ? `<p class="wb-muted">最近 8 条操作中没有分享相关记录；更早的操作请到「日志」lane 核对（本页不回填历史操作列表）。</p>`
          : `<ul class="wb-share-recent">${recent.map(op => `<li><b>${escapeHtml(op.action)}</b><small> · ${this.opTime(op.created_at)} · ${escapeHtml(op.state)}${op.error?.message ? ` · ${escapeHtml(op.error.message)}` : ""}</small></li>`).join("")}</ul>`);
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
    const shown = this.shareSecretHidden ? "••••••••••••••••" : escapeHtml(p.value);
    return `<div class="wb-share-secret" role="status">`
      + `<strong>${p.kind === "created" ? "密钥已创建" : "密钥已轮换"} · ${escapeHtml(p.name)} · 明文会话内可随时查看</strong>`
      + `<code class="wb-share-secret-value${this.shareSecretHidden ? " wb-share-secret-masked" : ""}">${shown}</code>`
      + `<div class="wb-share-secret-actions">`
      + `<button type="button" class="wb-btn" data-wb-share-secret-copy title="复制本次会话内的明文" aria-label="复制本次会话内的明文">${wbIcon("copy")}${wbIconHint("复制本次会话内的明文")}</button>`
      + `<button type="button" class="wb-btn" data-wb-share-secret-toggle>${this.shareSecretHidden ? "显示" : "隐藏"}</button>`
      + `<button type="button" class="wb-btn wb-danger" data-wb-share-secret-clear>清除</button>`
      + `</div>`
      + `<small>明文仅保存在本页面内存中，本次会话内可随时查看与复制；不写入 localStorage、不进操作记录。「访问密钥」列的明文回看走按 Key 端点（同一运行身份，跨页面会话可回看）；旧密钥需手动轮换后才可见。</small>`
      + `</div>`;
  }
  /** Operation state → the honest Chinese word the ledger rows render. Shared
   *  by the failure groups and the full list so the two views of the SAME 8096
   *  ledger can never disagree about what one row says. `cancelled` /
   *  `interrupted` are the backend's own terminal states
   *  (console_operations.py TERMINAL_STATES) and are named as such instead of
   *  falling through in English. */
  private opStateText(state: string): string {
    return ({ succeeded: "成功", failed: "失败", queued: "排队中", running: "执行中",
      cancelled: "已取消", interrupted: "被中断（服务重启）" } as Record<string, string>)[state] ?? state;
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
    const nonTerminal = op.state === "queued" || op.state === "running" || op.state === "pending";
    return op.cancel_requested === true && nonTerminal
      ? ` · 已请求取消（受理 ≠ 已取消，终态以账本为准）` : "";
  }
  /** Operation-record lane main area (contract 4.4): the FULL 8096 operation
   *  ledger window, newest first — the same rows the failure groups above are a
   *  filtered view of. It is never a second ledger: an unreachable 8096 says so
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
      const spec = this.actions?.find(a => a.action === op.action);
      if (!spec) return ` <button type="button" class="wb-btn wb-btn-sm" disabled title="动作注册表未读取：无法确认该动作可取消（不猜一个状态）">取消</button>`;
      if (!spec.cancelable) return ` <button type="button" class="wb-btn wb-btn-sm" disabled title="该动作后端标明不可取消（动作注册表 cancelable=false）">取消</button>`;
      if (this.cancelInFlight.has(op.id)) return ` <button type="button" class="wb-btn wb-btn-sm" disabled title="取消请求在途">取消中…</button>`;
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
    return `<section class="wb-block wb-char"><h3>操作账本（${live ? `最近 ${rows.length} 条 · 8096` : loading ? "读取中 · 未知 ≠ 0" : "本次未读到 · 未知 ≠ 0"}）</h3>`
      + `<p class="wb-muted wb-lane-note">${escapeHtml(this.failuresNote(true))}</p>`
      + this.laneNoteFoldHtml("ops-cancel", "失败分组与取消口径", `<p class="wb-muted">上方失败分组是同一账本按 action 聚合的过滤视图，不是另一份错误账本。取消只对后端标明可取消且仍在运行/排队的操作开放；受理（requested）只写 cancel_requested，「已请求取消」不等于「已取消」——终态由操作账本回答。</p>`)
      + (loading
        ? `<div class="wb-empty-card" data-state="loading"><strong>操作账本读取中</strong><small>8096 操作账本尚未返回；未返回前不写「不可读」，也不写「没有操作」。</small></div>`
        : live
        ? (rows.length
          ? `<div class="wb-list">${rows.map(op => `<div class="wb-op"><span class="wb-op-time">${this.opTime(op.created_at)}</span><span>${escapeHtml(op.action)}</span><b>${escapeHtml(op.target || "—")} · ${escapeHtml(this.opStateText(op.state))}${this.opCancelMark(op)}</b><i class="wb-dot wb-dot-${this.opDotCls(op.state)}" aria-hidden="true"></i>${cancel(op)}</div>`).join("")}</div>`
          : `<div class="wb-empty-card"><strong>账本已读取，暂无操作记录</strong><small>只读查询不计入操作账本；真实启停与账号动作会在此留痕。</small></div>`)
        : `<div class="wb-empty-card"><strong>操作账本不可读</strong><small>${escapeHtml(this.opsReadFailureText())}（未知，不是「没有操作」）。</small><button type="button" class="wb-btn wb-btn-sm" data-wb-ops-retry="1" title="重新读取 8096 操作账本（只读，不产生操作行）" aria-label="重试读取操作账本">${wbIcon("refresh")}${wbIconHint("重试读取操作账本")}</button></div>`)
      + this.opStatusHtml()
      + `</section>`;
  }
  private failuresLaneHtml(all = false) {
    // 账本不可读时不得断言空：原先 `sessionLive` 把「运行快照可读」也算作
    // 账本可读，于是 8096 操作账本读失败时，同一页一边写「操作账本不可读」
    // 一边写「暂无操作记录」/「没有失败的操作」——把未知写成了零。
    const live = this.opsRecent?.state === "live";
    // 读取在途（`undefined`）与读取失败（`gated`）分开：在途时不得先把结论
    // 写成「不可读 / 本次未读到」，只有读取真的失败了才写失败句与实测原因。
    const loading = this.opsRecent === undefined;
    const rows = live ? this.opsRecent!.rows : [];
    const scoped = all ? rows : rows.filter(op => op.state === "failed");
    // 两个区块共用同一句「不可读」会变成同一解释写两遍（§2.11a.2），
    // 因此按区块各自表述：失败分组说「没有失败的操作」，账本列表说「没有操作」。
    const unreadable = `<div class="wb-empty-card"><strong>${all ? "操作账本不可读" : "失败分组不可读"}</strong><small>${escapeHtml(this.opsReadFailureText())}（未知，不是「${all ? "没有操作" : "没有失败的操作"}」）。</small><button type="button" class="wb-btn wb-btn-sm" data-wb-ops-retry="1" title="重新读取 8096 操作账本（只读，不产生操作行）" aria-label="重试读取操作账本（失败分组）">${wbIcon("refresh")}${wbIconHint("重试读取操作账本（失败分组）")}</button></div>`;
    const loadingCard = `<div class="wb-empty-card" data-state="loading"><strong>${all ? "操作账本读取中" : "失败分组读取中"}</strong><small>8096 操作账本尚未返回；未返回前不写「不可读」，也不写「${all ? "没有操作" : "没有失败的操作"}」。</small></div>`;
    const stateText = (state: string) => this.opStateText(state);
    const dotCls = (state: string) => this.opDotCls(state);
    // R9-R40 task 2: failures aggregate by action — a group chip shows the
    // action and its failure count, clicking unfolds the rows. Honesty
    // stays: whatever failed is listed, nothing is folded away silently.
    const opLine = (op: OpRow) =>
      `<div class="wb-op"><span class="wb-op-time">${this.opTime(op.created_at)}</span><span>${escapeHtml(op.action)} → ${escapeHtml(op.target || "—")}</span><b>${escapeHtml(stateText(op.state))}${op.error?.code ? ` · ${escapeHtml(op.error.code)}` : ""}${op.error?.message ? ` · ${escapeHtml(op.error.message)}` : ""}</b><i class="wb-dot wb-dot-${dotCls(op.state)}" aria-hidden="true"></i></div>`;
    const body = loading ? loadingCard
      : !live ? unreadable
      : all
      ? (scoped.length ? `<div class="wb-list">${scoped.map(op => `<div class="wb-op"><span class="wb-op-time">${this.opTime(op.created_at)}</span><span>${escapeHtml(op.action)}</span><b>${escapeHtml(op.target || "—")} · ${escapeHtml(stateText(op.state))}${this.opCancelMark(op)}</b><i class="wb-dot wb-dot-${dotCls(op.state)}" aria-hidden="true"></i></div>`).join("")}</div>`
        : `<div class="wb-empty-card"><strong>账本已读取，暂无操作记录</strong><small>只读查询不计入操作账本；真实启停与账号动作会在此留痕。</small></div>`)
      : (scoped.length
        ? (() => {
          const groups = new Map<string, OpRow[]>();
          for (const op of scoped) {
            const list = groups.get(op.action) ?? [];
            list.push(op);
            groups.set(op.action, list);
          }
          return `<div class="wb-fail-groups">${Array.from(groups.entries()).map(([action, ops]) => {
            const on = this.failuresOpen.has(action);
            const detail = on ? `<div class="wb-fail-detail">${ops.map(opLine).join("")}</div>` : "";
            return `<div class="wb-fail-group"><button type="button" class="wb-fail-head${on ? " wb-fail-on" : ""}" data-wb-fail-toggle="${escapeHtml(action)}" aria-expanded="${on}"><i class="wb-dot wb-dot-down" aria-hidden="true"></i><span>${escapeHtml(action)}</span><b>失败 ×${ops.length}</b></button>${detail}</div>`;
          }).join("")}</div>`;
        })()
        : `<div class="wb-empty-card"><strong>账本已读取，没有失败的操作</strong><small>失败操作来自 8096 操作账本；为空即如实显示空态，不以文档冒充告警。</small></div>`);
    return `<p class="wb-muted lane-note">${this.failuresNote(all)}</p>${body}`;
  }

  /** One service row (R20-07): the only entry point is the real management
   *  panel — no archive-document detour. Registry-driven start/stop buttons;
   *  the usage host has no start/stop by contract. */
  /** Runtime-state badge for one service row (进程层, visible at first
   *  glance instead of hidden in a detail panel). The 8096 host row has no
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
      case "Running": return { text: "运行中", cls: "ok" };
      case "Degraded": return { text: "降级", cls: "warn" };
      case "Conflict": return { text: "端口冲突", cls: "danger" };
      case "Residual": return { text: "受管残留", cls: "warn" };
      case "Stopped": return { text: "已停止", cls: "idle" };
      default: return { text: state, cls: "idle" };
    }
  }
  /** Registry remains authoritative even when the compact row shows one action.
   *  契约 3.13b：主操作与危险动作**必须在按钮内有文字**（图标只做前缀），
   *  启停因此不改成纯图标；`wb-write-mark` 的写入边界标注同样保留文字。 */
  private servicePowerButton(s: ServiceRow, kind: "start" | "stop" | "restart"): string {
    const findAction = (name: string) => this.actions?.find(a => a.action === name);
    const label = kind === "start" ? "启动" : kind === "stop" ? "停止" : "重启";
    const icon = kind === "start" ? "play" : kind === "stop" ? "stop" : "restart";
    const row = findAction(kind === "start" ? "service.start" : kind === "stop" ? "service.stop" : "service.restart");
    if (row && !row.enabled) return `<button disabled title="${escapeHtml(row.disabled_reason ?? "动作已停用")}">${wbIcon(icon)}${label}</button>`;
    return `<button data-wb-svc-action="${kind}" data-wb-target="${s.targets?.[0] ?? ""}" title="经 8096 适配层真实${label}（授权目标集；需二次确认）${kind === "restart" ? "；重启会中断该服务在途工作" : ""}">${wbIcon(icon)}${label}</button>`;
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
    return `<button type="button" class="wb-md-item" data-wb-svc-detail="1" data-wb-detail="${s.id}" data-wb-md-item="${s.id}" data-wb-probe="${s.id}" data-wb-base-title="${escapeHtml(base)}" aria-pressed="${on}" aria-controls="wb-md-detail" title="${escapeHtml(`${base} · ${this.probeEvidence(s.id)}`)}">`
      + `<i class="wb-dot wb-dot-${this.dotState(s.id)}" aria-hidden="true"></i>`
      + `<span class="wb-md-name">${escapeHtml(s.name)}</span>`
      + `<span class="wb-md-state"><span class="wb-badge wb-badge-${badge.cls}">${escapeHtml(badge.text)}</span>${pending ? `<span class="wb-md-pending">${escapeHtml(pending)}</span>` : ""}</span>`
      + `<span class="wb-md-port">:${s.port}</span>`
      + (chip ? `<span class="wb-md-result">${chip}</span>` : "")
      + `</button>`;
  }
  /** An action on this chain that is still in flight, as one short phrase
   *  (「停止服务 · 进行中」); empty when none. */
  private svcPendingText(s: ServiceRow): string {
    for (const entry of this.feedbackEntries.values()) {
      if (entry.state !== "preparing" && entry.state !== "queued" && entry.state !== "running") continue;
      if (this.laneKey(entry.action, entry.target) !== `svc${s.id}`) continue;
      // The chain's own button word (「查询登录状态」) reads better than the
      // generic label, which is the bare action id for account reads.
      const own = [s.statusAction, ...(s.manageActions ?? [])].find(a => a?.action === entry.action);
      return `${own?.label ?? entry.label} · 进行中`;
    }
    return "";
  }
  /** Per-service management detail (R9-R25): runtime snapshot, probe
   *  evidence, account-status + manage actions, and real start/stop. The
   *  four state layers (process/health/account/session) are shown layered,
   *  not merged. */
  private serviceDetailHtml(id: number, pane: "overview" | "workspace"): string {
    const s = serviceById(id);
    if (!s || s.id === CONSOLE_HOST_ID) return "";
    const runtime = this.runtime?.[s.targets?.[0] ?? ""];
    const runtimeRead = this.opsRecent?.state === "live" || Boolean(this.runtime);
    const runtimeHtml = runtimeRead
      ? (runtime
        ? `<div class="wb-kv"><span>运行状态</span><b>${escapeHtml(runtime.state)} · ${escapeHtml(runtime.detail)}</b></div><div class="wb-kv"><span>进程 PID</span><b>${runtime.process_ids.length ? runtime.process_ids.join(", ") : "—"}</b></div>`
        : `<p class="wb-muted">运行快照读取中或暂缺（不冒充正常）。</p>`)
      : `<p class="wb-muted">运行快照读取中或暂缺（8096 runtime 快照）。</p>`;
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
    const layers = `<p class="wb-muted">运行状态（进程）、探针健康与账号认证是三个独立状态层；本面板分层显示，不混同。各动作的真实说明在其悬停提示中。</p>`;
    // Overview pane (PR11): the same facts as the inline detail, laid out as the
    // panel's head, four short readings, the account status and one action bar.
    // Runtime readings that are not read yet stay 「读取中或暂缺」, never a default.
    const badge = this.runtimeBadge(s);
    const pids = runtimeRead && runtime ? (runtime.process_ids.length ? runtime.process_ids.join(", ") : "—") : (s.targets?.length ? "读取中或暂缺" : "—");
    const runWord = runtimeRead && runtime ? `${runtime.state} · ${runtime.detail}` : (s.targets?.length ? "读取中或暂缺" : "控制台宿主，无运行快照");
    // Inside the gateway workspace the detail sits under the workspace's own head
    // (name, port, state), so it starts at its readings.
    const head = pane === "workspace" ? ""
      : `<div class="wb-md-head"><h4>${escapeHtml(s.name)}</h4><span class="wb-badge wb-badge-${badge.cls}">${escapeHtml(badge.text)}</span><span class="wb-port">端口 ${s.port}</span></div>`;
    return `<div class="wb-md-detail"${pane === "workspace" ? "" : ` id="wb-md-detail"`} role="region" aria-label="${escapeHtml(s.name)} · 管理详情">`
      + head
      + `<div class="wb-md-tiles"><div><small>探针证据</small><b>${escapeHtml(this.probeEvidence(s.id))}</b></div><div><small>运行状态</small><b>${escapeHtml(runWord)}</b></div><div><small>进程 PID</small><b>${escapeHtml(pids)}</b></div><div><small>说明</small><b>${escapeHtml(s.note)}</b></div></div>`
      + authHtml
      + `<div class="wb-md-actions">${accountHtml}${logHtml}${powerHtml}</div>`
      + layers
      + `</div>`;
  }
private serviceLogSource(id: number): string { return id === 0 ? "chat2api" : "console"; }
  private opTime(created: number | string): string {
    const v = Number(created);
    if (!Number.isFinite(v) || v <= 0) return "—";
    return new Date(v * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  }
  private opStatusHtml(): string {
    return this.opStatus ? `<p class="wb-op-status" data-state="${this.opStatus.state}">${escapeHtml(this.opStatus.text)}</p>` : "";
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
    const state = latest.state === "succeeded" ? "ok" : latest.state === "failed" ? "failed" : "pending";
    return `<p class="wb-op-status" data-state="${state}"><i class="wb-dot wb-dot-${stateDot(latest.state)}" aria-hidden="true"></i>${escapeHtml(latest.label)} · ${escapeHtml(stateText(latest.state))}${latest.detail ? ` · ${escapeHtml(latest.detail)}` : ""}</p>`;
  }
  /** Patch one feedback entry in place and repaint the notice pool only —
   *  never the panel (renderNotices must not call renderPanel). */
  private fbUpdate(key: string, state: FeedbackState, text: string, pinned = false) {
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
  private fbFinish(key: string, state: FeedbackState, text: string,
                   pinned = false, detail = "", repaint = true) {
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
    const line = entry.text.startsWith(entry.label) ? entry.text : `${head} ${entry.text}`;
    this.opStatus = {
      state: entry.state === "succeeded" ? "ok" : entry.state === "failed" ? "failed" : "pending",
      text: entry.detail ? `${line}。${entry.detail}` : line,
    };
  }
  /** Global notice pool + system banner (R9-R37 A37): at most three cards,
   *  newest first, overflow as a compact chip; 8096-unreachable class
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
    let pool = document.querySelector<HTMLElement>(".wb-notices[data-wb-notice-pool]");
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
    // a 5xx / unparsable / mis-shaped body means 8096 DID answer, so the
    // banner must not claim it was unreachable — the lane body writes the same
    // fact with its HTTP status, and the two must agree.
    const bannerDue = this.summaryState === "error" || this.summaryState === "unauthenticated";
    if (!bannerDue) this.bannerDismissed = false;
    const showBanner = bannerDue && !this.bannerDismissed;
    banner.hidden = !showBanner;
    banner.innerHTML = showBanner
      ? `<span>⚠ ${escapeHtml(this.summaryCause || "费用聚合读取失败")}——服务/操作数据暂不可用，恢复后自动接入</span><button type="button" data-wb-banner-dismiss="1" title="隐藏此提示">知道了</button>`
      : "";
    // 维护者 2026-09-28（Plan-Revision 9 逐页收纳 · 日志 / 操作页）：瞬时通知池最多 2 条
    // （原先 3 条，第 3 条会盖到页头时点与筛选行上）；多出来的只写一行计数，
    // 不静默丢弃——「还有 N 项」本身就是常驻事实。
    const visible = [...this.feedbackEntries.values()].filter(isVisibleState).sort((a, b) => b.updatedAt - a.updatedAt);
    const cards = visible.slice(0, 2).map(entry => {
      const closable = entry.pinned || entry.state === "failed" || entry.state === "unconfirmed";
      return `<div class="wb-notice"><i class="wb-dot wb-dot-${stateDot(entry.state)}" aria-hidden="true"></i>`
        + `<div class="wb-notice-body"><div class="wb-notice-head"><b>${escapeHtml(entry.label)}</b>`
        + `<span class="wb-notice-target">${escapeHtml(entry.target || "—")} · ${escapeHtml(stateText(entry.state))}</span></div>`
        + `<div class="wb-notice-text">${escapeHtml(entry.text)}</div>`
        + (entry.detail ? `<div class="wb-notice-detail">${escapeHtml(entry.detail)}</div>` : "")
        + `</div><div class="wb-notice-side"><small class="wb-notice-time">${escapeHtml(this.feedbackElapsed(entry.startedAt))}</small>`
        + (closable ? `<button type="button" class="wb-notice-close" data-wb-notice-dismiss="${escapeHtml(entry.key)}" aria-label="收起这条通知" title="收起">×</button>` : "")
        + `</div></div>`;
    }).join("");
    pool.innerHTML = visible.length > 2
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
    if (secs >= 3600) return `${Math.floor(secs / 3600)} 时 ${Math.floor((secs % 3600) / 60)} 分`;
    if (secs >= 60) return `${Math.floor(secs / 60)} 分 ${secs % 60} 秒`;
    return `${secs} 秒`;
  }
  /** Two-step arm: first click arms the button (visual confirm), a second
   *  click on the SAME button executes. Any other click disarms. */
  private armConfirm(button: HTMLElement, action: "start" | "stop" | "restart") {
    this.armConfirmLabel(button,
      action === "start" ? "确认启动？" : action === "stop" ? "确认停止？" : "确认重启？");
  }
  private armConfirmLabel(button: HTMLElement, armedText: string) {
    this.clearConfirm();
    this.confirmBtn = button;
    const action = button.dataset.wbModelAction ?? button.dataset.wbSvcAction ?? button.dataset.wbAcctAction ?? button.dataset.wbMsAction;
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
    return this.confirmBtn === button && this.armedConfirm?.key === confirmKey(button);
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
    const button = [...this.root.querySelectorAll<HTMLButtonElement>("button[data-wb-share-action], button[data-wb-share-key]")]
      .find(b => shareConfirmKey(b) === armed.key && !b.disabled);
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
    const keep = this.confirmBtn?.isConnected && confirmKey(this.confirmBtn) === armed.key ? this.confirmBtn : undefined;
    const button = keep ?? [...this.root.querySelectorAll<HTMLElement>(armed.sel)].find(b => confirmKey(b) === armed.key);
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
  /** Submit one real adapter action through the 8096 layer. The cookie
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
  private async submitAction(actionName: string, target: string, params?: Record<string, unknown>) {
    assertPublicOperation(actionName, target, params ?? {});
    const label = actionLabel(actionName);
    const key = `local-${Date.now()}-${++this.feedbackSeq}`;
    const startedAt = Date.now();
    const entry: FeedbackEntry = {
      key, action: actionName, label, target,
      state: "preparing",
      text: `${label} → ${target || "—"} 准备提交…`,
      detail: "", startedAt, updatedAt: startedAt, pinned: false,
    };
    this.feedbackEntries.set(key, entry);
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
    if (this.lane === 0 && this.laneKey(actionName, target).startsWith("svc")) this.renderPanel();
    // A15-3: the single-layer buttons lock while any share operation is in
    // flight (no second submit from this page).
    if (this.lane === 6 && actionName.startsWith("share.")) this.renderPanel();
    try {
      const response = await consoleFetch("/api/console/operations", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
        body: JSON.stringify({ action: actionName, target, ...(params ? { params } : {}) }),
      });
      const data = await response.json().catch(() => null) as { operation?: OpRow; error?: string; reason?: string; detail?: string } | null;
      if (!response.ok) {
        // usagesrv only reads `reason` (an OperationUnavailable attribute),
        // while a param rejection is an OperationError whose text lives on
        // `message` — the response therefore carries the code in `error` and the
        // backend's OWN safety-filtered text in `detail` (paths, long tokens and
        // absolute paths are dropped server-side). `reason` still wins when the
        // backend sends it; with no controlled field the wording stays the bare
        // code exactly as before.
        const code = data?.error ?? "";
        const reason = data?.reason || this.rejectDetailText(code, data?.detail)
          || code || `HTTP ${response.status}`;
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
        this.setLaneSummary(this.laneKey(actionName, target),
          `${label} → ${target || "—"} 被拒绝：${reason}`, "failed");
        this.fbFinish(key, "failed", `被拒绝：${reason}`, true);
        return;
      }
      const op = data?.operation;
      if (!op?.id) {
        this.releaseSpeedBusy(actionName);
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
      await this.pollOperation(op.id, actionName, target);
    } catch {
      // P2 (2026-09-24): the transport-level catch is a submit failure
      // too — the operation may never have been accepted, so the speedtest
      // busy marker must be released here as well.
      this.releaseSpeedBusy(actionName);
      this.setLaneSummary(this.laneKey(actionName, target),
        `${label} → ${target || "—"} 结果待确认：连接失败——可能已被受理，不自动重发`, "unconfirmed");
      this.fbFinish(key, "unconfirmed", "结果待确认", true, "连接失败——可能已被受理，不自动重发；稍后核对操作账本");
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
      sources: "来源", consumers: "客户端", models: "模型", probe: "探测开关",
      key: "模型键", source: "来源", model: "模型", max_tokens: "最大 token",
      limit: "条数", action: "动作", target: "目标",
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
  /** Poll one accepted operation to a terminal state (60s deadline). Live
   *  states only patch the notice pool; terminal states repaint the panel
   *  too (fbFinish). Transport-level uncertainty ends as unconfirmed:
   *  unknown ≠ failed, the 8096 ledger holds the real outcome. */
  private async pollOperation(opId: string, actionName: string, target: string) {
    this.opPollAbort = false;
    const deadline = Date.now() + 60_000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 1200));
      if (this.opPollAbort) return;
      try {
        const response = await consoleFetch(`/api/console/operations/${opId}`, { credentials: "include" });
        if (!response.ok) {
          this.settleSpeedPoll(actionName, opId);
          // 账号页只读查询：状态查询本身失败也要留下实测 HTTP 状态，
          // 否则失败会被写成「未读取」。
          if (REMEMBER_READ_RESULT.includes(actionName)) {
            this.actionResultFailures.set(actionName, `状态查询失败（HTTP ${response.status}）`);
          }
          this.setLaneSummary(this.laneKey(actionName, target),
            `${actionLabel(actionName)} → ${target || "—"} 状态查询失败（HTTP ${response.status}），结果未知，请稍后核对操作账本`, "unconfirmed");
          this.fbFinish(opId, "unconfirmed",
            `${actionLabel(actionName)} → ${target || "—"} 状态查询失败（HTTP ${response.status}），结果未知，请稍后核对操作账本`, true);
          return;
        }
        const data = await response.json().catch(() => null) as { operation?: OpRow } | null;
        const op = data?.operation;
        if (!op) {
          this.settleSpeedPoll(actionName, opId);
          this.setLaneSummary(this.laneKey(actionName, target),
            `${actionLabel(actionName)} → ${target || "—"} 操作记录查询失败，结果未知，请稍后核对操作账本`, "unconfirmed");
          this.fbFinish(opId, "unconfirmed",
            `${actionLabel(actionName)} → ${target || "—"} 操作记录查询失败，结果未知，请稍后核对操作账本`, true);
          return;
        }
        if (op.state === "queued" || op.state === "running") {
          // A15-3: a single-layer action shows its operation id and the latest
          // stage the backend wrote (drain / restart / resume), and the share
          // lane repaints when it changes so the button area follows along.
          const stage = SHARE_LAYER_ACTIONS.includes(actionName)
            ? [...(op.steps ?? [])].reverse().find(s => typeof s.note === "string" && s.note)?.note ?? "" : "";
          const text = (op.state === "queued" ? "排队中…" : "执行中…")
            + (SHARE_LAYER_ACTIONS.includes(actionName) ? `（操作 ${op.id.slice(0, 12)}${stage ? `；${stage.slice(0, 120)}` : ""}）` : "");
          const changed = this.feedbackEntries.get(opId)?.text !== text;
          this.fbUpdate(opId, op.state === "queued" ? "queued" : "running", text);
          if (changed && SHARE_LAYER_ACTIONS.includes(actionName) && this.lane === 6) this.renderPanel();
          continue;
        }
        if (op.state === "succeeded") {
          // 账号页的只读查询（认证状态 / 额度桶）与 model.* 一样记忆：
          // `accountAuthHtml` 读的是这张表，没有写入者时「已认证 / 未认证 /

          if (REMEMBER_READ_RESULT.includes(actionName)) {
            this.actionResults[actionName] = op.result ?? {};
            // 本次读取成功：清掉上一次的失败记忆，否则「读取失败」会一直挂着。
            this.actionResultFailures.delete(actionName);
          }
          // R9-R31: model.* results feed the model lane (catalog tables, sync
          // diffs) — remembered per action instead of only shown once.
          if (actionName.startsWith("model.")) {
            this.actionResults[actionName] = op.result ?? {};
            // A freshly computed preview reflects the post-apply state — the
            // staleness marker left by the last apply no longer applies.
            // 提交时范围快照：这次预览结果按**提交那一刻**的范围算出来
            // （`previewScopePending` 按 op id 取回），面板据此写「本次预览
            // 范围」并在勾选变化时标过期；取不到就写「未知」而不是猜当前勾选。
            if (actionName === "model.sync_preview") {
              this.previewStale = false;
              this.previewScopeParams = this.previewScopePending.get(op.id) ?? null;
              this.previewScopePending.delete(op.id);
            }
            // L-C: per-model speedtest memory (see speedPending mapping).
            if (actionName === "model.speedtest") {
              const ctxRow = this.speedPending.get(op.id);
              if (ctxRow) {
                this.speedResults.set(`${ctxRow.source}|${ctxRow.model}`,
                  { ...(op.result ?? {}), at: Date.now() });
              }
            }
            // L-C auto readback: an apply terminal state invalidates the old
            // preview and re-reads the pending overview read-only — never a
            // re-submitted write, never a charged probe.
            if (actionName === "model.sync_apply") {
              this.previewStale = true;
              this.pendingReadbackFailed = false;
              this.keepScroll = true;
              // 自动回读：这一路失败必须留下回读失败标记（fetchModelPending
              // 的 afterApply），否则「操作已完成、最新状态读取失败」永远不可达。
              // 范围：应用按**提交时快照**回读（同范围）；pin 无范围参数，
              // 按全局概览读（null）。
              const readbackScope = actionName === "model.sync_apply"
                ? this.applyScopeParams : null;
              void this.fetchModelPending(true, readbackScope);
            }
            // Rev8 契约 5/6 接线：保存草稿 / 应用设置的终态后按只读读面回读
            // （model.context_overrides），页面才有「草稿 · 已应用 · 客户端读回」
            // 的真实值；应用终态后草稿已转已应用（或被整体延期），页面会话内
            // 的旧输入不再冒充新草稿，交给读面回答。
            if (actionName === "model.settings_save") {
              void this.submitAction("model.context_overrides", "model");
            }
            if (actionName === "model.settings_apply") {
              const selection = op.result?.selection;
              if (selection && typeof selection === "object" && !Array.isArray(selection)) {
                for (const key of Object.keys(selection)) {
                  this.msAlias.delete(key);
                  this.msCtx.delete(key);
                  this.msCtxTokens.delete(key);
                  this.msEffort.delete(key);
                }
              }
              void this.submitAction("model.context_overrides", "model");
            }
          }
          // R9-R40 task 4, R9-R46 chip rework: the initiating lane block keeps
          // one inline terminal chip until its next action clears it or the
          // 10s auto-fade deletes it (no dual track with the R9-R31
          // actionResults memory).
          const summary = shareLayerSummary(actionName, op) ?? summarizeResult(op.result ?? {});
          this.setLaneSummary(this.laneKey(actionName, target), summary, "ok");
          if (actionName.startsWith("share.")) {
            // R9-R46: share terminal states re-read both share reads, so the
            // lane never keeps its entry snapshot after a lifecycle change.
            void this.refreshShareData();
          }
          // Rev19 A15 维护前后比较：单层重启动作自己的终态结果带回 before /
          // after 同层读数（后端 `_diag_pair()`），页面记下这一次动作的前后，
          // 供「分层维护」卡里比较；结果没带就不写（不重读一遍冒充）。
          if (actionName === "share.backend_restart" || actionName === "share.gateway_restart") {
            const before = op.result?.before, after = op.result?.after;
            this.shareRestartCompare = before && typeof before === "object" && after && typeof after === "object"
              ? { action: actionName, at: Date.now(), before: before as Record<string, unknown>, after: after as Record<string, unknown> }
              : undefined;
          }
          // R11-9: a per-row speedtest terminal state patches its ONE cell
          // in place instead of rebuilding the whole lane (settleSpeedPoll
          // reads the mapping before speedSettle consumes it — P2 2026-09-24).
          const speedCtx = this.settleSpeedPoll(actionName, opId);
          this.fbFinish(opId, "succeeded",
            `${actionLabel(actionName)} → ${target || "—"} 完成`,
            false, summary, !speedCtx);
          return;
        }
        const failedSpeedCtx = this.settleSpeedPoll(actionName, opId);
        // 预览终态失败：它那次提交的范围快照不再等结果，丢掉（同一次预览的
        // 结果只落一次；不把它留给下一次预览的结果）。
        this.previewScopePending.delete(opId);
        const code = op.error?.code ?? op.state;
        const message = op.error?.message ?? "";
        // 账号页只读查询的终态失败留下失败结论（含操作终态错误码/消息）：
        // 不留原因时 `accountAuthHtml` 只能落「未读取」分支，把「没读到」
        // 写成「还没问过」，也把「未知」折成「未认证」。
        if (REMEMBER_READ_RESULT.includes(actionName)) {
          this.actionResultFailures.set(actionName, `失败（${code}${message ? `：${message}` : ""}）`);
        }
        this.setLaneSummary(this.laneKey(actionName, target),
          `${actionLabel(actionName)} → ${target || "—"} 失败（${code}${message ? `：${message}` : ""}）`, "failed");
        if (actionName.startsWith("share.")) void this.refreshShareData();
        this.fbFinish(opId, "failed",
          `${actionLabel(actionName)} → ${target || "—"} 失败`, true,
          `失败（${code}${message ? `：${message}` : ""}）`, !failedSpeedCtx);
        void this.probeTick();
        return;
      } catch {
        continue;
      }
    }
    this.setLaneSummary(this.laneKey(actionName, target),
      `${actionLabel(actionName)} → ${target || "—"} 轮询超时，结果未知（未知 ≠ 失败），请稍后核对操作账本`, "unconfirmed");
    // R11-9 (completion-path integrity): a speedtest that times out still
    // settles its busy marker and patches its ONE cell — the poll deadline
    // must not leave the row disabled until an unrelated repaint.
    const timedOutSpeedCtx = this.settleSpeedPoll(actionName, opId);
    // 轮询超时的预览不会再落结果：丢掉它的提交时快照（未知 ≠ 失败，面板
    // 不拿旧快照冒充这次预览的范围）。
    this.previewScopePending.delete(opId);
    this.fbFinish(opId, "unconfirmed",
      `${actionLabel(actionName)} → ${target || "—"} 轮询超时，结果未知（未知 ≠ 失败），请稍后核对操作账本`, true,
      "", !timedOutSpeedCtx);
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
      const response = await consoleFetch(`/api/console/operations/${encodeURIComponent(opId)}/cancel`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
      });
      const data = await response.json().catch(() => null) as { requested?: boolean; operation?: OpRow; error?: string; detail?: string } | null;
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
