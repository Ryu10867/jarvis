"""The HUD: an Iron-Man style heads-up display. An animated arc reactor that reacts to Jarvis's voice and your
microphone, a live transcript, system gauges, the security line, and a box to type commands.
Click the reactor (or press the push-to-talk hotkey) to talk without the wake word."""
import ctypes
import math
import random
import time
import tkinter as tk

import psutil

from .state import STATE

BG = "#04070c"
PANEL = "#0a1119"
CYAN = "#22d3ff"
AMBER = "#ffb428"
RED = "#ff4338"
WHITE = "#e8fbff"
DIM = "#4b6b7a"

LABELS = {"booting": "INITIALISING", "idle": "STANDING BY", "listening": "LISTENING", "thinking": "PROCESSING",
          "speaking": "SPEAKING", "alert": "SECURITY ALERT"}


def mix(color: str, alpha: float, base: str = BG) -> str:
    """Blend `color` over `base` (Tk has no transparency, so glow is faked by blending)."""
    alpha = max(0.0, min(1.0, alpha))
    c = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(base[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(b[i] + (c[i] - b[i]) * alpha) for i in range(3))


class Hud:
    def __init__(self, cfg: dict, on_text, on_talk, on_quit, security=None):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        ui = cfg.get("ui", {})
        self.on_text, self.on_talk, self.on_quit = on_text, on_talk, on_quit
        self.security = security
        self.hotkey = cfg.get("voice", {}).get("push_to_talk", "ctrl+alt+j")
        self.root = tk.Tk()
        self.s = s = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        self.root.title("J.A.R.V.I.S.")
        self.root.configure(bg=BG)
        w, h = int(430 * s), int(700 * s)
        x = self.root.winfo_screenwidth() - w - int(24 * s)
        y = self.root.winfo_screenheight() - h - int(90 * s)
        self.root.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        self.root.minsize(int(360 * s), int(560 * s))
        self.root.attributes("-topmost", bool(ui.get("always_on_top", False)))
        try:
            self.root.attributes("-alpha", float(ui.get("opacity", 0.97)))
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        mono = ("Consolas", 9)

        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(top, text="J.A.R.V.I.S.", fg=CYAN, bg=BG, font=("Segoe UI Semibold", 13)).pack(side="left")
        self.clock = tk.Label(top, text="", fg=DIM, bg=BG, font=mono)
        self.clock.pack(side="right")

        self.cv = tk.Canvas(self.root, bg=BG, highlightthickness=0, height=int(300 * s), cursor="hand2")
        self.cv.pack(fill="x")
        self.cv.bind("<Button-1>", lambda e: self.on_talk())

        self.state_lbl = tk.Label(self.root, text="INITIALISING", fg=CYAN, bg=BG, font=("Segoe UI Semibold", 11))
        self.state_lbl.pack()
        self.hint = tk.Label(self.root, text=f'say "Jarvis"  ·  click the reactor  ·  {self.hotkey.upper()}',
                             fg=DIM, bg=BG, font=("Segoe UI", 8))
        self.hint.pack(pady=(0, 6))

        gauges = tk.Frame(self.root, bg=BG)
        gauges.pack(fill="x", padx=14)
        self.gauges = {}
        for i, name in enumerate(("CPU", "RAM", "PWR")):
            f = tk.Frame(gauges, bg=BG)
            f.grid(row=0, column=i, sticky="ew", padx=3)
            gauges.columnconfigure(i, weight=1)
            lbl = tk.Label(f, text=name, fg=DIM, bg=BG, font=mono, anchor="w")
            lbl.pack(fill="x")
            bar = tk.Canvas(f, bg=PANEL, height=int(5 * s), highlightthickness=0)
            bar.pack(fill="x")
            self.gauges[name] = (lbl, bar)

        self.sec = tk.Label(self.root, text="", fg=DIM, bg=BG, font=mono, anchor="w", justify="left")
        self.sec.pack(fill="x", padx=16, pady=(8, 2))

        entry_frame = tk.Frame(self.root, bg=PANEL, highlightbackground=mix(CYAN, 0.35), highlightthickness=1)
        entry_frame.pack(side="bottom", fill="x", padx=14, pady=(4, 12))
        tk.Label(entry_frame, text="›", fg=CYAN, bg=PANEL, font=("Consolas", 12)).pack(side="left", padx=(8, 2))
        self.entry = tk.Entry(entry_frame, bg=PANEL, fg=WHITE, insertbackground=CYAN, relief="flat",
                              font=("Segoe UI", 10))
        self.entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        self.entry.bind("<Return>", self._submit)

        self.log = tk.Text(self.root, bg=BG, fg=WHITE, relief="flat", wrap="word", font=("Segoe UI", 10),
                           highlightthickness=0, padx=14, pady=4, height=8, cursor="arrow", spacing3=5)
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("you", foreground="#9fb6c2")
        self.log.tag_configure("jarvis", foreground=CYAN)
        self.log.tag_configure("sys", foreground=DIM, font=mono)
        self.log.tag_configure("alert", foreground=RED)
        self.log.configure(state="disabled")

        self.t0 = time.time()
        self.level = 0.0
        self.spin = [0.0, 0.0, 0.0]
        self.bars = [0.0] * 56
        self.seen_version = -1
        self.last_stats = 0.0
        self.closed = False
        psutil.cpu_percent(None)
        self.root.after(30, self._frame)

    # ---- events
    def _submit(self, _):
        text = self.entry.get().strip()
        self.entry.delete(0, "end")
        if text:
            self.on_text(text)

    def _quit(self):
        """Window closed by the user: let Jarvis say goodbye, then go (forced after a few seconds)."""
        if not self.closed:
            self.closed = True
            self.on_quit()
            self.root.after(6000, self._destroy)

    def _destroy(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def close(self):
        """Thread-safe request to close the window."""
        self.closed = True
        try:
            self.root.after(0, self._destroy)
        except Exception:
            pass

    def run(self):
        self.root.mainloop()

    # ---- drawing
    def _frame(self):
        try:
            self._draw()
        except tk.TclError:
            return
        self.root.after(33, self._frame)

    def _draw(self):
        cv, s = self.cv, self.s
        mode = "alert" if STATE.alerting else STATE.mode
        color = {"thinking": AMBER, "alert": RED}.get(mode, CYAN)
        target = STATE.voice_level if mode == "speaking" else (STATE.mic_level if mode in ("listening", "idle") else 0.0)
        if mode == "idle":
            target *= 0.35
        self.level += (target - self.level) * (0.5 if target > self.level else 0.18)
        lvl = self.level
        t = time.time() - self.t0
        speed = {"thinking": 3.2, "listening": 1.5, "speaking": 1.2, "alert": 4.0, "booting": 2.2}.get(mode, 0.45)
        energy = {"idle": 0.55, "booting": 0.7}.get(mode, 1.0)
        self.spin[0] += 1.1 * speed
        self.spin[1] -= 0.8 * speed
        self.spin[2] += 0.35 * speed

        W, H = cv.winfo_width(), cv.winfo_height()
        cx, cy = W / 2, H / 2
        R = min(W, H) * 0.40
        cv.delete("all")

        def ring(r, **kw):
            cv.create_oval(cx - r, cy - r, cx + r, cy + r, **kw)

        def arc(r, start, extent, width, col):
            cv.create_arc(cx - r, cy - r, cx + r, cy + r, start=start % 360, extent=extent, style="arc",
                          width=width, outline=col)

        # audio spectrum halo
        active = mode in ("speaking", "listening")
        n = len(self.bars)
        for i in range(n):
            goal = lvl * (0.35 + 0.65 * abs(math.sin(i * 0.9 + t * 7))) * random.uniform(0.6, 1.0) if active else 0.0
            self.bars[i] += (goal - self.bars[i]) * 0.45
            a = 2 * math.pi * i / n
            r1 = R * 1.06
            r2 = r1 + 2 * s + self.bars[i] * R * 0.34
            cv.create_line(cx + r1 * math.cos(a), cy + r1 * math.sin(a), cx + r2 * math.cos(a), cy + r2 * math.sin(a),
                           fill=mix(color, 0.25 + 0.75 * min(1, self.bars[i] * 3)), width=max(1, 2 * s))

        # outer rings
        ring(R, outline=mix(color, 0.30 * energy), width=1)
        for k in range(3):
            arc(R * 0.93, self.spin[0] + k * 120, 78, 4 * s, mix(color, 0.85 * energy))
        for k in range(2):
            arc(R * 0.82, self.spin[1] + k * 180, 110, 2 * s, mix(color, 0.55 * energy))
        for k in range(24):                                     # tick marks
            a = math.radians(self.spin[2] + k * 15)
            r1, r2 = R * 0.70, R * (0.75 if k % 2 else 0.78)
            cv.create_line(cx + r1 * math.cos(a), cy + r1 * math.sin(a), cx + r2 * math.cos(a), cy + r2 * math.sin(a),
                           fill=mix(color, 0.5 * energy), width=1)
        # reactor coils
        pulse = 0.5 + 0.5 * math.sin(t * (6 if mode == "thinking" else 2))
        for k in range(10):
            lit = 0.45 + 0.4 * pulse * energy + 0.3 * lvl
            if mode in ("thinking", "booting"):
                lit = 0.3 + 0.7 * (((k - t * 9) % 10) < 3)
            arc(R * 0.58, k * 36 + 4 - self.spin[2] * 0.5, 28, 11 * s, mix(color, lit * energy))
        ring(R * 0.47, outline=mix(color, 0.5 * energy), width=1)
        # glowing core
        core = R * (0.26 + 0.16 * lvl + 0.02 * pulse)
        for i in range(7, 0, -1):
            rr = core * (1 + i * 0.13)
            ring(rr, fill=mix(color, (0.10 + 0.5 * lvl) * (1 - i / 8) * energy + 0.03), outline="")
        ring(core, fill=mix(color, 0.55 * energy + 0.3 * lvl), outline=mix(WHITE, 0.6 * energy), width=1)
        ring(core * 0.55, fill=mix(WHITE, 0.55 * energy + 0.4 * lvl, mix(color, 0.8)), outline="")

        self.state_lbl.configure(text=LABELS.get(mode, mode.upper()), fg=color)

        if STATE.version != self.seen_version:
            self._refresh_log()
        if time.time() - self.last_stats > 1.0:
            self._refresh_stats()

    def _refresh_log(self):
        self.seen_version = STATE.version
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        with STATE._lock:
            boot, items = list(STATE.boot_lines), list(STATE.log)
        if len(items) < 4:
            for line in boot[-9:]:
                self.log.insert("end", f"▸ {line}\n", "sys")
        for who, text, _ in items[-30:]:
            tag = {"YOU": "you", "JARVIS": "jarvis", "ALERT": "alert"}.get(who, "sys")
            prefix = {"YOU": "You   ", "JARVIS": "Jarvis  ", "ALERT": "⚠  "}.get(who, "▸ ")
            self.log.insert("end", f"{prefix}{text}\n", tag)
        self.log.configure(state="disabled")
        self.log.see("end")

    def _gauge(self, name: str, pct: float, text: str):
        lbl, bar = self.gauges[name]
        lbl.configure(text=f"{name} {text}")
        bar.delete("all")
        w = bar.winfo_width()
        col = RED if (pct > 90 and name != "PWR") or (pct < 15 and name == "PWR") else CYAN
        bar.create_rectangle(0, 0, w * pct / 100, 20, fill=mix(col, 0.85), outline="")

    def _refresh_stats(self):
        self.last_stats = time.time()
        self.clock.configure(text=time.strftime("%a %d %b  %H:%M:%S"))
        cpu, ram = psutil.cpu_percent(None), psutil.virtual_memory().percent
        self._gauge("CPU", cpu, f"{cpu:.0f}%")
        self._gauge("RAM", ram, f"{ram:.0f}%")
        bat = psutil.sensors_battery()
        if bat:
            self._gauge("PWR", bat.percent, f"{bat.percent:.0f}%{'+' if bat.power_plugged else ''}")
        else:
            self._gauge("PWR", 100, "AC")
        if self.security:
            mons = self.security.monitors
            face = getattr(self.security.face, "status", "off") if self.security.face else "off"
            self.sec.configure(text=f"SECURITY  {len(mons)} monitors active  ·  face guard: {face}",
                               fg=RED if STATE.alerting else DIM)
        else:
            self.sec.configure(text="SECURITY  monitors off")
