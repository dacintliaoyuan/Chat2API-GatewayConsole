import { paintTheme } from "../theme-ui";

/** Console appearance preferences, shared by both consoles.
 *
 *  - theme: light / dark / system.
 *  - rail: the sidebar shows icon + label (expanded) or icons only (collapsed).
 *    The key keeps its PR10 name, so a stored choice carries over from the old
 *    top-bar label toggle.
 *  - motion: follow the system, or reduce interface motion regardless of it.
 *  - transparency (2026-10-31): follow the system's prefers-reduced-transparency,
 *    or always draw the glass navigation layer as an opaque panel.
 *  - backdrop / ground / mode / intensity: retired preferences (the vivid / soft /
 *    solid CSS ground, PR10's ground, the archive canvas mode and its intensity).
 *    They no longer change anything and the dialog no longer offers them, but a
 *    stored value is kept as it was (read, validated, written back) so no saved
 *    preference is dropped. */
type Appearance = {
  theme: "light" | "dark" | "system";
  rail: "expanded" | "collapsed";
  motion: "system" | "reduced";
  transparency: "system" | "reduced";
  backdrop: "vivid" | "soft" | "solid";
  ground: "solid" | "gradient";
  mode: "dynamic" | "static" | "off";
  intensity: "clear" | "gentle";
};
const STORAGE_KEY = "gateway-console-appearance-v1";
const prefs: Appearance = {
  theme: "system",
  rail: "expanded",
  motion: "system",
  transparency: "system",
  backdrop: "vivid",
  ground: "solid",
  mode: "dynamic",
  intensity: "clear",
};
const CHOICES: { [K in keyof Appearance]: readonly Appearance[K][] } = {
  theme: ["light", "dark", "system"],
  rail: ["expanded", "collapsed"],
  motion: ["system", "reduced"],
  transparency: ["system", "reduced"],
  backdrop: ["vivid", "soft", "solid"],
  ground: ["solid", "gradient"],
  mode: ["dynamic", "static", "off"],
  intensity: ["clear", "gentle"],
};
function accept<K extends keyof Appearance>(key: K, value: unknown): boolean {
  if (!(CHOICES[key] as readonly unknown[]).includes(value)) return false;
  prefs[key] = value as Appearance[K];
  return true;
}

const darkSystem = matchMedia("(prefers-color-scheme: dark)");
const reducedMotionSystem = matchMedia("(prefers-reduced-motion: reduce)");
const reducedTransparencySystem = matchMedia(
  "(prefers-reduced-transparency: reduce)"
);
/** Below this width the sidebar always shows icons only, whatever the stored
 *  preference says; the dialog says so next to the choice. */
const narrowRail = matchMedia("(max-width: 1180px)");
let storageFailed = false;
let stage: HTMLElement | null = null;
let settings: HTMLDialogElement | null = null;
let settingsTrigger: HTMLElement | null = null;

function loadPrefs(): void {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const saved = JSON.parse(raw || "{}");
    if (!raw) {
      const legacy = JSON.parse(localStorage.getItem("rhine-settings") || "{}");
      if (["light", "dark"].includes(legacy.colorTheme))
        prefs.theme = legacy.colorTheme;
      if (legacy.reduced === true) prefs.motion = "reduced";
    }
    for (const key of Object.keys(CHOICES) as (keyof Appearance)[])
      accept(key, saved[key]);
  } catch {
    /* Storage denial or malformed preferences do not block the console. */
  }
}
function savePrefs(): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
    storageFailed = false;
  } catch {
    storageFailed = true;
  }
}

export function reduceMotion(): boolean {
  return reducedMotionSystem.matches || prefs.motion === "reduced";
}
function reduceTransparency(): boolean {
  return reducedTransparencySystem.matches || prefs.transparency === "reduced";
}

/** One segmented choice group of the dialog. */
const group = (
  legend: string,
  name: keyof Appearance,
  options: readonly [string, string][],
  note = ""
) =>
  `<fieldset data-group="${name}"><legend>${legend}</legend><div class="appearance-options">${options
    .map(
      ([value, label]) =>
        `<label><input type="radio" name="${name}" value="${value}"><span>${label}</span></label>`
    )
    .join("")}</div>${note}</fieldset>`;

function buildDialog(): HTMLDialogElement {
  const dialog = document.createElement("dialog");
  dialog.className = "console-appearance";
  dialog.setAttribute("aria-labelledby", "appearance-title");
  dialog.innerHTML = `<form method="dialog"><header><h2 id="appearance-title">外观设置</h2><button class="shell-icon-btn" aria-label="关闭外观设置" title="关闭外观设置"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg></button></header>
    ${group("主题", "theme", [
      ["light", "浅色"],
      ["dark", "深色"],
      ["system", "跟随系统"],
    ])}
    ${group(
      "侧栏",
      "rail",
      [
        ["expanded", "图标 + 文字"],
        ["collapsed", "仅图标"],
      ],
      `<p class="appearance-reason" data-rail-reason hidden></p>`
    )}
    ${group(
      "界面动效",
      "motion",
      [
        ["system", "跟随系统"],
        ["reduced", "减少动效"],
      ],
      `<p class="appearance-reason" data-motion-reason hidden></p>`
    )}
    ${group(
      "透明度",
      "transparency",
      [
        ["system", "跟随系统"],
        ["reduced", "降低透明度"],
      ],
      `<p class="appearance-reason" data-transparency-reason hidden></p>`
    )}
    <p class="appearance-hint">侧栏与顶部操作区是浮在内容上的玻璃层，内容区始终是实色；「降低透明度」把玻璃层换成不透明面板。「减少动效」关闭界面过渡与数字滚动。旧版的「背景」偏好原样保留、不再生效。</p>
    <p class="appearance-status" role="status" aria-live="polite"></p></form>`;
  return dialog;
}

function reason(selector: string, text: string): void {
  const node = settings?.querySelector<HTMLElement>(selector);
  if (!node) return;
  node.textContent = text;
  node.hidden = !text;
}

export function applyAppearance(): void {
  const dark =
    prefs.theme === "dark" || (prefs.theme === "system" && darkSystem.matches);
  paintTheme(dark ? 1 : 0);
  const root = document.documentElement;
  delete root.dataset.consoleBackdrop;
  root.dataset.consoleRail =
    prefs.rail === "collapsed" || narrowRail.matches ? "collapsed" : "expanded";
  // The dialog lives outside #stage, so reduced motion is published on the root
  // too (a root-level :has() would restyle the whole document on every write).
  root.dataset.consoleMotion = reduceMotion() ? "reduced" : "full";
  root.dataset.consoleTransparency = reduceTransparency() ? "reduced" : "full";
  stage?.classList.toggle("reduce-motion", reduceMotion());
  const collapsed = root.dataset.consoleRail === "collapsed";
  document
    .querySelectorAll<HTMLButtonElement>('[data-action="rail-toggle"]')
    .forEach((toggle) => {
      const name = collapsed ? "展开侧栏" : "收起侧栏";
      toggle.setAttribute("aria-label", name);
      toggle.setAttribute("aria-expanded", String(!collapsed));
      const hint = toggle.querySelector(".wb-icon-hint, .shell-hint");
      if (hint) hint.textContent = name;
    });
  if (!settings) return;
  settings
    .querySelectorAll<HTMLInputElement>("input[type=radio]")
    .forEach((input) => {
      input.checked = prefs[input.name as keyof Appearance] === input.value;
    });
  // A group the page overrides writes its reason next to it, and the stored
  // choice stays marked — so 「没生效」 reads differently from 「没选上」.
  reason(
    "[data-rail-reason]",
    narrowRail.matches
      ? "窗口宽度不足 1180px 时侧栏固定只显示图标；加宽窗口后恢复此处的选择。"
      : ""
  );
  reason(
    "[data-motion-reason]",
    reducedMotionSystem.matches && prefs.motion === "system"
      ? "系统已开启「减少动态效果」，界面动效已按系统减少。"
      : ""
  );
  reason(
    "[data-transparency-reason]",
    reducedTransparencySystem.matches && prefs.transparency === "system"
      ? "系统已开启「降低透明度」，玻璃层已按系统换成不透明面板。"
      : ""
  );
  settings.querySelector<HTMLElement>(".appearance-status")!.textContent =
    storageFailed
      ? "浏览器未允许保存设置，本次页面仍然生效。"
      : "设置会自动保存在此浏览器。";
}

/** Load the stored preferences, paint them, and wire the dialog and the
 *  sidebar toggle (both found by data-action, in either console). */
export function initAppearance(host: HTMLElement): void {
  stage = host;
  loadPrefs();
  settings = buildDialog();
  document.body.append(settings);
  settings.addEventListener("change", (event) => {
    const input = event.target as HTMLInputElement;
    if (!accept(input.name as keyof Appearance, input.value)) return;
    savePrefs();
    applyAppearance();
  });
  settings.addEventListener("close", () => settingsTrigger?.focus());
  document.addEventListener("click", (event) => {
    const target = event.target as Element;
    if (target.closest('[data-action="rail-toggle"]')) {
      prefs.rail =
        document.documentElement.dataset.consoleRail === "collapsed"
          ? "expanded"
          : "collapsed";
      savePrefs();
      applyAppearance();
      return;
    }
    const trigger = target.closest<HTMLElement>('[data-action="settings"]');
    if (!trigger || !settings) return;
    settingsTrigger = trigger;
    applyAppearance();
    if (!settings.open) {
      settings.showModal();
      // Land on the current theme choice, not on the close button.
      settings
        .querySelector<HTMLInputElement>("input[name=theme]:checked")
        ?.focus();
    }
  });
  for (const query of [
    darkSystem,
    reducedMotionSystem,
    reducedTransparencySystem,
    narrowRail,
  ])
    query.addEventListener("change", applyAppearance);
  applyAppearance();
}
