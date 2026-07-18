import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { TextDecoder, TextEncoder } from "node:util";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const testDirectory = dirname(fileURLToPath(import.meta.url));
const extensionDirectory = join(testDirectory, "..");

function loadCore() {
  const context = vm.createContext({
    TextDecoder,
    TextEncoder,
    URL,
    URLSearchParams,
  });
  for (const file of ["constants.js", "canonical-json.js", "redact.js", "classify.js"]) {
    const source = readFileSync(join(extensionDirectory, "src", file), "utf8");
    new vm.Script(source, { filename: file }).runInContext(context);
  }
  return context.RaybetMonitor;
}

function fixture(name) {
  return JSON.parse(readFileSync(join(testDirectory, "fixtures", name), "utf8"));
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

test("classic scripts load in order and expose the shared API", () => {
  const api = loadCore();
  assert.equal(api.DOTA2_GAME_ID, 151);
  assert.equal(api.LIMITS.RAW_BYTES, 1024 * 1024);
  assert.equal(api.LIMITS.SANITIZED_BYTES, 256 * 1024);
  assert.equal(api.ACTIONS.EVENT, "raybet.capture.event");
  assert.equal(api.ACTIONS.DIAGNOSTIC, "raybet.capture.diagnostic");
  assert.deepEqual(plain(api.RAYBET_HOSTS), [
    "ray086.com",
    "www.ray086.com",
    "cfinfo.365raylinks.com",
    "iminfo.esportsworldlink.com",
  ]);
  assert.equal(typeof api.sanitizeCandidate, "function");
  assert.equal(typeof api.classifyCandidate, "function");
  assert.equal(typeof api.extractVideoPayload, "function");
});

test("canonical JSON is stable across object insertion order", () => {
  const { canonicalJson } = loadCore();
  const first = { z: 1, a: { c: 3, b: 2 }, items: [{ y: 2, x: 1 }] };
  const second = { items: [{ x: 1, y: 2 }], a: { b: 2, c: 3 }, z: 1 };
  assert.equal(canonicalJson(first), canonicalJson(second));
  assert.equal(canonicalJson(first), '{"a":{"b":2,"c":3},"items":[{"x":1,"y":2}],"z":1}');
});

test("canonical JSON matches shared RFC 8785 vectors", () => {
  const { canonicalJson } = loadCore();
  for (const vector of fixture("canonical-vectors.json")) {
    assert.equal(canonicalJson(vector.value), vector.canonical, vector.name);
  }
});

test("canonical JSON rejects cycles", () => {
  const { canonicalJson } = loadCore();
  const cyclic = {};
  cyclic.self = cyclic;
  assert.throws(() => canonicalJson(cyclic), /Cyclic/);
});

test("redaction removes forbidden keys with punctuation variants at arbitrary depth", () => {
  const { sanitizeCandidate } = loadCore();
  const input = {
    safe: 1,
    nested: {
      "Authorization-Token": "fixture-secret",
      public: [{ "wallet.balance": 10, market_id: 22 }],
      request_headers: { anything: true },
      selectionSlip: { stake: 100 },
      team_id: 10,
      match_id: 410001,
    },
  };
  const result = sanitizeCandidate(input);
  assert.equal(result.ok, true);
  assert.deepEqual(plain(result.value), {
    safe: 1,
    nested: { public: [{ market_id: 22 }], team_id: 10, match_id: 410001 },
  });
  assert.equal(result.redactedKeys, 4);
});

test("redaction removes identity and prototype-pollution keys", () => {
  const { sanitizeCandidate } = loadCore();
  const input = JSON.parse(`{
    "safe": 1,
    "persistent_client": "x",
    "visitorId": "x",
    "browser-id": "x",
    "machine_id": "x",
    "installId": "x",
    "__proto__": "x",
    "prototype": "x",
    "constructor": "x"
  }`);
  const result = sanitizeCandidate(input);
  assert.equal(result.ok, true);
  assert.deepEqual(plain(result.value), {safe: 1});
  assert.equal(result.redactedKeys, 8);
});

test("redaction removes authorization-material key variants", () => {
  const { sanitizeCandidate } = loadCore();
  const input = {
    public: true,
    "API-Key": "x",
    access_key: "x",
    privateKey: "x",
    password: "x",
    passwd: "x",
    credential: "x",
    "url.signature": "x",
  };
  const result = sanitizeCandidate(input);
  assert.equal(result.ok, true);
  assert.deepEqual(plain(result.value), {public: true});
  assert.equal(result.redactedKeys, 7);
});

test("redaction skips accessors without invoking page-owned code", () => {
  const { sanitizeCandidate } = loadCore();
  let invoked = false;
  const input = { safe: "value" };
  Object.defineProperty(input, "computed", {
    enumerable: true,
    get() {
      invoked = true;
      return "must-not-run";
    },
  });
  const result = sanitizeCandidate(input);
  assert.equal(result.ok, true);
  assert.equal(invoked, false);
  assert.deepEqual(plain(result.value), { safe: "value" });
});

test("depth, node, array, object-key, and string bounds are exact", () => {
  const { sanitizeCandidate } = loadCore();
  const nested = (depth) => {
    let value = "leaf";
    for (let index = 0; index < depth; index += 1) value = { next: value };
    return value;
  };
  assert.equal(sanitizeCandidate(nested(32)).ok, true);
  assert.equal(sanitizeCandidate(nested(33)).reason, "max_depth");

  assert.equal(sanitizeCandidate([1, 2], { MAX_NODES: 3 }).ok, true);
  assert.equal(sanitizeCandidate([1, 2], { MAX_NODES: 2 }).reason, "max_nodes");
  assert.equal(sanitizeCandidate([1, 2], { MAX_ARRAY_ITEMS: 2 }).ok, true);
  assert.equal(sanitizeCandidate([1, 2, 3], { MAX_ARRAY_ITEMS: 2 }).reason, "max_array_items");

  const twoKeys = { a: 1, b: 2 };
  assert.equal(sanitizeCandidate(twoKeys, { MAX_OBJECT_KEYS: 2 }).ok, true);
  assert.equal(sanitizeCandidate(twoKeys, { MAX_OBJECT_KEYS: 1 }).reason, "max_object_keys");
  assert.equal(sanitizeCandidate("界", { MAX_STRING_BYTES: 3 }).ok, true);
  assert.equal(sanitizeCandidate("界", { MAX_STRING_BYTES: 2 }).reason, "max_string_bytes");
});

test("default traversal limits accept the boundary and reject one item beyond it", () => {
  const { sanitizeCandidate } = loadCore();
  assert.equal(sanitizeCandidate(new Array(5_000).fill(0)).ok, true);
  assert.equal(sanitizeCandidate(new Array(5_001).fill(0)).reason, "max_array_items");

  const keys256 = Object.fromEntries(Array.from({ length: 256 }, (_, index) => [`k${index}`, index]));
  const keys257 = { ...keys256, overflow: true };
  assert.equal(sanitizeCandidate(keys256).ok, true);
  assert.equal(sanitizeCandidate(keys257).reason, "max_object_keys");

  const nodes20k = [
    new Array(5_000).fill(0),
    new Array(5_000).fill(0),
    new Array(5_000).fill(0),
    new Array(4_995).fill(0),
  ];
  assert.equal(sanitizeCandidate(nodes20k).visitedNodes, 20_000);
  nodes20k[3].push(0);
  assert.equal(sanitizeCandidate(nodes20k).reason, "max_nodes");

  assert.equal(sanitizeCandidate("a".repeat(64 * 1024)).ok, true);
  assert.equal(sanitizeCandidate("a".repeat(64 * 1024 + 1)).reason, "max_string_bytes");
});

test("the 256 KiB sanitized payload boundary is exact", () => {
  const { LIMITS, sanitizeCandidate } = loadCore();
  const exact = {
    a: [
      "a".repeat(65_531),
      "b".repeat(65_531),
      "c".repeat(65_531),
      "d".repeat(65_532),
    ],
  };
  const accepted = sanitizeCandidate(exact);
  assert.equal(accepted.ok, true);
  assert.equal(accepted.bytes, LIMITS.SANITIZED_BYTES);
  exact.a[3] += "d";
  const rejected = sanitizeCandidate(exact);
  assert.equal(rejected.reason, "payload_too_large");
  assert.equal(rejected.bytes, LIMITS.SANITIZED_BYTES + 1);
});

test("cyclic input terminates with a stable reason", () => {
  const { sanitizeCandidate } = loadCore();
  const cyclic = { safe: true };
  cyclic.self = cyclic;
  assert.equal(sanitizeCandidate(cyclic).reason, "cycle");
});

test("match-list classification emits one event per Dota match and establishes the allowlist", () => {
  const { classifyCandidate, createClassificationState } = loadCore();
  const state = createClassificationState();
  const result = classifyCandidate({ sourcePath: "/v2/match", transport: "fetch", payload: fixture("match-list.json") }, state);
  assert.equal(result.events.length, 2);
  assert.deepEqual(plain(result.events.map((event) => event.raybetMatchId)), ["410001", "410002"]);
  assert.equal(result.events.every((event) => event.gameId === 151), true);
  assert.equal(result.events.every((event) => event.payload.result.length === 1), true);
  assert.deepEqual(Array.from(state.dotaMatchIds).sort(), ["410001", "410002"]);
  assert.equal(JSON.stringify(result.events).includes("fixture-account"), false);
  assert.equal(JSON.stringify(result.events).includes("member-token"), false);
  assert.equal(JSON.stringify(result.events).includes("fixture-token"), false);
});

test("odds classification uses positive allowlists and accepts an established Dota match", () => {
  const { classifyCandidate, createClassificationState } = loadCore();
  const state = createClassificationState(["410001"]);
  const payload = fixture("odds.json");
  delete payload.result.game_id;
  const result = classifyCandidate({
    sourceUrl: "https://cfinfo.365raylinks.com/v2/odds?match_id=410001&token=discarded",
    transport: "xhr",
    payload,
  }, state);
  assert.equal(result.events.length, 1);
  assert.equal(result.events[0].eventType, "odds");
  assert.equal(result.events[0].sourcePath, "/v2/odds");
  assert.equal(result.events[0].gameId, 151);
  const encoded = JSON.stringify(result.events[0].payload);
  assert.equal(encoded.includes("bet_limit"), false);
  assert.equal(encoded.includes("request_headers"), false);
  assert.equal(encoded.includes("wallet_balance"), false);
  assert.equal(encoded.includes("live_url"), false);
  assert.equal(result.events[0].payload.result.odds[0].odds, 2.1);
});

test("classification rejects contradictory or unproven identity", () => {
  const { classifyCandidate, createClassificationState } = loadCore();
  const payload = fixture("odds.json");
  payload.result.game_id = 1;
  assert.equal(classifyCandidate({ sourcePath: "/v2/odds", raybetMatchId: "410001", payload }, createClassificationState(["410001"])).ignoredReason, "non_dota");

  delete payload.result.game_id;
  assert.equal(classifyCandidate({ sourcePath: "/v2/odds", raybetMatchId: "999999", payload: { result: { id: 999999, odds: [] } } }).ignoredReason, "untrusted_match");
  assert.equal(classifyCandidate({
    sourcePath: "/v2/odds",
    raybetMatchId: "999999",
    gameId: 151,
    payload: { result: { id: 999999, odds: [] } },
  }).ignoredReason, "untrusted_match");

  payload.result.id = 410002;
  assert.equal(classifyCandidate({ sourceUrl: "https://cfinfo.365raylinks.com/v2/odds?match_id=410001", payload }).ignoredReason, "match_id_mismatch");
  assert.equal(classifyCandidate({
    sourceUrl: "https://untrusted.invalid/v2/odds?match_id=410002",
    payload,
  }).ignoredReason, "invalid_candidate");
});

test("manual control remains diagnostic and copies only primitive fields", () => {
  const { classifyCandidate, createClassificationState } = loadCore();
  const result = classifyCandidate({
    sourcePath: "/manualControlData",
    transport: "page_state",
    raybetMatchId: "410001",
    payload: { currentIndex: 2, time: "18:42", game_clock_seconds: 1122, extra: { unsafe: true } },
  }, createClassificationState(["410001"]));
  assert.equal(result.events[0].eventType, "manual_control");
  assert.equal(result.events[0].captureReason, "diagnostic_untrusted");
  assert.deepEqual(plain(result.events[0].payload), { currentIndex: 2, time: "18:42" });
  assert.equal(Object.hasOwn(result.events[0].payload, "game_clock_seconds"), false);
});

test("allowlisted WSS live payloads use market structure instead of HTTP paths", () => {
  const { classifyCandidate, createClassificationState } = loadCore();
  const payload = fixture("odds.json");
  const result = classifyCandidate({
    sourceUrl: "wss://cfinfo.365raylinks.com/live",
    transport: "websocket",
    raybetMatchId: "410001",
    payload,
  }, createClassificationState(["410001"]));
  assert.equal(result.events[0].eventType, "odds");
  assert.equal(result.events[0].sourcePath, "/live");
  assert.equal(result.events[0].payload.result.id, 410001);
});

test("video classification keeps public state but drops signed playback URLs", () => {
  const { classifyCandidate, createClassificationState, extractVideoPayload } = loadCore();
  const payload = {
    result: {
      state: "playing",
      currentTime: 912,
      duration: 3600,
      playback_url: "wss://cfinfo.365raylinks.com/live/410001.m3u8?token=fixture-token&expires=9",
      authorization: "must-not-be-retained",
      headers: { authorization: "must-not-be-retained" },
    },
  };
  const safe = extractVideoPayload(payload);
  assert.deepEqual(plain(safe), {
    result: {
      state: "playing",
      currentTime: 912,
      duration: 3600,
    },
  });
  const result = classifyCandidate({
    sourceUrl: "https://cfinfo.365raylinks.com/live?match_id=410001",
    transport: "fetch",
    raybetMatchId: "410001",
    payload,
  }, createClassificationState(["410001"]));
  assert.equal(result.events.length, 1);
  assert.equal(result.events[0].eventType, "video");
  assert.equal(JSON.stringify(result.events[0]).includes("fixture-token"), false);
  assert.equal(JSON.stringify(result.events[0]).includes("authorization"), false);
});

test("video classification retains only unsigned allowlisted HTTPS playback URLs", () => {
  const { extractVideoPayload } = loadCore();
  assert.deepEqual(plain(extractVideoPayload({
    result: {
      state: "playing",
      playback_url: "https://cfinfo.365raylinks.com/live/410001.m3u8",
    },
  })), {
    result: {
      state: "playing",
      playback_url: "https://cfinfo.365raylinks.com/live/410001.m3u8",
    },
  });
  for (const playback_url of [
    "javascript:alert(1)",
    "https://foreign.example/live/410001.m3u8",
    "wss://cfinfo.365raylinks.com/live/410001.m3u8",
  ]) {
    assert.deepEqual(plain(extractVideoPayload({
      result: {state: "playing", playback_url},
    })), {result: {state: "playing"}});
  }
});

test("video state-only payloads use constrained values and reject injected text", () => {
  const { classifyCandidate, createClassificationState, extractVideoPayload } = loadCore();
  const stateOnly = extractVideoPayload({result: {state: "playing"}});
  assert.deepEqual(plain(stateOnly), {result: {state: "playing"}});
  const malicious = extractVideoPayload({
    result: {
      state: "token=secret",
      currentTime: "bearer SECRET",
      width: 1920,
      quality: "ultra-secret",
    },
  });
  assert.equal(malicious, null);
  const result = classifyCandidate({
    sourcePath: "/live",
    transport: "websocket",
    raybetMatchId: "410001",
    payload: {result: {state: "playing"}},
  }, createClassificationState(["410001"]));
  assert.equal(result.events[0].eventType, "video");
});

test("metadata-only unknown events cannot establish a trusted match", () => {
  const { classifyCandidate, createClassificationState } = loadCore();
  const state = createClassificationState();
  const unknown = classifyCandidate({
    sourcePath: "/v2/unrecognized",
    transport: "websocket",
    payload: { result: { id: 420001, game_id: 151, arbitrary: "discarded" } },
  }, state);
  assert.equal(unknown.events[0].eventType, "unknown");
  assert.deepEqual(plain(unknown.events[0].payload), {});
  assert.equal(state.dotaMatchIds.has("420001"), false);

  const laterOdds = classifyCandidate({
    sourcePath: "/v2/odds",
    raybetMatchId: "420001",
    payload: { result: { id: 420001, odds: [] } },
  }, state);
  assert.equal(laterOdds.ignoredReason, "untrusted_match");
});
