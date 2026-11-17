"""Deadlines, calendar and Gmail: what is pending, what is due, and what is coming up soon.

Only the Python standard library is used (no Google client packages, no Google Cloud project):
  * Gmail     IMAP with an *app password* (Google Account > Security > 2-Step Verification > App passwords).
              Read-only: messages are opened with BODY.PEEK, so nothing gets marked as read.
  * Calendar  each calendar's "Secret address in iCal format" (Google Calendar > Settings > the calendar >
              Integrate calendar). Add the Google Classroom calendar too: that is where assignment due dates live.
The credentials are encrypted with Windows DPAPI (only this Windows account can read them) in data/google.bin.
`jarvis connect-google` (run_jarvis.bat connect-google) stores them.

Deadlines come from two places: calendar entries whose title reads like one ("Assignment 3", "DBMS lab record due",
anything on a Classroom calendar), and emails that name a due date ("Due Nov 20, 11:59 PM", "last date for
submission is 25/11/2026"). Jarvis reminds you 3 days, 1 day, 6 hours and 1 hour before each one, says when a new
one turns up, warns a few minutes before calendar events, and mentions new emails in the Primary inbox.
"""
import ctypes
import datetime as dt
import difflib
import email
import email.policy
import email.utils
import html
import imaplib
import json
import logging
import re
import subprocess
import sys
import threading
import time
import urllib.request

try:
    from ctypes import wintypes
    _DWORD = wintypes.DWORD
except Exception:
    _DWORD = ctypes.c_uint32

from .config import ROOT

log = logging.getLogger("jarvis.agenda")
SECRETS = ROOT / "data" / "google.bin"
STATE_FILE = ROOT / "data" / "agenda.json"

DEFAULT_QUERY = ("newer_than:30d -category:promotions -category:social {assignment assignments deadline due "
                 "submission submit quiz exam test project homework classroom viva lab report presentation}")
DEADLINE_WORDS = re.compile(r"\b(assignments?|homework|deadlines?|due|submissions?|submit|quiz(?:zes)?|exams?|"
                            r"tests?|projects?|viva|lab ?(?:record|report|work|manual)?s?|reports?|presentations?|"
                            r"case stud(?:y|ies)|worksheets?|essays?|thesis|dissertation|mid[- ]?sems?|end[- ]?sems?|"
                            r"internals?|hackathon|registration|last date)\b", re.I)
DUE_WORDS = re.compile(r"\b(due|deadline|last date|last day|submit|submission|turn(?:ed)? in|hand in|closes?|closing|"
                       r"no later than|on or before|expires?|ends?)\b", re.I)


# ===================================================================== credentials (Windows DPAPI)
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", _DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in, blob_out = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):   # 0x1: never show UI
        raise OSError(ctypes.FormatError())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


_KEYCHAIN = ["-a", "jarvis", "-s", "Jarvis Google connection"]      # macOS: kept in the login Keychain instead


def load_secrets() -> dict:
    if sys.platform == "darwin":
        r = subprocess.run(["security", "find-generic-password", *_KEYCHAIN, "-w"], capture_output=True, text=True)
        try:
            return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
        except ValueError:
            return {}
    try:
        return json.loads(_dpapi(SECRETS.read_bytes(), protect=False).decode("utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.error("could not read the saved Google connection: %s", e)
        return {}


def save_secrets(secrets: dict):
    if sys.platform == "darwin":
        subprocess.run(["security", "add-generic-password", *_KEYCHAIN, "-U", "-w", json.dumps(secrets)],
                       capture_output=True, check=True)
        return
    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    SECRETS.write_bytes(_dpapi(json.dumps(secrets).encode("utf-8"), protect=True))


def forget_secrets():
    if sys.platform == "darwin":
        subprocess.run(["security", "delete-generic-password", *_KEYCHAIN], capture_output=True)
    else:
        SECRETS.unlink(missing_ok=True)


# ===================================================================== dates in plain text
_MON = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
        r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
          "nov": 11, "dec": 12}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DATE_PATTERNS = [
    ("dmy", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}(?:,?\s*('?\d{{4}}|'\d{{2}}))?(?![a-z])", re.I)),
    ("mdy", re.compile(rf"\b{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?![\d:])(?:,?\s*(\d{{4}}))?", re.I)),
    ("iso", re.compile(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b")),
    ("num", re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](20\d\d|\d\d)\b")),
    ("rel", re.compile(r"\b(day after tomorrow|tomorrow|today|tonight)\b", re.I)),
    ("wd", re.compile(r"\b(?:(this|next|coming)\s+)?(mon|tues|wednes|thurs|fri|satur|sun)day\b", re.I)),
]
_TIME = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*([ap])\.?\s?m\b\.?|\b([01]?\d|2[0-3]):([0-5]\d)(?:\s*hrs?)?\b|"
                   r"\b(midnight|noon|end of (?:the )?day|eod)\b", re.I)


def _year_for(month: int, day: int, ref: dt.datetime) -> int:
    """A date written without a year means the next such date (counting a week back: 'due Nov 3' in a reminder sent
    on Nov 5 is still November this year)."""
    for y in (ref.year, ref.year + 1):
        try:
            if dt.date(y, month, day) >= ref.date() - dt.timedelta(days=7):
                return y
        except ValueError:
            return 0
    return ref.year + 1


def find_dates(text: str, ref: dt.datetime) -> list[tuple[dt.date, int, int]]:
    """Every date mentioned in `text`: [(date, start, end)], resolved against `ref` (when the email was sent)."""
    out = []
    for kind, pat in _DATE_PATTERNS:
        for m in pat.finditer(text):
            try:
                if kind in ("dmy", "mdy"):
                    d, mon, y = (m.group(1), m.group(2), m.group(3)) if kind == "dmy" else (m.group(2), m.group(1), m.group(3))
                    month, day = MONTHS[mon.lower()[:3]], int(d)
                    if y:
                        y = y.strip("'")
                        year = int(y) + (2000 if len(y) == 2 else 0)
                    else:
                        year = _year_for(month, day, ref)
                    date = dt.date(year, month, day)
                elif kind == "iso":
                    date = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                elif kind == "num":
                    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    y += 2000 if y < 100 else 0
                    day, month = (a, b) if b <= 12 else (b, a)            # dd/mm (India); mm/dd only if it must be
                    date = dt.date(y, month, day)
                elif kind == "rel":
                    w = m.group(1).lower()
                    date = ref.date() + dt.timedelta(days={"today": 0, "tonight": 0, "tomorrow": 1}.get(w, 2))
                else:
                    target = [w[:3] for w in WEEKDAYS].index(m.group(2).lower()[:3])
                    ahead = (target - ref.weekday()) % 7
                    if (m.group(1) or "").lower() == "next" and ahead == 0:
                        ahead = 7
                    date = ref.date() + dt.timedelta(days=ahead)
            except (ValueError, KeyError):
                continue
            if not any(s <= m.start() < e for _, s, e in out):           # "Nov 20" inside "20 Nov 2026" etc.
                out.append((date, m.start(), m.end()))
    return sorted(out, key=lambda x: x[1])


def _time_near(text: str, start: int, end: int) -> dt.time | None:
    """A clock time written right after (or just before) a date: 'Nov 20, 11:59 PM', 'by 5 pm on Friday'."""
    best = None
    for m in _TIME.finditer(text):
        gap = m.start() - end if m.start() >= end else start - m.end()
        if gap < 0 or gap > 28 or "\n" in text[min(m.start(), end):max(m.start(), end)]:
            continue
        if m.group(6):
            word = m.group(6).lower()
            t = dt.time(12, 0) if word == "noon" else dt.time(23, 59)
        elif m.group(3):
            h, mi = int(m.group(1)), int(m.group(2) or 0)
            if h > 12 or mi > 59:
                continue
            h = (h % 12) + (12 if m.group(3).lower() == "p" else 0)
            t = dt.time(h, mi)
        else:
            t = dt.time(int(m.group(4)), int(m.group(5)))
        if best is None or gap < best[0]:
            best = (gap, t)
    return best[1] if best else None


def extract_deadline(subject: str, body: str, sent: dt.datetime, now: dt.datetime | None = None
                     ) -> tuple[dt.datetime, bool] | None:
    """The due date an email announces, or None. Only dates written close to a word like 'due', 'deadline',
    'submit' or 'last date' count, so 'posted on Nov 3' or a footer date never becomes a deadline.
    Returns (when, all_day); an all-day deadline means the end of that day."""
    now = now or dt.datetime.now()
    if not DEADLINE_WORDS.search(subject + " " + body[:3000]):
        return None
    found = []
    for part in (subject, body[:6000]):
        for date, s, e in find_dates(part, sent):
            line_start = part.rfind("\n", 0, s) + 1
            before = part[max(line_start, s - 70):s]
            after = part[e:e + 25].split("\n")[0]
            strong = DUE_WORDS.search(before) or re.match(r"\W{0,3}(?:is )?(?:the )?(?:due|deadline)\b", after, re.I)
            by = re.search(r"\b(?:by|before|until|till)\s+(?:the\s+|this\s+|next\s+|coming\s+)?$|"
                           r"\bby\s+\d{1,2}(?:[:.]\d\d)?\s*[ap]\.?\s?m\.?\s+(?:on\s+)?$", before, re.I)
            if not (strong or by):
                continue
            if not (sent.date() - dt.timedelta(days=1) <= date <= sent.date() + dt.timedelta(days=150)):
                continue
            t = _time_near(part, s, e)
            when = dt.datetime.combine(date, t or dt.time(23, 59))
            found.append((when, t is None))
    if not found:
        return None
    upcoming = sorted(f for f in found if f[0] >= now - dt.timedelta(hours=1))
    return upcoming[0] if upcoming else max(found)


def clean_subject(subject: str) -> str:
    s = re.sub(r"^\s*((re|fwd?|fw|aw)\s*:\s*)+", "", subject or "", flags=re.I)
    m = re.match(r"\s*(?:(?:new|updated|reminder)\s+)?(?:assignment|material|question|quiz)s?\s*:\s*\"?(.+?)\"?\s*$|"
                 r"\s*(?:due|missing) (?:today|tomorrow|soon)\s*:\s*\"?(.+?)\"?\s*$", s, re.I)
    if m:
        s = m.group(1) or m.group(2)
    s = re.sub(r"^\s*(reminder|important|urgent|action required)\s*[:\-–]\s*", "", s, flags=re.I)
    s = s.strip(" \"'")
    return s if len(s) <= 80 else s[:77].rsplit(" ", 1)[0] + "..."


# ===================================================================== calendar (iCal)
_TZ_OFFSETS = {"asia/kolkata": 330, "asia/calcutta": 330, "utc": 0, "etc/utc": 0, "gmt": 0, "europe/london": 0,
               "asia/dubai": 240, "asia/singapore": 480, "america/new_york": -300, "america/los_angeles": -480}


def _ics_time(value: str, params: dict) -> tuple[dt.datetime, bool]:
    """iCal DTSTART/DTEND -> (local naive datetime, all_day)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return dt.datetime.strptime(value[:8], "%Y%m%d"), True
    naive = dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return naive.replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None), False
    off = _TZ_OFFSETS.get(params.get("TZID", "").lower())
    if off is not None:
        aware = naive.replace(tzinfo=dt.timezone(dt.timedelta(minutes=off)))
        return aware.astimezone().replace(tzinfo=None), False
    return naive, False                                   # unknown zone: assume it is local time


def _unfold(text: str) -> list[str]:
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def _occurrences(start: dt.datetime, rule: dict, lo: dt.datetime, hi: dt.datetime, exdates: set) -> list[dt.datetime]:
    """Expand the common recurrence rules (daily / weekly with BYDAY / monthly / yearly, INTERVAL, COUNT, UNTIL)."""
    freq = rule.get("FREQ", "")
    interval = max(1, int(rule.get("INTERVAL", 1) or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        try:
            until = _ics_time(rule["UNTIL"], {})[0]
            if len(rule["UNTIL"].strip()) == 8:
                until += dt.timedelta(days=1)
        except ValueError:
            pass
    days = [d[-2:] for d in rule.get("BYDAY", "").split(",") if d]
    codes = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
    out, n, i = [], 0, 0
    cur = start
    while i < 3000:
        i += 1
        if freq == "WEEKLY" and days:
            week0 = cur - dt.timedelta(days=cur.weekday())
            batch = sorted(week0 + dt.timedelta(days=codes.index(d)) for d in days if d in codes)
            batch = [b for b in batch if b >= start]
            cur_next = cur + dt.timedelta(weeks=interval)
        else:
            batch = [cur]
            if freq == "DAILY":
                cur_next = cur + dt.timedelta(days=interval)
            elif freq == "WEEKLY":
                cur_next = cur + dt.timedelta(weeks=interval)
            elif freq == "MONTHLY":
                mo = cur.month - 1 + interval
                try:
                    cur_next = cur.replace(year=cur.year + mo // 12, month=mo % 12 + 1)
                except ValueError:
                    cur_next = cur.replace(year=cur.year + mo // 12, month=mo % 12 + 1, day=28)
            elif freq == "YEARLY":
                cur_next = cur.replace(year=cur.year + interval)
            else:
                return [start] if lo <= start <= hi else []
        for occ in batch:
            if (until and occ > until) or (count is not None and n >= count) or occ > hi:
                return out
            n += 1
            if occ >= lo and occ not in exdates:
                out.append(occ)
        cur = cur_next
    return out


def parse_ics(text: str, lo: dt.datetime, hi: dt.datetime) -> tuple[str, list[dict]]:
    """-> (calendar name, [{uid, title, start, end, all_day}]) for events overlapping [lo, hi]."""
    name, events, ev = "", [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            ev = {"ex": set()}
            continue
        if line == "END:VEVENT":
            if ev is not None:
                events.append(ev)
            ev = None
            continue
        key, _, value = line.partition(":")
        key, *pp = key.split(";")
        params = dict(p.split("=", 1) for p in pp if "=" in p)
        if ev is None:
            if key == "X-WR-CALNAME":
                name = _unescape(value)
            continue
        try:
            if key in ("DTSTART", "DTEND", "RECURRENCE-ID"):
                ev[key] = _ics_time(value, params)
            elif key == "EXDATE":
                for v in value.split(","):
                    ev["ex"].add(_ics_time(v, params)[0])
            elif key == "RRULE":
                ev["RRULE"] = dict(p.split("=", 1) for p in value.split(";") if "=" in p)
            elif key in ("SUMMARY", "UID", "STATUS", "DESCRIPTION"):
                ev[key] = _unescape(value)
        except ValueError:
            continue
    overridden = {(e.get("UID"), e["RECURRENCE-ID"][0]) for e in events if "RECURRENCE-ID" in e}
    out = []
    for e in events:
        if "DTSTART" not in e or e.get("STATUS", "").upper() == "CANCELLED":
            continue
        start, all_day = e["DTSTART"]
        end = e.get("DTEND", (start + (dt.timedelta(days=1) if all_day else dt.timedelta(0)), all_day))[0]
        length = end - start
        if "RRULE" in e and "RECURRENCE-ID" not in e:
            starts = [s for s in _occurrences(start, e["RRULE"], lo - length, hi, e["ex"])
                      if (e.get("UID"), s) not in overridden]
        else:
            starts = [start] if start + length >= lo and start <= hi else []
        for s in starts:
            out.append({"uid": f"{e.get('UID', '')}@{s:%Y%m%dT%H%M}", "title": e.get("SUMMARY") or "(untitled)",
                        "start": s, "end": s + length, "all_day": all_day,
                        "desc": (e.get("DESCRIPTION") or "")[:400]})
    return name, out


# ===================================================================== speech helpers
def _clock(t: dt.datetime) -> str:
    s = t.strftime("%I:%M %p").lstrip("0")
    return s.replace(":00 ", " ")


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def when_text(when: dt.datetime, all_day: bool = False, now: dt.datetime | None = None) -> str:
    """'in 40 minutes', 'today at 11:59 PM', 'tomorrow', 'on Friday at 5 PM', 'on Monday the 1st of December'."""
    now = now or dt.datetime.now()
    left = (when - now).total_seconds()
    if not all_day and 0 <= left < 5400:
        mins = max(1, int(round(left / 60)))
        return f"in {mins} minute{'s' if mins != 1 else ''}" if mins < 55 else "in about an hour"
    days = (when.date() - now.date()).days
    at = "" if all_day else f" at {_clock(when)}"
    if days == 0:
        return f"today{at}"
    if days == 1:
        return f"tomorrow{at}"
    if days == -1:
        return f"yesterday{at}"
    if 1 < days < 7:
        return f"on {when:%A}{at}"
    return f"on {when:%A} the {_ordinal(when.day)} of {when:%B}{at}"


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _norm(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", title.lower()))


# ===================================================================== the tracker
class Agenda:
    STAGES = (72, 24, 6, 1)               # hours before a deadline at which Jarvis speaks up (config: deadline_alerts)

    def __init__(self, cfg: dict, announce=None):
        """`announce(text, urgent)` is how Jarvis says something unprompted (the assistant decides whether to speak
        it aloud or only show it, e.g. during quiet hours)."""
        self.cfg = cfg.get("agenda", {}) or {}
        self.announce = announce or (lambda text, urgent=False: None)
        self.stages = sorted({float(h) for h in self.cfg.get("deadline_alerts", self.STAGES)}, reverse=True)
        self.lock = threading.RLock()
        self.sync_lock = threading.Lock()
        self.items: list[dict] = []        # merged deadlines and events (epoch seconds)
        self.unread: list[dict] = []       # recent unread Primary emails: {uid, sender, subject, at}
        self.errors: dict[str, str] = {}
        self.synced_at = 0.0
        self.running = False
        try:
            self.state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            self.state = {}
        for key, empty in (("done", []), ("fired", {}), ("known", []), ("mail", {}), ("seen_unread", [])):
            self.state.setdefault(key, empty)

    # ---- persistence
    def _save(self):
        with self.lock:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            # keep the file small: forget what is long past
            self.state["seen_unread"] = self.state["seen_unread"][-300:]
            self.state["known"] = self.state["known"][-1000:]
            self.state["done"] = self.state["done"][-500:]
            live = {i["id"] for i in self.items}
            self.state["fired"] = {k: v for k, v in self.state["fired"].items() if k in live}
            STATE_FILE.write_text(json.dumps(self.state, indent=1), encoding="utf-8")

    # ---- configuration
    @staticmethod
    def secrets() -> dict:
        return load_secrets()

    def connected(self) -> bool:
        s = self.secrets()
        return bool((s.get("email") and s.get("app_password")) or s.get("calendars"))

    # ---- running
    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._loop, daemon=True, name="agenda").start()

    def _loop(self):
        time.sleep(1)
        every = max(3, int(self.cfg.get("refresh_minutes", 15))) * 60
        last = 0.0
        while self.running:
            if time.time() - last >= every:
                last = time.time()
                if self.connected():
                    self.sync()
            try:
                self.check_alerts()
            except Exception:
                log.exception("agenda alert check failed")
            time.sleep(30)

    def sync(self) -> bool:
        """Fetch calendars and Gmail. Safe to call from any thread; concurrent calls wait for the one running."""
        if not self.sync_lock.acquire(blocking=False):
            with self.sync_lock:                         # someone else is syncing: wait for their result
                return not self.errors
        try:
            s = self.secrets()
            first = not self.synced_at
            items, errors = [], {}
            horizon = int(self.cfg.get("horizon_days", 14))
            for cal in s.get("calendars", []):
                try:
                    items += self._calendar(cal, horizon)
                except Exception as e:
                    errors[f"calendar {cal.get('name', '')}".strip()] = str(e)
                    log.warning("calendar %s failed: %s", cal.get("name"), e)
            unread = self.unread
            if s.get("email") and s.get("app_password"):
                try:
                    mail_items, unread = self._gmail(s["email"], s["app_password"], announce=not first)
                    items += mail_items
                except Exception as e:
                    errors["gmail"] = str(e)
                    log.warning("gmail failed: %s", e)
            items = self._dedupe(items)
            with self.lock:
                self.items, self.unread, self.errors = items, unread, errors
                self.synced_at = time.time()
                new = [i for i in items if i["kind"] == "deadline" and i["id"] not in self.state["known"]
                       and i["due"] > time.time() and i["id"] not in self.state["done"]]
                self.state["known"] += [i["id"] for i in new]
                if first and not self.state.get("initialised"):
                    self.state["initialised"] = True     # the very first sync ever: nothing is "new" yet
                    new = []
                for i in new:                            # announcing it covers the reminder stages already passed
                    self._mark_stages(i)
                self._save()
            log.info("agenda synced: %d deadlines, %d events, %d unread%s", sum(i["kind"] == "deadline" for i in items),
                     sum(i["kind"] == "event" for i in items), len(unread), f", errors {errors}" if errors else "")
            if new and self.cfg.get("announce_new_deadlines", True):
                parts = [f"{i['title']}, due {when_text(dt.datetime.fromtimestamp(i['due']), i['all_day'])}" for i in new[:3]]
                more = f", and {len(new) - 3} more" if len(new) > 3 else ""
                self.announce(("Sir, a new deadline has come in: " if len(new) == 1 else
                               f"Sir, {len(new)} new deadlines have come in: ") + _join(parts) + more + ".", False)
            return not errors
        finally:
            self.sync_lock.release()

    # ---- sources
    def _calendar(self, cal: dict, horizon: int) -> list[dict]:
        req = urllib.request.Request(cal["url"], headers={"User-Agent": "Jarvis-Assistant/1.0"})
        with urllib.request.urlopen(req, timeout=40) as r:
            text = r.read().decode("utf-8", "replace")
        now = dt.datetime.now()
        name, events = parse_ics(text, now - dt.timedelta(days=1), now + dt.timedelta(days=horizon))
        label = cal.get("name") or name or "Calendar"
        # A Classroom calendar is named after the class, so the user says so when connecting it ("deadlines": true).
        classroom = bool(cal.get("deadlines")) or bool(re.search(r"classroom|assignments?|deadlines?", label, re.I))
        out = []
        for e in events:
            start = e["start"]
            if classroom or DEADLINE_WORDS.search(e["title"]):
                due = start.replace(hour=23, minute=59) if e["all_day"] else start    # Classroom: entry starts at the due time
                out.append({"id": "cal:" + e["uid"], "title": e["title"], "due": due.timestamp(), "all_day": e["all_day"],
                            "kind": "deadline", "source": label})
            else:
                out.append({"id": "cal:" + e["uid"], "title": e["title"], "due": start.timestamp(),
                            "end": e["end"].timestamp(), "all_day": e["all_day"], "kind": "event", "source": label})
        return out

    def _gmail(self, address: str, password: str, announce: bool) -> tuple[list[dict], list[dict]]:
        query = self.cfg.get("gmail_query") or DEFAULT_QUERY
        max_new = int(self.cfg.get("max_emails_per_sync", 40))
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=40)
        try:
            imap.login(address, password.replace(" ", ""))
            imap.select("INBOX", readonly=True)
            validity = (imap.response("UIDVALIDITY")[1] or [b"0"])[0] or b"0"
            validity = validity.decode() if isinstance(validity, bytes) else str(validity)

            # --- emails that may carry a deadline (Gmail's own search does the keyword filtering)
            uids = self._search(imap, query)
            keys = {f"{validity}:{u}" for u in uids}
            cache = self.state["mail"] = {k: v for k, v in self.state["mail"].items() if k in keys}   # 30-day window
            todo = [u for u in uids if f"{validity}:{u}" not in cache][-max_new:]
            for uid, msg in self._fetch(imap, todo, "BODY.PEEK[]<0.40000>"):
                cache[f"{validity}:{uid}"] = self._mail_deadline(msg)
            items = []
            for u in uids:
                d = cache.get(f"{validity}:{u}")
                if d:
                    items.append({"id": f"mail:{validity}:{u}", "kind": "deadline", **d})

            # --- recent unread mail in the Primary tab
            unread_uids = self._search(imap, "category:primary is:unread newer_than:3d")[-25:]
            heads = {u: m for u, m in self._fetch(imap, unread_uids, "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)]")}
            unread = []
            for u in reversed(unread_uids):
                m = heads.get(u)
                if m is None:
                    continue
                unread.append({"uid": f"{validity}:{u}", "sender": _sender(m.get("From", "")),
                               "subject": clean_subject(str(m.get("Subject", "") or "(no subject)")),
                               "at": _sent(m).timestamp()})
            fresh = [m for m in unread if m["uid"] not in self.state["seen_unread"]]
            self.state["seen_unread"] += [m["uid"] for m in fresh]
            if announce and fresh and self.cfg.get("announce_new_mail", True):
                if len(fresh) == 1:
                    self.announce(f"Sir, a new email from {fresh[0]['sender']}: {fresh[0]['subject']}.", False)
                else:
                    who = _join(sorted({m["sender"] for m in fresh[:4]}))
                    self.announce(f"Sir, {len(fresh)} new emails, from {who}.", False)
            return items, unread
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    @staticmethod
    def _search(imap, query: str) -> list[str]:
        q = '"' + query.replace("\\", "\\\\").replace('"', '\\"') + '"'
        typ, data = imap.uid("SEARCH", "X-GM-RAW", q)
        if typ != "OK":
            raise RuntimeError(f"Gmail search failed: {data}")
        return [u.decode() for u in (data[0] or b"").split()]

    @staticmethod
    def _fetch(imap, uids: list[str], what: str):
        for i in range(0, len(uids), 15):
            chunk = uids[i:i + 15]
            if not chunk:
                continue
            typ, data = imap.uid("FETCH", ",".join(chunk), f"(UID {what})")
            if typ != "OK":
                continue
            pending = None                              # a message whose UID the server sent after the body
            for part in data:
                head, raw = (part[0], part[1]) if isinstance(part, tuple) else (part or b"", None)
                m = re.search(rb"UID (\d+)", head)
                if raw is not None:
                    msg = email.message_from_bytes(raw, policy=email.policy.default)
                    if m:
                        yield m.group(1).decode(), msg
                    else:
                        pending = msg
                elif m and pending is not None:
                    yield m.group(1).decode(), pending
                    pending = None

    @staticmethod
    def _mail_deadline(msg) -> dict | None:
        subject = str(msg.get("Subject", "") or "")
        sent = _sent(msg)
        body = _body_text(msg)
        found = extract_deadline(subject, body, sent)
        if not found:
            return None
        when, all_day = found
        return {"title": clean_subject(subject) or "an email deadline", "due": when.timestamp(), "all_day": all_day,
                "source": "Gmail, " + _sender(msg.get("From", ""))}

    @staticmethod
    def _dedupe(items: list[dict]) -> list[dict]:
        """Classroom sends both a calendar entry and an email for the same assignment: keep one (the calendar's)."""
        items = sorted(items, key=lambda i: (not i["id"].startswith("cal:"), i["due"]))
        out = []
        for it in items:
            n = _norm(it["title"])
            day = dt.date.fromtimestamp(it["due"])
            if any(o["kind"] == it["kind"] == "deadline" and dt.date.fromtimestamp(o["due"]) == day and
                   (n in _norm(o["title"]) or _norm(o["title"]) in n or
                    difflib.SequenceMatcher(None, n, _norm(o["title"])).ratio() >= 0.6) for o in out):
                continue
            out.append(it)
        return sorted(out, key=lambda i: i["due"])

    # ---- reminders
    def _mark_stages(self, item: dict):
        left = (item["due"] - time.time()) / 3600
        fired = set(self.state["fired"].get(item["id"], []))
        fired |= {str(h) for h in self.stages if left <= h}
        self.state["fired"][item["id"]] = sorted(fired)

    def check_alerts(self, now: float | None = None):
        now = now or time.time()
        due_msgs, event_msgs, urgent = [], [], False
        lead = float(self.cfg.get("event_lead_minutes", 10)) * 60
        with self.lock:
            for it in self.items:
                if it["id"] in self.state["done"]:
                    continue
                left = it["due"] - now
                fired = set(self.state["fired"].get(it["id"], []))
                if it["kind"] == "deadline":
                    if left <= 0:
                        continue
                    passed = [str(h) for h in self.stages if left <= h * 3600]
                    if not passed or passed[-1] in fired:
                        continue
                    self.state["fired"][it["id"]] = sorted(fired | set(passed))
                    when = when_text(dt.datetime.fromtimestamp(it["due"]), it["all_day"],
                                     dt.datetime.fromtimestamp(now))
                    due_msgs.append(f"{it['title']} is due {when}")
                    urgent = urgent or left <= 6 * 3600
                elif lead and not it["all_day"] and 0 < left <= lead and "start" not in fired:
                    self.state["fired"][it["id"]] = sorted(fired | {"start"})
                    mins = max(1, int(round(left / 60)))
                    event_msgs.append(f"{it['title']} starts in {mins} minute{'s' if mins != 1 else ''}")
            if due_msgs or event_msgs:
                self._save()
        if due_msgs:
            text = ("Sir, a reminder: " + due_msgs[0] + "." if len(due_msgs) == 1 else
                    f"Sir, {len(due_msgs)} deadlines are close: " + _join(due_msgs) + ".")
            self.announce(text, urgent)
        if event_msgs:
            self.announce("Sir, " + _join(event_msgs) + ".", False)

    # ---- questions
    def _not_ready(self) -> str | None:
        if not self.connected():
            return ("I'm not connected to your Gmail or calendar yet, sir. Run jarvis connect-google once, "
                    "and I'll keep an eye on your deadlines from then on.")
        if not self.synced_at:
            self.sync()
        if not self.items and not self.unread and self.errors:
            what = ", ".join(self.errors)
            return f"I couldn't reach {what}, sir. {next(iter(self.errors.values()))[:120]}"
        return None

    def deadlines(self, days: int = 14) -> list[dict]:
        now = time.time()
        with self.lock:
            return [i for i in self.items if i["kind"] == "deadline" and i["id"] not in self.state["done"]
                    and now - 3600 < i["due"] <= now + days * 86400]

    def deadlines_text(self, days: int = 14) -> str:
        msg = self._not_ready()
        if msg:
            return msg
        items = self.deadlines(days)
        span = "two weeks" if days == 14 else "week" if days == 7 else f"{days} days"
        if not items:
            return f"Nothing is due in the next {span}, sir."
        parts = [f"{i['title']} {'is due ' if k == 0 else ''}{when_text(dt.datetime.fromtimestamp(i['due']), i['all_day'])}"
                 for k, i in enumerate(items[:5])]
        if len(items) == 1:
            return f"One thing is due, sir: {parts[0]}."
        more = f", and {len(items) - 5} more after that" if len(items) > 5 else ""
        listed = ", ".join(parts[:-1]) + (", and " if len(parts) > 2 else " and ") + parts[-1]
        return f"You have {len(items)} deadlines coming up, sir: " + listed + more + "."

    def day_text(self, offset: int = 0) -> str:
        msg = self._not_ready()
        if msg:
            return msg
        day = dt.date.today() + dt.timedelta(days=offset)
        label = "today" if offset == 0 else "tomorrow" if offset == 1 else f"on {day:%A}"
        with self.lock:
            evs = [i for i in self.items if i["kind"] == "event" and i["id"] not in self.state["done"]
                   and dt.date.fromtimestamp(i["due"]) <= day
                   <= dt.date.fromtimestamp(max(i["due"], i.get("end", i["due"]) - 1))]
            dues = [i for i in self.items if i["kind"] == "deadline" and i["id"] not in self.state["done"]
                    and dt.date.fromtimestamp(i["due"]) == day]
        if offset == 0:
            evs = [e for e in evs if e["all_day"] or e.get("end", e["due"]) > time.time()]
        parts = []
        if evs:
            names = [e["title"] if e["all_day"] else f"{e['title']} at {_clock(dt.datetime.fromtimestamp(e['due']))}"
                     for e in evs[:6]]
            parts.append(f"{label.capitalize()} you have {_join(names)}.")
        if dues:
            parts.append(("Also due " if evs else f"Due {label}: ") + _join(
                [d["title"] + ("" if d["all_day"] else f" at {_clock(dt.datetime.fromtimestamp(d['due']))}")
                 for d in dues]) + ".")
        if not parts:
            rest = "nothing left" if offset == 0 else "nothing"
            return f"Your calendar shows {rest} {label}, sir."
        return " ".join(parts)

    def mail_text(self) -> str:
        msg = self._not_ready()
        if msg:
            return msg
        if "gmail" in self.errors and not self.unread:
            return f"I couldn't check Gmail, sir: {self.errors['gmail'][:120]}"
        with self.lock:
            unread = list(self.unread)
        if not unread:
            return "No unread emails in your Primary inbox from the last three days, sir."
        parts = [f"{m['sender']} about {m['subject']}" for m in unread[:3]]
        n = len(unread)
        head = (f"You have one unread email, sir, from " if n == 1 else
                f"You have {n} unread emails from the last three days, sir. The latest: ")
        return head + _join(parts) + "."

    def overview(self) -> str:
        """Compact plain text for the language model."""
        msg = self._not_ready()
        if msg:
            return msg
        lines = ["Deadlines: " + ("; ".join(
            f"{i['title']} due {when_text(dt.datetime.fromtimestamp(i['due']), i['all_day'])} ({i['source']})"
            for i in self.deadlines(14)[:8]) or "none in the next 14 days")]
        lines.append("Today: " + self.day_text(0))
        lines.append("Unread email: " + ("; ".join(f"{m['sender']}: {m['subject']}" for m in self.unread[:5]) or "none"))
        return "\n".join(lines)

    def briefing(self) -> str:
        """One or two sentences for the start-up greeting ('' when there is nothing worth saying)."""
        if not self.connected() or not self.synced_at:
            return ""
        soon = self.deadlines(3)
        parts = []
        if soon:
            with self.lock:                              # said in the greeting: don't repeat it as an alert right after
                for i in soon:
                    self._mark_stages(i)
                self._save()
            first = soon[0]
            when = when_text(dt.datetime.fromtimestamp(first["due"]), first["all_day"])
            parts.append(f"{first['title']} is due {when}" + (
                f", and {len(soon) - 1} more deadline{'s' if len(soon) > 2 else ''} within three days" if len(soon) > 1 else ""))
        now = time.time()
        with self.lock:
            today = [i for i in self.items if i["kind"] == "event" and not i["all_day"]
                     and now < i["due"] and dt.date.fromtimestamp(i["due"]) == dt.date.today()]
        if today:
            parts.append(f"your next event is {today[0]['title']} at {_clock(dt.datetime.fromtimestamp(today[0]['due']))}")
        if not parts:
            return ""
        s = _join(parts) + "."
        return s[0].upper() + s[1:]

    def find(self, about: str) -> dict | None:
        """The open deadline whose title best matches what was said ('the dbms lab' -> 'DBMS Lab 5')."""
        about = re.sub(r"\b(the|my|a|an|assignment|homework|task)\b", " ", _norm(about)).split()
        if not about:
            return None
        key = " ".join(about)
        best, score = None, 0.0
        with self.lock:
            for i in self.items:
                if i["kind"] != "deadline" or i["id"] in self.state["done"]:
                    continue
                n = _norm(i["title"])
                s = 1.0 if key in n else difflib.SequenceMatcher(None, key, n).ratio()
                s = max(s, len(set(about) & set(n.split())) / len(set(about)) * 0.9)
                if s > score:
                    best, score = i, s
        return best if score >= 0.5 else None

    def mark_done(self, about: str) -> str:
        best = self.find(about)
        if not best:
            return f"I couldn't find a deadline matching {about}, sir." if about.strip() else "Which one, sir?"
        with self.lock:
            self.state["done"].append(best["id"])
            self._save()
        return f"Marked {best['title']} as done, sir. No more reminders about it."


def _sender(value) -> str:
    name, addr = email.utils.parseaddr(str(value or ""))
    name = re.sub(r"\s*\(.*?\)\s*", " ", name).strip(" \"'")
    if not name:
        name = addr.split("@")[0]
    return re.sub(r"\s+via\s+.*$", "", name).strip() or "someone"


def _sent(msg) -> dt.datetime:
    try:
        return email.utils.parsedate_to_datetime(str(msg.get("Date"))).astimezone().replace(tzinfo=None)
    except Exception:
        return dt.datetime.now()


def _body_text(msg) -> str:
    """Plain text of an email (HTML stripped), tolerant of the message being cut short by the partial fetch."""
    plain, rich = [], []
    try:
        parts = list(msg.walk())
    except Exception:
        parts = [msg]
    for part in parts:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html") or part.get_content_disposition() == "attachment":
            continue
        try:
            text = part.get_content()
        except Exception:
            try:
                text = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
        (plain if ctype == "text/plain" else rich).append(str(text))
    if plain:
        return "\n".join(plain)[:20000]
    raw = "\n".join(rich)
    raw = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", raw)
    raw = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"[ \t\xa0]+", " ", raw)[:20000]


# ===================================================================== `jarvis connect-google`
def connect_cli() -> int:
    import getpass
    print(__doc__.split("\n\n")[0])
    print("""
GMAIL (optional): Jarvis signs in with an *app password*, never your real password.
  1. Turn on 2-Step Verification:  https://myaccount.google.com/security
  2. Create an app password:       https://myaccount.google.com/apppasswords   (name it "Jarvis")
  3. Paste the 16-letter password below. IMAP must be on (Gmail > Settings > Forwarding and POP/IMAP).

CALENDAR (optional): for each calendar, Google Calendar > Settings > (the calendar) > Integrate calendar >
  "Secret address in iCal format". Add your main calendar AND the Classroom calendar (assignment due dates).
  Keep these links private: anyone who has one can read that calendar.
""")
    old = load_secrets()
    if old:
        cals = ", ".join(c.get("name", "?") for c in old.get("calendars", [])) or "none"
        print(f"Currently connected: Gmail = {old.get('email') or 'no'}; calendars = {cals}")
        if input("Remove the saved connection completely? [y/N] ").strip().lower() == "y":
            forget_secrets()
            print("Removed.")
            return 0
    secrets = dict(old)
    addr = input(f"Gmail address [{old.get('email', 'skip')}]: ").strip()
    if addr:
        pw = getpass.getpass("App password (input hidden): ").replace(" ", "").strip()
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
            imap.login(addr, pw)
            imap.logout()
            print("  Gmail: signed in OK.")
            secrets.update(email=addr, app_password=pw)
        except Exception as e:
            print(f"  Gmail sign-in failed: {e}\n  (Use an app password, not your normal one. Not saved.)")
    cals = list(old.get("calendars", []))
    if cals and input("Keep the calendars already saved? [Y/n] ").strip().lower() == "n":
        cals = []
    while True:
        url = input("Secret iCal address of a calendar (Enter when done): ").strip().strip('"')
        if not url:
            break
        if url.startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Jarvis-Assistant/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                text = r.read().decode("utf-8", "replace")
            now = dt.datetime.now()
            name, evs = parse_ics(text, now - dt.timedelta(days=1), now + dt.timedelta(days=14))
            label = input(f"  Found '{name or 'calendar'}' ({len(evs)} events in the next two weeks). "
                          f"Name it [{name or 'Calendar'}]: ").strip() or name or "Calendar"
            work = input("  Is this a Classroom / coursework calendar, where every entry is a deadline? [y/N] ")
            cals.append({"name": label, "url": url, "deadlines": work.strip().lower().startswith("y")})
        except Exception as e:
            print(f"  Could not read that calendar: {e}")
    secrets["calendars"] = cals
    if not (secrets.get("email") or secrets.get("calendars")):
        print("Nothing to save.")
        return 1
    save_secrets(secrets)
    print("\nSaved in your login Keychain." if sys.platform == "darwin" else
          f"\nSaved (encrypted for this Windows account) in {SECRETS}.")
    print("Restart Jarvis, then ask: \"what's due?\", \"what's on my calendar today?\", \"check my email\".")
    return 0
