"""Watches the webcam: verifies the owner, photographs + locks out unknown faces, warns about shoulder-surfers.

Fixes over the first build:
  * The webcam sometimes delivers frames upside down (every "intruder" photo was the owner, inverted, scoring
    ~0.1 instead of ~0.7). Each frame is now checked in both orientations and the working one is remembered.
  * No checks while the screen is already locked (it used to re-lock every minute and pile up photos).
  * Welcomes the owner back after an absence.
"""
import ctypes
import logging
import threading
import time

import cv2

from ..config import resolve
from .face_engine import FaceEngine

log = logging.getLogger("jarvis.face")


def screen_locked() -> bool:
    """True while the Windows lock screen / secure desktop is showing."""
    u = ctypes.windll.user32
    h = u.OpenInputDesktop(0, False, 0x0100)        # DESKTOP_SWITCHDESKTOP
    if not h:
        return True
    ok = u.SwitchDesktop(h)
    u.CloseDesktop(h)
    return not ok


class FaceGuard:
    def __init__(self, cfg: dict, alerts):
        c = cfg["security"]["face_guard"]
        self.cfg, self.alerts = c, alerts
        self.engine = FaceEngine()
        self.enrolled = self.engine.owner is not None
        self.intruder_dir = resolve(c["intruder_dir"])
        self.intruder_dir.mkdir(parents=True, exist_ok=True)
        self.cam = None
        self.cam_lock = threading.Lock()
        self.last_owner_seen = 0.0
        self.last_unknown_seen = 0.0
        self.last_face_seen = time.time()
        self.strikes = 0
        self.last_lock = 0.0
        self.last_shoulder = 0.0
        rot = str(c.get("camera_rotation", "auto")).lower()
        self.auto_rotate = rot == "auto"
        self.flipped = rot == "180"
        self.on_owner_return = None          # callable(seconds_away), set by main
        self.status = "starting"

    # ---- camera helpers
    def _open(self):
        if self.cam is None or not self.cam.isOpened():
            self.cam = cv2.VideoCapture(self.cfg["camera_index"], cv2.CAP_DSHOW)
        return self.cam.isOpened()

    def release(self):
        with self.cam_lock:
            if self.cam is not None:
                self.cam.release()
                self.cam = None

    def grab(self):
        """One frame, already turned the right way up."""
        with self.cam_lock:
            if not self._open():
                return None
            for _ in range(2):          # flush a stale buffered frame
                self.cam.grab()
            ok, frame = self.cam.read()
            if not ok:
                self.cam.release()
                self.cam = None
                return None
        return cv2.rotate(frame, cv2.ROTATE_180) if self.flipped else frame

    def save_photo(self, reason: str, frame):
        """Write the frame as a JPEG and return its path, or None if it really could not be saved.
        cv2.imwrite fails silently (returns False) on paths with non-ASCII characters, so encode in memory and
        write the bytes ourselves, then check the file is actually there."""
        if frame is None:
            return None
        try:
            self.intruder_dir.mkdir(parents=True, exist_ok=True)
            path = self.intruder_dir / f"{reason}_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
            ok, jpg = cv2.imencode(".jpg", frame)
            if not ok:
                raise ValueError("JPEG encoding failed")
            path.write_bytes(jpg.tobytes())
            if path.stat().st_size > 0:
                log.info("photo saved: %s", path)
                return path
        except Exception as e:
            log.error("could not save the %s photo in %s: %s", reason, self.intruder_dir, e)
        return None

    def snapshot(self, reason: str):
        return self.save_photo(reason, self.grab())

    def photo_note(self, path) -> str:
        """What to tell the owner about the photo: where it is, or plainly that it failed."""
        if path is None:
            return "I could not save a photo."
        return f"Photo saved in {self.intruder_dir.name}."

    def _scores(self, frame):
        return [self.engine.best_match(self.engine.embed(frame, f)) for f in self.engine.detect(frame)]

    def analyse(self, frame):
        """Match scores for every face in the frame. If the owner isn't found, try the frame the other way up;
        when that works the camera orientation is remembered."""
        scores = self._scores(frame)
        th = self.cfg["match_threshold"]
        if self.auto_rotate and not any(s >= th for s in scores):
            turned = cv2.rotate(frame, cv2.ROTATE_180)
            alt = self._scores(turned)
            if any(s >= th for s in alt):
                self.flipped = not self.flipped
                log.info("camera image is upside down; orientation corrected (flipped=%s)", self.flipped)
                return turned, alt
        return frame, scores

    # ---- state for the voice layer
    def owner_verified(self):
        """True = owner seen recently, False = a stranger is at the keyboard, None = unknown."""
        if not self.enrolled:
            return None
        now = time.time()
        if now - self.last_owner_seen < 60:
            return True
        if self.strikes > 0 and now - self.last_unknown_seen < 30:
            return False
        return None

    # ---- main loop
    def run(self):
        if not self.enrolled:
            self.status = "not enrolled (run enroll_face.py)"
            self.alerts.raise_alert("face_guard", "low", "Face guard is idle: no owner face enrolled yet.")
            return
        log.info("Face guard active")
        th = self.cfg["match_threshold"]
        while True:
            time.sleep(self.cfg["check_interval"])
            if screen_locked():
                self.status = "paused (screen locked)"
                self.strikes = 0
                self.release()              # free the camera (and its LED) while locked
                time.sleep(3)
                continue
            frame = self.grab()
            if frame is None:
                self.status = "camera unavailable"
                time.sleep(10)
                continue
            frame, scores = self.analyse(frame)
            now = time.time()
            if not scores:
                self.status = "no face in view"
                continue
            owner_here = any(s >= th for s in scores)
            strangers = sum(s < th for s in scores)
            away = now - self.last_face_seen
            self.last_face_seen = now

            if owner_here:
                if self.on_owner_return and self.last_owner_seen and away > self.cfg.get("welcome_back_after", 600):
                    try:
                        self.on_owner_return(away)
                    except Exception:
                        log.exception("welcome-back callback failed")
                self.last_owner_seen = now
                self.strikes = 0
                self.status = "owner verified"
                if strangers and now - self.last_shoulder > 120:
                    self.last_shoulder = now
                    p = self.snapshot("shoulder_surfer")
                    self.alerts.raise_alert("face_guard", "medium",
                                            "Someone else is looking at your screen. " + self.photo_note(p),
                                            {"photo": str(p) if p else None})
                continue

            self.strikes += 1
            self.last_unknown_seen = now
            self.status = f"unknown face ({self.strikes}/{self.cfg['strikes_to_lock']})"
            log.info("unknown face, best score %.3f", max(scores))
            if self.strikes >= self.cfg["strikes_to_lock"] and now - self.last_lock > 60:
                self.last_lock = now
                self.strikes = 0
                path = self.save_photo("intruder", frame)
                locking = self.cfg.get("lock_on_intruder", True)
                self.alerts.raise_alert("face_guard", "critical",
                                        "Unknown person at the laptop. " + self.photo_note(path) +
                                        (" Screen locked." if locking else ""),
                                        {"photo": str(path) if path else None, "folder": str(self.intruder_dir),
                                         "score": max(scores)})
                if locking:
                    ctypes.windll.user32.LockWorkStation()
