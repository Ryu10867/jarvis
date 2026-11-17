"""Smoke test run by .github/workflows/installers.yml on real Windows and macOS machines, after the installer.

Run with the *installed* Jarvis's Python. There is no microphone, speaker, camera or language model on a test
machine, so this checks everything up to them: the package and every module import, the models downloaded, the
voice synthesises speech, Whisper and Vosk understand that speech, the reflex router turns it into the right
action, and (on a Mac) the system layer really talks to macOS.
"""
import importlib
import platform
import sys
import time

fails = []


def check(name, fn):
    t = time.time()
    try:
        detail = fn()
        print(f"  [OK]   {name}{' - ' + str(detail) if detail else ''}  ({time.time() - t:.1f}s)", flush=True)
    except Exception as e:
        fails.append(name)
        print(f"  [FAIL] {name} - {type(e).__name__}: {e}", flush=True)


def soft(name, fn):
    """Things a headless test machine may legitimately refuse (no audio device, no permissions): report, don't fail."""
    try:
        print(f"  [info] {name}: {fn()}", flush=True)
    except Exception as e:
        print(f"  [info] {name}: unavailable here ({type(e).__name__}: {e})", flush=True)


print(f"Jarvis smoke test on {platform.platform()} / Python {platform.python_version()} / {platform.machine()}")
MODULES = ["config", "state", "alerts", "tools", "router", "brain", "agenda", "installer", "hotkey", "speech", "reminders",
           "memory", "shortcuts", "coursera", "seclab", "wake", "wakeword", "enroll", "security", "security.face_guard",
           "security.network_watch", "security.file_watch", "security.signing", "__main__"]
if sys.platform == "darwin":
    MODULES += ["macos"]
for m in MODULES:
    check(f"import jarvis.{m}", lambda m=m: importlib.import_module("jarvis." + m) and None)

import numpy as np

from jarvis import router, tools
from jarvis.config import ROOT, load_config, resolve

cfg = load_config()
tools.Ctx.cfg = cfg
check("home folder and config", lambda: str(ROOT) if (ROOT / "config.toml").exists() else 1 / 0)
check("tools registered", lambda: f"{len(tools.REGISTRY)} tools" if len(tools.REGISTRY) >= 50 else 1 / 0)
def apps():
    n = len(tools.refresh_apps(force=True))
    assert n > 5, f"only {n} apps found"
    return f"{n} apps"


check("apps discovered", apps)

v = cfg["voice"]
check("models downloaded", lambda: [resolve(v["vosk_model"]).stat(), resolve(v["piper_voice"]).stat()] and None)

# --- the voice says a command, the recognisers hear it, the router understands it
audio16 = {}


def speak():
    from piper import PiperVoice, SynthesisConfig
    voice = PiperVoice.load(str(resolve(v["piper_voice"])))
    a = np.frombuffer(b"".join(c.audio_int16_bytes for c in voice.synthesize("What time is it?", SynthesisConfig())),
                      dtype=np.int16).astype(np.float32)
    sr = voice.config.sample_rate
    t = np.arange(int(len(a) * 16000 / sr)) / 16000
    audio16["pcm"] = np.interp(t, np.arange(len(a)) / sr, a).astype(np.int16).tobytes()
    return f"{len(a) / sr:.1f}s of speech"


def whisper():
    from jarvis.speech import WhisperSTT
    text = WhisperSTT(v["whisper_model"], "en").transcribe(audio16["pcm"])
    assert "time" in text.lower(), text
    assert router.probe(text), f"router did not recognise {text!r}"
    return repr(text)


def vosk():
    import json
    import vosk as vk
    vk.SetLogLevel(-1)
    rec = vk.KaldiRecognizer(vk.Model(str(resolve(v["vosk_model"]))), 16000)
    rec.AcceptWaveform(audio16["pcm"])
    text = json.loads(rec.FinalResult()).get("text", "")
    assert text, "nothing recognised"
    return repr(text)


def wake():
    from jarvis import wakeword
    folder = resolve(v.get("wake_model_dir", "models/wakeword"))
    assert wakeword.available(folder), "wake-word models missing"
    w = wakeword.NeuralWake(folder)
    return f"score on silence {w.feed(np.zeros(1600, dtype=np.int16)):.2f}"


check("Piper voice synthesises speech", speak)
check("Whisper understands it, router knows the command", whisper)
check("Vosk (always-on recogniser)", vosk)
check("neural wake word loads", wake)
check("reflex: time", lambda: router.route("what time is it"))
check("reflex: maths goes to the calculator", lambda: tools.calculate("18% of 2450"))
check("face models load", lambda: importlib.import_module("jarvis.security.face_engine").FaceEngine() and None)

if sys.platform == "darwin":
    from jarvis import macos

    def osa():
        out = macos.osa("return 1 + 1")
        assert out == "2", out
        return "AppleScript works"

    def ocr():
        import cv2
        img = np.full((160, 900, 3), 255, np.uint8)
        cv2.putText(img, "JARVIS READS THE SCREEN 42", (20, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 4)
        p = ROOT / "data" / "ocr_test.png"
        cv2.imwrite(str(p), img)
        text = macos.ocr_image(p)
        p.unlink()
        assert "JARVIS" in text.upper() and "42" in text, repr(text)
        return repr(text)

    def clip():
        macos.clipboard("write", "jarvis-clipboard-test")
        out = macos.clipboard("read")
        assert "jarvis-clipboard-test" in out, out

    check("osascript", osa)
    check("Vision OCR (reading the screen)", ocr)
    check("clipboard round trip", clip)
    check("Safari / Finder found", lambda: tools.known_app("safari") and tools.known_app("finder") or 1 / 0)
    check("signature lookup", lambda: macos.signer("/System/Applications/Calculator.app/Contents/MacOS/Calculator") or 1 / 0)
    check("security audit runs", lambda: macos.security_audit()[:90])
    check("security sweep runs", lambda: macos.security_sweep()[:90])
    check("diagnostics", lambda: tools.run_diagnostics()[:90])
    check("network info", lambda: tools.network_info())
    check("notification", lambda: macos.notify("Jarvis", "smoke test"))
    check("open an app", lambda: macos.open_app("calculator"))
    time.sleep(2)
    soft("close it again", lambda: macos.close_app("calculator"))
    soft("volume", lambda: macos.volume("set", 20))
    soft("key press (needs Accessibility)", lambda: macos.press_keys("esc"))
    soft("screenshot (needs Screen Recording)", lambda: macos.screenshot())
    soft("screen locked?", macos.screen_locked)
    soft("brightness", lambda: macos.set_brightness(60))
    soft("Tk window toolkit", lambda: importlib.import_module("tkinter").Tcl().eval("info patchlevel"))
else:
    check("diagnostics", lambda: tools.run_diagnostics()[:90])
    check("network info", lambda: tools.network_info())
    soft("clipboard", lambda: tools.clipboard("read")[:40])

print("\nSMOKE TEST " + ("PASSED" if not fails else f"FAILED: {', '.join(fails)}"))
sys.exit(1 if fails else 0)
