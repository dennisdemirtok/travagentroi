const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
const btnThis = document.getElementById("btn-this");
const btnAll = document.getElementById("btn-all");

function send(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
}

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = cls || "";
}

function renderOne(r) {
  if (r.ok) {
    return `✅ ${r.game_type} ${r.day}${r.track ? " · " + r.track : ""} — ${r.interviews_count} intervjuer`;
  }
  const who = r.tabTitle ? `"${r.tabTitle.slice(0, 30)}…" ` : "";
  return `⚠️ ${who}${r.error || "misslyckades"}`;
}

btnThis.addEventListener("click", async () => {
  btnThis.disabled = true;
  resultsEl.innerHTML = "";
  setStatus("⏳ Hämtar & tolkar sidan…");
  const r = await send({ type: "ingestActive" });
  setStatus(renderOne(r), r.ok ? "ok" : "err");
  btnThis.disabled = false;
});

btnAll.addEventListener("click", async () => {
  btnAll.disabled = true;
  resultsEl.innerHTML = "";
  setStatus("⏳ Söker öppna tipsflikar…");
  const out = await send({ type: "ingestAll" });
  if (!out || !out.count) {
    setStatus("Inga öppna tipsflikar hittades.", "err");
    btnAll.disabled = false;
    return;
  }
  const okN = out.results.filter((r) => r.ok).length;
  setStatus(`Klart — ${okN}/${out.count} flikar sparade.`, okN ? "ok" : "err");
  resultsEl.innerHTML = out.results.map((r) => `<div>${renderOne(r)}</div>`).join("");
  btnAll.disabled = false;
});

document.getElementById("open-options").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});
