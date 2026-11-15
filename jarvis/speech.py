"""Voice in and out, fully offline.

Speaker  - neural British voice (Piper, en_GB "alan") with Windows SAPI as a fallback. Text is synthesised sentence by
           sentence on one thread and played on another, so Jarvis starts talking ~1 s after a reply begins and can be
           interrupted instantly.
Listener - always-on microphone -> Vosk (free text + a grammar recogniser that only knows "jarvis") -> utterances with
           their raw audio, which Whisper re-transcribes accurately when it matters.
"""
import json
import logging
import queue
import re
import threading
import time

import numpy as np

from .config import resolve
from .state import STATE

log = logging.getLogger("jarvis.speech")

SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+(?=[A-Z0-9\"'(])")


def split_sentences(text: str) -> list[str]:
    parts = []
    for line in text.splitlines():
        parts += [p.strip() for p in SENTENCE_END.split(line) if p.strip()]
    return parts


def normalise_for_speech(text: str) -> str:
    text = re.sub(r"[*_`#>|]", "", text)
    text = text.replace("%", " percent").replace("&", " and ").replace("°C", " degrees")
    text = re.sub(r"\bJ\.A\.R\.V\.I\.S\.?", "Jarvis", text)
    text = re.sub(r"https?://\S+", "the link", text)
    return text


# ===================================================================== output
class Speaker:
    def __init__(self, cfg_voice: dict, enabled: bool = True):
        self.cfg = cfg_voice
        self.enabled = enabled
        self.text_q: queue.Queue = queue.Queue()
        self.audio_q: queue.Queue = queue.Queue()
        self.speaking = threading.Event()      # True from say() until the audio has finished (plus echo tail)
        self.ready = threading.Event()
        self.engine_name = "none"
        self._gen = 0                          # bumps on stop(): stale text/audio is discarded
        self._pending = 0
        self.out_rate = 22050                  # the voice's sample rate once it has loaded (chimes are made to match)
        self._cache: dict[str, np.ndarray] = {}        # short phrases already synthesised ("Right away, sir.")
        self._warm: list[str] = []             # phrases to synthesise ahead of time while idle
        self._chimes: dict[tuple, np.ndarray] = {}
        self._cv = threading.Condition()
        threading.Thread(target=self._synth_loop, daemon=True, name="tts-synth").start()
        threading.Thread(target=self._play_loop, daemon=True, name="tts-play").start()

    # ---- public
    def say(self, text: str, log_it: bool = True):
        text = (text or "").strip()
        if not text:
            return
        print(f"\nJARVIS: {text}", flush=True)
        if log_it:
            STATE.add("JARVIS", text)
        if not self.enabled:
            STATE.caption("JARVIS", text)      # muted: the caption under the orb is the reply
            return
        with self._cv:
            self._pending += 1
            self.speaking.set()
            STATE.speaking = True
        self.text_q.put((self._gen, text))

    def precache(self, phrases):
        """Synthesise stock phrases ahead of time (while idle) so acknowledgements start instantly."""
        self._warm += [p for p in phrases if p and p not in self._cache and p not in self._warm]

    def chime(self, kind: str = "wake"):
        """A short soft two-note tone: an instant "I'm listening" that takes a fraction of the time of a spoken
        "Sir?". The microphone is muted only for the ~0.4 s it lasts, so Jarvis doesn't hear himself."""
        if not self.enabled:
            return
        sr = self.out_rate
        tone = self._chimes.get((kind, sr))
        if tone is None:
            notes = {"wake": (784.0, 1174.7), "done": (1174.7, 784.0), "error": (392.0, 311.1)}.get(kind, (784.0, 1174.7))
            n = int(sr * 0.085)
            env = np.sin(np.linspace(0, np.pi, n)) ** 2
            volume = float(self.cfg.get("chime_volume", 0.16))
            tone = np.concatenate([np.sin(2 * np.pi * f * np.arange(n) / sr) * env for f in notes])
            tone = self._chimes[(kind, sr)] = (tone * volume * 32767).astype(np.int16)
        with self._cv:
            self._pending += 1
            self.speaking.set()
        gen = self._gen
        self.audio_q.put((gen, tone, sr, ""))
        self.audio_q.put((gen, None, 0, 0.12))             # a chime leaves no echo worth waiting for

    def wait(self, timeout: float | None = 60):
        """Block until everything queued has been spoken."""
        end = None if timeout is None else time.time() + timeout
        with self._cv:
            while self._pending > 0:
                left = None if end is None else end - time.time()
                if left is not None and left <= 0:
                    break
                self._cv.wait(left)

    def stop(self):
        """Interrupt: drop everything queued and cut the current sentence."""
        with self._cv:
            self._gen += 1
            for q in (self.text_q, self.audio_q):
                while not q.empty():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
            self._pending = 0
            self._cv.notify_all()
        threading.Timer(0.3, self._maybe_clear).start()

    @property
    def busy(self) -> bool:
        return self._pending > 0

    # ---- internals
    def _done_one(self, tail: float | None = None):
        with self._cv:
            self._pending = max(0, self._pending - 1)
            if self._pending == 0:
                self._cv.notify_all()
        if self._pending == 0:
            threading.Timer(self.cfg.get("echo_tail", 0.35) if tail is None else tail, self._maybe_clear).start()

    def _maybe_clear(self):
        if self._pending == 0:
            self.speaking.clear()
            STATE.speaking = False
            STATE.voice_level = 0.0

    def _load_engine(self):
        if self.cfg.get("tts_engine", "piper") == "piper":
            try:
                from piper import PiperVoice, SynthesisConfig
                voice = PiperVoice.load(str(resolve(self.cfg["piper_voice"])))
                syn = SynthesisConfig(length_scale=self.cfg.get("piper_length_scale", 0.95),
                                      noise_scale=0.6, noise_w_scale=0.7)
                list(voice.synthesize("Ready.", syn))      # warm-up so the first real reply is quick
                self.engine_name = "piper"
                self.out_rate = int(voice.config.sample_rate)
                log.info("Piper voice loaded: %s", self.cfg["piper_voice"])
                return ("piper", voice, syn)
            except Exception as e:
                log.error("Piper unavailable (%s); falling back to Windows SAPI voice", e)
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        voice = comtypes.client.CreateObject("SAPI.SpVoice")
        voice.Rate = self.cfg.get("tts_rate", 1)
        try:
            for v in voice.GetVoices():
                if any(n in v.GetDescription() for n in ("George", "Ryan", "David", "Guy")):
                    voice.Voice = v
                    break
        except Exception:
            pass
        self.engine_name = "sapi"
        return ("sapi", voice, None)

    def _synth_loop(self):
        kind, voice, syn = self._load_engine()
        self.ready.set()

        def synth(sentence: str) -> np.ndarray:
            audio = self._cache.get(sentence)
            if audio is None:
                audio = np.frombuffer(b"".join(c.audio_int16_bytes for c in voice.synthesize(sentence, syn)), dtype=np.int16)
                if len(sentence) <= 48:                 # stock phrases repeat; long sentences never do
                    if len(self._cache) >= 120:
                        self._cache.pop(next(iter(self._cache)))
                    self._cache[sentence] = audio
            return audio

        while True:
            try:
                gen, text = self.text_q.get(timeout=0.5 if (self._warm and kind == "piper") else None)
            except queue.Empty:
                # Nothing to say right now: use the quiet moment to prepare a stock phrase.
                try:
                    for sentence in split_sentences(normalise_for_speech(self._warm.pop(0))):
                        synth(sentence)
                except Exception as e:
                    log.debug("precache failed: %s", e)
                continue
            if gen != self._gen:
                continue
            text = normalise_for_speech(text)
            try:
                if kind == "piper":
                    for sentence in split_sentences(text):
                        if gen != self._gen:
                            break
                        self.audio_q.put((gen, synth(sentence), voice.config.sample_rate, sentence))
                else:
                    self._speak_sapi(voice, text, gen)
            except Exception as e:
                log.error("TTS failed: %s", e)
            self.audio_q.put((gen, None, 0, None))     # end-of-text marker

    def _speak_sapi(self, voice, text, gen):
        STATE.caption("JARVIS", text)
        voice.Speak(text, 1)                            # async
        t = 0.0
        while not voice.WaitUntilDone(50):
            t += 0.05
            STATE.voice_level = 0.35 + 0.25 * abs(np.sin(t * 9))
            if gen != self._gen:
                voice.Speak("", 3)                      # purge
                break
        STATE.voice_level = 0.0

    def _play_loop(self):
        import sounddevice as sd
        stream, stream_sr = None, 0
        block = 1024
        while True:
            gen, audio, sr, meta = self.audio_q.get()      # meta: the sentence (audio) or the echo tail (end marker)
            if audio is None:
                if gen == self._gen:
                    self._done_one(meta)
                continue
            if gen != self._gen:
                continue
            if meta:
                STATE.caption("JARVIS", meta)              # caption each sentence at the moment it is spoken
            try:
                if stream is None or stream_sr != sr:
                    if stream:
                        stream.close()
                    stream = sd.OutputStream(samplerate=sr, channels=1, dtype="int16",
                                             device=self.cfg.get("output_device") or None)
                    stream.start()
                    stream_sr = sr
                pad = np.zeros(int(sr * 0.06), dtype=np.int16)
                audio = np.concatenate([pad, audio, pad])
                for i in range(0, len(audio), block):
                    if gen != self._gen:
                        break
                    chunk = audio[i:i + block]
                    STATE.voice_level = min(1.0, float(np.sqrt(np.mean((chunk / 32768.0) ** 2))) * 4.5)
                    stream.write(chunk.reshape(-1, 1))
                STATE.voice_level = 0.0
            except Exception as e:
                log.error("audio playback failed: %s", e)
                try:
                    stream and stream.close()
                except Exception:
                    pass
                stream = None


# ===================================================================== input
class Utterance:
    """One spoken phrase: Vosk's rough text + the raw audio Whisper can re-read accurately."""

    def __init__(self, text: str, audio: bytes, grammar_hit: bool, sample_rate: int = 16000,
                 neural: float = 0.0, neural_at: float | None = None):
        self.text, self.audio, self.grammar_hit = text, audio, grammar_hit
        self.duration = len(audio) / 2 / sample_rate
        self.whisper_text: str | None = None
        self.neural = neural              # best "Hey Jarvis" score (0..1) from the neural detector during this phrase
        self.neural_at = neural_at        # seconds into the phrase at which it first fired (None = it didn't)


WHISPER_HALLUCINATIONS = {"", "you", "thank you", "thank you.", "thanks for watching", "thanks for watching!",
                          "bye", "bye.", "[blank_audio]", "(music)", "[music]", "(silence)", ".", "...", "okay.",
                          "thank you for watching.", "thanks for watching.", "i'm sorry.", "so", "so.", "um", "uh"}


class WhisperSTT:
    """Whisper on ONNX Runtime (Microsoft-signed DLLs, so Windows Smart App Control allows it).
    'whisper-base' is the onnx-asr export with beam search inside the graph: ~0.4 s per phrase on this CPU
    (the onnx-community transformers.js export took 4-10 s)."""

    def __init__(self, model: str, language: str = "en"):
        import os
        import warnings
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        warnings.filterwarnings("ignore", module="huggingface_hub")
        import onnx_asr
        try:
            self.model = onnx_asr.load_model(model)
        except Exception:
            os.environ["HF_HUB_OFFLINE"] = "1"         # no network: use whatever is cached
            self.model = onnx_asr.load_model(model)
        self.language = language or None
        self.takes_language = True
        self.lock = threading.Lock()
        # First inference is slow (9 s in testing) while ONNX Runtime optimises the graph: pay that at boot.
        try:
            self.transcribe((np.random.default_rng(0).standard_normal(16000) * 300).astype(np.int16).tobytes())
        except Exception as e:
            log.warning("Whisper warm-up failed: %s", e)

    def transcribe(self, audio: bytes, sample_rate: int = 16000, hint: str = "") -> str:
        """`hint` is Vosk's rough transcript of the same audio. Whisper invents phrases like "Thank you." from
        noise; such a phrase is only believed when Vosk heard the same words."""
        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) < sample_rate * 0.3:
            return ""
        kw = {"language": self.language} if self.language and self.takes_language else {}
        with self.lock:
            try:
                text = str(self.model.recognize(samples, sample_rate=sample_rate, **kw)).strip()
            except TypeError:
                # Not every onnx-asr model takes a language (Parakeet v2 is English-only): remember and retry.
                self.takes_language = False
                text = str(self.model.recognize(samples, sample_rate=sample_rate)).strip()
        if re.fullmatch(r"[\[(].*[\])]", text):
            return ""
        if text.lower().strip() in WHISPER_HALLUCINATIONS:
            said = re.sub(r"[^a-z ]", "", text.lower()).strip()
            return text if said and said == re.sub(r"[^a-z ]", "", hint.lower()).strip() else ""
        return text


class Listener:
    """Always-on microphone. Every finished phrase becomes ('utt', Utterance) on the shared event queue.
    Audio is ignored while Jarvis is talking so he doesn't hear himself."""

    def __init__(self, cfg_voice: dict, speaker: Speaker, events: queue.Queue, whisper: WhisperSTT | None = None):
        import vosk
        vosk.SetLogLevel(-1)
        self.cfg = cfg_voice
        self.model = vosk.Model(str(resolve(cfg_voice["vosk_model"])))
        self.speaker = speaker
        self.whisper = whisper
        self.events = events
        self.device = self.find_device(cfg_voice.get("input_device", ""))
        self.sample_rate = 16000
        self.gain = float(cfg_voice.get("mic_gain", 1.0))
        self.grammar_conf = float(cfg_voice.get("wake_grammar_confidence", 0.85))
        self.wake_grammar = json.dumps(["jarvis", "hey jarvis", "ok jarvis", "[unk]"])
        self.status = "starting"
        self.hearing = False                  # True while someone is mid-phrase
        self.paused = threading.Event()
        # Neural "Hey Jarvis" detector: hears the phrase itself, so it doesn't depend on any transcript being right.
        self.neural = None
        self.neural_threshold = float(cfg_voice.get("neural_wake_threshold", 0.5))
        self.barge_in = bool(cfg_voice.get("barge_in", True))
        self.barge_threshold = float(cfg_voice.get("barge_in_threshold", 0.9))
        self.neural_score = 0.0               # latest score, for --mic-test
        if cfg_voice.get("neural_wake", True):
            from . import wakeword
            folder = resolve(cfg_voice.get("wake_model_dir", "models/wakeword"))
            if wakeword.available(folder):
                try:
                    self.neural = wakeword.NeuralWake(folder)
                    log.info("Neural wake-word detector loaded")
                except Exception as e:
                    log.warning("Neural wake word unavailable: %s", e)
            else:
                log.info("Neural wake-word models not found in %s (run setup_models.py)", folder)
        self.endpoint_silence = float(cfg_voice.get("endpoint_silence", 0.7))

    @staticmethod
    def find_device(name: str):
        if not name:
            return None
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and name.lower() in d["name"].lower():
                return i
        log.warning("Input device %r not found, using default", name)
        return None

    def start(self):
        threading.Thread(target=self._supervise, daemon=True, name="stt").start()

    def _supervise(self):
        warned = False
        while True:
            try:
                self._run()
            except Exception as e:
                self.status = f"microphone error: {e}"
                log.error("microphone failed: %s", e)
                if not warned:
                    warned = True
                    self.events.put(("system", "I've lost access to the microphone, sir. Retrying."))
                time.sleep(3)

    def _grammar_hit(self, result_json: str) -> bool:
        r = json.loads(result_json)
        return any(w.get("word") == "jarvis" and w.get("conf", 0) >= self.grammar_conf for w in r.get("result", []))

    def _run(self):
        import sounddevice as sd
        import vosk
        full = vosk.KaldiRecognizer(self.model, self.sample_rate)
        gram = vosk.KaldiRecognizer(self.model, self.sample_rate, self.wake_grammar)
        gram.SetWords(True)
        audio_q: queue.Queue[bytes] = queue.Queue()

        def callback(indata, frames, t, status):
            audio_q.put(bytes(indata))

        sr = self.sample_rate
        pre_roll = int(sr * 2 * 0.6)                     # keep 0.6 s before speech starts
        max_bytes = sr * 2 * int(self.cfg.get("max_utterance_sec", 20))
        with sd.RawInputStream(samplerate=sr, blocksize=1600, device=self.device, dtype="int16", channels=1,
                               callback=callback):
            log.info("Microphone open (device %s)", self.device if self.device is not None else "default")
            self.status = "listening"
            buf, start, in_speech, hit = bytearray(), 0, False, False
            silent_since, peak = time.time(), 0.0
            pos = 0                                       # bytes of audio received so far: a clock that never drifts
            hit_pos, scores = None, []                    # neural wake word: where it fired, and recent (pos, score)
            barge = 0                                     # consecutive high scores while Jarvis is speaking
            floor, quiet_since, last_partial, stable_since = 0.004, None, "", 0
            neural = self.neural

            def emit(text, hit):
                """Hand a finished phrase to the assistant, with whatever the neural detector made of it."""
                utt_start = pos - (len(buf) - start)
                best = max((s for p, s in scores if p >= utt_start - sr), default=0.0)
                at = None
                if hit_pos is not None and hit_pos >= utt_start:               # it fired inside this phrase
                    at = (hit_pos - utt_start) / 2 / sr
                self.hearing = False                      # the phrase is over (the assistant checks this at once)
                if (text and text not in ("the", "huh", "a")) or hit or at is not None:
                    log.info("heard: %r%s%s", text, " [wake-grammar]" if hit else "",
                             f" [neural {best:.2f}]" if best >= 0.2 else "")
                    self.events.put(("utt", Utterance(text, bytes(buf[start:]), hit, sr, best, at)))

            while True:
                data = audio_q.get()
                if self.gain != 1.0:
                    a = np.frombuffer(data, dtype=np.int16).astype(np.float32) * self.gain
                    data = np.clip(a, -32768, 32767).astype(np.int16).tobytes()
                samples = np.frombuffer(data, dtype=np.int16)
                pos += len(data)
                lvl = float(np.abs(samples).mean()) / 32768.0
                STATE.mic_level = min(1.0, lvl * 12)
                peak = max(peak, lvl)
                # A microphone that delivers pure digital silence is usually blocked by Windows privacy settings.
                if lvl > 0:
                    silent_since = time.time()
                elif time.time() - silent_since > 15:
                    silent_since = time.time()
                    self.status = "microphone is silent (muted or blocked in Windows privacy settings?)"
                    log.warning(self.status)
                    self.events.put(("system", "Sir, the microphone is delivering complete silence. It may be "
                                               "muted, or blocked under Windows privacy settings."))
                if self.speaker.speaking.is_set() or self.paused.is_set():
                    self.hearing = False
                    if buf or in_speech:
                        full.Reset()
                        gram.Reset()
                        buf, in_speech, hit = bytearray(), False, False
                        hit_pos, quiet_since, last_partial = None, None, ""
                    # Barge-in: "Hey Jarvis" said over his own voice stops him mid-sentence. No echo cancellation
                    # here, so it needs a very confident score twice in a row, and it is skipped while he is saying
                    # his own name (he would interrupt himself).
                    if neural and self.barge_in and not self.paused.is_set():
                        s = neural.feed(samples)
                        barge = barge + 1 if s >= self.barge_threshold else 0
                        if (barge >= 2 and "jarvis" not in STATE.live[1].lower()
                                and (hit_pos is None or pos - hit_pos > sr * 2 * 3)):
                            barge, hit_pos = 0, pos       # (also stops the same "Hey Jarvis" counting as a new wake)
                            log.info("barge-in (neural %.2f)", s)
                            self.events.put(("barge", s))
                    continue
                buf += data
                if neural:
                    s = self.neural_score = neural.feed(samples)
                    if s > 0.05:
                        scores.append((pos, s))
                        del scores[:-400]
                    if s >= self.neural_threshold and (hit_pos is None or pos - hit_pos > sr * 2 * 3):
                        hit_pos = pos                     # tell the assistant at once: the orb lights up right away
                        self.events.put(("wake", s))
                if gram.AcceptWaveform(data):
                    hit = hit or self._grammar_hit(gram.Result())
                if full.AcceptWaveform(data):
                    text = json.loads(full.Result()).get("text", "").strip()
                    hit = hit or self._grammar_hit(gram.FinalResult())
                    gram.Reset()
                    emit(text, hit)
                    buf, start, in_speech, hit = bytearray(), 0, False, False
                    quiet_since, last_partial = None, ""
                    continue
                self.hearing = in_speech
                partial = json.loads(full.PartialResult()).get("partial", "")
                if not in_speech:
                    floor += (min(lvl, 0.02) - floor) * 0.05             # running estimate of the room's noise level
                    if partial:
                        in_speech = self.hearing = True
                        start = max(0, len(buf) - len(data) - pre_roll)
                        last_partial, stable_since, quiet_since = partial, pos, None
                    elif len(buf) > pre_roll * 3:
                        del buf[:len(buf) - pre_roll]   # rolling pre-roll window while it's quiet
                elif len(buf) - start > max_bytes:
                    text = json.loads(full.FinalResult()).get("text", "").strip()
                    full.Reset()
                    gram.Reset()
                    if text:
                        emit(text, hit)
                    buf, start, in_speech, hit = bytearray(), 0, False, False
                    quiet_since, last_partial = None, ""
                else:
                    # Faster end-of-speech. Vosk waits 0.5 s of silence when it is sure the sentence is complete
                    # but 1-2 s when it isn't (accents, trailing "uh"). If the words have stopped changing AND the
                    # room has been quiet for `endpoint_silence`, the phrase is over: finish it now.
                    if partial != last_partial:
                        last_partial, stable_since = partial, pos
                    if lvl > max(floor * 2.5, 0.006):
                        quiet_since = None
                    elif quiet_since is None:
                        quiet_since = pos
                    need = int(self.endpoint_silence * sr * 2)
                    if (self.endpoint_silence > 0 and quiet_since is not None and pos - quiet_since >= need
                            and pos - stable_since >= need):
                        text = json.loads(full.FinalResult()).get("text", "").strip()
                        hit = hit or self._grammar_hit(gram.FinalResult())
                        full.Reset()
                        gram.Reset()
                        emit(text, hit)
                        buf, start, in_speech, hit = bytearray(), 0, False, False
                        quiet_since, last_partial = None, ""

    def tail_text(self, utt: Utterance, start_sec: float) -> str:
        """Whisper's transcript of the phrase from `start_sec` onwards (what was said after the wake word)."""
        if not self.whisper:
            return ""
        cut = int(max(0.0, start_sec) * self.sample_rate) * 2
        try:
            text = self.whisper.transcribe(utt.audio[cut:], self.sample_rate)
            log.info("whisper (after the wake word): %r", text)
            return text
        except Exception as e:
            log.error("Whisper failed on the tail: %s", e)
            return ""

    def accurate_text(self, utt: Utterance) -> str:
        """Whisper's transcript (cached per utterance); falls back to Vosk's."""
        if utt.whisper_text is not None:
            return utt.whisper_text
        if not self.whisper:
            utt.whisper_text = utt.text
            return utt.text
        t0 = time.time()
        try:
            utt.whisper_text = self.whisper.transcribe(utt.audio, self.sample_rate, hint=utt.text)
        except Exception as e:
            log.error("Whisper failed, using Vosk text: %s", e)
            utt.whisper_text = utt.text
        log.info("whisper (%.1fs): %r", time.time() - t0, utt.whisper_text)
        return utt.whisper_text
