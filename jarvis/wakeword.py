"""Neural wake-word detector: listens for "Hey Jarvis" in the raw microphone audio.

The first wake-word system relied on speech-to-text getting the word "Jarvis" right, and with an Indian accent it
often doesn't (the live log has it as "dallas", "childish", "yeah race", "errors"...). This detector doesn't
transcribe anything: it is a small neural network trained on ~200,000 spoken examples of the phrase itself, so it
recognises the *sound* of it. It runs on ONNX Runtime (already used by Whisper and Piper), fully offline, and costs
well under 1% of one CPU core.

Models: openWakeWord (github.com/dscripka/openWakeWord), "hey_jarvis" v0.1. Code licence Apache-2.0; the pretrained
model files are CC BY-NC-SA 4.0 (free for personal use). Fetched by setup_models.py into models/wakeword/.

Pipeline (the same as openWakeWord's own, re-implemented here so no extra packages are needed):
    16 kHz audio --80 ms steps--> mel spectrogram --76-frame window--> 96-number speech embedding
    --last 16 embeddings (~1.3 s)--> wake-word score 0..1
"""
import logging
from pathlib import Path

import numpy as np

log = logging.getLogger("jarvis.wakeword")
CHUNK = 1280                      # 80 ms at 16 kHz: the step the models were trained with
FILES = ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx")
RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"


def available(folder: Path) -> bool:
    return all((folder / f).exists() for f in FILES)


class NeuralWake:
    def __init__(self, folder: Path, model: str = FILES[2]):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3

        def load(name):
            return ort.InferenceSession(str(folder / name), opts, providers=["CPUExecutionProvider"])
        self.mel, self.emb, self.kw = load(FILES[0]), load(FILES[1]), load(model)
        self.mel_in = self.mel.get_inputs()[0].name
        self.emb_in = self.emb.get_inputs()[0].name
        self.kw_in = self.kw.get_inputs()[0].name
        self.frames = int(self.kw.get_inputs()[0].shape[1])            # 16 embeddings = ~1.3 s of context
        # Start from the embeddings of quiet noise, as the reference implementation does, so the first second
        # after start-up isn't judged against an all-zero history.
        rng = np.random.default_rng(0)
        self._quiet = self._embed_clip(rng.integers(-1000, 1000, 16000 * 4).astype(np.int16))
        self.reset()

    def _melspec(self, samples: np.ndarray) -> np.ndarray:
        spec = self.mel.run(None, {self.mel_in: samples[None, :].astype(np.float32)})[0]
        return np.squeeze(spec, axis=(0, 1)) / 10.0 + 2.0              # (frames, 32), scaled as in training

    def _embed_clip(self, samples: np.ndarray) -> np.ndarray:
        spec = self._melspec(samples)
        windows = [spec[i:i + 76] for i in range(0, spec.shape[0] - 75, 8)]
        batch = np.asarray(windows, np.float32)[..., None]
        return self.emb.run(None, {self.emb_in: batch})[0].reshape(len(windows), -1)

    def reset(self):
        self.raw = np.zeros(480, np.int16)                             # 30 ms of history for the first window
        self.pending = np.zeros(0, np.int16)
        self.melbuf = np.ones((76, 32), np.float32)
        self.feat = self._quiet[-self.frames:].copy()
        self.steps = 0

    def feed(self, samples: np.ndarray) -> float:
        """Add microphone audio (int16, 16 kHz, any length). Returns the highest wake score (0..1) among the
        80 ms steps completed by this call, or 0.0 if none completed."""
        self.pending = np.concatenate([self.pending, samples])
        best = 0.0
        while len(self.pending) >= CHUNK:
            chunk, self.pending = self.pending[:CHUNK], self.pending[CHUNK:]
            self.raw = np.concatenate([self.raw, chunk])[-(CHUNK + 480):]
            self.melbuf = np.vstack([self.melbuf, self._melspec(self.raw)])[-152:]
            window = self.melbuf[-76:][None, :, :, None].astype(np.float32)
            emb = self.emb.run(None, {self.emb_in: window})[0].reshape(1, -1)
            self.feat = np.vstack([self.feat, emb])[-self.frames:]
            score = float(np.squeeze(self.kw.run(None, {self.kw_in: self.feat[None].astype(np.float32)})[0]))
            self.steps += 1
            if self.steps > 5:                                         # the first steps after a reset are unreliable
                best = max(best, score)
        return best
