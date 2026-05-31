// Kungens Trav — Tipshämtare (service worker)
// Kör fetch mot vårt backend. Eftersom service workern tillhör tillägget
// (inte sidan) gäller INTE tipssajtens CSP här — därför inga "Failed to fetch".

const DEFAULT_BASE = "https://travagentroi-production.up.railway.app";

// Sajter vi vet kan innehålla travtips (för "hämta alla öppna flikar")
const TIPS_MATCHES = [
  "*://*.aftonbladet.se/*",
  "*://*.sportbladet.se/*",
  "*://*.expressen.se/*",
  "*://*.travronden.se/*",
  "*://*.kungenstrav.se/*",
  "*://*.travfakta.se/*"
];

async function getBase() {
  try {
    const { backendBase } = await chrome.storage.sync.get("backendBase");
    return (backendBase || DEFAULT_BASE).replace(/\/+$/, "");
  } catch (e) {
    return DEFAULT_BASE;
  }
}

// Körs I SIDANS kontext (serialiseras av chrome.scripting) — plockar texten.
function _extractPage() {
  return {
    url: location.href,
    title: document.title,
    text: (document.body ? document.body.innerText : "").slice(0, 40000)
  };
}

async function ingestTab(tab) {
  if (!tab || !tab.id) return { ok: false, error: "ingen flik" };
  let payload;
  try {
    const [inj] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: _extractPage
    });
    payload = inj && inj.result;
  } catch (e) {
    return { ok: false, error: "kunde inte läsa sidan: " + e.message, tabTitle: tab.title };
  }
  if (!payload || !payload.text || payload.text.trim().length < 50) {
    return { ok: false, error: "för lite text på sidan", tabTitle: tab.title };
  }
  const base = await getBase();
  try {
    const res = await fetch(base + "/api/tips/ingest-page", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    return { ...data, tabTitle: tab.title, tabUrl: payload.url };
  } catch (e) {
    return { ok: false, error: "nätverksfel: " + e.message, tabTitle: tab.title };
  }
}

async function ingestActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return await ingestTab(tab);
}

async function ingestAllTipsTabs() {
  let tabs = [];
  try {
    tabs = await chrome.tabs.query({ url: TIPS_MATCHES });
  } catch (e) {
    tabs = [];
  }
  const results = [];
  for (const tab of tabs) {
    results.push(await ingestTab(tab));
  }
  return { count: tabs.length, results };
}

// Meddelanden från popup + content script
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (msg.type === "ingestActive") {
      sendResponse(await ingestActiveTab());
    } else if (msg.type === "ingestThisTab") {
      const tab = sender.tab || (await chrome.tabs.query({ active: true, currentWindow: true }))[0];
      sendResponse(await ingestTab(tab));
    } else if (msg.type === "ingestAll") {
      sendResponse(await ingestAllTipsTabs());
    } else if (msg.type === "getBase") {
      sendResponse({ base: await getBase() });
    } else {
      sendResponse({ ok: false, error: "okänt kommando" });
    }
  })();
  return true; // async sendResponse
});
