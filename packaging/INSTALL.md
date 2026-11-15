# J.A.R.V.I.S. — install guide

An offline, Iron-Man-style voice assistant for **Windows 10/11**: say **"Hey Jarvis"** and talk to it.
Everything (speech, voice, the AI brain) runs on your own laptop.

## What you need
- Windows 10 or 11, a microphone and speakers
- ~8 GB free disk space (the AI model is 2–5 GB)
- Internet for the one-time setup (~300 MB of libraries + ~250 MB of speech models + the AI model)
- Recommended: an NVIDIA GPU with 6 GB+ (otherwise pick the lighter brain during setup)

The installer gets **Python** and **Ollama** (the program that runs the AI model) for you through `winget` if
they're missing.

## Install
1. Unzip this folder anywhere.
2. Double-click **`install.bat`** (or, in a terminal: `powershell -ExecutionPolicy Bypass -File install.ps1`).
3. Answer the setup questions: your name, which brain, and whether to download it now.

That's it. Start Jarvis with the **Jarvis** shortcut on the Desktop, or type `jarvis` in a new terminal.

> Already use Python? You can also do  `pip install jarvis_assistant-1.0.0-py3-none-any.whl`  then  `jarvis setup`.
> If Windows says *"An Application Control policy has blocked this file"* (Smart App Control), use
> `python -m jarvis setup` / `python -m jarvis` instead. The installer above already does this for you.

## Commands
| Command | What it does |
|---|---|
| `jarvis` | Start Jarvis. Say **"Hey Jarvis"**, click the orb, or press **Ctrl+Alt+J** |
| `jarvis setup` | Run the first-time setup again |
| `jarvis check` | Self-test every component |
| `jarvis mic-test` | See what the microphone hears (fix a quiet mic here) |
| `jarvis train-wake` | Teach Jarvis how *you* say "Jarvis" |
| `jarvis enroll-face` | Enrol your face so Face guard can lock the laptop for strangers |
| `jarvis intruders` | Open the photos Face guard took of strangers |
| `jarvis config` | Edit settings (name, voice, colours, security...) |
| `jarvis home` | Open the folder with settings, models and logs |
| `jarvis --text` | Type instead of talking |
| `jarvis --help` | Everything else |

## Good to know
- **Face guard** stays off until you run `jarvis enroll-face`. Once enrolled, it locks the screen when an unknown
  face is at the keyboard and saves a photo in **Pictures\Jarvis Intruders**.
- **Coursera helper** (optional): `jarvis coursera` restarts Chrome with the helper loaded. Your tabs reopen.
- Settings live in `%LOCALAPPDATA%\Jarvis\config.toml`.
- Windows may ask for microphone/camera permission the first time.

## Uninstall
Double-click **`uninstall.bat`**.

## Credits / licences
Vosk, Whisper (via onnx-asr), Piper voices (rhasspy), OpenCV YuNet/SFace, Ollama + Qwen 2.5.
The "Hey Jarvis" wake-word model is from openWakeWord and is **CC BY-NC-SA 4.0: personal, non-commercial use only**.
It's downloaded to your own laptop during setup, not bundled.
