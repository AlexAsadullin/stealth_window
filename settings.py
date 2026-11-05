"""Persistent settings and the in-window settings panel.

The panel lives inside the main window (not a separate dialog) so it inherits
the main window's screen-capture exclusion.
"""
from PySide6.QtCore import QRect, QSettings, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

# action id -> (label, default shortcut). In Qt on macOS "Ctrl" is ⌘, "Alt" is ⌥, "Meta" is ⌃.
# Hotkeys are system-wide and swallow the key in every other app, so defaults
# avoid common combinations (⌘L, ⌘Q, ...). "" means unassigned.
ACTIONS = {
    "answer": ("Answer from dialog", "Ctrl+Return"),
    "send": ("Send typed message", ""),  # Return in the input field always sends
    "toggle_listen": ("Start / stop listening", "Ctrl+Alt+M"),
    "transcript": ("Show / hide transcript", "Ctrl+Alt+T"),
    "screenshot": ("Screenshot + dialog", "Ctrl+Shift+Return"),
    "prev_page": ("Previous page", "Ctrl+Left"),
    "next_page": ("Next page", "Ctrl+Right"),
    "scroll_up": ("Scroll answer up", "Ctrl+Up"),  # by half the visible text
    "scroll_down": ("Scroll answer down", "Ctrl+Down"),
    "toggle_window": ("Hide / show window", "Ctrl+Shift+H"),
    "focus_input": ("Focus input", "Ctrl+Alt+L"),
    "settings": ("Open / close settings", "Ctrl+Alt+,"),
    "quit": ("Quit", ""),
}
HOTKEYS_VERSION = 4  # bump when defaults change in a way old saved values should not survive

DEFAULT_WINDOW_OPACITY = 100  # %, whole window including text
DEFAULT_BG_OPACITY = 88  # %, background panel only


class Config:
    def __init__(self):
        self.s = QSettings("stealth_window", "StealthChat")
        version = self.s.value("hotkeys/version", 1, type=int)
        if version < 2:
            # v1 hotkeys were in-app only; as global ones they would hijack ⌘Q, ⌘L, ...
            self.s.remove("hotkeys")
        elif version < 3 and self.s.value("hotkeys/toggle_window", type=str) == "Ctrl+\\":
            self.s.remove("hotkeys/toggle_window")  # v2 default; v3 moved it to ⌘⇧H
        if version < 4 and self.s.value("hotkeys/send", type=str) == "Ctrl+Return":
            self.s.remove("hotkeys/send")  # v4: ⌘↵ answers from the dialog instead
        self.s.setValue("hotkeys/version", HOTKEYS_VERSION)

    def hotkey(self, action: str) -> str:
        return self.s.value(f"hotkeys/{action}", ACTIONS[action][1], type=str)

    def set_hotkey(self, action: str, seq: str):
        self.s.setValue(f"hotkeys/{action}", seq)

    def window_opacity(self) -> int:
        return self.s.value("appearance/window_opacity", DEFAULT_WINDOW_OPACITY, type=int)

    def set_window_opacity(self, v: int):
        self.s.setValue("appearance/window_opacity", v)

    def bg_opacity(self) -> int:
        return self.s.value("appearance/bg_opacity", DEFAULT_BG_OPACITY, type=int)

    def set_bg_opacity(self, v: int):
        self.s.setValue("appearance/bg_opacity", v)

    def system_prompt(self, default: str) -> str:
        return self.s.value("assistant/system_prompt", default, type=str) or default

    def set_system_prompt(self, text: str):
        self.s.setValue("assistant/system_prompt", text)

    def geometry(self) -> QRect | None:
        g = self.s.value("window/geometry")
        return g if isinstance(g, QRect) and g.isValid() else None

    def set_geometry(self, g: QRect):
        self.s.setValue("window/geometry", g)


class SettingsPanel(QWidget):
    hotkey_changed = Signal(str, str)  # action, portable key sequence
    window_opacity_changed = Signal(int)
    bg_opacity_changed = Signal(int)
    system_prompt_changed = Signal(str)
    closed = Signal()

    def __init__(self, config: Config, default_prompt: str):
        super().__init__()
        self.config = config
        self.default_prompt = default_prompt

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(widgetResizable=True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget(objectName="content")
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        lay = QVBoxLayout(body)
        lay.setContentsMargins(4, 4, 10, 4)
        lay.setSpacing(10)

        lay.addWidget(QLabel("Transparency", objectName="section"))
        look = self._form()
        self.win_slider = self._slider(20, 100, config.window_opacity(), self._on_window_opacity)
        self.bg_slider = self._slider(0, 100, config.bg_opacity(), self._on_bg_opacity)
        look.addRow("Window", self._with_value(self.win_slider))
        look.addRow("Background", self._with_value(self.bg_slider))
        lay.addLayout(look)

        prompt_head = QHBoxLayout()
        prompt_head.addWidget(QLabel("System prompt", objectName="section"))
        prompt_head.addStretch()
        self.prompt_reset = QPushButton("↺", objectName="resetKey", toolTip="Restore the default prompt")
        self.prompt_reset.clicked.connect(lambda: self.prompt_edit.setPlainText(self.default_prompt))
        prompt_head.addWidget(self.prompt_reset)
        lay.addLayout(prompt_head)
        self.prompt_edit = QPlainTextEdit(objectName="transcript")
        self.prompt_edit.setPlainText(config.system_prompt(default_prompt))
        self.prompt_edit.setMinimumHeight(140)
        self.prompt_edit.textChanged.connect(self._on_prompt)
        self.prompt_reset.setEnabled(self.prompt_edit.toPlainText() != default_prompt)
        lay.addWidget(self.prompt_edit)
        lay.addWidget(QLabel("Sent with every request: who you are, the topic of the call, how long and in "
                             "what style answers should be. Applies from the next answer.",
                             objectName="hint", wordWrap=True))

        lay.addWidget(QLabel("Hotkeys", objectName="section"))
        keys = self._form()
        self.key_edits: dict[str, QKeySequenceEdit] = {}
        self.key_warnings: dict[str, QLabel] = {}
        self.key_resets: dict[str, QPushButton] = {}
        for action, (label, default) in ACTIONS.items():
            edit = QKeySequenceEdit(QKeySequence(config.hotkey(action)))
            edit.setMaximumSequenceLength(1)
            edit.keySequenceChanged.connect(lambda seq, a=action: self._on_hotkey(a, seq))
            native_default = QKeySequence(default).toString(QKeySequence.NativeText) or "none"
            reset = QPushButton("↺", objectName="resetKey", toolTip=f"Restore default ({native_default})")
            reset.clicked.connect(lambda _=False, e=edit, d=default: e.setKeySequence(QKeySequence(d)))
            reset.setEnabled(config.hotkey(action) != default)
            warn = QLabel("⚠", objectName="warn",
                          toolTip="Could not register: another app already uses this combination, "
                                  "or the key is not supported.")
            warn.setVisible(False)
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            row.addWidget(reset)
            row.addWidget(warn)
            self.key_resets[action] = reset
            self.key_edits[action] = edit
            self.key_warnings[action] = warn
            keys.addRow(label, row)
        lay.addLayout(keys)

        hint = QLabel("Hotkeys work from any app and any desktop, even when this window is not focused. "
                      "Click a field and press the new key combination; ↺ restores the default.",
                      objectName="hint", wordWrap=True)
        lay.addWidget(hint)
        lay.addStretch()

        buttons = QHBoxLayout()
        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(self.reset_defaults)
        done = QPushButton("Done")
        done.clicked.connect(self.closed)
        buttons.addWidget(reset)
        buttons.addStretch()
        buttons.addWidget(done)
        outer.addLayout(buttons)

    @staticmethod
    def _form() -> QFormLayout:
        # macOS defaults to fixed-width, centered fields; stretch them instead.
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setLabelAlignment(Qt.AlignLeft)
        return form

    @staticmethod
    def _slider(lo, hi, value, slot) -> QSlider:
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(value)
        s.valueChanged.connect(slot)
        return s

    @staticmethod
    def _with_value(slider: QSlider) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        value = QLabel(f"{slider.value()}%", objectName="hint")
        value.setFixedWidth(40)
        slider.valueChanged.connect(lambda v: value.setText(f"{v}%"))
        row.addWidget(slider, 1)
        row.addWidget(value)
        return w

    def _on_window_opacity(self, v):
        self.config.set_window_opacity(v)
        self.window_opacity_changed.emit(v)

    def _on_bg_opacity(self, v):
        self.config.set_bg_opacity(v)
        self.bg_opacity_changed.emit(v)

    def _on_prompt(self):
        text = self.prompt_edit.toPlainText()
        self.config.set_system_prompt(text)
        self.prompt_reset.setEnabled(text != self.default_prompt)
        self.system_prompt_changed.emit(text.strip() or self.default_prompt)

    def _on_hotkey(self, action, seq: QKeySequence):
        text = seq.toString(QKeySequence.PortableText)
        self.config.set_hotkey(action, text)
        self.key_resets[action].setEnabled(text != ACTIONS[action][1])
        self.hotkey_changed.emit(action, text)

    def set_hotkey_ok(self, action: str, ok: bool):
        self.key_warnings[action].setVisible(not ok)

    def reset_defaults(self):
        self.win_slider.setValue(DEFAULT_WINDOW_OPACITY)
        self.bg_slider.setValue(DEFAULT_BG_OPACITY)
        self.prompt_edit.setPlainText(self.default_prompt)
        for action, (_, default) in ACTIONS.items():
            self.key_edits[action].setKeySequence(QKeySequence(default))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.closed.emit()
        else:
            super().keyPressEvent(e)
