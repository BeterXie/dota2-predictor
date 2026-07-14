import {CompanionError, fetchCompanionStatus, sendEventBatch} from "./companion-client.js";
import {acknowledge, enqueueBounded, makeBatch, retryDelayMs} from "./queue.js";

const SESSION_KEY = "raybetMonitorSession";
const CONFIG_KEY = "raybetMonitorConfig";
const RETRY_ALARM = "raybet-monitor-delivery";
const SHORT_TIMER_LIMIT_MS = 25_000;

let stateMutex = Promise.resolve();
let deliveryPromise = null;
let retryTimer = null;

function initialDiagnostics() {
  return {
    initialization: {
      hook: {top: 0, child: 0},
      bridge: {top: 0, child: 0},
      ready: {top: 0, child: 0},
    },
    transports: {fetch: 0, xhr: 0, websocket: 0},
    classification: {accepted: 0, ignored: 0, ignoredReasons: {}},
    bridgeConfigLoaded: null,
    lastObserved: null,
    lastClassification: null,
    lastUpdatedAt: null,
  };
}

function randomHex(bytes) {
  return [...crypto.getRandomValues(new Uint8Array(bytes))]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

function initialState() {
  return {
    captureSessionId: randomHex(16),
    queue: [],
    counters: {
      accepted: 0,
      acknowledged: 0,
      candidates: 0,
      dropped: 0,
      ignored: 0,
      metadataOnly: 0,
      rejected: 0,
      retries: 0,
    },
    paused: false,
    state: "buffering",
    retryAttempt: 0,
    nextRetryAt: null,
    recognizedMatches: [],
    lastEvent: null,
    diagnostics: initialDiagnostics(),
    companion: {reachable: false, connected: false, lastError: null},
  };
}

function initialConfig() {
  return {
    enabledDomains: {ray086: true, raylinks: true},
  };
}

function serialized(task) {
  const next = stateMutex.then(task, task);
  stateMutex = next.catch(() => undefined);
  return next;
}

async function readState() {
  const stored = await chrome.storage.session.get(SESSION_KEY);
  const state = stored[SESSION_KEY] || initialState();
  const defaults = initialDiagnostics();
  state.diagnostics = {
    ...defaults,
    ...state.diagnostics,
    initialization: {
      hook: {...defaults.initialization.hook, ...state.diagnostics?.initialization?.hook},
      bridge: {...defaults.initialization.bridge, ...state.diagnostics?.initialization?.bridge},
      ready: {...defaults.initialization.ready, ...state.diagnostics?.initialization?.ready},
    },
    transports: {...defaults.transports, ...state.diagnostics?.transports},
    classification: {
      ...defaults.classification,
      ...state.diagnostics?.classification,
      ignoredReasons: {
        ...defaults.classification.ignoredReasons,
        ...state.diagnostics?.classification?.ignoredReasons,
      },
    },
  };
  return state;
}

async function writeState(state) {
  await chrome.storage.session.set({[SESSION_KEY]: state});
  return state;
}

async function readConfig() {
  const stored = await chrome.storage.local.get(CONFIG_KEY);
  const current = stored[CONFIG_KEY] || {};
  return {
    enabledDomains: {
      ray086: current.enabledDomains?.ray086 !== false,
      raylinks: current.enabledDomains?.raylinks !== false,
    },
  };
}

async function writeConfig(config) {
  await chrome.storage.local.set({[CONFIG_KEY]: config});
  return config;
}

async function initialize() {
  await Promise.all([
    chrome.storage.local.setAccessLevel?.({accessLevel: "TRUSTED_CONTEXTS"}),
    chrome.storage.session.setAccessLevel?.({accessLevel: "TRUSTED_CONTEXTS"}),
  ]);
  await writeConfig(await readConfig());
  await serialized(async () => {
    const state = await readState();
    await writeState(state);
  });
  void deliverQueued();
}

function acknowledgedIds(payload) {
  if (Array.isArray(payload.results)) {
    return payload.results.map((item) => item.event_id).filter(Boolean);
  }
  return [
    ...(payload.accepted || []),
    ...(payload.duplicates || []),
    ...(payload.rejected || []),
  ];
}

async function scheduleRetry(error) {
  const delay = await serialized(async () => {
    const state = await readState();
    const value = retryDelayMs(state.retryAttempt);
    state.retryAttempt += 1;
    state.nextRetryAt = Date.now() + value;
    state.counters.retries += 1;
    state.state = state.paused ? "paused" : "buffering";
    state.companion = {
      reachable: error instanceof CompanionError ? error.status > 0 : false,
      connected: false,
      lastError: error.code || error.name || "delivery_error",
    };
    await writeState(state);
    return value;
  });
  clearTimeout(retryTimer);
  if (delay <= SHORT_TIMER_LIMIT_MS) {
    retryTimer = setTimeout(() => void deliverQueued(), delay);
  }
  await chrome.alarms.create(RETRY_ALARM, {when: Date.now() + Math.max(delay, 30_000)});
}

async function deliverQueued() {
  if (deliveryPromise) {
    return deliveryPromise;
  }
  deliveryPromise = (async () => {
    const snapshot = await serialized(async () => {
      const state = await readState();
      if (state.paused) {
        return null;
      }
      return makeBatch(state.queue).events;
    });
    if (!snapshot || snapshot.length === 0) {
      return;
    }
    try {
      const response = await sendEventBatch(snapshot);
      const ids = acknowledgedIds(response);
      await serialized(async () => {
        const state = await readState();
        state.queue = acknowledge(state.queue, ids);
        state.counters.acknowledged += ids.length;
        state.retryAttempt = 0;
        state.nextRetryAt = null;
        state.state = state.paused ? "paused" : "capturing";
        state.companion = {reachable: true, connected: true, lastError: null};
        await writeState(state);
      });
      clearTimeout(retryTimer);
      await chrome.alarms.clear(RETRY_ALARM);
      const remaining = await serialized(async () => (await readState()).queue.length);
      if (remaining) {
        setTimeout(() => void deliverQueued(), 0);
      }
    } catch (error) {
      await scheduleRetry(error);
    }
  })().finally(() => {
    deliveryPromise = null;
  });
  return deliveryPromise;
}

async function enqueueEvent(event) {
  await serialized(async () => {
    const state = await readState();
    if (state.paused) {
      return;
    }
    const result = enqueueBounded(state.queue, event);
    state.queue = result.events;
    state.counters.accepted += 1;
    state.counters.dropped += result.dropped;
    state.lastEvent = {eventType: event.event_type, capturedAt: event.captured_at_utc};
    if (event.raybet_match_id && !state.recognizedMatches.includes(event.raybet_match_id)) {
      state.recognizedMatches.push(event.raybet_match_id);
    }
    state.state = "buffering";
    await writeState(state);
  });
  void deliverQueued();
}

function captureSenderAllowed(senderUrl) {
  try {
    const url = new URL(senderUrl || "");
    return url.protocol === "https:" && (
      url.hostname === "ray086.com"
      || url.hostname === "www.ray086.com"
      || url.hostname === "cfinfo.365raylinks.com"
      || url.hostname === "iminfo.esportsworldlink.com"
    );
  } catch {
    return false;
  }
}

function sourceOriginEnabled(sourceOrigin, config) {
  try {
    const url = new URL(sourceOrigin || "");
    if (url.origin !== sourceOrigin || url.protocol !== "https:") return false;
    if (["ray086.com", "www.ray086.com"].includes(url.hostname)) {
      return Boolean(config.enabledDomains.ray086);
    }
    if (["cfinfo.365raylinks.com", "iminfo.esportsworldlink.com"].includes(url.hostname)) {
      return Boolean(config.enabledDomains.raylinks);
    }
  } catch {
    return false;
  }
  return false;
}

function diagnosticTime(value) {
  return typeof value === "string" && value.length <= 64 && Number.isFinite(Date.parse(value))
    ? value : new Date().toISOString();
}

function updateDiagnostics(state, message) {
  const diagnostics = state.diagnostics;
  const frame = message.frame_context === "child" ? "child" : "top";
  const at = diagnosticTime(message.observed_at_utc);
  if (["hook_initialized", "bridge_initialized", "bridge_ready"].includes(message.kind)) {
    const key = {
      hook_initialized: "hook",
      bridge_initialized: "bridge",
      bridge_ready: "ready",
    }[message.kind];
    if (message.kind === "bridge_ready") {
      if (typeof message.config_loaded !== "boolean") return false;
      diagnostics.bridgeConfigLoaded = message.config_loaded;
    }
    diagnostics.initialization[key][frame] += 1;
  } else if (message.kind === "transport_observed") {
    if (!["fetch", "xhr", "websocket"].includes(message.transport)) return false;
    const amount = Number.isInteger(message.amount) && message.amount >= 1 && message.amount <= 1000
      ? message.amount : 1;
    if (typeof message.source_host !== "string"
        || !["ray086.com", "www.ray086.com", "cfinfo.365raylinks.com",
          "iminfo.esportsworldlink.com"].includes(message.source_host)
        || typeof message.source_path !== "string"
        || message.source_path.length > 512
        || !message.source_path.startsWith("/")
        || /[?#\0]/.test(message.source_path)) return false;
    diagnostics.transports[message.transport] += amount;
    diagnostics.lastObserved = {
      transport: message.transport,
      sourceHost: message.source_host,
      sourcePath: message.source_path,
      observedAt: at,
      frameContext: frame,
    };
  } else if (message.kind === "classification") {
    if (!["accepted", "ignored"].includes(message.outcome)) return false;
    const dangerousReasons = ["__proto__", "prototype", "constructor"];
    const reason = typeof message.reason === "string"
      && /^[a-z0-9_]{1,64}$/.test(message.reason)
      && !dangerousReasons.includes(message.reason)
      ? message.reason : "unknown";
    diagnostics.classification[message.outcome] += 1;
    if (message.outcome === "ignored") {
      const reasons = diagnostics.classification.ignoredReasons;
      if (Object.hasOwn(reasons, reason) || Object.keys(reasons).length < 32) {
        reasons[reason] = Number(reasons[reason] || 0) + 1;
      }
    }
    diagnostics.lastClassification = {outcome: message.outcome, reason, observedAt: at};
  } else {
    return false;
  }
  diagnostics.lastUpdatedAt = at;
  return true;
}

function captureStateMessage(state, config) {
  const enabledDomains = {...config.enabledDomains};
  return {
    action: "raybet.capture.state",
    paused: state.paused,
    enabled: Object.values(enabledDomains).some(Boolean),
    enabledDomains,
  };
}

async function broadcastCaptureState(state, config = null) {
  config = config || await readConfig();
  const tabs = await chrome.tabs.query({});
  const message = captureStateMessage(state, config);
  await Promise.all(
    tabs.map((tab) => chrome.tabs.sendMessage(tab.id, message).catch(() => undefined)),
  );
}

async function handleMessage(message, sender) {
  switch (message?.action) {
    case "raybet.capture.getConfig": {
      const [state, config] = await Promise.all([readState(), readConfig()]);
      return {
        paused: state.paused,
        enabled: captureSenderAllowed(sender.url)
          && Object.values(config.enabledDomains).some(Boolean),
        enabledDomains: {...config.enabledDomains},
        captureSessionId: state.captureSessionId,
      };
    }
    case "raybet.capture.event": {
      const config = await readConfig();
      if (!captureSenderAllowed(sender.url)
          || !sourceOriginEnabled(message.source_origin, config)) {
        return {accepted: false, reason: "disabled_source"};
      }
      await enqueueEvent(message.event);
      return {accepted: true};
    }
    case "raybet.capture.counter":
      await serialized(async () => {
        const state = await readState();
        const key = message.counter;
        if (Object.hasOwn(state.counters, key)) state.counters[key] += Number(message.amount || 1);
        await writeState(state);
      });
      return {accepted: true};
    case "raybet.capture.diagnostic": {
      if (!captureSenderAllowed(sender.url)) return {accepted: false, reason: "invalid_sender"};
      const accepted = await serialized(async () => {
        const state = await readState();
        const updated = updateDiagnostics(state, message);
        if (updated) await writeState(state);
        return updated;
      });
      return {accepted, reason: accepted ? null : "invalid_diagnostic"};
    }
    case "raybet.popup.getStatus": {
      const state = await readState();
      let remote = null;
      let companion = {...state.companion};
      try {
        remote = await fetchCompanionStatus();
        companion = {reachable: true, connected: true, lastError: null};
      } catch (error) {
        remote = null;
        companion = {
          reachable: error instanceof CompanionError ? error.status > 0 : false,
          connected: false,
          lastError: error.code || error.name || "status_error",
        };
      }
      return {...state, companion, remote};
    }
    case "raybet.popup.setPaused": {
      const state = await serialized(async () => {
        const current = await readState();
        current.paused = Boolean(message.paused);
        current.state = current.paused ? "paused" : "buffering";
        return writeState(current);
      });
      await broadcastCaptureState(state);
      if (!state.paused) void deliverQueued();
      return {paused: state.paused};
    }
    case "raybet.options.get":
      return readConfig();
    case "raybet.options.save": {
      const current = await readConfig();
      const config = await writeConfig({
        ...current,
        enabledDomains: {
          ray086: Boolean(message.enabledDomains?.ray086),
          raylinks: Boolean(message.enabledDomains?.raylinks),
        },
      });
      await broadcastCaptureState(await readState(), config);
      return config;
    }
    default:
      return {error: "unknown_action"};
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handleMessage(message, sender).then(sendResponse, (error) => {
    sendResponse({error: error.code || error.message || "extension_error"});
  });
  return true;
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === RETRY_ALARM) void deliverQueued();
});
chrome.runtime.onInstalled.addListener(() => void initialize());
chrome.runtime.onStartup.addListener(() => void initialize());
void initialize();
