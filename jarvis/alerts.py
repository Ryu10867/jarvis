"""Central alert bus: every security module reports here. Alerts are logged, toasted and spoken."""
import json
import logging
import subprocess
import threading
import time
from collections import deque

from .config import resolve
from .state import STATE

log = logging.getLogger("jarvis.alerts")

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


class AlertBus:
    def __init__(self, cfg: dict, speaker=None):
        sec = cfg["security"]
        self.path = resolve(sec["alerts_log"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.speak_alerts = sec.get("speak_alerts", True)
        self.toast_alerts = sec.get("toast_alerts", True)
        self.speaker = speaker
        self.recent = deque(maxlen=200)
        self._lock = threading.Lock()
        self._last_spoken: dict[str, float] = {}

    def raise_alert(self, source: str, severity: str, message: str, details: dict | None = None):
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
            "severity": severity,
            "message": message,
            "details": details or {},
        }
        with self._lock:
            self.recent.append(entry)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        log.warning("[%s/%s] %s", source, severity, message)

        if SEVERITY_ORDER.get(severity, 0) >= SEVERITY_ORDER["medium"]:
            STATE.add("ALERT", message)
            STATE.flash_alert(8 if severity in ("high", "critical") else 4)
            if self.toast_alerts:
                toast(f"Jarvis security: {source}", message)
            # Rate-limit speech per source so a noisy monitor doesn't talk non-stop.
            now = time.time()
            if self.speak_alerts and self.speaker and now - self._last_spoken.get(source, 0) > 30:
                self._last_spoken[source] = now
                lead = "Sir, we have a problem." if severity == "critical" else "Sir, a security alert."
                self.speaker.say(f"{lead} {message}", log_it=False)

    def summary(self, n: int = 5) -> list[dict]:
        with self._lock:
            return list(self.recent)[-n:]


def toast(title: str, body: str):
    """Show a Windows toast notification through PowerShell (no extra dependencies)."""
    def esc(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                 .replace('"', "&quot;").replace("'", "''"))
    script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{esc(title)}</text><text>{esc(body)}</text></binding></visual></toast>')
$app = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:  # never let a notification failure break monitoring
        log.debug("toast failed: %s", e)
