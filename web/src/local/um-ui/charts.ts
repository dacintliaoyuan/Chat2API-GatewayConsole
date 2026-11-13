import type { Reading, UmBucket, UmTrend } from "./contract";
import {
  dayKey,
  dayTitle,
  drawable,
  esc,
  full,
  monthDay,
  readingHtml,
  speed,
  speedBasisWord,
  tokens,
} from "./format";

/** The overview trend: stacked daily bars by model for Token / 费用 / 请求, or
 *  one line per model when the reader switches to 折线图, and always lines for
 *  速度 (a speed is never summed), with a compact ranking over the very same
 *  window. Drawn at the host's real pixel width so text is never scaled.
 *
 *  Lines are monotone cubic curves (Fritsch–Carlson) through the real samples:
 *  each drawn run passes every sample and stays inside that run's own min/max, so
 *  no peak or trough is invented. Only runs of two or more consecutive known
 *  values become a curve; a lone value, or a value split off by a gap, is never
 *  drawn as a dot or a line — it stays readable in the tooltip, the ranking and
 *  the request records. Missing / unreported / unknown never becomes 0, and no
 *  curve bridges a gap or joins two models. */

export type Metric = "tokens" | "requests" | "speed";
/** How Token / 费用 / 请求 are drawn; 速度 is always a line. */
export type Shape = "bar" | "line";
export const METRICS: readonly { id: Metric; label: string }[] = [
  { id: "tokens", label: "Token" },
  { id: "requests", label: "请求" },
  { id: "speed", label: "速度" },
];
const FIELD: Readonly<Record<Metric, keyof UmBucket>> = {
  tokens: "tokens",
  requests: "requests",
  speed: "speed",
};
const AXIS: Readonly<Record<Metric, (n: number) => string>> = {
  tokens,
  requests: full,
  speed: (n) => full(n),
};
/** Tooltip / value shapes; the ranking reads whole counts (`RANK_VALUE`). */
export const VALUE: Readonly<Record<Metric, (n: number) => string>> = {
  tokens,
  requests: (n) => `${full(n)} 次`,
  speed,
};
/** The ranking prefers complete thousands for counts; money and speed keep units. */
const RANK_VALUE: Readonly<Record<Metric, (n: number) => string>> = {
  tokens: full,
  requests: (n) => `${full(n)} 次`,
  speed,
};
const SLOTS = 4;
const OTHER = "其他模型";
/** What the drawn chart says about the window. `insufficient`: confirmable
 *  values exist, but no two consecutive ones, so no curve can be drawn. */
export type ChartState = "empty" | "insufficient" | "zero" | "drawn";

/** Colour slots follow the model, not its rank: a model keeps its slot while
 *  it is in the window, so switching metric or window never repaints a
 *  survivor. A model that left the window frees its slot for a newcomer.
 *  Models past the four validated slots fold into 「其他模型」 (additive
 *  metrics) or keep a neutral dashed line of their own (速度). */
export class SeriesColors {
  private slots = new Map<string, number>();
  /** `present`: every model in the window; `wanted`: the ones to colour, in order. */
  assign(present: readonly string[], wanted: readonly string[]): void {
    const here = new Set(present);
    for (const model of [...this.slots.keys()])
      if (!here.has(model)) this.slots.delete(model);
    for (const model of wanted) {
      if (this.slots.has(model)) continue;
      const used = new Set(this.slots.values());
      const free = [1, 2, 3, 4].find((slot) => !used.has(slot));
      if (free === undefined) return;
      this.slots.set(model, free);
    }
  }
  slot(model: string): number | null {
    return this.slots.get(model) ?? null;
  }
}
export function swatch(slot: number | null): string {
  return slot === null ? "var(--um-chart-other)" : `var(--md-chart-${slot})`;
}

type Series = {
  name: string;
  slot: number | null;
  values: Reading[];
  /** The models this series stands for: one, or the folded rest. */
  models: string[];
  /** True when this series is the 「其他模型」 total, not one model. */
  folded: boolean;
  /** At least one run of two or more consecutive known values exists. */
  drawn: boolean;
};

/** The numeric value a chart may draw. `drawable` decides by kind; a NaN or an
 *  Infinity from a broken controller would poison a whole path, so it is a gap. */
function plotValue(r: Reading | undefined): number | null {
  if (!r) return null;
  const v = drawable(r);
  return v !== null && Number.isFinite(v) ? v : null;
}

/** The drawn series: slotted models in trend order, the rest folded into one
 *  「其他模型」 series. A speed is not foldable, so for 速度 every remaining
 *  model keeps its own neutral line: no model with data leaves the chart. */
function seriesOf(
  trend: UmTrend,
  metric: Metric,
  colors: SeriesColors
): Series[] {
  colors.assign(
    trend.models.map((m) => m.model),
    trend.models.slice(0, SLOTS).map((m) => m.model)
  );
  const field = FIELD[metric];
  const make = (
    name: string,
    slot: number | null,
    values: Reading[],
    models: string[],
    folded = false
  ): Series => ({
    name,
    slot,
    values,
    models,
    folded,
    drawn: drawableRuns(values).some((run) => run.length > 1),
  });
  const own: Series[] = [],
    rest: UmTrend["models"] = [];
  for (const m of trend.models) {
    const slot = colors.slot(m.model);
    const values = m.daily.map((b) => b[field]);
    if (slot === null) rest.push(m);
    else own.push(make(m.model, slot, values, [m.model]));
  }
  if (metric === "speed")
    return [
      ...own,
      ...rest.map((m) =>
        make(
          m.model,
          null,
          m.daily.map((b) => b[field]),
          [m.model]
        )
      ),
    ];
  if (rest.length === 0) return own;
  if (rest.length === 1)
    return [
      ...own,
      make(
        rest[0].model,
        null,
        rest[0].daily.map((b) => b[field]),
        [rest[0].model]
      ),
    ];
  const values = trend.days.map((_, i) =>
    sumReadings(rest.map((m) => m.daily[i][field]))
  );
  return [
    ...own,
    make(
      `${OTHER}（${rest.length}）`,
      null,
      values,
      rest.map((m) => m.model),
      true
    ),
  ];
}
/** A folded total: absent members never become 0, and a day where every member
 *  is absent is missing — not a real 0 at the baseline. */
function sumReadings(list: Reading[]): Reading {
  let total = 0,
    lower = false,
    estimate = false,
    any = false;
  for (const r of list) {
    const v = plotValue(r);
    if (v === null) {
      lower = true;
      continue;
    }
    any = true;
    total += v;
    if (r.kind === "atLeast") lower = true;
    if (r.kind === "estimate") estimate = true;
  }
  if (!any) return { kind: "missing", note: "这些模型当天都没有可确认的数值" };
  return lower
    ? { kind: "atLeast", value: total }
    : estimate
      ? { kind: "estimate", value: total }
      : { kind: "exact", value: total };
}

function niceStep(max: number): number {
  const raw = max / 4;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step =
    [1, 2, 2.5, 5, 10].map((f) => f * power).find((s) => s >= raw) ?? raw;
  return step;
}

/** One point of a drawn run: its day index and the value on the axis. */
export type DrawablePoint = { index: number; value: number };

/** Runs of consecutive known values, per series. A missing, unreported or unknown
 *  reading ends a run; a real 0 continues it. A run of one stays a run of one:
 *  the caller must not draw it as a curve or a dot. */
export function drawableRuns(values: readonly Reading[]): DrawablePoint[][] {
  const runs: DrawablePoint[][] = [];
  let run: DrawablePoint[] = [];
  values.forEach((r, index) => {
    const value = plotValue(r);
    if (value === null) {
      if (run.length) runs.push(run);
      run = [];
      return;
    }
    run.push({ index, value });
  });
  if (run.length) runs.push(run);
  return runs;
}

/** A monotone cubic (Fritsch–Carlson / PCHIP) path through `points`, or null
 *  below two points. Each segment's tangents are limited so the curve stays
 *  monotone between its own endpoints: it passes every real sample and never
 *  rises above the run's peak or falls below its trough — in screen space too,
 *  because screen y is a negative affine of the value. Control points are clamped
 *  to the segment's own y range as a last guard against floating-point drift. */
export function monotonePath(
  points: readonly { x: number; y: number }[]
): string | null {
  const n = points.length;
  if (n < 2) return null;
  const h: number[] = [],
    d: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    const dx = points[i + 1].x - points[i].x;
    if (!(dx > 0) || !Number.isFinite(dx)) return null;
    h.push(dx);
    d.push((points[i + 1].y - points[i].y) / dx);
  }
  const m: number[] = new Array(n);
  m[0] = d[0];
  m[n - 1] = d[n - 2];
  for (let i = 1; i < n - 1; i++) {
    if (d[i - 1] * d[i] <= 0) m[i] = 0;
    else {
      const t = (d[i - 1] + d[i]) / 2;
      const a = d[i - 1] / t,
        b = d[i] / t;
      m[i] = a * a + b * b > 9 ? (3 * t) / Math.hypot(a, b) : t;
    }
  }
  const f = (v: number) => v.toFixed(2);
  const clamp = (v: number, lo: number, hi: number) =>
    Math.min(hi, Math.max(lo, v));
  let path = `M${f(points[0].x)},${f(points[0].y)}`;
  for (let i = 0; i < n - 1; i++) {
    const dx = h[i],
      lo = Math.min(points[i].y, points[i + 1].y),
      hi = Math.max(points[i].y, points[i + 1].y);
    const x1 = points[i].x + dx / 3,
      y1 = points[i].y + (m[i] * dx) / 3;
    const x2 = points[i + 1].x - dx / 3,
      y2 = points[i + 1].y - (m[i + 1] * dx) / 3;
    path += `C${f(x1)},${f(clamp(y1, lo, hi))} ${f(x2)},${f(clamp(y2, lo, hi))} ${f(points[i + 1].x)},${f(points[i + 1].y)}`;
  }
  return path;
}

export type Geometry = {
  width: number;
  height: number;
  left: number;
  top: number;
  plotW: number;
  plotH: number;
  step: number;
  n: number;
};

export class TrendView {
  trend: UmTrend | null = null;
  metric: Metric = "tokens";
  shape: Shape = "bar";
  active: number | null = null;
  /** The series a hovered / focused ranking row stands for; others are dimmed. */
  focusSeries: number | null = null;
  geometry: Geometry | null = null;
  /** What the drawing shows, by value. A controller may change readings in
   *  place, so the console repaints when this changes, not when an object does. */
  signature = "";
  /** What the window can say; computed from the data, not from a paint. */
  state: ChartState = "empty";
  /** Which quality the drawn lines carry: a lower bound, an estimate, or both. */
  quality = { lower: false, estimated: false };
  private series: Series[] = [];
  /** Points that belong to a drawn run, per series and day; null elsewhere. */
  private cursorPoints: ({ x: number; y: number } | null)[][] = [];
  private cursorAt: number | null = null;
  constructor(readonly colors: SeriesColors) {}

  /** Lines: 速度 always, Token / 费用 / 请求 when the reader asked for 折线图. */
  get lines(): boolean {
    return this.metric === "speed" || this.shape === "line";
  }

  set(trend: UmTrend, metric: Metric, shape: Shape = "bar"): void {
    const previous = this.series
      .map((s) => `${s.name}\u0000${s.slot}`)
      .join("\u0001");
    this.trend = trend;
    this.metric = metric;
    this.shape = shape;
    this.series = seriesOf(trend, metric, this.colors);
    // A row points at a series by index: a changed series list invalidates it.
    if (
      this.series.map((s) => `${s.name}\u0000${s.slot}`).join("\u0001") !==
      previous
    )
      this.focusSeries = null;
    const field = FIELD[metric];
    const figures = (values: Reading[]) =>
      values.map((r) => [r.kind, plotValue(r)]);
    this.signature = JSON.stringify([
      metric,
      this.lines,
      trend.days,
      figures(trend.all.daily.map((b) => b[field])),
      this.series.map((s) => [s.name, s.slot, figures(s.values)]),
    ]);
    if (this.active !== null && this.active >= trend.days.length)
      this.active = null;
    const values = this.series.flatMap((s) => s.values);
    const any = values.some((r) => plotValue(r) !== null);
    // Only a drawn curve carries a line style: a lower-bound or estimated value
    // that is not part of a two-point run is never drawn, so the scope line must
    // not claim a dash the chart does not show. Bars carry no line style at all.
    this.quality = { lower: false, estimated: false };
    if (this.lines) {
      for (const s of this.series) {
        for (const run of drawableRuns(s.values)) {
          if (run.length < 2) continue;
          for (const p of run) {
            const kind = s.values[p.index].kind;
            if (kind === "atLeast") this.quality.lower = true;
            if (kind === "estimate") this.quality.estimated = true;
          }
        }
      }
    }
    if (!any) this.state = "empty";
    else if (this.lines && !this.series.some((s) => s.drawn))
      this.state = "insufficient";
    else this.state = this.maxValue() === 0 ? "zero" : "drawn";
  }

  private columnMax(i: number): number {
    return this.lines
      ? Math.max(0, ...this.series.map((s) => plotValue(s.values[i]) ?? 0))
      : this.series.reduce((sum, s) => sum + (plotValue(s.values[i]) ?? 0), 0);
  }
  private maxValue(): number {
    const n = this.trend?.days.length ?? 0;
    let max = 0;
    for (let i = 0; i < n; i++) max = Math.max(max, this.columnMax(i));
    return max;
  }

  /** Paint into `host` at its current width. */
  paint(host: HTMLElement): void {
    const trend = this.trend;
    if (!trend) return;
    // 160 leaves the y-axis labels and a readable plot even on a phone; the
    // SVG's CSS max-width guards the frame before a resize repaint.
    const width = Math.max(160, Math.floor(host.clientWidth));
    // The plot stretches to the ranking's height (both share one grid row), so the
    // chart is never a strip beside a taller list; 248px is the floor.
    const height = Math.max(248, Math.round(host.clientHeight));
    const n = trend.days.length;
    const columns = Math.max(1, n);
    const stacked = !this.lines;
    const max = this.maxValue();
    const step = max > 0 ? niceStep(max) : 1;
    const axisMax = step * 4;
    const ticks = [0, 1, 2, 3, 4].map((k) => k * step);
    const labelW =
      Math.ceil(
        Math.max(...ticks.map((t) => textWidth(AXIS[this.metric](t))))
      ) + 12;
    const g: Geometry = {
      width,
      height,
      left: labelW,
      top: 10,
      plotW: width - labelW - 6,
      plotH: height - 10 - 28,
      step: 0,
      n,
    };
    g.step = g.plotW / columns;
    this.geometry = g;
    const y = (v: number) => g.top + g.plotH - (v / axisMax) * g.plotH;
    const cx = (i: number) => g.left + g.step * (i + 0.5);
    const parts: string[] = [];
    // A window with nothing in it gets no invented scale: the baseline and why.
    for (const t of max > 0 ? ticks : [0]) {
      parts.push(
        `<line class="um-grid" x1="${g.left}" x2="${width}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"/>`
      );
      parts.push(
        `<text class="um-axis" x="${g.left - 8}" y="${(y(t) + 3.5).toFixed(1)}" text-anchor="end">${esc(AXIS[this.metric](t))}</text>`
      );
    }
    host.dataset.umChartState = this.state;
    if (this.state === "empty") {
      parts.push(
        `<text class="um-chart-empty" x="${(g.left + g.plotW / 2).toFixed(1)}" y="${(g.top + g.plotH / 2).toFixed(1)}" text-anchor="middle">这段时间没有可画的${esc(METRICS.find((m) => m.id === this.metric)!.label)}数据</text>`
      );
    } else if (this.state === "insufficient") {
      parts.push(
        `<text class="um-chart-empty" x="${(g.left + g.plotW / 2).toFixed(1)}" y="${(g.top + g.plotH / 2).toFixed(1)}" text-anchor="middle">连续有效数据不足，暂无可绘制曲线</text>`
      );
    } else if (this.state === "zero") {
      parts.push(
        `<text class="um-chart-note" x="${(g.left + g.plotW / 2).toFixed(1)}" y="${(g.top + g.plotH / 2).toFixed(1)}" text-anchor="middle">本时段可确认的值都是 0</text>`
      );
    }
    parts.push(
      `<rect class="um-col-hl" x="0" y="${g.top}" width="${g.step.toFixed(1)}" height="${g.plotH}" rx="6" visibility="hidden"/>`
    );
    // Day labels from today backwards, as many as fit without touching. A centred
    // label on the last column would hang past the right edge when the columns
    // are narrow, so the first and last labels are kept inside the SVG.
    const every = Math.max(1, Math.ceil(40 / g.step));
    for (let i = n - 1; i >= 0; i -= every) {
      const label = monthDay(trend.days[i]);
      const half = textWidth(label) / 2;
      const x = Math.min(width - 2 - half, Math.max(g.left + 2 + half, cx(i)));
      parts.push(
        `<text class="um-axis" x="${x.toFixed(1)}" y="${height - 8}" text-anchor="middle">${esc(label)}</text>`
      );
    }
    this.cursorPoints = this.series.map(() => trend.days.map(() => null));
    if (stacked) {
      const barW = Math.max(4, Math.min(34, g.step * 0.62));
      trend.days.forEach((_, i) => {
        let base = 0;
        const drawn = this.series
          .map((s, si) => ({ s, si, v: plotValue(s.values[i]) ?? 0 }))
          .filter((d) => d.v > 0);
        drawn.forEach((d, k) => {
          const y0 = y(base),
            y1 = y(base + d.v);
          base += d.v;
          const last = k === drawn.length - 1;
          const h = y0 - y1 - (last || y0 - y1 < 3 ? 0 : 2);
          if (h <= 0) return;
          const x = cx(i) - barW / 2;
          const r = last ? Math.min(4, barW / 2, h) : 0;
          parts.push(
            `<path class="um-bar" data-s="${d.si}" fill="${swatch(d.s.slot)}" d="${roundTop(x, y0 - h, barW, h, r)}"/>`
          );
        });
        if (columnAbsent(this.series, i))
          parts.push(
            `<line class="um-absent-mark" x1="${(cx(i) - barW / 2).toFixed(1)}" x2="${(cx(i) + barW / 2).toFixed(1)}" y1="${y(0) - 1}" y2="${y(0) - 1}"/>`
          );
      });
    } else {
      this.series.forEach((s, si) => {
        // A model without a colour slot keeps its own line, neutral and dashed.
        const cls = s.slot === null ? "um-line um-line-other" : "um-line";
        for (const run of drawableRuns(s.values)) {
          // One value is not a curve; it stays in the tooltip, ranking and records.
          if (run.length < 2) continue;
          const points = run.map((p) => ({ x: cx(p.index), y: y(p.value) }));
          const d = monotonePath(points);
          if (!d) continue;
          const q = run.some((p) => s.values[p.index].kind === "atLeast")
            ? "lower"
            : run.some((p) => s.values[p.index].kind === "estimate")
              ? "estimated"
              : "exact";
          parts.push(
            `<path class="${cls}" data-s="${si}" data-quality="${q}" stroke="${swatch(s.slot)}" d="${d}"/>`
          );
          points.forEach((point, k) => {
            this.cursorPoints[si][run[k].index] = point;
          });
        }
      });
    }
    parts.push(
      `<g class="um-cursor" visibility="hidden"><line class="um-cursor-line" x1="0" x2="0" y1="${g.top.toFixed(1)}" y2="${(g.top + g.plotH).toFixed(1)}"/><g class="um-cursor-dots"></g></g>`
    );
    host.innerHTML = `<svg class="um-chart-svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" aria-hidden="true">${parts.join("")}</svg>`;
    this.cursorAt = null;
    this.highlight(host);
  }

  /** Column index under a client x, or null outside the plot. */
  indexAt(host: HTMLElement, clientX: number): number | null {
    const g = this.geometry;
    if (!g || g.n === 0) return null;
    const x = clientX - host.getBoundingClientRect().left - g.left;
    if (x < 0 || x > g.plotW) return null;
    return Math.min(g.n - 1, Math.max(0, Math.floor(x / g.step)));
  }

  /** The column band, the guide line and the markers attached to drawn curves.
   *  A column with no drawn curve (a gap, a lone value) gets no marker and no
   *  guide: only the tooltip and the ranking carry its confirmable values. */
  highlight(host: HTMLElement): void {
    const g = this.geometry;
    if (!g) return;
    const rect = host.querySelector<SVGRectElement>(".um-col-hl");
    if (rect) {
      if (this.active === null) rect.setAttribute("visibility", "hidden");
      else {
        rect.setAttribute("x", (g.left + g.step * this.active).toFixed(1));
        rect.setAttribute("visibility", "visible");
      }
    }
    const cursor = host.querySelector<SVGGElement>(".um-cursor");
    const guide = host.querySelector<SVGLineElement>(".um-cursor-line");
    const dots = host.querySelector<SVGGElement>(".um-cursor-dots");
    const active = this.active;
    const marks =
      active === null
        ? []
        : this.cursorPoints
            .map((points, si) => ({ si, point: points[active] }))
            .filter(
              (m): m is { si: number; point: { x: number; y: number } } =>
                m.point !== null && m.point !== undefined
            );
    if (cursor && guide && dots) {
      if (active === null || marks.length === 0) {
        cursor.setAttribute("visibility", "hidden");
        this.cursorAt = null;
      } else {
        const x = (g.left + g.step * (active + 0.5)).toFixed(1);
        guide.setAttribute("x1", x);
        guide.setAttribute("x2", x);
        if (this.cursorAt !== active) {
          dots.innerHTML = marks
            .map(
              (m) =>
                `<circle class="um-cursor-dot" data-s="${m.si}" cx="${m.point.x.toFixed(1)}" cy="${m.point.y.toFixed(1)}" r="3.5" fill="${swatch(this.series[m.si]?.slot ?? null)}"/>`
            )
            .join("");
          this.cursorAt = active;
        }
        cursor.setAttribute("visibility", "visible");
      }
    }
    this.paintFocus(host);
  }

  /** Dim every series but the one a ranking row points at; no row, no dimming. */
  private paintFocus(host: HTMLElement): void {
    const focus =
      this.focusSeries !== null && this.series[this.focusSeries]
        ? this.focusSeries
        : null;
    for (const el of host.querySelectorAll<SVGElement>("[data-s]")) {
      const on = focus === null || Number(el.dataset.s) === focus;
      el.classList.toggle("is-dim", !on);
      el.classList.toggle("is-focus", focus !== null && on);
    }
  }

  /** Client point the tooltip of the active column points at: the highest drawn
   *  point of that column, or the plot's top when no curve passes through it. */
  anchor(host: HTMLElement): { x: number; y: number } | null {
    const g = this.geometry;
    if (!g || this.active === null) return null;
    const box = host.getBoundingClientRect();
    const ys = this.cursorPoints
      .map((points) => points[this.active!])
      .filter(
        (p): p is { x: number; y: number } => p !== null && p !== undefined
      )
      .map((p) => p.y);
    return {
      x: box.left + g.left + g.step * (this.active + 0.5),
      y: box.top + (ys.length ? Math.min(...ys) : g.top),
    };
  }

  tipHtml(index: number): string {
    const trend = this.trend!;
    const day = trend.days[index];
    const today = day === dayKey(Date.now());
    const fmt = VALUE[this.metric];
    const rows = this.series
      .map(
        (s) =>
          `<div class="um-tip-row" data-um-series-name="${esc(s.name)}"><i style="background:${swatch(s.slot)}"></i><span title="${esc(s.name)}">${esc(s.name)}</span><b>${readingHtml(s.values[index], fmt)}</b></div>`
      )
      .join("");
    const all = trend.all.daily[index];
    const total =
      this.metric === "speed"
        ? `<span>全部模型</span><b>${readingHtml(all.speed, speed)}</b><span>${esc(speedBasisWord(trend.speedBasis))}</span>`
        : `<span>合计</span><b>${readingHtml(all[FIELD[this.metric]], fmt)}</b>${this.metric === "requests" ? "" : `<span>· ${readingHtml(all.requests, (n) => `${full(n)} 个请求`)}</span>`}`;
    return `<div class="um-tip-title">${esc(dayTitle(day))}${today ? "（今天，未结束）" : ""}</div>${rows || `<div class="um-tip-row"><span>当天没有模型记录</span></div>`}<div class="um-tip-total">${total}</div>`;
  }

  /** Accessible summary of the drawn window. */
  label(): string {
    const trend = this.trend;
    if (!trend) return "";
    const metric = METRICS.find((m) => m.id === this.metric)!.label;
    const base = `${monthDay(trend.days[0])} 至 ${monthDay(trend.days[trend.days.length - 1])} 每日${metric}，按模型${this.lines ? "画线" : "堆叠"}。`;
    if (this.state === "empty")
      return `${base}这段时间没有可画的${metric}数据。`;
    if (this.state === "insufficient")
      return `${base}连续有效数据不足，暂无可绘制曲线；可确认的数值见悬浮明细与排行。用左右方向键逐日查看当天各模型的数值。`;
    if (this.state === "zero")
      return `${base}本时段可确认的值都是 0。用左右方向键逐日查看当天各模型的数值。`;
    return `${base}用左右方向键逐日查看当天各模型的数值。`;
  }

  /** The series a model is drawn as on its own, or -1. In line mode a lone value
   *  is not a curve; in bar mode a real 0 (or nothing) draws no bar. Either way
   *  the row has no visible mark to point at, so it must not dim other series. */
  private seriesOfModel(model: string): number {
    const at = this.series.findIndex(
      (s) => !s.folded && s.models.length === 1 && s.models[0] === model
    );
    if (at < 0) return -1;
    return this.lines
      ? this.series[at].drawn
        ? at
        : -1
      : this.series[at].values.some((r) => (plotValue(r) ?? 0) > 0)
        ? at
        : -1;
  }

  /** Right-hand ranking over the same window, by the selected metric. */
  rankingHtml(): string {
    const trend = this.trend;
    if (!trend) return "";
    const field = FIELD[this.metric];
    const fmt = RANK_VALUE[this.metric];
    const ranked = [...trend.models].sort(
      (a, b) =>
        (plotValue(b.total[field]) ?? -1) - (plotValue(a.total[field]) ?? -1)
    );
    if (!ranked.length)
      return `<p class="um-rank-empty">这段时间没有记录到 UM 请求。</p>`;
    const top = plotValue(ranked[0].total[field]) ?? 0;
    const shown = ranked.slice(0, 5);
    const folded = this.series.find((s) => s.folded) ?? null;
    const rows = shown
      .map((m) => {
        const v = plotValue(m.total[field]);
        const width =
          v === null || v <= 0 || top <= 0
            ? 0
            : Math.max(0.75, Math.min(100, (v / top) * 100));
        const slot = this.colors.slot(m.model);
        const si = this.seriesOfModel(m.model);
        const inFold = folded?.models.includes(m.model) ?? false;
        const days = m.daily.reduce(
          (count, b) => count + (plotValue(b[field]) !== null ? 1 : 0),
          0
        );
        const meta = [
          // The row value already is the request count under 请求; repeating it in the
          // secondary line would print the same figure twice.
          this.metric === "requests"
            ? null
            : readingHtml(m.total.requests, (n) => `${full(n)} 个请求`),
          this.metric === "speed"
            ? readingHtml(m.total.tokens, (n) => `${full(n)} Token`)
            : speedReading(m.total.speed),
          failureText(m.failed),
        ]
          .filter(Boolean)
          .join(" · ");
        const note = inFold
          ? `<p class="um-rank-note">图中并入「${esc(folded!.name)}」合计，无单独曲线</p>`
          : this.lines && si < 0 && days > 0
            ? `<p class="um-rank-note">仅 ${days} 天有值，未连线</p>`
            : "";
        const sw = inFold
          ? `<i class="um-swatch" data-folded="true" aria-hidden="true" title="图中并入「${esc(folded!.name)}」"></i>`
          : `<i class="um-swatch" style="background:${swatch(slot)}" aria-hidden="true"></i>`;
        return `<li class="um-rank-row" data-um-rank="${esc(m.model)}"${si >= 0 ? ` data-um-series="${si}"` : ""} tabindex="0">
        <div class="um-rank-head">${sw}<span class="um-rank-name" title="${esc(m.model)}">${esc(m.model)}</span><span class="um-rank-value">${this.metric === "speed" ? speedReading(m.total.speed) : readingHtml(m.total[field], fmt)}</span></div>
        <div class="um-rank-bar" aria-hidden="true"><span style="width:${width.toFixed(1)}%;${v !== null && v > 0 ? "min-width:2px;" : ""}background:${inFold ? "var(--md-outline-variant)" : swatch(slot)}"></span></div>
        <div class="um-rank-meta">${meta}</div>${note}</li>`;
      })
      .join("");
    const hidden = ranked.length - shown.length;
    const foot = [
      hidden > 0 ? `另有 ${hidden} 个模型未列出，见请求记录` : "",
      folded
        ? `图中「${esc(folded.name)}」为 ${folded.models.length} 个未分配颜色的模型合计`
        : "",
    ]
      .filter(Boolean)
      .join("；");
    const scale = `按${esc(METRICS.find((m) => m.id === this.metric)!.label)}排序 · 条长＝相对本时段最高，非合计占比${this.metric === "speed" ? " · 速度不相加" : ""}`;
    return `<p class="um-rank-scale">${scale}</p><ol class="um-rank">${rows}</ol>${foot ? `<p class="um-rank-more">${foot}。</p>` : ""}`;
  }
}

/** A speed cell: a missing speed is 「无速度样本」, not a bare 「缺失」. */
function speedReading(r: Reading): string {
  return r.kind === "missing"
    ? `<span class="um-absent" title="${esc(r.note || "这段时间没有可计时的请求")}">无速度样本</span>`
    : readingHtml(r, speed);
}
/** The failure cell: only a real 0 is silent. A lower bound or an estimate
 *  keeps its ≥ / ≈ sign (the same shape the KPI uses), and an absent figure
 *  keeps its own word instead of being read as 「缺失」. */
function failureText(r: Reading): string | null {
  if (r.kind === "exact" && r.value === 0) return null;
  if (r.kind === "exact" || r.kind === "atLeast" || r.kind === "estimate") {
    return `<span class="um-err-text">${readingHtml(r, (n) => `${full(n)} 次失败`)}</span>`;
  }
  const word =
    r.kind === "unreported"
      ? "失败未报告"
      : r.kind === "unknown"
        ? "失败数未知"
        : "失败数缺失";
  return `<span class="um-absent">${word}</span>`;
}

let measure: CanvasRenderingContext2D | null = null;
/** Width of an axis label in the axis font (CJK units are wider than digits).
 *  Keep this in step with .um-axis's font-size: a mismatch makes labelW too
 *  small and the y-axis label crosses the SVG's left edge. */
function textWidth(text: string): number {
  measure ??= document.createElement("canvas").getContext("2d");
  if (!measure) return text.length * 12;
  measure.font = `12px ${getComputedStyle(document.documentElement).getPropertyValue("--md-font") || "sans-serif"}`;
  return measure.measureText(text).width;
}

/** No series has a drawable figure for the day: unknown / unreported / missing. */
function columnAbsent(series: Series[], i: number): boolean {
  return (
    series.length > 0 && series.every((s) => plotValue(s.values[i]) === null)
  );
}

/** A bar segment whose top corners are rounded by r (flat at the baseline). */
function roundTop(
  x: number,
  y: number,
  w: number,
  h: number,
  r: number
): string {
  const f = (n: number) => n.toFixed(2);
  if (r <= 0) return `M${f(x)},${f(y)}h${f(w)}v${f(h)}h${f(-w)}z`;
  return `M${f(x)},${f(y + h)}v${f(-(h - r))}a${f(r)},${f(r)} 0 0 1 ${f(r)},${f(-r)}h${f(w - 2 * r)}a${f(r)},${f(r)} 0 0 1 ${f(r)},${f(r)}v${f(h - r)}z`;
}
