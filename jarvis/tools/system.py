"""
The PC itself: apps, files, commands, and the screen (look, click, type).

Screen control is for desktop apps the other tools cannot reach. Web pages go
through the browser tools, which are far more reliable than clicking pixels.
"""
from __future__ import annotations

import io
import os
import platform
import shutil
import subprocess
import sys
import time

from jarvis.tools import Result, tool
from jarvis.tools.files import _shortcut

WIN = sys.platform == "win32"
_BROWSERS = {"opera", "opera gx", "chrome", "google chrome", "edge", "microsoft edge", "firefox",
             "brave", "safari", "internet explorer", "browser", "tarayıcı"}
_APPS = {  # spoken name -> Windows command
    "excel": "excel", "word": "winword", "powerpoint": "powerpnt", "outlook": "outlook",
    "notepad": "notepad", "not defteri": "notepad", "hesap makinesi": "calc", "calculator": "calc",
    "paint": "mspaint", "explorer": "explorer", "dosya gezgini": "explorer", "file explorer": "explorer",
    "task manager": "taskmgr", "görev yöneticisi": "taskmgr", "cmd": "cmd", "powershell": "powershell",
    "terminal": "wt", "settings": "ms-settings:", "ayarlar": "ms-settings:", "vscode": "code",
    "visual studio code": "code", "spotify": "spotify:", "whatsapp": "whatsapp:", "discord": "discord:",
    "steam": "steam:", "camera": "microsoft.windows.camera:", "kamera": "microsoft.windows.camera:",
}

# Screenshots are shrunk before the model sees them; clicks map back with this.
_scale = {"x": 1.0, "y": 1.0}


@tool("open_app", """Open a desktop application (Excel, Word, Spotify, WhatsApp, Notepad…).
Not for websites or browsers: use browser_open.""",
      {"name": {"type": "string"}}, ["name"], label=lambda a: f"Açılıyor: {a.get('name')}")
def open_app(name: str):
    key = name.lower().strip()
    if key in _BROWSERS:
        return "Browsers are opened with browser_open (JARVIS uses its own Opera window)."
    if not WIN:
        cmd = ["open", "-a", name] if sys.platform == "darwin" else [shutil.which(key) or key]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Opened {name}."
    target = _APPS.get(key, key)
    if target.endswith(":") or shutil.which(target):
        os.startfile(target) if target.endswith(":") else subprocess.Popen(
            f'start "" {target}', shell=True)
        time.sleep(1.5)
        return f"Opened {name}."
    # Fall back to the Start menu: what a person would do.
    import pyautogui
    pyautogui.press("win")
    time.sleep(0.8)
    pyautogui.write(name, interval=0.03)
    time.sleep(1.2)
    pyautogui.press("enter")
    time.sleep(2)
    return f"Searched the Start menu for {name!r} and pressed Enter; use screen_look to check it opened."


@tool("open_file", "Open a file or folder with its normal program (an .xlsx opens in Excel).",
      {"path": {"type": "string"}}, ["path"], label=lambda a: f"Açılıyor: {a.get('path')}")
def open_file(path: str):
    p = _shortcut(path)
    if not p.exists():
        return f"{p} does not exist."
    if p.suffix.lower() in (".html", ".htm", ".url"):
        from jarvis.tools.browser import BROWSER
        return BROWSER.open(p.as_uri())
    if WIN:
        os.startfile(str(p))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(p)])
    return f"Opened {p}."


@tool("run_command", """Run a PowerShell command (cmd/bash outside Windows) and return its output.
For things no other tool does: system settings, installed programs, network info…""",
      {"command": {"type": "string"}}, ["command"],
      confirm=lambda a: f"Bilgisayarında şu komut çalıştırılacak:\n{a.get('command')}",
      label=lambda a: f"Komut: {a.get('command', '')[:60]}")
def run_command(command: str):
    if WIN:
        args = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]
    else:
        args = ["bash", "-lc", command]
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120, creationflags=0x08000000 if WIN else 0)
    out = (r.stdout or "") + (("\nERR: " + r.stderr) if r.stderr.strip() else "")
    return f"exit {r.returncode}\n{out.strip() or '(no output)'}"


@tool("system_info", "Battery, CPU, memory, disk, time, and the OS.", label=lambda a: "Bilgisayar kontrol ediliyor")
def system_info():
    import psutil
    from datetime import datetime
    lines = [f"{platform.system()} {platform.release()}, {datetime.now():%Y-%m-%d %H:%M}",
             f"CPU {psutil.cpu_percent(interval=0.5)}%, RAM {psutil.virtual_memory().percent}%"]
    b = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if b:
        lines.append(f"Battery {b.percent:.0f}% {'charging' if b.power_plugged else 'on battery'}")
    for part in psutil.disk_partitions()[:4]:
        try:
            u = psutil.disk_usage(part.mountpoint)
            lines.append(f"Disk {part.mountpoint}: {u.free / 1e9:.0f} GB free of {u.total / 1e9:.0f} GB")
        except Exception:
            pass
    return "\n".join(lines)


@tool("screen_look", """Take a screenshot of the whole screen and look at it. Use it for desktop
apps and to check what the user sees. Coordinates for mouse_click are in this image's pixels.""",
      label=lambda a: "Ekrana bakılıyor")
def screen_look():
    import mss
    from PIL import Image
    with mss.mss() as s:
        mon = s.monitors[1]
        shot = s.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    w = 1280
    _scale["x"] = img.width / w
    _scale["y"] = _scale["x"]
    img = img.resize((w, int(img.height / _scale["x"])))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return Result(f"Screenshot attached ({img.width}x{img.height}).", image=buf.getvalue(), mime="image/jpeg")


@tool("mouse_click", "Click on the screen at x,y from the last screen_look image.",
      {"x": {"type": "integer"}, "y": {"type": "integer"},
       "button": {"type": "string", "enum": ["left", "right"]}, "double": {"type": "boolean"}},
      ["x", "y"], label=lambda a: f"Ekranda tıklanıyor ({a.get('x')},{a.get('y')})")
def mouse_click(x: int, y: int, button: str = "left", double: bool = False):
    import pyautogui
    rx, ry = int(x * _scale["x"]), int(y * _scale["y"])
    pyautogui.click(rx, ry, clicks=2 if double else 1, button=button)
    time.sleep(0.5)
    return f"Clicked at {x},{y}. Call screen_look to see the result."


@tool("keyboard_type", "Type text into whatever has focus on the PC (any app).",
      {"text": {"type": "string"}}, ["text"], label=lambda a: f"Yazılıyor: \"{a.get('text', '')[:40]}\"")
def keyboard_type(text: str):
    import pyautogui
    try:
        import pyperclip      # pasting handles ç, ğ, ş… which typing does not
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
    except Exception:
        pyautogui.write(text, interval=0.02)
    return "Typed."


@tool("key_press", "Press a key or shortcut on the PC, e.g. enter, ctrl+s, alt+tab, volumeup, playpause.",
      {"keys": {"type": "string"}}, ["keys"], label=lambda a: f"Tuş: {a.get('keys')}")
def key_press(keys: str):
    import pyautogui
    parts = [k.strip().lower() for k in keys.replace(" ", "").split("+") if k.strip()]
    pyautogui.hotkey(*parts) if len(parts) > 1 else pyautogui.press(parts[0])
    return f"Pressed {keys}."
