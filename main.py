"""Stealth chat window: hidden from screen sharing, screenshots and recordings."""
import html
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import (QBuffer, QEasingCurve, QElapsedTimer, QIODevice, QObject, QPoint, QPropertyAnimation, QRect,
                            QRunnable, Qt, QThreadPool, QTimer, Signal)
from PySide6.QtGui import QColor, QImage, QGuiApplication, QKeySequence, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ai_client import AnswerInterrupted, GigaChatClient, GigaChatError
from hotkeys import GlobalHotkeys
from math_render import markdown_to_html, warm_up as warm_up_math
from platform_support import IS_LINUX, IS_MAC, cache_dir, describe
from prompt_bar import PromptBar
from screenshot import ScreenshotError, capture_relevant_screen
from session_log import SessionLog
from settings import Config, SettingsPanel
from stealth import activate_app, hide_app, hide_from_dock, make_capture_proof, unhide_app
from transcriber import DialogListener
from transcript_panel import TranscriptPanel

SHOT_DIR = cache_dir()  # last screenshot sent, overwritten each time
GRIP = 7  # width of the invisible resize border, px
RADIUS = 12
MIN_W, MIN_H = 300, 220
DEFAULT_SIZE = (460, 560)

STYLE = """
QWidget { color: #e8e8ea; font-size: 14px; }
#title, #pageLabel, #hint { color: #8a8a90; font-size: 12px; }
#section { color: #7fa7e0; font-size: 12px; font-weight: 600; margin-top: 4px; }
QPushButton {
    background: rgba(255,255,255,0.08); border: none; border-radius: 8px;
    min-width: 32px; min-height: 30px; padding: 0 10px; font-size: 14px;
}
QPushButton:hover { background: rgba(255,255,255,0.16); }
QPushButton:disabled { color: #55555a; background: rgba(255,255,255,0.03); }
#navBtn { padding: 0; font-size: 16px; }
#closeBtn, #gearBtn { min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px; padding: 0; border-radius: 15px; font-size: 16px; }
#gearBtn { font-size: 20px; }
#resetKey { min-width: 30px; max-width: 30px; padding: 0; font-size: 16px; }
#closeBtn:hover { background: #d9534f; }
#warn { color: #e0a040; font-size: 15px; }
#promptBar { background: rgba(255,255,255,0.06); border-radius: 8px; }
#promptText { color: #c9c9cf; background: transparent; }
#listenBtn, #transcriptBtn { min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px; padding: 0; border-radius: 15px; font-size: 15px; }
#listenBtn:checked { background: rgba(217,83,79,0.55); }
#listenStatus { color: #8a8a90; font-size: 12px; }
#transcript {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px; padding: 4px; font-size: 13px; selection-background-color: #3b6ea8;
}
QLineEdit {
    background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.12);
    border-radius: 8px; padding: 6px 10px; selection-background-color: #3b6ea8;
}
QLineEdit:focus { border-color: rgba(120,170,255,0.6); }
QScrollArea, #content { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }
QScrollBar::handle:vertical { background: rgba(255,255,255,0.22); border-radius: 3px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page { height: 0; background: none; }
QSlider::groove:horizontal { height: 4px; background: rgba(255,255,255,0.15); border-radius: 2px; }
QSlider::sub-page:horizontal { background: #5b8fd6; border-radius: 2px; }
QSlider::handle:horizontal { background: #e8e8ea; width: 14px; margin: -5px 0; border-radius: 7px; }
"""


class _AskSignals(QObject):
    chunk = Signal(int, str)  # page index, next piece of the answer
    done = Signal(int)


class AskTask(QRunnable):
    """Streams client.ask_stream() off the UI thread, chunk by chunk, via signals.

    ``from_dialog``: answer the conversation (⌘↵, or ⌘⇧↵ with a screenshot). The
    phrase being spoken right now is cut off and transcribed first, so the answer
    covers the latest question; meanwhile a screenshot is read into text.
    """

    def __init__(self, client, listener, page_index: int, prompt: str, image: bytes | None = None,
                 from_dialog: bool = False):
        super().__init__()
        self.client, self.listener, self.page_index = client, listener, page_index
        self.prompt, self.image, self.from_dialog = prompt, image, from_dialog
        self.signals = _AskSignals()

    def run(self):
        with self.client.urgent():
            self._run()
        self.signals.done.emit(self.page_index)

    def _read_screenshot(self) -> str:
        with self.client.urgent():
            jpeg = shrink_screenshot(self.image)
            try:  # a copy to check what the model was shown; never worth failing the answer over
                SHOT_DIR.mkdir(parents=True, exist_ok=True)
                (SHOT_DIR / "last_screen.jpg").write_bytes(jpeg)
            except OSError:
                pass
            image_id = self.client.upload(jpeg, "screen.jpg", "image/jpeg")
            try:
                return self.client.read_screen(image_id)
            finally:  # low-priority cleanup; the answer must not wait for it
                threading.Thread(target=self.client.delete_file, args=(image_id,), daemon=True).start()

    def _run(self):
        try:
            started = time.monotonic()
            reading = None
            if self.image is not None:
                pool = ThreadPoolExecutor(1)
                reading = pool.submit(self._read_screenshot)
                pool.shutdown(wait=False)
            if self.from_dialog:
                self.listener.flush()
                self.listener.wait_pending()
            screen_text = reading.result() if reading else None
            print(f"answer: context ready in {time.monotonic() - started:.1f}s", file=sys.stderr)
            for attempt in range(2):
                try:
                    for piece in self.client.ask_stream(self.prompt, screen_text, transcript=self.listener.context(),
                                                        from_dialog=self.from_dialog):
                        self.signals.chunk.emit(self.page_index, piece)
                    return
                except AnswerInterrupted:
                    if attempt:
                        raise
                    self.signals.chunk.emit(self.page_index, "\n\n*(связь с GigaChat оборвалась — отвечаю заново)*\n\n")
                except GigaChatError as exc:
                    if attempt or not any(code in str(exc) for code in ("HTTP 413", "HTTP 422")):
                        raise
                    print(f"answer: request too big ({exc}); compacting the context", file=sys.stderr)
                    self.listener.compact_now()  # fold older dialog into the summary and try again
        except Exception as exc:  # surface API errors on the page instead of crashing
            self.signals.chunk.emit(self.page_index, f"\n\n[error] {exc}")


def shrink_screenshot(png: bytes, max_side: int = 2048) -> bytes:
    """Retina PNGs are several MB; a JPEG this size uploads fast and stays readable for the model."""
    image = QImage.fromData(png)
    if max(image.width(), image.height()) > max_side:
        image = image.scaled(max_side, max_side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    image.save(buf, "JPEG", 85)
    return bytes(buf.data())


class PageView(QScrollArea):
    """Text can be scrolled fully out of view through either edge, then stops.

    Spacers as tall as the viewport sit above and below the text, so the
    scroll range runs from "text just left through the bottom" (0) to
    "text just left through the top" (viewport + text height). The resting
    position is the top of the text. Horizontal trackpad swipes flip pages.
    """

    swiped = Signal(int)  # +1 next page, -1 previous page
    SWIPE_THRESHOLD = 90

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)

        content = QWidget(objectName="content")
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setContentsMargins(4, 0, 10, 0)  # no vertical margins: text must fully exit
        self.content_layout.setSpacing(0)

        self.top_spacer = QWidget()
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.RichText)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.bottom_spacer = QWidget()
        for w in (self.top_spacer, self.label, self.bottom_spacer):
            self.content_layout.addWidget(w)
        self.setWidget(content)

        self._swipe_acc = 0
        self._swipe_locked = False
        self._swipe_clock = QElapsedTimer()
        self._swipe_clock.start()
        self._scroll_anim = QPropertyAnimation(self.verticalScrollBar(), b"value", self,
                                               duration=160, easingCurve=QEasingCurve.OutCubic)

    def scroll_half(self, direction: int):
        """Keyboard scrolling: move by half the visible height, within the text (never past its ends)."""
        bar = self.verticalScrollBar()
        top = self.top_spacer.height()  # first line at the top edge
        bottom = max(top, top + self.label.height() - self.viewport().height())  # last line at the bottom edge
        running = self._scroll_anim.state() == QPropertyAnimation.Running
        current = self._scroll_anim.endValue() if running else bar.value()  # repeated presses add up
        target = min(max(current + direction * self.viewport().height() // 2, top), bottom)
        self._scroll_anim.stop()
        self._scroll_anim.setStartValue(bar.value())
        self._scroll_anim.setEndValue(target)
        self._scroll_anim.start()

    def set_html(self, text: str, keep_scroll=False):
        bar = self.verticalScrollBar()
        pos = bar.value()
        self.label.setText(text)
        self.content_layout.activate()  # update the scroll range now, not on the next event loop pass
        bar.setValue(pos if keep_scroll else self.top_spacer.height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        old, new = self.top_spacer.height(), self.viewport().height()
        if old == new:
            return
        bar = self.verticalScrollBar()
        pos = bar.value()
        self.top_spacer.setFixedHeight(new)
        self.bottom_spacer.setFixedHeight(new)
        self.content_layout.activate()
        bar.setValue(pos + new - old)  # keep the text where it was on screen

    def wheelEvent(self, e):
        d = e.pixelDelta() if not e.pixelDelta().isNull() else e.angleDelta()
        if abs(d.x()) <= abs(d.y()):
            super().wheelEvent(e)
            return
        # One flip per gesture: reset after a pause or when a new gesture begins.
        if e.phase() == Qt.ScrollBegin or self._swipe_clock.elapsed() > 350:
            self._swipe_acc, self._swipe_locked = 0, False
        self._swipe_clock.restart()
        if not self._swipe_locked:
            self._swipe_acc += d.x()
            if abs(self._swipe_acc) >= self.SWIPE_THRESHOLD:
                self._swipe_locked = True
                self.swiped.emit(-1 if self._swipe_acc > 0 else 1)
        e.accept()


class TitleBar(QWidget):
    """Drag handle for the frameless window."""

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.window().windowHandle().startSystemMove()


class ChatWindow(QWidget):
    def __init__(self, client, listener: DialogListener, config: Config, session: SessionLog | None = None):
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        if not IS_MAC:
            flags |= Qt.Tool  # no taskbar button on Windows/Linux (macOS hides the Dock icon instead)
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)  # hover cursor over the resize border
        self.setMinimumSize(MIN_W, MIN_H)
        self.setStyleSheet(STYLE)

        self.client = client
        self.capture_proof = True  # set_capture_proof() corrects this once the window is shown
        self.listener = listener
        self.config = config
        self.session = session
        client.system_prompt = config.system_prompt(client.default_system_prompt)
        # {"q": str, "image": bytes | None, "dialog": bool, "a": str | None, "done": bool}
        self.pages: list[dict] = []
        self.current = -1
        self._prompt_shown_for = None  # page whose prompt is in the bar; keeps it expanded across re-renders
        self.pool = QThreadPool.globalInstance()
        self._tasks = set()
        self.bg_alpha = config.bg_opacity()
        self.setWindowOpacity(config.window_opacity() / 100)

        self._resize_edges = Qt.Edges()
        self._press_global = QPoint()
        self._press_geom = QRect()

        # Save geometry shortly after the last move/resize, so a crash or kill keeps it too.
        self._save_timer = QTimer(self, singleShot=True, interval=400)
        self._save_timer.timeout.connect(lambda: self.config.set_geometry(self.geometry()))

        self._build_ui()
        self._build_shortcuts()
        self._restore_geometry()
        self._render()

    # ---------- UI ----------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(GRIP + 6, GRIP, GRIP + 6, GRIP + 6)
        root.setSpacing(8)

        bar = TitleBar()
        bar_l = QHBoxLayout(bar)
        bar_l.setContentsMargins(4, 4, 0, 0)
        bar_l.setSpacing(6)
        self.title = QLabel("● private", objectName="title")
        bar_l.addWidget(self.title)
        self.listen_status = QLabel(objectName="listenStatus")
        self.listen_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # long errors: elide, don't widen
        bar_l.addWidget(self.listen_status, 1)
        self.listen_btn = QPushButton("🎙", objectName="listenBtn", checkable=True,
                                      toolTip="Listening to the microphone and system audio (click to pause)")
        self.listen_btn.clicked.connect(self.toggle_listen)
        transcript_btn = QPushButton("≡", objectName="transcriptBtn", toolTip="Dialog transcript")
        transcript_btn.clicked.connect(self.toggle_transcript)
        gear_btn = QPushButton("⚙", objectName="gearBtn", toolTip="Settings")
        gear_btn.clicked.connect(self.toggle_settings)
        close_btn = QPushButton("✕", objectName="closeBtn")
        close_btn.clicked.connect(QApplication.quit)
        bar_l.addWidget(self.listen_btn)
        bar_l.addWidget(transcript_btn)
        bar_l.addWidget(gear_btn)
        bar_l.addWidget(close_btn)
        root.addWidget(bar)

        self.prompt_bar = PromptBar()
        self.view = PageView()
        self.view.swiped.connect(self.flip)
        self.chat_page = QWidget()
        chat_l = QVBoxLayout(self.chat_page)
        chat_l.setContentsMargins(0, 0, 0, 0)
        chat_l.setSpacing(8)
        chat_l.addWidget(self.prompt_bar)
        chat_l.addWidget(self.view, 1)
        self.settings_panel = SettingsPanel(self.config, self.client.default_system_prompt)
        self.settings_panel.system_prompt_changed.connect(lambda text: setattr(self.client, "system_prompt", text))
        self.settings_panel.closed.connect(self.toggle_settings)
        self.settings_panel.hotkey_changed.connect(self._set_hotkey)
        self.settings_panel.window_opacity_changed.connect(lambda v: self.setWindowOpacity(v / 100))
        self.settings_panel.bg_opacity_changed.connect(self._set_bg_opacity)
        self.transcript_panel = TranscriptPanel()
        self.transcript_panel.closed.connect(self.toggle_transcript)
        self.transcript_panel.clear_requested.connect(self.listener.clear)
        self.listener.signals.updated.connect(self._on_transcript)
        self.listener.signals.status.connect(self._on_listen_status)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.chat_page)
        self.stack.addWidget(self.settings_panel)
        self.stack.addWidget(self.transcript_panel)
        root.addWidget(self.stack, 1)

        self.chat_bar = QWidget()
        bottom = QHBoxLayout(self.chat_bar)
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(6)
        answer_key = QKeySequence(self.config.hotkey("answer")).toString(QKeySequence.NativeText)  # ⌘↵ / Ctrl+Return
        self.input = QLineEdit(placeholderText=f"Ask something… ({answer_key} answers from the dialog)")
        self.input.returnPressed.connect(self.send)
        self.prev_btn = QPushButton("‹", objectName="navBtn")
        self.prev_btn.clicked.connect(lambda: self.flip(-1))
        self.page_label = QLabel(objectName="pageLabel", alignment=Qt.AlignCenter)
        self.page_label.setMinimumWidth(44)
        self.next_btn = QPushButton("›", objectName="navBtn")
        self.next_btn.clicked.connect(lambda: self.flip(1))
        bottom.addWidget(self.input, 1)
        bottom.addSpacing(4)
        bottom.addWidget(self.prev_btn)
        bottom.addWidget(self.page_label)
        bottom.addWidget(self.next_btn)
        root.addWidget(self.chat_bar)

    def _build_shortcuts(self):
        self.hotkeys = GlobalHotkeys(self)
        handlers = {
            "answer": self.answer_dialog,
            "send": self.send,
            "toggle_listen": self.toggle_listen,
            "transcript": self.toggle_transcript,
            "screenshot": self.send_screenshot,
            "prev_page": lambda: self.flip(-1),
            "next_page": lambda: self.flip(1),
            "scroll_up": lambda: self.scroll_answer(-1),
            "scroll_down": lambda: self.scroll_answer(1),
            "toggle_window": self.toggle_window,
            "focus_input": self._focus_input,
            "settings": self.toggle_settings,
            "quit": QApplication.quit,
        }
        for action, handler in handlers.items():
            self.hotkeys.set(action, "", handler)
            self._set_hotkey(action, self.config.hotkey(action))

    def _set_hotkey(self, action: str, seq: str):
        self.settings_panel.set_hotkey_ok(action, self.hotkeys.set(action, seq))
        hint = QKeySequence(seq).toString(QKeySequence.NativeText)
        if action == "prev_page":
            self.prev_btn.setToolTip(f"Previous page  ({hint})" if hint else "Previous page")
        elif action == "next_page":
            self.next_btn.setToolTip(f"Next page  ({hint})" if hint else "Next page")

    def _set_bg_opacity(self, v: int):
        self.bg_alpha = v
        self.update()

    def _focus_input(self):
        if not self.isVisible():
            self.toggle_window()
        if self.stack.currentWidget() is not self.chat_page:
            self._show_panel(self.chat_page)
        activate_app()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    def toggle_window(self):
        """Fully hide the window (no pixels, no clicks caught) or bring it back.

        Showing doesn't activate the app, so focus stays in whatever app the user is in.
        """
        if self.isVisible():
            self.hide()
            hide_app()  # return keyboard focus to the previous app if we had it
        else:
            unhide_app()
            self.show()
            make_capture_proof(self)

    def set_capture_proof(self, ok: bool):
        """Say plainly in the title bar when the window is NOT hidden from screen sharing."""
        self.capture_proof = ok
        if not ok:
            why = ("Linux has no way to hide a window from screen capture" if IS_LINUX
                   else "this Windows version can't hide windows from capture (needs Windows 10 2004+)")
            self.title.setText("⚠ visible in screen share")
            self.title.setStyleSheet("color: #e0a040;")
            self.title.setToolTip(why)
            print(f"warning: the window is visible in screen sharing and recordings: {why}", file=sys.stderr)

    def toggle_settings(self):
        opening = self.stack.currentWidget() is not self.settings_panel
        self._show_panel(self.settings_panel if opening else self.chat_page)

    def toggle_transcript(self):
        opening = self.stack.currentWidget() is not self.transcript_panel
        self._show_panel(self.transcript_panel if opening else self.chat_page)

    def _show_panel(self, panel: QWidget):
        self.stack.setCurrentWidget(panel)
        self.chat_bar.setVisible(panel is self.chat_page)
        # Registered hotkeys would swallow key presses meant for the recorder fields.
        in_settings = panel is self.settings_panel
        self.hotkeys.set_enabled(not in_settings, keep={"settings", "toggle_window"})
        if panel is self.transcript_panel:
            self.transcript_panel.set_transcript(self.listener.transcript())
        (self.input if panel is self.chat_page else panel).setFocus()

    # ---------- listening ----------
    def toggle_listen(self):
        if self.listener.running:
            self.listener.stop()
        else:
            self.listener.start()
        self.listen_btn.setChecked(self.listener.running)

    def _on_listen_status(self, status: str):
        self.listen_btn.setChecked(self.listener.running)
        self.listen_status.setText(status)
        self.listen_status.setToolTip(status)

    def _on_transcript(self):
        if self.stack.currentWidget() is self.transcript_panel:
            self.transcript_panel.set_transcript(self.listener.transcript())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self.rect().adjusted(1, 1, -1, -1), RADIUS, RADIUS)
        # Keep a sliver of alpha so the window still receives mouse events when "fully" transparent.
        p.fillPath(path, QColor(22, 22, 26, max(2, round(self.bg_alpha * 2.55))))
        p.setPen(QColor(255, 255, 255, round(30 * self.bg_alpha / 100)))
        p.drawPath(path)

    # ---------- geometry persistence ----------
    def _restore_geometry(self):
        g = self.config.geometry()
        screens = [s.availableGeometry() for s in QGuiApplication.screens()]
        if g and any(s.intersects(g) for s in screens):
            self.setGeometry(g)
        else:
            self.resize(*DEFAULT_SIZE)

    def moveEvent(self, e):
        super().moveEvent(e)
        self._save_timer.start()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._save_timer.start()

    def save_geometry(self):
        self._save_timer.stop()
        self.config.set_geometry(self.geometry())

    def shutdown(self):
        """Clean stop (✕, the Quit hotkey, Ctrl+C, kill): the window goes away at once, then the
        phrases still being transcribed are finished and everything is written to the session folder."""
        self.save_geometry()
        self.hide()
        QApplication.processEvents()  # actually take the window off screen before the wait
        self.client.closing = True  # an answer still streaming stops here instead of holding the slot
        self.listener.shutdown()
        if self.session:
            for page in self.pages:
                if not page["done"] and page["a"]:
                    self.session.add_answer(self._kind(page) + " (unfinished)", page["q"], page["a"])
            self.session.close()
        self.client.close()

    @staticmethod
    def _kind(page) -> str:
        if not page["dialog"]:
            return "Question"
        return "Screenshot + dialog" if page["image"] is not None else "Answer from dialog"

    # ---------- pages ----------
    def send(self):
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self._ask(text)

    def send_screenshot(self):
        """⌘⇧↵: capture the display the call is on and answer from it plus the dialog.

        Typed text, if any, is an extra instruction.
        """
        text = self.input.text().strip()
        self.input.clear()
        # Where the window can't be hidden from capture (Linux), take it off screen for the shot,
        # or the model would see this window on top of the question.
        step_aside = not self.capture_proof and self.isVisible()
        if step_aside:
            self.hide()
            QApplication.processEvents()
            time.sleep(0.2)  # let the compositor redraw what was underneath
        try:
            image = capture_relevant_screen(fallback=self.screen())
        except ScreenshotError as exc:
            self._add_page(text, None, answer=f"[error] Screenshot: {exc}")
            return
        finally:
            if step_aside:
                self.show()
        self._ask(text, image, from_dialog=True)

    def answer_dialog(self):
        """⌘↵: answer the latest question in the conversation; typed text is an extra instruction."""
        text = self.input.text().strip()
        self.input.clear()
        self._ask(text, from_dialog=True)

    def _add_page(self, text, image, dialog=False, answer=None) -> int:
        self.pages.append({"q": text, "image": image, "dialog": dialog, "a": answer, "done": answer is not None})
        self.current = len(self.pages) - 1
        self._render()
        return self.current

    def _ask(self, text: str, image: bytes | None = None, from_dialog: bool = False):
        index = self._add_page(text, image, dialog=from_dialog)
        task = AskTask(self.client, self.listener, index, text, image, from_dialog)
        task.signals.chunk.connect(self._on_chunk)
        task.signals.done.connect(self._on_done)
        self._tasks.add(task)  # keep signals object alive until the reply is complete
        task.signals.done.connect(lambda *_: self._tasks.discard(task))
        self.pool.start(task)

    def _on_chunk(self, index: int, piece: str):
        page = self.pages[index]
        page["a"] = (page["a"] or "") + piece
        if index == self.current:
            self._render(keep_scroll=True)

    def _on_done(self, index: int):
        page = self.pages[index]
        page["done"] = True
        if self.session and not self.client.closing:
            self.session.add_answer(self._kind(page), page["q"], page["a"] or "")
        if index == self.current:
            self._render(keep_scroll=True)

    def scroll_answer(self, direction: int):
        """⌘↑ / ⌘↓: half a screen up or down in the answer (or in the transcript, when it's open)."""
        panel = self.stack.currentWidget()
        if panel is self.chat_page:
            self.view.scroll_half(direction)
        elif panel is self.transcript_panel:
            bar = self.transcript_panel.text.verticalScrollBar()
            bar.setValue(bar.value() + direction * max(1, bar.pageStep() // 2))

    def flip(self, step: int):
        target = self.current + step
        if 0 <= target < len(self.pages):
            self.current = target
            self._render()

    def _render(self, keep_scroll=False):
        n = len(self.pages)
        self.prev_btn.setEnabled(self.current > 0)
        self.next_btn.setEnabled(self.current < n - 1)
        self.page_label.setText(f"{self.current + 1} / {n}" if n else "0 / 0")

        self.prompt_bar.setVisible(bool(n))
        if n and self._prompt_shown_for != self.current:
            self._prompt_shown_for = self.current
            page = self.pages[self.current]
            prompt = page["q"]
            if page["dialog"]:
                kind = "📷 Screenshot + dialog" if page["image"] is not None else "🎧 Answer from dialog"
                prompt = f"{kind}{': ' + prompt if prompt else ''}"
            elif page["image"] is not None:
                prompt = f"📷 Screenshot{': ' + prompt if prompt else ''}"
            self.prompt_bar.set_text(prompt)

        if not n:
            hint = QKeySequence(self.config.hotkey("answer")).toString(QKeySequence.NativeText)
            body = (f"<p style='color:#8a8a90'>The app listens to the dialog (🎙: your microphone and "
                    f"everything the Mac plays). Press <b>{html.escape(hint)}</b> to get an answer to the "
                    f"latest question, or type a message below.</p><p style='color:#8a8a90'>Each answer "
                    f"opens a new page; use ‹ › or a two-finger swipe to move between pages.</p>")
        elif (answer := self.pages[self.current]["a"]) is None:
            page = self.pages[self.current]
            body = "<p style='color:#8a8a90'><i>{}</i></p>".format(
                "Catching the last phrase…" if page["dialog"] else "Thinking…")
        else:
            if not self.pages[self.current]["done"]:
                answer += " ▍"
            self.view.set_html(markdown_to_html(answer), keep_scroll=keep_scroll)
            return
        self.view.set_html(body)

    # ---------- edge / corner resizing ----------
    def _edges_at(self, pos: QPoint) -> Qt.Edges:
        g, corner = GRIP, GRIP * 2
        x, y, w, h = pos.x(), pos.y(), self.width(), self.height()
        edges = Qt.Edges()
        if x < g or (x < corner and (y < corner or y > h - corner)):
            edges |= Qt.LeftEdge
        if x > w - g or (x > w - corner and (y < corner or y > h - corner)):
            edges |= Qt.RightEdge
        if y < g or (y < corner and (x < corner or x > w - corner)):
            edges |= Qt.TopEdge
        if y > h - g or (y > h - corner and (x < corner or x > w - corner)):
            edges |= Qt.BottomEdge
        return edges

    @staticmethod
    def _cursor_for(edges) -> Qt.CursorShape:
        if edges in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge):
            return Qt.SizeFDiagCursor
        if edges in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge):
            return Qt.SizeBDiagCursor
        if edges & (Qt.LeftEdge | Qt.RightEdge):
            return Qt.SizeHorCursor
        if edges & (Qt.TopEdge | Qt.BottomEdge):
            return Qt.SizeVerCursor
        return Qt.ArrowCursor

    def mousePressEvent(self, e):
        edges = self._edges_at(e.position().toPoint())
        if e.button() == Qt.LeftButton and edges:
            self._resize_edges = edges
            self._press_global = e.globalPosition().toPoint()
            self._press_geom = self.geometry()

    def mouseMoveEvent(self, e):
        if not self._resize_edges:
            self.setCursor(self._cursor_for(self._edges_at(e.position().toPoint())))
            return
        d = e.globalPosition().toPoint() - self._press_global
        g = QRect(self._press_geom)
        if self._resize_edges & Qt.LeftEdge:
            g.setLeft(min(g.left() + d.x(), g.right() - MIN_W))
        if self._resize_edges & Qt.RightEdge:
            g.setRight(max(g.right() + d.x(), g.left() + MIN_W))
        if self._resize_edges & Qt.TopEdge:
            g.setTop(min(g.top() + d.y(), g.bottom() - MIN_H))
        if self._resize_edges & Qt.BottomEdge:
            g.setBottom(max(g.bottom() + d.y(), g.top() + MIN_H))
        self.setGeometry(g)

    def mouseReleaseEvent(self, _):
        self._resize_edges = Qt.Edges()

    def leaveEvent(self, _):
        if not self._resize_edges:
            self.unsetCursor()


def main():
    for stream in (sys.stdout, sys.stderr):
        # A Windows console may not be UTF-8: print "?" for a symbol it can't show instead of crashing.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if "--check" in sys.argv:
        from selfcheck import run_checks

        sys.exit(run_checks())
    app = QApplication(sys.argv)
    hide_from_dock()
    try:
        client = GigaChatClient()
    except GigaChatError as exc:
        sys.exit(f"error: {exc}")
    session = SessionLog()
    session.tee_stderr()
    print(f"session: {describe()}; recording to {session.dir}", file=sys.stderr)
    listener = DialogListener(client, session)
    win = ChatWindow(client, listener, Config(), session)
    app.aboutToQuit.connect(win.shutdown)
    _quit_on_signals(app)
    win.show()
    win.set_capture_proof(make_capture_proof(win))
    win.raise_()
    win.activateWindow()
    win.input.setFocus()
    # Token + connection ready before the first ⌘↵; listening starts right away.
    threading.Thread(target=client.warm_up, daemon=True).start()
    threading.Thread(target=warm_up_math, daemon=True).start()  # first formula would otherwise stall the UI
    QTimer.singleShot(0, win.toggle_listen)
    sys.exit(app.exec())


def _quit_on_signals(app: QApplication):
    """Ctrl+C / kill go through the same clean stop as ✕; a second Ctrl+C forces the exit."""
    def handler(signum, _frame):
        if getattr(app, "_quitting", False):
            print("forced exit", file=sys.stderr)
            os._exit(1)
        app._quitting = True
        QTimer.singleShot(0, app.quit)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    # Python only runs signal handlers between bytecodes; while Qt's event loop sits in C++,
    # a no-op timer gives it a chance to run.
    wake = QTimer(app, interval=200)
    wake.timeout.connect(lambda: None)
    wake.start()


if __name__ == "__main__":
    main()
