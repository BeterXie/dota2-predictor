import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {dirname, join} from "node:path";
import {fileURLToPath} from "node:url";
import vm from "node:vm";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

class FakeMessageEvent extends Event {
  constructor(type, init = {}) {
    super(type);
    this.data = init.data;
    this.source = init.source || null;
  }
}

class FakeWindow extends EventTarget {
  postMessage(data) {
    this.dispatchEvent(new FakeMessageEvent("message", {data, source: this}));
  }
}

function fixture(name) {
  return JSON.parse(readFileSync(join(root, "tests", "fixtures", name), "utf8"));
}

function loadBridge(pageUrl = "https://www.ray086.com/esports", rejectConfig = false) {
  const events = [];
  const eventMessages = [];
  const counters = [];
  const diagnostics = [];
  const stateListeners = [];
  const window = new FakeWindow();
  const chrome = {
    runtime: {
      sendMessage: async (message) => {
        if (message.action === "raybet.capture.getConfig") {
          if (rejectConfig) throw new Error("config unavailable");
          return {
            paused: false,
            enabled: true,
            enabledDomains: {ray086: true, raylinks: true},
            captureSessionId: "a".repeat(32),
          };
        }
        if (message.action === "raybet.capture.event") {
          eventMessages.push(message);
          events.push(message.event);
        }
        if (message.action === "raybet.capture.counter") counters.push(message);
        if (message.action === "raybet.capture.diagnostic") diagnostics.push(message);
        return {accepted: true};
      },
      onMessage: {addListener: (listener) => stateListeners.push(listener)},
    },
  };
  const context = vm.createContext({
    window,
    chrome,
    location: new URL(pageUrl),
    performance,
    crypto,
    TextEncoder,
    TextDecoder,
    URL,
    URLSearchParams,
    Event,
    EventTarget,
    MessageEvent: FakeMessageEvent,
    Object,
    Array,
    Set,
    WeakSet,
    JSON,
    Number,
    String,
    Date,
    Promise,
    setTimeout,
  });
  context.globalThis = context;
  for (const file of [
    "constants.js", "canonical-json.js", "redact.js", "classify.js", "content-bridge.js",
  ]) {
    vm.runInContext(readFileSync(join(root, "src", file), "utf8"), context, {filename: file});
  }
  return {window, events, eventMessages, counters, diagnostics, stateListeners};
}

function rawCandidate({
  sequence, path, payload, matchId = null, transport = "fetch", reason = null,
  origin = "https://cfinfo.365raylinks.com",
}) {
  const body = JSON.stringify(payload);
  return JSON.stringify({
    channel: "dota2-raybet-capture-v1",
    sequence,
    captured_at_utc: `2026-07-13T00:00:0${sequence}.000Z`,
    transport,
    source_url: `${origin}${path}`,
    raybet_match_id: matchId,
    body_text: body,
    raw_bytes: new TextEncoder().encode(body).byteLength,
    capture_reason: reason,
  });
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 30));

test("bridge emits sanitized per-match envelopes and honors pause", async () => {
  const harness = loadBridge();
  await flush();
  harness.window.postMessage(rawCandidate({
    sequence: 1,
    path: "/v2/match",
    payload: fixture("match-list.json"),
  }));
  await flush();
  assert.equal(harness.events.length, 2);
  assert.deepEqual(harness.events.map((event) => event.raybet_match_id), ["410001", "410002"]);
  assert.equal(JSON.stringify(harness.events).includes("fixture-token"), false);
  assert.equal(harness.events.every((event) => /^[0-9a-f]{64}$/.test(event.event_id)), true);

  harness.window.postMessage(rawCandidate({
    sequence: 2,
    path: "/v2/odds?match_id=410001",
    matchId: "410001",
    payload: fixture("odds.json"),
  }));
  await flush();
  assert.equal(harness.events[2].event_type, "odds");
  assert.equal(harness.events[2].game_id, 151);
  assert.equal(harness.eventMessages[2].source_origin, "https://cfinfo.365raylinks.com");
  assert.equal(JSON.stringify(harness.events[2]).includes("wallet_balance"), false);
  assert.equal(JSON.stringify(harness.events[2]).includes("bet_limit"), false);

  harness.stateListeners[0]({action: "raybet.capture.state", paused: true});
  harness.window.postMessage(rawCandidate({
    sequence: 3,
    path: "/v2/odds?match_id=410001",
    matchId: "410001",
    payload: fixture("odds.json"),
  }));
  await flush();
  assert.equal(harness.events.length, 3);
});

test("bridge applies source-domain changes immediately", async () => {
  const harness = loadBridge();
  await flush();
  harness.window.postMessage(rawCandidate({
    sequence: 1,
    path: "/v2/match",
    payload: fixture("match-list.json"),
  }));
  await flush();
  assert.equal(harness.events.length, 2);

  harness.stateListeners[0]({
    action: "raybet.capture.state",
    paused: false,
    enabled: true,
    enabledDomains: {ray086: true, raylinks: false},
  });
  harness.window.postMessage(rawCandidate({
    sequence: 2,
    path: "/v2/odds?match_id=410001",
    matchId: "410001",
    payload: fixture("odds.json"),
  }));
  await flush();
  assert.equal(harness.events.length, 2);

  harness.window.postMessage(rawCandidate({
    sequence: 3,
    path: "/v2/odds?match_id=410001",
    matchId: "410001",
    payload: fixture("odds.json"),
    origin: "https://www.ray086.com",
  }));
  await flush();
  assert.equal(harness.events.length, 3);
  assert.equal(harness.eventMessages[2].source_origin, "https://www.ray086.com");
});

test("bridge accepts the active no-www page and secondary market API", async () => {
  const harness = loadBridge("https://ray086.com/esports");
  await flush();
  harness.window.postMessage(rawCandidate({
    sequence: 1,
    path: "/v2/odds?match_id=410001&token=discarded",
    matchId: "410001",
    payload: fixture("odds.json"),
    origin: "https://iminfo.esportsworldlink.com",
  }));
  await flush();

  assert.equal(harness.events.length, 1);
  assert.equal(harness.events[0].page_origin, "https://ray086.com");
  assert.equal(harness.events[0].source_path, "/v2/odds");
  assert.equal(harness.eventMessages[0].source_origin, "https://iminfo.esportsworldlink.com");
  assert.equal(JSON.stringify(harness.events[0]).includes("token"), false);
  assert.ok(harness.diagnostics.some((item) => item.kind === "bridge_ready"));
  assert.ok(harness.diagnostics.some((item) => item.kind === "classification"
    && item.outcome === "accepted"));
});

test("diagnostic traffic cannot consume the candidate token bucket", async () => {
  const harness = loadBridge("https://ray086.com/esports");
  await flush();
  for (let index = 0; index < 25; index += 1) {
    harness.window.postMessage(JSON.stringify({
      channel: "dota2-raybet-diagnostic-v1",
      kind: "transport_observed",
      observed_at_utc: "2026-07-14T12:00:00.000Z",
      frame_context: "top",
      transport: "fetch",
      source_host: "iminfo.esportsworldlink.com",
      source_path: "/v2/odds",
      amount: 1,
    }));
  }
  harness.window.postMessage(rawCandidate({
    sequence: 1,
    path: "/v2/odds?match_id=410001",
    matchId: "410001",
    payload: fixture("odds.json"),
    origin: "https://iminfo.esportsworldlink.com",
  }));
  await flush();
  assert.equal(harness.events.length, 1);
  assert.equal(harness.events[0].event_type, "odds");
});

test("bridge diagnostics distinguish a failed config load", async () => {
  const harness = loadBridge("https://ray086.com/esports", true);
  await flush();
  const ready = harness.diagnostics.find((item) => item.kind === "bridge_ready");
  assert.equal(ready.config_loaded, false);
});

test("bridge accepts allowlisted WSS market events end to end", async () => {
  const harness = loadBridge();
  await flush();
  harness.window.postMessage(rawCandidate({
    sequence: 1,
    path: "/v2/match",
    payload: fixture("match-list.json"),
  }));
  await flush();

  harness.window.postMessage(rawCandidate({
    sequence: 2,
    path: "/live",
    matchId: "410001",
    transport: "websocket",
    origin: "wss://cfinfo.365raylinks.com",
    payload: fixture("odds.json"),
  }));
  await flush();

  const event = harness.events.at(-1);
  assert.equal(event.event_type, "odds");
  assert.equal(event.transport, "websocket");
  assert.equal(harness.eventMessages.at(-1).source_origin, "https://cfinfo.365raylinks.com");
});

test("bridge applies traversal limits before classification", async () => {
  const harness = loadBridge();
  await flush();
  harness.window.postMessage(rawCandidate({
    sequence: 1,
    path: "/v2/match",
    payload: fixture("match-list.json"),
  }));
  await flush();
  let nested = {value: true};
  for (let index = 0; index < 40; index += 1) nested = {nested};
  harness.window.postMessage(rawCandidate({
    sequence: 2,
    path: "/v2/odds?match_id=410001",
    matchId: "410001",
    payload: {result: {id: 410001, game_id: 151, nested}},
  }));
  await flush();
  const event = harness.events.at(-1);
  assert.equal(event.event_type, "unknown");
  assert.equal(event.capture_reason, "max_depth");
  assert.equal(JSON.stringify(event.payload), "{}");
});
