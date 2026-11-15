// Relays between Coursera tabs and Jarvis (http://127.0.0.1:8765, see [coursera] in config.toml).
// Pages can't reach 127.0.0.1 themselves (Chrome blocks web pages from calling local servers); an extension can.
const JARVIS = "http://127.0.0.1:8765/coursera";
let lastMark = 0;

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== "sync") return false;
  fetch(JARVIS, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tab: sender.tab ? sender.tab.id : null, state: msg.state }),
  })
    .then((r) => r.json())
    .then((reply) => {
      if (Date.now() - lastMark > 5000) {   // for the popup's "connected" line
        lastMark = Date.now();
        chrome.storage.local.set({ jarvisSeen: lastMark });
      }
      sendResponse(reply);
    })
    .catch(() => sendResponse({ offline: true }));
  return true; // answer asynchronously
});
