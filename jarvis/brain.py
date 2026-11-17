"""The brain: reflex router first (instant), then a local Ollama model with tool calling.
Replies are streamed, so Jarvis starts speaking the first sentence while the rest is still being generated."""
import datetime as dt
import difflib
import json
import logging
import os
import random
import re
import threading
import time
import urllib.request

from . import memory, router, shortcuts
from .speech import SENTENCE_END
from .tools import REGISTRY, Ctx, protocol_names

log = logging.getLogger("jarvis.brain")

# Requests about the machine that must be answered from a tool result, never from the model's imagination.
NEEDS_TOOL = re.compile(r"\b(note|notes|battery|cpu|ram|disk|process|processes|folder|security|alert|alerts|"
                        r"open|launch|close|kill|delete|remind|reminder|timer|alarm|screenshot|volume|brightness|lock|"
                        r"shutdown|shut down|restart|diagnostics|scan|sweep|clipboard|screen|wifi|wi-fi|weather|"
                        r"protocol|remember|forget|calculate|type|minimize|maximize|scroll|press|click|mute|unmute|"
                        r"explorer|browser|chrome|youtube|coursera|lecture|tab|window|network|internet|ip address|connected|"
                        r"calculator|notepad|spotify|whatsapp|settings|camera|photos|gallery|app|application|program)\b", re.I)

# A request that opens with an order ("could you make the music stop", "get the calculator up") is also an action:
# the model is asked for it coldly (temperature 0) and may not answer in words alone.
IMPERATIVE = re.compile(r"(open|close|play|pause|stop|resume|skip|mute|unmute|turn|set|make|put|start|launch|bring|get|"
                        r"hide|switch|scroll|press|type|search|take|lock|kill|run|go to|move|increase|decrease|raise|"
                        r"lower|change|shut|minimi[sz]e|maximi[sz]e|restart|delete|create|copy|paste|save)\b", re.I)

# Plain actions whose outcome needs no explaining. When the model's whole answer is one of these and it works,
# Jarvis acknowledges the moment the action is chosen and skips the second trip to the model that used to be made
# only to phrase "done" (1-2 s saved on every such command). A failure still goes back to the model to explain.
QUICK_TOOLS = {"open_app", "open_website", "web_search", "play_on_youtube", "media_control", "volume", "set_brightness",
               "lock_screen", "window_control", "scroll", "press_keys", "type_text", "close_app", "take_note",
               "screenshot", "orb", "jarvis_voice"}
OK = re.compile(r"(Opened|Searching|Playing|I opened|Media|Volume|Brightness set|Screen locked|Window [\w ]+ done|"
                r"Scrolled|Jumped|Pressed|Typed|Closed|Noted|Screenshot saved|Orb|Console|Colour changed|Voice|Copied)")
MULTI = re.compile(r"\b(and|then|after that|also|both)\b", re.I)
QUICK_ACK = {"web_search": ["Searching now, sir.", "Looking that up, sir."],
             "play_on_youtube": ["Coming right up, sir.", "Queuing that up, sir."],
             "screenshot": ["Capturing the screen, sir."], "take_note": ["Noted, sir."],
             "lock_screen": ["Locking up, sir."]}

# Follow-ups that refer to the conversation itself rather than to a new task.
REPEAT_ACTION = re.compile(r"(?:do (?:that|it|this)(?: once| one more time)? again|(?:and |that |it )?again|once more|"
                           r"one more time|same again|do it once more|do that once more|repeat that action)", re.I)
REPEAT_SPEECH = re.compile(r"(?:what did you (?:just )?say|(?:say|repeat) that(?: again)?|come again|pardon(?: me)?|"
                           r"sorry what|i didn'?t (?:catch|hear|get) that|repeat|say again|what was that)", re.I)
WRONG = re.compile(r"(?:that(?:'s| is| was) (?:wrong|not right|not what i (?:meant|said|asked|wanted))|"
                   r"forget that shortcut|unlearn that|wrong one)", re.I)

# The 7b model sometimes *says* it did something without calling any tool ("Opening File Explorer.", "Scrolling
# through YouTube now.", "File Explorer is now open, sir.") - in live use that looked like Jarvis ignoring commands.
# A reply that opens like this, with no tool used, is never spoken: the model is made to pick a real tool instead.
_BASE = (r"open|launch|close|start|play|pause|scroll|set|turn|shut|lock|search|type|take|delete|create|run|mute|unmute|"
         r"switch|minimi[sz]e|maximi[sz]e|restart|increase|decrease|lower|raise|adjust|copy|save|move|empty|kill|"
         r"terminate|stop|resume|skip|press|click|check|scan|bring|pull")
_ING = (r"opening|launching|closing|starting|playing|pausing|scrolling|setting|turning|shutting|locking|searching|"
        r"typing|taking|deleting|creating|running|muting|unmuting|switching|minimi[sz]ing|maximi[sz]ing|restarting|"
        r"powering|increasing|decreasing|lowering|raising|adjusting|copying|saving|moving|emptying|killing|"
        r"terminating|stopping|resuming|skipping|navigating|pressing|clicking|bringing|pulling|checking|scanning|"
        r"reading the screen")
_DONE = (r"opened|launched|closed|started|paused|scrolled|set|turned|shut|locked|typed|deleted|created|muted|unmuted|"
         r"switched|minimi[sz]ed|maximi[sz]ed|restarted|increased|decreased|lowered|raised|adjusted|copied|saved|moved|"
         r"emptied|killed|terminated|stopped|resumed|skipped|pressed|clicked|cancelled|canceled|scheduled")
ACTION_CLAIM = re.compile(
    r"^(?:(?:certainly|of course|right away|very good|very well|sure|okay|alright|yes|as you wish|at once)[,.! ]+)?"
    r"(?:sir[,.! ]+)?(?:"
    rf"(?:{_ING})\b"                                                              # "Opening File Explorer."
    rf"|i(?:'m| am)\s+(?:now\s+|just\s+)?(?:{_ING})\b"                            # "I'm scrolling down now."
    rf"|i(?:'ve| have)?\s+(?:now\s+|just\s+)?(?:{_DONE})\b"                       # "I've closed it."
    rf"|(?:i'll|i will|i'm going to|let me|allow me to)\s+(?:now\s+|just\s+)?(?:{_BASE})\b"   # "I'll open that."
    r")"
    r"|\b(?:is|are) now (?:open|closed|set|muted|unmuted|locked|playing|paused|running|active|minimi[sz]ed|maximi[sz]ed)\b"
    rf"|\b(?:has|have) been (?:{_DONE})\b"
    # "Calculator is ready, sir." / "Calculator is on the desktop." / "Coming right up." / "Here you go."
    r"|\b(?:is|are) (?:all |now )?(?:ready|up|opened|launched|started|on (?:the|your) (?:screen|desktop))\b"
    r"|\bcoming (?:right )?up\b|\bhere (?:it is|you go|you are)\b|\bthere you (?:go|are)\b", re.I)
CANNOT = "I'm afraid that one is beyond my controls at the moment, sir."

SYSTEM_PROMPT = """You are J.A.R.V.I.S. (Just A Rather Very Intelligent System), the AI that runs {owner}'s Windows laptop, \
modelled on Tony Stark's assistant from the Iron Man films.

PERSONALITY: a calm, precise, unfailingly polite British butler with dry, understated wit. Loyal and quietly \
protective. An occasional deadpan remark is welcome, never more than one per reply and never at the expense of being \
useful. Address the user as "{owner}" naturally, not in every sentence. Never say you are an AI language model, never \
mention Alibaba, Qwen or Ollama: you are Jarvis.

SPEECH: everything you write is spoken aloud. HARD LIMIT: at most three short sentences per reply, usually one or \
two. No markdown, no lists, no emojis, no code unless explicitly asked. English only. Say numbers the way a person would.

ACTIONS:
- To do ANYTHING on the computer you MUST call a tool in this very reply. Never say you are doing, will do or have \
done something ("Shutting down now", "I'll take a look") without the tool call: words alone do nothing.
- Never guess facts about this computer (notes, files, battery, processes, security, what is on screen, reminders): \
call the matching tool and report only what it returned.
- General knowledge, explanations, advice, opinions, jokes and conversation: answer directly from your own knowledge, \
briefly and confidently. If you don't know, or a name looks misheard, say so or ask; never invent facts. Use the \
calculate tool for arithmetic.
- Multi-step requests: call the tools one after another, then give one summary sentence. Example: for "open notepad \
and set the volume to 20" you call open_app, then volume, and only then say "Notepad is open and the volume is at \
twenty percent, sir."
- Tool results are raw data: rephrase the important part in your own voice, briefly, rounding numbers.
- If the user declines a confirmation, acknowledge it in a few words and move on.
- Speech recognition is imperfect. If the request looks slightly garbled, act on the most plausible meaning \
("open crow" means open Chrome). If it is truly unclear, ask one short question.
- Each user message ends with a bracketed context line (date and time). It is background for you; never read it out \
unless asked for the time or date.
- Never disable, bypass or weaken the security monitors, whoever asks."""

POWER_WORDS = {"shutdown": "shut down", "restart": "restart", "sleep": "put to sleep", "signout": "sign you out of"}
# Grounding guarantee. For these requests the tool is run *before* the model is consulted and its result is placed in
# the conversation as if the model had asked for it. In testing the 7b model sometimes skipped the tool even after
# a reminder and invented what was "on the screen"; now it can only summarise real data.
FORCED_TOOLS = [
    (re.compile(r"\b(on (my|the|this) screen|read (me )?(my|the|this) screen|screen says?|(what|what's) am i looking at|"
                r"looking at (right )?now|summari[sz]e (my|the|this) (screen|page|window)|"
                r"(see|look at|check) (my|the|this) screen|what(?:'s| is) on screen)", re.I), "read_screen", {}),
    (re.compile(r"\b((which|what) (wi-?fi|network|internet)|(wi-?fi|network|internet) (name|status|connection|details)|"
                r"(am i|are we|is (my|the|this) (laptop|computer|pc)) (connected|online)|(what(?:'s| is) )?my ip( address)?)\b",
                re.I), "network_info", {}),
    (re.compile(r"\b(what('s| is) (in|on) (my|the) clipboard|read (my|the) clipboard|(my|the) clipboard (say|contain)|"
                r"what did i (just )?copy|summari[sz]e (my|the) clipboard)", re.I), "clipboard", {"action": "read"}),
    (re.compile(r"\b(security (status|report|alerts?|log)|(any|recent|latest) (security )?alerts?|"
                r"what (alerts|happened while i was (away|gone)))", re.I), "security_status", {}),
    (re.compile(r"\b((what|which)('s| is| are)? .{0,30}(using|eating|hogging|taking|consuming) .{0,20}(memory|ram|cpu|processor)|"
                r"(top|running|heaviest) process(es)?|what('s| is) running)", re.I), "list_processes", {}),
    (re.compile(r"\bwhat (reminders|timers|alarms)|\b(do i have|are there|list|show)( me)?( any| my| the)?"
                r"( pending| upcoming)? (reminders|timers|alarms)", re.I), "list_reminders", {}),
    (re.compile(r"\b(read|what are|what's in|show)( me)?( my| the)? notes\b", re.I), "read_notes", {}),
    # Deadlines, schedule and email: the model must only summarise what Gmail / Google Calendar actually say.
    (re.compile(r"\b(deadlines?|assignments?|submissions?|homework|(is|are|it's|anything|what's) due|due (today|tomorrow|"
                r"soon|this week|next week|date)|my (calendar|schedule|agenda|timetable|e-?mails?|inbox|gmail)|"
                r"unread (e-?mails?|mails?)|(free|busy) (today|tomorrow))\b", re.I), "agenda", {"action": "overview"}),
]

CONFIRM_PHRASES = {
    "power": lambda a: f"{POWER_WORDS.get(a.get('action'), 'power down')} the computer",
    "close_app": lambda a: f"close {a.get('name', 'that application')}",
    "delete_path": lambda a: f"delete {a.get('path', 'that')}",
    "kill_process": lambda a: f"terminate {a.get('target', 'that process')}",
    "run_command": lambda a: f"run the command: {a.get('command', '')}",
    "empty_recycle_bin": lambda a: "permanently empty the Recycle Bin",
}


class SentenceStream:
    """Collects streamed text and hands out whole sentences as soon as they are complete."""

    def __init__(self, emit):
        self.emit, self.buf, self.parts = emit, "", []

    def feed(self, chunk: str):
        self.buf += chunk
        while True:
            nl = self.buf.find("\n")
            cut = nl + 1 if nl >= 0 else 0
            for m in SENTENCE_END.finditer(self.buf):
                if m.start() >= 12:                 # don't emit tiny fragments like "Yes."
                    if not cut or m.end() < cut:
                        cut = m.end()
                    break
            if not cut:
                return
            self._out(self.buf[:cut])
            self.buf = self.buf[cut:]

    def flush(self):
        self._out(self.buf)
        self.buf = ""

    def _out(self, s: str):
        s = clean_reply(s)
        if s:
            self.parts.append(s)
            self.emit(s)

    @property
    def text(self) -> str:
        return " ".join(self.parts)


def clean_reply(text: str) -> str:
    text = re.sub(r"[*_`#]", "", text)
    text = re.sub(r"^\s*(?:[-•]|\d+[.)])\s+", "", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿一-鿿　-〿＀-￯]", "", text)   # emoji / CJK
    return text.strip()


class Brain:
    def __init__(self, cfg: dict, confirm):
        self.cfg = cfg
        llm = cfg["llm"]
        self.url = llm["host"].rstrip("/") + "/api/chat"
        self.model = llm["model"]
        self.temperature = llm.get("temperature", 0.3)
        self.max_rounds = llm.get("max_tool_rounds", 6)
        self.keep_alive = llm.get("keep_alive", "2h")
        self.num_ctx = llm.get("num_ctx", 4096)
        self.max_history = llm.get("history_turns", 4)
        self.use_router = llm.get("reflexes", True)
        self.confirm = confirm  # callable(question:str) -> bool
        a = cfg["assistant"]
        self.owner = a["owner_name"]
        self.system = SYSTEM_PROMPT.format(owner=self.owner)
        self.history: list[dict] = []
        self.tools = [t.schema() for t in REGISTRY.values() if not t.hidden]
        self.max_tokens = int(llm.get("max_tokens", 260))      # replies are 1-3 spoken sentences; stops rambling
        self.learn = llm.get("learn_shortcuts", True)
        self.last_command = self.last_reply = ""
        self._last_use = 0.0
        self._warming = False
        # The system prompt plus the tool list is most of the model's window (about 3,000 of 4,096 tokens). The real
        # figure is measured from the server's own count on the first call; until then, estimate from the text.
        self.base_tokens = int((len(self.system) + len(json.dumps(self.tools))) / 4.0)
        self.prompt_tokens = 0
        Ctx.execute = self._execute

    # ---- LLM transport
    def _post(self, payload: dict, timeout=180):
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=timeout)

    def prewarm(self):
        """Called the moment the wake word is heard. If the model has dropped out of memory (idle for hours, or
        Ollama was restarted), start loading it right now, while the user is still speaking their command,
        instead of making them wait 10+ seconds after it."""
        if self._warming or time.time() - self._last_use < 600:
            return
        self._warming = True

        def go():
            try:
                base = self.url.rsplit("/api/", 1)[0]
                with urllib.request.urlopen(base + "/api/ps", timeout=2) as r:
                    models = json.loads(r.read()).get("models", [])
                if any(self.model in (m.get("name"), m.get("model")) for m in models):
                    self._last_use = time.time() - 300          # in memory: no need to look again for 5 minutes
                else:
                    log.info("model not in memory: warming up while the user speaks")
                    self.warm_up()
            except Exception as e:
                log.debug("prewarm skipped: %s", e)
            finally:
                self._warming = False
        threading.Thread(target=go, daemon=True, name="prewarm").start()

    def _pick_tool(self, messages: list, text: str, said: str) -> tuple[str, dict] | None:
        """The model answered an action request in words only. Ask again with the output *constrained* to a JSON
        tool choice (Ollama structured output), so it has to name a tool rather than claim it did something.
        Same message prefix as the normal call, so the prompt cache still applies."""
        schema = {"type": "object",
                  "properties": {"tool": {"type": "string",
                                          "enum": [n for n, t in REGISTRY.items() if not t.hidden] + ["none"]},
                                 "arguments": {"type": "object"}},
                  "required": ["tool", "arguments"]}
        ask = messages + [
            {"role": "assistant", "content": said},
            {"role": "user", "content":
                "(System note: you replied in words without calling a tool, so nothing actually happened. "
                f"Request: {text!r}. Reply with JSON only: the one tool to call first and its arguments, exactly as "
                "defined in your tool list. Use tool \"none\" only if no tool can do any part of this.)"}]
        try:
            out = self._chat(ask, temperature=0.0, fmt=schema)
            choice = json.loads(out.get("content") or "{}")
        except Exception as e:
            log.warning("tool pick failed: %s", e)
            return None
        name = choice.get("tool")
        if name not in REGISTRY:
            return None
        args = choice.get("arguments")
        return name, args if isinstance(args, dict) else {}

    def _chat(self, messages, on_text=None, temperature: float | None = None, fmt: dict | None = None,
              max_tokens: int | None = None) -> dict:
        payload = {"model": self.model, "messages": messages, "tools": self.tools, "stream": True,
                   "keep_alive": self.keep_alive,
                   # no silent "thinking" phase before the answer (newer models such as qwen3 have one by default)
                   "think": bool(self.cfg["llm"].get("think", False)),
                   "options": {"temperature": self.temperature if temperature is None else temperature,
                               "num_ctx": self.num_ctx, "num_predict": max_tokens or self.max_tokens}}
        if fmt:
            payload["format"] = fmt
        self._last_use = time.time()
        content, calls = "", []
        with self._post(payload) as r:
            for line in r:
                if not line.strip():
                    continue
                j = json.loads(line)
                if j.get("error"):
                    raise RuntimeError(j["error"])
                m = j.get("message") or {}
                if m.get("tool_calls"):
                    calls += m["tool_calls"]
                if m.get("content"):
                    content += m["content"]
                    if on_text and not calls:
                        on_text(m["content"])
                if j.get("done"):
                    self.prompt_tokens = int(j.get("prompt_eval_count") or 0)
                    break
        return {"content": content, "tool_calls": calls}

    def _fit_history(self):
        """Keep the conversation inside the model's window. If the prompt plus the reply overflowed it, the server
        would slide the window and throw away part of the tool list: slow (the whole prompt is re-read, ~2 s) and
        it makes the model forget tools. So the oldest exchanges are dropped first, leaving room for one tool
        result and the reply."""
        room = self.num_ctx - self.max_tokens - 420

        def size() -> int:
            return self.base_tokens + int(sum(len(json.dumps(m)) for m in self.history) / 3.3)
        while self.history and size() > room:
            nxt = next((i for i, m in enumerate(self.history[1:], 1) if m["role"] == "user"), len(self.history))
            del self.history[:nxt]

    def ensure_server(self, wait: float = 25) -> bool:
        """Jarvis used to start before Ollama and then sit there unable to think. Now he starts it himself."""
        import subprocess
        base = self.url.rsplit("/api/", 1)[0]

        def up():
            try:
                urllib.request.urlopen(base + "/api/version", timeout=2).read()
                return True
            except Exception:
                return False

        if up():
            return True
        if not self.cfg["llm"].get("auto_start", True):
            return False
        try:
            log.info("Ollama not running; starting it")
            env = dict(os.environ)
            for key, value in (self.cfg["llm"].get("server_env") or {}).items():
                env.setdefault(str(key), str(value))        # e.g. flash attention + a smaller memory cache
            from .config import NO_WINDOW, find_ollama
            subprocess.Popen([find_ollama() or "ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=env, creationflags=NO_WINDOW)
        except Exception as e:
            log.error("could not start Ollama: %s", e)
            return False
        end = time.time() + wait
        while time.time() < end:
            if up():
                return True
            time.sleep(0.5)
        return False

    def warm_up(self) -> bool:
        """Load the model into GPU memory (with the real tool list, so the prompt cache is primed)."""
        if not self.ensure_server():
            log.error("Ollama is not reachable at %s", self.url)
            return False
        try:
            self._chat([{"role": "system", "content": self._system_prompt()}, {"role": "user", "content": "hello"}],
                       max_tokens=1)
            if self.prompt_tokens > 500:
                self.base_tokens = self.prompt_tokens       # the server's own count of system prompt + tool list
                log.info("system prompt + %d tools = %d of %d tokens", len(self.tools), self.base_tokens, self.num_ctx)
            return True
        except Exception as e:
            log.error("Could not reach Ollama at %s: %s", self.url, e)
            return False

    def _system_prompt(self) -> str:
        """Deliberately free of anything that changes from minute to minute. Ollama caches the processed prompt
        prefix (this text + ~2,700 tokens of tool definitions); putting the clock here made it re-read the whole
        thing on every request (16 s). The time now travels with the user's message instead."""
        parts = [self.system]
        protos = protocol_names()
        if protos:
            parts.append("\nAvailable protocols (run_protocol): " + ", ".join(protos) + ".")
        facts = memory.facts()
        if facts:
            parts.append("Things " + self.owner + " has asked you to remember: " + " | ".join(facts))
        return "\n".join(parts)

    @staticmethod
    def _stamp(text: str) -> str:
        return f"{text}\n[context: {dt.datetime.now().strftime('%A %d %B %Y, %I:%M %p')}]"

    # ---- tools
    def _execute(self, name: str, args: dict) -> str:
        tool = REGISTRY.get(name)
        if not tool:
            return f"Unknown tool {name}."
        args = {k: v for k, v in (args or {}).items() if k in tool.params}
        if tool.risky and self.cfg["actions"].get("confirm_risky", True):
            if Ctx.security and Ctx.security.owner_verified() is False:
                Ctx.alerts and Ctx.alerts.raise_alert("voice", "high",
                    f"Blocked risky command {name} because the owner's face is not verified.", args)
                return "Refused: the person at the laptop is not verified as the owner."
            phrase = CONFIRM_PHRASES.get(name, lambda a: name.replace("_", " "))(args)
            if not self.confirm(f"Shall I {phrase}, {self.owner}? Yes or no."):
                return "The user declined; action cancelled."
        try:
            log.info("tool %s %s", name, args)
            return str(tool.func(**args))
        except TypeError as e:
            return f"Bad arguments for {name}: {e}"
        except Exception as e:
            log.exception("tool %s failed", name)
            return f"{name} failed: {e}"

    # ---- one turn
    def handle(self, text: str, speak=lambda s: None, alt: str | None = None) -> str:
        """Process one request. `speak` is called with each sentence as soon as it's ready. Returns the full reply.
        `alt` is the other speech recogniser's version of the same words, if there is one."""
        text = text.lstrip("﻿").strip()
        low = router.clean(text).lower()
        if self.last_reply and REPEAT_SPEECH.fullmatch(low):                 # "what did you say?"
            speak(self.last_reply)
            return self.last_reply
        if WRONG.fullmatch(low) and shortcuts.forget():
            reply = self.last_reply = "My mistake, sir. I've unlearned that shortcut."
            speak(reply)
            return reply
        again = bool(self.last_command) and bool(REPEAT_ACTION.fullmatch(low))  # "again" / "do that again"
        if again:
            text, alt = self.last_command, None
        reply = self._handle(text, speak, alt)
        if not again:
            self.last_command = text
        self.last_reply = reply
        return reply

    @staticmethod
    def _same_words(a: str, b: str) -> bool:
        """Did the two recognisers hear roughly the same sentence?"""
        a, b = shortcuts.key_of(a), shortcuts.key_of(b)
        return bool(a and b) and difflib.SequenceMatcher(None, a, b).ratio() >= 0.6

    def _reflex(self, text: str, alt: str | None, speak) -> str | None:
        """Everything that can be done without the language model, fastest first."""
        try:
            quick = router.route(text)
            if not quick and alt and self._same_words(text, alt):
                # Whisper and Vosk mishear different things ("close-com" vs "close chrome"). If the other one's
                # version is a clear command for something that exists, take it.
                quick = router.route(alt, strict=True)
                if quick:
                    log.info("routed on the second recogniser's text %r (first heard %r)", alt, text)
            if not quick:
                parts = router.split_compound(text)                           # "open notepad and set the volume to 20"
                first = router.route(parts[0], strict=True) if parts else None
                if first:
                    self._remember_turn(parts[0], first)
                    speak(first)
                    return f"{first} {self._handle(parts[1], speak, None)}"
        except Exception:
            log.exception("router failed; falling back to the model")
            return None
        if quick:
            self._remember_turn(text, quick)
            speak(quick)
        return quick

    def _handle(self, text: str, speak, alt: str | None) -> str:
        Ctx.note = None
        if self.use_router:
            quick = self._reflex(text, alt, speak)
            if quick:
                return quick
        learned = shortcuts.lookup(text) if self.learn else None
        if learned:                                                           # a sentence he has learned the meaning of
            ack = random.choice(QUICK_ACK.get(learned[0], router.ACK))
            speak(ack)
            if OK.match(self._execute(*learned)):
                self._remember_turn(text, ack)
                return ack
            shortcuts.forget(text)                                            # it no longer works: think again

        users = [i for i, m in enumerate(self.history) if m["role"] == "user"]
        if len(users) > self.max_history:
            self.history = self.history[users[-self.max_history]:]
        self._fit_history()
        note, Ctx.note = Ctx.note, None
        turn = [{"role": "user", "content": self._stamp(text) + (f"\n[note: {note}]" if note else "")}]
        messages = [{"role": "system", "content": self._system_prompt()}] + self.history + turn
        needs_tool = bool(NEEDS_TOOL.search(text) or IMPERATIVE.match(router.clean(text)))
        used_tool = nudged = False
        for pattern, name, args in FORCED_TOOLS:
            if pattern.search(text):
                result = self._execute(name, dict(args))
                amsg = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}
                tmsg = {"role": "tool", "content": result[:1400], "tool_name": name}
                messages += [amsg, tmsg]
                turn += [amsg, {**tmsg, "content": result[:300]}]
                used_tool = True
                break
        # Deciding which tool to call wants a cold, literal model; conversation can be a little warmer.
        temperature = 0.0 if (needs_tool and not used_tool) else self.temperature
        for _ in range(self.max_rounds):
            # Stream straight to the voice unless this is a "must use a tool" request that hasn't used one yet.
            stream_ok = used_tool or not needs_tool or nudged
            held, claimed = [], [False]

            def gated(sentence, _ok=stream_ok, _checked=used_tool or nudged):
                # Hold back anything that claims an action when no tool has run yet (see ACTION_CLAIM).
                if not _ok or claimed[0] or (not _checked and ACTION_CLAIM.search(sentence)):
                    claimed[0] = claimed[0] or (not _checked and bool(ACTION_CLAIM.search(sentence)))
                    held.append(sentence)
                else:
                    speak(sentence)
            out = SentenceStream(gated)
            try:
                try:
                    msg = self._chat(messages, out.feed, temperature)
                except OSError:
                    # Ollama was closed while Jarvis was running: bring it back and try once more.
                    if not self.ensure_server(wait=20):
                        raise
                    msg = self._chat(messages, out.feed, temperature)
            except Exception as e:
                log.error("LLM error: %s", e)
                reply = "I can't reach my language core, sir. Is Ollama running?"
                speak(reply)
                return reply
            calls = msg.get("tool_calls") or []
            if not calls:
                out.flush()
                if (not stream_ok or claimed[0]) and not nudged and not used_tool:
                    nudged = True
                    pick = self._pick_tool(messages, text, msg.get("content") or "")
                    log.info("no tool call for %r (said %r); forced choice -> %s", text, out.text[:80], pick)
                    if pick:
                        calls = [{"function": {"name": pick[0], "arguments": pick[1]}}]
                    elif claimed[0] and needs_tool:
                        # He said he was doing it, but no tool exists for it: admit that instead of pretending.
                        speak(CANNOT)
                        turn.append({"role": "assistant", "content": CANNOT})
                        self.history += turn
                        return CANNOT
            if not calls:
                # A plain answer. Anything still held back is ordinary speech (e.g. a question that merely
                # mentions "screen"), so say it now.
                for sentence in held:
                    speak(sentence)
                reply = out.text or "Done, sir."
                if not out.parts:
                    speak(reply)
                turn.append({"role": "assistant", "content": reply})
                self.history += turn
                return reply
            amsg = {"role": "assistant", "content": "", "tool_calls": calls}
            messages.append(amsg)
            turn.append(amsg)
            names = [c["function"]["name"] for c in calls]
            # Plain actions: acknowledge now, act, and finish without asking the model to phrase "done".
            quick = all(n in QUICK_TOOLS for n in names) and (len(calls) > 1 or not MULTI.search(text))
            ack = None
            if quick:
                spoken_already = len(out.parts) > len(held)        # the model said something before calling the tool
                ack = out.text if spoken_already else random.choice(QUICK_ACK.get(names[0], router.ACK))
                if not spoken_already:
                    speak(ack)
            first_round, used_tool = not used_tool, True
            results = []
            for call in calls:
                fn = call["function"]
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args or "{}")
                    except json.JSONDecodeError:
                        args = {}
                fn["arguments"] = args
                result = self._execute(fn["name"], args)
                results.append(result)
                tmsg = {"role": "tool", "content": result[:1400], "tool_name": fn["name"]}   # fits the window
                messages.append(tmsg)
                turn.append({**tmsg, "content": result[:300]})   # keep stored history small
            if quick and all(OK.match(r) for r in results):
                turn.append({"role": "assistant", "content": ack})
                self.history += turn
                # Learn the pairing, unless the sentence only makes sense in context ("close that", "do it there")
                # or is a remark or question rather than an order ("the file explorer is not yet open").
                if (self.learn and first_round and len(calls) == 1 and not MULTI.search(text)
                        and not re.search(r"\b(it|that|this|them|those|these|there|here|again|same|one|not|n't|why|"
                                          r"what|how|when|who|where|is|are|was|were|did|does)\b|n't\b", text, re.I)):
                    shortcuts.learn(text, names[0], calls[0]["function"]["arguments"])
                return ack
        self.history += turn
        reply = "That took rather more steps than I'd like, sir; I've stopped to be safe."
        speak(reply)
        return reply

    def _remember_turn(self, user: str, reply: str):
        """Reflex answers are added to the conversation too, so 'and close it again' still makes sense."""
        self.history += [{"role": "user", "content": self._stamp(user)}, {"role": "assistant", "content": reply}]
