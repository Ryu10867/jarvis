"""macOS versions of everything in tools.py that talks to the operating system.

tools.py was written for Windows (Win32 calls, PowerShell). Rather than fork it, `install()` swaps the Windows
implementations for the ones below when Jarvis runs on a Mac: same tool names, same arguments, same replies, so the
reflex router, the language model's tool list and the protocols all work unchanged.

How things are done here:
  * apps, windows, volume, power, notifications, Trash   AppleScript through `osascript`
  * key presses and typing                                System Events (needs Accessibility permission, see below)
  * screenshots                                           `screencapture` (needs Screen Recording permission)
  * reading the screen (OCR)                              Apple's Vision framework through PyObjC
  * media keys                                            Spotify / Music by AppleScript, else the hardware media key
macOS asks for each permission the first time it is needed: System Settings > Privacy & Security > Accessibility /
Screen Recording / Microphone / Camera, for the app Jarvis runs in (Terminal, unless you start him another way).
"""
import datetime as dt
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

import psutil

log = logging.getLogger("jarvis.macos")
HOME = Path.home()
PERMISSION = ("macOS blocked that. Allow it in System Settings > Privacy & Security > {what} for the app Jarvis "
              "runs in (Terminal), then try again.")


# ===================================================================== helpers
def sh(args: list[str], timeout: int = 20, text_in: str | None = None) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=text_in)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return f"error: {e}"


def osa(script: str, timeout: int = 20) -> str:
    """Run AppleScript; returns its output, or a line starting 'error:' if it failed."""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return f"error: {e}"
    if r.returncode != 0:
        return "error: " + (r.stderr or r.stdout).strip()
    return r.stdout.strip()


def q(s: str) -> str:
    """Quote text for AppleScript."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _denied(out: str) -> bool:
    return out.startswith("error:") and bool(re.search(r"not allowed|assistive|1002|-1743|-25211|not authori[sz]ed", out, re.I))


def notify(title: str, body: str):
    try:
        subprocess.Popen(["osascript", "-e", f"display notification {q(body[:240])} with title {q(title)}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log.debug("notification failed: %s", e)


def lock():
    """Lock the screen: the Ctrl+Cmd+Q shortcut, or putting the display to sleep if keystrokes aren't allowed."""
    out = osa('tell application "System Events" to keystroke "q" using {control down, command down}')
    if out.startswith("error:"):
        sh(["pmset", "displaysleepnow"])


def screen_locked() -> bool:
    try:
        import Quartz
        d = Quartz.CGSessionCopyCurrentDictionary()
        return bool(d and d.get("CGSSessionScreenIsLocked", 0))
    except Exception:
        return False


def open_file(path) -> None:
    subprocess.Popen(["open", str(path)])


def chrome_exe() -> str | None:
    for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              str(HOME / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome")):
        if os.path.exists(p):
            return p
    return None


def signer(exe: str) -> str | None:
    """Who signed a program (Developer ID / Apple), or None when it is unsigned or the signature is broken."""
    if not exe:
        return None
    if subprocess.run(["codesign", "--verify", exe], capture_output=True, timeout=20).returncode != 0:
        return None
    out = sh(["codesign", "-dvv", exe])
    m = re.search(r"^Authority=(.+)$", out, re.M)
    if not m:
        return None                                   # ad-hoc signature: anyone can make one
    name = m.group(1)
    return re.sub(r"^Developer ID Application: |\s*\([A-Z0-9]{10}\)$", "", name) or name


# ===================================================================== keyboard
KEYCODES = {"enter": 36, "return": 36, "tab": 48, "space": 49, "spacebar": 49, "backspace": 51, "esc": 53, "escape": 53,
            "delete": 117, "del": 117, "home": 115, "end": 119, "pageup": 116, "page up": 116, "pagedown": 121,
            "page down": 121, "left": 123, "right": 124, "down": 125, "up": 126, "f1": 122, "f2": 120, "f3": 99,
            "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111}
MODS = {"cmd": "command down", "command": "command down", "ctrl": "control down", "control": "control down",
        "alt": "option down", "option": "option down", "opt": "option down", "shift": "shift down"}
# Shortcuts are spoken (and written in the reflex table) the Windows way; this is what each one is on a Mac.
TRANSLATE = {"f5": "cmd+r", "ctrl+f5": "cmd+shift+r", "f11": "ctrl+cmd+f", "alt+left": "cmd+[", "alt+right": "cmd+]",
             "ctrl+home": "cmd+up", "ctrl+end": "cmd+down", "alt+f4": "cmd+q", "alt+tab": "cmd+tab", "ctrl+y": "cmd+shift+z",
             "home": "cmd+left", "end": "cmd+right", "ctrl+shift+home": "cmd+shift+up", "ctrl+shift+end": "cmd+shift+down",
             "win+d": "f11", "win+e": "cmd+shift+option+f", "ctrl+tab": "ctrl+tab", "ctrl+shift+tab": "ctrl+shift+tab",
             "ctrl+left": "option+left", "ctrl+right": "option+right", "ctrl+backspace": "option+backspace"}


def mac_combo(keys: str) -> str:
    """'ctrl+t' -> 'cmd+t', 'alt+left' -> 'cmd+[' ... (Ctrl+Tab stays as it is: that is the Mac shortcut too)."""
    k = "+".join(p.strip().lower() for p in str(keys).replace(" + ", "+").split("+") if p.strip())
    if k in TRANSLATE:
        return TRANSLATE[k]
    parts = k.split("+")
    out = ["cmd" if p in ("ctrl", "control", "win", "windows") else "option" if p == "alt" else p for p in parts[:-1]]
    return "+".join(out + parts[-1:])


def key(combo: str, times: int = 1) -> str:
    """Press a Mac key combination such as 'cmd+shift+t', 'return', 'f11'. Returns '' or an 'error: ...' line."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        return "error: no keys"
    mods = [MODS[p] for p in parts[:-1] if p in MODS]
    unknown = [p for p in parts[:-1] if p not in MODS]
    last = parts[-1]
    if unknown:
        return f"error: unknown key {unknown[0]}"
    using = f" using {{{', '.join(mods)}}}" if mods else ""
    if last in KEYCODES:
        action = f"key code {KEYCODES[last]}{using}"
    elif len(last) == 1:
        action = f"keystroke {q(last)}{using}"
    else:
        return f"error: unknown key {last}"
    body = action if times == 1 else f"repeat {int(times)} times\n{action}\ndelay 0.01\nend repeat"
    return osa(f'tell application "System Events"\n{body}\nend tell')


def press_keys(keys: str):
    spoken = "+".join(p.strip().lower() for p in str(keys).replace(" + ", "+").split("+") if p.strip())
    if not spoken:
        return "No keys given."
    out = key(mac_combo(spoken))
    if _denied(out):
        return PERMISSION.format(what="Accessibility")
    if out.startswith("error: unknown key"):
        return f"I don't know the key '{out.split()[-1]}'."
    return f"Pressed {spoken}." if not out.startswith("error:") else f"I couldn't press {spoken}: {out[7:120]}"


def type_text(text: str):
    """Type into the focused window. Plain text is typed key by key; anything else (accents, other scripts, emoji)
    goes through the clipboard, which is put back afterwards."""
    if not text:
        return "Typed 0 characters."
    if all(31 < ord(c) < 127 or c == "\n" for c in text):
        lines = text.split("\n")
        script = ['tell application "System Events"']
        for i, line in enumerate(lines):
            if line:
                script.append(f"keystroke {q(line)}")
            if i < len(lines) - 1:
                script.append("key code 36")
        script.append("end tell")
        out = osa("\n".join(script), timeout=60)
    else:
        old = sh(["pbpaste"])
        sh(["pbcopy"], text_in=text)
        out = key("cmd+v")
        time.sleep(0.25)
        sh(["pbcopy"], text_in=old)
    if _denied(out):
        return PERMISSION.format(what="Accessibility")
    return f"Typed {len(text)} characters." if not out.startswith("error:") else f"I couldn't type that: {out[7:120]}"


def type_code_template(language: str, class_name: str = "Main"):
    from . import tools
    lang = tools._LANG.get(language.lower().strip())
    if not lang:
        return f"I have templates for Java, C++, C and Python, not {language}."
    cls = re.sub(r"\W", "", class_name or "") or "Main"
    lines, logic_at = tools.CODE_TEMPLATES[lang]
    lines = [ln.format(cls=cls) for ln in lines]
    time.sleep(0.3)
    out = key("cmd+a")
    if _denied(out):
        return PERMISSION.format(what="Accessibility")
    key("backspace")
    for i, line in enumerate(lines):
        if line:
            type_text(line + (" " if re.search(r"\w$", line) else ""))
        if i < len(lines) - 1:
            key("return")
        time.sleep(0.03)
    key("cmd+shift+down")                       # editors that close brackets themselves leave extra "}" lines: remove
    key("delete")
    key("cmd+up")                               # back to the top, then down to the empty logic line
    if logic_at:
        key("down", logic_at)
    key("cmd+right")
    name = {"java": "Java", "cpp": "C++", "c": "C", "python": "Python"}[lang]
    return f"{name} template typed. The cursor is on the line for your logic."


def scroll(direction: str, amount: str = "page"):
    d = direction.lower().strip()
    if d in ("top", "bottom"):
        out = key("cmd+up" if d == "top" else "cmd+down")
        return PERMISSION.format(what="Accessibility") if _denied(out) else f"Jumped to the {d}."
    notches = {"small": 3, "page": 8, "large": 20}.get(str(amount).lower(), 8)
    try:
        import Quartz                                  # a real scroll-wheel event, at the mouse pointer
        for _ in range(notches):
            ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, 3 if d == "up" else -3)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.015)
        return f"Scrolled {d}."
    except Exception:
        out = key("up" if d == "up" else "down", notches * 3) if notches < 8 else \
            key("pageup" if d == "up" else "pagedown", max(1, notches // 8))
        return PERMISSION.format(what="Accessibility") if _denied(out) else f"Scrolled {d}."


# ===================================================================== sound, screen, power
def _running(app: str) -> bool:
    return any((p.info["name"] or "").lower() == app.lower() for p in psutil.process_iter(["name"]))


def _media_key(code: int) -> bool:
    """The keyboard's own play / next / previous key (NX_KEYTYPE_PLAY 16, NEXT 17, PREVIOUS 18)."""
    try:
        import Quartz
        from AppKit import NSEvent
        for down in (True, False):
            ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                14, (0, 0), 0xA00 if down else 0xB00, 0, 0, 0, 8, (code << 16) | ((0xA if down else 0xB) << 8), -1)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev.CGEvent())
        return True
    except Exception as e:
        log.debug("media key failed: %s", e)
        return False


def media_control(action: str):
    a = action if action in ("next", "previous") else "play_pause"
    if _media_key({"next": 17, "previous": 18}.get(a, 16)):
        return f"Media {a.replace('_', ' ')}."
    verb = {"next": "next track", "previous": "previous track"}.get(a, "playpause")
    for app in ("Spotify", "Music"):
        if _running(app):
            osa(f'tell application "{app}" to {verb}')
            return f"Media {a.replace('_', ' ')}."
    return "I couldn't reach a media player, sir."


def volume(action: str, level: int = 50):
    if action == "mute":
        muted = osa("output muted of (get volume settings)") == "true"
        osa(f"set volume output muted {'false' if muted else 'true'}")
    elif action in ("up", "down"):
        cur = osa("output volume of (get volume settings)")
        cur = int(cur) if cur.isdigit() else 50
        osa(f"set volume output volume {max(0, min(100, cur + (10 if action == 'up' else -10)))}")
        if action == "up":
            osa("set volume output muted false")
    else:
        osa(f"set volume output volume {max(0, min(100, int(level)))}")
        osa("set volume output muted false")
    return f"Volume {action}{' ' + str(level) if action == 'set' else ''}."


def set_brightness(level: int):
    level = max(0, min(100, int(level)))
    try:                                               # the private DisplayServices call the brightness keys use
        import ctypes
        ds = ctypes.CDLL("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices")
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        ds.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
        if ds.DisplayServicesSetBrightness(cg.CGMainDisplayID(), level / 100.0) == 0:
            return f"Brightness set to {level} percent."
    except Exception as e:
        log.debug("DisplayServices brightness failed: %s", e)
    out = sh(["brightness", f"{level / 100:.2f}"]) if _which("brightness") else "error"
    return (f"Brightness set to {level} percent." if not out.startswith("error")
            else "I couldn't change the brightness on this Mac (external displays don't allow it).")


def _which(name: str) -> bool:
    import shutil
    return bool(shutil.which(name))


def lock_screen():
    lock()
    return "Screen locked."


def power(action: str):
    cmds = {"shutdown": 'tell application "System Events" to shut down',
            "restart": 'tell application "System Events" to restart',
            "sleep": 'tell application "System Events" to sleep',
            "signout": 'tell application "System Events" to log out'}
    if action not in cmds:
        return "Unknown power action."
    out = osa(cmds[action])
    return f"{action.capitalize()} in progress." if not out.startswith("error:") else f"I couldn't do that: {out[7:120]}"


def capture_screen(out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    sh(["screencapture", "-x", str(out)], timeout=20)


def screenshot():
    out = HOME / "Pictures" / "Jarvis" / f"screenshot_{dt.datetime.now():%Y%m%d_%H%M%S}.png"
    capture_screen(out)
    return f"Screenshot saved to {out}." if out.exists() else PERMISSION.format(what="Screen Recording")


def ocr_image(path: Path) -> str:
    """Offline text recognition with Apple's Vision framework."""
    try:
        import Vision
        from Foundation import NSURL
    except Exception:
        return ""
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(path)), None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(0)                    # accurate
    request.setUsesLanguageCorrection_(True)
    ok = handler.performRequests_error_([request], None)
    if isinstance(ok, tuple):
        ok = ok[0]
    lines = []
    for obs in (request.results() or []) if ok else []:
        best = obs.topCandidates_(1)
        if best and len(best):
            lines.append(str(best[0].string()))
    return "\n".join(lines)


def clipboard(action: str, text: str = ""):
    if action == "write":
        sh(["pbcopy"], text_in=text)
        return "Copied to the clipboard."
    out = sh(["pbpaste"])
    return ("Clipboard: " + out[:1500]) if out else "The clipboard is empty."


def empty_recycle_bin():
    out = osa('tell application "Finder" to empty trash', timeout=60)
    return "Trash emptied." if not out.startswith("error:") or "empty" in out.lower() else f"I couldn't empty the Trash: {out[7:120]}"


def delete_path(path: str):
    from . import tools
    p = tools._user_path(path)
    if not p.exists():
        return f"{p} does not exist."
    out = osa(f'tell application "Finder" to delete (POSIX file {q(str(p))} as alias)')
    if _denied(out):
        return PERMISSION.format(what="Automation (Finder)")
    return f"Moved {p} to the Trash." if not p.exists() else f"Could not delete {p}."


def open_path(path: str):
    from . import tools
    p = tools._user_path(path)
    if not p.exists():
        name = path.lower().strip()
        if not re.search(r"[\\/:.]", path) and (name in tools.ALIASES or tools.known_app(name)):
            return open_app(path)
        return f"{p} does not exist."
    open_file(p)
    return f"Opened {p}."


def run_command(command: str):
    """The 'run a command' tool: a zsh command on a Mac (still asks for confirmation first, like on Windows)."""
    return sh(["/bin/zsh", "-lc", command], timeout=60)[:1500] or "Done (no output)."


def window_control(action: str):
    a = action.lower().strip()
    front = 'tell application "System Events" to tell (first application process whose frontmost is true)'
    if a in ("show_desktop", "minimize_all"):
        out = key("f11")
    elif a == "minimize":
        out = key("cmd+m")
    elif a == "maximize":
        out = osa(f'{front} to set value of attribute "AXFullScreen" of front window to true')
    elif a == "restore":
        out = osa(f'{front} to set value of attribute "AXFullScreen" of front window to false')
    elif a == "switch":
        out = key("cmd+tab")
    elif a in ("snap_left", "snap_right"):
        # Tile to half the screen by moving and resizing the front window (works on every macOS version).
        side = "0" if a == "snap_left" else "(w div 2)"
        out = osa('tell application "Finder" to set {x0, y0, w, h} to bounds of window of desktop\n'
                  'tell application "System Events"\ntell (first application process whose frontmost is true)\n'
                  f'set position of front window to {{{side}, 25}}\n'
                  'set size of front window to {w div 2, h - 25}\nend tell\nend tell')
    elif a == "close":
        out = key("cmd+w")
    else:
        return f"Unknown window action {action}."
    if _denied(out):
        return PERMISSION.format(what="Accessibility")
    return f"Window {a.replace('_', ' ')} done." if not out.startswith("error:") else f"I couldn't do that: {out[7:120]}"


# ===================================================================== apps
ALIASES = {"browser": "safari", "the browser": "safari", "chrome": "google chrome", "chrome browser": "google chrome",
           "crow": "google chrome", "curl": "google chrome", "crome": "google chrome", "krom": "google chrome",
           "chrom": "google chrome", "crown": "google chrome", "vs code": "visual studio code",
           "vscode": "visual studio code", "code": "visual studio code", "file explorer": "finder", "files": "finder",
           "explorer": "finder", "file manager": "finder", "command prompt": "terminal", "cmd": "terminal",
           "settings": "system settings", "preferences": "system settings", "system preferences": "system settings",
           "task manager": "activity monitor", "notepad": "textedit", "text editor": "textedit", "paint": "preview",
           "edge": "microsoft edge", "word": "microsoft word", "ms word": "microsoft word", "excel": "microsoft excel",
           "powerpoint": "microsoft powerpoint", "camera": "photo booth", "gallery": "photos", "my photos": "photos",
           "pictures app": "photos", "store": "app store", "snipping tool": "screenshot", "what's app": "whatsapp",
           "whats app": "whatsapp", "itunes": "music", "apple music": "music", "mail app": "mail", "imessage": "messages"}
PROTECTED = {"finder", "dock", "windowserver", "loginwindow", "systemuiserver", "launchd", "kernel_task", "controlcenter",
             "notificationcenter", "coreaudiod", "ollama", "spotlight", "airplayuiagent", "terminal"}
_APP_DIRS = ["/Applications", "/System/Applications", "/System/Applications/Utilities", "/Applications/Utilities",
             str(HOME / "Applications"), "/System/Library/CoreServices", "/System/Library/CoreServices/Applications"]


def refresh_apps(force: bool = False) -> dict[str, str]:
    """Every installed app (lower-case name -> path of its .app). Reading a few folders takes milliseconds."""
    from . import tools
    apps = {}
    for folder in _APP_DIRS:
        try:
            for entry in os.scandir(folder):
                if entry.name.endswith(".app"):
                    apps.setdefault(entry.name[:-4].lower(), entry.path)
                elif entry.is_dir() and not entry.name.startswith(".") and folder != "/System/Library/CoreServices":
                    try:
                        for sub in os.scandir(entry.path):         # e.g. /Applications/Microsoft Office/…
                            if sub.name.endswith(".app"):
                                apps.setdefault(sub.name[:-4].lower(), sub.path)
                    except OSError:
                        pass
        except OSError:
            continue
    if apps:
        tools._APPS.clear()
        tools._APPS.update(apps)
        tools._apps_loaded = time.time()
        try:
            tools._APPS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tools._APPS_FILE.write_text(json.dumps(apps, indent=0), encoding="utf-8")
        except OSError:
            pass
    return tools._APPS


def open_app(name: str):
    from . import tools
    said = name.lower().strip()
    target = ALIASES.get(said, said)
    if not tools._APPS:
        refresh_apps()
    app = tools.known_app(target)
    if app:
        subprocess.Popen(["open", "-a", tools._APPS[app]])
        shown = name if (app == target or target in app) else app.title()
        tools.Ctx.last_opened = ("app", app)
        return f"Opened {shown}."
    out = sh(["open", "-a", target])                   # not in the usual folders: let Launch Services look
    if not out:
        tools.Ctx.last_opened = ("app", target)
        return f"Opened {name}."
    return f"I couldn't find an app called {name}."


def _running_apps() -> list[str]:
    out = osa('tell application "System Events" to get name of every application process whose background only is false')
    return [] if out.startswith("error:") else [a.strip() for a in out.split(",") if a.strip()]


def close_app(name: str):
    """Quit an app the polite way (it asks about unsaved work itself), like Cmd+Q."""
    from . import tools
    said = re.sub(r"^(the|my)\s+|\s+(app|application|program|window|windows)$", "", name.lower().strip())
    target = ALIASES.get(said, said)
    if target == "finder":
        osa('tell application "Finder" to close every window')
        return "Closed the Finder windows."
    running = _running_apps()
    match = next((a for a in running if a.lower() == target), None) or \
        next((a for a in running if target in a.lower() or a.lower() in target), None)
    if not match or match.lower() in PROTECTED:
        return f"No {name} windows are open."
    out = osa(f"tell application {q(match)} to quit")
    if _denied(out):
        return PERMISSION.format(what="Automation")
    if tools.Ctx.last_opened and tools.Ctx.last_opened[0] == "app" and said in str(tools.Ctx.last_opened[1]).lower():
        tools.Ctx.last_opened = None
    return f"Closed {match}."


def kill_process(target: str):
    n, me = 0, os.getpid()
    target = ALIASES.get(str(target).lower().strip(), str(target).lower().strip())
    for p in psutil.process_iter(["name", "pid"]):
        pname = (p.info["name"] or "").lower()
        if p.info["pid"] == me or pname in PROTECTED:
            continue
        if str(p.info["pid"]) == target or pname == target or pname == target.replace(" ", ""):
            try:
                p.kill()
                n += 1
            except Exception:
                pass
    return f"Killed {n} process(es)." if n else f"No process {target}."


# ===================================================================== system information
def wifi_text() -> str:
    try:
        ports = sh(["networksetup", "-listallhardwareports"])
        m = re.search(r"Hardware Port: (?:Wi-Fi|AirPort)\s*\nDevice: (\w+)", ports)
        dev = m.group(1) if m else "en0"
        out = sh(["networksetup", "-getairportnetwork", dev])
        m = re.search(r"Current Wi-Fi Network: (.+)", out)
        if m:
            return f"Wi-Fi connected to {m.group(1).strip()}"
        out = sh(["ipconfig", "getsummary", dev])
        m = re.search(r"^\s*SSID : (.+)$", out, re.M)
        if m and "redacted" not in m.group(1).lower():
            return f"Wi-Fi connected to {m.group(1).strip()}"
        if re.search(r"^\s*LinkStatusActive : TRUE", out, re.M) or "yiaddr" in out:
            return "Wi-Fi is connected (macOS keeps the network name private from apps)"
    except Exception:
        pass
    up = [n for n, s in psutil.net_if_stats().items() if s.isup and not n.startswith(("lo", "utun", "awdl", "llw", "gif", "stf", "anpi", "ap", "bridge"))]
    return f"Network adapters up: {', '.join(up[:3])}" if up else "No network connection"


def gpu() -> dict | None:
    return None                                        # Apple silicon has no separate video card to report on


def security_audit() -> str:
    """Firewall, disk encryption, Gatekeeper, System Integrity Protection, remote login, listening ports, updates."""
    good, bad = [], []
    fw = sh(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    (good if re.search(r"enabled|State = [12]", fw) else bad).append(
        "the firewall is on" if re.search(r"enabled|State = [12]", fw) else "the firewall is OFF")
    fv = sh(["fdesetup", "status"])
    (good if "On" in fv else bad).append("FileVault disk encryption is on" if "On" in fv else "FileVault disk encryption is OFF")
    gk = sh(["spctl", "--status"])
    (good if "enabled" in gk else bad).append("Gatekeeper is on" if "enabled" in gk else "Gatekeeper is OFF")
    sip = sh(["csrutil", "status"])
    (good if "enabled" in sip else bad).append(
        "System Integrity Protection is on" if "enabled" in sip else "System Integrity Protection is OFF")
    ssh = sh(["launchctl", "print", "system/com.openssh.sshd"])
    if "state = " in ssh and "could not find" not in ssh.lower():
        bad.append("Remote Login (SSH) is switched on")
    ports = set()
    for p in psutil.process_iter(["pid"]):
        try:
            ports |= {c.laddr.port for c in p.net_connections(kind="inet")
                      if c.status == psutil.CONN_LISTEN and c.laddr.ip not in ("127.0.0.1", "::1")}
        except Exception:
            continue
    if ports:
        bad.append(f"{len(ports)} port(s) are listening on the network: {', '.join(map(str, sorted(ports)[:8]))}")
    upd = sh(["defaults", "read", "/Library/Preferences/com.apple.SoftwareUpdate", "AutomaticCheckEnabled"])
    if upd.strip() == "0":
        bad.append("automatic update checks are switched off")
    text = "Audit complete. "
    text += ("Worth a look: " + "; ".join(bad) + ". ") if bad else "Nothing worrying. "
    return text + ("Good: " + ", ".join(good) + "." if good else "")


def security_sweep() -> str:
    """The Mac sweep: unsigned programs running from Downloads / temp folders, backdoor ports, core protections."""
    from . import tools
    cfg = tools.Ctx.cfg.get("security", {})
    bad_ports = set(cfg.get("network_watch", {}).get("suspicious_ports", []))
    findings, risky, seen, hits, listening = [], [], set(), set(), set()
    for p in psutil.process_iter(["name", "exe", "pid"]):
        exe = p.info["exe"] or ""
        low = exe.lower()
        if exe and low not in seen and re.search(r"/downloads/|/tmp/|/private/var/folders/|/users/shared/", low):
            seen.add(low)
            if not signer(exe):
                risky.append(p.info["name"])
        try:
            for c in p.net_connections(kind="inet"):
                if c.status == psutil.CONN_LISTEN:
                    listening.add(c.laddr.port)
                elif c.raddr and c.raddr.port in bad_ports and c.raddr.ip not in ("127.0.0.1", "::1"):
                    hits.add(c.raddr.port)
        except Exception:
            continue
    if risky:
        findings.append(f"{len(risky)} unsigned program(s) running from Downloads or temp folders: {', '.join(risky[:4])}")
    if hits:
        findings.append(f"connections to suspicious ports {sorted(hits)}")
    if "enabled" not in sh(["spctl", "--status"]):
        findings.append("Gatekeeper is switched off")
    who = ""
    sec = tools.Ctx.security
    if sec and sec.face:
        v = sec.owner_verified()
        who = {True: " Owner identity verified by camera.", False: " Warning: the person at the keyboard is not verified.",
               None: ""}[v]
        if v is False:
            findings.append("an unverified person is at the keyboard")
    agents = sum(len(list(d.glob("*.plist"))) for d in (HOME / "Library/LaunchAgents", Path("/Library/LaunchAgents"),
                                                        Path("/Library/LaunchDaemons")) if d.is_dir())
    base = (f"Sweep complete: {len(psutil.pids())} processes, {len(listening)} listening ports and {agents} startup "
            f"items checked.{who}")
    return base + (" Findings: " + "; ".join(findings) + "." if findings else " No threats detected.")


# ===================================================================== wiring
def install():
    """Swap the Windows implementations in tools.py for the ones above. Called once, when tools.py is imported."""
    from . import tools
    replace = {
        "open_app": open_app, "close_app": close_app, "media_control": media_control, "volume": volume,
        "set_brightness": set_brightness, "lock_screen": lock_screen, "power": power, "screenshot": screenshot,
        "window_control": window_control, "scroll": scroll, "press_keys": press_keys, "type_text": type_text,
        "type_code_template": type_code_template, "clipboard": clipboard, "empty_recycle_bin": empty_recycle_bin,
        "delete_path": delete_path, "open_path": open_path, "run_command": run_command, "kill_process": kill_process,
        "security_audit": security_audit, "security_sweep": security_sweep,
    }
    for name, fn in replace.items():
        setattr(tools, name, fn)
        if name in tools.REGISTRY:
            tools.REGISTRY[name].func = fn
    # helpers other code reaches for by name
    tools._capture_screen = capture_screen
    tools.ocr_image = ocr_image
    tools._wifi_text = wifi_text
    tools._gpu = gpu
    tools.refresh_apps = refresh_apps
    tools._chrome_exe = chrome_exe
    tools._find_shortcut = lambda name: None
    tools._ps = lambda cmd, timeout=30: sh(["/bin/zsh", "-lc", cmd], timeout=timeout)
    # the same dictionaries the reflex router consults, with Mac contents
    tools.ALIASES.clear()
    tools.ALIASES.update(ALIASES)
    tools.BUILTINS.clear()
    tools._NEEDLES.clear()
    tools.PROTECTED.clear()
    tools.PROTECTED.update(PROTECTED)
    # wording: descriptions the language model reads
    for name, text in (("run_command", "Run a zsh shell command and return its output. Use only when no other tool fits."),
                       ("empty_recycle_bin", "Empty the macOS Trash permanently."),
                       ("delete_path", "Delete a file or folder (moves it to the Trash)."),
                       ("screenshot", "Take a screenshot and save it to Pictures/Jarvis."),
                       ("security_audit", "Audit THIS Mac's security: firewall, FileVault, Gatekeeper, SIP, remote "
                                          "login, exposed listening ports. For 'audit this laptop', 'how exposed am I'."),
                       ("open_app", "Open / launch an application by name (e.g. chrome, spotify, finder, notes, settings).")):
        if name in tools.REGISTRY:
            tools.REGISTRY[name].description = text
