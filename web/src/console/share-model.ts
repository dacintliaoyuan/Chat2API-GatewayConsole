/** API service facade shapes (GET /api/console/share-management/*), shared by the
 *  consoles that read them. The cloud console reuses the same contract instead
 *  of restating it (ADR 0001). */

export type ShareStatusPayload = {
  schema_version?: number;
  share_max_concurrency?: number;
  desired_generation?: number;
  applied_generation?: number;
  in_sync?: boolean;
  pending_revocation?: boolean;
  active_keys?: number;
  total_keys?: number;
  broker?: {
    running?: boolean;
    version?: string;
    pid?: number;
    error_code?: string;
  } | null;
  /** ShareManager.status() returns the failure as an object {kind,error,at};
   *  the old string type never matched the real payload. */
  last_publication_failure?: {
    kind?: string;
    error?: string;
    at?: string;
  } | null;
  /** Real payload is a list of publication-blocker rows, not a count. */
  blocked_publications?: Array<{
    operation_id?: string;
    status?: string;
    target_generation?: number;
    stop_confirmed?: boolean;
    blocked?: boolean;
    created_at?: string;
  }>;
  blocked?: boolean;
  share_url?: string | null;
  share_url_source?: string | null;
  share_url_reason?: string | null;
  funnel_on?: boolean;
  /** Gateway INSTANCE evidence (broker verify_running seam) — distinct from
   *  broker.running, which is the always-on management daemon and is never
   *  open/residue evidence. */
  gateway_running?: boolean;
  /** The backend sends only {running, valid}, and leaves the key out while the
   *  UM backend is down; the check time is `last_check_at` (backend
   *  cross-check, 2026-10-31). `failure` is not sent either - it stays typed
   *  only for the local share page, which retires with the local share split. */
  account?: {
    running?: boolean;
    valid?: boolean;
    failure?: string | null;
  } | null;
  upstream?: { reachable?: boolean } | null;
  /** r14 capacity block. `configured` is the admission cap, never a measured
   *  upstream capacity; `in_flight` is only present while the restricted broker
   *  view is readable, and `queued`/`upstream_capacity` stay null with a reason. */
  capacity?: {
    configured?: number | null;
    in_flight?: number | null;
    in_flight_reason?: string | null;
    queued?: number | null;
    queued_reason?: string | null;
    upstream_capacity?: number | null;
    upstream_capacity_measured?: boolean | null;
    upstream_capacity_reason?: string | null;
    checked_at?: number | null;
    note?: string | null;
  } | null;
  last_check_at?: number;
  last_failure?: string | null;
};

export type ShareKeyRow = {
  id?: string;
  name?: string;
  masked?: string;
  /** R11-F2: backend flag — this row stores a same-identity ciphertext and
   *  its plaintext is retrievable through the narrow per-key endpoint. */
  plaintext_viewable?: boolean;
  enabled?: boolean;
  max_concurrency?: number;
  row_version?: number;
  /** ISO 8601 UTC, e.g. 2026-10-22T02:14:07.381522Z (share_store). */
  created_at?: string;
};

/** 分享读口失败原因（维护者 2026-09-28 的 ④）：后端自己的原因串优先，
 *  它没有可读错误体时才回落到**实测** HTTP 状态——同一事实在页面上只写
 *  一次。原先无条件补一次「（HTTP 400）」会把后端已给出错误码的应答印成
 *  两种状态（「share_read_failed（HTTP 400）」），本轮复核发现 9 的口径是
 *  `body.reason || body.error || `HTTP {status}``，与分享动作被拒
 *  （`submitAction` 的「被拒绝：${reason}」）同一条：后端给了错误码时
 *  页面不得再出现实测状态。 */
export const shareReadReason = (
  body: { error?: string; reason?: string } | null,
  status: number
): string => body?.reason || body?.error || `HTTP ${status}`;
