import "./theme.css";
/** Frozen theme palette (design contract §2.2). One entry per interpolated
 *  colour token: light value first, dark value second. paintTheme() writes
 *  `--theme-<name>` and `--theme-<name>-rgb` for every entry onto <html>, so the
 *  background / panel / field / ink / muted / line / accent family keeps one fact
 *  source; the static tokens live in workbench.css and this object is never
 *  restated in CSS. These are the archive page's frozen values, kept verbatim
 *  (contract §0.4.1 / §5.1 #24); the console's own cool palette (PR11) lives
 *  in `consolePalette` below, not here. */
const palette = {
  ink: ["#080a08", "#e0e3dc"], muted: ["#77756d", "#a6b0b1"], line: ["#aaa59a", "#536166"],
  paper: ["#eae5e1", "#060708"], panel: ["#edebe4", "#151719"], field: ["#e7e3d9", "#202225"],
  accent: ["#9b7247", "#c5a16b"],
} as const;
/** Per-daypart glass temperature (R9-R28): small RGB offsets applied to the
 *  surface colors after the light/dark interpolation — warm dawn, amber dusk,
 *  cool night, neutral midday. Ink/muted/line stay untouched so text
 *  contrast never shifts with the clock. */
const TIME_TINTS: readonly { from: number; to: number; tint: [number, number, number] }[] = [
  { from: 5, to: 9, tint: [6, 4, -5] },   // 晨暖
  { from: 17, to: 20, tint: [10, 3, -12] }, // 黄昏琥珀
  { from: 20, to: 29, tint: [-4, -2, 6] },  // 夜冷（29 = 24+5，跨午夜归一）
];
/** Console surface palette (PR11「青柠」, maintainer 2026-09-30). Cool blue-grey
 *  ink on a vivid static ground (workbench.css "Console ground"): paper is the
 *  solid-ground colour, panel the card base that cards lay over the ground with
 *  alpha, field the input fill. `muted` is the secondary text that may sit
 *  directly on the gradient, so it is #354155 (≥ 5.4:1 on the ground's darkest
 *  periwinkle corner #aebcf7, 9.9:1 on white). The accent is the text-capable
 *  indigo (5.9:1 on white; #98a3ff ≥ 7:1 on the dark cards); the lemon fill
 *  lives in workbench.css as --wb-lemon and never carries as text. `palette`
 *  above stays the archive page's frozen contract; main.ts keeps calling
 *  paintTheme without a surface, so the archive page still gets those values. */
const consolePalette: Record<keyof typeof palette, readonly [string, string]> = {
  ink: ["#131820", "#eef2f7"], muted: ["#354155", "#aeb9c8"], line: ["#cfd7e6", "#2e3b4f"],
  paper: ["#e4eaf5", "#141b25"], panel: ["#ffffff", "#1b2433"],
  field: ["#eef2fa", "#222e40"], accent: ["#3f4fd6", "#98a3ff"],
};
/** Which surface the last paint wrote. paintTimeOfDay reuses it, so the console
 *  path never falls back to the archive branch on a clock tick (§2.1 #4). */
export type Surface = "archive" | "console";
/** Only surfaces take the daypart tint; ink / muted / line stay untouched so
 *  text contrast never moves with the clock (§2.2). The console light base drops
 *  the tint entirely (§2.1 #3, §0.4.2 #1) — the archive page keeps it. */
const TINTED = ["paper", "panel", "field"] as const satisfies readonly (keyof typeof palette)[];
let previous = -1;
let currentHour = new Date().getHours();
let currentSurface: Surface = "archive";
export let themeAmount = 0;
function rgb(hex: string) { return [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16)); }
function tintFor(hour: number): [number, number, number] | null {
  const normalized = hour >= 20 ? hour : hour < 5 ? hour + 24 : hour;
  for (const band of TIME_TINTS) {
    if (normalized >= band.from && normalized < band.to) return band.tint;
  }
  return null;
}
/** Write the interpolated theme colours and their `-rgb` companions onto <html>,
 *  and set documentElement.dataset.darkSurface - the only dark switch the
 *  production CSS keys on (contract §2.1). amount 0 is light and 1 is dark; the
 *  console only ever passes those two, the archive page interpolates between them.
 *  `surface` picks the palette branch: the default "archive" keeps the frozen
 *  archive palette and its daypart tint, "console" takes its own cool
 *  blue-grey palette (PR11) and no tint at all (§2.1 #2/#3). */
export function paintTheme(amount: number, surface: Surface = "archive") {
  const unchanged = Math.abs(amount - previous) < .0001 && surface === currentSurface;
  currentSurface = surface;
  if (unchanged) return;
  previous = themeAmount = amount;
  const root = document.documentElement;
  root.dataset.darkSurface = String(amount > .0001);
  const source: Record<keyof typeof palette, readonly [string, string]> =
    surface === "console" ? consolePalette : palette;
  for (const [name, values] of Object.entries(source)) {
    const from = rgb(values[0]), to = rgb(values[1]);
    let value = from.map((v, i) => Math.round(v + (to[i] - v) * amount));
    // Daypart tint rides on top of the light/dark interpolation for the archive
    // surfaces; the console light base is exempt so its paper value is the same at
    // any hour. Text is never tinted in either surface.
    if (surface === "archive" && amount < 0.5 && (TINTED as readonly string[]).includes(name)) {
      const tint = tintFor(currentHour);
      if (tint) value = value.map((v, i) => Math.max(0, Math.min(255, v + tint[i])));
    }
    const joined = value.join(", ");
    root.style.setProperty(`--theme-${name}`, `rgb(${joined})`);
    root.style.setProperty(`--theme-${name}-rgb`, joined);
  }
}
/** Repaint with the current hour (called once at boot and once per minute);
 *  a no-op repaint when neither theme nor hour changed. Without a `surface`
 *  argument it reuses the surface of the last paint, so the console path stays on
 *  the console palette across clock ticks (§2.1 #4). */
export function paintTimeOfDay(hour: number, surface?: Surface) {
  const next = surface ?? currentSurface;
  if (hour === currentHour && previous >= 0) {
    paintTheme(previous, next); // same hour: still cheap no-op inside paintTheme
    return;
  }
  currentHour = hour;
  previous = -1;
  paintTheme(themeAmount, next);
}
export function themeSettingsMarkup(dark: boolean) {
  return `<div class="theme-settings"><div><strong>界面配色</strong><span>玻璃阵列随配色逐张过渡</span></div><div class="theme-choices" role="group" aria-label="界面配色"><button data-color-theme="light" aria-pressed="${!dark}">亮色</button><button data-color-theme="dark" aria-pressed="${dark}">暗色</button></div></div>`;
}
