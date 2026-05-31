const DEFAULT_BASE = "https://travagentroi-production.up.railway.app";
const baseEl = document.getElementById("base");
const autoEl = document.getElementById("autosend");
const savedEl = document.getElementById("saved");

chrome.storage.sync.get(["backendBase", "autoSend"], (cfg) => {
  baseEl.value = cfg.backendBase || DEFAULT_BASE;
  autoEl.checked = !!cfg.autoSend;
});

document.getElementById("save").addEventListener("click", () => {
  const val = (baseEl.value || DEFAULT_BASE).trim().replace(/\/+$/, "");
  chrome.storage.sync.set({ backendBase: val, autoSend: autoEl.checked }, () => {
    savedEl.textContent = "✓ Sparat";
    setTimeout(() => (savedEl.textContent = ""), 2000);
  });
});
