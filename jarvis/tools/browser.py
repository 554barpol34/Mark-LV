"""
The browser: one Opera window that JARVIS drives, and nothing else.

How it stays reliable
  * ONE window. JARVIS starts Opera once with its own profile (~/.jarvis/
    browser-profile) and a DevTools port, then attaches to it. Logins you make
    in that window are kept. It never opens URLs through Windows, so no other
    browser and no second window ever appears.
  * No blank tabs. The tab Opera opens with is reused; extra about:blank tabs
    are closed.
  * Clicking by number. After every page change JARVIS lists what is clickable
    on screen, each with a number ("[12] link: Emlak"). The model clicks the
    number, so it never guesses CSS selectors that do not exist. Cookie banners
    inside iframes are listed too.
  * Same thread. Playwright's sync API must stay on the thread that started
    it; the agent runs every tool on one worker thread.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

from jarvis import config
from jarvis.tools import Result, tool

PROFILE_DIR = config.DATA_DIR / "browser-profile"
SNAPSHOT_LIMIT = 70

_SNAPSHOT_JS = r"""
(start) => {
  const SEL = 'a[href],button,input:not([type=hidden]),textarea,select,summary,' +
    '[role=button],[role=link],[role=tab],[role=menuitem],[role=option],[role=checkbox],' +
    '[role=radio],[role=switch],[role=combobox],[role=searchbox],[role=textbox],' +
    '[contenteditable=""],[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"])';
  document.querySelectorAll('[data-jarvis-id]').forEach(e => e.removeAttribute('data-jarvis-id'));
  const W = innerWidth, H = innerHeight, out = [];
  let n = start;
  const seen = new Set();
  for (const el of document.querySelectorAll(SEL)) {
    const r = el.getBoundingClientRect();
    if (r.width < 3 || r.height < 3) continue;
    if (r.bottom < 0 || r.top > H || r.right < 0 || r.left > W) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || +st.opacity === 0) continue;
    // Is it actually on top at its centre? Skips things hidden under overlays.
    const cx = Math.min(Math.max(r.left + r.width / 2, 0), W - 1);
    const cy = Math.min(Math.max(r.top + r.height / 2, 0), H - 1);
    const top = document.elementFromPoint(cx, cy);
    if (top && top !== el && !el.contains(top) && !top.contains(el)) continue;
    // A link wrapping a button etc: keep the outer one only.
    let skip = false;
    for (let p = el.parentElement; p; p = p.parentElement) { if (seen.has(p)) { skip = true; break; } }
    if (skip) continue;
    seen.add(el);
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') ||
      (tag === 'a' ? 'link' : tag === 'input' ? (el.type || 'text') + ' input' :
       tag === 'textarea' ? 'text box' : tag === 'select' ? 'dropdown' : tag === 'button' ? 'button' : tag);
    let name = el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder ||
               el.title || el.getAttribute('alt') || '';
    if (!name) { const img = el.querySelector('img[alt]'); if (img) name = img.alt; }
    name = name.replace(/\s+/g, ' ').trim().slice(0, 80);
    if (!name && tag === 'a') name = (el.getAttribute('href') || '').slice(0, 60);
    let extra = '';
    if (tag === 'input' || tag === 'textarea') {
      if (el.placeholder && name !== el.placeholder) extra += ` placeholder="${el.placeholder.slice(0, 40)}"`;
      if (el.value && name !== el.value && el.type !== 'password') extra += ` value="${String(el.value).slice(0, 40)}"`;
      if (el.type === 'checkbox' || el.type === 'radio') extra += el.checked ? ' [checked]' : '';
    }
    if (tag === 'select' && el.selectedOptions.length) extra += ` selected="${el.selectedOptions[0].text.slice(0, 40)}"`;
    el.setAttribute('data-jarvis-id', String(n));
    out.push({id: n, role, name, extra});
    n++;
    if (out.length >= 400) break;
  }
  const below = document.documentElement.scrollHeight - (scrollY + H);
  return {items: out, below: Math.max(0, Math.round(below / H * 10) / 10)};
}
"""


def _find_opera() -> str | None:
    want = (config.get("browser") or "opera").lower()
    custom = config.get("browser_path") or os.environ.get("JARVIS_BROWSER_EXE")
    if custom:
        return custom if Path(custom).exists() else None
    if Path(want).exists():
        return want
    names = ["Opera GX", "Opera"] if want == "operagx" else ["Opera", "Opera GX"]
    if sys.platform == "win32":
        roots = [os.environ.get("LOCALAPPDATA", ""), os.environ.get("PROGRAMFILES", ""),
                 os.environ.get("PROGRAMFILES(X86)", "")]
        for name in names:
            for root in roots:
                for sub in (Path(root) / "Programs" / name, Path(root) / name):
                    if (sub / "opera.exe").exists():
                        return str(sub / "opera.exe")
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\opera.exe") as k:
                        exe = winreg.QueryValue(k, None).strip('"')
                        if Path(exe).exists():
                            return exe
                except OSError:
                    pass
        except ImportError:
            pass
    if sys.platform == "darwin":
        for name in names:
            p = Path(f"/Applications/{name}.app/Contents/MacOS/{name}")
            if p.exists():
                return str(p)
    return shutil.which("opera")


class Browser:
    def __init__(self):
        self._pw = None
        self._browser = None
        self.page = None
        self._refs: dict[int, object] = {}     # number -> frame holding it

    # ── connection ────────────────────────────────────────────────────────
    def _alive(self) -> bool:
        try:
            return bool(self.page) and not self.page.is_closed() and self._browser.is_connected()
        except Exception:
            return False

    def _port_file_port(self) -> int | None:
        f = PROFILE_DIR / "DevToolsActivePort"
        try:
            port = int(f.read_text().split()[0])
        except Exception:
            return None
        import socket
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return port
        except OSError:
            return None

    def _connect(self):
        if self._alive():
            return self.page
        # The window may still be open with only our page gone: adopt a tab.
        try:
            if self._browser and self._browser.is_connected():
                pages = [p for c in self._browser.contexts for p in c.pages if not p.is_closed()]
                if pages:
                    self.page = pages[-1]
                    return self.page
        except Exception:
            pass

        from playwright.sync_api import sync_playwright
        if self._pw is None:
            self._pw = sync_playwright().start()

        port = self._port_file_port()          # our Opera window is already open
        if port is not None:
            try:
                self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            except Exception:
                # The window was closing as we connected; start a fresh one.
                time.sleep(1.5)
                port = None
        if port is None:
            self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self._launch()}")
        ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        ctx.on("page", self._on_new_page)
        pages = [p for p in ctx.pages if not p.url.startswith(("devtools:", "chrome-extension:"))]
        self.page = pages[0] if pages else ctx.new_page()
        for extra in pages[1:]:                 # no stray about:blank tabs
            if extra.url in ("about:blank", "") or "startpage" in extra.url:
                try:
                    extra.close()
                except Exception:
                    pass
        try:
            self.page.bring_to_front()
        except Exception:
            pass
        return self.page

    def _launch(self) -> int:
        exe = _find_opera()
        if not exe:
            raise RuntimeError(
                "Opera was not found. Install Opera, or put its full path in "
                f"{config.CONFIG_FILE} as \"browser_path\".")
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            (PROFILE_DIR / "DevToolsActivePort").unlink()
        except FileNotFoundError:
            pass
        args = [exe, f"--user-data-dir={PROFILE_DIR}", "--remote-debugging-port=0",
                "--no-first-run", "--no-default-browser-check",
                "--autoplay-policy=no-user-gesture-required"]
        args += os.environ.get("JARVIS_BROWSER_ARGS", "").split()
        flags = 0x00000008 if sys.platform == "win32" else 0   # DETACHED_PROCESS
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags)
        deadline = time.time() + 25
        while time.time() < deadline:
            time.sleep(0.3)
            port = self._port_file_port()
            if port:
                return port
        raise RuntimeError("Opera started but did not open its control port. "
                           "Close every JARVIS Opera window and try again.")

    def _on_new_page(self, page):
        # A link that opens in a new tab: follow it, like a person would.
        self.page = page

    def _settle(self, timeout=8000):
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except Exception:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=2500)
        except Exception:
            pass

    # ── seeing ────────────────────────────────────────────────────────────
    def snapshot(self, limit: int = SNAPSHOT_LIMIT) -> str:
        page = self._connect()
        self._refs = {}
        lines, n, below = [], 1, 0
        frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]
        for frame in frames[:12]:
            try:
                if frame is not page.main_frame:
                    el = frame.frame_element()
                    box = el.bounding_box()
                    if not box or box["width"] < 50 or box["height"] < 20:
                        continue
                data = frame.evaluate(_SNAPSHOT_JS, n)
            except Exception:
                continue
            if frame is page.main_frame:
                below = data.get("below", 0)
            for it in data["items"]:
                self._refs[it["id"]] = frame
                where = "" if frame is page.main_frame else " (in a pop-up frame)"
                lines.append(f"[{it['id']}] {it['role']}: {it['name'] or '(no label)'}{it['extra']}{where}")
            n += len(data["items"])
        shown = lines[:limit]
        head = f"Page: {page.title()[:100]} — {page.url[:150]}"
        body = "\n".join(shown) if shown else "(nothing clickable on screen)"
        more = ""
        if len(lines) > limit:
            more += f"\n…{len(lines) - limit} more on screen (browser_elements with limit=200 lists all)."
        if below > 0.2:
            more += f"\nAbout {below} screens more below (browser_scroll to see)."
        return f"{head}\nClickable on screen:\n{body}{more}"

    def _locator(self, ref: int):
        frame = self._refs.get(int(ref))
        if frame is None:
            raise ValueError(f"No element [{ref}] on the current view. Call browser_elements to refresh the numbers.")
        return frame.locator(f'[data-jarvis-id="{int(ref)}"]').first

    # ── acting ────────────────────────────────────────────────────────────
    def open(self, url: str) -> str:
        page = self._connect()
        url = url.strip()
        if " " in url or "." not in url:
            url = "https://www.google.com/search?q=" + quote_plus(url)
        elif not url.startswith(("http://", "https://", "file:", "about:")):
            url = "https://" + url
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self._settle()
        return self.snapshot()

    def click(self, ref: int) -> str:
        self._connect()
        loc = self._locator(ref)
        before = self.page.url
        try:
            loc.scroll_into_view_if_needed(timeout=3000)
            loc.click(timeout=5000)
        except Exception:
            try:
                loc.click(timeout=3000, force=True)
            except Exception:
                loc.evaluate("e => e.click()")
        time.sleep(0.6)
        self._settle(6000)
        note = "Clicked; the page changed." if self.page.url != before else "Clicked."
        return f"{note}\n{self.snapshot()}"

    def type(self, ref: int, text: str, submit: bool = False) -> str:
        self._connect()
        loc = self._locator(ref)
        try:
            loc.fill(text, timeout=5000)
        except Exception:
            loc.click(timeout=5000)
            self.page.keyboard.press("Control+A")
            self.page.keyboard.type(text, delay=20)
        if submit:
            loc.press("Enter")
            time.sleep(0.6)
            self._settle()
        return f"Typed {text!r}{' and pressed Enter' if submit else ''}.\n{self.snapshot()}"

    def select(self, ref: int, option: str) -> str:
        self._connect()
        loc = self._locator(ref)
        try:
            loc.select_option(label=option, timeout=5000)
        except Exception:
            loc.select_option(value=option, timeout=5000)
        self._settle(4000)
        return f"Selected {option!r}.\n{self.snapshot()}"

    def press(self, key: str) -> str:
        self._connect().keyboard.press(key)
        time.sleep(0.4)
        self._settle(4000)
        return f"Pressed {key}.\n{self.snapshot()}"

    def scroll(self, direction: str = "down", screens: float = 1) -> str:
        page = self._connect()
        h = page.evaluate("innerHeight")
        dy = int(h * 0.85 * float(screens or 1)) * (-1 if direction == "up" else 1)
        page.mouse.move(page.viewport_size["width"] / 2 if page.viewport_size else 400, h / 2)
        page.mouse.wheel(0, dy)
        time.sleep(0.6)
        return self.snapshot()

    def read(self, find: str = "") -> str:
        page = self._connect()
        text = page.evaluate("""() => {
            const pick = document.querySelector('main, article, [role=main]');
            const t = (pick && pick.innerText.length > 400 ? pick : document.body).innerText;
            return t.replace(/\\n{3,}/g, '\\n\\n');
        }""")
        head = f"Page: {page.title()[:100]} — {page.url[:150]}\n"
        if find:
            words = [w.lower() for w in find.split() if w]
            lines = text.splitlines()
            hits = [i for i, ln in enumerate(lines) if any(w in ln.lower() for w in words)]
            if not hits:
                return head + f"No line mentions {find!r}. Full text starts:\n" + text[:3000]
            keep = sorted({j for i in hits for j in range(max(0, i - 2), min(len(lines), i + 3))})
            return head + "\n".join(lines[j] for j in keep)
        return head + text

    def tables(self) -> str:
        page = self._connect()
        data = page.evaluate("""() => [...document.querySelectorAll('table')].slice(0, 8).map(t =>
            [...t.rows].slice(0, 200).map(r => [...r.cells].map(c => c.innerText.replace(/\\s+/g, ' ').trim())))""")
        if not data:
            return "There are no HTML tables on this page. Use browser_read to get the text."
        out = []
        for i, rows in enumerate(data, 1):
            out.append(f"Table {i} ({len(rows)} rows):")
            out += ["\t".join(r) for r in rows]
        return "\n".join(out)

    def screenshot(self) -> Result:
        page = self._connect()
        png = page.screenshot(type="jpeg", quality=70)
        return Result(f"Screenshot of {page.url[:150]} attached.\n" + self.snapshot(40),
                      image=png, mime="image/jpeg")

    def back(self) -> str:
        self._connect().go_back(wait_until="domcontentloaded", timeout=15000)
        self._settle()
        return self.snapshot()

    def tabs(self, action: str = "list", index: int | None = None, url: str = "") -> str:
        page = self._connect()
        ctx = page.context
        pages = [p for p in ctx.pages if not p.is_closed()]
        if action == "new":
            self.page = ctx.new_page()
            return self.open(url) if url else "Opened a new tab."
        if action in ("switch", "close") and index is not None:
            if not 1 <= int(index) <= len(pages):
                return f"There is no tab {index}; there are {len(pages)}."
            target = pages[int(index) - 1]
            if action == "close":
                target.close()
                rest = [p for p in ctx.pages if not p.is_closed()]
                self.page = rest[-1] if rest else ctx.new_page()
                return f"Closed tab {index}."
            self.page = target
            target.bring_to_front()
            return self.snapshot()
        return "\n".join(f"{i}. {'*' if p is page else ' '} {p.title()[:60]} — {p.url[:100]}"
                         for i, p in enumerate(pages, 1)) + "\n(* = current tab)"


BROWSER = Browser()


def _label(verb):
    return lambda a: f"Opera: {verb} {a.get('url') or a.get('text') or a.get('key') or a.get('ref') or ''}".strip()


@tool("browser_open", """Open a web page in JARVIS's Opera window (a URL, a site name like
sahibinden.com, or words to search on Google). Returns the page title and the numbered list of
clickable things on screen.""",
      {"url": {"type": "string", "description": "URL, domain, or search words"}}, ["url"],
      label=_label("açılıyor:"))
def browser_open(url: str):
    return BROWSER.open(url)


@tool("browser_click", """Click an element by its number from the latest list, e.g. 12 for
"[12] link: Emlak". Returns the updated list. If a cookie/consent banner is in the way, click its
accept button first.""",
      {"ref": {"type": "integer"}}, ["ref"], label=lambda a: f"Opera: [{a.get('ref')}] tıklanıyor")
def browser_click(ref: int):
    return BROWSER.click(ref)


@tool("browser_type", "Type into an input box by its number. submit=true presses Enter afterwards.",
      {"ref": {"type": "integer"}, "text": {"type": "string"},
       "submit": {"type": "boolean"}}, ["ref", "text"],
      label=lambda a: f"Opera: yazılıyor \"{str(a.get('text'))[:40]}\"")
def browser_type(ref: int, text: str, submit: bool = False):
    return BROWSER.type(ref, text, submit)


@tool("browser_select", "Choose an option in a dropdown by its number and the option's visible text.",
      {"ref": {"type": "integer"}, "option": {"type": "string"}}, ["ref", "option"],
      label=lambda a: f"Opera: seçiliyor {a.get('option')}")
def browser_select(ref: int, option: str):
    return BROWSER.select(ref, option)


@tool("browser_press", "Press a key in the page, e.g. Enter, Escape, PageDown, Control+F.",
      {"key": {"type": "string"}}, ["key"], label=_label("tuşa basılıyor:"))
def browser_press(key: str):
    return BROWSER.press(key)


@tool("browser_scroll", "Scroll the page and return what is clickable afterwards.",
      {"direction": {"type": "string", "enum": ["down", "up"]},
       "screens": {"type": "number", "description": "how many screens, default 1"}},
      label=lambda a: "Opera: sayfa kaydırılıyor")
def browser_scroll(direction: str = "down", screens: float = 1):
    return BROWSER.scroll(direction, screens)


@tool("browser_elements", "List the clickable elements on screen again (refreshes the numbers).",
      {"limit": {"type": "integer"}}, label=lambda a: "Opera: sayfaya bakılıyor")
def browser_elements(limit: int = SNAPSHOT_LIMIT):
    return BROWSER.snapshot(limit)


@tool("browser_read", """Read the text of the current page, to pull out data (prices, listings,
articles). With find="word" only lines mentioning those words come back.""",
      {"find": {"type": "string"}}, label=lambda a: "Opera: sayfa okunuyor")
def browser_read(find: str = ""):
    return BROWSER.read(find)


@tool("browser_tables", "Return the HTML tables on the current page as tab-separated rows.",
      label=lambda a: "Opera: tablolar okunuyor")
def browser_tables():
    return BROWSER.tables()


@tool("browser_screenshot", """See the page as an image, when the element list is not enough to
understand it (maps, images, odd layouts).""", label=lambda a: "Opera: ekran görüntüsü")
def browser_screenshot():
    return BROWSER.screenshot()


@tool("browser_back", "Go back to the previous page.", label=lambda a: "Opera: geri")
def browser_back():
    return BROWSER.back()


@tool("browser_tabs", "List, switch to, open or close tabs. Tabs are numbered from 1.",
      {"action": {"type": "string", "enum": ["list", "switch", "new", "close"]},
       "index": {"type": "integer"}, "url": {"type": "string"}},
      label=lambda a: "Opera: sekmeler")
def browser_tabs(action: str = "list", index: int | None = None, url: str = ""):
    return BROWSER.tabs(action, index, url)
