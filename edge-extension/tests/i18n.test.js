import test from "node:test";
import assert from "node:assert/strict";
import {spawnSync} from "node:child_process";
import {fileURLToPath} from "node:url";

await import("../src/i18n.js");

const i18n = globalThis.RaybetI18n;
const companionPath = fileURLToPath(
  new URL("../../live_betting/browser_companion.py", import.meta.url),
);
const astEnumerator = String.raw`
import ast
import json
import sys
import tokenize

path = sys.argv[1]
with tokenize.open(path) as source_file:
    tree = ast.parse(source_file.read(), filename=path)

def argument(call, name, index):
    if len(call.args) > index:
        return call.args[index]
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None

errors = []
invalid = []
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    if not isinstance(node.func, ast.Name) or node.func.id != "_error":
        continue
    code_node = argument(node, "code", 0)
    status_node = argument(node, "status", 1)
    code = code_node.value if isinstance(code_node, ast.Constant) else None
    status = status_node.value if isinstance(status_node, ast.Constant) else None
    if not isinstance(code, str) or type(status) is not int:
        invalid.append(f"line {node.lineno}: _error code/status must be string/int literals")
        continue
    errors.append({"code": code, "status": status, "line": node.lineno})

if invalid:
    raise SystemExit("\n".join(invalid))
if not errors:
    raise SystemExit("no _error calls found")
print(json.dumps(errors, sort_keys=True))
`;

function enumerateCompanionErrors() {
  const candidates = process.platform === "win32"
    ? [["python", []], ["py", ["-3"]]]
    : [["python3", []], ["python", []]];
  const missing = [];
  for (const [command, prefix] of candidates) {
    const result = spawnSync(command, [...prefix, "-c", astEnumerator, companionPath], {
      encoding: "utf8",
      windowsHide: true,
    });
    if (result.error?.code === "ENOENT") {
      missing.push(command);
      continue;
    }
    if (result.error) {
      throw new Error(`failed to start ${command}: ${result.error.message}`);
    }
    if (result.status !== 0) {
      const detail = result.stderr.trim() || result.stdout.trim() || "no error output";
      throw new Error(`Companion AST enumeration failed via ${command} (${result.status}): ${detail}`);
    }
    try {
      const errors = JSON.parse(result.stdout);
      if (!Array.isArray(errors)) throw new Error("enumerator did not return an array");
      return errors.sort((left, right) => left.code.localeCompare(right.code));
    } catch (error) {
      throw new Error(`invalid Companion AST enumeration output from ${command}: ${error.message}`);
    }
  }
  throw new Error(`Python interpreter not found; tried: ${missing.join(", ")}`);
}

const companionErrors = enumerateCompanionErrors();
const companionErrorContract = [...new Map(
  companionErrors.map(({code, status}) => [`${code}\0${status}`, {code, status}]),
).values()].sort((left, right) => left.code.localeCompare(right.code));

function fakeElement(dataset = {}) {
  const attributes = new Map();
  return {
    dataset,
    attributes,
    textContent: "",
    value: "",
    listeners: new Map(),
    addEventListener(name, listener) { this.listeners.set(name, listener); },
    setAttribute(name, value) { attributes.set(name, value); },
  };
}

function fakeDocument(groups = {}) {
  return {
    title: "",
    documentElement: fakeElement(),
    querySelectorAll(selector) { return groups[selector] || []; },
  };
}

function fakeChrome(initial = {}) {
  const stored = {...initial};
  const listeners = new Set();
  const writes = [];
  return {
    stored,
    listeners,
    writes,
    storage: {
      local: {
        async get(key) { return {[key]: stored[key]}; },
        async set(value) {
          writes.push(structuredClone(value));
          for (const [key, newValue] of Object.entries(value)) {
            const oldValue = stored[key];
            stored[key] = newValue;
            for (const listener of listeners) {
              listener({[key]: {oldValue, newValue}}, "local");
            }
          }
        },
      },
      onChanged: {
        addListener(listener) { listeners.add(listener); },
        removeListener(listener) { listeners.delete(listener); },
      },
    },
  };
}

test("language resolution follows navigator preference and code fallbacks preserve protocol values", () => {
  assert.equal(i18n.normalizeLanguage("zh-Hans-CN"), "zh-CN");
  assert.equal(i18n.normalizeLanguage("en-US"), "en");
  assert.equal(i18n.normalizeLanguage("fr-FR"), null);
  assert.equal(i18n.preferredLanguage({languages: ["fr-FR", "zh-Hans"]}), "zh-CN");
  assert.equal(i18n.preferredLanguage({languages: ["fr-FR"], language: "de-DE"}), "en");

  const protocolStatus = {
    captureStatus: "waiting_for_traffic",
    statusReason: "future_reason_code",
  };
  const original = structuredClone(protocolStatus);
  assert.equal(i18n.translateCode("zh-CN", "captureStatus", protocolStatus.captureStatus), "等待页面流量");
  assert.equal(i18n.translateCode("zh-CN", "statusReason", protocolStatus.statusReason), "future_reason_code");
  assert.deepEqual(protocolStatus, original);
});

test("popup-owned display code groups have English and Chinese text", () => {
  const codes = {
    captureStatus: [
      "capturing", "backpressure", "buffering", "companion_offline", "paused",
      "reconnecting", "waiting_for_traffic", "degraded", "page_hook_missing",
      "unsupported_page", "error",
    ],
    statusReason: [
      "capture_paused", "unsupported_page", "active_tab_unavailable", "events_dropped",
      "events_rejected", "queue_state_invalid", "bridge_config_failed", "database_unavailable",
      "forbidden_origin", "invalid_origin", "invalid_protocol_response", "invalid_status_body",
      "origin_required", "origin_not_allowed", "unsupported_extension_version",
      "unsupported_protocol_version",
      "companion_http_error", "queue_event_high_water", "queue_byte_high_water",
      "bridge_unreachable", "main_hook_not_seen", "status_probe_timeout",
      "status_probe_network_error", "draining_queue",
      "retry_scheduled", "companion_retry_pending", "page_loading", "hook_initializing",
      "no_transport_observed", "no_dota_event_accepted", "acknowledgements_current",
      "extension_status_unavailable",
    ],
    outcome: ["accepted", "ignored"],
    reason: [
      "non_dota", "match_list", "video", "odds", "market_update", "manual_control",
      "unknown", "service_worker_rejected", "payload_too_large", "raw_payload_too_large",
      "binary_payload", "invalid_envelope", "unknown_structure", "invalid_raw",
      "disabled_source", "invalid_json", "invalid_candidate", "invalid_page_origin",
      "invalid_bridge_json", "match_id_mismatch", "untrusted_match", "missing_match_id",
      "invalid_odds", "invalid_manual_control", "diagnostic_untrusted", "max_nodes",
      "max_depth", "non_json_value", "max_string_bytes", "cycle", "max_array_items",
      "max_object_keys", "metadata_untrusted_match", "capture_inactive", "no_events",
      "rate_limited", "processing_error", "invalid_sender", "invalid_diagnostic",
    ],
    eventType: ["match_list", "video", "odds", "market_update", "manual_control", "unknown"],
  };

  for (const language of ["en", "zh-CN"]) {
    for (const [category, values] of Object.entries(codes)) {
      for (const code of values) {
        assert.notEqual(
          i18n.translateCode(language, category, code),
          code,
          `${language} ${category}.${code}`,
        );
      }
    }
  }
});

test("AST-enumerated Companion errors and derived rate-limit output have bilingual text", () => {
  assert.deepEqual(companionErrorContract, [
    {code: "body_too_large", status: 413},
    {code: "database_unavailable", status: 503},
    {code: "forbidden_field", status: 400},
    {code: "invalid_batch", status: 400},
    {code: "invalid_json", status: 400},
    {code: "invalid_status_body", status: 400},
    {code: "origin_not_allowed", status: 403},
    {code: "rate_limited", status: 429},
    {code: "unsupported_extension_version", status: 400},
    {code: "unsupported_media_type", status: 415},
  ]);

  for (const {code} of companionErrorContract.filter(({code}) => code !== "rate_limited")) {
    for (const language of ["en", "zh-CN"]) {
      assert.notEqual(
        i18n.translateCode(language, "statusReason", code),
        code,
        `${language} Companion ${code}`,
      );
    }
  }

  for (const language of ["en", "zh-CN"]) {
    assert.notEqual(
      i18n.translateCode(language, "statusReason", "companion_rate_limited"),
      "companion_rate_limited",
      `${language} service-worker-derived companion_rate_limited`,
    );
  }

  assert.equal(
    i18n.translateCode("en", "statusReason", "origin_not_allowed"),
    "Origin not allowed",
  );
  assert.equal(
    i18n.translateCode("zh-CN", "statusReason", "origin_not_allowed"),
    "来源不在允许范围内",
  );
});

test("document translation updates text, title, aria labels, titles, and html language", () => {
  const textNode = fakeElement({i18n: "popup.capture"});
  const titleNode = fakeElement({i18nTitle: "popup.settings"});
  const ariaNode = fakeElement({i18nAriaLabel: "popup.settings"});
  const documentObject = fakeDocument({
    "[data-i18n]": [textNode],
    "[data-i18n-title]": [titleNode],
    "[data-i18n-aria-label]": [ariaNode],
  });

  i18n.translateDocument(documentObject, "popup", "zh-CN");

  assert.equal(documentObject.documentElement.attributes.get("lang"), "zh-CN");
  assert.equal(documentObject.title, "Dota 2 监控");
  assert.equal(textNode.textContent, "采集");
  assert.equal(titleNode.attributes.get("title"), "设置");
  assert.equal(ariaNode.attributes.get("aria-label"), "设置");
});

test("explicit selection persists independently and storage changes synchronize pages", async () => {
  const chromeObject = fakeChrome({[i18n.STORAGE_KEY]: "en"});
  const popupSelect = fakeElement();
  const optionsSelect = fakeElement();
  const popupDocument = fakeDocument();
  const optionsDocument = fakeDocument();
  const popupLanguages = [];
  const optionsLanguages = [];

  const popupController = i18n.createLanguageController({
    document: popupDocument,
    page: "popup",
    select: popupSelect,
    chrome: chromeObject,
    navigator: {languages: ["zh-CN"]},
    onLanguageChanged: (language) => popupLanguages.push(language),
  });
  const optionsController = i18n.createLanguageController({
    document: optionsDocument,
    page: "options",
    select: optionsSelect,
    chrome: chromeObject,
    navigator: {languages: ["zh-CN"]},
    onLanguageChanged: (language) => optionsLanguages.push(language),
  });
  await Promise.all([popupController.ready, optionsController.ready]);

  assert.equal(popupSelect.value, "en");
  assert.equal(optionsSelect.value, "en");
  assert.equal(popupDocument.title, "Dota 2 Monitor");

  popupSelect.value = "zh-CN";
  await popupSelect.listeners.get("change")();

  assert.deepEqual(chromeObject.writes, [{[i18n.STORAGE_KEY]: "zh-CN"}]);
  assert.equal(Object.hasOwn(chromeObject.stored, "raybetMonitorConfig"), false);
  assert.equal(optionsSelect.value, "zh-CN");
  assert.equal(optionsDocument.title, "Dota 2 监控设置");
  assert.equal(popupLanguages.at(-1), "zh-CN");
  assert.equal(optionsLanguages.at(-1), "zh-CN");

  popupController.dispose();
  optionsController.dispose();
  assert.equal(chromeObject.listeners.size, 0);
});
