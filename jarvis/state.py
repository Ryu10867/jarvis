"""Shared runtime state, read by the HUD (and anything else that wants to show what Jarvis is doing)."""
import collections
import threading
import time


class State:
    def __init__(self):
        self.booting = True
        self.speaking = False        # Jarvis is talking
        self.thinking = False        # waiting on the language model / a tool
        self.attentive = False       # awake and waiting for a command (after the wake word / during a conversation)
        self.mic_level = 0.0         # 0..1 recent microphone loudness
        self.voice_level = 0.0       # 0..1 loudness of what Jarvis is saying right now
        self.alert_until = 0.0
        self.boot_lines: list[str] = []
        self.log = collections.deque(maxlen=60)   # (who, text, time)
        self._lock = threading.Lock()
        self.version = 0             # bumps whenever log/boot_lines change so the HUD only redraws when needed
        self.live = ("", "", 0.0)    # (who, text, when): the line to caption under the orb right now
        self.ui_requests = collections.deque(maxlen=20)   # (action, value) asks for the orb: show/hide/theme/size...

    @property
    def mode(self) -> str:
        if self.speaking:
            return "speaking"
        if self.thinking:
            return "thinking"
        if self.booting:
            return "booting"
        return "listening" if self.attentive else "idle"

    def add(self, who: str, text: str):
        with self._lock:
            self.log.append((who, text, time.strftime("%H:%M")))
            self.version += 1
        if who != "JARVIS":          # his own lines are captioned as they are actually spoken (see Speaker)
            self.caption(who, text)

    def boot(self, line: str):
        with self._lock:
            self.boot_lines.append(line)
            self.version += 1
        self.caption("BOOT", line)
        print(f"  > {line}", flush=True)

    def caption(self, who: str, text: str):
        self.live = (who, text, time.time())

    def ui(self, action: str, value=None):
        """Ask the orb to do something (safe to call from any thread)."""
        self.ui_requests.append((action, value))

    def flash_alert(self, seconds: float = 6):
        self.alert_until = time.time() + seconds

    @property
    def alerting(self) -> bool:
        return time.time() < self.alert_until


STATE = State()
