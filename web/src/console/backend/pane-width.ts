/** Browser-local column width for the two resizable console splits — the §10.1
 *  design decision (2026-10-10): the log source sidebar and the model detail
 *  pane.
 *
 *  Data-only module: no DOM, page adapter, CSS, production API or real browser
 *  storage access — the page adapter passes a Storage-like seam (typically
 *  localStorage) plus the Demo/production namespace, exactly like the gateway
 *  order preference. Splits are addressed by a stable identity string, never by
 *  list position: a reordered or renamed column keeps its width, and a column
 *  that disappears keeps its stored width for when it comes back.
 *
 *  The persisted payload is exactly the format version plus one bounded map of
 *  identity → width in CSS pixels. Every read clamps into the design-decided
 *  [min, max] range of that split, so a stale payload written for a different
 *  design, a hand-edited value or a negative number can never produce an
 *  unusable column; a non-finite or non-numeric value is corrupt and falls back
 *  to the design default with an explicit status instead of being salvaged. A save
 *  writes the complete map (the caller passes every split it currently holds),
 *  so two splits can never overwrite each other. Nothing is written during a
 *  drag: only the released width is saved. */
import { ContractError, object } from "./normalize.ts";

export const PANE_WIDTH_FORMAT_VERSION = 1;
/** Storage key prefix; the Demo/production namespace is appended after a colon. */
export const PANE_WIDTH_STORAGE_KEY = "gateway-console-pane-width-v1";
/** Longer maps are corrupt on read and refused on save, never silently truncated. */
export const MAX_PANE_WIDTHS = 16;

/** Minimal synchronous seam; the page adapter passes localStorage or an isolated
 *  test double. */
export interface PaneWidthStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** One resizable split: the stable identity, its CSS custom property, and the
 *  design-decided bounds. `fallback` is also the CSS default, so a payload that
 *  cannot be used and a payload that was never written render identically. */
export interface PaneWidthSpec {
  id: string;
  /** The custom property the page writes on the split container, e.g.
   *  `--wb-gw-side-w`. */
  cssVar: string;
  fallback: number;
  min: number;
  max: number;
}

export const PANE_WIDTH_SPECS: readonly PaneWidthSpec[] = [
  // 列表侧栏（日志来源 / 网关列表共用同一列）：默认 300px（CSS 现值），
  // 最小 200px（搜索框 + 名称仍可读），最大 480px（再宽右侧事件窗口就失去意义）。
  {
    id: "gw-side",
    cssVar: "--wb-gw-side-w",
    fallback: 300,
    min: 200,
    max: 480,
  },
  // 模型详情栏：默认 320px（CSS 现值），最小 260px（表单数字输入 9rem + 标签仍可读），
  // 最大 560px（再宽目录表就失去意义）。
  {
    id: "model-detail",
    cssVar: "--wb-model-detail-w",
    fallback: 320,
    min: 260,
    max: 560,
  },
];

/** The spec for one split identity; an unknown identity is a caller bug. */
export function paneWidthSpec(id: string): PaneWidthSpec {
  const spec = PANE_WIDTH_SPECS.find((candidate) => candidate.id === id);
  if (!spec) throw new ContractError(`unknown pane width identity ${id}`);
  return spec;
}

/** Clamp any finite number into the split's design-decided range; a non-finite
 *  number is not a width and falls back to the design default. */
export function clampPaneWidth(spec: PaneWidthSpec, width: number): number {
  if (!Number.isFinite(width)) return spec.fallback;
  return Math.min(spec.max, Math.max(spec.min, Math.round(width)));
}

/** Move the separator horizontally: a right-hand pane grows toward the left. */
export function paneWidthAfterMove(
  spec: PaneWidthSpec,
  width: number,
  deltaX: number
): number {
  return clampPaneWidth(
    spec,
    width + (spec.id === "model-detail" ? -deltaX : deltaX)
  );
}

/** Explicit, presentable derivation outcome; every reset leaves the stored
 *  payload untouched. */
export type PaneWidthStatus =
  | "default" // no usable stored preference: key absent, empty map
  | "stored" // at least one stored width was adopted (clamped)
  | "reset_corrupt" // malformed JSON/shape, a bad width or an over-bound map
  | "reset_version" // missing or unknown format version
  | "storage_read_failed"; // storage denied the read; defaults, nothing assumed

export interface PaneWidthRead {
  /** Effective widths for the known splits; a split with no stored value is absent
   *  and the caller uses the design default. */
  widths: Record<string, number>;
  status: PaneWidthStatus;
}

export interface PaneWidthSave {
  /** False keeps the in-page width authoritative; nothing was written. */
  persisted: boolean;
  reason: "storage_write_failed" | "map_too_long" | "unknown_split" | null;
}

function exactWidths(value: unknown): Record<string, number> {
  const row = object(value);
  if (!row) throw new ContractError("pane widths must be an object");
  const entries = Object.entries(row);
  if (entries.length > MAX_PANE_WIDTHS)
    throw new ContractError("pane widths are over bound");
  const widths: Record<string, number> = {};
  for (const [id, width] of entries) {
    paneWidthSpec(id); // an unknown split identity is corrupt, never adopted
    if (typeof width !== "number" || !Number.isFinite(width))
      throw new ContractError("pane widths must be finite numbers");
    widths[id] = clampPaneWidth(paneWidthSpec(id), width);
  }
  return widths;
}

export class PaneWidthPreferences {
  readonly storageKey: string;
  private readonly storage: PaneWidthStorage;

  constructor(options: { storage: PaneWidthStorage; namespace: string }) {
    if (typeof options?.namespace !== "string" || !options.namespace.trim())
      throw new ContractError("pane width namespace must be a nonempty string");
    this.storage = options.storage;
    this.storageKey = `${PANE_WIDTH_STORAGE_KEY}:${options.namespace}`;
  }

  /** Read the stored widths; nothing is written and no other key is touched. */
  read(): PaneWidthRead {
    let raw: string | null;
    try {
      raw = this.storage.getItem(this.storageKey);
    } catch {
      return { widths: {}, status: "storage_read_failed" };
    }
    if (raw === null || raw === "") return { widths: {}, status: "default" };
    let payload: unknown;
    try {
      payload = JSON.parse(raw);
    } catch {
      return { widths: {}, status: "reset_corrupt" };
    }
    const row = object(payload);
    if (!row) return { widths: {}, status: "reset_corrupt" };
    if (row.version !== PANE_WIDTH_FORMAT_VERSION)
      return { widths: {}, status: "reset_version" };
    let widths: Record<string, number>;
    try {
      widths = exactWidths(row.widths);
    } catch {
      return { widths: {}, status: "reset_corrupt" };
    }
    if (!Object.keys(widths).length) return { widths: {}, status: "default" };
    return { widths, status: "stored" };
  }

  /** Persist one complete map; the caller passes every split it currently
   *  holds, so a save can never drop another split's width. */
  save(widths: Record<string, number>): PaneWidthSave {
    let candidate: Record<string, number>;
    try {
      candidate = exactWidths(widths);
    } catch (error) {
      const reason =
        error instanceof ContractError &&
        /unknown pane width identity/.test(error.message)
          ? "unknown_split"
          : "map_too_long";
      return { persisted: false, reason };
    }
    if (!Object.keys(candidate).length)
      return { persisted: false, reason: "map_too_long" };
    try {
      this.storage.setItem(
        this.storageKey,
        JSON.stringify({
          version: PANE_WIDTH_FORMAT_VERSION,
          widths: candidate,
        })
      );
      return { persisted: true, reason: null };
    } catch {
      return { persisted: false, reason: "storage_write_failed" };
    }
  }

  /** Drop the stored widths; absence reads back as the design defaults. */
  restoreDefault(): PaneWidthSave {
    try {
      this.storage.removeItem(this.storageKey);
      return { persisted: true, reason: null };
    } catch {
      return { persisted: false, reason: "storage_write_failed" };
    }
  }
}
