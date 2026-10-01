"""
The window: a chat with JARVIS. Each request shows the steps JARVIS takes
under it as they happen, then the answer. The microphone button (or
Ctrl+Alt+J from anywhere) listens; answers are read aloud when 🔊 is on.
"Sohbet" mode keeps listening after each answer, for a hands-free back and forth.
"""
from __future__ import annotations

import html
import os
import re
import subprocess
import sys
import threading

from PyQt6.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QFont, QKeyEvent
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                             QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
                             QSizePolicy, QVBoxLayout, QWidget)

from jarvis import config, llm, voice
from jarvis.agent import Agent

STYLE = """
QWidget { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', 'Inter', sans-serif; font-size: 14px; }
#header { background: #10161f; border-bottom: 1px solid #1f2a37; }
#title { font-size: 18px; font-weight: 700; color: #58c4ff; letter-spacing: 3px; }
#status { color: #8b98a8; font-size: 12px; }
QPushButton { background: #18212d; border: 1px solid #263243; border-radius: 8px; padding: 6px 10px; }
QPushButton:hover { background: #213042; }
QPushButton:checked { background: #12395a; border-color: #58c4ff; }
#send { background: #1f6feb; border: none; font-weight: 600; min-width: 64px; }
#send[stop="true"] { background: #b42318; }
#mic { min-width: 44px; font-size: 16px; }
#mic[live="true"] { background: #b42318; border-color: #ff6b6b; }
QPlainTextEdit { background: #121a24; border: 1px solid #263243; border-radius: 10px; padding: 8px; }
#user { background: #1f6feb; border-radius: 12px; padding: 8px 12px; color: white; }
#bot { background: #161f2b; border: 1px solid #222e3d; border-radius: 12px; padding: 8px 12px; }
#interim { color: #a8b5c4; font-style: italic; padding: 2px 6px; }
#step { color: #8b98a8; font-size: 12px; padding: 0 6px; }
#error { background: #3a1517; border: 1px solid #7a2a2f; border-radius: 10px; padding: 8px 12px; color: #ffb4b4; }
QScrollArea { border: none; }
QScrollBar:vertical { width: 8px; background: transparent; }
QScrollBar::handle:vertical { background: #263243; border-radius: 4px; }
"""


def md_to_html(text: str) -> str:
    t = html.escape(text, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"(https?://[^\s<]+)", r'<a style="color:#79c0ff" href="\1">\1</a>', t)
    t = re.sub(r"(?m)^\s*[-*] ", "• ", t)
    return t.replace("\n", "<br>")


class Bridge(QObject):
    event = pyqtSignal(str, dict)
    ask = pyqtSignal(str, str)
    heard = pyqtSignal(str)
    listen_state = pyqtSignal(bool)
    play = pyqtSignal(str)
    spoken = pyqtSignal()
    hotkey = pyqtSignal()


class Input(QPlainTextEdit):
    submitted = pyqtSignal()

    def keyPressEvent(self, e: QKeyEvent):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(e)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("JARVIS")
        self.resize(500, 780)
        self.bridge = Bridge()
        self._answer = (threading.Event(), [False])
        self.recorder = voice.Recorder()
        self.listening = False
        self._steps_box = None
        self._step_rows: list[QLabel] = []
        self._player = None

        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        header = QFrame(objectName="header")
        h = QHBoxLayout(header)
        h.setContentsMargins(14, 10, 10, 10)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(QLabel("JARVIS", objectName="title"))
        self.status = QLabel("Hazır", objectName="status")
        col.addWidget(self.status)
        h.addLayout(col)
        h.addStretch()
        self.speak_btn = self._tool_btn("🔊", "Cevapları sesli oku", checkable=True)
        self.speak_btn.setChecked(bool(config.get("speak")))
        self.speak_btn.toggled.connect(lambda on: config.set("speak", on))
        self.talk_btn = self._tool_btn("Sohbet", "Her cevaptan sonra yine dinle (eller serbest)", checkable=True)
        for b, tip, fn in ((self._tool_btn("📁", "Çalışma klasörünü aç"), None, self._open_workspace),
                           (self._tool_btn("＋", "Yeni sohbet"), None, self._new_chat),
                           (self._tool_btn("⚙", "Gemini API anahtarı"), None, self._ask_key)):
            b.clicked.connect(fn)
            h.addWidget(b)
        h.insertWidget(2, self.speak_btn)
        h.insertWidget(3, self.talk_btn)
        lay.addWidget(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.feed = QWidget()
        self.feed_lay = QVBoxLayout(self.feed)
        self.feed_lay.setContentsMargins(12, 12, 12, 12)
        self.feed_lay.setSpacing(8)
        self.feed_lay.addStretch()
        self.scroll.setWidget(self.feed)
        lay.addWidget(self.scroll, 1)

        bottom = QFrame()
        b = QHBoxLayout(bottom)
        b.setContentsMargins(10, 8, 10, 10)
        self.mic = QPushButton("🎤", objectName="mic")
        self.mic.setToolTip("Konuş (Ctrl+Alt+J)")
        self.mic.clicked.connect(self.toggle_listen)
        self.input = Input()
        self.input.setPlaceholderText("Ne yapayım? (ör. sahibinden'den Kadıköy kiralık daireleri Excel'e çek)")
        self.input.setFixedHeight(64)
        self.input.submitted.connect(self._submit)
        self.send = QPushButton("Gönder", objectName="send")
        self.send.clicked.connect(self._send_or_stop)
        b.addWidget(self.mic)
        b.addWidget(self.input, 1)
        b.addWidget(self.send)
        lay.addWidget(bottom)
        self.setCentralWidget(root)

        self.bridge.event.connect(self._on_event)
        self.bridge.ask.connect(self._on_ask)
        self.bridge.heard.connect(self._on_heard)
        self.bridge.listen_state.connect(self._on_listen_state)
        self.bridge.play.connect(self._play)
        self.bridge.spoken.connect(self._after_speaking)
        self.bridge.hotkey.connect(self._on_hotkey)

        self.agent = Agent(on_event=lambda k, d: self.bridge.event.emit(k, d), approve=self._approve)
        self._add("bot", "Merhaba, ben JARVIS. Yazabilir ya da 🎤'ya basıp konuşabilirsin.")
        self._start_hotkey()
        if not config.api_key():
            QTimer.singleShot(300, self._ask_key)

    # ── building blocks ───────────────────────────────────────────────────
    def _tool_btn(self, text, tip, checkable=False):
        b = QPushButton(text)
        b.setToolTip(tip)
        b.setCheckable(checkable)
        return b

    def _add(self, kind: str, text: str) -> QLabel:
        lab = QLabel(md_to_html(text) if kind != "step" else html.escape(text, quote=False), objectName=kind)
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse |
                                    Qt.TextInteractionFlag.LinksAccessibleByMouse)
        lab.setOpenExternalLinks(False)
        lab.linkActivated.connect(lambda url: self.agent.call_tool("browser_open", {"url": url}))
        lab.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        row = QHBoxLayout()
        if kind == "user":
            row.addStretch()
            lab.setMaximumWidth(380)
        row.addWidget(lab, 0 if kind == "user" else 1)
        holder = QWidget()
        holder.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        self.feed_lay.insertWidget(self.feed_lay.count() - 1, holder)
        QTimer.singleShot(30, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()))
        return lab

    def _set_busy(self, busy: bool):
        self.send.setText("Durdur" if busy else "Gönder")
        self.send.setProperty("stop", "true" if busy else "false")
        self.send.style().unpolish(self.send)
        self.send.style().polish(self.send)
        if not busy:
            self.status.setText("Hazır")

    # ── sending ───────────────────────────────────────────────────────────
    def _submit(self):
        if self.agent.busy:
            return
        text = self.input.toPlainText().strip()
        if text:
            self.input.clear()
            self.ask(text)

    def _send_or_stop(self):
        if self.agent.busy:
            self.agent.stop()
            self.status.setText("Durduruluyor…")
        else:
            self._submit()

    def ask(self, text: str):
        if not config.api_key():
            self._ask_key()
            if not config.api_key():
                return
        self._stop_audio()
        self._add("user", text)
        self._step_rows = []
        self._set_busy(True)
        self.agent.send(text)

    # ── agent events ──────────────────────────────────────────────────────
    def _on_event(self, kind: str, d: dict):
        if kind == "status":
            self.status.setText(d["text"])
        elif kind == "thinking":
            self.status.setText("Düşünüyor…")
        elif kind == "tool":
            self.status.setText(d["label"])
            self._step_rows.append(self._add("step", "›  " + d["label"]))
        elif kind == "tool_done" and self._step_rows:
            row = self._step_rows[-1]
            mark = "✓" if d["ok"] else "✗"
            row.setText(row.text().replace("›  ", f"{mark}  ", 1))
            if not d["ok"]:
                row.setToolTip(d.get("short", ""))
        elif kind == "say":
            if d.get("interim"):
                self._add("interim", d["text"])
            else:
                self._add("bot", d["text"])
                self._speak(d["text"])
        elif kind == "error":
            if d["text"] == "NO_KEY":
                self._ask_key()
            else:
                self._add("error", "Bir sorun çıktı: " + d["text"])
        elif kind == "done":
            self._set_busy(False)
            if not d.get("text") and self.talk_btn.isChecked():
                self._after_speaking()

    def _approve(self, action: str, reason: str) -> bool:
        """Called on the worker thread; blocks until the user answers the dialog."""
        ev, box = self._answer
        ev.clear()
        self.bridge.ask.emit(action, reason)
        ev.wait()
        return box[0]

    def _on_ask(self, action: str, reason: str):
        self.raise_()
        self.activateWindow()
        r = QMessageBox.question(self, "JARVIS onay istiyor", f"{reason}\n\nİzin veriyor musun?",
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                 QMessageBox.StandardButton.No)
        self._answer[1][0] = r == QMessageBox.StandardButton.Yes
        self._add("step", ("✓ izin verildi: " if self._answer[1][0] else "✗ izin verilmedi: ") + action)
        self._answer[0].set()

    # ── voice ─────────────────────────────────────────────────────────────
    def toggle_listen(self):
        if self.listening:
            self.recorder.cancel()
            return
        if self.agent.busy:
            return
        self._stop_audio()
        self.bridge.listen_state.emit(True)

        def run():
            try:
                wav = self.recorder.record()
                if wav:
                    self.bridge.event.emit("status", {"text": "Anlıyorum…"})
                    text = llm.transcribe(wav, config.get("language") or "tr")
                    self.bridge.heard.emit(text)
                else:
                    self.bridge.heard.emit("")
            except Exception as e:
                print(f"[voice] {e}")
                self.bridge.event.emit("error", {"text": f"Mikrofon: {e}"})
                self.bridge.heard.emit("")
            finally:
                self.bridge.listen_state.emit(False)
        threading.Thread(target=run, daemon=True).start()

    def _on_listen_state(self, on: bool):
        self.listening = on
        self.mic.setProperty("live", "true" if on else "false")
        self.mic.style().unpolish(self.mic)
        self.mic.style().polish(self.mic)
        self.mic.setText("■" if on else "🎤")
        if on:
            self.status.setText("Dinliyorum…")
        elif not self.agent.busy:
            self.status.setText("Hazır")

    def _on_heard(self, text: str):
        if text:
            self.ask(text)
        elif self.talk_btn.isChecked():
            self.talk_btn.setChecked(False)       # nobody spoke: end hands-free mode
            self.status.setText("Sohbet modu kapandı (ses gelmedi)")

    def _speak(self, text: str):
        if not self.speak_btn.isChecked():
            return

        def run():
            path = voice.synthesize(text)
            if path:
                self.bridge.play.emit(str(path))
            else:
                voice.speak_fallback(text)
                self.bridge.spoken.emit()
        threading.Thread(target=run, daemon=True).start()

    def _play(self, path: str):
        from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
        if self._player is None:
            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._player.setAudioOutput(self._audio)
            self._player.mediaStatusChanged.connect(
                lambda s: self.bridge.spoken.emit() if s == QMediaPlayer.MediaStatus.EndOfMedia else None)
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()
        self.status.setText("Konuşuyor…")

    def _stop_audio(self):
        if self._player is not None:
            self._player.stop()

    def _after_speaking(self):
        if not self.agent.busy:
            self.status.setText("Hazır")
        if self.talk_btn.isChecked() and not self.agent.busy and not self.listening:
            QTimer.singleShot(250, self.toggle_listen)

    def _start_hotkey(self):
        try:
            from pynput import keyboard
            hk = keyboard.GlobalHotKeys({"<ctrl>+<alt>+j": self.bridge.hotkey.emit})
            hk.daemon = True
            hk.start()
        except Exception as e:
            print(f"[ui] global hotkey unavailable: {e}")

    def _on_hotkey(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.toggle_listen()

    # ── header buttons ────────────────────────────────────────────────────
    def _open_workspace(self):
        path = str(config.workspace())
        if sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])

    def _new_chat(self):
        if self.agent.busy:
            self.agent.stop()
        self.agent.new_chat()
        while self.feed_lay.count() > 1:
            w = self.feed_lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        self._add("bot", "Yeni sohbet. Ne yapalım?")

    def _ask_key(self):
        key, ok = QInputDialog.getText(
            self, "Gemini API anahtarı",
            "JARVIS'in beyni Google Gemini. Ücretsiz anahtarı aistudio.google.com/apikey adresinden alıp buraya yapıştır:",
            QLineEdit.EchoMode.Password, config.get("gemini_api_key") or "")
        if ok and key.strip():
            config.set("gemini_api_key", key.strip())
            self.status.setText("Anahtar kaydedildi")


def run():
    app = QApplication(sys.argv)
    app.setApplicationName("JARVIS")
    app.setStyleSheet(STYLE)
    app.setFont(QFont("Segoe UI", 10))
    w = Window()
    w.show()
    return app.exec()
