// Appends marked lines to a local run-log file and records when each write
// happened, so the prototype metrics can be paired with the line's `t=` marker.
//
// Usage:
//   node inject-log.mjs --log tmp/109-proto/service.log --out tmp/109-proto/inject.jsonl \
//     --count 25 --interval 300 --tag latency

import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

function arg(name, fallback) {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const log = arg("log", "tmp/109-proto/service.log");
const out = arg("out", "tmp/109-proto/inject.jsonl");
const count = Number(arg("count", "25"));
const interval = Number(arg("interval", "300"));
const tag = arg("tag", "latency");

if (!Number.isInteger(count) || count < 1) throw new Error("--count must be a positive integer");
if (!Number.isFinite(interval) || interval < 0) throw new Error("--interval must be >= 0");

mkdirSync(dirname(log), { recursive: true });
mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, "");

function stamp(date) {
  const pad = (value, width = 2) => String(value).padStart(width, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())},${pad(date.getMilliseconds(), 3)}`;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

for (let index = 0; index < count; index += 1) {
  const at = Date.now();
  const id = `109-${tag}-${at}-${index}`;
  const line = `${stamp(new Date(at))} INFO PROTO109 id=${id} t=${at} probe\n`;
  appendFileSync(log, line, "utf8");
  appendFileSync(out, `${JSON.stringify({ id, written_ms: at, tag })}\n`, "utf8");
  if (index + 1 < count && interval > 0) await sleep(interval);
}

console.log(`wrote ${count} lines to ${log}; manifest ${out}`);
