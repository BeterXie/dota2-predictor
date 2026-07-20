(function installRaybetContentBridge(root) {
  "use strict";
  const api = root.RaybetMonitor;
  if (!api?.classifyCandidate || !api?.sanitizeCandidate || !api?.canonicalJson || !api?.sha256Hex) {
    throw new Error("RaybetMonitor helpers must load before the content bridge");
  }

  const encoder = new TextEncoder();
  const classificationState = api.createClassificationState();
  const rawLimit = api.LIMITS.RAW_BYTES + 16 * 1024;
  let config = {
    paused: true,
    enabled: false,
    enabledDomains: {ray086: false, raylinks: false},
    captureSessionId: null,
  };
  let tokens = 60;
  let lastRefill = performance.now();
  let diagnosticTokens = 20;
  let lastDiagnosticRefill = performance.now();

  function safeRuntimeSendMessage(message) {
    try {
      const runtime = root.chrome?.runtime;
      if (typeof runtime?.sendMessage !== "function") return Promise.resolve(null);
      return Promise.resolve(runtime.sendMessage.call(runtime, message)).then(
        (value) => value === undefined ? null : value,
        () => null,
      );
    } catch {
      return Promise.resolve(null);
    }
  }

  function addRuntimeMessageListener(listener) {
    try {
      const onMessage = root.chrome?.runtime?.onMessage;
      if (typeof onMessage?.addListener !== "function") return false;
      onMessage.addListener.call(onMessage, listener);
      return true;
    } catch {
      return false;
    }
  }

  function frameContext() {
    return window.top === undefined || window === window.top ? "top" : "child";
  }

  function takeToken() {
    const now = performance.now();
    tokens = Math.min(60, tokens + ((now - lastRefill) / 1000) * 30);
    lastRefill = now;
    if (tokens < 1) return false;
    tokens -= 1;
    return true;
  }

  function takeDiagnosticToken() {
    const now = performance.now();
    diagnosticTokens = Math.min(20, diagnosticTokens + ((now - lastDiagnosticRefill) / 1000) * 5);
    lastDiagnosticRefill = now;
    if (diagnosticTokens < 1) return false;
    diagnosticTokens -= 1;
    return true;
  }

  function count(counter, amount = 1) {
    void safeRuntimeSendMessage({action: api.ACTIONS.COUNTER, counter, amount});
  }

  function diagnostic(kind, detail = {}) {
    void safeRuntimeSendMessage({
      action: api.ACTIONS.DIAGNOSTIC,
      kind,
      observed_at_utc: new Date().toISOString(),
      frame_context: frameContext(),
      ...detail,
    });
  }

  function ignored(reason) {
    count("ignored");
    diagnostic("classification", {outcome: "ignored", reason});
  }

  function accepted(reason) {
    diagnostic("classification", {outcome: "accepted", reason});
  }

  async function forwardEvent(origin, event) {
    const response = await safeRuntimeSendMessage({
      action: api.ACTIONS.EVENT,
      source_origin: origin,
      event,
    });
    if (response?.accepted) accepted(event.capture_reason || event.event_type);
    else ignored(response?.reason || "service_worker_rejected");
  }

  function safePageOrigin() {
    return ["https://ray086.com", "https://www.ray086.com"].includes(location.origin)
      ? location.origin : null;
  }

  function sourceOrigin(raw) {
    if (raw.source_path === "/manualControlData") return safePageOrigin();
    try {
      const parsed = new URL(raw.source_url);
      if (!["https:", "wss:"].includes(parsed.protocol)
          || !api.RAYBET_HOSTS.includes(parsed.hostname)
          || (parsed.port && parsed.port !== "443")) return null;
      return `https://${parsed.hostname}`;
    } catch {
      return null;
    }
  }

  function sourceEnabled(origin) {
    if (["https://ray086.com", "https://www.ray086.com"].includes(origin)) {
      return Boolean(config.enabledDomains?.ray086);
    }
    if (["https://cfinfo.365raylinks.com", "https://iminfo.esportsworldlink.com"].includes(origin)) {
      return Boolean(config.enabledDomains?.raylinks);
    }
    return false;
  }

  function sourcePath(raw) {
    if (raw.source_path === "/manualControlData") return raw.source_path;
    try {
      return new URL(raw.source_url).pathname;
    } catch {
      return null;
    }
  }

  function parsePayload(raw) {
    if (typeof raw.body_text !== "string") return null;
    try {
      return JSON.parse(raw.body_text);
    } catch {
      return null;
    }
  }

  async function envelopeFor(raw, classified) {
    const sanitized = api.sanitizeCandidate(classified.payload);
    let payload = {};
    let payloadBytes = Number(raw.raw_bytes || 0);
    let payloadHash;
    let reason = classified.captureReason || raw.capture_reason || null;

    if (sanitized.ok) {
      payload = sanitized.value;
      payloadBytes = sanitized.bytes;
      payloadHash = await api.sha256Hex(api.canonicalJson(payload));
    } else if (sanitized.reason === "payload_too_large") {
      reason = "payload_too_large";
      payloadBytes = sanitized.bytes;
      payloadHash = await api.sha256Hex(api.canonicalJson(sanitized.value));
    } else {
      ignored(sanitized.reason || "invalid_envelope");
      return null;
    }

    if (classified.eventType === "unknown") {
      payload = {};
      payloadBytes = Number(raw.raw_bytes || payloadBytes);
      payloadHash = await api.sha256Hex("{}");
      reason = reason || "unknown_structure";
    }
    if (reason === "raw_payload_too_large" || reason === "binary_payload") {
      payload = {};
      payloadHash = await api.sha256Hex("{}");
    }

    const capturedAt = typeof raw.captured_at_utc === "string"
      ? raw.captured_at_utc : new Date().toISOString();
    const eventInput = [
      config.captureSessionId,
      capturedAt,
      String(raw.sequence),
      raw.transport,
      classified.sourcePath,
      classified.raybetMatchId || "",
      payloadHash,
    ].join("\n");
    const eventId = await api.sha256Hex(eventInput);
    return {
      schema_version: api.SCHEMA_VERSION,
      event_id: eventId,
      capture_session_id: config.captureSessionId,
      captured_at_utc: capturedAt,
      page_origin: safePageOrigin(),
      page_path: location.pathname,
      source_path: classified.sourcePath,
      transport: raw.transport,
      event_type: classified.eventType,
      raybet_match_id: classified.raybetMatchId,
      game_id: classified.gameId,
      payload,
      payload_hash: payloadHash,
      payload_bytes: payloadBytes,
      capture_reason: reason,
      extension_version: api.EXTENSION_VERSION,
    };
  }

  async function metadataEnvelope(raw, reason) {
    const matchId = raw.raybet_match_id ? String(raw.raybet_match_id) : null;
    if (!matchId || !classificationState.dotaMatchIds.has(matchId)) {
      ignored("metadata_untrusted_match");
      return null;
    }
    return envelopeFor(raw, {
      eventType: "unknown",
      gameId: api.DOTA2_GAME_ID,
      raybetMatchId: matchId,
      sourcePath: sourcePath(raw) || "/unknown",
      payload: {},
      captureReason: reason,
    });
  }

  async function processRaw(raw) {
    if (!config.enabled || config.paused || !config.captureSessionId) {
      ignored("capture_inactive");
      return;
    }
    if (!api.ALLOWED_TRANSPORTS.includes(raw.transport) || !Number.isInteger(raw.sequence)) {
      ignored("invalid_raw");
      return;
    }
    const origin = sourceOrigin(raw);
    if (!sourceEnabled(origin)) {
      ignored("disabled_source");
      return;
    }
    count("candidates");
    if (raw.capture_reason === "raw_payload_too_large" || raw.capture_reason === "binary_payload") {
      const event = await metadataEnvelope(raw, raw.capture_reason);
      if (event) {
        count("metadataOnly");
        await forwardEvent(origin, event);
      }
      return;
    }
    const payload = parsePayload(raw);
    if (!payload) {
      ignored("invalid_json");
      return;
    }
    const guarded = api.sanitizeCandidate(payload, {SANITIZED_BYTES: api.LIMITS.RAW_BYTES});
    if (!guarded.ok) {
      const event = await metadataEnvelope(raw, guarded.reason || "invalid_candidate");
      if (event) {
        count("metadataOnly");
        await forwardEvent(origin, event);
      }
      return;
    }
    const candidate = {
      transport: raw.transport,
      sourceUrl: raw.source_path ? undefined : raw.source_url,
      sourcePath: raw.source_path,
      raybetMatchId: raw.raybet_match_id,
      payload: guarded.value,
    };
    const result = api.classifyCandidate(candidate, classificationState);
    if (!result.events.length) {
      ignored(result.ignoredReason || "no_events");
      return;
    }
    for (const classified of result.events) {
      const event = await envelopeFor(raw, classified);
      if (!event) continue;
      if (!event.page_origin) {
        ignored("invalid_page_origin");
        continue;
      }
      if (event.capture_reason || event.event_type === "unknown") count("metadataOnly");
      await forwardEvent(origin, event);
    }
  }

  function forwardHookDiagnostic(raw) {
    if (raw.kind === "hook_initialized") {
      diagnostic("hook_initialized", {
        observed_at_utc: raw.observed_at_utc,
        frame_context: raw.frame_context,
      });
      return;
    }
    if (raw.kind !== "transport_observed"
        || !["fetch", "xhr", "websocket"].includes(raw.transport)
        || !api.RAYBET_HOSTS.includes(raw.source_host)
        || typeof raw.source_path !== "string"
        || raw.source_path.length > 512
        || !raw.source_path.startsWith("/")
        || raw.source_path.includes("?")
        || raw.source_path.includes("#")) return;
    diagnostic("transport_observed", {
      observed_at_utc: raw.observed_at_utc,
      frame_context: raw.frame_context,
      transport: raw.transport,
      source_host: raw.source_host,
      source_path: raw.source_path,
      amount: Number.isInteger(raw.amount) && raw.amount >= 1 && raw.amount <= 1000
        ? raw.amount : 1,
    });
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window || typeof event.data !== "string") return;
    if (event.data === api.BRIDGE_READY_CHANNEL) return;
    if (!event.data.startsWith("{") || encoder.encode(event.data).byteLength > rawLimit) {
      count("ignored");
      return;
    }
    let raw;
    try {
      raw = JSON.parse(event.data);
    } catch {
      ignored("invalid_bridge_json");
      return;
    }
    if (raw.channel === api.DIAGNOSTIC_CHANNEL) {
      if (takeDiagnosticToken()) forwardHookDiagnostic(raw);
      return;
    }
    if (raw.channel !== api.HOOK_CHANNEL) return;
    if (!takeToken()) {
      ignored("rate_limited");
      return;
    }
    void processRaw(raw).catch(() => ignored("processing_error"));
  });

  addRuntimeMessageListener((message) => {
    if (message?.action === api.ACTIONS.STATE) {
      config = {
        ...config,
        paused: Boolean(message.paused),
        enabled: Boolean(message.enabled),
        enabledDomains: message.enabledDomains || config.enabledDomains,
      };
    }
  });

  diagnostic("bridge_initialized");
  safeRuntimeSendMessage({action: api.ACTIONS.GET_CONFIG}).then((value) => {
    if (value && typeof value.captureSessionId === "string") config = value;
    diagnostic("bridge_ready", {config_loaded: Boolean(config.captureSessionId)});
    window.postMessage(api.BRIDGE_READY_CHANNEL, "*");
  });

  Object.freeze(api);
})(globalThis);
