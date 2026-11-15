const binge = document.getElementById("binge");
const speed = document.getElementById("speed");
const link = document.getElementById("link");

chrome.storage.local.get({ binge: false, speed: 1, jarvisSeen: 0 }, (s) => {
  binge.checked = s.binge;
  speed.value = String(s.speed);
  if (Date.now() - s.jarvisSeen < 15000) {
    link.textContent = "Jarvis: connected";
    link.className = "on";
  } else {
    link.textContent = "Jarvis: not connected (open a Coursera page with Jarvis running)";
  }
});
binge.addEventListener("change", () => chrome.storage.local.set({ binge: binge.checked }));
speed.addEventListener("change", () => chrome.storage.local.set({ speed: Number(speed.value) }));
