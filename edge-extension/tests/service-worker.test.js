import test from "node:test";
import assert from "node:assert/strict";

function storageArea() {
  const values = new Map();
  const access = [];
  return {
    values,
    access,
    async get(key) {
      if (typeof key === "string") return {[key]: values.get(key)};
      return Object.fromEntries(values);
    },
    async set(items) {
      for (const [key, value] of Object.entries(items)) values.set(key, structuredClone(value));
    },
    async setAccessLevel(value) { access.push(value); },
  };
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {"Content-Type": "application/json"},
  });
}

test("service worker connects directly, cleans legacy secrets, and honors pause", async () => {
  const local = storageArea();
  const session = storageArea();
  const messageListeners = [];
  const alarmListeners = [];
  const alarms = [];
  const tabMessages = [];
  local.values.set("raybetMonitorConfig", {
    paired: true,
    secret: "legacy-secret-must-be-removed",
    enabledDomains: {ray086: true, raylinks: true},
  });

  const previousFetch = globalThis.fetch;
  let eventOutcome = "accepted";
  globalThis.fetch = async (url, init) => {
    if (url.endsWith("/v1/status")) return jsonResponse({protocol_version: 1});
    const events = JSON.parse(init.body);
    return jsonResponse({
      protocol_version: 1,
      results: events.map((item) => ({event_id: item.event_id, status: eventOutcome})),
    });
  };
  globalThis.chrome = {
    storage: {local, session},
    runtime: {
      onMessage: {addListener: (listener) => messageListeners.push(listener)},
      onInstalled: {addListener: () => undefined},
      onStartup: {addListener: () => undefined},
    },
    alarms: {
      onAlarm: {addListener: (listener) => alarmListeners.push(listener)},
      async create(name, info) { alarms.push({name, info}); },
      async clear() { return true; },
    },
    tabs: {
      async query() {
        return [{id: 7, url: "https://ray086.com/esports", status: "complete"}];
      },
      async sendMessage(tabId, message) {
        tabMessages.push({tabId, message});
        if (message.action === "raybet.capture.probe") {
          return {
            bridgeInitializedAt: new Date(Date.now() - 5000).toISOString(),
            configLoaded: true,
            hookSeen: true,
            hookSeenAt: new Date(Date.now() - 4900).toISOString(),
            transports: {fetch: 1, xhr: 0, websocket: 0},
            lastObservedAt: new Date(Date.now() - 100).toISOString(),
            acceptedCount: 1,
            lastAcceptedAt: new Date(Date.now() - 100).toISOString(),
          };
        }
        return undefined;
      },
    },
  };

  await import(`../src/service-worker.js?test=${Date.now()}`);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(messageListeners.length, 1);
  assert.equal(alarmListeners.length, 1);
  assert.deepEqual(local.access, [{accessLevel: "TRUSTED_CONTEXTS"}]);
  assert.deepEqual(session.access, [{accessLevel: "TRUSTED_CONTEXTS"}]);
  const normalized = local.values.get("raybetMonitorConfig");
  assert.deepEqual(normalized, {
    enabledDomains: {ray086: true, raylinks: true},
  });
  assert.equal(Object.hasOwn(normalized, "paired"), false);
  assert.equal(Object.hasOwn(normalized, "secret"), false);

  const send = (message, sender = {url: "https://ray086.com/esports"}) => new Promise((resolve) => {
    const keepAlive = messageListeners[0](message, sender, resolve);
    assert.equal(keepAlive, true);
  });

  const config = await send({action: "raybet.capture.getConfig"});
  assert.equal(config.enabled, true);
  assert.deepEqual(config.enabledDomains, {ray086: true, raylinks: true});
  assert.match(config.captureSessionId, /^[0-9a-f]{32}$/);

  const event = {
    event_id: "1".repeat(64),
    event_type: "odds",
    captured_at_utc: "2026-07-13T00:00:00.000Z",
    raybet_match_id: "42",
    payload: {},
  };
  await send({
    action: "raybet.capture.event",
    source_origin: "https://iminfo.esportsworldlink.com",
    event,
  });
  await send({
    action: "raybet.capture.diagnostic",
    kind: "hook_initialized",
    frame_context: "top",
    observed_at_utc: "2026-07-14T12:00:00.000Z",
  });
  await send({
    action: "raybet.capture.diagnostic",
    kind: "bridge_ready",
    frame_context: "top",
    config_loaded: true,
    observed_at_utc: "2026-07-14T12:00:00.500Z",
  });
  await send({
    action: "raybet.capture.diagnostic",
    kind: "transport_observed",
    frame_context: "top",
    transport: "fetch",
    source_host: "iminfo.esportsworldlink.com",
    source_path: "/v2/odds",
    amount: 7,
    observed_at_utc: "2026-07-14T12:00:01.000Z",
  });
  await send({
    action: "raybet.capture.diagnostic",
    kind: "classification",
    outcome: "ignored",
    reason: "non_dota",
    observed_at_utc: "2026-07-14T12:00:02.000Z",
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  let status = await send({action: "raybet.popup.getStatus"});
  assert.equal(status.queue.length, 0);
  assert.deepEqual(status.recognizedMatches, ["42"]);
  assert.equal(status.companion.reachable, true);
  assert.equal(status.companion.connected, true);
  assert.equal(status.companion.lastError, null);
  assert.equal(status.captureStatus, "capturing");
  assert.equal(status.statusReason, "acknowledgements_current");
  assert.equal(status.statusSignals.queueCount, 0);
  assert.equal(status.statusSignals.activePageSupported, true);
  assert.equal(status.diagnostics.initialization.hook.top, 1);
  assert.equal(status.diagnostics.bridgeConfigLoaded, true);
  assert.equal(status.diagnostics.transports.fetch, 7);
  assert.deepEqual(status.diagnostics.lastObserved, {
    transport: "fetch",
    sourceHost: "iminfo.esportsworldlink.com",
    sourcePath: "/v2/odds",
    observedAt: "2026-07-14T12:00:01.000Z",
    frameContext: "top",
  });
  assert.equal(status.diagnostics.classification.ignoredReasons.non_dota, 1);

  eventOutcome = "rejected";
  await send({
    action: "raybet.capture.event",
    source_origin: "https://www.ray086.com",
    event: {...event, event_id: "6".repeat(64)},
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  status = await send({action: "raybet.popup.getStatus"});
  assert.equal(status.counters.rejected, 1);
  assert.equal(status.captureStatus, "degraded");
  assert.equal(status.statusReason, "events_rejected");
  eventOutcome = "accepted";

  const invalidDiagnostic = await send({
    action: "raybet.capture.diagnostic",
    kind: "transport_observed",
    transport: "fetch",
    source_host: "iminfo.esportsworldlink.com",
    source_path: "/v2/odds?token=forbidden",
  });
  assert.deepEqual(invalidDiagnostic, {accepted: false, reason: "invalid_diagnostic"});

  await send({
    action: "raybet.options.save",
    enabledDomains: {ray086: true, raylinks: false},
  });
  assert.deepEqual(tabMessages.at(-1).message.enabledDomains, {ray086: true, raylinks: false});
  const disabled = await send({
    action: "raybet.capture.event",
    source_origin: "https://cfinfo.365raylinks.com",
    event: {...event, event_id: "2".repeat(64)},
  });
  assert.deepEqual(disabled, {accepted: false, reason: "disabled_source"});

  await send({action: "raybet.popup.setPaused", paused: true});
  await send({
    action: "raybet.capture.event",
    source_origin: "https://www.ray086.com",
    event: {...event, event_id: "3".repeat(64)},
  });
  status = await send({action: "raybet.popup.getStatus"});
  assert.equal(status.state, "paused");
  assert.equal(status.queue.length, 0);

  let rejectDelivery;
  let markDeliveryStarted;
  const deliveryStarted = new Promise((resolve) => { markDeliveryStarted = resolve; });
  globalThis.fetch = async (url) => {
    if (url.endsWith("/v1/status")) return jsonResponse({protocol_version: 1});
    markDeliveryStarted();
    return new Promise((_, reject) => { rejectDelivery = reject; });
  };
  await send({action: "raybet.popup.setPaused", paused: false});
  await send({
    action: "raybet.capture.event",
    source_origin: "https://www.ray086.com",
    event: {...event, event_id: "4".repeat(64)},
  });
  await deliveryStarted;
  await send({action: "raybet.popup.setPaused", paused: true});
  rejectDelivery(new TypeError("network unavailable"));
  await new Promise((resolve) => setTimeout(resolve, 20));
  const persisted = session.values.get("raybetMonitorSession");
  assert.equal(persisted.paused, true);
  assert.equal(persisted.state, "paused");
  assert.equal(persisted.queue.length, 1);
  const retryWait = Math.max(0, persisted.nextRetryAt - Date.now()) + 100;
  await new Promise((resolve) => setTimeout(resolve, retryWait));

  globalThis.fetch = previousFetch;
  delete globalThis.chrome;
});

test("service worker keeps an incompatible companion disconnected", async () => {
  const local = storageArea();
  const session = storageArea();
  const messageListeners = [];
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    if (url.endsWith("/v1/status")) return jsonResponse({protocol_version: 2});
    const events = JSON.parse(init.body);
    return jsonResponse({
      protocol_version: 2,
      results: events.map((event) => ({event_id: event.event_id, status: "accepted"})),
    });
  };
  globalThis.chrome = {
    storage: {local, session},
    runtime: {
      onMessage: {addListener: (listener) => messageListeners.push(listener)},
      onInstalled: {addListener: () => undefined},
      onStartup: {addListener: () => undefined},
    },
    alarms: {
      onAlarm: {addListener: () => undefined},
      async create() { return undefined; },
      async clear() { return true; },
    },
    tabs: {
      async query() { return []; },
      async sendMessage() { return undefined; },
    },
  };

  await import(`../src/service-worker.js?protocol-test=${Date.now()}`);
  await new Promise((resolve) => setTimeout(resolve, 20));
  const send = (message, sender = {url: "https://ray086.com/esports"}) => new Promise((resolve) => {
    messageListeners[0](message, sender, resolve);
  });
  await send({
    action: "raybet.capture.event",
    source_origin: "https://ray086.com",
    event: {
      event_id: "5".repeat(64),
      event_type: "odds",
      captured_at_utc: "2026-07-16T00:00:00.000Z",
      raybet_match_id: "42",
      payload: {},
    },
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  const failedState = session.values.get("raybetMonitorSession");
  assert.equal(failedState.companion.connected, false);
  assert.equal(failedState.queue.length, 1);

  const status = await send({action: "raybet.popup.getStatus"}, {});
  assert.equal(status.remote, null);
  assert.deepEqual(status.companion, {
    reachable: true,
    connected: false,
    lastError: "unsupported_protocol_version",
  });

  await send({action: "raybet.popup.setPaused", paused: true}, {});

  globalThis.fetch = previousFetch;
  delete globalThis.chrome;
});

test("popup status derives current-page readiness with deterministic priority", async () => {
  const local = storageArea();
  const session = storageArea();
  const messageListeners = [];
  let companionMode = "ready";
  let activeTab = {id: 9, url: "https://ray086.com/esports", status: "complete"};
  let probe;
  const healthyProbe = (overrides = {}) => ({
    bridgeInitializedAt: new Date(Date.now() - 5000).toISOString(),
    configLoaded: true,
    hookSeen: true,
    hookSeenAt: new Date(Date.now() - 4900).toISOString(),
    transports: {fetch: 0, xhr: 0, websocket: 0},
    lastObservedAt: null,
    acceptedCount: 0,
    lastAcceptedAt: null,
    ...overrides,
  });
  probe = healthyProbe();

  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    if (!url.endsWith("/v1/status")) throw new Error("unexpected event delivery");
    if (companionMode === "offline") throw new TypeError("network unavailable");
    if (companionMode === "protocol") return jsonResponse({protocol_version: 2});
    if (companionMode === "database") {
      return jsonResponse({protocol_version: 1, database_health: "unavailable"});
    }
    if (companionMode === "rate_limited") {
      return jsonResponse({code: "rate_limited", detail: "request rejected"}, 429);
    }
    return jsonResponse({protocol_version: 1, database_health: "ok"});
  };
  globalThis.chrome = {
    storage: {local, session},
    runtime: {
      onMessage: {addListener: (listener) => messageListeners.push(listener)},
      onInstalled: {addListener: () => undefined},
      onStartup: {addListener: () => undefined},
    },
    alarms: {
      onAlarm: {addListener: () => undefined},
      async create() { return undefined; },
      async clear() { return true; },
    },
    tabs: {
      async query() { return activeTab ? [activeTab] : []; },
      async sendMessage(_tabId, message) {
        if (message.action !== "raybet.capture.probe") return undefined;
        if (probe instanceof Error) throw probe;
        return structuredClone(probe);
      },
    },
  };

  await import(`../src/service-worker.js?readiness-test=${Date.now()}`);
  await new Promise((resolve) => setTimeout(resolve, 20));
  const send = (message, sender = {}) => new Promise((resolve) => {
    messageListeners[0](message, sender, resolve);
  });
  const original = structuredClone(session.values.get("raybetMonitorSession"));
  const queuedEvent = (index, payload = undefined) => ({
    event_id: String(index).padStart(64, "0"),
    event_type: "odds",
    captured_at_utc: "2026-07-21T00:00:00.000Z",
    ...(payload === undefined ? {} : {payload}),
  });
  const eventHighWater = Array.from({length: 800}, (_, index) => queuedEvent(index));
  const byteHighWater = [queuedEvent(1, "x".repeat(Math.ceil(5 * 1024 * 1024 * 0.8)))];

  const cases = [
    {
      name: "loss outranks a full queue",
      state: {queue: eventHighWater, counters: {dropped: 1}},
      mode: "offline",
      expected: ["degraded", "events_dropped"],
      issues: ["events_dropped", "queue_event_high_water", "status_probe_network_error"],
    },
    {
      name: "event high-water outranks an outage",
      state: {queue: eventHighWater},
      mode: "offline",
      expected: ["backpressure", "queue_event_high_water"],
      issues: ["queue_event_high_water", "status_probe_network_error"],
    },
    {
      name: "byte high-water is backpressure",
      state: {queue: byteHighWater},
      expected: ["backpressure", "queue_byte_high_water"],
    },
    {
      name: "missing hook outranks an outage",
      mode: "offline",
      pageProbe: healthyProbe({
        bridgeInitializedAt: new Date(Date.now() - 3000).toISOString(),
        hookSeen: false,
        hookSeenAt: null,
      }),
      expected: ["page_hook_missing", "main_hook_not_seen"],
      issues: ["main_hook_not_seen", "status_probe_network_error"],
    },
    {
      name: "hook grace remains waiting",
      pageProbe: healthyProbe({
        bridgeInitializedAt: new Date(Date.now() - 1000).toISOString(),
        hookSeen: false,
        hookSeenAt: null,
      }),
      expected: ["waiting_for_traffic", "hook_initializing"],
    },
    {
      name: "companion network failure is offline",
      mode: "offline",
      expected: ["companion_offline", "status_probe_network_error"],
    },
    {
      name: "reachable protocol mismatch is degraded",
      mode: "protocol",
      expected: ["degraded", "unsupported_protocol_version"],
    },
    {
      name: "database status failure is degraded",
      mode: "database",
      expected: ["degraded", "database_unavailable"],
    },
    {
      name: "companion rate limit is reconnecting",
      mode: "rate_limited",
      expected: ["reconnecting", "companion_rate_limited"],
    },
    {
      name: "small queued retry is reconnecting",
      state: {queue: [queuedEvent(1)], retryAttempt: 1, nextRetryAt: Date.now() + 1000},
      pageProbe: healthyProbe({acceptedCount: 1, lastAcceptedAt: new Date().toISOString()}),
      expected: ["reconnecting", "retry_scheduled"],
    },
    {
      name: "healthy hook without transport waits",
      expected: ["waiting_for_traffic", "no_transport_observed"],
    },
    {
      name: "non-Dota transport still waits",
      pageProbe: healthyProbe({
        transports: {fetch: 2, xhr: 0, websocket: 0},
        lastObservedAt: new Date().toISOString(),
      }),
      expected: ["waiting_for_traffic", "no_dota_event_accepted"],
    },
    {
      name: "accepted current-page traffic captures",
      pageProbe: healthyProbe({
        transports: {fetch: 1, xhr: 0, websocket: 0},
        lastObservedAt: new Date().toISOString(),
        acceptedCount: 1,
        lastAcceptedAt: new Date().toISOString(),
      }),
      expected: ["capturing", "acknowledgements_current"],
    },
  ];

  for (const item of cases) {
    companionMode = item.mode || "ready";
    activeTab = {id: 9, url: "https://ray086.com/esports", status: "complete"};
    probe = item.pageProbe || healthyProbe();
    const next = structuredClone(original);
    Object.assign(next, item.state || {});
    next.counters = {...original.counters, ...(item.state?.counters || {})};
    session.values.set("raybetMonitorSession", next);
    const status = await send({action: "raybet.popup.getStatus"});
    assert.equal(status.captureStatus, item.expected[0], item.name);
    assert.equal(status.statusReason, item.expected[1], item.name);
    assert.ok(status.statusSignals.issues.includes(item.expected[1]), item.name);
    for (const issue of item.issues || []) {
      assert.ok(status.statusSignals.issues.includes(issue), `${item.name}: ${issue}`);
    }
  }

  companionMode = "ready";
  probe = new Error("Receiving end does not exist");
  activeTab = {id: 9, url: "https://ray086.com/esports", status: "loading"};
  session.values.set("raybetMonitorSession", structuredClone(original));
  let status = await send({action: "raybet.popup.getStatus"});
  assert.equal(status.captureStatus, "waiting_for_traffic");
  assert.equal(status.statusReason, "page_loading");

  activeTab = {id: 9, url: "edge://extensions", status: "complete"};
  status = await send({action: "raybet.popup.getStatus"});
  assert.equal(status.captureStatus, "unsupported_page");
  assert.equal(status.state, original.state);

  const paused = structuredClone(original);
  paused.paused = true;
  paused.state = "paused";
  session.values.set("raybetMonitorSession", paused);
  status = await send({action: "raybet.popup.getStatus"});
  assert.equal(status.captureStatus, "paused");
  assert.equal(status.state, "paused");

  globalThis.fetch = previousFetch;
  delete globalThis.chrome;
});
