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

test("service worker serializes session queue and honors pause", async () => {
  const local = storageArea();
  const session = storageArea();
  const messageListeners = [];
  const alarmListeners = [];
  const alarms = [];
  const tabMessages = [];
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
  let status = await send({action: "raybet.popup.getStatus"});
  assert.equal(status.queue.length, 1);
  assert.deepEqual(status.recognizedMatches, ["42"]);

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
  status = await send({action: "raybet.popup.getStatus"});
  assert.equal(status.queue.length, 1);

  await send({action: "raybet.popup.setPaused", paused: true});
  await send({
    action: "raybet.capture.event",
    source_origin: "https://www.ray086.com",
    event: {...event, event_id: "3".repeat(64)},
  });
  status = await send({action: "raybet.popup.getStatus"});
  assert.equal(status.state, "paused");
  assert.equal(status.queue.length, 1);

  const previousFetch = globalThis.fetch;
  const storedConfig = local.values.get("raybetMonitorConfig");
  local.values.set("raybetMonitorConfig", {
    ...storedConfig,
    paired: true,
    secret: btoa(String.fromCharCode(...new Uint8Array(32).fill(1))),
  });
  globalThis.fetch = async () => new Response(JSON.stringify({protocol_version: 1}), {
    status: 200,
    headers: {"Content-Type": "application/json"},
  });
  status = await send({action: "raybet.popup.getStatus"});
  assert.equal(status.companion.reachable, true);
  assert.equal(status.companion.authenticated, true);
  assert.equal(status.companion.lastError, null);
  globalThis.fetch = previousFetch;

  let rejectDelivery;
  let markDeliveryStarted;
  const deliveryStarted = new Promise((resolve) => { markDeliveryStarted = resolve; });
  globalThis.fetch = async () => {
    markDeliveryStarted();
    return new Promise((_, reject) => { rejectDelivery = reject; });
  };
  await send({action: "raybet.popup.setPaused", paused: false});
  await deliveryStarted;
  await send({action: "raybet.popup.setPaused", paused: true});
  rejectDelivery(new TypeError("network unavailable"));
  await new Promise((resolve) => setTimeout(resolve, 20));
  const persisted = session.values.get("raybetMonitorSession");
  assert.equal(persisted.paused, true);
  assert.equal(persisted.state, "paused");
  globalThis.fetch = previousFetch;
  const retryWait = Math.max(0, persisted.nextRetryAt - Date.now()) + 100;
  await new Promise((resolve) => setTimeout(resolve, retryWait));

  delete globalThis.chrome;
});
