import os
import shutil
import tomllib
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
_SOURCE = PACKAGE.parent


def _home() -> Path:
    """Where config.toml, data/ and models/ live.
    * JARVIS_HOME set           -> that folder
    * running from a source checkout (config.toml next to the jarvis/ package) -> the checkout itself
    * installed with pip        -> %LOCALAPPDATA%\\Jarvis"""
    env = os.environ.get("JARVIS_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if (_SOURCE / "config.toml").exists():
        return _SOURCE
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Jarvis"


ROOT = _home()
# The Chrome helper: next to the package in a source checkout, inside it in an installed wheel.
_EXT_SOURCES = (PACKAGE / "extension", _SOURCE / "extension")


def ensure_home() -> Path:
    """First run after `pip install`: create the home folder with a fresh config.toml and the Chrome helper."""
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "models").mkdir(exist_ok=True)
    cfg = ROOT / "config.toml"
    if not cfg.exists():
        default = PACKAGE / "default_config.toml"
        if default.exists():
            shutil.copyfile(default, cfg)
    ext = ROOT / "extension"
    for src in _EXT_SOURCES:
        if src.is_dir() and src.resolve() != ext.resolve():
            shutil.copytree(src, ext, dirs_exist_ok=True)   # refreshed on every upgrade
            break
    return ROOT


def find_ollama() -> str | None:
    """ollama.exe, also when it was installed in this session and PATH hasn't caught up yet."""
    found = shutil.which("ollama")
    if found:
        return found
    for p in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
              Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Ollama" / "ollama.exe"):
        if p.exists():
            return str(p)
    return None


def load_config(path: Path | None = None) -> dict:
    if path is None:
        path = ROOT / "config.toml"
        if not path.exists():
            ensure_home()
    with open(path, "rb") as f:
        return tomllib.load(f)


_KNOWN = {"desktop": 0x0000, "documents": 0x0005, "pictures": 0x0027, "music": 0x000D, "videos": 0x000E}


def known_folder(name: str) -> Path | None:
    """Real location of Desktop/Documents/... (handles OneDrive redirection)."""
    import ctypes
    csidl = _KNOWN.get(name.lower())
    if csidl is None:
        return None
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return Path(buf.value) if buf.value else None


def resolve(p: str) -> Path:
    """Resolve a config path: "known:Desktop" = the real Desktop folder, ~ expands to home,
    relative paths are relative to the Jarvis home folder."""
    if p.lower().startswith("known:"):          # "known:Pictures" or "known:Pictures/Jarvis Intruders"
        name, _, rest = p[6:].replace("\\", "/").partition("/")
        base = known_folder(name) or Path.home() / name
        return base / rest if rest else base
    path = Path(p).expanduser()
    return path if path.is_absolute() else ROOT / path
