"""In-window view of the live dialog transcript.

Like the settings panel, it lives inside the main window so it inherits the
window's screen-capture exclusion.
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget


class TranscriptPanel(QWidget):
    closed = Signal()
    clear_requested = Signal()

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        lay.addWidget(QLabel("Dialog transcript", objectName="section"))
        self.text = QPlainTextEdit(objectName="transcript", readOnly=True)
        self.text.setPlaceholderText("Nothing heard yet. «Я» is your microphone, «Собеседник» is "
                                     "everything the Mac plays (calls, videos, music).")
        lay.addWidget(self.text, 1)

        buttons = QHBoxLayout()
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear_requested)
        done = QPushButton("Done")
        done.clicked.connect(self.closed)
        buttons.addWidget(clear)
        buttons.addStretch()
        buttons.addWidget(done)
        lay.addLayout(buttons)

    def set_transcript(self, text: str):
        if text == self.text.toPlainText():
            return
        bar = self.text.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 4  # keep following new lines unless the user scrolled up
        pos = bar.value()
        self.text.setPlainText(text)
        bar.setValue(bar.maximum() if follow else pos)
