"""
Voice: listen until the user stops talking, and speak answers.

Listening records from the default microphone and stops by itself after about
a second of silence. The recording is transcribed by Gemini (no extra model to
download). Speaking uses Microsoft's neural voices through edge-tts, which
sound natural in Turkish; if that is unavailable it falls back to the voices
built into Windows (pyttsx3).
"""
from __future__ import annotations

import asyncio
import io
import re
import tempfile
import threading
import time
import wave
from pathlib import Path

from jarvis import config

RATE = 16000


class Recorder:
    """record() blocks until speech ended; returns WAV bytes, or None if nobody spoke."""

    def __init__(self):
        self._cancel = threading.Event()
        self.level = 0.0     # 0..1, for the UI meter

    def cancel(self):
        self._cancel.set()

    def record(self, max_seconds=30, wait_for_speech=8.0, silence_end=1.1) -> bytes | None:
        import numpy as np
        import sounddevice as sd
        self._cancel.clear()
        frames: list = []
        block = int(RATE * 0.05)
        floor = None
        speaking = False
        last_voice = start = time.time()
        with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=block) as stream:
            while not self._cancel.is_set():
                data, _ = stream.read(block)
                frames.append(data.copy())
                rms = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
                now = time.time()
                if floor is None or (not speaking and now - start < 0.4):
                    floor = rms if floor is None else (floor * 0.8 + rms * 0.2)
                    continue
                threshold = max(floor * 3.0, 350.0)
                self.level = min(1.0, rms / 4000)
                if rms > threshold:
                    if not speaking:
                        frames = frames[-8:]           # keep 0.4 s before speech starts
                    speaking = True
                    last_voice = now
                if not speaking and now - start > wait_for_speech:
                    return None
                if speaking and now - last_voice > silence_end:
                    break
                if now - start > max_seconds:
                    break
        self.level = 0.0
        if self._cancel.is_set() or not speaking:
            return None
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"".join(f.tobytes() for f in frames))
        return buf.getvalue()


def speakable(text: str) -> str:
    """Strip markdown, links and paths so they are not read out character by character."""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[A-Za-z]:\\\S+", lambda m: Path(m.group(0)).name, t)
    t = re.sub(r"[*_#`>|]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _voice_for(text: str) -> str:
    chosen = config.get("voice")
    if chosen:
        return chosen
    if re.search(r"[çğışöüÇĞİŞÖÜ]", text) or config.get("language") == "tr" and not re.search(r"\b(the|and|you)\b", text):
        return "tr-TR-AhmetNeural"
    return "en-GB-RyanNeural"


def synthesize(text: str) -> Path | None:
    """MP3 of the text, or None (then speak_fallback is used)."""
    text = speakable(text)
    if not text:
        return None
    try:
        import edge_tts
    except ImportError:
        return None
    out = Path(tempfile.gettempdir()) / f"jarvis_tts_{int(time.time() * 1000)}.mp3"
    try:
        asyncio.run(edge_tts.Communicate(text, _voice_for(text), rate="+8%").save(str(out)))
        return out
    except Exception as e:
        print(f"[voice] edge-tts failed: {e}")
        return None


def speak_fallback(text: str) -> None:
    try:
        import pyttsx3
        eng = pyttsx3.init()
        for v in eng.getProperty("voices"):
            if "tr" in (v.id + v.name).lower() and _voice_for(text).startswith("tr"):
                eng.setProperty("voice", v.id)
                break
        eng.say(speakable(text))
        eng.runAndWait()
    except Exception as e:
        print(f"[voice] no speech output available: {e}")
