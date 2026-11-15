"""Wake-word logic.

The original build only woke up when Vosk transcribed the exact word "jarvis". With a non-American accent the
small Vosk model hears "harris", "doris", "norris", "darius", "iris" ... so Jarvis never answered.

Detection is now layered (any one is enough):
  1. A grammar-restricted Vosk recognizer that can only output "jarvis" or [unk] says "jarvis" confidently.
  2. The free-text Vosk transcript starts with "jarvis" or a known / learned mishearing of it.
  3. The transcript starts with something that merely *sounds* like jarvis (or is a short 1-3 word phrase):
     Whisper re-listens to the audio and decides.
Learned mishearings come from `python -m jarvis --train-wake` (stored in data/wake_variants.json).
"""
import difflib
import json
import logging
import re

from .config import ROOT

log = logging.getLogger("jarvis.wake")

LEARNED_FILE = ROOT / "data" / "wake_variants.json"
GREETINGS = {"hey", "hi", "hello", "ok", "okay", "oh", "a", "yo", "hay", "high", "the", "uh", "um"}
# Words Whisper writes when it hears "Jarvis" spoken with various accents.
WHISPER_FORMS = {"jarvis", "jervis", "javis", "jarvys", "jarves", "jarvi", "jaavis", "jarviss", "jarvis's", "jervis's",
                 "charvis", "garvis", "jarbis", "jarwis",
                 # seen in the owner's live log (Indian English, "Jaa-vis"):
                 "yavis", "yarvis", "davis", "davies", "jovis", "chavis", "javies", "jarvish", "zarvis", "jawis"}


def load_learned() -> set[str]:
    try:
        return set(json.loads(LEARNED_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_learned(words: set[str]):
    LEARNED_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEARNED_FILE.write_text(json.dumps(sorted(words), indent=1), encoding="utf-8")


def similarity(a: str, b: str = "jarvis") -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def words_of(text: str) -> list[str]:
    return re.findall(r"[a-z']+", text.lower())


class WakeMatcher:
    def __init__(self, wake_words: list[str], fuzzy: float = 0.5, whisper_verify: bool = True, soft: float = 0.3):
        self.variants = {w.lower() for w in wake_words} | load_learned()
        self.fuzzy = fuzzy
        self.whisper_verify = whisper_verify
        self.soft = soft              # neural score that counts as "leaning towards the wake word"

    def reload(self):
        self.variants |= load_learned()

    def _lead(self, words: list[str]) -> tuple[int, str] | None:
        """Index and word of the first non-greeting word among the first three words."""
        for i, w in enumerate(words[:3]):
            if w not in GREETINGS:
                return i, w
        return None

    def check(self, vosk_text: str, grammar_hit: bool, duration: float, neural: float = 0.0) -> str:
        """Returns 'yes', 'maybe' (ask Whisper) or 'no'. Also tells the caller where the command starts.
        `neural` is the neural detector's best score for the phrase (0..1). A full hit never reaches this function
        (it is a 'yes' already); a partial score is treated as supporting evidence: bare "Jarvis" without "hey"
        often scores 0.2-0.5, well above the 0.13 of unrelated speech."""
        words = words_of(vosk_text)
        lead = self._lead(words)
        if lead and lead[1] in self.variants:
            return "yes"
        leaning = neural >= self.soft
        if grammar_hit:
            # Live testing: the "jarvis"-only recogniser catches an accented "Jarvis" that free text hears as
            # "no race", but it also fires inside long unrelated sentences. Trust it alone only for short phrases;
            # for anything longer Whisper (or the neural detector) has to agree.
            if (len(words) <= 3 and duration <= 3.0) or leaning or not self.whisper_verify:
                return "yes"
            return "maybe"
        if leaning and len(words) <= 3 and duration <= 3.0:
            return "yes"                  # a short phrase the detector half-recognised: "dollars", "yeah", "can"
        if not self.whisper_verify:
            return "no"
        if lead and 4 <= len(lead[1]) <= 9 and similarity(lead[1]) >= self.fuzzy:
            return "maybe"
        if 0 < len(words) <= 3 and duration <= 3.5:
            return "maybe"            # short phrase: cheap for Whisper to double-check
        return "no"

    def led_by_variant(self, text: str) -> bool:
        lead = self._lead(words_of(text))
        return bool(lead and lead[1] in self.variants)

    def only_wake(self, vosk_text: str) -> bool:
        """True when the phrase is nothing but the wake word (plus a greeting): 'jarvis', 'hey jarvis', ''."""
        rest = [w for w in words_of(vosk_text) if w not in GREETINGS]
        return len(rest) <= 1 and (not rest or rest[0] in self.variants)

    @staticmethod
    def whisper_says_wake(text: str) -> bool:
        words = words_of(text)[:4]
        return any(w in WHISPER_FORMS or (len(w) >= 5 and similarity(w) >= 0.72) for w in words)

    def strip(self, text: str) -> str:
        """Remove the leading wake word (and greeting) from a transcript: 'Hey Jarvis, open Chrome.' -> 'open Chrome.'"""
        tokens = list(re.finditer(r"[A-Za-z']+", text))
        for i, m in enumerate(tokens[:4]):
            w = m.group().lower()
            if w in GREETINGS:
                continue
            if w in self.variants or w in WHISPER_FORMS or similarity(w) >= 0.6:
                return text[m.end():].lstrip(" ,.!?;:-")
            break
        return text.strip()
