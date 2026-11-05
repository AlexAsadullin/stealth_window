"""Collapsible view of the user's prompt: first line by default, full text on demand."""
from PySide6.QtCore import QEasingCurve, QPointF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton, QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy

ANIM_MS = 180
MAX_EXPANDED_SHARE = 0.4  # expanded prompt may take at most this share of the window height


class ArrowButton(QAbstractButton):
    """Chevron that rotates from → (collapsed) to ↓ (expanded)."""

    def __init__(self):
        super().__init__()
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(QSize(22, 22))
        self._angle = 0.0
        self._anim = QVariantAnimation(self, duration=ANIM_MS, easingCurve=QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._set_angle)
        self.toggled.connect(self._animate)

    def set_checked_instantly(self, checked: bool):
        self.blockSignals(True)
        self.setChecked(checked)
        self.blockSignals(False)
        self._anim.stop()
        self._set_angle(90.0 if checked else 0.0)

    def _animate(self, checked):
        self._anim.stop()
        self._anim.setStartValue(self._angle)
        self._anim.setEndValue(90.0 if checked else 0.0)
        self._anim.start()

    def _set_angle(self, a):
        self._angle = float(a)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.underMouse():
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 28))
            p.drawRoundedRect(self.rect(), 6, 6)
        p.translate(self.width() / 2, self.height() / 2)
        p.rotate(self._angle)
        pen = QPen(QColor("#c9c9cf"), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        path = QPainterPath(QPointF(-2.5, -5))
        path.lineTo(2.5, 0)
        path.lineTo(-2.5, 5)
        p.drawPath(path)

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)


class PromptBar(QFrame):
    """Shows the prompt's first line; the arrow expands the full text below it.

    The arrow sits at the top-right and never moves; only the text area grows.
    """

    def __init__(self):
        super().__init__(objectName="promptBar")
        self._text = ""
        self._expanded = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 4, 4)
        lay.setSpacing(4)

        self.arrow = ArrowButton()
        self.arrow.toggled.connect(self._set_expanded)
        policy = self.arrow.sizePolicy()
        policy.setRetainSizeWhenHidden(True)  # keep the text from shifting when there's nothing to expand
        self.arrow.setSizePolicy(policy)

        self.label = QLabel(objectName="promptText")
        self.label.setTextFormat(Qt.PlainText)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # don't widen the window

        self.area = QScrollArea(widgetResizable=True)
        self.area.setFrameShape(QFrame.NoFrame)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.area.setWidget(self.label)
        lay.addWidget(self.area, 1)
        lay.addWidget(self.arrow, 0, Qt.AlignTop)

        self._height_anim = QVariantAnimation(self, duration=ANIM_MS, easingCurve=QEasingCurve.OutCubic)
        self._height_anim.valueChanged.connect(lambda h: self.area.setFixedHeight(int(h)))
        self._height_anim.finished.connect(self._on_anim_finished)

    # ---------- public ----------
    def set_text(self, text: str):
        """Show a new prompt, collapsed, without animation."""
        self._text = text
        self._expanded = False
        self._height_anim.stop()
        self.arrow.set_checked_instantly(False)
        self._show_collapsed()

    # ---------- internals ----------
    def _line_height(self) -> int:
        return self.label.fontMetrics().lineSpacing() + 2

    def _first_line(self) -> str:
        return self._text.split("\n", 1)[0]

    def _show_collapsed(self):
        self.label.setWordWrap(False)
        width = max(10, self.area.viewport().width())
        first = self._first_line()
        elided = self.label.fontMetrics().elidedText(first, Qt.ElideRight, width)
        multiline, fits = "\n" in self._text.strip(), elided == first
        self.label.setText(first + " …" if fits and multiline else elided)
        self.arrow.setVisible(multiline or not fits)
        self.area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.area.setFixedHeight(self._line_height())

    def _full_height(self) -> int:
        self.label.setWordWrap(True)
        self.label.setText(self._text)
        need = self.label.heightForWidth(self.area.viewport().width()) + 2
        cap = int(self.window().height() * MAX_EXPANDED_SHARE)
        return max(self._line_height(), min(need, cap))

    def _set_expanded(self, expanded: bool):
        self._expanded = expanded
        self._height_anim.stop()
        start = self.area.height()
        if expanded:
            end = self._full_height()
            self.area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        else:
            end = self._line_height()
            self.area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.area.verticalScrollBar().setValue(0)
        self._height_anim.setStartValue(start)
        self._height_anim.setEndValue(end)
        self._height_anim.start()

    def _on_anim_finished(self):
        if not self._expanded:
            self._show_collapsed()  # swap the wrapped text back to the elided first line

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._height_anim.state() == QVariantAnimation.Running:
            return
        if self._expanded:
            self.area.setFixedHeight(self._full_height())
        else:
            self._show_collapsed()
