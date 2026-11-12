import "./theme.css";
/** The --theme-* family the local workbench styles read, written inline on <html>.
 *  Since 2026-10-31 the values are the M3 roles of shell/tokens.css (ink =
 *  on-surface, muted = on-surface-variant, line = outline-variant, paper =
 *  surface, panel = surface-lowest, field = surface-low / container, accent =
 *  primary), so both consoles paint from one palette. */
const consolePalette = {
  ink: ["#181c22", "#e3e6ed"],
  muted: ["#434955", "#c0c6d1"],
  line: ["#c4cad6", "#3d434e"],
  paper: ["#f4f6fa", "#0f1217"],
  panel: ["#ffffff", "#151920"],
  field: ["#eef1f6", "#21252d"],
  accent: ["#1b5ccc", "#a8c2ff"],
} as const;
let previous = -1;
function rgb(hex: string) {
  return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
}
/** The console uses one fixed light/dark palette; time does not change it. */
export function paintTheme(amount: number) {
  if (Math.abs(amount - previous) < 0.0001) return;
  previous = amount;
  const root = document.documentElement;
  root.dataset.darkSurface = String(amount > 0.0001);
  for (const [name, values] of Object.entries(consolePalette)) {
    const from = rgb(values[0]),
      to = rgb(values[1]);
    const joined = from
      .map((v, i) => Math.round(v + (to[i] - v) * amount))
      .join(", ");
    root.style.setProperty(`--theme-${name}`, `rgb(${joined})`);
    root.style.setProperty(`--theme-${name}-rgb`, joined);
  }
}
