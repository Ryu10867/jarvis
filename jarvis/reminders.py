"""Persistent reminders, timers and alarms. Stored in data/reminders.json so they survive a restart."""
import datetime as dt
import json
import logging
import re
import threading
import time

from .config import ROOT

log = logging.getLogger("jarvis.reminders")
PATH = ROOT / "data" / "reminders.json"


def parse_when(text: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """'17:30', '5:30 pm', '5pm', 'tomorrow 9 am', 'in 20 minutes', 'in 2 hours' -> datetime."""
    now = now or dt.datetime.now()
    t = re.sub(r"(\d)\.(\d\d)\b", r"\1:\2", text.lower().strip())     # "5.30 pm" -> "5:30 pm"
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", "", t)                          # "p.m." -> "pm", keeps "2.5 hours"
    m = re.search(r"in\s+(\d+(?:\.\d+)?)\s*(second|sec|minute|min|hour|hr|day)s?", t)
    if m:
        n = float(m.group(1))
        unit = m.group(2)
        secs = n * {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400}[unit]
        return now + dt.timedelta(seconds=secs)
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a m|p m)?", t)
    if not m:
        return None
    hour, minute, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(" ", "")
    if ap == "pm" and hour < 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if "tomorrow" in t:
        when += dt.timedelta(days=1)
    elif when <= now:
        # "at 5" said at 3 pm means 5 pm today, otherwise the next morning.
        if not ap and hour < 12 and when + dt.timedelta(hours=12) > now:
            when += dt.timedelta(hours=12)
        else:
            when += dt.timedelta(days=1)
    return when


class Reminders:
    def __init__(self, on_fire):
        self.on_fire = on_fire            # callable(message)
        self.lock = threading.Lock()
        self.items: list[dict] = []
        try:
            self.items = json.loads(PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        threading.Thread(target=self._loop, daemon=True, name="reminders").start()

    def _save(self):
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(self.items, indent=1), encoding="utf-8")

    def add(self, when: dt.datetime, message: str) -> dict:
        with self.lock:
            item = {"id": int(time.time() * 1000) % 100000, "at": when.timestamp(), "message": message}
            self.items.append(item)
            self._save()
        return item

    def cancel(self, about: str) -> int:
        about = about.lower().strip()
        with self.lock:
            keep = [i for i in self.items if about not in ("all", "everything", "") and about not in i["message"].lower()]
            n = len(self.items) - len(keep)
            self.items = keep
            self._save()
        return n

    def pending(self) -> list[dict]:
        with self.lock:
            return sorted(self.items, key=lambda i: i["at"])

    def _loop(self):
        while True:
            time.sleep(1)
            now = time.time()
            with self.lock:
                due = [i for i in self.items if i["at"] <= now]
                if due:
                    self.items = [i for i in self.items if i["at"] > now]
                    self._save()
            for i in due:
                late = now - i["at"]
                msg = i["message"] if late < 120 else f"{i['message']} (this was due at {time.strftime('%I:%M %p', time.localtime(i['at']))})"
                try:
                    self.on_fire(msg)
                except Exception:
                    log.exception("reminder callback failed")


def describe(when: dt.datetime) -> str:
    now = dt.datetime.now()
    delta = (when - now).total_seconds()
    if delta < 90:
        return f"in {int(round(delta))} seconds"
    if delta < 3600:
        return f"in {int(round(delta / 60))} minutes"
    day = "today" if when.date() == now.date() else ("tomorrow" if when.date() == now.date() + dt.timedelta(days=1)
                                                     else when.strftime("on %A"))
    return f"at {when.strftime('%I:%M %p').lstrip('0')} {day}"
