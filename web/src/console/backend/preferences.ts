/** Browser-local gateway sidebar order preference (Plan-Revision 12, 2026-10-04).
 *
 *  Data-only module: no DOM, page adapter, CSS, production API, or real browser
 *  storage access — the page adapter later passes a Storage-like seam (typically
 *  localStorage) plus a Demo/production namespace. It accepts stable gateway
 *  service IDs and orders by identity only, never by list position, port or
 *  display name. The persisted payload is exactly the format version plus one
 *  bounded, duplicate-free ID sequence; the pinned 「全部模型」 entry belongs to
 *  the consumer and never enters this sequence (a save containing IDs outside
 *  the available gateway set is refused).
 *
 *  Read is strictly read-only and re-coordinates the stored sequence with the
 *  currently available gateway set every time: known IDs keep their stored
 *  relative order, removed IDs are filtered out of the effective order (the
 *  stored payload itself stays untouched, so a transient empty gateway set
 *  cannot wipe the preference), and new gateways are appended in the given
 *  default order. A corrupt payload, an unknown format version or a duplicate
 *  ID conservatively falls back to the default order with an explicit status
 *  instead of being silently salvaged, and no other storage key (e.g. the
 *  console appearance preference) is ever read or written.
 *
 *  Save is one full-sequence overwrite after a completed move; nothing is
 *  written during a drag or after a cancel, because only read/merge run then.
 *  A storage refusal, quota exhaustion or private-mode failure is reported as
 *  not persisted (single attempt, no retry) and the in-page order stays
 *  authoritative. An external update from another tab stays safe: every write
 *  is a complete self-contained sequence, so the last completed move wins and
 *  the next read re-coordinates from scratch. Demo and production namespaces
 *  are part of the storage key; different ports are already different origins.
 *  A gateway absent at save time re-joins later as a new (appended) gateway. */
import { ContractError, object } from "./normalize.ts";

export const GATEWAY_ORDER_FORMAT_VERSION = 1;
/** Storage key prefix; the Demo/production namespace is appended after a colon. */
export const GATEWAY_ORDER_STORAGE_KEY = "gateway-console-gateway-order-v1";
/** Longer stored sequences are corrupt on read and refused on save, never silently truncated. */
export const MAX_GATEWAY_ORDER_IDS = 64;

/** Minimal synchronous seam; the page adapter passes localStorage or an isolated test double. */
export interface GatewayOrderStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** Explicit, presentable derivation outcome; every reset leaves the stored payload untouched. */
export type GatewayOrderStatus =
  | "default"              // no usable stored preference: key absent, empty sequence
  | "coordinated"          // stored order kept known IDs, dropped removed, appended new
  | "reset_corrupt"        // malformed JSON/shape or an over-bound sequence
  | "reset_version"        // missing or unknown format version
  | "reset_duplicate"      // the same gateway ID twice in the stored sequence
  | "storage_read_failed"; // storage denied the read; default order, nothing assumed

export interface GatewayOrderRead {
  /** Effective order: a permutation of the available IDs (empty when none are available). */
  order: string[];
  status: GatewayOrderStatus;
}

export interface GatewayOrderSave {
  /** False keeps the in-page order authoritative; nothing was written to storage. */
  persisted: boolean;
  reason: "storage_write_failed" | "sequence_too_long" | null;
}

function exactGatewayIds(value: readonly unknown[]): string[] {
  if (!Array.isArray(value) || value.some(id => typeof id !== "string" || !id.trim()))
    throw new ContractError("gateway identities must be nonempty strings");
  const ids = value as string[];
  if (new Set(ids).size !== ids.length) throw new ContractError("gateway identities contain duplicates");
  return [...ids];
}

/** Narrow data seam for the gateway sidebar order. Naming stays neutral until the
 *  actual sidebar component wires it: no invented server actions, no page layout. */
export class GatewayOrderPreferences {
  readonly storageKey: string;
  private readonly storage: GatewayOrderStorage;

  constructor(options: { storage: GatewayOrderStorage; namespace: string }) {
    if (typeof options?.namespace !== "string" || !options.namespace.trim())
      throw new ContractError("gateway order namespace must be a nonempty string");
    this.storage = options.storage;
    this.storageKey = `${GATEWAY_ORDER_STORAGE_KEY}:${options.namespace}`;
  }

  /** Coordinate the stored preference with the currently available gateway set. */
  read(availableGatewayIds: readonly string[]): GatewayOrderRead {
    const available = exactGatewayIds(availableGatewayIds);
    let raw: string | null;
    try {
      raw = this.storage.getItem(this.storageKey);
    } catch {
      return { order: available, status: "storage_read_failed" };
    }
    if (raw === null || raw === "") return { order: available, status: "default" };
    let payload: unknown;
    try {
      payload = JSON.parse(raw);
    } catch {
      return { order: available, status: "reset_corrupt" };
    }
    const row = object(payload);
    if (!row) return { order: available, status: "reset_corrupt" };
    if (row.version !== GATEWAY_ORDER_FORMAT_VERSION) return { order: available, status: "reset_version" };
    if (!Array.isArray(row.order) || row.order.some(id => typeof id !== "string"))
      return { order: available, status: "reset_corrupt" };
    const stored = row.order as string[];
    if (stored.length > MAX_GATEWAY_ORDER_IDS) return { order: available, status: "reset_corrupt" };
    if (new Set(stored).size !== stored.length) return { order: available, status: "reset_duplicate" };
    if (!stored.length) return { order: available, status: "default" };
    const availableSet = new Set(available);
    const kept = stored.filter(id => availableSet.has(id));
    const keptSet = new Set(kept);
    return { order: [...kept, ...available.filter(id => !keptSet.has(id))], status: "coordinated" };
  }

  /** Persist one complete reordering of exactly the available gateways; the
   *  pinned all-models entry and filtered-out hidden gateways must already be
   *  merged into `order` by the caller (see reorderGatewaySubset). */
  save(order: readonly string[], availableGatewayIds: readonly string[]): GatewayOrderSave {
    const available = exactGatewayIds(availableGatewayIds);
    if (!available.length) throw new ContractError("gateway order cannot be saved for an empty gateway set");
    const candidate = exactGatewayIds(order);
    const availableSet = new Set(available);
    if (candidate.length !== available.length || candidate.some(id => !availableSet.has(id)))
      throw new ContractError("gateway order must reorder exactly the available gateways");
    if (candidate.length > MAX_GATEWAY_ORDER_IDS) return { persisted: false, reason: "sequence_too_long" };
    try {
      this.storage.setItem(this.storageKey, JSON.stringify({ version: GATEWAY_ORDER_FORMAT_VERSION, order: candidate }));
      return { persisted: true, reason: null };
    } catch {
      return { persisted: false, reason: "storage_write_failed" };
    }
  }

  /** Drop the stored preference; absence reads back as the default order. */
  restoreDefault(): GatewayOrderSave {
    try {
      this.storage.removeItem(this.storageKey);
      return { persisted: true, reason: null };
    } catch {
      return { persisted: false, reason: "storage_write_failed" };
    }
  }
}

/** Merge a completed reorder of the visible subset back into the complete list:
 *  visible slots take the new order while hidden gateways keep their exact
 *  position and are never dropped. Pure; persistence happens only via save. */
export function reorderGatewaySubset(fullOrder: readonly string[], visibleOrder: readonly string[]): string[] {
  const full = exactGatewayIds(fullOrder);
  const visible = exactGatewayIds(visibleOrder);
  const visibleSet = new Set(visible);
  const hiddenCount = full.filter(id => !visibleSet.has(id)).length;
  if (visible.length + hiddenCount !== full.length)
    throw new ContractError("reordered subset must contain only gateways from the complete list");
  const next = [...visible];
  let taken = 0;
  return full.map(id => (visibleSet.has(id) ? next[taken++] : id));
}
