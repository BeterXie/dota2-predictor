function render(config) {
  document.querySelector("#ray086Enabled").checked = config.enabledDomains?.ray086 !== false;
  document.querySelector("#raylinksEnabled").checked = config.enabledDomains?.raylinks !== false;
}

document.querySelector("#saveButton").addEventListener("click", async () => {
  const result = await chrome.runtime.sendMessage({
    action: "raybet.options.save",
    enabledDomains: {
      ray086: document.querySelector("#ray086Enabled").checked,
      raylinks: document.querySelector("#raylinksEnabled").checked,
    },
  });
  document.querySelector("#saveMessage").textContent = result.error ? "Save failed" : "Saved";
});

chrome.runtime.sendMessage({action: "raybet.options.get"}).then(render);
