"""The orb: Jarvis as a floating 3D sphere on the desktop instead of a window.

A frameless, always-on-top, see-through window (per-pixel alpha) showing a turning holographic sphere: three nested
node lattices, a glowing core and orbital rings. It shows what Jarvis is doing:

    standing by   slow turn, dim                 listening   brighter, voice ticks follow your microphone
    thinking      fast spin, scan ring, sparks   speaking    core and surface pulse with his voice
    alert         red                            starting    the sphere assembles itself

Click it to talk, drag it anywhere, double-click for the console (transcript + a box to type commands), right-click
for the menu. Captions under the orb show what was heard and what is being said.
Try it on its own with:  python -m jarvis.orb"""
import ctypes
import json
import logging
import math
import time
import tkinter as tk
from ctypes import wintypes

import cv2
import numpy as np
import psutil

from .config import resolve
from .hud import BG, DIM, LABELS, PANEL, RED, WHITE, mix
from .state import STATE

log = logging.getLogger("jarvis.orb")
F32 = np.float32
CAM = 6.0          # camera distance in sphere radii: a mild perspective
NB = 6             # brightness steps used when batching lines into few draw calls
FP = 3             # fixed-point bits for sub-pixel line drawing
SPHERE = 0.29      # sphere radius as a fraction of the window size (the rest is rings and glow)

# Colours are (blue, green, red): what Windows bitmaps and OpenCV both use.  theme = (base, white-hot, thinking)
THEMES = {
    "gold": ((40, 170, 255), (205, 240, 255), (20, 105, 255)),
    "arc": ((255, 190, 45), (255, 246, 222), (40, 180, 255)),
    "emerald": ((150, 255, 40), (225, 255, 235), (255, 220, 40)),
    "violet": ((255, 110, 170), (255, 232, 240), (200, 90, 255)),
    "crimson": ((60, 70, 255), (215, 225, 255), (40, 150, 255)),
    "white": ((236, 240, 248), (255, 255, 255), (255, 232, 205)),
}
ALERT = np.array((40, 45, 255), F32)
COLOUR_WORDS = {"gold": "gold", "golden": "gold", "yellow": "gold", "orange": "gold", "amber": "gold",
                "blue": "arc", "cyan": "arc", "arc": "arc", "ice": "arc",
                "green": "emerald", "emerald": "emerald",
                "purple": "violet", "violet": "violet", "pink": "violet",
                "red": "crimson", "crimson": "crimson",
                "white": "white", "silver": "white", "pearl": "white", "ice white": "white"}

#               spin  energy scan  sparks voice-ticks
MODES = {"idle": (0.30, 0.62, 0.0, 0.10, 0.0),
         "listening": (0.75, 1.00, 0.0, 0.30, 1.0),
         "thinking": (2.30, 0.92, 1.0, 1.00, 0.0),
         "speaking": (0.85, 1.00, 0.0, 0.35, 1.0),
         "booting": (1.50, 0.85, 1.0, 0.70, 0.0),
         "alert": (2.80, 1.00, 1.0, 1.00, 0.0)}


# ------------------------------------------------------------------ geometry
def _rx(a):
    c, s = math.cos(a), math.sin(a)
    return np.array(((1, 0, 0), (0, c, -s), (0, s, c)), F32)


def _ry(a):
    c, s = math.cos(a), math.sin(a)
    return np.array(((c, 0, s), (0, 1, 0), (-s, 0, c)), F32)


def _rz(a):
    c, s = math.cos(a), math.sin(a)
    return np.array(((c, -s, 0), (s, c, 0), (0, 0, 1)), F32)


def _lattice(n, rng, jitter):
    """n points spread evenly over a unit sphere, then nudged so it looks grown rather than printed."""
    i = np.arange(n) + 0.5
    phi = np.arccos(1 - 2 * i / n)
    th = np.pi * (1 + 5 ** 0.5) * i
    p = np.stack([np.cos(th) * np.sin(phi), np.cos(phi), np.sin(th) * np.sin(phi)], 1)
    p += rng.normal(0, jitter, p.shape)
    return (p / np.linalg.norm(p, axis=1, keepdims=True)).astype(F32)


def _links(p, k):
    """Join every point to its k nearest neighbours."""
    d = p @ p.T
    np.fill_diagonal(d, -2)
    near = np.argsort(-d, axis=1)[:, :k]
    pairs = {(min(i, int(j)), max(i, int(j))) for i in range(len(p)) for j in near[i]}
    return np.array(sorted(pairs), np.int32)


class _Shell:
    """One layer of the sphere: nodes, the links between them and how it turns."""

    def __init__(self, rng, n, k, radius, jitter, tilt, rate, node, line, wave):
        self.p = _lattice(n, rng, jitter)
        self.e = _links(self.p, k)
        self.radius, self.rate, self.node, self.line, self.wave = radius, rate, node, line, wave
        self.tilt = _rz(tilt[1]) @ _rx(tilt[0])
        self.yaw = float(rng.uniform(0, 6.28))
        self.phase = rng.uniform(0, 6.28, len(self.e)).astype(F32)       # each link sparks on its own beat
        hub = rng.random(n) < 0.13
        self.size = np.where(hub, 1.9, rng.uniform(0.7, 1.15, n)).astype(F32)
        self.glow = np.where(hub, 1.25, 1.0).astype(F32)
        self.delay = rng.random(n).astype(F32)                           # arrival order while assembling
        d = rng.normal(0, 1, (n, 3))
        self.scatter = (d / np.linalg.norm(d, axis=1, keepdims=True) * rng.uniform(2.2, 3.6, (n, 1))).astype(F32)


class _Ring:
    def __init__(self, radius, tx, tz, speed, pattern, sats, gain):
        self.radius, self.tx, self.tz, self.speed, self.pattern, self.sats, self.gain = radius, tx, tz, speed, pattern, sats, gain
        self.phase = 0.0


class OrbRenderer:
    """Draws one frame of the sphere into a premultiplied BGRA image. Knows nothing about windows."""

    def __init__(self, size: int, theme: str = "gold", glass: float = 1.0):
        rng = np.random.default_rng(11)
        self.rng = np.random.default_rng(5)
        self.glass = float(max(0.0, min(1.0, glass)))
        self.shells = [
            _Shell(rng, 150, 3, 1.00, 0.050, (0.42, 0.20), 1.0, 0.021, 0.95, 1.0),
            _Shell(rng, 58, 3, 0.66, 0.070, (-0.70, 0.50), -1.7, 0.019, 0.72, 0.5),
            _Shell(rng, 22, 3, 0.36, 0.080, (1.10, -0.40), 2.6, 0.017, 0.70, 0.0),
        ]
        n = 120
        self.ring_a = np.linspace(0, 2 * np.pi, n + 1).astype(F32)
        i = np.arange(n)
        self.rings = [
            _Ring(1.27, 1.25, 0.30, 0.50, ((i // 6) % 2 == 0).astype(F32), (0,), 0.95),
            _Ring(1.40, -1.05, -0.55, -0.34, np.full(n, 0.62, F32), (20, 80), 0.80),
            _Ring(1.53, 0.45, 1.20, 0.22, (i % 3 == 0).astype(F32), (55,), 1.00),
        ]
        self.k1 = np.array((0.3, 1.0, 0.2), F32)
        self.k2 = np.array((1.0, -0.2, 0.6), F32)
        nt = 72
        ta = np.linspace(0, 2 * np.pi, nt, endpoint=False)
        self.tick_cos, self.tick_sin = np.cos(ta).astype(F32), np.sin(ta).astype(F32)
        self.tick_i = np.arange(nt, dtype=F32)
        self.bars = np.zeros(nt, F32)
        self.scan_a = np.linspace(0, 2 * np.pi, 65).astype(F32)
        # a little cube at the core: it sits still at the centre, and hops when Jarvis speaks
        self.cube_p = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], F32)
        self.cube_e = np.array([(i, j) for i in range(8) for j in range(i + 1, 8)
                                if bin(i ^ j).count("1") == 1], np.int64)      # the 12 edges
        self.cube_yaw = 0.0
        self.hop_y = 0.0        # height above the centre, in sphere radii
        self.hop_v = 0.0        # vertical speed
        self.t = 0.0
        self.level = self.asm = self.scan = self.ticks = 0.0
        self.spin, self.energy, self.spark = 0.3, 0.6, 0.1
        self.look = np.zeros(2, F32)             # where the pointer is, -1..1 each way: the orb leans towards it
        self.look_to = np.zeros(2, F32)
        self.boost = 0.0                         # extra brightness (pointer hovering over the orb)
        self.pulses: list[float] = []            # ages of the shock rings in flight
        self.prev_mode = "booting"
        self.set_theme(theme)
        self.col = self.theme[0].copy()
        self.resize(size)

    def set_theme(self, name: str):
        self.theme = tuple(np.array(c, F32) for c in THEMES.get(name, THEMES["gold"]))

    def pulse(self):
        """Send a ring of light out from the ball (he has just been woken, or clicked)."""
        if len(self.pulses) < 4:
            self.pulses.append(0.0)

    def resize(self, size: int):
        """(Re)build everything that depends on the picture size: the glass ball shading, rim light and glow maps."""
        S = self.S = int(size)
        c = self.c = (S - 1) / 2.0
        R = self.R = S * SPHERE
        y, x = np.mgrid[0:S, 0:S].astype(F32)
        dx, dy = (x - c) / R, (y - c) / R
        r2 = dx * dx + dy * dy
        r = np.sqrt(r2)
        inside = np.clip((1.0 - r) * R / 1.5 + 0.5, 0.0, 1.0)             # 1 inside the ball, soft 1.5px edge
        nz = np.sqrt(np.clip(1.0 - r2, 0.0, 1.0))                         # surface normal towards the viewer
        light = np.array((-0.42, -0.58, 0.70))
        light /= np.linalg.norm(light)
        diffuse = np.clip(dx * light[0] + dy * light[1] + nz * light[2], 0.0, 1.0)
        half = light + (0, 0, 1)
        half /= np.linalg.norm(half)
        rc = np.clip(r, 0.0, 1.0)
        self.body_a = (inside * (0.60 + 0.28 * rc ** 3) * self.glass).astype(F32)
        self.body = (self.body_a * (0.45 + 1.0 * diffuse)).astype(F32)
        self.rim = (inside * rc ** 6 * (0.5 + 0.5 * diffuse)).astype(F32)
        self.spec = (inside * np.clip(dx * half[0] + dy * half[1] + nz * half[2], 0, 1) ** 90 * 70.0 * self.glass).astype(F32)
        self.halo = ((1.0 - inside) * np.exp(-np.clip(r - 1.0, 0.0, None) * 3.6)).astype(F32)
        self.coreglow = np.exp(-r2 / 0.16).astype(F32)
        self.corehot = np.exp(-r2 / 0.045).astype(F32)
        self.fade = (np.clip((S * 0.5 - 1.0 - r * R) / (S * 0.05), 0.0, 1.0) / 255.0).astype(F32)
        self.e8 = np.zeros((S, S, 3), np.uint8)
        self.b1 = np.zeros((S, S, 3), np.uint8)
        self.s1 = max(0.8, S / 240.0)
        self.Sq = max(8, S // 4)
        self.s2 = max(1.0, S * 0.009)
        self.lw = 1 if S < 470 else 2

    # ---- drawing helpers
    def _project(self, q):
        f = CAM / (CAM - q[:, 2])
        return self.c + self.R * q[:, 0] * f, self.c - self.R * q[:, 1] * f

    def _stroke(self, seg, bright, colour, thick=1):
        """Draw line segments (m,2,2), each at its own brightness, in a handful of batched calls."""
        if not len(seg):
            return
        q = np.minimum((np.clip(bright, 0.0, 1.0) * NB).astype(np.int32), NB - 1)
        q[bright < 0.035] = -1
        fixed = np.rint(seg * (1 << FP)).astype(np.int32)
        cb, cg, cr = float(colour[0]), float(colour[1]), float(colour[2])
        for k in range(NB):
            sel = fixed[q == k]
            if len(sel):
                v = (k + 0.75) / NB
                cv2.polylines(self.e8, list(sel), False, (cb * v, cg * v, cr * v), thick, cv2.LINE_AA, FP)

    def _dots(self, x, y, rad, bright, mask):
        col, hot = self.col, self.theme[1]
        b = np.clip(bright, 0.0, 1.3)
        cols = np.clip(col[None, :] * b[:, None] + (hot - col)[None, :] * (np.minimum(b, 1.0) ** 3)[:, None], 0, 255).tolist()
        S = self.S
        ok = mask & (b > 0.05) & (x > -8) & (x < S + 8) & (y > -8) & (y < S + 8)
        idx = np.flatnonzero(ok)
        idx = idx[np.argsort(b[idx])]
        xi = np.rint(x * 16).astype(np.int64).tolist()
        yi = np.rint(y * 16).astype(np.int64).tolist()
        ri = np.maximum(np.rint(rad * 16), 9).astype(np.int64).tolist()
        e8 = self.e8
        for i in idx.tolist():
            cv2.circle(e8, (xi[i], yi[i]), ri[i], cols[i], -1, cv2.LINE_AA, 4)

    def _shell(self, geo, part):
        sh, x, y, zn, b, seg, docked = geo
        e0, e1 = sh.e[:, 0], sh.e[:, 1]
        zm = (zn[e0] + zn[e1]) * 0.5
        be = (b[e0] + b[e1]) * 0.5
        spark = np.clip(np.sin(self.t * 4.2 + sh.phase), 0.0, 1.0) ** 8
        be = be * sh.line * self.energy + spark * self.spark * (0.25 + 0.75 * be)
        if docked is not None:                                            # a link lights up once both its nodes arrive
            be = be * np.minimum(docked[e0], docked[e1]) ** 8
        emask = zm < 0 if part == "back" else zm >= 0 if part == "front" else np.ones(len(zm), bool)
        self._stroke(seg[emask], be[emask], self.col, self.lw)
        nmask = zn < 0 if part == "back" else zn >= 0 if part == "front" else np.ones(len(zn), bool)
        twinkle = 1.0 + 0.22 * np.sin(self.t * 2.6 + sh.delay * 6.283)
        bn = b * (0.5 + 0.5 * self.energy) * sh.glow * twinkle
        rad = self.R * sh.node * sh.size * (0.65 + 0.55 * np.clip(zn * 0.5 + 0.5, 0, 1))
        self._dots(x, y, rad, bn, nmask)

    # ---- one frame
    def render(self, dt: float, mode: str = "idle", level: float = 0.0, assemble: float = 1.0, out=None):
        dt = max(0.0, min(dt, 0.1))
        spin_t, energy_t, scan_t, spark_t, ticks_t = MODES.get(mode, MODES["idle"])
        k = min(1.0, dt * 4.5)
        self.spin += (spin_t - self.spin) * k
        self.energy += (energy_t - self.energy) * k
        self.scan += (scan_t - self.scan) * k
        self.spark += (spark_t - self.spark) * k
        self.ticks += (ticks_t - self.ticks) * k
        self.level += (level - self.level) * min(1.0, dt * (16.0 if level > self.level else 5.0))
        self.asm += (assemble - self.asm) * min(1.0, dt * 1.8)
        base, hot, think = self.theme
        goal = ALERT if mode == "alert" else think if mode == "thinking" else base + (hot - base) * 0.30 if mode == "listening" else base
        self.col += (goal - self.col) * min(1.0, dt * 5.0)
        self.look += (self.look_to - self.look) * min(1.0, dt * 5.0)
        if mode != self.prev_mode:
            if mode == "listening" and self.prev_mode in ("idle", "speaking", "thinking"):
                self.pulse()                                              # woken: a ring of light goes out
            self.prev_mode = mode
        self.t += dt
        t, lvl, en, col = self.t, self.level, min(1.15, self.energy + self.boost), self.col
        lean = _rx(float(self.look[1]) * 0.38) @ _ry(float(self.look[0]) * 0.50)   # tip towards the pointer
        vis = self.asm * self.asm * (3 - 2 * self.asm)                    # 0..1: how assembled the sphere is
        S, c, R, lw = self.S, self.c, self.R, self.lw
        E = self.e8
        E[:] = 0
        c16 = (int(round(c * 16)), int(round(c * 16)))

        # --- orbital rings: work out where they are; the far halves are drawn first, the near halves last
        ring_col = col * 0.85 + hot * 0.15
        rings = []
        for i, ring in enumerate(self.rings):
            ring.phase += dt * ring.speed * (0.6 + self.spin)
            tilt = lean @ _rz(ring.tz + 0.16 * math.sin(t * 0.23 + i * 2.1)) @ _rx(ring.tx + 0.13 * math.sin(t * 0.19 + i))
            a = self.ring_a + ring.phase
            p = np.zeros((len(a), 3), F32)
            p[:, 0], p[:, 2] = np.cos(a), np.sin(a)
            q = (p * (ring.radius * (1.0 + 0.05 * lvl))) @ tilt.T
            x, y = self._project(q)
            z = q[:, 2] / ring.radius
            zm = (z[:-1] + z[1:]) * 0.5
            b = (0.28 + 0.72 * np.clip(zm * 0.5 + 0.5, 0, 1)) * ring.gain * en * vis
            mx, my = (x[:-1] + x[1:]) * 0.5 - c, (y[:-1] + y[1:]) * 0.5 - c
            behind = (zm < 0) & (mx * mx + my * my < (R * 0.99) ** 2)     # seen through the glass ball
            b = np.where(behind, b * 0.28, b) * ring.pattern
            seg = np.stack([np.stack([x[:-1], y[:-1]], 1), np.stack([x[1:], y[1:]], 1)], 1)
            rings.append((ring, seg, b, zm < 0, x, y, z))
        for ring, seg, b, back, *_ in rings:
            self._stroke(seg[back], b[back], ring_col, lw)

        # --- the three lattice shells
        geo = []
        for sh in self.shells:
            sh.yaw += dt * sh.rate * self.spin
            m = lean @ sh.tilt @ _ry(sh.yaw)
            p = sh.p
            rad = 1.0 + 0.012 * math.sin(t * 1.4)
            if sh.wave:
                rad = rad + (lvl * 0.15 * sh.wave) * np.sin(p @ self.k1 * 5.0 + t * 8.0) * np.sin(p @ self.k2 * 3.0 - t * 5.5)
                p = p * rad[:, None]
            else:
                p = p * rad
            docked = None
            if self.asm < 0.999:                                          # still flying in
                docked = np.clip(self.asm * 1.7 - sh.delay * 0.7, 0.0, 1.0)
                docked = docked * docked * (3 - 2 * docked)
                p = sh.scatter + (p - sh.scatter) * docked[:, None]
            q = (p @ m.T) * sh.radius
            x, y = self._project(q)
            zn = np.clip(q[:, 2] / sh.radius, -1.5, 1.5)
            b = 0.14 + 0.86 * np.clip(zn * 0.5 + 0.5, 0.0, 1.0) ** 1.5
            e0, e1 = sh.e[:, 0], sh.e[:, 1]
            seg = np.stack([np.stack([x[e0], y[e0]], 1), np.stack([x[e1], y[e1]], 1)], 1)
            geo.append((sh, x, y, zn, b, seg, docked))

        # --- scan ring: a line of latitude sweeping up and down the ball while thinking
        scan = None
        if self.scan > 0.03:
            y0 = 0.94 * math.sin(t * 1.7)
            rr = math.sqrt(max(0.0, 1.0 - y0 * y0)) * 1.02
            p = np.zeros((len(self.scan_a), 3), F32)
            p[:, 0], p[:, 1], p[:, 2] = rr * np.cos(self.scan_a), y0 * 1.02, rr * np.sin(self.scan_a)
            q = p @ _rx(0.30).T
            x, y = self._project(q)
            zm = (q[:-1, 2] + q[1:, 2]) * 0.5
            b = (0.16 + 0.84 * np.clip(zm * 0.5 + 0.5, 0, 1)) * self.scan * vis
            seg = np.stack([np.stack([x[:-1], y[:-1]], 1), np.stack([x[1:], y[1:]], 1)], 1)
            scan = (seg, b, zm < 0)
            self._stroke(seg[scan[2]], b[scan[2]], hot, lw)

        self._shell(geo[0], "back")
        self._shell(geo[1], "all")
        self._shell(geo[2], "all")

        # --- core: a white-hot nucleus with arcs turning round it
        core_r = R * (0.10 + 0.09 * lvl + 0.008 * math.sin(t * 2.3)) * (0.35 + 0.65 * vis)
        kc = 0.7 + 0.3 * en
        cv2.circle(E, c16, max(16, int(core_r * 16)), tuple(float(v) * kc for v in hot), -1, cv2.LINE_AA, 4)
        arc_col = tuple(float(v) * (0.55 + 0.45 * en) * vis for v in (col * 0.6 + hot * 0.4))
        for rr, speed, span in ((0.19, 1.6, 70), (0.25, -1.1, 46)):
            ang = math.degrees(t * speed * (0.5 + self.spin))
            ax = int(R * (rr + 0.07 * lvl) * 16)
            for j in range(3):
                cv2.ellipse(E, c16, (ax, ax), ang + j * 120, 0, span, arc_col, lw, cv2.LINE_AA, 4)

        # --- the hopping cube: still at the centre, springs up while he speaks, lands and rests when quiet
        self.hop_v -= 7.0 * dt                                             # gravity
        self.hop_y += self.hop_v * dt
        if self.hop_y <= 0.0:
            self.hop_y = 0.0
            if mode == "speaking" and lvl > 0.05:                          # kick off again on every landing
                self.hop_v = 1.9 * (0.5 + 0.9 * min(1.0, lvl)) * float(self.rng.uniform(0.85, 1.15))
            else:
                self.hop_v = 0.0
        self.cube_yaw += dt * (0.5 + self.spin)
        hs = R * 0.19 * vis                                                # half-size, grows in as it assembles
        if hs > 1.0:
            cm = lean @ _ry(self.cube_yaw) @ _rx(self.cube_yaw * 0.6)
            qc = (self.cube_p * (hs / R)) @ cm.T                           # rotate in sphere units
            qc[:, 1] += self.hop_y                                         # ...then hop upward
            xc, yc = self._project(qc)
            z0, z1 = qc[self.cube_e[:, 0], 2], qc[self.cube_e[:, 1], 2]
            zb = (z0 + z1) * 0.5
            be = (0.30 + 0.70 * np.clip(zb / (hs / R) * 0.5 + 0.5, 0, 1)) * en * vis
            seg = np.stack([np.stack([xc[self.cube_e[:, 0]], yc[self.cube_e[:, 0]]], 1),
                            np.stack([xc[self.cube_e[:, 1]], yc[self.cube_e[:, 1]]], 1)], 1)
            self._stroke(seg, be, hot, lw)
            cv = np.clip(0.35 + 0.65 * (qc[:, 2] / (hs / R) * 0.5 + 0.5), 0, 1) * en * vis   # bright vertices
            for i in range(8):
                if cv[i] > 0.05:
                    cv2.circle(E, (int(xc[i] * 16), int(yc[i] * 16)), int(R * 0.028 * 16),
                               tuple(float(h) * float(cv[i]) for h in hot), -1, cv2.LINE_AA, 4)

        self._shell(geo[0], "front")
        if scan:
            self._stroke(scan[0][~scan[2]], scan[1][~scan[2]], hot, lw)

        # --- voice ticks round the ball: your microphone while listening, his voice while speaking
        if self.ticks > 0.02:
            n = len(self.bars)
            goal = lvl * (0.35 + 0.65 * np.abs(np.sin(self.tick_i * 0.9 + t * 7.0))) * self.rng.uniform(0.6, 1.0, n)
            self.bars += (goal.astype(F32) - self.bars) * min(1.0, dt * 14.0)
            r1 = R * 1.075
            r2 = r1 + R * (0.02 + self.bars * 0.20)
            seg = np.empty((n, 2, 2), F32)
            seg[:, 0, 0], seg[:, 0, 1] = c + r1 * self.tick_cos, c + r1 * self.tick_sin
            seg[:, 1, 0], seg[:, 1, 1] = c + r2 * self.tick_cos, c + r2 * self.tick_sin
            self._stroke(seg, (0.18 + 0.62 * np.minimum(1.0, self.bars * 3.0)) * self.ticks * en * vis, col, lw)
        else:
            self.bars[:] = 0

        # --- shock rings: one goes out when he is woken or clicked
        if self.pulses:
            self.pulses = [age + dt for age in self.pulses if age + dt < 0.7]
            for age in self.pulses:
                k2 = age / 0.7
                v = (1.0 - k2) ** 1.5
                cv2.circle(E, c16, int(R * (1.02 + 0.62 * k2 ** 0.6) * 16), tuple(float(h) * v for h in hot),
                           lw + 1, cv2.LINE_AA, 4)

        # --- near halves of the rings, and the bright satellites that ride them
        for ring, seg, b, back, x, y, z in rings:
            self._stroke(seg[~back], b[~back], ring_col, lw)
            for sidx in ring.sats:
                bz = float(np.clip(z[sidx] * 0.5 + 0.5, 0, 1))
                dxs, dys = float(x[sidx]) - c, float(y[sidx]) - c
                dim = 0.3 if (z[sidx] < 0 and dxs * dxs + dys * dys < R * R) else 1.0
                v = (0.45 + 0.55 * bz) * dim * en * vis
                if v > 0.05:
                    cv2.circle(E, (int(x[sidx] * 16), int(y[sidx] * 16)), int(R * 0.030 * (0.7 + 0.5 * bz) * 16),
                               tuple(float(h) * v for h in hot), -1, cv2.LINE_AA, 4)

        # --- glow: a tight bloom plus a wide one, then light the glass ball and composite
        cv2.GaussianBlur(E, (0, 0), self.s1, dst=self.b1)
        small = cv2.GaussianBlur(cv2.resize(E, (self.Sq, self.Sq), interpolation=cv2.INTER_AREA), (0, 0), self.s2)
        wide = cv2.resize(small, (S, S), interpolation=cv2.INTER_LINEAR)
        lit = cv2.addWeighted(cv2.addWeighted(E, 1.0, self.b1, 0.8, 0.0), 1.0, wide, 1.15, 0.0)
        g = (self.rim * (0.55 * en * vis) + self.halo * ((0.16 + 0.42 * lvl) * en * vis)
             + self.coreglow * ((0.36 + 0.50 * lvl) * en * (0.3 + 0.7 * vis)))
        h = self.corehot * ((0.25 + 0.45 * lvl) * en * vis)
        # one colour plane at a time: flat 2D arrays keep numpy quick (a 3-wide last axis is very slow)
        planes = []
        for ch, plane in enumerate(cv2.split(lit)):
            p = plane.astype(F32)
            p += g * float(col[ch])
            if hot[ch] > col[ch]:
                p += h * float(hot[ch] - col[ch])
            p += self.spec
            p *= self.fade                                                # now 0..1, and gone by the window edge
            planes.append(p)
        a = np.maximum(np.maximum(planes[0], planes[1]), planes[2])
        a += self.body_a
        np.minimum(a, 1.0, out=a)
        tint = np.array((0.075, 0.050, 0.035), F32) + col * (0.05 / 255.0)
        chans = []
        for ch, p in enumerate(planes):
            p += self.body * float(tint[ch])
            np.minimum(p, a, out=p)                                       # premultiplied alpha: colour <= alpha
            chans.append(cv2.convertScaleAbs(p, alpha=255.0))
        chans.append(cv2.convertScaleAbs(a, alpha=255.0))
        frame = cv2.merge(chans)
        if out is None:
            return frame
        np.copyto(out, frame)
        return out


# ------------------------------------------------------------------ Win32 plumbing
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_PTR = ctypes.c_ssize_t
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x20, 0x80, 0x80000, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
TOPMOST = wintypes.HWND(-1)
SWP_QUIET = 0x0001 | 0x0002 | 0x0010                                     # no size, no move, don't activate


class _BLEND(ctypes.Structure):
    _fields_ = [("op", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("alpha", ctypes.c_ubyte), ("format", ctypes.c_ubyte)]


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


user32.GetWindowLongPtrW.restype = _PTR
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = _PTR
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, _PTR]
user32.GetParent.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       wintypes.DWORD, ctypes.POINTER(_BLEND), wintypes.DWORD]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BMIH), wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


class _Surface:
    """A 32-bit bitmap the orb is drawn straight into, pushed to the screen with per-pixel transparency."""

    def __init__(self, size: int):
        self.size = size
        self.screen = user32.GetDC(None)
        self.dc = gdi32.CreateCompatibleDC(self.screen)
        head = _BMIH(ctypes.sizeof(_BMIH), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)       # negative height = top-down
        bits = ctypes.c_void_p()
        self.bmp = gdi32.CreateDIBSection(self.dc, ctypes.byref(head), 0, ctypes.byref(bits), None, 0)
        if not self.bmp or not bits.value:
            raise OSError("could not create the orb's drawing surface")
        self.old = gdi32.SelectObject(self.dc, self.bmp)
        self._buf = (ctypes.c_ubyte * (size * size * 4)).from_address(bits.value)
        self.pixels = np.frombuffer(self._buf, np.uint8).reshape(size, size, 4)

    def present(self, hwnd, alpha: float = 255) -> bool:
        blend = _BLEND(0, 0, max(0, min(255, int(alpha))), 1)
        size, origin = wintypes.SIZE(self.size, self.size), wintypes.POINT(0, 0)
        return bool(user32.UpdateLayeredWindow(hwnd, self.screen, None, ctypes.byref(size), self.dc,
                                               ctypes.byref(origin), 0, ctypes.byref(blend), 2))

    def free(self):
        self.pixels = self._buf = None
        gdi32.SelectObject(self.dc, self.old)
        gdi32.DeleteObject(self.bmp)
        gdi32.DeleteDC(self.dc)
        user32.ReleaseDC(None, self.screen)


def _hwnd(win) -> int:
    """The real top-level window handle behind a Tk window."""
    win.update_idletasks()
    try:
        h = int(win.wm_frame(), 16)
    except Exception:
        h = 0
    return h or user32.GetParent(win.winfo_id()) or win.winfo_id()


def _add_style(hwnd, bits: int):
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) | bits)


def _work_area():
    r = wintypes.RECT()
    if user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0):
        return r.left, r.top, r.right, r.bottom
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def _fullscreen_app() -> int:
    """What owns the screen: 0 = nothing special, 2 = a full-screen app or video, 3 = an exclusive full-screen
    (Direct3D) game, 4 = a presentation."""
    state = ctypes.c_int(0)
    try:
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value in (2, 3, 4):
            return state.value
    except Exception:
        pass
    return 0


def _hex(bgr) -> str:
    return "#%02x%02x%02x" % (int(bgr[2]), int(bgr[1]), int(bgr[0]))


CAPTION_WHO = {"YOU": "YOU", "JARVIS": "J.A.R.V.I.S.", "ALERT": "SECURITY ALERT", "BOOT": "INITIALISING"}
HINT = "click: talk  ·  double-click: type to me  ·  right-click: menu  ·  wheel: volume  ·  drag: move"


# ------------------------------------------------------------------ the orb on the desktop
class Orb:
    """Same interface as the old HUD window (run / close), but it is a ball floating on the desktop."""

    def __init__(self, cfg: dict, on_text, on_talk, on_quit, security=None, on_stop=None):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        ui = cfg.get("ui", {})
        self.on_text, self.on_talk, self.on_quit, self.security = on_text, on_talk, on_quit, security
        self.on_stop = on_stop                                           # middle-click: stop talking
        self.hotkey = cfg.get("voice", {}).get("push_to_talk", "ctrl+alt+j") or "ctrl+alt+j"
        self.captions = bool(ui.get("captions", True))
        self.hide_fullscreen = bool(ui.get("hide_in_fullscreen", True))
        self.frame_ms = 1000.0 / max(10, min(60, int(ui.get("orb_fps", 30))))
        self.store = resolve("data/orb.json")
        saved = self._load()
        self.theme = saved.get("theme") if saved.get("theme") in THEMES else ui.get("orb_theme", "gold")
        if self.theme not in THEMES:
            self.theme = "gold"
        self.size = max(140, min(560, int(saved.get("size") or ui.get("orb_size", 300))))

        self.root = root = tk.Tk()
        root.title("J.A.R.V.I.S.")
        root.overrideredirect(True)
        root.configure(bg="black", cursor="hand2")
        root.geometry("16x16+-4000+-4000")                               # created off-screen: no flash of a grey square
        root.attributes("-topmost", True)
        root.update()
        self.s = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.hwnd = _hwnd(root)
        # layered = we supply every pixel with its own transparency; no-activate = clicking the orb never steals
        # the keyboard from the app you are working in (so "type hello" still lands in that app)
        _add_style(self.hwnd, WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        self.W = int(self.size * self.s) // 2 * 2
        self.renderer = OrbRenderer(self.W, self.theme, float(ui.get("orb_glass", 1.0)))
        self.surface = _Surface(self.W)
        left, top, right, bottom = _work_area()
        x = saved.get("x", right - self.W - int(10 * self.s))
        y = saved.get("y", bottom - self.W - int(6 * self.s))
        self.x, self.y = self._clamp(int(x), int(y))
        root.geometry(f"{self.W}x{self.W}+{self.x}+{self.y}")

        root.bind("<ButtonPress-1>", self._press)
        root.bind("<B1-Motion>", self._drag)
        root.bind("<ButtonRelease-1>", self._release)
        root.bind("<Double-Button-1>", self._double)
        root.bind("<Button-3>", self._menu_open)
        root.bind("<Button-2>", lambda _: self.on_stop() if self.on_stop else None)
        root.bind("<MouseWheel>", self._wheel)
        root.bind("<Enter>", lambda _: self._hover(True))
        root.bind("<Leave>", lambda _: self._hover(False))

        self.alpha = 0.0
        self.shown = True
        self.hidden = self.suppressed = self.closing = self.quitting = self.dead = False
        self.hover = False
        self.hover_since = 0.0
        self.fullscreen = 0
        self.busy_until = 0.0
        self._mode = "booting"
        self._down = None
        self._moved = self._dbl = False
        self._click_job = None
        self.menu = None
        self.console = None
        self.console_open = False
        self.seen_version = -1
        self.last_stats = 0.0
        self.cap_shown = None
        self.cap_visible = False
        self._slow = 0.0
        self._errors = 0
        self._build_caption()
        psutil.cpu_percent(None)
        self.last = time.perf_counter()
        root.after(20, self._frame)

    # ---- small state file: where the orb sits, how big, which colour
    def _load(self) -> dict:
        try:
            return json.loads(self.store.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self):
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            self.store.write_text(json.dumps({"x": self.x, "y": self.y, "size": self.size, "theme": self.theme}),
                                  encoding="utf-8")
        except Exception as e:
            log.debug("could not save orb state: %s", e)

    def _clamp(self, x: int, y: int):
        """Keep the middle of the orb on a screen."""
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78) or user32.GetSystemMetrics(0), user32.GetSystemMetrics(79) or user32.GetSystemMetrics(1)
        half = self.W // 2
        return max(vx - half + 20, min(vx + vw - half - 20, x)), max(vy - half + 20, min(vy + vh - half - 20, y))

    def _place(self, x: int, y: int):
        self.x, self.y = self._clamp(int(x), int(y))
        self.root.geometry(f"{self.W}x{self.W}+{self.x}+{self.y}")
        self.cap_shown = None                                            # caption follows the orb

    # ---- public (same as the HUD window)
    def run(self):
        self.root.mainloop()

    def close(self):
        """Thread-safe request to close: the frame loop fades the orb out and ends the program."""
        self.closing = True

    # ---- mouse
    def _press(self, _):
        self._menu_close()
        # the real cursor position, not the event's: event coordinates are relative to a window that is moving
        self._down = (*self.root.winfo_pointerxy(), self.x, self.y)
        self._moved = False

    def _drag(self, _):
        if not self._down:
            return
        px, py = self.root.winfo_pointerxy()
        dx, dy = px - self._down[0], py - self._down[1]
        if abs(dx) + abs(dy) > 5 * self.s:
            self._moved = True
        if self._moved:
            self._place(self._down[2] + dx, self._down[3] + dy)

    def _release(self, _):
        if self._dbl:
            self._dbl = False
        elif self._moved:
            self._save()
        elif self._down:
            if self._click_job:
                self.root.after_cancel(self._click_job)
            self._click_job = self.root.after(240, self._click)          # wait a beat in case it is a double-click
        self._down = None

    def _click(self):
        self._click_job = None
        self.renderer.pulse()
        self.on_talk()

    def _wheel(self, e):
        """Roll the mouse wheel over the orb to turn the system volume up or down, one small step per notch."""
        try:
            from .tools import _press
            _press(0xAF if e.delta > 0 else 0xAE, 1)
            self.renderer.boost = 0.35
        except Exception:
            pass

    def _double(self, _):
        if self._click_job:
            self.root.after_cancel(self._click_job)
            self._click_job = None
        self._dbl = True
        self.toggle_console()

    def _hover(self, on: bool):
        self.hover, self.hover_since = on, time.time()

    # ---- things the menu and voice commands can do
    def hide(self):
        self.hidden = True

    def show(self):
        self.hidden = False

    def set_theme(self, name: str):
        name = COLOUR_WORDS.get(str(name).lower(), str(name).lower())
        if name in THEMES:
            self.theme = name
            self.renderer.set_theme(name)
            self.cap_shown = None
            self._save()

    def resize(self, size: float):
        size = max(140, min(560, int(size)))
        if size == self.size:
            return
        centre = (self.x + self.W // 2, self.y + self.W // 2)
        self.size = size
        self.W = int(size * self.s) // 2 * 2
        old = self.surface
        self.surface = _Surface(self.W)
        self.renderer.resize(self.W)
        old.free()
        self._place(centre[0] - self.W // 2, centre[1] - self.W // 2)
        self.root.update_idletasks()
        self._save()

    def move_to(self, where: str):
        left, top, right, bottom = _work_area()
        m = int(8 * self.s)
        where = (where or "").lower()
        x = left + m if "left" in where else right - self.W - m if "right" in where else (left + right - self.W) // 2
        y = top + m if "top" in where else bottom - self.W - m if "bottom" in where else (top + bottom - self.W) // 2
        if where.strip() in ("left", "right"):
            y = self.y
        if where.strip() in ("top", "bottom"):
            x = self.x
        self._place(x, y)
        self._save()

    def _requests(self):
        """Voice commands for the orb arrive here from the assistant thread."""
        while STATE.ui_requests:
            try:
                action, value = STATE.ui_requests.popleft()
            except IndexError:
                break
            if action == "show":
                self.show()
            elif action == "hide":
                self.hide()
            elif action == "console":
                self.show_console() if value else self.hide_console()
            elif action == "theme":
                self.set_theme(value)
            elif action == "size":
                self.resize(self.size * (1.25 if value and value > 0 else 0.8))
            elif action == "move":
                self.move_to(value)

    def _quit(self):
        """Asked to quit from the menu: let Jarvis say goodbye, then go (forced after a few seconds)."""
        if not self.quitting:
            self.quitting = True
            self.on_quit()
            self.root.after(6000, self.close)

    def _destroy(self):
        self.dead = True
        try:
            self.surface.free()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---- the frame loop
    def _frame(self):
        if self.dead:
            return
        t0 = time.perf_counter()
        try:
            self._step(t0)
        except tk.TclError:
            return
        except Exception:
            self._errors += 1
            if self._errors <= 3:
                log.exception("orb frame failed")
        if self.dead:
            return
        spent = (time.perf_counter() - t0) * 1000.0
        # Standing by with nobody near: 20 frames a second is plenty for a slow turn and halves the cost.
        lively = self._mode != "idle" or self.hover or self._down or self.renderer.pulses or self.alpha < 1.0
        self.root.after(max(4, int((self.frame_ms if lively else max(self.frame_ms, 50.0)) - spent)), self._frame)

    def _step(self, now: float):
        dt, self.last = now - self.last, now
        self._requests()
        if now - self._slow > 1.0:
            self._slow = now
            self.fullscreen = _fullscreen_app() if self.hide_fullscreen else 0
            if not self.menu:                                            # stay above other always-on-top windows
                user32.SetWindowPos(self.hwnd, TOPMOST, 0, 0, 0, 0, SWP_QUIET)
        mode = self._mode = "alert" if STATE.alerting else STATE.mode
        if mode != "idle":
            self.busy_until = now + 3.0
        # Step aside for full-screen apps: always for an exclusive full-screen game, otherwise only while he is
        # standing by. Talk to him during a full-screen video and he fades in for the exchange, then out again.
        self.suppressed = self.fullscreen == 3 or (self.fullscreen in (2, 4) and now > self.busy_until)
        want = 0.0 if (self.closing or self.hidden or self.suppressed) else 1.0
        self.alpha += (want - self.alpha) * min(1.0, dt * 6.0)
        if abs(want - self.alpha) < 0.02:
            self.alpha = want
        if self.closing and self.alpha <= 0.0:
            self._destroy()
            return
        showing = self.alpha > 0.0
        if showing != self.shown:
            self.shown = showing
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE if showing else SW_HIDE)
        if showing:
            level = (STATE.voice_level if mode == "speaking" else STATE.mic_level if mode == "listening"
                     else STATE.mic_level * 0.3 if mode == "idle" else 0.0)
            if self.closing:
                assemble = 0.0
            elif STATE.booting:
                assemble = min(0.8, 0.12 + len(STATE.boot_lines) / 9.0)   # comes together as systems come online
            else:
                assemble = 1.0
            # The orb leans towards the mouse pointer when it is nearby, and brightens under it.
            r = self.renderer
            try:
                px, py = self.root.winfo_pointerxy()
                dx = (px - (self.x + self.W / 2)) / (self.W * 1.6)
                dy = (py - (self.y + self.W / 2)) / (self.W * 1.6)
                far = max(abs(dx), abs(dy))
                near = max(0.0, min(1.0, 2.2 - far)) if far > 1 else 1.0  # full within ~1.6 widths, gone by ~3.5
                r.look_to[0], r.look_to[1] = max(-1.0, min(1.0, dx)) * near, max(-1.0, min(1.0, dy)) * near
            except Exception:
                pass
            r.boost += ((0.14 if self.hover else 0.0) - r.boost) * min(1.0, dt * 6.0)
            r.render(dt, mode, level, assemble, out=self.surface.pixels)
            self.surface.present(self.hwnd, self.alpha * 255)
        self._caption(mode, showing)
        if self.console_open:
            self._console_tick(mode)

    # ---- captions: what was heard / what is being said, under the orb
    def _build_caption(self):
        s = self.s
        cap = self.cap = tk.Toplevel(self.root)
        cap.overrideredirect(True)
        cap.attributes("-topmost", True)
        try:
            cap.attributes("-alpha", 0.93)
        except Exception:
            pass
        cap.geometry("+-4000+-4000")
        box = tk.Frame(cap, bg="#070b12")
        box.pack(padx=1, pady=1)
        self.cap_who = tk.Label(box, text="", bg="#070b12", fg=WHITE, font=("Consolas", 8, "bold"))
        self.cap_who.pack(padx=int(14 * s), pady=(int(6 * s), 0))
        self.cap_text = tk.Label(box, text="", bg="#070b12", fg=WHITE, font=("Segoe UI", 10), justify="center",
                                 wraplength=int(360 * s))
        self.cap_text.pack(padx=int(14 * s), pady=(0, int(7 * s)))
        cap.update()
        self.cap_hwnd = _hwnd(cap)
        _add_style(self.cap_hwnd, WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)   # clicks pass through it
        user32.ShowWindow(self.cap_hwnd, SW_HIDE)

    def _caption(self, mode: str, showing: bool):
        label = body = None
        if self.captions and showing and not self.closing:
            who, text, stamp = STATE.live
            age = time.time() - stamp
            if text and (age < 2.4 + min(6.0, len(text) * 0.05) or (who == "JARVIS" and STATE.speaking and age < 40)):
                label, body = CAPTION_WHO.get(who, LABELS.get(mode, "")), text
            elif mode == "listening":
                label, body = "LISTENING", "Go ahead, sir."
            elif mode == "thinking":
                label, body = "PROCESSING", "One moment, sir."
            elif self.hover and not self._down and not self.menu and time.time() - self.hover_since > 0.7:
                label, body = "J.A.R.V.I.S.", HINT
        if body is None:
            if self.cap_visible:
                self.cap_visible = False
                self.cap_shown = None
                user32.ShowWindow(self.cap_hwnd, SW_HIDE)
            return
        if (label, body) == self.cap_shown:
            return
        self.cap_shown = (label, body)
        accent = _hex(ALERT) if label == "SECURITY ALERT" else _hex(THEMES[self.theme][0])
        self.cap.configure(bg=mix(accent, 0.45, "#070b12"))
        self.cap_who.configure(text=label, fg=accent)
        self.cap_text.configure(text=body if len(body) < 260 else body[:257] + "...")
        self.cap.update_idletasks()
        w, h = self.cap.winfo_reqwidth(), self.cap.winfo_reqheight()
        left, top, right, bottom = _work_area()
        x = max(left + 4, min(right - w - 4, self.x + self.W // 2 - w // 2))
        y = self.y + int(self.W * 0.97)
        if y + h > bottom - 4:                                           # no room underneath: sit on top instead
            y = self.y + int(self.W * 0.03) - h
        self.cap.geometry(f"+{x}+{max(top + 4, y)}")
        if not self.cap_visible:
            self.cap_visible = True
            user32.ShowWindow(self.cap_hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(self.cap_hwnd, TOPMOST, 0, 0, 0, 0, SWP_QUIET)

    # ---- right-click menu (drawn by us so it matches the orb)
    def _menu_close(self):
        if self.menu:
            try:
                self.menu.destroy()
            except Exception:
                pass
            self.menu = None

    def _menu_open(self, e=None):
        self._menu_close()
        s = self.s
        accent = _hex(THEMES[self.theme][0])
        m = self.menu = tk.Toplevel(self.root)
        m.overrideredirect(True)
        m.attributes("-topmost", True)
        m.configure(bg=mix(accent, 0.5, PANEL))
        box = tk.Frame(m, bg=PANEL)
        box.pack(padx=1, pady=1)
        font = ("Segoe UI", 10)
        pad = int(14 * s)

        def item(text, command, fg=WHITE):
            lbl = tk.Label(box, text=text, anchor="w", bg=PANEL, fg=fg, font=font, padx=pad, pady=int(5 * s), cursor="hand2")
            lbl.pack(fill="x")
            lbl.bind("<Enter>", lambda _: lbl.configure(bg=mix(accent, 0.20, PANEL)))
            lbl.bind("<Leave>", lambda _: lbl.configure(bg=PANEL))
            lbl.bind("<Button-1>", lambda _: (self._menu_close(), command()))

        def row(title):
            f = tk.Frame(box, bg=PANEL)
            f.pack(fill="x", padx=pad, pady=int(5 * s))
            tk.Label(f, text=title, bg=PANEL, fg=DIM, font=font, width=7, anchor="w").pack(side="left")
            return f

        item(f"Talk to Jarvis      {self.hotkey.upper()}", self.on_talk)
        item("Close the console" if self.console_open else "Open the console (type to Jarvis)", self.toggle_console)
        colours = row("Colour")
        for name, theme in THEMES.items():
            sw = tk.Label(colours, bg=_hex(theme[0]), width=3, cursor="hand2",
                          relief="solid" if name == self.theme else "flat", bd=1)
            sw.pack(side="left", padx=int(3 * s))
            sw.bind("<Button-1>", lambda _, n=name: (self.set_theme(n), self._menu_open()))
        sizes = row("Size")
        for text, factor in (("  –  ", 0.85), ("  +  ", 1.18)):
            b = tk.Label(sizes, text=text, bg=mix(accent, 0.16, PANEL), fg=WHITE, font=("Segoe UI Semibold", 10), cursor="hand2")
            b.pack(side="left", padx=int(3 * s))
            b.bind("<Button-1>", lambda _, f=factor: self.resize(self.size * f))
        item('Hide the orb  (say "Jarvis, show yourself")', self.hide)
        item("Quit Jarvis", self._quit, fg=RED)

        m.update_idletasks()
        w, h = m.winfo_reqwidth(), m.winfo_reqheight()
        left, top, right, bottom = _work_area()
        if e is not None:
            self._menu_at = (e.x_root, e.y_root)
        px, py = getattr(self, "_menu_at", (self.x + self.W // 2, self.y + self.W // 2))
        x = max(left + 4, min(right - w - 4, px - w // 2))
        y = py - h - int(6 * s) if py - h - int(6 * s) > top else py + int(6 * s)
        m.geometry(f"+{x}+{y}")
        m.bind("<Escape>", lambda _: self._menu_close())
        m.bind("<FocusOut>", lambda ev: self._menu_close() if ev.widget is m else None)
        m.after(15000, lambda: self._menu_close() if self.menu is m else None)
        try:
            m.focus_force()
        except Exception:
            pass

    # ---- console: transcript, gauges and a box to type commands (opened on demand)
    def _build_console(self):
        s = self.s
        w = self.console = tk.Toplevel(self.root)
        w.withdraw()
        w.title("J.A.R.V.I.S. console")
        w.configure(bg=BG)
        w.minsize(int(320 * s), int(360 * s))
        w.protocol("WM_DELETE_WINDOW", self.hide_console)
        w.bind("<Escape>", lambda _: self.hide_console())
        mono = ("Consolas", 9)
        top = tk.Frame(w, bg=BG)
        top.pack(fill="x", padx=14, pady=(10, 4))
        self.c_state = tk.Label(top, text="", fg=WHITE, bg=BG, font=("Segoe UI Semibold", 11))
        self.c_state.pack(side="left")
        self.c_clock = tk.Label(top, text="", fg=DIM, bg=BG, font=mono)
        self.c_clock.pack(side="right")
        gauges = tk.Frame(w, bg=BG)
        gauges.pack(fill="x", padx=14)
        self.c_gauges = {}
        for i, name in enumerate(("CPU", "RAM", "PWR")):
            f = tk.Frame(gauges, bg=BG)
            f.grid(row=0, column=i, sticky="ew", padx=3)
            gauges.columnconfigure(i, weight=1)
            lbl = tk.Label(f, text=name, fg=DIM, bg=BG, font=mono, anchor="w")
            lbl.pack(fill="x")
            bar = tk.Canvas(f, bg=PANEL, height=int(5 * s), highlightthickness=0)
            bar.pack(fill="x")
            self.c_gauges[name] = (lbl, bar)
        self.c_sec = tk.Label(w, text="", fg=DIM, bg=BG, font=mono, anchor="w", justify="left")
        self.c_sec.pack(fill="x", padx=16, pady=(8, 2))
        frame = tk.Frame(w, bg=PANEL, highlightbackground=DIM, highlightthickness=1)
        frame.pack(side="bottom", fill="x", padx=14, pady=(4, 12))
        self.c_prompt = tk.Label(frame, text="›", fg=WHITE, bg=PANEL, font=("Consolas", 12))
        self.c_prompt.pack(side="left", padx=(8, 2))
        self.c_entry = tk.Entry(frame, bg=PANEL, fg=WHITE, insertbackground=WHITE, relief="flat", font=("Segoe UI", 10))
        self.c_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        self.c_entry.bind("<Return>", self._submit)
        self.c_log = tk.Text(w, bg=BG, fg=WHITE, relief="flat", wrap="word", font=("Segoe UI", 10), highlightthickness=0,
                             padx=14, pady=4, height=8, cursor="arrow", spacing3=5)
        self.c_log.pack(fill="both", expand=True)
        self.c_log.tag_configure("you", foreground="#9fb6c2")
        self.c_log.tag_configure("jarvis", foreground=WHITE)
        self.c_log.tag_configure("sys", foreground=DIM, font=mono)
        self.c_log.tag_configure("alert", foreground=RED)
        self.c_log.configure(state="disabled")

    def _submit(self, _):
        text = self.c_entry.get().strip()
        self.c_entry.delete(0, "end")
        if text:
            self.on_text(text)

    def show_console(self):
        if not self.console:
            self._build_console()
        s = self.s
        w, h = int(400 * s), int(540 * s)
        left, top, right, bottom = _work_area()
        x = self.x - w - int(4 * s) if self.x - w - int(4 * s) >= left else self.x + self.W + int(4 * s)
        y = max(top + 4, min(bottom - h - int(40 * s), self.y + self.W // 2 - h // 2))
        self.console.geometry(f"{w}x{h}+{max(left, min(right - w, x))}+{y}")
        self.console.deiconify()
        self.console.update_idletasks()
        try:                                                             # dark title bar to match
            hwnd = _hwnd(self.console)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 20, ctypes.byref(ctypes.c_int(1)), 4)
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_QUIET | 0x0004 | 0x0020)
        except Exception:
            pass
        self.console_open = True
        self.seen_version = -1
        self.last_stats = 0.0
        self.console.lift()
        try:
            self.console.focus_force()
            self.c_entry.focus_set()
        except Exception:
            pass

    def hide_console(self):
        self.console_open = False
        if self.console:
            self.console.withdraw()

    def toggle_console(self):
        self.hide_console() if self.console_open else self.show_console()

    def _console_tick(self, mode: str):
        accent = _hex(ALERT) if mode == "alert" else _hex(THEMES[self.theme][0])
        self.c_state.configure(text=LABELS.get(mode, mode.upper()), fg=accent)
        if STATE.version != self.seen_version:
            self.seen_version = STATE.version
            self.c_prompt.configure(fg=accent)
            self.c_log.tag_configure("jarvis", foreground=accent)
            self.c_log.configure(state="normal")
            self.c_log.delete("1.0", "end")
            with STATE._lock:
                boot, items = list(STATE.boot_lines), list(STATE.log)
            if len(items) < 4:
                for line in boot[-9:]:
                    self.c_log.insert("end", f"▸ {line}\n", "sys")
            for who, text, _ in items[-40:]:
                tag = {"YOU": "you", "JARVIS": "jarvis", "ALERT": "alert"}.get(who, "sys")
                prefix = {"YOU": "You   ", "JARVIS": "Jarvis  ", "ALERT": "⚠  "}.get(who, "▸ ")
                self.c_log.insert("end", f"{prefix}{text}\n", tag)
            self.c_log.configure(state="disabled")
            self.c_log.see("end")
        if time.time() - self.last_stats > 1.0:
            self.last_stats = time.time()
            self.c_clock.configure(text=time.strftime("%a %d %b  %H:%M:%S"))
            cpu, ram = psutil.cpu_percent(None), psutil.virtual_memory().percent
            bat = psutil.sensors_battery()
            stats = [("CPU", cpu, f"{cpu:.0f}%"), ("RAM", ram, f"{ram:.0f}%"),
                     ("PWR", bat.percent, f"{bat.percent:.0f}%{'+' if bat.power_plugged else ''}") if bat else ("PWR", 100, "AC")]
            for name, pct, text in stats:
                lbl, bar = self.c_gauges[name]
                lbl.configure(text=f"{name} {text}")
                bar.delete("all")
                bad = (pct > 90 and name != "PWR") or (pct < 15 and name == "PWR")
                bar.create_rectangle(0, 0, bar.winfo_width() * pct / 100, 20, fill=RED if bad else accent, outline="")
            if self.security:
                face = getattr(self.security.face, "status", "off") if self.security.face else "off"
                self.c_sec.configure(text=f"SECURITY  {len(self.security.monitors)} monitors active  ·  face guard: {face}",
                                     fg=RED if STATE.alerting else DIM)
            else:
                self.c_sec.configure(text="SECURITY  monitors off")


# ------------------------------------------------------------------ try the orb on its own
def demo(seconds: float = 0.0):
    """Show the orb by itself, walking through its states. No microphone, no AI: nothing is listening."""
    from .config import load_config
    cfg = load_config()
    box = {}
    talk = {"until": 0.0}
    orb = box["orb"] = Orb(cfg, on_text=lambda t: STATE.add("YOU", t),
                           on_talk=lambda: talk.update(until=time.time() + 4),
                           on_quit=lambda: box["orb"].close())
    boot = ["J.A.R.V.I.S. boot sequence initiated", "Voice synthesis ... ONLINE", "Speech recognition ... ONLINE",
            "Microphone and wake word ... ONLINE", "Security grid ... ACTIVE", "Language core ... loading",
            "Language core ... ONLINE"]
    script = [(4.0, "idle", None), (4.0, "listening", ("YOU", "Jarvis, run a full diagnostic.")),
              (3.0, "thinking", None),
              (6.0, "speaking", ("JARVIS", "All systems are operating within normal parameters, sir.")), (5.0, "idle", None)]
    cycle = sum(step[0] for step in script)
    start = time.time()
    told = {"boot": 0, "step": None}

    def drive():
        t = time.time() - start
        if seconds and t > seconds:
            return orb.close()
        if t < 4.2:
            STATE.booting = True
            while told["boot"] < len(boot) and t > told["boot"] * 0.6:
                STATE.boot(boot[told["boot"]])
                told["boot"] += 1
        else:
            STATE.booting = False
            u = (t - 4.2) % cycle
            for i, (length, mode, line) in enumerate(script):
                if u < length:
                    break
                u -= length
            if time.time() < talk["until"]:
                mode, line = "listening", None
            STATE.attentive, STATE.thinking, STATE.speaking = mode == "listening", mode == "thinking", mode == "speaking"
            wobble = abs(math.sin(t * 9.0)) * abs(math.sin(t * 2.3))
            STATE.mic_level = 0.15 + 0.6 * wobble if mode == "listening" else 0.03
            STATE.voice_level = 0.2 + 0.7 * wobble if mode == "speaking" else 0.0
            if line and told["step"] != (i, int((t - 4.2) // cycle)):
                told["step"] = (i, int((t - 4.2) // cycle))
                STATE.add(*line) if line[0] == "YOU" else STATE.caption(*line)
        orb.root.after(60, drive)

    orb.root.after(60, drive)
    orb.run()


if __name__ == "__main__":
    import sys
    demo(float(sys.argv[1]) if len(sys.argv) > 1 else 0.0)
