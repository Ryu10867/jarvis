"""Downloads everything Jarvis needs to run offline into <jarvis home>/models (speech, face, voice and wake-word models).

    jarvis setup-models                  the standard set (small files; Whisper base is ~150 MB)
    jarvis setup-models --stt parakeet   also fetch NVIDIA Parakeet (~650 MB): sharper and faster speech-to-text.
                                         Afterwards set  whisper_model = "nemo-parakeet-tdt-0.6b-v2"  in config.toml
"""
import sys
import urllib.request
import zipfile
from pathlib import Path

from .config import ROOT, ensure_home

MODELS = ROOT / "models"
FILES = {
    "face_detection_yunet_2023mar.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}
VOSK = ("vosk-model-small-en-us-0.15", "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip")
# Jarvis's voice: Piper neural TTS, British English male.
PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB"
PIPER = {
    "en_GB-alan-medium": f"{PIPER_BASE}/alan/medium/en_GB-alan-medium",
    "en_GB-northern_english_male-medium": f"{PIPER_BASE}/northern_english_male/medium/en_GB-northern_english_male-medium",
}
# Neural "Hey Jarvis" wake word (openWakeWord; model files CC BY-NC-SA 4.0). About 3.7 MB in total.
WAKE_BASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
WAKE = ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx")
OPTIONAL_STT = {"parakeet": "nemo-parakeet-tdt-0.6b-v2"}


def fetch(url: str, dest: Path):
    print(f"Downloading {dest.name} ...", flush=True)
    part = dest.with_name(dest.name + ".part")          # an interrupted download never looks finished
    urllib.request.urlretrieve(url, part)
    part.replace(dest)
    print(f"  {dest.stat().st_size // 1024} KB")


def main(argv: list[str] | None = None):
    argv = sys.argv[1:] if argv is None else argv
    ensure_home()
    MODELS.mkdir(exist_ok=True)
    print(f"Models folder: {MODELS}")
    for name, url in FILES.items():
        if not (MODELS / name).exists():
            fetch(url, MODELS / name)
    name, url = VOSK
    if not (MODELS / name).exists():
        z = MODELS / f"{name}.zip"
        fetch(url, z)
        with zipfile.ZipFile(z) as zf:
            for member in zf.namelist():   # refuse path traversal
                if member.startswith(("/", "\\")) or ".." in Path(member).parts:
                    raise RuntimeError(f"unsafe path in zip: {member}")
            zf.extractall(MODELS)
        z.unlink()
    (MODELS / "piper").mkdir(exist_ok=True)
    for name, url in PIPER.items():
        for ext in (".onnx", ".onnx.json"):
            dest = MODELS / "piper" / f"{name}{ext}"
            if not dest.exists():
                fetch(url + ext, dest)
    (MODELS / "wakeword").mkdir(exist_ok=True)
    for name in WAKE:
        dest = MODELS / "wakeword" / name
        if not dest.exists():
            fetch(WAKE_BASE + name, dest)
    try:                                   # Whisper is cached by huggingface_hub on first load
        import onnx_asr
        print("Fetching Whisper (whisper-base) ...", flush=True)
        onnx_asr.load_model("whisper-base")
        if "--stt" in argv:
            i = argv.index("--stt") + 1
            want = argv[i] if i < len(argv) else ""
            model = OPTIONAL_STT.get(want, want)
            if model:
                print(f"Fetching {model} (large download) ...")
                onnx_asr.load_model(model)
                print(f'  Done. To use it, set  whisper_model = "{model}"  in config.toml')
    except Exception as e:
        print(f"  Speech model will be downloaded on first run ({e})")
    print("All models ready.")


if __name__ == "__main__":
    main()
