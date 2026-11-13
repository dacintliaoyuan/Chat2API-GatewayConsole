import test from "node:test";
import assert from "node:assert/strict";
import {
  ActionBusyError,
  ActionUnavailableError,
  BackendClient,
  BackendSession,
  ContractError,
  FixtureTransport,
  FIXTURE_CONNECTION_BODIES,
  FIXTURE_CONNECTION_KEY,
  FIXTURE_CONNECTION_STATES,
  GATEWAY_ORDER_FORMAT_VERSION,
  GATEWAY_ORDER_STORAGE_KEY,
  GatewayConnectionBinding,
  GatewayOrderPreferences,
  LatestRequests,
  MAX_GATEWAY_ORDER_IDS,
  copyGatewayConnectionField,
  count,
  createFetchTransport,
  gatewayConnectionFixtureReads,
  gatewayConnectionFixtureResponse,
  joinModelDetails,
  normalizeCatalog,
  normalizeContextEvidence,
  normalizeContexts,
  normalizeGatewayConnection,
  normalizeLogPage,
  normalizeShareUsage,
  reorderGatewaySubset,
  PaneWidthPreferences,
  PANE_WIDTH_FORMAT_VERSION,
  PANE_WIDTH_STORAGE_KEY,
  MAX_PANE_WIDTHS,
  clampPaneWidth,
  paneWidthSpec,
  paneWidthAfterMove,
} from "../../src/console/backend/index.ts";

// Local samples exercise individual boundaries; check-generated.mjs separately
// consumes the synthetic snapshots produced by the real Python handlers.
const actions = [
  "model.catalog",
  "model.context_overrides",
  "model.context_probe_preview",
  "model.context_probe_run",
].map((action) => ({
  action,
  enabled: true,
  cancelable: action === "model.context_probe_run",
  disabledReason: null,
  description: "offline fixture",
}));
const catalog = {
  catalogs: {
    um: [
      {
        id: "test-model",
        display_name: "目录模型",
        context: 131072,
        context_state: "declared",
        output_token_cap: { value: 16384, source: "compat_default" },
        efforts: ["low", "high"],
        default_effort: "low",
        efforts_source: "upstream",
        publish: true,
      },
    ],
  },
  warnings: [],
  checked_epoch: 1790920800,
};
const contexts = {
  overrides: [
    {
      source: "um",
      model_id: "test-model",
      declared_context: 131072,
      override_context: null,
      effective_context: 131072,
      effective_source: "declared",
      declared_at: null,
      settings_draft: {
        alias: "新名称",
        contextSource: "manual",
        contextTokens: 64000,
        defaultEffort: "low",
      },
      settings_draft_version: 2,
      settings_applied: null,
      settings_applied_version: null,
      settings_held: true,
      client_context: null,
      client_readback_state: "absent",
      client_matches_applied: null,
    },
  ],
};
const evidence = {
  records: [
    {
      source: "um",
      model_id: "test-model",
      declared_context: 131072,
      token_evidence: 4096,
      observed_floor: 16000,
      observed_ceiling: 32000,
      status: "verified",
      evidence_revision: 1,
      scheme_revision: 2,
      last_success: "2026-10-02T12:00:00+08:00",
      last_run: "2026-10-02T12:00:00+08:00",
      stale_reason: null,
      history_count: 0,
    },
  ],
};
const logs = {
  status: "ok",
  rows: [
    {
      ts: "2026-10-02 12:00:00,000",
      level: "INFO",
      message: "request summary request=fixture-id final=HTTP400",
      parse_ok: true,
      cut: false,
      event: {
        timestamp: "2026-10-02 12:00:00,000",
        request_id: "fixture-id",
        model: null,
        phase: null,
        outcome: "error",
        error_code: "HTTP400",
        duration_ms: null,
        token_rate: null,
        token_rate_kind: null,
      },
    },
    {
      ts: null,
      level: null,
      message: "unknown runtime text",
      parse_ok: false,
      cut: false,
      event: {
        timestamp: null,
        request_id: null,
        model: null,
        phase: null,
        outcome: null,
        error_code: null,
        duration_ms: null,
        token_rate: 0,
        token_rate_kind: "mean_overall",
      },
    },
  ],
  next_cursor: null,
  reset_hint: false,
  truncated: false,
  scanned_bytes: 400,
};
const share = {
  requests: 4,
  checkedAt: "2026-10-02T12:00:00+08:00",
  keys: [
    {
      key_id: "fixture-stable-key",
      keyName: "研发",
      deleted: false,
      requests: 4,
      knownCostUsd: 0,
      estimatedCostUsd: 0.0001,
      inputTokens: 50,
      outputTokens: 10,
      unknownRequests: 1,
      unpricedRequests: 1,
      models: [
        {
          model: "test-model",
          requests: 4,
          knownCostUsd: 0,
          estimatedCostUsd: 0.0001,
          inputTokens: 50,
          outputTokens: 10,
          unknownRequests: 1,
          unpricedRequests: 1,
        },
      ],
      days: [
        {
          day: "2026-10-02",
          requests: 4,
          knownCostUsd: 0,
          estimatedCostUsd: 0.0001,
          inputTokens: 50,
          outputTokens: 10,
          unknownRequests: 1,
          unpricedRequests: 1,
        },
      ],
    },
  ],
  unattributed: {
    key_id: null,
    requests: 1,
    costCny: null,
    knownCostCny: 0,
    estimatedCostCny: null,
    costCnyPartial: true,
    knownCostCnyPartial: false,
    estimatedCostCnyPartial: true,
  },
  coverage: { records: 5, priceFn: true },
};
for (const row of [
  share.keys[0],
  ...share.keys[0].models,
  ...share.keys[0].days,
]) {
  row.coverage = {
    records: 4,
    pricedRequests: 2,
    unknownTimeRequests: 0,
    pricingUnavailableRequests: 0,
    costComplete: false,
  };
  row.priceVersions = ["fixture-price-v1"];
  row.tokenUsage = {
    totalTokens: 40,
    inputTokens: 30,
    outputTokens: 10,
    lowerBoundTokens: 20,
    lowerBoundInputTokens: 20,
    lowerBoundOutputTokens: null,
    estimatedTokens: 0,
    estimatedInputTokens: 0,
    estimatedOutputTokens: 0,
    completeRequests: 1,
    lowerBoundRequests: 1,
    estimatedRequests: 1,
    unknownRequests: 1,
    complete: false,
  };
  row.costCny = 0.0012;
  row.knownCostCny = 0.0008;
  row.estimatedCostCny = 0.0004;
  row.costCnyPartial = true;
  row.knownCostCnyPartial = true;
  row.estimatedCostCnyPartial = false;
}
// The per-model row is fully convertible in this fixture; the key/day rows are
// lower bounds, so the partial flag must stay attached to the exact row.
share.keys[0].models[0].costCnyPartial = false;
share.keys[0].models[0].knownCostCnyPartial = false;
share.keys[0].models[0].estimatedCostCnyPartial = false;
function scenario(operations = {}) {
  return {
    name: "offline-contract",
    actions,
    reads: {
      "/share-usage": { status: 200, body: share },
      "/log-entries": { status: 200, body: logs },
    },
    operations: {
      "model.catalog": [{ result: catalog }],
      "model.context_overrides": [{ result: contexts }],
      "model.context_probe_preview": [{ result: evidence }],
      ...operations,
    },
  };
}
function clock() {
  let now = 0;
  return {
    now: () => now,
    sleep: async (ms) => {
      now += ms;
    },
  };
}
function posts(transport) {
  return transport.calls.filter(
    (call) => call.method === "POST" && call.path === "/operations"
  );
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

// Rev15 gateway connection read face (2026-10-06): synthetic fixtures only —
// invented services, ports, paths and a sentinel key; no live gateway, file, key,
// Python process or network request.
function connectionScenario(reads = gatewayConnectionFixtureReads()) {
  return {
    name: "offline-connection-contract",
    actions: [],
    reads,
    operations: {},
  };
}
function connectionClient(reads = gatewayConnectionFixtureReads()) {
  return new BackendClient(
    new FixtureTransport(connectionScenario(reads)),
    clock()
  );
}

test("a zero duration is evidence; a nonpositive output capacity is unavailable", () => {
  const runtime = structuredClone(logs);
  runtime.rows[0].event.duration_ms = 0;
  assert.equal(normalizeLogPage(runtime).events[0].durationMs, 0);
  for (const invalid of [0, -1, true, "0", null, NaN, Infinity]) {
    const directory = structuredClone(catalog);
    directory.catalogs.um[0].output_token_cap.value = invalid;
    assert.equal(normalizeCatalog(directory).models[0].outputCap.value, null);
  }
});

test("unknown numeric values stay null; actual zero stays known", () => {
  for (const invalid of [
    undefined,
    null,
    true,
    false,
    "0",
    -1,
    1.5,
    NaN,
    Infinity,
  ])
    assert.equal(count(invalid), null);
  assert.equal(count(0), 0);
  assert.equal(count(600), 600);
});

test("model joins use source + real model identity and separate character/Token evidence", () => {
  const normalized = normalizeCatalog(catalog);
  const detail = joinModelDetails(
    normalized,
    normalizeContexts(contexts),
    normalizeContextEvidence(evidence)
  )[0];
  assert.equal(detail.catalog.modelId, "test-model");
  assert.equal(detail.context.draft.alias, "新名称");
  assert.equal(detail.context.clientContextTokens, null);
  assert.equal(detail.context.clientReadbackState, "absent");
  assert.equal(detail.evidence.verifiedTokenFloor, 4096);
  assert.equal(detail.evidence.observedCharacterFloor, 16000);
  assert.equal(detail.catalog.outputCap.source, "compat_default");
  const zero = structuredClone(evidence);
  zero.records[0].token_evidence = 0;
  assert.equal(normalizeContextEvidence(zero)[0].verifiedTokenFloor, 0);
  const other = structuredClone(contexts);
  other.overrides[0].source = "bogus_source";
  assert.equal(
    joinModelDetails(normalized, normalizeContexts(other), []).find(
      (row) => row.key === "bogus_source|test-model"
    ).catalog,
    null
  );
});

test("history and per-client readback stay independent from the applied record", () => {
  const snapshot = structuredClone(contexts);
  snapshot.overrides[0].client_readbacks = [
    {
      consumer: "fixture-client",
      state: "ok",
      contextTokens: 64000,
      alias: "old name",
      defaultEffort: "high",
      outputTokenCap: { value: 16384, source: "client_config" },
      matchesApplied: { context: true, alias: false, defaultEffort: false },
    },
  ];
  const normalized = normalizeContexts(snapshot)[0];
  assert.equal(normalized.clientReadbacks[0].matchesApplied.alias, false);
  assert.equal(normalized.applied, null);
  const history = structuredClone(evidence);
  history.records[0].history = [
    {
      evidence_revision: 1,
      observed_floor: 8000,
      token_evidence: null,
      status: "verified",
      archived_at: "2026-10-01",
    },
  ];
  const record = normalizeContextEvidence(history)[0].history[0];
  assert.equal(record.observedCharacterFloor, 8000);
  assert.equal(record.verifiedTokenFloor, null);
});

test("log lookup passes an exact bounded identity independently of the text query", async () => {
  const transport = new FixtureTransport(scenario());
  const client = new BackendClient(transport, clock());
  await client.readLogs({
    source: "chat2api",
    category: "service",
    request_id: "abc",
    q: "HTTP400",
  });
  const call = transport.calls.find((row) =>
    row.path.startsWith("/log-entries")
  );
  const params = new URL(call.path, "http://fixture.invalid").searchParams;
  assert.equal(params.get("request_id"), "abc");
  assert.equal(params.get("q"), "HTTP400");
  await assert.rejects(
    client.readLogs({
      source: "chat2api",
      category: "service",
      request_id: "a".repeat(65),
    }),
    ContractError
  );
});

test("logs preserve structured null fields, raw fallback and a genuine zero rate", () => {
  const view = normalizeLogPage(logs);
  assert.equal(view.events[0].requestId, "fixture-id");
  assert.equal(view.events[0].tokenRate, null);
  assert.equal(view.events[1].tokenRate, 0);
  assert.equal(view.events[1].parsed, false);
  assert.equal(view.events[1].rawMessage, "unknown runtime text");
});

test("key name and stable identity are independent; missing local coverage is not zero", () => {
  const view = normalizeShareUsage(share);
  assert.equal(view.keys[0].keyName, "研发");
  assert.equal(view.keys[0].knownCostUsd, 0);
  assert.equal(view.keys[0].coverage.unknownRequests, 1);
  assert.equal(view.keys[0].days[0].coverage.localCoverageAvailable, true);
  assert.equal(view.keys[0].days[0].coverage.costComplete, false);
  assert.equal(view.keys[0].tokenUsage.totalTokens, 40);
  assert.equal(view.keys[0].tokenUsage.lowerBoundTokens, 20);
  assert.equal(view.keys[0].tokenUsage.estimatedTokens, 0);
  const legacy = structuredClone(share);
  delete legacy.keys[0].days[0].unknownRequests;
  delete legacy.keys[0].days[0].unpricedRequests;
  legacy.keys[0].keyName = null;
  assert.equal(
    normalizeShareUsage(legacy).keys[0].days[0].coverage.localCoverageAvailable,
    false
  );
  assert.equal(
    normalizeShareUsage(legacy).keys[0].days[0].coverage.unknownRequests,
    null
  );
  assert.equal(normalizeShareUsage(legacy).keys[0].keyId, "fixture-stable-key");
});

test("CNY fields normalize per row and a legacy response stays unknown, never zero", () => {
  const view = normalizeShareUsage(share);
  assert.equal(view.keys[0].costCny, 0.0012);
  assert.equal(view.keys[0].knownCostCny, 0.0008);
  assert.equal(view.keys[0].estimatedCostCny, 0.0004);
  assert.equal(view.keys[0].coverage.costCnyPartial, true);
  assert.equal(view.keys[0].coverage.knownCostCnyPartial, true);
  assert.equal(view.keys[0].coverage.estimatedCostCnyPartial, false);
  assert.equal(view.keys[0].models[0].costCny, 0.0012);
  assert.equal(view.keys[0].models[0].coverage.costCnyPartial, false);
  assert.equal(view.keys[0].models[0].coverage.knownCostCnyPartial, false);
  assert.equal(view.keys[0].models[0].coverage.estimatedCostCnyPartial, false);
  assert.equal(view.keys[0].days[0].estimatedCostCny, 0.0004);
  assert.equal(view.keys[0].days[0].coverage.costCnyPartial, true);
  assert.equal(view.unattributed.costCny, null);
  assert.equal(view.unattributed.knownCostCny, 0);
  assert.equal(view.unattributed.estimatedCostCny, null);
  assert.equal(view.unattributed.coverage.costCnyPartial, true);
  assert.equal(view.unattributed.coverage.knownCostCnyPartial, false);
  assert.equal(view.unattributed.coverage.estimatedCostCnyPartial, true);
  // A pre-CNY response (fields absent) keeps null: unknown is not zero, and the
  // normaliser does not reject the legacy shape.
  const legacy = structuredClone(share);
  for (const row of [
    legacy.keys[0],
    ...legacy.keys[0].models,
    ...legacy.keys[0].days,
    legacy.unattributed,
  ]) {
    delete row.costCny;
    delete row.knownCostCny;
    delete row.estimatedCostCny;
    delete row.costCnyPartial;
    delete row.knownCostCnyPartial;
    delete row.estimatedCostCnyPartial;
  }
  const old = normalizeShareUsage(legacy);
  assert.equal(old.keys[0].costCny, null);
  assert.equal(old.keys[0].knownCostCny, null);
  assert.equal(old.keys[0].estimatedCostCny, null);
  assert.equal(old.keys[0].coverage.costCnyPartial, null);
  assert.equal(old.keys[0].coverage.knownCostCnyPartial, null);
  assert.equal(old.keys[0].coverage.estimatedCostCnyPartial, null);
  assert.equal(old.keys[0].models[0].costCny, null);
  assert.equal(old.keys[0].days[0].knownCostCny, null);
  assert.equal(old.keys[0].days[0].coverage.costCnyPartial, null);
  assert.equal(old.unattributed.costCny, null);
  assert.equal(old.unattributed.knownCostCny, null);
  assert.equal(old.unattributed.estimatedCostCny, null);
  assert.equal(old.unattributed.coverage.costCnyPartial, null);
  assert.equal(old.unattributed.undated, null);
  // Malformed CNY values are unknown, never coerced into a number.
  const broken = structuredClone(share);
  broken.keys[0].costCny = "0.0012";
  broken.keys[0].knownCostCny = -1;
  broken.keys[0].costCnyPartial = "true";
  const rejected = normalizeShareUsage(broken).keys[0];
  assert.equal(rejected.costCny, null);
  assert.equal(rejected.knownCostCny, null);
  assert.equal(rejected.coverage.costCnyPartial, null);
});

test("the unattributed bucket carries its own Token split and is never merged into a key", () => {
  // Rev19: the backend delivers the bucket's own tokenUsage / models / days, so
  // the normaliser reads them exactly as a key's rows; the bucket stays its own
  // row (keyId null) and is never spread over the keys.
  const payload = structuredClone(share);
  payload.unattributed = {
    key_id: null,
    requests: 1,
    knownCostUsd: 0.5,
    estimatedCostUsd: 0.25,
    costCny: null,
    knownCostCny: 3.5,
    estimatedCostCny: null,
    costCnyPartial: true,
    inputTokens: 50,
    outputTokens: 10,
    unknownRequests: 1,
    unpricedRequests: 0,
    priceVersions: ["fixture-price-v1"],
    coverage: {
      records: 1,
      pricedRequests: 1,
      unknownTimeRequests: 0,
      pricingUnavailableRequests: 0,
      costComplete: true,
    },
    tokenUsage: {
      totalTokens: 60,
      inputTokens: 50,
      outputTokens: 10,
      lowerBoundTokens: 0,
      lowerBoundInputTokens: null,
      lowerBoundOutputTokens: null,
      estimatedTokens: 0,
      estimatedInputTokens: null,
      estimatedOutputTokens: null,
      completeRequests: 1,
      lowerBoundRequests: 0,
      estimatedRequests: 0,
      unknownRequests: 0,
      complete: true,
    },
    models: [{ model: "test-model", requests: 1, knownCostUsd: 0.5 }],
    days: [{ day: "2026-10-02", requests: 1, knownCostUsd: 0.5 }],
    undated: {
      day: null,
      requests: 1,
      knownCostUsd: 0.5,
      costCny: null,
      knownCostCny: 3.5,
      estimatedCostCny: null,
      costCnyPartial: true,
    },
  };
  const view = normalizeShareUsage(payload);
  assert.equal(view.unattributed.keyId, null);
  assert.equal(view.unattributed.tokenUsage.totalTokens, 60);
  assert.equal(view.unattributed.tokenUsage.inputTokens, 50);
  assert.equal(view.unattributed.knownCostUsd, 0.5);
  assert.equal(view.unattributed.costCny, null);
  assert.equal(view.unattributed.knownCostCny, 3.5);
  assert.equal(view.unattributed.estimatedCostCny, null);
  assert.equal(view.unattributed.coverage.costCnyPartial, true);
  assert.equal(view.unattributed.coverage.records, 1);
  assert.equal(view.unattributed.models[0].identity, "test-model");
  assert.equal(view.unattributed.days[0].identity, "2026-10-02");
  assert.equal(view.unattributed.days[0].knownCostUsd, 0.5);
  assert.equal(view.unattributed.undated.knownCostUsd, 0.5);
  assert.equal(view.unattributed.undated.knownCostCny, 3.5);
  assert.equal(view.unattributed.undated.costCny, null);
  assert.equal(view.unattributed.undated.coverage.costCnyPartial, true);
  assert.equal(view.keys.length, normalizeShareUsage(share).keys.length);
  // A payload that carries no split keeps nulls and no rows - never a fabricated
  // complete total, and never a row spread over the keys.
  const legacy = normalizeShareUsage(share).unattributed;
  assert.equal(legacy.tokenUsage.totalTokens, null);
  assert.equal(legacy.coverage.records, null);
  assert.equal(legacy.knownCostUsd, null);
  assert.deepEqual(legacy.models, []);
  assert.deepEqual(legacy.days, []);
  assert.equal(legacy.undated, null);
  assert.equal(normalizeShareUsage(share).keys.length, 1);
});

test("catalog shape errors do not become an empty successful directory", () => {
  assert.throws(() => normalizeCatalog({ catalogs: [] }), ContractError);
  assert.throws(
    () => normalizeCatalog({ catalogs: { um: "login page" } }),
    ContractError
  );
  assert.throws(
    () => normalizeCatalog({ catalogs: { um: [{}] } }),
    ContractError
  );
});

test("latest response guard survives transports that ignore abort", async () => {
  const requests = new LatestRequests();
  const old = deferred();
  const first = requests.run("model", () => old.promise);
  const second = await requests.run("model", async () => "new");
  old.resolve("old");
  assert.deepEqual(second, { state: "current", value: "new" });
  assert.deepEqual(await first, { state: "superseded" });
});

test("cancelled read cannot write back, and unrelated lanes retain their result", async () => {
  const requests = new LatestRequests();
  const gate = deferred();
  const first = requests.run("logs", () => gate.promise);
  requests.cancel("logs");
  assert.deepEqual(await requests.run("share", async () => 0), {
    state: "current",
    value: 0,
  });
  gate.resolve("late");
  assert.deepEqual(await first, { state: "cancelled" });
});

test("a late error from a superseded read is ignored, not displayed on a new model", async () => {
  const requests = new LatestRequests();
  const old = deferred();
  const first = requests.run("model", () => old.promise);
  await requests.run("model", async () => "new");
  old.reject(new Error("old network failure"));
  assert.deepEqual(await first, { state: "superseded" });
});

test("fixture client traverses real submit/poll envelope without fetch or model calls", async () => {
  const transport = new FixtureTransport(scenario());
  const client = new BackendClient(transport, clock());
  assert.equal((await client.readCatalog()).models[0].declaredContext, 131072);
  assert.equal((await client.readContexts())[0].draftVersion, 2);
  assert.equal(
    (await client.readContextEvidence())[0].verifiedTokenFloor,
    4096
  );
  assert.equal(posts(transport).length, 3);
  assert.ok(
    transport.calls.every(
      (call) => call.path.startsWith("/") && call.access === "session"
    )
  );
});

test("unregistered/disabled actions are detected before POST", async () => {
  const fixture = scenario();
  fixture.actions.find(
    (row) => row.action === "model.context_probe_run"
  ).enabled = false;
  const transport = new FixtureTransport(fixture);
  const client = new BackendClient(transport, clock());
  await assert.rejects(
    client.runAction(
      "model.context_probe_run",
      "model",
      {},
      { intent: "probe" }
    ),
    ActionUnavailableError
  );
  assert.equal(posts(transport).length, 0);
});

test("share read errors keep version/auth/transport state separate and never invent zero", async () => {
  for (const [status, state] of [
    [404, "unavailable"],
    [401, "refused"],
    [503, "error"],
  ]) {
    const fixture = scenario();
    fixture.reads["/share-usage"] = { status, body: { error: "read_failure" } };
    const transport = new FixtureTransport(fixture);
    const client = new BackendClient(transport, clock());
    const result = await client.readShareUsage();
    assert.equal(result.state, state);
    assert.equal(result.value, null);
    assert.equal(result.httpStatus, status);
    assert.equal(transport.calls[0].access, "public");
  }
});

test("fetch adapter uses same-origin session/CSRF contract and omits credentials on share usage", async () => {
  const calls = [];
  const transport = createFetchTransport({
    fetch: async (url, init) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    },
  });
  await transport.request({
    method: "POST",
    path: "/operations",
    body: { action: "fixture" },
    access: "session",
  });
  await transport.request({
    method: "GET",
    path: "/share-usage",
    access: "public",
  });
  assert.equal(calls[0].url, "/gw/usagesrv/api/console/operations");
  assert.equal(calls[0].init.credentials, "include");
  assert.equal(calls[0].init.headers["X-Requested-With"], "XMLHttpRequest");
  assert.equal(calls[1].init.credentials, "omit");
  assert.equal(calls[1].init.headers, undefined);
  await assert.rejects(
    transport.request({
      method: "GET",
      path: "https://invalid.example",
      access: "session",
    }),
    ContractError
  );
});

test("failed model refresh preserves last data and marks it stale", async () => {
  let fail = false;
  const sample = {
    readCatalog: async () => {
      if (fail) throw new Error("catalog_unavailable");
      return normalizeCatalog(catalog);
    },
    readContexts: async () => normalizeContexts(contexts),
    readContextEvidence: async () => normalizeContextEvidence(evidence),
  };
  const session = new BackendSession(sample, () => 12);
  assert.equal(await session.refreshModels(), true);
  const before = session.models.value;
  fail = true;
  assert.equal(await session.refreshModels(), false);
  assert.equal(session.models.value, before);
  assert.equal(session.models.stale, true);
  assert.match(session.models.reason, /catalog_unavailable/);
});

test("model refresh discards older results even if the old read ignores cancellation", async () => {
  const old = deferred();
  let generation = 0;
  const sample = {
    readCatalog: async () => {
      if (++generation === 1) return old.promise;
      const fresh = structuredClone(catalog);
      fresh.catalogs.um[0].display_name = "fresh";
      return normalizeCatalog(fresh);
    },
    readContexts: async () => normalizeContexts(contexts),
    readContextEvidence: async () => normalizeContextEvidence(evidence),
  };
  const session = new BackendSession(sample);
  const first = session.refreshModels();
  assert.equal(await session.refreshModels(), true);
  old.resolve(normalizeCatalog(catalog));
  assert.equal(await first, false);
  assert.equal(session.models.value[0].catalog.displayName, "fresh");
});

// Gateway sidebar order preference (Plan-Revision 12): every case below runs on
// an isolated storage double — no real browser storage, DOM, or production API.
function memoryStorage() {
  const map = new Map();
  const writes = [];
  const control = { failRead: false, failWrite: false };
  return {
    control,
    map,
    writes,
    getItem(key) {
      if (control.failRead) throw new Error("storage_denied");
      return map.has(key) ? map.get(key) : null;
    },
    setItem(key, value) {
      if (control.failWrite) throw new Error("quota_exceeded");
      writes.push({ op: "set", key, value });
      map.set(key, value);
    },
    removeItem(key) {
      if (control.failWrite) throw new Error("storage_denied");
      writes.push({ op: "remove", key });
      map.delete(key);
    },
  };
}

test("gateway order reads stay read-only: empty, stored, removed and added gateways", () => {
  const storage = memoryStorage();
  const demo = new GatewayOrderPreferences({ storage, namespace: "demo" });
  assert.equal(demo.storageKey, `${GATEWAY_ORDER_STORAGE_KEY}:demo`);
  assert.deepEqual(demo.read(["gw-a", "gw-b"]), {
    order: ["gw-a", "gw-b"],
    status: "default",
  });
  assert.deepEqual(demo.read([]), { order: [], status: "default" });
  storage.map.set(
    demo.storageKey,
    JSON.stringify({
      version: GATEWAY_ORDER_FORMAT_VERSION,
      order: ["gw-b", "gw-a"],
    })
  );
  assert.deepEqual(demo.read(["gw-a", "gw-b"]), {
    order: ["gw-b", "gw-a"],
    status: "coordinated",
  });
  // Removed IDs are filtered, new gateways append in default order, and a read never writes.
  storage.map.set(
    demo.storageKey,
    JSON.stringify({ version: 1, order: ["gw-b", "gw-gone", "gw-a"] })
  );
  assert.deepEqual(demo.read(["gw-a", "gw-b", "gw-c"]), {
    order: ["gw-b", "gw-a", "gw-c"],
    status: "coordinated",
  });
  assert.deepEqual(demo.read([]), { order: [], status: "coordinated" });
  assert.equal(
    storage.map.get(demo.storageKey),
    JSON.stringify({ version: 1, order: ["gw-b", "gw-gone", "gw-a"] })
  );
  assert.deepEqual(storage.writes, []);
  assert.throws(() => demo.read(["gw-a", "gw-a"]), ContractError);
  assert.throws(() => demo.read(["gw-a", 7]), ContractError);
});

test("corrupt, unknown-version, duplicate and over-bound stored orders reset to default with explicit status", () => {
  const storage = memoryStorage();
  const demo = new GatewayOrderPreferences({ storage, namespace: "demo" });
  const available = ["gw-a", "gw-b"];
  const cases = [
    ["{not json", "reset_corrupt"],
    [JSON.stringify(["gw-b", "gw-a"]), "reset_corrupt"],
    [JSON.stringify({ version: 2, order: ["gw-b", "gw-a"] }), "reset_version"],
    [
      JSON.stringify({ version: "1", order: ["gw-b", "gw-a"] }),
      "reset_version",
    ],
    [JSON.stringify({ version: 2 }), "reset_version"],
    [JSON.stringify({ version: 1 }), "reset_corrupt"],
    [JSON.stringify({ version: 1, order: "gw-b" }), "reset_corrupt"],
    [JSON.stringify({ version: 1, order: ["gw-b", 7] }), "reset_corrupt"],
    [
      JSON.stringify({ version: 1, order: ["gw-a", "gw-a", "gw-b"] }),
      "reset_duplicate",
    ],
    [
      JSON.stringify({
        version: 1,
        order: Array.from(
          { length: MAX_GATEWAY_ORDER_IDS + 1 },
          (_, i) => `gw-${i}`
        ),
      }),
      "reset_corrupt",
    ],
    [JSON.stringify({ version: 1, order: [] }), "default"],
    ["", "default"],
  ];
  for (const [raw, status] of cases) {
    storage.map.set(demo.storageKey, raw);
    assert.deepEqual(demo.read(available), { order: available, status }, raw);
  }
  // An over-bound available set is refused instead of being silently truncated.
  const tooLong = Array.from(
    { length: MAX_GATEWAY_ORDER_IDS + 1 },
    (_, i) => `gw-${i}`
  );
  assert.deepEqual(demo.save(tooLong, tooLong), {
    persisted: false,
    reason: "sequence_too_long",
  });
  assert.deepEqual(storage.writes, []);
});

test("filtered reorder updates visible positions in the complete list and never drops hidden gateways", () => {
  const full = ["gw-a", "gw-b", "gw-c", "gw-d"];
  assert.deepEqual(reorderGatewaySubset(full, ["gw-d", "gw-a", "gw-c"]), [
    "gw-d",
    "gw-b",
    "gw-a",
    "gw-c",
  ]);
  assert.deepEqual(reorderGatewaySubset(full, full), full);
  assert.deepEqual(reorderGatewaySubset(full, []), full);
  assert.deepEqual(reorderGatewaySubset([], []), []);
  assert.throws(
    () => reorderGatewaySubset(full, ["gw-d", "gw-x"]),
    ContractError
  );
  assert.throws(
    () => reorderGatewaySubset(full, ["gw-a", "gw-a"]),
    ContractError
  );
  const storage = memoryStorage();
  const demo = new GatewayOrderPreferences({ storage, namespace: "demo" });
  const merged = reorderGatewaySubset(full, ["gw-d", "gw-a", "gw-c"]);
  assert.deepEqual(demo.save(merged, full), { persisted: true, reason: null });
  assert.deepEqual(JSON.parse(storage.map.get(demo.storageKey)), {
    version: GATEWAY_ORDER_FORMAT_VERSION,
    order: merged,
  });
  // Persisting the visible-only list would delete the hidden gateway, so save refuses it.
  assert.throws(() => demo.save(["gw-d", "gw-a", "gw-c"], full), ContractError);
  // The pinned all-models entry is not a gateway ID and never enters the sequence.
  assert.throws(
    () => demo.save(["__total__", "gw-a"], ["gw-a"]),
    ContractError
  );
  assert.throws(() => demo.save([], []), ContractError);
});

test("storage failure keeps the in-page order authoritative and never pretends to persist", () => {
  const storage = memoryStorage();
  const demo = new GatewayOrderPreferences({ storage, namespace: "demo" });
  storage.control.failRead = true;
  assert.deepEqual(demo.read(["gw-a", "gw-b"]), {
    order: ["gw-a", "gw-b"],
    status: "storage_read_failed",
  });
  assert.deepEqual(storage.writes, []);
  storage.control.failWrite = true;
  assert.deepEqual(demo.save(["gw-b", "gw-a"], ["gw-a", "gw-b"]), {
    persisted: false,
    reason: "storage_write_failed",
  });
  assert.equal(storage.map.has(demo.storageKey), false);
  assert.deepEqual(demo.restoreDefault(), {
    persisted: false,
    reason: "storage_write_failed",
  });
  storage.control.failWrite = false;
  assert.deepEqual(demo.save(["gw-b", "gw-a"], ["gw-a", "gw-b"]), {
    persisted: true,
    reason: null,
  });
  storage.control.failWrite = true;
  assert.deepEqual(demo.restoreDefault(), {
    persisted: false,
    reason: "storage_write_failed",
  });
  assert.deepEqual(JSON.parse(storage.map.get(demo.storageKey)).order, [
    "gw-b",
    "gw-a",
  ]);
});

test("restoring the default removes the stored sequence and reads back as default", () => {
  const storage = memoryStorage();
  const demo = new GatewayOrderPreferences({ storage, namespace: "demo" });
  demo.save(["gw-b", "gw-a"], ["gw-a", "gw-b"]);
  assert.deepEqual(demo.read(["gw-a", "gw-b"]), {
    order: ["gw-b", "gw-a"],
    status: "coordinated",
  });
  assert.deepEqual(demo.restoreDefault(), { persisted: true, reason: null });
  assert.equal(storage.map.has(demo.storageKey), false);
  assert.deepEqual(demo.read(["gw-a", "gw-b"]), {
    order: ["gw-a", "gw-b"],
    status: "default",
  });
  // Restoring an already-absent preference is still a completed default restore.
  assert.deepEqual(demo.restoreDefault(), { persisted: true, reason: null });
});

test("dragging and cancelling never persist: reads and merges stay write-free", () => {
  const storage = memoryStorage();
  const demo = new GatewayOrderPreferences({ storage, namespace: "demo" });
  demo.save(["gw-a", "gw-b"], ["gw-a", "gw-b"]);
  const before = storage.writes.length;
  demo.read(["gw-a", "gw-b", "gw-c"]);
  reorderGatewaySubset(["gw-a", "gw-b", "gw-c"], ["gw-c", "gw-a"]);
  assert.equal(storage.writes.length, before);
});

test("demo and production namespaces stay separate and foreign preferences are never touched", () => {
  const storage = memoryStorage();
  const demo = new GatewayOrderPreferences({ storage, namespace: "demo" });
  const production = new GatewayOrderPreferences({
    storage,
    namespace: "production",
  });
  assert.notEqual(demo.storageKey, production.storageKey);
  demo.save(["gw-b", "gw-a"], ["gw-a", "gw-b"]);
  assert.deepEqual(production.read(["gw-a", "gw-b"]), {
    order: ["gw-a", "gw-b"],
    status: "default",
  });
  assert.deepEqual(demo.read(["gw-a", "gw-b"]), {
    order: ["gw-b", "gw-a"],
    status: "coordinated",
  });
  const appearanceKey = "gateway-console-appearance-v1";
  storage.map.set(appearanceKey, JSON.stringify({ theme: "dark" }));
  storage.map.set(demo.storageKey, "{corrupt");
  assert.deepEqual(demo.read(["gw-a", "gw-b"]), {
    order: ["gw-a", "gw-b"],
    status: "reset_corrupt",
  });
  assert.deepEqual(production.read(["gw-a", "gw-b"]), {
    order: ["gw-a", "gw-b"],
    status: "default",
  });
  demo.restoreDefault();
  assert.equal(
    storage.map.get(appearanceKey),
    JSON.stringify({ theme: "dark" })
  );
  assert.equal(storage.map.has(production.storageKey), false);
  assert.throws(
    () => new GatewayOrderPreferences({ storage, namespace: "" }),
    ContractError
  );
  assert.throws(
    () => new GatewayOrderPreferences({ storage, namespace: "  " }),
    ContractError
  );
});

test("an external update between moves is a complete sequence swap, not a corrupt merge", () => {
  const storage = memoryStorage();
  const first = new GatewayOrderPreferences({
    storage,
    namespace: "production",
  });
  const second = new GatewayOrderPreferences({
    storage,
    namespace: "production",
  });
  first.save(["gw-b", "gw-a"], ["gw-a", "gw-b"]);
  second.save(["gw-c", "gw-a", "gw-b"], ["gw-a", "gw-b", "gw-c"]);
  assert.deepEqual(first.read(["gw-a", "gw-b", "gw-c"]), {
    order: ["gw-c", "gw-a", "gw-b"],
    status: "coordinated",
  });
  assert.deepEqual(JSON.parse(storage.map.get(first.storageKey)), {
    version: GATEWAY_ORDER_FORMAT_VERSION,
    order: ["gw-c", "gw-a", "gw-b"],
  });
  assert.throws(() => first.save(["gw-a"], ["gw-a", "gw-b"]), ContractError);
});

// Rev15 connection info read face: the fixture carries only the chat2api
// absent shape; the sentinel is not a real key and no case reads a real file or
// gateway. The other key states are synthetic bodies built inline, never a live
// read face.
function presentConnectionBody(overrides = {}) {
  return {
    ok: true,
    service: "chat2api",
    display_name: "chat2api",
    port: 8000,
    health_path: "/health",
    health_path_source: "descriptor",
    base_url: "http://127.0.0.1:8000/v1",
    protocol: "openai-compatible",
    protocol_path: "/v1",
    runtime_state: "Running",
    runtime_healthy: true,
    auth: "bearer",
    key_state: "present",
    key_plaintext: FIXTURE_CONNECTION_KEY,
    key_source: "env:CHAT2API_LOCAL_API_KEY",
    key_source_state: "present",
    key_in_use: "per_request",
    reason: "合成夹具：权威环境变量可读且非空",
    evidence: ["合成夹具：进程环境可读"],
    checked_epoch: 1790920800,
    ...overrides,
  };
}

function needsKeyConnectionBody() {
  return presentConnectionBody({
    key_state: "needs_key_unconfigured",
    key_plaintext: null,
    key_source_state: "absent",
    key_in_use: "startup_env",
  });
}

function unreadableConnectionBody() {
  return presentConnectionBody({
    key_state: "unreadable",
    key_plaintext: null,
    key_source_state: "unreadable",
    key_in_use: "config_consumed",
  });
}

function unknownConnectionBody() {
  return presentConnectionBody({
    key_state: "unknown",
    key_plaintext: null,
    key_source_state: "unknown",
    key_in_use: "config_consumed",
  });
}

function connectionResponse(service, body) {
  return { status: 200, body: { ...structuredClone(body), service } };
}

test("connection key states mirror the read face and never degrade to a no-key state", () => {
  assert.deepEqual([...FIXTURE_CONNECTION_STATES], ["absent"]);
  const absent = normalizeGatewayConnection(FIXTURE_CONNECTION_BODIES.absent);
  assert.equal(absent.keyState, "absent");
  assert.equal(absent.auth, "loopback_no_key");
  assert.equal(absent.keySourceState, "absent");
  assert.equal(absent.keyPlaintext, null);
  assert.equal(absent.keyInUse, "startup_env");
  // An out-of-contract state is a contract error, never a silent "no key".
  for (const [field, value] of [
    ["key_state", "auth_free_loopback"],
    ["key_state", null],
    ["auth", "api_key"],
    ["key_source_state", "gone"],
    ["key_in_use", "file_value"],
    ["key_in_use", null],
  ]) {
    const body = structuredClone(FIXTURE_CONNECTION_BODIES.absent);
    body[field] = value;
    assert.throws(() => normalizeGatewayConnection(body), ContractError);
  }
  assert.throws(() => normalizeGatewayConnection({}), ContractError);
});

test("the key-in-use mechanism keeps file value and in-use value apart", () => {
  // Rev15 R32-KEY-ACTIVE: the read face must say how the shown value is
  // consumed; an unknown mechanism stays "unknown" instead of being coerced.
  assert.equal(
    normalizeGatewayConnection(presentConnectionBody()).keyInUse,
    "per_request"
  );
  assert.equal(
    normalizeGatewayConnection(needsKeyConnectionBody()).keyInUse,
    "startup_env"
  );
  assert.equal(
    normalizeGatewayConnection(unreadableConnectionBody()).keyInUse,
    "config_consumed"
  );
  assert.equal(
    normalizeGatewayConnection(unknownConnectionBody()).keyInUse,
    "config_consumed"
  );
});

test("a known key state must name a mechanism and absent stays startup_env", () => {
  // Rev15 R32-KEY-ACTIVE cross-field mirror: a state that carries (or needs)
  // a value always names how the instance consumes it, while `unknown` keeps a
  // mechanism the file source confirmed.
  assert.equal(
    normalizeGatewayConnection(FIXTURE_CONNECTION_BODIES.absent).keyInUse,
    "startup_env"
  );
  for (const value of ["unknown", "per_request", "config_consumed"]) {
    const body = structuredClone(FIXTURE_CONNECTION_BODIES.absent);
    body.key_in_use = value;
    assert.throws(() => normalizeGatewayConnection(body), ContractError);
  }
  for (const state of ["present", "needs_key_unconfigured", "unreadable"]) {
    for (const value of ["unknown", null, "file_value"]) {
      const body = structuredClone(presentConnectionBody());
      body.key_state = state;
      body.key_plaintext =
        state === "present" ? FIXTURE_CONNECTION_KEY : null;
      body.key_in_use = value;
      assert.throws(() => normalizeGatewayConnection(body), ContractError);
    }
  }
  // `unknown` stays honest: the config-unconfirmed read keeps
  // `bearer`/`config_consumed` instead of being coerced into "no key".
  const unknown = normalizeGatewayConnection(unknownConnectionBody());
  assert.equal(unknown.keyState, "unknown");
  assert.equal(unknown.auth, "bearer");
  assert.equal(unknown.keyInUse, "config_consumed");
  assert.equal(unknown.keyPlaintext, null);
});

test("the connection copy helper copies baseUrl/keyPlaintext and reports the real outcome", async () => {
  // Synthetic values only: the clipboard is an injected stub that records what
  // it was asked to write; nothing here touches the DOM or a real clipboard.
  const view = normalizeGatewayConnection(presentConnectionBody());
  const written = [];
  const clipboard = {
    writeText: async (value) => {
      written.push(value);
    },
  };
  assert.deepEqual(
    await copyGatewayConnectionField(view, "baseUrl", clipboard),
    { field: "baseUrl", ok: true, reason: null }
  );
  assert.deepEqual(
    await copyGatewayConnectionField(view, "keyPlaintext", clipboard),
    { field: "keyPlaintext", ok: true, reason: null }
  );
  assert.deepEqual(written, [view.baseUrl, FIXTURE_CONNECTION_KEY]);
  // A field outside the closed set is a caller bug, not a copy.
  await assert.rejects(
    copyGatewayConnectionField(view, "reason", clipboard),
    ContractError
  );
});

test("a refused clipboard write is an explicit failure, never a success", async () => {
  const view = normalizeGatewayConnection(presentConnectionBody());
  const denied = {
    writeText: async () => {
      throw new DOMException("synthetic denial", "NotAllowedError");
    },
  };
  const thrown = {
    writeText: () => {
      throw new Error("synthetic_sync_failure");
    },
  };
  for (const clipboard of [
    denied,
    thrown,
    null,
    undefined,
    { writeText: null },
  ]) {
    const result = await copyGatewayConnectionField(
      view,
      "keyPlaintext",
      clipboard
    );
    assert.equal(result.ok, false);
    assert.equal(result.field, "keyPlaintext");
    assert.ok(result.reason);
    assert.ok(!result.reason.includes(FIXTURE_CONNECTION_KEY));
  }
  // The failure keeps only the error class name, never the copied value.
  assert.equal(
    (await copyGatewayConnectionField(view, "keyPlaintext", denied)).reason,
    "clipboard_write_failed:NotAllowedError"
  );
  assert.equal(
    (await copyGatewayConnectionField(view, "baseUrl", thrown)).reason,
    "clipboard_write_failed"
  );
});

test("copying a state without a plaintext key is refused instead of claiming success", async () => {
  let calls = 0;
  const clipboard = {
    writeText: async () => {
      calls += 1;
    },
  };
  for (const body of [
    FIXTURE_CONNECTION_BODIES.absent,
    needsKeyConnectionBody(),
    unreadableConnectionBody(),
    unknownConnectionBody(),
  ]) {
    const view = normalizeGatewayConnection(body);
    assert.equal(view.keyPlaintext, null);
    assert.deepEqual(
      await copyGatewayConnectionField(view, "keyPlaintext", clipboard),
      { field: "keyPlaintext", ok: false, reason: "no_plaintext_key" }
    );
  }
  // A cleared slot and a missing base URL are explicit refusals too.
  assert.deepEqual(
    await copyGatewayConnectionField(null, "baseUrl", clipboard),
    { field: "baseUrl", ok: false, reason: "no_base_url" }
  );
  assert.deepEqual(
    await copyGatewayConnectionField(null, "keyPlaintext", clipboard),
    { field: "keyPlaintext", ok: false, reason: "no_plaintext_key" }
  );
  // A plaintext key that does not belong to a `present` state is refused.
  const smuggled = {
    ...normalizeGatewayConnection(presentConnectionBody()),
    keyState: "needs_key_unconfigured",
  };
  assert.deepEqual(
    await copyGatewayConnectionField(smuggled, "keyPlaintext", clipboard),
    { field: "keyPlaintext", ok: false, reason: "no_plaintext_key" }
  );
  assert.equal(calls, 0);
});

test("a switched or cleared binding never copies the dropped key or a late response", async () => {
  const gate = deferred();
  const started = deferred();
  const reads = {
    "/gateway-connection?service=chat2api": connectionResponse(
      "chat2api",
      presentConnectionBody()
    ),
    "/gateway-connection?service=chat2api-alt": connectionResponse(
      "chat2api-alt",
      unreadableConnectionBody()
    ),
  };
  const inner = new FixtureTransport(connectionScenario(reads));
  const transport = {
    request: async (request) => {
      if (request.path === "/gateway-connection?service=chat2api") {
        started.resolve();
        await gate.promise; // deliberately ignores abort
        return connectionResponse("chat2api", presentConnectionBody());
      }
      return inner.request(request);
    },
  };
  const written = [];
  const clipboard = {
    writeText: async (value) => {
      written.push(value);
    },
  };
  const binding = new GatewayConnectionBinding(
    new BackendClient(transport, clock())
  );
  const pending = binding.select("chat2api");
  await started.promise;
  const switched = await binding.select("chat2api-alt");
  assert.equal(switched.service, "chat2api-alt");
  assert.equal(switched.result.value.keyState, "unreadable");
  assert.equal(switched.result.value.keyPlaintext, null);
  const refused = await copyGatewayConnectionField(
    switched.result.value,
    "keyPlaintext",
    clipboard
  );
  assert.deepEqual(refused, {
    field: "keyPlaintext",
    ok: false,
    reason: "no_plaintext_key",
  });
  gate.resolve();
  assert.equal(await pending, null);
  assert.equal(binding.current.service, "chat2api-alt");
  assert.equal(binding.current.result.value.keyPlaintext, null);
  assert.equal(written.length, 0);
});

test("a key state and auth pair the read face never produces is refused", () => {
  // The backend pairs absent with loopback_no_key, unknown with unknown or
  // bearer (a readable key file whose config consistency check failed) and
  // every credential-bearing state with bearer. A mismatched pair would read as
  // "no key required", so the normalizer refuses it instead of degrading.
  for (const [keyState, auth] of [
    ["absent", "bearer"],
    ["present", "loopback_no_key"],
    ["needs_key_unconfigured", "unknown"],
    ["unknown", "loopback_no_key"],
    ["unreadable", "loopback_no_key"],
  ]) {
    const body =
      keyState === "absent"
        ? structuredClone(FIXTURE_CONNECTION_BODIES.absent)
        : presentConnectionBody();
    body.key_state = keyState;
    body.auth = auth;
    assert.throws(() => normalizeGatewayConnection(body), ContractError);
  }
});

test("the plaintext connection key lives only in the read result, never in errors", async () => {
  const body = presentConnectionBody();
  const view = normalizeGatewayConnection(body);
  assert.equal(view.keyPlaintext, FIXTURE_CONNECTION_KEY);
  assert.ok(!view.reason.includes(FIXTURE_CONNECTION_KEY));
  assert.ok(
    !view.evidence.some((entry) => entry.includes(FIXTURE_CONNECTION_KEY))
  );
  // A malformed payload that carries the key must not leak it through the error.
  const broken = structuredClone(body);
  broken.checked_epoch = "not-a-number";
  assert.throws(
    () => normalizeGatewayConnection(broken),
    (error) =>
      error instanceof ContractError &&
      !error.message.includes(FIXTURE_CONNECTION_KEY)
  );
  // A key must not ride along with a state that says there is no key.
  assert.throws(
    () =>
      normalizeGatewayConnection({
        ...body,
        key_state: "needs_key_unconfigured",
      }),
    ContractError
  );
  // A present state without a key is a broken contract, not a silent null.
  assert.throws(
    () => normalizeGatewayConnection({ ...body, key_plaintext: null }),
    ContractError
  );
  const reads = {
    "/gateway-connection?service=chat2api": {
      status: 400,
      body: { error: "service_out_of_scope" },
    },
  };
  const result = await connectionClient(reads).readGatewayConnection(
    "chat2api"
  );
  assert.equal(result.state, "error");
  assert.equal(result.value, null);
  assert.ok(!result.reason.includes(FIXTURE_CONNECTION_KEY));
});

test("the connection read is one narrow GET bound to the requested service", async () => {
  const reads = {
    "/gateway-connection?service=chat2api": connectionResponse(
      "chat2api",
      presentConnectionBody()
    ),
  };
  const transport = new FixtureTransport(connectionScenario(reads));
  const client = new BackendClient(transport, clock());
  const result = await client.readGatewayConnection("chat2api");
  assert.equal(result.state, "ready");
  assert.equal(result.value.service, "chat2api");
  assert.equal(result.value.baseUrl, "http://127.0.0.1:8000/v1");
  assert.equal(result.value.keyState, "present");
  assert.equal(result.value.keyPlaintext, FIXTURE_CONNECTION_KEY);
  assert.equal(result.value.alternateProtocols.length, 0);
  assert.equal(transport.calls.length, 1);
  assert.equal(transport.calls[0].method, "GET");
  assert.equal(transport.calls[0].path, "/gateway-connection?service=chat2api");
  assert.equal(transport.calls[0].access, "session");
  await assert.rejects(client.readGatewayConnection("  "), ContractError);
  const failing = {
    request: async () => {
      throw new Error("request_timeout");
    },
  };
  const timedOut = await new BackendClient(
    failing,
    clock()
  ).readGatewayConnection("chat2api");
  assert.equal(timedOut.state, "error");
  assert.equal(timedOut.value, null);
  assert.equal(timedOut.reason, "request_timeout");
});

test("switching gateways clears the previous key and a late response is never installed", async () => {
  const gate = deferred();
  const started = deferred();
  const reads = {
    "/gateway-connection?service=chat2api": connectionResponse(
      "chat2api",
      presentConnectionBody()
    ),
    "/gateway-connection?service=chat2api-alt": connectionResponse(
      "chat2api-alt",
      unreadableConnectionBody()
    ),
  };
  const inner = new FixtureTransport(connectionScenario(reads));
  const transport = {
    request: async (request) => {
      if (request.path === "/gateway-connection?service=chat2api") {
        started.resolve();
        await gate.promise; // deliberately ignores abort
        return connectionResponse("chat2api", presentConnectionBody());
      }
      return inner.request(request);
    },
  };
  const binding = new GatewayConnectionBinding(
    new BackendClient(transport, clock())
  );
  const first = binding.select("chat2api");
  await started.promise;
  assert.equal(binding.current, null);
  const second = await binding.select("chat2api-alt");
  assert.equal(second.service, "chat2api-alt");
  assert.equal(second.result.state, "ready");
  assert.equal(second.result.value.keyState, "unreadable");
  assert.equal(second.result.value.keyPlaintext, null);
  gate.resolve();
  assert.equal(await first, null);
  assert.equal(binding.current.service, "chat2api-alt");
  assert.equal(binding.current.result.value.keyPlaintext, null);
});

test("clearing a binding drops the key immediately and refuses a later response", async () => {
  const gate = deferred();
  const started = deferred();
  const reads = {
    "/gateway-connection?service=chat2api": connectionResponse(
      "chat2api",
      presentConnectionBody()
    ),
  };
  const inner = new FixtureTransport(connectionScenario(reads));
  const transport = {
    request: async (request) => {
      if (request.path === "/gateway-connection?service=chat2api") {
        started.resolve();
        await gate.promise;
        return connectionResponse("chat2api", presentConnectionBody());
      }
      return inner.request(request);
    },
  };
  const binding = new GatewayConnectionBinding(
    new BackendClient(transport, clock())
  );
  const pending = binding.select("chat2api");
  await started.promise;
  binding.clear();
  assert.equal(binding.current, null);
  gate.resolve();
  assert.equal(await pending, null);
  assert.equal(binding.current, null);
});

test("a failed read for the new gateway never retains the previous key", async () => {
  const reads = gatewayConnectionFixtureReads();
  reads["/gateway-connection?service=chat2api-alt"] = {
    status: 503,
    body: { error: "runtime status unavailable" },
  };
  reads["/gateway-connection?service=chat2api"] = connectionResponse(
    "chat2api",
    presentConnectionBody()
  );
  const binding = new GatewayConnectionBinding(connectionClient(reads));
  const first = await binding.select("chat2api");
  assert.equal(first.result.value.keyPlaintext, FIXTURE_CONNECTION_KEY);
  const second = await binding.select("chat2api-alt");
  assert.equal(second.result.state, "error");
  assert.equal(second.result.value, null);
  assert.equal(binding.current.service, "chat2api-alt");
  assert.equal(binding.current.result.value, null);
});

test("a response bound to another service identity is refused, not installed", async () => {
  const reads = {
    "/gateway-connection?service=chat2api": connectionResponse(
      "chat2api-alt",
      presentConnectionBody()
    ),
  };
  const binding = new GatewayConnectionBinding(connectionClient(reads));
  const view = await binding.select("chat2api");
  assert.equal(view.result.state, "error");
  assert.equal(view.result.value, null);
  assert.match(view.result.reason, /different service/);
  assert.equal(binding.current.result.value, null);
});

// §10.1 两侧调宽（2026-10-10）：日志来源侧栏 / 模型详情栏的宽度偏好。存储键、最小 /
// 最大宽度与设计默认值都在 pane-width.ts 里声明一次；下面每一例只读那个契约。
test("pane widths read the design bounds: default, clamped, corrupt and separate namespaces", () => {
  const storage = memoryStorage();
  const demo = new PaneWidthPreferences({ storage, namespace: "demo" });
  assert.equal(demo.storageKey, `${PANE_WIDTH_STORAGE_KEY}:demo`);
  assert.deepEqual(demo.read(), { widths: {}, status: "default" });
  assert.deepEqual(storage.writes, []);
  // 一次完整写回：两个分隔条的宽度一起写，互不覆盖。
  assert.deepEqual(demo.save({ "gw-side": 340, "model-detail": 380 }), {
    persisted: true,
    reason: null,
  });
  assert.deepEqual(JSON.parse(storage.map.get(demo.storageKey)), {
    version: PANE_WIDTH_FORMAT_VERSION,
    widths: { "gw-side": 340, "model-detail": 380 },
  });
  assert.deepEqual(demo.read(), {
    widths: { "gw-side": 340, "model-detail": 380 },
    status: "stored",
  });
  // 越界值读回时夹到设计范围（陈旧载荷 / 手改值都不能产生不可用的列宽）。
  storage.map.set(
    demo.storageKey,
    JSON.stringify({
      version: 1,
      widths: { "gw-side": 9000, "model-detail": 1 },
    })
  );
  const side = paneWidthSpec("gw-side");
  const detail = paneWidthSpec("model-detail");
  assert.deepEqual(demo.read().widths, {
    "gw-side": side.max,
    "model-detail": detail.min,
  });
  assert.equal(clampPaneWidth(side, Number.NaN), side.fallback);
  assert.equal(clampPaneWidth(side, -1), side.min);
  assert.equal(clampPaneWidth(side, 10_000), side.max);
  assert.ok(side.min < side.fallback && side.fallback < side.max);
  assert.notEqual(side.cssVar, detail.cssVar);
  // 损坏 / 未知版本 / 未知分隔条 / 非数字 / 超上限都回默认，并写清是哪一种。
  const cases = [
    ["{not json", "reset_corrupt"],
    [
      JSON.stringify({ version: 2, widths: { "gw-side": 340 } }),
      "reset_version",
    ],
    [JSON.stringify({ version: 1 }), "reset_corrupt"],
    [JSON.stringify({ version: 1, widths: [] }), "reset_corrupt"],
    [
      JSON.stringify({ version: 1, widths: { "gw-side": "wide" } }),
      "reset_corrupt",
    ],
    [
      JSON.stringify({
        version: 1,
        widths: { "gw-side": 340, "gw-other": 340 },
      }),
      "reset_corrupt",
    ],
    [
      JSON.stringify({
        version: 1,
        widths: Object.fromEntries(
          Array.from({ length: MAX_PANE_WIDTHS + 1 }, (_, i) => [
            `pane-${i}`,
            300,
          ])
        ),
      }),
      "reset_corrupt",
    ],
    [JSON.stringify({ version: 1, widths: {} }), "default"],
    ["", "default"],
  ];
  for (const [raw, status] of cases) {
    storage.map.set(demo.storageKey, raw);
    assert.deepEqual(demo.read(), { widths: {}, status }, raw);
  }
  // 只写过一侧时另一侧读出缺席：调用方用设计默认值，而不是 0。
  storage.map.set(
    demo.storageKey,
    JSON.stringify({ version: 1, widths: { "gw-side": 340 } })
  );
  assert.deepEqual(Object.keys(demo.read().widths), ["gw-side"]);
  // 未知分隔条 / 空表拒绝写，页面宽度仍为准；越界值夹进范围再写。
  assert.deepEqual(demo.save({ "gw-side": 340, "gw-nope": 300 }), {
    persisted: false,
    reason: "unknown_split",
  });
  assert.deepEqual(demo.save({}), { persisted: false, reason: "map_too_long" });
  assert.deepEqual(
    demo.save(
      Object.fromEntries(
        Array.from({ length: MAX_PANE_WIDTHS + 1 }, (_, i) => [
          `pane-${i}`,
          300,
        ])
      )
    ),
    { persisted: false, reason: "map_too_long" }
  );
  assert.deepEqual(demo.save({ "gw-side": 9000 }), {
    persisted: true,
    reason: null,
  });
  assert.deepEqual(JSON.parse(storage.map.get(demo.storageKey)).widths, {
    "gw-side": side.max,
  });
  // 存储读 / 写失败都不伪装成功，也不碰别的偏好键。
  const appearanceKey = "gateway-console-appearance-v1";
  storage.map.set(appearanceKey, JSON.stringify({ theme: "dark" }));
  storage.control.failRead = true;
  assert.deepEqual(demo.read(), { widths: {}, status: "storage_read_failed" });
  storage.control.failRead = false;
  storage.control.failWrite = true;
  assert.deepEqual(demo.save({ "gw-side": 340 }), {
    persisted: false,
    reason: "storage_write_failed",
  });
  assert.deepEqual(demo.restoreDefault(), {
    persisted: false,
    reason: "storage_write_failed",
  });
  // Demo 与生产命名空间分开，读绝不写。
  storage.control.failWrite = false;
  const production = new PaneWidthPreferences({
    storage,
    namespace: "production",
  });
  assert.notEqual(demo.storageKey, production.storageKey);
  assert.deepEqual(production.read(), { widths: {}, status: "default" });
  demo.save({ "gw-side": 340 });
  assert.deepEqual(production.read(), { widths: {}, status: "default" });
  assert.equal(
    storage.map.get(appearanceKey),
    JSON.stringify({ theme: "dark" })
  );
  assert.deepEqual(demo.restoreDefault(), { persisted: true, reason: null });
  assert.equal(storage.map.has(demo.storageKey), false);
  assert.equal(storage.map.has(production.storageKey), false);
  assert.throws(
    () => new PaneWidthPreferences({ storage, namespace: "" }),
    ContractError
  );
  assert.throws(
    () => new PaneWidthPreferences({ storage, namespace: "  " }),
    ContractError
  );
  assert.throws(() => paneWidthSpec("no-such-split"), ContractError);
});

test("separator movement follows the pane edge and clamps at its bounds", () => {
  const left = paneWidthSpec("gw-side"),
    right = paneWidthSpec("model-detail");
  assert.equal(paneWidthAfterMove(left, 300, 40), 340);
  assert.equal(paneWidthAfterMove(right, 320, -40), 360);
  assert.equal(paneWidthAfterMove(right, 320, 40), 280);
  assert.equal(paneWidthAfterMove(right, 320, -9999), right.max);
  assert.equal(paneWidthAfterMove(left, 300, -9999), left.min);
});
