import "./fonts.css";
import "./console-fonts.css";
import "./shell/tokens.css";
import "./shell/shell.css";
import { initAppearance } from "./shell/appearance";
import { BUILD_ROLE, RoleGate, installRoleGate } from "./console/role-gate";

/** Shared boot for both consoles (ADR 0001). The role is a build-time
 *  constant, so each build carries only its own console: the other branch and
 *  its chunk drop out. The role gate is installed before any console code runs,
 *  so no write can leave the page before the backend's role is confirmed. */
const stage = document.querySelector<HTMLElement>("#stage")!;
stage.classList.add("console-stage");
document.documentElement.dataset.consoleRole = BUILD_ROLE;
const gate = new RoleGate(BUILD_ROLE);
installRoleGate(gate);
initAppearance(stage);

if (__CONSOLE_ROLE__ === "cloud") {
  void import("./cloud/cloud-console").then(({ mountCloudConsole }) =>
    mountCloudConsole(stage, gate)
  );
} else {
  void import("./local/local-console").then(({ mountLocalConsole }) =>
    mountLocalConsole(stage, gate)
  );
}
