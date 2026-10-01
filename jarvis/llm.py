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


_dead: set[str] = set()        # models that answered 404 this session
_listed: list[str] | None = None


def _available() -> list[str]:
    """Flash models this key can use, newest first, from Google's own list.

    Google retires model names (2.5-flash-lite went 404 for new keys), so a
    hardcoded list breaks. The configured names are tried first if they exist.
    """
    global _listed
    if _listed is None:
        _listed = []
        try:
            for m in client().models.list():
                name = (m.name or "").removeprefix("models/")
                actions = getattr(m, "supported_actions", None) or []
                if actions and "generateContent" not in actions:
                    continue
                if "flash" not in name or any(x in name for x in (
                        "tts", "image", "live", "audio", "embedding", "exp", "thinking", "8b")):
                    continue
                _listed.append(name)
        except NoKey:
            raise
        except Exception as e:
            print(f"[llm] could not list models: {e}")

        def rank(n):
            import re
            v = re.search(r"(\d+(?:\.\d+)?)", n)
            return (-(float(v.group(1)) if v else 0), "lite" in n, "preview" in n, len(n))
        _listed.sort(key=rank)
        print(f"[llm] available: {_listed[:8]}")
    return _listed


def ladder(lite: bool = False) -> list[str]:
    found = _available()
    wanted = list(config.get("models") or [])
    if found:
        wanted = [m for m in wanted if m in found]
        best = [m for m in found if ("lite" in m) == lite] + [m for m in found if ("lite" in m) != lite]
        wanted += [m for m in best if m not in wanted][:4]
    else:
        wanted += ["gemini-flash-latest", "gemini-flash-lite-latest"]
    return [m for m in dict.fromkeys(wanted) if m not in _dead] or ["gemini-flash-latest"]


def _retryable(e: Exception) -> bool:
    s = str(e)
    return any(code in s for code in ("429", "500", "502", "503", "504", "RESOURCE_EXHAUSTED",
                                      "UNAVAILABLE", "DEADLINE", "overloaded", "timed out"))


def generate(contents, *, system: str = "", tools: list[dict] | None = None, models=None,
             thinking: int | None = 512, lite: bool = False, json: bool = False):
    from google.genai import types
    cfg = dict(system_instruction=system or None, temperature=0.4)
    if json:
        cfg["response_mime_type"] = "application/json"
    if tools:
        cfg["tools"] = [types.Tool(function_declarations=tools)]
        cfg["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
    last = None
    for model in models or ladder(lite):
        c = dict(cfg)
        if thinking is not None and "2.5" in model:
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
                if "404" in str(e) or "NOT_FOUND" in str(e):
                    _dead.add(model)
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
        lite=True, thinking=0)
    return (resp.text or "").strip()
