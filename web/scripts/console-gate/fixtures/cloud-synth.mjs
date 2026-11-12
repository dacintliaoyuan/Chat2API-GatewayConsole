// Deterministic synthetic fixtures for the console's CLOUD role (UM_SHARE_ROLE=cloud).
//
// Every body is what the cloud backend code path answers for a synthetic deployment;
// nothing is captured from a real host. Bodies and the code they follow:
//   /api/summary                      usagesrv.summary_cached -> _build_cloud_summary (+ load_price_table)
//   /api/console/health               usagesrv._console_health_payload cloud branch (+ console_adapters.cloud_service_status)
//   /api/console/services             usagesrv._console_api "services" -> console_services_snapshot (cloud)
//   /api/console/actions              console_operations.action_info over console_adapters.build_cloud_registry
//   /api/console/operations?limit=8   console_operations.OperationManager._view rows
//   /api/console/prices               usagesrv._console_api "prices" (prices.json passthrough)
//   /api/console/share-management/*   usagesrv._console_share_api (status / keys / public / control-status)
//   /api/console/share-usage          usagesrv._share_usage_response -> share_metering.read_share_usage
//   cloud-blocked reads               usagesrv._console_api cloud gate -> 503 capability_not_assembled
// The share-usage and summary numbers are COMPUTED by small ports of the backend pricer
// (usagesrv) and aggregation (share_metering) over the synthetic ledger and price table
// below, so totals, coverage counters and price versions stay internally consistent.
// The pricer port matches exact model ids only (match_model_id is not ported): every
// ledger model must be a key of the price table, which cloudFixtures() enforces.
//
// Data rules: synthetic names only, no key plaintext, no token-like strings of 24+
// characters, fixed epochs around ANCHOR (fixtures/shift.mjs moves them to today).
//
// CLI: node cloud-synth.mjs [--variant normal|um-login-needed|um-backend-down] [--out <file>]
import { writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const ANCHOR = "2026-10-31"; // Asia/Shanghai day of NOW (a Saturday)
const NOW = Date.UTC(2026, 9, 31, 2, 20, 0) / 1000; // 2026-10-31 10:20:00 +08:00
export const VARIANTS = Object.freeze([
  "normal",
  "um-login-needed",
  "um-backend-down",
]);

// ------------------------------------------------------------------ deployment facts
const CONSOLE_PORT = 18096; // usagesrv.CLOUD_DEFAULT_PORT
const GATEWAY_PORT = 8100; // console_adapters.DEFAULT_CLOUD_GATEWAY_PORT
const GATEWAY_PID = 21877; // share-gateway after the 08:52 service.restart below
const BACKEND_DIR = "/srv/fixture/um-share/share-backend"; // CONSOLE_SHARE_BACKEND_DIR (required on Linux)
const LEDGER_NAME = "usage-share-backend.jsonl";
// share_attribution_key_path() with neither CONSOLE_SHARE_ATTRIBUTION_KEY_FILE nor
// CONSOLE_SHARE_RUN_ROOT set (as in tests/linux_share_chain.py): the Windows RunRoot
// default joined with POSIX separators, a relative path absent on Linux -> "missing".
const ATTRIBUTION_KEY_FILE =
  "C:\\ProgramData\\UMShareRun/run/share-attribution.key";
const STORE_GENERATION = 6; // fresh store 1; +3 creates, alice 8->4, carol disabled

// ------------------------------------------------------------------ time helpers
const pad2 = (n) => String(n).padStart(2, "0");
/** isoformat(timespec="seconds") of epoch seconds at a whole-hour UTC offset. */
function isoSeconds(sec, offsetHours) {
  const d = new Date(Math.floor(sec) * 1000 + offsetHours * 3600_000);
  return (
    `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}T${pad2(d.getUTCHours())}` +
    `:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}+${pad2(offsetHours)}:00`
  );
}
/** Epoch seconds of a Beijing wall-clock time. */
const bj = (day, hhmm, plusSec = 0) => {
  const [y, m, d] = day.split("-").map(Number),
    [hh, mm] = hhmm.split(":").map(Number);
  return Date.UTC(y, m - 1, d, hh - 8, mm) / 1000 + plusSec;
};
/** share_store._now(): "%Y-%m-%dT%H:%M:%S.%fZ". */
const storeStamp = (sec, micros) =>
  `${isoSeconds(sec, 0).slice(0, 19)}.${String(micros).padStart(6, "0")}Z`;
const ymdUtc = (ms) => new Date(ms).toISOString().slice(0, 10);

// ------------------------------------------------------------------ share keys
// share_store rows through Handler._console_share_public_row. Ids use the repo's synthetic
// uuid convention; masked = share_store._mask ("um-share-****" + last 4 chars of the secret).
// POSIX stores every new secret with the AES-GCM file key, so plaintext_viewable is true.
const KEYS = [
  {
    who: "alice",
    id: "00000000-0000-4000-8000-0000000000a1",
    name: "alice-laptop",
    masked: "um-share-****Qm7x",
    enabled: true,
    max: 4,
    rowVersion: 2,
    created: storeStamp(bj("2026-10-22", "10:14", 7), 381522),
  },
  {
    who: "bob",
    id: "00000000-0000-4000-8000-0000000000b2",
    name: "bob-cursor",
    masked: "um-share-****h2Kd",
    enabled: true,
    max: 2,
    rowVersion: 1,
    created: storeStamp(bj("2026-10-22", "10:16", 51), 904113),
  },
  {
    who: "carol",
    id: "00000000-0000-4000-8000-0000000000c3",
    name: "carol-test",
    masked: "um-share-****Zp_4",
    enabled: false,
    max: 2,
    rowVersion: 2,
    created: storeStamp(bj("2026-10-24", "15:42", 19), 226480),
  },
];
const KEY_ID = Object.fromEntries(KEYS.map((k) => [k.who, k.id]));

// ------------------------------------------------------------------ price table (data/prices.json)
const PEAK = {
  enabled: true,
  effectiveAt: "2026-08-01T00:00:00Z",
  windows: [
    { start: 1, end: 4 },
    { start: 6, end: 10 },
  ],
};
const deepseek = (miss, out, peakMiss, peakOut) => ({
  cacheHit: 0.007,
  cacheMiss: miss,
  output: out,
  offPeak: { cacheHit: 0.007, cacheMiss: miss, output: out },
  peak: { cacheHit: 0.014, cacheMiss: peakMiss, output: peakOut },
});
const providers = () => ({
  "z-ai": {
    models: {
      "glm-5.3-flash": {
        input: 0.15,
        cachedInput: 0.03,
        output: 0.5,
        billingMode: "flat",
      },
    },
  },
  alibaba: {
    models: {
      "qwen3.5-397b-a17b": {
        input: 0.6,
        cachedInput: 0.06,
        output: 3.6,
        billingMode: "flat",
      },
    },
  },
  google: {
    models: {
      "gemma-4-31b-it": {
        unpriced: true,
        billingMode: "flat",
        notes: "目录未公布单价：无价不标价（夹具示例）",
      },
    },
  },
});
const V1 = {
  versionId: "1970-01-01",
  effectiveAt: "1970-01-01T00:00:00Z",
  checkedAt: "2026-10-08",
  exchangeRate: 7.2,
  peak: PEAK,
  models: { "deepseek-v4-flash": deepseek(0.22, 0.66, 0.44, 1.32) },
  providers: providers(),
};
const V2 = {
  versionId: "2026-10-27T10:00:00+08:00",
  effectiveAt: "2026-10-27T10:00:00+08:00",
  checkedAt: "2026-10-27",
  exchangeRate: 7.1,
  peak: PEAK,
  models: { "deepseek-v4-flash": deepseek(0.25, 0.75, 0.5, 1.5) },
  providers: providers(),
};
const PRICE_FILE = {
  checkedAt: V2.checkedAt,
  exchangeRate: V2.exchangeRate,
  peak: PEAK,
  models: V2.models,
  providers: V2.providers,
  versions: [V1, V2],
};

// ------------------------------------------------------------------ share ledger (8110)
// One row = `count` records (47 s apart) as chat2api.record_usage writes them.
// [Beijing day, "HH:MM", key (null = unattributed), model, count, input, output, cached, flags]
// flags: src (usage_source, default upstream), status (default ok),
//        cache (cache_report_status, default reported), attr (attribution, default verified).
const LEDGER = [
  ["2026-10-22", "14:30", "bob", "glm-5.3-flash", 7, 3000, 900, 1024],
  ["2026-10-23", "10:15", "alice", "deepseek-v4-flash", 8, 5200, 640, 2048],
  ["2026-10-23", "20:40", "alice", "glm-5.3-flash", 3, 1800, 420, 0],
  ["2026-10-24", "16:05", "bob", "glm-5.3-flash", 4, 2600, 700, 0],
  ["2026-10-25", "21:05", "alice", "deepseek-v4-flash", 5, 4100, 512, 1024],
  [
    "2026-10-26",
    "11:30",
    null,
    "glm-5.3-flash",
    2,
    900,
    120,
    0,
    { attr: "absent" },
  ],
  ["2026-10-27", "15:30", "alice", "deepseek-v4-flash", 12, 6400, 880, 3072],
  ["2026-10-28", "10:40", "bob", "gemma-4-31b-it", 2, 2100, 380, 0],
  ["2026-10-28", "13:15", "bob", "glm-5.3-flash", 8, 3300, 820, 2048],
  ["2026-10-29", "09:20", "alice", "deepseek-v4-flash", 10, 5600, 700, 2048],
  // An image-only prompt cancelled before any usage: no estimate either -> usage unknown.
  [
    "2026-10-29",
    "11:02",
    "alice",
    "deepseek-v4-flash",
    1,
    0,
    0,
    0,
    { src: "none", status: "client_disconnected", cache: "unreported" },
  ],
  [
    "2026-10-29",
    "16:20",
    null,
    "deepseek-v4-flash",
    1,
    1500,
    0,
    0,
    {
      attr: "no_key",
      src: "estimated",
      status: "client_disconnected",
      cache: "unreported",
    },
  ],
  ["2026-10-30", "17:50", "bob", "glm-5.3-flash", 6, 2900, 760, 1024],
  ["2026-10-30", "19:45", "alice", "glm-5.3-flash", 6, 2300, 510, 512],
  [
    "2026-10-30",
    "22:10",
    "alice",
    "deepseek-v4-flash",
    2,
    3900,
    450,
    0,
    { cache: "unreported" },
  ],
  [
    "2026-10-31",
    "07:35",
    "bob",
    "glm-5.3-flash",
    1,
    4200,
    0,
    0,
    { src: "estimated", status: "client_disconnected", cache: "unreported" },
  ],
  ["2026-10-31", "08:10", "alice", "deepseek-v4-flash", 9, 4800, 600, 2048],
  ["2026-10-31", "09:05", "bob", "glm-5.3-flash", 5, 2700, 640, 512],
];
// 8110 in needs_login: the auth wait ends in ReauthenticationRequired and chat2api.py
// records (model, input_est, 0, status="reauthentication_required") -> estimated rows.
const LOGIN_NEEDED_LEDGER = [
  [
    "2026-10-31",
    "09:52",
    "alice",
    "deepseek-v4-flash",
    2,
    3600,
    0,
    0,
    {
      src: "estimated",
      status: "reauthentication_required",
      cache: "unreported",
    },
  ],
  [
    "2026-10-31",
    "10:08",
    "bob",
    "glm-5.3-flash",
    1,
    2400,
    0,
    0,
    {
      src: "estimated",
      status: "reauthentication_required",
      cache: "unreported",
    },
  ],
];

function ledgerRecords(variant) {
  const rows =
    variant === "um-login-needed"
      ? [...LEDGER, ...LOGIN_NEEDED_LEDGER]
      : LEDGER;
  const out = [];
  for (const [
    day,
    hhmm,
    who,
    model,
    count,
    input,
    output,
    cached,
    flags = {},
  ] of rows) {
    const source = flags.src ?? "upstream";
    for (let i = 0; i < count; i++) {
      out.push({
        time: isoSeconds(bj(day, hhmm, i * 47), 8),
        service: "chat2api",
        model,
        stream: true,
        status: flags.status ?? "ok",
        input_tokens: input,
        output_tokens: output,
        cached_input_tokens: cached,
        cache_report_status: flags.cache ?? "reported",
        reasoning_tokens: 0,
        total_tokens: input + output,
        estimated: source === "estimated" || source === "none",
        usage_source: source,
        protocol: "chat",
        key_id: who ? KEY_ID[who] : null,
        attribution: flags.attr ?? "verified",
      });
    }
  }
  return out.sort((a, b) => Date.parse(a.time) - Date.parse(b.time)); // append order = time order
}

/** The synthetic inputs behind share-usage and summary, for cross-checking these bodies
 *  against the real share_metering / usagesrv modules. */
export function cloudInputs({ variant = "normal" } = {}) {
  return {
    now: NOW,
    prices: structuredClone(PRICE_FILE),
    ledger: ledgerRecords(variant),
    ledgerName: LEDGER_NAME,
  };
}

// ------------------------------------------------------------------ pricer port (usagesrv)
const LEGACY_BASE_BOUNDARY = Date.parse("2026-08-16T16:00:00Z");
const WEEKEND_OFFPEAK_AT = Date.parse("2026-08-22T16:00:00Z");
const PRICE_STALE_DAYS = 30;
const parseTs = (text) => {
  const ms = text ? Date.parse(String(text).trim()) : NaN;
  return Number.isNaN(ms) ? null : ms;
};
const rateOf = (raw) => (typeof raw === "number" && raw > 0 ? raw : 7.2);
const peakOf = (raw) => ({
  enabled: raw?.enabled === true,
  effectiveAtMs: parseTs(raw?.effectiveAt),
  windows: raw?.windows ?? [],
});
const tiersOf = (models, providerTable) => [
  ...(models && Object.keys(models).length ? [models] : []),
  ...Object.values(providerTable ?? {})
    .filter((t) => t?.models && Object.keys(t.models).length)
    .map((t) => t.models),
];
const checkedAgeDays = (checkedAt, nowSec) => {
  const ms = typeof checkedAt === "string" ? parseTs(checkedAt) : null;
  return ms === null
    ? null
    : Math.max(0, Math.floor((nowSec * 1000 - ms) / 86_400_000));
};
const stale = (age) => (age === null ? null : age > PRICE_STALE_DAYS);

function loadPriceTable(prices, nowSec) {
  const versions = prices.versions
    .map((raw) => ({
      versionId: raw.versionId ?? raw.effectiveAt,
      effectiveAt: raw.effectiveAt,
      effectiveAtMs: parseTs(raw.effectiveAt),
      exchangeRate: rateOf(raw.exchangeRate),
      peak: peakOf(raw.peak),
      tiers: tiersOf(raw.models, raw.providers),
      checkedAt: raw.checkedAt ?? null,
      checkedAgeDays: checkedAgeDays(raw.checkedAt, nowSec),
    }))
    .sort((a, b) => a.effectiveAtMs - b.effectiveAtMs);
  const current = versionAt(versions, nowSec * 1000) ?? versions[0];
  const age = checkedAgeDays(prices.checkedAt, nowSec);
  return {
    exchangeRate: current.exchangeRate,
    peak: current.peak,
    versions,
    current,
    meta: {
      pricesFound: current.tiers.length > 0,
      checkedAt: prices.checkedAt ?? null,
      checkedAgeDays: age,
      checkedStale: stale(age),
      overrideFound: false,
    },
  };
}
function versionAt(versions, atMs) {
  if (atMs === null) return null;
  let hit = null;
  for (const v of versions)
    if (
      v.effectiveAtMs !== null &&
      atMs >= v.effectiveAtMs &&
      (!hit || v.effectiveAtMs > hit.effectiveAtMs)
    )
      hit = v;
  return hit;
}
function completeTier(raw) {
  if (!raw || typeof raw !== "object") return null;
  const num = (k) =>
    typeof raw[k] === "number" && raw[k] >= 0 ? raw[k] : null;
  const miss = num("cacheMiss") ?? num("input");
  const hit = num("cacheHit") ?? num("cachedInput") ?? num("cacheRead") ?? miss;
  return {
    cacheHit: hit || 0,
    cacheMiss: miss || 0,
    output: num("output") || 0,
    reasoning: num("reasoning"),
    unpriced: raw.unpriced === true,
  };
}
function normalizePrice(raw) {
  const entry = completeTier(raw);
  if (entry)
    for (const k of ["offPeak", "peak", "legacyBase"]) {
      const sub = completeTier(raw[k]);
      if (sub) entry[k] = sub;
    }
  return entry;
}
const canon = (id) =>
  String(id)
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
/** resolve_price restricted to exact ids: (entry, matched) or (null, null) incl. the unpriced veto. */
function resolvePrice(version, model) {
  let best = null,
    unpricedScore = null;
  for (const models of version.tiers) {
    if (!Object.hasOwn(models, model)) continue;
    const entry = normalizePrice(models[model]);
    if (!entry) continue;
    const score = 1000 + canon(model).length;
    if (entry.unpriced) {
      if (unpricedScore === null || score > unpricedScore)
        unpricedScore = score;
      continue;
    }
    if (!best || score > best.score) best = { score, entry };
  }
  return !best || (unpricedScore !== null && unpricedScore >= best.score)
    ? [null, null]
    : [best.entry, model];
}
function isWeekend(atMs) {
  if (atMs < WEEKEND_OFFPEAK_AT) return false;
  const weekday = (Math.floor((atMs + 8 * 3600_000) / 86_400_000) + 4) % 7;
  return weekday === 6 || weekday === 0;
}
function isPeakHour(atMs, effMs, windows) {
  if (
    atMs === null ||
    !windows.length ||
    isWeekend(atMs) ||
    (effMs !== null && atMs < effMs)
  )
    return false;
  const hour = new Date(atMs).getUTCHours();
  return windows.some(({ start, end }) =>
    start < end ? start <= hour && hour < end : hour >= start || hour < end
  );
}
function tierFor(entry, atMs, peakCfg) {
  const asTier = (p) => ({
    cacheHit: p.cacheHit || 0,
    cacheMiss: p.cacheMiss || 0,
    output: p.output || 0,
    reasoning: p.reasoning ?? null,
  });
  if (atMs !== null && atMs < LEGACY_BASE_BOUNDARY)
    return asTier(entry.legacyBase ?? entry);
  if (!peakCfg.enabled) return asTier(entry);
  if (isPeakHour(atMs, peakCfg.effectiveAtMs, peakCfg.windows))
    return asTier(entry.peak ?? entry);
  if (
    atMs !== null &&
    peakCfg.effectiveAtMs !== null &&
    atMs >= peakCfg.effectiveAtMs &&
    entry.offPeak
  )
    return asTier(entry.offPeak);
  return asTier(entry);
}
/** estimate_record_with_version_and_rate: (cost_usd, priced, matched, peak, version_id, rate). */
function estimateRecord(
  table,
  model,
  atMs,
  input,
  output,
  cacheTok,
  reasoning
) {
  const version = versionAt(table.versions, atMs);
  if (!version) return [0, false, null, false, null, null];
  const [entry, matched] = resolvePrice(version, model);
  if (!entry) return [0, false, null, false, version.versionId, null];
  const cache = Math.max(0, cacheTok || 0);
  const tier = tierFor(entry, atMs, version.peak);
  const cost =
    (Math.max(0, (input || 0) - cache) * tier.cacheMiss +
      (output || 0) * tier.output +
      cache * tier.cacheHit +
      (reasoning || 0) * (tier.reasoning || 0)) /
    1_000_000;
  return [
    Number.isFinite(cost) && cost > 0 ? cost : 0,
    true,
    matched,
    isPeakHour(atMs, version.peak.effectiveAtMs, version.peak.windows),
    version.versionId,
    version.exchangeRate,
  ];
}

// ------------------------------------------------------------------ share_metering port
const USAGE_SOURCES = ["upstream", "partial", "estimated", "none"];
const round = (x, n) => Number(x.toFixed(n));
const count = (rec, ...names) => {
  for (const name of names) {
    const v = rec[name];
    if (typeof v === "number" && Number.isFinite(v) && v >= 0)
      return Math.trunc(v);
  }
  return null;
};
const trusted = (v) => (Number.isInteger(v) && v >= 0 ? v : null);
const validRate = (r) => typeof r === "number" && Number.isFinite(r) && r > 0;
const cacheStatus = (rec) =>
  ["reported", "unreported"].includes(rec.cache_report_status)
    ? rec.cache_report_status
    : "unknown";
function sourceOf(rec) {
  let source;
  if (USAGE_SOURCES.includes(rec.usage_source)) source = rec.usage_source;
  else if (typeof rec.estimated === "boolean")
    source = rec.estimated ? "estimated" : "upstream";
  else return "none";
  if (source === "none") return source;
  return count(rec, "input_tokens", "output_tokens", "total_tokens") === null
    ? "none"
    : source;
}
const cnySum = () => ({ total: 0, converted: 0, partial: false });
const cnyResult = (s) => (!s.converted && s.partial ? null : round(s.total, 4));
const emptySummary = () => ({
  requests: 0,
  costUsd: 0,
  knownCostUsd: 0,
  estimatedCostUsd: 0,
  unknownRequests: 0,
  unpricedRequests: 0,
  inputTokens: 0,
  outputTokens: 0,
  coverage: {
    records: 0,
    pricedRequests: 0,
    unknownTimeRequests: 0,
    pricingUnavailableRequests: 0,
    cacheReportedRequests: 0,
    cacheUnreportedRequests: 0,
    cacheUnknownRequests: 0,
    cacheUnknownPricedRequests: 0,
    costCompleteRecords: 0,
  },
  priceVersions: new Set(),
  cny: { total: cnySum(), known: cnySum(), estimated: cnySum() },
  tokenUsage: {
    completeRequests: 0,
    lowerBoundRequests: 0,
    estimatedRequests: 0,
    unknownRequests: 0,
    inputTokens: 0,
    outputTokens: 0,
    lowerBoundInputTokens: 0,
    lowerBoundOutputTokens: 0,
    estimatedInputTokens: 0,
    estimatedOutputTokens: 0,
    lowerBoundInputKnown: 0,
    lowerBoundOutputKnown: 0,
    estimatedInputKnown: 0,
    estimatedOutputKnown: 0,
    cachedInputTokens: 0,
    cacheReportedRequests: 0,
    cacheUnreportedRequests: 0,
    cacheUnknownRequests: 0,
  },
});
const emptyKey = (keyId) => ({
  ...emptySummary(),
  key_id: keyId,
  models: new Map(),
  days: new Map(),
  undated: { day: null, ...emptySummary() },
});
const isOk = (rec) => String(rec.status || "ok") === "ok";

function accumulateTokenUsage(u, rec, source) {
  const cs = cacheStatus(rec);
  if (cs === "reported") {
    const cached = trusted(rec.cached_input_tokens ?? rec.cache_tokens);
    if (cached === null) u.cacheUnknownRequests++;
    else {
      u.cacheReportedRequests++;
      u.cachedInputTokens += cached;
    }
  } else if (cs === "unreported") u.cacheUnreportedRequests++;
  else u.cacheUnknownRequests++;
  const input = trusted(rec.input_tokens),
    output = trusted(rec.output_tokens);
  if (source === "none" || (input === null && output === null)) {
    u.unknownRequests++;
    return;
  }
  if (source === "upstream" && input !== null && output !== null && isOk(rec)) {
    u.completeRequests++;
    u.inputTokens += input;
    u.outputTokens += output;
    return;
  }
  const prefix = source === "estimated" ? "estimated" : "lowerBound";
  u[`${prefix}Requests`]++;
  for (const [side, value] of [
    ["Input", input],
    ["Output", output],
  ]) {
    if (value !== null) {
      u[`${prefix}${side}Tokens`] += value;
      u[`${prefix}${side}Known`]++;
    }
  }
}
function addSummary(
  entry,
  rec,
  source,
  atMs,
  cost,
  priced,
  versionId,
  rate,
  includeUnknownRequest,
  cacheUnknown
) {
  if (source !== "none" || includeUnknownRequest) entry.requests++;
  const cov = entry.coverage,
    cs = cacheStatus(rec);
  cov.records++;
  cov.unknownTimeRequests += Number(atMs === null);
  cov.cacheReportedRequests += Number(cs === "reported");
  cov.cacheUnreportedRequests += Number(cs === "unreported");
  cov.cacheUnknownRequests += Number(cs === "unknown");
  if (cacheUnknown) cov.cacheUnknownPricedRequests++;
  accumulateTokenUsage(entry.tokenUsage, rec, source);
  const cny = entry.cny;
  if (source === "none") cny.total.partial = true;
  else if (priced && validRate(rate)) {
    cny.total.total += cost * rate;
    cny.total.converted++;
    const side = source === "upstream" ? cny.known : cny.estimated;
    side.total += cost * rate;
    side.converted++;
  } else {
    cny.total.partial = true;
    (source === "upstream" ? cny.known : cny.estimated).partial = true;
  }
  if (cacheUnknown)
    cny.total.partial = cny.known.partial = cny.estimated.partial = true;
  if (source === "none") {
    entry.unknownRequests++;
    return;
  }
  if (!priced) entry.unpricedRequests++;
  if (versionId !== null) entry.priceVersions.add(String(versionId));
  const input = count(rec, "input_tokens"),
    output = count(rec, "output_tokens");
  if (input !== null) entry.inputTokens += input;
  if (output !== null) entry.outputTokens += output;
  if (priced) {
    cov.pricedRequests++;
    entry.costUsd += cost;
    if (source === "upstream") entry.knownCostUsd += cost;
    else entry.estimatedCostUsd += cost;
    if (
      atMs !== null &&
      ["upstream", "estimated"].includes(source) &&
      isOk(rec) &&
      trusted(rec.input_tokens) !== null &&
      trusted(rec.output_tokens) !== null &&
      !cacheUnknown
    )
      cov.costCompleteRecords++;
  }
}
function finishTokenUsage(u, records) {
  const out = {
    completeRequests: u.completeRequests,
    lowerBoundRequests: u.lowerBoundRequests,
    estimatedRequests: u.estimatedRequests,
    unknownRequests: u.unknownRequests,
    inputTokens: u.completeRequests ? u.inputTokens : null,
    outputTokens: u.completeRequests ? u.outputTokens : null,
    totalTokens: u.completeRequests ? u.inputTokens + u.outputTokens : null,
  };
  for (const prefix of ["lowerBound", "estimated"]) {
    for (const side of ["Input", "Output"])
      out[`${prefix}${side}Tokens`] = u[`${prefix}${side}Known`]
        ? u[`${prefix}${side}Tokens`]
        : null;
    out[`${prefix}Tokens`] =
      u[`${prefix}InputKnown`] || u[`${prefix}OutputKnown`]
        ? u[`${prefix}InputTokens`] + u[`${prefix}OutputTokens`]
        : null;
  }
  out.complete = records > 0 && u.completeRequests === records;
  out.cachedInputTokens = u.cacheReportedRequests ? u.cachedInputTokens : null;
  out.cacheReportedRequests = u.cacheReportedRequests;
  out.cacheUnreportedRequests = u.cacheUnreportedRequests;
  out.cacheUnknownRequests = u.cacheUnknownRequests;
  return out;
}
function finishSummary(e) {
  const { costCompleteRecords, ...coverage } = e.coverage;
  coverage.costComplete =
    coverage.records > 0 && costCompleteRecords === coverage.records;
  const tokenUsage = finishTokenUsage(e.tokenUsage, coverage.records);
  Object.assign(coverage, {
    completeUsageRequests: tokenUsage.completeRequests,
    partialUsageRequests: tokenUsage.lowerBoundRequests,
    estimatedUsageRequests: tokenUsage.estimatedRequests,
    unknownUsageRequests: tokenUsage.unknownRequests,
  });
  return {
    requests: e.requests,
    inputTokens: e.inputTokens,
    outputTokens: e.outputTokens,
    unknownRequests: e.unknownRequests,
    unpricedRequests: e.unpricedRequests,
    costUsd: round(e.costUsd, 6),
    knownCostUsd: round(e.knownCostUsd, 6),
    estimatedCostUsd: round(e.estimatedCostUsd, 6),
    costCny: cnyResult(e.cny.total),
    knownCostCny: cnyResult(e.cny.known),
    estimatedCostCny: cnyResult(e.cny.estimated),
    costCnyPartial: e.cny.total.partial,
    knownCostCnyPartial: e.cny.known.partial,
    estimatedCostCnyPartial: e.cny.estimated.partial,
    coverage,
    tokenUsage,
    priceVersions: [...e.priceVersions].sort(),
  };
}
function finishKey(b) {
  const models = [...b.models.values()].sort(
    (x, y) =>
      y.costUsd - x.costUsd ||
      (x.model < y.model ? -1 : x.model > y.model ? 1 : 0)
  );
  return {
    key_id: b.key_id,
    ...finishSummary(b),
    models: models.map((m) => ({ model: m.model, ...finishSummary(m) })),
    days: [...b.days.keys()]
      .sort()
      .map((day) => ({ day, ...finishSummary(b.days.get(day)) })),
    undated: b.undated.coverage.records
      ? { day: null, ...finishSummary(b.undated) }
      : null,
  };
}
const beijingDay = (ms) =>
  ms === null ? null : new Date(ms + 8 * 3600_000).toISOString().slice(0, 10);

/** share_metering.read_share_usage(ledger, estimate_record_with_version_and_rate, limit=200000). */
function readShareUsage(records, table, nowSec, limit = 200000) {
  const buckets = new Map();
  const c = {
    attributed: 0,
    unattributed: 0,
    incompleteRequests: 0,
    unknownUsageRequests: 0,
    unknownTimeRequests: 0,
    unpricedRequests: 0,
    peakRequests: 0,
    unpricedModels: new Set(),
    priceVersions: new Set(),
    attributionReasons: {},
    costCnyPartial: false,
    rateFn: false,
    cacheReportedRequests: 0,
    cacheUnreportedRequests: 0,
    cacheUnknownRequests: 0,
    cacheUnknownPricedRequests: 0,
  };
  for (const rec of records) {
    const keyId =
      typeof rec.key_id === "string" && rec.key_id ? rec.key_id : null;
    c[keyId === null ? "unattributed" : "attributed"]++;
    if (!buckets.has(keyId)) buckets.set(keyId, emptyKey(keyId));
    const bucket = buckets.get(keyId);
    if (keyId === null) {
      const reason =
        typeof rec.attribution === "string" && rec.attribution
          ? rec.attribution
          : "attribution" in rec
            ? "absent"
            : "legacy";
      c.attributionReasons[reason] = (c.attributionReasons[reason] ?? 0) + 1;
    }
    if (!isOk(rec)) c.incompleteRequests++;
    const model = String(rec.model || "unknown");
    const atMs = parseTs(rec.time);
    if (atMs === null) c.unknownTimeRequests++;
    const source = sourceOf(rec),
      cs = cacheStatus(rec);
    c[
      cs === "reported"
        ? "cacheReportedRequests"
        : cs === "unreported"
          ? "cacheUnreportedRequests"
          : "cacheUnknownRequests"
    ]++;
    let cost = 0,
      priced = false,
      matched = null,
      peak = false,
      versionId = null,
      rate = null,
      cacheUnknown = false;
    if (source === "none") c.unknownUsageRequests++;
    else {
      const cacheTok =
        cs === "unreported"
          ? null
          : count(rec, "cached_input_tokens", "cache_tokens");
      cacheUnknown = cs === "unreported";
      [cost, priced, matched, peak, versionId, rate] = estimateRecord(
        table,
        model,
        atMs,
        count(rec, "input_tokens") || 0,
        count(rec, "output_tokens") || 0,
        cacheTok,
        count(rec, "reasoning_tokens") || 0
      );
      if (!validRate(rate)) rate = null;
      c.rateFn = true; // the pricer returns six-tuples
    }
    if (cacheUnknown) c.cacheUnknownPricedRequests++;
    if (!(priced && validRate(rate))) c.costCnyPartial = true;
    if (source !== "none") {
      if (!priced) {
        c.unpricedRequests++;
        c.unpricedModels.add(matched || model);
      }
      if (versionId !== null) c.priceVersions.add(String(versionId));
    }
    if (peak) c.peakRequests++;
    const group = matched ? String(matched) : model;
    if (!bucket.models.has(group))
      bucket.models.set(group, { model: group, ...emptySummary() });
    const day = beijingDay(atMs);
    if (day && !bucket.days.has(day))
      bucket.days.set(day, { day, ...emptySummary() });
    const dayEntry = day ? bucket.days.get(day) : bucket.undated;
    addSummary(
      bucket,
      rec,
      source,
      atMs,
      cost,
      priced,
      versionId,
      rate,
      true,
      cacheUnknown
    );
    for (const entry of [bucket.models.get(group), dayEntry])
      addSummary(
        entry,
        rec,
        source,
        atMs,
        cost,
        priced,
        versionId,
        rate,
        false,
        cacheUnknown
      );
  }
  const keys = [...buckets.values()].sort(
    (a, b) =>
      b.requests - a.requests || ((a.key_id ?? "") < (b.key_id ?? "") ? -1 : 1)
  );
  const total = records.length;
  return {
    requests: total,
    keys: keys.filter((k) => k.key_id !== null).map(finishKey),
    unattributed: finishKey(buckets.get(null) ?? emptyKey(null)),
    checkedAt: isoSeconds(nowSec, 0),
    coverage: {
      ledgerPresent: true,
      ledgerReadable: true,
      recordsTotal: total,
      unparsedLines: 0,
      truncated: total > limit,
      ledger: LEDGER_NAME,
      limit,
      records: total,
      attributed: c.attributed,
      unattributed: c.unattributed,
      incompleteRequests: c.incompleteRequests,
      unknownUsageRequests: c.unknownUsageRequests,
      unknownTimeRequests: c.unknownTimeRequests,
      unpricedRequests: c.unpricedRequests,
      peakRequests: c.peakRequests,
      unpricedModels: [...c.unpricedModels].sort(),
      priceVersions: [...c.priceVersions].sort(),
      priceFn: true,
      attributionReasons: Object.fromEntries(
        Object.entries(c.attributionReasons).sort(([a], [b]) =>
          a < b ? -1 : 1
        )
      ),
      cacheReportedRequests: c.cacheReportedRequests,
      cacheUnreportedRequests: c.cacheUnreportedRequests,
      cacheUnknownRequests: c.cacheUnknownRequests,
      cacheUnknownPricedRequests: c.cacheUnknownPricedRequests,
      costCnyPartial: c.costCnyPartial,
      rateFn: c.rateFn,
    },
  };
}

/** Handler._share_usage_project_key_names: whitelisted name / deletion state per key. */
function shareUsageBody(variant, table) {
  const payload = readShareUsage(ledgerRecords(variant), table, NOW);
  const byId = new Map(KEYS.map((k) => [k.id, k]));
  for (const entry of payload.keys) {
    const row = byId.get(entry.key_id);
    entry.keyName = row ? row.name : null;
    entry.deleted = row ? false : null;
  }
  payload.coverage.keyNames = payload.keys.filter((k) =>
    byId.has(k.key_id)
  ).length;
  return payload;
}

// ------------------------------------------------------------------ summary / prices
function summaryBody(table) {
  const builtAt = NOW - 152.6; // summary_cached: background build, then served from cache
  const v = (t) => ({
    versionId: t.versionId,
    effectiveAt: t.effectiveAt,
    exchangeRate: t.exchangeRate,
    checkedAt: t.checkedAt,
    checkedStale: stale(t.checkedAgeDays),
  });
  return {
    generatedAt: isoSeconds(builtAt - 0.04, 8),
    exchangeRate: table.exchangeRate,
    currency: "CNY",
    role: "cloud",
    assembly: {
      role: "cloud",
      services: ["share"],
      excluded: ["qoder", "chat2api", "m365", "dsh"],
      note: "云端角色只装配 UM 分享：不读取本地私人账本、不探测 Qoder/CLIProxy/model_sync；分享用量见 GET /api/console/share-usage",
    },
    sources: { priceMeta: table.meta },
    peak: {
      enabled: table.peak.enabled,
      effectiveAt: table.peak.effectiveAtMs
        ? ymdUtc(table.peak.effectiveAtMs)
        : null,
      windows: table.peak.windows.map((w) => ({
        beijing: `${pad2((w.start + 8) % 24)}:00-${pad2((w.end + 8) % 24)}:00`,
        rate: 2.0,
      })),
      weekendOffpeak: true,
      note: "高峰按价表 peak 子档计价；北京时间周六/周日全天谷价（与分享计价同一价目表）",
    },
    priceVersions: {
      versioned: true,
      invalidCount: 0,
      current: v(table.current),
      versions: table.versions.map(v),
      note: "逐条按记录时刻取当时生效版本段的价目与汇率",
    },
    services: {},
    qoderQuota: {
      cn: null,
      global: null,
      note: "云端角色未装配 Qoder 探测（不访问 8095/8097）",
    },
    dsh: { url: null, note: "云端角色未装配 DSH 快照读取" },
    dshMergedIntoTotal: {
      day: null,
      costCny: 0.0,
      note: "云端角色不并入任何本地 DSH 账本",
    },
    _generated_epoch: builtAt,
    _cached_age_seconds: 152.6,
  };
}

// ------------------------------------------------------------------ runtime / share facade
const gatewayRow = () => ({
  key: "share-gateway",
  display_name: "UM 分享入口 8100",
  port: GATEWAY_PORT,
  state: "Running",
  healthy: true,
  detail: "ok",
  process_ids: [GATEWAY_PID],
});
const ledgerPath = `${BACKEND_DIR}/${LEDGER_NAME}`;
/** console_share_production.share_attribution_status for the default (unset) key path. */
const attribution = () => ({
  file_ready: false,
  file_reason: "missing",
  key_file: ATTRIBUTION_KEY_FILE,
  key_chars: null,
  instance_loaded: {
    value: "unknown",
    basis: "inference",
    rule: "gateway_process_start_time_vs_key_file_mtime",
    reason: "key_file_not_ready",
    gateway_pid: null,
    gateway_started_at: null,
    key_file_mtime: null,
  },
  request_attribution_observed: false,
  request_attribution_detail: {
    verdict: "not_observed_here",
    runtime_fact: "backend_ledger_row_attribution_verified",
    ledger_file: ledgerPath,
  },
});
/** production_backend_observe as the facade and share.status consume it, per variant. */
const BACKEND_AUTH = {
  normal: { running: true, state: "valid", label: "认证有效" },
  "um-login-needed": { running: true, state: "needs_login", label: "需要登录" },
  "um-backend-down": { running: false, state: null, label: null },
};

function shareStatusBody(variant) {
  const auth = BACKEND_AUTH[variant];
  return {
    schema_version: 3,
    share_max_concurrency: 64,
    desired_generation: STORE_GENERATION,
    applied_generation: STORE_GENERATION,
    in_sync: true,
    pending_revocation: false,
    active_keys: KEYS.filter((k) => k.enabled).length,
    total_keys: KEYS.length,
    // BrokerClientAdapter state "ok"; the Linux broker status carries no version/pid, and
    // the BrokerDiag ring is empty after a broker restart, so neither key is mapped.
    broker: { running: true },
    last_publication_failure: null,
    blocked_publications: [],
    blocked: false,
    // No funnel_on / share_url: the cloud leaves the Tailscale/Funnel seams unwired.
    gateway_running: true,
    // account only when the 8110 auth state was observed (running); never {running:false}.
    ...(auth.state !== null
      ? { account: { valid: auth.state === "valid", running: auth.running } }
      : {}),
    attribution: attribution(),
    last_check_at: NOW,
  };
}
const keysBody = () => ({
  // list_keys only returns ACTIVE identities; a revoked row is a tombstone and
  // never appears in the console list.
  keys: KEYS.filter((k) => k.enabled).map((k) => ({
    id: k.id,
    name: k.name,
    masked: k.masked,
    plaintext_viewable: true,
    enabled: k.enabled,
    max_concurrency: k.max,
    row_version: k.rowVersion,
    created_at: k.created,
  })),
});
/** share_public_supervision.read_status with no watch files (the cloud runs no Funnel watch). */
const publicBody = () => ({
  available: true,
  version: 1,
  checked_at: NOW + 0.412,
  snapshot: null,
  events: [],
  events_truncated: false,
  events_unreadable: false,
  budget: {
    unreadable: false,
    reason: null,
    bootstrap: true,
    window_s: 1800.0,
    mapping_attempts_utc: [],
    ts_restarts_utc: [],
    service_starts_utc: [],
    mapping_used: 0,
    mapping_limit: 2,
    ts_restart_used: 0,
    ts_restart_limit: 1,
    service_start_used: 0,
    service_start_limit: 3,
    mapping_next_eligible_at_utc: null,
    ts_restart_next_eligible_at_utc: null,
    service_start_next_eligible_at_utc: null,
  },
  cancel: { cancelled: false, reason: null },
  unreadable: false,
  reason: null,
  state_file: "share-public-state.json",
});

// ------------------------------------------------------------------ action catalog
const CLOUD_ORCH_REASON = "云端直连 HTTPS：Tailscale/Funnel 编排未装配";
const BUDGET = {
  poll_interval_s: [1, 10],
  ts_confirm_budget_s: [1, 600],
  connectivity_budget_s: [1, 600],
  backend_budget_s: [1, 600],
  drain_budget_s: [1, 600],
  backend_confirm_budget_s: [1, 600],
};
// build_cloud_registry: [action, enabled, description, param schema {name: [min, max]} (all int)]
const CLOUD_ACTIONS = [
  [
    "service.start",
    true,
    "启动 UM 分享入口（Linux pid 文件与真实进程身份）",
    {},
  ],
  ["service.stop", true, "停止 UM 分享入口（先持久化停用意图，再发信号）", {}],
  [
    "service.restart",
    true,
    "重启 UM 分享入口（停止后显式启动；中断在途工作）",
    {},
  ],
  [
    "share.start",
    false,
    "UM 分享开启链（TS 服务→连接→后端→Funnel）",
    { ...BUDGET, backend_auth_budget_s: [1, 600] },
  ],
  ["share.stop", false, "UM 分享正常关闭链（撤入口→排空→后端→TS）", BUDGET],
  ["share.estop", false, "UM 分享紧急停止（立即撤入口，不排空）", BUDGET],
  [
    "share.retry_stop",
    false,
    "UM 分享残留关闭重试（按剩余步骤）",
    { ...BUDGET, step_interval_ms: [1, 60000] },
  ],
  [
    "share.backend_login",
    true,
    "登录分享专用后端（触发独立 profile 浏览器 UM 登录）",
    {},
  ],
  ["share.status", true, "UM 分享分层状态观察（只读）", {}],
  [
    "share.diagnostics",
    true,
    "UM 分享九层分层诊断（只读；含看门狗、维护视图与故障留证）",
    {},
  ],
  [
    "share.backend_reauthenticate",
    true,
    "仅重新认证分享专用后端 8110（不冷启动、不重启进程、不绕过人工认证）",
    {},
  ],
  [
    "share.backend_restart",
    true,
    "重启分享专用后端 8110（单层：暂停 8100 接收→有界排空→重启→恢复接收；不动 8100 实例与私人 8000）",
    {
      drain_budget_s: [1, 600],
      poll_interval_s: [1, 10],
      stop_budget_s: [1, 120],
      start_budget_s: [1, 180],
      lease_s: [60, 600],
    },
  ],
  [
    "share.gateway_restart",
    true,
    "重启分享入口 8100（单层：经受限 broker 宿主协议；不动 8110 实例）",
    { restart_budget_s: [10, 180] },
  ],
];
const actionsBody = () => ({
  actions: CLOUD_ACTIONS.map(([action, enabled, description, schema]) => ({
    action,
    enabled,
    disabled_reason: enabled ? null : CLOUD_ORCH_REASON,
    cancelable: true,
    description,
    param_schema: Object.fromEntries(
      Object.entries(schema).map(([k, [min, max]]) => [
        k,
        { type: "int", min, max },
      ])
    ),
  })).sort((a, b) => (a.action < b.action ? -1 : 1)),
});

// ------------------------------------------------------------------ operation ledger
/** console_share_actions.share_status in the cloud: TS/connectivity/Funnel seams unwired. */
function shareStatusResult(variant, observedAt) {
  const auth = BACKEND_AUTH[variant];
  const word =
    auth.state === "valid" ? "有效" : `未知(${auth.state ?? "未观察"})`; // _auth_word
  return {
    layers: {
      share_backend: auth.running ? "running" : "stopped",
      ts_service: "unavailable",
      connectivity: "unavailable",
      backend: "running",
      funnel: "unavailable",
    },
    desired_stop_in_force: false, // Linux intent file reads "running" after the explicit restart
    backend_auth: {
      state: auth.state,
      label: auth.label,
      observed_at: observedAt,
      attribution: attribution(),
    },
    note: `只读分层观察；unavailable 表示对应缝未接线/不可确认（如实，不冒充）；分享开启已确认（本进程无停止意图）；专用后端上游认证=${word}`,
  };
}
// Real ids are uuid4().hex; these short synthetic ids keep the data rule (< 24 chars).
function operationRow(
  id,
  action,
  target,
  resource,
  createdAt,
  runSec,
  steps,
  result
) {
  const startedAt = round(createdAt + 0.018, 6),
    finishedAt = round(startedAt + runSec, 6);
  return {
    id,
    action,
    target,
    state: "succeeded",
    resource,
    generation: 0,
    created_at: round(createdAt, 6),
    started_at: startedAt,
    finished_at: finishedAt,
    cancel_requested: false,
    steps: steps.map((note) => ({ at: round(startedAt + 0.003, 3), note })),
    result: result(round(finishedAt - 0.001, 6)),
    error: null,
    idempotency_key: null,
  };
}
function operationsBody(variant) {
  const statusStep = ["只读分层观察（无副作用，不触发任何启停）"];
  return {
    operations: [
      operationRow(
        "fx-cloud-op-3",
        "share.status",
        "share",
        "share:orchestration",
        NOW - 95.316,
        variant === "um-backend-down" ? 0.071 : 0.884,
        statusStep,
        (at) => shareStatusResult(variant, at)
      ),
      operationRow(
        "fx-cloud-op-2",
        "service.restart",
        "share-gateway",
        "service:share-gateway",
        bj("2026-10-31", "08:52", 10.207),
        2.413,
        [],
        () => ({ key: "share-gateway", status: "Restarted", action: "restart" })
      ),
      operationRow(
        "fx-cloud-op-1",
        "share.status",
        "share",
        "share:orchestration",
        bj("2026-10-30", "21:31", 5.412),
        0.902,
        statusStep,
        (at) => shareStatusResult("normal", at)
      ),
    ],
  };
}

// ------------------------------------------------------------------ cloud-blocked reads
const BLOCKED = [
  "usage-tokens",
  "usage-rates",
  "gateway-connection",
  "quota-snapshot",
  "logs",
  "log-sources",
  "log-entries",
  "log-overview",
  "model-pending",
  "stats",
];
const blockedBody = (capability) => ({
  error: "capability_not_assembled",
  capability,
  role: "cloud",
  detail: "云端角色只装配 UM 分享：该读面未装配，不探测/不读取非 UM 资源",
});

// ------------------------------------------------------------------ assembly
export function cloudFixtures({ variant = "normal" } = {}) {
  if (!VARIANTS.includes(variant))
    throw new Error(`unknown variant ${variant}; use ${VARIANTS.join(" / ")}`);
  const table = loadPriceTable(PRICE_FILE, NOW);
  for (const rec of ledgerRecords(variant)) {
    const version = versionAt(table.versions, parseTs(rec.time));
    if (!version?.tiers.some((models) => Object.hasOwn(models, rec.model)))
      throw new Error(
        `ledger model ${rec.model} is not an exact price-table id (fuzzy matching is not ported)`
      );
  }
  const ok = (body) => ({ status: 200, body });
  return {
    _anchor: ANCHOR,
    _note:
      `Synthetic cloud-role console fixtures (variant ${variant}) generated by cloud-synth.mjs from the cloud backend ` +
      "code paths; dates are shifted to today by shift.mjs at load. Do not hand-edit: regenerate.",
    "/api/summary": ok(summaryBody(table)),
    "/api/console/health": ok({
      checked_at: NOW,
      cached: false,
      services: [
        gatewayRow(),
        {
          id: "usagesrv",
          port: CONSOLE_PORT,
          state: "listening",
          note: "console host itself",
        },
      ],
    }),
    "/api/console/services": ok({
      ok: true,
      services: [gatewayRow()],
      meta: { source: "linux-pid-file", generated_epoch: NOW, cached: false },
    }),
    "/api/console/actions": ok(actionsBody()),
    "/api/console/operations?limit=8": ok(operationsBody(variant)),
    "/api/console/prices": ok({
      ok: true,
      ...structuredClone(PRICE_FILE),
      checkedStale: stale(table.meta.checkedAgeDays),
    }),
    "/api/console/share-management/status": ok(shareStatusBody(variant)),
    "/api/console/share-management/keys": ok(keysBody()),
    "/api/console/share-usage": ok(shareUsageBody(variant, table)),
    "/api/console/share-management/public": ok(publicBody()),
    "/api/console/share-management/control-status": ok({ responsive: true }),
    ...Object.fromEntries(
      BLOCKED.map((name) => [
        `/api/console/${name}`,
        { status: 503, body: blockedBody(name) },
      ])
    ),
  };
}

if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  const arg = (name, fallback) => {
    const i = process.argv.indexOf(name);
    return i >= 0 ? process.argv[i + 1] : fallback;
  };
  const variant = arg("--variant", "normal");
  const out = resolve(
    arg(
      "--out",
      resolve(dirname(fileURLToPath(import.meta.url)), "cloud-api.json")
    )
  );
  writeFileSync(
    out,
    JSON.stringify(cloudFixtures({ variant }), null, 2) + "\n",
    "utf8"
  );
  console.log(JSON.stringify({ variant, out }));
}
