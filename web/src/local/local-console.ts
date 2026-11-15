import type { RoleGate } from "../console/role-gate";

/** The local console (ADR 0001; frontend handoff r3).
 *
 *  Candidate builds show the UM console (um-ui/: 总览 / 请求与诊断 / 模型与设置).
 *  `?um-preview=<scenario>` swaps in the labelled offline preview, which never
 *  sends a request. B6 (2026-11-01) also mounts the wired UM console in a
 *  production build. The offline `?um-preview=` swap stays candidate-only so a
 *  production URL cannot show synthetic data.
 */
export function mountLocalConsole(stage: HTMLElement, gate: RoleGate): void {
  const params = new URLSearchParams(location.search);
  const preview = __CONSOLE_CANDIDATE__ ? params.get("um-preview") : null;
  void mountUm(stage, gate, preview);
}

async function mountUm(
  stage: HTMLElement,
  gate: RoleGate,
  scenario: string | null
): Promise<void> {
  const [{ mountUmConsole }, { unwiredState }] = await Promise.all([
    import("./um-ui/um-console"),
    import("./um-ui/contract"),
  ]);
  stage.dataset.mode = "um";
  if (scenario === null && __CONSOLE_CANDIDATE__) {
    // Candidate builds ship before the B6 wiring: the real console is
    // honestly unwired and issues no reads. Only a production build mounts
    // the data controller below.
    mountUmConsole(stage, unwiredState(), {
      onEvent: () => {},
      gate,
      previewHref: "?um-preview=normal",
    });
    return;
  }
  if (scenario === null) {
    // B6: the real console is fed by the data controller; the console never
    // fetches. The controller is attached before it starts so no read can land
    // before the handle exists, and it is disposed when the page goes away.
    const { UmDataController } = await import("./um-data-controller");
    const controller = new UmDataController({ gate });
    controller.prepare();
    const handle = mountUmConsole(stage, controller.viewState(), {
      onEvent: (event) => controller.handleEvent(event),
      gate,
      previewHref: "?um-preview=normal",
    });
    controller.attach(handle);
    controller.start();
    // A page kept in the back/forward cache is restored, not reloaded: only a
    // real unload drops the controller and its in-flight reads.
    const onPageHide = (event: PageTransitionEvent) => {
      if (event.persisted) return;
      window.removeEventListener("pagehide", onPageHide);
      controller.dispose();
    };
    window.addEventListener("pagehide", onPageHide);
    return;
  }
  const preview = await import("./um-ui/preview");
  const id = preview.isScenario(scenario) ? scenario : "normal";
  let update: ((state: Parameters<typeof mountUmConsole>[1]) => void) | null =
    null;
  let first = unwiredState();
  const send = preview.startPreview(id, (state) => {
    if (update) update(state);
    else first = state;
  });
  const handle = mountUmConsole(stage, first, {
    onEvent: send,
    gate,
    previewHref: null,
    // One leading slash: a path such as //host/ would make the exit link
    // protocol-relative and point at another site.
    scenarios: {
      list: preview.SCENARIOS,
      current: id,
      href: (next) =>
        next === null
          ? `${location.pathname.replace(/^\/+/, "/")}${location.hash}`
          : `?um-preview=${next}${location.hash}`,
    },
  });
  update = (state) => handle.update(state);
}
