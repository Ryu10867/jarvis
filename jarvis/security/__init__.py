"""Starts every enabled security monitor on its own thread and restarts it if it crashes."""
import logging
import threading
import time

log = logging.getLogger("jarvis.security")


class SecurityManager:
    def __init__(self, cfg: dict, alerts):
        self.cfg, self.alerts = cfg, alerts
        self.monitors: dict[str, object] = {}
        self.face = None

    def start(self):
        import sys
        sec = self.cfg["security"]
        if sys.platform != "win32":
            # Process watch (PowerShell / registry rules), USB watch and Login watch read Windows-only sources.
            # On a Mac: Face guard, Network watch and File watch.
            sec = {**sec, "process_watch": {"enabled": False}, "usb_watch": {"enabled": False},
                   "login_watch": {"enabled": False}}
        if sec["face_guard"]["enabled"]:
            try:
                from .face_guard import FaceGuard
                self.face = FaceGuard(self.cfg, self.alerts)
                self.monitors["Face guard"] = self.face
            except Exception as e:
                log.error("Face guard unavailable: %s", e)
        if sec["process_watch"]["enabled"]:
            from .process_watch import ProcessWatch
            self.monitors["Process watch"] = ProcessWatch(self.cfg, self.alerts)
        if sec["network_watch"]["enabled"]:
            from .network_watch import NetworkWatch
            self.monitors["Network watch"] = NetworkWatch(self.cfg, self.alerts)
        if sec["usb_watch"]["enabled"]:
            from .device_watch import UsbWatch
            self.monitors["USB watch"] = UsbWatch(self.cfg, self.alerts)
        if sec["login_watch"]["enabled"]:
            from .device_watch import LoginWatch
            self.monitors["Login watch"] = LoginWatch(self.cfg, self.alerts, self.face)
        if sec["file_watch"]["enabled"]:
            from .file_watch import FileWatch
            self.monitors["File watch"] = FileWatch(self.cfg, self.alerts)

        for name, mon in self.monitors.items():
            threading.Thread(target=self._supervise, args=(name, mon), daemon=True, name=name).start()

    def _supervise(self, name, mon):
        while True:
            try:
                mon.run()
                return  # a monitor returning normally means it chose to stay disabled
            except Exception as e:
                log.exception("%s crashed; restarting in 15s", name)
                mon.status = f"crashed: {e}"
                time.sleep(15)

    def owner_verified(self):
        return self.face.owner_verified() if self.face else None

    def status_report(self) -> str:
        parts = [f"{n}: {getattr(m, 'status', '?')}" for n, m in self.monitors.items()]
        recent = [a for a in self.alerts.summary(20) if a["severity"] not in ("info", "low")][-3:]
        if recent:
            parts.append("Recent alerts: " + "; ".join(f"{a['time'][11:16]} {a['message']}" for a in recent))
        else:
            parts.append("No recent alerts.")
        return ". ".join(parts)
