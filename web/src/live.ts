/** Liveness-evidence shape shared by the workbench's service cards.
 *
 *  R11-6/B2 (2026-09-23) moved liveness evidence to the 8096 backend's
 *  server-side loopback probes (GET /api/console/health): the browser no longer
 *  probes static fallthrough paths that can answer with 404. A static resource
 *  response must never count as a service heartbeat. The rule is documented where the evidence
 *  is now produced — workbench.ts's probeTick. */

export interface ProbeResult {
  alive: boolean;
  /** Tri-state for the liveness dot (P1, 2026-09-24): "unknown" is the
   *  honest middle state for evidence that cannot decide (a connect timeout
   *  on the server side, or a snapshot that did not cover this service).
   *  Absent = derive from `alive` (browser-side probes always decide). */
  state?: "alive" | "dead" | "unknown";
  /** Short human-readable evidence line (status code or failure cause). */
  note: string;
  ms: number;
  status?: number;
  /** Evidence observation time, epoch ms — WHEN this verdict was seen, not
   *  when it is read. The card keeps the last evidence across failed reads
   *  (a failed read must not fake "everything is down"), so only this stamp
   *  can tell a fresh verdict from a frozen one; the workbench uses it to
   *  mark aged evidence as stale instead of showing it as current. */
  observedAt?: number;
}
