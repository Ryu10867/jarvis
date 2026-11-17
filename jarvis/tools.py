"""Actions Jarvis can perform. Each tool has a JSON schema (sent to the LLM) and a `risky` flag.
Risky tools need a spoken/typed "yes" and, if face-guard is on, a recently verified owner face."""
import ast
import ctypes
import datetime as dt
import logging
import math
import operator
import os
import re
import socket
import subprocess
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass

try:
    from ctypes import wintypes
except Exception:                       # not Windows: the Mac versions of these tools live in macos.py
    wintypes = None
from pathlib import Path
from typing import Callable

import psutil

from . import memory
from .alerts import toast
from .config import ROOT, known_folder, resolve
from .reminders import describe, parse_when

log = logging.getLogger("jarvis.tools")
HOME = Path.home()


@dataclass
class Tool:
    name: str
    description: str
    params: dict           # {param: (type, description, required)}
    func: Callable
    risky: bool = False
    hidden: bool = False   # runnable by reflexes / forced calls, but not offered to the model (its window is full)

    def schema(self) -> dict:
        props = {k: {"type": t, "description": d} for k, (t, d, _) in self.params.items()}
        required = [k for k, (_, _, r) in self.params.items() if r]
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": props, "required": required}}}


REGISTRY: dict[str, Tool] = {}


def tool(description: str, risky: bool = False, hidden: bool = False, **params):
    def deco(fn):
        REGISTRY[fn.__name__] = Tool(fn.__name__, description, params, fn, risky, hidden)
        return fn
    return deco


class Ctx:
    """Runtime context injected by main: speaker, alert bus, security manager, config, reminders, executor."""
    cfg: dict = {}
    speaker = None
    alerts = None
    security = None
    reminders = None
    agenda = None           # agenda.Agenda: Gmail + Google Calendar deadlines (None when switched off)
    execute = None         # Brain._execute, so protocols go through the same confirmation rules
    last_opened = None      # ("app", name) or ("tab", url): what "close it" / "close that" refers to
    note = None             # something the reflexes found out that the language model should know for this request
    report = None           # report(message): say a finished background job's result as soon as Jarvis is free


def _ps(cmd: str, timeout: int = 30) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                       timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    return (r.stdout + r.stderr).strip()


def _press(vk: int, times: int = 1):
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
        time.sleep(0.01)


def _combo(*vks: int):
    for vk in vks:
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


def _folder(name: str) -> Path | None:
    """Real location of a well-known user folder (Desktop/Documents/Pictures follow OneDrive redirection)."""
    n = name.lower().strip().rstrip("\\/")
    if n in ("downloads", "download"):
        return HOME / "Downloads"
    if n in ("home", "user", "~"):
        return HOME
    return known_folder(n) or known_folder(n + "s")


def _user_path(path: str) -> Path:
    p = Path(os.path.expandvars(path)).expanduser()
    if p.is_absolute():
        return p
    parts = p.parts
    base = _folder(parts[0]) if parts else None
    return base.joinpath(*parts[1:]) if base else HOME / p


# ------------------------------------------------------------------ info
@tool("Get the current date and time.")
def get_time():
    return dt.datetime.now().strftime("%A, %d %B %Y, %I:%M %p")


def _battery_text() -> str | None:
    bat = psutil.sensors_battery()
    if not bat:
        return None
    text = f"Battery {bat.percent:.0f}% {'charging' if bat.power_plugged else 'on battery'}"
    if not bat.power_plugged and bat.secsleft and 0 < bat.secsleft < 48 * 3600:
        text += f", about {bat.secsleft // 3600}h {bat.secsleft % 3600 // 60}m left"
    return text


@tool("Quick system status: CPU, memory, battery, disk usage.")
def system_status():
    disk = psutil.disk_usage(str(HOME.anchor))
    parts = [f"CPU {psutil.cpu_percent(interval=0.4):.0f}%",
             f"RAM {psutil.virtual_memory().percent:.0f}% used",
             f"Disk {HOME.anchor} {disk.percent:.0f}% used ({disk.free // 2**30} GB free)"]
    if (b := _battery_text()):
        parts.append(b)
    return ", ".join(parts)


def _gpu() -> dict | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=8,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
        name, util, temp, used, total = [x.strip() for x in out.splitlines()[0].split(",")]
        return {"name": name.replace("NVIDIA GeForce ", ""), "util": int(util), "temp": int(temp),
                "used": int(used), "total": int(total)}
    except Exception:
        return None


def _gpu_text() -> str | None:
    g = _gpu()
    return (f"GPU {g['name']} at {g['util']}% load, {g['temp']} degrees, {g['used']} of {g['total']} MB video memory"
            if g else None)


def _wifi_text() -> str:
    try:
        out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=8,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
        sig = re.search(r"^\s*Signal\s*:\s*(\d+)%", out, re.M)
        if ssid:
            return f"Wi-Fi connected to {ssid.group(1).strip()}" + (f" at {sig.group(1)}% signal" if sig else "")
    except Exception:
        pass
    up = [n for n, s in psutil.net_if_stats().items() if s.isup and "loopback" not in n.lower()]
    return f"Network adapters up: {', '.join(up[:3])}" if up else "No network connection"


def diagnostics_data() -> dict:
    """Raw numbers behind the diagnostics report (the reflex router narrates these without the LLM)."""
    d = {"cpu": psutil.cpu_percent(interval=0.4), "ram": psutil.virtual_memory().percent}
    disk = psutil.disk_usage(str(HOME.anchor))
    d["disk"], d["disk_free_gb"] = disk.percent, disk.free // 2**30
    bat = psutil.sensors_battery()
    if bat:
        d["battery"], d["plugged"] = bat.percent, bat.power_plugged
    d["gpu"] = _gpu()
    d["wifi"] = _wifi_text()
    d["uptime"] = time.time() - psutil.boot_time()
    try:
        procs = [(p.info["name"], p.info["memory_info"].rss >> 20) for p in psutil.process_iter(["name", "memory_info"])
                 if p.info["memory_info"]]
        d["processes"] = len(procs)
        d["heaviest"], d["heaviest_mb"] = max(procs, key=lambda x: x[1])
    except Exception:
        pass
    if Ctx.security:
        mons = Ctx.security.monitors
        d["monitors"] = len(mons)
        d["monitors_bad"] = [n for n, m in mons.items()
                             if any(w in str(getattr(m, "status", "")) for w in ("crash", "unavailable"))]
        d["alerts"] = [a for a in Ctx.alerts.summary(30) if a["severity"] in ("medium", "high", "critical")] if Ctx.alerts else []
    return d


@tool("Full diagnostics report: CPU, memory, disk, battery, GPU, network, uptime, heaviest process, security monitors.")
def run_diagnostics():
    d = diagnostics_data()
    parts = [f"CPU {d['cpu']:.0f}%, RAM {d['ram']:.0f}% used, disk {d['disk']:.0f}% used ({d['disk_free_gb']} GB free)"]
    if (b := _battery_text()):
        parts.append(b)
    if (g := d.get("gpu")):
        parts.append(f"GPU {g['name']} at {g['util']}% load, {g['temp']} degrees, {g['used']} of {g['total']} MB video memory")
    parts.append(d["wifi"])
    parts.append(f"Uptime {int(d['uptime'] // 3600)}h {int(d['uptime'] % 3600 // 60)}m")
    if "heaviest" in d:
        parts.append(f"{d['processes']} processes, heaviest is {d['heaviest']} at {d['heaviest_mb']} MB")
    if "monitors" in d:
        bad = d["monitors_bad"]
        parts.append(f"{d['monitors'] - len(bad)} of {d['monitors']} security monitors active"
                     + (f" ({', '.join(bad)} need attention)" if bad else ""))
    return ". ".join(parts)


@tool("List the top processes by CPU or memory usage.",
      sort_by=("string", "'cpu' or 'memory'", False))
def list_processes(sort_by: str = "memory"):
    procs = []
    for p in psutil.process_iter(["name", "memory_info", "cpu_percent"]):
        try:
            procs.append((p.info["name"], p.info["memory_info"].rss >> 20, p.info["cpu_percent"] or 0))
        except Exception:
            pass
    key = (lambda x: x[2]) if sort_by == "cpu" else (lambda x: x[1])
    top = sorted(procs, key=key, reverse=True)[:8]
    return "; ".join(f"{n} {m} MB cpu {c:.0f}%" for n, m, c in top)


@tool("Network details: Wi-Fi name and signal, local IP address.")
def network_info():
    ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))        # no packet is sent; just picks the outbound interface
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    return _wifi_text() + (f". Local IP {ip}" if ip else "")


@tool("Current weather (needs internet).", city=("string", "city name; leave empty for the current location", False))
def weather(city: str = ""):
    city = city or Ctx.cfg.get("assistant", {}).get("city", "")
    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=%l:+%C,+%t,+feels+like+%f,+humidity+%h,+wind+%w"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        return urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "replace").strip()
    except Exception:
        return "I couldn't reach the weather service; the internet connection may be down."


# ------------------------------------------------------------------ security
@tool("Report Jarvis security monitor status and the most recent security alerts.")
def security_status():
    return Ctx.security.status_report() if Ctx.security else "Security monitors are not running."


@tool("Run a security sweep now (unsigned programs, open ports, startup entries, antivirus, who is at the keyboard). "
      "For 'scan the system', 'are we secure', 'any viruses'.")
def security_sweep():
    from .security.sweep import sweep
    return sweep(Ctx.cfg, Ctx.security)


# ------------------------------------------------------------------ security lab (own systems / CTF / authorised)
@tool("Scan YOUR OWN local network for live devices and open ports, and flag risky exposed services. "
      "For 'scan my network', 'what's on my wi-fi', 'any open ports on the network'.")
def network_scan():
    from .seclab import net_scan
    if Ctx.speaker:
        Ctx.speaker.say("Scanning the local network, sir. One moment.", log_it=False)
    return net_scan()


@tool("Audit THIS laptop's security: firewall, exposed listening ports, Defender, Remote Desktop, last update. "
      "For 'audit this laptop', 'check my security settings', 'how exposed am I'.")
def security_audit():
    from .seclab import local_audit
    return local_audit()


@tool("Port-scan a single host you OWN or are authorised to test (e.g. your own lab VM). Asks for confirmation "
      "that it's yours first. Use for 'scan my VM at 192.168.1.50', 'port scan my test server'.", risky=True,
      target=("string", "host name or IP of a machine you own / may test", True))
def scan_host(target: str):
    from .seclab import scan_target
    return scan_target(target)


@tool("Offline CTF / crypto helper: hash text, identify a hash or encoding, encode or decode "
      "(base64, base32, hex, url, rot13, binary), or dictionary-crack a hash you own.",
      operation=("string", "'hash', 'identify', 'encode', 'decode' or 'crack'", True),
      value=("string", "the text, hash or encoded string to work on", True),
      scheme=("string", "for hash/crack: md5/sha1/sha256/...; for encode/decode: base64/hex/rot13/...", False))
def ctf_tool(operation: str, value: str, scheme: str = ""):
    from . import seclab
    op = operation.lower().strip()
    if op == "hash":
        return seclab.ctf_hash(value, scheme or "sha256")
    if op in ("identify", "id", "detect"):
        return seclab.ctf_identify(value)
    if op == "encode":
        return seclab.ctf_encode(value, scheme or "base64")
    if op == "decode":
        return seclab.ctf_decode(value, scheme or "base64")
    if op == "crack":
        wl = ["password", "123456", "admin", "letmein", "qwerty", "welcome", "root", "toor",
              "password123", "secret", "flag", "ctf", "test", "guest", "changeme", "abc123", "iloveyou"]
        wlfile = ROOT / "data" / "wordlist.txt"       # drop your own wordlist here to extend the crack
        if wlfile.exists():
            try:
                wl += [w.strip() for w in wlfile.read_text(encoding="utf-8", errors="replace").splitlines() if w.strip()]
            except OSError:
                pass
        return seclab.ctf_crack(value, wl[:200000], scheme)
    return f"I can hash, identify, encode, decode or crack. Not '{operation}'."


# ------------------------------------------------------------------ apps & web
def _find_shortcut(name: str) -> Path | None:
    name = name.lower()
    roots = [Path(os.environ.get("ProgramData", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
             Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs",
             known_folder("desktop") or HOME / "Desktop"]
    best = None
    for root in roots:
        if not root.exists():
            continue
        for lnk in list(root.rglob("*.lnk")) + list(root.rglob("*.url")):
            stem = lnk.stem.lower()
            if "uninstall" in stem:
                continue
            if stem == name:
                return lnk
            if name in stem and (best is None or len(stem) < len(best.stem)):
                best = lnk
    return best


ALIASES = {"browser": "chrome", "google chrome": "chrome", "vs code": "visual studio code", "vscode": "visual studio code",
           # what speech recognition tends to make of "Chrome"
           "crow": "chrome", "curl": "chrome", "crome": "chrome", "krom": "chrome", "chrom": "chrome", "crown": "chrome",
           "chrome browser": "chrome", "the browser": "chrome",
           "code": "visual studio code", "file explorer": "explorer", "files": "explorer", "calculator": "calc",
           "command prompt": "cmd", "terminal": "wt", "settings": "ms-settings:", "task manager": "taskmgr",
           "notepad": "notepad", "paint": "mspaint", "edge": "microsoft edge", "word": "word", "camera": "microsoft.windows.camera:",
           "control panel": "control", "snipping tool": "snippingtool"}
ALIASES.update({"gallery": "photos", "my photos": "photos", "pictures app": "photos", "windows terminal": "wt",
                "microsoft store": "store", "app store": "store", "ms word": "word", "microsoft word": "word",
                "excel": "excel", "microsoft excel": "excel", "powerpoint": "powerpoint", "clock": "clock",
                "what's app": "whatsapp", "whats app": "whatsapp", "you tube": "youtube"})
BUILTINS = {"explorer", "calc", "cmd", "wt", "ms-settings:", "taskmgr", "notepad", "mspaint", "powershell", "control",
            "snippingtool", "microsoft.windows.camera:"}

# Every app in the Start menu (name -> AppID), read once in the background and kept on disk. Looking an app up used to
# mean starting PowerShell on every "open ..." (the live log shows "open gallery" taking 7.8 s); now it's a dictionary.
_APPS: dict[str, str] = {}
_APPS_FILE = ROOT / "data" / "start_apps.json"
_apps_loaded = 0.0


def refresh_apps(force: bool = False) -> dict[str, str]:
    """Load the Start-menu app list (from disk at once; from Windows if it is missing, forced or over a day old)."""
    global _apps_loaded
    import json
    if not _APPS and _APPS_FILE.exists():
        try:
            _APPS.update(json.loads(_APPS_FILE.read_text(encoding="utf-8")))
            _apps_loaded = _APPS_FILE.stat().st_mtime
        except Exception:
            pass
    if force or not _APPS or time.time() - _apps_loaded > 86400:
        try:
            out = _ps("Get-StartApps | ForEach-Object { $_.Name + [char]9 + $_.AppID }", timeout=40)
            apps = {}
            for line in out.splitlines():
                name, _, appid = line.partition("\t")
                if name.strip() and appid.strip() and "uninstall" not in name.lower():
                    apps[name.strip().lower()] = appid.strip()
            if apps:
                _APPS.clear()
                _APPS.update(apps)
                _apps_loaded = time.time()
                _APPS_FILE.parent.mkdir(parents=True, exist_ok=True)
                _APPS_FILE.write_text(json.dumps(apps, indent=0), encoding="utf-8")
        except Exception as e:
            log.warning("could not list Start-menu apps: %s", e)
    return _APPS


def known_app(name: str) -> str | None:
    """The Start-menu name that `name` most plausibly means: exact, then contained, then a near miss
    ("crome" -> "google chrome", "calculater" -> "calculator"). None if nothing is close."""
    import difflib
    n = name.lower().strip()
    if not n or not _APPS:
        return None
    if n in _APPS:
        return n
    inside = [a for a in _APPS if re.search(rf"\b{re.escape(n)}\b", a)]
    if inside:
        return min(inside, key=len)
    words = {w: a for a in _APPS for w in [a] + a.split()}
    close = difflib.get_close_matches(n, list(words), n=1, cutoff=0.8 if len(n) > 4 else 0.9)
    return words[close[0]] if close else None


@tool("Open / launch an application by name (e.g. chrome, spotify, file explorer, notepad, settings).",
      name=("string", "application name", True))
def open_app(name: str):
    target = ALIASES.get(name.lower().strip(), name.lower().strip())
    if target in BUILTINS:
        os.startfile(target) if target.endswith(":") else subprocess.Popen(target, shell=True)
        Ctx.last_opened = ("app", name)
        return f"Opened {name}."
    app = known_app(target)
    if app and (app == target or target in app.split() or target in app):
        subprocess.Popen(["explorer", f"shell:AppsFolder\\{_APPS[app]}"])
        Ctx.last_opened = ("app", name)
        return f"Opened {name}."
    lnk = _find_shortcut(target)
    if lnk:
        os.startfile(lnk)
        Ctx.last_opened = ("app", lnk.stem)
        return f"Opened {lnk.stem}."
    if app:                                              # a near miss ("crome"): better than giving up
        subprocess.Popen(["explorer", f"shell:AppsFolder\\{_APPS[app]}"])
        Ctx.last_opened = ("app", app)
        return f"Opened {app.title()}."
    if not _APPS:                                        # the app list hasn't loaded yet: ask Windows directly
        out = _ps(f"Get-StartApps | Where-Object {{ $_.Name -like '*{target.replace(chr(39), '')}*' }} "
                  f"| Select-Object -First 1 -ExpandProperty AppID")
        if out and "\n" not in out:
            subprocess.Popen(["explorer", f"shell:AppsFolder\\{out}"])
            Ctx.last_opened = ("app", name)
            return f"Opened {name}."
    return f"I couldn't find an app called {name}."


# Processes Jarvis must never terminate, whatever he is asked: the Windows shell and core system services.
PROTECTED = {"explorer.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "smss.exe",
             "svchost.exe", "dwm.exe", "system", "registry", "fontdrvhost.exe", "sihost.exe", "ctfmon.exe",
             "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe", "textinputhost.exe",
             "runtimebroker.exe", "taskhostw.exe", "audiodg.exe", "ollama.exe", "ollama app.exe", "llama-server.exe"}


_NEEDLES = {"vs code": "code", "visual studio code": "code", "edge": "msedge", "microsoft edge": "msedge",
            "file explorer": "explorer", "files": "explorer", "windows explorer": "explorer", "this pc": "explorer",
            "folder": "explorer", "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
            "calculator": "calculatorapp", "camera": "windowscamera", "gallery": "photos", "settings": "systemsettings",
            "task manager": "taskmgr", "command prompt": "cmd", "terminal": "windowsterminal", "paint": "mspaint",
            "browser": "chrome", "google chrome": "chrome", "snipping tool": "snippingtool", "store": "winstore.app"}


def _app_windows(needle: str, title: str) -> list[int]:
    """Visible top-level windows that belong to the app: by process name, or by title for Store apps (whose
    window is owned by ApplicationFrameHost rather than by the app's own process)."""
    u = ctypes.windll.user32
    names = {}
    for p in psutil.process_iter(["name", "pid"]):
        names[p.info["pid"]] = (p.info["name"] or "").lower()
    found, me = [], os.getpid()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if not u.IsWindowVisible(hwnd) or u.GetWindow(hwnd, 4):          # skip hidden and owned (dialog) windows
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc = names.get(pid.value, "")
        if pid.value == me or proc in PROTECTED:                          # never Jarvis himself or the Windows shell
            return True
        if (len(needle) >= 3 and needle in proc) or proc == needle + ".exe" or \
                (proc == "applicationframehost.exe" and len(title) >= 3 and title in buf.value.lower()):
            found.append(hwnd)
        return True
    u.EnumWindows(each, 0)
    return found


@tool("Close an application by name (e.g. chrome, notepad, camera), like clicking its X.",
      name=("string", "application name", True))
def close_app(name: str):
    key = name.lower().strip()
    key = re.sub(r"^(the|my)\s+|\s+(app|application|program|window|windows)$", "", key)
    needle = _NEEDLES.get(key, ALIASES.get(key, key)).replace(" ", "").removesuffix(".exe").rstrip(":")
    if needle in ("explorer", "fileexplorer"):
        # explorer.exe is also the taskbar and desktop: terminating it (as the first live test did) makes the
        # whole shell restart. Close just the folder windows instead.
        out = _ps("$n=0; (New-Object -ComObject Shell.Application).Windows() | Where-Object { $_.FullName -like "
                  "'*\\explorer.exe' } | ForEach-Object { $_.Quit(); $n++ }; $n")
        n = int(out) if out.strip().isdigit() else 0
        return f"Closed {n} File Explorer window(s)." if n else "No File Explorer windows are open."
    # WM_CLOSE, not TerminateProcess: nothing is lost (the app asks about unsaved work itself), which is why this
    # no longer needs a spoken "yes or no" first. The old way cost 5-6 s per "close ..." in live use.
    windows = _app_windows(needle, key)
    for hwnd in windows:
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
    if windows:
        if Ctx.last_opened and Ctx.last_opened[0] == "app" and key in str(Ctx.last_opened[1]).lower():
            Ctx.last_opened = None
        return f"Closed {len(windows)} {name} window(s)."
    return f"No {name} windows are open."


@tool("Open a website URL in the default browser.", url=("string", "URL or domain, e.g. github.com", True))
def open_website(url: str):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        if "." not in url:
            url = url.replace(" ", "") + ".com"
        url = "https://" + url
    webbrowser.open(url)
    Ctx.last_opened = ("tab", url)
    return f"Opened {url}."


@tool("Search the web (Google) or YouTube and show the results page.",
      query=("string", "search terms", True), site=("string", "'google' or 'youtube'", False))
def web_search(query: str, site: str = "google"):
    q = urllib.parse.quote_plus(query)
    url = (f"https://www.youtube.com/results?search_query={q}" if site == "youtube"
           else f"https://www.google.com/search?q={q}")
    webbrowser.open(url)
    Ctx.last_opened = ("tab", url)
    return f"Searching {site} for {query}."


@tool("Play a song or video: finds it on YouTube and starts playing the top result.",
      query=("string", "song / video to play", True))
def play_on_youtube(query: str):
    q = urllib.parse.quote_plus(query)
    Ctx.last_opened = ("tab", "youtube")
    try:
        req = urllib.request.Request(f"https://www.youtube.com/results?search_query={q}",
                                     headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
        html = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "replace")
        vid = re.search(r'"videoId":"([\w-]{11})"', html)
        if vid:
            webbrowser.open(f"https://www.youtube.com/watch?v={vid.group(1)}")
            return f"Playing {query} on YouTube."
    except Exception as e:
        log.info("youtube lookup failed: %s", e)
    webbrowser.open(f"https://www.youtube.com/results?search_query={q}")
    return f"I opened the YouTube results for {query}."


_EXT_DIR = str(ROOT / "extension")


def _coursera_help() -> str:
    """The right message when Jarvis can't reach a Coursera tab: either the extension isn't loaded, or no lecture
    is open. When it has never loaded, open chrome://extensions once so the one-time setup is a click away."""
    from .coursera import BRIDGE
    if not BRIDGE.installed:
        if not getattr(BRIDGE, "prompted", False):
            BRIDGE.prompted = True
            for opener in (lambda: subprocess.Popen([_chrome_exe(), "chrome://extensions/"]) if _chrome_exe() else None,
                           lambda: webbrowser.open("chrome://extensions/")):
                try:
                    if opener():
                        break
                except Exception:
                    continue
        return (f"The Coursera helper isn't loaded in Chrome yet, so I can't control the videos. I've opened "
                f"chrome://extensions - switch on Developer mode, click Load unpacked, and choose the folder "
                f"{_EXT_DIR}. Then reload your Coursera tab and I'll take over.")
    return "I don't see a Coursera lecture open in Chrome. Open one and I'll play it and carry on from there."


def _chrome_exe() -> str | None:
    for p in (os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
              os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
              os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe")):
        if os.path.exists(p):
            return p
    return None


# hidden: every Coursera phrasing is routed here by the reflexes (router._coursera), so the model needn't see it
@tool("Coursera lessons in Chrome: open or resume a course, play, pause, next/previous video, speed, autoplay on/off, status.",
      hidden=True,
      action=("string", "'open', 'resume' (open and autoplay), 'progress', 'dashboard', 'play', 'pause', 'next', 'previous', 'restart', 'forward', 'back', 'speed', "
                        "'autoplay_on', 'autoplay_off' or 'status'", True),
      value=("string", "course name for 'open'; rate like 1.5 for 'speed'; seconds for forward/back", False))
def coursera(action: str, value: str = ""):
    from .coursera import BRIDGE
    a = action.lower().strip().replace(" ", "_")
    v = (value or "").strip()
    if a in ("open", "resume", "continue"):
        autoplay = a != "open"                       # "resume my course" also switches autoplay on
        if BRIDGE.active() and not v:
            if autoplay:
                return BRIDGE.send("binge", "on") or "Resuming Coursera."
            return "Coursera is already open. " + (BRIDGE.status() or "")
        course = BRIDGE.find_course(v)
        if autoplay:
            BRIDGE.autostart_until = time.time() + 90
        if course:
            webbrowser.open(course["url"])
            Ctx.last_opened = ("tab", course["url"])
            return f"Opening {course.get('name') or 'your course'} where you left off: {course.get('title') or 'last lesson'}."
        url = ("https://www.coursera.org/search?query=" + urllib.parse.quote_plus(v)) if v \
            else "https://www.coursera.org/my-learning"
        webbrowser.open(url)
        Ctx.last_opened = ("tab", url)
        then = " Open a lecture and I'll play it and carry on to the next." if autoplay else ""
        if autoplay and not BRIDGE.installed:
            then = " " + _coursera_help()
        return (f"I don't know that course yet, so I've searched Coursera for {v}.{then}"
                if v else f"Opening My Learning on Coursera.{then}")
    if a == "status":
        return BRIDGE.status() or _coursera_help()
    if a in ("progress", "summary"):
        return BRIDGE.progress_summary()
    if a == "dashboard":
        url = f"http://127.0.0.1:{BRIDGE.port}/dashboard"
        webbrowser.open(url)
        Ctx.last_opened = ("tab", url)
        return "Opening your Coursera progress dashboard."
    cmd, val = {"play": ("play", ""), "resume_video": ("play", ""), "pause": ("pause", ""), "stop": ("pause", ""),
                "next": ("next", ""), "previous": ("previous", ""), "restart": ("restart", ""),
                "forward": ("seek", v or "10"), "back": ("seek", "-" + (v.lstrip("-") or "10")),
                "speed": ("speed", v), "autoplay_on": ("binge", "on"), "binge_on": ("binge", "on"),
                "autoplay_off": ("binge", "off"), "binge_off": ("binge", "off")}.get(a, (None, None))
    if not cmd:
        return f"Unknown Coursera action {action}."
    r = BRIDGE.send(cmd, val)
    if r is None:
        return _coursera_help()
    return r or "Done, though the Coursera page didn't confirm it."


# ------------------------------------------------------------------ system control
@tool("Control media playback.", action=("string", "'play_pause', 'next' or 'previous'", True))
def media_control(action: str):
    _press({"next": 0xB0, "previous": 0xB1}.get(action, 0xB3))
    return f"Media {action.replace('_', ' ')}."


@tool("Change system volume.",
      action=("string", "'up', 'down', 'mute' or 'set'", True), level=("integer", "0-100, only for 'set'", False))
def volume(action: str, level: int = 50):
    if action == "mute":
        _press(0xAD)
    elif action == "up":
        _press(0xAF, 5)
    elif action == "down":
        _press(0xAE, 5)
    else:  # each key press is 2 %
        _press(0xAE, 50)
        _press(0xAF, max(0, min(100, int(level))) // 2)
    return f"Volume {action}{' ' + str(level) if action == 'set' else ''}."


@tool("Set the laptop screen brightness.", level=("integer", "0-100", True))
def set_brightness(level: int):
    level = max(0, min(100, int(level)))
    out = _ps(f"(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | "
              f"Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout=1;Brightness={level}}}) | Out-Null")
    return f"Brightness set to {level} percent." if not out else f"I couldn't change the brightness: {out[:120]}"


@tool("Lock the computer screen immediately.")
def lock_screen():
    ctypes.windll.user32.LockWorkStation()
    return "Screen locked."


@tool("Shut down, restart, sleep or sign out of the computer.", risky=True,
      action=("string", "'shutdown', 'restart', 'sleep' or 'signout'", True))
def power(action: str):
    cmds = {"shutdown": "shutdown /s /t 10", "restart": "shutdown /r /t 10",
            "signout": "shutdown /l", "sleep": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0"}
    if action not in cmds:
        return "Unknown power action."
    subprocess.Popen(cmds[action], shell=True)
    return f"{action.capitalize()} in progress."


def _capture_screen(out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    _ps("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
        "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
        "$g=[System.Drawing.Graphics]::FromImage($bmp);$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size);"
        f"$bmp.Save('{out}')")


@tool("Take a screenshot and save it to Pictures\\Jarvis.")
def screenshot():
    out = (known_folder("pictures") or HOME / "Pictures") / "Jarvis" / f"screenshot_{dt.datetime.now():%Y%m%d_%H%M%S}.png"
    _capture_screen(out)
    return f"Screenshot saved to {out}." if out.exists() else "The screenshot failed."


OCR_SCRIPT = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) { $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result }
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync('__PATH__')) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('en-US')) }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$result.Lines | ForEach-Object { $_.Text }
"""


def ocr_image(path: Path) -> str:
    """Offline text recognition using the OCR engine built into Windows."""
    return _ps(OCR_SCRIPT.replace("__PATH__", str(path).replace("'", "''")), timeout=40)


@tool("Read the text currently visible on the screen. For 'what's on my screen', 'read this', 'can you see my screen'.")
def read_screen():
    tmp = ROOT / "data" / "tmp" / "screen.png"
    _capture_screen(tmp)
    if not tmp.exists():
        return "I couldn't capture the screen."
    try:
        text = ocr_image(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return ("Text on screen:\n" + text[:1800]) if text else "I couldn't make out any text on the screen."


@tool("Manage windows.", action=("string", "'show_desktop', 'minimize', 'maximize', 'restore', 'switch', 'snap_left', "
                                           "'snap_right' or 'close'", True))
def window_control(action: str):
    u = ctypes.windll.user32
    hwnd = u.GetForegroundWindow()
    a = action.lower().strip()
    if a in ("show_desktop", "minimize_all"):
        _combo(0x5B, 0x44)                 # Win+D
    elif a == "minimize":
        u.ShowWindow(hwnd, 6)
    elif a == "maximize":
        u.ShowWindow(hwnd, 3)
    elif a == "restore":
        u.ShowWindow(hwnd, 9)
    elif a == "switch":
        _combo(0x12, 0x09)                 # Alt+Tab
    elif a == "snap_left":
        _combo(0x5B, 0x25)
    elif a == "snap_right":
        _combo(0x5B, 0x27)
    elif a == "close":
        u.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE: lets the app ask about unsaved work
    else:
        return f"Unknown window action {action}."
    return f"Window {a.replace('_', ' ')} done."


@tool("Scroll the window under the mouse pointer (web pages, documents, lists).",
      direction=("string", "'up', 'down', 'top' or 'bottom'", True),
      amount=("string", "'small', 'page' (default) or 'large'", False))
def scroll(direction: str, amount: str = "page"):
    d = direction.lower().strip()
    if d in ("top", "bottom"):
        _combo(0x11, 0x24 if d == "top" else 0x23)          # Ctrl+Home / Ctrl+End
        return f"Jumped to the {d}."
    notches = {"small": 3, "page": 8, "large": 20}.get(str(amount).lower(), 8)
    for _ in range(notches):
        ctypes.windll.user32.mouse_event(0x0800, 0, 0, 120 if d == "up" else -120, 0)   # MOUSEEVENTF_WHEEL
        time.sleep(0.015)
    return f"Scrolled {d}."


_VK = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "windows": 0x5B, "enter": 0x0D,
       "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "spacebar": 0x20, "backspace": 0x08,
       "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "page up": 0x21, "pagedown": 0x22,
       "page down": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "insert": 0x2D,
       "printscreen": 0x2C, "print screen": 0x2C, **{f"f{i}": 0x6F + i for i in range(1, 13)}}


@tool("Press a key or shortcut in the focused window, e.g. ctrl+t (new tab), ctrl+w (close tab), alt+left (back), "
      "ctrl+tab (next tab), f5 (refresh), f11 (full screen), ctrl+s, ctrl+z, ctrl+f, enter, esc, space.",
      keys=("string", "key or combination joined with +", True))
def press_keys(keys: str):
    parts = [p.strip().lower() for p in str(keys).replace(" + ", "+").split("+") if p.strip()]
    vks = []
    for p in parts:
        vk = _VK.get(p) or (ord(p.upper()) if len(p) == 1 and p.isalnum() else None)
        if vk is None:
            return f"I don't know the key '{p}'."
        vks.append(vk)
    if not vks:
        return "No keys given."
    # Win+R / Win+X followed by typing would be a way around the "risky command" confirmation, so only harmless
    # Windows-key shortcuts are allowed here.
    if 0x5B in vks and set(parts) - {"win", "windows"} - {"d", "e", "tab", "up", "down", "left", "right", "shift", "s"}:
        return "That Windows-key shortcut isn't allowed."
    _combo(*vks)
    return f"Pressed {'+'.join(parts)}."


if os.name == "nt":
    class _KI(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]

    class _INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", _KI), ("pad", ctypes.c_byte * 32)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]


@tool("Type text into the window that currently has focus (dictation).", text=("string", "text to type", True))
def type_text(text: str):
    send = ctypes.windll.user32.SendInput
    for ch in text:
        if ch == "\n":
            _press(0x0D)
            continue
        code = ord(ch)
        for unit in ([code] if code < 0x10000 else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]):
            for flags in (0x0004, 0x0004 | 0x0002):       # KEYEVENTF_UNICODE down, up
                inp = _INPUT(type=1)
                inp.ki = _KI(0, unit, flags, 0, None)
                send(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        time.sleep(0.004)
    return f"Typed {len(text)} characters."


# Starter code: (lines, index of the empty line where the user's own logic goes). Lines carry no indentation:
# code editors indent by themselves as you type.
CODE_TEMPLATES = {
    "java": (["import java.util.*;", "", "public class {cls} {{", "public static void main(String[] args) {{",
              "Scanner sc = new Scanner(System.in);", "", "}}", "}}"], 5),
    "cpp": (["#include <bits/stdc++.h>", "using namespace std;", "", "int main() {{", "", "return 0;", "}}"], 4),
    "c": (["#include <stdio.h>", "", "int main() {{", "", "return 0;", "}}"], 3),
    "python": (["import sys", "", ""], 2),
}
_LANG = {"java": "java", "c++": "cpp", "cpp": "cpp", "c plus plus": "cpp", "c": "c", "python": "python", "py": "python"}


@tool("Type a starter code template (imports, class, main method) into the code editor that has focus, replacing "
      "what's in it, and leave the cursor where the logic goes. The user writes the logic.",
      language=("string", "'java', 'cpp', 'c' or 'python'", True),
      class_name=("string", "Java class name (default Main)", False))
def type_code_template(language: str, class_name: str = "Main"):
    lang = _LANG.get(language.lower().strip())
    if not lang:
        return f"I have templates for Java, C++, C and Python, not {language}."
    cls = re.sub(r"\W", "", class_name or "") or "Main"
    lines, logic_at = CODE_TEMPLATES[lang]
    lines = [ln.format(cls=cls) for ln in lines]
    CTRL, SHIFT = 0x11, 0x10
    time.sleep(0.3)
    _combo(CTRL, 0x41)                       # select everything in the editor ...
    _press(0x2E)                             # ... and clear it
    for i, line in enumerate(lines):
        if line:
            # a line ending in a word may leave the editor's suggestion list open, and Enter would accept a
            # suggestion: a trailing space closes the list
            type_text(line + (" " if re.search(r"\w$", line) else ""))
        if i < len(lines) - 1:
            _press(0x0D)
        time.sleep(0.03)
    # Editors that close brackets by themselves have left extra "}" lines below the cursor: remove them.
    _combo(CTRL, SHIFT, 0x23)
    _press(0x2E)
    _combo(CTRL, 0x24)                       # back to the top, then down to the empty logic line
    _press(0x28, logic_at)
    _press(0x23)
    name = {"java": "Java", "cpp": "C++", "c": "C", "python": "Python"}[lang]
    return f"{name} template typed. The cursor is on the line for your logic."


@tool("Read or set the clipboard text.", action=("string", "'read' or 'write'", True),
      text=("string", "text to copy, only for 'write'", False))
def clipboard(action: str, text: str = ""):
    if action == "write":
        subprocess.run(["clip"], input=text, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        return "Copied to the clipboard."
    out = _ps("Get-Clipboard -Raw")
    return ("Clipboard: " + out[:1500]) if out else "The clipboard is empty."


@tool("Empty the Windows Recycle Bin permanently.", risky=True)
def empty_recycle_bin():
    _ps("Clear-RecycleBin -Force -ErrorAction SilentlyContinue")
    return "Recycle Bin emptied."


# ------------------------------------------------------------------ maths
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {n: getattr(math, n) for n in ("sqrt", "sin", "cos", "tan", "log", "log10", "log2", "exp", "floor", "ceil",
                                         "radians", "degrees", "factorial")} | {"abs": abs, "round": round}
_CONSTS = {"pi": math.pi, "e": math.e}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    raise ValueError("unsupported expression")


@tool("Evaluate a maths expression exactly, e.g. '18% of 2450', 'sqrt(2)*3', '(45+12)/3'. Always use for arithmetic.",
      expression=("string", "the expression", True))
def calculate(expression: str):
    e = expression.lower().replace("^", "**").replace("×", "*").replace("÷", "/").replace(",", "")
    e = re.sub(r"(?<=[\d)])\s*x\s*(?=[\d(])", "*", e)          # "12 x 4"
    e = re.sub(r"\b(what is|what's|calculate|equals?)\b|[=?]", "", e).strip()
    e = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"(\1/100)*", e)
    e = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", e)
    try:
        val = _eval(ast.parse(e, mode="eval").body)
    except Exception as ex:
        return f"I couldn't evaluate {expression}: {ex}"
    if isinstance(val, float):
        val = round(val, 6)
        val = int(val) if val == int(val) else val
    return f"{expression} = {val}"


# ------------------------------------------------------------------ notes, memory, reminders
@tool("Save a note for later.", text=("string", "the note", True))
def take_note(text: str):
    path = resolve(Ctx.cfg["actions"]["notes_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] {text}\n")
    return "Noted."


@tool("Read back the most recent saved notes.")
def read_notes():
    path = resolve(Ctx.cfg["actions"]["notes_file"])
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return "You have no notes."
    return " ".join(path.read_text(encoding="utf-8").strip().splitlines()[-5:])


@tool("Remember a lasting fact about the user (name, birthday, preferences). For 'remember that ...'.",
      fact=("string", "the fact, as a full sentence", True))
def remember(fact: str):
    return memory.remember(fact)


@tool("Forget remembered facts that mention something.", about=("string", "keywords of the fact to forget", True))
def forget(about: str):
    return memory.forget(about)


@tool("Set a reminder, timer or alarm.",
      when=("string", "when to fire: 'in 10 minutes', 'in 45 seconds', '5:30 pm', 'tomorrow 9 am'", True),
      message=("string", "what to remind about (use 'timer' for a plain timer)", True))
def set_reminder(when: str, message: str):
    at = parse_when(str(when))
    if not at:
        return f"I couldn't understand the time '{when}'."
    Ctx.reminders.add(at, message)
    return f"Reminder set {describe(at)}: {message}."


@tool("List pending reminders and timers.")
def list_reminders():
    items = Ctx.reminders.pending()
    if not items:
        return "There are no pending reminders."
    return "; ".join(f"{i['message']} {describe(dt.datetime.fromtimestamp(i['at']))}" for i in items[:8])


@tool("Cancel reminders whose message mentions something ('all' cancels every reminder).",
      about=("string", "keyword, or 'all'", True))
def cancel_reminder(about: str):
    n = Ctx.reminders.cancel(about)
    return f"Cancelled {n} reminder(s)." if n else "No matching reminder."


# Hidden from the model: the system prompt + tool list already fill ~3,700 of its 4,096 tokens. Deadline / calendar /
# email questions are answered by reflexes (router._agenda), and anything that reaches the model about them gets the
# real data put in front of it first (brain.FORCED_TOOLS), so it never has to ask for it.
@tool("The user's Gmail and Google Calendar: deadlines, assignments and anything due, today's or tomorrow's "
      "schedule, unread emails. Never guess these.", hidden=True,
      action=("string", "'overview', 'deadlines', 'today', 'tomorrow', 'emails', 'refresh', or 'done' (mark a "
                        "deadline finished; value = its name)", True),
      value=("string", "for 'done': which deadline", False))
def agenda(action: str = "overview", value: str = ""):
    a = Ctx.agenda
    if a is None:
        return "Calendar and email tracking is switched off ([agenda] enabled = false in config.toml)."
    action = (action or "overview").lower().strip()
    if action == "deadlines":
        return a.deadlines_text()
    if action in ("today", "tomorrow", "calendar", "schedule"):
        return a.day_text(1 if action == "tomorrow" else 0)
    if action in ("emails", "email", "mail", "inbox"):
        return a.mail_text()
    if action in ("refresh", "sync"):
        if not a.connected():
            return a.deadlines_text()
        ok = a.sync()
        return ("Synced, sir. " if ok else "Synced, with some trouble, sir. ") + a.deadlines_text()
    if action == "done":
        return a.mark_done(value)
    return a.overview()


# ------------------------------------------------------------------ files
@tool("Find files by name under a folder (default: home folder).",
      pattern=("string", "part of the file name", True), folder=("string", "folder to search, e.g. Documents", False))
def find_files(pattern: str, folder: str = ""):
    base = _user_path(folder) if folder else HOME
    hits, start = [], time.time()
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("AppData", "Library", "node_modules", ".venv")]
        hits += [os.path.join(root, f) for f in files if pattern.lower() in f.lower()]
        if len(hits) >= 10 or time.time() - start > 8:
            break
    return "; ".join(hits[:10]) or f"No files matching {pattern} in {base}."


@tool("Open a file or folder with its default program. Folder names like Downloads, Documents, Desktop, Pictures work.",
      path=("string", "full path, or a folder/file under the home folder", True))
def open_path(path: str):
    p = _user_path(path)
    if not p.exists():
        # The model sometimes passes an application name here ("File Explorer"): treat it as one.
        if not re.search(r"[\\/:.]", path) and (path.lower().strip() in ALIASES or path.lower().strip() in BUILTINS
                                                 or _find_shortcut(path.lower().strip())):
            return open_app(path)
        return f"{p} does not exist."
    os.startfile(p)
    return f"Opened {p}."


@tool("Create a new folder.", path=("string", "folder path (relative paths are under home, e.g. Desktop/Projects)", True))
def create_folder(path: str):
    p = _user_path(path)
    p.mkdir(parents=True, exist_ok=True)
    return f"Created {p}."


@tool("Delete a file or folder (moves it to the Recycle Bin).", risky=True,
      path=("string", "full path to delete", True))
def delete_path(path: str):
    p = _user_path(path)
    if not p.exists():
        return f"{p} does not exist."
    kind = "DeleteDirectory" if p.is_dir() else "DeleteFile"
    safe = str(p).replace("'", "''")
    _ps(f"Add-Type -AssemblyName Microsoft.VisualBasic;"
        f"[Microsoft.VisualBasic.FileIO.FileSystem]::{kind}('{safe}','OnlyErrorDialogs','SendToRecycleBin')")
    return f"Moved {p} to the Recycle Bin." if not p.exists() else f"Could not delete {p}."


@tool("The user's own files: convert images to another type (png, jpg, webp, bmp, gif, tiff, ico, pdf; Word/PowerPoint/"
      "Excel to pdf), compress or zip files, or put their images into one PDF ('one document') or PowerPoint.",
      action=("string", "'convert', 'compress', 'zip', 'pdf' or 'pptx'", True),
      files=("string", "'selected' (selected in File Explorer), e.g. 'last 4 images', or file names", True),
      to=("string", "target type for convert, e.g. png", False),
      save_to=("string", "folder to save in; empty = next to the originals", False))
def file_tools(action: str, files: str = "selected", to: str = "", save_to: str = "", name: str = ""):
    from . import filetools as ft
    a = action.lower().strip().replace(" ", "_")
    try:
        paths, how = ft.resolve_files(files, "image" if a in ("pdf", "pptx", "ppt", "powerpoint", "slides") else "")
        if a == "convert":
            done = ft.convert(paths, to or "png", save_to, name)
        elif a in ("compress", "shrink"):
            done = ft.compress(paths, save_to, name)
        elif a == "zip":
            done = ft.compress(paths, save_to, name, zip_it=True)
        elif a == "pdf":
            done = ft.make_pdf(paths, save_to, name)
        elif a in ("pptx", "ppt", "powerpoint", "slides"):
            done = ft.make_pptx(paths, save_to, name)
        else:
            return f"Unknown action '{action}'. Use convert, compress, zip, pdf or pptx."
    except ft.FileError as e:
        return str(e)
    except Exception as e:
        log.exception("file_tools failed")
        return f"That didn't work, sir: {e}"
    if how.startswith(("the first", "the last")):                 # picked by position: say which, so a wrong guess shows
        names = [p.name for p in paths]
        listed = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]
        return f"Using {how}: {listed}. {done}"
    return done


@tool("Research a subject online and build a PowerPoint presentation about it, then open it. Not for the user's own "
      "pictures or files (use file_tools).",
      topic=("string", "what the presentation is about", True),
      slides=("integer", "number of content slides, default 8", False))
def make_presentation(topic: str, slides: int = 8, save_to: str = ""):
    from . import filetools as ft
    # The small model sometimes sends "slides of the pictures I picked" here: those are the user's files, not a subject.
    if re.search(r"\b(selected|picked|chosen|chose|highlighted|these|those)\b|^(?:my |the )?(?:selected |picked )?"
                 r"(?:pictures|photos|images|screenshots|files|pics)$", topic.strip().lower()):
        return file_tools("pptx", "selected", save_to=save_to)

    def done(msg: str):
        if Ctx.report:
            Ctx.report(msg)
        elif Ctx.speaker:
            Ctx.speaker.say(msg)
    try:
        return ft.presentation(topic, slides, save_to, Ctx.cfg, done)
    except ft.FileError as e:
        return str(e)


@tool("Force-kill a process (unsaved work is lost). Only for 'kill', 'force close' or a frozen app; otherwise use "
      "close_app.", risky=True, target=("string", "process name or PID", True))
def kill_process(target: str):
    n, me = 0, os.getpid()
    target = str(target).lower().strip()
    target = _NEEDLES.get(target, target).replace(" ", "")
    for p in psutil.process_iter(["name", "pid"]):
        if p.info["pid"] == me or (p.info["name"] or "").lower() in PROTECTED:
            continue
        if str(p.info["pid"]) == target or (p.info["name"] or "").lower() in (target.lower(), target.lower() + ".exe"):
            try:
                p.kill()
                n += 1
            except Exception:
                pass
    return f"Killed {n} process(es)." if n else f"No process {target}."


@tool("Run a PowerShell command and return its output. Use only when no other tool fits.", risky=True,
      command=("string", "PowerShell command", True))
def run_command(command: str):
    return _ps(command, timeout=60)[:1500] or "Done (no output)."


# ------------------------------------------------------------------ protocols & self-control
def protocol_names() -> list[str]:
    return list(Ctx.cfg.get("protocols", {}).keys())


@tool("Engage a named protocol (a preset routine). For '<name> protocol' or 'initiate <name>'.",
      name=("string", "protocol name", True))
def run_protocol(name: str):
    protos = Ctx.cfg.get("protocols", {})
    key = re.sub(r"[^a-z0-9]+", "_", name.lower().replace("protocol", "")).strip("_")
    proto = protos.get(key) or next((p for k, p in protos.items() if key and (key in k or k in key)), None)
    if not proto:
        return f"There is no protocol called {name}. Available protocols: {', '.join(protos) or 'none'}."
    results = []
    for step in proto.get("steps", []):
        tool_name, args = step[0], (step[1] if len(step) > 1 else {})
        results.append(Ctx.execute(tool_name, dict(args)) if Ctx.execute else str(REGISTRY[tool_name].func(**args)))
        time.sleep(0.4)
    say = proto.get("say", f"{key.replace('_', ' ')} protocol engaged.")
    failed = [r for r in results if any(w in r.lower() for w in ("couldn't", "failed", "unknown", "declined", "refused"))]
    return say + (" Note: " + " ".join(failed) if failed else "")


@tool("Mute or unmute Jarvis's own voice (replies still appear on screen).",
      action=("string", "'mute' or 'unmute'", True))
def jarvis_voice(action: str):
    if not Ctx.speaker:
        return "Voice output is unavailable."
    Ctx.speaker.enabled = action != "mute"
    return "Voice muted. I'll reply on screen only." if action == "mute" else "Voice restored."


# hidden: hide/show/console/colour/size/move of the orb are all reflexes (router.route), so the model needn't see it
@tool("Control Jarvis's own on-screen orb (the glowing ball).", hidden=True,
      action=("string", "'hide', 'show', 'console' (typing window), 'close_console', 'colour', 'bigger', 'smaller' or 'move'", True),
      value=("string", "colour: gold, blue, green, purple, red, white. move: top left, top right, bottom left, bottom right, centre", False))
def orb(action: str, value: str = ""):
    from .state import STATE
    a = action.lower().strip().replace(" ", "_")
    v = (value or "").lower().strip()
    if a in ("hide", "show"):
        STATE.ui(a)
        return "Orb hidden. Say 'show yourself' to bring it back." if a == "hide" else "Orb shown."
    if a in ("console", "open_console", "close_console"):
        STATE.ui("console", a != "close_console")
        return "Console closed." if a == "close_console" else "Console opened."
    if a in ("colour", "color", "theme"):
        if not v:
            return "Which colour? gold, blue, green, purple, red or white."
        STATE.ui("theme", v)
        return f"Colour changed to {v}."
    if a in ("bigger", "larger", "smaller"):
        STATE.ui("size", -1 if a == "smaller" else 1)
        return f"Orb made {a}."
    if a == "move":
        STATE.ui("move", v or "bottom right")
        return f"Orb moved to the {v or 'bottom right'}."
    return f"Unknown orb action {action}."


def notify(title: str, body: str):
    toast(title, body)


def open_file(path):
    """Open a file or folder in its default program."""
    os.startfile(path)


# On a Mac, swap everything above that talks to Windows for its macOS version (same names, same arguments).
import sys as _sys
if _sys.platform == "darwin":
    from . import macos as _macos
    open_file = _macos.open_file
    _macos.install()
