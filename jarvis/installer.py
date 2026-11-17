"""`jarvis setup` and the other housekeeping commands (none of them start the assistant)."""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import ROOT, ensure_home, find_ollama

BRAINS = {
    "1": ("qwen2.5:7b", "best answers; wants a 6 GB+ NVIDIA GPU, an Apple-silicon Mac, or 16 GB RAM (4.7 GB download)"),
    "2": ("qwen2.5:3b", "lighter and faster on laptops without a big GPU (1.9 GB download)"),
}


def ask(question: str, default: str = "", assume: bool = False) -> str:
    if assume or not sys.stdin or not sys.stdin.isatty():
        return default
    try:
        answer = input(f"{question} " + (f"[{default}] " if default else "")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return answer or default


def yes(question: str, default: bool = True, assume: bool = False) -> bool:
    a = ask(question + (" (Y/n)" if default else " (y/N)"), "", assume).lower()
    return default if not a else a.startswith("y")


def set_option(key: str, value: str, section: str):
    """Change `key = "..."` inside [section] of config.toml, keeping every comment."""
    path = ROOT / "config.toml"
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"(?ms)^\[{re.escape(section)}\]\s*$(.*?)(?=^\[|\Z)", text)
    if not m:
        return
    body = re.sub(rf'(?m)^({re.escape(key)}\s*=\s*)"[^"]*"', lambda g: f'{g.group(1)}"{value}"', m.group(1), count=1)
    path.write_text(text[:m.start(1)] + body + text[m.end(1):], encoding="utf-8")


def step(n: int, title: str):
    print(f"\n=== [{n}/5] {title} " + "=" * max(0, 50 - len(title)))


def setup(argv: list[str]):
    assume = "--yes" in argv or "-y" in argv
    skip_brain = "--no-brain" in argv           # (used by the automated install test: no multi-GB download)
    mac = sys.platform == "darwin"
    if os.name != "nt" and not mac:
        sys.exit("Jarvis runs on Windows 10/11 and macOS.")
    home = ensure_home()
    print(f"J.A.R.V.I.S. setup\nEverything Jarvis keeps (settings, models, logs) lives in:\n  {home}")

    step(1, "Your name")
    name = ask("What should Jarvis call you?", "sir", assume)
    name = re.sub(r'["\\\n]', "", name)[:40] or "sir"
    set_option("owner_name", name, "assistant")

    step(2, "Brain (local language model via Ollama)")
    for k, (model, why) in BRAINS.items():
        print(f"  {k}) {model:12} {why}")
    model = BRAINS.get(ask("Choose 1 or 2:", "1", assume), BRAINS["1"])[0]
    set_option("model", model, "llm")

    step(3, "Speech, voice, face and wake-word models (~250 MB)")
    from .setup_models import main as fetch_models
    try:
        fetch_models([])
    except Exception as e:
        print(f"  Download failed ({e}). Run  jarvis setup-models  again later; finished files are kept.")

    step(4, "Ollama")
    exe = find_ollama()
    if not exe and not skip_brain:
        print("  Ollama (the program that runs the language model) is not installed.")
        if mac:
            exe = _install_ollama_mac(assume)
        elif shutil.which("winget") and yes("  Install it now with winget?", True, assume):
            subprocess.call(["winget", "install", "--id", "Ollama.Ollama", "-e",
                             "--accept-source-agreements", "--accept-package-agreements"])
            exe = find_ollama()
        if not exe:
            print("  Get it from https://ollama.com/download, then run:  ollama pull " + model)
    if exe and not skip_brain:
        _ensure_ollama_running(exe)
        have = subprocess.run([exe, "list"], capture_output=True, text=True).stdout
        if model in have:
            print(f"  {model} is already downloaded.")
        elif yes(f"  Download {model} now? (large)", True, assume):
            subprocess.call([exe, "pull", model])
        else:
            print(f"  Later, run:  ollama pull {model}")
    if skip_brain:
        print("  Skipped (--no-brain). Later: install Ollama, then  ollama pull " + model)
    if mac:
        print("\n  macOS will ask for a few permissions the first time each is needed (System Settings > Privacy &\n"
              "  Security, for the Terminal app): Microphone (to hear you), Accessibility (to press keys and type),\n"
              "  Screen Recording (screenshots / reading the screen), Camera (Face guard), Automation (to control apps).")

    step(5, "Face guard (optional)")
    print("  Face guard locks the laptop when a stranger is at the keyboard. It stays idle until you enrol your face.")
    if not assume and yes("  Enrol your face now? (opens the webcam)", False):
        from .enroll import main as enroll
        enroll()
    else:
        print("  Later:  jarvis enroll-face")

    print("\nSetup finished.\n"
          "  jarvis              start Jarvis (say \"Hey Jarvis\"" + (")\n" if mac else ", or press Ctrl+Alt+J)\n") +
          "  jarvis check        self-test every component\n"
          "  jarvis mic-test     check the microphone\n"
          "  jarvis config       edit the settings\n"
          "  jarvis --help       everything else")


def _install_ollama_mac(assume: bool) -> str | None:
    """Homebrew if it is there, otherwise the official app from ollama.com into /Applications."""
    if shutil.which("brew") and yes("  Install it now with Homebrew?", True, assume):
        subprocess.call(["brew", "install", "ollama"])
        return find_ollama()
    if yes("  Download the Ollama app now (about 200 MB)?", True, assume):
        import tempfile
        import urllib.request
        import zipfile
        apps = Path("/Applications") if os.access("/Applications", os.W_OK) else Path.home() / "Applications"
        apps.mkdir(exist_ok=True)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                z = Path(tmp) / "Ollama-darwin.zip"
                print("  Downloading Ollama ...", flush=True)
                urllib.request.urlretrieve("https://ollama.com/download/Ollama-darwin.zip", z)
                # `ditto` keeps the app's signature and permissions intact (Python's zipfile would not)
                subprocess.check_call(["ditto", "-x", "-k", str(z), str(apps)])
            subprocess.call(["open", "-g", str(apps / "Ollama.app")])
            time.sleep(4)
        except Exception as e:
            print(f"  That didn't work ({e}).")
    return find_ollama()


def _ensure_ollama_running(exe: str):
    """`ollama list` / `ollama pull` need the server; start it quietly if it isn't up."""
    import urllib.request
    for attempt in range(2):
        try:
            urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=2).read()
            return
        except Exception:
            if attempt == 0:
                from .config import NO_WINDOW
                try:
                    subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=NO_WINDOW)
                except Exception:
                    return
                time.sleep(4)


def _open(path, edit: bool = False):
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-t", str(path)] if edit else ["open", str(path)])
    elif edit:
        try:
            os.startfile(path, "edit")
        except OSError:
            subprocess.Popen(["notepad.exe", str(path)])
    else:
        os.startfile(path)


def open_config():
    ensure_home()
    path = ROOT / "config.toml"
    print(path)
    _open(path, edit=True)


def show_home():
    ensure_home()
    print(ROOT)
    _open(ROOT)


def coursera_mode():
    """Restart Chrome (normal profile, tabs restored) with the Jarvis Coursera helper loaded."""
    from .tools import _chrome_exe
    ensure_home()
    chrome, ext = _chrome_exe(), ROOT / "extension"
    if not chrome:
        sys.exit("Google Chrome was not found.")
    if not (ext / "manifest.json").exists():
        sys.exit(f"The Chrome helper is missing from {ext}.")
    print("Closing Chrome so the Jarvis Coursera helper can load (your tabs will reopen)...")
    if sys.platform == "darwin":
        subprocess.call(["osascript", "-e", 'tell application "Google Chrome" to quit'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.call(["taskkill", "/IM", "chrome.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    subprocess.Popen([chrome, "--profile-directory=Default", "--restore-last-session", f"--load-extension={ext}"])
    print("Done. Open your Coursera course, then tell Jarvis \"resume my coursera course\".")


def uninstall_data():
    if (ROOT / "jarvis" / "__main__.py").exists():
        sys.exit(f"{ROOT} is a source checkout, not an installed copy; refusing to delete it.")
    if yes(f"Delete {ROOT} (settings, models, logs, face data)?", False):
        shutil.rmtree(ROOT, ignore_errors=True)
        print("Deleted.")
