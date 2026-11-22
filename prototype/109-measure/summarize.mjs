// Summarizes the prototype measurement JSONL files.
//
// Usage:
//   node summarize.mjs --metrics tmp/109-slint/metrics.jsonl \
//     --inject tmp/109-proto/inject.jsonl [--json]

import { readFileSync } from "node:fs";

function arg(name, fallback) {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

function readJsonl(path) {
  return readFileSync(path, "utf8")
    .split(/\r?\n/)
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line));
}

function percentile(sorted, fraction) {
  if (sorted.length === 0) return null;
  const rank = Math.max(1, Math.ceil(fraction * sorted.length));
  return sorted[rank - 1];
}

const metrics = readJsonl(arg("metrics", "tmp/109-slint/metrics.jsonl"));
const injected = readJsonl(arg("inject", "tmp/109-proto/inject.jsonl"));
const written = new Map(injected.map((row) => [row.id, row.written_ms]));

const frame = metrics.filter((row) => row.name === "log_visible_frame");
const visible = metrics.filter((row) => row.name === "log_visible");
const source = frame.length > 0 ? frame : visible;
const samples = [];
for (const row of source) {
  const ids = Array.isArray(row.extra?.ids)
    ? row.extra.ids
    : row.extra?.id
      ? [row.extra.id]
      : [];
  for (const id of ids) {
    const at = written.get(id);
    if (at !== undefined) {
      samples.push({ id, latency_ms: row.at_ms - at });
    }
  }
}

const latencies = samples.map((sample) => sample.latency_ms).sort((a, b) => a - b);
const first = (name) => metrics.find((row) => row.name === name);

const processStart = first("process_start");
const uiReady = first("ui_ready") ?? first("ui_interactive");
const trayClick = first("tray_click");
const trayShown = first("tray_restore_shown");
const trayEvent = first("tray_restored_event");
const trayVisible = first("tray_restore_visible") ?? first("tray_restored_visible");

const summary = {
  latency: {
    source: frame.length > 0 ? "log_visible_frame" : "log_visible",
    count: latencies.length,
    min_ms: latencies[0] ?? null,
    p50_ms: percentile(latencies, 0.5),
    p95_ms: percentile(latencies, 0.95),
    max_ms: latencies[latencies.length - 1] ?? null,
    samples,
  },
  cold_start: {
    process_start_ms: processStart?.at_ms ?? null,
    ui_ready_ms: uiReady ? uiReady.at_ms - processStart.at_ms : null,
  },
  tray_restore: {
    click_ms: trayClick?.at_ms ?? null,
    shown_ms: trayShown && trayClick ? trayShown.at_ms - trayClick.at_ms : null,
    event_ms: trayEvent && trayClick ? trayEvent.at_ms - trayClick.at_ms : null,
    visible_ms: trayVisible && trayClick ? trayVisible.at_ms - trayClick.at_ms : null,
  },
  record_counts: metrics.reduce((counts, row) => {
    counts[row.name] = (counts[row.name] ?? 0) + 1;
    return counts;
  }, {}),
};

console.log(JSON.stringify(summary, null, 2));
