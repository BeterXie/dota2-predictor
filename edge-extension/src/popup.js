const i18n = globalThis.RaybetI18n;
let currentLanguage = i18n.preferredLanguage();
let currentStatus = null;

function stateTone(state) {
  if (state === "capturing") return "ok";
  if ([
    "backpressure",
    "buffering",
    "companion_offline",
    "paused",
    "reconnecting",
    "waiting_for_traffic",
  ].includes(state)) return "warn";
  return "error";
}

function text(key) {
  return i18n.translate(currentLanguage, key);
}

function code(category, value) {
  return i18n.translateCode(currentLanguage, category, value);
}

function time(value) {
  return new Date(value).toLocaleTimeString(currentLanguage);
}

function render(status) {
  currentStatus = status;
  const state = status.captureStatus || status.state || "error";
  const dot = document.querySelector("#stateDot");
  const stateText = document.querySelector("#stateText");
  dot.className = stateTone(state);
  stateText.textContent = code("captureStatus", state);
  stateText.title = code("statusReason", status.statusReason);
  document.querySelector("#captureToggle").checked = !status.paused;
  document.querySelector("#companionValue").textContent =
    status.companion?.reachable && status.companion?.connected
      ? text("common.connected") : text("common.unavailable");
  document.querySelector("#matchCount").textContent = String(status.recognizedMatches?.length || 0);
  document.querySelector("#queueCount").textContent = String(status.queue?.length || 0);
  document.querySelector("#dropCount").textContent = String(status.counters?.dropped || 0);
  const last = status.lastEvent;
  document.querySelector("#lastEvent").textContent = last
    ? i18n.format(currentLanguage, "popup.lastEventValue", {
      eventType: code("eventType", last.eventType),
      time: time(last.capturedAt),
    }) : text("common.none");
  const diagnostics = status.diagnostics || {};
  const initialization = diagnostics.initialization || {};
  const initText = (value) => {
    const top = Number(value?.top || 0);
    const child = Number(value?.child || 0);
    return top || child
      ? i18n.format(currentLanguage, "popup.initialization", {top, child})
      : text("common.notSeen");
  };
  document.querySelector("#hookStatus").textContent = initText(initialization.hook);
  document.querySelector("#bridgeStatus").textContent = diagnostics.bridgeConfigLoaded === false
    ? text("common.configFailed") : initText(initialization.ready);
  const transports = diagnostics.transports || {};
  document.querySelector("#observedCount").textContent =
    i18n.format(currentLanguage, "popup.observedCounts", {
      fetch: Number(transports.fetch || 0),
      xhr: Number(transports.xhr || 0),
      websocket: Number(transports.websocket || 0),
    });
  const classification = diagnostics.classification || {};
  document.querySelector("#classifiedCount").textContent =
    i18n.format(currentLanguage, "popup.classifiedCounts", {
      accepted: Number(classification.accepted || 0),
      ignored: Number(classification.ignored || 0),
    });
  const observed = diagnostics.lastObserved;
  document.querySelector("#lastObserved").textContent = observed
    ? i18n.format(currentLanguage, "popup.lastObservedValue", {
      host: observed.sourceHost,
      path: observed.sourcePath,
      time: time(observed.observedAt),
    }) : text("common.none");
  const decision = diagnostics.lastClassification;
  document.querySelector("#lastDecision").textContent = decision
    ? i18n.format(currentLanguage, "popup.lastDecisionValue", {
      outcome: code("outcome", decision.outcome),
      reason: code("reason", decision.reason),
    }) : text("common.none");
  const report = status.remote?.report_url;
  const link = document.querySelector("#reportLink");
  if (report && /^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//.test(report)) {
    link.href = report;
    link.hidden = false;
  } else {
    link.href = "";
    link.hidden = true;
  }
}

function unavailableStatus() {
  return {
    state: "error",
    captureStatus: "degraded",
    statusReason: "extension_status_unavailable",
    paused: false,
    companion: {reachable: false, connected: false},
    recognizedMatches: [],
    queue: [],
    counters: {dropped: 0},
    diagnostics: {},
    remote: null,
  };
}

async function getStatus() {
  try {
    return await chrome.runtime.sendMessage({action: "raybet.popup.getStatus"});
  } catch {
    return unavailableStatus();
  }
}

document.querySelector("#captureToggle").addEventListener("change", async (event) => {
  try {
    await chrome.runtime.sendMessage({
      action: "raybet.popup.setPaused",
      paused: !event.target.checked,
    });
  } catch {
    // The status refresh below renders the invalidated extension context.
  }
  render(await getStatus());
});
document.querySelector("#openOptions").addEventListener("click", () => chrome.runtime.openOptionsPage());
const languageController = i18n.createLanguageController({
  document,
  page: "popup",
  select: document.querySelector("#languageSelect"),
  chrome,
  navigator: globalThis.navigator,
  onLanguageChanged(language) {
    currentLanguage = language;
    if (currentStatus) render(currentStatus);
  },
});
void Promise.all([languageController.ready, getStatus()]).then(([, status]) => render(status));
