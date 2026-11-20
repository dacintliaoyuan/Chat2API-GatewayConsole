import type { RoleVerdict } from "../console/role-gate";
import type { ShareKeyRow, ShareStatusPayload } from "../console/share-model";
import type {
  CloudSecurityStatus,
  ShareUsage,
  ShareUsageRequestsPage,
  UmLoginRoundsPagePayload,
} from "../console/backend/types";
import type {
  ActionRow,
  CloudHealth,
  CloudSummary,
  DisplaySession,
  DisplayState,
  OperationRow,
  ReadState,
} from "./cloud-reads";

export type ConfirmCopy = {
  title: string;
  body: string;
  confirm: string;
  detail?: string;
  danger?: boolean;
};

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

export type KeyNotice = {
  tone: "ok" | "warn" | "err" | "neutral";
  text: string;
  at: number;
};
export type KeyTest = {
  state: "busy" | "ok" | "error";
  rowVersion?: number;
  reason?: string;
  stage?: string;
  httpStatus?: number;
  model?: string;
  elapsedMs?: number;
  publicIp?: string;
  from?: "public" | "private";
  at?: number;
};
export type KeyUi = {
  busy: Set<string>;
  notices: Map<string, KeyNotice>;
  secrets: Map<
    string,
    { secret: string; rowVersion: number; at: number; revealed: boolean }
  >;
  tests: Map<string, KeyTest>;
  /** The one key whose detail panel is open. */
  open: string | null;
  caps: Map<string, string>;
  createBusy: boolean;
  createIdem: string;
  createError: string | null;
  /** `viewable` is the backend's `plaintext_viewable` for the row just created
   *  or rotated; null means the answer did not report it. A created Key opens in
   *  full once so it can be copied; a rotated Key starts masked until asked. */
  plain: {
    kind: "created" | "rotated";
    name: string;
    value: string;
    at: number;
    hidden: boolean;
    viewable: boolean | null;
  } | null;
};
export type UsageTime = {
  kind: "all" | "today" | "7" | "14" | "30" | "day";
  day: string;
};
export type UsageDimension = "key" | "model" | "day";
export type UsageUi = {
  dimension: UsageDimension;
  key: string;
  model: string;
  time: UsageTime;
  page: number;
  open: string | null;
  /** D18-2: the result filter opened from the failure summary; "" = all. */
  result: string;
};

/** Ticket #9: the request-detail result filter. `unknown` is the ledger's
 *  "result was never recorded" outcome; it is never folded into success. */
export type RequestsResultFilter =
  "" | "success" | "failed" | "unknown" | "cancelled";
export type RequestsTimeRange = "today" | "7" | "30";
export type RequestsUi = {
  result: RequestsResultFilter;
  time: RequestsTimeRange;
  /** The cursor used to load each visited page; index 0 is the first page. */
  cursors: (string | null)[];
  /** Zero-based index into `cursors`. */
  page: number;
  loading: boolean;
};

/** #116: the cloud read-only login-round history filters. */
export type LoginRoundsUi = {
  round: string;
  q: string;
  loading: boolean;
};

export interface CloudModel {
  summary: ReadState<CloudSummary>;
  health: ReadState<CloudHealth>;
  actions: ReadState<ActionRow[]>;
  operations: ReadState<OperationRow[]>;
  status: ReadState<ShareStatusPayload>;
  keys: ReadState<ShareKeyRow[]>;
  usage: ReadState<ShareUsage>;
  /** Ticket #8: today's failed requests, from the backend's `result=failed`
   *  aggregate read. A failed or stale read stays unknown, never 0. */
  usageFailed: ReadState<ShareUsage>;
  /** Per-request page, loaded on demand from /share-usage/requests. */
  usageRequests: ReadState<ShareUsageRequestsPage> | null;
  /** Ticket #9: result/time filters and cursor-backed pages. */
  usageRequestsUi: RequestsUi;
  /** #116: the cloud 8003 login-round history read and its filters. */
  loginRounds: ReadState<UmLoginRoundsPagePayload> | null;
  loginRoundsUi: LoginRoundsUi;
  /** D18-11: only the security read face remains. */
  security: ReadState<CloudSecurityStatus>;
  display: ReadState<DisplaySession>;
  /** D18-7/D18-8/D18-9: the read-only display auth/operator snapshot. */
  displayState: ReadState<DisplayState>;
  verdict: RoleVerdict;
  overviewUi: OverviewUi;
  usageUi: UsageUi;
  /** Ticket #5: whether the status row's evidence is expanded. */
  statusOpen: boolean;
  /** Ticket #6: the primary start this page submitted and has not settled yet;
   *  the operations read alone lags behind the click. */
  primaryPending?: { at: number; stage: string } | null;
  keyUi: KeyUi;
  feedback: readonly Feedback[];
  /** A write button: enabled only when the role is confirmed, the action is
   *  registered and enabled, and no submit of it is in flight. */
  actionButton(
    action: string,
    target: string,
    html: string,
    cls: string
  ): string;
  /** Share-key writes need the confirmed backend role too. */
  writeReady(): { ok: boolean; reason: string };
  keyBusy(id: string): boolean;
  keyNotice(id: string): KeyNotice | undefined;
  keySecret(
    id: string
  ):
    | { secret: string; rowVersion: number; at: number; revealed: boolean }
    | undefined;
  keyTest(id: string): KeyTest | undefined;
  keyCapValue(id: string): string | undefined;
  /** The page's own latest test of one scope; never persisted evidence. */
  latestKeyTest(
    from: "public" | "private"
  ): { name: string; test: KeyTest } | null;
}

export type Feedback = {
  key: string;
  action: string;
  target: string;
  tone: "ok" | "warn" | "err" | "neutral";
  text: string;
  opId: string | null;
  at: number;
  done: boolean;
  /** A2: the stable, non-secret identity this page can re-query. Null once the
   *  backend returned a terminal result (or the intent was abandoned). */
  intentKey: string | null;
};
