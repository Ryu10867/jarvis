"""Global push-to-talk hotkey (default Ctrl+Alt+J): works from any application, no wake word needed."""
import ctypes
import logging
import threading
from ctypes import wintypes

log = logging.getLogger("jarvis.hotkey")
MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
KEYS = {"space": 0x20, "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
        "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B}


def start_hotkey(combo: str, callback) -> bool:
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        return False
    mods = 0x4000                                   # MOD_NOREPEAT
    for p in parts[:-1]:
        mods |= MODS.get(p, 0)
    key = parts[-1]
    vk = KEYS.get(key) or (ord(key.upper()) if len(key) == 1 else 0)
    if not vk:
        log.warning("Unknown hotkey %r", combo)
        return False

    def loop():
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, mods, vk):
            log.warning("Hotkey %s is already taken by another program", combo)
            return
        log.info("Push-to-talk hotkey registered: %s", combo)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:               # WM_HOTKEY
                try:
                    callback()
                except Exception:
                    log.exception("hotkey callback failed")

    threading.Thread(target=loop, daemon=True, name="hotkey").start()
    return True
