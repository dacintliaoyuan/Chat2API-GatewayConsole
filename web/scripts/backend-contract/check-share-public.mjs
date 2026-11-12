import test from "node:test";
import assert from "node:assert/strict";
import { BackendClient } from "../../src/console/backend/client.ts";
import {
  ContractError,
  normalizePublicDnsDetail,
  normalizePublicShareEvents,
  normalizePublicShareExport,
  normalizePublicShareStatus,
} from "../../src/console/backend/normalize.ts";
import {
  publicBudgetText,
  publicDnsTtlText,
  publicShareSummary,
  publicSupervisionReading,
  publicTlsReading,
} from "../../src/console/share-readiness.ts";

const BUDGET = {
  unreadable: false,
  reason: null,
  window_s: 1800,
  mapping_used: 2,
  mapping_limit: 2,
  ts_restart_used: 1,
  ts_restart_limit: 1,
  mapping_attempts_utc: ["2026-10-24T12:00:00+00:00"],
  ts_restarts_utc: ["2026-10-24T12:10:00+00:00"],
  mapping_next_eligible_at_utc: "2026-10-24T12:30:00+00:00",
  ts_restart_next_eligible_at_utc: "2026-10-24T12:40:00+00:00",
};

const DNS = {
  version: 1,
  checked_at_utc: "2026-10-24T12:00:00+00:00",
  checked_at_epoch: 1761307200.0,
  state: "disagreement",
  reason: "dns_disagreement",
  addresses: ["203.0.113.10"],
  addresses_truncated: false,
  ttl_s: 60,
  negative_ttl_s: 300,
  sources: [
    {
      source: "cloudflare",
      state: "nxdomain",
      status: 3,
      classification: "nxdomain",
      addresses: [],
      addresses_truncated: false,
      ttl_s: null,
      negative_ttl_s: 300,
      checked_at_epoch: 1761307200.0,
      elapsed_ms: 42,
    },
    {
      source: "google",
      state: "ok",
      status: 0,
      classification: "ok",
      addresses: ["203.0.113.10"],
      addresses_truncated: false,
      ttl_s: 60,
      negative_ttl_s: null,
      checked_at_epoch: 1761307200.0,
      elapsed_ms: 38,
    },
  ],
};

const SNAPSHOT = {
  observed_at_epoch: 1761307200.0,
  overall: "all_failed",
  reason: "all_entries_failed",
  public_url: "https://share.example.ts.net",
  host: "share.example.ts.net",
  entries: [
    {
      host: "share.example.ts.net",
      ip: "203.0.113.10",
      state: "failed",
      dns: "ok",
      tls: "failed",
      certificate: "not_checked",
      reason: "tls_handshake_failed",
      elapsed_ms: 42,
      checked_at_epoch: 1761307200.0,
    },
  ],
  dns: DNS,
  recovery: {
    state: "manual_required",
    action: "mapping_resend",
    stage: "verify",
    reason: "security_unconfirmed",
    attempted_at_utc: "2026-10-24T12:00:00+00:00",
    finished_at_utc: "2026-10-24T12:00:10+00:00",
    manual: false,
  },
  budget: BUDGET,
  intent: { state: "running", at_utc: "2026-10-24T11:59:00+00:00" },
  round: {
    next_check_at_utc: "2026-10-24T12:01:00+00:00",
    next_action_at_utc: null,
  },
  local: { maintenance: "none", connectivity: "ok" },
};

const STATUS = {
  available: true,
  version: 1,
  checked_at: 1761307200.0,
  snapshot: SNAPSHOT,
  events: [
    {
      at_utc: "2026-10-24T12:00:05+00:00",
      status: "all_failed",
      code: "all_entries_failed",
      action: "none",
    },
  ],
  events_truncated: false,
  events_unreadable: false,
  budget: BUDGET,
  cancel: { cancelled: false, reason: null },
  unreadable: false,
  reason: null,
  state_file: "share-public-state.json",
};

test("available is mandatory; the endpoint always returns it", () => {
  assert.throws(
    () => normalizePublicShareStatus({ snapshot: SNAPSHOT }),
    ContractError
  );
  assert.throws(
    () => normalizePublicShareEvents({ events: [] }),
    ContractError
  );
});

test("missing optional faces stay null instead of a healthy default", () => {
  const status = normalizePublicShareStatus({ available: true });
  assert.equal(status.snapshot, null);
  assert.equal(status.events, null);
  assert.equal(status.budget, null);
  assert.equal(status.cancel, null);
  assert.equal(status.unreadable, true);
  assert.equal(status.version, null);
  assert.equal(status.checkedAt, null);
  const events = normalizePublicShareEvents({ available: true });
  assert.equal(events.events, null);
  assert.equal(events.nextBefore, null);
  assert.equal(events.truncated, null);
});

test("events_unreadable is preserved, never rendered as no events", () => {
  const status = normalizePublicShareStatus({
    ...STATUS,
    events: null,
    events_unreadable: true,
  });
  assert.equal(status.events, null);
  assert.equal(status.eventsUnreadable, true);
});

test("an unreadable budget is never folded into 0/0", () => {
  const status = normalizePublicShareStatus({
    ...STATUS,
    budget: { ...BUDGET, unreadable: true, reason: "budget_unreadable" },
  });
  assert.equal(status.budget?.unreadable, true);
  assert.equal(status.budget?.mappingUsed, 2);
  assert.equal(publicBudgetText(status.budget), "预算不可读，不自动恢复");
});

test("a missing unreadable flag or count stays unknown, never a readable 0/0", () => {
  const missingFlag = normalizePublicShareStatus({
    ...STATUS,
    budget: { ...BUDGET, unreadable: undefined },
  });
  assert.equal(missingFlag.budget?.unreadable, null);
  assert.equal(
    publicBudgetText(missingFlag.budget),
    "预算未提供（不可读标记未提供），不自动恢复"
  );
  const missingCount = normalizePublicShareStatus({
    ...STATUS,
    budget: { ...BUDGET, mapping_used: undefined },
  });
  assert.equal(missingCount.budget?.mappingUsed, null);
  assert.equal(
    publicBudgetText(missingCount.budget),
    "预算未提供（次数不完整），不自动恢复"
  );
});

test("unknown closed-set values become explicit unknowns, never ok", () => {
  const status = normalizePublicShareStatus({
    ...STATUS,
    snapshot: {
      ...SNAPSHOT,
      overall: "healthy",
      reason: "invented_reason",
      entries: [{ ...SNAPSHOT.entries[0], state: "maybe" }],
      recovery: { ...SNAPSHOT.recovery, state: "resuming" },
      intent: { ...SNAPSHOT.intent, state: "paused" },
    },
  });
  assert.equal(status.snapshot?.overall, "unknown");
  assert.equal(status.snapshot?.reason, "invented_reason");
  assert.equal(status.snapshot?.entries[0]?.state, "unchecked");
  assert.equal(status.snapshot?.recovery?.state, "unknown");
  assert.equal(status.snapshot?.intent?.state, "unknown");
});

test("missing intent/round are null, not invented running/next times", () => {
  const status = normalizePublicShareStatus({
    ...STATUS,
    snapshot: { ...SNAPSHOT, intent: undefined, round: undefined },
  });
  assert.equal(status.snapshot?.intent, null);
  assert.equal(status.snapshot?.round, null);
});

test("DNS detail keeps two sources and unknown fields stay unknown", () => {
  const status = normalizePublicShareStatus({
    ...STATUS,
    snapshot: {
      ...SNAPSHOT,
      dns: {
        ...DNS,
        state: "healthy",
        ttl_s: -1,
        sources: [
          { ...DNS.sources[0], state: "maybe", ttl_s: "soon" },
          { ...DNS.sources[1], state: "ok" },
        ],
      },
    },
  });
  assert.equal(status.snapshot?.dns?.state, "not_checked");
  assert.equal(status.snapshot?.dns?.ttlSeconds, null);
  assert.equal(status.snapshot?.dns?.sources[0]?.state, "not_checked");
  assert.equal(status.snapshot?.dns?.sources[0]?.ttlSeconds, null);
  assert.equal(status.snapshot?.dns?.sources.length, 2);
  const missing = normalizePublicShareStatus({
    ...STATUS,
    snapshot: { ...SNAPSHOT, dns: undefined },
  });
  assert.equal(missing.snapshot?.dns, null);
});

test("DNS disagreement is a warning, never a healthy public TLS reading", () => {
  const snapshot = normalizePublicShareStatus({
    ...STATUS,
    snapshot: {
      ...SNAPSHOT,
      overall: "degraded",
      reason: "dns_disagreement",
      entries: [
        {
          ...SNAPSHOT.entries[0],
          state: "ok",
          dns: "ok",
          tls: "ok",
          certificate: "ok",
        },
      ],
    },
  }).snapshot;
  assert.equal(publicTlsReading(snapshot).state, "warn");
  assert.equal(publicTlsReading(snapshot).text.includes("DNS 解析分歧"), true);
  assert.equal(
    publicDnsTtlText(snapshot?.dns, 1761307210),
    "TTL 剩余约 50 s · 负缓存剩余约 290 s"
  );
  assert.equal(
    publicDnsTtlText(snapshot?.dns, 1761307500),
    "TTL 已到期，等待新检查 · 负缓存已到期，等待新检查"
  );
  assert.equal(
    publicDnsTtlText(
      normalizePublicDnsDetail({ ...DNS, checked_at_epoch: undefined }),
      1761307300
    ),
    "TTL 剩余未知（缺采样时间） · 负缓存剩余未知（缺采样时间）"
  );
  const nxdomain = normalizePublicShareStatus({
    ...STATUS,
    snapshot: {
      ...SNAPSHOT,
      overall: "not_checked",
      reason: "dns_nxdomain",
      entries: [],
      dns: { ...DNS, state: "nxdomain", reason: "dns_nxdomain" },
    },
  }).snapshot;
  assert.equal(publicTlsReading(nxdomain).state, "warn");
  assert.equal(publicTlsReading(nxdomain).text, "DNS 域名不存在（NXDOMAIN）");
  assert.equal(
    publicDnsTtlText(nxdomain?.dns, 1761307300),
    "负缓存剩余约 200 s"
  );
  assert.equal(
    publicDnsTtlText(nxdomain?.dns, 1761307500),
    "负缓存已到期，等待新检查"
  );
});

test("pure DNS degradation is page-only: no automatic recovery wording", () => {
  const snapshot = normalizePublicShareStatus({
    ...STATUS,
    snapshot: {
      ...SNAPSHOT,
      overall: "degraded",
      reason: "dns_disagreement",
      entries: [
        { ...SNAPSHOT.entries[0], state: "ok", tls: "ok", certificate: "ok" },
      ],
      dns: DNS,
    },
  }).snapshot;
  const reading = publicSupervisionReading(snapshot);
  assert.equal(reading.state, "warn");
  assert.equal(reading.text.includes("DNS 解析分歧"), true);
  assert.equal(reading.text.includes("自动恢复"), false);
  assert.equal(publicDnsTtlText(null), "TTL 未提供");
});

test("a cap-saturated source is not_checked, not a timeout", () => {
  const snapshot = normalizePublicShareStatus({
    ...STATUS,
    snapshot: {
      ...SNAPSHOT,
      overall: "not_checked",
      reason: "dns_not_checked",
      entries: [],
      observed_at_epoch: Math.floor(Date.now() / 1000),
      round: {
        next_check_at_utc: new Date(Date.now() + 40000).toISOString(),
        next_action_at_utc: null,
      },
      dns: {
        ...DNS,
        state: "not_checked",
        reason: "dns_not_checked",
        addresses: [],
        ttl_s: null,
        negative_ttl_s: null,
        sources: DNS.sources.map((source) => ({
          ...source,
          state: "not_checked",
          classification: "not_checked",
          addresses: [],
          ttl_s: null,
          negative_ttl_s: null,
        })),
      },
    },
  }).snapshot;
  assert.equal(publicTlsReading(snapshot).state, "warn");
  assert.equal(publicTlsReading(snapshot).text, "DNS 未检查");
  assert.equal(publicSupervisionReading(snapshot).text, "DNS 未检查");
});

test("a truncated address set is an incomplete inspection, not all failed", () => {
  const snapshot = normalizePublicShareStatus({
    ...STATUS,
    snapshot: {
      ...SNAPSHOT,
      overall: "incomplete",
      reason: "addresses_truncated",
      entries: [
        { ...SNAPSHOT.entries[0], state: "failed", tls: "failed" },
        { ...SNAPSHOT.entries[0], state: "failed", tls: "failed" },
      ],
      dns: { ...DNS, addresses_truncated: true },
    },
  }).snapshot;
  assert.equal(
    publicSupervisionReading(snapshot).text.includes("地址覆盖不完整"),
    true
  );
  assert.equal(publicTlsReading(snapshot).text.includes("地址超过 4 个"), true);
  assert.equal(publicTlsReading(snapshot).state, "warn");
});

test("a manual test keeps its DNS detail and never claims a clean success", () => {
  const detail = normalizePublicDnsDetail({ ...DNS, state: "healthy" });
  assert.equal(detail?.state, "not_checked");
  assert.equal(detail?.ttlSeconds, 60);
  const summary = publicShareSummary(
    { share_url: "https://share.example.ts.net" },
    {
      rowVersion: 1,
      state: "ok",
      reason: "ok",
      startedAt: 1761307200,
      at: Math.floor(Date.now() / 1000),
      publicUrl: "https://share.example.ts.net",
      dns: DNS,
    }
  );
  assert.equal(summary.state, "warn");
  assert.equal(summary.text.includes("DNS 解析分歧"), true);
});

test("numbers only accept finite nonnegative values; booleans only accept booleans", () => {
  const status = normalizePublicShareStatus({
    ...STATUS,
    version: -1,
    checked_at: Number.NaN,
    snapshot: {
      ...SNAPSHOT,
      observed_at_epoch: -5,
      entries: [
        {
          ...SNAPSHOT.entries[0],
          elapsed_ms: Number.POSITIVE_INFINITY,
          checked_at_epoch: "now",
        },
      ],
      recovery: { ...SNAPSHOT.recovery, manual: "yes" },
    },
  });
  assert.equal(status.version, null);
  assert.equal(status.checkedAt, null);
  assert.equal(status.snapshot?.observedAt, null);
  assert.equal(status.snapshot?.entries[0]?.elapsedMs, null);
  assert.equal(status.snapshot?.entries[0]?.checkedAt, null);
  assert.equal(status.snapshot?.recovery?.manual, null);
});

test("the export keeps only allowlisted faces and carries no injected secret", () => {
  const exported = normalizePublicShareExport({
    export_version: 1,
    generated_at: "2026-10-24T12:00:00+00:00",
    redaction: { fields: "allowlist" },
    secret: "should-not-survive",
    token: "should-not-survive",
    cookie: "should-not-survive",
    authorization: "should-not-survive",
    password: "should-not-survive",
    snapshot: {
      ...SNAPSHOT,
      secret: "should-not-survive",
      token: "should-not-survive",
      local: { maintenance: "none", connectivity: "ok" },
    },
    events: STATUS.events,
    budget: BUDGET,
    cancel: { cancelled: false, reason: null },
    limits: { window_s: 1800, mapping_limit: 2 },
  });
  const text = JSON.stringify(exported);
  for (const secret of [
    "should-not-survive",
    "authorization",
    "password",
    "cookie",
  ]) {
    assert.equal(text.includes(secret), false);
  }
  assert.equal(exported.snapshot?.overall, "all_failed");
  assert.equal(exported.limits?.window_s, 1800);
});

function transport(status = 200, body = STATUS) {
  const calls = [];
  return {
    calls,
    request: async (request) => {
      calls.push(request);
      return { status, body };
    },
  };
}

test("client reads the public faces on their own same-origin paths", async () => {
  const session = transport();
  const client = new BackendClient(session);
  const status = await client.readPublicShareStatus();
  assert.equal(status.state, "ready");
  assert.equal(status.value?.snapshot?.overall, "all_failed");
  assert.deepEqual(session.calls[0], {
    method: "GET",
    path: "/share-management/public",
    access: "session",
    signal: undefined,
  });

  const eventsTransport = transport(200, {
    available: true,
    events: [],
    next_before: 1761300000.0,
    truncated: false,
  });
  const events = await new BackendClient(eventsTransport).readPublicShareEvents(
    1761300000,
    25
  );
  assert.equal(events.state, "ready");
  assert.equal(
    eventsTransport.calls[0].path,
    "/share-management/public-events?limit=25&before=1761300000"
  );

  const exportTransport = transport(200, {
    export_version: 1,
    generated_at: "2026-10-24T12:00:00+00:00",
    redaction: { fields: "allowlist" },
    snapshot: SNAPSHOT,
    events: [],
    budget: BUDGET,
    cancel: { cancelled: false, reason: null },
    limits: { window_s: 1800 },
  });
  const exported = await new BackendClient(
    exportTransport
  ).readPublicShareExport();
  assert.equal(exported.state, "ready");
  assert.equal(
    exportTransport.calls[0].path,
    "/share-management/public-export?format=json"
  );
});

test("events query bounds are rejected before any request is sent", async () => {
  const session = transport();
  const client = new BackendClient(session);
  await assert.rejects(
    () => client.readPublicShareEvents(null, 0),
    ContractError
  );
  await assert.rejects(
    () => client.readPublicShareEvents(null, 201),
    ContractError
  );
  await assert.rejects(
    () => client.readPublicShareEvents(0, 10),
    ContractError
  );
  await assert.rejects(
    () => client.readPublicShareEvents(-1, 10),
    ContractError
  );
  assert.equal(session.calls.length, 0);
});

test("non-2xx maps 404 to unavailable, 401/403 to refused and others to error", async () => {
  for (const [httpStatus, state] of [
    [404, "unavailable"],
    [401, "refused"],
    [403, "refused"],
    [500, "error"],
  ]) {
    const read = await new BackendClient(
      transport(httpStatus, { error: "x" })
    ).readPublicShareStatus();
    assert.equal(read.state, state);
    assert.equal(read.value, null);
    assert.equal(read.httpStatus, httpStatus);
  }
});
