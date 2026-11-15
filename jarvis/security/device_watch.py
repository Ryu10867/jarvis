"""USB drive / new keyboard (BadUSB) detection and failed Windows login detection."""
import ctypes
import logging
import re
import string
import subprocess
import time
from pathlib import Path

log = logging.getLogger("jarvis.devices")
NO_WINDOW = subprocess.CREATE_NO_WINDOW
DRIVE_REMOVABLE = 2


def removable_drives() -> set[str]:
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    drives = set()
    for i, letter in enumerate(string.ascii_uppercase):
        if mask & (1 << i) and ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") == DRIVE_REMOVABLE:
            drives.add(letter)
    return drives


def keyboards() -> set[str]:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-PnpDevice -PresentOnly -Class Keyboard,HIDClass | Select-Object -ExpandProperty InstanceId"],
                         capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW).stdout
    return {l.strip() for l in out.splitlines() if l.strip().upper().startswith(("HID\\", "USB\\"))}


class UsbWatch:
    def __init__(self, cfg: dict, alerts):
        self.interval = cfg["security"]["usb_watch"]["interval"]
        self.alerts = alerts
        self.status = "starting"

    def run(self):
        drives = removable_drives()
        kbs = keyboards()
        last_kb = time.time()
        self.status = "watching"
        while True:
            time.sleep(self.interval)
            now = removable_drives()
            for d in now - drives:
                root = Path(f"{d}:\\")
                autorun = (root / "autorun.inf").exists()
                try:
                    exes = [p.name for p in root.iterdir() if p.suffix.lower() in (".exe", ".bat", ".lnk", ".scr", ".vbs")]
                except OSError:
                    exes = []
                sev = "high" if autorun or exes else "medium"
                msg = f"USB drive {d} connected."
                if autorun or exes:
                    msg += " It contains autorun or executable files; don't open them."
                self.alerts.raise_alert("usb", sev, msg, {"drive": d, "autorun": autorun, "executables": exes[:10]})
            for d in drives - now:
                self.alerts.raise_alert("usb", "info", f"USB drive {d} removed.")
            drives = now

            if time.time() - last_kb > 15:
                last_kb = time.time()
                new = keyboards()
                added = new - kbs
                if added and any("USB" in a.upper() or "VID_" in a.upper() for a in added):
                    self.alerts.raise_alert("usb", "medium",
                        "A new USB keyboard or input device was connected. If you didn't plug one in, unplug it now.",
                        {"devices": sorted(added)[:5]})
                kbs = new


class LoginWatch:
    """Reads Security event 4625 (failed logon). Needs Administrator rights."""

    def __init__(self, cfg: dict, alerts, face_guard=None):
        c = cfg["security"]["login_watch"]
        self.interval = c["interval"]
        self.photo = c.get("photo_on_failed_login", True)
        self.alerts = alerts
        self.face_guard = face_guard
        self.status = "starting"

    def _query(self):
        out = subprocess.run(["wevtutil", "qe", "Security", "/q:*[System[(EventID=4625)]]", "/c:20", "/rd:true", "/f:xml"],
                             capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        if out.returncode != 0:
            raise PermissionError(out.stderr.strip())
        events = []
        for ev in re.findall(r"<Event .*?</Event>", out.stdout, re.S):
            rid = int(re.search(r"<EventRecordID>(\d+)<", ev).group(1))
            user = re.search(r"Name='TargetUserName'>([^<]*)<", ev)
            ip = re.search(r"Name='IpAddress'>([^<]*)<", ev)
            events.append((rid, user.group(1) if user else "?", ip.group(1) if ip else "-"))
        return events

    def run(self):
        if not ctypes.windll.shell32.IsUserAnAdmin():
            self.status = "disabled (run Jarvis as Administrator to enable)"
            log.info("Login watch needs Administrator rights; skipping.")
            return
        try:
            last = max((r for r, _, _ in self._query()), default=0)
        except Exception as e:
            self.status = f"error: {e}"
            return
        self.status = "watching"
        while True:
            time.sleep(self.interval)
            try:
                events = [e for e in self._query() if e[0] > last]
            except Exception as e:
                log.debug("login query failed: %s", e)
                continue
            if not events:
                continue
            last = max(e[0] for e in events)
            photo = self.face_guard.snapshot("failed_login") if (self.photo and self.face_guard) else None
            users = sorted({u for _, u, _ in events})
            self.alerts.raise_alert("login", "high" if len(events) < 3 else "critical",
                                    f"{len(events)} failed Windows login attempt(s)." +
                                    (" " + self.face_guard.photo_note(photo) if (self.photo and self.face_guard) else ""),
                                    {"users": users, "sources": sorted({ip for _, _, ip in events}),
                                     "photo": str(photo) if photo else None})
