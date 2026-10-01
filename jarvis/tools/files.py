"""Files and folders. Relative paths mean the JARVIS workspace (Documents/JARVIS)."""
from __future__ import annotations

import fnmatch
import os
import shutil
from datetime import datetime
from pathlib import Path

from jarvis import config
from jarvis.tools import tool

_TEXT = {".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".html", ".xml", ".ini", ".yaml", ".yml", ".tsv"}


def _outside_workspace(p: Path) -> bool:
    try:
        p.resolve().relative_to(config.workspace().resolve())
        return False
    except ValueError:
        return True


@tool("list_files", """List a folder. Empty path = the JARVIS workspace. Shortcuts: "Desktop",
"Downloads", "Documents" mean the user's folders.""",
      {"path": {"type": "string"}}, label=lambda a: f"Klasöre bakılıyor: {a.get('path') or 'JARVIS'}")
def list_files(path: str = ""):
    p = _shortcut(path) if path else config.workspace()
    if not p.exists():
        return f"{p} does not exist."
    rows = []
    for e in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))[:300]:
        try:
            st = e.stat()
            when = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            rows.append(f"{'[dir] ' if e.is_dir() else ''}{e.name}" + ("" if e.is_dir() else f"  ({st.st_size:,} B, {when})"))
        except OSError:
            rows.append(e.name)
    return f"{p}\n" + ("\n".join(rows) or "(empty)")


def _shortcut(path: str) -> Path:
    home = Path.home()
    key = path.strip().lower()
    names = {"desktop": "Desktop", "masaüstü": "Desktop", "downloads": "Downloads", "indirilenler": "Downloads",
             "documents": "Documents", "belgeler": "Documents", "pictures": "Pictures", "resimler": "Pictures",
             "music": "Music", "videos": "Videos"}
    if key in names:
        for base in (home / "OneDrive", home):
            if (base / names[key]).exists():
                return base / names[key]
        return home / names[key]
    return config.resolve(path)


@tool("find_files", "Find files by name pattern (e.g. *.xlsx, *fatura*) under a folder (default: the user's home).",
      {"pattern": {"type": "string"}, "folder": {"type": "string"}}, ["pattern"],
      label=lambda a: f"Dosya aranıyor: {a.get('pattern')}")
def find_files(pattern: str, folder: str = ""):
    root = _shortcut(folder) if folder else Path.home()
    pat = pattern if any(c in pattern for c in "*?[") else f"*{pattern}*"
    hits = []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("AppData", "node_modules", "$Recycle.Bin")]
        for n in names:
            if fnmatch.fnmatch(n.lower(), pat.lower()):
                hits.append(os.path.join(dirpath, n))
                if len(hits) >= 50:
                    return "\n".join(hits) + "\n(first 50)"
    return "\n".join(hits) or "Nothing found."


@tool("read_file", "Read a text, Word (.docx) or PDF file. For Excel use excel_read.",
      {"path": {"type": "string"}}, ["path"], label=lambda a: f"Okunuyor: {Path(a.get('path', '')).name}")
def read_file(path: str):
    p = _shortcut(path)
    ext = p.suffix.lower()
    if ext == ".docx":
        import docx
        return "\n".join(par.text for par in docx.Document(p).paragraphs)
    if ext == ".pdf":
        try:
            import pdfplumber
            with pdfplumber.open(p) as pdf:
                return "\n\n".join((pg.extract_text() or "") for pg in pdf.pages[:50])
        except ImportError:
            from PyPDF2 import PdfReader
            return "\n\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages[:50])
    if ext in (".xlsx", ".xlsm", ".xls"):
        return "This is a spreadsheet; use excel_read."
    return p.read_text(encoding="utf-8", errors="replace")


def _confirm_write(a):
    p = _shortcut(a.get("path", ""))
    if p.exists() and _outside_workspace(p) and not a.get("append"):
        return f"JARVIS klasörü dışındaki mevcut bir dosyanın üzerine yazılacak: {p}"
    return None


@tool("write_file", """Save text to a file (txt, md, csv, html…). Relative paths go to the JARVIS
workspace. append=true adds to the end. Returns the full path.""",
      {"path": {"type": "string"}, "content": {"type": "string"}, "append": {"type": "boolean"}},
      ["path", "content"], confirm=_confirm_write, label=lambda a: f"Kaydediliyor: {Path(a.get('path', '')).name}")
def write_file(path: str, content: str, append: bool = False):
    p = _shortcut(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a" if append else "w", encoding="utf-8-sig" if p.suffix.lower() == ".csv" and not append else "utf-8") as f:
        f.write(content)
    return f"Saved {p} ({p.stat().st_size:,} bytes)."


@tool("move_file", "Move or rename a file or folder; copy=true copies instead.",
      {"source": {"type": "string"}, "destination": {"type": "string"}, "copy": {"type": "boolean"}},
      ["source", "destination"],
      confirm=lambda a: None if a.get("copy") else f"Taşınacak: {a.get('source')} → {a.get('destination')}",
      label=lambda a: f"{'Kopyalanıyor' if a.get('copy') else 'Taşınıyor'}: {Path(a.get('source', '')).name}")
def move_file(source: str, destination: str, copy: bool = False):
    s, d = _shortcut(source), _shortcut(destination)
    if not s.exists():
        return f"{s} does not exist."
    d.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        (shutil.copytree if s.is_dir() else shutil.copy2)(s, d)
    else:
        shutil.move(str(s), str(d))
    return f"{'Copied' if copy else 'Moved'} to {d}."


@tool("delete_file", "Send a file or folder to the Recycle Bin.",
      {"path": {"type": "string"}}, ["path"],
      confirm=lambda a: f"Geri Dönüşüm Kutusuna gönderilecek: {_shortcut(a.get('path', ''))}",
      label=lambda a: f"Siliniyor: {Path(a.get('path', '')).name}")
def delete_file(path: str):
    from send2trash import send2trash
    p = _shortcut(path)
    if not p.exists():
        return f"{p} does not exist."
    send2trash(str(p))
    return f"Moved {p} to the Recycle Bin."
