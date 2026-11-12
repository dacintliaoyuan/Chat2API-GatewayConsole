/** B6 data controller for the frozen UM console (handoff r3).
 *
 *  The three pages are views only: this controller owns every read, task,
 *  cancel and backend action and hands the console a `UmViewState` through
 *  `UmConsoleHandle.update`. It never renders and the views never fetch.
 *
 *  Reads go through the existing console backend seam:
 *  - `/api/console/um/today|trend|requests|logs|request-logs|maintenance`
 *  - `/api/console/services` (runtime snapshot, the authority for chat2api)
 *  - `/api/console/gateway-connection?service=chat2api` (access facts)
 *  - `/api/summary` (`umReference`: Magpie reference price facts)
 *  - the operation registry for `account.um_status`, `account.um_verify`,
 *    `account.login`, `service.start|stop|restart`, `model.catalog`,
 *    `model.context_probe_preview`, `model.context_probe_run` and
 *    `model.speedtest` (the explicit UM batch).
 *
 *  Honesty rules kept from the frozen contract:
 *  - a real zero is `{ kind: "exact", value: 0 }`; every absent figure keeps
 *    the backend's own kind (unreported / unknown / missing);
 *  - a failed refresh keeps the last good data and marks it stale;
 *  - a cancelled or superseded read never installs its answer;
 *  - a missing/unavailable ledger is a failure, never an empty list;
 *  - the role gate folds into every write: nothing is posted until the backend
 *    role is confirmed, and a cloud backend (no local UM read face) is reported
 *    as unavailable instead of being filled with invented data.
 *
 *  No chat body is collected: the request/log reads use the backend's bounded,
 *  already-redacted read faces and never request message content. */

import { UmDataActions } from "./actions";

export type { UmDataControllerOptions } from "./state";

/** The local UM console's data controller: a composition of the state, read and
 *  action modules. The public API (constructor, `viewState`, `attach`,
 *  `prepare`, `start`, `dispose` and `handleEvent`) is unchanged. */
export class UmDataController extends UmDataActions {}
