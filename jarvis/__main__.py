"""JARVIS entry point.
    python -m jarvis               voice + HUD (say "Jarvis", click the reactor, or press Ctrl+Alt+J)
    python -m jarvis --text        no microphone: type commands in the HUD / console
    python -m jarvis --mute        no spoken replies
    python -m jarvis --no-hud      console only
    python -m jarvis --no-security assistant only
    python -m jarvis --check       offline self-test of every component (does not start Jarvis)
    python -m jarvis --mic-test    show microphone level and what the recognisers hear for 12 seconds
    python -m jarvis --train-wake  teach Jarvis how *you* pronounce the wake word
"""
import argparse
import ctypes
import datetime as dt
import logging
import queue
import random
import re
import sys
import threading
import time

from .config import ROOT, load_config, resolve
from .state import STATE

YES = re.compile(r"\b(yes|yeah|yep|yup|sure|do it|confirm|confirmed|go ahead|okay|ok|affirmative|proceed|please do|"
                 r"absolutely|of course|haan|ha)\b", re.I)
NO = re.compile(r"\b(no|nope|cancel|stop|don't|do not|negative|abort|never ?mind|nahi)\b", re.I)
EXIT = re.compile(r"(exit|quit|good ?bye|bye|shut ?down jarvis|go offline|power down jarvis|close jarvis|"
                  r"that will be all|go to sleep)", re.I)
END = re.compile(r"(stop|cancel|never ?mind|nothing|that's all|that is all|shut up|quiet|be quiet|forget it|"
                 r"no thanks|no thank you|not now|stop talking|enough)", re.I)
THANKS = re.compile(r"(thanks|thank you|thanks a lot|thank you so much|cheers|good job|well done|nice work)"
                    r"( jarvis)?", re.I)
ACKS = ["Sir?", "Yes, sir?", "At your service, sir.", "I'm listening, sir.", "For you, sir, always."]
# "Stay with me": keep listening after every reply (no wake word needed) until told that's all.
CONVERSE = re.compile(r"(stay with me|keep listening|conversation mode|let'?s (have a )?(talk|chat)|stay awake|"
                      r"don'?t go( away)?|listen( to me)?( for a (while|bit|moment))?)", re.I)
STOCK = ["Right away, sir.", "Of course, sir.", "Certainly, sir.", "As you wish, sir.", "Consider it done.",
         "Very good, sir.", "Done, sir.", "One moment, sir.", "Working on it, sir.", "Let me see.", "Just a moment, sir.",
         "My pleasure, sir.", "Standing by."] + ACKS
SHORT_OK = re.compile(r"(yes|no|stop|pause|play|next|mute|skip|thanks|cancel|louder|quieter|screenshot|resume)", re.I)


def setup_logging():
    (ROOT / "data").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(ROOT / "data" / "jarvis.log", encoding="utf-8")])
    logging.getLogger("comtypes").setLevel(logging.WARNING)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("  [%(name)s] %(message)s"))
    logging.getLogger().addHandler(console)


def greeting_word() -> str:
    h = dt.datetime.now().hour
    return "Good morning" if 4 <= h < 12 else "Good afternoon" if 12 <= h < 17 else "Good evening"


class Jarvis:
    def __init__(self, args):
        from .alerts import AlertBus
        from .speech import Speaker
        from .tools import Ctx
        self.args = args
        self.cfg = cfg = load_config()
        self.owner = cfg["assistant"]["owner_name"]
        self.voice_mode = cfg["voice"]["enabled"] and not args.text
        self.events: queue.Queue = queue.Queue()
        self.speaker = Speaker(cfg["voice"], enabled=not args.mute)
        self.alerts = AlertBus(cfg, self.speaker)
        self.security = None
        if not args.no_security:
            from .security import SecurityManager
            self.security = SecurityManager(cfg, self.alerts)
        self.listener = None
        self.brain = None
        self.matcher = None
        self.hud = None
        self.deadline = 0.0          # while now < deadline Jarvis is "attentive": no wake word needed
        self.running = True
        self.ack_due = 0.0           # the wake word was just heard: acknowledge at this time unless a command follows
        self.converse = False        # conversation mode: keep listening after each reply
        self.alt = None              # the other recogniser's version of the command being handled
        self.heard_at = 0.0          # when the current command finished being spoken (for the latency log)
        Ctx.cfg, Ctx.speaker, Ctx.alerts, Ctx.security = cfg, self.speaker, self.alerts, self.security

    # ------------------------------------------------------------------ boot
    def boot(self):
        from .brain import Brain
        from .hotkey import start_hotkey
        from .reminders import Reminders
        from .tools import Ctx, notify
        cfg = self.cfg
        STATE.boot("J.A.R.V.I.S. boot sequence initiated")

        self.speaker.ready.wait(60)
        STATE.boot(f"Voice synthesis ({self.speaker.engine_name}) ... ONLINE")

        if self.voice_mode:
            from .speech import Listener, WhisperSTT
            from .wake import WakeMatcher
            model = resolve(cfg["voice"]["vosk_model"])
            if not model.exists():
                STATE.boot("Speech model missing: run setup_models.py. Text mode only.")
                self.voice_mode = False
            else:
                whisper = None
                if cfg["voice"].get("stt_engine", "whisper") == "whisper":
                    try:
                        whisper = WhisperSTT(cfg["voice"]["whisper_model"], cfg["voice"].get("whisper_language", "en"))
                        STATE.boot("Speech recognition (Whisper) ... ONLINE")
                    except Exception as e:
                        logging.getLogger("jarvis").error("Whisper unavailable: %s", e)
                        STATE.boot("Whisper unavailable; using Vosk only")
                self.matcher = WakeMatcher(cfg["assistant"]["wake_words"],
                                           fuzzy=cfg["voice"].get("wake_fuzzy", 0.5),
                                           whisper_verify=whisper is not None,
                                           soft=cfg["voice"].get("neural_wake_soft", 0.3))
                self.listener = Listener(cfg["voice"], self.speaker, self.events, whisper)
                # Followed word by word: a phrase led by his name is meant for him even before he is "awake", and a
                # phrase that already reads as a complete known command can end after a short pause.
                self.listener.for_me = self.matcher.led_by_variant
                self.listener.is_command = lambda partial: self.quick_text(partial) is not None
                self.listener.start()
                STATE.boot("Microphone and wake word ... ONLINE"
                           + (" (neural 'Hey Jarvis' detector active)" if self.listener.neural else ""))

        def fire(msg):
            notify("Jarvis reminder", msg)
            self.speaker.say("Sir, your timer is up." if msg.lower().strip() == "timer" else f"Sir, a reminder: {msg}")
        Ctx.reminders = Reminders(fire)

        if cfg.get("agenda", {}).get("enabled", True):
            from .agenda import Agenda
            Ctx.agenda = Agenda(cfg, announce=self.announce)
            Ctx.agenda.start()
            STATE.boot("Calendar and Gmail watch ... " + ("ACTIVE" if Ctx.agenda.connected()
                                                          else "not connected (run: jarvis connect-google)"))

        cr = cfg.get("coursera", {})
        if cr.get("enabled", True):
            from .coursera import BRIDGE
            if BRIDGE.start(int(cr.get("port", 8765)), on_event=lambda msg: self.speaker.say(f"Sir, {msg[0].lower()}{msg[1:]}")):
                STATE.boot("Coursera link ... STANDING BY")

        if self.security:
            self.security.start()
            if self.security.face:
                self.security.face.on_owner_return = self.welcome_back
            STATE.boot(f"Security grid ({len(self.security.monitors)} monitors) ... ACTIVE")

        self.brain = Brain(cfg, self.confirm)
        STATE.boot(f"Language core ({cfg['llm']['model']}) ... loading")
        brain_ok = self.brain.warm_up()
        STATE.boot("Language core ... " + ("ONLINE" if brain_ok else "OFFLINE (is Ollama installed and the model pulled?)"))

        combo = cfg["voice"].get("push_to_talk", "ctrl+alt+j")
        if self.voice_mode and combo:
            # the hotkey also brings a hidden orb back, so it can never be lost
            start_hotkey(combo, lambda: (STATE.ui("show"), self.events.put(("ptt", None))))
        from . import tools
        threading.Thread(target=tools.refresh_apps, daemon=True, name="apps").start()   # so "open ..." is instant
        self.speaker.precache(STOCK)                     # stock phrases are synthesised ahead, ready to play at once
        threading.Thread(target=self.console_reader, daemon=True, name="console").start()
        threading.Thread(target=self.sentinel, daemon=True, name="sentinel").start()

        STATE.booting = False
        self.speaker.say(self.briefing(brain_ok))

    def briefing(self, brain_ok: bool) -> str:
        from .tools import _battery_text
        now = dt.datetime.now().strftime("%I:%M %p").lstrip("0")
        parts = [f"{greeting_word()}, {self.owner}. Jarvis is online and all systems are operational."
                 if brain_ok else
                 f"{greeting_word()}, {self.owner}. Jarvis is online, but I can't reach my language core. "
                 f"Simple commands still work; please check that Ollama is running."]
        bits = [f"It is {now}"]
        bat = _battery_text()
        if bat:
            m = re.match(r"Battery (\d+)% (charging|on battery)", bat)
            if m:
                charging = m.group(2) == "charging" and int(m.group(1)) < 100
                bits.append(f"power is at {m.group(1)} percent" + (" and charging" if charging else ""))
        if self.security:
            bits.append(f"{len(self.security.monitors)} security monitors are standing guard")
        parts.append(", ".join(bits[:-1]) + (", and " if len(bits) > 1 else "") + bits[-1] + ".")
        from .tools import Ctx
        pend = len(Ctx.reminders.pending()) if Ctx.reminders else 0
        if pend:
            parts.append(f"You have {pend} pending reminder{'s' if pend > 1 else ''}.")
        if Ctx.agenda and Ctx.agenda.connected():
            # The first sync starts with the boot; give it a few seconds so the greeting can mention deadlines.
            end = time.time() + 6
            while not Ctx.agenda.synced_at and time.time() < end:
                time.sleep(0.2)
            line = Ctx.agenda.briefing()
            if line:
                parts.append(line)
        return " ".join(parts)

    def quiet_hours(self) -> bool:
        start, end = (self.cfg.get("proactive", {}).get("quiet_hours") or [23, 7])[:2]
        hour = dt.datetime.now().hour
        return (start <= hour or hour < end) if start > end else (start <= hour < end)

    def announce(self, msg: str, urgent: bool = False):
        """Deadlines, calendar and email news (agenda.py). Always a Windows notification; spoken as soon as Jarvis
        is free (not over a reply, a command being spoken or a conversation), except during quiet hours, when it is
        only shown, unless [agenda] speak_in_quiet_hours = true and it is urgent."""
        from .tools import notify
        logging.getLogger("jarvis").info("agenda: %s", msg)
        try:
            notify("Jarvis", msg.removeprefix("Sir, "))
        except Exception:
            pass
        STATE.add("SYS", msg)
        if self.quiet_hours() and not (urgent and self.cfg.get("agenda", {}).get("speak_in_quiet_hours", False)):
            return

        def wait_then_say():
            give_up = time.time() + 180
            while time.time() < give_up and self.running:
                busy = (self.speaker.busy or STATE.thinking or (self.listener and self.listener.hearing)
                        or time.time() < self.deadline or STATE.booting)
                if not busy:
                    self.speaker.say(msg, log_it=False)
                    return
                time.sleep(0.5)
        threading.Thread(target=wait_then_say, daemon=True, name="announce").start()

    def sentinel(self):
        """Proactive remarks, the way the film's Jarvis volunteers things: low power warnings."""
        import socket
        import psutil
        warned = 101                      # lowest threshold already announced
        pro = self.cfg.get("proactive", {})

        def online() -> bool:
            for host in (("1.1.1.1", 53), ("8.8.8.8", 53)):
                try:
                    socket.create_connection(host, timeout=2.5).close()
                    return True
                except OSError:
                    continue
            return False

        def volunteer(msg: str):
            """Say something unprompted, unless he is busy, mid-conversation, or it is the middle of the night
            (quiet hours: the remark is shown under the orb instead)."""
            start, end = (pro.get("quiet_hours") or [23, 7])[:2]
            hour = dt.datetime.now().hour
            asleep = (start <= hour or hour < end) if start > end else (start <= hour < end)
            if asleep or self.speaker.busy or STATE.thinking or time.time() < self.deadline:
                STATE.caption("SYS", msg)
            else:
                self.speaker.say(msg)

        net = plugged = full = None
        misses = ticks = 0
        while self.running:
            time.sleep(20)
            ticks += 1
            # --- network: say so when the connection drops or returns (two misses in a row, so a blip is ignored)
            if pro.get("network", True) and ticks % 2 == 0:
                up = online()
                misses = 0 if up else misses + 1
                if net is None:
                    net = up
                elif up and not net:
                    net = True
                    volunteer("We're back online, sir.")
                elif not up and net and misses >= 2:
                    net = False
                    volunteer("Sir, we've lost the internet connection.")
            try:
                bat = psutil.sensors_battery()
            except Exception:
                bat = None
            if not bat:
                continue
            # --- charger plugged / unplugged, battery full: a quiet caption under the orb, not a speech
            if plugged is not None and bat.power_plugged != plugged and pro.get("power", True):
                STATE.caption("SYS", f"Power connected. Battery at {bat.percent:.0f}%." if bat.power_plugged
                              else f"Running on battery: {bat.percent:.0f}%.")
            plugged = bat.power_plugged
            if bat.power_plugged and bat.percent >= 100 and full is False and pro.get("power", True):
                STATE.caption("SYS", "Battery fully charged.")
            full = bat.percent >= 100
            if ticks % 3:
                continue                  # the spoken low-power warnings are checked once a minute
            if bat.power_plugged:
                warned = 101
                continue
            for level, msg in ((10, "Sir, power is critical at {p} percent. Please plug in now."),
                               (20, "Sir, power is down to {p} percent. I'd recommend plugging in.")):
                if bat.percent <= level < warned:
                    warned = level
                    volunteer(msg.format(p=int(bat.percent)))
                    break

    def welcome_back(self, away: float):
        if self.speaker.busy or STATE.thinking:
            return
        since = time.time() - away
        n = sum(1 for a in self.alerts.summary(50)
                if a["severity"] in ("medium", "high", "critical")
                and time.mktime(time.strptime(a["time"], "%Y-%m-%d %H:%M:%S")) >= since)
        msg = f"Welcome back, {self.owner}."
        if n:
            msg += f" There {'was' if n == 1 else 'were'} {n} security alert{'s' if n > 1 else ''} while you were away."
        self.speaker.say(msg)

    # ------------------------------------------------------------------ input helpers
    def console_reader(self):
        while self.running:
            try:
                line = input()
            except (EOFError, OSError, RuntimeError):
                return
            except KeyboardInterrupt:
                self.events.put(("quit", None))
                return
            if line.strip():
                self.events.put(("text", line.strip()))

    def drain_speech(self):
        """Throw away phrases captured before now (keeps typed commands and system events)."""
        keep = []
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            if ev[0] != "utt":
                keep.append(ev)
        for ev in keep:
            self.events.put(ev)

    def attend(self, seconds: float):
        # never shortens: a chime or a second "Hey Jarvis" must not cut a 45 s conversation window down to 8 s
        self.deadline = max(self.deadline, time.time() + seconds)
        STATE.attentive = True

    def acknowledge(self):
        """Answer the bare wake word, then listen for the command. wake_ack = "chime" (default): a soft two-note
        tone, at once. "voice": a spoken "Sir?" as before (about 1.5 s slower). "none": the orb lighting up only."""
        cfg_v = self.cfg["voice"]
        timeout = cfg_v.get("command_timeout", 8)
        mode = cfg_v.get("wake_ack", "chime")
        self.ack_due = 0.0
        self.attend(timeout)
        if mode == "voice":
            # If the user is already carrying on ("Jarvis ... open Chrome"), don't talk over them: speaking mutes
            # the microphone and the command would be lost.
            end = time.time() + 0.7
            while time.time() < end:
                if self.listener.hearing or not self.events.empty():
                    return
                time.sleep(0.05)
            self.speaker.say(random.choice(ACKS), log_it=False)
            self.speaker.wait(5)
            self.drain_speech()
        elif mode == "chime":
            if self.listener and self.listener.hearing:
                return                                   # already speaking the command: just listen
            self.speaker.chime("wake")
            self.speaker.wait(2)
        self.attend(timeout)

    def prewarm(self):
        if self.brain:
            self.brain.prewarm()

    def quick_text(self, rough: str) -> str | None:
        """If the fast recogniser's rough transcript already reads as a clear, known command ("volume up",
        "open chrome", "scroll down"), use it as it is: no need to wait ~0.4 s for Whisper to agree."""
        if not self.cfg["voice"].get("fast_reflex", True):
            return None
        from . import router
        text = self.matcher.strip(rough).strip()
        return text if text and router.probe(text) else None

    def confirm(self, question: str) -> bool:
        STATE.thinking = False
        self.speaker.say(question)
        self.speaker.wait()
        self.drain_speech()
        STATE.attentive = True
        ans, end = "", time.time() + 12
        while time.time() < end:
            try:
                kind, payload = self.events.get(timeout=max(0.05, end - time.time()))
            except queue.Empty:
                break
            if kind == "text":
                ans = payload
                break
            if kind == "utt":
                ans = (self.listener.accurate_text(payload) or payload.text) if self.listener else payload.text
                if ans:
                    break
            elif kind == "quit":
                self.events.put((kind, payload))
                break
        if ans:
            STATE.add("YOU", ans)
            print(f"YOU: {ans}", flush=True)
        STATE.thinking = True
        return bool(YES.search(ans)) and not NO.search(ans)

    # ------------------------------------------------------------------ main loop
    def loop(self):
        try:
            self.boot()
            self.speaker.wait()
            if self.voice_mode:
                self.drain_speech()
            while self.running:
                attentive = time.time() < self.deadline
                STATE.attentive = attentive
                try:
                    kind, payload = self.events.get(timeout=0.2)
                except queue.Empty:
                    # "Hey Jarvis" was heard a moment ago and nothing has followed: acknowledge. (When the phrase
                    # itself arrives from the recogniser first, it is acknowledged there and this is cancelled.)
                    if self.ack_due and time.time() >= self.ack_due:
                        if self.listener and self.listener.hearing:
                            self.ack_due = time.time() + 0.3     # still talking: wait for them to finish
                        else:
                            self.acknowledge()
                    continue
                # A phrase that *started* inside the listening window counts, even if it finished after it.
                if kind == "utt" and time.time() - payload.duration - 1.0 < self.deadline:
                    attentive = True
                text = self.interpret(kind, payload, attentive)
                if text:
                    self.respond(text)
        except Exception:
            logging.getLogger("jarvis").exception("main loop crashed")
            print("Jarvis hit an internal error; see data/jarvis.log")
        finally:
            self.running = False
            if self.hud:
                self.hud.close()

    def interpret(self, kind: str, payload, attentive: bool) -> str | None:
        """Turn an event into the text of a command (or None if there's nothing to act on)."""
        cfg_v = self.cfg["voice"]
        if kind == "quit":
            self.speaker.stop()
            self.speaker.say(f"Goodbye, {self.owner}.")
            self.speaker.wait(8)
            self.running = False
            return None
        if kind == "system":
            self.speaker.say(payload)
            return None
        if kind == "ptt":
            self.speaker.stop()
            if not self.listener:
                self.speaker.say(f"The microphone is switched off, {self.owner}. You can type to me instead.")
                return None
            self.prewarm()
            self.drain_speech()
            if cfg_v.get("wake_ack", "chime") == "voice":
                self.speaker.say(random.choice(ACKS[:2]), log_it=False)
                self.speaker.wait(5)
                self.drain_speech()
            elif cfg_v.get("wake_ack", "chime") == "chime":
                self.speaker.chime("wake")
                self.speaker.wait(2)
            self.ack_due = 0.0
            self.attend(cfg_v.get("command_timeout", 8))
            return None
        if kind == "wake":
            # The neural detector heard "Hey Jarvis" this instant (the rest of the sentence may still be coming).
            # Light up straight away and get the language model ready; the phrase itself follows as an "utt".
            if not self.speaker.speaking.is_set():
                self.attend(cfg_v.get("command_timeout", 8))
                self.ack_due = time.time() + 1.3
                self.prewarm()
            return None
        if kind == "barge":
            # "Hey Jarvis" said over his own voice: stop talking and listen.
            self.speaker.stop()
            self.ack_due = 0.0
            self.attend(cfg_v.get("command_timeout", 8))
            return None
        if kind == "text":
            self.speaker.stop()
            self.alt, self.heard_at = None, time.time()
            return payload.strip()
        if kind != "utt" or self.speaker.speaking.is_set():
            return None

        utt = payload
        self.ack_due = 0.0
        self.heard_at = time.time()
        woke = utt.neural_at is not None                 # the neural detector fired inside this very phrase
        if attentive and not woke:                       # mid-conversation: no wake word needed
            quick = self.quick_text(utt.text)
            if quick:
                self.alt = None
                return quick
            raw = self.listener.accurate_text(utt) or ""
            text = self.matcher.strip(raw).strip()
            bare = text.strip(" .!?,")
            if not bare:
                if raw and self.matcher.whisper_says_wake(raw):
                    self.acknowledge()                   # "Jarvis?" again: keep listening
                return None
            if len(bare.split()) == 1 and len(bare) < 3 and not SHORT_OK.fullmatch(bare):
                return None                              # a stray noise / half word: ignore
            self.alt = self.matcher.strip(utt.text)
            return text

        verdict = "yes" if woke else self.matcher.check(utt.text, utt.grammar_hit, utt.duration, utt.neural)
        if verdict == "no":
            return None
        # Nothing but the wake word? Then answer at once, without waiting for Whisper. Either Vosk heard just
        # "jarvis", or the detector fired and at most a word or two was said ('dallas', 'yeah race': his name, misheard).
        full = rest = ""
        quick = self.quick_text(utt.text) if verdict == "yes" else None
        if quick:
            rest = quick                                 # "hey jarvis open chrome": clear already, skip Whisper
        elif not (verdict == "yes" and (self.matcher.only_wake(utt.text) or
                                        (woke and len(utt.text.split()) <= 2 and utt.duration - utt.neural_at < 1.6))):
            full = self.listener.accurate_text(utt)
            if verdict == "maybe" and not (full and self.matcher.whisper_says_wake(full)):
                return None
            rest = self.matcher.strip(full or utt.text).strip(" ,.!?")
            if woke and not (full and self.matcher.whisper_says_wake(full)):
                # The detector heard the wake phrase but Whisper wrote something else for it ("Cabas?", "Cheers.").
                # Go by timing instead: whatever was said after the phrase is the command.
                if full and len(utt.text.split()) >= 3 and utt.duration - utt.neural_at > 0.8:
                    rest = self.matcher.strip(self.listener.tail_text(utt, utt.neural_at - 0.3)).strip(" ,.!?")
                else:
                    rest = ""
            elif (utt.grammar_hit and full and not self.matcher.whisper_says_wake(full)
                    and not self.matcher.led_by_variant(utt.text)):
                # Only the grammar recogniser heard "Jarvis" (short phrase, e.g. free text "no race"): that was
                # the wake word itself said with an accent, not a command to act on.
                rest = ""
        logging.getLogger("jarvis").info("wake word (%s%s): vosk=%r whisper=%r", verdict,
                                         f", neural {utt.neural:.2f}" if utt.neural >= 0.2 else "", utt.text, full)
        self.prewarm()
        if len(rest) < 3:
            self.acknowledge()                           # just the wake word: chime (or "Sir?") and listen
            return None
        self.alt = None if quick else self.matcher.strip(utt.text)
        return rest

    def respond(self, text: str):
        text = text.strip()
        low = text.lower().strip(" .!?,")
        STATE.add("YOU", text)
        print(f"\nYOU: {text}", flush=True)
        if EXIT.fullmatch(low):
            self.events.put(("quit", None))
            return
        if END.fullmatch(low):
            self.speaker.stop()
            self.speaker.say(random.choice(["Very good, sir.", "As you wish, sir.", "Standing by."]))
            self.deadline = 0
            self.converse = False
            return
        if THANKS.fullmatch(low):
            self.speaker.say(random.choice(["My pleasure, sir.", "Always a pleasure, sir.", "Of course, sir."]))
            self.deadline = 0
            self.converse = False
            return
        if CONVERSE.fullmatch(low) and self.voice_mode:
            self.converse = True
            self.speaker.say("I'm all ears, sir. Just say that's all when you're done.")
            self.speaker.wait()
            self.drain_speech()
            self.attend(self.cfg["voice"].get("conversation_seconds", 45))
            return
        STATE.thinking = True
        t0 = time.time()
        spoke = threading.Event()
        first = [0.0]
        alt, self.alt = self.alt, None

        def speak(sentence: str):
            if not spoke.is_set():
                first[0] = time.time()
            spoke.set()
            self.speaker.say(sentence, log_it=False)

        def filler():
            # Thinking is taking a while: say so rather than leaving dead air (only if nothing has been said yet).
            if not spoke.is_set() and STATE.thinking and not self.speaker.busy:
                self.speaker.say(random.choice(["One moment, sir.", "Working on it, sir.", "Let me see.",
                                                "Just a moment, sir."]), log_it=False)
        timer = threading.Timer(self.cfg["voice"].get("filler_after", 3.5), filler)
        timer.daemon = True
        timer.start()
        try:
            reply = self.brain.handle(text, speak=speak, alt=alt)
        finally:
            timer.cancel()
            STATE.thinking = False
        STATE.add("JARVIS", reply)
        # Where the time went: from the end of the spoken command to understanding it, and to the first word back.
        heard = self.heard_at or t0
        logging.getLogger("jarvis").info(
            "handled %r in %.1fs (understood +%.1fs, first words +%.1fs after you stopped speaking) -> %r",
            text, time.time() - t0, t0 - heard, (first[0] or time.time()) - heard, reply)
        self.speaker.wait()
        follow = self.cfg["voice"].get("follow_up_seconds", 7)
        if self.converse:
            follow = self.cfg["voice"].get("conversation_seconds", 45)
        if self.voice_mode and follow:
            self.drain_speech()
            self.attend(follow)                           # keep listening for a follow-up, no wake word needed


# ====================================================================== utilities (never start the assistant)
def self_check():
    """Verify every component offline, without opening the microphone/camera or starting the monitors."""
    import importlib
    cfg = load_config()
    ok = True

    def line(name, good, detail=""):
        nonlocal ok
        ok = ok and good
        print(f"  [{'OK' if good else 'FAIL'}] {name}{' - ' + detail if detail else ''}")

    print("JARVIS self-check")
    for mod in ("vosk", "sounddevice", "numpy", "cv2", "psutil", "watchdog", "onnxruntime", "onnx_asr", "piper", "tkinter"):
        try:
            importlib.import_module(mod)
            line(f"module {mod}", True)
        except Exception as e:
            line(f"module {mod}", False, str(e))
    v = cfg["voice"]
    line("Vosk model", resolve(v["vosk_model"]).exists(), str(resolve(v["vosk_model"])))
    line("Piper voice", resolve(v["piper_voice"]).exists(), str(resolve(v["piper_voice"])))
    try:
        from .speech import WhisperSTT
        t = time.time()
        WhisperSTT(v["whisper_model"], v.get("whisper_language", "en"))
        line("Whisper", True, f"{v['whisper_model']} loaded in {time.time() - t:.1f}s")
    except Exception as e:
        line("Whisper", False, str(e))
    try:
        import sounddevice as sd
        d = sd.query_devices(kind="input")
        line("Default microphone", True, d["name"])
    except Exception as e:
        line("Default microphone", False, str(e))
    from .security.face_engine import FaceEngine
    try:
        enrolled = FaceEngine().owner is not None
        line("Face models / enrolment", True, "owner enrolled" if enrolled else
             "models OK; no face enrolled yet (optional: jarvis enroll-face)")
    except Exception as e:
        line("Face models", False, str(e))
    try:
        import json
        import urllib.request
        tags = json.loads(urllib.request.urlopen(cfg["llm"]["host"].rstrip("/") + "/api/tags", timeout=3).read())
        names = [m["name"] for m in tags.get("models", [])]
        line("Ollama", cfg["llm"]["model"] in names, f"models: {', '.join(names) or 'none'}")
    except Exception as e:
        line("Ollama", False, f"not running ({e}); Jarvis will try to start it himself")
    from .tools import REGISTRY
    line("Tools", True, f"{len(REGISTRY)} registered")
    try:
        from .agenda import load_secrets
        g = load_secrets()
        cals = ", ".join(c.get("name", "?") for c in g.get("calendars", []))
        line("Gmail / Calendar (optional)", True,
             (f"Gmail {g['email']}" if g.get("email") else "Gmail not linked") + "; " +
             (f"calendars: {cals}" if cals else "no calendars") + ("" if g else " (link them: jarvis connect-google)"))
    except Exception as e:
        line("Gmail / Calendar", False, str(e))
    try:
        import numpy as np
        from . import wakeword
        folder = resolve(v.get("wake_model_dir", "models/wakeword"))
        if wakeword.available(folder):
            det = wakeword.NeuralWake(folder)
            det.feed(np.zeros(16000, np.int16))
            line("Neural wake word ('Hey Jarvis')", True, "loaded")
        else:
            line("Neural wake word ('Hey Jarvis')", False, f"models missing in {folder}: run setup_models.py")
    except Exception as e:
        line("Neural wake word", False, str(e))
    try:
        if sys.platform != "win32":
            raise ImportError("skip")              # the floating orb is Windows-only; other systems use the window
        from .orb import OrbRenderer
        r = OrbRenderer(360)
        for _ in range(5):
            r.render(1 / 30, "speaking", 0.6)
        t = time.time()
        for _ in range(20):
            r.render(1 / 30, "speaking", 0.6)
        line("Orb renderer", True, f"{(time.time() - t) / 20 * 1000:.0f} ms per frame")
    except ImportError as e:
        if str(e) != "skip":
            line("Orb renderer", False, str(e))
    except Exception as e:
        line("Orb renderer", False, str(e))
    if sys.platform == "darwin":
        from . import macos
        line("AppleScript (apps, volume, windows)", not macos.osa("return 1").startswith("error:"))
        try:
            import Quartz  # noqa: F401
            import Vision  # noqa: F401
            line("PyObjC (screen reading, media keys)", True)
        except Exception as e:
            line("PyObjC (screen reading, media keys)", False, str(e))
    print("All good." if ok else "Some checks failed (see above).")
    return 0 if ok else 1


def mic_test(seconds: int = 12):
    import json
    import numpy as np
    import sounddevice as sd
    import vosk
    from .speech import Listener, WhisperSTT
    from .wake import WakeMatcher
    cfg = load_config()
    v = cfg["voice"]
    print("Input devices:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"   {i}: {d['name']}")
    dev = Listener.find_device(v.get("input_device", ""))
    print(f"Using: {sd.query_devices(dev, 'input')['name']}\nLoading models ...")
    vosk.SetLogLevel(-1)
    model = vosk.Model(str(resolve(v["vosk_model"])))
    full = vosk.KaldiRecognizer(model, 16000)
    gram = vosk.KaldiRecognizer(model, 16000, json.dumps(["jarvis", "hey jarvis", "[unk]"]))
    gram.SetWords(True)
    whisper = WhisperSTT(v["whisper_model"], v.get("whisper_language", "en"))
    matcher = WakeMatcher(cfg["assistant"]["wake_words"], soft=v.get("neural_wake_soft", 0.3))
    neural = None
    try:
        from . import wakeword
        folder = resolve(v.get("wake_model_dir", "models/wakeword"))
        if v.get("neural_wake", True) and wakeword.available(folder):
            neural = wakeword.NeuralWake(folder)
    except Exception as e:
        print(f"(neural wake word unavailable: {e})")
    threshold = float(v.get("neural_wake_threshold", 0.5))
    gain = float(v.get("mic_gain", 1.0))
    print(f"\nSpeak now for {seconds} seconds: say 'Hey Jarvis' a few times, then 'Hey Jarvis, what time is it'.\n")
    buf, peak, end, best = bytearray(), 0.0, time.time() + seconds, 0.0
    with sd.RawInputStream(samplerate=16000, blocksize=1600, device=dev, dtype="int16", channels=1) as stream:
        while time.time() < end:
            data, _ = stream.read(1600)
            a = np.frombuffer(bytes(data), dtype=np.int16).astype(np.float32) * gain
            data = np.clip(a, -32768, 32767).astype(np.int16).tobytes()
            lvl = float(np.abs(a).mean()) / 32768
            peak = max(peak, lvl)
            if neural:
                best = max(best, neural.feed(np.frombuffer(data, dtype=np.int16)))
            print("\r  level " + "#" * min(50, int(lvl * 600)) + " " * 50, end="\r")
            buf += data
            gram.AcceptWaveform(data)
            if full.AcceptWaveform(data):
                text = json.loads(full.Result()).get("text", "")
                g = json.loads(gram.FinalResult())
                gram.Reset()
                hit = any(w["word"] == "jarvis" and w["conf"] >= 0.85 for w in g.get("result", []))
                if text or hit or best >= threshold:
                    wtext = whisper.transcribe(bytes(buf[-16000 * 2 * 8:]))
                    verdict = "yes" if best >= threshold else matcher.check(text, hit, 2.0, best)
                    woke = verdict == "yes" or (verdict == "maybe" and matcher.whisper_says_wake(wtext))
                    print(f"  vosk: {text!r:30} whisper: {wtext!r:30} neural: {best:.2f} -> {'WAKE' if woke else 'ignored'}" + " " * 12)
                buf.clear()
                best = 0.0
    print(f"\n\nPeak level: {peak:.4f}")
    if peak == 0:
        print("The microphone delivered pure silence. Check Settings > Privacy & security > Microphone "
              "('Let desktop apps access your microphone') and that the mic isn't muted.")
    elif peak < 0.01:
        print("The microphone is very quiet. Raise its level in Sound settings, or set mic_gain = 3.0 in config.toml.")
    else:
        print("Microphone level looks fine.")


def train_wake(rounds: int = 6):
    """Record how the owner says 'Jarvis' and remember whatever Vosk makes of it."""
    import json
    import sounddevice as sd
    import vosk
    from .speech import Listener
    from .wake import GREETINGS, load_learned, save_learned, words_of
    cfg = load_config()
    v = cfg["voice"]
    vosk.SetLogLevel(-1)
    model = vosk.Model(str(resolve(v["vosk_model"])))
    dev = Listener.find_device(v.get("input_device", ""))
    learned = load_learned()
    print(f"I'll listen {rounds} times. Each time you see 'Say it', say just:  Jarvis")
    with sd.RawInputStream(samplerate=16000, blocksize=1600, device=dev, dtype="int16", channels=1) as stream:
        for i in range(rounds):
            rec = vosk.KaldiRecognizer(model, 16000)
            print(f"\n[{i + 1}/{rounds}] Say it ... ", end="", flush=True)
            end, text = time.time() + 5, ""
            while time.time() < end:
                data, _ = stream.read(1600)
                if rec.AcceptWaveform(bytes(data)):
                    text = json.loads(rec.Result()).get("text", "")
                    if text:
                        break
            text = text or json.loads(rec.FinalResult()).get("text", "")
            words = [w for w in words_of(text) if w not in GREETINGS]
            print(f"heard {text!r}")
            if len(words) == 1 and 3 <= len(words[0]) <= 10:
                learned.add(words[0])
    save_learned(learned)
    print(f"\nSaved. Jarvis will now also answer to: {', '.join(sorted(learned)) or '(nothing new)'}")


COMMANDS = {
    "run": "start Jarvis (the default)",
    "setup": "first-time setup: your name, models, Ollama, face (add --yes to accept defaults)",
    "setup-models": "download / repair the offline models (--stt parakeet for the larger speech model)",
    "check": "self-test the installation",
    "mic-test": "test the microphone / wake word",
    "train-wake": "teach Jarvis your pronunciation of the wake word",
    "enroll-face": "enrol your face for Face guard",
    "config": "open config.toml in an editor",
    "home": "show the folder with Jarvis's settings, models and logs",
    "intruders": "open the folder of photos Face guard took of strangers",
    "coursera": "restart Chrome with the Jarvis Coursera helper loaded",
    "connect-google": "link Gmail + Google Calendar so Jarvis tracks deadlines (run it again to change or remove)",
    "agenda": "print what Jarvis currently knows: deadlines, today's calendar, unread email",
    "orb-demo": "show the orb on its own",
    "uninstall-data": "delete Jarvis's settings, models and logs",
}


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("setup", "setup-models"):     # these take their own options
        from .config import ensure_home
        ensure_home()
        if argv[0] == "setup":
            from .installer import setup
            return setup(argv[1:])
        from .setup_models import main as fetch_models
        return fetch_models(argv[1:])
    ap = argparse.ArgumentParser(
        prog="jarvis", description="Jarvis: voice assistant + laptop security layer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="commands:\n" + "\n".join(f"  {k:15} {v}" for k, v in COMMANDS.items()))
    ap.add_argument("command", nargs="?", default="run", choices=list(COMMANDS), metavar="command",
                    help="see the list below (default: run)")
    ap.add_argument("--camera", type=int, default=0, help="camera index for enroll-face")
    ap.add_argument("--text", action="store_true", help="type commands instead of using the microphone")
    ap.add_argument("--mute", action="store_true", help="don't speak replies")
    ap.add_argument("--no-security", action="store_true", help="assistant only")
    ap.add_argument("--no-hud", action="store_true", help="console only, no orb / window")
    ap.add_argument("--panel", action="store_true", help="use the old arc-reactor window instead of the floating orb")
    ap.add_argument("--orb-demo", action="store_true", help="show the orb on its own, cycling its states (nothing listens)")
    ap.add_argument("--check", action="store_true", help="self-test the installation and exit")
    ap.add_argument("--mic-test", action="store_true", help="test the microphone / wake word and exit")
    ap.add_argument("--train-wake", action="store_true", help="teach Jarvis your pronunciation of the wake word")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    from . import installer
    from .config import ensure_home
    ensure_home()
    cmd = args.command
    if cmd == "config":
        return installer.open_config()
    if cmd == "home":
        return installer.show_home()
    if cmd == "coursera":
        return installer.coursera_mode()
    if cmd == "intruders":
        from . import router
        from .tools import Ctx
        Ctx.cfg = load_config()
        return print(router.intruder_folder())
    if cmd == "connect-google":
        from .agenda import connect_cli
        return connect_cli()
    if cmd == "agenda":
        from .agenda import Agenda
        a = Agenda(load_config())
        if not a.connected():
            print(a.deadlines_text())
            return 1
        a.sync()
        print(a.deadlines_text(), a.day_text(0), a.day_text(1), a.mail_text(), sep="\n")
        for name, err in a.errors.items():
            print(f"  ({name} failed: {err})")
        return 0
    if cmd == "uninstall-data":
        return installer.uninstall_data()
    if cmd == "enroll-face":
        from .enroll import main as enroll
        return enroll(args.camera)
    args.check = args.check or cmd == "check"
    args.mic_test = args.mic_test or cmd == "mic-test"
    args.train_wake = args.train_wake or cmd == "train-wake"
    args.orb_demo = args.orb_demo or cmd == "orb-demo"
    if not (ROOT / "models").is_dir() or not any((ROOT / "models").iterdir()):
        print("Jarvis isn't set up yet: run  jarvis setup  first.")
        return 1
    if args.check:
        return self_check()
    if args.mic_test:
        return mic_test()
    if args.train_wake:
        return train_wake()
    if args.orb_demo:
        from .orb import demo
        return demo()

    # Only one Jarvis at a time (two would fight over the microphone and camera).
    if sys.platform == "win32":
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\JarvisAssistantSingleton")
        if ctypes.windll.kernel32.GetLastError() == 183:
            print("Jarvis is already running.")
            return
    else:
        import fcntl
        (ROOT / "data").mkdir(parents=True, exist_ok=True)
        mutex = open(ROOT / "data" / "jarvis.lock", "w")
        try:
            fcntl.flock(mutex, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("Jarvis is already running.")
            return

    setup_logging()
    jarvis = Jarvis(args)
    use_hud = jarvis.cfg.get("ui", {}).get("hud", True) and not args.no_hud
    if not use_hud:
        try:
            jarvis.loop()
        except KeyboardInterrupt:
            print("\nJarvis stopped.")
        return
    # The face of Jarvis: a floating 3D orb on the desktop (default), or the old arc-reactor window.
    style = "panel" if args.panel else str(jarvis.cfg.get("ui", {}).get("style", "orb")).lower()
    if sys.platform != "win32":
        style = "panel"          # the floating orb is drawn with Win32 layered windows; elsewhere, the window
    hooks = dict(on_text=lambda t: jarvis.events.put(("text", t)), on_talk=lambda: jarvis.events.put(("ptt", None)),
                 on_quit=lambda: jarvis.events.put(("quit", None)), security=jarvis.security)
    if style == "orb":
        try:
            from .orb import Orb
            jarvis.hud = Orb(jarvis.cfg, on_stop=jarvis.speaker.stop, **hooks)
        except Exception as e:
            logging.getLogger("jarvis").exception("orb unavailable; falling back to the window")
            print(f"Orb unavailable ({e}); using the window instead.")
    if jarvis.hud is None:
        try:
            from .hud import Hud
            jarvis.hud = Hud(jarvis.cfg, **hooks)
        except Exception as e:
            print(f"HUD unavailable ({e}); running in the console.")
            return jarvis.loop()
    threading.Thread(target=jarvis.loop, daemon=True, name="core").start()
    try:
        jarvis.hud.run()
    except KeyboardInterrupt:
        pass
    jarvis.running = False
    del mutex


if __name__ == "__main__":
    main()
