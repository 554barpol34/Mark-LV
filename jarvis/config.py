"""
Settings and paths.

Everything JARVIS keeps lives in ~/.jarvis (settings, memory, the browser
profile, logs). Files it makes for you go to the workspace, Documents/JARVIS
by default, so you can find them without hunting.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("JARVIS_HOME") or Path.home() / ".jarvis")
CONFIG_FILE = DATA_DIR / "config.json"
REPO_DIR = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "gemini_api_key": "",
    # Tried first, in order; then the newest available Flash models are added.
    "models": [],               # empty = the newest Flash models your key can use
    "workspace": "",            # empty = Documents/JARVIS
    "browser": "opera",         # opera | operagx | path to any Chromium-based browser
    "browser_path": "",         # set this if Opera is installed somewhere unusual
    "speak": True,              # read answers out loud
    "voice": "",                # edge-tts voice; empty = picked from the answer's language
    "language": "tr",           # language JARVIS listens for and answers in by default
    "max_steps": 40,
}

_lock = threading.Lock()
_cache: dict | None = None


def load() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            data = {}
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
            _cache = {**DEFAULTS, **data}
        return dict(_cache)


def get(key: str):
    return load().get(key, DEFAULTS.get(key))


def set(key: str, value) -> None:  # noqa: A001 - mirrors get()
    global _cache
    cfg = load()
    cfg[key] = value
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        saved = {k: v for k, v in cfg.items() if DEFAULTS.get(k) != v}
        CONFIG_FILE.write_text(json.dumps(saved, indent=2, ensure_ascii=False), encoding="utf-8")
        _cache = cfg


def api_key() -> str:
    """Env var first, then our settings, then the key Mark-LV already stored."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or get("gemini_api_key")
    if key:
        return key
    try:
        old = json.loads((REPO_DIR / "config" / "api_keys.json").read_text(encoding="utf-8"))
        return str(old.get("gemini_api_key") or "")
    except Exception:
        return ""


def workspace() -> Path:
    path = Path(get("workspace") or Path.home() / "Documents" / "JARVIS").expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve(path: str) -> Path:
    """A user path: absolute stays as is, ~ expands, anything else is in the workspace."""
    p = Path(os.path.expandvars(str(path or "").strip().strip('"'))).expanduser()
    return p if p.is_absolute() else workspace() / p
