"""Learned shortcuts: Jarvis gets faster at the things you actually say.

When the language model turns a sentence into one simple action ("get the calculator up" -> open_app calculator)
and that works, the pairing is remembered. Once the same sentence has led to the same action twice, it is done
instantly next time, without consulting the model (saving 1-3 seconds). Only plain, harmless actions are learned;
a shortcut that ever fails is forgotten, and "that was wrong" / "forget that shortcut" removes the last one used.
Stored in data/learned_commands.json."""
import json
import logging
import re
import threading

from .config import ROOT

log = logging.getLogger("jarvis.shortcuts")
FILE = ROOT / "data" / "learned_commands.json"
NEEDED = 2                     # times a sentence must give the same action before it becomes a shortcut
_lock = threading.Lock()
_data: dict | None = None
last_used: str | None = None   # key of the shortcut used most recently (for "that was wrong")


def key_of(text: str) -> str:
    t = re.sub(r"[^a-z0-9' ]+", " ", text.lower())
    t = re.sub(r"\b(jarvis|please|sir|okay|ok|so|um|uh|hey|now|for me|can you|could you|would you|will you)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _load() -> dict:
    global _data
    if _data is None:
        try:
            _data = json.loads(FILE.read_text(encoding="utf-8"))
        except Exception:
            _data = {}
    return _data


def _save():
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(_data, indent=1), encoding="utf-8")
    except Exception as e:
        log.debug("could not save shortcuts: %s", e)


def lookup(text: str) -> tuple[str, dict] | None:
    global last_used
    key = key_of(text)
    with _lock:
        hit = _load().get(key)
        if hit and hit.get("count", 0) >= NEEDED:
            last_used = key
            return hit["tool"], dict(hit["args"])
    return None


def learn(text: str, tool: str, args: dict):
    key = key_of(text)
    if len(key) < 4 or len(key) > 80:
        return
    with _lock:
        data = _load()
        hit = data.get(key)
        if hit and hit["tool"] == tool and hit["args"] == args:
            hit["count"] = hit.get("count", 1) + 1
        else:
            data[key] = {"tool": tool, "args": args, "count": 1}       # new, or the model changed its mind: start over
        if len(data) > 400:
            data.pop(next(iter(data)))
        _save()


def forget(text: str | None = None) -> bool:
    """Forget the shortcut for `text` (or the one used last)."""
    global last_used
    key = key_of(text) if text else last_used
    with _lock:
        if key and _load().pop(key, None) is not None:
            _save()
            last_used = None
            return True
    return False
