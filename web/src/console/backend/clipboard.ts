/** Narrow clipboard seam for the Rev15 gateway connection read result.
 *
 *  This module owns only the copy decision: it has no DOM, logger, storage or
 *  network side effects, never persists the plaintext key and never writes a log
 *  line. A copy counts as successful only after the injected clipboard writer
 *  resolved; every refusal and every rejected write returns an explicit failure
 *  result with a reason instead of a success, and the copied value never rides
 *  along in that reason. */
import type { GatewayConnection } from "./types.ts";
import { ContractError } from "./normalize.ts";

/** Only the two displayable fields of the read result can be copied. */
export type GatewayConnectionCopyField = "baseUrl" | "keyPlaintext";

export interface GatewayConnectionCopyResult {
  /** Echo of the requested field so a late feedback can be matched to it. */
  field: GatewayConnectionCopyField;
  /** True only after the clipboard write resolved. */
  ok: boolean;
  /** Failure reason (never the copied value); null exactly when ok is true. */
  reason: string | null;
}

/** The clipboard seam a page injects; a missing writer is an honest failure. */
export interface ClipboardWriter {
  writeText(value: string): Promise<void>;
}

const copyFields = new Set<GatewayConnectionCopyField>(["baseUrl", "keyPlaintext"]);

/** A write failure keeps only the error class name, so a copied value can never
 *  escape through an error text (same discipline as the read face reasons). */
function copyFailureReason(error: unknown): string {
  const name = error instanceof Error ? error.name : "";
  return name && name !== "Error" ? `clipboard_write_failed:${name}` : "clipboard_write_failed";
}

/** Copy one field of the current read result.
 *
 *  `baseUrl` needs a real address and `keyPlaintext` needs a plaintext key, so a
 *  state that says there is no key (or that it could not be read) is refused —
 *  never reported as copied. `view` may be null: the binding clears its slot on a
 *  gateway switch, cancel or page exit, and a late response must not be copyable.
 */
export async function copyGatewayConnectionField(
  view: GatewayConnection | null,
  field: GatewayConnectionCopyField,
  clipboard: ClipboardWriter | null | undefined,
): Promise<GatewayConnectionCopyResult> {
  if (!copyFields.has(field))
    throw new ContractError("connection copy field must be baseUrl or keyPlaintext");
  const value = field === "baseUrl"
    ? (view?.baseUrl && view.baseUrl.trim() ? view.baseUrl : null)
    : (view && view.keyState === "present" && view.keyPlaintext?.trim() ? view.keyPlaintext : null);
  if (value === null)
    return { field, ok: false, reason: field === "baseUrl" ? "no_base_url" : "no_plaintext_key" };
  if (!clipboard || typeof clipboard.writeText !== "function")
    return { field, ok: false, reason: "clipboard_unavailable" };
  try {
    await clipboard.writeText(value);
  } catch (error) {
    return { field, ok: false, reason: copyFailureReason(error) };
  }
  return { field, ok: true, reason: null };
}
