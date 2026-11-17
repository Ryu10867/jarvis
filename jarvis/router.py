"""Reflexes: very common commands are recognised with plain patterns and executed instantly, without waiting
2-4 seconds for the language model. Anything not matched here goes to the LLM brain."""
import datetime as dt
import random
import re

from . import tools
from .reminders import describe, parse_when
from .tools import Ctx

ACK = ["Right away, sir.", "Of course, sir.", "Certainly, sir.", "As you wish, sir.", "Consider it done.", "Very good, sir."]
POLITE = re.compile(r"^(?:(?:hey |ok |okay )?jarvis[,.!]?\s+|please\s+|can you\s+|could you\s+|would you\s+|will you\s+|"
                    r"kindly\s+|i want you to\s+|i need you to\s+|go ahead and\s+)+", re.I)
TRAIL = re.compile(r"(?:[,.!?]|\s+please|\s+for me|\s+now|\s+jarvis|\s+sir|\s+right now|\s+okay|\s+ok)+$", re.I)
# How people actually start a spoken command ("Okay, so scroll down", "And open Chrome"): not part of the command.
FILLER = re.compile(r"^(?:(?:okay|ok|so|alright|all right|now|and|then|well|u+m+|u+h+|hm+|yeah|yes|oh)[,.!]?\s+)+", re.I)
JOIN = re.compile(r"\s+(?:and then|and also|and after that|after that|then|and)\s+", re.I)
VERB = re.compile(r"(open|close|launch|start|play|pause|resume|stop|set|turn|put|volume|mute|unmute|scroll|take|lock|"
                  r"search|google|type|go|show|minimi[sz]e|maximi[sz]e|press|tell|what|what's|how|remind|run|switch|"
                  r"increase|decrease|lower|raise|skip|next|previous|refresh|check|read|copy|paste|save|make|create|"
                  r"give|who|when|where|why|is|are|do|can)\b", re.I)

SITES = {"youtube": "youtube.com", "gmail": "mail.google.com", "google": "google.com", "github": "github.com",
         "whatsapp": "web.whatsapp.com", "instagram": "instagram.com", "twitter": "x.com", "facebook": "facebook.com",
         "netflix": "netflix.com", "chat gpt": "chatgpt.com", "chatgpt": "chatgpt.com", "amazon": "amazon.in",
         "reddit": "reddit.com", "linkedin": "linkedin.com", "google maps": "maps.google.com", "maps": "maps.google.com",
         "google drive": "drive.google.com", "stack overflow": "stackoverflow.com", "wikipedia": "wikipedia.org",
         "hotstar": "hotstar.com", "prime video": "primevideo.com", "claude": "claude.ai"}
FOLDERS = {"downloads", "documents", "desktop", "pictures", "music", "videos"}
NUM_WORDS = {"zero": 0, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
             "eighty": 80, "ninety": 90, "hundred": 100, "full": 100, "max": 100, "maximum": 100, "half": 50,
             "minimum": 0, "min": 0}


# Spoken shortcuts for whatever window has focus (mostly the browser). (pattern, keys, reply)
_IN = r"(?: (?:in|on) (?:the |my )?(?:browser|chrome|edge|youtube|page|window))?"
SHORTCUTS = [
    (rf"(?:go )?back(?: a page| one page)?{_IN}|(?:go (?:back )?to (?:the )?)?previous page{_IN}", "alt+left", "Going back, sir."),
    (rf"(?:go )?forward(?: a page)?{_IN}|next page{_IN}", "alt+right", "Going forward."),
    (rf"(?:open |make |create )?(?:a |another )?new tab{_IN}", "ctrl+t", "New tab, sir."),
    (rf"close (?:this |the |that |current )?tab{_IN}", "ctrl+w", "Tab closed."),
    (rf"(?:re-?open|restore|bring back) (?:the |that |my )?(?:last |closed )?tab{_IN}", "ctrl+shift+t", "Restored, sir."),
    (rf"(?:next|switch) tab{_IN}|(?:go to (?:the )?)?next tab{_IN}", "ctrl+tab", "Next tab."),
    (rf"(?:previous|last) tab{_IN}|(?:go to (?:the )?)?previous tab{_IN}", "ctrl+shift+tab", "Previous tab."),
    (rf"(?:refresh|reload)(?: (?:the |this )?page)?{_IN}", "f5", "Refreshing."),
    (rf"(?:go |make it |enter |exit |toggle )?full ?screen{_IN}", "f11", "Done, sir."),
    (r"select all|select everything", "ctrl+a", "Selected."),
    (r"copy(?: that| this| it)?", "ctrl+c", "Copied."),
    (r"paste(?: that| this| it)?(?: here)?", "ctrl+v", "Pasted."),
    (r"undo(?: that| this| it)?", "ctrl+z", "Undone."),
    (r"redo(?: that| this| it)?", "ctrl+y", "Redone."),
    (r"save(?: that| this| it| the file| the document)?", "ctrl+s", "Saved, sir."),
    (r"(?:press |hit )?enter|submit(?: it| that)?", "enter", "Done."),
    (r"(?:press |hit )?escape|dismiss(?: that| it)?", "esc", "Done."),
]


def _num(s: str) -> int | None:
    m = re.search(r"\d+", s)
    if m:
        return int(m.group())
    for w, n in NUM_WORDS.items():
        if re.search(rf"\b{w}\b", s):
            return n
    return None


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


_WORDNUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "twenty five": 25,
            "thirty": 30, "forty": 40, "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90}


def _digits(t: str) -> str:
    """'in ten minutes' -> 'in 10 minutes', 'half an hour' -> '30 minutes' (Whisper usually writes digits already)."""
    t = t.replace("half an hour", "30 minutes").replace("quarter of an hour", "15 minutes")
    for w in sorted(_WORDNUM, key=len, reverse=True):
        t = re.sub(rf"\b{w}(?= (?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|a\.?m|p\.?m|o'clock)\b)", str(_WORDNUM[w]), t)
    return t.replace(" o'clock", "")


def _battery_line(sir: bool = True) -> str:
    import psutil
    bat = psutil.sensors_battery()
    tail = ", sir." if sir else "."
    if not bat:
        return "This machine is on mains power; there's no battery to report" + tail
    line = f"Power is at {bat.percent:.0f} percent"
    if bat.power_plugged:
        return line + (" and charging" if bat.percent < 100 else ", fully charged") + tail
    if bat.secsleft and 0 < bat.secsleft < 48 * 3600:
        h, mnt = bat.secsleft // 3600, bat.secsleft % 3600 // 60
        hours = f"{h} hour{'' if h == 1 else 's'} and " if h else ""
        line += f", with roughly {hours}{mnt} minutes remaining"
    if bat.percent < 20:
        line += ". I'd suggest plugging in"
    return line + tail


def _outcome(result: str, success: str) -> str:
    """Translate the safety layer's verdict on a risky action into Jarvis's words."""
    if "declined" in result:
        return random.choice(["Very good, sir. Cancelled.", "Standing down, sir.", "As you wish. Cancelled."])
    if result.startswith("Refused"):
        return "I'm afraid I can't do that. I can't verify that it's you at the keyboard, sir."
    return success


def intruder_folder() -> str:
    """Open the folder of face-guard photos and say how many there are."""
    import os
    from .config import resolve
    cfg = (Ctx.cfg or {}).get("security", {}).get("face_guard", {})
    folder = resolve(cfg.get("intruder_dir", "known:Pictures/Jarvis Intruders"))
    photos = sorted(folder.glob("*.jpg")) if folder.is_dir() else []
    if not photos:
        return "There are no intruder photos, sir. Nobody unfamiliar has been caught at the laptop."
    tools.open_file(folder)
    return f"Here they are, sir: {len(photos)} photo{'s' if len(photos) != 1 else ''} in {folder.name}."


def narrate_diagnostics(d: dict) -> str:
    """A spoken systems report built directly from the numbers (no language model, so it's instant)."""
    issues, normal = [], []
    if d["cpu"] > 85:
        issues.append(f"the processor is working hard at {d['cpu']:.0f} percent")
    else:
        normal.append(f"processor load is at {d['cpu']:.0f} percent")
    if d["ram"] > 88:
        culprit = re.split(r"[-_.]", d["heaviest"])[0] if d.get("heaviest") else ""
        issues.append(f"memory is running high at {d['ram']:.0f} percent" + (f", mostly thanks to {culprit}" if culprit else ""))
    else:
        normal.append(f"memory is at {d['ram']:.0f} percent")
    if d["disk"] > 90:
        issues.append(f"the main drive is nearly full, with only {d['disk_free_gb']} gigabytes free")
    if d.get("battery") is not None:
        if d["battery"] < 20 and not d.get("plugged"):
            issues.append(f"the battery is down to {d['battery']:.0f} percent")
        else:
            normal.append(f"power is at {d['battery']:.0f} percent" + (" and charging" if d.get("plugged") and d["battery"] < 100 else ""))
    hot = bool(d.get("gpu")) and d["gpu"]["temp"] >= 85
    if hot:
        issues.append(f"the graphics card is running hot at {d['gpu']['temp']} degrees")
    if d.get("monitors_bad"):
        issues.append(f"{' and '.join(d['monitors_bad'])} need{'s' if len(d['monitors_bad']) == 1 else ''} attention")
    parts = ["All systems are operational, sir." if not issues
             else "Mostly in order, sir, but " + ", and ".join(issues) + "."]
    if normal:
        s = (", ".join(normal[:-1]) + ", and " + normal[-1]) if len(normal) > 1 else normal[0]
        parts.append(s[0].upper() + s[1:] + ".")
    bits = []
    if d.get("gpu") and not hot:
        bits.append(f"the graphics card is at {d['gpu']['util']} percent load and {d['gpu']['temp']} degrees")
    m = re.match(r"Wi-Fi connected to (.+?)(?: at (\d+)% signal)?$", d.get("wifi", ""))
    if m:
        bits.append(f"Wi-Fi is connected to {m.group(1)}" + (f" at {m.group(2)} percent signal" if m.group(2) else ""))
    elif d.get("wifi", "").startswith("No network"):
        bits.append("there is no network connection")
    if bits:
        s = ", and ".join(bits)
        parts.append(s[0].upper() + s[1:] + ".")
    if "monitors" in d:
        n = len(d.get("alerts") or [])
        parts.append(f"{d['monitors']} security monitors are active, "
                     + ("with nothing to report." if not n else f"with {n} alert{'s' if n != 1 else ''} logged this session."))
    return " ".join(parts)


def clean(text: str) -> str:
    t = text.strip()
    for _ in range(2):                                   # "Okay, so please open ..." / "Please, okay, open ..."
        t = FILLER.sub("", t)
        t = POLITE.sub("", t)
    t = TRAIL.sub("", t)
    return t.strip()


def split_compound(text: str) -> list[str] | None:
    """'open notepad and set the volume to 20' -> ['open notepad', 'set the volume to 20'].
    Only when the second half is clearly a command of its own; 'play guns and roses' is left alone."""
    t = clean(text)
    m = JOIN.search(t)
    if not m:
        return None
    first, rest = t[:m.start()].strip(), t[m.end():].strip()
    if not first or not rest:
        return None
    if not VERB.match(rest):
        v = re.match(r"(open|launch|start|close|quit)\b", first, re.I)      # "open youtube and chrome"
        if not v or len(rest.split()) > 3:
            return None
        rest = f"{v.group(1)} {rest}"
    return [first, rest]


def _site(name: str) -> str | None:
    n = name.strip()
    return SITES.get(n) or SITES.get(n.replace(" ", ""))


def _known_target(name: str) -> bool:
    """Is this the name of something that really exists here: an app alias, a built-in, or a Start-menu app?"""
    n = re.sub(r"^(the|my)\s+|\s+(app|application|program)$", "", name.lower().strip())
    if n in tools.ALIASES or n in tools.BUILTINS or n in tools._NEEDLES:
        return True
    app = tools.known_app(n)
    return bool(app) and (app == n or n in app.split())


class _Probe(Exception):
    """Raised instead of running a tool while only asking "would this be handled here?"."""


def _probing(name, args):
    raise _Probe()


def probe(text: str) -> bool:
    """Would this text be a clear, known command? Nothing is executed. Used on the fast recogniser's rough
    transcript: if it already reads as "volume up" or "open chrome", there is no need to wait ~0.4 s for the
    accurate recogniser to say the same thing. Commands carrying numbers are excluded (rough text gets numbers
    wrong: "fifty" / "fifteen")."""
    if re.search(r"\d|\b(" + "|".join(list(NUM_WORDS) + list(_WORDNUM)[3:]) + r")\b", text.lower()):
        return False
    try:
        return route(text, strict=True, _probe=True) is not None
    except _Probe:
        return True
    except Exception:
        return False


_SPEED_WORDS = {"normal": 1.0, "one": 1.0, "regular": 1.0, "double": 2.0, "two": 2.0, "one and a half": 1.5,
                "one point five": 1.5, "one point two five": 1.25, "one point two": 1.2, "one point seven five": 1.75,
                "one point seven": 1.7, "half": 0.5, "point seven five": 0.75, "zero point seven five": 0.75}
_CR_ITEM = r"(?:video|lecture|lesson|item|one|clip)"
# Any mention of these means Coursera, never a YouTube search.
_CR_INTENT = re.compile(r"\bcoursera\b|\blecture\b|\blesson\b", re.I)
_CR_VIDEO = re.compile(r"(?:play|start|resume|continue|watch|finish|complete|do)\s+"
                       r"(?:the\s+|this\s+|my\s+|all\s+(?:of\s+)?(?:the\s+)?)?(?:video|lecture|lesson)s?\b"
                       r"|\bcontinue watching\b|\bnext video\b|\bplay all\b|\bwatch all\b", re.I)


_AG_ASK = re.compile(r"^(?:so |and |also )?(what|what's|whats|any|anything|is there|are there|do i have|have i got|did i get|"
                     r"got any|show|list|tell|check|read|how many|which|when|give me|go through)\b")
_AG_DUE = re.compile(r"\b(deadlines?|assignments?|submissions?|homework|due|pending|coursework)\b")
_AG_CAL = re.compile(r"\b(calendar|schedule|agenda|timetable)\b")
_AG_DAY_THINGS = re.compile(r"\b(meetings?|events?|classes|lectures?|appointments?|plans?)\b.*\b(today|tomorrow)\b")
_AG_MAIL = re.compile(r"\b(e-?mails?|mails?|gmail|inbox)\b")


def _agenda(t: str, run, free: bool) -> str | None:
    """Deadlines, calendar and email questions (agenda.py), answered from the real data, never from the model."""
    if re.search(r"\b(reminders?|timers?|alarms?|notes?|mean|meaning|define|definition|security|alerts?)\b", t):
        return None
    if re.fullmatch(r"(?:sync|refresh|update|reload|recheck)(?: my| the)? (?:calendars?|e-?mails?|gmail|deadlines|agenda|"
                    r"inbox|assignments)", t):
        return run("agenda", {"action": "refresh"})
    asking = bool(_AG_ASK.match(t))
    if asking and _AG_MAIL.search(t) and not re.match(r"(open|send|write|compose|reply)\b", t):
        return run("agenda", {"action": "emails"})
    if (asking and (_AG_CAL.search(t) or _AG_DAY_THINGS.search(t))) or re.fullmatch(
            r"(?:what do i have|what have i got|am i (?:free|busy)|what(?:'s| is) (?:on|happening|planned))"
            r"(?: on| going on)? (?:for )?(today|tomorrow)", t):
        if _AG_DUE.search(t) and not _AG_CAL.search(t):
            return run("agenda", {"action": "deadlines"})
        return run("agenda", {"action": "tomorrow" if "tomorrow" in t else "today"})
    if asking and _AG_DUE.search(t):
        return run("agenda", {"action": "deadlines"})
    m = re.fullmatch(r"(?:mark|set) (?:the |my )?(.+?) (?:as )?(?:done|complete|completed|submitted|finished)|"
                     r"i(?:'ve| have)? (?:already |just )?(?:submitted|finished|completed|turned in|done with) "
                     r"(?:the |my )?(.+)", t) if free else None
    if m and Ctx.agenda and Ctx.agenda.find(m.group(1) or m.group(2)):
        return run("agenda", {"action": "done", "value": m.group(1) or m.group(2)})
    return None


# --- files (filetools.py): convert, compress / zip, images into one PDF or PowerPoint, researched presentations
_FMT = r"(png|jpe?g|webp|bmp|bitmap|gif|tiff?|ico|icon|pdf)"
_DECK = r"(?:ppt|pptx|power ?point(?: presentation)?|presentation|slide ?show|slide deck|deck|slides)"
_SAVE_IN = re.compile(r",?\s+(?:and\s+)?(?:(?:save|put|store|keep|place|drop)\s+(?:it|them|that|those|the (?:file|files|pdf|result))?"
                      r"\s*)?(?:in|on|to|into|onto)\s+(?:the |my )?(desktop|downloads?|documents|pictures|music|videos|onedrive|"
                      r"this folder|the same folder|same folder|[\w .-]+? folder)$")
_CALL_IT = re.compile(r",?\s+(?:and\s+)?(?:call|name)\s+it\s+(.+)$")
_FILEREF = re.compile(r"\b(this|that|these|those|them|it|selected|selection|highlighted|images?|photos?|pictures?|pics|"
                      r"screenshots?|files?|folders?)\b|\.(?:jpe?g|png|webp|bmp|gif|tiff?|heic|docx?|pptx?|xlsx?|pdf)\b")
_NUMBER = {"three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
           "twelve": 12, "fifteen": 15, "twenty": 20}


def _files(t: str, run, free: bool) -> str | None:
    if not free or not re.search(r"\b(convert|change|turn|make|save|export|compress|zip|shrink|squash|reduce|create|"
                                 r"build|prepare|put|combine|merge|join|research|generate|draft|design|do|stick|take|"
                                 r"gather|collect|grab|compile)\b", t):
        return None
    s, save_to, name = t, "", ""
    # "..., they're too big to email" / "... because I need to send them": the reason isn't part of what to do
    s = re.sub(r"(?:,\s+(?:so|since|they're|they are|it's|it is|i need|i want)|\s+(?:because|cause|coz|cuz|so that))\s.*$",
               "", s).strip()
    m = _CALL_IT.search(s)
    if m:
        name, s = m.group(1).strip(" \"'"), s[:m.start()].strip()
    m = _SAVE_IN.search(s)
    if m:
        save_to, s = m.group(1), s[:m.start()].strip()

    def files(action, obj, **extra):
        args = {"action": action, "files": (obj or "selected").strip(), "save_to": save_to, "name": name}
        args.update(extra)
        return run("file_tools", args)

    # "take the pictures I picked and stick them in one document" / "gather the screenshots and make slides out of them"
    m = re.fullmatch(r"(?:take|grab|get|gather(?: up)?|collect|use)\s+(.+?)\s+and\s+(?:then\s+)?(?:stick|put|combine|merge|turn|"
                     r"make|join|compile)\s+(?:them|those|it)?\s*(?:all\s+)?(?:in|into|to|as)?\s*(?:a\s+|one\s+|a single\s+|an?\s+)?"
                     r"(?:single\s+)?(pdf|document|file|" + _DECK[3:-1] + r")(?:\s+(?:out of|from|of|with)\s+(?:them|those|it))?", s)
    if m and _FILEREF.search(m.group(1)):
        return files("pdf" if m.group(2) in ("pdf", "document", "file") else "pptx", m.group(1))

    # a presentation: about a topic (researched) or of some images
    m = (re.fullmatch(rf"(?:make|create|prepare|build|do|generate|design|draft|put together|write)\s+(?:me\s+|us\s+)?"
                      rf"(?:a\s+|an\s+|one\s+)?(?:(\d+|\w+)[- ]slides?\s+)?{_DECK}\s+(?:on|about|regarding|covering|for|of|"
                      rf"from|with|using|out of)\s+(.+?)(?:\s+with\s+(\d+|\w+)\s+slides)?", s)
         or re.fullmatch(rf"(?:do (?:some )?|)research(?: on| about)?\s+(.+?)\s+and\s+(?:then\s+)?(?:make|create|prepare|build|"
                         rf"do)\s+(?:me\s+)?(?:a\s+|an\s+)?{_DECK}(?: (?:on|about) (?:it|that|them))?(?:\s+with\s+(\d+|\w+)\s+slides)?", s))
    if m:
        if m.re.pattern.startswith("(?:do (?:some )?|)research"):
            topic, n = m.group(1), m.group(2)
        else:
            topic, n = m.group(2), m.group(3) or m.group(1)
        if _FILEREF.search(topic) and not re.search(r"\b(about|on)\b", s.split(topic)[0][-8:]):
            return files("pptx", topic)                                     # "a ppt of these 4 images"
        count = int(n) if n and n.isdigit() else _NUMBER.get(n or "", 8)
        return run("make_presentation", {"topic": topic, "slides": count, "save_to": save_to})
    m = re.fullmatch(rf"(?:put|turn|make|convert|combine|merge)\s+(.+?)\s+(?:in|into|to)\s+(?:a\s+|one\s+|a single\s+)?{_DECK}", s)
    if m and _FILEREF.search(m.group(1)):
        return files("pptx", m.group(1))

    # one PDF of several images
    m = (re.fullmatch(r"(?:make|create|build|generate|prepare|put together|do)\s+(?:me\s+)?(?:a\s+|an\s+|one\s+|a single\s+)?"
                      r"(?:single\s+)?pdf(?: file| document)?\s+(?:of|from|with|out of|using|containing|for)\s+(.+)", s)
         or re.fullmatch(r"(?:combine|merge|put|join|add|turn|make|stick|collect|gather)\s+(?:all\s+)?(.+?)\s+(?:all\s+)?"
                         r"(?:in|into|to|as)\s+(?:a\s+|one\s+|a single\s+)?(?:single\s+)?pdf(?: file| document)?", s)
         or re.fullmatch(r"(?:combine|merge|put|join|turn|make|stick|collect|gather)\s+(?:all\s+)?(.+?)\s+(?:all\s+)?"
                         r"(?:in|into|to|as)\s+(?:one|a single)\s+(?:document|file)", s))
    if m and _FILEREF.search(m.group(1)) or m and re.search(r"\.\w{3,4}\b|,| and ", m.group(1)):
        return files("pdf", m.group(1))

    # convert to another type ("convert this to png", "change the file type to webp", "save these as jpg")
    m = (re.fullmatch(rf"(?:convert|change)\s+(?:the\s+|its\s+|their\s+)?(?:file\s+)?(?:type|format|extension)(?:\s+of\s+(.+?))?\s+"
                      rf"(?:from\s+\w+\s+)?(?:to|into)\s+(?:an?\s+)?{_FMT}", s)
         or re.fullmatch(rf"(?:convert|change|turn|make|save|export|transform)\s+(.+?)\s+(?:from\s+\w+\s+)?(?:to|into|as|in)\s+"
                         rf"(?:an?\s+|the\s+)?{_FMT}(?:\s+(?:format|files?|images?|type|file type))?", s))
    if m:
        obj = m.group(1)
        if obj in ("this", "that", "it", "these", "those", "them", "this one", "that one"):
            obj = "selected"
        if obj and not _FILEREF.search(obj) and not re.search(r"\b(jpe?g|png|webp|bmp|gif|tiff?|heic)s?\b", obj) \
                and not re.search(r"\.\w{3,4}\b|,| and ", obj):
            obj = obj if len(obj.split()) <= 4 else None                   # a spoken file name, or not about files at all
            if obj is None:
                return None
        if obj and re.fullmatch(r"(?:this|that|these|those|the|my|all)?\s*(?:jpe?g|png|webp|bmp|gif|tiff?|heic)s?(?: files?| images?)?", obj):
            obj = "selected"                                                # "convert this jpg to png"
        return files("convert", obj, to=m.group(2))

    # smaller files: images re-saved smaller, anything else (or "zip") into a .zip
    m = (re.fullmatch(r"(?:compress|zip(?: up)?|shrink|squash)\s+(.+?)(?:\s+(?:into|in|to|as)\s+(?:a\s+)?zip(?: file| folder| archive)?)?", s)
         or re.fullmatch(r"(?:reduce|cut|lower|decrease|shrink)\s+(?:down\s+)?(?:the\s+)?(?:file\s+)?size\s+of\s+(.+)", s)
         or re.fullmatch(r"(?:make|create)\s+(?:a\s+)?zip(?: file| archive)?\s+(?:of|from|with)\s+(.+)", s)
         or re.fullmatch(r"make\s+(.+?)\s+(?:smaller|lighter)(?:\s+in size)?", s))
    if m and (_FILEREF.search(m.group(1)) or m.re.pattern.startswith(("(?:compress", "(?:make|create)\\s+(?:a\\s+)?zip"))):
        if re.search(r"\b(window|screen|orb|ball|sphere|font|text|volume|yourself)\b", m.group(1)):
            return None
        return files("zip" if re.search(r"\bzip\b", t) else "compress", m.group(1))
    return None


def _speed(s: str) -> float | None:
    s = re.sub(r"\s*(?:x|times|speed)$", "", s.strip()).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?|\.\d+", s):
        return float(s)
    return _SPEED_WORDS.get(s)


def _coursera(t: str, run, free: bool) -> str | None:
    """Coursera by voice. Words like "next", "pause" and "skip" only mean Coursera while a Coursera tab is
    connected (otherwise they stay media keys); the course name in "play my Coursera course on X" needs free mode."""
    from .coursera import BRIDGE

    # open / resume a course
    m = re.fullmatch(r"(?:play|open|start|resume|continue|watch|go to)(?: with)? (?:my |the )?coursera"
                     r"(?: courses?| class(?:es)?| lectures?| videos?| lessons?)?(?: (?:called|named|on|for|about) (.+))?", t) \
        or re.fullmatch(r"(?:play|open|start|resume|continue|watch) (?:my |the )?(.+?) (?:course|class|lectures?) on coursera", t)
    if m:
        name = (m.group(1) or "").strip() if free else ""
        verb = "open" if t.startswith(("open", "go to")) else "resume"      # play / resume also start autoplay
        return run("coursera", {"action": verb, "value": name})

    # progress & dashboard
    if re.fullmatch(r"(?:show|open|bring up)(?: me)? (?:the |my )?coursera (?:dashboard|progress board)|"
                    r"coursera dashboard|(?:open|show) (?:the |my )?(?:course|learning) dashboard", t):
        return run("coursera", {"action": "dashboard"})
    if re.fullmatch(r"(?:what(?:'s| is) )?my coursera progress|coursera progress|how (?:many|much) (?:videos|lectures)"
                    r"(?: do i have)?(?: left| remaining| to go)|how (?:far|much) (?:am i |have i )?(?:through|into|done)"
                    r"(?: with)?(?: my| the)? course|course progress|how many videos left", t):
        return run("coursera", {"action": "progress"})

    # autoplay ("binge mode")
    if re.fullmatch(r"(?:turn |switch |put )?on (?:the )?(?:binge|auto ?-?play)(?: mode)?|(?:turn |switch |put )?(?:the )?"
                    r"(?:binge|auto ?-?play)(?: mode)? on|(?:start|enable|activate) (?:the )?(?:binge|auto ?-?play)(?: mode)?|"
                    r"keep playing (?:the )?(?:videos|lectures|course)|play (?:all|every)(?: of)? (?:the )?(?:videos|lectures)"
                    r"(?: one after (?:another|the other))?|play (?:the )?(?:videos|lectures) one (?:after|by) (?:another|one)|"
                    r"(?:complete|finish|do|watch|run)\s+(?:all\s+)?(?:of\s+)?(?:the\s+|my\s+)?(?:videos?|lectures?|lessons?|course)"
                    r"(?:\s+after\s+this)?|play (?:the (?:rest|remaining)|through)(?: of)?(?: the)?(?: course| videos| lectures)?", t):
        return run("coursera", {"action": "autoplay_on"})
    if re.fullmatch(r"(?:turn |switch )?off (?:the )?(?:binge|auto ?-?play)(?: mode)?|(?:turn |switch )?(?:the )?"
                    r"(?:binge|auto ?-?play)(?: mode)? off|(?:stop|disable|end) (?:the )?(?:binge|auto ?-?play)(?: mode)?", t):
        return run("coursera", {"action": "autoplay_off"})

    if not BRIDGE.active():
        # "lecture" / "lesson" only ever mean Coursera, so these never become a YouTube search for "next lecture"
        m = re.fullmatch(r"(?:play |go to |open |skip to |start )?(?:the |my )?(next|previous|first|this) (?:lecture|lesson)s?"
                         r"(?: on coursera)?|(?:play |go to |open )?(?:the )?(next|previous) (?:video|item) on coursera|"
                         r"coursera (next|previous)|(?:play|start|watch) (?:the |my )?(?:lectures?|lessons?)", t)
        if m:
            which = next((g for g in m.groups() if g), "play")
            return run("coursera", {"action": {"next": "next", "previous": "previous"}.get(which, "play")})
        # Anything else that clearly means Coursera (a lesson, a lecture, "continue watching", "start the video")
        # gets the clear "load the extension" reply here, so it can never fall through to a YouTube search.
        if _CR_INTENT.search(t) or _CR_VIDEO.search(t):
            return run("coursera", {"action": "status"})
        return None

    if re.fullmatch(rf"(?:play |go to |open |skip to |go on to )?(?:the )?next(?: {_CR_ITEM})?|"
                    rf"skip(?: it| this| (?:the |this )?{_CR_ITEM})?", t):
        return run("coursera", {"action": "next"})
    if re.fullmatch(rf"(?:play |go to |open |go back to )?(?:the )?previous(?: {_CR_ITEM})?|(?:go )?back (?:a|one) {_CR_ITEM}", t):
        return run("coursera", {"action": "previous"})
    if re.fullmatch(rf"pause(?: it| (?:the |this )?{_CR_ITEM})?|stop (?:the |this )?{_CR_ITEM}", t):
        return run("coursera", {"action": "pause"})
    if re.fullmatch(rf"play|resume|continue|unpause|(?:play|resume|continue|unpause) (?:the |this )?(?:{_CR_ITEM}|it)|"
                    r"continue playing|keep going", t):
        return run("coursera", {"action": "play"})
    if re.fullmatch(rf"(?:restart|replay)(?: (?:the |this )?{_CR_ITEM})?|start (?:the |this )?{_CR_ITEM} (?:again|over)|"
                    r"(?:play it |go back to the |start )?from the (?:start|beginning)", t):
        return run("coursera", {"action": "restart"})
    m = re.fullmatch(r"(?:go |skip |jump |fast )?(forward|ahead|back|backward|backwards|rewind)(?: by)?(?: (\d+|[a-z]+) ?"
                     r"(seconds?|secs?|minutes?|mins?))?", t)
    if m and (m.group(2) or m.group(1) in ("ahead", "rewind") or m.group(1) == "forward"):
        n = _num(m.group(2) or "") if m.group(2) else 10
        if n is not None:
            secs = n * (60 if (m.group(3) or "").startswith("min") else 1)
            return run("coursera", {"action": "back" if m.group(1).startswith(("back", "rewind")) else "forward",
                                    "value": str(secs)})
    if re.fullmatch(r"how (?:much|long) (?:time )?(?:is )?left(?: in (?:this|the) (?:video|lecture))?|"
                    r"what(?:'s| is) (?:playing|this (?:video|lecture)|the (?:video|lecture))|coursera status", t):
        return run("coursera", {"action": "status"})
    tgt = BRIDGE.target()
    cur = float((tgt[1].get("speed") if tgt else 1) or 1)
    if re.fullmatch(r"speed (?:it )?up|(?:play )?faster|increase (?:the )?speed", t):
        return run("coursera", {"action": "speed", "value": str(min(3.0, cur + 0.25))})
    if re.fullmatch(r"slow (?:it )?down|(?:play )?slower|decrease (?:the )?speed|reduce (?:the )?speed", t):
        return run("coursera", {"action": "speed", "value": str(max(0.25, cur - 0.25))})
    m = re.fullmatch(r"(?:set |change |make |put )?(?:the )?(?:playback |video )?speed(?: to| at)? (.+)|"
                     r"(?:play|watch)(?: it| this| the video)? at (.+?)(?: speed)?|(normal) speed", t)
    if m:
        rate = _speed(m.group(1) or m.group(2) or m.group(3))
        if rate:
            return run("coursera", {"action": "speed", "value": f"{rate:g}"})
    return None


def route(text: str, strict: bool = False, _probe: bool = False) -> str | None:
    """Return a spoken reply if the command was handled here, else None.
    strict=True only acts on fixed phrases and on things known to exist (apps, sites, folders): used for the
    second speech recogniser's version of a sentence and for the first half of "do X and Y", where guessing at
    free-form text ("play ...", "search for ...", "type ...") would be wrong."""
    t = clean(text).lower()
    if not t:
        return None
    run = _probing if _probe else Ctx.execute or (lambda name, args: str(tools.REGISTRY[name].func(**args)))
    free = not strict

    # --- keyboard shortcuts by voice ("new tab", "close this tab", "go back", "refresh", "full screen", "undo" ...)
    # Checked first so "close this tab" isn't mistaken for closing an application.
    for pattern, keys, reply in SHORTCUTS:
        if re.fullmatch(pattern, t):
            run("press_keys", {"keys": keys})
            return reply

    # --- time & date
    if re.fullmatch(r"(what(?:'s| is) the time|what time is it|time|the time|current time|tell me the time)", t):
        return f"It's {dt.datetime.now().strftime('%I:%M %p').lstrip('0')}, sir."
    if re.fullmatch(r"(what(?:'s| is) (the |today's )?date( today)?|what day is it( today)?|what(?:'s| is) today)", t):
        now = dt.datetime.now()
        return f"It's {now.strftime('%A')}, the {_ordinal(now.day)} of {now.strftime('%B %Y')}."

    # --- battery
    if re.fullmatch(r"(how(?:'s| is) (the |my )?(battery|power)|(battery|power)( status| level| percentage)?|"
                    r"what(?:'s| is) (the |my )?battery( level| status| percentage)?|how much (battery|power|charge)"
                    r"( is left| do i have| do we have)?)", t):
        return _battery_line()

    # --- small talk straight from the films
    if re.fullmatch(r"(are )?you (there|up|awake|online|with me)|wake up|daddy's home|i'm (home|back)", t):
        return random.choice(["For you, sir, always.", "At your service, sir.", "Online and ready, sir."])
    m = re.fullmatch(r"good (morning|afternoon|evening)", t)
    if m:
        pend = len(Ctx.reminders.pending()) if Ctx.reminders else 0
        return (f"Good {m.group(1)}, sir. It's {dt.datetime.now().strftime('%I:%M %p').lstrip('0')}. {_battery_line(False)}"
                + (f" You have {pend} pending reminder{'s' if pend != 1 else ''}." if pend else ""))
    if re.fullmatch(r"good ?night", t):
        return "Goodnight, sir. I'll keep watch."
    if re.fullmatch(r"(what can you do|what are you capable of|help|help me|what are your (commands|abilities|features|skills)|"
                    r"show (me )?(the |your )?commands|what do you do|what can i (say|ask)( you)?)", t):
        return ("I can open and close apps and websites, control the volume, brightness, music and your browser, read "
                "your screen, convert, compress and zip your files, turn pictures into a PDF or slides, research a topic "
                "and build you a presentation, set reminders and timers, take notes, check security, do sums and "
                "answer questions, sir. "
                "Say stay with me for a longer chat, or double-click me to type instead.")

    # --- diagnostics & security sweep
    if re.fullmatch(r"(run |do |perform |give me )?(a |the )?(full |complete |system )?diagnostics?( report| check)?|"
                    r"(give me a |system )?status report|system status|how are (the |all )?systems|"
                    r"how(?:'s| is) (the |my )?(system|computer|laptop|pc)( doing| holding up)?|how are we (doing|looking)", t):
        if _probe:
            raise _Probe()
        return narrate_diagnostics(tools.diagnostics_data())
    if re.fullmatch(r"(run |do |perform |start )?(a |the )?(full |quick |complete )?(security |system |threat )?(scan|sweep)"
                    r"( (of |on )?(the |my )?(system|laptop|computer|pc))?( for threats)?|"
                    r"scan (the |my )?(system|laptop|computer|pc)( for threats)?|are we (secure|safe)|any threats", t):
        return run("security_sweep", {}).rstrip(".") + ", sir."
    if re.fullmatch(r"(show|open|view|see)( me)? (the |my )?(intruders?|intruder (photos?|pictures?|pics|folder|images?)|"
                    r"(photos?|pictures?|pics) of (the )?(intruders?|strangers?))", t):
        if _probe:
            raise _Probe()
        return intruder_folder()

    # --- network: answered from the real adapter state (the model once invented a Wi-Fi name and IP address here)
    if re.fullmatch(r"(which|what) (wi-?fi|network|internet)( am i| i am| are we| we are| (is )?(my|the|this) (laptop|computer|pc)( is)?)?"
                    r"( connected to| connected with| on| using)?|(what(?:'s| is) )?(my|the) (ip|ip address|wi-?fi|network)( name| status)?|"
                    r"(am i|are we) (connected|online)( to the internet)?|(is )?(the )?(internet|wi-?fi|network) "
                    r"(working|connected|up|down|status)|(network|wi-?fi|internet) (status|details|info)", t):
        info = run("network_info", {})
        return info.rstrip(".") + ", sir."

    # --- Jarvis's own orb
    if re.fullmatch(r"(hide|go away|disappear|get out of (the|my) way|hide yourself|hide (the|your) (orb|ball|sphere)|"
                    r"(get|go) off (the|my) screen)", t):
        run("orb", {"action": "hide"})
        return "Out of your way, sir. Say show yourself when you want me back."
    if re.fullmatch(r"(show yourself|come back|show (the|your) (orb|ball|sphere)|where are you|appear|reappear|unhide)", t):
        run("orb", {"action": "show"})
        return "Right here, sir."
    m = re.fullmatch(r"(open|show|bring up|close|hide) (the |your |my )?(console|transcript|chat( window)?|command (box|window)|"
                     r"text (box|window))", t)
    if m:
        closing = m.group(1) in ("close", "hide")
        run("orb", {"action": "close_console" if closing else "console"})
        return "Console closed." if closing else "Console open, sir. You can type to me there."
    m = re.fullmatch(r"(?:change|switch|set|make|turn)?\s*(?:yourself|your(?:self)?|the orb|the ball|the sphere)?\s*"
                     r"(?:colou?r|theme|look)?\s*(?:to|into)?\s*(gold|golden|yellow|orange|amber|blue|cyan|green|emerald|"
                     r"purple|violet|pink|red|crimson|white|silver|pearl)(?: colou?r)?", t)
    if m and re.search(r"colou?r|theme|yourself|orb|ball|sphere|turn|go ", t):
        run("orb", {"action": "colour", "value": m.group(1)})
        return f"{m.group(1).title()} it is, sir."
    m = re.fullmatch(r"(?:get|grow|become|be|make yourself|make (?:the )?(?:orb|ball|sphere))\s+(bigger|larger|smaller|tinier)", t)
    if m:
        run("orb", {"action": "smaller" if m.group(1) in ("smaller", "tinier") else "bigger"})
        return "As you wish, sir."
    m = re.fullmatch(r"(?:move|go|shift|sit)(?: yourself| the orb| the ball)?(?: over)? (?:to |in |on )?(?:the )?"
                     r"((?:top|bottom|upper|lower)[ -](?:left|right)|left|right|top|bottom|cent(?:er|re)|middle)"
                     r"(?: corner| side| of (?:the|my) screen)*", t)
    if m and re.search(r"yourself|orb|ball|corner|screen|side|cent|middle", t):
        where = m.group(1).replace("upper", "top").replace("lower", "bottom").replace("-", " ").replace("middle", "centre")
        run("orb", {"action": "move", "value": where})
        return "Relocating, sir."

    # --- reminders, timers, alarms
    tw = _digits(t)
    WHEN = (r"(in (?:\d+(?:\.\d+)?) ?(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)|"
            r"(?:at|by) \d{1,2}(?:[:.]\d{2})? ?(?:a\.?m\.?|p\.?m\.?)?|tomorrow(?: at)? \d{1,2}(?:[:.]\d{2})? ?(?:a\.?m\.?|p\.?m\.?)?)")
    m = re.fullmatch(rf"remind me {WHEN} (?:to |about |that )?(.+)", tw) if free else None
    when, msg = (m.group(1), m.group(2)) if m else (None, None)
    if not m and free:
        m = re.fullmatch(rf"remind me (?:to |about |that )?(.+?) {WHEN}", tw)
        when, msg = (m.group(2), m.group(1)) if m else (None, None)
    if not m and free:
        m = re.fullmatch(r"(?:set|start|put) (?:a |an )?(timer|alarm) (?:for |of |at |to )?(.+)|wake me (?:up )?(?:at |in )(.+)", tw)
        if m:
            kind = m.group(1) or "alarm"
            spec = (m.group(2) or m.group(3)).strip()
            when = spec if kind == "alarm" or spec.startswith("in ") else "in " + spec
            msg = kind
    if when:
        at = parse_when(when)
        if at:
            run("set_reminder", {"when": when, "message": msg})
            if msg == "timer":
                return f"Timer set, sir. I'll let you know {describe(at)}."
            if msg == "alarm":
                return f"Alarm set {describe(at)}, sir."
            return f"Very good, sir. I'll remind you {describe(at)}."
    if re.fullmatch(r"(what are|list|show( me)?|read( me)?|do i have( any)?|any) (my |the )?(pending |upcoming )?(reminders|timers|alarms).*", t):
        return run("list_reminders", {})
    if re.fullmatch(r"(read|show|what are|tell me)( me)?( my| the)? notes", t):
        return run("read_notes", {})
    m = re.fullmatch(r"(?:cancel|delete|remove|clear|stop) (?:(all)(?: of)? )?(?:my |the |that )?(reminder|timer|alarm)s?"
                     r"(?: (?:about|for|to) (.+))?", t)
    if m and (free or not m.group(3)):
        # "cancel the timer" -> timers only; "cancel my reminders" -> everything; "... about the oven" -> that one
        about = m.group(3) or ("all" if m.group(1) or m.group(2) == "reminder" else m.group(2))
        r = run("cancel_reminder", {"about": about})
        return "Cancelled, sir." if r.startswith("Cancelled") else "I couldn't find a matching reminder, sir."
    reply = _agenda(t, run, free)
    if reply:
        return reply
    m = re.fullmatch(r"copy (.+?) (?:to|into|onto) (?:the |my )?clipboard", clean(text), re.I) if free else None
    if m:
        run("clipboard", {"action": "write", "text": m.group(1).strip(" \"'")})
        return "Copied to the clipboard, sir."
    m = re.fullmatch(r"remember that (.+)", clean(text), re.I) if free else None
    if m:
        run("remember", {"fact": m.group(1).strip()})
        return "Committed to memory, sir."
    reply = _files(t, run, free)                         # before the arithmetic below: "these 4 images" has a digit
    if reply:
        return reply

    # --- arithmetic: exact and instant ("what's 15 times 23", "18 percent of 2450", "square root of 144")
    m = re.fullmatch(r"(?:(?:what(?:'s| is)|calculate|compute|how much is|work out|solve)\s+)?(.*\d.*)", t) if free else None
    if m:
        e = m.group(1).replace(",", "")
        for word, sym in (("multiplied by", "*"), ("times", "*"), (" x ", " * "), ("plus", "+"), ("minus", "-"),
                          ("divided by", "/"), (" over ", " / "), ("percent of", "% of"), ("% off", "% of"),
                          ("to the power of", "**"), ("squared", "**2"), ("cubed", "**3")):
            e = e.replace(word, sym)
        e = re.sub(r"(?:the )?square root of (\d+(?:\.\d+)?)", r"sqrt(\1)", e).strip(" ?=")
        if re.fullmatch(r"(?:[\d\s.+\-*/()%]|of|sqrt)+", e) and re.search(r"[+\-*/%]|sqrt", e):
            r = run("calculate", {"expression": e})
            if "=" in r:
                val = r.rsplit("=", 1)[-1].strip()
                return f"That's {val}, sir."

    # --- weather
    m = re.fullmatch(r"(?:what(?:'s| is) the |how(?:'s| is) the |check the )?weather(?: like| report| forecast| today| outside| right now)*"
                     r"(?: (?:in|for|at) ([a-z .'-]+?))?(?: today| right now| like)*", t)
    if m and strict and m.group(1):
        m = None                                         # a city name from the rough recogniser can't be trusted
    if m and not (m.group(1) and (len(m.group(1).split()) > 3 or re.search(r"\b(and|should|will|do|is)\b", m.group(1)))):
        w = run("weather", {"city": m.group(1) or ""})
        w = re.sub(r"[^\x00-\x7f°]", "", w).replace("+", "").replace("°C", " degrees").replace("km/h", " kilometres per hour")
        return w.rstrip(".") + ("." if "couldn't" in w else ", sir.")

    # --- power (always confirmed by the brain's safety layer before anything happens)
    m = re.fullmatch(r"(shut ?down|power off|power down|turn off|switch off|restart|reboot|sleep|hibernate|sign ?out|"
                     r"log ?off|log ?out)(?: the| my| this)?( computer| laptop| pc| system| machine)?", t)
    if m and (m.group(2) or m.group(1) in ("shutdown", "shut down", "restart", "reboot", "sign out", "signout", "log off", "log out")):
        verb = m.group(1).replace(" ", "")
        action = {"shutdown": "shutdown", "poweroff": "shutdown", "powerdown": "shutdown", "turnoff": "shutdown",
                  "switchoff": "shutdown", "restart": "restart", "reboot": "restart", "sleep": "sleep",
                  "hibernate": "sleep", "signout": "signout", "logoff": "signout", "logout": "signout"}[verb]
        return _outcome(run("power", {"action": action}),
                        {"shutdown": "Shutting down in ten seconds, sir. Goodbye.", "restart": "Restarting in ten seconds, sir.",
                         "sleep": "Going to sleep, sir.", "signout": "Signing you out, sir."}[action])
    # "kill chrome" / "force close notepad" / "end task spotify": the hard way, so it is confirmed first
    m = re.fullmatch(r"(?:kill|force[ -]?(?:close|quit|stop)|end task(?: for| on)?|terminate)\s+(?:the\s+|my\s+)?(.+)", t)
    if m and len(m.group(1).split()) <= 3 and (free or _known_target(m.group(1))):
        target = re.sub(r"\s+(app|application|program|process)$", "", m.group(1))
        r = run("kill_process", {"target": target})
        return _outcome(r, f"{target.title()} terminated, sir." if r.startswith("Killed") else f"I don't see {target} running, sir.")
    m = re.fullmatch(r"(?:close|quit|exit)\s+(?:down\s+)?(?:the\s+|my\s+)?(.+)", t)
    if m and len(m.group(1).split()) <= 4 and not re.search(r"\b(and|then)\b", m.group(1)):
        target = re.sub(r"\s+(app|application|program|first|again|one|please)$", "", m.group(1)).strip()
        target = re.sub(r"\s+(app|application|program)$", "", target)
        if target in ("it", "that", "this", "that one", "this one"):
            # "close it": whatever Jarvis opened last, else the window in front
            last = Ctx.last_opened
            if last and last[0] == "tab":
                run("press_keys", {"keys": "ctrl+w"})
                Ctx.last_opened = None
                return "Tab closed, sir."
            if last and last[0] == "app":
                r = run("close_app", {"name": str(last[1])})
                Ctx.last_opened = None
                if r.startswith("Closed"):
                    return f"{str(last[1]).title()} closed, sir."
            run("window_control", {"action": "close"})
            return "Closed, sir."
        if target in ("window", "this window", "the window", "that window", "current window"):
            run("window_control", {"action": "close"})
            return "Closed, sir."
        if free or _known_target(target):
            r = run("close_app", {"name": target})
            return _outcome(r, f"{target.title()} closed, sir." if r.startswith("Closed") else f"I don't see {target} open, sir.")

    # --- security lab (own network / this laptop / CTF)
    if re.fullmatch(r"(?:scan|check|map|probe)(?: my| the| our)?(?: local| home)?(?: wi-?fi)?(?: network)?"
                    r"(?: for (?:devices|open ports|hosts))?|what(?:'s| is) on (?:my|the|our) (?:network|wi-?fi)|"
                    r"(?:find|list|show) (?:the )?devices on (?:my|the|our) (?:network|wi-?fi)|network scan|"
                    r"scan (?:my |the )?(?:wi-?fi|network)", t) and re.search(r"network|wi-?fi", t):
        return run("network_scan", {})
    if re.fullmatch(r"(?:audit|check|review)(?: (?:my|this|the))?(?: (?:laptop|computer|pc|machine|system))?"
                    r"(?:'s)? security(?: settings)?|security audit|audit (?:my|this) (?:laptop|computer|pc|security)|"
                    r"how (?:exposed|secure) am i|(?:check|audit) (?:my )?(?:firewall|open ports)|am i exposed", t):
        return run("security_audit", {})

    # --- starter code ("write the java template", "type the basic code in c++", "java boilerplate with class Solution")
    m = re.fullmatch(r"(?:write|type|put|add|give me|insert|paste)?\s*(?:the |a |my )?(?:basic |starter |starting )?"
                     r"(java|python|c\+\+|cpp|c plus plus|c)\s*(?:template|boilerplate|starter code|basic code|skeleton|"
                     r"basics|setup|structure)(?: code)?(?: with (?:the )?class (?:name )?(\w+))?"
                     r"|(?:write|type|put|add|give me|insert)\s+(?:the |a )?(?:basic |starter )?(?:code |template |"
                     r"boilerplate |skeleton )(?:in|for) (java|python|c\+\+|cpp|c plus plus|c)", t)
    if m:
        args = {"language": m.group(1) or m.group(3)}
        if m.group(2):                                   # speech comes in lower case; Java class names don't
            args["class_name"] = m.group(2)[0].upper() + m.group(2)[1:]
        return run("type_code_template", args)

    # --- Coursera (Chrome extension link; see jarvis/coursera.py)
    reply = _coursera(t, run, free)
    if reply:
        return reply

    # --- protocols
    m = re.fullmatch(r"(?:initiate|engage|activate|start|run|execute|begin)?\s*(?:the\s+)?(.+?)\s+protocol", t) \
        or re.fullmatch(r"(?:initiate|engage|activate|execute)\s+(?:the\s+)?(?:protocol\s+)?(.+)", t)
    if m:
        name = m.group(1).strip()
        key = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
        if any(key and (key in k or k in key) for k in tools.protocol_names()):
            return run("run_protocol", {"name": name})

    # --- open things
    m = re.fullmatch(r"(open|launch|start|fire up|bring up|pull up|go to|take me to|visit)\s+(?:the\s+|my\s+|up\s+)?(.+)", t)
    if not m:
        # "get the calculator up", "bring chrome up for me", "i want notepad open", "get me the file explorer"
        m2 = re.fullmatch(r"(?:get|bring|pull|put|fire)\s+(?:me\s+)?(?:the\s+|my\s+)?(.+?)\s+(?:up|open|on (?:the |my )?screen)|"
                          r"(?:i want|i need|give me|get me)\s+(?:the\s+|my\s+)?(.+?)(?:\s+(?:open|opened|up))?", t)
        target2 = (m2.group(1) or m2.group(2) or "").strip() if m2 else ""
        if target2 and (_site(target2) or target2 in FOLDERS or _known_target(target2)):   # known things only
            m = re.fullmatch(r"(open)\s+(.+)", "open " + target2)
    if m:
        target = m.group(2).strip()
        # "open youtube in chrome" / "... in a new tab" / "... on the browser": where it opens is not part of the name
        target = re.sub(r"\s+(?:in|on)\s+(?:a\s+|the\s+|my\s+)?(?:new\s+)?(?:tab|chrome|google chrome|browser|edge)(?:\s+tab)?$", "", target)
        target = re.sub(r"\s+(app|application|website|site|page|folder)$", "", target)
        if _site(target):
            run("open_website", {"url": _site(target)})
            return f"Opening {target.title()}, sir."
        if target in FOLDERS:
            run("open_path", {"path": target})
            return f"Your {target} folder, sir."
        if free and re.fullmatch(r"[\w-]+\.(com|org|net|in|io|ai|dev|co|edu|gov)(/\S*)?", target):
            run("open_website", {"url": target})
            return f"Opening {target}."
    if m and m.group(1) not in ("go to", "take me to", "visit"):     # those three are only for sites and folders
        # Known app names go straight through. (The word filter below used to reject "file explorer" because it
        # contains "file", so the request fell to the language model, which said "Opening File Explorer" and did
        # nothing.)
        known = _known_target(target)
        if known or (free and len(target.split()) <= 4 and not re.search(r"\b(file|and|then|in|on|with)\b", target)):
            result = run("open_app", {"name": target})
            if result.startswith("Opened"):
                shown = result[7:].rstrip(".").title() if not known else target.title()
                if tools.ALIASES.get(target) == "chrome":            # "open crow" / "open curl": he heard Chrome
                    shown = "Chrome"
                return random.choice([f"Opening {shown}, sir.", f"{shown} coming up.", f"Launching {shown}."])
            # Tell the language model what was already tried, so it explains or asks instead of opening
            # something else in its place (it once answered "open the flux capacitor" by launching Chrome).
            Ctx.note = f"an app called '{target}' was looked for just now and is not installed on this computer"
        return None

    # --- volume
    if re.fullmatch(r"(volume up|turn (it |the volume )?up|louder|increase (the )?volume|raise (the )?volume)", t):
        run("volume", {"action": "up"})
        return "Volume up."
    if re.fullmatch(r"(volume down|turn (it |the volume )?down|quieter|softer|decrease (the )?volume|lower (the )?volume|reduce (the )?volume)", t):
        run("volume", {"action": "down"})
        return "Volume down."
    if re.fullmatch(r"(mute|unmute|mute (the )?(volume|sound|audio|speakers?|computer|laptop)|silence)", t):
        run("volume", {"action": "mute"})
        return "Done, sir."
    m = re.fullmatch(r"(?:set|turn|change|put)?\s*(?:the\s+)?volume\s*(?:to|at)?\s*(.+?)(?:\s*percent|%)?", t)
    if m and (n := _num(m.group(1))) is not None:
        run("volume", {"action": "set", "level": n})
        return f"Volume at {n} percent."
    m = re.fullmatch(r"(?:set|turn|change|put)?\s*(?:the\s+)?(?:screen\s+)?brightness\s*(?:to|at)?\s*(.+?)(?:\s*percent|%)?", t)
    if m and (n := _num(m.group(1))) is not None:
        return run("set_brightness", {"level": n})

    # --- media
    if re.fullmatch(r"(pause|play|resume|pause (the )?(music|song|video|it)|play (the )?music|resume (the )?(music|song|video)|"
                    r"continue playing|stop (the )?(music|song|video))", t):
        run("media_control", {"action": "play_pause"})
        return random.choice(["Done, sir.", "Of course."])
    if re.fullmatch(r"(next|skip|next (song|track|one|video)|skip (this|the) (song|track|one)|skip (it|this))", t):
        run("media_control", {"action": "next"})
        return "Next track."
    if re.fullmatch(r"(previous|previous (song|track|one)|last (song|track)|go back a (song|track))", t):
        run("media_control", {"action": "previous"})
        return "Previous track."
    if strict:
        m = None                                         # from here on, free-form text is skipped in strict mode
    else:
        m = re.fullmatch(r"play\s+(.+?)(?:\s+on\s+youtube)?", t)
    if m and m.group(1) not in ("something", "some music", "a song", "music", "it"):
        return run("play_on_youtube", {"query": m.group(1)}).replace("Playing", "Playing").rstrip(".") + ", sir."

    # --- search
    m = re.fullmatch(r"(?:search(?: the web| google| online)?(?: for)?|google|look up)\s+(.+?)(?:\s+on\s+(google|youtube))?", t) \
        if free else None
    if m:
        run("web_search", {"query": m.group(1), "site": m.group(2) or "google"})
        return f"Here's what I found for {m.group(1)}, sir."

    # --- screen / windows
    if re.fullmatch(r"lock( the| my)?( screen| computer| laptop| pc| system)?( down)?", t) and t != "lock":
        run("lock_screen", {})
        return "Locking up, sir."
    if re.fullmatch(r"(take|grab|capture) a? ?(screenshot|screen shot|screen capture)|screenshot", t):
        r = run("screenshot", {})
        return "Screenshot saved to your Pictures folder, sir." if "saved" in r else r
    if re.fullmatch(r"(show (me )?(the )?desktop|minimi[sz]e (everything|all( windows)?)|hide (everything|all windows)|clear (the|my) screen)", t):
        run("window_control", {"action": "show_desktop"})
        return "Clearing the desk, sir."
    m = re.fullmatch(r"(minimi[sz]e|maximi[sz]e|restore)( this| the)?( window| app)?", t)
    if m:
        run("window_control", {"action": {"minimise": "minimize", "maximise": "maximize"}.get(m.group(1), m.group(1))})
        return "Done."
    if re.fullmatch(r"(switch|change) (the )?(window|app|application)s?|alt tab", t):
        run("window_control", {"action": "switch"})
        return "Switched."
    m = re.fullmatch(r"(?:scroll|go|page|move)\s+(?:the page\s+)?(?:back\s+)?(up|down)(?: (a bit|a little|slightly|more|a lot|further|fast))?"
                     r"(?: (?:in|on) .+)?", t)
    if m:
        amount = {"a bit": "small", "a little": "small", "slightly": "small", "a lot": "large", "fast": "large"}.get(m.group(2), "page")
        run("scroll", {"direction": m.group(1), "amount": amount})
        return random.choice(["Scrolling.", f"Scrolling {m.group(1)}, sir."])
    m = re.fullmatch(r"(?:scroll|go|jump)(?: back)? to (?:the )?(top|bottom|end|start|beginning)(?: of (?:the )?page)?", t)
    if m:
        run("scroll", {"direction": "top" if m.group(1) in ("top", "start", "beginning") else "bottom", "amount": "page"})
        return "Done, sir."
    if strict:
        return None
    m = re.fullmatch(r"(?:press|hit|tap)\s+(?:the\s+)?(.+?)(?:\s+(?:key|button))?", t)
    if m:
        keys = re.sub(r"\s*(?:plus|and|\+)\s*", "+", m.group(1)).replace("control", "ctrl").replace("escape", "esc")
        r = run("press_keys", {"keys": keys})
        if r.startswith("Pressed"):
            return "Done, sir."
    said = FILLER.sub("", POLITE.sub("", FILLER.sub("", text.strip())))    # keep the tail exactly as dictated
    m = re.fullmatch(r"(?:type|write down|dictate)\s+(.+)", said.rstrip("."), re.I)
    if m and len(m.group(1)) < 300 and not re.match(r"(a|an|me|up|out)\s", m.group(1), re.I):
        run("type_text", {"text": m.group(1)})
        return "Typed, sir."

    # --- notes
    m = re.fullmatch(r"(?:take|make|write|save) a note(?: that| saying|:)?\s+(.+)|note(?: that| down)?\s+(.+)", t)
    if m:
        run("take_note", {"text": (m.group(1) or m.group(2)).strip()})
        return "Noted, sir."
    return None
