(function installRaybetCaptureHook() {
  "use strict";

  const HOOK_CHANNEL = "dota2-raybet-capture-v1";
  const DIAGNOSTIC_CHANNEL = "dota2-raybet-diagnostic-v1";
  const BRIDGE_READY_CHANNEL = "dota2-raybet-bridge-ready-v1";
  const RAW_LIMIT = 1024 * 1024;
  const EARLY_LIMIT_COUNT = 60;
  const EARLY_LIMIT_BYTES = 1024 * 1024;
  const DIAGNOSTIC_FLUSH_MS = 250;
  const ALLOWED_HOSTS = new Set([
    "ray086.com",
    "www.ray086.com",
    "cfinfo.365raylinks.com",
    "iminfo.esportsworldlink.com",
  ]);
  const encoder = new TextEncoder();
  const decoder = new TextDecoder("utf-8", {fatal: false});
  const early = [];
  let earlyBytes = 0;
  let bridgeReady = false;
  let sequence = 0;
  let activeDotaMatchId = null;
  let lastDotaOddsAt = 0;
  const pendingTransportDiagnostics = new Map();
  let diagnosticFlushTimer = null;

  function ownDataValue(object, key) {
    if (object === null || (typeof object !== "object" && typeof object !== "function")) return undefined;
    const descriptor = Object.getOwnPropertyDescriptor(object, key);
    if (!descriptor || !("value" in descriptor)) return undefined;
    return descriptor.value;
  }

  function parseUrl(value) {
    try {
      if (typeof value === "string" || value instanceof URL) return new URL(String(value), location.href);
      if (value && typeof value.url === "string") return new URL(value.url, location.href);
    } catch {
      return null;
    }
    return null;
  }

  function allowedUrl(url) {
    return Boolean(url && ALLOWED_HOSTS.has(url.hostname));
  }

  function publicSourceUrl(value) {
    const url = value instanceof URL ? value : parseUrl(value);
    return url ? `${url.origin}${url.pathname}` : "";
  }

  function relevantHttpUrl(url) {
    return allowedUrl(url) && (url.pathname === "/v2/match" || url.pathname === "/v2/odds");
  }

  function candidateString(candidate) {
    const safeCandidate = {...candidate};
    if (Object.hasOwn(safeCandidate, "source_url")) {
      safeCandidate.source_url = publicSourceUrl(safeCandidate.source_url);
    }
    return JSON.stringify({
      channel: HOOK_CHANNEL,
      sequence: sequence += 1,
      captured_at_utc: new Date().toISOString(),
      ...safeCandidate,
    });
  }

  function postSerialized(serialized) {
    window.postMessage(serialized, "*");
  }

  function emitSerialized(serialized) {
    const bytes = encoder.encode(serialized).byteLength;
    if (bridgeReady) {
      postSerialized(serialized);
      return;
    }
    while (early.length >= EARLY_LIMIT_COUNT || earlyBytes + bytes > EARLY_LIMIT_BYTES) {
      const removed = early.shift();
      if (!removed) break;
      earlyBytes -= removed.bytes;
    }
    if (bytes <= EARLY_LIMIT_BYTES) {
      early.push({serialized, bytes});
      earlyBytes += bytes;
    }
  }

  function emitCandidate(candidate) {
    let serialized = candidateString(candidate);
    const bytes = encoder.encode(serialized).byteLength;
    if (bytes > RAW_LIMIT + 16 * 1024) {
      serialized = candidateString({
        transport: candidate.transport,
        source_url: publicSourceUrl(candidate.source_url),
        raybet_match_id: candidate.raybet_match_id || null,
        body_text: null,
        raw_bytes: candidate.raw_bytes || bytes,
        capture_reason: "raw_payload_too_large",
      });
    }
    emitSerialized(serialized);
  }

  function emitDiagnostic(kind, detail = {}) {
    emitSerialized(JSON.stringify({
      channel: DIAGNOSTIC_CHANNEL,
      kind,
      observed_at_utc: new Date().toISOString(),
      frame_context: window.top === undefined || window === window.top ? "top" : "child",
      ...detail,
    }));
  }

  function emitTransportObserved(transport, url) {
    if (!allowedUrl(url)) return;
    const knownPath = ["/v2/match", "/v2/odds", "/live"].includes(url.pathname)
      ? url.pathname : "/other";
    const key = `${transport}\n${url.hostname}\n${knownPath}`;
    const current = pendingTransportDiagnostics.get(key) || {
      transport,
      source_host: url.hostname,
      source_path: knownPath,
      amount: 0,
    };
    current.amount = Math.min(1000, current.amount + 1);
    pendingTransportDiagnostics.set(key, current);
    if (diagnosticFlushTimer !== null) return;
    diagnosticFlushTimer = setTimeout(() => {
      diagnosticFlushTimer = null;
      for (const detail of pendingTransportDiagnostics.values()) {
        emitDiagnostic("transport_observed", detail);
      }
      pendingTransportDiagnostics.clear();
    }, DIAGNOSTIC_FLUSH_MS);
  }

  function markActiveDota(sourceUrl, bodyText) {
    const url = parseUrl(sourceUrl);
    if (!url || url.pathname !== "/v2/odds" || !bodyText) return;
    try {
      const payload = JSON.parse(bodyText);
      const result = payload && payload.result;
      if (Number(result?.game_id) !== 151) return;
      activeDotaMatchId = String(result.id || url.searchParams.get("match_id") || "") || null;
      lastDotaOddsAt = Date.now();
    } catch {
      // The isolated bridge owns validation; this only gates the fixed diagnostic sampler.
    }
  }

  function emitText(transport, sourceUrl, bodyText, rawBytes, captureReason = null) {
    markActiveDota(sourceUrl, bodyText);
    const url = parseUrl(sourceUrl);
    emitCandidate({
      transport,
      source_url: publicSourceUrl(url),
      raybet_match_id: url?.searchParams.get("match_id") || activeDotaMatchId,
      body_text: bodyText,
      raw_bytes: rawBytes,
      capture_reason: captureReason,
    });
  }

  async function readFetchClone(response, sourceUrl) {
    try {
      const contentLength = Number(response.headers?.get?.("content-length") || 0);
      if (contentLength > RAW_LIMIT) {
        emitText("fetch", sourceUrl, null, contentLength, "raw_payload_too_large");
        return;
      }
      if (!response.body?.getReader) {
        const text = await response.text();
        const bytes = encoder.encode(text).byteLength;
        emitText("fetch", sourceUrl, bytes > RAW_LIMIT ? null : text, bytes,
          bytes > RAW_LIMIT ? "raw_payload_too_large" : null);
        return;
      }
      const reader = response.body.getReader();
      const chunks = [];
      let total = 0;
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        total += value.byteLength;
        if (total > RAW_LIMIT) {
          await reader.cancel();
          emitText("fetch", sourceUrl, null, total, "raw_payload_too_large");
          return;
        }
        chunks.push(value);
      }
      const joined = new Uint8Array(total);
      let offset = 0;
      for (const chunk of chunks) {
        joined.set(chunk, offset);
        offset += chunk.byteLength;
      }
      emitText("fetch", sourceUrl, decoder.decode(joined), total);
    } catch {
      // Capture failures must never affect the page's original response.
    }
  }

  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    function wrappedFetch(...args) {
      const result = Reflect.apply(originalFetch, this, args);
      Promise.resolve(result).then((response) => {
        const sourceUrl = response?.url ? parseUrl(response.url) : parseUrl(args[0]);
        if (!response || !relevantHttpUrl(sourceUrl)) return;
        emitTransportObserved("fetch", sourceUrl);
        void readFetchClone(response.clone(), sourceUrl.href);
      }).catch(() => undefined);
      return result;
    }
    Object.setPrototypeOf(wrappedFetch, Object.getPrototypeOf(originalFetch));
    window.fetch = wrappedFetch;
  }

  const xhrMeta = new WeakMap();
  const Xhr = window.XMLHttpRequest;
  if (Xhr?.prototype) {
    const originalOpen = Xhr.prototype.open;
    const originalSend = Xhr.prototype.send;
    Xhr.prototype.open = function wrappedOpen(method, url, ...rest) {
      xhrMeta.set(this, {method, url: parseUrl(url)});
      return Reflect.apply(originalOpen, this, [method, url, ...rest]);
    };
    Xhr.prototype.send = function wrappedSend(...args) {
      const meta = xhrMeta.get(this);
      if (meta?.url && relevantHttpUrl(meta.url)) {
        this.addEventListener("loadend", () => queueMicrotask(() => {
          try {
            emitTransportObserved("xhr", meta.url);
            let text = null;
            if (!this.responseType || this.responseType === "text") text = this.responseText;
            else if (this.responseType === "json") text = JSON.stringify(this.response);
            if (typeof text !== "string") return;
            const bytes = encoder.encode(text).byteLength;
            emitText("xhr", meta.url.href, bytes > RAW_LIMIT ? null : text, bytes,
              bytes > RAW_LIMIT ? "raw_payload_too_large" : null);
          } catch {
            // XHR observation is best effort and isolated from page events.
          }
        }), {once: true});
      }
      return Reflect.apply(originalSend, this, args);
    };
  }

  const OriginalWebSocket = window.WebSocket;
  if (typeof OriginalWebSocket === "function") {
    function WrappedWebSocket(...args) {
      if (!new.target) return Reflect.apply(OriginalWebSocket, this, args);
      const target = new.target === WrappedWebSocket ? OriginalWebSocket : new.target;
      const socket = Reflect.construct(OriginalWebSocket, args, target);
      const url = parseUrl(args[0]);
      if (allowedUrl(url)) {
        socket.addEventListener("message", (event) => {
          emitTransportObserved("websocket", url);
          if (typeof event.data === "string") {
            const bytes = encoder.encode(event.data).byteLength;
            emitText("websocket", url.href, bytes > RAW_LIMIT ? null : event.data, bytes,
              bytes > RAW_LIMIT ? "raw_payload_too_large" : null);
          } else {
            const bytes = Number(event.data?.byteLength || event.data?.size || 0);
            emitText("websocket", url.href, null, bytes, "binary_payload");
          }
        });
      }
      return socket;
    }
    Object.setPrototypeOf(WrappedWebSocket, OriginalWebSocket);
    WrappedWebSocket.prototype = OriginalWebSocket.prototype;
    for (const name of ["CONNECTING", "OPEN", "CLOSING", "CLOSED"]) {
      const descriptor = Object.getOwnPropertyDescriptor(OriginalWebSocket, name);
      if (descriptor) Object.defineProperty(WrappedWebSocket, name, descriptor);
    }
    window.WebSocket = WrappedWebSocket;
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window || event.data !== BRIDGE_READY_CHANNEL || bridgeReady) return;
    bridgeReady = true;
    for (const item of early.splice(0)) postSerialized(item.serialized);
    earlyBytes = 0;
  });

  emitDiagnostic("hook_initialized");

  setInterval(() => {
    if (!activeDotaMatchId || Date.now() - lastDotaOddsAt > 30_000 || document.visibilityState !== "visible") return;
    const root = ownDataValue(window, "manualControlData");
    const index = ownDataValue(root, "currentIndex");
    const data = ownDataValue(root, "data");
    if (!Number.isInteger(index) || !Array.isArray(data)) return;
    const row = ownDataValue(data, String(index));
    const time = ownDataValue(row, "time");
    if (!["string", "number"].includes(typeof time)) return;
    const body = JSON.stringify({currentIndex: index, time});
    emitCandidate({
      transport: "page_state",
      source_url: `${location.origin}${location.pathname}`,
      source_path: "/manualControlData",
      raybet_match_id: activeDotaMatchId,
      body_text: body,
      raw_bytes: encoder.encode(body).byteLength,
      capture_reason: "diagnostic_untrusted",
    });
  }, 5000);
})();
