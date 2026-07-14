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
  document.querySelector("#companionValue").textContent = status.paired
    ? (status.companion?.reachable && status.companion?.authenticated
      ? "Connected" : "Unavailable") : "Not paired";
  document.querySelector("#matchCount").textContent = String(status.recognizedMatches?.length || 0);
  document.querySelector("#queueCount").textContent = String(status.queue?.length || 0);
  document.querySelector("#dropCount").textContent = String(status.counters?.dropped || 0);
  const last = status.lastEvent;
  document.querySelector("#lastEvent").textContent = last
    ? `${last.eventType} · ${new Date(last.capturedAt).toLocaleTimeString()}` : "None";
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
