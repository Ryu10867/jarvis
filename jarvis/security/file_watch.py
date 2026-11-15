"""Watches important folders for ransomware-like behaviour: mass changes, encrypted extensions, ransom notes."""
import logging
import re
import threading
import time
from collections import deque

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from ..config import resolve

log = logging.getLogger("jarvis.files")
IGNORE = re.compile(r"[\\/](\.git|node_modules|\.venv|__pycache__|\.cache)[\\/]|~\$|\.tmp$|\.crdownload$", re.I)
RANSOM_NOTE = re.compile(r"(readme.*(decrypt|restore|recover)|how.?to.?(decrypt|recover|restore)|decrypt.*instructions|_readme\.txt)", re.I)


class _Handler(FileSystemEventHandler):
    def __init__(self, watch):
        self.w = watch

    def on_any_event(self, event):
        if event.is_directory or event.event_type in ("opened", "closed", "closed_no_write"):
            return
        path = getattr(event, "dest_path", "") or event.src_path
        if IGNORE.search(path):
            return
        self.w.record(event.event_type, path)


class FileWatch:
    def __init__(self, cfg: dict, alerts):
        c = cfg["security"]["file_watch"]
        self.paths = [resolve(p) for p in c["paths"]]
        self.threshold, self.window = c["burst_threshold"], c["burst_window"]
        self.exts = tuple(e.lower() for e in c["ransom_extensions"])
        self.alerts = alerts
        self.events: deque = deque()
        self.lock = threading.Lock()
        self.last_burst = 0.0
        self.alerted_paths: set[str] = set()
        self.status = "starting"

    def record(self, kind: str, path: str):
        now = time.time()
        low = path.lower()
        if kind in ("created", "moved") and path not in self.alerted_paths:
            name = low.rsplit("\\", 1)[-1]
            if low.endswith(self.exts):
                self.alerted_paths.add(path)
                self.alerts.raise_alert("files", "high", "A file was renamed to an encrypted-looking extension.", {"path": path})
            elif RANSOM_NOTE.search(name):
                self.alerted_paths.add(path)
                self.alerts.raise_alert("files", "critical", "A possible ransom note appeared in your files.", {"path": path})
        with self.lock:
            self.events.append((now, kind, path))
            while self.events and now - self.events[0][0] > self.window:
                self.events.popleft()
            burst = len(self.events) >= self.threshold and now - self.last_burst > 60
            if burst:
                self.last_burst = now
                sample = [p for _, _, p in list(self.events)[-5:]]
                kinds = {}
                for _, k, _ in self.events:
                    kinds[k] = kinds.get(k, 0) + 1
        if burst:
            self.alerts.raise_alert("files", "high",
                f"{len(self.events)} files changed in {self.window} seconds. If you're not doing this, it could be ransomware.",
                {"counts": kinds, "sample": sample})

    def run(self):
        obs = Observer()
        watched = []
        for p in self.paths:
            if p.exists():
                obs.schedule(_Handler(self), str(p), recursive=True)
                watched.append(p.name)
        obs.start()
        self.status = f"watching {', '.join(watched)}"
        obs.join()
