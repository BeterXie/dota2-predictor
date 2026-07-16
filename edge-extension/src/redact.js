(function installRedaction(root) {
  "use strict";
  const api = root.RaybetMonitor || (root.RaybetMonitor = {});
  const { LIMITS, canonicalJson, utf8ByteLength } = api;
  if (!LIMITS || !canonicalJson || !utf8ByteLength) {
    throw new Error("RaybetMonitor constants and canonical JSON must load before redaction");
  }

const FORBIDDEN_PARTS = Object.freeze([
  "cookie", "authorization", "bearer", "token", "secret", "session", "csrf",
  "apikey", "accesskey", "privatekey", "password", "passwd", "credential", "signature",
  "user", "member", "account", "profile", "username", "phone", "email", "identity",
  "balance", "wallet", "currency", "deposit", "withdrawal", "rebate", "transaction",
  "device", "fingerprint", "advertising", "analytics", "persistentclient", "clientid",
  "visitorid", "browserid", "machineid", "installid",
  "betslip", "selectionslip", "stake", "potentialreturn", "order", "submit", "ticket",
  "requestheader", "responseheader", "requestbody", "formdata", "postbody",
]);

const DANGEROUS_KEYS = new Set(["__proto__", "prototype", "constructor"]);

function normalizeKey(key) {
  return String(key).toLowerCase().replace(/[^a-z0-9]/g, "");
}

function isForbiddenKey(key) {
  if (DANGEROUS_KEYS.has(String(key).toLowerCase())) return true;
  const normalized = normalizeKey(key);
  return FORBIDDEN_PARTS.some((part) => normalized.includes(part));
}

function failure(reason, details = {}) {
  return { ok: false, reason, ...details };
}

function sanitizeCandidate(input, overrides = {}) {
  const limits = { ...LIMITS, ...overrides };
  const holder = { value: null };
  const stack = [{ source: input, parent: holder, key: "value", depth: 0 }];
  const seen = new WeakSet();
  let visitedNodes = 0;
  let redactedKeys = 0;

  while (stack.length > 0) {
    const item = stack.pop();
    visitedNodes += 1;
    if (visitedNodes > limits.MAX_NODES) return failure("max_nodes", { visitedNodes });
    if (item.depth > limits.MAX_DEPTH) return failure("max_depth", { visitedNodes });

    const source = item.source;
    const type = typeof source;
    if (source === null || type === "boolean") {
      item.parent[item.key] = source;
      continue;
    }
    if (type === "number") {
      if (!Number.isFinite(source)) return failure("non_json_value", { visitedNodes });
      item.parent[item.key] = source;
      continue;
    }
    if (type === "string") {
      if (source.length > limits.MAX_STRING_BYTES || utf8ByteLength(source) > limits.MAX_STRING_BYTES) {
        return failure("max_string_bytes", { visitedNodes });
      }
      item.parent[item.key] = source;
      continue;
    }
    if (type !== "object") return failure("non_json_value", { visitedNodes });
    if (seen.has(source)) return failure("cycle", { visitedNodes });
    seen.add(source);

    if (Array.isArray(source)) {
      if (source.length > limits.MAX_ARRAY_ITEMS) {
        return failure("max_array_items", { visitedNodes });
      }
      const target = new Array(source.length);
      item.parent[item.key] = target;
      for (let index = source.length - 1; index >= 0; index -= 1) {
        const descriptor = Object.getOwnPropertyDescriptor(source, String(index));
        if (!descriptor || !("value" in descriptor)) {
          target[index] = null;
          continue;
        }
        stack.push({ source: descriptor.value, parent: target, key: index, depth: item.depth + 1 });
      }
      continue;
    }

    const keys = Object.keys(source);
    if (keys.length > limits.MAX_OBJECT_KEYS) return failure("max_object_keys", { visitedNodes });
    const target = {};
    item.parent[item.key] = target;
    for (let index = keys.length - 1; index >= 0; index -= 1) {
      const key = keys[index];
      if (isForbiddenKey(key)) {
        redactedKeys += 1;
        continue;
      }
      const descriptor = Object.getOwnPropertyDescriptor(source, key);
      if (!descriptor || !("value" in descriptor)) continue;
      Object.defineProperty(target, key, {
        value: null,
        writable: true,
        enumerable: true,
        configurable: true,
      });
      stack.push({ source: descriptor.value, parent: target, key, depth: item.depth + 1 });
    }
  }

  const serialized = canonicalJson(holder.value);
  const bytes = utf8ByteLength(serialized);
  if (bytes > limits.SANITIZED_BYTES) {
    return failure("payload_too_large", {
      value: holder.value,
      bytes,
      visitedNodes,
      redactedKeys,
    });
  }
  return { ok: true, value: holder.value, bytes, visitedNodes, redactedKeys };
}

  Object.assign(api, { normalizeKey, isForbiddenKey, sanitizeCandidate });
})(globalThis);
