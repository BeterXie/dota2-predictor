(function installConstants(root) {
  "use strict";
  const api = root.RaybetMonitor || (root.RaybetMonitor = {});
  const ACTIONS = Object.freeze({
  EVENT: "raybet.capture.event",
  COUNTER: "raybet.capture.counter",
  GET_CONFIG: "raybet.capture.getConfig",
  STATE: "raybet.capture.state",
  });

  const LIMITS = Object.freeze({
  RAW_BYTES: 1024 * 1024,
  SANITIZED_BYTES: 256 * 1024,
  MAX_DEPTH: 32,
  MAX_NODES: 20_000,
  MAX_ARRAY_ITEMS: 5_000,
  MAX_OBJECT_KEYS: 256,
  MAX_STRING_BYTES: 64 * 1024,
  });

  const ALLOWED_TRANSPORTS = Object.freeze([
  "fetch",
  "xhr",
  "websocket",
  "page_state",
  ]);

  const ALLOWED_EVENT_TYPES = Object.freeze([
  "match_list",
  "odds",
  "market_update",
  "video",
  "manual_control",
  "unknown",
  ]);

  const RAYBET_HOSTS = Object.freeze([
  "www.ray086.com",
  "cfinfo.365raylinks.com",
  ]);

  Object.assign(api, {
    SCHEMA_VERSION: 1,
    EXTENSION_VERSION: "0.1.0",
    DOTA2_GAME_ID: 151,
    HOOK_CHANNEL: "dota2-raybet-capture-v1",
    BRIDGE_READY_CHANNEL: "dota2-raybet-bridge-ready-v1",
    ACTIONS,
    LIMITS,
    ALLOWED_TRANSPORTS,
    ALLOWED_EVENT_TYPES,
    RAYBET_HOSTS,
  });
})(globalThis);
