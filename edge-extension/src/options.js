function render(config) {
  document.querySelector("#pairState").textContent = config.paired ? "Paired" : "Not paired";
  document.querySelector("#ray086Enabled").checked = config.enabledDomains?.ray086 !== false;
  document.querySelector("#raylinksEnabled").checked = config.enabledDomains?.raylinks !== false;
  document.querySelector("#debugLevel").value = config.debugLevel || "errors";
}

document.querySelector("#pairButton").addEventListener("click", async () => {
  const message = document.querySelector("#pairMessage");
  message.textContent = "Pairing";
  const result = await chrome.runtime.sendMessage({
    action: "raybet.options.pair",
    code: document.querySelector("#pairCode").value,
  });
  message.textContent = result.error ? `Pairing failed: ${result.error}` : "Paired";
  if (!result.error) document.querySelector("#pairState").textContent = "Paired";
});

document.querySelector("#saveButton").addEventListener("click", async () => {
  const result = await chrome.runtime.sendMessage({
    action: "raybet.options.save",
    enabledDomains: {
      ray086: document.querySelector("#ray086Enabled").checked,
      raylinks: document.querySelector("#raylinksEnabled").checked,
    },
    debugLevel: document.querySelector("#debugLevel").value,
  });
  document.querySelector("#saveMessage").textContent = result.error ? "Save failed" : "Saved";
});

chrome.runtime.sendMessage({action: "raybet.options.get"}).then(render);
