import test from "node:test";
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import vm from "node:vm";

const source = await readFile(new URL("../src/main-hook.js", import.meta.url), "utf8");

class FakeMessageEvent extends Event {
  constructor(type, init = {}) {
    super(type);
    this.data = init.data;
    this.source = init.source || null;
  }
}

class FakeWindow extends EventTarget {
  constructor(fetchImpl, Xhr, WebSocketImpl) {
    super();
    this.fetch = fetchImpl;
    this.XMLHttpRequest = Xhr;
    this.WebSocket = WebSocketImpl;
    this.messages = [];
    this.top = this;
  }

  postMessage(data) {
    this.messages.push(data);
    this.dispatchEvent(new FakeMessageEvent("message", {data, source: this}));
  }
}

class FakeXhr extends EventTarget {
  open(method, url) {
    this.opened = {method, url};
    return "opened";
  }
  send(value) {
    this.sent = value;
    return "sent";
  }
}

class FakeSocket extends EventTarget {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  constructor(url) {
    super();
    this.url = url;
  }
}

function contextFor(fetchImpl) {
  const window = new FakeWindow(fetchImpl, FakeXhr, FakeSocket);
  const context = vm.createContext({
    window,
    location: new URL("https://www.ray086.com/esports"),
    document: {visibilityState: "visible"},
    URL,
    TextEncoder,
    TextDecoder,
    MessageEvent: FakeMessageEvent,
    Event,
    EventTarget,
    WeakMap,
    Object,
    Promise,
    Reflect,
    Date,
    JSON,
    Number,
    Array,
    String,
    setTimeout,
    queueMicrotask,
    setInterval: () => 1,
  });
  context.globalThis = context;
  return {context, window};
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 300));

test("fetch return value is unchanged and early capture flushes after bridge ready", async () => {
  const payload = JSON.stringify({result: {id: 42, game_id: 151, odds: []}});
  const response = new Response(payload, {headers: {"content-type": "application/json"}});
  const originalPromise = Promise.resolve(response);
  const fetchImpl = function fetchImpl() { return originalPromise; };
  const {context, window} = contextFor(fetchImpl);
  vm.runInContext(source, context);

  const returned = window.fetch("https://cfinfo.365raylinks.com/v2/odds?match_id=42");
  assert.equal(returned, originalPromise);
  await flush();
  assert.equal(window.messages.length, 0);

  window.postMessage("dota2-raybet-bridge-ready-v1", "*");
  await flush();
  const messages = window.messages
    .filter((item) => typeof item === "string" && item.startsWith("{"))
    .map((item) => JSON.parse(item));
  const candidate = messages.find((item) => item.channel === "dota2-raybet-capture-v1");
  assert.equal(candidate.transport, "fetch");
  assert.equal(candidate.raybet_match_id, "42");
  assert.equal(candidate.body_text, payload);
  assert.equal(candidate.source_url, "https://cfinfo.365raylinks.com/v2/odds");
  assert.equal(candidate.source_url.includes("token"), false);
  const diagnostics = messages.filter((item) => item.channel === "dota2-raybet-diagnostic-v1");
  assert.ok(diagnostics.some((item) => item.kind === "hook_initialized"));
  const observed = diagnostics.find((item) => item.kind === "transport_observed");
  assert.equal(observed.source_host, "cfinfo.365raylinks.com");
  assert.equal(observed.source_path, "/v2/odds");
  assert.equal(JSON.stringify(observed).includes("match_id"), false);
});

test("XHR completion and WebSocket listener preserve page-facing behavior", async () => {
  const {context, window} = contextFor(() => Promise.reject(new Error("unused")));
  vm.runInContext(source, context);
  window.postMessage("dota2-raybet-bridge-ready-v1", "*");

  const xhr = new window.XMLHttpRequest();
  assert.equal(xhr.open("GET", "https://cfinfo.365raylinks.com/v2/match"), "opened");
  assert.equal(xhr.send(null), "sent");
  xhr.responseType = "";
  xhr.responseText = JSON.stringify({result: [{id: 5, game_id: 151}]});
  xhr.dispatchEvent(new Event("loadend"));

  const socket = new window.WebSocket("wss://cfinfo.365raylinks.com/live");
  let pageMessages = 0;
  socket.onmessage = () => { pageMessages += 1; };
  socket.addEventListener("message", socket.onmessage);
  socket.dispatchEvent(new MessageEvent("message", {data: "{\"game_id\":151}"}));
  await flush();

  const candidates = window.messages
    .filter((item) => typeof item === "string" && item.startsWith("{"))
    .map((item) => JSON.parse(item));
  assert.ok(candidates.some((item) => item.transport === "xhr"));
  assert.ok(candidates.some((item) => item.transport === "websocket"));
  assert.ok(candidates.some((item) => item.kind === "transport_observed"
    && item.source_path === "/v2/match"));
  assert.equal(pageMessages, 1);
  assert.equal(window.WebSocket.OPEN, 1);
  assert.equal(socket instanceof FakeSocket, true);
});
