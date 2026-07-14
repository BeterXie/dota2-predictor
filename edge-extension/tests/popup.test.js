import test from "node:test";
import assert from "node:assert/strict";

function fakeElement() {
  const listeners = new Map();
  return {
    listeners,
    className: "",
    textContent: "",
    checked: false,
    hidden: true,
    href: "",
    addEventListener(name, listener) { listeners.set(name, listener); },
  };
}

test("popup refreshes connection and capture state after a toggle", async () => {
  const selectors = [
    "#stateDot", "#stateText", "#captureToggle", "#companionValue",
    "#matchCount", "#queueCount", "#dropCount", "#lastEvent",
    "#reportLink", "#openOptions",
  ];
  const elements = new Map(selectors.map((selector) => [selector, fakeElement()]));
  let status = {
    state: "capturing",
    paused: false,
    companion: {reachable: true, connected: true},
    recognizedMatches: ["42"],
    queue: [],
    counters: {dropped: 0},
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
  assert.equal(elements.get("#stateText").textContent, "capturing");
  assert.equal(elements.get("#companionValue").textContent, "Connected");
  assert.equal(elements.get("#captureToggle").checked, true);
  assert.equal(elements.get("#reportLink").hidden, false);

  elements.get("#captureToggle").checked = false;
  await elements.get("#captureToggle").listeners.get("change")({
    target: elements.get("#captureToggle"),
  });
  assert.equal(elements.get("#stateText").textContent, "paused");
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
