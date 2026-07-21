const i18n = globalThis.RaybetI18n;
let currentLanguage = i18n.preferredLanguage();
let saveOutcome = null;

function render(config) {
  document.querySelector("#ray086Enabled").checked = config.enabledDomains?.ray086 !== false;
  document.querySelector("#raylinksEnabled").checked = config.enabledDomains?.raylinks !== false;
}

function renderSaveMessage() {
  const message = document.querySelector("#saveMessage");
  message.textContent = saveOutcome
    ? i18n.translate(currentLanguage, `options.${saveOutcome}`)
    : "";
}

document.querySelector("#saveButton").addEventListener("click", async () => {
  try {
    const result = await chrome.runtime.sendMessage({
      action: "raybet.options.save",
      enabledDomains: {
        ray086: document.querySelector("#ray086Enabled").checked,
        raylinks: document.querySelector("#raylinksEnabled").checked,
      },
    });
    saveOutcome = result?.error ? "saveFailed" : "saved";
  } catch {
    saveOutcome = "saveFailed";
  }
  renderSaveMessage();
});

const languageController = i18n.createLanguageController({
  document,
  page: "options",
  select: document.querySelector("#languageSelect"),
  chrome,
  navigator: globalThis.navigator,
  onLanguageChanged(language) {
    currentLanguage = language;
    renderSaveMessage();
  },
});

void languageController.ready;
void chrome.runtime.sendMessage({action: "raybet.options.get"}).then(render);
