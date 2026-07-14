function stateTone(state) {
  if (state === "capturing") return "ok";
  if (state === "buffering" || state === "paused") return "warn";
  return "error";
}

function render(status) {
  const state = status.state || "error";
  const dot = document.querySelector("#stateDot");
  dot.className = stateTone(state);
  document.querySelector("#stateText").textContent = state.replaceAll("_", " ");
  document.querySelector("#captureToggle").checked = !status.paused;
  document.querySelector("#companionValue").textContent =
    status.companion?.reachable && status.companion?.connected
      ? "Connected" : "Unavailable";
  document.querySelector("#matchCount").textContent = String(status.recognizedMatches?.length || 0);
  document.querySelector("#queueCount").textContent = String(status.queue?.length || 0);
  document.querySelector("#dropCount").textContent = String(status.counters?.dropped || 0);
  const last = status.lastEvent;
  document.querySelector("#lastEvent").textContent = last
    ? `${last.eventType} · ${new Date(last.capturedAt).toLocaleTimeString()}` : "None";
  const diagnostics = status.diagnostics || {};
  const initialization = diagnostics.initialization || {};
  const initText = (value) => {
    const top = Number(value?.top || 0);
    const child = Number(value?.child || 0);
    return top || child ? `top ${top} | child ${child}` : "Not seen";
  };
  document.querySelector("#hookStatus").textContent = initText(initialization.hook);
  document.querySelector("#bridgeStatus").textContent = diagnostics.bridgeConfigLoaded === false
    ? "Config failed" : initText(initialization.ready);
  const transports = diagnostics.transports || {};
  document.querySelector("#observedCount").textContent =
    `F ${Number(transports.fetch || 0)} | X ${Number(transports.xhr || 0)} | W ${Number(transports.websocket || 0)}`;
  const classification = diagnostics.classification || {};
  document.querySelector("#classifiedCount").textContent =
    `${Number(classification.accepted || 0)} accepted | ${Number(classification.ignored || 0)} ignored`;
  const observed = diagnostics.lastObserved;
  document.querySelector("#lastObserved").textContent = observed
    ? `${observed.sourceHost}${observed.sourcePath} | ${new Date(observed.observedAt).toLocaleTimeString()}`
    : "None";
  const decision = diagnostics.lastClassification;
  document.querySelector("#lastDecision").textContent = decision
    ? `${decision.outcome}: ${decision.reason}` : "None";
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

document.querySelector("#captureToggle").addEventListener("change", async (event) => {
  await chrome.runtime.sendMessage({
    action: "raybet.popup.setPaused",
    paused: !event.target.checked,
  });
  render(await chrome.runtime.sendMessage({action: "raybet.popup.getStatus"}));
});
document.querySelector("#openOptions").addEventListener("click", () => chrome.runtime.openOptionsPage());
chrome.runtime.sendMessage({action: "raybet.popup.getStatus"}).then(render);
