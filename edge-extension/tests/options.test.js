import test from "node:test";
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";

await import("../src/i18n.js");

function fakeElement() {
  return {
    checked: false,
    textContent: "",
    value: "",
    listeners: new Map(),
    addEventListener(name, listener) { this.listeners.set(name, listener); },
  };
}

test("popup and options use classic shared i18n scripts and real language labels", async () => {
  const popupHtml = await readFile(new URL("../src/popup.html", import.meta.url), "utf8");
  const optionsHtml = await readFile(new URL("../src/options.html", import.meta.url), "utf8");

  for (const [html, pageScript] of [
    [popupHtml, "popup.js"],
    [optionsHtml, "options.js"],
  ]) {
    assert.ok(html.indexOf('src="i18n.js"') < html.indexOf(`src="${pageScript}"`));
    assert.doesNotMatch(html, /<script[^>]+type=["']module["']/i);
    assert.doesNotMatch(html, /\son(?:click|change|input|load)\s*=/i);
    assert.match(html, /<label\s+for="languageSelect"/);
    assert.match(html, /<select\s+id="languageSelect"/);
  }
});

test("options translates save results immediately without changing config protocol values", async () => {
  const selectors = [
    "#languageSelect", "#ray086Enabled", "#raylinksEnabled", "#saveButton", "#saveMessage",
  ];
  const elements = new Map(selectors.map((selector) => [selector, fakeElement()]));
  const htmlAttributes = new Map();
  const storageListeners = new Set();
  const storageWrites = [];
  const runtimeMessages = [];
  const initialConfig = {enabledDomains: {ray086: false, raylinks: true}};
  let saveError = false;

  globalThis.document = {
    title: "",
    documentElement: {
      setAttribute(name, value) { htmlAttributes.set(name, value); },
    },
    querySelector(selector) { return elements.get(selector); },
    querySelectorAll() { return []; },
  };
  globalThis.chrome = {
    storage: {
      local: {
        async get(key) { return {[key]: key === "raybetMonitorUiLanguage" ? "zh-CN" : undefined}; },
        async set(value) {
          storageWrites.push(structuredClone(value));
          for (const [key, newValue] of Object.entries(value)) {
            for (const listener of storageListeners) {
              listener({[key]: {newValue}}, "local");
            }
          }
        },
      },
      onChanged: {
        addListener(listener) { storageListeners.add(listener); },
        removeListener(listener) { storageListeners.delete(listener); },
      },
    },
    runtime: {
      async sendMessage(message) {
        runtimeMessages.push(structuredClone(message));
        if (message.action === "raybet.options.get") return structuredClone(initialConfig);
        return saveError ? {error: "write_failed"} : {ok: true};
      },
    },
  };

  await import(`../src/options.js?test=${Date.now()}`);
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(elements.get("#languageSelect").value, "zh-CN");
  assert.equal(document.title, "Dota 2 监控设置");
  assert.equal(htmlAttributes.get("lang"), "zh-CN");
  assert.equal(elements.get("#ray086Enabled").checked, false);
  assert.equal(elements.get("#raylinksEnabled").checked, true);

  elements.get("#ray086Enabled").checked = true;
  elements.get("#raylinksEnabled").checked = false;
  await elements.get("#saveButton").listeners.get("click")();
  assert.equal(elements.get("#saveMessage").textContent, "已保存");
  assert.deepEqual(runtimeMessages.at(-1), {
    action: "raybet.options.save",
    enabledDomains: {ray086: true, raylinks: false},
  });
  assert.deepEqual(initialConfig, {enabledDomains: {ray086: false, raylinks: true}});

  elements.get("#languageSelect").value = "en";
  await elements.get("#languageSelect").listeners.get("change")();
  assert.deepEqual(storageWrites, [{raybetMonitorUiLanguage: "en"}]);
  assert.equal(document.title, "Dota 2 Monitor Settings");
  assert.equal(elements.get("#saveMessage").textContent, "Saved");

  saveError = true;
  await elements.get("#saveButton").listeners.get("click")();
  assert.equal(elements.get("#saveMessage").textContent, "Save failed");

  delete globalThis.chrome;
  delete globalThis.document;
});
