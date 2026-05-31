const DEFAULT_BASE = "https://travagentroi-production.up.railway.app";
const baseEl = document.getElementById("base");
const savedEl = document.getElementById("saved");

chrome.storage.sync.get("backendBase", ({ backendBase }) => {
  baseEl.value = backendBase || DEFAULT_BASE;
});

document.getElementById("save").addEventListener("click", () => {
  const val = (baseEl.value || DEFAULT_BASE).trim().replace(/\/+$/, "");
  chrome.storage.sync.set({ backendBase: val }, () => {
    savedEl.textContent = "✓ Sparat";
    setTimeout(() => (savedEl.textContent = ""), 2000);
  });
});
