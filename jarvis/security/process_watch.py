"""Watches new processes for malware-like behaviour, startup persistence changes, and Defender being turned off."""
import json
import logging
import os
import re
import subprocess
import time
import winreg
from pathlib import Path

import psutil

from .signing import signer

log = logging.getLogger("jarvis.process")

# (regex on lowercase command line, severity, description)
RULES = [
    (r"vssadmin.*delete\s+shadows|wmic.*shadowcopy.*delete|wbadmin.*delete|bcdedit.*recoveryenabled\s+no",
     "critical", "Backup / shadow-copy deletion (typical ransomware step)"),
    (r"powershell.*(-enc\b|-encodedcommand|-e\s+[a-z0-9+/=]{40,})", "high", "Encoded PowerShell command"),
    (r"powershell.*(downloadstring|downloadfile|invoke-webrequest|iwr\s|iex\s*\(|invoke-expression).*http",
     "high", "PowerShell downloading and running code"),
    (r"powershell.*-w(indowstyle)?\s+h(idden)?", "medium", "Hidden PowerShell window"),
    (r"certutil.*(-urlcache|-decode)", "high", "certutil used to download/decode files"),
    (r"mshta(\.exe)?\s+.*(http|javascript|vbscript)", "high", "mshta running remote script"),
    (r"regsvr32.*/i:\s*http|regsvr32.*scrobj", "high", "regsvr32 script execution (Squiblydoo)"),
    (r"rundll32.*javascript:", "high", "rundll32 running JavaScript"),
    (r"bitsadmin.*/transfer", "medium", "bitsadmin file download"),
    (r"(netsh\s+advfirewall\s+set\s+.*state\s+off)", "high", "Firewall being disabled"),
    (r"set-mppreference.*-disable", "critical", "Attempt to disable Windows Defender"),
    (r"reg(\.exe)?\s+add.*\\currentversion\\run", "medium", "Registry autorun being added"),
    (r"schtasks.*/create", "medium", "Scheduled task being created"),
]
OFFICE = {"winword.exe", "excel.exe", "powerpnt.exe", "outlook.exe", "acrord32.exe", "acrobat.exe"}
SHELLS = {"cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe", "mshta.exe", "rundll32.exe"}
RUN_KEYS = [(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run")]
STARTUP_DIRS = [Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup",
                Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs\StartUp"]
NO_WINDOW = subprocess.CREATE_NO_WINDOW


def startup_entries() -> set[str]:
    entries = set()
    for hive, key in RUN_KEYS:
        try:
            with winreg.OpenKey(hive, key) as k:
                i = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(k, i)
                        entries.add(f"{key}\\{name} = {val}")
                        i += 1
                    except OSError:
                        break
        except OSError:
            pass
    for d in STARTUP_DIRS:
        if d.exists():
            entries |= {f"Startup folder: {p.name}" for p in d.iterdir() if p.name != "desktop.ini"}
    return entries


def scheduled_tasks() -> set[str]:
    try:
        out = subprocess.run(["schtasks", "/query", "/fo", "csv", "/nh"], capture_output=True, text=True,
                             timeout=30, creationflags=NO_WINDOW).stdout
        return {line.split(",")[0].strip('"') for line in out.splitlines() if line.startswith('"\\')}
    except Exception:
        return set()


def antivirus_status() -> dict | None:
    """Ask Windows Security Center which antivirus products are registered and whether real-time protection is on.
    (Defender switches itself off when another AV such as McAfee is active, so we check all of them.)"""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct "
                              "| Select-Object displayName,productState | ConvertTo-Json"],
                             capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW).stdout
        if not out.strip():
            return None
        data = json.loads(out)
        data = data if isinstance(data, list) else [data]
        return {d["displayName"]: ((int(d["productState"]) >> 12) & 0xF) == 1 for d in data}
    except Exception:
        return None


class ProcessWatch:
    def __init__(self, cfg: dict, alerts):
        c = cfg["security"]["process_watch"]
        self.interval = c["interval"]
        self.sus_dirs = [d.lower() for d in c["suspicious_dirs"]]
        # Full paths (not names, which malware can copy) of tools that legitimately run encoded PowerShell etc.
        self.trusted_parents = {os.path.normcase(os.path.expandvars(p)) for p in c.get("trusted_parents", [])}
        self.alerts = alerts
        self.seen_pids: set[int] = set()
        self.alerted_exes: set[str] = set()
        self.status = "starting"

    def _check_proc(self, p: psutil.Process):
        try:
            info = p.as_dict(["name", "exe", "cmdline", "ppid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        name = (info["name"] or "").lower()
        exe = info["exe"] or ""
        cmd = " ".join(info["cmdline"] or []).lower()
        try:
            pproc = psutil.Process(info["ppid"])
            parent, parent_exe = pproc.name().lower(), pproc.exe() or ""
        except Exception:
            parent, parent_exe = "", ""
        details = {"pid": p.pid, "name": name, "exe": exe, "cmdline": cmd[:500], "parent": parent_exe or parent}

        # Jarvis's own helpers (toast notifications, OCR, brightness...) are hidden PowerShell children of this
        # process. They used to trigger "Hidden PowerShell window" alerts about Jarvis himself.
        own = info["ppid"] == os.getpid()
        for pattern, sev, desc in RULES:
            if re.search(pattern, cmd):
                if own and sev != "critical":
                    return
                if parent_exe and os.path.normcase(parent_exe) in self.trusted_parents and sev != "critical":
                    # Still recorded in the log, just not spoken / toasted.
                    self.alerts.raise_alert("process", "info", f"{desc} from trusted {parent}.", details)
                else:
                    self.alerts.raise_alert("process", sev, f"{desc}: {name} (pid {p.pid}), started by {parent or 'unknown'}.", details)
                return
        if parent in OFFICE and name in SHELLS:
            self.alerts.raise_alert("process", "high", f"{parent} launched {name} (possible malicious document).", details)
            return
        exe_l = exe.lower()
        if exe and exe_l not in self.alerted_exes and any(d in exe_l for d in self.sus_dirs):
            self.alerted_exes.add(exe_l)
            org = signer(exe)
            details["signed_by"] = org
            if org:
                self.alerts.raise_alert("process", "low", f"{name} running from an unusual folder (signed by {org}).", details)
            else:
                self.alerts.raise_alert("process", "medium", f"Unsigned program running from an unusual folder: {name}.", details)

    def run(self):
        self.seen_pids = set(psutil.pids())
        startup = startup_entries()
        tasks = scheduled_tasks()
        last_slow = time.time()
        self.status = f"watching ({len(self.seen_pids)} processes baselined)"
        while True:
            time.sleep(self.interval)
            current = set(psutil.pids())
            for pid in current - self.seen_pids:
                try:
                    self._check_proc(psutil.Process(pid))
                except psutil.NoSuchProcess:
                    pass
            self.seen_pids = current

            now_startup = startup_entries()
            for e in now_startup - startup:
                self.alerts.raise_alert("persistence", "high", "New program added to Windows startup.", {"entry": e})
            startup = now_startup

            if time.time() - last_slow > 120:     # heavier checks every 2 minutes
                last_slow = time.time()
                now_tasks = scheduled_tasks()
                if tasks:
                    for t in now_tasks - tasks:
                        self.alerts.raise_alert("persistence", "medium", f"New scheduled task: {t}", {"task": t})
                tasks = now_tasks or tasks
                av = antivirus_status()
                if av and not any(av.values()):
                    self.alerts.raise_alert("antivirus", "critical",
                                            "No antivirus real-time protection is active on this laptop.", av)
