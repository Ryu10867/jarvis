# J.A.R.V.I.S. — install guide

An offline, Iron-Man-style voice assistant for **Windows 10/11** and **macOS 12+**: say **"Hey Jarvis"** and talk
to it. Everything (speech, voice, the AI brain) runs on your own computer. Website: https://ryu10867.github.io/jarvis/

## What you need
- Windows 10/11, or a Mac with macOS 12 or newer (Apple silicon recommended)
- A microphone and speakers
- ~8 GB free disk space (the AI model is 2–5 GB)
- Internet for the one-time setup (~400 MB of libraries + ~250 MB of speech models + the AI model)
- Recommended: an NVIDIA GPU with 6 GB+, or an Apple-silicon Mac (otherwise pick the lighter brain during setup)

The installer gets **Python** and **Ollama** (the program that runs the AI model) for you if they're missing.

## Install on Windows
1. Unzip `Jarvis-Installer-Windows.zip` anywhere.
2. Double-click **`install.bat`**.
3. Answer the setup questions: your name, which brain, and whether to download it now.

Or paste this one line in PowerShell (nothing to download first):

    irm https://ryu10867.github.io/jarvis/install.ps1 | iex

Start Jarvis with the **Jarvis** shortcut on the Desktop, or type `jarvis` in a new terminal.

> If Windows says *"An Application Control policy has blocked this file"* (Smart App Control), use
> `python -m jarvis` instead of `jarvis.exe`. The installer already does this for you.

## Install on a Mac
1. Unzip `Jarvis-Installer-macOS.zip`.
2. Double-click **`Install Jarvis.command`**. If macOS says it "cannot be opened", right-click it > **Open** > **Open**.
3. Answer the setup questions.

Or paste this one line in Terminal (nothing to download first):

    curl -fsSL https://ryu10867.github.io/jarvis/install.sh | bash

Start Jarvis with **Jarvis** on the Desktop, or type `jarvis` in a new Terminal window.

The first time each is needed, macOS asks for permission (System Settings > Privacy & Security, for **Terminal**):
**Microphone** (to hear you), **Accessibility** (to press keys and type), **Screen Recording** (screenshots and
reading the screen), **Camera** (Face guard), **Automation** (to control apps). Say yes to the ones you want.

**The Mac version is new (a beta).** What differs from Windows:
- Jarvis appears as a window (the arc-reactor panel) rather than the floating orb.
- No global Ctrl+Alt+J hotkey yet: use the wake word, or click the reactor in the window.
- Security monitors on a Mac: Face guard, Network watch and File watch. (Process, USB and Login watch read
  Windows-only sources.) The security sweep and audit check the Mac's own protections instead: firewall,
  FileVault, Gatekeeper, System Integrity Protection.
- Shortcuts are translated for you: "new tab" presses Cmd+T, "go back" Cmd+[, and so on.

## Commands (both systems)
| Command | What it does |
|---|---|
| `jarvis` | Start Jarvis. Say **"Hey Jarvis"** (Windows: or click the orb / press **Ctrl+Alt+J**) |
| `jarvis setup` | Run the first-time setup again |
| `jarvis check` | Self-test every component |
| `jarvis mic-test` | See what the microphone hears (fix a quiet mic here) |
| `jarvis train-wake` | Teach Jarvis how *you* say "Jarvis" |
| `jarvis enroll-face` | Enrol your face so Face guard can lock the computer for strangers |
| `jarvis connect-google` | Link Gmail + Google Calendar so Jarvis tracks your deadlines |
| `jarvis agenda` | Print the deadlines, calendar and unread mail Jarvis knows about |
| `jarvis intruders` | Open the photos Face guard took of strangers |
| `jarvis config` | Edit settings (name, voice, colours, security...) |
| `jarvis home` | Open the folder with settings, models and logs |
| `jarvis --text` | Type instead of talking |
| `jarvis --help` | Everything else |

## Good to know
- **Face guard** stays off until you run `jarvis enroll-face`. Once enrolled, it locks the screen when an unknown
  face is at the keyboard and saves a photo in **Pictures/Jarvis Intruders**.
- **Coursera helper** (optional): `jarvis coursera` restarts Chrome with the helper loaded. Your tabs reopen.
- Settings live in `%LOCALAPPDATA%\Jarvis\config.toml` (Windows) or
  `~/Library/Application Support/Jarvis/config.toml` (Mac).

## Uninstall
Windows: double-click **`uninstall.bat`**. Mac: double-click **`Uninstall Jarvis.command`**.

## Credits / licences
Vosk, Whisper (via onnx-asr), Piper voices (rhasspy), OpenCV YuNet/SFace, Ollama + Qwen 2.5.
The "Hey Jarvis" wake-word model is from openWakeWord and is **CC BY-NC-SA 4.0: personal, non-commercial use only**.
It's downloaded to your own computer during setup, not bundled.
