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
})();
