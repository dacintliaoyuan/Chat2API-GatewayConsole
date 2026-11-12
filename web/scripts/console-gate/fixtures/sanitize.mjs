// Build the committed console-gate fixtures (api.json) from a LOCAL read-only
// capture of the console's GET reads. The capture itself is private (real usage,
// key names, paths, log lines) and must stay under the gitignored output/ tree;
// only this script's output is committed.
//
//   node scripts/console-gate/fixtures/sanitize.mjs <capture.json> [--anchor YYYY-MM-DD]
//
// Capture format: { "<path>?<query>": { status, body } } - the same keys the
// fixture server answers (paths relative to /gw/usagesrv). Re-capture after a
// backend read changes shape (see ../README.md), then re-run this script.
//
// What it does (deterministic: no clock, no randomness beyond a fixed seed):
//   - endpoint keys, JSON shapes, types and nullability are kept exactly;
//   - identifiers are replaced by counters of the same format and kept consistent
//     across endpoints (share key id <-> share-usage key_id, operation ids,
//     preview tokens, request ids, broker instance ids, PIDs, log cursors);
//   - share key names / masked keys / the share URL / local paths are replaced by
//     synthetic values; log message bodies are regenerated from templates
//     (synthetic numbers and ids), and a few warning / error / cut / unparsed rows
//     are appended so those states stay covered;
//   - aggregate token, cost, credit and duration amounts (summary, usage-tokens,
//     usage-rates, stats, share-usage) are scaled by one factor, which keeps sums
//     and rates internally consistent while not publishing the real volumes. The
//     factor is derived from the capture's own hash (deterministic for one capture,
//     in [0.25, 1.75)) and is neither printed nor written anywhere, so the
//     committed numbers cannot be divided back into the real ones;
//   - model ids, public price data (incl. its documentation URLs) and backend
//     schema / status text are kept: they are product data, not private data;
//   - "_anchor" records the capture day; shift.mjs moves every date to "today".
// Finally it re-scans the output for every private value it replaced plus generic
// patterns (paths, hosts, e-mail, the local account name, long tokens) and refuses
// to write on any hit.
import { readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { userInfo } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2);
const capturePath = argv.find((a) => !a.startsWith("--"));
if (!capturePath) {
  console.error(
    "usage: node sanitize.mjs <capture.json> [--anchor YYYY-MM-DD]"
  );
  process.exit(2);
}
const anchorArg = argv.includes("--anchor")
  ? argv[argv.indexOf("--anchor") + 1]
  : null;
const captureBytes = readFileSync(capturePath);
const capture = JSON.parse(captureBytes.toString("utf8"));

// ------------------------------------------------------------------ helpers
const SCALE =
  0.25 +
  (parseInt(
    createHash("sha256").update(captureBytes).digest("hex").slice(0, 8),
    16
  ) /
    0x100000000) *
    1.5;
const SCALED_ENDPOINTS =
  /^\/api\/(summary|console\/(usage-tokens|usage-rates|stats|share-usage))(\?|$)/;
const AMOUNT_KEY =
  /^(input|output|cache|reasoning|tokens?|token_total|partial_token_total|estimated_token_total|costCny|costUsd|knownCostUsd|estimatedCostUsd|estimated_cost_cny|credits|duration_ms)$|_tokens$|Tokens$/;

let seed = 0x5eed1234;
const rand = () => {
  seed |= 0;
  seed = (seed + 0x6d2b79f5) | 0;
  let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};
const randInt = (lo, hi) => lo + Math.floor(rand() * (hi - lo + 1));

const decimals = (v) => {
  const s = String(v);
  const i = s.indexOf(".");
  return i < 0 ? 0 : Math.min(6, s.length - i - 1);
};
const scale = (v) =>
  Number.isInteger(v)
    ? Math.round(v * SCALE)
    : Number((v * SCALE).toFixed(decimals(v)));

/** Consistent replacement maps: the same private value always gets the same fake. */
const maps = new Map();
const replaced = new Set();
const generated = new Set();
function fake(kind, real, make) {
  if (real === null || real === undefined || real === "") return real;
  if (!maps.has(kind)) maps.set(kind, new Map());
  const m = maps.get(kind);
  if (!m.has(real)) {
    const made = make(m.size + 1, real);
    m.set(real, made);
    if (typeof real === "string" && real.length >= 4) replaced.add(real);
    if (typeof made === "string") generated.add(made);
  }
  return m.get(real);
}
const gen = (value) => {
  generated.add(value);
  return value;
};
const hexOf = (n, len) => n.toString(16).padStart(len, "0").slice(-len);
const fakeHex = (n, len) =>
  ("f17e" + "0".repeat(len)).slice(0, len - 8) + hexOf(n, 8);
const fakeUuid = (n) => `00000000-0000-4000-8000-${hexOf(n, 12)}`;
const sameShapeId = (n, real) =>
  /^[0-9a-f]{8}-[0-9a-f]{4}-/.test(real)
    ? fakeUuid(n)
    : fakeHex(n, real.length);

const KEY_NAMES = [
  "共享示例甲",
  "共享示例乙",
  "研究组共享 · 长名称示例（用于宽度检查）",
  "自用示例",
  "测试 Key（已删除示例）",
  "共享示例丙",
  "共享示例丁",
];
const keyName = (real) =>
  fake("keyName", real, (n) => KEY_NAMES[n - 1] ?? `共享示例 ${n}`);
const masked = (real) =>
  fake("masked", real, (n) => `um-share-****FX${String(n).padStart(2, "0")}`);
const pid = (real) => fake("pid", real, (n) => 4200 + n * 7);
const fakePath = (real) =>
  fake("path", real, (n) => `D:\\fixture\\share\\${real.split(/[\\/]/).pop()}`);

// ------------------------------------------------------------------ per-field rules
/** Field-specific replacement; returns undefined to fall through to the generic walk. */
function rule(ep, key, value) {
  if (typeof value === "string") {
    switch (key) {
      case "id":
        if (/operations|share-management\/keys/.test(ep))
          return fake("id", value, sameShapeId);
        break;
      case "key_id":
        return fake("id", value, sameShapeId);
      case "preview_token":
        return fake("token", value, (n) => fakeHex(n, value.length));
      case "instance_id":
        return fake("instance", value, (n) => fakeHex(n, value.length));
      case "code_stamp":
        return fake("stamp", value, (n) => fakeHex(n, value.length));
      case "request_id":
        return fake("rid", value, (n) => fakeHex(n, value.length));
      case "name":
        if (/share-management\/keys/.test(ep)) return keyName(value);
        break;
      case "keyName":
        return keyName(value);
      case "masked":
        return masked(value);
      case "share_url":
        return "https://share.example.invalid/";
      case "key_file":
      case "ledger_file":
      case "pid_file":
        return fakePath(value);
      case "file_key":
        return fake("filekey", value, (n) => `1000${n}:2000${n}:3000${n}`);
      // Rev18 客户端连接读面：明文调用 Key 与来源路径是私有值；
      // 来源是环境变量名（产品事实）时保留，是文件路径时换成合成路径。
      case "key_plaintext":
        return fake(
          "plaintext",
          value,
          () => "fixture-connection-key-not-real"
        );
      case "key_source":
        return /[\\/]/.test(value) ? fakePath(value) : value;
    }
  }
  if (typeof value === "number") {
    if (key === "gateway_pid" || key === "process_ids" || key === "pid")
      return pid(value);
    if (SCALED_ENDPOINTS.test(ep) && AMOUNT_KEY.test(key)) return scale(value);
  }
  return undefined;
}

/** Free text (reason / evidence / detail) may cite absolute local paths and real
 *  process ids; both are private, so they are replaced before the re-scan. */
const scrubText = (s) =>
  s
    .replace(/[A-Za-z]:\\[^\s"'；，。]+/g, "<local-path>")
    .replace(/\bpid=\d+/g, "pid=<pid>");

function walk(ep, value, key = "") {
  const r = rule(ep, key, value);
  if (r !== undefined) return r;
  if (Array.isArray(value)) return value.map((v) => walk(ep, v, key));
  if (value && typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) out[k] = walk(ep, v, k);
    return out;
  }
  return typeof value === "string" ? scrubText(value) : value;
}

// ------------------------------------------------------------------ log entries
const LOG_CAP = [65536, 131072, 384000];
function synthLogRows(rows) {
  const out = rows.map((row) => {
    const ev = row.event ?? {};
    const rid = ev.request_id
      ? fake("rid", ev.request_id, (n) => fakeHex(n, ev.request_id.length))
      : gen(fakeHex(randInt(1, 1e6), 12));
    const model = ev.model ?? "DeepSeek-V4.1-Flash";
    const d = ev.duration_ms ?? randInt(800, 40000);
    let message;
    if (/^stream end/.test(row.message))
      message = `stream end model=${model} status=ok data_lines=${randInt(40, 1200)} duration_ms=${d} upstream_request_id=${gen(fakeUuid(randInt(1, 1e6)))} upstream_server=fixture upstream_connection=None`;
    else if (/^request summary/.test(row.message))
      message = `request summary request=${rid} final=${ev.outcome ?? "ok"} physical_attempts=1/12 upstream_retries=0 slow_retries=0 repetition_retries=0 line_loop_hits=0 images_before=0 images_after=0 images_trimmed=0 duration_ms=${d}`;
    else if (/^output budget/.test(row.message)) {
      const cap = LOG_CAP[randInt(0, 2)];
      message = `output budget request=${rid} model=${model} level=max max_tokens_present=False max_tokens=- max_tokens_valid=False max_tokens_kind=absent max_completion_tokens_present=True max_completion_tokens=${cap} max_completion_tokens_valid=True max_completion_tokens_kind=ok output_cap_effective=${cap} cap_source=client_request protocol=chat`;
    } else message = `fixture event model=${model} duration_ms=${d}`;
    return {
      ...row,
      message,
      event: { ...ev, request_id: ev.request_id ? rid : ev.request_id },
    };
  });
  // Keep the non-happy row kinds covered even when the capture had none of them.
  // Event shapes follow console_log_reader.py: a parsed service line derives
  // request/model/duration/outcome from its key=value tokens (phase stays null,
  // a non-2xx final= gives outcome "error" + error_code "HTTPnnn"); an unparsable
  // line carries the eight event keys, all null.
  const ts = rows.at(-1)?.ts ?? "2026-10-06 08:00:00,000";
  const ev = (o) => ({
    timestamp: ts,
    model: null,
    request_id: null,
    phase: null,
    outcome: null,
    error_code: null,
    duration_ms: null,
    token_rate: null,
    token_rate_kind: null,
    ...o,
  });
  const slow = gen(fakeHex(900001, 12)),
    failed = gen(fakeHex(900002, 12));
  out.push(
    {
      ts,
      level: "WARNING",
      message: `upstream slow request=${slow} model=GLM-5.3-Flash first_byte_ms=18450 threshold_ms=15000 action=keep_waiting`,
      parse_ok: true,
      cut: false,
      event: ev({ request_id: slow, model: "GLM-5.3-Flash" }),
    },
    {
      ts,
      level: "ERROR",
      message: `request summary request=${failed} final=HTTP502 physical_attempts=3/12 upstream_retries=2 slow_retries=0 repetition_retries=0 line_loop_hits=0 images_before=0 images_after=0 images_trimmed=0 duration_ms=31200
Traceback (most recent call last):
  File "fixture_upstream.py", line 12, in relay
fixture_upstream.UpstreamError: HTTP 502 from upstream (synthetic)`,
      parse_ok: true,
      cut: false,
      event: ev({
        request_id: failed,
        outcome: "error",
        error_code: "HTTP502",
        duration_ms: 31200,
      }),
    },
    {
      ts,
      level: "INFO",
      message: `stream end model=GLM-5.3-Flash status=ok data_lines=4096 duration_ms=58000 note=${"long-line-fixture ".repeat(40).trim()}`,
      parse_ok: true,
      cut: true,
      event: ev({ model: "GLM-5.3-Flash", duration_ms: 58000 }),
    },
    {
      ts: null,
      level: null,
      message: "  [continued] fixture line without a parsable prefix",
      parse_ok: false,
      cut: false,
      event: {
        timestamp: null,
        model: null,
        request_id: null,
        phase: null,
        outcome: null,
        error_code: null,
        duration_ms: null,
        token_rate: null,
      },
    }
  );
  return out;
}

// ------------------------------------------------------------------ build
const out = {};
for (const [ep, entry] of Object.entries(capture)) {
  if (ep.startsWith("@")) continue; // non-usagesrv probes are never served by the fixture server
  let body = walk(ep, entry.body);
  if (ep.startsWith("/api/console/log-entries") && body?.rows) {
    body = { ...body, rows: synthLogRows(entry.body.rows) };
    if (body.next_cursor)
      body.next_cursor = gen(
        Buffer.from(
          JSON.stringify({
            src: "chat2api",
            cat: "service",
            off: body.window_start_offset ?? 0,
            key: body.file_key,
          })
        ).toString("base64")
      );
  }
  out[ep] = { status: entry.status, body };
}

const anchor =
  anchorArg ??
  (() => {
    const s =
      capture["/api/summary"]?.body?.generatedAt ??
      capture["/api/console/share-usage"]?.body?.checkedAt;
    return typeof s === "string" ? s.slice(0, 10) : null;
  })();
if (!anchor || !/^\d{4}-\d{2}-\d{2}$/.test(anchor)) {
  console.error("cannot determine the capture day; pass --anchor YYYY-MM-DD");
  process.exit(2);
}

// ------------------------------------------------------------------ privacy re-scan
// Values this script generated are excluded from the generic patterns (a synthetic
// 64-char preview token or base64 cursor looks exactly like a secret). The text is
// JSON, so each generated value is removed BOTH raw and JSON-escaped (a synthetic
// Windows path would otherwise match the absolute-path pattern).
let text = JSON.stringify(out);
for (const g of [...generated].sort((a, b) => b.length - a.length)) {
  text = text.split(g).join("");
  const escaped = JSON.stringify(g).slice(1, -1);
  if (escaped !== g) text = text.split(escaped).join("");
}
const leaks = [];
for (const real of replaced)
  if (text.includes(real))
    leaks.push(`replaced value still present: ${real.slice(0, 6)}…`);
const PATTERNS = {
  "windows user path": /[A-Za-z]:\\\\(Users|ProgramData)\\\\/,
  "unix home path": /\/(Users|home)\//,
  // Any absolute local path the per-field rules did not generate (the synthetic
  // paths are excluded via the `generated` set): a surviving real path means a
  // new field carried one that no rule covers yet.
  "absolute local path": /[A-Za-z]:\\\\/,
  "tailnet host": /\.ts\.net|tailscale/i,
  "e-mail": /[\w.+-]+@[\w-]+\.(com|net|org|cn|mo|edu)\b/,
  "private ipv4": /\b(10|172\.(1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}/,
  // The running account's own name, read at run time (never written into this file).
  ...(() => {
    let name = "";
    try {
      name = userInfo().username;
    } catch {}
    return name.length >= 3
      ? {
          "account name": new RegExp(
            name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
            "i"
          ),
        }
      : {};
  })(),
  "long secret-like token":
    /(?<![\w/.-])(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{40,}(?![\w/.-])/,
  "upstream request id (uuid)":
    /\b(?!00000000-0000-4000-8000-)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/,
};
for (const [name, re] of Object.entries(PATTERNS)) {
  const m = text.match(re);
  if (m) leaks.push(`${name}: …${m[0].slice(0, 12)}…`);
}
if (leaks.length) {
  console.error("refusing to write api.json:\n" + leaks.join("\n"));
  process.exit(1);
}

const result = {
  _anchor: anchor,
  _note:
    "Synthetic console fixtures generated by sanitize.mjs from a local read-only capture; dates are shifted to today by shift.mjs at load. Do not hand-edit.",
  ...out,
};
writeFileSync(
  resolve(here, "api.json"),
  JSON.stringify(result, null, 1) + "\n"
);
console.log(
  JSON.stringify({
    anchor,
    endpoints: Object.keys(out).length,
    replacedValues: replaced.size,
    bytes: Buffer.byteLength(JSON.stringify(result)),
  })
);
