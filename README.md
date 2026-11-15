# J.A.R.V.I.S. — voice assistant + laptop security layer

An Iron-Man-style assistant for **Windows 10/11** that runs **fully offline** on your own laptop.

## Install
**Easiest:** download `Jarvis-Installer-x.y.z.zip` from the [Releases](../../releases) page, unzip it and double-click
**`install.bat`**. It sets up Python, Ollama, the speech models and a Desktop shortcut, and asks you a few questions.
See [packaging/INSTALL.md](packaging/INSTALL.md) for details.

**With Python 3.11+ already installed:**
```powershell
pip install "jarvis-assistant @ https://github.com/Ryu10867/jarvis/releases/latest/download/jarvis_assistant-1.0.0-py3-none-any.whl"
python -m jarvis setup      # your name, the AI model, speech models, optional face enrolment
python -m jarvis            # start (say "Hey Jarvis", click the orb, or press Ctrl+Alt+J)
```
**From source:** clone, `python -m venv .venv`, `.venv\Scripts\pip install -r requirements.txt`,
`.venv\Scripts\python setup_models.py`, then `run_jarvis.bat`. Build the installer with `build_package.ps1`.

## What's inside

| Part | What it uses |
|---|---|
| Face | A **floating 3D orb** on the desktop: no window, see-through, always on top. It turns, glows and reacts to what he is doing |
| Wake word | A neural **"Hey Jarvis"** detector (hears the sound of the phrase itself) + the older layers for plain "Jarvis": accent variants → "jarvis"-only recogniser → Whisper second opinion |
| Ears | **Vosk** (always-on, light) + **Whisper base** on ONNX Runtime (~0.4 s per phrase) |
| Brain | Instant **reflexes** for common commands, then **Ollama / qwen2.5:7b** with 43 tools, streamed |
| Voice | **Piper** neural TTS, British male (`en_GB-alan`), spoken sentence-by-sentence as the reply is generated |
| Eyes | OpenCV YuNet + SFace face recognition; Windows built-in OCR to read the screen |

> Why ONNX everywhere: Windows Smart App Control blocks unsigned DLLs (faster-whisper's `ctranslate2.dll`).
> ONNX Runtime is Microsoft-signed, so Whisper, Piper and the wake-word detector all run without weakening that protection.

## Run
| Command | What |
|---|---|
| `run_jarvis.bat` | Orb + voice. Say **"Hey Jarvis"**, click the orb, or press **Ctrl+Alt+J** |
| `run_jarvis.bat --orb-demo` | Just the orb, cycling through its states. Nothing listens |
| `run_jarvis.bat --check` | Self-test of every component. Does **not** start Jarvis |
| `run_jarvis.bat --mic-test` | 12 s microphone test: level meter + what each recogniser hears + the wake score |
| `run_jarvis.bat --train-wake` | Say "Jarvis" 6 times; whatever the recogniser makes of your accent becomes a wake word |
| `run_jarvis.bat --text` | No microphone; type in the orb's console or the terminal |
| `run_jarvis.bat --panel` | The old arc-reactor window instead of the orb |
| `run_jarvis.bat --mute` / `--no-hud` / `--no-security` | No voice / console only / assistant only |
| Right-click → *Run as administrator* | Also enables failed-login alerts |

## The orb
| You do | It does |
|---|---|
| Click | Push-to-talk (same as Ctrl+Alt+J) |
| Double-click | Opens the **console**: transcript, system gauges and a box to type commands |
| Right-click | Menu: talk, console, colour, size, hide, quit |
| Drag | Moves it anywhere (position is remembered) |
| Mouse wheel over it | System volume up / down |
| Middle-click | Stops him talking |
| Move the pointer near it | It leans towards the pointer and brightens |

| State | Look |
|---|---|
| Starting | The sphere assembles itself as each system comes online |
| Standing by | Slow turn, dim |
| Listening | Brighter, a ring of light goes out, ticks round the ball follow your microphone |
| Thinking | Fast spin, deeper colour, a scan ring sweeps the ball |
| Speaking | Core and surface pulse with his voice |
| Security alert | Red |

Captions under the orb show what was heard and what is being said. It hides itself while a game or full-screen
video is running (he still listens). By voice: *"Jarvis, hide"*, *"show yourself"*, *"turn blue"* (gold, blue, green,
purple, red), *"get bigger / smaller"*, *"move to the top left corner"*, *"open the console"*.

## Talking to him
- **"Hey Jarvis"** → a soft chime → speak your command. Or in one breath: **"Hey Jarvis, open Chrome."**
  Plain "Jarvis" works too, but "Hey Jarvis" is recognised far more reliably.
- After every answer he keeps listening for ~7 s, so follow-ups need **no wake word**.
- **"Jarvis, stay with me"** = conversation mode: he keeps listening after every reply until **"that's all"**.
- **"Thank you" / "that's all" / "never mind"** ends the conversation. **"Goodbye"** shuts Jarvis down.
- **Interrupt him** by saying "Hey Jarvis" over him, clicking the orb, or Ctrl+Alt+J.
- **"Again"** repeats the last action; **"what did you say?"** repeats the last reply; **"close it"** closes whatever
  he opened last.
- You can always type in the orb's console instead (double-click the orb).

### If he doesn't hear the wake word
1. Say **"Hey Jarvis"** rather than just "Jarvis".
2. `run_jarvis.bat --mic-test` — check the level bar moves; the `neural` number should jump towards 1.0 on "Hey Jarvis".
3. Too deaf → lower `neural_wake_threshold` (0.5 → 0.4). Wakes by mistake → raise it (0.6-0.7).
4. Quiet microphone → `mic_gain = 3.0` in `config.toml`. Wrong microphone → `input_device = "part of its name"`.
5. Ctrl+Alt+J and clicking the orb always work regardless.

## What he can do
**Instant reflexes (no thinking delay):** time/date, battery, open apps / sites / folders ("open Chrome", "get the
calculator up", "open YouTube in a new tab"), close apps, browser shortcuts (new tab, close tab, back, refresh, full
screen), scrolling, volume, brightness, media keys, play *X* on YouTube, web search, lock, screenshot, show desktop,
window minimise/maximise/switch, dictation ("type …"), notes, reminders / timers / alarms, "remember that …",
weather, arithmetic ("what's 18 percent of 2450"), which Wi-Fi / IP address, full **diagnostics** report,
**security sweep**, shutdown/restart/sleep, **protocols**, and two commands in one sentence ("open notepad and set
the volume to 20").

**Through the language model:** conversation with the Jarvis personality, general knowledge, "what's on my screen"
(OCR + summary), clipboard, finding/opening/deleting files, processes, anything phrased unusually.

**He gets faster with use:** a sentence that led to the same simple action twice is done instantly from then on
(`data/learned_commands.json`). Say *"that was wrong"* to make him unlearn the last one.

**Memory:** "Jarvis, remember that my project deadline is the 20th" survives restarts (`data/memory.json`).
Reminders survive restarts too (`data/reminders.json`).

**Protocols** (`[protocols.*]` in `config.toml`): "Jarvis, work protocol", "initiate party protocol",
"clean slate protocol", "focus protocol", "lockdown protocol", "goodnight protocol". Add your own.

**Unprompted:** low-battery warnings, "we've lost the internet connection" / "back online", a caption when the
charger is connected or removed, "welcome back" when you return. Never spoken during `quiet_hours` (23:00-07:00).

**Coursera** (needs the small Chrome extension in `extension/`, one-time setup: open `chrome://extensions`, turn on
*Developer mode*, click *Load unpacked*, pick the `jarvis\extension` folder):
- "Resume my Coursera course" / "play my Coursera course on *X*" → reopens the last lecture of that course and
  turns **autoplay** on. "Open Coursera" just opens it (My Learning).
- With a lecture open: "next video", "skip", "previous video", "pause", "play", "restart the video", "go back 30
  seconds", "fast forward", "speed 1.5", "speed up", "slow down", "normal speed", "how much is left".
- "Autoplay on" / "play all the videos one after another" / "autoplay off". Autoplay plays each video and, when
  it ends, opens the next video. Readings in between are skipped. It **stops at graded items** (quizzes, assignments)
  and says so; "next video" goes past them. It never answers, submits or marks anything complete.
- Autoplay runs inside Chrome, so it keeps going even with Jarvis closed. The toolbar button toggles it too.
- If Chrome blocks sound the first time, the video plays muted. Click the player's speaker icon once and the rest
  will play with sound.
- **Progress**: "how many videos left" / "my Coursera progress" — he tells you how many lecture videos you've
  watched and how many remain. "Open the Coursera dashboard" opens a live progress board (one card per course,
  a progress bar and the lecture list) served locally at `http://127.0.0.1:8765/dashboard`.
- Pop-ups (cookie bars, "continue" nags) are dismissed automatically during autoplay; assessment dialogs are left alone.
- Completion is read from Coursera's own markers and Coursera records it the normal way — Jarvis never marks
  anything complete itself.
- Words like "next", "pause" and "skip" mean Coursera only while a Coursera tab is open. Otherwise they are media keys.

**Starter code:** click inside a code editor, then say "write the Java template" (also C++, C, Python; "Java
template with class Solution" for another class name). Jarvis **replaces what's in the editor** with the imports,
class and `main` (Ctrl+Z brings the old text back), then leaves the cursor on the empty line for your own logic.
It only types the template, never the solution.

**Security lab** (for the cybersecurity project — all of it targets your own systems, a CTF, or a box you control):
- "Scan my network" — finds live devices and open ports on your own LAN and flags risky exposed services.
- "Audit this laptop" / "how exposed am I" — firewall, exposed listening ports, Defender, Remote Desktop, last update.
- "Port scan my VM at 192.168.1.50" — port + banner scan of one host you name; asks to confirm it's yours first.
- CTF helper (`ctf_tool`): hash text, identify a hash/encoding, encode or decode (base64/base32/hex/url/rot13/binary),
  or dictionary-crack a hash you own. Drop a `data/wordlist.txt` to extend the crack.
- These will **not** touch systems you don't own. Jarvis has no tooling to break into, alter, or quietly pull data
  from a third-party website — that was asked for and deliberately left out.

**Risky actions** (power, delete, force-kill, run PowerShell, empty Recycle Bin) always ask *"Shall I …? Yes or
no."* and are **refused outright if Face guard sees a stranger** at the keyboard. Closing an app is done the polite
way (like clicking X, so the app itself asks about unsaved work) and needs no confirmation; "kill …" / "force close …"
is the hard way and does.

## Security monitors
| Monitor | Detects |
|---|---|
| Face guard | Unknown face for 3 checks → photo in `data/intruders/` + screen lock. Second face → shoulder-surfer warning. Auto-corrects an upside-down webcam image, pauses while the screen is locked, says "Welcome back, sir" |
| Process watch | Encoded/hidden PowerShell, certutil/mshta/regsvr32 abuse, shadow-copy deletion (ransomware), Office spawning shells, programs from Temp/Downloads, new startup entries & scheduled tasks, no antivirus active |
| Network watch | Connections to known backdoor ports, programs in temp folders talking to the internet, a program listening for connections for the first time (remembered in `data/known_listeners.json`) |
| USB watch | USB drives (warns if autorun/executables), new USB keyboards (BadUSB/rubber ducky) |
| Login watch | Failed Windows logins + webcam photo of whoever tried *(needs admin)* |
| File watch | Mass file changes, encrypted extensions, ransom notes in Documents/Desktop/Pictures |

Alerts → the orb turns red + Windows toast + spoken + `data/alerts.jsonl`. Ask *"Jarvis, are we secure?"* any time.

## Tuning (`config.toml`)
- Orb: `[ui]` `orb_size`, `orb_theme`, `orb_glass`, `captions`, `hide_in_fullscreen`; `style = "panel"` for the old window.
- Wake word: `neural_wake_threshold`, `wake_ack` (`"chime"`, `"voice"` for a spoken "Sir?", or `"none"`), `barge_in`.
- Snappiness: `endpoint_silence` (how long a pause ends your sentence), `fast_reflex`, `learn_shortcuts`.
- Voice: `piper_voice` (try `en_GB-northern_english_male-medium.onnx`), `piper_length_scale` (speed).
- Brain speed: `num_ctx = 4096` keeps most of qwen2.5:7b on the GPU. A smaller model is faster still (see below).
- Conversation window: `follow_up_seconds` (0 = always require the wake word), `conversation_seconds`.
- Unprompted remarks: `[proactive]` `network`, `power`, `quiet_hours`.
- Face guard too strict/lenient → `match_threshold`. Re-run `enroll_face.py` in different lighting for robustness.
- Large OneDrive syncs can trip the file-burst alarm → raise `burst_threshold`.
- Speak Hindi/Hinglish → `whisper_language = ""` (auto-detect).

### Optional upgrades (each is a download, so they are not installed automatically)
Neither has been tried on this machine yet; both are one setting to undo.
- **Sharper ears:** `python setup_models.py --stt parakeet` fetches NVIDIA Parakeet (a large download, several hundred
  MB), reported to be both more accurate and faster than Whisper base; then set
  `whisper_model = "nemo-parakeet-tdt-0.6b-v2"`. To go back, set it to `"whisper-base"` again.
- **Faster brain:** qwen2.5:7b only fits about 80% in the 6 GB of video memory (≈21 words-pieces a second). A
  4-billion-parameter model fits entirely and should answer faster, possibly at the cost of picking the wrong action
  more often: `ollama pull qwen3:4b` (≈2.5 GB), then `model = "qwen3:4b"`.

## Files
```
jarvis/__main__.py   boot sequence, main loop, --check / --mic-test / --train-wake / --orb-demo
jarvis/orb.py        the floating 3D orb, captions, console, menu     jarvis/hud.py   the old arc-reactor window
jarvis/speech.py     Speaker (Piper/SAPI, chime), Listener (Vosk + neural wake word), WhisperSTT
jarvis/wakeword.py   neural "Hey Jarvis" detector       jarvis/wake.py      wake-word decision logic
jarvis/router.py     instant reflex commands            jarvis/shortcuts.py learned shortcuts
jarvis/brain.py      LLM + tool calling + streaming     jarvis/tools.py     the 49 tools
jarvis/coursera.py   local link (127.0.0.1:8765) to the Chrome extension in extension/
jarvis/memory.py     long-term facts                    jarvis/reminders.py persistent reminders
jarvis/hotkey.py     Ctrl+Alt+J                         jarvis/state.py     what he is doing right now
jarvis/security/     the six monitors + on-demand sweep
```

Wake-word models: [openWakeWord](https://github.com/dscripka/openWakeWord) "hey_jarvis" (code Apache-2.0; the
pretrained model files are CC BY-NC-SA 4.0, i.e. free for personal, non-commercial use).

## Start automatically at login
```powershell
schtasks /create /tn "Jarvis" /tr "%LOCALAPPDATA%\Programs\Jarvis\bin\jarvis.cmd" /sc onlogon /rl highest
# (from a source checkout use the full path to run_jarvis.bat instead)
```
