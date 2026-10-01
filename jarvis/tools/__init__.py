"""
The tool registry.

A tool is a plain function with a @tool decorator that says what it does and
which arguments it takes (JSON schema). The model sees the description; the
agent calls the function. Every tool module is imported here so the
decorators run.

Tools return a string, or a Result when they also have an image to show the
model (a screenshot).
"""
from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from typing import Callable

MAX_RESULT_CHARS = 15000


@dataclass
class Result:
    text: str
    image: bytes | None = None          # PNG/JPEG the model should look at
    mime: str = "image/png"


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    required: list[str]
    fn: Callable
    # None = never ask. Otherwise a function of the arguments that returns a
    # sentence describing the risk when the user must approve first.
    confirm: Callable[[dict], str | None] | None = None
    label: Callable[[dict], str] | None = None   # short human line for the UI

    def declaration(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters_json_schema": {
                "type": "object",
                "properties": self.params,
                "required": self.required,
            },
        }


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, params: dict | None = None,
         required: list[str] | None = None, confirm=None, label=None):
    def wrap(fn):
        REGISTRY[name] = Tool(name, description.strip(), params or {}, required or [],
                              fn, confirm, label)
        return fn
    return wrap


def describe(name: str, args: dict) -> str:
    t = REGISTRY.get(name)
    if t and t.label:
        try:
            return t.label(args)
        except Exception:
            pass
    shown = ", ".join(f"{k}={str(v)[:40]}" for k, v in args.items())
    return f"{name}({shown})"


def call(name: str, args: dict, approve: Callable[[str, str], bool] | None = None) -> Result:
    t = REGISTRY.get(name)
    if t is None:
        return Result(f"ERROR: there is no tool called {name}.")
    if t.confirm and approve:
        reason = t.confirm(args)
        if reason and not approve(describe(name, args), reason):
            return Result("The user did NOT approve this action. Do not retry it; "
                          "ask what they want instead.")
    try:
        out = t.fn(**args)
    except TypeError as e:
        return Result(f"ERROR: bad arguments for {name}: {e}")
    except Exception as e:
        traceback.print_exc()
        return Result(f"ERROR: {type(e).__name__}: {e}")
    if not isinstance(out, Result):
        if not isinstance(out, str):
            out = json.dumps(out, ensure_ascii=False, default=str)
        out = Result(out)
    if len(out.text) > MAX_RESULT_CHARS:
        out.text = out.text[:MAX_RESULT_CHARS] + f"\n…[cut, {len(out.text)} chars total]"
    return out


def load_all() -> dict[str, Tool]:
    from jarvis.tools import browser, web, files, excel, system, media, memory  # noqa: F401
    return REGISTRY
