import "./fonts.css";
import "./console-fonts.css";
import "@kitlangton/rolling-number/styles.css";
import { Workbench } from "./workbench";
import { paintTheme } from "./theme-ui";
import "./console-entry.css";

/** Console appearance preferences (PR11, maintainer 2026-09-30).
 *
 *  - theme: light / dark / system.
 *  - backdrop: the static CSS ground — vivid (default), soft (desaturated) or
 *    solid. No canvas, animation or backdrop-filter in any of the three.
 *  - rail: the top navigation shows icon + label (expanded) or icons only
 *    (collapsed). The key keeps its PR10 name so a stored choice carries over.
 *  - motion: follow the system, or reduce interface motion regardless of it.
 *  - ground / mode / intensity: retired preferences (PR10's solid-or-soft ground,
 *    the archive canvas mode and its intensity). They no longer change anything on
 *    this page and the overlay no longer offers them, but a stored value is kept
 *    as it was (read, validated, written back) so no saved preference is dropped. */
type Appearance = {
  theme: "light" | "dark" | "system";
  backdrop: "vivid" | "soft" | "solid";
  rail: "expanded" | "collapsed";
  motion: "system" | "reduced";
  ground: "solid" | "gradient";
  mode: "dynamic" | "static" | "off";
  intensity: "clear" | "gentle";
};
const storageKey = "gateway-console-appearance-v1";
const prefs: Appearance = { theme: "system", backdrop: "vivid", rail: "expanded", motion: "system", ground: "solid", mode: "dynamic", intensity: "clear" };
const CHOICES: { [K in keyof Appearance]: readonly Appearance[K][] } = {
  theme: ["light", "dark", "system"],
  backdrop: ["vivid", "soft", "solid"],
  rail: ["expanded", "collapsed"],
  motion: ["system", "reduced"],
  ground: ["solid", "gradient"],
  mode: ["dynamic", "static", "off"],
  intensity: ["clear", "gentle"],
};
function accept<K extends keyof Appearance>(key: K, value: unknown): boolean {
  if (!(CHOICES[key] as readonly unknown[]).includes(value)) return false;
  prefs[key] = value as Appearance[K];
  return true;
}
try {
  const raw = localStorage.getItem(storageKey);
  const saved = JSON.parse(raw || "{}");
  if (!raw) {
    const legacy = JSON.parse(localStorage.getItem("rhine-settings") || "{}");
    if (["light", "dark"].includes(legacy.colorTheme)) prefs.theme = legacy.colorTheme;
    if (legacy.reduced === true) prefs.motion = "reduced";
  }
  for (const key of Object.keys(CHOICES) as (keyof Appearance)[]) accept(key, saved[key]);
} catch { /* Storage denial or malformed preferences do not block the console. */ }

const stage = document.querySelector<HTMLElement>("#stage")!;
stage.dataset.mode = "archive";
stage.classList.add("console-stage");
const workbench = new Workbench(stage, () => {}, () => {});
const darkSystem = matchMedia("(prefers-color-scheme: dark)");
const reducedSystem = matchMedia("(prefers-reduced-motion: reduce)");
/** Below this width the top navigation always shows icons only (workbench.css
 *  hides the label toggle there), whatever the stored preference says. */
const narrowRail = matchMedia("(max-width: 1180px)");
let storageFailed = false;

/** One segmented choice group of the overlay. `swatch` draws a small preview
 *  of the option (theme, backdrop and navigation), so the choice is seen, not guessed. */
const group = (legend: string, name: keyof Appearance, options: readonly [string, string][], note = "") =>
  `<fieldset data-group="${name}"><legend>${legend}</legend><div class="appearance-options">${options.map(([value, label]) =>
    `<label><input type="radio" name="${name}" value="${value}"><span class="appearance-swatch" data-swatch="${name}-${value}" aria-hidden="true"></span><span>${label}</span></label>`).join("")}</div>${note}</fieldset>`;

const settings = document.createElement("dialog");
settings.className = "console-appearance";
settings.setAttribute("aria-labelledby", "appearance-title");
settings.innerHTML = `<form method="dialog"><header><h2 id="appearance-title">外观设置</h2><button aria-label="关闭外观设置" class="appearance-close"><span class="appearance-close-mark" aria-hidden="true"></span><span class="wb-icon-hint" role="tooltip" aria-hidden="true">关闭外观设置</span></button></header>
  ${group("主题", "theme", [["light", "浅色"], ["dark", "深色"], ["system", "跟随系统"]])}
  ${group("背景", "backdrop", [["vivid", "鲜明"], ["soft", "柔和"], ["solid", "纯色"]])}
  ${group("导航", "rail", [["expanded", "文字 + 图标"], ["collapsed", "仅图标"]], `<p class="appearance-reason" data-rail-reason hidden></p>`)}
  ${group("界面动效", "motion", [["system", "跟随系统"], ["reduced", "减少动效"]], `<p class="appearance-reason" data-motion-reason hidden></p>`)}
  <details class="wb-secondary-details appearance-detail"><summary title="背景与动效说明 · 点击展开 / 收起">背景与动效说明<span class="wb-fold-mark" data-wb-fold-chevron="1" aria-hidden="true"></span></summary><p class="appearance-hint">背景是静态的 CSS 渐变：「鲜明」是默认的多色光斑，「柔和」是同一构图的低饱和版，「纯色」不带渐变；三档都不绘制画布、不做动画、不随时间或指针变化，卡片与文字的对比度在三档下都按最不利位置核对。「减少动效」关闭界面过渡、数字滚动、加载扫光与按压反馈；系统开启「减少动态效果」时同样生效。旧版的「纯色 / 柔和渐变」「动态 / 静态 / 关闭」与「动效强度」偏好原样保留、不再显示。</p></details>
  <p class="appearance-status" role="status" aria-live="polite"></p></form>`;
document.body.append(settings);
let settingsTrigger: HTMLElement | null = null;

function reduceMotion(): boolean { return reducedSystem.matches || prefs.motion === "reduced"; }
function applyAppearance() {
  const dark = prefs.theme === "dark" || (prefs.theme === "system" && darkSystem.matches);
  // The console is its own surface: its own cool blue-grey palette and no
  // daypart tint (so no clock repaint either), while the archive page (main.ts)
  // keeps calling this without a surface and keeps its frozen palette and tint.
  paintTheme(dark ? 1 : 0, "console");
  const root = document.documentElement;
  root.dataset.consoleBackdrop = prefs.backdrop;
  root.dataset.consoleRail = prefs.rail === "collapsed" || narrowRail.matches ? "collapsed" : "expanded";
  stage.classList.toggle("reduce-motion", reduceMotion());
  // The dialog lives outside #stage, so its reduced-motion rule keys off this
  // root attribute (console-entry.css) instead of an html:has() rule that
  // restyled the whole document on every DOM write.
  root.dataset.consoleMotion = reduceMotion() ? "reduced" : "full";
  const railToggle = document.querySelector<HTMLButtonElement>('[data-action="rail-toggle"]');
  if (railToggle) {
    const collapsed = root.dataset.consoleRail === "collapsed";
    const name = collapsed ? "显示导航文字" : "导航仅显示图标";
    railToggle.setAttribute("aria-label", name);
    railToggle.setAttribute("aria-expanded", String(!collapsed));
    const label = railToggle.querySelector(".wb-nav-label");
    const hint = railToggle.querySelector(".wb-icon-hint");
    if (label) label.textContent = name;
    if (hint) hint.textContent = name;
  }
  settings.querySelectorAll<HTMLInputElement>("input[type=radio]").forEach(input => {
    input.checked = prefs[input.name as keyof Appearance] === input.value;
  });
  // §3.6: a group the page overrides writes its own reason next to it, and the
  // stored choice stays marked (the preference is kept, only its effect is
  // overridden) - so the maintainer can tell 「没生效」 from 「没选上」.
  const railReason = settings.querySelector<HTMLElement>("[data-rail-reason]")!;
  railReason.textContent = narrowRail.matches ? "窗口宽度不足 1180px 时导航固定只显示图标；加宽窗口后恢复此处的选择。" : "";
  railReason.hidden = !narrowRail.matches;
  const motionReason = settings.querySelector<HTMLElement>("[data-motion-reason]")!;
  motionReason.textContent = reducedSystem.matches && prefs.motion === "system" ? "系统已开启「减少动态效果」，界面动效已按系统减少。" : "";
  motionReason.hidden = !(reducedSystem.matches && prefs.motion === "system");
  settings.querySelector<HTMLElement>(".appearance-status")!.textContent = storageFailed
    ? "浏览器未允许保存设置，本次页面仍然生效。"
    : "设置会自动保存在此浏览器。";
}
function savePrefs() {
  try { localStorage.setItem(storageKey, JSON.stringify(prefs)); storageFailed = false; } catch { storageFailed = true; }
}
settings.addEventListener("change", event => {
  const input = event.target as HTMLInputElement;
  if (!accept(input.name as keyof Appearance, input.value)) return;
  savePrefs();
  applyAppearance();
});
document.addEventListener("click", event => {
  const target = event.target as Element;
  if (target.closest('[data-action="rail-toggle"]')) {
    prefs.rail = document.documentElement.dataset.consoleRail === "collapsed" ? "expanded" : "collapsed";
    savePrefs();
    applyAppearance();
    return;
  }
  const trigger = target.closest<HTMLElement>('[data-action="settings"]');
  if (!trigger) return;
  settingsTrigger = trigger;
  applyAppearance();
  if (!settings.open) {
    settings.showModal();
    // Land on the current theme choice, not on the close button (whose name
    // plate would otherwise open over it on every visit).
    settings.querySelector<HTMLInputElement>("input[name=theme]:checked")?.focus();
  }
});
settings.addEventListener("close", () => settingsTrigger?.focus());
function onButtonPress(event: PointerEvent): void {
  if (!event.isPrimary || event.button !== 0 || reduceMotion()) return;
  const target = event.target;
  if (!(target instanceof Element)) return;
  const button = target.closest<HTMLButtonElement>("button");
  if (!button || button.disabled || (!stage.contains(button) && !settings.contains(button))) return;
  const rect = button.getBoundingClientRect();
  const pulse = document.createElement("span");
  pulse.className = "console-click-feedback";
  pulse.setAttribute("aria-hidden", "true");
  const diameter = Math.max(48, Math.max(rect.width, rect.height) * 1.7);
  pulse.style.width = pulse.style.height = `${diameter}px`;
  pulse.style.left = `${event.clientX - rect.left}px`;
  pulse.style.top = `${event.clientY - rect.top}px`;
  button.classList.add("console-click-host");
  button.append(pulse);
  pulse.addEventListener("animationend", () => pulse.remove(), { once: true });
}
document.addEventListener("pointerdown", onButtonPress, { passive: true });
darkSystem.addEventListener("change", applyAppearance);
reducedSystem.addEventListener("change", applyAppearance);
narrowRail.addEventListener("change", applyAppearance);
applyAppearance();
workbench.setEnabled(true);
window.addEventListener("pagehide", event => {
  if (event.persisted) return;
  document.removeEventListener("pointerdown", onButtonPress);
});
