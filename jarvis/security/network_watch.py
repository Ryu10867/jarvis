"""Watches network connections: suspicious ports, programs in temp folders phoning home, new listening programs.

Listening-port alerts are now keyed on the *program* and remembered in data/known_listeners.json, so the same
signed updater opening a different random port after every restart no longer raises an alert each time.
Windows' own kernel listeners (PID 0/4, e.g. NetBIOS on 139) are ignored instead of being reported as "unsigned"."""
import json
import logging
import time

import psutil

from ..config import ROOT
from .signing import signer

log = logging.getLogger("jarvis.network")
KNOWN_FILE = ROOT / "data" / "known_listeners.json"


def _proc(pid):
    try:
        p = psutil.Process(pid)
        return p.name(), (p.exe() or "")
    except Exception:
        return "unknown", ""


class NetworkWatch:
    def __init__(self, cfg: dict, alerts):
        c = cfg["security"]["network_watch"]
        self.interval = c["interval"]
        self.bad_ports = set(c["suspicious_ports"])
        self.sus_dirs = [d.lower() for d in cfg["security"]["process_watch"]["suspicious_dirs"]]
        self.alerts = alerts
        self.alerted: set = set()
        self.listening: set = set()
        try:
            self.known: set[str] = set(json.loads(KNOWN_FILE.read_text(encoding="utf-8")))
        except Exception:
            self.known = set()
        self.status = "starting"

    def snapshot(self):
        try:
            return psutil.net_connections(kind="inet")
        except Exception as e:
            log.debug("net_connections failed: %s", e)
            return []

    def _remember(self, key: str):
        self.known.add(key)
        try:
            KNOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
            KNOWN_FILE.write_text(json.dumps(sorted(self.known), indent=1), encoding="utf-8")
        except Exception as e:
            log.debug("could not save known listeners: %s", e)

    def _check_listener(self, port: int, pid: int, baseline: bool):
        if pid in (0, 4):                       # Windows kernel (SMB / NetBIOS / HTTP.sys)
            return
        name, exe = _proc(pid)
        key = (exe or name).lower()
        if port in self.bad_ports and f"{key}:{port}" not in self.known:
            # e.g. Razer's signed SDK server legitimately listens on 1337: mention it once, quietly, and remember.
            self._remember(f"{key}:{port}")
            org = signer(exe)
            self.alerts.raise_alert("network", "low" if org else "high",
                                    f"{name} ({'signed by ' + org if org else 'UNSIGNED'}) is listening on port {port}, "
                                    f"which is often used by backdoors.", {"port": port, "pid": pid, "exe": exe})
        if key in self.known:
            return
        self._remember(key)
        if baseline:
            return                              # whatever is already running at first start is the baseline
        org = signer(exe)
        info = {"port": port, "pid": pid, "exe": exe, "signed_by": org}
        if org:
            self.alerts.raise_alert("network", "low", f"{name} (signed by {org}) started listening for connections "
                                                      f"for the first time, on port {port}.", info)
        else:
            self.alerts.raise_alert("network", "high", f"Unsigned program {name} opened listening port {port}.", info)

    def run(self):
        first_ever = not self.known
        self.listening = {(c.laddr.port, c.pid) for c in self.snapshot() if c.status == psutil.CONN_LISTEN}
        for port, pid in self.listening:
            self._check_listener(port, pid, baseline=first_ever)
        self.status = f"watching ({len(self.listening)} listening ports baselined)"
        while True:
            time.sleep(self.interval)
            conns = self.snapshot()
            listening_now = set()
            for c in conns:
                if c.status == psutil.CONN_LISTEN:
                    listening_now.add((c.laddr.port, c.pid))
                    continue
                if not c.raddr or c.raddr.ip in ("127.0.0.1", "::1"):
                    continue
                key = (c.pid, c.raddr.ip, c.raddr.port)
                if key in self.alerted:
                    continue
                if c.raddr.port in self.bad_ports:
                    name, exe = _proc(c.pid)
                    self.alerted.add(key)
                    self.alerts.raise_alert("network", "high", f"{name} is connected to a suspicious port {c.raddr.port}.",
                                            {"pid": c.pid, "process": name, "exe": exe,
                                             "remote": f"{c.raddr.ip}:{c.raddr.port}", "status": c.status})
                elif c.status == psutil.CONN_ESTABLISHED:
                    name, exe = _proc(c.pid)
                    if exe and any(d in exe.lower() for d in self.sus_dirs):
                        self.alerted.add(key)
                        self.alerts.raise_alert("network", "medium",
                                                f"{name}, running from an unusual folder, is talking to the internet.",
                                                {"pid": c.pid, "process": name, "exe": exe,
                                                 "remote": f"{c.raddr.ip}:{c.raddr.port}"})
            for port, pid in listening_now - self.listening:
                self._check_listener(port, pid, baseline=False)
            self.listening = listening_now
            self.status = f"watching ({len(conns)} connections)"
