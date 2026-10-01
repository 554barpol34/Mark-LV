"""
Gemini calls. One client, a ladder of models (the next is tried when one is
rate-limited or down), and a timeout on everything.
"""
from __future__ import annotations

import time

from jarvis import config

_client = None
_client_key = None


class NoKey(RuntimeError):
    pass


def client():
    global _client, _client_key
    key = config.api_key()
    if not key:
        raise NoKey("No Gemini API key yet.")
    if _client is None or key != _client_key:
        from google import genai
        _client = genai.Client(api_key=key, http_options={"timeout": 90_000})
        _client_key = key
    return _client


def _retryable(e: Exception) -> bool:
    s = str(e)
    return any(code in s for code in ("429", "500", "502", "503", "504", "RESOURCE_EXHAUSTED",
                                      "UNAVAILABLE", "DEADLINE", "overloaded", "timed out"))


def generate(contents, *, system: str = "", tools: list[dict] | None = None, models=None,
             thinking: int | None = 512):
    from google.genai import types
    cfg = dict(system_instruction=system or None, temperature=0.4)
    if tools:
        cfg["tools"] = [types.Tool(function_declarations=tools)]
        cfg["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
    last = None
    for model in models or config.get("models"):
        c = dict(cfg)
        if thinking is not None and ("2.5" in model or "latest" in model):
            c["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking)
        for attempt in range(2):
            try:
                return client().models.generate_content(
                    model=model, contents=contents, config=types.GenerateContentConfig(**c))
            except NoKey:
                raise
            except Exception as e:
                last = e
                print(f"[llm] {model}: {str(e)[:200]}")
                if not _retryable(e):
                    break
                if attempt == 0 and "429" not in str(e):
                    time.sleep(1.5)
                    continue
                break
    raise RuntimeError(f"Gemini did not answer: {last}")


def transcribe(wav: bytes, language: str = "tr") -> str:
    from google.genai import types
    resp = generate(
        [types.Content(role="user", parts=[
            types.Part.from_bytes(data=wav, mime_type="audio/wav"),
            types.Part.from_text(text=(
                f"Transcribe this speech exactly (it is most likely in language '{language}'). "
                "Output only the words. If there is no speech, output nothing."))])],
        models=["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-flash-latest"], thinking=0)
    return (resp.text or "").strip()
