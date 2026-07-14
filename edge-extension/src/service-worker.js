import {CompanionError, fetchCompanionStatus, sendEventBatch} from "./companion-client.js";
import {acknowledge, enqueueBounded, makeBatch, retryDelayMs} from "./queue.js";

const SESSION_KEY = "raybetMonitorSession";
const CONFIG_KEY = "raybetMonitorConfig";
const RETRY_ALARM = "raybet-monitor-delivery";
const SHORT_TIMER_LIMIT_MS = 25_000;

let stateMutex = Promise.resolve();
let deliveryPromise = null;
let retryTimer = null;

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
    companion: {reachable: false, connected: false, lastError: null},
  };
}

function initialConfig() {
  return {
    enabledDomains: {ray086: true, raylinks: true},
    debugLevel: "errors",
  };
}

function serialized(task) {
  const next = stateMutex.then(task, task);
  stateMutex = next.catch(() => undefined);
  return next;
}

async function readState() {
  const stored = await chrome.storage.session.get(SESSION_KEY);
  return stored[SESSION_KEY] || initialState();
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
    debugLevel: ["off", "errors", "diagnostic"].includes(current.debugLevel)
      ? current.debugLevel : "errors",
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
      url.hostname === "www.ray086.com" || url.hostname === "cfinfo.365raylinks.com"
    );
  } catch {
    return false;
  }
}

function sourceOriginEnabled(sourceOrigin, config) {
  try {
    const url = new URL(sourceOrigin || "");
    if (url.origin !== sourceOrigin || url.protocol !== "https:") return false;
    if (url.hostname === "www.ray086.com") return Boolean(config.enabledDomains.ray086);
    if (url.hostname === "cfinfo.365raylinks.com") {
      return Boolean(config.enabledDomains.raylinks);
    }
  } catch {
    return false;
  }
  return false;
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
      const debugLevel = ["off", "errors", "diagnostic"].includes(message.debugLevel)
        ? message.debugLevel : current.debugLevel;
      const config = await writeConfig({
        ...current,
        enabledDomains: {
          ray086: Boolean(message.enabledDomains?.ray086),
          raylinks: Boolean(message.enabledDomains?.raylinks),
        },
        debugLevel,
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
