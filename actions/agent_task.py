"""
actions/agent_task.py — agent mode: give JARVIS a goal, it works until it is done.

WHAT WAS MISSING
    The live conversation calls one tool per request. "Find the cheapest RTX 5070
    in three shops, put the prices in a spreadsheet and open it" is five or six
    calls that depend on each other, and the voice model either stops after the
    first one or narrates the rest without doing it. The readme promised an agent
    mode for exactly this; there was no code behind it.

HOW IT WORKS
    A loop on its own thread, so the conversation stays free while it runs:

        plan the next step  ->  run it with an existing tool  ->  read the result
             ^                                                        |
             +---------- (optionally look at the screen) <-----------+

    Each step is one Gemini call through core/gemini.py, so it gets the model
    ladder, the timeouts and the cooldowns like every other side call. The model
    sees the goal, every tool the assistant has (actions, plugins and the safe
    inline ones), what each previous step did and returned, and — when it asks to
    look — a fresh screenshot. It answers with one JSON step. Nothing here knows
    about any particular tool: whatever is installed is what the agent can use.

    The user hears from it twice: one sentence when it starts (the tool result)
    and one when it ends (through request_say). In between, every step is written
    to the activity log and the step list is kept on the content panel, so the
    work is visible without being talked over.

WHAT KEEPS IT SAFE
    * It acts only through the same tools the conversation uses, so every gate
      those tools already have still applies: irreversible actions wait on the
      HUD's CONFIRM button (core/confirm.py), and file and settings changes go
      onto the undo stack.
    * Anything that speaks for the user to someone else (sending a message) is
      parked behind that same CONFIRM button before it runs.
    * A step budget, a time limit, a repeat detector and "stop" — said at any
      time — all end the run, and the user is told why.
    * One run at a time. A second goal while one is running is refused, not
      queued behind it.

main.py wires it up with bind(): the dispatcher it uses to run tools and the
catalogue of tools it may use. Unbound (tests, headless), it says so instead of
pretending.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

# ── Limits ────────────────────────────────────────────────────────────────────
DEFAULT_STEPS = 15
MAX_STEPS = 40
TIME_LIMIT_S = 15 * 60
# A result longer than this is cut before it goes back into the next prompt.
# Long enough for a search result or a file listing; short enough that forty
# steps of history still fit comfortably.
RESULT_CHARS = 1500
# Older steps are kept as one line each so a long run does not drown the planner.
FULL_HISTORY = 8
# The same tool with the same arguments this many times in a row is a loop.
REPEAT_LIMIT = 3
# Planner calls that come back with nothing usable before the run gives up.
PLANNER_FAILURES = 2
# How long a step that needs the user's CONFIRM may wait for it.
CONFIRM_WAIT_S = 90.0

# Tools the agent never calls: itself, ending the assistant, and the ones whose
# work is tied to the live conversation (vision is handled here with "look").
_EXCLUDED = {"agent_task", "shutdown_jarvis", "screen_process", "close_camera",
             "save_memory"}
# Tools that act on the user's behalf towards other people. These run only after
# the user presses CONFIRM on the HUD, whatever the plan says.
_ASK_FIRST = {"send_message"}

_PSEUDO = ("look", "wait", "finish", "ask_user")

_SYSTEM = """You are the planning core of a desktop assistant's agent mode. You
work towards the user's GOAL one step at a time, using only the TOOLS listed.

Each turn you receive the goal, the tools, and every step taken so far with its
result. Reply with exactly ONE JSON object and nothing else:

{"thought": "<one short sentence: what you learned and why this step>",
 "action": "<a tool name, or look | wait | finish | ask_user>",
 "args": {<arguments for the tool, matching its parameters>},
 "look_after": <true to get a screenshot after this step, else false>,
 "summary": "<only for finish / ask_user: what was done, or the question>"}

Special actions:
- look: take a screenshot now and see it next turn. Use it before clicking or
  typing into anything you have not seen, and to check that a step worked.
- wait: pause {"seconds": 1-10} for a page or app to load.
- finish: the goal is done, or cannot be done. "summary" is what you would tell
  the user: the concrete result (numbers, names, file paths), or what blocked it.
- ask_user: you need a decision or a fact only the user has. "summary" is the
  question. The run stops; the user answers in conversation.

Rules:
- Trust tool results over assumptions. If a step failed, try a different
  approach rather than repeating it; after two failed approaches, finish and
  say what blocked you.
- Prefer direct tools (open_app, file_controller, browser_control, web_search)
  over mouse and keyboard; use screen-level control only when nothing more
  direct exists, and look first.
- Never invent results. Only report what a tool actually returned.
- Do not ask the user for things you can find out with a tool.
- When a result says a confirmation is waiting on screen, the user decides; do
  not try to work around it.
- Write tool arguments in the form the tool expects (English action names,
  real paths). The summary should be in the language of the goal."""


@dataclass
class _Step:
    n: int
    thought: str
    action: str
    args: dict
    result: str = ""


@dataclass
class _Run:
    goal: str
    max_steps: int
    started: float = field(default_factory=time.monotonic)
    steps: list[_Step] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)
    state: str = "running"          # running | done | stopped | failed
    outcome: str = ""


_run_tool: Optional[Callable[[str, dict], str]] = None
_catalogue: Optional[Callable[[], list[dict]]] = None
_current: Optional[_Run] = None
_lock = threading.Lock()


def bind(run_tool: Callable[[str, dict], str],
         tools: Callable[[], list[dict]]) -> None:
    """Called once by main.py after the registries exist.

    run_tool(name, args) -> str runs one tool exactly as the conversation would.
    tools() -> list of function declarations the agent may choose from."""
    global _run_tool, _catalogue
    _run_tool, _catalogue = run_tool, tools


# ── Small helpers ─────────────────────────────────────────────────────────────

def _log(player, text: str) -> None:
    if player is not None:
        try:
            player.write_log(f"SYS: Agent — {text}")
        except Exception:
            pass
    print(f"[Agent] {text}")


def _say(player, instruction: str) -> None:
    if player is not None and hasattr(player, "request_say"):
        try:
            player.request_say(instruction)
        except Exception:
            pass


def _panel(player, run: _Run) -> None:
    """Keep the step list on the content panel while the run is going."""
    if player is None or not hasattr(player, "show_content"):
        return
    lines = [f"GOAL: {run.goal}", ""]
    for s in run.steps:
        args = json.dumps(s.args, ensure_ascii=False) if s.args else ""
        lines.append(f"{s.n}. {s.action} {args[:80]}")
        if s.result:
            lines.append(f"   → {_one_line(s.result, 140)}")
    if run.state != "running":
        lines += ["", f"[{run.state.upper()}] {run.outcome}"]
    try:
        player.show_content(f"AGENT — {run.goal[:36]}", "\n".join(lines))
    except Exception:
        pass


def _one_line(text: str, limit: int) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= limit else t[:limit - 1] + "…"


def _clip(text: str, limit: int = RESULT_CHARS) -> str:
    t = str(text or "").strip()
    return t if len(t) <= limit else t[:limit] + f"… [{len(t) - limit} more characters]"


def _tool_list(decls: list[dict]) -> str:
    """Compact catalogue: name, description, and the argument schema."""
    out = []
    for d in decls:
        name = d.get("name")
        if not name or name in _EXCLUDED:
            continue
        desc = _one_line(d.get("description", ""), 400)
        props = (d.get("parameters") or {}).get("properties") or {}
        params = {}
        for k, v in props.items():
            if not isinstance(v, dict):
                continue
            p = _one_line(v.get("description", ""), 160)
            if v.get("enum"):
                p += f" (one of: {', '.join(map(str, v['enum']))})"
            params[k] = p
        out.append(f"- {name}: {desc}\n  args: {json.dumps(params, ensure_ascii=False)}")
    return "\n".join(out)


def _history(run: _Run) -> str:
    if not run.steps:
        return "(no steps yet)"
    lines = []
    cut = len(run.steps) - FULL_HISTORY
    for i, s in enumerate(run.steps):
        args = json.dumps(s.args, ensure_ascii=False)
        if i < cut:
            lines.append(f"{s.n}. {s.action} {args[:100]} → {_one_line(s.result, 120)}")
        else:
            lines.append(f"{s.n}. thought: {s.thought}\n   action: {s.action} {args}\n"
                         f"   result: {s.result}")
    return "\n".join(lines)


def _screenshot():
    """(bytes, mime) of the main display, or None. Imported lazily: mss and
    Pillow are only needed once the agent actually looks."""
    try:
        from actions.screen_processor import _capture_screen
        return _capture_screen()
    except Exception as e:
        print(f"[Agent] screenshot failed: {e}")
        return None


def _plan(run: _Run, tools_text: str, image) -> Optional[dict]:
    from core import gemini

    elapsed = int(time.monotonic() - run.started)
    prompt = (
        f"GOAL: {run.goal}\n\n"
        f"TOOLS:\n{tools_text}\n- look / wait / finish / ask_user (see rules)\n\n"
        f"STEPS SO FAR:\n{_history(run)}\n\n"
        f"This is step {len(run.steps) + 1} of at most {run.max_steps}; "
        f"{elapsed}s of {TIME_LIMIT_S}s used."
        + ("\nA screenshot taken just now is attached." if image else "")
        + "\nReply with the JSON for the next step."
    )
    contents: list = [prompt]
    if image:
        try:
            from google.genai import types as gtypes
            contents.append(gtypes.Part.from_bytes(data=image[0], mime_type=image[1]))
        except Exception as e:
            print(f"[Agent] could not attach screenshot: {e}")

    try:
        from google.genai import types as gtypes
        config = gtypes.GenerateContentConfig(system_instruction=_SYSTEM)
    except Exception:
        config = {"system_instruction": _SYSTEM}

    step = gemini.as_json(contents, tier=gemini.SMART, config=config,
                          timeout_ms=45_000, default=None)
    if isinstance(step, list) and step and isinstance(step[0], dict):
        step = step[0]
    if not isinstance(step, dict) or not str(step.get("action", "")).strip():
        return None
    if not isinstance(step.get("args"), dict):
        step["args"] = {}
    return step


def _confirmed_run(name: str, args: dict, player) -> str:
    """Run a tool only after the user presses CONFIRM on the HUD."""
    from core import confirm

    if confirm.pending_title():
        return ("Another confirmation is already waiting on screen, so this step "
                "was not attempted.")
    box: dict = {}
    done = threading.Event()

    def _work() -> str:
        try:
            box["result"] = _run_tool(name, args)
        except Exception as e:
            box["result"] = f"Tool '{name}' failed: {e}"
        done.set()
        return _one_line(box["result"], 120)

    detail = _one_line(json.dumps(args, ensure_ascii=False), 200)
    reply = confirm.request(f"agent-{name}", f"Agent: {name}", detail, _work)
    if "[CONFIRMATION_PENDING]" not in reply:
        return reply                     # no interface — refused, nothing done
    _say(player, f"Tell the user in one short sentence, in their own language, "
                 f"that agent mode wants to run {name} ({detail}) and needs them "
                 f"to press CONFIRM on screen.")
    deadline = time.monotonic() + CONFIRM_WAIT_S + 2
    while time.monotonic() < deadline:
        if done.wait(0.5):
            return box.get("result", "Done.")
        if not confirm.pending_title():
            # The banner is gone and the work never ran: cancelled or expired.
            if done.wait(1.0):
                return box.get("result", "Done.")
            return "The user did not confirm this step, so it was NOT done."
    return "No confirmation arrived in time, so this step was NOT done."


def _wait_for_tool_confirmation(run: _Run) -> str:
    """A tool parked itself behind the HUD gate. Wait for the user's decision so
    the next step plans against what actually happened."""
    from core import confirm

    deadline = time.monotonic() + CONFIRM_WAIT_S + 2
    while time.monotonic() < deadline and not run.cancel.is_set():
        if not confirm.pending_title():
            return " The confirmation banner has closed (the user confirmed or cancelled)."
        time.sleep(0.5)
    return " No decision arrived on the confirmation banner."


# ── The loop ──────────────────────────────────────────────────────────────────

def _execute(run: _Run, player) -> None:
    tools_text = _tool_list(_catalogue() if _catalogue else [])
    image = None
    planner_misses = 0

    while True:
        if run.cancel.is_set():
            run.state, run.outcome = "stopped", "Stopped on request."
            break
        if len(run.steps) >= run.max_steps:
            run.state = "failed"
            run.outcome = f"Ran out of steps ({run.max_steps}) before finishing."
            break
        if time.monotonic() - run.started > TIME_LIMIT_S:
            run.state = "failed"
            run.outcome = f"Ran out of time ({TIME_LIMIT_S // 60} minutes)."
            break

        step = _plan(run, tools_text, image)
        image = None
        if run.cancel.is_set():
            continue
        if step is None:
            planner_misses += 1
            if planner_misses >= PLANNER_FAILURES:
                run.state = "failed"
                run.outcome = "The planning model did not answer, so I stopped."
                break
            time.sleep(2)
            continue
        planner_misses = 0

        action = str(step.get("action", "")).strip()
        args = step["args"]
        s = _Step(n=len(run.steps) + 1, thought=str(step.get("thought", "")).strip(),
                  action=action, args=args)
        run.steps.append(s)
        _log(player, f"step {s.n}: {action} {_one_line(json.dumps(args, ensure_ascii=False), 90)}")

        if action == "finish":
            s.result = "finished"
            run.state = "done"
            run.outcome = str(step.get("summary") or s.thought or "Done.").strip()
            break
        if action == "ask_user":
            s.result = "asked the user"
            run.state = "stopped"
            run.outcome = ("Needs your input: "
                           + str(step.get("summary") or s.thought).strip())
            break
        if action == "look":
            image = _screenshot()
            s.result = "screenshot attached to the next step" if image else \
                "screenshot failed — no image available"
            _panel(player, run)
            continue
        if action == "wait":
            try:
                secs = min(10.0, max(0.5, float(args.get("seconds", 2))))
            except (TypeError, ValueError):
                secs = 2.0
            run.cancel.wait(secs)
            s.result = f"waited {secs:g}s"
            _panel(player, run)
            continue

        # A real tool.
        recent = run.steps[-REPEAT_LIMIT - 1:-1]     # the ones before this step
        if (len(recent) == REPEAT_LIMIT
                and all(r.action == action and r.args == args for r in recent)):
            s.result = "not run — identical to the previous attempts"
            run.state = "failed"
            run.outcome = (f"I was repeating the same step ({action}) without "
                           f"progress, so I stopped.")
            break

        if action in _ASK_FIRST:
            result = _confirmed_run(action, args, player)
        else:
            try:
                result = _run_tool(action, args)
            except Exception as e:
                result = f"Tool '{action}' failed: {e}"
            if "[CONFIRMATION_PENDING]" in str(result):
                _say(player, "Tell the user in one short sentence, in their own "
                             "language, that agent mode needs them to confirm "
                             "the action shown on screen.")
                result = str(result) + _wait_for_tool_confirmation(run)
        s.result = _clip(result)
        _panel(player, run)

        if step.get("look_after"):
            time.sleep(0.8)              # let the window settle before looking
            image = _screenshot()


def _worker(run: _Run, player) -> None:
    global _current
    try:
        _execute(run, player)
    except Exception as e:                                   # noqa: BLE001
        run.state, run.outcome = "failed", f"Agent mode crashed: {e}"
        import traceback
        traceback.print_exc()

    _log(player, f"{run.state} after {len(run.steps)} steps — {_one_line(run.outcome, 160)}")
    _panel(player, run)
    if run.state != "stopped" or run.outcome.startswith("Needs your input"):
        _say(player,
             f"[AGENT_{run.state.upper()}] Agent mode has ended for the goal "
             f"'{run.goal}'. Report this to the user in one or two sentences, in "
             f"their own language, using only these facts: {run.outcome}")
    with _lock:
        if _current is run:
            _current = None


# ── Tool entry point ──────────────────────────────────────────────────────────

def agent_task(parameters: dict, player=None) -> str:
    global _current
    action = str(parameters.get("action") or "start").strip().lower()

    if action == "stop":
        with _lock:
            run = _current
        if run is None:
            return "Agent mode is not running."
        run.cancel.set()
        return "Agent mode is stopping. Tell the user in one short sentence."

    if action == "status":
        with _lock:
            run = _current
        if run is None:
            return "Agent mode is not running."
        last = run.steps[-1] if run.steps else None
        return (f"Agent mode is working on '{run.goal}': step {len(run.steps)} of "
                f"at most {run.max_steps}"
                + (f", last did {last.action} → {_one_line(last.result, 160)}" if last else "")
                + ".")

    goal = str(parameters.get("goal") or "").strip()
    if not goal:
        return "Agent mode needs a goal: what should be achieved?"
    if _run_tool is None:
        return "Agent mode is not available in this build (the tool dispatcher is not connected)."
    try:
        max_steps = int(parameters.get("max_steps") or DEFAULT_STEPS)
    except (TypeError, ValueError):
        max_steps = DEFAULT_STEPS
    max_steps = max(1, min(MAX_STEPS, max_steps))

    with _lock:
        if _current is not None:
            return (f"Agent mode is already working on '{_current.goal}'. Tell the "
                    f"user; they can say stop to cancel it first.")
        run = _Run(goal=goal, max_steps=max_steps)
        _current = run

    _log(player, f"started — {goal}")
    _panel(player, run)
    threading.Thread(target=_worker, args=(run, player), daemon=True,
                     name="agent-task").start()
    return ("[AGENT_STARTED] Agent mode is now working on this in the background "
            "and will report back when it is done. Say ONE short sentence in the "
            "user's language that you are on it. Do not describe steps, do not "
            "wait, and do not call agent_task again for this goal.")


TOOL = {
    "name": "agent_task",
    "description": (
        "Agent mode: hands a GOAL to an autonomous loop that plans and carries "
        "out as many steps as it takes, using every other tool (apps, browser, "
        "files, mouse and keyboard, web search, messages) and looking at the "
        "screen as it goes. Use it whenever a request needs several dependent "
        "steps or figuring out along the way, e.g. 'find the cheapest flight "
        "and put it in a note', 'clean up my downloads and tell me what you "
        "removed', 'open Spotify and play my liked songs', 'research X and save "
        "a summary on the desktop'. For a single direct action call that tool "
        "yourself instead. It runs in the background and reports back on its "
        "own. action='stop' cancels it (when the user says stop, cancel, enough "
        "during a task); action='status' says how far it is."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "start (default) | stop | status",
                "enum": ["start", "stop", "status"],
            },
            "goal": {
                "type": "STRING",
                "description": ("For start: the complete goal in the user's own "
                                "words, with every detail they gave (names, "
                                "places, limits, where to save the result)."),
            },
            "max_steps": {
                "type": "INTEGER",
                "description": f"Optional step budget, default {DEFAULT_STEPS}, at most {MAX_STEPS}.",
            },
        },
        "required": [],
    },
    "handler": agent_task,
}
