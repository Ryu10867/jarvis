"""Long-term memory: facts Jarvis is told to remember survive restarts and are shown to the model on every turn."""
import json
import threading
import time

from .config import ROOT

PATH = ROOT / "data" / "memory.json"
_lock = threading.Lock()


def _load() -> list[dict]:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items: list[dict]):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")


def remember(fact: str) -> str:
    fact = fact.strip()
    with _lock:
        items = _load()
        if any(i["fact"].lower() == fact.lower() for i in items):
            return "I already have that on record."
        items.append({"fact": fact, "when": time.strftime("%Y-%m-%d")})
        _save(items[-200:])
    return "Committed to memory."


def forget(about: str) -> str:
    words = [w for w in about.lower().split() if len(w) > 2]
    with _lock:
        items = _load()
        keep = [i for i in items if not (words and all(w in i["fact"].lower() for w in words))]
        if len(keep) == len(items):
            return f"I have nothing on record matching {about}."
        _save(keep)
    return f"Forgotten {len(items) - len(keep)} item(s)."


def facts(limit: int = 40) -> list[str]:
    return [i["fact"] for i in _load()[-limit:]]
