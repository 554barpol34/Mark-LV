"""Things the user asked JARVIS to remember. They go into every conversation's instructions."""
from __future__ import annotations

import json
from datetime import date

from jarvis import config
from jarvis.tools import tool

FILE = config.DATA_DIR / "memory.json"


def facts() -> list[str]:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


@tool("remember", "Save a lasting fact or preference about the user (only when they ask, or it is clearly useful later).",
      {"fact": {"type": "string"}}, ["fact"], label=lambda a: "Hafızaya yazılıyor")
def remember(fact: str):
    items = facts()
    items.append(f"{fact.strip()} ({date.today().isoformat()})")
    _save(items[-100:])
    return "Saved."


@tool("forget", "Delete remembered facts containing these words.",
      {"words": {"type": "string"}}, ["words"], label=lambda a: "Hafızadan siliniyor")
def forget(words: str):
    items = facts()
    keep = [f for f in items if words.lower() not in f.lower()]
    _save(keep)
    return f"Removed {len(items) - len(keep)}."
