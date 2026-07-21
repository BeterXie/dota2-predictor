import test from "node:test";
import assert from "node:assert/strict";

await import("../src/i18n.js");

function fakeElement() {
  const listeners = new Map();
  return {
    listeners,
    className: "",
    textContent: "",
    title: "",
    checked: false,
    hidden: true,
    href: "",
    value: "",
    addEventListener(name, listener) { listeners.set(name, listener); },
  };
}

test("popup refreshes connection and capture state after a toggle", async () => {
  const selectors = [
    "#stateDot", "#stateText", "#captureToggle", "#companionValue",
    "#matchCount", "#queueCount", "#dropCount", "#lastEvent",
    "#hookStatus", "#bridgeStatus", "#observedCount", "#classifiedCount",
    "#lastObserved", "#lastDecision",
    "#reportLink", "#openOptions", "#languageSelect",
  ];
  const elements = new Map(selectors.map((selector) => [selector, fakeElement()]));
  let status = {
    state: "capturing",
    paused: false,
    companion: {reachable: true, connected: true},
    recognizedMatches: ["42"],
    queue: [],
    counters: {dropped: 0},
    diagnostics: {
      bridgeConfigLoaded: true,
      initialization: {
        hook: {top: 1, child: 0},
        ready: {top: 1, child: 0},
      },
      transports: {fetch: 3, xhr: 2, websocket: 1},
      classification: {accepted: 2, ignored: 1},
      lastObserved: {
        sourceHost: "iminfo.esportsworldlink.com",
        sourcePath: "/v2/odds",
        observedAt: "2026-07-14T12:00:00.000Z",
      },
      lastClassification: {outcome: "ignored", reason: "non_dota"},
    },
    lastEvent: null,
    remote: {report_url: "http://127.0.0.1:9000/report"},
  };
  const actions = [];
  globalThis.document = {
    querySelector(selector) { return elements.get(selector); },
  };
  globalThis.chrome = {
    runtime: {
      async sendMessage(message) {
        actions.push(message.action);
        if (message.action === "raybet.popup.setPaused") {
          status = {
            ...status,
            paused: Boolean(message.paused),
            state: message.paused ? "paused" : "buffering",
            remote: null,
          };
          return {paused: status.paused};
        }
        return structuredClone(status);
      },
      openOptionsPage() {},
    },
  };

  await import(`../src/popup.js?test=${Date.now()}`);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(elements.get("#stateText").textContent, "Capturing");
  assert.equal(elements.get("#companionValue").textContent, "Connected");
  assert.equal(elements.get("#captureToggle").checked, true);
  assert.equal(elements.get("#reportLink").hidden, false);
  assert.equal(elements.get("#hookStatus").textContent, "top 1 | child 0");
  assert.equal(elements.get("#observedCount").textContent, "F 3 | X 2 | W 1");
  assert.equal(elements.get("#classifiedCount").textContent, "2 accepted | 1 ignored");
  assert.match(elements.get("#lastObserved").textContent, /^iminfo\.esportsworldlink\.com\/v2\/odds \| /);
  assert.equal(elements.get("#lastDecision").textContent, "Ignored: Non-Dota traffic");

  elements.get("#captureToggle").checked = false;
  await elements.get("#captureToggle").listeners.get("change")({
    target: elements.get("#captureToggle"),
  });
  assert.equal(elements.get("#stateText").textContent, "Paused");
  assert.equal(elements.get("#captureToggle").checked, false);
  assert.equal(elements.get("#reportLink").hidden, true);
  assert.equal(elements.get("#reportLink").href, "");
  assert.deepEqual(actions.slice(-2), [
    "raybet.popup.setPaused",
    "raybet.popup.getStatus",
  ]);

  delete globalThis.chrome;
  delete globalThis.document;
});

test("popup prefers additive capture status and exposes its reason", async () => {
  const selectors = [
    "#stateDot", "#stateText", "#captureToggle", "#companionValue",
    "#matchCount", "#queueCount", "#dropCount", "#lastEvent",
    "#hookStatus", "#bridgeStatus", "#observedCount", "#classifiedCount",
    "#lastObserved", "#lastDecision", "#reportLink", "#openOptions", "#languageSelect",
  ];
  const elements = new Map(selectors.map((selector) => [selector, fakeElement()]));
  globalThis.document = {
    querySelector(selector) { return elements.get(selector); },
  };
  globalThis.chrome = {
    runtime: {
      async sendMessage() {
        return {
          state: "buffering",
          captureStatus: "page_hook_missing",
          statusReason: "main_hook_not_seen",
          paused: false,
          companion: {reachable: true, connected: true},
          recognizedMatches: [],
          queue: [],
          counters: {dropped: 0},
          diagnostics: {},
        };
      },
      openOptionsPage() {},
    },
  };

  await import(`../src/popup.js?capture-status-test=${Date.now()}`);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(elements.get("#stateText").textContent, "Page hook missing");
  assert.equal(elements.get("#stateText").title, "Main page hook not seen");
  assert.equal(elements.get("#stateDot").className, "error");

  delete globalThis.chrome;
  delete globalThis.document;
});

test("popup renders a failed status request as degraded", async () => {
  const selectors = [
    "#stateDot", "#stateText", "#captureToggle", "#companionValue",
    "#matchCount", "#queueCount", "#dropCount", "#lastEvent",
    "#hookStatus", "#bridgeStatus", "#observedCount", "#classifiedCount",
    "#lastObserved", "#lastDecision", "#reportLink", "#openOptions", "#languageSelect",
  ];
  const elements = new Map(selectors.map((selector) => [selector, fakeElement()]));
  globalThis.document = {
    querySelector(selector) { return elements.get(selector); },
  };
  globalThis.chrome = {
    runtime: {
      async sendMessage() { throw new Error("extension context unavailable"); },
      openOptionsPage() {},
    },
  };

  await import(`../src/popup.js?status-error-test=${Date.now()}`);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(elements.get("#stateText").textContent, "Degraded");
  assert.equal(elements.get("#stateText").title, "Extension status unavailable");
  assert.equal(elements.get("#stateDot").className, "error");
  assert.equal(elements.get("#companionValue").textContent, "Unavailable");

  delete globalThis.chrome;
  delete globalThis.document;
});

test("popup switches dynamic status text immediately and preserves unknown protocol codes", async () => {
  const selectors = [
    "#stateDot", "#stateText", "#captureToggle", "#companionValue",
    "#matchCount", "#queueCount", "#dropCount", "#lastEvent",
    "#hookStatus", "#bridgeStatus", "#observedCount", "#classifiedCount",
    "#lastObserved", "#lastDecision", "#reportLink", "#openOptions", "#languageSelect",
  ];
  const elements = new Map(selectors.map((selector) => [selector, fakeElement()]));
  const writes = [];
  const storageListeners = new Set();
  const status = {
    state: "buffering",
    captureStatus: "waiting_for_traffic",
    statusReason: "no_dota_event_accepted",
    paused: false,
    companion: {reachable: true, connected: true},
    recognizedMatches: ["38411208"],
    queue: [],
    counters: {dropped: 0},
    lastEvent: {eventType: "odds", capturedAt: "2026-07-21T00:00:00.000Z"},
    diagnostics: {
      bridgeConfigLoaded: false,
      initialization: {
        hook: {top: 1, child: 0},
        ready: {top: 1, child: 0},
      },
      transports: {fetch: 1, xhr: 2, websocket: 0},
      classification: {accepted: 1, ignored: 2},
      lastClassification: {outcome: "accepted", reason: "future_reason"},
    },
    remote: null,
  };
  const original = structuredClone(status);
  globalThis.document = {
    querySelector(selector) { return elements.get(selector); },
  };
  globalThis.chrome = {
    storage: {
      local: {
        async get(key) { return {[key]: "zh-CN"}; },
        async set(value) { writes.push(structuredClone(value)); },
      },
      onChanged: {
        addListener(listener) { storageListeners.add(listener); },
        removeListener(listener) { storageListeners.delete(listener); },
      },
    },
    runtime: {
      async sendMessage() { return structuredClone(status); },
      openOptionsPage() {},
    },
  };

  await import(`../src/popup.js?language-test=${Date.now()}`);
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(elements.get("#stateText").textContent, "等待页面流量");
  assert.equal(elements.get("#stateText").title, "尚未接受 Dota 事件");
  assert.equal(elements.get("#companionValue").textContent, "已连接");
  assert.match(elements.get("#lastEvent").textContent, /^赔率 · /);
  assert.equal(elements.get("#hookStatus").textContent, "顶层 1 | 子框架 0");
  assert.equal(elements.get("#bridgeStatus").textContent, "配置失败");
  assert.equal(elements.get("#classifiedCount").textContent, "已接受 1 | 已忽略 2");
  assert.equal(elements.get("#lastDecision").textContent, "已接受：future_reason");

  elements.get("#languageSelect").value = "en";
  await elements.get("#languageSelect").listeners.get("change")();

  assert.deepEqual(writes, [{raybetMonitorUiLanguage: "en"}]);
  assert.equal(elements.get("#stateText").textContent, "Waiting for traffic");
  assert.equal(elements.get("#stateText").title, "No Dota event accepted yet");
  assert.equal(elements.get("#lastDecision").textContent, "Accepted: future_reason");
  assert.deepEqual(status, original);

  delete globalThis.chrome;
  delete globalThis.document;
});
