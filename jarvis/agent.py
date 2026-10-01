"""
The agent: the conversation with Gemini, and the loop that runs its tools.

One conversation lives across messages (so "now put that in Excel" works),
until the user starts a new chat. Every turn and every tool runs on ONE worker
thread, because the browser's Playwright connection must stay on the thread
that opened it.

Events go to the UI through `on_event(kind, data)`:
    "thinking"  {}                      a model call started
    "tool"      {"name", "label"}       a tool is about to run
    "tool_done" {"name", "ok", "short"} it finished
    "say"       {"text"}                text from JARVIS (final answer, or a remark mid-task)
    "done"      {"text", "steps"}       the turn is over
    "error"     {"text"}
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from typing import Callable

from jarvis import config, llm, tools
from jarvis.prompt import system_prompt
from jarvis.tools import memory

KEEP_IMAGES = 2          # older screenshots are dropped from the conversation
OLD_RESULT_CHARS = 1500  # older tool results are shortened to this


class Agent:
    def __init__(self, on_event: Callable[[str, dict], None],
                 approve: Callable[[str, str], bool] | None = None):
        self.on_event = on_event
        self.approve = approve
        self.registry = tools.load_all()
        self.history: list = []
        self._stop = threading.Event()
        self._jobs: queue.Queue = queue.Queue()
        self.busy = False
        threading.Thread(target=self._worker, name="jarvis-worker", daemon=True).start()

    # ── public ────────────────────────────────────────────────────────────
    def send(self, text: str) -> None:
        self._jobs.put(("turn", text))

    def stop(self) -> None:
        self._stop.set()

    def new_chat(self) -> None:
        self._jobs.put(("reset", None))

    def call_tool(self, name: str, args: dict) -> None:
        """Run a tool on the worker thread outside a turn (UI shortcuts)."""
        self._jobs.put(("tool", (name, args)))

    # ── worker ────────────────────────────────────────────────────────────
    def _worker(self):
        while True:
            kind, payload = self._jobs.get()
            try:
                if kind == "reset":
                    self.history = []
                elif kind == "tool":
                    tools.call(*payload)
                else:
                    self.busy = True
                    self._stop.clear()
                    self._turn(payload)
            except llm.NoKey:
                self.on_event("error", {"text": "NO_KEY"})
            except Exception as e:
                traceback.print_exc()
                self.on_event("error", {"text": f"{type(e).__name__}: {e}"})
                self.on_event("done", {"text": "", "steps": 0})
            finally:
                self.busy = False

    def _turn(self, text: str):
        from google.genai import types
        self.history.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))
        decls = [t.declaration() for t in self.registry.values()]
        max_steps = int(config.get("max_steps") or 40)
        steps = 0
        recent: list[str] = []
        final = ""
        started = time.time()
        while True:
            if self._stop.is_set():
                final = "Durdurdum."
                self._note("The user pressed stop. Stopped here.")
                break
            if steps >= max_steps:
                final = f"{max_steps} adımda bitiremedim; buraya kadar yaptıklarım yukarıda."
                break
            self._compact()
            self.on_event("thinking", {})
            resp = llm.generate(self.history, system=system_prompt(memory.facts()), tools=decls)
            cand = (resp.candidates or [None])[0]
            content = getattr(cand, "content", None)
            if content is None or not content.parts:
                reason = getattr(cand, "finish_reason", "")
                if steps == 0 or not final:
                    final = "Bir cevap üretemedim, tekrar dener misin?"
                print(f"[agent] empty response, finish_reason={reason}")
                break
            self.history.append(content)
            calls = [p.function_call for p in content.parts if p.function_call]
            said = "".join(p.text for p in content.parts
                           if p.text and not getattr(p, "thought", False)).strip()
            if not calls:
                final = said
                break
            if said:
                self.on_event("say", {"text": said, "interim": True})

            replies, images = [], []
            for call in calls:
                if self._stop.is_set():
                    replies.append(types.Part.from_function_response(
                        name=call.name, response={"result": "Skipped: the user pressed stop."}))
                    continue
                steps += 1
                args = dict(call.args or {})
                self.on_event("tool", {"name": call.name, "label": tools.describe(call.name, args)})
                result = tools.call(call.name, args, approve=self.approve)
                ok = not result.text.startswith(("ERROR", "The user did NOT"))
                self.on_event("tool_done", {"name": call.name, "ok": ok,
                                            "short": result.text.splitlines()[0][:160] if result.text else ""})
                sig = f"{call.name}:{sorted(args.items())!s}:{result.text[:200]}"
                recent.append(sig)
                if recent[-3:].count(sig) == 3:
                    result.text += ("\n\nNOTE: this exact call has now given the same result three "
                                    "times. Do something different.")
                replies.append(types.Part.from_function_response(
                    name=call.name, response={"result": result.text}))
                if result.image:
                    images.append(types.Part.from_bytes(data=result.image, mime_type=result.mime))
            self.history.append(types.Content(role="user", parts=replies + images))
        if final:
            self.on_event("say", {"text": final})
        print(f"[agent] turn done: {steps} tool calls, {time.time() - started:.1f}s")
        self.on_event("done", {"text": final, "steps": steps})

    def _note(self, text: str):
        """Leave the conversation in a state the model accepts (it must not end on a call)."""
        from google.genai import types
        last = self.history[-1] if self.history else None
        if last is not None and last.role == "model" and any(p.function_call for p in last.parts):
            self.history.append(types.Content(role="user", parts=[
                types.Part.from_function_response(name=p.function_call.name, response={"result": text})
                for p in last.parts if p.function_call]))
        self.history.append(types.Content(role="model", parts=[types.Part.from_text(text=text)]))

    def _compact(self):
        """Old screenshots and long page dumps are the bulk of the tokens; trim them."""
        seen_images = 0
        for i in range(len(self.history) - 1, -1, -1):
            c = self.history[i]
            if c.role != "user":
                continue
            keep = []
            for p in c.parts:
                if p.inline_data is not None:
                    seen_images += 1
                    if seen_images > KEEP_IMAGES:
                        continue
                keep.append(p)
            if len(keep) != len(c.parts) and keep:
                c.parts = keep
            # Tool results older than the last few exchanges get shortened.
            if i < len(self.history) - 6:
                for p in c.parts:
                    fr = p.function_response
                    if fr is not None and isinstance(fr.response, dict):
                        r = fr.response.get("result")
                        if isinstance(r, str) and len(r) > OLD_RESULT_CHARS:
                            fr.response["result"] = r[:OLD_RESULT_CHARS] + "\n…[older output shortened]"
        # Very long chats: drop the oldest whole exchanges (starting at a real user message).
        while len(self.history) > 120:
            cut = next((j for j in range(2, len(self.history))
                        if self.history[j].role == "user"
                        and any(p.text for p in self.history[j].parts)), None)
            if cut is None:
                break
            del self.history[:cut]
