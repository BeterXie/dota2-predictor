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

function jsonResponse(payload) {
  return new Response(JSON.stringify(payload), {
    status: 200,
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
    debugLevel: "errors",
  });

  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    if (url.endsWith("/v1/status")) return jsonResponse({protocol_version: 1});
    const events = JSON.parse(init.body);
    return jsonResponse({
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
      onAlarm: {addListener: (listener) => alarmListeners.push(listener)},
      async create(name, info) { alarms.push({name, info}); },
      async clear() { return true; },
    },
    tabs: {
      async query() { return [{id: 7}]; },
      async sendMessage(tabId, message) { tabMessages.push({tabId, message}); },
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
    debugLevel: "errors",
  });
  assert.equal(Object.hasOwn(normalized, "paired"), false);
  assert.equal(Object.hasOwn(normalized, "secret"), false);

  const send = (message, sender = {url: "https://www.ray086.com/esports"}) => new Promise((resolve) => {
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
    source_origin: "https://cfinfo.365raylinks.com",
    event,
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  let status = await send({action: "raybet.popup.getStatus"});
  assert.equal(status.queue.length, 0);
  assert.deepEqual(status.recognizedMatches, ["42"]);
  assert.equal(status.companion.reachable, true);
  assert.equal(status.companion.connected, true);
  assert.equal(status.companion.lastError, null);

  await send({
    action: "raybet.options.save",
    enabledDomains: {ray086: true, raylinks: false},
    debugLevel: "errors",
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
