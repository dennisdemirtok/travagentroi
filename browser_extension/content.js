// Kungens Trav — flytande knapp på tipssajter.
// Lägger en liten knapp nere till höger. Ett klick skickar sidan till backend
// via service workern (ingen CSP-spärr). Visar status direkt på sidan.

(function () {
  if (window.__ktInjected) return;
  window.__ktInjected = true;

  const btn = document.createElement("button");
  btn.textContent = "🏇 Skicka tips";
  btn.setAttribute("aria-label", "Skicka tips till Kungens Trav");
  Object.assign(btn.style, {
    position: "fixed",
    right: "16px",
    bottom: "16px",
    zIndex: "2147483647",
    background: "#f59e0b",
    color: "#1a1a2e",
    border: "none",
    borderRadius: "999px",
    padding: "11px 18px",
    font: "600 14px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",
    boxShadow: "0 6px 20px rgba(0,0,0,.3)",
    cursor: "pointer"
  });

  let hideTimer = null;
  function flash(text, color) {
    btn.textContent = text;
    btn.style.background = color || "#f59e0b";
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      btn.textContent = "🏇 Skicka tips";
      btn.style.background = "#f59e0b";
      btn.disabled = false;
    }, 7000);
  }

  btn.addEventListener("click", () => {
    btn.disabled = true;
    flash("⏳ Skickar…", "#334155");
    chrome.runtime.sendMessage({ type: "ingestThisTab" }, (r) => {
      if (chrome.runtime.lastError) {
        flash("⚠️ " + chrome.runtime.lastError.message, "#b91c1c");
        return;
      }
      if (r && r.ok) {
        flash(`✅ ${r.game_type} ${r.day} • ${r.interviews_count} intervjuer`, "#15803d");
      } else {
        flash("⚠️ " + ((r && r.error) || "misslyckades"), "#b91c1c");
      }
    });
  });

  // Vänta tills body finns
  if (document.body) {
    document.body.appendChild(btn);
  } else {
    window.addEventListener("DOMContentLoaded", () => document.body.appendChild(btn));
  }

  // ── Auto-skicka-läge ──────────────────────────────────────────────────────
  // Skickar artikeln själv (noll klick) OM:
  //  1) "autoSend" är påslaget i inställningarna,
  //  2) sidan ser ut att handla om trav (annars slösar vi LLM-anrop),
  //  3) den inte redan skickats de senaste 6 timmarna.
  const TRAV_RE = /\b(v75|v86|v85|v64|v65|gs75|trav|travtips|spik|gardering|streck|lopp)\b/i;

  function looksLikeTrav() {
    const body = document.body ? document.body.innerText.slice(0, 4000) : "";
    const hay = (location.href + " " + document.title + " " + body).toLowerCase();
    return TRAV_RE.test(hay);
  }

  async function maybeAutoSend() {
    let cfg;
    try { cfg = await chrome.storage.sync.get("autoSend"); } catch (e) { return; }
    if (!cfg || !cfg.autoSend) return;
    if (!looksLikeTrav()) return;

    const key = "sent:" + location.href.split("#")[0];
    let store;
    try { store = await chrome.storage.local.get(key); } catch (e) { store = {}; }
    if (Date.now() - (store[key] || 0) < 6 * 3600 * 1000) return; // redan skickad nyligen

    // Markera direkt så två triggers inte dubbelskickar
    try { await chrome.storage.local.set({ [key]: Date.now() }); } catch (e) {}

    flash("⏳ Auto-skickar…", "#334155");
    chrome.runtime.sendMessage({ type: "ingestThisTab" }, (r) => {
      if (chrome.runtime.lastError) {
        try { chrome.storage.local.remove(key); } catch (e) {}
        flash("⚠️ " + chrome.runtime.lastError.message, "#b91c1c");
        return;
      }
      if (r && r.ok) {
        flash(`✅ ${r.game_type} ${r.day} • ${r.interviews_count} intervjuer`, "#15803d");
      } else {
        // rensa markeringen vid fel så man kan försöka igen
        try { chrome.storage.local.remove(key); } catch (e) {}
        flash("⚠️ " + ((r && r.error) || "auto misslyckades"), "#b91c1c");
      }
    });
  }

  // Kör vid laddning + en retry (nyhetssajter laddar ofta innehåll sent/SPA)
  maybeAutoSend();
  setTimeout(maybeAutoSend, 3500);
})();
